# Persistence Roadmap

Status: active

Long-term references:

- plans/000-long-term-specification.md — §2 invariant 4, §3 ownership boundaries, §5 performance posture
- plans/002-long-term-roadmap.md — Phase 3 persistence and publication bounds
- plans/003-planning-process.md — evidence-gated polish and closure requirements

Related ADRs:

- None required for the current milestones. These are reversible internal optimizations that preserve the single-connection/gate contract and public compatibility surface.
- Stop and require a new ADR or explicit architecture decision if implementation needs a second SQLite connection/writer, a public checkpoint configuration surface, altered durability semantics, or a new process-lifecycle authority.

## 1. Purpose and ownership boundary

This subsystem owns the native SQLite persistence performance boundary after the migration and residual-efficiency campaigns. Its production authority is rust/src/db/, with request publication/finalization callers under rust/src/coordinator/, low-wear analytics under rust/src/operations/metrics.rs, and the process-owned maintenance schedule under rust/src/task_supervisor.rs and rust/src/runtime_lifecycle/process.rs.

The roadmap is deliberately narrow. It improves where checkpoint work executes and how much avoidable CPU/allocation is performed around the one SQLite gate. It does not change request semantics, wire behavior, routing policy, provider transport, schema compatibility, or the public HTTP/CLI/Rust API.

## 2. Work classification

### Invariants

- Keep one SQLite connection, one serialized database gate, and one tokio-rusqlite worker.
- Keep WAL mode and synchronous = NORMAL unless a separately reviewed durability decision supersedes this roadmap.
- Preserve publication/finalization transaction atomicity, idempotency, compensation, and startup reconciliation.
- Commit/rollback ambiguity continues to fail closed.
- Backup, restore, reload, shutdown, and recovery remain bounded and coherent with WAL state.
- Credentials, prompts, raw request/provider bodies, cache keys, SQL text, and filesystem paths do not enter diagnostics.
- Qualification-only database diagnostics remain absent from ordinary release behavior.

### Capabilities

No new user-facing capability is planned. Existing inference, statistics, backup/restore, reload, and operator surfaces must remain behaviorally compatible.

### Infrastructure

- Existing process-owned checkpoint task and Database checkpoint primitives.
- Existing qualification-db-diagnostics feature and SBC qualification runner.
- Existing metrics coalescer and durable publication/finalization repositories.

### Polish

- Move routine checkpoint work away from latency-sensitive request COMMIT where evidence permits.
- Reduce avoidable serialization, cloning, and repeated SQLite statement preparation around the shared database gate.
- Keep the implementation simpler than adding a pool, second writer, or generic background-work framework.

## 3. Non-goals

- No second SQLite connection, read pool, writer pool, or checkpoint-only connection.
- No synchronous = OFF, journal-mode change, relaxed transaction durability, or early finite-response delivery.
- No public wal_autocheckpoint/checkpoint tuning key in Config, CLI, environment documentation, or example config.
- No schema migration solely for performance.
- No replacement of tokio-rusqlite.
- No multithread Tokio runtime or additional runtime worker pool.
- No unbounded queue or checkpoint drain.
- No permanent benchmark dependency or hardware CI.
- No rewrite of completed legacy Plans 237–240 or 242.

## 4. Current state

The repository already has unusually strong target evidence.

Plan 239 and artifacts/qualification/239-sbc-db-phase-checkpoint-diagnostic.json localized the physical Raspberry Pi 5/MMC finite tail to SQLite automatic-checkpoint work performed by foreground COMMIT. With 1000-page auto-checkpointing, three 60-request runs recorded maximum request latencies of 4009 ms, 12012 ms, and 14903 ms, with publication COMMIT dominating the worst samples. With qualification-only wal_autocheckpoint = 0, the three maxima fell to 14 ms, 5 ms, and 4 ms and the foreground commit phases remained sub-millisecond. A 256-page automatic threshold traded the large pauses for more frequent visible pauses and still reached 826–3042 ms maxima.

SQLite documents the same mechanism: automatic WAL checkpoints are PASSIVE, default to 1000 pages, and run from the COMMIT that crosses the threshold. The application may instead initiate PASSIVE checkpoints; disabling automatic checkpointing without another bound can allow the WAL to grow excessively.

Current EggPool production behavior therefore remains conservative:

- Database::configure enables WAL/NORMAL and does not override wal_autocheckpoint.
- Database::checkpoint runs PRAGMA wal_checkpoint(PASSIVE) on the existing connection.
- The process-owned checkpoint task runs immediately at startup and then every 14,400 seconds.
- The automatic SQLite threshold remains the effective safety mechanism during ordinary traffic.
- Plans 239/240 did not authorize a production default change.

A second independent inefficiency remains around the same serialized gate:

- coordinator/publication.rs builds routing-decision JSON inside the publication transaction and clones a full SelectionSnapshot solely to move data into the worker closure.
- operations/metrics.rs clones owned metric-key strings on enqueue, clones them again when building a flush batch, deep-clones the complete row batch for failure recovery, and calls connection.execute with the same large UPSERT once per row, causing repeated prepare/finalize work while the gate is held.

## 5. Target architecture

The target remains one SQLite authority.

Routine checkpointing should use the existing process-owned maintenance boundary and existing DB worker. The first production candidate must be opportunistic and bounded: inspect WAL state only after writes, avoid queueing a checkpoint behind an already-busy database, run PASSIVE work before the default automatic threshold when the target workload allows it, and retain the existing automatic checkpoint as a hard safety ceiling until physical evidence proves that a stronger policy is safe. If this narrow periodic strategy cannot prevent the measured foreground tail, it must stop rather than grow into an event-driven checkpoint subsystem inside the same milestone.

Request publication/finalization continue to own their current transactions. Deterministic data preparation that does not require SQLite should happen before gate acquisition. Metrics flush should move existing owned data rather than recursively cloning it and should prepare its repeated UPSERT once per transaction.

## 6. Dependency graph

- Legacy Plan 239 physical diagnostic → hard evidence dependency for M001.
- Legacy Plan 240 design handoff → hard design constraint for M001.
- Physical Raspberry Pi-class MMC qualification → operational closure dependency for M001 production-default acceptance.
- Current publication/finalization ownership contract → interface dependency for M002; already stable.
- M001 and M002 are otherwise independent and may be implemented in either order.
- A future event-driven checkpoint coordinator is conditional on M001 failing its narrow periodic design; it is not dependency-ready now.

## 7. Milestones

### Milestone 001 — Bounded passive checkpoint scheduling and target qualification

Class: polish

Objective:

Use the existing checkpoint task and existing DB worker to perform bounded opportunistic PASSIVE checkpoints before routine traffic reaches the SQLite automatic-checkpoint threshold, without weakening the existing automatic threshold safety ceiling or changing durability.

Dependencies:

- Hard: Plans 239 and 240 evidence/design constraints.
- Operational: physical Linux/aarch64 Raspberry Pi-class MMC evidence before closure as a production performance improvement.

Deliverable boundary:

- Internal checkpoint policy/result types only.
- Non-blocking/skip-capable task behavior on the one gate.
- WAL-page observation through SQLite, not filesystem polling.
- Qualification-only tuning hooks if needed to select constants.
- No public config or schema change.

User or operator value:

Eliminate or substantially reduce multi-second local finite-request tails caused by automatic checkpoint work while preserving persistence semantics.

Exit conditions:

- Foreground publication/finalization COMMIT no longer owns routine checkpoint work in the accepted target corpus.
- WAL growth remains bounded by the unchanged SQLite automatic safety ceiling.
- Backup/restore, restart, reload, recovery, shutdown, and cancellation evidence is green.
- If the narrow periodic candidate cannot prevent the tail, close the milestone as a keep decision and open a separate event-driven design rather than widening scope.

Deferred work:

- Event-triggered checkpoint coordination.
- Disabling automatic checkpointing in ordinary production.
- Any second connection/writer design.

### Milestone 002 — Single-gate tenure and metrics-flush allocation cleanup

Class: polish

Objective:

Reduce avoidable CPU, heap churn, and repeated statement preparation around the existing serialized database gate without changing transaction contents or persistence behavior.

Dependencies:

- Interface: current publication/finalization and metrics schemas; already stable.
- No hard dependency on M001.

Deliverable boundary:

- Precompute routing-decision serialization/scalars before publication acquires the DB gate.
- Move only the minimal prepared routing row into the publication transaction.
- Move metric-event key strings rather than cloning them when ownership permits.
- Build flush rows by consuming the buffered map; avoid a second deep batch clone for failure recovery.
- Prepare the metrics UPSERT once per flush transaction and execute it for each row.
- Preserve failure rebuffering, capacity/drop counters, ordering, and immediate/low-wear semantics.

User or operator value:

Lower local request overhead and reduce the chance that analytics work occupies the database gate longer than necessary.

Exit conditions:

- Durable rows and public metrics snapshots remain equivalent.
- Publication/finalization ownership and errors remain equivalent.
- operations_o007 and publication/finalization/database suites pass.
- No new dependency, config key, migration, or synchronization authority.

### Milestone 003 — Event-driven checkpoint coordination, conditional

Class: infrastructure

Objective:

Only if M001 demonstrates that a periodic opportunistic task cannot checkpoint soon enough to avoid the measured foreground tail, design one process-owned coalesced checkpoint trigger without per-request task spawning or a second SQLite connection.

Dependencies:

- Hard: M001 closure evidence must explicitly show the periodic strategy is insufficient.
- Architecture review required before an implementation plan is written.

Deliverable boundary:

No implementation plan is authorized by this roadmap yet.

Exit conditions:

A separate reviewed plan proves lifecycle, cancellation, shutdown, WAL bounds, and single-worker ownership before code changes.

## 8. Cross-cutting requirements

Storage and migration: no schema change. WAL/NORMAL and schema 54 remain authoritative.

Protocol and compatibility: no HTTP, wire, CLI, config, or public Rust API change.

Security: diagnostics remain scalar/metadata-only. Never expose SQL, request identity, body, credential, provider/model/account names, or database path in new checkpoint observations.

Concurrency and cancellation: the single DB gate remains authoritative. A maintenance checkpoint must not wait behind a known busy gate merely to perform optional work. Do not claim that aborting an async task cancels an already-running SQLite worker closure; shutdown evidence must reflect the real worker ownership.

Recovery: backup_to, startup reconciliation, restore, and close must continue to work whether the WAL is below or above the soft maintenance threshold.

Observability: use existing task snapshots and qualification diagnostics. Any new ordinary runtime counter must be bounded, scalar, and justified; prefer no new public runtime JSON.

Performance: structural allocation/statement-preparation removal is acceptable evidence for M002. M001 needs the existing physical target-class method because its claim is storage-specific.

## 9. Verification strategy

Focused persistence/coordinator tests:

~~~bash
cargo test --manifest-path rust/Cargo.toml --test database_compatibility -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_publication -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test operations_o006 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test operations_o007 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r008 -- --test-threads=1
~~~

Run strict formatting/clippy, default and no-default serial workspace suites, and locked release build before closure. M001 additionally runs the existing SBC qualification tooling and records the physical target, filesystem/storage class, effective pragmas, WAL-frame observations, task ticks, foreground transaction phases, and request latency distribution.

## 10. Risks and decision points

- A periodic checkpoint may still begin just before a foreground request arrives. Physical evidence must include DB gate-wait time, not only COMMIT time.
- Lower checkpoint thresholds can trade rare large pauses for frequent smaller I/O, as Plan 239 H2 already demonstrated.
- Disabling automatic checkpointing would remove SQLite's existing growth safety. That is intentionally outside M001 unless a later reviewed design supplies an equally strong bound.
- Metrics flush failure rebuffering is correctness behavior, not expendable analytics polish.
- Moving publication serialization outside the transaction must preserve its existing observable error category and fault-injection stages.
- If a clean routing/persistence optimization requires a public API break, new dependency, or new concurrency owner, stop and re-plan.

## 11. Completion definition

This roadmap closes when the accepted checkpoint policy has physical target evidence or a documented keep decision, single-gate CPU/allocation cleanup is closed, no high/medium correctness finding remains, current persistence architecture/docs describe the resulting policy, and any need for event-driven checkpoint coordination is either explicitly rejected or represented by a separate reviewed milestone.

## 12. Milestone status

| Milestone | Status | Implementation plan | Closure record | Blockers |
|---|---|---|---|---|
| 001 — bounded passive checkpoint scheduling and target qualification | conditionally closed | plans/implementation/persistence/001-bounded-passive-checkpoint-scheduling-and-qualification.md | plans/closure/persistence/001-status.md | production mechanism landed; physical Pi/MMC target evidence outstanding per closure §11 |
| 002 — single-gate tenure and metrics-flush allocation cleanup | closed | plans/implementation/persistence/002-single-gate-tenure-and-metrics-flush-allocation-cleanup.md | plans/closure/persistence/002-status.md | none |
| 003 — event-driven checkpoint coordination, conditional | not started | — | — | only if M001 proves periodic scheduling insufficient |
