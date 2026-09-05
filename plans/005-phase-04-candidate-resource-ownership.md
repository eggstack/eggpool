# Phase 4 — Candidate Resource Ownership and Abort Cleanup

Date: 2026-07-19
Status: complete (2026-09-05)
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

## Closure evidence

The candidate ownership implementation was delivered by `0f5e815d` with the
cancellation/diagnostics gap-fill in `41bf80fc`. The final corrective closure
is recorded in `2e5e6ba0e2ac36bde20c09eb6c91cb38e4f4b7e5`.

The ownership contract is explicit in `RuntimeGenerationCandidate`:

- `building → prepared → transferred` and `building/prepared → aborted` are
  enforced state transitions.
- The factory registers every generation-owned closeable immediately after
  construction: `client_pool`, conditional `outbound_manager`,
  `missing_account_recovery`, `finalization_supervisor`, and `supervisor`.
- Abort closes callbacks in reverse registration order, continues after close
  failures, records bounded redacted diagnostics, and is idempotent.
- Abort after transfer is a true no-op and preserves `transferred`; the
  runtime manager alone owns retirement cleanup after publication.
- Pre-commit reload failures and cancellation use the shared abort owner with
  shielded bounded cleanup. Process-owned database, repositories, metrics
  coalescer, shared writers, runtime metrics, dashboard telemetry, update
  checker, and model-router affinity are not registered on candidates.
- Construction diagnostics preserve the original failure as the primary
  cause; no `cleanup_partial_generation` implementation remains.

Focused verification passed:

```text
uv run pytest \
  tests/unit/test_candidate_resource_ownership.py \
  tests/unit/test_generation_factory.py \
  tests/unit/test_runtime_manager.py \
  tests/unit/test_runtime_generation_retirement.py \
  tests/unit/test_reload_manager.py \
  tests/unit/test_reload_diagnostics_matrix.py \
  tests/integration/reload/ \
  tests/integration/test_rehash_streaming_swap.py \
  -q --tb=short --maxfail=1
557 passed in 90.86s

uv run pytest \
  tests/integration/reload/test_reload_resources.py::test_resource_cycle_does_not_accumulate \
  -q --tb=short --maxfail=1
1 passed in 3.69s

uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
728 files already formatted; Ruff clean; Pyright 0 errors; 14 smoke tests passed.
```

The 100-cycle plateau regression exercises four barriers (`on_candidate_started`,
`on_candidate_complete`, `on_reconcile_started`, and `on_publish_started`) and
asserts after every failed reload that the active generation and open
client/outbound/supervisor resource counts do not grow. Successful transfer
and later exactly-once retirement are covered by
`test_transition_ownership.py`, `test_retention_close_counts.py`, and the
runtime retirement suite.

The repository-wide run reached `7,967 passed, 45 skipped, 1 warning` and
stopped at the unrelated pre-existing failure
`tests/unit/test_wire_ir.py::test_anthropic_request_normalizes_system_and_tool_blocks`.
The failure expects a string while the current renderer returns a text-block
list; C005 changes no wire-IR code or tests.

## Dependency review

The direct successor, Phase 5 (`plans/006-phase-05-shared-runtime-generation-factory.md`),
is already `complete`, so closing C005 introduces no newly blocked work. Phase
6 is already `implemented`, Phase 7 is already `implemented`, and Phases 8–12
remain available implementation-handoff plans with no explicit blocked
status. No future plan status required changing.
