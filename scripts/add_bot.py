#!/usr/bin/env python3
"""
Register a new copy-trading bot by providing a target Polymarket proxy wallet address.

Usage:
    python scripts/add_bot.py <wallet_address> [--name "My Bot"] [--capital 2500] [--interval 30]

Example:
    python scripts/add_bot.py 0xABCDEF1234567890 --name "BTC Trader" --capital 2600
"""

import sys
import os
import argparse
import uuid
from sqlalchemy import select

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.database import init_db, get_session
from core.models import BotRegistry


def main():
    parser = argparse.ArgumentParser(description="Register a new copy-trading bot")
    parser.add_argument("target_address", help="Polymarket proxy wallet address to copy")
    parser.add_argument("--name", default="", help="Friendly name for this bot")
    parser.add_argument("--capital", type=float, default=2000.0,
                        help="Estimated target wallet's deployed capital in USD (default: 2000). "
                             "Used for proportional sizing. Check their positions to estimate.")
    parser.add_argument("--interval", type=int, default=30, help="Poll interval in seconds (default: 30)")
    args = parser.parse_args()

    address = args.target_address.lower().strip()
    name = args.name or f"Bot-{address[:6]}"

    init_db()

    with get_session() as session:
        existing = session.execute(
            select(BotRegistry).where(BotRegistry.target_address == address)
        ).scalar_one_or_none()

        if existing:
            print(f"ERROR: A bot already exists for {address} (id={existing.id}, name={existing.name})")
            sys.exit(1)

        bot_id = str(uuid.uuid4())
        bot = BotRegistry(
            id=bot_id,
            name=name,
            target_address=address,
            wallet_address="",
            poll_interval_sec=args.interval,
            target_daily_capital=args.capital,
            paper_mode=True,
            active=True,
            paused=False,
        )
        session.add(bot)

    ratio = 100.0 / args.capital
    print(f"\n✓ Bot registered:")
    print(f"  ID:              {bot_id}")
    print(f"  Name:            {name}")
    print(f"  Target wallet:   {address}")
    print(f"  Target capital:  ${args.capital:,.0f}")
    print(f"  Scaling ratio:   {ratio:.3f}× (our $100 / target ${args.capital:,.0f})")
    print(f"  Min trade size:  $1.00 → target must trade ≥ ${1.0/ratio:.0f} for us to copy")
    print(f"  Poll interval:   {args.interval}s")
    print(f"  Mode:            PAPER")
    print(f"\nTo start:")
    print(f"  python -m bots.registry --bot-id {bot_id}")


if __name__ == "__main__":
    main()
