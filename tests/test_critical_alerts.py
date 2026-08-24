import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


def _isolated_session():
    from app.db import Base

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def test_critical_event_creates_one_correlated_alert_and_consumer_receipt(monkeypatch):
    from app.config import settings
    from app.models import EventConsumerReceipt, OwnerNotification
    from app.platform import event_bus, process_next_event

    monkeypatch.setattr(settings, "owner_telegram_id", "70001")
    monkeypatch.setattr(settings, "telegram_bot_token", "123456:test-token")
    sessions = _isolated_session()
    with sessions() as db:
        first = event_bus.publish(
            db,
            "payment.overdue",
            "payment",
            "88",
            {"deadline_at": "2026-08-12T10:00:00"},
            idempotency_key="critical-payment-overdue-test",
            correlation_id="corr-payment-88",
        )
        duplicate = event_bus.publish(
            db,
            "payment.overdue",
            "payment",
            "88",
            {"deadline_at": "2026-08-12T10:00:00"},
            idempotency_key="critical-payment-overdue-test",
            correlation_id="corr-payment-88",
        )
        assert first.id == duplicate.id
        db.commit()

        processed = process_next_event(db)
        assert processed.id == first.id
        alert = db.scalar(
            select(OwnerNotification).where(
                OwnerNotification.idempotency_key
                == f"critical-event:{first.event_id}:telegram"
            )
        )
        assert alert.status == "queued"
        assert alert.severity == "critical"
        assert alert.correlation_id == "corr-payment-88"
        assert alert.data["event_id"] == first.event_id
        assert "88" in alert.body
        receipt = db.scalar(
            select(EventConsumerReceipt).where(
                EventConsumerReceipt.event_id == first.id,
                EventConsumerReceipt.consumer == "critical_alerts",
            )
        )
        assert receipt.status == "succeeded"
        assert receipt.result_ref == f"owner_notification:{alert.id}"
        assert process_next_event(db) is None
        assert db.scalar(
            select(func.count())
            .select_from(OwnerNotification)
            .where(
                OwnerNotification.idempotency_key
                == f"critical-event:{first.event_id}:telegram"
            )
        ) == 1


def test_non_task_approval_event_gets_buttons_notification_fallback(monkeypatch):
    from app.config import settings
    from app.models import ApprovalRequest, OwnerNotification
    from app.platform import event_bus, process_next_event

    monkeypatch.setattr(settings, "owner_telegram_id", "70004")
    monkeypatch.setattr(settings, "telegram_bot_token", "123456:test-token")
    sessions = _isolated_session()
    with sessions() as db:
        approval = ApprovalRequest(
            action_kind="bulk_outreach",
            resource_type="outreach_campaign",
            resource_id="campaign-42",
            rationale="Owner must approve the campaign",
            status="pending",
            payload={},
        )
        db.add(approval)
        db.flush()
        event = event_bus.publish(
            db,
            "approval.requested",
            "campaign",
            "campaign-42",
            {"approval_id": approval.id},
            idempotency_key="non-task-approval-buttons-test",
        )
        db.commit()

        process_next_event(db)
        alert = db.scalar(
            select(OwnerNotification).where(
                OwnerNotification.idempotency_key
                == f"critical-event:{event.event_id}:telegram"
            )
        )
        assert alert is not None
        assert alert.data["approval_id"] == approval.id
        assert alert.status == "queued"


def test_approval_event_reuses_existing_explicit_notification(monkeypatch):
    from app.config import settings
    from app.models import ApprovalRequest, OwnerNotification
    from app.notifications import queue_owner_notification
    from app.platform import event_bus, process_next_event

    monkeypatch.setattr(settings, "owner_telegram_id", "70005")
    monkeypatch.setattr(settings, "telegram_bot_token", "123456:test-token")
    sessions = _isolated_session()
    with sessions() as db:
        approval = ApprovalRequest(
            action_kind="social_publication",
            resource_type="social_content_batch",
            resource_id="batch-7",
            rationale="Review content",
            status="pending",
            payload={},
        )
        db.add(approval)
        db.flush()
        explicit = queue_owner_notification(
            db,
            idempotency_key="explicit-social-approval-test",
            channel="telegram",
            resource_type="social_content_batch",
            resource_id="batch-7",
            subject="Review posts",
            body="Approval required",
            data={"approval_id": approval.id},
        )
        event = event_bus.publish(
            db,
            "approval.requested",
            "social_content_batch",
            "batch-7",
            {"approval_id": approval.id},
            idempotency_key="explicit-social-approval-event-test",
        )
        db.commit()

        process_next_event(db)
        rows = db.scalars(select(OwnerNotification)).all()
        approval_rows = [
            row
            for row in rows
            if isinstance(row.data, dict)
            and row.data.get("approval_id") == approval.id
        ]
        assert [row.id for row in approval_rows] == [explicit.id]
        assert db.scalar(
            select(OwnerNotification).where(
                OwnerNotification.idempotency_key
                == f"critical-event:{event.event_id}:telegram"
            )
        ) is None


def test_pending_approval_reconciliation_restores_missing_button_card(monkeypatch):
    from app.config import settings
    from app.models import ApprovalRequest, OwnerNotification
    from app.notifications import queue_missing_approval_notifications

    monkeypatch.setattr(settings, "owner_telegram_id", "70006")
    monkeypatch.setattr(settings, "telegram_bot_token", "123456:test-token")
    sessions = _isolated_session()
    with sessions() as db:
        approval = ApprovalRequest(
            action_kind="tender_participation",
            resource_type="tender",
            resource_id="99",
            rationale="Owner decision is required",
            status="pending",
            expires_at=datetime.now(timezone.utc).replace(tzinfo=None)
            + timedelta(hours=2),
            payload={},
        )
        db.add(approval)
        db.commit()

        first = queue_missing_approval_notifications(db)
        second = queue_missing_approval_notifications(db)
        assert len(first) == 1
        assert second == []
        row = db.scalar(
            select(OwnerNotification).where(
                OwnerNotification.idempotency_key
                == f"approval-reconciliation:{approval.id}:telegram"
            )
        )
        assert row.data["approval_id"] == approval.id
        assert row.status == "queued"


def test_pending_approval_reconciliation_revives_dead_letter_once(monkeypatch):
    from app.config import settings
    from app.models import ApprovalRequest, OwnerNotification
    from app.notifications import queue_missing_approval_notifications

    monkeypatch.setattr(settings, "owner_telegram_id", "70007")
    monkeypatch.setattr(settings, "telegram_bot_token", "123456:test-token")
    sessions = _isolated_session()
    with sessions() as db:
        approval = ApprovalRequest(
            action_kind="social_publication",
            resource_type="social_content_batch",
            resource_id="batch-dead-letter",
            rationale="Review posts",
            status="pending",
            expires_at=datetime.now(timezone.utc).replace(tzinfo=None)
            + timedelta(hours=2),
            payload={},
        )
        db.add(approval)
        db.flush()
        notification = OwnerNotification(
            idempotency_key="dead-letter-approval-card",
            channel="telegram",
            recipient="70007",
            resource_type=approval.resource_type,
            resource_id=approval.resource_id,
            subject="Review posts",
            body="Approval required",
            data={"approval_id": approval.id},
            status="dead_letter",
            attempts=5,
            last_error="transport failed",
            dead_lettered_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        db.add(notification)
        db.commit()

        recovered = queue_missing_approval_notifications(db)
        assert [row.id for row in recovered] == [notification.id]
        assert notification.status == "queued"
        assert notification.attempts == 0
        assert notification.last_error == ""
        assert notification.data["approval_recovery_attempted"] is True

        notification.status = "dead_letter"
        db.flush()
        assert queue_missing_approval_notifications(db) == []


def test_critical_alert_retries_then_enters_dead_letter(monkeypatch):
    from app import notifications
    from app.config import settings
    from app.models import OwnerNotification

    monkeypatch.setattr(settings, "owner_telegram_id", "70002")
    monkeypatch.setattr(settings, "telegram_bot_token", "123456:test-token")
    sessions = _isolated_session()

    def fail_delivery(db, row):
        raise OSError("controlled Telegram transport failure")

    monkeypatch.setattr(notifications, "_send_telegram", fail_delivery)
    with sessions() as db:
        alert = notifications.queue_owner_notification(
            db,
            idempotency_key="critical-retry-test",
            channel="telegram",
            resource_type="task",
            resource_id="99",
            subject="Critical retry test",
            body="Controlled test",
            severity="critical",
            correlation_id="corr-retry-99",
        )
        db.commit()
        for expected_attempt in range(1, 6):
            alert.available_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
            db.commit()
            assert notifications.send_next_owner_notification(db) is True
            db.refresh(alert)
            assert alert.attempts == expected_attempt
        assert alert.status == "dead_letter"
        assert alert.dead_lettered_at is not None
        assert "controlled Telegram transport failure" in alert.last_error
        same = notifications.queue_owner_notification(
            db,
            idempotency_key="critical-retry-test",
            channel="telegram",
            resource_type="task",
            resource_id="99",
            subject="Duplicate",
            body="Duplicate",
            severity="critical",
        )
        assert same.id == alert.id


def test_telegram_alert_acknowledgement_is_signed_audited_and_idempotent(
    client, monkeypatch
):
    from app import bot
    from app.config import settings
    from app.db import SessionLocal
    from app.models import AuditLog, DomainEvent, OwnerNotification
    from app.notifications import queue_owner_notification
    from app.telegram_control import issue_alert_ack_token, parse_alert_ack_token

    owner_user, owner_chat = 71001, 72001
    monkeypatch.setattr(settings, "owner_telegram_id", str(owner_user))
    monkeypatch.setattr(settings, "owner_telegram_chat_id", str(owner_chat))
    monkeypatch.setattr(settings, "telegram_callback_secret", "alert-test-secret")
    monkeypatch.setattr(settings, "telegram_bot_token", "123456:test-token")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with SessionLocal() as db:
        alert = queue_owner_notification(
            db,
            idempotency_key="telegram-alert-ack-test",
            channel="telegram",
            resource_type="task",
            resource_id="100",
            subject="Critical alert",
            body="Acknowledge this alert",
            severity="critical",
            correlation_id="corr-alert-100",
        )
        alert.status = "sent"
        alert.sent_at = now
        db.commit()
        token = issue_alert_ack_token(alert)
        notification_id = alert.id
    assert parse_alert_ack_token(token)["notification_id"] == notification_id
    assert len(token.encode()) <= 64

    async def in_process_api(method, path, **kwargs):
        response = client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()

    class Message:
        def __init__(self):
            self.replies = []

        async def reply_text(self, value, **kwargs):
            self.replies.append((value, kwargs))

    class Query:
        data = token

        async def answer(self):
            return None

    class User:
        id = owner_user

    class Chat:
        id = owner_chat

    class Update:
        effective_message = Message()
        effective_user = User()
        effective_chat = Chat()
        callback_query = Query()

    class Context:
        user_data = {}

    monkeypatch.setattr(bot, "api", in_process_api)
    update = Update()
    asyncio.run(bot.callback(update, Context()))
    assert "подтверждено" in update.effective_message.replies[-1][0]
    asyncio.run(bot.callback(update, Context()))
    assert "ранее" in update.effective_message.replies[-1][0]

    with SessionLocal() as db:
        alert = db.get(OwnerNotification, notification_id)
        assert alert.acknowledged_at is not None
        assert alert.acknowledged_by == "telegram-owner"
        assert db.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(
                AuditLog.action == "owner_notification.acknowledged",
                AuditLog.resource_id == str(notification_id),
            )
        ) == 1
        assert db.scalar(
            select(func.count())
            .select_from(DomainEvent)
            .where(
                DomainEvent.idempotency_key
                == f"owner-notification:{notification_id}:acknowledged"
            )
        ) == 1

    metrics = client.get(
        "/api/owner-notifications/metrics", headers={"X-Role": "manager"}
    )
    assert metrics.status_code == 200
    assert metrics.json()["acknowledged"] >= 1
    rows = client.get(
        "/api/owner-notifications", headers={"X-Role": "manager"}
    ).json()
    saved = next(row for row in rows if row["id"] == notification_id)
    assert saved["severity"] == "critical"
    assert saved["correlation_id"] == "corr-alert-100"
    assert saved["acknowledged_at"] is not None


def test_critical_telegram_delivery_contains_signed_acknowledgement(monkeypatch):
    from app import notifications
    from app.config import settings
    from app.models import OwnerNotification
    from app.telegram_control import parse_alert_ack_token

    captured = {}

    class Response:
        def raise_for_status(self):
            return None

    class Client:
        def __init__(self, *args, **kwargs):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, json):
            captured.update({"url": url, "payload": json})
            return Response()

    monkeypatch.setattr(notifications.httpx, "Client", Client)
    monkeypatch.setattr(settings, "telegram_bot_token", "123456:test-token")
    monkeypatch.setattr(settings, "telegram_callback_secret", "alert-test-secret")
    row = OwnerNotification(
        id=321,
        idempotency_key="critical-delivery-buttons",
        channel="telegram",
        recipient="70003",
        subject="Critical",
        body="Critical body",
        data={},
        severity="critical",
    )
    notifications._send_telegram(object(), row)
    button = captured["payload"]["reply_markup"]["inline_keyboard"][-1][0]
    assert button["text"] == "✅ Принять оповещение"
    assert parse_alert_ack_token(button["callback_data"])["notification_id"] == 321
