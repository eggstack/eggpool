"""Pydantic configuration models for optional model routers.

This module intentionally contains only structural validation.  Concrete
model availability is a catalog/runtime concern and must not become a config
parse or startup dependency.
"""

from __future__ import annotations

import re
import unicodedata
from typing import TYPE_CHECKING, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from eggpool.errors import ConfigError

if TYPE_CHECKING:
    from collections.abc import Mapping

VIRTUAL_MODEL_MAX_BYTES: Final = 128
ROUTE_LABEL_MAX_BYTES: Final = 128
CONCRETE_MODEL_MAX_BYTES: Final = 128
ROUTE_DESCRIPTION_MAX_BYTES: Final = 512
COMPILED_POLICY_MAX_BYTES: Final = 64 * 1024
SELECTOR_PROTOCOL_VERSION: Final = "model-router/v1"

_ASCII_WHITESPACE_RE = re.compile(r"[\t\n\f\r\v ]+")


def _utf8_size(value: str) -> int:
    return len(value.encode("utf-8"))


def _has_control_character(value: str, *, allow_ascii_whitespace: bool) -> bool:
    return any(
        unicodedata.category(char) == "Cc"
        and not (allow_ascii_whitespace and char in "\t\n\f\r\v ")
        for char in value
    )


def validate_virtual_model_id(
    value: str, *, field_name: str = "virtual model ID"
) -> str:
    """Validate and return a public virtual model ID without normalizing it."""
    if not value.strip():
        raise ValueError(f"{field_name} must not be empty")
    if _utf8_size(value) > VIRTUAL_MODEL_MAX_BYTES:
        raise ValueError(
            f"{field_name} must be at most {VIRTUAL_MODEL_MAX_BYTES} UTF-8 bytes"
        )
    if _has_control_character(value, allow_ascii_whitespace=False):
        raise ValueError(f"{field_name} must not contain control characters")
    if "/" in value:
        raise ValueError(f"{field_name} must not contain '/'")
    return value


def _validate_reference(value: str, field_name: str) -> str:
    if not value.strip():
        raise ValueError(f"{field_name} must not be empty")
    if _utf8_size(value) > CONCRETE_MODEL_MAX_BYTES:
        raise ValueError(
            f"{field_name} must be at most {CONCRETE_MODEL_MAX_BYTES} UTF-8 bytes"
        )
    if _has_control_character(value, allow_ascii_whitespace=False):
        raise ValueError(f"{field_name} must not contain control characters")
    return value


def _validate_route_label(value: str, field_name: str) -> str:
    if not value.strip():
        raise ValueError(f"{field_name} must not be empty")
    if _utf8_size(value) > ROUTE_LABEL_MAX_BYTES:
        raise ValueError(
            f"{field_name} must be at most {ROUTE_LABEL_MAX_BYTES} UTF-8 bytes"
        )
    if _has_control_character(value, allow_ascii_whitespace=False):
        raise ValueError(f"{field_name} must not contain control characters")
    return value


def _validate_description(value: str) -> str:
    if not value.strip():
        raise ValueError("route description must not be empty")
    if _utf8_size(value) > ROUTE_DESCRIPTION_MAX_BYTES:
        raise ValueError(
            "route description must be at most "
            f"{ROUTE_DESCRIPTION_MAX_BYTES} UTF-8 bytes"
        )
    if _has_control_character(value, allow_ascii_whitespace=True):
        raise ValueError("route description must not contain control characters")
    return value


def normalize_route_description(value: str) -> str:
    """Apply the sole deterministic normalization used by compilation."""
    return _ASCII_WHITESPACE_RE.sub(" ", value.strip())


class ModelRouteConfig(BaseModel):
    """One operator-labelled route to a concrete upstream model reference."""

    model_config = ConfigDict(extra="forbid")

    model: str
    description: str

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        return _validate_reference(value, "route model")

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str) -> str:
        return _validate_description(value)


class ModelRouterConfig(BaseModel):
    """Structural configuration for one virtual model router."""

    model_config = ConfigDict(extra="forbid")

    selector_model: str
    default_model: str
    routes: dict[str, ModelRouteConfig]
    sticky: bool = True
    affinity_ttl_s: float = Field(default=43_200.0, ge=1.0, le=604_800.0)
    selector_timeout_s: float = Field(default=2.0, ge=0.05, le=30.0)
    max_input_bytes: int = Field(default=2_048, ge=128, le=16_384)
    repair_attempts: Literal[0, 1] = 1

    @field_validator("selector_model")
    @classmethod
    def validate_selector_model(cls, value: str) -> str:
        return _validate_reference(value, "selector_model")

    @field_validator("default_model")
    @classmethod
    def validate_default_model(cls, value: str) -> str:
        return _validate_reference(value, "default_model")

    @model_validator(mode="after")
    def validate_routes(self) -> ModelRouterConfig:
        if not self.routes:
            raise ConfigError("model router must declare at least one route")
        for label in self.routes:
            _validate_route_label(label, "route label")
        if self.default_model not in {route.model for route in self.routes.values()}:
            raise ConfigError(
                "model router default_model must exactly match at least one route model"
            )
        return self


def validate_model_router_mapping(
    model_routers: Mapping[str, ModelRouterConfig],
) -> None:
    """Validate virtual IDs and reject virtual-to-virtual references globally."""
    virtual_ids = set(model_routers)
    for virtual_model, router in model_routers.items():
        validate_virtual_model_id(virtual_model)
        if router.selector_model in virtual_ids:
            raise ConfigError(
                f"model router {virtual_model!r} selector_model cannot target "
                f"virtual model {router.selector_model!r}"
            )
        for label, route in router.routes.items():
            if route.model in virtual_ids:
                raise ConfigError(
                    f"model router {virtual_model!r} route {label!r} cannot target "
                    f"virtual model {route.model!r}"
                )
