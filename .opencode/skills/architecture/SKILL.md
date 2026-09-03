---
name: architecture
description: Architecture principles and design decisions for the EggPool project. Use when understanding the codebase structure, making design decisions, or reviewing architectural changes. Covers package boundaries, request lifecycle, and core invariants.
---

# Architecture Skill

**Full design details**: `architecture/README.md` and the per-subsystem deep dives in `architecture/`.

Use the architecture index and the relevant deep dive as the starting point
for implementation. Completed plans are historical provenance, not a required
navigation chain; consult only the active plan that scopes the change.

## Core Principles

- Database transactions use one shared connection and one asyncio-task owner; rollback is private to the owning transaction path.
- Database failure tests patch private commit/rollback callables; production database objects do not expose test-only injection state.
- Database shutdown joins generation-owned DB users before `Database.disconnect()`; direct-test fixtures must disconnect in `finally` on the canonical event loop.

- Package boundaries must remain explicit
- Request proxying, routing, accounting, and dashboard concerns must not be combined in endpoint handlers
- Use Pydantic v2 for all data validation
- Use aiosqlite for all database operations

## Request Lifecycle

All data-plane requests flow through `RequestCoordinator`:

1. **Endpoint** (`api/chat_completions.py` or `api/messages.py`) extracts model ID, parses provider suffix
2. **Routing** selects an eligible account via quota-aware scoring (`routing/router.py`) and publishes its provisional request/token load before durable persistence
3. **Attempt** is persisted to SQLite before upstream dispatch
4. **Provider Contract** renders absolute URL (`compose_provider_url()`) and auth headers (`build_upstream_headers()`) from `providers/contract.py`
5. **Protocol Transcoding** (if enabled) translates the request body when the client protocol differs from the upstream protocol
6. **Proxy** sends the request via the provider's `httpx.AsyncClient` from `ProviderClientPool`
7. **Streaming** is handled by one `proxy/sse.py` decoder; the coordinator fans shared frames to `proxy/sse_observer.py` and the selected frame-level transcoder
8. **Finalization** records usage, releases reservations, updates health state

### Key Invariants

- Requests must be persisted before upstream dispatch
- A successful account claim publishes provisional request/token load under `_selection_claim_lock` before SQLite persistence. Persistence stays outside the lock; durable success converts the same ownership to the canonical reservation, while failure/cancellation releases it exactly once.
- Dispatch persistence is binary: the direct per-request transaction returns fully valid durable identities or raises; rollback never creates placeholder success results. SQLite persistence stays outside `_selection_claim_lock`.
- Dispatch exception boundaries are stage-local. Provider/client preparation, request construction or serialization, and client-facing response adaptation faults are local terminal errors with no provider retry or penalty. Only typed HTTPX transport failures are retry candidates.
- Retries consume one shared upstream-submission budget of `1 + max_retries_before_stream`. Normal retry destinations are another account on the same wire; a deterministic pre-handoff wire rejection may reopen the same account for an alternate candidate. The request records `attempt_ceiling_reached` when the configured ceiling leaves eligible destinations unattempted.
- Bare/unknown 401 responses are client-visible only: they do not disable credentials, penalize health, or cascade to other accounts. Explicit credential invalidity disables only the selected account; deterministic wire auth/surface/schema rejection is resolver-only and may retry the same account on another candidate.
- Pre-handoff failures can retry; `downstream_started` becomes true when the proxy forwards ASGI `http.response.start`, before body iteration, so no retry is possible after response handoff. `asyncio.CancelledError` propagates. Empty started streams are post-handoff even with zero body bytes.
- Non-streaming response adaptation completes before durable `COMPLETED`; native invalid JSON may pass through when usage is optional, while required transcoded responses must adapt successfully.
- Every retryable failed attempt must reach terminal state through retained attempt cleanup before the next attempt
- Each attempt reservation is released exactly once via `AttemptFinalizer`
- Streaming success requires native upstream terminal evidence: OpenAI `[DONE]`, Anthropic `message_stop`, Responses `response.completed`, Gemini Interactions `interaction.completed`, or Gemini `finishReason=STOP`. Use `StreamCompletionSnapshot` and `classify_stream_eof()`. Non-success terminal events are forwarded in the client's grammar; transport EOF never synthesizes a terminal marker.
- `_crash_recovery` runs at every startup and repairs pending requests and active reservations left by a previous process. Normal request handling has no age-only stale sweep.

## Protocol Transcoding

OpenAI Chat Completions ↔ Anthropic Messages conversion lives in
`src/eggpool/transcoder/` with the canonical semantic boundary in
`src/eggpool/wire/ir.py`. The closed wire registry provides concrete
Responses, Gemini Interactions, and Gemini `generateContent` codecs alongside
the Chat/Messages adapters. Controlled by `[transcoder]` config where the
legacy field-level transcoder applies; on by default. Provider payload is
`ProviderBoundRequest` with copy-on-write ownership; cross-protocol
encoders receive a read-only `Mapping` and return fresh target graphs.
`ReasoningIntent` is captured before target selection; effort labels are not
converted to guessed budgets. Reasoning support and caller controls are
discovered per provider/model from explicit catalog or verified model-info
metadata, with operator overrides as the intentional escape hatch; model
family names never supply defaults. The compatibility Chat/Messages codecs in
`wire/codecs/compat.py` are the staged boundary adapters; mature field-level
translators remain production-owned until later surface migration.
Native prompt-cache, thinking control, structured outputs, and tool
calling are capability-gated. See `architecture/deep-dive-transcoder.md`
for streaming hot path, ownership lifecycle, and full translation table.

## Wire Profiles

`src/eggpool/wire/` owns the closed `WireSurfaceName` and immutable
`WireProfile` structural contract. Wire surfaces are independent of the
compatibility `ProtocolName` values: a provider can expose multiple concrete
paths/auth shapes for one protocol family. `ProviderConfig.wire_surfaces` is
the explicit candidate map; absent maps are synthesized from legacy path
fields. `providers/_wire_profiles.toml` selects only Python-registered codec
IDs and carries advisory exact model hints. It cannot import code, retain
account secrets, or trigger network/probe behavior. Surface auth is rendered
with the selected account key only at dispatch-header construction time.

`ProcessRuntime.wire_profile_resolver` owns bounded runtime preference learning,
per-provider/model single-flight, and provider-only abnormal-dispatch gating.
Generation-owned resolved profiles are keyed by a structural fingerprint that
omits credential values. Ordinary success refreshes a preference; only the
canonical failure-effects decision may authorize candidate suppression or an
alternate-surface transition. The leader owns the gate permit while submitting
discovery candidates; followers wait on a shielded wire decision and then send
their own inference requests. Rate pressure completes the flight without
candidate suppression, and cancellation releases only coordination state owned
by that request. No background probes or second retry budget.

## Model-router Registry

`src/eggpool/model_router/` owns the typed optional `[model_routers.<id>]`
configuration and its immutable compiled registry. Virtual aliases are exact,
bounded, control-safe identifiers without `/`; selector and route targets must
remain concrete references, and current catalog availability is deliberately
not checked during config parsing. Routes receive deterministic compact IDs
from sorted operator labels, while each compiled router carries a stable
SHA-256 semantic fingerprint and bounded static selector policy.

`RuntimeGenerationFactory` compiles the registry before candidate publication.
The empty configuration uses a shared empty registry and adds no model-router-
specific DB, catalog, health, quota, provider-client, or background-task work.
The complete
`model_routers` mapping is one atomic `LIVE` reload field. Exact virtual aliases
are resolved before concrete request preflight. Sticky routers use the
process-owned bounded `ModelRouterAffinity` cache keyed by virtual model,
semantic fingerprint, and hashed explicit/automatic session identity; it never
pins provider/account state or stores raw identity/request data. The
independently callable `ModelRouterSelector` compiles a deterministic bounded
prompt and invokes `RequestCoordinator.execute()` with a child concrete
context; exact route IDs, one optional repair, default fallback, and parent
cancellation semantics are owned by that component.

Failure classification gives typed, context-qualified wire signals precedence
over generic `capability`/`unsupported` error-class fallbacks; those class
names alone never authorize migration. Strong model absence remains
model-scoped. Bounded `model ... is not available` wording is ambiguous and
can be treated as endpoint-local only when provider-scoped model knowledge and
pre-handoff alternate-surface context support that conclusion.

The optional `RequestCoordinator.outbound_observer` runs after a real upstream
`client.send` returns and receives only a sanitized structural observation. It
is intended for explicit live diagnostics, never stores raw bodies or
credential values, and must not affect retry/finalization behavior. See
`docs/live-wire-e2e.md` and `architecture/deep-dive-providers.md`.

Semantic model-router decisions are separate from provider/account routing.
Exact virtual aliases resolve before concrete model checks; the resolved target
then follows the ordinary coordinator lifecycle. Process-local router metrics
are bounded structural counters only, and `X-EggPool-Route-Session` is hashed
and dropped before upstream dispatch.

## JSON Backend

`src/eggpool/jsonx.py` — wire bodies, SSE frame helpers, and hot-path request body parsing.

- **Preferred**: `orjson` (install `eggpool[fast]`); falls back to stdlib
- **Override**: `EGGPOOL_JSON_BACKEND=orjson|stdlib|auto`
- Off the request path, stdlib `json` allowed for deterministic hashing

## Database Invariants

- SQLite WAL, single-connection serialization, `async with db.transaction():` for all DML
- A transaction is owned by exactly one asyncio task. Same-task nesting is allowed; inherited child tasks fail with a typed local database invariant before SQL.
- Busy/locked SQLite errors are classified as bounded local contention. Disk, corruption, and indeterminate connection errors close admission and terminate the worker for supervisor restart.
- `Database.vacuum()` is the only sanctioned path for `VACUUM`
- Readiness probes use `probe_writable()` with owned transactions
- Shutdown closes generation-owned request/finalization/background work, then process-owned DB users, then the primary/statistics connections; never mask an outliving task with a warning filter.
- Schema migrations in `src/eggpool/db/schema/` (numbered SQL files)
- Analytics indexes are fixed schema assets, not dashboard feature toggles;
  migration 0053 removes only the unused per-attempt status aggregate index.
- WAL residue is bounded by `journal_size_limit` when configured; the SBC
  profile defaults to 64 MiB. The pragma caps WAL file size after passive
  checkpoints without altering durability semantics.
- `DatabaseLifecycleState` transitions are diagnostic; the caller sets
  admission flags independently. `FAILED_CLOSED` is terminal and triggers
  worker restart.

The historical `requests` table is frozen for optional diagnostics. Add columns
only for durable lifecycle/accounting facts, routing repair, billing/usage truth,
or externally visible compatibility. New feature diagnostics belong in an
existing sparse diagnostic/event table or a narrowly scoped request-keyed
sidecar; disabled features create no sidecar row. Follow retention/redaction
rules and never introduce a generic EAV/property store.

## Quota and Routing

- Tier-based routing via `routing_priority`, `QuotaFairScorer`, upstream-authoritative suppression, same-tier fairness rotor
- Positive account `weight` scales effective request/token capacity within the selected eligible tier: `1.0` is baseline, `2.0` is approximately double capacity, and `0.5` is approximately half. Weight never enters cost scoring and never overrides priority or health eligibility.
- Ordered `QuotaWindow` observations use cached totals and left-edge expiry; out-of-order observations use one bounded rebuild path.
- Persisted 5h/7d/30d snapshots refresh from timestamped retained request data, preserving exact horizon boundaries for long-lived generations.
- Catalog refresh persistence is delta-based: stable semantic model/provider fields are compared outside the write transaction, compact per-account freshness lives in `catalog_refresh_state`, and steady successful pings are sampled internally while failures/transitions remain immediate.
- **Load-based, never cost-based**: request count + token count + active count + health
- Pending request/token claims are included in the existing `QuotaEstimator` reservation-load snapshot; they are not a second routing system or durable table.
- `QuotaFairScorer` does NOT consume cache fields

Routing trace batches use one `executemany` call inside the transaction owner’s
transaction. Unexpected database errors propagate to the writer or fatal
worker boundary.

## Error Hierarchy

```
AggregatorError (base)
├── ConfigError
├── ConfigValidationError (config_validation.py, not errors.py; inherits AggregatorError, not ConfigError)
│   ├── ConfigFileAccessError
│   ├── ConfigParseError
│   ├── ConfigSchemaError
│   ├── ConfigStartupAuthError
│   ├── ConfigAccountCredentialError
│   └── ConfigInternalError
├── DatabaseError
│   ├── DatabaseCommitError
│   ├── DatabaseConnectionInvalidatedError
│   ├── DatabaseRollbackError
│   ├── DatabaseTransactionOwnershipError
│   ├── ModelQuarantineHydrationError
│   └── ModelQuarantineRecoveryError
├── UpstreamError (status_code attribute)
│   ├── TemporaryUpstreamError
│   ├── TransientUpstreamError
│   ├── AuthenticationError
│   ├── QuotaExhaustedError
│   ├── RateLimitError (retry_after attribute)
│   └── ModelUnavailableError
├── ProxyError
│   └── PrematureStreamEOFError
├── ModelNotFoundError (model_id attribute)
├── NoEligibleAccountError
├── CatalogUnavailableError
├── AuthenticationUnavailableError
├── UpstreamExhaustedError
├── AccountSuspendedError
├── RequestTooLargeError
├── ModelInfoSourceFetchError
├── ContextLimitExceededError
├── CapabilityError (400 for thinking mismatches)
│   └── BudgetResolutionError (thinking budget rejection)
└── AcceptedFinalizationInvariantError (reload invariant violation)
```

Plus `RuntimeManagerLeaseExhaustedError` (RuntimeError, mapped to HTTP 503).
Plus `TranscodeLossError` (from `transcoder.errors`) — HTTP 400 when `loss_policy = "reject"`.
Plus `ProtocolMismatchError` (from `catalog.protocols`) — endpoint/model-protocol mismatch.

## Process Model

- The copyable SBC profile binds to loopback by default. LAN or wildcard binds
  are explicit operator choices and must use the existing server API key;
  authorization diagnostics and transcode warnings are metadata-only and never
  contain credential or malformed request-content bytes.

- Supervisor + Granian worker (`workers=1`), daemon mode (`--verbose` for foreground)
- `[server].threads` default `1`; values greater than `1` fail configuration
  validation because long-lived asyncio primitives are loop-bound
- `database_worker_threads=1` by default; `2` explicitly opts into a separate
  stats connection
- Readiness probe is process-owned (survives generation swaps) and disabled by
  default
- The SBC profile binds to loopback, uses low-wear analytics, and leaves
  model-info, routing traces, detailed spans, backups, event-loop
  lag, and the in-process PyPI checker dormant. The standard
  default enables the model-info sidecar while keeping the other
  optional diagnostics dormant
- Optional diagnostics are genuinely dormant when disabled: their clients,
  writers, queues, recorders, and tasks are not instantiated. The canonical
  shipped template is `config.example.toml`; `config.sbc.example.toml` is a
  loopback-by-default SBC profile. Change its bind only deliberately and with
  server API-key authentication configured.
- Model-info external enrichment is generation-owned and piggybacks on the
  leased `catalog_refresh` event. Startup runs one bounded pass when
  `model_info.startup_refresh` is enabled; recurring work uses per-row
  `next_refresh_at`, status TTLs, source TTLs, and cooldowns. There is no
  standalone `model_info_refresh` task. The compatibility-only
  `model_info.refresh_interval_s` field must not become a second scheduler;
  disabling `models.refresh_interval_s` removes recurring opportunities after
  startup.

## Runtime Generations

`RuntimeManager` owns active/retiring generation slots. Lease acquisition is fail-closed: `RuntimeManagerLeaseExhaustedError` → HTTP 503.

`RequestFinalizationSupervisor` is generation-owned. It is constructed in the
generation factory and retained jobs acquire one synchronous terminal
reference on their owning slot before registration returns. Duplicate
registration and retries do not multiply references; completion releases one
reference only after durable and required runtime convergence. Generation
close waits for both request leases and terminal references. A live retirement
deadline with unresolved terminal references invokes the existing fatal worker
handler and leaves the generation resident; process shutdown may abandon
references for startup repair after process death.

- Staged reload swap: `stage()` → `commit()`/`rollback()` → `finalize_retirement()`
- `RuntimeGenerationCandidate` owns reload-created resources; `candidate.abort()` closes in reverse order
- `RuntimeGenerationFactory` eliminates behavior drift between startup and reload
- `ReloadTransaction` state machine executes process transitions inside SQLite transaction
- Model-quarantine hydration is a publication prerequisite: successful zero-row
  reads are valid, while read or strict row-conversion failures reject startup
  or abort a reload candidate. Authoritative catalog reappearance clears the
  exact durable identity before in-memory suppression and matching backoff.

## Live Rehash

`eggpool rehash` applies provider/account/routing/model-override/model-capability changes without restart. Control socket at `~/.local/state/eggpool/eggpool.sock`.

## Health Management

`src/eggpool/health/` — `HealthManager` circuit breaker, per-account health
tracking, and bounded self-healing backoff. Every nonterminal runtime
suppression is capped at 1,800 seconds after exponential growth, provider
`Retry-After`, and jitter. Durable rows are restart hints: startup hydration
ignores expired, malformed, unknown, disabled-account, contradictory-scope,
and overlong state, while authentication and authoritative model withdrawal
remain explicit terminal states. Validated rehash resets only the changed
account's resolved credential/provider binding and its durable auth hint.
`DatabaseWritableProbe` uses real SQLite writes but is process-owned and cached
for `/readyz`.

## Request Finalization

`RequestFinalizationSupervisor` is the sole generation-owned terminal owner.
Selected request finalization, failed-attempt cleanup, and claim compensation
use kind-qualified identities, typed immutable submissions, mutable component
progress, one bounded capacity, one retry timer, and one shutdown drain.
`FinalizationData.downstream_started` is the explicit response handoff fact;
`bytes_emitted` is payload accounting only. `AttemptRuntimeLease` owns usage,
health, and account-runtime outcome obligations independently of durable request
transition, so already-terminal durable state can still converge them.
`FinalizationResult` distinguishes durable terminal state, durable transition,
reservation convergence, and runtime cleanup. The coordinator submits and
joins commands; it has no retained terminal registry or parallel capacity.

The common first-finalization path uses the repository `..._returning()`
variants for request, attempt, and reservation mutations. SQLite's returned
terminal values prove convergence without read-after-write SELECTs. A
conditional mutation that does not transition still falls back to the focused
durable read needed for idempotent replay, partial convergence, and expired
reservation handling. All three durable components remain in one correctness
transaction; compatibility boolean repository methods remain for other
callers.

The first-attempt diagnostic timestamp is captured once at the first durable
attempt boundary and stored by the existing request INSERT. The request's
`last_attempt_id` backlink is written by that same request terminal mutation,
so retryable intermediate attempts remain queryable without being presented as
the winning attempt.

Before a durable identity exists, `RuntimePublicationReceipt` owns the
provisional request/token claim and health probe. After persistence, publication
converts the provisional load to the canonical reservation in one claim-lock
transition; post-commit compensation and `AttemptRuntimeLease` own only the
components actually acquired.

Request and attempt recovery use explicit durable identities; canonical terminal
status sets live in `request/terminal_status.py`. Unknown status or identity
mismatch remains unresolved and keeps startup repair fail-closed. A selected
attempt has one retained terminal owner and one component-progress record; the
bounded supervisor is the only in-process retry owner. Bounded history is
diagnostic only and never supplies finalization correctness.

## Streaming Completion

`classify_stream_eof()` uses provider-bound `stream_completion_policy` (`strict`/`compatible`/`permissive_observe`). Only canonical OpenAI `[DONE]` or Anthropic `message_stop` is strict. Incomplete EOF → `MIDSTREAM_ERROR`, never retried after handoff.

## Thinking Control Normalization

Provider-bound `ThinkingControlContract` validates/normalizes independent
`toggle`, `effort`, and `budget` control dimensions. The catalog's shared
`parse_reasoning_options()` distinguishes absent metadata from a complete
empty list and never assigns effort token budgets from labels. Legacy `mode`
values are decode-only compatibility input. `ControlFieldAdaptation` provides
per-field dispositions. Built-in contract resolution: specificity before
priority.

Catalog source authority is field-level: manual operator overrides outrank
explicit live provider metadata, which outranks verified provider-scoped
model-info metadata; omitted facts remain unknown. Model-family names and
provider identity alone never supply reasoning-control defaults.
Routing and post-selection adaptation match each requested toggle, exact effort
label, or numeric budget against the selected provider/model row. A supported
reasoning status does not authorize every caller control, and a rejected
control is request-local rather than an upstream health failure.

## Failure Effects and Quarantine

`classify_failure_effects()` is the canonical pure classifier for one immutable
retry/effects decision. Its bounded response signal, source, provider
attribution, circuit transition, and probe-convergence fields travel unchanged
through coordinator retry, retained attempt cleanup, and finalization.
Idempotency is scoped to the durable `(proxy_request_id, attempt_id)` lifecycle:
component progress is retained by the cleanup/finalization owner and retired
after convergence. `HealthManager.record_failure()` owns a circuit failure;
the applier must not record it again. Request-local failures release a
half-open probe without provider penalties. `ModelQuarantine` remains a
bounded state machine with corroboration before terminal withdrawal.

## Background Tasks

`src/eggpool/background/` — `TaskSupervisor`, fixed-delay scheduler. Process-owned tasks survive generation swaps; generation-leased tasks retire with their generation.

## Runtime Observability

- `DispatchOverheadRecorder` — bounded rolling-window recorder for EggPool-local pre-dispatch overhead
- `LocalPreUpstreamRecorder` — full EggPool-side window from ASGI handler entry to upstream dispatch
- `DispatchSpanRecorder` — `SPAN_*` constants in `runtime_dispatch.py` with per-span p50/p95/max
- `StreamDiagnostics` — stream outcomes with bounded ring histograms
- `EventLoopLagMonitor` — bounded event-loop lag telemetry
- `MetricsWriteCoalescer` — dual locks for thread-safe buffering
- `RuntimeMetricsService.snapshot()["finalization_supervisor"]` — the active
  generation's bounded terminal-job supervisor snapshot, or `None` during
  lightweight/partial startup.
- `RuntimeMetricsService.snapshot()["finalization_ownership"]` — bounded
  generation-aware counts for active and retiring supervisors, terminal
  references, retirement age, and redacted blocked/failure facts.

For deployment comparison, use the existing runtime snapshot after a fixed
short stabilization window and keep local proxy timings separate from upstream
latency. Record the host, Python, config shape, database state, and optional
feature flags. These are descriptive manual observations, not thresholds or a
benchmark gate; workstation data must not be labeled as SBC data. A
provider-backed characterization uses only a real configured account, short
synthetic requests, and existing runtime/OS tools. If an account or target
dimension is unavailable, record it as `not measured`; do not add a benchmark,
soak harness, hardware CI, or performance threshold. Plan 126 is the retained
example of this evidence boundary.

## Database Recovery

Startup runs migrations, SQLite integrity checks, crash reconciliation, and the
required writable probe before admission. A non-`ok` integrity result, an
integrity exception, or an indeterminate runtime database state closes
readiness and exits the worker; systemd restarts it and startup repair is the
final recovery boundary. There is no same-process replacement-connection
recovery path, recovery wait gate, or cross-loop lock rebinding.

## Gotchas

- Retained tests prove capability contracts at focused seams. Historical phase
  and closure matrices are not part of routine navigation, and performance
  diagnostics are manual rather than CI gates.

- **Update targeting**: `eggpool update` without an argument follows the live latest-release path. An explicit version must be normalized, verified through PyPI's exact release endpoint, installed with a pinned requirement/tag, and verified before restart; source checkouts refuse exact targeting.

- **`fastcli` and `runtime_paths` are stdlib-only**: no transitive imports. Raspberry Pi watchdog contract.
- **`/readyz` never performs a write**: reads a cached probe snapshot.
- **Single event-loop thread is canonical**: all `asyncio.Lock` objects are loop-bound.
- **`app.state` generation-owned attributes are mirrors, not authority**: use `get_active_generation(request)` or acquire a lease.
- **Routing is load-based, not cost-based**: never `cost_microdollars`.
- **Process transitions execute inside `db.transaction()`**: atomic rollback on any failure.
- **Terminal lifecycle**: streaming 4xx paths defer terminal work to `_handle_exhausted()`.
- **SSE diagnostics**: `stream_diagnostics` exposes canonical/compatibility completion, premature EOF, HTTPX transport outcomes. Stream content and credentials are never persisted.
