#!/usr/bin/env python3
"""
Initialize the PolyFarm database.
Creates all tables and seeds default system_config values.

Usage:
    python scripts/setup_db.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.database import init_db, get_session
from core.models import SystemConfig


DEFAULTS = {
    "trading_mode": "paper",
    "emergency_stop": "0",
}


def main():
    print("Initializing PolyFarm database...")
    init_db()
    print("  ✓ Tables created")

    with get_session() as session:
        for key, value in DEFAULTS.items():
            existing = session.get(SystemConfig, key)
            if not existing:
                session.add(SystemConfig(key=key, value=value))
                print(f"  ✓ Config: {key} = {value}")
            else:
                print(f"  – Config: {key} already set to '{existing.value}' (skipped)")

    print("\nDatabase ready at:", os.path.abspath("data/polyfarm.db"))


if __name__ == "__main__":
    main()
