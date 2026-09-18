from __future__ import annotations

import hashlib
import ipaddress
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse, urlunparse

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from .lead_reports import LEAD_REPORT_RECORD_TYPE, build_instant_lead_report
from .management_companies import normalize_emails, normalize_phones
from .models import AuditLog, BusinessGoal, BusinessRecord, OwnerNotification, Task
from .notifications import queue_owner_notification
from .task_state import record_task_created


LEAD_HANDOFF_METRIC = "qualified_owner_handoffs"
MANAGEMENT_COMPANY_RECORD_TYPE = "management_company"
VERIFICATION_ACTION = "verify_existing_management_company_candidate"
VERIFICATION_BATCH_SIZE = 8
VERIFICATION_MAX_ATTEMPTS = 3
_TITLE_STOP_WORDS = {
    "ао",
    "гбу",
    "жк",
    "жск",
    "зао",
    "компания",
    "ооо",
    "пао",
    "тсж",
    "тсн",
    "ук",
    "управляющая",
}
_SCOUT_AGENTS = {
    "commercial_lead_scout",
    "management_lead_scout",
    "social_lead_scout",
    "tender_lead_scout",
}
_FREE_MAIL_DOMAINS = {
    "gmail.com",
    "mail.ru",
    "outlook.com",
    "rambler.ru",
    "yandex.com",
    "yandex.ru",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _month_bounds(current: datetime) -> tuple[datetime, datetime]:
    start = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


def _lead_goal(db: Session) -> BusinessGoal | None:
    return db.scalar(
        select(BusinessGoal)
        .where(
            BusinessGoal.metric == LEAD_HANDOFF_METRIC,
            BusinessGoal.status == "active",
        )
        .order_by(BusinessGoal.id)
    )


def reconcile_lead_handoff_goal(
    db: Session,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Update the lead goal from reports that actually reached the owner."""

    current = now or utcnow()
    goal = _lead_goal(db)
    if goal is None:
        return {
            "status": "goal_not_configured",
            "metric": LEAD_HANDOFF_METRIC,
            "current": 0,
            "target": 0,
            "shortfall": 0,
            "source": "sent owner notifications for lead discovery reports",
        }
    start, end = _month_bounds(current)
    notifications = db.scalars(
        select(OwnerNotification).where(
            OwnerNotification.resource_type == LEAD_REPORT_RECORD_TYPE,
            OwnerNotification.status == "sent",
            OwnerNotification.sent_at.is_not(None),
            OwnerNotification.sent_at >= start,
            OwnerNotification.sent_at < end,
        )
    ).all()
    report_ids = {
        int(row.resource_id)
        for row in notifications
        if str(row.resource_id).isdigit()
    }
    lead_ids: set[int] = set()
    for report_id in report_ids:
        report = db.get(BusinessRecord, report_id)
        if report is None or report.record_type != LEAD_REPORT_RECORD_TYPE:
            continue
        for value in (report.data or {}).get("lead_ids") or []:
            if not str(value).isdigit():
                continue
            lead = db.get(BusinessRecord, int(value))
            if (
                lead is not None
                and lead.record_type == "lead"
                and lead.status in {"owner_review", "qualified", "sales_ready", "won"}
            ):
                lead_ids.add(lead.id)
    previous = float(goal.current)
    goal.current = float(len(lead_ids))
    if previous != goal.current:
        db.add(
            AuditLog(
                actor="ceo",
                action="business_goal.measured",
                resource_type="business_goal",
                resource_id=str(goal.id),
                details={
                    "metric": goal.metric,
                    "previous": previous,
                    "current": goal.current,
                    "period_start": start.isoformat(),
                    "period_end": end.isoformat(),
                    "sent_report_count": len(report_ids),
                },
            )
        )
    target = max(0, int(goal.target))
    measured = len(lead_ids)
    return {
        "status": "target_met" if measured >= target else "behind_target",
        "goal_id": goal.id,
        "title": goal.title,
        "metric": goal.metric,
        "current": measured,
        "target": target,
        "shortfall": max(0, target - measured),
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "sent_report_count": len(report_ids),
        "source": "sent owner notifications for lead discovery reports",
    }


def _safe_candidate_url(value: object) -> str | None:
    raw = str(value or "").strip()
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower().rstrip(".")
    try:
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username
        or parsed.password
        or port not in {None, 443}
        or host == "localhost"
        or host.endswith((".local", ".internal", ".localhost"))
    ):
        return None
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return None
    return urlunparse(("https", host, parsed.path or "/", "", parsed.query, ""))


def _verification_candidates(db: Session) -> list[tuple[BusinessRecord, str]]:
    status_json = BusinessRecord.data["internet_verification_status"].as_string()
    rows = db.scalars(
        select(BusinessRecord)
        .where(
            BusinessRecord.record_type == MANAGEMENT_COMPANY_RECORD_TYPE,
            BusinessRecord.data["scope_status"].as_string() == "in_scope",
            or_(
                status_json.is_(None),
                status_json.notin_({"verified_public_website", "needs_manual_review"}),
            ),
        )
        .order_by(
            case((status_json == "not_checked", 0), else_=1),
            BusinessRecord.id,
        )
        .limit(2_048)
    ).all()
    candidates: list[tuple[BusinessRecord, str]] = []
    for row in rows:
        data = row.data or {}
        url = _safe_candidate_url(data.get("candidate_website"))
        if url is None:
            continue
        if not normalize_emails(data.get("email"), data.get("emails")) and not normalize_phones(
            data.get("phone"), data.get("phones")
        ):
            continue
        candidates.append((row, url))
    return sorted(
        candidates,
        key=lambda item: (
            bool((item[0].data or {}).get("internet_verification_checked_at")),
            str((item[0].data or {}).get("internet_verification_checked_at") or ""),
            item[0].id,
        ),
    )


def _latest_provider_health(db: Session) -> dict[str, Any]:
    rows = db.scalars(
        select(Task)
        .where(
            Task.agent_type.in_(_SCOUT_AGENTS),
            Task.payload["action"].as_string() == "discover_public_business_leads",
        )
        .order_by(Task.id.desc())
        .limit(100)
    ).all()
    if not rows:
        return {"status": "not_measured", "task_ids": [], "errors": []}
    latest_by_agent: dict[str, Task] = {}
    for row in rows:
        latest_by_agent.setdefault(row.agent_type, row)
    unavailable: list[Task] = []
    for row in latest_by_agent.values():
        result = row.result or {}
        if row.status in {"failed", "blocked"} or result.get("status") == "unavailable":
            unavailable.append(row)
    errors = sorted(
        {
            str((row.result or {}).get("error") or (row.result or {}).get("reason") or row.status)[:160]
            for row in unavailable
        }
    )
    return {
        "status": "unavailable" if unavailable else "available",
        "task_ids": sorted(row.id for row in latest_by_agent.values()),
        "unavailable_task_ids": sorted(row.id for row in unavailable),
        "errors": errors,
        "responsible_party": "owner_configuration" if unavailable else None,
    }


def _schedule_verification_tasks(
    db: Session,
    *,
    current: datetime,
    cycle_key: str,
) -> dict[str, Any]:
    created: list[int] = []
    reused: list[int] = []
    candidates = _verification_candidates(db)
    day_key = current.date().isoformat()
    for row, url in candidates[:VERIFICATION_BATCH_SIZE]:
        digest = hashlib.sha256(url.encode()).hexdigest()[:12]
        title = f"Lead outcome · verify management company {row.id} · {day_key} · {digest}"
        existing = db.scalar(select(Task).where(Task.title == title))
        if existing is not None:
            reused.append(existing.id)
            continue
        task = Task(
            title=title,
            description=(
                "Проверить публичный сайт организации, сохранить подтверждённый лид в CRM "
                "и передать его владельцу без автоматической рассылки."
            ),
            agent_type="lead_coordinator",
            status="queued",
            priority="high",
            max_attempts=3,
            run_after=current,
            due_at=current + timedelta(hours=4),
            payload={
                "action": VERIFICATION_ACTION,
                "record_id": row.id,
                "candidate_url": url,
                "cycle_key": cycle_key,
                "notify_owner": True,
                "automatic_outreach": False,
                "read_only_tools": [
                    {
                        "name": "web.public_crawl",
                        "arguments": {"url": url, "max_chars": 8_000},
                    }
                ],
            },
        )
        db.add(task)
        db.flush()
        record_task_created(
            db,
            task,
            actor="ceo",
            reason="lead_goal_shortfall_fallback_verification",
        )
        created.append(task.id)
    return {
        "eligible_candidates": len(candidates),
        "tasks_created": created,
        "tasks_reused": reused,
        "batch_limit": VERIFICATION_BATCH_SIZE,
    }


def _title_tokens(title: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-zа-я0-9]{3,}", title.lower().replace("ё", "е"))
        if token not in _TITLE_STOP_WORDS
    }


def _host(value: object) -> str:
    return (urlparse(str(value or "")).hostname or "").lower().removeprefix("www.").rstrip(".")


def _organization_evidence_matches(record: BusinessRecord, url: str, markdown: str) -> tuple[bool, list[str]]:
    normalized_text = " ".join(markdown.lower().replace("ё", "е").split())
    title_tokens = _title_tokens(record.title)
    matched_tokens = sorted(token for token in title_tokens if token in normalized_text)
    data = record.data or {}
    email_domains = {
        email.rsplit("@", 1)[1].lower().removeprefix("www.")
        for email in normalize_emails(data.get("email"), data.get("emails"))
        if email.rsplit("@", 1)[1].lower().removeprefix("www.") not in _FREE_MAIL_DOMAINS
    }
    host = _host(url)
    domain_match = any(host == domain or host.endswith("." + domain) or domain.endswith("." + host) for domain in email_domains)
    name_match = len(title_tokens) >= 2 and len(matched_tokens) >= 2
    inn = re.sub(r"\D", "", str(data.get("inn") or ""))
    page_digits = re.sub(r"\D", "", markdown)
    inn_match = len(inn) in {10, 12} and inn in page_digits
    reasons: list[str] = []
    if name_match:
        reasons.append("organization_name_in_public_page")
    if domain_match:
        reasons.append("organization_email_domain_matches_website")
    if inn_match:
        reasons.append("organization_inn_in_public_page")
    return bool(name_match or domain_match or inn_match), reasons


def _record_verification_failure(
    db: Session,
    *,
    row: BusinessRecord,
    failure_status: str,
    reason: str,
    checked_at: datetime,
    source_url: str,
    content_sha256: str = "",
) -> tuple[int, bool]:
    data = row.data or {}
    try:
        previous_attempts = max(0, int(data.get("internet_verification_attempts") or 0))
    except (TypeError, ValueError):
        previous_attempts = 0
    attempts = previous_attempts + 1
    manual_review = attempts >= VERIFICATION_MAX_ATTEMPTS
    row.data = {
        **data,
        "internet_verification_status": (
            "needs_manual_review" if manual_review else failure_status
        ),
        "internet_verification_attempts": attempts,
        "internet_verification_checked_at": checked_at.isoformat(),
        "internet_verification_content_sha256": content_sha256[:80],
    }
    db.add(
        AuditLog(
            actor="lead_coordinator",
            action="management_company.website_not_confirmed",
            resource_type=MANAGEMENT_COMPANY_RECORD_TYPE,
            resource_id=str(row.id),
            details={
                "candidate_url": source_url,
                "reason": reason,
                "attempts": attempts,
                "manual_review_required": manual_review,
                "content_stored": False,
            },
        )
    )
    return attempts, manual_review


def _reconcile_failed_verification_tasks(db: Session) -> dict[str, Any]:
    """Apply the candidate retry limit when the guarded tool fails before agent execution."""

    failed_tasks = db.scalars(
        select(Task)
        .where(
            Task.agent_type == "lead_coordinator",
            Task.status == "failed",
            Task.payload["action"].as_string() == VERIFICATION_ACTION,
        )
        .order_by(Task.id.desc())
        .limit(200)
    ).all()
    reconciled: list[int] = []
    manual_review: list[int] = []
    for task in failed_tasks:
        result = task.result or {}
        if result.get("error_type") not in {
            "AgentToolDenied",
            "AgentToolError",
            "AgentToolTimedOut",
        }:
            continue
        payload = task.payload or {}
        try:
            raw_record_id = payload.get("record_id")
            if raw_record_id is None or isinstance(raw_record_id, bool):
                continue
            record_id = int(raw_record_id)
        except (TypeError, ValueError):
            continue
        row = db.get(BusinessRecord, record_id)
        if row is None or row.record_type != MANAGEMENT_COMPANY_RECORD_TYPE:
            continue
        data = row.data or {}
        if data.get("internet_verification_last_failed_task_id") == task.id:
            continue
        source_url = _safe_candidate_url(payload.get("candidate_url"))
        if source_url is None:
            source_url = "invalid_candidate_url"
        _, needs_manual_review = _record_verification_failure(
            db,
            row=row,
            failure_status="candidate_tool_rejected",
            reason=str(result.get("error_type") or "agent_tool_failure"),
            checked_at=task.updated_at or utcnow(),
            source_url=source_url,
        )
        row.data = {
            **(row.data or {}),
            "internet_verification_last_failed_task_id": task.id,
        }
        reconciled.append(task.id)
        if needs_manual_review:
            manual_review.append(row.id)
    return {
        "reconciled_task_ids": sorted(reconciled),
        "manual_review_record_ids": sorted(manual_review),
    }


def verify_existing_management_company_candidate(
    db: Session,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Turn one verified public company page into an owner-review lead, never outreach."""

    raw_record_id = payload.get("record_id")
    try:
        if raw_record_id is None or isinstance(raw_record_id, bool):
            raise ValueError
        record_id = int(raw_record_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("record_id must identify a management company") from exc
    row = db.get(BusinessRecord, record_id)
    if row is None or row.record_type != MANAGEMENT_COMPANY_RECORD_TYPE:
        raise ValueError("Management company record was not found")
    requested_url = _safe_candidate_url(payload.get("candidate_url"))
    stored_url = _safe_candidate_url((row.data or {}).get("candidate_website"))
    if requested_url is None or requested_url != stored_url:
        raise ValueError("Candidate website does not match the CRM record")
    tool_results = payload.get("read_only_tool_results")
    if not isinstance(tool_results, list) or len(tool_results) != 1:
        raise ValueError("Exactly one audited Crawl4AI result is required")
    tool_result = tool_results[0]
    crawl = tool_result.get("result") if isinstance(tool_result, dict) else None
    if (
        not isinstance(crawl, dict)
        or tool_result.get("name") != "web.public_crawl"
        or crawl.get("success") is not True
    ):
        attempts, manual_review = _record_verification_failure(
            db,
            row=row,
            failure_status="candidate_unavailable",
            reason="public_page_unavailable",
            checked_at=utcnow(),
            source_url=requested_url,
        )
        return {
            "status": "not_verified",
            "record_id": row.id,
            "reason": "public_page_unavailable",
            "verification_attempts": attempts,
            "manual_review_required": manual_review,
            "external_messages_sent": False,
            "evidence": [{"type": "management_company_website_check", "record_id": row.id, "matched": False}],
        }
    resolved_url = _safe_candidate_url(crawl.get("resolved_url"))
    if resolved_url is None or _host(resolved_url) != _host(requested_url):
        attempts, manual_review = _record_verification_failure(
            db,
            row=row,
            failure_status="candidate_redirect_mismatch",
            reason="website_redirected_to_different_host",
            checked_at=utcnow(),
            source_url=requested_url,
        )
        return {
            "status": "not_verified",
            "record_id": row.id,
            "reason": "website_redirected_to_different_host",
            "verification_attempts": attempts,
            "manual_review_required": manual_review,
            "external_messages_sent": False,
            "evidence": [{"type": "management_company_website_check", "record_id": row.id, "matched": False}],
        }
    markdown = str(crawl.get("markdown") or "")
    matched, match_reasons = _organization_evidence_matches(row, resolved_url, markdown)
    checked_at = utcnow()
    if not matched:
        attempts, manual_review = _record_verification_failure(
            db,
            row=row,
            failure_status="candidate_not_confirmed",
            reason="organization_identity_not_confirmed",
            checked_at=checked_at,
            source_url=requested_url,
            content_sha256=str(crawl.get("content_sha256") or ""),
        )
        return {
            "status": "not_verified",
            "record_id": row.id,
            "reason": "organization_identity_not_confirmed",
            "verification_attempts": attempts,
            "manual_review_required": manual_review,
            "external_messages_sent": False,
            "evidence": [{"type": "management_company_website_check", "record_id": row.id, "matched": False}],
        }

    source_hash = str(crawl.get("content_sha256") or "")[:80]
    provenance = [dict(item) for item in (row.data or {}).get("provenance") or [] if isinstance(item, dict)]
    web_provenance = {
        "source_kind": "crawl4ai_public_website_verification",
        "source_url": resolved_url,
        "content_sha256": source_hash,
        "verified_at": checked_at.isoformat(),
    }
    if web_provenance not in provenance:
        provenance.append(web_provenance)
    row.data = {
        **(row.data or {}),
        "website": resolved_url,
        "internet_verification_status": "verified_public_website",
        "internet_verification_checked_at": checked_at.isoformat(),
        "internet_verification_content_sha256": source_hash,
        "internet_verification_attempts": int(
            (row.data or {}).get("internet_verification_attempts") or 0
        )
        + 1,
        "provenance": provenance,
    }

    external_id = f"management-company:{row.id}"
    lead = db.scalar(
        select(BusinessRecord).where(
            BusinessRecord.record_type == "lead",
            BusinessRecord.external_id == external_id,
        )
    )
    created = lead is None
    if lead is None:
        lead = BusinessRecord(
            record_type="lead",
            external_id=external_id,
            title=row.title,
            owner="sales",
            source="verified_management_company_website",
        )
        db.add(lead)
        db.flush()
    company_data = row.data or {}
    emails = normalize_emails(company_data.get("email"), company_data.get("emails"))
    phones = normalize_phones(company_data.get("phone"), company_data.get("phones"))
    lead.title = row.title
    lead.status = "owner_review"
    lead.score = float(min(95, 65 + (10 if emails else 0) + (10 if phones else 0)))
    lead.owner = "sales"
    lead.source = "verified_management_company_website"
    lead.data = {
        **(lead.data or {}),
        "region": str(company_data.get("region") or ""),
        "city": str(company_data.get("region") or ""),
        "organization_type": str(company_data.get("organization_type") or "management_company"),
        "website": resolved_url,
        "public_emails": emails,
        "public_phones": phones,
        "source_urls": [resolved_url],
        "linked_management_company_id": row.id,
        "last_verified_at": checked_at.isoformat(),
        "verification_reasons": match_reasons,
        "contact_scope": "organization",
        "outreach_consent": "not_verified",
        "marketing_contact_allowed": False,
        "automatic_outreach": False,
        "inn": str(company_data.get("inn") or ""),
    }
    report = build_instant_lead_report(
        db,
        lead_ids=[lead.id],
        scout_role="management_lead_scout",
        generated_at=checked_at,
        notify_owner=bool(payload.get("notify_owner", True)),
    )
    db.add(
        AuditLog(
            actor="lead_coordinator",
            action="management_company.verified_as_lead",
            resource_type="lead",
            resource_id=str(lead.id),
            details={
                "management_company_id": row.id,
                "created": created,
                "report_id": report.get("report_id"),
                "automatic_outreach": False,
                "content_stored": False,
            },
        )
    )
    return {
        "status": "owner_review",
        "record_id": row.id,
        "lead_id": lead.id,
        "lead_created": created,
        "instant_lead_report": report,
        "automatic_outreach": False,
        "external_messages_sent": False,
        "evidence": [
            {
                "type": "verified_management_company_lead",
                "management_company_id": row.id,
                "lead_id": lead.id,
                "source_url": resolved_url,
                "content_sha256": source_hash,
                "verification_reasons": match_reasons,
            }
        ],
    }


def run_ceo_lead_outcome_cycle(
    db: Session,
    *,
    cycle_key: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Make the CEO accountable for measured owner handoffs, not task volume."""

    current = now or utcnow()
    failed_verification_reconciliation = _reconcile_failed_verification_tasks(db)
    goal = reconcile_lead_handoff_goal(db, now=current)
    provider = _latest_provider_health(db)
    scheduled = (
        _schedule_verification_tasks(db, current=current, cycle_key=cycle_key)
        if goal["status"] != "target_met"
        else {"eligible_candidates": 0, "tasks_created": [], "tasks_reused": [], "batch_limit": VERIFICATION_BATCH_SIZE}
    )
    manual_review_count = int(
        db.scalar(
            select(func.count(BusinessRecord.id)).where(
                BusinessRecord.record_type == MANAGEMENT_COMPANY_RECORD_TYPE,
                BusinessRecord.data["internet_verification_status"].as_string()
                == "needs_manual_review",
            )
        )
        or 0
    )
    provider_recovery_task: Task | None = None
    if provider["status"] == "unavailable":
        title = f"CEO → System Admin · Lead provider recovery · {current.date().isoformat()}"
        provider_recovery_task = db.scalar(select(Task).where(Task.title == title))
        if provider_recovery_task is None:
            provider_recovery_task = Task(
                title=title,
                description=(
                    "Проверить доступность внешнего поискового провайдера, не запрашивая и не "
                    "сохраняя секреты в задаче."
                ),
                agent_type="system_admin",
                status="queued",
                priority="critical",
                max_attempts=3,
                run_after=current,
                due_at=current + timedelta(hours=4),
                payload={
                    "action": "system_admin_audit",
                    "source": "ceo_lead_provider_health",
                    "notify_owner": True,
                    "provider_status": "unavailable",
                    "failed_task_ids": provider.get("unavailable_task_ids") or [],
                    "notification_idempotency_key": (
                        f"ceo-lead-provider:{current.date().isoformat()}:telegram"
                    ),
                },
            )
            db.add(provider_recovery_task)
            db.flush()
            record_task_created(
                db,
                provider_recovery_task,
                actor="ceo",
                reason="lead_provider_unavailable",
            )
    deadline = (current + timedelta(hours=24)).replace(microsecond=0).isoformat()
    priorities = [
        {
            "priority": 1,
            "accountable_agent": "lead_coordinator",
            "action": "Проверить публичные сайты уже собранных управляющих компаний через Crawl4AI и создать карточки лидов в CRM.",
            "metric": f"до {VERIFICATION_BATCH_SIZE} проверок за цикл; создано задач: {len(scheduled['tasks_created'])}",
            "deadline": deadline,
            "dependencies": ["Crawl4AI", "CRM management_company"],
            "approval_required": False,
            "stop_condition": "цель месяца достигнута или проверяемые кандидаты исчерпаны",
        },
        {
            "priority": 2,
            "accountable_agent": "ceo",
            "action": "Считать результатом только лиды, чей отчёт фактически доставлен владельцу.",
            "metric": f"{goal['current']} из {goal['target']} передач за текущий месяц",
            "deadline": deadline,
            "dependencies": ["owner notification delivery"],
            "approval_required": False,
            "stop_condition": "метрика подтверждена доставленными уведомлениями",
        },
        {
            "priority": 3,
            "accountable_agent": "system_admin",
            "action": "Восстановить внешний канал поиска, если последняя проверка провайдера недоступна.",
            "metric": "последний запуск каждого lead scout завершён с доступным провайдером",
            "deadline": deadline,
            "dependencies": ["Perplexity/API configuration"],
            "approval_required": provider["status"] == "unavailable",
            "stop_condition": "контрольный поиск возвращает валидные публичные результаты",
        },
        {
            "priority": 4,
            "accountable_agent": "sales",
            "action": "Держать новые карточки в owner_review; не отправлять сообщения без подтверждённого согласия и отдельного допуска.",
            "metric": "0 несанкционированных внешних сообщений",
            "deadline": deadline,
            "dependencies": ["consent registry", "suppression list"],
            "approval_required": True,
            "stop_condition": "владелец выбрал дальнейшее действие по конкретному лиду",
        },
    ]
    risk_reasons: list[str] = []
    if goal["status"] == "goal_not_configured":
        risk_reasons.append("lead_handoff_goal_not_configured")
    elif goal["shortfall"] > 0:
        risk_reasons.append("monthly_owner_handoff_target_not_met")
    if provider["status"] == "unavailable":
        risk_reasons.append("external_lead_provider_unavailable")
    if goal["status"] != "target_met" and scheduled["eligible_candidates"] == 0:
        risk_reasons.append("verified_fallback_candidates_exhausted")
    if manual_review_count:
        risk_reasons.append("management_company_candidates_need_manual_review")
    status = "on_track" if not risk_reasons else "at_risk"
    notification = queue_owner_notification(
        db,
        idempotency_key=f"ceo-lead-plan:{current.date().isoformat()}:telegram",
        channel="telegram",
        resource_type="business_goal",
        resource_id=str(goal.get("goal_id") or "lead-handoffs"),
        subject="CEO-план лидогенерации на 24 часа",
        body=(
            f"Результат месяца: {goal['current']} из {goal['target']} подтверждённых передач. "
            f"CEO поставил {len(scheduled['tasks_created'])} новых проверок базы. "
            f"Статус внешнего поиска: {provider['status']}. Автоматическая рассылка не выполняется."
        ),
        data={
            "cycle_key": cycle_key,
            "goal": goal,
            "provider_health": provider,
            "verification_work": scheduled,
            "failed_verification_reconciliation": failed_verification_reconciliation,
            "manual_review_count": manual_review_count,
            "provider_recovery_task_id": (
                provider_recovery_task.id if provider_recovery_task is not None else None
            ),
            "priorities": priorities,
        },
        severity="high" if status == "at_risk" else "normal",
        correlation_id=cycle_key[:128],
    )
    return {
        "status": status,
        "risk_reasons": risk_reasons,
        "goal": goal,
        "provider_health": provider,
        "verification_work": scheduled,
        "failed_verification_reconciliation": failed_verification_reconciliation,
        "manual_review_count": manual_review_count,
        "provider_recovery_task_id": (
            provider_recovery_task.id if provider_recovery_task is not None else None
        ),
        "priorities": priorities,
        "owner_plan_notification_id": notification.id,
        "automatic_outreach": False,
        "external_messages_sent": False,
        "evidence": [
            {
                "type": "ceo_lead_outcome_control",
                "goal_current": goal["current"],
                "goal_target": goal["target"],
                "fallback_tasks_created": len(scheduled["tasks_created"]),
                "provider_status": provider["status"],
            }
        ],
    }


def main() -> None:
    from .db import SessionLocal

    current = utcnow()
    with SessionLocal() as db:
        result = run_ceo_lead_outcome_cycle(
            db,
            cycle_key=f"manual-deploy:{current.isoformat(timespec='minutes')}",
            now=current,
        )
        db.commit()
        print(
            {
                "status": result["status"],
                "goal": result["goal"],
                "verification_work": result["verification_work"],
                "provider_status": result["provider_health"]["status"],
            }
        )


if __name__ == "__main__":
    main()
