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
- Persists dispatch bundles (via `DispatchPersistenceWriter` in Milestone C)
- Builds upstream URL via `_get_upstream_url()` (provider contract)
- Builds upstream headers via `_build_upstream_headers()` (provider contract)
- Applies synthetic cache controls (post-route, Phase 9)
- Manages retry/failover loop
- Handles streaming via `_build_stream_generator`
- Finalizes via `RequestFinalizer`

**Key invariants**:
- Request persisted before upstream dispatch
- Pre-body failures can retry; no retry after first downstream byte
- Every retryable failed attempt reaches terminal state before next attempt
- Same URL composition rules for catalog fetch and chat dispatch

### `request/finalizer.py` — RequestFinalizer

Persists usage, releases reservations, updates health state. Reads from `FinalizationData` using duck-typed `getattr` defaults so the transcoder module doesn't appear in unrelated callers' import paths.

### `request/attempt_finalizer.py` — AttemptFinalizer

Per-attempt finalization with idempotent reservation release. Each attempt reservation is released exactly once.

### Coordinator-retained cleanup

Retryable pre-body attempt cleanup and post-commit claim compensation are
separate coordinator-owned commands. Each records durable transition,
reservation, active-count, quota, health/probe, and completion progress before
the next await, so a later duplicate caller resumes only unfinished releases.
The durable attempt transition and reservation terminal state remain separate:
an attempt update is not evidence that its reservation is released. A caller
may proceed only after the progress record explicitly reports convergence;
normal child-task completion alone is insufficient.
Each registry is capped at 128 entries by default; capacity exhaustion fails
closed rather than creating detached work. Generation shutdown performs one
bounded drain and reports unresolved identities for the existing startup
recovery safety net. If a request waiter is cancelled during either command,
the coordinator submits the canonical `CLIENT_CANCELLED` request terminal only
after the retained command converges. Between retries, attempt-scoped
publication metadata is cleared and cancellation receives the last converged
attempt explicitly rather than reading stale context flags.

### `request/body.py` — Request Body Reader

Reads and validates incoming request bodies.

### `request/parsed_payload.py` — ParsedRequestPayload

Cached JSON parse with derived state (model, tools, messages, etc.).

### `request/limits.py` — Request Limit Enforcement

Enforces model context and output limits before dispatch.

### `request/dispatch_intent.py` / `dispatch_writer.py`

Milestone C durable dispatch write pipeline:
- `DispatchIntent`: immutable intent object
- `DispatchPersistenceWriter`: process-owned microbatching writer

Dispatch persistence is a fail-closed boundary. The repository validates
intents before `BEGIN` and either commits a complete request/reservation/
attempt bundle or raises the transaction error. The writer fans that same
failure to all batch waiters and does not count those intents as persisted.
`PersistedDispatchResult` validates its durable IDs, and the coordinator
validates them again before runtime quota/active ownership publication. The
writer is single-loop only: `start()` captures the owner loop and submissions
from another loop fail immediately.
- No upstream request sent before dispatch bundle commit acknowledged

### `request/finalization_queue.py` — FinalizationRetryQueue

Bounded retry for escaped finalizations that didn't complete.

### `request/stream_diagnostics.py` — StreamDiagnostics

Stream outcome tracking with bounded histograms.

### `request/selection_claim.py` / `selection_claim_diagnostics.py`

Milestone B selection-claim lock deconvoying. Split into three phases:
1. Phase A: circuit breaker probe + identity resolution (under lock)
2. Phase B: durable commit (outside lock)
3. Phase C: runtime state publication (under lock)

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
- Pre-body failures can retry; no retry after first downstream byte
- Every retryable failed attempt reaches terminal state before next attempt
- Each attempt reservation released exactly once via `AttemptFinalizer`
- Same URL composition rules for catalog fetch and chat dispatch
- Selection-claim lock splits DB I/O from runtime publication (Milestone B)
- Dispatch persistence is microbatched (Milestone C), not per-request transactional
