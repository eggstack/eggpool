# ADR-0006: Authoritative terminal model withdrawal ownership

Status: proposed

Date: 2026-10-25

Decision owners: project maintainers

Related specification sections:

- `plans/000-long-term-specification.md` — §2 end-state invariants, §3 ownership boundaries
- `plans/001-terminology-and-domain-model.md` — normative language for accounts, model absence, and quarantine state
- `plans/002-long-term-roadmap.md` — sustaining work must preserve a working proxy and current compatibility

Affected subsystem roadmaps:

- `plans/subsystems/routing-selection-roadmap.md`

## Context

EggPool's health subsystem has two model-absence behaviours: a bounded quarantine
and a terminal withdrawal. The terminal withdrawal has a complete consumer half
and no producer at all.

**The consumer half is built and correct.** `QuarantineState::TerminalWithdrawn`
(`rust/src/health/quarantine.rs:59`) is part of the durable vocabulary of the
`model_quarantine` table. `quarantine.set_terminal_withdrawn`
(`rust/src/health/quarantine.rs:400`) writes it. `entry_from_row`
(`rust/src/health/quarantine.rs:592`) parses it. The routing eligibility gate
consults it in three places (`rust/src/health/health_manager.rs:99`, `:222`,
`:340`), backed by the in-memory `terminal_models` set
(`rust/src/health/health_manager.rs:484`) that `disable_model` populates when
called with the terminal flag.

**The producer half does not exist.** `ProviderModelPresence::AbsentAuthoritative`
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

That variant is **never constructed anywhere in the repository**. The only
producer of `provider_model_presence` is the observation constructor at
`rust/src/coordinator/failure.rs:167`, which sets `Unknown`. The only other
assignments are to `Known`, and all three are in test files
(`rust/tests/coordinator_c011.rs:1377`, `rust/tests/coordinator_finalization.rs:405`,
`rust/tests/coordinator_c013.rs:126`). The field has exactly one read — the
comparison above — and no setter.

The consequences chain from there:

- `model_effect` is never `"terminal_withdrawal"`.
- `quarantine_model` is computed as `model_effect == "quarantine"`
  (`rust/src/coordinator/failure.rs:691`), so it is false for a withdrawal.
- `rust/src/routing/router.rs:645-653` therefore never runs for a withdrawal,
  and `rust/src/routing/router.rs:650` — the expression
  `model_effect == "terminal_withdrawal"` that selects the terminal flag — is
  unreachable.
- `disable_model` is never called with the terminal flag, so `terminal_models`
  is never populated, so the three eligibility checks that read it never fire.
- `set_terminal_withdrawn` has **no production caller**. The only call site is
  `rust/src/health/effects.rs:144`, inside the parallel `HealthEffectApplier`
  that has no production caller either.

The same pattern holds for the durable provenance vocabulary:
`EvidenceProvenance::ProviderCatalog` and `EvidenceProvenance::MigrationLegacy`
(`rust/src/health/quarantine.rs:91-95`) are **never constructed** anywhere.

**The durable terminal state was a deliberate exclusion, not a loss.** The
closed M005 record is explicit
(`plans/closure/routing-selection/005-status.md:227`):

> A terminal model withdrawal is mirrored as a bounded observation rather than a
> terminal durable row. … The source plan asks for the terminal state to be
> persisted. Persisting it would let a stale withdrawal outlive the process that
> observed the authoritative absence, and the in-memory success path that lifts
> a terminal withdrawal could never lift the durable row, because
> `record_success` is synchronous and the lifting write would have to be
> awaited. The bounded mirror is the conservative choice and preserves the
> pre-milestone restart behavior exactly. **A follow-up plan must either make
> the success path's clearing durable or add an explicit terminal-withdrawal
> durable state with its own clearing path.** Milestone 006 must not depend on
> the terminal state being durable

`architecture/deep-dive-health.md:135` and `:165` record the same rule: a
terminal withdrawal is mirrored as a bounded observation and is never persisted
as a terminal row.

So the repository's accepted design has two halves: an **in-memory** terminal
state that lifts on the synchronous success path, and a **bounded durable
mirror** that auto-clears. The in-memory half is live in the code and has no
producer. The durable half was deliberately not built.

**The Python-era runtime had a producer.** `src/eggpool/failure/classifier.py:317`
computed `authoritative = obs.source == "provider_catalog"`, and
`src/eggpool/model_info/sources/provider_catalog.py` produced observations with
that source. The Rust runtime has no equivalent source. `rust/src/runtime.rs:1303`
exposes `refresh_model_info_from_catalog` as an **operator command**, not a
runtime task, and the `model_info` table it writes is never read by the failure
path.

**The account catalogue refresh computes the fact and discards it.**
`rust/src/catalog/refresh.rs:346` produces `withdrawn_model_ids`, the diff of
the model ids an authoritative refresh no longer lists. Whether that diff is
acted on is decided at `rust/src/catalog/refresh.rs:449-456`:

```rust
let allow_withdrawals = authoritative
    && self.config.models.catalog_withdrawal_policy != "preserve_until_health";
```

The default policy is `preserve_until_health`
(`rust/src/config.rs:194`), so under the default configuration the
authoritative-absence fact is computed and not used. The configuration
comment (`rust/config.example.toml:83-93`) states the intent:

> Default "preserve_until_health" never de-pools on catalog uncertainty: only
> explicit health/config state (auth failure, quota exhaustion, rate-limit,
> cooldown, circuit open, account disabled) suppresses routing.

The Python-era producer was also the source of a second divergence: the dead
`HealthEffectApplier` (`rust/src/health/effects.rs`) is the only implementation
of `set_terminal_withdrawn` in the repository, and it implements the durable
terminal row that M005 rejected.

## Decision drivers

- Code must not contain a capability that no producer can reach. The repository
  states this directly in its own conventions and in the M007 deferral, which
  offered exactly two dispositions for the parallel applier: delete it, or give
  it a consumer.
- A published API surface must not be removed without a recorded decision.
  `pub mod health` (`rust/src/lib.rs:16`) makes
  `crate::health::{HealthEffect, HealthEffectApplier, HealthEffectOutcome}`
  public API of the library crate.
- The accepted M005 design must not be silently reversed. M005 chose the
  bounded mirror and named the prerequisite for anything more.
- The default configuration's behaviour must not change. A decision that alters
  what `preserve_until_health` suppresses is a configuration-semantics change.
- The correction must be a corrective. Adding an authoritative-absence producer
  is a new capability with new durable semantics, not a fix.
- Every retained piece of durable state must have a reader that keeps working,
  so that an existing row is never reinterpreted or dropped by the correction.

## Considered options

### Option A — Wire the producer

Give the terminal withdrawal a producer, so that an authoritative catalogue
omission terminally withdraws the model for that account while the catalogue's
own support row is preserved.

The producer is available: `withdrawn_model_ids` is already computed by the
authoritative catalogue refresh and already discarded under the default policy.

It is rejected as a corrective for two reasons.

First, it changes what the default configuration suppresses. The configuration
comment enumerates what suppresses routing under `preserve_until_health` —
authentication failure, quota exhaustion, rate-limit, cooldown, circuit open,
account disabled — and a terminal model withdrawal is not on that list. Adding
it extends the policy's suppression set and needs its own configuration and
documentation decision, not a defect fix.

Second, and decisively, the durable producer cannot lift its own state. The
catalogue refresh is a periodic background task; it can only write durably. A
durable terminal row could only be lifted by the success path, and
`record_success` is synchronous while the lifting write would have to be
awaited — which is exactly the reason M005 gave for refusing to persist a
terminal state in the first place. Option A therefore requires the M005
prerequisite to be solved first: *"make the success path's clearing durable"*.
That is a durable-clearing redesign, not a corrective.

### Option B — Retire the unreachable producer path and the parallel applier; retain the durable reader

Remove the code that no producer can reach:

- the parallel `HealthEffectApplier` module and its public re-export;
- the `terminal_withdrawal` model effect and the `AbsentAuthoritative` variant
  that only it depended on, together with the `provider_model_presence` field
  that exists solely to carry that variant;
- the unreachable terminal selection at `rust/src/routing/router.rs:650` and the
  in-memory `terminal_models` set and its three eligibility checks.

Retain the durable terminal-withdrawal vocabulary —
`QuarantineState::TerminalWithdrawn`, `set_terminal_withdrawn`,
`EvidenceProvenance::ProviderCatalog`, and `EvidenceProvenance::MigrationLegacy`
— as a reader for rows this runtime does not currently write.

The retained half has no producer today, and that is recorded rather than
hidden. It is not dead code in the same sense as the removed half: it is a
correct reader for a durable state the schema admits, and removing it would
change the behaviour of an existing row. A future milestone that introduces a
producer must first solve the durable-clearing prerequisite M005 named, and
must reintroduce the producer half deliberately.

This is the option that makes the codebase say what it does: the bounded
quarantine is the live model-absence behaviour, and the terminal state is
admitted by the durable vocabulary but not produced.

### Option C — Retire the capability entirely, including its durable vocabulary

Remove `QuarantineState::TerminalWithdrawn`, `set_terminal_withdrawn`, the
`EvidenceProvenance::ProviderCatalog` and `MigrationLegacy` variants, and their
parsing, and add a migration that normalizes any existing rows.

Rejected. `entry_from_row` (`rust/src/health/quarantine.rs:595`) returns an
error for an unparseable state or provenance, and that error propagates through
the quarantine hydration into the generation build, exactly like the legacy
backoff row that ADR-0005 addresses. A retirement that can make an existing
database refuse to start is a worse outcome than a retained reader. The
retained vocabulary costs a few lines; the normalization migration costs a
destructive step on a durable table for no behavioural gain.

### Option D — Leave the capability dormant and document it

Keep everything as it is and record that the producer is absent.

Rejected. The parallel applier is not merely dormant: it asserts a durable
terminal behaviour that M005 explicitly rejected, it diverges from the live
path in twelve other establishable ways, and its test asserts a backoff
duration (`500.0` seconds) that the live policy never produces (`462.5` seconds
with jitter). Leaving it in place keeps a false assurance in the test suite and
keeps two contradictory designs of the same subsystem in one repository.

## Decision

Option B is selected.

- The parallel `HealthEffectApplier` module is removed, together with its
  public re-export from `rust/src/health/mod.rs`. The public API of the library
  crate loses `health::HealthEffect`, `health::HealthEffectApplier`, and
  `health::HealthEffectOutcome`. This is a published-surface change and is
  recorded as such.
- The `terminal_withdrawal` model effect, the `AbsentAuthoritative` variant, the
  `provider_model_presence` field, and the unreachable terminal selection in the
  router are removed. The live model-absence behaviour is the bounded
  quarantine, which is what the code already does in every reachable case.
- The in-memory `terminal_models` set and the eligibility checks that read it
  are removed, because nothing populates them once the producer is gone.
- The durable terminal-withdrawal vocabulary is **retained**: the
  `QuarantineState::TerminalWithdrawn` state, the `set_terminal_withdrawn`
  writer, and the `EvidenceProvenance::ProviderCatalog` and `MigrationLegacy`
  provenances remain, and the in-memory terminal enforcement that reads them is
  therefore also retained. They are the reader for a durable state the schema
  admits; removing them would change the behaviour of an existing row.
- The M005 prerequisite is recorded explicitly: reintroducing a producer
  requires a milestone that first makes the success path's clearing durable, or
  that adds an explicit terminal-withdrawal durable state with its own clearing
  path. That milestone is not this one.
- The architecture deep dives that name the parallel applier as the effect
  owner are corrected, and the M007 deferral is discharged by this milestone
  rather than by editing the closed M007 record.

The deeper contract this records is that a capability whose producer does not
exist must be removed rather than described, and that a durable state may be
retained without a producer only when the durable reader still enforces it.

## Consequences

### Positive

- The repository contains one design of the health subsystem rather than two
  contradictory ones. The parallel applier's durable terminal row, which M005
  rejected on stated grounds, is gone.
- A test that asserted a backoff duration the live policy never produces, and
  that exercised a path with no production caller, is gone. The remaining
  durable-health tests describe behaviour the runtime has.
- The public API of the library crate no longer advertises an effect applier
  that cannot be constructed in production.
- The capability question is answered in writing instead of left implicit in
  unreachable branches.

### Negative

- The public API of the library crate loses three types. A downstream consumer
  of `eggpool::health::HealthEffectApplier` would need to change. The crate is
  the runtime, and the repository's own tooling consumes it only in-tree, but
  the removal is a published-surface change and is not free.
- The retained terminal-withdrawal vocabulary has no producer, so a reader
  without a writer remains in the tree. That is a deliberate, recorded state,
  not an oversight, and it is bounded: the vocabulary is small, it is enforced,
  and it is annotated as producer-less.
- If the maintainers later want the authoritative-absence capability, the
  producer half has to be rebuilt, and the M005 durable-clearing prerequisite
  has to be solved first. That is a roadmap-scale cost, accepted here.

### Neutral or deferred

- The bounded quarantine is unchanged. Its window, its durable mirror, its
  corroboration rule, and its auto-clear on recovery are unchanged.
- The `model_quarantine` table, its schema, its migration history, and its
  durable vocabulary are unchanged. No migration is required by this decision.
- The account catalogue refresh, `catalog_withdrawal_policy`, and the
  `preserve_until_health` default are unchanged. The decision does not extend
  what the default policy suppresses.
- The routing, selection, admission, and account-scoring behaviour of a healthy
  system is unchanged.
- The routing-selection roadmap's M007 deferral of the parallel applier is
  discharged by this milestone. The closed M007 record is not edited.

## Compatibility and migration

No storage migration. No table, column, index, schema version, or durable
vocabulary changes. The `model_quarantine` table keeps every state and
provenance value it admits today, and every existing row is parsed and
enforced exactly as it is today.

The published-surface change is the removal of three public re-exports:
`health::HealthEffect`, `health::HealthEffectApplier`, and
`health::HealthEffectOutcome`. `pub mod health` remains; the module's remaining
exports are unchanged. No endpoint, response shape, configuration key, reason
string, or durable row format changes.

No credential, prompt, raw body, or cache key enters any affected path.

## Security and reliability implications

The change removes a code path, so it cannot weaken a control. The capability it
removes was unreachable, and the behaviour of every reachable case is unchanged:
an authoritative or non-authoritative model absence is handled by the bounded
quarantine, which suppresses routing for a bounded window and auto-clears on
recovery.

The retained durable reader keeps enforcing a terminal withdrawal for any row
that carries one, so an existing database's behaviour is not weakened by the
retirement.

The parallel applier is removed from the test suite, which removes a false
assurance rather than a real one. The live durable-health behaviour is covered
by `rust/tests/durable_health_effects.rs`, which exercises the production
writers. The retirement must not leave the live path uncovered: the milestone
adds the assertions that the live path's durable row carries the live reason and
the scope's own escalation rung, so that the coverage the removed test gave the
applier is given to the path that actually runs.

The one behavioural difference a downstream consumer could observe is the loss
of the three public types. A consumer that constructed a `HealthEffectApplier`
had no production path to apply it to; the removal surfaces that rather than
hiding it.

## Verification

- A test asserts that the live model-absence path produces the bounded
  quarantine effect and that no terminal effect is produced.
- A test asserts that the live durable-health writers record the live reason
  and the scope's own escalation rung, so the coverage removed with the
  parallel applier's test is held by the production path.
- A test asserts that a durable quarantine row carrying the terminal state is
  still parsed and still enforced, so the retained vocabulary keeps working.
- A test asserts that the removed public types are absent from the library
  crate's public surface.
- The routing-selection and request-admission closure records remain
  unedited, and the roadmap's M007 deferral is discharged by the new milestone
  rather than by a rewrite.
- The architecture deep dives that named the parallel applier as the effect
  owner are corrected, and the architecture census guard passes.

## Supersession

None. This record supersedes no accepted decision. It discharges the deferral
recorded in `plans/closure/routing-selection/007-status.md` and
`plans/closure/request-admission-wire/008-status.md`, both of which offered
the two dispositions this decision chooses between; those records are immutable
and are not edited. It also gives effect to the follow-up note in
`plans/closure/routing-selection/005-status.md` by recording that the
prerequisite it named — a durable success path — is unresolved, so the producer
half is not reintroduced by this milestone.
