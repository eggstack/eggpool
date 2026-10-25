# Routing Selection Milestone 009 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/routing-selection/009-health-effect-ownership-and-terminal-withdrawal.md`

Decision implemented:

- `plans/adrs/ADR-0006-authoritative-terminal-model-withdrawal-ownership.md`
  (accepted 2026-10-25)

Source subsystem roadmap:

- `plans/subsystems/routing-selection-roadmap.md`

Repository baseline reviewed: `08c1b114`

Implementation commits or pull requests:

`2304d6ca`

- Health effect ownership and terminal-withdrawal producer retirement: the
  parallel `health::effects` applier is deleted, the unreachable
  authoritative-absence producer is removed with the terminal-withdrawal
  branch it could select, the health manager's in-memory terminal set is
  removed, and the durable terminal vocabulary is retained, annotated, and
  still enforced.

## 1. Executive finding

The health subsystem now has exactly one effect-application path, and it is the
path the runtime uses.

`rust/src/health/effects.rs` was a second implementation of the failure-effect
application boundary. It had never had a production caller:
`git log -S 'HealthEffectApplier' -- rust/` returned exactly one commit, the
one that introduced it. It was also wrong in two ways the test suite already
pinned. It disabled jitter where the live path enables it, so its own test
asserted a backoff of `500.0` seconds where the live policy produces `462.5`.
And it wrote the account's escalation rung into a model durable row, which is
the defect Routing Selection M007 corrected in the production path only. A
second, contradictory implementation of a bounded safety mechanism is a defect
in its own right: the next reader cannot tell which one the runtime honours.
The file, its module declaration, its re-exports, and its test are deleted.

The unreachable terminal-withdrawal producer is gone with it.
`ProviderModelPresence::AbsentAuthoritative` was never constructed in
production. The only producer set `Unknown`, and the only other assignments
were `Known`, all three in tests. So the `terminal_withdrawal` model effect was
never selected, the router's terminal branch never ran, and the in-memory
`terminal_models` set was never populated in production. The
`AbsentAuthoritative` variant, the `FailureObservation::provider_model_presence`
field, the terminal branch in the `model_effect` selection, the `terminal: bool`
parameter of `HealthManager::disable_model`, and the `terminal_models` field on
`AccountHealth` and `AccountHealthSnapshot` are removed. What the health
manager could no longer express — a terminal withdrawal that a later timed
disable must not clear — had no producer and no reachable state.

What stays is the durable terminal vocabulary, and it stays enforced.
`QuarantineState::TerminalWithdrawn`, `ModelQuarantine::set_terminal_withdrawn`,
and the `EvidenceProvenance::ProviderCatalog` and `MigrationLegacy` variants
are retained, each annotated in `rust/src/health/quarantine.rs` with the fact
that the runtime has no producer for it, that it is retained so an existing
durable row keeps being parsed and keeps being enforced, and that
reintroducing a producer requires a milestone that first makes the success
path's clearing durable — the prerequisite the closed M005 record records at
`plans/closure/routing-selection/005-status.md:227`.

The retained enforcement is a different mechanism from the one that was
removed, and the distinction matters. The removed `terminal_models` set was
the health manager's own and was populated only by `disable_model`. The
retained enforcement is the quarantine map's terminal state together with the
routing eligibility gate that consults it, and that is where a hydrated
`terminal_withdrawn` row lands. A durable row carrying the terminal state is
still parsed, still excludes the model from the routing plan with the
`model_quarantined` reason code, and is still lifted by the success path.

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
| The repository contains one effect-application path, and it is the one the runtime uses | `rust/tests/durable_health_effects.rs::the_health_effect_ownership_is_single_and_the_terminal_producer_is_gone`; `git log -S 'HealthEffectApplier' -- rust/` before the change returned exactly one commit, the one that introduced it | pass | `rust/src/health/effects.rs` does not exist; `rust/src/health/mod.rs` declares no `effects` module and re-exports no `HealthEffect` type; the guard asserts the file's absence, the facade's exports, and that `rust/src/routing/router.rs::RoutingRouter::apply_failure_effects` is the live owner |
| No module, function, type, enum variant, or field in the health subsystem is unreachable for want of a producer, except the retained durable terminal vocabulary, which is annotated and still enforced | `rust/tests/durable_health_effects.rs::the_health_effect_ownership_is_single_and_the_terminal_producer_is_gone`; `rust/tests/durable_health_effects.rs::a_durable_terminal_quarantine_row_is_parsed_and_enforced`; `rust/tests/health.rs` (13 passed) | pass | The applier module, its three public types, and the health manager's `terminal_models` field are gone. `QuarantineState::TerminalWithdrawn`, `set_terminal_withdrawn`, `EvidenceProvenance::ProviderCatalog`, and `EvidenceProvenance::MigrationLegacy` are retained, each carrying a source comment that records the absent producer, the retained-row reason, and the M005 prerequisite |
| The three public types are gone from the library crate's public surface, and the removal is recorded in the ADR and in this milestone's closure record | `rust/src/health/mod.rs`; `cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --offline --locked -- -D warnings` (exit 0); `rust/tests/durable_health_effects.rs::the_health_effect_ownership_is_single_and_the_terminal_producer_is_gone` | pass | `health::HealthEffect`, `health::HealthEffectApplier`, and `health::HealthEffectOutcome` are no longer exported. The ADR's Compatibility section names them; this record names them again in §3 and §7 |
| A durable quarantine row carrying the terminal state is still parsed and still enforced | `rust/tests/durable_health_effects.rs::a_durable_terminal_quarantine_row_is_parsed_and_enforced`; `rust/tests/health.rs::quarantine_is_exact_key_bounded_and_terminal_recovery_is_authoritative`; `rust/tests/health.rs::authoritative_reappearance_lifts_a_terminal_withdrawal_by_account_and_model` | pass | The seeded durable row carries `state = 'terminal_withdrawn'` and `expiry_epoch IS NULL`; the hydration parses it into `QuarantineState::TerminalWithdrawn`; `is_model_quarantined` is true; the routing plan excludes the account with the `model_quarantined` reason code; `RoutingRouter::record_success` lifts it and the account becomes routable again |
| The bounded quarantine, the durable backoff writers, the policy table, the reason vocabulary, the escalation rungs, and the routing behaviour of a healthy system are unchanged | `git diff -- rust/src/health/backoff.rs` is empty; `rust/tests/backoff_policy_adoption.rs` (12 passed); `rust/tests/health.rs` (13 passed); `rust/tests/durable_health_effects.rs` (21 passed, of which the twelve pre-existing tests are unchanged); `rust/tests/status_command.rs` (16 passed); `rust/tests/coordinator_c011.rs` (18 passed); `rust/tests/coordinator_c013.rs` (5 passed); `rust/tests/coordinator_finalization.rs` (11 passed) | pass | The policy table was not edited. The durable-effects target grew from eighteen tests to twenty-one without changing one of the eighteen. The classifier's model effect for a model absence is still the bounded `quarantine` |
| The default and no-default-features workspace suites pass serially, and the tooling suite and the linters pass | `cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --offline --locked -- --test-threads=1`; `cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features --offline --locked -- --test-threads=1`; `cargo check`/`cargo clippy` with `--no-default-features`; `uv run pytest tests/tooling/ -q --tb=short`; `uv run ruff format --check scripts/ tests/tooling/`; `uv run ruff check scripts/ tests/tooling/`; `uv run pyright scripts/` | pass | See §4 for the exit status and the counts of every command |

## 3. Production implementation evidence

The following production files were changed by this milestone.

- `rust/src/health/effects.rs` — deleted. It was 241 lines and contained
  `HealthEffect`, `HealthEffectApplier`, `HealthEffectOutcome`, and the
  `apply` method that duplicated the live path. The deletion is committed with
  this milestone's implementation, at `2304d6ca`; see §4 for the note on how it
  was staged.
- `rust/src/health/mod.rs` — `mod effects;` and
  `pub use effects::{HealthEffect, HealthEffectApplier, HealthEffectOutcome};`
  are removed. `pub mod health` itself remains public and its remaining exports
  are unchanged.
- `rust/src/health/health_manager.rs` — the `terminal_models: BTreeSet<String>`
  field is removed from `AccountHealth` and from `AccountHealthSnapshot`,
  together with its clone, its initialiser, its three readers, and its three
  removal sites. `HealthManager::disable_model` loses its `terminal: bool`
  parameter, and with it the `terminal || duration.is_none()` branch and the
  terminal-withdrawal comment block. The method now does one thing: it inserts
  the model into `disabled_models` with the deadline it was given.
- `rust/src/health/quarantine.rs` — no behaviour change. `QuarantineState::TerminalWithdrawn`,
  `ModelQuarantine::set_terminal_withdrawn`, `EvidenceProvenance::ProviderCatalog`,
  and `EvidenceProvenance::MigrationLegacy` are annotated in place with the
  absent producer, the retained-row reason, and the M005 prerequisite.
  `ModelQuarantine::is_model_quarantined` is annotated as the retained
  enforcement mechanism and is distinguished from the removed in-memory set.
- `rust/src/coordinator/failure.rs` — the `provider_model_presence` field is
  removed from `FailureObservation` and from its `local` constructor; the
  `AbsentAuthoritative` variant is removed from `ProviderModelPresence`; and
  the terminal branch in the `model_effect` selection collapses into the
  bounded `quarantine` arm. `ProviderModelPresence` keeps `Known` and
  `Unknown`, as ADR-0006 requires.
- `rust/src/routing/router.rs` — the terminal flag argument is removed from the
  `disable_model` call in `apply_failure_effects`. The `model_effect`
  parameter of that method is retained in place but renamed `_model_effect`,
  because ADR-0006's Compatibility section does not list
  `RoutingRouter::apply_failure_effects` among the changed public surfaces and
  the method's three coordinator call sites are outside this milestone's file
  ownership. The published signature is unchanged. The doc comment on
  `persist_model_quarantine`, which described the live path mirroring a
  terminal withdrawal, is corrected: the live path only ever writes a bounded
  observation, because the runtime has no producer for the terminal state.
- `rust/src/operations/status.rs` — `has_model_quarantine` reads
  `!snapshot.disabled_models.is_empty()`. The two expressions it replaces
  agree in every reachable state, because `disable_model` inserts into
  `disabled_models` in both of its branches and `terminal_models` was only
  ever inserted as a subset of it, so the published boolean does not change.

The published-surface change is exactly the one ADR-0006's Compatibility
section names, and nothing else: `health::HealthEffect`,
`health::HealthEffectApplier`, and `health::HealthEffectOutcome` are removed;
the public `terminal_models` field is removed from `health::AccountHealth` and
`health::AccountHealthSnapshot`; the public
`coordinator::ProviderModelPresence::AbsentAuthoritative` variant is removed;
the public `coordinator::FailureObservation::provider_model_presence` field
is removed; and the `terminal: bool` parameter of
`health::HealthManager::disable_model` is removed. `pub mod health` and
`pub mod coordinator` remain public. `FiniteRequest` and
`CompactAdmittedRequest` were not touched.

No migration was added, and no migration, migration checksum, or schema asset
was edited. `rust/src/health/repository.rs` was not edited; it is owned by
Routing Selection M008, which is closed.

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
backed up to `/tmp` before the first `uv` command and were verified
byte-identical afterwards (`cmp` clean, and `git status --short` clean for
both).

### Results

| Command | Exit | Observed |
|---|---|---|
| `cargo fmt --all -- --check` | 0 | No diff; the tree is formatted |
| `cargo clippy --workspace --all-targets -- -D warnings` | 0 | No warnings; no `#[allow(...)]` was added |
| `cargo check --workspace --all-targets --no-default-features` | 0 | Compiles without the SSH default |
| `cargo clippy --workspace --all-targets --no-default-features -- -D warnings` | 0 | No warnings; no `#[allow(...)]` was added |
| `cargo test --workspace --all-targets -- --test-threads=1` | 0 | 90 test binaries; **1,216 passed, 0 failed, 0 ignored**. `durable_health_effects` 21 passed; `health` 13 passed; `status_command` 16 passed; `backoff_policy_adoption` 12 passed; `coordinator_c011` 18 passed; `coordinator_c013` 5 passed; `coordinator_finalization` 11 passed |
| `cargo test --workspace --all-targets --no-default-features -- --test-threads=1` | 0 | **1,217 passed, 0 failed, 0 ignored** (additional evidence; CI runs no-default for `check`/`clippy` only. The one-test difference from the default run is `rust/tests/provider_transport.rs`, which is feature-gated) |
| `uv run pytest tests/tooling/ -q --tb=short` | 0 | 251 passed, 1 skipped in 100.07 s |
| `uv run ruff format --check scripts/ tests/tooling/` | 0 | 71 files already formatted |
| `uv run ruff check scripts/ tests/tooling/` | 0 | All checks passed! |
| `uv run pyright scripts/` | 0 | 0 errors, 0 warnings, 0 informations |
| `uv run python scripts/validate_release_docs.py` | 0 | `{"docs_checked": 7, "production_release": "published 0.8.2", "python_reference": "historical external artifacts", "status": "pass", "targets": ["linux-aarch64", "linux-x86_64", "macos-arm64"], "version": "0.8.2"}` |

### Regression-guard evidence (mutation check)

Each corrected behaviour was shown to fail against a deliberate perturbation of
the production code. The repository forbids `git checkout`, `git restore`,
`git stash`, `git reset`, and `git clean`, so every perturbation was applied by
hand and undone by hand. Each restored file was verified byte-identical
against a `/tmp` copy with `cmp` before the suite was re-run green.

| Perturbation | Observed failure | Restored run |
|---|---|---|
| The terminal branch is restored: `FailureObservation::provider_model_presence` is re-added, `ProviderModelPresence::AbsentAuthoritative` is re-added, the `terminal_withdrawal` branch is restored in the model-absent arm, and the observation's default presence is set to `AbsentAuthoritative` so the restored branch has a producer | `the_live_model_absence_path_quarantines_and_never_withdraws_terminally` FAILED — `assertion 'left == right' failed: a model absence must produce the bounded quarantine effect` / `left: "terminal_withdrawal"` / `right: "quarantine"`; `the_health_effect_ownership_is_single_and_the_terminal_producer_is_gone` FAILED — `the terminal-withdrawal producer must not come back: terminal_withdrawal` — 19 passed, 2 failed | `rust/src/coordinator/failure.rs` restored byte-identical (`cmp` clean); `durable_health_effects` 21 passed, 0 failed |
| The applier is restored: `rust/src/health/effects.rs` is recreated from `08c1b114` (241 lines; the fourth argument its `disable_model` call no longer takes is dropped so the crate still compiles), and `mod effects;` plus the three `pub use` re-exports are restored in `rust/src/health/mod.rs` | `the_health_effect_ownership_is_single_and_the_terminal_producer_is_gone` FAILED — `the parallel health effect applier must not come back` — 20 passed, 1 failed | `rust/src/health/effects.rs` removed again with an absolute-path deletion; `rust/src/health/mod.rs` restored byte-identical (`cmp` clean); `durable_health_effects` 21 passed, 0 failed |
| The live-path assertions are removed: the `assert_eq!` on `effects.model_effect` and the `assert!` on `effects.quarantine_model` are deleted from `the_live_model_absence_path_quarantines_and_never_withdraws_terminally` | `the_health_effect_ownership_is_single_and_the_terminal_producer_is_gone` FAILED — `the live-path coverage must stay: a model absence must produce the bounded quarantine effect` — 20 passed, 1 failed | `rust/tests/durable_health_effects.rs` restored byte-identical (`cmp` clean); `durable_health_effects` 21 passed, 0 failed |

The third perturbation exposed a defect in the coverage guard as first written:
the guard's own source contained the literal assertion messages it searched
for, so it satisfied its own search and did not fail. The guard now assembles
its needles at runtime from two halves each, so its own source cannot satisfy
the search. The mutation was then re-run against the corrected guard and
failed as recorded above.

### Note on how the applier deletion was committed

`rust/src/health/effects.rs` was removed with `git rm`, which staged the
deletion. While this milestone was in progress, a commit created by another
process swept that staged deletion into itself, so the deletion was briefly
recorded under a message that did not describe it. The milestone owner resolved
this before closing: the index was reset, the plan-header commit was rebuilt
without the deletion, and the deletion is now committed with this milestone's
implementation at `2304d6ca`, under a message that describes it. The milestone's
own content was verified byte-for-byte identical across that change; see §10.

This milestone authored no commit and ran no commit command.

## 5. Invariant review

- *The backoff policy table is unchanged.* `git diff -- rust/src/health/backoff.rs`
  is empty; `rust/tests/backoff_policy_adoption.rs` passes unchanged (12
  passed).
- *The reason vocabulary is unchanged.* No `BackoffReason` variant was added,
  removed, or reclassified, and the persisted reason strings are byte-identical
  to the pre-change set.
- *The scope rules are unchanged.* A NULL `model_id` still means account-wide
  suppression and a non-NULL value still scopes the row to one (account,
  model) pair. The durable writers are untouched.
- *The escalation rungs are unchanged.* A model-scoped row carries the model's
  own rung and an account-scoped row carries the account's. The router's
  `policy_backoff` still derives the model rung from `record_model_failure`
  and seeds it from the durable rung on hydration.
- *The quarantine state machine is unchanged.* `Suspected` still promotes to
  `Quarantined`, an exact key still clears, and the terminal state still never
  expires. The only change to `rust/src/health/quarantine.rs` is annotation.
- *The routing behaviour of a healthy system is unchanged.* The only
  production change on the routing path is the removal of a terminal argument
  that was always `false`. The source-level guard
  `the_selection_path_performs_no_durable_health_write` still passes, and the
  eligibility gate, the selection lock, and the claim book are untouched.
- *The failure classification is unchanged.* The model-absent 404 arm still
  classifies as `FailureCategory::ModelUnavailable` with the bounded
  `quarantine` model effect. The arm it collapsed into is the arm the runtime
  already took, because the terminal branch had no producer.
- *One effect-application path remains.* The live path is
  `RoutingRouter::apply_failure_effects` in `rust/src/routing/router.rs`. The
  applier that duplicated it is deleted, along with the divergence it carried:
  a jitter-free backoff and an account rung written into a model durable row.
- *The single SQLite gate, the streaming mpsc bridge, the Tokio
  `current_thread` runtime, and the routing selection lock are unchanged.* No
  lock, no gate, and no shared state was added or removed.
- *No credential, prompt, raw body, or cache key enters any affected path.*
  The removed code carried account names, model ids, reasons, and timestamps
  only. The new test fixtures use synthetic account names, synthetic model ids,
  synthetic upstream protocols, and epoch timestamps.
- *The durable terminal vocabulary is still enforced.* A durable row carrying
  `terminal_withdrawn` is parsed into `QuarantineState::TerminalWithdrawn`,
  excludes the model from the routing plan with the `model_quarantined` reason
  code, and is lifted by the success path.

## 6. Failure and recovery review

- *Duplicate delivery.* The removed applier was not on any delivery path, so
  nothing that depended on it changed. The live path's durable writes are
  unchanged and remain idempotent on their keys.
- *Restart.* A database that already contains a `terminal_withdrawn` row still
  starts, still hydrates the row, and still gates the model. This is the
  behaviour the retained vocabulary exists to preserve, and it is asserted
  end to end.
- *Partial persistence failure.* Unchanged. A failed durable write is warned
  about and the live in-memory effect stands, exactly as before.
- *Stale rows.* Unchanged. A row naming an unknown account is still skipped
  with a warning rather than failing the hydration batch.
- *Malformed rows.* Unchanged. A terminal row with an expiry is still rejected
  by the row validator; a non-terminal row without an expiry is still
  rejected.
- *Cancellation.* Unchanged. The removed code was not on a cancellation path;
  the live path's writes are unchanged and idempotent.
- *Contention.* Unchanged. No lock was added or removed; the removed
  `terminal_models` set was read under the health manager's existing accounts
  lock and its removal removes a read, not a write.
- *The health manager's terminal protection.* The capability that a later
  timed disable must not clear a terminal withdrawal is gone with the set that
  expressed it. It had no producer: `disable_model` was never called with a
  terminal flag by any production path. The only production call sites pass a
  policy delay, and every reason that can quarantine a model
  (`ModelUnavailable`, `ConnectTimeout`, `UpstreamServerError`) has a positive
  policy delay, so `disable_model` is never reached with a deadline-free
  duration from the router. The hydration path always passes a remaining
  duration. The behaviour of a deadline-free model disable is unchanged in
  every reachable state: the model stays disabled indefinitely.

## 7. Migration and compatibility review

There is no storage migration. No migration file was added, renumbered, or
edited, and no migration checksum was changed. The migration chain is
unchanged at v1–v64.

- The `model_quarantine` table keeps every state and provenance it admits
  today. `QuarantineState` keeps `Healthy`, `Suspected`, `Quarantined`, and
  `TerminalWithdrawn`; `EvidenceProvenance` keeps `RuntimeHttp`,
  `ProviderCatalog`, `ModelInfo`, `ManualOverride`, `OperatorAction`, and
  `MigrationLegacy`. The `state` and `evidence_provenance` columns are plain
  text with no check constraint, so the schema still admits every value it
  admitted before this milestone.
- Every existing row is parsed and enforced exactly as it is today. A row
  carrying `terminal_withdrawn` was already parsed by
  `QuarantineState::try_from` and already enforced by
  `ModelQuarantine::is_model_quarantined` and the routing eligibility gate;
  this milestone removed no parser and no gate. The new test
  `a_durable_terminal_quarantine_row_is_parsed_and_enforced` proves it from a
  durable row through hydration, through the gate, and through the success
  path's lifting write.
- The durable writers are unchanged. `rust/src/health/repository.rs` was not
  edited; it is owned by Routing Selection M008, which is closed.
- The published API change is exactly the one ADR-0006's Compatibility section
  names: `health::HealthEffect`, `health::HealthEffectApplier`, and
  `health::HealthEffectOutcome` are removed; the public `terminal_models`
  field is removed from `health::AccountHealth` and
  `health::AccountHealthSnapshot`; the public
  `coordinator::ProviderModelPresence::AbsentAuthoritative` variant is
  removed; the public `coordinator::FailureObservation::provider_model_presence`
  field is removed; and the `terminal: bool` parameter of
  `health::HealthManager::disable_model` is removed. A downstream consumer of
  any of those names must stop using it; nothing else in the public surface
  changed.
- `pub mod health` and `pub mod coordinator` remain public. `ProviderModelPresence`
  remains public with `Known` and `Unknown`, as ADR-0006 requires.
- `RoutingRouter::apply_failure_effects` keeps its published signature. Its
  `model_effect` parameter is retained in place and renamed `_model_effect`;
  ADR-0006's Compatibility section does not list that method among the changed
  surfaces, and its three coordinator call sites are outside this milestone's
  file ownership.
- The `FiniteRequest` and `CompactAdmittedRequest` compatibility surfaces were
  not touched.
- No configuration key, endpoint, response shape, diagnostic label, or
  operator-visible output changed. `has_model_quarantine` in
  `rust/src/operations/status.rs` keeps the same value in every reachable
  state.
- Rollback limits: rolling back to a release that predates this milestone
  restores the removed public types, which is a source-compatibility change
  for any consumer that adopted them, and the applier. No durable row is
  invalidated by the rollback, because no durable format changed.

## 8. Security review

- *Auth enforcement* — unchanged. No credential check, no auth path, and no
  account scope was touched.
- *Secret handling* — unchanged. The removed code carried account names,
  model ids, reason vocabulary, and timestamps only. No credential, prompt,
  raw body, or cache key enters any log, diagnostic, or test fixture; the new
  fixtures use synthetic account names (`account-a`), synthetic model ids
  (`shared-model`), synthetic upstream protocols (`openai`), a synthetic
  reason string, and epoch timestamps.
- *Privilege bounds* — unchanged. No capability, permission, or scope was
  added. The removed `terminal_models` set was a health manager projection,
  not an access-control structure.
- *DoS bounds* — unchanged. The quarantine map's cap, the hydration row cap,
  and the backoff policy caps are untouched.
- *Audit behavior* — unchanged. The failure-path labels
  (`retry_action`, `account_effect`, `model_effect`, `circuit_effect`,
  `wire_effect`) keep their values; the model effect for a model absence was
  already `quarantine` in every reachable state.

## 9. Documentation and operations

- `architecture/deep-dive-health.md` — the effect-application section now
  names `routing/router.rs::RoutingRouter::apply_failure_effects` as the one
  effect-application path the runtime uses, and records that the parallel
  applier is gone. The quarantine section describes the terminal vocabulary as
  producer-less and names the retained-row reason. The durable-mirror section
  records that the live path only ever writes a bounded observation.
- `architecture/deep-dive-retry.md` — the "narrowest health effect and
  durability" section names the live path as the consequence owner instead of
  `HealthEffectApplier`, and describes the terminal state as a producer-less
  durable reader. The status-mapping section records that a model-absent 404
  always quarantines and that the terminal state stays as a producer-less
  reader the eligibility gate keeps enforcing.
- `plans/subsystems/routing-selection-roadmap.md` — the M009 status row reads
  `closed` and names this record; the M007 deferred-work entry for the
  parallel `HealthEffectApplier` now records that Milestone 009 discharged it
  and is closed; the dependency graph records that M009 is closed.
- `plans/closure/routing-selection/007-status.md` and
  `plans/closure/request-admission-wire/008-status.md` are immutable and were
  not edited. `plans/registry.md` was not edited; §12 describes the registry
  changes the milestone owner applies.
- No change to `AGENTS.md`, to the release documentation, or to any published
  user-facing documentation.
- Operator guidance: no operator action is required. The `status` command's
  model-quarantine reporting is unchanged. A database that already contains a
  `terminal_withdrawn` row keeps gating that model, exactly as before.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| resolved | `rust/src/health/effects.rs` was deleted with `git rm`, and a commit created during this milestone by another process swept that staged deletion into itself, so the deletion was briefly recorded under a message that did not describe it. This milestone authored no commit | None outstanding. The milestone owner reset the index, rebuilt the plan-header commit without the deletion, and committed the deletion with this milestone's implementation at `2304d6ca` under a message that describes it. The milestone's content was verified byte-for-byte identical across the change | None. Closed by the milestone owner before closure |
| low | `ProviderModelPresence` now has no production producer and no production reader: the `provider_model_presence` field is gone, so neither remaining variant (`Known`, `Unknown`) is constructed anywhere | A public enum whose two variants are only reachable from tests. ADR-0006 requires both variants to be retained, so this is the ADR's intent, not an oversight | None for this milestone. A successor milestone that introduces an authoritative-absence producer reattaches the field; until then the type is producer-less |
| low | `ModelQuarantine::set_terminal_withdrawn` now has no production caller: its only in-tree caller was the deleted applier | The durable terminal state can no longer be written by the runtime. The read, the parse, the gate, and the success path's lifting write are all still live | None. This is the retained durable terminal vocabulary ADR-0006 keeps. Reintroducing a writer requires the M005 prerequisite recorded at `plans/closure/routing-selection/005-status.md:227` |
| low | `EvidenceProvenance::ProviderCatalog` and `EvidenceProvenance::MigrationLegacy` have no producer | The two provenances are read-only vocabulary. The `migration_legacy` observation is already recorded as an open finding by the M008 closure record | None. `migration_legacy` is the M008 record's inherited observation, unchanged by this milestone. `provider_catalog` is retained with it under ADR-0006 |
| info | `HealthManager::disable_model` with a deadline-free duration now records a permanent model disable that a later success clears, where it previously also recorded a terminal set entry that a later success did not clear | No reachable production difference: the router always passes a policy delay for every reason that can quarantine a model, and the hydration path always passes a remaining duration | None. The capability had no producer and no reachable state, which is the premise of ADR-0006 |

## 11. Roadmap disposition

The milestone is closed, and the next dependency may proceed. Milestone 009's
status row in `plans/subsystems/routing-selection-roadmap.md` reads `closed`
and names this record. Milestone 009 was the last milestone of the Routing
Selection roadmap's health-effect work, and the roadmap's completion
definition — M001 and M003 closed with semantic and performance evidence, and
M004–M009 closed with correctness and durable-health evidence — is now
satisfied.

Milestone 007's deferred-work entry for the parallel `HealthEffectApplier` now
records that this milestone discharged it and is closed.

The successor work is named and not started: an authoritative-absence producer
that would restore a terminal-withdrawal producer requires a milestone that
first makes the success path's clearing durable, the prerequisite the closed
M005 record records at `plans/closure/routing-selection/005-status.md:227`. It
is not this milestone's work, and no plan for it is registered.

## 12. Registry updates

Changes required in `plans/registry.md`, to be applied by the milestone owner
in the same change set. This milestone did not edit the registry.

- The `| Routing selection | M009 | ready | ... |` row moves out of the
  registry's **Dependency-ready implementation plans** table. That table
  becomes empty when this row leaves it, because the Deployment and Packaging
  M008 row was closed earlier in the same change set.
- The **Recently closed** table gains a Routing Selection M009 row. The
  disposition records that the repository now contains one effect-application
  path and it is the live one — the parallel `HealthEffectApplier` that had no
  production caller, disabled jitter, and wrote the account rung into a model
  durable row is deleted; that the unreachable authoritative-absence producer
  and the health manager's in-memory terminal set are removed with the
  terminal-withdrawal branch they fed; that the durable terminal vocabulary is
  retained, annotated as producer-less, and still parsed and still enforced by
  the routing eligibility gate; that the backoff policy table, the reason
  vocabulary, the scope rules, the escalation rungs, the quarantine state
  machine, and the routing behaviour of a healthy system are unchanged; and
  that three new durable-effects tests each guard is shown failing on a
  deliberate perturbation. The evidence column names
  `plans/closure/routing-selection/009-status.md`.
- The routing-selection row in the registry's **active subsystem roadmaps**
  table is updated: its latest-milestone column moves from "M009 ready — health
  effect ownership and terminal-withdrawal producer retirement" to "M009
  closed", and its note records that M009 is closed through
  `plans/closure/routing-selection/009-status.md`.
- The registry's routing-selection narrative paragraph, which currently states
  that M009 is ready and that its only other blocker, M008 closed, is
  discharged, is updated to record the M009 closure and the milestone's
  published-surface change.
