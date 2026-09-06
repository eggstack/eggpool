# C012 Closure — Coordinator Core Contract Correction

Status: closed

Implementation commits: [`5495f72`](https://github.com/eggstack/eggpool/commit/5495f72), [`2f37f7b`](https://github.com/eggstack/eggpool/commit/2f37f7b)

Plan: [C012 — coordinator core contract correction](../../implementation/coordinator/012-coordinator-core-contract-correction.md)

Repository baseline: `dc3f9970`

## Outcome

C012 corrects the bounded post-C006 coordinator findings without changing the
schema, adding a second HTTP stack, or pulling M8 lifecycle work into M7.
Provider-native model identity now survives the M5 claim, durable publication,
M6 adaptation, path expansion, and provider body. Request construction filters
local credentials/hop-by-hop/framing headers, applies static/surface/auth
precedence, carries bounded request identities, and extracts bounded upstream
request-ID/timing evidence.

Failure observations/effects now carry attempt, provider/account/model/native
identity, protocol/surface, transport phase, signal, model-presence,
Retry-After, downstream-start, and alternate-wire facts. Classification keeps
ambiguous 401 responses non-destructive while distinguishing explicit
credential invalidity, model absence, wire mismatch, rate pressure, transport
failure, and post-handoff no-replay. The effect ledger has explicit capacity,
capacity failure, and retirement.

Wire resolution now uses a structural SHA-256 fingerprint, separate operator
fixed/hint/learned inputs, TTL/cooldown filtering, reactive negotiation delay,
single-flight provider/model coordination, and bounded cache/provider/metric
state. Resolve, accept, and reject all participate in LRU capacity discipline.

Durable finalization validates request/attempt/reservation relationships,
re-reads after zero-row conditional transitions, rejects missing or
incompatible durable truth, updates the existing terminal attempt backlink,
and exposes explicit durable/runtime progress. Retained jobs compare immutable
terminal/accounting facts before sharing work, use injectable bounded retry
delay, and remain cancellation-independent.

## Requirement-to-evidence matrix

| C012 requirement | Evidence | Result |
|---|---|---|
| C003 fixed/hint/learned ordering, TTL/cooldown, fingerprint, delay, and bounds | `WireResolver` implementation; `wire_state_is_bounded_on_all_insertion_paths_and_rate_delay_is_reactive`; existing leader/follower test | Pass |
| C004 provider-native alias survives actual request path/body | `attempt_preparation_expands_path_and_never_debugs_credentials` asserts `/v1/upstream-model-a/stream` and native JSON model | Pass |
| C004 header/auth/forwarding/request-ID/redaction boundary | same attempt test asserts credential replacement and denied connection-nominated forwarding; redacted `Debug` | Pass |
| C005 complete policy dimensions and ambiguous auth | `FailureObservation`/`FailureEffects`; `failure_policy_distinguishes_ambiguous_credentials_and_model_evidence` | Pass |
| C005 response-start no replay and Retry-After bound | `failure_classifier_enforces_handoff_and_retry_after_bounds`; classifier handoff guard | Pass |
| C005 exactly-once state bounded and retires | `effect_ledger_retirement_keeps_capacity_available`; `EffectLedger::try_apply_once`/`retire` | Pass |
| C006 zero-row durable truth and identity relationships | `finalization_rejects_missing_durable_identity_and_incompatible_jobs`; durable re-read branches | Pass |
| C006 incompatible retained commands fail closed | same test; `CommandCompatibility` registration check | Pass |
| C006 explicit progress and resumable retained work | `FinalizationProgress`, retained supervisor, configurable retry delay, existing duplicate/failure cleanup tests | Pass |
| No schema/dependency/M8 scope expansion | migration count remains 54; `Cargo.toml` unchanged; diff contains no schema, scheduler, generation, or second client changes | Pass |

## Verification commands actually run

```text
rtk cargo fmt --manifest-path rust/Cargo.toml -- --check
rtk cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
rtk cargo test --manifest-path rust/Cargo.toml --all-targets       # 198 passed
rtk uv run pytest tests/migration_rs -q --tb=short --maxfail=1     # 83 passed, 3 skipped
rtk uv run pytest tests/unit/test_failure_effects_table.py tests/unit/test_effects_idempotency.py tests/unit/test_wire_resolver.py tests/unit/test_request_finalization_state_machine.py tests/unit/test_request_finalization_supervisor.py tests/unit/test_finalizer_reservation_regression.py tests/integration/test_wire_negotiation_e2e.py tests/integration/test_failover_matrix.py -q --tb=short --maxfail=1  # 130 passed
rtk uv run ruff format --check tests/migration_rs
rtk uv run ruff check tests/migration_rs
rtk git diff --check
```

No live provider, credential, database migration, dependency, or network
prerequisite was used.

## Future-plan audit and registry transition

C012 is removed from the dependency-ready table and recorded as completed.
C013 is promoted as the sole dependency-ready coordinator plan because it is
the explicit independent requalification gate. C007 remains re-blocked behind
accepted C013 closure; C008-C011 retain their serial dependencies. M8 remains
blocked on accepted C011 M7 closure and its separate planning review. The
historical C003-C006 closure records are unchanged.

Unresolved mandatory findings within C012 scope: none. C013 still owns
differential requalification and may create a new corrective plan if that
evidence exposes a defect; this closure does not claim C007 readiness.
