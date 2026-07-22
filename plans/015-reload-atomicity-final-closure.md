# Reload Atomicity Final Closure Plan

Date: 2026-07-22
Status: implementation handoff
Depends on: `plans/014-reload-correctness-corrective-pass.md`
Supersedes: any Plan 014 closure claim that does not satisfy the acceptance criteria in this file.

## Objective

Close the remaining reload correctness, control-plane hardening, and verification gaps after the first Plan 014 implementation passes.

The repository now has most of the correct building blocks:

- atomic reload admission;
- generation leases and asynchronous retirement;
- explicit candidate ownership;
- a shared runtime-generation factory;
- typed process transitions;
- generation-coherent proxy request handling;
- structured reload diagnostics;
- strict control request parsing;
- XDG runtime/state path separation;
- dedicated reload CI coverage;
- offline consistency auditing.

The remaining work is narrower but production-critical. The current commit sequence can still expose a candidate runtime before SQLite commit succeeds. If SQLite `COMMIT` then fails, the runtime can remain on the candidate while persistence rolls back to the old provider/account state. The old generation may also already be retiring, candidate ownership may have transferred, and transaction diagnostics may still claim publication did not occur.

This pass must establish one explicit reload linearization protocol and prove it under commit failure, cancellation, transition failure, and concurrent request pressure.

## Required end state

A completed implementation must guarantee all of the following:

1. No request can acquire the candidate generation until its persistence transaction and required process transitions have succeeded.
2. A SQLite commit failure after an in-memory candidate stage restores the complete old runtime state before lease admission resumes.
3. The old generation cannot begin retirement until the candidate commit is irrevocably accepted.
4. Candidate ownership cannot transfer permanently before the reload commit is accepted.
5. Transaction diagnostics record the actual runtime state at every boundary; they do not infer publication from broad state categories.
6. Every process transition runs through explicit preflight, apply, rollback, and finalize handling.
7. A process-transition plan with no task specs still applies independent writer, guard, and effective-state transitions.
8. Rollback executes in reverse application order and exposes rollback failures as structured degraded state.
9. Control peer credential validation works on Linux and rejects mismatched UIDs before request processing.
10. Stale-socket cleanup unlinks only a socket that is positively identified as stale.
11. The XDG isolation test and roadmap-relevant concurrency tests are strict tests, not skips or non-strict xfails.
12. CI is green on Python 3.11 and 3.12 with bounded per-test diagnostics and no duplicated reload suite.

## Production blockers being closed

### P0 — Runtime-ahead-of-persistence split

The current flow applies provider/account SQL inside `db.transaction()`, calls runtime publication, transfers ownership and mirrors generation state, then exits the database context. SQLite commits only when the context exits.

If the final commit raises after the active runtime pointer changed:

- the candidate can be active;
- the old generation can be scheduled for retirement;
- candidate resources can be transferred;
- `app.state` can point to candidate state;
- SQLite can roll back to the old rows;
- `ReloadTransaction.publication_occurred` can remain false because it is marked after the database context exits.

This is not an acceptable compensating model. The commit protocol must prevent candidate lease visibility until the database commit and must preserve a rollback path until acceptance.

### P0 — Retirement begins too early

`RuntimeManager.install_candidate()` currently changes the active pointer and spawns retirement after leaving its lock. That API is too coarse for a cross-layer transaction because it combines:

- pointer replacement;
- candidate lease eligibility;
- old-generation retirement eligibility;
- retirement task creation.

These operations require separate boundaries.

### P1 — Transition lifecycle is incomplete

The current transition plan defines preflight and rollback methods, but production wiring does not consistently execute them. `_apply_process_transitions()` also returns early when task specs are absent, which can skip unrelated transitions.

### P1 — Publication facts are recorded after the real event

The transaction records `publication_occurred` only after `_publish_generation()` and SQLite commit return. Failure between the pointer assignment and that mark produces incorrect classification and cleanup behavior.

### P1 — Linux peer credential check is not reliable

`SO_PEERCRED` must request and decode the complete `struct ucred` buffer. Closing the writer without returning or raising also allows the connection handler to continue processing.

### P1 — Stale socket cleanup is too permissive

Only positive stale signals should authorize unlinking. Permission errors, timeouts, malformed filesystem entries, and ambiguous connection failures must fail closed.

### P1 — Closure exemptions remain

Roadmap-relevant xfails/skips remain allowlisted, including concurrent rehash subprocess coverage, drain timeout behavior, and XDG isolation. The XDG skip rationale is obsolete after path changes.

---

# Workstream A — Introduce a lease-gated staged swap protocol

## A1. Replace the coarse publication API

Do not use `RuntimeManager.install_candidate()` as the transactional primitive for reload.

Introduce an explicit pending swap object owned by `RuntimeManager`. Suggested shape:

```python
pending = await runtime_manager.prepare_candidate_swap(
    generation=candidate_generation,
    expected_active_generation_id=old_generation_id,
    drain_timeout_s=drain_timeout_s,
)

await pending.stage()
await pending.commit()
# or
await pending.rollback()
```

Names may differ, but responsibilities must be explicit.

### `prepare_candidate_swap()`

Must:

- validate shutdown state;
- validate the expected active generation ID and digest when supplied;
- construct the candidate slot;
- capture the exact old slot identity;
- allocate any non-fallible metadata needed for later retirement;
- return a single-use pending swap owner;
- perform no active pointer mutation;
- leave lease admission unchanged;
- spawn no retirement task;
- transfer no candidate ownership.

### `PendingGenerationSwap.stage()`

Must run under the runtime-manager lock and:

- close or gate new generation lease admission;
- retain existing leases on the old slot;
- place the candidate in a staged, non-leaseable state;
- preserve the old slot and all rollback metadata;
- record that the pointer staging operation occurred;
- spawn no retirement task;
- avoid closing any resource;
- be idempotent or fail deterministically on repeated calls.

Two acceptable internal representations:

1. The candidate becomes the internal active pointer but remains non-accepting while a separate admission gate blocks `acquire()`.
2. The old active pointer remains externally authoritative and the candidate is held in a separate staged slot until commit.

The second design is simpler if the final pointer assignment can be made non-fallible after all preconditions succeed. The first design is acceptable only if rollback restores the exact old slot before lease admission reopens.

### `PendingGenerationSwap.commit()`

Must be deliberately minimal and non-fallible after preflight. It must:

- make the candidate the authoritative active slot if not already staged there;
- mark the candidate accepting;
- mark the old slot retiring;
- release the lease-admission gate;
- transfer rollback responsibility away from the pending swap;
- return the old generation ID for retirement finalization.

No database access, configuration parsing, resource construction, logging serialization, `app.state` traversal, or arbitrary callback should run inside this critical method.

### `PendingGenerationSwap.rollback()`

Must:

- restore the old active slot exactly;
- restore old lease admission;
- remove the staged candidate slot;
- guarantee that no candidate lease was issued;
- leave the old slot non-retiring;
- leave retirement task count unchanged;
- be idempotent;
- return the candidate to its candidate owner for abort.

### `PendingGenerationSwap.finalize_retirement()`

Must run only after the reload transaction is accepted. It may:

- schedule the old generation retirement task;
- transfer candidate resources to the runtime manager;
- finalize swap-owned metadata.

Retirement scheduling failure must not roll back an already visible committed generation. Instead, it must leave a tracked degraded retirement record and retry through a bounded process-owned mechanism.

## A2. Add an explicit lease-admission gate

`RuntimeManager.acquire()` must distinguish:

- no active generation;
- shutdown;
- a very short transaction gate during staged publication;
- permanent lease exhaustion.

During a staged swap, new requests should wait on an event or condition rather than polling every 10 ms. Requirements:

- bounded wait using the existing lease acquisition deadline;
- event-driven wakeup on commit or rollback;
- no busy loop;
- no lease granted from the staged candidate before commit;
- no lease granted from an old slot after the swap commit;
- existing old-generation leases continue normally;
- shutdown wakes blocked acquisitions and returns the existing controlled 503 path.

Add diagnostics:

- `lease_admission_gated`;
- `pending_swap_generation_id`;
- `pending_swap_old_generation_id`;
- `pending_swap_started_at`;
- `lease_gate_waiter_count`;
- `last_pending_swap_outcome`.

Do not expose secrets or complete config payloads.

## A3. Preserve request throughput outside the commit window

The gate must be held only for the narrow sequence that requires rollback safety. Candidate construction, config validation, semantic diffing, persistence-delta preparation, and transition preflight remain outside the gate.

Target gate sequence:

1. enter SQLite transaction;
2. apply prepared provider/account delta;
3. stage pending runtime swap and gate lease admission;
4. apply required reversible process transitions;
5. commit SQLite;
6. commit pending runtime swap and reopen admission;
7. apply compatibility mirrors that are not request-authoritative;
8. transfer candidate ownership;
9. schedule retirement.

If the project chooses to commit the runtime swap immediately before SQLite commit, the candidate must remain non-leaseable and rollbackable until the database commit succeeds.

## A4. Define the linearization point precisely

Document and encode two facts rather than one ambiguous publication flag:

- `runtime_staged`: candidate pointer/slot staging occurred but is not request-visible;
- `publication_committed`: candidate is active and leaseable, and SQLite commit succeeded.

Keep `publication_occurred` only as a compatibility field if required. If retained, define it unambiguously and derive it from one of the two facts.

Recommended transaction facts:

- `swap_prepared`;
- `lease_gate_closed`;
- `runtime_staged`;
- `persistence_commit_attempted`;
- `persistence_committed`;
- `runtime_swap_committed`;
- `lease_gate_reopened`;
- `process_transitions_preflighted`;
- `process_transitions_applied`;
- `process_transitions_rolled_back`;
- `candidate_ownership_transferred`;
- `effective_state_updated`;
- `retirement_scheduled`.

Every fact must be set immediately after the corresponding real operation, not after a larger wrapper returns.

## A5. Required failure behavior

### Failure before staging

- candidate aborts;
- SQLite is unchanged;
- runtime is unchanged;
- process transitions are unchanged;
- lease gate was never closed.

### Failure after staging but before SQLite commit

- rollback applied process transitions in reverse order;
- rollback the pending runtime swap;
- allow SQLite context to roll back;
- reopen lease admission on the old slot;
- abort candidate resources;
- no old-generation retirement task exists;
- classify as pre-publication commit failure.

### SQLite commit failure

This is the primary acceptance case:

- detect the commit exception;
- rollback the staged swap before admission reopens;
- rollback process transitions;
- verify SQLite returned to pre-state;
- verify the old generation remains active and accepting;
- verify candidate ownership did not transfer;
- verify no candidate request executed;
- expose a typed `persistence_commit_failed` result.

### Failure after committed publication

After candidate lease admission opens, do not pretend full rollback remains safe. Post-commit failures must use completion/retry semantics:

- compatibility mirror failure: retry boundedly; request path remains generation-coherent without the mirror;
- ownership-transfer bookkeeping failure: preserve a runtime-owned pending-finalization record and retry;
- retirement scheduling failure: retain old slot and retry scheduling; do not lose it;
- operational event persistence failure: log and retry independently; it must not change the committed reload result.

---

# Workstream B — Complete process-transition semantics

## B1. Run all transition preflights before the commit gate

`ProcessTransitionPlan.preflight_all()` should:

- invoke every transition in declared order;
- collect the exact transition that failed;
- mutate no process state;
- mark the plan preflighted exactly once;
- reject apply before successful preflight;
- produce structured diagnostics.

Preflight must be called after candidate and persistence-delta preparation, but before opening the SQLite transaction or closing lease admission.

## B2. Remove task-spec-dependent early returns

`_apply_process_transitions()` must iterate over `plan.transitions` regardless of:

- whether `process_supervisor` exists;
- whether `plan.task_specs` is empty;
- whether the task-spec transition is present.

Each transition is responsible for its own no-op behavior.

Add a strict regression test with:

- no process supervisor;
- empty `task_specs`;
- a routing-trace writer transition;
- a routing-trace guard transition;
- an effective-state transition.

The independent transitions must still execute.

## B3. Track applied transitions

`ProcessTransitionPlan.apply_all()` must:

- apply transitions in order;
- append each successfully applied transition to an internal applied stack;
- stop at the first failure;
- report the failed transition name and exception class;
- never mark the whole plan applied if only a prefix completed.

## B4. Roll back in reverse order

`ProcessTransitionPlan.rollback_applied()` must:

- run only transitions that completed apply;
- execute in reverse order;
- continue attempting rollback after one rollback failure;
- aggregate rollback errors;
- be idempotent;
- expose whether the old process state was fully restored.

Do not merely log rollback failure and return success. A rollback failure must produce:

- `compensation_failed` or a more specific typed category;
- failed readiness when correctness-affecting state is uncertain;
- operator diagnostics naming transition classes, not secret values.

## B5. Add transition finalization

After successful publication:

- call `finalize()` on applied transitions;
- release captured old-state snapshots;
- ensure finalize is idempotent;
- treat finalize failure as post-commit housekeeping, not a reason to misclassify publication.

## B6. Correct effective-state handling

The proxy request path no longer requires generation-owned `app.state` values for correctness. Preserve that property.

Compatibility mirrors should update after committed publication and must include one coherent snapshot operation rather than partially assigning fields across multiple awaits.

`EffectiveStateTransition` should capture and restore all fields it changes. Do not restore only non-`None` old values; distinguish:

- attribute absent;
- attribute present with `None`;
- attribute present with a value.

Use an explicit sentinel per field.

## B7. Decide the routing-trace ownership boundary

The shared guard and writer are observational, but their configuration is process-visible. Choose and document one model:

### Preferred

- make routing-trace policy immutable and generation-owned;
- pass policy with each trace event;
- keep the writer itself process-owned only as a sink;
- eliminate global guard mutation during reload.

### Acceptable closure scope

- retain typed reversible transitions;
- apply them while lease admission is gated;
- prove rollback returns exact old mode/sample rate/thresholds;
- prove no request uses a candidate policy before committed publication.

Do not leave the singleton mutation model undocumented.

---

# Workstream C — Align transaction state, cancellation, and diagnostics

## C1. Expand transaction states

Recommended state sequence:

```text
created
validated
diffed
candidate_prepared
persistence_prepared
process_transitions_preflighted
commit_started
runtime_staged
process_transitions_applied
persistence_committed
runtime_swap_committed
effective_state_updated
ownership_transferred
retirement_scheduled
completed
```

Abort/compensation states:

```text
aborting
runtime_stage_rolled_back
process_transitions_rolled_back
aborted
post_commit_finalization_pending
compensation_failed
```

Exact names may differ, but state must distinguish staged, committed, and externally visible publication.

## C2. Fix cancellation policy

### Before `runtime_staged`

Cancellation may propagate after candidate abort and normal cleanup.

### After `runtime_staged` but before `runtime_swap_committed`

Cancellation must be shielded until one of these terminal outcomes occurs:

- old runtime restored and SQLite rolled back; or
- candidate publication committed and lease admission reopened.

Do not let cancellation strand the lease gate closed.

### After `runtime_swap_committed`

Cancellation must not reverse the active generation. Complete ownership transfer, effective-state mirror, and retirement scheduling through bounded shielding/retry records.

## C3. Derive result classification from facts

Result classification must not use `txn.is_committing` as a substitute for publication.

Add exact typed categories:

- `persistence_apply_failed`;
- `runtime_stage_failed`;
- `process_transition_apply_failed`;
- `persistence_commit_failed`;
- `runtime_stage_rollback_failed`;
- `process_transition_rollback_failed`;
- `publication_committed`;
- `post_commit_finalization_pending`;
- `retirement_schedule_failed`.

The active generation ID and persistence status included in diagnostics must be read after recovery/finalization, not copied from the intended candidate.

## C4. Readiness degradation

Readiness must fail closed if any of these are true:

- lease gate remains closed beyond the bounded commit deadline;
- a staged swap has no active recovery task;
- runtime/persistence consistency audit fails;
- process transition rollback failed;
- old-generation retirement ownership is lost;
- committed publication finalization has exceeded its retry deadline.

Expose a concise reason code and operation ID.

---

# Workstream D — Add the missing atomicity and transition tests

## D1. SQLite commit-failure injection

Add a deterministic database fault seam that raises on outer transaction commit after:

- provider/account SQL succeeded;
- the candidate was staged;
- required process transitions applied.

Do not simulate this by failing `_publish_generation()` before the pointer change.

The test must assert complete pre/post equality for:

- active generation ID and digest;
- active config values;
- lease acceptance state;
- persisted providers;
- persisted accounts and enabled/weight state;
- process task specs;
- routing-trace writer mode/sample rate;
- routing-trace guard thresholds;
- compatibility mirror fields;
- candidate ownership state;
- active and retiring slots;
- retirement task count;
- closeable resource counts;
- transaction facts and terminal category.

Also assert that a concurrent request blocked during the staged window resumes on the old generation after rollback.

## D2. Candidate visibility test

Use deterministic barriers:

1. pause after `runtime_staged`;
2. start multiple proxy request acquisitions;
3. assert they are waiting rather than receiving candidate leases;
4. release commit success;
5. assert all new requests receive the candidate generation.

Repeat with commit failure and assert all requests receive the old generation.

## D3. No-early-retirement test

Pause after staging and verify:

- old slot state remains rollbackable;
- no retirement task exists;
- old resources remain open;
- existing old leases remain valid.

After commit, verify retirement is scheduled exactly once.

## D4. Transition order and rollback test

Create three deterministic fake transitions and inject failure in the third.

Assert call order:

```text
preflight A
preflight B
preflight C
apply A
apply B
apply C -> failure
rollback B
rollback A
```

Assert finalize is not called.

On success assert:

```text
preflight A/B/C
apply A/B/C
finalize A/B/C
```

## D5. Empty task-spec plan regression

Add a production-wiring test that proves writer/guard/effective transitions apply even when task specs are empty and the process supervisor is absent.

## D6. Effective-state sentinel test

Verify rollback restores:

- a missing attribute to missing;
- an attribute whose old value was `None` to `None`;
- a normal value exactly.

## D7. Cancellation matrix

Inject cancellation at:

- before transaction open;
- after SQLite writes;
- after lease gate close;
- after runtime staging;
- during transition application;
- during SQLite commit;
- after persistence commit but before swap commit;
- after swap commit but before ownership transfer;
- before retirement scheduling.

Each test must assert a strict terminal state and that the lease gate is open at completion.

## D8. Repeated failure plateau

Run at least 250 alternating staged commit failures and successful reloads. After quiescence require:

- active generation count exactly one;
- retiring generation count zero;
- pending swap count zero;
- lease gate open;
- retirement task count zero;
- no positive FD/thread/task/client slope;
- writer queues drained;
- persistence and active config consistent.

---

# Workstream E — Finish control-socket correctness

## E1. Correct Linux `SO_PEERCRED`

Use the platform API correctly:

```python
size = struct.calcsize("3i")
raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, size)
pid, uid, gid = struct.unpack("3i", raw)
```

Requirements:

- if `SO_PEERCRED` is available and credential retrieval fails, fail closed unless a documented platform exception applies;
- if UID differs, close the connection and return immediately or raise a typed control-auth error;
- do not parse credentials after closing and continue into request handling;
- do not include peer-sensitive details in client responses;
- log PID/UID only at appropriate operational level;
- keep macOS behavior based on filesystem permissions because `SO_PEERCRED` is unavailable there.

Add Linux-only tests using a fake socket object for exact buffer length and unpack behavior. Add a handler test proving a rejected peer never invokes `_process_request()` or the reload handler.

## E2. Harden stale-socket classification

Only unlink automatically for positive stale conditions:

- path is a Unix socket and connect returns `ECONNREFUSED`;
- path disappeared during probe (`ENOENT`);
- a platform-specific stale code explicitly documented and tested.

Fail closed for:

- `EACCES`/`EPERM`;
- timeout;
- resource exhaustion;
- address/path errors;
- unknown `OSError`;
- regular files;
- symlinks unless ownership and target policy explicitly permit removal.

Do not unlink a symlink merely because it exists at the configured path.

## E3. Protect against pathname replacement races

Capture file identity before probing:

- `lstat()` device/inode/mode;
- probe the socket;
- `lstat()` again before unlink;
- unlink only if identity is unchanged and still a Unix socket.

Server shutdown should unlink only the socket it created. Store its bound path identity after bind and compare before removal so one process cannot remove a replacement socket created by another process.

## E4. Runtime directory permissions

Ensure the runtime directory is private:

- create with mode `0700` where EggPool owns it;
- verify ownership is current UID;
- verify it is not group/world writable unless it is a trusted externally managed XDG runtime directory with documented semantics;
- fail closed when the socket parent is unsafe.

Add tests for unsafe parent mode and mismatched ownership using mocks where real ownership changes require privilege.

---

# Workstream F — Remove closure exemptions and repair operator contracts

## F1. Enable the XDG isolation test

Remove the stale unconditional skip. Update the test to reflect the actual design:

- control sockets isolate through `XDG_RUNTIME_DIR`, not `XDG_STATE_HOME`;
- persistent databases/logs/config-derived state isolate through `XDG_STATE_HOME` or explicit overrides;
- run two server instances with distinct runtime and state homes;
- verify both sockets exist independently;
- verify commands reach the intended instance;
- verify stopping one instance does not unlink the other instance's socket.

## F2. Replace concurrent rehash xfails

The subprocess tests should not remain non-strict xfails merely because timing cannot reliably hit the admission guard.

Use one of:

- a test-only deterministic barrier exposed through an environment-gated hook;
- a control test server with an injected `ReloadObserver`;
- a lower-level integration test that exercises the real control server and real `ReloadManager` in one process.

The strict contract:

- exactly one request is admitted;
- competitors receive `busy` without queuing;
- no second candidate is built;
- total operation counters are correct.

Keep an end-to-end subprocess smoke test, but do not use nondeterministic timing as the sole correctness proof.

## F3. Replace the drain-timeout skip

Make drain timeout configurable to a short deterministic value in tests. Assert:

- old lease prevents normal close;
- timeout triggers documented forced-close behavior;
- retirement diagnostics record forced close;
- runtime manager does not leak the slot or task;
- new generation remains usable.

## F4. Strengthen retirement response assertions

A successful result may legitimately serialize after retirement completes, but the fields must be internally consistent:

- `retirement_pending == false` implies `retiring_generation_id` is absent;
- `retirement_pending == true` implies a valid old generation ID is present;
- the ID cannot equal the new active generation ID;
- runtime diagnostics agree at the observation point, allowing completion races through a documented snapshot strategy.

Do not replace a brittle assertion with no assertion.

## F5. Update the xfail/skip audit

Remove Plan 014 reload/control exemptions as they are closed. The audit should reject new exemptions in:

- `tests/integration/reload/`;
- control server/client tests;
- D3 acceptance/operator workflow tests;
- runtime manager publication tests.

Any remaining exemption must include owner, issue/plan reference, and expiry criterion.

---

# Workstream G — CI and operational closure

## G1. Add real per-test timeout support

A workflow job timeout prevents indefinite resource consumption but does not identify the hanging test cleanly. Use a supported per-test timeout mechanism for reload/control suites.

Preferred:

- add `pytest-timeout` as a dev dependency;
- use a generous default such as 120–300 seconds for normal tests;
- override only known soak/performance tests;
- enable thread/faulthandler dumps on timeout.

Do not use aggressive limits that create host-load flakes.

## G2. Keep suite partitioning exact

Maintain:

- unit/normal integration on Python 3.11 and 3.12;
- reload/control integration on Python 3.11 and 3.12;
- performance and soak on the documented primary interpreter;
- no duplicated reload directory execution.

Add a CI audit that enumerates collected node IDs per job selection and detects overlap between general and dedicated reload suites.

## G3. Add focused atomicity job

Create a small, high-signal job or command group containing:

- SQLite commit-failure test;
- staged lease visibility test;
- no-early-retirement test;
- transition reverse rollback test;
- cancellation matrix;
- control peer credential and stale socket tests.

This job should fail quickly before the longer suite if the core protocol regresses.

## G4. Integrate the consistency audit with test artifacts

The offline audit script is useful, but closure requires exercising it with real test-generated snapshots.

After successful and failed reload scenarios:

- emit active runtime snapshot;
- run `scripts/audit_reload_consistency.py` against the temporary SQLite database;
- require exit 0 after recovery/quiescence;
- preserve the compact audit JSON on failure.

Avoid requiring production secrets or raw config files in artifacts.

## G5. Verify current head, not only local commands

Closure evidence must include a GitHub Actions run for the exact final commit with:

- lint green;
- pyright green;
- Python 3.11 unit/integration green;
- Python 3.12 unit/integration green;
- reload/control matrix green;
- atomicity-focused job green;
- performance-contract job green;
- short soak/audit green.

A commit with no associated status contexts is not closure evidence.

---

# Implementation milestones

## Milestone 1 — Failing gates and transaction model

Deliver:

- deterministic SQLite commit-failure seam;
- failing runtime-ahead-of-persistence test;
- failing candidate lease visibility test;
- failing no-early-retirement test;
- transaction-state additions;
- pending swap interface and invariants documented.

Exit criteria:

- new tests fail against the current implementation for the intended reasons;
- no production behavior is changed except narrow test seams and diagnostics.

## Milestone 2 — Pending swap and lease gate

Deliver:

- pending swap owner;
- event-driven lease gate;
- stage/commit/rollback paths;
- separation of retirement scheduling;
- runtime diagnostics;
- shutdown handling for a pending swap.

Exit criteria:

- SQLite commit failure restores the old runtime before requests resume;
- candidate leases are impossible before commit;
- old retirement cannot start early.

## Milestone 3 — Transition lifecycle and transaction wiring

Deliver:

- `preflight_all()`;
- `apply_all()`;
- reverse `rollback_applied()`;
- `finalize_all()`;
- removal of the task-spec early return;
- complete effective-state sentinel restoration;
- corrected cancellation shielding and result classification.

Exit criteria:

- every transition failure point has a strict test;
- rollback failure produces degraded readiness and structured diagnostics;
- transaction facts match runtime and SQLite state.

## Milestone 4 — Control socket and exemption closure

Deliver:

- correct Linux peer credentials;
- fail-closed peer rejection;
- positive stale-socket classification;
- inode replacement protection;
- runtime-directory checks;
- strict XDG, concurrency, and drain-timeout tests;
- stronger retirement response contract.

Exit criteria:

- Plan 014-related xfail/skip allowlist entries are removed;
- real Unix socket tests pass on supported CI platforms.

## Milestone 5 — CI, soak, and final proof

Deliver:

- per-test timeout diagnostics;
- atomicity-focused CI selection;
- no suite overlap audit;
- repeated failure/success plateau test;
- real consistency-audit artifact integration;
- final closure report.

Exit criteria:

- exact final commit has a complete green CI run;
- no pending swap, task, resource, or persistence drift remains after soak;
- documentation matches implementation rather than intended design.

---

# Primary implementation seams

Expected production files:

- `src/eggpool/runtime_manager.py`
- `src/eggpool/control/reload_manager.py`
- `src/eggpool/reload_transaction.py`
- `src/eggpool/db/core.py` or the database transaction implementation
- `src/eggpool/api/proxy_request.py` only if lease-gate propagation requires adjustment
- `src/eggpool/control/server.py`
- `src/eggpool/runtime_paths.py`
- `src/eggpool/app.py`
- `src/eggpool/reload_diagnostics.py`
- `scripts/audit_reload_consistency.py`
- `scripts/audit_xfail_skips.py`
- `.github/workflows/ci.yml`
- `pyproject.toml` / lockfile if adding `pytest-timeout`

Expected tests:

- `tests/integration/reload/test_persistence_publication_split.py`
- `tests/integration/reload/test_reload_fault_matrix.py`
- `tests/integration/reload/test_request_overlap.py`
- new `tests/integration/reload/test_sqlite_commit_failure.py`
- new `tests/integration/reload/test_pending_swap_visibility.py`
- `tests/unit/test_published_swap_protocol.py`
- `tests/unit/test_runtime_manager.py`
- `tests/unit/test_reload_manager.py`
- new `tests/unit/test_process_transition_plan.py`
- `tests/unit/test_control_server.py`
- `tests/integration/test_rehash_d3_acceptance.py`
- `tests/integration/test_rehash_d3_operator_workflow.py`
- `tests/integration/test_rehash_d3_operator_workflow_closure.py`
- soak/resource tests under `tests/soak/`.

---

# Required commands and evidence

At minimum, implementation handoff should run:

```bash
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
uv run pyright src scripts

uv run pytest tests/unit/test_runtime_manager.py -v
uv run pytest tests/unit/test_published_swap_protocol.py -v
uv run pytest tests/unit/test_process_transition_plan.py -v
uv run pytest tests/unit/test_control_server.py -v

uv run pytest tests/integration/reload/test_persistence_publication_split.py -v
uv run pytest tests/integration/reload/test_sqlite_commit_failure.py -v
uv run pytest tests/integration/reload/test_pending_swap_visibility.py -v
uv run pytest tests/integration/reload/test_reload_fault_matrix.py -v
uv run pytest tests/integration/reload/test_request_overlap.py -v

uv run pytest tests/integration/test_rehash_d3_acceptance.py -v
uv run pytest tests/integration/test_rehash_d3_operator_workflow.py -v
uv run pytest tests/integration/test_rehash_d3_operator_workflow_closure.py -v

uv run python scripts/audit_xfail_skips.py
uv run pytest tests/unit/test_audit_reload_consistency.py -v
```

Then run the repository's complete partitioned CI commands on Python 3.11 and 3.12.

The handoff report must include:

- exact commit SHA;
- tests added and why they failed before the fix;
- final transaction sequence;
- lease-gate maximum observed duration;
- SQLite commit-failure recovery snapshot;
- transition rollback matrix;
- control socket platform coverage;
- xfail/skip audit result;
- short soak resource summary;
- GitHub Actions run IDs or links for the final commit.

---

# Performance constraints

Correctness is primary, but the fix must not turn normal dispatch into a serialized path.

Requirements:

- `RuntimeManager.acquire()` remains lock-free or near-lock-free outside a pending swap;
- no database check occurs on request acquisition;
- no polling loop remains for pending swap admission;
- pending swap gate duration is measured;
- reload commit p50/p95/p99 is reported before and after;
- dispatch overhead p50/p95/p99 under no reload remains statistically unchanged within existing regression tolerances;
- during reload, requests may wait for the bounded commit gate but must not receive mixed-generation state;
- Raspberry Pi/SBC deployment constraints remain part of benchmark interpretation.

Recommended gate target:

- normal successful reload gate p95 below 100 ms on the primary CI/server profile;
- no hard correctness dependence on that number;
- explicit warnings when gate duration exceeds a configurable operational threshold.

---

# Non-goals

- Do not replace SQLite with an external transactional service.
- Do not add a second process or distributed transaction coordinator.
- Do not remove generation leases or asynchronous retirement.
- Do not make every request acquire the runtime-manager global lock.
- Do not permit candidate requests before persistence acceptance to improve latency.
- Do not treat a future reload as compensation for a split state.
- Do not hide rollback failures behind log-only warnings.
- Do not weaken database durability or dispatch persistence.
- Do not broaden live reload to restart-required fields.
- Do not introduce Rust solely for this closure pass.
- Do not claim completion from documentation, test count, or a local green run without exact-head CI evidence.

---

# Definition of done

This line of work is complete only when all statements below are true.

1. A forced SQLite outer-commit failure after candidate staging leaves the old runtime active, accepting leases, and fully consistent with SQLite.
2. No request receives a candidate lease before the reload commit is accepted.
3. The old generation cannot enter retirement before accepted publication.
4. Candidate ownership transfers only after accepted publication.
5. A staged swap always ends in committed or rolled-back terminal state.
6. The lease gate is always reopened on success, failure, cancellation, and shutdown.
7. Transaction facts identify staging and committed visibility separately.
8. Diagnostics agree with the actual runtime-manager active generation after every injected failure.
9. All process transitions are preflighted.
10. Independent transitions run even when task specs are empty.
11. Partial transition application rolls back in reverse order.
12. Rollback failures fail readiness and surface structured operator diagnostics.
13. Effective-state rollback handles missing, `None`, and normal old values correctly.
14. Proxy requests remain generation-coherent throughout old/new overlap.
15. Linux peer credential validation decodes `struct ucred` correctly and rejects mismatched UIDs before dispatch.
16. Ambiguous stale-socket probe errors do not authorize unlinking.
17. Socket identity is revalidated before unlink.
18. Runtime directory and socket permissions are verified fail-closed.
19. XDG multi-instance isolation is a strict passing test.
20. Concurrent reload admission subprocess/control coverage is strict.
21. Forced drain timeout behavior is a strict passing test.
22. Retirement response fields are internally consistent.
23. Plan-related xfail/skip allowlist entries are removed.
24. Reload tests execute exactly once per intended CI interpreter.
25. Per-test timeout diagnostics identify hangs without relying only on a 30-minute job timeout.
26. The repeated failure/success plateau returns tasks, slots, clients, descriptors, and queues to baseline.
27. The consistency audit passes after every recovery scenario.
28. The exact final commit has complete green GitHub Actions evidence.
29. Architecture and operator documentation describe the implemented protocol accurately.
30. No known runtime/persistence split state is accepted as deferred repair.
