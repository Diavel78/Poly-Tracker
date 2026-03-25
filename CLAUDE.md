# Poly-Tracker

Polymarket US position tracker — a Flask web dashboard deployed on Vercel that displays live betting positions, P&L, and trade history from the Polymarket US sports betting platform.

## Architecture

- **Backend**: `app.py` — single-file Flask app, deployed as a Vercel serverless function
- **Frontend**: `templates/dashboard.html` — single-page app with inline CSS/JS, fetches data from `/api/data` via AJAX
- **Login**: `templates/login.html` — simple session-based auth (username/password from env vars)
- **SDK**: `polymarket-us` pip package — authenticates with API key/secret to fetch positions, balances, activities, and market data
- **Deployment**: Vercel via `vercel.json`, env vars configured in Vercel dashboard

## Key Files

- `app.py` — All backend logic: SDK client, data fetching, enrichment, routes
- `templates/dashboard.html` — Full dashboard UI (stats cards, positions table, closed positions, activity log, bet slip modal, dark/light theme)
- `templates/login.html` — Login page
- `.env.example` — Required environment variables
- `vercel.json` — Vercel deployment config
- `requirements.txt` — Python dependencies

## How Data Flows

1. Browser loads `/` → serves `dashboard.html`
2. JS calls `/api/data` → Flask fetches from Polymarket US SDK:
   - `client.portfolio.positions()` → dict of `{slug: position_dict}` with `marketMetadata`
   - `client.portfolio.activities()` → paginated activity history
   - `client.account.balances()` → account balance
   - `client.markets.retrieve_by_slug(slug)` → full market detail (used for line/spread info)
   - `client.markets.bbo(slug)` → best bid/offer for current price
3. `enrich_positions()` processes raw positions into display-ready data with pick labels, odds, P&L
4. `parse_activities()` processes activity history for closed positions and trade log
5. JSON response returned to frontend, which renders everything client-side

## Pick Label Logic (enrich_positions)

The SDK's `marketMetadata` provides: `title` (event name), `outcome` (line value or Yes/No), `team.name`, `slug`, `eventSlug`. The full `MarketDetail` provides a `question` field with line descriptions.

Pick labels are derived in priority order:
1. **Spread**: `team.name` + numeric `outcome` → "Sabres -1.50"
2. **Over/Under**: `outcome` ("Over"/"Under") + total extracted from `question` field → "Over 6.5"
3. **Moneyline/Futures**: `team.name` → "Purdue Boilermakers"
4. **Non-Yes/No outcome**: raw `outcome` value
5. **Slug parsing**: strip `eventSlug` prefix from `slug`, title-case remainder

## Bet Slip

Modal overlay showing current open positions in a shareable sportsbook-ticket format:
- Format: `Event — Pick, American Odds` (e.g., "BOS Bruins vs. BUF Sabres — Sabres -1.50, +127")
- Trailing dates stripped from event titles (regex: `\s+\d{4}-\d{2}-\d{2}$`)
- American odds converted from entry price (implied probability) via `probToAmericanOdds()`
- No YES/NO side labels — the pick itself tells the story

## Dashboard Features

- **Stats cards**: Balance, Open Positions, Portfolio Value, Today's P&L, Yesterday's P&L, Total P&L, Win Rate
- **Open Positions table**: Market — Pick, Qty, Entry, Current, P&L, Return %
- **Closed Positions tab**: Resolved bets with outcome and P&L
- **All Activity tab**: Full trade + resolution + balance change history
- **Dark/Light theme toggle**: persisted in localStorage
- **Auto-refresh**: polls `/api/data` every 60 seconds
- **Activity cutoff**: filters out activity before 2026-03-01 (prior arb trading data excluded)

## Environment Variables

| Variable | Purpose |
|---|---|
| `POLYMARKET_KEY_ID` | Polymarket US API key ID (UUID) |
| `POLYMARKET_SECRET_KEY` | Polymarket US API secret (base64 ed25519 private key) |
| `DASHBOARD_USER` | Login username |
| `DASHBOARD_PASS` | Login password |
| `FLASK_SECRET_KEY` | Flask session secret (auto-generated if not set) |
| `PORT` | Server port (default 5000) |

## Debug Endpoints

- `/api/raw` — Dumps raw SDK responses (positions, balances, activities)
- `/api/debug-markets` — Shows raw `marketMetadata` + full `MarketDetail` for each open position

## SDK Types Reference

Key types from `polymarket_us` (in `polymarket_us.types`):
- `MarketMetadata`: `slug`, `icon`, `title`, `outcome`, `eventSlug`, `teamId`, `team`
- `MarketDetail`: `id`, `slug`, `title`, `outcome`, `description`, `eventSlug`, `team`, plus runtime fields like `question`, `outcomes`, `sportsMarketType`
- `Team`: `name`, `abbreviation`, `league`, `record`, `logo`, `displayAbbreviation`
- Position dict keys: `netPosition`, `cost`, `cashValue`, `realized`, `expired`, `marketMetadata`
- Amount fields are dicts: `{"value": "1.23", "currency": "USD"}` — use `_safe_float()` to extract

## Common Tasks

- **Add a new stats card**: Add HTML in dashboard.html stat-grid section, populate in `renderDashboard()`
- **Change bet slip format**: Edit `buildBetSlipLabel()` in dashboard.html
- **Modify pick label logic**: Edit `enrich_positions()` in app.py (priority chain for outcome derivation)
- **Add new data source**: Add fetch function in app.py, call it in `api_data()`, pass to frontend via JSON
- **Change activity cutoff date**: Edit `CUTOFF_DATE` in `api_data()` route
