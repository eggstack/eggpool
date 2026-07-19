# Phase 12 — CI Partitioning, Fault Matrix, Soak Validation, and Performance Closure

Date: 2026-07-19
Status: implementation handoff
Roadmap: `plans/001-reload-correctness-performance-roadmap.md`
Prerequisites: Phases 1–11.

## Objective

Close the reload/lifecycle/performance line of work with continuously enforced correctness invariants, efficient CI partitioning, complete fault injection, long-running mixed-workload evidence, and targeted optimizations backed by measurements.

This phase is not a general performance rewrite. It validates that the preceding phases remain correct under repetition, concurrency, streaming, cancellation, control-plane traffic, and constrained-resource conditions. Only then should narrowly scoped optimizations be accepted.

## Problems addressed

The current CI shape can run an unrestricted pytest suite and then run soak/performance categories again, increasing wall-clock time and flakiness without improving coverage. Supported Python versions are not necessarily exercised as an explicit matrix. Core reload concurrency and XDG behavior have also been represented by expected failures or skips rather than strict gates.

A successful unit test pass is insufficient for lifecycle work. Rehash defects often emerge only after repeated failures, overlapping streams, resource retirement, and sustained SQLite pressure.

## Non-goals

- Do not add unbounded or multi-hour tests to every pull request.
- Do not use unstable microbenchmarks as hard correctness gates.
- Do not optimize by weakening persistence durability, queue bounds, or lifecycle safety.
- Do not introduce Rust extensions unless profiling after all Python-level fixes identifies a remaining CPU-bound hotspot with a compelling maintenance case.
- Do not claim roadmap closure while core tests remain skipped or non-strict xfailed.

## Workstream A — CI partitioning

### Required job groups

Create distinct jobs for:

1. formatting and lint;
2. type checking;
3. core unit tests;
4. normal integration tests;
5. reload transaction/control-plane tests;
6. bounded performance-contract tests;
7. short soak/resource tests;
8. consistency/audit tests;
9. supported Python version matrix.

Jobs may be combined where startup overhead dominates, but logical suites must not be run twice unintentionally.

### Marker policy

Define explicit markers such as:

- `integration`;
- `reload`;
- `performance`;
- `soak`;
- `slow`;
- `network` if applicable.

The normal test command must explicitly exclude dedicated performance/soak suites. Dedicated jobs must select them exactly once.

Document canonical commands in contributor/developer documentation and CI comments.

### Python matrix

Run correctness suites on every supported interpreter declared by the package, currently Python 3.11 and 3.12 unless metadata changes before implementation.

A practical split:

- full core correctness on both versions;
- expensive short soak/performance on one documented primary version;
- optional periodic extended soak on the other version or latest supported interpreter.

Do not rely on the runner’s implicit Python version.

### Workflow concurrency and artifacts

- cancel superseded workflow runs where appropriate;
- upload bounded test logs and benchmark summaries on failure;
- preserve resource/latency summaries as artifacts;
- avoid uploading secrets, full configs, or oversized raw databases;
- make failures attributable to one logical suite.

## Workstream B — Eliminate coverage exemptions

Convert all roadmap-relevant `skip`, `xfail`, and non-strict expected failures into strict passing tests.

At minimum:

- concurrent reload admission;
- control-client concurrency/hang reproduction;
- XDG runtime/state isolation;
- candidate cleanup/resource plateau;
- transactional fault matrix;
- retirement task hygiene;
- dispatch-writer selection;
- readiness no-write behavior.

Add a CI audit that rejects new non-strict xfails or broad skips in the reload/control/runtime test areas unless an explicit allowlist entry includes rationale and expiry/removal criteria.

## Workstream C — Complete fault-injection matrix

Use Phase 1 infrastructure to exercise every stage and major sub-operation.

### Preparation faults

- config read/parse;
- validation;
- semantic diff;
- provider/account delta preparation;
- each generation resource construction step;
- persisted backoff hydration;
- process transition preflight;
- writer transition preflight.

### Commit faults

- database transaction begin;
- each persistence delta operation;
- active-generation revalidation;
- runtime publication;
- ownership transfer;
- each process transition apply;
- effective-state update;
- database commit;
- retirement scheduling.

### Cleanup/compensation faults

- each candidate close callback;
- process transition rollback;
- persistence rollback/inverse delta;
- runtime rollback if implemented;
- operational-event persistence;
- retirement close;
- shutdown while cleanup is active.

### Cancellation and shutdown

Inject cancellation or shutdown at every barrier. Assert the documented Phase 6 rule:

- before commit point: complete abort to old state;
- after commit point: complete or compensate under bounded shielding;
- never expose mixed state.

For each case compare the complete Phase 1 snapshot across runtime, database, tasks, writers, effective configuration, resources, and diagnostics.

## Workstream D — Short pull-request soak

Create a bounded test suitable for normal CI, targeting approximately minutes rather than hours.

Suggested workload:

- hundreds of successful no-op and semantic reload checks;
- at least 100 successful generation-changing reloads;
- at least 100 injected pre-publication failures;
- concurrent short and long-lived streams;
- readiness/dashboard polling;
- dispatch-writer load;
- provider success, timeout, and backoff scenarios;
- periodic cancellations;
- control-plane busy attempts;
- repeated generation retirement.

Use deterministic seeds and record them on failure.

Track early, middle, and late windows for:

- RSS and Python allocator metrics where available;
- file descriptor count;
- thread count;
- EggPool-owned task count;
- active/retiring generation count;
- open client/resource count;
- dispatch overhead;
- SQLite lock wait;
- writer queue depth and batch size;
- finalization retry depth;
- request consistency counts.

## Workstream E — Extended scheduled soak

Add a scheduled/manual workflow for longer validation. Suggested duration: 30–120 minutes, adjusted to CI budget.

Include:

- thousands of mixed reload attempts;
- sustained concurrent dispatch and streaming;
- repeated provider-account changes;
- database checkpoint/maintenance overlap;
- control reconnects and stale-socket cleanup;
- forced retirement deadlines;
- writer queue pressure;
- periodic invalid configs;
- simulated process shutdown/restart recovery if durable reload intent was added.

The scheduled workflow should publish a compact summary artifact and fail on clear monotonic resource growth or invariant violation.

## Resource plateau criteria

Define tolerances before running tests. Example principles:

- tasks, open clients, and retiring generations must return exactly to baseline after quiescence;
- descriptors may have a small documented fixed warm-up delta but no positive slope in late windows;
- RSS may plateau above startup due to allocator behavior, but late-window growth slope must remain within a documented bound;
- database row counts must match completed request/reload semantics;
- writer queue must drain after load stops;
- no unobserved task exception is permitted.

Avoid brittle exact RSS equality. Use repeated-window medians and slope/plateau checks.

## Consistency audits

After each soak, run deterministic audits for:

- orphan request/reservation/attempt rows;
- duplicate request identifiers;
- provider/account rows inconsistent with active committed config;
- reload-intent rows stuck in intermediate states;
- active generation digest inconsistent with committed effective config;
- process task specs inconsistent with active config;
- writer accepted bundles missing persistence;
- unclosed generation resources;
- retirement entries with no task or vice versa.

## Performance baseline

Capture before/after results using one reproducible fixed workload and environment description.

Metrics:

- dispatch overhead p50/p95/p99;
- local pre-upstream overhead p50/p95/p99;
- SQLite primary lock wait p50/p95/p99;
- request throughput;
- TTFT and stream completion latency where relevant;
- CPU utilization;
- RSS;
- event-loop lag;
- dispatch-writer queue wait and batch size;
- reload prepare, commit, and total latency;
- readiness latency under polling.

Run enough repetitions to report median and variability. Performance contracts should detect major regressions rather than fail on normal host noise.

## Targeted optimization pass

Only after correctness and baseline evidence, profile the remaining hot paths.

Candidate optimizations:

- avoid rebuilding unchanged catalog/provider structures on routing-only reloads;
- reuse immutable process-owned lookup data;
- batch persisted backoff hydration rather than per-row lookups;
- calculate provider/account deltas without repeated normalization;
- reduce lock scope around runtime metadata snapshots;
- precompute task-spec and routing-policy transitions;
- move nonessential warm-up after publication only when safe under a generation lease;
- reduce detailed tracing allocation when sampling excludes a request;
- tune dispatch-writer batch size/delay for SBC and server profiles;
- remove duplicate JSON serialization or database reads identified by profiling.

Every optimization must retain:

- a full-rebuild fallback;
- semantic equivalence tests;
- resource ownership invariants;
- measured improvement or justified simplification.

## Optional SBC validation

Because EggPool targets smaller systems, add a documented constrained profile or manual validation:

- one or two CPU cores;
- limited memory;
- file-backed SQLite on realistic storage;
- bounded concurrency;
- compression/transcoding workloads where relevant.

This may run outside required PR CI, but results should inform writer/probe/reload defaults.

## Documentation and operator closure

Update:

- architecture documentation for process/generation/transaction ownership;
- rehash command semantics and result categories;
- restart-required configuration fields;
- control socket/runtime path behavior;
- readiness probe freshness;
- dispatch-writer diagnostics;
- troubleshooting for stuck retirement or compensation failure;
- canonical test and benchmark commands.

Remove obsolete comments describing retirement as non-blocking if implementation differs, old `app.state` mirror documentation, and historical expected-failure notes.

## Implementation sequence

1. Inventory current CI commands and marker behavior.
2. Partition workflows and add explicit Python matrix.
3. Add skip/xfail audit and close all roadmap exemptions.
4. Complete the stage-by-stage fault matrix.
5. Implement short deterministic soak and consistency audit.
6. Implement scheduled/manual extended soak.
7. Define resource plateau tolerances and artifact summaries.
8. Capture fixed-load performance baseline.
9. Profile remaining hot paths.
10. Apply only measured, parity-tested optimizations.
11. Run SBC/constrained validation where available.
12. Update architecture, operations, and testing documentation.

## Acceptance criteria

- Every logical test category runs exactly once per intended CI job.
- Python 3.11 and 3.12 correctness jobs pass, or the package metadata is deliberately updated with justification.
- No core reload/runtime/control invariant remains skipped or non-strict xfailed.
- Fault injection covers every transaction, cleanup, compensation, cancellation, and shutdown stage.
- Every fault yields complete old state or complete new state.
- Short soak completes with no leaked task, client, descriptor trend, writer item, or retiring generation.
- Extended soak demonstrates stable late-window resource and latency plateaus.
- Consistency audits report no orphan, duplicate, stuck-intent, or active-config mismatch.
- Dispatch writer remains selected and batching under load.
- Readiness polling creates no write transactions.
- Reload latency is separated into prepare/commit/retirement scheduling and remains bounded.
- Performance changes are backed by reproducible before/after evidence and show no capability regression.
- Architecture and operator documentation matches final implementation.

## Final roadmap closure gate

Do not mark the roadmap complete until all twelve roadmap invariants in `plans/001-reload-correctness-performance-roadmap.md` are represented by strict automated tests or documented scheduled evidence.

## Handoff evidence

The implementing agent should attach or commit:

- final CI job/marker matrix;
- list of removed skips/xfails;
- fault-injection coverage table;
- short and extended soak summaries;
- resource plateau tables/plots or compact artifacts;
- database consistency audit output;
- fixed-load before/after performance report;
- constrained/SBC observations if available;
- documentation files updated;
- explicit statement of any remaining risk that prevents full closure.