"""
CEO persistent memory system.

Stores CEO memory in two places:
  - data/ceo_memory.md  — human-readable, injected into every session
  - ceo_conversations   — full conversation log in SQLite (for history)

After each conversation the CEO writes a structured update to ceo_memory.md.
Every new session loads this file and injects it into the system prompt.
"""

import json
import os
import uuid
from datetime import datetime
from pathlib import Path

from sqlalchemy import Column, String, Text, DateTime, Integer, select, desc
from sqlalchemy.orm import DeclarativeBase

from core.database import engine, get_session, SessionLocal
from core.models import Base

DB_PATH = os.environ.get("DB_PATH", "data/polyfarm.db")
MEMORY_PATH = Path(DB_PATH).parent / "ceo_memory.md"


# ── Conversation log table ────────────────────────────────────────────────────

class CeoConversation(Base):
    """Persists full conversation history across sessions."""
    __tablename__ = "ceo_conversations"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    role = Column(String, nullable=False)       # user | assistant
    content = Column(Text, nullable=False)
    turn_index = Column(Integer, default=0)


def init_memory_tables():
    """Create ceo_conversations table if it doesn't exist."""
    Base.metadata.create_all(engine)


# ── Session management ────────────────────────────────────────────────────────

def new_session_id() -> str:
    return str(uuid.uuid4())


def save_turn(session_id: str, role: str, content: str, turn_index: int):
    with get_session() as session:
        session.add(CeoConversation(
            session_id=session_id,
            role=role,
            content=content,
            turn_index=turn_index,
        ))


def load_session_history(session_id: str) -> list[dict]:
    """Load all messages for a given session."""
    with get_session() as session:
        rows = session.execute(
            select(CeoConversation)
            .where(CeoConversation.session_id == session_id)
            .order_by(CeoConversation.turn_index)
        ).scalars().all()
        return [{"role": r.role, "content": r.content} for r in rows]


def load_recent_conversations(limit: int = 5) -> list[dict]:
    """Load the last N sessions for context (summary format)."""
    with get_session() as session:
        rows = session.execute(
            select(CeoConversation)
            .order_by(desc(CeoConversation.created_at))
            .limit(limit * 10)
        ).scalars().all()
        # Group by session
        sessions: dict[str, list] = {}
        for r in rows:
            sessions.setdefault(r.session_id, []).append(r)
        recent = []
        for sid, msgs in list(sessions.items())[:limit]:
            first = min(msgs, key=lambda m: m.turn_index)
            recent.append({
                "session_id": sid,
                "started_at": first.created_at.strftime("%Y-%m-%d %H:%M UTC"),
                "message_count": len(msgs),
                "first_user_message": next(
                    (m.content[:120] for m in sorted(msgs, key=lambda x: x.turn_index)
                     if m.role == "user"), ""
                ),
            })
        return recent


# ── Memory file ───────────────────────────────────────────────────────────────

def read_memory() -> str:
    """Read current CEO memory. Returns empty string if not initialised yet."""
    if MEMORY_PATH.exists():
        return MEMORY_PATH.read_text(encoding="utf-8")
    return ""


def write_memory(content: str):
    """Overwrite CEO memory file."""
    MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    MEMORY_PATH.write_text(content, encoding="utf-8")


def get_memory_prompt() -> str:
    """Return memory block to inject into CEO system prompt."""
    mem = read_memory()
    if not mem.strip():
        return ""
    return f"""
---
## Your persistent memory (updated after each conversation)

{mem}
---
"""


# ── Memory update tool ────────────────────────────────────────────────────────

UPDATE_MEMORY_TOOL = {
    "name": "update_memory",
    "description": (
        "Update your persistent memory after a conversation. "
        "Call this at the END of every conversation to record key decisions, "
        "observations, user preferences, and action items. "
        "Your memory is injected at the start of every future session — "
        "write only what's useful for future-you to know."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "key_decisions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Important decisions made in this conversation (dated)",
            },
            "observations": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Performance observations, anomalies, patterns noticed",
            },
            "user_preferences": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Things the user prefers, dislikes, or has strong opinions about",
            },
            "action_items": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Open tasks or follow-ups (include dates where relevant)",
            },
            "farm_state": {
                "type": "string",
                "description": "One-paragraph summary of current farm state and context",
            },
        },
        "required": ["farm_state"],
    },
}


def apply_memory_update(inputs: dict) -> str:
    """Write a structured memory update to ceo_memory.md."""
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    # Load existing memory to preserve history
    existing = read_memory()

    lines = [f"# PolyFarm CEO Memory\n_Last updated: {now}_\n"]

    lines.append("## Farm State\n" + inputs.get("farm_state", "").strip())

    if inputs.get("action_items"):
        lines.append("\n## Open Action Items")
        for item in inputs["action_items"]:
            lines.append(f"- {item}")

    if inputs.get("user_preferences"):
        lines.append("\n## Owner Preferences")
        for pref in inputs["user_preferences"]:
            lines.append(f"- {pref}")

    if inputs.get("observations"):
        lines.append("\n## Performance Observations")
        for obs in inputs["observations"]:
            lines.append(f"- {obs}")

    if inputs.get("key_decisions"):
        lines.append("\n## Key Decisions")
        for dec in inputs["key_decisions"]:
            lines.append(f"- {dec}")

    # Append previous decision history (keep last 20 entries to avoid bloat)
    if "## Key Decisions" in existing:
        old_decisions = existing.split("## Key Decisions")[-1].strip().split("\n")
        old_entries = [l for l in old_decisions if l.startswith("- ")][:20]
        new_decisions = inputs.get("key_decisions", [])
        # Merge: new first, then old (deduped)
        all_decisions = new_decisions + [
            e[2:] for e in old_entries if e[2:] not in new_decisions
        ]
        if all_decisions:
            # Replace the decisions section
            idx = lines.index("\n## Key Decisions") if "\n## Key Decisions" in lines else -1
            if idx >= 0:
                lines = lines[:idx + 1] + [f"- {d}" for d in all_decisions[:25]]

    write_memory("\n".join(lines))
    return json.dumps({"success": True, "memory_updated": now})
