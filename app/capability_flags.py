from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from .models import CapabilityFlag, SafetyControl


PROTECTED_CAPABILITIES = (
    "agent_replay",
    "bulk_outreach",
    "contract",
    "financial",
    "hr_final",
    "legal",
    "social_publication",
    "tender_participation",
    "tender_submission",
)
PROTECTED_CAPABILITY_SET = frozenset(PROTECTED_CAPABILITIES)
TENDER_SCOPED_CAPABILITIES = frozenset(
    {"tender_participation", "tender_submission"}
)
INITIAL_COMPATIBILITY_REASON = (
    "Initial compatibility flag; owner approval remains mandatory"
)
GLOBAL_EXTERNAL_ACTIONS_CONTROL = "global_external_actions"
TENDER_EXTERNAL_ACTIONS_CONTROL_PREFIX = "tender_external_actions:"


def tender_external_actions_control_key(tender_id: int) -> str:
    if isinstance(tender_id, bool) or tender_id <= 0:
        raise ValueError("Tender ID must be a positive integer")
    return f"{TENDER_EXTERNAL_ACTIONS_CONTROL_PREFIX}{tender_id}"


def external_action_gate(
    db: Session,
    capability_key: str,
    *,
    tender_id: int | None = None,
) -> dict[str, Any]:
    """Evaluate the shared persisted stop controls for an external action.

    Callers must still enforce action-specific approval, consent and rate limits.
    This gate adds the global stop, an optional per-tender stop and the exact
    capability flag, in that order. A missing capability row fails closed.
    """

    if capability_key not in PROTECTED_CAPABILITY_SET:
        raise ValueError("Unknown protected capability")
    kill_switch = db.get(SafetyControl, GLOBAL_EXTERNAL_ACTIONS_CONTROL)
    if kill_switch is not None and kill_switch.active:
        return {
            "allowed": False,
            "reason": "global_kill_switch_active",
            "kill_switch": {
                "key": kill_switch.key,
                "version": kill_switch.version,
                "reason": kill_switch.reason,
            },
        }
    if tender_id is not None:
        tender_control = db.get(
            SafetyControl,
            tender_external_actions_control_key(tender_id),
        )
        if tender_control is not None and tender_control.active:
            return {
                "allowed": False,
                "reason": "tender_kill_switch_active",
                "tender_id": tender_id,
                "kill_switch": {
                    "key": tender_control.key,
                    "version": tender_control.version,
                    "reason": tender_control.reason,
                },
            }
    capability_flag = db.get(CapabilityFlag, capability_key)
    if capability_flag is None or not capability_flag.enabled:
        return {
            "allowed": False,
            "reason": "capability_disabled",
            "capability_flag": {
                "key": capability_key,
                "enabled": False,
                "version": capability_flag.version if capability_flag else 0,
                "reason": (
                    capability_flag.reason
                    if capability_flag
                    else "capability_flag_missing"
                ),
            },
        }
    return {
        "allowed": True,
        "reason": "capability_enabled",
        "capability_flag": {
            "key": capability_key,
            "enabled": True,
            "version": capability_flag.version,
            "reason": capability_flag.reason,
        },
    }


def ensure_capability_flags(db: Session) -> None:
    """Idempotently materialize the complete code-owned capability registry."""

    for key in PROTECTED_CAPABILITIES:
        if db.get(CapabilityFlag, key) is None:
            db.add(
                CapabilityFlag(
                    key=key,
                    enabled=True,
                    reason=INITIAL_COMPATIBILITY_REASON,
                    version=1,
                    updated_by="system",
                )
            )
    db.flush()


def capability_flag_view(row: CapabilityFlag) -> dict[str, Any]:
    return {
        "key": row.key,
        "enabled": row.enabled,
        "reason": row.reason,
        "version": row.version,
        "updated_by": row.updated_by,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }
