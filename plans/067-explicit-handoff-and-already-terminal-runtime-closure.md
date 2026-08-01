# Plan 067 — Explicit Handoff and Already-Terminal Runtime Closure

Date: 2026-08-01
Status: completed
Parent roadmap: `plans/058-durable-convergence-exact-update-sbc-hotpath-roadmap.md`
Corrective predecessor: `plans/066-terminal-runtime-ownership-and-supervisor-closure.md`
Planning baseline: `052b81ed38598c2c07cfa283d0a1968ee2e5519c`

## Purpose

Close the two remaining semantic defects found during review of Plan 066 without reopening its lease, retry-scheduler, durable-finalization, or runtime-metrics architecture.

Plan 066 correctly introduced explicit runtime publication ownership, retained component-level cleanup progress, execution-time retry-age enforcement, coordinator-side capacity handling, and supervisor diagnostics. The remaining defects are narrower:

1. coordinator capacity handling uses `FinalizationData.bytes_emitted` as a proxy for whether the downstream response has started; and
2. runtime usage, health outcome, and account-runtime convergence are still gated by `DurableFinalizationResult.request_transitioned`, so a retained lease can be declared complete after observing already-terminal durable state without applying outstanding process-local outcome obligations.

This plan must correct those boundaries with explicit facts. It must not add another queue, supervisor, durable marker table, response lifecycle framework, or verification layer.

The deployment target remains a private single-process SBC or small LAN host. The preferred implementation is a few explicit fields and focused call-site updates.

## Confirmed defects

### 1. Byte count is not a downstream-handoff fact

`RequestCoordinator._finalize_terminal()` currently handles `FinalizationCapacityError` by checking `data.bytes_emitted`:

- `bytes_emitted <= 0` is treated as pre-handoff and raises a typed local invariant error;
- `bytes_emitted > 0` is treated as post-handoff and returns after recording the saturation condition.

This classification is incorrect.

For a non-streaming response, EggPool finalizes before returning `PreparedProxyResponse`, but `bytes_emitted` is already populated from the upstream response-body length. A non-empty response rejected at finalization capacity is therefore treated as post-handoff even though the local response has not been returned to the API layer.

For a streaming response, the ASGI response start may already have been sent before the iterator emits any body bytes. A zero-byte stream or a stream whose translated output is suppressed can therefore be post-handoff while `bytes_emitted == 0`.

Payload size remains useful accounting data, but it cannot determine response mutability.

### 2. Already-terminal durable state can suppress outstanding runtime outcome work

`AttemptRuntimeLease` now truthfully owns active-count, quota-reservation, and probe acquisition facts and records completed runtime components. A normal partial-runtime failure resumes correctly from `RUNTIME_RELEASE_PENDING` using the retained durable result.

However, `RequestFinalizer.apply_runtime_convergence()` still conditions final usage, health outcome, and account-runtime updates on `durable.request_transitioned`. When a job first observes a request that is already terminal, those operations are skipped and are excluded from the completion requirement even if the retained lease still owes them.

A durable no-op is not proof that process-local outcome effects were previously applied. The lease must state which outcome obligations it owns independently of whether this invocation changed the SQLite request row.

### 3. Focused acceptance evidence is missing

The Plan 066 commit added deadline and metrics tests, but it did not add the planned focused regressions proving:

- non-streaming and streaming capacity behavior at the actual handoff boundary;
- component-resuming runtime cleanup after an injected post-durable failure;
- truthful result fields while cleanup is incomplete; or
- already-terminal durable convergence with outstanding runtime usage/health/account obligations.

The correction should add only the cases needed to prove these two defects are closed.

## Scope

Primary runtime files:

- `src/eggpool/request/coordinator.py`
- `src/eggpool/request/finalization_job.py`
- `src/eggpool/request/finalizer.py`
- the API/stream wrapper that owns the actual downstream response-start fact, only if the coordinator cannot receive it directly from existing call structure

Focused existing test files:

- `tests/unit/test_request_finalization_state_machine.py`
- the existing coordinator terminal-path or streaming lifecycle test file
- the existing request-finalizer convergence test file

Planning metadata:

- `plans/058-durable-convergence-exact-update-sbc-hotpath-roadmap.md`
- `plans/066-terminal-runtime-ownership-and-supervisor-closure.md`
- this plan

Operator or architecture documentation should change only if the exact handoff or lease-obligation semantics are currently described incorrectly.

## Explicitly out of scope

- another finalization supervisor or retry queue;
- a durable finalization work table or migration;
- persisting process-local runtime component markers across restart;
- a generic ASGI response lifecycle state machine;
- changing upstream retry policy;
- dynamic finalization admission control or supervisor resizing;
- redesigning `HealthManager`, `QuotaEstimator`, `Router`, or `AccountRegistry`;
- changing durable request, attempt, reservation, or cost semantics;
- new runtime dependencies;
- new CI jobs, matrices, coverage thresholds, soak tests, benchmark gates, timing gates, evidence bundles, or plan-numbered test suites.

## Governing decisions

1. `bytes_emitted` remains payload accounting only. It must not classify whether a response status or headers can still be changed.
2. The handoff fact must be explicit at the call site that knows the response lifecycle.
3. Non-streaming finalization performed before returning `PreparedProxyResponse` is pre-handoff regardless of upstream body length.
4. Finalization performed inside a streaming response iterator is post-handoff once the local ASGI response has started, including a zero-byte body.
5. `AttemptRuntimeLease` remains the sole process-local ownership record for terminal runtime convergence.
6. Required runtime outcome components are lease obligations, not consequences inferred from `request_transitioned`.
7. A component is marked complete only after its operation succeeds or after the lease explicitly proves that component was not required.
8. Existing stale/startup repair remains the process-loss safety net. Do not make runtime markers durable.
9. Verification remains deterministic and small.

## Phase A — Carry an explicit downstream-handoff fact

### Required design

Use one narrow explicit fact, with naming such as:

- `downstream_started: bool`;
- `response_handed_off: bool`; or
- `response_mutable: bool` with the inverse convention.

Prefer adding the fact to the `_finalize_terminal()` call or to `FinalizationData` if that object already crosses every terminal call site. Do not create a new response-state class solely for this distinction.

### Required changes

1. Remove all capacity-rejection branching based on `bytes_emitted`.
2. Audit every `_finalize_terminal()` call site and classify it by actual local response lifecycle:
   - non-streaming completion and non-retryable responses finalized before `PreparedProxyResponse` return: pre-handoff;
   - setup or selection failures before a streaming response object is returned: pre-handoff;
   - terminal work executed from inside the streaming iterator after ASGI response start: post-handoff, even when no body chunk was yielded;
   - cancellation/error paths must use the lifecycle state of the local response, not the upstream byte count.
3. Pass the explicit handoff fact into the canonical finalization boundary.
4. On `FinalizationCapacityError` before handoff:
   - raise the existing typed local terminal-invariant error;
   - do not penalize the provider/account;
   - do not return a nominal successful response;
   - do not spawn detached cleanup.
5. On capacity rejection after handoff:
   - do not attempt to replace the response status or headers;
   - record one bounded scalar diagnostic through existing logging/supervisor/stream diagnostics;
   - leave the durable request, attempt, and reservation identity discoverable by stale/startup repair;
   - do not recursively invoke terminal finalization.
6. Keep `bytes_emitted` unchanged for accounting, diagnostics, and persisted request metadata.
7. Avoid threading the new fact through unrelated routing or provider APIs.

### Acceptance criteria

- A non-streaming response with a non-empty body is still classified pre-handoff when finalization occurs before response return.
- A zero-byte streaming response finalized from inside the active stream iterator is classified post-handoff.
- Pre-handoff saturation raises the typed local invariant error and cannot return a nominal success.
- Post-handoff saturation records the bounded invariant and does not raise an error that attempts to replace the started response.
- Neither path creates detached work, another queue, or a provider penalty.
- `bytes_emitted` is no longer read to determine handoff state.

## Phase B — Make runtime outcome obligations independent of durable transition

### Required design

Extend the existing `AttemptRuntimeLease` only with explicit obligation facts needed to answer:

- is final live usage application required for this terminal owner?;
- is health outcome application or probe release required?;
- is account runtime success/failure update required?; and
- which of those components have completed?

A few booleans plus the existing completed-component set are sufficient. Do not add a second lease type or a generalized effects graph.

Possible fields include:

```python
usage_outcome_required: bool
health_outcome_required: bool
account_runtime_outcome_required: bool
```

Exact naming may follow repository conventions. Requirements may be bound when the terminal command is first registered, provided duplicate registrations prove compatible facts.

### Required changes

1. Derive runtime outcome obligations from the accepted selected attempt, terminal outcome, publication ownership, and `health_already_applied` facts—not from whether this invocation changed the request row.
2. Preserve durable convergence as a prerequisite: runtime outcome operations run only after request, attempt, and durable reservation state are proven terminal.
3. Remove `durable.request_transitioned` as the sole gate for:
   - final live usage application;
   - health success/failure outcome application;
   - account runtime success/failure update; and
   - whether those markers are required for `runtime_lease.released`.
4. Keep durable cost and normalized usage data available to the runtime convergence step even when the durable request row was already terminal.
5. For a production lease that owns an outcome component:
   - execute it once if its marker is incomplete;
   - mark it complete only after success;
   - leave the job at `RUNTIME_RELEASE_PENDING` if it fails;
   - resume at that component on the supervisor retry without replaying completed components or durable finalization.
6. For components explicitly not required:
   - either mark them complete when the lease is bound; or
   - exclude them from the required set using the explicit obligation flag.
7. Preserve `health_already_applied` semantics. A caller that already applied health/account effects must bind those obligations as not required rather than relying on `request_transitioned=False`.
8. Ensure duplicate terminal registration validates obligation compatibility along with acquisition facts.
9. Keep the no-supervisor compatibility path on the same convergence method. Any inference retained for legacy direct callers must not weaken production lease semantics.
10. Preserve truthful `FinalizationResult` fields:
    - durable reservation state remains separate from live quota removal;
    - runtime completion remains false while any owned required component is incomplete;
    - health and account outcome markers reflect actual application, not durable transition.

### Acceptance criteria

- An already-terminal durable result can complete outstanding lease-owned usage, health, and account-runtime obligations.
- `request_transitioned=False` does not by itself suppress an owned runtime outcome component.
- An injected failure after one or more components succeed leaves the job retry-pending at `RUNTIME_RELEASE_PENDING`.
- Retry applies only unfinished components and does not rerun durable finalization.
- Usage, health, account-runtime, quota removal, active decrement, and probe release are each applied at most once per lease.
- `runtime_cleanup_complete` remains false until every acquired and required component has completed.
- `health_already_applied=True` does not double-apply health or account effects.

## Phase C — Focused verification and closure metadata

### Test budget

Add or modify no more than four focused regression cases in existing capability-based files. Parameterize closely related handoff cases where it remains readable.

Required coverage:

1. **Explicit handoff classification:** a non-streaming non-empty response at supervisor capacity follows the pre-handoff typed-error path, while a zero-byte active stream follows the post-handoff diagnostic path.
2. **Already-terminal obligations:** with `request_terminal=True` and `request_transitioned=False`, an owned lease still applies outstanding usage, health, and account-runtime components exactly once.
3. **Component-resuming retry:** inject failure in a middle runtime component, verify progress remains `RUNTIME_RELEASE_PENDING`, then verify retry completes only the missing component and durable finalization was invoked once.
4. **Truthful result state:** durable reservation convergence can be true while live runtime fields and `runtime_cleanup_complete` remain false; all become true only after their actual operations complete.

Use direct fakes/mocks and explicit progress inspection. Do not use long sleeps, live provider calls, repeated fault campaigns, or a new test harness.

### Required local checks

Focused checks:

```bash
uv run ruff format <changed paths>
uv run ruff check <changed paths>
uv run pytest tests/unit/test_request_finalization_state_machine.py -q --tb=short --maxfail=1
uv run pytest <affected coordinator/finalizer test files> -q --tb=short --maxfail=1
```

Existing repository gate:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
```

Do not add CI jobs or retained evidence artifacts. Record the exact commands and outcomes in the implementation commit or plan closure note; do not claim checks that were not run.

### Planning closure

When implementation and focused verification complete:

1. mark this plan complete and check each satisfied acceptance item;
2. update Plan 066 with a short post-implementation review note linking Plan 067;
3. mark Plan 066 fully complete only after the explicit handoff and already-terminal obligation regressions pass;
4. register Plan 067 in Plan 058 and return the roadmap to `completed` only after these criteria pass;
5. keep prior Plan 066 accomplishments checked, but leave the corrected handoff and already-terminal criteria pending until proven;
6. ensure roadmap/current-state prose describes the actual final code rather than the pre-Plan-066 defect.

## Recommended implementation sequence

1. introduce the explicit handoff boolean and update terminal call sites;
2. replace byte-count capacity classification;
3. add explicit lease outcome-obligation facts;
4. remove `request_transitioned` as the production obligation gate;
5. add the four focused regressions;
6. run focused checks and the existing smoke gate;
7. reconcile Plans 058, 066, and 067 metadata.

## Plan acceptance criteria

- [x] Capacity handling uses an explicit downstream-handoff fact rather than `bytes_emitted`.
- [x] Non-streaming finalization before response return is pre-handoff regardless of body size.
- [x] Streaming finalization inside the active iterator is post-handoff even with zero emitted body bytes.
- [x] Pre-handoff saturation fails closed with the typed local invariant error.
- [x] Post-handoff saturation records a bounded diagnostic without recursive finalization or response replacement.
- [x] Lease-owned usage, health, and account-runtime obligations are independent of `request_transitioned`.
- [x] Already-terminal durable state can converge all outstanding runtime obligations.
- [x] Partial runtime failure resumes from the unfinished component without replaying durable or completed runtime work.
- [x] Runtime result fields remain truthful throughout incomplete and completed convergence.
- [x] Focused regressions cover both defects and the existing smoke gate passes.
- [x] Plans 058, 066, and 067 have coherent status and acceptance metadata.
- [x] No migration, new queue, lifecycle framework, runtime dependency, CI expansion, soak gate, benchmark gate, or evidence system is introduced.

## Rejection conditions

Do not close this plan if:

- any capacity path still infers downstream handoff from payload byte count;
- a non-streaming response can silently succeed after pre-return finalization saturation;
- a started zero-byte stream raises a pre-handoff replacement error;
- an already-terminal durable row suppresses an outstanding lease-owned usage, health, or account-runtime component;
- runtime completion excludes an owned component merely because `request_transitioned=False`;
- partial failure can double-apply quota, active-count, usage, health, account-runtime, or probe effects;
- the required focused regression cases are absent;
- planning documents claim completion before the checks run;
- implementation adds a parallel retry/ownership system or disproportionate verification infrastructure.

## Definition of done

This corrective pass is complete when response mutability is represented by one explicit handoff fact, retained runtime leases own outcome obligations independently of durable transition, already-terminal durable state can converge all outstanding process-local components exactly once, the focused regressions and existing smoke gate pass, and Plans 058, 066, and 067 accurately report closure without adding new infrastructure.

## Implementation closure

Implemented with `FinalizationData.downstream_started` and explicit
`AttemptRuntimeLease` outcome-obligation flags. Focused verification passed:

```text
uv run pytest tests/unit/test_request_coordinator_cleanup.py tests/unit/test_request_finalization_state_machine.py tests/unit/test_request_finalizer.py -q --tb=short --maxfail=1
46 passed
uv run pyright src/eggpool/request/finalization_job.py src/eggpool/request/finalizer.py src/eggpool/request/coordinator.py
0 errors, 0 warnings, 0 informations
uv run ruff format --check src/ tests/ scripts/
734 files already formatted
uv run ruff check src/ tests/ scripts/
All checks passed
uv run pyright src/ scripts/
0 errors, 0 warnings, 0 informations
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
14 passed
```

The relevant coordinator integration suite also passed with 59 tests. No new
CI job, queue, migration, dependency, or evidence format was added.
