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

`integrations/common.py` owns configsetup context construction, catalog-backed
default model resolution, and format-safe scalar/key rendering helpers. New
agent targets should reuse those helpers instead of hand-quoting JSON, TOML,
YAML, shell, or model ID values in target modules.

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
- **Runtime wiring**: `RequestCoordinator` receives `config.transcoder`
  via the `transcoder_policy` constructor parameter so that
  `self._transcoder_policy.features` gates per-feature transcoding
  during actual dispatch. `app.state.transcoder_policy` remains set
  for preflight helpers and diagnostics in `proxy_request.py`.

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

**Phase 6.1 — Tool-use transcoding**: bidirectional tool calling translation in both directions for non-streaming and streaming requests. `OpenAIToAnthropic.encode_request` / `decode_response` and `AnthropicToOpenAI.encode_request` / `decode_response` translate `tools`, `tool_choice`, `parallel_tool_calls`, assistant `tool_calls` history, `role: "tool"` history, and `tool_use` / `tool_result` content blocks. A per-request `ToolCallIdMap` (on `TranscodeContext.id_map`) mints `call_<24 hex>` and `toolu_<24 hex>` ids so the two namespaces never collide. The streaming transcoders (`OpenAIToAnthropicStreaming`, `AnthropicToOpenAIStreaming`) extend their state machines to track `content_block_start` / `input_json_delta` / `content_block_stop` triples and emit OpenAI `tool_calls` deltas in insertion order; the reverse direction buffers OpenAI `tool_calls[*].function.arguments` chunks and flushes Anthropic `tool_use` blocks on `finish_reason: "tool_calls"`. Anthropic's `pause_turn` `stop_reason` maps to `finish_reason: "tool_calls"` plus a synthetic `__eggpool_pause_turn__` tool_call entry so OpenAI clients can detect pause-and-resume flows. `stream_options.include_usage` is lifted onto `TranscodeContext.request_include_usage` so the streaming transcoder can decide whether to forward upstream usage chunks. New loss-warning kinds (`tool_call_id_translated`, `tool_call_id_changed`, `parallel_tool_calls_collapsed`, `malformed_tool_arguments`, `invalid_tool_choice`, `unsupported_tool_type`, `empty_tool_use_block`, `tool_result_image_dropped`, `tool_result_error_passthrough`, `cache_control_dropped`, `pause_turn`, `non_text_content_dropped`, `tool_result_inferred`) are added to `LOSS_WARNING_KINDS`. See `docs/transcoding.md` § Tool-Use Transcoding and `plans/tooltranscoding.md` for the full design.

**Phase 7 — Budget resolution**: `resolve_thinking_budget()` in `src/eggpool/transcoder/budget_resolver.py` is the single source of truth for effort-to-budget translation. Resolution order: explicit `thinking.budget_tokens` (Anthropic style) → `reasoning_effort` (OpenAI style) via `ThinkingCapability.effort_to_budget_tokens` → `[transcoder.thinking_budget_defaults]` → hard-coded fallback (low=1024, medium=4096, high=16384). Budgets are clamped to `budget_tokens_min`/`budget_tokens_max` when known. `budget_resolution_policy = "strict"` rejects unknown efforts and clamped budgets before dispatch. New loss-warning kinds: `budget_clamped`, `unknown_effort`, `budget_rejected`, `budget_resolution_no_input`. The `BodyTranscoder.encode_request` protocol accepts optional `thinking_capability`, `budget_defaults`, and `budget_resolution_policy` kwargs.

**Phase 8 — Response-field compatibility**: configurable OpenAI-compatible reasoning field names for both streaming and non-streaming responses. `[transcoder.openai_reasoning_fields]` controls `non_stream` (default `["reasoning_content"]`) and `stream_delta` (default `["reasoning"]`) field names. `emit_compat_aliases = false` (default) emits only the primary field; when true, additional aliases are emitted. Streaming thinking deltas are now feature-gated consistently with non-streaming paths — when `[transcoder.features].thinking = false`, streaming thinking deltas are dropped. The `build_reasoning_fields()` helper in `src/eggpool/transcoder/policy.py` builds the field dict from config. `AnthropicToOpenAIStreaming` and `OpenAIToAnthropic.decode_response` accept optional `reasoning_field_names` and `emit_compat_aliases` parameters forwarded from the coordinator via `TranscoderPolicy.openai_reasoning_fields`.

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

Ordered SQL migrations in `db/schema/` (0001 through 0038). Checksums tracked in `checksums.json`.

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

EggPool is not purely lowest-score-wins for same-tier peer accounts. When
accounts are effectively tied by priority, weight, health, protocol, and
utilization score, same-tier fairness rotates candidates to avoid stable
config-order bias and subscription starvation.

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

The rotor's position map is capped at 4096 entries
(``_ROTOR_HARD_CAP``).  When the cap is reached the entire map is
cleared and rotation restarts from position 0 for all keys.  This is a
blunt eviction strategy — there is no LRU or partial eviction.  The cap
prevents unbounded memory growth when model IDs or fairness keys vary
heavily.

Fairness is controlled by three ``[routing]`` config fields, all honored
by the server runtime:

- ``fairness_mode``: ``"round_robin"`` (default), ``"random"``, or ``"off"``.
- ``fairness_epsilon``: score proximity threshold; defaults to ``near_tie_epsilon``
  when omitted.
- ``fairness_scope``: rotation group granularity — ``"provider_model_protocol"``
  (default), ``"provider_model"``, or ``"priority_model_protocol"``.

Scope semantics for the fairness key:

- ``provider_model_protocol``: key includes provider, model, routed protocol,
  priority tier, and client protocol. Separate rotor per provider/model/protocol
  group. This is the default and recommended scope for subscription aggregation.
- ``provider_model``: key includes provider, model, priority tier, and client
  protocol; protocol is intentionally excluded so OpenAI and Anthropic traffic
  for the same model collapses into one rotation group.
- ``priority_model_protocol``: key excludes provider but includes model, routed
  protocol, priority tier, and client protocol. Co-balances accounts from
  different providers serving the same model in the same priority tier.

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
    "scope": "provider_model_protocol",
    "key": "provider=opencode-go|model=gpt-4|protocol=openai|tier=0|client_protocol=openai",
    "candidate_count": 3,
    "selected_index": 0,
    "selected_account_name": "0002",
    "reason": "ok"
  }
}
```

The ``top_candidates`` array in the same payload carries per-candidate
fairness annotations:

- ``rank_before_fairness``: candidate's position in the score-ordered list
  before the fairness rotor reordered the band.
- ``rank_after_fairness``: candidate's position in the final list.
- ``fairness_band_member``: ``true`` when the candidate was part of the
  fairness band eligible for rotation.

#### Diagnosing routing skew

When skew persists after deploying the fairness patch, run:

```bash
eggpool accounts explain --model '<hot-model>' --protocol openai --scores
```

Then inspect recent routing decisions:

```sql
SELECT
  selected_account_name,
  eligible_count,
  scored_count,
  top_score_account_name,
  selected_score,
  json_extract(score_components_json, '$.fairness.mode') AS fairness_mode,
  json_extract(score_components_json, '$.fairness.applied') AS fairness_applied,
  json_extract(score_components_json, '$.fairness.scope') AS fairness_scope,
  json_extract(score_components_json, '$.fairness.reason') AS fairness_reason,
  json_extract(score_components_json, '$.fairness.candidate_count') AS fairness_candidates
FROM routing_decisions
ORDER BY id DESC
LIMIT 50;
```

Interpretation:

- ``fairness_applied = true`` with ``fairness_candidates = 3``: fairness is
  working; check that the distribution is approximately balanced.
- ``fairness_applied = false`` with ``reason = not_tied``: scores diverge
  beyond ``fairness_epsilon``; the skew is driven by score policy, not
  config order.
- ``fairness_applied = false`` with ``reason = different_weights``: accounts
  have unequal weights; adjust weights to match if equal peer rotation is
  desired.
- ``eligible_count = 1`` or ``scored_count = 1``: this is not a fairness
  problem; accounts are excluded by catalog, health, or quota policy.
- ``fairness_applied = false`` with ``reason = disabled``: ``fairness_mode``
  is set to ``"off"``; switch to ``"round_robin"`` to enable rotation.

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

## Catalog Refresh Semantics

The catalog refresh path is **non-destructive by default**. Healthy
account/model support rows survive every refresh cycle so a transient
network blip, an empty upstream response, or a partially-normalized
response cannot silently de-pool a healthy account. The only de-pooling
mechanism in the catalog layer is `HealthManager`; configuration and
health state jointly own the eligibility decision.

### Per-account outcome classification

`CatalogService._fetch_and_process_account` categorizes every refresh
attempt and returns an `AccountCatalogOutcome` plus, on success, an
`AccountCatalogUpdateResult`:

| Outcome | When | Cache touched? |
| ------- | ---- | -------------- |
| `SUCCESS_AUTHORITATIVE` | HTTP 2xx, fully protocol-resolved, non-empty | Add/update only unless `catalog_withdrawal_policy` permits withdrawal |
| `SUCCESS_PARTIAL` | HTTP 2xx, but at least one model lacks a resolved protocol | Add/update only; withdrawal forced off |
| `SUCCESS_EMPTY` | HTTP 2xx with zero normalizable models | No-op (prior support preserved) |
| `FAILED` | Network/5xx/auth/quota/JSON-shape failure | **No** (cache untouched) |
| `SKIPPED` | Fetcher returned without contacting upstream | **No** (cache untouched) |

### Withdrawal policy

`ModelsConfig.catalog_withdrawal_policy` controls when withdrawal is
permitted:

- `preserve_until_health` (default) — withdrawal is **never**
  triggered by a refresh. Health state is the sole de-pooling
  mechanism.
- `confirmed_once` — a single authoritative refresh may withdraw
  support for models no longer advertised.
- `confirmed_twice` — two consecutive authoritative refreshes are
  required to withdraw support.

`SUCCESS_PARTIAL` overrides the policy for that cycle and forces
`allow_withdrawals = False` because a partial response is never a
complete withdrawal confirmation. The destructive
`mark_account_models_unavailable` step is also gated on
`authoritative=True, allow_withdrawals=True` so the cache layer
itself enforces the invariant; the service decides which flags to
flip based on outcome category and policy.

### Per-cycle operational logging

`CatalogService.refresh()` calls `_log_refresh_summary` after every
cycle. The INFO log enumerates per-outcome counts on one line so
operators can spot catalog uncertainty without enabling debug
logging:

```
Catalog refresh summary: policy=preserve_until_health total=3 authoritative=1 partial=0 empty=1 failed=1 skipped=0
```

A run with many `FAILED` or `PARTIAL` rows is signal that an upstream
or DNS path is unhealthy; a sudden shift in `AUTHORITATIVE` count
indicates a real catalog change that may need rebalancing.

### Gate diagnostics for `accounts explain`

`Router.explain_account_eligibility(include_gates=True)` returns a
per-account gate breakdown dict (config, credentials, health,
circuit, provider, protocol, model support, freshness, provider
metadata, protocol match, local quota, final eligible) so operators
can pinpoint exactly which gate is failing without running live
traffic. The dict is informational; the canonical decision still
comes from `_classify_eligibility`. The `eggpool accounts explain
--gates` CLI command renders the same breakdown as a compact
text table.

### Shared per-provider metadata and sibling-wins protocol guard

`_provider_models` is keyed by `(model_id, provider_id)` and is
**shared** by every account that lists that provider — e.g. all
`opencode-go-0001`/`-0002`/`-0003` accounts share one row per
model on the `opencode-go` provider. The previous
`update_from_account()` clobbered this shared dict unconditionally
even on partial responses, which produced the upstream-reported
"all traffic on `opencode-go-0001`, none on `0002`/`0003`" regression
when a single account's refresh resolved the protocol as `None`.
`_preserve_resolved_protocol()` now applies a sibling-wins guard
in the non-destructive path: when a per-provider row already has a
resolved protocol and the new entry arrives with `protocol=None`,
the prior protocol is preserved and the resulting per-provider row
is shared across all sibling accounts. The destructive path
(`authoritative=True AND allow_withdrawals=True`) intentionally
skips the guard so operator-initiated withdrawals remain effective.

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
├── ContextLimitExceededError
└── CapabilityError (model_id, capability, requested_fields attributes)
```

## Model Information

- **Sidecar subsystem**: `model_info/` package provides persistent model metadata sidecar tables (`model_info_canonical`, `model_info_observations`, `model_info_aliases`, `model_info_source_health`) via migration `0036`, with phase 5 hardening and override storage added by migration `0038`
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
- **`model_info_overrides` table** (phase 5): migration `0038` adds a `model_info_overrides` table for persisting operator-set overrides to canonical fields. Overridden fields are marked with an `overridden` flag on canonical rows to distinguish from source-provided values

### Corrective Pass (Phases A–F)

The model-info corrective plan in `plans/model-info-corrective-catalog-models-and-cards.md` makes the sidecar **observation-first** instead of usage-first, ensures external sources reach every model they should, and surfaces catalog presence on the dashboard.

- **Configured-alias seeding (Phase A)**: `ModelInfoService.seed_configured_aliases()` runs at startup (inside `load_cache()`) and inserts every `[model_info.aliases]` entry into `model_info_aliases` before the first external source fetch. Skips empty source/source_model_id; tolerates duplicates; uses `_alias_confidence_to_float()` to map names like `exact`/`curated`/`high` to numeric confidences. Mandates Hugging Face exact-source matches, which are otherwise impossible to link.
- **Observation-driven canonical detail (Phase B)**: `build_canonical_detail(latest_observations, sources, *, summary=None, supports_vision=None, ...)` merges the freshest observation per source into a single `detail` dict, then layers manual overrides and conflict detection (`_detect_context_conflicts`, `_detect_benchmark_conflicts`). The merged detail exposes a nested `detail["limits"]` block with `effective_context`, `external_context`, `effective_output`, `external_output`; the API detail handler reads from this block via a legacy fallback that maps the pre-Phase-B flat keys (`context_tokens`→`effective_context`, `context_window_external`→`external_context`, etc.). `reconcile_catalog_snapshot()` and `refresh_due_models()` are non-destructive — observation `last_refreshed_at` is preserved across restarts.
- **Single-model refresh (Phase C)**: `ModelInfoService.refresh_model_info(model_id, *, provider_id=None, source=None, force=False)` runs an immediate refresh for one model: provider catalog observation is always written (the source of truth for callability), the requested external source is fetched and indexed by `source_model_id`, and alias rows are matched identity-first. When `provider_id` is supplied the provider-catalog branch only matches that provider's record, narrowing the per-account store. `POST /api/model-info/refresh?model_id=<id>&source=<provider_catalog|openrouter|artificial_analysis|huggingface>&force=1` exposes the entry point and returns a `scope=model` payload with counts (`refreshed`, `skipped`, `errors`, `sources_attempted`, `sources_matched`, `observations`) plus the canonical `model_id`, the original `requested_model_id`, and the resolved `provider_id`. The endpoint URL-decodes `model_id`, then calls `parse_model_provider()` so `?model_id=gpt-4o/openai&force=1` refreshes the canonical `gpt-4o` row with `provider_id="openai"`. `source=all` (or absent) means every enabled source; unknown `source` values return HTTP 400 before the service is touched.
- **Catalog-complete Models page (Phase D)**: `handle_models()` runs `_get_model_info_summary_map()`, `get_dashboard_models()`, and the new `_get_catalog_rows()` concurrently via `asyncio.gather`. `_get_catalog_rows()` builds sparse `models` rows for every catalog entry, honoring `[models].collapse_models`:
  - **`collapse_models = false` (default)** — emits one row per `(model_id, provider_id)` pair by iterating `catalog.cache.get_provider_model_entries()`.
  - **`collapse_models = true`** — emits one row per unsuffixed `model_id` by iterating `catalog.get_models_for_exposure()` and threading the `providers` list (sorted) onto each row. `provider_id` is set to the first sorted provider so stats rows keyed by `(model_id, provider_id)` still match. `routing_priority_max` reflects the max priority across contributing providers.
  Both paths apply the account filter, set the diagnostic fields (`base_model_id`, `providers`, `available`, `catalog_status`, `routing_priority`, `protocol`, `display_name`), and produce zero-activity placeholders so unused models still render. `_merge_models_with_catalog(stats, catalog, *, collapse_models=...)` dedupes by `(model_id, provider_id)` in provider-scoped mode and by `model_id` only in collapsed mode (via `_model_row_key()`); legacy stats rows that omit `provider_id` fall back to `catalog_by_id[model_id]` for diagnostic fields but do **not** suppress provider-scoped catalog rows, so an unused sibling provider for the same base model still renders. The merged list is sorted by request count descending with model_id and provider_id as tie-breakers. `render_models()` URL-encodes the detail link path segment via `urllib.parse.quote(safe="")` so model ids with provider suffixes, query metacharacters (`?`, `#`), or HTML-special characters round-trip cleanly through `/models/{model_id:path}` + the detail handler's `unquote()`.
- **Detail API legacy schema (Phase E)**: `handle_model_info_detail()` reads `detail["limits"]` first and falls back to the legacy flat keys only when the nested block is absent. Combined with Phase B's normalization, every pre-Phase-B canonical row remains API-readable; new writes always use the nested schema.
- **Legacy detail backfill (Phase F)**: `ModelInfoService.backfill_legacy_detail_blocks(limit=200)` walks `repo.list_all_canonical(limit)` and lifts pre-Phase-B flat keys into `detail["limits"]` via the private `_legacy_flat_keys_to_limits()`. Observation-derived `external_*` keys can overwrite stale legacy seeds so fresh OpenRouter/Artificial Analysis data wins. The provenance `backfilled_limits=True` marker prevents double-lift. Wired into `app.py` startup immediately after `backfill_missing_canonical()`.

## Model Capabilities

Protocol-neutral capability schema in `src/eggpool/catalog/capabilities.py` provides a structured representation for model capabilities, currently focused on thinking/reasoning. The schema decouples capability knowledge from any specific transcoder implementation so catalog, routing, serialization, and config code can import it without circular dependencies.

### Capability Model

- **`ThinkingCapability`** — structured thinking/reasoning capability with `status` (`CapabilityStatus`), `source` (`CapabilitySource`), `native_protocols`, `client_controls` (per-protocol field mappings), `budget_tokens_min`/`budget_tokens_max`, and `effort_to_budget_tokens`
- **`ModelCapabilities`** — top-level container holding a `ThinkingCapability` field; designed to grow future capability families (vision, tools, structured outputs, prompt caching, logprobs)
- **`ThinkingClientControls`** — per-protocol field mappings for request, response, and streaming delta fields
- **`CapabilityStatus`** — `Literal["supported", "unsupported", "unknown", "mixed", "conflicting"]` where `"unknown"` means no data observed (not `"unsupported"`)
- **`CapabilitySource`** — `Literal["provider_catalog", "model_info", "manual_override", "heuristic", "aggregate", "unknown"]`

### Merge Semantics

Capability merge order is deterministic (lowest to highest priority):

1. Built-in safe defaults (absent capability = `"unknown"`)
2. Provider catalog / model-info data
3. Global model overrides
4. Provider-scoped model overrides

`merge_thinking_capabilities()` and `merge_model_capabilities()` implement override-wins semantics: the higher-priority value wins; on tie, the override is preferred. Manual overrides win over discovered metadata.

### Aggregate Semantics

Collapsed model entries may represent multiple providers. `aggregate_thinking_status()` derives a single status:

- `"supported"` only if every backing provider is `"supported"`
- `"unsupported"` only if every backing provider is `"unsupported"`
- `"unknown"` if all are `"unknown"`
- `"conflicting"` if any entry is `"conflicting"`
- `"mixed"` otherwise

`aggregate_thinking_capabilities()` produces a conservative aggregate: union of native protocols, last-wins per-protocol client controls, conservative budget bounds (max of mins, min of maxes).

### Serialization

`serialize_model_capabilities()` and `serialize_thinking_for_models()` produce a compact dict for the `/v1/models` response under the `eggpool.capabilities` namespace. Unknown/empty values are omitted.

**Provider-scoped entries** emit the full thinking capability shape including per-protocol client control field mappings (`openai_request_fields`, `openai_response_fields`, `openai_stream_delta_fields`, `anthropic_request_fields`, `anthropic_response_block_types`) when available.

**Collapsed entries** (unsuffixed model IDs) aggregate capabilities across all visible providers. When the aggregate status is `"mixed"` or `"conflicting"`, a `providers` dict maps each provider ID to its individual thinking status so clients can see per-provider truth without overclaiming support.

Example provider-scoped shape:
```json
{
  "id": "minimax-m3/minimax",
  "eggpool": {
    "capabilities": {
      "thinking": {
        "status": "supported",
        "source": "provider_catalog",
        "native_protocols": ["anthropic"],
        "openai_request_fields": ["reasoning_effort"],
        "openai_response_fields": ["reasoning_content"],
        "openai_stream_delta_fields": ["reasoning"],
        "anthropic_request_fields": ["thinking"],
        "anthropic_response_block_types": ["thinking"],
        "effort_to_budget_tokens": {"low": 1024, "medium": 4096, "high": 16384}
      }
    }
  }
}
```

Example collapsed mixed shape:
```json
{
  "id": "minimax-m3",
  "eggpool": {
    "capabilities": {
      "thinking": {
        "status": "mixed",
        "providers": {"minimax": "supported", "openrouter": "unknown"}
      }
    }
  }
}
```

### Request-Level Helpers

- `client_requests_thinking()` — heuristic check for thinking-related keys in the request body; returns `False` for unsupported/unknown/conflicting statuses
- `has_thinking_support()` — `True` when status is `"supported"` or `"mixed"`
- `classify_thinking_request()` — classifies whether a request explicitly requires thinking support, returning a `ThinkingRequestRequirement` with `required`, `client_protocol`, `fields`, `requested_effort`, and `requested_budget_tokens`
- `check_candidate_thinking_eligibility()` — determines whether a candidate model/provider is eligible for a thinking request based on its capability status and the configured policy

### Capability-Aware Routing

When a client sends a request with explicit thinking/reasoning controls, EggPool routes to ensure the upstream model can honor those controls. The pipeline is:

1. **Request classification** (`classify_thinking_request`): inspects the body for OpenAI `reasoning_effort`/`reasoning` and Anthropic `thinking`/`thinking_budget` indicators, plus assistant history `reasoning_content` blocks
2. **Eligibility filtering** (`get_eligible_accounts`): each candidate's thinking capability status (from the catalog cache) is checked against `[transcoder.capability_policy]` settings
3. **Candidate selection** (`select_account`/`select_accounts_for_failover`): only thinking-eligible candidates are considered; `CapabilityError` is raised if none remain
4. **Error responses**: `CapabilityError` (HTTP 400) is distinct from `ModelNotFoundError` (404) and `ModelUnavailableError` (503)

**Policy configuration** (`[transcoder.capability_policy]`):

```toml
[transcoder.capability_policy]
unsupported_thinking = "reject"       # reject | warn_drop | route_best_effort
unknown_thinking = "reject"           # reject | allow_with_warning | route_best_effort
mixed_collapsed_thinking = "filter"   # filter | reject | allow
```

Default policy is `reject` for all axes — a client explicitly asking for thinking gets either a compatible upstream or a clear error. The `route_best_effort` escape hatch ignores the status entirely. `mixed_collapsed_thinking = "filter"` silently drops non-thinking providers when a model is served by multiple providers; if no supported providers remain, the original unfiltered list is returned. `conflicting` status is always rejected — operators resolve conflicts via manual overrides (`[model_capabilities."<model>".thinking]`), which are merged before the eligibility check runs. `CapabilityError` carries `model_id`, `capability`, `requested_fields`, and a human-readable `message`.

### Design Principle

Protocol compatibility alone does not imply thinking support. An OpenAI-protocol model may or may not support reasoning controls; an Anthropic-protocol model may or may not support extended thinking. The capability schema captures this explicitly.

### Model-Info Capability Enrichment

`build_canonical_detail()` in `src/eggpool/model_info/service.py` merges thinking capability metadata from provider catalogs and external model-info sources into the canonical detail block under `capabilities.thinking`. The merge priority is:

1. **Provider catalog** data (highest — authoritative)
2. **External model-info** data (OpenRouter, etc. — advisory)
3. **Global config override** (`[model_capabilities."<model_id>".thinking]`)
4. **Provider-scoped config override** (`[providers.<id>.model_capabilities."<model_id>".thinking]`)

Provider catalog data always outranks external source data. When two external sources disagree, the merged status is set to `"conflicting"` with details preserved in the `notes` field.

Only explicit API-control documentation produces `status = "supported"`. For example, OpenRouter's `supported_parameters` listing "reasoning" or "thinking" is treated as explicit API-control evidence. Vague descriptions like "reasoning model" or "thinking model" do NOT produce `status = "supported"` — they remain `unknown`.

`_propagate_enriched_capabilities()` writes the enriched thinking capability back to the catalog cache during reconciliation, so `_copy_exposed_model` picks it up before config overrides are applied. Provider-native thinking capabilities (source == "provider_catalog") are never overwritten by model-info enrichment.

See `plans/thinking_reasoning_phase_04_model_info_enrichment.md` for the full design.

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
