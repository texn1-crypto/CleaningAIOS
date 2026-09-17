from __future__ import annotations

import hashlib
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from .agents import AGENTS
from .models import AgentReplayRequest, AgentRun, ApprovalRequest, Task
from .orchestrator import audit, dispatch
from .task_state import record_task_created


TERMINAL_RUN_STATUSES = {"succeeded", "failed", "incomplete"}
SENSITIVE_KEY_PARTS = (
    "access_key",
    "api_key",
    "authorization",
    "bank_card",
    "callback_token",
    "cvv",
    "password",
    "private_key",
    "secret",
    "token",
)
STALE_APPROVAL_KEYS = {"approval_id", "approval_token", "decision_version"}


class ReplayError(RuntimeError):
    pass


class ReplayNotFound(ReplayError):
    pass


class ReplayConflict(ReplayError):
    pass


def _clean_for_replay(value: Any) -> Any:
    """Remove credentials and approval artifacts recursively from stored input."""

    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).lower()
            if normalized in STALE_APPROVAL_KEYS or any(
                marker in normalized for marker in SENSITIVE_KEY_PARTS
            ):
                continue
            clean[str(key)] = _clean_for_replay(item)
        return clean
    if isinstance(value, list):
        return [_clean_for_replay(item) for item in value]
    return value


def _request_hash(requested_by: str, idempotency_key: str) -> str:
    return hashlib.sha256(
        f"{requested_by}\0{idempotency_key}".encode("utf-8")
    ).hexdigest()


def _result(
    db: Session,
    row: AgentReplayRequest,
    *,
    idempotent: bool,
) -> dict[str, Any]:
    task = db.get(Task, row.replay_task_id)
    approval = db.get(ApprovalRequest, row.approval_id)
    approval_status = approval.status if approval else "missing"
    return {
        "replay_request_id": row.id,
        "source_run_id": row.source_run_id,
        "task_id": row.replay_task_id,
        "approval_id": row.approval_id,
        "status": (
            "owner_approval_required" if approval_status == "pending" else approval_status
        ),
        "task_status": task.status if task else "missing",
        "idempotent_replay": idempotent,
        "old_approval_reused": False,
        "automatic_execution": False,
    }


def request_agent_replay(
    db: Session,
    *,
    run_id: int,
    idempotency_key: str,
    requested_by: str,
) -> dict[str, Any]:
    if not 8 <= len(idempotency_key) <= 128:
        raise ReplayError("Idempotency-Key must contain 8 to 128 characters")
    request_hash = _request_hash(requested_by, idempotency_key)
    existing = db.scalar(
        select(AgentReplayRequest).where(
            AgentReplayRequest.request_hash == request_hash
        )
    )
    if existing:
        if existing.source_run_id != run_id:
            raise ReplayConflict("Idempotency-Key was already used for another run")
        return _result(db, existing, idempotent=True)

    source_run = db.get(AgentRun, run_id)
    if source_run is None:
        raise ReplayNotFound("Agent run not found")
    if source_run.status not in TERMINAL_RUN_STATUSES:
        raise ReplayConflict("Only a terminal agent run can be replayed")
    if source_run.agent_type not in AGENTS:
        raise ReplayConflict("The source agent is no longer registered")

    source_input = _clean_for_replay(source_run.input or {})
    if not isinstance(source_input, dict):
        raise ReplayConflict("Stored agent input is not a JSON object")
    if source_input.get("action_kind") == "agent_replay":
        raise ReplayConflict("A replay task cannot be replayed recursively")
    original_action_kind = str(source_input.get("action_kind") or "")
    source_input["action_kind"] = "agent_replay"
    source_input["source"] = "agent_replay"
    source_input["correlation_id"] = uuid4().hex
    source_input["replay"] = {
        "source_run_id": source_run.id,
        "source_task_id": source_run.task_id,
        "original_action_kind": original_action_kind,
        "fresh_owner_approval_required": True,
    }

    source_task = db.get(Task, source_run.task_id) if source_run.task_id else None
    task = Task(
        title=f"Replay agent run #{source_run.id}",
        description="Owner-approved reproduction of a historical agent run",
        status="open",
        priority="normal",
        agent_type=source_run.agent_type,
        payload=source_input,
        max_attempts=1,
        timeout_seconds=source_task.timeout_seconds if source_task else 120,
    )
    db.add(task)
    db.flush()
    record_task_created(
        db,
        task,
        actor=requested_by,
        reason="agent_replay_requested",
        correlation_id=str(source_input["correlation_id"]),
    )
    dispatch_result = dispatch(db, task)
    approval_id = dispatch_result.get("approval_id")
    if task.status != "blocked" or not isinstance(approval_id, int):
        raise ReplayConflict("Replay safety gate did not create a fresh approval")

    row = AgentReplayRequest(
        source_run_id=source_run.id,
        replay_task_id=task.id,
        approval_id=approval_id,
        request_hash=request_hash,
        requested_by=requested_by,
    )
    db.add(row)
    db.flush()
    audit(
        db,
        requested_by,
        "agent.replay_requested",
        "agent_run",
        str(source_run.id),
        {
            "replay_request_id": row.id,
            "replay_task_id": task.id,
            "approval_id": approval_id,
            "old_approval_reused": False,
        },
    )
    return _result(db, row, idempotent=False)
