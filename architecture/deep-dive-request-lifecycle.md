# Deep Dive: Request Lifecycle

Back to [Overview](overview.md)

## Purpose

The request lifecycle is the data-plane hot path: every Chat Completions,
Responses, and Messages request flows through `RequestCoordinator` from
endpoint to finalization.

After account/provider selection and before provider-body serialization, the
coordinator resolves the generation's declared wire profile through the
process-owned `WireProfileResolver`. The selected profile supplies both the
URL path template and surface-specific authentication shape. A successful
ordinary request refreshes the provider/model preference. Alternate-surface
enumeration is not inferred from HTTP status; it remains gated by the
canonical failure-effects transition and shares the request's existing
submission budget.

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
     │ 4. Routing          │
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

The orchestrator. Wires together all lifecycle stages as a thin sequencing
facade.  Implementation details are delegated to focused helper modules:

- Receives `ProxyRequestContext` from endpoint handlers
- Calls `Router.build_routing_plan()` for routing
- Persists the request/reservation/attempt bundle in one transaction
- Builds upstream URL via `_get_upstream_url()` (delegates to `upstream_helpers.py`)
- Builds upstream headers via `_build_upstream_headers()` (provider contract)
- Manages retry/failover loop
- Handles streaming via `_build_stream_generator`
- Finalizes via `RequestFinalizer`
- Post-selection thinking control adaptation delegates to `thinking_adaptation.py`
- Upstream failure observation/classification delegates to `failure_helpers.py`
- Endpoint validation and protocol resolution delegate to `upstream_helpers.py`
- Static/timing helpers delegate to `static_helpers.py`
- Usage extraction delegates to `usage_helpers.py`
- Routing trace payloads delegate to `routing_helpers.py`
- Backoff persistence delegates to `backoff_persistence.py`
- Upstream request dispatch delegates to `upstream_execution.py`
- Claim lifecycle compensation delegates to `claim_lifecycle.py`

**Key invariants**:
- Request persisted before upstream dispatch
- Dispatch stages have narrow exception ownership: local preparation, request construction/serialization, and client-facing adaptation faults are local terminal errors; only typed HTTPX transport faults are retry candidates.
- Retry consumes one shared upstream-submission budget of `1 + max_retries_before_stream`, including alternate-wire submissions. The normal destination is another account on the same wire; a deterministic wire rejection may instead reopen the same account for the next wire candidate. Failed-attempt cleanup converges before reselection, and a ceiling-truncated traversal records `attempt_ceiling_reached`.
- A bare or unknown 401 is not credential evidence: it is returned without account disablement, health penalty, or failover. Explicit credential invalidity may disable only the selected account; deterministic auth/surface/schema mismatch may reject only the selected wire candidate before downstream handoff.
- Response handoff is an explicit `downstream_started` fact marked immediately before forwarding ASGI `http.response.start`; no retry occurs after handoff, even when zero payload bytes have been emitted. Body-byte accounting is separate.
- Non-streaming response adaptation completes before durable `COMPLETED`; same-surface byte paths may retain invalid JSON, but a selected alternate wire codec must decode and re-encode successfully first.
- Every retryable failed attempt reaches terminal state before next attempt
- Same URL composition rules for catalog fetch and chat dispatch
- Canonical request and reasoning intent are captured before provider-specific
  adaptation; retries rebuild from the original client source, never from a
  prior translated payload.

### Dispatch exception boundaries

The coordinator treats request preparation, transport, response handling, and
client-facing adaptation as separate stages. Generic exceptions retain their
stage and become bounded local errors after selected ownership is finalized;
they do not become provider health evidence. HTTPX transport exceptions are
classified from their type and can fail over only before handoff;
`asyncio.CancelledError` propagates normally.

For non-streaming responses, `ParsedUpstreamResponse` is built once after the
body read. Usage extraction is best effort, native protocol bodies can remain
pass-through when no adaptation is required, and a selected wire codec must
adapt alternate response grammars before durable success finalization. For
streams, the upstream response is closed on success,
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
completion alone is insufficient. One global 256-entry capacity, retry timer,
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

### `request/thinking_adaptation.py` — Post-selection thinking controls

Extracted from `RequestCoordinator` in Plan 136 Phase 5.  Owns provider-specific
thinking control resolution, budget recompute, and control normalization — the
post-selection preparation stage that runs after account selection but before
upstream dispatch.  Functions receive dependencies explicitly (catalog, policy,
config) rather than referencing coordinator state.  Strict-policy rejections
propagate as `CapabilityError`; callers must finalize the attempt before
re-raising.

- `resolve_selected_thinking_capability()` — best-effort catalog lookup
- `client_has_thinking_controls()` — pure detection of thinking/reasoning fields
- `recompute_thinking_budget_for_provider()` — re-resolves budget against selected provider's capability
- `adapt_provider_thinking_controls()` — validates and adapts controls against the provider contract

`_determine_thinking_rejection_status()` (coordinator) — attributes a
thinking rejection to an aggregated capability status when all
eligible accounts are filtered out. Consults the collapsed
`models` row first, then falls back to the provider-scoped row when
`context.provider_id` is known, and otherwise iterates every
provider entry returned by `cache.get_provider_model_entries()` —
including quarantined accounts — and aggregates the most permissive
status. Most-permissive order: `supported` > `mixed` >
`unsupported` > `unknown`. Returns `"unknown"` or `"unsupported"`
only when the aggregated status is genuinely authoritative;
otherwise returns `None` so the caller falls through to the
generic transient `no_eligible_providers` reason.

### `request/upstream_helpers.py` — Upstream URL and endpoint validation

Extracted from `RequestCoordinator` in Plan 136 Phase 5.  Resolves the absolute
upstream URL from provider configuration and validates that the client endpoint
matches the model's supported protocols.

- `get_upstream_url()` — URL composition via `compose_provider_url()`
- `resolve_upstream_protocol()` — protocol resolution for transcoding
- `validate_endpoint_or_transcode()` — endpoint/protocol mismatch detection and resolution

### `request/failure_helpers.py` — Failure observation and classification

Extracted from `RequestCoordinator` in Plan 136 Phase 5.  Normalizes upstream
failures into typed observations, classifies them into retry/effects decisions,
and maps the result to the public upstream error hierarchy.

- `build_failure_observation()` — single normalization point for upstream failures
- `error_from_failure_effects()` — maps canonical decisions to public error types
- `classify_upstream_failure()` — combined observation + classification + error mapping
- `classify_upstream_error()` — lightweight classifier for error status codes

### `request/static_helpers.py` — Pure static and timing helpers

Extracted from `RequestCoordinator` in Plan 136 Phase 5.  Stateless utilities
for header lookups, timing calculations, error-to-status-code mapping, and
local error response construction.  No coordinator state dependency.

- `get_header_value()` — case-insensitive header lookup by name or list of names
- `elapsed_ms()` — monotonic request latency
- `upstream_read_ms()` / `upstream_header_ms()` / `coordinator_overhead_ms()` — timing decomposition
- `error_status_code()` — exception-to-HTTP-status mapping
- `build_local_error_response()` — protocol-shaped error without exception text
- `close_response()` — safe upstream response cleanup

### `request/usage_helpers.py` — Usage extraction from upstream responses

Extracted from `RequestCoordinator` in Plan 136 Phase 5.  Parses non-streaming
response bodies and pre-parsed responses to extract token usage data.

- `extract_non_stream_usage()` — JSON parse + protocol-specific usage extraction
- `extract_non_stream_usage_from_parsed()` — reuses pre-parsed dict to avoid duplicate decode

### `request/routing_helpers.py` — Routing trace observation

Extracted from `RequestCoordinator` in Plan 136 Phase 5.  Builds the
score-components payload and tie-break summary for routing decision
observability.  Purely computational, no coordinator state dependency.

- `build_score_components()` — full routing decision payload for dashboard
- `build_top_candidates()` — top-N ranked candidates with fairness band positions
- `derive_tie_break_summary()` — decisive factor between selected and runner-up

### `request/backoff_persistence.py` — Durable backoff state

Extracted from `RequestCoordinator` in Plan 136 Phase 5.  Persists and clears
account backoff rows in SQLite so suppression survives restarts.

- `persist_backoff()` — upserts failure backoff with account ID resolution
- `clear_backoff()` — removes backoff rows on successful requests

### `request/upstream_execution.py` — Upstream request dispatch

Extracted from `RequestCoordinator` in Plan 136 Phase 5.  Sends the upstream
HTTP request and captures shared dispatch timing metrics.

- `send_upstream_request()` — timing-instrumented `client.send()` with connect/header recording

### `request/claim_lifecycle.py` — Claim compensation

Extracted from `RequestCoordinator` in Plan 136 Phase 5.  Releases provisional
claim ownership and performs stepwise compensation when post-commit publication
fails.  Receives dependencies explicitly rather than referencing coordinator state.

- `release_unpublished_claim()` — synchronous provisional claim release (quota + health probe)
- `run_claim_compensation()` — stepwise compensation runner for committed claims

### Provider payload ownership

`ProxyRequestContext` owns the accepted client bytes and parsed payload until
the selected attempt is prepared. `ProviderBoundRequest` treats that graph as
logically immutable. Narrow changes use path-level copy-on-write, while
unknown mutators use a conservative deep-owning helper. EggPool-owned
path-level transforms use the explicit `adopt_provider_payload()` boundary.
If no body semantics
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
Cross-protocol body encoders receive the provider-bound payload directly as a
read-only `Mapping` and construct a fresh target graph. The coordinator adopts
that graph through `adopt_provider_payload(reason="protocol_transcode")`; it
does not recursively copy the source first or rematerialize the translated
graph afterward. `replace_provider_payload()` remains as a conservative
conditional ownership primitive for compatibility callers; the former
`provider_payload_copy()` transcode helper was removed in Plan 124.

### Dispatch-freeze lifecycle

`serialize_provider_payload()` serializes the current generation and
sets a dispatch-freeze flag (`_frozen`). Once frozen, mutations that
do **not** begin a new generation — `replace_provider_payload()` and
`set_provider_payload(increment_generation=False)` — raise
`RuntimeError("provider payload is frozen")` because they cannot
safely overwrite a body that downstream dispatch already consumed.

Methods that **begin a new generation** — `set_provider_payload(increment_generation=True)` and `adopt_provider_payload(increment_generation=True)` — reset `_frozen` alongside the cached serialized-bytes. The new generation is the canonical provider payload, and the next `serialize_provider_payload()` call re-encodes from scratch. This is what allows the post-selection cross-protocol transcoder (`_apply_selected_provider_transcode`) to replace a body that the previous attempt already serialized and dispatched — a retry that selects a different provider must rebuild from the original client payload, but the transcoder may also need to replace the previously adopted transcode result when account failover picks a different upstream. `adopt_provider_payload(increment_generation=False)` still raises when
frozen: it does not begin a new generation.

### Thinking rejection attribution

When all eligible accounts are filtered out (including quarantine) and
the request carries thinking controls,
`_determine_thinking_rejection_status()` attributes the rejection to a
capability status. Per-provider thinking overrides (e.g. the bundled
OpenCode Go host capabilities for `muse-spark-1.2-contributor`)
live in the provider-scoped cache row, **not** the collapsed
`models` row. `cache.get_model()` deliberately does **not** apply
overrides; `cache.get_provider_model_entry()` and
`cache.get_provider_model_entries()` do.

When `context.provider_id` is unset, the method iterates every
provider entry for the model and aggregates the most permissive
status. Quarantine does **not** erase a provider's capability —
quarantined entries still contribute to the aggregated status. A
400 (`CapabilityError`) is surfaced only when the aggregated status
is genuinely `unknown` or `unsupported`; when the status is
`supported` or `mixed` but every supporting account is currently
quarantined, the caller falls through to `ModelUnavailableError`
(503) / `UpstreamExhaustedError` so a transient retry is possible.
This prevents a misleading `thinking capability status: unknown`
400 from masking a recoverable quarantine-state failure.

Prepared transcode results retain one request-local translated JSON generation
without recursively freezing or rematerializing it. Valid unchanged reuse
adopts that generation through `adopt_provider_payload()` and attaches the
already encoded body. Later provider-specific changes use the normal path-level
copy-on-write or conservative owning APIs, so the prepared source generation is
not mutated. Prepared graphs are discarded with the request and are never a
cross-request cache.

For cross-protocol requests carrying **provider-sensitive media** (images,
documents, audio, or media inside tool results), Plan 141 explicitly defers
the definitive translation to **after** `SelectedAttempt` exists. The
proxy layer's preflight still runs for context-limit and loss-policy
validation, but it does **not** create a `PreparedTranscode` for
media-bearing requests. Inside the retry loop, the coordinator's
`_apply_selected_provider_transcode` helper reverts the `ProviderBoundRequest`
to the original client payload and re-translates against the *selected*
provider's `MultimodalCapabilities` row. Capability resolution is
`catalog.cache.get_model_for_provider(model_id, selected.provider_id)`,
never a global first-seen lookup. A retry that selects a different provider
always rebuilds from the original client payload; provider A's translation
is never stacked on provider B's. Text-only and tool-only requests continue
to reuse the preflight `PreparedTranscode` unchanged.

Selected-provider transcode rejections (`CapabilityError` from a
selected-provider capability check, or `TranscodeLossError` from the
transcoder under `loss_policy = "reject"`) are client-validation
outcomes, not internal defects. Plan 142 makes the attempt-loop seam
preserve these as typed exceptions: `_finalize_selected_capability_rejection`
and `_finalize_selected_transcode_loss_rejection` converge selected
durable/runtime ownership synchronously through the canonical finalization
owner, then the typed exception is re-raised so `proxy_request.py`
renders it as HTTP 400 (`openai_capability_error_response` /
`anthropic_capability_error_response` for thinking-budget rejections;
the generic `endpoint.error_response(400, "invalid_request_error")`
for transcode-loss rejections). No retry selects another account, no
upstream HTTP request is built/sent, and no provider health,
suppression, quarantine, circuit, or durable backoff effect is
applied. A simulated durable finalization failure on either path
propagates `DatabaseError` so the existing supervisor/restart
ownership path can recover, instead of silently reporting a clean
400 while convergence is unknown.

Provider-bound serialized-size rejection is a local client-validation
failure observed after `SelectedAttempt` exists. The provider-bound
helper `_validate_serialized_request_size` raises `RequestTooLargeError`
when the selected provider's `max_serialized_request_bytes` ceiling is
exceeded. `_finalize_selected_oversize_rejection` then converges the
selected durable/runtime ownership through the canonical finalization
owner before marking `_oversize_finalized`. The `RequestTooLargeError`
is explicitly rendered as HTTP 413 by the proxy layer; no upstream I/O
is performed, no health/backoff/quarantine penalty is applied, and no
retry selects a different account.

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
- `ProviderBoundRequest` dispatch-freeze flag resets together with `payload_generation`; mutations that do not start a new generation (`replace_provider_payload`, `set_provider_payload(increment_generation=False)`, `adopt_provider_payload(increment_generation=False)`) reject when frozen. The post-selection transcoder relies on the generation-incrementing methods to replace a previously dispatched body on retry
