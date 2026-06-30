# Catalog Non-Destructive Refresh Corrective Pass

## Context

Live testing still shows all Opencode Go traffic routed to `opencode-go-0001`, with no requests placed on `opencode-go-0002` or `opencode-go-0003`. The same-tier fairness rotor is now implemented and wired into the coordinator hot path, so a total absence of traffic to peer accounts strongly suggests those accounts are disappearing before fairness can run.

The current routing chain makes this plausible:

1. `RequestCoordinator._select_and_persist_attempt()` asks `Router.get_eligible_account_names()` for candidates.
2. `Router._selection_candidates()` delegates to `get_eligible_accounts()`.
3. `get_eligible_accounts()` excludes accounts that fail runtime health, provider match, protocol support, circuit health, or catalog availability.
4. Catalog availability requires `ModelCatalogCache.is_account_model_available(account_name, model_id, protocol=...)`.
5. `is_account_model_available()` requires the account to support the model and requires provider-specific metadata to have a resolved protocol matching the requested endpoint protocol.
6. Catalog refresh currently treats each account refresh response as authoritative for that account: `ModelCatalogCache.update_from_account()` calls `mark_account_models_unavailable(account_name)` before adding back whatever the latest refresh returned.
7. Persistence then computes `desired_support` from the in-memory cache and disables `account_models` rows that are no longer desired.

That means a failed, empty, partial, malformed, protocol-unresolved, or transiently incomplete catalog refresh can silently remove an account/model link from routing. That is the wrong suppression mechanism for subscription pooling. A provider/account should leave the routing pool only through explicit account disablement, credentials failure, health/backoff/circuit state, upstream quota/rate-limit state, or a confirmed model withdrawal policy. Catalog uncertainty should degrade observability, not silently de-pool a healthy account.

## Problem statement

EggPool currently conflates two different events:

- **Confirmed model withdrawal**: an upstream explicitly and successfully reports that an account no longer serves a model.
- **Catalog uncertainty**: the catalog refresh failed, returned an empty/partial result, lost protocol resolution, or could not normalize enough metadata to prove support.

For routing safety and debuggability, these must be separated. Unknown catalog state must not silently remove a healthy configured account from routing.

## Goals

1. Make catalog refresh non-destructive by default for existing account/model support.
2. Never silently remove an account from the routing pool because one catalog refresh failed or returned an empty/partial/unresolved response.
3. Only remove or disable account/model support through explicit, observable causes:
   - account disabled in config,
   - unusable credentials/auth health,
   - upstream quota/rate-limit/cooldown health,
   - circuit breaker state,
   - confirmed successful catalog withdrawal under a deliberate policy.
4. Preserve fresh discovery of new models and new account support.
5. Preserve model withdrawal capability, but require a stronger confirmation policy and log/persist why support was removed.
6. Add routing diagnostics that expose every account’s gate-by-gate status for a given model/protocol.
7. Add regression tests for the exact failure class: `0002`/`0003` must not lose support after failed or empty refreshes.

## Non-goals

1. Do not change the fairness rotor in this pass.
2. Do not disable model discovery.
3. Do not ignore explicit upstream health failures.
4. Do not keep routing to accounts with known authentication failure or explicit quota exhaustion.
5. Do not add complex durable versioning for the entire catalog unless a small field is already required.
6. Do not redesign pricing/model-info background tasks.

## Desired invariant

A configured, credential-usable, healthy account that has previously supported a model must remain eligible for that model unless one of these happens:

1. The account is disabled or removed from config.
2. The account enters an explicit health suppression state.
3. The provider/account returns a successful catalog response that satisfies the configured withdrawal confirmation policy for that model.
4. The operator explicitly clears/purges catalog support.

A failed refresh, empty response, exception, timeout, unresolved protocol, or partial parse must not by itself disable the account/model link.

## Phase 1: Add explicit catalog refresh outcome semantics

Files likely to touch:

- `src/eggpool/catalog/fetcher.py`
- `src/eggpool/catalog/service.py`
- `src/eggpool/catalog/cache.py`

Introduce a small refresh outcome model that distinguishes:

```python
class AccountCatalogOutcome(Enum):
    SUCCESS_AUTHORITATIVE = "success_authoritative"
    SUCCESS_EMPTY = "success_empty"
    SUCCESS_PARTIAL = "success_partial"
    FAILED = "failed"
    SKIPPED = "skipped"
```

The exact type can be a dataclass or typed dict. The important point is that `_fetch_and_process_account()` must know whether it has an authoritative model list before it calls any destructive cache update.

Recommended behavior:

- HTTP/network exception: `FAILED`; do not mutate existing account support.
- auth failure: record explicit health/auth failure; do not use catalog mutation as the suppression mechanism.
- timeout: `FAILED`; do not mutate support.
- HTTP 5xx / retryable upstream error: `FAILED`; do not mutate support.
- HTTP 429 / 402 quota: record health/backoff if appropriate; do not mutate support.
- HTTP 2xx with valid non-empty model list: `SUCCESS_AUTHORITATIVE`.
- HTTP 2xx with empty model list: treat as `SUCCESS_EMPTY`, not destructive by default.
- HTTP 2xx with malformed/partially normalized list: `SUCCESS_PARTIAL`, not destructive by default unless enough data proves a specific model withdrawal.

## Phase 2: Make `ModelCatalogCache.update_from_account()` non-destructive by default

File: `src/eggpool/catalog/cache.py`

Current risk: `update_from_account()` unconditionally calls `mark_account_models_unavailable(account_name)` before adding the refreshed list back. That is destructive for failed/empty/partial refreshes if the caller reaches it with an incomplete model list.

Change the API to make destructiveness explicit:

```python
def update_from_account(
    self,
    account_name: str,
    provider_id: str,
    models: list[dict[str, Any]],
    *,
    authoritative: bool = False,
    allow_withdrawals: bool = False,
) -> AccountCatalogUpdateResult:
    ...
```

Recommended semantics:

- Always set `account -> provider` mapping.
- Always add or update the models supplied in `models`.
- If `authoritative and allow_withdrawals`, compute withdrawals and remove only those models absent from the new response.
- If not authoritative, never call `mark_account_models_unavailable(account_name)` and never remove provider rows or account support.
- Return a result object with counts:
  - `added_support`
  - `updated_support`
  - `withdrawn_support`
  - `preserved_support`
  - `authoritative`
  - `allow_withdrawals`

For v1 of this corrective patch, a simpler approach is acceptable:

- Add `replace: bool = False` to `update_from_account()`.
- Only call `mark_account_models_unavailable(account_name)` when `replace=True`.
- Make all live refresh callers pass `replace=False` unless withdrawal policy says otherwise.

But use names that make safety clear. `replace=True` or `allow_withdrawals=True` must be rare and explicit.

## Phase 3: Add a model withdrawal confirmation policy

Files likely to touch:

- `src/eggpool/models/config.py`
- `config.example.toml`
- `src/eggpool/catalog/service.py`

Add a config knob under `[models]` or `[routing]`, preferably `[models]`:

```python
catalog_withdrawal_policy: Literal[
    "preserve_until_health",
    "confirmed_once",
    "confirmed_twice",
] = "preserve_until_health"
```

Recommended default: `preserve_until_health`.

Policy meanings:

- `preserve_until_health`: catalog refresh never removes existing account/model support; only health/config can suppress routing. This matches the user’s stated desired behavior for subscription aggregation.
- `confirmed_once`: a successful authoritative non-empty catalog response that omits a model can remove that account/model support.
- `confirmed_twice`: require two consecutive authoritative omissions before removing support.

If adding durable counters for `confirmed_twice` is too much for this pass, implement only:

```python
catalog_refresh_destructive: bool = False
```

Default `False`. This is less expressive but fixes the regression class.

## Phase 4: Treat static models as sticky support

Static provider models are operator intent. If a provider has `static_models`, those rows should not be removed by a live catalog response unless the operator removes them from config.

Rules:

1. `_seed_static_models()` should add support with a sticky/static source marker.
2. A later live response should update metadata where useful, but must not remove static support.
3. If live metadata lacks protocol or resolves incorrectly, static `protocol_source = "static_config"` should remain authoritative.

The current cache already tries to preserve static fields. This pass should harden the stronger invariant: static support remains routable unless config removes it or health suppresses it.

## Phase 5: Stop disabling `account_models` from uncertain refreshes

File: `src/eggpool/catalog/service.py`

Current persistence computes:

```python
support_to_enable = desired_support - existing_support
support_to_disable = existing_support - desired_support
```

and then disables `support_to_disable`.

That must become conditional on the withdrawal policy and account refresh outcome. If the current refresh did not authoritatively prove withdrawal, `existing_support` must be preserved.

Suggested implementation options:

### Option A: cache already preserves support

If `ModelCatalogCache` no longer removes existing support during uncertain refreshes, then `desired_support` continues to include the old account/model links and the current persistence code no longer disables them. This is the simplest route.

### Option B: persistence-side guard

Track `safe_to_disable` pairs and only disable rows in that set:

```python
support_to_disable = (existing_support - desired_support) & confirmed_withdrawals
```

Where `confirmed_withdrawals` is populated only from authoritative successful refreshes that satisfy the withdrawal policy.

Option A is acceptable for the first patch if tests prove failed/empty refreshes do not disable existing rows.

## Phase 6: Make health the explicit de-pooling mechanism

Files likely to touch:

- `src/eggpool/health/health_manager.py`
- `src/eggpool/catalog/service.py`
- `src/eggpool/request/coordinator.py`

If a provider/account cannot serve traffic due to auth, quota, rate-limit, or repeated transient failures, record that in health/backoff. Do not encode that state by removing model support.

Ensure the following already-existing states remain the suppression mechanism:

- `authentication_failed`
- `quota_exhausted`
- `cooldown`
- `rate_limited`
- circuit breaker open

Catalog refresh failures should be visible as catalog freshness/diagnostic warnings, not as de-pooling.

## Phase 7: Add gate-by-gate routing diagnostics

Add a targeted command, preferably extending the existing `accounts explain` command:

```bash
eggpool accounts explain --model '<model>' --protocol openai --gates
```

For each account, print fields like:

- account name
- provider id from registry
- provider id from catalog
- config enabled
- credentials usable
- health state
- provider filter matched
- provider supports requested protocol
- account supports requested protocol
- model support row exists
- model support enabled
- fresh support according to stale window
- provider model metadata exists
- provider model protocol
- protocol match
- local quota gate result
- final eligible boolean
- reason code

This command should make the current failure obvious in one run. For example, it should show whether `opencode-go-0002` is failing because `account_models.enabled=0`, `provider_model_metadata.protocol IS NULL`, health is suppressing it, or provider mapping is wrong.

If CLI implementation is too large for the patch, add a router method first:

```python
Router.explain_account_gates(model_id, protocol, provider_id=None) -> list[dict[str, Any]]
```

Then wire CLI in a follow-up.

## Phase 8: Add regression tests for non-destructive refresh

Tests should cover the observed regression class directly.

### Test 1: failed refresh preserves existing support

Setup:

- account `opencode-go-0001`, `opencode-go-0002`, `opencode-go-0003`
- all three have account/model support for `test-model`
- simulate refresh failure for `0002` and `0003`

Expected:

- all three remain in `ModelCatalogCache.get_supporting_accounts("test-model")`
- persisted `account_models.enabled` remains `1` for all three after `_persist_catalog()`
- `Router.get_eligible_account_names("test-model", protocol="openai")` returns all three when health is good

### Test 2: empty response preserves existing support by default

Setup same as above, but feed an empty model response for `0002`.

Expected under default policy:

- `0002` support remains enabled
- an operational event or warning is emitted for empty catalog response
- routing still considers `0002` eligible if health is good

### Test 3: successful non-empty response adds new support without removing old support

Setup:

- `0002` previously supports `model-a`
- refresh returns only `model-b`
- default policy is non-destructive

Expected:

- `0002` supports both `model-a` and `model-b`
- no support is removed unless destructive policy is enabled

### Test 4: explicit destructive policy can remove confirmed withdrawal

If implementing `catalog_refresh_destructive` or `confirmed_once`, add one test proving the policy can still remove a model when explicitly enabled.

### Test 5: static model support is sticky

Setup:

- provider has `static_models = [test-model]`
- live refresh returns no models or unresolved protocol

Expected:

- `test-model` remains routable for every configured account
- static protocol remains available

### Test 6: routing explain gates surfaces cause

Simulate a disabled account/model row or unresolved provider protocol and verify the diagnostic output identifies that exact gate.

## Phase 9: Add operational events/logging

When catalog uncertainty occurs, log and optionally persist an operational event:

- `catalog_refresh_failed_preserved_support`
- `catalog_refresh_empty_preserved_support`
- `catalog_refresh_partial_preserved_support`
- `catalog_withdrawal_confirmed`
- `catalog_withdrawal_skipped_policy_preserve`

This makes it possible to see why stale support remains sticky while still making catalog failures visible.

The log should include:

- account name
- provider id
- model count returned
- prior support count
- preserved support count
- withdrawal policy
- error/status if present

## Phase 10: Live validation SQL

Before patch, run:

```sql
SELECT
  a.name,
  a.provider_id,
  a.enabled AS account_enabled,
  am.model_id,
  am.enabled AS account_model_enabled,
  pmm.protocol AS provider_model_protocol,
  pmm.protocol_source,
  pmm.resolution_status
FROM accounts a
LEFT JOIN account_models am ON am.account_id = a.id
LEFT JOIN provider_model_metadata pmm
  ON pmm.model_id = am.model_id
 AND pmm.provider_id = a.provider_id
WHERE a.name LIKE 'opencode-go-%'
  AND am.model_id = '<HOT_MODEL>'
ORDER BY a.name;
```

After patch and a refresh, verify `0002` and `0003` are not silently disabled.

Then run:

```sql
SELECT
  selected_account_name,
  eligible_count,
  scored_count,
  json_extract(score_components_json, '$.fairness.applied') AS fairness_applied,
  json_extract(score_components_json, '$.fairness.reason') AS fairness_reason,
  json_extract(score_components_json, '$.fairness.candidate_count') AS fairness_candidates,
  COUNT(*) AS n
FROM routing_decisions
WHERE created_at >= datetime('now', '-30 minutes')
GROUP BY selected_account_name, eligible_count, scored_count,
         fairness_applied, fairness_reason, fairness_candidates
ORDER BY n DESC;
```

Expected healthy outcome:

- `eligible_count = 3`
- `scored_count = 3`
- fairness applies for equal peers or explains `not_tied`
- distribution is no longer `0001` only

## Acceptance criteria

This corrective pass is complete when:

1. Failed model refreshes do not remove account/model support.
2. Empty model refresh responses do not remove account/model support by default.
3. Partial/unresolved protocol refreshes do not silently remove healthy accounts from routing.
4. Existing `account_models.enabled = 1` rows remain enabled unless config, health, or explicit confirmed-withdrawal policy suppresses them.
5. Static model seeds remain sticky and routable across failed/empty live refreshes.
6. Health/backoff remains the only automatic de-pooling mechanism for account-level failure.
7. A regression test with `opencode-go-0001`, `opencode-go-0002`, and `opencode-go-0003` proves all three stay eligible after failed/empty refreshes.
8. Routing diagnostics can show every account’s gate status for a given model/protocol.
9. Live Opencode Go testing shows all healthy same-tier accounts remain candidates and fairness has a chance to run.
10. `uv run pytest tests/unit/test_fairness.py` still passes.
11. New catalog-refresh preservation tests pass.
12. `uv run ruff check src/ tests/` and `uv run pyright src/ scripts/` pass.

## Implementation caution

Do not solve this by forcing the router to ignore catalog availability entirely. The catalog should still drive model discovery and protocol compatibility. The fix is narrower: catalog uncertainty must preserve prior known-good support, while explicit health/config states remain authoritative for removing an account from the pool.
