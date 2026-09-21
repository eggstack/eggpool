# Plan 231 — Compact Routing and Selection Allocation Cleanup

Date: 2026-09-21
Status: complete
Planning baseline: 3b9b63861e554161c152520491e0bd050c864f02
Parent roadmap: plans/230-residual-native-runtime-efficiency-roadmap.md
Priority: P1 deterministic request-path allocation reduction

## Purpose

Remove two source-proven redundant allocation patterns from compact admission/routing and ordinary provider/account selection without changing routing policy, request semantics, public Rust APIs, or concurrency ownership.

This plan is intentionally structural. It does not require a benchmark threshold because the target operations clone data that is provably not consumed by the resulting calculation.

## Finding A — Compact routing facts deep-clone native request preservation

Authority:

- rust/src/request/admission.rs
- rust/src/coordinator/endpoints.rs
- rust/src/coordinator/finite.rs
- rust/tests/codex_compaction_compat.rs
- rust/tests/canonical_request.rs

Current CompactAdmittedRequest::routing_facts builds a temporary AdmittedRequest containing:

- canonical.clone();
- native_preservation.clone();
- raw body/token counters.

NativeRequestPreservation::clone recursively clones its serde_json::Value. routing_request_facts does not inspect native_preservation. Therefore a compact history can be recursively copied solely to calculate routing facts and immediately discarded.

### Required implementation

Factor routing-fact construction around the data it actually consumes.

A suitable shape is conceptually:

~~~rust
fn routing_request_facts_from_parts(
    canonical: &CanonicalRequest,
    reservation_tokens: u64,
    inputs: &StaticRoutingFacts,
) -> RoutingRequestFacts
~~~

Then:

- AdmittedRequest::routing_facts delegates to that helper;
- CompactAdmittedRequest::routing_facts delegates directly to the same helper;
- the existing public methods remain present and retain their signatures;
- routing_request_facts, if public compatibility requires it, remains available and delegates rather than duplicating logic.

Do not introduce Arc<Value>, Cow<Value>, or a public NativeRequestPreservation representation change in this plan.

### Required semantic parity

The resulting RoutingRequestFacts must remain identical for:

- direct concrete models;
- provider-qualified models;
- virtual models after concrete resolution;
- Chat Completions, Responses, Messages;
- compact Responses;
- requested protocol;
- client protocol;
- request surface;
- projected tokens;
- capability policy;
- thinking requirement;
- stale-catalog threshold;
- provider pin;
- timestamp.

Add a focused unit regression demonstrating that compact routing-fact calculation does not require native preservation content. Prefer a helper-level equivalence assertion; do not add production counters or expose ParsedRequestBody.

## Finding B — Capability policy is cloned per account

Authority:

- rust/src/routing/eligibility.rs
- rust/src/routing/router.rs
- rust/src/quota/scorer.rs
- rust/src/quota/estimator.rs
- rust/tests/routing_domain.rs
- rust/tests/routing_domain_d008.rs
- rust/tests/routing_claims.rs
- rust/tests/quota.rs

build_eligible_candidates currently selects facts.capability_policy vs. policy.capability_policy inside the account loop by cloning a BTreeMap each iteration.

### Required implementation

Choose the effective capability policy once by reference before iterating accounts:

~~~rust
let capability_policy = if facts.capability_policy.is_empty() {
    &policy.capability_policy
} else {
    &facts.capability_policy
};
~~~

Pass that borrowed map to candidate_for_account for all identities.

Where ownership permits without public breakage, also pass EligibilityPolicy by reference rather than cloning it at each selection call. Keep public/exported type definitions unchanged.

### Conditional transient-collection cleanup

After the definite map-clone removal, inspect the current scoring path:

~~~text
Vec<RoutingCandidate>
 -> Vec<String> names
 -> BTreeMap<String, i64> active
 -> BTreeMap<String, i64> projected
 -> empty BTreeMap<String, f64> penalties
 -> Vec<RoutingScore>
 -> BTreeMap<String, RoutingCandidate>
 -> cloned candidate Vec
~~~

Reduce this only if it can be done with a small internal API and exact scoring-order parity.

Preferred direction:

- snapshot estimator state once;
- score candidate references/indices rather than owned account-name maps where practical;
- projected_tokens is one request scalar and should not require a per-account map internally;
- empty health penalties should not require constructing a map;
- preserve QuotaFairScorer public methods if they are consumed by tests or other code;
- add a crate-private routing-oriented scoring helper rather than replacing the public scorer contract if necessary;
- move candidates by index/ownership rather than cloning them back out where the implementation remains straightforward.

Do not introduce HashMap merely as a performance guess. BTreeMap determinism is part of several diagnostic/test expectations. Any data-structure change requires explicit ordering evidence.

If the cleanup becomes invasive, stop after the definite capability-policy fix. The objective is allocation reduction, not a scoring subsystem rewrite.

## Selection-lock rule

rust/src/routing/router.rs selection_lock remains in place.

Do not shorten the critical section by moving claim/quota/fairness mutations outside their atomic selection transaction. Optimize synchronous work inside the lock without changing correctness boundaries.

No provider or SQLite await may enter the lock.

## Tests

At minimum add/regress:

1. compact routing facts are byte/field-equivalent before and after the helper refactor;
2. request capability policy overrides configured capability policy exactly as before;
3. empty request capability policy falls back to configured policy;
4. policy exclusion reason codes are unchanged;
5. direct/provider-qualified routing produces identical candidate order;
6. fairness round-robin/random/off behavior remains identical;
7. hard-cap and score-only quota behavior remains identical;
8. no change to selected provider/account/model/protocol under existing deterministic fixtures.

Do not test performance with sleeps or timing assertions.

## Focused qualification

~~~bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings

cargo test --manifest-path rust/Cargo.toml --test canonical_request -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test codex_compaction_compat -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test routing_domain -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test routing_domain_d008 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test routing_claims -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test quota -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c009 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c011 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_boundaries -- --test-threads=1
~~~

Then run the shared Plan 230 closure gates.

## Structural evidence to record

Before:

- CompactAdmittedRequest::routing_facts recursively clones NativeRequestPreservation.parsed.
- capability policy selection can clone one BTreeMap for every account considered.
- any accepted conditional scoring cleanup should document its temporary owned collections.

After:

- compact routing facts do not clone native preservation;
- capability policy selection borrows one effective map for the complete account iteration;
- public request/routing types and methods are unchanged;
- selected candidates, traces, exclusions, quota effects, and fairness results are unchanged;
- no new synchronization primitive or dependency is introduced.

## Stop conditions

Stop rather than widening the plan if:

- removing a clone requires changing a public struct field type;
- scoring cleanup changes deterministic ordering;
- an optimization would move claim/quota/fairness mutation outside selection_lock;
- a proposed change adds unsafe code, a new dependency, or a concurrent map;
- routing tests reveal semantic differences rather than ownership-only differences.

## Completion criteria

- [x] compact routing-fact construction no longer clones NativeRequestPreservation;
- [x] existing public routing/admission methods remain source compatible;
- [x] effective capability policy is selected once by reference;
- [x] EligibilityPolicy is not cloned unnecessarily on the hot selection path where a borrow is sufficient;
- [x] additional scoring collection cleanup was deferred to preserve the deterministic public scorer contract;
- [x] routing/fairness/quota/compact tests pass;
- [x] no HTTP, CLI, config, persistence, provider, retry, or health contract changes;
- [x] closure evidence records the removed allocations and implementation commit.

## Closure evidence

The compact helper now derives routing facts directly from canonical request
and token fields; it never clones `NativeRequestPreservation`. Eligibility
selects one borrowed effective capability policy before iterating accounts, and
the router borrows its immutable `EligibilityPolicy` instead of cloning it per
selection call. The transient scoring collections remain unchanged because
their ordered public behavior is already covered and a broader rewrite was not
justified.

Focused evidence: `canonical_request`, `codex_compaction_compat`,
`routing_domain`, `routing_domain_d008`, `routing_claims`, `quota`,
`coordinator_c009`, `coordinator_c011`, and `coordinator_boundaries` all pass
with serial tests. No public request/routing type or selection-lock boundary
changed.
