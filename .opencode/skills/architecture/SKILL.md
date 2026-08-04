---
name: architecture
description: Architecture principles and design decisions for the EggPool project. Use when understanding the codebase structure, making design decisions, or reviewing architectural changes. Covers package boundaries, request lifecycle, and core invariants.
---

# Architecture Skill

**Full design details**: `architecture/README.md` and the per-subsystem deep dives in `architecture/`.

## Core Principles

- Package boundaries must remain explicit
- Request proxying, routing, accounting, and dashboard concerns must not be combined in endpoint handlers
- Use Pydantic v2 for all data validation
- Use aiosqlite for all database operations

## Request Lifecycle

All data-plane requests flow through `RequestCoordinator`:

1. **Endpoint** (`api/chat_completions.py` or `api/messages.py`) extracts model ID, parses provider suffix
2. **Routing** selects an eligible account via quota-aware scoring (`routing/router.py`)
3. **Attempt** is persisted to SQLite before upstream dispatch
4. **Provider Contract** renders absolute URL (`compose_provider_url()`) and auth headers (`build_upstream_headers()`) from `providers/contract.py`
5. **Protocol Transcoding** (if enabled) translates the request body when the client protocol differs from the upstream protocol
6. **Proxy** sends the request via the provider's `httpx.AsyncClient` from `ProviderClientPool`
7. **Streaming** is handled by one `proxy/sse.py` decoder; the coordinator fans shared frames to `proxy/sse_observer.py` and the selected frame-level transcoder
8. **Finalization** records usage, releases reservations, updates health state

### Key Invariants

- Requests must be persisted before upstream dispatch
- Dispatch persistence is binary: a batch returns fully valid durable identities or raises; rollback never creates placeholder success results. The process-owned writer is bound to the canonical single event loop and rejects foreign-loop submissions.
- Pre-body failures can retry; no retry after first downstream byte emitted
- Every retryable failed attempt must reach terminal state through retained attempt cleanup before the next attempt
- Each attempt reservation is released exactly once via `AttemptFinalizer`
- Streaming success requires upstream protocol terminal evidence: OpenAI `[DONE]` or Anthropic `message_stop`. Use `StreamCompletionSnapshot` and `classify_stream_eof()`
- `_crash_recovery` runs at every startup and recovers ALL pending requests and active reservations

## Protocol Transcoding

Transparent request/response format conversion between OpenAI and Anthropic protocols in `src/eggpool/transcoder/`. `select_transcoder()` in `protocol.py` is the dispatch source of truth. Controlled by `[transcoder]` config; on by default.

- **Streaming hot path**: one bounded `SSEDecoder` per upstream stream, synchronous `translate_frame()`/`finish()`, compact JSON separators `(",",":")`, lazy JSON-object parse cache
- **Provider payload lifecycle**: `ProviderBoundRequest` is the sole provider-payload authority after client parsing. Copy-on-write generation-aware mutations, one final serialization cache, frozen before dispatch. `ProxyRequestContext.upstream_body` is a compatibility mirror only.
- The transcoder's `usage` property returns a default; finalization must read usage from the coordinator's observer

## JSON Backend

`src/eggpool/jsonx.py` — wire bodies, SSE frame helpers, and hot-path request body parsing.

- **Preferred**: `orjson` (install `eggpool[fast]`); falls back to stdlib
- **Override**: `EGGPOOL_JSON_BACKEND=orjson|stdlib|auto`
- Off the request path, stdlib `json` allowed for deterministic hashing

## Database Invariants

- SQLite WAL, single-connection serialization, `async with db.transaction():` for all DML
- `Database.vacuum()` is the only sanctioned path for `VACUUM`
- Readiness probes use `probe_writable()` with owned transactions
- Schema migrations in `src/eggpool/db/schema/` (numbered SQL files)

## Quota and Routing

- Tier-based routing via `routing_priority`, `QuotaFairScorer`, upstream-authoritative suppression, same-tier fairness rotor
- Ordered `QuotaWindow` observations use cached totals and left-edge expiry; out-of-order observations use one bounded rebuild path.
- Persisted 5h/7d/30d snapshots refresh from timestamped retained request data, preserving exact horizon boundaries for long-lived generations.
- **Load-based, never cost-based**: request count + token count + active count + health
- `QuotaFairScorer` does NOT consume cache/compression fields

Routing trace batches use one `executemany` call inside the transaction owner’s
transaction. Unexpected database errors propagate to the writer/recovery path.

## Error Hierarchy

```
AggregatorError (base)
├── ConfigError
│   └── ConfigValidationError
├── DatabaseError
│   ├── DatabaseCommitError
│   ├── DatabaseConnectionInvalidatedError
│   └── DatabaseRollbackError
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
└── AcceptedFinalizationInvariantError
```

Plus `RuntimeManagerLeaseExhaustedError` (RuntimeError, mapped to HTTP 503).

## Process Model

- Supervisor + Granian worker (`workers=1`), daemon mode (`--verbose` for foreground)
- `[server].threads` default `4` (values > 1 emit startup warning)
- `database_worker_threads=2`
- Readiness probe is process-owned (survives generation swaps)

## Runtime Generations

`RuntimeManager` owns active/retiring generation slots. Lease acquisition is fail-closed: `RuntimeManagerLeaseExhaustedError` → HTTP 503.

- Staged reload swap: `stage()` → `commit()`/`rollback()` → `finalize_retirement()`
- `RuntimeGenerationCandidate` owns reload-created resources; `candidate.abort()` closes in reverse order
- `RuntimeGenerationFactory` eliminates behavior drift between startup and reload
- `ReloadTransaction` state machine executes process transitions inside SQLite transaction

## Live Rehash

`eggpool rehash` applies provider/account/routing/model-override/model-capability changes without restart. Control socket at `~/.local/state/eggpool/eggpool.sock`.

## Health Management

`src/eggpool/health/` — `HealthManager` circuit breaker, per-account health tracking, `DatabaseWritableProbe` (real SQLite write probes, cached for `/readyz`).

## Request Finalization

`RequestFinalizationJob` keyed by `(proxy_request_id, attempt_id)`. `RequestFinalizationSupervisor` is the sole process-owned retry owner and uses one bounded timer with configured retry age/backoff. `FinalizationData.downstream_started` is the explicit response handoff fact; `bytes_emitted` is payload accounting only. `AttemptRuntimeLease` owns usage, health, and account-runtime outcome obligations independently of durable request transition, so already-terminal durable state can still converge them. `FinalizationResult` distinguishes durable terminal state, durable transition, reservation convergence, and runtime cleanup, and projects completed lease markers even when a later runtime component is still retry-pending. Retryable attempts use coordinator-retained cleanup with 128-entry capacity.

Request and attempt recovery descriptors use distinct strategies and explicit
identities; canonical terminal status sets live in
`request/terminal_status.py`. Unknown status or identity mismatch remains
unresolved and keeps recovery fail-closed.

The stale-request safety net remains bounded and accounting-focused after its
durable transition: it preserves one active-count unit per transitioned
request, aggregates decrements by account, and releases every owned quota
dimension even when reserved monetary cost is zero. The router bulk decrement
API clamps underflow to zero while logging the invariant violation.

## Streaming Completion

`classify_stream_eof()` uses provider-bound `stream_completion_policy` (`strict`/`compatible`/`permissive_observe`). Only canonical OpenAI `[DONE]` or Anthropic `message_stop` is strict. Incomplete EOF → `MIDSTREAM_ERROR`, never retried after handoff.

## Thinking Control Normalization

Provider-bound `ThinkingControlContract` validates/normalizes thinking controls. `ControlFieldAdaptation` provides per-field dispositions. Built-in contract resolution: specificity before priority.

## Failure Effects and Quarantine

`classify_failure_effects()` centralizes consequences. `ModelQuarantine` — bounded state machine with corroboration before terminal withdrawal.

## Background Tasks

`src/eggpool/background/` — `TaskSupervisor`, fixed-delay scheduler. Process-owned tasks survive generation swaps; generation-leased tasks retire with their generation.

## Runtime Observability

- `DispatchOverheadRecorder` — bounded rolling-window recorder for EggPool-local pre-dispatch overhead
- `LocalPreUpstreamRecorder` — full EggPool-side window from ASGI handler entry to upstream dispatch
- `DispatchSpanRecorder` — ~22 `SPAN_*` constants with per-span p50/p95/max
- `StreamDiagnostics` — stream outcomes with bounded ring histograms
- `EventLoopLagMonitor` — bounded event-loop lag telemetry
- `MetricsWriteCoalescer` — dual locks for thread-safe buffering
- `RuntimeMetricsService.snapshot()["finalization_supervisor"]` — the active
  generation's bounded terminal-job supervisor snapshot, or `None` during
  lightweight/partial startup.

## Database Recovery

`DatabaseRecoveryController` — single-flight recovery, bounded retry, transaction reconciliation.

## Gotchas

- **Update targeting**: `eggpool update` without an argument follows the live latest-release path. An explicit version must be normalized, verified through PyPI's exact release endpoint, installed with a pinned requirement/tag, and verified before restart; source checkouts refuse exact targeting.

- **`fastcli` and `runtime_paths` are stdlib-only**: no transitive imports. Raspberry Pi watchdog contract.
- **`/readyz` never performs a write**: reads a cached probe snapshot.
- **Single event-loop thread is canonical**: all `asyncio.Lock` objects are loop-bound.
- **`app.state` generation-owned attributes are mirrors, not authority**: use `get_active_generation(request)` or acquire a lease.
- **Routing is load-based, not cost-based**: never `cost_microdollars`.
- **Process transitions execute inside `db.transaction()`**: atomic rollback on any failure.
- **Terminal lifecycle**: streaming 4xx paths defer terminal work to `_handle_exhausted()`.
- **SSE diagnostics**: `stream_diagnostics` exposes canonical/compatibility completion, premature EOF, HTTPX transport outcomes. Stream content and credentials are never persisted.
