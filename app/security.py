from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from typing import Optional

from fastapi import Header, HTTPException

from .config import settings


@dataclass(frozen=True)
class Principal:
    subject: str
    role: str


ROLE_ORDER = {"viewer": 0, "operator": 1, "manager": 2, "owner": 3}


def configured_api_keys() -> list[tuple[str, str, str]]:
    return [
        (settings.api_key, "owner", "owner-api-key"),
        (settings.manager_api_key, "manager", "manager-api-key"),
        (settings.operator_api_key, "operator", "operator-api-key"),
        (settings.viewer_api_key, "viewer", "viewer-api-key"),
    ]


def validate_production_security() -> None:
    if not settings.production:
        return
    if not settings.api_key or settings.api_key == "development-only-change-me":
        raise RuntimeError("API_KEY must be replaced in production")
    keys = [key for key, _, _ in configured_api_keys() if key]
    if len(keys) != len(set(keys)):
        raise RuntimeError("Production API keys must be unique")


def unsubscribe_token(address: str) -> str:
    secret = settings.unsubscribe_secret or settings.api_key
    return hmac.new(secret.encode(), address.strip().lower().encode(), hashlib.sha256).hexdigest()


def valid_unsubscribe_token(address: str, token: str) -> bool:
    return bool(token) and secrets.compare_digest(token, unsubscribe_token(address))


def principal(
    x_api_key: Optional[str] = Header(default=None),
    x_actor: str = Header(default="api-user"),
    x_role: str = Header(default="owner"),
) -> Principal:
    if x_api_key:
        for configured_key, role, subject in configured_api_keys():
            if configured_key and secrets.compare_digest(x_api_key, configured_key):
                return Principal(subject if settings.production else x_actor, role)
        raise HTTPException(401, "Invalid API key")
    if settings.production:
        raise HTTPException(401, "X-API-Key is required")
    role = x_role if x_role in ROLE_ORDER else "viewer"
    return Principal(x_actor, role)


def require_role(current: Principal, minimum: str) -> None:
    if ROLE_ORDER[current.role] < ROLE_ORDER[minimum]:
        raise HTTPException(403, f"Role {minimum} required")
