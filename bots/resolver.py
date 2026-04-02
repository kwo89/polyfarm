"""
Market resolution checker — runs every 15 minutes as a background thread.

Source of truth: Polymarket Gamma API.
We NEVER infer Won/Lost from our own P&L calculations.
The flow is:
  1. Ask Polymarket: is this market closed? Which outcome won?
  2. Record the winning_outcome directly from their data
  3. Compare our trade's outcome vs winning_outcome → Won or Lost
  4. Calculate hypothetical P&L purely as a consequence of that result

Won/Lost status is always determined by Polymarket's on-chain resolution data,
not by any internal formula or estimation.
"""

import logging
import time
from datetime import datetime, date
from typing import Optional

from sqlalchemy import select, func

from core.database import get_session
from core.models import PaperTrade, MarketResolution, DailyPnl, BotRegistry
from services.polymarket.data_api import get_market

logger = logging.getLogger(__name__)

RESOLVE_INTERVAL_SEC = 15 * 60   # 15 minutes


# ── P&L calculation ───────────────────────────────────────────────────────────

def _calc_pnl_from_dict(trade: dict, winning_outcome: str) -> float:
    """
    Calculate hypothetical P&L from a plain trade dict.

    BUY: we spent hypothetical_size USD to buy shares at hypothetical_price.
         If our outcome wins → receive (size / price) USD → pnl = size*(1/price - 1)
         If our outcome loses → receive 0 → pnl = -size

    SELL: position already closed at trade price; resolution adds no further P&L.
    """
    if trade.get("side") == "SELL":
        return 0.0

    size  = trade.get("hypothetical_size")  or 0.0
    price = trade.get("hypothetical_price") or 0.5
    if price <= 0:
        price = 0.001

    won = (trade.get("outcome") or "").upper() == (winning_outcome or "").upper()

    if won:
        shares = size / price
        return round(shares - size, 4)
    else:
        return round(-size, 4)


# ── Gamma API parsing ─────────────────────────────────────────────────────────

def _parse_resolution(market_data: dict) -> Optional[str]:
    """
    Extract the winning outcome from Polymarket API data.
    Returns "YES", "NO", or None if not yet resolved.

    Source of truth: Polymarket CLOB API (clob.polymarket.com/markets/{id})
    CLOB response has tokens[] with outcome + price fields.
    A price of exactly 1.0 means that outcome won.
    A price of exactly 0.0 means that outcome lost.

    Also handles Gamma API format as fallback (winnerOutcome / tokens[].winner).
    """
    if not market_data:
        return None

    # Market must be closed on Polymarket's end
    if not market_data.get("closed") and not market_data.get("resolved"):
        return None

    tokens = market_data.get("tokens", [])

    # 1. CLOB API format: token price == 1.0 means that outcome won
    #    This is the primary signal for BTC 5-min and all CLOB markets
    for token in tokens:
        try:
            price = float(token.get("price", -1))
            if price == 1.0:
                raw = str(token.get("outcome", "")).strip().upper()
                if raw in ("YES", "NO"):
                    return raw
                if raw in ("TRUE", "UP", "1"):
                    return "YES"
                if raw in ("FALSE", "DOWN", "0"):
                    return "NO"
        except (TypeError, ValueError):
            pass

    # 2. Explicit winner flag (CLOB and Gamma both use this)
    for token in tokens:
        if token.get("winner") is True:
            raw = str(token.get("outcome", "")).strip().upper()
            if raw in ("YES", "NO"):
                return raw
            if raw in ("TRUE", "UP", "1"):
                return "YES"
            if raw in ("FALSE", "DOWN", "0"):
                return "NO"

    # 3. Gamma API format: winnerOutcome string field
    winner = str(market_data.get("winnerOutcome") or "").strip().upper()
    if winner in ("YES", "NO"):
        return winner

    # 4. outcomePrices array (Gamma format): ["1", "0"] → YES won, ["0", "1"] → NO won
    outcome_prices_raw = market_data.get("outcomePrices")
    outcomes_raw = market_data.get("outcomes")
    if outcome_prices_raw and outcomes_raw:
        try:
            import json as _json
            prices = _json.loads(outcome_prices_raw) if isinstance(outcome_prices_raw, str) else outcome_prices_raw
            outcomes = _json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
            for i, price in enumerate(prices):
                if float(price) == 1.0 and i < len(outcomes):
                    raw = str(outcomes[i]).strip().upper()
                    if raw in ("YES", "NO"):
                        return raw
        except (ValueError, TypeError, KeyError):
            pass

    # Market is closed but winner not yet determinable
    logger.debug("Market closed but winner not determinable: %s",
                 str(market_data.get("condition_id") or market_data.get("conditionId", ""))[:16])
    return None


# ── Daily P&L update ──────────────────────────────────────────────────────────

def _patch_daily_pnl(session, bot_id: str, trade_date: date, pnl_delta: float):
    """Add resolved P&L to the daily_pnl row for a bot/date."""
    row = session.execute(
        select(DailyPnl).where(
            DailyPnl.bot_id == bot_id,
            DailyPnl.date == str(trade_date),
        )
    ).scalar_one_or_none()

    if row:
        row.realized_pnl = round((row.realized_pnl or 0.0) + pnl_delta, 4)
    else:
        # Create a minimal row if it doesn't exist yet
        session.add(DailyPnl(
            bot_id=bot_id,
            date=str(trade_date),
            realized_pnl=round(pnl_delta, 4),
            unrealized_pnl=0.0,
            total_traded_usd=0.0,
            num_trades=0,
        ))


# ── Core resolution pass ──────────────────────────────────────────────────────

def run_resolution_pass():
    """
    Check all unresolved paper trades once.
    Groups by market_id to minimise API calls (one call per market, not per trade).
    All ORM attribute access happens inside session blocks to avoid DetachedInstanceError.
    """
    # Read everything we need while session is open — store as plain dicts
    with get_session() as session:
        rows = session.execute(
            select(PaperTrade).where(PaperTrade.market_resolved == False)
        ).scalars().all()

        # Extract all fields while still inside session
        unresolved = [
            {
                "id": t.id,
                "market_id": t.market_id,
                "question": t.question,
                "outcome": t.outcome,
                "side": t.side,
                "hypothetical_size": t.hypothetical_size,
                "hypothetical_price": t.hypothetical_price,
                "bot_id": t.bot_id,
                "created_at": t.created_at,
            }
            for t in rows
        ]

    if not unresolved:
        logger.debug("Resolution pass: no unresolved trades.")
        return

    # Group by market_id
    markets: dict[str, list[dict]] = {}
    for t in unresolved:
        markets.setdefault(t["market_id"], []).append(t)

    logger.info("Resolution pass: %d unresolved trades across %d markets",
                len(unresolved), len(markets))

    resolved_count = 0

    for market_id, trades in markets.items():
        try:
            market_data = get_market(market_id)
            winning_outcome = _parse_resolution(market_data)
        except Exception as e:
            logger.warning("Could not fetch market %s: %s", market_id[:12], e)
            continue

        # Update resolution cache
        with get_session() as session:
            cache = session.get(MarketResolution, market_id)
            if not cache:
                cache = MarketResolution(market_id=market_id)
                session.add(cache)
            cache.question = market_data.get("question") or trades[0]["question"]
            cache.resolved = winning_outcome is not None
            cache.winning_outcome = winning_outcome
            cache.last_checked = datetime.utcnow()
            if winning_outcome and not cache.resolved_at:
                cache.resolved_at = datetime.utcnow()

        if not winning_outcome:
            continue

        # Market is resolved — update all trades for it
        with get_session() as session:
            for trade in trades:
                db_trade = session.get(PaperTrade, trade["id"])
                if not db_trade or db_trade.market_resolved:
                    continue

                pnl = _calc_pnl_from_dict(trade, winning_outcome)
                db_trade.market_resolved = True
                db_trade.winning_outcome = winning_outcome
                db_trade.hypothetical_pnl = pnl

                trade_date = (trade["created_at"] or datetime.utcnow()).date()
                _patch_daily_pnl(session, trade["bot_id"], trade_date, pnl)

                won = (trade["outcome"] or "").upper() == winning_outcome.upper()
                logger.info(
                    "✅ Polymarket resolved %s | bet=%s winner=%s → %s | hyp. P&L: %+.2f",
                    market_id[:12], trade["outcome"], winning_outcome,
                    "WIN" if won else "LOSS", pnl,
                )
                resolved_count += 1

    if resolved_count:
        logger.info("Resolution pass complete: %d trades resolved.", resolved_count)


# ── Background thread ─────────────────────────────────────────────────────────

def run_resolver_loop():
    """
    Runs forever as a daemon thread.
    Checks market resolutions every 15 minutes.
    """
    logger.info("Resolution checker started (interval: %ds)", RESOLVE_INTERVAL_SEC)
    while True:
        try:
            run_resolution_pass()
        except Exception as e:
            logger.exception("Resolution pass failed: %s", e)
        time.sleep(RESOLVE_INTERVAL_SEC)
