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

from sqlalchemy import select

from core.database import get_session
from core.models import BotRegistry, HealthEvent
from services.polymarket.data_api import get_wallet_activity

logger = logging.getLogger(__name__)

CALIBRATION_INTERVAL_SEC = 7 * 24 * 60 * 60   # run every 7 days
MIN_TRADES_FOR_CALIBRATION = 5                  # need at least 5 trades to trust the estimate


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
    Calibrates all bots immediately on startup, then every 7 days.
    """
    logger.info("[calibrator] Weekly calibrator started (interval: 7 days).")

    # Run once at startup so scaling ratio is fresh
    try:
        run_calibration_pass()
    except Exception as e:
        logger.exception("[calibrator] Startup calibration failed: %s", e)

    while True:
        time.sleep(CALIBRATION_INTERVAL_SEC)
        try:
            run_calibration_pass()
        except Exception as e:
            logger.exception("[calibrator] Calibration pass failed: %s", e)
