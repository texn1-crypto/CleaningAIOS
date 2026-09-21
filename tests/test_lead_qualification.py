from __future__ import annotations

from datetime import datetime

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.agents import SalesAgent
from app.business_policy import LEAD_GOAL_TITLE
from app import lead_qualification, lead_research
from app.lead_outcomes import LEAD_HANDOFF_METRIC, run_ceo_lead_outcome_cycle
from app.lead_qualification import (
    QUALIFICATION_ACTION,
    QUALIFICATION_RECORD_TYPE,
    prioritize_owner_review_leads,
)
from app.models import (
    AuditLog,
    BusinessGoal,
    BusinessRecord,
    OutboundMessage,
    OutreachConsent,
    OwnerNotification,
    Task,
)
from app.money_opportunities import build_money_opportunities


def _session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    from app.db import Base

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _lead(*, title: str = "УК Проверка", complete: bool = False) -> BusinessRecord:
    data = {
        "region": "Санкт-Петербург",
        "organization_type": "management_company",
        "website": "https://example.test/",
        "public_emails": ["info@example.test"],
        "public_phones": ["+78120000000"],
        "source_urls": ["https://example.test/"],
        "last_verified_at": "2045-06-05T10:00:00",
        "verification_reasons": ["organization_name_match"],
        "outreach_consent": "not_verified",
        "automatic_outreach": False,
    }
    if complete:
        data.update(
            {
                "public_need_evidence": ["published cleaning procurement notice"],
                "area_m2": 4200,
                "service_scope": ["daily_cleaning"],
                "frequency": "daily",
                "procurement_timing": "this_month",
                "buyer_role": "procurement_department",
                "estimated_monthly_value": 450_000,
            }
        )
    return BusinessRecord(
        record_type="lead",
        external_id=f"qualification:{title}",
        title=title,
        status="owner_review",
        score=85,
        owner="sales",
        source="test",
        data=data,
    )


def _goal() -> BusinessGoal:
    return BusinessGoal(
        title=LEAD_GOAL_TITLE,
        description="Verified owner handoffs",
        status="active",
        owner="lead_coordinator",
        metric=LEAD_HANDOFF_METRIC,
        baseline=0,
        target=20,
        current=0,
        unit="leads/month",
    )


def test_owner_review_qualification_records_evidence_gaps_without_outreach():
    session_factory = _session_factory()
    now = datetime(2045, 6, 5, 12, 0)
    with session_factory() as db:
        lead = _lead()
        db.add(lead)
        db.flush()

        result = prioritize_owner_review_leads(db, current=now)

        db.flush()
        qualification = lead.data["qualification"]
        assert result["outcomes"] == {"owner_review": 1}
        assert qualification["outcome"] == "owner_review"
        assert qualification["score"] == 70
        assert qualification["contact_path"] == "consent_or_lawful_basis_required"
        assert "public_evidence_of_need" in qualification["missing_facts"]
        assert "approved_outreach_channel" in qualification["missing_facts"]
        assert lead.data["next_action"] == "research_need_scope_timing_and_buyer_route"
        assert lead.status == "owner_review"
        assert result["automatic_outreach"] is False
        assert result["external_messages_sent"] is False
        summary = build_money_opportunities(db)["sales"]["summary"]
        assert summary["owner_review_triaged"] == 1
        assert summary["owner_review_untriaged"] == 0
        assert summary["owner_action_queue"] == 1
        assert db.scalar(select(func.count()).select_from(OutboundMessage)) == 0


def test_legacy_public_researched_lead_is_promoted_into_owner_review_pipeline():
    session_factory = _session_factory()
    now = datetime(2045, 6, 5, 12, 0)
    with session_factory() as db:
        lead = _lead(title="Публичный БЦ")
        lead.status = "researched"
        lead.source = "perplexity_public_business_search"
        db.add(lead)
        db.flush()

        result = prioritize_owner_review_leads(db, current=now, notify_owner=False)

        assert result["promoted_lead_ids"] == [lead.id]
        assert lead.status == "owner_review"
        assert lead.data["qualification"]["outcome"] == "owner_review"
        assert db.scalar(select(func.count()).select_from(OutboundMessage)) == 0
        db.flush()
        audit = db.scalar(
            select(AuditLog).where(
                AuditLog.action == "lead.owner_review_prioritized"
            )
        )
        assert audit is not None
        assert audit.details["promoted_lead_ids"] == [lead.id]


def test_cited_public_scout_lead_counts_as_verified_organization():
    session_factory = _session_factory()
    now = datetime(2045, 6, 5, 12, 0)
    with session_factory() as db:
        lead = _lead(title="Проверенный публичный БЦ")
        lead.source = "perplexity_public_business_search"
        lead.data = {
            **lead.data,
            "verification_reasons": [],
            "contact_scope": "organization",
        }
        db.add(lead)
        db.flush()

        result = prioritize_owner_review_leads(db, current=now, notify_owner=False)

        assert result["outcomes"] == {"owner_review": 1}
        assert (
            "organization_identity_verified"
            not in lead.data["qualification"]["missing_facts"]
        )
        assert lead.data["qualification"]["score_breakdown"][
            "organization_evidence"
        ] == 15


def test_russian_management_company_type_is_normalized():
    session_factory = _session_factory()
    now = datetime(2045, 6, 5, 12, 0)
    with session_factory() as db:
        lead = _lead(title="УК с русским типом")
        lead.data = {**lead.data, "organization_type": "УК"}
        db.add(lead)
        db.flush()

        result = prioritize_owner_review_leads(db, current=now)

        qualification = lead.data["qualification"]
        assert result["outcomes"] == {"owner_review": 1}
        assert qualification["score_breakdown"]["property_type_fit"] == 15
        assert qualification["responsible_scout"] == "management_lead_scout"
        assert "property_type" not in qualification["missing_facts"]


def test_sales_agent_routes_the_persisted_qualification_action():
    session_factory = _session_factory()
    with session_factory() as db:
        lead = _lead()
        lead.data = {
            **lead.data,
            "last_verified_at": datetime.now().isoformat(),
        }
        db.add(lead)
        db.flush()

        result = SalesAgent().execute(
            db,
            {"action": QUALIFICATION_ACTION, "notify_owner": False},
        )

        assert result["status"] == "completed"
        assert result["lead_count"] == 1
        assert result["notification_id"] is None
        assert result["external_messages_sent"] is False
        assert db.scalar(select(func.count()).select_from(OutboundMessage)) == 0


def test_sales_ready_recommendation_requires_verified_unsuppressed_consent():
    session_factory = _session_factory()
    now = datetime(2045, 6, 5, 12, 0)
    with session_factory() as db:
        lead = _lead(title="БЦ с запросом", complete=True)
        db.add(lead)
        db.flush()
        db.add(
            OutreachConsent(
                address="info@example.test",
                record_id=lead.id,
                status="verified",
                purpose="commercial_outreach",
                source_url="https://example.test/consent",
                evidence_hash="a" * 64,
                verified_by="owner",
            )
        )
        db.flush()

        result = prioritize_owner_review_leads(db, current=now)

        assert result["outcomes"] == {"sales_ready": 1}
        assert lead.data["qualification"]["score"] == 100
        assert lead.data["qualification"]["missing_facts"] == []
        assert lead.data["qualification"]["contact_path"] == "approved"
        assert lead.status == "owner_review"
        assert db.scalar(select(func.count()).select_from(OutboundMessage)) == 0


def test_research_outcome_names_missing_organization_identity():
    session_factory = _session_factory()
    now = datetime(2045, 6, 5, 12, 0)
    with session_factory() as db:
        lead = _lead(title="Непроверенная организация", complete=True)
        lead.data = {
            **lead.data,
            "verification_reasons": [],
            "source_urls": [],
        }
        db.add(lead)
        db.flush()

        result = prioritize_owner_review_leads(db, current=now)

        assert result["outcomes"] == {"research": 1}
        assert (
            "organization_identity_verified"
            in lead.data["qualification"]["missing_facts"]
        )
        assert lead.data["qualification"]["outcome"] == "research"


def test_generic_or_out_of_scope_region_is_not_treated_as_service_area_fit():
    session_factory = _session_factory()
    now = datetime(2045, 6, 5, 12, 0)
    with session_factory() as db:
        generic = _lead(title="Регион не определён")
        generic.data = {**generic.data, "region": "область"}
        out_of_scope = _lead(title="Другой регион")
        out_of_scope.data = {**out_of_scope.data, "region": "Свердловская область"}
        db.add_all([generic, out_of_scope])
        db.flush()

        result = prioritize_owner_review_leads(db, current=now)

        assert result["outcomes"] == {"reject": 2}
        for lead in (generic, out_of_scope):
            assert "service_area_fit" in lead.data["qualification"]["missing_facts"]
            assert lead.status == "owner_review"


def test_nurture_outcome_and_metrics_preserve_missing_contact_permission():
    session_factory = _session_factory()
    now = datetime(2045, 6, 5, 12, 0)
    with session_factory() as db:
        lead = _lead(title="Есть потребность без согласия", complete=True)
        db.add(lead)
        db.flush()

        result = prioritize_owner_review_leads(db, current=now)
        summary = build_money_opportunities(db)["sales"]["summary"]

        assert result["outcomes"] == {"nurture": 1}
        assert "approved_outreach_channel" in lead.data["qualification"]["missing_facts"]
        assert summary["qualification_nurture"] == 1
        assert summary["qualification_research"] == 0
        assert summary["qualification_reject"] == 0
        assert db.scalar(select(func.count()).select_from(OutboundMessage)) == 0


def test_bounded_runs_prioritize_untriaged_leads(monkeypatch):
    monkeypatch.setattr(lead_qualification, "QUALIFICATION_LIMIT", 1)
    session_factory = _session_factory()
    now = datetime(2045, 6, 5, 12, 0)
    with session_factory() as db:
        older = _lead(title="Старшая карточка")
        newer = _lead(title="Новая карточка")
        db.add_all([older, newer])
        db.flush()

        first = prioritize_owner_review_leads(db, current=now, notify_owner=False)
        second = prioritize_owner_review_leads(db, current=now, notify_owner=False)

        assert first["changed_lead_ids"] == [newer.id]
        assert second["changed_lead_ids"] == [older.id]
        assert older.data.get("qualification_fingerprint")
        assert newer.data.get("qualification_fingerprint")


def test_qualification_retry_reuses_daily_digest_notification_and_audit():
    session_factory = _session_factory()
    now = datetime(2045, 6, 5, 12, 0)
    with session_factory() as db:
        lead = _lead()
        db.add(lead)
        db.flush()

        first = prioritize_owner_review_leads(db, current=now)
        repeated = prioritize_owner_review_leads(db, current=now)
        db.flush()

        assert first["digest_id"] == repeated["digest_id"]
        assert first["notification_id"] == repeated["notification_id"]
        assert repeated["changed_lead_ids"] == []
        assert repeated["unchanged_lead_ids"] == [lead.id]
        assert db.scalar(
            select(func.count()).select_from(BusinessRecord).where(
                BusinessRecord.record_type == QUALIFICATION_RECORD_TYPE
            )
        ) == 1
        assert db.scalar(select(func.count()).select_from(OwnerNotification)) == 1
        notification = db.get(OwnerNotification, first["notification_id"])
        assert notification is not None
        assert notification.severity == "normal"
        assert db.scalar(
            select(func.count()).select_from(AuditLog).where(
                AuditLog.action == "lead.owner_review_prioritized"
            )
        ) == 1
        assert db.scalar(select(func.count()).select_from(OutboundMessage)) == 0

        new_lead = _lead(title="Новая карточка в тот же день")
        db.add(new_lead)
        db.flush()
        changed_batch = prioritize_owner_review_leads(db, current=now)
        db.flush()

        assert changed_batch["digest_id"] == first["digest_id"]
        assert changed_batch["notification_id"] != first["notification_id"]
        assert db.scalar(select(func.count()).select_from(OwnerNotification)) == 2
        assert db.scalar(
            select(func.count()).select_from(AuditLog).where(
                AuditLog.action == "lead.owner_review_prioritized"
            )
        ) == 2


def test_ceo_schedules_one_daily_sales_qualification_task():
    session_factory = _session_factory()
    now = datetime(2045, 6, 5, 12, 0)
    with session_factory() as db:
        db.add_all([_goal(), _lead()])
        db.flush()

        first = run_ceo_lead_outcome_cycle(db, cycle_key="2045-06-05T12:00", now=now)
        repeated = run_ceo_lead_outcome_cycle(db, cycle_key="2045-06-05T13:00", now=now)

        assert first["qualification_task_id"] == repeated["qualification_task_id"]
        assert first["owner_review_count"] == 1
        task = db.get(Task, first["qualification_task_id"])
        assert task is not None
        assert task.agent_type == "sales"
        assert task.payload == {
            "action": QUALIFICATION_ACTION,
            "notify_owner": True,
            "automatic_outreach": False,
            "backlog_fingerprint": first["qualification_backlog"]["fingerprint"],
            "backlog_count": 1,
        }
        assert db.scalar(
            select(func.count()).select_from(Task).where(
                Task.payload["action"].as_string() == QUALIFICATION_ACTION
            )
        ) == 1
        assert db.scalar(select(func.count()).select_from(OutboundMessage)) == 0


def test_ceo_schedules_qualification_for_legacy_public_researched_lead():
    session_factory = _session_factory()
    now = datetime(2045, 6, 5, 12, 0)
    with session_factory() as db:
        lead = _lead(title="Старая публичная карточка")
        lead.status = "researched"
        lead.source = "perplexity_public_business_search"
        db.add_all([_goal(), lead])
        db.flush()

        result = run_ceo_lead_outcome_cycle(
            db,
            cycle_key="2045-06-05T12:00",
            now=now,
        )

        assert result["owner_review_count"] == 0
        assert result["qualification_candidate_count"] == 1
        assert result["qualification_backlog"]["count"] == 1
        assert result["qualification_task_id"] is not None


def test_ceo_schedules_targeted_research_after_qualification(monkeypatch):
    monkeypatch.setattr(lead_research.settings, "perplexity_api_key", "configured")
    session_factory = _session_factory()
    now = datetime(2045, 6, 5, 12, 0)
    with session_factory() as db:
        lead = _lead(title="Карточка для исследования")
        db.add_all([_goal(), lead])
        db.flush()
        prioritize_owner_review_leads(db, current=now, notify_owner=False)

        result = run_ceo_lead_outcome_cycle(
            db,
            cycle_key="2045-06-05T12:00",
            now=now,
        )

        assert result["lead_research_work"]["eligible_leads"] == 1
        assert len(result["lead_research_work"]["tasks_created"]) == 1
        task = db.get(Task, result["lead_research_work"]["tasks_created"][0])
        assert task is not None
        assert task.agent_type == "management_lead_scout"
        assert task.payload["action"] == "research_public_lead_evidence"
        assert task.payload["automatic_outreach"] is False


def test_ceo_creates_new_same_day_task_when_qualification_backlog_changes():
    session_factory = _session_factory()
    now = datetime(2045, 6, 5, 12, 0)
    with session_factory() as db:
        first_lead = _lead(title="Первая карточка")
        db.add_all([_goal(), first_lead])
        db.flush()

        first = run_ceo_lead_outcome_cycle(
            db,
            cycle_key="2045-06-05T12:00",
            now=now,
        )
        prioritize_owner_review_leads(db, current=now, notify_owner=False)
        first_task = db.get(Task, first["qualification_task_id"])
        assert first_task is not None
        first_task.status = "done"

        second_lead = _lead(title="Вторая карточка")
        db.add(second_lead)
        db.flush()
        second = run_ceo_lead_outcome_cycle(
            db,
            cycle_key="2045-06-05T13:00",
            now=now,
        )

        assert second["qualification_backlog"]["count"] == 1
        assert second["qualification_task_id"] != first["qualification_task_id"]


def test_researched_fact_requeues_same_lead_for_qualification():
    session_factory = _session_factory()
    now = datetime(2045, 6, 5, 12, 0)
    with session_factory() as db:
        lead = _lead(title="Карточка с новым фактом")
        db.add_all([_goal(), lead])
        db.flush()

        first = run_ceo_lead_outcome_cycle(
            db,
            cycle_key="2045-06-05T12:00",
            now=now,
        )
        prioritize_owner_review_leads(db, current=now, notify_owner=False)
        first_task = db.get(Task, first["qualification_task_id"])
        assert first_task is not None
        first_task.status = "done"
        data = dict(lead.data or {})
        data["public_need_evidence"] = "Опубликован запрос на клининг"
        data.pop("qualification_fingerprint", None)
        lead.data = data

        second = run_ceo_lead_outcome_cycle(
            db,
            cycle_key="2045-06-05T13:00",
            now=now,
        )

        assert second["qualification_backlog"]["count"] == 1
        assert second["qualification_task_id"] != first["qualification_task_id"]
