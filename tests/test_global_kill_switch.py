from app.db import SessionLocal
from app.models import ApprovalRequest, AuditLog, DomainEvent, SafetyControl


def test_global_kill_switch_is_owner_controlled_audited_and_fail_closed(client):
    with SessionLocal() as db:
        db.query(SafetyControl).delete()
        db.commit()

    default_status = client.get("/api/safety/external-actions-kill-switch")
    assert default_status.status_code == 200
    assert default_status.json()["active"] is False
    assert default_status.json()["version"] == 0

    denied = client.put(
        "/api/safety/external-actions-kill-switch",
        headers={"X-Role": "manager"},
        json={"active": True, "reason": "Incident containment"},
    )
    assert denied.status_code == 403
    missing_reason = client.put(
        "/api/safety/external-actions-kill-switch",
        json={"active": True, "reason": ""},
    )
    assert missing_reason.status_code == 422

    activated = client.put(
        "/api/safety/external-actions-kill-switch",
        json={"active": True, "reason": "Incident containment"},
    )
    assert activated.status_code == 200
    assert activated.json()["active"] is True
    assert activated.json()["version"] == 1
    assert activated.json()["changed"] is True

    repeated = client.put(
        "/api/safety/external-actions-kill-switch",
        json={"active": True, "reason": "Incident containment"},
    )
    assert repeated.status_code == 200
    assert repeated.json()["version"] == 1
    assert repeated.json()["changed"] is False

    with SessionLocal() as db:
        approvals_before = db.query(ApprovalRequest).count()

    task = client.post(
        "/api/tasks",
        json={
            "title": "Kill-switch protected submission",
            "agent_type": "tender",
            "payload": {"action_kind": "tender_submission"},
        },
    ).json()
    blocked = client.post(f"/api/tasks/{task['id']}/run").json()
    assert blocked["status"] == "blocked"
    assert blocked["result"]["reason"] == "global_kill_switch_active"
    assert blocked["result"]["approval_id"] is None
    assert blocked["result"]["kill_switch"]["version"] == 1

    unprotected = client.post(
        "/api/tasks",
        json={"title": "Kill-switch read-only check", "agent_type": "tender"},
    ).json()
    completed = client.post(f"/api/tasks/{unprotected['id']}/run").json()
    assert completed["status"] == "done"

    with SessionLocal() as db:
        assert db.query(ApprovalRequest).count() == approvals_before
        assert db.query(AuditLog).filter(
            AuditLog.action == "safety.external_actions_kill_switch_updated"
        ).count() == 1
        policy_event = db.query(DomainEvent).filter(
            DomainEvent.event_type == "policy.execution_blocked",
            DomainEvent.aggregate_id == str(task["id"]),
        ).one()
        assert policy_event.payload["reason"] == "global_kill_switch_active"

    deactivated = client.put(
        "/api/safety/external-actions-kill-switch",
        json={"active": False, "reason": "Incident resolved"},
    )
    assert deactivated.status_code == 200
    assert deactivated.json()["active"] is False
    assert deactivated.json()["version"] == 2

    normal_task = client.post(
        "/api/tasks",
        json={
            "title": "Approval remains mandatory after switch release",
            "agent_type": "tender",
            "payload": {"action_kind": "tender_submission"},
        },
    ).json()
    normal_block = client.post(f"/api/tasks/{normal_task['id']}/run").json()
    assert normal_block["result"]["reason"] == "owner_approval_required"
    assert normal_block["result"]["approval_id"] is not None

    with SessionLocal() as db:
        db.query(SafetyControl).delete()
        db.commit()
