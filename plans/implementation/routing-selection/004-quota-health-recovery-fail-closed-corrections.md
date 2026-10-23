# Routing Selection Milestone 004 — Quota, recovery-interval, and health-absence fail-closed corrections

Status: implemented

Repository baseline: `e6822d8f78838b273cee2351d14e5d5dd3ee1e10`

Source roadmap:

- `plans/subsystems/routing-selection-roadmap.md#7-milestones`

Long-term requirements:

- `plans/000-long-term-specification.md` — §2 end-state invariants, §3 ownership boundaries, §5 performance posture
- `plans/001-terminology-and-domain-model.md` — normative language for quota state, account health, and routing exclusions
- `plans/002-long-term-roadmap.md` — sustaining work must preserve a working proxy and current compatibility

Applicable ADRs:

- None required. Every change preserves routing semantics, the selection-lock
  boundary, public scorer interfaces, and the routing-decision schema. Stop
  for architecture review if a correction would require changing routing
  exclusion reason codes or the public scorer contract.

Primary class: invariant

## 1. Objective

Close four fail-open and non-finite-input defects in the selection path that
make routing, quota admission, and recovery timing decide the permissive
outcome when the inputs are unknown or malformed.

## 2. Why this milestone is ready

No dependencies. Every defect is local to the selection and quota modules, has
a deterministic failing case, and needs no new dependency, schema, or
physical target.

## 3. Current implementation evidence

**Quota hard-cap fails open on an unknown account.** In
`rust/src/routing/eligibility.rs` the hard-cap admission check evaluates
`estimator.get_account_quota(&identity.account_name).is_some_and(...)`. When
the estimator has no quota record for the account, the `Option` is `None`,
`is_some_and` yields `false`, and the candidate is not excluded. The account
then routes without any hard-cap comparison. The `LocalQuotaMode::HardCap`
contract in the same file treats a quota record that exceeds its limits as an
exclusion, so an unknown record must not be treated as a passing one.

**A missing health manager reads as healthy.** In `rust/src/routing/router.rs`
the health evaluation uses `self.state.health.as_ref().is_none_or(...)`.
`is_none_or` is true when the manager is absent, so an absent
`HealthManager` satisfies the same predicate as a healthy one. The router's
own construction path always supplies a manager, so the production impact is
limited to construction variants, but the predicate cannot distinguish
"unknown" from "known good".

**A non-finite recovery interval silently becomes zero.** In
`rust/src/routing/router.rs` the recovery interval setter applies
`min_interval_s.max(0.0)`. Rust's `f64::max` returns the non-finite operand
when either operand is non-finite, so `f64::NAN.max(0.0)` is `0.0` and
`f64::INFINITY.max(0.0)` is `f64::INFINITY`. A zero interval means every
request may re-probe a missing account immediately. The same field is
additionally floored at `1.0` at one call site and used unfloored at another,
so the two readers disagree.

**A non-finite observation timestamp wedges the quota window.** In
`rust/src/quota/state.rs` the observation append guard compares
`timestamp >= last_observation_timestamp`. A `NaN` timestamp compares false
against every value, so the observation is dropped while the window's
`last_observation_timestamp` still advances, and the window can stall.

**A stale account-health comment asserts the previous behavior.** The
comment above the health-manager construction in
`rust/src/coordinator/endpoints.rs` states that "the router is otherwise
constructed without a manager and no cooldown, circuit-breaker, or
model-withdrawal effect ever runs in production". The code immediately below
constructs the manager and passes it to the router, so the comment describes a
superseded state. Milestone 005 addresses the durable half of the same
surface; this milestone corrects the comment so it does not mislead.

Relevant existing tests: `rust/tests/routing_domain.rs`,
`rust/tests/routing_domain_d008.rs`, `rust/tests/routing_claims.rs`,
`rust/tests/quota.rs`, `rust/tests/health.rs`.

## 4. Invariants that must not regress

- Routing remains deterministic and load/quota/health based, never cost based.
- `RoutingRouter::selection_lock` remains the atomic async mutex around
  synchronous selection, claim, quota, and fairness mutation.
- No provider, SQLite, filesystem, or network await enters the selection lock.
- Candidate ordering, exclusion reason codes, fairness mode and scope,
  pending-claim accounting, and routing-decision traces remain equivalent for
  every input that was previously handled.
- The public `QuotaFairScorer` methods and exported routing and request types
  remain source compatible.
- `LocalQuotaMode::HardCap` continues to exclude over-limit accounts with the
  existing exclusion reason code.
- No secret, prompt, raw body, or cache key enters routing diagnostics.

## 5. Scope

### In scope

- The hard-cap quota admission decision for an account with no quota record.
- The health-evaluation predicate for an absent `HealthManager`.
- The recovery-interval setter's handling of non-finite and out-of-range
  inputs, and the disagreement between its two readers.
- The quota observation append guard's handling of a non-finite timestamp.
- The stale health comment in `rust/src/coordinator/endpoints.rs`.
- Regression tests and guards for each of the above.

### Explicitly out of scope

- Durable health persistence, quarantine gating, and the backoff escalation
  policy. Those are milestones 005 and 006 in this subsystem.
- Any change to the quota formula, capacity, EWMA, weight, or offset
  semantics.
- Any change to the fairness algorithm, the rotor, or claim accounting.
- Any change to the public scorer API or the routing-decision schema.
- Any change to the model-router affinity cache.
- Any new dependency, concurrent map, or lock architecture change.

## 6. Required production changes

**Routing.** In `rust/src/routing/eligibility.rs`, the hard-cap branch must
distinguish "no quota record" from "quota record within limits". The
correct fail-closed behavior for `LocalQuotaMode::HardCap` is to exclude an
account with no record, using the existing quota exclusion reason code rather
than a new one, so the routing-decision schema does not change. The
`LocalQuotaMode::ScoreOnly` mode must be unaffected: it does not admit on
quota grounds and must continue to route an account with no record.

**Health absence.** In `rust/src/routing/router.rs`, replace the
`is_none_or` health predicate with an explicit match that distinguishes an
absent manager from a present manager whose account is healthy. The absent
case must remain fail-closed in the same direction the existing construction
path implies: an account that cannot be health-evaluated is not treated as
healthy. Because the production construction path always supplies a manager,
this change alters no production selection result; it changes only the
behavior of construction variants that omit one, which the milestone must
cover with a test.

**Recovery interval.** In `rust/src/routing/router.rs`, the setter must reject
non-finite and non-positive intervals rather than coercing them, and the two
readers must apply the same floor. Choose one floor value and apply it in
both places so the readers cannot disagree.

**Quota observation.** In `rust/src/quota/state.rs`, a non-finite timestamp
must not silently advance `last_observation_timestamp`. Either reject the
observation before any state mutation or treat the ordering comparison so a
non-finite value never wins it. The chosen behavior must be observable in a
test and must not change behavior for finite timestamps.

**Comment.** Correct the health comment in
`rust/src/coordinator/endpoints.rs` to describe the current construction.

**Guards.** Add regression coverage that fails on the pre-correction behavior.
A guard must construct the failing input directly rather than relying on a
probabilistic path.

## 7. Ordered work packages

### Work package A — Fail-closed quota hard-cap admission

Intent:

Make `LocalQuotaMode::HardCap` exclude an account that has no quota record
instead of admitting it.

Required changes:

- Rework the hard-cap branch in `rust/src/routing/eligibility.rs` so the
  no-record case is an exclusion.
- Confirm `LocalQuotaMode::ScoreOnly` and any other quota mode keep their
  current behavior for the same input.
- Keep the existing exclusion reason code so routing-decision consumers see no
  schema change.

Acceptance evidence:

- A test asserts that a `HardCap` request against an account with no quota
  record is excluded with the quota exclusion reason code.
- A test asserts that the same account under `ScoreOnly` is still a candidate.
- The pre-existing routing-domain and quota suites pass unchanged.

### Work package B — Distinguish absent health from healthy health

Intent:

Stop an absent `HealthManager` from satisfying the healthy predicate.

Required changes:

- Replace the `is_none_or` health predicate in `rust/src/routing/router.rs`
  with an explicit absent/present match.
- Keep `record_cooldown`, `record_failure`, and `disable_model` behavior for
  a present manager identical.

Acceptance evidence:

- A test constructs a router without a manager and asserts the account is not
  evaluated as healthy.
- A test constructs a router with a manager, registers a healthy account, and
  asserts the account is still selected, proving no production regression.

### Work package C — Recovery-interval input validation and reader agreement

Intent:

Reject non-finite and non-positive recovery intervals and make the two
readers agree.

Required changes:

- Validate the setter input in `rust/src/routing/router.rs` and reject
  non-finite and non-positive values.
- Apply the same floor at both readers.

Acceptance evidence:

- A test asserts a `NaN` interval and an infinite interval are rejected.
- A test asserts both readers observe the same effective interval for the
  same configured value.

### Work package D — Quota observation timestamp guard

Intent:

Stop a non-finite observation timestamp from wedging the quota window.

Required changes:

- Guard the append comparison in `rust/src/quota/state.rs` so a non-finite
  timestamp cannot advance `last_observation_timestamp`.

Acceptance evidence:

- A test asserts that a `NaN` observation leaves the window's usable and that
  a subsequent finite observation is still accepted.

### Work package E — Correction comment and routing-subsystem documentation

Intent:

Remove the comment that asserts a superseded health construction state.

Required changes:

- Correct the comment in `rust/src/coordinator/endpoints.rs`.
- Update `architecture/deep-dive-routing.md` if it repeats the same claim.

Acceptance evidence:

- No file in `rust/src/` or `architecture/` asserts that the production
  router has no health manager.

## 8. Failure, cancellation, restart, contention semantics

- The selection lock is unchanged; no new lock is introduced.
- A rejected recovery interval must not leave the router in a partially
  applied state. Either the previous value is retained or the router keeps an
  explicitly invalid marker; choose one and cover it with a test.
- A rejected quota observation must not mutate any quota counter.
- No cancellation path is added or changed.
- No restart behavior changes: the router is rebuilt per generation and the
  corrections are constructor-time and selection-time only.

## 9. Compatibility and migration

- No migration. No schema change.
- No config key is added, removed, or reclassified, so
  `rust/src/config_reload_policy.rs` needs no change and its schema-coverage
  guard must continue to pass.
- No public Rust API signature changes. The `QuotaFairScorer` methods and the
  exported routing and request types stay source compatible.
- No HTTP or CLI surface change. Routing-decision traces keep their existing
  field set and exclusion reason codes.

## 10. Required tests

- `rust/tests/routing_domain.rs`: quota hard-cap no-record exclusion, quota
  mode comparison, absent-manager health behavior, present-manager healthy
  selection parity.
- `rust/tests/quota.rs`: non-finite observation timestamp behavior.
- `rust/tests/routing_claims.rs`: regression cover for claim and fairness
  behavior when the corrected branches execute.
- A new focused test target may be added under `rust/tests/` if the
  corrections do not fit the existing files. Name it for the behavior, not for
  a milestone number.
- All Rust tests run with `--test-threads=1`.

## 11. Required verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --test routing_domain -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test quota -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test routing_claims -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
```

## 12. Documentation updates

- `architecture/deep-dive-routing.md` — record the corrected hard-cap
  fail-closed rule, the absent-health rule, and the recovery-interval
  validation rule.
- `architecture/deep-dive-health.md` — no ownership change, but confirm the
  health-absence rule is stated once and does not contradict this milestone.
- `AGENTS.md` — no change is required; the routing invariants it already
  states are preserved.

## 13. Acceptance criteria

- An account with no quota record is excluded under `LocalQuotaMode::HardCap`
  and remains a candidate under the score-only mode.
- An absent `HealthManager` is not evaluated as healthy, and a present
  manager with a registered healthy account selects that account exactly as
  before.
- A `NaN` or infinite recovery interval is rejected, and both readers apply
  the same floor.
- A `NaN` quota observation timestamp does not advance the quota window.
- No routing-decision schema, exclusion reason code, public scorer API, or
  config classification changes.
- The default and no-default workspace suites pass.

## 14. Stop conditions

Stop and report rather than improvise when:

- A correction would require a new routing exclusion reason code or a
  routing-decision schema change.
- A correction would require changing the public `QuotaFairScorer` API.
- A correction would require awaiting inside the selection lock.
- The production behavior of a currently healthy account would change as a
  side effect; that indicates the correction is wider than this milestone.
- Evidence contradicts a routing invariant stated in `AGENTS.md`.

## 15. Closure evidence required

- The implementation commits.
- A requirement-to-evidence matrix mapping each work package to the specific
  test and line that proves it.
- The exact verification commands run and their outcomes, labeled honestly
  between local and CI runs.
- Evidence that the pre-correction behavior fails under the new tests.
- A statement that the selection lock, public scorer API, routing-decision
  schema, and config classification are unchanged.
- Any severity-tagged unresolved finding, with a recommendation.

## 16. Handoff notes

The four defects share one ownership boundary and one reviewable surface, so
they belong in one pass. Do not split them across agents; the fail-closed
quota change in particular is easiest to validate against the same routing
fixtures as the health change.

Rust tests must run serially. Strict Clippy with `-D warnings` is an
invariant of this repository. Preserve unrelated user changes in the working
tree.
