# Routing Selection Milestone 006 — Backoff policy adoption and health-reason vocabulary unification

Status: implemented

Repository baseline: `e6822d8f78838b273cee2351d14e5d5dd3ee1e10`

Source roadmap:

- `plans/subsystems/routing-selection-roadmap.md#7-milestones`

Long-term requirements:

- `plans/000-long-term-specification.md` — §2 end-state invariants, §3 ownership boundaries
- `plans/001-terminology-and-domain-model.md` — normative language for failure categories and account health
- `plans/002-long-term-roadmap.md` — sustaining work must preserve a working proxy and current compatibility

Applicable ADRs:

- None required. The policy, the reason vocabulary, and the durable schema all
  exist; this milestone changes which of them the live path uses.
- Stop for architecture review if unifying the vocabulary would change the
  persisted reason strings in the durable health tables, because that is a
  durable on-disk contract.

Primary class: invariant

## 1. Objective

Make the live path use the bounded reason-specific backoff policy that
already exists, and replace the hand-written string vocabulary that maps
failure categories to health reasons with one typed mapping, so the live
backoff durations and the durable reason values cannot diverge from the
policy that defines them.

## 2. Why this milestone is ready

Hard dependency: milestone 005 must be closed. Milestone 005 makes the durable
health path live, and this milestone changes the policy and vocabulary that
path persists. Adopting the policy before the durable path exists would
produce two disagreeing sources of truth.

Interface dependency: the persisted reason strings in the durable health
tables are a stable contract. They must keep their current values.

## 3. Current implementation evidence

**The policy exists and is not used.** `rust/src/health/backoff.rs` provides
`BackoffPolicy` with a base delay, multiplier, cap, jitter fraction, scope
flag, and consecutive-failure ceiling, plus `get_backoff_policy` returning a
reason-specific policy and `compute_backoff_seconds` applying it with a
seeded jitter source. The policy table sets, among others, a 300-second base
for a quota exhaustion, a 60-second base for a rate limit, and a 20-second
base for an upstream server error, each with a doubling multiplier, a bounded
cap, and a 15 percent jitter fraction.

At baseline `get_backoff_policy`, `compute_backoff_seconds`, and
`compute_backoff_seconds_with_rng` have no caller under `rust/src/`. The only
callers are inside `rust/src/health/effects.rs`, whose applier is itself
constructed only in tests, and in `rust/tests/health.rs`.

**The live path inlines the policy's first value.** In
`rust/src/coordinator/failure.rs` the live failure-classification arms assign
`backoff_until` from hardcoded durations: 30 seconds for a connection failure
and for a rate limit without a usable retry hint, 300 seconds for a model
absence and for a model unavailability, and 20 seconds for an upstream server
error. Those are the base delays of the corresponding policies, written as
constants. The escalation behavior the policy defines — doubling, the
consecutive-failure ceiling, the jitter, and the account-versus-model scope
flag — therefore never occurs in production.

**Two failure vocabularies coexist.** `rust/src/coordinator/failure.rs`
defines `FailureCategory` with the variants `BadRequest`, `Authentication`,
`Quota`, `RateLimit`, `Temporary`, `TransientTransport`, `ModelUnavailable`,
`WireRejected`, `Cancelled`, and `Fatal`. `rust/src/health/backoff.rs`
defines `BackoffReason` with the variants `AuthenticationFailed`,
`QuotaExhausted`, `RateLimited`, `ModelUnavailable`, `ConnectTimeout`,
`ConnectionFailure`, `UpstreamServerError`, `ProtocolError`,
`ContextLimitExceeded`, and `Unknown`. The mapping between them is
hand-written: nine `backoff_reason = Some("...")` string literals appear
across the live classification arms, one per failure site.

**The reverse mapping degrades silently.** `rust/src/routing/router.rs`
reconstructs the reason from the stored string with
`BackoffReason::try_from(value)` and falls back to `Unknown` when the value
does not parse, logging only at debug level. A vocabulary drift would
therefore be visible as a reason change rather than as an error.

**The durable clamp contradicts the policy cap.** In
`rust/src/health/repository.rs` the persisted backoff deadline is clamped to
30 minutes regardless of the policy's own cap. This is latent while the
durable path is inert, and becomes a live contradiction once milestone 005
lands.

## 4. Invariants that must not regress

- Routing remains deterministic and load/quota/health based, never cost based.
- The persisted reason strings in the durable health tables keep their current
  values. A row written before this milestone must remain readable and must
  still map to the same effect.
- A reason whose policy has a zero base delay and a zero ceiling, such as an
  authentication failure, keeps its current effect of no timed backoff.
- `RoutingRouter::selection_lock` remains the atomic async mutex around
  synchronous selection, claim, quota, and fairness mutation. No database
  await may enter it.
- The routing-decision schema, exclusion reason codes, and public
  `QuotaFairScorer` API remain unchanged.
- No secret, prompt, raw body, or cache key enters health state or diagnostics.
- No new dependency, concurrent map, or lock architecture change.

## 5. Scope

### In scope

- The live backoff duration computation in `rust/src/coordinator/failure.rs`.
- The mapping from a classified failure to a health reason.
- The reverse mapping from a stored reason string to a health reason.
- The durable backoff deadline clamp in `rust/src/health/repository.rs`.
- Regression tests and guards for each of the above.

### Explicitly out of scope

- Durable health wiring and quarantine gating. That is milestone 005.
- Quota fail-closed corrections, the recovery interval, and the health
  absence predicate. That is milestone 004.
- Any change to the retry decision itself — how many attempts are made, on
  which scope, or with which retry-after hint. Only the resulting backoff
  duration and reason are in scope.
- Any change to the policy table's values. The policy is adopted as written.
- Any change to the persisted reason strings.
- Any change to the public HTTP status schema.

## 6. Required production changes

**Live policy adoption.** The live failure-classification path must obtain its
backoff duration from the existing policy rather than from an inline
constant. The policy application must be bounded by the same cap the policy
declares, and the retry-after hint path that already clamps against
`max_retry_after` must remain the ceiling for an upstream-supplied hint.

**Typed reason mapping.** Replace the nine hand-written reason string
literals with one typed mapping from the classified failure to a
`BackoffReason`. The mapping must be total: every `FailureCategory` that can
reach the backoff assignment must map to a reason, and the unmapped cases
must map to `BackoffReason::Unknown` rather than to a string that later
fails to parse.

**Reverse mapping robustness.** The reconstruction of a reason from a stored
string must not silently become `Unknown` for a value the current writer can
produce. Either the reverse mapping is total over the writer's value set, or
the fallback is recorded at a level that an operator can observe.

**Durable clamp.** The persisted backoff deadline clamp in
`rust/src/health/repository.rs` must be consistent with the policy cap so a
policy-derived deadline is not silently shortened on write.

**Guard.** Add a coverage guard that asserts the writer's reason value set and
the reader's accepted value set are the same set, so the two cannot drift
again.

## 7. Ordered work packages

### Work package A — Typed failure-to-reason mapping

Intent:

Replace the hand-written reason strings with one typed mapping.

Required changes:

- Introduce one mapping from the classified failure to a `BackoffReason` in
  `rust/src/coordinator/failure.rs`.
- Remove the nine string literals.
- Keep the persisted string values byte-identical to the current ones.

Acceptance evidence:

- A test asserts each classified failure maps to the reason string the
  current implementation writes, compared against the pre-change literals.
- A test asserts that every `FailureCategory` reaching the backoff assignment
  maps to a reason with no unmapped arm.

### Work package B — Live policy application

Intent:

Derive the live backoff duration from the existing policy.

Required changes:

- Replace the hardcoded durations in `rust/src/coordinator/failure.rs` with
  the policy's computed value.
- Keep the upstream retry-after hint clamped against the configured maximum,
  as the existing code already does.

Acceptance evidence:

- A test asserts the first failure for a quota exhaustion produces the
  policy's base delay rather than an inline constant, and that a repeated
  failure produces a different, bounded value under the policy's escalation
  rule.
- A test asserts the computed duration never exceeds the policy cap.

### Work package C — Reverse mapping and durable clamp

Intent:

Make the stored-reason reconstruction and the durable write consistent with
the policy.

Required changes:

- Ensure the reverse mapping covers every value the writer can produce.
- Align the durable deadline clamp with the policy cap.

Acceptance evidence:

- A test round-trips every reason the writer can produce through the stored
  string and asserts the reconstructed reason equals the original.
- A test asserts a policy-derived deadline survives a durable write unchanged
  when it is within the cap.

### Work package D — Vocabulary coverage guard

Intent:

Stop the writer and reader vocabularies from drifting again.

Required changes:

- Add a guard that compares the writer's reason value set against the
  reader's accepted value set.

Acceptance evidence:

- The guard fails when a reason is removed from the writer's set without a
  corresponding reader change.

## 8. Failure, cancellation, restart, contention semantics

- A failure whose category has no specific policy must still produce a
  bounded, non-zero-or-explicitly-zero backoff according to the policy's
  own definition for `Unknown`, not an unbounded duration.
- Escalation state is per account and per model. A concurrent failure on the
  same account must not double-count in a way that exceeds the policy's
  consecutive-failure ceiling.
- A restart must not reset a persisted backoff earlier than its persisted
  deadline.
- No cancellation path is added or changed.
- The selection lock is unchanged; no await is added inside it.

## 9. Compatibility and migration

- No migration. The durable reason strings keep their current values, so
  existing rows remain valid.
- No config key is added, removed, or reclassified.
- No public HTTP or CLI schema change.
- No public Rust API break: the exported failure and reason types keep their
  variants and their string representations.

## 10. Required tests

- `rust/tests/health.rs`: policy application, escalation, cap, and
  round-trip coverage.
- `rust/tests/routing_domain.rs`: regression cover that a rate-limited
  failure still backs the account off, and that an authentication failure
  still produces no timed backoff.
- `rust/tests/routing_claims.rs`: claim and fairness regression cover.
- A focused test target may be added under `rust/tests/` for the vocabulary
  guard. Name it for the behavior, not for a milestone number.
- All Rust tests run with `--test-threads=1`.

## 11. Required verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --test health -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test routing_domain -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
```

## 12. Documentation updates

- `architecture/deep-dive-health.md` — record the adopted policy, the typed
  reason vocabulary, and the persisted string values.
- `architecture/deep-dive-routing.md` — record that the live backoff duration
  is policy-derived.
- `docs/api-reference.md` — only if a status field exposes a reason value
  that changes.

## 13. Acceptance criteria

- The live backoff duration for every classifiable failure is derived from
  the existing policy, not from an inline constant.
- Every reason string the writer can produce is reconstructed by the reader
  as the same reason.
- The persisted reason strings are byte-identical to the pre-change values.
- A persisted backoff deadline within the policy cap is not shortened by the
  durable write.
- A repeated failure escalates under the policy and never exceeds the cap.
- No routing-decision schema, exclusion reason code, public scorer API, or
  config classification changes.
- The default and no-default workspace suites pass.

## 14. Stop conditions

Stop and report rather than improvise when:

- Adopting the policy would change a persisted reason string.
- The policy table itself needs a value change; that is a separate decision
  and must not be smuggled into this milestone.
- The typed mapping would require a new `FailureCategory` or
  `BackoffReason` variant; that changes the durable vocabulary and needs its
  own plan.
- Escalation state would have to move into the selection lock.
- Evidence contradicts an invariant stated in `AGENTS.md`.

## 15. Closure evidence required

- The implementation commits.
- A requirement-to-evidence matrix mapping each work package to the specific
  test that proves it.
- The exact verification commands run and their outcomes, labeled honestly
  between local and CI runs.
- A before-and-after table of the live backoff duration per failure class,
  showing the inline constants and the policy-derived values.
- Evidence that the persisted reason strings are unchanged, compared against
  the pre-change literal set.
- Any severity-tagged unresolved finding, with a recommendation.

## 16. Handoff notes

Do not start this milestone before milestone 005 closes. The durable path is
what makes the policy and the vocabulary observable; landing them out of
order produces a live path and a durable path that disagree.

The policy table and the reason vocabulary already exist and are already
covered by `rust/tests/health.rs`. This milestone changes which of them the
production path uses. Do not add a second policy or a second vocabulary.

Rust tests must run serially. Strict Clippy with `-D warnings` is an
invariant of this repository. Preserve unrelated user changes in the working
tree.
