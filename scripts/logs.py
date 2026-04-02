#!/usr/bin/env python3
"""
View recent trade logs from the PolyFarm SQLite database.

Usage:
    python scripts/logs.py                    # last 50 paper trades
    python scripts/logs.py --type target      # all detected target trades (including skipped)
    python scripts/logs.py --type paper       # paper trades only
    python scripts/logs.py --n 100            # last 100 entries
    python scripts/logs.py --bot-id <id>      # filter by bot
"""

import sys
import os
import argparse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select, desc
from core.database import init_db, get_session
from core.models import BotRegistry, PaperTrade, TargetTrade


SIDE_ICON = {"BUY": "🟢", "SELL": "🔴"}
STATUS_ICON = {"paper": "📄", "skipped": "⏭ ", "pending": "⏳", "executed": "✅", "live": "🔴"}


def main():
    parser = argparse.ArgumentParser(description="View PolyFarm trade logs")
    parser.add_argument("--type", choices=["paper", "target", "all"], default="paper",
                        help="Which log to show (default: paper)")
    parser.add_argument("--n", type=int, default=50, help="Number of entries (default: 50)")
    parser.add_argument("--bot-id", help="Filter to a specific bot ID")
    args = parser.parse_args()

    init_db()

    with get_session() as session:
        # Load bot names for display
        bots = {b.id: b.name for b in session.execute(select(BotRegistry)).scalars().all()}

        if args.type in ("paper", "all"):
            _show_paper_trades(session, bots, args.n, args.bot_id)

        if args.type in ("target", "all"):
            _show_target_trades(session, bots, args.n, args.bot_id)


def _show_paper_trades(session, bots, n, bot_id_filter):
    q = select(PaperTrade).order_by(desc(PaperTrade.created_at)).limit(n)
    if bot_id_filter:
        q = q.where(PaperTrade.bot_id == bot_id_filter)
    trades = session.execute(q).scalars().all()

    print(f"\n{'─'*90}")
    print(f"  PAPER TRADES  (last {n})")
    print(f"{'─'*90}")
    print(f"  {'Time (UTC)':<20} {'Bot':<14} {'Side':<6} {'Out':<6} {'Size':>8} {'Price':>7}  Market/Question")
    print(f"  {'─'*86}")

    if not trades:
        print("  No paper trades yet.")
    for t in trades:
        bot_name = bots.get(t.bot_id, t.bot_id[:8])
        icon = SIDE_ICON.get(t.side, "  ")
        ts = t.created_at.strftime("%Y-%m-%d %H:%M:%S") if t.created_at else "—"
        question = (t.question or t.market_id)[:45]
        print(f"  {ts:<20} {bot_name:<14} {icon} {t.side:<4} {t.outcome:<6} "
              f"${t.hypothetical_size:>6.2f}  {t.hypothetical_price:>5.3f}  {question}")


def _show_target_trades(session, bots, n, bot_id_filter):
    q = select(TargetTrade).order_by(desc(TargetTrade.detected_at)).limit(n)
    if bot_id_filter:
        q = q.where(TargetTrade.bot_id == bot_id_filter)
    trades = session.execute(q).scalars().all()

    print(f"\n{'─'*100}")
    print(f"  TARGET TRADES (detected from watched wallet)  (last {n})")
    print(f"{'─'*100}")
    print(f"  {'Time (UTC)':<20} {'Bot':<14} {'St':<3} {'Side':<5} {'Out':<6} "
          f"{'Raw $':>8} {'Scaled $':>9}  Skip reason / Question")
    print(f"  {'─'*96}")

    if not trades:
        print("  No target trades detected yet.")
    for t in trades:
        bot_name = bots.get(t.bot_id, t.bot_id[:8])
        icon = STATUS_ICON.get(t.status, "  ")
        ts = t.detected_at.strftime("%Y-%m-%d %H:%M:%S") if t.detected_at else "—"
        raw = t.target_size or 0
        scaled = t.scaled_size or 0
        detail = t.skip_reason or (t.question or t.market_id or "")
        detail = (detail or "")[:45]
        print(f"  {ts:<20} {bot_name:<14} {icon} {t.side:<5} {t.outcome:<6} "
              f"${raw:>6.2f}  ${scaled:>7.2f}  {detail}")

    print()


if __name__ == "__main__":
    main()
