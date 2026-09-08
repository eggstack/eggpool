# R007 Closure — Transactional Live Rehash and Coherent Acceptance

Status: closed

Recommendation: closed

Implementation commit: [1b03ade393e7caa532d71ee3b9ce9b3989c49612](https://github.com/eggstack/eggpool/commit/1b03ade393e7caa532d71ee3b9ce9b3989c49612)

Plan: [R007 — transactional live rehash and coherent acceptance](../../implementation/runtime-lifecycle/007-transactional-live-rehash-and-coherent-acceptance.md)

Repository baseline: 9124e6ae30675692854e2abbe598cf80d5b7fab4

## Outcome

R007 adds the typed server-side ReloadService and composes the closed R002
generation factory, R003 staged publication/leases, R004 retirement backlog,
R005 fail-closed config diff, and R006 task-spec supervisor into one
process-owned reload transaction. M9 control-socket and CLI work remains out
of scope.

The service accepts a canonical config path or already-read TOML bytes,
optionally verifies an expected content digest, and returns only bounded
secret-free result data: category, active generation/digest prefix, changed
sections, restart-required paths, retirement-pending state, and a reason code.
The reload lock is process-owned and shared by all service handles.

## Requirement-to-evidence matrix

| R007 requirement | Evidence | Result |
|---|---|---|
| Typed result vocabulary | ReloadResultCategory covers applied, no-op, restart-required, validation, stale digest, busy, retirement backlog, aborted, and compensation-failed outcomes | Pass |
| No-op and restart/mixed fail-closed behavior | identical_semantic_config_is_a_noop_without_candidate_or_epoch_change and restart_invalid_and_stale_inputs_leave_runtime_unchanged | Pass |
| Candidate isolation and new-account support | PersistenceDelta projects stable/new account identities before factory construction; new_account_is_projected_before_candidate_build_and_persisted_atomically | Pass |
| Serialized reload and stale protection | process-owned async reload mutex, active-generation snapshot, R003 expected-generation stage check, concurrent_callers_are_serialized_with_a_busy_result | Pass |
| Narrow admission gate | TOML read/validation/diff, persistence snapshot, projected account ids, candidate factory, and task preflight all run before RuntimeManager::stage; only SQLite delta, pointer commit, task commit, and accept run with admission closed | Pass |
| SQLite/runtime/task coherence | caller-controlled DatabaseTransaction holds BEGIN IMMEDIATE across pointer/task acceptance; commit failure restores durable rows, pointer, task specs, or enters typed fail-closed compensation | Pass |
| Existing-schema persistence safety | provider/account upsert and disable-only reconciliation use the existing providers, accounts, and account_backoffs tables; request/attempt/reservation history is never deleted | Pass |
| Authentication reset and compensation | credential identity changes clear only terminal authentication hints, with targeted row snapshots for inverse restoration | Pass |
| Candidate cleanup | every pre-stage candidate error explicitly aborts; staged failures roll back the pointer/gate and close transferred candidate resources; R002/R007 ownership remains explicit | Pass |
| Cancellation and shutdown | reload work is owned by a spawned transaction task; shutdown-era durable commits use a fail-closed accepted pointer, and unrecoverable post-commit errors cannot restore the old pointer into a mixed database state; caller_cancellation_does_not_leave_admission_closed | Pass |
| Retirement | accepted publication transfers the old slot to R004 manager-owned retirement; retirement backlog is returned as a distinct result before staging | Pass |
| Deferred task capabilities | R007 stages only callback capabilities present in the R006 process supervisor; R008/M9 callbacks remain explicit missing capabilities and no no-op loops are created | Pass |
| Dependency/schema/security scope | no Cargo dependency, schema migration, provider/network operation, control socket, CLI, or raw config/credential diagnostic was added | Pass |

## Implementation notes and supported differences

The existing async SQLite API exposed only an all-in-one transaction closure.
R007 adds a deliberately narrow caller-controlled transaction primitive so the
short acceptance section can hold the SQLite worker gate while committing the
staged runtime pointer and task diff. Normal repositories continue to use the
existing all-in-one transaction API.

Candidate account projections reserve deterministic SQLite ids during
preflight and insert those ids explicitly in the acceptance transaction. This
keeps a newly configured account available to the immutable candidate graph
without mutating durable state during candidate construction. Removed
configured accounts and providers are disabled/retained, preserving historical
M7 foreign-key identities.

R006 intentionally has only the process-owned checkpoint callback. R007
therefore reconciles the available task subset and leaves catalog,
retention, metrics, update, and backup business callbacks to R008/M9. It does
not create placeholder background work.

## Verification commands actually run

    rtk cargo fmt --manifest-path rust/Cargo.toml --all -- --check
    rtk cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
    rtk cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r007 -- --test-threads=1
    rtk cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1
    rtk uv run pytest tests/migration_rs -q --tb=short --maxfail=1
    rtk uv run pytest tests/unit/test_config_reload_policy.py tests/integration/test_rehash_acceptance.py tests/integration/test_rehash_retirement_edge_cases.py tests/integration/test_rehash_streaming_swap.py -q --tb=short --maxfail=1
    rtk uv run pytest tests/smoke/ -q --tb=short --maxfail=1
    rtk git diff --check

Observed results:

- Rust R007: 6 passed.
- Rust aggregate: 347 passed across 36 suites.
- Python migration oracle: 89 passed, 3 skipped.
- Python reload/config integration bundle: 68 passed, 1 skipped.
- Python smoke: 14 passed.
- formatting, Clippy, and diff checks passed.

## Unresolved findings

No unresolved R007 correctness, resource, security, compatibility, or schema
finding remains. Deferred callback capabilities are intentional and owned by
R008/M9 as described above.

## Future-plan audit and registry transition

R007 moves from the dependency-ready table to the completed implementation
table with implementation commit
1b03ade393e7caa532d71ee3b9ce9b3989c49612 and this append-only closure
record. R008 is promoted as the sole dependency-ready M8 plan because its
hard dependency, accepted R007 closure, is now satisfied.

R009 remains queued behind R008; R010 remains queued behind R009; R011
remains queued behind R010 and is still the only plan allowed to close M8.
M9 operational CLI/control/daemon work remains blocked on accepted R011 M8
closure and its separate planning/implementation review. No later plan is
unblocked by R007 beyond R008.
