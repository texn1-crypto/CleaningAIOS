import logging
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from .config import settings
from .db import SessionLocal
from .models import BusinessRecord, OperatingEntity, Task
from .notifications import queue_missing_approval_notifications
from .operations import maintain_ceo_development_backlog
from .platform import event_bus
from .task_state import record_task_created

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("cleaningai.scheduler")


def owner_report_window(now: datetime, interval_minutes: int) -> datetime:
    """Return a UTC-aligned window start for any supported interval."""
    utc_now = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
    minute = int(utc_now.timestamp() // 60)
    window_minute = minute - minute % interval_minutes
    return datetime.fromtimestamp(window_minute * 60, tz=timezone.utc).replace(tzinfo=None)


def schedule_cycle() -> None:
    with SessionLocal() as db:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        maintain_ceo_development_backlog(
            db,
            now=now,
            cadence_hours=settings.ceo_development_cadence_hours,
        )
        queue_missing_approval_notifications(db)
        social_day = now.date().isoformat()
        social_title = f"Daily cleaning news social plan · {social_day}"
        if not db.scalar(select(Task.id).where(Task.title == social_title)):
            task = Task(
                title=social_title,
                agent_type="marketing",
                status="queued",
                priority="high",
                run_after=now,
                max_attempts=3,
                payload={"action": "prepare_daily_cleaning_news_plan", "day": now.isoformat(), "source": "scheduler"},
            )
            db.add(task)
            db.flush()
            record_task_created(db, task, actor="scheduler", reason="daily_cleaning_news_social_plan")
        tender_sources = [source.strip() for source in settings.tender_sources.split(",") if source.strip()]
        tender_interval = max(5, min(settings.tender_monitor_interval_minutes, 24 * 60))
        tender_recent_since = now - timedelta(minutes=tender_interval)
        tender_monitor_active = db.scalar(
            select(Task.id).where(
                Task.agent_type == "research",
                Task.status.in_(["open", "queued", "running"]),
                Task.title.like("Tender source monitoring · %"),
            )
        )
        tender_monitor_recent = db.scalar(
            select(Task.id).where(
                Task.agent_type == "research",
                Task.created_at >= tender_recent_since,
                Task.title.like("Tender source monitoring · %"),
            )
        )
        if tender_sources and not tender_monitor_active and not tender_monitor_recent:
            task = Task(
                title=f"Tender source monitoring · {now.isoformat(timespec='minutes')}",
                agent_type="research",
                status="queued",
                priority="high",
                run_after=now,
                max_attempts=3,
                payload={"collection": "tenders", "sources": tender_sources, "source": "scheduler"},
            )
            db.add(task)
            db.flush()
            record_task_created(db, task, actor="scheduler", reason="recurring_tender_source_monitoring")
        system_admin_interval = max(
            1,
            min(settings.system_admin_interval_minutes, 24 * 60),
        )
        system_admin_window = owner_report_window(now, system_admin_interval)
        system_admin_report_interval = max(
            system_admin_interval,
            min(settings.system_admin_report_interval_minutes, 24 * 60),
        )
        system_admin_report_window = owner_report_window(
            now,
            system_admin_report_interval,
        )
        system_admin_title = (
            f"System administrator audit · {system_admin_window.isoformat()}"
        )
        if not db.scalar(select(Task.id).where(Task.title == system_admin_title)):
            task = Task(
                title=system_admin_title,
                agent_type="system_admin",
                status="queued",
                priority="critical",
                run_after=now,
                max_attempts=3,
                payload={
                    "action": "system_admin_audit",
                    "source": "scheduler",
                    "notify_owner": True,
                    "stale_task_minutes": settings.system_admin_stale_task_minutes,
                    "scheduled_window_start": system_admin_window.isoformat(),
                    "notification_idempotency_key": (
                        f"system-admin-report:{system_admin_report_window.isoformat()}:telegram"
                    ),
                },
            )
            db.add(task)
            db.flush()
            record_task_created(
                db,
                task,
                actor="scheduler",
                reason="recurring_system_admin_audit",
            )
        report_interval = max(5, min(settings.owner_activity_report_interval_minutes, 24 * 60))
        report_window = owner_report_window(now, report_interval)
        coordination_title = f"Marketing/Sales coordination · {report_window.isoformat()}"
        if not db.scalar(select(Task.id).where(Task.title == coordination_title)):
            task = Task(
                title=coordination_title,
                agent_type="orchestrator",
                status="queued",
                priority="high",
                run_after=now,
                max_attempts=3,
                payload={
                    "action": "marketing_sales_coordination",
                    "period_minutes": report_interval,
                    "source": "scheduler",
                    "scheduled_window_start": report_window.isoformat(),
                },
            )
            db.add(task)
            db.flush()
            record_task_created(
                db,
                task,
                actor="scheduler",
                reason="recurring_marketing_sales_coordination",
            )
        report_key = f"owner-activity-report:{report_window.isoformat()}"
        report_title = f"Регулярный отчёт владельцу · {report_window.isoformat()}"
        if not db.scalar(select(Task.id).where(Task.title == report_title)):
            task = Task(
                title=report_title,
                agent_type="orchestrator",
                status="queued",
                priority="high",
                run_after=now,
                max_attempts=3,
                payload={
                    "action": "system_activity_report",
                    "period_minutes": report_interval,
                    "source": "scheduler",
                    "notify_owner": True,
                    "scheduled_window_start": report_window.isoformat(),
                    "notification_idempotency_key": report_key,
                },
            )
            db.add(task)
            db.flush()
            record_task_created(db, task, actor="scheduler", reason="recurring_owner_activity_report")
        perplexity_interval = max(
            5,
            min(settings.perplexity_coach_interval_minutes, 24 * 60),
        )
        perplexity_window = owner_report_window(now, perplexity_interval)
        perplexity_title = f"Perplexity agent coaching · {perplexity_window.isoformat()}"
        perplexity_active = db.scalar(
            select(Task.id).where(
                Task.agent_type == "meta_brain",
                Task.status.in_(["open", "queued", "running"]),
                Task.title.like("Perplexity agent coaching · %"),
            )
        )
        if (
            settings.perplexity_api_key
            and not perplexity_active
            and not db.scalar(select(Task.id).where(Task.title == perplexity_title))
        ):
            task = Task(
                title=perplexity_title,
                agent_type="meta_brain",
                status="queued",
                priority="high",
                run_after=now,
                max_attempts=3,
                payload={
                    "action": "perplexity_agent_coaching",
                    "source": "scheduler",
                    "advisory_only": True,
                    "period_minutes": perplexity_interval,
                    "scheduled_window_start": perplexity_window.isoformat(),
                },
            )
            db.add(task)
            db.flush()
            record_task_created(
                db,
                task,
                actor="scheduler",
                reason="recurring_perplexity_agent_coaching",
            )
        evolution_queries = [
            item.strip()
            for item in settings.evolution_research_queries.split("|")
            if item.strip()
        ]
        evolution_local_now = now.replace(tzinfo=timezone.utc).astimezone(
            ZoneInfo(settings.evolution_research_timezone)
        )
        evolution_day = evolution_local_now.date().isoformat()
        evolution_title = f"AI evolution research · {evolution_day}"
        evolution_active = db.scalar(
            select(Task.id).where(
                Task.agent_type == "evolution_researcher",
                Task.status.in_(["open", "queued", "running"]),
            )
        )
        if (
            settings.perplexity_api_key
            and evolution_queries
            and evolution_local_now.hour >= max(0, min(settings.evolution_research_daily_hour, 23))
            and not evolution_active
            and not db.scalar(select(Task.id).where(Task.title == evolution_title))
        ):
            task = Task(
                title=evolution_title,
                agent_type="evolution_researcher",
                status="queued",
                priority="high",
                run_after=now,
                max_attempts=3,
                payload={
                    "action": "daily_source_grounded_evolution_research",
                    "source": "scheduler",
                    "advisory_only": True,
                    "notify_owner": True,
                    "research_at": now.isoformat(),
                    "scheduled_local_day": evolution_day,
                },
            )
            db.add(task)
            db.flush()
            record_task_created(
                db,
                task,
                actor="scheduler",
                reason="daily_source_grounded_evolution_research",
            )
        active = db.scalar(select(Task.id).where(Task.agent_type == "ceo", Task.status.in_(["open", "queued", "running"])))
        recent = db.scalar(select(Task.id).where(Task.agent_type == "ceo", Task.created_at >= now - timedelta(hours=settings.ceo_review_interval_hours)))
        if not active and not recent:
            task = Task(title="AI CEO business review", agent_type="ceo", status="queued", priority="high", run_after=now)
            db.add(task); db.flush(); record_task_created(db, task, actor="scheduler", reason="recurring_ceo_review")
        growth_active = db.scalar(select(Task.id).where(Task.agent_type == "growth_officer", Task.status.in_(["open", "queued", "running"])))
        growth_recent = db.scalar(select(Task.id).where(Task.agent_type == "growth_officer", Task.created_at >= now - timedelta(hours=settings.growth_review_interval_hours)))
        if not growth_active and not growth_recent:
            task = Task(
                title=f"Growth Officer review · {now.date().isoformat()}",
                agent_type="growth_officer",
                status="queued",
                priority="critical",
                run_after=now,
                max_attempts=3,
                payload={"action": "billion_revenue_review", "review_at": now.isoformat(), "source": "scheduler"},
            )
            db.add(task); db.flush(); record_task_created(db, task, actor="scheduler", reason="recurring_billion_revenue_review")
        tenders = db.scalars(select(BusinessRecord).where(BusinessRecord.record_type == "tender", BusinessRecord.deadline_at.is_not(None), BusinessRecord.deadline_at <= now + timedelta(days=3), BusinessRecord.status.not_in(["submitted", "won", "lost", "expired"]))).all()
        payments = db.scalars(select(BusinessRecord).where(BusinessRecord.record_type == "payment", BusinessRecord.deadline_at.is_not(None), BusinessRecord.deadline_at < now, BusinessRecord.status.not_in(["paid", "cancelled"]))).all()
        shifts = db.scalars(select(OperatingEntity).where(OperatingEntity.entity_type == "shift", OperatingEntity.started_at.is_not(None), OperatingEntity.started_at <= now + timedelta(hours=24), OperatingEntity.status.not_in(["completed", "cancelled"]))).all()
        for record, agent, reason in [(x, "tender", "deadline_within_3_days") for x in tenders] + [(x, "finance", "payment_overdue") for x in payments]:
            title = f"{reason}: {record.title}"
            if not db.scalar(select(Task.id).where(Task.title == title, Task.status.in_(["open", "queued", "running"]))):
                task = Task(title=title, agent_type=agent, priority="high", payload={"record_id": record.id, "reason": reason})
                db.add(task); db.flush(); record_task_created(db, task, actor="scheduler", reason=reason)
                event_bus.publish(db, reason.replace("_", "."), record.record_type, str(record.id), {"deadline_at": record.deadline_at.isoformat() if record.deadline_at else None}, idempotency_key=f"watch:{reason}:{record.id}:{now.date()}")
        for shift in shifts:
            if shift.data.get("employee_id"): continue
            title = f"unfilled_shift: {shift.name}"
            if not db.scalar(select(Task.id).where(Task.title == title, Task.status.in_(["open", "queued", "running"]))):
                task = Task(title=title, agent_type="hr", priority="high", payload={"shift_id": shift.id, "site_id": shift.parent_id, "reason": "unfilled_shift"})
                db.add(task); db.flush(); record_task_created(db, task, actor="scheduler", reason="unfilled_shift")
        db.commit()


def main() -> None:
    log.info("scheduler started")
    while True:
        try:
            schedule_cycle()
            with SessionLocal() as db:
                from .system_admin import record_component_recovery

                record_component_recovery(db, component="scheduler")
                db.commit()
        except Exception as exc:
            log.exception("scheduler cycle failed")
            try:
                with SessionLocal() as db:
                    from .system_admin import record_component_failure

                    record_component_failure(db, component="scheduler", error=exc)
                    db.commit()
            except Exception:
                log.exception("system administrator could not persist scheduler failure")
        time.sleep(settings.scheduler_interval_seconds)


if __name__ == "__main__":
    main()
