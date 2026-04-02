"""
Market resolution checker — runs every 15 minutes as a background thread.

For every unresolved paper trade, checks Polymarket's Gamma API to see if the
market has resolved. When it has:
  1. Updates paper_trades: market_resolved, winning_outcome, hypothetical_pnl
  2. Updates market_resolutions cache
  3. Patches daily_pnl with the newly realised P&L

P&L formula (for BUY trades):
  shares      = hypothetical_size / hypothetical_price
  win_pnl     = shares - hypothetical_size   (= size * (1/price - 1))
  lose_pnl    = -hypothetical_size

For SELL trades the position was already closed — we log 0 P&L on resolution
(the gain/loss was realised at trade time vs the position cost basis).
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

def _calc_pnl(trade: PaperTrade, winning_outcome: str) -> float:
    """
    Calculate hypothetical P&L for a resolved paper trade.

    BUY: we spent hypothetical_size USD to buy shares at hypothetical_price.
         If our outcome wins → receive (size / price) USD → pnl = size*(1/price - 1)
         If our outcome loses → receive 0 → pnl = -size

    SELL: position was closed at trade price; resolution doesn't create new P&L
          (would need original cost basis to calculate accurately).
    """
    if trade.side == "SELL":
        return 0.0

    size = trade.hypothetical_size or 0.0
    price = trade.hypothetical_price or 0.5

    if price <= 0:
        price = 0.001  # guard against division by zero

    won = (trade.outcome or "").upper() == (winning_outcome or "").upper()

    if won:
        shares = size / price
        return round(shares - size, 4)
    else:
        return round(-size, 4)


# ── Gamma API parsing ─────────────────────────────────────────────────────────

def _parse_resolution(market_data: dict) -> Optional[str]:
    """
    Extract the winning outcome string from a Gamma API market response.
    Returns "YES", "NO", or None if not yet resolved.

    Gamma API market fields we look for:
      - closed: bool
      - tokens: [{"outcome": "Yes", "winner": true}, ...]
      - winnerOutcome: "Yes" | "No"
    """
    if not market_data:
        return None

    # Must be closed/resolved
    if not market_data.get("closed") and not market_data.get("resolved"):
        return None

    # Try tokens array first (most reliable)
    tokens = market_data.get("tokens", [])
    for token in tokens:
        if token.get("winner"):
            raw = str(token.get("outcome", "")).strip().upper()
            if raw in ("YES", "NO"):
                return raw
            if raw == "TRUE":
                return "YES"
            if raw == "FALSE":
                return "NO"

    # Fallback: winnerOutcome field
    winner = market_data.get("winnerOutcome", "")
    if winner:
        raw = str(winner).strip().upper()
        if raw in ("YES", "NO"):
            return raw

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
    """
    with get_session() as session:
        unresolved = session.execute(
            select(PaperTrade).where(PaperTrade.market_resolved == False)
        ).scalars().all()

    if not unresolved:
        logger.debug("Resolution pass: no unresolved trades.")
        return

    # Group by market_id
    markets: dict[str, list[PaperTrade]] = {}
    for t in unresolved:
        markets.setdefault(t.market_id, []).append(t)

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

        # Update cache regardless of resolution status
        with get_session() as session:
            cache = session.get(MarketResolution, market_id)
            if not cache:
                cache = MarketResolution(market_id=market_id)
                session.add(cache)
            cache.question = market_data.get("question") or trades[0].question
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
                db_trade = session.get(PaperTrade, trade.id)
                if not db_trade or db_trade.market_resolved:
                    continue

                pnl = _calc_pnl(db_trade, winning_outcome)
                db_trade.market_resolved = True
                db_trade.winning_outcome = winning_outcome
                db_trade.hypothetical_pnl = pnl

                trade_date = (db_trade.created_at or datetime.utcnow()).date()
                _patch_daily_pnl(session, db_trade.bot_id, trade_date, pnl)

                won = (db_trade.outcome or "").upper() == winning_outcome.upper()
                logger.info(
                    "✅ Resolved %s | %s %s | %s | P&L: %+.2f",
                    market_id[:12],
                    db_trade.side, db_trade.outcome,
                    "WIN" if won else "LOSS",
                    pnl,
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
