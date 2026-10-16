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
- The same middleware consults the process-local short-window fairness
  limiter after authentication and generation acquisition but before reading
  the body or reserving durable user/global budget. It keys buckets by
  authenticated user ID, shares allowance across that user's credentials,
  applies optional per-principal/global 60-second token buckets, and bounds
  retained principal state. It never queues, writes SQLite, or changes
  provider routing. State resets on restart; absent rules allocate no bucket.
  Throttles are 429 with `Retry-After` and
  `short_window_rate_limited`; successful admissions expose bounded remaining
  allowance headers.
- Declared `Content-Length` above the live limit is rejected before polling
  (413); malformed `Content-Length` is 400. A 32 KiB initial
  `RawBodyReservation` is taken against `effective_ceiling()` =
  `max(64 MiB, live limit)` capped at 256 MiB (`request/resource_budget.rs`);
  actual bytes grow the reservation (`try_grow`) so lying declarations cannot
  under-reserve. Exhaustion is 429 with `Retry-After: 1`, not 503.
- `authenticate`/`requires_auth` enforce constant-time `Bearer`/`x-api-key`
  auth; `/api/integrations/*`, `/api/stats/runtime`, `/api/stats/update`,
  `/api/status`, and all `/v1/*` stay authenticated.
  For authenticated inference, middleware installs a typed
  `AuthenticatedPrincipal`: personal agent tokens resolve user, credential,
  scopes, and auth epoch from SQLite; browser sessions resolve the same user
  and session identity; the staged shared-key path is explicitly marked
  `legacy_shared_key` with no downstream user ID. Request headers never set
  the principal. Managed user requests capture one immutable policy snapshot
  before model resolution. Requested aliases, provider-qualified models,
  selector models, and resolved concrete targets are checked against it before
  concrete provider dispatch. Denial is a generic 403; policy reads fail
  closed, and admitted work retains its snapshot.
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
  and affinity commit in the endpoints layer only. The typed principal is
  passed through virtual selection too; selector dispatches persist as
  `internal_selector` children linked to the parent proxy request ID, so
  they remain attributable without counting as another logical user request.
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
  closed. The request row stores owner user ID, credential/session ID, auth
  epoch, principal kind, request kind, and optional selector parent. The
  publication transaction rechecks active user/credential state before
  dispatch; the immutable owner fields travel in `FinalizationIdentity` and
  must still match before terminalization. Null user IDs remain explicitly
  legacy/unattributed history. The worker survives caller cancellation and
  compensates lost delivery.
- `FinalizationSupervisor::register`/`wait` retains terminal ownership;
  `DurableFinalizer::finalize_request` vs `finalize_failed_attempt` separate
  terminal from retry-cleanup convergence. `CrashReconciler::reconcile_once`
  converges crash leftovers to interrupted/released without replay or
  double-charge.
- For managed principals, publication checks aggregate and exact-model
  daily/monthly user budget rules and inserts the logical-request hold in that
  same SQLite transaction. The hold survives failed attempts and is extended
  by retries; it is not released until terminal settlement. Concurrency counts
  all live holds across UTC window boundaries. Finalization records each
  transitioned attempt once and settles one logical request in the terminal
  transaction. Unknown usage consumes the remaining estimate and is marked
  unknown in the indexed rollup. Request and concurrency caps are strictly
  enforceable in both modes. Token/cost rules carry an explicit `hard` or
  `estimated` mode (migration 61, default estimated): estimated rules keep the
  request-size token projection and fallback microdollars, which do not prove
  exact output or self-hosted cost bounds; hard rules admit only requests with
  a provable worst-case bound — admission-carried input bound plus the
  caller's explicit compatible output cap (`max_tokens`/`max_completion_tokens`
  on chat, `max_output_tokens` on Responses/compact, `max_tokens` on Anthropic
  Messages) converted at the operator-configured provider tariff — and deny
  with `BudgetNotEnforceable` (429, no upstream submission) when unbounded,
  unpriced, or overflowing. The initial hard hold covers two attempt
  equivalents (concrete attempt plus unattributed selector overhead); retries
  extend by one equivalent after re-checking. Observed provider usage above
  the hold is settled visibly, never clamped.
- An optional instance-global ceiling is intersected in the same publication
  transaction for every admitted call, attributed or not: the request must
  satisfy all applicable instance-global, instance-model, user-global, and
  user-model rules before claim conversion, with one shared hold per scope so
  concurrent users cannot both take the final slot and retries never
  double-count. Global usage folds the same attempt rows at terminal
  settlement; absent global rules, no global hold or row is written.
- Crash recovery converges budget holds without folding usage. A restart
  interrupts every `pending` request, and the same reconciliation pass
  releases each active user/instance hold on those requests (`active →
  released`, never settled): the logical request can never settle
  afterwards, so the release is compensation, not credit, and unknown
  upstream usage stays unknown. Settled holds and folded buckets are
  untouched, and a stale duplicate terminal command conflicts instead of
  re-folding. A retry under the same proxy ID after interruption is refused
  at the duplicate gate (non-pending requests never re-enter admission), so
  no hold is resurrected on dead work. Cancel terminalizes through the
  normal terminal path and settles the hold conservatively at the held
  amounts. Backups capture active holds verbatim as committed rows; a
  restored image reconciles exactly like a crashed live database.
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
