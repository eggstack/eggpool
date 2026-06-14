"""Pydantic v2 models for TOML configuration."""

from __future__ import annotations

import os
import tomllib
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from go_aggregator.constants import (
    DEFAULT_DATABASE_PATH,
    DEFAULT_HOST,
    DEFAULT_PORT,
)
from go_aggregator.errors import ConfigError


class ServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    api_key_env: str = "GO_AGGREGATOR_API_KEY"
    log_level: str = "INFO"
    access_log: bool = True


class UpstreamConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str = "https://opencode.ai/zen/go/v1"
    connect_timeout_s: float = 5
    read_timeout_s: float = 300
    write_timeout_s: float = 30
    max_connections: int = 100
    max_keepalive: int = 20
    keepalive_timeout_s: float = 30


class DatabaseConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = DEFAULT_DATABASE_PATH
    busy_timeout_ms: int = 5000
    wal: bool = True
    synchronous: str = "NORMAL"


class ModelsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refresh_interval_s: int = 3600
    expose_mode: Literal["union", "intersection", "healthy_union"] = "union"
    startup_refresh: bool = True
    stale_after_s: int = 7200
    allow_stale_catalog: bool = True


class RoutingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: Literal["quota_fair"] = "quota_fair"
    near_tie_epsilon: float = 0.1
    max_retries_before_stream: int = 3
    unknown_request_reservation_microdollars: int = 1_000_000
    inflight_penalty: int = 100_000
    health_penalty: int = 500_000
    randomize_near_ties: bool = True


class LimitsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    five_hour_microdollars: int = 12_000_000
    weekly_microdollars: int = 30_000_000
    monthly_microdollars: int = 60_000_000


class DashboardConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    public: bool = False
    retain_request_stats_days: int = 30
    store_request_content: bool = False
    refresh_interval_s: int = 60


class SecurityConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed_hosts: list[str] = ["localhost", "127.0.0.1"]
    cors_origins: list[str] = []
    trust_proxy_headers: bool = False
    redact_headers: list[str] = ["authorization", "x-api-key"]


class AccountConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    api_key_env: str
    enabled: bool = True
    weight: float = 1.0
    five_hour_offset_microdollars: int = 0
    weekly_offset_microdollars: int = 0
    monthly_offset_microdollars: int = 0


class ModelOverrideConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol: Literal["openai", "anthropic"] | None = None
    max_tokens: int | None = None


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
    model_overrides: dict[str, ModelOverrideConfig] = {}

    @model_validator(mode="after")
    def validate_accounts(self) -> AppConfig:
        names: list[str] = []
        for acct in self.accounts:
            if acct.name in names:
                raise ConfigError(f"Duplicate account name: {acct.name!r}")
            names.append(acct.name)
            if acct.enabled and not os.environ.get(acct.api_key_env):
                raise ConfigError(
                    f"Account {acct.name!r} is enabled but env var "
                    f"{acct.api_key_env!r} is not set"
                )
            if acct.weight <= 0:
                raise ConfigError(
                    f"Account {acct.name!r} has non-positive weight: {acct.weight}"
                )
        return self

    @classmethod
    def from_dict(cls, data: dict) -> AppConfig:
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
        except FileNotFoundError:
            raise ConfigError(f"Config file not found: {path}") from None
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"Invalid TOML in {path}: {exc}") from exc

        try:
            return cls.model_validate(raw)
        except Exception as exc:
            raise ConfigError(f"Config validation failed: {exc}") from exc
