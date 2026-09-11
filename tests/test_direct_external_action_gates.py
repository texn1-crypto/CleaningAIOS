from datetime import datetime

from app.capability_flags import ensure_capability_flags, external_action_gate
from app.db import SessionLocal
from app.models import CapabilityFlag, ContentItem, OutboundMessage, SafetyControl
from app.social_runtime import publish_next_social_post
from app.worker import send_next_email


def test_direct_worker_actions_share_fail_closed_persisted_gate(client, monkeypatch):
    smtp_attempts: list[str] = []
    social_attempts: list[str] = []

    class ForbiddenSmtp:
        def __init__(self, *args, **kwargs):
            smtp_attempts.append("attempted")
            raise AssertionError("SMTP must not run while the safety gate is closed")

    class ForbiddenSocialClient:
        def __init__(self, *args, **kwargs):
            social_attempts.append("attempted")
            raise AssertionError("Social provider must not run while the safety gate is closed")

    monkeypatch.setattr("app.worker.smtplib.SMTP", ForbiddenSmtp)
    monkeypatch.setattr("app.worker.smtplib.SMTP_SSL", ForbiddenSmtp)
    monkeypatch.setattr("app.social_runtime.httpx.Client", ForbiddenSocialClient)

    due = datetime(2042, 1, 1, 12, 0)
    with SessionLocal() as db:
        db.query(SafetyControl).delete()
        ensure_capability_flags(db)
        for key in ("bulk_outreach", "social_publication"):
            flag = db.get(CapabilityFlag, key)
            assert flag is not None
            flag.enabled = False
            flag.reason = "Direct executor test stop"
        email = OutboundMessage(
            campaign_key="direct-gate-test",
            recipient="consented-business@example.com",
            subject="Approved subject",
            body="Approved body",
            status="queued",
            scheduled_at=datetime(1999, 1, 1),
        )
        post = ContentItem(
            channel="telegram",
            title="Approved social draft",
            body="Approved social body",
            status="scheduled",
            scheduled_at=datetime(1999, 1, 1),
        )
        db.add_all([email, post])
        db.commit()
        email_id = email.id
        post_id = post.id

        assert send_next_email(db, now=due) is False
        assert publish_next_social_post(db, now=due) is False
        assert db.get(OutboundMessage, email_id).status == "queued"
        assert db.get(ContentItem, post_id).status == "scheduled"

        for key in ("bulk_outreach", "social_publication"):
            flag = db.get(CapabilityFlag, key)
            assert flag is not None
            flag.enabled = True
        db.add(
            SafetyControl(
                key="global_external_actions",
                active=True,
                reason="Direct executor incident containment",
                version=1,
                updated_by="owner-test",
            )
        )
        db.commit()

        assert send_next_email(db, now=due) is False
        assert publish_next_social_post(db, now=due) is False
        assert external_action_gate(db, "bulk_outreach")["reason"] == (
            "global_kill_switch_active"
        )

        db.query(SafetyControl).delete()
        for key in ("bulk_outreach", "social_publication"):
            flag = db.get(CapabilityFlag, key)
            assert flag is not None
            db.delete(flag)
        db.commit()

        assert send_next_email(db, now=due) is False
        assert publish_next_social_post(db, now=due) is False
        assert external_action_gate(db, "social_publication")[
            "capability_flag"
        ] == {
            "key": "social_publication",
            "enabled": False,
            "version": 0,
            "reason": "capability_flag_missing",
        }
        assert db.get(OutboundMessage, email_id).status == "queued"
        assert db.get(ContentItem, post_id).status == "scheduled"

        db.delete(db.get(OutboundMessage, email_id))
        db.delete(db.get(ContentItem, post_id))
        ensure_capability_flags(db)
        db.commit()

    assert smtp_attempts == []
    assert social_attempts == []
