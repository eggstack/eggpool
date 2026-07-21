# Phase 4 — Candidate Resource Ownership and Abort Cleanup

Date: 2026-07-19
Status: implemented
Roadmap: `plans/001-reload-correctness-performance-roadmap.md`
Prerequisites: Phases 1–3.

## Objective

Make ownership of reload-created resources explicit from the moment each resource is constructed. Any failure before successful publication must close the complete candidate graph, and successful publication must transfer ownership exactly once to the runtime manager.

This phase replaces the current partial-cleanup shape, including no-op cleanup helpers and early-return paths that leave successfully built candidates open.

## Resource taxonomy

Document and enforce four ownership classes:

- **Process-owned**: database handle, process supervisor, shared dispatch writer, shared routing-trace writer, process metrics/coalescers, control server.
- **Generation-owned**: provider client pool, outbound client manager, DNS backend, registry/catalog/router/coordinator graph, generation task supervisor, generation-local queues and guards.
- **Candidate-owned**: generation resources during construction, before ownership transfer.
- **Request-owned**: lease objects and per-request/stream resources.

The candidate container must never assume ownership of process-owned services merely because they are injected into the candidate graph.

## Candidate container

Introduce a typed container, provisionally `RuntimeGenerationCandidate`, with fields such as:

- generation metadata and digest;
- constructed `RuntimeGeneration` or builder state;
- `AsyncExitStack` or explicit cleanup stack;
- prepared process transitions, without applying them;
- prepared persistence delta, without committing it;
- ownership state: `building`, `prepared`, `transferred`, `aborted`;
- cleanup diagnostics.

Required methods:

- `register_resource(resource, close_callback)` immediately after construction;
- `mark_prepared()` after the graph is complete;
- `transfer_to_runtime_manager()` to detach candidate cleanup only after successful publication;
- `abort(cause)` to close all registered candidate-owned resources in reverse construction order;
- idempotent finalization guards preventing double close or transfer-after-abort.

An `AsyncExitStack` is preferred where it fits, but explicit callbacks are acceptable when close ordering or typed diagnostics require them.

## Registration discipline

Every closeable resource must be registered immediately after successful construction and before the next await that could fail. Do not wait until the full generation object exists.

Audit at minimum:

- provider-specific HTTP clients;
- shared/provider client pool wrappers;
- outbound client manager;
- DNS resolver/backend;
- generation task supervisor and started tasks;
- finalization retry queues or workers if generation-owned;
- routing guards or watchers;
- any catalog/model-info client created per generation;
- future resources exposed through a common close protocol.

Where a resource owns nested resources, define whether only the parent closes them. Avoid registering both parent and child when that would double close.

## Abort behavior

Candidate abort must run on:

- construction failure;
- validation or hydration failure after partial construction;
- persistence preparation failure;
- process-transition preparation failure;
- publication precondition failure;
- publication failure before ownership transfer;
- caller cancellation before the commit point;
- unexpected exception before transfer.

Abort should:

1. preserve the primary failure;
2. close resources in reverse registration order;
3. collect close errors without masking the primary error;
4. emit structured cleanup diagnostics;
5. leave process-owned resources untouched;
6. be safe when called repeatedly.

## Cancellation policy

Use shielding only for the bounded cleanup operation, not for arbitrary candidate work. If the reload task is cancelled before publication:

- record cancellation as the primary outcome;
- shield `candidate.abort()` long enough to complete bounded cleanup;
- re-raise cancellation after cleanup and admission finalization.

If a close operation can hang, apply existing bounded close timeouts and record forced-abort status.

## Transfer behavior

Ownership transfer must occur only after the runtime manager has accepted the candidate generation. The transfer action should be narrow and non-awaiting where possible:

- verify state is `prepared`;
- detach generation resource callbacks from candidate cleanup;
- mark state `transferred`;
- make the runtime manager solely responsible for eventual retirement/close.

If publication returns metadata before transfer, ensure no cancellation window can cause both the candidate and runtime manager to believe they own the generation. Prefer a runtime-manager install API that performs acceptance and transfer atomically through a defined callback or ownership token.

## Remove misleading cleanup paths

Replace or delete helpers that claim to clean a partial generation but only log. Update call sites so there is one authoritative abort path.

Search for:

- `cleanup_partial_generation`;
- builder cleanup helpers;
- broad `except` blocks returning reload failure without closing the candidate;
- manually closed resources that should be registered in the candidate stack;
- duplicate close logic between reload manager and runtime manager.

## Cleanup diagnostics

Capture:

- candidate generation ID;
- ownership state at failure;
- resource types registered;
- resource types closed;
- close duration;
- close errors by type and bounded message;
- whether cleanup timed out;
- primary reload failure stage.

Do not include API keys, provider tokens, full URLs with credentials, or config secrets.

## Tests

### Stage-by-stage construction failures

Inject a failure after each registered resource. Assert all earlier resources close exactly once and later resources were never constructed.

### Post-construction pre-publication failures

Inject failures during backoff hydration, persistence-plan preparation, process-transition preparation, and publication precondition checks. Assert complete candidate cleanup.

### Cancellation

Cancel at every Phase 1 barrier before publication. Assert bounded cleanup completes and cancellation propagates.

### Cleanup failures

Configure one or more resource close callbacks to fail. Assert:

- all remaining callbacks still execute;
- primary reload error remains primary;
- close errors appear in diagnostics;
- no double-close occurs.

### Successful transfer

Publish a candidate and assert candidate abort no longer closes generation resources. Retire the generation later and assert runtime manager closes them exactly once.

### Repeated-failure plateau

Run at least 100 failed reloads at several injection stages. Assert stable:

- file descriptor count;
- EggPool-owned task count;
- open fake client count;
- DNS backend count;
- worker count;
- memory plateau within a documented tolerance.

## Implementation sequence

1. Inventory all process- and generation-owned resources.
2. Add ownership documentation to runtime-generation types.
3. Implement the candidate container and registration API.
4. Migrate builder construction one resource at a time.
5. Replace partial cleanup helpers.
6. Wrap all pre-publication reload exits with abort.
7. Define ownership transfer with runtime-manager installation.
8. Add cancellation shielding for bounded cleanup.
9. Add cleanup diagnostics and resource plateau tests.
10. Run full reload, streaming, and shutdown suites.

## Acceptance criteria

- Every generation-owned closeable is registered immediately after construction.
- Every pre-publication failure closes the complete candidate graph.
- Cleanup is idempotent and reverse ordered.
- Cleanup errors never prevent remaining resources from closing.
- Successful publication transfers ownership once and only once.
- Process-owned services are never closed by candidate abort.
- Repeated failed reloads show no monotonic growth in tasks, descriptors, clients, or workers.
- The no-op partial cleanup implementation no longer exists.
- No new untracked background task is introduced.

## Handoff evidence

Provide the ownership inventory, focused failure-injection commands, repeated-failure resource table, one successful transfer/retirement trace, and any resource intentionally classified as process-owned with justification.