# Plan 151 — Negotiation-Safe Failure Effects and Shared Attempt Budgeting

Date: 2026-09-02
Status: ready after Plan 148; integrate with Plan 150
Parent roadmap: `plans/147-dynamic-wire-surface-negotiation-roadmap.md`
Depends on: Plan 148; coordinates with Plans 149–150
Planning baseline: `0bc0e02bbea5eebae70b247542d084e6fa6b122f`
Priority: P0 failure isolation / API correctness
Execution target: GPT-5.6 Luna or comparable implementation model

## Purpose

Make EggPool's canonical failure system able to distinguish:

- invalid credentials;
- a wire/auth/header mismatch;
- an unsupported endpoint/surface;
- request-schema incompatibility;
- model absence;
- account quota/rate limit;
- provider/server/transport failure;
- local transcoder/capability failure.

Then use those distinctions to authorize exactly one of:

```text
no retry
same account + alternate wire profile
other account + same wire profile
existing provider/model fallback
```

This phase must eliminate the current failure mode where an arbitrary HTTP 401 can permanently mark an account as `authentication_failed`, repeat the same bad request against other accounts, and exhaust an entire provider pool.

---

# Immediate P0 safety patch

The first implementation step may land independently before the rest of dynamic negotiation:

> A numeric HTTP 401 by itself must no longer be sufficient evidence to set `account_effect="disable_auth"`.

Current `failure/classifier.py` special-cases `MODEL_ABSENT`, but every other 401 falls through to durable authentication failure. Current `health_manager.record_failure(... reason="authentication_failed")` makes that state non-self-healing under ordinary success.

Change this before enabling broad surface negotiation.

Until explicit credential evidence is available:

- unknown/bare 401 -> no durable account health mutation;
- no account-wide auth blacklist;
- no automatic other-account retry merely because the status is 401;
- return a bounded client/upstream auth/wire failure as appropriate.

Preserve explicit confirmed invalid-credential handling once the new signal distinction is in place.

---

# Extend canonical failure signals

Update `failure/signal.py` / `signal_extract.py` with explicit structural signals. Exact names may follow current conventions, but semantics must include:

```text
CREDENTIAL_INVALID
WIRE_AUTH_MISMATCH
WIRE_SURFACE_UNSUPPORTED
WIRE_SCHEMA_MISMATCH
MODEL_ABSENT
RATE_LIMITED
QUOTA_EXHAUSTED
UNSUPPORTED_REQUEST_CONTROL
GENERIC_CLIENT_VALIDATION
TRANSPORT_FAILURE
```

Do not infer all of these from status alone.

### Credential-invalid evidence

Strong evidence includes provider-structured errors/messages that explicitly state the supplied credential/token is invalid, expired, revoked or otherwise rejected as a credential.

Examples of semantic intent:

```text
invalid API key
invalid token
expired credential/token
revoked API key
```

Patterns must be conservative and tested against current real provider response shapes where available.

### Wire-auth mismatch evidence

Messages such as:

```text
missing API key
x-api-key required
Authorization header required
missing authentication header
```

may indicate that EggPool used the wrong endpoint/header contract, not that the configured key is invalid.

Classify them as wire-auth mismatch when the request actually had a configured credential and an alternate configured wire profile/auth shape exists. Do not make raw message strings the only source of truth if a provider returns a structured error code/type.

A wire-auth mismatch must not disable the account.

### Surface unsupported

Recognize:

- HTTP 405 on a configured candidate endpoint;
- HTTP 404 with no model-absence evidence when the request path itself is the likely missing resource;
- structured unsupported endpoint/API/surface errors;
- provider-specific structured codes normalized at the HTTP boundary only where needed.

Bare 404 is safe for alternate-surface negotiation because the server rejected the resource before inference, but preserve `MODEL_ABSENT` precedence when body evidence says the model is missing.

### Wire schema mismatch

Do **not** treat generic 400/422 as a surface mismatch.

Only authorize alternate-surface negotiation when the response specifically identifies an API/request-shape incompatibility, such as an endpoint expecting a different required top-level shape or explicitly rejecting known surface fields before inference.

Unknown parameter errors for optional controls (thinking level, `reasoning_effort`, unsupported response format) should remain request/capability errors unless the evidence specifically proves the entire wire dialect is wrong.

This avoids reacting to one unsupported thinking field by moving the whole request to another API unnecessarily.

---

# Extend `FailureEffects`

Current effects primarily encode `retry: bool` plus `retry_scope="other_account"`.

Make retry destination explicit enough for negotiation.

Preferred shape:

```text
retry_action:
  none
  alternate_wire_same_account
  other_account_same_wire
  existing_route_retry
```

or retain `retry` plus an extended typed `retry_scope` if that minimizes churn:

```text
none
same_account_other_wire
other_account
```

The important requirement is that the coordinator not infer the retry destination from status code after classification.

Add a separate wire effect if clearer:

```text
wire_effect:
  none
  reject_candidate
  invalidate_preference
```

Avoid overfitting the names; one immutable canonical decision should still own all shared-state effects.

---

# Failure decision matrix

Implement and test approximately this semantic matrix.

| Failure evidence | Account effect | Wire effect | Retry destination |
| --- | --- | --- | --- |
| explicit invalid/expired/revoked credential | disable only selected account | none | other account, same best-known wire |
| bare/unknown 401 | none | none unless separate wire evidence | normally none |
| explicit missing/wrong auth-header shape | none | reject/invalidate candidate | same account, alternate wire if safe/configured |
| endpoint 405 | none | reject/invalidate candidate | same account, alternate wire |
| generic/path 404 without model evidence | none | reject/invalidate candidate | same account, alternate wire |
| model absent structured evidence | existing model/account scope | none | existing routing policy |
| explicit wire-schema mismatch | none | reject/invalidate candidate | same account, alternate wire |
| unsupported optional request control | none | none | local/client error or existing control adaptation |
| generic 400/422 | none | none | client error |
| 429 | existing account rate limit | none | other account using same wire; stop negotiation |
| quota 402/403 | existing quota effect | none | other account using same wire |
| 5xx | existing bounded model/account failure | none | existing retry policy, never surface enumeration |
| transport failure before/after possible transmit | existing transport policy | none | existing retry policy only; never infer surface |
| midstream error/truncated stream | existing stream failure | none | no retry after handoff |
| local transcode/capability failure | none | none | local/client error |

Where existing behavior intentionally differs, preserve it only if it remains safe under the key invariant: **surface knowledge and durable credential health may change only from evidence about that exact dimension**.

---

# Shared total attempt budget

Do not introduce a separate negotiation retry limit that can stack on top of account retries.

Represent one per-request attempt budget initialized from current routing policy:

```text
total_allowed_upstream_submissions = max_retries_before_stream + 1
```

Every actual upstream HTTP submission consumes one slot, whether it is:

- first attempt;
- alternate surface on the same account;
- another account after 429/auth failure;
- existing provider/model fallback.

Local validation, cache lookup, candidate enumeration and waiting for a single-flight owner do not consume attempts because they do not submit upstream traffic.

If the budget is exhausted, return the best canonical final failure; do not make one more "negotiation probe".

### Current default

At the reviewed baseline:

```text
max_retries_before_stream = 3
```

therefore the normal default budget is four actual upstream submissions. Do not raise this merely because wire negotiation is added.

---

# Avoid retry multiplication

The coordinator must not nest loops like:

```python
for account in accounts:
    for surface in surfaces:
        send()
```

Instead the canonical decision chooses the next dimension.

Examples:

## Surface rejection

```text
account A / Responses
  -> WIRE_SURFACE_UNSUPPORTED
account A / Chat
  -> accepted
```

Do not try account B/Responses first.

## Account rate limit

```text
account A / Responses
  -> 429
account B / Responses
```

Do not try Chat/Messages because 429 contains no surface evidence.

## Confirmed bad credential

```text
account A / Messages
  -> explicit invalid credential
account B / Messages
```

Do not mark Messages invalid.

## Optional thinking control rejected

```text
account A / Responses + unsupported effort
  -> control/capability handling
```

Do not infer that Responses itself is invalid unless the error specifically says the endpoint/schema is incompatible.

---

# Duplicate-inference protection

Alternate-surface negotiation is dangerous if the previous attempt might already be processing.

Add or preserve enough transport-phase evidence in `FailureObservation` to distinguish:

- locally rejected before send;
- response status received with deterministic 4xx;
- connect failure before request transmission where existing safe retry applies;
- write started/completed;
- response started;
- downstream started;
- stream/midstream.

Only deterministic HTTP rejection responses can authorize a wire transition in the first implementation.

Do **not** negotiate after:

- read timeout;
- post-write connection reset;
- unknown transport error where send status is ambiguous;
- response stream starts and then fails;
- downstream response start.

This is stricter than generic account retry and intentionally so. Sending the same logical request to a different endpoint after an ambiguous failure can duplicate model/tool work.

---

# Provider/account health application

Update `failure/applier.py` and health-manager interactions so:

- `alternate_wire_same_account` has no account failure/circuit penalty solely due to surface mismatch;
- candidate rejection updates only the Plan 150 wire resolver/cache;
- explicit credential invalidity still calls the existing durable auth-invalid account path;
- bare/unknown 401 never calls that path;
- local/request capability failures never call provider health mutation;
- per-model 5xx behavior retains current latest isolation improvements and does not regress to account-wide poisoning.

Surface learning is not health state. Do not add surface failures into account circuit-breaker counts.

---

# Auth recovery semantics

Existing authentication-failed state is deliberately sticky. Preserve that only for **confirmed** credential failure.

For an account already marked `authentication_failed` under old runtime behavior, do not automatically resurrect it in this phase without explicit proof/rehash/operator action; changing persisted historical account state is a separate migration concern.

However, after the fix, new ambiguous 401/wire failures must not create new sticky auth-invalid rows.

Consider a narrow startup/rehash cleanup only if the existing app version stores false auth poison in a way that would make the corrected release remain unusable. If required, scope it to a versioned compatibility migration and document exactly which rows are reset. Do not routinely clear genuine invalid credentials.

---

# Signal extraction constraints

`signal_extract.py` may inspect bounded response error material already permitted by current code, but:

- do not persist raw bodies;
- cap text inspected;
- normalize case safely;
- prefer structured error type/code/message fields over broad substring matching;
- order specific signals before generic ones;
- preserve OpenCode Go `MODEL_ABSENT` precedence;
- add tests proving "missing API key" and "invalid API key" do not collapse to the same signal.

Do not add a provider-specific exception table in `classifier.py`. If a provider has a distinct structured code, normalize that code at extraction/config-contract boundaries and feed the canonical signal to the generic classifier.

---

# Retry-After / negotiation governor integration

Plan 150 owns provider negotiation throttling.

Plan 151 must expose the bounded `retry_after_s`/rate-limit effect so the negotiator can:

- stop candidate enumeration;
- advance provider `next_negotiation_allowed_at`;
- preserve the current wire preference;
- let ordinary account failover proceed if budget remains.

Do not convert a 429 into a negative wire-cache entry.

---

# Error returned to client

When multiple pre-handoff attempts occur, preserve useful final failure without leaking upstream internals.

Suggested priority for final rendering:

1. locally determined client/capability error if the original request cannot be represented anywhere;
2. explicit invalid credential if all eligible accounts are genuinely invalid;
3. rate/quota exhaustion if that is the binding final state;
4. no compatible/accepted wire profile if all configured profiles receive deterministic rejection;
5. existing service-unavailable/upstream error for ordinary provider failures.

Do not return "no account available" merely because every account was falsely poisoned by the same request-shape failure.

---

# Expected code surfaces

Likely files:

- `src/eggpool/failure/signal.py`;
- `src/eggpool/failure/signal_extract.py`;
- `src/eggpool/failure/effects.py`;
- `src/eggpool/failure/classifier.py`;
- `src/eggpool/failure/applier.py`;
- `src/eggpool/failure/observation.py` if transport-phase evidence needs extension;
- `src/eggpool/health/health_manager.py` only for confirmed-auth semantics/compatibility migration;
- `src/eggpool/request/coordinator.py` / attempt loop;
- Plan 150 resolver integration;
- focused failure/retry tests.

Do not add a second failure classifier in the wire package.

---

# Required regression tests

## 401 isolation

1. `401 + "Missing API key"` with a configured key:
   - account is not marked authentication_failed;
   - no circuit penalty;
   - if classified as wire-auth mismatch and alternate profile exists, same-account wire transition may occur;
   - sibling model remains routable.

2. `401 + explicit "Invalid API key"`:
   - only selected account becomes authentication_failed;
   - another account may be tried with the same wire profile;
   - wire preference is unchanged.

3. bare 401:
   - no durable account mutation;
   - no blind other-account cascade.

## Surface vs model 404

- endpoint/path 404 -> wire rejection, same-account alternate;
- structured model-not-supported 404/401 -> existing model absence handling, not wire rejection;
- no permanent account disable in either case.

## 405

- safe alternate-surface transition before handoff;
- candidate gets rejection cooldown.

## 400/422

- explicit whole-wire schema mismatch -> alternate surface;
- unsupported `reasoning_effort`/thinking control -> request capability handling, not surface invalidation;
- generic validation error -> no negotiation.

## 429

- same wire on another account if retry allowed;
- no alternate surface;
- Retry-After propagated to Plan 150 governor.

## 5xx/transport

- no surface transition;
- preserve existing model/account failure isolation;
- budget remains globally bounded.

## Attempt budget

Construct mixed sequence, for example:

```text
A/Responses -> safe surface rejection
A/Chat      -> confirmed credential invalid
B/Chat      -> 429
C/Chat      -> success
```

With total budget four, exactly four upstream submissions occur. With lower configured budget, the sequence stops at the bound.

## Recovery

After any surface/schema failure, issue a valid sibling-model request and require ordinary routing to work without restart/database reset.

---

# Acceptance criteria

- [ ] Bare HTTP 401 no longer implies durable credential failure.
- [ ] `CREDENTIAL_INVALID` and `WIRE_AUTH_MISMATCH` are distinguishable canonical signals.
- [ ] `WIRE_SURFACE_UNSUPPORTED` and `WIRE_SCHEMA_MISMATCH` are distinct from model absence and unsupported optional controls.
- [ ] Failure effects explicitly choose same-account alternate-wire vs other-account retry.
- [ ] Surface mismatch does not penalize account health/circuit state.
- [ ] Confirmed credential invalidity disables only the selected account and preserves wire knowledge.
- [ ] 429/quota preserves wire knowledge and does not enumerate surfaces.
- [ ] Generic 400/422 does not cause endpoint roulette.
- [ ] 404 model-absence evidence takes precedence over generic path/surface handling.
- [ ] 5xx/transport/midstream failures do not trigger alternate wire profiles.
- [ ] No alternate-surface request occurs after ambiguous possible inference start.
- [ ] All actual upstream submissions share one request attempt budget derived from existing retry configuration.
- [ ] The coordinator contains no nested accounts×surfaces retry product.
- [ ] Current account/model 5xx isolation behavior remains intact.
- [ ] A bad request/surface cannot poison later unrelated requests.

---

# Rejection conditions

Reject implementation if it:

- keeps `status == 401 -> disable_auth` as a fallback;
- retries the same malformed/wrong-surface request across all accounts before trying the appropriate surface;
- treats any 400/422 unknown field as proof the endpoint is wrong;
- changes wire preference on rate limits/5xx/timeouts;
- surface-negotiates after downstream start or ambiguous request transmission;
- adds provider-name branches to the pure classifier;
- adds a second independent retry counter;
- clears all historical auth failures indiscriminately on startup;
- makes surface failures contribute to account circuit-breaker penalties.

---

# Verification

Run focused failure classifier/applier/coordinator tests plus the normal project gate. The P0 401 regression cases should run in smoke-level or similarly small high-value coverage if the repository has an appropriate existing suite; do not create a broad new CI job.

Live verification of real OpenCode Go 401/surface behavior belongs to Plan 153.

---

# Handoff

1. Land the narrow ambiguous-401 poison-prevention correction first if current main is operationally affected.
2. Add canonical signals/effects and strict decision matrix.
3. Extend coordinator retry destination/attempt budget.
4. Integrate Plan 150 wire state mutations only through canonical effects.
5. Add focused 401/404/405/400/429/5xx/attempt-budget tests.
6. Verify sibling requests remain routable after each failure class.
7. Run normal gate and record implementation SHA/results.
