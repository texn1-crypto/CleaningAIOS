import logging
import time
from datetime import datetime, timezone

from sqlalchemy import select

from .config import settings
from .db import SessionLocal
from .models import Task

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("cleaningai.scheduler")


def schedule_cycle() -> None:
    with SessionLocal() as db:
        active = db.scalar(select(Task.id).where(Task.agent_type == "ceo", Task.status.in_(["open", "queued", "running"])))
        if not active:
            db.add(Task(title="AI CEO business review", agent_type="ceo", status="queued", priority="high", run_after=datetime.now(timezone.utc).replace(tzinfo=None)))
            db.commit()


def main() -> None:
    log.info("scheduler started")
    while True:
        try: schedule_cycle()
        except Exception: log.exception("scheduler cycle failed")
        time.sleep(settings.scheduler_interval_seconds)


if __name__ == "__main__":
    main()
