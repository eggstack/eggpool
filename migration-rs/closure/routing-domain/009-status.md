# D009 Closure — Selection Fairness and Frozen Routing-Trace Correction

Status: closed

Recommendation: closed

Implementation commit: [`1557d59`](https://github.com/eggstack/eggpool/commit/1557d59)

Plan: [D009 — selection fairness and frozen routing-trace correction](../../implementation/routing-domain/009-selection-fairness-and-trace-snapshot-correction.md)

Contract and oracle: [M5 routing-domain contract](../../routing-domain-contract.md),
D001's [Python observations](../../fixtures/routing-domain/d001-python-observations.json)
and [fixture matrix](../../fixtures/routing-domain/d001-fixture-matrix.json), plus the
Python routing/fairness and routing-trace boundaries named by D001/D006/D008.

## Outcome

D009 corrects the two post-D008 selection-contract gaps without reopening the
M5 architecture. `RoutingRouter::select_and_claim` now applies the injected
random source to the actual claimable near-tie band. Read-only plan, readiness,
and no-claim trace construction remain side-effect free. Probe availability is
revalidated before the fairness choice; a probe race is excluded and the
already chosen ordering is filtered without another random draw. Round-robin
continues to commit once after an accepted claim, while random mode never
creates rotor state.

Each accepted `SelectionClaim` now owns an `Arc`-backed immutable
`SelectionSnapshot`. It captures the request routing facts, pre-publication
candidate scores/order, top candidate, selected identity and score, exclusions,
fairness decision, and the assigned local claim ID. `RoutingRouter::trace_for`
serializes that snapshot for accepted claims and only builds a read-only plan
when no accepted claim exists. Rollback, pending-to-reserved conversion, active
release, and later health/probe changes do not alter the snapshot.

The snapshot is bounded and secret-free: no credentials, raw request content,
session data, or provider error text is retained. No SQLite/network await,
schema migration, request codec, coordinator, retry/finalization path, or new
dependency was introduced.

## Requirement-to-evidence matrix

| D009 requirement | Evidence | Result |
|---|---|---|
| Actual random accepted selection | `rust/tests/routing_claims.rs::random_fairness_is_applied_by_accepted_claims_exactly_once` uses a recording injected RNG and selects both controlled peers through `select_and_claim`. | Pass |
| Exactly-once random consumption | The same test records one call and the supplied band size per accepted claim; the read-only test records zero calls. | Pass |
| Read-only plan/readiness/trace safety | `random_read_only_calls_do_not_consume_or_rotate_state` proves zero RNG calls and zero rotor keys after all three operations. | Pass |
| Off and round-robin preservation | `off_fairness_remains_score_name_deterministic`, existing claim rotation tests, and the full routing-domain suites pass. | Pass |
| Frozen accepted decision | `accepted_trace_is_frozen_before_claim_publication` proves the post-claim plan reorders to account B while the accepted trace remains account A with the pre-claim scores/fairness. | Pass |
| Claim lifecycle stability | The frozen-trace test compares traces after conversion and active release; existing rollback tests cover rollback stability and ownership cleanup. | Pass |
| Probe/priority/native boundaries | Existing routing-domain and D008 suites cover strict priority, native/transcode separation, material score differences, and half-open single-probe contention; the claim path revalidates probe eligibility before fairness. | Pass |
| Security and bounded state | `SelectionSnapshot` contains only typed routing identity, scores, exclusions, fairness metadata, and local IDs; no secret/raw payload fields exist. Existing redaction tests remain green. | Pass |
| D008 regression | `routing_domain_d008` remains green with the integrated schema-54 generation and contention scenarios. | Pass |
| M6/M7 boundary | No request parsing, codec/SSE, persistence, retry, finalization, or coordinator behavior was added. | Pass |

## Verification commands actually run

```text
rtk cargo fmt --manifest-path rust/Cargo.toml
rtk cargo fmt --manifest-path rust/Cargo.toml -- --check
rtk cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
rtk cargo test --manifest-path rust/Cargo.toml --test routing_claims -- --test-threads=1  # 11 passed
rtk cargo test --manifest-path rust/Cargo.toml --test routing_domain -- --test-threads=1  # 12 passed
rtk cargo test --manifest-path rust/Cargo.toml --test routing_domain_d008 -- --test-threads=4  # 4 passed
rtk cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1  # 106 passed
rtk uv sync --frozen --extra ci
rtk uv run pytest tests/migration_rs -q --tb=short --maxfail=1  # 55 passed, 3 skipped
rtk uv run pytest tests/unit/test_accounts.py tests/unit/test_catalog.py tests/unit/test_catalog_withdrawal_policy.py tests/unit/test_catalog_service_ping.py tests/unit/test_catalog_service_limits.py tests/unit/test_catalog_resolvers.py tests/unit/test_quota.py tests/unit/test_health.py tests/unit/test_account_backoff_repository.py tests/unit/test_failure_effects_table.py tests/unit/test_routing.py tests/unit/test_routing_priority.py tests/unit/test_routing_provider.py tests/unit/test_routing_transcode_eligibility.py tests/unit/test_model_router_config.py tests/unit/test_model_router_registry.py tests/unit/test_model_router_affinity.py tests/unit/test_model_router_selector.py -q --tb=short --maxfail=1  # 485 passed
rtk uv run ruff format --check src/ tests/ scripts/  # 721 files already formatted
rtk uv run ruff check src/ tests/ scripts/  # passed
rtk uv run pyright src/ scripts/  # 0 errors
rtk uv run pytest tests/smoke/ -q --tb=short --maxfail=1  # 14 passed
rtk git diff --check
```

## Historical closure and future-plan state

D009 supersedes the D006/D008 aggregate conclusion only for the two findings
named in the D009 plan: actual random fairness selection and frozen accepted
selection/trace evidence. D006 and D008 closure records remain unchanged
historical evidence. All other accepted M5 evidence remains valid.

M5 is now recorded as `closed after D009 corrective pass`. D009 has moved from
the dependency-ready registry section to the completed table. M6 canonical
request/codec/transcoding/SSE implementation handoff is unblocked, but no M6
implementation plan is registered yet and its own planning review is still
required. M7 coordinator/retry/finalization remains blocked on M6; M8 through
M12 remain sequenced behind their stated predecessors.

Unresolved mandatory findings: none.
