"""Pydantic models for database row mapping."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from datetime import datetime


class AccountRow(BaseModel):
    id: int
    name: str
    api_key_env: str
    enabled: bool
    weight: float
    created_at: datetime


class ModelRow(BaseModel):
    model_id: str
    display_name: str | None = None
    protocol: str
    capabilities: str
    source_metadata: str
    first_seen_at: datetime
    last_seen_at: datetime


class AccountModelRow(BaseModel):
    account_id: int
    model_id: str
    enabled: bool
    created_at: datetime


class RequestRow(BaseModel):
    id: int
    account_id: int
    model_id: str
    started_at: datetime
    completed_at: datetime | None = None
    status: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_microdollars: int = 0
    bytes_received: int = 0
    bytes_emitted: int = 0
    upstream_latency_ms: float = 0
    error_message: str | None = None
    protocol: str = "openai"
    streamed: bool = False
    exactness: str = "unknown"
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    thinking_characters: int | None = None
    reserved_microdollars: int = 0
    first_byte_ms: int | None = None
    retry_count: int = 0
    upstream_request_id: str | None = None
    error_class: str | None = None
    error_detail: str | None = None
    status_code: int | None = None


class ReservationRow(BaseModel):
    id: int
    request_id: int
    account_id: int
    model_id: str
    reserved_microdollars: int
    created_at: datetime
    released_at: datetime | None = None
    status: str
    estimated_tokens: int = 0
    expires_at: datetime | None = None
    release_reason: str | None = None


class RequestAttemptRow(BaseModel):
    id: int
    request_id: int
    attempt_number: int
    account_id: int
    started_at: datetime
    completed_at: datetime | None = None
    status_code: int | None = None
    error_class: str | None = None
    upstream_request_id: str | None = None
    bytes_emitted: int = 0
    error_detail: str | None = None
