# Routing Selection Milestone 008 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/routing-selection/008-durable-health-state-repair-and-authentication-failure-durability.md`

Decision implemented:

- `plans/adrs/ADR-0005-durable-health-state-repair-and-authentication-failure-durability.md`
  (accepted 2026-10-25; Option A)

Source subsystem roadmap:

- `plans/subsystems/routing-selection-roadmap.md`

Repository baseline reviewed: `239afb7a680d909703bdea2d961bd45081a3341c`

Implementation commits or pull requests:

`b66373f2`

- Durable health state repair and authentication-failure durability: the
  hydration skips and reports a legacy row instead of failing the batch, the
  durable account-scoped scope is unique, and the live failure path writes the
  deadline-less `authentication_failed` row.

## 1. Executive finding

The durable health state is now internally consistent in all three respects the
plan names.

A row this runtime cannot interpret no longer decides whether the process
starts. A legacy `model_unavailable` row whose `model_id` is NULL — the shape a
restored backup, a hand-edited database, or a writer this runtime does not own
can leave behind — is parsed, skipped, counted, and reported. The hydration
batch succeeds, the generation is published, and the skipped row grants neither
an account-wide nor a model-wide suppression, because the row has no model
identity to scope it by and no coherent account-wide meaning.

The durable scope key is unique where the table's own migration documents it
must be. Migration v64 adds the partial unique index
`uq_account_backoffs_null_model` over `(account_id, reason)` where
`model_id IS NULL`, after the duplicate-row cleanup that must precede it. The
NULL-safe lookup key `model_id IS ?2` is unchanged; the defect was never the
key.

The credential rejection is durable. The live failure path now writes the
deadline-less `authentication_failed` row that `validate_backoff_record`
accepts, that `hydrate_backoffs` honours with `disable_account(name, reason,
None)`, that `reload.rs` clears for an account whose credential changed, and
that `clear_authentication` deletes. Before this milestone that row shape had a
reader, a validator branch, a rehash path, and a cleanup, but no writer: the
`AuthenticationFailed` policy declares no delay, so
`compute_backoff_seconds` returns `None` and the durable writer returned early.
An account whose credential was rejected was eligible again after a restart.

The capability boundary is complete for this milestone. The backoff policy
table, the reason vocabulary, the scope rules, the escalation rungs, the
quarantine state machine, and the routing behaviour of a healthy system are
unchanged and are guarded by the pre-existing assertions, all of which still
pass.

## 2. Requirement-to-evidence matrix

Every acceptance criterion in the plan's §13 is listed, with the test or
command that proves it and the observed result.

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| A database containing a legacy `model_unavailable` row with a NULL `model_id` starts, and the skip is reported | `rust/tests/durable_health_effects.rs::a_legacy_model_unavailable_row_is_skipped_and_reported` | pass | The hydration returns `Ok`; `report.skipped_legacy_backoffs == 1` and `report.backoffs == 0`; the account has no cooldown, no expiry, and no disabled model |
| A database containing a legacy row still publishes a generation | `rust/tests/durable_health_effects.rs::a_legacy_row_does_not_prevent_a_generation_from_being_published` | pass | `build_generation` hydrates and publishes; `account-a` is still a routing candidate |
| The durable backoff table rejects a duplicate account-scoped row for the same `(account_id, reason)` | `rust/tests/durable_health_effects.rs::the_durable_schema_rejects_a_second_account_scoped_row` | pass | The second insert of `(1, NULL, 'rate_limited')` is rejected and exactly one row remains |
| The v64 migration removes the legacy rows, collapses the NULL-scope duplicates, leaves the model-scoped row alone, and installs the index | `rust/tests/database_compatibility.rs::the_v64_migration_removes_the_legacy_rows_and_constrains_the_null_scope` | pass | The legacy NULL-scope `model_unavailable` row is gone; the duplicate NULL-scope `rate_limited` pair collapses to the most recently updated row; the model-scoped row survives; the index then rejects a new duplicate |
| An account whose credential was rejected is still disabled after a restart | `rust/tests/durable_health_effects.rs::a_credential_rejection_writes_the_durable_authentication_failed_row` | pass | The live path writes `(account_id, NULL, 'authentication_failed')` with no deadline; a fresh hydration restores `health_state == "authentication_failed"` with `disabled_until == None` |
| A credential rotation still re-enables the account | `rust/tests/durable_health_effects.rs::a_credential_rotation_releases_the_durable_authentication_failure` | pass | The credential-change release deletes the row and the next hydration leaves the account enabled |
| The live path still writes no `model_unavailable` row with a NULL `model_id` | `rust/tests/durable_health_effects.rs::the_live_path_writes_no_model_unavailable_row_with_a_null_model` | pass | Only the model-scoped `model_unavailable` row is present |
| The migration chain is contiguous from v1 to v64 | `rust/tests/database_compatibility.rs::the_v64_migration_removes_the_legacy_rows_and_constrains_the_null_scope`; `rust/tests/database_compatibility.rs::fresh_database_migrates_idempotently_and_preserves_pragmas` | pass | `applied_versions.first() == Some(&1)` and `.last() == Some(&64)`; the applied count is 64 |
| The checksum inventory guard passes | `rust/tests/build_manifest.rs` (4 passed); `rust/src/db/migrations.rs::canonical_inventory_is_complete_and_immutable` | pass | 64 checksum entries; the embedded digests match; the runtime asset manifest's digest matches `checksums.json` |
| The backoff policy table, the reason vocabulary, the scope rules, the escalation rungs, the quarantine state machine, and the routing behaviour of a healthy system are unchanged | `git diff -- rust/src/health/backoff.rs` is empty; `rust/tests/backoff_policy_adoption.rs` (12 passed); `rust/tests/health.rs` (15 passed); `rust/tests/durable_health_effects.rs` (18 passed, including the twelve pre-existing tests unchanged) | pass | The policy table was not edited; the durable-effects target grew from twelve tests to eighteen without changing one of the twelve |
| The default and no-default-features workspace suites pass serially | `cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --offline --locked -- --test-threads=1`; the `--no-default-features` equivalent | pass | 1,215 passed / 0 failed (default); 1,216 passed / 0 failed (no default features) |
| The tooling suite, the linters, and the release-documentation validator pass | `uv run pytest tests/tooling/ -q --tb=short`; `uv run ruff format --check scripts/ tests/tooling/`; `uv run ruff check scripts/ tests/tooling/`; `uv run pyright scripts/`; `uv run python scripts/validate_release_docs.py` | pass | 251 passed / 1 skipped; 71 files already formatted; All checks passed; 0 errors; status `pass` |

## 3. Production implementation evidence

`rust/src/health/health_manager.rs`, `rust/src/health/repository.rs`, and
`rust/src/routing/router.rs` are the only production files changed by this
milestone.

- `HealthManager::hydrate_backoffs` no longer returns
  `Err(HealthManagerError::MissingModel)` for a `model_unavailable` row whose
  `model_id` is NULL. The row is skipped, counted, and the batch continues. It
  returns the applied count and the skipped-legacy count.
- `HealthManagerError::MissingModel` became unreachable and was removed
  together with its `Display` arm. `EndpointError::MissingModel` in
  `coordinator/endpoints.rs` belongs to a different subsystem and was left
  alone. `HealthManagerError::UnknownAccount` was already unused before this
  milestone and was left alone.
- `parse_backoff` no longer rejects the legacy shape at parse time. It
  recognises it, and exempts it from the non-terminal-expiry requirement, so
  the row reaches the hydration that skips and reports it. Every other
  malformed row is still rejected.
- `AccountBackoffRepository::hydrate_into` forwards both counts.
- `HealthHydrationReport` gained `skipped_legacy_backoffs`.
  `hydrate_generation_health` fills it and surfaces it through the existing
  diagnostics path: a `tracing::warn!` carrying the count and the reason
  vocabulary (`model_unavailable`) only. No credential, prompt, raw body, or
  cache key is involved, and no new logging framework was introduced.
- `RoutingRouter::persist_account_backoff` writes the deadline-less
  `authentication_failed` row on the `(account_id, NULL,
  'authentication_failed')` key. Every other reason keeps its early return, so
  no other reason's durable behaviour changed, and the in-memory disable and
  its timing are unchanged. The durable row is an addition.
- `rust/src/health/backoff.rs` is unchanged. The policy table, the reason
  vocabulary, the escalation rungs, and `compute_backoff_seconds` are exactly
  as M006 adopted them.

The v64 migration and its checksum were already committed before this
milestone and were not edited. The migration's SQL and its checksum entry are
outside this milestone's file ownership.

Three inventory assertions that state the current migration chain were stale
at the committed v64 migration and are corrected here, because the suite could
not otherwise pass: `rust/src/db/migrations.rs`
(`canonical_inventory_is_complete_and_immutable` asserted 63), and
`rust/tests/build_manifest.rs` with `rust/assets/runtime-manifest.json`
(`parsed.len()` asserted 63, `schema_migration_count` asserted 63, and the
recorded SHA-256 of `checksums.json` was the pre-v64 digest).

## 4. Verification executed

### Commands run

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --offline --locked -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features --offline --locked
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features --offline --locked -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --offline --locked -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features --offline --locked -- --test-threads=1
uv run pytest tests/tooling/ -q --tb=short
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run python scripts/validate_release_docs.py
```

`--offline --locked` was added to every cargo invocation because the network is
blocked in this environment and a re-resolving build hangs. The `uv run`
commands were run without `--offline`; `uv.lock` and `pyproject.toml` were
verified byte-identical afterwards (`git status --short` clean for both).

### Results

| Command | Exit | Observed |
|---|---|---|
| `cargo fmt --all -- --check` | 0 | No diff; the tree is formatted |
| `cargo clippy --workspace --all-targets -- -D warnings` | 0 | No warnings, no allowlist added |
| `cargo check --workspace --all-targets --no-default-features` | 0 | Compiles without the SSH default |
| `cargo clippy --workspace --all-targets --no-default-features -- -D warnings` | 0 | No warnings, no allowlist added |
| `cargo test --workspace --all-targets -- --test-threads=1` | 0 | 90 test binaries; **1,215 passed, 0 failed**. `durable_health_effects` 18 passed; `database_compatibility` 12 passed; `health` 15 passed; `backoff_policy_adoption` 12 passed; `build_manifest` 4 passed |
| `cargo test --workspace --all-targets --no-default-features -- --test-threads=1` | 0 | **1,216 passed, 0 failed** (additional evidence; CI runs no-default for `check`/`clippy` only. The one-test difference from the default run is `rust/tests/provider_transport.rs`, which is feature-gated) |
| `uv run pytest tests/tooling/ -q --tb=short` | 0 | 251 passed, 1 skipped in 148.12 s |
| `uv run ruff format --check scripts/ tests/tooling/` | 0 | 71 files already formatted |
| `uv run ruff check scripts/ tests/tooling/` | 0 | All checks passed! |
| `uv run pyright scripts/` | 0 | 0 errors, 0 warnings, 0 informations |
| `uv run python scripts/validate_release_docs.py` | 0 | `{"status": "pass", "docs_checked": 7, "version": "0.8.2"}` |

### Regression-guard evidence

Each corrected behaviour was shown to fail against a deliberate perturbation of
the production code. The repository forbids `git checkout`, `git restore`,
`git stash`, `git reset`, and `git clean`, so every perturbation was applied by
hand and undone by hand; each restored file was verified byte-identical against
a `/tmp` copy with `diff` before the suite was re-run green.

| Perturbation | Observed failure | Restored run |
|---|---|---|
| The hydration fails the batch again on a legacy row: `HealthManagerError::MissingModel` re-added and the skip replaced with `Err(...)` in `hydrate_backoffs` | `a_legacy_model_unavailable_row_is_skipped_and_reported` FAILED — `a legacy row must not fail the hydration: Backoff(Invalid("model-scoped backoff model_unavailable for account 1 has no model"))`; `a_legacy_row_does_not_prevent_a_generation_from_being_published` FAILED — `durable health hydrates: Backoff(Invalid("model-scoped backoff model_unavailable for account 1 has no model"))` — 16 passed, 2 failed | `rust/src/health/health_manager.rs` restored byte-identical (`diff` clean); `durable_health_effects` 18 passed, 0 failed |
| The partial unique index is dropped from the live schema in the uniqueness test's fixture (`DROP INDEX uq_account_backoffs_null_model`) | `the_durable_schema_rejects_a_second_account_scoped_row` FAILED — `a second account-scoped row for the same account and reason must be rejected` — 17 passed, 1 failed | `rust/tests/durable_health_effects.rs` restored byte-identical (`diff` clean); `durable_health_effects` 18 passed, 0 failed |
| The durable authentication-failure write is removed: `persist_account_backoff` returns early for the `AuthenticationFailed` reason | `a_credential_rejection_writes_the_durable_authentication_failed_row` FAILED — `the credential rejection must be persisted: {backoffs:?}` — 17 passed, 1 failed | `rust/src/routing/router.rs` restored byte-identical (`diff` clean); `durable_health_effects` 18 passed, 0 failed |

The index perturbation drops the index from the live database rather than
editing the committed migration SQL: the migration is immutable and outside
this milestone's file ownership, and the drop reproduces exactly the
pre-migration schema the table-level NULL-distinct constraint left behind.

## 5. Invariant review

- *The durable backoff scope convention is unchanged.* A NULL `model_id` still
  means account-wide suppression; a non-NULL value still scopes the row to one
  (account, model) pair. The lookup key is still the NULL-safe `model_id IS
  ?2`.
- *A `model_unavailable` durable row always carries a model identity.* The
  writer still refuses to create one without it
  (`validate_backoff_record` is unchanged).
- *The backoff policy table is unchanged.* `git diff -- rust/src/health/backoff.rs`
  is empty; `rust/tests/backoff_policy_adoption.rs` passes unchanged.
- *The escalation rungs are unchanged.* A model-scoped row carries the model's
  own rung and an account-scoped row carries the account's.
- *The hydration scope rule is unchanged.* A row is still restored by its
  scope, not by its reason value.
- *The `clear_success` scope rule is unchanged.*
- *The quarantine state machine is unchanged.* This milestone does not touch
  `rust/src/health/quarantine.rs`; the durable-effects target's quarantine
  tests are untouched and pass.
- *The routing, selection, account-scoring, and admission behaviour of a
  healthy system is unchanged.* The only production change on the routing path
  is the durable write for the `AuthenticationFailed` reason, which happens
  after the in-memory effect and outside the selection lock. The
  source-level guard `the_selection_path_performs_no_durable_health_write`
  still passes.
- *The failure classification is unchanged.* No reason is added, removed, or
  reclassified.
- *The single SQLite gate, the streaming mpsc bridge, the Tokio
  `current_thread` runtime, and the routing selection lock are unchanged.* No
  lock, no gate, and no shared state was added; the skipped count travels as
  a return value.
- *No credential, prompt, raw body, or cache key enters any affected path.*
  The new durable row carries an account id, a reason, a rung, and timestamps
  only. The new warning carries a count and the reason vocabulary only.

## 6. Failure and recovery review

- *Duplicate delivery.* The durable authentication-failure write is idempotent
  on its `(account_id, NULL, 'authentication_failed')` key, so a repeated
  credential rejection converges on the same row.
- *Restart.* A database that contains a legacy `model_unavailable` row with a
  NULL `model_id` starts: the row is skipped, the skip is reported, and no
  scope is granted for it. After the v64 migration has run, the row is gone
  and there is nothing to skip.
- *Restart.* An account whose credential was rejected stays disabled after a
  restart; the durable row restores the disable with no deadline.
- *Partial persistence failure.* A failed durable authentication-failure write
  is warned about and the live in-memory disable stands, exactly as for every
  other durable backoff write. The durable row is an improvement in
  durability, not a precondition for the live control.
- *Stale rows.* A row naming an unknown account is still skipped with a
  warning rather than failing the whole batch.
- *Malformed rows.* A legacy row is skipped and counted. Every other malformed
  row — an unknown reason, an empty model id, a missing expiry on a
  non-terminal reason — is still rejected and still fails the batch.
- *The report must never become an error.* The skipped count is a value on the
  hydration report and a `tracing::warn!`; a count that cannot be reported
  cannot fail the hydration.
- *Cancellation.* A cancelled request that had already produced a credential
  rejection may or may not have written the durable row, exactly as for any
  other durable backoff write. The write is idempotent on its key.
- *Contention.* The upsert runs inside the existing transaction under the
  repository's single writer gate. The partial unique index adds a second
  enforcement of the same invariant, so the two cannot disagree.
- *Credential rotation.* The credential-change path clears the durable
  authentication row for the accounts whose credentials change and restores it
  only if the reload rolls back. The rotation is therefore not blocked by the
  failure it is repairing.

## 7. Migration and compatibility review

One migration was added, numbered v64, and it was committed before this
milestone; its SQL and its checksum entry were not edited here.

- The chain is contiguous from v1: `applied_versions.first() == Some(&1)` and
  `applied_versions.last() == Some(&64)` are asserted by
  `rust/tests/database_compatibility.rs::the_v64_migration_removes_the_legacy_rows_and_constrains_the_null_scope`.
- The chain is checksummed: `rust/assets/db/migrations/checksums.json` holds
  64 entries, the build script's inventory guard passes, and
  `MigrationRunner::validate_embedded_checksums` verifies every embedded
  digest before any migration is applied.
- The stated chain matches the migration directory: `architecture/overview.md`
  states `v1–v64`, `AGENTS.md` states `v1–v64 immutable`, and
  `architecture/deep-dive-database.md` names `0064_*.sql`, versions 1–64, and
  64 checksum entries. The architecture migration-census guard
  (`tests/tooling/test_architecture_census.py`) compares those statements with
  the migration directory and passes.
- The migration's two operations are the duplicate-row cleanup and the partial
  unique index, in that order, mirroring migration 0054. The cleanup is scoped
  to `(account_id, reason)` where `model_id IS NULL`, so it cannot affect a
  model-scoped row; the new test asserts that the model-scoped row survives.
- Migrations v1–v63 are immutable and were not edited, renumbered, or
  rewritten.
- No published API, endpoint, response shape, configuration key, reason string,
  or durable row format changes. The reason vocabulary is unchanged and the
  `authentication_failed` row keeps the shape the validator already accepted.
- The migration is destructive on one scope only: it deletes the duplicate
  account-scoped rows and the legacy `model_unavailable` rows that carry no
  model identity. It is not reversible by any EggPool mechanism, and it is
  scoped narrowly enough that the deleted rows are exactly the ones the
  hydration already refused to interpret.
- `architecture/deep-dive-on-prem-user-access.md` is unchanged and remains
  correct: migration 64 creates no on-prem table, so the subsystem's tables are
  still migrations 55–63.
- The two Rust-level type changes this milestone required are
  `HealthHydrationReport::skipped_legacy_backoffs` (a new field on the
  hydration report the plan names) and the `(applied, skipped_legacy)` return
  of `HealthManager::hydrate_backoffs` and
  `AccountBackoffRepository::hydrate_into`. No other public shape changed, and
  the `FiniteRequest` and `CompactAdmittedRequest` compatibility surfaces were
  not touched.

## 8. Security review

- *Auth enforcement* — unchanged. The credential rejection's in-memory disable
  is unchanged; the durable row is an addition that makes it survive a
  restart.
- *Secret handling* — unchanged. The durable authentication row carries the
  account id, the reason, the rung, and timestamps. The new hydration warning
  carries a count and the reason vocabulary (`model_unavailable`) only. No
  credential, prompt, raw body, or cache key enters any log, diagnostic, or
  test fixture; the new fixtures use synthetic account names, a synthetic
  `error_class`, and epoch timestamps.
- *Privilege bounds* — unchanged. The rehash path that clears a credential
  failure is the same path, with the same account scope.
- *DoS bounds* — unchanged. The hydration still truncates at 5,000 rows, the
  skip is a bounded counter, and the backoff policy caps are unchanged.
- *Audit behavior* — the skip is reported through the tracing subscriber, so
  an operator can level-control it, and it is bounded to a count and a reason.

## 9. Documentation and operations

- `architecture/deep-dive-health.md` — the health-manager section describes the
  skip-and-count behaviour and the report field; the restart-safety section
  describes the NULL-scope uniqueness, the migration that adds it, the
  unchanged NULL-safe lookup key, and the deadline-less authentication row;
  the invariants list gains the two new statements.
- `architecture/deep-dive-routing.md` — the failure-effect section describes
  the durable account-scoped mirror, including the deadline-less
  authentication row, and the invariants list gains the uniqueness and
  credential-rejection statements.
- `architecture/overview.md`, `AGENTS.md`, and
  `architecture/deep-dive-database.md` already stated `v1–v64` when this
  milestone began and were not edited here.
- `plans/subsystems/routing-selection-roadmap.md` — the M008 status row is
  `closed` and points at this record; the M009 row moves from `blocked` to
  `ready`, because its only recorded blocker (M008 closed) is discharged.
- Operator guidance: no operator action is required. A database restored from
  a backup that predates migration v64 starts, and the operator sees a
  `skipped_legacy_backoffs` count and a warning naming the count and the
  reason. The v64 migration deletes the rows it repairs when it runs.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| medium | The credential-rotation release is asserted at the durable-row level (`AccountBackoffRepository::clear_authentication`, the same `DELETE ... reason = 'authentication_failed'` the credential-change path executes) and not through a full `ReloadService` reload | The reload path's own `clear_auth_rows`/`restore_auth_rows` calls are not exercised by this milestone's test | None for this milestone. `rust/src/reload.rs` is unchanged by this milestone, and the reload path was verified unchanged by the full workspace suite |
| medium | The account-scoped durable writer still attempts a `model_unavailable` row with no model id when a model-scoped reason reaches the account penalty path; the validator rejects it and the write is warned about | The account-scoped cooldown of a model-unavailable failure is not durable; the model-scoped row still restores the model disable, so the effect survives the restart | This is the M007 closure record's inherited observation, unchanged by this milestone. Narrowing that writer belongs to a change that owns the durable writers |
| low | Three migration-inventory assertions outside this milestone's declared file ownership (`rust/src/db/migrations.rs`, `rust/tests/build_manifest.rs`, `rust/assets/runtime-manifest.json`) were stale at the committed v64 migration and were corrected here | The suite could not pass without the correction; the corrections are the count, the highest version, and the recorded digest of `checksums.json` | None. The milestone owner should stage these three paths together with the milestone's own paths |
| low | `HealthManagerError::UnknownAccount` has no constructor anywhere in the crate | A public error variant that is never produced | None. It predates this milestone and was deliberately left alone, like `EndpointError::MissingModel` |
| info | The `authentication_failed` durable row has no expiry and is therefore not retired by `expire_old` | A credential rejection stays durable until the credential changes or `clear_authentication` runs | None. That is the documented shape of the row: a credential rejection is not a timed cooldown |

## 11. Roadmap disposition

The milestone is closed, and the next dependency may proceed. Milestone 008's
status row in `plans/subsystems/routing-selection-roadmap.md` reads `closed`
and names this record. Milestone 009's only recorded blocker — Milestone 008
closed — is discharged, so its row moves from `blocked` to `ready`; it remains
the successor that retires the parallel `HealthEffectApplier` and the
unreachable terminal-withdrawal producer, and it must not run concurrently with
this milestone's edits because both touch `rust/src/health/` and
`rust/src/routing/router.rs`.

## 12. Registry updates

Changes required in `plans/registry.md`, to be applied by the milestone owner
in the same change set. This milestone did not edit the registry.

- The `| Routing selection | M008 | ready | ... |` row moves out of the
  registry's **Dependency-ready implementation plans** table. The Deployment
  and Packaging M008 row was closed earlier in the same change set, so the
  table becomes empty when this row leaves it.
- The **Recently closed** table gains a Routing Selection M008 row: the
  disposition records that the hydration skips and reports a legacy
  `model_unavailable` row with no model identity instead of failing the batch,
  that the durable account-scoped scope is unique under the v64 partial unique
  index, that the live failure path writes the deadline-less
  `authentication_failed` row so a credential rejection survives a restart, and
  that the backoff policy table is unchanged. The evidence column names
  `plans/closure/routing-selection/008-status.md`.
- The routing-selection row in the registry's **active subsystem roadmaps**
  table is updated: its latest-milestone column moves from "M009 blocked — health
  effect ownership and terminal-withdrawal producer retirement" to "M009 ready",
  and its note records that M008 is closed through
  `plans/closure/routing-selection/008-status.md`.
- Milestone 009 becomes dependency-ready once M008 is closed: its row moves
  out of the registry's **Blocked work** table into the **Dependency-ready
  implementation plans** table, keeping the note that ADR-0006 was accepted on
  2026-10-25.
- The registry's routing-selection narrative paragraph, which currently states
  that M008 is ready and that M009 is blocked only on M008 closed, is updated
  to record the M008 closure.
