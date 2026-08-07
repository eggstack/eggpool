# Plan 087 — Weighted Routing Semantics

Date: 2026-08-07
Status: ready for implementation
Parent roadmap: `plans/086-sbc-routing-and-storage-efficiency-roadmap.md`
Depends on: none
Planning baseline: `d6c49dea5ed800bfcd22d95fe8c7943a29590125`

## Purpose

Make the existing per-account `weight` configuration produce a real, predictable routing effect without adding a second routing strategy or changing the existing quota/load signals.

Today `AccountConfig.weight` is stored and copied into `RoutingScore`, but `RoutingScore.final_score` does not consume it. Unequal weights therefore mainly prevent accounts from sharing the same fairness band; they do not implement meaningful weighted load distribution.

This plan corrects that semantic defect before Plan 088 changes concurrent claim visibility.

## Required reading

- `plans/086-sbc-routing-and-storage-efficiency-roadmap.md`
- `AGENTS.md`
- `src/eggpool/models/config.py`
- `src/eggpool/quota/estimation.py`
- `src/eggpool/quota/scorer.py`
- `src/eggpool/routing/router.py`
- account/config documentation that describes `weight`
- existing routing/quota/fairness tests found by searching for `QuotaFairScorer`, `weight`, `fairness_mode`, and `routing_priority`

## Governing decision

Use weight as a capacity/share multiplier, not as an unrelated additive score penalty.

For otherwise equivalent accounts, a larger positive weight should make the account able to absorb proportionally more load before its utilization score catches up with a lower-weight peer. The implementation should therefore apply weight to the effective request/token capacity or equivalently divide the utilization pressure by weight.

Do **not** multiply the final score by weight directly. A larger weight should make an account more preferred under equal observed load, not less preferred.

Do **not** use cost as the routing signal. Existing request-count, token-count, in-flight, health, priority-tier, native-protocol preference, and upstream-authoritative suppression semantics remain in place.

## Workstream A — Pin the public semantics

Document the exact operator contract in the closest existing configuration/routing documentation:

- `weight = 1.0`: baseline share;
- `weight = 2.0`: approximately twice the effective request/token capacity of a comparable `weight = 1.0` account;
- `weight = 0.5`: approximately half the effective capacity;
- priority tiers still dominate weight across different tiers;
- health/circuit/quarantine eligibility still dominates scoring;
- provider-native preference still applies where configured;
- weight influences routing only among otherwise eligible accounts.

Do not promise exact long-run request ratios when requests differ greatly in token size or providers have different health/capacity histories. Describe weight as a relative capacity/share hint.

## Workstream B — Correct the score calculation

Implement the smallest change in `src/eggpool/quota/scorer.py` that makes weight affect utilization consistently.

Preferred approach:

1. obtain the already-configured positive account weight from `AccountQuota`;
2. scale the effective request and token capacities used by `_calc_window_utilization()` by that weight, or perform the mathematically equivalent normalization on the resulting utilization;
3. preserve existing offset/reservation/incoming-token arithmetic;
4. preserve existing zero/nonconfigured capacity handling;
5. keep `RoutingScore.weight` populated for diagnostics;
6. do not introduce a new `weighted_score`, policy object, routing strategy enum, or persistent field.

Be careful with integer capacities. If capacities are scaled before division, avoid truncating a positive effective capacity to zero. A float denominator is acceptable inside scoring if it keeps semantics clear.

## Workstream C — Reconcile fairness-band behavior

Inspect `_fairness_band()` in `src/eggpool/routing/router.py`.

The current function requires matching weight before grouping accounts into a fairness band. After weight is represented in the score itself, this equality gate may be redundant or actively harmful: two accounts with different weights can legitimately reach the same normalized utilization score and should then be eligible for the same near-tie fairness treatment.

Required decision:

- remove the explicit `score.weight == best_score.weight` fairness-band gate unless a concrete invariant requires it;
- retain same-priority and same-native/transcode-status boundaries;
- retain existing epsilon semantics;
- keep round-robin/random fairness behavior otherwise unchanged.

Do not add special fairness state keyed by weight.

## Workstream D — Focused regression coverage

Extend existing routing/scorer tests. Do not create a new plan-specific test framework.

Required deterministic cases:

1. two equal accounts with `weight = 1.0` produce the same score as before;
2. with equal persisted/reserved/in-flight load, `weight = 2.0` produces lower utilization pressure than `weight = 1.0`;
3. with equal load, `weight = 0.5` produces higher utilization pressure than `weight = 1.0`;
4. when the higher-weight account accumulates roughly proportional load, scores converge near the expected relative-share point;
5. request-count and token-count dimensions both honor weight;
6. priority tier still dominates weight — a lower-priority high-weight account must not leapfrog a healthy eligible higher-priority tier;
7. health/circuit exclusion remains authoritative regardless of weight;
8. equal normalized scores with different configured weights may participate in the same fairness band if all other fairness requirements match;
9. default configs with all weights at `1.0` preserve routing order/near-tie behavior except for random/round-robin nondeterminism already present by design.

Use fixed inputs and deterministic fairness mode where necessary. Do not use statistical tests over thousands of requests.

## Workstream E — Documentation/config reconciliation

Update only documentation that actually describes account routing weight. Ensure examples do not imply cost weighting or hard capacity limits.

No config migration is required: existing positive float values remain valid.

## Verification

Run focused tests for scorer/router/account config first. Then:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
```

No live-provider test is required.

## Acceptance criteria

- [ ] `weight` materially affects quota/load routing score.
- [ ] `weight = 1.0` preserves existing baseline semantics.
- [ ] larger weight means proportionally more effective routing capacity, not an additive arbitrary preference.
- [ ] both request-count and token-count utilization honor weight.
- [ ] cost remains excluded from routing decisions.
- [ ] priority tiers, health/circuit/quarantine gates, and native-protocol preference retain existing precedence.
- [ ] fairness-band logic no longer rejects a near tie merely because configured weights differ, unless a concrete invariant is documented and tested.
- [ ] no new routing strategy/config field/persistence table is added.
- [ ] focused routing tests pass.
- [ ] standard smoke gate passes.
- [ ] operator documentation describes weight as a relative capacity/share hint.

## Rejection conditions

Do not close this plan if:

- weight is only copied into diagnostics but still does not affect selection;
- higher weight makes an otherwise identical account less preferred;
- implementation introduces a second weighted-routing strategy;
- cost becomes part of the route score;
- tests rely on probabilistic long-run ratios;
- priority or health eligibility semantics change;
- Plan 088 pending-claim machinery is implemented here.

## Implementation sequence for GPT-5.6 Luna

1. Inspect all uses of `.weight` in config, quota, scorer, router, dashboard/explain output, and tests.
2. Write/adjust focused scorer tests that expose the current no-op behavior.
3. Implement weight as effective capacity/share scaling in the smallest scoring location.
4. Remove/reconcile the fairness-band weight-equality condition if redundant after normalized scoring.
5. Run focused scorer/router tests.
6. Update concise routing/config documentation.
7. Run lint/type/smoke checks.
8. Record exact commands/results in this plan and mark complete only after all acceptance criteria are proven.