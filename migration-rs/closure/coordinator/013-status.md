# C013 Closure — Coordinator Core Differential Requalification

Status: closed

Implementation commit: [`85ad837b`](https://github.com/eggstack/eggpool/commit/85ad837b4008184c8f23468fae8a4968b8071a48)

Plan: [C013 — coordinator core differential requalification](../../implementation/coordinator/013-coordinator-core-differential-requalification.md)

Repository baseline: `3673bc3454b3afa3d2d8bffcaa5f2ad86956b861`

## Outcome

C013 independently requalified the corrected C003-C006 core against the
committed C001 Python observation corpus and deterministic M4/M5/M6 fixtures.
The Rust classifier now preserves the Python policy vocabulary and local-source
distinctions, including retry destination/scope, client outcome, account/model/
wire effects, circuit/probe behavior, evidence class, and relative durable
backoff. The committed observation projection was extended to carry those
policy-bearing fields; no secret, body, path, process, or credential data was
added.

The corrected wire resolver is exercised through fixed, hinted, learned, and
configured ordering, learned expiry, rejection cooldown, structural
fingerprints, bounded LRU/provider state, rate-limit negotiation delay,
leader/follower sharing, cancellation, gate saturation, and independent
providers. The provider-attempt fixture sends one real request to a local
HTTP target and observes the provider-native model alias in both path and
body, together with static/surface/auth/forwarded/request-ID behavior.

Durable finalization now compares retained-command identity as well as terminal
facts. Replacement publication refuses to acquire durable attempt ownership
until the prior attempt's durable attempt and reservation state has converged.
Integrated two-attempt and negative-race fixtures prove that retry ownership is
fresh, prior ownership is released, and the final request converges once.

## Requirement-to-evidence matrix

| C013 requirement | Evidence | Result |
|---|---|---|
| C001 failure/effect differential matrix | `rust/tests/coordinator_c013.rs::c013_failure_effects_match_every_committed_c001_policy_case` compares all 23 committed cases across retry, action/scope, outcome, account/model/wire, evidence, circuit/probe, durable backoff, and response-start semantics | Pass |
| Ambiguous/explicit auth, model absence/wire mismatch, 429, transport phases, local errors | Same differential test constructs each typed source/signal; `FailureEffects` keeps local/database failures non-destructive and distinguishes the required provider scopes | Pass |
| Retry-After parsing and bounds | `c013_retry_after_parsing_preserves_python_semantics_and_bound`; numeric values are bounded, dates and invalid/missing values match the C001 oracle | Pass |
| Wire ordering, TTL, cooldown, fingerprint, delay, bounds | `c013_wire_precedence_ttl_eviction_and_concurrency_are_bounded` plus existing coordinator boundary coverage | Pass |
| Wire leader/follower/cancellation/gate behavior | Same test covers shared acceptance, leader cancellation, throttling, post-delay recovery, provider gate saturation, and independent providers; final snapshot has no flights or gates | Pass |
| Provider-native identity and C004 request boundary | `c013_attempt_submission_observes_native_alias_and_one_request` target-observes `/v1/provider-native/dispatch`, native JSON model, static/surface/auth/allowed headers, denied headers, correlation IDs, and one request | Pass |
| M4 transport/no implicit replay and proxy/direct boundaries | Existing `rust/tests/provider_transport.rs` transport fixture suite; full Rust target run | Pass |
| Effect idempotency and bounded retirement | `c013_effect_ledger_retires_before_capacity_and_decision_cannot_hide_overflow` processes 512 retired attempts and proves capacity errors are surfaced before ownership | Pass |
| Durable finalization truth and supervisor compatibility | Existing finalization convergence tests plus missing request/attempt/reservation and incompatible identity-command assertions in `coordinator_finalization.rs` | Pass |
| Replacement ownership ordering | `retry_replacement_waits_for_prior_attempt_cleanup_and_converges_once` and `replacement_claim_cannot_bypass_a_blocked_prior_publication` | Pass |
| Python/Rust oracle safety | C001 observation projection remains scalar and secret-safe; migration C001 snapshot tests and Pyright/Ruff checks pass | Pass |
| Schema/dependency/scope review | No schema, Cargo.toml, HTTP stack, workflow, live-provider, Docker, or M8 lifecycle changes; `cargo tree --depth 0` reviewed | Pass |

## Verification commands actually run

```text
rtk cargo fmt --all -- --check
rtk cargo clippy --all-targets -- -D warnings
rtk cargo test --all-targets                         # 205 passed
rtk uv run pytest tests/migration_rs -q --tb=short --maxfail=1  # 83 passed, 3 skipped
rtk uv run pytest tests/unit/test_failure_effects_table.py tests/unit/test_effects_idempotency.py tests/unit/test_wire_resolver.py tests/unit/test_request_finalization_state_machine.py tests/unit/test_request_finalization_supervisor.py tests/unit/test_finalizer_reservation_regression.py tests/integration/test_wire_negotiation_e2e.py tests/integration/test_failover_matrix.py -q --tb=short --maxfail=1  # 130 passed
rtk uv run ruff format --check tests/migration_rs
rtk uv run ruff check tests/migration_rs
rtk uv run pyright tests/migration_rs/coordinator_fixtures.py tests/migration_rs/test_c001_coordinator.py
rtk cargo tree --depth 0
rtk git diff --check
```

## Security, contention, restart, and resource review

No credential, authorization value, proxy secret, raw provider body, prompt,
response, or session identity is retained in the new differential state. The
target-observed fixture asserts auth replacement and denied client-controlled
headers; debug output remains structural. Wire flights, provider gates,
effect records, retained jobs, and resolver maps are explicitly bounded and
retired. Leader/follower cancellation and SQLite publication cancellation
fixtures leave no flight, gate, active claim, or durable-row fanout. C013
qualifies retained finalization interfaces but does not claim M8 restart scan,
generation publication, rehash, shutdown, or recurring scheduling parity.

Known supported normalization is limited to injected epoch time versus Rust
relative `Duration` backoff, synthetic SQLite IDs, and exception wording. No
unresolved high- or medium-severity C003-C006 correctness/security finding
remains.

## Registry transition and future-plan audit

C012 and C013 are recorded as closed. The M7 corrective core is closed while
aggregate M7 remains open under C011. C007 is promoted as the sole
dependency-ready plan because its hard dependency is now accepted. C008 stays
blocked on C007, C009 on C008, C010 on C009, and C011 on C010. M8 remains
blocked on accepted C011 M7 closure and its separate planning review. No other
future plan is unblocked by this core requalification.

Recommendation: **closed**; C007 may proceed, with C008-C011 and M8 retaining
their existing serial gates.
