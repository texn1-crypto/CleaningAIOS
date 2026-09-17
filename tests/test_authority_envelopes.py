from datetime import datetime, timedelta, timezone

from app.capability_flags import ensure_capability_flags
from app.db import SessionLocal
from app.models import (
    ApprovalRequest,
    AuditLog,
    AuthorityEnvelope,
    AuthorityEnvelopeUse,
    BusinessRecord,
    CapabilityFlag,
    DomainEvent,
    InboxMessage,
    OutboundMessage,
    SafetyControl,
)


def _future(hours: int = 24) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def _enable_external_actions() -> None:
    with SessionLocal() as db:
        db.query(OutboundMessage).filter(
            OutboundMessage.authority_envelope_use_id.is_not(None)
        ).delete(synchronize_session=False)
        db.query(AuthorityEnvelopeUse).delete()
        db.query(AuthorityEnvelope).delete()
        db.query(SafetyControl).delete()
        ensure_capability_flags(db)
        for row in db.query(CapabilityFlag).all():
            row.enabled = True
            row.reason = "Authority envelope test baseline"
        db.commit()


def test_owner_can_grant_one_bounded_authority_and_usage_is_append_only(client):
    _enable_external_actions()
    payload = {
        "envelope_key": "supplier-rfq-pilot-1",
        "action": "supplier_rfq",
        "scope": {"recipient_categories": ["verified_supplier"]},
        "limits": {
            "max_actions_total": 1,
            "max_recipients_per_action": 2,
            "minimum_margin_percent": "15.00",
            "maximum_risk_score": 40,
        },
        "expires_at": _future(),
        "rationale": "One verified supplier RFQ pilot within fixed limits",
    }
    denied = client.post(
        "/api/autonomy/envelopes",
        headers={"X-Role": "manager"},
        json=payload,
    )
    assert denied.status_code == 403

    created = client.post("/api/autonomy/envelopes", json=payload)
    assert created.status_code == 201
    assert created.json()["created"] is True
    assert created.json()["mode"] == "AUTO_WITHIN_LIMIT"
    assert created.json()["status"] == "active"

    replay = client.post("/api/autonomy/envelopes", json=payload)
    assert replay.status_code == 201
    assert replay.json()["created"] is False
    assert replay.json()["id"] == created.json()["id"]

    first = client.post(
        "/api/tasks",
        json={
            "title": "Bounded supplier RFQ authorization",
            "agent_type": "sales",
            "payload": {
                "autonomy_action": "supplier_rfq",
                "autonomy_idempotency_key": "supplier-rfq:pilot:request-1",
                "autonomy_context": {
                    "recipient_category": "verified_supplier",
                    "recipients": 2,
                    "margin_percent": "21.50",
                    "risk_score": 20,
                },
            },
        },
    ).json()
    executed = client.post(f"/api/tasks/{first['id']}/run")
    assert executed.status_code == 200
    assert executed.json()["status"] == "blocked"
    assert "execution_gap" in executed.json()["result"]

    with SessionLocal() as db:
        use = db.query(AuthorityEnvelopeUse).one()
        assert use.task_id == first["id"]
        assert use.idempotency_key == "supplier-rfq:pilot:request-1"
        assert (
            db.query(AuditLog)
            .filter(AuditLog.action == "autonomy.envelope_authorized")
            .count()
            == 1
        )
        assert (
            db.query(DomainEvent)
            .filter(DomainEvent.event_type == "autonomy.envelope_authorized")
            .count()
            == 1
        )

    second = client.post(
        "/api/tasks",
        json={
            "title": "RFQ beyond envelope limit",
            "agent_type": "sales",
            "payload": {
                "autonomy_action": "supplier_rfq",
                "autonomy_idempotency_key": "supplier-rfq:pilot:request-2",
                "autonomy_context": {
                    "recipient_category": "verified_supplier",
                    "recipients": 1,
                    "margin_percent": "21.50",
                    "risk_score": 20,
                },
            },
        },
    ).json()
    blocked = client.post(f"/api/tasks/{second['id']}/run")
    assert blocked.status_code == 200
    assert blocked.json()["status"] == "blocked"
    assert blocked.json()["result"]["reason"] == "max_actions_total_exceeded"
    with SessionLocal() as db:
        assert db.query(AuthorityEnvelopeUse).count() == 1


def test_envelope_never_bypasses_protected_or_forbidden_actions(client):
    _enable_external_actions()
    forbidden = client.post(
        "/api/tasks",
        json={
            "title": "Never sign automatically",
            "agent_type": "sales",
            "payload": {"autonomy_action": "automatic_contract_signature"},
        },
    ).json()
    forbidden_result = client.post(f"/api/tasks/{forbidden['id']}/run").json()
    assert forbidden_result["status"] == "blocked"
    assert forbidden_result["result"]["reason"] == "autonomy_action_forbidden"

    approval_task = client.post(
        "/api/tasks",
        json={
            "title": "Tender submission remains owner-approved",
            "agent_type": "tender",
            "payload": {"autonomy_action": "tender_submission"},
        },
    ).json()
    approval_result = client.post(
        f"/api/tasks/{approval_task['id']}/run"
    ).json()
    assert approval_result["status"] == "blocked"
    assert approval_result["result"]["reason"] == "owner_approval_required"
    with SessionLocal() as db:
        approval = db.query(ApprovalRequest).filter(
            ApprovalRequest.resource_type == "task",
            ApprovalRequest.resource_id == str(approval_task["id"]),
        ).one()
        assert approval.action_kind == "tender_submission"

    unknown = client.post(
        "/api/tasks",
        json={
            "title": "Unknown autonomy action fails closed",
            "agent_type": "sales",
            "payload": {"autonomy_action": "invented_money_action"},
        },
    ).json()
    unknown_result = client.post(f"/api/tasks/{unknown['id']}/run").json()
    assert unknown_result["status"] == "blocked"
    assert unknown_result["result"]["reason"] == "autonomy_action_unknown"


def test_owner_can_revoke_envelope_and_replay_does_not_consume_twice(client):
    _enable_external_actions()
    payload = {
        "envelope_key": "follow-up-sequence-1",
        "action": "outreach_follow_up",
        "scope": {
            "channels": ["email"],
            "recipient_categories": ["inbound_consented_lead"],
            "template_keys": ["follow-up-v1"],
        },
        "limits": {
            "max_actions_total": 5,
            "max_actions_per_day": 2,
            "max_recipients_per_action": 1,
        },
        "expires_at": _future(),
        "rationale": "Approved inbound follow-up sequence",
    }
    row = client.post("/api/autonomy/envelopes", json=payload).json()
    task_payload = {
        "autonomy_action": "outreach_follow_up",
        "autonomy_idempotency_key": "follow-up:lead-1:step-1",
        "autonomy_context": {
            "channel": "email",
            "recipient_category": "inbound_consented_lead",
            "template_key": "follow-up-v1",
            "recipients": 1,
        },
    }
    task = client.post(
        "/api/tasks",
        json={
            "title": "Approved follow-up",
            "agent_type": "sales",
            "payload": task_payload,
        },
    ).json()
    executed = client.post(f"/api/tasks/{task['id']}/run").json()
    assert executed["status"] == "blocked"
    assert "execution_gap" in executed["result"]

    with SessionLocal() as db:
        envelope = db.get(AuthorityEnvelope, row["id"])
        assert envelope is not None
        use = db.query(AuthorityEnvelopeUse).one()
        # Direct policy re-evaluation of the same effect is idempotent.
        from app.autonomy import authorize_within_envelope

        result = authorize_within_envelope(
            db,
            action="outreach_follow_up",
            context=task_payload["autonomy_context"],
            idempotency_key="follow-up:lead-1:step-1",
            actor="sales",
            task_id=task["id"],
        )
        assert result["allowed"] is True
        assert result["usage_created"] is False
        assert result["usage_id"] == use.id
        assert db.query(AuthorityEnvelopeUse).count() == 1
        wrong_task = authorize_within_envelope(
            db,
            action="outreach_follow_up",
            context=task_payload["autonomy_context"],
            idempotency_key="follow-up:lead-1:step-1",
            actor="sales",
            task_id=task["id"] + 1,
        )
        assert wrong_task == {
            "allowed": False,
            "reason": "autonomy_idempotency_conflict",
            "mode": "AUTO_WITHIN_LIMIT",
        }
        wrong_actor = authorize_within_envelope(
            db,
            action="outreach_follow_up",
            context=task_payload["autonomy_context"],
            idempotency_key="follow-up:lead-1:step-1",
            actor="marketing",
            task_id=task["id"],
        )
        assert wrong_actor["allowed"] is False
        assert wrong_actor["reason"] == "autonomy_idempotency_conflict"
        assert db.query(AuthorityEnvelopeUse).count() == 1

    revoked = client.post(
        f"/api/autonomy/envelopes/{row['id']}/revoke",
        json={"reason": "Pilot completed"},
    )
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "revoked"
    repeated = client.post(
        f"/api/autonomy/envelopes/{row['id']}/revoke",
        json={"reason": "Pilot completed"},
    )
    assert repeated.status_code == 200
    assert repeated.json()["changed"] is False


def test_marketing_envelope_requires_money_cap_and_policy_is_visible(client):
    policy = client.get("/api/autonomy/policy")
    assert policy.status_code == 200
    indexed = {row["action"]: row for row in policy.json()["policies"]}
    assert indexed["marketing_campaign_manage"]["mode"] == "AUTO_WITHIN_LIMIT"
    assert indexed["automatic_payment"]["mode"] == "FORBIDDEN"
    assert indexed["tender_submission"]["mode"] == "APPROVAL_REQUIRED"

    invalid = client.post(
        "/api/autonomy/envelopes",
        json={
            "envelope_key": "marketing-without-money-cap",
            "action": "marketing_campaign_manage",
            "scope": {"channels": ["yandex_direct"]},
            "limits": {"max_actions_total": 10},
            "expires_at": _future(),
            "rationale": "Must be rejected without a monetary cap",
        },
    )
    assert invalid.status_code == 422
    assert "monetary limit" in invalid.json()["detail"]


def test_inbound_reply_is_really_queued_and_revocation_blocks_delivery(client):
    _enable_external_actions()
    with SessionLocal() as db:
        lead = BusinessRecord(
            record_type="lead",
            external_id="authority-inbound-lead",
            title="Inbound customer",
            status="qualified",
            score=90,
            source="public_site",
            data={
                "name": "Клиент",
                "email": "inbound@example.com",
                "service": "office",
                "location": "Москва",
                "consent": True,
            },
        )
        db.add(lead)
        db.flush()
        db.add(
            InboxMessage(
                channel="email",
                external_id="authority-inbound-message",
                sender="inbound@example.com",
                recipient="sales@example.com",
                subject="Request",
                body="Please contact me",
                record_id=lead.id,
                data={"consent": True},
            )
        )
        db.commit()
        lead_id = lead.id

    envelope = client.post(
        "/api/autonomy/envelopes",
        json={
            "envelope_key": "inbound-reply-pilot-1",
            "action": "inbound_lead_reply",
            "scope": {
                "channels": ["email"],
                "recipient_categories": ["inbound_consented_lead"],
                "template_keys": ["inbound-reply-v1"],
            },
            "limits": {
                "max_actions_total": 1,
                "max_recipients_per_action": 1,
            },
            "expires_at": _future(),
            "rationale": "One deterministic response to a proven inbound request",
        },
    ).json()
    task = client.post(
        "/api/tasks",
        json={
            "title": "Reply to proven inbound lead",
            "agent_type": "sales",
            "payload": {
                "action": "send_inbound_lead_reply",
                "record_id": lead_id,
                "template_key": "inbound-reply-v1",
                "autonomy_action": "inbound_lead_reply",
                "autonomy_idempotency_key": "inbound-reply:lead-1:first",
                "autonomy_context": {
                    "channel": "email",
                    "recipient_category": "inbound_consented_lead",
                    "template_key": "inbound-reply-v1",
                    "recipients": 1,
                },
            },
        },
    ).json()
    result = client.post(f"/api/tasks/{task['id']}/run")
    assert result.status_code == 200
    assert result.json()["status"] == "done"
    assert result.json()["result"]["outbound_message_id"]
    assert result.json()["result"]["evidence"][0]["type"] == "outreach_message_queued"

    revoked = client.post(
        f"/api/autonomy/envelopes/{envelope['id']}/revoke",
        json={"reason": "Stop the pilot before provider delivery"},
    )
    assert revoked.status_code == 200
    with SessionLocal() as db:
        message = db.scalar(
            db.query(OutboundMessage).filter(
                OutboundMessage.recipient == "inbound@example.com"
            ).statement
        )
        assert message is not None
        assert message.authority_envelope_use_id is not None
        message_id = message.id
        message.scheduled_at = datetime.now(timezone.utc).replace(
            tzinfo=None, hour=7, minute=0, second=0, microsecond=0
        )
        db.commit()

    from app.worker import send_next_email

    with SessionLocal() as db:
        check_at = datetime.now(timezone.utc).replace(
            tzinfo=None, hour=8, minute=0, second=0, microsecond=0
        )
        assert send_next_email(db, now=check_at) is False
    with SessionLocal() as db:
        message = db.get(OutboundMessage, message_id)
        assert message is not None
        assert message.status == "blocked_authority"
        db.query(OutboundMessage).filter(OutboundMessage.id == message_id).delete()
        db.query(InboxMessage).filter(
            InboxMessage.external_id == "authority-inbound-message"
        ).delete()
        db.query(BusinessRecord).filter(BusinessRecord.id == lead_id).delete()
        db.query(AuthorityEnvelopeUse).delete()
        db.query(AuthorityEnvelope).delete()
        db.commit()
