#!/usr/bin/env python3
"""Polymarket US Position Tracker — Web Dashboard

Flask app with session-based login that displays Polymarket US positions,
P&L, balances, and trade history in a secured web dashboard.
"""

import os
import sys
import secrets
import functools
from datetime import datetime, timezone

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


def fetch_activities(client):
    try:
        response = client.portfolio.activities()
        return response.get("activities", [])
    except Exception as e:
        print(f"ERROR fetching activities: {e}")
        return []


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


def compute_summary(enriched):
    total_invested = 0.0
    total_current = 0.0
    total_pnl = 0.0
    resolved_wins = 0
    resolved_total = 0

    for p in enriched:
        if p["entry_price"] is not None and p["quantity"]:
            total_invested += p["quantity"] * p["entry_price"]
        if p["current_value"] is not None:
            total_current += p["current_value"]
        if p["pnl"] is not None:
            total_pnl += p["pnl"]
            if p.get("expired"):
                resolved_total += 1
                if p["pnl"] > 0:
                    resolved_wins += 1

    win_rate = (resolved_wins / resolved_total * 100) if resolved_total > 0 else None
    return {
        "total_positions": len(enriched),
        "total_invested": total_invested,
        "total_current": total_current,
        "total_pnl": total_pnl,
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


def parse_activities(activities):
    """Convert activity dicts for the template."""
    parsed = []
    for act in activities:
        act_type = act.get("type", "unknown")
        detail = act.get(act_type, {}) if isinstance(act.get(act_type), dict) else {}

        market = detail.get("marketTitle") or detail.get("title") or ""
        side = detail.get("side") or detail.get("outcome") or ""
        price = _safe_float(detail.get("price") or detail.get("fillPrice"))
        quantity = _safe_float(detail.get("quantity") or detail.get("size") or detail.get("amount"))
        timestamp = detail.get("timestamp") or detail.get("createdAt") or act.get("timestamp") or ""

        parsed.append({
            "timestamp": str(timestamp),
            "market": str(market),
            "side": str(side),
            "price": price,
            "quantity": quantity,
            "type": act_type,
        })
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
def dashboard():
    return render_template("dashboard.html")


@app.route("/api/data")
@login_required
def api_data():
    """JSON endpoint that fetches all Polymarket data for the dashboard."""
    errors = []
    try:
        client = get_client()
    except Exception as e:
        return jsonify({"ok": False, "error": f"Client init failed: {e}"}), 500

    now = datetime.now(timezone.utc)

    positions = []
    try:
        positions = fetch_positions(client)
    except Exception as e:
        errors.append(f"positions: {e}")

    enriched = []
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

    summary = compute_summary(enriched)

    return jsonify({
        "ok": True,
        "timestamp": now.isoformat(),
        "positions": enriched,
        "activities": parse_activities(activities),
        "balances": parse_balances(balances),
        "summary": summary,
        "errors": errors,
        "debug": {
            "raw_positions_count": len(positions),
            "enriched_count": len(enriched),
            "raw_activities_count": len(activities),
            "balances_type": type(balances).__name__ if balances else "None",
            "has_credentials": bool(POLYMARKET_KEY_ID and POLYMARKET_SECRET_KEY),
        },
    })


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    debug = os.getenv("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)
