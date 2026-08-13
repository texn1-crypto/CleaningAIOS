from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import case, select
from sqlalchemy.orm import Session

from .models import AgentRun, AuditLog, Task
from .platform import agent_runtime, decision_engine, event_bus
from .task_state import record_task_created, transition_task


def audit(db: Session, actor: str, action: str, resource_type: str, resource_id: str = "", details: dict | None = None) -> None:
    db.add(AuditLog(actor=actor, action=action, resource_type=resource_type, resource_id=resource_id, details=details or {}))


def _event_trace(task: Task, actor: str) -> dict[str, str]:
    payload = task.payload or {}
    return {
        "actor": actor,
        "correlation_id": str(payload.get("correlation_id") or f"task:{task.id}"),
        "causation_id": str(payload.get("event_uid") or payload.get("causation_id") or ""),
    }


def _execution_gap(task: Task, result: dict) -> tuple[str, bool] | None:
    payload = task.payload or {}
    source = str(payload.get("source", ""))
    if source not in {"telegram_natural_language", "telegram_document"}:
        return None
    status = str(result.get("status", ""))
    required = result.get("credentials_required")
    if required or status in {"adapter_required", "credentials_required", "configuration_required"}:
        return str(result.get("reason") or "Для полного выполнения не настроены обязательные credentials или внешний адаптер."), True
    evidence = result.get("evidence") if isinstance(result.get("evidence"), list) else []
    message = str(payload.get("original_message") or payload.get("request_text") or "").lower()
    execution_request = any(word in message for word in ("сделай", "подготов", "создай", "собери", "найди", "отправ", "опублик", "измени"))
    concrete_result = bool(
        result.get("download_url")
        or result.get("download_urls")
        or result.get("files")
        or result.get("created")
        or result.get("record_id")
        or result.get("proposal_id")
        or result.get("proposal_revision_id")
    )
    requested_files = any(token in message for token in ("pdf", "xls", "xlsx", "word", "docx"))
    artifact_types = {"proposal_pdf", "file_export", "document_export", "dataset_export"}
    artifact_evidence = any(
        isinstance(item, dict) and item.get("type") in artifact_types
        for item in evidence
    )
    if requested_files and not concrete_result and not artifact_evidence:
        return "Запрошены файлы или экспорт данных, но агент не создал ни одного проверяемого артефакта.", False
    if execution_request and not evidence and not concrete_result:
        return "Агент завершил технический запуск, но не предоставил проверяемый бизнес-результат или доказательство выполнения.", False
    return None


def _escalate_execution_gap(db: Session, task: Task, reason: str, *, credentials_required: bool) -> dict:
    from .improvements import record_execution_gap

    escalation = record_execution_gap(db, task, reason, credentials_required=credentials_required)
    title = f"Инцидент агента {task.agent_type} по задаче #{task.id}"
    existing = db.scalar(select(Task).where(Task.agent_type == "ceo", Task.title == title))
    if existing:
        escalation["ceo_incident_task_id"] = existing.id
        return escalation
    incident = Task(
        title=title,
        agent_type="ceo",
        priority="high",
        payload={
            "action": "agent_incident_report",
            "source": "agent_incident",
            "source_task_id": task.id,
            "failed_agent_type": task.agent_type,
            "failure_reason": reason,
            **escalation,
        },
        max_attempts=1,
    )
    db.add(incident)
    db.flush()
    record_task_created(db, incident, actor="orchestrator", reason="agent_incident_created", correlation_id=_event_trace(task, "ceo")["correlation_id"])
    incident_result = agent_runtime.execute(db, incident)
    audit(db, "ceo", "agent.incident_reported", "task", str(task.id), incident_result)
    event_bus.publish(
        db,
        "agent.incident_reported",
        "task",
        str(task.id),
        incident_result,
        idempotency_key=f"task:{task.id}:agent-incident",
        **_event_trace(task, "ceo"),
    )
    escalation["ceo_incident_task_id"] = incident.id
    return escalation


def _mark_latest_run_incomplete(db: Session, task: Task, reason: str) -> None:
    run = db.scalar(select(AgentRun).where(AgentRun.task_id == task.id).order_by(AgentRun.id.desc()))
    if run:
        run.status = "incomplete"
        run.error = reason[:4000]


def dispatch(db: Session, task: Task) -> dict:
    record_task_created(
        db,
        task,
        actor="orchestrator",
        reason="dispatch_discovered",
        correlation_id=str((task.payload or {}).get("correlation_id", "")),
    )
    policy = decision_engine.evaluate(db, task)
    if not policy["allowed"]:
        transition_task(
            db,
            task,
            "blocked",
            actor="decision_engine",
            reason="owner_approval_required",
            correlation_id=_event_trace(task, "decision_engine")["correlation_id"],
            transition_key=f"task:{task.id}:approval:{policy['approval_id']}:blocked",
        )
        result = {"blocked": True, **policy}
        task.result = result
        audit(db, "decision_engine", "task.blocked", "task", str(task.id), result)
        event_bus.publish(db, "approval.requested", "task", str(task.id), result, idempotency_key=f"task:{task.id}:approval:{policy['approval_id']}", **_event_trace(task, "decision_engine"))
        return result
    try:
        result = agent_runtime.execute(db, task, finalize_task=False)
        gap = _execution_gap(task, result)
        if gap:
            reason, credentials_required = gap
            escalation = _escalate_execution_gap(db, task, reason, credentials_required=credentials_required)
            transition_task(
                db,
                task,
                "blocked",
                actor="orchestrator_quality_gate",
                reason="execution_evidence_missing",
                correlation_id=_event_trace(task, "orchestrator_quality_gate")["correlation_id"],
                details={"execution_gap": reason},
                transition_key=f"task:{task.id}:attempt:{task.attempts}:incomplete",
            )
            task.result = {**result, "execution_gap": reason, **escalation}
            _mark_latest_run_incomplete(db, task, reason)
            audit(db, "orchestrator_quality_gate", "task.incomplete", "task", str(task.id), task.result)
            event_bus.publish(
                db,
                "task.incomplete",
                "task",
                str(task.id),
                task.result,
                idempotency_key=f"task:{task.id}:incomplete:{task.attempts}",
                **_event_trace(task, "orchestrator_quality_gate"),
            )
            return task.result
        audit(db, task.agent_type, "task.completed", "task", str(task.id), result)
        transition_task(
            db,
            task,
            "done",
            actor="orchestrator",
            reason="execution_evidence_verified",
            correlation_id=_event_trace(task, "orchestrator")["correlation_id"],
            transition_key=f"task:{task.id}:attempt:{task.attempts}:done",
        )
        event_bus.publish(db, "task.completed", "task", str(task.id), result, idempotency_key=f"task:{task.id}:completed:{task.attempts}", **_event_trace(task, task.agent_type))
        return result
    except Exception as exc:
        source = str((task.payload or {}).get("source", ""))
        if source in {"telegram_natural_language", "telegram_document"}:
            escalation = _escalate_execution_gap(db, task, str(exc), credentials_required=False)
            task.result = {**(task.result or {}), **escalation}
        audit(db, task.agent_type, "task.failed", "task", str(task.id), task.result)
        if task.attempts < task.max_attempts:
            transition_task(
                db,
                task,
                "queued",
                actor="orchestrator",
                reason="retry_scheduled",
                correlation_id=_event_trace(task, "orchestrator")["correlation_id"],
                details={"attempt": task.attempts, "max_attempts": task.max_attempts},
                transition_key=f"task:{task.id}:attempt:{task.attempts}:retry",
            )
            task.next_retry_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=min(300, 2 ** task.attempts))
            task.run_after = task.next_retry_at
            event_bus.publish(db, "task.retry_scheduled", "task", str(task.id), {"attempt": task.attempts, "max_attempts": task.max_attempts, "next_retry_at": task.next_retry_at.isoformat()}, idempotency_key=f"task:{task.id}:retry:{task.attempts}", **_event_trace(task, "orchestrator"))
        else:
            event_bus.publish(db, "task.failed", "task", str(task.id), task.result, idempotency_key=f"task:{task.id}:failed", **_event_trace(task, "orchestrator"))
        return task.result


def run_next(db: Session) -> Task | None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    priority_order = case((Task.priority == "critical", 4), (Task.priority == "high", 3), (Task.priority == "normal", 2), (Task.priority == "low", 1), else_=0)
    task = db.scalar(select(Task).where(Task.status.in_(["open", "queued"]), Task.run_after <= now).order_by(priority_order.desc(), Task.id).with_for_update(skip_locked=True))
    if task:
        dispatch(db, task)
        db.commit()
    return task
