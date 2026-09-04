# D006 Closure — Routing Eligibility, Fairness, and Local Claims

Status: closed

Implementation commit: [`b009023`](https://github.com/eggstack/eggpool/commit/b009023)

Plan: [D006 — routing eligibility, fairness, and local claims](../../implementation/routing-domain/006-routing-eligibility-fairness-and-claims.md)

Contract and oracle: [M5 routing-domain contract](../../routing-domain-contract.md),
D001's [Python observations](../../fixtures/routing-domain/d001-python-observations.json),
and the Python routing/quota/health modules named by the plan.

## Outcome

D006 adds the Rust `routing` boundary. `RoutingRequestFacts` is a typed,
request-independent input and `build_routing_plan` performs ordered,
credential-free eligibility, catalog/protocol/surface/capability/quarantine/
health gates, strict priority grouping, request/token scoring, native versus
transcode facts, stable exclusions, and bounded fairness diagnostics.

`RoutingRouter::select_and_claim` owns one cancellable async selection gate.
After acquisition, the critical section performs only synchronous in-memory
work: revalidation, half-open probe acquisition, active ownership publication,
pending quota publication, and fairness commit. SQLite, network, and refresh
callbacks are outside that gate. `SelectionClaim` provides explicit,
idempotent rollback, durable-publication conversion, and active release; no
cleanup is attempted from `Drop`.

## Requirement-to-evidence matrix

| D006 requirement | Evidence | Result |
|---|---|---|
| Typed request facts | `routing/eligibility.rs::RoutingRequestFacts` carries canonical model/provider, protocol/surface, transcode set, thinking requirement, projected tokens, freshness, and controlled time. | Pass |
| Stable eligibility gates/reasons | `build_eligible_candidates` emits ordered `RoutingExclusion` values for configuration, credentials, provider, surface, protocol, health, quarantine-capable health state, catalog, capability, local hard-cap, and malformed score gates. | Pass |
| Provider-qualified input | `RoutingRequestFacts::from_model_id` delegates parsing to D002 `ModelCatalogCache::parse_model_provider`. | Pass |
| Native/transcode preference | Provider model protocol determines `requires_transcode`; score ordering applies native preference only within a priority tier and fairness band. | Pass |
| Strict priority tiers | Candidate ordering compares descending priority before score; fairness tests use a single best tier and cannot rotate across the boundary. | Pass |
| Request/token scoring | D004 `QuotaFairScorer` is reused with local active counts and projected tokens; cost remains a diagnostic estimate only. | Pass |
| Non-finite state | Infinite/ineligible scores become the stable `malformed_score` exclusion and cannot enter fairness or selection. | Pass |
| Bounded fairness | `FairnessRotor` sorts names, previews without mutation, commits only after an accepted claim, and caps LRU state at 4,096 keys. | Pass |
| Random injection | `FairnessRandom` is an injectable selection dependency; read-only plans do not advance it. | Pass |
| Read-only plan/readiness | `build_routing_plan` and `has_eligible_pairing` do not acquire probes, advance the rotor, publish active/pending load, invoke recovery, or perform durable I/O. | Pass |
| Atomic local claim | `select_and_claim` revalidates under one Tokio mutex and publishes active plus pending ownership before release; health probe contention reselects without leaking state. | Pass |
| Claim ownership | `SelectionClaim` contains non-secret identity, provider/model/protocol/tier, transcode and projected load facts, claim ID, and probe ownership. | Pass |
| Rollback/conversion/release | Explicit claim methods use D004 ownership-checking operations, prevent active underflow, are idempotent on duplicate compensation, and retain active ownership through conversion. | Pass |
| Missing-account recovery | Injected recovery callback is per-account monotonic-throttled, filtered to plausible accounts, and bounded to 4,096 keys; callback is outside selection lock. | Pass |
| Trace boundary | `RoutingDecisionTrace` serializes request identity, candidates, exclusions, selected account, and local claim ID without request bodies or credentials. | Pass |
| M5 boundary | No request parser, provider inference client, SQLite selection write, retry/failover, attempt persistence, finalization, or semantic model-router selector was added. | Pass |

## Differential and concurrency evidence

`rust/tests/routing_claims.rs` covers two-account pending-load separation,
idempotent rollback, pending-to-reserved conversion, read-only fairness/claim
invariance, and the 4,096-key cap. Existing D001 Python observations remain
the frozen oracle; `tests/migration_rs/test_d001_routing_domain.py` passes.
The Rust implementation reuses the already-closed D002/D004/D005 typed
catalog, scorer, quota, and health boundaries rather than creating parallel
state machines.

No credentials, proxy values, request bodies, raw upstream errors, or session
content enter routing plans, claims, traces, errors, fairness keys, or tests.

## Verification commands actually run

```text
rtk cargo fmt --manifest-path rust/Cargo.toml -- --check
rtk cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
rtk cargo test --manifest-path rust/Cargo.toml --test routing_claims -- --test-threads=1  # 5 passed
rtk cargo test --manifest-path rust/Cargo.toml --lib -- --test-threads=1  # 16 passed
rtk cargo test --manifest-path rust/Cargo.toml --test quota -- --test-threads=1  # 7 passed
rtk cargo test --manifest-path rust/Cargo.toml --test health -- --test-threads=1  # 8 passed
rtk cargo test --manifest-path rust/Cargo.toml --test routing_domain -- --test-threads=1  # 12 passed
rtk cargo test --manifest-path rust/Cargo.toml --test database_compatibility -- --test-threads=1  # 6 passed
rtk uv run pytest tests/migration_rs/test_d001_routing_domain.py -q --tb=short --maxfail=1  # 7 passed
rtk git diff --check
```

The repository-wide Rust all-target command was attempted during this pass
and does not terminate at the pre-existing catalog-refresh fixture in this
workspace, as documented by the D004/D005 closure records. The affected
predecessor suites and all new D006 tests pass independently. No live
provider, inference request, or performance gate is required for this
state-only milestone.

## Acceptance and stop-condition review

The stale-selection window is closed by the local claim gate; pending request
and token load is visible before another selector can enter. Fairness is
strictly tier/native/epsilon bounded, restart-local, and capped. Readiness and
plan construction are non-mutating. Half-open probe ownership is read-only
until claim time and explicit release handles rollback. Claim transitions use
ownership-checking quota operations and cannot silently decrement another
claim. M6/M7 request parsing, persistence, retry/failover, and finalization
remain outside this implementation.

Unresolved mandatory findings by severity: none.

## Future-plan state

D006 is removed from the dependency-ready table and recorded as completed in
the registry, roadmap, handoff sequence, and this closure record. D007 is
promoted to the sole dependency-ready M5 implementation plan because its hard
predecessor D006 is closed. D008 remains queued behind D007 closure. M6
planning may continue conceptually, but M6 implementation handoff remains
blocked on integrated D008 closure; M7 remains additionally blocked on M6.
