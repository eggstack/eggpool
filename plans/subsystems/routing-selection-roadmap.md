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

No user-visible routing capability change is planned.

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
- M002 affinity LRU is soft/evidence-gated and should be reconsidered only after M001 closure and a representative sticky-alias workload.
- Persistence work is independent; no cross-subsystem dependency.

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

No implementation plan is authorized yet. If the retain scan is not material, record a keep decision and leave the simple exact LRU unchanged.

Exit conditions:

A later plan exists only with measured evidence and a bounded exact-LRU design. Approximate eviction or an unbounded stale-node queue is not acceptable.

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
- Affinity LRU redesign has materially higher correctness complexity than M001 and stays deferred without measurement.

## 11. Completion definition

This roadmap closes when M001 has removed the known deferred transient scoring collections with full parity evidence and M002 has either produced a measured exact-LRU follow-up or an explicit keep decision. No routing architecture or public compatibility surface may regress.

## 12. Milestone status

| Milestone | Status | Implementation plan | Closure record | Blockers |
|---|---|---|---|---|
| 001 — ordered quota-scoring and candidate-allocation cleanup | closed | plans/implementation/routing-selection/001-ordered-quota-scoring-and-candidate-allocation-cleanup.md | plans/closure/routing-selection/001-status.md | none |
| 002 — semantic-affinity exact-LRU cost qualification | not started | — | — | M001 closed; still needs a representative 64/512/4096-entry workload showing the exact VecDeque touch is material (see `plans/closure/routing-selection/001-status.md` §11) |
