# Plan 056 — Retained Cleanup Convergence Closure

Date: 2026-07-31
Status: ready for implementation
Parent roadmap: `plans/045-upstream-streaming-hardening-hotpath-roadmap.md`
Predecessor: `plans/055-terminal-stream-lifecycle-corrective-pass.md`
Planning baseline: `13cdd493c90bd6019ca4730e0d49c354e5b3e30e`

## Purpose

Close the narrow residual defects left after Plan 055 without reopening the streaming, timeout, CI, or broader request-lifecycle architecture.

Plan 055 successfully corrected the original `pending_stream` supervisor leak, moved stream terminal outcomes onto `_finalize_terminal()`, removed unsafe generator-stage EOF retry claims, and retired the active total-lifetime timer. Those areas are not to be redesigned in this pass.

The remaining work is limited to:

1. making retained retry-attempt cleanup resumable after partial failure;
2. making post-commit claim compensation resumable after partial failure;
3. ensuring cancellation of the request waiter still produces a terminal request row after retained cleanup converges;
4. bounding and draining the two coordinator-owned retained-task registries;
5. correcting the final top-level thinking-control policy inconsistencies;
6. adding a small set of focused tests that exercise the actual residual failure boundaries.

This is a closure pass. It must not introduce another supervisor, workflow engine, recovery table, test harness, soak runner, CI job, or evidence system.

## Confirmed residual defects

### 1. Failed-attempt cleanup loses component progress

The retained failed-attempt cleanup task currently performs durable attempt finalization, quota cleanup, active-count release, and health/probe cleanup as a sequence. If one later step fails after an earlier step succeeded, the task exits and is removed from the coordinator registry.

A subsequent call can observe that the durable attempt already transitioned and return early, skipping unfinished runtime cleanup. This can leave active-count, quota, or health ownership stranded even though the durable attempt is terminal.

The retained command therefore needs a small in-memory progress record that survives task failure and permits a later caller to resume only unfinished steps.

### 2. Request cancellation can leave the request row pending

`asyncio.shield()` correctly allows retained attempt cleanup or claim compensation to continue after the caller is cancelled. It does not, by itself, finalize the overall request.

If the request waiter is cancelled while a retryable failed-attempt cleanup is running, `execute()` may exit before `_handle_exhausted()` or another request-terminal path runs. The attempt and reservation can converge while the durable request remains `pending` until startup/stale recovery.

Normal-path cancellation must submit a `CLIENT_CANCELLED` request-terminal command after the retained attempt cleanup or compensation reaches a safe convergence point.

### 3. Claim compensation can repeat completed releases

`RuntimePublicationReceipt` correctly distinguishes whether active-count and quota publication occurred. The compensation task, however, does not persist which releases have already completed.

If active-count decrement succeeds and quota removal or durable compensation later fails, a rejoin can repeat the decrement because the original publication receipt still says the component was acquired. Clamping a count at zero masks the duplicate operation but does not satisfy exact, resumable ownership semantics.

Compensation needs component-level release progress and must resume only unfinished steps.

### 4. Retained cleanup registries are unbounded and not drained explicitly

The coordinator currently retains attempt-cleanup and claim-compensation tasks in ordinary dictionaries. Completed tasks are removed, but hung or blocked tasks can accumulate without a capacity limit. The registries are also not part of the documented shutdown drain.

A small explicit limit and bounded shutdown drain are sufficient. Do not generalize this into a new task-supervision subsystem.

### 5. Top-level thinking-budget policy still bypasses configured behavior

Nested thinking-field handling was improved by Plan 055, but top-level `thinking_budget` and the `none` contract still contain inconsistent behavior:

- an unsupported top-level budget can be silently dropped even under `unsupported_control = "reject"`;
- `map_if_known` can fall through to dropping controls where no valid mapping exists;
- existing tests encode at least one of these inconsistent expectations.

All selectable controls must obey the same policy regardless of whether they arrive as top-level fields or inside a `thinking` object.

## Scope

### Primary implementation files

- `src/eggpool/request/coordinator.py`
- the existing request runtime-ownership helper module, if one already contains `AttemptRuntimeLease` or equivalent component state
- `src/eggpool/request/attempt_finalizer.py` only where a result type needs to expose durable transition facts already known by the finalizer
- `src/eggpool/transcoder/provider_adaptation.py`
- existing focused unit/integration/smoke tests for coordinator cleanup and provider adaptation
- `plans/055-terminal-stream-lifecycle-corrective-pass.md` or adjacent lifecycle documentation only to correct premature closure wording

### Explicitly out of scope

- changes to streaming EOF classification;
- changes to canonical stream finalization paths already corrected in Plan 055;
- changes to retry count, routing policy, health penalties, quarantine, or provider backoff;
- database migrations or new durable cleanup tables;
- a second finalization supervisor;
- generalized retained-task abstractions shared across unrelated subsystems;
- automatic infinite retry of cleanup failures;
- new background polling loops;
- new CI jobs, matrices, markers, artifacts, coverage gates, or soak tests;
- broad rewrites of `RequestCoordinator`, `RequestFinalizer`, or `AttemptFinalizer`;
- restoring the full test suite as a mandatory push gate.

## Design constraints

1. **One owner per component.** Active-count, quota reservation, durable attempt/reservation state, health effects, and probe ownership must each have an explicit acquired/released state.
2. **Resume, do not replay.** Rejoining a failed retained command must execute only unfinished releases.
3. **Request terminalization is separate from attempt cleanup.** Retryable attempt cleanup must not falsely mark the request terminal, but caller cancellation must eventually submit a real request-terminal command.
4. **No hidden success.** A retained cleanup failure must abort further selection/retry for the request. It must not silently continue with another account.
5. **Bounded state.** The coordinator-owned registries must have a small hard capacity and a bounded shutdown drain.
6. **Keep recovery as a safety net.** Startup/stale reconciliation remains useful, but normal cancellation and partial failure must not intentionally depend on it.
7. **Minimal verification.** Use fast deterministic fault injection, not duration-based tests.

## Phase A — Add resumable attempt-cleanup progress

### Goal

Make failed-attempt cleanup idempotent across partial failure, rejoin, and waiter cancellation.

### Required shape

Introduce one small progress structure keyed by `(proxy_request_id, attempt_id)`. It may be a dataclass adjacent to the existing coordinator cleanup code or may reuse an existing component-aware runtime lease if that type already matches the need.

A suitable shape is:

```python
@dataclass(slots=True)
class AttemptCleanupProgress:
    durable_transition_checked: bool = False
    durable_attempt_transitioned: bool = False
    durable_reservation_released: bool = False
    quota_released: bool = False
    active_count_released: bool = False
    health_effect_applied: bool = False
    probe_released: bool = False
    completed: bool = False
```

Exact field names are discretionary. The implementation must preserve the distinctions represented above.

### Required behavior

1. Create or retrieve the progress record before starting the retained cleanup task.
2. Durable attempt finalization runs once per cleanup identity.
3. Record the durable result before any runtime release awaits.
4. Release quota only when the attempt/reservation facts indicate the coordinator owns a published quota reservation and the progress record does not already show it released.
5. Decrement active count only when it was acquired and not already released.
6. Apply health effects at most once.
7. Release a health/circuit probe even when the selected failure carries no provider penalty.
8. Mark `completed` only after every required component has converged.
9. Remove the progress/task entry only after successful completion.
10. If the task fails, retain enough progress to allow one later explicit rejoin to resume.
11. Do not automatically spin or retry indefinitely. A later duplicate submission, request shutdown drain, or explicit test rejoin may resume the command.

### Early-return rule

Do not use `attempt_transitioned == False` as a reason to skip runtime cleanup. It can mean the durable transition occurred during an earlier partial run. The progress record, durable reservation result, and acquired-component facts must decide which runtime releases remain.

### Failure behavior

- A cleanup exception aborts further account selection for the current request.
- The exception is surfaced or translated into the existing internal upstream/system error path.
- The retained progress remains available for a bounded rejoin or shutdown drain.
- Duplicate callers join the same in-flight task.

### Acceptance criteria

- Failure after durable attempt transition but before quota release can be rejoined and completes all remaining runtime cleanup.
- Failure after quota release but before active-count decrement does not remove quota twice.
- Failure after active-count decrement but before health/probe release does not decrement active count twice.
- A completed cleanup leaves no task or progress entry.
- A failed cleanup retains one bounded resumable entry rather than spawning duplicates.

## Phase B — Make claim compensation resumable

### Goal

Make post-commit runtime-publication compensation converge exactly once across partial failure, cancellation, and rejoin.

### Required shape

Extend `RuntimePublicationReceipt` or pair it with a compact compensation progress structure. The implementation must distinguish both acquisition and release:

```python
@dataclass(slots=True)
class RuntimePublicationReceipt:
    active_count_added: bool = False
    quota_reservation_added: bool = False

@dataclass(slots=True)
class ClaimCompensationProgress:
    active_count_released: bool = False
    quota_reservation_released: bool = False
    durable_attempt_finalized: bool = False
    durable_reservation_released: bool = False
    probe_released: bool = False
    completed: bool = False
```

The exact type split is discretionary. A single dataclass is acceptable if it remains clear.

### Required order

Use a deterministic order and persist progress after each successful step. Recommended order:

1. release runtime active-count publication if acquired;
2. remove runtime quota publication if acquired;
3. finalize the durable attempt/reservation as `post_commit_interrupted`;
4. release the health/probe slot;
5. record completion diagnostics;
6. mark complete and remove the registry entry.

A different order is acceptable only if it preserves the same convergence and no-new-selection guarantees.

### Required behavior

- A rejoin must not repeat a completed active-count decrement.
- A rejoin must not repeat a completed quota removal.
- Durable finalization remains idempotent through the existing repository/finalizer guard.
- The task is retained independently of the request waiter.
- A failure keeps the progress record for one later rejoin or shutdown drain.
- No new account may be selected while compensation for the committed claim remains incomplete.

### Acceptance criteria

- Failure immediately after active-count release resumes at quota release.
- Failure immediately after quota release resumes at durable compensation.
- Failure after durable compensation resumes at probe release.
- Each acquired runtime component is released exactly once according to observable mock call counts.
- Final durable state is `post_commit_interrupted` with the reservation released.

## Phase C — Bridge waiter cancellation to request-terminal finalization

### Goal

Ensure cancellation during retained attempt cleanup or compensation does not leave `requests.status = pending` as the normal outcome.

### Required behavior

1. Catch `CancelledError` around awaits of retained attempt cleanup and retained claim compensation.
2. Do not cancel the retained child task.
3. Wait for, join, or arrange a retained continuation that observes the child cleanup result.
4. Once the selected attempt/claim ownership is safe, submit the canonical request-terminal command using `_finalize_terminal()` with `CLIENT_CANCELLED`.
5. Preserve the original cancellation semantics to the downstream caller after terminal ownership has been submitted.
6. If cleanup itself fails, do not falsely record a clean client cancellation before ownership convergence. Record/log the cleanup failure and leave the bounded retained entry available for drain/rejoin.
7. Do not submit both `CLIENT_CANCELLED` and another terminal result for the same selected attempt.

### Preferred narrow implementation

Use one small helper such as:

```python
async def _await_cleanup_then_finalize_cancelled(...):
    ...
```

It may itself be retained by the existing request finalization supervisor or by the same bounded coordinator registry. Avoid adding another general-purpose cancellation manager.

### Terminal identity

The final request-terminal job must use the real `(proxy_request_id, attempt_id)` and the actual `CLIENT_CANCELLED` outcome. Do not introduce placeholder outcomes such as `pending_cleanup`.

### Acceptance criteria

- Cancelling the request waiter during failed-attempt cleanup leaves the attempt terminal, reservation released, runtime ownership released, and request terminal as `client_cancelled`.
- Cancelling during claim compensation produces the same request-level terminal convergence after compensation completes.
- The stale-request finalizer is not required in either test.
- Supervisor history contains one real request-terminal outcome and no placeholder entry.

## Phase D — Bound and drain retained cleanup registries

### Goal

Prevent hung cleanup tasks from accumulating indefinitely while keeping the implementation lightweight.

### Required behavior

1. Add one small hard capacity for each coordinator-owned registry, or a shared combined capacity if both registries are stored together.
2. The default capacity should be proportional to realistic in-flight requests and remain small for SBC deployment. A value in the range `64–256` is acceptable; prefer the smallest value that does not interfere with configured concurrency.
3. At capacity, fail closed:
   - do not create an untracked detached task;
   - do not proceed to another account selection;
   - return/log a clear internal cleanup-capacity error.
4. Add a bounded shutdown drain that awaits current retained cleanup/compensation tasks for the existing shutdown budget.
5. After the budget, report unresolved identities and allow existing startup recovery to handle durable leftovers.
6. Do not add periodic scanning, background retry, persistence, or a new supervisor class.

### Diagnostics

Expose only compact process-local counts where an existing runtime diagnostics surface already exists:

- active attempt-cleanup tasks;
- failed/resumable attempt-cleanup entries;
- active compensation tasks;
- failed/resumable compensation entries;
- capacity-rejection count.

Omit this step if adding fields would require a broad dashboard or schema change. Logging plus tests is sufficient for closure.

### Acceptance criteria

- Registry size never exceeds its configured capacity.
- Capacity exhaustion does not create an untracked task or continue routing.
- Shutdown drain removes completed tasks and reports unresolved ones without hanging indefinitely.
- No new background loop or persistence table is introduced.

## Phase E — Finish selectable-control policy consistency

### Goal

Apply `reject`, `warn_drop`, and `map_if_known` consistently to all selectable thinking controls, including top-level budget fields.

### Required behavior

1. Route top-level `thinking_budget` through the same typed field-adaptation policy used for other controls.
2. Under `reject`, an unsupported top-level budget raises `CapabilityError` before upstream dispatch.
3. Under `warn_drop`, an unsupported top-level budget is removed and reported as dropped.
4. Under `map_if_known`, a known budget-to-effort or effort-to-budget mapping may be applied only when the contract explicitly defines it; otherwise reject rather than silently drop.
5. For the `none` contract:
   - `reject` rejects any selectable control;
   - `warn_drop` drops and reports it;
   - `map_if_known` rejects because there is no supported target unless an explicit mapping exists.
6. Keep nested and top-level controls behaviorally aligned.
7. Preserve historical reasoning content and non-selectable message content unchanged.
8. Correct existing tests that currently expect silent dropping under the default rejecting policy.

### Focused policy table

| Contract | Input | Policy | Required result |
|---|---|---|---|
| effort-only | top-level `thinking_budget` | reject | `CapabilityError` |
| effort-only | top-level `thinking_budget` | warn_drop | field removed and reported |
| effort-only | known budget mapping | map_if_known | mapped only when contract defines it |
| effort-only | unknown budget mapping | map_if_known | `CapabilityError` |
| none | any selectable control | reject | `CapabilityError` |
| none | any selectable control | warn_drop | removed and reported |
| none | any selectable control | map_if_known | `CapabilityError` unless explicit mapping exists |

## Phase F — Focused verification and documentation correction

### Test budget

Add or modify no more than **seven focused tests** for this closure pass. Prefer table-driven cases and existing fixtures.

Required coverage:

1. failed-attempt cleanup partial failure after durable transition, then successful rejoin;
2. failed-attempt cleanup partial failure after one runtime release, proving no duplicate release;
3. claim compensation partial failure at both publication-release boundaries, table-driven;
4. cancellation during failed-attempt cleanup terminalizes the request;
5. cancellation during claim compensation terminalizes the request;
6. registry capacity fails closed and remains bounded;
7. top-level thinking-budget/`none` policy table.

A separate test is not required for every row if one parameterized test covers the policy table.

### Existing stream verification

Do not expand stream test infrastructure. Keep the existing Plan 055 regression that verifies stream supervisor active count returns to zero. A fast count loop may be increased only if it remains materially cheap; do not add a timed soak.

### Documentation changes

After implementation and tests pass:

- change Plan 055 status from unconditional complete to a truthful note that stream-specific fixes landed at `13cdd493...` and remaining convergence closure moved to Plan 056;
- mark Plan 056 complete with the implementation commit SHA;
- correct any architecture wording that calls the coordinator-owned dictionaries “bounded” unless the capacity and drain are actually implemented;
- do not add evidence files or test transcripts.

## Required validation commands

Use the repository's existing lightweight environment and commands. Do not alter CI.

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
```

Run only the focused unit/integration files changed by this plan in addition to the smoke suite, for example:

```bash
uv run pytest \
  tests/unit/test_request_coordinator_cleanup.py \
  tests/unit/test_provider_adaptation.py \
  tests/integration/test_request_lifecycle.py \
  -q --tb=short --maxfail=1
```

Use actual existing file names rather than creating files solely to match this example.

The full test suite is optional and must not become a closure requirement or CI gate.

## Final acceptance criteria

Plan 056 is complete only when all of the following are true:

1. Failed-attempt cleanup records component-level progress and resumes after a partial failure.
2. Rejoining failed-attempt cleanup does not repeat completed quota, active-count, health, or probe releases.
3. Claim compensation records component-level progress and resumes after a partial failure.
4. Rejoining claim compensation does not repeat completed active-count or quota releases.
5. No new account is selected while cleanup or compensation for the previous committed attempt remains incomplete.
6. Cancellation during retained failed-attempt cleanup eventually submits one canonical `CLIENT_CANCELLED` request-terminal job.
7. Cancellation during retained claim compensation eventually submits one canonical `CLIENT_CANCELLED` request-terminal job.
8. Normal cancellation tests leave no durable request row in `pending` state.
9. Attempt/reservation/runtime ownership converges without restart, database deletion, or stale-recovery execution.
10. Attempt-cleanup and compensation registries have an explicit hard capacity.
11. Capacity exhaustion fails closed and does not create detached untracked work.
12. Shutdown performs a bounded drain of retained cleanup and compensation tasks.
13. Unsupported top-level `thinking_budget` obeys the configured policy.
14. `map_if_known` never silently drops an unmappable selectable control.
15. The `none` contract obeys `reject`, `warn_drop`, and `map_if_known` consistently.
16. Existing OpenCode Go fixed-contract handling remains unchanged and passing.
17. Existing stream completion, premature EOF, and supervisor-leak regressions remain passing.
18. No database migration, new supervisor, background retry loop, soak runner, evidence apparatus, CI job, or test matrix is added.
19. Mandatory CI remains the single Python 3.11 lint/type/smoke job established by Plan 054.
20. Plan and architecture closure wording matches the implemented behavior rather than claiming stronger guarantees.

## Handoff order

Recommended implementation order:

1. Phase A — resumable attempt cleanup;
2. Phase B — resumable claim compensation;
3. Phase C — cancellation-to-terminal bridge;
4. Phase D — capacity and shutdown drain;
5. Phase E — thinking-policy consistency;
6. Phase F — focused verification and documentation correction.

Keep commits small and behavior-oriented. Suggested commit boundaries:

1. `Make retained attempt cleanup resumable`
2. `Make claim compensation resumable`
3. `Finalize cancelled requests after retained cleanup`
4. `Bound cleanup registries and finish thinking policy`
5. `Add focused Plan 056 regressions and close documentation`

Fewer commits are acceptable if the final diff remains easy to review. Do not split this work into additional planning phases unless implementation discovers a materially different defect.