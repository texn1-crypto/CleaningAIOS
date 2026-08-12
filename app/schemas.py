from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, EmailStr, Field


class TaskCreate(BaseModel):
    title: str = Field(min_length=2, max_length=255)
    description: str = ""
    priority: str = "normal"
    agent_type: str = "orchestrator"
    payload: dict[str, Any] = Field(default_factory=dict)
    run_after: Optional[datetime] = None


class DecisionCreate(BaseModel):
    title: str = Field(min_length=2, max_length=255)
    rationale: str = ""
    kind: str = "operational"
    payload: dict[str, Any] = Field(default_factory=dict)


class RecordCreate(BaseModel):
    record_type: str = Field(min_length=2, max_length=64)
    title: str = Field(min_length=2, max_length=255)
    external_id: Optional[str] = None
    status: str = "new"
    score: Optional[float] = None
    owner: Optional[str] = None
    data: dict[str, Any] = Field(default_factory=dict)
    source: str = "manual"
    deadline_at: Optional[datetime] = None


class OutreachCreate(BaseModel):
    campaign_key: str = Field(min_length=2, max_length=128)
    recipient: EmailStr
    subject: str = Field(min_length=1, max_length=255)
    body: str = Field(min_length=1)
    scheduled_at: Optional[datetime] = None


class SuppressionCreate(BaseModel):
    address: EmailStr
    reason: str = "manual"
