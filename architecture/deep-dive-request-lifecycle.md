# Deep Dive: Request Lifecycle

Back to [Overview](overview.md)

## Purpose

The request lifecycle is the data-plane hot path: every chat completion and messages request flows through `RequestCoordinator` from endpoint to finalization.

## Request Flow

```
Client Request
    │
    ▼
┌─────────────────────────────────────────┐
│ 1. API Endpoint                         │
│    api/chat_completions.py (OpenAI)     │
│    api/messages.py (Anthropic)          │
│    → extract model ID, parse provider   │
└──────────────┬──────────────────────────┘
               │
    ┌──────────▼──────────┐
    │ 2. Body Parsing     │
    │    request/body.py  │
    │    parsed_payload.py│
    │    → ParsedRequest  │
    └──────────┬──────────┘
               │
    ┌──────────▼──────────┐
    │ 3. Segmentation     │
    │    transcoder/      │
    │    segmentation.py  │
    │    → stable/semi/   │
    │      volatile       │
    └──────────┬──────────┘
               │
    ┌──────────▼──────────┐
    │ 4. Compression      │
    │    compression/     │
    │    analyze + apply  │
    │    (observational   │
    │     by default)     │
    └──────────┬──────────┘
               │
    ┌──────────▼──────────┐
    │ 5. Routing          │
    │    routing/router.py│
    │    → select account │
    └──────────┬──────────┘
               │
    ┌──────────▼──────────┐
    │ 6. Persistence      │
    │    db/              │
    │    → request +      │
    │      attempt +      │
    │      routing_decision│
    └──────────┬──────────┘
               │
    ┌──────────▼──────────┐
    │ 7. Provider Contract│
    │    providers/       │
    │    contract.py      │
    │    → URL + headers  │
    └──────────┬──────────┘
               │
    ┌──────────▼──────────┐
    │ 8. Transcoding      │
    │    transcoder/      │
    │    → body translation│
    └──────────┬──────────┘
               │
    ┌──────────▼──────────┐
    │ 9. Proxy Dispatch   │
    │    proxy/           │
    │    → httpx send     │
    └──────────┬──────────┘
               │
    ┌──────────▼──────────┐
    │ 10. Streaming       │
    │     proxy/sse_      │
    │     observer.py     │
    │     → SSE parse +   │
    │       usage extract │
    └──────────┬──────────┘
               │
    ┌──────────▼──────────┐
    │ 11. Finalization    │
    │     request/        │
    │     finalizer.py    │
    │     → usage persist,│
    │       reservation   │
    │       release,      │
    │       health update │
    └─────────────────────┘
```

## Key Modules

### `request/coordinator.py` — RequestCoordinator

The orchestrator. Wires together all lifecycle stages:

- Receives `ProxyRequestContext` from endpoint handlers
- Calls `Router.select_accounts_for_failover()` for routing
- Persists the request/reservation/attempt bundle in one transaction
- Builds upstream URL via `_get_upstream_url()` (provider contract)
- Builds upstream headers via `_build_upstream_headers()` (provider contract)
- Manages retry/failover loop
- Handles streaming via `_build_stream_generator`
- Finalizes via `RequestFinalizer`

**Key invariants**:
- Request persisted before upstream dispatch
- Dispatch stages have narrow exception ownership: local preparation, request construction/serialization, and client-facing adaptation faults are local terminal errors; only typed HTTPX transport faults are retry candidates.
- Retry uses distinct accounts only, converges failed-attempt cleanup before reselection, and stops at `min(distinct eligible accounts, 1 + max_retries_before_stream)` without sleeping. A ceiling-truncated traversal records `attempt_ceiling_reached`.
- Response handoff is an explicit `downstream_started` fact marked immediately before forwarding ASGI `http.response.start`; no retry occurs after handoff, even when zero payload bytes have been emitted. Body-byte accounting is separate.
- Non-streaming response adaptation completes before durable `COMPLETED`; native pass-through may retain invalid JSON, but required transcoded response adaptation must succeed first.
- Every retryable failed attempt reaches terminal state before next attempt
- Same URL composition rules for catalog fetch and chat dispatch

### Dispatch exception boundaries

The coordinator treats request preparation, transport, response handling, and
client-facing adaptation as separate stages. Generic exceptions retain their
stage and become bounded local errors after selected ownership is finalized;
they do not become provider health evidence. HTTPX transport exceptions are
classified from their type and can fail over only before handoff;
`asyncio.CancelledError` propagates normally.

For non-streaming responses, `ParsedUpstreamResponse` is built once after the
body read. Usage extraction is best effort, native protocol bodies can remain
pass-through, and required response transcoding happens before durable success
finalization. For streams, the upstream response is closed on success,
cancellation, premature EOF, transport failure, or local frame translation
failure.

### `request/finalizer.py` — RequestFinalizer

Persists usage, releases reservations, updates health state. Reads from `FinalizationData` using duck-typed `getattr` defaults so the transcoder module doesn't appear in unrelated callers' import paths.

### `request/attempt_finalizer.py` — AttemptFinalizer

Per-attempt finalization with idempotent reservation release. Each attempt reservation is released exactly once.

### Generation-owned terminal commands

Retryable pre-body attempt cleanup and post-commit claim compensation are
typed commands in the same generation-owned `RequestFinalizationSupervisor`
as selected-request finalization. Each records durable transition,
reservation, active-count, quota, health/probe, and completion progress before
the next await, so a later duplicate caller resumes only unfinished releases.
The durable attempt transition and reservation terminal state remain separate:
an attempt update is not evidence that its reservation is released. Reselection
may proceed only after the supervisor reports convergence; normal child-task
completion alone is insufficient. One global 128-entry capacity, retry timer,
diagnostics surface, and shutdown drain cover all three command kinds. If a
request waiter is cancelled during cleanup, it joins the existing supervisor
command and submits the canonical `CLIENT_CANCELLED` request terminal only
after convergence. The coordinator has no parallel retained registry.

There is no age-based runtime stale-request safety net. Startup crash
reconciliation repairs durable rows left by a prior process, while the
retained terminal owner converges live requests and reservations. Router bulk
decrements log and clamp underflow rather than permitting a
negative count.

### `request/body.py` — Request Body Reader

Reads and validates incoming request bodies. The whole JSON body is bounded by
`[server].max_request_body_bytes` (10 MiB by default) before parsing; provider
document/media limits are subsequent, narrower constraints.

### `request/parsed_payload.py` — ParsedRequestPayload

Cached JSON parse with derived state (model, tools, messages, etc.) through
`eggpool.jsonx`, preserving the selected stdlib/orjson backend.

### Provider payload ownership

`ProxyRequestContext` owns the accepted client bytes and parsed payload until
the selected attempt is prepared. `ProviderBoundRequest` treats that graph as
logically immutable. Narrow changes use path-level copy-on-write, while
unknown mutators use a conservative deep-owning helper. EggPool-owned
path-level transforms use the explicit `adopt_provider_payload()` boundary;
safe compression therefore retains unchanged subtree identity instead of
being recursively rematerialized at provider binding. If no body semantics
change, the accepted client bytes are sent unchanged. Once an upstream
response is chosen and the stream is handed off, dispatch-only bytes, parsed
state, and provider payload buffers are released; scalar body-size/accounting
metadata remains for finalization.

Selected-provider thinking-control adaptation (`adapt_thinking_controls`) is
called with the current provider-bound payload as a read-only `Mapping` and
adopts changed results through `adopt_provider_payload(reason="thinking_control")`.
The adapter builds its own shallow-copied working root, so the source graph
is never mutated and unchanged descendants (`messages`, `tools`, etc.) retain
their identity. No-op adaptation leaves `payload_generation` unchanged and
preserves the cached provider bytes. The `mutate_provider_payload()` arbitrary
mutator was removed (Plan 121) because no production caller remained; the
explicit narrow ownership primitives (`mutate_top_level_mapping`,
`adopt_provider_payload`, `set_provider_payload`) cover the narrowed surface.
`provider_payload_copy()` and `replace_provider_payload()` remain as
conservative helpers for the upstream protocol transcode path, which is
intentionally off the corrected hot path.

Prepared transcode results retain one request-local translated JSON generation
without recursively freezing or rematerializing it. Valid unchanged reuse
adopts that generation through `adopt_provider_payload()` and attaches the
already encoded body. Later provider-specific changes use the normal path-level
copy-on-write or conservative owning APIs, so the prepared source generation is
not mutated. Prepared graphs are discarded with the request and are never a
cross-request cache.

### `request/limits.py` — Request Limit Enforcement

Enforces model context and output limits before dispatch. ASCII-heavy decoded
strings use a native `str.isascii()` fast path while preserving the existing
four-characters-per-token estimate. When an effective input/context limit is
enforced, `check_context_limits()` returns the exact decoded-payload estimate
it used so the endpoint can carry it into `ProxyRequestContext` without a
second recursive walk. Unbounded/no-enforcement models leave that optional
field unset because routing admission uses the separate bounded reservation
estimate. Translated tool-schema allowance is added to the byte-floor
arithmetic through `extra_input_tokens`; rough tool padding reuses the shared
decoded structural estimator and never serializes each tool independently.
The preflight keeps the encoded provider body unchanged and never materializes
synthetic zero-byte padding.

### `request/finalization_job.py` — RequestFinalizationSupervisor

The supervisor is the single generation-owned terminal retry owner. It retains
selected jobs and kind-qualified terminal commands by
`(proxy_request_id, attempt_id, command_kind)`, reports structured convergence
facts, and schedules bounded retryable failures through one timer using
configured backoff and an absolute execution-time maximum retry age. Each selected job
carries an `AttemptRuntimeLease` made from publication facts, and runtime
cleanup resumes component-by-component after durable convergence. Capacity
rejects before ownership transfer; there is no detached terminal task.
`FinalizationResult` is refreshed from the lease after both successful and
failed convergence attempts, so completed active-count, quota, health, or
probe components remain visible while an outstanding component is retried.
When the lease is released, transient runtime-retry metadata is cleared so the
completed result is non-retryable and has no stale cleanup detail.

### `request/stream_diagnostics.py` — StreamDiagnostics

Stream outcome tracking with bounded histograms.

### `RequestCoordinator` selection claim and diagnostics

The coordinator owns the selection-claim lock and keeps database I/O outside
the lock. Selection diagnostics are process-wide bounded counters exposed in
the runtime snapshot; there is no separate claim state-machine module.

### `request/routing_trace_guard.py` — RoutingTraceGuard

Pre-enqueue pressure signal for routing trace writes.

## API Endpoints

### `api/chat_completions.py`

`POST /v1/chat/completions` — OpenAI protocol handler. Extracts model ID, parses provider suffix, delegates to `RequestCoordinator`.

### `api/messages.py`

`POST /v1/messages` — Anthropic protocol handler. Same flow, Anthropic request/response format.

### `api/proxy_request.py`

Core proxy handler. Orchestrates:
- Body parsing + validation
- Segmentation (observational)
- Compression (observational or safe mode)
- Transcoding preflight
- Coordinator dispatch

### `api/models.py`

`GET /v1/models` — Lists available models with provider-suffixed IDs.

### `api/model_info.py`

`/api/model-info/*` — Enriched model metadata endpoints.

### `api/stats.py`

`/api/stats/*` — All stats/dashboard JSON endpoints.

### `api/errors.py`

API error rendering (400/404/429/503). Maps exception hierarchy to HTTP status codes.

## Proxy Layer

### `proxy/client.py`

`build_upstream_headers()` — header sanitization + auth injection in single pass.

### `proxy/sse.py` / `proxy/sse_observer.py` — shared framing and observation

`SSEDecoder` owns bounded UTF-8/SSE framing. The coordinator fans each shared
frame to `IncrementalSSEObserver` for usage/completion evidence and to a
frame-level transcoder when protocols differ.

### `proxy/usage.py` — StreamUsageResult

Streaming usage accumulation across chunks.

### `proxy/normalized_usage.py`

`normalize_usage()` — provider-neutral cache counter classification.

## Key Invariants

- Requests persisted before upstream dispatch
- Pre-handoff failures can retry; streaming handoff occurs at ASGI
  `http.response.start`, so no retry occurs after response start. An empty
  started stream is post-handoff; `bytes_emitted` is payload accounting only.
- Every retryable failed attempt reaches terminal state before next attempt
- Each attempt reservation released exactly once via `AttemptFinalizer`
- Same URL composition rules for catalog fetch and chat dispatch
- Selection-claim lock splits DB I/O from runtime publication (Milestone B)
- Dispatch persistence is one direct per-request transaction
