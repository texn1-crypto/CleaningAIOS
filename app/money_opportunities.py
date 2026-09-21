from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import settings
from .models import (
    ApprovalRequest,
    AuthorityEnvelope,
    AuthorityEnvelopeUse,
    BusinessRecord,
    InboxMessage,
    OperatingEntity,
    OutboundMessage,
    SenderMailbox,
    TenderAssessmentSnapshot,
)


ACTIVE_TENDER_STATUSES = frozenset(
    {"new", "screened", "data_required", "qualified", "awaiting_approval"}
)


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _decimal(value: object) -> Decimal:
    try:
        result = Decimal(str(value or 0))
    except (InvalidOperation, ValueError):
        return Decimal("0")
    return result if result.is_finite() else Decimal("0")


def _money(value: object) -> str:
    return format(_decimal(value).quantize(Decimal("0.01")), "f")


def _stored_revenue(lead: BusinessRecord) -> Decimal:
    data = lead.data if isinstance(lead.data, dict) else {}
    for key in ("recognized_revenue", "contract_value", "won_amount", "revenue"):
        if data.get(key) is not None:
            return max(Decimal("0"), _decimal(data[key]))
    return Decimal("0")


def _tender_lane(db: Session) -> dict[str, Any]:
    tenders = list(
        db.scalars(
            select(BusinessRecord)
            .where(BusinessRecord.record_type == "tender")
            .order_by(BusinessRecord.deadline_at, BusinessRecord.id)
        ).all()
    )
    cards: list[dict[str, Any]] = []
    for tender in [row for row in tenders if row.status in ACTIVE_TENDER_STATUSES][
        :20
    ]:
        snapshot = db.scalar(
            select(TenderAssessmentSnapshot)
            .where(TenderAssessmentSnapshot.record_id == tender.id)
            .order_by(TenderAssessmentSnapshot.id.desc())
        )
        result = snapshot.result_snapshot if snapshot else {}
        economics = result.get("economics") if isinstance(result, dict) else {}
        economics = economics if isinstance(economics, dict) else {}
        base: dict[str, Any] = (
            economics["base"] if isinstance(economics.get("base"), dict) else {}
        )
        conservative: dict[str, Any] = (
            economics["conservative"]
            if isinstance(economics.get("conservative"), dict)
            else {}
        )
        if snapshot is None:
            action = "run_assessment"
        elif snapshot.status == "ready_for_owner_review":
            action = "owner_participation_review"
        elif snapshot.status == "needs_verification":
            action = "provide_missing_evidence"
        elif snapshot.status == "owner_risk_review_required":
            action = "owner_risk_review"
        else:
            action = "none"
        cards.append(
            {
                "record_id": tender.id,
                "title": tender.title,
                "status": snapshot.status if snapshot else tender.status,
                "recommendation": snapshot.recommendation if snapshot else None,
                "expected_net_profit": base.get("net_profit"),
                "conservative_net_profit": conservative.get("net_profit"),
                "margin_percent": base.get("margin_percent"),
                "risk_score": (
                    (result.get("risk") or {}).get("score")
                    if isinstance(result.get("risk"), dict)
                    else None
                ),
                "deadline_at": tender.deadline_at,
                "missing_data": sorted(
                    set(result.get("verification_gaps") or [])
                    | set(result.get("hard_stops") or [])
                ),
                "next_action": action,
                "evidence": {
                    "source_record_id": tender.id,
                    "assessment_snapshot_id": snapshot.id if snapshot else None,
                    "assessment_input_hash": snapshot.input_hash if snapshot else None,
                },
            }
        )
    return {
        "summary": {
            "total": len(tenders),
            "active": sum(row.status in ACTIVE_TENDER_STATUSES for row in tenders),
            "ready_for_owner_review": sum(
                card["status"] == "ready_for_owner_review" for card in cards
            ),
            "with_verified_economics": sum(
                card["expected_net_profit"] is not None for card in cards
            ),
        },
        "cards": cards,
        "source": {
            "records": "business_records:record_type=tender",
            "assessments": "tender_assessment_snapshots",
            "configured_external_sources": len(
                [item for item in settings.tender_sources.split(",") if item.strip()]
            ),
        },
    }


def _sales_lane(db: Session, current: datetime) -> dict[str, Any]:
    leads = list(
        db.scalars(
            select(BusinessRecord)
            .where(BusinessRecord.record_type == "lead")
            .order_by(BusinessRecord.id.desc())
        ).all()
    )
    statuses = Counter(str(row.status or "unknown") for row in leads)
    outbound_status_rows = db.execute(
            select(OutboundMessage.status, func.count(OutboundMessage.id)).group_by(
                OutboundMessage.status
            )
        ).all()
    outbound_statuses: dict[str, int] = {
        str(status): int(count) for status, count in outbound_status_rows
    }
    replies = int(
        db.scalar(
            select(func.count(InboxMessage.id)).where(
                InboxMessage.channel == "email"
            )
        )
        or 0
    )
    contracts = list(
        db.scalars(
            select(OperatingEntity).where(OperatingEntity.entity_type == "contract")
        ).all()
    )
    active_contracts = [row for row in contracts if row.status == "active"]
    monthly_revenue = sum(
        _decimal((row.data or {}).get("monthly_revenue")) for row in active_contracts
    )
    overdue_next_actions = 0
    hot_cards: list[dict[str, Any]] = []
    qualification_outcomes: Counter[str] = Counter()
    owner_review_untriaged = 0
    for lead in leads:
        data = lead.data if isinstance(lead.data, dict) else {}
        if lead.status == "owner_review":
            qualification = data.get("qualification")
            if isinstance(qualification, dict) and qualification.get("outcome"):
                qualification_outcomes[str(qualification["outcome"])] += 1
            else:
                owner_review_untriaged += 1
        due_raw = data.get("next_action_at")
        if isinstance(due_raw, str):
            try:
                due = datetime.fromisoformat(due_raw.replace("Z", "+00:00"))
                if due.tzinfo is not None:
                    due = due.astimezone(timezone.utc).replace(tzinfo=None)
                overdue_next_actions += due < current
            except ValueError:
                pass
        if lead.status == "qualified" or float(lead.score or 0) >= settings.hot_lead_score:
            hot_cards.append(
                {
                    "record_id": lead.id,
                    "title": lead.title,
                    "status": lead.status,
                    "score": lead.score,
                    "source": lead.source,
                    "next_action": data.get("next_action") or "qualify_next_step",
                    "next_action_at": data.get("next_action_at"),
                    "utm_campaign": data.get("utm_campaign"),
                    "evidence": {"source_record_id": lead.id},
                }
            )
    pending_outreach = int(
        db.scalar(
            select(func.count(ApprovalRequest.id)).where(
                ApprovalRequest.action_kind == "bulk_outreach",
                ApprovalRequest.status == "pending",
            )
        )
        or 0
    )
    return {
        "summary": {
            "leads": len(leads),
            "owner_review": statuses.get("owner_review", 0),
            "owner_review_triaged": sum(qualification_outcomes.values()),
            "owner_review_untriaged": owner_review_untriaged,
            "owner_action_queue": qualification_outcomes.get("owner_review", 0),
            "qualification_research": qualification_outcomes.get("research", 0),
            "qualification_nurture": qualification_outcomes.get("nurture", 0),
            "qualification_sales_ready": qualification_outcomes.get("sales_ready", 0),
            "qualification_reject": qualification_outcomes.get("reject", 0),
            "qualified": statuses.get("qualified", 0),
            "sales_ready": statuses.get("sales_ready", 0),
            "won": statuses.get("won", 0),
            "lost": statuses.get("lost", 0),
            "outbound_sent": int(outbound_statuses.get("sent", 0)),
            "email_replies": replies,
            "pending_outreach_approvals": pending_outreach,
            "overdue_next_actions": overdue_next_actions,
            "active_contracts": len(active_contracts),
            "active_monthly_revenue": _money(monthly_revenue),
        },
        "hot_leads": hot_cards[:20],
        "funnel": {
            "lead_statuses": dict(sorted(statuses.items())),
            "outbound_statuses": dict(sorted(outbound_statuses.items())),
        },
        "source": {
            "leads": "business_records:record_type=lead",
            "outbound": "outbound_messages",
            "replies": "inbox_messages:channel=email",
            "contracts": "operating_entities:entity_type=contract",
        },
    }


def _marketing_lane(db: Session) -> dict[str, Any]:
    experiments = list(
        db.scalars(
            select(BusinessRecord)
            .where(BusinessRecord.record_type == "marketing_experiment")
            .order_by(BusinessRecord.id.desc())
        ).all()
    )
    leads = list(
        db.scalars(
            select(BusinessRecord).where(BusinessRecord.record_type == "lead")
        ).all()
    )
    cards: list[dict[str, Any]] = []
    total_spend = Decimal("0")
    total_revenue = Decimal("0")
    for row in experiments[:20]:
        data = row.data if isinstance(row.data, dict) else {}
        attributed = [
            lead
            for lead in leads
            if (lead.data or {}).get("utm_campaign") == row.external_id
        ]
        won = [lead for lead in attributed if lead.status == "won"]
        spend = max(Decimal("0"), _decimal(data.get("spent")))
        revenue = sum(_stored_revenue(lead) for lead in won)
        total_spend += spend
        total_revenue += revenue
        roi = (
            ((revenue - spend) / spend * Decimal("100")).quantize(Decimal("0.01"))
            if spend > 0 and revenue > 0
            else None
        )
        if row.status in {"approval", "pending_approval"}:
            action = "owner_budget_review"
        elif row.status == "draft":
            action = "prepare_channel_launch"
        elif row.status == "running":
            action = "monitor_performance"
        else:
            action = "none"
        cards.append(
            {
                "experiment_id": row.id,
                "title": row.title,
                "channel": row.source,
                "status": row.status,
                "budget_limit": data.get("budget_limit"),
                "actual_spend": _money(spend),
                "leads": len(attributed),
                "qualified_leads": sum(
                    lead.status == "qualified"
                    or float(lead.score or 0) >= settings.hot_lead_score
                    for lead in attributed
                ),
                "won": len(won),
                "recognized_revenue": _money(revenue),
                "roi_percent": str(roi) if roi is not None else None,
                "roi_confidence": (
                    "stored_revenue_based" if roi is not None else "insufficient_data"
                ),
                "external_campaign_verified": bool(
                    data.get("external_campaign_verified_at")
                ),
                "next_action": action,
                "evidence": {
                    "source_record_id": row.id,
                    "attributed_lead_ids": [lead.id for lead in attributed],
                    "won_lead_ids": [lead.id for lead in won],
                },
            }
        )
    portfolio_roi = (
        ((total_revenue - total_spend) / total_spend * Decimal("100")).quantize(
            Decimal("0.01")
        )
        if total_spend > 0 and total_revenue > 0
        else None
    )
    return {
        "summary": {
            "experiments": len(experiments),
            "running": sum(row.status == "running" for row in experiments),
            "actual_spend": _money(total_spend),
            "recognized_revenue": _money(total_revenue),
            "roi_percent": str(portfolio_roi) if portfolio_roi is not None else None,
            "roi_confidence": (
                "stored_revenue_based"
                if portfolio_roi is not None
                else "insufficient_data"
            ),
        },
        "cards": cards,
        "connections": {
            "yandex_direct": "credentials_present"
            if settings.yandex_direct_token
            else "credentials_required",
            "vk_ads": "credentials_present"
            if settings.vk_ads_token
            else "credentials_required",
            "telegram_ads": "credentials_present"
            if settings.telegram_ads_token
            else "credentials_required",
            "automatic_payment": "forbidden",
        },
        "source": {
            "experiments": "business_records:record_type=marketing_experiment",
            "attribution": "business_records:record_type=lead:data.utm_campaign",
            "spend": "marketing_experiment.data.spent",
            "revenue": "won lead stored revenue fields",
        },
    }


def _autonomy_lane(db: Session, current: datetime) -> dict[str, Any]:
    active = list(
        db.scalars(
            select(AuthorityEnvelope).where(
                AuthorityEnvelope.status == "active",
                AuthorityEnvelope.starts_at <= current,
                AuthorityEnvelope.expires_at > current,
            )
        ).all()
    )
    usage_count = int(db.scalar(select(func.count(AuthorityEnvelopeUse.id))) or 0)
    return {
        "active_envelopes": len(active),
        "authorized_actions": usage_count,
        "envelopes": [
            {
                "id": row.id,
                "action": row.action,
                "expires_at": row.expires_at,
                "input_hash": row.input_hash,
            }
            for row in active
        ],
    }


def _credentials_lane(db: Session) -> dict[str, str]:
    inbound_mailboxes = int(
        db.scalar(
            select(func.count(SenderMailbox.id)).where(
                SenderMailbox.active.is_(True),
                SenderMailbox.inbound_enabled.is_(True),
            )
        )
        or 0
    )
    return {
        "postgresql": "configured"
        if settings.database_url.startswith("postgresql")
        else "development_sqlite",
        "telegram": "configured"
        if settings.telegram_bot_token and settings.owner_telegram_id
        else "credentials_required",
        "smtp": "configured"
        if all(
            (
                settings.smtp_host,
                settings.smtp_username,
                settings.smtp_password,
                settings.smtp_from_email,
            )
        )
        else "credentials_required",
        "imap": "configured" if inbound_mailboxes else "mailbox_configuration_required",
        "tender_sources": "configured"
        if settings.tender_sources.strip()
        else "source_configuration_required",
        "yandex_direct": "credentials_present"
        if settings.yandex_direct_token
        else "credentials_required",
    }


def build_money_opportunities(db: Session) -> dict[str, Any]:
    """Return a factual, source-labelled snapshot; never synthesise opportunities."""

    current = now_utc()
    return {
        "generated_at": current,
        "facts_only": True,
        "empty_is_honest": True,
        "tenders": _tender_lane(db),
        "sales": _sales_lane(db, current),
        "marketing": _marketing_lane(db),
        "autonomy": _autonomy_lane(db, current),
        "credentials": _credentials_lane(db),
    }
