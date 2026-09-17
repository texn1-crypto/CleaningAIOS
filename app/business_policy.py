from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .db import SessionLocal
from .models import AuditLog, BusinessGoal, CompanyKnowledge
from .platform import company_brain, event_bus


LEAD_GOAL_TITLE = "Квалифицированные лиды СПб и ЛО переданы владельцу"


def _regions() -> list[str]:
    regions = [
        item.strip()
        for item in settings.management_contact_regions.split("|")
        if item.strip()
    ]
    return regions or ["Санкт-Петербург", "Ленинградская область"]


def _knowledge_entries() -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (
            "commercial_profile",
            "service_area",
            {
                "regions": _regions(),
                "coverage": "all_districts",
                "service_area": settings.company_service_area,
                "service_type": "commercial_cleaning",
            },
        ),
        (
            "sales_policy",
            "proposal_requirements",
            {
                "proposal_timing": "after_customer_requirements",
                "pricing": "individual",
                "required_discovery": [
                    "facility_type",
                    "area_m2",
                    "schedule",
                    "service_scope",
                    "special_requirements",
                ],
            },
        ),
        (
            "sales_policy",
            "primary_objective",
            {
                "objective": "find_qualified_leads_and_handoff_to_owner",
                "crm_required": True,
                "owner_contact_channel": "phone",
                "owner_phone": settings.company_phone,
            },
        ),
        (
            "lead_operations",
            "workflow",
            {
                "steps": [
                    "discover",
                    "verify_public_evidence",
                    "qualify",
                    "persist_in_crm",
                    "notify_owner",
                ],
                "service_area_required": True,
                "external_outreach_uses_existing_consent_and_suppression_controls": True,
            },
        ),
    ]


def sync_owner_business_policy(db: Session) -> dict[str, int]:
    if not settings.company_phone.strip():
        raise RuntimeError("COMPANY_PHONE is required for owner lead handoff")

    knowledge_updated = 0
    for namespace, key, value in _knowledge_entries():
        existing = db.scalar(
            select(CompanyKnowledge).where(
                CompanyKnowledge.namespace == namespace,
                CompanyKnowledge.key == key,
            )
        )
        if existing and existing.value == value and existing.source == "owner_configuration":
            continue
        row = company_brain.remember(
            db,
            namespace,
            key,
            value,
            source="owner_configuration",
            confidence=1.0,
        )
        event_bus.publish(
            db,
            "knowledge.updated",
            "knowledge",
            str(row.id),
            {"namespace": namespace, "key": key, "version": row.version},
            idempotency_key=f"owner-business-policy:{namespace}:{key}:v{row.version}",
            actor="owner_configuration",
        )
        db.add(
            AuditLog(
                actor="owner_configuration",
                action="knowledge.updated",
                resource_type="knowledge",
                resource_id=str(row.id),
                details={"namespace": namespace, "key": key, "version": row.version},
            )
        )
        knowledge_updated += 1

    strategy = {
        "regions": _regions(),
        "crm_required": True,
        "owner_handoff_policy": "sales_policy.primary_objective",
        "proposal_policy": "sales_policy.proposal_requirements",
        "automatic_external_outreach": False,
    }
    goal = db.scalar(select(BusinessGoal).where(BusinessGoal.title == LEAD_GOAL_TITLE))
    goal_updated = 0
    if goal is None:
        goal = BusinessGoal(
            title=LEAD_GOAL_TITLE,
            description=(
                "Находить и квалифицировать заказчиков коммерческого клининга во всех "
                "районах Санкт-Петербурга и Ленинградской области, сохранять их в CRM "
                "и передавать владельцу."
            ),
            status="active",
            owner="lead_coordinator",
            metric="qualified_owner_handoffs",
            baseline=0,
            target=20,
            current=0,
            unit="leads/month",
            strategy=strategy,
        )
        db.add(goal)
        db.flush()
        goal_updated = 1
    elif goal.strategy != strategy or goal.status != "active":
        goal.strategy = strategy
        goal.status = "active"
        goal.owner = "lead_coordinator"
        goal_updated = 1

    if goal_updated:
        event_bus.publish(
            db,
            "business_goal.updated",
            "business_goal",
            str(goal.id),
            {"metric": goal.metric, "status": goal.status},
            idempotency_key=f"owner-business-goal:{goal.id}:{goal.metric}:{goal.status}",
            actor="owner_configuration",
        )
        db.add(
            AuditLog(
                actor="owner_configuration",
                action="business_goal.updated",
                resource_type="business_goal",
                resource_id=str(goal.id),
                details={"metric": goal.metric, "status": goal.status},
            )
        )

    db.commit()
    return {"knowledge_updated": knowledge_updated, "goal_updated": goal_updated}


def main() -> None:
    with SessionLocal() as db:
        result = sync_owner_business_policy(db)
    print(
        "Owner business policy synchronized: "
        f"knowledge={result['knowledge_updated']} goal={result['goal_updated']}"
    )


if __name__ == "__main__":
    main()
