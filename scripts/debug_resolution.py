#!/usr/bin/env python3
"""
Debug script — checks why trades are stuck on Pending.
Run on the server: python scripts/debug_resolution.py

Prints:
  1. Sample market IDs from unresolved paper trades
  2. Raw Gamma API response for each
  3. What our resolver extracts from it
"""
import sys, os, json, requests
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select
from core.database import init_db, get_session
from core.models import PaperTrade

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"

def raw_clob(condition_id: str) -> dict:
    r = requests.get(f"{CLOB_API}/markets/{condition_id}", timeout=15)
    r.raise_for_status()
    return r.json()

def raw_gamma(condition_id: str) -> dict:
    r = requests.get(f"{GAMMA_API}/markets", params={"conditionId": condition_id}, timeout=15)
    r.raise_for_status()
    data = r.json()
    return data[0] if isinstance(data, list) and data else data

init_db()

with get_session() as session:
    trades = session.execute(
        select(PaperTrade).where(PaperTrade.market_resolved == False).limit(5)
    ).scalars().all()
    samples = [(t.market_id, t.outcome, t.question) for t in trades]

if not samples:
    print("No unresolved trades found in DB.")
    sys.exit(0)

print(f"Found {len(samples)} unresolved trades. Checking Gamma API...\n")

seen = set()
for market_id, outcome, question in samples:
    if market_id in seen:
        continue
    seen.add(market_id)

    print(f"{'─'*60}")
    print(f"Market ID : {market_id}")
    print(f"Question  : {question}")
    print(f"Our bet   : {outcome}")

    print(f"\n--- CLOB API (clob.polymarket.com) ---")
    try:
        clob = raw_clob(market_id)
        key_fields = {k: clob.get(k) for k in [
            "condition_id", "question", "closed", "active",
            "tokens", "enable_order_book"
        ] if k in clob}
        print(json.dumps(key_fields, indent=2))
    except Exception as e:
        print(f"CLOB ERROR: {e}")

    print(f"\n--- Gamma API (gamma-api.polymarket.com) ---")
    try:
        gamma = raw_gamma(market_id)
        key_fields = {k: gamma.get(k) for k in [
            "conditionId", "question", "closed", "resolved",
            "winnerOutcome", "tokens", "outcomePrices", "outcomes", "endDate"
        ] if k in gamma}
        print(json.dumps(key_fields, indent=2))
    except Exception as e:
        print(f"GAMMA ERROR: {e}")
    print()
