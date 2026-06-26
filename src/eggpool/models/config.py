"""Pydantic v2 models for TOML configuration."""

from __future__ import annotations

import os
import re
import tomllib
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from eggpool.catalog.pricing import (
    parse_microdollars_per_million,
    parse_price_per_1k,
)
from eggpool.catalog.protocols import ProtocolName  # noqa: TCH001 — used by Pydantic
from eggpool.constants import (
    DEFAULT_DATABASE_PATH,
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_PROVIDER_ID,
)
from eggpool.errors import ConfigError
from eggpool.providers.auth import has_auth_scheme_prefix

_HTTP_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_PROXY_MANAGED_HEADERS = frozenset(
    {
        "connection",
        "content-length",
        "host",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)


def _validate_upstream_header_name(value: str) -> str:
    """Reject malformed or proxy-managed upstream header names."""
    if _HTTP_HEADER_NAME_RE.fullmatch(value) is None:
        raise ValueError(f"Invalid HTTP header name {value!r}")
    if value.casefold() in _PROXY_MANAGED_HEADERS:
        raise ValueError(f"HTTP header {value!r} is managed by the proxy")
    return value


def _validate_upstream_header_value(value: str) -> str:
    """Reject control characters that cannot be represented safely on wire."""
    if any(char in value for char in ("\r", "\n", "\x00")):
        raise ValueError("HTTP header values must not contain CR, LF, or NUL")
    return value


class ServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = DEFAULT_HOST
    port: int = Field(default=DEFAULT_PORT, ge=0, le=65535)
    api_key: str | None = None
    api_key_env: str = "SERVER_API_KEY"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    access_log: bool = True
    # Number of Granian runtime (event-loop) threads. Granian already
    # defaults to 1 — keep the field explicit so single-board / SBC
    # deployments have a documented, configurable knob for low-memory
    # tuning. Increase on more capable hardware for higher request
    # parallelism; Granian still keeps ``workers=1`` so the process
    # count remains small.
    threads: int = Field(default=1, ge=1, le=64)

    @property
    def resolved_api_key(self) -> str | None:
        """Return the API key, checking inline first then env var."""
        if self.api_key:
            return self.api_key
        return os.environ.get(self.api_key_env)


class UpstreamConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str = "https://opencode.ai/zen/go/v1"
    connect_timeout_s: float = Field(default=5, gt=0)
    read_timeout_s: float = Field(default=300, gt=0)
    write_timeout_s: float = Field(default=30, gt=0)
    pool_timeout_s: float = Field(default=30, gt=0)
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
    # aiosqlite uses one Python worker thread per connection. Keep the
    # default at one for small-device deployments; setting this to 2
    # opens a separate read-only stats connection so dashboard analytics
    # do not share the data-plane connection lock.
    worker_threads: int = Field(default=1, ge=1, le=2)


class ModelsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refresh_interval_s: int = Field(default=300, ge=0)
    expose_mode: Literal["union", "intersection", "healthy_union"] = "union"
    startup_refresh: bool = True
    stale_after_s: int = Field(default=7200, gt=0)
    allow_stale_catalog: bool = True
    ping_retain_days: int = Field(default=7, ge=1)
    collapse_models: bool = False


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
    # Local quota mode controls whether locally estimated over-capacity
    # usage hard-excludes accounts from routing or only affects rank.
    # "score_only" (default) is safe for subscription aggregation:
    # upstream 429/402/5xx remain the authoritative suppression signal.
    # "hard_cap" is an opt-in escape hatch that re-enables local quota
    # as a hard eligibility gate (legacy behavior).
    local_quota_mode: Literal["score_only", "hard_cap"] = "score_only"


class PricingCatalogEntry(BaseModel):
    """One external pricing catalog entry.

    External catalogs (OpenRouter, OpenCode Zen, ...) supply authoritative
    upstream pricing for upstream model IDs that do not surface pricing
    metadata via the OpenAI / Anthropic ``/v1/models`` endpoint.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    priority: int = Field(default=100, ge=0)
    ttl_seconds: int = Field(default=86_400, gt=0)
    base_url: str | None = None
    api_key: str | None = None
    options: dict[str, object] = Field(default_factory=dict[str, object])


class PricingCatalogsConfig(BaseModel):
    """Map of external pricing catalogs keyed by canonical name.

    The known catalog names are ``"openrouter"`` and ``"opencode_zen"``;
    operators may add additional catalog names but the resolver pipeline
    only ships implementations for the two built-ins.
    """

    model_config = ConfigDict(extra="forbid")

    openrouter: PricingCatalogEntry = Field(default_factory=PricingCatalogEntry)
    opencode_zen: PricingCatalogEntry = Field(default_factory=PricingCatalogEntry)
    aliases: list[dict[str, object]] = Field(default_factory=list[dict[str, object]])


class PricingConfig(BaseModel):
    """Pricing resolution configuration.

    ``catalogs`` configures external pricing catalogs that supplement
    the upstream metadata path. ``fallback`` controls how missing cache
    rates are filled (see CostCalculator for the category-specific
    constants used when ``fallback`` is ``"generic_estimate"``).
    """

    model_config = ConfigDict(extra="forbid")

    catalogs: PricingCatalogsConfig = Field(default_factory=PricingCatalogsConfig)
    fallback: Literal["generic_estimate", "off"] = "generic_estimate"


class LimitsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    five_hour_microdollars: int = Field(default=12_000_000, gt=0)
    weekly_microdollars: int = Field(default=30_000_000, gt=0)
    monthly_microdollars: int = Field(default=60_000_000, gt=0)


class MetricsConfig(BaseModel):
    """Controls observability write buffering for reduced microSD wear.

    ``write_mode`` selects the buffering strategy:
    - ``immediate``: existing direct-write behavior (best for debugging).
    - ``balanced``: buffer lossy analytics with short flush intervals.
    - ``low_wear``: longer flush interval, coarser buckets, optional
      trace sampling — designed for microSD / SBC deployments.

    Buffered analytics may lose at most ``flush_interval_s`` seconds of
    data after abrupt power loss. Correctness-critical request state
    (request rows, reservations, attempts, routing) is never buffered.
    """

    model_config = ConfigDict(extra="forbid")

    write_mode: Literal["immediate", "balanced", "low_wear"] = "balanced"
    flush_interval_s: int = Field(default=30, ge=1, le=600)
    max_buffered_events: int = Field(default=500, ge=1, le=100_000)
    timeseries_bucket_s: int = Field(default=60, ge=10, le=3600)
    trace_sample_rate: float = Field(default=1.0, ge=0.0, le=1.0)
    aggregate_only: bool = False
    rollup_retain_days: int = Field(default=90, gt=0)
    cleanup_interval_s: int = Field(default=86_400, gt=0)
    cleanup_max_rows_per_pass: int = Field(default=5000, gt=0)


class DashboardConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    public: bool = True
    theme: str = "Cyber Red"
    themes_dir: str | None = None
    retain_request_stats_days: int = Field(default=30, gt=0)
    retain_event_days: int = Field(default=90, gt=0)
    store_request_content: bool = False
    refresh_interval_s: int = Field(default=60, gt=0)

    @field_validator("store_request_content", mode="before")
    @classmethod
    def reject_storing_content(cls, value: object) -> object:
        if value:
            raise ValueError(
                "store_request_content must be false; "
                "request content must not be persisted"
            )
        return value


class SecurityConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed_hosts: list[str] = Field(default_factory=list)
    cors_origins: list[str] = Field(default_factory=list)
    redact_headers: list[str] = Field(
        default_factory=lambda: ["authorization", "x-api-key"]
    )
    persist_redacted_error_detail: bool = False


class ProxyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str | None = None
    url_env: str | None = None

    @model_validator(mode="after")
    def validate_source(self) -> ProxyConfig:
        if bool(self.url) == bool(self.url_env):
            raise ConfigError("Proxy config must set exactly one of url or url_env")
        return self


class AccountConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    api_key: str | None = None
    api_key_env: str = ""
    enabled: bool = True
    weight: float = Field(default=1.0, gt=0)
    five_hour_offset_microdollars: int = 0
    weekly_offset_microdollars: int = 0
    monthly_offset_microdollars: int = 0
    proxy: str | None = None
    proxy_url: str | None = None
    proxy_url_env: str | None = None

    @model_validator(mode="after")
    def validate_proxy_source(self) -> AccountConfig:
        configured = [
            value
            for value in (self.proxy, self.proxy_url, self.proxy_url_env)
            if value is not None
        ]
        if len(configured) > 1:
            raise ConfigError(
                f"Account {self.name!r} must set at most one of proxy, "
                "proxy_url, or proxy_url_env"
            )
        return self


class ProviderAuthConfig(BaseModel):
    """Provider-specific authentication configuration."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["bearer", "api_key", "raw_authorization", "none"] = "bearer"
    header: str = "Authorization"
    scheme: str = "Bearer"

    @field_validator("header")
    @classmethod
    def validate_header(cls, value: str) -> str:
        return _validate_upstream_header_name(value)

    @field_validator("scheme")
    @classmethod
    def validate_scheme(cls, value: str) -> str:
        if not value or any(char.isspace() for char in value):
            raise ValueError("Authentication scheme must be a non-empty token")
        return _validate_upstream_header_value(value)


class ProviderStaticHeaderConfig(BaseModel):
    """A static header to include in upstream requests."""

    model_config = ConfigDict(extra="forbid")

    name: str
    value: str | None = None
    value_env: str | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _validate_upstream_header_name(value)

    @field_validator("value")
    @classmethod
    def validate_value(cls, value: str | None) -> str | None:
        return None if value is None else _validate_upstream_header_value(value)

    @model_validator(mode="after")
    def validate_value_source(self) -> ProviderStaticHeaderConfig:
        if self.value is not None and self.value_env is not None:
            raise ConfigError(
                f"Static header {self.name!r} must set exactly one of "
                "value or value_env"
            )
        return self


class ProviderModelsEndpointConfig(BaseModel):
    """Provider-specific model listing endpoint configuration."""

    model_config = ConfigDict(extra="forbid")

    method: Literal["GET", "POST", "DISABLED"] = "GET"
    path: str = "/models"
    body: dict[str, Any] | None = None
    query: dict[str, str] = Field(default_factory=dict)
    required: bool = True


class ProviderStaticModelConfig(BaseModel):
    """Operator-supplied static model entry for a provider.

    Providers whose upstream does not expose a usable ``/models`` listing
    can declare model seeds in config so the catalog still has rows to
    route against. Static rows participate in the same protocol, limit,
    and exposure machinery as live-discovered entries, and live
    refreshes may augment but must not erase explicit static fields.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    display_name: str | None = None
    protocol: ProtocolName | None = None
    max_context_tokens: int | None = Field(default=None, gt=0)
    max_input_tokens: int | None = Field(default=None, gt=0)
    max_output_tokens: int | None = Field(default=None, gt=0)
    supports_tools: bool | None = None
    supports_vision: bool | None = None
    source_metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_limit_relationships(self) -> ProviderStaticModelConfig:
        """Ensure field-specific limits do not exceed the context limit."""
        context = self.max_context_tokens
        if context is None:
            return self
        for field_name in ("max_input_tokens", "max_output_tokens"):
            value = getattr(self, field_name)
            if value is not None and value > context:
                raise ConfigError(
                    f"providers.{self.id!r}: {field_name} ({value}) exceeds "
                    f"max_context_tokens ({context})"
                )
        return self


class ProviderVerifyConfig(BaseModel):
    """Configuration for live verification of provider endpoints."""

    model_config = ConfigDict(extra="forbid")

    probe_model: str | None = None
    probe_protocol: Literal["openai", "anthropic"] = "openai"
    require_models: bool = True


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    base_url: str
    protocols: list[ProtocolName] = Field(
        default_factory=lambda: ["openai"],
        min_length=1,
    )
    openai_path: str = "/chat/completions"
    anthropic_path: str = "/messages"
    models_method: Literal["GET", "POST"] = "GET"
    models_path: str = "/models"
    connect_timeout_s: float = Field(default=5, gt=0)
    read_timeout_s: float = Field(default=300, gt=0)
    write_timeout_s: float = Field(default=30, gt=0)
    pool_timeout_s: float = Field(default=30, gt=0)
    max_connections: int = Field(default=100, gt=0)
    max_keepalive: int = Field(default=20, gt=0)
    keepalive_timeout_s: float = Field(default=30, ge=0)
    routing_priority: int = Field(default=0, ge=0)
    accounts: list[AccountConfig] = Field(default_factory=list[AccountConfig])
    model_overrides: dict[str, ModelOverrideConfig] = Field(default_factory=dict)
    auth: ProviderAuthConfig = Field(default_factory=ProviderAuthConfig)
    headers: list[ProviderStaticHeaderConfig] = Field(
        default_factory=list[ProviderStaticHeaderConfig]
    )
    models_endpoint: ProviderModelsEndpointConfig | None = None
    static_models: list[ProviderStaticModelConfig] = Field(
        default_factory=list[ProviderStaticModelConfig]
    )
    verify: ProviderVerifyConfig = Field(default_factory=ProviderVerifyConfig)

    @field_validator("models_method", mode="before")
    @classmethod
    def normalize_models_method(cls, value: object) -> object:
        """Normalize supported HTTP methods before strict validation."""
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        """Require an absolute credential-free HTTP(S) provider URL."""
        if value != value.strip() or any(char.isspace() for char in value):
            raise ConfigError("Provider base_url must not contain whitespace")
        try:
            parsed = urlsplit(value)
            _port = parsed.port
        except ValueError as exc:
            raise ConfigError(f"Invalid provider base_url {value!r}: {exc}") from exc
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ConfigError(
                f"Provider base_url {value!r} must be an absolute HTTP(S) URL"
            )
        if parsed.username is not None or parsed.password is not None:
            raise ConfigError("Provider base_url must not contain credentials")
        if parsed.query or parsed.fragment:
            raise ConfigError("Provider base_url must not contain a query or fragment")
        return value

    @model_validator(mode="after")
    def validate_keepalive(self) -> ProviderConfig:
        if self.max_keepalive > self.max_connections:
            raise ConfigError(
                f"max_keepalive ({self.max_keepalive}) must not exceed "
                f"max_connections ({self.max_connections})"
            )
        return self

    @model_validator(mode="after")
    def validate_static_headers(self) -> ProviderConfig:
        """Keep static headers from replacing credentials or each other."""
        seen: set[str] = set()
        auth_header = self.auth.header.casefold()
        for header in self.headers:
            name = header.name.casefold()
            if name in seen:
                raise ConfigError(
                    f"Provider {self.id!r} has duplicate static header {header.name!r}"
                )
            if name == auth_header:
                raise ConfigError(
                    f"Provider {self.id!r} static header {header.name!r} "
                    "conflicts with the configured authentication header"
                )
            seen.add(name)
        return self

    @model_validator(mode="after")
    def _synthesize_models_endpoint(self) -> ProviderConfig:
        """Synthesize models_endpoint from legacy fields when not set."""
        if self.models_endpoint is None and self.models_path:
            method: Literal["GET", "POST", "DISABLED"] = self.models_method  # type: ignore[assignment]
            self.models_endpoint = ProviderModelsEndpointConfig(
                method=method,
                path=self.models_path,
            )
        return self

    @model_validator(mode="after")
    def validate_static_models(self) -> ProviderConfig:
        """Reject duplicate static model IDs within a provider."""
        seen: set[str] = set()
        for static in self.static_models:
            if static.id in seen:
                raise ConfigError(
                    f"Provider {self.id!r} declares duplicate static model "
                    f"id {static.id!r}"
                )
            seen.add(static.id)
        return self

    @model_validator(mode="after")
    def _validate_no_duplicate_version(self) -> ProviderConfig:
        """Reject base_url + path combinations that duplicate /v1 prefixes."""
        base = self.base_url.rstrip("/")
        versioned_suffixes = ("/v1", "/api/v1", "/compatible-mode/v1")
        for suffix in versioned_suffixes:
            if base.endswith(suffix):
                paths_to_check = [self.openai_path, self.anthropic_path]
                if self.models_endpoint is not None:
                    paths_to_check.append(self.models_endpoint.path)
                elif self.models_path:
                    paths_to_check.append(self.models_path)
                for p in paths_to_check:
                    if p and p.startswith(suffix + "/"):
                        raise ConfigError(
                            f"Provider {self.id!r}: base_url ends with {suffix!r} "
                            f"but path {p!r} also starts with {suffix}/ — "
                            f"this creates a duplicate version prefix"
                        )
        return self

    @field_validator("id")
    @classmethod
    def validate_provider_id(cls, value: str) -> str:
        if re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?", value) is None:
            raise ConfigError(
                f"Provider ID {value!r} must be alphanumeric with optional hyphens"
            )
        return value


class ModelLimitOverrideConfig(BaseModel):
    """Reusable model limit override fields for context/input/output ceilings."""

    model_config = ConfigDict(extra="forbid")

    max_context_tokens: int | None = Field(default=None, gt=0)
    max_input_tokens: int | None = Field(default=None, gt=0)
    max_output_tokens: int | None = Field(default=None, gt=0)
    enforce_context_limit: bool = True

    @model_validator(mode="after")
    def validate_limit_relationships(self) -> ModelLimitOverrideConfig:
        """Ensure field-specific limits do not exceed the context limit."""
        context = self.max_context_tokens
        if context is None:
            return self
        for field_name in ("max_input_tokens", "max_output_tokens"):
            value = getattr(self, field_name)
            if value is not None and value > context:
                raise ConfigError(
                    f"{field_name} ({value}) exceeds max_context_tokens ({context})"
                )
        return self


class ModelOverrideConfig(ModelLimitOverrideConfig):
    model_config = ConfigDict(extra="forbid")

    protocol: ProtocolName | None = None
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

    server: ServerConfig = Field(default_factory=ServerConfig)
    upstream: UpstreamConfig = Field(default_factory=UpstreamConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    pricing: PricingConfig = Field(default_factory=PricingConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    metrics: MetricsConfig = Field(default_factory=MetricsConfig)
    proxies: dict[str, ProxyConfig] = Field(default_factory=dict)
    accounts: list[AccountConfig] = Field(default_factory=list[AccountConfig])
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    model_overrides: dict[str, ModelOverrideConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _normalize_providers(self) -> AppConfig:
        """Convert flat accounts to default provider if no providers defined."""
        if not self.providers and self.accounts:
            self.providers = {
                DEFAULT_PROVIDER_ID: ProviderConfig(
                    id=DEFAULT_PROVIDER_ID,
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
    def validate_provider_ids(self) -> AppConfig:
        """Ensure mapping keys and declared provider IDs cannot diverge."""
        for provider_id, provider in self.providers.items():
            if provider.id != provider_id:
                raise ConfigError(
                    f"Provider key {provider_id!r} does not match its "
                    f"declared id {provider.id!r}"
                )
        return self

    @model_validator(mode="after")
    def validate_accounts(self) -> AppConfig:
        names: set[str] = set()
        for provider in self.providers.values():
            for acct in provider.accounts:
                if acct.name in names:
                    raise ConfigError(f"Duplicate account name: {acct.name!r}")
                names.add(acct.name)
                if provider.auth.mode != "none" and not (
                    acct.api_key or acct.api_key_env
                ):
                    raise ConfigError(
                        f"Account {acct.name!r} must set api_key or api_key_env"
                    )
                if acct.weight <= 0:
                    raise ConfigError(
                        f"Account {acct.name!r} has non-positive weight: {acct.weight}"
                    )
                if acct.proxy is not None and acct.proxy not in self.proxies:
                    raise ConfigError(
                        f"Account {acct.name!r} references unknown proxy {acct.proxy!r}"
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
        from eggpool.constants import PLACEHOLDER_API_KEYS

        for provider_id, provider in self.providers.items():
            for acct in provider.accounts:
                if not acct.enabled or provider.auth.mode == "none":
                    continue
                raw_key = acct.api_key or os.environ.get(acct.api_key_env)
                if not raw_key:
                    source = (
                        "api_key" if acct.api_key else f"env var {acct.api_key_env!r}"
                    )
                    raise ConfigError(
                        f"Provider {provider_id!r} account {acct.name!r}: "
                        f"{source} is not set"
                    )
                if any(char in raw_key for char in ("\r", "\n", "\x00")):
                    source = (
                        "api_key" if acct.api_key else f"env var {acct.api_key_env!r}"
                    )
                    raise ConfigError(
                        f"Provider {provider_id!r} account {acct.name!r}: "
                        f"{source} contains CR, LF, or NUL"
                    )
                if provider.auth.mode == "bearer" and has_auth_scheme_prefix(
                    raw_key, provider.auth.scheme
                ):
                    source = (
                        "api_key" if acct.api_key else f"env var {acct.api_key_env!r}"
                    )
                    raise ConfigError(
                        f"Provider {provider_id!r} account {acct.name!r}: "
                        f"{source} must be the raw token, not "
                        f"'{provider.auth.scheme} <token>'. EggPool adds the "
                        f"{provider.auth.scheme} scheme automatically."
                    )

                if not raw_key.strip():
                    source = (
                        "api_key" if acct.api_key else f"env var {acct.api_key_env!r}"
                    )
                    raise ConfigError(
                        f"Account {acct.name!r} has a whitespace-only API key "
                        f"in {source}"
                    )
                if raw_key.strip().lower() in PLACEHOLDER_API_KEYS:
                    source = (
                        "api_key" if acct.api_key else f"env var {acct.api_key_env!r}"
                    )
                    raise ConfigError(
                        f"Account {acct.name!r} has a placeholder API key "
                        f"in {source}; "
                        f"set a real key before starting the service"
                    )

    def resolve_account_proxy_url(self, account: AccountConfig) -> str | None:
        """Resolve the outbound proxy URL for an account, if configured."""
        if account.proxy_url is not None:
            return account.proxy_url
        if account.proxy_url_env is not None:
            return self._resolve_proxy_url_env(account.proxy_url_env, account.name)
        if account.proxy is None:
            return None

        proxy = self.proxies[account.proxy]
        if proxy.url is not None:
            return proxy.url
        assert proxy.url_env is not None
        return self._resolve_proxy_url_env(proxy.url_env, account.name)

    @staticmethod
    def _resolve_proxy_url_env(env_name: str, account_name: str) -> str:
        value = os.environ.get(env_name)
        if not value:
            raise ConfigError(
                f"Account {account_name!r} references proxy env var "
                f"{env_name!r}, but it is not set"
            )
        if not value.strip():
            raise ConfigError(
                f"Account {account_name!r} references proxy env var "
                f"{env_name!r}, but it is whitespace-only"
            )
        return value.strip()

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
