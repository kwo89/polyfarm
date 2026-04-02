"""
CEO persistent memory system.

Each conversation ends with the CEO rewriting its memory file from scratch —
condensed, accurate, no stale assumptions. Every rewrite is auto-committed to
Git so you get full version history for free. Revert anytime with git.

Files:
  data/ceo_memory.md        — current CEO memory (injected into every session)
  ceo_conversations table   — full conversation log in SQLite
"""

import json
import os
import subprocess
import uuid
from datetime import datetime
from pathlib import Path

from sqlalchemy import Column, String, Text, DateTime, Integer, select, desc
from sqlalchemy.orm import DeclarativeBase

from core.database import engine, get_session
from core.models import Base

DB_PATH = os.environ.get("DB_PATH", "data/polyfarm.db")
MEMORY_PATH = Path(DB_PATH).parent / "ceo_memory.md"
REPO_ROOT = Path(__file__).parent.parent.parent  # PolyFarm/


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
## Your persistent memory
This is your current knowledge base. It was written by you at the end of the last session.
Treat it as ground truth about the farm state, owner preferences, and open items.

{mem}
---
"""


# ── Git version control ───────────────────────────────────────────────────────

def _git_commit_memory(commit_message: str):
    """
    Commit the memory file to git. Silently no-ops if git is unavailable
    or nothing changed. This gives free version history — revert anytime with:
        git log data/ceo_memory.md
        git checkout <hash> -- data/ceo_memory.md
    """
    try:
        # Configure git identity if not set (needed on fresh servers)
        subprocess.run(
            ["git", "config", "user.email", "ceo@polyfarm.local"],
            cwd=REPO_ROOT, capture_output=True
        )
        subprocess.run(
            ["git", "config", "user.name", "PolyFarm CEO"],
            cwd=REPO_ROOT, capture_output=True
        )
        subprocess.run(
            ["git", "add", str(MEMORY_PATH)],
            cwd=REPO_ROOT, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", commit_message],
            cwd=REPO_ROOT, check=True, capture_output=True
        )
    except subprocess.CalledProcessError:
        pass  # Nothing changed or git unavailable — not a hard failure


# ── Memory update tool ────────────────────────────────────────────────────────

UPDATE_MEMORY_TOOL = {
    "name": "update_memory",
    "description": (
        "Rewrite your persistent memory at the END of every conversation. "
        "You receive your current memory (injected at the top of this session) "
        "and the full conversation that just happened. "
        "Produce a complete, concise rewrite of the memory file — include everything "
        "that's still true and useful, remove anything outdated or superseded. "
        "Do NOT just append. Rewrite the whole thing from scratch. "
        "The previous version is preserved in Git — you can always revert."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "rewritten_memory": {
                "type": "string",
                "description": (
                    "Complete rewrite of ceo_memory.md in markdown. "
                    "Sections to include (only if non-empty): "
                    "## Farm State, ## Active Bots, ## Owner Preferences, "
                    "## Open Action Items, ## Key Decisions, ## Observations. "
                    "Be concise — every line should be useful to future-you. "
                    "Remove anything no longer accurate."
                ),
            },
            "what_changed": {
                "type": "string",
                "description": (
                    "One-line summary of what changed vs previous memory "
                    "(used as the git commit message). "
                    "Example: 'Bot-1 paused, owner prefers morning briefings'"
                ),
            },
        },
        "required": ["rewritten_memory", "what_changed"],
    },
}


def apply_memory_update(inputs: dict) -> str:
    """Write rewritten memory and commit it to git."""
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    rewritten = inputs.get("rewritten_memory", "").strip()
    what_changed = inputs.get("what_changed", "memory update").strip()

    if not rewritten:
        return json.dumps({"error": "rewritten_memory was empty"})

    # Stamp the file with last-updated time
    header = f"_Last updated: {now}_\n\n"
    write_memory(header + rewritten)

    # Commit to git — free version history
    commit_msg = f"CEO memory: {what_changed} [{now}]"
    _git_commit_memory(commit_msg)

    return json.dumps({
        "success": True,
        "memory_updated": now,
        "committed": commit_msg,
    })
