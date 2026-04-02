"""Lightweight task queue backed by agent_tasks table.

CEO dispatches tasks here; subagents poll for their tasks.
No Redis, no RabbitMQ — SQLite WAL handles the throughput.
"""

import json
import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import select
from core.database import get_session
from core.models import AgentTask


def enqueue(
    assigned_to: str,
    task_type: str,
    payload: dict,
    dispatched_by: str = "system",
    priority: int = 5,
) -> str:
    """Add a task to the queue. Returns the task ID."""
    task_id = str(uuid.uuid4())
    with get_session() as session:
        task = AgentTask(
            id=task_id,
            dispatched_by=dispatched_by,
            assigned_to=assigned_to,
            task_type=task_type,
            payload=json.dumps(payload),
            priority=priority,
        )
        session.add(task)
    return task_id


def dequeue(agent_name: str) -> Optional[AgentTask]:
    """Claim the next pending task for this agent (highest priority first)."""
    with get_session() as session:
        task = session.execute(
            select(AgentTask)
            .where(AgentTask.assigned_to == agent_name)
            .where(AgentTask.status == "pending")
            .order_by(AgentTask.priority, AgentTask.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        ).scalar_one_or_none()

        if task:
            task.status = "running"
            task.started_at = datetime.utcnow()
            session.flush()
            session.expunge(task)
            session.commit()
            # Re-fetch outside the lock to return a clean object
            return session.get(AgentTask, task.id)
    return None


def ack(task_id: str, result: dict) -> None:
    """Mark a task as done with a result payload."""
    with get_session() as session:
        task = session.get(AgentTask, task_id)
        if task:
            task.status = "done"
            task.result = json.dumps(result)
            task.completed_at = datetime.utcnow()


def fail(task_id: str, error: str) -> None:
    """Mark a task as failed."""
    with get_session() as session:
        task = session.get(AgentTask, task_id)
        if task:
            task.status = "failed"
            task.error = error
            task.completed_at = datetime.utcnow()


def get_result(task_id: str) -> Optional[dict]:
    """Return the result of a completed task, or None if not done yet."""
    with get_session() as session:
        task = session.get(AgentTask, task_id)
        if task and task.status == "done" and task.result:
            return json.loads(task.result)
    return None


def get_task_status(task_id: str) -> Optional[str]:
    with get_session() as session:
        task = session.get(AgentTask, task_id)
        return task.status if task else None
