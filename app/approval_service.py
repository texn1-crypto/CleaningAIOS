from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .models import (
    ApprovalDecisionRecord,
    ApprovalRequest,
    BusinessRecord,
    ContentItem,
    Decision,
    Task,
)
from .orchestrator import audit
from .platform import event_bus
from .security import Principal
from .task_state import transition_task


ALLOWED_DECISIONS = {"approve", "reject", "request_changes"}
STATUS_BY_DECISION = {
    "approve": "approved",
    "reject": "rejected",
    "request_changes": "changes_requested",
}


class ApprovalError(RuntimeError):
    pass


class ApprovalNotFound(ApprovalError):
    pass


class ApprovalConflict(ApprovalError):
    pass


class ApprovalExpired(ApprovalConflict):
    pass


class ApprovalStale(ApprovalConflict):
    pass


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _existing_decision(db: Session, approval_id: int) -> ApprovalDecisionRecord | None:
    return db.scalar(
        select(ApprovalDecisionRecord).where(
            ApprovalDecisionRecord.approval_id == approval_id
        )
    )


def _decision_result(
    row: ApprovalRequest,
    *,
    execution: str = "not_executed",
    task_status: str | None = None,
    idempotent_replay: bool = False,
) -> dict:
    return {
        "id": row.id,
        "status": row.status,
        "action_kind": row.action_kind,
        "execution": execution,
        "task_status": task_status,
        "automatic_commitment": False,
        "decision_version": row.decision_version,
        "idempotent_replay": idempotent_replay,
    }


def _expire_pending(db: Session, row: ApprovalRequest) -> None:
    version = row.decision_version
    decided_at = now_utc()
    claimed = db.execute(
        update(ApprovalRequest)
        .where(
            ApprovalRequest.id == row.id,
            ApprovalRequest.status == "pending",
            ApprovalRequest.decision_version == version,
        )
        .values(
            status="expired",
            decision_version=version + 1,
            decided_by="system",
            decision_note="Approval expired before a decision",
            decided_at=decided_at,
        )
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        raise ApprovalConflict("Approval was decided concurrently")
    db.add(
        ApprovalDecisionRecord(
            approval_id=row.id,
            action="expire",
            result_status="expired",
            actor="system",
            channel="system",
            reason="Approval expired before a decision",
            request_version=version,
        )
    )
    event_bus.publish(
        db,
        "approval.expired",
        row.resource_type,
        row.resource_id,
        {"approval_id": row.id, "action_kind": row.action_kind},
        idempotency_key=f"approval:{row.id}:expired",
        actor="system",
    )
    audit(
        db,
        "system",
        "approval.expired",
        row.resource_type,
        row.resource_id,
        {"approval_id": row.id, "request_version": version},
    )
    db.flush()


def _apply_resource_transition(
    db: Session,
    row: ApprovalRequest,
    action: str,
    actor: Principal,
    *,
    social_batch: BusinessRecord | None = None,
    social_items: list[ContentItem] | None = None,
) -> tuple[str, str | None]:
    execution = "not_executed"
    task_status = None
    if row.resource_type == "task":
        task = db.get(Task, int(row.resource_id))
        if task and task.status == "blocked" and action == "approve":
            task.payload = {**task.payload, "approval_id": row.id}
            transition_task(
                db,
                task,
                "queued",
                actor=actor.subject,
                reason="owner_approval_granted",
                transition_key=f"task:{task.id}:approval:{row.id}:queued",
            )
            task.run_after = now_utc()
            task_status = task.status
            if row.action_kind == "bulk_outreach":
                execution = "queued"
    elif row.resource_type == "decision":
        decision = db.get(Decision, int(row.resource_id))
        if decision and action in {"approve", "reject"}:
            decision.status = row.status
            decision.decided_by = actor.subject
            decision.decided_at = row.decided_at
    elif row.resource_type in {"marketing_invoice", "marketing_experiment", "proposal_revision"}:
        resource = db.get(BusinessRecord, int(row.resource_id))
        if resource and resource.record_type == row.resource_type and action in {"approve", "reject"}:
            if row.resource_type == "marketing_invoice":
                resource.status = "approved_for_manual_payment" if action == "approve" else "rejected"
            elif row.resource_type == "proposal_revision":
                resource.status = "approved" if action == "approve" else "rejected"
                resource.data = {
                    **resource.data,
                    "owner_approved": action == "approve",
                    "sent_to_client": False,
                    "approval_decision_at": row.decided_at.isoformat() if row.decided_at else None,
                }
            else:
                resource.status = "approved" if action == "approve" else "rejected"
    elif row.resource_type == "social_content_batch" and action in {"approve", "reject"}:
        batch = social_batch or db.get(BusinessRecord, int(row.resource_id))
        if batch and batch.record_type == "social_content_batch":
            item_ids = [int(value) for value in (batch.data or {}).get("content_item_ids", [])]
            items = social_items or (
                db.scalars(select(ContentItem).where(ContentItem.id.in_(item_ids))).all()
                if item_ids
                else []
            )
            batch.status = "scheduled" if action == "approve" else "rejected"
            for item in items:
                legal_review = item.channel == "instagram"
                item.status = (
                    "approval"
                    if action == "approve" and legal_review
                    else "scheduled"
                    if action == "approve"
                    else "cancelled"
                )
                item.metrics = {
                    **(item.metrics or {}),
                    "publication_status": (
                        "legal_review_required"
                        if action == "approve" and legal_review
                        else "scheduled_waiting_channel_credentials"
                        if action == "approve"
                        else "owner_rejected"
                    ),
                    "approval_id": row.id,
                }
    return execution, task_status


def decide_approval(
    db: Session,
    *,
    approval_id: int,
    action: str,
    note: str,
    actor: Principal,
    channel: str = "api",
    expected_version: int | None = None,
    idempotent: bool = False,
) -> dict:
    """Atomically persist one decision and resume its workflow at most once."""

    if action not in ALLOWED_DECISIONS:
        raise ApprovalError("Unsupported approval action")
    row = db.scalar(
        select(ApprovalRequest)
        .where(ApprovalRequest.id == approval_id)
        .with_for_update()
    )
    if not row:
        raise ApprovalNotFound("Approval not found")

    existing = _existing_decision(db, row.id)
    if row.status != "pending":
        if (
            idempotent
            and existing
            and existing.action == action
            and (expected_version is None or existing.request_version == expected_version)
        ):
            return _decision_result(row, idempotent_replay=True)
        if row.status == "expired":
            raise ApprovalExpired("Approval expired")
        raise ApprovalConflict("Approval already decided")
    if expected_version is not None and row.decision_version != expected_version:
        raise ApprovalStale("Approval callback is stale")
    if row.expires_at is not None and row.expires_at <= now_utc():
        _expire_pending(db, row)
        raise ApprovalExpired("Approval expired")

    social_batch = None
    social_items = None
    if action == "approve" and row.resource_type == "social_content_batch":
        from .social_marketing import validate_social_approval

        try:
            social_batch, social_items = validate_social_approval(db, row)
        except (TypeError, ValueError) as exc:
            raise ApprovalStale(str(exc)) from exc

    request_version = row.decision_version
    target_status = STATUS_BY_DECISION[action]
    decided_at = now_utc()
    claimed = db.execute(
        update(ApprovalRequest)
        .where(
            ApprovalRequest.id == row.id,
            ApprovalRequest.status == "pending",
            ApprovalRequest.decision_version == request_version,
        )
        .values(
            status=target_status,
            decision_version=request_version + 1,
            decided_by=actor.subject,
            decision_note=note,
            decided_at=decided_at,
        )
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        raise ApprovalConflict("Approval was decided concurrently")
    db.expire(row)
    row = db.get(ApprovalRequest, approval_id)
    if row is None:
        raise ApprovalNotFound("Approval not found")

    db.add(
        ApprovalDecisionRecord(
            approval_id=row.id,
            action=action,
            result_status=row.status,
            actor=actor.subject,
            channel=channel,
            reason=note,
            request_version=request_version,
        )
    )
    execution, task_status = _apply_resource_transition(
        db,
        row,
        action,
        actor,
        social_batch=social_batch,
        social_items=social_items,
    )
    event_bus.publish(
        db,
        f"approval.{row.status}",
        row.resource_type,
        row.resource_id,
        {"approval_id": row.id, "action_kind": row.action_kind, "decision_version": request_version},
        idempotency_key=f"approval:{row.id}:{row.status}",
        actor=actor.subject,
    )
    audit(
        db,
        actor.subject,
        f"approval.{row.status}",
        row.resource_type,
        row.resource_id,
        {
            "approval_id": row.id,
            "request_version": request_version,
            "channel": channel,
        },
    )
    db.flush()
    return _decision_result(row, execution=execution, task_status=task_status)
