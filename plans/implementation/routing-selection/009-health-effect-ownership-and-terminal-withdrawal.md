# Routing Selection Milestone 009 — Health effect ownership and terminal-withdrawal producer retirement

Status: ready

Repository baseline: `1929abf5a725cc7faac0f866622afa7af017694a`

Source roadmap:

- `plans/subsystems/routing-selection-roadmap.md#7-milestones`

Long-term requirements:

- `plans/000-long-term-specification.md` — §2 end-state invariants, §3 ownership boundaries
- `plans/001-terminology-and-domain-model.md` — normative language for accounts, model absence, and quarantine state
- `plans/002-long-term-roadmap.md` — sustaining work must preserve a working proxy and current compatibility

Applicable ADRs:

- `plans/adrs/ADR-0006-authoritative-terminal-model-withdrawal-ownership.md`
  (**accepted** 2026-10-25 — this milestone implements that decision; see §2)

Primary class: invariant

## 1. Objective

Make the health subsystem say what it does: one effect-application path, one
owner, and no code that no producer can reach.

Concretely: remove the parallel `HealthEffectApplier` — which has never had a
production caller and which implements a durable terminal-withdrawal design
that M005 explicitly rejected — along with the unreachable terminal-withdrawal
producer that depends on it, and move the coverage that the removed test gave
the applier onto the path that actually runs.

## 2. Why this milestone is ready

**Ready.** This milestone removes three types from the library
crate's public surface (`pub mod health` at `rust/src/lib.rs:16` makes them
public API), and it changes a capability boundary: whether the authoritative
terminal model withdrawal exists as a supported capability. Both are the
repository's ADR threshold. The decision — what is retired, what is retained, and
what the prerequisite for reintroducing a producer is — is recorded in
`plans/adrs/ADR-0006-authoritative-terminal-model-withdrawal-ownership.md`,
which the project maintainers accepted on 2026-10-25.

The acceptance revision is the commit that changes that record from `proposed`
to `accepted`. Three passages in the proposed draft were corrected before
acceptance because they contradicted the record's own Decision or understated its
consequence: the two Decision bullets that read as contradictory now name the
removed set and the retained enforcement as the two different mechanisms they
are; the Compatibility section now lists the complete published-surface change
rather than three names; and the Negative consequences now name the capability
the milestone retires. The Decision is unchanged in substance: Option B is
selected.

The milestone's one hard dependency, **Routing Selection M008 closed**, is
discharged. M008 closed at implementation `b66373f2` with its closure record
`plans/closure/routing-selection/008-status.md`. M008 owned the durable health
state repair in `rust/src/health/repository.rs`,
`rust/src/health/health_manager.rs`, `rust/src/routing/router.rs`, and the
migration directory; this milestone edits the same files, which is why the two
did not run concurrently.

Every defect it addresses is reproducible from the repository at the stated
baseline. The milestone is dependency-ready in the registry.

## 3. Current implementation evidence

**The parallel applier has never had a production caller.**
`git log -S 'HealthEffectApplier' -- rust/` returns exactly one commit,
`d5dd16da` (D005), which introduced it. The live implementation of the same
responsibility is `RoutingRouter::apply_failure_effects`
(`rust/src/routing/router.rs:625`). The only references to the applier outside
its own definition are `rust/src/health/mod.rs:20` (the public re-export) and
`rust/tests/health.rs` (the test at `:365`).

**It diverges from the live path in ways the test suite pins.** The applier
passes `jitter = false` where the live path passes `jitter = true`, so the
test at `rust/tests/health.rs:380` asserts `backoff_seconds == Some(500.0)`
while the live policy produces **462.5 seconds** for the same failure. The
applier writes the *account's* escalation rung into a *model* durable row
(`rust/src/health/effects.rs:214` writes
`escalated_failures.max(next_cooldown)`; the live path writes
`model_rung.max(1)` at `rust/src/routing/router.rs:816`), which is the
defect M007 corrected in the production path only. The applier also calls
`quarantine.set_terminal_withdrawn` (`rust/src/health/effects.rs:144`), which
persists a terminal durable row that M005 refused to adopt.

**The terminal-withdrawal producer does not exist.**
`ProviderModelPresence::AbsentAuthoritative`
(`rust/src/coordinator/failure.rs:190`) is the only thing that selects the
terminal effect:

```rust
model_effect = if observation.provider_model_presence
    == ProviderModelPresence::AbsentAuthoritative
{
    "terminal_withdrawal"
} else {
    "quarantine"
};
```

The variant is **never constructed anywhere in the repository**. The only
producer of `provider_model_presence` is the observation constructor at
`rust/src/coordinator/failure.rs:167`, which sets `Unknown`; the only other
assignments are to `Known`, and all three are in test files
(`rust/tests/coordinator_c011.rs:1377`,
`rust/tests/coordinator_finalization.rs:405`,
`rust/tests/coordinator_c013.rs:126`). The field has exactly one read — the
comparison above — and no setter.

The consequences chain from there:

- `model_effect` is never `"terminal_withdrawal"`.
- `quarantine_model` is `model_effect == "quarantine"`
  (`rust/src/coordinator/failure.rs:691`), so it is false for a withdrawal.
- `rust/src/routing/router.rs:645-654` never runs for a withdrawal, and
  `rust/src/routing/router.rs:650` — the expression that selects the terminal
  flag — is unreachable.
- `disable_model` is never called with the terminal flag, so the in-memory
  `terminal_models` set (`rust/src/health/health_manager.rs:44`, `:68`) is
  never populated.

**`terminal_models` has no second route either.** `disable_model`
(`rust/src/health/health_manager.rs:468-490`) inserts into `terminal_models`
when `terminal || duration.is_none()`. The `duration.is_none()` arm needs a
policy with no timed backoff. `get_backoff_policy`
(`rust/src/health/backoff.rs:179-209`) yields no delay only for
`AuthenticationFailed` (base delay 0.0), `ContextLimitExceeded`, and
`Unknown`. The three live quarantine sites
(`rust/src/coordinator/failure.rs:506`, `:584`, `:622`) carry
`ModelUnavailable`, `ConnectTimeout` (408), and `UpstreamServerError`, all
of which have a non-zero base delay, so `policy.delay` is always `Some` on the
quarantine path and `duration` is never `None` there. The
`terminal_models` set is therefore unreachable in production.

**The durable terminal state was a deliberate exclusion.**
`plans/closure/routing-selection/005-status.md:227` records that "a terminal
model withdrawal is mirrored as a bounded observation rather than a terminal
durable row", because the in-memory success path that lifts a terminal
withdrawal is synchronous and could never lift a durable row. The record
states the prerequisite for doing more: "A follow-up plan must either make the
success path's clearing durable or add an explicit terminal-withdrawal durable
state with its own clearing path."
`architecture/deep-dive-health.md:135` and `:165` record the same rule.

**The same pattern holds for the durable provenance vocabulary.**
`EvidenceProvenance::ProviderCatalog` and
`EvidenceProvenance::MigrationLegacy`
(`rust/src/health/quarantine.rs:91-95`) are **never constructed** anywhere.
They are retained by this milestone as durable vocabulary, with the
in-memory terminal enforcement that reads them.

**The Python-era runtime had a producer; the Rust runtime does not.**
`src/eggpool/failure/classifier.py:317` computed
`authoritative = obs.source == "provider_catalog"`, and
`src/eggpool/model_info/sources/provider_catalog.py` produced observations
with that source. The Rust runtime has no equivalent source:
`rust/src/runtime.rs:1303` exposes `refresh_model_info_from_catalog` as an
operator command, not a runtime task, and the `model_info` table it writes is
never read by the failure path.

**The catalogue refresh computes the fact and discards it.**
`rust/src/catalog/refresh.rs:346` produces `withdrawn_model_ids`. Whether it
is acted on is decided at `rust/src/catalog/refresh.rs:449-456`, which permits
withdrawal only when the policy is not `preserve_until_health` — the default
(`rust/src/config.rs:194`). The configuration comment
(`rust/config.example.toml:83-93`) enumerates what suppresses routing under
that policy — authentication failure, quota exhaustion, rate-limit, cooldown,
circuit open, account disabled — and a terminal model withdrawal is not among
them.

**The two architecture deep dives name the applier as the owner.**
`architecture/deep-dive-health.md:70-79` and
`architecture/deep-dive-retry.md:122` describe the parallel applier as the
effect-application owner. The deep dives are not covered by CI's documentation
skip, so the correction ships with the code change.

**The deferral is already recorded.** The closed M007 record
(`plans/closure/routing-selection/007-status.md`) and the closed M008 record
(`plans/closure/request-admission-wire/008-status.md`) each offer exactly two
dispositions for the applier: delete it, or give it a consumer. ADR-0006
chooses the first, because M005's accepted design rules out the second for the
durable half.

## 4. Invariants that must not regress

- The bounded quarantine is unchanged: its window, its escalation, its
  corroboration rule, its durable mirror, and its auto-clear on recovery.
- The `model_quarantine` table, its schema, its states, and its provenances are
  unchanged. No migration is required by this milestone.
- A durable quarantine row that carries the terminal state is still parsed and
  still enforced. The retained vocabulary keeps working exactly as it does
  today.
- `EvidenceProvenance::ProviderCatalog` and
  `EvidenceProvenance::MigrationLegacy` remain parseable, so an existing row
  with either provenance is never reinterpreted or dropped.
- The live durable backoff writers are unchanged: the reason they record, the
  escalation rung of the scope they belong to, and the account-scoped and
  model-scoped partition are unchanged.
- The backoff policy table and the reason vocabulary are unchanged.
- The failure classification is unchanged for every reachable case. No reason
  is added, removed, or reclassified, and no category's effect changes.
- The routing, selection, account-scoring, and admission behaviour of a healthy
  system is unchanged.
- The single SQLite gate, the streaming mpsc bridge, the Tokio
  `current_thread` runtime, and the routing selection lock are unchanged.
- No credential, prompt, raw body, or cache key enters any affected path.

## 5. Scope

### In scope

- The parallel `HealthEffectApplier` module and its public re-export.
- The unreachable terminal-withdrawal producer: the `terminal_withdrawal` model
  effect, the `AbsentAuthoritative` variant, the `provider_model_presence`
  field that carries only that variant, and the unreachable terminal selection
  in the router.
- The in-memory `terminal_models` set and the eligibility checks that read it,
  which nothing populates once the producer is gone.
- The coverage the removed applier test gave the removed applier, moved onto
  the live path.
- The two architecture deep dives that name the applier as the effect owner.
- The routing-selection roadmap's M007 deferral, discharged by this milestone.

### Explicitly out of scope

- The durable health state repair, which is
  `plans/implementation/routing-selection/008-durable-health-state-repair-and-authentication-failure-durability.md`
  under
  `plans/adrs/ADR-0005-durable-health-state-repair-and-authentication-failure-durability.md`.
  This milestone must not depend on it having landed, only on it having
  closed.
- The retained durable terminal vocabulary and its enforcement:
  `QuarantineState::TerminalWithdrawn`, `set_terminal_withdrawn`,
  `EvidenceProvenance::ProviderCatalog`, and
  `EvidenceProvenance::MigrationLegacy` all stay, with the in-memory terminal
  enforcement that reads them.
- Any change to `catalog_withdrawal_policy`, the default
  `preserve_until_health`, the account catalogue refresh, or what that policy
  suppresses. This milestone does not extend the policy's suppression set.
- Any introduction of an authoritative-absence producer. That requires the
  M005 prerequisite — a durable success path — and is a separate milestone.
- The `model_quarantine` table, its migrations, and its durable schema.
- The wire codecs, the provider transport, the request admission path, and the
  coordinator's retry behaviour.
- Any change to the on-prem surfaces, the browser session, or the
  authentication throttle.

## 6. Required production changes

### 6.1 The parallel applier module is removed

`rust/src/health/effects.rs` is deleted. It defines `HealthEffect`,
`HealthEffectOutcome`, and `HealthEffectApplier`, and nothing else in the
repository constructs the applier.

`rust/src/health/mod.rs:20` drops the re-export
`pub use effects::{HealthEffect, HealthEffectApplier, HealthEffectOutcome};`
and the `mod effects;` declaration at `:9`.

This removes three names from the library crate's public surface. `pub mod
health` at `rust/src/lib.rs:16` stays; the module's remaining exports are
unchanged.

### 6.2 The unreachable terminal-withdrawal producer is removed

`rust/src/coordinator/failure.rs`:

- The `ProviderModelPresence` enum loses the `AbsentAuthoritative` variant.
- The `provider_model_presence` field on the failure observation is removed,
  along with its default at `:167`, because its only read is the comparison
  that selects the terminal effect.
- The `model_effect` selection at `:551-557` collapses to the quarantine arm.
  The live model-absence behaviour is therefore the bounded quarantine, which
  is what the code already does in every reachable case.

`rust/src/routing/router.rs:645-654`: the `disable_model` call no longer
computes a terminal flag. `disable_model`'s `terminal` parameter
(`rust/src/health/health_manager.rs:468-474`) is removed if no other caller
passes it, along with the `terminal || duration.is_none()` condition at `:483`
and the terminal-withdrawal comment at `:486-489`.

`rust/src/health/health_manager.rs`: the in-memory `terminal_models` set
(`:44`, `:68`, `:89`, `:179`) and its eligibility checks (`:99`, `:222`,
`:340`) are removed, along with the removal sites (`:512`, `:536`), because
nothing populates the set once the producer is gone. That set is the
HealthManager's own mechanism: it is populated only by `disable_model` and is
never hydrated from a durable row. It is not the enforcement that reads the
retained durable terminal vocabulary — that enforcement is the quarantine map's
terminal state together with the routing eligibility gate that consults it, and
it stays.

`rust/src/operations/status.rs`: the `has_model_quarantine` projection reads
`!snapshot.terminal_models.is_empty() || !snapshot.disabled_models.is_empty()`
and reads `!snapshot.disabled_models.is_empty()` after the change. The two
agree in every reachable state, because `disable_model` inserts into
`disabled_models` in both of its branches and `terminal_models` is only ever
inserted as a subset of it, so the published boolean does not change.

Because the set is removed, the HealthManager no longer expresses "a terminal
withdrawal that a later timed disable must not clear". That capability had no
producer and no reachable state, and the durable terminal withdrawal remains
enforced by the retained quarantine gate, which the success path still lifts.

### 6.3 The retained vocabulary is annotated, not removed

`rust/src/health/quarantine.rs`:

- `QuarantineState::TerminalWithdrawn`, `set_terminal_withdrawn`,
  `EvidenceProvenance::ProviderCatalog`, and
  `EvidenceProvenance::MigrationLegacy` stay.
- Each gains a source comment recording that the runtime has no producer for
  it, that it is retained so an existing durable row keeps being parsed and
  enforced, and that reintroducing a producer requires a milestone that first
  makes the success path's clearing durable.

### 6.4 The coverage moves onto the live path

`rust/tests/health.rs`: the applier test is removed together with its
imports (`:19-20`). The assertions it made are re-established against the live
path in `rust/tests/durable_health_effects.rs`:

- the durable row records the live reason and the scope's own escalation rung;
- a model-scoped durable row disables the model without cooling the account
  down;
- the live model-absence path produces the bounded quarantine effect and no
  terminal effect.

The test at `rust/tests/health.rs` that exercises the quarantine's terminal
state directly (`:234`, `:255`, `:281`) is retained, because the terminal
vocabulary is retained.

The HealthManager-level test that asserts a terminal withdrawal survives a
later timed disable (`rust/tests/health.rs:136`) is removed together with the
capability it asserts. The retained enforcement is asserted instead: a durable
quarantine row carrying the terminal state is still parsed and still enforced
by the quarantine gate, and the success path still lifts it.

### 6.5 The deep dives are corrected

`architecture/deep-dive-health.md:70-79` and `architecture/deep-dive-retry.md:122`
no longer name the parallel applier as the effect owner. They name the live
path.

## 7. Ordered work packages

### Work package A — Remove the parallel applier and its public re-export

Files: `rust/src/health/effects.rs` (deleted),
`rust/src/health/mod.rs`, `rust/tests/health.rs`.

- Delete `rust/src/health/effects.rs`.
- Remove the `mod effects;` declaration and the `pub use effects::{…}` line.
- Remove the applier test and the `HealthEffect`, `HealthEffectApplier`, and
  `HealthEffectOutcome` imports from `rust/tests/health.rs`.
- Leave the quarantine terminal-state tests in that file.

Acceptance:

- The library crate compiles with the three public names gone.
- `rust/tests/health.rs` compiles and its remaining tests pass.

### Work package B — Remove the unreachable terminal-withdrawal producer

Files: `rust/src/coordinator/failure.rs`, `rust/src/routing/router.rs`,
`rust/src/health/health_manager.rs`, `rust/src/health/quarantine.rs`.

- Remove the `AbsentAuthoritative` variant, the `provider_model_presence`
  field, and its default.
- Collapse the `model_effect` selection to the quarantine arm.
- Remove the unreachable terminal selection in the router and the `terminal`
  parameter of `disable_model`.
- Remove the in-memory `terminal_models` set and its eligibility checks.
- Annotate the retained durable vocabulary with a comment recording that it
  has no producer.

Acceptance:

- A test asserts that the live model-absence path produces the bounded
  quarantine effect and that no terminal effect is produced.
- A test asserts that a durable quarantine row carrying the terminal state is
  still parsed and still enforced.
- The three test files that set `ProviderModelPresence::Known` are updated to
  the observation shape that remains.

### Work package C — Move the coverage onto the live path

Files: `rust/tests/durable_health_effects.rs`, `rust/tests/health.rs`.

- Re-establish against the live path the assertions the applier test made:
  the durable row's reason, the scope's escalation rung, the model-scoped
  disable, and the absence of a terminal effect.
- Assert that the live path's durable row is not the account rung written into
  a model row, which is the specific defect the applier encoded.

Acceptance:

- The live durable-health path has assertions for its reason, its rung, and
  its scope.
- The coverage removed with the applier test is held by the path that runs.

### Work package D — Correct the deep dives and discharge the deferral

Files: `architecture/deep-dive-health.md`, `architecture/deep-dive-retry.md`,
`plans/subsystems/routing-selection-roadmap.md`.

- Correct both deep dives so that they name the live effect-application path
  and describe the retained terminal vocabulary as producer-less.
- Update the roadmap's M007 deferred-work entry to point at this milestone.
  The closed M007 closure record is not edited.

Acceptance:

- The architecture census and link guards pass.
- No deep dive names the removed module.

## 8. Failure, cancellation, restart, contention semantics

**Restart.** No behaviour changes. The durable terminal vocabulary and its
enforcement are retained, so a row that carries them is honoured exactly as it
is today.

**Failure.** Nothing in this milestone adds a failure path. The removed code
had no production caller, so its failure modes were unreachable.

**Cancellation.** Nothing changes. The removed code was not on any request
path.

**Contention.** Nothing changes. No lock, gate, or shared state is added or
removed.

**Concurrency.** Nothing changes.

**Public surface.** The removed names are a published-surface change, not a
runtime behaviour change, and the complete list is the one in §9. A downstream
consumer that constructed a `HealthEffectApplier` had no production path to
apply it to; the removal surfaces that rather than hiding it. The same holds
for the `terminal_models` fields and for the `AbsentAuthoritative` variant: no
reachable production state depended on them.

## 9. Compatibility and migration

No storage migration. No table, column, index, schema version, state,
provenance, durable row format, endpoint, response shape, configuration key, or
reason string changes. The `model_quarantine` table keeps every state and
provenance value it admits today, and every existing row is parsed and
enforced exactly as it is today.

The published-surface change is the removal of three public re-exports plus
four public members that exist only to carry the retired capability:

- `health::HealthEffect`, `health::HealthEffectApplier`, and
  `health::HealthEffectOutcome` are removed.
- The public `terminal_models` field is removed from `health::AccountHealth`
  and from `health::AccountHealthSnapshot`.
- The public `coordinator::ProviderModelPresence::AbsentAuthoritative` variant
  is removed; `Known` and `Unknown` remain.
- The public `coordinator::FailureObservation::provider_model_presence` field
  is removed.
- `health::HealthManager::disable_model` loses its `terminal: bool` parameter.

`pub mod health` and `pub mod coordinator` remain, and the modules' remaining
exports keep their names and shapes apart from the members listed above. The
`has_model_quarantine` projection in `rust/src/operations/status.rs` is
unchanged in value; see §6.2.

The routing-selection roadmap's M007 deferred-work entry is updated to point
at this milestone. The closed closure records
`plans/closure/routing-selection/007-status.md` and
`plans/closure/request-admission-wire/008-status.md` are immutable and are not
edited.

No credential, prompt, raw body, or cache key enters any affected path.

## 10. Required tests

- A test asserts that the live model-absence path produces the bounded
  quarantine effect and that no terminal effect is produced.
- A test asserts that a durable quarantine row carrying the terminal state is
  still parsed and still enforced, so the retained vocabulary keeps working.
- A durable-effects test asserts that the live durable row records the live
  reason and the scope's own escalation rung.
- A durable-effects test asserts that a model-scoped durable row disables the
  model without cooling the account down.
- A test asserts that the removed public types are absent from the library
  crate's public surface.
- The existing health, durable-effects, and routing tests continue to pass.

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

- `architecture/deep-dive-health.md` — the effect-application owner becomes the
  live path; the retained terminal vocabulary is described as producer-less,
  with the M005 prerequisite named.
- `architecture/deep-dive-retry.md` — the same correction.
- `plans/subsystems/routing-selection-roadmap.md` — the M007 deferred-work
  entry points at this milestone, and the milestone status table gains the row.

No change to `AGENTS.md`, to the release documentation, or to any published
user-facing documentation.

## 13. Acceptance criteria

- The repository contains one effect-application path, and it is the one the
  runtime uses.
- No module, function, type, enum variant, or field in the health subsystem
  is unreachable for want of a producer, except the retained durable terminal
  vocabulary, which is annotated and still enforced.
- The three public types are gone from the library crate's public surface, and
  the removal is recorded in the ADR and in this milestone's closure record.
- A durable quarantine row carrying the terminal state is still parsed and
  still enforced.
- The bounded quarantine, the durable backoff writers, the policy table, the
  reason vocabulary, the escalation rungs, and the routing behaviour of a
  healthy system are unchanged.
- The default and no-default-features workspace suites pass serially, and the
  tooling suite and the linters pass.

## 14. Stop conditions

Stop and report if any of the following is observed:

- The removal would change the behaviour of a durable row that carries the
  terminal state, or the behaviour of a quarantine row that carries the
  `provider_catalog` or `migration_legacy` provenance.
- The bounded quarantine's window, corroboration rule, or auto-clear behaviour
  would change.
- A durable-health assertion would have to be weakened to accommodate the
  removed applier, rather than being re-established against the live path.
- The removal would change the public surface beyond the three names the ADR
  names.
- A guard would need to be weakened, skipped, or given a baseline allowlist to
  pass.
- The ADR-0006 acceptance would have to be reinterpreted for the implementation
  to proceed.

## 15. Closure evidence required

The closure record must contain:

- The requirement-to-evidence matrix mapping every §13 acceptance criterion to
  the test or command that proves it, with the test name and the observed
  result.
- The verification table for every command in §11, with its exit status and
  the pass counts.
- A mutation-check table: at least one deliberate perturbation per corrected
  behaviour — restoring the terminal branch, restoring the applier, or removing
  the live-path assertions — each shown to fail the named test.
- The evidence that the retained durable vocabulary is still parsed and still
  enforced.
- The invariant, security, compatibility, and migration reviews, each stating
  what was checked and what was deliberately left unchanged.
- The unresolved-findings table, which may be empty, listing anything the
  milestone did not address.

## 16. Handoff notes

- The predecessor milestone is
  `plans/implementation/routing-selection/008-durable-health-state-repair-and-authentication-failure-durability.md`,
  which must be closed before this milestone starts.
- The closed records
  `plans/closure/routing-selection/005-status.md`,
  `plans/closure/routing-selection/007-status.md`, and
  `plans/closure/request-admission-wire/008-status.md` are immutable and are
  not edited by this milestone.
- A successor milestone that introduces an authoritative-absence producer must
  first make the success path's clearing durable, as
  `plans/closure/routing-selection/005-status.md:227` requires. That
  prerequisite is recorded in ADR-0006 and is not this milestone's work.
- The registry's routing-selection row and the "Dependency-ready implementation
  plans" or "Blocked work" table carry this milestone's status. One commit
  changes one status.
