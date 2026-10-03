# Deep Dive: Request Lifecycle

Back to [Architecture](README.md). See also the review index in
[overview.md](overview.md) §§4-6.

`rust/src/server/inference.rs` owns the four thin public adapters
(`chat_completions`, `messages`, `responses`, `responses_compact`); route
assembly and the pre-bound listener stay in `rust/src/server/mod.rs`
(`build_router`). The coordinator (`rust/src/coordinator/`) prepares bounded
canonical input, resolves exact virtual aliases, selects a provider/account,
publishes request/attempt identity, dispatches through the provider pool,
adapts the response, and converges durable state.

## 1. Downstream transport and middleware

- EggServe 0.4.0 (`eggserve-server` with `tower`) drives the pre-bound listener
  through the server-owned `TowerToEggserve` (`with_policy` +
  `RequestBodyPolicy::Stream`, 1 GiB `EGG_SERVE_REQUEST_BODY_LIMIT` above the
  live generation limit). Parser ceilings, 5-minute body-read timeout, and
  24-hour handler timeout are EggServe-owned transport policy.
- `server/middleware.rs::admit_inference_body` runs before the Axum `Bytes`
  extractor: it acquires a `GenerationLease`, reads the live
  `server.max_request_body_bytes`, and enforces it while the body streams.
  `is_inference_path()` covers exactly the four public inference routes.
- Declared `Content-Length` above the live limit is rejected before polling
  (413); malformed `Content-Length` is 400. A 32 KiB initial
  `RawBodyReservation` is taken against `effective_ceiling()` =
  `max(64 MiB, live limit)` capped at 256 MiB (`request/resource_budget.rs`);
  actual bytes grow the reservation (`try_grow`) so lying declarations cannot
  under-reserve. Exhaustion is 429 with `Retry-After: 1`, not 503.
- `authenticate`/`requires_auth` enforce constant-time `Bearer`/`x-api-key`
  auth; `/api/integrations/*`, `/api/stats/runtime`, `/api/stats/update`,
  `/api/status`, and all `/v1/*` stay authenticated.
  `finish_empty_transport_body` only drains provably bodyless requests.

## 2. Endpoint classification (one parse, one call)

- `handle_inference` calls `coordinator::execute_endpoint`
  (`coordinator/endpoints.rs:777`) exactly once per request;
  `handle_finite_compact` calls `execute_compact_finite` exactly once.
  `execute_finite`/`execute_stream` are thin wrappers delegating to
  `execute_endpoint` and rejecting the mismatched `EndpointExecution`.
  `server/*` holds no routing, retry, or finalization logic.
- `coordinator/endpoints.rs::execute_endpoint` parses once via
  `request::parse_request_body(raw_body, max_body_bytes)` (bounded `Bytes`
  size + depth), reads the `stream` flag from the same `ParsedRequestBody`
  (`stream_flag`), resolves the concrete model (`resolve_concrete`), admits
  (`admit_resolved` wrapping `admit_parsed_request`), and constructs
  `FiniteRequest::from_admitted` or `StreamRequest::from_admitted`. Canonical
  `stream` must match the flag or the request is rejected.
- Model resolution mutates the one parsed tree: `parse_provider_qualified_model`
  strips a known `model/provider` qualifier (native no-rewrite path clones only
  the ingress `Bytes` handle via refcount; provider-qualified rewrite
  re-encodes only on an actual model-id change); virtual aliases resolve through
  `SemanticSelector` over the same `FiniteCoordinator` with recursion refusal
  and affinity commit in the endpoints layer only.
- `EndpointError::status` maps admission errors; `endpoint_error_body` shapes
  OpenAI vs Messages envelopes truncated to 512 bytes. `new_proxy_request_id`
  mints the opaque proxy identity; `coordinator/attempt.rs::add_forwarded_headers`
  strips `HOP_BY_HOP_HEADERS` + `LOCAL_HEADERS` + `Connection`-listed tokens
  before upstream dispatch.
- Finite and streaming coordinators borrow the selected provider and its
  generation-static wire-profile slice while selecting and preparing an
  attempt. The borrow ends at synchronous preparation; the resulting
  `PreparedUpstreamAttempt` owns everything needed across provider I/O.

## 3. Bounded admission and ownership

- `request/admission.rs` owns the bounded parse: byte-scan depth pre-check plus
  DOM validation at `MAX_JSON_DEPTH` 64, one `serde_json` decode, then the pure
  kernel decoder (`wire::decode` with `DecodeLimits::current()`).
  `canonical_request_from_object`/`canonical_request_from_value` enforce the
  stateless Responses policy first, preserving pre-extraction precedence.
- Stateless policy (`validate_responses_stateless_policy`): `store` omitted or
  `false` accepted; `store: true`, `previous_response_id`, `conversation`, and
  `background: true` rejected. `has_compaction_trigger` detects v2 trigger
  items without treating them as text.
- Responses admission emits two bounded products: `CanonicalRequest` for
  routing/accounting plus `NativeRequestPreservation` (already-parsed JSON +
  `NativeFeatureSummary` counts only). `body.rs::encode_compact_json` is the
  single deterministic encoder; `limits.rs` owns overflow-safe token, media,
  and output-token estimates.
- `AttemptPreparation` borrows generation/request data only synchronously;
  `PreparedUpstreamAttempt` is fully owned before `submit_once` is awaited
  (`coordinator/attempt.rs::prepare_borrowed`). Credentials render only at
  dispatch-header construction.

## 4. Compaction as an explicit operation

- `POST /v1/responses/compact` is a bounded distinct operation
  (`InferenceOperation::Compact` vs `Generate`), finite-only, history `input`
  required, trigger rejected, same stateless and body bounds.
- Production uses `execute_compact_finite` (finite-only) plus
  `FiniteCoordinator::execute_compact_admitted` (private single-owner
  `FiniteExecutionInput::compact`): the `CompactAdmittedRequest`
  remains the single owner of the preserved JSON tree, never duplicated
  into a public `FiniteRequest`. Public
  `FiniteRequest::new_compact`/`from_compact_admitted` keep their dual-view
  shape for compatibility callers only.
- Routing filters to `CompactionCapabilities::native_v1_supported()` Responses
  targets before submission; accounts without one are skipped without upstream
  I/O. No translated compaction fallback exists.

## 5. Publication, finalization, reconciliation

- `PublicationService::publish` converts one routing claim into durable
  `requests`/`request_attempts`/`reservations`/`routing_decisions` rows plus a
  `FinalizationIdentity`; duplicates observe `AlreadyPublished`, conflicts fail
  closed. The worker survives caller cancellation and compensates lost
  delivery.
- `FinalizationSupervisor::register`/`wait` retains terminal ownership;
  `DurableFinalizer::finalize_request` vs `finalize_failed_attempt` separate
  terminal from retry-cleanup convergence. `CrashReconciler::reconcile_once`
  converges crash leftovers to interrupted/released without replay or
  double-charge.
- Reload policy is referenced, never duplicated: only
  `config_reload_policy.rs::classify_transition` decides reload vs restart.

## 6. Streaming handoff boundary

```text
server/Axum
 -> streaming::coordinator::StreamingCoordinator (pre-handoff)
 -> provider transport -> wire::WireStream
 -> streaming::execution::StreamingExecution (post-handoff)
 -> retained finalization/accounting
```

- `StreamingCoordinator::execute` owns route claims, header/first-byte timers
  (`StreamTimeoutPolicy`: header, first-byte, idle; no whole-stream deadline),
  non-2xx terminalization, first-byte prefetch, and all retry/alternate-wire
  decisions. Retry is available only before the downstream handoff
  (`!response_started && !downstream_started`); returning `StreamingExecution`
  closes the retry window. Production `RetryPolicy` is
  `max_retries_before_stream.max(1) + 1` (`RetryPolicy::default` is
  `max_attempts` 3).
- `StreamingExecution` (`execution.rs`) owns the one live `ProviderBody`,
  `next_chunk` pull loop, idle timeout, and `complete`. Native
  `NativeObserved` streams call `observe_native_push` and forward original
  bytes; translated streams call `push` plus `encode_client_event_stateful`.
- `terminal.rs` consumes `StreamTerminalSummary` via `classify_eof` and the
  `store_*` helpers; it never reparses provider events. Both Responses paths
  require `response.completed`; transport EOF alone is never success (non-SSE
  pass-through keeps its legacy EOF rule).
- `inference.rs::finish_stream_execution` pumps chunks over an mpsc channel
  (capacity 32), holding the lease in the body task; terminal usage flows
  through `usage_metric_event` without bodies or secrets.

## Invariants

- One bounded parse per request; virtual/provider-qualified mutation reuses
  the parsed tree, never reparses.
- Generation lease acquired before body collection; live limit enforced during
  streaming collection, not after.
- Retries and alternate-wire decisions exist only in
  `streaming/coordinator.rs` pre-handoff; `execution.rs` finalizes
  post-handoff failures without replay; `terminal.rs` classifies summaries
  without decoding events.
- `store: true`, stateful continuation, and background execution are rejected
  at admission and at the adapter.
- Native no-rewrite dispatch transfers the `Bytes` handle; rewrites serialize
  exactly once and only when the EggPool-owned `model` field changes.
- Compact production input is private and single-owner; public
  `FiniteRequest` shapes stay compatibility surfaces.

## Verification

```bash
cargo test --manifest-path rust/Cargo.toml --test coordinator_c008 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_boundaries -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_publication -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_stream -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test codex_responses_compat -- --test-threads=1
```
