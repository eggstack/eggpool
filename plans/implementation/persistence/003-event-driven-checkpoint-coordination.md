# Persistence Milestone 003 — Event-Driven Checkpoint Coordination

Status: ready

Repository planning baseline: `b692ca26d674f45325f4b5867be479b2bc73e5c2`

Execution baseline:

- Accepted persistence M006 closure: `abfdb18b` (`plans/closure/persistence/006-status.md`).
- M003 implementation must use this engine-safety baseline.
- If M006 materially changes the database/task ownership surfaces described below, stop and reconcile this plan before editing runtime code.

Source roadmap:

- `plans/subsystems/persistence-roadmap.md#milestone-003--event-driven-checkpoint-coordination`

Hard prerequisite:

- `plans/implementation/persistence/006-sqlite-noop-and-wal-reset-safety-baseline.md`
- future `plans/closure/persistence/006-status.md`

Evidence lineage:

- legacy Plan 239 physical checkpoint diagnostic;
- legacy Plan 240 production design handoff;
- `plans/closure/persistence/001-status.md`;
- `plans/closure/persistence/004-status.md`;
- `plans/closure/persistence/005-status.md`;
- `tests/tooling/test_persistence_m004_evidence.py`.

Long-term requirements:

- `plans/000-long-term-specification.md` — persistence ownership, bounded resources, fail-closed durability
- `plans/002-long-term-roadmap.md` — Phase 3 persistence/performance posture
- `plans/003-planning-process.md` — infrastructure evidence and closure rules

External research references:

- SQLite WAL behavior — `https://sqlite.org/wal.html`
- SQLite WAL hook contract — `https://sqlite.org/c3ref/wal_hook.html`
- SQLite automatic checkpoint contract — `https://sqlite.org/c3ref/wal_autocheckpoint.html`
- SQLite checkpoint modes — `https://sqlite.org/pragma.html#pragma_wal_checkpoint`

Applicable ADRs:

- None required for the reviewed design because it preserves the existing process-owned checkpoint task, one SQLite connection/gate/worker, WAL/NORMAL, automatic 1000-page fallback, schema, and public surfaces.
- Stop and require an ADR/explicit architecture decision if implementation needs a second SQLite connection/worker, disables or raises the automatic fallback, adds a public checkpoint setting, uses a SQLite WAL hook, or creates a new process-lifecycle owner.

Primary class: infrastructure

## 1. Objective

Replace timer-only checkpoint opportunity discovery with one bounded, process-owned, event-assisted checkpoint loop that can react after successful SQLite transaction commits and attempt PASSIVE maintenance before routine finite bursts hit the 1000-page automatic-checkpoint ceiling.

The implementation must:

- keep the existing single `checkpoint` runtime task as the sole checkpoint lifecycle owner;
- emit a non-blocking coalesced wake only after a successful COMMIT and after the EggPool database gate is released;
- distinguish successful durable commits from transaction attempts/rollbacks;
- preserve the 60-second timer as a fallback/recovery wake;
- keep `try_acquire` deferral and never queue optional checkpoint work behind a known-busy foreground gate;
- keep the 256-frame soft threshold and 1000-page automatic fallback unchanged;
- use true `wal_checkpoint(NOOP)` observation supplied by M006 before deciding whether PASSIVE is due;
- prove on the Pi 5/ext4/MMC target that the event-assisted strategy removes the measured foreground COMMIT tail rather than moving it into gate wait.

## 2. Architecture review conclusion

The architecture review required by the persistence roadmap is complete for planning purposes.

### Selected design

Use an EggPool-owned commit signal:

- a private successful-commit sequence/watermark in the shared `Database` state;
- a private coalescing Tokio `Notify` (or equivalently bounded one-permit wake primitive already available from Tokio);
- notification emitted after successful COMMIT and gate release;
- the existing process-owned checkpoint task waits on cancellation, the existing 60-second fallback interval, or the coalesced commit notification;
- every wake invokes the existing bounded maintenance policy on the same database connection/gate/worker.

### Rejected designs

**SQLite `sqlite3_wal_hook`: rejected.**

SQLite's automatic checkpoint mechanism itself uses the WAL hook. Registering a custom WAL hook replaces/disables the automatic checkpoint callback. M003 must retain the 1000-page safety fallback, so it must not enable rusqlite's `hooks` feature or register a WAL hook.

**Per-request/per-transaction `tokio::spawn`: rejected.**

It would create unbounded scheduling pressure and a second lifecycle/cleanup problem.

**A second checkpoint connection/worker: rejected.**

It violates the persistence roadmap and introduces exactly the multi-connection checkpoint/write concurrency EggPool has deliberately avoided.

**Checkpoint inside foreground COMMIT or coordinator code: rejected.**

The defect is foreground checkpoint ownership; moving explicit maintenance into publication/finalization would preserve the latency coupling.

**Disable automatic checkpointing and own all WAL bounds: rejected.**

M003 is not authorized to remove the existing hard safety ceiling.

## 3. Readiness dependency resolution

M004/M005 satisfy the evidence dependency: timer-only periodic scheduling is insufficient on the target class.

The architecture review above satisfies the former design-planning dependency.

M006 was the remaining hard correctness dependency:

- the current locked SQLite 3.50.2 predates true `wal_checkpoint(NOOP)`;
- M003 relies on an observational WAL query before deciding whether to run PASSIVE;
- M006 must also move the bundled engine to a WAL-reset-fixed release before increasing manual checkpoint opportunity frequency.

M006 closed in `abfdb18b`; the implementation baseline above records its accepted closure SHA. This separate registry status-change commit promotes M003 to ready.

## 4. Current implementation evidence

### Database transaction ownership

`rust/src/db/connection.rs` owns:

- one `tokio_rusqlite::Connection`;
- one `Arc<Semaphore>` gate;
- one tokio-rusqlite worker behind the connection;
- `with_transaction_kind()`;
- explicit `DatabaseTransaction::commit()/rollback()`;
- `checkpoint_maintenance()`;
- qualification checkpoint stats.

The current `transactions: AtomicU64` is incremented when a transaction is begun/attempted, before COMMIT outcome is known. It is therefore not a valid event source for M003. Rollbacks, body failures, begin failures after the counter increment, and commit failures must not be treated as successful durable commits.

M003 requires a separate successful-commit signal.

### Checkpoint task ownership

`rust/src/task_supervisor.rs` owns exactly one process-owned `checkpoint` task:

- `RuntimeTaskSpec` remains the schedule/diagnostic contract;
- `CallbackRegistry::with_checkpoint()` installs the callback;
- `TaskState` owns cancellation/join/tick diagnostics;
- `run_task()` owns task execution;
- `wait_or_cancel()` currently owns fixed-delay waiting.

The event wake must be integrated into this owner. M003 must not spawn a sibling checkpoint loop.

### Target evidence

M005's machine-checked M004 corpus shows:

- every 30s/60s phase run had zero checkpoint-task ticks inside the measured finite batch;
- the 1s/64 stress run had three in-window checkpoint ticks and all three returned `gate_busy`;
- no in-batch maintenance PASSIVE checkpoint completed;
- all tested periodic candidates retained foreground auto-checkpoint tails;
- 60s/256 maxima were 1709 / 561 / 10 943 ms.

The event design must solve both timing dimensions:

1. wake while finite-burst writes are occurring;
2. wake immediately after a successful commit/gate release rather than at an arbitrary timer instant that repeatedly collides with foreground gate ownership.

## 5. Invariants that must not regress

- One SQLite connection.
- One serialized EggPool database gate.
- One tokio-rusqlite worker.
- One process-owned checkpoint task.
- WAL + `synchronous=NORMAL`.
- Ordinary `wal_autocheckpoint=1000`.
- Soft maintenance threshold remains 256 frames in M003.
- 60-second fallback interval remains present.
- PASSIVE is the only explicit ordinary maintenance checkpoint mode.
- No SQLite WAL hook.
- No public checkpoint config/env/CLI/HTTP/Rust API.
- No schema migration.
- No per-request task spawning.
- No unbounded queue/channel.
- No generation-owned checkpoint task.
- Publication/finalization transaction grouping and failure mapping unchanged.
- Backup/restore/recovery/reload/shutdown ownership unchanged.
- Qualification-only instrumentation stays absent from ordinary release JSON.
- Secrets/request bodies/provider bodies/identities do not enter checkpoint diagnostics.

## 6. Required production design

### 6.1 Successful-commit signal

Add a crate-private database checkpoint signal with two pieces of state:

- a monotonically advancing successful-commit sequence (`AtomicU64`);
- one coalescing wake primitive (`Arc<Notify>` or equivalent one-permit bounded primitive).

Do not repurpose the current `transactions` counter.

Provide narrow crate-private access sufficient for:

- checkpoint maintenance to compare current successful-commit sequence against its last observed value;
- the process-owned checkpoint task to await the coalesced wake.

Signal only on successful COMMIT.

#### `with_transaction_kind()`

After SQLite reports COMMIT success:

1. release/drop the EggPool database gate permit;
2. advance the successful-commit sequence;
3. issue `notify_one()`;
4. return the committed result.

No signal on:

- BEGIN failure;
- transaction body failure;
- rollback;
- COMMIT failure or ambiguous rollback handling;
- connection/worker failure.

The gate must be released before notification so the woken maintenance task has a real opportunity to acquire it.

#### `DatabaseTransaction::commit()`

Apply the same rule to explicit caller-controlled transactions used by reload/runtime acceptance:

1. successful COMMIT;
2. release permit;
3. advance successful-commit sequence;
4. notify.

`rollback()` must not notify.

### 6.2 Coalescing semantics

The wake is advisory; the successful-commit sequence is authoritative.

Required properties:

- repeated commits while no task is waiting coalesce to at most one stored wake permit;
- repeated commits while checkpoint work is running do not enqueue unbounded work;
- after a wake, maintenance observes the newest sequence, not one queued item per commit;
- a lost/coalesced notification cannot lose correctness because the 60-second fallback remains;
- sequence comparison is equality/watermark based; no queue length is required.

Use acquire/release atomic ordering or another explicitly justified ordering sufficient for the commit-sequence handoff. Do not add a mutex to the foreground commit path.

### 6.3 Event-aware checkpoint task

Extend the existing task supervisor rather than adding a new task.

The `checkpoint` TaskState may carry one optional internal event-wake handle. Keep this out of public/config-derived `RuntimeTaskSpec` unless repository mechanics make a private typed field there strictly clearer; ordinary task inventory and diagnostics must remain backward-compatible.

The wait phase for the checkpoint task becomes:

- cancellation/shutdown;
- event wake;
- existing 60-second fallback timer.

All other runtime tasks retain ordinary fixed-delay behavior.

Cancellation must win when shutdown and an event are simultaneously ready. Do not let a stored wake start a fresh checkpoint tick after shutdown admission.

The existing task tick/callback ownership remains authoritative for timeout, cancellation, join, and diagnostics.

### 6.4 Maintenance watermark

Change `checkpoint_maintenance()` to use the successful-commit sequence instead of the current begin/attempt-derived transaction counter.

Rules:

- no new successful commit since last successful inspection => `NotDue`, no SQLite work;
- gate already owned => `GateBusy`, do not advance observed sequence;
- true NOOP inspection reports below soft threshold => advance observed sequence and return `BelowThreshold`;
- threshold due and PASSIVE succeeds => advance observed sequence and return `Checkpointed`;
- inspection/checkpoint error => do not advance observed sequence;
- SQLite-side checkpoint busy/deferred => do not advance observed sequence if work was not actually inspected to a stable result.

The current attempt/transaction counter may remain for existing stats. Do not silently change its external meaning.

### 6.5 No hot-loop retry

A `GateBusy` event tick must not self-reschedule immediately.

Retry occurs only through:

- a later successful commit notification; or
- the 60-second fallback.

This is necessary because M004's 1s/64 evidence showed arbitrary repeated timer wakes can collide with foreground gate ownership.

### 6.6 Qualification observability

Prefer existing bounded evidence:

- task `tick_count_delta`;
- checkpoint maintenance `gate_busy`, `below_threshold`, `checkpointed`, failures;
- publication/finalization phase records;
- WAL sequence correlation;
- durable convergence.

If wake-cause attribution cannot be proven unambiguously with the existing feature-only evidence, add only feature-gated scalar counters such as `event_wakes` / `timer_wakes`. Do not change ordinary authenticated runtime JSON.

## 7. Ordered work packages

### Work package A — Successful-COMMIT signal

Intent:

Create the correct event source without touching request/coordinator call sites.

Required changes:

- database-private successful-commit sequence;
- coalescing wake;
- signal after successful COMMIT + gate release in both transaction APIs;
- rollback/failure suppression.

Acceptance evidence:

- successful ordinary transaction advances once and wakes;
- explicit `DatabaseTransaction::commit` advances once and wakes;
- rollback/body/commit-failure paths do not advance/wake;
- foreground path allocates no per-commit task or queue element.

### Work package B — Integrate wake with the existing checkpoint task

Intent:

Make the single process-owned task event-assisted while retaining timer fallback and supervisor ownership.

Required changes:

- optional internal checkpoint wake handle;
- cancellation/event/timer wait;
- cancellation-first shutdown behavior;
- no task inventory expansion.

Acceptance evidence:

- exactly one checkpoint task;
- event can produce a checkpoint callback before the 60-second timer;
- many commit notifications coalesce;
- shutdown joins cleanly;
- reload does not duplicate checkpoint ownership.

### Work package C — Commit-sequence maintenance semantics

Intent:

Remove transaction-attempt ambiguity from the M001 watermark.

Required changes:

- maintenance compares successful-commit sequence;
- `GateBusy`/error leaves sequence pending;
- successful inspection consumes through current sequence;
- true NOOP from M006 is the observation primitive;
- PASSIVE remains threshold-gated.

Acceptance evidence:

- failed/rolled-back transaction cannot make maintenance due;
- gate-busy event is retried on later commit/fallback;
- below-threshold commit settles without PASSIVE;
- threshold-due commit runs one bounded PASSIVE;
- no immediate retry spin.

### Work package D — Local contention/lifecycle qualification

Intent:

Prove ownership before physical performance work.

Required tests:

- burst commits coalesce;
- foreground gate ownership causes bounded `GateBusy` and later recovery;
- event arriving while callback runs leaves at most one subsequent wake;
- shutdown while waiting/woken/running;
- reload/generation swap keeps one process checkpoint owner;
- backup after recent event checkpoint;
- recovery/restart after recent event checkpoint;
- default/no-default parity.

Use observable transitions/Notify/test barriers, not arbitrary sleep/yield counts.

### Work package E — Paired Pi/MMC target qualification

Intent:

Determine whether event assistance actually fixes the production tail.

Run on the same qualifying target class as M004:

- Raspberry Pi 5-class Linux/aarch64;
- ext4;
- MMC/non-rotational target;
- SQLite page size/effective pragmas recorded.

Build and hash two binaries:

1. **baseline** — accepted M006 closure commit, timer-only M001 behavior on the upgraded/fixed SQLite engine;
2. **candidate** — M003 event-assisted commit.

The baseline is required because the SQLite engine/NOOP semantics changed in M006; do not compare the M003 candidate only against the older M004/SQLite-3.50.2 binary.

For each binary, collect at minimum three accepted Plan-239-style 60-request sequential phase runs with production 60s/256/1000 settings.

Candidate also runs:

- ordinary SBC benchmark including concurrency-4;
- backup/recovery/restart/shutdown/reload-rehash checks;
- bounded WAL/convergence evidence.

### Work package F — Final disposition

Accept event-assisted production behavior only if the physical evidence clears the gates below.

If it does not:

- revert/withhold M003 runtime changes;
- keep the M006-fixed timer-only baseline;
- close M003 as rejected/conditionally unresolved with evidence;
- do not disable the auto ceiling, add a second connection, or weaken gates inside M003.

## 8. Failure, cancellation, restart, contention semantics

### Notification loss/coalescing

Notification is optimization, not durable state. The commit sequence plus fallback timer makes coalescing safe.

### Commit ambiguity

Only a COMMIT reported successful generates the signal. Existing fail-closed commit/rollback ambiguity remains unchanged.

### Gate contention

Event checkpointing remains optional. `try_acquire` failure never queues behind foreground work and never consumes the pending commit watermark.

### SQLite worker cancellation

Aborting the async callback does not imply cancellation of an already-running tokio-rusqlite worker closure. Existing shutdown truthfulness remains required; tests and closure must not claim stronger cancellation than the worker supplies.

### Reload

The checkpoint task is process-owned and survives safe generation swaps. A successful explicit reload transaction can signal it only after releasing the DB gate. Reload must not register a duplicate wake/task.

### Restart/recovery

The event signal is in-memory and need not survive process restart. SQLite WAL + automatic fallback + startup recovery remain durable authorities. The first post-start successful commit or the fallback timer resumes maintenance opportunity.

### Shutdown

Shutdown cancellation wins over a stored event wake. The task supervisor joins/terminates the checkpoint owner before database close under the existing process shutdown order.

## 9. Compatibility and migration

No schema migration.

No database file-format migration.

No public API/config/CLI migration.

No task-name change.

No ordinary diagnostic JSON change unless an existing private scalar is repurposed compatibly; feature-only qualification additions are permitted when necessary.

M006's bundled SQLite compatibility remains authoritative.

## 10. Required tests

Database unit/integration:

- successful-commit signal ordinary path;
- successful explicit transaction path;
- rollback/body failure no signal;
- gate-busy watermark preservation;
- below-threshold true-NOOP path;
- threshold PASSIVE path;
- burst coalescing;
- no immediate retry loop.

Task/lifecycle:

- checkpoint event wake before fallback;
- exactly one process-owned checkpoint task;
- cancellation-first shutdown;
- reload keeps one owner;
- runtime lifecycle R006/R008;
- backup/recovery/restart/rehash.

Coordinator/persistence:

- publication/finalization suites;
- metrics persistence;
- database compatibility.

Broad:

- fmt;
- strict clippy;
- full default workspace;
- full no-default workspace;
- locked release build.

Physical:

- paired M006 baseline vs M003 candidate Pi/MMC runs.

## 11. Required verification commands

~~~bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo test --manifest-path rust/Cargo.toml --lib db:: -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --lib task_supervisor -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test database_compatibility -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_publication -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test operations_o006 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test operations_o007 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r006 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r008 -- --test-threads=1
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked --release
git diff --check
~~~

Physical qualification uses `scripts/qualification_sbc.py` and the existing M004 feature-gated checkpoint diagnostics. Closure must record exact commands, binary hashes, target attestation, and artifact paths rather than relying on prose summaries.

## 12. Documentation updates

On implementation/closure:

- `architecture/deep-dive-database.md` — successful-commit signal and maintenance watermark.
- `architecture/deep-dive-background.md` — checkpoint task event+timer wait and sole-owner semantics.
- `plans/subsystems/persistence-roadmap.md`.
- `plans/registry.md`.
- `docs/raspberry-pi.md` only if operator-visible qualification guidance changes; no new tuning advice by default.

Do not rewrite M001/M004/M005 closure history.

## 13. Acceptance criteria

M003 may close as accepted only if all of the following hold.

### Architecture/correctness

- M006 is closed.
- One SQLite connection/gate/worker.
- Exactly one process-owned checkpoint task.
- No WAL hook.
- No second connection/worker.
- No per-request task or unbounded queue.
- Successful-COMMIT-only sequence/wake behavior.
- Rollback/failure produces no wake.
- `GateBusy` does not consume the pending commit sequence.
- Cancellation-first shutdown.
- WAL/NORMAL and 1000-page automatic fallback unchanged.
- Soft threshold remains 256 and timer fallback remains 60 seconds.
- Backup/recovery/reload/restart/shutdown green.
- No schema/public surface change.

### Physical performance

Against three accepted candidate 60-request sequential runs on the qualifying Pi/MMC target:

- p95 total request latency < 100 ms in every run;
- maximum total request latency < 500 ms in every run;
- maximum foreground publication/finalization COMMIT < 50 ms in every run;
- no multi-second foreground COMMIT tail;
- no unexplained foreground gate wait >= 50 ms caused by maintenance;
- event-assisted checkpoint task executes inside the measured finite batch;
- at least one explicit maintenance checkpoint completes before routine traffic reaches the 1000-page automatic fallback;
- checkpoint-maintenance failures = 0;
- WAL growth remains bounded;
- direct provider control remains ordinary;
- pending requests = 0 and active reservations = 0 after stabilization.

For ordinary concurrency-4 target evidence:

- no multi-second maintenance-induced stall;
- no regression in functional IDs, durable convergence, backup/recovery/restart/shutdown;
- any material throughput/CPU/RSS regression relative to the paired M006 baseline must be explained and accepted explicitly, not hidden behind tail improvement.

A passing result must not depend on changing the automatic threshold, disabling it, or adding another connection.

## 14. Stop conditions

Stop and re-plan if:

- M006 is not closed;
- true NOOP or WAL-reset-fixed engine evidence is absent;
- implementation needs a custom WAL hook;
- implementation needs a second connection/worker;
- event wake cannot stay inside the existing process-owned task supervisor;
- successful COMMIT cannot be distinguished cleanly from attempt/rollback;
- a bounded coalescing signal cannot be implemented without a queue;
- shutdown/reload ownership becomes ambiguous;
- candidate only passes by changing 60/256/1000 policy;
- target improvement merely shifts latency from COMMIT to gate wait;
- physical target evidence is unavailable at closure;
- event-driven candidate fails the acceptance gates and further work would require widening architecture.

## 15. Closure evidence required

`plans/closure/persistence/003-status.md` must include:

- M006 closure SHA and exact SQLite engine;
- implementation commit(s);
- code ownership summary for commit sequence/wake/task integration;
- proof of successful-commit-only signaling;
- proof rollback/failure paths do not signal;
- coalescing/boundedness evidence;
- task inventory proof (one process checkpoint task);
- cancellation/reload/restart/backup/recovery evidence;
- full default/no-default/clippy/release results with local vs CI labels;
- exact M006 baseline and M003 candidate binary SHA-256 values;
- Pi/MMC target metadata/effective pragmas;
- paired baseline/candidate run tables;
- publication/finalization COMMIT and gate-wait summaries;
- maintenance event/tick/checkpoint deltas;
- WAL/fallback evidence;
- ordinary concurrency/resource/convergence results;
- final keep/reject disposition;
- severity-tagged residual findings;
- registry/roadmap update and blocker audit.

## 16. Handoff notes

This plan is intentionally blocked until M006 closes.

When unblocked, implement the event source at the database transaction authority, not in publication/finalization call sites. That guarantees metrics, catalog/config writes, reload transactions, and future legitimate database writers share one commit-derived mechanism without duplicating hooks.

The most important ordering rule is:

~~~text
SQLite COMMIT succeeds
    -> release EggPool DB gate
    -> advance successful-commit sequence
    -> notify existing checkpoint task
    -> task tries gate
    -> true NOOP inspection
    -> PASSIVE only when soft threshold is due
~~~

Do not notify before gate release.

The event path is an optimization. The 60-second fallback and SQLite 1000-page auto-checkpoint remain the recovery/safety layers.
