"""Pydantic models for API responses."""

from __future__ import annotations

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str


class ReadyResponse(BaseModel):
    status: str
    reason: str | None = None


class ErrorResponse(BaseModel):
    error: str
    code: str | None = None


class ModelObject(BaseModel):
    id: str
    object: str = "model"
    created: int = 0
    owned_by: str = "opencode"
    name: str | None = None


class ModelListResponse(BaseModel):
    object: str = "list"
    data: list[ModelObject] = Field(default_factory=list[ModelObject])
