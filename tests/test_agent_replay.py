from __future__ import annotations

from sqlalchemy import select

from app.db import SessionLocal
from app.models import AgentReplayRequest, AgentRun, ApprovalRequest, AuditLog, Task


def _source_run(*, status: str = "succeeded", action_kind: str = "financial") -> int:
    with SessionLocal() as db:
        run = AgentRun(
            agent_type="finance",
            status=status,
            input={
                "action": "read_only_summary",
                "action_kind": action_kind,
                "approval_id": 999999,
                "nested": {
                    "safe": "kept",
                    "api_token": "must-not-be-copied",
                    "password": "must-not-be-copied",
                },
            },
            output={"status": "historical"},
        )
        db.add(run)
        db.commit()
        return run.id


def test_agent_replay_requires_fresh_bound_owner_approval_and_is_idempotent(client):
    run_id = _source_run()
    headers = {"X-Role": "manager", "Idempotency-Key": "replay-request-0001"}

    response = client.post(f"/api/agent-runs/{run_id}/replay", headers=headers)
    assert response.status_code == 201
    result = response.json()
    assert result["status"] == "owner_approval_required"
    assert result["task_status"] == "blocked"
    assert result["old_approval_reused"] is False
    assert result["automatic_execution"] is False
    assert result["approval_id"] != 999999

    duplicate = client.post(f"/api/agent-runs/{run_id}/replay", headers=headers)
    assert duplicate.status_code == 201
    assert duplicate.json()["idempotent_replay"] is True
    assert duplicate.json()["task_id"] == result["task_id"]
    assert duplicate.json()["approval_id"] == result["approval_id"]

    with SessionLocal() as db:
        replay = db.scalar(
            select(AgentReplayRequest).where(
                AgentReplayRequest.source_run_id == run_id
            )
        )
        assert replay is not None
        task = db.get(Task, result["task_id"])
        approval = db.get(ApprovalRequest, result["approval_id"])
        assert task.status == "blocked"
        assert task.max_attempts == 1
        assert task.payload["action_kind"] == "agent_replay"
        assert task.payload["nested"] == {"safe": "kept"}
        assert task.payload["approval_id"] == result["approval_id"]
        assert task.payload["approval_id"] != 999999
        assert task.payload["replay"]["source_run_id"] == run_id
        assert task.payload["replay"]["original_action_kind"] == "financial"
        assert approval.action_kind == "agent_replay"
        assert approval.resource_type == "task"
        assert approval.resource_id == str(task.id)
        assert approval.status == "pending"
        assert db.scalar(
            select(AuditLog).where(
                AuditLog.action == "agent.replay_requested",
                AuditLog.resource_id == str(run_id),
            )
        ) is not None


def test_agent_replay_approval_only_queues_new_task(client):
    run_id = _source_run()
    requested = client.post(
        f"/api/agent-runs/{run_id}/replay",
        headers={"X-Role": "manager", "Idempotency-Key": "replay-request-0002"},
    ).json()

    approved = client.post(
        f"/api/approvals/{requested['approval_id']}/approve",
        headers={"X-Role": "owner"},
        json={"note": "Approve this exact replay"},
    )
    assert approved.status_code == 200
    assert approved.json()["task_status"] == "queued"
    assert approved.json()["execution"] == "not_executed"

    duplicate = client.post(
        f"/api/agent-runs/{run_id}/replay",
        headers={"X-Role": "manager", "Idempotency-Key": "replay-request-0002"},
    )
    assert duplicate.status_code == 201
    assert duplicate.json()["status"] == "approved"
    assert duplicate.json()["task_status"] == "queued"
    assert duplicate.json()["idempotent_replay"] is True

    with SessionLocal() as db:
        task = db.get(Task, requested["task_id"])
        assert task.status == "queued"
        assert task.payload["approval_id"] == requested["approval_id"]
        assert db.scalar(
            select(AgentRun).where(AgentRun.task_id == task.id)
        ) is None


def test_agent_replay_rejects_nonterminal_recursive_and_cross_run_key_reuse(client):
    running_id = _source_run(status="running")
    recursive_id = _source_run(action_kind="agent_replay")
    first_id = _source_run()
    second_id = _source_run()
    headers = {"X-Role": "manager", "Idempotency-Key": "replay-shared-key"}

    assert client.post(
        f"/api/agent-runs/{running_id}/replay", headers=headers
    ).status_code == 409
    assert client.post(
        f"/api/agent-runs/{recursive_id}/replay",
        headers={"X-Role": "manager", "Idempotency-Key": "recursive-replay-key"},
    ).status_code == 409
    assert client.post(f"/api/agent-runs/{first_id}/replay", headers=headers).status_code == 201
    reused = client.post(f"/api/agent-runs/{second_id}/replay", headers=headers)
    assert reused.status_code == 409


def test_agent_replay_requires_manager_and_valid_idempotency_key(client):
    run_id = _source_run()
    denied = client.post(
        f"/api/agent-runs/{run_id}/replay",
        headers={"X-Role": "viewer", "Idempotency-Key": "replay-denied-key"},
    )
    assert denied.status_code == 403

    invalid = client.post(
        f"/api/agent-runs/{run_id}/replay",
        headers={"X-Role": "manager", "Idempotency-Key": "short"},
    )
    assert invalid.status_code == 422
    missing = client.post(
        "/api/agent-runs/999999999/replay",
        headers={"X-Role": "manager", "Idempotency-Key": "replay-missing-key"},
    )
    assert missing.status_code == 404
