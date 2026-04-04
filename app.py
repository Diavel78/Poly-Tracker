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

import requests as http_requests

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
OWLS_INSIGHT_API_KEY = os.getenv("OWLS_INSIGHT_API_KEY", "")
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
# Polymarket SDK client
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

    # Compute realized P&L from resolved positions and closed trades
    realized_pnl = 0.0
    resolved_wins = 0
    resolved_total = 0
    today_pnl = 0.0
    yesterday_pnl = 0.0

    # Use client's timezone for today/yesterday boundaries
    client_tz = timezone(timedelta(minutes=-tz_offset_minutes))
    now_local = datetime.now(client_tz)
    today_str = now_local.strftime("%Y-%m-%d")
    yesterday_str = (now_local - timedelta(days=1)).strftime("%Y-%m-%d")

    for act in parsed_activities:
        has_pnl = act["pnl"] is not None
        is_resolution = act["type"] == "Position Resolution"
        is_trade_close = act["type"] == "Trade" and act.get("_is_close") and has_pnl

        if (is_resolution or is_trade_close) and has_pnl:
            realized_pnl += act["pnl"]
            resolved_total += 1
            if act["pnl"] > 0:
                resolved_wins += 1

        if (is_resolution or is_trade_close) and has_pnl:
            # Convert activity timestamp (UTC) to client's local date
            ts = act.get("timestamp", "")
            act_local = ""
            if ts:
                try:
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
    TYPE_KEY_MAP = {
        "ACTIVITY_TYPE_POSITION_RESOLUTION": "positionResolution",
        "ACTIVITY_TYPE_TRADE": "trade",
        "ACTIVITY_TYPE_ACCOUNT_BALANCE_CHANGE": "accountBalanceChange",
    }

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
            # Don't trust SDK's realizedPnl VALUE — it uses complement pricing.
            # But non-null realizedPnl reliably indicates a sell/close trade.
            # P&L will be computed correctly in post-processing.
            sdk_rpnl = _safe_float(detail.get("realizedPnl"))
            pnl = None

            # Detect sell: realizedPnl is non-null (primary), or position qty decreased (fallback)
            t_before = detail.get("beforePosition") or {}
            t_after = detail.get("afterPosition") or {}
            bq = abs(_safe_float(t_before.get("netPosition")) or 0)
            aq = abs(_safe_float(t_after.get("netPosition")) or 0)
            is_close = sdk_rpnl is not None or bq > aq

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
            if market_slug and market:
                slug_to_title[market_slug] = market

            side = detail.get("side", "")
            side = side.replace("POSITION_RESOLUTION_SIDE_", "")

            quantity = abs(_safe_float(before.get("netPosition")) or 0)
            cost = _safe_float(before.get("cost"))
            if cost is not None and quantity > 0:
                price = cost / quantity

            # Compute P&L from win/loss logic:
            # LONG = YES side won, SHORT = NO side won
            # Positive netPosition = held YES, negative = held NO
            if cost is not None:
                net = _safe_float(before.get("netPosition")) or 0
                held_yes = net > 0
                yes_won = side in ("YES", "LONG")
                no_won = side in ("NO", "SHORT")
                won = (held_yes and yes_won) or (not held_yes and no_won)
                if won:
                    pnl = quantity - cost  # payout is $1 * qty minus what you paid
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

    # Post-process: for trades where SDK didn't provide P&L, compute from
    # tracked average cost. Activities come newest-first; process chronologically.
    slug_positions = {}  # slug -> {"qty": float, "total_cost": float}

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
            # Sell/close: P&L = (sell_price - avg_buy_price) * qty
            if pos["qty"] > 0:
                avg_cost = pos["total_cost"] / pos["qty"]
                act["pnl"] = round((act["price"] - avg_cost) * act["quantity"], 2)
                # Show entry cost in the price field for display
                act["price"] = round(avg_cost, 4)
                # Reduce tracked position
                sold_qty = min(act["quantity"], pos["qty"])
                pos["total_cost"] -= avg_cost * sold_qty
                pos["qty"] -= sold_qty

    # Strip internal fields before returning (keep _is_close for compute_summary)
    for act in parsed:
        act.pop("_market_slug", None)

    return parsed


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
def odds_page():
    return render_template("odds.html")


@app.route("/dashboard")
@login_required
def dashboard():
    return render_template("dashboard.html")


# ---------------------------------------------------------------------------
# Owls Insight API helpers
# ---------------------------------------------------------------------------

OWLS_BASE = "https://api.owlsinsight.com/api/v1"
OWLS_DEFAULT_BOOKS = ["fanduel", "draftkings", "betmgm", "pinnacle", "caesars"]
OWLS_SPORTS = ["mlb", "nba", "nhl", "nfl", "ncaab", "ncaaf", "mma", "soccer", "tennis"]
OWLS_CACHE_TTL = 180  # seconds — serve cached odds for 3 minutes

# Simple in-memory cache: { "sport:books" -> { "data": ..., "ts": time } }
_owls_cache = {}


def _owls_get(path, params=None):
    """Make an authenticated GET to Owls Insight API."""
    headers = {"Authorization": f"Bearer {OWLS_INSIGHT_API_KEY}"}
    resp = http_requests.get(f"{OWLS_BASE}{path}", headers=headers,
                             params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def _owls_get_cached(sport, books):
    """Fetch odds with server-side cache to avoid burning requests."""
    import time
    cache_key = f"{sport}:{books}"
    now = time.time()
    cached = _owls_cache.get(cache_key)
    if cached and (now - cached["ts"]) < OWLS_CACHE_TTL:
        return cached["data"], True  # data, from_cache

    params = {}
    if books:
        params["books"] = books
    raw = _owls_get(f"/{sport}/odds", params)
    _owls_cache[cache_key] = {"data": raw, "ts": now}
    return raw, False


def _normalize_owls_odds(sport, raw_data):
    """Normalize Owls Insight response into a unified event list.

    Response: { "data": { "fanduel": [events...], "pinnacle": [events...] } }
    Each event has bookmakers[0] with markets (h2h, spreads, totals).
    We merge the same event across books using eventId.
    """
    books_data = raw_data.get("data", {})
    if not isinstance(books_data, dict):
        return []

    events_map = {}  # eventId -> merged event

    for book_key, book_events in books_data.items():
        if not isinstance(book_events, list):
            continue

        for ev in book_events:
            eid = ev.get("eventId") or ev.get("id", "")
            if not eid:
                continue

            if eid not in events_map:
                events_map[eid] = {
                    "id": eid,
                    "numeric_id": str(ev.get("id", "")),
                    "sport": sport,
                    "home_team": ev.get("home_team", ""),
                    "away_team": ev.get("away_team", ""),
                    "commence_time": ev.get("commence_time", ""),
                    "league": ev.get("league", ""),
                    "status": ev.get("status", ""),
                    "books": {},
                }

            # Extract odds from the bookmakers array (usually 1 entry per book)
            for bk in ev.get("bookmakers", []):
                bk_key = bk.get("key", book_key)
                book_odds = {
                    "moneyline": {}, "spread": {}, "total": {},
                    "event_link": bk.get("event_link", ""),
                }

                for mkt in bk.get("markets", []):
                    mkt_key = mkt.get("key", "")
                    outcomes = mkt.get("outcomes", [])

                    if mkt_key == "h2h":
                        for o in outcomes:
                            book_odds["moneyline"][o["name"]] = o.get("price")

                    elif mkt_key == "spreads":
                        for o in outcomes:
                            book_odds["spread"][o["name"]] = {
                                "price": o.get("price"),
                                "point": o.get("point"),
                            }

                    elif mkt_key == "totals":
                        for o in outcomes:
                            book_odds["total"][o["name"]] = {
                                "price": o.get("price"),
                                "point": o.get("point"),
                            }

                events_map[eid]["books"][bk_key] = book_odds

    return sorted(events_map.values(), key=lambda e: e.get("commence_time", ""))


def _fetch_splits(sport):
    """Fetch betting splits (handle % vs ticket %) for a sport."""
    import time
    cache_key = f"splits:{sport}"
    now = time.time()
    cached = _owls_cache.get(cache_key)
    if cached and (now - cached["ts"]) < OWLS_CACHE_TTL:
        return cached["data"], True

    try:
        raw = _owls_get(f"/{sport}/splits")
        _owls_cache[cache_key] = {"data": raw, "ts": now}
        return raw, False
    except Exception:
        return {}, False


def _normalize_splits(raw_splits):
    """Parse splits into { event_id -> { circa: {...}, dk: {...} } }.

    Response: { "data": [ { "event_id": "1627328008", "away_team", "home_team",
        "splits": [ { "book": "circa", "title": "Circa Sports",
            "moneyline": { "away_bets_pct", "away_handle_pct", "home_bets_pct", "home_handle_pct" },
            "spread": { "away_bets_pct", "away_handle_pct", "away_line", "home_line", ... },
            "total": { "over_bets_pct", "over_handle_pct", "under_bets_pct", "under_handle_pct", "line" }
        } ] } ] }
    """
    splits_map = {}
    raw_data = raw_splits.get("data", [])
    if not isinstance(raw_data, list):
        return {}

    for ev in raw_data:
        eid = ev.get("event_id") or ev.get("eventId") or ev.get("id", "")
        if not eid:
            continue
        eid = str(eid)

        ev_splits = {}
        for sp in ev.get("splits", []):
            book = sp.get("book", "")
            if not book:
                continue
            ev_splits[book] = {
                "title": sp.get("title", book),
                "moneyline": sp.get("moneyline", {}),
                "spread": sp.get("spread", {}),
                "total": sp.get("total", {}),
            }

        if ev_splits:
            splits_map[eid] = ev_splits

    return splits_map


def _merge_splits(events, splits_map):
    """Attach splits data to each event, matching by numeric_id or id."""
    for ev in events:
        nid = ev.get("numeric_id", "")
        eid = ev.get("id", "")
        ev["splits"] = splits_map.get(nid) or splits_map.get(eid, {})
    return events


@app.route("/api/odds")
@login_required
def api_odds():
    """Fetch odds + splits from Owls Insight for requested sports."""
    if not OWLS_INSIGHT_API_KEY:
        return jsonify({"ok": False, "error": "OWLS_INSIGHT_API_KEY not configured"}), 500

    sport = request.args.get("sport", "mlb")
    books = request.args.get("books", "")

    errors = []
    events = []

    from_cache = False
    meta_message = ""
    try:
        raw, from_cache = _owls_get_cached(sport, books)
        events = _normalize_owls_odds(sport, raw)
        meta = raw.get("meta", {})
        if meta.get("message"):
            meta_message = meta["message"]
    except http_requests.HTTPError as e:
        errors.append(f"{sport}: HTTP {e.response.status_code}")
    except Exception as e:
        errors.append(f"{sport}: {e}")

    # Fetch splits and merge (MVP plan)
    try:
        raw_splits, _ = _fetch_splits(sport)
        splits_map = _normalize_splits(raw_splits)
        events = _merge_splits(events, splits_map)
    except Exception as e:
        errors.append(f"splits: {e}")

    active_books = set()
    leagues = set()
    for ev in events:
        active_books.update(ev.get("books", {}).keys())
        if ev.get("league"):
            leagues.add(ev["league"])

    return jsonify({
        "ok": True,
        "cached": from_cache,
        "sport": sport,
        "events": events,
        "books": sorted(active_books),
        "leagues": sorted(leagues),
        "meta_message": meta_message,
        "errors": errors,
    })


@app.route("/api/odds/raw")
@login_required
def api_odds_raw():
    """Debug: raw Owls Insight response."""
    if not OWLS_INSIGHT_API_KEY:
        return jsonify({"error": "no key"}), 500
    sport = request.args.get("sport", "mlb")
    books = request.args.get("books", "pinnacle,fanduel")
    try:
        raw = _owls_get(f"/{sport}/odds", {"books": books})
        return jsonify(raw)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/splits/raw")
@login_required
def api_splits_raw():
    """Debug: raw splits response."""
    if not OWLS_INSIGHT_API_KEY:
        return jsonify({"error": "no key"}), 500
    sport = request.args.get("sport", "mlb")
    try:
        raw = _owls_get(f"/{sport}/splits")
        return jsonify(raw)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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


@app.route("/api/debug-trades")
@login_required
def api_debug_trades():
    """Debug: show raw trade detail fields, grouped by slug."""
    try:
        client = get_client()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    try:
        all_acts = fetch_activities(client)
    except Exception as e:
        return jsonify({"error": f"activities: {e}"}), 500

    # Group trades by slug, show key fields
    by_slug = {}
    for act in all_acts:
        if act.get("type") != "ACTIVITY_TYPE_TRADE":
            continue
        detail = act.get("trade", {})
        slug = detail.get("marketSlug", "unknown")
        rpnl = detail.get("realizedPnl")
        entry = {
            "timestamp": detail.get("updateTime") or detail.get("timestamp"),
            "price": detail.get("price"),
            "qty": detail.get("qty"),
            "cost": detail.get("cost"),
            "realizedPnl": rpnl,
            "is_sell": rpnl is not None,
        }
        # Sell-only fields
        if rpnl is not None:
            entry["costBasis"] = detail.get("costBasis")
            entry["originalPrice"] = detail.get("originalPrice")
            entry["effectiveRealizedPnl"] = detail.get("effectiveRealizedPnl")
            entry["effectiveCostBasis"] = detail.get("effectiveCostBasis")
            entry["effectiveOriginalPrice"] = detail.get("effectiveOriginalPrice")
        if slug not in by_slug:
            by_slug[slug] = []
        by_slug[slug].append(entry)

    # Only show slugs that have sells (most interesting for debugging)
    sell_slugs = {s: trades for s, trades in by_slug.items()
                  if any(t["is_sell"] for t in trades)}

    # Optional slug filter via query param: /api/debug-trades?slug=veg
    slug_filter = request.args.get("slug", "").lower()
    if slug_filter:
        sell_slugs = {s: t for s, t in sell_slugs.items() if slug_filter in s.lower()}

    return jsonify({
        "total_slugs": len(by_slug),
        "slugs_with_sells": len(sell_slugs),
        "trades_by_slug": sell_slugs,
    })


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
    """JSON endpoint that fetches all data for the dashboard."""
    errors = []
    now = datetime.now(timezone.utc)

    enriched = []
    parsed_acts = []
    balance = 0.0

    try:
        client = get_client()

        try:
            positions = fetch_positions(client)
        except Exception as e:
            positions = []
            errors.append(f"positions: {e}")

        try:
            enriched = enrich_positions(client, positions)
        except Exception as e:
            errors.append(f"enrich: {e}")

        activities = []
        try:
            activities = fetch_activities(client)
        except Exception as e:
            errors.append(f"activities: {e}")

        balances = None
        try:
            balances = fetch_balances(client)
        except Exception as e:
            errors.append(f"balances: {e}")

        parsed_acts = parse_activities(client, activities)
        bal = parse_balances(balances)
        balance = bal.get("current_balance") or 0.0

    except Exception as e:
        errors.append(f"client: {e}")
        bal = {}

    # Exclude activity before 2026-03-01 (prior data was arb trading)
    CUTOFF_DATE = "2026-03-01"
    parsed_acts = [a for a in parsed_acts if a.get("timestamp", "") >= CUTOFF_DATE]

    parsed_acts.sort(key=lambda a: a.get("timestamp", ""), reverse=True)

    open_positions = [p for p in enriched if not p.get("expired")]
    closed_positions = [a for a in parsed_acts
                        if a["type"] == "Position Resolution"
                        or (a["type"] == "Trade" and a.get("_is_close") and a.get("pnl") is not None)]

    tz_offset = request.args.get("tz", 0, type=int)
    summary = compute_summary(enriched, parsed_acts, tz_offset_minutes=tz_offset)

    # Strip internal fields before sending to frontend
    for act in parsed_acts:
        act.pop("_is_close", None)

    return jsonify({
        "ok": True,
        "timestamp": now.isoformat(),
        "positions": open_positions,
        "closed_positions": closed_positions,
        "balances": {
            "current_balance": balance,
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
