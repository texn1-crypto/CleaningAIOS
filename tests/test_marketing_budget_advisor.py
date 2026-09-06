from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.agents import MarketingAgent
from app.approval_service import decide_approval
from app.db import Base
from app.marketing_budget_advisor import (
    HARD_DAILY_BUDGET_CAP_RUB,
    build_daily_marketing_budget_advice,
    verified_official_url,
)
from app.models import (
    ApprovalRequest,
    BusinessRecord,
    OutboundMessage,
    OwnerNotification,
    Task,
)
from app.security import Principal


def _session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def test_advisor_caps_budget_uses_funnel_and_never_spends(monkeypatch):
    session_factory = _session_factory()
    with session_factory() as db:
        db.add_all(
            [
                BusinessRecord(
                    record_type="lead",
                    title="Inbound lead",
                    status="qualified",
                    score=85,
                    source="website",
                    data={"utm_campaign": "cleaning-spb"},
                ),
                BusinessRecord(
                    record_type="lead",
                    title="Won lead",
                    status="won",
                    score=90,
                    source="telegram",
                    data={"utm_campaign": "cleaning-spb"},
                ),
                BusinessRecord(
                    record_type="marketing_experiment",
                    external_id="cleaning-spb",
                    title="VK experiment",
                    status="running",
                    source="vk_ads",
                    data={"spent": 500},
                ),
            ]
        )
        db.flush()

        first = build_daily_marketing_budget_advice(
            db,
            local_day="2042-05-10",
            requested_daily_budget_rub=99_999,
        )
        db.commit()

        assert first["created"] is True
        assert first["daily_budget_rub"] == HARD_DAILY_BUDGET_CAP_RUB
        assert first["automatic_spend"] is False
        assert first["activation_mode"] == "manual_after_owner_approval"
        assert first["channel"] == "vk_ads"
        assert first["official_url"] == "https://ads.vk.com/"
        assert first["official_domain_verified"] is True
        assert first["funnel_facts"] == {
            "leads": 2,
            "qualified": 2,
            "won": 1,
            "attributed": 2,
            "qualification_rate": 1.0,
            "sources": {"telegram": 1, "website": 1},
        }
        assert first["expected_kpi"]["estimate_not_guarantee"] is True
        approval = db.get(ApprovalRequest, first["approval_id"])
        assert approval.action_kind == "financial"
        assert approval.status == "pending"
        assert approval.payload["daily_budget_rub"] == HARD_DAILY_BUDGET_CAP_RUB
        assert approval.payload["automatic_spend"] is False
        notification = db.scalar(select(OwnerNotification))
        assert notification.data["approval_id"] == approval.id
        assert db.scalar(select(func.count()).select_from(OutboundMessage)) == 0

        replay = build_daily_marketing_budget_advice(
            db,
            local_day="2042-05-10",
            requested_daily_budget_rub=1,
        )
        db.commit()
        assert replay["created"] is False
        assert replay["idempotent_replay"] is True
        assert replay["daily_budget_rub"] == HARD_DAILY_BUDGET_CAP_RUB
        assert db.scalar(
            select(func.count()).select_from(BusinessRecord).where(
                BusinessRecord.record_type == "marketing_budget_advice"
            )
        ) == 1
        assert db.scalar(select(func.count()).select_from(ApprovalRequest)) == 1
        assert db.scalar(select(func.count()).select_from(OwnerNotification)) == 1

        decision = decide_approval(
            db,
            approval_id=approval.id,
            action="approve",
            note="Approved for a manual test",
            actor=Principal(subject="test-owner", role="owner"),
        )
        db.commit()
        db.refresh(existing := db.get(BusinessRecord, first["id"]))
        assert decision["execution"] == "not_executed"
        assert existing.status == "approved_for_manual_activation"
        assert existing.data["automatic_spend"] is False
        assert existing.data["activation_mode"] == "manual_after_owner_approval"
        assert db.scalar(select(func.count()).select_from(OutboundMessage)) == 0


def test_advisor_rejects_non_official_channel_url(monkeypatch):
    from app import marketing_budget_advisor

    monkeypatch.setitem(
        marketing_budget_advisor.OFFICIAL_CHANNEL_URLS,
        "yandex_direct",
        "https://example.com/pay",
    )
    try:
        verified_official_url("yandex_direct")
    except ValueError as exc:
        assert "verified official URL" in str(exc)
    else:
        raise AssertionError("Unsafe advertising URL was accepted")


def test_marketing_agent_runs_advisor_without_external_action():
    session_factory = _session_factory()
    with session_factory() as db:
        result = MarketingAgent().execute(
            db,
            {
                "action": "daily_marketing_budget_advice",
                "scheduled_local_day": "2042-05-11",
                "daily_budget_rub": 2_500,
                "notify_owner": False,
            },
        )
        db.commit()
        assert result["daily_budget_rub"] == 2_000
        assert result["owner_notification"] == "disabled"
        assert result["automatic_spend"] is False
        assert db.scalar(select(func.count()).select_from(OutboundMessage)) == 0


def test_scheduler_creates_one_capped_daily_advisor_task(monkeypatch):
    from app import scheduler

    session_factory = _session_factory()
    monkeypatch.setattr(scheduler, "SessionLocal", session_factory)
    monkeypatch.setattr(scheduler.settings, "marketing_budget_advisor_timezone", "UTC")
    monkeypatch.setattr(scheduler.settings, "marketing_budget_advisor_daily_hour", 0)
    monkeypatch.setattr(scheduler.settings, "marketing_budget_daily_limit_rub", 50_000)
    monkeypatch.setattr(scheduler.settings, "perplexity_api_key", "")
    monkeypatch.setattr(scheduler.settings, "tender_sources", "")

    scheduler.schedule_cycle()
    scheduler.schedule_cycle()

    with session_factory() as db:
        tasks = db.scalars(
            select(Task).where(Task.title.like("Daily marketing budget advice · %"))
        ).all()
        assert len(tasks) == 1
        assert tasks[0].agent_type == "marketing"
        assert tasks[0].payload["action"] == "daily_marketing_budget_advice"
        assert tasks[0].payload["daily_budget_rub"] == HARD_DAILY_BUDGET_CAP_RUB
        assert tasks[0].payload["automatic_spend"] is False


def test_budget_advice_api_exposes_advisor_result_with_rbac(client):
    task = client.post(
        "/api/tasks",
        json={
            "title": "Marketing budget API integration · 2042-05-12",
            "agent_type": "marketing",
            "max_attempts": 1,
            "payload": {
                "action": "daily_marketing_budget_advice",
                "scheduled_local_day": "2042-05-12",
                "daily_budget_rub": 20_000,
                "notify_owner": False,
                "automatic_spend": False,
            },
        },
    ).json()
    result = client.post(f"/api/tasks/{task['id']}/run").json()["result"]
    assert result["daily_budget_rub"] == HARD_DAILY_BUDGET_CAP_RUB
    assert result["automatic_spend"] is False

    denied = client.get("/api/marketing/budget-advice", headers={"X-Role": "viewer"})
    assert denied.status_code == 403
    rows = client.get(
        "/api/marketing/budget-advice", headers={"X-Role": "manager"}
    ).json()
    saved = next(row for row in rows if row["id"] == result["id"])
    assert saved["approval_status"] == "pending"
    assert saved["official_domain_verified"] is True
