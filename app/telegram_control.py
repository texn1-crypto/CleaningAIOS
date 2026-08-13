from __future__ import annotations

import base64
import hashlib
import hmac
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from .config import settings
from .models import ApprovalRequest, AuditLog, RoleBinding
from .security import ROLE_ORDER


CALLBACK_RE = re.compile(
    r"^tc1\.([0-9a-z]+)\.([0-9a-z]+)\.([arc])\.([0-9a-z]+)\.([A-Za-z0-9_-]{16})$"
)
ACTION_CODE = {"approve": "a", "reject": "r", "request_changes": "c"}
CODE_ACTION = {value: key for key, value in ACTION_CODE.items()}
RISK_BY_ACTION = {
    "financial": "critical",
    "legal": "critical",
    "contract": "critical",
    "hr_final": "critical",
    "tender_submission": "critical",
    "bulk_outreach": "high",
    "social_publication": "high",
}


@dataclass(frozen=True)
class TelegramIdentity:
    subject: str
    role: str
    user_id: int
    chat_id: int


class CallbackTokenError(ValueError):
    pass


class CallbackTokenExpired(CallbackTokenError):
    pass


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def telegram_subject(user_id: int, chat_id: int) -> str:
    return f"telegram:{int(user_id)}:{int(chat_id)}"


def audit_subject(user_id: int, chat_id: int, *, bound: bool = True) -> str:
    """Return a stable pseudonym so Telegram identifiers never enter logs."""
    secret = settings.telegram_callback_secret or settings.api_key or "local-audit"
    digest = hmac.new(
        secret.encode(),
        f"{int(user_id)}:{int(chat_id)}".encode(),
        hashlib.sha256,
    ).hexdigest()[:20]
    prefix = "telegram" if bound else "telegram-unbound"
    return f"{prefix}:{digest}"


def _denied_actor(user_id: int, chat_id: int) -> str:
    return audit_subject(user_id, chat_id, bound=False)


def resolve_identity(db: Session, *, user_id: int, chat_id: int) -> TelegramIdentity | None:
    owner_chat_id = settings.owner_telegram_chat_id or settings.owner_telegram_id
    if (
        settings.owner_telegram_id
        and owner_chat_id
        and str(user_id) == str(settings.owner_telegram_id)
        and str(chat_id) == str(owner_chat_id)
    ):
        return TelegramIdentity(
            subject="telegram-owner",
            role="owner",
            user_id=int(user_id),
            chat_id=int(chat_id),
        )
    subject = telegram_subject(user_id, chat_id)
    binding = db.get(RoleBinding, subject)
    if not binding or not binding.active or binding.role not in ROLE_ORDER:
        return None
    return TelegramIdentity(
        subject=audit_subject(user_id, chat_id),
        role=binding.role,
        user_id=int(user_id),
        chat_id=int(chat_id),
    )


def authorize_identity(
    db: Session,
    *,
    user_id: int,
    chat_id: int,
    minimum_role: str,
) -> tuple[TelegramIdentity | None, str]:
    if minimum_role not in ROLE_ORDER:
        raise ValueError("Unknown Telegram minimum role")
    identity = resolve_identity(db, user_id=user_id, chat_id=chat_id)
    if identity is None:
        reason = "identity_not_bound"
    elif ROLE_ORDER[identity.role] < ROLE_ORDER[minimum_role]:
        reason = "role_not_allowed"
    else:
        return identity, "authorized"
    db.add(
        AuditLog(
            actor=identity.subject if identity else _denied_actor(user_id, chat_id),
            action="telegram.access_denied",
            resource_type="telegram_control",
            resource_id="",
            details={"minimum_role": minimum_role, "reason": reason},
        )
    )
    return None, reason


def bind_identity(
    db: Session,
    *,
    user_id: int,
    chat_id: int,
    role: str,
) -> RoleBinding:
    if role not in ROLE_ORDER:
        raise ValueError("Unknown Telegram role")
    subject = telegram_subject(user_id, chat_id)
    binding = db.get(RoleBinding, subject)
    if binding is None:
        binding = RoleBinding(subject=subject, role=role, active=True)
        db.add(binding)
    else:
        binding.role = role
        binding.active = True
    db.flush()
    return binding


def _base36(value: int) -> str:
    if value < 0:
        raise ValueError("Negative callback values are not allowed")
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    if value == 0:
        return "0"
    result = ""
    while value:
        value, remainder = divmod(value, 36)
        result = alphabet[remainder] + result
    return result


def _callback_secret() -> bytes:
    secret = settings.telegram_callback_secret or settings.api_key
    if not secret:
        raise RuntimeError("Telegram callback secret is not configured")
    return secret.encode()


def _signature(body: str) -> str:
    digest = hmac.new(_callback_secret(), body.encode(), hashlib.sha256).digest()[:12]
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def issue_callback_token(
    approval: ApprovalRequest,
    action: str,
    *,
    now: datetime | None = None,
) -> str:
    if action not in ACTION_CODE:
        raise ValueError("Unsupported callback action")
    current = now or now_utc()
    expires_at = approval.expires_at or current + timedelta(
        hours=max(1, settings.approval_ttl_hours)
    )
    expires_epoch = int(expires_at.replace(tzinfo=timezone.utc).timestamp())
    body = ".".join(
        (
            "tc1",
            _base36(approval.id),
            _base36(approval.decision_version),
            ACTION_CODE[action],
            _base36(expires_epoch),
        )
    )
    token = f"{body}.{_signature(body)}"
    if len(token.encode()) > 64:
        raise RuntimeError("Telegram callback token exceeds 64 bytes")
    return token


def parse_callback_token(
    token: str,
    *,
    now: datetime | None = None,
) -> dict[str, int | str]:
    match = CALLBACK_RE.fullmatch(token or "")
    if not match:
        raise CallbackTokenError("Malformed approval callback")
    approval_raw, version_raw, action_code, expiry_raw, signature = match.groups()
    body = ".".join(("tc1", approval_raw, version_raw, action_code, expiry_raw))
    if not hmac.compare_digest(signature, _signature(body)):
        raise CallbackTokenError("Invalid approval callback signature")
    expires_epoch = int(expiry_raw, 36)
    current_epoch = int((now or now_utc()).replace(tzinfo=timezone.utc).timestamp())
    if expires_epoch < current_epoch:
        raise CallbackTokenExpired("Approval callback expired")
    return {
        "approval_id": int(approval_raw, 36),
        "decision_version": int(version_raw, 36),
        "action": CODE_ACTION[action_code],
        "expires_epoch": expires_epoch,
    }


def approval_card(approval: ApprovalRequest) -> dict:
    payload = approval.payload if isinstance(approval.payload, dict) else {}
    amount = next(
        (
            payload[key]
            for key in ("amount", "budget_limit", "total", "price")
            if payload.get(key) not in (None, "")
        ),
        None,
    )
    callbacks = {}
    if approval.status == "pending" and (
        approval.expires_at is None or approval.expires_at > now_utc()
    ):
        callbacks = {
            action: issue_callback_token(approval, action)
            for action in ("approve", "reject", "request_changes")
        }
    return {
        "id": approval.id,
        "action_kind": approval.action_kind,
        "resource_type": approval.resource_type,
        "resource_id": approval.resource_id,
        "status": approval.status,
        "risk": str(payload.get("risk") or RISK_BY_ACTION.get(approval.action_kind, "medium")),
        "amount": amount,
        "rationale": approval.rationale,
        "requested_by": approval.requested_by,
        "decision_version": approval.decision_version,
        "expires_at": approval.expires_at,
        "callbacks": callbacks,
    }
