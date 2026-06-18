"""Pydantic v2 models for TOML configuration."""

from __future__ import annotations

import os
import re
import tomllib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from go_aggregator.catalog.pricing import (
    parse_microdollars_per_million,
    parse_price_per_1k,
)
from go_aggregator.constants import (
    DEFAULT_DATABASE_PATH,
    DEFAULT_HOST,
    DEFAULT_PORT,
)
from go_aggregator.errors import ConfigError


class ServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = DEFAULT_HOST
    port: int = Field(default=DEFAULT_PORT, ge=0, le=65535)
    api_key_env: str = "GO_AGGREGATOR_API_KEY"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    access_log: bool = True


class UpstreamConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str = "https://opencode.ai/zen/go/v1"
    connect_timeout_s: float = Field(default=5, gt=0)
    read_timeout_s: float = Field(default=300, gt=0)
    write_timeout_s: float = Field(default=30, gt=0)
    max_connections: int = Field(default=100, gt=0)
    max_keepalive: int = Field(default=20, gt=0)
    keepalive_timeout_s: float = Field(default=30, ge=0)

    @model_validator(mode="after")
    def validate_keepalive(self) -> UpstreamConfig:
        if self.max_keepalive > self.max_connections:
            raise ConfigError(
                f"max_keepalive ({self.max_keepalive}) must not exceed "
                f"max_connections ({self.max_connections})"
            )
        return self


class DatabaseConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = DEFAULT_DATABASE_PATH
    busy_timeout_ms: int = Field(default=5000, gt=0)
    wal: bool = True
    synchronous: Literal["OFF", "NORMAL", "FULL", "EXTRA"] = "NORMAL"


class ModelsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refresh_interval_s: int = Field(default=3600, ge=0)
    expose_mode: Literal["union", "intersection", "healthy_union"] = "union"
    startup_refresh: bool = True
    stale_after_s: int = Field(default=7200, gt=0)
    allow_stale_catalog: bool = True


class RoutingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: Literal["quota_fair"] = "quota_fair"
    near_tie_epsilon: float = Field(default=0.1, ge=0)
    max_retries_before_stream: int = Field(
        default=3,
        ge=0,
        description="Retries after first attempt. Total attempts = value + 1.",
    )
    unknown_request_reservation_microdollars: int = Field(default=1_000_000, ge=0)
    inflight_penalty: int = Field(default=100_000, ge=0)
    health_penalty: int = Field(default=500_000, ge=0)
    randomize_near_ties: bool = True
    quota_exhausted_cooldown_seconds: float = Field(default=300.0, ge=0)


class LimitsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    five_hour_microdollars: int = Field(default=12_000_000, gt=0)
    weekly_microdollars: int = Field(default=30_000_000, gt=0)
    monthly_microdollars: int = Field(default=60_000_000, gt=0)


class DashboardConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    public: bool = False
    retain_request_stats_days: int = Field(default=30, gt=0)
    retain_event_days: int = Field(default=90, gt=0)
    store_request_content: bool = False
    refresh_interval_s: int = Field(default=60, gt=0)

    @field_validator("store_request_content")
    @classmethod
    def reject_storing_content(cls, value: bool) -> bool:
        if value:
            raise ValueError(
                "store_request_content must be false; "
                "request content must not be persisted"
            )
        return value


class SecurityConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed_hosts: list[str] = []
    cors_origins: list[str] = []
    redact_headers: list[str] = ["authorization", "x-api-key"]
    persist_redacted_error_detail: bool = False


class AccountConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    api_key_env: str
    enabled: bool = True
    weight: float = Field(default=1.0, gt=0)
    five_hour_offset_microdollars: int = 0
    weekly_offset_microdollars: int = 0
    monthly_offset_microdollars: int = 0


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    base_url: str
    protocols: list[str] = ["openai"]
    openai_path: str = "/chat/completions"
    anthropic_path: str = "/messages"
    models_method: str = "GET"
    models_path: str = "/models"
    connect_timeout_s: float = Field(default=5, gt=0)
    read_timeout_s: float = Field(default=300, gt=0)
    write_timeout_s: float = Field(default=30, gt=0)
    max_connections: int = Field(default=100, gt=0)
    max_keepalive: int = Field(default=20, gt=0)
    keepalive_timeout_s: float = Field(default=30, ge=0)
    accounts: list[AccountConfig] = []

    @model_validator(mode="after")
    def validate_keepalive(self) -> ProviderConfig:
        if self.max_keepalive > self.max_connections:
            raise ConfigError(
                f"max_keepalive ({self.max_keepalive}) must not exceed "
                f"max_connections ({self.max_connections})"
            )
        return self

    @field_validator("id")
    @classmethod
    def validate_provider_id(cls, value: str) -> str:
        if not re.match(r"^[a-zA-Z0-9]([a-zA-Z0-9\-]*[a-zA-Z0-9])?$", value):
            raise ConfigError(
                f"Provider ID {value!r} must be alphanumeric with optional hyphens"
            )
        return value


class ModelOverrideConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol: Literal["openai", "anthropic"] | None = None
    input_price_per_1k: float | None = None
    output_price_per_1k: float | None = None
    cache_read_per_million_microdollars: int | None = None
    cache_write_per_million_microdollars: int | None = None

    @field_validator("input_price_per_1k", "output_price_per_1k", mode="before")
    @classmethod
    def parse_legacy_price(cls, value: object) -> float | None:
        return parse_price_per_1k(value)

    @field_validator(
        "cache_read_per_million_microdollars",
        "cache_write_per_million_microdollars",
        mode="before",
    )
    @classmethod
    def parse_cache_price(cls, value: object) -> int | None:
        return parse_microdollars_per_million(value)


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    server: ServerConfig = ServerConfig()
    upstream: UpstreamConfig = UpstreamConfig()
    database: DatabaseConfig = DatabaseConfig()
    models: ModelsConfig = ModelsConfig()
    routing: RoutingConfig = RoutingConfig()
    limits: LimitsConfig = LimitsConfig()
    dashboard: DashboardConfig = DashboardConfig()
    security: SecurityConfig = SecurityConfig()
    accounts: list[AccountConfig] = []
    providers: dict[str, ProviderConfig] = {}
    model_overrides: dict[str, ModelOverrideConfig] = {}

    @model_validator(mode="after")
    def _normalize_providers(self) -> AppConfig:
        """Convert flat accounts to default provider if no providers defined."""
        if not self.providers and self.accounts:
            self.providers = {
                "opencode-go": ProviderConfig(
                    id="opencode-go",
                    base_url=self.upstream.base_url,
                    protocols=["openai", "anthropic"],
                    openai_path="/chat/completions",
                    anthropic_path="/messages",
                    models_method="GET",
                    models_path="/models",
                    accounts=self.accounts,
                )
            }
            self.accounts = []
        return self

    @model_validator(mode="after")
    def validate_accounts(self) -> AppConfig:
        names: list[str] = []
        for acct in self.all_accounts():
            if acct.name in names:
                raise ConfigError(f"Duplicate account name: {acct.name!r}")
            names.append(acct.name)
            if acct.weight <= 0:
                raise ConfigError(
                    f"Account {acct.name!r} has non-positive weight: {acct.weight}"
                )
        return self

    def all_accounts(self) -> list[AccountConfig]:
        """Return all accounts across all providers."""
        result: list[AccountConfig] = []
        for provider in self.providers.values():
            result.extend(provider.accounts)
        return result

    def validate_account_credentials(self) -> None:
        """Validate that enabled accounts have their API key env vars set.

        Called separately from structural validation so CLI commands that
        do not need upstream credentials (``migrate``, ``accounts status``,
        ``db vacuum``) can skip this check.
        """
        _placeholder_keys = frozenset(
            {
                "your-proxy-api-key",
                "your-opencode-go-key-1",
                "your-opencode-go-key-2",
                "your-api-key-here",
                "your-local-api-key-here",
            }
        )
        for acct in self.all_accounts():
            if acct.enabled:
                raw_key = os.environ.get(acct.api_key_env)
                if not raw_key:
                    raise ConfigError(
                        f"Account {acct.name!r} is enabled but env var "
                        f"{acct.api_key_env!r} is not set"
                    )
                if not raw_key.strip():
                    raise ConfigError(
                        f"Account {acct.name!r} has a whitespace-only API key "
                        f"in env var {acct.api_key_env!r}"
                    )
                if raw_key.strip().lower() in _placeholder_keys:
                    raise ConfigError(
                        f"Account {acct.name!r} has a placeholder API key "
                        f"in env var {acct.api_key_env!r}; "
                        f"set a real key before starting the service"
                    )

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> AppConfig:
        """Create config from a dictionary."""
        try:
            return cls.model_validate(data)
        except Exception as exc:
            raise ConfigError(f"Config validation failed: {exc}") from exc

    @classmethod
    def from_toml(cls, path: str) -> AppConfig:
        """Read and validate a TOML configuration file."""
        try:
            with open(path, "rb") as f:
                raw = tomllib.load(f)
        except FileNotFoundError as exc:
            raise ConfigError(f"Config file not found: {path}") from exc
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"Invalid TOML in {path}: {exc}") from exc

        try:
            return cls.model_validate(raw)
        except Exception as exc:
            raise ConfigError(f"Config validation failed: {exc}") from exc
