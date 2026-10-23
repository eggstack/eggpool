# Routing Selection Milestone 005 — Durable health effect application and quarantine gating

Status: implemented

Repository baseline: `e6822d8f78838b273cee2351d14e5d5dd3ee1e10`

Source roadmap:

- `plans/subsystems/routing-selection-roadmap.md#7-milestones`

Long-term requirements:

- `plans/000-long-term-specification.md` — §2 end-state invariants, §3 ownership boundaries
- `plans/001-terminology-and-domain-model.md` — normative language for account health, quarantine state, and evidence provenance
- `plans/002-long-term-roadmap.md` — sustaining work must preserve a working proxy and current compatibility

Applicable ADRs:

- None required. The durable tables already exist under the migration chain
  and the change adds no new protocol, dependency, or public HTTP contract.
- Stop for architecture review if the implementation requires a new storage
  topology, a change to the migration chain, or a change to the routing
  exclusion reason code set.

Primary class: capability

## 1. Objective

Make the durable health subsystem a live part of production routing: hydrate
persisted account backoffs and model quarantine entries into the running
generation, persist the effects that the routing path applies, and let the
quarantine gate that already exists in the eligibility path actually execute.

## 2. Why this milestone is ready

No hard dependencies. Every piece of machinery already exists, is already
tested in isolation, and needs a production consumer rather than new
behavior. Milestone 004 is independent and may land first or in parallel;
if 004 has not landed, this milestone must not depend on its outcomes.

## 3. Current implementation evidence

The durable health subsystem is implemented, documented, and inert.

**The repositories are never instantiated in production.**
`rust/src/health/repository.rs` provides `AccountBackoffRepository` with
`new`, `list_all`, `hydrate_into`, `list_active`, `upsert`, `clear_success`,
`clear_authentication`, and `expire_old`, and `ModelQuarantineRepository`
with the equivalent entry-oriented surface. At baseline neither type is
constructed anywhere under `rust/src/`. The only construction sites are in
`rust/tests/health.rs`.

**Hydration exists but is never called.** `rust/src/health/health_manager.rs`
provides `hydrate_backoffs`, which takes persisted backoff records, an
account-id-to-name map, and a wall-clock instant, and anchors each deadline in
the process-local monotonic domain. It has no production caller. The
repository-side `hydrate_into` methods likewise have no production caller.

**The effect applier is test-only.** `rust/src/health/effects.rs` provides
`HealthEffect`, the `HealthEffect::account` and `HealthEffect::model`
builders, and `HealthEffectApplier::apply`, which applies the narrowest safe
effect and persists the result. The only construction site of
`HealthEffectApplier` at baseline is `rust/tests/health.rs`.

**The quarantine gate is unreachable.** `rust/src/routing/eligibility.rs`
consults `quarantine.is_model_quarantined_for(...)` inside a
`if let Some(quarantine) = quarantine` block. The router's default
construction sets the quarantine to absent, and `RoutingRouter::with_quarantine`
is the only way to attach one. The only callers of `with_quarantine` at
baseline are `rust/tests/routing_domain_d008.rs` and `rust/tests/routing_claims.rs`.
Because the gate is skipped, the fail-closed partition logic it contains —
including the rule that a missing protocol is its own partition so an
unclassifiable candidate cannot bypass an exact persisted quarantine — never
runs in production.

**What is live today.** `rust/src/routing/router.rs` exposes
`apply_failure_effects`, which calls `HealthManager::disable_model`,
`record_cooldown`, and `record_failure` on the in-memory manager. The
`HealthManager` itself is constructed at `rust/src/coordinator/endpoints.rs`
and passed to the router at the same file's router construction. So model
disablement and account cooldowns work, but only in process memory: they are
lost on restart and are not visible to any other reader.

**The routing wiring point.** The router is constructed per generation inside
`rust/src/coordinator/endpoints.rs`'s
`build_inference_state_with_shared_and_accounts`, which is called from
`rust/src/runtime_lifecycle/generation.rs` when a generation is built. That
is the correct hydration point, because a generation is rebuilt on reload and
must observe current durable state.

**The documentation asserts the opposite.** `architecture/overview.md`
describes routing as selecting over quarantine and describes `health/` as
applying the narrowest safe effect and persisting restart-safety state.
`architecture/deep-dive-health.md` states that the quarantine predicate gates
routing and that readiness reuses the durable health state through
`rust/src/operations/status.rs::evaluate_readiness`. The readiness function
takes seven scalar counts and reads no health repository, so that statement
is not accurate either.

## 4. Invariants that must not regress

- Routing remains deterministic and load/quota/health based, never cost based.
- `RoutingRouter::selection_lock` remains the atomic async mutex around
  synchronous selection, claim, quota, and fairness mutation. No database
  await may enter the selection lock.
- A quarantined model is excluded before selection, and an unclassifiable
  candidate cannot bypass an exact persisted quarantine.
- The narrowest safe effect is applied: a pair-scoped quarantine is preferred
  over an account-scoped breaker whenever the evidence supports it.
- Health effects remain deterministic for the same failure input.
- No secret, prompt, raw body, or cache key enters health state, quarantine
  entries, or health diagnostics.
- The routing-decision schema and exclusion reason codes do not change.
- The public `QuotaFairScorer` methods and exported routing and request types
  remain source compatible.
- No new dependency, concurrent map, or lock architecture change.

## 5. Scope

### In scope

- Constructing the durable health repositories in the generation build path.
- Hydrating persisted account backoffs and model quarantine entries into the
  running generation's health state.
- Persisting the account backoff and model quarantine effects that the
  routing path applies.
- Attaching the hydrated quarantine to the router so the existing
  eligibility gate executes.
- Expiring stale durable rows on the same schedule that already owns other
  bounded maintenance.
- Recording the durable health state in the operator readiness and status
  projection so the readiness claim in `architecture/deep-dive-health.md`
  becomes true, or removing that claim if the work is deferred.
- Correcting `architecture/overview.md` and `architecture/deep-dive-health.md`
  to describe the shipped behavior.

### Explicitly out of scope

- The backoff escalation and jitter policy. Milestone 006 adopts the existing
  `compute_backoff_seconds` policy in the live path and unifies the health
  reason vocabulary.
- Any change to the quota formula, capacity, EWMA, weight, or offset.
- Any change to the fairness algorithm or the claim book.
- Any new table, column, index, or migration. The durable tables already
  exist under the current migration chain.
- Any change to the public HTTP status schema or to the embedded dashboard
  assets.
- Any new provider-facing protocol or dependency.
- Any change to the model-router affinity cache.

## 6. Required production changes

**Repositories.** Construct `AccountBackoffRepository` and
`ModelQuarantineRepository` against the generation's database handle where
the health state is assembled. The repositories must not be constructed per
request.

**Hydration.** At generation build time, load active backoff records and
active quarantine entries and apply them to the generation's health state
before the router becomes reachable. The existing `hydrate_backoffs` and
`ModelQuarantine::hydrate_entry` surfaces are the intended mechanisms. The
hydration must be bounded and must not fail generation publication because of
one unknown account row; the existing skip-and-warn behavior for a stale
account id is the intended model.

**Quarantine attachment.** Attach the hydrated quarantine to the router
through the existing `with_quarantine` constructor so the eligibility gate
executes. The gate's own fail-closed partition rules then apply without
modification.

**Effect persistence.** The routing path's `apply_failure_effects` currently
applies effects to the in-memory manager only. Persist the corresponding
durable rows through the existing repository upsert surface so a restart
preserves the backoff and the quarantine. The persistence must happen outside
the selection lock, on the existing bounded maintenance or finalization
path, never inside the selection critical section.

**Expiry.** Run the existing `expire_old` surfaces on the schedule that
already owns bounded database maintenance, so a stale row cannot suppress
routing indefinitely.

**Operator visibility.** Either extend the readiness and status projection to
report durable health state, or record explicitly in the deep dive that
readiness observes in-memory health only. The deep dive must not continue to
claim a readiness dependency that does not exist.

**Documentation.** Correct `architecture/overview.md` and
`architecture/deep-dive-health.md` to describe the shipped behavior. If any
part of the durable design is deliberately not wired, say so in the same
document rather than leaving the claim in place.

## 7. Ordered work packages

### Work package A — Durable repositories and generation-time hydration

Intent:

Give the running generation the durable health state it should have had.

Required changes:

- Construct the health repositories in the generation build path.
- Load active backoff records and active quarantine entries and apply them
  before the generation is published.
- Keep hydration bounded and skip-and-warn on unknown account ids.

Acceptance evidence:

- A test builds a generation against a database seeded with an active
  backoff and an active quarantine entry and asserts both are visible to the
  generation's health state before any request is served.
- A test asserts that a stale row naming an unknown account does not fail
  generation publication.

### Work package B — Quarantine gate activation

Intent:

Make the existing eligibility quarantine gate execute.

Required changes:

- Attach the hydrated quarantine to the router through
  `RoutingRouter::with_quarantine`.
- Change no gate logic; the existing partition and fail-closed rules apply as
  written.

Acceptance evidence:

- A test asserts a request for an exactly quarantined model is excluded with
  the existing quarantine exclusion reason code.
- A test asserts a candidate whose upstream protocol is unknown does not
  bypass an exact persisted quarantine.
- A test asserts a non-quarantined model on the same provider and account is
  still selected.

### Work package C — Durable effect persistence

Intent:

Persist the backoff and quarantine effects the routing path already applies
in memory.

Required changes:

- Persist the account backoff and model quarantine effect alongside the
  existing in-memory application.
- Perform the write outside the selection lock.

Acceptance evidence:

- A test drives a provider failure through the routing path and asserts the
  corresponding durable row exists afterwards.
- A test asserts the selection lock is not held across the write, using the
  observable fixture boundary rather than a timing assertion.

### Work package D — Bounded expiry of stale durable rows

Intent:

Keep the durable tables from accumulating rows that suppress routing forever.

Required changes:

- Run the existing expiry surfaces on the bounded maintenance schedule.

Acceptance evidence:

- A test asserts an expired row no longer gates routing after expiry runs.

### Work package E — Operator visibility and documentation correction

Intent:

Make the documentation match the shipped behavior in both directions.

Required changes:

- Extend the readiness and status projection to report durable health state,
  or record the deliberate omission.
- Correct `architecture/overview.md` and `architecture/deep-dive-health.md`.

Acceptance evidence:

- No file under `architecture/` asserts that a subsystem is live when it is
  not, and no file asserts a readiness dependency that does not exist.
- The readiness projection test matches the documented contract.

## 8. Failure, cancellation, restart, contention semantics

- A generation must not become reachable with partially hydrated health state.
  Hydration completes before publication, and a hydration failure that cannot
  be bounded fails generation publication closed.
- A restart must observe the persisted backoff and quarantine. This is the
  capability the milestone delivers.
- A reload rebuilds the generation and therefore re-hydrates. A reload must
  not clear a live quarantine as a side effect of hydration.
- Two concurrent generations must not race on the same durable row. The
  repositories' upsert semantics must remain the single writer.
- Cancellation of a request must not leave a durable effect half-written.
  The write is idempotent and bounded.
- Expiry must not delete a row that a concurrent write just refreshed.

## 9. Compatibility and migration

- No migration. The durable tables already exist under the current chain, and
  this milestone adds no column, index, or table.
- No config key is added, removed, or reclassified.
- No public HTTP or CLI schema change, except for the optional readiness and
  status projection fields, which must be additive.
- The routing-decision schema and exclusion reason codes are unchanged. The
  existing quarantine reason code simply becomes reachable.
- Existing accounts, providers, and generated configs are unaffected.

## 10. Required tests

- `rust/tests/health.rs`: hydration, persistence, and expiry coverage.
- `rust/tests/routing_domain_d008.rs`: quarantine gate coverage now exercised
  against a production-constructed router rather than only a test-only
  constructor.
- `rust/tests/routing_domain.rs`: regression cover that a non-quarantined
  account is unaffected by the newly live gate.
- `rust/tests/routing_claims.rs`: claim and fairness regression cover.
- A restart or recovery test that rebuilds the generation against the same
  database and asserts the backoff and quarantine survive.
- All Rust tests run with `--test-threads=1`.
- Cancellation-path tests synchronize on an observable fixture boundary or
  invariant under a bounded timeout. Fixed millisecond sleeps and
  yield-count loops are prohibited.

## 11. Required verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --test health -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test routing_domain_d008 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test routing_domain -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
```

## 12. Documentation updates

- `architecture/overview.md` — correct the routing selection description and
  the `health/` ownership description so they match the shipped wiring.
- `architecture/deep-dive-health.md` — correct the quarantine gating claim
  and the readiness claim; document where hydration happens and what happens
  on a hydration failure.
- `architecture/deep-dive-runtime.md` — record the generation-time hydration
  step if the runtime deep dive enumerates generation construction.
- `docs/api-reference.md` — only if the readiness or status projection gains
  fields.

## 13. Acceptance criteria

- A generation built against a database containing an active backoff and an
  active quarantine entry serves requests with both effects applied before
  the first request is accepted.
- An exactly quarantined model is excluded from selection, and an
  unclassifiable candidate does not bypass an exact persisted quarantine.
- A provider failure that applies a health effect leaves a durable row that
  survives a process restart and still gates or backs off after the restart.
- The durable tables do not grow without bound, and expired rows stop gating.
- No routing-decision schema, exclusion reason code, public scorer API, or
  config classification changes.
- The default and no-default workspace suites pass.

## 14. Stop conditions

Stop and report rather than improvise when:

- The implementation would require a new table, column, index, or migration.
- The implementation would require awaiting inside the selection lock.
- Wiring the quarantine gate changes the selection outcome for a currently
  healthy account; that indicates the persisted state is wrong, not that the
  gate is wrong.
- The durable write cannot be performed outside the selection lock without a
  new concurrency primitive.
- Evidence contradicts a routing or health invariant stated in `AGENTS.md`.

## 15. Closure evidence required

- The implementation commits.
- A requirement-to-evidence matrix mapping each work package to the specific
  test that proves it.
- The exact verification commands run and their outcomes, labeled honestly
  between local and CI runs.
- Evidence that the pre-milestone construction could not produce the
  behavior, so the new coverage is genuinely a regression guard.
- A restart-survival demonstration, recorded as an artifact if a physical
  target is not required.
- The documentation diff, showing the corrected claims.
- Any severity-tagged unresolved finding, with a recommendation.

## 16. Handoff notes

This is the largest milestone in the subsystem. Work package A is the
dependency for B, C, and D; do not begin B before A lands, because attaching
an empty quarantine would make the gate live but meaningless.

The durable tables and repository surfaces already exist and are already
exercised by `rust/tests/health.rs`. The work is consumer wiring plus
documentation correction, not new machinery. Resist adding new tables.

Rust tests must run serially. Strict Clippy with `-D warnings` is an
invariant of this repository. Preserve unrelated user changes in the working
tree.
