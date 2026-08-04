# Plan 073 — Bounded Backoff and Router Self-Healing

Date: 2026-08-04
Status: completed
Parent roadmap: `plans/070-failure-resilience-router-recovery-and-sbc-simplification-roadmap.md`
Depends on:

- `plans/071-attempt-scoped-failure-classification-and-effects.md`
- `plans/072-upstream-dispatch-retry-and-response-isolation.md`

Planning baseline: `e73db213e7e381043cda3cfb8a3dd8109f3f39ca`

## Purpose

Ensure every temporary routing suppression is bounded, recoverable, correctly scoped, and unable to leave an account/model/circuit unavailable indefinitely after the underlying provider condition has cleared.

The maximum nonterminal backoff must be reduced from one day to 30 minutes. Authentication failure and authoritative model withdrawal remain explicit terminal states, but they must have clear operator or authoritative recovery paths and must not be confused with temporary runtime observations.

## Confirmed defects and risks

### 1. Several transient policies cap at 24 hours

Current backoff policy caps include:

- quota exhausted: 86,400 seconds;
- rate limited: 86,400 seconds;
- model unavailable: 86,400 seconds;
- server and transport errors: 1,800 seconds.

A 24-hour runtime suppression is disproportionate for a private aggregation proxy and can make a corrected account look permanently unusable.

### 2. Retry-After can create sticky suppression

The current policy honors provider `Retry-After` and clamps it to the policy cap. With a one-day cap, a malformed, stale, or intentionally extreme header can remove an account for a day.

The final jittered delay must also remain within the cap.

### 3. Recovery exits are spread across multiple subsystems

Current recovery can involve:

- `HealthManager.record_success()`;
- transient cooldown expiry;
- durable backoff row deletion;
- model quarantine clear-on-success;
- catalog reappearance;
- account/model CLI enable operations;
- circuit half-open success.

These must agree on scope and terminality.

### 4. Durable rows can outlive in-memory recovery

Backoff writes and clears are intentionally best-effort so SQLite analytics failure does not fail a client request. If a clear fails, a stale row can be rehydrated after restart and suppress an account that had recovered.

A bounded 30-minute expiry mitigates this, but hydration must also reject expired, malformed, contradictory, and absurd future rows.

### 5. Model-unavailable state can become broader than its evidence

A runtime model-like 404 should suppress only the account/model pair for a bounded period. It must not disable the entire account or create permanent model withdrawal without authoritative provider-catalog evidence.

### 6. Probe/circuit recovery must be total

Every half-open probe acquisition must end through success, provider failure, request-local rejection, cancellation, or terminal cleanup. A suppression expiry alone is insufficient if the breaker still retains an in-flight half-open flag.

## Scope

Primary files:

- `src/eggpool/health/backoff.py`
- `src/eggpool/health/health_manager.py`
- `src/eggpool/health/circuit_breaker.py`
- `src/eggpool/failure/classifier.py`
- `src/eggpool/failure/applier.py`
- `src/eggpool/failure/quarantine.py`
- `src/eggpool/request/coordinator.py`
- account backoff repositories and startup hydration paths;
- existing account/model reset CLI/API paths;
- configuration examples and architecture documentation.

## Explicitly out of scope

- a distributed health service;
- active background probing of every account/model;
- a retry queue for failed client requests;
- provider-specific machine-learning health prediction;
- cost-based routing;
- automatic credential rotation;
- hiding authentication failures behind a temporary cooldown;
- changing authoritative provider catalog semantics without evidence;
- adding a new database table if the current backoff table can express bounded expiry and scope;
- adding notification or alerting infrastructure;
- live provider tests or wall-clock sleeps.

## Governing decisions

1. Every nonterminal runtime backoff caps at 1,800 seconds.
2. The cap applies after exponential growth, `Retry-After`, and jitter.
3. No negative, NaN, infinite, or absurd time value enters health or durable state.
4. Runtime model absence is account/model scoped and bounded.
5. Authentication failure is account scoped and terminal until explicit reset or credential/config replacement during validated rehash.
6. Authoritative provider-catalog withdrawal is account/model scoped and terminal until authoritative reappearance or explicit operator action.
7. Success clears matching transient state, not unrelated terminal state.
8. Durable backoff is a restart hint; it cannot be the sole process-local routing authority.
9. Expired rows are ignored and opportunistically deleted.
10. Recovery paths must be idempotent.
11. No periodic high-frequency health sweeper is added; lazy expiry during eligibility checks and existing low-frequency maintenance are sufficient.

## Phase A — Cap all nonterminal policies at 30 minutes

### Required policy changes

Update `get_backoff_policy()` so these reasons use a cap of `1800.0` seconds:

- `quota_exhausted`;
- `rate_limited`;
- `upstream_server_error`;
- `connect_timeout`;
- `connection_failure`;
- `protocol_error`;
- runtime `model_unavailable` quarantine.

Authentication failure remains terminal and returns no exponential delay.

Context-limit and request-local validation remain no-backoff.

### Retry-After handling

1. Parse numeric and HTTP-date values as currently supported.
2. Reject non-finite or invalid values.
3. Clamp valid values to `[0, 1800]` for nonterminal policies.
4. Apply jitter without allowing the final value to exceed 1,800 seconds.
5. Preserve a zero value as immediate eligibility recovery where the provider explicitly requests zero wait.
6. Do not use a provider `Retry-After` to convert authentication or authoritative withdrawal into a temporary state.

### Exponential behavior

- Preserve reason-specific base delays unless a focused behavior test shows a concrete problem.
- Preserve jitter, but make deterministic tests inject or disable it.
- Cap exponent growth before floating-point overflow.
- Avoid storing a `max_consecutive` value whose only effect occurs after the cap has already been reached; simplify where safe.

### Acceptance criteria

- No nonterminal policy returns more than 1,800 seconds.
- A `Retry-After: 86400` results in at most 1,800 seconds.
- Jitter cannot push a capped value above 1,800 seconds.
- NaN, infinity, negative HTTP-date deltas, and malformed values use the bounded fallback.
- Authentication remains terminal and is not assigned an arbitrary one-year timestamp.

## Phase B — Define scoped success and expiry behavior

### Account-wide transient success

A successful request on an account should:

- reset consecutive transient failure count;
- record one circuit success;
- clear transient account-wide health state and cooldown;
- clear matching durable account-wide rows for:
  - quota exhaustion;
  - rate limiting;
  - server error;
  - connect timeout;
  - connection failure;
  - protocol error;
- preserve authentication failure if an explicit operator/config action has not re-enabled the account.

A success cannot ordinarily occur while authentication failure is enforced, but late in-flight success must not undo an operator disable or terminal auth state.

### Account/model success

A successful request for one account/model should:

- clear bounded runtime model quarantine for that exact account/model/protocol identity;
- clear the matching bounded durable `model_unavailable` row;
- preserve authoritative terminal withdrawal unless catalog reappearance or operator action proves recovery;
- not enable the same model on other accounts automatically.

### Expiry

Eligibility checks may lazily refresh transient state:

- when `now >= cooldown_until`, restore ordinary health and clear cooldown fields;
- when a bounded model disable expires, remove it;
- when a circuit open interval expires, transition through the existing half-open policy;
- expiry must not reset terminal authentication or authoritative withdrawal.

### Acceptance criteria

- Success on account A does not clear account B state.
- Success for model M on account A does not clear model N or authoritative withdrawal.
- Expired quota/rate/transport cooldown restores eligibility without restart.
- Late success cannot override explicit operator disable/auth failure.
- All clear operations are idempotent.

## Phase C — Harden durable backoff persistence and hydration

### Write contract

Persist only normalized values from the Plan 071 decision:

- actual account ID;
- optional model ID for account/model scope;
- normalized reason;
- bounded status/error class metadata;
- finite epoch deadline or `NULL` only for explicit terminal reasons;
- nonnegative bounded consecutive count.

Do not persist raw response bodies, headers, exception messages, or credentials.

### Best-effort availability contract

- A backoff write failure is logged and cannot fail an otherwise valid client response.
- In-memory state remains authoritative for the current process.
- Do not add a durable queue solely to retry backoff analytics.
- A bounded retained terminal owner may retry a failed write only if that write is already one of its component obligations; it must not delay downstream response after correctness ownership has converged.

### Hydration contract

At startup or rehash hydration:

1. ignore rows for missing/disabled accounts;
2. ignore and delete expired bounded rows;
3. reject non-finite/unparseable deadlines;
4. clamp or reject deadlines more than 1,800 seconds in the future for nonterminal reasons;
5. treat `NULL` as terminal only for the explicit terminal reason set;
6. do not let an unknown reason disable anything;
7. scope runtime model-unavailable rows to the exact account/model;
8. merge duplicate rows deterministically, preferring the strictest valid state within the same scope but never promoting a bounded runtime reason to terminal;
9. continue startup if deleting an expired hint fails, but do not apply the expired hint.

### Migration of existing long rows

Do not add a schema migration merely to change policy constants.

On first hydration after implementation:

- existing nonterminal rows with a future deadline beyond 30 minutes should be clamped in memory to `now + 1800`;
- opportunistically update or replace the row with the bounded deadline;
- if the update fails, continue with bounded in-memory state;
- terminal authentication and authoritative withdrawal rows remain unchanged.

### Acceptance criteria

- A stale 24-hour row cannot suppress more than 30 minutes after upgraded startup.
- An expired row is never applied even if deletion fails.
- An unknown or malformed row is visible in logs but has zero routing effect.
- A runtime model row cannot disable the whole account.
- Persistence failure does not crash or reject unrelated proxy traffic.

## Phase D — Guarantee circuit and probe self-healing

### Required changes

1. Consume Plan 071 attempt-scoped component progress.
2. For every acquired half-open probe, one terminal path must mark it converged:
   - provider success;
   - provider failure;
   - request-local rejection after selection;
   - cancellation;
   - local internal error;
   - retained finalization/startup repair.
3. `release_request()` must remain idempotent.
4. Circuit open duration must be finite for transient failures.
5. When an open interval expires, one half-open request may probe; other requests route elsewhere where alternatives exist.
6. A failed half-open probe reopens once, not twice.
7. A successful half-open probe closes the breaker and clears transient consecutive failure state.
8. A request that never reached upstream but held a probe releases it without success/failure attribution.
9. Rehash/account replacement must not carry a stale in-flight probe into a new account identity.

### Startup safety

Process-local half-open in-flight flags do not survive restart. Durable backoff hydration restores only reason/deadline state, not an in-flight probe. This is intentional.

### Acceptance criteria

- No selected terminal path leaves the half-open flag set.
- Duplicate cleanup is a no-op.
- A failed half-open attempt changes the failure count once.
- A later success restores routing without restart.
- Rehash does not inherit probe state across different durable account identities.

## Phase E — Make operator and authoritative recovery explicit

### Authentication recovery

Use existing configuration/CLI mechanisms where possible:

- validated rehash with corrected credentials may re-enable the account only after the account identity/config transition succeeds;
- explicit account enable/reset clears authentication failure and resets the circuit;
- merely waiting does not clear authentication failure;
- an unrelated model success does not clear authentication failure.

### Authoritative model recovery

- a provider catalog refresh that authoritatively advertises the model clears authoritative withdrawal for that exact account/model/protocol identity;
- operator model enable/reset may clear it explicitly;
- a runtime response from a different account does not clear it;
- bounded runtime quarantine may clear on matching success without requiring catalog refresh.

### Diagnostics

Existing account explanation/runtime stats should distinguish at least:

- transient cooldown with remaining seconds;
- rate limit;
- quota exhausted;
- bounded runtime model quarantine;
- terminal authentication failure;
- authoritative model withdrawal;
- circuit open/half-open.

Do not add another database event subsystem. Extend existing bounded diagnostics only where fields already exist or a small computed value is sufficient.

### Acceptance criteria

- Every terminal state has one explicit operator or authoritative exit.
- Temporary states never require an operator reset.
- Diagnostics do not label bounded runtime model quarantine as permanent withdrawal.
- Recovery commands are idempotent and do not clear unrelated state.

## Phase F — Focused regressions

Required representative tests:

1. quota failures grow but cap at 1,800 seconds;
2. rate-limit `Retry-After` above cap is clamped after jitter;
3. server/transport/model runtime backoffs cap at 1,800 seconds;
4. authentication has no timed expiry;
5. successful account request clears matching transient account rows/state;
6. matching account/model success clears bounded runtime model quarantine;
7. success does not clear auth or authoritative withdrawal;
8. expired durable row is ignored even if delete fails;
9. existing long nonterminal row is bounded during hydration;
10. malformed/unknown row has no routing effect;
11. half-open success closes the breaker;
12. half-open failure reopens once;
13. cancellation/local rejection releases the probe;
14. rehash with corrected credentials uses explicit reset semantics.

Use a fake clock or explicit `now`; no wall-clock sleeps.

## Verification

```bash
uv run ruff format src/eggpool/health src/eggpool/failure src/eggpool/request src/eggpool/db tests/
uv run ruff check src/eggpool/health src/eggpool/failure src/eggpool/request src/eggpool/db tests/
uv run pyright src/eggpool/health src/eggpool/failure src/eggpool/request src/eggpool/db
uv run pytest <affected backoff/health/hydration/router tests> -q --tb=short --maxfail=1
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
```

Run the normal repository gate afterward. Do not add a periodic integration test, live provider test, or CI matrix.

## Recommended implementation sequence

1. Change and test policy caps and final clamping.
2. define exact success/expiry scope.
3. harden durable write/hydration normalization.
4. migrate existing long rows lazily at hydration.
5. prove attempt-scoped probe convergence.
6. align operator and authoritative reset paths.
7. update existing diagnostics and documentation.
8. run focused tests and smoke.
9. stop; do not add active probing or alerting.

## Plan acceptance criteria

- [x] Every nonterminal backoff is at most 1,800 seconds.
- [x] Retry-After and jitter cannot exceed the cap.
- [x] Authentication and authoritative withdrawal remain explicit terminal states.
- [x] Runtime model absence is bounded and account/model scoped.
- [x] Matching success clears only matching transient state.
- [x] Expiry restores temporary eligibility without restart.
- [x] Expired, malformed, unknown, or absurd durable rows have no unbounded routing effect.
- [x] Existing 24-hour nonterminal rows are bounded during upgraded hydration.
- [x] Persistence failure does not fail client traffic.
- [x] Every acquired probe converges on every terminal path.
- [x] Half-open failure/success changes the circuit once.
- [x] Operator and authoritative exits are explicit and idempotent.
- [x] Focused regressions and smoke pass.
- [x] No active probe fleet, queue, schema expansion, notification system, live-provider suite, or CI expansion is added.

## Rejection conditions

Do not close this plan if:

- any transient policy or Retry-After can suppress beyond 30 minutes;
- jitter can exceed the cap;
- runtime model 404 can create permanent withdrawal without authoritative evidence;
- success clears authentication or unrelated model state;
- an expired row is applied because deletion failed;
- malformed durable data disables routing;
- a half-open probe can remain occupied after cancellation or local error;
- a single failed half-open attempt records two failures;
- temporary recovery requires restart or operator reset;
- implementation adds active background probing or another durable queue.

## Definition of done

Plan 073 is complete when all temporary account/model suppression is capped at 30 minutes, provider Retry-After cannot extend it, success and expiry restore only the correct scope, durable hints cannot resurrect stale or malformed suppression, every circuit probe has a total recovery path, and terminal authentication/authoritative withdrawal have explicit non-automatic exits.
