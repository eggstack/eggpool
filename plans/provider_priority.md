# Provider routing priority and model collapse plan

## Objective

Introduce two related configuration knobs that give operators deterministic control over how EggPool distributes requests for the same base model across multiple upstream providers.

1. **`routing_priority` (per provider, default `0`)** — A non-negative integer that ranks providers for the same model. Higher values are preferred. Accounts within the same priority tier continue to load balance with the existing quota-fair scorer. A tier that becomes unhealthy or has no eligible accounts is skipped, falling through to the next lower tier.

2. **`collapse_models` (top-level `[models]` flag, default `false`)** — When `false` (the default), the same base model exposed by different providers is surfaced as one provider-suffixed model ID per provider (e.g. `minimax-m2.7/generalcompute`, `minimax-m2.7/minimax`). The router pools requests across accounts of the same provider and only ever picks one provider per request. When `true`, the same base model collapses to a single unsuffixed ID and is routed evenly across all eligible providers regardless of which one advertises the model.

The combination produces the following default behavior, which is the desired baseline:

- Three providers (`opencode-go`, `minimax`, `generalcompute`) all expose `minimax-m2.7`. With `collapse_models = false` and all priorities `0`, the catalog exposes `minimax-m2.7/opencode-go`, `minimax-m2.7/minimax`, `minimax-m2.7/generalcompute` as three distinct routable model IDs. The router load-balances within each provider. The model is *not* fanned out across providers for a single request.
- If `generalcompute.routing_priority = 3`, `minimax.routing_priority = 2`, and `opencode-go.routing_priority = 0`, then any request for `minimax-m2.7/generalcompute` first tries the `generalcompute` provider, falling through to `minimax` and then to `opencode-go` only on pre-body failure or exhaustion.
- For accounts of the same provider at the same priority (e.g. three `opencode-go` API keys all at priority `0`), the existing quota-fair scorer load balances.

This plan does not change the data plane, request coordinator, or proxy behavior beyond what is needed to thread the new flag and integer into routing decisions. It does not introduce provider failover at the connection-pool level (a single request still picks one upstream account). The failure semantics between providers for a single suffixed model ID follow the existing `max_retries_before_stream` / `exclude_accounts` policy.

## Non-goals

- Live config reload of `routing_priority` and `collapse_models`. Configuration changes require a restart, consistent with the existing `models.expose_mode` and `model_overrides` model.
- Cross-provider request fan-out (one user request fulfilled by two upstreams). The router still selects a single account per attempt.
- Provider-level circuit breakers or sticky sessions. The existing `HealthManager` is the only signal that demotes a provider tier.
- Per-model priority overrides. Priority is provider-scoped, not per-(provider, model).
- Numeric `weight` deprecation. `weight` continues to bias the quota-fair scorer inside a single priority tier. `routing_priority` orders tiers; `weight` orders within a tier. The two compose.
- An HTTP API to mutate priorities. CLI and TOML only.

## Current codebase observations

The relevant pieces already in place:

- `src/eggpool/models/config.py` defines `AppConfig`, `ProviderConfig`, and `AccountConfig` with Pydantic `extra="forbid"`. `ModelsConfig` carries `expose_mode` (`union`, `intersection`, `healthy_union`).
- `src/eggpool/accounts/registry.py` exposes `get_provider_for_account(account_name) -> str | None` and `get_accounts_for_provider(provider_id) -> list[AccountRuntimeState]`. The registry has no notion of priority.
- `src/eggpool/routing/router.py` calls `get_eligible_accounts(...)` to get a flat list of eligible `AccountRuntimeState` objects, then scores them all together with `QuotaFairScorer`. The current scorer treats every eligible account as interchangeable, regardless of provider.
- `src/eggpool/catalog/cache.py` already exposes `get_provider_suffixed_models(...)` (one row per `(model_id, provider_id)`) and `get_models_for_exposure(...)` (unsuffixed, conservative-merged). The two are mutually exclusive in the current `/v1/models` route.
- `src/eggpool/providers/connect.py` writes a new account block via `merge_provider_into_config` and `_format_provider_block`. The connect command does not currently emit any provider-level option other than what the template carries.
- `src/eggpool/providers/_templates.toml` carries per-provider template data. It does not currently declare `routing_priority`.

The minimum invasive change is to:

1. Add two new config fields with safe defaults.
2. Thread `routing_priority` from provider config into the registry / state and into the routing eligibility filter.
3. Sort the eligible account list by priority before the scorer, so the scorer can break ties within the highest non-empty tier.
4. Switch the catalog exposure default from "unsuffixed with conservative merge" to "provider-suffixed" when `collapse_models = false`.
5. Make `eggpool connect` write `routing_priority = 0` on every new account block so operators can edit one number to rebalance.

## Configuration design

### 1. Add `routing_priority` to `ProviderConfig`

`src/eggpool/models/config.py` — add to `ProviderConfig`:

```python
routing_priority: int = Field(default=0, ge=0)
```

Constraints:

- Integer, `>= 0`.
- Default `0`. All currently configured providers remain at the same effective tier.
- A model-level validator is not required. Per-field precedence is straightforward.
- Pydantic `extra="forbid"` already prevents unknown fields, so existing configs that omit the key keep working.

Provider-level (not account-level) is the right scope because the user's stated intent is "route through provider X first, then Y, then Z" — keys are pooled *within* a provider. If account-level priority is later required, the same field can be added to `AccountConfig` and resolved with a small precedence rule, but that is out of scope here.

### 2. Add `collapse_models` to `ModelsConfig`

`src/eggpool/models/config.py` — add to `ModelsConfig`:

```python
collapse_models: bool = False
```

Semantic:

- `False` (default) — catalog exposes one provider-suffixed entry per `(model_id, provider_id)`. Routing within a suffixed ID only ever picks accounts of that provider. Operators get the natural per-provider identity.
- `True` — catalog exposes a single unsuffixed `model_id` whose routing fan-out spans every provider that supports the model. This is the legacy behavior, kept available for operators who want a single model name regardless of upstream.

The default `False` is the *change* in behavior. Existing deployments that relied on the unsuffixed `minimax-m2.7` exposure will need to either set `collapse_models = true` or migrate to the suffixed form. This trade-off is acceptable because:

- The suffixed IDs are already what the runtime `/v1/models` endpoint returns when callers opt into provider-suffixed exposure.
- The previous unsuffixed exposure used `conservative_limits()` merges that masked per-provider context windows. The suffixed form is strictly more accurate.
- The CLI has not yet been released with the unsuffixed default, and the provider-suffixed form is already what `parse_model_id()` and the coordinator understand.

### 3. Document precedence and acceptance

- `collapse_models` and `routing_priority` are independent. `routing_priority` only affects routing within a single suffixed or unsuffixed model ID; it does not change exposure.
- When `collapse_models = false`, the catalog emits `minimax-m2.7/<provider>` for each provider that supports `minimax-m2.7`. Each entry's `routing_priority` is the provider's priority.
- When `collapse_models = true`, the catalog emits `minimax-m2.7` once. The router picks from any eligible provider. Higher-priority providers are still tried first; lower-priority providers are tried on pre-body failure or when the higher tier has no eligible account.

## Catalog exposure changes

### 1. Switch `/v1/models` default

`src/eggpool/app.py` already calls `catalog.get_models_for_exposure(health_manager=health_mgr)`. The change is to call a new method on `CatalogService` that branches on `models.collapse_models`.

```python
if self._config.models.collapse_models:
    models = self._cache.get_models_for_exposure(
        self._config.models.expose_mode,
        eligible,
    )
else:
    models = self._cache.get_provider_suffixed_models(
        self._config.models.expose_mode,
        eligible,
    )
```

This is the only behavioral switch in the HTTP layer. The existing `get_provider_suffixed_models()` already produces the correct shape, so the work is plumbing the new flag through `CatalogService`.

### 2. Add `CatalogService.get_models_for_exposure_for_dispatch(...)`

Most callers should keep working with the legacy method name. To avoid breaking `app.py` and tests, rename the existing `CatalogService.get_models_for_exposure` to `get_models_for_dispatch` (the existing return is provider-suffixed) and add a new `get_models_for_exposure` that branches on `collapse_models`. This rename is small and mechanical:

- `get_models_for_dispatch` — returns provider-suffixed model dicts (current behavior, suitable for internal dispatch / OpenCode provider-suffixed clients).
- `get_models_for_exposure` — returns either unsuffixed (if `collapse_models=true`) or suffixed (if `collapse_models=false`) for client-facing exposure via `/v1/models`.

If the rename is too disruptive, an alternative is to add a `for_dispatch: bool` parameter to the existing method. Pick the rename; it makes the calling sites self-documenting.

### 3. Update `eggpool configsetup opencode`

The CLI command in `src/eggpool/cli.py` already calls `build_opencode_config_json(...)`. The new behavior:

- If `collapse_models = false`, the generated `models` map uses provider-suffixed keys (`minimax-m2.7/opencode-go`) and the existing per-provider `effective_limits`.
- If `collapse_models = true`, the generated `models` map uses unsuffixed keys (`minimax-m2.7`) and the existing conservative-merged `effective_limits`.

A previous `--json-only` flag was removed in commit `2296c4f` because the opencode config setup is now always JSON on stdout, with status messages on stderr. This plan does not reintroduce that flag.

## Routing changes

### 1. Carry `routing_priority` on `AccountRuntimeState`

`src/eggpool/accounts/state.py` — add a field:

```python
routing_priority: int = 0
```

This is set at registry construction time from `provider.routing_priority`.

### 2. Initialize from config

`src/eggpool/accounts/registry.py` — `_initialize()` constructs `AccountRuntimeState` from `AccountConfig`. Look up the provider for the account and copy its `routing_priority`:

```python
state = AccountRuntimeState(
    name=acct_config.name,
    enabled=acct_config.enabled,
    weight=acct_config.weight,
    routing_priority=self._config.providers[provider_id].routing_priority,
)
```

The same change is needed in `reload()`. `account_config_rows()` in `registry.py` does not need to persist `routing_priority` to the `accounts` table because the value is a static config setting; the runtime re-reads it from config on registry rebuild.

### 3. Tiered routing selection

`src/eggpool/routing/router.py` — change `select_account()` (and `select_accounts_for_failover()`) to:

1. Compute the full set of eligible `AccountRuntimeState` (unchanged).
2. Group eligible states by `routing_priority`. Higher priority comes first.
3. Drop any group where every account is in a health state that prevents routing (`is_eligible()` is already enforced by `get_eligible_accounts`, so this is implicit).
4. Within the highest-priority non-empty group, use the existing `QuotaFairScorer` to break ties. The scorer already randomizes near-ties, so a tier with N accounts will load balance cleanly.
5. If the highest-priority group has no eligible accounts, descend to the next priority. Do not retry or block.

The natural implementation is a small wrapper that returns a `list[list[AccountRuntimeState]]` of priority tiers, then loops tiers in order. The scoring path stays the same — only the order of the input list changes. This keeps the existing `QuotaFairScorer` logic intact.

Concretely, replace the current `candidates = self._selection_candidates(...)` in `select_account()` with:

```python
eligible_states = self._selection_candidates(
    model_id, exclude_accounts, provider_id, protocol
).states
if not eligible_states:
    return None

# Stable sort: higher routing_priority first; within a tier, preserve
# the eligibility order produced by get_eligible_accounts so that
# any deterministic tie-breakers upstream are not lost.
tiers = _group_by_priority(eligible_states)

scores = await self._score_eligible_accounts(
    RoutingCandidates(states=tiers[0], by_name=...),
    model_id,
    request_estimates,
)
best = self._scorer.select_account(scores)
if best is not None:
    return tiers[0]_by_name[best.account_name]
return None
```

For `select_accounts_for_failover()`, return the merged ranked list across all tiers in priority order, with a tier boundary marker (`tier: int`) on each `RoutingScore` so callers that want strict tier-only failover can short-circuit. The coordinator's existing retry loop continues to use `exclude_accounts` to avoid reusing a failed account within one request — no change there.

### 4. Tier boundary semantics for failover

Today, `select_accounts_for_failover()` returns a flat ranked list of up to `max_accounts` accounts. With priorities, the desired semantics are:

- The first attempt uses the highest-priority tier.
- Failover to the next attempt can be either:
  - **Tier-bounded** — only accounts within the same priority tier as the failed attempt. (Strict per-provider failover.)
  - **Tier-leaking** — once the tier is exhausted, fall through to the next priority. (Cross-provider failover.)

The current behavior is already tier-leaking for accounts (any eligible account is fair game after a fail). The new plan keeps that behavior: failover can leak to a lower priority tier. Operators who want tier-bounded behavior can pin `routing_priority` to the same value across all desired fallback providers and use a separate higher priority for the "primary" provider.

This is documented but not enforced in code. The `exclude_accounts` set already prevents same-request retries of a known-broken account.

### 5. Eligible behavior under `collapse_models = true`

When `collapse_models = true`, the user request comes in with a bare `model_id` and no provider suffix. The router must consider every provider that supports the model. The tiered selection already does this — the only change is that the `provider_id` argument to `select_account` is `None`, so eligibility no longer filters by provider. The existing `Router._selection_candidates(..., provider_id=None, ...)` path handles this. No code change is needed beyond the tier grouping.

### 6. QuotaEstimator and QuotaFairScorer

These do not need to know about `routing_priority`. Priority is an *ordering* of the input to scoring, not a *weight* inside scoring. The scorer's `weight` field continues to bias scoring within a single tier.

## CLI changes

### 1. `eggpool connect` writes `routing_priority = 0` automatically

`src/eggpool/providers/connect.py` — `_format_provider_block()` currently emits the provider template fields, then a `[[providers.<id>.accounts]]` block with `name` and (optionally) `api_key`. Update it to:

1. Emit `routing_priority = 0` on the provider block, but only if the provider block is being *created* (i.e., not when appending an account to an existing provider, where editing the priority would surprise the operator).
2. If the provider block already exists, leave its `routing_priority` untouched.

`_append_account()` does not change the provider header. It only adds a new account entry, so the priority stays whatever the operator set previously.

The comment in the generated block should explain:

```toml
# routing_priority: higher = preferred. Default 0 = load balanced within
# the existing quota-fair scorer. Increase to push this provider's models
# to the front of the request queue.
routing_priority = 0
```

### 2. Update `_format_provider_block` signature

The current function takes `(provider_id, data, api_key, account_name)`. Add a `provider_exists: bool` argument so it can skip the `routing_priority` line for existing providers:

```python
def _format_provider_block(
    provider_id: str,
    data: dict[str, Any],
    api_key: str | None,
    account_name: str,
    *,
    include_routing_priority: bool = True,
) -> str:
    ...
```

Caller is `merge_provider_into_config`, which already branches on whether the provider block exists. Pass `include_routing_priority=True` on the new-block path and `False` on the append path.

### 3. Update `connect_list` summary

Add `routing_priority` to the per-line display so operators can see at a glance which provider is at which tier:

```
* opencode-go: OpenCode Go (...) [✓] (priority 0)
* minimax: MiniMax International (...) [~] (priority 2)
* generalcompute: GeneralCompute (...) [?] (priority 3)
```

This is a minor visual change and can be skipped if it requires reading the active config (it does, via `AppConfig.from_toml`).

### 4. `eggpool set` integration

The existing `eggpool set` command writes arbitrary config values. No special-casing is needed for `routing_priority` — operators can already set `[providers.<id>].routing_priority = N` via the existing escape hatches. Document the field in the README so operators know it exists.

## `/v1/models` example output

With `collapse_models = false` and three providers all supporting `minimax-m2.7` at priorities `0`, `2`, `3`:

```json
{
  "object": "list",
  "data": [
    {
      "id": "minimax-m2.7/generalcompute",
      "object": "model",
      "owned_by": "generalcompute",
      "eggpool": { "provider_id": "generalcompute", "base_model_id": "minimax-m2.7", "routing_priority": 3, "limits": { "context": 200000 } }
    },
    {
      "id": "minimax-m2.7/minimax",
      "object": "model",
      "owned_by": "minimax",
      "eggpool": { "provider_id": "minimax", "base_model_id": "minimax-m2.7", "routing_priority": 2, "limits": { "context": 128000 } }
    },
    {
      "id": "minimax-m2.7/opencode-go",
      "object": "model",
      "owned_by": "opencode-go",
      "eggpool": { "provider_id": "opencode-go", "base_model_id": "minimax-m2.7", "routing_priority": 0, "limits": { "context": 220000 } }
    }
  ]
}
```

With `collapse_models = true`:

```json
{
  "object": "list",
  "data": [
    {
      "id": "minimax-m2.7",
      "object": "model",
      "owned_by": "eggpool",
      "eggpool": { "routing_priority_max": 3, "providers": ["generalcompute", "minimax", "opencode-go"], "limits": { "context": 128000 } }
    }
  ]
}
```

The `eggpool.limits` extension is the existing field. The `routing_priority` (or `routing_priority_max`) field is new. Adding it to the existing extension keeps the namespacing consistent.

## Documentation changes

### 1. `docs/providers.md` (new or existing)

Add a section that explains:

- How `routing_priority` and `collapse_models` interact.
- A worked example with three providers and three priorities, including a screenshot-style diagram of the request flow.
- The default behavior (collapse = false, priority = 0) and what changes for existing deployments.
- How to migrate: the same `minimax-m2.7` request now goes to `minimax-m2.7/<provider>`. The OpenCode client picks a specific suffixed ID. If a client only knows `minimax-m2.7`, set `collapse_models = true` or rewrite the client to use suffixed IDs.

### 2. `config.example.toml`

Add commented examples for the new fields:

```toml
# [models]
# collapse_models = false  # when true, a base model exposed by multiple
#                          # providers is collapsed to a single unsuffixed
#                          # ID and routed across all of them.
#
# [providers.opencode-go]
# routing_priority = 0  # higher = preferred; default 0 means "load balance
#                       # within this provider using the existing scorer".
```

### 3. `README.md`

Update the multi-provider section to call out:

- Each provider account is auto-labeled with `routing_priority = 0` on first `eggpool connect`.
- Operators can rebalance by editing `[providers.<id>].routing_priority`.
- `eggpool configsetup opencode` output reflects the current collapse / priority settings.

### 4. `architecture/README.md`

Update the routing section to describe the tiered selection. The current section says the router uses a `QuotaFairScorer`; the new section adds the priority grouping step in front of it.

## Test plan

### Configuration parsing tests (`tests/unit/test_config.py`)

1. `routing_priority` defaults to `0` when omitted.
2. `routing_priority = 3` parses.
3. `routing_priority = -1` is rejected by Pydantic (`ge=0`).
4. `routing_priority = "high"` is rejected.
5. `collapse_models` defaults to `False`.
6. `collapse_models = true` parses.
7. `collapse_models = "yes"` is rejected (Pydantic `bool`).
8. Unknown extra fields at the provider level are still rejected (regression check for `extra="forbid"`).

### Registry tests (`tests/unit/test_provider_registry.py`)

1. `AccountRuntimeState.routing_priority` is copied from `provider.routing_priority` at construction.
2. Reloading the config with a changed priority updates the state.
3. Two providers with different priorities produce two different `routing_priority` values on their respective accounts.

### Routing tests (`tests/unit/test_routing.py` and `tests/unit/test_coordinator_provider.py`)

1. **Same model, three providers, all priority 0** — every account is selected roughly equally over many trials (existing `randomize_near_ties` covers this; assert load balance).
2. **Same model, three providers, priorities 3 / 2 / 0** — `select_account()` returns an account from the priority-3 provider for the first 100 requests unless the priority-3 group is empty, in which case it returns from priority-2, and so on.
3. **Same provider, two accounts, priority 0** — load balance is unchanged from current behavior.
4. **Mixed: priority-3 provider with one account and priority-0 provider with three accounts** — about 25% of requests go to priority 0 (load balanced across its 3 accounts), 75% to priority 3 (its only account).
5. **Priority-3 group has all accounts in `cooldown`** — router falls through to priority-2 group, then to priority-0.
6. **Failover** — when a priority-3 attempt fails pre-body, the coordinator's retry selects a different priority-3 account if one is available, then falls to the next priority tier. Confirm that the tier boundary is respected (i.e., a single request does not bounce between two providers within one retry loop unless explicitly configured to do so).
7. **`exclude_accounts` interaction** — the existing `exclude_accounts` set still works across tier boundaries.

### Catalog exposure tests (`tests/unit/test_catalog.py`)

1. `collapse_models = false` returns suffixed entries (current behavior of `get_provider_suffixed_models`).
2. `collapse_models = true` returns unsuffixed entries with conservative-merged limits.
3. The `routing_priority` field appears in the `eggpool` extension block of each entry.
4. `expose_mode` (`union` / `intersection` / `healthy_union`) is still respected under both modes.

### CLI connect tests (`tests/unit/test_connect.py`)

1. `eggpool connect` for a new provider emits `routing_priority = 0` in the new provider block.
2. `eggpool connect` for an existing provider (appending an account) does *not* modify the existing `routing_priority` line.
3. `eggpool set` (or direct TOML edit) with `routing_priority = 5` parses and survives a restart.

### OpenCode config tests (`tests/unit/test_opencode_config.py`)

1. `collapse_models = false` → `build_opencode_provider_config` produces suffixed keys.
2. `collapse_models = true` → `build_opencode_provider_config` produces unsuffixed keys with conservative-merged limits.

### End-to-end integration tests (`tests/integration/test_provider_routing_e2e.py` and friends)

1. Spin up an `EggPool` instance with three mock providers all serving `minimax-m2.7`. With `collapse_models = false` and `routing_priority = 0` everywhere, send 30 requests for `minimax-m2.7/opencode-go`, 30 for `minimax-m2.7/minimax`, 30 for `minimax-m2.7/generalcompute`. Each suffixed ID's traffic is load-balanced within its provider. None of the requests cross providers.
2. Set `generalcompute.routing_priority = 3`. With `collapse_models = true`, send 30 unsuffixed `minimax-m2.7` requests. Assert that the first 30 requests are served by the `generalcompute` mock (mock it to fail on the 31st to confirm fallthrough to `minimax`).
3. With both `collapse_models = true` and three providers all at priority `0`, send 90 unsuffixed requests and assert that each provider serves roughly 30.

### Backward-compatibility regression tests

1. Existing fixtures (which never set `routing_priority` or `collapse_models`) continue to pass. `routing_priority = 0` and `collapse_models = false` must produce the same routing behavior the existing tests expect.
2. `tests/unit/test_multi_provider.py` and `tests/integration/test_e2e_key_flow_and_config.py` are the primary regression targets.

## Implementation phases

### Phase 1: Configuration plumbing

Files:

- `src/eggpool/models/config.py`
- `src/eggpool/accounts/state.py`
- `src/eggpool/accounts/registry.py`
- configuration parsing tests

Deliverables:

- `ProviderConfig.routing_priority` (default `0`).
- `ModelsConfig.collapse_models` (default `False`).
- `AccountRuntimeState.routing_priority` (default `0`).
- Registry populates the state field from the provider config.

Acceptance criteria:

- All existing tests still pass.
- New parsing tests cover defaults, valid values, and validation errors.

### Phase 2: Routing tiering

Files:

- `src/eggpool/routing/router.py`
- `src/eggpool/quota/scorer.py` (no behavior change; verify)
- routing tests

Deliverables:

- `_group_by_priority()` helper.
- `Router.select_account()` iterates tiers from highest priority to lowest.
- `Router.select_accounts_for_failover()` returns ranked candidates with a tier field; coordinator retries stay inside a tier until exhausted.
- Existing `weight`, `near_tie_epsilon`, and `randomize_near_ties` continue to operate inside a tier.

Acceptance criteria:

- The tiered selection tests pass.
- The failover tests pass.
- `test_routing.py` and `test_coordinator_provider.py` continue to pass.

### Phase 3: Catalog exposure switch

Files:

- `src/eggpool/catalog/service.py`
- `src/eggpool/catalog/cache.py` (add a new method or rename, see "Catalog exposure changes" above)
- `src/eggpool/app.py` (call the new method)
- `src/eggpool/api/models.py` (extend the namespaced `eggpool` extension with `routing_priority`)
- catalog and API tests

Deliverables:

- `CatalogService.get_models_for_exposure` honors `collapse_models`.
- `/v1/models` returns suffixed IDs by default.
- `eggpool.routing_priority` (or `routing_priority_max` for collapsed entries) is present in the extension.

Acceptance criteria:

- Tests in `test_catalog.py` and `test_api_models.py` pass.
- A regression test in `test_e2e_key_flow_and_config.py` confirms three providers with `minimax-m2.7` produce three suffixed entries in `/v1/models`.

### Phase 4: OpenCode config generation

Files:

- `src/eggpool/integrations/opencode.py` (already pure)
- `src/eggpool/cli.py` (`configsetup_opencode` honors `collapse_models`)
- integration test in `tests/integration/test_api_key_e2e.py`

Deliverables:

- Generated OpenCode config uses suffixed keys when `collapse_models = false`.

Acceptance criteria:

- Existing `test_opencode_config.py` passes.
- New tests for suffixed vs. unsuffixed generation pass.

### Phase 5: `eggpool connect` template

Files:

- `src/eggpool/providers/connect.py`
- `tests/unit/test_connect.py`

Deliverables:

- `_format_provider_block` emits `routing_priority = 0` for new provider blocks.
- `_append_account` does not edit the existing block's `routing_priority`.
- `connect_list` displays the priority for each provider.

Acceptance criteria:

- Connect flow tests pass.
- Manual smoke test: run `eggpool connect`, pick a new provider, confirm the generated `config.toml` contains `routing_priority = 0`.

### Phase 6: Documentation

Files:

- `docs/providers.md` (new section)
- `config.example.toml` (commented examples)
- `README.md` (one paragraph in the multi-provider section)
- `architecture/README.md` (routing section update)

Deliverables:

- Worked example with three providers.
- Migration note for operators who relied on the old unsuffixed `minimax-m2.7` behavior.
- Reference to `routing_priority` and `collapse_models` in the config example.

Acceptance criteria:

- Docs build cleanly (no broken links).
- One reviewer unfamiliar with the feature can read the docs and configure three providers with the right priorities.

## Small-model implementation guidance

Keep these invariants explicit while coding:

1. `routing_priority` is per **provider**, never per account. A single `opencode-go` account and three `opencode-go` accounts at priority `0` are equivalent from the routing tier perspective.
2. `routing_priority = 0` is the *standard* default. Do not pick a non-zero default to avoid surprising operators.
3. The priority tier is determined once per request and is stable for the duration of the request's retry loop. The tier boundary is not re-evaluated mid-retry unless a structural change (account disabled, provider removed) happens.
4. The quota-fair scorer is unchanged. It is a within-tier tiebreaker.
5. `collapse_models = true` collapses the *exposure* and the *dispatch identity* but does not collapse the *provider pool*. Each request still goes to one provider.
6. `collapse_models` and `routing_priority` are independent. Either can change without re-deriving the other.
7. `eggpool connect` writes `routing_priority = 0` only on the new-provider path. Do not let connect mutate an existing block.
8. The CLI must surface priorities in `connect list` so operators can see the current state without opening `config.toml`.
9. Provider-suffixed IDs (`minimax-m2.7/opencode-go`) are routing selectors, not display names. Operators and clients must be able to use them as `model_id` in any chat-completions / messages call.
10. Restart is required for any `routing_priority` or `collapse_models` change. Hot reload is not in scope.

## Suggested initial configuration

For an operator with the three providers from the user's example:

```toml
[models]
# collapse_models = false  # default; emit suffixed model IDs

[providers.opencode-go]
routing_priority = 0  # 3 API keys load balance within this tier

[providers.minimax]
routing_priority = 2  # tried after generalcompute, before opencode-go

[providers.generalcompute]
routing_priority = 3  # tried first
```

A request for `minimax-m2.7/generalcompute` then routes:

1. All eligible `generalcompute` accounts, load balanced by quota-fair scoring.
2. If all `generalcompute` accounts are unhealthy, exhausted, or failing pre-body, retry against `minimax` accounts.
3. If all `minimax` accounts are unavailable, retry against `opencode-go` accounts.

A request for `minimax-m2.7/opencode-go` only ever routes against `opencode-go` accounts, regardless of priorities. Priority only affects ordering *within* an unsuffixed or suffixed model ID's eligible account set.

## Final definition of done

The feature is complete when all of the following are true:

- `AppConfig` accepts `routing_priority` per provider (default `0`) and `collapse_models` per `[models]` (default `false`).
- `eggpool connect` writes `routing_priority = 0` when creating a new provider block and leaves the existing block's value intact when appending accounts.
- The router groups eligible accounts by `routing_priority`, picks the highest non-empty tier, and uses the existing `QuotaFairScorer` to load balance within the tier.
- The catalog exposure honors `collapse_models`: suffixed IDs when `false`, unsuffixed with conservative merge when `true`.
- `/v1/models` emits the new shape by default; `eggpool` extension carries `routing_priority` for each entry.
- `eggpool configsetup opencode` emits suffixed or unsuffixed keys to match `collapse_models`.
- Documentation explains the new fields, the worked three-provider example, and the migration path for operators using unsuffixed IDs.
- All existing tests pass; new tests cover priority tiering, collapsed vs. suffixed exposure, and CLI behavior.
- No request coordinator or proxy code path regresses. A single request still picks one upstream account.
