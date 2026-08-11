# EggPool Architecture Overview

EggPool is a lightweight, LAN-hosted proxy that aggregates multiple LLM provider accounts behind one OpenAI/Anthropic-compatible endpoint. Designed for Raspberry Pi and SBC deployments, it provides protocol transcoding, quota-aware routing, and a self-updating dashboard.

## System Architecture at a Glance

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Client (OpenAI/Anthropic SDK)                │
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
│  │  quotas | pings | backoffs | model_info_* | compression_*    │  │
│  └──────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
                                │
                    ┌───────────▼───────────┐
                    │   Upstream Providers   │
                    │   (27+ providers)      │
                    └───────────────────────┘
```

## Request Lifecycle (Simplified)

1. **Endpoint** — Client hits `/v1/chat/completions` (OpenAI) or `/v1/messages` (Anthropic)
2. **Routing** — `Router` selects best account via priority tiers + `QuotaFairScorer`
3. **Persistence** — Attempt + routing decision written to SQLite before dispatch
4. **Provider Contract** — `compose_provider_url()` + `build_upstream_headers()` from provider config
5. **Transcoding** — Body translated if client protocol ≠ upstream protocol
6. **Proxy** — Request sent via `ProviderClientPool` httpx client
7. **Streaming** — One bounded `SSEDecoder` frames each upstream stream; shared frames are observed and transcoded if needed
8. **Finalization** — Usage recorded, reservations released, health updated

## Module Overview

Each module below is a discrete component of the system. For implementation details, follow the deep-dive links.

### Core Application

| | |
|---|---|
| **Path** | `src/eggpool/cli.py`, `src/eggpool/cli_full.py`, `src/eggpool/fastcli.py`, `src/eggpool/config.py`, `src/eggpool/errors.py`, `src/eggpool/constants.py`, `src/eggpool/jsonx.py`, `src/eggpool/auth.py` |
| **Deep Dive** | [deep-dive-core.md](deep-dive-core.md) |

The CLI entry point is a two-phase bootstrap: `cli.py` (73 lines) first tries `fastcli.maybe_run_fast_command()` for `croncheck`/`ensure-running` without importing Click, keeping the Raspberry Pi watchdog path lightweight. Everything else falls through to `cli_full.py` (~2135 lines of Click commands). Configuration lives in `config.toml` + `.env`; `config.py` ensures a config file exists by copying the bundled template. `errors.py` defines the typed exception hierarchy (`AggregatorError` → 20+ subclasses). `jsonx.py` abstracts over `orjson` (preferred) and stdlib `json` for hot-path serialization. `auth.py` provides constant-time API key verification via `hmac.compare_digest`.

### Request Lifecycle

| | |
|---|---|
| **Path** | `src/eggpool/request/`, `src/eggpool/api/`, `src/eggpool/proxy/` |
| **Deep Dive** | [deep-dive-request-lifecycle.md](deep-dive-request-lifecycle.md) |

The request lifecycle is orchestrated by `RequestCoordinator` in `request/coordinator.py`. API handlers (`api/chat_completions.py` for OpenAI, `api/messages.py` for Anthropic) parse the request and call `handle_proxy_request()` — a shared 1022-line pipeline in `api/proxy_request.py` that handles auth, body parsing, model/provider resolution, capability checks, context-limit enforcement, transcoding preflight, segmentation, compression, dispatch, and response handling. The coordinator persists each attempt to SQLite before dispatch, manages retry with failover, and ensures every terminal outcome is owned by exactly one `RequestFinalizationJob`. Dispatch retries are limited to typed HTTPX transport failures before an explicit response-handoff fact; local preparation and response-adaptation faults are isolated as local errors. Non-streaming adaptation precedes durable success, while streaming responses flow through `proxy/sse.py` (bounded SSE decoder) and `proxy/sse_observer.py` (diagnostic observation). The `request/` subpackage also contains `ProviderBoundRequest` (single-decode lifecycle for provider payloads) and `RequestFinalizationSupervisor` (shutdown drain).

### Protocol Transcoding

| | |
|---|---|
| **Path** | `src/eggpool/transcoder/` |
| **Deep Dive** | [deep-dive-transcoder.md](deep-dive-transcoder.md) |

Transparent request/response format conversion between OpenAI and Anthropic protocols. When a client sends Anthropic Messages but the routed provider only supports OpenAI Chat Completions (or vice versa), the transcoder translates both the request body and the streaming response. `BodyTranscoder` Protocol (`protocol.py`) defines the interface; `OpenAIToAnthropic` and `AnthropicToOpenAI` are the concrete implementations. Streaming translation (`streaming.py`) handles SSE frame-by-frame with synchronous `translate_frame()` and `finish()`. The transcoder also handles tool-use translation (Phase 6.1), thinking/reasoning control normalization (Phase 7), and configurable reasoning field names (Phase 8). A compression sub-package (`transcoder/compression/`) implements the cache-preserving request-shaping stack: segmentation, observe-mode analysis, safe-mode suffix compression, policy overrides, synthetic cache controls, and threshold tuning. The `usage` property returns a default; finalization reads usage from the coordinator's observer.

### Routing & Quota

| | |
|---|---|
| **Path** | `src/eggpool/routing/`, `src/eggpool/quota/` |
| **Deep Dive** | [deep-dive-routing.md](deep-dive-routing.md) |

Quota-aware account routing with tiered priority, fairness rotation, and eligibility filtering. `Router` (`routing/router.py`) groups eligible accounts into priority tiers (highest `routing_priority` first), scores within a tier via `QuotaFairScorer` (`quota/scorer.py`), and applies a `FairnessRotor` within epsilon-bands of equally-scored accounts. Scoring is load-based (request count + token count utilization), never cost-based. `routing/eligibility.py` implements a multi-gate eligibility chain (enabled, credentials, health, provider match, protocol support, circuit breaker, catalog freshness, thinking capability, quarantine, quota capacity). `quota/estimation.py` tracks per-account usage with 5h/7d/30d rolling windows and EWMA cost estimation. Scope modes include `provider_model_protocol`, `provider_model`, and `priority_model_protocol`.

### Provider Architecture

| | |
|---|---|
| **Path** | `src/eggpool/providers/`, `src/eggpool/accounts/` |
| **Deep Dive** | [deep-dive-providers.md](deep-dive-providers.md) |

Supports 27+ upstream providers (OpenCode Go, OpenAI, Anthropic, Groq, DeepInfra, Gemini, xAI, Mistral, SiliconFlow, DeepSeek, Together, Fireworks, OpenRouter, Alibaba, MiniMax, and more). `ProviderClientPool` (`providers/client_pool.py`) manages per-provider `httpx.AsyncClient` instances with independent connection pools. `providers/contract.py` centralizes URL composition (`compose_provider_url()`) and auth header construction (`build_auth_headers()`). `providers/outbound.py` manages a shared HTTP client for non-provider network paths. Models are exposed with provider-suffixed IDs (`model-id/provider-id`); `parse_model_provider()` in `routing/provider.py` is the canonical suffix parser. `accounts/registry.py` maintains the in-memory account registry; `accounts/state.py` tracks per-account runtime state (active requests, health, quota).

### Database Layer

| | |
|---|---|
| **Path** | `src/eggpool/db/` |
| **Deep Dive** | [deep-dive-database.md](deep-dive-database.md) |

Async SQLite with WAL mode, single-connection serialization, and 53 numbered schema migrations. `db/connection.py` wraps `aiosqlite` with `BEGIN IMMEDIATE` transactions and explicit one-task ownership; inherited child tasks fail before SQL. Runtime invalidation closes admission and exits the worker for systemd restart. Startup integrity checking and crash reconciliation are the only durable recovery boundary. All DML runs inside `async with db.transaction():`. `db/repositories.py` contains `AccountRepository`, `ProviderRepository`, `RequestRepository`, and others. `db/dispatch_repository.py` handles dispatch persistence. `db/rollup_repository.py` manages usage rollups. Schema lives in `db/schema/` with 53 numbered SQL files and a `checksums.json` manifest.

### Runtime & Process Management

| | |
|---|---|
| **Path** | `src/eggpool/runtime_manager.py`, `src/eggpool/runtime.py`, `src/eggpool/reload_transaction.py`, `src/eggpool/generation_factory.py` |
| **Deep Dive** | [deep-dive-runtime.md](deep-dive-runtime.md) |

Process model: supervisor + 1 Granian worker (`workers=1`), PID file owned by supervisor, default `runtime_threads=1` (single event-loop thread is canonical) and `database.worker_threads=1`. The ordinary profile binds to loopback and uses low-wear analytics; LAN binding, model-info, traces, readiness, backups, DNS caching, detailed spans, and the update checker are explicit opt-ins. `RuntimeManager` owns active/retiring generation slots — immutable frozen-dataclass snapshots (`RuntimeGeneration`) swapped atomically. Request-path code obtains a `GenerationLease`; a generation swap never interrupts an in-flight request. Lease acquisition is fail-closed: `RuntimeManagerLeaseExhaustedError` → HTTP 503. Staged reload: `stage()` → `commit()`/`rollback()` → `finalize_retirement()`. `ReloadTransaction` (`reload_transaction.py`) is a monotonic state machine with atomic commit semantics across SQLite and runtime publication. `runtime.py` handles PID file lifecycle, process start/stop/restart, and health probing. `runtime_paths.py` (stdlib-only) resolves PID and log paths.

### Health Management

| | |
|---|---|
| **Path** | `src/eggpool/health/`, `src/eggpool/failure/` |
| **Deep Dive** | [deep-dive-health.md](deep-dive-health.md) |

Circuit breaker-based health tracking for accounts and models. `HealthManager` (`health/health_manager.py`) manages per-account health with consecutive failure counting, bounded 1,800-second nonterminal cooldowns, circuit breaker probe-slot acquisition, operator-initiated disable/enable, and scoped model quarantine. `health/circuit_breaker.py` implements per-account circuit breaker logic with total probe convergence. `health/writable_probe.py` provides `DatabaseWritableProbe` — a real SQLite write probe cached for `/readyz` (never performs a write on the request path). `failure/` implements typed failure effects: `classify_failure_effects()` returns the canonical retry/effects decision, and `EffectsApplier` applies it with attempt-owned component progress. Authentication and authoritative model withdrawal remain terminal; runtime model absence is account/model scoped and expires or clears on matching recovery. `health/backoff.py` implements bounded per-account backoff logic and defensive restart hydration.

### Background Tasks

| | |
|---|---|
| **Path** | `src/eggpool/background/` |
| **Deep Dive** | [deep-dive-background.md](deep-dive-background.md) |

`TaskSupervisor` manages supervised background tasks with restart, backoff, and periodic scheduling. Tasks run in daemon mode (long-running coroutine with restart + exponential backoff) or periodic mode (supervisor-owned scheduling with tick factory, initial delay, timeout). Process-owned tasks survive generation swaps; generation-leased tasks retire with their generation. The ordinary profile registers checkpoint and low-wear metrics flush; update_checker and automatic_backup are opt-in process-owned tasks. `background/backup.py` handles automatic backup scheduling. `background/cleanup.py` reconciles expired reservations. `background/maintenance.py` implements periodic DB maintenance. `apply_spec_diff()` provides atomic task schedule swap during reload.

### Dashboard & Stats

| | |
|---|---|
| **Path** | `src/eggpool/dashboard/`, `src/eggpool/stats/`, `src/eggpool/api/stats.py` |
| **Deep Dive** | [deep-dive-dashboard.md](deep-dive-dashboard.md) |

Self-updating server-rendered HTML dashboard with 50+ themes. `dashboard/routes.py` registers page routes (overview, cache, runtime, etc.) and JSON API endpoints. `dashboard/render.py` handles HTML rendering with `CacheAdvancedState` controlling disclosure visibility. `dashboard/static/` contains bundled CSS, JS, and favicon. `dashboard/themes/` holds bundled `.toml` theme files. The stats layer (`stats/service.py`, `stats/queries.py`) provides SQL query functions for timeseries, segmentation, cache metrics, transcoding stats, and dashboard explanations. JSON API endpoints under `/api/stats/` expose summary, accounts, models, timeseries, errors, latency, pings, routing, operational, and pending-health data. The dashboard is auth-gated separately from the proxy API.

### Model Info Sidecar

| | |
|---|---|
| **Path** | `src/eggpool/model_info/` |
| **Deep Dive** | [deep-dive-model-info.md](deep-dive-model-info.md) |

Multi-source model metadata enrichment. `ModelInfoService` (`model_info/service.py`) orchestrates periodic refresh from external sources (OpenRouter, Hugging Face, Artificial Analysis, provider catalog). `model_info/sources/` contains adapter implementations for each source. `model_info/matching.py` implements tiered matching for model lookup. `model_info/identity.py` handles model identity resolution. `model_info/normalization.py` normalizes metadata across sources. `model_info/dedup.py` deduplicates entries. `model_info/repository.py` persists metadata to SQLite. `model_info/scheduler.py` manages refresh scheduling. Enriched metadata powers model display, pricing, and capability detection.

### External Integrations

| | |
|---|---|
| **Path** | `src/eggpool/integrations/` |
| **Deep Dive** | [deep-dive-integrations.md](deep-dive-integrations.md) |

Configuration snippet generators for 11 external tools: OpenCode, Claude Code, Aider, Codex, Qwen Code, Kilo, Continue, Cline, Roo Code, Goose, and OpenHands. `integrations/common.py` owns configsetup context construction, catalog-backed default model resolution, and format-safe scalar/key rendering helpers. Each integration module (e.g., `integrations/opencode.py`, `integrations/aider.py`) generates the tool-specific config format. Invoked via `eggpool configsetup <target>` CLI commands.

### Security

| | |
|---|---|
| **Path** | `src/eggpool/security/`, `src/eggpool/auth.py` |
| **Deep Dive** | [deep-dive-security.md](deep-dive-security.md) |

Header redaction middleware (`security/redaction.py`) strips configured sensitive headers from upstream responses. API key auth (`auth.py`) uses constant-time `hmac.compare_digest` comparison, accepting `Authorization: Bearer <key>` or `X-API-Key` header. The bearer-prefix guard rejects API keys that begin with `Bearer` for providers configured with `auth.mode = "bearer"` (EggPool adds the scheme automatically). The middleware stack in `app.py` includes body-limit enforcement (10 MB max), header redaction, CORS, and trusted-host filtering.

### Cache & Compression

| | |
|---|---|
| **Path** | `src/eggpool/transcoder/compression/`, `src/eggpool/proxy/normalized_usage.py`, `src/eggpool/transcoder/cache_stability.py`, `src/eggpool/transcoder/cache_synthesis.py` |
| **Deep Dive** | [deep-dive-cache-compression.md](deep-dive-cache-compression.md) |

Cache-preserving request-shaping stack spanning 10 phases: cache reporting (Phase 1), canonical request segmentation (Phase 2), transcoder cache stability (Phase 3), observe-mode compression accounting (Phase 4), safe-mode suffix compression (Phase 5), compression policy overrides (Phase 6), dashboard/runtime views (Phase 7), routing guardrails (Phase 8), synthetic cache controls (Phase 9), and closed-loop threshold tuning (Phase 10). The stack is observational by default — no request body, route, or scoring is altered unless the operator explicitly opts in. `transcoder/compression/analyzer.py` analyzes compression opportunities; `transcoder/compression/apply.py` applies safe-mode transforms with fail-closed stable-prefix verification. `proxy/normalized_usage.py` extracts cache counters from provider responses. `transcoder/cache_synthesis.py` annotates stable-prefix containers with synthetic `cache_control` hints. `transcoder/cache_stability.py` tracks cache boundary events during transcoding. Routing is hardcoded to never consume cache/compression fields.

### Failure Effects & Quarantine

| | |
|---|---|
| **Path** | `src/eggpool/failure/` |
| **Deep Dive** | [deep-dive-health.md](deep-dive-health.md) |

Typed attempt-scoped failure decisions and bounded model quarantine. `failure/classifier.py` (`classify_failure_effects()`) is the only production decision table for retry and shared-state effects. `failure/effects.py` defines the immutable decision, including retry scope, provider attribution, circuit transition, and probe convergence. `failure/applier.py` applies component progress owned by the retained `(proxy_request_id, attempt_id)` cleanup/finalization lifecycle; its compatibility cache is bounded and is not the production idempotency boundary. `HealthManager.record_failure()` owns the circuit-failure transition, so one attempt cannot incur a duplicate circuit penalty. `failure/quarantine.py` implements `ModelQuarantine` — a bounded state machine with corroboration before terminal withdrawal. `failure/signal.py` and `failure/signal_extract.py` extract bounded failure signals from upstream responses. `failure/observation.py` records normalized failure facts without raw bodies, credentials, or tracebacks.

### Model Catalog

| | |
|---|---|
| **Path** | `src/eggpool/catalog/` |
| **Deep Dive** | [deep-dive-catalog.md](deep-dive-catalog.md) |

Model discovery, normalization, pricing, protocol resolution, and capability detection. `catalog/service.py` (~1678 lines) orchestrates periodic refresh from provider `/v1/models` endpoints. `catalog/fetcher.py` calls each provider's models endpoint; `catalog/normalizer.py` normalizes heterogeneous response shapes into a canonical model list. `catalog/protocols.py` resolves per-model protocol (`openai`/`anthropic`) via a 6-tier resolution chain (TOML override → upstream metadata → exact known mapping → family prefix → persisted → error). `catalog/capabilities.py` tracks model capabilities (thinking support, budget bounds, `CapabilityStatus`). `catalog/pricing.py` and `catalog/pricing_resolver.py` handle cost estimation with alias resolution (`catalog/pricing_aliases.py`). `catalog/cache.py` maintains the in-memory model catalog cache with `AccountCatalogOutcome` tracking. `catalog/limits.py` extracts upstream context-window limits. `catalog/models_dev.py` merges metadata from external `models.dev` sources and derives OpenCode Go supported efforts. `catalog/catalog_resolvers.py` implements OpenRouter and pricing resolver pipelines.

### Control Plane

| | |
|---|---|
| **Path** | `src/eggpool/control/` |
| **Deep Dive** | [deep-dive-control.md](deep-dive-control.md) |

Unix-domain socket control server for live config rehash. `control/server.py` implements a single-shot newline-delimited JSON protocol (v1) on a UDS (`~/.local/state/eggpool/eggpool.sock`) for `eggpool rehash`. Socket mode `0o600` (owner-only). `control/client.py` connects from the CLI to issue reload commands. `control/reload_manager.py` orchestrates the staged reload: `stage()` → `commit()`/`rollback()` → `finalize_retirement()`. `control/accepted_finalization.py` tracks accepted finalization invariants during reload. The control plane is the only path for live config changes without process restart.

### Data Models

| | |
|---|---|
| **Path** | `src/eggpool/models/` |
| **Deep Dive** | [deep-dive-models.md](deep-dive-models.md) |

Pydantic v2 models for configuration, domain objects, API payloads, and database rows. `models/config.py` (~1571 lines) defines `AppConfig` and all nested config models (`ProviderConfig`, `AccountConfig`, `RoutingConfig`, `TranscoderPolicy`, `CompressionConfig`, etc.) with field validators and TOML parsing. `models/api.py` defines OpenAI and Anthropic request/response models. `models/database.py` defines SQLite row models. `models/domain.py` defines domain objects shared across modules. These models are the single source of truth for schema validation and serialization boundaries.

### Observability

| | |
|---|---|
| **Path** | `src/eggpool/observability/` |
| **Deep Dive** | [deep-dive-observability.md](deep-dive-observability.md) |

Routing trace persistence for debugging and dashboard drill-down. `observability/routing_trace_writer.py` implements a process-owned, single-drain-task writer that collects immutable `RoutingTraceEvent` objects via a non-blocking `submit()` and persists them in micro-batches via `RoutingDecisionRepository`. Bounded queue (`collections.deque(maxlen=queue_capacity)`) drops newest events when full. Thread-safe submission via `threading.Lock`. Silent failures — every exception is swallowed and its counter incremented. Routing traces are opt-in and powered by the `[routing].trace_enabled` config flag.

### Retry Classification

| | |
|---|---|
| **Path** | `src/eggpool/retry/` |
| **Deep Dive** | [deep-dive-retry.md](deep-dive-retry.md) |

Upstream failure classification and retry decision logic. `retry/classification.py` defines `RetryCategory` (NEVER, BAD_REQUEST, AUTH_FAILURE, QUOTA_EXCEEDED, TEMPORARY, TRANSIENT, FATAL, MODEL_UNAVAILABLE) and `classify_retry()` which maps HTTP status codes and error patterns to retry categories with optional `retry_after` durations. Integrates with `failure/classifier.py` for typed failure effects. The retry module is consumed by `RequestCoordinator` to determine whether a failed attempt should retry, which accounts to exclude, and how long to backoff.

### Metrics & Telemetry

| | |
|---|---|
| **Path** | `src/eggpool/metrics/`, `src/eggpool/event_loop_lag.py`, `src/eggpool/runtime_metrics.py`, `src/eggpool/runtime_dispatch.py` |
| **Deep Dive** | [deep-dive-metrics.md](deep-dive-metrics.md) |

Structured observability across three subsystems. `metrics/buffer.py` implements a low-wear metrics buffer with periodic flush to SQLite. `metrics/thinking.py` (`ThinkingMetricsCounter`) tracks thinking/reasoning decision outcomes with low-cardinality labels (protocol, decision, capability_status, provider_id). `metrics/failure_effects.py` records normalized failure effect counters. `event_loop_lag.py` implements a lightweight event-loop lag monitor for SBC deployments — fixed-size sample buffer, single background task, no per-request allocations; exposes `EventLoopLagSnapshot` with p50/p50/p95/avg/min/max. `runtime_metrics.py` gathers process topology, memory, background task state, database health, OS load average, and dispatch-overhead distribution. `runtime_dispatch.py` implements `DispatchOverheadRecorder` (always-on coarse coordinator slice) and `LocalPreUpstreamRecorder` (detailed span sampling) using monotonic clocks.

### Lifecycle Management

| | |
|---|---|
| **Path** | `src/eggpool/lifecycle/` |
| **Deep Dive** | [deep-dive-lifecycle.md](deep-dive-lifecycle.md) |

Backup, restore, and uninstall orchestration. `lifecycle/backup.py` (~564 lines) creates timestamped `.zip` archives containing `config.toml`, `.env`, and `usage.sqlite3` with uncompressed storage (contents are small). `lifecycle/uninstall.py` (~760 lines) reverses installation by detecting the installer method (`pipx`, `uv tool`, source, manual) and scrubbing PATH entries added by the install script. Both modules are invoked by CLI commands (`eggpool backup`, `eggpool uninstall`) and designed for testability without terminal interaction.

### Config Reload Policy

| | |
|---|---|
| **Path** | `src/eggpool/config_reload_policy.py` |
| **Deep Dive** | [deep-dive-core.md](deep-dive-core.md) |

Typed configuration diff and reload policy. Classifies every `AppConfig` field as `LIVE` (applied via staged generation swap), `RESTART_REQUIRED` (consumed by constructor-owned state), or `IGNORED` (audit-only). The default for unclassified fields is `RESTART_REQUIRED` — fail-closed against partial live reloads. `config_reload_policy.py` (~836 lines) is the single reviewable map of fields that staged generation swaps can apply live. Used by `eggpool rehash` and the control plane to determine which changes take effect immediately vs. requiring a restart.

## Component Index

| # | Component | Deep Dive |
|---|-----------|-----------|
| 1 | **Core Application** — CLI entry point, config, errors, constants, JSON backend | [deep-dive-core.md](deep-dive-core.md) |
| 2 | **Request Lifecycle** — Coordinator, API endpoints, proxy, finalization | [deep-dive-request-lifecycle.md](deep-dive-request-lifecycle.md) |
| 3 | **Protocol Transcoding** — OpenAI↔Anthropic body + streaming translation | [deep-dive-transcoder.md](deep-dive-transcoder.md) |
| 4 | **Routing & Quota** — Priority tiers, fairness rotor, quota estimation | [deep-dive-routing.md](deep-dive-routing.md) |
| 5 | **Provider Architecture** — Client pool, contracts, auth, 27+ providers | [deep-dive-providers.md](deep-dive-providers.md) |
| 6 | **Database Layer** — SQLite WAL, migrations, repositories | [deep-dive-database.md](deep-dive-database.md) |
| 7 | **Runtime & Process Management** — Generations, supervisor, Granian worker | [deep-dive-runtime.md](deep-dive-runtime.md) |
| 8 | **Dashboard & Stats** — Server-rendered HTML, JSON API, stats service | [deep-dive-dashboard.md](deep-dive-dashboard.md) |
| 9 | **Background Tasks** — TaskSupervisor, cleanup, backups | [deep-dive-background.md](deep-dive-background.md) |
| 10 | **Health Management** — Circuit breaker, cooldown, failure effects, quarantine | [deep-dive-health.md](deep-dive-health.md) |
| 11 | **Model Catalog** — Discovery, normalization, pricing, protocol resolution, capabilities | [deep-dive-catalog.md](deep-dive-catalog.md) |
| 12 | **Model Info Sidecar** — Multi-source metadata enrichment | [deep-dive-model-info.md](deep-dive-model-info.md) |
| 13 | **Control Plane** — Unix socket, live reload, staged generation swap | [deep-dive-control.md](deep-dive-control.md) |
| 14 | **Data Models** — Pydantic config, domain, API, database models | [deep-dive-models.md](deep-dive-models.md) |
| 15 | **External Integrations** — OpenCode, Claude Code, Aider, Codex, 8+ tools | [deep-dive-integrations.md](deep-dive-integrations.md) |
| 16 | **Security** — Header redaction, API key auth, constant-time compare | [deep-dive-security.md](deep-dive-security.md) |
| 17 | **Cache & Compression** — Observability, safe compression, synthetic cache, tuning | [deep-dive-cache-compression.md](deep-dive-cache-compression.md) |
| 18 | **Observability** — Routing trace writer, structured diagnostics | [deep-dive-observability.md](deep-dive-observability.md) |
| 19 | **Retry Classification** — Error categorization, backoff, retry decisions | [deep-dive-retry.md](deep-dive-retry.md) |
| 20 | **Metrics & Telemetry** — Thinking counters, event-loop lag, dispatch overhead | [deep-dive-metrics.md](deep-dive-metrics.md) |
| 21 | **Lifecycle Management** — Backup, restore, uninstall orchestration | [deep-dive-lifecycle.md](deep-dive-lifecycle.md) |
| 22 | **Deployment & Operations** — Systemd, scripts, install, operational tools | [deep-dive-deployment.md](deep-dive-deployment.md) |

## Key Architecture Patterns

### Runtime Generations
Immutable frozen-dataclass snapshots swapped atomically via `RuntimeManager`. Request-path code obtains a `GenerationLease` — a generation swap never interrupts an in-flight request.

### Process Model
Supervisor + 1 Granian worker (`workers=1`). PID file owned by supervisor. Default `runtime_threads=1` (single event-loop thread is canonical).

### Database Invariants
SQLite WAL with single-connection serialization. All DML runs inside `async with db.transaction():`. 51+ schema migrations tracked by checksums.

### JSON Backend
`eggpool.jsonx` abstracts over `orjson` (preferred) and stdlib `json`. Hot-path serialization, SSE frame helpers, and request body parsing all route through this layer.

### Error Hierarchy
`AggregatorError` → `UpstreamError` → specific subclasses. `CapabilityError` for thinking/reasoning mismatches. `TranscodeLossError` for loss-policy rejects.

### Fast-Path CLI
`src/eggpool/fastcli.py` handles `croncheck` and `ensure-running` without importing Click — stays lightweight for Raspberry Pi watchdog cron jobs.

### Provider Payload Lifecycle
`ProviderBoundRequest` is the single provider-payload authority after client parsing. Generation-aware path-level copy-on-write handles narrow mutations, `adopt_provider_payload()` accepts EggPool-owned transformed graphs, conservative setters protect unknown graphs, and one final serialization cache freezes the request before dispatch. `ProxyRequestContext` retains original client bytes separately and carries the provider-bound object without a second body mirror.

### Request Finalization
Every live terminal outcome is owned by one kind-qualified command in the generation-owned `RequestFinalizationSupervisor`: selected request finalization, failed-attempt cleanup, or post-commit claim compensation. Each accepted command retains one terminal reference on its generation until durable and required runtime convergence; compatible duplicates and retries reuse that reference. `FinalizationData.downstream_started` records whether ASGI `http.response.start` was sent or attempted: it is false before response handoff and true inside the active stream, including a zero-byte stream. Payload `bytes_emitted` is accounting only. Runtime usage, health, and account-runtime obligations belong to the lease and still converge when the durable request row was already terminal. Retryable runtime cleanup resumes component-by-component from `RUNTIME_RELEASE_PENDING`; the supervisor enforces one absolute retry-age deadline and exposes its bounded snapshot as `finalization_supervisor` in `/api/stats/runtime`, while `runtime_manager.finalization_ownership` reports bounded cross-generation ownership facts. A released lease projects a non-retryable result without stale runtime-cleanup detail. Startup crash reconciliation repairs only durable work left by a prior process; normal runtime requests are never reclaimed by age.

## Directory Structure

```
src/eggpool/
├── accounts/          # Account registry and runtime state
├── api/               # API endpoint handlers (chat completions, messages, stats)
├── background/        # TaskSupervisor, cleanup, periodic tasks
├── catalog/           # Model catalog, pricing, protocols, fetcher, normalizer
├── control/           # Control plane (Unix socket, live reload)
├── dashboard/         # Server-rendered HTML dashboard (50+ themes)
├── db/                # SQLite connection, migrations, repositories, schema
├── failure/           # Failure effects and quarantine
├── health/            # Circuit breaker and health tracking
├── integrations/      # External tool configuration generation (11 tools)
├── lifecycle/         # Backup and uninstall orchestration
├── metrics/           # Metrics buffering, thinking observability, failure counters
├── model_info/        # Model metadata sidecar (multi-source enrichment)
├── models/            # Pydantic config, domain, API, database models
├── observability/     # Routing trace writer
├── providers/         # Provider client pool, contracts, auth
├── proxy/             # Transparent proxy, SSE observer, usage normalization
├── quota/             # Quota estimation, reservations, scoring
├── request/           # RequestCoordinator, finalizers, dispatch, body reader
├── retry/             # Error classification and retry decisions
├── routing/           # Quota-aware routing, eligibility, fairness, provider parsing
├── security/          # Header redaction, security utilities
├── stats/             # Statistics queries and service
├── transcoder/        # Protocol transcoding (OpenAI ↔ Anthropic)
│   └── compression/   # Safe compression, policy, tuning
├── _share/            # Bundled config examples for pipx
├── auth.py            # Local API key auth (constant-time)
├── cli.py             # CLI bootstrap (tiny)
├── cli_full.py        # Click CLI commands (heavy imports)
├── cli_rehash_format.py  # Rehash output formatting
├── cli_rehash_helper.py  # Rehash CLI helpers
├── config.py          # Config file helpers
├── config_reload_policy.py  # Typed config diff and reload policy
├── config_utils.py    # Config utility functions
├── config_validation.py  # Reusable validation contract
├── constants.py       # Project-wide constants
├── cost_recompute.py  # Cost recompute CLI command
├── cost_repair.py     # Cost repair CLI command
├── deploy_user.py     # Deploy user and path resolution
├── errors.py          # Exception hierarchy (20+ subclasses)
├── event_loop_lag.py  # Lightweight event-loop lag monitor
├── fastcli.py         # Fast-path CLI (stdlib-only, croncheck/ensure-running)
├── generation_factory.py  # Runtime generation construction
├── jsonx.py           # JSON backend abstraction (orjson/stdlib)
├── logging.py         # Structured logging setup
├── onboard.py         # Interactive onboarding script
├── reload_diagnostics.py  # Reload result categories and counters
├── reload_transaction.py  # Monotonic state machine for atomic reload
├── runtime.py         # Process management (restart, stop, PID lifecycle)
├── runtime_dispatch.py  # Dispatch timing recorders
├── runtime_manager.py # Runtime generation ownership
├── runtime_metrics.py # Runtime/ops metrics
├── runtime_paths.py   # PID/log path resolution (stdlib-only)
├── runtime_task_inventory.py  # Task inventory
├── runtime_tasks.py   # Runtime task management
├── toml_edit.py       # Formatting-preserving TOML edits
├── update_checker.py  # PyPI update checker
├── py.typed           # PEP 561 type marker
├── __init__.py        # Package init, version
└── __main__.py        # python -m eggpool entry

tests/
├── unit/              # Focused module-level behavior
├── integration/       # Cross-component request and lifecycle behavior
├── contract/          # Provider and transcoder contracts
├── perf/              # Optional local performance checks
├── live/              # Opt-in live external-source tests
├── smoke/             # Small canonical CI correctness floor
├── soak/              # Long-running stability checks
├── helpers/           # Shared test utilities
└── fixtures/          # Test fixtures (cache_compression, streaming, etc.)

scripts/               # Operational, diagnostic, installer scripts
deploy/                # Systemd, logrotate, cron artifacts
docs/                  # Operator documentation (20+ topics)
plans/                 # 90+ design/implementation plans
config-examples/       # Agent config examples
architecture/          # Architecture docs and deep dives
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
| `[compression]` | Request shaping (observe/safe modes, transforms) |
| `[cache]` | Synthetic cache controls |
| `[model_info]` | Source enablement, TTLs, overrides |
| `[dashboard]` | Theme, auth policy |
| `[metrics]` | Buffering, flush modes |
| `[backup]` | Automatic backup schedule |
| `[security]` | Header redaction |

## Testing

The ordinary verification floor is `tests/smoke/`, covering import and CLI startup, configuration validation, database migration, representative OpenAI and Anthropic requests, canonical streaming completion, premature EOF, and request-local failure recovery. Use focused unit, integration, or contract tests for changed behavior. Performance and live checks are optional manual diagnostics; they are not part of ordinary CI.

## Further Reading

- `architecture/README.md` — Detailed design (cache/compression phases, routing guardrails, etc.)
- `.opencode/skills/architecture/SKILL.md` — Architecture principles and invariants
- `.opencode/skills/deployment/SKILL.md` — Deployment and operations
- `.opencode/skills/development/SKILL.md` — Development workflow
- `AGENTS.md` — Agent instructions, pre-commit checks, gotchas
