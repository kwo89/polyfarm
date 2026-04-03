"""
Loads config.yml and syncs bot definitions into the database.

Secrets (wallet addresses, API keys) are read from .env — never from config.yml.
Bot wallet env var convention: BOT_{UPPERNAME}_TARGET_WALLET
  e.g. name "Bot1"  → BOT_BOT1_TARGET_WALLET
  e.g. name "Alpha" → BOT_ALPHA_TARGET_WALLET

Usage:
    from core.config_loader import load_config, sync_bots_from_config
    cfg = load_config()
    sync_bots_from_config()   # call at startup
"""

import logging
import os
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select

from core.database import get_session
from core.models import BotRegistry

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent.parent / "config.yml"


def load_config() -> dict[str, Any]:
    """Load and return config.yml as a dict."""
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"config.yml not found at {CONFIG_PATH}")
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _env_key(bot_name: str) -> str:
    """Convert bot name to env var prefix: 'Bot-1' → 'BOT_BOT_1'"""
    safe = bot_name.upper().replace("-", "_").replace(" ", "_")
    return f"BOT_{safe}"


def get_bot_wallet(bot_name: str) -> str:
    """Read wallet address from .env for a given bot name."""
    key = f"{_env_key(bot_name)}_TARGET_WALLET"
    val = os.environ.get(key, "")
    if not val:
        logger.warning("Wallet not set: %s (add to .env)", key)
    return val


def get_bot_capital(bot_name: str) -> float:
    """Read target capital from .env for a given bot name."""
    key = f"{_env_key(bot_name)}_TARGET_CAPITAL"
    try:
        return float(os.environ.get(key, "2000"))
    except ValueError:
        return 2000.0


def sync_bots_from_config():
    """
    Sync bots defined in config.yml into the database.
    - Matches existing bots by wallet address (so renaming works correctly)
    - Creates new bots that don't exist yet
    - Updates name, settings, active/paused state for existing bots
    - Never deletes bots (preserves trade history)
    - Skips bots with no wallet configured in .env
    """
    cfg = load_config()
    bot_cfgs = cfg.get("bots", [])

    if not bot_cfgs:
        logger.warning("No bots defined in config.yml")
        return

    with get_session() as session:
        all_bots = session.execute(select(BotRegistry)).scalars().all()
        # Match by wallet address — renaming in config.yml just updates the name
        by_wallet = {b.target_address.lower(): b for b in all_bots}

        for bot_cfg in bot_cfgs:
            name = bot_cfg["name"]
            wallet = get_bot_wallet(name)

            if not wallet:
                logger.warning("Skipping bot '%s' — no wallet in .env (%s_TARGET_WALLET)", name, _env_key(name))
                continue

            capital = get_bot_capital(name)
            active = bot_cfg.get("active", True)
            paper_mode = bot_cfg.get("paper_mode", True)
            poll_interval = bot_cfg.get("poll_interval_sec", 30)
            our_capital = float(bot_cfg.get("our_capital", 100.0))

            existing = by_wallet.get(wallet.lower())

            if existing:
                changed = []
                if existing.name != name:
                    changed.append(f"name: {existing.name!r} → {name!r}")
                    existing.name = name
                if existing.active != active:
                    existing.active = active
                    changed.append(f"active={active}")
                if existing.paper_mode != paper_mode:
                    existing.paper_mode = paper_mode
                    changed.append(f"paper_mode={paper_mode}")
                if existing.poll_interval_sec != poll_interval:
                    existing.poll_interval_sec = poll_interval
                    changed.append(f"poll={poll_interval}s")
                if existing.target_daily_capital != capital:
                    existing.target_daily_capital = capital
                    changed.append(f"target_capital={capital}")
                if existing.our_capital != our_capital:
                    existing.our_capital = our_capital
                    changed.append(f"our_capital={our_capital}")
                if changed:
                    logger.info("Updated bot '%s': %s", name, ", ".join(changed))
                else:
                    logger.info("Bot '%s' up to date", name)
            else:
                new_bot = BotRegistry(
                    name=name,
                    target_address=wallet,
                    active=active,
                    paper_mode=paper_mode,
                    poll_interval_sec=poll_interval,
                    target_daily_capital=capital,
                    our_capital=our_capital,
                    initial_capital=our_capital,  # locked forever
                    total_trades=0,
                )
                session.add(new_bot)
                logger.info("Registered new bot: '%s' (wallet …%s, our_capital=$%.0f)",
                            name, wallet[-6:], our_capital)

    logger.info("Bot sync complete.")


def get_risk_config() -> dict[str, Any]:
    """Return risk settings from config.yml."""
    return load_config().get("risk", {})
