# Dispatch Stability Closure Pass — Evidence, Policy Alignment, and Release Closure

Date: 2026-07-17
Status: detailed handoff plan
Roadmap: `plans/2026-07-15-long-running-dispatch-overhead-stability-roadmap.md`
Depends on: Dispatch Stability Milestones A–G

## Objective

Close the remaining gaps in the long-running dispatch stability roadmap without expanding the architecture or reopening completed milestones. The core scheduler, lock de-convoying, durable write pipeline, off-path observability, bounded maintenance, and hot-path hardening work is already present. This pass must convert that implementation into defensible release evidence, resolve operator-facing policy contradictions, add the final concurrency-specific regression coverage, and leave an explicit closure record.

The closure pass is complete only when EggPool has reproducible evidence that dispatch performance and resource usage remain stable over time on realistic file-backed SQLite storage, the documented runtime-thread recommendations match the supported safety model, hosted validation is visible and green, and all remaining acceptance claims are traceable to tests or retained artifacts.

## Current state and closure gaps

The implementation presently provides:

- corrected one-time scheduler initial-delay semantics and cadence diagnostics;
- distinct local pre-upstream and coordinator dispatch timing boundaries;
- selection-claim lock de-convoying with durable persistence outside the claim lock;
- a process-owned bounded dispatch persistence writer with batching, cancellation handling, and reconciliation;
- a process-owned routing trace writer with nonblocking submission and pressure-aware degradation;
- budgeted, chunked, starvation-safe maintenance and SQLite diagnostics;
- event-loop lag, queue, resource, and runtime topology instrumentation;
- broad unit, integration, performance, failure-injection, and short soak coverage;
- workload profiles, consistency auditing, deployment profiles, and an operator runbook.

The remaining gaps are:

1. Strict early-versus-late stability gates are no longer enforced by ordinary CI and there is no retained multi-hour file-backed soak evidence proving the roadmap's time-dependent acceptance criteria.
2. Documentation recommends `server.threads = 4` or `8` in some profiles while runtime hardening treats `threads = 1` as the conservative supported default and warns on higher values.
3. The three-phase selection claim flow needs a focused slow-writer burst test proving that durable-before-publication overlap does not create unacceptable transient account-selection skew.
4. Current hosted CI status and test collection are not clearly evidenced at repository head.
5. The final roadmap closure status is not recorded in a concise, auditable report tied to artifacts and exact acceptance gates.

## Non-goals

- Replacing SQLite.
- Rewriting the dispatch writer, selection state machine, routing trace writer, or task supervisor.
- Introducing additional worker processes or a new distributed coordination layer.
- Adding public-provider benchmarking as a release dependency.
- Optimizing absolute throughput before long-duration stability is proven.
- Masking instability with watchdog restarts.
- Treating documentation-only claims as evidence.

## Required deliverables

1. A reproducible extended-soak runner suitable for local Linux, CI runners, and Raspberry Pi-class hosts.
2. Machine-readable and human-readable soak summaries retained as artifacts.
3. Dedicated strict stability assertions that run only in the extended-soak mode.
4. A slow-writer burst fairness regression suite.
5. Corrected runtime-thread policy and deployment profiles.
6. Hosted CI workflow/status coverage for bounded validation and an explicit optional scheduled extended run.
7. A final closure report under `plans/` or `docs/validation/` linking each roadmap acceptance criterion to evidence.

## Workstream 1 — Establish a reproducible extended-soak entry point

### 1.1 Add a first-class runner

Add a script such as:

- `scripts/run_dispatch_stability_soak.py`, or
- `scripts/run_dispatch_stability_soak.sh` backed by a Python harness.

The runner must:

- use file-backed SQLite in a fresh temporary or explicitly selected directory;
- create separate database, WAL, SHM, logs, and artifact paths;
- use deterministic seeds;
- start EggPool as a real process using the supported production server path rather than only constructing the coordinator in-process;
- start a deterministic local mock upstream server;
- exercise dashboard/runtime metrics polling at a fixed cadence;
- support clean shutdown and forced cleanup after timeout;
- capture the exact git SHA, Python version, Granian version, OS/kernel, architecture, CPU count, memory, storage path/type where detectable, and effective EggPool config;
- redact credentials and never persist request content or provider secrets;
- return nonzero on correctness failure, stability-gate failure, harness failure, or incomplete artifact generation.

### 1.2 Define explicit profiles

At minimum support:

- `balanced-file-backed`: representative sustained mixed traffic;
- `burst-recovery`: repeated high-concurrency bursts and drain periods;
- `cancellation-maintenance`: cancellations while retention/reconciliation runs;
- `rehash-churn`: active streams across repeated valid and rejected rehash attempts;
- `slow-storage`: injected or naturally constrained SQLite write latency;
- `sbc-reference`: conservative one-thread, one-DB-worker profile.

Profiles must be versioned data or code, not undocumented command-line combinations.

### 1.3 Define duration modes

Provide clearly separated modes:

- `smoke`: 2–5 minutes, developer-only harness verification;
- `ci`: 10–30 minutes, bounded correctness and drain validation;
- `nightly`: 1–3 hours, file-backed early/late comparison;
- `reference`: 6–24 hours, release evidence on representative hardware.

Ordinary unit/per-PR CI must not accidentally invoke nightly or reference durations.

### 1.4 Preserve stationarity

The runner must record and enforce:

- fixed arrival-rate or concurrency distribution;
- fixed upstream delay/error distribution;
- fixed dashboard polling frequency;
- fixed configuration after warm-up except in the dedicated rehash profile;
- fixed metric sampling cadence;
- no increasing dataset preload or workload size between early and late windows;
- harness event-loop lag and process CPU sufficient to detect a harness bottleneck.

A run whose load generator cannot maintain the configured schedule must be marked invalid rather than interpreted as EggPool throughput degradation.

## Workstream 2 — Implement strict extended-soak gates

### 2.1 Separate framework tests from evidence gates

Retain short CI tests that validate:

- metric collection;
- percentile calculation;
- queue drain;
- reservation and pending-request cleanup;
- bounded resource counters;
- no monotonic queue growth over repeated short cycles.

Add a separate strict gate module or runner path, for example:

- `tests/soak/test_extended_stability_gates.py`, marked `extended_soak`; or
- gate evaluation inside the artifact summarizer.

Strict gates must not silently skip because values are inconvenient. They may declare a run invalid when sample count, stationarity, or duration requirements are not met.

### 2.2 Required relative gates

After warm-up, compare equivalent early and late windows:

- late dispatch-overhead p95 <= 1.20x early p95;
- late dispatch-overhead p99 <= 1.50x early p99;
- late local-pre-upstream p95 <= 1.20x early p95;
- late SQLite lock-wait p95 <= 1.25x early p95 unless both windows are below a documented trivial floor;
- late event-loop-lag p95 <= 1.25x early p95 unless both are below a documented trivial floor;
- throughput decline <= 10% under unchanged offered load and without host saturation;
- post-warm-up RSS slope remains within a documented host-profile cap;
- queue depth, oldest-item age, FD count, thread count, task count, active generations, and WAL size do not exhibit unbounded positive trends.

Use confidence-aware handling for noisy percentiles:

- require minimum sample counts;
- record raw values and ratios;
- use absolute floors only to avoid meaningless microsecond ratios;
- never replace a failed strict gate with a wider hidden CI threshold;
- allow profile-specific absolute caps only when committed and documented.

### 2.3 Required absolute invariants

Every extended run must assert:

- no DB transaction overlaps `selection_claim_lock` held intervals;
- dispatch writer queue returns to zero or the documented idle baseline after traffic stops;
- routing trace queue returns to zero or the documented idle baseline after traffic stops;
- finalization retry queue returns to zero;
- no pending requests remain beyond the finalization tolerance;
- no active reservations remain after drain;
- no leaked health slots or runtime active counts remain;
- no more than one process-owned dispatch writer, trace writer, metrics flush task, checkpoint task, update checker, and backup scheduler exists;
- all retiring runtime generations close within configured timeout plus tolerance;
- the consistency auditor reports zero unwaived lifecycle violations;
- shutdown completes within the configured bounded deadline;
- no unhandled task exceptions or coroutine-leak warnings occur.

### 2.4 Gate evaluator output

Generate:

- `summary.json` with schema version, environment, config fingerprint, windows, raw metrics, ratios, gate status, and failure reasons;
- `summary.md` with a compact operator-readable table;
- time-series CSV or JSONL for dispatch, DB, queues, resources, and generations;
- process logs;
- consistency-audit output;
- a manifest with SHA-256 checksums for all artifacts.

The evaluator must make pass/fail deterministic from the recorded artifact set so a reviewer can rerun evaluation without rerunning the soak.

## Workstream 3 — Add slow-writer burst fairness and publication-overlap tests

### 3.1 Targeted risk

Milestone B intentionally performs durable persistence outside the selection claim lock and publishes runtime counters afterward. Under a slow writer and a burst of concurrent selectors, several claims may be durable but not yet published. The state machine and compensation paths protect correctness, but the repository needs explicit evidence that this overlap does not cause unacceptable concentration on one account or bypass quota/fairness intent.

### 3.2 Deterministic unit/integration harness

Add a test seam that can:

- block or delay dispatch writer commit completion;
- release commits in a controlled order;
- launch 25–100 simultaneous selection attempts;
- use multiple equivalent accounts with known quota/health state;
- record claim choice, durable commit order, publication order, retries, compensation, and final router/quota state.

The test must not depend on arbitrary sleeps. Use barriers/events around claim, enqueue, commit, and publication phases.

### 3.3 Required assertions

Prove:

- the claim lock is not held while waiting for the writer;
- unrelated selectors continue to enter Phase A while another claim waits for persistence;
- account selection remains within a documented fairness bound during the overlap window;
- the bound is stated in terms appropriate to the routing strategy, not an artificial perfect round-robin requirement if the strategy is quota-weighted;
- no account exceeds a hard concurrency/quota limit because publication is delayed;
- failed persistence releases claim/health state exactly once;
- failed post-commit publication invokes compensation exactly once;
- ambiguous commits reconcile without duplicate attempts or duplicate reservations;
- after all completions, durable and runtime state converge exactly.

If the test reveals that runtime publication lag can violate a hard constraint, implement the smallest corrective mechanism, such as an in-memory pending-claim debit visible to subsequent Phase A selections. Do not re-expand the lock across SQLite persistence.

### 3.4 Performance guard

Add a contention test confirming that the correction, if needed, does not recreate the original convoy:

- blocked writer latency must not materially increase claim-lock-held p95;
- claim throughput must remain substantially higher than the pre-Milestone-B serialized design;
- pending-claim accounting must be bounded and cleared on every terminal path.

## Workstream 4 — Reconcile runtime-thread support and documentation

### 4.1 Establish one authoritative policy

Audit:

- `src/eggpool/models/config.py` defaults;
- startup warnings in `src/eggpool/app.py` or server startup code;
- `config.example.toml`;
- `docs/config-profiles.md`;
- `docs/deployment.md`;
- `docs/raspberry-pi.md`;
- `README.md`;
- architecture and agent guidance.

Select and document one policy:

**Preferred closure policy:**

- `server.threads = 1` is the supported default and recommended production/SBC value;
- values greater than one are experimental until a real multi-loop topology matrix is proven;
- high concurrency is primarily handled by asyncio concurrency, HTTP connection-pool sizing, and bounded writers rather than multiplying event loops;
- process-owned async primitives must not be shared unsafely across loops.

Do not describe four- or eight-thread profiles as validated unless evidence demonstrates their safety and performance.

### 4.2 Correct profiles

Update profiles so that:

- Balanced Default uses one runtime thread unless evidence supports otherwise;
- Minimum-Footprint SBC remains one thread;
- Full Diagnostics remains one thread by default, with an explicit experimental override note if useful;
- High-Concurrency General uses one thread by default and scales connection/queue/maintenance settings independently;
- any multi-thread variant is labeled experimental and includes exact validation prerequisites.

Remove unsupported statements such as approximate SQLite write ceilings unless benchmark provenance and hardware/storage context are retained.

### 4.3 Add policy tests

Tests must verify:

- default config resolves to the authoritative thread count;
- startup emits the intended warning or error for unsupported/experimental topology;
- example profiles parse successfully;
- documentation snippets remain synchronized with config field names;
- live rehash correctly treats runtime thread changes as restart-required;
- runtime metrics accurately report effective worker/thread topology.

### 4.4 Optional multi-thread evidence

A multi-thread experiment may be retained, but it cannot block closure. If run, verify:

- process-owned writers are not awaited or mutated across incompatible loops;
- no `attached to a different loop` failures occur;
- metrics and diagnostics remain coherent;
- shutdown closes every loop-owned resource;
- performance is better than the one-thread baseline for the target workload.

Absent all of these, retain one-thread support guidance.

## Workstream 5 — Hosted CI and test collection closure

### 5.1 Verify collection

Add or run explicit collection checks for:

- `tests/unit/test_periodic_cadence.py`;
- selection claim and lock-scope suites;
- dispatch writer suites;
- routing trace writer/guard suites;
- maintenance budget suites;
- runtime topology and hot-path equivalence suites;
- `tests/soak/` short validation suites;
- strict extended-soak tests under their dedicated marker.

The CI log must show collected counts rather than relying only on documentation test counts.

### 5.2 Per-commit required workflow

The standard hosted workflow should run:

- formatting check;
- Ruff;
- Pyright;
- unit and integration suites;
- bounded performance-contract tests that assert invariants, not unstable wall-clock microbenchmarks;
- short soak framework/drain tests;
- configuration profile parsing;
- consistency auditor tests.

Pin deterministic environment variables already used by the project, including timezone and hash seed.

### 5.3 Scheduled/manual extended workflow

Add a workflow-dispatch and, where runner budget permits, scheduled workflow that:

- invokes `nightly` mode;
- uses file-backed SQLite;
- uploads the complete artifact bundle even on failure;
- has explicit timeout and cleanup steps;
- records runner hardware metadata;
- does not use public provider credentials;
- does not mark ordinary PRs failed solely because an external/self-hosted reference runner is unavailable.

### 5.4 Status visibility

Ensure the repository exposes current checks on the head commit. If branch protection is used, document which checks are required. The final closure report must identify the exact successful workflow run or retained local/reference evidence when hosted extended execution is not available.

## Workstream 6 — Execute the validation matrix

### 6.1 General Linux reference run

Run at least one 1–3 hour `nightly` soak on a general Linux host with:

- one runtime thread;
- file-backed SQLite on SSD/NVMe or documented storage;
- balanced default profile;
- moderate sustained mixed traffic;
- periodic burst/recovery intervals;
- dashboard polling;
- normal maintenance cadence;
- at least one valid live rehash during active streams.

This run must pass all strict relative and absolute gates.

### 6.2 Slow-storage run

Run at least one 1–3 hour constrained-storage profile using either:

- actual slower storage;
- a documented SQLite/file-system latency injection seam; or
- an SBC with microSD/USB storage.

Required outcomes:

- queues remain bounded;
- claim-lock-held latency remains flat while persistence wait rises;
- maintenance defers/yields according to policy;
- trace degradation, if any, is explicit and does not affect dispatch;
- all queues drain and consistency checks pass.

### 6.3 SBC reference run

Run at least one 6-hour reference soak on Raspberry Pi 4/5 or comparable ARM SBC when available.

Record:

- board/model and RAM;
- OS/kernel and Python architecture;
- storage type;
- thermal/throttling state where available;
- effective configuration;
- CPU, RSS, threads, FDs, event-loop lag, DB/WAL sizes, and latency windows.

If an SBC run cannot be obtained during this pass, do not claim SBC validation. Mark it as an explicit post-release evidence gap and keep conservative one-thread guidance. General-host closure may still proceed if all non-SBC acceptance criteria pass.

### 6.4 Failure-injection run

Execute at least these faults during bounded validation:

- writer transaction failure;
- ambiguous commit reconciliation;
- queue saturation/backpressure;
- client cancellation before and after upstream headers;
- routing trace writer flush failure;
- maintenance cancellation;
- SQLite busy/slow transaction;
- invalid rehash rejection;
- runtime generation retirement timeout.

For every injected failure, prove subsequent valid requests recover without process restart unless the failure is explicitly classified fatal.

## Workstream 7 — Correct documentation and operational claims

Update:

- `docs/config-profiles.md`;
- `docs/operations/dispatch-stability.md`;
- `docs/deployment.md`;
- `docs/raspberry-pi.md`;
- `architecture/README.md`;
- `AGENTS.md`;
- `config.example.toml`;
- `README.md` if it makes stability claims;
- `CHANGELOG.md`.

Required documentation outcomes:

- distinguish implementation-complete from evidence-validated;
- state the supported runtime-thread policy consistently;
- identify which gates run per commit, nightly, and manually;
- document how to run each soak mode;
- document artifact locations and evaluator usage;
- explain how to interpret queue growth, DB lock waits, event-loop lag, memory slope, WAL growth, and generation retention;
- provide safe mitigations in priority order without recommending restart loops as a normal remedy;
- identify trace sampling/drops as observability degradation, not request loss;
- avoid claiming Raspberry Pi validation without retained Raspberry Pi evidence;
- avoid calling profiles evidence-based unless the linked artifact actually validates them.

## Workstream 8 — Produce the final closure report

Create a dated closure report such as:

- `plans/2026-07-XX-dispatch-stability-closure-status.md`, or
- `docs/validation/dispatch-stability-2026-07.md`.

The report must include:

### Implementation inventory

For Milestones A–G, list:

- principal commits;
- primary files/modules;
- tests covering the milestone;
- final status: closed, closed with limitation, or open.

### Evidence inventory

For every executed soak:

- artifact path or workflow URL;
- git SHA;
- host profile;
- duration;
- effective config fingerprint;
- offered workload;
- pass/fail summary;
- early/late metrics;
- resource plateau results;
- consistency audit result;
- known anomalies.

### Acceptance matrix

Map each roadmap and closure-pass criterion to one of:

- `PASS` with evidence reference;
- `PASS WITH LIMITATION` with exact scope;
- `FAIL` with issue and owner;
- `NOT RUN` with reason.

No criterion may be marked passed solely because a test file or constant exists.

### Residual risks

At minimum discuss:

- SQLite single-writer saturation under unsustainable offered load;
- observability drops under pressure;
- experimental multi-thread topology if retained;
- host/storage sensitivity of absolute latency;
- absence of SBC evidence, if applicable;
- public-provider behavior being outside deterministic closure scope.

### Release recommendation

Choose exactly one:

- ready for release;
- ready with documented limitations;
- not ready pending named blockers.

## Test plan

### Focused correctness

```bash
uv run pytest \
  tests/unit/test_periodic_cadence.py \
  tests/unit/test_dispatch_timing_boundaries.py \
  tests/unit/test_selection_claim.py \
  tests/unit/test_selection_claim_diagnostics.py \
  tests/unit/test_coordinator_claim_lock_scope.py \
  tests/unit/test_dispatch_writer.py \
  tests/unit/test_routing_trace_writer.py \
  tests/unit/test_routing_trace_guard.py \
  tests/unit/test_maintenance_budget.py \
  tests/unit/test_granian_topology.py \
  tests/unit/test_metrics_coalescer_invariants.py \
  tests/unit/test_telemetry_bounded_growth.py -v
```

Add the new slow-writer fairness test module to this command.

### Short soak and consistency

```bash
uv run pytest tests/soak/ -v -m "not extended_soak"
```

### Performance-contract suite

```bash
uv run pytest tests/perf/ -v -m performance
```

Performance-contract tests should assert ordering, queueing, boundedness, and relative architectural behavior. Do not make ordinary CI depend on fragile sub-millisecond absolute timing.

### Full repository validation

```bash
uv run ruff format --check src tests scripts
uv run ruff check src tests scripts
uv run pyright
uv run pytest
```

Use the repository's canonical commands where they differ.

### Extended soak

Example expected interface:

```bash
uv run python scripts/run_dispatch_stability_soak.py \
  --profile balanced-file-backed \
  --mode nightly \
  --output artifacts/dispatch-soak/nightly
```

Then evaluate artifacts independently:

```bash
uv run python scripts/evaluate_dispatch_stability_soak.py \
  artifacts/dispatch-soak/nightly/manifest.json
```

Exact names may differ, but equivalent reproducibility and offline evaluation are required.

## Acceptance criteria

The closure pass is complete when all of the following are true:

1. A reproducible process-level, file-backed soak runner exists with deterministic profiles and bounded smoke/CI/nightly/reference modes.
2. Strict early/late gates are implemented separately from short CI framework tests.
3. At least one 1–3 hour general Linux file-backed run passes all strict gates.
4. At least one slow-storage or constrained-host run demonstrates bounded queues, flat claim-lock-held latency, recovery, and consistency.
5. A 6-hour SBC run is retained, or the lack of SBC evidence is explicitly documented and all SBC validation claims are removed.
6. Every run produces machine-readable metrics, human summary, logs, consistency audit, environment/config metadata, and checksummed manifest.
7. Slow-writer burst tests prove lock de-convoying, bounded fairness skew, hard quota/concurrency safety, exact compensation, and final durable/runtime convergence.
8. No corrective fairness mechanism reintroduces DB I/O under the selection claim lock.
9. Runtime-thread defaults, warnings, examples, deployment profiles, and documentation state one consistent support policy.
10. Profiles with `threads > 1` are either removed from recommended defaults or backed by explicit multi-loop safety and performance evidence.
11. Hosted CI visibly collects and passes the required unit, integration, bounded performance-contract, short soak, profile-parse, and consistency-audit tests on repository head.
12. An optional scheduled/manual extended workflow uploads artifacts on both pass and failure.
13. The final consistency audit reports no unwaived lifecycle violations after drain and shutdown.
14. Dispatch, trace, finalization, metrics, and generation queues return to baseline after their respective profiles.
15. RSS, FDs, threads, tasks, generations, and WAL/storage measurements plateau within documented limits.
16. No process-owned writer or periodic task is duplicated across rehash cycles.
17. Documentation no longer labels unexecuted profiles or hardware targets as validated.
18. A final closure report maps every roadmap and closure criterion to concrete evidence and makes an explicit release recommendation.

## Stop conditions and escalation

Stop and open a corrective implementation plan rather than weakening gates if any of the following occur:

- late dispatch or local-pre-upstream latency repeatedly violates the ratios under a stationary workload;
- a queue or resource counter grows monotonically after warm-up;
- the consistency auditor finds lifecycle corruption;
- fairness tests show hard quota/concurrency violations during persistence/publication overlap;
- a corrective pending-claim mechanism requires holding the selection lock across SQLite I/O;
- process-owned async primitives fail under the documented supported topology;
- shutdown leaks tasks, DB connections, HTTP clients, or writers;
- a failure-injection scenario requires an undocumented process restart to recover.

Do not respond by relaxing thresholds without identifying and documenting the underlying cause. Any threshold adjustment must include raw artifacts, rationale, and an updated host/profile-specific acceptance contract.

## Recommended implementation order

1. Correct the runtime-thread policy and profile documentation first so new evidence runs use the intended supported topology.
2. Add deterministic slow-writer fairness tests and fix any hard-limit defect discovered.
3. Build the process-level soak runner and artifact schema.
4. Add strict offline gate evaluation.
5. Wire bounded hosted CI and optional scheduled/manual extended execution.
6. Run general-host and slow-storage evidence profiles.
7. Run the SBC reference profile when hardware is available, or explicitly scope the limitation.
8. Produce the final closure report and update roadmap status.

## Handoff guidance

Keep this pass evidence-driven and narrow. The implementation already contains the principal architectural corrections. Avoid broad refactors unless a strict soak or fairness test exposes a concrete defect. Commit runner/evaluator infrastructure separately from behavior changes, retain failing artifacts during corrective work, and make every final closure claim traceable to a test, workflow run, or checksummed soak artifact.