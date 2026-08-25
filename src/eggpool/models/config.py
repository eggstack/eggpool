"""Pydantic v2 models for TOML configuration."""

from __future__ import annotations

import os
import re
import tomllib
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from eggpool.catalog.capabilities import (  # noqa: TCH001
    CapabilitySource,
    CapabilityStatus,
    TranscodingCapabilities,
)
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
from eggpool.transcoder.policy import TranscoderPolicy

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
    # ``0`` remains available to direct in-memory application/test helpers;
    # file-backed production configuration rejects it in ``from_toml``.
    port: int = Field(default=DEFAULT_PORT, ge=0, le=65535)
    api_key: str | None = None
    api_key_env: str = "SERVER_API_KEY"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    access_log: bool = False
    # Maps to Granian ``runtime_threads``: the number of Rust I/O threads per
    # worker performing network work. Python coroutines always execute on the
    # single asyncio event loop Granian creates per worker process regardless
    # of this value (granian marshals every request onto that loop via
    # ``loop.call_soon_threadsafe``), so values > 1 are safe for loop-bound
    # asyncio primitives. Keep the default 1 on SBC profiles.
    threads: int = Field(default=1, ge=1, le=64)
    max_request_body_bytes: int = Field(default=10 * 1024 * 1024, gt=0)

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
    max_connections: int = Field(default=16, gt=0)
    max_keepalive: int = Field(default=4, gt=0)
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
    # aiosqlite uses one Python worker thread per connection. The lean default
    # uses one connection; set to 2 to open a separate read-only stats
    # connection when dashboard reads should avoid the data-plane lock.
    worker_threads: int = Field(default=1, ge=1, le=2)
    # Bound WAL file size after checkpoints.  ``None`` (default) retains the
    # SQLite default (unbounded).  A sensible SBC value is 67108864 (64 MiB).
    journal_size_limit: int | None = Field(default=None, gt=0)


class ReadinessProbeConfig(BaseModel):
    """Configuration for the process-owned database writable probe.

    The probe removes SQLite write activity from the ``/readyz`` path
    by executing a real write transaction on a bounded cadence and
    caching the result.  ``/readyz`` reads the cached snapshot without
    any write.

    The probe is process-owned and survives generation swaps (rehash).
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        default=False,
        description="Enable the background database writable probe.",
    )
    interval_s: float = Field(
        default=10.0,
        gt=0.0,
        le=60.0,
        description="Seconds between successive writable probes.",
    )
    freshness_s: float = Field(
        default=30.0,
        gt=0.0,
        le=300.0,
        description=(
            "Maximum age (seconds) of a successful probe result before "
            "readiness reports stale.  Must be greater than interval_s."
        ),
    )
    timeout_s: float = Field(
        default=5.0,
        gt=0.0,
        le=30.0,
        description="Per-probe write timeout in seconds.",
    )
    initial_probe: bool = Field(
        default=True,
        description="Perform an immediate probe at startup before accepting readiness.",
    )

    @model_validator(mode="after")
    def _validate_freshness(self) -> ReadinessProbeConfig:
        if self.freshness_s <= self.interval_s:
            raise ConfigError(
                f"readiness_probe.freshness_s ({self.freshness_s}) must be "
                f"greater than readiness_probe.interval_s ({self.interval_s})"
            )
        return self


class ModelsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refresh_interval_s: int = Field(default=300, ge=0)
    expose_mode: Literal["union", "intersection", "healthy_union"] = "union"
    startup_refresh: bool = True
    stale_after_s: int = Field(default=7200, gt=0)
    allow_stale_catalog: bool = True
    ping_retain_days: int = Field(default=7, ge=1)
    collapse_models: bool = False
    catalog_withdrawal_policy: Literal[
        "preserve_until_health",
        "confirmed_once",
        "confirmed_twice",
    ] = "preserve_until_health"


class RoutingTraceConfig(BaseModel):
    """Controls routing decision trace write pressure.

    Routing traces are diagnostic rows written by a background writer.
    Reducing write volume here has no effect on billing, retry, or
    crash-recovery semantics.
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal["all", "sampled", "off"] = Field(
        default="off",
        description=(
            '"sampled" = deterministic request-id sampling '
            "(opt-in, low write pressure). "
            '"all" = every attempt (full diagnostics). '
            '"off" = no routing trace rows.'
        ),
    )
    sample_rate: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description=("Fraction of selection-time traces to persist in sampled mode."),
    )
    include_score_components: bool = Field(
        default=False,
        description="Include score_components_json in persisted traces (larger rows).",
    )
    skip_above_lock_wait_p95_ms: float = Field(
        default=200.0,
        ge=0.0,
        description=(
            "When the SQLite lock-wait p95 (rolling) exceeds this value, "
            "routing trace events are dropped before enqueueing to avoid "
            "amplifying contention. Traces are diagnostic; their absence "
            "must never fail dispatch. Set to 0 to disable the guardrail."
        ),
    )
    queue_capacity: int = Field(
        default=1000,
        ge=100,
        le=100_000,
        description=(
            "Bounded queue capacity for the async trace writer. "
            "Newest events are dropped when the queue is full."
        ),
    )
    flush_interval_s: float = Field(
        default=1.0,
        ge=0.1,
        le=60.0,
        description="Maximum seconds between background trace flushes.",
    )
    max_batch_size: int = Field(
        default=50,
        ge=1,
        le=500,
        description="Maximum events per database flush batch.",
    )
    shutdown_flush_timeout_s: float = Field(
        default=5.0,
        ge=1.0,
        le=30.0,
        description="Seconds to flush remaining traces on shutdown.",
    )
    guard_queue_occupancy_threshold: float = Field(
        default=0.8,
        ge=0.0,
        le=1.0,
        description=(
            "When the writer queue occupancy (depth/capacity) exceeds "
            "this fraction, the guard skips trace submission to avoid "
            "amplifying backpressure.  Set to 1.0 to disable."
        ),
    )
    guard_oldest_event_age_s: float = Field(
        default=30.0,
        ge=0.0,
        le=600.0,
        description=(
            "When the oldest queued event is older than this, the "
            "guard skips submission (the drain is falling behind)."
        ),
    )
    guard_cooldown_s: float = Field(
        default=5.0,
        ge=0.0,
        le=60.0,
        description=(
            "After the guard triggers a skip, it continues skipping "
            "for this many seconds to avoid oscillation."
        ),
    )


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
    quota_exhausted_cooldown_seconds: float = Field(
        default=300.0,
        ge=0,
        le=1800.0,
        description="Maximum transient quota cooldown in seconds (at most 30 minutes).",
    )
    # Local quota mode controls whether locally estimated over-capacity
    # usage hard-excludes accounts from routing or only affects rank.
    # "score_only" (default) is safe for subscription aggregation:
    # upstream 429/402/5xx remain the authoritative suppression signal.
    # "hard_cap" is an opt-in escape hatch that re-enables local quota
    # as a hard eligibility gate (legacy behavior).
    local_quota_mode: Literal["score_only", "hard_cap"] = "score_only"
    fairness_mode: Literal["off", "round_robin", "random"] = "round_robin"
    fairness_epsilon: float | None = None
    fairness_scope: Literal[
        "provider_model_protocol",
        "provider_model",
        "priority_model_protocol",
    ] = "provider_model_protocol"
    trace: RoutingTraceConfig = Field(default_factory=RoutingTraceConfig)


class PricingCatalogEntry(BaseModel):
    """One external pricing catalog entry.

    External catalogs (OpenRouter, OpenCode Zen, ...) supply authoritative
    upstream pricing for upstream model IDs that do not surface pricing
    metadata via the OpenAI / Anthropic ``/v1/models`` endpoint.
    """

    model_config = ConfigDict(extra="forbid")

    # External pricing is analytics enrichment, not request correctness.
    # Keep it dormant until an operator explicitly opts in.
    enabled: bool = False
    priority: int = Field(default=100, ge=0)
    ttl_seconds: int = Field(default=86_400, gt=0)
    max_entries: int = Field(default=4096, gt=0)
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


class DispatchSpansConfig(BaseModel):
    """Configuration for fine-grained dispatch-span instrumentation.

    Plan 029, Workstream H: request-coherent sampling replaces
    per-span counter-based sampling.  ``sample_rate`` is a
    deterministic, request-level decision (stable per request ID)
    so that one sampled request records all relevant spans,
    preserving a coherent trace.

    Coarse dispatch overhead metrics remain always-on and bounded. The
    more detailed local pre-upstream and span windows are only constructed
    when detailed sampling is enabled.
    """

    model_config = ConfigDict(extra="forbid")

    sample_rate: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description=(
            "Fraction of requests for which detailed dispatch spans are "
            "recorded (0.0-1.0). 0.0 = coarse dispatch only; 1.0 = full "
            "detail. Deterministic by request ID so one sampled request "
            "records all spans (coherent trace)."
        ),
    )
    window_size: int = Field(
        default=200,
        ge=1,
        le=10_000,
        description="Rolling-window sample count retained per span.",
    )


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

    write_mode: Literal["immediate", "balanced", "low_wear"] = "low_wear"
    flush_interval_s: int = Field(default=120, ge=1, le=600)
    max_buffered_events: int = Field(default=250, ge=1, le=100_000)
    timeseries_bucket_s: int = Field(default=300, ge=10, le=3600)
    trace_sample_rate: float = Field(default=0.05, ge=0.0, le=1.0)
    aggregate_only: bool = True
    rollup_retain_days: int = Field(default=90, gt=0)
    operational_event_retain_days: int = Field(default=90, gt=0)
    routing_decision_retain_days: int = Field(default=90, gt=0)
    cleanup_interval_s: int = Field(default=86_400, gt=0)
    cleanup_max_rows_per_pass: int = Field(default=5000, gt=0)
    event_loop_lag_enabled: bool = Field(
        default=False,
        description="Enable the one-second event-loop lag diagnostic monitor.",
    )
    dispatch_spans: DispatchSpansConfig = Field(
        default_factory=DispatchSpansConfig,
        description="Fine-grained dispatch-span instrumentation (Plan 029).",
    )
    detailed_span_sample_rate: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "Deprecated: use ``[metrics.dispatch_spans].sample_rate`` instead. "
            "Fraction of detailed dispatch spans to record (0.0-1.0). "
            "1.0 = full detail; 0.0 = coarse dispatch only. "
            "Deterministic by request ID. Maintained for backward "
            "compatibility; overrides ``dispatch_spans.sample_rate`` only when "
            "explicitly present."
        ),
    )

    @model_validator(mode="after")
    def _warn_deprecated_span_rate(self) -> MetricsConfig:
        if self.detailed_span_sample_rate is not None:
            import warnings  # noqa: PLC0415

            warnings.warn(
                "metrics.detailed_span_sample_rate is deprecated; use "
                "metrics.dispatch_spans.sample_rate",
                DeprecationWarning,
                stacklevel=2,
            )
        return self


class DashboardConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    public: bool = False
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
    trusted_proxies: list[str] = Field(default_factory=list)
    redact_headers: list[str] = Field(
        default_factory=lambda: ["authorization", "x-api-key"]
    )
    persist_redacted_error_detail: bool = False

    @field_validator("trusted_proxies")
    @classmethod
    def validate_trusted_proxies(cls, value: list[str]) -> list[str]:
        """Accept exact peer-address entries, not proxy networks."""
        normalized: list[str] = []
        for peer in value:
            if (
                not peer
                or peer != peer.strip()
                or len(peer) > 64
                or any(ord(char) < 32 or ord(char) == 127 for char in peer)
            ):
                raise ConfigError(
                    "security.trusted_proxies entries must be bounded, "
                    "non-empty exact peer addresses"
                )
            normalized.append(peer)
        return normalized


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


class ProviderStreamTimeoutConfig(BaseModel):
    """Explicit timeout policy for a provider's streaming response."""

    model_config = ConfigDict(extra="forbid")

    # ``None`` preserves the historical HTTPX read-timeout behaviour.  When
    # set, the coordinator owns the first-byte/idle timer and the client pool
    # raises its read guardrail to the largest configured stream interval.
    first_byte_timeout_s: float | None = Field(default=None, gt=0, le=86_400)
    idle_timeout_s: float | None = Field(default=None, gt=0, le=86_400)
    # Parsed for one-release backward compatibility; the coordinator no
    # longer enforces an absolute lifetime.
    max_lifetime_s: float | None = Field(default=None, ge=0, le=86_400)

    @model_validator(mode="after")
    def validate_lifetime(self) -> ProviderStreamTimeoutConfig:
        if self.max_lifetime_s == 0:
            self.max_lifetime_s = None
        return self

    def transport_read_timeout(self, configured_read_timeout_s: float) -> float:
        """Return the HTTPX read guardrail for this policy."""
        values = [configured_read_timeout_s]
        for value in (self.first_byte_timeout_s, self.idle_timeout_s):
            if value is not None:
                values.append(value)
        return max(values)


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    base_url: str
    protocols: list[ProtocolName] = Field(
        default_factory=lambda: ["openai"],
        min_length=1,
    )
    kind: str | None = None
    """Provider family (e.g. ``"anthropic"``, ``"openai"``)."""
    openai_path: str = "/chat/completions"
    anthropic_path: str = "/messages"
    # Plan 143: stateless OpenAI Responses endpoint surface. ``None``
    # means the provider does not advertise a Responses endpoint and
    # cannot receive ``POST /v1/responses`` traffic. The field is a
    # *surface* declaration rather than a new ``ProtocolName`` value;
    # ``protocols`` still records the OpenAI family.
    responses_path: str | None = None
    models_method: Literal["GET", "POST"] = "GET"
    models_path: str = "/models"
    connect_timeout_s: float = Field(default=5, gt=0)
    read_timeout_s: float = Field(default=300, gt=0)
    write_timeout_s: float = Field(default=30, gt=0)
    pool_timeout_s: float = Field(default=30, gt=0)
    stream_completion_policy: Literal["strict", "compatible", "permissive_observe"] = (
        "strict"
    )
    stream_timeouts: ProviderStreamTimeoutConfig = Field(
        default_factory=ProviderStreamTimeoutConfig
    )
    max_connections: int = Field(default=32, gt=0)
    max_keepalive: int = Field(default=8, gt=0)
    keepalive_timeout_s: float = Field(default=30, ge=0)
    routing_priority: int = Field(default=0, ge=0)
    accounts: list[AccountConfig] = Field(default_factory=list[AccountConfig])
    model_overrides: dict[str, ModelOverrideConfig] = Field(default_factory=dict)
    model_capabilities: dict[str, ModelCapabilitiesOverrideConfig] = Field(
        default_factory=dict,
    )
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
                if self.responses_path is not None:
                    paths_to_check.append(self.responses_path)
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
        result = parse_price_per_1k(value)
        # parse_price_per_1k returns None for negative upstream prices.
        # Operator-provided config values must still reject negatives.
        if result is not None:
            return result
        if value is None:
            return None
        # Detect negative values that parse_price_per_1k silently returns
        # as None for (catalog leniency). Config overrides must reject them.
        if isinstance(value, bool):
            raise ValueError("price must be numeric, not boolean")
        if isinstance(value, (int, float)) and value < 0:
            raise ValueError("price must be non-negative")
        if isinstance(value, str):
            import re as _re

            stripped = value.strip().replace("$", "").replace(",", "").replace("_", "")
            match = _re.search(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[-+]?\d+)?", stripped)
            if match is not None:
                from decimal import Decimal

                num = Decimal(match.group(0))
                if num < 0:
                    raise ValueError("price must be non-negative")
        return result

    @field_validator(
        "cache_read_per_million_microdollars",
        "cache_write_per_million_microdollars",
        mode="before",
    )
    @classmethod
    def parse_cache_price(cls, value: object) -> int | None:
        return parse_microdollars_per_million(value)


class ThinkingCapabilityOverrideConfig(BaseModel):
    """Override fields for the thinking/reasoning capability.

    When ``status`` is ``None`` the entire override is a no-op (all other
    fields should also be ``None``).  When ``status`` is set but ``source``
    is omitted it defaults to ``"manual_override"``.
    """

    model_config = ConfigDict(extra="forbid")

    status: CapabilityStatus | None = None
    source: CapabilitySource | None = None
    native_protocols: list[str] | None = None
    budget_tokens_min: int | None = None
    budget_tokens_max: int | None = None
    supported_efforts: list[str] | None = None
    effort_to_budget_tokens: dict[str, int] | None = None
    notes: str | None = None

    @field_validator("native_protocols", mode="after")
    @classmethod
    def validate_native_protocols(cls, value: list[str] | None) -> list[str] | None:
        """Reject unknown protocol names."""
        if value is None:
            return None
        allowed = {"openai", "anthropic"}
        for proto in value:
            if proto not in allowed:
                raise ConfigError(
                    f"Unknown native protocol {proto!r}; "
                    f"must be one of {sorted(allowed)}"
                )
        return value

    @model_validator(mode="after")
    def validate_thinking_overrides(self) -> ThinkingCapabilityOverrideConfig:
        """Enforce cross-field constraints for thinking overrides."""
        # When status is None the override is a no-op — all other fields
        # should be None too.  We silently accept and clear them so callers
        # don't have to be precise about every key.
        if self.status is None:
            self.source = None
            self.native_protocols = None
            self.budget_tokens_min = None
            self.budget_tokens_max = None
            self.supported_efforts = None
            self.effort_to_budget_tokens = None
            self.notes = None
            return self

        # Default source to manual_override when status is set but source is not.
        if self.source is None:
            self.source = "manual_override"

        if self.budget_tokens_min is not None and self.budget_tokens_min <= 0:
            raise ConfigError("budget_tokens_min must be > 0")
        if self.budget_tokens_max is not None and self.budget_tokens_max <= 0:
            raise ConfigError("budget_tokens_max must be > 0")
        if (
            self.budget_tokens_min is not None
            and self.budget_tokens_max is not None
            and self.budget_tokens_min > self.budget_tokens_max
        ):
            raise ConfigError(
                f"budget_tokens_min ({self.budget_tokens_min}) exceeds "
                f"budget_tokens_max ({self.budget_tokens_max})"
            )
        if self.effort_to_budget_tokens is not None:
            for effort, tokens in self.effort_to_budget_tokens.items():
                if tokens <= 0:
                    raise ConfigError(
                        f"effort_to_budget_tokens[{effort!r}] must be > 0, got {tokens}"
                    )
        return self


class MediaCapabilityOverrideConfig(BaseModel):
    """Override fields for a single media modality (image, document, audio).

    When every field is ``None`` the override is a no-op.  Boolean fields
    set to ``True`` enable the corresponding source form; ``False`` is
    treated as unknown/conservative and ignored.
    """

    model_config = ConfigDict(extra="forbid")

    base64: bool | None = None
    url: bool | None = None
    max_source_bytes: int | None = None

    @model_validator(mode="after")
    def validate_media_overrides(self) -> MediaCapabilityOverrideConfig:
        """Clear ``False`` booleans (they mean unknown, not disabled)."""
        if self.base64 is False:
            self.base64 = None
        if self.url is False:
            self.url = None
        if self.max_source_bytes is not None and self.max_source_bytes <= 0:
            raise ConfigError("max_source_bytes must be > 0")
        return self


class MultimodalCapabilityOverrideConfig(BaseModel):
    """Override fields for multimodal capabilities.

    When every field is ``None`` the override is a no-op.  Only
    explicitly-set non-``None`` values are merged into the base
    capabilities.
    """

    model_config = ConfigDict(extra="forbid")

    image_input: MediaCapabilityOverrideConfig | None = None
    document_input: MediaCapabilityOverrideConfig | None = None
    audio_input: MediaCapabilityOverrideConfig | None = None
    non_text_tool_result: bool | None = None
    max_serialized_request_bytes: int | None = None

    @model_validator(mode="after")
    def validate_multimodal_overrides(self) -> MultimodalCapabilityOverrideConfig:
        """Enforce cross-field constraints for multimodal overrides."""
        if (
            self.max_serialized_request_bytes is not None
            and self.max_serialized_request_bytes <= 0
        ):
            raise ConfigError("max_serialized_request_bytes must be > 0")
        return self


class ModelCapabilitiesOverrideConfig(BaseModel):
    """Per-model capability overrides.

    Wraps capability-specific override blocks (currently only ``thinking``).
    """

    model_config = ConfigDict(extra="forbid")

    thinking: ThinkingCapabilityOverrideConfig | None = None
    transcoding: TranscodingCapabilities | None = None
    multimodal: MultimodalCapabilityOverrideConfig | None = None


_OPENCODE_GO_BASE_URL = "https://opencode.ai/zen/go/v1"
_OPENCODE_GO_THINKING_MODELS: frozenset[str] = frozenset({"mimo-v2.5"})


def _default_opencode_go_thinking_capabilities() -> dict[
    str, ModelCapabilitiesOverrideConfig
]:
    """Return built-in capability metadata for the canonical OpenCode Go host."""
    return {
        model_id: ModelCapabilitiesOverrideConfig(
            thinking=ThinkingCapabilityOverrideConfig(
                status="supported",
                source="provider_catalog",
                native_protocols=["openai", "anthropic"],
                supported_efforts=["low", "medium", "high"],
                effort_to_budget_tokens={
                    "low": 1024,
                    "med": 4096,
                    "medium": 4096,
                    "high": 16384,
                },
                notes="OpenCode Go exposes low/medium/high thinking controls.",
            )
        )
        for model_id in _OPENCODE_GO_THINKING_MODELS
    }


def _provider_is_canonical_opencode_go(provider: ProviderConfig) -> bool:
    """Return whether *provider* is the bundled OpenCode Go endpoint."""
    return provider.base_url.rstrip("/") == _OPENCODE_GO_BASE_URL


def _seed_builtin_provider_capabilities(provider: ProviderConfig) -> None:
    """Seed known provider capabilities without clobbering operator overrides."""
    if not _provider_is_canonical_opencode_go(provider):
        return
    for model_id, capability in _default_opencode_go_thinking_capabilities().items():
        provider.model_capabilities.setdefault(model_id, capability)


class NetworkConfig(BaseModel):
    """Outbound HTTP client transport settings for background/CLI paths.

    Controls the optional shared ``OutboundClientManager`` client used by
    external pricing/model-info fetches and the opt-in update checker.
    Provider-specific clients (LLM forwarding) use per-provider
    ``[providers.<id>]`` transport settings instead.
    """

    model_config = ConfigDict(extra="forbid")

    connect_timeout_s: float = Field(default=10.0, gt=0)
    read_timeout_s: float = Field(default=30.0, gt=0)
    max_connections: int = Field(default=8, gt=0)
    max_keepalive: int = Field(default=2, gt=0)
    keepalive_expiry_s: float = Field(default=90.0, ge=0)

    @model_validator(mode="after")
    def validate_keepalive(self) -> NetworkConfig:
        if self.max_keepalive > self.max_connections:
            raise ConfigError(
                f"max_keepalive ({self.max_keepalive}) must not exceed "
                f"max_connections ({self.max_connections})"
            )
        return self


class MaintenanceBudgetConfig(BaseModel):
    """Budgets for periodic database maintenance tasks.

    Controls how many rows, batches, and how much wall-clock time each
    maintenance tick may use.  Conservative defaults protect SBC
    deployments from write-lock monopolization.
    """

    model_config = ConfigDict(extra="forbid")

    max_rows_per_batch: int = Field(default=500, ge=50, le=10000)
    max_batches_per_tick: int = Field(default=4, ge=1, le=20)
    max_tick_duration_ms: float = Field(default=500.0, ge=50.0, le=10000.0)
    contention_defer_above_lock_wait_p95_ms: float = Field(
        default=200.0,
        ge=0.0,
        le=5000.0,
        description="Defer P1/P2 maintenance when SQLite lock-wait p95 exceeds this.",
    )
    max_deferral_age_s: float = Field(
        default=3600.0,
        ge=60.0,
        le=86400.0,
        description="Maximum seconds a P1/P2 task may defer before forcing execution.",
    )
    # P0 tasks get higher budgets for correctness recovery
    p0_max_rows_per_batch: int = Field(default=1000, ge=100, le=20000)
    p0_max_batches_per_tick: int = Field(default=2, ge=1, le=10)
    p0_max_tick_duration_ms: float = Field(default=1000.0, ge=100.0, le=30000.0)


class BackupConfig(BaseModel):
    """Automatic backup configuration.

    Controls the in-process background backup task that periodically
    creates restore-compatible ``.zip`` archives of the configuration
    and database.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    interval_s: int = Field(default=86_400, ge=0)
    retain_count: int = Field(default=14, ge=1)
    startup_delay_s: int = Field(default=300, ge=0)
    directory: str | None = None
    include_env: bool = True


class UpdateCheckerConfig(BaseModel):
    """Optional in-process PyPI release check for dashboard status."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False


class ModelInfoSourceConfig(BaseModel):
    """Configuration for a single model-info source."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    priority: int = Field(default=100, ge=0)
    ttl_seconds: int = Field(default=86_400, gt=0)
    base_url: str | None = None
    api_key: str | None = None
    api_key_env: str | None = None
    max_entries: int = Field(default=4096, gt=0)
    options: dict[str, object] = Field(default_factory=dict[str, object])

    @property
    def resolved_api_key(self) -> str | None:
        if self.api_key:
            return self.api_key
        if self.api_key_env:
            return os.environ.get(self.api_key_env)
        return None


class ModelInfoSourcesConfig(BaseModel):
    """Configuration for all model-info sources."""

    model_config = ConfigDict(extra="forbid")

    provider_catalog: ModelInfoSourceConfig = Field(
        default_factory=lambda: ModelInfoSourceConfig(priority=0, ttl_seconds=300)
    )
    openrouter: ModelInfoSourceConfig = Field(default_factory=ModelInfoSourceConfig)
    artificial_analysis: ModelInfoSourceConfig = Field(
        default_factory=lambda: ModelInfoSourceConfig(enabled=False, priority=50)
    )
    huggingface: ModelInfoSourceConfig = Field(
        default_factory=lambda: ModelInfoSourceConfig(
            enabled=False, priority=200, ttl_seconds=604800
        )
    )


class ModelInfoAliasConfig(BaseModel):
    """A configured alias mapping a local model to a source-specific ID."""

    model_config = ConfigDict(extra="forbid")

    provider_id: str
    model_id: str
    source: str
    source_model_id: str
    confidence: str = "curated"
    notes: str | None = None


class ModelInfoOverrideConfig(BaseModel):
    """Manual field-level override for a model."""

    model_config = ConfigDict(extra="forbid")

    summary: str | None = None
    family: str | None = None
    display_name: str | None = None
    notes: str | None = None
    hide_benchmark_sources: bool = False
    status_override: str | None = None


class ModelInfoConfig(BaseModel):
    """Configuration for the model-info subsystem."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    startup_refresh: bool = True
    refresh_interval_s: int = Field(default=21_600, ge=0)
    known_ttl_s: int = Field(default=86_400, gt=0)
    partial_ttl_s: int = Field(default=43_200, gt=0)
    sparse_new_initial_ttl_s: int = Field(default=3_600, gt=0)
    sparse_new_later_ttl_s: int = Field(default=21_600, gt=0)
    sparse_new_accelerated_days: int = Field(default=7, ge=1)
    conflict_ttl_s: int = Field(default=7_200, gt=0)
    max_models_per_cycle: int = Field(default=50, ge=1, le=10_000)
    include_in_models_endpoint: bool = True
    store_raw_observations: bool = True
    sources: ModelInfoSourcesConfig = Field(default_factory=ModelInfoSourcesConfig)
    aliases: list[ModelInfoAliasConfig] = Field(
        default_factory=list[ModelInfoAliasConfig]
    )
    overrides: dict[str, ModelInfoOverrideConfig] = Field(
        default_factory=dict[str, ModelInfoOverrideConfig]
    )


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
    maintenance: MaintenanceBudgetConfig = Field(
        default_factory=MaintenanceBudgetConfig,
    )
    backup: BackupConfig = Field(default_factory=BackupConfig)
    readiness_probe: ReadinessProbeConfig = Field(
        default_factory=ReadinessProbeConfig,
    )
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    proxies: dict[str, ProxyConfig] = Field(default_factory=dict)
    accounts: list[AccountConfig] = Field(default_factory=list[AccountConfig])
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    model_overrides: dict[str, ModelOverrideConfig] = Field(default_factory=dict)
    model_capabilities: dict[str, ModelCapabilitiesOverrideConfig] = Field(
        default_factory=dict,
    )
    transcoder: TranscoderPolicy = Field(default_factory=TranscoderPolicy)
    model_info: ModelInfoConfig = Field(default_factory=ModelInfoConfig)
    update_checker: UpdateCheckerConfig = Field(default_factory=UpdateCheckerConfig)

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
        for provider in self.providers.values():
            _seed_builtin_provider_capabilities(provider)
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

    def validate_optional_dependencies(self) -> None:
        """Validate optional runtime dependencies required by this config."""
        proxy_configured = False
        for provider in self.providers.values():
            for account in provider.accounts:
                if self.resolve_account_proxy_url(account) is not None:
                    proxy_configured = True
                    break
            if proxy_configured:
                break
        if not proxy_configured:
            return

        import importlib.util

        if importlib.util.find_spec("pproxy") is None:
            raise ConfigError(
                "Configured account proxy support requires the optional pproxy "
                "dependency; install with `pip install 'eggpool[proxy]'`"
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
            config = cls.model_validate(raw)
            if config.server.port == 0:
                raise ConfigError(
                    "server.port must be between 1 and 65535 in production "
                    "configuration"
                )
            return config
        except Exception as exc:
            if isinstance(exc, ConfigError):
                raise
            raise ConfigError(f"Config validation failed: {exc}") from exc
