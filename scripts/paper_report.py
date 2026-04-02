#!/usr/bin/env python3
"""
Print a paper trading P&L summary to the terminal.

Usage:
    python scripts/paper_report.py
    python scripts/paper_report.py --bot-id <id>
    python scripts/paper_report.py --days 7
"""

import sys
import os
import argparse
from datetime import datetime, timedelta, date
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select, func
from core.database import init_db, get_session
from core.models import BotRegistry, PaperTrade, TargetTrade, DailyPnl


def fmt_usd(val: Optional[float]) -> str:
    if val is None:
        return "  —    "
    sign = "+" if val >= 0 else ""
    return f"{sign}${val:.2f}"


def main():
    parser = argparse.ArgumentParser(description="Paper trading P&L report")
    parser.add_argument("--bot-id", help="Filter to a specific bot ID")
    parser.add_argument("--days", type=int, default=7, help="Look-back window in days (default: 7)")
    args = parser.parse_args()

    init_db()
    since = (datetime.utcnow() - timedelta(days=args.days)).isoformat()

    with get_session() as session:
        # Load bots
        q = select(BotRegistry)
        if args.bot_id:
            q = q.where(BotRegistry.id == args.bot_id)
        bots = session.execute(q).scalars().all()

        if not bots:
            print("No bots found.")
            return

        print(f"\n{'─'*60}")
        print(f"  POLYFARM — Paper Trading Report (last {args.days} days)")
        print(f"  Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
        print(f"{'─'*60}\n")

        for bot in bots:
            print(f"  Bot: {bot.name}  [{bot.id[:8]}]")
            print(f"  Target: {bot.target_address}")
            print(f"  Mode: {'PAPER' if bot.paper_mode else 'LIVE'} | "
                  f"Active: {'YES' if bot.active else 'NO'} | "
                  f"Paused: {'YES' if bot.paused else 'NO'}")

            # Trade counts
            total_detected = session.execute(
                select(func.count(TargetTrade.id)).where(
                    TargetTrade.bot_id == bot.id,
                    TargetTrade.detected_at >= since,
                )
            ).scalar_one()

            total_paper = session.execute(
                select(func.count(PaperTrade.id)).where(
                    PaperTrade.bot_id == bot.id,
                    PaperTrade.created_at >= since,
                )
            ).scalar_one()

            total_skipped = session.execute(
                select(func.count(TargetTrade.id)).where(
                    TargetTrade.bot_id == bot.id,
                    TargetTrade.status == "skipped",
                    TargetTrade.detected_at >= since,
                )
            ).scalar_one()

            total_volume = session.execute(
                select(func.sum(PaperTrade.hypothetical_value)).where(
                    PaperTrade.bot_id == bot.id,
                    PaperTrade.created_at >= since,
                )
            ).scalar_one()

            # Resolved P&L
            resolved = session.execute(
                select(PaperTrade).where(
                    PaperTrade.bot_id == bot.id,
                    PaperTrade.market_resolved == True,
                    PaperTrade.created_at >= since,
                )
            ).scalars().all()

            resolved_pnl = sum(t.hypothetical_pnl or 0 for t in resolved)

            print(f"\n  Activity ({args.days}d):")
            print(f"    Trades detected:   {total_detected}")
            print(f"    Paper executed:    {total_paper}")
            print(f"    Skipped:           {total_skipped}")
            print(f"    Hypothetical vol:  ${total_volume or 0:.2f}")

            print(f"\n  P&L (resolved markets only):")
            if resolved:
                print(f"    Resolved trades:   {len(resolved)}")
                print(f"    Realized P&L:      {fmt_usd(resolved_pnl)}")
                wins = [t for t in resolved if (t.hypothetical_pnl or 0) > 0]
                losses = [t for t in resolved if (t.hypothetical_pnl or 0) < 0]
                win_rate = len(wins) / len(resolved) * 100 if resolved else 0
                print(f"    Win rate:          {win_rate:.0f}%  ({len(wins)}W / {len(losses)}L)")
            else:
                print(f"    No resolved markets yet.")

            # Daily breakdown
            daily_rows = session.execute(
                select(DailyPnl).where(
                    DailyPnl.bot_id == bot.id,
                ).order_by(DailyPnl.date.desc()).limit(args.days)
            ).scalars().all()

            if daily_rows:
                print(f"\n  Daily summary:")
                print(f"    {'Date':<12} {'Trades':>8} {'Vol':>10} {'P&L':>10}")
                print(f"    {'─'*44}")
                for row in daily_rows:
                    print(f"    {row.date:<12} {row.num_trades:>8} "
                          f"${row.total_traded_usd:>8.2f} {fmt_usd(row.realized_pnl):>10}")

            print(f"\n{'─'*60}\n")


if __name__ == "__main__":
    main()
