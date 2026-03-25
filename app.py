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
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _getattr_chain(obj, *attrs, default=None):
    for attr in attrs:
        val = getattr(obj, attr, None)
        if val is not None:
            return val
    return default


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def fetch_positions(client):
    try:
        response = client.portfolio.positions()
        if hasattr(response, "data"):
            return list(response.data)
        if hasattr(response, "results"):
            return list(response.results)
        if isinstance(response, list):
            return response
        return list(response)
    except Exception as e:
        print(f"ERROR fetching positions: {e}")
        return []


def fetch_market_price(client, market_slug):
    try:
        bbo = client.markets.bbo(market_slug)
        best_bid = _safe_float(getattr(bbo, "best_bid_price", None) or getattr(bbo, "bid", None))
        best_ask = _safe_float(getattr(bbo, "best_ask_price", None) or getattr(bbo, "ask", None))
        if best_bid is not None and best_ask is not None:
            return (best_bid + best_ask) / 2
        return best_bid or best_ask
    except Exception:
        return None


def fetch_market(client, market_id):
    try:
        return client.markets.retrieve(market_id)
    except Exception:
        return None


def fetch_activities(client):
    try:
        response = client.portfolio.activities()
        if hasattr(response, "data"):
            return list(response.data)
        if hasattr(response, "results"):
            return list(response.results)
        if isinstance(response, list):
            return response
        return list(response)
    except Exception as e:
        print(f"ERROR fetching activities: {e}")
        return []


def fetch_balances(client):
    try:
        return client.account.balances()
    except Exception as e:
        print(f"ERROR fetching balances: {e}")
        return None


def enrich_positions(client, positions):
    enriched = []
    for pos in positions:
        market_id = _getattr_chain(pos, "market_id", "marketId", "market")
        market_slug = _getattr_chain(pos, "market_slug", "marketSlug", "slug")
        market_name = _getattr_chain(pos, "market_name", "marketName", "title", "question")
        side = _getattr_chain(pos, "side", "outcome", default="YES")
        quantity = _safe_float(_getattr_chain(pos, "quantity", "size", "qty", "amount")) or 0
        entry_price = _safe_float(_getattr_chain(pos, "entry_price", "entryPrice", "avg_price", "avgPrice"))

        if not market_name and market_id:
            market = fetch_market(client, str(market_id))
            if market:
                market_name = _getattr_chain(market, "title", "question", "name", default=str(market_id))
                if not market_slug:
                    market_slug = _getattr_chain(market, "slug")

        current_price = None
        if market_slug:
            current_price = fetch_market_price(client, str(market_slug))
        if current_price is None and market_id:
            current_price = fetch_market_price(client, str(market_id))
        if current_price is None:
            current_price = _safe_float(_getattr_chain(pos, "current_price", "currentPrice", "price"))

        pnl = None
        pnl_pct = None
        current_value = None
        if current_price is not None and quantity:
            current_value = quantity * current_price
            if entry_price is not None:
                pnl = quantity * (current_price - entry_price)
                if entry_price > 0:
                    pnl_pct = ((current_price - entry_price) / entry_price) * 100

        enriched.append({
            "market_name": market_name or str(market_id or "Unknown"),
            "market_id": str(market_id or ""),
            "market_slug": str(market_slug or ""),
            "side": str(side).upper(),
            "quantity": quantity,
            "entry_price": entry_price,
            "current_price": current_price,
            "current_value": current_value,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
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
            if p["current_price"] in (0.0, 1.0):
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
    """Extract balance fields into a simple dict."""
    if balances is None:
        return {}
    return {
        "current_balance": _safe_float(_getattr_chain(balances, "currentBalance", "current_balance", "balance")),
        "buying_power": _safe_float(_getattr_chain(balances, "buyingPower", "buying_power")),
        "open_orders": _safe_float(_getattr_chain(balances, "openOrders", "open_orders")),
        "unsettled": _safe_float(_getattr_chain(balances, "unsettledFunds", "unsettled_funds")),
    }


def parse_activities(activities):
    """Convert activity objects to plain dicts for the template."""
    parsed = []
    for act in activities:
        parsed.append({
            "timestamp": str(_getattr_chain(act, "timestamp", "created_at", "createdAt", "time") or ""),
            "market": str(_getattr_chain(act, "market_name", "marketName", "title", "market") or ""),
            "side": str(_getattr_chain(act, "side", "outcome") or ""),
            "price": _safe_float(_getattr_chain(act, "price", "fill_price", "fillPrice")),
            "quantity": _safe_float(_getattr_chain(act, "quantity", "size", "qty", "amount")),
            "type": str(_getattr_chain(act, "type", "action", "activity_type", default="trade")),
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
