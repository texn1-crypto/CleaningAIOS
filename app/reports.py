from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from .models import AgentRun, AgentState, ApprovalRequest, DomainEvent, ImprovementRequest, Task
from .readiness import integration_status


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _count(db: Session, model: type, *criteria: Any) -> int:
    return int(db.scalar(select(func.count()).select_from(model).where(*criteria)) or 0)


def build_activity_report(db: Session, *, period_hours: int = 24) -> dict[str, Any]:
    """Build a verifiable read-only report from the shared operational database."""
    hours = max(1, min(int(period_hours), 168))
    generated_at = _utcnow()
    cutoff = generated_at - timedelta(hours=hours)

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

    active_total = _count(db, Task, Task.status.in_(["open", "queued", "running"]))
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
        "queued_improvements": _count(db, ImprovementRequest, ImprovementRequest.status == "queued"),
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
    return {
        "outcome": "completed",
        "report_kind": "system_activity",
        "period_hours": hours,
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
        "agent_statuses": agent_statuses,
        "blockers": blockers,
        "evidence": [
            {
                "type": "database_snapshot",
                "generated_at": generated_at.isoformat(),
                "period_hours": hours,
                **summary,
            },
            *task_evidence,
        ],
    }


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
        "Sales/CRM": "sales",
        "HR": "hr",
        "Finance": "finance",
        "Marketing/SMM": "marketing",
        "Meta Brain": "meta_brain",
        "Request Analyst": "request_analyst",
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
        credentials=[] if llm_ready else ["LLM_API_KEY"],
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
