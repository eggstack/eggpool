# C008 Closure — Streaming Handoff, Timeouts, Cancellation, and Terminal Policy

Status: closed

Implementation commit: [`ecce4212`](https://github.com/eggstack/eggpool/commit/ecce4212)

Plan: [C008 — streaming handoff, timeouts, cancellation, and terminal policy](../../implementation/coordinator/008-streaming-handoff-timeouts-and-cancellation.md)

Repository baseline: `e78e6e55`

## Outcome

C008 completes the streaming inference path: one C004 upstream dispatch at a
time through the M6 incremental stream runtime, M7-owned response-header /
first-byte / idle timeout policy, monotonic downstream handoff, per-chunk
client-surface adaptation, C005 success/failure effects, retained C006
terminal finalization, and the client-visible streaming contract.

The new `coordinator::streaming` module owns the streaming retry loop because
timeout classification, response-start monotonicity, and failed-attempt
cleanup must be decided together. `ResponseHandoffState` is reused from C007
as the monotonic process-local fact: once the caller marks response start
(sent or attempted), transparent replay is forbidden and any later idle
timeout, midstream transport failure, provider terminal event, EOF outcome,
write failure, or cancellation finalizes as a post-handoff outcome instead of
retrying. Every terminal path — pre-handoff terminal error, live-stream
terminal, and dropped execution — registers retained C006 ownership; dropping
a pending execution without completing schedules an interrupted/cancelled
terminal command so cancellation cannot strand a converted claim, while a
stored natural terminal survives cancellation during the finalization handoff.

## Requirement-to-evidence matrix

| C008 requirement | Evidence | Result |
|---|---|---|
| Phase model (headers wait, headers accepted, first byte wait, downstream started, body in progress, terminal evidence, EOF/failure/cancellation, retained finalization) | `stream_phase_progresses_through_lifecycle` observes DownstreamPending → Streaming → Closed; pre-handoff timeout tests observe the first three phases through failover | Pass |
| Response-header timeout policy with injected timers | `stream_header_timeout_fails_over_before_handoff` (150 ms M7 timer vs 5 s server barrier, fails over) and `stream_header_timeout_without_failover_is_terminal` (terminal 503 envelope, no retry) | Pass |
| First-byte timeout policy | `stream_first_byte_timeout_fails_over_before_handoff` (headers accepted, body stalled, fails over pre-handoff) | Pass |
| Active stream with no idle timeout; idle timeout terminal midstream | `stream_active_flow_with_no_idle_timeout_succeeds` (paced chunks, no timer); `stream_idle_timeout_is_terminal_without_retry` (stall after first chunk, terminal, standby account untouched) | Pass |
| No whole-stream deadline | `stream_timeout_policy_follows_provider_config` proves `max_lifetime_s` never becomes a coordinator timer; paced/idle-absent tests run past any absolute budget | Pass |
| Incremental M6 stream runtime, per-chunk adaptation, no complete-stream buffer | 15-combination success matrix plus `stream_first_chunk_arrives_before_eof` (first chunk arrives before EOF is available); only scalar counters, the current chunk, and bounded M6 decoder state exist per call | Pass |
| All five upstream profiles to three client surfaces | `stream_success_matrix_covers_every_client_surface_and_upstream_profile` asserts terminal markers per client surface, usage 10/4, headers, and exactly-once convergence for all 15 combinations | Pass |
| Terminal success | Same matrix: native terminal evidence → `completed` with `stream_completed_canonical` diagnostic | Pass |
| Compatibility EOF | `stream_compatibility_eof_succeeds_when_policy_allows` (usage-complete EOF under `compatible`); `stream_strict_policy_rejects_usage_only_eof` (strict stays `premature_eof_before_body`) | Pass |
| Empty EOF | `stream_empty_eof_is_terminal` proves `EmptyEof`, never success | Pass |
| Partial premature EOF | `stream_partial_eof_after_start_is_midstream` proves `premature_eof_midstream` after downstream start | Pass |
| Malformed SSE / invalid UTF-8 | `stream_malformed_sse_is_terminal_not_success` (skipped bad chunk poisons terminal) and `stream_invalid_utf8_is_terminal_not_success`; EOF can never become false success | Pass |
| Responses failed/incomplete terminal events | `stream_responses_failed_is_forwarded_terminal` and `stream_responses_incomplete_is_forwarded_terminal` (event forwarded, clean caller EOF, durable `error`, no retry) | Pass |
| Gemini incomplete terminal event | `stream_gemini_incomplete_is_terminal` (payload forwards, terminal-incomplete diagnostic, durable `error`) | Pass |
| Upstream midstream exception | `stream_midstream_transport_error_never_retries` (abrupt chunked abort after first chunk; terminal, standby untouched) | Pass |
| Client disconnect before/after start | `stream_cancellation_before_start_finalizes_interrupted` (durable `error`, no client-cancelled count) and `stream_cancellation_after_start_finalizes_cancelled` (durable `cancelled`, `client_cancelled` count) | Pass |
| Downstream write error | `stream_downstream_write_failure_after_handoff_never_retries` (cancelled, no replay) | Pass |
| Cancellation during finalization handoff | `stream_cancellation_during_finalization_handoff_preserves_terminal` (drop after natural terminal keeps `completed` 10/4) | Pass |
| Header/first-byte failures retryable only through C005; no post-handoff replay | Failover tests assert 2 attempts in order with prior-attempt convergence first; idle/midstream/EOF tests assert the standby account count stays 0 | Pass |
| Attempt counts, handoff monotonicity, bounded buffer state, transport closure, C006 convergence | Matrix and every terminal test assert counts, `transport_released()`, zero active reservations/claims, and `progress.completed` | Pass |
| No new HTTP/server framework or generic streaming framework | No `Cargo.toml`/`Cargo.lock` change; reuses M4 pool/transport, M5 router/claims, M6 `WireStream`, C005 classifier/engine, C006 supervisor, Tokio timers only | Pass |

## Failing-before / passing-after evidence

Before the implementation there was no streaming coordinator: no timeout
ownership, no handoff fact for streams, no incremental body driver, no EOF
classification, and no tests. `rust/tests/coordinator_c008.rs` did not exist.

After implementation, `rust/tests/coordinator_c008.rs` passes 29/29 and the
full Rust run passes 252 tests across all targets with no failures (223
pre-existing + 29 new), `tests/migration_rs` passes 83 with 3 skipped, the
targeted Python retry/finalization/stream suites pass 238, `tests/smoke/`
passes 14, `pyright src/ scripts/` reports 0 errors, and ruff format/check
pass on all 728 files.

Implementation review caught and fixed three defects before closure, each with
regression coverage in the final suite: a double-counted header-timeout
diagnostic (timer path recorded once, in the shared terminal helper only), an
exhaustion-terminal conflict with the already-persisted failed-attempt facts
(C014 fails closed on incompatible commands; the terminal now reuses the
recorded failure facts under the synthetic 503 envelope), and mid-frame test
framing that assumed per-event chunk batching instead of per-pull batching.

## Verification commands actually run

```text
cargo fmt --all -- --check
cargo clippy --all-targets -- -D warnings
cargo test --all-targets                         # 252 passed, 0 failed
cargo test --test coordinator_c008               # 29 passed, 0 failed
uv run pytest tests/migration_rs -q --tb=short --maxfail=1  # 83 passed, 3 skipped
uv run pytest tests/unit/test_retry_classification.py tests/unit/test_backoff.py tests/unit/test_failure_effects_table.py tests/unit/test_request_finalization_state_machine.py tests/unit/test_request_finalization_supervisor.py tests/unit/test_request_finalizer.py tests/unit/test_finalizer_reservation_regression.py tests/unit/test_accepted_finalization_state_machine.py tests/unit/test_stream_completion.py tests/unit/test_stream_diagnostics.py -q --tb=short --maxfail=1  # 238 passed
uv run pytest tests/smoke/ -q --tb=short --maxfail=1  # 14 passed
uv run pyright src/ scripts/                     # 0 errors, 0 warnings
uv run ruff check tests/migration_rs
uv run ruff format --check src/ tests/ scripts/  # 728 files formatted
uv run ruff check src/ tests/ scripts/
git diff --check
```

No live/paid provider, external network, schema migration, or dependency
update was required.

## Security, contention, restart, and resource review

No credential, request body, or raw provider chunk enters `FinalizationData`,
diagnostics, or `Debug` output beyond lengths and counts. Diagnostics carry
only outcome labels, attempt numbers, byte counts, and elapsed times.
Upstream request IDs are bounded to 128 chars with control characters
stripped. Client credentials are filtered before upstream headers are built
(C007 boundary, reused unchanged), and response filtering drops
authorization, hop-by-hop, framing, and connection-nominated headers while
preserving duplicates and useful values. Wire flights, effect records,
retained jobs, resolver maps, timer overrides, and diagnostic counters remain
bounded through the existing C005/C006/C013 interfaces; C008 adds no retained
state beyond one bounded outcome map per coordinator and no retry budget of
its own. Attempt-effect bookkeeping stays first-observation-wins so duplicate
observations cannot double-apply health transitions. Timeout expiry drops the
M4 body before terminal registration, releasing the connection promptly.
Cancellation before handoff converges as interrupted without downstream
started; cancellation or write failure after handoff converges as cancelled
with downstream started and never replays upstream. No schema fork, no second
HTTP stack, no scheduler, and no M8 lifecycle change were introduced.

## Supported differences from the Python oracle

- Exhaustion with no eligible account and no upstream response converges to a
  synthetic 503 (C007 parity). Python derives that synthetic status from the
  last exception (typically 502); terminality, non-retry, and retained
  ownership match either way.
- Pre-handoff M7 timer expiry classifies through the transport policy table
  (`transport_failure` evidence). Python raises distinct
  `ProviderStreamTimeoutError` outcomes at the same boundary; the observable
  retry/terminal behavior and the recorded timeout outcome labels match.
- Provider-error pass-through forwards the raw upstream body without
  transcoder re-encoding, as in C007. Timeout/error envelopes are synthesized
  per client surface (Messages keeps the `api_error` shape).
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

## Registry transition and future-plan audit

C008 moves from the dependency-ready table to completed implementation plans
with commit `ecce4212` and this accepted closure record. C009 has C008 as its
sole hard dependency, so C009 is promoted to the dependency-ready table as
the sole ready plan. C010 remains queued behind C009, C011 behind C010, and
no other future plan is unblocked: C010 requires the public endpoint surface
C009 will wire, and C011 aggregates the full M7 qualification. M8 remains
blocked on accepted C011 M7 closure and its separate planning review.

Unresolved mandatory findings within C008 scope: none. C008 does not replace
the aggregate C011 M7 closure and does not claim public-endpoint,
restart-reconciliation, or M8 lifecycle parity.

Recommendation: **closed**; C009 may proceed, with C010-C011 and M8 retaining
their existing serial gates.
