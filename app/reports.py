from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from .chat import redact_sensitive_text
from .config import settings
from .models import (
    AgentRun,
    AgentState,
    ApprovalRequest,
    AuditLog,
    BusinessGoal,
    BusinessRecord,
    ContentItem,
    DomainEvent,
    ImprovementRequest,
    MediaAsset,
    OwnerNotification,
    Task,
    TaskTransition,
)
from .money_opportunities import build_money_opportunities
from .notifications import notification_failure_is_active
from .operations import goal_progress
from .readiness import integration_status
from .growth import growth_snapshot
from .task_state import task_failure_is_reconciled, task_waits_for_configuration


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _count(db: Session, model: type, *criteria: Any) -> int:
    return int(db.scalar(select(func.count()).select_from(model).where(*criteria)) or 0)


def _short_text(value: object, limit: int = 120) -> str:
    compact = " ".join(redact_sensitive_text(str(value or "")).split())
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"


def _alert_thread_key(row: OwnerNotification) -> tuple[str, str, str]:
    data = row.data or {}
    if row.resource_type in {"system_admin_report", "weekly_ceo_brief"}:
        return row.resource_type, "current_snapshot", ""
    approval_id = data.get("approval_id")
    if approval_id not in (None, ""):
        return "approval", str(approval_id), ""
    return (
        str(row.resource_type or "notification"),
        str(row.resource_id or row.id),
        str(data.get("event_type") or ""),
    )


def _alert_is_actionable(
    db: Session,
    row: OwnerNotification,
    *,
    now: datetime,
    latest_sent_by_channel: dict[str, datetime],
) -> bool:
    if row.status in {"retry", "dead_letter", "waiting_configuration"} and not (
        notification_failure_is_active(
            db,
            row,
            now=now,
            latest_sent_at=latest_sent_by_channel.get(str(row.channel or "unknown")),
        )
    ):
        return False
    data = row.data or {}
    approval_id = data.get("approval_id")
    if approval_id not in (None, ""):
        try:
            approval = db.get(ApprovalRequest, int(str(approval_id)))
        except (TypeError, ValueError):
            return True
        return bool(
            approval
            and approval.status == "pending"
            and (approval.expires_at is None or approval.expires_at > now)
        )
    if row.resource_type != "task":
        return True
    try:
        task = db.get(Task, int(row.resource_id))
    except (TypeError, ValueError):
        return True
    if task is None:
        return True
    if task.status == "failed":
        return not task_failure_is_reconciled(task)
    if task.status == "blocked":
        return not task_waits_for_configuration(task)
    return False


def _current_alerts(
    db: Session,
    rows: list[OwnerNotification],
    *,
    now: datetime,
    latest_sent_by_channel: dict[str, datetime],
) -> list[OwnerNotification]:
    latest_by_thread: dict[tuple[str, str, str], OwnerNotification] = {}
    for row in rows:
        latest_by_thread.setdefault(_alert_thread_key(row), row)
    return [
        row
        for row in latest_by_thread.values()
        if _alert_is_actionable(
            db,
            row,
            now=now,
            latest_sent_by_channel=latest_sent_by_channel,
        )
    ]


def _social_runtime_activity(db: Session, *, cutoff: datetime) -> list[dict[str, Any]]:
    published = _count(
        db,
        ContentItem,
        ContentItem.status == "published",
        ContentItem.published_at >= cutoff,
    )
    social_statuses = dict(
        db.execute(
            select(ContentItem.status, func.count(ContentItem.id))
            .where(ContentItem.status != "published")
            .group_by(ContentItem.status)
        ).all()
    )
    blockers = [
        ("approval", "ожидают подтверждения владельца"),
        ("visual_pending", "ожидают готовых изображений"),
        ("credentials_required", "требуют credentials площадки"),
        ("adapter_required", "требуют официального адаптера"),
        ("publication_failed", "завершились ошибкой публикации"),
        ("reconciliation_required", "требуют проверки результата площадки"),
    ]
    publication_reasons = [
        f"{int(social_statuses.get(status, 0))} {label}"
        for status, label in blockers
        if social_statuses.get(status)
    ]
    social_publisher = {
        "agent_type": "social_publisher",
        "status": "worked" if published else "blocked" if publication_reasons else "idle",
        "runs": published,
        "succeeded": published,
        "failed": 0,
        "running": 0,
        "active_tasks": 0,
        "last_task_id": None,
        "last_task_title": "",
        "last_heartbeat_at": None,
        "did_work": bool(published),
        "inactivity_reason": "" if published else "; ".join(publication_reasons) or "нет подготовленных публикаций",
    }

    generated = _count(
        db,
        AuditLog,
        AuditLog.actor == "social_image_agent",
        AuditLog.action == "marketing.social_visual_generated",
        AuditLog.created_at >= cutoff,
    )
    generation_failures = _count(
        db,
        AuditLog,
        AuditLog.actor == "social_image_agent",
        AuditLog.action == "marketing.social_visual_failed",
        AuditLog.created_at >= cutoff,
    )
    media_statuses = dict(
        db.execute(
            select(MediaAsset.status, func.count(MediaAsset.id))
            .where(MediaAsset.kind == "image")
            .group_by(MediaAsset.status)
        ).all()
    )
    if generated:
        image_reason = ""
    elif media_statuses.get("credentials_required"):
        image_reason = (
            f"{int(media_statuses['credentials_required'])} визуалов требуют IMAGE_GENERATION_API_KEY "
            "или нового уникального media pool"
        )
    elif media_statuses.get("queued"):
        image_reason = f"{int(media_statuses['queued'])} визуалов ожидают обработки worker"
    else:
        image_reason = "за период не было новых запросов на изображения"
    social_image = {
        "agent_type": "social_image",
        "status": "worked" if generated else "blocked" if media_statuses.get("credentials_required") else "idle",
        "runs": generated + generation_failures,
        "succeeded": generated,
        "failed": generation_failures,
        "running": int(media_statuses.get("queued", 0)),
        "active_tasks": int(media_statuses.get("queued", 0)),
        "last_task_id": None,
        "last_task_title": "",
        "last_heartbeat_at": None,
        "did_work": bool(generated),
        "inactivity_reason": image_reason,
    }
    return [social_image, social_publisher]


def _agent_activity(db: Session, *, cutoff: datetime) -> list[dict[str, Any]]:
    from .agents import AGENTS

    states = {
        row.agent_type: row
        for row in db.scalars(select(AgentState)).all()
        if row.agent_type in AGENTS
    }
    runs = db.scalars(
        select(AgentRun)
        .where(AgentRun.started_at >= cutoff)
        .order_by(AgentRun.started_at.desc(), AgentRun.id.desc())
    ).all()
    runs_by_agent: dict[str, list[AgentRun]] = {name: [] for name in AGENTS}
    for run in runs:
        if run.agent_type in runs_by_agent:
            runs_by_agent[run.agent_type].append(run)
    task_ids = {run.task_id for run in runs if run.task_id is not None}
    tasks_by_id = (
        {
            row.id: row
            for row in db.scalars(select(Task).where(Task.id.in_(task_ids))).all()
        }
        if task_ids
        else {}
    )
    active_tasks = db.scalars(
        select(Task)
        .where(Task.status.in_(["open", "queued", "running"]))
        .order_by(Task.id)
    ).all()
    active_by_agent: dict[str, list[Task]] = {name: [] for name in AGENTS}
    for task in active_tasks:
        if task.agent_type in active_by_agent:
            active_by_agent[task.agent_type].append(task)

    rows: list[dict[str, Any]] = []
    for agent_type in sorted(AGENTS):
        agent_runs = runs_by_agent[agent_type]
        succeeded = sum(row.status == "succeeded" for row in agent_runs)
        failed = sum(row.status in {"failed", "incomplete"} for row in agent_runs)
        running = sum(row.status == "running" for row in agent_runs)
        latest_run = agent_runs[0] if agent_runs else None
        latest_task = tasks_by_id.get(latest_run.task_id) if latest_run else None
        waiting = active_by_agent[agent_type]
        state = states.get(agent_type)
        if succeeded or failed:
            inactivity_reason = ""
            status = "worked" if not failed else "degraded"
        elif running:
            inactivity_reason = ""
            status = "running"
        elif waiting:
            inactivity_reason = f"задача #{waiting[0].id} ожидает выполнения worker"
            status = "waiting"
        elif state and state.last_error:
            inactivity_reason = "последняя ошибка: " + _short_text(state.last_error, 160)
            status = "error"
        else:
            inactivity_reason = "за отчётный период агенту не назначались задачи"
            status = "idle"
        rows.append(
            {
                "agent_type": agent_type,
                "status": status,
                "runs": len(agent_runs),
                "succeeded": succeeded,
                "failed": failed,
                "running": running,
                "active_tasks": len(waiting),
                "last_task_id": latest_task.id if latest_task else None,
                "last_task_title": _short_text(latest_task.title, 100) if latest_task else "",
                "last_heartbeat_at": (
                    state.last_heartbeat_at.isoformat()
                    if state and state.last_heartbeat_at
                    else None
                ),
                "did_work": bool(succeeded or failed),
                "inactivity_reason": inactivity_reason,
            }
        )
    rows.extend(_social_runtime_activity(db, cutoff=cutoff))
    return sorted(rows, key=lambda row: str(row["agent_type"]))


def _latest_timestamp(db: Session, column: Any) -> str | None:
    value = db.scalar(select(func.max(column)))
    return value.isoformat() if isinstance(value, datetime) else None


def _integration_summary() -> dict[str, str]:
    summary: dict[str, str] = {}

    def collect(prefix: str, value: object) -> None:
        if not isinstance(value, dict):
            return
        status = value.get("status")
        if isinstance(status, str):
            summary[prefix] = status
        for key, nested in value.items():
            if key == "status":
                continue
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(nested, dict):
                collect(path, nested)
            elif not isinstance(status, str) and isinstance(nested, str):
                summary[path] = nested

    for integration, details in integration_status().items():
        collect(str(integration), details)
    return dict(sorted(summary.items()))


def _weekly_ceo_plan(
    *,
    generated_at: datetime,
    goals: list[BusinessGoal],
    sales: dict[str, Any],
    tenders: dict[str, Any],
    growth: dict[str, Any],
    failed_tasks: list[Task],
    blocked_tasks: list[Task],
    failed_task_count: int,
    blocked_task_count: int,
    credential_statuses: dict[str, str],
    hot_lead_ids: list[int],
) -> list[dict[str, Any]]:
    deadline = (generated_at + timedelta(days=7)).date().isoformat()
    priorities: list[dict[str, Any]] = []
    lead_goal = next(
        (row for row in goals if row.metric == "qualified_owner_handoffs"),
        None,
    )
    if lead_goal is not None and lead_goal.current < lead_goal.target:
        priorities.append(
            {
                "rank": 1,
                "owner_agent": lead_goal.owner or "lead_coordinator",
                "action": "increase_verified_owner_handoffs",
                "outcome": "Передать владельцу новые проверенные коммерческие лиды.",
                "metric": lead_goal.metric,
                "baseline": lead_goal.current,
                "target": min(lead_goal.target, lead_goal.current + 5),
                "unit": lead_goal.unit,
                "deadline": deadline,
                "source_endpoint": "/api/goals",
                "source_ids": [lead_goal.id],
                "requires_owner_approval": False,
                "automatic_external_action": False,
            }
        )
    elif lead_goal is None:
        priorities.append(
            {
                "rank": 1,
                "owner_agent": "ceo",
                "action": "configure_verified_owner_handoff_goal",
                "outcome": "Зафиксировать месячную цель по проверенным передачам лидов.",
                "metric": "qualified_owner_handoffs",
                "baseline": 0,
                "target": 20,
                "unit": "leads/month",
                "deadline": deadline,
                "source_endpoint": "/api/goals",
                "source_ids": [],
                "requires_owner_approval": False,
                "automatic_external_action": False,
            }
        )

    overdue_next_actions = int(sales.get("overdue_next_actions") or 0)
    if overdue_next_actions:
        priorities.append(
            {
                "rank": len(priorities) + 1,
                "owner_agent": "sales",
                "action": "clear_overdue_lead_next_actions",
                "outcome": "Разобрать просроченные следующие шаги по лидам без автоматической рассылки.",
                "metric": "overdue_next_actions",
                "baseline": overdue_next_actions,
                "target": 0,
                "unit": "leads",
                "deadline": deadline,
                "source_endpoint": "/api/records?record_type=lead",
                "source_ids": hot_lead_ids,
                "requires_owner_approval": False,
                "automatic_external_action": False,
            }
        )

    unhealthy_tasks = failed_tasks + blocked_tasks
    unhealthy_task_count = failed_task_count + blocked_task_count
    if unhealthy_task_count:
        priorities.append(
            {
                "rank": len(priorities) + 1,
                "owner_agent": "system_admin",
                "action": "recover_failed_or_blocked_work",
                "outcome": "Устранить подтверждённые причины сбоев и блокировок задач.",
                "metric": "failed_and_blocked_tasks",
                "baseline": unhealthy_task_count,
                "target": 0,
                "unit": "tasks",
                "deadline": deadline,
                "source_endpoint": "/api/tasks",
                "source_ids": [row.id for row in unhealthy_tasks[:20]],
                "requires_owner_approval": False,
                "automatic_external_action": False,
            }
        )

    if growth.get("status") == "behind_plan":
        priorities.append(
            {
                "rank": len(priorities) + 1,
                "owner_agent": "growth_officer",
                "action": "recover_revenue_run_rate_pace",
                "outcome": "Подготовить измеримый план прироста активной месячной выручки.",
                "metric": "annual_revenue_run_rate_rub",
                "baseline": growth.get("current_rub", 0),
                "target": growth.get("expected_today_rub", 0),
                "unit": "RUB/year",
                "deadline": deadline,
                "source_endpoint": "/api/goals",
                "source_ids": [growth["goal_id"]] if growth.get("goal_id") else [],
                "requires_owner_approval": False,
                "automatic_external_action": False,
            }
        )

    ready_tenders = int(tenders.get("ready_for_owner_review") or 0)
    if ready_tenders:
        priorities.append(
            {
                "rank": len(priorities) + 1,
                "owner_agent": "tender",
                "action": "prepare_tender_owner_reviews",
                "outcome": "Подготовить доказательства и экономику тендеров, готовых к решению владельца.",
                "metric": "tenders_ready_for_owner_review",
                "baseline": ready_tenders,
                "target": 0,
                "unit": "tenders_pending_review",
                "deadline": deadline,
                "source_endpoint": "/api/money-opportunities",
                "source_ids": [],
                "requires_owner_approval": True,
                "automatic_external_action": False,
            }
        )

    missing_credentials = sorted(
        key
        for key, value in credential_statuses.items()
        if value in {"credentials_required", "mailbox_configuration_required", "source_configuration_required"}
    )
    if missing_credentials and len(priorities) < 5:
        priorities.append(
            {
                "rank": len(priorities) + 1,
                "owner_agent": "system_admin",
                "action": "prepare_external_integration_recovery",
                "outcome": "Показать владельцу точный список недостающих авторизаций без передачи секретов агентам.",
                "metric": "blocked_integrations",
                "baseline": len(missing_credentials),
                "target": 0,
                "unit": "integrations",
                "deadline": deadline,
                "source_endpoint": "/api/integrations",
                "source_ids": [],
                "blocked_integrations": missing_credentials,
                "requires_owner_approval": True,
                "automatic_external_action": False,
            }
        )

    if not priorities:
        priorities.append(
            {
                "rank": 1,
                "owner_agent": "lead_coordinator",
                "action": "maintain_verified_lead_flow",
                "outcome": "Сохранить недельный поток проверенных лидов и передач владельцу.",
                "metric": "qualified_leads",
                "baseline": int(sales.get("qualified") or 0),
                "target": int(sales.get("qualified") or 0) + 5,
                "unit": "leads",
                "deadline": deadline,
                "source_endpoint": "/api/records?record_type=lead",
                "source_ids": hot_lead_ids,
                "requires_owner_approval": False,
                "automatic_external_action": False,
            }
        )
    return priorities[:5]


def build_ceo_brief(
    db: Session,
    *,
    generated_at: datetime | None = None,
    period_days: int = 7,
) -> dict[str, Any]:
    """Build a cross-domain, source-linked brief without using an LLM as a fact source."""
    generated_at = generated_at or _utcnow()
    if generated_at.tzinfo is not None:
        generated_at = generated_at.astimezone(timezone.utc).replace(tzinfo=None)
    period_days = max(1, min(int(period_days), 31))
    period_start = generated_at - timedelta(days=period_days)
    all_active_tasks = list(db.scalars(
        select(Task)
        .where(Task.status.in_(["open", "queued", "running"]))
        .order_by(Task.priority.desc(), Task.id.desc())
    ).all())
    active_tasks = all_active_tasks[:20]
    scheduled_active = [
        row
        for row in all_active_tasks
        if (
            row.status in {"open", "queued"}
            and row.run_after is not None
            and row.run_after > generated_at
        )
    ]
    runnable_active = [
        row
        for row in all_active_tasks
        if row.status == "running" or row.run_after is None or row.run_after <= generated_at
    ]
    scheduled_active_tasks = scheduled_active[:20]
    runnable_active_tasks = runnable_active[:20]
    all_failed_tasks = list(db.scalars(
        select(Task).where(Task.status == "failed").order_by(Task.id.desc())
    ).all())
    failed_tasks = all_failed_tasks[:20]
    reconciled_failed = [
        row for row in all_failed_tasks if task_failure_is_reconciled(row)
    ]
    actionable_failed = [
        row for row in all_failed_tasks if not task_failure_is_reconciled(row)
    ]
    reconciled_failed_tasks = reconciled_failed[:20]
    actionable_failed_tasks = actionable_failed[:20]
    all_blocked_tasks = list(db.scalars(
        select(Task).where(Task.status == "blocked").order_by(Task.id.desc())
    ).all())
    blocked_tasks = all_blocked_tasks[:20]
    waiting_configuration = [
        row for row in all_blocked_tasks if task_waits_for_configuration(row)
    ]
    actionable_blocked = [
        row for row in all_blocked_tasks if not task_waits_for_configuration(row)
    ]
    waiting_configuration_tasks = waiting_configuration[:20]
    actionable_blocked_tasks = actionable_blocked[:20]
    all_pending_approvals = list(db.scalars(
        select(ApprovalRequest)
        .where(ApprovalRequest.status == "pending")
        .order_by(ApprovalRequest.id.desc())
    ).all())
    current_pending_approvals = [
        row
        for row in all_pending_approvals
        if row.expires_at is None or row.expires_at > generated_at
    ]
    expired_pending_approvals = [
        row
        for row in all_pending_approvals
        if row.expires_at is not None and row.expires_at <= generated_at
    ]
    approvals = current_pending_approvals[:20]
    alert_scan_limit = 5000
    all_alert_rows = list(db.scalars(
        select(OwnerNotification)
        .where(
            OwnerNotification.severity.in_(["high", "critical"]),
            OwnerNotification.acknowledged_at.is_(None),
        )
        .order_by(OwnerNotification.id.desc())
        .limit(alert_scan_limit)
    ).all())
    latest_sent_by_channel = {
        str(channel): sent_at
        for channel, sent_at in db.execute(
            select(
                OwnerNotification.channel,
                func.max(OwnerNotification.sent_at),
            )
            .where(OwnerNotification.status == "sent")
            .group_by(OwnerNotification.channel)
        ).all()
        if sent_at is not None
    }
    actionable_alerts = _current_alerts(
        db,
        all_alert_rows,
        now=generated_at,
        latest_sent_by_channel=latest_sent_by_channel,
    )
    alerts = actionable_alerts[:20]
    all_dead_letter_count = _count(
        db,
        OwnerNotification,
        OwnerNotification.status == "dead_letter",
    )
    dead_letter_rows = list(db.scalars(
        select(OwnerNotification)
        .where(OwnerNotification.status == "dead_letter")
        .order_by(OwnerNotification.id.desc())
        .limit(alert_scan_limit)
    ).all())
    actionable_dead_letters = [
        row
        for row in dead_letter_rows
        if notification_failure_is_active(
            db,
            row,
            now=generated_at,
            latest_sent_at=latest_sent_by_channel.get(str(row.channel or "unknown")),
        )
    ]
    goals = list(db.scalars(
        select(BusinessGoal)
        .where(BusinessGoal.status == "active")
        .order_by(BusinessGoal.id.desc())
        .limit(20)
    ).all())
    overdue_payments = list(db.scalars(
        select(BusinessRecord)
        .where(
            BusinessRecord.record_type == "payment",
            BusinessRecord.status == "overdue",
        )
        .order_by(BusinessRecord.id.desc())
    ).all())

    active_task_count = len(all_active_tasks)
    scheduled_active_count = len(scheduled_active)
    runnable_active_count = len(runnable_active)
    failed_task_count = len(all_failed_tasks)
    reconciled_failed_count = len(reconciled_failed)
    actionable_failed_count = len(actionable_failed)
    blocked_task_count = _count(db, Task, Task.status == "blocked")
    waiting_configuration_count = len(waiting_configuration)
    actionable_blocked_count = len(actionable_blocked)
    pending_approval_count = len(current_pending_approvals)
    pending_approval_total_count = len(all_pending_approvals)
    expired_pending_approval_count = len(expired_pending_approvals)
    unacknowledged_alert_count = _count(
        db,
        OwnerNotification,
        OwnerNotification.severity.in_(["high", "critical"]),
        OwnerNotification.acknowledged_at.is_(None),
    )
    historical_alert_count = max(0, len(all_alert_rows) - len(actionable_alerts))

    opportunities = build_money_opportunities(db)
    sales_lane = opportunities.get("sales") or {}
    sales_summary = dict(sales_lane.get("summary") or {})
    tender_summary = dict((opportunities.get("tenders") or {}).get("summary") or {})
    marketing_summary = dict((opportunities.get("marketing") or {}).get("summary") or {})
    credential_statuses = {
        **dict(opportunities.get("credentials") or {}),
        **_integration_summary(),
    }
    hot_lead_ids = [
        int(row["record_id"])
        for row in (sales_lane.get("hot_leads") or [])
        if isinstance(row, dict) and str(row.get("record_id") or "").isdigit()
    ][:20]
    growth = growth_snapshot(db, now=generated_at)
    agent_activity = _agent_activity(db, cutoff=period_start)
    runtime_agent_activity = [
        row
        for row in agent_activity
        if row.get("agent_type") not in {"social_image", "social_publisher"}
    ]
    agent_runs = sum(int(row.get("runs") or 0) for row in runtime_agent_activity)
    agent_succeeded = sum(
        int(row.get("succeeded") or 0) for row in runtime_agent_activity
    )
    agent_failed = sum(int(row.get("failed") or 0) for row in runtime_agent_activity)
    finished_runs = agent_succeeded + agent_failed
    new_leads = _count(
        db,
        BusinessRecord,
        BusinessRecord.record_type == "lead",
        BusinessRecord.created_at >= period_start,
    )
    workflow_created = _count(db, Task, Task.created_at >= period_start)
    workflow_done = int(
        db.scalar(
            select(func.count(func.distinct(TaskTransition.task_id))).where(
                TaskTransition.to_status == "done",
                TaskTransition.created_at >= period_start,
            )
        )
        or 0
    )
    workflow_failed = int(
        db.scalar(
            select(func.count(func.distinct(TaskTransition.task_id))).where(
                TaskTransition.to_status == "failed",
                TaskTransition.created_at >= period_start,
            )
        )
        or 0
    )

    facts = {
        "tasks": {
            "active": active_task_count,
            "runnable_active": runnable_active_count,
            "scheduled_active": scheduled_active_count,
            "failed": failed_task_count,
            "actionable_failed": actionable_failed_count,
            "reconciled_failed": reconciled_failed_count,
            "blocked": blocked_task_count,
            "actionable_blocked": actionable_blocked_count,
            "waiting_configuration": waiting_configuration_count,
            "active_ids": [row.id for row in active_tasks],
            "runnable_active_ids": [row.id for row in runnable_active_tasks],
            "scheduled_active_ids": [row.id for row in scheduled_active_tasks],
            "next_scheduled_at": (
                min(row.run_after for row in scheduled_active).isoformat()
                if scheduled_active
                else None
            ),
            "failed_ids": [row.id for row in failed_tasks],
            "actionable_failed_ids": [row.id for row in actionable_failed_tasks],
            "reconciled_failed_ids": [row.id for row in reconciled_failed_tasks],
            "blocked_ids": [row.id for row in blocked_tasks],
            "actionable_blocked_ids": [row.id for row in actionable_blocked_tasks],
            "waiting_configuration_ids": [row.id for row in waiting_configuration_tasks],
        },
        "approvals": {
            "pending": pending_approval_count,
            "pending_total": pending_approval_total_count,
            "expired_pending": expired_pending_approval_count,
            "ids": [row.id for row in approvals],
            "expired_pending_ids": [row.id for row in expired_pending_approvals[:20]],
        },
        "critical_alerts": {
            "unacknowledged": unacknowledged_alert_count,
            "actionable": len(actionable_alerts),
            "historical_or_superseded": historical_alert_count,
            "ids": [row.id for row in alerts],
            "unacknowledged_ids": [row.id for row in all_alert_rows[:20]],
            "classification_scanned": len(all_alert_rows),
            "classification_unscanned": max(
                0,
                unacknowledged_alert_count - len(all_alert_rows),
            ),
            "dead_letter": all_dead_letter_count,
            "actionable_dead_letter": len(actionable_dead_letters),
            "historical_dead_letter": max(
                0,
                len(dead_letter_rows) - len(actionable_dead_letters),
            ),
            "dead_letter_classification_scanned": len(dead_letter_rows),
            "dead_letter_classification_unscanned": max(
                0,
                all_dead_letter_count - len(dead_letter_rows),
            ),
        },
        "goals": {
            "active": len(goals),
            "items": [
                {
                    "id": row.id,
                    "title": row.title,
                    "owner": row.owner,
                    "metric": row.metric,
                    "current": row.current,
                    "target": row.target,
                    "unit": row.unit,
                    "progress_percent": goal_progress(row)["progress_percent"],
                }
                for row in goals
            ],
        },
        "finance": {
            "overdue_payments": len(overdue_payments),
            "payment_ids": [row.id for row in overdue_payments[:20]],
            "overdue_amount": round(
                sum(float((row.data or {}).get("amount", 0) or 0) for row in overdue_payments),
                2,
            ),
        },
        "sales": {
            **sales_summary,
            "new_leads_in_period": new_leads,
            "hot_lead_ids": hot_lead_ids,
            "owner_handoff_goal": next(
                (
                    {
                        "current": row.current,
                        "target": row.target,
                        "unit": row.unit,
                        "status": (
                            "target_met" if row.current >= row.target else "behind_target"
                        ),
                    }
                    for row in goals
                    if row.metric == "qualified_owner_handoffs"
                ),
                None,
            ),
        },
        "tenders": tender_summary,
        "marketing": marketing_summary,
        "growth": growth,
        "agents": {
            "registered": len(runtime_agent_activity),
            "observed_components": len(agent_activity),
            "runs": agent_runs,
            "succeeded": agent_succeeded,
            "failed": agent_failed,
            "success_rate_percent": (
                round(agent_succeeded / finished_runs * 100, 2)
                if finished_runs
                else None
            ),
            "inactive_agents": [
                str(row.get("agent_type"))
                for row in runtime_agent_activity
                if not row.get("did_work")
            ],
            "activity": agent_activity,
        },
        "workflow": {
            "created_in_period": workflow_created,
            "completed_in_period": workflow_done,
            "failed_in_period": workflow_failed,
        },
        "integrations": credential_statuses,
    }
    recommendations: list[dict[str, Any]] = []
    if actionable_failed_tasks or actionable_blocked_tasks:
        recommendations.append(
            {
                "kind": "create_review_task",
                "priority": "high",
                "text": "Разобрать failed/blocked задачи и назначить ответственных.",
                "source_ids": [
                    row.id for row in (actionable_failed_tasks + actionable_blocked_tasks)
                ],
            }
        )
    if approvals:
        recommendations.append(
            {
                "kind": "review_approvals",
                "priority": "critical",
                "text": "Проверить ожидающие решения; никаких действий без owner approval.",
                "source_ids": [row.id for row in approvals],
            }
        )
    if alerts:
        recommendations.append(
            {
                "kind": "acknowledge_alerts",
                "priority": "critical",
                "text": "Проверить и подтвердить получение критических оповещений.",
                "source_ids": [row.id for row in alerts],
            }
        )
    if overdue_payments:
        recommendations.append(
            {
                "kind": "create_review_task",
                "priority": "high",
                "text": "Создать безопасную задачу Finance на разбор просроченной дебиторки.",
                "source_ids": [row.id for row in overdue_payments[:20]],
            }
        )
    execution_plan = _weekly_ceo_plan(
        generated_at=generated_at,
        goals=goals,
        sales=sales_summary,
        tenders=tender_summary,
        growth=growth,
        failed_tasks=actionable_failed_tasks,
        blocked_tasks=actionable_blocked_tasks,
        failed_task_count=actionable_failed_count,
        blocked_task_count=actionable_blocked_count,
        credential_statuses=credential_statuses,
        hot_lead_ids=hot_lead_ids,
    )
    source_freshness = {
        "tasks": _latest_timestamp(db, Task.updated_at),
        "agent_runs": _latest_timestamp(db, AgentRun.started_at),
        "business_records": _latest_timestamp(db, BusinessRecord.updated_at),
        "goals": _latest_timestamp(db, BusinessGoal.updated_at),
        "owner_notifications": _latest_timestamp(db, OwnerNotification.created_at),
    }
    return {
        "report_kind": "weekly_ceo_brief",
        "generated_at": generated_at.isoformat(),
        "period": {
            "days": period_days,
            "start": period_start.isoformat(),
            "end": generated_at.isoformat(),
        },
        "freshness": {
            "as_of": generated_at.isoformat(),
            "source": "primary_database",
            "sources": source_freshness,
        },
        "facts": facts,
        "recommendations": recommendations,
        "execution_plan": execution_plan,
        "sources": [
            {"resource": "tasks", "endpoint": "/api/tasks"},
            {"resource": "agent_runs", "endpoint": "/api/agent-runs"},
            {"resource": "approvals", "endpoint": "/api/approvals"},
            {"resource": "alerts", "endpoint": "/api/owner-notifications"},
            {"resource": "goals", "endpoint": "/api/goals"},
            {"resource": "payments", "endpoint": "/api/finance/payment-calendar"},
            {"resource": "sales_marketing_tenders", "endpoint": "/api/money-opportunities"},
        ],
        "evidence": [
            {
                "type": "cross_domain_primary_database_snapshot",
                "period_start": period_start.isoformat(),
                "period_end": generated_at.isoformat(),
                "source_record_ids": {
                    "tasks": [row.id for row in active_tasks + failed_tasks + blocked_tasks],
                    "approvals": [row.id for row in approvals],
                    "alerts": [row.id for row in alerts],
                    "goals": [row.id for row in goals],
                    "payments": [row.id for row in overdue_payments[:20]],
                    "hot_leads": hot_lead_ids,
                },
            }
        ],
        "ai_generated_facts": False,
        "automatic_critical_action": False,
    }


def format_ceo_brief(data: dict[str, Any]) -> str:
    """Render the source-linked CEO brief within Telegram's message budget."""
    facts = data.get("facts") or {}
    task_facts = facts.get("tasks") or {}
    approval_facts = facts.get("approvals") or {}
    alert_facts = facts.get("critical_alerts") or {}
    finance = facts.get("finance") or {}
    sales = facts.get("sales") or {}
    agents = facts.get("agents") or {}
    workflow = facts.get("workflow") or {}
    tenders = facts.get("tenders") or {}
    growth = facts.get("growth") or {}
    recommendations = data.get("recommendations") or []
    execution_plan = data.get("execution_plan") or []
    handoff_goal = sales.get("owner_handoff_goal") or {}
    raw_actionable_failed_ids = (
        task_facts.get("actionable_failed_ids")
        if "actionable_failed_ids" in task_facts
        else task_facts.get("failed_ids") or []
    )
    actionable_failed_ids = (
        list(raw_actionable_failed_ids)
        if isinstance(raw_actionable_failed_ids, list)
        else []
    )
    success_rate = agents.get("success_rate_percent")
    success_rate_text = f"{success_rate}%" if success_rate is not None else "нет завершённых запусков"
    lines = [
        "🤖 AI CEO · Недельный brief",
        f"Актуально на: {data.get('generated_at')}",
        "",
        "ФАКТЫ ИЗ БД",
        (
            f"• Лиды: всего {sales.get('leads', 0)}, новых за период {sales.get('new_leads_in_period', 0)}, "
            f"передано владельцу {sales.get('owner_review', 0)}, "
            f"разобрано {sales.get('owner_review_triaged', 0)}, "
            f"ещё не квалифицировано {sales.get('owner_review_untriaged', 0)}, "
            f"qualified {sales.get('qualified', 0)}, won {sales.get('won', 0)}"
        ),
        (
            f"• Цель передач: {handoff_goal.get('current', 0)} из "
            f"{handoff_goal.get('target', 0)} {handoff_goal.get('unit', '')} · "
            f"{handoff_goal.get('status', 'goal_not_initialized')}"
        ),
        (
            f"• Контракты: active {sales.get('active_contracts', 0)}, "
            f"месячная выручка {sales.get('active_monthly_revenue', '0.00')} ₽"
        ),
        (
            f"• Агенты: {agents.get('succeeded', 0)}/{agents.get('runs', 0)} успешно, "
            f"ошибок {agents.get('failed', 0)}, success rate {success_rate_text}"
        ),
        (
            f"• Workflow: создано {workflow.get('created_in_period', 0)}, "
            f"завершено {workflow.get('completed_in_period', 0)}, failed {workflow.get('failed_in_period', 0)}"
        ),
        (
            f"• Задачи сейчас: active {task_facts.get('active', 0)}, "
            f"runnable {task_facts.get('runnable_active', task_facts.get('active', 0))}, "
            f"scheduled {task_facts.get('scheduled_active', 0)}, "
            f"actionable failed {task_facts.get('actionable_failed', task_facts.get('failed', 0))}, "
            f"reconciled failed {task_facts.get('reconciled_failed', 0)}, "
            f"actionable blocked {task_facts.get('actionable_blocked', task_facts.get('blocked', 0))}, "
            f"waiting configuration {task_facts.get('waiting_configuration', 0)}"
        ),
        (
            "  source task IDs: "
            f"{actionable_failed_ids + (task_facts.get('actionable_blocked_ids') or task_facts.get('blocked_ids') or [])}"
        ),
        f"• Тендеры: active {tenders.get('active', 0)}, owner review {tenders.get('ready_for_owner_review', 0)}",
        f"• Рост: {growth.get('status', 'goal_not_initialized')} · progress {growth.get('progress_percent', 0)}%",
        (
            f"• Ожидают owner approval: {approval_facts.get('pending', 0)} "
            f"· истекли и ожидают фиксации {approval_facts.get('expired_pending', 0)} "
            f"· IDs {approval_facts.get('ids') or []}"
        ),
        (
            f"• Alerts: актуальные {alert_facts.get('actionable', alert_facts.get('unacknowledged', 0))} "
            f"· исторические/заменённые {alert_facts.get('historical_or_superseded', 0)} "
            f"· dead-letter актуальные {alert_facts.get('actionable_dead_letter', alert_facts.get('dead_letter', 0))} "
            f"· IDs {alert_facts.get('ids') or []}"
        ),
        (
            f"• Просроченные платежи: {finance.get('overdue_payments', 0)} "
            f"на {finance.get('overdue_amount', 0)} ₽ · IDs {finance.get('payment_ids') or []}"
        ),
        "",
        "ПЛАН НА 7 ДНЕЙ",
    ]
    for item in execution_plan:
        lines.append(
            f"• #{item.get('rank')} {item.get('owner_agent')}: {item.get('outcome')} "
            f"KPI {item.get('baseline')} → {item.get('target')} {item.get('unit', '')} "
            f"до {item.get('deadline')}"
        )
    if not execution_plan:
        lines.append("• План не сформирован: требуется проверка источников.")
    lines.append("\nРЕКОМЕНДАЦИИ (НЕ ВЫПОЛНЕНЫ)")
    lines.extend(
        f"• [{item.get('priority', 'normal')}] {item.get('text')} · source IDs {item.get('source_ids') or []}"
        for item in recommendations
    )
    if not recommendations:
        lines.append("• Срочных рекомендаций по текущему snapshot нет.")
    lines.append("\nКритические действия и внешние сообщения автоматически не выполнялись.")
    rendered = "\n".join(lines)
    return rendered if len(rendered) <= 3900 else rendered[:3899] + "…"


def build_activity_report(
    db: Session,
    *,
    period_hours: int | float = 24,
    period_minutes: int | None = None,
) -> dict[str, Any]:
    """Build a verifiable read-only report from the shared operational database."""
    minutes = (
        max(1, min(int(period_minutes), 7 * 24 * 60))
        if period_minutes is not None
        else max(60, min(int(float(period_hours) * 60), 7 * 24 * 60))
    )
    hours: int | float = minutes / 60
    if float(hours).is_integer():
        hours = int(hours)
    generated_at = _utcnow()
    cutoff = generated_at - timedelta(minutes=minutes)

    running_reports = db.scalars(
        select(Task).where(Task.status == "running", Task.agent_type == "orchestrator")
    ).all()
    current_report_count = sum(
        1 for row in running_reports if (row.payload or {}).get("action") == "system_activity_report"
    )

    completed = db.scalars(
        select(Task)
        .where(Task.status == "done", Task.updated_at >= cutoff)
        .order_by(Task.updated_at.desc(), Task.id.desc())
    ).all()
    recent_completed = [row for row in completed if row.agent_type != "request_analyst"][:5]
    upcoming = db.scalars(
        select(Task)
        .where(Task.status.in_(["open", "queued"]))
        .order_by(Task.run_after, Task.id)
        .limit(5)
    ).all()

    active_total = _count(db, Task, Task.status.in_(["open", "queued", "running"]))
    queued_improvements = _count(
        db, ImprovementRequest, ImprovementRequest.status == "queued"
    )
    queued_improvements_perplexity = _count(
        db,
        ImprovementRequest,
        ImprovementRequest.status == "queued",
        ImprovementRequest.source_user == "perplexity_agent_coach",
    )
    queued_improvements_research = _count(
        db,
        ImprovementRequest,
        ImprovementRequest.status == "queued",
        ImprovementRequest.source_user == "github_evolution_researcher",
    )
    queued_improvements_telegram = _count(
        db,
        ImprovementRequest,
        ImprovementRequest.status == "queued",
        ImprovementRequest.source_channel == "telegram",
    )
    summary = {
        "tasks_completed": len(completed),
        "business_tasks_completed": sum(row.agent_type != "request_analyst" for row in completed),
        "tasks_active": max(0, active_total - current_report_count),
        "tasks_failed": _count(db, Task, Task.status == "failed"),
        "tasks_blocked": _count(db, Task, Task.status == "blocked"),
        "agent_runs_succeeded": _count(
            db, AgentRun, AgentRun.status == "succeeded", AgentRun.finished_at >= cutoff
        ),
        "agent_runs_failed": _count(
            db, AgentRun, AgentRun.status == "failed", AgentRun.finished_at >= cutoff
        ),
        "queued_improvements": queued_improvements,
        "queued_improvements_perplexity": queued_improvements_perplexity,
        "queued_improvements_research": queued_improvements_research,
        "queued_improvements_telegram": queued_improvements_telegram,
        "queued_improvements_other": max(
            0,
            queued_improvements
            - queued_improvements_perplexity
            - queued_improvements_research
            - queued_improvements_telegram,
        ),
        "implemented_improvements": _count(
            db,
            ImprovementRequest,
            ImprovementRequest.status == "implemented",
            ImprovementRequest.updated_at >= cutoff,
        ),
        "pending_approvals": _count(db, ApprovalRequest, ApprovalRequest.status == "pending"),
        "events_pending": _count(db, DomainEvent, DomainEvent.status == "pending"),
        "events_dead_letter": _count(db, DomainEvent, DomainEvent.status == "dead_letter"),
    }

    agents = db.scalars(select(AgentState).order_by(AgentState.agent_type)).all()
    agent_statuses = [
        {
            "agent_type": row.agent_type,
            "status": row.status,
            "last_heartbeat_at": row.last_heartbeat_at.isoformat() if row.last_heartbeat_at else None,
            "last_error": row.last_error,
        }
        for row in agents
    ]
    agent_activity = _agent_activity(db, cutoff=cutoff)
    from .agents import AGENTS

    agent_topology = {
        "registered_roles": len(AGENTS),
        "worker_replicas_desired": max(
            1, min(int(settings.agent_worker_replicas), 60)
        ),
        "worker_replicas_max_supported": 60,
        "coordination": "postgresql_task_queue_skip_locked",
    }
    blockers = []
    if summary["tasks_failed"]:
        blockers.append(f"Задач с ошибкой: {summary['tasks_failed']}")
    if summary["tasks_blocked"]:
        blockers.append(f"Заблокированных задач: {summary['tasks_blocked']}")
    if summary["events_dead_letter"]:
        blockers.append(f"Событий в dead-letter: {summary['events_dead_letter']}")
    if summary["pending_approvals"]:
        blockers.append(f"Подтверждений владельца ожидают: {summary['pending_approvals']}")

    task_evidence = [
        {
            "type": "completed_task",
            "task_id": row.id,
            "agent_type": row.agent_type,
            "status": row.status,
            "updated_at": row.updated_at.isoformat(),
        }
        for row in recent_completed
    ]
    latest_coordination_task = next(
        (
            row
            for row in db.scalars(
                select(Task)
                .where(
                    Task.agent_type == "orchestrator",
                    Task.status == "done",
                    Task.updated_at >= generated_at - timedelta(minutes=max(60, minutes * 2)),
                )
                .order_by(Task.updated_at.desc(), Task.id.desc())
                .limit(50)
            ).all()
            if (row.result or {}).get("report_kind") == "marketing_sales_coordination"
        ),
        None,
    )
    marketing_sales_coordination = None
    if latest_coordination_task is not None:
        coordination_result = latest_coordination_task.result or {}
        marketing_sales_coordination = {
            "task_id": latest_coordination_task.id,
            "generated_at": coordination_result.get("generated_at"),
            "participants": coordination_result.get("participants") or [],
            "discussion": coordination_result.get("discussion") or [],
            "decisions": coordination_result.get("decisions") or [],
            "actions": coordination_result.get("actions") or [],
            "funnel": coordination_result.get("funnel") or {},
            "automatic_outreach": bool(coordination_result.get("automatic_outreach")),
            "outbound_messages_created": int(
                coordination_result.get("outbound_messages_created") or 0
            ),
        }
    strategic_growth = growth_snapshot(db, now=generated_at)
    from .operational_links import operational_links

    return {
        "outcome": "completed",
        "report_kind": "system_activity",
        "period_hours": hours,
        "period_minutes": minutes,
        "generated_at": generated_at.isoformat(),
        "summary": summary,
        "recent_completed_tasks": [
            {
                "id": row.id,
                "title": row.title,
                "agent_type": row.agent_type,
                "status": row.status,
                "updated_at": row.updated_at.isoformat(),
            }
            for row in recent_completed
        ],
        "upcoming_tasks": [
            {
                "id": row.id,
                "title": row.title,
                "agent_type": row.agent_type,
                "run_after": row.run_after.isoformat(),
            }
            for row in upcoming
            if (row.payload or {}).get("action") != "system_activity_report"
        ],
        "agent_statuses": agent_statuses,
        "agent_activity": agent_activity,
        "agent_topology": agent_topology,
        "strategic_growth": strategic_growth,
        "marketing_sales_coordination": marketing_sales_coordination,
        "links": operational_links(),
        "blockers": blockers,
        "evidence": [
            {
                "type": "database_snapshot",
                "generated_at": generated_at.isoformat(),
                "period_hours": hours,
                "period_minutes": minutes,
                **summary,
            },
            {
                "type": "billion_revenue_goal_snapshot",
                "goal_id": strategic_growth.get("goal_id"),
                "current_rub": strategic_growth["current_rub"],
                "target_rub": strategic_growth["target_rub"],
                "source": strategic_growth.get("source"),
            },
            *task_evidence,
        ],
    }


def format_activity_report(result: dict[str, Any]) -> str:
    """Format the same verified report for Telegram and queued notifications."""
    summary = result.get("summary") or {}
    period_minutes = int(result.get("period_minutes") or float(result.get("period_hours", 24)) * 60)
    period_label = f"{period_minutes} мин." if period_minutes < 60 else f"{period_minutes / 60:g} ч."
    lines = [
        f"📋 Отчёт CleaningAI OS за {period_label}",
        f"✅ Выполнено задач: {summary.get('tasks_completed', 0)}",
        f"🔄 В работе и очереди: {summary.get('tasks_active', 0)}",
        f"⚠️ Ошибок: {summary.get('tasks_failed', 0)}",
        f"⛔ Заблокировано: {summary.get('tasks_blocked', 0)}",
        (
            f"🛠 Улучшений в очереди: {summary.get('queued_improvements', 0)} "
            f"(Perplexity: {summary.get('queued_improvements_perplexity', 0)}, "
            f"GitHub research: {summary.get('queued_improvements_research', 0)}, "
            f"Telegram: {summary.get('queued_improvements_telegram', 0)}, "
            f"прочие: {summary.get('queued_improvements_other', 0)})"
        ),
        f"🔐 Ожидают подтверждения: {summary.get('pending_approvals', 0)}",
    ]
    crm_url = str((result.get("links") or {}).get("crm") or "").strip()
    if crm_url:
        lines.append(f"🔗 CRM: {crm_url}")
    agent_activity = result.get("agent_activity") or []
    if agent_activity:
        topology = result.get("agent_topology") or {}
        lines.append(
            "\n🧠 Контур агентов: "
            f"{int(topology.get('registered_roles') or 0)} ролей · "
            f"{int(topology.get('worker_replicas_desired') or 0)} параллельных worker"
        )
        lines.append("\n🤖 Работа каждого ИИ-агента:")
        for row in agent_activity:
            agent_type = _short_text(row.get("agent_type"), 40)
            if row.get("did_work"):
                detail = (
                    f"{int(row.get('succeeded') or 0)}/{int(row.get('runs') or 0)} успешно"
                )
                if row.get("failed"):
                    detail += f", ошибок: {int(row['failed'])}"
                if row.get("last_task_id"):
                    detail += (
                        f"; последнее #{row['last_task_id']} "
                        f"{_short_text(row.get('last_task_title'), 70)}"
                    )
            elif row.get("running"):
                detail = f"работает — выполняется запусков: {int(row['running'])}"
                if row.get("last_task_id"):
                    detail += f"; сейчас #{row['last_task_id']} {_short_text(row.get('last_task_title'), 70)}"
            else:
                detail = "не работал — " + _short_text(
                    row.get("inactivity_reason") or "причина не зафиксирована",
                    150,
                )
            if row.get("active_tasks"):
                detail += f"; ожидают задач: {int(row['active_tasks'])}"
            lines.append(f"• {agent_type}: {detail}")
    growth = result.get("strategic_growth") or {}
    if growth:
        lines.extend([
            "\n🎯 Цель: годовой оборот 1 млрд ₽",
            f"Факт run-rate: {growth.get('current_rub', 0):,} ₽/год".replace(",", " "),
            f"Прогресс: {growth.get('progress_percent', 0)}% · разрыв: {growth.get('gap_to_target_rub', growth.get('gap_rub', 0)):,} ₽".replace(",", " "),
            f"Темп: {'по плану' if growth.get('status') == 'on_track' else 'ниже плана'} · данные: активные договоры",
        ])
    coordination = result.get("marketing_sales_coordination") or {}
    if coordination:
        lines.append("\n🤝 Marketing ↔ Research ↔ Sales")
        for item in (coordination.get("discussion") or [])[:2]:
            message = str(item.get("message") or "").strip()
            if len(message) > 160:
                message = message[:157] + "..."
            lines.append(f"• {item.get('agent', 'agent')}: {message}")
        actions = coordination.get("actions") or []
        if actions:
            lines.append("Следующие действия:")
            for item in actions[:3]:
                status = str(item.get("status") or "planned")
                action = str(item.get("action") or "").strip()
                if len(action) > 140:
                    action = action[:137] + "..."
                task_id = f"#{item['task_id']} " if item.get("task_id") else ""
                lines.append(f"• {task_id}[{item.get('agent', 'agent')}/{status}] {action}")
        lines.append("Автоматическая отправка: не выполнялась.")
    recent = result.get("recent_completed_tasks") or []
    if recent:
        lines.append("\nПоследние результаты:")
        lines.extend(f"• #{row['id']} [{row['agent_type']}] {row['title']}" for row in recent[:3])
    upcoming = result.get("upcoming_tasks") or []
    if upcoming:
        lines.append("\nЗапланировано AI CEO:")
        lines.extend(f"• #{row['id']} [{row['agent_type']}] {row['title']}" for row in upcoming[:3])
    blockers = result.get("blockers") or []
    lines.append("\nТребуют внимания: " + ("; ".join(blockers) if blockers else "нет."))
    return "\n".join(lines)


def build_system_self_check(db: Session, *, registered_agents: list[str]) -> dict[str, Any]:
    """Inspect safe capabilities without executing external or protected actions."""
    db.execute(text("SELECT 1"))
    integrations = integration_status()
    registered = set(registered_agents)

    checks: list[dict[str, Any]] = []

    def add(name: str, status: str, detail: str, *, credentials: list[str] | None = None) -> None:
        checks.append(
            {
                "name": name,
                "status": status,
                "detail": detail,
                "credentials_required": credentials or [],
            }
        )

    add("PostgreSQL", "ready", "Контрольный запрос к общей базе выполнен.")
    add("API и Orchestrator", "ready", "Задача самопроверки выполнена через общий runtime и audit log.")
    add(
        "Telegram-бот",
        "ready" if integrations["telegram"]["status"] == "configured" else "credentials_required",
        "Бот настроен для владельца."
        if integrations["telegram"]["status"] == "configured"
        else "Нужны токен бота и Telegram ID владельца.",
        credentials=[]
        if integrations["telegram"]["status"] == "configured"
        else ["TELEGRAM_BOT_TOKEN", "OWNER_TELEGRAM_ID"],
    )

    internal_agents = {
        "AI CEO": "ceo",
        "Growth Officer": "growth_officer",
        "Sales/CRM": "sales",
        "HR": "hr",
        "Finance": "finance",
        "Marketing/SMM": "marketing",
        "Meta Brain": "meta_brain",
        "Request Analyst": "request_analyst",
        "Copywriter Agent": "copywriter",
        "Creative Agent": "creative",
    }
    for label, agent_type in internal_agents.items():
        add(
            label,
            "ready" if agent_type in registered else "unavailable",
            "Агент зарегистрирован и работает с общей моделью данных."
            if agent_type in registered
            else "Исполнитель не зарегистрирован.",
        )

    tender_ready = integrations["tender_sources"]["status"] == "configured"
    add(
        "Тендеры и Research",
        "ready" if tender_ready else "configuration_required",
        "Источники тендеров настроены."
        if tender_ready
        else "Внутренний анализ готов, но внешние источники тендеров не настроены.",
        credentials=[] if tender_ready else ["TENDER_SOURCES", "TENDER_SOURCE_TOKEN (если требуется источником)"],
    )

    smtp_ready = integrations["smtp_default"]["status"] == "configured"
    add(
        "Email и горячие лиды",
        "ready" if smtp_ready else "credentials_required",
        "SMTP настроен; suppression, unsubscribe и лимиты остаются обязательными."
        if smtp_ready
        else "CRM работает, но отправка писем и email-уведомления отключены.",
        credentials=[]
        if smtp_ready
        else ["SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_FROM_EMAIL", "OWNER_NOTIFICATION_EMAIL"],
    )

    llm_ready = integrations["llm"]["status"] == "configured"
    add(
        "LLM-анализ",
        "ready" if llm_ready else "credentials_required",
        "AI-провайдер настроен."
        if llm_ready
        else "Детерминированные функции работают; расширенный AI-анализ отключён.",
        credentials=[]
        if llm_ready
        else ["LLM_API_KEY or ANTHROPIC_API_KEY or GEMINI_API_KEY"],
    )

    marketing_credentials = {
        "yandex": ["YANDEX_DIRECT_TOKEN"],
        "vk_ads": ["VK_ADS_TOKEN"],
        "2gis": ["TWOGIS_BUSINESS_TOKEN"],
        "avito": ["AVITO_CLIENT_ID", "AVITO_CLIENT_SECRET"],
        "telegram_ads": ["TELEGRAM_ADS_TOKEN"],
    }
    marketing_missing = [
        key for key, value in integrations["marketing_channels"].items() if value == "credentials_required"
    ]
    add(
        "Рекламные платформы",
        "ready" if not marketing_missing else "credentials_required",
        "Все рекламные credentials присутствуют; внешние действия всё равно требуют policy/approval."
        if not marketing_missing
        else "Внутренняя аналитика готова, внешние рекламные кабинеты не подключены.",
        credentials=[
            credential for name in marketing_missing for credential in marketing_credentials[name]
        ],
    )

    workspace_ready = integrations["workspace_agent_handoff"]["status"] == "configured"
    add(
        "Передача улучшений в Codex",
        "ready" if workspace_ready else "credentials_required",
        "Workspace Agent handoff настроен."
        if workspace_ready
        else "Улучшения сохраняются в PostgreSQL, но автоматическая передача в Workspace Agent отключена.",
        credentials=[]
        if workspace_ready
        else ["WORKSPACE_AGENT_TRIGGER_ID", "WORKSPACE_AGENT_ACCESS_TOKEN"],
    )

    video_ready = integrations["media_generation"]["video"] != "credentials_required"
    add(
        "Генерация медиа",
        "ready" if video_ready else "configuration_required",
        "Image workflow доступен; видеопровайдер настроен."
        if video_ready
        else "Image workflow доступен, для реального видео нужен лицензированный провайдер.",
        credentials=[] if video_ready else ["VIDEO_GENERATION_API_KEY"],
    )

    site_ready = integrations["public_website"]["status"] == "ready"
    add(
        "Публичный сайт и лид-форма",
        "ready" if site_ready else "configuration_required",
        "Сайт и согласие на обработку данных готовы."
        if site_ready
        else "Не заполнен обязательный профиль оператора персональных данных.",
    )

    status_counts: dict[str, int] = {}
    for item in checks:
        status_counts[item["status"]] = status_counts.get(item["status"], 0) + 1
    all_ready = all(item["status"] == "ready" for item in checks)
    credentials_required = sorted(
        {credential for item in checks for credential in item["credentials_required"]}
    )
    return {
        "outcome": "completed",
        "check_kind": "system_functional_readiness",
        "overall_status": "ready" if all_ready else "partial",
        "generated_at": _utcnow().isoformat(),
        "checks": checks,
        "summary": {
            "total": len(checks),
            "ready": status_counts.get("ready", 0),
            "configuration_required": status_counts.get("configuration_required", 0),
            "credentials_required": status_counts.get("credentials_required", 0),
            "unavailable": status_counts.get("unavailable", 0),
        },
        "credentials_required": credentials_required,
        "safety": {
            "protected_actions_executed": False,
            "external_messages_sent": False,
            "financial_commitments_created": False,
            "owner_approval_bypassed": False,
        },
        "evidence": [
            {"type": "database_probe", "status": "passed"},
            {"type": "registered_agents", "agents": sorted(registered)},
            {
                "type": "configuration_presence",
                "statuses": {
                    "telegram": integrations["telegram"]["status"],
                    "smtp": integrations["smtp_default"]["status"],
                    "tender_sources": integrations["tender_sources"]["status"],
                    "llm": integrations["llm"]["status"],
                    "website": integrations["public_website"]["status"],
                },
            },
        ],
    }


_INTERNAL_REPORT_ACTIONS = {
    "system_activity_report",
    "system_self_check",
    "task_timing_report",
}


def _is_user_business_task(task: Task) -> bool:
    payload = task.payload or {}
    return (
        task.agent_type != "request_analyst"
        and payload.get("action") not in _INTERNAL_REPORT_ACTIONS
        and payload.get("source") in {"telegram_natural_language", "telegram_document"}
    )


def _verified_task_result(task: Task) -> bool:
    result = task.result or {}
    if result.get("credentials_required") or result.get("status") in {
        "adapter_required",
        "credentials_required",
    }:
        return False
    payload = task.payload or {}
    request_text = f"{task.title} {payload.get('original_message', '')}".lower()
    requested_files = any(
        token in request_text for token in ("pdf", "xls", "xlsx", "word", "docx")
    )
    artifact_keys = (
        "download_url",
        "storage_path",
        "proposal_id",
        "workspace_conversation_url",
    )
    if requested_files and not any(result.get(key) for key in artifact_keys):
        evidence = result.get("evidence")
        artifact_types = {
            "proposal_pdf",
            "file_export",
            "document_export",
            "dataset_export",
        }
        return isinstance(evidence, list) and any(
            isinstance(item, dict) and item.get("type") in artifact_types
            for item in evidence
        )
    evidence = result.get("evidence")
    if isinstance(evidence, list) and evidence:
        return any(
            not (isinstance(item, dict) and set(item) == {"delegations_requested"})
            for item in evidence
        )
    return any(result.get(key) for key in artifact_keys)


def _scope_estimate(task: Task) -> dict[str, Any] | None:
    payload = task.payload or {}
    text_value = f"{task.title} {payload.get('original_message', '')}".lower().replace("ё", "е")
    external_research = any(
        phrase in text_value
        for phrase in (
            "собери базу",
            "всех возможных источников",
            "найди сайты",
            "найди там почты",
            "проверкой реально ли",
        )
    )
    formats = sum(token in text_value for token in ("pdf", "xls", "xlsx", "word", "docx"))
    if external_research and formats >= 2:
        return {
            "min_hours": 8,
            "max_hours": 24,
            "confidence": "low",
            "starts_after": [
                "утверждены и подключены источники данных",
                "реализован проверяемый сбор с дедупликацией",
                "доступен экспорт в запрошенные форматы",
            ],
            "basis": "массовый сбор и проверка контактов плюс несколько форматов экспорта",
        }
    if external_research:
        return {
            "min_hours": 2,
            "max_hours": 8,
            "confidence": "low",
            "starts_after": ["утверждены и подключены источники данных"],
            "basis": "внешний сбор и проверка данных",
        }
    if payload.get("action") == "generate_proposal":
        return {
            "min_hours": 0.02,
            "max_hours": 0.08,
            "confidence": "high",
            "starts_after": ["клиент найден в CRM"],
            "basis": "автоматическая генерация одного PDF из CRM",
        }
    if payload.get("action") == "revise_proposal":
        return {
            "min_hours": 0.05,
            "max_hours": 0.25,
            "confidence": "medium",
            "starts_after": ["DOCX/PDF успешно скачан из Telegram"],
            "basis": "локальная редакция текста, вёрстка DOCX/PDF и создание owner approval",
        }
    return None


def build_task_timing_report(db: Session, *, task_id: int | None = None) -> dict[str, Any]:
    """Return a truthful task ETA or explain why one cannot yet be promised."""
    task = db.get(Task, int(task_id)) if task_id is not None else None
    selection = "explicit_id" if task_id is not None else "latest_telegram_business_task"
    if task_id is None:
        candidates = db.scalars(select(Task).order_by(Task.id.desc()).limit(200)).all()
        task = next((row for row in candidates if _is_user_business_task(row)), None)
    if task is None:
        return {
            "outcome": "needs_clarification",
            "report_kind": "task_timing",
            "task_id": task_id,
            "message": "Задача не найдена. Укажите её номер, например: «Сколько времени нужно на задачу #9?»",
            "evidence": [{"type": "task_lookup", "selection": selection, "found": False}],
        }

    run = db.scalar(
        select(AgentRun)
        .where(AgentRun.task_id == task.id)
        .order_by(AgentRun.id.desc())
        .limit(1)
    )
    actual_seconds = None
    if run and run.started_at and run.finished_at:
        actual_seconds = round(max(0.0, (run.finished_at - run.started_at).total_seconds()), 3)

    verified = _verified_task_result(task)
    estimate = _scope_estimate(task)
    if task.status == "done" and verified:
        timing_status = "completed"
        remaining_seconds = 0
        reason = "Результат завершён и подтверждён evidence или артефактом."
    elif task.status == "done":
        timing_status = "result_unverified"
        remaining_seconds = None
        reason = "Задача помечена done, но подтверждённый результат или файл отсутствует; считать её выполненной нельзя."
    elif task.status == "blocked":
        timing_status = "blocked"
        remaining_seconds = None
        reason = "Задача ожидает обязательного решения владельца или другой блокирующей зависимости."
    elif task.status == "failed":
        timing_status = "failed"
        remaining_seconds = None
        reason = "Последняя попытка завершилась ошибкой; срок появится после устранения причины."
    elif task.status == "running":
        timing_status = "running"
        remaining_seconds = None
        reason = "Исполнитель работает, но подтверждённая бизнес-оценка срока в задаче не задана."
    else:
        timing_status = "queued"
        remaining_seconds = None
        reason = "Задача ожидает запуска; технический timeout попытки не является сроком бизнес-результата."

    return {
        "outcome": "completed",
        "report_kind": "task_timing",
        "generated_at": _utcnow().isoformat(),
        "selection": selection,
        "task": {
            "id": task.id,
            "title": task.title,
            "agent_type": task.agent_type,
            "status": task.status,
            "attempts": task.attempts,
            "max_attempts": task.max_attempts,
        },
        "timing_status": timing_status,
        "remaining_seconds": remaining_seconds,
        "actual_runtime_seconds": actual_seconds,
        "result_verified": verified,
        "reason": reason,
        "planning_estimate": estimate,
        "evidence": [
            {
                "type": "task_timing_snapshot",
                "task_id": task.id,
                "task_status": task.status,
                "result_verified": verified,
                "run_id": run.id if run else None,
                "run_status": run.status if run else None,
                "actual_runtime_seconds": actual_seconds,
            }
        ],
    }
