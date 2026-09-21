import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import func, select


def test_ceo_brief_separates_primary_facts_from_recommendations(client, monkeypatch):
    from app.agents import AGENTS
    from app.db import SessionLocal
    from app.models import BusinessRecord, OwnerNotification, Task

    class FailingAgent:
        name = "ceo_brief_failure"

        def execute(self, db, payload):
            raise RuntimeError("controlled CEO brief failure")

    monkeypatch.setitem(AGENTS, "ceo_brief_failure", FailingAgent())
    failed_task = client.post(
        "/api/tasks",
        json={
            "title": "CEO brief failed task",
            "agent_type": "ceo_brief_failure",
            "max_attempts": 1,
        },
    ).json()
    assert client.post(f"/api/tasks/{failed_task['id']}/run").json()["status"] == "failed"
    protected_task = client.post(
        "/api/tasks",
        json={
            "title": "CEO brief blocked task",
            "agent_type": "tender",
            "payload": {"action_kind": "tender_submission"},
        },
    ).json()
    blocked = client.post(f"/api/tasks/{protected_task['id']}/run").json()
    approval_id = blocked["result"]["approval_id"]

    with SessionLocal() as db:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        payment = BusinessRecord(
            record_type="payment",
            title="CEO brief overdue payment",
            status="overdue",
            data={"amount": 12500},
        )
        alert = OwnerNotification(
            idempotency_key="ceo-brief-critical-alert",
            channel="telegram",
            subject="CEO brief critical alert",
            body="Test alert",
            severity="critical",
            correlation_id="corr-ceo-brief",
            status="sent",
            sent_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        waiting_configuration = Task(
            title="CEO brief integration wait",
            agent_type="sales",
            status="blocked",
            payload={"action": "sync_twenty_verified_leads"},
            result={
                "status": "unavailable",
                "credentials_required": ["TWENTY_API_KEY"],
            },
        )
        legacy_configuration_wait = Task(
            title="CEO brief legacy configuration wait",
            agent_type="research",
            status="blocked",
            payload={
                "blocking_requirements": [
                    "YANDEX_SEARCH_API_KEY",
                    "YANDEX_CLOUD_FOLDER_ID",
                ]
            },
        )
        runnable_task = Task(
            title="CEO brief runnable task",
            agent_type="research",
            status="queued",
            run_after=now - timedelta(minutes=1),
        )
        scheduled_task = Task(
            title="CEO brief scheduled task",
            agent_type="research",
            status="queued",
            run_after=now + timedelta(days=1),
        )
        reconciled_failure = Task(
            title="CEO brief reconciled verification failure",
            agent_type="lead_coordinator",
            status="failed",
            payload={"action": "verify_existing_management_company_candidate"},
            result={
                "error_type": "AgentToolDenied",
                "resolution_status": "reconciled",
                "resolution_kind": "verification_candidate_retry_accounted",
            },
        )
        db.add_all(
            [
                payment,
                alert,
                waiting_configuration,
                legacy_configuration_wait,
                runnable_task,
                scheduled_task,
                reconciled_failure,
            ]
        )
        db.commit()
        payment_id = payment.id
        alert_id = alert.id
        waiting_configuration_id = waiting_configuration.id
        legacy_configuration_wait_id = legacy_configuration_wait.id
        runnable_task_id = runnable_task.id
        scheduled_task_id = scheduled_task.id
        reconciled_failure_id = reconciled_failure.id

    response = client.get("/api/ceo/brief", headers={"X-Role": "manager"})
    assert response.status_code == 200
    brief = response.json()
    assert brief["freshness"]["source"] == "primary_database"
    assert brief["generated_at"] == brief["freshness"]["as_of"]
    assert brief["ai_generated_facts"] is False
    assert brief["automatic_critical_action"] is False
    assert failed_task["id"] in brief["facts"]["tasks"]["failed_ids"]
    assert failed_task["id"] in brief["facts"]["tasks"]["actionable_failed_ids"]
    assert reconciled_failure_id in brief["facts"]["tasks"]["failed_ids"]
    assert reconciled_failure_id in brief["facts"]["tasks"]["reconciled_failed_ids"]
    assert reconciled_failure_id not in brief["facts"]["tasks"]["actionable_failed_ids"]
    assert brief["facts"]["tasks"]["reconciled_failed"] >= 1
    assert protected_task["id"] in brief["facts"]["tasks"]["blocked_ids"]
    assert protected_task["id"] in brief["facts"]["tasks"]["actionable_blocked_ids"]
    assert waiting_configuration_id in brief["facts"]["tasks"]["waiting_configuration_ids"]
    assert legacy_configuration_wait_id in brief["facts"]["tasks"]["waiting_configuration_ids"]
    assert legacy_configuration_wait_id not in brief["facts"]["tasks"]["actionable_blocked_ids"]
    assert brief["facts"]["tasks"]["waiting_configuration"] >= 2
    assert runnable_task_id in brief["facts"]["tasks"]["runnable_active_ids"]
    assert scheduled_task_id in brief["facts"]["tasks"]["scheduled_active_ids"]
    assert brief["facts"]["tasks"]["runnable_active"] >= 1
    assert brief["facts"]["tasks"]["scheduled_active"] >= 1
    assert brief["facts"]["tasks"]["next_scheduled_at"]
    assert approval_id in brief["facts"]["approvals"]["ids"]
    assert alert_id in brief["facts"]["critical_alerts"]["ids"]
    assert payment_id in brief["facts"]["finance"]["payment_ids"]
    assert brief["facts"]["finance"]["overdue_amount"] >= 12500
    assert brief["recommendations"]
    assert all(item["source_ids"] for item in brief["recommendations"])
    assert {source["endpoint"] for source in brief["sources"]} >= {
        "/api/tasks",
        "/api/approvals",
        "/api/owner-notifications",
    }
    assert client.get("/api/ceo/brief", headers={"X-Role": "viewer"}).status_code == 403


def test_telegram_ceo_brief_is_read_only_and_task_button_uses_tasks_api(monkeypatch):
    from app import bot

    brief = {
        "generated_at": "2026-08-13T12:00:00",
        "facts": {
            "tasks": {
                "active": 2,
                "runnable_active": 1,
                "scheduled_active": 1,
                "failed": 1,
                "actionable_failed": 1,
                "reconciled_failed": 0,
                "blocked": 1,
                "failed_ids": [10],
                "actionable_failed_ids": [10],
                "blocked_ids": [11],
            },
            "approvals": {"pending": 1, "ids": [22]},
            "critical_alerts": {"unacknowledged": 1, "dead_letter": 0, "ids": [33]},
            "finance": {"overdue_payments": 1, "overdue_amount": 5000, "payment_ids": [44]},
        },
        "recommendations": [
            {
                "priority": "high",
                "text": "Разобрать задачи.",
                "source_ids": [10, 11],
            }
        ],
    }
    calls = []

    async def fake_api(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/api/ceo/brief":
            return brief
        if path == "/api/tasks":
            return {"id": 701}
        raise AssertionError(path)

    async def fake_authorize(update, minimum_role):
        assert minimum_role == "manager"
        return {"authorized": True, "role": "manager"}

    class Message:
        def __init__(self):
            self.replies = []

        async def reply_text(self, value, **kwargs):
            self.replies.append((value, kwargs))

    class Query:
        def __init__(self, data):
            self.data = data

        async def answer(self):
            return None

    class Update:
        def __init__(self):
            self.effective_message = Message()
            self.callback_query = Query("ceo")

    class Context:
        user_data = {}

    monkeypatch.setattr(bot, "api", fake_api)
    monkeypatch.setattr(bot, "_authorize_update", fake_authorize)
    update = Update()
    asyncio.run(bot.callback(update, Context()))
    text, kwargs = update.effective_message.replies[-1]
    assert "ФАКТЫ ИЗ БД" in text
    assert "РЕКОМЕНДАЦИИ (НЕ ВЫПОЛНЕНЫ)" in text
    assert "active 2, runnable 1, scheduled 1" in text
    assert "source task IDs: [10, 11]" in text
    buttons = [button for row in kwargs["reply_markup"].inline_keyboard for button in row]
    assert [button.callback_data for button in buttons] == [
        "ceo:refresh",
        "ceo:create_review_task",
    ]
    assert [path for _, path, _ in calls] == ["/api/ceo/brief"]

    update.callback_query = Query("ceo:create_review_task")
    asyncio.run(bot.callback(update, Context()))
    assert [path for _, path, _ in calls] == ["/api/ceo/brief", "/api/tasks"]
    task_payload = calls[-1][2]["json"]
    assert task_payload["agent_type"] == "ceo"
    assert task_payload["payload"]["automatic_critical_action"] is False
    assert "Критические действия не запускались" in update.effective_message.replies[-1][0]


def test_ceo_brief_does_not_render_reconciled_failures_as_actionable_sources():
    from app.reports import format_ceo_brief

    rendered = format_ceo_brief(
        {
            "generated_at": "2045-06-05T12:00:00",
            "facts": {
                "tasks": {
                    "failed": 1,
                    "actionable_failed": 0,
                    "reconciled_failed": 1,
                    "failed_ids": [17],
                    "actionable_failed_ids": [],
                }
            },
        }
    )

    assert "actionable failed 0" in rendered
    assert "reconciled failed 1" in rendered
    assert "source task IDs: []" in rendered


def test_task_configuration_wait_supports_legacy_payload_without_false_positive():
    from app.models import Task
    from app.task_state import task_waits_for_configuration

    legacy_wait = Task(
        title="Legacy credentials wait",
        status="blocked",
        payload={"blocking_requirements": ["YANDEX_SEARCH_API_KEY", "YANDEX_CLOUD_FOLDER_ID"]},
    )
    operational_wait = Task(
        title="Owner decision wait",
        status="blocked",
        payload={"blocking_requirements": ["owner approval"]},
    )

    assert task_waits_for_configuration(legacy_wait) is True
    assert task_waits_for_configuration(operational_wait) is False


def test_ceo_brief_separates_current_alerts_and_approval_expiry(client):
    from app.db import SessionLocal
    from app.models import ApprovalRequest, OwnerNotification, Task

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with SessionLocal() as db:
        configuration_wait = Task(
            title="Alert configuration wait",
            agent_type="sales",
            status="blocked",
            result={"credentials_required": ["TWENTY_API_KEY"]},
        )
        reconciled_failure = Task(
            title="Alert reconciled failure",
            agent_type="lead_coordinator",
            status="failed",
            result={
                "resolution_status": "reconciled",
                "resolution_kind": "verification_candidate_retry_accounted",
            },
        )
        db.add_all([configuration_wait, reconciled_failure])
        db.flush()
        expired = ApprovalRequest(
            action_kind="financial",
            status="pending",
            expires_at=now - timedelta(minutes=1),
        )
        current = ApprovalRequest(
            action_kind="financial",
            status="pending",
            expires_at=now + timedelta(hours=1),
        )
        db.add_all([expired, current])
        db.flush()
        old_snapshot = OwnerNotification(
            idempotency_key=f"old-system-admin-{uuid4().hex}",
            channel="telegram",
            resource_type="system_admin_report",
            resource_id="old",
            severity="high",
            status="sent",
            sent_at=now - timedelta(minutes=5),
            created_at=now - timedelta(minutes=5),
        )
        current_snapshot = OwnerNotification(
            idempotency_key=f"current-system-admin-{uuid4().hex}",
            channel="telegram",
            resource_type="system_admin_report",
            resource_id="current",
            severity="high",
            status="sent",
            sent_at=now,
            created_at=now,
        )
        configuration_alert = OwnerNotification(
            idempotency_key=f"configuration-alert-{uuid4().hex}",
            channel="telegram",
            resource_type="task",
            resource_id=str(configuration_wait.id),
            severity="critical",
            status="sent",
            sent_at=now,
            data={"event_type": "agent.incident_reported"},
        )
        reconciled_alert = OwnerNotification(
            idempotency_key=f"reconciled-alert-{uuid4().hex}",
            channel="telegram",
            resource_type="task",
            resource_id=str(reconciled_failure.id),
            severity="critical",
            status="sent",
            sent_at=now,
            data={"event_type": "task.failed"},
        )
        db.add_all(
            [
                old_snapshot,
                current_snapshot,
                configuration_alert,
                reconciled_alert,
            ]
        )
        db.commit()
        current_approval_id = current.id
        expired_approval_id = expired.id
        old_snapshot_id = old_snapshot.id
        current_snapshot_id = current_snapshot.id
        configuration_alert_id = configuration_alert.id
        reconciled_alert_id = reconciled_alert.id

    brief = client.get("/api/ceo/brief", headers={"X-Role": "manager"}).json()
    approvals = brief["facts"]["approvals"]
    alerts = brief["facts"]["critical_alerts"]
    assert current_approval_id in approvals["ids"]
    assert expired_approval_id not in approvals["ids"]
    assert expired_approval_id in approvals["expired_pending_ids"]
    assert approvals["pending_total"] == approvals["pending"] + approvals["expired_pending"]
    assert current_snapshot_id in alerts["ids"]
    assert old_snapshot_id not in alerts["ids"]
    assert configuration_alert_id not in alerts["ids"]
    assert reconciled_alert_id not in alerts["ids"]
    assert alerts["historical_or_superseded"] >= 3
    assert alerts["unacknowledged"] >= alerts["actionable"]


def test_weekly_ceo_brief_joins_business_facts_and_reuses_notification(client):
    from app.db import SessionLocal
    from app.models import (
        AgentRun,
        BusinessGoal,
        BusinessRecord,
        OperatingEntity,
        OwnerNotification,
    )
    from app.reports import format_ceo_brief

    suffix = uuid4().hex[:8]
    current = datetime.now(timezone.utc).replace(tzinfo=None)
    with SessionLocal() as db:
        db.add_all(
            [
                BusinessRecord(
                    record_type="lead",
                    title=f"Weekly CEO lead {suffix}",
                    status="qualified",
                    score=90,
                    source="weekly-ceo-test",
                    data={"next_action": "owner_review"},
                ),
                BusinessRecord(
                    record_type="lead",
                    title=f"Weekly CEO owner review lead {suffix}",
                    status="owner_review",
                    score=90,
                    source="weekly-ceo-test",
                ),
                BusinessRecord(
                    record_type="tender",
                    title=f"Weekly CEO tender {suffix}",
                    status="qualified",
                    source=f"weekly-ceo-test-{suffix}",
                    external_id=f"weekly-ceo-tender-{suffix}",
                ),
                BusinessRecord(
                    record_type="marketing_experiment",
                    title=f"Weekly CEO marketing {suffix}",
                    status="running",
                    source="telegram",
                    external_id=f"weekly-ceo-marketing-{suffix}",
                    data={"spent": 1000},
                ),
                OperatingEntity(
                    entity_type="contract",
                    name=f"Weekly CEO contract {suffix}",
                    status="active",
                    data={"monthly_revenue": 125000},
                ),
                AgentRun(
                    agent_type="sales",
                    status="succeeded",
                    started_at=current,
                    finished_at=current,
                    evidence=[{"type": "weekly_ceo_test"}],
                ),
                BusinessGoal(
                    title=f"Weekly CEO handoff goal {suffix}",
                    owner="lead_coordinator",
                    metric="qualified_owner_handoffs",
                    baseline=0,
                    current=23,
                    target=100,
                    unit="leads/month",
                    status="active",
                ),
            ]
        )
        db.commit()

    response = client.get("/api/ceo/brief?period_days=7", headers={"X-Role": "manager"})
    assert response.status_code == 200
    brief = response.json()
    assert brief["report_kind"] == "weekly_ceo_brief"
    assert brief["period"]["days"] == 7
    assert brief["facts"]["sales"]["new_leads_in_period"] >= 1
    assert brief["facts"]["sales"]["qualified"] >= 1
    assert brief["facts"]["sales"]["owner_review"] >= 1
    assert brief["facts"]["sales"]["owner_handoff_goal"]["current"] == 23
    assert brief["facts"]["sales"]["owner_handoff_goal"]["target"] == 100
    assert brief["facts"]["sales"]["active_contracts"] >= 1
    assert float(brief["facts"]["sales"]["active_monthly_revenue"]) >= 125000
    assert brief["facts"]["tenders"]["active"] >= 1
    assert brief["facts"]["marketing"]["running"] >= 1
    assert brief["facts"]["agents"]["succeeded"] >= 1
    assert brief["freshness"]["sources"]["business_records"]
    assert brief["execution_plan"]
    assert all(item["owner_agent"] for item in brief["execution_plan"])
    assert all(item["deadline"] for item in brief["execution_plan"])
    assert all(item["automatic_external_action"] is False for item in brief["execution_plan"])
    rendered = format_ceo_brief(brief)
    assert "ПЛАН НА 7 ДНЕЙ" in rendered
    assert "передано владельцу" in rendered
    assert "внешние сообщения автоматически не выполнялись" in rendered
    assert len(rendered) < 4096
    assert client.get("/api/ceo/brief?period_days=32", headers={"X-Role": "manager"}).status_code == 422

    notification_key = f"weekly-ceo-brief-test:{suffix}"
    payload = {
        "action": "weekly_business_brief",
        "period_days": 7,
        "report_at": current.isoformat(),
        "scheduled_week_start": current.date().isoformat(),
        "notify_owner": True,
        "notification_idempotency_key": notification_key,
        "automatic_external_action": False,
    }
    first_task = client.post(
        "/api/tasks",
        json={
            "title": f"Weekly CEO brief first {suffix}",
            "agent_type": "ceo",
            "payload": payload,
            "max_attempts": 1,
        },
    ).json()
    second_task = client.post(
        "/api/tasks",
        json={
            "title": f"Weekly CEO brief retry {suffix}",
            "agent_type": "ceo",
            "payload": payload,
            "max_attempts": 1,
        },
    ).json()
    first = client.post(f"/api/tasks/{first_task['id']}/run").json()
    second = client.post(f"/api/tasks/{second_task['id']}/run").json()
    assert first["status"] == "done"
    assert second["status"] == "done"
    assert first["result"]["owner_notification_id"] == second["result"]["owner_notification_id"]
    assert first["result"]["automatic_critical_action"] is False
    assert first["result"]["ai_generated_facts"] is False
    assert len(first["result"]["execution_plan"]) <= 5
    with SessionLocal() as db:
        assert db.scalar(
            select(func.count())
            .select_from(OwnerNotification)
            .where(OwnerNotification.idempotency_key == notification_key)
        ) == 1


def test_scheduler_creates_one_weekly_ceo_brief_per_local_week(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app import scheduler
    from app.db import Base
    from app.models import ApprovalRequest, Task

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(scheduler, "SessionLocal", session_factory)
    monkeypatch.setattr(scheduler.settings, "ceo_weekly_brief_timezone", "UTC")
    monkeypatch.setattr(
        scheduler.settings,
        "ceo_weekly_brief_weekday",
        datetime.now(timezone.utc).weekday(),
    )
    monkeypatch.setattr(scheduler.settings, "ceo_weekly_brief_hour", 0)
    monkeypatch.setattr(scheduler.settings, "tender_sources", "")
    monkeypatch.setattr(scheduler.settings, "perplexity_api_key", "")
    monkeypatch.setattr(scheduler.settings, "evolution_research_queries", "")

    with session_factory() as db:
        expired = ApprovalRequest(
            action_kind="financial",
            status="pending",
            expires_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1),
        )
        db.add(expired)
        db.commit()
        expired_id = expired.id

    scheduler.schedule_cycle()
    scheduler.schedule_cycle()

    with session_factory() as db:
        assert db.get(ApprovalRequest, expired_id).status == "expired"
        tasks = db.scalars(
            select(Task).where(Task.title.like("Weekly CEO brief · %"))
        ).all()
        assert len(tasks) == 1
        task = tasks[0]
        assert task.agent_type == "ceo"
        assert task.payload["action"] == "weekly_business_brief"
        assert task.payload["notify_owner"] is True
        assert task.payload["period_days"] == 7
        assert task.payload["automatic_external_action"] is False
        assert task.payload["notification_idempotency_key"].startswith(
            "weekly-ceo-brief:"
        )
