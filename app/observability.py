from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import settings
from .models import AgentRun, AgentToolCall, ApprovalRequest, DomainEvent, Task


MAX_RUN_SAMPLE = 10_000


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _duration_seconds(run: AgentRun) -> float | None:
    if not run.finished_at:
        return None
    return max(0.0, (run.finished_at - run.started_at).total_seconds())


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return round(ordered[index], 3)


def _status_counts(db: Session, model: type[Task] | type[DomainEvent]) -> dict[str, int]:
    rows = db.execute(select(model.status, func.count(model.id)).group_by(model.status)).all()
    return {str(status): int(count) for status, count in rows}


def agent_observability_snapshot(
    db: Session,
    *,
    window_hours: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build an aggregate-only agent and workflow health snapshot."""
    generated_at = now or _utcnow()
    hours = max(1, min(int(window_hours or settings.agent_slo_window_hours), 7 * 24))
    cutoff = generated_at - timedelta(hours=hours)
    stale_before = generated_at - timedelta(minutes=settings.agent_stale_run_minutes)

    completed_filter = (
        AgentRun.started_at >= cutoff,
        AgentRun.status.in_(["succeeded", "failed"]),
    )
    completed_total = int(
        db.scalar(select(func.count(AgentRun.id)).where(*completed_filter)) or 0
    )
    completed = db.scalars(
        select(AgentRun)
        .where(*completed_filter)
        .order_by(AgentRun.id.desc())
        .limit(MAX_RUN_SAMPLE)
    ).all()
    sample_capped = completed_total > len(completed)

    stale_running = int(
        db.scalar(
            select(func.count(AgentRun.id)).where(
                AgentRun.status == "running",
                AgentRun.started_at < stale_before,
            )
        )
        or 0
    )
    running = int(
        db.scalar(
            select(func.count(AgentRun.id)).where(
                AgentRun.status == "running",
                AgentRun.started_at >= cutoff,
            )
        )
        or 0
    )

    by_agent: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"succeeded": 0, "failed": 0, "durations": [], "cost": 0.0}
    )
    durations: list[float] = []
    total_cost = 0.0
    for run in completed:
        bucket = by_agent[run.agent_type]
        bucket[run.status] += 1
        duration = _duration_seconds(run)
        if duration is not None:
            durations.append(duration)
            bucket["durations"].append(duration)
        cost = max(0.0, float(run.cost or 0))
        total_cost += cost
        bucket["cost"] += cost

    succeeded = sum(int(item["succeeded"]) for item in by_agent.values())
    failed = sum(int(item["failed"]) for item in by_agent.values())
    sample_total = succeeded + failed
    success_rate = round(succeeded * 100 / sample_total, 2) if sample_total else None
    p95_duration = _percentile(durations, 0.95)

    per_agent = []
    for agent_type, item in sorted(by_agent.items()):
        agent_total = int(item["succeeded"]) + int(item["failed"])
        per_agent.append(
            {
                "agent_type": agent_type,
                "completed": agent_total,
                "succeeded": int(item["succeeded"]),
                "failed": int(item["failed"]),
                "success_rate_percent": round(int(item["succeeded"]) * 100 / agent_total, 2),
                "p95_duration_seconds": _percentile(item["durations"], 0.95),
                "cost": round(float(item["cost"]), 6),
            }
        )

    success_objective = (
        "insufficient_data"
        if success_rate is None
        else "met"
        if success_rate >= settings.agent_slo_success_rate_percent
        else "missed"
    )
    latency_objective = (
        "insufficient_data"
        if p95_duration is None
        else "met"
        if p95_duration <= settings.agent_slo_p95_duration_seconds
        else "missed"
    )
    stale_objective = "met" if stale_running == 0 else "missed"
    objective_states = [success_objective, latency_objective, stale_objective]
    overall_status = (
        "degraded"
        if "missed" in objective_states
        else "insufficient_data"
        if "insufficient_data" in objective_states
        else "healthy"
    )

    task_statuses = _status_counts(db, Task)
    event_statuses = _status_counts(db, DomainEvent)
    pending_approvals = int(
        db.scalar(
            select(func.count(ApprovalRequest.id)).where(ApprovalRequest.status == "pending")
        )
        or 0
    )
    tool_rows = db.execute(
        select(
            AgentToolCall.tool_name,
            AgentToolCall.status,
            func.count(AgentToolCall.id),
        )
        .where(AgentToolCall.created_at >= cutoff)
        .group_by(AgentToolCall.tool_name, AgentToolCall.status)
        .order_by(AgentToolCall.tool_name, AgentToolCall.status)
    ).all()
    tool_statuses: dict[str, int] = defaultdict(int)
    per_tool: dict[str, dict[str, int]] = defaultdict(dict)
    for tool_name, status, count in tool_rows:
        value = int(count)
        tool_statuses[str(status)] += value
        per_tool[str(tool_name)][str(status)] = value
    return {
        "generated_at": generated_at.isoformat() + "Z",
        "window_hours": hours,
        "sample": {
            "completed_total": completed_total,
            "completed_evaluated": sample_total,
            "max_runs": MAX_RUN_SAMPLE,
            "capped": sample_capped,
        },
        "runs": {
            "succeeded": succeeded,
            "failed": failed,
            "running": running,
            "stale_running": stale_running,
            "success_rate_percent": success_rate,
            "p95_duration_seconds": p95_duration,
            "cost": round(total_cost, 6),
        },
        "slo": {
            "overall_status": overall_status,
            "success_rate": {
                "target_percent": settings.agent_slo_success_rate_percent,
                "actual_percent": success_rate,
                "status": success_objective,
            },
            "p95_duration": {
                "target_seconds": settings.agent_slo_p95_duration_seconds,
                "actual_seconds": p95_duration,
                "status": latency_objective,
            },
            "stale_runs": {
                "target": 0,
                "actual": stale_running,
                "status": stale_objective,
            },
        },
        "per_agent": per_agent,
        "workflow": {
            "tasks": task_statuses,
            "events": event_statuses,
            "pending_approvals": pending_approvals,
        },
        "tools": {
            "mode": "read_only",
            "total": sum(tool_statuses.values()),
            "by_status": dict(sorted(tool_statuses.items())),
            "per_tool": [
                {"tool_name": name, "statuses": dict(sorted(statuses.items()))}
                for name, statuses in sorted(per_tool.items())
            ],
        },
    }


def _label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def prometheus_metrics(snapshot: dict[str, Any]) -> str:
    """Render the aggregate snapshot in Prometheus text exposition format."""
    lines = [
        "# HELP cleaningai_agent_runs Agent runs completed in the configured window.",
        "# TYPE cleaningai_agent_runs gauge",
    ]
    for item in snapshot["per_agent"]:
        for status in ("succeeded", "failed"):
            lines.append(
                f'cleaningai_agent_runs{{agent_type="{_label(item["agent_type"])}",status="{status}"}} '
                f'{item[status]}'
            )
    runs = snapshot["runs"]
    lines.extend(
        [
            "# HELP cleaningai_agent_success_rate_percent Agent run success rate in the configured window.",
            "# TYPE cleaningai_agent_success_rate_percent gauge",
            f'cleaningai_agent_success_rate_percent {runs["success_rate_percent"] if runs["success_rate_percent"] is not None else "NaN"}',
            "# HELP cleaningai_agent_p95_duration_seconds Agent run p95 duration in seconds.",
            "# TYPE cleaningai_agent_p95_duration_seconds gauge",
            f'cleaningai_agent_p95_duration_seconds {runs["p95_duration_seconds"] if runs["p95_duration_seconds"] is not None else "NaN"}',
            "# HELP cleaningai_agent_stale_runs Agent runs stuck in running state beyond the threshold.",
            "# TYPE cleaningai_agent_stale_runs gauge",
            f'cleaningai_agent_stale_runs {runs["stale_running"]}',
            "# HELP cleaningai_pending_approvals Owner approvals currently pending.",
            "# TYPE cleaningai_pending_approvals gauge",
            f'cleaningai_pending_approvals {snapshot["workflow"]["pending_approvals"]}',
        ]
    )
    for family, statuses in (
        ("tasks", snapshot["workflow"]["tasks"]),
        ("events", snapshot["workflow"]["events"]),
    ):
        lines.extend(
            [
                f"# HELP cleaningai_{family} Current {family} by status.",
                f"# TYPE cleaningai_{family} gauge",
            ]
        )
        for status, count in sorted(statuses.items()):
            lines.append(f'cleaningai_{family}{{status="{_label(status)}"}} {count}')
    lines.extend(
        [
            "# HELP cleaningai_agent_tool_calls Read-only agent tool calls in the configured window.",
            "# TYPE cleaningai_agent_tool_calls gauge",
        ]
    )
    for item in snapshot["tools"]["per_tool"]:
        for status, count in item["statuses"].items():
            lines.append(
                f'cleaningai_agent_tool_calls{{tool_name="{_label(item["tool_name"])}",status="{_label(status)}"}} {count}'
            )
    return "\n".join(lines) + "\n"
