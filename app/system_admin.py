from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .chat import redact_sensitive_text
from .improvements import build_codex_prompt, retry_workspace_handoff
from .models import (
    AgentRun,
    AgentState,
    AuditLog,
    ImprovementRequest,
    MailTransportState,
    OutboundMessage,
    OwnerNotification,
    Task,
    TaskTransition,
)
from .notifications import queue_owner_notification


COMMAND_CATALOG = [
    "/start — открыть центр управления",
    "/dashboard — состояние бизнеса и агентов",
    "/tasks — задачи и фактические статусы",
    "/sysadmin — ошибки, доработки и результат перепроверки",
    "/outreach — состояние рассылок",
    "/mailing — безопасный мастер рассылки",
    "/social — публикации в социальных сетях",
    "/ceo — отчёт AI CEO",
]

TECHNICAL_TASK_STATUSES = {"blocked", "failed"}
NOTIFICATION_FAILURE_STATUSES = {"retry", "dead_letter", "waiting_configuration"}
OUTREACH_FAILURE_STATUSES = {"failed", "waiting_configuration"}


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _safe_text(value: Any, limit: int = 1000) -> str:
    return redact_sensitive_text(str(value or "")).strip()[:limit]


def _fingerprint(kind: str, resource_type: str, resource_id: str) -> str:
    value = f"system-admin:{kind}:{resource_type}:{resource_id}"
    return hashlib.sha256(value.encode()).hexdigest()


def _latest_transition(db: Session, task_id: int) -> TaskTransition | None:
    return db.scalar(
        select(TaskTransition)
        .where(TaskTransition.task_id == task_id)
        .order_by(TaskTransition.id.desc())
    )


def _task_reason(db: Session, task: Task) -> str:
    result = task.result or {}
    for key in ("execution_gap", "error", "reason", "status"):
        if result.get(key):
            return _safe_text(result[key])
    transition = _latest_transition(db, task.id)
    if transition:
        detail_error = (transition.details or {}).get("error")
        return _safe_text(detail_error or transition.reason or f"task status: {task.status}")
    return f"task status: {task.status}"


def _is_owner_approval_wait(task: Task, transition: TaskTransition | None) -> bool:
    result = task.result or {}
    return bool(
        result.get("approval_id")
        and (
            result.get("reason") == "owner_approval_required"
            or (transition and transition.reason == "owner_approval_required")
        )
    )


def _incident(
    *,
    kind: str,
    resource_type: str,
    resource_id: str,
    reason: str,
    severity: str = "high",
    count: int = 1,
    credentials_required: bool = False,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    safe_reason = _safe_text(reason)
    return {
        "fingerprint": _fingerprint(kind, resource_type, resource_id),
        "kind": kind,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "reason": safe_reason,
        "severity": severity,
        "count": int(count),
        "credentials_required": credentials_required,
        "data": data or {},
    }


def collect_incidents(
    db: Session,
    *,
    now: datetime,
    stale_task_minutes: int,
) -> list[dict[str, Any]]:
    incidents: list[dict[str, Any]] = []
    stale_before = now - timedelta(minutes=max(1, stale_task_minutes))
    tasks = db.scalars(
        select(Task).where(
            Task.agent_type != "system_admin",
            (
                Task.status.in_(TECHNICAL_TASK_STATUSES)
                | ((Task.status == "running") & (Task.updated_at < stale_before))
            ),
        )
    ).all()
    for task in tasks:
        transition = _latest_transition(db, task.id)
        if task.status == "blocked" and _is_owner_approval_wait(task, transition):
            continue
        stale = task.status == "running"
        result = task.result or {}
        incidents.append(
            _incident(
                kind="stale_task" if stale else f"task_{task.status}",
                resource_type="task",
                resource_id=str(task.id),
                reason=(
                    f"Задача выполняется дольше {stale_task_minutes} минут без завершения"
                    if stale
                    else _task_reason(db, task)
                ),
                severity="critical" if stale or task.status == "failed" else "high",
                credentials_required=bool(
                    result.get("credentials_required")
                    or result.get("status") in {"credentials_required", "configuration_required"}
                ),
                data={"task_id": task.id, "agent_type": task.agent_type, "status": task.status},
            )
        )

    outreach_groups = db.execute(
        select(
            OutboundMessage.mailbox_id,
            OutboundMessage.status,
            OutboundMessage.error,
            func.count(OutboundMessage.id),
        )
        .where(OutboundMessage.status.in_(OUTREACH_FAILURE_STATUSES))
        .group_by(OutboundMessage.mailbox_id, OutboundMessage.status, OutboundMessage.error)
    ).all()
    mailbox_groups: dict[str, dict[str, Any]] = {}
    for mailbox_id, status, error, count in outreach_groups:
        mailbox_key = str(mailbox_id or "default")
        group = mailbox_groups.setdefault(
            mailbox_key,
            {"count": 0, "statuses": set(), "errors": set()},
        )
        group["count"] += int(count)
        group["statuses"].add(str(status))
        if error:
            group["errors"].add(_safe_text(error, 500))
    for mailbox_key, group in mailbox_groups.items():
        errors = sorted(group["errors"])
        reason = "; ".join(errors[:3]) or "SMTP-отправитель не настроен или недоступен"
        incidents.append(
            _incident(
                kind="outreach_delivery_blocked",
                resource_type="sender_mailbox",
                resource_id=mailbox_key,
                reason=reason,
                severity="critical" if "authentication" in reason.lower() else "high",
                count=group["count"],
                credentials_required=True,
                data={"statuses": sorted(group["statuses"]), "message_count": group["count"]},
            )
        )

    transport_states = db.scalars(
        select(MailTransportState).where(MailTransportState.status != "ready")
    ).all()
    for state in transport_states:
        if state.mailbox_key in mailbox_groups:
            continue
        if (
            state.status == "rate_limited"
            and state.retry_after is not None
            and state.retry_after <= now
        ):
            continue
        incidents.append(
            _incident(
                kind="outreach_transport_quarantined",
                resource_type="sender_mailbox",
                resource_id=state.mailbox_key,
                reason=_safe_text(state.reason, 500) or f"SMTP transport status: {state.status}",
                severity=(
                    "critical"
                    if state.status in {"provider_blocked", "credentials_required"}
                    else "high"
                ),
                credentials_required=state.status in {"provider_blocked", "credentials_required"},
                data={
                    "status": state.status,
                    "retry_after": state.retry_after.isoformat() if state.retry_after else None,
                    "consecutive_failures": state.consecutive_failures,
                },
            )
        )

    notification_groups = db.execute(
        select(
            OwnerNotification.channel,
            OwnerNotification.status,
            OwnerNotification.last_error,
            func.count(OwnerNotification.id),
        )
        .where(OwnerNotification.status.in_(NOTIFICATION_FAILURE_STATUSES))
        .group_by(OwnerNotification.channel, OwnerNotification.status, OwnerNotification.last_error)
    ).all()
    channel_groups: dict[str, dict[str, Any]] = {}
    for channel, status, error, count in notification_groups:
        channel_key = str(channel or "unknown")
        group = channel_groups.setdefault(
            channel_key,
            {"count": 0, "statuses": set(), "errors": set()},
        )
        group["count"] += int(count)
        group["statuses"].add(str(status))
        if error:
            group["errors"].add(_safe_text(error, 500))
    for channel, group in channel_groups.items():
        errors = sorted(group["errors"])
        incidents.append(
            _incident(
                kind="owner_notification_unavailable",
                resource_type="notification_channel",
                resource_id=channel,
                reason="; ".join(errors[:3]) or f"Канал {channel} не настроен",
                severity="critical",
                count=group["count"],
                credentials_required=True,
                data={"statuses": sorted(group["statuses"]), "notification_count": group["count"]},
            )
        )

    agent_states = db.scalars(
        select(AgentState).where(
            (AgentState.status == "error") | (AgentState.last_error != "")
        )
    ).all()
    for state in agent_states:
        if state.agent_type == "system_admin":
            continue
        incidents.append(
            _incident(
                kind="component_error",
                resource_type="agent_state",
                resource_id=state.agent_type,
                reason=state.last_error or f"component status: {state.status}",
                severity="critical",
                data={"component": state.agent_type, "status": state.status},
            )
        )
    return sorted(
        incidents,
        key=lambda item: (item["severity"] != "critical", item["kind"], item["resource_id"]),
    )


def _assessment(incident: dict[str, Any]) -> dict[str, Any]:
    credentials_required = bool(incident["credentials_required"])
    return {
        "reason": incident["reason"],
        "suggested_function": (
            "Восстановить обязательные credentials и добавить безопасную проверку доступности"
            if credentials_required
            else "Устранить техническую причину и добавить регрессионную проверку"
        ),
        "missing_capabilities": [
            "external_credentials" if credentials_required else "verified_runtime_recovery"
        ],
        "acceptance_criteria": [
            "Системный агент больше не обнаруживает исходное условие ошибки.",
            "Повторная проверка сохраняется в audit log с результатом исправлено или не исправлено.",
            "Владелец получает отчёт без раскрытия credentials и персональных данных.",
            "Защитные подтверждения и лимиты бизнес-действий не обходятся.",
        ],
        "test_plan": [
            "Воспроизвести исходный технический сбой.",
            "Проверить дедупликацию инцидента и связанного improvement.",
            "Устранить условие и запустить повторную проверку.",
            "Запустить полный pytest, миграции и Docker health checks.",
        ],
    }


def _upsert_incident_improvement(
    db: Session,
    incident: dict[str, Any],
    *,
    now: datetime,
) -> tuple[ImprovementRequest, str]:
    row = db.scalar(
        select(ImprovementRequest).where(ImprovementRequest.dedup_key == incident["fingerprint"])
    )
    assessment = _assessment(incident)
    signature = hashlib.sha256(incident["reason"].encode()).hexdigest()
    state = "ongoing"
    should_handoff = False
    if row is None:
        request_text = (
            f"System administrator incident: {incident['kind']} in "
            f"{incident['resource_type']} #{incident['resource_id']}. Reason: {incident['reason']}"
        )
        row = ImprovementRequest(
            dedup_key=incident["fingerprint"],
            source_channel="system",
            source_user="system_admin",
            request_text=request_text,
            intent={
                "kind": "system_incident",
                "incident_kind": incident["kind"],
                "resource_type": incident["resource_type"],
                "resource_id": incident["resource_id"],
                "failure_signature": signature,
                "first_detected_at": now.isoformat(),
            },
            capability_score=0.0,
            classification=(
                "configuration_required"
                if incident["credentials_required"]
                else "execution_gap"
            ),
            reason=incident["reason"],
            missing_capabilities=assessment["missing_capabilities"],
            suggested_function=assessment["suggested_function"],
            codex_prompt=build_codex_prompt(request_text, assessment),
            acceptance_criteria=assessment["acceptance_criteria"],
            test_plan=assessment["test_plan"],
        )
        db.add(row)
        db.flush()
        row.codex_prompt = build_codex_prompt(request_text, assessment, row.id)
        state = "new"
        should_handoff = True
    else:
        previous_signature = str((row.intent or {}).get("failure_signature") or "")
        if row.status == "implemented":
            row.status = "queued"
            row.handoff_status = "pending"
            row.occurrence_count += 1
            state = "regression"
            should_handoff = True
        elif previous_signature and previous_signature != signature:
            row.occurrence_count += 1
            state = "changed"
        row.intent = {
            **(row.intent or {}),
            "failure_signature": signature,
            "last_detected_at": now.isoformat(),
        }
        row.reason = incident["reason"]
        row.updated_at = now
    if should_handoff:
        retry_workspace_handoff(row)
    return row, state


def _resolve_cleared_incidents(
    db: Session,
    *,
    active_fingerprints: set[str],
    now: datetime,
) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(ImprovementRequest).where(
            ImprovementRequest.source_user == "system_admin",
            ImprovementRequest.status.not_in(["implemented", "rejected"]),
        )
    ).all()
    resolved: list[dict[str, Any]] = []
    for row in rows:
        if row.dedup_key in active_fingerprints:
            continue
        intent = row.intent or {}
        resource_type = str(intent.get("resource_type") or "")
        resource_id = str(intent.get("resource_id") or "")
        if not resource_type or not resource_id:
            continue
        evidence = {
            "check": "system_admin_recheck",
            "result": "condition_clear",
            "checked_at": now.isoformat(),
            "resource_type": resource_type,
            "resource_id": resource_id,
        }
        row.status = "implemented"
        row.implementation_summary = (
            "Автоматическая повторная проверка подтвердила, что исходное условие ошибки больше не наблюдается."
        )
        row.test_evidence = [*(row.test_evidence or []), evidence][-20:]
        row.last_error = ""
        row.updated_at = now
        db.add(
            AuditLog(
                actor="system_admin",
                action="system.incident_resolved",
                resource_type=resource_type[:64],
                resource_id=resource_id[:128],
                details={"improvement_id": row.id, "verification": "condition_clear"},
            )
        )
        resolved.append(
            {
                "improvement_id": row.id,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "verification_status": "fixed",
            }
        )
    return resolved


def _recent_request_trace(db: Session, *, now: datetime, limit: int = 10) -> list[dict[str, Any]]:
    runs = db.scalars(
        select(AgentRun)
        .where(
            AgentRun.agent_type == "request_analyst",
            AgentRun.started_at >= now - timedelta(hours=24),
        )
        .order_by(AgentRun.id.desc())
        .limit(limit)
    ).all()
    trace: list[dict[str, Any]] = []
    for run in runs:
        intent = (run.input or {}).get("intent") or {}
        output = run.output or {}
        trace.append(
            {
                "task_id": run.task_id,
                "request": _safe_text((run.input or {}).get("message"), 300),
                "intent": str(intent.get("kind") or "unknown")[:64],
                "classification": str(output.get("classification") or run.status)[:64],
                "improvement_id": output.get("improvement_id"),
                "execution_status": run.status,
            }
        )
    return trace


def _notification_body(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "Системный администратор завершил проверку.",
        f"Активных ошибок: {summary['active']}; новых: {summary['new']}; исправлено: {summary['resolved']}.",
    ]
    for incident in report["incidents"][:5]:
        lines.append(
            f"• {incident['resource_type']} #{incident['resource_id']}: "
            f"{incident['reason']} — improvement #{incident['improvement_id']} "
            f"({incident['verification_status']})."
        )
    if len(report["incidents"]) > 5:
        lines.append(f"Ещё инцидентов: {len(report['incidents']) - 5}.")
    lines.append("Критические бизнес-действия и повторная рассылка автоматически не выполнялись.")
    return "\n".join(lines)


def run_system_admin_audit(
    db: Session,
    *,
    now: datetime | None = None,
    stale_task_minutes: int = 15,
    notify_owner: bool = False,
    notification_idempotency_key: str = "",
) -> dict[str, Any]:
    now = now or now_utc()
    incidents = collect_incidents(
        db,
        now=now,
        stale_task_minutes=stale_task_minutes,
    )
    report_incidents: list[dict[str, Any]] = []
    state_counts: dict[str, int] = {"new": 0, "ongoing": 0, "changed": 0, "regression": 0}
    for incident in incidents:
        improvement, state = _upsert_incident_improvement(db, incident, now=now)
        state_counts[state] = state_counts.get(state, 0) + 1
        if state in {"new", "changed", "regression"}:
            db.add(
                AuditLog(
                    actor="system_admin",
                    action="system.incident_detected",
                    resource_type=incident["resource_type"][:64],
                    resource_id=incident["resource_id"][:128],
                    details={
                        "incident_kind": incident["kind"],
                        "severity": incident["severity"],
                        "count": incident["count"],
                        "improvement_id": improvement.id,
                        "verification_status": state,
                    },
                )
            )
        report_incidents.append(
            {
                **incident,
                "improvement_id": improvement.id,
                "improvement_status": improvement.status,
                "handoff_status": improvement.handoff_status,
                "verification_status": "not_fixed" if state == "ongoing" else state,
                "responsible_party": (
                    "owner_configuration"
                    if incident["credentials_required"]
                    else "system_codex"
                ),
            }
        )
    active_fingerprints = {incident["fingerprint"] for incident in incidents}
    resolved = _resolve_cleared_incidents(
        db,
        active_fingerprints=active_fingerprints,
        now=now,
    )
    recent_requests = _recent_request_trace(db, now=now)
    report = {
        "outcome": "completed",
        "report_kind": "system_admin",
        "generated_at": now.isoformat(),
        "overall_status": "degraded" if incidents else "healthy",
        "summary": {
            "active": len(incidents),
            "critical": sum(item["severity"] == "critical" for item in incidents),
            "new": state_counts.get("new", 0) + state_counts.get("regression", 0),
            "changed": state_counts.get("changed", 0),
            "resolved": len(resolved),
        },
        "incidents": report_incidents,
        "resolved": resolved,
        "recent_requests": recent_requests,
        "command_catalog": COMMAND_CATALOG,
        "safety": {
            "automatic_business_retry": False,
            "credentials_redacted": True,
            "owner_approval_preserved": True,
        },
        "evidence": [
            {"type": "structured_audit_cycle", "active_incidents": len(incidents)},
            {"type": "request_trace", "requests_checked": len(recent_requests)},
        ],
    }
    if notify_owner and (incidents or resolved):
        notification = queue_owner_notification(
            db,
            idempotency_key=(
                notification_idempotency_key
                or f"system-admin-report:{now.isoformat(timespec='minutes')}:telegram"
            ),
            channel="telegram",
            resource_type="system_admin_report",
            resource_id=now.isoformat(timespec="minutes"),
            subject="🛡 Отчёт системного администратора",
            body=_notification_body(report),
            data={
                "report_kind": "system_admin",
                "active": len(incidents),
                "resolved": len(resolved),
            },
            severity="critical" if report["summary"]["critical"] else "high",
            correlation_id=f"system-admin:{now.isoformat(timespec='minutes')}",
        )
        report["owner_notification"] = notification.status
        report["owner_notification_id"] = notification.id
    db.add(
        AuditLog(
            actor="system_admin",
            action="system.audit_completed",
            resource_type="system",
            resource_id="cleaningaios",
            details={**report["summary"], "overall_status": report["overall_status"]},
        )
    )
    return report


def record_component_failure(
    db: Session,
    *,
    component: str,
    error: Exception | str,
    now: datetime | None = None,
) -> None:
    now = now or now_utc()
    safe_error = _safe_text(error, 1000) or type(error).__name__
    state = db.get(AgentState, component) or AgentState(agent_type=component)
    changed = state.status != "error" or state.last_error != safe_error
    metrics = dict(state.metrics or {})
    metrics["failure_count"] = int(metrics.get("failure_count", 0)) + 1
    metrics["last_failure_at"] = now.isoformat()
    state.status = "error"
    state.last_error = safe_error
    state.last_heartbeat_at = now
    state.metrics = metrics
    db.add(state)
    if changed:
        db.add(
            AuditLog(
                actor="system_admin",
                action="system.component_failed",
                resource_type="component",
                resource_id=component[:128],
                details={"error": safe_error},
            )
        )


def record_component_recovery(
    db: Session,
    *,
    component: str,
    now: datetime | None = None,
) -> None:
    state = db.get(AgentState, component)
    if state is None or state.status != "error":
        return
    now = now or now_utc()
    state.status = "idle"
    state.last_error = ""
    state.last_heartbeat_at = now
    state.metrics = {**(state.metrics or {}), "last_recovered_at": now.isoformat()}
    db.add(
        AuditLog(
            actor="system_admin",
            action="system.component_recovered",
            resource_type="component",
            resource_id=component[:128],
            details={"verification": "successful_cycle"},
        )
    )
