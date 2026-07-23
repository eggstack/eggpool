# Reload Atomicity Corrective Closure Plan

Date: 2026-07-23
Status: implementation handoff
Depends on: `plans/015-reload-atomicity-final-closure.md`
Implementation baseline: `25ba442efec0b20524a7f272443b461de8b8ebc9`

## Objective

Correct the remaining implementation defects in the Plan 015 staged reload protocol without reopening the broader reload architecture.

Plan 015 established the right major components:

- `PendingGenerationSwap`;
- event-driven lease gating;
- explicit runtime-staged and runtime-committed states;
- reversible process transitions;
- stricter control-socket handling;
- focused reload integration tests.

The remaining defects are implementation-level correctness gaps around those components:

1. staged-swap state is mutated without one runtime-manager synchronization boundary;
2. an acquire operation can race publication and return an old-generation lease after commit;
3. cancellation after staging can leave lease admission gated indefinitely;
4. a partially applied process-transition stack is lost when `apply_all()` raises;
5. several transition rollback implementations suppress their own failures;
6. post-commit housekeeping failures are processed as if rollback were still safe;
7. the current “SQLite commit failure” test injects before `COMMIT`, not at the outer commit boundary;
8. peer-credential validation fails open and does not terminate request handling cleanly;
9. transaction and pending-swap diagnostics can remain incomplete or stale;
10. two roadmap-relevant test exemptions and exact-head CI evidence remain unresolved.

This plan is complete only when these defects are closed with deterministic tests and the exact implementation commit is verified by CI.

---

# Scope

## In scope

- `RuntimeManager` and `PendingGenerationSwap` synchronization and lease linearization;
- cancellation-safe staged-swap cleanup;
- process-transition apply, rollback, finalize, and failure ownership;
- post-commit finalization and compensation semantics;
- true SQLite outer-commit failure injection;
- staged-swap race and visibility tests using distinct generations;
- Linux `SO_PEERCRED` fail-closed behavior;
- pending-swap and transaction diagnostic accuracy;
- removal of the remaining reload-related skip/xfail exemptions;
- focused CI coverage and exact-head evidence.

## Explicit non-goals

Do not:

- replace the generation/lease architecture;
- redesign configuration diffing or validation;
- move reload to another process or distributed transaction system;
- introduce a general two-phase commit framework;
- make `app.state` request-authoritative again;
- change provider routing, transcoding, compression, or dashboard behavior;
- expand the control protocol beyond the existing reload command;
- optimize unrelated request-path hot spots;
- refactor all legacy publication helpers unless required to prevent production use.

The target is a narrow correctness closure of the Plan 015 implementation.

---

# Required invariants

The implementation must encode and test the following invariants.

## Runtime publication invariants

1. There is at most one pending swap owned by a `RuntimeManager`.
2. Every mutation of active slot, pending swap, lease gate, slot lease eligibility, and retirement eligibility occurs under the runtime-manager synchronization boundary.
3. Once candidate publication commits, no new lease can be issued from the old generation.
4. Before candidate publication commits, no lease can be issued from the candidate generation.
5. Existing old-generation leases remain valid through staging and drain normally after commit.
6. A staged swap can be rolled back exactly once or committed exactly once.
7. Commit and rollback wake every blocked lease waiter.
8. Cancellation cannot leave lease admission permanently gated.
9. The old generation cannot enter retirement before committed publication.
10. Candidate ownership does not transfer before committed publication.

## Persistence and transition invariants

11. A failure before SQLite outer commit leaves persistence, runtime generation, process transitions, ownership, and retirement unchanged.
12. An injected failure from the SQLite outer `COMMIT` path produces the same unchanged-state result.
13. Every successfully applied transition remains reachable through a rollback owner until commit acceptance.
14. Partial transition failure rolls back the successfully applied prefix in reverse order.
15. Rollback failures are aggregated and surfaced as degraded/compensation failure; they are not swallowed.
16. After runtime and persistence commit, process-transition rollback is forbidden.
17. Post-commit failures use retry/finalization semantics and cannot classify the reload as a clean pre-publication abort.

## Control and diagnostic invariants

18. On Linux, inability to validate peer credentials rejects the connection before request parsing.
19. A mismatched peer UID cannot invoke the reload handler.
20. Completed or rolled-back swaps do not remain visible as pending.
21. Committed transaction diagnostics identify the new active generation.
22. `publication_occurred` reflects candidate lease visibility, not merely that commit work started.
23. CI closure is based on the exact implementation commit, not an earlier branch state.

---

# Workstream A — Make staged swap a synchronized runtime-manager operation

## A1. Remove unsynchronized mutation from `PendingGenerationSwap`

`PendingGenerationSwap.stage()`, `commit()`, and `rollback()` currently mutate `RuntimeManager` internals directly. Correct this by selecting one of the following equivalent designs.

### Preferred design

Keep `PendingGenerationSwap` as an ownership/token object, but route state mutations through private runtime-manager methods:

```python
await runtime_manager._stage_pending_swap(swap)
await runtime_manager._commit_pending_swap(swap)
await runtime_manager._rollback_pending_swap(swap)
```

Each method acquires `RuntimeManager._lock` and validates that:

- `runtime_manager._pending_swap is swap`;
- the swap is in the expected lifecycle state;
- the active generation still matches the captured old generation;
- shutdown has not invalidated the operation;
- no second swap has replaced it.

### Acceptable alternative

Allow `PendingGenerationSwap` methods to acquire `runtime_manager._lock` internally.

Do not leave synchronization as a documented caller responsibility. The methods must enforce their own locking contract.

## A2. Introduce an explicit pending-swap lifecycle

Use an enum rather than independent booleans where practical:

```python
class PendingSwapState(Enum):
    PREPARED = "prepared"
    STAGED = "staged"
    COMMITTED = "committed"
    ROLLED_BACK = "rolled_back"
    FINALIZED = "finalized"
```

Required transitions:

- `PREPARED -> STAGED`;
- `STAGED -> COMMITTED`;
- `STAGED -> ROLLED_BACK`;
- `COMMITTED -> FINALIZED`.

Repeated terminal calls may be idempotent only when they request the same terminal result. Conflicting calls must fail deterministically.

Examples:

- rollback after rollback: no-op or same result;
- commit after commit: no-op or same result;
- rollback after commit: explicit error/no-op with a diagnostic, never mutation;
- commit after rollback: explicit error.

## A3. Enforce one pending swap

`prepare_candidate_swap()` must reject a second unresolved swap.

It must not overwrite `RuntimeManager._pending_swap` while the prior swap is `PREPARED` or `STAGED`.

A prior `COMMITTED`, `ROLLED_BACK`, or `FINALIZED` swap must be cleared before a new swap is accepted.

Add a typed error such as:

- `RuntimeManagerSwapInProgressError`;
- `RuntimeManagerSwapStateError`.

Do not use generic `RuntimeError` for lifecycle conflicts that diagnostics need to classify.

## A4. Clear pending-swap ownership at terminal boundaries

On rollback:

- restore old active state;
- clear the lease gate;
- set the swap state to `ROLLED_BACK`;
- clear `RuntimeManager._pending_swap` under the lock.

On successful finalization:

- confirm retirement scheduling/finalization ownership has been transferred;
- set state to `FINALIZED`;
- clear `RuntimeManager._pending_swap` under the lock.

If post-commit finalization is pending, retain a distinct finalization record rather than pretending the swap is still staged.

## A5. Do not use private swap fields from `ReloadManager`

Replace production access to:

- `pending_swap._old_slot`;
- `runtime_manager._spawn_retirement_task(...)`;

with an owned API:

```python
finalization = await pending_swap.finalize_retirement()
```

The finalization method must schedule retirement itself or return a typed finalization object with a public runtime-manager method.

The reload manager must not reach through the swap abstraction to manipulate private slots.

### Acceptance criteria — Workstream A

- [ ] `stage()`, `commit()`, and `rollback()` cannot mutate runtime-manager publication state outside the manager lock.
- [ ] A second unresolved pending swap is rejected with a typed error.
- [ ] Conflicting lifecycle operations fail deterministically.
- [ ] Rollback clears `_pending_swap` and the lease gate under one lock acquisition.
- [ ] Successful finalization clears `_pending_swap` or moves it to a distinct tracked post-commit finalization record.
- [ ] Production reload code no longer accesses `pending_swap._old_slot`.
- [ ] Production reload code no longer calls `_spawn_retirement_task()` directly.
- [ ] Unit tests cover all legal and illegal pending-swap transitions.

---

# Workstream B — Close the old-generation lease race

## B1. Define the lease publication linearization point

The linearization point must be the locked operation that simultaneously:

1. makes the candidate slot authoritative;
2. marks the candidate accepting;
3. marks the old slot non-accepting;
4. changes the swap state to committed;
5. releases the lease gate.

These actions must occur under one `RuntimeManager._lock` critical section.

Do not release the gate before the old slot is non-accepting.

## B2. Make acquisition validate the active slot under synchronization

The current hot path snapshots `_lease_gate_event` and `_active` without a manager-level atomic validation. A request can therefore miss the gate and later lease an old slot.

Use one of these approaches.

### Preferred approach: generation epoch validation

Maintain a monotonic publication epoch:

- increment only when active publication commits;
- acquire snapshots `(gate, active_slot, epoch)`;
- before incrementing the slot lease count, validate under the slot/manager synchronization that:
  - gate is still absent;
  - active slot is still the same object;
  - epoch is unchanged;
  - slot still accepts leases.

If validation fails, retry through the gate path.

### Acceptable approach: manager-lock lease claim

Acquire `RuntimeManager._lock` for the short active-slot claim:

- check shutdown;
- check gate;
- check active slot and `accepting_leases`;
- increment the slot lease count;
- release lock immediately.

This is acceptable if benchmark evidence shows no meaningful dispatch regression under expected SBC concurrency.

Do not rely only on `slot.close_lock` while the active pointer and gate are managed elsewhere.

## B3. Use an event-driven wait for every publication race

Remove the remaining 10 ms polling fallback for ordinary publication races where possible.

Maintain a manager-level state-change event/condition that wakes on:

- initial generation install;
- swap commit;
- swap rollback;
- shutdown.

A blocked acquire must wait on a bounded event rather than loop with `asyncio.sleep(0.01)`.

## B4. Preserve in-flight old leases

At commit:

- set old slot `accepting_leases=False`;
- do not invalidate or decrement existing leases;
- move the old slot to retirement eligibility only after commit;
- allow existing leases to release normally.

The retirement task must observe the exact lease count after old-slot admission closes.

## B5. Add deterministic race tests

Create a barrier-capable test hook or use controlled locks/events to prove these cases with distinct generation IDs.

### Required case 1 — acquire misses initial gate snapshot

1. request starts acquire and pauses after reading pre-swap state;
2. reload stages and commits generation N+1;
3. request resumes;
4. request must not return a lease for generation N;
5. request must retry and return N+1.

### Required case 2 — acquire waits during staging

1. generation N active;
2. distinct generation N+1 staged;
3. acquire begins;
4. no lease completes before commit/rollback;
5. on commit, acquire returns N+1;
6. on rollback, acquire returns N.

### Required case 3 — old leases drain

1. acquire an N lease before staging;
2. commit N+1;
3. verify no new N lease can be obtained;
4. existing N lease remains usable;
5. retirement waits for that lease release.

Run the race test repeatedly, at least 500 iterations locally or through a deterministic barrier loop.

### Acceptance criteria — Workstream B

- [ ] Candidate activation, old-slot admission closure, and gate release are one locked operation.
- [ ] No acquire can return an old-generation lease after committed publication.
- [ ] No acquire can return a candidate lease before committed publication.
- [ ] Existing old-generation leases survive publication and drain normally.
- [ ] Publication-race waiting is event-driven; no unbounded or fixed-interval busy polling remains.
- [ ] Distinct-generation barrier tests cover commit and rollback.
- [ ] The old-generation race test passes for at least 500 deterministic iterations.
- [ ] A focused concurrency benchmark shows no material regression from the synchronization change.

---

# Workstream C — Make cancellation rollback-safe

## C1. Give the reload operation one cleanup owner

Track the following objects in outer-scope variables as soon as they are created:

- `pending_swap`;
- `transition_result`;
- `candidate`;
- whether SQLite outer commit completed;
- whether runtime swap committed;
- whether ownership transferred;
- whether retirement/finalization was scheduled.

Create one cleanup helper that is used by both `Exception` and `CancelledError` paths.

Suggested shape:

```python
async def _abort_precommit_reload(
    *,
    pending_swap,
    transition_result,
    candidate,
    cause,
) -> AbortDiagnostics:
    ...
```

The helper must be idempotent and safe under cancellation shielding.

## C2. Catch cancellation around the staged transaction

The inner commit protocol must handle `BaseException` selectively or use explicit `except asyncio.CancelledError` plus `except Exception`.

On cancellation before runtime swap commit:

1. shield rollback of applied transitions;
2. shield rollback of the pending swap;
3. allow/force SQLite transaction rollback;
4. shield candidate abort;
5. verify the lease gate reopened;
6. re-raise cancellation.

Do not let `CancelledError` bypass pending-swap rollback.

## C3. Distinguish cancellation before and after commit

### Before runtime/persistence commit

Cancellation outcome:

- old generation remains active;
- SQLite old state remains active;
- transitions restored;
- candidate aborted;
- no retirement scheduled;
- result/event classified `cancelled_precommit`.

### After runtime/persistence commit

Cancellation outcome:

- do not roll back process transitions;
- do not restore old runtime;
- shield post-commit finalization to a bounded completion point;
- if finalization cannot finish, persist a pending-finalization record;
- classify `cancelled_postcommit_finalization_pending` or completed success, depending on final state.

## C4. Handle cancellation at every boundary

Add deterministic cancellation injection points:

- after pending swap preparation;
- after stage/gate close;
- after first process transition applies;
- immediately before SQLite outer commit;
- immediately after SQLite outer commit;
- immediately after runtime swap commit;
- during compatibility mirror;
- during retirement scheduling.

Use events rather than sleeps.

## C5. Verify no stuck gate

Every precommit cancellation test must assert:

- `lease_admission_gated is False`;
- no pending staged swap remains;
- a new lease can be acquired immediately from the old generation;
- retirement task count is unchanged;
- candidate cleanup completed or produced explicit cleanup diagnostics.

### Acceptance criteria — Workstream C

- [ ] `CancelledError` after staging cannot bypass swap rollback.
- [ ] Precommit cancellation reopens lease admission before propagating.
- [ ] Post-commit cancellation never attempts runtime or process-transition rollback.
- [ ] Every cancellation boundary has a deterministic test.
- [ ] Cancellation tests prove no pending swap, stuck gate, leaked candidate, or premature retirement remains.
- [ ] Cancellation diagnostics distinguish precommit abort from post-commit finalization.

---

# Workstream D — Preserve and roll back partially applied transitions

## D1. Never lose `TransitionApplyResult`

The caller must own the result object before applying transitions:

```python
transition_result = TransitionApplyResult(plan)
await transition_result.apply_all()
```

Do not create the result inside a helper that raises before returning it.

Either:

- make `_apply_process_transitions(result)` accept an existing result; or
- return a typed exception containing the result/applied stack.

The first option is simpler.

## D2. Add a typed transition apply error

Introduce a typed error carrying:

- failed transition name;
- failed transition index;
- applied transition names;
- original exception class/message;
- reference or immutable snapshot of the rollback owner.

Suggested type:

```python
class ProcessTransitionApplyError(ReloadCommitError):
    ...
```

Do not classify every runtime error in commit as a process-transition failure based only on transaction state.

## D3. Propagate rollback failures

Concrete transition `rollback()` methods must not swallow exceptions.

Change these patterns:

```python
try:
    ...
except Exception:
    logger.warning(...)
```

into exception propagation with context. `TransitionApplyResult.rollback_applied()` is the aggregation layer.

Required behavior:

- continue rollback after one failure;
- collect every failure;
- preserve transition name and exception class;
- mark the transition result as not fully restored;
- expose structured degraded state.

## D4. Make rollback idempotent

Track each transition rollback state so repeated cleanup does not reapply old state unpredictably.

A transition should expose or internally track:

- not applied;
- applied;
- rolled back;
- finalized.

A second rollback after successful rollback is a no-op.

A rollback after finalize is invalid and must fail deterministically.

## D5. Correct preflight and transaction state ordering

The intended state sequence must be:

```text
PROCESS_TRANSITIONS_PREPARED
  -> PROCESS_TRANSITIONS_PREFLIGHTED
  -> COMMIT_STARTED
  -> RUNTIME_STAGED
  -> RUNTIME_SWAP_COMMITTED
  -> PROCESS_TRANSITIONS_APPLIED
  -> PERSISTENCE_COMMITTED
  -> OBSERVABLE_STATE_UPDATED
  -> RETIREMENT_SCHEDULED
  -> COMPLETED
```

Current code marks commit started before transition preflight and contains duplicate `PROCESS_TRANSITIONS_PREFLIGHTED` keys in `_VALID_TRANSITIONS`.

Fix the state map so every state appears once and method docstrings match production ordering.

Run all transition preflights before declaring `COMMIT_STARTED` and before opening SQLite/gating lease admission.

## D6. Add production-path partial failure tests

Use three transitions:

- transition A applies successfully;
- transition B applies successfully;
- transition C raises.

Assert through `ReloadManager.reload()`, not only `TransitionApplyResult` unit tests:

- rollback order is B then A;
- runtime remains generation N;
- persistence remains old state;
- lease gate reopens;
- candidate aborts;
- no retirement starts;
- diagnostic names C as apply failure.

Add a variant where B rollback raises and A rollback succeeds. Assert:

- both rollback attempts occur;
- failure is classified `compensation_failed` or equivalent degraded category;
- readiness/diagnostics reflect uncertainty;
- the original apply failure remains available as primary cause.

### Acceptance criteria — Workstream D

- [ ] A partial transition apply failure cannot lose its rollback stack.
- [ ] Production reload tests prove reverse rollback of an applied prefix.
- [ ] Concrete transitions propagate rollback errors to the aggregator.
- [ ] Rollback errors produce structured degraded-state diagnostics.
- [ ] Transition rollback/finalize lifecycle is idempotent and validated.
- [ ] The transaction state map contains no duplicate keys.
- [ ] Preflight occurs before `COMMIT_STARTED` and before lease gating.
- [ ] State transition docstrings and tests match the real production order.

---

# Workstream E — Separate irreversible commit from post-commit finalization

## E1. Define commit acceptance

A reload becomes irrevocably committed only after both are true:

- SQLite outer commit succeeded;
- runtime swap committed and candidate became leaseable.

Immediately record an explicit fact:

```python
commit_accepted = True
```

or equivalent transaction state.

After this fact, no code path may:

- roll back process transitions;
- abort transferred candidate resources;
- restore the old runtime generation;
- report a clean pre-publication abort.

## E2. Move process-transition finalization after commit acceptance

Applied transitions should remain in their new state after commit acceptance.

`finalize_all()` may release old snapshots and perform housekeeping. Its failure must:

- not call `rollback_applied()`;
- not mark persistence uncommitted;
- not classify publication as failed;
- create a retryable post-commit finalization record.

## E3. Make candidate ownership transfer explicit and retryable

Candidate ownership transfer must occur after commit acceptance.

If bookkeeping fails after the runtime already owns the active generation:

- retain a finalization owner referencing the candidate/resource registry;
- ensure candidate abort cannot close active resources;
- retry or reconcile transfer bookkeeping;
- expose `ownership_transfer_pending` diagnostics.

Prefer making transfer bookkeeping non-fallible once prepared.

## E4. Treat compatibility mirrors as non-authoritative

`app.state` mirroring happens after commit acceptance.

If mirroring fails:

- request path remains correct through generation leases;
- do not roll back transitions;
- store `mirror_update_pending`;
- retry a bounded number of times;
- expose a warning/degraded diagnostic if retry exhausts.

Mirror updates must be one coherent synchronous operation where possible.

## E5. Make retirement scheduling retryable

Use `PendingGenerationSwap.finalize_retirement()` or a typed finalization record.

If retirement task creation fails:

- retain the old slot;
- keep it non-accepting;
- do not close it synchronously on the commit path;
- record `retirement_scheduling_pending`;
- retry through a process-owned bounded mechanism;
- never lose the old slot reference.

## E6. Correct outer exception classification

The outer exception handler must recognize all committed states, including:

- `RUNTIME_SWAP_COMMITTED`;
- later post-commit states;
- an explicit `COMMIT_ACCEPTED` state if introduced.

Do not limit post-publication handling to legacy `RUNTIME_PUBLISHED`.

Use transaction facts rather than `txn.is_committing` for wire fields:

- `publication_occurred=txn.publication_occurred`;
- `persistence_committed=txn.persistence_committed`;
- `process_transitions_applied=txn.process_transitions_applied`.

## E7. Add post-commit failure tests

Inject failure at:

- candidate ownership transfer;
- compatibility mirror update;
- transition finalize;
- retirement task creation;
- operational event recording.

For each case assert:

- candidate generation remains active;
- SQLite remains candidate state;
- process transitions remain candidate state;
- candidate active resources are not aborted;
- no old-generation lease can be newly acquired;
- result is committed-with-finalization-pending or committed-with-warning, not precommit failure;
- retry/finalization diagnostics are populated.

### Acceptance criteria — Workstream E

- [ ] The code has an explicit irreversible commit-accepted boundary.
- [ ] No post-commit failure invokes transition rollback or candidate abort.
- [ ] `RUNTIME_SWAP_COMMITTED` is treated as a committed state by cancellation and exception handlers.
- [ ] Ownership, mirror, and retirement failures use retry/finalization semantics.
- [ ] Post-commit failure tests prove runtime, SQLite, and transitions remain aligned.
- [ ] Wire diagnostics derive from explicit transaction facts, not broad state categories.

---

# Workstream F — Add a true SQLite outer-commit failure seam

## F1. Inject at `Database.transaction()` commit

The current `TEST_INJECT_PUBLISH_FAILURE` seam runs before SQLite outer commit. Retain it for staged publication failure testing, but do not call it a SQLite commit failure.

Add a deterministic database-layer test seam that raises from the outermost transaction commit path after the transaction body succeeds.

Possible designs:

- injectable commit callback/hook on the test database instance;
- test-only connection wrapper whose `commit()` raises once;
- explicit `TEST_INJECT_OUTER_COMMIT_FAILURE` hook in `Database.transaction()` guarded for tests.

Requirements:

- nested transaction bodies do not trigger the hook;
- the hook fires only at the outer commit boundary;
- rollback is attempted after failure;
- the hook is one-shot and reset safely;
- production default has zero behavior change.

## F2. Prove the body completed before failure

The test must prove:

- persistence delta SQL ran;
- pending swap reached `STAGED`;
- process transitions applied;
- SQLite outer `COMMIT` then raised;
- cleanup restored every layer.

Use observer events or explicit test hooks rather than inferring from final state alone.

## F3. Required commit-failure assertions

Capture pre- and post-snapshots and assert equality for:

- active generation ID and digest;
- service identities;
- provider/account rows;
- process transition targets;
- compatibility mirrors;
- lease counts;
- retirement task count;
- pending swap diagnostics;
- lease gate state;
- candidate ownership/resource closure counts.

Also assert:

- no candidate request executed;
- candidate cleanup ran exactly once;
- failure category is `persistence_commit_failed`;
- `publication_occurred is False`;
- `persistence_committed is False`.

## F4. Rename misleading tests

Rename or rewrite `tests/integration/reload/test_sqlite_commit_failure.py` so its file name and test docstrings accurately describe the injected boundary.

Keep separate tests for:

- failure after runtime stage but before outer commit;
- actual SQLite outer commit failure.

### Acceptance criteria — Workstream F

- [ ] A deterministic seam raises from the actual outer SQLite commit operation.
- [ ] The test proves SQL, staging, and transition apply occurred before commit failure.
- [ ] Commit failure restores runtime, SQLite, transitions, mirrors, ownership, and retirement state.
- [ ] Candidate resources close exactly once.
- [ ] No candidate lease or request is observed.
- [ ] The result category is specifically persistence/SQLite commit failure.
- [ ] Test file names and docstrings accurately describe their fault boundary.

---

# Workstream G — Make peer credential validation fail closed

## G1. Return an explicit authorization result

Change `_reject_unmatched_peer_uid()` into a function with an explicit contract, for example:

```python
class PeerCredentialResult(Enum):
    ALLOWED = "allowed"
    UNSUPPORTED = "unsupported"
    REJECTED = "rejected"
```

or raise a typed `ControlPeerAuthenticationError`.

On Linux where `SO_PEERCRED` is available:

- missing socket object: reject;
- short credential buffer: reject;
- `getsockopt` error: reject;
- unpack error: reject;
- UID mismatch: reject;
- matching UID: allow.

On platforms without `SO_PEERCRED`, return `UNSUPPORTED` and rely on socket filesystem permissions as documented.

## G2. Terminate before reading request bytes

`ControlServer._handle_connection()` must exit immediately when peer validation rejects.

The reload handler must never be invoked for a rejected peer.

Do not close the writer and then continue into `reader.readline()`.

## G3. Avoid writing an application response to an unauthorized peer

For a credential rejection:

- close the connection;
- optionally log a sanitized audit event;
- do not parse JSON;
- do not invoke the reload handler;
- do not attempt a normal control response after writer close.

## G4. Add unit tests with fake sockets

Test:

- matching UID buffer;
- mismatched UID buffer;
- short buffer;
- `getsockopt` raises;
- no socket extra info;
- platform without `SO_PEERCRED`;
- handler not called on rejection.

Use `struct.pack("3i", pid, uid, gid)` for exact Linux layout expected by the implementation.

### Acceptance criteria — Workstream G

- [ ] Linux credential read/decode failures reject rather than allow.
- [ ] UID mismatch exits the connection handler before reading or parsing a request.
- [ ] The reload handler is never called for a rejected peer.
- [ ] Unsupported platforms retain the documented filesystem-permission fallback.
- [ ] Tests cover every allowed, rejected, error, and unsupported branch.

---

# Workstream H — Correct diagnostics and transaction facts

## H1. Populate committed generation facts

`mark_runtime_swap_committed()` must record:

- candidate/new generation ID;
- `active_generation_after`;
- publication duration;
- old generation ID;
- `publication_occurred=True`;
- runtime swap commit timestamp.

Pass the published generation or its typed metadata into the method rather than only the old generation ID.

## H2. Separate staged, committed, and finalization-pending diagnostics

Runtime diagnostics should expose mutually coherent states:

- `pending_swap_state`;
- `pending_swap_generation_id`;
- `pending_swap_old_generation_id`;
- `lease_admission_gated`;
- `post_commit_finalization_pending`;
- `ownership_transfer_pending`;
- `mirror_update_pending`;
- `retirement_scheduling_pending`;
- `last_pending_swap_outcome`.

A committed/finalized or rolled-back swap must not appear as actively staged.

## H3. Track real waiter count or remove it

Do not return a hardcoded `lease_gate_waiter_count=0`.

Either:

- increment/decrement a waiter counter around gate waits; or
- remove the field until correctly implemented.

A misleading metric is worse than no metric.

## H4. Fix abort-state publication facts

`mark_aborting()` must recognize:

- `RUNTIME_STAGED` as publication attempted but not occurred;
- `RUNTIME_SWAP_COMMITTED` as publication occurred;
- all later states as publication occurred.

Diagnostics must not infer `publication_occurred` from `is_committing`.

## H5. Add invariant assertions to diagnostic tests

For every terminal result:

```text
publication_occurred == True
    => active_generation_after == result generation

persistence_committed == True
    => publication_occurred == True

retirement_scheduled == True
    => publication_occurred == True

lease_admission_gated == True
    => pending_swap_state == STAGED
```

Add result-category tests for:

- preflight failure;
- stage failure;
- transition apply failure;
- SQLite commit failure;
- cancellation before commit;
- post-commit finalization pending;
- full success.

### Acceptance criteria — Workstream H

- [ ] Committed swap diagnostics include the new active generation ID.
- [ ] Terminal swaps do not remain reported as staged/pending.
- [ ] Waiter count is real or removed.
- [ ] Abort and cancellation output uses explicit publication facts.
- [ ] Diagnostic invariant tests cover every terminal outcome class.

---

# Workstream I — Strict verification and closure evidence

## I1. Remove remaining reload-related exemptions

Close and remove from `scripts/audit_xfail_skips.py`:

- concurrent operator-workflow reload serialization xfail;
- drain-timeout unconditional skip.

If subprocess testing cannot reliably hit the admission guard, replace it with a deterministic in-process or control-server integration test. Do not retain a roadmap acceptance item as non-strict because the original harness is inconvenient.

For drain timeout:

- use a short injected timeout;
- hold a lease deliberately;
- prove forced close/degraded retirement diagnostics;
- keep wall-clock duration bounded for CI.

## I2. Add a focused corrective closure test command

Add a documented command that includes at minimum:

```bash
uv run pytest \
  tests/unit/test_runtime_manager.py \
  tests/unit/test_process_transition_plan.py \
  tests/unit/test_control_server.py \
  tests/integration/reload/test_pending_swap_visibility.py \
  tests/integration/reload/test_sqlite_commit_failure.py \
  tests/integration/reload/test_reload_fault_matrix.py \
  -v --tb=short
```

Adjust file names if the commit-failure tests are split/renamed.

## I3. Add a focused CI job or explicit step

The exact Plan 016 tests must run on Python 3.11 and 3.12.

Acceptable options:

- a separate `reload-atomicity-closure` matrix job; or
- a clearly named step in `reload-control` that invokes the focused files before the general reload suite.

Requirements:

- per-job timeout;
- `faulthandler_timeout` or `pytest-timeout` diagnostics;
- artifact upload on failure;
- no duplicate execution of the same tests across jobs without a documented reason.

## I4. Run full repository gates

Required commands:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
uv run pytest
uv run python scripts/audit_xfail_skips.py
```

Additionally run:

```bash
uv run pytest tests/integration/reload/ -v --tb=short
uv run pytest tests/unit/test_control_server.py -v --tb=short
uv run pytest tests/unit/test_process_transition_plan.py -v --tb=short
```

## I5. Capture exact-head evidence

The implementation handoff must record:

- exact commit SHA;
- commands run locally;
- pass/fail counts;
- Python versions;
- GitHub Actions run URL or run ID;
- status of every required matrix job;
- confirmation that the run tested the exact commit SHA.

Do not claim closure from an unverified local-only run or from CI attached to an earlier commit.

### Acceptance criteria — Workstream I

- [ ] No reload-correctness xfail or unconditional skip remains in the allowlist.
- [ ] Concurrent admission and drain timeout are strict deterministic tests.
- [ ] Focused Plan 016 tests run on Python 3.11 and 3.12 in CI.
- [ ] Full repository lint, type, test, and exemption audits pass.
- [ ] CI evidence references the exact implementation SHA.
- [ ] No closure claim is made while required CI jobs are absent, pending, cancelled, or failing.

---

# Implementation sequence

Follow this order to avoid testing an unstable protocol.

## Milestone 1 — Synchronization and lease linearization

Implement Workstreams A and B first.

Deliverables:

- locked pending-swap lifecycle;
- one-pending-swap enforcement;
- atomic commit operation;
- corrected acquire validation;
- deterministic lease-race tests.

Exit gate:

- no old-generation lease after commit in 500 deterministic race iterations;
- no candidate lease before commit;
- runtime-manager unit suite green.

## Milestone 2 — Cancellation and transition rollback

Implement Workstreams C and D.

Deliverables:

- shared precommit cleanup owner;
- cancellation rollback at every boundary;
- retained partial transition stack;
- propagated rollback failures;
- corrected state ordering.

Exit gate:

- every cancellation boundary leaves no stuck gate or staged swap;
- production partial-transition tests pass;
- rollback-degraded outcome is observable.

## Milestone 3 — Commit acceptance and finalization

Implement Workstream E.

Deliverables:

- explicit irreversible boundary;
- post-commit finalization records/retries;
- no post-commit transition rollback;
- correct committed failure classification.

Exit gate:

- injected ownership, mirror, finalize, and retirement failures preserve aligned committed state;
- active resources are never candidate-aborted after commit.

## Milestone 4 — True commit failure and control security

Implement Workstreams F and G.

Deliverables:

- outer SQLite commit failure seam;
- complete rollback proof;
- fail-closed peer credentials;
- handler non-invocation tests.

Exit gate:

- actual outer commit failure test passes;
- all credential branches have strict unit tests.

## Milestone 5 — Diagnostics and strict closure

Implement Workstreams H and I.

Deliverables:

- coherent transaction/runtime diagnostics;
- removal of remaining exemptions;
- focused CI gate;
- exact-head verification evidence.

Exit gate:

- all explicit acceptance criteria in this file pass;
- exact implementation SHA is green in required CI jobs.

---

# Required test matrix

## Runtime-manager unit tests

Cover:

- one pending swap at a time;
- legal swap lifecycle;
- illegal conflicting lifecycle calls;
- stage under shutdown;
- active generation changed before stage;
- commit atomically closes old admission and opens candidate admission;
- rollback restores old admission;
- pending-swap clearing;
- event waiter wakeup;
- acquire race against stage/commit;
- old lease drainage.

## Reload-manager unit tests

Cover:

- preflight before commit started;
- partial transition apply failure;
- rollback failure aggregation;
- cancellation at each boundary;
- post-commit housekeeping failure;
- correct fact-based wire output;
- no candidate abort after committed publication.

## Reload integration tests

Cover:

- actual SQLite outer commit failure;
- candidate visibility under gate;
- old-generation race barrier;
- persistence/runtime/transition snapshot equality after failure;
- no resource leak;
- no retirement on precommit failure;
- retryable retirement scheduling after commit;
- concurrent control reload serialization;
- bounded drain timeout.

## Control-server tests

Cover:

- same UID allowed;
- wrong UID rejected;
- short peer credential buffer rejected;
- credential syscall error rejected;
- unsupported platform fallback;
- rejected peer never invokes handler;
- existing stale-socket and permission tests remain green.

## Stress and boundedness tests

Run at least:

- 500 deterministic acquire/publication race iterations;
- 100 cancellation iterations distributed across injection boundaries;
- 250 alternating failed/successful reloads while checking resource plateau;
- repeated retirement timeout test with bounded execution.

The stress tests may be marked slow/soak where appropriate, but at least one bounded representative of every correctness behavior must run in normal CI.

---

# Closure checklist

Plan 016 is complete only when all items below are true.

## Synchronization

- [ ] All swap state mutation is enforced under `RuntimeManager._lock`.
- [ ] There is at most one unresolved pending swap.
- [ ] Candidate activation, old admission closure, and gate release are atomic.
- [ ] `ReloadManager` does not manipulate private old-slot fields.

## Lease correctness

- [ ] No new old-generation lease is possible after commit.
- [ ] No candidate lease is possible before commit.
- [ ] Existing old leases drain without interruption.
- [ ] Waits are event-driven and bounded.

## Cancellation

- [ ] Cancellation after staging rolls back the swap and transitions.
- [ ] Cancellation never leaves the gate closed.
- [ ] Post-commit cancellation completes or records finalization pending.
- [ ] Candidate resources are not leaked or double-closed.

## Process transitions

- [ ] Partial apply retains a rollback owner.
- [ ] Rollback runs in reverse order.
- [ ] Concrete rollback errors propagate to the aggregator.
- [ ] Rollback failure produces degraded-state diagnostics.
- [ ] Preflight occurs before commit state and lease gate.
- [ ] The transaction transition map has no duplicate state keys.

## Commit and finalization

- [ ] SQLite plus runtime publication has one explicit commit-accepted boundary.
- [ ] Post-commit failures never roll back process state.
- [ ] Ownership, mirrors, and retirement have bounded retry/finalization handling.
- [ ] All committed states are recognized by exception and cancellation paths.

## Fault injection

- [ ] An actual outer SQLite `COMMIT` failure is injected deterministically.
- [ ] The transaction body demonstrably completed before that failure.
- [ ] All cross-layer state returns to the pre-reload snapshot.
- [ ] No candidate request or lease is observed.

## Control security

- [ ] Linux peer credential failures reject before request parsing.
- [ ] Wrong UID cannot invoke the handler.
- [ ] Unsupported-platform behavior remains documented and tested.

## Diagnostics

- [ ] Committed transactions record the new active generation.
- [ ] Rolled-back/finalized swaps are not reported as staged.
- [ ] Waiter metrics are accurate or removed.
- [ ] Terminal wire results use explicit facts.

## Verification

- [ ] Remaining reload-related skip and xfail exemptions are removed.
- [ ] Focused Plan 016 CI runs on Python 3.11 and 3.12.
- [ ] Full repository gates pass.
- [ ] Exact-head GitHub Actions evidence is recorded.
- [ ] No unresolved P0/P1 item from this plan remains documented as future work.

---

# Handoff expectations

The implementing agent should deliver:

1. production code changes;
2. deterministic unit and integration tests;
3. any narrowly required test hooks, disabled by default;
4. updated architecture/development documentation where contracts changed;
5. removal of obsolete xfail/skip allowlist entries;
6. focused and full verification output;
7. a concise completion note mapping every acceptance criterion to evidence.

Do not mark this plan complete based only on the existence of the new abstractions or passing happy-path tests. Closure requires failure-boundary, cancellation, race, and exact-head CI evidence.