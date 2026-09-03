# Plan 154 — Production Wire Single-Flight and Governor Corrective Pass

Date: 2026-09-03
Status: ready
Parent roadmap: `plans/147-dynamic-wire-surface-negotiation-roadmap.md`
Corrects: Plans 150–151 implementation gap
Depends on: Plans 148–153 implementation currently on `main`
Planning baseline: `73912c1c726471f7ad4a2f8829aa156bc240cf41`
Priority: P0 routing resilience / rate-limit safety
Execution target: GPT-5.6 Luna or comparable implementation model

## Purpose

Complete the runtime-negotiation design that is already present in `src/eggpool/wire/resolver.py` but is not yet connected to the actual alternate-surface dispatch path.

Current `main` has:

- a bounded `WireProfileResolver`;
- provider/model negotiation flights;
- leader/follower roles;
- a provider-wide concurrency gate;
- minimum negotiation intervals;
- provider negotiation delay after rate pressure;
- deterministic candidate-rejection cooldowns;
- shared upstream-submission budgeting in `RequestCoordinator`.

However, the production coordinator currently reacts to an `alternate_wire_same_account` failure by recording the rejected profile and immediately re-entering its normal retry loop. It does not call `begin_negotiation()`, join an existing flight, or acquire the provider negotiation gate. Therefore the configured negotiation concurrency/interval settings and single-flight behavior are not governing real alternate-surface submissions.

This pass must connect those pieces without introducing another routing subsystem, another retry loop, background probing, persistence, or new provider-specific branches.

---

# Governing invariants

1. Ordinary known-good inference must never wait on the negotiation gate.
2. Only a canonical deterministic pre-handoff wire rejection may enter negotiation.
3. Negotiation remains scoped to `(provider_id, canonical_model_id)` for the flight decision.
4. Surface rejection remains provider/model-wide; credential invalidity remains account-specific.
5. One provider-wide gate bounds only abnormal alternate-surface discovery work.
6. All upstream submissions, including the leader's alternate-surface submission, consume the existing request submission budget.
7. Followers must not submit the stale surface or independently enumerate alternatives while a leader owns the same negotiation flight.
8. 429/quota/5xx/timeout/reset/midstream failures must never cause additional surface enumeration.
9. Cancellation must not leak flights, consume/release another task's gate permit, or cancel the decision future for unrelated followers.
10. No SQLite migration, background task, provider SDK, or CI expansion is permitted for this pass.

---

# Current implementation gap

The relevant code is already split appropriately:

- `src/eggpool/wire/resolver.py`
  - `WireProfileResolver.begin_negotiation()`
  - `NegotiationHandle`
  - `_ProviderGate`
  - `record_deterministic_rejection()`
  - `record_success()`
  - `delay_provider_negotiation()`
- `src/eggpool/request/coordinator.py`
  - shared upstream-submission loop;
  - `_apply_wire_failure_effect()`;
  - `_retry_same_account` handling;
  - `_resolve_wire_profile()`;
  - `_record_wire_success()`.
- `src/eggpool/failure/classifier.py`
  - canonical `alternate_wire_same_account` authorization.

Do not replace these components. Wire them together.

---

# Phase A — Make negotiation-handle ownership cancellation-safe

Before production integration, correct the handle/gate ownership edge cases.

## A1. Gate acquisition ownership

Current `NegotiationHandle.__aenter__()` associates `_gate` with the handle before `await gate.acquire(...)` completes. If cancellation occurs while waiting, the cancellation path can call `finish()`, which may release a gate permit that the handle never acquired.

Change ownership semantics so release occurs only after successful acquisition.

Acceptable approaches:

- assign the releasable gate only after `acquire()` returns; or
- keep an explicit `_gate_acquired` boolean set only after successful acquisition.

Do not make `_ProviderGate.release()` guess whether the caller owns capacity.

### Acceptance

- cancelling a leader while blocked behind another provider negotiation does not decrement the active count;
- the first leader retains its permit until it actually exits;
- a later leader cannot exceed `max_concurrent_per_provider` because a cancelled waiter released phantom capacity.

## A2. Follower cancellation isolation

A follower awaiting a shared `asyncio.Future` must not cancel the shared flight future when that follower request is cancelled.

Use cancellation shielding or an equivalent ownership pattern for follower waits.

### Acceptance

- cancel one follower waiting on a leader;
- the leader continues;
- another follower still receives the leader decision;
- the shared flight is removed exactly once after the leader finishes;
- no `InvalidStateError`, leaked flight, or cancelled shared decision remains.

## A3. Leader cancellation publication

If a leader is cancelled before it publishes an accepted surface:

- release any permit actually acquired;
- publish a bounded rejected/aborted decision to followers;
- remove the flight;
- propagate `CancelledError` to the cancelled request;
- do not record a candidate as successfully learned.

Do not turn cancellation into provider health failure or another surface retry.

---

# Phase B — Integrate single-flight at the canonical transition point

The integration point must remain the canonical failure-effects decision, not raw status handling.

## Required control flow

For a request whose failed attempt produces:

```text
retry_action = alternate_wire_same_account
wire_effect = reject_candidate
```

perform the following logical flow:

1. apply the current candidate rejection to the resolver;
2. build/reuse the same complete family resolution used by `_resolve_wire_profile()`;
3. call `WireProfileResolver.begin_negotiation(resolution)`;
4. branch on leader/follower/throttled role;
5. keep the same total request submission ceiling.

### Leader

The leader may perform the alternate-surface submission on the same account through the existing coordinator retry loop.

Requirements:

- acquire the provider negotiation gate before the alternate submission;
- do not create a nested request/retry loop;
- carry the active negotiation handle as coordinator-local request state or an equivalently narrow internal field;
- the next `_resolve_wire_profile()` must select the first non-suppressed alternate candidate;
- the existing `_retry_same_account` mechanism may continue to reopen the account;
- if the alternate request succeeds and final client adaptation succeeds, publish `accept(selected_surface)`;
- ordinary success still refreshes runtime preference using `_record_wire_success()`;
- avoid double-recording success if `accept()` already writes the same preference; one canonical helper should own the write.

### Follower

A follower must not independently submit another alternate while the leader is discovering the new profile.

Requirements:

- await the leader's decision using the cancellation-safe follower path;
- do not consume an upstream submission while only waiting;
- when the leader reports `accepted(surface)`, re-resolve the provider/model and continue with the learned surface if the follower still has request budget and remains pre-handoff;
- the follower's eventual model response must still come from its own upstream request; never share another client's generated model response;
- when the leader reports rejected/rate-limited/throttled, do not enumerate surfaces independently in the same request.

The single-flight shares only the wire decision, never model output.

### Throttled

If provider negotiation is currently throttled:

- do not send another discovery candidate merely because this request encountered the same stale surface;
- do not sleep inside dispatch to wait out the throttle;
- return/continue through the existing bounded failure route appropriate to the request;
- do not penalize credential health merely because discovery is throttled.

The implementation may select another already-known routing destination if existing policy permits it, but must not create another surface enumeration path.

---

# Phase C — Make 429 pressure stop the active negotiation flight

A leader can encounter rate pressure on the alternate candidate itself.

When an active negotiation leader's alternate submission receives a canonical 429/rate-limit effect:

- stop surface enumeration immediately;
- call the resolver's rate-pressure completion (`rate_limited(retry_after_s=...)`) or equivalent;
- honor the existing bounded `Retry-After` interpretation;
- do not mark the candidate unsupported;
- do not try a third wire surface as a response to 429;
- preserve normal account rate-limit handling from Plan 151;
- wake followers with a rate-limited decision so they do not independently negotiate.

Do not use 429 as evidence that the chosen wire surface is incorrect.

If the normal request routing policy then chooses another account on the same already-selected wire, that remains normal account failover and still consumes the same request submission budget.

---

# Phase D — Keep the shared submission ceiling authoritative

The coordinator already owns one ceiling of `1 + max_retries_before_stream` upstream submissions. Preserve this exactly.

Examples:

### Example 1 — stale surface, successful alternate

```text
attempt 1: account A /responses -> deterministic surface rejection
attempt 2: account A /chat/completions -> success
```

Two submissions consumed.

### Example 2 — stale surface, alternate gets 429

```text
attempt 1: account A /responses -> deterministic rejection
attempt 2: account A /chat/completions -> 429
```

No third surface is tried as a negotiation action. Any later account retry must still fit within the original ceiling.

### Example 3 — concurrent followers

Leader consumes its own submissions. Followers consume no submission while waiting for the wire decision. Their later model inference is their own normal submission and counts against their own request ceiling.

Do not count `begin_negotiation()`, waiting, cache reads, or resolution as upstream submissions.

---

# Phase E — Remove or collapse dead/duplicated negotiation state

After wiring the governor, inspect for state that became redundant during the phased implementation.

Likely candidates:

- `_wire_negotiating` metadata if it no longer controls candidate expansion;
- duplicate family resolution calls in `_resolve_wire_profile()`;
- duplicate preference writes between negotiation acceptance and ordinary-success learning;
- configuration fields that are currently parsed but only become meaningful after this pass.

Prefer deletion/simplification over adding another state flag.

Do not refactor unrelated coordinator code in this pass.

---

# Focused deterministic tests

Extend existing tests rather than creating a large new framework.

## `tests/unit/test_wire_resolver.py`

Add/strengthen:

1. cancelled waiting leader does not release another leader's gate permit;
2. cancelled follower does not cancel the shared future;
3. multiple followers receive one accepted decision;
4. leader cancellation clears the flight and releases exactly one owned permit;
5. rate-limited leader publishes one result and delays later negotiations;
6. normal resolver selection is unaffected by provider negotiation gate occupancy.

## `tests/integration/test_wire_negotiation_e2e.py`

Add a real coordinator-level concurrency case with an in-process fake upstream:

- seed/prefer stale Responses;
- launch several simultaneous requests for the same provider/model;
- make `/responses` deterministically reject;
- delay `/chat/completions` enough to allow followers to converge behind the leader;
- assert only one leader performs the discovery transition while followers wait for the wire decision;
- after acceptance, followers use the learned Chat surface for their own model requests;
- assert no request exceeds its own shared attempt budget;
- assert account health remains healthy;
- assert subsequent steady-state requests go directly to Chat.

The assertion must count actual upstream path calls, not merely resolver metrics.

## Provider-wide gate test

Use two different model IDs on one synthetic provider and force both into negotiation concurrently.

With `max_concurrent_per_provider = 1`:

- only one alternate discovery submission may be active at once;
- ordinary known-good requests for either model must remain able to dispatch concurrently.

This proves the gate is control-plane/abnormal-path only.

---

# Verification commands

Use the repository's existing environment and commands; do not add CI jobs.

Minimum focused gate:

```bash
uv run pytest tests/unit/test_wire_resolver.py -q
uv run pytest tests/integration/test_wire_negotiation_e2e.py -q
uv run pytest tests/unit/test_failure_effects_table.py -q
uv run pytest tests/unit/test_failure_signal_extraction.py -q
```

Then run the ordinary lean project gate already documented by the repository.

Live provider calls are not required to prove this concurrency mechanism; Plan 155 owns live/cross-surface closure.

---

# Explicit non-goals

Do not add:

- distributed locks;
- Redis;
- SQLite negotiation leases;
- background probe workers;
- scheduled profile refresh;
- response sharing/deduplication across users;
- provider-wide inference serialization;
- adaptive concurrency algorithms;
- a second retry counter;
- general-purpose circuit-breaker replacement;
- new dependencies for synchronization.

The existing asyncio/process-local resolver is enough for EggPool's local/SBC deployment model.

---

# Acceptance criteria

This plan is complete only when all are true:

- [ ] Production alternate-surface dispatch calls the resolver's negotiation admission path.
- [ ] Concurrent stale-profile requests do not independently enumerate the same alternatives.
- [ ] Followers share only a wire decision, never another request's generated output.
- [ ] `max_concurrent_per_provider` actually bounds real alternate-surface discovery submissions.
- [ ] `min_negotiation_interval_s` actually affects real discovery admission.
- [ ] A negotiation 429 stops further surface enumeration and wakes followers with rate pressure.
- [ ] 429 does not suppress/reject a wire candidate as unsupported.
- [ ] All upstream submissions remain bounded by the existing per-request ceiling.
- [ ] Cancelling a blocked leader cannot release capacity it never acquired.
- [ ] Cancelling one follower cannot cancel the leader/shared decision future.
- [ ] Leader cancellation removes the flight and releases owned capacity exactly once.
- [ ] Known-good ordinary requests are never serialized by the negotiation gate.
- [ ] No DB schema, dependency, CI matrix, or background task is added.
- [ ] Focused deterministic tests pass.
- [ ] Ordinary repository gate passes.

---

# Handoff order

1. Fix gate/future cancellation ownership first.
2. Add coordinator-level negotiation admission without changing failure classification.
3. Wire leader success/rejection/rate-pressure completion.
4. Wire follower decision waiting.
5. Prove shared attempt-budget behavior.
6. Add coordinator-level concurrency tests.
7. Remove redundant temporary metadata/state if possible.
8. Run focused and ordinary gates.
9. Record implementation SHA and test evidence in this file or the final corrective closure record from Plan 155.

Do not begin broader provider-surface heuristic changes in this plan; those belong to Plan 155.
