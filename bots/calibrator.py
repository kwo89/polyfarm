"""
Weekly position size calibrator — fully hardcoded, no LLM calls.

Runs every 7 days automatically as a background thread.
For each active bot, it:
  1. Fetches the target wallet's last 7 days of on-chain trading activity
  2. Calculates weekly volume and daily average
  3. Updates target_daily_capital in DB (the denominator of the scaling ratio)
  4. Logs the full calibration report to health_events for CEO briefings

Scaling ratio = our_capital / target_daily_capital
  e.g. our=$1000, target daily avg=$5000 → ratio=20% → we copy 20% of each trade size

No manual management required. CEO reads calibration logs in weekly briefings.
"""

import json
import logging
import time
from datetime import datetime, timedelta

from sqlalchemy import select, func

from core.database import get_session
from core.models import BotRegistry, HealthEvent, PaperTrade
from services.polymarket.data_api import get_wallet_activity

logger = logging.getLogger(__name__)

CAPITAL_UPDATE_INTERVAL_SEC = 24 * 60 * 60     # run every 24 hours
CALIBRATION_INTERVAL_SEC    = 7 * 24 * 60 * 60 # run every 7 days
MIN_TRADES_FOR_CALIBRATION  = 5                 # need at least 5 trades to trust the estimate


def calibrate_bot(bot_id: str) -> dict:
    """
    Fetch last 7 days of target wallet activity, compute volume,
    update target_daily_capital, log the report.
    Returns the calibration report dict.
    """
    # Load bot from DB — extract all fields while session is open
    with get_session() as session:
        bot = session.get(BotRegistry, bot_id)
        if not bot or not bot.active:
            return {}
        name            = bot.name
        target_address  = bot.target_address
        our_capital     = bot.our_capital or 100.0
        old_daily_cap   = bot.target_daily_capital or 2000.0

    logger.info("[calibrator] Calibrating %s (wallet …%s)", name, target_address[-6:])

    # Fetch up to 500 recent transactions — filter last 7 days by timestamp
    try:
        activity = get_wallet_activity(target_address, limit=500)
    except Exception as e:
        logger.error("[calibrator] Failed to fetch activity for %s: %s", name, e)
        return {}

    now_ts     = datetime.utcnow().timestamp()
    week_ago   = now_ts - 7 * 86400
    day_ago    = now_ts - 86400

    week_sizes, day_sizes = [], []
    for tx in activity:
        ts = tx.get("timestamp", 0)
        try:
            ts = float(ts)
        except (TypeError, ValueError):
            continue
        # Polymarket API returns timestamps in milliseconds — normalise to seconds
        if ts > 1e11:
            ts /= 1000.0
        if tx.get("type", "").upper() != "TRADE":
            continue
        try:
            size = float(tx.get("usdcSize") or 0)
        except (TypeError, ValueError):
            size = 0.0
        if size <= 0:
            continue
        if ts >= week_ago:
            week_sizes.append(size)
        if ts >= day_ago:
            day_sizes.append(size)

    weekly_volume  = sum(week_sizes)
    daily_24h      = sum(day_sizes)
    daily_avg      = round(weekly_volume / 7, 2) if weekly_volume > 0 else 0.0
    trade_count_7d = len(week_sizes)

    # Only update if we have enough data to trust the estimate
    if trade_count_7d >= MIN_TRADES_FOR_CALIBRATION and daily_avg > 0:
        new_daily_cap = daily_avg
    else:
        new_daily_cap = old_daily_cap
        logger.warning("[calibrator] %s: only %d trades in 7d — keeping existing estimate $%.0f",
                       name, trade_count_7d, old_daily_cap)

    # Calculate scaling ratios
    old_ratio_pct = round(our_capital / old_daily_cap * 100, 2) if old_daily_cap > 0 else 0
    new_ratio_pct = round(our_capital / new_daily_cap * 100, 2) if new_daily_cap > 0 else 0

    # Update DB
    with get_session() as session:
        bot = session.get(BotRegistry, bot_id)
        if bot:
            bot.target_daily_capital = new_daily_cap

    report = {
        "bot_name":               name,
        "wallet":                 f"…{target_address[-8:]}",
        "period_days":            7,
        "trade_count_7d":         trade_count_7d,
        "weekly_volume_usd":      round(weekly_volume, 2),
        "daily_avg_volume_usd":   daily_avg,
        "daily_24h_volume_usd":   round(daily_24h, 2),
        "our_capital_usd":        our_capital,
        "old_target_daily_cap":   old_daily_cap,
        "new_target_daily_cap":   new_daily_cap,
        "old_scaling_ratio_pct":  old_ratio_pct,
        "new_scaling_ratio_pct":  new_ratio_pct,
        "ratio_changed":          new_daily_cap != old_daily_cap,
        "calibrated_at":          datetime.utcnow().isoformat(),
        "trusted":                trade_count_7d >= MIN_TRADES_FOR_CALIBRATION,
    }

    # Log to health_events for CEO weekly briefing
    with get_session() as session:
        session.add(HealthEvent(
            component=f"calibrator:{name}",
            event_type="recalibration",
            details=json.dumps(report),
        ))

    logger.info(
        "[calibrator] %s | 7d vol=$%.0f | daily_avg=$%.0f | ratio: %.1f%% → %.1f%%",
        name, weekly_volume, daily_avg, old_ratio_pct, new_ratio_pct,
    )
    return report


def recalibrate_capital(bot_id: str) -> dict:
    """
    Recompute our_capital = initial_capital + cumulative resolved P&L.
    Runs daily so scaling ratio always reflects real bankroll.
    """
    with get_session() as session:
        bot = session.get(BotRegistry, bot_id)
        if not bot or not bot.active:
            return {}
        name        = bot.name
        old_capital = bot.our_capital or 100.0

        # Lock in initial_capital on first run — never changed after this
        if bot.initial_capital is None:
            bot.initial_capital = old_capital
            session.flush()

        initial = bot.initial_capital

    # Sum ALL resolved hypothetical P&L for this bot
    with get_session() as session:
        cumulative_pnl = session.execute(
            select(func.sum(PaperTrade.hypothetical_pnl))
            .where(PaperTrade.bot_id == bot_id)
            .where(PaperTrade.market_resolved == True)
            .where(PaperTrade.hypothetical_pnl.is_not(None))
        ).scalar_one() or 0.0

    new_capital = round(max(initial + cumulative_pnl, 1.0), 2)  # floor at $1

    with get_session() as session:
        bot = session.get(BotRegistry, bot_id)
        if bot:
            bot.our_capital = new_capital

    change_usd = round(new_capital - old_capital, 2)
    change_pct = round(change_usd / old_capital * 100, 2) if old_capital else 0

    report = {
        "bot_name":         name,
        "initial_capital":  initial,
        "cumulative_pnl":   round(cumulative_pnl, 2),
        "old_capital":      old_capital,
        "new_capital":      new_capital,
        "change_usd":       change_usd,
        "change_pct":       change_pct,
        "updated_at":       datetime.utcnow().isoformat(),
    }

    with get_session() as session:
        session.add(HealthEvent(
            component=f"capital_update:{name}",
            event_type="capital_recalibration",
            details=json.dumps(report),
        ))

    logger.info(
        "[capital] %s | initial=$%.0f | cum_pnl=%+.2f | capital: $%.2f → $%.2f (%+.1f%%)",
        name, initial, cumulative_pnl, old_capital, new_capital, change_pct,
    )
    return report


def run_capital_update_pass():
    """Update our_capital for all active bots based on cumulative P&L."""
    with get_session() as session:
        bot_ids = [
            b.id for b in session.execute(
                select(BotRegistry).where(BotRegistry.active == True)
            ).scalars().all()
        ]
    if not bot_ids:
        return
    logger.info("[capital] Running daily capital update for %d bot(s).", len(bot_ids))
    for bot_id in bot_ids:
        try:
            recalibrate_capital(bot_id)
        except Exception as e:
            logger.exception("[capital] Error updating capital for %s: %s", bot_id, e)


def calibrate_buckets(bot_id: str) -> dict:
    """
    Compute 20/40/60/80th percentile thresholds from the last 500 trades
    of the target wallet. These define 5 buckets mapped to 1%–5% of our capital.
    Runs at bot setup and every 7 days thereafter.
    """
    with get_session() as session:
        bot = session.get(BotRegistry, bot_id)
        if not bot or not bot.active:
            return {}
        name   = bot.name
        wallet = bot.target_address

    activity = get_wallet_activity(wallet, limit=500)

    sizes = []
    for tx in activity:
        if tx.get("type") != "TRADE":
            continue
        try:
            size = float(tx.get("usdcSize") or tx.get("size") or 0)
        except (TypeError, ValueError):
            continue
        if size > 0:
            sizes.append(size)

    if len(sizes) < 5:
        logger.info("[buckets] %s — only %d trades found, skipping bucket calibration", name, len(sizes))
        return {"skipped": True, "reason": f"Only {len(sizes)} trades"}

    sizes.sort()
    n = len(sizes)

    def pct(p: float) -> float:
        return round(sizes[min(int(p / 100 * n), n - 1)], 2)

    t1, t2, t3, t4 = pct(20), pct(40), pct(60), pct(80)

    with get_session() as session:
        bot = session.get(BotRegistry, bot_id)
        if bot:
            bot.bucket_t1, bot.bucket_t2 = t1, t2
            bot.bucket_t3, bot.bucket_t4 = t3, t4

    report = {
        "bot_name":       name,
        "trades_analyzed": len(sizes),
        "min_trade":      sizes[0],
        "max_trade":      sizes[-1],
        "bucket_1_1pct":  f"$0 – ${t1}",
        "bucket_2_2pct":  f"${t1} – ${t2}",
        "bucket_3_3pct":  f"${t2} – ${t3}",
        "bucket_4_4pct":  f"${t3} – ${t4}",
        "bucket_5_5pct":  f"${t4}+",
        "thresholds":     [t1, t2, t3, t4],
        "calibrated_at":  datetime.utcnow().isoformat(),
    }

    with get_session() as session:
        session.add(HealthEvent(
            component=f"buckets:{name}",
            event_type="bucket_calibration",
            details=json.dumps(report),
        ))

    logger.info(
        "[buckets] %s | $0–$%.2f (1%%) / $%.2f–$%.2f (2%%) / $%.2f–$%.2f (3%%) / $%.2f–$%.2f (4%%) / $%.2f+ (5%%) | %d trades",
        name, t1, t1, t2, t2, t3, t3, t4, t4, len(sizes),
    )
    return report


def run_bucket_calibration_pass():
    """Calibrate bucket thresholds for all active bots."""
    with get_session() as session:
        bot_ids = [
            b.id for b in session.execute(
                select(BotRegistry).where(BotRegistry.active == True)
            ).scalars().all()
        ]
    if not bot_ids:
        return
    logger.info("[buckets] Running bucket calibration for %d bot(s).", len(bot_ids))
    for bot_id in bot_ids:
        try:
            calibrate_buckets(bot_id)
        except Exception as e:
            logger.exception("[buckets] Error calibrating buckets for %s: %s", bot_id, e)


def run_calibration_pass():
    """Calibrate all active bots once."""
    with get_session() as session:
        bot_ids = [
            b.id for b in session.execute(
                select(BotRegistry).where(BotRegistry.active == True)
            ).scalars().all()
        ]

    if not bot_ids:
        logger.debug("[calibrator] No active bots to calibrate.")
        return

    logger.info("[calibrator] Starting calibration pass for %d bot(s).", len(bot_ids))
    for bot_id in bot_ids:
        try:
            calibrate_bot(bot_id)
        except Exception as e:
            logger.exception("[calibrator] Error calibrating %s: %s", bot_id, e)


def run_calibrator_loop():
    """
    Runs forever as a daemon thread.
    - Capital update (our_capital from P&L): at startup + every 24 hours
    - Wallet volume calibration (target_daily_capital): at startup + every 7 days
    """
    logger.info("[calibrator] Calibrator started — capital: daily, wallet volume: weekly.")

    # Run all passes immediately at startup
    try:
        run_capital_update_pass()
    except Exception as e:
        logger.exception("[calibrator] Startup capital update failed: %s", e)

    try:
        run_bucket_calibration_pass()
    except Exception as e:
        logger.exception("[calibrator] Startup bucket calibration failed: %s", e)

    try:
        run_calibration_pass()
    except Exception as e:
        logger.exception("[calibrator] Startup wallet calibration failed: %s", e)

    day = 0
    while True:
        time.sleep(CAPITAL_UPDATE_INTERVAL_SEC)   # wake every 24h
        day += 1

        try:
            run_capital_update_pass()
        except Exception as e:
            logger.exception("[calibrator] Daily capital update failed: %s", e)

        if day % 7 == 0:                          # every 7th day
            try:
                run_bucket_calibration_pass()
            except Exception as e:
                logger.exception("[calibrator] Weekly bucket calibration failed: %s", e)
            try:
                run_calibration_pass()
            except Exception as e:
                logger.exception("[calibrator] Weekly wallet calibration failed: %s", e)
