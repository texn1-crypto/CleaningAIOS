from datetime import datetime, timedelta

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.approval_service import expire_due_approvals
from app.db import Base
from app.models import (
    ApprovalDecisionRecord,
    ApprovalRequest,
    AuditLog,
    DomainEvent,
    Task,
)


def test_due_approvals_expire_once_without_resuming_protected_work():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    now = datetime(2040, 1, 1, 12, 0)

    with session_factory() as db:
        task = Task(
            title="Protected expired work",
            agent_type="sales",
            status="blocked",
        )
        db.add(task)
        db.flush()
        expired = ApprovalRequest(
            action_kind="outreach_send",
            resource_type="task",
            resource_id=str(task.id),
            status="pending",
            expires_at=now - timedelta(seconds=1),
        )
        current = ApprovalRequest(
            action_kind="financial",
            resource_type="task",
            resource_id=str(task.id),
            status="pending",
            expires_at=now + timedelta(hours=1),
        )
        db.add_all([expired, current])
        db.commit()
        expired_id = expired.id
        current_id = current.id

        assert expire_due_approvals(db, now=now) == [expired_id]
        db.commit()
        db.expire_all()

        assert db.get(ApprovalRequest, expired_id).status == "expired"
        assert db.get(ApprovalRequest, current_id).status == "pending"
        assert db.get(Task, task.id).status == "blocked"
        decision = db.scalar(
            select(ApprovalDecisionRecord).where(
                ApprovalDecisionRecord.approval_id == expired_id
            )
        )
        assert decision is not None
        assert decision.action == "expire"
        assert decision.actor == "system"
        assert db.scalar(
            select(func.count())
            .select_from(DomainEvent)
            .where(DomainEvent.event_type == "approval.expired")
        ) == 1
        assert db.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "approval.expired")
        ) == 1

        assert expire_due_approvals(db, now=now + timedelta(minutes=1)) == []
        db.commit()
        assert db.scalar(
            select(func.count()).select_from(ApprovalDecisionRecord)
        ) == 1
