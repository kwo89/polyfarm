"""
Hardcoded risk rules — no LLM involved.
Every proposed trade passes through check_trade() before execution.
All constants can be overridden via environment variables if needed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal, Optional

from sqlalchemy import func, select

from core.database import get_session
from core.models import DailyPnl, Position

logger = logging.getLogger(__name__)

# ── Risk constants ────────────────────────────────────────────────────────────
MIN_TRADE_SIZE_USD: float = 0.50        # skip trades below $0.50
MAX_TRADE_PCT: float = 0.08             # 8% of portfolio per single trade
MAX_MARKET_PCT: float = 0.25           # 25% of portfolio in one market
MIN_LIQUID_RESERVE_USD: float = 15.00  # always keep $15 undeployed
DAILY_LOSS_LIMIT_PCT: float = 0.20     # halt bot if down >20% today
MAX_CONCURRENT_MARKETS: int = 20


@dataclass
class TradeProposal:
    bot_id: str
    market_id: str
    outcome: str                        # YES | NO
    side: Literal["BUY", "SELL"]
    proposed_size_usd: float            # already scaled to our portfolio
    current_price: float                # 0.0–1.0


@dataclass
class RiskDecision:
    approved: bool
    reason: str                         # human-readable explanation
    adjusted_size: Optional[float] = None  # set when trade is capped to max; use this size


def check_trade(proposal: TradeProposal, portfolio_balance: float) -> RiskDecision:
    """
    Runs all hardcoded risk checks in order.
    Returns RiskDecision(approved=True/False, reason=...).
    """
    size = proposal.proposed_size_usd
    adjusted_size: Optional[float] = None

    # 1. Minimum size — skip if below $0.50
    if size < MIN_TRADE_SIZE_USD:
        return RiskDecision(False, f"Below min size ${MIN_TRADE_SIZE_USD:.2f} (got ${size:.2f})")

    # 2. Maximum per-trade size — cap and continue instead of skipping
    max_allowed = round(portfolio_balance * MAX_TRADE_PCT, 2)
    if size > max_allowed:
        size = max_allowed
        adjusted_size = size

    # 3. Liquid reserve check
    if (portfolio_balance - size) < MIN_LIQUID_RESERVE_USD:
        return RiskDecision(False, f"Would breach liquid reserve ${MIN_LIQUID_RESERVE_USD:.2f}")

    # 4. Daily loss limit
    today = date.today().isoformat()
    with get_session() as session:
        row = session.execute(
            select(DailyPnl).where(
                DailyPnl.bot_id == proposal.bot_id,
                DailyPnl.date == today,
            )
        ).scalar_one_or_none()
        if row:
            daily_loss = min(0.0, row.realized_pnl)
            loss_limit = portfolio_balance * DAILY_LOSS_LIMIT_PCT
            if abs(daily_loss) >= loss_limit:
                return RiskDecision(False, f"Daily loss limit hit: ${daily_loss:.2f} >= ${loss_limit:.2f}")

        # 5. Market exposure check (uses capped size; skip for SELL — reduces exposure)
        if proposal.side == "BUY":
            exposure = _get_market_exposure(session, proposal.bot_id, proposal.market_id)
            max_market = portfolio_balance * MAX_MARKET_PCT
            if (exposure + size) > max_market:
                return RiskDecision(
                    False,
                    f"Market exposure would be ${exposure + size:.2f}, limit is ${max_market:.2f} (25%)"
                )

            # 6. Concurrent markets cap
            active_markets = _count_active_markets(session, proposal.bot_id)
            if active_markets >= MAX_CONCURRENT_MARKETS:
                return RiskDecision(False, f"Already in {active_markets} markets (limit {MAX_CONCURRENT_MARKETS})")

    return RiskDecision(True, "All checks passed", adjusted_size=adjusted_size)


TIER_PCTS = [0.01, 0.02, 0.03, 0.04, 0.05]   # 1% – 5% of our capital per tier


def calculate_scaled_size(
    target_size_usd: float,
    target_daily_capital: float,
    our_balance: float,
    bucket_thresholds: Optional[list] = None,
) -> float:
    """
    Tiered sizing (preferred): when bucket_thresholds [t1,t2,t3,t4] are set,
    maps the target's trade size to one of 5 tiers (1%–5% of our capital).
    Tier is determined by which of the 5 percentile buckets the target size falls into.

    Fallback (no thresholds yet): proportional scaling by daily volume ratio.
    Returns 0.0 if the resulting size is below MIN_TRADE_SIZE_USD (skip trade).
    """
    if bucket_thresholds and len(bucket_thresholds) == 4 and all(t is not None for t in bucket_thresholds):
        t1, t2, t3, t4 = bucket_thresholds
        if target_size_usd <= t1:
            tier = 0
        elif target_size_usd <= t2:
            tier = 1
        elif target_size_usd <= t3:
            tier = 2
        elif target_size_usd <= t4:
            tier = 3
        else:
            tier = 4
        size = round(our_balance * TIER_PCTS[tier], 2)
        return size if size >= MIN_TRADE_SIZE_USD else 0.0

    # Fallback: proportional (used until first bucket calibration completes)
    if target_daily_capital <= 0:
        return 0.0
    ratio = our_balance / target_daily_capital
    scaled = target_size_usd * ratio
    return scaled if scaled >= MIN_TRADE_SIZE_USD else 0.0


def _get_market_exposure(session, bot_id: str, market_id: str) -> float:
    """Sum of current BUY positions in this market for this bot."""
    rows = session.execute(
        select(Position).where(
            Position.bot_id == bot_id,
            Position.market_id == market_id,
        )
    ).scalars().all()
    return sum(
        (p.size * (p.avg_cost or 0.0))
        for p in rows
        if p.size > 0
    )


def _count_active_markets(session, bot_id: str) -> int:
    """Count distinct markets where the bot has open positions."""
    result = session.execute(
        select(func.count(Position.market_id.distinct())).where(
            Position.bot_id == bot_id,
            Position.size > 0,
        )
    ).scalar_one()
    return result or 0
