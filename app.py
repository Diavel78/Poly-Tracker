#!/usr/bin/env python3
"""Polymarket US Position Tracker — Web Dashboard

Flask app with session-based login that displays Polymarket US positions,
P&L, balances, and trade history in a secured web dashboard.
"""

import os
import re
import sys
import secrets
import functools
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, jsonify,
)

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
POLYMARKET_KEY_ID = os.getenv("POLYMARKET_KEY_ID", "")
POLYMARKET_SECRET_KEY = os.getenv("POLYMARKET_SECRET_KEY", "")
KALSHI_API_KEY = os.getenv("KALSHI_API_KEY", "")
KALSHI_PRIVATE_KEY = os.getenv("KALSHI_PRIVATE_KEY", "")
DASHBOARD_USER = os.getenv("DASHBOARD_USER", "")
DASHBOARD_PASS = os.getenv("DASHBOARD_PASS", "")

if not DASHBOARD_USER or not DASHBOARD_PASS:
    print("ERROR: DASHBOARD_USER and DASHBOARD_PASS must be set in .env")
    sys.exit(1)

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", secrets.token_hex(32))
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def login_required(f):
    """Decorator: redirect to login if not authenticated."""
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# Polymarket SDK client (reuse from pm_tracker)
# ---------------------------------------------------------------------------

def get_client():
    """Return an authenticated PolymarketUS client."""
    from polymarket_us import PolymarketUS

    if not POLYMARKET_KEY_ID or not POLYMARKET_SECRET_KEY:
        raise RuntimeError("Polymarket API credentials not configured")
    return PolymarketUS(key_id=POLYMARKET_KEY_ID, secret_key=POLYMARKET_SECRET_KEY)


def _safe_float(val):
    """Extract a float from a value, handling Amount dicts like {"value": "1.23", "currency": "USD"}."""
    if val is None:
        return None
    if isinstance(val, dict) and "value" in val:
        val = val["value"]
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _get(obj, *keys, default=None):
    """Get first matching key from a dict."""
    for key in keys:
        if isinstance(obj, dict) and key in obj:
            return obj[key]
    return default


# ---------------------------------------------------------------------------
# Data fetching — SDK returns plain dicts
# ---------------------------------------------------------------------------

def fetch_positions(client):
    """Returns list of (slug, position_dict) tuples."""
    try:
        response = client.portfolio.positions()
        positions_map = response.get("positions", {})
        return list(positions_map.items())
    except Exception as e:
        print(f"ERROR fetching positions: {e}")
        return []


def fetch_market_price(client, market_slug):
    try:
        bbo = client.markets.bbo(market_slug)
        best_bid = _safe_float(bbo.get("bestBidPrice") or bbo.get("bid"))
        best_ask = _safe_float(bbo.get("bestAskPrice") or bbo.get("ask"))
        if best_bid is not None and best_ask is not None:
            return (best_bid + best_ask) / 2
        return best_bid or best_ask
    except Exception:
        return None


def fetch_market(client, slug_or_id):
    try:
        return client.markets.retrieve_by_slug(slug_or_id)
    except Exception:
        try:
            return client.markets.retrieve(slug_or_id)
        except Exception:
            return None


def fetch_activities(client, max_pages=20):
    """Fetch all activities using cursor-based pagination."""
    all_activities = []
    cursor = None
    try:
        for _ in range(max_pages):
            params = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            response = client.portfolio.activities(params=params)
            activities = response.get("activities", [])
            all_activities.extend(activities)
            if response.get("eof", True) or not response.get("nextCursor"):
                break
            cursor = response.get("nextCursor")
    except Exception as e:
        print(f"ERROR fetching activities: {e}")
    return all_activities


def fetch_balances(client):
    try:
        response = client.account.balances()
        bal_list = response.get("balances", [])
        if bal_list:
            return bal_list[0]
        return None
    except Exception as e:
        print(f"ERROR fetching balances: {e}")
        return None


def enrich_positions(client, positions):
    """positions is a list of (slug, pos_dict) tuples from the SDK."""
    enriched = []
    for slug, pos in positions:
        metadata = pos.get("marketMetadata", {})
        market_name = metadata.get("title") or metadata.get("question") or slug
        market_slug = metadata.get("slug") or slug
        event_slug = metadata.get("eventSlug") or ""
        raw_outcome = metadata.get("outcome") or ""
        team = metadata.get("team") or {}
        team_name = team.get("name", "") if isinstance(team, dict) else ""

        # Fetch full market detail for extra context (question field, outcomes list)
        market_detail = fetch_market(client, market_slug)
        md = {}
        if market_detail and isinstance(market_detail, dict):
            md = market_detail.get("market", market_detail)

        # The question field often has the full line description
        # e.g., "Spread: BOS Bruins (+1.5)" or "Total: Over/Under 6.5"
        question = md.get("question", "")
        outcomes_list = md.get("outcomes", [])

        # Derive meaningful pick label with line info:
        # raw_outcome has the line value (e.g., "-1.50") or label (e.g., "Over")
        # team_name has the team (e.g., "Sabres")
        if team_name and raw_outcome and re.search(r'[0-9]', raw_outcome):
            # Spread/line: combine team + line (e.g., "Sabres -1.50")
            outcome = f"{team_name} {raw_outcome}"
        elif raw_outcome.lower() in ("over", "under") and question:
            # O/U: extract total from question (e.g., "Total: Over/Under 6.5")
            total_match = re.search(r'(\d+\.?\d*)', question)
            if total_match:
                outcome = f"{raw_outcome} {total_match.group(1)}"
            else:
                outcome = raw_outcome
        elif team_name:
            outcome = team_name
        elif raw_outcome.lower() not in ("yes", "no", ""):
            outcome = raw_outcome
        elif event_slug and market_slug.startswith(event_slug + "-"):
            suffix = market_slug[len(event_slug) + 1:]
            outcome = suffix.replace("-", " ").title()
        else:
            outcome = ""

        net_position = _safe_float(pos.get("netPosition")) or 0
        quantity = abs(net_position)
        side = "YES" if net_position >= 0 else "NO"

        cost = _safe_float(pos.get("cost"))
        entry_price = (cost / quantity) if cost is not None and quantity > 0 else None

        cash_value = _safe_float(pos.get("cashValue"))
        realized = _safe_float(pos.get("realized"))

        current_price = None
        if market_slug:
            current_price = fetch_market_price(client, market_slug)
        if current_price is None:
            current_price = (cash_value / quantity) if cash_value is not None and quantity > 0 else None

        current_value = cash_value if cash_value is not None else (
            quantity * current_price if current_price is not None and quantity else None
        )

        pnl = None
        pnl_pct = None
        if current_value is not None and cost is not None:
            pnl = current_value - cost
            if realized is not None:
                pnl += realized
            if cost > 0:
                pnl_pct = (pnl / cost) * 100
        elif realized is not None:
            pnl = realized

        expired = pos.get("expired", False)

        enriched.append({
            "platform": "polymarket",
            "market_name": market_name,
            "market_slug": market_slug,
            "outcome": outcome,
            "side": side,
            "quantity": quantity,
            "entry_price": entry_price,
            "current_price": current_price,
            "current_value": current_value,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "expired": expired,
        })

    return enriched


def compute_summary(enriched, parsed_activities, tz_offset_minutes=0):
    """Compute summary stats from open positions + resolved activity P&L.

    tz_offset_minutes: client's timezone offset from UTC in minutes
    (e.g., MST = -420, EST = -300). Matches JS getTimezoneOffset().
    """
    total_invested = 0.0
    total_current = 0.0
    open_pnl = 0.0

    for p in enriched:
        if p["entry_price"] is not None and p["quantity"]:
            total_invested += p["quantity"] * p["entry_price"]
        if p["current_value"] is not None:
            total_current += p["current_value"]
        if p["pnl"] is not None:
            open_pnl += p["pnl"]

    # Compute realized P&L from resolved positions in activity history
    realized_pnl = 0.0
    resolved_wins = 0
    resolved_total = 0
    today_pnl = 0.0
    yesterday_pnl = 0.0

    # Use client's timezone for today/yesterday boundaries
    # JS getTimezoneOffset() returns minutes AHEAD of UTC (MST = 420),
    # so we subtract to get local time
    client_tz = timezone(timedelta(minutes=-tz_offset_minutes))
    now_local = datetime.now(client_tz)
    today_str = now_local.strftime("%Y-%m-%d")
    yesterday_str = (now_local - timedelta(days=1)).strftime("%Y-%m-%d")

    for act in parsed_activities:
        if act["type"] == "Position Resolution" and act["pnl"] is not None:
            realized_pnl += act["pnl"]
            resolved_total += 1
            if act["pnl"] > 0:
                resolved_wins += 1

            # Convert activity timestamp (UTC) to client's local date
            ts = act.get("timestamp", "")
            act_local = ""
            if ts:
                try:
                    # Normalize: "2026-03-26 03:14:50" → "2026-03-26T03:14:50"
                    ts_norm = str(ts).replace(" ", "T").replace("Z", "+00:00")
                    act_dt = datetime.fromisoformat(ts_norm)
                    if act_dt.tzinfo is None:
                        act_dt = act_dt.replace(tzinfo=timezone.utc)
                    act_local = act_dt.astimezone(client_tz).strftime("%Y-%m-%d")
                except (ValueError, TypeError):
                    act_local = ""

            if act_local == today_str:
                today_pnl += act["pnl"]
            elif act_local == yesterday_str:
                yesterday_pnl += act["pnl"]

    total_pnl = open_pnl + realized_pnl
    win_rate = (resolved_wins / resolved_total * 100) if resolved_total > 0 else None

    return {
        "total_positions": len([p for p in enriched if not p.get("expired")]),
        "total_invested": total_invested,
        "total_current": total_current,
        "total_pnl": total_pnl,
        "open_pnl": open_pnl,
        "realized_pnl": realized_pnl,
        "today_pnl": today_pnl,
        "yesterday_pnl": yesterday_pnl,
        "resolved_total": resolved_total,
        "resolved_wins": resolved_wins,
        "win_rate": win_rate,
        "_debug_tz": {
            "tz_offset_minutes": tz_offset_minutes,
            "today_str": today_str,
            "yesterday_str": yesterday_str,
            "now_local": now_local.isoformat(),
        },
    }


def parse_balances(balances):
    """Extract balance fields from a UserBalance dict."""
    if not isinstance(balances, dict):
        return {}
    return {
        "current_balance": _safe_float(balances.get("currentBalance")),
        "buying_power": _safe_float(balances.get("buyingPower")),
        "open_orders": _safe_float(balances.get("openOrders")),
        "unsettled": _safe_float(balances.get("unsettledFunds")),
    }


def _resolve_market_title(client, slug):
    """Look up a market title by slug."""
    try:
        market = client.markets.retrieve_by_slug(slug)
        return market.get("title", "") or market.get("question", "") or slug
    except Exception:
        # Fall back to making the slug readable
        return slug.replace("-", " ").replace("aec ", "").replace("asc ", "").title()


def _activity_type_label(raw_type):
    """Convert ACTIVITY_TYPE_POSITION_RESOLUTION -> Resolution, etc."""
    label = raw_type.replace("ACTIVITY_TYPE_", "").replace("_", " ").title()
    return label or raw_type


def parse_activities(client, activities):
    """Convert activity dicts for the template.

    Activity types and their detail keys:
      ACTIVITY_TYPE_POSITION_RESOLUTION -> positionResolution
      ACTIVITY_TYPE_TRADE -> trade
      ACTIVITY_TYPE_ACCOUNT_BALANCE_CHANGE -> accountBalanceChange
    """
    # Map type enum to the detail key in the activity dict
    TYPE_KEY_MAP = {
        "ACTIVITY_TYPE_POSITION_RESOLUTION": "positionResolution",
        "ACTIVITY_TYPE_TRADE": "trade",
        "ACTIVITY_TYPE_ACCOUNT_BALANCE_CHANGE": "accountBalanceChange",
    }

    # Cache for slug -> market title lookups
    slug_to_title = {}

    parsed = []
    for act in activities:
        act_type = act.get("type", "unknown")
        detail_key = TYPE_KEY_MAP.get(act_type, "")
        detail = act.get(detail_key, {}) if detail_key else {}

        timestamp = detail.get("updateTime") or detail.get("timestamp") or ""
        market_slug = detail.get("marketSlug", "")

        market = ""
        side = ""
        price = None
        quantity = None
        pnl = None

        if act_type == "ACTIVITY_TYPE_TRADE":
            price = _safe_float(detail.get("price"))
            quantity = _safe_float(detail.get("qty"))
            # SDK's realizedPnl: null for buys, non-null for sells/closes
            sdk_rpnl = _safe_float(detail.get("realizedPnl"))
            is_close = sdk_rpnl is not None
            pnl = None  # computed in post-processing pass below

            # Resolve market name from slug
            if market_slug:
                if market_slug not in slug_to_title:
                    slug_to_title[market_slug] = _resolve_market_title(client, market_slug)
                market = slug_to_title[market_slug]

        elif act_type == "ACTIVITY_TYPE_POSITION_RESOLUTION":
            before = detail.get("beforePosition", {})
            after = detail.get("afterPosition", {})
            meta = before.get("marketMetadata", {}) or after.get("marketMetadata", {})
            market = meta.get("title", "")
            # Cache it for trade lookups
            if market_slug and market:
                slug_to_title[market_slug] = market

            side = detail.get("side", "")
            side = side.replace("POSITION_RESOLUTION_SIDE_", "")

            quantity = abs(_safe_float(before.get("netPosition")) or 0)
            cost = _safe_float(before.get("cost"))
            if cost is not None and quantity > 0:
                price = cost / quantity

            # Compute P&L: use difference in realized between before/after,
            # or fall back to computing from position direction and cost
            before_realized = _safe_float(before.get("realized")) or 0
            after_realized = _safe_float(after.get("realized")) or 0
            pnl_diff = after_realized - before_realized
            if abs(pnl_diff) > 0.001:
                pnl = pnl_diff
            elif cost is not None:
                # Determine if position won:
                # SDK side can be YES/NO or LONG/SHORT after prefix strip
                # LONG = YES side won, SHORT = NO side won
                net = _safe_float(before.get("netPosition")) or 0
                held_yes = net > 0
                yes_won = side in ("YES", "LONG")
                no_won = side in ("NO", "SHORT")
                won = (held_yes and yes_won) or (not held_yes and no_won)
                if won:
                    pnl = quantity - cost  # payout is $1 * qty
                else:
                    pnl = -cost  # total loss

        elif act_type == "ACTIVITY_TYPE_ACCOUNT_BALANCE_CHANGE":
            amount = _safe_float(detail.get("amount"))
            reason = detail.get("reason", "")
            market = reason.replace("_", " ").title() if reason else "Balance Change"
            pnl = amount

        # Format timestamp
        if timestamp and "T" in str(timestamp):
            timestamp = str(timestamp).replace("T", " ")[:19]

        parsed.append({
            "platform": "polymarket",
            "timestamp": str(timestamp),
            "market": str(market) or market_slug,
            "_market_slug": market_slug,
            "_is_close": is_close if act_type == "ACTIVITY_TYPE_TRADE" else False,
            "side": str(side),
            "price": price,
            "quantity": quantity,
            "type": _activity_type_label(act_type),
            "pnl": pnl,
        })

    # Post-process: compute trade P&L from tracked average cost per slug.
    # SDK's realizedPnl and costBasis are unreliable, so we track it ourselves.
    # Activities come newest-first; process chronologically (reversed).
    slug_positions = {}  # slug -> {"qty": float, "total_cost": float}
    trade_indices = []   # indices of Trade activities (in chronological order)

    for i in range(len(parsed) - 1, -1, -1):
        act = parsed[i]
        if act["type"] != "Trade":
            continue
        slug = act["_market_slug"]
        if not slug or act["price"] is None or not act["quantity"]:
            continue

        if slug not in slug_positions:
            slug_positions[slug] = {"qty": 0.0, "total_cost": 0.0}
        pos = slug_positions[slug]

        if not act["_is_close"]:
            # Buy/open: accumulate cost
            pos["qty"] += act["quantity"]
            pos["total_cost"] += act["price"] * act["quantity"]
        else:
            # Sell/close: compute P&L from tracked average cost
            if pos["qty"] > 0:
                avg_cost = pos["total_cost"] / pos["qty"]
                act["pnl"] = round((act["price"] - avg_cost) * act["quantity"], 2)
                pos["total_cost"] -= avg_cost * act["quantity"]
                pos["qty"] -= act["quantity"]
            else:
                act["pnl"] = None

    # Strip internal fields before returning
    for act in parsed:
        act.pop("_market_slug", None)
        act.pop("_is_close", None)

    return parsed


# ---------------------------------------------------------------------------
# Kalshi integration
# ---------------------------------------------------------------------------

def get_kalshi_client():
    """Return an authenticated Kalshi client, or None if not configured."""
    if not KALSHI_API_KEY or not KALSHI_PRIVATE_KEY:
        return None
    try:
        from kalshi_python_sync import KalshiClient, Configuration
        config = Configuration()
        config.api_key_id = KALSHI_API_KEY
        pem = KALSHI_PRIVATE_KEY.replace("\\n", "\n")
        config.private_key_pem = pem
        return KalshiClient(configuration=config)
    except Exception as e:
        print(f"ERROR creating Kalshi client: {e}")
        return None


def kalshi_fetch_positions(kclient):
    """Fetch open positions from Kalshi."""
    try:
        resp = kclient.get_positions(
            settlement_status="unsettled",
            count_filter="has_value"
        )
        return resp.get("market_positions", [])
    except Exception as e:
        print(f"ERROR fetching Kalshi positions: {e}")
        return []


def kalshi_fetch_settled(kclient):
    """Fetch settled positions from Kalshi."""
    try:
        resp = kclient.get_positions(
            settlement_status="settled",
            count_filter="has_value"
        )
        return resp.get("market_positions", [])
    except Exception as e:
        print(f"ERROR fetching Kalshi settled: {e}")
        return []


def kalshi_fetch_balance(kclient):
    """Fetch Kalshi account balance (returns cents)."""
    try:
        resp = kclient.get_balance()
        return resp.get("balance", 0) / 100.0
    except Exception as e:
        print(f"ERROR fetching Kalshi balance: {e}")
        return 0.0


def kalshi_fetch_fills(kclient, limit=500):
    """Fetch recent fills (trades) from Kalshi."""
    try:
        resp = kclient.get_fills(limit=limit)
        return resp.get("fills", [])
    except Exception as e:
        print(f"ERROR fetching Kalshi fills: {e}")
        return []


def kalshi_fetch_market(kclient, ticker):
    """Fetch market details for a Kalshi ticker."""
    try:
        resp = kclient.get_market(ticker)
        return resp.get("market", {})
    except Exception as e:
        print(f"ERROR fetching Kalshi market {ticker}: {e}")
        return {}


def kalshi_enrich_positions(kclient, positions):
    """Convert Kalshi positions to the same enriched format as Polymarket."""
    enriched = []
    for pos in positions:
        ticker = pos.get("ticker", "")
        market = kalshi_fetch_market(kclient, ticker)

        market_name = market.get("title", ticker)
        subtitle = market.get("subtitle", "")

        # Position qty and cost
        quantity = abs(pos.get("position", 0))
        if quantity == 0:
            continue

        # Kalshi prices are in cents
        market_exposure = pos.get("market_exposure", 0) / 100.0
        entry_price = (market_exposure / quantity) if quantity > 0 else None

        # Current price from market mid
        yes_bid = market.get("yes_bid", 0) / 100.0
        yes_ask = market.get("yes_ask", 0) / 100.0
        current_price = (yes_bid + yes_ask) / 2 if (yes_bid + yes_ask) > 0 else None

        current_value = quantity * current_price if current_price and quantity else None

        pnl = None
        pnl_pct = None
        if current_value is not None and market_exposure > 0:
            pnl = current_value - market_exposure
            pnl_pct = (pnl / market_exposure) * 100

        enriched.append({
            "platform": "kalshi",
            "market_name": market_name,
            "market_slug": ticker,
            "outcome": subtitle or "",
            "side": "YES" if pos.get("position", 0) > 0 else "NO",
            "quantity": quantity,
            "entry_price": entry_price,
            "current_price": current_price,
            "current_value": current_value,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "expired": False,
        })

    return enriched


def kalshi_parse_fills(kclient, fills):
    """Convert Kalshi fills to activity format matching Polymarket activities."""
    # Cache market titles
    ticker_titles = {}
    parsed = []

    for fill in fills:
        ticker = fill.get("ticker", "")
        if ticker not in ticker_titles:
            m = kalshi_fetch_market(kclient, ticker)
            ticker_titles[ticker] = m.get("title", ticker)

        action = fill.get("action", "")  # "buy" or "sell"
        side = fill.get("side", "")  # "yes" or "no"
        count = fill.get("count", 0)
        price_cents = fill.get("yes_price", 0)
        price = price_cents / 100.0

        timestamp = fill.get("created_time", "")
        # Kalshi timestamps: "2026-03-26T03:14:50Z" → "2026-03-26 03:14:50"
        if "T" in str(timestamp):
            timestamp = str(timestamp).replace("T", " ").replace("Z", "")[:19]

        label = f"{action.title()} {side.upper()}" if action and side else "Trade"

        parsed.append({
            "platform": "kalshi",
            "timestamp": str(timestamp),
            "market": ticker_titles.get(ticker, ticker),
            "side": side.upper() if side else "",
            "price": price,
            "quantity": count,
            "type": label,
            "pnl": None,
        })

    return parsed


def kalshi_parse_settled(kclient, settled_positions):
    """Convert settled Kalshi positions to closed position activity format."""
    closed = []
    for pos in settled_positions:
        ticker = pos.get("ticker", "")
        market = kalshi_fetch_market(kclient, ticker)
        market_name = market.get("title", ticker)

        quantity = abs(pos.get("position", 0))
        if quantity == 0:
            continue

        market_exposure = pos.get("market_exposure", 0) / 100.0
        entry_price = (market_exposure / quantity) if quantity > 0 else None

        settlement = pos.get("settlement_value", 0) / 100.0
        payout = quantity * settlement
        pnl = payout - market_exposure if market_exposure > 0 else None

        closed.append({
            "platform": "kalshi",
            "timestamp": pos.get("settlement_time", ""),
            "market": market_name,
            "side": "",
            "price": entry_price,
            "quantity": quantity,
            "type": "Position Resolution",
            "pnl": pnl,
        })

    return closed


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("authenticated"):
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if secrets.compare_digest(username, DASHBOARD_USER) and secrets.compare_digest(password, DASHBOARD_PASS):
            session["authenticated"] = True
            session.permanent = True
            return redirect(url_for("dashboard"))
        flash("Invalid credentials", "error")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def dashboard():
    return render_template("dashboard.html")


@app.route("/api/raw")
@login_required
def api_raw():
    """Debug endpoint: dump raw SDK responses to see exact structure."""
    try:
        client = get_client()
    except Exception as e:
        return jsonify({"error": f"Client init: {e}"}), 500

    raw = {}
    for name, call in [
        ("positions", lambda: client.portfolio.positions()),
        ("balances", lambda: client.account.balances()),
        ("activities", lambda: client.portfolio.activities()),
    ]:
        try:
            result = call()
            raw[name] = result
        except Exception as e:
            raw[name] = {"_error": str(e), "_type": type(e).__name__}

    return jsonify(raw)


@app.route("/api/debug-kalshi")
@login_required
def api_debug_kalshi():
    """Debug: show raw Kalshi API responses."""
    try:
        debug = {
            "has_api_key": bool(KALSHI_API_KEY),
            "api_key_preview": KALSHI_API_KEY[:8] + "..." if KALSHI_API_KEY else "",
            "has_private_key": bool(KALSHI_PRIVATE_KEY),
            "private_key_length": len(KALSHI_PRIVATE_KEY),
            "private_key_starts": KALSHI_PRIVATE_KEY[:30] if KALSHI_PRIVATE_KEY else "",
        }

        # Check if package can be imported
        try:
            import kalshi_python_sync
            debug["package"] = "imported OK"
            debug["package_version"] = getattr(kalshi_python_sync, "__version__", "unknown")
        except Exception as e:
            debug["package"] = f"IMPORT FAILED: {type(e).__name__}: {e}"
            return jsonify(debug)

        try:
            kclient = get_kalshi_client()
        except Exception as e:
            debug["client"] = f"FAILED: {type(e).__name__}: {e}"
            return jsonify(debug)

        if not kclient:
            debug["client"] = "None — credentials not detected or client creation failed"
            return jsonify(debug)

        debug["client"] = "OK"

        for name, call in [
            ("balance", lambda: kclient.get_balance()),
            ("positions_unsettled", lambda: kclient.get_positions(settlement_status="unsettled", count_filter="has_value")),
            ("positions_settled", lambda: kclient.get_positions(settlement_status="settled", count_filter="has_value")),
            ("fills", lambda: kclient.get_fills(limit=5)),
        ]:
            try:
                debug[name] = call()
            except Exception as e:
                debug[name] = {"_error": str(e), "_type": type(e).__name__}

        return jsonify(debug)
    except Exception as e:
        return jsonify({"fatal_error": f"{type(e).__name__}: {e}"})


@app.route("/api/debug-markets")
@login_required
def api_debug_markets():
    """Debug: show raw market detail for each open position."""
    try:
        client = get_client()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    try:
        positions_resp = client.portfolio.positions()
        positions_map = positions_resp.get("positions", {})
    except Exception as e:
        return jsonify({"error": f"positions: {e}"}), 500

    results = []
    for slug, pos in positions_map.items():
        metadata = pos.get("marketMetadata", {})
        market_slug = metadata.get("slug") or slug
        try:
            market_detail = fetch_market(client, market_slug)
        except Exception as e:
            market_detail = {"_error": str(e)}
        results.append({
            "slug": slug,
            "marketMetadata": metadata,
            "marketDetail": market_detail,
        })
    return jsonify(results)


@app.route("/api/data")
@login_required
def api_data():
    """JSON endpoint that fetches all platform data for the dashboard."""
    errors = []
    now = datetime.now(timezone.utc)

    # --- Polymarket ---
    enriched = []
    parsed_acts = []
    pm_balance = 0.0
    positions = []

    try:
        client = get_client()

        try:
            positions = fetch_positions(client)
        except Exception as e:
            errors.append(f"pm positions: {e}")

        try:
            enriched = enrich_positions(client, positions)
        except Exception as e:
            errors.append(f"pm enrich: {e}")

        activities = []
        try:
            activities = fetch_activities(client)
        except Exception as e:
            errors.append(f"pm activities: {e}")

        balances = None
        try:
            balances = fetch_balances(client)
        except Exception as e:
            errors.append(f"pm balances: {e}")

        parsed_acts = parse_activities(client, activities)
        bal = parse_balances(balances)
        pm_balance = bal.get("current_balance") or 0.0

    except Exception as e:
        errors.append(f"pm client: {e}")
        bal = {}

    # Exclude activity before 2026-03-01 (prior data was arb trading, not legit bets)
    CUTOFF_DATE = "2026-03-01"
    parsed_acts = [a for a in parsed_acts if a.get("timestamp", "") >= CUTOFF_DATE]

    # --- Kalshi ---
    kalshi_enriched = []
    kalshi_acts = []
    kalshi_balance = 0.0

    kclient = get_kalshi_client()
    if kclient:
        try:
            k_positions = kalshi_fetch_positions(kclient)
            kalshi_enriched = kalshi_enrich_positions(kclient, k_positions)
        except Exception as e:
            errors.append(f"kalshi positions: {e}")

        try:
            kalshi_balance = kalshi_fetch_balance(kclient)
        except Exception as e:
            errors.append(f"kalshi balance: {e}")

        try:
            k_fills = kalshi_fetch_fills(kclient)
            kalshi_acts = kalshi_parse_fills(kclient, k_fills)
        except Exception as e:
            errors.append(f"kalshi fills: {e}")

        try:
            k_settled = kalshi_fetch_settled(kclient)
            kalshi_settled_acts = kalshi_parse_settled(kclient, k_settled)
            kalshi_acts.extend(kalshi_settled_acts)
        except Exception as e:
            errors.append(f"kalshi settled: {e}")

    # --- Merge ---
    all_enriched = enriched + kalshi_enriched
    all_activities = parsed_acts + kalshi_acts
    all_activities.sort(key=lambda a: a.get("timestamp", ""), reverse=True)

    total_balance = pm_balance + kalshi_balance

    open_positions = [p for p in all_enriched if not p.get("expired")]
    closed_positions = [a for a in all_activities if a["type"] == "Position Resolution"]

    tz_offset = request.args.get("tz", 0, type=int)
    summary = compute_summary(all_enriched, all_activities, tz_offset_minutes=tz_offset)

    return jsonify({
        "ok": True,
        "timestamp": now.isoformat(),
        "positions": open_positions,
        "closed_positions": closed_positions,
        "activities": all_activities,
        "balances": {
            "current_balance": total_balance,
            "polymarket_balance": pm_balance,
            "kalshi_balance": kalshi_balance,
        },
        "summary": summary,
        "errors": errors,
    })


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    debug = os.getenv("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)
