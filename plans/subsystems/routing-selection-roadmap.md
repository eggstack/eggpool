# Routing Selection Roadmap

Status: active

Long-term references:

- plans/000-long-term-specification.md — §2 end-state invariants, §3 ownership boundaries, §5 performance posture
- plans/002-long-term-roadmap.md — sustaining work must preserve a working proxy and current compatibility
- plans/003-planning-process.md — evidence-gated polish and bounded implementation plans

Related ADRs:

- None required for the current milestone. The planned change is private allocation/ownership cleanup that preserves deterministic routing, fairness, quota, health, selection-lock, and public scorer contracts.
- Stop for architecture review if an optimization requires changing routing semantics, the selection-lock atomicity boundary, public scorer/policy behavior, or a new concurrent-map/dependency.

## 1. Purpose and ownership boundary

This subsystem owns performance work inside the deterministic provider/account selection path:

- rust/src/routing/
- rust/src/quota/
- rust/src/accounts/
- rust/src/health/ as an input only
- rust/src/catalog/ as an input only
- rust/src/model_router.rs for process-owned semantic affinity, as a distinct later milestone

It does not own provider transport, HTTP admission, persistence, retries, or wire adaptation.

The current goal is to remove transient allocations and candidate cloning inside the already-correct synchronous selection transaction while keeping every routing decision, diagnostic trace, quota claim, health effect, and fairness result equivalent.

## 2. Work classification

### Invariants

- Routing remains deterministic and load/quota/health based, never cost based.
- Semantic model selection occurs before provider/account selection and cannot pin an account or bypass health/quota.
- RoutingRouter::selection_lock remains the atomic async mutex around synchronous selection/claim/quota/fairness mutation.
- No provider, SQLite, filesystem, or network await enters the selection lock.
- Candidate ordering, exclusion reason codes, fairness mode/scope, pending-claim accounting, and routing-decision traces remain equivalent.
- Public QuotaFairScorer methods and exported routing/request types remain source compatible.
- Ordered diagnostics remain deterministic; do not replace BTreeMap with HashMap merely as a performance guess.
- No secret/raw body data enters routing diagnostics.

### Capabilities

- M005 makes durable account backoff and model quarantine operator-visible:
  a provider that goes bad or a model that must be withdrawn stays excluded
  across a process restart. This is a user-visible routing capability, not
  infrastructure, because the consumer path exists once M005 lands.
- M001–M003 and M004 planned no user-visible routing capability change.

### Infrastructure

- QuotaEstimator state snapshotting.
- QuotaFairScorer internal scoring core.
- RoutingCandidate/SelectionSnapshot construction.
- Model-router affinity cache, only for a later evidence-gated milestone.

### Polish

- Remove String-keyed temporary maps and candidate clones that Plan 231 identified and intentionally deferred.
- Only consider the affinity-cache exact-LRU O(n) hit cost after the simpler selection cleanup is closed and measured.

## 3. Non-goals

- No selection-lock removal, lock-free claim book, concurrent map, or async work inside selection.
- No fairness algorithm change.
- No quota formula, capacity, EWMA, health, quarantine, provider pinning, or priority change.
- No HashMap substitution for ordered routing facts.
- No public QuotaFairScorer API break.
- No routing-decision schema change.
- No model-router selector protocol/fingerprint change.
- No new dependency.
- No permanent benchmark framework.
- No request-admission or provider-client-pool work in this subsystem milestone.

## 4. Current state

Legacy Plan 231 completed the low-risk routing fixes but explicitly deferred the broader transient scoring-collection cleanup to preserve the deterministic public scorer contract.

The current source still contains that deferred pipeline in rust/src/routing/eligibility.rs:

eligible Vec<RoutingCandidate>
    -> clone account names into Vec<String>
    -> build BTreeMap<String, i64> active
    -> build BTreeMap<String, i64> projected
    -> create empty BTreeMap<String, f64> penalties
    -> QuotaFairScorer::score_accounts
         -> QuotaEstimator::snapshot returns BTreeMap<String, QuotaAccountSnapshot>
         -> lookup + clone each snapshot
    -> build BTreeMap<String, RoutingCandidate>
    -> lookup + clone each candidate back into scored Vec
    -> validate + sort

All of that runs while RoutingRouter::select_and_claim_with_preference holds selection_lock, after taking the claim active snapshot and catalog lock.

The public scorer API is useful to tests/consumers and is not itself the defect. The missing piece is a private ordered scoring path for the router.

A separate current finding exists in rust/src/model_router.rs: exact affinity hits use VecDeque::retain while holding the affinity Mutex, making LRU touch O(cache size) at the 4096-entry cap. That has not yet been shown to dominate a realistic workload and requires a more invasive exact-LRU data-structure decision, so it is deferred behind evidence rather than bundled into M001.

The roadmap reopened on repository evidence gathered at baseline `e6822d8f78`. Four selection-path defects were found that decide the permissive outcome on unknown or malformed input, and the durable half of the health subsystem was found to be implemented, documented, and inert in production. The durable health repositories and the effect applier under `rust/src/health/` have no production constructor, the quarantine store is never attached to a router, the quarantine gate in `rust/src/routing/eligibility.rs` is therefore skipped, the backoff escalation policy has no production caller, and the live backoff durations are inline constants. M004–M006 address this. The historical M001–M003 records remain closed and immutable.

## 5. Target architecture

Routing selection keeps the same ownership and locks but uses an ordered internal score-input path:

- eligible candidates remain one Vec in deterministic account iteration order;
- estimator state is snapshotted once under one estimator lock into an ordered result aligned with borrowed account names/candidate indices;
- projected_tokens remains one request scalar rather than a per-account map;
- active request counts are read directly from the existing active snapshot;
- the zero health-penalty case does not allocate an empty map on the hot path;
- scores are produced in the same order as candidates;
- candidate ownership is moved/zip-associated with scores rather than inserted into and cloned back out of a BTreeMap;
- the final sort and all trace/fairness semantics remain unchanged.

Public score_accounts/rank_accounts/near_ties remain available and delegate to the same score-one semantics where practical.

## 6. Dependency graph

- Legacy Plan 231 → historical evidence and explicit deferred-work source for M001.
- Current quota/routing public contracts → stable interface dependency; already satisfied.
- M001 has no external hard dependency.
- M002 affinity LRU qualification is closed; the representative Pi 5 workload justifies the exact bounded M003 follow-up.
- Persistence work is independent; no cross-subsystem dependency.
- M004 had no hard dependency; it was a local fail-closed correction set and is now closed.
- M005 had no hard dependency; the durable tables already existed and only a consumer path was missing. It is now closed, and the durable path persists the same policy the live path applies.
- M006 had a hard dependency on M005: the policy and vocabulary it adopts must be the ones the durable path persists. The M005 closure satisfied it, and M006 is now closed.
- M007 had a hard dependency on M005 and M006 closed, and is now closed.
- M008 required the acceptance of
  `plans/adrs/ADR-0005-durable-health-state-repair-and-authentication-failure-durability.md`,
  which the maintainers accepted on 2026-10-25, and on no other milestone.
- M009 required the acceptance of
  `plans/adrs/ADR-0006-authoritative-terminal-model-withdrawal-ownership.md`,
  which the maintainers accepted on 2026-10-25, and on M008 closed. M008 is a
  hard dependency because both milestones edit `rust/src/health/` and
  `rust/src/routing/router.rs`.
- No plan in the dashboard, request admission and wire, operations CLI, deployment and packaging, or planning governance subsystems depends on this roadmap's milestones.

## 7. Milestones

### Milestone 001 — Ordered quota-scoring and candidate-allocation cleanup

Class: polish

Objective:

Remove the deferred transient String/BTreeMap/candidate-clone pipeline from provider/account selection without changing public scorer APIs or any routing result.

Dependencies:

- None beyond the current stable routing/quota interfaces.

Deliverable boundary:

- Private ordered quota snapshot/scoring helper(s).
- Router/eligibility uses borrowed account names/indices and request scalar projected_tokens.
- Existing public score_accounts behavior retained.
- Candidate Vec is moved into final scored results without by-name reindex/clone.
- Selection lock and final sort/fairness remain unchanged.

User or operator value:

Lower per-request CPU/allocation under the serialized selection critical section, especially as configured account count grows.

Exit conditions:

- Structural temporary-map/candidate-clone removal is recorded.
- Exact route/trace/fairness/quota parity tests pass for representative 1/4/16/128-account fixtures.
- No public/API/config/dependency/concurrency change.
- Full default/no-default qualification passes.

Deferred work:

- Exact-LRU affinity redesign.
- Fairness data-structure redesign beyond obvious move-only cleanup.
- Lock architecture changes.

### Milestone 002 — Semantic-affinity exact-LRU cost qualification, evidence-gated

Class: polish

Objective:

Determine whether ModelRouterAffinity's O(n) VecDeque::retain touch on cache hits is material at realistic sticky-alias cache sizes. Only if it is material, design an exact, bounded, dependency-free replacement that preserves TTL, eviction order, single-flight, and stats.

Dependencies:

- Hard: M001 closed, so ordinary provider/account allocation cost is no longer mixed into routing measurements.
- Evidence: representative affinity workload at 64/512/4096 live entries.

Deliverable boundary:

Closed with Pi 5 evidence: the 4,096-entry cache-hit p95 was 92.056 μs. See `plans/closure/routing-selection/002-affinity-lru-qualification.md`. The evidence authorizes M003.

Exit conditions:

M003 implemented and qualified a bounded exact indexed LRU. Approximate eviction and unbounded stale-node queues were not introduced.

### Milestone 003 — Bounded exact affinity LRU

Class: polish

Objective: replace the O(n) cache-hit recency scan with an exact, bounded, dependency-free indexed LRU while preserving all cache and single-flight semantics.

Dependency: M002's representative Pi 5 workload showed material hit-path cost at the configured 4,096-entry cap.

Deliverable: slot-bearing cache entries and a reusable slot-indexed doubly linked list under the existing mutex; semantic regression coverage; matching Pi 5 before/after release measurements. The implementation plan and closure evidence are `plans/implementation/routing-selection/003-bounded-affinity-exact-lru.md` and `plans/closure/routing-selection/003-status.md`.

Exit conditions: exact eviction/TTL/stats/cancellation behavior, bounded node/free-slot storage, default/no-default workspace and strict Clippy/fmt gates, and the Pi 5 p95 thresholds all pass. **Closed** with implementation `dc6ea713`; 4,096-entry p95 improved about 90× and 64-entry p95 improved about 3.2×.

### Milestone 004 — Quota, recovery-interval, and health-absence fail-closed corrections

Class: invariant

Objective:

Close the selection-path defects that decide the permissive outcome when the
inputs are unknown or malformed: the hard-cap quota admission for an account
with no quota record, the absent-health predicate, the non-finite recovery
interval, and the non-finite quota observation timestamp.

Dependencies:

- None. Each defect is local, deterministic, and needs no schema, dependency,
  or physical target.

Deliverable boundary:

- `LocalQuotaMode::HardCap` excludes an account with no quota record, using
  the existing quota exclusion reason code.
- An absent `HealthManager` is not evaluated as healthy, while a present
  manager with a registered healthy account selects that account exactly as
  before.
- A non-finite or non-positive recovery interval is rejected, and both
  readers of that interval apply the same floor.
- A non-finite quota observation timestamp does not advance the quota window.
- The stale health comment in `rust/src/coordinator/endpoints.rs` is corrected.

User or operator value:

An installation with an unknown quota record, an unreadable health state, or
a misconfigured recovery interval stops admitting traffic it should not
admit, instead of failing open.

Exit conditions:

- Each correction has a regression test that fails against the pre-change
  behavior.
- The routing-decision schema, exclusion reason codes, public scorer API, and
  config classification are unchanged.
- Default and no-default workspace qualification passes.

Deferred work:

- Durable health persistence and quarantine gating (M005).
- Backoff policy adoption and reason vocabulary unification (M006).

### Milestone 005 — Durable health effect application and quarantine gating

Class: capability

Objective:

Make the durable health subsystem a live part of production routing: hydrate
the persisted account backoffs and model quarantine entries into the running
generation, persist the effects the routing path applies, and let the
existing quarantine gate actually execute.

Dependencies:

- None. The durable tables and repository surfaces already exist under the
  current migration chain; only the production consumer path is missing.

Deliverable boundary:

- The durable health repositories are constructed in the generation build
  path, not per request.
- Active backoff records and quarantine entries are hydrated before the
  generation is published.
- The quarantine is attached to the router so the eligibility gate executes,
  with no change to the gate's fail-closed partition logic.
- The account backoff and model quarantine effects are persisted outside the
  selection lock.
- Stale durable rows are expired on the bounded maintenance schedule.
- `architecture/overview.md` and `architecture/deep-dive-health.md` describe
  the shipped behavior rather than the intended one.

User or operator value:

A provider that goes bad stays bad across a restart, and a model that must be
withdrawn is not routed to again after the process comes back.

Exit conditions:

- A generation built against a database with an active backoff and an active
  quarantine serves requests with both effects applied.
- An exactly quarantined model is excluded, and an unclassifiable candidate
  does not bypass an exact persisted quarantine.
- A provider failure leaves a durable row that survives a process restart.
- No routing-decision schema, exclusion reason code, or config classification
  changes.
- Default and no-default workspace qualification passes.

Deferred work:

- Backoff policy adoption and reason vocabulary unification (M006).
- Any new durable health table or column; the current schema is sufficient.

### Milestone 006 — Backoff policy adoption and health-reason vocabulary unification

Class: invariant

Objective:

Make the live path use the bounded reason-specific backoff policy that
already exists, and replace the hand-written failure-to-reason string
vocabulary with one typed mapping, so live durations and durable reason
values cannot diverge from the policy that defines them.

Dependencies:

- Hard: M005 closed. The durable path is what makes the policy and the
  vocabulary observable; landing them out of order produces a live path and a
  durable path that disagree.
- Interface: the persisted reason strings in the durable health tables are a
  stable contract and keep their current values.

Deliverable boundary:

- The live backoff duration comes from the existing policy rather than from
  an inline constant.
- One typed mapping replaces the hand-written reason strings, and the
  persisted string values stay byte-identical.
- The reader reconstructs every reason the writer can produce.
- The durable deadline clamp agrees with the policy cap.
- A guard keeps the writer's and reader's reason value sets equal.

User or operator value:

Backoff behaves the way the operator-facing policy describes — escalating,
bounded, and jittered — instead of applying a fixed first-step delay, and a
stored reason never degrades silently into an unknown reason.

Exit conditions:

- Every classifiable failure derives its duration from the policy, and the
  result never exceeds the policy cap.
- A repeated failure escalates under the policy.
- The persisted reason strings are unchanged.
- Default and no-default workspace qualification passes.

Deferred work:

- Any change to the policy table's values, which is a separate decision.
- Any new failure category or health reason variant, which changes the
  durable vocabulary.

### Milestone 007 — Durable backoff scope and reason correction

Class: invariant

Objective:

Make a durable backoff row describe the failure it actually records — the
live reason and the escalation rung of the scope it belongs to — so a restart
restores exactly the effect the live path applied.

Dependencies:

- Hard: M005 and M006 closed. The defect lives in the interaction between the
  durable writers M005 added and the policy-derived rung M006 introduced.
- Interface: the durable backoff table's schema and its unique key
  `(account_id, model_id, reason)` are unchanged.

Deliverable boundary:

- The model-scoped durable row records the reason the live path classified,
  not a fixed reason.
- The model-scoped row carries the model's own escalation rung and the
  account-scoped row carries the account's.
- The hydration restores a row by its scope rather than by its reason value.
- The backoff policy table is unchanged.

User or operator value:

A model that failed for an upstream reason no longer inherits an inflated
model-absence escalation after a restart, and the durable rows distinguish a
model absence from an upstream failure.

Exit conditions:

- A durable-effects test asserts the model-scoped row's reason and the two
  rows' rungs.
- A durable-effects test asserts that a hydrated model-scoped row disables the
  model without cooling the account down.
- Default and no-default workspace suites pass.

Deferred work:

- Whether a provider hint should escalate along the policy ladder. That is a
  policy change on the dominant rate-limit path and needs its own milestone.
- The parallel `HealthEffectApplier` in `rust/src/health/effects.rs`, which
  has no production caller. Discharged by Milestone 009.

### Milestone 008 — Durable health state repair and authentication-failure durability

Class: invariant

Objective:

Make the durable health state internally consistent — a row the runtime cannot
interpret must not decide whether the process starts, the durable table's
uniqueness must match the scope convention its own migration documents, and an
account whose credential was rejected must stay disabled across a restart.

Dependencies:

- Hard: `plans/adrs/ADR-0005-durable-health-state-repair-and-authentication-failure-durability.md`
  accepted. The milestone implements that decision, and the decision changes
  what durable state means across a restart and changes auth semantics. The
  maintainers accepted it on 2026-10-25.
- Interface: the durable backoff table's scope convention is unchanged. A NULL
  `model_id` still means account-wide suppression.
- Hard: none on another milestone. Milestone 009 follows this one and is not
  a dependency of it.

Deliverable boundary:

- A durable `model_unavailable` row with a NULL `model_id` is reported and
  skipped by the hydration instead of failing the whole batch, and the
  generation is still published.
- The durable backoff table gains the NULL-scope uniqueness that migration 0054
  gave the quarantine table, preceded by the same duplicate-row cleanup.
- The live failure path writes the deadline-less `authentication_failed` row
  that the schema, the validator, the hydrator, and the credential-change
  rehash path already exist for.
- The backoff policy table, the reason vocabulary, the scope rules, the
  escalation rungs, and the quarantine state machine are unchanged.

User or operator value:

A database written by a predecessor release no longer prevents EggPool from
starting, and an account whose credential the upstream rejected is still
disabled after a restart instead of quietly becoming eligible again.

Exit conditions:

- A durable-effects test asserts that a hydration over a table containing the
  malformed row succeeds and reports the skip.
- A durable-effects test asserts that the presence of such a row does not
  prevent a generation from being published.
- A durable-effects test asserts that a credential rejection writes the
  durable row and that hydration restores the account disable.
- The migration chain is contiguous from v1 to v64 and the checksum inventory
  guard passes.
- Default and no-default workspace suites pass.

Deferred work:

- The `migration_legacy` provenance promised by migration 0051 is not
  produced, because a legacy account-wide row carries no model identity to
  attribute. The new migration records that.

### Milestone 009 — Health effect ownership and terminal-withdrawal producer retirement

Class: invariant

Objective:

Make the health subsystem say what it does — one effect-application path, one
owner, and no code that no producer can reach.

Dependencies:

- Hard: `plans/adrs/ADR-0006-authoritative-terminal-model-withdrawal-ownership.md`
  accepted. The milestone removes three types from the library crate's public
  surface and changes a capability boundary. The maintainers accepted it on
  2026-10-25.
- Hard: Milestone 008 closed. Both edit `rust/src/health/` and
  `rust/src/routing/router.rs`, so they do not run concurrently.

Deliverable boundary:

- The parallel `HealthEffectApplier` and its public re-export are removed.
- The unreachable terminal-withdrawal producer — the `terminal_withdrawal`
  model effect, the `AbsentAuthoritative` variant, the field that carried it,
  and the in-memory terminal set nothing populates — are removed.
- The durable terminal-withdrawal vocabulary and its enforcement are retained,
  so an existing durable row keeps being parsed and honoured.
- The coverage the removed applier test gave the applier is re-established
  against the live path.
- The bounded quarantine, the durable writers, the policy table, and the
  routing behaviour of a healthy system are unchanged.

User or operator value:

The repository stops carrying a second, contradictory design of the same
subsystem, and a test stops asserting a backoff duration the live policy never
produces.

Exit conditions:

- A test asserts that the live model-absence path produces the bounded
  quarantine effect and no terminal effect.
- A test asserts that a durable quarantine row carrying the terminal state is
  still parsed and still enforced.
- The removed public types are absent from the library crate's public surface.
- Default and no-default workspace suites pass.

Deferred work:

- An authoritative-absence producer. It requires the durable success-path
  clearing that Milestone 005 identified as its prerequisite, and it is not
  this milestone's work.

## 8. Cross-cutting requirements

Storage/migration: none.

Protocol/compatibility: model IDs, provider-qualified parsing, routing decision JSON, selector protocol/fingerprint, and public scorer interfaces remain unchanged.

Security: account names are already routing metadata; no new raw content or credentials may be captured.

Concurrency/cancellation: selection_lock remains. Estimator snapshotting must not introduce nested lock inversion with claim/catalog/health state. No await inside the critical section.

Observability: RoutingDecisionTrace/SelectionSnapshot fields, order, exclusion codes, score components, and accepted fairness decision remain equivalent.

Performance: prefer fewer owned collections and fewer candidate/String clones. Do not trade them for a new dependency, concurrent map, or a longer-lived lock.

## 9. Verification strategy

Focused suites:

~~~bash
cargo test --manifest-path rust/Cargo.toml --test routing_domain -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test routing_domain_d008 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test routing_claims -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test quota -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_publication -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_boundaries -- --test-threads=1
~~~

M001 should add deterministic parity coverage across account counts, fairness modes/scopes, provider pinning, transcode/native preference, health/quarantine exclusions, hard-cap/score-only quota modes, and retries' excluded-account sets. Timing assertions are not acceptance gates; structural allocation removal plus semantic parity is sufficient.

Run strict Clippy and the full serial default/no-default workspace before closure.

## 10. Risks and decision points

- The public scorer currently accepts String-keyed BTreeMaps. Replacing that public interface would save less than it risks; keep it and add a private routing-oriented path.
- QuotaEstimator::snapshot currently calls sync_mirrors and clones AccountQuota. Avoid per-account lock calls, but do not hold the estimator Mutex while doing expensive routing/fairness work.
- Reordering score production can silently alter deterministic tie behavior even if numeric scores match. Order parity is a first-class acceptance criterion.
- Moving fairness candidates rather than cloning is optional; stop if it complicates accepted-fairness/rejected-probe diagnostics.
- Affinity LRU redesign was separately measured and implemented as M002/M003; exact order and bounded slot reuse passed on the current Pi 5.

## 11. Completion definition

This roadmap closes when M001 and M003 are closed with semantic and performance
evidence, and when M004–M009 are closed with correctness and durable-health
evidence. No routing architecture or public compatibility surface may regress.

## 12. Milestone status

| Milestone | Status | Implementation plan | Closure record | Blockers |
|---|---|---|---|---|
| 001 — ordered quota-scoring and candidate-allocation cleanup | closed | plans/implementation/routing-selection/001-ordered-quota-scoring-and-candidate-allocation-cleanup.md | plans/closure/routing-selection/001-status.md | none |
| 002 — semantic-affinity exact-LRU cost qualification | closed | — | plans/closure/routing-selection/002-affinity-lru-qualification.md | Pi 5 workload justified M003, now closed |
| 003 — bounded exact affinity LRU | closed | plans/implementation/routing-selection/003-bounded-affinity-exact-lru.md | plans/closure/routing-selection/003-status.md | Exact bounded LRU passed semantic tests and Pi 5 performance gates |
| 004 — quota, recovery-interval, and health-absence fail-closed corrections | closed | plans/implementation/routing-selection/004-quota-health-recovery-fail-closed-corrections.md | plans/closure/routing-selection/004-status.md | none |
| 005 — durable health effect application and quarantine gating | closed | plans/implementation/routing-selection/005-durable-health-effect-application-and-quarantine-gating.md | plans/closure/routing-selection/005-status.md | none; closed through M007 |
| 006 — backoff policy adoption and health-reason vocabulary unification | closed | plans/implementation/routing-selection/006-backoff-policy-adoption-and-health-reason-vocabulary.md | plans/closure/routing-selection/006-status.md | none; closed through M007 |
| 007 — durable backoff scope and reason correction | closed | plans/implementation/routing-selection/007-durable-backoff-scope-and-reason-correction.md | plans/closure/routing-selection/007-status.md | none |
| 008 — durable health state repair and authentication-failure durability | closed | plans/implementation/routing-selection/008-durable-health-state-repair-and-authentication-failure-durability.md | plans/closure/routing-selection/008-status.md | none |
| 009 — health effect ownership and terminal-withdrawal producer retirement | ready | plans/implementation/routing-selection/009-health-effect-ownership-and-terminal-withdrawal.md | — | none; ADR-0006 accepted 2026-10-25; M008 closed |
