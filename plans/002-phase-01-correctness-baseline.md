# Phase 1 — Deterministic Reload Correctness Baseline

Date: 2026-07-19
Status: implementation handoff
Roadmap: `plans/001-reload-correctness-performance-roadmap.md`

## Objective

Build deterministic test infrastructure that captures the current reload, runtime-generation, persistence, and process-state defects before implementation changes begin. This phase is evidence gathering and invariant definition, not architectural remediation.

The central requirement is to replace timing-sensitive subprocess tests and non-strict expected failures with a harness that can stop execution at exact reload stages, inspect all state domains, and prove whether an operation produced a complete old state, a complete new state, or an invalid mixture.

## Scope

This phase covers:

- reload admission concurrency;
- candidate construction and cleanup;
- persistence reconciliation and publication ordering;
- process-supervisor and shared-writer mutation;
- runtime-manager publication and retirement;
- request lease behavior;
- `app.state` compatibility mirrors;
- persisted account/model backoff hydration;
- dispatch-writer and metrics dependency parity;
- resource accounting across failed and successful reloads.

## Non-goals

- Do not fix the known reload race in this phase.
- Do not redesign the runtime manager or reload transaction.
- Do not broadly refactor production construction code before the tests expose its current behavior.
- Do not use long sleeps or probabilistic stress as the primary proof of a race.
- Do not weaken assertions to accommodate nondeterminism.

## Primary code and test seams

Inspect and instrument at minimum:

- `src/eggpool/control/reload_manager.py`
- `src/eggpool/control/server.py`
- `src/eggpool/runtime.py` or the module containing `RuntimeManager`
- `src/eggpool/app.py`
- `src/eggpool/request/coordinator.py`
- `src/eggpool/db/*`
- process-supervisor and task-spec modules
- dispatch and routing-trace writer modules
- existing reload/control integration tests
- existing runtime-generation lease and retirement tests

Exact filenames may differ; follow current ownership boundaries rather than forcing new modules prematurely.

## Required test infrastructure

### 1. Real reload harness

Create a reusable in-process fixture that starts the production application graph with:

- a temporary file-backed SQLite database;
- a temporary state/runtime directory;
- a temporary Unix control socket where supported;
- a deterministic initial configuration;
- at least two valid candidate configurations with observable differences;
- instrumented provider clients and closeable resources;
- a runtime manager and reload manager using production code paths;
- no outbound network dependency.

The two configurations should differ in enough domains to expose split state:

- provider/account membership;
- routing policy;
- task intervals or enabled process tasks;
- routing-trace writer settings;
- metrics detailed-span sample rate;
- a setting that is ignored or classified as no-op;
- a restart-required setting for rejection coverage.

### 2. Reload stage barriers

Add a test-only observer or injectable hook interface that can pause at named stages without changing normal behavior. Recommended stages:

- admission claimed;
- validation complete;
- diff computed;
- candidate construction started;
- candidate construction complete;
- persistence reconciliation started;
- persistence reconciliation prepared;
- process transition prepared;
- immediately before publication;
- immediately after publication;
- process transition applied;
- persistence committed;
- retirement scheduled;
- finalization complete.

Hooks must be explicit awaitable barriers, not monkey-patched sleeps. Production code may expose a no-op observer protocol or callback collection with no runtime cost when absent.

### 3. Fault injector

Introduce a test-only fault injector capable of raising a distinct exception at every stage above and during each close operation. The injector should identify:

- stage name;
- candidate generation ID;
- whether publication occurred;
- whether the injected error is recoverable, cancellation, or close failure.

Use one exception type per broad class or include structured metadata. Avoid matching arbitrary exception strings.

### 4. Complete state snapshot

Create a snapshot helper that records, at minimum:

- active generation ID and config digest;
- identities of registry, catalog, router, coordinator, health manager, stats service, and recorders;
- active generation configuration values relevant to the candidate;
- `app.state` generation-owned mirrors and effective config/digest;
- providers and accounts persisted in SQLite;
- active account/model backoffs in memory and persistence;
- process-supervisor task specifications and running task IDs;
- routing-trace writer configuration;
- dispatch-writer existence, enabled selection, queue state, and worker identity;
- open provider client pools, outbound managers, DNS backends, and closeable fakes;
- active and retiring generation counts;
- active lease counts;
- live `asyncio.Task` count filtered to EggPool-owned tasks;
- open file descriptor count where portable;
- control socket path and inode where supported.

Snapshots should support value comparison and identity comparison. Serialize only stable values for assertion messages.

### 5. Instrumented closeable resources

Provide fakes for generation-owned network resources that expose:

- construction count;
- open/closed state;
- close count;
- optional close barrier;
- optional close failure;
- generation ID;
- attempted use after close.

Using a closed fake must raise a deterministic test error. This is necessary to detect stale `app.state` fallback rather than merely checking object IDs.

## Required failing tests

### Concurrent admission race

Coordinate two reload calls so both reach the current pre-lock admission check before either acquires the reload lock. Assert the intended invariant:

- exactly one call is admitted;
- the other receives an immediate `reload_in_progress` result;
- the rejected call does not enter candidate construction;
- the rejected call does not wait for the accepted reload to finish.

This test should fail against the current check-then-await-then-lock implementation.

### Persistence/publication split

Inject failure after provider/account persistence changes are prepared or committed but before publication completes. Compare the pre-state and post-state. The desired future invariant is total equality with the old state; document the current mixed state precisely.

### Process mutation before publication

Use candidate configuration that changes task specs or routing-trace settings. Pause before publication and assert that no process-owned mutation should yet be visible. Record current behavior if the supervisor or writer has already changed.

### Candidate resource leak

Inject failure after each candidate resource is created and before publication. Assert all resource counters return to baseline. Repeat the cycle enough times to show whether descriptors/tasks/clients accumulate.

### Stale compatibility state

Complete a successful reload, then compare runtime-manager active objects with `app.state` mirrors. Exercise at least one route or diagnostic consumer that reads the mirror and detect use of the old object.

### Lease-acquisition fallback

Force runtime-manager acquisition to raise its expected exhaustion/shutdown error. Assert the future contract is HTTP 503 and no legacy coordinator invocation. Record the current fallback behavior.

### Retirement blocking

Hold a generation lease with a test stream, publish a candidate, and measure whether reload completion waits for the lease. The test must use barriers rather than a real five-minute timeout.

### Construction parity

Build one runtime through startup and another through reload candidate construction. Compare dependency presence and configured values, including:

- dispatch writer;
- local pre-upstream recorder;
- stream diagnostics;
- detailed-span sampling rate;
- health-manager persisted backoffs.

### No-op and failure diagnostics

Exercise semantic no-op, ignored-only change, validation failure, preparation failure, and publication failure. Assert the desired final diagnostic contract and record current discrepancies.

## Test organization

Prefer a dedicated structure such as:

- `tests/integration/reload/test_reload_admission.py`
- `tests/integration/reload/test_reload_atomicity.py`
- `tests/integration/reload/test_reload_resources.py`
- `tests/integration/reload/test_reload_retirement.py`
- `tests/integration/reload/test_reload_parity.py`
- `tests/support/reload_harness.py`
- `tests/support/reload_faults.py`
- `tests/support/runtime_snapshot.py`

Reuse existing conventions where the repository already has equivalent locations.

## Implementation sequence

1. Inventory current fixtures and consolidate duplicated server/config setup.
2. Add instrumented closeable resources.
3. Add state snapshot support.
4. Add no-op stage observer interface.
5. Add deterministic barriers.
6. Add fault injection.
7. Port the existing concurrent reload test to the barrier harness.
8. Add atomicity, resource, stale-state, retirement, and parity tests.
9. Mark tests with a focused `reload` marker if consistent with project marker policy.
10. Remove or tighten obsolete non-strict xfails only when the deterministic replacements exist.

## Acceptance criteria

- A focused test command reproduces every known reload/lifecycle defect deterministically.
- Concurrent admission coverage passes or fails consistently for at least 100 repeated runs.
- No test depends on subprocess scheduling to create a race.
- Every reload stage can be paused and faulted.
- State snapshots cover runtime, persistence, process tasks, shared writers, mirrors, and resources.
- Resource tests can detect use-after-close and double-close.
- The current implementation’s mixed-state and leak behavior is documented in assertion output or test comments.
- No production behavior changes beyond inert test hooks and observability required by the harness.
- Full unit/integration suite remains runnable.
- New tests are strict; no new non-strict `xfail` or broad skip is introduced.

## Handoff evidence

The implementing agent should record:

- focused test commands;
- repeated-run command for the admission race;
- which tests intentionally fail before subsequent phases;
- any test hook added to production modules and why it is inert in normal operation;
- baseline task, descriptor, and resource counts for one successful and one failed reload;
- exact existing expected-failure/skip markers superseded by the new harness.