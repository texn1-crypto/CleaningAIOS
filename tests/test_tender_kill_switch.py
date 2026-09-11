from sqlalchemy import func, select

from app.capability_flags import (
    GLOBAL_EXTERNAL_ACTIONS_CONTROL,
    ensure_capability_flags,
    tender_external_actions_control_key,
)
from app.db import SessionLocal
from app.models import (
    ApprovalRequest,
    AuditLog,
    CapabilityFlag,
    DomainEvent,
    SafetyControl,
    Task,
)


MANAGER = {"X-Role": "manager"}
VIEWER = {"X-Role": "viewer"}


def _create_tender(client, suffix: str) -> int:
    response = client.post(
        "/api/records",
        headers=MANAGER,
        json={
            "record_type": "tender",
            "title": f"Per-tender stop test {suffix}",
            "external_id": f"per-tender-stop-{suffix}",
            "data": {},
        },
    )
    assert response.status_code == 201
    return response.json()["id"]


def _protected_task(
    client,
    tender_id: int,
    suffix: str,
    *,
    action_kind: str = "tender_submission",
) -> int:
    response = client.post(
        "/api/tasks",
        json={
            "title": f"Protected tender action {suffix}",
            "agent_type": "tender",
            "payload": {
                "action_kind": action_kind,
                "record_id": tender_id,
            },
        },
    )
    assert response.status_code == 201
    return response.json()["id"]


def test_per_tender_kill_switch_is_isolated_audited_and_precedes_approval(client):
    with SessionLocal() as db:
        db.query(SafetyControl).delete()
        ensure_capability_flags(db)
        flag = db.get(CapabilityFlag, "tender_submission")
        assert flag is not None
        flag.enabled = True
        db.commit()

    stopped_tender_id = _create_tender(client, "stopped")
    other_tender_id = _create_tender(client, "other")
    control_key = tender_external_actions_control_key(stopped_tender_id)

    default = client.get(
        f"/api/tenders/{stopped_tender_id}/kill-switch",
        headers=MANAGER,
    )
    assert default.status_code == 200
    assert default.json() == {
        "tender_id": stopped_tender_id,
        "key": control_key,
        "active": False,
        "reason": "",
        "version": 0,
        "updated_by": "",
        "updated_at": None,
    }
    assert client.get(
        f"/api/tenders/{stopped_tender_id}/kill-switch",
        headers=VIEWER,
    ).status_code == 403
    assert client.put(
        f"/api/tenders/{stopped_tender_id}/kill-switch",
        headers=MANAGER,
        json={"active": True, "reason": "Tender evidence conflict"},
    ).status_code == 403
    assert client.put(
        f"/api/tenders/{stopped_tender_id}/kill-switch",
        json={"active": True, "reason": ""},
    ).status_code == 422
    assert client.get(
        "/api/tenders/999999999/kill-switch",
        headers=MANAGER,
    ).status_code == 404

    activated = client.put(
        f"/api/tenders/{stopped_tender_id}/kill-switch",
        json={"active": True, "reason": "Tender evidence conflict"},
    )
    assert activated.status_code == 200
    assert activated.json()["active"] is True
    assert activated.json()["version"] == 1
    assert activated.json()["changed"] is True

    repeated = client.put(
        f"/api/tenders/{stopped_tender_id}/kill-switch",
        json={"active": True, "reason": "Tender evidence conflict"},
    )
    assert repeated.status_code == 200
    assert repeated.json()["version"] == 1
    assert repeated.json()["changed"] is False

    with SessionLocal() as db:
        approvals_before = db.scalar(
            select(func.count()).select_from(ApprovalRequest)
        )

    stopped_task_id = _protected_task(client, stopped_tender_id, "stopped")
    stopped = client.post(f"/api/tasks/{stopped_task_id}/run").json()
    assert stopped["status"] == "blocked"
    assert stopped["result"]["reason"] == "tender_kill_switch_active"
    assert stopped["result"]["approval_id"] is None
    assert stopped["result"]["tender_id"] == stopped_tender_id
    assert stopped["result"]["kill_switch"]["version"] == 1

    participation_task_id = _protected_task(
        client,
        stopped_tender_id,
        "participation",
        action_kind="tender_participation",
    )
    participation = client.post(
        f"/api/tasks/{participation_task_id}/run"
    ).json()
    assert participation["result"]["reason"] == "tender_kill_switch_active"
    assert participation["result"]["approval_id"] is None

    with SessionLocal() as db:
        assert (
            db.scalar(select(func.count()).select_from(ApprovalRequest))
            == approvals_before
        )
        audit_count = db.scalar(
            select(func.count()).select_from(AuditLog).where(
                AuditLog.action == "safety.tender_kill_switch_updated",
                AuditLog.resource_id == str(stopped_tender_id),
            )
        )
        assert audit_count == 1
        update_event_count = db.scalar(
            select(func.count()).select_from(DomainEvent).where(
                DomainEvent.event_type == "safety.tender_kill_switch_updated",
                DomainEvent.aggregate_id == str(stopped_tender_id),
            )
        )
        assert update_event_count == 1
        event = db.scalar(
            select(DomainEvent).where(
                DomainEvent.event_type == "policy.execution_blocked",
                DomainEvent.aggregate_id == str(stopped_task_id),
            )
        )
        assert event is not None
        assert event.payload["reason"] == "tender_kill_switch_active"

    other_task_id = _protected_task(client, other_tender_id, "other")
    other = client.post(f"/api/tasks/{other_task_id}/run").json()
    assert other["result"]["reason"] == "owner_approval_required"
    assert other["result"]["approval_id"] is not None

    released = client.put(
        f"/api/tenders/{stopped_tender_id}/kill-switch",
        json={"active": False, "reason": "Evidence conflict resolved"},
    )
    assert released.status_code == 200
    assert released.json()["active"] is False
    assert released.json()["version"] == 2
    with SessionLocal() as db:
        assert db.get(Task, stopped_task_id).status == "blocked"

    released_task_id = _protected_task(client, stopped_tender_id, "released")
    released_task = client.post(f"/api/tasks/{released_task_id}/run").json()
    assert released_task["result"]["reason"] == "owner_approval_required"
    assert released_task["result"]["approval_id"] is not None

    client.put(
        f"/api/tenders/{stopped_tender_id}/kill-switch",
        json={"active": True, "reason": "Second tender-only stop"},
    )
    with SessionLocal() as db:
        db.add(
            SafetyControl(
                key=GLOBAL_EXTERNAL_ACTIONS_CONTROL,
                active=True,
                reason="Global incident",
                version=1,
                updated_by="test",
            )
        )
        db.commit()
    global_task_id = _protected_task(client, stopped_tender_id, "global")
    global_block = client.post(f"/api/tasks/{global_task_id}/run").json()
    assert global_block["result"]["reason"] == "global_kill_switch_active"

    with SessionLocal() as db:
        db.query(SafetyControl).filter(
            SafetyControl.key.in_((control_key, GLOBAL_EXTERNAL_ACTIONS_CONTROL))
        ).delete(synchronize_session=False)
        db.commit()
