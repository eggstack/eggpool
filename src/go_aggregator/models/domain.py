"""Internal domain objects."""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from datetime import datetime


class UsageExactnessLevel(Enum):
    EXACT = "exact"
    DERIVED = "derived"
    ESTIMATED = "estimated"
    UNKNOWN = "unknown"


class Account(BaseModel):
    id: int
    name: str
    enabled: bool = True
    weight: float = 1.0
    api_key_env: str
    created_at: datetime


class AccountRuntimeState(BaseModel):
    health_state: str = "healthy"
    cooldown_until: datetime | None = None
    consecutive_failures: int = 0
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    active_request_count: int = 0
    reserved_microdollars: int = 0


class ModelDescriptor(BaseModel):
    model_id: str
    display_name: str | None = None
    protocol: str = "openai"
    capabilities: dict[str, object] = {}
    source_metadata: dict[str, object] = {}
    first_seen_at: datetime
    last_seen_at: datetime
