# C009 Closure — Public Inference Endpoints and Semantic-Router Internal Dispatch

Status: closed

Implementation commit: [`0813ba62`](https://github.com/eggstack/eggpool/commit/0813ba62)

Plan: [C009 — public inference endpoints and semantic-router dispatch](../../implementation/coordinator/009-inference-endpoints-and-semantic-router-dispatch.md)

Repository baseline: `9215ff71`

## Outcome

C009 wires the qualified coordinator lifecycle into Rust's Axum public
inference surfaces and completes the D007 semantic model-router selector
path that intentionally waited for M7. HTTP handlers remain thin:
admission/auth/body limits are existing boundaries; each handler invokes
exactly one coordinator entry point and translates its typed result to the
established client surface.

The new `coordinator::endpoints` module owns the thin adapter (surface
mapping, protocol-shaped errors, Responses stateless validation,
provider-qualified parsing, exact virtual-alias resolution, single
finite/stream invocation). The new `coordinator::semantic` module owns the
bounded selector (deterministic prompt/repair/parse, recursion refusal,
separate budgets, deterministic fallback). Affinity commit stays in the
endpoints layer through the existing `ModelRouterAffinity` D007 contract.
Internal selector dispatch constructs a typed `FiniteRequest` directly and
calls the same `FiniteCoordinator`; there is no HTTP loopback, RPC
framework, or new web stack.

## Requirement-to-evidence matrix

| C009 requirement | Evidence | Result |
|---|---|---|
| OpenAI Chat Completions route | `thin_finite_path_covers_three_surfaces_end_to_end` (Chat) and `axum_endpoints_preserve_auth_body_limits_and_surfaces` (POST /v1/chat/completions through the real Axum router, 200) | Pass |
| OpenAI Responses route with stateless contract | Same finite matrix (Responses with explicit `store=false`) plus `endpoint_rejects_malformed_model_and_stateless_violations` (missing `store` rejected 400, no dispatch) | Pass |
| Anthropic Messages route with protocol-shaped errors | Same matrix (Messages) plus `endpoint_error_body` shape asserts (`api_error` vs `upstream_error`) | Pass |
| Frozen compatibility aliases not redesigned | Only the three production POST routes are served; no new paths, no GET inference, no envelope redesign; `build_router` preserves auth middleware and `RequestBodyLimitLayer` | Pass |
| Auth middleware preserved | `axum_endpoints_preserve_auth_body_limits_and_surfaces` (no credentials → 401 with zero upstream dispatches; Bearer → 200 with one dispatch) | Pass |
| Request-body ceilings before dispatch | `endpoint_enforces_body_ceiling_before_dispatch` (oversized → 413, zero dispatches) plus Tower limit layer preserved in `build_router` | Pass |
| Content type, status, filtered headers, proxy IDs | Finite/stream executions assert `application/json` vs `text/event-stream`, `x-proxy-request-id`, `x-proxy-attempt-count`, `x-custom` passthrough, hop-by-hop/auth stripping (reused C007/C008 filters) | Pass |
| Stream vs finite behavior | `thin_stream_path_covers_three_surfaces_without_buffering` (incremental `next_chunk` forwarding, no complete-stream buffer) vs finite matrix; handler peeks the boolean `stream` flag and rejects non-boolean shapes as 400 | Pass |
| Request/session headers hashed, never forwarded or logged | `filtered_incoming_headers` drops `authorization`/`x-api-key`/`x-eggpool-route-session` before provider headers (attempt boundary reuses C004 filtering); affinity tests assert `Debug` never contains session text | Pass |
| Provider-qualified model IDs | `parse_provider_qualified_model` unit asserts plus `provider_qualified_model_ids_select_exact_provider` (qualified pins to provider-b, failing provider-a untouched, one dispatch) | Pass |
| Native/cross-wire paths | Finite matrix covers Chat upstream for all three client surfaces; C007/C008 qualification already proves all five upstream profiles; cross-wire preparation failures are terminal without dispatch | Pass |
| Retries before handoff, never after | `finite_retry_before_handoff_and_no_retry_after_handoff` (500 fails over 1+1, then counts frozen; post-handoff `WriteFailed` converges without replay) | Pass |
| Errors after handoff terminal | Same test plus streaming `Some(Err)` path ends the Axum stream without replay; durable status converges as error/cancelled | Pass |
| Selector bounded internal dispatch through same lifecycle | `semantic_router_success_uses_bounded_internal_dispatch` (one selector + one concrete dispatch, virtual facts returned, `completed`) | Pass |
| Recursion/cycle protection | Structural validation still forbids virtual selector/route targets; runtime guard tested in `semantic_router_recursion_guard_falls_back_without_loop` (virtual-reported selector refuses without I/O, falls back to default) | Pass |
| Separate bounded attempt/token/body/time budget | `selector_timeout_s` covers initial+repair via `tokio::time::timeout`; `max_input_bytes` truncates variable text; 16 KiB response bound in `parse_route_id`; `repair_attempts` 0/1 gates exactly one repair with the same bounded context | Pass |
| Deterministic fallback/error policy | `semantic_router_fallback_and_affinity_without_leak` (2xx-invalid + failed repair → `repair_failed` default) plus non-2xx → `unavailable` without repair, timeout → `timeout` | Pass |
| Affinity committed only per D007 | Sticky resolve through `ModelRouterAffinity` validates route ID/label/model/virtual before storing; `sticky=false` bypasses; second same-session request hits affinity with zero new selector dispatches; invalid selections never cached | Pass |
| Selector lifecycle observable without leaking prompt/body | `SelectorDiagnostics` carries attempts/fallback/repair/source/counts/latency only; no prompt text, response body, session header, or credential in diagnostics or `Debug` | Pass |
| Typed internal dispatch, no loopback | `SemanticSelector::execute_selector` builds `FiniteRequest` directly; no localhost HTTP, no new client; `grep` shows no `loopback`/`127.0.0.1` selector path outside test fixtures | Pass |
| Handlers contain no routing/retry/finalization loops | `endpoints::execute_finite`/`execute_stream` each call one coordinator `execute` exactly once; `server.rs` handlers only peek `stream`, resolve session/proxy ID, and translate the typed execution | Pass |
| Isolation: bad request/upstream/selector/disconnect cannot poison shared state | `bad_requests_do_not_require_restart_for_recovery` (malformed client + malformed upstream terminal, then valid succeeds), `cancellation_before_and_after_handoff_never_replays`, `concurrent_requests_converge_without_poisoning_shared_state` (8 concurrent, zero active reservations, zero active counts) | Pass |
| Subsequent valid request works without restart/repair | Same recovery test plus concurrency/cancellation tests assert active counts return to baseline and next dispatch succeeds | Pass |
| No internal HTTP loopback, RPC, or new web stack | No loopback client; only addition is `tokio-stream` (`default-features=false`, `sync` only) as the Axum `Body::from_stream` bridge — a narrow streaming utility, not a web stack; no second HTTP client, ORM, actor framework, or scheduler | Pass |

## Failing-before / passing-after evidence

Before the implementation the Axum inference routes were placeholders
returning 501 (`placeholder_inference`), there was no semantic selector
(prompt/repair/parse/dispatch/fallback), no virtual-alias resolution, no
provider-qualifier pinning, and no test file. `rust/tests/coordinator_c009.rs`
did not exist.

After implementation, `rust/tests/coordinator_c009.rs` passes 13/13 and the
full Rust run passes 268 tests across all targets with no failures (255
pre-existing including 3 new semantic unit tests + 13 new endpoint tests),
`tests/migration_rs` passes 83 with 3 skipped, the targeted Python
retry/finalization/stream suites pass 238, `tests/smoke/` passes 14,
`pyright src/ scripts/` reports 0 errors, and ruff format/check pass on all
728 files.

Implementation review caught and fixed three defects before closure, each
with regression coverage in the final suite: authoritative catalog updates
withdrawing previously seeded model support (fixture now seeds one
authoritative list per account), provider qualifiers lost before admission
(endpoints and selector both strip for the body and preserve the pin in
`routing_facts.provider_id`), and selector dispatches landing on concrete
providers due to uniform seeding (virtual tests now use provider-qualified
selector/route targets for deterministic counts).

## Verification commands actually run

```text
cargo fmt --all -- --check
cargo clippy --all-targets -- -D warnings
cargo test --all-targets                         # 268 passed, 0 failed
cargo test --test coordinator_c009               # 13 passed, 0 failed
uv run pytest tests/migration_rs -q --tb=short --maxfail=1  # 83 passed, 3 skipped
uv run pytest tests/unit/test_retry_classification.py tests/unit/test_backoff.py tests/unit/test_failure_effects_table.py tests/unit/test_request_finalization_state_machine.py tests/unit/test_request_finalization_supervisor.py tests/unit/test_request_finalizer.py tests/unit/test_finalizer_reservation_regression.py tests/unit/test_accepted_finalization_state_machine.py tests/unit/test_stream_completion.py tests/unit/test_stream_diagnostics.py -q --tb=short --maxfail=1  # 238 passed
uv run pytest tests/smoke/ -q --tb=short --maxfail=1  # 14 passed
uv run pyright src/ scripts/                     # 0 errors, 0 warnings
uv run ruff format --check src/ tests/ scripts/  # 728 files formatted
uv run ruff check src/ tests/ scripts/
git diff --check
```

No live/paid provider, external network, schema migration, or Eggress
feature change was required. The only Cargo change is the narrow
`tokio-stream` streaming bridge noted above; `Cargo.lock` updates
accordingly with no other dependency change.

## Security, contention, restart, and resource review

No credential, request body, provider body, prompt text, selector response,
or session header enters `FinalizationData`, selector diagnostics,
affinity `Debug`, or coordinator `Debug` beyond lengths and counts. Session
headers are SHA-256 hashed at the boundary; raw values are never forwarded
upstream, persisted, or logged. Upstream request IDs remain bounded to 128
chars with control characters stripped. Client credentials are filtered
before upstream headers are built (C004/C007 boundary reused); response
filtering drops authorization, hop-by-hop, framing, and
connection-nominated headers while preserving duplicates and useful values.
Wire flights, effect records, retained jobs, resolver maps, affinity
entries/flights, selector diagnostics, and stream outcome counters remain
bounded through the existing C005/C006/C013 and D007 interfaces; C009 adds
no retained state beyond one bounded outcome per coordinator plus the
process-owned affinity LRU (4096 entries, TTL, single-flight table), and no
retry budget of its own beyond the shared `1 + max_retries_before_stream`
upstream-submission budget. Attempt-effect bookkeeping stays
first-observation-wins. Cancellation before handoff converges as
interrupted without downstream started; cancellation or write failure after
handoff converges as cancelled with downstream started and never replays
upstream. No schema fork, no second HTTP stack, no scheduler, and no M8
lifecycle change were introduced.

## Supported differences from the Python oracle

- Provider-error pass-through forwards the raw upstream body without
  transcoder re-encoding, as in C007/C008. Timeout/error envelopes are
  synthesized per client surface (Messages keeps the `api_error` shape).
- `CanonicalUsage` carries no reported-cost fields, so provider/local cost
  remain unset rather than fabricated. Token and cache counters persist
  exactly as M6 reports them.
- Latency persists total elapsed and first-byte elapsed; connect/read/overhead
  splits are not tracked separately.
- Release reasons use the Python vocabulary (`completed`,
  `attempt_failed`, `attempt_retryable`, `capability_rejected`).
- Non-SSE upstream bodies preserve the Python legacy pass-through (raw
  chunks, complete at EOF); SSE completion rules apply to event-stream
  responses only.
- Finite Axum handlers converge finalization as `Delivered` before the
  framework writes the body. A handler-task cancellation before return drops
  the execution and converges as interrupted without replay, matching the
  Python pre-handoff cancellation boundary; post-return transport write
  failures are owned by the framework connection, not replayed upstream.
- Selector provider pinning uses explicit `model/provider` qualifiers in
  tests for deterministic counts. The Python oracle resolves unqualified
  selector models by load/fairness across all supporting accounts; the Rust
  selector preserves that semantic when unqualified and pins exactly when
  qualified, identical to concrete request routing.

## Registry transition and future-plan audit

C009 moves from the dependency-ready table to completed implementation
plans with commit `0813ba62` and this accepted closure record. C010 has
C009 as its sole hard dependency, so C010 is promoted to the
dependency-ready table as the sole ready plan. C011 remains queued behind
C010, and no other future plan is unblocked: C011 aggregates the full M7
qualification and requires C010's reconciliation primitives, and M8 remains
blocked on accepted C011 M7 closure plus its separate planning review.

Unresolved mandatory findings within C009 scope: none. C009 does not
replace the aggregate C011 M7 closure and does not claim
restart-reconciliation or M8 lifecycle parity.

Recommendation: **closed**; C010 may proceed, with C011 and M8 retaining
their existing serial gates.
