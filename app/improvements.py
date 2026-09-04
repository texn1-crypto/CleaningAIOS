from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .chat import redact_sensitive_text
from .config import settings
from .llm import llm_advisor
from .models import ImprovementRequest, Task


READ_INTENTS = {"greeting", "acknowledgement", "clarification", "help", "menu", "dashboard", "tasks", "decisions", "approvals", "records", "summary", "inbox", "improvements", "activity_report", "system_admin_report", "system_self_check", "task_eta"}


def _normalize(text: str) -> str:
    return " ".join(text.lower().replace("ё", "е").split())


def _suggested_function(text: str, agent_type: str) -> tuple[str, list[str]]:
    if re.search(r"\b(позвони|позвонить|звонок|телефони)\w*\b", text):
        return "Добавить безопасную интеграцию телефонии и журнал звонков", ["telephony_adapter", "call_audit_log"]
    if "коммерческ" in text or re.search(r"\bкп\b", text):
        return "Добавить генератор коммерческих предложений из данных CRM", ["proposal_generator", "document_template"]
    if any(word in text for word in ("pdf", "документ", "договор", "акт", "счет")):
        return "Добавить управляемую генерацию и проверку документов", ["document_generator", "document_validation"]
    if any(word in text for word in ("календар", "встреч", "расписан")):
        return "Добавить календарную интеграцию с подтверждением владельца", ["calendar_adapter", "scheduling_workflow"]
    if any(word in text for word in ("групп", "канал", "опубликуй", "размести")):
        return "Добавить Telegram publishing workflow с preview и approval", ["telegram_publisher", "publication_preview"]
    if agent_type == "orchestrator":
        return "Расширить маршрутизацию и исполнение свободных запросов Orchestrator", ["intent_support", "agent_executor"]
    return f"Добавить исполняемую функцию для агента {agent_type}", [f"{agent_type}_action_executor"]


def deterministic_assessment(message: str, intent: dict[str, Any]) -> dict[str, Any]:
    safe_message = redact_sensitive_text(message.strip())[:4000]
    text = _normalize(safe_message)
    kind = str(intent.get("kind", "task"))
    agent_type = str(intent.get("agent_type", "orchestrator"))
    if kind in READ_INTENTS:
        return {
            "fully_supported": True,
            "capability_score": 1.0,
            "classification": "supported",
            "reason": "Запрос напрямую соответствует существующей функции Telegram-бота.",
            "missing_capabilities": [],
            "suggested_function": "",
            "acceptance_criteria": [],
            "test_plan": [],
            "should_create_improvement": False,
        }

    protected = bool(intent.get("protected"))
    if protected:
        return {
            "fully_supported": True,
            "capability_score": 1.0,
            "classification": "approval_required",
            "reason": "Функция поддерживается, но политика безопасности требует подтверждения владельца.",
            "missing_capabilities": [],
            "suggested_function": "",
            "acceptance_criteria": [],
            "test_plan": [],
            "should_create_improvement": False,
        }

    if intent.get("payload", {}).get("action") in {
        "generate_proposal",
        "improve_referenced_text",
        "review_previous_text",
        "revise_proposal",
        "prepare_social_account_setup",
        "refresh_social_visuals",
        "generate_image",
        "run_safe_operations_cycle",
    }:
        return {
            "fully_supported": True,
            "capability_score": 1.0,
            "classification": "supported",
            "reason": "Запрос связан с проверенным исполняемым workflow и возвращает фактический результат из общей базы.",
            "missing_capabilities": [],
            "suggested_function": "",
            "acceptance_criteria": [],
            "test_plan": [],
            "should_create_improvement": False,
        }

    explicit_task = bool(re.search(r"\b(создай|поставь|добавь)\s+(мне\s+)?задач", text))
    if explicit_task:
        return {
            "fully_supported": True,
            "capability_score": 1.0,
            "classification": "supported",
            "reason": "Пользователь попросил создать задачу; очередь и выбранный агент доступны.",
            "missing_capabilities": [],
            "suggested_function": "",
            "acceptance_criteria": [],
            "test_plan": [],
            "should_create_improvement": False,
        }

    if agent_type == "research" and any(word in text for word in ("тендер", "закупк", "конкурс")) and not settings.tender_sources.strip():
        return {
            "fully_supported": False,
            "capability_score": 0.7,
            "classification": "configuration_required",
            "reason": "Сбор тендеров реализован, но внешние источники ещё не настроены.",
            "missing_capabilities": ["tender_source_credentials"],
            "suggested_function": "",
            "acceptance_criteria": [],
            "test_plan": [],
            "should_create_improvement": False,
        }

    supported_analysis = any(word in text for word in ("проанализируй", "проверь", "оцени", "сделай анализ", "подведи итог")) and agent_type != "orchestrator"
    if supported_analysis:
        return {
            "fully_supported": True,
            "capability_score": 0.95,
            "classification": "supported",
            "reason": f"Запрос может выполнить аналитический агент {agent_type} по данным общей базы.",
            "missing_capabilities": [],
            "suggested_function": "",
            "acceptance_criteria": [],
            "test_plan": [],
            "should_create_improvement": False,
        }

    unsupported_action = bool(re.search(r"\b(позвони|напиши|подготовь|создай|добавь|отправь|опубликуй|размести|запланируй|сформируй)\w*\b", text))
    if agent_type != "orchestrator" and not unsupported_action:
        return {
            "fully_supported": True,
            "capability_score": 0.9,
            "classification": "supported",
            "reason": f"Запрос маршрутизирован существующему агенту {agent_type}.",
            "missing_capabilities": [],
            "suggested_function": "",
            "acceptance_criteria": [],
            "test_plan": [],
            "should_create_improvement": False,
        }

    suggested, missing = _suggested_function(text, agent_type)
    acceptance = [
        "Запрос выполняется из Telegram обычной русской фразой без slash-команды.",
        "Бот сообщает фактический результат и не выдаёт постановку задачи за выполненное действие.",
        "Финансовые, юридические, договорные, тендерные и кадровые ограничения не обходятся.",
        "Результат и ошибки сохраняются в общей базе и audit log.",
    ]
    tests = [
        "Добавить unit-тест распознавания исходной русской формулировки.",
        "Добавить API/agent integration test успешного и ошибочного сценариев.",
        "Запустить полный pytest.",
        "Пересобрать Docker и проверить Telegram smoke test.",
    ]
    return {
        "fully_supported": False,
        "capability_score": 0.4 if agent_type == "orchestrator" else 0.55,
        "classification": "capability_gap",
        "reason": "Текущая версия может сохранить поручение, но не имеет проверенного исполнителя для полного результата.",
        "missing_capabilities": missing,
        "suggested_function": suggested,
        "acceptance_criteria": acceptance,
        "test_plan": tests,
        "should_create_improvement": True,
    }


def _merge_llm_assessment(message: str, intent: dict[str, Any], baseline: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    if intent.get("kind") != "task" or baseline["classification"] in {"supported", "approval_required", "configuration_required"}:
        return baseline, {"status": "not_needed"}
    advice = llm_advisor.analyze_request(redact_sensitive_text(message), intent, baseline)
    if advice.get("status") != "succeeded":
        return baseline, advice
    if not advice.get("should_create_improvement") and not baseline["should_create_improvement"]:
        return baseline, advice
    merged = dict(baseline)
    merged.update({
        "fully_supported": False,
        "capability_score": min(float(baseline["capability_score"]), float(advice.get("capability_score", 0.5))),
        "classification": "capability_gap",
        "reason": str(advice.get("reason") or baseline["reason"])[:2000],
        "missing_capabilities": [str(x)[:200] for x in (advice.get("missing_capabilities") or baseline["missing_capabilities"])[:10]],
        "suggested_function": str(advice.get("suggested_function") or baseline["suggested_function"])[:1000],
        "acceptance_criteria": [str(x)[:1000] for x in (advice.get("acceptance_criteria") or baseline["acceptance_criteria"])[:10]],
        "test_plan": [str(x)[:1000] for x in (advice.get("test_plan") or baseline["test_plan"])[:10]],
        "should_create_improvement": True,
    })
    return merged, advice


def build_codex_prompt(request_text: str, assessment: dict[str, Any], improvement_id: int | None = None) -> str:
    identifier = f" #{improvement_id}" if improvement_id else ""
    return f"""CleaningAI OS improvement request{identifier}

Work only in the existing repository texn1-crypto/CleaningAIOS. Do not create a new project.

Original Telegram request (credentials already redacted):
{request_text}

Capability gap:
{assessment['reason']}

Suggested function:
{assessment['suggested_function']}

Missing capabilities:
{json.dumps(assessment['missing_capabilities'], ensure_ascii=False)}

Acceptance criteria:
{json.dumps(assessment['acceptance_criteria'], ensure_ascii=False)}

Required test plan:
{json.dumps(assessment['test_plan'], ensure_ascii=False)}

Inspect the current implementation before changing it. Preserve the shared data model,
orchestrator, RBAC, audit log, backward-compatible Telegram behavior, suppression rules,
and owner approval gates. Implement the smallest production-grade capability that closes
the gap. Add regression tests, run the full test suite and Docker health checks, then commit
to the existing working branch and update the current pull request. Never perform a real
financial, legal, contractual, tender-submission or final-HR action while testing.
""".strip()


def workspace_agent_configuration_status() -> str:
    if not settings.workspace_agent_trigger_id or not settings.workspace_agent_access_token:
        return "credentials_required"
    if not re.fullmatch(r"agtch_[A-Za-z0-9_-]+", settings.workspace_agent_trigger_id):
        return "invalid_configuration"
    return "configured"


def trigger_workspace_agent(row: ImprovementRequest) -> dict[str, Any]:
    configuration = workspace_agent_configuration_status()
    if configuration != "configured":
        return {"status": configuration}
    endpoint = f"https://api.chatgpt.com/v1/workspace_agents/{settings.workspace_agent_trigger_id}/trigger"
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or parsed.hostname != "api.chatgpt.com":
        return {"status": "invalid_configuration"}
    try:
        with httpx.Client(timeout=settings.workspace_agent_timeout_seconds, follow_redirects=False) as client:
            response = client.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {settings.workspace_agent_access_token}",
                    "Content-Type": "application/json",
                    "OpenAI-Beta": "workspace_agent_runs=v1",
                    "Idempotency-Key": row.dedup_key,
                },
                json={"conversation_key": f"cleaningaios-{row.dedup_key[:24]}", "input": row.codex_prompt},
            )
            response.raise_for_status()
            body = response.json()
        conversation_url = str(body.get("conversation_url", ""))
        if conversation_url:
            target = urlparse(conversation_url)
            if target.scheme != "https" or target.hostname != "chatgpt.com":
                raise ValueError("Unexpected workspace conversation URL")
        return {
            "status": "queued",
            "conversation_url": conversation_url,
            "run_id": str(body.get("agent_trigger_run_id", ""))[:128],
        }
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        return {
            "status": "failed",
            "error": "Workspace agent handoff failed",
            "error_type": type(exc).__name__[:128],
        }


def retry_workspace_handoff(row: ImprovementRequest) -> dict[str, Any]:
    result = trigger_workspace_agent(row)
    row.handoff_status = str(result["status"])
    row.workspace_conversation_url = str(result.get("conversation_url", ""))
    row.workspace_run_id = str(result.get("run_id", ""))
    row.last_error = str(result.get("error", ""))
    if result["status"] == "queued":
        row.status = "handed_off"
    return result


def analyze_and_record(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    request_text = redact_sensitive_text(str(payload.get("message", "")))[:4000]
    intent = payload.get("intent") if isinstance(payload.get("intent"), dict) else {}
    baseline = deterministic_assessment(request_text, intent)
    assessment, llm_analysis = _merge_llm_assessment(request_text, intent, baseline)
    result = {**assessment, "llm_analysis": llm_analysis, "improvement_id": None, "handoff_status": "not_needed"}
    if not assessment["should_create_improvement"]:
        return result

    signature = json.dumps({"request": _normalize(request_text), "missing": assessment["missing_capabilities"]}, ensure_ascii=False, sort_keys=True)
    dedup_key = hashlib.sha256(signature.encode()).hexdigest()
    row = db.scalar(select(ImprovementRequest).where(ImprovementRequest.dedup_key == dedup_key))
    if row:
        row.occurrence_count += 1
        row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        if row.status in {"implemented", "rejected"}:
            row.status = "queued"
            row.handoff_status = "pending"
    else:
        row = ImprovementRequest(
            dedup_key=dedup_key,
            source_channel=str(payload.get("source_channel", "telegram"))[:32],
            source_user=str(payload.get("source_user", "owner"))[:128],
            request_text=request_text,
            intent=intent,
            capability_score=float(assessment["capability_score"]),
            classification=str(assessment["classification"])[:64],
            reason=str(assessment["reason"])[:4000],
            missing_capabilities=assessment["missing_capabilities"],
            suggested_function=str(assessment["suggested_function"])[:4000],
            codex_prompt=build_codex_prompt(request_text, assessment),
            acceptance_criteria=assessment["acceptance_criteria"],
            test_plan=assessment["test_plan"],
        )
        db.add(row)
        db.flush()
        row.codex_prompt = build_codex_prompt(request_text, assessment, row.id)
    handoff = retry_workspace_handoff(row) if row.handoff_status in {"pending", "credentials_required", "failed"} else {"status": row.handoff_status}
    result.update({"improvement_id": row.id, "handoff_status": handoff["status"], "workspace_conversation_url": row.workspace_conversation_url})
    return result


def record_execution_gap(db: Session, task: Task, reason: str, *, credentials_required: bool = False) -> dict[str, Any]:
    """Create one privacy-minimized, deduplicated Codex handoff for a failed user task."""
    action = str((task.payload or {}).get("action") or "unspecified")[:128]
    safe_reason = redact_sensitive_text(str(reason))[:1000]
    request_text = (
        f"Execution gap in Telegram task #{task.id}: agent={task.agent_type}, "
        f"action={action}, status={task.status}. Reason: {safe_reason}"
    )
    missing = ["external_credentials"] if credentials_required else ["verified_agent_execution"]
    assessment = {
        "fully_supported": False,
        "capability_score": 0.0,
        "classification": "configuration_required" if credentials_required else "execution_gap",
        "reason": safe_reason,
        "missing_capabilities": missing,
        "suggested_function": f"Устранить препятствие выполнения агента {task.agent_type} и добавить регрессионный тест",
        "acceptance_criteria": [
            f"Повтор задачи #{task.id} завершается проверяемым результатом, а не только постановкой в очередь.",
            "При неуспехе бот честно сообщает причину, improvement ID и ответственную сторону.",
            "CEO получает связанный отчёт об инциденте с исходной задачей и доказательствами.",
            "Owner approval и остальные защитные политики не обходятся.",
        ],
        "test_plan": [
            "Воспроизвести исходный сценарий через API/Telegram handler.",
            "Проверить дедупликацию improvement и связанный CEO incident report.",
            "Добавить регрессионный тест успешного и ошибочного сценариев.",
            "Запустить полный pytest и Docker health checks.",
        ],
        "should_create_improvement": True,
    }
    signature = json.dumps(
        {"source_task_id": task.id, "agent": task.agent_type, "action": action, "reason": safe_reason},
        ensure_ascii=False,
        sort_keys=True,
    )
    dedup_key = hashlib.sha256(signature.encode()).hexdigest()
    row = db.scalar(select(ImprovementRequest).where(ImprovementRequest.dedup_key == dedup_key))
    if row:
        row.occurrence_count += 1
        row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        if row.status in {"implemented", "rejected"}:
            row.status = "queued"
            row.handoff_status = "pending"
    else:
        row = ImprovementRequest(
            dedup_key=dedup_key,
            source_channel="system",
            source_user="orchestrator_quality_gate",
            request_text=request_text,
            intent={"kind": "execution_gap", "source_task_id": task.id, "agent_type": task.agent_type, "action": action},
            capability_score=0.0,
            classification=assessment["classification"],
            reason=safe_reason,
            missing_capabilities=missing,
            suggested_function=assessment["suggested_function"],
            codex_prompt=build_codex_prompt(request_text, assessment),
            acceptance_criteria=assessment["acceptance_criteria"],
            test_plan=assessment["test_plan"],
        )
        db.add(row)
        db.flush()
        row.codex_prompt = build_codex_prompt(request_text, assessment, row.id)
    handoff = retry_workspace_handoff(row) if row.handoff_status in {"pending", "credentials_required", "failed"} else {"status": row.handoff_status}
    return {
        "improvement_id": row.id,
        "handoff_status": handoff["status"],
        "responsible_party": "owner_configuration" if credentials_required else "system_codex",
    }


def record_agent_coaching_improvements(
    db: Session,
    coaching: dict[str, Any],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Turn bounded, advisory Perplexity findings into a deduplicated Codex backlog."""
    if coaching.get("status") != "succeeded":
        return []
    recorded: list[dict[str, Any]] = []
    for item in (coaching.get("recommendations") or [])[: max(0, min(limit, 10))]:
        if not isinstance(item, dict):
            continue
        agent_type = re.sub(r"[^a-z0-9_-]", "", str(item.get("agent_type", "general")).lower())[:64] or "general"
        change = redact_sensitive_text(str(item.get("change", "")).strip())[:2000]
        validation = redact_sensitive_text(str(item.get("validation", "")).strip())[:2000]
        expected_effect = redact_sensitive_text(str(item.get("expected_effect", "")).strip())[:2000]
        if not change or not validation:
            continue
        signature = json.dumps(
            {"provider": "perplexity_sonar", "agent_type": agent_type, "change": _normalize(change), "validation": _normalize(validation)},
            ensure_ascii=False,
            sort_keys=True,
        )
        dedup_key = hashlib.sha256(signature.encode()).hexdigest()
        row = db.scalar(select(ImprovementRequest).where(ImprovementRequest.dedup_key == dedup_key))
        if row:
            row.occurrence_count += 1
            row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        else:
            request_text = f"Perplexity agent-quality recommendation for {agent_type}: {change}"
            assessment = {
                "reason": expected_effect or "Research-grounded agent quality improvement",
                "suggested_function": change,
                "missing_capabilities": [f"{agent_type}_quality_improvement"],
                "acceptance_criteria": [
                    change,
                    expected_effect or "The change produces a measurable improvement without weakening safety controls.",
                    "RBAC, audit log and owner approval gates remain enforced.",
                ],
                "test_plan": [
                    validation,
                    "Add a deterministic regression test for the affected agent workflow.",
                    "Run the complete test suite and production health checks before implementation status is set.",
                ],
            }
            row = ImprovementRequest(
                dedup_key=dedup_key,
                source_channel="system",
                source_user="perplexity_agent_coach",
                request_text=request_text,
                intent={"kind": "agent_quality_improvement", "agent_type": agent_type, "advisory_only": True},
                capability_score=0.5,
                classification="agent_quality_gap",
                reason=assessment["reason"],
                missing_capabilities=assessment["missing_capabilities"],
                suggested_function=change,
                codex_prompt=build_codex_prompt(request_text, assessment),
                acceptance_criteria=assessment["acceptance_criteria"],
                test_plan=assessment["test_plan"],
                status="queued",
                handoff_status="pending",
            )
            db.add(row)
            db.flush()
            row.codex_prompt = build_codex_prompt(request_text, assessment, row.id)
        recorded.append({"id": row.id, "status": row.status, "agent_type": agent_type})
    return recorded


def record_evolution_research_improvements(
    db: Session,
    research: dict[str, Any],
    *,
    allowed_source_urls: set[str],
    limit: int,
) -> list[dict[str, Any]]:
    """Persist only recommendations grounded in the exact reviewed GitHub batch."""
    if research.get("status") != "succeeded":
        return []
    recorded: list[dict[str, Any]] = []
    bounded_limit = max(0, min(limit, 10))
    for item in (research.get("recommendations") or [])[:bounded_limit]:
        if not isinstance(item, dict):
            continue
        source_urls = sorted(
            {
                str(value)[:1000]
                for value in (item.get("source_urls") or [])[:5]
                if str(value) in allowed_source_urls
                and urlparse(str(value)).scheme == "https"
                and urlparse(str(value)).hostname == "github.com"
            }
        )
        title = redact_sensitive_text(str(item.get("title") or "").strip())[:240]
        change = redact_sensitive_text(str(item.get("change") or "").strip())[:2000]
        rationale = redact_sensitive_text(str(item.get("rationale") or "").strip())[:2000]
        validation = redact_sensitive_text(str(item.get("validation") or "").strip())[:1500]
        domain = re.sub(r"[^a-z_]", "", str(item.get("domain") or ""))[:64]
        if not title or not change or not validation or not source_urls:
            continue
        signature = json.dumps(
            {
                "kind": "evolution_research",
                "domain": domain,
                "change": _normalize(change),
                "sources": source_urls,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        dedup_key = hashlib.sha256(signature.encode()).hexdigest()
        row = db.scalar(select(ImprovementRequest).where(ImprovementRequest.dedup_key == dedup_key))
        if row:
            row.occurrence_count += 1
            row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        else:
            source_text = ", ".join(source_urls)
            request_text = f"AI Evolution Researcher: {title}. Проверенные источники: {source_text}"
            assessment = {
                "reason": rationale or "Source-grounded product evolution opportunity",
                "suggested_function": change,
                "missing_capabilities": [f"{domain or 'product'}_evolution"],
                "acceptance_criteria": [
                    change,
                    "Каждый заимствованный паттерн имеет совместимую лицензию или реализован независимо без копирования кода.",
                    "RBAC, audit log, идемпотентность и owner approval остаются включены.",
                    "Изменение не активируется автоматически на production до тестов и CI.",
                ],
                "test_plan": [
                    validation,
                    "Добавить детерминированный регрессионный тест изменяемого workflow.",
                    "Запустить полный pytest, миграции, Docker health checks и релевантный smoke test.",
                ],
            }
            row = ImprovementRequest(
                dedup_key=dedup_key,
                source_channel="system",
                source_user="github_evolution_researcher",
                request_text=request_text,
                intent={
                    "kind": "source_grounded_product_research",
                    "domain": domain,
                    "source_urls": source_urls,
                    "advisory_only": True,
                    "automatic_code_changes": False,
                    "owner_action_required": bool(item.get("owner_action_required")),
                    "owner_action": redact_sensitive_text(str(item.get("owner_action") or ""))[:1000],
                },
                capability_score=0.5,
                classification="source_grounded_improvement",
                reason=assessment["reason"],
                missing_capabilities=assessment["missing_capabilities"],
                suggested_function=change,
                codex_prompt=build_codex_prompt(request_text, assessment),
                acceptance_criteria=assessment["acceptance_criteria"],
                test_plan=assessment["test_plan"],
                status="queued",
                handoff_status="pending",
            )
            db.add(row)
            db.flush()
            row.codex_prompt = build_codex_prompt(request_text, assessment, row.id)
        recorded.append(
            {
                "id": row.id,
                "status": row.status,
                "domain": domain,
                "source_urls": source_urls,
            }
        )
    return recorded
