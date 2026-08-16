from datetime import datetime, timedelta

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import (
    AgentRun,
    AuditLog,
    ImprovementRequest,
    OutboundMessage,
    OwnerNotification,
    SenderMailbox,
    Task,
)


def _session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def test_system_admin_deduplicates_incidents_and_verifies_recovery(monkeypatch):
    from app import system_admin

    session_factory = _session_factory()
    now = datetime(2040, 1, 1, 12, 0)

    def fake_handoff(row):
        row.handoff_status = "credentials_required"
        return {"status": "credentials_required"}

    monkeypatch.setattr(system_admin, "retry_workspace_handoff", fake_handoff)

    with session_factory() as db:
        task = Task(
            title="Mail campaign blocked",
            agent_type="sales",
            status="blocked",
            result={
                "status": "credentials_required",
                "execution_gap": "SMTP credentials rejected",
                "credentials_required": ["SMTP_PASSWORD"],
            },
        )
        mailbox = SenderMailbox(name="Primary", address="sender@example.com")
        db.add_all([task, mailbox])
        db.flush()
        db.add_all(
            [
                OutboundMessage(
                    campaign_key="sysadmin-test",
                    recipient=f"recipient-{index}@example.com",
                    subject="Test",
                    body="Body",
                    mailbox_id=mailbox.id,
                    status="waiting_configuration",
                    error="SMTP authentication failed",
                    scheduled_at=now,
                )
                for index in range(2)
            ]
        )
        notification = OwnerNotification(
            idempotency_key="sysadmin-notification-test",
            channel="telegram",
            recipient="123",
            status="retry",
            last_error="401 Unauthorized",
            available_at=now,
        )
        request_run = AgentRun(
            agent_type="request_analyst",
            task_id=task.id,
            status="succeeded",
            input={
                "message": "Почему почта не отправляет письма?",
                "intent": {"kind": "outreach"},
            },
            output={"classification": "supported", "improvement_id": None},
            started_at=now - timedelta(minutes=1),
            finished_at=now - timedelta(minutes=1),
        )
        db.add_all([notification, request_run])
        db.commit()

        first = system_admin.run_system_admin_audit(db, now=now)
        db.commit()
        assert first["overall_status"] == "degraded"
        assert first["summary"] == {
            "active": 3,
            "critical": 2,
            "new": 3,
            "changed": 0,
            "resolved": 0,
        }
        assert {row["resource_type"] for row in first["incidents"]} == {
            "task",
            "sender_mailbox",
            "notification_channel",
        }
        assert first["recent_requests"][0]["intent"] == "outreach"
        assert any(command.startswith("/sysadmin") for command in first["command_catalog"])
        assert db.scalar(select(func.count()).select_from(ImprovementRequest)) == 3
        assert db.scalar(
            select(func.count()).select_from(AuditLog).where(
                AuditLog.action == "system.incident_detected"
            )
        ) == 3

        second = system_admin.run_system_admin_audit(db, now=now + timedelta(minutes=5))
        db.commit()
        assert second["summary"]["active"] == 3
        assert second["summary"]["new"] == 0
        assert {row["verification_status"] for row in second["incidents"]} == {"not_fixed"}
        assert db.scalar(select(func.count()).select_from(ImprovementRequest)) == 3
        assert db.scalar(
            select(func.count()).select_from(AuditLog).where(
                AuditLog.action == "system.incident_detected"
            )
        ) == 3

        task.status = "done"
        task.result = {"outcome": "completed", "evidence": [{"type": "smtp_recheck"}]}
        for message in db.scalars(select(OutboundMessage)).all():
            message.status = "sent"
            message.error = ""
        notification.status = "sent"
        notification.last_error = ""
        db.commit()

        recovered = system_admin.run_system_admin_audit(
            db,
            now=now + timedelta(minutes=10),
        )
        db.commit()
        assert recovered["overall_status"] == "healthy"
        assert recovered["summary"]["active"] == 0
        assert recovered["summary"]["resolved"] == 3
        improvements = db.scalars(select(ImprovementRequest)).all()
        assert {row.status for row in improvements} == {"implemented"}
        assert all(
            row.test_evidence[-1]["result"] == "condition_clear"
            for row in improvements
        )
        assert db.scalar(
            select(func.count()).select_from(AuditLog).where(
                AuditLog.action == "system.incident_resolved"
            )
        ) == 3


def test_system_admin_does_not_treat_owner_approval_as_technical_failure(monkeypatch):
    from app import system_admin

    session_factory = _session_factory()
    now = datetime(2040, 1, 1, 12, 0)
    monkeypatch.setattr(
        system_admin,
        "retry_workspace_handoff",
        lambda row: {"status": "credentials_required"},
    )
    with session_factory() as db:
        db.add(
            Task(
                title="Protected outreach approval",
                agent_type="sales",
                status="blocked",
                result={
                    "blocked": True,
                    "reason": "owner_approval_required",
                    "approval_id": 7,
                },
            )
        )
        db.commit()
        report = system_admin.run_system_admin_audit(db, now=now)
        assert report["summary"]["active"] == 0


def test_system_admin_component_failure_is_redacted_and_recovery_is_audited():
    from app.system_admin import record_component_failure, record_component_recovery

    session_factory = _session_factory()
    now = datetime(2040, 1, 1, 12, 0)
    with session_factory() as db:
        record_component_failure(
            db,
            component="worker",
            error="token=secret-value password=hunter2",
            now=now,
        )
        db.commit()
        failure = db.scalar(
            select(AuditLog).where(AuditLog.action == "system.component_failed")
        )
        assert "secret-value" not in str(failure.details)
        assert "hunter2" not in str(failure.details)

        record_component_recovery(
            db,
            component="worker",
            now=now + timedelta(minutes=1),
        )
        db.commit()
        assert db.scalar(
            select(func.count()).select_from(AuditLog).where(
                AuditLog.action == "system.component_recovered"
            )
        ) == 1


def test_system_admin_agent_runs_through_orchestrator(client, monkeypatch):
    from app import system_admin

    monkeypatch.setattr(
        system_admin,
        "retry_workspace_handoff",
        lambda row: {"status": "credentials_required"},
    )
    task = client.post(
        "/api/tasks",
        json={
            "title": "System administrator orchestrator integration",
            "agent_type": "system_admin",
            "priority": "critical",
            "payload": {
                "action": "system_admin_audit",
                "source": "test",
                "notify_owner": False,
            },
            "max_attempts": 1,
        },
    ).json()
    completed = client.post(f"/api/tasks/{task['id']}/run").json()
    assert completed["status"] == "done"
    assert completed["result"]["outcome"] == "completed"
    assert completed["result"]["report_kind"] == "system_admin"
    assert completed["result"]["safety"] == {
        "automatic_business_retry": False,
        "credentials_redacted": True,
        "owner_approval_preserved": True,
    }
