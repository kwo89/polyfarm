# Polymarket Copy-Trading Bot — Context & Findings

## Goal

Build a bot that monitors a target Polymarket wallet in near real-time and replicates its trades proportionally, starting with a $100 portfolio. The bot should copy buys, handle exits (merges/redeems/resolution), and scale position sizes relative to the target's capital deployment.

---

## Key Findings from Analysis

### How Polymarket trades work
- All trades go through a CLOB (Central Limit Order Book) on Polygon.
- Binary markets: each market has two outcomes. 1 YES share + 1 NO share = $1.
- A BUY of Outcome A at price 0.60 is equivalent to a SELL of Outcome B at 0.40. The system mirrors orders.
- Exits can happen three ways: (1) selling shares back on the CLOB, (2) merging matched YES+NO pairs into $1 USDC via the CTF smart contract, or (3) holding to resolution where winning shares pay $1 and losing shares pay $0 automatically on-chain.
- Resolution payouts are on-chain settlement events — they do NOT appear as trades in the `/activity` API endpoint.

### What the activity API shows (and doesn't
- `GET https://data-api.polymarket.com/activity?user={address}` returns trades, merges, redeems, conversions.
- No authentication needed for this endpoint.
- Many accounts show only BUY trades — this means they hold to resolution rather than actively selling.
- Filters available: `type` (TRADE, SPLIT, MERGE, REDEEM, REWARD, CONVERSION), `side` (BUY, SELL), `start`, `end`, `limit`, `sortBy`, `sortDirection`.
- The `side` filter only applies to type=TRADE.
- Resolution profits/losses are NOT reflected in this endpoint. You need to cross-reference with market resolution data or on-chain CTF events.

### Important warning: volume farming vs. actual trading profit
- Some high-volume accounts are farming for token airdrops (generating volume to qualify) rather than trading for profit.
- These accounts intentionally buy both sides at near-breakeven to maximize volume while minimizing losses.
- At $100 scale, copying such an account means inheriting small trading losses without the airdrop upside.
- Before building, verify the target account is genuinely profitable from trading by checking resolved market PnL on analytics tools like polydata.org or polymarketanalytics.com.

---

## Bot Architecture

### 1. Monitor Service
- Poll `/activity?user={target_address}&limit=50` every 15–60 seconds (varies per bot)
- No type filter — capture TRADE, MERGE, REDEEM, CONVERSION.
- Deduplicate by `transactionHash` (rolling set of last 500).
- Track `last_seen_timestamp` to detect new activity.

### 2. Proportional Sizing
- Core formula: `your_size = target_size × (your_balance / target_daily_capital)`
- At $100 vs a $43k/day account, scaling factor ≈ 0.23%.
- Most individual trades scale below $1 — not practical. Solution: **batch trades**.
- Accumulate target's trades per market+outcome. Execute one aggregated order when your scaled position crosses a $1–2 minimum threshold.
- Recalculate scaling factor dynamically as your balance changes.

### 3. Trade Execution
- CLOB API requires authentication (apiKey, secret, passphrase).
- Use limit orders at `target_price + 0.01–0.02` for slippage tolerance.
- Cancel unfilled orders after 30 seconds. Never chase prices.
- Skip if price moved >5% from target's fill.
- Python: `py-clob-client`. JavaScript: `@polymarket/clob-client`.

### 4. Exit Handling
- **If target merges:** Detect MERGE in activity feed. Merge your own matched YES+NO pairs via the CTF contract on Polygon.
- **If target redeems:** Detect REDEEM. Redeem your winning shares.
- **If target holds to resolution:** Your shares auto-settle on-chain. Winning = $1, losing = $0.
- **If target sells on CLOB:** Detect TRADE with side=SELL. Place matching sell order.

### 5. Risk Management (for $100 portfolio)
- Max single trade: 8% of balance ($8)
- Max per-market exposure: 25% of balance ($25)
- Max concurrent markets: 20
- Minimum balance reserve: $15 always liquid
- Daily loss limit: 20% — stop trading for the day
- Skip trades scaling below $0.50 (gas isn't worth it)

---

## Available APIs & Data Sources

| Source | URL | Auth | Use |
|--------|-----|------|-----|
| Data API | `https://data-api.polymarket.com` | None | Activity, positions, leaderboards |
| CLOB API | `https://clob.polymarket.com` | API key | Order book, pricing, placing orders |
| WebSocket | Via CLOB | Optional | Real-time trade/price streaming |
| Bitquery | `https://graphql.bitquery.io` | API key | On-chain trade data (buyer+seller) |
| CTF Contract | `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045` | Web3 wallet | Merge/redeem operations |

### Useful endpoints
- `GET /activity?user={addr}` — all activity (trades, merges, redeems)
- `GET /positions?user={addr}` — current open positions
- `GET /trades?market={id}` — trade history for a market
- WebSocket `market` channel — stream live orderbook + trades per market

---

## Recommended Tech Stack
- Runtime: Python 3.11+ or Node.js
- Libraries: `py-clob-client`, `web3.py`, `requests`
- State: SQLite (positions, trade log, dedup hashes)
- Hosting: VPS or Railway (must run during US sports hours ~01:00–06:00 UTC)
- Alerts: Telegram bot for trade notifications

---

## Implementation Phases

### Phase 1: Paper Trading (Week 1)
- Poll target activity, log all trades
- Calculate what you WOULD have traded
- Track hypothetical P&L to validate before risking real money

### Phase 2: Single Market (Week 2)
- CLOB auth setup
- Execute copy trades on ONE market at a time
- Test merge detection and execution
- Max $10 exposure

### Phase 3: Full Copy (Week 3+)
- All markets, batch aggregation, full risk management
- Monitoring dashboard + Telegram alerts
- Track real vs target P&L divergence

---

## Open Questions to Resolve
1. Is the target account profitable from TRADING or from airdrop farming? Verify with resolved market PnL.
2. Does the target ever sell via CLOB (side=SELL trades)? If not, all exits are hold-to-resolution.
3. What's the practical minimum trade size on Polymarket before gas makes it uneconomical?
4. Should the bot filter to NBA-only (where the target concentrates capital) to simplify at $100 scale?
5. At $100, is batching sufficient or should minimum portfolio be $250–500 for viable economics?
