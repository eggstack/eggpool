# R013 Closure — Wire-Policy Acceptance and Boundary Requalification

Status: closed

Recommendation: closed; M8 is re-closed and M9 is eligible for its own
planning/implementation review

Implementation commit: `55a01b3`

Plan: [R013 — wire-policy acceptance and boundary requalification](../../implementation/runtime-lifecycle/013-wire-policy-acceptance-and-boundary-requalification.md)

## Outcome

R013 closes the four residual M8 findings identified after R012. Rust now
rejects the exact current Python wire-policy bounds, converts runtime durations
fallibly, stages policy without mutating shared resolver authority, publishes
that policy only after the durable reload transaction commits and before
admission reopens, and immediately enforces restored bounds on rollback. The
real Axum `/v1/chat/completions` path proves that accepted process policy is
request-visible and rejected policy is not.

The initial R013 handoff table described learned TTL as allowing zero. The
current Python authority is `Field(..., gt=0, le=604800.0)`, so the plan and
roadmap were corrected to record the actual contract: strictly positive and at
most 604800 seconds. No silent normalization was introduced.

## Requirement-to-evidence matrix

| R013 requirement | Evidence | Result |
|---|---|---|
| Exact Python wire-policy bounds | `wire_configuration_matches_python_bounds_exactly`; `Config::validate` in `rust/src/config.rs`; `WireResolverConfig::from_config` in `rust/src/coordinator/wire_resolver.rs` | Pass |
| Huge finite and non-finite runtime values fail closed | `programmatic_invalid_wire_policy_fails_closed_without_panicking`; `invalid_startup_and_live_staging_return_errors_instead_of_panicking` | Pass |
| One process-owned resolver survives generation changes | R012 shared-resolver tests plus R013 accepted Axum reload | Pass |
| Candidate policy is invisible before durable acceptance | `rejected_reload_preserves_authority_and_accepted_reload_publishes_once`; reload ordering in `rust/src/reload.rs` | Pass |
| Persistence commit failure does not expose candidate policy | R013 feature-gated `PersistenceCommit` fault | Pass |
| Post-commit adoption cannot mix runtime and wire policy | `ReloadService` commits wire policy while admission remains closed, then accepts or fail-closed-adopts the staged generation | Pass |
| Rollback restores policy and bounds immediately | `rollback_restores_policy_and_bounds_before_returning` | Pass |
| Outstanding negotiation permits converge after rollback | Same test keeps two old leaders outstanding, restores concurrency one, then verifies new leadership after completion | Pass |
| Real public M7 inference request observes accepted policy | `real_axum_inference_observes_accepted_policy_and_not_rejected_policy` | Pass |
| Full ReloadService rejection/fault isolation | `reload_fault_and_rejection_matrix_preserves_old_authority`; retirement backlog test | Pass |
| Retained diagnostics under cancellation/Busy/shutdown | R012 retained diagnostic suite, rerun unchanged | Pass |
| No schema, dependency, M9, or second HTTP/runtime stack | Source/dependency/security review below | Pass |

## Python-bound parity

| Field | Python authority | Rust authority | Boundary evidence |
|---|---:|---|---|
| `max_concurrent_per_provider` | `1..=8` | `Config::validate`; fallible resolver construction | 1, 8 accepted; 0 and 9 rejected |
| `min_negotiation_interval_s` | `0..=1800` | `Config::validate`; checked `Duration` conversion | 0, 1800 accepted; negative and 1801 rejected |
| `rejection_cooldown_s` | `0..=1800` | `Config::validate`; checked `Duration` conversion | 0, 1800 accepted; negative and 1801 rejected |
| `learned_preference_ttl_s` | `0 < value <= 604800` | `Config::validate`; checked `Duration` conversion | epsilon and 604800 accepted; 0, 604801, negative, huge, infinity, NaN rejected |
| `cache_max_entries` | `1..=65536` | `Config::validate`; resolver policy construction | 1, 65536 accepted; 0 and 65537 rejected |

Both configuration parsing and direct Rust construction are fail-closed. The
fallible conversion uses `Duration::try_from_secs_f64`; it does not depend on
TOML validation as its only panic barrier.

## Failing-before / passing-after evidence

The R012 baseline at `a4495488` accepted finite floating-point wire durations
after only a finite/non-negative check and used `Duration::from_secs_f64`; a
huge finite value could therefore reach a panic during startup or live policy
staging. The same baseline committed the shared policy before the SQLite
transaction committed and did not enforce restored bounds in rollback.

R013 adds exact boundary tests, typed `WireResolverConfigError` construction
failures, and the ordered reload path. The R013 default suite passes 7 tests;
the feature-gated fault matrix passes 8 tests. No test weakens an older R012
assertion.

## Acceptance ordering

Before R013, the effective order was:

```text
prepare candidate
  -> stage wire policy
  -> apply SQLite transaction
  -> commit shared wire policy       [externally visible too early]
  -> commit generation pointer/tasks
  -> commit SQLite transaction
  -> accept/reopen admission
```

R013 makes the acceptance window coherent:

```text
prepare candidate and policy stage   [validation only; shared policy unchanged]
  -> apply SQLite transaction
  -> commit staged generation pointer/tasks while admission is closed
  -> durable SQLite commit           [irreversible point]
  -> publish shared wire policy
  -> accept or fail-closed-adopt matching generation/tasks
  -> finalize policy stage and reopen admission
```

Every pre-accept return rolls back the staged pointer/tasks and leaves the old
resolver policy authoritative. After the durable point, the new policy is
finalized together with the adopted new runtime, including shutdown adoption.

## Reload rejection and fault matrix

The R013 matrix captures generation id, publication epoch, resolver snapshot,
admission state, and terminal diagnostic state before and after each case. For
all pre-accept cases the baseline remains generation 1 / epoch 0, the resolver
snapshot is unchanged, admission is open, and diagnostics are idle after the
operation.

| Case | Result/reason | Evidence |
|---|---|---|
| No-op | `Noop` / `no_changes` | R013 feature matrix |
| Restart-required | `RestartRequired` / `restart_required` | R013 default and feature matrices |
| Mixed live + restart-required | `RestartRequired` / `restart_required` | R013 feature matrix |
| Invalid TOML | `ValidationFailed` / validation | R013 feature matrix |
| Python-bound-invalid wire setting | `ValidationFailed` / validation | R013 feature matrix |
| Stale expected digest | `StaleDigest` / `digest_mismatch` | R013 feature matrix |
| Candidate construction | `Aborted` / `candidate_prepare_failed` | R013 feature matrix uses URI-parser failure after config validation |
| Task preflight | `Aborted` / `task_preflight_failed` | One-shot `ReloadTestFault::TaskPreflight` |
| Task commit | `Aborted` / `task_commit_failed` | One-shot `ReloadTestFault::TaskCommit` |
| Persistence begin | `Aborted` / `persistence_begin_failed` | One-shot `ReloadTestFault::PersistenceBegin` |
| Persistence apply | `Aborted` / `persistence_apply_failed` | One-shot `ReloadTestFault::PersistenceApply` |
| Persistence commit | `Aborted` / `persistence_commit_failed` | One-shot `ReloadTestFault::PersistenceCommit` plus compensation |
| Retirement backlog | `RetirementBacklog` / `retirement_backlog` | Four held old-generation leases fill the manager bound |
| Caller cancellation before observed completion | retained worker completes; admission converges | R012 cancellation/Busy storm test |
| Shutdown racing with acceptance | retained worker converges with shutdown-owned gate | R012 shutdown race test |
| Accepted live wire-policy reload | `Applied` / `applied`, epoch increments once | R013 default matrix and Axum test |

`ReloadTestFault` is compiled only with the existing `test-support` feature and
is one-shot, in-memory test control. It adds no normal fault-injection surface,
task, database table, network path, or production dependency.

## Immediate rollback-bound evidence

The rollback regression starts with cache capacity 2, provider-state bound 1,
metric-label bound 1, and concurrency 1. A staged policy raises those to 16,
creates 16 cache entries, and starts two old leaders. Rollback returns with
the exact old policy restored and cache entries already at most 2, without a
follow-up resolver operation. The two old leaders are allowed to finish; the
provider gate then has at most one retained gate and a new leader can acquire
without deadlock or permit underflow.

## Real Axum/M7 inference evidence

The local provider binds to loopback and returns deterministic OpenAI or
Anthropic JSON. The test boots the real Axum router and M7 coordinator with
`/v1/chat/completions`, first with a learned Anthropic preference. The observed
provider paths are:

```text
baseline request       /messages
rejected reload request /messages
accepted disabled-policy request /chat/completions
```

The invalid reload attempted to disable negotiation while also setting
`cache_max_entries = 65537`; it was rejected and did not change the second
request. The accepted reload disabled negotiation, so the static candidate
order selected `/chat/completions`. All three finite requests returned 200,
and the provider task, generation manager, task supervisor, and database were
closed cleanly.

## Reload-diagnostic correction retained

R012's retained-worker tests were rerun without modification. They cover
caller cancellation while the owned worker succeeds, Busy callers that cannot
clear the owner, a Busy/cancellation storm followed by convergence, and
shutdown racing with the retained worker. The assertions retain exactly one
attempt for the owned transaction, accepted/failure accounting, idle
`reload_in_progress`, bounded projections, and secret-free diagnostics.

## Verification

```text
rtk cargo fmt --manifest-path rust/Cargo.toml --all -- --check
rtk cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
rtk cargo clippy --manifest-path rust/Cargo.toml --features test-support --all-targets -- -D warnings
rtk cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r013 -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --features test-support --test runtime_lifecycle_r013 -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1
rtk uv run pytest tests/migration_rs -q --tb=short --maxfail=1
rtk uv run pytest tests/unit/test_config_reload_policy.py tests/integration/test_rehash_acceptance.py tests/integration/test_rehash_retirement_edge_cases.py tests/integration/test_rehash_streaming_swap.py tests/integration/reload/test_stale_app_state.py tests/integration/reload/test_diagnostics_contract.py tests/integration/reload/test_reload_diagnostics_assertions.py -q --tb=short --maxfail=1
rtk uv run pytest tests/smoke/ -q --tb=short --maxfail=1
rtk uv run pyright src/ scripts/
rtk uv run ruff format --check src/ tests/ scripts/
rtk uv run ruff check src/ tests/ scripts/
rtk git diff --check
```

Observed results:

- Rust aggregate: 386 passed across 42 suites.
- R013 focused Rust: 7 passed; test-support fault matrix: 8 passed.
- Affected runtime suites: R003 6, R005 9, R007 6, R009 5, R010 4, R011 10,
  and R012 10 passed.
- Slow affected coordinator regressions: C008 29, C009 13, C010 20, and C011
  17 passed.
- Python migration oracle: 89 passed, 3 skipped.
- Python reload/config focus: 89 passed, 1 skipped.
- Python smoke: 14 passed.
- Pyright: 0 errors, 0 warnings, 0 informations.
- Ruff formatting: 730 files already formatted; Ruff checks passed.
- Git diff check: passed.

No paid or live provider was used.

## Resource, dependency, schema, and security review

The process still owns exactly one bounded resolver. Cache, provider-state,
metric-label, provider-gate, task, retirement, and diagnostic bounds remain
enforced. No new database migration, database table, scheduler, HTTP client
stack, actor/workflow framework, or normal runtime dependency was added. The
existing `test-support` Cargo feature gates only deterministic test controls;
Cargo's dependency set is unchanged.

Resolver fingerprints, test faults, reload reasons, and diagnostics contain no
credentials, proxy URLs, request bodies, provider bodies, or unbounded error
text. The local inference fixture is loopback-only and uses no secrets.

No unresolved high/medium M8 correctness, resource, security, compatibility,
schema, dependency, or lifecycle finding remains.

## Future-plan audit and registry transition

R013 is removed from the dependency-ready table and recorded in the completed
implementation table with commit `55a01b3`. The implementation index, handoff
sequence, subsystem roadmap, and registry now mark M8 closed after R013.
R011 and R012 remain append-only historical closure evidence and were not
rewritten. M9 is eligible for its own separate planning/implementation review;
no M9 plan is created or auto-promoted. M10-M12 remain sequenced by the
long-term roadmap, and no other represented future plan has a newly satisfied
direct dependency requiring a status change.
