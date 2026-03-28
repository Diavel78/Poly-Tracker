# Poly-Tracker

Multi-platform sports betting position tracker — a Flask web dashboard deployed on Vercel that displays live betting positions, P&L, and trade history from both Polymarket US and Kalshi.

## Architecture

- **Backend**: `app.py` — single-file Flask app, deployed as a Vercel serverless function
- **Frontend**: `templates/dashboard.html` — single-page app with inline CSS/JS, fetches data from `/api/data` via AJAX
- **Login**: `templates/login.html` — simple session-based auth (username/password from env vars)
- **SDKs**: `polymarket-us` pip package for Polymarket; `kalshi_python_sync` for Kalshi (RSA-PSS auth)
- **Multi-platform**: Positions, activities, and balances merged from both platforms with platform badges (PM/K)
- **Deployment**: Vercel via `vercel.json`, env vars configured in Vercel dashboard

## Key Files

- `app.py` — All backend logic: SDK clients, data fetching, enrichment, routes
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
3. `enrich_positions()` processes raw Polymarket positions into display-ready data with pick labels, odds, P&L
4. `parse_activities()` processes activity history for closed positions and trade log
5. If Kalshi credentials are set, also fetches via `kalshi_python_sync` and raw API:
   - `get_positions()` → open positions (SDK)
   - `get_balance()` → account balance (SDK)
   - `/portfolio/settlements` → settled positions with P&L (raw API)
   - `/portfolio/fills` → trade history (raw API)
   - `/markets/{ticker}` → market details for titles (raw API, cached)
6. `kalshi_parse_settlements()` and `kalshi_parse_fills()` convert Kalshi data to same format as Polymarket
7. All positions/activities merged, sorted by timestamp, returned with `"platform"` field ("polymarket" or "kalshi")
8. JSON response returned to frontend, which renders everything client-side with platform badges

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
- Platform badges (PM/K) shown next to each position

## Dashboard Features

- **Stats cards**: Balance (combined PM + Kalshi), Open Positions, Portfolio Value, Today's P&L, Yesterday's P&L, Total P&L, Win Rate
- **Open Positions table**: Market — Pick, Qty, Entry, Current, P&L, Return %
- **Closed Positions tab**: Resolved bets with outcome and P&L (from both platforms)
- **All Activity tab**: Full trade + resolution + balance change history
- **Platform badges**: Blue "PM" for Polymarket, orange "K" for Kalshi on all positions/activities
- **Manual refresh**: Refresh button (no auto-refresh to avoid Vercel timeout issues with Kalshi API calls)
- **Activity cutoff**: filters out Polymarket activity before 2026-03-01 (prior arb trading data excluded)

## Environment Variables

| Variable | Purpose |
|---|---|
| `POLYMARKET_KEY_ID` | Polymarket US API key ID (UUID) |
| `POLYMARKET_SECRET_KEY` | Polymarket US API secret (base64 ed25519 private key) |
| `KALSHI_API_KEY` | Kalshi API key ID (optional — enables Kalshi integration) |
| `KALSHI_PRIVATE_KEY` | Kalshi RSA private key in PEM format (paste directly, Vercel preserves newlines) |
| `DASHBOARD_USER` | Login username |
| `DASHBOARD_PASS` | Login password |
| `FLASK_SECRET_KEY` | Flask session secret (auto-generated if not set) |
| `PORT` | Server port (default 5000) |

## Debug Endpoints

- `/api/raw` — Dumps raw Polymarket SDK responses (positions, balances, activities)
- `/api/debug-markets` — Shows raw `marketMetadata` + full `MarketDetail` for each open Polymarket position
- `/api/debug-kalshi` — Shows Kalshi client status, balance, positions, settlements (raw), and title cache size

## Polymarket SDK Types Reference

Key types from `polymarket_us` (in `polymarket_us.types`):
- `MarketMetadata`: `slug`, `icon`, `title`, `outcome`, `eventSlug`, `teamId`, `team`
- `MarketDetail`: `id`, `slug`, `title`, `outcome`, `description`, `eventSlug`, `team`, plus runtime fields like `question`, `outcomes`, `sportsMarketType`
- `Team`: `name`, `abbreviation`, `league`, `record`, `logo`, `displayAbbreviation`
- Position dict keys: `netPosition`, `cost`, `cashValue`, `realized`, `expired`, `marketMetadata`
- Amount fields are dicts: `{"value": "1.23", "currency": "USD"}` — use `_safe_float()` to extract
- **Resolution side values**: SDK returns `POSITION_RESOLUTION_SIDE_LONG` / `POSITION_RESOLUTION_SIDE_SHORT` (stripped to `LONG`/`SHORT`). LONG = YES side won, SHORT = NO side won. Do NOT compare against "YES"/"NO" — use `side in ("YES", "LONG")` etc.
- **marketMetadata.outcome**: For spread markets this is the numeric line (e.g., "-1.50"), NOT the team name. For O/U it's "Over"/"Under". For moneyline/futures it's often just "Yes"/"No". Combine with `team.name` for full pick labels.
- **MarketDetail.question**: Contains line descriptions (e.g., "Spread: BOS Bruins (+1.5)", "Total: Over/Under 6.5"). Use regex to extract totals for O/U picks.
- **Timestamps**: Activity timestamps are UTC with space separator (e.g., "2026-03-26 03:14:50"). Normalize to "T" separator before `fromisoformat()`. Always convert to client timezone for today/yesterday P&L boundaries.

## Kalshi SDK Reference

- **Auth**: RSA-PSS signature via `kalshi_python_sync`. Client configured with `Configuration()` setting `host`, `api_key_id`, and `private_key_pem`.
- **Host**: `https://api.elections.kalshi.com/trade-api/v2` — this is the correct host for ALL Kalshi markets (sports included). `api.kalshi.com` does NOT resolve.
- **SDK returns typed objects**: Responses are Pydantic models (e.g., `GetBalanceResponse`), NOT plain dicts. Use `_to_dict()` helper to convert before accessing data.
- **SDK Pydantic bugs**: `get_fills()` crashes with `ValidationError` because the API returns `count_fp` (string) and `yes_price_dollars` (string) but the SDK expects `count` (int) and `yes_price` (int). Use raw API calls for fills and settlements to bypass.
- **Raw API pattern**: Use `kclient.call_api(method='GET', url=url, header_params={'Accept': 'application/json'})` then `response.read()` and parse `response.data` as JSON. Auth headers are applied automatically. URL base is `kclient.configuration.host` — append paths like `/portfolio/fills`, `/portfolio/settlements`, `/markets/{ticker}`.
- **Fills API fields**: `action` ("buy"/"sell"), `side` ("yes"/"no"), `count_fp` (string like "5.00"), `yes_price_dollars`/`no_price_dollars` (string like "0.3800"), `ticker`, `created_time` (ISO timestamp with Z).
- **Settlements API fields**: `ticker`, `market_result` ("yes"/"no"/"void"), `yes_count_fp`/`no_count_fp` (strings), `yes_total_cost_dollars`/`no_total_cost_dollars` (strings in dollars), `revenue` (int in cents), `fee_cost` (string in dollars), `settled_time` (ISO timestamp).
- **Positions**: `get_positions()` returns open positions only — settled positions do NOT appear here. Use `/portfolio/settlements` for closed positions.
- **Prices**: Dollar amounts in fills/settlements are strings already in dollars (NOT cents). Balance from `get_balance()` is in cents.
- **Market titles**: Fetched via `/markets/{ticker}` raw API. Module-level `_kalshi_title_cache` persists between Vercel requests on the same container. Settlements look up max 20 new titles per request to avoid timeouts. Fills reuse the cache.
- **Timeout management**: Vercel serverless functions have ~10s timeout on hobby plan. Kalshi data fetching is ordered: balance → positions → settlements → fills (50 max). No pagination on any Kalshi endpoint to stay within limits.

## Common Tasks

- **Add a new stats card**: Add HTML in dashboard.html stat-grid section, populate in `renderDashboard()`
- **Change bet slip format**: Edit `buildBetSlipLabel()` in dashboard.html
- **Modify pick label logic**: Edit `enrich_positions()` in app.py (priority chain for outcome derivation)
- **Add new data source**: Add fetch function in app.py, call it in `api_data()`, pass to frontend via JSON
- **Change activity cutoff date**: Edit `CUTOFF_DATE` in `api_data()` route
- **Add new platform**: Follow Kalshi pattern — add client setup, fetch functions, enrich/parse functions, merge in `api_data()`. Use raw API calls if SDK has Pydantic validation issues.
- **Fix Kalshi SDK issues**: Always use raw API (`call_api()`) instead of SDK methods for endpoints that return data with string/float fields the SDK expects as int. The SDK's Pydantic models are strict and don't handle the API's actual response format.
