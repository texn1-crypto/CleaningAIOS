from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

import httpx
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .ai_router import provider_catalog
from .config import settings
from .improvements import record_evolution_research_improvements
from .llm import llm_advisor
from .models import AgentState, BusinessRecord, ImprovementRequest, Task
from .notifications import queue_owner_notification


GITHUB_API_ROOT = "https://api.github.com"
GITHUB_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
PERMISSIVE_LICENSES = {
    "Apache-2.0",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "ISC",
    "MIT",
    "Unlicense",
}
RECIPROCAL_LICENSES = {"LGPL-2.1", "LGPL-3.0", "MPL-2.0"}
USER_AGENT = "CleaningAIOS-Evolution-Research/1.0"

ARCHITECTURE_PRINCIPLES = [
    "One shared SQL data model and audited task runtime for all agents.",
    "Deterministic policy gates decide execution; external AI providers are advisory only.",
    "RBAC, owner approval, idempotency and evidence are preserved across workflows.",
    "Bulk outreach keeps consent, suppression, unsubscribe and rate-limit controls.",
    "Changes reach production only after regression tests, full pytest, health checks and CI.",
]


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def configured_queries() -> list[str]:
    return [
        item.strip()[:256]
        for item in settings.evolution_research_queries.split("|")
        if item.strip()
    ]


def daily_research_slice(now: datetime, queries: list[str]) -> tuple[str, int]:
    if not queries:
        raise ValueError("EVOLUTION_RESEARCH_QUERIES is empty")
    localized = now.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(settings.evolution_research_timezone))
    day = localized.date().toordinal()
    query_index = day % len(queries)
    # GitHub search exposes at most ten 100-item pages. Rotating both query and
    # page grows a bounded corpus without repeatedly downloading page one.
    page = (day // len(queries)) % 10 + 1
    return queries[query_index], page


def _github_headers(*, raw: bool = False) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github.raw+json" if raw else "application/vnd.github+json",
        "User-Agent": USER_AGENT,
    }
    if settings.github_research_token:
        headers["Authorization"] = f"Bearer {settings.github_research_token}"
    return headers


def _license_policy(spdx_id: str) -> str:
    if spdx_id in PERMISSIVE_LICENSES:
        return "pattern_review_allowed"
    if spdx_id in RECIPROCAL_LICENSES:
        return "legal_review_required"
    if spdx_id and spdx_id not in {"NOASSERTION", "OTHER"}:
        return "reference_only_copyleft_or_custom"
    return "reference_only_unverified_license"


def _validated_repo_url(value: object) -> str:
    url = str(value or "")
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "github.com" or parsed.username or parsed.password:
        return ""
    return url[:1000]


def _safe_repo(item: dict[str, Any]) -> dict[str, Any] | None:
    full_name = str(item.get("full_name") or "")
    html_url = _validated_repo_url(item.get("html_url"))
    if not GITHUB_REPOSITORY_RE.fullmatch(full_name) or not html_url or item.get("private"):
        return None
    license_value = item.get("license") if isinstance(item.get("license"), dict) else {}
    spdx_id = str(license_value.get("spdx_id") or "NOASSERTION")[:64]
    topics = [str(topic)[:80] for topic in (item.get("topics") or [])[:20]]
    return {
        "full_name": full_name,
        "url": html_url,
        "description": str(item.get("description") or "")[:1000],
        "language": str(item.get("language") or "")[:80],
        "topics": topics,
        "stars": max(0, int(item.get("stargazers_count") or 0)),
        "forks": max(0, int(item.get("forks_count") or 0)),
        "open_issues": max(0, int(item.get("open_issues_count") or 0)),
        "updated_at": str(item.get("updated_at") or "")[:64],
        "license_spdx": spdx_id,
        "license_policy": _license_policy(spdx_id),
        "archived": bool(item.get("archived")),
    }


def _raise_for_github_status(response: httpx.Response) -> None:
    if response.status_code in {403, 429}:
        reset = response.headers.get("x-ratelimit-reset") or response.headers.get("retry-after") or "unknown"
        raise RuntimeError(f"GitHub API rate limit reached; retry after {reset}")
    response.raise_for_status()


def collect_github_repositories(query: str, page: int, *, limit: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    bounded_limit = max(1, min(limit, 20))
    with httpx.Client(timeout=settings.evolution_research_timeout_seconds, follow_redirects=False) as client:
        requested_page = max(1, min(page, 10))
        params = {
            "q": f"{query} fork:false archived:false",
            "sort": "stars",
            "order": "desc",
            "per_page": bounded_limit,
            "page": requested_page,
        }
        search = client.get(
            f"{GITHUB_API_ROOT}/search/repositories",
            headers=_github_headers(),
            params=params,
        )
        _raise_for_github_status(search)
        body = search.json()
        total_count = max(0, int(body.get("total_count") or 0))
        searchable_count = min(total_count, 1000)
        available_pages = max(1, min(10, (searchable_count + bounded_limit - 1) // bounded_limit))
        effective_page = (requested_page - 1) % available_pages + 1
        if effective_page != requested_page:
            params = {**params, "page": effective_page}
            search = client.get(
                f"{GITHUB_API_ROOT}/search/repositories",
                headers=_github_headers(),
                params=params,
            )
            _raise_for_github_status(search)
            body = search.json()
        repositories: list[dict[str, Any]] = []
        for raw_item in (body.get("items") or [])[:bounded_limit]:
            if not isinstance(raw_item, dict):
                continue
            item = _safe_repo(raw_item)
            if not item or item["archived"]:
                continue
            owner, repo = item["full_name"].split("/", 1)
            readme = client.get(
                f"{GITHUB_API_ROOT}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/readme",
                headers=_github_headers(raw=True),
            )
            if readme.status_code == 200:
                raw = readme.content[:12_001]
                if len(raw) <= 12_000:
                    text = raw.decode("utf-8", errors="replace")
                    item["readme_excerpt"] = " ".join(text.split())[:3000]
                    item["readme_sha256"] = hashlib.sha256(raw).hexdigest()
            elif readme.status_code in {403, 429}:
                _raise_for_github_status(readme)
            repositories.append(item)
        return repositories, {
            "query": query,
            "requested_page": requested_page,
            "page": effective_page,
            "github_total_count": max(0, int(body.get("total_count") or 0)),
            "github_incomplete_results": bool(body.get("incomplete_results")),
            "rate_limit_remaining": search.headers.get("x-ratelimit-remaining", "unknown"),
        }


def _persist_source(db: Session, source: dict[str, Any], *, query: str, observed_at: datetime) -> BusinessRecord:
    external_id = f"github:{source['full_name'].lower()}"
    row = db.scalar(
        select(BusinessRecord).where(
            BusinessRecord.record_type == "evolution_research_source",
            BusinessRecord.external_id == external_id,
        )
    )
    history = [] if row is None else list((row.data or {}).get("query_history") or [])
    if query not in history:
        history = (history + [query])[-20:]
    data = {
        **source,
        "query_history": history,
        "last_observed_at": observed_at.isoformat(),
        "source_kind": "github_public_repository",
        "automatic_code_import": False,
    }
    if row is None:
        row = BusinessRecord(
            record_type="evolution_research_source",
            external_id=external_id,
            title=source["full_name"][:255],
            status="researched",
            source="github_api",
            data=data,
        )
        db.add(row)
    else:
        row.status = "researched"
        row.data = data
    return row


def build_project_snapshot(db: Session, *, registered_agents: list[str]) -> dict[str, Any]:
    task_counts = {
        str(status): int(count)
        for status, count in db.execute(
            select(Task.status, func.count(Task.id)).group_by(Task.status)
        ).all()
    }
    state_counts = {
        str(status): int(count)
        for status, count in db.execute(
            select(AgentState.status, func.count(AgentState.agent_type)).group_by(AgentState.status)
        ).all()
    }
    return {
        "application": settings.app_name,
        "architecture_principles": ARCHITECTURE_PRINCIPLES,
        "registered_agents": sorted(registered_agents),
        "provider_statuses": [
            {
                "capability": item["capability"],
                "provider": item["provider"],
                "status": item["status"],
            }
            for item in provider_catalog()
        ],
        "task_status_counts": task_counts,
        "agent_state_counts": state_counts,
        "queued_improvements": int(
            db.scalar(
                select(func.count(ImprovementRequest.id)).where(ImprovementRequest.status == "queued")
            )
            or 0
        ),
        "constraints": {
            "no_customer_personal_data": True,
            "no_secrets": True,
            "no_automatic_external_code_execution": True,
            "license_review_required": True,
            "owner_approval_preserved": True,
        },
    }


def _font_path() -> Path:
    candidates = [
        settings.proposal_font_path,
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    raise RuntimeError("Cyrillic TTF font is required for the evolution PDF")


def _register_font() -> str:
    name = "CleaningAIEvolution"
    if name not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(name, str(_font_path())))
    return name


def _paragraph(value: object, style: ParagraphStyle) -> Paragraph:
    return Paragraph(escape(str(value or "")).replace("\n", "<br/>"), style)


def build_evolution_pdf(
    path: Path,
    *,
    generated_at: datetime,
    research: dict[str, Any],
    repositories: list[dict[str, Any]],
) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    font = _register_font()
    styles = getSampleStyleSheet()
    title = ParagraphStyle("EvolutionTitle", parent=styles["Title"], fontName=font, fontSize=22, leading=28, textColor=colors.HexColor("#173f32"), alignment=TA_LEFT, spaceAfter=5 * mm)
    h2 = ParagraphStyle("EvolutionH2", parent=styles["Heading2"], fontName=font, fontSize=13, leading=17, textColor=colors.HexColor("#174f3c"), spaceBefore=4 * mm, spaceAfter=2 * mm)
    body = ParagraphStyle("EvolutionBody", parent=styles["BodyText"], fontName=font, fontSize=9.5, leading=14, textColor=colors.HexColor("#29332e"), spaceAfter=2 * mm)
    small = ParagraphStyle("EvolutionSmall", parent=body, fontSize=7.5, leading=10, textColor=colors.HexColor("#657068"))
    doc = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        rightMargin=16 * mm,
        leftMargin=16 * mm,
        topMargin=17 * mm,
        bottomMargin=16 * mm,
        title="CleaningAIOS - отчёт агента развития",
        author=settings.company_name,
    )

    def page(canvas, document):
        canvas.saveState()
        canvas.setFillColor(colors.HexColor("#174f3c"))
        canvas.rect(0, A4[1] - 6 * mm, A4[0], 6 * mm, fill=1, stroke=0)
        canvas.setFont(font, 7.5)
        canvas.setFillColor(colors.HexColor("#657068"))
        canvas.drawString(16 * mm, 8 * mm, "CleaningAIOS - advisory research, not an automatic code release")
        canvas.drawRightString(A4[0] - 16 * mm, 8 * mm, f"Страница {document.page}")
        canvas.restoreState()

    status = str(research.get("status") or "unavailable")
    summary = str(research.get("summary") or "Проверяемые рекомендации не сформированы.")
    story: list[Any] = [
        _paragraph("AI Evolution Researcher", title),
        _paragraph(f"Ежедневный отчёт - {generated_at.strftime('%d.%m.%Y %H:%M UTC')}", small),
        Spacer(1, 2 * mm),
        Table(
            [[_paragraph("Режим", small), _paragraph("Только исследование и рекомендации", body)], [_paragraph("Статус анализа", small), _paragraph(status, body)], [_paragraph("Источников в партии", small), _paragraph(len(repositories), body)]],
            colWidths=[42 * mm, 132 * mm],
            style=TableStyle([
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#e8f1ed")),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#b8c9c1")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]),
        ),
        _paragraph("Краткий вывод", h2),
        _paragraph(summary, body),
        _paragraph("Рекомендации для CleaningAIOS", h2),
    ]
    recommendations = research.get("recommendations") if isinstance(research.get("recommendations"), list) else []
    if not recommendations:
        story.append(_paragraph("Нет рекомендации, прошедшей проверку источников. Код и настройки не изменялись.", body))
    for index, item in enumerate(recommendations[:8], 1):
        story.extend([
            _paragraph(f"{index}. {item.get('title') or 'Без названия'}", h2),
            _paragraph(item.get("change") or "", body),
            _paragraph(f"Почему: {item.get('rationale') or 'не указано'}", body),
            _paragraph(f"Проверка: {item.get('validation') or 'не указана'}", body),
        ])
        if item.get("owner_action_required"):
            story.append(_paragraph(f"Нужно от владельца: {item.get('owner_action') or 'решение владельца'}", body))
        source_urls = [str(value) for value in (item.get("source_urls") or [])[:5]]
        if source_urls:
            story.append(_paragraph("Источники: " + "; ".join(source_urls), small))

    story.append(_paragraph("Исследованные открытые репозитории", h2))
    for source in repositories[:20]:
        story.append(
            _paragraph(
                f"{source['full_name']} - {source['stars']} stars - {source['license_spdx']} "
                f"({source['license_policy']})\n{source['url']}",
                small,
            )
        )
    story.extend([
        _paragraph("Границы автономности", h2),
        _paragraph(
            "Агент не запускает найденный код, не копирует код с непроверенной лицензией, не меняет production, "
            "не регистрирует компанию, не оплачивает сервисы и не выполняет защищённые бизнес-действия. "
            "Каждое изменение проходит отдельную improvement-задачу, тесты, CI и существующие подтверждения владельца.",
            body,
        ),
    ])
    temporary = path.with_suffix(".tmp.pdf")
    doc.filename = str(temporary)
    doc.build(story, onFirstPage=page, onLaterPages=page)
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_evolution_research(
    db: Session,
    *,
    registered_agents: list[str],
    now: datetime | None = None,
    notify_owner: bool = False,
) -> dict[str, Any]:
    current = now or utcnow()
    queries = configured_queries()
    if not queries:
        return {
            "status": "configuration_required",
            "reason": "EVOLUTION_RESEARCH_QUERIES is empty",
            "credentials_required": [],
            "evidence": [],
        }
    query, page = daily_research_slice(current, queries)
    try:
        repositories, collection = collect_github_repositories(
            query,
            page,
            limit=settings.evolution_research_max_sources_per_cycle,
        )
    except (httpx.HTTPError, RuntimeError, TypeError, ValueError) as exc:
        repositories = []
        collection = {
            "query": query,
            "page": page,
            "github_total_count": 0,
            "github_incomplete_results": True,
            "rate_limit_remaining": "unknown",
            "error_type": type(exc).__name__,
        }

    for repository in repositories:
        _persist_source(db, repository, query=query, observed_at=current)
    db.flush()
    if repositories:
        project = build_project_snapshot(db, registered_agents=registered_agents)
        research = llm_advisor.research_evolution(
            {
                "research_kind": "source_grounded_product_evolution",
                "project": project,
                "github_query": collection,
                "github_sources": repositories,
                "benchmark_targets": {
                    "global_companies": 10_000,
                    "russian_cleaning_companies": 100,
                    "status": "separate_source_corpus_required",
                },
            }
        )
    else:
        source_status = "source_unavailable" if collection.get("error_type") else "no_sources"
        research = {
            "status": source_status,
            "provider": None,
            "model": None,
            "summary": (
                "GitHub-источники в этой партии недоступны; рекомендации не создавались."
                if source_status == "source_unavailable"
                else "Подходящие GitHub-источники в этой партии не найдены; рекомендации не создавались."
            ),
            "findings": [],
            "recommendations": [],
        }
    improvements = record_evolution_research_improvements(
        db,
        research,
        allowed_source_urls={item["url"] for item in repositories},
        limit=settings.evolution_research_max_improvements_per_cycle,
    )
    report_day = current.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(settings.evolution_research_timezone)).date().isoformat()
    report_path = Path(settings.document_storage_path) / "reports" / "evolution" / f"evolution-research-{report_day}.pdf"
    checksum = build_evolution_pdf(
        report_path,
        generated_at=current,
        research=research,
        repositories=repositories,
    )
    notification_status = "not_requested"
    notification_id = None
    if notify_owner:
        notification = queue_owner_notification(
            db,
            idempotency_key=f"evolution-research:{report_day}:telegram",
            channel="telegram",
            resource_type="evolution_research_report",
            resource_id=report_day,
            subject="Ежедневный отчёт AI Evolution Researcher",
            body=(
                f"Проверено репозиториев: {len(repositories)}. "
                f"Новых/повторных improvement-записей: {len(improvements)}. "
                "PDF содержит источники, лицензионные ограничения и действия, которые требуют владельца."
            ),
            data={
                "document_path": str(report_path),
                "document_filename": report_path.name,
                "document_sha256": checksum,
                "document_content_type": "application/pdf",
                "research_status": research.get("status"),
            },
        )
        notification_status = notification.status
        notification_id = notification.id
    corpus_size = int(
        db.scalar(
            select(func.count(BusinessRecord.id)).where(
                BusinessRecord.record_type == "evolution_research_source"
            )
        )
        or 0
    )
    return {
        "status": "completed" if research.get("status") == "succeeded" else research.get("status"),
        "research_status": research.get("status"),
        "provider": research.get("provider"),
        "model": research.get("model"),
        "summary": research.get("summary", ""),
        "query": query,
        "page": page,
        "sources_researched": len(repositories),
        "github_corpus_size": corpus_size,
        "improvements": improvements,
        "report": {
            "filename": report_path.name,
            "storage_path": str(report_path),
            "checksum_sha256": checksum,
        },
        "owner_notification": notification_status,
        "owner_notification_id": notification_id,
        "automatic_code_changes_applied": False,
        "evidence": [
            {
                "type": "github_public_repository_batch",
                **collection,
                "source_count": len(repositories),
            },
            {
                "type": "license_and_execution_gate",
                "automatic_code_import": False,
                "source_count": len(repositories),
            },
            {
                "type": "evolution_pdf",
                "filename": report_path.name,
                "checksum_sha256": checksum,
            },
        ],
    }
