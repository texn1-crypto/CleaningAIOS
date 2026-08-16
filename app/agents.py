from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import settings
from .llm import llm_advisor
from .models import AgentState, BusinessGoal, BusinessRecord, Decision, DecisionOutcome, MediaAsset, OperatingEntity, Task
from .ai_router import provider_catalog
from .operations import create_ceo_actions, goal_progress, site_economics
from .task_state import record_task_created


class Agent(Protocol):
    name: str
    def execute(self, db: Session, payload: dict[str, Any]) -> dict[str, Any]: ...


class DataCollectorAgent:
    name = "research"
    def execute(self, db: Session, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("collection") == "management_company_contacts":
            from .management_companies import audit_management_company_contacts

            return audit_management_company_contacts(
                db,
                batch_limit=int(payload.get("batch_limit") or 20),
                enrich_verified_websites=bool(payload.get("enrich_verified_websites")),
            )
        if payload.get("collection") == "management_company_internet_discovery":
            return {
                "status": "adapter_required",
                "collection": "management_company_internet_discovery",
                "configured": False,
                "credentials_required": ["YANDEX_SEARCH_API_KEY", "YANDEX_CLOUD_FOLDER_ID"],
                "reason": (
                    "Не настроен официальный поисковый адаптер. До его подключения нельзя выдавать "
                    "угаданный домен или найденный каталог как официальный сайт организации."
                ),
                "evidence": [],
            }
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
        if payload.get("action") == "run_safe_operations_cycle":
            from .safe_operations import run_safe_operations_cycle

            return run_safe_operations_cycle(db, registered_agents=sorted(AGENTS))
        if payload.get("action") == "marketing_sales_coordination":
            from .marketing_coordination import run_marketing_sales_coordination

            return run_marketing_sales_coordination(
                db,
                scheduled_window_start=payload.get("scheduled_window_start"),
                period_minutes=int(payload.get("period_minutes") or 30),
            )
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
        if payload.get("action") == "prepare_tender_package":
            from .models import TenderDocument
            from .tender_intelligence import TERMINAL_TENDER_STATUSES, classify_tender_scope, evaluate_tender_viability, merge_registered_document_risks

            record_id = int(payload.get("record_id") or 0)
            tender = db.get(BusinessRecord, record_id)
            if not tender or tender.record_type != "tender":
                return {
                    "status": "not_found",
                    "record_id": record_id,
                    "submission_allowed": False,
                    "evidence": [{"type": "tender_record_lookup", "found": False}],
                }

            if tender.status in TERMINAL_TENDER_STATUSES:
                return {
                    "status": "tender_closed",
                    "record_id": record_id,
                    "reason": f"Тендер находится в конечном статусе: {tender.status}.",
                    "submission_allowed": False,
                    "separate_submission_approval_required": True,
                    "evidence": [{"type": "tender_status", "status": tender.status, "open": False}],
                }

            tender.data = {
                **(tender.data or {}),
                "title": tender.title,
                "external_id": tender.external_id or "",
                "source_url": str((tender.data or {}).get("source_url") or ""),
                "deadline_at": tender.deadline_at.isoformat() if tender.deadline_at else "",
            }
            tender.data = {
                **tender.data,
                "scope_assessment": classify_tender_scope(tender.title, tender.data),
            }
            tender.data = merge_registered_document_risks(db, tender, tender.data or {})
            evaluation = evaluate_tender_viability(tender.data or {})
            expected_fingerprint = str(payload.get("evaluation_fingerprint") or "")
            if evaluation.get("fingerprint") != expected_fingerprint:
                return {
                    "status": "stale_evaluation",
                    "record_id": record_id,
                    "reason": "Параметры тендера изменились после подтверждения; требуется новый расчёт и новое решение владельца.",
                    "submission_allowed": False,
                    "separate_submission_approval_required": True,
                    "evidence": [{"type": "tender_evaluation_fingerprint", "matches": False}],
                }
            if evaluation.get("status") != "ready_for_owner_review":
                return {
                    "status": "participation_no_longer_recommended",
                    "record_id": record_id,
                    "evaluation": evaluation,
                    "submission_allowed": False,
                    "separate_submission_approval_required": True,
                    "evidence": [{"type": "tender_evaluation_rechecked", "eligible": False}],
                }

            documents = db.scalars(
                select(TenderDocument).where(TenderDocument.record_id == tender.id).order_by(TenderDocument.id)
            ).all()
            available_names = {document.name.strip().casefold() for document in documents}
            required_documents = [
                str(name).strip() for name in (tender.data or {}).get("required_documents", []) if str(name).strip()
            ]
            missing_documents = [name for name in required_documents if name.casefold() not in available_names]
            document_risks = [
                {
                    "document_id": document.id,
                    "name": document.name,
                    "risk_level": str((document.analysis or {}).get("risk_level") or "unclassified"),
                    "risks": (document.analysis or {}).get("risks") or [],
                }
                for document in documents
                if (document.analysis or {}).get("risks") or (document.analysis or {}).get("risk_level")
            ]
            critical_document_risks = [
                risk for risk in document_risks if risk["risk_level"] == "critical"
            ]
            status = (
                "legal_stop"
                if critical_document_risks
                else "package_incomplete"
                if missing_documents
                else "package_review_prepared"
            )
            portal_url = str((tender.data or {}).get("source_url") or (tender.data or {}).get("url") or "")
            portal_guidance = (tender.data or {}).get("submission_instructions")
            portal_instruction_status = (
                "source_instructions_available"
                if isinstance(portal_guidance, dict) and portal_guidance.get("steps")
                else "portal_navigation_required"
            )
            return {
                "status": status,
                "record_id": tender.id,
                "title": tender.title,
                "evaluation": evaluation,
                "documents": [
                    {
                        "id": document.id,
                        "name": document.name,
                        "status": document.status,
                        "checksum": document.checksum,
                        "analysis": document.analysis,
                    }
                    for document in documents
                ],
                "required_documents": required_documents,
                "missing_documents": missing_documents,
                "document_risks": document_risks,
                "manual_submission_checklist": [
                    "Уполномоченный человек повторно проверяет срок и номер закупки на официальной площадке.",
                    "Живой юрист проверяет итоговые формы, риски и полномочия подписанта.",
                    "Человек входит на площадку под своей учётной записью и сверяет актуальный перечень полей.",
                    "Каждый файл сверяется по имени, версии и контрольной сумме перед загрузкой.",
                    "Цена, обеспечение и состав заявки повторно показываются владельцу для отдельного подтверждения.",
                    "Только после отдельного подтверждения уполномоченный человек подписывает ЭЦП и подаёт заявку.",
                ],
                "portal_url": portal_url,
                "portal_instruction_status": portal_instruction_status,
                "portal_guidance": portal_guidance if portal_instruction_status == "source_instructions_available" else None,
                "screenshots_status": "source_material_required" if portal_instruction_status != "source_instructions_available" else "review_required",
                "email_delivery_status": "recipient_and_attachment_approval_required",
                "human_legal_review_required": True,
                "human_upload_required": True,
                "submission_allowed": False,
                "separate_submission_approval_required": True,
                "evidence": [
                    {"type": "tender_evaluation_rechecked", "eligible": True, "fingerprint": expected_fingerprint},
                    {"type": "tender_document_registry", "registered": len(documents), "missing": missing_documents},
                    {"type": "tender_document_risk_summary", "risks": document_risks},
                ],
            }
        from .tender_intelligence import TERMINAL_TENDER_STATUSES, classify_tender_scope, ensure_participation_review_task, evaluate_tender_viability, merge_registered_document_risks, screening_record_status

        keywords = payload.get("keywords", ["уборка МКД", "клининг БЦ", "ТСЖ", "УК"])
        rows = db.scalars(select(BusinessRecord).where(BusinessRecord.record_type == "tender")).all()
        for row in rows:
            if row.status in TERMINAL_TENDER_STATUSES:
                continue
            row.data = {
                **(row.data or {}),
                "title": row.title,
                "external_id": row.external_id or "",
                "source_url": str((row.data or {}).get("source_url") or ""),
            }
            if row.deadline_at:
                row.data = {**(row.data or {}), "deadline_at": row.deadline_at.isoformat()}
            row.data = {**(row.data or {}), "scope_assessment": classify_tender_scope(row.title, row.data or {})}
            row.data = merge_registered_document_risks(db, row, row.data)
            evaluation = evaluate_tender_viability(row.data or {})
            row.score = evaluation.get("score")
            row.status = screening_record_status(evaluation["status"])
            row.data = {
                **(row.data or {}),
                "viability_evaluation": evaluation,
                "score_breakdown": evaluation.get("score_breakdown", {}),
                "recommendation": evaluation["decision"],
            }
            ensure_participation_review_task(db, row, evaluation, actor="tender_agent")
        ranked = sorted(rows, key=lambda x: x.score or 0, reverse=True)
        return {"keywords": keywords, "tenders": [{"id": r.id, "title": r.title, "score": r.score, "deadline": r.deadline_at, "recommendation": (r.data.get("viability_evaluation") or {}).get("decision")} for r in ranked], "submission_requires_owner_approval": True, "evidence": [{"record_id": r.id, "score": r.score} for r in ranked]}


class SalesAgent:
    name = "sales"
    def execute(self, db: Session, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("action") == "prepare_lead_follow_up":
            from .marketing_coordination import prepare_lead_follow_up

            return prepare_lead_follow_up(
                db,
                record_id=int(payload.get("record_id") or 0),
                fingerprint=str(payload.get("fingerprint") or ""),
            )
        if payload.get("action") == "generate_proposal":
            from .proposals import generate_proposal
            return generate_proposal(db, payload)
        if payload.get("action") == "prepare_management_company_outreach":
            from .management_companies import management_company_outreach_summary

            return management_company_outreach_summary(db)
        if payload.get("action") == "execute_bulk_outreach_campaign":
            from .outreach import queue_campaign_in_batches

            result = queue_campaign_in_batches(
                db,
                campaign_key=payload["campaign_key"],
                recipients=payload["recipients"],
                subject=payload["subject"],
                body=payload["body"],
                mailbox_id=payload.get("mailbox_id"),
                template_id=payload.get("template_id"),
                scheduled_at=(
                    datetime.fromisoformat(payload["scheduled_at"])
                    if isinstance(payload.get("scheduled_at"), str)
                    else payload.get("scheduled_at")
                ),
                attachments=payload.get("attachments") or [],
                auto_balance_mailboxes=payload.get("auto_balance_mailboxes", True),
                batch_size=min(100, max(1, int(payload.get("batch_size") or 100))),
            )
            return {
                **result,
                "campaign_key": payload["campaign_key"],
                "owner_approval_verified": True,
                "evidence": [{
                    "type": "outreach_campaign_queued",
                    "queued": result["queued"],
                    "batch_count": result["batch_count"],
                    "mailbox_distribution": result["mailbox_distribution"],
                }],
            }
        leads = db.scalars(select(BusinessRecord).where(BusinessRecord.record_type == "lead")).all()
        pipeline = sum(float(x.data.get("budget", 0) or 0) for x in leads if x.status not in {"won", "lost"})
        return {"lead_count": len(leads), "qualified": sum(1 for x in leads if (x.score or 0) >= 60 or x.status == "qualified"), "follow_ups_due": sum(1 for x in leads if x.status == "follow_up"), "pipeline_amount": pipeline, "loss_reasons": [x.data.get("loss_reason") for x in leads if x.status == "lost" and x.data.get("loss_reason")], "evidence": [{"record_id": x.id, "status": x.status} for x in leads]}


class MarketingAgent:
    name = "marketing"
    def execute(self, db: Session, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("action") == "prepare_lead_generation_strategy":
            from .marketing_coordination import prepare_lead_generation_strategy

            return prepare_lead_generation_strategy(db)
        if payload.get("action") == "generate_image":
            from .social_runtime import queue_direct_image_request

            return queue_direct_image_request(db, payload)
        if payload.get("action") == "prepare_social_account_setup":
            from .social_marketing import prepare_social_account_setup

            return prepare_social_account_setup(db, channels=payload.get("channels") or [])
        if payload.get("action") == "prepare_daily_social_plan":
            from .social_marketing import prepare_daily_social_plan

            day = datetime.fromisoformat(payload["day"]) if payload.get("day") else None
            return prepare_daily_social_plan(db, day=day)
        if payload.get("action") == "prepare_daily_cleaning_news_plan":
            from .social_marketing import prepare_daily_cleaning_news_plan

            day = datetime.fromisoformat(payload["day"]) if payload.get("day") else None
            return prepare_daily_cleaning_news_plan(db, day=day)
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
        return {"business_health": health, "open_tasks": open_tasks, "pending_owner_decisions": pending, "goals": goals, "site_economics": economics, "recommendations": recommendations, "llm_advice": llm_advice, "tasks_created": tasks_created, "evidence": [{"type": "database_snapshot", "tasks": open_tasks, "pending": pending, "failed": failed}, {"type": "llm_advisory", "status": llm_advice.get("status"), "provider": llm_advice.get("provider"), "model": llm_advice.get("model")} ]}


class MetaBrainAgent:
    name = "meta_brain"
    def execute(self, db: Session, payload: dict[str, Any]) -> dict[str, Any]:
        states = db.scalars(select(AgentState)).all()
        gaps = [s.agent_type for s in states if s.last_error or not s.last_heartbeat_at]
        outcomes = db.scalars(select(DecisionOutcome)).all()
        measured = [x for x in outcomes if x.successful is not None]
        success_rate = round(sum(bool(x.successful) for x in measured) / len(measured) * 100, 2) if measured else None
        return {"agents_evaluated": len(states), "data_gaps": gaps, "decision_outcomes_measured": len(measured), "decision_success_rate": success_rate, "recommendations": [f"Restore telemetry for {x}" for x in gaps] + (["Start measuring decision outcomes"] if not measured else []), "evidence": [{"decision_id": x.decision_id, "successful": x.successful} for x in measured]}


class SystemAdminAgent:
    name = "system_admin"

    def execute(self, db: Session, payload: dict[str, Any]) -> dict[str, Any]:
        from .system_admin import run_system_admin_audit

        return run_system_admin_audit(
            db,
            stale_task_minutes=int(
                payload.get("stale_task_minutes")
                or settings.system_admin_stale_task_minutes
            ),
            notify_owner=bool(payload.get("notify_owner")),
            notification_idempotency_key=str(
                payload.get("notification_idempotency_key") or ""
            ),
        )


class GrowthOfficerAgent:
    name = "growth_officer"

    def execute(self, db: Session, payload: dict[str, Any]) -> dict[str, Any]:
        from .growth import run_growth_review

        review_at = datetime.fromisoformat(payload["review_at"]) if payload.get("review_at") else None
        return run_growth_review(db, now=review_at)


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
        if payload.get("action") == "improve_referenced_text":
            from .text_studio import improve_referenced_text

            return improve_referenced_text(payload)
        if payload.get("action") == "review_previous_text":
            from .text_studio import review_referenced_text

            return review_referenced_text(payload)
        from .proposal_studio import build_professional_copy

        return build_professional_copy(payload)


class CreativeAgent:
    name = "creative"

    def execute(self, db: Session, payload: dict[str, Any]) -> dict[str, Any]:
        from .proposal_studio import build_creative_direction

        return build_creative_direction(payload)


AGENTS: dict[str, Agent] = {}
for agent in [OrchestratorAgent(), DataCollectorAgent(), TenderAgent(), SalesAgent(), MarketingAgent(), HRAgent(), FinanceAgent(), CEOAgent(), GrowthOfficerAgent(), MetaBrainAgent(), SystemAdminAgent(), RequestAnalystAgent(), CopywriterAgent(), CreativeAgent()]:
    AGENTS[agent.name] = agent


def heartbeat(db: Session, agent_type: str, status: str, error: str = "", metrics: dict | None = None) -> None:
    state = db.get(AgentState, agent_type) or AgentState(agent_type=agent_type)
    state.status = status
    state.last_error = error
    state.last_heartbeat_at = datetime.now(timezone.utc).replace(tzinfo=None)
    if metrics is not None:
        state.metrics = metrics
    db.add(state)
