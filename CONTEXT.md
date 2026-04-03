# PolyFarm — Context for New Conversations

## What This Is
Autonomous Polymarket copy-trading farm. One or more bots watch target wallets on-chain, detect trades, and mirror them in **paper mode** (simulated). All recurring logic is hardcoded Python — LLMs only fire on user Telegram messages or critical alerts.

## Infrastructure
- **Server**: Hetzner CX21, `/opt/polyfarm`
- **Stack**: Docker Compose — `bots`, `dashboard`, `db_init` containers
- **DB**: SQLite with WAL mode at `/opt/polyfarm/data/polyfarm.db`
- **Deploy workflow**: `git push` on Mac → `git pull && docker compose up -d --build` on server
- **Restart dashboard only**: `docker compose restart dashboard` (no rebuild needed for HTML/logic changes)
- **Migrations**: `core/database.py → _migrate()` runs `ALTER TABLE` on startup — new columns added there, never manually

## Key Architectural Decisions

### Position Sizing — Tiered Buckets
- Target wallet's last 500 trades are sorted by size
- 20th/40th/60th/80th percentiles → 4 thresholds → 5 buckets
- Each bucket maps to 1%/2%/3%/4%/5% of `our_capital`
- Stored as `bucket_t1–t4` on `BotRegistry`
- Calibrated at bot setup + every 7 days by `calibrator.py`
- **Bet sizes are rounded to 2dp** (e.g. $4.89, not $5) — this is intentional, reflects % of actual capital
- Skip any target trade with `target_size < $1.00` before scaling
- Cap oversized bets at `MAX_TRADE_PCT = 8%` of capital (don't skip, just cap)

### Capital Accounting
- `initial_capital` — locked at bot creation, never changes (raised/lowered only by explicit deposit/withdraw)
- `our_capital` — updated daily by `recalibrate_capital()`: `initial_capital + sum(resolved hypothetical_pnl)`
- **Display capital** = `initial_capital + realized_pnl` filtered by `reset_at`
- **Locked capital** = `sum(hypothetical_value for unresolved trades)` — shown as info only, NOT subtracted from balance (paper trading doesn't literally lock funds; subtracting accumulates into large negatives over time)
- Deposit: raises both `initial_capital` and `our_capital` → P&L unchanged
- Withdraw: lowers both → P&L unchanged
- Reset: sets `reset_at = now`, restores `our_capital = initial_capital`

### Bot Reset
- `reset_at` column on `BotRegistry` (added via migration)
- All P&L queries in dashboard filter `WHERE created_at >= reset_at`
- Historical data is preserved — just excluded from current view
- Capital restored to `initial_capital` on reset

### Timestamps
- All DB datetimes stored as UTC naive
- Dashboard displays in **Europe/London** (BST/GMT) via `zoneinfo.ZoneInfo("Europe/London")`
- Helper: `_to_london(dt)` in `app.py`

## Dashboard (`services/dashboard/app.py`)
Single-file HTTP server (no framework). All HTML is inline Python strings (`DASHBOARD_HTML`, `BOTS_HTML`, `CHAT_HTML`).

### Common pitfall: DOM before script
JS event listeners that reference DOM elements (e.g. modals) must have those elements defined **before** the `<script>` block — not after. Caused a silent crash that left the page on "Loading…".

### API endpoints
- `GET /api/data` — main dashboard data (respects `reset_at` per bot)
- `GET /api/bots` — manage bots page data (real-time capital calc)
- `POST /api/update_bot` — actions: pause/unpause/deactivate/rename/deposit/withdraw/reset/delete
- `GET /api/bot_chart` — per-bot P&L chart data

### Per-bot filtering
Dashboard supports clicking a bot to filter all KPIs/logbook/daily table to that bot. Implemented via `selectedBot` JS variable + `updateView()`.

### Real-time capital in Manage Bots
`get_all_bots()` fetches all paper trades, aggregates in Python (not SQL) so it can respect per-bot `reset_at` correctly. Returns: `our_capital`, `realized_pnl`, `locked_capital`, `resolved_count`, `pending_count`.

## Known Pitfalls

### Docker
- Two stale containers can exist (`polyfarm-dashboard-1` AND `9a189089dace_polyfarm-dashboard-1`) — browser may hit old one
- Fix: `docker compose down && docker compose up -d --build`
- `docker compose restart` does NOT re-read `.env` — use `up -d` for env changes

### SQLAlchemy / SQLite
- Reading ORM attributes after session closes → `DetachedInstanceError` — always read values inside the `with get_session()` block
- `reset_at` stored as `TEXT` via migration but model declares `DateTime` — SQLAlchemy coerces correctly on read

### JS in Python triple-quoted strings
- Template literals (backticks) are fine inside `"""..."""`
- Only `"""` would break the string — none present in JS code

## Bot Management UI (Manage Bots `/bots`)
- Add bot: name + wallet + capital → triggers immediate bucket + volume calibration in background thread
- Actions on each bot: Pause/Resume · Rename · +Capital · –Capital · Reset · Deactivate · Delete
- Delete: removes bot + ALL child records (seen_transactions, paper_trades, target_trades, daily_pnl, positions, orders) — only available after deactivation
- Capital cell shows: balance, start/P&L breakdown, locked in open bets, resolved/pending counts
- Bot name cell shows: `↺ Reset <timestamp>` or `▶ Trading since <created_at>`

## Dashboard Bot Row Layout
- Far left: `Paper`/`Live` badge
- Then: coloured dot indicator (green/yellow/grey — no "Running" text for active bots, Paused/Inactive labels shown)
- Then: bot name + wallet address
- Right side (bot-meta): Trades · Resolved/Open · Volume · P&L · Win Rate · Last Active · Graph · View ↗
- Graph button opens P&L chart modal (SVG line + bar chart, respects reset_at)
- Portfolio Total summary row at bottom aggregates all bots

## Calibrator (`bots/calibrator.py`)
- Runs as daemon thread
- **Capital update**: daily — `our_capital = initial_capital + cumulative_resolved_pnl`
- **Bucket calibration**: startup + weekly — fetches 500 trades, computes percentiles
- **Volume calibration**: startup + weekly — `target_daily_capital` for scaling ratio fallback
- Timestamp fix: Polymarket API returns ms not seconds — `if ts > 1e11: ts /= 1000`

## Risk Rules (`bots/risk.py`)
- `MIN_TRADE_SIZE_USD = 0.50` (our scaled bet minimum)
- `MAX_TRADE_PCT = 0.08` (cap, not skip)
- `MAX_MARKET_PCT = 0.25`
- `MIN_LIQUID_RESERVE_USD = 15.00`
- `DAILY_LOSS_LIMIT_PCT = 0.20`
- `RiskDecision.adjusted_size` — set when trade is capped to max; base_bot uses this size
