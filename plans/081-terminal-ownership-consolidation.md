# Plan 081 — Terminal Ownership Consolidation

Date: 2026-08-05
Status: complete
Parent roadmap: `plans/077-sbc-lifecycle-simplification-and-runtime-correctness-roadmap.md`
Depends on:

- `plans/078-runtime-invariant-and-request-boundary-corrections.md`
- `plans/080-generation-finalization-ownership-alignment.md`

Planning baseline: `cd8967799e6613f3a5965af8cd15ce3c5269aaa8`

## Purpose

Replace overlapping retained terminal-work implementations with one bounded generation-owned terminal owner.

At the planning baseline, request finalization is owned by `RequestFinalizationSupervisor`, while retryable failed-attempt cleanup and post-commit claim compensation are owned by a separate coordinator registry. `AttemptFinalizer`, `RequestFinalizer`, a compatibility finalization queue, direct no-supervisor fallbacks, and startup repair add further execution branches around the same durable/runtime obligations.

Plan 080 first makes generation lifetime safe. This plan then consolidates live retained ownership without changing provider retry policy, response-handoff semantics, or startup crash-repair SQL.

## Design target

Extend the existing generation-owned finalization supervisor into one narrowly scoped terminal-work supervisor. It must own exactly three live command kinds:

1. selected-request finalization;
2. retryable failed-attempt cleanup;
3. post-commit selection-claim compensation.

Do not build a generic command framework. The command union is closed and local to request lifecycle.

Startup crash reconciliation remains separate because it runs after process death and operates from durable rows rather than in-memory command progress.

## Governing decisions

1. Every live selected attempt obligation has one authoritative retained owner.
2. Registration precedes cancellation-sensitive work.
3. One durable identity and command kind identify one command.
4. Duplicate compatible submissions join; conflicting submissions fail closed.
5. Completed component progress is recorded before the next await.
6. Retry resumes only incomplete components.
7. No command can penalize a provider for local database, serialization, capacity, or invariant failures.
8. Capacity is global and bounded across all three command kinds.
9. Supervisor retry scheduling remains one bounded timer/heap, not one task per delayed retry.
10. Generation retirement references from Plan 080 apply uniformly to all accepted commands.
11. Startup repair is not invoked to reclaim live in-process work by age.
12. The coordinator orchestrates and submits; it no longer retains a parallel task registry.

## Workstream A — Define one closed terminal command union

### Identity

Use one immutable identity structure that contains only the durable and routing facts required by all command kinds. Preserve explicit:

- proxy request ID;
- database request ID when allocated;
- attempt ID;
- reservation ID when allocated;
- account ID/name;
- provider/model/protocol;
- attempt number.

A pre-publication compensation identity may have a subset of durable IDs. Represent absence explicitly; do not invent placeholder IDs.

### Command kind

Define a closed literal/enum:

```python
selected_request_finalization
failed_attempt_cleanup
claim_compensation
```

Each kind has one typed payload/progress type. Prefer a frozen submission object plus a mutable progress record.

Do not expose `Any` as the authoritative payload type inside the supervisor. Compatibility edges may adapt into the typed union before registration.

### Keying

Use a key that prevents accidental collision between sequential phases for the same attempt. Acceptable forms:

- `(proxy_request_id, attempt_id, command_kind)`; or
- a typed identity whose equality includes command kind.

Do not force one attempt-cleanup command and one later request-finalization command to conflict merely because they share the same attempt ID.

## Workstream B — Move failed-attempt cleanup into the supervisor

Migrate the existing `AttemptCleanupProgress` semantics from the coordinator.

Required component order:

1. finalize/check durable attempt transition;
2. converge durable reservation terminal state;
3. release quota reservation if this command owns it;
4. decrement active count if owned;
5. apply the canonical attempt-scoped failure effects;
6. converge/release half-open probe;
7. mark command completed only when every required component is complete.

The retry loop may select another account only after the cleanup command reports complete convergence.

Cancellation behavior:

- the request waiter may be cancelled;
- the retained cleanup command continues under supervisor ownership;
- cancellation joins the existing command rather than creating another task;
- canonical client-cancelled request terminal submission occurs only after the attempt cleanup has converged.

Remove coordinator task creation, done callbacks, progress dictionaries, drain loops, and capacity counters for this command after migration.

## Workstream C — Move claim compensation into the supervisor

Migrate post-commit publication compensation.

Required component order follows the existing publication receipt and durable claim semantics:

1. release any published active count;
2. release any published quota reservation;
3. converge durable attempt terminal state when allocated;
4. converge durable reservation state when allocated;
5. release an acquired health probe;
6. mark complete only when all actually acquired/allocated components converge.

Do not create durable rows that were never allocated solely to simplify identity shape.

A failure before runtime publication should not submit compensation for components that were never acquired.

Remove coordinator-owned compensation task/progress state after migration.

## Workstream D — Unify scheduling, capacity, diagnostics, and drain

### Capacity

One configured supervisor capacity applies to active/retry-pending commands across all kinds.

Capacity rejection:

- occurs before generation reference acquisition and before detached task creation;
- remains a local invariant/overload failure;
- before response handoff, returns the existing fail-closed local error;
- after response handoff, records bounded diagnostics and uses the existing fail-closed worker/startup-repair policy if correctness ownership cannot be accepted;
- never applies provider penalties.

Do not maintain separate hidden capacities in the coordinator.

### Retry

Retain one bounded retry scheduler. Per-kind retry policy may differ only where existing behavior requires it:

- selected request finalization may use the existing configured retry age/backoff;
- failed-attempt cleanup must converge before reselection and therefore may run/join immediately with bounded local retries;
- claim compensation may use the same short bounded retry mechanism.

Do not add per-command exponential backoff objects or daily retry windows.

### Diagnostics

Expose bounded counts by command kind plus aggregate:

- active;
- retry pending;
- completed-history count;
- capacity rejections;
- oldest active age;
- last bounded error class/stage.

Do not expose request bodies, credentials, or full command payloads.

### Shutdown/retirement drain

One supervisor drain handles all live command kinds.

- generation retirement waits according to Plan 080;
- process shutdown uses one bounded drain;
- incomplete durable work after process death remains for startup reconciliation;
- unresolved in-memory-only ownership that cannot be reconstructed must trigger fail-closed handling before ordinary process continuation.

## Workstream E — Remove duplicate and compatibility paths

After all production call sites use the consolidated supervisor, remove:

- `_retained_terminal_commands` and related coordinator task/progress helpers;
- coordinator retained-capacity/drain metrics;
- duplicated command-kind dispatch in the coordinator;
- no-supervisor direct finalization fallback if production startup always installs the supervisor;
- `request/finalization_queue.py` if it is now test/compatibility residue with no supported external integration;
- stale aliases and documentation describing multiple terminal owners.

Keep `RequestFinalizer` and `AttemptFinalizer` as small durable-operation services if they remain useful. Consolidation concerns ownership/scheduling, not necessarily merging all SQL into one file.

If one service becomes a one-call wrapper with no independent invariant, inline/delete it only when tests prove no semantic loss.

## Workstream F — Simplify coordinator responsibilities

The coordinator should retain only:

- construct typed command submission;
- register/join the supervisor command;
- wait for required convergence before retry/reselection;
- translate supervisor result into request flow.

It should no longer:

- own retained tasks;
- own terminal retry timers;
- store resumable terminal progress;
- perform shutdown dispatch by command kind;
- project separate capacity snapshots.

Split helpers only where it reduces the coordinator's responsibility. Do not conduct a broad coordinator rewrite.

## Focused verification

Migrate/extend existing request coordinator and finalization tests.

Required cases:

1. each command kind registers and acquires one generation reference;
2. compatible duplicates join the same task/progress;
3. conflicting payload/kind identity fails before mutation;
4. failed-attempt cleanup converges before another account is selected;
5. cancellation during failed-attempt cleanup does not cancel retained work;
6. claim compensation releases only acquired components;
7. partial component failure retries only incomplete components;
8. completed components are never replayed;
9. global capacity applies across mixed command kinds;
10. mixed-kind drain runs each correct handler exactly once;
11. coordinator contains no parallel retained registry after migration;
12. post-handoff capacity failure never retries or penalizes a provider;
13. process shutdown leaves only reconstructable durable work for startup repair;
14. existing streaming/non-streaming success, cancellation, and failover smoke cases remain green.

Do not add a large combinatorial matrix. One representative case per invariant is sufficient.

Suggested commands:

```bash
uv run ruff format src/eggpool/request/coordinator.py src/eggpool/request/finalization_job.py src/eggpool/request/finalizer.py src/eggpool/request/attempt_finalizer.py tests/unit tests/integration
uv run ruff check src/eggpool/request/coordinator.py src/eggpool/request/finalization_job.py src/eggpool/request/finalizer.py src/eggpool/request/attempt_finalizer.py tests/unit tests/integration
uv run pyright src/eggpool/request/coordinator.py src/eggpool/request/finalization_job.py src/eggpool/request/finalizer.py src/eggpool/request/attempt_finalizer.py
uv run pytest <affected cleanup/compensation/finalization/failover tests> -q --tb=short --maxfail=1
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
```

## Acceptance criteria

- [x] One generation-owned supervisor is authoritative for all three live terminal command kinds.
- [x] Every command has an explicit typed identity, submission, and progress record.
- [x] Failed-attempt cleanup converges before reselection.
- [x] Claim compensation releases only actually acquired/allocated components.
- [x] Duplicate submissions join and conflicting submissions fail closed.
- [x] One bounded capacity and retry scheduler covers all command kinds.
- [x] Generation references are acquired/released exactly once per accepted command.
- [x] The coordinator no longer owns retained terminal tasks or progress.
- [x] Obsolete finalization compatibility queue/fallback paths are removed when unsupported.
- [x] Startup repair remains limited to prior-process durable leftovers.
- [x] Focused tests and smoke pass.
- [x] Source/test line count for terminal ownership decreases or any increase is explicitly temporary and justified by subsequent deletion in the same plan.
- [x] No generic workflow, command bus, persistence table, or CI expansion is introduced.

## Rejection conditions

Do not close this plan if:

- two production components can still own retry/scheduling for the same terminal obligation;
- coordinator retained registries remain active alongside supervisor ownership;
- cleanup can proceed to reselection before convergence;
- capacity is independently enforced in multiple owners;
- progress completion is inferred from task completion alone;
- a command can release a component it did not acquire;
- startup repair is used as a live age-based cleanup mechanism;
- consolidation increases abstraction without deleting old paths.

## Implementation sequence for GPT-5.6 Luna

1. Read Plan 080 implementation and verify generation references are stable.
2. Inventory all registration/join/drain/capacity call sites for the three command kinds.
3. Add the closed typed command union to the existing supervisor.
4. Migrate failed-attempt cleanup with focused tests.
5. Migrate claim compensation with focused tests.
6. Unify capacity, retry, diagnostics, and drain.
7. Remove coordinator retained ownership and unsupported compatibility paths.
8. Reconcile architecture documentation.
9. Run focused checks, then smoke.
10. Mark complete only after proving the old paths are unreachable/deleted.
