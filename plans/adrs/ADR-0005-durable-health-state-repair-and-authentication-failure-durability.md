# ADR-0005: Durable health state repair and authentication-failure durability

Status: proposed

Date: 2026-10-25

Decision owners: project maintainers

Related specification sections:

- `plans/000-long-term-specification.md` — §2 end-state invariants, §3 ownership boundaries
- `plans/001-terminology-and-domain-model.md` — normative language for accounts, backoff scope, and quarantine state
- `plans/002-long-term-roadmap.md` — sustaining work must preserve a working proxy and current compatibility

Affected subsystem roadmaps:

- `plans/subsystems/routing-selection-roadmap.md`

## Context

EggPool persists upstream-observed backoffs in `account_backoffs`
(`rust/assets/db/migrations/0024_account_backoffs.sql`). The table's key is
`UNIQUE(account_id, model_id, reason)`, and the migration documents the scope
convention explicitly:

> ``model_id`` NULL means account-wide suppression; a non-NULL value scopes the
> backoff to a specific (account, model) pair so the same account can still
> serve other models.

Three facts about that table drive this decision, and they were verified against
the repository at baseline `1929abf5`.

**SQLite treats NULLs as distinct in a unique index.** The unique constraint
therefore does not deduplicate rows whose `model_id` is NULL. This was confirmed
directly against SQLite 3.51.2: inserting two rows with the same
`(account_id, NULL, reason)` succeeds, `SELECT id ... WHERE model_id IS NULL`
returns both rows, and `INSERT OR REPLACE` with a NULL `model_id` **adds** a row
rather than replacing one. The repository has already met this hazard once, in
migration `0054_model_quarantine_null_identity.sql`, which adds a partial unique
index over the NULL scope for `model_quarantine` after deleting the duplicate
rows the old table-level constraint had admitted. `account_backoffs` never
received the equivalent treatment.

**The durable writer already refuses the malformed shape.** The upsert key lookup
at `rust/src/health/repository.rs:240` uses `model_id IS ?2`, which is the
correct NULL-safe form and is inherited faithfully from the Python-era
repository (`git show 39aec1c9^:src/eggpool/db/repositories.py`, whose
`upsert_failure` docstring documents the same NULL-distinct hazard and performs
an explicit existence check for it). Before the write, `validate_backoff_record`
(`rust/src/health/repository.rs:554`) rejects any record whose reason is
`model_unavailable` and whose `model_id` is `None`. That guard has existed since
the file's first commit (`d5dd16da`, D005). **No new malformed row can be
written by the Rust runtime.**

**The durable reader treats such a row as a fatal error.** `hydrate_backoffs`
(`rust/src/health/health_manager.rs:575-584`) returns
`Err(HealthManagerError::MissingModel)` for a row whose reason is
`model_unavailable` and whose `model_id` is `None`. It does not skip the row and
continue. That error propagates without any recovery path:

- `hydrate_into` (`rust/src/health/repository.rs:192-202`) → `Err`
- `hydrate_generation_health` (`rust/src/health/repository.rs:60-70`) → `Err`
- `rust/src/coordinator/endpoints.rs:1419-1428` → `Err("durable health hydration
  failed: …")`
- `rust/src/runtime_lifecycle/generation.rs:687-695` →
  `Err(GenerationBuildError::Graph { … })`
- `rust/src/server/mod.rs:864-877` → `return Err(map_generation_error(error))`

The server closes the database and returns an error. **EggPool does not start.**
The same failure recurs on every subsequent reload attempt, so the condition is
not self-clearing.

The rows that trigger this are reachable from the system's own history. The
Python-era writer permitted `model_id=None` with any reason, and the Python-era
reader encountered such rows in practice: commit `3583301b` ("Bound router
backoff and self-healing") **added** an explicit
`logger.warning("Ignoring account-wide model_unavailable backoff")` skip,
replacing code in which an account-wide `model_unavailable` row fell through to
the generic account-wide cooldown path. The maintainers added that guard because
such rows existed.

Three repository facts show the current handling is an unfinished migration
rather than a settled design:

- No migration in `rust/assets/db/migrations/` reads or normalises
  `account_backoffs` rows. Migration `0051_model_quarantine.sql:5-7` states
  that "Legacy `model_unavailable` rows in `account_backoffs` are preserved but
  hydrated as `migration_legacy` provenance on startup."
- `EvidenceProvenance::MigrationLegacy` (`rust/src/health/quarantine.rs:95`) is
  **never constructed anywhere in the codebase.** It is defined and
  (de)serialized, and nothing produces it.
- The promise is not implementable as written. A legacy account-wide
  `model_unavailable` row carries no model identity, and
  `model_quarantine.canonical_model_id` is `NOT NULL`. There is no model to
  attribute such a row to, so it cannot become a quarantine entry.

A separate defect sits in the same durable contract. The `authentication_failed`
row shape is **fully designed and has no writer**:

- `validate_backoff_record` (`rust/src/health/repository.rs:555-556`) explicitly
  *rejects* an `authentication_failed` record that carries a deadline and
  accepts the deadline-less shape.
- `hydrate_backoffs` (`rust/src/health/health_manager.rs:562-566`) honours that
  shape with `disable_account(account_name, reason, None)`, which sets
  `disabled_until = None` — an account disabled until explicitly cleared
  (`rust/src/health/health_manager.rs:428-446`).
- `reload.rs:1107-1141` clears those rows for the accounts whose credentials
  change and restores them afterwards, so a rotated credential re-enables the
  account.
- `clear_authentication` (`rust/src/health/repository.rs:297`) deletes them.

But nothing writes one. The `AuthenticationFailed` backoff policy has
`base_delay: 0.0` and `cap: 0.0` (`rust/src/health/backoff.rs:189-192`), so
`compute_backoff_seconds` returns `None`, and `persist_account_backoff`
(`rust/src/routing/router.rs:826-860`) returns early when there is no deadline.
The live path disables the account in memory
(`health_manager.rs:362-367` sets `is_healthy = false`), and the durable
suppression is lost on restart. **An account whose credential was rejected is
eligible again after a restart**, despite four parts of the durable contract
being written for exactly that row.

## Decision drivers

- A legacy row must not be able to prevent the runtime from starting. A
  database written by a supported predecessor release is a supported input, not
  an attack.
- Fail-closed must not be traded for fail-open. Skipping a row must not
  silently *enable* a suppression the operator believes is in force; it must
  report what it skipped.
- A shape that the validator forbids must not be permitted by the schema. The
  durable writer and the durable reader must agree on which rows exist.
- Durable state must have exactly one writer per shape. A row shape with a
  reader, a validator branch, a rehash path, and a cleanup but no writer is a
  half-built contract.
- The correction must not renumber, rewrite, or edit any shipped migration.
  Migrations v1–v63 are immutable; a repair is a new migration.
- The correction must not change the routing, selection, or admission
  behaviour of a healthy system.

## Considered options

### Option A — Delete the meaningless rows once, and skip any that reappear

A new migration deletes every `account_backoffs` row whose reason is
`model_unavailable` and whose `model_id` is NULL. The hydration path is changed
to skip such a row with a warning instead of failing the batch, so a database
restored from a backup, or written by a non-Rust tool, still starts.

Separately, the `authentication_failed` durable row is written by the live
failure path, using the deadline-less shape the validator already accepts and
the reader already honours.

This keeps the existing scope convention (`NULL` means account-wide, and
`model_unavailable` is inherently model-scoped), leaves the routing semantics
of a healthy system untouched, and closes the gap that lets a legacy row brick
startup.

The cost is that the malformed scope is removed by deletion rather than by
making it representable. The justification is that it was never representable:
the row has no model identity, so no correct scope can be recovered from it, and
the predecessor runtime already chose to ignore it.

### Option B — Keep failing closed, and rely on a migration alone

The migration deletes the rows, and the reader keeps failing closed on any
such row that appears later.

This is simpler and it keeps a strict reader. It is rejected because the
strictness is unconditional and unbounded: a restored backup, a hand-edited
database, or any future writer that is not the Rust upsert reintroduces a
condition whose only remedy is to edit SQLite by hand. A fail-closed reader is
correct when the row means something and the system cannot decide what; here it
means nothing, so refusing to start is not a safety property, it is an
availability defect with no diagnostic.

### Option C — Recover a model identity for the legacy row

Derive the model from the row's other columns, or from the account's catalogue,
and hydrate it as a model-scoped backoff.

Rejected. `account_backoffs` stores no model identity for such a row, and
inferring one from the account's catalogue would attribute a suppression to a
model the failure may never have concerned. A wrong model scope is a routing
defect; a missing row is a smaller defect than a mis-scoped one. This is also
the shape the `migration_legacy` promise in migration 0051 would require, which
is why that promise is unimplementable as written.

### Option D — Re-key `model_unavailable` rows onto a sentinel instead of NULL

Represent "no model" for a `model_unavailable` row with a sentinel value rather
than NULL, so the table-level unique constraint applies.

Rejected. It changes the key vocabulary of a shipped table and its documented
scope convention, collides with the validator's existing rejection of an empty
`model_id`, and does nothing for rows that already exist in a predecessor
database. The NULL-safe lookup already handles the key correctly; the defect
never was the key.

### Option E — Leave the authentication-failure row unwritten

Keep the authentication disable in memory only, and record that it does not
survive a restart.

Rejected, and this option is the one the current code silently implements. The
durable contract for this row exists in four places; leaving the writer absent
means the credential-suppression state is process-local while the schema, the
validator, the hydrator, and the rehash path all assume it is durable. A
credential rejection that a restart forgets is a weaker control than the
repository's own design intends.

## Decision

Option A is selected.

- A new migration deletes every `account_backoffs` row whose reason is
  `model_unavailable` and whose `model_id` is NULL. Such a row carries no model
  identity, has no correct scope, and had no coherent meaning in the
  predecessor runtime either, which ignored it.
- The hydration path skips any such row that is nonetheless present — from a
  restored backup, from a hand-edited database, or from a writer this runtime
  does not own — and reports it as a skipped legacy row. It does **not** fail
  the batch, and it does **not** silently enable a suppression: the skipped row
  is counted and reported, and the account is not given a model scope that the
  row never had.
- The new migration records, in its own comment, that the `migration_legacy`
  provenance promised by migration 0051 is not produced, and why: the legacy row
  has no model identity to attribute. Migration 0051 is not edited; it is
  immutable.
- `account_backoffs` gains the same NULL-scope uniqueness that migration 0054
  gave `model_quarantine`: a partial unique index over the
  `(account_id, reason)` scope where `model_id IS NULL`, preceded by the same
  duplicate-row cleanup migration 0054 performs. The table-level
  `UNIQUE(account_id, model_id, reason)` is left in place, because it remains
  correct for the non-NULL scopes.
- The live failure path writes the `authentication_failed` durable row in the
  deadline-less shape the validator already accepts, so the authentication
  suppression survives a restart and is released by the existing credential-change
  rehash path.
- The `model_unavailable` scope rule stays as it is: the reason requires a model
  identity, and the writer refuses to create a row without one.

The deeper contract this records is that a durable table's schema, its writer,
and its reader must agree on which rows exist, and that a row whose meaning
cannot be recovered must be removed or reported rather than allowed to decide
whether the process starts.

## Consequences

### Positive

- A database written by a predecessor release no longer prevents EggPool from
  starting.
- The authentication suppression for a rejected credential survives a restart,
  which is what the schema, the validator, the hydrator, and the rehash path
  already assume.
- The durable backoff table's uniqueness matches the scope convention its own
  migration documents, using the pattern the repository already established for
  `model_quarantine`.
- The skipped-row case is reported rather than silent, so an operator who
  restores an old backup sees what was dropped.

### Negative

- An account-wide `model_unavailable` suppression, if one existed, is dropped
  rather than honoured. That is the intended reading: the row had no model
  identity, the predecessor runtime ignored it, and the current runtime refuses
  to write one.
- The migration deletes rows. It is a destructive step on the durable table,
  scoped to exactly one reason and one NULL scope, and it is not reversible by
  any EggPool mechanism.
- Writing the authentication-failure row adds a durable write on the single
  SQLite gate the repository treats as a performance boundary. The write is
  scoped to a credential rejection, which is not a hot path, and it is
  idempotent on the same key.
- The schema changes, so a deployment needs a migration-capable database. This
  is already true of every release since v1.

### Neutral or deferred

- The backoff policy table is unchanged. The `AuthenticationFailed` policy keeps
  its zero base delay and zero cap: the durable row it now produces is a
  deadline-less disable, not a cooldown, and the policy's numbers are not its
  source.
- The account-scoped and model-scoped durable writers, the escalation rungs, the
  quarantine state machine, and the routing eligibility gate are unchanged.
- The reason vocabulary is unchanged. No reason is added, removed, or
  reclassified.
- The `model_unavailable` scope rule, the hydration scope rule, and the
  `clear_success` scope rule are unchanged.
- The parallel `HealthEffectApplier` in `rust/src/health/effects.rs` is out of
  scope here and is addressed by a separate decision.

## Compatibility and migration

One new migration is added, numbered v64. Migrations v1–v63 are immutable and
are not edited, renumbered, or rewritten.

The new migration performs two operations, in this order:

1. Delete the duplicate rows that the NULL-distinct table-level unique
   constraint admitted, scoped to `(account_id, reason)` where `model_id IS
   NULL`. This mirrors the cleanup migration `0054_model_quarantine_null_identity.sql`
   performs before adding its partial unique index, and it makes the index
   creation below safe on an existing database.
2. Create a partial unique index over `(account_id, reason)` restricted to
   `model_id IS NULL`. This is the same pattern migration 0054 established for
   `model_quarantine`.

The legacy `model_unavailable` rows with a NULL `model_id` are removed by the
migration. Their removal is a consequence of the same cleanup; the migration
comment states why they carry no recoverable scope.

Registering the migration requires the canonical migration inventory to stay
consistent: the SQL file is added to `rust/assets/db/migrations/`, and its
SHA-256 is added to `rust/assets/db/migrations/checksums.json`. The build
script panics if a migration file is absent from the checksum inventory or if
the two inventories differ in length, and `MigrationRunner::validate_embedded_checksums`
(`rust/src/db/migrations.rs:41-57`) verifies the embedded digest at runtime.

Two assertions and one document follow the migration. `rust/tests/database_compatibility.rs`
asserts the applied migration count in four places (`:43`, `:44`, `:273`,
`:649`) and the highest applied version in two of them; these become v64.
`architecture/overview.md:350` states the current chain as `v1–v63` and becomes
`v1–v64`, because the architecture census guard
(`tests/tooling/test_architecture_census.py:242-258`) fails a live-behaviour
document that names an older schema as the present one.

No published API, endpoint, response shape, configuration key, or reason string
changes. No credential, prompt, raw body, or cache key enters any affected path.

## Security and reliability implications

The change moves one reader from "refuse to start" to "skip and report". That
is a deliberate relaxation, and it is safe because of what the skipped row can
and cannot mean. It cannot mean a model-scoped suppression, because it has no
model identity. It cannot mean an account-wide suppression that the current
runtime intended, because the current runtime refuses to write one and the
predecessor runtime ignored one. The only thing it can mean is a leftover
artifact, and an artifact that is removed and reported is strictly better than an
artifact that decides whether the process starts.

The authentication-failure change is a tightening, not a relaxation. An account
whose credential was rejected stays disabled across a restart. The suppression
is released by the existing credential-change rehash path
(`reload.rs:1107-1141`), which clears the rows for the accounts whose
credentials change and restores them otherwise, so a legitimate credential
rotation is not blocked by the failure it is repairing. Operators can also
clear the row directly through the existing `clear_authentication` path. The
control is bounded by the same operator surfaces that already exist.

The uniqueness index removes a class of silent state divergence. Under the old
constraint, two writers could both believe they had inserted the account-scoped
row, and the upsert's `SELECT` (`rust/src/health/repository.rs:240`) would pick
one arbitrarily and update only it, leaving the other stale. The partial index
makes that state unreachable. The upsert's transaction is already serialized
(`BEGIN IMMEDIATE` under a single-writer gate), so the change adds a
correctness guarantee rather than a new concurrency mechanism.

## Verification

- A test inserts a durable `account_backoffs` row with reason `model_unavailable`
  and a NULL `model_id`, hydrates, and asserts that the hydration succeeds, that
  the row is reported as skipped, and that no account or model scope is granted
  on the strength of it.
- A test asserts that the same malformed row does not prevent a generation from
  being published, so the startup path is exercised rather than assumed.
- A test asserts that a durable `authentication_failed` row disables the account
  on hydration, with no deadline, and that the row is released when the
  account's credential changes.
- A test asserts that the live failure path writes the `authentication_failed`
  durable row on a credential rejection, and that it does not write a
  `model_unavailable` row with a NULL `model_id`.
- A test asserts that inserting a second account-scoped row for the same
  `(account_id, reason)` is rejected by the durable schema.
- A migration test asserts that the v64 migration removes the legacy rows and
  that the migration chain remains contiguous from v1.
- The migration checksum inventory guard and the architecture migration census
  guard both pass.

## Supersession

None. This record supersedes no accepted decision. It gives effect to the
`migration_legacy` promise stated in
`rust/assets/db/migrations/0051_model_quarantine.sql:5-7` by recording why that
promise is not implementable as written, and it closes the deferred observation
in `plans/closure/routing-selection/007-status.md` and
`plans/closure/request-admission-wire/008-status.md` only to the extent that
those records describe the parallel `HealthEffectApplier`. Those closure records
are immutable and are not edited; the applier's disposition is deferred to a
successor milestone in this subsystem.
