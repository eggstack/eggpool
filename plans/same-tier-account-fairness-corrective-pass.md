# Same-Tier Account Fairness Corrective Pass

## Context

Live testing still shows severe account skew after the lock/publication fixes: account `0001` received roughly 1000 requests while `0002` received 4 and `0003` received none. That distribution is unacceptable when those accounts are intended to be same-priority, same-weight, equally healthy, and equally model-eligible.

The prior fixes were necessary but not sufficient:

- The stale-publication race was fixed: the selected account's active count and reservation are now published under `_select_lock` after the durable transaction commits and before another selector can enter.
- `accounts explain` now uses real persisted catalog state and runs migrations before hydration.
- `routing_decisions.score_components_json` records score diagnostics.

The remaining issue is policy-level fairness. The hot coordinator path uses `Router.select_accounts_for_failover()`, ranks candidates, then chooses the first circuit-breaker-accepted candidate. That path uses `QuotaFairScorer.rank_accounts()`, not the simpler `select_account()` random near-tie path. If scores are not in the near-tie range, or candidate ordering is stable, account `0001` can remain structurally advantaged.

For subscription aggregation, same-tier peer accounts need a stronger fairness guarantee than probabilistic near-tie randomization. Equal-priority/equal-weight/equally-eligible accounts should be selected by bounded round-robin or least-recently-used fairness, while quota scoring should still steer away from genuinely more-used or unhealthy accounts.

## Goals

1. Prevent starvation among same-priority, same-weight, equally healthy, equally model-eligible accounts.
2. Add deterministic same-tier fairness for equal peers: `0001 -> 0002 -> 0003 -> 0001` style rotation when accounts are effectively tied.
3. Preserve priority-tier semantics: higher `routing_priority` providers still win before lower-priority providers.
4. Preserve quota-aware behavior when accounts are meaningfully not tied.
5. Preserve native-protocol preference when configured.
6. Make skew explainable in `routing_decisions.score_components_json`.
7. Add regression tests that would fail on a 1000/4/0 distribution.
8. Keep the implementation in memory and lightweight; no new DB table is required for the first pass.

## Non-goals

1. Do not make routing globally random across different priority tiers.
2. Do not route to ineligible accounts for the sake of fairness.
3. Do not ignore explicit weights. Weight must remain a first-class policy input.
4. Do not add durable round-robin state in this pass. Restart-reset rotor state is acceptable.
5. Do not remove quota scoring. Add fairness around same-tier effective ties.
6. Do not expand dashboard UI in this pass unless a small JSON/API field is already trivial.

## Phase 0: Confirm whether this is eligibility skew or score-policy skew

Before implementing the rotor, add a targeted diagnostic checklist for the live deployment.

Operators should run:

```bash
eggpool accounts status
eggpool accounts explain --model '<hot-model>' --protocol openai
```

Then inspect recent routing decisions:

```sql
SELECT
  selected_account_name,
  eligible_count,
  scored_count,
  top_score_account_name,
  selected_score,
  score_components_json
FROM routing_decisions
ORDER BY id DESC
LIMIT 50;
```

Interpretation:

- If `eligible_count = 1` or `scored_count = 1`, this is not a fairness problem. Accounts `0002` and `0003` are being excluded by catalog, provider, protocol, health, quota hard-cap, missing credentials, or model staleness.
- If all three accounts are scored but `top_score_account_name` is almost always `0001`, score policy is overpowering peer fairness.
- If all three accounts are scored, score deltas are tiny, and `0001` still dominates, the rank/failover path is preserving stable account order or not applying tie fairness strongly enough.

This phase is operational only, but the implementation should make this easier by adding trace fields in later phases.

## Phase 1: Add routing fairness config

File: `src/eggpool/models/config.py`

Extend `RoutingConfig` with a small set of fairness controls.

Recommended fields:

```python
fairness_mode: Literal["off", "round_robin", "random"] = "round_robin"
fairness_epsilon: float | None = None
fairness_scope: Literal[
    "provider_model_protocol",
    "provider_model",
    "priority_model_protocol",
] = "provider_model_protocol"
```

Semantics:

- `off`: existing behavior, useful for debugging or strict lowest-score routing.
- `round_robin`: deterministic rotor across the fairness band. This should be the default for subscription aggregation.
- `random`: choose randomly within the fairness band.
- `fairness_epsilon = None`: use `near_tie_epsilon`.
- `provider_model_protocol`: separate rotor per provider, model, protocol, and priority tier.
- `provider_model`: separate rotor per provider, model, and priority tier, regardless of endpoint protocol.
- `priority_model_protocol`: useful when multiple providers in the same priority tier intentionally share a model and should be co-balanced.

Keep defaults backwards-safe but skew-resistant. The recommended default is `round_robin` because current observed behavior proves probabilistic near-tie handling is insufficient.

Update `config.example.toml` with comments explaining the new settings.

Example:

```toml
[routing]
# Same-tier fairness for effectively tied accounts. Keeps equal subscription
# accounts from starving behind stable config order.
fairness_mode = "round_robin"  # off | round_robin | random
# Defaults to near_tie_epsilon when omitted.
# fairness_epsilon = 0.1
fairness_scope = "provider_model_protocol"
```

## Phase 2: Add a lightweight FairnessRotor

Create a small utility, preferably in a routing-focused module:

- `src/eggpool/routing/fairness.py`, or
- inside `src/eggpool/routing/router.py` if keeping the surface minimal is preferable.

Recommended types:

```python
@dataclass(frozen=True, slots=True)
class FairnessKey:
    provider_id: str | None
    model_id: str
    protocol: str | None
    priority: int
    client_protocol: str | None = None

@dataclass(slots=True)
class FairnessDecision:
    mode: str
    applied: bool
    key: str
    candidate_count: int
    selected_index: int | None
    selected_account_name: str | None
    reason: str
```

Recommended rotor implementation:

```python
class FairnessRotor:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._positions: dict[FairnessKey, int] = {}

    async def order(
        self,
        key: FairnessKey,
        candidates: list[tuple[AccountRuntimeState, RoutingScore]],
    ) -> tuple[list[tuple[AccountRuntimeState, RoutingScore]], FairnessDecision]:
        ...
```

Round-robin algorithm:

1. Sort the candidate group by a stable account identifier, preferably account name, so rotor behavior is deterministic and not dependent on config insertion order.
2. Read current position for the key, default `0`.
3. Rotate the sorted list by `position % len(group)`.
4. Increment stored position by `1` modulo group length.
5. Return rotated group and decision metadata.

Important implementation details:

- Only call the rotor for the fairness band, not the full candidate list.
- Hold the rotor lock only while reading/updating the integer position.
- The rotor must be in-memory. Restart resets are acceptable.
- Cap `_positions` to avoid unbounded cardinality if model IDs vary heavily. A simple hard cap such as 4096 keys is enough. Evict oldest or clear the map when over cap; document this.
- Do not perform DB I/O inside the rotor.

## Phase 3: Define the fairness band

Implement a helper in `Router` or `QuotaFairScorer` that extracts the fairness band from a ranked/scored tier.

Inputs:

- ranked candidates from a single priority tier
- model id
- provider id / resolved provider id
- protocol / client protocol
- config-derived fairness mode and epsilon

Band membership should require:

1. Same `routing_priority` tier.
2. Same `requires_transcode` value when `prefer_native` is enabled.
3. Same or effectively same weight unless implementing weighted round-robin in this pass.
4. Final score within `fairness_epsilon` of the best score.
5. Candidate remains eligible and has finite score.

First-pass recommendation: only apply round-robin to same-weight peers. If weights differ, keep score order and record `fairness.applied = false`, `reason = "different_weights"`.

Pseudo-code:

```python
def _fairness_band(
    ranked: list[tuple[AccountRuntimeState, RoutingScore]],
    *,
    epsilon: float,
    prefer_native: bool,
) -> tuple[list[tuple[AccountRuntimeState, RoutingScore]], list[tuple[AccountRuntimeState, RoutingScore]], str]:
    if len(ranked) < 2:
        return [], ranked, "single_candidate"

    best_state, best_score = ranked[0]
    if not math.isfinite(best_score.final_score):
        return [], ranked, "non_finite_score"

    band = []
    for state, score in ranked:
        if state.routing_priority != best_state.routing_priority:
            break
        if prefer_native and score.requires_transcode != best_score.requires_transcode:
            break
        if abs(score.weight - best_score.weight) > 1e-9:
            break
        if abs(score.final_score - best_score.final_score) > epsilon:
            break
        band.append((state, score))

    if len(band) < 2:
        return [], ranked, "not_tied"
    return band, ranked[len(band):], "ok"
```

Then order as:

```python
band, rest, reason = _fairness_band(...)
if band and fairness_mode == "round_robin":
    band, decision = await self._fairness_rotor.order(key, band)
elif band and fairness_mode == "random":
    random.shuffle(band)
    decision = ...
else:
    decision = FairnessDecision(applied=False, reason=reason, ...)
return band + rest
```

## Phase 4: Integrate fairness into the actual hot path

The hot path currently uses:

- `RequestCoordinator._select_and_persist_attempt()`
- `Router.select_accounts_for_failover()`
- `QuotaFairScorer.rank_accounts()`
- then coordinator picks first circuit-breaker-accepted candidate.

Therefore fairness must apply inside `Router.select_accounts_for_failover()`, not only inside `Router.select_account()`.

Change `Router.select_accounts_for_failover()` so after scoring/ranking one priority tier, it reorders the top fairness band before appending candidates to the result.

Do not rely only on `QuotaFairScorer.select_account()`. That is not the coordinator's primary path.

Implementation details:

- Add `Router` constructor parameters or setters for fairness config:
  - `fairness_mode`
  - `fairness_epsilon`
  - `fairness_scope`
- Wire these from `create_app()` in `src/eggpool/app.py` after constructing `Router`.
- Prefer storing the rotor on `Router`, not `RequestCoordinator`, because fairness is a routing policy concern and tests can exercise it directly.
- Ensure failover still works. If the first rotated candidate is rejected by circuit breaker, the coordinator should continue to the next candidate in the rotated order.
- Keep tier boundaries intact. Do not rotate a lower-priority candidate ahead of a higher-priority candidate.

## Phase 5: Make fairness visible in routing traces

Extend `score_components_json` to include fairness metadata.

Recommended payload additions:

```json
{
  "fairness": {
    "mode": "round_robin",
    "applied": true,
    "scope": "provider_model_protocol",
    "key": "provider=opencode-go|model=...|protocol=openai|tier=0",
    "candidate_count": 3,
    "selected_index": 1,
    "selected_account_name": "0002",
    "reason": "ok"
  }
}
```

Also add candidate-level fields to `top_candidates` if not already present:

- `rank_before_fairness`
- `rank_after_fairness`
- `fairness_band_member: bool`

If carrying both ranks is too invasive, at minimum include the fairness metadata object at the selected decision level.

The purpose is operational: when an account still skews, the trace should say whether fairness was disabled, skipped because scores were not close enough, skipped because weights differed, or applied but candidates were rejected by the circuit breaker.

## Phase 6: Add operator diagnostics for skew

Enhance `eggpool accounts explain` or add a narrow diagnostic subcommand.

Option A: extend `accounts explain` output with optional `--scores`:

```bash
eggpool accounts explain --model '<model>' --protocol openai --scores
```

Output additional columns:

- priority
- weight
- model/provider protocol
- current active count
- reserved microdollars
- score if cheaply computable

Option B: add `eggpool routing explain` later. For this pass, avoid expanding CLI scope too far. The trace JSON may be sufficient.

Minimum requirement for this pass: document the SQL query operators should run and ensure `score_components_json.fairness` exposes the needed data.

## Phase 7: Add regression tests for same-tier fairness

Tests must fail under the current 1000/4/0 behavior.

Recommended tests in `tests/unit/test_routing_coordinator_concurrent.py` or a new routing fairness test file.

### Test 1: direct router round-robin ordering

```python
async def test_select_accounts_for_failover_round_robins_equal_peers():
    ...
```

Setup:

- 3 enabled accounts: `0001`, `0002`, `0003`
- same provider
- same model support
- same priority
- same weight
- same protocol/native status
- no health penalties
- no persisted usage differences
- fairness_mode = `round_robin`
- fairness_epsilon high enough to include all three

Call `select_accounts_for_failover()` repeatedly and take the first candidate each time.

Expected:

- First 6 selected accounts should rotate through all three accounts.
- Do not assert exact starting account unless the implementation intentionally starts at lexical first. Better assert windows of length 3 contain all 3 accounts.

### Test 2: coordinator distribution under sequential load

Simulate 300 selections through `_select_and_persist_attempt()` without finalizing immediately, or finalize after each attempt depending on intended production pattern.

Two variants are useful:

- With outstanding in-flight requests: active/reservation penalties plus rotor should distribute strongly.
- With immediate finalization: rotor alone should still distribute equal peers.

Expected distribution for 300 requests across 3 equal accounts:

- every account receives at least 20%
- no account receives more than 45%

For stricter round-robin, expect exactly or near-exactly 100 each if no circuit breaker rejection occurs.

### Test 3: priority tier isolation

Setup:

- two high-priority accounts, one low-priority account
- all equal otherwise

Expected:

- high-priority accounts rotate with each other
- low-priority account receives 0 while high tier remains eligible

### Test 4: different weights do not use equal-peer rotor

Setup:

- accounts weights 1.0, 2.0, 1.0

Expected:

- equal-peer rotor either applies only to the weight-1.0 subset when it is the top fairness band, or skips with `reason = "different_weights"` if the best/runner-up weights differ.
- Document whichever behavior is implemented.

### Test 5: circuit breaker skip preserves rotation

Setup:

- three equal accounts
- circuit breaker rejects the current rotor-selected first candidate

Expected:

- coordinator selects the next rotated candidate
- fairness trace records the fairness-applied band and the circuit-breaker exclusion.

## Phase 8: Add a live-skew-oriented integration test

Add an integration-style test that mirrors the observed failure:

- account names: `0001`, `0002`, `0003`
- same provider, same priority, same weight
- same model support
- 1000 simulated requests, or fewer if runtime is a concern

Acceptance bounds:

- For 300 requests: each account gets between 80 and 120 if strict round-robin with immediate finalization.
- For 1000 requests: each account should be roughly 333. Accept a broad range such as 250-400 if randomness is allowed; for round-robin, assert exact or nearly exact distribution.

Keep test runtime reasonable. If 1000 is expensive, use 300 but name the test after the production skew.

Example assertion:

```python
assert min(counts.values()) >= attempts * 0.20
assert max(counts.values()) <= attempts * 0.45
```

For `round_robin`, prefer stronger bounds:

```python
assert max(counts.values()) - min(counts.values()) <= 1
```

when finalization resets active/reservation between attempts.

## Phase 9: Tune defaults and explain interaction with quota scoring

Update docs to clarify routing behavior:

- `routing_priority` selects the tier.
- eligibility filters determine who can serve the request.
- quota score ranks meaningfully different accounts.
- fairness rotor rotates effectively tied same-tier peers.
- weights affect quota capacities and may opt accounts out of equal-peer rotation.

Docs to update:

- `README.md`
- `architecture/README.md`
- `AGENTS.md`
- `config.example.toml`
- `CHANGELOG.md`

Important wording:

> EggPool is not purely lowest-score-wins for same-tier peer accounts. When accounts are effectively tied by priority, weight, health, protocol, and utilization score, same-tier fairness rotates candidates to avoid stable config-order bias and subscription starvation.

## Phase 10: Operational validation after implementation

After deploying the patch, run:

```bash
eggpool accounts status
eggpool accounts explain --model '<hot-model>' --protocol openai
```

Then send a controlled burst and inspect distribution:

```sql
SELECT a.name, COUNT(*) AS requests
FROM requests r
JOIN accounts a ON a.id = r.account_id
WHERE r.started_at >= datetime('now', '-30 minutes')
GROUP BY a.name
ORDER BY requests DESC;
```

Inspect fairness traces:

```sql
SELECT selected_account_name,
       eligible_count,
       scored_count,
       selected_score,
       json_extract(score_components_json, '$.fairness.mode') AS fairness_mode,
       json_extract(score_components_json, '$.fairness.applied') AS fairness_applied,
       json_extract(score_components_json, '$.fairness.reason') AS fairness_reason,
       json_extract(score_components_json, '$.fairness.candidate_count') AS fairness_candidates
FROM routing_decisions
ORDER BY id DESC
LIMIT 50;
```

Expected healthy equal-peer result:

- `eligible_count = 3`
- `scored_count = 3`
- `fairness_mode = round_robin`
- `fairness_applied = true`
- `fairness_candidates = 3`
- distribution is approximately balanced, not 1000 / 4 / 0.

If skew persists:

- `fairness_applied = false` should explain why: `not_tied`, `different_weights`, `single_candidate`, `disabled`, `wrong_provider`, etc.
- If `not_tied`, inspect `top_candidates` score deltas and usage/capacity inputs.
- If only one candidate is eligible, this is a catalog/health/config issue rather than a fairness issue.

## Acceptance criteria

The corrective pass is complete when:

1. `RoutingConfig` exposes same-tier fairness controls.
2. Default routing mode rotates effectively tied same-tier equal-weight peers.
3. `Router.select_accounts_for_failover()` applies fairness before the coordinator takes the first acceptable candidate.
4. Priority tiers remain strict: lower-priority accounts do not receive traffic while higher-priority eligible accounts exist.
5. Native-protocol preference remains honored.
6. Different-weight accounts are handled intentionally and documented.
7. `routing_decisions.score_components_json` records whether fairness applied and why.
8. A regression test with accounts `0001`, `0002`, `0003` cannot produce severe skew.
9. At least one test directly exercises the coordinator hot path, not only `Router.select_account()`.
10. Existing route-skew/publication-order tests still pass.
11. `uv run ruff check src/ tests/`, `uv run pyright src/ scripts/`, and relevant pytest targets pass.
12. Live validation no longer shows 1000 / 4 / 0 when accounts are same-tier, same-weight, healthy, and eligible.

## Implementation caution

Do not try to solve this only by increasing `near_tie_epsilon` or increasing `randomize_near_ties`. That may reduce skew statistically but does not guarantee bounded distribution, and it still leaves stable-order bias in the failover ranking path. The fix needs to live in the path actually used by the coordinator: `select_accounts_for_failover()`.
