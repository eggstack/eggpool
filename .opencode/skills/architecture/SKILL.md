---
name: architecture
description: Architecture principles and design decisions for the EggPool project. Use when understanding the codebase structure, making design decisions, or reviewing architectural changes. Covers package boundaries, request lifecycle, and core invariants.
---

# Architecture Skill

**Full design details**: `architecture/README.md` and the per-subsystem deep dives in `architecture/`.

## Core Principles

- Database transactions use one shared connection and one asyncio-task owner; rollback is private to the owning transaction path.

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
- Retries use distinct accounts only, converge failed-attempt cleanup before reselection, and stop at `min(distinct eligible accounts, 1 + max_retries_before_stream)`. The request records `attempt_ceiling_reached` when the configured ceiling leaves eligible accounts unattempted.
- Pre-handoff failures can retry; `downstream_started` becomes true when the proxy forwards ASGI `http.response.start`, before body iteration, so no retry is possible after response handoff. `asyncio.CancelledError` propagates. Empty started streams are post-handoff even with zero body bytes.
- Non-streaming response adaptation completes before durable `COMPLETED`; native invalid JSON may pass through when usage is optional, while required transcoded responses must adapt successfully.
- Every retryable failed attempt must reach terminal state through retained attempt cleanup before the next attempt
- Each attempt reservation is released exactly once via `AttemptFinalizer`
- Streaming success requires upstream protocol terminal evidence: OpenAI `[DONE]` or Anthropic `message_stop`. Use `StreamCompletionSnapshot` and `classify_stream_eof()`
- `_crash_recovery` runs at every startup and repairs pending requests and active reservations left by a previous process. Normal request handling has no age-only stale sweep.

## Protocol Transcoding

Transparent request/response format conversion between OpenAI and Anthropic protocols in `src/eggpool/transcoder/`. `select_transcoder()` in `protocol.py` is the dispatch source of truth. Controlled by `[transcoder]` config; on by default.

- **Streaming hot path**: one bounded `SSEDecoder` per upstream stream, synchronous `translate_frame()`/`finish()`, compact JSON separators `(",",":")`, lazy JSON-object parse cache
- **Request preparation hot path**: ASCII-only context strings use the native `str.isascii()` path; translated tool allowance is passed as estimator arithmetic rather than synthetic bytes. Provider IDs and trusted proxies are immutable, generation-owned lookup sets used directly by leased requests.
- **Provider payload lifecycle**: `ProviderBoundRequest` is the sole provider-payload authority after client parsing. Copy-on-write generation-aware mutations, one final serialization cache, frozen before dispatch. Original client bytes remain separate from the provider-bound payload.
- The transcoder's `usage` property returns a default; finalization must read usage from the coordinator's observer

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
- Schema migrations in `src/eggpool/db/schema/` (numbered SQL files)

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
- `QuotaFairScorer` does NOT consume cache/compression fields

Routing trace batches use one `executemany` call inside the transaction owner’s
transaction. Unexpected database errors propagate to the writer or fatal
worker boundary.

## Error Hierarchy

```
AggregatorError (base)
├── ConfigError
│   └── ConfigValidationError (config_validation.py, not errors.py)
│       ├── ConfigFileAccessError
│       ├── ConfigParseError
│       ├── ConfigSchemaError
│       ├── ConfigStartupAuthError
│       ├── ConfigAccountCredentialError
│       └── ConfigInternalError
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

- Supervisor + Granian worker (`workers=1`), daemon mode (`--verbose` for foreground)
- `[server].threads` default `1`; values greater than `1` fail configuration
  validation because long-lived asyncio primitives are loop-bound
- `database_worker_threads=1` by default; `2` explicitly opts into a separate
  stats connection
- Readiness probe is process-owned (survives generation swaps) and disabled by
  default
- The lean default binds to loopback, uses low-wear analytics, and leaves
  model-info, routing traces, detailed spans, backups, DNS caching, event-loop
  lag, and the in-process PyPI checker dormant
- Optional diagnostics are genuinely dormant when disabled: their clients,
  writers, queues, recorders, and tasks are not instantiated. The canonical
  shipped template is `config.example.toml`; `config.sbc.example.toml` is an
  explicit LAN/SBC profile.

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

Provider-bound `ThinkingControlContract` validates/normalizes thinking controls. `ControlFieldAdaptation` provides per-field dispositions. Built-in contract resolution: specificity before priority.

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
- `DispatchSpanRecorder` — ~22 `SPAN_*` constants with per-span p50/p95/max
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
benchmark gate; workstation data must not be labeled as SBC data.

## Database Recovery

Startup runs migrations, SQLite integrity checks, crash reconciliation, and the
required writable probe before admission. A non-`ok` integrity result, an
integrity exception, or an indeterminate runtime database state closes
readiness and exits the worker; systemd restarts it and startup repair is the
final recovery boundary. There is no same-process replacement-connection
recovery path, recovery wait gate, or cross-loop lock rebinding.

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
