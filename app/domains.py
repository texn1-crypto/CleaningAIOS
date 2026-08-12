from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import BusinessRecord, ContactEvent


DOMAIN_STATUSES = {
    "lead": {"new", "qualified", "proposal", "negotiation", "won", "lost", "follow_up"},
    "tender": {"new", "screening", "preparing", "approval", "submitted", "won", "lost", "expired"},
    "candidate": {"new", "screening", "interview", "trial", "approved", "rejected", "hired"},
    "cashflow": {"planned", "pending", "approved", "paid", "cancelled"},
    "expense": {"planned", "pending", "approved", "paid", "cancelled"},
    "payment": {"planned", "pending", "approved", "paid", "overdue", "cancelled"},
    "campaign": {"draft", "approval", "scheduled", "running", "paused", "completed", "cancelled"},
    "marketing_provider": {"scouting", "contacted", "proposal", "active", "paused", "rejected"},
    "marketing_experiment": {"draft", "approval", "approved", "running", "paused", "completed", "cancelled", "rejected"},
    "marketing_invoice": {"pending_approval", "approved_for_manual_payment", "paid", "rejected", "cancelled"},
}

TERMINAL_STATUSES = {"won", "lost", "expired", "rejected", "hired", "paid", "cancelled", "completed"}


def validate_status(record_type: str, status: str) -> None:
    allowed = DOMAIN_STATUSES.get(record_type)
    if allowed and status not in allowed:
        raise HTTPException(422, {"message": f"Invalid status for {record_type}", "allowed": sorted(allowed)})


def validate_record(record_type: str, status: str, data: dict[str, Any], deadline_at: datetime | None) -> None:
    validate_status(record_type, status)
    if record_type in {"cashflow", "expense", "payment"}:
        try:
            float(data.get("amount"))
        except (TypeError, ValueError):
            raise HTTPException(422, "Finance records require numeric data.amount")
    if record_type == "tender" and status in {"preparing", "approval", "submitted"} and not deadline_at:
        raise HTTPException(422, "Active tender requires deadline_at")
    if record_type == "lead" and status == "lost" and not data.get("loss_reason"):
        raise HTTPException(422, "Lost lead requires data.loss_reason")


def module_summary(db: Session) -> dict[str, Any]:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    records = db.scalars(select(BusinessRecord)).all()

    def rows(kind: str) -> list[BusinessRecord]:
        return [x for x in records if x.record_type == kind]

    leads = rows("lead")
    tenders = rows("tender")
    candidates = rows("candidate")
    campaigns = rows("campaign")
    experiments = rows("marketing_experiment")
    providers = rows("marketing_provider")
    marketing_invoices = rows("marketing_invoice")
    finance = [x for x in records if x.record_type in {"cashflow", "expense", "payment"}]
    touches = db.scalar(select(func.count(ContactEvent.id))) or 0
    overdue_tenders = sum(1 for x in tenders if x.deadline_at and x.deadline_at < now and x.status not in TERMINAL_STATUSES)
    overdue_payments = sum(1 for x in finance if x.record_type == "payment" and x.status == "overdue")

    signed_amount = sum(float(x.data.get("amount", 0) or 0) for x in leads if x.status == "won")
    cash_balance = sum(float(x.data.get("amount", 0) or 0) for x in finance)
    return {
        "sales": {"total": len(leads), "qualified": sum(x.status == "qualified" for x in leads), "won": sum(x.status == "won" for x in leads), "pipeline_amount": sum(float(x.data.get("budget", 0) or 0) for x in leads if x.status not in TERMINAL_STATUSES), "signed_amount": signed_amount, "contact_events": touches},
        "tenders": {"total": len(tenders), "active": sum(x.status not in TERMINAL_STATUSES for x in tenders), "awaiting_approval": sum(x.status == "approval" for x in tenders), "overdue": overdue_tenders},
        "hr": {"candidates": len(candidates), "available": sum(bool(x.data.get("available")) for x in candidates), "hired": sum(x.status == "hired" for x in candidates)},
        "finance": {"entries": len(finance), "net_amount": cash_balance, "pending": sum(x.status == "pending" for x in finance), "overdue_payments": overdue_payments},
        "marketing": {"campaigns": len(campaigns), "active": sum(x.status in {"scheduled", "running"} for x in campaigns) + sum(x.status == "running" for x in experiments), "experiments": len(experiments), "providers": len(providers), "pending_invoices": sum(x.status == "pending_approval" for x in marketing_invoices), "planned_budget": sum(float(x.data.get("budget", 0) or 0) for x in campaigns) + sum(float(x.data.get("budget_limit", 0) or 0) for x in experiments)},
    }
