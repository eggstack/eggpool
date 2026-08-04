# Plan 069 — Final Runtime Result and Roadmap Closure

Date: 2026-08-04
Status: completed
Parent roadmap: `plans/058-durable-convergence-exact-update-sbc-hotpath-roadmap.md`
Corrective predecessor: `plans/068-partial-runtime-result-truthfulness-polish.md`
Planning baseline: `1d3b5fe46c339727881a6b3b7a39462417ed48ea`

## Purpose

Close the last two verified residuals in the Plan 058 terminal-finalization line without changing runtime ownership, durable convergence, component ordering, retry scheduling, response handoff, supervisor capacity, metrics, or recovery architecture.

Plan 068 correctly added a single lease-to-result projection helper and a real middle-component failure regression. Completed active-count, quota-reservation, usage, and health/probe markers now remain visible when a later component fails, and retry resumes without replaying completed work.

Two narrow closure defects remain:

1. after a runtime attempt fails, `FinalizationResult.retryable` and `FinalizationResult.detail` retain the retry-pending state even after the retained lease later converges successfully; and
2. Roadmap 058 describes Plan 068 in its phase prose but does not include Plan 068 in the top-level implementation-plan registry or extend the dependency sequence through the polish phase.

This pass must correct those facts and then close the planning metadata. It must not reopen the surrounding architecture or add additional verification machinery.

## Confirmed residual defects

### 1. Successful retry retains stale failure metadata

`RequestFinalizationJob._execute_runtime_release()` currently behaves as follows:

1. a middle runtime component fails;
2. `_refresh_runtime_result_from_lease()` preserves the successfully completed component fields;
3. the exception path sets:
   - `runtime_cleanup_complete=False`;
   - `retryable=True`;
   - `detail="runtime cleanup incomplete"`;
4. the supervisor retries the same retained job;
5. the remaining runtime components converge and `runtime_lease.released` becomes true;
6. `_refresh_runtime_result_from_lease()` updates runtime component fields and `runtime_cleanup_complete=True`, but does not clear the earlier retry metadata.

The completed job can therefore expose the contradictory state:

```text
runtime_cleanup_complete = true
retryable = true
detail = "runtime cleanup incomplete"
```

The runtime work itself is complete and exactly-once. The defect is the final structured result projection.

### 2. Roadmap registration is incomplete

Roadmap 058 contains a `Plan 068 — Partial Runtime Result Truthfulness Polish` phase and mentions Plan 068 in its definition of done, but:

- the top-level `Implementation plans` list stops at Plan 067; and
- the dependency diagram terminates at Plan 067.

This conflicts with Plan 068’s checked claim that the closure metadata is coherent.

## Scope

Primary runtime file:

- `src/eggpool/request/finalization_job.py`

Focused test file:

- `tests/unit/test_request_finalization_state_machine.py`

Planning metadata:

- `plans/058-durable-convergence-exact-update-sbc-hotpath-roadmap.md`
- `plans/068-partial-runtime-result-truthfulness-polish.md`
- this plan

Only touch Plan 066 or Plan 067 if a direct reference is demonstrably false after this pass. Do not edit them ceremonially.

## Explicitly out of scope

- changing `AttemptRuntimeLease` acquisition or component markers;
- changing runtime convergence ordering;
- changing durable request, attempt, reservation, usage, or cost transactions;
- changing supervisor scheduling, retry age, backoff, capacity, or exhaustion;
- changing response handoff or streaming behavior;
- changing health, quota, router, account registry, or stale-repair behavior;
- adding result fields or a second result type;
- adding another helper abstraction beyond the existing lease-to-result projection helper;
- adding a queue, migration, worker, dependency, endpoint, or metrics subsystem;
- adding CI jobs, matrices, coverage gates, fault campaigns, soak tests, benchmarks, or evidence bundles;
- running or requiring the unrelated full test suite as a closure gate.

## Governing decisions

1. `AttemptRuntimeLease` remains the source of truth for runtime completion.
2. `FinalizationResult` remains a projection of durable facts plus the current lease state.
3. Retry metadata describes the current state, not historical failure. It must be cleared when the retained runtime lease has fully converged.
4. Durable terminal, transition, reservation, and component result fields must remain unchanged while transient retry metadata is normalized.
5. The existing real middle-component regression is the correct test seam. Extend it rather than adding another fixture or test module.
6. Roadmap registration must describe the actual sequence through Plan 069, but historical plan prose should not be rewritten unnecessarily.
7. Verification remains the existing focused finalization checks plus the repository smoke gate.

## Phase A — Normalize final retry metadata after convergence

### Required changes

1. Update the existing lease-to-result projection path so a successfully released runtime lease produces a coherent completed result.
2. When `runtime_lease.released` is true, ensure the resulting `FinalizationResult` has:
   - `runtime_cleanup_complete=True`;
   - `retryable=False`; and
   - no stale runtime-incomplete detail (`detail=""` unless another currently valid terminal detail is explicitly owned by the result contract).
3. Preserve the existing exception behavior while runtime work remains incomplete:
   - completed component fields remain visible;
   - `runtime_cleanup_complete=False`;
   - `retryable=True`;
   - `detail="runtime cleanup incomplete"` or the repository’s established bounded equivalent.
4. Prefer keeping this normalization in `_refresh_runtime_result_from_lease()` or immediately adjacent to its successful call site. Do not create a separate retry-state machine.
5. Do not clear durable retry/conflict information unless the field is specifically the transient runtime-cleanup marker introduced by the failed runtime attempt.
6. Inspect `retry_queued` only if it is currently set by this runtime path. Clear it on terminal success only when the existing contract defines it as present-tense queue state; do not expand this pass merely to rename or redesign that field.
7. Preserve every existing component projection:
   - live quota reservation removal;
   - router active-count decrement;
   - health/probe convergence; and
   - overall lease release.
8. A direct first-attempt success must continue to produce the same coherent non-retryable result.

### Acceptance criteria

- After the first injected middle-component failure, the result remains retryable and incomplete while completed component fields remain true.
- After the retry completes the lease, `runtime_cleanup_complete` is true, `retryable` is false, and stale runtime-incomplete detail is absent.
- Durable terminal and reservation-convergence fields survive both projections.
- Completed active-count, quota, usage, health/probe, and account-runtime work remains exactly-once.
- No new ownership or retry state is introduced.

## Phase B — Extend the existing regression

### Required test changes

Extend `test_runtime_failure_resumes_without_repeating_durable_finalization` in `tests/unit/test_request_finalization_state_machine.py`.

Retain its existing real convergence sequence:

- durable finalization returns one converged result;
- active-count decrement succeeds once;
- quota reservation removal succeeds once;
- usage recording succeeds once;
- health raises on the first runtime attempt and succeeds on retry;
- account runtime succeeds once after health;
- completed components are not replayed.

Add explicit final assertions after the successful retry:

- `job.result.runtime_cleanup_complete is True`;
- `job.result.retryable is False`;
- `job.result.detail` does not report incomplete runtime cleanup;
- durable and reservation fields remain true;
- component counters remain exactly as expected.

Also retain the first-failure assertions proving:

- `job.progress == RUNTIME_RELEASE_PENDING`;
- `runtime_cleanup_complete is False`;
- `retryable is True`;
- active-count and quota fields are already true;
- health completion remains false;
- durable finalization and completed runtime components have each run once.

### Test budget

- Modify one existing test.
- Add no new test file.
- Add no new fixture unless a few local assertions cannot express the behavior.
- No sleeps, live providers, HTTP, subprocesses, or repeated fault loops.

### Acceptance criteria

- The extended assertion fails against `1d3b5fe46c339727881a6b3b7a39462417ed48ea` because retry metadata remains stale.
- It passes once successful convergence clears transient retry metadata.
- It continues to prove non-replay of durable and completed runtime work.
- It does not broaden into unrelated supervisor or stream testing.

## Phase C — Reconcile closure metadata

### Roadmap 058

1. Change status from `completed` to a narrow pending state while Plan 069 is unimplemented, such as `final closure pending`.
2. Add both of the missing entries to the top-level implementation-plan registry:
   - `plans/068-partial-runtime-result-truthfulness-polish.md`
   - `plans/069-final-runtime-result-and-roadmap-closure.md`
3. Add a short Plan 069 phase describing final retry-metadata normalization and registry closure.
4. Extend the dependency sequence through:

```text
... --> 066 runtime ownership --> 067 semantic closure --> 068 result truthfulness --> 069 final metadata closure
```

5. Reopen only the acceptance criterion governing truthful completed runtime results and coherent focused regression evidence.
6. Return the roadmap to `completed` only after the focused test and smoke gate pass.
7. Do not reopen unrelated dispatch, recovery, stale accounting, update, quota, database, capacity, handoff, or metrics criteria.

### Plan 068

1. Change status to a narrow follow-up-pending state until Plan 069 lands.
2. Add Plan 069 as the corrective successor.
3. Preserve Plan 068’s verified partial-progress accomplishments.
4. Reopen only the criteria that imply coherent final result state and fully coherent closure metadata.
5. Add a short post-review note describing the stale `retryable/detail` residual without rewriting the historical implementation closure.
6. Mark Plan 068 completed again only after Plan 069’s final assertions pass.

### Plan 069 closure

After implementation and verification:

1. mark this plan `completed`;
2. check each acceptance criterion;
3. record exact focused and smoke commands actually run;
4. update Roadmap 058 and Plan 068 to completed;
5. keep the interrupted unrelated full-suite observation historical rather than turning it into a new closure requirement.

## Verification

Run the smallest affected checks first:

```bash
uv run ruff format src/eggpool/request/finalization_job.py tests/unit/test_request_finalization_state_machine.py
uv run ruff check src/eggpool/request/finalization_job.py tests/unit/test_request_finalization_state_machine.py
uv run pytest tests/unit/test_request_finalization_state_machine.py -q --tb=short --maxfail=1
uv run pyright src/eggpool/request/finalization_job.py
```

Then run the existing repository gate:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
```

Optionally rerun the already-used focused grouping if convenient:

```bash
uv run pytest tests/unit/test_request_coordinator_cleanup.py tests/unit/test_request_finalization_state_machine.py tests/unit/test_request_finalizer.py -q --tb=short --maxfail=1
```

Do not require an unfiltered full-suite run for this patch. Do not add CI or retained evidence artifacts. Record only commands that actually completed.

## Recommended implementation sequence

1. extend the existing result projection to clear transient retry metadata when the lease is released;
2. add the final-state assertions to the real middle-component retry regression;
3. run the focused checks;
4. run the existing smoke gate;
5. reconcile Plan 058, Plan 068, and Plan 069 metadata in one documentation closure;
6. stop—no further architecture or verification pass is warranted unless the focused regression finds a new concrete defect.

## Plan acceptance criteria

- [x] A released runtime lease produces `runtime_cleanup_complete=True` and `retryable=False`.
- [x] Successful retry removes stale `runtime cleanup incomplete` detail.
- [x] Partial failure continues to expose completed runtime components while remaining retryable and incomplete.
- [x] Durable terminal and reservation fields remain intact across failed and successful runtime projections.
- [x] The existing real component-resume test asserts coherent partial and final result states.
- [x] Retry still does not replay durable finalization or completed active/quota/usage components.
- [x] Roadmap 058 registers Plans 068 and 069 in its implementation-plan list.
- [x] Roadmap 058’s dependency sequence extends through Plan 069.
- [x] Plan 068 identifies Plan 069 as its narrow corrective successor.
- [x] Plans 058, 068, and 069 have coherent status and acceptance metadata after verification.
- [x] Focused formatting, lint, type, unit, and smoke checks pass.
- [x] No queue, migration, dependency, result hierarchy, state machine, CI expansion, fault matrix, soak gate, benchmark gate, or evidence system is introduced.

## Rejection conditions

Do not close this plan if:

- a completed retained job can still report both `runtime_cleanup_complete=True` and `retryable=True`;
- a completed retained job still reports `runtime cleanup incomplete`;
- the first failure loses completed component progress;
- retry replays durable finalization or a completed runtime component;
- durable result fields are lost while normalizing retry metadata;
- Roadmap 058 still omits Plan 068 or Plan 069 from its implementation registry;
- planning documents claim completion before the focused checks run;
- implementation adds another ownership record, retry mechanism, result abstraction, or disproportionate verification infrastructure.

## Definition of done

This final closure patch is complete when the retained finalization result is coherent both while retry-pending and after successful convergence, the existing real component-resume regression proves those states and non-replay, Roadmap 058 accurately registers the full Plan 059–069 sequence, Plans 058, 068, and 069 report truthful closure, and the existing focused plus smoke verification passes without expanding the project’s architecture or CI burden.

## Implementation closure

Implemented in the existing lease-to-result projection helper. A released lease
now normalizes transient runtime retry metadata to `retryable=False` and
`detail=""`; incomplete convergence still reports partial component progress
with `retryable=True` and `detail="runtime cleanup incomplete"`.

The existing real middle-component regression now asserts the coherent final
result and preserved durable/reservation facts without replaying completed
runtime components.

Focused and repository CI-equivalent verification completed:

```text
rtk uv run ruff format src/eggpool/request/finalization_job.py tests/unit/test_request_finalization_state_machine.py
2 files left unchanged
rtk uv run ruff check src/eggpool/request/finalization_job.py tests/unit/test_request_finalization_state_machine.py
All checks passed
rtk uv run pytest tests/unit/test_request_finalization_state_machine.py -q --tb=short --maxfail=1
26 passed
rtk uv run pyright src/eggpool/request/finalization_job.py
0 errors, 0 warnings, 0 informations
rtk uv run pytest tests/unit/test_request_coordinator_cleanup.py tests/unit/test_request_finalization_state_machine.py tests/unit/test_request_finalizer.py -q --tb=short --maxfail=1
46 passed
rtk uv run ruff format --check src/ tests/ scripts/
734 files already formatted
rtk uv run ruff check src/ tests/ scripts/
All checks passed
rtk uv run pyright src/ scripts/
0 errors, 0 warnings, 0 informations
rtk uv run pytest tests/smoke/ -q --tb=short --maxfail=1
14 passed
```
