# C011 Closure — Differential Qualification and M7 Closure

Status: closed

Implementation commit: [`0216410f`](https://github.com/eggstack/eggpool/commit/0216410f9dbc472dab5f2773d13c72daa2a210ca)

Plan: [C011 — differential qualification and M7 closure](../../implementation/coordinator/011-differential-qualification-and-m7-closure.md)

Repository baseline: `4e7db2a8`

## Outcome

C011 aggregates the full M7 qualification and closes M7. The implementation
adds `rust/tests/coordinator_c011.rs` (17 tests) exercising the integrated
Python/Rust coordinator boundary through the real thin endpoint path
(`execute_finite`/`execute_stream` over `InferenceState`) against
deterministic local HTTP providers: all three public client surfaces
(Chat Completions, Responses with explicit `store=false`, Messages) against
all five upstream wire profiles (Chat, Responses, Messages, Gemini
Interactions, Gemini GenerateContent), finite and streaming, direct and
proxied M4 account topologies, single/multiple accounts, fixed and
negotiable wire profiles, and virtual-router semantic selection with
affinity. No production code change was required: the matrix passes against
the already-closed C002/C007-C010/C012-C014 implementation. Four failures
observed during qualification were test-fixture bugs (proxy scheme,
compiled route-ID vocabulary, undersized scripted servers), each fixed in
the implementation commit with no oracle weakening.

M7 is now closed. M8 runtime generations/background lifecycle becomes
eligible for its own planning review; no M8 implementation plan is
promoted by this closure.

## Requirement-to-evidence matrix

| C011 requirement | Evidence | Result |
|---|---|---|
| Three surfaces x native and cross-wire profiles, finite | `c011_finite_matrix_covers_three_surfaces_native_and_crosswire` (15 cells via `execute_finite`; status/headers/body, `x-custom`/`x-proxy-request-id`/`x-proxy-attempt-count`, usage 10/4, upstream-ID, durable `completed`, 1 attempt, no replay, zero leaks) | Pass |
| Three surfaces x native and cross-wire profiles, streaming | `c011_stream_matrix_covers_three_surfaces_native_and_crosswire` (15 cells via `execute_stream`; incremental first chunk, per-surface terminal marker, `transport_released`, usage 10/4, 1 attempt, no replay, zero leaks) | Pass |
| Direct and proxied M4 account clients | `c011_direct_and_proxied_account_clients_share_semantics` (qualified pin to direct succeeds; unknown-scheme proxy fails closed at pool construction with no secret in the error; proxied topology builds via `socks5://`; T006 owns live proxy interop) | Pass |
| Single/multiple accounts | Failover test (1+1 across two accounts in order); exhaustion/pin test (provider-b pin leaves provider-a untouched); concurrency batch (8 requests share one account) | Pass |
| Fixed and negotiable wire profiles | `c011_wire_fixed_and_negotiable_with_leader_follower` (hint ordering, learned reorder, deterministic rejection + cooldown, fixed single-candidate collapse, plus classifier proof that wire-signal + alternate maps to `RetryWire`) | Pass |
| Virtual-router semantic selection | `c011_virtual_router_semantic_selection_with_affinity` (selector once + concrete once, route `1` → `model-fast/fast-provider`, second same-session hits affinity with zero new selector I/O) | Pass |
| Client status/headers/body or ordered SSE frames | Finite/stream matrices assert status, filtered headers, adapted bodies, ordered terminal markers, incremental delivery | Pass |
| Exact number/order of provider attempts | Every matrix/failover/timeout/EOF test asserts exact dispatch counts and order (1+1 failover, 1 terminal, 0 after handoff) | Pass |
| Selected provider/account/wire sequence | Pin test, failover test, virtual test, wire-precedence test assert exact selection | Pass |
| Retry category/scope/action and exhaustion | `c011_c001_failure_corpus_matches_rust_classifier` (all ≥23 C001 rows); exhaustion passthrough; `RetryExhausted` never after handoff | Pass |
| Response-start point and later-retry bar | Handoff monotonicity asserted in every matrix cell; `post_handoff_500` and `response_started` classifier cases never retry; write-failure/cancel-after-handoff never replay | Pass |
| Durable rows (request/attempt/reservation/routing-decision) | `durable_counts`/`request_status` in every integrated test (1 request, terminal attempts, 0 active reservations); C002/C010 publication/reconciliation evidence reused | Pass |
| M5 health/backoff/quarantine/quota/circuit effects | C001 corpus asserts account/model/circuit/backoff per case; failover applies success once; duplicate finalization never repeats health transitions (first-observation-wins ledger) | Pass |
| Wire resolver preference/rejection/flight state | Precedence/TTL/eviction/concurrency/throttling/leader-follower/cancellation assertions; snapshot flights/gates return to 0 | Pass |
| Usage/cost/request-ID/timing class and release reason | Matrices assert tokens 10/4, bounded upstream request ID, non-zero byte counters; release reasons use the Python vocabulary (`completed`, `attempt_failed`, `attempt_retryable`, `capability_rejected`); cost stays unset rather than fabricated (supported difference) | Pass |
| Retained finalization convergence | `c011_finalization_duplicate_conflict_release_and_capacity` (duplicate converges, incompatible fails closed, runtime released exactly once, supervisor shares duplicates, ledger retires, capacity errors before effect ownership) | Pass |
| Restart reconciliation result | `c011_publication_conflict_and_crash_reconciliation_recovery` (duplicate proxy ID fails closed; crash publish → drop claim → `reconcile_once` fixes 1, second pass fixes 0, zero nonterminal rows, recovery succeeds without restart) | Pass |
| Malformed client input | Oversized/invalid-JSON/missing-model/stateless-violation/invalid-stream rejected before dispatch with zero upstream calls | Pass |
| Selection exhaustion | Unknown model fails closed with zero dispatches | Pass |
| DB publication fault | Duplicate proxy identity fails closed (409 semantics); C002 `PublicationFaultInjector` and C010 finalizer-fault matrices remain the write/commit/conversion fault evidence | Pass |
| Post-commit interruption | Crash-reconciliation test (dropped claim converges via reconciler, idempotent second pass) | Pass |
| Pool/connect/TLS/proxy/write/header/read failures | C001 transport-phase classifier rows plus live connect-failure failover (500→200 across accounts); proxied closed-port topology builds; unknown proxy scheme fails closed | Pass |
| Auth/quota/rate-limit/model absence/server errors | C001 rows (ambiguous 401 never disables, explicit invalid disables only the account, 429 retryable with bounded delay, model-absent quarantine, 5xx failover) plus live 500-failover and 400-passthrough cells | Pass |
| Retry-After variants | `c011_retry_after_variants_are_uniformly_bounded` (numeric/date/invalid/missing/negative/overflow, custom cap, 429 stays retryable under the ceiling) | Pass |
| Alternate-wire deterministic rejection | Fixed/negotiable test plus classifier `RetryWire` proof | Pass |
| Negotiation leader/follower cancellation | Leader/follower share, follower-drop keeps leader, leader-drop resolves follower as rejected, flights/gates return to 0, throttling reactive | Pass |
| Finite malformed/provider error | Malformed 2xx terminal without retry; 400 passthrough with proxy headers, no retry | Pass |
| Header/first-byte/idle timeout | Live C008 timeout failover boundaries reused (`stream_header_timeout_fails_over_before_handoff`, `stream_first_byte_timeout_fails_over_before_handoff`, idle-terminal); C011 proves the EOF/terminal taxonomy live and keeps the timeout policy classification at the coordinator boundary | Pass |
| Empty/partial/malformed stream EOF | Each served live: terminal, exactly 1 attempt, transport released, durable error (never false `completed`), zero leaks | Pass |
| Terminal failure/incomplete | Responses `failed`/`incomplete` forwarded as terminal without retry | Pass |
| Upstream midstream exception | Abrupt midstream abort (truncated body, connection drop) terminal without retry | Pass |
| Client disconnect before/after handoff | Dropped finite execution converges via retained ownership; streaming cancel before start converges interrupted; cancel after handoff converges cancelled; never replays | Pass |
| Downstream write failure | `WriteFailed` after handoff converges terminally with no additional upstream attempt | Pass |
| Finalizer DB fault | C010 finalizer write/release fault matrices reused; C011 proves duplicate-compatible convergence and incompatible-conflict closure at the same boundary | Pass |
| Runtime release fault | Claim-backed completion releases exactly once; duplicate needs no runtime work; retained command owns release-error retries (C006/C014 boundary, no new retry budget in C011) | Pass |
| Terminal conflict | Incompatible outcome fails closed as `TerminalConflict`; supervisor rejects incompatible coalescing by compatibility hash | Pass |
| Supervisor capacity | `with_capacity(1)` shares duplicates and returns `Capacity` before effect ownership when full; `snapshot`/`drain` converge to zero jobs | Pass |
| Simulated crash/restart | `CrashReconciler::reconcile_once` converges 1+1+1 with frozen Python reasons, second pass is a noop, same-DB restart stays converged | Pass |
| Subsequent valid request recovery | Every failure test ends with a valid request succeeding without restart; dedicated storm-recovery cell proves post-cancellation recovery | Pass |

## Closure criteria (plan §Closure criteria)

1. All C001 mandatory corpus rows pass or have an approved supported
   difference: pass — the committed `c001-python-observations.json` rows
   are asserted exactly (retry/action/scope/outcome/account/model/wire/
   evidence), with the Retry-After HTTP-date ceiling (`1800s`) as the
   approved C014-supported bound.
2. No transparent retry occurs after downstream handoff: pass — matrices,
   failover, timeout, EOF, abort, cancellation, and write-failure cells
   assert frozen upstream counts after `mark_started`, and the classifier
   forbids retry for `downstream_started`/`response_started` evidence.
3. Every failed attempt is terminal/cleanup-owned before replacement
   ownership: pass — failover asserts prior-attempt terminal convergence
   before the replacement dispatches; C006/C013/C014 retained-ownership
   evidence is reused unchanged.
4. Terminal commands converge under duplicate/cancel/fault/restart cases:
   pass — duplicate-compatible convergence, incompatible-conflict closure,
   cancellation-during-handoff preservation (C008), fault-injection
   convergence (C010), and idempotent reconciliation are all green.
5. No local/durable resource leak remains after bounded recovery: pass —
   8-concurrent batch plus 4-drop cancellation storm converge to zero
   active requests/reservations, zero flights/gates/jobs, with the next
   valid request succeeding without restart.
6. Public finite/stream endpoints match Python semantically: pass — the
   15+15 endpoint-path matrices assert status/headers/bodies/SSE terminal
   markers, usage, proxy IDs, and durable outcomes per surface.
7. Wire learning/rejection and retry scopes are correct and bounded: pass —
   precedence/TTL/eviction/capacity/throttling/leader-follower/cancellation
   assertions plus the shared `1 + max_retries_before_stream` budget (no
   new budget in C011).
8. Restart reconciliation is idempotent and never replays unknown in-flight
   work: pass — `reconcile_once` fixes exactly once, repeats fix zero,
   writes only the frozen `interrupted`/`crash_recovery`/
   `process_interrupted` markers, never mints attempts/reservations or
   mutates cost/tokens/bytes.
9. No unresolved high/medium M7 correctness/security finding remains: pass —
   the four qualification failures were test-fixture bugs (see below);
   no production defect, no new ADR, no oracle weakening.
10. M8 receives explicit stable interfaces: pass — see the exact handoff
    below.

## Failing-before / passing-after evidence

Before the implementation there was no aggregate M7 qualification:
`rust/tests/coordinator_c011.rs` did not exist, and the C007-C010/C013-C014
suites each proved only their own slice. The full Rust run passed 288
tests with no integrated finite+streaming+endpoint+reconciliation proof.

During qualification four test-fixture bugs were caught and fixed in the
implementation commit (no production change, no oracle change):

- proxied fixture used the non-registered `socks5h://` scheme, so pool
  construction failed closed at `build_state`; fixed to the registered
  `socks5://` topology URL (live proxy interop stays owned by T006).
- virtual-router selector returned the config key `"fast"` instead of the
  compiled route ID `"1"` (label-sorted `0=default`, `1=fast`), so affinity
  never hit and the selector dispatched twice; fixed to `"1"` with
  route-ID/concrete-model assertions.
- failover and storm fixtures undersized their scripted servers (1–2
  scripts for 2–5 requests), so later requests hit closed listeners and
  produced transport failures/conflicts; fixed by sizing servers to the
  planned request count and by isolating the post-handoff `WriteFailed`
  cell on a fresh single-account fixture.
- the streaming-taxonomy cell started two 5s-barrier servers it never
  called, adding 30s of idle join budget; removed in favor of the live
  C008 timeout-failover evidence plus the live EOF/terminal cells.

After implementation, `rust/tests/coordinator_c011.rs` passes 17/17 and
the full Rust run passes 305 tests with no failures (288 pre-existing +
17 new).

## Verification commands actually run

```text
cargo fmt --all -- --check
cargo clippy --all-targets -- -D warnings
cargo test --all-targets                         # 305 passed, 0 failed
cargo test --test coordinator_c011               # 17 passed, 0 failed
uv run pytest tests/migration_rs -q --tb=short --maxfail=1  # 83 passed, 3 skipped
uv run pytest tests/unit/test_retry_classification.py tests/unit/test_backoff.py tests/unit/test_failure_effects_table.py tests/unit/test_request_finalization_state_machine.py tests/unit/test_request_finalization_supervisor.py tests/unit/test_request_finalizer.py tests/unit/test_finalizer_reservation_regression.py tests/unit/test_accepted_finalization_state_machine.py tests/unit/test_stream_completion.py tests/unit/test_stream_diagnostics.py -q --tb=short --maxfail=1  # 238 passed
uv run pytest tests/smoke/ -q --tb=short --maxfail=1  # 14 passed
uv run pyright src/ scripts/                     # 0 errors, 0 warnings
uv run ruff format --check src/ tests/ scripts/  # 728 files formatted
uv run ruff check src/ tests/ scripts/
git diff --check
```

No live/paid provider, external network, schema migration, Eggress
feature change, or Python fixture change was required. `Cargo.toml` and
`Cargo.lock` are untouched; no dependency was added. The only new file is
`rust/tests/coordinator_c011.rs`.

## Security, contention, restart, and resource review

No credential, proxy secret, session identity, request body, provider
body, prompt text, selector response, or diagnostic prose enters
coordinator `Debug`, selector diagnostics, affinity `Debug`, reconciler
reports, or durable `error_detail` beyond lengths and counts. Session
headers are SHA-256 hashed at the endpoint boundary and never forwarded
upstream, persisted, or logged. Upstream request IDs stay bounded to 128
chars with control characters stripped. Client credentials are filtered
before upstream headers are built; response filtering drops
authorization, hop-by-hop, framing, and connection-nominated headers.
The unknown-scheme proxy error carries provider/account identity only.

Every fixing update re-guards on its nonterminal predicate with
ordered-`id` `LIMIT` scans; concurrent reconcilers converge without
double-fixing and bounded passes drain without growth. Wire flights,
gates, learned/rejection maps, effect ledgers, retained jobs, affinity
entries/flights, and stream outcome counters remain bounded through the
existing C005/C006/C013/D007 interfaces; C011 adds no retained state, no
retry budget, no scheduler, no second HTTP stack, no ORM, no actor
framework, and no M8 lifecycle change. Cancellation before handoff
converges as interrupted without downstream start; cancellation or write
failure after handoff converges as cancelled with downstream start and
never replays upstream. No schema fork was introduced.

## Supported differences from the Python oracle

Carried forward unchanged from the accepted C007-C010/C013-C014 closures:

- Provider-error pass-through forwards the raw upstream body without
  transcoder re-encoding; timeout/error envelopes are synthesized per
  client surface (Messages keeps the `api_error` shape).
- `CanonicalUsage` carries no reported-cost fields, so provider/local cost
  remain unset rather than fabricated; token and cache counters persist
  exactly as M6 reports them.
- Latency persists total elapsed and first-byte elapsed; connect/read/
  overhead splits are not tracked separately.
- Release reasons use the Python vocabulary (`completed`,
  `attempt_failed`, `attempt_retryable`, `capability_rejected`).
- A synthetic 503 is returned only when no upstream response was ever
  received and no account remains; terminality, non-retry, and retained
  ownership match Python either way.
- Pre-handoff M7 timer expiry classifies through the transport policy
  table; Python raises distinct stream-timeout outcomes at the same
  boundary with identical retry/terminal behavior.
- Non-SSE upstream bodies preserve the Python legacy pass-through;
  SSE completion rules apply to event-stream responses only.
- Finite Axum handlers converge finalization as `Delivered` before the
  framework writes the body; handler-task cancellation before return
  converges as interrupted without replay (pre-handoff boundary).
- Selector provider pinning uses explicit `model/provider` qualifiers for
  deterministic counts; unqualified selector models resolve by
  load/fairness exactly like concrete routing.
- Reconciliation writes no per-account audit/summary events (Python does);
  durable lifecycle convergence is the frozen contract and reports carry
  the same counts. Reconciliation is the explicit `reconcile_once`
  primitive with a bounded batch (default 500, ceiling 5000); Python
  sweeps in one transaction and M8 owns scheduling.
- Numeric and HTTP-date Retry-After values are capped at the configured
  `max_retry_after` (default 1800s); the committed C001 fixture keeps the
  raw Python parser observation for provenance.

No new supported difference is introduced by C011.

## Exact M8 handoff

M7 exposes these stable, bounded interfaces for M8 generation
publication, teardown/drain, and scheduling. M8 must own when they run,
which generation retains them, and shutdown ordering — not their
semantics.

- Retained terminal ownership: `FinalizationSupervisor::new` /
  `with_capacity` / `with_retry_delay` / `with_fault_injector` /
  `register(FinalizationCommand)` → `FinalizationHandle::wait` /
  `snapshot` / `drain` / `reconcile_once`; `DurableFinalizer::new` /
  `finalize_request` / `finalize_failed_attempt`; `FinalizationCommand`,
  `FinalizationIdentity`, `FinalizationData`, `FinalizationResult`,
  `FinalizationProgress`, `FinalizationError` (including
  `TerminalConflict` and `Capacity`).
- Crash/restart reconciliation: `CrashReconciler::new(database)` /
  `with_batch_limit` / `with_fault_injector` / `reconcile_once()` →
  `ReconciliationReport` (`requests_interrupted`,
  `attempts_terminalized`, `reservations_released`, `classification`,
  `bounded`); `ReconciliationConfig` batch bounds;
  `CoordinatorFaultInjector` / `CrashFaultPoint` test hooks.
- Public inference lifecycle: `InferenceState::from_parts` /
  `finite_coordinator` / `streaming_coordinator` / `router_handle` /
  `active_request_count` / `registry` / `affinity` / `known_providers` /
  `max_body_bytes`; `execute_finite` / `execute_stream` (exactly one
  coordinator invocation per request, typed `FiniteExecution` /
  `StreamingExecution` with monotonic `mark_started`/`handoff_started`
  and terminal `complete(DownstreamResult)`);
  `build_inference_state` for config+DB+pool wiring;
  `parse_provider_qualified_model`, `validate_responses_stateless`,
  `endpoint_error_body`, `new_proxy_request_id`.
- Request leases and routing claims: `RoutingRouter::select_and_claim` →
  `SelectionClaim` with pending/active/quota/probe ownership;
  `RuntimePublicationReceipt` conversion/compensation;
  `PublicationService::publish(PublicationInput)` →
  `PublicationOutcome::Published(PublishedAttempt)` with
  `PublicationStage` identity; `WireResolver` preference/rejection/flight
  state with `WireResolverConfig` bounds; `FailureDecisionEngine` +
  `EffectLedger` first-observation-wins effect ownership.

M7 owns no generation publication, ArcSwap replacement, live rehash,
signal/shutdown orchestration, or recurring scheduling. Its supervisor
and reconciler intentionally expose no background tasks.

## Registry transition and future-plan audit

C011 moves from the dependency-ready table to completed implementation
plans with commit `0216410f` and this accepted closure record. M7
(coordinator/retry/finalization) is closed: C001-C002, C007-C010, and
C012-C014 are closed, C003-C006 remain append-only historical evidence
for the findings corrected by C012-C014, and C011 is the aggregate
closure.

No other implementation plan is unblocked by C011: M8 has no
implementation plan yet and remains gated on its own separate planning
review, which this closure makes eligible. M9-M12 remain sequenced by
`002-long-term-roadmap.md`. There are no unresolved mandatory findings
within C011 scope, and C011 does not claim M8 lifecycle, M9 CLI, M10
qualification, cutover, or retirement parity.

Recommendation: **closed**; M7 is closed and M8 may proceed to its
planning review.
