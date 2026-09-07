# C007 Closure — Finite Response Handoff and Completion

Status: closed

Implementation commit: [`a7a119ed`](https://github.com/eggstack/eggpool/commit/a7a119ed)

Plan: [C007 — finite response handoff and completion](../../implementation/coordinator/007-finite-response-handoff-and-completion.md)

Repository baseline: `a7c23a07`

## Outcome

C007 completes the non-streaming inference path: one C004 upstream
response through M6 finite decoding/adaptation, monotonic downstream
handoff, C005 success/failure effects, retained C006 terminal
finalization, and the client-visible response contract.

The new `coordinator::finite` module owns the finite retry loop because
response classification, response-start monotonicity, and failed-attempt
cleanup must be decided together. `ResponseHandoffState` is a monotonic
process-local fact: once the caller marks response start (sent or
attempted), transparent replay is forbidden and any later write failure
or cancellation finalizes as a post-handoff outcome instead of retrying.
Every terminal path registers retained C006 ownership; dropping a
pending execution without completing schedules an interrupted terminal
command so cancellation cannot strand a converted claim.

## Requirement-to-evidence matrix

| C007 requirement | Evidence | Result |
|---|---|---|
| Successful finite response decoded by M6 | `finite_success_matrix_covers_every_client_surface_and_upstream_profile` runs all 15 client-surface x upstream-profile combinations against local providers and asserts M6-adapted bodies | Pass |
| Valid provider error envelope | Same matrix plus `finite_non_retryable_provider_error_passes_through_with_proxy_headers` (400 passthrough) and `finite_error_shape_matches_client_surface_protocol` | Pass |
| Malformed provider success/error body | `finite_malformed_success_is_terminal_without_retry` (non-JSON 2xx) and `finite_terminal_paths_leave_no_claim_or_reservation_state` (malformed case) prove terminal 500 with no retry | Pass |
| Retryable vs terminal classification via C005 | `finite_retryable_response_fails_over_before_handoff_and_cleans_up` (500 fails over), `finite_non_retryable_provider_error_passes_through_with_proxy_headers` (400 terminal), `finite_exhausted_retryable_passes_through_last_response`, `finite_attempt_ceiling_stops_retry_with_last_response` | Pass |
| Response body/resource limit violations | `finite_provider_body_limit_is_terminal_without_retry` shrinks the provider bound and proves terminal 502 with no retry | Pass |
| Adaptation/loss rejection on client encoding | M6 decode-`Err` (including `ResponseAdaptation` loss rejection and client-body bounds) converges as a terminal 500 client error with retained ownership; exercised through the same `local_failure_data` path as the malformed regression | Pass |
| Retry only pre-handoff with failed-attempt cleanup first | Failover test asserts 2 attempts in order, prior-attempt terminal convergence before replacement ownership, and unchanged counts after handoff | Pass |
| Monotonic handoff, no replay after start | `finite_handoff_state_is_monotonic_and_process_local`, `finite_downstream_write_failure_after_handoff_never_retries` (ClientCancelled, still 1 upstream attempt), both cancellation tests | Pass |
| Filtered headers, content type, status, request IDs, compat headers, no auth leak | Matrix asserts `x-custom`, `x-proxy-request-id`, `x-proxy-attempt-count`; `finite_header_filtering_drops_hop_by_hop_and_internal_headers`; `finite_forwards_filtered_headers_and_never_leaks_client_credentials` proves client secrets never reach the provider | Pass |
| C006 terminal ownership, usage/counters/cost/bytes/timing/upstream ID/wire/account/transcode/release per Python contract | Matrix asserts durable `completed` status, input/output tokens (10/4), bounded upstream request ID, and non-zero byte counters; every terminal test asserts exactly-once convergence with zero active claims/reservations | Pass |
| Health/backoff clearing exactly once | Success applies `record_success` once before handoff; duplicate finalization never repeats it (C014 supervisor convergence; Drop is a no-op after `complete` consumes ownership) | Pass |
| Attempt counts/order and the exact retry stop point | Failover (1+1 across accounts), exhaustion (1 attempt, passthrough), ceiling (1 attempt, `x-proxy-retry-reason`), post-handoff counts unchanged in every test | Pass |
| No new HTTP/server framework | No `Cargo.toml`/`Cargo.lock` change; reuses M4 pool/transport, M5 router/claims, M6 runtime, C005 classifier, C006 supervisor | Pass |

## Failing-before / passing-after evidence

Before the implementation there was no finite coordinator: no handoff
fact, no retry loop, no terminal ownership, and no tests. The starting
working tree additionally carried uncommitted partial changes that left
`coordinator_finalization` red (5 failures, `NOT NULL constraint
failed: requests.cache_counter_status`), left Messages requests with
zero eligible routing candidates, omitted proxy compatibility headers,
inverted byte accounting, fabricated zero cost estimates, and returned a
synthetic 503 instead of the last upstream response on exhaustion.

After implementation, `rust/tests/coordinator_c007.rs` passes 15/15,
the full Rust run passes 223 tests across all 26 targets with no
failures, `tests/migration_rs` passes 83 with 3 skipped, and the
targeted Python retry/finalization suites pass 215. The previously red
finalization tests pass 10/10 after the `not_reported` persistence
default described below.

## Verification commands actually run

```text
cargo fmt --all -- --check
cargo clippy --all-targets -- -D warnings
cargo test --all-targets                         # 223 passed, 0 failed
uv run pytest tests/migration_rs -q --tb=short --maxfail=1  # 83 passed, 3 skipped
uv run pytest tests/unit/test_retry_classification.py tests/unit/test_backoff.py tests/unit/test_failure_effects_table.py tests/unit/test_request_finalization_state_machine.py tests/unit/test_request_finalization_supervisor.py tests/unit/test_request_finalizer.py tests/unit/test_finalizer_reservation_regression.py tests/unit/test_accepted_finalization_state_machine.py -q --tb=short --maxfail=1  # 215 passed
uv run ruff check tests/migration_rs
uv run ruff format --check src/ tests/ scripts/  # 728 files formatted
uv run ruff check src/ tests/ scripts/
git diff --check
```

No live/paid provider, external network, schema migration, or
dependency update was required.

## Security, contention, restart, and resource review

No credential, request body, or raw provider body enters
`FinalizationData`, compatibility state, or `Debug` output beyond
lengths. Upstream request IDs are bounded to 128 chars with control
characters stripped at both the attempt boundary and finalization.
Client credentials are filtered before upstream headers are built (the
credential-hygiene test observes the provider-visible bytes), and
response filtering drops authorization, hop-by-hop, framing, and
connection-nominated headers while preserving duplicates and useful
values. Wire flights, effect records, retained jobs, and resolver maps
remain bounded through the existing C005/C006/C013 interfaces; C007
adds no retained state or retry budget of its own. Attempt-effect
bookkeeping stays first-observation-wins so duplicate observations
cannot double-apply health transitions. Cancellation before handoff
converges as interrupted without downstream started; cancellation or
write failure after handoff converges as cancelled/interrupted with
downstream started and never replays upstream. No schema fork, no second
HTTP stack, no scheduler, and no M8 lifecycle change were introduced.

## Supported differences from the Python oracle

- Provider-error pass-through forwards the raw upstream body without
  transcoder re-encoding. M6 `ProviderError` carries no client-adapted
  body, and the M6 wire path is authoritative; Python re-encodes only
  on its legacy transcoder path.
- `CanonicalUsage` carries no reported-cost fields, so provider/local
  cost remain unset rather than fabricated. Token and cache counters
  persist exactly as M6 reports them.
- Latency persists total elapsed and header elapsed (first byte);
  connect/read/overhead splits are not tracked separately.
- Release reasons use the Python vocabulary (`completed`,
  `attempt_failed`, `attempt_retryable`, `capability_rejected`).
- A synthetic 503 is returned only when no upstream response was ever
  received and no account remains. Python derives that synthetic status
  from the last exception (typically 502); terminality, non-retry, and
  retained ownership match either way.
- Routing now advertises the Messages surface for anthropic-capable
  providers (previously zero candidates before any coordinator logic
  ran). The change is additive; chat/responses behavior is unchanged.
  C009 owns the public endpoint wiring that will use it.

## Registry transition and future-plan audit

C007 moves from the dependency-ready table to completed implementation
plans with commit `a7a119ed` and this accepted closure record. C008 is
promoted as the sole dependency-ready plan because C007 is its hard
dependency. C009 remains queued behind C008, C010 behind C009, and C011
behind C010. No other future plan is unblocked. M8 remains blocked on
accepted C011 M7 closure and its separate planning review.

Unresolved mandatory findings within C007 scope: none. C007 does not
replace the aggregate C011 M7 closure and does not claim streaming,
endpoint, restart-reconciliation, or M8 lifecycle parity.

Recommendation: **closed**; C008 may proceed, with C009-C011 and M8
retaining their existing serial gates.
