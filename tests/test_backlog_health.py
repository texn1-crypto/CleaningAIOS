import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.backlog_health import improvement_backlog_counts, task_backlog_counts
from app.db import Base
from app.main import dashboard
from app.mission_control import MISSION_CONTROL_HTML
from app.models import ImprovementRequest, Task
from app.reports import build_activity_report, format_activity_report
from app.task_state import task_waits_for_configuration


def _session():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False)()


def test_task_counts_keep_history_and_unknown_failures_without_writes():
    with _session() as db:
        db.add_all([
            Task(title="Accounted verification", status="failed", result={
                "resolution_status": "reconciled",
                "resolution_kind": "verification_candidate_retry_accounted",
            }),
            Task(title="Unproved resolution", status="failed", result={"resolution_status": "reconciled"}),
            Task(title="Auth", status="blocked", result={"credentials_required": ["PERPLEXITY_API_KEY"]}),
            Task(title="Provider 503", status="blocked", result={
                "status": "unavailable", "reason": "HTTP 503",
                "handoff_status": "credentials_required", "responsible_party": "system_codex",
            }),
        ])
        db.commit()
        before = [(t.id, t.status, dict(t.result)) for t in db.scalars(select(Task))]
        counts = task_backlog_counts(db)
        assert counts == {
            "tasks_failed": 2, "tasks_failed_actionable": 1, "tasks_failed_reconciled": 1,
            "tasks_blocked": 2, "tasks_blocked_actionable": 1, "tasks_waiting_configuration": 1,
        }
        assert not db.dirty and not db.new
        assert before == [(t.id, t.status, dict(t.result)) for t in db.scalars(select(Task))]


def test_polling_counts_do_not_hydrate_growing_failure_history():
    with _session() as db:
        db.add_all(Task(title=f"Historical failure {index}", status="failed", result={
            "resolution_status": "reconciled", "resolution_kind": "verification_candidate_retry_accounted",
            "irrelevant_history": "x" * 2000,
        }) for index in range(3000))
        db.add(Task(title="Old unresolved block", status="blocked", created_at=datetime(2020, 1, 1),
                    result={"status": "unavailable", "irrelevant_history": "x" * 100_000}))
        db.commit()
        db.expunge_all()
        loaded = []
        statements = []
        event.listen(db, "loaded_as_persistent", lambda session, instance: loaded.append(instance))
        event.listen(db.get_bind(), "before_cursor_execute",
                     lambda conn, cursor, statement, parameters, context, many: statements.append(statement))
        counts = task_backlog_counts(db)
        assert counts["tasks_failed_reconciled"] == 3000
        assert counts["tasks_failed_actionable"] == 0
        assert counts["tasks_blocked_actionable"] == 1
        assert loaded == []
        assert len(statements) == 2
        assert statements[0].lower().count("count(") == 2
        assert counts["tasks_failed"] == counts["tasks_failed_actionable"] + counts["tasks_failed_reconciled"]
        assert counts["tasks_blocked"] == counts["tasks_blocked_actionable"] + counts["tasks_waiting_configuration"]
        assert not db.dirty and not db.new


def test_handoff_credentials_never_mask_a_business_failure():
    for responsible_party in ("system_codex", "owner_configuration", ""):
        task = Task(status="blocked", result={
            "status": "unavailable", "handoff_status": "credentials_required",
            "responsible_party": responsible_party,
        })
        assert task_waits_for_configuration(task) is False
        task.result = {**task.result, "credentials_required": ["PERPLEXITY_API_KEY"]}
        assert task_waits_for_configuration(task) is True


def test_explicit_and_legacy_configuration_evidence_still_classified():
    assert task_waits_for_configuration(Task(result={"status": "credentials_required"}))
    assert task_waits_for_configuration(Task(payload={"blocking_requirements": ["TENDER_SOURCE_TOKEN"]}))
    assert not task_waits_for_configuration(Task(payload={"blocking_requirements": ["owner approval"]}))
    legacy = {"status": "unavailable", "responsible_party": "owner_configuration",
              "improvement_id": 91, "execution_gap": "HTTPStatusError: 401 Unauthorized"}
    assert task_waits_for_configuration(Task(result=legacy))
    assert not task_waits_for_configuration(Task(result={**legacy, "responsible_party": "system_codex"}))
    assert not task_waits_for_configuration(Task(result={**legacy, "improvement_id": None}))
    assert not task_waits_for_configuration(Task(result={**legacy, "improvement_id": True}))


def test_execution_gap_persists_business_failure_category_separately(monkeypatch):
    from app import improvements

    monkeypatch.setattr(improvements, "retry_workspace_handoff", lambda row: {"status": "credentials_required"})
    with _session() as db:
        task = Task(title="Provider unavailable", agent_type="research", status="running")
        db.add(task)
        db.flush()
        for requires_credentials in (False, True):
            result = improvements.record_execution_gap(
                db, task, "HTTP 401" if requires_credentials else "HTTP 503",
                credentials_required=requires_credentials,
            )
            assert result["handoff_status"] == "credentials_required"
            assert result["failure_category"] == ("credentials_required" if requires_credentials else "execution_gap")
            assert task_waits_for_configuration(Task(result=result)) is requires_credentials


def test_proposal_sources_partition_overlapping_channels_and_separate_configuration():
    with _session() as db:
        for key, user, channel, classification, status in [
            ("coach", "perplexity_agent_coach", "telegram", "agent_quality_gap", "queued"),
            ("github", "github_evolution_researcher", "system", "source_grounded_improvement", "queued"),
            ("owner", "owner", "telegram", "capability_gap", "queued"),
            ("config", "system_admin", "system", "configuration_required", "queued"),
            ("done", "system_admin", "system", "configuration_required", "implemented"),
        ]:
            db.add(ImprovementRequest(dedup_key=key, request_text=key, codex_prompt=key,
                source_user=user, source_channel=channel, classification=classification, status=status))
        db.commit()
        counts = improvement_backlog_counts(db)
        assert counts["queued_improvements"] == 4
        assert counts["queued_improvements_configuration"] == 1
        assert counts["queued_improvements_development"] == 3
        assert all(counts[f"queued_improvements_{source}"] == 1 for source in ("perplexity", "research", "telegram", "other"))
        assert not db.dirty and not db.new


def test_activity_report_and_dashboard_do_not_reopen_reconciled_failures():
    with _session() as db:
        task = Task(title="Accounted failure", status="failed", updated_at=datetime.now(timezone.utc).replace(tzinfo=None),
            result={"resolution_status": "reconciled", "resolution_kind": "verification_candidate_retry_accounted"})
        db.add(task)
        db.commit()
        report = build_activity_report(db, period_minutes=120)
        assert report["summary"]["tasks_failed"] == 1
        assert report["summary"]["tasks_failed_actionable"] == 0
        assert not any("ошиб" in item for item in report["blockers"])
        rendered = format_activity_report(report)
        assert "Необработанных ошибок: 0" in rendered
        assert "Обработанных ошибок в истории: 1" in rendered
        assert "источники, не ошибки сервисов" in rendered
        overview = dashboard(db=db)
        assert overview["failed_tasks"] == 1
        assert overview["company_health"] == 100
        assert overview["task_backlog"]["tasks_failed_reconciled"] == 1
        assert task.status == "failed"


def test_old_stored_reports_remain_readable_without_new_fields():
    rendered = format_activity_report({"summary": {"tasks_failed": 4, "tasks_blocked": 2, "queued_improvements": 8}})
    assert "Ошибок в истории (без классификации): 4" in rendered
    assert "Блокировок (без классификации): 2" in rendered
    assert "Классификация предложений в этом сохранённом отчёте отсутствует" in rendered
    assert "Необработанных ошибок" not in rendered
    assert "настройка доступа 0" not in rendered
    assert "разбор и разработка 8" not in rendered


def test_telegram_dashboard_shows_current_and_historical_counts(monkeypatch):
    from app import bot

    data = {"company_health": 100, "open_tasks": 0, "pending_decisions": 0,
            "pending_approvals": 0, "failed_tasks": 114, "task_backlog": {
                "tasks_failed_actionable": 0, "tasks_failed_reconciled": 114,
                "tasks_blocked_actionable": 5, "tasks_waiting_configuration": 90,
            }}
    api = AsyncMock(return_value=data)
    monkeypatch.setattr(bot, "api", api)
    reply = AsyncMock()
    update = SimpleNamespace(effective_message=SimpleNamespace(reply_text=reply))
    asyncio.run(bot.dashboard(update, None))
    api.assert_awaited_once_with("GET", "/api/dashboard")
    text = reply.call_args.args[0]
    assert "Необработанных ошибок: 0" in text
    assert "Обработанных ошибок в истории: 114" in text
    assert "Операционных блокировок: 5" in text
    assert "Ожидают настройки или доступа: 90" in text
    assert "не SLO" in text
    del data["task_backlog"]
    asyncio.run(bot.dashboard(update, None))
    text = reply.call_args.args[0]
    assert "Ошибок в истории (без классификации): 114" in text
    assert "Необработанных ошибок" not in text


def test_mission_control_only_fetches_one_task_page():
    assert "get('/api/tasks?limit=30')" in MISSION_CONTROL_HTML
    assert "get('/api/tasks')" not in MISSION_CONTROL_HTML
    assert "backlogCards(d)" in MISSION_CONTROL_HTML
    for key in ("tasks_failed_actionable", "tasks_failed_reconciled",
                "tasks_blocked_actionable", "tasks_waiting_configuration"):
        assert key in MISSION_CONTROL_HTML
