# Poly-Tracker

Sports betting odds board + Polymarket position tracker. Flask web app deployed on Vercel with two main pages: **Odds Board** (multi-sportsbook odds comparison) and **Dashboard** (Polymarket P&L tracking).

## Architecture

- **Backend**: `app.py` — single-file Flask app, Vercel serverless function
- **Frontend**: `templates/odds.html` (odds board), `templates/dashboard.html` (P&L tracker), `templates/login.html`
- **APIs**: Owls Insight (multi-book odds, splits, scores), Polymarket US SDK (positions, P&L)
- **Deployment**: Vercel via `vercel.json`, env vars in Vercel dashboard

## Key Files

- `app.py` — All backend: API clients, data fetching, normalization, routes
- `templates/odds.html` — Odds board (best lines, splits, movement, live scores, bet indicators)
- `templates/dashboard.html` — Polymarket P&L dashboard (stats, positions, closed trades, bet slip)
- `templates/login.html` — Login page
- `.env.example` — Required environment variables
- `vercel.json` — Vercel deployment config
- `requirements.txt` — Python dependencies (flask, polymarket-us, requests, python-dotenv)

## Environment Variables

| Variable | Purpose |
|---|---|
| `POLYMARKET_KEY_ID` | Polymarket US API key ID (UUID) |
| `POLYMARKET_SECRET_KEY` | Polymarket US API secret (base64 ed25519 private key) |
| `OWLS_INSIGHT_API_KEY` | Owls Insight API key (multi-sportsbook odds) — MVP plan ($49.99/mo) |
| `DASHBOARD_USER` | Login username |
| `DASHBOARD_PASS` | Login password |
| `FLASK_SECRET_KEY` | Flask session secret (auto-generated if not set) |
| `PORT` | Server port (default 5000) |

## Pages & Routes

| Route | Page |
|---|---|
| `/` | Odds Board (front page) |
| `/dashboard` | Polymarket P&L Dashboard |
| `/login` | Login |
| `/logout` | Logout |
| `/api/odds` | Odds + splits + scores JSON |
| `/api/data` | Dashboard positions/P&L JSON |
| `/api/my-bets` | Active Polymarket positions for odds board matching |
| `/api/odds/raw` | Debug: raw Owls Insight odds response |
| `/api/splits/raw` | Debug: raw splits response |
| `/api/scores/raw` | Debug: raw live scores response |
| `/api/realtime/raw` | Debug: raw realtime/ps3838 sharp odds |
| `/api/raw` | Debug: raw Polymarket SDK responses |
| `/api/debug-trades` | Debug: raw trade details grouped by slug |

---

## Odds Board (`/`)

### Features
- **Best Odds Column** (left, always visible): Best ML, Spread, Total across all enabled books with book attribution
- **Multi-Book Columns** (scrollable right): Individual book odds side by side
- **Sport Tabs**: MLB, NBA, NHL, NFL, NCAAB, MMA, Soccer, Tennis
- **Search**: Filter by team name (client-side, instant)
- **Book Selector**: Dropdown with checkboxes + up/down arrows to reorder. Saved to localStorage
- **Live Scores**: Green LIVE badge with score between team names. Fetched from `/api/v1/{sport}/scores/live`
- **Circa Splits**: Handle % vs Ticket % per market (ML, SPR, TOT). SHARP tags when divergence >= 15%
- **Line Movement**: Circa opening line vs current, with arrows and diffs. Openers stored in localStorage per date
- **Reverse Line Movement (RLM)**: Pulsing red flag when line moves to make sharp side MORE expensive despite money pouring in
- **Polymarket Bet Indicators**: Purple check badge showing pick + entry American odds (e.g., "✓ POLY: Over 7.5 (-110)")
- **Live Bet Status**: Badge color changes based on score — green (winning), yellow (not yet), red (dead/busted)
- **Auto-refresh**: 5 seconds (odds+splits), 30 seconds (scores), 60 seconds (my-bets)
- **Opener Prefetch**: First visit of the day fetches ALL sports to capture opening lines

### Owls Insight API (multi-sportsbook odds)

**Base URL**: `https://api.owlsinsight.com`
**Auth**: `Authorization: Bearer {OWLS_INSIGHT_API_KEY}`
**Plan**: MVP ($49.99/mo) — 300K req/month, 400/min, real-time sharp odds, full props, historical archive

#### Endpoints Used
| Endpoint | Purpose |
|---|---|
| `GET /api/v1/{sport}/odds` | All odds (spreads, moneylines, totals) from all books |
| `GET /api/v1/{sport}/splits` | Circa + DK betting splits (handle %, ticket %) |
| `GET /api/v1/{sport}/scores/live` | Live scores with team names, logos |
| `GET /api/v1/{sport}/realtime` | Real-time Pinnacle sharp odds (sub-second) |
| `GET /api/v1/{sport}/ps3838-realtime` | PS3838 (Pinnacle Asia) real-time odds |

#### Sports Keys
`mlb`, `nba`, `nhl`, `nfl`, `ncaab`, `ncaaf`, `mma`, `soccer`, `tennis`, `cs2`, `valorant`, `lol`

#### Sportsbook Keys
`pinnacle`, `fanduel`, `draftkings`, `betmgm`, `caesars`, `bet365`, `circa`, `south_point`, `westgate`, `wynn`, `stations`, `hardrock`, `betonline`, `1xbet`, `polymarket`, `kalshi`, `novig`

#### Response Structure (odds)
```json
{
  "data": {
    "fanduel": [
      {
        "away_team": "Chicago Cubs",
        "home_team": "Cleveland Guardians",
        "commence_time": "2026-04-04T23:15:00Z",
        "eventId": "mlb:Chicago Cubs@Cleveland Guardians-20260404",
        "id": "1627328041",
        "league": "USA - MLB",
        "status": "scheduled",
        "bookmakers": [
          {
            "key": "fanduel",
            "title": "FanDuel",
            "event_link": "https://sportsbook.fanduel.com/...",
            "markets": [
              { "key": "h2h", "outcomes": [{ "name": "Chicago Cubs", "price": -134 }, ...] },
              { "key": "spreads", "outcomes": [{ "name": "Chicago Cubs", "point": -1.5, "price": 128 }, ...] },
              { "key": "totals", "outcomes": [{ "name": "Over", "point": 8, "price": -105 }, ...] }
            ]
          }
        ]
      }
    ],
    "pinnacle": [...],
    ...
  }
}
```
**Key**: Top-level `data` is keyed by book name. Each book has an array of events. Market keys: `h2h` (moneyline), `spreads`, `totals`.

#### Response Structure (splits)
```json
{
  "data": [
    {
      "event_id": "35447537",
      "away_team": "Houston Astros",
      "home_team": "Oakland Athletics",
      "splits": [
        {
          "book": "circa",
          "title": "Circa Sports",
          "moneyline": { "away_bets_pct": 61, "away_handle_pct": 29, "home_bets_pct": 39, "home_handle_pct": 71 },
          "spread": { "away_bets_pct": 67, "away_handle_pct": 86, "away_line": -1.5, "home_bets_pct": 33, "home_handle_pct": 14, "home_line": 1.5 },
          "total": { "line": 9.5, "over_bets_pct": 88, "over_handle_pct": 12, "under_bets_pct": 12, "under_handle_pct": 88 }
        },
        { "book": "dk", ... }
      ]
    }
  ]
}
```
**IMPORTANT**: Only use Circa splits. DK splits are worthless — never fall back to DK.
**IMPORTANT**: Splits response contains DUPLICATE entries (today + tomorrow) for same teams/event_id. The tomorrow entry is DK-only. Always prefer entries that have Circa data.

#### Response Structure (scores)
Sport-specific endpoint returns `{ "count": N, "events": [...] }` (NOT `data.sports.{sport}`).
Generic endpoint returns `{ "data": { "sports": { "mlb": [...] } } }`.
Home/away may be swapped between odds and scores feeds — match by team name set (frozenset), not position.

#### Normalization Pipeline
1. `_normalize_owls_odds()` — Merges events across books by `eventId`. Stores both `id` (eventId string) and `numeric_id` (numeric book ID)
2. `_normalize_splits()` — Indexes by event_id AND team name frozenset. Prefers Circa over DK-only duplicates
3. `_merge_splits()` — Matches by numeric_id, then id, then team names (frozenset fallback)
4. `_merge_scores()` — Matches by team name set (order-independent). Flips scores to match odds-feed orientation

#### Caching
- Odds: 10 second TTL server-side (in-memory `_owls_cache`)
- Splits: 10 second TTL
- Scores: 30 second TTL
- My-bets: 60 second TTL
- **IMPORTANT**: Vercel serverless = in-memory cache resets on cold start. Opening lines tracked in browser localStorage, NOT server memory

#### Client-Side State (localStorage)
| Key | Purpose |
|---|---|
| `odds_sport` | Last selected sport tab |
| `odds_books` | Enabled sportsbooks (JSON array) |
| `odds_book_order` | Custom book display order (JSON array) |
| `openers:{date}:{sport}` | Opening lines per event (auto-cleaned daily) |
| `prefetched:{date}` | Flag: all sports prefetched today |

### League Filtering
MLB tab filters to `league` containing "MLB" (API returns "USA - MLB"). Empty league = minor league/college, filtered out.
NCAAB/NCAAF/MMA/soccer/tennis pass through unfiltered.

### Sharp Line Source Priority
Circa > Pinnacle > Wynn > Westgate. Circa is primary because:
- Always available (powers splits data)
- Pinnacle feed drops randomly
- Sharpest Vegas book, doesn't limit winners

### Book Sort Order
Circa first, Pinnacle second, rest alphabetical.

---

## Dashboard (`/dashboard`)

### Features
- **Stats cards**: Balance, Open Positions, Portfolio Value, Today's P&L, Yesterday's P&L, Total P&L, Win Rate
- **Open Positions table**: Market — Pick, Qty, Entry, Current, P&L, Return %
- **Closed Positions tab**: Resolved bets AND sold trades with Result (W/L/Sold) and P&L
- **Bet Slip modal**: Shareable sportsbook-ticket format
- **Auto-refresh**: 60 seconds

### Polymarket US SDK

Package: `polymarket-us` (pip). Client: `PolymarketUS(key_id=..., secret_key=...)`.

#### SDK Methods Used
```python
client.portfolio.positions()           # {slug: position_dict}
client.portfolio.activities(params={}) # Paginated activity history
client.account.balances()              # Account balance
client.markets.retrieve_by_slug(slug)  # Full market detail
client.markets.bbo(slug)               # Best bid/offer (current price)
client.orders.list()                   # Open orders
client.events.list(params={})          # Events listing
```

### P&L Computation — CRITICAL NOTES

#### Trade Sell P&L
- **Do NOT trust SDK's `realizedPnl`** — it uses complement pricing and produces wrong results
- **Do NOT trust SDK's `price` field** — for sells it's usually the actual sell price, but occasionally the complement
- Self-tracking average cost: accumulate buy costs per slug, compute `(sell_price - avg_buy_cost) * qty`
- The `realizedPnl` field IS reliable as a **sell indicator** (null for buys, non-null for sells), just don't use its VALUE

#### Sell Detection
- `realizedPnl is not None` = sell (primary detection)
- `beforePosition.netPosition > afterPosition.netPosition` = sell (fallback)

#### What Counts in Stats
Both "Position Resolution" (market settled) AND closed trades (sells with `_is_close=True` and P&L) count toward:
- Realized P&L
- Today/Yesterday P&L
- Win Rate
- Closed Positions list

#### Position Resolution P&L
```python
net = beforePosition.netPosition  # positive = YES, negative = NO
held_yes = net > 0
yes_won = side in ("YES", "LONG")
won = (held_yes and yes_won) or (not held_yes and no_won)
pnl = quantity - cost if won else -cost
```

### Activity Cutoff
Filters out activity before `2026-03-01` (prior arb trading data excluded).

### Timestamps
Activity timestamps are UTC with space separator ("2026-03-26 03:14:50"). Normalize to "T" separator before `fromisoformat()`. Convert to client timezone for today/yesterday P&L boundaries using `tz_offset_minutes` from frontend.

---

## Common Tasks

### Odds Board
- **Add a new sport**: Add to `OWLS_SPORTS` in app.py and `SPORTS` array in odds.html
- **Change refresh interval**: Edit `setInterval(loadOdds, 5000)` in odds.html
- **Change cache TTL**: Edit `OWLS_CACHE_TTL` in app.py
- **Add a new book**: Just enable it in the Books dropdown — API returns all available books
- **Change sharp line source**: Edit the `pin = (ev.books || {}).circa || ...` fallback chain in odds.html
- **Adjust SHARP threshold**: Change the `>= 15` divergence check in `renderSplitsRow()`
- **Adjust RLM detection**: Edit `detectRLM()` in odds.html — currently triggers when line makes sharp side MORE expensive while 60%+ handle is on that side

### Dashboard
- **Add a new stats card**: Add HTML in dashboard.html stat-grid section, populate in `renderCards()`
- **Change bet slip format**: Edit `buildBetSlipLabel()` in dashboard.html
- **Modify pick label logic**: Edit `enrich_positions()` in app.py
- **Change activity cutoff date**: Edit `CUTOFF_DATE` in `api_data()` route

### Both Pages
- **Modify navigation**: Both pages have matching nav with Odds/Dashboard tabs
- **Change auth**: Edit `DASHBOARD_USER`/`DASHBOARD_PASS` env vars

---

## Known Issues & Gotchas

1. **Pinnacle feed drops randomly** — Owls Insight's Pinnacle poller crashes sometimes. Circa is the reliable fallback.
2. **Live in-game odds not available via REST** — Games drop off the odds endpoint once live. Would need WebSocket add-on ($$$). Pre-game odds stay with "pre-game odds" tag.
3. **MMA/UFC odds are sparse** — Only appear when fight cards have posted odds (often same-day).
4. **Splits duplicates** — API returns today + tomorrow entries for same event. Must prefer Circa-containing entries.
5. **League field varies** — "MLB", "USA - MLB", or empty. Use substring match, not exact.
6. **Score home/away swapped** — Odds and scores feeds may have different home/away designation. Match by team name set.
7. **Vercel cold starts** — In-memory cache resets. Opening lines must be in localStorage, not server memory.
8. **SDK `realizedPnl` is unreliable** — Do not trust the value. Only use non-null as a sell indicator.

## API Budget (MVP Plan)
- 300K requests/month, 400/minute
- At 5s refresh, 8hrs/day: ~201K/month (67% of limit)
- Calls per refresh cycle: odds (every 10s) + splits (every 10s) + scores (every 30s) = ~14 actual API calls/min
- Opener prefetch: 8 extra calls total, once per day
