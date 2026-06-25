# Upstream-Authoritative Suppression Plan

## Problem Statement

EggPool currently appears able to enter a self-inflicted 503 state under sustained multi-session use with several accounts for the same upstream subscription provider. The observed field behavior is:

- `eggpool serve` works for roughly 5 to 10 minutes.
- Two client machines can use the same aggregator concurrently.
- Roughly 5 to 8 active OpenCode sessions are enough to trigger the issue.
- Four active Opencode Go subscriptions initially balance correctly.
- After the failure begins, requests return 503.
- Restarting EggPool does not fix the outage.
- Starting from a fresh SQLite database fixes the outage until enough traffic accumulates again.

That symptom pattern strongly implicates persisted local accounting state rather than process-local health state or an upstream outage. The likely failure path is that local quota/cost estimates are being treated as suppressive authority. Once local accounting decides every account has exceeded a configured microdollar budget, routing sees no eligible accounts and returns 503 even if the upstream provider has not actually rejected those accounts.

This is the wrong design for subscription aggregation. Local cost and quota estimates are useful for ranking, fairness, dashboards, and approximate utilization. They must not, by default, exclude accounts from routing. The authoritative suppressive signal should be an upstream-observed response such as authentication failure, quota exhaustion, rate limiting, model unavailability, transport failure, or upstream overload.

## Design Decision

Move suppressive authority away from guesstimated local quota accounting and toward observed upstream errors.

The invariant after this fix should be:

```text
Local usage estimates may influence account preference.
Only provider-observed failures, explicit operator disablement, catalog/protocol incompatibility, or an explicit hard-cap mode may make an account ineligible.
```

Local accounting should be advisory by default:

- Use local request/cost estimates for routing priority and fairness.
- Use local accounting for dashboard/statistics views.
- Use local accounting for reservation sizing and approximate utilization.
- Do not hard-suppress accounts from eligibility based on estimated local microdollar usage.
- Do not return 503 merely because every account is locally estimated above quota.

Upstream-observed errors should be authoritative:

- Upstream 401/403 means the selected account is authentically unusable until key/config correction or an explicit reset.
- Upstream 402 or provider-specific quota-exhaustion errors mean account suppression with bounded backoff.
- Upstream 429 means account suppression with `Retry-After` when present, otherwise exponential backoff.
- Upstream 5xx/connect/read failures mean retry/failover with shorter transient backoff.
- Model-specific 404 or model-unavailable errors should suppress only the account/model pairing when the provider/account is otherwise healthy.
- Context-limit errors should not globally suppress the account; they should surface as client/request errors or model-specific policy failures.

## Current Code Areas to Inspect

Likely relevant files:

- `src/eggpool/routing/eligibility.py`
- `src/eggpool/routing/router.py`
- `src/eggpool/quota/scorer.py`
- `src/eggpool/quota/estimation.py`
- `src/eggpool/health/health_manager.py`
- `src/eggpool/request/coordinator.py`
- `src/eggpool/request/finalizer.py`
- `src/eggpool/request/attempt_finalizer.py`
- `src/eggpool/retry/classification.py`
- `src/eggpool/db/repositories.py`
- `src/eggpool/db/schema/*`
- `src/eggpool/api/proxy_request.py`
- `src/eggpool/models/config.py`
- `config.example.toml`
- related unit/integration tests under `tests/`

Specific high-probability bug:

`routing/eligibility.py` currently hard-excludes accounts when `quota.is_within_limits()` is false. This converts estimated/local quota accounting into a suppressive eligibility gate. That behavior conflicts with the intended scorer policy where above-capacity accounts should remain scoreable but worse-ranked.

## Target Behavior

### Multi-account same-provider behavior

When multiple accounts can serve a request:

1. Select the best account using existing routing priority, health, active request count, and quota-fair scoring.
2. Dispatch to the selected account.
3. If the account returns a retryable or suppressive upstream error before the response body is committed to the client, record the failure/backoff for that account or account/model pair.
4. Retry the next eligible account, excluding already-attempted accounts for that request.
5. Continue until an account succeeds, the retry budget is exhausted, or all candidates have been attempted.
6. If all candidates fail, surface the most relevant upstream error to the client. Prefer preserving upstream status/body over returning a synthetic 503 when at least one upstream was actually reached.

### Single-account behavior

When only one account can serve a request:

1. Send the request upstream.
2. If upstream responds with an error, pass through the upstream status/body where safe and protocol-compatible.
3. Record health/backoff based on the upstream error.
4. Do not convert a real upstream response into `No accounts available` unless the request was never dispatchable at all.

### No-dispatch behavior

Return 503 only when EggPool cannot dispatch the request before reaching upstream. Examples:

- No enabled accounts.
- Missing credentials.
- Catalog unavailable and stale catalog is disallowed.
- Model is not known or not mapped to any configured account/provider.
- All accounts are explicitly disabled by operator configuration.
- All accounts are under active upstream-derived backoff and no pass-through candidate exists.

Do not return 503 because every account is locally estimated over a configured microdollar limit.

## Phase 1: Stop Local Quota Estimates from Hard-Gating Eligibility

Goal: remove the immediate self-inflicted outage mode.

Implementation steps:

1. In `src/eggpool/routing/eligibility.py`, remove the hard eligibility check that skips an account when `quota.is_within_limits()` is false.
2. Leave catalog freshness, provider filter, protocol compatibility, configured enablement, credentials, explicit health disablement, and circuit-breaker health checks intact.
3. Ensure `QuotaFairScorer` still reads persisted usage, reservations, offsets, active request count, and request estimate to rank high-utilization accounts worse.
4. Add a comment documenting that local quota state is advisory in default routing.
5. Add or update tests so an account above local quota remains eligible.
6. Add or update tests so an above-quota account receives a worse score than an under-quota account when both are available.

Suggested tests:

- `tests/unit/routing/test_eligibility.py`
  - Configure one enabled account that supports the model.
  - Configure quota capacity below persisted usage.
  - Assert `get_eligible_accounts(...)` still returns the account.

- `tests/unit/quota/test_scorer.py`
  - Configure two accounts with equal weight and support.
  - Give account A high persisted usage and account B low persisted usage.
  - Assert both accounts are scoreable.
  - Assert account B has lower final score.

Acceptance criteria:

- Sustained usage cannot make every account disappear from eligibility through local estimates alone.
- Existing local quota/cost telemetry remains visible and still affects rank.
- 503 caused by local estimated overage is no longer possible in the default mode.

## Phase 2: Add an Explicit Local Quota Mode

Goal: preserve an operator escape hatch while making advisory routing the default.

Add a config setting similar to:

```toml
[routing]
local_quota_mode = "score_only"  # score_only | hard_cap
```

Semantics:

- `score_only`: default. Local quota estimates affect routing score only.
- `hard_cap`: opt-in safety mode. Local quota capacities can suppress accounts from eligibility.

Implementation steps:

1. Add `local_quota_mode` to config models with validation.
2. Default to `score_only` in code and `config.example.toml`.
3. If `hard_cap` is enabled, reintroduce local quota eligibility suppression through an explicit branch, not by implicit default.
4. Add warning-level startup logging when `hard_cap` is enabled, because it can intentionally produce local 503s under estimate drift.
5. Document that subscription aggregators should normally use `score_only`.

Tests:

- Config default is `score_only`.
- Invalid mode is rejected.
- `hard_cap` preserves previous suppressive behavior only when explicitly enabled.
- `score_only` keeps accounts eligible even when above local capacity.

Acceptance criteria:

- Existing users who want hard local budget enforcement can opt in.
- Default behavior is safe for subscription aggregation and cannot brick routing based on estimated cost alone.

## Phase 3: Implement Reason-Specific Upstream Backoff Policy

Goal: make upstream-observed errors the authoritative suppression mechanism.

Create a dedicated policy layer, for example:

- `src/eggpool/health/backoff.py`
- or extend `src/eggpool/health/health_manager.py` with a separate `BackoffPolicy` object.

Suggested policy:

```text
authentication_failed:
  scope: account
  behavior: disable until explicit reset/reload/manual enable

quota_exhausted:
  scope: account, or account+model if provider error clearly model-scoped
  base_delay: 5 minutes
  multiplier: 2
  cap: 24 hours
  jitter: 10-20%

rate_limited:
  scope: account by default
  base_delay: Retry-After if present, otherwise 60 seconds
  multiplier: 2
  cap: 24 hours
  jitter: 10-20%

upstream_server_error:
  scope: account
  base_delay: 15-30 seconds
  multiplier: 2
  cap: 10-30 minutes unless repeated across many attempts
  jitter: 10-20%

connect_timeout/connection_failure/protocol_error:
  scope: account or provider transport
  base_delay: 15-60 seconds
  multiplier: 2
  cap: 10-30 minutes
  jitter: 10-20%

model_unavailable:
  scope: account+model
  base_delay: 5 minutes
  multiplier: 2
  cap: 24 hours

context_limit_exceeded:
  scope: none for account suppression
  behavior: client/request error, not account health failure
```

Implementation details:

1. Normalize upstream error categories through `retry/classification.py` and `health/classify_failure_category(...)`.
2. Capture `Retry-After` from upstream headers for 429 and provider-specific quota errors if available.
3. Record consecutive failures per suppression scope.
4. Reset the failure count and clear suppression on success for the relevant scope.
5. Ensure auth failure remains terminal/manual rather than exponential.
6. Ensure context-limit and client-side 4xx errors do not poison account health.

Acceptance criteria:

- Suppression duration is caused by actual upstream failures.
- Backoff is bounded at 24 hours for quota/rate-limit style failures.
- Success clears transient backoff.
- Context-limit failures do not suppress accounts globally.

## Phase 4: Persist Backoff State in SQLite

Goal: avoid losing real upstream-derived suppression state across restarts while avoiding persistent local-estimate suppression.

Add a migration for a table similar to:

```sql
CREATE TABLE account_backoffs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    model_id TEXT,
    reason TEXT NOT NULL,
    status_code INTEGER,
    error_class TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 1,
    backoff_until TEXT,
    last_failure_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(account_id, model_id, reason)
);

CREATE INDEX idx_account_backoffs_active
    ON account_backoffs(backoff_until);

CREATE INDEX idx_account_backoffs_account_model
    ON account_backoffs(account_id, model_id);
```

Notes:

- `model_id` null means account-wide suppression.
- Auth failures may use `backoff_until` null plus a reason/state meaning manual recovery required, or may continue to live in the existing health state if already persisted elsewhere.
- Avoid persisting estimated local quota overage as a backoff reason.

Repository work:

1. Add `AccountBackoffRepository` to `db/repositories.py` or a separate repository module.
2. Add methods:
   - `upsert_failure(...)`
   - `clear_success(account_id, model_id=None)`
   - `list_active(now)`
   - `expire_old(now)`
   - `clear_account(account_id)`
3. Hydrate active backoffs into `HealthManager` during startup after account sync.
4. Record/refresh backoff after failed attempt finalization.
5. Clear transient backoff after successful request finalization.

Acceptance criteria:

- A true upstream rate-limit/quota backoff survives EggPool restart.
- A false local quota estimate cannot create a durable backoff.
- Expired backoffs no longer suppress routing after their deadline.
- Dashboard/status APIs can expose durable backoff state.

## Phase 5: Tighten Multi-Account Failover Semantics

Goal: transparently handle failures when several accounts can serve the same model/provider.

Implementation steps:

1. In `RequestCoordinator.execute`, ensure retryable pre-body upstream failures select the next account and do not retry the same account during the same request.
2. Ensure the failing account receives backoff/suppression before the next selection pass.
3. Ensure `context.attempted_accounts` is always updated after durable attempt creation and before retry selection.
4. Ensure retry selection checks active upstream-derived backoff but ignores local estimated overage in `score_only` mode.
5. Preserve the most useful upstream error response in case all accounts fail.
6. If at least one upstream was reached, prefer returning upstream error/pass-through over synthetic 503 when all fail.

Important distinction:

- `No eligible account before dispatch` can be 503.
- `All upstream accounts rejected/failed after dispatch attempts` should usually be 429, 402, 401, 502, or the most relevant upstream status, not 503.

Tests:

- Two accounts, first returns 429, second succeeds: final client response is success, first account gets backoff.
- Two accounts, first returns 402, second succeeds: final client response is success, first account gets quota backoff.
- Two accounts, both return 429: final response is upstream-like 429 or the selected final upstream response, not synthetic no-account 503.
- One account returns 429: response passes through 429 and account gets backoff.
- One account returns 402: response passes through 402 and account gets quota backoff.

Acceptance criteria:

- Multi-account same-provider routing transparently fails over on suppressive upstream errors.
- Single-account mode behaves like a proxy and preserves upstream error semantics.
- Synthetic 503 is reserved for pre-dispatch unavailability.

## Phase 6: Improve Error Classification and Header Handling

Goal: make suppression decisions correct across providers.

Implementation steps:

1. Audit `retry/classification.py` and `health/classify_failure_category(...)`.
2. Ensure status codes map as follows:
   - 401/403 -> authentication/authorization failure.
   - 402 -> quota exhausted.
   - 408/timeout exceptions -> timeout/transient.
   - 409/422 -> provider-specific; do not blindly suppress account unless classified.
   - 429 -> rate limited.
   - 500/502/503/504 -> upstream/transient failure.
3. Parse provider error bodies for quota/rate-limit terms where status code is ambiguous.
4. Parse `Retry-After` as seconds or HTTP date.
5. Preserve sanitized upstream status/body when pass-through is appropriate.
6. Ensure sensitive headers are removed from pass-through responses.

Acceptance criteria:

- Provider-specific quota/rate-limit messages are classified correctly.
- Retry-After is honored and bounded.
- Client/request errors do not degrade account health.

## Phase 7: Fix Cost/Usage Inflation Separately

Goal: prevent exaggerated statistics while keeping routing robust even when estimates are imperfect.

This is separate from suppressive policy. The router must remain robust even when accounting is wrong. Still, exaggerated aggregate usage should be investigated.

Audit:

1. Request reservation creation and release paths.
2. Streaming finalization paths, especially client disconnect and cancellation.
3. Whether estimated reservation cost is being added in addition to actual final cost.
4. Whether retry attempts double-count cost at request and attempt levels.
5. Whether persisted window snapshots are incremented more than once per transitioned request.
6. Whether `exactness = estimated` requests dominate cost totals for providers without reliable usage fields.
7. Whether subscription providers without token pricing should use `requests`, not dollar-like microdollars, for primary balancing.

Suggested diagnostic queries for manual testing:

```sql
SELECT a.name,
       COUNT(*) AS requests,
       SUM(r.cost_microdollars) AS total_cost,
       SUM(CASE WHEN r.started_at >= datetime('now', '-5 hours') THEN r.cost_microdollars ELSE 0 END) AS cost_5h,
       SUM(CASE WHEN r.status = 'pending' THEN r.reserved_microdollars ELSE 0 END) AS pending_reserved
FROM accounts a
LEFT JOIN requests r ON r.account_id = a.id
GROUP BY a.id, a.name
ORDER BY a.name;
```

```sql
SELECT a.name,
       COUNT(*) AS active_reservations,
       SUM(reserved_microdollars) AS active_reserved
FROM reservations r
JOIN accounts a ON a.id = r.account_id
WHERE r.status = 'active'
GROUP BY a.id, a.name;
```

```sql
SELECT exactness,
       COUNT(*) AS n,
       SUM(cost_microdollars) AS total_cost
FROM requests
GROUP BY exactness;
```

Acceptance criteria:

- Completed requests are counted once.
- Released reservations do not remain active after request finalization.
- Retried attempts do not double-charge local usage unless actual upstream work was done and usage is observed.
- Unknown/estimated usage is clearly separated from exact usage in stats and dashboard.

## Phase 8: Dashboard and Operator Visibility

Goal: make routing state understandable during incidents.

Expose separate fields for:

- Local estimated usage.
- Exact observed usage.
- Active reservations.
- Active request count.
- Routing score/utilization.
- Upstream-derived backoff reason.
- Backoff expiration time.
- Consecutive upstream failure count.
- Authentication failure state.
- Explicit operator disabled state.
- Catalog freshness/model availability.

Avoid conflating:

- `estimated over local budget`
- `upstream quota exhausted`
- `upstream rate limited`
- `auth failed`
- `model unavailable`
- `operator disabled`

Acceptance criteria:

- During the reproduced bug scenario, the dashboard/status view should show accounts as high-utilization but still routable unless upstream errors have occurred.
- If upstream rate-limits or quota-exhausts an account, the dashboard should show that exact authoritative reason and backoff deadline.

## Phase 9: Reproduction and Verification Scenario

Build an integration test or scripted smoke test with a fake provider.

Scenario A: Local overage must not suppress

1. Configure four accounts for one fake provider.
2. Configure tiny local microdollar capacities.
3. Seed usage above capacity for all four accounts.
4. Fake provider returns success for all accounts.
5. Send requests.
6. Assert requests succeed and do not return 503.
7. Assert high-usage accounts are still ranked but not excluded.

Scenario B: Upstream failure suppresses and fails over

1. Configure four accounts.
2. Fake provider returns 429 for account 1 and success for account 2.
3. Send request.
4. Assert client receives success.
5. Assert account 1 has active rate-limit backoff.
6. Assert subsequent requests avoid account 1 until backoff expires.

Scenario C: Single-account pass-through

1. Configure one account.
2. Fake provider returns 429.
3. Assert client receives 429, not 503.
4. Assert backoff is recorded.

Scenario D: Restart preserves authoritative backoff only

1. Trigger upstream 429 for account 1.
2. Restart app using same database.
3. Assert account 1 remains in backoff until deadline.
4. Seed local overage for account 2.
5. Restart app.
6. Assert account 2 is not suppressed in `score_only` mode.

## Suggested Implementation Order for a Smaller Model

1. Make the minimal safe fix: remove local quota hard-gate from eligibility and add tests.
2. Add `local_quota_mode = "score_only"` config default and tests.
3. Improve existing health/backoff behavior in memory only.
4. Tighten failover/pass-through semantics for multi-account and single-account cases.
5. Add durable backoff migration/repository/hydration.
6. Add dashboard/status fields.
7. Investigate and correct exaggerated cost accounting.

This order prevents the production-like 503 outage first, then adds the more complete upstream-authoritative suppression model.

## Non-Goals

- Do not remove quota/cost accounting entirely.
- Do not remove routing fairness.
- Do not rely on local estimates as authoritative provider quota.
- Do not turn every upstream error into account suppression.
- Do not suppress accounts for context-limit or malformed-client-request errors.
- Do not conflate exact usage, estimated usage, and subscription-plan request limits.

## Final Acceptance Criteria

The fix is complete when all of the following are true:

1. Local estimated usage cannot make all accounts disappear from eligibility in the default configuration.
2. High local usage still affects routing priority and dashboard statistics.
3. Upstream 429/402/5xx-style failures cause bounded account or account/model backoff.
4. Backoff uses exponential growth with jitter and a maximum of 24 hours where appropriate.
5. Retry/failover transparently tries another account when multiple accounts can serve the request.
6. Single-account upstream errors pass through to the client rather than becoming synthetic no-account 503s.
7. Durable backoff survives restart, but local estimated overage does not become durable suppression.
8. Tests cover local-overage routing, upstream-derived suppression, multi-account failover, single-account pass-through, and restart hydration.
