from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import AgentState, BusinessRecord, Decision, Task


class Agent(Protocol):
    name: str
    def execute(self, db: Session, payload: dict[str, Any]) -> dict[str, Any]: ...


class DataCollectorAgent:
    name = "research"
    def execute(self, db: Session, payload: dict[str, Any]) -> dict[str, Any]:
        sources = payload.get("sources", [])
        query = payload.get("query", "")
        return {"query": query, "sources_requested": sources, "credentials_required": not bool(sources), "items": []}


class TenderAgent:
    name = "tender"
    def execute(self, db: Session, payload: dict[str, Any]) -> dict[str, Any]:
        keywords = payload.get("keywords", ["уборка МКД", "клининг БЦ", "ТСЖ", "УК"])
        rows = db.scalars(select(BusinessRecord).where(BusinessRecord.record_type == "tender")).all()
        ranked = sorted(rows, key=lambda x: x.score or 0, reverse=True)
        return {"keywords": keywords, "tenders": [{"id": r.id, "title": r.title, "score": r.score, "deadline": r.deadline_at} for r in ranked], "submission_requires_owner_approval": True}


class SalesAgent:
    name = "sales"
    def execute(self, db: Session, payload: dict[str, Any]) -> dict[str, Any]:
        leads = db.scalars(select(BusinessRecord).where(BusinessRecord.record_type == "lead")).all()
        return {"lead_count": len(leads), "qualified": sum(1 for x in leads if (x.score or 0) >= 60), "follow_ups_due": sum(1 for x in leads if x.status == "follow_up")}


class MarketingAgent:
    name = "marketing"
    def execute(self, db: Session, payload: dict[str, Any]) -> dict[str, Any]:
        campaigns = db.scalar(select(func.count(BusinessRecord.id)).where(BusinessRecord.record_type == "campaign")) or 0
        return {"campaigns": campaigns, "content_ideas": payload.get("topics", []), "analytics_requires_connected_sources": True}


class HRAgent:
    name = "hr"
    def execute(self, db: Session, payload: dict[str, Any]) -> dict[str, Any]:
        candidates = db.scalars(select(BusinessRecord).where(BusinessRecord.record_type == "candidate")).all()
        return {"candidates": len(candidates), "available": sum(1 for x in candidates if x.data.get("available")), "final_decisions_require_owner_approval": True}


class FinanceAgent:
    name = "finance"
    def execute(self, db: Session, payload: dict[str, Any]) -> dict[str, Any]:
        rows = db.scalars(select(BusinessRecord).where(BusinessRecord.record_type.in_(["cashflow", "expense", "payment"]))).all()
        total = sum(float(r.data.get("amount", 0)) for r in rows)
        return {"entries": len(rows), "net_amount": total, "financial_commitments_require_owner_approval": True}


class CEOAgent:
    name = "ceo"
    def execute(self, db: Session, payload: dict[str, Any]) -> dict[str, Any]:
        open_tasks = db.scalar(select(func.count(Task.id)).where(Task.status.in_(["open", "queued", "running"]))) or 0
        pending = db.scalar(select(func.count(Decision.id)).where(Decision.status == "pending")) or 0
        failed = db.scalar(select(func.count(Task.id)).where(Task.status == "failed")) or 0
        health = max(0, 100 - pending * 5 - failed * 10)
        return {"business_health": health, "open_tasks": open_tasks, "pending_owner_decisions": pending, "recommendations": ["Resolve failed tasks"] if failed else []}


class MetaBrainAgent:
    name = "meta_brain"
    def execute(self, db: Session, payload: dict[str, Any]) -> dict[str, Any]:
        states = db.scalars(select(AgentState)).all()
        gaps = [s.agent_type for s in states if s.last_error or not s.last_heartbeat_at]
        return {"agents_evaluated": len(states), "data_gaps": gaps, "recommendations": [f"Restore telemetry for {x}" for x in gaps]}


AGENTS: dict[str, Agent] = {a.name: a for a in [DataCollectorAgent(), TenderAgent(), SalesAgent(), MarketingAgent(), HRAgent(), FinanceAgent(), CEOAgent(), MetaBrainAgent()]}


def heartbeat(db: Session, agent_type: str, status: str, error: str = "", metrics: dict | None = None) -> None:
    state = db.get(AgentState, agent_type) or AgentState(agent_type=agent_type)
    state.status = status
    state.last_error = error
    state.last_heartbeat_at = datetime.now(timezone.utc).replace(tzinfo=None)
    if metrics is not None:
        state.metrics = metrics
    db.add(state)
