from __future__ import annotations

from datetime import datetime

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import agent_tools, lead_outcomes, lead_reports
from app.business_policy import LEAD_GOAL_TITLE
from app.db import Base, SessionLocal
from app.lead_outcomes import (
    LEAD_HANDOFF_METRIC,
    VERIFICATION_ACTION,
    reconcile_lead_handoff_goal,
    run_ceo_lead_outcome_cycle,
    verify_existing_management_company_candidate,
)
from app.models import AgentToolCall, BusinessGoal, BusinessRecord, OutboundMessage, OwnerNotification, Task


def _session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


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


def _management_company(title: str, website: str, email: str) -> BusinessRecord:
    return BusinessRecord(
        record_type="management_company",
        external_id=f"test:{title}",
        title=title,
        status="collected",
        source="test",
        data={
            "region": "Санкт-Петербург",
            "scope_status": "in_scope",
            "candidate_website": website,
            "internet_verification_status": "not_checked",
            "email": email,
            "emails": [email],
            "phone": "+78120000000",
            "phones": ["+78120000000"],
            "marketing_consent_status": "unknown",
        },
    )


def test_ceo_cycle_schedules_bounded_idempotent_crawl_work_and_daily_plan(
    monkeypatch,
):
    session_factory = _session_factory()
    now = datetime(2045, 6, 5, 12, 0)
    monkeypatch.setattr(lead_outcomes.settings, "lead_verification_batch_size", 8)
    with session_factory() as db:
        db.add(_goal())
        for index in range(12):
            db.add(
                _management_company(
                    f"УК Северный Дом {index}",
                    f"https://uk-{index}.example/",
                    f"info@uk-{index}.example",
                )
            )
        db.add(
            Task(
                title="Broken public search",
                agent_type="management_lead_scout",
                status="blocked",
                payload={"action": "discover_public_business_leads"},
                result={"status": "unavailable", "error": "401 Unauthorized"},
            )
        )
        db.flush()

        first = run_ceo_lead_outcome_cycle(db, cycle_key="2045-06-05T12:00", now=now)
        repeated = run_ceo_lead_outcome_cycle(db, cycle_key="2045-06-05T13:00", now=now)

        tasks = db.scalars(select(Task).where(Task.payload["action"].as_string() == VERIFICATION_ACTION)).all()
        assert first["status"] == "at_risk"
        assert first["goal"]["current"] == 0
        assert first["goal"]["shortfall"] == 20
        assert len(first["verification_work"]["tasks_created"]) == 8
        assert repeated["verification_work"]["tasks_created"] == []
        assert len(repeated["verification_work"]["tasks_reused"]) == 8
        assert len(tasks) == 8
        assert all(task.agent_type == "lead_coordinator" for task in tasks)
        assert all(task.payload["automatic_outreach"] is False for task in tasks)
        assert all(task.payload["read_only_tools"][0]["name"] == "web.public_crawl" for task in tasks)
        assert len(first["priorities"]) == 4
        assert first["priorities"][0]["accountable_agent"] == "system_admin"
        assert all(
            priority[key]
            for priority in first["priorities"]
            for key in ("accountable_agent", "metric", "deadline", "dependencies", "stop_condition")
        )
        assert all(priority["accountable_agent"] for priority in first["priorities"])
        assert first["provider_health"]["status"] == "unavailable"
        assert first["provider_recovery_task_id"] == repeated["provider_recovery_task_id"]
        recovery = db.get(Task, first["provider_recovery_task_id"])
        assert recovery is not None
        assert recovery.agent_type == "system_admin"
        assert recovery.priority == "critical"
        assert db.scalar(select(func.count()).select_from(OwnerNotification)) == 1
        assert db.scalar(select(func.count()).select_from(OutboundMessage)) == 0


def test_verified_management_company_becomes_owner_review_lead_without_outreach(monkeypatch, tmp_path):
    monkeypatch.setattr(lead_reports.settings, "document_storage_path", str(tmp_path))
    session_factory = _session_factory()
    with session_factory() as db:
        company = _management_company(
            "УК Северный Дом",
            "https://severny-dom.example/contacts",
            "info@severny-dom.example",
        )
        db.add(company)
        db.flush()

        result = verify_existing_management_company_candidate(
            db,
            {
                "record_id": company.id,
                "candidate_url": "https://severny-dom.example/contacts",
                "notify_owner": True,
                "automatic_outreach": False,
                "read_only_tool_results": [
                    {
                        "name": "web.public_crawl",
                        "result": {
                            "success": True,
                            "resolved_url": "https://severny-dom.example/contacts",
                            "markdown": "Официальный сайт УК Северный Дом. Контакты: info@severny-dom.example",
                            "content_sha256": "sha256:test",
                        },
                    }
                ],
            },
        )

        lead = db.get(BusinessRecord, result["lead_id"])
        assert result["status"] == "owner_review"
        assert lead is not None
        assert lead.status == "owner_review"
        assert lead.data["outreach_consent"] == "not_verified"
        assert lead.data["automatic_outreach"] is False
        assert company.data["internet_verification_status"] == "verified_public_website"
        assert result["instant_lead_report"]["status"] == "completed"
        assert result["instant_lead_report"]["notification_id"] is not None
        assert db.scalar(select(func.count()).select_from(OutboundMessage)) == 0


def test_verified_candidate_runs_through_audited_agent_tool_gateway(client, monkeypatch, tmp_path):
    monkeypatch.setattr(lead_reports.settings, "document_storage_path", str(tmp_path))
    monkeypatch.setattr(
        agent_tools,
        "crawl_public_page",
        lambda arguments: {
            "provider": "crawl4ai",
            "success": True,
            "resolved_url": arguments["url"],
            "markdown": "Официальный сайт УК Надёжный Дом. info@reliable.example",
            "content_sha256": "sha256:gateway-test",
            "untrusted_external_data": True,
            "automatic_action_allowed": False,
        },
    )
    with SessionLocal() as db:
        outbound_before = int(db.scalar(select(func.count()).select_from(OutboundMessage)) or 0)
        company = _management_company(
            "УК Надёжный Дом",
            "https://reliable.example/",
            "info@reliable.example",
        )
        db.add(company)
        db.commit()
        company_id = company.id

    task = client.post(
        "/api/tasks",
        json={
            "title": "Audited management company verification",
            "agent_type": "lead_coordinator",
            "max_attempts": 1,
            "payload": {
                "action": VERIFICATION_ACTION,
                "record_id": company_id,
                "candidate_url": "https://reliable.example/",
                "automatic_outreach": False,
                "read_only_tools": [
                    {
                        "name": "web.public_crawl",
                        "arguments": {"url": "https://reliable.example/", "max_chars": 8_000},
                    }
                ],
            },
        },
    ).json()
    completed = client.post(f"/api/tasks/{task['id']}/run").json()

    assert completed["status"] == "done"
    assert completed["result"]["status"] == "owner_review"
    assert completed["result"]["external_messages_sent"] is False
    with SessionLocal() as db:
        tool_call = db.scalar(
            select(AgentToolCall).where(AgentToolCall.task_id == task["id"])
        )
        assert tool_call is not None
        assert tool_call.tool_name == "web.public_crawl"
        assert tool_call.status == "succeeded"
        assert db.scalar(select(func.count()).select_from(OutboundMessage)) == outbound_before


def test_mismatched_public_page_does_not_create_a_lead():
    session_factory = _session_factory()
    with session_factory() as db:
        company = _management_company(
            "УК Дом",
            "https://unrelated.example/",
            "info@different-domain.example",
        )
        db.add(company)
        db.flush()

        payload = {
                "record_id": company.id,
                "candidate_url": "https://unrelated.example/",
                "read_only_tool_results": [
                    {
                        "name": "web.public_crawl",
                        "result": {
                            "success": True,
                            "resolved_url": "https://unrelated.example/",
                            "markdown": "Большой дом и совершенно другая организация.",
                            "content_sha256": "sha256:unrelated",
                        },
                    }
                ],
            }
        result = verify_existing_management_company_candidate(db, payload)

        assert result["status"] == "not_verified"
        assert result["reason"] == "organization_identity_not_confirmed"
        assert result["manual_review_required"] is False
        verify_existing_management_company_candidate(db, payload)
        third = verify_existing_management_company_candidate(db, payload)
        assert third["manual_review_required"] is True
        assert company.data["internet_verification_status"] == "needs_manual_review"
        assert company.data["internet_verification_attempts"] == 3
        assert db.scalar(
            select(func.count()).select_from(BusinessRecord).where(BusinessRecord.record_type == "lead")
        ) == 0
        assert db.scalar(select(func.count()).select_from(OutboundMessage)) == 0


def test_goal_counts_only_reports_actually_sent_to_owner_this_month():
    session_factory = _session_factory()
    now = datetime(2045, 6, 5, 12, 0)
    with session_factory() as db:
        goal = _goal()
        db.add(goal)
        db.flush()
        sent_leads = [
            BusinessRecord(
                record_type="lead",
                external_id=f"lead:{index}",
                title=f"Lead {index}",
                status="owner_review",
                source="perplexity_public_business_search",
                data={
                    "region": "Санкт-Петербург",
                    "organization_type": "business_center",
                    "contact_scope": "organization",
                    "public_phones": ["+78120000000"],
                    "source_urls": [f"https://lead-{index}.example/"],
                    "last_verified_at": now.isoformat(),
                },
            )
            for index in (101, 102)
        ]
        queued_lead = BusinessRecord(
            record_type="lead",
            external_id="lead:103",
            title="Lead 103",
            status="owner_review",
            data={},
        )
        db.add_all([*sent_leads, queued_lead])
        db.flush()
        sent_report = BusinessRecord(
            record_type="lead_discovery_report",
            external_id="report:sent",
            title="Sent",
            status="completed",
            data={"lead_ids": [row.id for row in sent_leads]},
        )
        queued_report = BusinessRecord(
            record_type="lead_discovery_report",
            external_id="report:queued",
            title="Queued",
            status="completed",
            data={"lead_ids": [queued_lead.id]},
        )
        db.add_all([sent_report, queued_report])
        db.flush()
        db.add_all(
            [
                OwnerNotification(
                    idempotency_key="sent",
                    channel="telegram",
                    resource_type="lead_discovery_report",
                    resource_id=str(sent_report.id),
                    status="sent",
                    sent_at=now,
                ),
                OwnerNotification(
                    idempotency_key="queued",
                    channel="telegram",
                    resource_type="lead_discovery_report",
                    resource_id=str(queued_report.id),
                    status="queued",
                ),
            ]
        )
        db.flush()

        result = reconcile_lead_handoff_goal(db, now=now)

        assert result["current"] == 2
        assert result["sent_report_count"] == 1
        assert goal.current == 2


def test_goal_rechecks_evidence_and_deduplicates_organizations_without_changing_lifecycle(monkeypatch):
    monkeypatch.setattr(lead_outcomes.settings, "management_contact_regions", "Санкт-Петербург|Ленинградская область")
    session_factory = _session_factory()
    now = datetime(2045, 6, 5, 12, 0)
    with session_factory() as db:
        goal = _goal()
        goal.current = 62
        db.add(goal)
        base = {
            "region": "Санкт-Петербург",
            "organization_type": "business_center",
            "contact_scope": "organization",
            "public_phones": ["+78120000000"],
            "source_urls": ["https://verified.example/contacts"],
            "last_verified_at": now.isoformat(),
            # Stored classifications must not override current source evidence.
            "qualification": {"outcome": "owner_review"},
        }
        changes = [
            {"inn": "1234567890"},
            {"inn": "1234567890"},
            {"region": "Москва"},
            {"source_urls": []},
            {"public_phones": []},
            {"last_verified_at": "2044-01-01T00:00:00"},
            {"organization_type": "unknown"},
            {"contact_scope": "person"},
        ]
        leads = [
            BusinessRecord(
                record_type="lead", external_id=f"evidence:{index}",
                title=f"Candidate {index}", status="owner_review",
                source="perplexity_public_business_search", data={**base, **change},
            )
            for index, change in enumerate(changes)
        ]
        db.add_all(leads)
        db.flush()
        report = BusinessRecord(
            record_type="lead_discovery_report", external_id="evidence:report",
            title="Delivered report", status="completed",
            data={"lead_ids": [lead.id for lead in leads] + [leads[0].id]},
        )
        db.add(report)
        db.flush()
        for suffix in ("first", "duplicate"):
            db.add(OwnerNotification(
                idempotency_key=f"evidence:{suffix}", channel="telegram",
                resource_type="lead_discovery_report", resource_id=str(report.id),
                status="sent", sent_at=now,
            ))
        db.flush()
        result = reconcile_lead_handoff_goal(db, now=now)
        assert result["current"] == 1
        assert result["excluded_unqualified_lead_count"] == 6
        assert result["sent_report_count"] == 1
        assert goal.current == 1
        assert all(lead.status == "owner_review" for lead in leads)
        assert reconcile_lead_handoff_goal(db, now=now)["current"] == 1
        assert db.scalar(select(func.count()).select_from(OutboundMessage)) == 0


def test_ceo_cycle_has_four_complete_priorities_with_a_healthy_provider():
    session_factory = _session_factory()
    with session_factory() as db:
        db.add(_goal())
        db.add(Task(
            title="Available provider", agent_type="management_lead_scout",
            status="done", payload={"action": "discover_public_business_leads"},
            result={"status": "completed"},
        ))
        db.flush()
        result = run_ceo_lead_outcome_cycle(db, cycle_key="healthy-plan")
        assert len(result["priorities"]) == 4
        assert [item["priority"] for item in result["priorities"]] == [1, 2, 3, 4]
        assert result["priorities"][0]["accountable_agent"] == "lead_scout"


def test_ceo_cycle_counts_a_guarded_tool_failure_once_for_candidate_retry_limit():
    session_factory = _session_factory()
    now = datetime(2045, 6, 5, 12, 0)
    with session_factory() as db:
        db.add(_goal())
        company = _management_company(
            "УК Некорректный Адрес",
            "https://invalid-candidate.example/",
            "info@invalid-candidate.example",
        )
        db.add(company)
        db.flush()
        failed = Task(
            title="Failed guarded candidate verification",
            agent_type="lead_coordinator",
            status="failed",
            payload={
                "action": VERIFICATION_ACTION,
                "record_id": company.id,
                "candidate_url": "https://invalid-candidate.example/",
            },
            result={"error_type": "AgentToolDenied"},
        )
        db.add(failed)
        db.flush()

        first = run_ceo_lead_outcome_cycle(db, cycle_key="2045-06-05T12:00", now=now)
        repeated = run_ceo_lead_outcome_cycle(db, cycle_key="2045-06-05T13:00", now=now)

        assert first["failed_verification_reconciliation"]["reconciled_task_ids"] == [failed.id]
        assert repeated["failed_verification_reconciliation"]["reconciled_task_ids"] == []
        assert company.data["internet_verification_attempts"] == 1
        assert company.data["internet_verification_last_failed_task_id"] == failed.id
        assert failed.result["resolution_status"] == "reconciled"
        assert failed.result["resolution_kind"] == "verification_candidate_retry_accounted"


def test_ceo_cycle_reconciles_each_failed_verification_task_once_for_same_candidate():
    session_factory = _session_factory()
    now = datetime(2045, 6, 5, 12, 0)
    with session_factory() as db:
        db.add(_goal())
        company = _management_company(
            "УК Два Сбоя",
            "https://two-failures.example/",
            "info@two-failures.example",
        )
        db.add(company)
        db.flush()
        first_failure = Task(
            title="First guarded candidate verification failure",
            agent_type="lead_coordinator",
            status="failed",
            payload={
                "action": VERIFICATION_ACTION,
                "record_id": company.id,
                "candidate_url": "https://two-failures.example/",
            },
            result={"error_type": "AgentToolDenied"},
        )
        db.add(first_failure)
        db.flush()

        run_ceo_lead_outcome_cycle(db, cycle_key="2045-06-05T12:00", now=now)
        second_failure = Task(
            title="Second guarded candidate verification failure",
            agent_type="lead_coordinator",
            status="failed",
            payload={
                "action": VERIFICATION_ACTION,
                "record_id": company.id,
                "candidate_url": "https://two-failures.example/",
            },
            result={"error_type": "AgentToolError"},
        )
        db.add(second_failure)
        db.flush()

        second = run_ceo_lead_outcome_cycle(db, cycle_key="2045-06-05T13:00", now=now)
        repeated = run_ceo_lead_outcome_cycle(db, cycle_key="2045-06-05T14:00", now=now)

        assert second["failed_verification_reconciliation"]["reconciled_task_ids"] == [
            second_failure.id
        ]
        assert repeated["failed_verification_reconciliation"]["reconciled_task_ids"] == []
        assert company.data["internet_verification_attempts"] == 2
        assert first_failure.result["resolution_status"] == "reconciled"
        assert second_failure.result["resolution_status"] == "reconciled"


def test_ceo_cycle_backfills_legacy_reconciliation_without_incrementing_attempts():
    session_factory = _session_factory()
    now = datetime(2045, 6, 5, 12, 0)
    with session_factory() as db:
        db.add(_goal())
        company = _management_company(
            "УК Старый Учёт",
            "https://legacy-failure.example/",
            "info@legacy-failure.example",
        )
        db.add(company)
        db.flush()
        failure = Task(
            title="Legacy reconciled verification failure",
            agent_type="lead_coordinator",
            status="failed",
            payload={
                "action": VERIFICATION_ACTION,
                "record_id": company.id,
                "candidate_url": "https://legacy-failure.example/",
            },
            result={"error_type": "AgentToolDenied"},
        )
        db.add(failure)
        db.flush()
        company.data = {
            **(company.data or {}),
            "internet_verification_attempts": 1,
            "internet_verification_last_failed_task_id": failure.id,
        }

        result = run_ceo_lead_outcome_cycle(
            db,
            cycle_key="2045-06-05T12:00",
            now=now,
        )

        reconciliation = result["failed_verification_reconciliation"]
        assert reconciliation["reconciled_task_ids"] == []
        assert reconciliation["backfilled_task_ids"] == [failure.id]
        assert company.data["internet_verification_attempts"] == 1
        assert failure.result["resolution_status"] == "reconciled"
