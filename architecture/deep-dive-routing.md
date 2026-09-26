# Deep Dive: Routing and Quota

Back to [Architecture](README.md)

`rust/src/routing/` and `rust/src/quota/` select eligible provider accounts
using health, availability, active load, quota reservations, routing priority,
and bounded fairness. Routing is load-based and never cost-based.

`rust/crates/eggpool-model-routing/` owns the neutral semantic policy boundary:
structural validation, deterministic compilation, route IDs, static selector
policy bytes, fingerprints, and hashed session identities. EggPool's
`rust/src/model_router.rs` retains the process-owned Tokio TTL/LRU/single-flight
affinity cache and adapts TOML config into the neutral policy types. A selector
chooses a concrete model before ordinary provider routing; it cannot pin an
account, bypass health/quota, or reselect after submission.

Claims and reservations are released on every terminal path (`rollback_claim`,
`convert_claim_after_durable_publication`, `release_active_claim`,
`release_quota_reservation`). Quarantine, backoff, and capability gates are
evaluated before selection and remain scoped to the provider/model facts that
produced them.

Eligibility selects the effective capability policy once by reference before
the account iteration. Request-provided policy overrides configured policy,
empty request policy falls back to configuration, and deterministic scoring
collections remain ordered `BTreeMap`-based. The selection lock is an async
mutex held across one synchronous claim/quota/fairness critical section
(`select_and_claim_with_preference`); no provider, SQLite, or network await
enters it after acquisition.

Router scoring uses a private ordered path (routing-selection M001):
`QuotaEstimator::snapshot_ordered` borrows caller-ordered account names and
snapshots state once under one estimator lock, returning one entry per
account (preserving the missing-account case) without a String-keyed result
map. `QuotaFairScorer::score_ordered` feeds those snapshots through the same
`score_one` core as public `score_accounts`, reading active counts directly
from the existing snapshot and keeping `projected_tokens` as one request
scalar with zero health penalty. Scores align by index with the eligible
`Vec<RoutingCandidate>`, whose ownership moves into the final scored Vec —
no `BTreeMap<String, RoutingCandidate>` reindex or candidate clone-back.
Public `score_accounts`/`rank_accounts`/`near_ties` remain available and
numerically equivalent; the final deterministic comparator, fairness,
selection lock, claim, health, quota, exclusion, and trace behavior are
unchanged.

## Shared crate contract

`eggpool-model-routing` is a neutral policy boundary: structural validation,
deterministic compilation, route IDs, bounded static selector policy bytes
(`model-router/v1`, 64 KiB cap), fingerprints, and hashed session identities.
Downstream consumers adapt their local config into the neutral policy types
and retain selector execution plus concrete-provider selection. Public Rust
API breaks and policy-byte/fingerprint semantic changes require explicit
compatibility review and may require a selector protocol-version decision.
Provider/account routing, quota, health, retry, and transport remain outside
the shared crate. See [Data models](deep-dive-models.md) for the
crate-vs-affinity ownership split.
