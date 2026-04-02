"""
Bot registry — loads active bots from DB and runs them as threads.

Usage:
    python -m bots.registry
    python -m bots.registry --bot-id <specific_bot_id>
"""

import argparse
import logging
import threading
import time
from typing import Optional

from sqlalchemy import select

from core.database import get_session, init_db
from core.models import BotRegistry
from bots.base_bot import CopyBot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def run_all_bots(bot_id: Optional[str] = None):
    """Launch all active bots (or a specific one) as daemon threads."""
    with get_session() as session:
        q = select(BotRegistry).where(BotRegistry.active == True)
        if bot_id:
            q = q.where(BotRegistry.id == bot_id)
        bots = session.execute(q).scalars().all()
        bot_configs = [(b.id, b.name) for b in bots]

    if not bot_configs:
        logger.warning("No active bots found. Use scripts/add_bot.py to register one.")
        return

    threads = []
    for bid, name in bot_configs:
        logger.info("Starting bot: %s (%s)", name, bid)
        bot = CopyBot(bid)
        t = threading.Thread(target=bot.run, name=f"bot-{name}", daemon=True)
        t.start()
        threads.append(t)

    logger.info("Running %d bot(s). Press Ctrl+C to stop.", len(threads))
    try:
        while True:
            alive = [t for t in threads if t.is_alive()]
            if not alive:
                logger.warning("All bot threads have exited.")
                break
            time.sleep(10)
    except KeyboardInterrupt:
        logger.info("Shutting down.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run PolyFarm copy-trading bots")
    parser.add_argument("--bot-id", help="Run a specific bot by ID (default: all active)")
    args = parser.parse_args()

    init_db()
    run_all_bots(bot_id=args.bot_id)
