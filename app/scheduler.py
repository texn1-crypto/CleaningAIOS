import logging
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from .config import settings
from .db import SessionLocal
from .models import BusinessRecord, OperatingEntity, Task
from .platform import event_bus

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("cleaningai.scheduler")


def schedule_cycle() -> None:
    with SessionLocal() as db:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        active = db.scalar(select(Task.id).where(Task.agent_type == "ceo", Task.status.in_(["open", "queued", "running"])))
        recent = db.scalar(select(Task.id).where(Task.agent_type == "ceo", Task.created_at >= now - timedelta(hours=settings.ceo_review_interval_hours)))
        if not active and not recent:
            db.add(Task(title="AI CEO business review", agent_type="ceo", status="queued", priority="high", run_after=now))
        tenders = db.scalars(select(BusinessRecord).where(BusinessRecord.record_type == "tender", BusinessRecord.deadline_at.is_not(None), BusinessRecord.deadline_at <= now + timedelta(days=3), BusinessRecord.status.not_in(["submitted", "won", "lost", "expired"]))).all()
        payments = db.scalars(select(BusinessRecord).where(BusinessRecord.record_type == "payment", BusinessRecord.deadline_at.is_not(None), BusinessRecord.deadline_at < now, BusinessRecord.status.not_in(["paid", "cancelled"]))).all()
        shifts = db.scalars(select(OperatingEntity).where(OperatingEntity.entity_type == "shift", OperatingEntity.started_at.is_not(None), OperatingEntity.started_at <= now + timedelta(hours=24), OperatingEntity.status.not_in(["completed", "cancelled"]))).all()
        for record, agent, reason in [(x, "tender", "deadline_within_3_days") for x in tenders] + [(x, "finance", "payment_overdue") for x in payments]:
            title = f"{reason}: {record.title}"
            if not db.scalar(select(Task.id).where(Task.title == title, Task.status.in_(["open", "queued", "running"]))):
                db.add(Task(title=title, agent_type=agent, priority="high", payload={"record_id": record.id, "reason": reason}))
                event_bus.publish(db, reason.replace("_", "."), record.record_type, str(record.id), {"deadline_at": record.deadline_at.isoformat() if record.deadline_at else None}, idempotency_key=f"watch:{reason}:{record.id}:{now.date()}")
        for shift in shifts:
            if shift.data.get("employee_id"): continue
            title = f"unfilled_shift: {shift.name}"
            if not db.scalar(select(Task.id).where(Task.title == title, Task.status.in_(["open", "queued", "running"]))):
                db.add(Task(title=title, agent_type="hr", priority="high", payload={"shift_id": shift.id, "site_id": shift.parent_id, "reason": "unfilled_shift"}))
        db.commit()


def main() -> None:
    log.info("scheduler started")
    while True:
        try: schedule_cycle()
        except Exception: log.exception("scheduler cycle failed")
        time.sleep(settings.scheduler_interval_seconds)


if __name__ == "__main__":
    main()
