from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import ImprovementRequest, Task
from .task_state import RECONCILED_FAILURE_KINDS, RECONCILED_FAILURE_STATUS, task_waits_for_configuration


def task_backlog_counts(db: Session) -> dict[str, int]:
    """Keep immutable failure history separate from current recovery work."""
    counts = dict.fromkeys((
        "tasks_failed", "tasks_failed_actionable", "tasks_failed_reconciled",
        "tasks_blocked", "tasks_blocked_actionable", "tasks_waiting_configuration",
    ), 0)
    # One statement keeps the failed partition consistent under READ COMMITTED
    # while workers are transitioning tasks in other transactions.
    counts["tasks_failed"], counts["tasks_failed_reconciled"] = db.execute(
        select(func.count(Task.id), func.count(Task.id).filter(
            Task.result["resolution_status"].as_string() == RECONCILED_FAILURE_STATUS,
            Task.result["resolution_kind"].as_string().in_(RECONCILED_FAILURE_KINDS),
        )).where(Task.status == "failed")
    ).one()
    counts["tasks_failed_actionable"] = counts["tasks_failed"] - counts["tasks_failed_reconciled"]
    # Never hydrate terminal failure history or full task result/payload blobs on
    # the polling path. Current blocks need exact legacy classification, so read
    # only its evidence fields in bounded batches; do not hide old unresolved blocks.
    result_keys = ("status", "failure_category", "responsible_party", "improvement_id",
                   "execution_gap", "credentials_required")
    payload_keys = ("credentials_required", "required_credentials", "blocking_requirements")
    rows = db.execute(
        select(
            *(Task.result[key] for key in result_keys),
            *(Task.payload[key] for key in payload_keys),
        ).where(Task.status == "blocked")
        .execution_options(yield_per=100)
    )
    for row in rows:
        counts["tasks_blocked"] += 1
        task = Task(result=dict(zip(result_keys, row[:len(result_keys)], strict=True)),
                    payload=dict(zip(payload_keys, row[len(result_keys):], strict=True)))
        key = "tasks_waiting_configuration" if task_waits_for_configuration(task) else "tasks_blocked_actionable"
        counts[key] += 1
    return counts


def task_backlog_rows(summary: Mapping[str, Any]) -> list[tuple[str, int]]:
    """Do not invent an actionable classification for older stored snapshots."""
    fields = (
        ("Необработанных ошибок", "tasks_failed_actionable"),
        ("Обработанных ошибок в истории", "tasks_failed_reconciled"),
        ("Операционных блокировок", "tasks_blocked_actionable"),
        ("Ожидают настройки или доступа", "tasks_waiting_configuration"),
    )
    if all(key in summary for _, key in fields):
        return [(label, int(summary[key])) for label, key in fields]
    return [
        (label, int(summary[key]))
        for label, key in (
            ("Ошибок в истории (без классификации)", "tasks_failed"),
            ("Блокировок (без классификации)", "tasks_blocked"),
        )
        if key in summary
    ]


def improvement_backlog_counts(db: Session) -> dict[str, int]:
    """Count proposal provenance, not provider failures or completed features."""
    counts = dict.fromkeys((
        "queued_improvements", "queued_improvements_perplexity",
        "queued_improvements_research", "queued_improvements_telegram",
        "queued_improvements_other", "queued_improvements_configuration",
        "queued_improvements_development",
    ), 0)
    rows = db.execute(
        select(ImprovementRequest.source_user, ImprovementRequest.source_channel,
               ImprovementRequest.classification)
        .where(ImprovementRequest.status == "queued")
        .execution_options(yield_per=100)
    )
    for source_user, source_channel, classification in rows:
        counts["queued_improvements"] += 1
        if source_user == "perplexity_agent_coach":
            source = "perplexity"
        elif source_user == "github_evolution_researcher":
            source = "research"
        elif source_channel == "telegram":
            source = "telegram"
        else:
            source = "other"
        counts[f"queued_improvements_{source}"] += 1
        category = "configuration" if classification == "configuration_required" else "development"
        counts[f"queued_improvements_{category}"] += 1
    return counts
