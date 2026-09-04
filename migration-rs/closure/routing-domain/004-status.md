# D004 Closure — Quota, Claims, and Fair-Share Scoring

Status: closed

Recommendation: closed; D005 is now the sole dependency-ready M5 implementation plan. D006 remains blocked on D005, D007 remains blocked on D006, and D008 remains blocked on D001-D007 closure.

Implementation commit: [`d649e8a`](https://github.com/eggstack/eggpool/commit/d649e8a)

Plan: [D004 — quota, claims, and fair-share scoring](../../implementation/routing-domain/004-quota-claims-and-fair-scoring.md)

Contract and oracle: [M5 routing-domain contract](../../routing-domain-contract.md) and D001's [Python observation fixture](../../fixtures/routing-domain/d001-python-observations.json).

## Outcome

D004 adds the Rust quota boundary without importing the deprecated Python
`ReservationManager`. `QuotaEstimator` owns bounded account/model and global
EWMA state, persisted usage snapshots, request/token/cost reservation mirrors,
and synchronous pending-claim operations. `QuotaFairScorer` copies the small
per-account aggregate once, computes request/token utilization with weights and
offsets, retains cost for diagnostics only, and provides deterministic ranking.

`UsageWindowRepository::get_all_usage_windows` reads the existing schema-54
`requests` table in one grouped query. Hydration preserves the Python fallback
boundary: only the 5h value can use the bounded hourly window; missing 7d/30d
values remain zero. No migration, request parser, provider I/O, retry path,
durable inference attempt, or finalization behavior was added.

## Requirement-to-evidence matrix

| D004 requirement | Evidence | Result |
|---|---|---|
| Typed quota state and defaults | `quota/state.rs` defines `AccountQuota`, `QuotaPolicy`, persisted snapshots, rolling windows, all six request/token defaults, offsets, weights, and diagnostic mirrors. | Pass |
| Request/token pressure only | `quota/scorer.rs` computes each window from request count and token count; cost fields are copied only into `RoutingScore` diagnostics. D001 numeric score assertions pass. | Pass |
| Weight scaling and malformed capacity | Effective weight scales both denominators; zero/negative capacity or non-finite/invalid weight produces an infinite, ineligible score without division panic. | Pass |
| Advisory vs hard-cap boundary | Scoring leaves above-capacity accounts eligible; `AccountQuota::is_within_limits` exposes exact `>=` exhaustion for D006 hard-cap eligibility. | Pass |
| Pending ownership | `add_pending_claim` publishes one request plus projected tokens/cost; negative inputs and pending underflow return `QuotaInvariantError`. | Pass |
| Atomic pending conversion | `convert_pending_claim` validates, removes pending ownership, adds one reservation, and resynchronizes mirrors under one mutex critical section. | Pass |
| Durable reservation mirrors | Add/remove cost, request, and token operations are bounded; durable removal clamps at zero while pending release remains ownership-checking. | Pass |
| Batched persisted usage | `UsageWindowRepository::get_all_usage_windows` performs one grouped schema-54 query and excludes pending requests; the test checks the database call delta is one. | Pass |
| Bounded EWMA hierarchy | Account/model cap 4,096, global model and outlier cap 1,024, deterministic LRU touch/eviction, outlier rejection, override/family/global fallbacks, per-token and absolute ceilings are covered. | Pass |
| Deterministic ranking | `rank_accounts` orders by final score, native preference, and account name; `near_ties` keeps the epsilon/native boundary explicit. | Pass |
| Boundary ownership | The module has no JSON parsing, provider client, SQLite write, retry, attempt, or finalization dependency. | Pass |

## Differential, concurrency, restart, and security evidence

The Rust quota test reproduces the D001 account-a/account-b request/token
counts, weights, pending load, incoming projection, and expected scores
`0.3378125` and `0.16075`. It also verifies that a large cost difference does
not alter those scores, exact capacity is exhausted for hard-cap callers, and
default capacities are materialized.

The claim tests verify pending visibility, conversion without double counting,
explicit underflow failure, bounded durable removal, and publication observed
after a concurrent thread crosses a deterministic barrier. Counters use
saturating non-negative arithmetic compatible with SQLite's signed integer
range. Window tests cover ordered and out-of-order observations and left-edge
expiry.

The schema-54 seed is applied to a temporary database and the batched query
returns the completed account's request/token totals while excluding the
pending account. Database teardown closes the connection before removing the
temporary file. No credentials, proxy values, request bodies, prompts, or
provider responses enter quota state, snapshots, errors, or tests.

## Verification commands actually run

All commands below completed successfully:

```text
rtk cargo fmt --manifest-path rust/Cargo.toml
rtk cargo check --manifest-path rust/Cargo.toml
rtk cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
rtk cargo test --manifest-path rust/Cargo.toml --test quota -- --test-threads=1  # 7 passed
rtk cargo test --manifest-path rust/Cargo.toml --test database_compatibility -- --test-threads=1  # 6 passed
rtk cargo test --manifest-path rust/Cargo.toml --test routing_domain -- --test-threads=1  # 12 passed
rtk cargo test --manifest-path rust/Cargo.toml --lib -- --test-threads=1  # 16 passed
rtk cargo test --manifest-path rust/Cargo.toml --test provider_transport -- --test-threads=1  # 29 passed
rtk uv run pytest tests/unit/test_quota.py tests/unit/test_routing.py tests/unit/test_routing_guardrails.py tests/integration/test_quota_cooldown.py tests/migration_rs/test_d001_routing_domain.py -q --tb=short --maxfail=1  # 125 passed
rtk git diff --check
```

The repository-wide Rust all-target command was not used as closure evidence
because the existing catalog-refresh fixture does not terminate in this
workspace when run through the non-interactive wrapper; its affected
provider-transport suite and all D004/database/routing Rust suites passed
individually. No live provider, inference request, or performance gate was
required for this state-only milestone.

## Unresolved findings and future-plan state

Unresolved mandatory findings by severity: none.

D004 is removed from the dependency-ready table and recorded in the completed
implementation table with commit `d649e8a`. The routing-domain roadmap,
implementation README, and handoff sequence now show D001-D004 closed and
D005 ready. This is the one future plan unblocked by D004 under the repository's
default serial handoff policy. D006 remains queued behind D005, D007 behind
D006, and D008 behind D001-D007. M6 planning may continue conceptually, but
M6 implementation handoff remains blocked on integrated D008 closure; M7 is
additionally blocked on M6.
