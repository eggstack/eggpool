# Same-Tier Account Fairness Closeout Plan

## Context

The same-tier fairness line of work was introduced to correct severe routing skew observed in production testing: account `0001` received roughly 1000 requests while `0002` received 4 and `0003` received none. The earlier select-lock/publication fixes were necessary, but not sufficient: the coordinator hot path still ranked accounts through `Router.select_accounts_for_failover()` and then selected the first circuit-breaker-accepted candidate, so stable score/config ordering could still dominate equal peer accounts.

Recent commits added the correct core mechanism:

- `RoutingConfig` now has `fairness_mode`, `fairness_epsilon`, and `fairness_scope` fields.
- `FairnessRotor` implements in-memory round-robin rotation for effectively tied same-tier peers.
- `_fairness_band()` extracts equal-priority/equal-weight/equal-transcode-status peers whose final scores are within the configured fairness epsilon.
- `Router.select_accounts_for_failover()` applies fairness in the actual coordinator hot path.
- `routing_decisions.score_components_json` now carries fairness metadata and candidate-level rank annotations.
- `eggpool accounts explain --scores` gives operators a score-oriented diagnostic view.
- Regression tests cover direct rotor behavior, band extraction, failover ordering, priority isolation, mixed weights, and a coordinator-path 300-selection distribution case.

The implementation is close to complete. Two closure issues remain before this line of work should be considered fully closed:

1. `create_app()` constructs `Router(...)` without passing `config.routing.fairness_mode`, `config.routing.fairness_epsilon`, or `config.routing.fairness_scope`. Defaults still work because `Router` defaults to `round_robin`, `None`, and `provider_model_protocol`, but operator overrides are currently ignored in the server path.
2. The `provider_model_protocol` scope currently does not actually include the routed protocol in the fairness key. `FairnessKey(protocol=...)` is set to `None` in the router path, so protocol-specific rotation groups may collapse together.

This closeout should be a small correctness and verification pass, not another large routing rewrite.

## Goals

1. Ensure server runtime respects all new `[routing]` fairness config fields.
2. Ensure `provider_model_protocol` scope includes the relevant protocol in the fairness key.
3. Add tests that fail if `create_app()` ignores fairness config overrides.
4. Add tests that fail if protocol-scoped fairness keys collapse OpenAI and Anthropic traffic into the same rotor group.
5. Preserve current default behavior: `fairness_mode = "round_robin"`, `fairness_epsilon = None`, `fairness_scope = "provider_model_protocol"`.
6. Keep priority-tier isolation, native-protocol preference, and mixed-weight semantics unchanged.
7. Confirm live diagnostics show fairness applying to the `0001` / `0002` / `0003` case.

## Non-goals

1. Do not redesign `QuotaFairScorer`.
2. Do not add durable rotor state.
3. Do not add a new database table.
4. Do not implement weighted round-robin in this pass. Different weights should remain explicitly handled or skipped according to the current design.
5. Do not expand dashboard UI unless necessary to expose already-recorded JSON fields.
6. Do not change provider priority semantics.

## Phase 1: Wire fairness config into `create_app()`

File: `src/eggpool/app.py`

Current issue: the router is constructed with routing-local quota mode but without the new fairness fields.

Patch shape:

```python
router = Router(
    registry,
    catalog,
    health_manager=health_manager,
    stale_after_s=float(config.models.stale_after_s),
    local_quota_mode=config.routing.local_quota_mode,
    fairness_mode=config.routing.fairness_mode,
    fairness_epsilon=config.routing.fairness_epsilon,
    fairness_scope=config.routing.fairness_scope,
)
```

Important details:

- Keep scorer wiring immediately after router construction as-is.
- Keep `router._scorer.tiebreaker_range = config.routing.near_tie_epsilon`; when `fairness_epsilon is None`, `Router._fairness_effective_epsilon()` should continue to fall back to the scorer tiebreaker range.
- Keep `randomize_near_ties = false` behavior intact. If `near_tie_epsilon` is forced to 0, the fairness fallback epsilon becomes 0 unless `fairness_epsilon` is explicitly set.
- Do not mutate private router fairness fields after construction if constructor injection is sufficient.

## Phase 2: Correct protocol-scoped fairness keys

File: `src/eggpool/routing/router.py`

Current issue: in the fairness key construction, `protocol=None` is used even under `provider_model_protocol` scope. That makes the default scope behave more like `provider_model` or `provider_model_client_protocol`, depending on `client_protocol`.

Desired semantics:

- `provider_model_protocol`: key includes provider, model, routed/upstream protocol, priority tier, and client protocol when available.
- `provider_model`: key includes provider, model, priority tier, and optionally client protocol only if needed for transcode/native distinction; protocol should not split the group.
- `priority_model_protocol`: key excludes provider but includes model, routed/upstream protocol, priority tier, and client protocol when available.

Suggested helper:

```python
def _fairness_key(
    self,
    *,
    provider_id: str | None,
    model_id: str,
    protocol: str | None,
    priority: int,
    client_protocol: str | None,
) -> FairnessKey:
    return FairnessKey(
        provider_id=(
            None
            if self._fairness_scope == "priority_model_protocol"
            else provider_id
        ),
        model_id=model_id,
        protocol=(
            None
            if self._fairness_scope == "provider_model"
            else protocol
        ),
        priority=priority,
        client_protocol=client_protocol,
    )
```

Use the helper in both:

- `Router.select_accounts_for_failover()`
- `Router.select_account()`

If the code currently constructs a key in two places, centralize it to avoid future drift.

Clarify naming in comments:

- `protocol` should be the routed/upstream protocol filter passed into the router.
- `client_protocol` should remain separate because native/transcode preference can differ from upstream protocol.

## Phase 3: Tighten fairness trace metadata

Files:

- `src/eggpool/routing/fairness.py`
- `src/eggpool/request/coordinator.py`

Current trace already includes `mode`, `applied`, `scope`, `key`, `candidate_count`, `selected_index`, `selected_account_name`, and `reason`. Keep that shape.

Small improvements:

1. Ensure the key string includes `protocol=openai` or `protocol=anthropic` for `provider_model_protocol` and `priority_model_protocol` scopes.
2. Ensure the key string uses `protocol=*` only for `provider_model` scope or when no protocol is available.
3. For `random` mode, avoid `key=""` if possible. Build and store the same fairness key as round-robin so traces remain useful.
4. For skipped fairness, store the key when enough context exists. Empty key is acceptable only for impossible cases, but diagnostic value is higher when the intended group is visible.

Suggested trace expectations:

```json
{
  "fairness": {
    "mode": "round_robin",
    "applied": true,
    "scope": "provider_model_protocol",
    "key": "provider=opencode-go|model=gpt-4|protocol=openai|tier=0|client_protocol=openai",
    "candidate_count": 3,
    "selected_index": 0,
    "selected_account_name": "0002",
    "reason": "ok"
  }
}
```

## Phase 4: Add server config propagation tests

Add a focused test that exercises `create_app()` or the lifespan/router construction path enough to prove config values reach `Router`.

Potential test locations:

- `tests/unit/test_app_lifespan.py`
- `tests/integration/test_app_startup.py`
- a new small file such as `tests/unit/test_routing_config_wiring.py`

The test should construct an `AppConfig` with non-default fairness settings:

```toml
[routing]
fairness_mode = "off"
fairness_epsilon = 0.333
fairness_scope = "priority_model_protocol"
```

Then verify the constructed router has the configured values.

If direct lifespan boot is too heavy, extract a helper from `create_app()` for router construction:

```python
def _build_router_from_config(..., config: AppConfig) -> Router:
    ...
```

But avoid a broad app refactor unless the existing test harness makes lifespan construction painful.

Minimum assertion target:

- `_fairness_mode == "off"`
- `_fairness_epsilon == 0.333`
- `_fairness_scope == "priority_model_protocol"`

Private-field assertions are acceptable here because this is configuration wiring, and existing tests already inspect private scorer/router fields for routing behavior.

## Phase 5: Add protocol-scope key tests

Add unit tests around fairness key construction and trace output.

Recommended test cases:

### Test 1: provider_model_protocol separates protocol groups

Setup:

- one provider
- same model
- same priority
- same accounts
- `fairness_scope = "provider_model_protocol"`

Call `select_accounts_for_failover()` once with `protocol="openai"`, once with `protocol="anthropic"` or another distinct protocol path supported by the fixture.

Assert:

- first trace key contains `protocol=openai`
- second trace key contains `protocol=anthropic`
- the two keys differ

### Test 2: provider_model collapses protocol groups

Setup same as above, but `fairness_scope = "provider_model"`.

Assert:

- trace key contains `protocol=*` or otherwise omits protocol
- OpenAI and Anthropic invocations share the same provider/model/tier key shape

### Test 3: priority_model_protocol excludes provider

Setup two providers in the same priority tier with eligible accounts for the same model.

Use `fairness_scope = "priority_model_protocol"`.

Assert:

- trace key contains `provider=*`
- trace key contains `protocol=<requested protocol>`
- accounts from both providers can be in the same fairness band when other conditions match

Keep these tests small; they do not need to hit the coordinator persistence path.

## Phase 6: Add config override behavior tests

Add tests that prove overrides affect routing behavior:

### Test 1: fairness_mode off restores deterministic score ordering

Setup:

- three equal accounts: `0001`, `0002`, `0003`
- deterministic scorer: `tiebreaker_range = 0`
- no active/reservation drift or reset between calls
- `fairness_mode = "off"`

Call `select_accounts_for_failover(..., max_accounts=1)` multiple times.

Expected:

- first account remains stable, likely `0001`, because the rotor is disabled.
- fairness trace: `applied = false`, `reason = "disabled"`.

This test proves `fairness_mode` is not ignored.

### Test 2: explicit fairness_epsilon controls band size

Setup:

- three accounts with final scores separated by a known amount, e.g. 0.00, 0.05, 0.20.
- `near_tie_epsilon` can remain default.
- Set `fairness_epsilon = 0.01` and assert only no/one candidate enters band.
- Set `fairness_epsilon = 0.10` and assert the first two enter band.

This can be done directly with `_fairness_band()` or through `Router` if the score setup is easy.

## Phase 7: Verify coordinator-path distribution remains covered

Current `tests/unit/test_fairness.py` includes a coordinator hot-path test that runs 300 sequential `_select_and_persist_attempt()` calls and asserts each account receives 80–120 selections. Keep it.

Add one additional coordinator-level check only if cheap:

- Confirm persisted `routing_decisions.score_components_json` rows show `fairness.applied = true` and `fairness.candidate_count = 3` during the 300-selection run.

Suggested SQL inside the test:

```sql
SELECT score_components_json
FROM routing_decisions
ORDER BY id DESC
LIMIT 10;
```

Then parse JSON and assert:

- `fairness.mode == "round_robin"`
- `fairness.applied is True`
- `fairness.candidate_count == 3`
- `fairness.reason == "ok"`

This closes the gap between distribution and observability.

## Phase 8: Documentation closeout

Update docs only if implementation semantics change from the current text.

Files likely to touch:

- `architecture/README.md`
- `AGENTS.md`
- `config.example.toml`
- `CHANGELOG.md`

Docs should explicitly state:

- Server runtime honors `[routing] fairness_mode`, `fairness_epsilon`, and `fairness_scope`.
- `provider_model_protocol` includes the routed protocol in the rotor key.
- `provider_model` intentionally collapses protocol groups.
- `priority_model_protocol` intentionally co-balances same-priority providers serving the same model/protocol.
- Defaults remain suitable for subscription aggregation.

Avoid overstating guarantees:

- Fairness only applies to accounts in the fairness band.
- Eligibility, health, priority tier, weight, transcode status, and score distance can still exclude accounts from rotation.
- Restart resets rotor state.

## Phase 9: Validation commands

Run focused checks:

```bash
uv run pytest tests/unit/test_fairness.py
uv run pytest tests/unit/test_routing_coordinator_concurrent.py
uv run pytest tests/integration/test_cli_provider_aware.py -k accounts_explain
```

Run static checks:

```bash
uv run ruff check src/ tests/
uv run pyright src/ scripts/
```

Run full suite if feasible:

```bash
uv run pytest
```

If the two pre-existing dashboard rollup failures still exist, document them separately. Do not mark new routing/fairness failures as pre-existing.

## Phase 10: Live deployment validation

After deploying the closeout patch, validate on the actual Opencode Go account set.

Commands:

```bash
eggpool accounts status
eggpool accounts explain --model '<hot-model>' --protocol openai --scores
```

Check distribution over a controlled burst:

```sql
SELECT a.name, COUNT(*) AS requests
FROM requests r
JOIN accounts a ON a.id = r.account_id
WHERE r.started_at >= datetime('now', '-30 minutes')
GROUP BY a.name
ORDER BY requests DESC;
```

Check fairness traces:

```sql
SELECT
  selected_account_name,
  eligible_count,
  scored_count,
  json_extract(score_components_json, '$.fairness.mode') AS fairness_mode,
  json_extract(score_components_json, '$.fairness.applied') AS fairness_applied,
  json_extract(score_components_json, '$.fairness.scope') AS fairness_scope,
  json_extract(score_components_json, '$.fairness.key') AS fairness_key,
  json_extract(score_components_json, '$.fairness.reason') AS fairness_reason,
  json_extract(score_components_json, '$.fairness.candidate_count') AS fairness_candidates,
  COUNT(*) AS n
FROM routing_decisions
WHERE created_at >= datetime('now', '-30 minutes')
GROUP BY selected_account_name, fairness_mode, fairness_applied,
         fairness_scope, fairness_key, fairness_reason, fairness_candidates
ORDER BY n DESC;
```

Expected healthy equal-peer result:

- `eligible_count = 3`
- `scored_count = 3`
- `fairness_mode = round_robin`
- `fairness_applied = true`
- `fairness_scope = provider_model_protocol`
- `fairness_key` includes `protocol=openai`
- `fairness_candidates = 3`
- account distribution is near-even, not `1000 / 4 / 0`

If skew persists:

- `fairness_applied = false, reason = not_tied`: scores diverge beyond epsilon. Inspect `top_candidates` and consider increasing `fairness_epsilon` or correcting usage/capacity inputs.
- `fairness_applied = false, reason = different_weights` or band size < 3: account weights differ or score order starts with a differently weighted account.
- `eligible_count = 1` or `scored_count = 1`: this is not a fairness issue; inspect catalog/model/protocol/health.
- `fairness_key` lacks `protocol=openai` under `provider_model_protocol`: protocol key closeout failed.

## Acceptance criteria

The line of work is closed when:

1. `create_app()` passes `fairness_mode`, `fairness_epsilon`, and `fairness_scope` from `config.routing` into `Router`.
2. `provider_model_protocol` fairness keys include the routed protocol.
3. `provider_model` scope intentionally collapses protocol in the key.
4. `priority_model_protocol` intentionally omits provider and includes protocol.
5. Tests prove server/router construction respects non-default fairness config values.
6. Tests prove protocol-scoped keys differ across protocol values.
7. Tests prove `fairness_mode = "off"` disables the rotor.
8. Existing 300-selection router and coordinator distribution tests still pass.
9. Coordinator-path trace rows show `fairness.applied = true` for equal-peer distribution runs.
10. Focused routing/fairness tests pass.
11. Ruff and pyright pass.
12. Live Opencode Go testing no longer shows severe skew when accounts are equal-priority, equal-weight, healthy, and eligible.

## Implementation caution

Do not solve the remaining issues by changing defaults only. Defaults already mask the server wiring bug because `Router` and `RoutingConfig` currently share the same default values. The closeout must explicitly test non-default config propagation so future routing options do not silently become no-ops.
