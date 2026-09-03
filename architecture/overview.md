# EggPool Architecture Overview

EggPool is a lightweight, LAN-hosted proxy that aggregates multiple LLM provider accounts behind OpenAI Chat, OpenAI Responses, and Anthropic Messages-compatible paths. Designed for Raspberry Pi and SBC deployments, it provides protocol transcoding, quota-aware routing, native Gemini wire codecs, and a self-updating dashboard.

The public OpenAI contract is intentionally limited to Chat Completions
(`POST /v1/chat/completions`), the stateless Responses surface
(`POST /v1/responses`), and model listing (`GET /v1/models`), alongside
Anthropic Messages (`POST /v1/messages`). EggPool does not claim full OpenAI
API parity. Responses rejects `previous_response_id`, conversation
references (including empty objects), omitted `store` (must be `false`
explicitly), `store=true`, and `background=true` before any provider
selection, but may adapt through the canonical boundary to an eligible
upstream wire surface. It does not implement
retrieval, cancellation, background jobs, or WebSocket transport.
`response.completed` is the only successful terminal Responses event;
`response.failed` and `response.incomplete` are terminal non-success
outcomes that do not trigger provider failover after downstream handoff.
Gemini streams use their native interaction or candidate finish evidence;
transport EOF never creates a terminal event.

## Quick Navigation

| # | Component | What It Does | Deep Dive |
|---|-----------|--------------|-----------|
| 1 | [Core Application](#1-core-application) | CLI, config, errors, JSON backend | [deep-dive-core.md](deep-dive-core.md) |
| 2 | [Request Lifecycle](#2-request-lifecycle) | Coordinator, API endpoints, proxy, finalization | [deep-dive-request-lifecycle.md](deep-dive-request-lifecycle.md) |
| 3 | [Protocol Transcoding](#3-protocol-transcoding) | OpenAI ↔ Anthropic body + streaming translation | [deep-dive-transcoder.md](deep-dive-transcoder.md) |
| 4 | [Routing & Quota](#4-routing--quota) | Priority tiers, fairness rotor, quota estimation | [deep-dive-routing.md](deep-dive-routing.md) |
| 5 | [Provider Architecture](#5-provider-architecture) | Client pool, contracts, auth, 23 bundled providers | [deep-dive-providers.md](deep-dive-providers.md) |
| 6 | [Database Layer](#6-database-layer) | SQLite WAL, migrations, repositories | [deep-dive-database.md](deep-dive-database.md) |
| 7 | [Runtime & Process Mgmt](#7-runtime--process-management) | Generations, supervisor, Granian worker | [deep-dive-runtime.md](deep-dive-runtime.md) |
| 8 | [Health Management](#8-health-management) | Circuit breaker, cooldown, failure effects, quarantine | [deep-dive-health.md](deep-dive-health.md) |
| 9 | [Background Tasks](#9-background-tasks) | TaskSupervisor, cleanup, backups | [deep-dive-background.md](deep-dive-background.md) |
| 10 | [Dashboard & Stats](#10-dashboard--stats) | Server-rendered HTML, JSON API, stats service | [deep-dive-dashboard.md](deep-dive-dashboard.md) |
| 11 | [Model Catalog](#11-model-catalog) | Discovery, normalization, pricing, protocol resolution | [deep-dive-catalog.md](deep-dive-catalog.md) |
| 12 | [Model Info Sidecar](#12-model-info-sidecar) | Multi-source metadata enrichment | [deep-dive-model-info.md](deep-dive-model-info.md) |
| 13 | [Control Plane](#13-control-plane) | Unix socket, live reload, staged generation swap | [deep-dive-control.md](deep-dive-control.md) |
| 14 | [Data Models](#14-data-models) | Pydantic config, domain, API, database models | [deep-dive-models.md](deep-dive-models.md) |
| 15 | [External Integrations](#15-external-integrations) | OpenCode, Claude Code, Aider, Codex, 8+ tools | [deep-dive-integrations.md](deep-dive-integrations.md) |
| 16 | [Security](#16-security) | Header redaction, API key auth, constant-time compare | [deep-dive-security.md](deep-dive-security.md) |
| 17 | [Observability](#17-observability) | Routing trace writer, structured diagnostics | [deep-dive-observability.md](deep-dive-observability.md) |
| 18 | [Retry Classification](#18-retry-classification) | Error categorization, backoff, retry decisions | [deep-dive-retry.md](deep-dive-retry.md) |
| 19 | [Metrics & Telemetry](#19-metrics--telemetry) | Thinking counters, event-loop lag, dispatch overhead | [deep-dive-metrics.md](deep-dive-metrics.md) |
| 20 | [Lifecycle Management](#20-lifecycle-management) | Backup, restore, uninstall orchestration | [deep-dive-lifecycle.md](deep-dive-lifecycle.md) |
| 21 | [Deployment & Operations](#21-deployment--operations) | Systemd, scripts, install, operational tools | [deep-dive-deployment.md](deep-dive-deployment.md) |

## System Architecture at a Glance

```
┌─────────────────────────────────────────────────────────────────────┐
│              Client (OpenAI Chat / Anthropic Messages SDK)          │
└───────────────────────────────┬─────────────────────────────────────┘
                                │ HTTP
┌───────────────────────────────▼─────────────────────────────────────┐
│                        FastAPI Application                          │
│  ┌──────────────┐  ┌──────────────┐  ┌────────────────────────┐    │
│  │  API Handlers │  │  Dashboard   │  │  Control Plane         │    │
│  │  (chat, msgs) │  │  (HTML/JSON) │  │  (Unix socket rehash)  │    │
│  └──────┬───────┘  └──────────────┘  └────────────────────────┘    │
│         │                                                          │
│  ┌──────▼───────────────────────────────────────────────────────┐  │
│  │              RequestCoordinator                               │  │
│  │  endpoint → routing → persistence → contract → transcode     │  │
│  │              → proxy → streaming → finalization               │  │
│  └──────┬───────────────────────────────────────────────────────┘  │
│         │                                                          │
│  ┌──────▼──────┐  ┌──────────────┐  ┌────────────────────────┐   │
│  │   Router     │  │  Transcoder  │  │  Provider Client Pool  │   │
│  │  (quota-aware│  │  (OpenAI↔    │  │  (httpx per-provider)  │   │
│  │   scoring)   │  │   Anthropic) │  │                        │   │
│  └──────────────┘  └──────────────┘  └────────────┬───────────┘   │
│                                                     │               │
│  ┌─────────────────────────────────────────────────▼────────────┐  │
│  │                     SQLite (WAL mode)                         │  │
│  │  requests | attempts | routing_decisions | models | accounts  │  │
│  │  quotas | pings | backoffs | model_info_*                 │  │
│  └──────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
                                │
                    ┌───────────▼───────────┐
                    │   Upstream Providers   │
                    │   (23 bundled providers)  │
                    └───────────────────────┘
```

## Request Lifecycle (Simplified)

1. **Endpoint** — Client hits `/v1/chat/completions` (OpenAI) or `/v1/messages` (Anthropic)
2. **Routing** — `Router` selects best account via priority tiers + `QuotaFairScorer`
3. **Persistence** — Attempt + routing decision written to SQLite before dispatch
4. **Provider Contract** — `compose_provider_url()` + `build_upstream_headers()` from provider config
5. **Wire Codec** — Canonical source intent encoded for the selected concrete upstream surface
6. **Proxy** — Request sent via `ProviderClientPool` httpx client
7. **Streaming** — One bounded `SSEDecoder` frames each upstream stream; shared frames are observed and codec-translated if needed, with native terminal evidence required
8. **Finalization** — Usage recorded, reservations released, health updated

## Module Overview

Each module below is a discrete component of the system. Cross-references
point to related deep dives for implementation details.

### 1. Core Application

| | |
|---|---|
| **Path** | `src/eggpool/cli.py`, `src/eggpool/cli_full.py`, `src/eggpool/fastcli.py`, `src/eggpool/config.py`, `src/eggpool/errors.py`, `src/eggpool/constants.py`, `src/eggpool/jsonx.py`, `src/eggpool/auth.py` |
| **Deep Dive** | [deep-dive-core.md](deep-dive-core.md) |

The CLI entry point is a two-phase bootstrap: `cli.py` (73 lines) first tries `fastcli.maybe_run_fast_command()` for `croncheck`/`ensure-running` without importing Click, keeping the Raspberry Pi watchdog path lightweight. Everything else falls through to `cli_full.py` (~5000 lines of Click commands). Configuration lives in `config.toml` + `.env`; `config.py` ensures a config file exists by copying the bundled template. `errors.py` defines the typed exception hierarchy (`AggregatorError` → 20+ subclasses). `jsonx.py` abstracts over `orjson` (preferred) and stdlib `json` for hot-path serialization. `auth.py` provides constant-time API key verification via `hmac.compare_digest`.

**Related**: [deep-dive-runtime.md](deep-dive-runtime.md) (fastcli), [deep-dive-control.md](deep-dive-control.md) (reload policy), [deep-dive-security.md](deep-dive-security.md) (auth)

### 2. Request Lifecycle

| | |
|---|---|
| **Path** | `src/eggpool/request/`, `src/eggpool/api/`, `src/eggpool/proxy/` |
| **Deep Dive** | [deep-dive-request-lifecycle.md](deep-dive-request-lifecycle.md) |

The request lifecycle is orchestrated by `RequestCoordinator` in `request/coordinator.py`. API handlers (`api/chat_completions.py` for OpenAI Chat Completions, `api/responses.py` for the stateless OpenAI Responses surface, `api/messages.py` for Anthropic) parse the request and call `handle_proxy_request()` — a shared pipeline in `api/proxy_request.py` that handles auth, body parsing, model/provider resolution, capability checks, context-limit enforcement, transcoding preflight, dispatch, and response handling. The coordinator persists each attempt to SQLite before dispatch, manages retry with failover, and ensures every terminal outcome is owned by exactly one `RequestFinalizationJob`. Dispatch retries are limited to typed HTTPX transport failures before an explicit response-handoff fact; local preparation and response-adaptation faults are isolated as local errors. Non-streaming adaptation precedes durable success, while streaming responses flow through `proxy/sse.py` (bounded SSE decoder) and `proxy/sse_observer.py` (diagnostic observation). The wire surface is selected by the `request_surface` field on
`ProxyEndpointConfig` and `ProxyRequestContext` (`"chat_completions"` or
`"responses"`), so the same coordinator dispatches both surfaces without
adding a third protocol family.

**Related**: [deep-dive-routing.md](deep-dive-routing.md), [deep-dive-transcoder.md](deep-dive-transcoder.md), [deep-dive-providers.md](deep-dive-providers.md), [deep-dive-health.md](deep-dive-health.md)

### 3. Protocol Transcoding

| | |
|---|---|
| **Path** | `src/eggpool/transcoder/` |
| **Deep Dive** | [deep-dive-transcoder.md](deep-dive-transcoder.md) |

Transparent request/response format conversion between OpenAI Chat Completions and Anthropic Messages protocols. When a client sends Anthropic Messages but the routed provider only supports OpenAI Chat Completions (or vice versa), the transcoder translates both the request body and the streaming response. `BodyTranscoder` Protocol (`protocol.py`) defines the interface; `OpenAIToAnthropic` and `AnthropicToOpenAI` are the concrete implementations. Streaming translation (`streaming.py`) handles SSE frame-by-frame with synchronous `translate_frame()` and `finish()`. The transcoder also handles tool-use translation, thinking/reasoning control normalization, and configurable reasoning field names.

**Related**: [deep-dive-providers.md](deep-dive-providers.md), [deep-dive-request-lifecycle.md](deep-dive-request-lifecycle.md)

### 4. Routing & Quota

| | |
|---|---|
| **Path** | `src/eggpool/routing/`, `src/eggpool/quota/` |
| **Deep Dive** | [deep-dive-routing.md](deep-dive-routing.md) |

Quota-aware account routing with tiered priority, fairness rotation, and eligibility filtering. `Router` (`routing/router.py`) groups eligible accounts into priority tiers (highest `routing_priority` first), scores within a tier via `QuotaFairScorer` (`quota/scorer.py`), and applies a `FairnessRotor` within epsilon-bands of equally-scored accounts. Scoring is load-based (request count + token count utilization), never cost-based. `routing/eligibility.py` implements a multi-gate eligibility chain (enabled, credentials, health, provider match, protocol support, circuit breaker, catalog freshness, thinking capability, quarantine, quota capacity). `quota/estimation.py` tracks per-account usage with 5h/7d/30d rolling windows and EWMA cost estimation.

**Related**: [deep-dive-health.md](deep-dive-health.md), [deep-dive-providers.md](deep-dive-providers.md), [deep-dive-database.md](deep-dive-database.md)

### 5. Provider Architecture

| | |
|---|---|
| **Path** | `src/eggpool/providers/`, `src/eggpool/accounts/` |
| **Deep Dive** | [deep-dive-providers.md](deep-dive-providers.md) |

Supports 23 bundled upstream providers (including OpenCode Go, native Gemini, and a generic custom-compatible endpoint). Additional providers are supportable through the generic custom-compatible path when they implement a declared EggPool wire surface. `ProviderClientPool` (`providers/client_pool.py`) manages per-provider `httpx.AsyncClient` instances with independent connection pools. `providers/contract.py` centralizes URL composition (`compose_provider_url()`), provider auth, and resolved wire-profile header construction; `wire/` keeps concrete endpoint and codec identities independent from `ProtocolName`. `providers/outbound.py` manages a shared HTTP client for non-provider network paths. Models are exposed with provider-suffixed IDs (`model-id/provider-id`); `parse_model_provider()` in `routing/provider.py` is the canonical suffix parser. `accounts/registry.py` maintains the in-memory account registry; `accounts/state.py` tracks per-account runtime state (active requests, health, quota).

**Related**: [deep-dive-catalog.md](deep-dive-catalog.md), [deep-dive-routing.md](deep-dive-routing.md), [deep-dive-request-lifecycle.md](deep-dive-request-lifecycle.md)

### 6. Database Layer

| | |
|---|---|
| **Path** | `src/eggpool/db/` |
| **Deep Dive** | [deep-dive-database.md](deep-dive-database.md) |

Async SQLite with WAL mode, single-connection serialization, and 54 numbered schema migrations (`0001_initial.sql` through `0054_model_quarantine_null_identity.sql`). `db/connection.py` wraps `aiosqlite` with `BEGIN IMMEDIATE` transactions and explicit one-task ownership; inherited child tasks fail before SQL. Runtime invalidation closes admission and exits the worker for systemd restart. Startup integrity checking and crash reconciliation are the only durable recovery boundary. All DML runs inside `async with db.transaction():`. `db/repositories.py` contains `AccountRepository`, `ProviderRepository`, `RequestRepository`, and others; `db/rollup_repository.py` owns pre-aggregated rollups. Schema lives in `db/schema/` with numbered SQL files and a `checksums.json` manifest applied by `db/migrations.MigrationRunner`.

**Related**: [deep-dive-request-lifecycle.md](deep-dive-request-lifecycle.md), [deep-dive-routing.md](deep-dive-routing.md), [deep-dive-health.md](deep-dive-health.md)

### 7. Runtime & Process Management

| | |
|---|---|
| **Path** | `src/eggpool/runtime_manager.py`, `src/eggpool/runtime.py`, `src/eggpool/reload_transaction.py`, `src/eggpool/generation_factory.py` |
| **Deep Dive** | [deep-dive-runtime.md](deep-dive-runtime.md) |

Process model: supervisor + 1 Granian worker (`workers=1`), PID file owned by supervisor, default `runtime_threads=1` (single event-loop thread is canonical) and `database.worker_threads=1`. `RuntimeManager` owns active/retiring generation slots — immutable frozen-dataclass snapshots (`RuntimeGeneration`) swapped atomically. Request-path code obtains a `GenerationLease`; a generation swap never interrupts an in-flight request. Lease acquisition is fail-closed: `RuntimeManagerLeaseExhaustedError` → HTTP 503. Staged reload: `stage()` → `commit()`/`rollback()` → `finalize_retirement()`. `ReloadTransaction` (`reload_transaction.py`) is a monotonic state machine with atomic commit semantics across SQLite and runtime publication.

**Related**: [deep-dive-core.md](deep-dive-core.md), [deep-dive-control.md](deep-dive-control.md), [deep-dive-background.md](deep-dive-background.md)

### 8. Health Management

| | |
|---|---|
| **Path** | `src/eggpool/health/`, `src/eggpool/failure/` |
| **Deep Dive** | [deep-dive-health.md](deep-dive-health.md) |

Circuit breaker-based health tracking for accounts and models. `HealthManager` (`health/health_manager.py`) manages per-account health with consecutive failure counting, bounded 1,800-second nonterminal cooldowns, circuit breaker probe-slot acquisition, operator-initiated disable/enable, and scoped model quarantine. `health/circuit_breaker.py` implements per-account circuit breaker logic with total probe convergence. `health/writable_probe.py` provides `DatabaseWritableProbe` — a real SQLite write probe cached for `/readyz` (never performs a write on the request path). `failure/` implements typed failure effects: `classify_failure_effects()` returns the canonical retry/effects decision, and `EffectsApplier` applies it with attempt-owned component progress. Authentication and authoritative model withdrawal remain terminal; runtime model absence is account/model scoped and expires or clears on matching recovery.

**Related**: [deep-dive-routing.md](deep-dive-routing.md), [deep-dive-request-lifecycle.md](deep-dive-request-lifecycle.md), [deep-dive-retry.md](deep-dive-retry.md)

### 9. Background Tasks

| | |
|---|---|
| **Path** | `src/eggpool/background/` |
| **Deep Dive** | [deep-dive-background.md](deep-dive-background.md) |

`TaskSupervisor` manages supervised background tasks with restart, backoff, and periodic scheduling. Tasks run in daemon mode (long-running coroutine with restart + exponential backoff) or periodic mode (supervisor-owned scheduling with tick factory, initial delay, timeout). Process-owned tasks survive generation swaps; generation-leased tasks retire with their generation. The ordinary profile registers checkpoint and low-wear metrics flush; update_checker and automatic_backup are opt-in process-owned tasks. `background/backup.py` handles automatic backup scheduling. `background/cleanup.py` reconciles expired reservations. `background/maintenance.py` implements periodic DB maintenance.

**Related**: [deep-dive-runtime.md](deep-dive-runtime.md), [deep-dive-database.md](deep-dive-database.md), [deep-dive-catalog.md](deep-dive-catalog.md)

### 10. Dashboard & Stats

| | |
|---|---|
| **Path** | `src/eggpool/dashboard/`, `src/eggpool/stats/`, `src/eggpool/api/stats.py` |
| **Deep Dive** | [deep-dive-dashboard.md](deep-dive-dashboard.md) |

Self-updating server-rendered HTML dashboard with 50 bundled themes (user-supplied themes merge over them). `dashboard/routes.py` registers page routes (overview, cache, runtime, etc.) and JSON API endpoints. `dashboard/render.py` handles HTML rendering with `CacheAdvancedState` controlling disclosure visibility. The stats layer (`stats/service.py`, `stats/queries.py`) provides SQL query functions for timeseries, cache metrics, transcoding stats, and dashboard explanations. JSON API endpoints under `/api/stats/` expose summary, accounts, models, timeseries, errors, latency, pings, routing, operational, and pending-health data. The dashboard is auth-gated separately from the proxy API.

**Related**: [deep-dive-metrics.md](deep-dive-metrics.md), [deep-dive-observability.md](deep-dive-observability.md)

### 11. Model Catalog

| | |
|---|---|
| **Path** | `src/eggpool/catalog/` |
| **Deep Dive** | [deep-dive-catalog.md](deep-dive-catalog.md) |

Model discovery, normalization, pricing, protocol resolution, and capability detection. `catalog/service.py` orchestrates periodic refresh from provider `/v1/models` endpoints. `catalog/fetcher.py` calls each provider's models endpoint; `catalog/normalizer.py` normalizes heterogeneous response shapes into a canonical model list. `catalog/protocols.py` resolves per-model protocol (`openai`/`anthropic`) via a 6-tier resolution chain. `catalog/capabilities.py` tracks model capabilities (thinking support, budget bounds). `catalog/pricing.py` and `catalog/pricing_resolver.py` handle cost estimation with alias resolution. `catalog/cache.py` maintains the in-memory model catalog cache with `AccountCatalogOutcome` tracking.

**Related**: [deep-dive-providers.md](deep-dive-providers.md), [deep-dive-model-info.md](deep-dive-model-info.md), [deep-dive-routing.md](deep-dive-routing.md)

### 12. Model Info Sidecar

| | |
|---|---|
| **Path** | `src/eggpool/model_info/` |
| **Deep Dive** | [deep-dive-model-info.md](deep-dive-model-info.md) |

Multi-source model metadata enrichment. `ModelInfoService` orchestrates periodic refresh from external sources (OpenRouter, Hugging Face, Artificial Analysis, provider catalog). `model_info/sources/` contains adapter implementations for each source. `model_info/matching.py` implements 7-tier matching for model lookup (configured alias → exact source ID → normalized exact → deployment-suffix → release-suffix → regex rule → guarded similarity). `model_info/repository.py` persists metadata to SQLite. Enriched metadata powers model display, pricing, and capability detection.

**Related**: [deep-dive-catalog.md](deep-dive-catalog.md)

### 13. Control Plane

| | |
|---|---|
| **Path** | `src/eggpool/control/` |
| **Deep Dive** | [deep-dive-control.md](deep-dive-control.md) |

Unix-domain socket control server for live config rehash. `control/server.py` implements a single-shot newline-delimited JSON protocol (v1) on a UDS at `<runtime_dir>/eggpool.sock` (resolved by `runtime_paths.runtime_dir()`: `$EGGPOOL_RUNTIME_DIR` → `$XDG_RUNTIME_DIR/eggpool` → `/tmp/eggpool-<UID>.runtime`). for `eggpool rehash`. Socket mode `0o600` (owner-only). `control/client.py` connects from the CLI to issue reload commands. `control/reload_manager.py` orchestrates the staged reload: `stage()` → `commit()`/`rollback()` → `finalize_retirement()`. The control plane is the only path for live config changes without process restart.

**Related**: [deep-dive-runtime.md](deep-dive-runtime.md), [deep-dive-core.md](deep-dive-core.md), [deep-dive-deployment.md](deep-dive-deployment.md)

### 14. Data Models

| | |
|---|---|
| **Path** | `src/eggpool/models/` |
| **Deep Dive** | [deep-dive-models.md](deep-dive-models.md) |

Pydantic v2 models for configuration, domain objects, internal API payloads, and database rows. `models/config.py` defines `AppConfig` and all nested config models with field validators and TOML parsing. `models/api.py` defines internal API models (`HealthResponse`, `ReadyResponse`, `ErrorResponse`, model-listing payloads) — wire request/response shapes are parsed at the endpoint layer. `models/database.py` defines SQLite row models. `models/domain.py` defines shared domain objects (`Provider`, `Account`, `AccountRuntimeState`, `ModelDescriptor`). These models are the single source of truth for schema validation and serialization boundaries.

**Related**: [deep-dive-core.md](deep-dive-core.md), [deep-dive-database.md](deep-dive-database.md), [deep-dive-request-lifecycle.md](deep-dive-request-lifecycle.md)

### 15. External Integrations

| | |
|---|---|
| **Path** | `src/eggpool/integrations/` |
| **Deep Dive** | [deep-dive-integrations.md](deep-dive-integrations.md) |

Configuration snippet generators for 11 external tools: OpenCode, Claude Code, Aider, Codex, Qwen Code, Kilo, Continue, Cline, Roo Code, Goose, and OpenHands. `integrations/common.py` owns configsetup context construction, catalog-backed default model resolution, and format-safe scalar/key rendering helpers. Invoked via `eggpool configsetup <target>` CLI commands.

**Related**: [deep-dive-catalog.md](deep-dive-catalog.md)

### 16. Security

| | |
|---|---|
| **Path** | `src/eggpool/security/`, `src/eggpool/auth.py` |
| **Deep Dive** | [deep-dive-security.md](deep-dive-security.md) |

Header redaction middleware (`security/redaction.py`) strips configured sensitive headers from upstream responses. API key auth (`auth.py`) uses constant-time `hmac.compare_digest` comparison, accepting `Authorization: Bearer <key>` or `X-API-Key` header. The bearer-prefix guard rejects API keys that begin with `Bearer` for providers configured with `auth.mode = "bearer"`. The middleware stack in `app.py` includes body-limit enforcement (10 MB max), header redaction, CORS, and trusted-host filtering.

**Related**: [deep-dive-core.md](deep-dive-core.md)

### 17. Observability

| | |
|---|---|
| **Path** | `src/eggpool/observability/` |
| **Deep Dive** | [deep-dive-observability.md](deep-dive-observability.md) |

Routing trace persistence for debugging and dashboard drill-down. `observability/routing_trace_writer.py` implements a process-owned, single-drain-task writer that collects immutable `RoutingTraceEvent` objects via a non-blocking `submit()` and persists them in micro-batches via `RoutingDecisionRepository`. Bounded queue drops newest events when full. Thread-safe submission via `threading.Lock`. Silent failures — every exception is swallowed and its counter incremented. Routing traces are opt-in via `[routing.trace] mode = "off" | "sampled" | "all"` (default off).

**Related**: [deep-dive-routing.md](deep-dive-routing.md), [deep-dive-dashboard.md](deep-dive-dashboard.md)

### 18. Retry Classification

| | |
|---|---|
| **Path** | `src/eggpool/retry/` |
| **Deep Dive** | [deep-dive-retry.md](deep-dive-retry.md) |

Upstream failure classification and retry decision logic. `retry/classification.py` defines `RetryCategory` (NEVER, BAD_REQUEST, AUTH_FAILURE, QUOTA_EXCEEDED, TEMPORARY, TRANSIENT, FATAL, MODEL_UNAVAILABLE) and `RetryClassifier.classify()`, which adapts the canonical `classify_failure_effects()` decision into a retry category with optional `retry_after` durations. Integrates with `failure/classifier.py` for typed failure effects.

**Related**: [deep-dive-health.md](deep-dive-health.md), [deep-dive-request-lifecycle.md](deep-dive-request-lifecycle.md), [deep-dive-providers.md](deep-dive-providers.md)

### 19. Metrics & Telemetry

| | |
|---|---|
| **Path** | `src/eggpool/metrics/`, `src/eggpool/event_loop_lag.py`, `src/eggpool/runtime_metrics.py`, `src/eggpool/runtime_dispatch.py` |
| **Deep Dive** | [deep-dive-metrics.md](deep-dive-metrics.md) |

Structured observability across three subsystems. `metrics/buffer.py` implements a low-wear metrics buffer with periodic flush to SQLite. `metrics/thinking.py` tracks thinking/reasoning decision outcomes with low-cardinality labels. `metrics/failure_effects.py` records normalized failure effect counters. `event_loop_lag.py` implements a lightweight event-loop lag monitor for SBC deployments. `runtime_metrics.py` gathers process topology, memory, background task state, database health, OS load average, and dispatch-overhead distribution. `runtime_dispatch.py` implements always-on and opt-in dispatch timing recorders using monotonic clocks.

**Related**: [deep-dive-dashboard.md](deep-dive-dashboard.md), [deep-dive-request-lifecycle.md](deep-dive-request-lifecycle.md), [deep-dive-runtime.md](deep-dive-runtime.md)

### 20. Lifecycle Management

| | |
|---|---|
| **Path** | `src/eggpool/lifecycle/` |
| **Deep Dive** | [deep-dive-lifecycle.md](deep-dive-lifecycle.md) |

Backup, restore, and uninstall orchestration. `lifecycle/backup.py` creates timestamped `.zip` archives containing `config.toml`, `.env`, and `usage.sqlite3` with uncompressed storage. `lifecycle/uninstall.py` reverses installation by detecting the installer method (`pipx`, `uv tool`, source, manual) and scrubbing PATH entries added by the install script. Both modules are invoked by CLI commands (`eggpool backup`, `eggpool uninstall`) and designed for testability without terminal interaction.

**Related**: [deep-dive-core.md](deep-dive-core.md), [deep-dive-deployment.md](deep-dive-deployment.md), [deep-dive-database.md](deep-dive-database.md)

### 21. Deployment & Operations

| | |
|---|---|
| **Path** | `deploy/`, `scripts/`, `src/eggpool/deploy/` |
| **Deep Dive** | [deep-dive-deployment.md](deep-dive-deployment.md) |

Production deployment assets and operational tooling. `deploy/eggpool.service` is a security-hardened systemd unit (NoNewPrivileges, ProtectSystem=strict, PrivateTmp, syscall filtering). `deploy/eggpool-logrotate.conf` handles daily log rotation with 14-day retention. `scripts/` contains 12 operational scripts: `install.sh` (quick installer via pipx/uv tool), `check_database.py` (read-only DB invariant checker), `validate_routing.py` (routing invariant validator), `verify_upstream_auth.py` (direct upstream auth verification), `smoke_test.py` (deployment smoke test), and diagnostic/repro scripts. `src/eggpool/deploy/__init__.py` bundles systemd unit, crontab entry, and install script as Python constants for programmatic access.

**Related**: [deep-dive-control.md](deep-dive-control.md), [deep-dive-lifecycle.md](deep-dive-lifecycle.md), [deep-dive-core.md](deep-dive-core.md)

## Key Architecture Patterns

### Runtime Generations
Immutable frozen-dataclass snapshots swapped atomically via `RuntimeManager`. Request-path code obtains a `GenerationLease` — a generation swap never interrupts an in-flight request.

### Process Model
Supervisor + 1 Granian worker (`workers=1`). PID file owned by supervisor. Default `runtime_threads=1` (single event-loop thread is canonical).

### Database Invariants
SQLite WAL with single-connection serialization. All DML runs inside `async with db.transaction():`. 54 schema migrations tracked by checksums.

### JSON Backend
`eggpool.jsonx` abstracts over `orjson` (preferred) and stdlib `json`. Hot-path serialization, SSE frame helpers, and request body parsing all route through this layer.

### Error Hierarchy
`AggregatorError` → `UpstreamError` → specific subclasses. `CapabilityError` for thinking/reasoning mismatches. `TranscodeLossError` for loss-policy rejects.

### Fast-Path CLI
`src/eggpool/fastcli.py` handles `croncheck` and `ensure-running` without importing Click — stays lightweight for Raspberry Pi watchdog cron jobs.

### Provider Payload Lifecycle
`ProviderBoundRequest` is the single provider-payload authority after client parsing. Generation-aware path-level copy-on-write handles narrow mutations, `adopt_provider_payload()` accepts EggPool-owned transformed graphs and request-local prepared transcode generations, conservative setters protect unknown graphs, and one final serialization cache freezes the request before dispatch.

### Thinking-Control Adaptation Ownership
`adapt_thinking_controls()` accepts a read-only `Mapping[str, Any]` source and builds its own shallow-copied working root. No-op adaptation leaves `payload_generation` unchanged and preserves the cached provider bytes.

Provider/model reasoning metadata is represented by `ThinkingControlContract`
with independent `toggle`, `effort`, and `budget` support dimensions. The
catalog normalizer distinguishes omitted `reasoning_options` from a complete
empty list and never fabricates effort token budgets; legacy `mode` and
top-level capability fields are compatibility inputs/projections only.
Authority is field-level: operator overrides, explicit live provider metadata,
verified provider-scoped model-info metadata, then unknown. Model-family names
never provide reasoning-control defaults.

### Request Finalization
Every live terminal outcome is owned by one kind-qualified command in the generation-owned `RequestFinalizationSupervisor`: selected request finalization, failed-attempt cleanup, or post-commit claim compensation. Each accepted command retains one terminal reference on its generation until durable and required runtime convergence.

## Directory Structure

```
src/eggpool/
├── _share/                # Bundled config examples for pipx installs
├── accounts/              # Account registry and per-account runtime state
├── api/                   # Endpoint handlers: chat completions, responses,
│                          #   messages, models, stats, runtime, update, backoff
├── background/            # TaskSupervisor tasks: cleanup, backup scheduling,
│                          #   maintenance
├── catalog/               # Model catalog: fetcher, normalizer, protocols,
│                          #   capabilities, pricing, cache, refresh state
├── control/               # Control plane: UDS server/client, reload manager,
│                          #   accepted-finalization invariant
├── dashboard/             # Server-rendered HTML dashboard (50 bundled themes)
│                          #   + rendering, theming, timeseries bucketing
├── db/                    # SQLite connection (one-task ownership),
│                          #   MigrationRunner, repositories, rollups, schema/
├── deploy/                # Bundled deployment assets as Python constants
├── failure/               # classify_failure_effects(), EffectsApplier,
│                          #   signal extraction, model quarantine
├── health/                # HealthManager, circuit breaker, bounded backoff,
│                          #   DatabaseWritableProbe (/readyz)
├── integrations/          # configsetup generators + TARGET_SPECS registry
├── lifecycle/             # backup / uninstall orchestration
├── metrics/               # Metrics buffer, thinking counters, failure counters
├── model_info/            # Metadata sidecar: sources, matching, repository,
│                          #   scheduler, dedup/identity/normalization
├── models/                # Pydantic v2: config, api, database, domain models
├── observability/         # Routing trace writer (micro-batched, opt-in)
├── providers/             # _templates.toml (23 bundled providers), client
│                          #   pool, URL/auth contracts, outbound, pproxy
├── proxy/                 # SSE decoder/observer, usage normalization, cost
│                          #   reporting, shared upstream client
├── quota/                 # QuotaWindow estimation, reservations, scorer, audit
├── request/               # RequestCoordinator, AttemptFinalizer, finalization
│                          #   job/supervisor glue, claim lifecycle, terminal
│                          #   status, stream completion classification
├── retry/                 # RetryCategory classification from HTTP outcomes
├── routing/               # Router (priority tiers), eligibility chain,
│                          #   fairness rotor, provider-suffix parsing
├── security/              # Header redaction middleware
├── stats/                 # Dashboard query layer: timeseries, cache metrics,
│                          #   segmentation, explanations
├── transcoder/            # OpenAI ↔ Anthropic encoders, streaming translation,
│                          #   budget resolver, cache stability, policy
├── app.py                 # FastAPI application factory with lifespan management
├── auth.py                # Local API key auth (constant-time compare)
├── cli.py                 # CLI bootstrap (~73 lines; fast commands first)
├── cli_exit_codes.py      # Stable exit codes (e.g. EXIT_RELOAD_BUSY)
├── cli_full.py            # Full Click CLI (lazy-imported by cli.py)
├── cli_rehash_format.py   # rehash JSON/human output formatting
├── cli_rehash_helper.py   # Shared validate-and-rehash helper
├── config.py              # Config file discovery/bootstrap helpers
├── config_reload_policy.py  # Typed config diff and live-reload policy
├── config_utils.py        # Config utilities for CLI and integrations
├── config_validation.py   # validate_config_file() → ConfigValidationError
├── constants.py           # Project-wide constants
├── cost_recompute.py      # Recompute historical costs from current prices
├── cost_repair.py         # Repair suspicious historical costs (guarded)
├── deploy_user.py         # User/path resolution for `eggpool deploy`
├── errors.py              # Typed exception hierarchy (AggregatorError base)
├── event_loop_lag.py      # Event-loop lag monitor (opt-in)
├── fastcli.py             # Stdlib-only fast path: croncheck / ensure-running
├── generation_factory.py  # RuntimeGenerationFactory (startup ≡ reload path)
├── jsonx.py               # JSON backend abstraction (orjson preferred)
├── logging.py             # Structured logging setup
├── onboard.py             # Interactive first-run onboarding
├── reload_diagnostics.py  # Reload result categories/counters/finalization
├── reload_transaction.py  # Staged reload state machine (SQLite-atomic)
├── runtime.py             # Process management: start/restart/stop, PID files
├── runtime_dispatch.py    # Dispatch overhead / pre-upstream timing recorders
├── runtime_manager.py     # Generation slots, leases, staged swap ownership
├── runtime_metrics.py     # RuntimeMetricsService snapshots
├── runtime_paths.py       # PID/log path resolution (stdlib-only)
├── runtime_task_inventory.py  # Task ownership inventory for shutdown/reload
├── runtime_tasks.py       # Unified background task registration per profile
├── toml_edit.py           # Formatting-preserving scalar TOML edits
└── update_checker.py      # PyPI release checking (freshness-aware)

tests/
├── unit/                  # 249 files — focused module-level behavior
├── integration/           # 72 files + 46 in reload/ — cross-component
│                          #   request, reload, and lifecycle behavior
├── contract/              # 3 files — wire-level protocol preservation
├── smoke/                 # 10 files — CI gate (import, config, DB, requests)
├── perf/                  # 4 files — hot-path microbenchmarks (manual)
├── live/                  # 1 file — opt-in real-network enrichment
├── helpers/               # Shared test utilities
└── fixtures/              # Test fixtures (streaming, etc.)

scripts/                # 12 operational, diagnostic, installer scripts
deploy/                 # Systemd unit, logrotate, env template
docs/                   # Operator documentation
plans/                  # Historical implementation plans
architecture/           # This directory — architecture docs and deep dives
```

## Configuration

Runtime configuration lives in `config.toml` + `.env` (API keys). Key sections:

| Section | Purpose |
|---------|---------|
| `[server]` | Host, port, workers |
| `[upstream]` | Default upstream settings |
| `[database]` | SQLite path and WAL mode |
| `[routing]` | Fairness mode/epsilon/scope |
| `[models]` | `collapse_models`, catalog withdrawal |
| `[providers.<id>]` | Per-provider config (URL, auth, protocols, accounts) |
| `[transcoder]` | Protocol transcoding features, thinking/reasoning |
| `[model_info]` | Source enablement, TTLs, overrides |
| `[dashboard]` | Theme, auth policy |
| `[metrics]` | Buffering, flush modes |
| `[backup]` | Automatic backup schedule |
| `[security]` | Header redaction |

## Testing

The ordinary verification floor is `tests/smoke/`, covering import and CLI startup, configuration validation, database migration, representative OpenAI and Anthropic requests, canonical streaming completion, premature EOF, and request-local failure recovery. Use focused unit, integration, or contract tests for changed behavior. Performance and live checks are optional manual diagnostics; they are not part of ordinary CI.

| Directory | Scope | Count | CI? |
|-----------|-------|-------|-----|
| `tests/smoke/` | Import, config, DB, one request per protocol | 10 | Yes |
| `tests/unit/` | Module-level behavior, isolated | 249 | No (changed code) |
| `tests/integration/` | Cross-component, mocked upstream | 72 + 46 reload | No (changed code) |
| `tests/contract/` | Wire-level protocol preservation | 3 | No (changed code) |
| `tests/perf/` | Hot-path microbenchmarks | 4 | Manual |
| `tests/live/` | Real-network enrichment | 1 | Manual (opt-in) |

## Further Reading

- [architecture/README.md](README.md) — Authoritative design index and runtime shape
- `.opencode/skills/architecture/SKILL.md` — Architecture principles and invariants
- `.opencode/skills/deployment/SKILL.md` — Deployment and operations
- `.opencode/skills/development/SKILL.md` — Development workflow
- [AGENTS.md](../AGENTS.md) — Agent instructions, pre-commit checks, gotchas
