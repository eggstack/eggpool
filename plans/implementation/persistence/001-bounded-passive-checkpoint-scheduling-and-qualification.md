# Persistence Milestone 001 — Bounded Passive Checkpoint Scheduling and Target Qualification

Status: active

Repository baseline: 4060c5a90f2c2c84d91273e489a922febf2af42c

Source roadmap:

- plans/subsystems/persistence-roadmap.md#milestone-001--bounded-passive-checkpoint-scheduling-and-target-qualification

Long-term requirements:

- plans/000-long-term-specification.md — §2 invariant 4 and §5 performance posture
- plans/002-long-term-roadmap.md — Phase 3 persistence and publication bounds

Applicable ADRs:

- None required while the implementation keeps one SQLite connection/gate/worker, WAL/NORMAL durability, no public config, and the existing process-owned checkpoint task.
- Stop for architecture review if those conditions cannot be preserved.

Primary class: polish

## 1. Objective

Implement and physically qualify the narrowest production checkpoint improvement supported by Plans 239/240: let the existing process-owned checkpoint task perform opportunistic PASSIVE checkpoint work before normal traffic reaches SQLite's automatic checkpoint threshold, while keeping the automatic threshold unchanged as a hard safety ceiling.

The objective is specifically to reduce the Pi/MMC foreground COMMIT tail without replacing it with unbounded WAL growth, a second database owner, or an always-blocking maintenance task.

## 2. Why this milestone is ready

Hard evidence already exists.

artifacts/qualification/239-sbc-db-phase-checkpoint-diagnostic.json records a Raspberry Pi 5, Ubuntu 24.04.4, ext4 MMC target with 4096-byte pages. Three default 1000-page runs had maximum request latencies of 4009 ms, 12012 ms, and 14903 ms; the worst samples were dominated by publication COMMIT. Qualification-only wal_autocheckpoint = 0 reduced the three maxima to 14 ms, 5 ms, and 4 ms. A 256-page automatic threshold still produced 826–3042 ms maxima, showing that simply lowering the foreground automatic threshold is not the solution.

Plan 240 then required a separate bounded production design and preserved the one-gate/worker/WAL/NORMAL invariants.

SQLite's own WAL documentation confirms the mechanism: automatic checkpoints are PASSIVE, normally trigger at 1000 pages, and execute from the COMMIT that crosses the threshold; application-initiated PASSIVE checkpoints can be scheduled separately, while disabling automatic checkpoints without another bound risks excessive WAL growth.

Research references:

- https://www.sqlite.org/wal.html
- https://www.sqlite.org/c3ref/wal_autocheckpoint.html
- https://www.sqlite.org/pragma.html#pragma_wal_checkpoint

## 3. Current implementation evidence

Authority paths:

- rust/src/db/connection.rs
- rust/src/task_supervisor.rs
- rust/src/runtime_lifecycle/process.rs
- rust/src/coordinator/publication.rs
- rust/src/coordinator/finalization.rs
- rust/src/runtime_lifecycle/recovery.rs
- rust/src/operations/backup.rs
- rust/tests/database_compatibility.rs
- rust/tests/coordinator_publication.rs
- rust/tests/coordinator_finalization.rs
- rust/tests/runtime_lifecycle_r008.rs
- rust/tests/operations_o006.rs
- scripts/qualification_sbc.py
- tests/tooling/test_qualification_sbc.py
- artifacts/qualification/239-sbc-db-phase-checkpoint-diagnostic.json

Current production facts:

- Database::configure sets WAL and synchronous but does not override wal_autocheckpoint.
- Database::checkpoint issues PRAGMA wal_checkpoint(PASSIVE) on the existing serialized connection.
- runtime_task_inventory defines checkpoint as a process-owned task that runs immediately and then every 14,400 seconds.
- The qualification feature can observe effective WAL pragmas and foreground transaction phases without changing ordinary builds.
- The existing task/single-gate model already supplies the ownership boundary needed for a first conservative candidate.

## 4. Invariants that must not regress

- One SQLite connection, one semaphore/gate, one tokio-rusqlite worker.
- WAL mode and synchronous = NORMAL.
- The production automatic checkpoint threshold remains unchanged in this milestone and continues to bound worst-case WAL growth.
- Publication/finalization transaction grouping, durable request state, reservation ownership, fault injection, compensation, and startup repair remain unchanged.
- No checkpoint work runs inside coordinator publication/finalization code.
- No public Config, CLI, example-config, environment, HTTP, or Rust compatibility surface is added for checkpoint tuning.
- Qualification overrides remain feature-only and absent from ordinary release behavior.
- No raw SQL, request identity, body, credentials, model/provider/account names, database path, or raw WAL bytes enter diagnostics.
- Shutdown/backup/restore/reload/recovery must remain bounded and correct.

## 5. Scope

### In scope

- Add a crate-private checkpoint policy/result boundary in rust/src/db/connection.rs or the smallest equivalent owner.
- Let a checkpoint tick cheaply determine whether new durable transactions occurred since the previous inspection.
- Add a non-queueing gate path for optional maintenance: if the database gate is already owned, the checkpoint tick must skip/defer rather than wait ahead of foreground work.
- Once the gate is acquired, query WAL frame counts through SQLite itself; PRAGMA wal_checkpoint(NOOP) is the preferred observation because it reports log/checkpointed frames without doing checkpoint work.
- Run PRAGMA wal_checkpoint(PASSIVE) only when the soft maintenance threshold is due.
- Keep the existing SQLite automatic threshold as the hard fallback if the maintenance task is late or repeatedly skipped.
- Reschedule the existing checkpoint task often enough to be useful; use qualification-only tuning values to select the final internal cadence/soft threshold from physical evidence.
- Extend the existing Plan-239-style SBC report so it distinguishes foreground publication/finalization phase time, checkpoint-task time, WAL frame progress, skipped-busy ticks, and whether SQLite's automatic threshold was still reached.
- Update current architecture/deployment documentation after the accepted keep/change decision.

### Explicitly out of scope

- Setting production wal_autocheckpoint = 0.
- Lowering the production automatic threshold to 256 or another speculative value.
- A second connection, dedicated checkpoint connection, DB pool, or additional SQLite worker.
- A per-request spawned checkpoint task.
- A new event-driven checkpoint coordinator/Notify loop.
- New public checkpoint configuration.
- FULL, RESTART, or TRUNCATE checkpoints on the request-serving path.
- Changing backup format, schema, migration chain, synchronous mode, or journal mode.
- Hiding a long SQLite operation behind tokio timeout/abort and claiming the underlying worker closure was cancelled.

## 6. Required production changes

### 6.1 Bounded checkpoint observation/result

Introduce a small internal result type that can distinguish at least:

- no new writes / not due;
- gate busy / deferred;
- inspected but below soft threshold;
- PASSIVE checkpoint attempted with returned log/checkpointed frame counts;
- SQLite failure.

Do not expose arbitrary error strings or paths through runtime diagnostics.

The ordinary Database::checkpoint compatibility method may remain for existing callers/tests. Prefer factoring both it and the new conditional path through one internal checkpoint primitive rather than duplicating SQL.

### 6.2 Non-queueing optional maintenance

Optional checkpoint work must not sit in the semaphore wait queue ahead of foreground database operations.

Use the existing gate with try-acquire semantics or an equivalent race-safe non-queueing check. Once a checkpoint actually owns the gate, the existing worker remains the only SQLite executor. Do not add another connection to avoid contention.

A foreground request that arrives after PASSIVE work has started can still wait. Qualification must measure that gate wait explicitly.

### 6.3 Avoid idle polling work

The callback should use the existing DatabaseStats transaction counter or an equivalent bounded scalar to skip SQLite inspection when no durable transaction has occurred since its last successful/attempted inspection.

The task may wake periodically, but an idle process should normally perform only cheap in-memory state inspection.

### 6.4 Qualification-only tuning matrix

The final production cadence and soft frame threshold must be selected from physical evidence, not guessed from desktop results.

It is acceptable to add feature-gated qualification-only overrides for:

- checkpoint polling interval;
- soft WAL-frame threshold.

They must follow the Plan 239 rules:

- only compiled/consulted with qualification-db-diagnostics;
- bounded numeric ranges;
- startup-validated;
- absent from Config/CLI/help/examples/ordinary docs;
- no dependency;
- effective values recorded in the sanitized qualification artifact.

The implementation should begin with a small bounded matrix around thresholds below 256 pages and polling cadences capable of observing the 60-request local corpus before it routinely reaches the 1000-page automatic threshold. The closure record must state the tested values and why the final internal constants were selected.

### 6.5 Preserve automatic checkpoint as safety ceiling

Do not disable or raise SQLite's production automatic checkpoint threshold in this milestone.

The intended first candidate is additive: routine low-volume maintenance should checkpoint before the threshold, while the unchanged SQLite threshold remains the fallback under bursts, scheduler starvation, or repeated busy-gate deferrals.

This avoids introducing an unbounded-WAL failure mode while evaluating whether the existing periodic-task architecture is sufficient.

## 7. Ordered work packages

### Work package A — Conditional checkpoint primitive

Intent:

Make the existing Database boundary capable of observing and optionally checkpointing WAL state without queueing optional work behind an already-busy gate.

Required changes:

- factor checkpoint SQL into one internal owner;
- add try/defer behavior;
- return bounded frame/progress scalars;
- keep Database::checkpoint behavior compatible.

Acceptance evidence:

- database unit/integration tests cover no-WAL/no-op, below-threshold, due, busy-gate defer, error, and close behavior;
- no second connection is opened.

### Work package B — Process task scheduling

Intent:

Use the already-supervised process checkpoint task rather than adding a new lifecycle owner.

Required changes:

- track whether durable transaction count changed since the previous tick;
- apply the conditional checkpoint policy;
- select a candidate polling interval through the qualification-only path;
- retain normal task snapshot/error accounting.

Acceptance evidence:

- runtime_lifecycle_r008 and task-supervisor unit tests show one checkpoint task, process ownership, correct reschedule/shutdown behavior, and no duplicate loop;
- a busy DB gate causes a skipped/deferred tick, not a queued maintenance operation.

### Work package C — Physical qualification

Intent:

Prove that the narrow periodic design actually moves routine checkpoint cost out of foreground COMMIT on the target where the defect was measured.

Required changes:

- extend scripts/qualification_sbc.py and tooling tests only as needed;
- run the existing sequential native finite corpus and a modest concurrent/burst corpus;
- capture transaction-phase, gate-wait, task-tick, WAL-frame, total-latency, ownership-convergence, and backup/restart checks.

Acceptance evidence:

On the Raspberry Pi-class MMC target, the selected candidate must:

- avoid multi-second foreground publication/finalization COMMIT tails in three accepted 60-request sequential runs;
- keep p95 below 100 ms and maximum below 500 ms in each accepted sequential run, unless a stricter already-recorded target is adopted by the implementation;
- keep foreground publication/finalization COMMIT phases below 50 ms in that corpus;
- show that routine maintenance checkpoint progress occurs before the automatic 1000-page fallback in the accepted runs;
- retain pending-request = 0 and active-reservation = 0 convergence;
- preserve the unchanged direct-provider control behavior;
- show no unbounded WAL growth during the bounded burst/concurrency case.

If these criteria cannot be met with the periodic task while the automatic safety ceiling remains intact, stop. Do not add event-driven checkpoint coordination in this milestone.

### Work package D — Recovery and lifecycle qualification

Intent:

Make sure moving routine checkpoint timing does not shift risk into operations.

Required changes/evidence:

- startup/restart with a non-empty WAL;
- database close after recent checkpoint activity;
- backup_to and O006 backup while checkpoint work can be due;
- restore/reopen smoke path;
- reload/task-diff behavior;
- shutdown while a checkpoint tick is pending or skipped;
- no claim that aborting a task interrupts SQLite worker I/O.

## 8. Failure, cancellation, restart, contention semantics

Checkpoint-task failure is non-fatal to a request and is reflected through existing task failure accounting. The unchanged automatic SQLite threshold remains the hard fallback.

A busy gate causes optional maintenance to defer. It must not steal queue position from a transaction that already owns or is waiting on the foreground path.

Once PASSIVE work starts on the worker, do not fabricate cancellation. If shutdown reaches an in-progress SQLite closure, lifecycle behavior must be tested against the actual tokio-rusqlite semantics. The implementation may rely on the small soft threshold plus bounded physical evidence, not on dropping an await future as proof of I/O cancellation.

Restart/recovery must tolerate a WAL that has not reached the soft checkpoint threshold. The SQLite automatic threshold and normal close/reopen behavior remain authoritative.

## 9. Compatibility and migration

No schema migration.

No public config or CLI migration.

No change to HTTP response shapes, request publication, routing, provider behavior, or the qualification feature's absence from ordinary release JSON.

The task name checkpoint remains unchanged so diagnostics and qualification task-quiescence checks stay compatible.

## 10. Required tests

At minimum:

- rust/tests/database_compatibility.rs — WAL/NORMAL, checkpoint result/close/busy behavior.
- rust/tests/runtime_lifecycle_r008.rs — task ownership/install/shutdown and checkpoint scheduling contract.
- rust/tests/coordinator_publication.rs and coordinator_finalization.rs — durable request ownership unchanged.
- rust/tests/operations_o006.rs — backup path remains valid.
- tests/tooling/test_qualification_sbc.py — new sanitized checkpoint-policy evidence parser/validation.
- Existing qualification-db-diagnostics tests — ordinary build remains unchanged and feature values stay bounded.

Add deterministic tests for the busy-gate skip using an observable barrier/transaction holder; do not use arbitrary sleeps.

## 11. Required verification commands

~~~bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --test database_compatibility -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_publication -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test operations_o006 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r008 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked --release

uv sync --frozen
uv run pytest tests/tooling/test_qualification_sbc.py -q
git diff --check
~~~

For physical evidence, build the qualification-db-diagnostics release and run the Plan-239-derived SBC diagnostic on the same storage class. Record commands and artifact path in closure; do not claim hosted CI as physical SBC evidence.

## 12. Documentation updates

- architecture/deep-dive-database.md — accepted checkpoint policy and keep/change decision.
- architecture/deep-dive-background.md — process checkpoint task cadence/conditional behavior if changed.
- architecture/README.md — only if the current residual-efficiency summary needs a concise update.
- .opencode skills only if implementation changes a reusable development/qualification rule.

Keep Plans 239/240/242 immutable as historical evidence.

## 13. Acceptance criteria

- One DB connection/gate/worker remains.
- WAL/NORMAL and production automatic checkpoint safety threshold remain unchanged.
- Optional task checkpointing skips rather than queues behind a busy gate.
- Idle task ticks avoid SQLite work when no durable transactions occurred.
- PASSIVE/NOOP work uses the existing worker and retains only bounded scalar observations.
- No public configuration or protocol change.
- Physical Pi/MMC evidence satisfies Work package C or the milestone records a keep decision and stops.
- Backup/restart/recovery/reload/shutdown evidence is green.
- Ordinary and no-default release behavior remain qualified.

## 14. Stop conditions

Stop and report rather than improvise if:

- the periodic task cannot prevent the foreground tail under the Plan-239-style target corpus;
- the only effective design requires disabling automatic checkpoints without a replacement hard WAL bound;
- the implementation needs a second SQLite connection/writer;
- optional checkpoint work must queue ahead of known foreground DB work;
- checkpoint cancellation would require pretending that dropping a Tokio future cancels SQLite worker I/O;
- a public operator tuning surface appears necessary;
- target evidence is unavailable and the requested conclusion depends on physical MMC behavior.

A failure of the narrow design should produce the roadmap's conditional M003 architecture plan, not scope expansion here.

## 15. Closure evidence required

The closure record must contain:

- implementation commit(s);
- exact physical board/OS/kernel/filesystem/storage class;
- effective journal_mode/synchronous/page_size/wal_autocheckpoint;
- tested polling intervals and soft thresholds;
- selected policy or explicit keep decision;
- three accepted sequential-run p50/p95/max summaries;
- foreground gate-wait/begin/body/commit phase summaries;
- checkpoint task attempted/skipped/success/failure counts and WAL-frame progress;
- peak observed WAL-frame count and whether automatic fallback fired;
- backup/restart/recovery/shutdown evidence;
- full focused/default/no-default verification results;
- severity-tagged residual findings.

## 16. Handoff notes

Do not start by setting wal_autocheckpoint = 0. The first safe experiment is to make the existing maintenance owner useful while retaining SQLite's current hard fallback.

The distinction to preserve is:

foreground request transaction
    -> finishes with ordinary COMMIT
process checkpoint task
    -> only if new writes + gate immediately available + soft WAL threshold due
    -> PASSIVE checkpoint on the same worker
SQLite automatic checkpoint
    -> unchanged emergency/fallback ceiling

If physical evidence says the periodic task is too slow to intercept the local write rate, stop. That is evidence for a different scheduling design, not permission to add one ad hoc.
