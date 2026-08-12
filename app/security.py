from __future__ import annotations

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


def principal(
    x_api_key: Optional[str] = Header(default=None),
    x_actor: str = Header(default="api-user"),
    x_role: str = Header(default="owner"),
) -> Principal:
    if settings.production and not x_api_key:
        raise HTTPException(401, "X-API-Key is required")
    if x_api_key and not secrets.compare_digest(x_api_key, settings.api_key):
        raise HTTPException(401, "Invalid API key")
    role = x_role if x_role in ROLE_ORDER else "viewer"
    return Principal(x_actor, role)


def require_role(current: Principal, minimum: str) -> None:
    if ROLE_ORDER[current.role] < ROLE_ORDER[minimum]:
        raise HTTPException(403, f"Role {minimum} required")
