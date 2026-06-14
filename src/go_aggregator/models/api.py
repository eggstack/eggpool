"""Pydantic models for API responses."""

from __future__ import annotations

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str


class ReadyResponse(BaseModel):
    status: str
    reason: str | None = None


class ErrorResponse(BaseModel):
    error: str
    code: str | None = None
