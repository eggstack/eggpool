# C005 — Failure Effects, Retry Budget, and Failover

Status: complete; see [closure record](../../closure/coordinator/005-status.md)

Implementation commit: `97a48464b775514f90d36d021607c091881a36d3`

Source roadmap: `migration-rs/subsystems/coordinator-roadmap.md`

Primary class: invariant/capability

Hard dependency: C004.

## Objective

Port the canonical decision engine that turns transport/HTTP/provider evidence into failure effects and a bounded next action: terminal client outcome, retry another account, retry another wire candidate, wait/stop for rate limit, or exhaust the request. Centralize retry legality here; no lower layer may replay requests.

## Python oracle

Use C001 plus `retry/classification.py`, `failure/*`, health/backoff/quarantine behavior, coordinator retry loops, `wire/resolver.py`, stream/response-start evidence, and M5 D005/D006 state interfaces.

## Decision inputs

Use one typed `FailureObservation`/equivalent containing source, status, transport error class, provider/account/model/upstream-model, client/upstream protocol/wire, bounded response signal, Retry-After, response-start fact, stream terminal evidence where applicable, and attempt number. Do not pass arbitrary provider bodies into policy.

## Required behavior

Preserve category/effect semantics for bad request, auth failure, quota/rate limit, temporary/transient transport/server errors, model-unavailable/model-specific 404, endpoint/wire mismatch, and fatal/nonretryable outcomes.

Apply M5-owned effects through explicit interfaces: durable backoff, circuit/health transition, model quarantine/withdrawal, probe convergence, account runtime state, and quota effects. Effects must be idempotent per attempt identity and may not be applied twice by both retry code and finalization.

Retry action must distinguish account failover from alternate-wire negotiation. A deterministic wire rejection is not an account health penalty unless the frozen failure contract says so. Provider rate limiting may stop wire enumeration and delay negotiation. Auth/account failure must never be “fixed” by switching to direct transport or suppressing proxy policy.

## Retry budget

Freeze and implement the Python maximum-attempt semantics (currently default pre-body budget is three unless config/oracle says otherwise). Bound total attempts, repeated account selection, repeated wire candidates, and cycles. Each replacement attempt requires the previous attempt to become independently terminal/cleanup-owned before accepting new durable attempt ownership.

No transparent retry is legal after downstream handoff. If response-start is true, return a terminal/midstream action even if the underlying category would otherwise be retryable.

## Retry-After

Support numeric seconds and HTTP-date forms, reject nonfinite/negative/invalid values, and preserve the existing 1,800-second suppression cap where the failure/backoff contract applies. Use fake clocks in tests.

## Tests

Build a table-driven differential matrix across transport classes, relevant statuses/signals, response-start false/true, attempt budget boundaries, account availability, fixed vs negotiable wire profiles, Retry-After variants, and duplicate effect application. Verify exact next-action class, retry scope, M5 effect deltas, wire rejection/learning calls, and exhaustion response.

Concurrency tests must prove two retrying requests do not bypass M5 claim/fairness state and one attempt's effect token cannot mutate another attempt.

## Dependencies

No retry library or workflow engine. Pure enums/state plus existing M5/C003 interfaces are sufficient.

## Acceptance criteria

C005 closes when all retry/failover actions are bounded and differential, no post-handoff replay is possible, failure effects are exactly-once per attempt, wire/account scopes remain distinct, and an exhausted request has one deterministic terminal decision.

## Closure

Accepted closure: [C005 status record](../../closure/coordinator/005-status.md). C006 is complete in the same implementation sequence.
