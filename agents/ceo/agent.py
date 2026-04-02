"""
CEO Agent — Claude-backed conversational interface for PolyFarm.

Called by the dashboard chat endpoint. Stateless per-call (history passed in).
Tools query the live SQLite database directly — no LLM for data, only for reasoning.
"""

import json
import os
from datetime import datetime, timedelta
from typing import Optional

import anthropic
from sqlalchemy import select, func, desc

from core.database import get_session
from core.models import (
    BotRegistry, PaperTrade, TargetTrade, DailyPnl,
    SystemConfig, Alert,
)
from agents.ceo.system_prompt import SYSTEM_PROMPT

MODEL = "claude-sonnet-4-5"

# ── Tool definitions ──────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "get_status",
        "description": "Get current system status: active bots, trading mode, emergency stop, recent alerts.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_paper_trades",
        "description": "Get recent paper trades with details (side, outcome, size, price, market, P&L if resolved).",
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "Look-back window in days (default 7)"},
                "limit": {"type": "integer", "description": "Max trades to return (default 50)"},
            },
            "required": [],
        },
    },
    {
        "name": "get_performance_summary",
        "description": "Get aggregated performance stats: total trades, volume, P&L, win rate, daily breakdown.",
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "Look-back window in days (default 7)"},
            },
            "required": [],
        },
    },
    {
        "name": "get_skipped_trades",
        "description": "Get trades the bot detected but skipped, with skip reasons. Useful for understanding what's being filtered.",
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "Look-back window in days (default 1)"},
                "limit": {"type": "integer", "description": "Max entries (default 20)"},
            },
            "required": [],
        },
    },
    {
        "name": "pause_bot",
        "description": "Pause a bot so it stops copying trades. It stays registered but won't execute.",
        "input_schema": {
            "type": "object",
            "properties": {
                "bot_name": {"type": "string", "description": "Bot name (e.g. 'Bot-1')"},
            },
            "required": ["bot_name"],
        },
    },
    {
        "name": "unpause_bot",
        "description": "Resume a paused bot.",
        "input_schema": {
            "type": "object",
            "properties": {
                "bot_name": {"type": "string", "description": "Bot name (e.g. 'Bot-1')"},
            },
            "required": ["bot_name"],
        },
    },
    {
        "name": "set_emergency_stop",
        "description": "Enable or disable the emergency stop. When enabled, ALL bots halt immediately.",
        "input_schema": {
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean", "description": "True to stop all trading, False to resume"},
            },
            "required": ["enabled"],
        },
    },
]


# ── Tool implementations ──────────────────────────────────────────────────────

def _run_tool(name: str, inputs: dict) -> str:
    try:
        if name == "get_status":
            return _tool_get_status()
        elif name == "get_paper_trades":
            return _tool_get_paper_trades(inputs.get("days", 7), inputs.get("limit", 50))
        elif name == "get_performance_summary":
            return _tool_get_performance_summary(inputs.get("days", 7))
        elif name == "get_skipped_trades":
            return _tool_get_skipped_trades(inputs.get("days", 1), inputs.get("limit", 20))
        elif name == "pause_bot":
            return _tool_set_bot_paused(inputs["bot_name"], True)
        elif name == "unpause_bot":
            return _tool_set_bot_paused(inputs["bot_name"], False)
        elif name == "set_emergency_stop":
            return _tool_set_emergency_stop(inputs["enabled"])
        else:
            return json.dumps({"error": f"Unknown tool: {name}"})
    except Exception as e:
        return json.dumps({"error": str(e)})


def _tool_get_status() -> str:
    with get_session() as session:
        bots = session.execute(select(BotRegistry)).scalars().all()
        mode = session.get(SystemConfig, "trading_mode")
        estop = session.get(SystemConfig, "emergency_stop")
        alerts = session.execute(
            select(Alert).where(Alert.acknowledged == False)
            .order_by(desc(Alert.created_at)).limit(5)
        ).scalars().all()

        return json.dumps({
            "trading_mode": mode.value if mode else "paper",
            "emergency_stop": estop.value == "1" if estop else False,
            "bots": [
                {
                    "name": b.name,
                    "target": b.target_address,
                    "active": b.active,
                    "paused": b.paused,
                    "paper_mode": b.paper_mode,
                    "total_trades": b.total_trades,
                    "last_activity": b.last_activity_at.isoformat() if b.last_activity_at else None,
                    "target_capital": b.target_daily_capital,
                }
                for b in bots
            ],
            "unacknowledged_alerts": [
                {"severity": a.severity, "source": a.source, "message": a.message,
                 "created_at": a.created_at.isoformat()}
                for a in alerts
            ],
        })


def _tool_get_paper_trades(days: int, limit: int) -> str:
    since = (datetime.utcnow() - timedelta(days=days)).isoformat()
    with get_session() as session:
        trades = session.execute(
            select(PaperTrade)
            .where(PaperTrade.created_at >= since)
            .order_by(desc(PaperTrade.created_at))
            .limit(limit)
        ).scalars().all()

        bot_names = {b.id: b.name for b in session.execute(select(BotRegistry)).scalars().all()}

        return json.dumps([
            {
                "time": t.created_at.strftime("%Y-%m-%d %H:%M UTC") if t.created_at else None,
                "bot": bot_names.get(t.bot_id, "?"),
                "side": t.side,
                "outcome": t.outcome,
                "size_usd": round(t.hypothetical_size or 0, 2),
                "price": round(t.hypothetical_price or 0, 3),
                "market": t.question or t.market_id,
                "resolved": t.market_resolved,
                "pnl": round(t.hypothetical_pnl or 0, 2) if t.market_resolved else None,
            }
            for t in trades
        ])


def _tool_get_performance_summary(days: int) -> str:
    since = (datetime.utcnow() - timedelta(days=days)).isoformat()
    since_date = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")

    with get_session() as session:
        total_detected = session.execute(
            select(func.count(TargetTrade.id)).where(TargetTrade.detected_at >= since)
        ).scalar_one() or 0

        total_paper = session.execute(
            select(func.count(PaperTrade.id)).where(PaperTrade.created_at >= since)
        ).scalar_one() or 0

        total_skipped = session.execute(
            select(func.count(TargetTrade.id))
            .where(TargetTrade.status == "skipped")
            .where(TargetTrade.detected_at >= since)
        ).scalar_one() or 0

        total_volume = session.execute(
            select(func.sum(PaperTrade.hypothetical_size))
            .where(PaperTrade.created_at >= since)
        ).scalar_one() or 0

        resolved = session.execute(
            select(PaperTrade)
            .where(PaperTrade.market_resolved == True)
            .where(PaperTrade.created_at >= since)
        ).scalars().all()

        wins = [t for t in resolved if (t.hypothetical_pnl or 0) > 0]
        losses = [t for t in resolved if (t.hypothetical_pnl or 0) < 0]
        total_pnl = sum(t.hypothetical_pnl or 0 for t in resolved)

        daily = session.execute(
            select(DailyPnl)
            .where(DailyPnl.date >= since_date)
            .order_by(desc(DailyPnl.date))
        ).scalars().all()

        bot_names = {b.id: b.name for b in session.execute(select(BotRegistry)).scalars().all()}

        return json.dumps({
            "period_days": days,
            "trades_detected": total_detected,
            "trades_paper_executed": total_paper,
            "trades_skipped": total_skipped,
            "skip_rate_pct": round(total_skipped / total_detected * 100, 1) if total_detected else 0,
            "total_volume_usd": round(total_volume, 2),
            "resolved_trades": len(resolved),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": round(len(wins) / len(resolved) * 100, 1) if resolved else None,
            "total_pnl_usd": round(total_pnl, 2),
            "daily_breakdown": [
                {
                    "date": r.date,
                    "bot": bot_names.get(r.bot_id, "?"),
                    "trades": r.num_trades,
                    "volume_usd": round(r.total_traded_usd or 0, 2),
                    "realized_pnl": round(r.realized_pnl or 0, 2),
                }
                for r in daily
            ],
        })


def _tool_get_skipped_trades(days: int, limit: int) -> str:
    since = (datetime.utcnow() - timedelta(days=days)).isoformat()
    with get_session() as session:
        trades = session.execute(
            select(TargetTrade)
            .where(TargetTrade.status == "skipped")
            .where(TargetTrade.detected_at >= since)
            .order_by(desc(TargetTrade.detected_at))
            .limit(limit)
        ).scalars().all()

        bot_names = {b.id: b.name for b in session.execute(select(BotRegistry)).scalars().all()}

        return json.dumps([
            {
                "time": t.detected_at.strftime("%Y-%m-%d %H:%M UTC") if t.detected_at else None,
                "bot": bot_names.get(t.bot_id, "?"),
                "side": t.side,
                "outcome": t.outcome,
                "target_size_usd": round(t.target_size or 0, 2),
                "scaled_size_usd": round(t.scaled_size or 0, 2),
                "skip_reason": t.skip_reason,
                "market": t.question or t.market_id,
            }
            for t in trades
        ])


def _tool_set_bot_paused(bot_name: str, paused: bool) -> str:
    with get_session() as session:
        bot = session.execute(
            select(BotRegistry).where(BotRegistry.name == bot_name)
        ).scalar_one_or_none()
        if not bot:
            return json.dumps({"error": f"Bot '{bot_name}' not found"})
        bot.paused = paused
        action = "paused" if paused else "resumed"
        return json.dumps({"success": True, "message": f"Bot '{bot_name}' {action}."})


def _tool_set_emergency_stop(enabled: bool) -> str:
    with get_session() as session:
        row = session.get(SystemConfig, "emergency_stop")
        if row:
            row.value = "1" if enabled else "0"
        else:
            session.add(SystemConfig(key="emergency_stop", value="1" if enabled else "0"))
        state = "ENABLED — all trading halted" if enabled else "DISABLED — trading resumed"
        return json.dumps({"success": True, "emergency_stop": state})


# ── Main chat function ────────────────────────────────────────────────────────

def chat(messages: list[dict], api_key: str) -> str:
    """
    Run one CEO agent turn.
    messages: list of {"role": "user"|"assistant", "content": "..."}
    Returns the assistant's text reply.
    """
    client = anthropic.Anthropic(api_key=api_key)

    history = list(messages)  # copy

    while True:
        response = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=history,
        )

        # Collect text from response
        history.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            # Extract plain text
            text_parts = [b.text for b in response.content if hasattr(b, "text")]
            return " ".join(text_parts).strip()

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = _run_tool(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            history.append({"role": "user", "content": tool_results})
            continue

        # Unexpected stop reason
        break

    return "Something went wrong. Please try again."
