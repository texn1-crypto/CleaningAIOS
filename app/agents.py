from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import settings
from .llm import llm_advisor
from .models import AgentState, BusinessGoal, BusinessRecord, Decision, DecisionOutcome, MediaAsset, OperatingEntity, Task
from .ai_router import provider_catalog
from .operations import create_ceo_actions, goal_progress, score_tender, site_economics
from .task_state import record_task_created


class Agent(Protocol):
    name: str
    def execute(self, db: Session, payload: dict[str, Any]) -> dict[str, Any]: ...


class DataCollectorAgent:
    name = "research"
    def execute(self, db: Session, payload: dict[str, Any]) -> dict[str, Any]:
        sources = payload.get("sources") or [x.strip() for x in settings.tender_sources.split(",") if x.strip()]
        query = payload.get("query", "")
        if payload.get("collection", "tenders") == "tenders":
            from .integrations import collect_tenders
            result = collect_tenders(db, sources=sources)
            return {"collection": "tenders", "query": query, "sources_requested": sources, "credentials_required": not bool(sources), "configured": bool(sources), **result, "evidence": [{"type": "tender_feed_collection", "source_count": len(sources), "created": result["created"], "updated": result["updated"]}]}
        return {"collection": payload.get("collection"), "query": query, "status": "adapter_required", "sources_requested": sources, "credentials_required": True, "configured": False, "evidence": []}


class OrchestratorAgent:
    name = "orchestrator"
    def execute(self, db: Session, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("action") == "system_activity_report":
            from .notifications import queue_owner_notification
            from .reports import build_activity_report, format_activity_report

            result = build_activity_report(
                db,
                period_hours=payload.get("period_hours", 24),
                period_minutes=payload.get("period_minutes"),
            )
            if payload.get("notify_owner"):
                notification = queue_owner_notification(
                    db,
                    idempotency_key=str(
                        payload.get("notification_idempotency_key") or f"activity-report:{result['generated_at']}"
                    ),
                    channel="telegram",
                    resource_type="activity_report",
                    resource_id=str(payload.get("scheduled_window_start") or result["generated_at"]),
                    subject="Регулярный отчёт CleaningAI OS",
                    body=format_activity_report(result),
                    data={
                        "report_kind": result["report_kind"],
                        "generated_at": result["generated_at"],
                        "period_minutes": result["period_minutes"],
                    },
                )
                result["owner_notification"] = notification.status
                result["owner_notification_id"] = notification.id
                result["evidence"].append(
                    {
                        "type": "owner_notification_queued",
                        "notification_id": notification.id,
                        "status": notification.status,
                    }
                )
            return result
        if payload.get("action") == "system_self_check":
            from .reports import build_system_self_check

            return build_system_self_check(db, registered_agents=sorted(AGENTS))
        if payload.get("action") == "task_timing_report":
            from .reports import build_task_timing_report

            return build_task_timing_report(db, task_id=payload.get("task_id"))
        if payload.get("action") == "revise_proposal":
            if payload.get("document_status") == "credentials_required":
                return {
                    "status": "credentials_required",
                    "credentials_required": ["TELEGRAM_BOT_API_BASE_URL"],
                    "reason": "Telegram Cloud Bot API не скачивает этот файл: нужен локальный Bot API server для документов больше 20 МБ.",
                    "evidence": [],
                }
            from .orchestrator import dispatch
            from .proposal_studio import create_proposal_revision

            copy_task = Task(
                title="Профессиональная редакция текста коммерческого предложения",
                agent_type="copywriter",
                priority="high",
                payload={
                    "source": "proposal_pipeline",
                    "source_path": payload.get("source_path"),
                    "source_filename": payload.get("source_filename"),
                },
                max_attempts=1,
            )
            db.add(copy_task)
            db.flush()
            copy_result = dispatch(db, copy_task)
            if copy_task.status != "done" or copy_result.get("status") != "ready":
                raise RuntimeError(f"Текстовый агент не завершил редакцию: {copy_result.get('error') or copy_result.get('reason') or copy_task.status}")

            creative_task = Task(
                title="Дизайн и компоновка коммерческого предложения",
                agent_type="creative",
                priority="high",
                payload={"source": "proposal_pipeline", "copy": copy_result},
                max_attempts=1,
            )
            db.add(creative_task)
            db.flush()
            creative_result = dispatch(db, creative_task)
            if creative_task.status != "done" or creative_result.get("status") != "ready":
                raise RuntimeError(f"Creative Agent не завершил дизайн: {creative_result.get('error') or creative_result.get('reason') or creative_task.status}")
            return create_proposal_revision(
                db,
                {**payload, "copy_task_id": copy_task.id, "creative_task_id": creative_task.id},
                copy_result,
                creative_result,
            )
        created = []
        for item in payload.get("delegations", []):
            agent_type = item.get("agent_type")
            if agent_type not in AGENTS or agent_type == self.name:
                continue
            task = Task(title=item.get("title", f"Delegated task for {agent_type}"), agent_type=agent_type, priority=item.get("priority", "normal"), payload=item.get("payload", {}))
            db.add(task); db.flush(); record_task_created(db, task, actor="orchestrator", reason="agent_delegation"); created.append({"id": task.id, "agent_type": agent_type})
        return {"coordinated": True, "delegated_tasks": created, "message": payload.get("message", "Task accepted by orchestrator"), "evidence": [{"delegations_requested": len(payload.get("delegations", []))}]}


class TenderAgent:
    name = "tender"
    def execute(self, db: Session, payload: dict[str, Any]) -> dict[str, Any]:
        keywords = payload.get("keywords", ["уборка МКД", "клининг БЦ", "ТСЖ", "УК"])
        rows = db.scalars(select(BusinessRecord).where(BusinessRecord.record_type == "tender")).all()
        for row in rows:
            if row.score is None and row.data:
                calculated = score_tender(row.data); row.score = calculated["score"]; row.data = {**row.data, "score_breakdown": calculated["breakdown"], "recommendation": calculated["recommendation"]}
        ranked = sorted(rows, key=lambda x: x.score or 0, reverse=True)
        return {"keywords": keywords, "tenders": [{"id": r.id, "title": r.title, "score": r.score, "deadline": r.deadline_at, "recommendation": r.data.get("recommendation")} for r in ranked], "submission_requires_owner_approval": True, "evidence": [{"record_id": r.id, "score": r.score} for r in ranked]}


class SalesAgent:
    name = "sales"
    def execute(self, db: Session, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("action") == "generate_proposal":
            from .proposals import generate_proposal
            return generate_proposal(db, payload)
        leads = db.scalars(select(BusinessRecord).where(BusinessRecord.record_type == "lead")).all()
        pipeline = sum(float(x.data.get("budget", 0) or 0) for x in leads if x.status not in {"won", "lost"})
        return {"lead_count": len(leads), "qualified": sum(1 for x in leads if (x.score or 0) >= 60 or x.status == "qualified"), "follow_ups_due": sum(1 for x in leads if x.status == "follow_up"), "pipeline_amount": pipeline, "loss_reasons": [x.data.get("loss_reason") for x in leads if x.status == "lost" and x.data.get("loss_reason")], "evidence": [{"record_id": x.id, "status": x.status} for x in leads]}


class MarketingAgent:
    name = "marketing"
    def execute(self, db: Session, payload: dict[str, Any]) -> dict[str, Any]:
        campaigns = db.scalar(select(func.count(BusinessRecord.id)).where(BusinessRecord.record_type == "campaign")) or 0
        experiments = db.scalars(select(BusinessRecord).where(BusinessRecord.record_type == "marketing_experiment")).all()
        providers = db.scalar(select(func.count(BusinessRecord.id)).where(BusinessRecord.record_type == "marketing_provider")) or 0
        queued_media = db.scalar(select(func.count(MediaAsset.id)).where(MediaAsset.status.in_(["queued", "credentials_required"]))) or 0
        leads = db.scalars(select(BusinessRecord).where(BusinessRecord.record_type == "lead")).all()
        attribution: dict[str, int] = {}
        for lead in leads: attribution[lead.source] = attribution.get(lead.source, 0) + 1
        return {"campaigns": campaigns, "experiments": len(experiments), "running_experiments": sum(x.status == "running" for x in experiments), "providers": providers, "queued_media": queued_media, "content_ideas": payload.get("topics", []), "lead_attribution": attribution, "analytics_requires_connected_sources": not bool(attribution), "ai_providers": provider_catalog(), "financial_actions_require_owner_approval": True, "evidence": [{"source": key, "leads": value} for key, value in attribution.items()]}


class HRAgent:
    name = "hr"
    def execute(self, db: Session, payload: dict[str, Any]) -> dict[str, Any]:
        candidates = db.scalars(select(BusinessRecord).where(BusinessRecord.record_type == "candidate")).all()
        employees = db.scalars(select(OperatingEntity).where(OperatingEntity.entity_type == "employee")).all()
        shifts = db.scalars(select(OperatingEntity).where(OperatingEntity.entity_type == "shift")).all()
        unfilled = [x for x in shifts if not x.data.get("employee_id") and x.status not in {"completed", "cancelled"}]
        return {"candidates": len(candidates), "available": sum(1 for x in candidates if x.data.get("available")), "employees": len(employees), "unfilled_shifts": len(unfilled), "final_decisions_require_owner_approval": True, "evidence": [{"shift_id": x.id, "site_id": x.parent_id} for x in unfilled]}


class FinanceAgent:
    name = "finance"
    def execute(self, db: Session, payload: dict[str, Any]) -> dict[str, Any]:
        rows = db.scalars(select(BusinessRecord).where(BusinessRecord.record_type.in_(["cashflow", "expense", "payment"]))).all()
        total = sum(float(r.data.get("amount", 0)) for r in rows)
        economics = site_economics(db)
        return {"entries": len(rows), "net_amount": total, "sites": economics, "low_margin_sites": [x for x in economics if x["revenue"] and x["margin_percent"] < 15], "financial_commitments_require_owner_approval": True, "evidence": [{"record_id": r.id, "amount": r.data.get("amount", 0)} for r in rows]}


class CEOAgent:
    name = "ceo"
    def execute(self, db: Session, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("action") == "agent_incident_report":
            return {
                "outcome": "completed",
                "report_kind": "agent_incident",
                "source_task_id": payload.get("source_task_id"),
                "agent_type": payload.get("failed_agent_type"),
                "failure_reason": payload.get("failure_reason"),
                "improvement_id": payload.get("improvement_id"),
                "handoff_status": payload.get("handoff_status"),
                "responsible_party": payload.get("responsible_party"),
                "evidence": [{
                    "type": "agent_incident",
                    "source_task_id": payload.get("source_task_id"),
                    "improvement_id": payload.get("improvement_id"),
                }],
            }
        open_tasks = db.scalar(select(func.count(Task.id)).where(Task.status.in_(["open", "queued", "running"]))) or 0
        pending = db.scalar(select(func.count(Decision.id)).where(Decision.status == "pending")) or 0
        failed = db.scalar(select(func.count(Task.id)).where(Task.status == "failed")) or 0
        health = max(0, 100 - pending * 5 - failed * 10)
        goals = [goal_progress(x) for x in db.scalars(select(BusinessGoal).where(BusinessGoal.status == "active")).all()]
        economics = site_economics(db)
        created = create_ceo_actions(db)
        recommendations = ["Resolve failed tasks"] if failed else []
        recommendations.extend(f"Recover margin at {x['site']}" for x in economics if x["revenue"] and x["margin_percent"] < 15)
        llm_advice = llm_advisor.review({
            "business_health": health,
            "open_tasks": open_tasks,
            "failed_tasks": failed,
            "pending_owner_decisions": pending,
            "goals": goals,
            "site_economics": economics,
        })
        llm_tasks: list[Task] = []
        if llm_advice.get("status") == "succeeded":
            for item in llm_advice.get("recommendations", [])[:5]:
                if not isinstance(item, dict) or item.get("needs_owner_decision"):
                    continue
                agent_type = item.get("agent_type")
                title = str(item.get("title", "")).strip()[:240]
                if agent_type not in AGENTS or agent_type in {"ceo", "orchestrator"} or not title:
                    continue
                exists = db.scalar(select(Task.id).where(Task.title == title, Task.status.in_(["open", "queued", "running", "blocked"])))
                if exists:
                    continue
                task = Task(
                    title=title,
                    agent_type=agent_type,
                    priority=item.get("priority") if item.get("priority") in {"low", "normal", "high"} else "normal",
                    payload={"origin": "llm_ceo", "advisory_only": True, "rationale": str(item.get("rationale", ""))[:2000]},
                )
                db.add(task)
                llm_tasks.append(task)
            db.flush()
            for task in llm_tasks:
                record_task_created(db, task, actor="ceo", reason="llm_advisory_task")
        tasks_created = [
            {"id": x.id, "title": x.title, "agent_type": x.agent_type, "source": "deterministic_policy"}
            for x in created
        ] + [
            {"id": x.id, "title": x.title, "agent_type": x.agent_type, "source": "llm_advisory"}
            for x in llm_tasks
        ]
        return {"business_health": health, "open_tasks": open_tasks, "pending_owner_decisions": pending, "goals": goals, "site_economics": economics, "recommendations": recommendations, "llm_advice": llm_advice, "tasks_created": tasks_created, "evidence": [{"type": "database_snapshot", "tasks": open_tasks, "pending": pending, "failed": failed}, {"type": "llm_advisory", "status": llm_advice.get("status"), "model": llm_advice.get("model")} ]}


class MetaBrainAgent:
    name = "meta_brain"
    def execute(self, db: Session, payload: dict[str, Any]) -> dict[str, Any]:
        states = db.scalars(select(AgentState)).all()
        gaps = [s.agent_type for s in states if s.last_error or not s.last_heartbeat_at]
        outcomes = db.scalars(select(DecisionOutcome)).all()
        measured = [x for x in outcomes if x.successful is not None]
        success_rate = round(sum(bool(x.successful) for x in measured) / len(measured) * 100, 2) if measured else None
        return {"agents_evaluated": len(states), "data_gaps": gaps, "decision_outcomes_measured": len(measured), "decision_success_rate": success_rate, "recommendations": [f"Restore telemetry for {x}" for x in gaps] + (["Start measuring decision outcomes"] if not measured else []), "evidence": [{"decision_id": x.decision_id, "successful": x.successful} for x in measured]}


class RequestAnalystAgent:
    name = "request_analyst"

    def execute(self, db: Session, payload: dict[str, Any]) -> dict[str, Any]:
        from .improvements import analyze_and_record

        result = analyze_and_record(db, payload)
        return {
            **result,
            "evidence": [
                {
                    "type": "request_capability_assessment",
                    "classification": result["classification"],
                    "capability_score": result["capability_score"],
                    "improvement_id": result.get("improvement_id"),
                }
            ],
        }


class CopywriterAgent:
    name = "copywriter"

    def execute(self, db: Session, payload: dict[str, Any]) -> dict[str, Any]:
        from .proposal_studio import build_professional_copy

        return build_professional_copy(payload)


class CreativeAgent:
    name = "creative"

    def execute(self, db: Session, payload: dict[str, Any]) -> dict[str, Any]:
        from .proposal_studio import build_creative_direction

        return build_creative_direction(payload)


AGENTS: dict[str, Agent] = {}
for agent in [OrchestratorAgent(), DataCollectorAgent(), TenderAgent(), SalesAgent(), MarketingAgent(), HRAgent(), FinanceAgent(), CEOAgent(), MetaBrainAgent(), RequestAnalystAgent(), CopywriterAgent(), CreativeAgent()]:
    AGENTS[agent.name] = agent


def heartbeat(db: Session, agent_type: str, status: str, error: str = "", metrics: dict | None = None) -> None:
    state = db.get(AgentState, agent_type) or AgentState(agent_type=agent_type)
    state.status = status
    state.last_error = error
    state.last_heartbeat_at = datetime.now(timezone.utc).replace(tzinfo=None)
    if metrics is not None:
        state.metrics = metrics
    db.add(state)
