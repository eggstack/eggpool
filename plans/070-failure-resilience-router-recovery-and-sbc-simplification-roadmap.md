# Failure Resilience, Router Recovery, and SBC Simplification Roadmap

Date: 2026-08-04
Status: ready for implementation
Plan: 070
Planning baseline: `e73db213e7e381043cda3cfb8a3dd8109f3f39ca`
Completed predecessor: `plans/058-durable-convergence-exact-update-sbc-hotpath-roadmap.md`

Implementation plans:

- `plans/071-attempt-scoped-failure-classification-and-effects.md`
- `plans/072-upstream-dispatch-retry-and-response-isolation.md`
- `plans/073-bounded-backoff-and-router-self-healing.md`
- `plans/074-restart-safe-runtime-and-database-simplification.md`
- `plans/075-sbc-resource-profile-and-verification-consolidation.md`

## Purpose

Make EggPool resilient to malformed client requests, provider-specific validation behavior, transport failures, unexpected upstream responses, cancellation, local response-adaptation failures, and persistence faults without allowing one request or one account to poison routing for unrelated traffic.

The desired result is not a production-grade distributed control plane. EggPool remains a private, single-host LAN/SBC proxy with one canonical event loop, one SQLite database, a modest account set, and systemd available to restart the process when process-local recovery is less reliable than a clean restart.

The work must ensure that:

- a retryable pre-response upstream failure is routed to a different eligible account;
- a request is never retried after downstream response handoff;
- client validation failures and local EggPool faults do not penalize provider health;
- provider failures affect only the correct durable attempt, account, model, and scope;
- every temporary suppression has a bounded and testable recovery path;
- a bad request or unusual provider response cannot leave an account, circuit probe, reservation, active count, or model route permanently stuck;
- restart and database deletion are not routine recovery mechanisms for request-level faults;
- the maximum nonterminal exponential backoff is 30 minutes rather than 24 hours;
- resource reductions and runtime simplification preserve the routing and accounting contract;
- CI remains the existing small smoke gate.

## Design center

- One process supervisor and one Granian worker.
- One supported event-loop thread.
- SQLite WAL on local storage.
- A small number of provider accounts and concurrent streams.
- Private LAN or localhost exposure, not hostile public multi-tenancy.
- Immediate rerouting between distinct accounts for retryable pre-handoff failures.
- No in-request exponential sleep.
- Bounded in-memory state and bounded persistence hints.
- systemd restart as the final recovery boundary for an indeterminate database connection or internal invariant failure.
- Focused deterministic tests, not fault-campaign infrastructure.

## Confirmed findings

### A. Retry and failure effects are classified by divergent layers

`RetryClassifier` decides whether an HTTP response should reroute, while `classify_failure_effects()` separately decides health, circuit, quarantine, and backoff consequences. Additional coordinator branches still classify errors directly.

The layers do not carry one immutable decision object. Provider response-body evidence can therefore influence retry without reaching shared-state effects. Transport exceptions can also be presented to the effects classifier as generic `upstream_http` observations rather than transport observations.

Consequences include:

- an ambiguous 403/409/422 quota response may reroute but fail to suppress the affected account;
- model-like 404 evidence can be interpreted differently by retry and quarantine paths;
- connection, timeout, and protocol failures can reroute within one request but remain immediately eligible for the next request;
- future changes can silently alter one layer without altering the others.

### B. Failure-effect idempotency is keyed by failure shape rather than attempt identity

The current effects keys are assembled from account/model/provider/protocol/status/error-class values. They do not contain the proxy request ID or durable attempt ID.

Two independent requests producing the same failure shape can therefore collide. The second failure can be treated as already applied, skipping account effects, circuit state, quarantine, or half-open probe release. This is a direct route to process-local stuck state that can appear to require restart.

Changing the key to a unique attempt identity without retiring entries would create unbounded memory. Attempt-scoped effect progress must instead be owned by the retained attempt/finalization lifecycle and retired when that lifecycle converges.

### C. One failure can increment the circuit breaker twice

For generic failure/cooldown effects, `HealthManager.record_failure()` already records a circuit failure. `EffectsApplier` can then independently apply another circuit penalty for the same observation.

A single provider fault can therefore consume two failure counts and open the circuit earlier than the configured policy intends.

### D. Broad exception conversion can misattribute EggPool faults to providers

The upstream execution try blocks cover client lookup, request construction, request transmission, response reading, and portions of local response preparation. Generic exceptions are converted to retryable upstream errors.

A malformed local configuration, invalid header, provider-client construction defect, serialization bug, or unexpected EggPool exception can therefore be rerouted and penalize unrelated accounts as though the provider failed.

Unexpected exceptions must be isolated and rendered safely, but they must retain their origin. Failure isolation does not mean labeling every internal exception as upstream pressure.

### E. Non-streaming success is finalized before all local response work is proven

The non-streaming path performs durable success finalization before response transcoding is complete. A local response-decoding or transcoding exception can therefore occur after the request is recorded as completed, while the client receives a proxy error.

Provider response validation and client-facing adaptation must be completed, or deliberately classified as best-effort pass-through, before committing the final client-visible terminal outcome.

### F. Backoff caps are inconsistent with the deployment model

Quota exhaustion, rate limiting, and model-unavailable policies currently permit a 24-hour cap. Transport and server-error policies already cap at 30 minutes.

For a private aggregation proxy, a 24-hour transient suppression is too sticky and can make a corrected provider appear permanently absent. All nonterminal runtime backoffs must cap at 1,800 seconds, including an excessive `Retry-After`. Authentication failure and authoritative catalog withdrawal remain explicit terminal states with operator or authoritative recovery.

### G. Success and expiry do not have one explicit self-healing contract

The process clears some transient durable rows on success, the health manager clears some in-memory state, and model quarantine has separate recovery methods. These paths must agree on scope:

- account success clears only transient account-wide suppression;
- account/model success clears bounded runtime model quarantine for that exact pair;
- success must not clear authentication failure or an authoritative model withdrawal;
- expired durable rows must never rehydrate suppression;
- malformed or extreme timestamps must not create effectively permanent cooldown.

### H. Active-process stale cleanup can confuse stream age with abandonment

The stale-request sweep uses an age threshold related to upstream read timeout. A healthy long-lived stream can exceed that wall-clock age without being abandoned.

Normal in-process traffic must not be reclaimed solely because its request row is old. Startup crash recovery is the correct default safety net unless an explicit owner-death or liveness fact proves abandonment.

### I. Database safety and recovery machinery exceed the local failure model

Startup integrity checking currently logs some failures rather than making admission fail closed. Transaction context can also be inherited by child tasks, creating a latent commit/child-operation race.

The current in-process recovery architecture contains connection epochs, ambiguous-operation retention, admission state, retry policy, and reconciliation state. Some correctness checks are necessary, but a local systemd service should prefer a clean process restart after an indeterminate SQLite connection or transaction rather than attempting to preserve a partially understood process state indefinitely.

### J. Finalization correctness relies partly on bounded diagnostic history

The finalization supervisor consults a bounded completed-history deque when deduplicating late duplicate terminal submissions. Once a record is evicted, the same durable attempt can begin a new process-local lifecycle. A synthetic completed job returned from history also does not reproduce the original structured result.

Durable attempt identity must remain the permanent idempotency boundary. Diagnostic history must not be correctness state.

### K. The default runtime profile remains heavier than necessary for SBC deployment

The default configuration enables or provisions multiple diagnostic and background facilities, including sampled traces, detailed spans, model-info refresh, writable probes, multiple SQLite connections, and relatively high HTTP connection ceilings.

Disabled features should not instantiate active writers or timer loops. A documented SBC profile should reduce wakeups, writes, sockets, and retained metrics without changing proxy correctness.

### L. CI is already proportionate

The existing CI is one Python 3.11 job running formatting, lint, type checking, and the smoke suite. Release remains manual. No additional matrix, coverage gate, fault campaign, benchmark gate, soak gate, or evidence archive is warranted.

The reduction opportunity is in test duplication and plan-specific regression accumulation, not in removing the current smoke gate.

## Governing decisions

1. **One classification result per failure.** Retry, client outcome, account/model effects, circuit consequence, evidence, and backoff derive from the same immutable classification.
2. **Attempt identity owns idempotency.** Effects apply once per `(proxy_request_id, attempt_id)`, not once per failure shape and not forever in a process-global set.
3. **No retry after handoff.** Header/first-byte failures may reroute before the downstream response begins. Midstream failures finalize and close; they never replay the request.
4. **Distinct-account retries only.** One request does not retry the same account. It moves through eligible accounts up to the existing configured safety ceiling.
5. **No in-request sleeping.** Backoff affects eligibility for future requests; retries within the current request move immediately to another account.
6. **Provider effects require provider evidence.** Client errors, capability rejection, context-limit rejection, cancellation, database failure, and local EggPool defects do not penalize provider health.
7. **All transient suppression is bounded.** The maximum nonterminal backoff is 30 minutes. Explicit terminal states require an operator action or authoritative reappearance.
8. **Durable backoff is a restart hint.** Current-process in-memory state is authoritative. Backoff persistence failure cannot fail the client response, and stale durable rows cannot suppress beyond their bounded expiry.
9. **Process restart is a valid recovery primitive.** After an indeterminate database connection or internal invariant breach, fail readiness and exit cleanly rather than add another recovery state machine.
10. **Durable identity, not history, is the idempotency boundary.** Bounded diagnostic history may be discarded without changing correctness.
11. **Disabled means dormant.** Disabled diagnostic or enrichment features do not create writers, queues, or periodic wakeups.
12. **No distributed-system expansion.** No broker, external queue, workflow engine, consensus layer, multi-node lease, or generalized cross-loop runtime.
13. **No verification expansion.** Add focused capability regressions to existing files and keep the single smoke CI gate.

## Phase sequence

### Plan 071 — Attempt-Scoped Failure Classification and Effects

Create one typed failure decision consumed by retry, finalization, health, circuit, quarantine, and durable backoff. Replace failure-shape idempotency with attempt-scoped ownership, carry response-body and transport evidence through the decision, and ensure one circuit transition per failed attempt.

### Plan 072 — Upstream Dispatch, Retry, and Response Isolation

Narrow exception boundaries, distinguish local/internal faults from provider failures, enforce distinct-account pre-handoff rerouting, prove stream handoff semantics, finish non-streaming response adaptation before terminal success, and add a final ASGI safety boundary that cannot crash the worker on ordinary request exceptions.

### Plan 073 — Bounded Backoff and Router Self-Healing

Cap every nonterminal backoff and `Retry-After` at 30 minutes, define success/expiry/reset semantics, guarantee probe and cooldown recovery, harden persisted backoff hydration, and prove that repeated failures and subsequent success cannot leave routing permanently suppressed.

### Plan 074 — Restart-Safe Runtime and Database Simplification

Remove age-only active stream reclamation, make startup integrity checks fail closed, enforce transaction task ownership, classify SQLite lock failures correctly, make durable attempt identity the finalization idempotency boundary, and reduce in-process database/finalization recovery machinery in favor of bounded shutdown plus startup reconciliation.

### Plan 075 — SBC Resource Profile and Verification Consolidation

Ship a simple SBC-oriented configuration example, avoid constructing disabled components, reduce periodic wakeups and connection ceilings, make optional proxy support lazy, fix dispatch-span configuration precedence, add conservative dependency bounds, and consolidate tests/plans without weakening the existing smoke gate.

## Dependency order

```text
071 unified failure decision
        |
        +--> 072 dispatch/retry isolation --> 073 bounded recovery
        |                                      |
        +--------------------------------------+--> 074 restart-safe simplification
                                                       |
                                                       +--> 075 SBC/resource consolidation
```

Plan 071 establishes the semantic contract used by Plans 072 and 073. Plan 072 must land before Plan 073 closes because recovery behavior depends on accurate source and handoff facts. Plan 074 consumes the corrected attempt identity and failure boundaries before deleting overlapping recovery machinery. Plan 075 follows functional simplification so it does not optimize components that are about to be removed.

## Cross-phase invariants

- Every selected upstream dispatch has a non-empty durable request/reservation identity and a positive attempt ID.
- One failed attempt produces one immutable failure decision.
- The same attempt can replay cleanup without replaying shared-state effects.
- Two separate attempts with identical status/error shapes each apply their own effects.
- A provider circuit receives no more than one failure transition per attempt.
- A request-local error releases any acquired probe and leaves account/model health unchanged.
- A retryable pre-handoff provider failure excludes the failed account and may select another eligible account immediately.
- A request never reuses the same account in its retry loop.
- A response is never replayed after downstream handoff.
- Midstream failure is terminal for that client request but cannot crash the worker.
- Local serialization, transcoding, database, and internal exceptions do not create provider cooldown or quarantine.
- Runtime quota, active-count, usage, health, account-state, and probe ownership converge exactly once.
- A temporary account or account/model suppression expires in at most 1,800 seconds.
- Upstream `Retry-After` cannot extend a nonterminal local suppression beyond 1,800 seconds.
- Authentication failure remains explicit and operator-resettable.
- Authoritative catalog withdrawal remains explicit and clears only through authoritative reappearance or operator action.
- A successful request clears only matching transient state and cannot erase unrelated terminal state.
- Old, malformed, or expired durable backoff rows cannot create permanent suppression at startup.
- Normal active streams are never reclaimed only because their wall-clock lifetime exceeds a read-timeout value.
- Database integrity failure prevents readiness and dispatch.
- An indeterminate database transaction never results in continued admission with ambiguous process state.
- Bounded finalization history can be cleared without changing durable idempotency.
- Disabled diagnostics create no active writer or timer.
- CI remains one smoke-oriented job.

## Verification budget

- Extend existing capability-based unit and smoke files; do not create plan-numbered test suites.
- Prefer table-driven representative cases over a full status/exception Cartesian matrix.
- Use one real SQLite test for each durable ownership or hydration invariant that mocks could hide.
- Use simulated HTTPX transports; no live provider credentials.
- No sleeps except deterministic fake-clock advancement.
- No repeated random fault campaign, soak loop, chaos framework, or process farm.
- No benchmark or latency percentage as a merge gate.
- One short manual SBC measurement may record idle RSS, idle wakeups/writes, and a small mixed request run; it is diagnostic, not retained evidence.
- Run focused formatting, lint, type, and affected tests first, then the existing smoke suite.
- Do not require the unfiltered full test suite for each narrow phase.

## Roadmap acceptance criteria

- [ ] Retry and shared-state effects consume one typed failure decision.
- [ ] Provider response-body signals and transport source survive classification into effects.
- [ ] Effect idempotency uses actual attempt identity and retires with attempt ownership.
- [ ] Separate identical failures do not collide, while replay of one attempt remains idempotent.
- [ ] One failed attempt cannot increment the circuit breaker twice.
- [ ] Client validation, capability, context-limit, cancellation, database, and local internal failures produce no provider penalty.
- [ ] Retryable pre-handoff failures route only to distinct eligible accounts and respect the configured safety ceiling.
- [ ] No request is retried after downstream handoff.
- [ ] Non-streaming terminal success is not committed before required client-facing response adaptation succeeds.
- [ ] Unexpected ordinary request exceptions return a stable bounded error and do not terminate proxy service.
- [ ] All nonterminal backoff policies, including `Retry-After`, cap at 1,800 seconds.
- [ ] Success, expiry, authoritative reappearance, and operator reset provide explicit recovery exits for every suppressed state.
- [ ] Persisted backoff cannot resurrect expired or malformed suppression after restart.
- [ ] Healthy long streams cannot be finalized by an age-only active-process sweep.
- [ ] Startup database integrity failure prevents readiness.
- [ ] Database transactions cannot be inherited by an unsynchronized child task.
- [ ] Indeterminate SQLite state fails readiness/process admission rather than remaining half-recovered.
- [ ] Durable attempt identity remains idempotent after diagnostic history eviction.
- [ ] Finalization and cleanup no longer depend on a synthetic completed-history result.
- [ ] An SBC configuration example materially reduces writes, wakeups, sockets, and enrichment work without changing routing correctness.
- [ ] Disabled writers and enrichers are not instantiated.
- [ ] The existing single CI smoke job remains the complete mandatory CI surface.
- [ ] No broker, workflow engine, distributed lease, generalized cross-loop queue, matrix, coverage gate, soak gate, benchmark gate, or evidence system is introduced.

## Rejection conditions

Do not close this roadmap if:

- retry classification and health/backoff classification can still disagree for the same response;
- attempt idempotency is still keyed only by account/model/status/error shape;
- attempt keys accumulate indefinitely in process-global memory;
- one transport or 5xx failure can apply two circuit penalties;
- a local EggPool exception is converted into provider suppression;
- an already-started downstream response can be retried;
- a failed account can be selected twice within one request;
- transient backoff can exceed 30 minutes;
- a runtime model error can create an indefinite withdrawal without authoritative evidence;
- a successful matching request cannot restore an account/model after bounded failure;
- an expired durable row can re-disable an account after restart;
- a healthy long stream can be reclaimed based only on request age;
- integrity-check failure logs and continues accepting traffic;
- diagnostic history remains a correctness requirement;
- simplification replaces current machinery with another framework of equal or greater complexity;
- CI or release automation grows beyond the current scope.

## Definition of done

This roadmap is complete when EggPool has one attempt-scoped failure contract, correctly reroutes retryable pre-handoff provider failures between distinct accounts, isolates client and local faults from provider state, caps transient suppression at 30 minutes, self-recovers every temporary routing state, cannot strand probes or account eligibility through duplicate failure shapes, treats startup/database ambiguity fail closed, removes recovery complexity that is unnecessary for a single-host service, provides a genuinely low-footprint SBC profile, and retains only the existing focused smoke CI gate.