# Dispatch Stability Milestone G — Soak Validation, Rollout, and Operational Closure

Date: 2026-07-15
Status: detailed handoff plan
Roadmap: `plans/2026-07-15-long-running-dispatch-overhead-stability-roadmap.md`
Milestone: G of G
Depends on: Milestones A–F

## Objective

Prove that EggPool's dispatch latency, database queues, background tasks, runtime generations, memory, file descriptors, threads, HTTP clients, and WAL/storage behavior remain stable over long-running operation. Convert the proof into CI gates, SBC deployment guidance, rollout thresholds, and a clear rollback/runbook contract.

This milestone is not satisfied by a short benchmark showing improved median latency. The original failure mode is time-dependent. Acceptance therefore requires comparing warmed early windows with late windows under a stationary workload and demonstrating resource plateaus.

## Scope

### In scope

- Multi-hour deterministic soak tests.
- Accelerated long-duration maintenance/reload scenarios.
- Native, transcoded, streaming, retry, cancellation, dashboard, and background task workload matrix.
- Early-versus-late percentile stability checks.
- Queue, lock, database, WAL, event-loop, memory, FD, thread, and generation plateau checks.
- Failure injection and recovery.
- Raspberry Pi/SBC reference runs.
- Configuration profile recommendations.
- Operator diagnostics/runbook.
- Release/rollback gates and final acceptance report.

### Out of scope

- New architectural features unrelated to closure.
- Replacing the deterministic mock upstream with public provider benchmarking as a release dependency.
- Claiming universal absolute performance across all hardware/storage/provider combinations.
- Hiding unstable behavior through automatic process restarts.

## Target files and modules

Testing/tooling:

- existing performance/soak harness created in milestone A;
- new tests under `tests/soak/`, `tests/performance/`, or repository-standard equivalent;
- reusable local mock upstream server;
- optional scripts under `scripts/` for extended manual/reference-host runs;
- GitHub Actions workflow or scheduled workflow for bounded CI coverage;
- artifact summary generator.

Runtime diagnostics:

- `src/eggpool/runtime_metrics.py`
- dashboard runtime/operations pages;
- CLI stats/diagnostic commands if appropriate.

Documentation:

- `architecture/README.md`
- `docs/deployment.md`
- `docs/raspberry-pi.md`
- `config.example.toml`
- `CHANGELOG.md`
- optional `docs/operations/dispatch-stability.md`

## Workstream G1 — Define canonical workload profiles

Create deterministic workload profiles with fixed random seeds and local mock upstream behavior.

### Profile 1 — Low-volume steady native

- serial or low concurrency;
- native OpenAI-compatible requests;
- mixed streaming/non-streaming;
- dashboard polling at normal refresh cadence;
- normal background tasks.

Purpose: detect fixed overhead regressions and idle/steady resource growth.

### Profile 2 — Moderate sustained mixed traffic

- 5–10 concurrent streams;
- native OpenAI and Anthropic-compatible requests;
- OpenAI <-> Anthropic transcoding;
- thinking controls;
- compression/cache modes according to supported defaults;
- realistic request-size distribution.

Purpose: representative long-running deployment.

### Profile 3 — Burst and recovery

- repeated bursts of 25–100 dispatches depending on host capability;
- quiet recovery periods;
- varied stream lengths;
- dashboard polling during bursts.

Purpose: prove dispatch writer, finalization queue, and DB lock queues return to baseline.

### Profile 4 — Retry/health churn

- controlled 429, quota, 5xx, reset, and timeout responses;
- multiple accounts/providers;
- half-open circuit behavior;
- retries before stream;
- recovery to success.

Purpose: test backoff persistence, attempted-account semantics, health slots, and queue cleanup.

### Profile 5 — Cancellation-heavy streaming

- client cancellation before upstream headers;
- cancellation after headers;
- midstream disconnects;
- cancellation bursts while DB maintenance is active.

Purpose: prove finalization retry queue and reservation cleanup do not grow over time.

### Profile 6 — Maintenance backlog

- large synthetic request/event/trace/rollup tables;
- stale pending requests and expired reservations;
- accelerated bounded retention/reconciliation;
- WAL threshold checkpointing.

Purpose: prove maintenance drains without monopolizing dispatch.

### Profile 7 — Rehash churn

- repeated accepted no-op and changed-config rehashes;
- rejected invalid configs;
- active long streams across generation swaps;
- trace/metrics/task config transitions.

Purpose: prove process-owned writers are singular and generations retire.

### Profile 8 — Slow-storage simulation/reference SBC

- file-backed SQLite;
- constrained CPU/thread profile;
- optional fsync/latency injection where portable;
- actual Raspberry Pi or equivalent reference run.

Purpose: validate intended deployment class.

## Workstream G2 — Define measurement windows and stationarity

For each soak:

1. Start process and complete startup refresh/recovery.
2. Warm until connection pools, caches, JIT-free Python code paths, and initial background tasks settle.
3. Record an early steady-state window.
4. Continue the same stationary workload for the test duration.
5. Record one or more middle windows.
6. Record a late steady-state window.
7. Stop traffic and observe recovery/drain.
8. Shut down cleanly and verify final flush/closure.

Suggested extended reference run:

- warm-up: 30 minutes;
- early window: 30–90 minutes;
- middle windows: hourly;
- late window: final hour of a 6–24 hour run;
- recovery observation: 5–15 minutes;
- shutdown deadline: bounded by configured drain settings.

CI may use accelerated 10–30 minute profiles with shorter task intervals, but at least one scheduled/manual reference run should remain multi-hour.

Stationarity requirements:

- fixed request arrival distribution;
- fixed upstream response distribution;
- no silent increase in concurrency over time;
- fixed dashboard cadence;
- recorded config and seed;
- resource metrics sampled at a fixed cadence.

Do not attribute rising latency to EggPool if the harness itself increases load. Validate harness event-loop and CPU overhead separately.

## Workstream G3 — Required metrics

Collect at 10–30 second intervals, with bounded storage:

### Dispatch/request path

- total local pre-upstream p50/p95/p99;
- dispatch overhead p50/p95/p99;
- routing plan p50/p95/p99;
- selection claim wait/held p50/p95/p99;
- dispatch persistence queue wait p50/p95/p99;
- transaction/commit p50/p95/p99;
- upstream connect/header/TTFT distributions;
- request throughput and error/retry counts.

### SQLite/storage

- primary DB lock wait p50/p95/p99/max;
- transaction counts and operations by kind;
- DB/WAL/SHM sizes;
- checkpoint timing/result;
- page/freelist metrics where supported;
- maintenance backlog and oldest eligible age;
- finalization and dispatch durable row consistency checks.

### Queues/tasks

- dispatch writer queue depth/capacity/oldest age;
- observability queue depth/drops/oldest age;
- finalization retry queue depth/oldest age;
- metrics buffer pending/dropped/flushed;
- background task actual cadence, drift, duration, stopped reason, and row counts.

### Runtime resources

- RSS and optional USS/PSS where available;
- open file descriptors;
- OS thread count;
- task count;
- event-loop lag;
- active streams/requests;
- HTTP client/pool counts;
- DNS cache/singleflight size;
- active and retiring runtime generations;
- writer/supervisor identities/counts.

### Correctness invariants

Periodically query/assert:

- no orphan active reservation for finalized request;
- no pending request older than allowed threshold unless active stream is verified;
- no incomplete attempt for terminal request;
- router active counts match authoritative active reservation/request state within defined reconciliation tolerance;
- quota reservation state matches durable reservations;
- no duplicate attempt numbers per request;
- no duplicate process-owned writers/tasks.

## Workstream G4 — Stability assertions

Use relative and absolute guards.

### Required relative gates

After warm-up under a stationary workload:

- late dispatch p95 <= 1.20x early p95;
- late dispatch p99 <= 1.50x early p99;
- late DB lock-wait p95 <= 1.25x early p95, unless both are below a small floor such as 1 ms;
- late event-loop lag p95 <= 1.25x early p95;
- throughput does not decline by more than 10% absent resource saturation;
- queue depths show no positive monotonic trend and return to baseline after bursts;
- memory growth slope after warm-up is statistically/operationally negligible and within a fixed cap;
- FD/thread/task/generation counts plateau exactly or within documented bounded fluctuation;
- WAL size remains bounded by checkpoint/traffic policy rather than monotonic growth.

### Suggested absolute gates

Calibrate per host profile, but establish initial reference goals:

- selection claim-held p95 < 5 ms on CI/general host, < 15 ms on reference SBC;
- no selection claim held while waiting for DB;
- dispatch writer oldest age below enqueue timeout under sustainable load;
- observability drops zero under standard profile;
- finalization retry queue returns to zero after cancellation profile;
- no more than one active process-owned dispatch writer, trace writer, metrics flush task, checkpoint task, update checker, and backup scheduler;
- no retiring generation remains beyond configured drain timeout plus cleanup tolerance.

Avoid failing CI on noisy microsecond-level ratios when both values are trivial. Apply minimum floors and retain raw values.

## Workstream G5 — Resource plateau methodology

### Memory

Sample RSS and, where available, PSS/USS. Separate:

- startup growth;
- cache warm-up;
- active stream payload buffering;
- post-burst recovery baseline;
- generation overlap during rehash;
- late steady state.

Use linear regression or robust slope over the post-warm-up baseline samples. Also enforce a maximum late-minus-early delta.

Investigate retained object classes with `tracemalloc` only in dedicated diagnostic runs; do not enable it in normal performance runs because it changes overhead.

### File descriptors/sockets

Count total FDs and classify where `/proc` permits. Verify:

- stream cancellations close upstream responses;
- retired clients close sockets;
- DNS/outbound managers close on generation retirement;
- log/PID/control socket descriptors remain bounded;
- SQLite connections remain at configured count.

### Threads/tasks

Track OS threads and asyncio tasks. Ensure:

- aiosqlite worker threads match connections;
- Granian runtime threads match config;
- writer/task supervisors are not duplicated;
- completed tasks are not retained in unbounded diagnostics lists.

### Generations

During repeated rehash, record active and retiring generations. The count may temporarily rise with long streams but must fall after drain/timeout. Record close errors and force-close events.

## Workstream G6 — Failure injection matrix

Inject failures at controlled points:

- dispatch queue saturation;
- writer transaction failure;
- ambiguous commit outcome;
- observability writer failure;
- maintenance transaction cancellation;
- metrics flush cancellation;
- SQLite busy/slow transaction;
- checkpoint busy result;
- upstream connect/read/write timeout;
- client disconnect;
- runtime generation retirement timeout;
- task supervisor tick exception;
- disk-full-like SQLite error where safely simulated;
- invalid rehash config and rejected process-owned setting transition.

For each failure assert:

- request error is explicit and protocol-correct;
- no upstream dispatch without durable state;
- no leaked health/active/quota reservation state;
- queues recover or fail readiness according to policy;
- process does not require restart unless the failure class is declared fatal;
- diagnostics retain bounded error information;
- later valid requests recover.

## Workstream G7 — Database consistency audit command/test helper

Add a read-only diagnostic helper or test utility that checks lifecycle invariants without mutating the database. Potential checks:

- pending request with no active attempt;
- active reservation for non-pending request;
- incomplete attempt for terminal request;
- duplicate attempt numbers;
- request selected account mismatch with latest attempt where semantics require match;
- orphan routing trace/observability rows;
- stale backoff rows;
- rollup/request count reconciliation within lossy-policy expectations.

The operator-facing command, if exposed, must redact sensitive fields and support JSON output for soak automation.

Use the read-only stats connection where possible.

## Workstream G8 — CI strategy

### Per-PR gates

Keep duration bounded:

- scheduler three-tick regression;
- blocked-DB deconvoy concurrency test;
- dispatch writer batch/cancellation/failure tests;
- trace off-path test;
- bounded maintenance row-limit test;
- 10–20 minute accelerated mixed soak if CI budget permits;
- resource count assertions with generous platform-aware tolerance.

### Scheduled/nightly gates

- 1–3 hour mixed soak;
- rehash/cancellation/maintenance profiles;
- SQLite file-backed artifacts;
- early/late stability comparison;
- retained compact JSON/CSV summaries and logs on failure.

### Manual/reference gates

- 6–24 hour run on Raspberry Pi 4/5 or equivalent SBC;
- microSD and SSD-backed comparison where available;
- production-like config with secrets replaced by mock provider credentials;
- thermal throttling/CPU frequency recorded if available.

Do not make public-provider availability or billing a CI dependency.

## Workstream G9 — Configuration profiles

Produce evidence-based profiles in docs/config examples.

### Balanced default

Likely characteristics, subject to benchmarks:

- one Granian worker;
- supported runtime thread count from milestone F;
- two database worker threads/connections for file-backed DB: primary + read-only stats;
- WAL + synchronous NORMAL;
- dispatch writer enabled with conservative bounded queue/microbatch;
- routing traces sampled, score components off;
- balanced metrics flush;
- bounded maintenance defaults.

### Minimum-footprint SBC

- one runtime thread if supported/recommended;
- optionally one DB connection if operator accepts dashboard contention, with clear warning;
- low-wear metrics/trace settings;
- smaller queues/batches;
- conservative maintenance budgets;
- longer dashboard refresh/cache.

### Full diagnostics

- full routing traces and detailed span sampling 100%;
- documented increased write/CPU cost;
- recommended faster storage;
- not the default SBC profile.

### High-concurrency general host

- supported runtime threads only;
- larger writer queues/batches based on evidence;
- same single worker/process-owned state;
- faster storage recommendation;
- explicit limits beyond which SQLite single-writer throughput is expected to saturate.

## Workstream G10 — Operator runbook

Document a diagnostic sequence for rising dispatch overhead:

1. Confirm metric boundary and compare local pre-upstream versus upstream connect/TTFT.
2. Inspect selection claim wait/held.
3. Inspect dispatch writer queue wait/depth/oldest age.
4. Inspect DB lock-wait percentiles and current transaction/task.
5. Inspect maintenance tick duration/stopped reason/backlog.
6. Inspect finalization retry queue and stale pending counts.
7. Inspect event-loop lag and CPU/thermal pressure.
8. Inspect DB/WAL size/checkpoint status/storage latency.
9. Inspect retiring generations, FDs, threads, clients, and DNS state.
10. Apply safe mitigations in priority order.

Safe mitigations may include:

- pause/defer low-priority maintenance through documented controls;
- reduce full trace/detail settings;
- ensure separate stats connection;
- reduce unsupported runtime thread count;
- perform manual checkpoint during low traffic;
- reduce dashboard polling;
- move database to healthier/faster storage;
- restart only as a last resort after collecting diagnostics.

Include explicit warnings not to disable correctness-critical dispatch persistence or finalization.

## Workstream G11 — Release and rollback criteria

Release requires:

- all milestone acceptance criteria complete;
- per-PR and scheduled soak gates passing;
- reference SBC run passing stability/resource bounds;
- no unresolved correctness invariant failures;
- runtime diagnostics and docs complete;
- upgrade/rehash behavior tested from prior release config/database;
- schema migrations reversible by backup/restore policy;
- changelog and operator notes.

Rollback triggers:

- late-window dispatch/DB latency exceeds stability ratios consistently;
- monotonic writer/finalization queue growth;
- memory/FD/thread/task/generation leak;
- upstream dispatch before durable commit;
- duplicate/missing reservation or attempt state;
- rehash duplicates process-owned writer/task;
- cancellation leaves durable pending state beyond safety window;
- maintenance starves or blocks dispatch beyond configured budget;
- supported runtime thread profile produces loop-affinity failures.

Rollback plan must identify which milestone feature can be disabled/reverted without weakening correctness. Prefer disabling lossy trace/detail features first. A dispatch writer rollback must return to the shared repository bundle direct path and preserve durability.

## Workstream G12 — Final acceptance report

Commit a concise report under `plans/` or `docs/operations/` containing:

- implementation commit range;
- hardware/software/storage/config matrix;
- workload seeds and durations;
- early/middle/late percentile tables;
- queue/resource plateau plots or compact tables;
- failure injection results;
- correctness audit results;
- rehash/resource ownership results;
- chosen default profiles;
- known limits and residual risks;
- release recommendation.

Do not commit secrets, raw prompts, provider credentials, or excessively large artifacts.

## Test plan

The milestone itself is primarily integration, performance, and operational testing. It must run the full functional suite plus the workload/failure matrices above.

Minimum automated closure scenarios:

1. Six-window stationary mixed soak with early/late comparison.
2. Burst queue recovery.
3. Cancellation burst with finalization queue recovery.
4. Maintenance backlog drain under request load.
5. Ten or more rehashes with active streams and resource retirement.
6. Dispatch writer saturation fail-closed behavior.
7. Trace writer overload with zero request failures.
8. Ambiguous commit reconciliation.
9. Multi-thread profile validation according to milestone F decision.
10. Clean shutdown with queues drained or explicitly bounded drops.

## Acceptance criteria

1. All milestone A–F acceptance criteria are implemented and passing.
2. Stationary multi-hour workload meets the early/late dispatch p95 and p99 stability ratios.
3. DB lock wait, event-loop lag, throughput, and queue metrics meet declared stability bounds.
4. Dispatch, observability, metrics, and finalization queues remain bounded and recover after bursts.
5. RSS, FDs, threads, tasks, clients, DNS state, and generations plateau.
6. DB/WAL sizes remain bounded by retention/checkpoint policy.
7. Correctness consistency audit reports no unresolved lifecycle violations.
8. Maintenance backlogs drain without transactions exceeding configured budgets.
9. Rehash does not duplicate process-owned writers/tasks and retiring generations close by deadline.
10. Failure injection recovers according to policy without unsafe upstream dispatch or leaked accounting state.
11. Supported runtime-thread profiles are validated; unsupported profiles are rejected or clearly warned.
12. Reference SBC run passes or documented defaults are adjusted until it does.
13. Operator runbook, config profiles, architecture docs, and changelog are complete.
14. Full tests, ruff, format check, and pyright pass.
15. Final acceptance report is committed with reproducible configuration and workload metadata.

## Handoff evidence

The implementing/release agent should provide:

- CI/scheduled/manual soak links or artifact references;
- compact early/late metrics table;
- queue/resource plateau evidence;
- consistency audit output;
- rehash identity/retirement evidence;
- failure injection matrix;
- selected default/profile values;
- known saturation limits;
- release or rollback recommendation.

## Exit condition

Milestone G and the broader roadmap are complete when EggPool demonstrates stable late-window dispatch behavior under representative long-running load, all queues and resources remain bounded, SQLite maintenance and WAL behavior are controlled, rehash does not duplicate ownership, and operators have sufficient diagnostics and runbooks to identify and mitigate future contention without relying on periodic hard restarts.