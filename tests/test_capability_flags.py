from app.capability_flags import PROTECTED_CAPABILITIES, ensure_capability_flags
from app.db import SessionLocal
from app.models import (
    ApprovalRequest,
    AuditLog,
    CapabilityFlag,
    DomainEvent,
    SafetyControl,
)


def test_capability_flags_are_owner_controlled_and_block_before_approval(client):
    with SessionLocal() as db:
        db.query(SafetyControl).delete()
        ensure_capability_flags(db)
        for row in db.query(CapabilityFlag).all():
            row.enabled = True
            row.reason = "Test compatibility baseline"
        db.commit()

    visible = client.get("/api/safety/capability-flags")
    assert visible.status_code == 200
    assert [row["key"] for row in visible.json()] == list(PROTECTED_CAPABILITIES)
    assert all(row["enabled"] is True for row in visible.json())
    assert client.get(
        "/api/safety/capability-flags", headers={"X-Role": "viewer"}
    ).status_code == 403

    denied = client.put(
        "/api/safety/capability-flags/tender_submission",
        headers={"X-Role": "manager"},
        json={"enabled": False, "reason": "Release gate is not ready"},
    )
    assert denied.status_code == 403
    assert client.put(
        "/api/safety/capability-flags/tender_submission",
        json={"enabled": False, "reason": ""},
    ).status_code == 422
    assert client.put(
        "/api/safety/capability-flags/not_a_capability",
        json={"enabled": False, "reason": "Unknown capability"},
    ).status_code == 404

    disabled = client.put(
        "/api/safety/capability-flags/tender_submission",
        json={"enabled": False, "reason": "Release gate is not ready"},
    )
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False
    assert disabled.json()["version"] >= 2
    assert disabled.json()["changed"] is True

    repeated = client.put(
        "/api/safety/capability-flags/tender_submission",
        json={"enabled": False, "reason": "Release gate is not ready"},
    )
    assert repeated.status_code == 200
    assert repeated.json()["version"] == disabled.json()["version"]
    assert repeated.json()["changed"] is False

    with SessionLocal() as db:
        approvals_before = db.query(ApprovalRequest).count()

    task = client.post(
        "/api/tasks",
        json={
            "title": "Capability-gated tender submission",
            "agent_type": "tender",
            "payload": {"action_kind": "tender_submission"},
        },
    ).json()
    blocked = client.post(f"/api/tasks/{task['id']}/run").json()
    assert blocked["status"] == "blocked"
    assert blocked["result"]["reason"] == "capability_disabled"
    assert blocked["result"]["approval_id"] is None
    assert blocked["result"]["capability_flag"]["enabled"] is False
    assert blocked["result"]["capability_flag"]["version"] == disabled.json()[
        "version"
    ]

    with SessionLocal() as db:
        assert db.query(ApprovalRequest).count() == approvals_before
        assert (
            db.query(AuditLog)
            .filter(
                AuditLog.action == "safety.capability_flag_updated",
                AuditLog.resource_id == "tender_submission",
            )
            .count()
            == 1
        )
        policy_event = (
            db.query(DomainEvent)
            .filter(
                DomainEvent.event_type == "policy.execution_blocked",
                DomainEvent.aggregate_id == str(task["id"]),
            )
            .one()
        )
        assert policy_event.payload["reason"] == "capability_disabled"

    enabled = client.put(
        "/api/safety/capability-flags/tender_submission",
        json={"enabled": True, "reason": "Compatibility path restored"},
    )
    assert enabled.status_code == 200
    assert enabled.json()["enabled"] is True
    assert enabled.json()["version"] == disabled.json()["version"] + 1

    approval_task = client.post(
        "/api/tasks",
        json={
            "title": "Approval remains required after capability enable",
            "agent_type": "tender",
            "payload": {"action_kind": "tender_submission"},
        },
    ).json()
    approval_block = client.post(
        f"/api/tasks/{approval_task['id']}/run"
    ).json()
    assert approval_block["result"]["reason"] == "owner_approval_required"
    assert approval_block["result"]["approval_id"] is not None


def test_missing_capability_flag_fails_closed(client):
    with SessionLocal() as db:
        flag = db.get(CapabilityFlag, "contract")
        assert flag is not None
        db.delete(flag)
        db.commit()

    task = client.post(
        "/api/tasks",
        json={
            "title": "Missing capability flag must not default to enabled",
            "agent_type": "tender",
            "payload": {"action_kind": "contract"},
        },
    ).json()
    blocked = client.post(f"/api/tasks/{task['id']}/run").json()
    assert blocked["status"] == "blocked"
    assert blocked["result"]["reason"] == "capability_disabled"
    assert blocked["result"]["capability_flag"] == {
        "key": "contract",
        "enabled": False,
        "version": 0,
        "reason": "capability_flag_missing",
    }

    with SessionLocal() as db:
        ensure_capability_flags(db)
        db.commit()
