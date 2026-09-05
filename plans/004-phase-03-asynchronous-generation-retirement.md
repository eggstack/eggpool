# Phase 3 — Asynchronous Runtime-Generation Retirement

Date: 2026-07-19
Status: complete (2026-09-05)
Roadmap: `plans/001-reload-correctness-performance-roadmap.md`
Prerequisites: Phases 1–2.

## Objective

Make runtime-generation publication bounded and independent of old-generation drainage. A successful rehash must install the candidate generation and return promptly even when long-lived streams still hold leases on the previous generation.

The runtime manager must own, track, diagnose, and shut down retirement tasks. Old resources remain valid until leases drain or the configured retirement deadline is reached, then close exactly once.

## Current risk addressed

The current installation path swaps the active slot and then awaits retirement. Retirement can wait for active leases for up to the configured drain timeout, so the control command and reload admission slot can remain occupied long after the new generation is active. This creates unnecessary control-plane latency and can serialize later operational work behind long-lived streams.

## Non-goals

- Do not change the transactional ordering of database and process-owned mutations; Phase 6 addresses that.
- Do not terminate healthy streams immediately after publication.
- Do not silently ignore retirement errors.
- Do not create untracked fire-and-forget tasks.
- Do not change provider request timeout semantics.

## Required lifecycle model

Each generation slot should have explicit state:

- `active`: accepts new leases;
- `retiring`: accepts no new leases, may have active leases;
- `closing`: close sequence in progress;
- `closed`: all owned resources closed;
- `failed_close`: terminal close error recorded, no new leases.

The active slot and retiring slots must be managed under one runtime-manager ownership boundary.

## Publication behavior

Refactor candidate installation into a bounded sequence:

1. Validate candidate and expected active generation.
2. Acquire the runtime-manager state lock.
3. Mark the old slot non-accepting.
4. Install the candidate as active.
5. Register the old slot in the retiring collection.
6. Create and register one retirement task for the old slot.
7. Release the state lock.
8. Return publication metadata immediately.

No network close, lease-drain wait, sleep, or long-running cleanup may occur while holding the state lock.

If there is no old generation, no retirement task is needed and `retirement_pending` is false.

## Retirement task ownership

The runtime manager must maintain a task registry keyed by generation ID. Each task must:

- wait for active lease count to reach zero or the drain deadline;
- observe shutdown acceleration policy;
- transition slot state under the manager lock;
- close generation-owned resources exactly once;
- consume and record all exceptions;
- remove itself from the registry in `finally`;
- notify any waiters or diagnostics subscribers.

Use a done callback only for minimal bookkeeping if necessary. The coroutine itself should perform structured finalization.

## Deadline behavior

Define and document what happens when the drain deadline expires:

- stop waiting for leases;
- mark the generation forced-closing;
- close resources using the existing bounded close policy;
- record active lease count at deadline;
- log a structured warning;
- ensure late lease releases remain safe and idempotent.

A forced close may cause active streams to fail; that outcome is preferable to leaking a generation indefinitely, but it must be visible in diagnostics.

## Lease semantics

Audit lease acquisition and release:

- active generation accepts leases until atomically marked retiring;
- retiring generations never accept new leases;
- a lease object retains a strong reference to its slot/generation;
- release is idempotent or guarded against double release;
- release signals the retirement waiter when the count reaches zero;
- cancellation and stream disconnect release in `finally`.

Avoid polling lease counts with short sleeps. Prefer an event or condition signaled on release.

## Shutdown semantics

`RuntimeManager.shutdown()` must:

1. mark the manager as shutting down and reject new leases/publications;
2. mark the active slot retiring;
3. schedule or join retirement for the active slot;
4. await all registered retirement tasks within a bounded shutdown deadline;
5. force close remaining generations if necessary;
6. leave no EggPool-owned retirement tasks pending.

Shutdown must be idempotent. Repeated calls should not double close generations.

## Diagnostics

Expose a stable retirement snapshot containing:

- active generation ID;
- retiring generation IDs;
- state per generation;
- active lease count;
- retirement start time;
- drain deadline;
- whether close was forced;
- close start/completion time;
- last close error class and bounded message;
- number of tracked retirement tasks.

The reload result should derive `retirement_pending` from actual runtime-manager state, not infer it from generic reload success.

## Tests

### Prompt reload completion

Hold a lease on generation A. Publish generation B. Assert:

- B becomes active immediately;
- install returns without releasing A’s lease;
- A appears as retiring;
- new leases resolve to B;
- `retirement_pending` is true.

### Natural drainage

Release A’s final lease and assert:

- A closes exactly once;
- its retirement task completes;
- A disappears from retiring state;
- B remains active.

### Deadline force close

Hold a lease past a short test deadline using a barrier. Assert forced-close diagnostics and exactly-once cleanup without real-time long waits.

### Multiple generations

Publish B while A retires, then C while B has leases. Assert multiple retirement tasks coexist safely and each generation closes independently.

### Shutdown

Cover shutdown with:

- no retiring generations;
- one naturally draining generation;
- multiple generations;
- close exception;
- repeated shutdown calls;
- cancellation of the caller waiting for shutdown.

### Task hygiene

After each test, assert no runtime retirement tasks remain pending and all instrumented resources are closed exactly once.

## Implementation sequence

1. Add explicit slot state and retirement diagnostics types.
2. Add a manager-owned retirement task registry.
3. Split publication from drainage.
4. Replace lease-count polling with event/condition signaling where practical.
5. Implement deadline and forced-close policy.
6. Update shutdown to join/force retirement tasks.
7. Update reload result derivation.
8. Add multi-generation and task-hygiene tests.
9. Run streaming integration tests and full suite.

## Acceptance criteria

- Candidate publication performs no lease-drain wait.
- Reload completion is not bounded by the old generation’s drain timeout.
- Long-lived streams may continue on the old generation until completion or deadline.
- New requests immediately use the new generation.
- Every retiring generation has exactly one tracked retirement task.
- Generation resources close exactly once.
- Retirement task exceptions are consumed, logged, and visible in diagnostics.
- Shutdown leaves no retirement tasks or unclosed generations.
- Tests require no multi-minute timeout and do not use probabilistic sleeps.

## Handoff evidence

Record focused test commands, before/after rehash latency with a held stream, retirement diagnostic examples, forced-close behavior, and task/resource counts after shutdown.

## Closure evidence

Implemented and verified on implementation commit
`d7b3cd7fa9965ba0d10dff913473e73c12a8505c`.

The runtime manager now publishes the candidate and records the prior slot in
the retiring collection with exactly one tracked retirement task under the
manager lock. Lease release uses the slot drain event rather than polling;
retirement tasks consume unexpected failures and retain bounded
`failed_close` diagnostics; close attempts are idempotent and preserve the
existing resource order; and shutdown wakes/joins all tracked retirements,
including held-lease generations. Reload-result retirement status uses the
manager's public pending-retirement predicate, which includes retained
failed-close slots.

Focused verification passed:

```text
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
uv run pytest tests/smoke/ -q --tb=short --maxfail=1

uv run pytest \
  tests/unit/test_runtime_generation_retirement.py \
  tests/unit/test_runtime_manager.py \
  tests/unit/test_reload_manager.py \
  tests/unit/test_reload_diagnostics_matrix.py \
  tests/unit/test_generation_finalization_ownership.py \
  tests/integration/reload/test_reload_retirement.py \
  tests/integration/reload/test_lease_condition.py \
  tests/integration/reload/test_shutdown_adoption.py \
  tests/integration/reload/test_shutdown_adoption_final.py \
  tests/integration/test_rehash_streaming_swap.py \
  -q --tb=short --maxfail=1
34 focused ownership/retirement regressions passed; the broader listed
retirement/reload suites also passed.
```

The focused coverage proves prompt held-lease publication (`<1s`), immediate
active-generation replacement, natural drainage, deadline force-close, late
idempotent release, concurrent multi-generation retirement, exact close
counts, consumed/diagnosable retirement-task failure, and shutdown task
hygiene. The retirement tests no longer rely on scheduler sleeps; they join
the tracked task or assert manager state registered at publication.

The before/after publication comparison is explicit in history: baseline
implementation `f0005cb9` awaited `begin_retirement()` from
`install_candidate()`; the closure implementation removes that drain wait and
the held-lease integration assertion requires completion under one second.

The final repository-wide run reached `7,967 passed, 45 skipped` and stopped
at the unrelated pre-existing failure
`tests/unit/test_wire_ir.py::test_anthropic_request_normalizes_system_and_tool_blocks`:
the current wire renderer returns a text-block list where that test expects a
string. C004 changed no wire-IR source or test files; the failure is therefore
outside this closure's scope and is recorded rather than silently attributed
to retirement.

## Dependency review

Phase 4 (`plans/005-phase-04-candidate-resource-ownership.md`) is the direct
successor. Its status is already `implemented`, so completing C004 removes its
remaining Phase 3 prerequisite without requiring a status transition. Phases
5–7 are already `complete`/`implemented`; Phases 8–12 are handoff plans with
no explicit blocked status. No future plan status required changing.
