# D004 — Quota, Claims, and Fair-Share Scoring

Status: queued behind D003 closure

Source roadmap: `migration-rs/subsystems/routing-domain-roadmap.md#d004--quota-reservation-mirrors-claims-and-fair-share-scoring`

Primary class: capability/invariant

## 1. Objective

Port the quota/load state that feeds account ranking, including persisted usage windows, configured capacities/offsets/weights, bounded learned estimates, pending and reserved ownership mirrors, local claim conversion/release invariants, and Python's request/token fair-share score.

D004 must not port the deprecated in-memory `ReservationManager` as production architecture. The existing SQLite reservation representation plus the estimator's bounded in-memory mirrors remain canonical.

## 2. State model

Implement typed Rust equivalents for the routing-relevant `AccountQuota`, persisted window snapshot, bounded EWMA estimate, and quota estimator state.

Preserve at minimum:

- account weight;
- 5h/7d/30d persisted cost/request/token observations;
- configured cost/request/token capacities for all horizons;
- configured per-window offsets;
- pending request/token/cost ownership;
- reserved request/token/cost ownership;
- bounded account/model and global model EWMAs;
- configured model/provider price overrides used only for reservation sizing/audit;
- stable diagnostic snapshot fields.

Cost must not become a hidden routing-score term. Routing pressure is request count + token count; cost remains reservation/accounting metadata.

## 3. Default capacities and utilization

Preserve Python's current soft defaults when explicit request/token capacities are absent:

- requests: 2,500 / 5h, 35,000 / 7d, 150,000 / 30d;
- tokens: 500,000,000 / 5h, 7,000,000,000 / 7d, 30,000,000,000 / 30d.

For each window:

```text
request_util = max(0, used_requests + reserved_requests + pending_requests
                      + incoming_request + request_offset)
               / (request_capacity * effective_weight)

token_util = max(0, used_tokens + reserved_tokens + pending_tokens
                    + incoming_tokens + token_offset)
             / (token_capacity * effective_weight)

window_util = max(request_util, token_util)
```

The routing base score remains:

```text
max(p5, p7, p30) + mean_weight * mean(p5, p7, p30)
```

then add active-request/inflight and health penalties. Default values must match the D001 oracle (`mean_weight`, inflight penalty, health penalty, near-tie range, native preference).

Zero/invalid capacity that reaches runtime despite config validation must produce a non-finite/ineligible score rather than division panic or accidental preference.

## 4. Score-only vs hard-cap semantics

Expose `is_within_limits`/remaining-capacity behavior for D006, but preserve the policy distinction:

- default `score_only`: above-local-capacity accounts remain candidates with high utilization;
- explicit `hard_cap`: local capacity exhaustion becomes an eligibility exclusion;
- provider-observed authoritative quota exhaustion is health state owned by D005 and excludes regardless of local score mode.

Exact capacity (`used == capacity`) is exhausted for hard-cap checks.

Do not convert cost estimates into a hard gate unless Python currently does so for the configured local quota mode.

## 5. Persisted usage windows

Extend the Rust repository layer to read the existing schema's usage-window/rollup data exactly as Python's `UsageWindowRepository` does for routing snapshots. Prefer one batched read for all enabled accounts rather than N per-account queries.

Hydrate request/token/cost snapshots with account durable IDs and a controlled loaded-at timestamp. Missing 7d/30d persisted data falls back exactly as the oracle specifies; do not substitute the 24h in-memory window for a missing 7d window because that biases routing.

D004 does not own periodic rollup maintenance. M8 later owns background maintenance scheduling. Tests may seed the durable tables directly.

## 6. Pending claim ownership

Port the Python provisional-claim invariant as synchronous/local state operations suitable for D006's selection critical section:

- `add_pending_claim(account, tokens, cost)` increments exactly one request plus projected tokens/cost;
- `release_pending_claim(...)` requires sufficient ownership and surfaces underflow as an invariant error;
- `convert_pending_claim(...)` replaces one pending claim with one reserved mirror without exposing a moment when neither/both representations are counted;
- diagnostic `reserved_*` mirrors include pending + reserved ownership exactly as Python does;
- all counters use bounded/clamped integer arithmetic compatible with SQLite integer limits.

D004 does not create a durable request or reservation row during selection. D006 publishes a local pending claim; M7 will perform the durable transition and then call conversion/release operations.

## 7. Reservation mirror API

Port bounded add/remove reservation operations used after durable publication:

- add/remove cost;
- request count (normally one per active reservation);
- projected token count;
- batched snapshot reads for scoring;
- no negative counters;
- underflow policy matching the oracle (explicit invariant failure for pending ownership; bounded removal for already-durable mirrors where Python clamps).

Keep a narrow synchronization boundary around reservation/persisted-snapshot mutation. Scoring many candidates should acquire the state lock once and copy the needed small aggregate snapshot rather than await/lock per account.

## 8. Reservation cost estimation

Port the bounded estimate hierarchy only because M7 will need it for durable reservation sizing and D006 needs projected audit state:

1. account/model EWMA;
2. global model EWMA;
3. configured provider/model override;
4. model-family fallback;
5. global unknown fallback.

Preserve current outlier rejection, first-seed guard, safety factor, per-token ceiling, absolute reservation ceiling, non-negative/SQLite integer clamps, and fallback behavior.

Bound learned maps at least as tightly as Python:

- account/model EWMA hard cap: 4,096 effective entries/bounded buckets per the frozen oracle;
- global model EWMA hard cap: 1,024;
- deterministic LRU/touch behavior under D001 fixtures.

Do not persist raw request bodies or prompts to improve estimation.

## 9. Incoming projected tokens

D004 accepts a caller-supplied non-negative projected token count. It does not parse an inference payload because canonical request/token estimation belongs to M6/M7.

Use zero as the safe unknown projection where Python does. D006's `RoutingRequestFacts` supplies the estimate when available.

## 10. Deterministic ranking

Implement `RoutingScore` and scorer outputs with all traceable components required by D006/diagnostics:

- account name;
- quota/base score;
- weight;
- eligible flag;
- inflight and health penalties;
- request/token/cost usage and capacities by window;
- pending/reserved load;
- active request count;
- tier placeholder;
- `requires_transcode` annotation.

`rank_accounts` must be deterministic: final score, native/transcode preference when enabled, then account name as the stable tie-break anchor. Fairness rotation among near ties belongs to D006, not to a random scorer call.

If legacy `select_account` compatibility is retained for tests, seed/inject its randomness and do not use it as the production D006 path.

## 11. Concurrency tests

Test at minimum:

- two concurrent snapshots see pending load published by the first completed claim;
- pending add/release/convert cannot lose counts;
- conversion never double-counts one request;
- cancellation before local claim commit leaves counters unchanged;
- explicit rollback restores baseline;
- invariant underflow is surfaced;
- concurrent reservation add/remove and persisted-window update do not lose increments;
- scoring does not hold a lock across SQLite I/O;
- bounded EWMA eviction remains at cap under adversarial model/account churn.

Use deterministic barriers rather than sleeps where practical.

## 12. Differential cases

Compare Python/Rust for:

- default and explicit capacities;
- weight scaling;
- positive/negative offsets;
- exact/above-capacity behavior in score-only and hard-cap modes;
- request-heavy vs token-heavy accounts;
- reserved + pending + incoming load;
- active-request penalty;
- health penalty input;
- non-finite malformed capacity;
- stable ranking/native preference;
- EWMA tier selection/outlier/cap/eviction;
- reservation cost ceilings;
- Python DB usage snapshot -> Rust hydrate.

Float comparison tolerance must be explicitly frozen by D001 and tight enough that it cannot alter fairness-band membership unnoticed.

## 13. Acceptance criteria

D004 closes only if:

- request/token pressure, not cost, drives score parity;
- local quota remains advisory by default;
- pending claims become visible to subsequent scoring before the D006 claim lock is released;
- pending->reserved conversion has no double/missing ownership window;
- underflow/counter corruption cannot silently pass;
- usage hydration is batched and schema-compatible;
- EWMA/reservation estimates are bounded and memory-capped;
- deterministic rank matches the oracle;
- no request parsing, provider I/O, or retry/finalization enters this module.

## 14. Stop conditions

Do not close if Rust ports the deprecated in-memory ReservationManager as canonical state, uses cost as a routing signal, hard-gates local estimates by default, performs per-candidate SQLite reads, silently clamps a pending-claim ownership underflow, or leaves learned maps unbounded.