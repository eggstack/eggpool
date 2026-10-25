# Routing Selection Milestone 008 — Durable health state repair and authentication-failure durability

Status: ready

Repository baseline: `1929abf5a725cc7faac0f866622afa7af017694a`

Source roadmap:

- `plans/subsystems/routing-selection-roadmap.md#7-milestones`

Long-term requirements:

- `plans/000-long-term-specification.md` — §2 end-state invariants, §3 ownership boundaries
- `plans/001-terminology-and-domain-model.md` — normative language for accounts, backoff scope, and quarantine state
- `plans/002-long-term-roadmap.md` — sustaining work must preserve a working proxy and current compatibility

Applicable ADRs:

- `plans/adrs/ADR-0005-durable-health-state-repair-and-authentication-failure-durability.md`
  (**accepted** 2026-10-25 — this milestone implements that decision; see §2)

Primary class: invariant

## 1. Objective

Make the durable health state internally consistent: a row that this runtime
cannot interpret must not decide whether the process starts, the durable table's
uniqueness must match the scope convention the table's own migration documents,
and an account whose credential was rejected must stay disabled across a restart.

Concretely: a legacy `model_unavailable` row whose `model_id` is NULL is
reported and skipped instead of failing the whole hydration batch; the durable
backoff table gains the same NULL-scope uniqueness that migration 0054 gave the
quarantine table; and the live failure path writes the deadline-less
`authentication_failed` row that the schema, the validator, the hydrator, and
the credential-change rehash path already exist for.

## 2. Why this milestone is ready

**Ready.** This milestone changes what durable state means across a restart,
which is the repository's ADR threshold: it touches auth semantics (whether a
credential rejection survives a restart) and it changes a durable storage
contract (a new migration and a new index). The decision — what a malformed
legacy row means, whether the authentication failure is durable, and what the
durable scope key guarantees — is recorded in
`plans/adrs/ADR-0005-durable-health-state-repair-and-authentication-failure-durability.md`,
which the project maintainers accepted on 2026-10-25.

The acceptance revision is the commit that changes that record from `proposed`
to `accepted`. Two passages in the proposed draft were corrected before
acceptance because they contradicted the record's own Decision: the draft did
not say how the skipped-row report reaches the operator, and it described the
migration's cleanup scope as two rules where the migration applies one. Both
corrections are recorded in the decision record's acceptance note. The Decision
is unchanged in substance: Option A is selected, the migration is v64, the
hydration skips and reports rather than failing the batch, the NULL-scope
uniqueness follows migration 0054's pattern, and the live failure path writes
the deadline-less `authentication_failed` row.

The milestone has no hard dependency on another milestone. Every defect it
addresses is reproducible from the repository at the stated baseline. The
milestone is dependency-ready in the registry.

The related milestone for the parallel effect applier and the unreachable
terminal-withdrawal producer is a successor milestone in this subsystem. That
milestone is separate and follows this one; it is not a dependency of this
milestone, and the two do not run concurrently because both touch
`rust/src/health/` and `rust/src/routing/router.rs`.

## 3. Current implementation evidence

**The durable writer already refuses the malformed shape.**
`validate_backoff_record` (`rust/src/health/repository.rs:554`) rejects any
record whose reason is `model_unavailable` and whose `model_id` is `None`. The
guard has existed since the file's first commit (`d5dd16da`, D005). No new
malformed row can be written by the Rust runtime.

**The upsert key is correct.** `rust/src/health/repository.rs:240` uses
`model_id IS ?2`, which is the NULL-safe form. It is inherited faithfully from
the Python-era repository (`git show 39aec1c9^:src/eggpool/db/repositories.py`),
whose `upsert_failure` docstring documents the same NULL-distinct hazard. The
key form is not a defect and is not changed by this milestone.

**The schema does not constrain the NULL scope.** `rust/assets/db/migrations/0024_account_backoffs.sql:24`
declares `UNIQUE(account_id, model_id, reason)`. SQLite treats NULLs as
distinct in a unique index, so the constraint does not deduplicate rows whose
`model_id` is NULL. This was confirmed directly against SQLite 3.51.2: two rows
with the same `(account_id, NULL, reason)` insert successfully, the
`model_id IS NULL` lookup returns both, and `INSERT OR REPLACE` with a NULL
`model_id` adds a row rather than replacing one. The repository has already met
this hazard once — `rust/assets/db/migrations/0054_model_quarantine_null_identity.sql`
adds a partial unique index over the NULL scope for `model_quarantine` after
deleting the duplicate rows the table-level constraint admitted.
`account_backoffs` never received the equivalent treatment.

**The reader treats such a row as fatal, and the fatality reaches startup.**
`hydrate_backoffs` (`rust/src/health/health_manager.rs:575-584`) returns
`Err(HealthManagerError::MissingModel)` for a `model_unavailable` row whose
`model_id` is `None`. It does not skip the row and continue. The error has no
recovery path:

- `rust/src/health/repository.rs:192-202` → `Err`
- `rust/src/health/repository.rs:60-70` → `Err`
- `rust/src/coordinator/endpoints.rs:1419-1428` → `Err("durable health
  hydration failed: …")`
- `rust/src/runtime_lifecycle/generation.rs:687-695` →
  `Err(GenerationBuildError::Graph { … })`
- `rust/src/server/mod.rs:864-877` → `return Err(map_generation_error(error))`

The server closes the database and returns an error. **EggPool does not start.**
The same failure recurs on every reload attempt, so it is not self-clearing.

**The rows are reachable from the system's own history.** The Python-era writer
permitted `model_id=None` with any reason, and the Python-era reader met such
rows in practice: commit `3583301b` ("Bound router backoff and self-healing")
**added** the explicit skip
`logger.warning("Ignoring account-wide model_unavailable backoff")`, replacing
code in which an account-wide `model_unavailable` row fell through to the
generic account-wide cooldown path.

**The documented remediation was never implemented.**
`rust/assets/db/migrations/0051_model_quarantine.sql:5-7` states that "Legacy
`model_unavailable` rows in `account_backoffs` are preserved but hydrated as
`migration_legacy` provenance on startup."
`EvidenceProvenance::MigrationLegacy`
(`rust/src/health/quarantine.rs:95`) is **never constructed** anywhere in the
repository. The promise is not implementable as written: a legacy account-wide
row carries no model identity, and `model_quarantine.canonical_model_id` is
`NOT NULL`, so there is no model to attribute it to.

**The authentication-failure durable row has a reader and no writer.**
`validate_backoff_record` (`rust/src/health/repository.rs:555-556`) rejects an
`authentication_failed` record that carries a deadline and accepts the
deadline-less shape. `hydrate_backoffs` (`rust/src/health/health_manager.rs:562-566`)
honours that shape with `disable_account(account_name, reason, None)`, which
sets `disabled_until = None` — an account disabled until explicitly cleared
(`rust/src/health/health_manager.rs:428-446`). `rust/src/reload.rs:1107-1141`
clears those rows for the accounts whose credentials change and restores them
afterwards. `clear_authentication` (`rust/src/health/repository.rs:297`)
deletes them.

Nothing writes one. The `AuthenticationFailed` policy has `base_delay: 0.0` and
`cap: 0.0` (`rust/src/health/backoff.rs:189-192`), so `compute_backoff_seconds`
returns `None`, and `persist_account_backoff`
(`rust/src/routing/router.rs:826-860`) returns early when there is no deadline.
The live path disables the account in memory
(`rust/src/health/health_manager.rs:362-367` sets `is_healthy = false`); the
durable suppression is lost on restart. **An account whose credential was
rejected is eligible again after a restart**, while four parts of the durable
contract exist for exactly that row.

**The migration inventory is strict.** `rust/build.rs:46-59` panics if a
migration file is absent from `rust/assets/db/migrations/checksums.json` or if
the two inventories differ in length, and
`MigrationRunner::validate_embedded_checksums`
(`rust/src/db/migrations.rs:41-57`) verifies the embedded digest at runtime.

## 4. Invariants that must not regress

- The durable backoff scope convention is unchanged: a NULL `model_id` means
  account-wide suppression, and a non-NULL value scopes the backoff to one
  (account, model) pair.
- A `model_unavailable` durable row always carries a model identity. The writer
  continues to refuse to create one without it.
- The backoff policy table is unchanged. No reason's base delay, cap, or
  escalation changes.
- The escalation rungs are unchanged. A model-scoped row carries the model's
  own rung and an account-scoped row carries the account's, as M007 established.
- The hydration scope rule is unchanged: a row is restored by its scope, not by
  its reason value.
- The `clear_success` scope rule is unchanged: a success for one model clears
  the account-wide row and that model's row, and a success with no model
  identity clears only the account-wide row.
- The quarantine state machine is unchanged. This milestone does not touch
  `model_quarantine`.
- The routing, selection, account-scoring, and admission behaviour of a healthy
  system is unchanged.
- The failure classification is unchanged. No reason is added, removed, or
  reclassified, and no category's effect changes.
- The single SQLite gate, the streaming mpsc bridge, the Tokio
  `current_thread` runtime, and the routing selection lock are unchanged.
- No credential, prompt, raw body, or cache key enters any affected path.

## 5. Scope

### In scope

- The hydration of durable backoff rows: a `model_unavailable` row whose
  `model_id` is NULL is skipped and reported, not fatal.
- The durable backoff table's NULL-scope uniqueness: a new migration adds the
  partial unique index migration 0054 established for the quarantine table, and
  the duplicate-row cleanup that must precede it.
- The durable `authentication_failed` row: the live failure path writes it in
  the deadline-less shape the validator accepts and the hydrator honours.
- The migration registration: the new SQL file, its checksum, and the
  assertions and documents that state the current migration chain.
- The architecture deep dive that describes the durable health state.

### Explicitly out of scope

- The parallel `HealthEffectApplier` in `rust/src/health/effects.rs` and the
  unreachable terminal-withdrawal producer. Both belong to a successor milestone
  in this subsystem, under its own architecture decision.
- The `model_quarantine` table, its states, its provenances, and its
  hydration. Nothing in this milestone changes them.
- The account catalogue refresh and `catalog_withdrawal_policy`.
- The quarantine state machine's corroboration rule, its window, and its
  auto-clear on recovery.
- The wire codecs, the provider transport, the request admission path, and the
  coordinator's retry behaviour.
- Any change to the account-scoped or model-scoped durable writers' scope
  decisions beyond the addition of the authentication-failure row.
- Any change to the authentication throttle, the browser session, the CSRF
  delivery, or the on-prem sign-in surface. Those belong to the closed
  On-prem user access milestone M024.

## 6. Required production changes

### 6.1 The hydration reports a skipped legacy row instead of failing

`rust/src/health/health_manager.rs` — `hydrate_backoffs` currently returns
`Err(HealthManagerError::MissingModel)` for a `model_unavailable` row whose
`model_id` is `None`. It must instead skip that row, count it, and continue
with the remaining rows.

The skip must be reported. The hydration report type
(`rust/src/health/repository.rs`) gains a counter for skipped legacy rows, and
the caller surfaces it through the existing diagnostics surface so an operator
whose database was restored from a backup can see that rows were dropped. The
report must not name a credential, a prompt, a raw body, or a cache key; a
count and a reason vocabulary are sufficient.

The skip must not grant scope. A skipped row grants neither an account-wide
nor a model-wide suppression, because the row has no model identity to scope
it by and no coherent account-wide meaning.

The `HealthManagerError::MissingModel` variant is not removed if another arm
produces it; if it becomes unreachable, it is removed together with its
`Display` arm and its `EndpointError::MissingModel` mapping is left alone,
because that mapping belongs to a different subsystem.

### 6.2 The durable scope key gains NULL-scope uniqueness

A new migration numbered v64 is added under
`rust/assets/db/migrations/`. It performs two operations in this order:

1. Delete the duplicate rows that the NULL-distinct table-level unique
   constraint admitted, scoped to `(account_id, reason)` where `model_id IS
   NULL`. This mirrors the cleanup
   `rust/assets/db/migrations/0054_model_quarantine_null_identity.sql` performs
   before adding its partial unique index.
2. Create a partial unique index over `(account_id, reason)` restricted to
   `model_id IS NULL`.

The table-level `UNIQUE(account_id, model_id, reason)` is left in place, because
it remains correct for the non-NULL scopes.

The migration's comment records that the legacy `model_unavailable` rows with a
NULL `model_id` carry no recoverable scope, that the predecessor runtime ignored
them, and that the `migration_legacy` provenance promised by migration 0051 is
not produced because the row has no model identity to attribute. Migration 0051
is immutable and is not edited.

### 6.3 The live failure path writes the authentication-failure durable row

`rust/src/routing/router.rs` — `persist_account_backoff` currently returns
early when the policy yields no deadline. It must instead write the
deadline-less `authentication_failed` row that `validate_backoff_record`
accepts and that `hydrate_backoffs` honours.

The write is scoped to the `AuthenticationFailed` reason. It does not change
the behaviour of any other reason, and it does not introduce a deadline the
policy does not define. The row is written on the same durable key the
validator and the hydrator already use: `(account_id, NULL,
'authentication_failed')`.

The account's in-memory disable is unchanged and remains the live behaviour.
The durable row is an addition that makes the suppression survive a restart;
it does not replace the in-memory state and it does not change the timing of
the in-memory disable.

The credential-change rehash path
(`rust/src/reload.rs:1107-1141`) is unchanged: it already clears the rows for
the accounts whose credentials change and restores them afterwards, so a
legitimate credential rotation is not blocked by the failure it is repairing.

### 6.4 The migration inventory and the stated chain are updated

- A new migration numbered v64 is added under `rust/assets/db/migrations/`.
- Its SHA-256 is added to `rust/assets/db/migrations/checksums.json`. The build
  script panics if the file is absent from the inventory or the two inventories
  differ in length.
- `rust/tests/database_compatibility.rs` asserts the applied migration count in
  four places (`:43`, `:44`, `:273`, `:649`) and the highest applied version in
  two of them. All become v64.
- `architecture/overview.md:350` states the chain as `v1–v63` and becomes
  `v1–v64`, because the architecture census guard
  (`tests/tooling/test_architecture_census.py:242-258`) fails a live-behaviour
  document that names an older schema as the present one.

## 7. Ordered work packages

### Work package A — Legacy backoff row tolerance in hydration

Files: `rust/src/health/health_manager.rs`, `rust/src/health/repository.rs`,
`rust/tests/durable_health_effects.rs`.

- Change `hydrate_backoffs` so that a `model_unavailable` row whose `model_id`
  is `None` is skipped and counted rather than returned as
  `Err(HealthManagerError::MissingModel)`.
- Add the skipped-row count to the hydration report and surface it through the
  existing diagnostics path.
- Assert in a test that the hydration of such a row succeeds, that the row is
  reported as skipped, and that neither an account-wide nor a model-wide
  suppression is granted on the strength of it.
- Assert in a test that a generation is published when such a row is present,
  so the startup path is exercised rather than assumed.

Acceptance:

- A durable-effects test asserts that a hydration over a table containing the
  malformed row succeeds and reports the skip.
- A durable-effects test asserts that no scope is granted for the skipped row.
- The malformed row does not prevent a generation from being published.

### Work package B — Durable scope uniqueness for the NULL key

Files: `rust/assets/db/migrations/`, `rust/assets/db/migrations/checksums.json`,
`rust/tests/database_compatibility.rs`.

- Write the migration: duplicate-row cleanup first, then the partial unique
  index, mirroring migration 0054's structure.
- Register the migration's checksum.
- Update the four migration-count assertions and the two highest-version
  assertions in `rust/tests/database_compatibility.rs`.
- Assert in a test that inserting a second account-scoped row for the same
  `(account_id, reason)` is rejected by the durable schema.

Acceptance:

- The migration chain is contiguous from v1 and ends at v64.
- The checksum inventory guard passes.
- A duplicate account-scoped insert is rejected.

### Work package C — The authentication-failure durable row

Files: `rust/src/routing/router.rs`, `rust/src/health/backoff.rs`,
`rust/tests/durable_health_effects.rs`, `rust/tests/health.rs`.

- Write the deadline-less `authentication_failed` row from the live failure
  path, on the `(account_id, NULL, 'authentication_failed')` key.
- Leave the in-memory disable and the policy table unchanged.
- Assert in a test that a credential rejection writes the durable row.
- Assert in a test that the durable row disables the account on hydration with
  no deadline, and that the credential-change path releases it.
- Assert in a test that the live path still writes no `model_unavailable` row
  with a NULL `model_id`.

Acceptance:

- A durable-effects test asserts that a credential rejection produces the
  durable row and that hydration restores the account disable.
- The backoff policy table is unchanged, and its existing assertions still pass.

### Work package D — The stated chain and the deep dive

Files: `architecture/overview.md`, `architecture/deep-dive-health.md`,
`architecture/deep-dive-routing.md`.

- Update the stated migration chain to `v1–v64`.
- Correct the deep dives that describe the durable health state so that the
  skipped-row behaviour, the NULL-scope uniqueness, and the durable
  authentication-failure suppression are described accurately.
- Leave every other deep dive unchanged.

Acceptance:

- The architecture migration census guard passes.
- The architecture census and link guards pass.

## 8. Failure, cancellation, restart, contention semantics

**Restart.** A database that contains a legacy `model_unavailable` row with a
NULL `model_id` starts. The row is skipped, the skip is reported, and no scope
is granted for it. After the v64 migration has run, the row is gone and there
is nothing to skip.

**Restart.** An account whose credential was rejected stays disabled after a
restart. The durable `authentication_failed` row restores the account disable
through `disable_account(account_name, reason, None)`, which has no deadline
and is therefore cleared only by the credential-change rehash path or by
`clear_authentication`.

**Failure.** A write of the durable authentication-failure row that fails is
handled the same way as any other durable backoff write failure: the error is
logged and the live in-memory disable stands. The durable row is an
improvement in durability, not a precondition for the live control.

**Failure.** The skipped-row report must not fail the hydration. A count that
cannot be reported must not become an error.

**Contention.** The upsert runs inside the existing transaction, which takes
the write lock up front (`BEGIN IMMEDIATE`) under the repository's single
writer gate. The partial unique index adds a second enforcement of the same
invariant, so the two cannot disagree: the index is authoritative and the
transaction is merely the mechanism that keeps the lookup and the write
together.

**Cancellation.** A cancelled request that had already produced a credential
rejection may or may not have written the durable row, exactly as for any other
durable backoff write. The write is idempotent on its key, so a retry
converges on the same row.

**Concurrency.** No new lock, no new gate, no new shared state. The new index
is a durable constraint; the new write is one statement on the existing
durable path.

## 9. Compatibility and migration

One new migration is added, numbered v64. Migrations v1–v63 are immutable and
are not edited, renumbered, or rewritten.

The migration's two operations are the duplicate-row cleanup and the partial
unique index, in that order, mirroring migration 0054. The cleanup is scoped
to `(account_id, reason)` where `model_id IS NULL`, so it cannot affect a
model-scoped row.

The registration requires the canonical migration inventory to stay consistent:
the SQL file is added to `rust/assets/db/migrations/`, and its SHA-256 is added
to `rust/assets/db/migrations/checksums.json`. The build script panics if a
migration file is absent from the checksum inventory or if the two inventories
differ in length, and the runner verifies the embedded digest before applying
any migration.

`rust/tests/database_compatibility.rs` moves from 63 to 64 in the four
migration-count assertions and the two highest-version assertions.
`architecture/overview.md:350` moves from `v1–v63` to `v1–v64`, because the
architecture census guard fails a live-behaviour document that names an older
schema as the present one.

No published API, endpoint, response shape, configuration key, reason string,
or durable row format changes. No credential, prompt, raw body, or cache key
enters any affected path.

The migration is destructive on one scope only: it deletes duplicate
account-scoped rows and the legacy `model_unavailable` rows that carry no model
identity. It is not reversible by any EggPool mechanism, and it is scoped
narrowly enough that the deleted rows are exactly the ones the hydration
already refused to interpret.

## 10. Required tests

- A durable-effects test asserts that a hydration over a table containing a
  `model_unavailable` row with a NULL `model_id` succeeds, that the row is
  reported as skipped, and that no account or model scope is granted.
- A durable-effects test asserts that the presence of such a row does not
  prevent a generation from being published.
- A durable-effects test asserts that a credential rejection writes the
  deadline-less `authentication_failed` row, that hydration restores the
  account disable from it, and that the credential-change path releases it.
- A durable-effects test asserts that the live path writes no
  `model_unavailable` row with a NULL `model_id`.
- A durable-effects test asserts that a second account-scoped row for the same
  `(account_id, reason)` is rejected by the durable schema.
- A migration test asserts that the v64 migration removes the legacy rows and
  that the migration chain remains contiguous from v1.
- The existing backoff policy, reason vocabulary, scope, and rung assertions
  continue to pass unchanged.

## 11. Required verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
uv run pytest tests/tooling/ -q --tb=short
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
```

The no-default-features test suite is additional evidence and is run with
`--offline --locked`, because a re-resolving build reaches the network:

```bash
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets \
  --no-default-features --offline --locked -- --test-threads=1
```

## 12. Documentation updates

- `architecture/overview.md` — the stated migration chain becomes `v1–v64`.
- `architecture/deep-dive-health.md` — the durable health state description
  gains the skipped-legacy-row behaviour, the NULL-scope uniqueness, and the
  durable authentication-failure suppression.
- `architecture/deep-dive-routing.md` — the durable backoff description, if it
  describes the malformed row as fatal, is corrected.
- The v64 migration — its comment records the cleanup's scope, the reason the
  legacy rows carry no recoverable scope, and the fact that the
  `migration_legacy` provenance promised by migration 0051 is not produced.

No change to `AGENTS.md`, to the release documentation, or to any published
user-facing documentation.

## 13. Acceptance criteria

- A database containing a legacy `model_unavailable` row with a NULL `model_id`
  starts, and the skip is reported.
- The durable backoff table rejects a duplicate account-scoped row for the
  same `(account_id, reason)`.
- An account whose credential was rejected is still disabled after a restart.
- A credential rotation still re-enables the account.
- The migration chain is contiguous from v1 to v64, and the checksum inventory
  guard passes.
- The backoff policy table, the reason vocabulary, the scope rules, the
  escalation rungs, the quarantine state machine, and the routing behaviour of a
  healthy system are unchanged.
- The default and no-default-features workspace suites pass serially, and the
  tooling suite, the linters, and the release-documentation validator pass.

## 14. Stop conditions

Stop and report if any of the following is observed:

- The migration chain would become non-contiguous, or an existing migration
  file would need to change.
- The duplicate-row cleanup would remove a row that carries a model identity.
- The partial unique index would reject a legitimate non-duplicate write.
- The durable `authentication_failed` row would change the account's in-memory
  disable timing, or would make a credential rotation fail to re-enable the
  account.
- The skipped-row report would require a credential, a prompt, a raw body, or a
  cache key to be useful.
- The hydration of a healthy database would change in any observable way.
- A guard would need to be weakened, skipped, or given a baseline allowlist to
  pass.
- The ADR-0005 acceptance would have to be reinterpreted for the implementation
  to proceed.

## 15. Closure evidence required

The closure record must contain:

- The requirement-to-evidence matrix mapping every §13 acceptance criterion to
  the test or command that proves it, with the test name and the observed
  result.
- The verification table for every command in §11, with its exit status and
  the pass counts.
- A mutation-check table: at least one deliberate perturbation per corrected
  behaviour — restoring the fatal hydration, dropping the partial unique index,
  removing the durable authentication-failure write — each shown to fail the
  named test.
- The evidence that the migration is contiguous and checksummed, and that the
  stated chain in `architecture/overview.md` matches the migration directory.
- The invariant, security, compatibility, and migration reviews, each stating
  what was checked and what was deliberately left unchanged.
- The unresolved-findings table, which may be empty, listing anything the
  milestone did not address.

## 16. Handoff notes

- The next milestone in this subsystem retires the parallel `HealthEffectApplier`
  and the unreachable terminal-withdrawal producer. It depends on this milestone
  closing, because both touch `rust/src/health/` and `rust/src/routing/router.rs`.
- The closed records
  `plans/closure/routing-selection/005-status.md`,
  `plans/closure/routing-selection/007-status.md`, and
  `plans/closure/request-admission-wire/008-status.md` are immutable and are
  not edited by this milestone. The M007 record's deferred observation about
  the parallel applier is discharged by that successor milestone, not by this
  one.
- The registry's routing-selection row and the "Dependency-ready implementation
  plans" or "Blocked work" table carry this milestone's status. One commit
  changes one status.
- A successor milestone that introduces an authoritative-absence producer must
  first make the success path's clearing durable, as
  `plans/closure/routing-selection/005-status.md:227` requires. That
  prerequisite is recorded in that milestone's decision record and is not this
  milestone's work.
