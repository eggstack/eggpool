# Plan 080 — Generation Finalization Ownership Alignment

Date: 2026-08-05
Status: complete
Parent roadmap: `plans/077-sbc-lifecycle-simplification-and-runtime-correctness-roadmap.md`
Depends on:

- `plans/078-runtime-invariant-and-request-boundary-corrections.md`
- `plans/079-quarantine-durability-and-generation-publication.md`

Planning baseline: `cd8967799e6613f3a5965af8cd15ce3c5269aaa8`

## Purpose

Establish one truthful lifetime contract for retained request finalization before consolidating terminal command implementations.

At the planning baseline, `RequestFinalizationSupervisor` is constructed inside `RuntimeGenerationFactory.prepare()` for each generation, receives generation-owned router/quota/health dependencies, and is attached privately to the generation-owned coordinator. Documentation nevertheless calls it process-owned. Retained jobs can continue after the client waiter has been cancelled, which means generation retirement must not close the dependencies they still require.

This plan does not yet merge failed-attempt cleanup, claim compensation, or request finalization. It only aligns ownership, lifecycle, and retirement so the later consolidation in Plan 081 has a safe foundation.

## Required ownership decision

Treat the existing finalization supervisor as **generation-owned**.

This is the smallest truthful model because:

- it is constructed per generation;
- its jobs bind the generation's coordinator/finalizer/router/quota/health objects;
- those objects are not valid after generation close;
- making it process-owned would require a larger redesign of every retained job dependency.

The runtime manager must therefore treat active retained finalization work as a generation-retirement reference, equivalent in effect to an in-flight generation lease.

Do not move the supervisor to `ProcessRuntime` in this plan.

## Governing decisions

1. One finalization supervisor belongs to one runtime generation.
2. A supervisor job may outlive its client request waiter.
3. A generation cannot close resources while its supervisor has active or retry-pending correctness work.
4. Completed diagnostic history does not block retirement.
5. Jobs that have exhausted bounded in-process retry but still leave durable work unresolved must produce an explicit retirement outcome; they cannot be treated as complete by absence of a running task.
6. Rehash publication must not wait for the retiring generation to close before accepting new requests on the new generation.
7. Retiring generations may remain resident for bounded terminal convergence.
8. No generic reference-count framework or second runtime manager is added.
9. Startup crash repair remains authoritative only after process death, not as a substitute for live retained ownership.
10. Resource close ordering must remain deterministic and bounded.

## Workstream A — Make ownership explicit in runtime types

### Runtime generation field

Add an explicit typed `finalization_supervisor` field to `RuntimeGeneration` and its builder/factory output.

Remove private post-construction attachment where practical. The coordinator may still retain a direct reference for submission, but the generation must be the visible owner.

Update:

- `PreparedRuntimeGeneration`;
- `RuntimeGeneration`;
- `RuntimeGenerationBuilder`;
- `RuntimeGenerationFactory.prepare()`;
- generation installation and candidate abort paths;
- runtime snapshots/documentation.

Do not expose the supervisor through arbitrary `app.state` mirrors as an authority.

### Ownership documentation

Correct `runtime_manager.py`, `generation_factory.py`, `AGENTS.md`, and architecture documents:

- the supervisor is generation-owned;
- process-owned database/repositories remain shared;
- retained jobs block generation resource close;
- diagnostics may be aggregated process-wide by reading active/retiring generations, but ownership remains generation-local.

Remove the contradictory “process-owned supervisor” wording.

## Workstream B — Add a bounded retirement reference

### Preferred mechanism

Use the existing generation slot/lease lifecycle rather than inventing another task registry.

Implement one small retirement reference contract, for example:

- supervisor calls an injected `retain_generation()` callback when a newly registered job first accepts correctness ownership;
- supervisor calls the matching idempotent release callback only when the job reaches a terminal retirement-safe state;
- the callback increments/decrements a terminal reference count on the owning generation slot;
- slot close requires both request/stream lease count and terminal reference count to be zero.

Alternative acceptable implementation:

- the supervisor acquires one explicit generation lease per accepted job through a generation-local lease factory and releases it on retirement-safe completion.

Choose the design that reuses the most existing runtime-manager machinery and introduces the fewest new states.

### Required reference semantics

- registration acquires the reference synchronously before the first cancellation-sensitive await;
- duplicate registration for the same job does not acquire another reference;
- retries do not acquire additional references;
- completion releases exactly once;
- terminal conflict does not leak a reference;
- capacity rejection occurs before reference acquisition;
- diagnostic-history retention does not retain the generation;
- forced process shutdown may abandon references because startup repair follows process death;
- rehash retirement does not use age alone to reclaim a live reference.

### Retirement-safe terminal states

Define explicitly when a job releases its generation reference.

It may release when:

1. durable request/attempt/reservation state has converged;
2. all acquired and required generation-local runtime obligations have converged;
3. no retryable correctness work remains.

Analytics-only failure must not retain the generation indefinitely.

A job that reaches a bounded non-retryable failure with unresolved durable correctness work must not falsely release as successful. Choose one existing supported outcome:

- keep the retiring generation retained and report degraded retirement until process restart; or
- transition the worker to fail-closed shutdown so startup repair becomes authoritative.

For the local supervised deployment, prefer fail-closed worker shutdown if the job cannot safely converge and would otherwise retain a generation indefinitely. Reuse the existing fatal runtime/database handler; do not add a watchdog service.

## Workstream C — Update generation retirement and close ordering

### Slot behavior

Audit `RuntimeManager` slot states and close scheduling.

Required behavior:

- active generation accepts new request leases and terminal references;
- retiring generation rejects new request leases but its existing supervisor may finish accepted jobs;
- close begins only when request/stream leases and terminal references are zero;
- failed close remains observable;
- generation is removed from retiring inventory only after close finishes or process shutdown owns the unresolved state.

Do not poll with frequent sleeps. Use the existing condition/event/notification mechanism or add one bounded event per slot.

### Close order

When closing a generation:

1. reject/stop new generation-owned background task submissions;
2. confirm no terminal references remain;
3. stop/drain non-authoritative diagnostic writers with existing timeouts;
4. close coordinator/supervisor bookkeeping;
5. close provider clients/outbound clients/DNS backend and other dependencies in reverse ownership order.

The finalization supervisor must not be closed before its jobs release router/quota/health obligations.

Candidate abort before publication is different: a candidate has accepted no production terminal jobs, so existing reverse-order abort remains sufficient.

## Workstream D — Make metrics ownership truthful

`/api/stats/runtime` currently exposes one active finalization-supervisor snapshot. After this plan it should expose bounded generation-aware facts without becoming a historical metrics system.

Minimum fields:

- active generation ID and active supervisor counts;
- number of retiring generations;
- total retiring terminal references;
- oldest retiring generation age;
- whether any generation is blocked on terminal convergence;
- bounded last failure class/stage, redacted.

Do not aggregate full job lists or identities across generations.

Existing dashboard fields may keep compatibility aliases for one release if required, but authoritative documentation must describe generation-local ownership.

## Workstream E — Eliminate private wiring and stale fallbacks

Within this plan only:

- replace direct assignment to `coordinator._finalization_supervisor` with a constructor argument or explicit typed bind method;
- ensure production startup always installs the supervisor before request admission;
- retain the no-supervisor direct-finalization fallback only if a current supported embedder path exists.

If the fallback is used solely by tests, migrate those tests to a small real supervisor fixture and remove the production fallback. If removal is broader, record it for Plan 084 rather than expanding this plan.

## Focused verification

Extend existing runtime-manager, rehash, and finalization tests.

Required cases:

1. first accepted finalization job acquires one terminal generation reference;
2. duplicate registration does not double-acquire;
3. capacity rejection acquires no reference;
4. completed job releases exactly once;
5. retry-pending job continues blocking generation close;
6. completed diagnostic history does not block close;
7. a retiring generation with zero request leases but one terminal reference does not close resources;
8. releasing the last terminal reference triggers normal close;
9. rehash publishes the new generation while the old generation remains safely retiring;
10. old generation dependencies remain usable until the retained job converges;
11. an unrecoverable correctness failure uses the selected fail-closed policy rather than reporting successful retirement;
12. process shutdown remains bounded and leaves startup repair authoritative after death.

Use deterministic fake jobs and events; do not use timing races or long sleeps.

Suggested commands:

```bash
uv run ruff format src/eggpool/runtime_manager.py src/eggpool/generation_factory.py src/eggpool/request/finalization_job.py src/eggpool/request/coordinator.py tests/unit tests/integration
uv run ruff check src/eggpool/runtime_manager.py src/eggpool/generation_factory.py src/eggpool/request/finalization_job.py src/eggpool/request/coordinator.py tests/unit tests/integration
uv run pyright src/eggpool/runtime_manager.py src/eggpool/generation_factory.py src/eggpool/request/finalization_job.py src/eggpool/request/coordinator.py
uv run pytest <affected runtime-manager/rehash/finalization tests> -q --tb=short --maxfail=1
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
```

## Acceptance criteria

- [x] `RequestFinalizationSupervisor` is explicitly generation-owned in code and documentation.
- [x] The supervisor is a typed field on the generation/factory output.
- [x] Accepted retained correctness work acquires one generation retirement reference.
- [x] Duplicate registration/retry does not multiply references.
- [x] A generation cannot close dependencies while terminal references remain.
- [x] Rehash can publish a new generation while the old one safely retires.
- [x] Terminal references release only after durable and required runtime convergence.
- [x] Unrecoverable unresolved work uses an explicit fail-closed policy.
- [x] Runtime metrics report bounded generation-aware ownership facts.
- [x] Private supervisor assignment is removed or reduced to a typed binding.
- [x] Focused tests and the smoke gate pass.
- [x] No second runtime manager, polling service, or generic reference framework is introduced.

## Implementation notes

The generation slot now combines request leases and terminal references behind
one event-based drain contract. Live retirement leaves unresolved terminal
references resident and invokes the existing fatal worker handler; the final
reference release resumes normal close. Bounded process shutdown explicitly
allows reference abandonment so startup crash repair remains authoritative.

Regression coverage is in capability-based runtime/finalization suites,
including `tests/unit/test_generation_finalization_ownership.py`; no
plan-numbered test suite or CI job was added.

## Rejection conditions

Do not close this plan if:

- documentation still calls a per-generation supervisor process-owned;
- a retained job can outlive closed router/quota/health dependencies;
- generation close relies only on request lease count;
- age-based cleanup can reclaim a live terminal reference;
- retry attempts acquire repeated retirement references;
- rehash blocks all new request publication until old finalization jobs finish;
- unresolved correctness work is converted into analytics-only completion;
- tests depend on race-prone sleeps.

## Implementation sequence for GPT-5.6 Luna

1. Read runtime slot/lease close paths, generation factory, supervisor registration/completion, rehash publication, and shutdown tests.
2. Add the explicit generation-owned supervisor field and typed wiring.
3. Implement the smallest retirement reference using existing slot/lease primitives.
4. Bind acquire/release to supervisor job lifecycle.
5. Update close ordering and fail-closed unresolved policy.
6. Add bounded generation-aware metrics.
7. Correct ownership documentation.
8. Run focused checks, then smoke.
9. Mark complete only with exact commands and observed outcomes.
