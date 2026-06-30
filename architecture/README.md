# Architecture

High-level design overview for the EggPool aggregator.

## Package Structure

```
src/eggpool/
├── accounts/          # Account registry and runtime state
├── api/               # API endpoint handlers (chat completions, messages, stats)
├── background/        # TaskSupervisor, retention cleanup, periodic tasks
├── catalog/           # Model catalog, pricing, protocols, fetcher, normalizer, limits
├── model_info/        # Model information sidecar: persistent metadata, observations, summaries, source adapters
├── dashboard/         # Self-updating server-rendered HTML dashboard
├── db/                # SQLite connection, migrations, repositories, schema
├── health/            # Circuit breaker and health tracking
├── integrations/      # External tool configuration generation (OpenCode, Claude Code, Aider, Codex, Qwen Code, Kilo, Continue, Cline, Roo Code, Goose, OpenHands)
├── models/            # Pydantic config, domain, API, and database models
├── providers/         # ProviderClientPool, pproxy transport, connect CLI
├── proxy/             # Transparent proxy, SSE observer, usage extraction
├── transcoder/        # Protocol transcoding (OpenAI ↔ Anthropic, body + streaming)
├── quota/             # Quota estimation, reservations, scoring
├── request/           # RequestCoordinator, finalizers, body reader, limit enforcement
├── retry/             # Error classification and failover
├── routing/           # Quota-aware routing, eligibility, provider parsing
├── security/          # Header redaction, security utilities
├── stats/             # Statistics queries and service
├── lifecycle/         # Backup and uninstall orchestration
├── deploy/            # Bundled systemd/logrotate/cron snippets for CLI output
├── _share/            # Bundled config examples and assets for pipx installs
├── auth.py            # Local API key authentication (constant-time)
├── cli.py             # CLI bootstrap entry point (tiny, dispatches fast-path then Click)
├── cli_full.py        # Click CLI commands (heavy imports)
├── fastcli.py         # Fast-path CLI (stdlib-only, croncheck/ensure-running)
├── errors.py          # Exception hierarchy
├── logging.py         # Structured logging setup
├── runtime.py         # Process management (restart, stop, PID lifecycle)
├── runtime_metrics.py # Runtime/ops metrics: process, memory, DB, background tasks, OS load average
├── runtime_dispatch.py # Bounded rolling-window recorder for EggPool-local upstream dispatch overhead
├── runtime_paths.py   # PID file and log path resolution (stdlib-only)
├── update_checker.py  # PyPI update checker (background + CLI)
├── cost_recompute.py  # Cost recompute CLI command
└── constants.py       # Project-wide constants
```

## Request Lifecycle

All data-plane requests flow through `RequestCoordinator`:

1. **Endpoint** (`api/chat_completions.py` or `api/messages.py`) extracts model ID, parses provider suffix
2. **Routing** selects an eligible account via quota-aware scoring (`routing/router.py`)
3. **Attempt** is persisted to SQLite before upstream dispatch
4. **Provider Contract** renders absolute URL (`compose_provider_url()`) and auth headers (`build_upstream_headers()`) from `providers/contract.py`
5. **Protocol Transcoding** (if enabled) translates the request body when the client protocol differs from the upstream protocol
6. **Proxy** sends the request via the provider's `httpx.AsyncClient` from `ProviderClientPool`
6. **Streaming** is handled by `proxy/sse_observer.py` with chunk-level usage extraction
7. **Finalization** records usage, releases reservations, updates health state

All outbound dispatch paths (non-streaming chat, streaming chat, catalog refresh) share the same `compose_provider_url()` rules so a provider cannot list models at one host and dispatch requests to another. The coordinator's `_get_upstream_url()` returns an absolute URL for provider-configured paths, falling back to bare paths only when no provider config is loaded.

Key invariants:
- Requests must be persisted before upstream dispatch
- Pre-body failures can retry; no retry after first downstream byte emitted
- Every retryable failed attempt must reach terminal state before the next attempt
- Each attempt reservation is released exactly once via `AttemptFinalizer`
- The same URL composition rules apply to catalog fetch and chat dispatch
- **Structured observability persistence (migrations 0026-0029)** every `request_attempts` row carries provider/model/protocol/retry_category/latency/bytes/streamed/is_retry_outcome; every routing decision is persisted to `routing_decisions` in the same transaction as the `request_attempts` INSERT; safety-net tasks (`_crash_recovery`, `_finalize_stale_requests_once`, `reconcile_expired_reservations`) record `operational_events` rows inside the same transaction as the durable state mutation; latency is decomposed into `upstream_connect_ms / upstream_read_ms / coordinator_overhead_ms` so the dashboard can distinguish network vs upstream vs eggpool-side bottlenecks
- **Runtime metrics are best-effort and process-local** — the `/api/stats/runtime` endpoint and `eggpool runtime-status` CLI command gather process topology, memory, background task state, database health, OS load average (`os.getloadavg` + normalized per-core), and a bounded rolling-window dispatch-overhead distribution via `DispatchOverheadRecorder` (`src/eggpool/runtime_dispatch.py`); failed probes return `null` rather than raising, `probe_errors` is capped to 16 truncated entries, and the endpoint is always auth-gated even with a public dashboard

## Multi-Provider Architecture

EggPool supports 27+ upstream providers (OpenCode Go, OpenAI, Anthropic, Groq, DeepInfra, Gemini, xAI, Mistral, SiliconFlow, DeepSeek, Together, Fireworks, OpenRouter, Alibaba, MiniMax, and more), each with its own base URL, account pool, supported protocols, and model catalog. See `docs/providers.md` for the full roster.

### MiniMax templates

- **`minimax`** — international host `https://api.minimax.io/anthropic`. Anthropic-compatible transport (key sent as `x-api-key` plus `anthropic-version: 2023-06-01`). Model listing is exclusively live via `/v1/models`; no static seeds are shipped because the provider already accepts the anthropic value produced by the family mapping. The Anthropic model-list normalizer auto-detects MiniMax's hybrid response shape. Default for keys from `minimax.io`.
- **`minimax-cn`** — China host `https://api.minimaxi.com/v1` with the same OpenAI paths as a standard provider. Live verification is required because the China endpoint family has not been confirmed against EggPool's Anthropic-compatible transport.

The stored key must be the raw token; EggPool prepends the configured auth scheme automatically. An optional `[providers.<id>.verify]` block lets the verifier know which model to probe when neither `--openai-model` nor `--anthropic-model` is passed on the CLI.

### Provider Configuration

Providers are configured under `[providers.<id>]` in `config.toml`:

```toml
[providers.opencode-go]
id = "opencode-go"
base_url = "https://opencode.ai/zen/go/v1"
protocols = ["openai", "anthropic"]

[[providers.opencode-go.accounts]]
name = "personal"
api_key_env = "OPENCODE_GO_KEY_1"
```

Legacy flat `[[accounts]]` configs auto-normalize to a default `opencode-go` provider.

### Client Pool

`ProviderClientPool` (`providers/client_pool.py`) manages per-provider `httpx.AsyncClient` instances with independent connection pools, timeouts, and optional per-account proxy support.

### Model ID Format

Models are exposed with provider-suffixed IDs: `model-id/provider-id` (e.g., `claude-sonnet-4/opencode-go`). `parse_model_provider()` in `routing/provider.py` is the canonical suffix parser; `catalog/cache.py` retains a compatibility alias.

### Provider-Specific Paths

Each provider can configure custom upstream paths:
- `openai_path` (default: `/chat/completions`)
- `anthropic_path` (default: `/messages`)
- `models_endpoint` — `[providers.<id>.models_endpoint]` table with `method`, `path`, `query`, `body`, `required`. Use `method = "DISABLED"` for providers that do not expose a live model listing (catalog is then populated from `static_models`).
- `models_method` / `models_path` — legacy scalar fields still accepted; auto-synthesized into a default `models_endpoint` table on parse.

### Provider Contracts

Each provider declares an explicit contract for authentication, URL composition, and model listing via `ProviderAuthConfig`, `ProviderStaticHeaderConfig`, `ProviderModelsEndpointConfig`, and `ProviderVerifyConfig` in `config.toml`.

`src/eggpool/providers/contract.py` centralizes:
- `compose_provider_url()` — absolute URL composition (rejects duplicate `/v1` prefix)
- `build_auth_headers()` — provider-aware auth header construction (`bearer`, `api_key`, `raw_authorization`, `none`)
- `build_static_headers()` — static provider headers from config
- `build_upstream_headers()` — combines auth + static headers

The coordinator calls `_build_upstream_headers()` and `_get_upstream_url()` which use the provider contract when available, falling back to legacy Bearer auth and bare paths respectively.

#### Bearer-prefix guard

`AppConfig.validate_account_credentials()` rejects API keys that begin with the `Bearer` scheme for providers configured with `auth.mode = "bearer"`. EggPool adds the scheme automatically, so a stored `Bearer <token>` would produce `Authorization: Bearer Bearer <token>` upstream and cause 401s. The same guard runs in `scripts/verify_upstream_auth.py` so the operator gets an explicit error before any upstream call. Providers using `auth.mode = "raw_authorization"` are unaffected because they pass the value verbatim.

## Protocol Transcoding

When a client sends a request in one protocol (e.g., Anthropic Messages API)
but the routed provider only supports another (e.g., OpenAI Chat Completions
API), the `transcoder` module translates the request body before dispatch and
the response body (including streaming chunks) after receipt.

**Phase 1 (foundation)** lands the data model, configuration surface, and
helper modules without changing runtime behaviour:
- `TranscoderPolicy` config model (`[transcoder]` section)
- `TranscodeContext` per-request state dataclass
- `upstream_protocol` field on `ProxyRequestContext`
- Mechanical refactor: upstream-side reads in the coordinator use
  `context.upstream_protocol` instead of `context.protocol`
- Routing eligibility accepts a `transcode_eligibility` parameter
- Helper modules: `ids.py` (tool-call ID map), `usage.py` (usage
  canonicalisation), `errors.py` (upstream error envelope parser)

**Phase 2 — Body translation**: text-only, non-streaming request/response
body translation is implemented in `src/eggpool/transcoder/`. The
`BodyTranscoder` Protocol (`protocol.py`) defines the interface;
`OpenAIToAnthropic` and `AnthropicToOpenAI` are the concrete translators.
`select_transcoder()` is the single source of truth for dispatch. The
coordinator pre-translates the request body before dispatch, decodes the
response body on success, and re-renders non-retryable errors in the client
protocol. Loss-of-information warnings are accumulated on
`TranscodeContext.loss_warnings` and logged at request completion.

**Phase 3 — Streaming translation**: SSE stream translation in both
directions for text-only streams. `StreamingTranscoder` implementations
(`OpenAIToAnthropicStreaming`, `AnthropicToOpenAIStreaming`) translate
upstream SSE frames into client-format bytes chunk-by-chunk.
`select_streaming_transcoder()` in `streaming.py` is the dispatch source
of truth. The coordinator's `_build_stream_generator` applies the transcoder
when the client and upstream protocols differ. Same-protocol requests pass
through unchanged. Tool calls, thinking, and routing widening are out of
scope (phases 4–6).

**Phase 4 — Routing eligibility widening**: transcoding is **on by default**. The routing layer widens the candidate set to include accounts whose `provider.protocols` includes the model's native protocol even if it does not include the client protocol. `_validate_endpoint` checks for transcodable routes before raising `ProtocolMismatchError`. The `_resolve_upstream_protocol` method determines which protocol to use upstream based on the largest eligible-account set. `prefer_native = true` (default) keeps native-protocol accounts ranked above transcodable ones via a secondary sort key in `QuotaFairScorer`. The two-pass context-limit check in `api/proxy_request.py` validates both client-side and upstream limits when transcoding is active. The `[transcoder] enabled = false` flag is a deprecated escape hatch that disables all translation and reverts to the pre-default protocol-exact routing.

**Phase 5 — Operator controls and docs**: the default `[transcoder]` config block is documented in `config.example.toml`. `eggpool stats transcoding` reports transcoded request counts and loss-warning summaries. The dashboard `/runtime` page includes a "Transcoding" card showing real-time counters. Structured INFO logs are emitted for every transcoded request and a startup line announces transcoding state. See `docs/transcoding.md` for the full operator guide.

Token counts are mapped between protocol-specific fields (e.g.,
`input_tokens` → `prompt_tokens`, `cache_creation_input_tokens` →
separate cache counters). Controlled by `[transcoder]` config.

## Database

SQLite via aiosqlite with WAL mode. Single-connection serialization via a lock + ContextVar.

### Key Invariants

- Every DML write must run inside `async with db.transaction():`
- `Database.vacuum()` is the only sanctioned path for `VACUUM`
- Readiness probes use `probe_writable()` with owned transactions
- Child tasks cannot inherit transaction ownership

### Schema Migrations

Ordered SQL migrations in `db/schema/` (0001 through 0036). Checksums tracked in `checksums.json`.

### Repositories

| Repository | Purpose |
|------------|---------|
| `AccountRepository` | Account CRUD, config sync |
| `RequestRepository` | Request lifecycle (pending → selected → completed) |
| `ReservationRepository` | Quota reservations with release/reconciliation |
| `AttemptRepository` | Per-request attempt tracking |
| `UsageWindowRepository` | Aggregated cost queries (5h/7d/30d) |
| `PriceSnapshotRepository` | Model price snapshots |
| `ProviderRepository` | Provider CRUD and config sync |
| `PingRepository` | Provider health ping results |

## Quota and Routing

Routing happens in two stages: a *priority grouping* step picks the highest
non-empty tier of providers, then a `QuotaFairScorer` load-balances inside
that tier.

The grouping step partitions eligible `AccountRuntimeState` records by their
provider's `routing_priority` (default `0`, must be `>= 0`). The router
selects the highest-priority tier that contains at least one eligible account;
if every account in that tier becomes unhealthy, exhausted, or fails pre-body,
the request falls through to the next tier. The `QuotaFairScorer` runs
unchanged against the accounts of the chosen tier, balancing across:

- Quota utilization across 5h/7d/30d windows
- In-flight request penalty
- Health penalty for degraded accounts
- Random tie-breaking for near-equal scores

The `weight` field continues to bias scoring inside a single tier. `weight`
orders accounts within a tier; `routing_priority` orders tiers.

Accounts are excluded from routing when:
- Upstream-observed failure (`quota_exhausted`, `rate_limited`, auth, 5xx) is still inside its bounded backoff window (recovers after cooldown)
- Account is explicitly disabled or suspended by the operator
- Model is not supported by the account (catalog/protocol incompatibility)
- Health circuit breaker is open
- `local_quota_mode = "hard_cap"` is enabled AND local estimate exceeds capacity (opt-in legacy behavior; default is `score_only` advisory)

In the default `score_only` mode, local cost and quota estimates influence
routing **priority** only — above-capacity accounts stay eligible. Only
upstream-observed failures, explicit operator disablement, and catalog/
protocol incompatibility can suppress routing.

Upstream-derived backoffs (429, 402, model-unavailable) persist across
restarts in the `account_backoffs` table (`src/eggpool/db/schema/0024_account_backoffs.sql`)
and are rehydrated into the in-memory `HealthManager` at startup.
Local-estimate overage never produces a backoff row.

A single request still picks one upstream account. Failover across priority
tiers happens only through the existing `exclude_accounts` retry path.
When every candidate account has been attempted and exhausted mid-request,
the coordinator raises `UpstreamExhaustedError` (502) — synthetic 503 is
reserved for genuine pre-dispatch unavailability (no enabled accounts,
missing credentials, all explicitly disabled, model unknown).

### Same-Tier Fairness

When multiple accounts share the same `routing_priority`, weight, transcode
status, and have scores within `fairness_epsilon` of the best (default:
`near_tie_epsilon`), they are considered *same-tier peers*. Without fairness
intervention, stable config order or minor score noise can cause severe
routing skew (one account receiving nearly all traffic).

EggPool applies a deterministic round-robin rotor
(``FairnessRotor`` in ``src/eggpool/routing/fairness.py``) to the
*fairness band* — the set of tied peers within a single priority tier.
The rotor maintains an in-memory position counter per fairness key
(provider × model × protocol × priority × client_protocol) and rotates
the candidate list so the first-selected account advances on each
routing decision.

Fairness is controlled by three ``[routing]`` config fields:

- ``fairness_mode``: ``"round_robin"`` (default), ``"random"``, or ``"off"``.
- ``fairness_epsilon``: score proximity threshold; defaults to ``near_tie_epsilon``
  when omitted.
- ``fairness_scope``: rotation group granularity — ``"provider_model_protocol"``
  (default), ``"provider_model"``, or ``"priority_model_protocol"``.

The fairness band is extracted *after* quota scoring and *before* the
coordinator selects the first circuit-breaker-accepted candidate. Priority
tier boundaries remain strict: lower-priority accounts never advance ahead
of higher-priority eligible accounts. Different-weight accounts opt out of
equal-peer rotation; the band requires identical weights within floating-point
tolerance.

Fairness decisions are recorded in ``routing_decisions.score_components_json``
under the ``fairness`` key for operator diagnostics:

```json
{
  "fairness": {
    "mode": "round_robin",
    "applied": true,
    "key": "provider=opencode-go|model=gpt-4|protocol=openai|tier=0",
    "candidate_count": 3,
    "selected_index": 0,
    "selected_account_name": "0002",
    "reason": "ok"
  }
}
```

### Lock scope and publish ordering

The `RequestCoordinator._select_and_persist_attempt()` method holds
`_select_lock` across both the durable transaction (`request_attempts`
+ `routing_decisions` INSERT inside `async with self._db.transaction():`)
AND the runtime publication step (`Router.increment_active_request_count`
+ `QuotaEstimator.add_reservation`). The publication runs AFTER the
transaction commits but BEFORE the lock releases, so a concurrent
selector that enters the lock next observes this attempt's runtime
state. The publish is fast (in-process counter + cache mutation), so
the lock-hold stays tight while still closing the burst-skew race
previously caused by publishing inside the transaction body.

Note: the two contexts are written as explicit nested `async with`
blocks (outer `_select_lock`, inner `_db.transaction()`) — NOT as a
compound `async with self._select_lock, self._db.transaction():`. A
compound form would still exit right-to-left (transaction commits
before the lock releases), so context-exit order alone is not the
invariant. The actual bug was that the runtime publication block lived
INSIDE the transaction body; active-count and reserved-cost state were
therefore published before the transaction committed. The explicit
nested form makes it hard to accidentally place publication inside the
transaction while still keeping publication under `_select_lock`. The
key invariant is block placement (publication must be outside the DB
transaction body but still inside `_select_lock`), not context-exit
order.

The compensation chain (`decrement` → finalize-as-cancelled → release
health slot → set `client_metadata["post_commit_interrupted"]` →
re-raise) wraps the publish step and catches `BaseException` (including
`CancelledError` / `SystemExit` / `KeyboardInterrupt`, re-raised
without swallowing).

### Score components and eligibility diagnostics

Every persisted `routing_decisions` row carries the per-account score
breakdown captured by `QuotaFairScorer` at the moment the coordinator
chose the selected account. Migration `0035` adds the
`score_components_json` column; `RoutingDecisionTrace.to_score_components_json()`
serializes the diagnostic payload (TEXT JSON, defaults to `'{}'` on rows
written by code paths that pre-date the migration). The payload now also
includes per-window `util_5h` / `util_7d` / `util_30d` utilization ratios
(None when the scorer's capacity is unconfigured) and a `tie_break`
summary naming the decisive factor between the chosen account and its
runner-up (`tier`, `quota`, `inflight`, `transcode`, `near_tie`,
`exact_tie`, `no_runner_up`). The same data flows through
`eggpool accounts explain --model <id> [--provider P] [--protocol P]`
and `GET /api/stats/routing/eligibility` for live operator diagnostics.

`Router.explain_account_eligibility(model_id, provider_id, protocol)`
returns one row per registered account with `eligible: bool`, a stable
`reason_code` (`ok`, `disabled`, `auth_failed`, `quota_exhausted`,
`cooldown`, `rate_limited`, `circuit_open`, `wrong_provider`,
`no_protocol`, `protocol_mismatch`, `no_model`, `model_stale`), and a
short `reason_detail` that names the account, its provider, its
configured protocols, the requested model id, and the stale-window
seconds (so the operator can act directly on the diagnosis). The
classification mirrors the live filter chain in
`eggpool.routing.eligibility.get_eligible_accounts` so explanations
match the routing path exactly.

`eggpool accounts explain` opens the database, runs migrations on a
fresh install, and calls `ModelCatalogCache.hydrate_from_db(db)` to
populate the in-memory model / provider / account-support tables from
the durable `models`, `provider_model_metadata`, and `account_models`
rows. The cache is wrapped in a tiny `_CatalogShim` so `Router` can
consume it without booting a full `CatalogService`; output is rendered
with `click.echo` (the previous `rich` table was removed because the
dependency was undeclared).

## Provider Routing Priority and Model Collapse

Two related configuration knobs let operators control how requests for the
same base model fan out across providers and how that model appears in the
catalog.

- **`routing_priority`** — `[providers.<id>].routing_priority` is a non-negative
  integer (default `0`). Higher values are preferred. The field is per-provider,
  not per-account: keys of the same provider share a tier and are
  load-balanced by `QuotaFairScorer`.
- **`collapse_models`** — `[models].collapse_models` is a boolean (default
  `false`). When `false`, the catalog exposes one provider-suffixed entry per
  `(model_id, provider_id)`. When `true`, the same base model collapses to a
  single unsuffixed `model_id` and is routed across every provider that
  supports it.

`collapse_models` and `routing_priority` are independent. Either can change
without re-deriving the other. Both require a service restart.

### Default behavior

With defaults (`collapse_models = false`, `routing_priority = 0`), three
providers that all expose `minimax-m2.7` (`opencode-go`, `minimax`,
`generalcompute`) are surfaced as three distinct suffixed model IDs:
`minimax-m2.7/opencode-go`, `minimax-m2.7/minimax`,
`minimax-m2.7/generalcompute`. Each suffixed ID routes only against its own
provider's accounts, load-balanced within the provider.

### Worked example

A `generalcompute`-first / `minimax`-second / `opencode-go`-last ordering
with three `opencode-go` keys load-balancing inside their tier:

```toml
[models]
# collapse_models = false  # default; emit suffixed IDs

[providers.opencode-go]
routing_priority = 0  # load balance within this tier

[providers.minimax]
routing_priority = 2

[providers.generalcompute]
routing_priority = 3  # tried first
```

A request for `minimax-m2.7/generalcompute` first hits the
`generalcompute` accounts (load balanced inside the tier). If every
`generalcompute` account fails pre-body, the coordinator retries the
`minimax` tier, then the `opencode-go` tier. A request for
`minimax-m2.7/opencode-go` only ever hits `opencode-go` accounts regardless
of priority — priority only orders the eligible account set inside one
suffixed (or unsuffixed) model ID.

### Catalog exposure and CLI surface

- `/v1/models` includes an `eggpool.routing_priority` extension field on
  each suffixed entry.
- `eggpool configsetup opencode` generates suffixed IDs when
  `collapse_models = false` and a single unsuffixed ID per base model when
  `collapse_models = true`.
- `eggpool connect` writes `routing_priority = 0` on every newly created
  provider block and leaves existing blocks untouched, so operators can edit
  one number to rebalance.

## Error Hierarchy

```
AggregatorError (base)
├── ConfigError
├── DatabaseError
├── UpstreamError (status_code attribute)
│   ├── TemporaryUpstreamError
│   ├── TransientUpstreamError
│   ├── AuthenticationError
│   ├── QuotaExhaustedError
│   ├── RateLimitError (retry_after attribute)
│   └── ModelUnavailableError
├── ProxyError
├── ModelNotFoundError (model_id attribute)
├── NoEligibleAccountError
├── CatalogUnavailableError
├── AuthenticationUnavailableError
├── UpstreamExhaustedError
├── AccountSuspendedError
├── RequestTooLargeError
├── ModelInfoSourceFetchError
└── ContextLimitExceededError
```

## Model Information

- **Sidecar subsystem**: `model_info/` package provides persistent model metadata sidecar tables (`model_info_canonical`, `model_info_observations`, `model_info_aliases`, `model_info_source_health`) via migration `0036`
- **Source adapter pattern**: `ModelInfoSource` protocol in `sources/base.py` defines `name`, `priority`, `fetch_all()`, `fetch_one()`; concrete adapters implement this interface
- **Provider-native observations**: `ProviderCatalogSource` reads in-memory `ModelCatalogCache` entries and emits `SourceModelRecord`s; no network I/O
- **OpenRouter metadata source** (phase 3): `OpenRouterModelInfoSource` fetches the OpenRouter `/models` catalog and emits `SourceModelRecord` observations for each entry. TTL-cached per source; uses the shared outbound HTTP client from `OutboundClientManager`. Exact/curated alias matching only (no fuzzy matching). Failures are recorded in source health and never break startup, catalog refresh, or routing
- **Identity resolution**: `model_info/identity.py` (`resolve_openrouter_record()`) matches OpenRouter source model IDs to local model IDs via exact `model_info_aliases` rows, exact source_model_id equality, or exact pricing aliases. No substring or edit-distance matching. Ambiguous matches (multiple aliases) return no match
- **Status classification**: models classified as `sparse_new`, `partial`, `fresh`, etc. based on available metadata (display name, context limit, capabilities)
- **Deterministic summaries**: generated from fields only (no LLM); sparse models explicitly note metadata sparsity
- **Lifecycle wiring**: `ModelInfoService` initialized at startup after catalog load; accepts optional `outbound_client` for external sources. `CatalogService.refresh()` returns `CatalogRefreshResult` with diff information (new/withdrawn models, changed provider keys)
- **Background refresh**: supervised `model_info_refresh` task processes due models via `ModelInfoRefreshScheduler`; reconciliation also runs after successful catalog refreshes. External source catalogs are fetched once per cycle (bulk) and matched to due models via identity resolution
- **Refresh scheduling**: `ModelInfoRefreshScheduler` computes next refresh time based on status, first-seen age, and config TTLs; sparse-new models receive accelerated refresh within a configurable window
- **Source health**: per-source health tracking with cooldown backoff; `record_source_success`/`record_source_error` helpers
- **Write deduplication**: observations deduplicated by `(source, source_model_id, raw_hash)`; canonical rows compared before rewrite
- **Error hierarchy**: `ModelInfoSourceFetchError` (subclasses `AggregatorError`) raised by source adapters on network/HTTP/parse failures; caught by `ModelInfoService` and recorded as source-health errors
- **CLI**: `eggpool modelinfo show/list/refresh` commands for inspection and manual refresh
- **Config**: `[model_info]` section in `config.toml` with TTL controls, refresh intervals, and source enablement (`[model_info.sources.openrouter]` for OpenRouter)
- **JSON API endpoints** (phase 4): `src/eggpool/api/model_info.py` registers four endpoints:
  - `GET /api/model-info` — summary list of all models (status, sparse, summary, sources, timestamps)
  - `GET /api/model-info/{model_id:path}` — per-model detail (limits, modalities, external IDs, provenance, observations, conflicts)
  - `GET /api/model-info/sources` — source health snapshot (redacts secrets and raw error messages)
  - `POST /api/model-info/refresh` — manual refresh (always auth-gated; accepts `?model_id=<id>`, `?source=`, `?force=1`)
  - Registered in `create_app()` under dashboard auth policy when `config.model_info.enabled`
- **`/v1/models` enrichment** (phase 4): `serialize_openai_model()` accepts an optional `model_info` mapping; when present, compact fields are nested under `eggpool["model_info"]` (status, sparse, summary, sources, last_refreshed_at). Raw observations, benchmarks, provenance, and conflicts are never included. The `/v1/models` route reads `config.model_info.include_in_models_endpoint` and `app.state.model_info` to build a summary map once before the loop, resolving by `base_model_id` for provider-suffixed entries. Model-info errors are logged and silently omitted.
- **Dashboard integration** (phase 4): `handle_models()` in `dashboard/routes.py` fetches model-info summaries concurrently with stats via `asyncio.gather()`. `render_models()` renders an "Info" column with colored status pills (`pill-fresh`, `pill-partial`, `pill-sparse`, `pill-stale`, `pill-conflict`, `pill-unmatched`, `pill-source-unavailable`). Tooltips are plain `title` attributes with escaped summary text, sources, and last-refreshed timestamp. All source-provided text is HTML-escaped via `escape()`. CSS added to `dashboard/static/dashboard.css` using theme-compatible colors.
- **Dashboard detail page** (post-phase 5): `GET /models/{model_id:path}` renders a full model-info detail page with status cards, summary, provider/callability, metadata, benchmarks, Hugging Face metadata, conflicts, and provenance sections. `handle_model_detail()` in `dashboard/routes.py` fetches `model_info` service from `app.state`; `render_model_detail()` in `dashboard/render.py` renders all sections with HTML-escaped output. Models page links to detail via model ID hyperlinks.
- **Artificial Analysis source** (phase 5): `ArtificialAnalysisSource` fetches benchmark data (throughput, latency, pricing) from the Artificial Analysis API. Gated behind an API key (`[model_info.sources.artificial_analysis]`); disabled by default. Emits `SourceModelRecord` observations with benchmark fields (tokens_per_second, time_to_first_token, cost_per_1k_input, cost_per_1k_output)
- **Hugging Face source** (phase 5): `HuggingFaceSource` fetches model card metadata and pipeline tags from the Hugging Face Hub API. Exact alias matching only (no fuzzy matching). Disabled by default; enable via `[model_info.sources.huggingface]`
- **Manual overrides** (phase 5): field-level, config-driven overrides in `[model_info.overrides.<model-id>]`. Supports display_name, summary, and other canonical fields. Overrides are applied after all source merges and take precedence over any source-provided value
- **Alias expansion** (phase 5): configured aliases in `[model_info.aliases]` map canonical model IDs to alternative identifiers. Source-specific alias matching (e.g., OpenRouter model IDs) uses these aliases during identity resolution. Aliases are also persisted in the `model_info_aliases` table
- **Source health hardening** (phase 5): `model_info_source_health` tracks `rate_limited_until` (explicit backoff timestamp), `last_status_code`, `last_payload_count`, and `last_success_duration_ms`. Sources respect `rate_limited_until` to avoid hammering rate-limited APIs. Health data is exposed via `GET /api/model-info/sources`
- **Detail API enhancements** (phase 5): `GET /api/model-info/{model_id}` returns benchmark data (per-source throughput/latency/pricing), alias list, Hugging Face metadata (pipeline_tags, model_card_url, library_name), and manual override indicators
- **Richer summary generation** (phase 5): deterministic summaries now include sparse-data warnings, benchmark highlights (e.g., "74 tok/s on Artificial Analysis"), Hugging Face card availability, and conflict annotations when sources disagree on fields
- **`model_info_overrides` table** (phase 5): migration `0037` adds a `model_info_overrides` table for persisting operator-set overrides to canonical fields. Overridden fields are marked with an `overridden` flag on canonical rows to distinguish from source-provided values

## Model Context Limits

EggPool supports configurable effective context limits per model per provider, allowing operators to advertise smaller context windows than the provider physically supports.

### Configuration

- **`ModelLimitOverrideConfig`** — reusable Pydantic model with `max_context_tokens`, `max_input_tokens`, `max_output_tokens`, `enforce_context_limit`
- **Global overrides** — `[model_overrides.<model-id>]` applies to all providers
- **Provider overrides** — `[providers.<id>.model_overrides.<model-id>]` per provider

### Resolution

`ModelLimitResolver` in `catalog/limits.py` resolves effective limits per field with precedence:
1. Provider-specific override
2. Global override
3. Upstream-reported metadata
4. Unknown (None)

### Exposure

- **Unsuffixed models** — `conservative_limits()` takes the minimum across all visible providers
- **Provider-suffixed models** — each provider's exact limits are preserved
- **`/v1/models`** — includes namespaced `eggpool.limits` extension for observability

### OpenCode Integration

`eggpool configsetup opencode --json-only` generates OpenCode provider config with explicit `limit.context`, `limit.input`, and `limit.output` per model. This drives OpenCode's native compaction machinery.

## Daemon Mode

`eggpool serve --daemon` is a one-shot detach helper for personal / SBC
deployments. It validates the configuration, refuses to start a second
instance, spawns a detached child running the normal foreground `serve`
command, and returns promptly with a short success message pointing at
the log file.

The parent only validates the config and refuses to start a second
instance. The detached child runs the foreground supervisor (Granian +
worker) unchanged. The `--daemon` flag is **never** forwarded to the
child; detachment is purely a parent-side concern. The child owns its
own PID file lifecycle via `runtime.write_pid_file()` /
`runtime.clear_pid_file()`.

### Detach mechanics

- `start_new_session=True` so the child survives shell exit and signals to the parent CLI do not propagate
- `stdin=subprocess.DEVNULL` to detach from the calling terminal
- `stdout`/`stderr` redirected to a log file (or `/dev/null` when `--quiet` is set without `--log-file`)
- Default log file: `~/.local/state/eggpool/eggpool.log` (resolvable via `eggpool.runtime_paths.default_log_file()`); override with `--log-file PATH` or `$EGGPOOL_LOG_FILE`. A log file beats `/dev/null` by default because a silent background failure is hard to diagnose
- The `subprocess.Popen` handle is intentionally not awaited by the CLI parent; the parent returns as soon as the child has been spawned

### PID file resolution

PID file path resolution lives in `eggpool.runtime_paths.default_pid_file()` and is the single source of truth shared by `serve`, `serve --daemon`, `croncheck`, `ensure-running`, `stop`, `restart`, systemd, and the cron watchdog. Precedence:

1. `$EGGPOOL_PID_FILE` (if set)
2. `$XDG_RUNTIME_DIR/eggpool.pid` (if `XDG_RUNTIME_DIR` is set)
3. `~/.local/state/eggpool/eggpool.pid` (state dir auto-created)
4. `/tmp/eggpool-<UID>.pid` (UID-scoped fallback)

The `eggpool.constants.PID_FILE` constant is now a `_PIDFileProxy` that
resolves through `default_pid_file()` on every read, so the constant
inherits the same resolver for backwards compatibility with code that
imports it directly.

### Root-user guard

`serve --daemon` refuses to daemonize when the effective UID is 0 unless
`--as-root` is passed. This prevents accidentally starting a personal
deployment as root; the explicit flag exists for intentional system-wide
installs. systemd production deployments should run foreground `serve`
under the systemd unit (with `User=` set) and must not use `--daemon`.

### `runtime.start_server()` signature

`runtime.start_server()` accepts:

```python
def start_server(
    config_path: str,
    *,
    cwd: str | None = None,
    daemon: bool = True,
    log_path: str | None = None,
    quiet: bool = True,
    verify: bool = False,
    verify_timeout_s: float = 3.0,
) -> subprocess.Popen[bytes]:
    ...
```

`runtime.restart_server()` accepts the same `daemon`, `log_path`, and
`quiet` options. The CLI flags `eggpool serve --daemon`, `--log-file`,
`--quiet`, and `--as-root` map directly to these parameters.

### Installation and Deployment

The install / deploy / uninstall surface is split across two source
modules and one CLI module so the responsibility is explicit:

- **`eggpool.deploy_user`** — user and path resolution:
  - `DeployUser` dataclass (`user`, `uid`, `gid`, `home`, `is_root`, `is_sudo`)
  - `resolve_deploy_user()` — handles normal, sudo (`SUDO_USER`/`SUDO_UID`/`SUDO_GID`), and direct-root cases via `pwd.getpwnam` / `pwd.getpwuid`
  - `resolve_config_path()` — `--config` > `$EGGPOOL_CONFIG` > `~/.config/eggpool/config.toml` > `./config.toml` (single source of truth for every CLI command)
  - `resolve_env_path()` — `$EGGPOOL_ENV` > `<config-dir>/.env` > XDG default
  - `default_config_dir()` / `default_data_dir()` / `default_state_dir()` / `default_config_path()` / `default_env_path()` — XDG-aware default paths honoring `$XDG_CONFIG_HOME`, `$XDG_DATA_HOME`, `$XDG_STATE_HOME`

- **`eggpool.deploy`** — bundled snippets + dynamic builders:
  - Bundled constants: `SYSTEMD_UNIT` (the hardened production layout, byte-for-byte identical to `deploy/eggpool.service`), `LOGROTATE_CONF`, `CRON_BACKUP_FILE`, `CRON_BACKUP_SCRIPT`
  - Personal builders: `build_personal_systemd_unit()` (renders `User=`/`Group=` from the resolved `DeployUser`), `build_personal_watchdog_cron()`, `build_personal_backup_block()`, `build_personal_logrotate()`
  - Cron block management: `install_cron_block()`, `remove_cron_block()`, `strip_managed_cron_blocks()` — every block is bracketed by `# BEGIN EggPool ...` / `# END EggPool ...` markers so uninstall only strips eggpool-owned lines

- **`eggpool.cli_full.deploy_*`** — Click commands that consume the modules above:
  - `deploy systemd [--install] [--production] [--as-root]` — personal mode (default) renders the unit with `User=`/`Group=` set to the invoking user; `--production` provisions `/etc/eggpool` + `/var/lib/eggpool` + dedicated `eggpool` system user
  - `deploy cron [--install|--uninstall] [--interval N]` — watchdog (`@reboot` + `*/N * * * *` `ensure-running`), bracketed by `BEGIN EggPool watchdog` markers
  - `deploy backup-cron [--install|--uninstall] [--production]` — daily backup (user cron for personal, `/etc/cron.d/eggpool-backup` for production)
  - `deploy logrotate [--install]` — writes `/etc/logrotate.d/eggpool` and validates via `logrotate -d` (no `systemctl restart logrotate`)
  - `deploy all [--install]` — systemd + logrotate + watchdog cron (backup-cron is separate)

- **`eggpool.cli_full.uninstall`** — orchestrates `eggpool.lifecycle.uninstall.uninstall()`. Pass `--deploy-artifacts` to also remove the systemd unit, logrotate config, watchdog + backup cron blocks, and backup script. PATH edits are previewed via `preview_eggpool_path_changes()` / `RcFileChange` before being written so the operator can confirm the diff.

The production systemd unit (`SYSTEMD_UNIT` constant) is the source
of truth for the production layout. The matching file at
`deploy/eggpool.service` is kept byte-for-byte identical so both
source-checkout operators and wheel-installed users see the same
content. To update either, edit `eggpool.deploy.SYSTEMD_UNIT` AND
`deploy/eggpool.service` together.

### Filesystem Layout

Personal use (XDG defaults — overridable via `$XDG_*`):

```
~/.config/eggpool/
├── config.toml          # Main configuration
└── .env                 # Environment variables (API keys)

~/.local/share/eggpool/
├── usage.sqlite3        # SQLite database
├── usage.sqlite3-wal    # WAL journal
└── usage.sqlite3-shm    # Shared memory file

~/.local/state/eggpool/
├── eggpool.pid          # Live PID (owner: supervisor)
├── eggpool.log          # Daemon log
└── cron.log             # Watchdog cron output
```

Production (`eggpool deploy systemd --install --production`):

```
/etc/eggpool/            # Configuration + env
/var/lib/eggpool/        # Database + working state
/var/log/eggpool/        # Daemon logs
/var/backups/eggpool/    # Daily backup archives
/opt/eggpool/            # Source checkout + venv
```

## Security

- Local client credentials are stripped before upstream forwarding
- Only the selected account's bearer token is injected
- API keys stored as environment variable names, never in SQLite
- Constant-time comparison for API key verification
- Fail-closed error detail redaction (configurable)
- Optional CORS and trusted host middleware

## Background Tasks

`TaskSupervisor` (`background/__init__.py`) manages long-running loops with restart-on-failure and exponential backoff. All tasks are registered in `app.py` during lifespan setup:

| Task | Condition | Description |
|------|-----------|-------------|
| `catalog_refresh` | `refresh_interval_s > 0` | Periodic upstream model catalog refresh |
| `retention_cleanup` | Always | Hourly cleanup of old requests, events, pings, rollups, and expired reservations |
| `checkpoint` | Always | Periodic SQLite WAL checkpoint (every 4h) |
| `usage_window_refresh` | Always | Refreshes persisted usage windows every 60s |
| `stale_request_finalizer` | Always | Safety net for leaked streaming requests (every 60s) |
| `metrics_flush` | `write_mode != "immediate"` | Buffered analytics flush to `usage_rollups` |
| `update_checker` | Always | Periodic PyPI update check (default 24h); see `update_checker.py` |
| `automatic_backup` | `backup.enabled` | In-process SQLite backup with count-based retention; see `background/backup.py` |
| `health_disabled_models_prune` | Always | Periodic sweep that drops stale `model_availability` and `disabled_models` entries (every 60s) |

## In-Memory Bounds and Memory Footprint

Long-running deployments — especially Raspberry Pi / SBC nodes — must keep steady-state RSS bounded by workload throughput, not workload cardinality. Every growth axis in the hot path is capped by a hardcoded module constant or a per-catalog config knob; see `plans/memory.md` for the full design and the per-request regression test (`tests/integration/test_memory.py`, marked `pytest.mark.slow`).

| Structure | Location | Cap | Eviction |
|-----------|----------|-----|----------|
| `QuotaEstimator.account_model_ewma` | `src/eggpool/quota/estimation.py:285` | `EWMA_HARD_CAP = 4096` (hardcoded) | LRU; on miss recomputes from persisted `QuotaWindow` |
| `QuotaEstimator.global_model_ewma` | `src/eggpool/quota/estimation.py:286` | `GLOBAL_EWMA_HARD_CAP = 1024` (hardcoded) | LRU |
| `CatalogResolverPipeline.TTLCache._data` | `src/eggpool/catalog/catalog_resolvers.py:128` | `max_entries = 4096` per `[pricing.catalogs.<name>]` (configurable) | LRU on store; `entry.raw` stripped after parse |
| `ModelCatalogCache._models` / `_provider_models` | `src/eggpool/catalog/cache.py:109-111` | De-duplicated (per-provider override only when it differs from global) | — |
| `ModelCatalogCache._account_support` | `src/eggpool/catalog/cache.py:114` | `frozenset[str]` (no per-call `.copy()`); bounded by registered account × model cardinality | — |
| `OutboundClientManager._per_host_*` | `src/eggpool/providers/outbound.py:85` | `MAX_TRACKED_HOSTS = 256` (hardcoded) | Coldest-total eviction; `evictions_total` surfaced in `snapshot()` and the `outbound_client` runtime metric |
| `AccountRuntimeState.model_availability` | `src/eggpool/accounts/state.py` | Pruned at every `AccountRegistry.sync_accounts` against advertised model set | — |
| `HealthManager.AccountHealth.disabled_models` | `src/eggpool/health/health_manager.py:111` | Pruned by `health_disabled_models_prune` supervisor task (60s cycle) | — |

The `frozenset` switch on `_account_support` (`src/eggpool/catalog/cache.py:639`) eliminates one O(n) `set.copy()` per routing decision. Every caller of `get_supporting_accounts(...)` / `get_supporting_accounts_for_model(...)` is read-only (membership, intersection, iteration), so the immutability is a strict superset of caller needs.
