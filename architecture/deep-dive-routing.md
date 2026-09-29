# Deep Dive: Routing and Quota

Back to [Architecture](README.md)

See also the review index in [overview.md](overview.md) (§8).

## Ownership

`rust/src/routing/` owns deterministic provider/account selection. `rust/src/quota/` owns
windowed quota state and fair-share scoring. Authority files are `rust/src/routing/mod.rs`,
`router.rs`, `eligibility.rs`, `fairness.rs`, `claim.rs` and `rust/src/quota/mod.rs`,
`state.rs`, `estimator.rs`, `scorer.rs`.

`RoutingRouter` (`router.rs`) holds one generation of in-memory routing state: `AccountRegistry`,
shared `ModelCatalogCache`, `QuotaEstimator`, optional `HealthManager` and `ModelQuarantine`,
`EligibilityPolicy`, the claim book, `FairnessRotor`, and one async selection lock.
Construction is `new` or `with_shared_catalog`; the catalog lock is shared with
`CatalogService` so refreshes become visible to routing atomically. `with_quarantine`,
`with_random_source`, `with_missing_account_recovery`, and `with_recovery_clock` attach
optional collaborators without changing the selection contract.

Semantic model choice happens before this module: the neutral `eggpool-model-routing` crate
validates/compiles policy and `rust/src/model_router.rs` owns the Tokio affinity cache. A
selector resolves a virtual model to one concrete model; it cannot pin an account, bypass
health/quota, or reselect after submission.

## Eligibility gates

`eligibility.rs::build_eligible_candidates` is the read-only gate. `EligibilityPolicy::from_config`
derives it from `RoutingConfig` (`local_quota_mode`, `fairness_mode`, `fairness_epsilon`,
`fairness_scope`, `near_tie_epsilon`) plus `TranscoderPolicy` (`prefer_native`,
`capability_policy`). `RoutingRequestFacts::from_model_id` splits an optional
`provider/model` suffix with `ModelCatalogCache::parse_model_provider`.

Per account, `candidate_for_account` checks, in order: enabled, usable credentials,
requested provider, request surface (`RequestSurface`), requested/transcodable protocol,
read-only health (`is_model_healthy_read_only`), exact quarantine keys, catalog support,
freshness (`account_model_is_fresh`), thinking capability policy, and `LocalQuotaMode::HardCap`
(`AccountQuota::is_within_limits`). Failures emit bounded `RoutingExclusion` reason codes
(`disabled`, `auth_failed`, `wrong_provider`, `no_surface`, `no_protocol`, `circuit_open`,
`cooldown`, `rate_limited`, `quota_exhausted`, `model_quarantined`, `no_model`, `model_stale`,
`thinking_unsupported`, `thinking_unknown`, `protocol_mismatch`, `probe_unavailable`,
`malformed_score`). Request-provided capability policy overrides configured policy only when
non-empty; scoring collections stay `BTreeMap`-ordered.

## Scoring (load-based, never cost-based)

`QuotaFairScorer` (`scorer.rs`) is pure: it snapshots estimator state once and never does
SQLite/network I/O. `RoutingScore::final_score` is `quota_score + inflight_penalty +
health_penalty` (`INFINITY` when ineligible). Policy constants are `ScoringPolicy`
(`mean_weight`, `inflight_penalty_per_request`, `health_penalty_value`, `near_tie_epsilon`,
`prefer_native`).

The router hot path uses the ordered pair `QuotaEstimator::snapshot_ordered` plus
`QuotaFairScorer::score_ordered`: borrowed caller-ordered names, one estimator lock, one
request-wide `projected_tokens` scalar, zero health penalty, scores aligned by index with the
eligible `Vec<RoutingCandidate>`. Missing accounts score exactly like the public
`score_accounts` empty case. `rank_accounts` and `near_ties` remain available and numerically
equivalent. `AccountQuota::utilization` blends 5h/weekly/monthly request/token pressure;
`ScoringPolicy` weights and native preference break ties before account-name ordering.
Priority tiers sort strictly first: higher `routing_priority` always beats load, and fairness
applies only inside one priority band.

`QuotaEstimator` (`estimator.rs`) mirrors durable usage plus local ownership:
`add_pending_claim`, `release_pending_claim`, `convert_pending_claim`, `add_reservation`,
`remove_reservation`, `record_usage`, `hydrate_usage_windows`, `estimate_cost`,
`configure_policy`, `get_account_quota`. Estimates use per-account/model EWMA, then global
EWMA, then configured overrides, then family fallback, then a bounded global fallback; costs
are reservation sizing only and never steer selection toward cheaper providers.

## Fairness

`fairness.rs` partitions a `FairnessRotor` by `FairnessKey` (`provider_id`, `model_id`,
`protocol`, `priority`, `client_protocol`; `FairnessKey::to_key_string` renders it).
`FairnessMode` is `Off`, `RoundRobin`, or `Random`; `FairnessScope` is
`ProviderModelProtocol`, `ProviderModel`, or `PriorityModelProtocol`. Only near-tied
candidates (within `fairness_epsilon`, defaulting to `near_tie_epsilon`) in the top priority
band rotate. `order_named` previews without mutating; `commit` advances only after a claim
owns all local state. `preview`/`preview_named` keep `build_routing_plan` and
`has_eligible_pairing` side-effect free. Keys are LRU-bounded by `FAIRNESS_KEY_HARD_CAP`
(4,096); randomness is injected via `FairnessRandom::choose_index`
(`DeterministicFairnessRandom` by default). `FairnessDecision` records mode, application,
key, scope, width, anchor score, and ordered accounts for secret-free traces.

## Selection claim transaction

`select_and_claim_with_preference` (via `select_and_claim` and the alternate-wire-only
`select_and_claim_for_account`) holds one async mutex across a synchronous critical section:
snapshot active counts, build candidates, apply exclusions/preference, read-only probe check,
fairness order, `try_acquire_request`, `estimate_cost`, `add_pending_claim`, and
`claim::publish`. No provider, SQLite, or network await enters after acquisition.

`SelectionClaim` (`claim.rs`) is an explicit token with no `Drop` side effects. Terminal
paths are `rollback_claim`, `convert_claim_after_durable_publication`,
`release_active_claim`, `release_quota_reservation`, and combined `release_all`.
`SelectionSnapshot` (`RoutingDecisionTrace`) carries only bounded routing metadata for
durable publication via `trace_for`; request bodies, credentials, and provider error text
never enter it. `publish` fails closed on a poisoned claim book (`ClaimError::Poisoned`);
duplicate finalization returns `ClaimTransition::AlreadyTransitioned` instead of double
subtracting. `record_success` and `apply_failure_effects` apply only the narrow typed
health transitions owned at that boundary; model-scoped failures never advance the account
circuit.

## Invariants

- Selection is deterministic and load-based; pricing/accounting never influences ranking.
- Selectors resolve models only; they cannot pin accounts, bypass health/quota, or reselect
  after submission.
- Quarantine, backoff, capability, and freshness gates run before scoring and stay scoped to
  the provider/model facts that produced them.
- Every claim is released exactly once per ownership bit on all terminal paths.
- Read-only plan/readiness paths never advance the fairness rotor or acquire probes.
- Failures fail closed (poisoned locks, unknown accounts, underflow) without silently
  clamping ownership counters.
