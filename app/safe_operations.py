from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .marketing_coordination import run_marketing_sales_coordination
from .models import ApprovalRequest, AuditLog, OutboundMessage, Task
from .reports import build_activity_report, build_system_self_check


SAFE_ACTIONS = {
    ("marketing", "prepare_lead_generation_strategy"),
    ("sales", "prepare_management_company_outreach"),
    ("sales", "prepare_lead_follow_up"),
}
SAFE_COLLECTIONS = {
    ("research", "management_company_contacts"),
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _count(db: Session, model: type) -> int:
    return int(db.scalar(select(func.count()).select_from(model)) or 0)


def _is_safe_coordination_task(task: Task) -> bool:
    payload = task.payload or {}
    if payload.get("source") != "marketing_sales_coordination":
        return False
    if payload.get("action_kind") or payload.get("external_send") is True:
        return False
    if payload.get("external_publish") is True or payload.get("automatic_send") is True:
        return False
    action_key = (task.agent_type, str(payload.get("action") or ""))
    collection_key = (task.agent_type, str(payload.get("collection") or ""))
    return action_key in SAFE_ACTIONS or collection_key in SAFE_COLLECTIONS


def run_safe_operations_cycle(
    db: Session,
    *,
    registered_agents: list[str],
) -> dict[str, Any]:
    """Execute only audited internal work that cannot commit the owner externally."""
    from .orchestrator import dispatch

    now = _utcnow()
    outbound_before = _count(db, OutboundMessage)
    approvals_before = _count(db, ApprovalRequest)
    readiness = build_system_self_check(db, registered_agents=registered_agents)
    coordination = run_marketing_sales_coordination(
        db,
        scheduled_window_start=now.isoformat(),
        period_minutes=30,
    )

    processes: list[dict[str, Any]] = [
        {
            "name": "system_readiness",
            "status": readiness["overall_status"],
            "detail": (
                f"Готово модулей: {readiness['summary']['ready']} "
                f"из {readiness['summary']['total']}."
            ),
        },
        {
            "name": "marketing_sales_coordination",
            "status": coordination["outcome"],
            "detail": (
                f"Согласовано действий: {len(coordination.get('actions') or [])}; "
                "автоматическая отправка отключена."
            ),
        },
    ]

    seen_task_ids: set[int] = set()
    for action in coordination.get("actions") or []:
        task_id = action.get("task_id")
        if not task_id or int(task_id) in seen_task_ids:
            continue
        seen_task_ids.add(int(task_id))
        task = db.get(Task, int(task_id))
        if task is None:
            processes.append(
                {
                    "name": "coordination_action",
                    "task_id": int(task_id),
                    "status": "not_found",
                    "detail": "Связанная задача не найдена; повторный запуск не выполнялся.",
                }
            )
            continue
        if not _is_safe_coordination_task(task):
            processes.append(
                {
                    "name": "coordination_action",
                    "task_id": task.id,
                    "agent_type": task.agent_type,
                    "status": "not_executed",
                    "detail": "Действие не входит в явный список безопасного автономного цикла.",
                }
            )
            continue
        if task.status in {"open", "queued"}:
            dispatch(db, task)
        result = task.result or {}
        processes.append(
            {
                "name": "coordination_action",
                "task_id": task.id,
                "agent_type": task.agent_type,
                "status": task.status,
                "result_status": result.get("status") or result.get("outcome") or "recorded",
                "detail": action.get("action") or task.title,
            }
        )

    activity = build_activity_report(db, period_hours=24)
    outbound_after = _count(db, OutboundMessage)
    approvals_after = _count(db, ApprovalRequest)
    protected_operations = [
        {"name": "payments_and_transfers", "status": "not_executed", "reason": "owner_approval_required"},
        {"name": "contracts_and_signatures", "status": "not_executed", "reason": "owner_approval_required"},
        {"name": "tender_submission", "status": "not_executed", "reason": "owner_approval_required"},
        {"name": "bulk_outreach", "status": "not_executed", "reason": "owner_approval_required"},
        {"name": "social_publication_and_ad_spend", "status": "not_executed", "reason": "owner_approval_required"},
        {"name": "final_hr_decisions", "status": "not_executed", "reason": "owner_approval_required"},
    ]
    completed = sum(row["status"] in {"done", "completed", "ready"} for row in processes)
    incomplete = sum(
        row["status"] in {"failed", "blocked", "not_found", "not_executed"}
        for row in processes
    )
    result = {
        "outcome": "completed" if incomplete == 0 else "partial",
        "report_kind": "safe_operations_cycle",
        "generated_at": now.isoformat(),
        "processes": processes,
        "summary": {
            "reported": len(processes),
            "completed": completed,
            "incomplete": incomplete,
            "active_tasks": activity["summary"]["tasks_active"],
            "blocked_tasks": activity["summary"]["tasks_blocked"],
        },
        "configuration_blockers": readiness.get("credentials_required") or [],
        "protected_operations": protected_operations,
        "safety": {
            "protected_actions_executed": False,
            "external_messages_sent": False,
            "financial_commitments_created": False,
            "owner_approval_bypassed": False,
            "outbound_messages_created": max(0, outbound_after - outbound_before),
            "approval_requests_created": max(0, approvals_after - approvals_before),
        },
        "evidence": [
            {"type": "system_readiness", "status": readiness["overall_status"]},
            {
                "type": "marketing_sales_coordination",
                "status": coordination["outcome"],
                "action_task_ids": sorted(seen_task_ids),
            },
            {
                "type": "safe_execution_allowlist",
                "executed_task_ids": [
                    row["task_id"]
                    for row in processes
                    if row.get("task_id") and row["status"] == "done"
                ],
                "external_messages_sent": False,
            },
        ],
    }
    db.add(
        AuditLog(
            actor="orchestrator",
            action="safe_operations.cycle_completed",
            resource_type="safe_operations_cycle",
            resource_id=now.isoformat(),
            details={
                "outcome": result["outcome"],
                "completed": completed,
                "incomplete": incomplete,
                "task_ids": sorted(seen_task_ids),
                "external_messages_sent": False,
                "protected_actions_executed": False,
            },
        )
    )
    db.flush()
    return result
