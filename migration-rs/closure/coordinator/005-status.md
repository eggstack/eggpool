# C005 Closure — Failure Effects, Retry Budget, and Failover

Status: closed

Recommendation: closed; C006 is complete and C007 is now dependency-ready after its accepted closure.

Implementation commit: [`97a4846`](https://github.com/eggstack/eggpool/commit/97a48464b775514f90d36d021607c091881a36d3)

Plan: [C005 — failure effects, retry budget, and failover](../../implementation/coordinator/005-failure-effects-retry-and-failover.md)

Repository baseline: `9b730c59`

## Outcome

C005 adds typed failure observations, categories, retry scope, next action,
bounded retry policy, Retry-After parsing/capping, and an attempt-keyed effect
ledger. The decision engine keeps account failover, wire negotiation, rate
limit waiting, and terminal completion distinct. A response-start fact is a
hard no-replay boundary.

## Requirement-to-evidence matrix

| C005 requirement | Evidence | Result |
|---|---|---|
| Typed bounded decision inputs | `FailureObservation` contains structural status/signal facts only | Pass |
| Account/wire scope separation | `FailureEffects`, `RetryScope`, and boundary test | Pass |
| No post-handoff retry | `classify` gates all retry actions on `response_started`; focused test | Pass |
| Retry budget | `RetryPolicy::max_attempts` and attempt-number gating | Pass |
| Retry-After numeric/date handling | bounded numeric parser and RFC1123 parser, capped at 1,800 seconds | Pass |
| Exactly-once effect application | `EffectLedger` and `FailureDecisionEngine` keyed by attempt ID | Pass |
| No provider-body retention | observations use optional bounded signal text only | Pass |

## Compatibility evidence

The category and action vocabulary follows the Python retry/failure boundary,
while M5 effects remain represented as explicit typed decisions rather than
being duplicated in transport. Deterministic Rust tests cover the required
scope and handoff invariants. Live provider and public endpoint differential
evidence are intentionally deferred to the later coordinator plans.

## Verification commands actually run

```text
rtk cargo fmt --all
rtk cargo test --test coordinator_boundaries -- --nocapture  # 3 passed
rtk cargo clippy --all-targets -- -D warnings                 # clean
rtk cargo test --all-targets                                  # passed
rtk git diff --check                                           # passed
```

No lower-layer retries, provider health writes, schema changes, or live traffic
were introduced.

## Future-plan audit and registry transition

C005 is removed from the active queue and recorded as completed. C006's
durable finalization boundary is accepted by its own closure record. C007 is
the sole dependency-ready successor; C008-C011 remain queued, and M8 remains
blocked on C011 plus its separate planning review.

Unresolved mandatory findings: none.
