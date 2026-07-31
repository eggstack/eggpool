# Plan 057 — Retained Cleanup Final Closure

Date: 2026-07-31
Status: ready for implementation
Parent roadmap: `plans/045-upstream-streaming-hardening-hotpath-roadmap.md`
Predecessor: `plans/056-retained-cleanup-convergence-closure.md`
Planning baseline: `be6fdc6eaa8d1073982970104607751a562ff2f0`

## Purpose

Close the final narrow defects remaining after Plan 056 without reopening the broader request lifecycle, streaming, timeout, CI, or test architecture.

Plan 056 materially improved the implementation. It added component-progress records, retained task ownership, hard registry capacities, bounded generation-shutdown draining, cancellation-to-terminal helpers, and consistent top-level thinking-budget policy. Those structures are to be retained.

This pass is limited to four corrections:

1. require retained cleanup and compensation to prove full convergence before callers may continue;
2. stop treating an attempt transition as proof that its reservation is released;
3. clear per-attempt selection metadata between retries and use an explicit current/fallback identity for cancellation;
4. make nested `thinking.budget_tokens` obey the same explicit `map_if_known` behavior as top-level `thinking_budget`.

This is the final closure pass for Plans 045–057 unless implementation discovers a materially different runtime defect. Do not add another supervisor, recovery table, background loop, database migration, workflow engine, soak runner, evidence artifact, CI job, or generalized lifecycle abstraction.

## Confirmed residual defects

### 1. Normally returning retained work can still be incomplete

`_cleanup_failed_attempt()` awaits its retained task and then returns without requiring `AttemptCleanupProgress.completed` to be true.

A retained task can return normally while still incomplete. The clearest case is:

- the attempt transitions successfully;
- the reservation release updates no row;
- runtime cleanup is not performed because ownership is ambiguous;
- `progress.completed` remains false;
- the caller nevertheless proceeds toward another account selection.

The same class of defect exists in `_compensate_or_rollback_claim()`: it awaits compensation, marks diagnostics successful, and returns without an explicit final convergence assertion.

A completed task and a converged ownership command are not equivalent. Both caller-facing helpers must fail closed when their progress record is incomplete.

### 2. Claim compensation conflates attempt transition with reservation release

`AttemptFinalizeResult` exposes two separate facts:

- `attempt_transitioned`;
- `reservation_released`.

The compensation path currently treats `attempt_transitioned` as sufficient evidence that the reservation is released. That inference is invalid. The attempt update and reservation update are separate statements, and a zero-row reservation update may mean:

- the reservation was already released by another owner;
- the reservation is still active but the supplied identity is wrong;
- the reservation row is missing;
- another unexpected state prevented transition.

Only the first case is converged. The implementation needs one explicit reservation-terminal fact rather than using an `or` expression to collapse distinct outcomes.

### 3. Per-attempt selection metadata survives retry cleanup

After durable selection, the coordinator stores attempt-specific values in `context.client_metadata`, including:

- `_post_commit_selected`;
- `post_commit_published`;
- `post_commit_interrupted`.

After a retryable attempt is cleaned up and the request moves toward another selection, those values can still describe the prior attempt.

If the request is cancelled during the next routing or persistence phase, `_handle_selection_cancellation()` can consume the stale prior-attempt identity and stale publication flags. This can produce incorrect cancellation attribution, incorrect `health_already_applied` behavior, and misleading retained-finalization history.

The request still needs a valid identity when cancellation occurs between attempts because the overall request row remains pending. That identity must be passed explicitly from the retry loop, not inferred from stale publication metadata.

### 4. Nested budget mapping is stricter than the equivalent top-level mapping

Top-level `thinking_budget` under `map_if_known` now performs an exact inverse lookup in `effort_to_budget_tokens` and maps to an effort only when the contract explicitly defines the relationship.

Nested `thinking.budget_tokens` currently rejects under an effort-only contract without attempting the equivalent exact mapping. Rejection is safer than dropping, but the nested and top-level forms do not implement the same policy.

The nested path should map only an exact known budget and reject all unknown or ambiguous values.

## Scope

### Primary implementation files

- `src/eggpool/request/coordinator.py`
- `src/eggpool/request/attempt_finalizer.py`
- `src/eggpool/db/repositories.py` only if a small reservation-status helper belongs there
- `src/eggpool/transcoder/provider_adaptation.py`
- `tests/unit/test_request_coordinator_cleanup.py`
- existing provider-adaptation tests
- one existing request-lifecycle integration file only if needed to prove a durable request does not remain pending
- Plans 056 and 057 status wording after verification

### Explicitly out of scope

- stream completion or EOF behavior;
- stream timeout policy;
- retry counts, routing scores, account health policy, backoff, or quarantine;
- redesigning `RequestFinalizationSupervisor`;
- replacing the coordinator-owned retained dictionaries;
- new persistent cleanup state;
- automatic cleanup retry loops;
- new runtime endpoints or dashboard cards;
- broad refactoring of `RequestCoordinator`, `RequestFinalizer`, or `AttemptFinalizer`;
- new CI jobs, matrices, markers, coverage gates, artifacts, or soak tests;
- making the full test suite mandatory.

## Design constraints

1. **Convergence is explicit.** Caller-facing cleanup helpers may return successfully only when their progress record says every required component is converged.
2. **Durable facts remain distinct.** Attempt transition and reservation terminal state must not be collapsed into one boolean.
3. **Retry metadata is attempt-scoped.** Publication and compensation markers must be cleared before moving to the next attempt.
4. **Cancellation identity is explicit.** Cancellation between attempts uses a selected-attempt value passed by the retry loop, not stale context metadata.
5. **No replay of completed releases.** Existing component progress remains authoritative for resumable runtime cleanup.
6. **No new architecture.** Use the existing progress records, retained tasks, finalizers, and cancellation helpers.
7. **Minimal verification.** Add no more than five focused tests for this plan.

## Phase A — Enforce successful-return convergence

### Goal

Prevent retry selection or compensation success reporting while any required component remains unresolved.

### Required changes

1. Add one small local exception or use an existing internal lifecycle exception to represent incomplete retained cleanup. A new public error hierarchy is not required.
2. After `await asyncio.shield(task)` in `_cleanup_failed_attempt()`:
   - re-read the progress record by identity;
   - require `progress.completed is True`;
   - if the record is missing unexpectedly or remains incomplete, raise a clear internal error;
   - do not select another account.
3. After retained work in `_compensate_or_rollback_claim()`:
   - require `progress.completed is True` before setting `post_commit_interrupted` success metadata or recording compensation success;
   - an incomplete record must remain retained for explicit rejoin or shutdown drain;
   - diagnostics must record failure/incomplete rather than success.
4. `_join_attempt_cleanup()` and `_join_claim_compensation()` remain boolean convergence checks, but their callers must treat `False` as fail-closed rather than a soft diagnostic.
5. Do not silently convert incomplete cleanup into an ordinary upstream exhaustion response.
6. Preserve the existing rule that no further account selection occurs until the previous committed attempt has converged.

### Suggested narrow helper

A private helper is acceptable to avoid duplicated assertions:

```python
def _require_cleanup_completed(
    *,
    identity: tuple[str, int],
    progress: AttemptCleanupProgress | ClaimCompensationProgress | None,
    operation: str,
) -> None:
    ...
```

Keep the helper local to the coordinator. Do not introduce a generic retained-work framework.

### Acceptance criteria

- A normally returning attempt-cleanup task with `completed=False` causes the request to fail closed.
- No second account selection occurs after that incomplete result.
- A normally returning claim-compensation task with `completed=False` is not reported as successful.
- Incomplete progress remains available for one explicit rejoin or shutdown drain.
- Completed progress preserves current behavior and is removed normally.

## Phase B — Add truthful reservation convergence

### Goal

Represent whether the durable reservation is terminal independently from whether the current call performed the release.

### Preferred implementation

Extend `AttemptFinalizeResult` with one additional fact:

```python
@dataclass(frozen=True, slots=True)
class AttemptFinalizeResult:
    attempt_transitioned: bool
    reservation_released: bool
    reservation_converged: bool
```

Semantics:

- `reservation_released=True`: this invocation changed the reservation from active to released;
- `reservation_converged=True`: after the transaction, the reservation is known to be non-active/released for the supplied reservation identity;
- `reservation_converged=False`: the reservation remains active, is missing, or cannot be confirmed terminal.

### Required behavior

1. Keep attempt finalization and reservation release in the existing transaction.
2. When the release update affects a row, set both `reservation_released` and `reservation_converged` true.
3. When the release update affects no row, perform one bounded status lookup using the same transaction/connection:
   - released/terminal status -> `reservation_converged=True`;
   - active status -> `reservation_converged=False`;
   - missing or unknown status -> fail closed with `reservation_converged=False`.
4. If the attempt was already terminal, still determine the reservation's current durable state when the caller needs convergence.
5. Do not redefine `reservation_released`; existing callers may depend on its meaning as “this invocation performed the transition.”
6. Update attempt-cleanup and compensation progress using `reservation_converged`, not `attempt_transitioned`.
7. Runtime quota and active-count cleanup remains governed by the existing acquired/released progress. Do not replay runtime releases merely because the durable reservation was already terminal.

### Repository helper option

If direct SQL in `AttemptFinalizer` would duplicate repository conventions, add one small method such as:

```python
async def get_status(self, reservation_id: str) -> str | None:
    ...
```

Do not add a new repository class, table, migration, or reconciliation subsystem.

### Acceptance criteria

- `attempt_transitioned=True` and `reservation_released=False` does not automatically count as converged.
- A previously released reservation is recognized as converged without releasing it again.
- An active reservation remains incomplete and fails closed.
- A missing reservation is not reported as successfully released.
- Claim compensation records success only after `reservation_converged=True`.

## Phase C — Reset retry-boundary metadata and use explicit cancellation identity

### Goal

Ensure cancellation is attributed to the correct attempt throughout a multi-attempt request.

### Required changes

1. Define the attempt-scoped metadata keys in one local tuple or helper:

```python
_ATTEMPT_SELECTION_METADATA_KEYS = (
    "_post_commit_selected",
    "post_commit_published",
    "post_commit_interrupted",
)
```

2. After a retryable attempt cleanup fully converges and before checking/selecting another account, clear those attempt-scoped keys.
3. Do not clear request-wide metadata such as:
   - `db_request_id`;
   - thinking/compression/segmentation diagnostics;
   - `_cancelled_request_finalized` once terminal submission has occurred.
4. Track the most recent fully converged selected attempt in a local variable within `execute()`.
5. When cancellation occurs during a subsequent selection:
   - prefer a newly committed `_post_commit_selected` identity when present;
   - otherwise use the explicit last-converged attempt passed from `execute()`;
   - do not infer publication state for the fallback identity from stale metadata.
6. For cancellation between attempts using the fallback identity:
   - terminalize the overall request as `CLIENT_CANCELLED`;
   - set `health_already_applied=True`, because the prior attempt cleanup already applied/released its health ownership;
   - do not release the prior attempt's runtime quota or active count again.
7. If cancellation occurs after a new claim commits, retain the existing compensation/publication convergence logic for that new attempt.
8. Ensure only one request-terminal job is submitted.

### Suggested signature adjustment

A narrow signature change is sufficient:

```python
async def _handle_selection_cancellation(
    self,
    context: ProxyRequestContext,
    *,
    fallback_selected: SelectedAttempt | None = None,
) -> bool:
    ...
```

No context-wide state machine is needed.

### Acceptance criteria

- After first-attempt cleanup, the attempt-scoped publication metadata is absent before retry selection.
- Cancellation before the second attempt commits uses the first attempt only as an explicit fallback request identity.
- Cancellation after the second attempt commits uses the second attempt identity.
- The fallback path uses `health_already_applied=True` and does not replay quota, active-count, health, or probe release.
- Supervisor history contains one cancellation terminal for the correct identity.
- The durable request does not remain pending.

## Phase D — Align nested budget `map_if_known`

### Goal

Make nested and top-level explicit-budget controls obey the same mapping rule.

### Required behavior

1. In `_adapt_thinking_block()`, when `thinking.budget_tokens` is present under an effort-only contract:
   - `reject`: reject;
   - `warn_drop`: remove the budget and report the drop;
   - `map_if_known`: perform an exact inverse lookup in `contract.effort_to_budget_tokens`.
2. If exactly one effort maps to the requested budget:
   - remove `budget_tokens`;
   - emit `thinking.effort=<mapped effort>`;
   - retain a valid structural `thinking.type` where the selected contract accepts it;
   - report a mapped decision and warning/trace consistent with top-level mapping.
3. If no effort maps exactly, reject.
4. If multiple efforts map to the same budget, reject as ambiguous rather than choosing by dictionary order.
5. Validate the mapped effort against `accepted_efforts` when that list is present.
6. Preserve budget behavior for `budget` and `effort_or_budget` contracts.
7. Preserve historical reasoning content and unrelated payload fields.
8. Do not create approximate, nearest-budget, clamped, or heuristic mappings.

### Acceptance criteria

- Known nested budget maps to one accepted effort under `map_if_known`.
- Unknown nested budget rejects.
- Ambiguous inverse mapping rejects.
- `warn_drop` behavior remains unchanged.
- Top-level and nested exact mappings produce equivalent provider intent.
- Existing OpenCode Go fixed-contract behavior remains unchanged.

## Phase E — Focused verification and truthful closure wording

### Test budget

Add or modify no more than **five focused tests** for Plan 057. Prefer parameterization and existing fixtures.

Required coverage:

1. **Incomplete attempt cleanup fails closed**
   - mock an attempt transition with reservation not converged;
   - assert `_cleanup_failed_attempt()` raises;
   - assert a second selection is not invoked.

2. **Compensation requires reservation convergence**
   - first run reports attempt transitioned but reservation still active;
   - assert compensation is incomplete and not recorded successful;
   - second explicit rejoin observes the reservation released and completes without duplicate runtime release.

3. **Actual waiter cancellation during retained cleanup**
   - use an `asyncio.Event` or future to hold the retained child;
   - cancel the parent waiter while the child is active;
   - release the child;
   - assert one `CLIENT_CANCELLED` terminal submission and no pending durable request in the integration fixture where available.

4. **Two-attempt cancellation identity**
   - complete cleanup for attempt one;
   - begin attempt-two selection but cancel before its commit;
   - assert stale attempt-one publication metadata was cleared;
   - assert fallback cancellation uses the explicit converged identity and does not replay runtime release.

5. **Nested budget policy table**
   - exact known mapping;
   - unknown mapping rejection;
   - ambiguous mapping rejection;
   - `warn_drop` preservation.

One test may cover more than one requirement. Do not add a bespoke harness or timed test.

### Existing verification to retain

Run the focused files changed by this plan plus the repository's existing lightweight gates:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
```

Then run only the relevant focused tests, for example:

```bash
uv run pytest \
  tests/unit/test_request_coordinator_cleanup.py \
  tests/unit/test_provider_request_adaptation.py \
  -q --tb=short --maxfail=1
```

Use actual existing test paths. Add one existing lifecycle integration file only if required for the durable request-status assertion.

The full test suite remains optional and must not become a CI or closure requirement.

### Documentation correction

After implementation and focused checks pass:

1. Mark Plan 057 complete with the implementation commit SHA.
2. Amend Plan 056 status to state that its main implementation landed at `a5c7924`, while final convergence and retry-identity closure is completed by Plan 057.
3. Update lifecycle documentation only where it currently claims that any normally returning retained task necessarily proves convergence.
4. Do not add test transcripts, evidence JSON, screenshots, or release notes solely for this corrective pass.

## Final acceptance criteria

Plan 057 is complete only when all of the following are true:

1. `_cleanup_failed_attempt()` cannot return successfully with incomplete progress.
2. `_compensate_or_rollback_claim()` cannot report success with incomplete progress.
3. No new account is selected after incomplete cleanup or compensation.
4. Attempt transition and reservation convergence remain separate facts.
5. A reservation is considered converged only when its durable state is confirmed terminal.
6. A missing or active reservation is never treated as successfully released.
7. Rejoin does not repeat completed quota, active-count, health, or probe releases.
8. Retryable-attempt publication metadata is cleared before another selection.
9. Cancellation between attempts uses an explicit fallback selected identity rather than stale publication metadata.
10. Cancellation after a new commit uses the new attempt identity.
11. Cancellation terminalization produces one real `CLIENT_CANCELLED` request outcome.
12. The durable request does not rely on stale-request recovery to leave `pending` in covered cancellation paths.
13. Nested `thinking.budget_tokens` supports exact known `map_if_known` mapping.
14. Unknown or ambiguous nested budget mappings reject.
15. Existing top-level budget policy behavior remains passing.
16. Existing fixed-contract/OpenCode Go behavior remains passing.
17. Existing stream completion, premature EOF, and supervisor-leak behavior remains unchanged.
18. Registry capacity and shutdown drain behavior from Plan 056 remains unchanged.
19. No database migration, new supervisor, background loop, soak runner, evidence system, CI job, or generalized lifecycle framework is introduced.
20. Mandatory CI remains the single Python 3.11 format/lint/type/smoke job established by Plan 054.

## Handoff order

Recommended implementation order:

1. Phase B — expose truthful reservation convergence;
2. Phase A — enforce convergence at caller boundaries;
3. Phase C — clear retry metadata and pass explicit cancellation identity;
4. Phase D — align nested budget mapping;
5. Phase E — add focused regressions and correct closure wording.

Suggested commit boundaries:

1. `Require durable reservation convergence`
2. `Fail closed on incomplete retained cleanup`
3. `Reset retry metadata before reselection`
4. `Align nested thinking budget mapping`
5. `Add Plan 057 regressions and close documentation`

Fewer commits are acceptable if the implementation remains reviewable. Do not split this into additional plans unless a materially different defect is discovered.