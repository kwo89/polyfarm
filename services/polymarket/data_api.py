"""
Polymarket Data API client (no auth required).

Data API: https://data-api.polymarket.com  — activity, positions, trades
Gamma API: https://gamma-api.polymarket.com — market metadata, resolution info

IMPORTANT: Polymarket wallets are Proxy Wallets (Gnosis Safes), NOT EOA addresses.
Always pass the proxy wallet address (from a user's profile URL), not MetaMask address.

Activity response fields (real):
  proxyWallet, timestamp, conditionId, type, size, usdcSize, transactionHash,
  price, asset, side, outcomeIndex, title, slug, outcome, name, pseudonym
"""

import logging
from typing import Any, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"

# Shared session with retry + connection pooling
_session = requests.Session()
_retry = Retry(
    total=3,
    backoff_factor=1.0,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET"],
)
_session.mount("https://", HTTPAdapter(max_retries=_retry))


def _get(base: str, path: str, params: Optional[dict] = None, timeout: int = 15) -> Any:
    url = f"{base}{path}"
    try:
        resp = _session.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except requests.HTTPError as e:
        logger.error("HTTP error fetching %s: %s", url, e)
        raise
    except requests.RequestException as e:
        logger.error("Request error fetching %s: %s", url, e)
        raise


# ─── ACTIVITY ─────────────────────────────────────────────────────────────────

def get_wallet_activity(
    address: str,
    limit: int = 100,
) -> list[dict]:
    """
    Returns recent on-chain activity for a proxy wallet address.
    Sorted by timestamp DESC (newest first).

    Response fields: proxyWallet, timestamp, conditionId, type, size, usdcSize,
                     transactionHash, price, asset, side, outcomeIndex, title, outcome
    type values: TRADE, SPLIT, MERGE, REDEEM, REWARD, CONVERSION
    """
    data = _get(
        DATA_API,
        "/activity",
        params={"user": address, "limit": limit, "sortDirection": "DESC"},
    )
    if isinstance(data, list):
        return data
    return data.get("data", [])


def get_wallet_activity_since(address: str, since_timestamp: int) -> list[dict]:
    """Fetch activity newer than a given Unix timestamp (stops at boundary)."""
    results = []
    batch = get_wallet_activity(address, limit=100)
    for item in batch:
        ts = item.get("timestamp", 0)
        if isinstance(ts, str):
            ts = int(ts)
        if ts <= since_timestamp:
            break
        results.append(item)
    return results


# ─── POSITIONS ────────────────────────────────────────────────────────────────

def get_wallet_positions(address: str, limit: int = 500) -> list[dict]:
    """
    Returns current open positions for a proxy wallet.
    Fields: proxyWallet, asset, conditionId, size, avgPrice, initialValue,
            currentValue, cashPnl, percentPnl, title, outcome, etc.
    """
    data = _get(DATA_API, "/positions", params={"user": address, "limit": limit})
    if isinstance(data, list):
        return data
    return data.get("data", [])


# ─── MARKETS ─────────────────────────────────────────────────────────────────

def get_market_clob(condition_id: str) -> dict:
    """
    Fetch market data from the CLOB API by conditionId (most reliable).
    Returns token prices — a price of 1.0 means that token/outcome won.
    Works correctly for BTC 5-min markets and all other market types.

    Response includes:
      condition_id, question, closed, tokens (list of {token_id, outcome, price})
    """
    data = _get(CLOB_API, f"/markets/{condition_id}")
    if isinstance(data, list):
        return data[0] if data else {}
    return data or {}


def get_market(condition_id: str) -> dict:
    """
    Fetch market metadata. Tries CLOB API first (reliable for all markets),
    falls back to Gamma API for additional metadata if needed.
    """
    try:
        clob_data = get_market_clob(condition_id)
        if clob_data:
            return clob_data
    except Exception as e:
        logger.debug("CLOB API failed for %s: %s — trying Gamma", condition_id[:12], e)
    # Fallback to Gamma (may not work for all market types)
    data = _get(GAMMA_API, "/markets", params={"conditionId": condition_id})
    if isinstance(data, list):
        return data[0] if data else {}
    return data or {}


def get_markets(limit: int = 100, offset: int = 0, active: bool = True) -> list[dict]:
    """List active markets with metadata."""
    params = {"limit": limit, "offset": offset}
    if active:
        params["active"] = "true"
    data = _get(GAMMA_API, "/markets", params=params)
    if isinstance(data, list):
        return data
    return data.get("data", [])


def get_resolved_markets(limit: int = 200, offset: int = 0) -> list[dict]:
    """Fetch recently resolved markets — used by Quant Analyst for P&L calc."""
    data = _get(GAMMA_API, "/markets", params={
        "limit": limit, "offset": offset, "closed": "true"
    })
    if isinstance(data, list):
        return data
    return data.get("data", [])


# ─── LEADERBOARD (via Gamma API) ──────────────────────────────────────────────

def get_leaderboard(limit: int = 50, window: str = "1w") -> list[dict]:
    """
    Fetch top performing wallets from Gamma API.
    window: '1d' | '1w' | '1m' | 'all'
    """
    data = _get(GAMMA_API, "/leaderboard", params={"limit": limit, "window": window})
    if isinstance(data, list):
        return data
    return data.get("data", [])
