from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import BusinessGoal, BusinessRecord, OperatingEntity, Task
from .task_state import record_task_created


GROWTH_METRIC = "annual_revenue_run_rate_rub"
GROWTH_TARGET_RUB = Decimal("1000000000")


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _money(value: object) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def actual_annual_revenue_run_rate(db: Session) -> Decimal:
    contracts = db.scalars(
        select(OperatingEntity).where(
            OperatingEntity.entity_type == "contract",
            OperatingEntity.status == "active",
        )
    ).all()
    monthly = sum((_money((row.data or {}).get("monthly_revenue")) for row in contracts), Decimal("0"))
    return monthly * Decimal("12")


def ensure_billion_revenue_goal(db: Session, *, now: datetime | None = None) -> BusinessGoal:
    current_time = now or utcnow()
    current = actual_annual_revenue_run_rate(db)
    goal = db.scalar(select(BusinessGoal).where(BusinessGoal.metric == GROWTH_METRIC).order_by(BusinessGoal.id))
    if goal is None:
        try:
            deadline = current_time.replace(year=current_time.year + 2)
        except ValueError:
            deadline = current_time.replace(year=current_time.year + 2, day=28)
        goal = BusinessGoal(
            title="Годовой оборот 1 млрд ₽",
            description="Достичь подтверждённого годового revenue run-rate 1 млрд рублей за 24 месяца.",
            owner="growth_officer",
            metric=GROWTH_METRIC,
            baseline=float(current),
            target=float(GROWTH_TARGET_RUB),
            current=float(current),
            unit="RUB/year",
            deadline_at=deadline,
            strategy={
                "interpretation": "annual_revenue_run_rate",
                "horizon_months": 24,
                "workstreams": ["sales", "marketing", "tenders", "operations", "finance", "hr"],
                "source_of_truth": "active_contracts.monthly_revenue_x12",
                "external_commitments_require_owner_approval": True,
            },
        )
        db.add(goal)
    else:
        goal.current = float(current)
    db.flush()
    return goal


def growth_snapshot(db: Session, *, goal: BusinessGoal | None = None, now: datetime | None = None) -> dict:
    current_time = now or utcnow()
    goal = goal or db.scalar(select(BusinessGoal).where(BusinessGoal.metric == GROWTH_METRIC).order_by(BusinessGoal.id))
    current = actual_annual_revenue_run_rate(db)
    if not goal:
        return {
            "status": "goal_not_initialized",
            "metric": GROWTH_METRIC,
            "target_rub": int(GROWTH_TARGET_RUB),
            "current_rub": int(current),
            "gap_rub": int(max(Decimal("0"), GROWTH_TARGET_RUB - current)),
        }
    baseline = _money(goal.baseline)
    target = _money(goal.target)
    total_seconds = max(1, (goal.deadline_at - goal.created_at).total_seconds()) if goal.deadline_at else 1
    elapsed_seconds = max(0, min(total_seconds, (current_time - goal.created_at).total_seconds()))
    expected = baseline + (target - baseline) * Decimal(str(elapsed_seconds / total_seconds))
    progress = ((current - baseline) / (target - baseline) * 100) if target > baseline else Decimal("100")
    pace_gap = current - expected
    monthly_revenue = current / Decimal("12")
    return {
        "status": "on_track" if pace_gap >= 0 else "behind_plan",
        "goal_id": goal.id,
        "metric": goal.metric,
        "deadline_at": goal.deadline_at.isoformat() if goal.deadline_at else None,
        "target_rub": int(target),
        "current_rub": int(current),
        "current_monthly_revenue_rub": int(monthly_revenue),
        "expected_today_rub": int(expected),
        "gap_to_target_rub": int(max(Decimal("0"), target - current)),
        "pace_gap_rub": int(pace_gap),
        "progress_percent": round(float(max(Decimal("0"), min(Decimal("100"), progress))), 2),
        "source": "active contract monthly_revenue fields multiplied by 12",
        "freshness_at": current_time.isoformat(),
    }


def _weekly_delegations(db: Session, snapshot: dict, *, now: datetime) -> list[Task]:
    year, week, _ = now.isocalendar()
    templates = (
        ("sales", "расширить квалифицированную воронку", "growth_sales_pipeline"),
        ("marketing", "подготовить гипотезы привлечения лидов", "growth_demand_generation"),
        ("tender", "обновить тендерную воронку", "growth_tender_pipeline"),
        ("finance", "проверить unit economics роста", "growth_unit_economics"),
        ("hr", "оценить кадровую ёмкость для роста", "growth_staff_capacity"),
    )
    created: list[Task] = []
    for agent_type, label, action in templates:
        title = f"Growth W{week:02d}/{year} · {label}"
        if db.scalar(select(Task.id).where(Task.title == title)):
            continue
        task = Task(
            title=title,
            agent_type=agent_type,
            priority="high",
            payload={
                "action": action,
                "origin": "growth_officer",
                "growth_snapshot": snapshot,
                "advisory_only": True,
                "external_actions_require_owner_approval": True,
            },
            max_attempts=3,
        )
        db.add(task)
        db.flush()
        record_task_created(db, task, actor="growth_officer", reason="weekly_billion_revenue_workstream")
        created.append(task)
    return created


def run_growth_review(db: Session, *, now: datetime | None = None) -> dict:
    current_time = now or utcnow()
    goal = ensure_billion_revenue_goal(db, now=current_time)
    snapshot = growth_snapshot(db, goal=goal, now=current_time)
    goal.current = float(snapshot["current_rub"])
    created = _weekly_delegations(db, snapshot, now=current_time) if snapshot["gap_to_target_rub"] > 0 else []
    return {
        **snapshot,
        "delegated_tasks": [{"id": row.id, "agent_type": row.agent_type, "title": row.title} for row in created],
        "owner_approval_preserved": True,
        "evidence": [{
            "type": "growth_goal_snapshot",
            "goal_id": goal.id,
            "current_rub": snapshot["current_rub"],
            "target_rub": snapshot["target_rub"],
            "source": snapshot["source"],
        }],
    }
