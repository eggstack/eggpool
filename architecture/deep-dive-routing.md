# Deep Dive: Routing and Quota

Back to [Architecture](README.md)

See also the review index in [overview.md](overview.md) (§8).

## Ownership

`rust/src/routing/` owns deterministic provider/account selection. `rust/src/quota/` owns
windowed quota state and fair-share scoring. Authority files are `rust/src/routing/mod.rs`,
`router.rs`, `eligibility.rs`, `fairness.rs`, `claim.rs` and `rust/src/quota/mod.rs`,
`state.rs`, `estimator.rs`, `scorer.rs`.

`RoutingRouter` (`router.rs`) holds one generation of in-memory routing state: `AccountRegistry`,
shared `ModelCatalogCache`, `QuotaEstimator`, `HealthManager`, `ModelQuarantine`,
`DurableHealth`, `EligibilityPolicy`, the claim book, `FairnessRotor`, and one async
selection lock. Construction is `new` or `with_shared_catalog`; the catalog lock is shared
with `CatalogService` so refreshes become visible to routing atomically. `with_quarantine`,
`with_durable_health`, `with_random_source`, `with_missing_account_recovery`, and
`with_recovery_clock` attach optional collaborators without changing the selection
contract.

The health manager is not optional in the production generation: the build path always
supplies one, and the router treats an absent manager as fail-closed rather than as
"nothing to check". An account whose health cannot be evaluated is not treated as healthy,
so a router built without a manager selects nothing. The quarantine and the durable health
repositories are attached by the generation build path, which hydrates them from the
persisted rows before the generation is published.

Semantic model choice happens before this module: the neutral `eggpool-model-routing` crate
validates/compiles policy and `rust/src/model_router.rs` owns the Tokio affinity cache. A
selector resolves a virtual model to one concrete model; it cannot pin an account, bypass
health/quota, or reselect after submission.

Downstream per-user model authorization is a separate earlier gate in
`coordinator/endpoints.rs`. One immutable `ModelPolicySnapshot` is resolved for
the authenticated user and checks the requested ID, the virtual alias, the
selector model, and the resolved concrete route before ordinary account
selection. Exact `model/provider` rules are supported; instance denials win
over user overrides, which win over profile rules and defaults. An affinity
hit and selector repair use the same request snapshot. A policy denial returns
403 before the concrete provider attempt, while provider eligibility,
health, quota, and fairness remain owned by this routing module.

## Eligibility gates

`eligibility.rs::build_eligible_candidates` is the read-only gate. `EligibilityPolicy::from_config`
derives it from `RoutingConfig` (`local_quota_mode`, `fairness_mode`, `fairness_epsilon`,
`fairness_scope`, `near_tie_epsilon`) plus `TranscoderPolicy` (`prefer_native`,
`capability_policy`). `RoutingRequestFacts::from_model_id` splits an optional
`provider/model` suffix with `ModelCatalogCache::parse_model_provider`.

Per account, `candidate_for_account` checks, in order: enabled, usable credentials,
requested provider (`no_provider` when the account has no provider vs `wrong_provider`),
request surface (`RequestSurface`), requested/transcodable protocol,
read-only health (`is_model_healthy_read_only`), exact quarantine keys (exact upstream plus
canonical-only, `unknown` protocol partition for missing protocol), catalog support,
freshness (`account_model_is_fresh`), thinking capability policy, and `LocalQuotaMode::HardCap`
(`AccountQuota::is_within_limits`). Failures emit bounded `RoutingExclusion` reason codes
(`disabled`, `auth_failed`, `no_provider`/`wrong_provider`, `no_surface`, `no_protocol`,
`circuit_open`, `cooldown`, `rate_limited`, `quota_exhausted` (health-gate when the health
snapshot reports `quota_exhausted`, distinct from the `HardCap` estimator gate below),
`model_quarantined`, `no_model`, `model_stale`, `thinking_unsupported`, `thinking_unknown`,
`thinking_conflicting`, `thinking_toggle_unsupported`/`thinking_toggle_unknown`,
`thinking_effort_unsupported`/`thinking_effort_unknown`,
`thinking_budget_unsupported`/`thinking_budget_unknown`, `protocol_mismatch`,
`malformed_score`). `probe_unavailable` is not an eligibility code:
`router.rs::select_and_claim_with_preference` adds it after eligibility when
`try_acquire_request` loses the half-open probe race. Request-provided capability policy
overrides configured policy only when non-empty; scoring collections stay `BTreeMap`-ordered.

Two gates fail closed rather than open. The `LocalQuotaMode::HardCap` gate requires a
quota record: an account with no record cannot be shown to be within its limits, so it is
excluded with the same `quota_exhausted` reason code as a record that is over its limits.
`LocalQuotaMode::ScoreOnly` does not admit on quota grounds and is unaffected. The
read-only health probe requires a health manager for the same reason: an absent manager is
not the same fact as a manager reporting a healthy account.

A quota observation with a non-finite timestamp is rejected before any counter moves.
Such a timestamp cannot be ordered against the window's other observations and could never
be pruned, so admitting it would inflate the window permanently.

## Scoring (load-based, never cost-based)

`QuotaFairScorer` (`scorer.rs`) is pure: it snapshots estimator state once and never does
SQLite/network I/O. `RoutingScore::final_score` is `quota_score + inflight_penalty +
health_penalty` (`INFINITY` when ineligible). Policy constants are `ScoringPolicy`
(`mean_weight`, `inflight_penalty_per_request`, `health_penalty_value`, `near_tie_epsilon`,
`prefer_native`).

The router hot path uses the ordered pair `QuotaEstimator::snapshot_ordered` plus
`QuotaFairScorer::score_ordered`: borrowed caller-ordered names, one estimator lock, one
request-wide `projected_tokens` scalar, hard-zeroed health penalty (`scorer.rs:141` passes
`0.0`; `eligibility.rs:248-258` wires the ordered path), scores aligned by index with the
eligible `Vec<RoutingCandidate>`. Health acts via the read-only gate and breaker/probe, not
via score; the public `score_accounts` is the only path honoring `health_penalties`.
Missing accounts score exactly like the public `score_accounts` empty case. `rank_accounts`
sorts only `final_score` → `prefer_native` → name with no tier, so it is not order-equivalent
to the router sort; `near_ties` remains available. `AccountQuota::utilization` blends
5h/weekly/monthly request/token pressure. The router patches `score.tier` and
`requires_transcode` post-score (`eligibility.rs:276-278`) then sorts priority tier DESC →
`final_score` ASC → `prefer_native` → account name; higher `routing_priority` always beats
load, and fairness applies only inside one priority band.

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
`ProviderModelProtocol`, `ProviderModel`, or `PriorityModelProtocol`. Only the top-priority
near-tied band (within `fairness_epsilon`, defaulting to `near_tie_epsilon`) rotates; the
band also splits on `requires_transcode` when `prefer_native` is set. `Off` or fewer than 2
band members emits `FairnessDecision::not_applied` (`disabled`/`not_tied`). `RoundRobin`
rotates via the rotor (`order_named` preview, `commit` only after a claim owns all local
state); `Random` picks via `FairnessRandom::choose_index`, while preview (`apply=false`)
returns identity order with `applied=false`/`reason="preview"`. `build_routing_plan` and
`has_eligible_pairing` always preview, so they never advance the rotor or consume randomness.
Keys are LRU-bounded by `FAIRNESS_KEY_HARD_CAP`
(4,096); randomness is injected via `FairnessRandom::choose_index`
(`DeterministicFairnessRandom` by default). `FairnessDecision` records mode, application,
key, scope, width, anchor score, and ordered accounts for secret-free traces.

## Selection claim transaction

`select_and_claim_with_preference` (via `select_and_claim` and the alternate-wire-only
`select_and_claim_for_account`) holds `selection_lock` (one async mutex) across a
synchronous critical section: snapshot active counts, build candidates, apply
exclusions/preference, read-only probe check, fairness order, `try_acquire_request`,
`estimate_cost`, `add_pending_claim`, and `claim::publish`. There is no `.await` and no
provider, SQLite, or network operation after the lock is acquired.

`SelectionClaim` (`claim.rs`) is an explicit token with no `Drop` side effects. Terminal
paths are `rollback_claim`, `convert_claim_after_durable_publication`,
`release_active_claim`, `release_quota_reservation`, and combined `release_all`.
`SelectionSnapshot` (`RoutingDecisionTrace`) carries only bounded routing metadata for
durable publication via `trace_for`; request bodies, credentials, and provider error text
never enter it. `publish` fails closed on a poisoned claim book (`ClaimError::Poisoned`);
duplicate finalization returns `ClaimTransition::AlreadyTransitioned` instead of double
subtracting. A pending reservation and its half-open probe are acquired before `publish`,
so a refused publication compensates both before returning. The active-count decrement is
checked before the estimator is mutated under the same book lock, so a claim can never be
left `Pending` after a failed release. `record_success` and `apply_failure_effects` apply only the narrow typed
health transitions owned at that boundary; model-scoped failures never advance the account
circuit. `apply_failure_effects` runs on the failure path, after the claim has been
published, so the durable mirror it writes never happens inside the selection critical
section. It records the decision the live path applied rather than a recomputed one.

The backoff duration that effect applies is policy-derived, never an inline constant.
`apply_failure_effects` resolves the typed `BackoffReason` the classifier produced, reads
the escalation rung the health manager already tracks for that account (or, for a
model-scoped reason, for that model), and asks `health::compute_backoff_seconds` for the
bounded delay. The classifier's duration is the policy's retry-after hint input: for the
reasons whose policy consumes a provider hint it is the upstream hint, and for every other
reason the policy ignores it and derives the duration from its own base delay and rung. The
result never exceeds the cap the reason's policy declares, and a reason whose policy has no
timed backoff produces none. The escalation state is therefore the health manager's
existing counters, never a second map inside this module, and no await is added to the
selection lock.

`with_missing_account_recovery` rejects a non-finite or non-positive interval instead of
coercing it: `f64::max` ignores NaN, so a NaN interval silently became "no interval at
all" and let every request re-probe a missing account immediately. A rejected value leaves
the previously configured interval in place. Both recovery readers take the effective
interval from one helper, so the attempt-retention window and the attempt-allowance
check cannot disagree about the same configured value.

User budgets are enforced later at the durable publication boundary, after the
principal and concrete model are bound. `coordinator/publication.rs` checks
every matching aggregate and exact-model UTC day/month rule and inserts a
SQLite hold atomically with the request, attempt, and provider reservation.
This is independent from account quota scoring and does not change the
load-based selection order. Token holds use a bounded request-size estimate;
microdollar holds use the existing estimated cost projection, including its
fallback. They are not exact output-token or invoice caps.

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
- The live backoff duration is the reason's policy, bounded by that policy's cap, and the
  escalation rung is the health manager's existing per-account and per-model counter.
- A durable health mirror never widens the live window: the persisted expiry is the
  deadline the live path applied, and a terminal withdrawal is never persisted as a
  terminal row.

## Verification

```bash
cargo test --manifest-path rust/Cargo.toml --test routing_domain -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test routing_claims -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test backoff_policy_adoption -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test quota -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test model_router -- --test-threads=1
```
