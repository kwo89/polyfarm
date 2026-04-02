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

POLL_FOR_NEW_BOTS_INTERVAL = 30  # seconds between checks for newly registered bots


def run_all_bots(bot_id: Optional[str] = None):
    """
    Launch all active bots as daemon threads.
    Keeps running forever — polls for newly added bots every 30s.
    Never exits on its own (Docker restart: unless-stopped handles crashes).
    """
    running: dict[str, threading.Thread] = {}  # bot_id -> thread

    logger.info("PolyFarm bot runner started. Waiting for bots...")

    while True:
        try:
            with get_session() as session:
                q = select(BotRegistry).where(BotRegistry.active == True).where(BotRegistry.paused == False)
                if bot_id:
                    q = q.where(BotRegistry.id == bot_id)
                bots = session.execute(q).scalars().all()
                bot_configs = [(b.id, b.name) for b in bots]

            if not bot_configs:
                logger.info("No active bots registered yet. Checking again in %ds...", POLL_FOR_NEW_BOTS_INTERVAL)
            else:
                # Start any bot that isn't already running
                for bid, name in bot_configs:
                    if bid not in running or not running[bid].is_alive():
                        if bid in running:
                            logger.warning("Bot %s thread died — restarting.", name)
                        else:
                            logger.info("Starting bot: %s (%s)", name, bid)
                        bot = CopyBot(bid)
                        t = threading.Thread(target=bot.run, name=f"bot-{name}", daemon=True)
                        t.start()
                        running[bid] = t

                alive = [bid for bid, t in running.items() if t.is_alive()]
                logger.info("%d bot(s) running: %s", len(alive),
                            ", ".join(n for bid, n in bot_configs if bid in alive))

        except Exception as e:
            logger.exception("Registry loop error: %s", e)

        time.sleep(POLL_FOR_NEW_BOTS_INTERVAL)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run PolyFarm copy-trading bots")
    parser.add_argument("--bot-id", help="Run a specific bot by ID (default: all active)")
    args = parser.parse_args()

    init_db()
    run_all_bots(bot_id=args.bot_id)
