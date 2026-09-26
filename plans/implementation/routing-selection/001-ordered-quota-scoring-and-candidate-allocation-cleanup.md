# Routing Selection Milestone 001 — Ordered Quota-Scoring and Candidate-Allocation Cleanup

Status: active

Repository baseline: e64df3391a85a19267c69c95897f2515ba0463d0

Source roadmap:

- plans/subsystems/routing-selection-roadmap.md#milestone-001--ordered-quota-scoring-and-candidate-allocation-cleanup

Long-term requirements:

- plans/000-long-term-specification.md — §3 ownership boundaries and §5 performance posture
- plans/003-planning-process.md — reversible polish must preserve invariants and public capability

Applicable ADRs:

- None required. Stop for review if implementation needs to change routing semantics, selection-lock ownership, public scorer contracts, or deterministic diagnostic ordering.

Primary class: polish

## 1. Objective

Finish the routing transient-collection cleanup that legacy Plan 231 deliberately deferred.

Remove the per-selection String-keyed active/projected/penalty/candidate maps and candidate re-cloning from the private router path, while keeping QuotaFairScorer's public methods, the selection lock, score formulas, ordering, fairness, quota/health effects, and RoutingDecisionTrace exactly equivalent.

## 2. Why this milestone is ready

The optimization is now well bounded by current code and previous history.

Plan 231 already identified this exact transient pipeline, fixed the simpler capability-policy clone, and stopped because rewriting the public scorer was disproportionate to that plan. The current source still contains the deferred maps and candidate clone. Explicit user direction has reopened performance work, but there is still no reason to break the public scorer: the router can gain a private ordered path.

No external dependency or architecture decision is required.

## 3. Current implementation evidence

Authority paths:

- rust/src/routing/eligibility.rs
- rust/src/routing/router.rs
- rust/src/routing/claim.rs
- rust/src/routing/fairness.rs
- rust/src/quota/scorer.rs
- rust/src/quota/estimator.rs
- rust/src/quota/state.rs
- rust/tests/routing_domain.rs
- rust/tests/routing_domain_d008.rs
- rust/tests/routing_claims.rs
- rust/tests/quota.rs
- rust/tests/coordinator_publication.rs
- architecture/deep-dive-routing.md

The current build_eligible_candidates path:

1. builds Vec<RoutingCandidate>;
2. clones every account_name into Vec<String>;
3. clones those names again as BTreeMap keys for active counts;
4. clones them again as BTreeMap keys for the same request-wide projected_tokens scalar;
5. allocates an empty penalties BTreeMap;
6. QuotaFairScorer::score_accounts asks QuotaEstimator::snapshot for a BTreeMap<String, QuotaAccountSnapshot>;
7. score_accounts looks each name up and clones the quota snapshot;
8. eligibility builds BTreeMap<String, RoutingCandidate>;
9. each RoutingCandidate is cloned back out to associate it with RoutingScore;
10. final validation and sort run.

The selection_lock intentionally covers this synchronous work together with local claim/quota/fairness mutation. The lock is not the target; the avoidable work inside it is.

## 4. Invariants that must not regress

- Selection lock remains one async mutex and no await enters after it is acquired.
- Catalog/claim/quota/health/quarantine eligibility checks occur in the same logical order.
- Quota score formula and all RoutingScore fields remain exact.
- Malformed/ineligible score behavior and exclusion reason codes remain exact.
- Final candidate sort precedence remains priority, score, native preference, account name exactly as today.
- Fairness off/round-robin/random behavior, key scope, rotor commit timing, probe handling, and accepted-fairness trace remain exact.
- Pending claim creation/conversion/release semantics remain exact.
- RoutingDecisionTrace/SelectionSnapshot remains deterministic and field-equivalent.
- Public QuotaFairScorer::score_accounts, rank_accounts, near_ties and exported data types remain available.
- No HashMap substitution for ordered public/diagnostic facts.
- No new dependency or unsafe code.

## 5. Scope

### In scope

- Add a crate-private ordered quota snapshot API that accepts borrowed account names/candidate references and returns results in the same order without constructing a String-keyed result map.
- Snapshot estimator state once under one estimator Mutex acquisition.
- Release the estimator lock before final candidate sorting/fairness/claim work.
- Add a private QuotaFairScorer routing-oriented method or shared score-one core that accepts ordered snapshots, direct active counts, one projected_tokens scalar, and the zero-penalty case without temporary maps.
- Keep score_accounts as a compatibility wrapper using the same scoring core.
- Associate scores with candidates by index/zip and move candidate ownership rather than building by_name and cloning candidates back out.
- Remove now-unused temporary maps from build_eligible_candidates.
- Add deterministic parity tests at increasing account counts and multiple policy modes.
- Optionally remove an immediately adjacent move-only clone in fairness_order only if it is trivial and exact parity tests already cover it; do not make that optional cleanup a requirement.

### Explicitly out of scope

- Changing SelectionSnapshot contents to make traces cheaper.
- Changing fairness algorithm or data structures.
- Changing claim::active_snapshot representation.
- Changing selection_lock or catalog lock architecture.
- Holding QuotaEstimator's Mutex across final sorting/fairness/claim publication.
- Replacing BTreeMap in public APIs or persisted/diagnostic representations.
- Model-router affinity cache redesign.
- ProviderClientPool ArcSwap lookup changes.
- Request-admission/token-estimation changes.
- New benchmark dependency.

## 6. Required production changes

### 6.1 Ordered quota snapshot boundary

Introduce a private API on QuotaEstimator, or the smallest equivalent helper, that:

- takes borrowed account names in caller order;
- acquires EstimatorState once;
- performs sync_mirrors for the requested accounts exactly as snapshot currently does;
- returns one ordered entry per requested account, preserving the missing-account case;
- clones only the quota/account state actually required to score after the lock is released;
- does not allocate String keys for the returned collection.

A conceptual shape is:

~~~text
snapshot_ordered(names: impl Iterator<Item = &str>)
    -> Vec<Option<QuotaAccountSnapshot>>
~~~

The exact signature is implementation-owned. Prefer a slice/iterator API that does not allocate another Vec<String> solely to call it.

Do not replace one map allocation with N estimator lock acquisitions.

### 6.2 Shared scoring core

Factor QuotaFairScorer so public score_accounts and the router-private ordered path share the same numeric score-one implementation.

The router-private path should receive:

- borrowed account name;
- ordered QuotaAccountSnapshot or missing entry;
- active request count read directly from the existing active_requests BTreeMap;
- facts.projected_tokens.max(0) as one scalar;
- zero health penalty for the current build_eligible_candidates path.

If a future caller needs nonzero health penalties, public score_accounts continues to support its current map contract.

No scoring formula or field population may diverge between the two paths.

### 6.3 Move candidates instead of reindexing by String

Scores must be emitted in the same order as the eligible candidate Vec.

Then consume eligible.into_iter().zip(scores) or an equivalent index-preserving move so each RoutingCandidate receives its score without:

- BTreeMap<String, RoutingCandidate>;
- candidate.account_name clone solely as a lookup key;
- candidate clone back into the scored Vec.

If a score entry is missing or malformed, preserve the current empty/ineligible behavior and exclusion reason.

### 6.4 Keep final deterministic sort unchanged

Do not optimize away or alter the final sort in this milestone.

The exact comparator is current routing policy authority. Retaining it isolates the change to ownership/allocation and makes parity review straightforward.

## 7. Ordered work packages

### Work package A — Ordered estimator snapshots

Intent:

Eliminate the String-keyed QuotaEstimator snapshot result from the router path without multiplying lock acquisitions.

Required changes:

- private ordered snapshot helper;
- shared mirror-sync behavior;
- tests for present/missing accounts and reservation/pending counters.

Acceptance evidence:

- quota tests show ordered values equal current snapshot semantics;
- estimator Mutex is acquired once per ordered snapshot call.

### Work package B — Router-private scoring adapter

Intent:

Remove active/projected/penalty maps while preserving the public scorer.

Required changes:

- shared score-one core;
- private ordered scoring method;
- direct active_requests lookup and projected scalar;
- public score_accounts remains source-compatible and semantically unchanged.

Acceptance evidence:

- numeric RoutingScore equality across representative quota policies/windows;
- existing public scorer tests unchanged.

### Work package C — Candidate ownership move

Intent:

Remove by_name reindexing and candidate cloning.

Required changes:

- score/candidate index parity;
- move candidate into final scored Vec;
- preserve malformed_score exclusion.

Acceptance evidence:

- source path no longer builds BTreeMap<String, RoutingCandidate>;
- selected candidate ordering and trace corpus remain exact.

### Work package D — Deterministic parity matrix

Intent:

Prove this is ownership-only.

Required coverage:

- 1, 4, 16, and 128 configured accounts in synthetic deterministic fixtures;
- FairnessMode Off/RoundRobin/Random;
- all FairnessScope values where practical;
- provider-pinned and unpinned requests;
- native vs transcode preference;
- LocalQuotaMode hard-cap and score-only;
- active request pressure and pending reservations;
- unhealthy/quarantined/probe-unavailable exclusions;
- excluded_accounts retry set;
- malformed/ineligible quota inputs.

Compare:

- candidate account order;
- each RoutingScore field;
- exclusions and reason codes;
- fairness decision fields;
- selected account/provider/model/protocol;
- SelectionSnapshot/trace facts;
- claim pending counters before/after rollback/release.

Do not use timing assertions as correctness tests.

## 8. Failure, cancellation, restart, contention semantics

The selection path remains synchronous after selection_lock acquisition. No new cancellation point is added.

Quota estimator snapshotting must not create lock inversion. Acquire/release its Mutex wholly within the ordered snapshot helper and return owned bounded score inputs before fairness/claim mutation proceeds.

If any helper panics/poisons the existing std::sync::Mutex behavior remains unchanged; do not add recovery semantics as part of this optimization.

Restart/reload semantics are unchanged because all state remains generation/process owned exactly as before.

## 9. Compatibility and migration

No migration.

No config, HTTP, CLI, provider, database, persisted schema, or public Rust API change.

Public QuotaFairScorer and routing types keep their signatures/fields.

Test-only fixtures may be added; no external compatibility version is changed.

## 10. Required tests

At minimum:

- rust/tests/quota.rs — public scorer parity plus ordered snapshot helper behavior through exposed owners.
- rust/tests/routing_domain.rs — eligibility/policy parity.
- rust/tests/routing_domain_d008.rs — deterministic routing/fairness parity.
- rust/tests/routing_claims.rs — claim ownership/active counts.
- rust/tests/coordinator_publication.rs — RoutingDecisionTrace persistence remains compatible.
- rust/tests/coordinator_boundaries.rs — coordinator/routing integration.

Add the bounded account-count parity matrix to the narrowest existing routing target or a new focused rust/tests/routing_selection_efficiency.rs if that keeps the existing tests readable.

Do not duplicate the entire production scorer in test code as a permanent second oracle. Prefer captured expected values and cross-check public score_accounts against the private path at the scoring seam.

## 11. Required verification commands

~~~bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --test routing_domain -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test routing_domain_d008 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test routing_claims -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test quota -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_publication -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_boundaries -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
git diff --check
~~~

No Cargo/dependency change is expected. If one appears, stop and justify it before running the dependency/release matrix.

## 12. Documentation updates

- architecture/deep-dive-routing.md — record that router scoring uses an ordered private snapshot/scoring path and no longer constructs the deferred transient maps.
- plans/subsystems/routing-selection-roadmap.md — lifecycle/status only.
- Keep legacy Plans 230/231 immutable; reference their deferred-work decision rather than rewriting it.

## 13. Acceptance criteria

- build_eligible_candidates no longer constructs per-selection active/projected/zero-penalty String-keyed maps solely for scoring.
- Router scoring no longer requires a BTreeMap<String, QuotaAccountSnapshot> result.
- The candidate Vec is not converted to BTreeMap<String, RoutingCandidate> and cloned back out.
- Estimator state is snapshotted under one lock acquisition, not one lock per account.
- Public QuotaFairScorer APIs remain unchanged and numerically equivalent.
- Final deterministic comparator, fairness, selection-lock, claim, health, quota, exclusion, and trace behavior remain exact.
- 1/4/16/128-account deterministic parity coverage passes.
- No dependency/config/API/schema/concurrency change.
- Full default/no-default suite passes.

## 14. Stop conditions

Stop and report rather than improvise if:

- the cleanup requires changing a public scorer or routing struct signature;
- numeric score or deterministic candidate ordering changes;
- the only implementation holds EstimatorState across fairness/claim work or acquires the estimator lock once per account;
- a HashMap/concurrent map/new dependency is proposed merely to avoid BTreeMap allocations;
- selection_lock atomicity would be weakened;
- the optimization expands into affinity LRU, provider pool, persistence, or admission changes.

## 15. Closure evidence required

The closure record must include:

- implementation commit(s);
- before/after structural pipeline showing removed temporary collections/clones;
- proof that the public scorer path remains present;
- deterministic parity matrix results for account counts/policies;
- focused routing/quota/coordinator results;
- full default/no-default results;
- documentation updates;
- severity-tagged residual findings;
- explicit disposition of roadmap M002: still evidence-gated unless affinity measurements were separately produced.

## 16. Handoff notes

This milestone should make the existing simple routing architecture cheaper, not smarter.

Preferred flow:

eligible Vec<RoutingCandidate>
    -> one ordered estimator snapshot under one lock
    -> one shared score-one implementation
    -> scores aligned by index
    -> move candidates + scores together
    -> unchanged validation/sort/fairness/claim

Avoid turning an allocation cleanup into a scorer redesign.
