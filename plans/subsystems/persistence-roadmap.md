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

Current EggPool production state after persistence M001/M002/M004 is:

- `Database::configure` keeps WAL/NORMAL and the ordinary SQLite automatic checkpoint threshold unchanged at the effective 1000-page fallback.
- M001 (`6eae94db`, conditionally closed) made the existing process-owned `checkpoint` task opportunistic: a 60-second poll checks a transaction watermark, uses non-queueing `try_acquire`, and runs PASSIVE only at the internal 256-frame soft threshold. M006 upgrades the bundled stack to tokio-rusqlite 0.8.0 / rusqlite 0.40.2 / libsqlite3-sys 0.38.2 with SQLite 3.53.2, and adds a file-backed guard proving NOOP does not advance checkpoint progress. The M003 attempt added a separate successful-COMMIT signal and was reverted after physical qualification showed repeated tail transfer to foreground finalization gate wait; no commit signal exists in the retained runtime.
- M002 (`52494140`, closed) moved deterministic routing-decision preparation outside the database gate, removed the full `SelectionSnapshot` transaction clone, changed the metrics common path to move owned keys/batches, and prepares its repeated UPSERT once per flush transaction with row/rebuffer equivalence tests.
- M004 (`plans/closure/persistence/004-status.md`, closed) collected **14 accepted** Pi 5 / ext4 / MMC physical runs (`artifacts/qualification/m004/`: 11 phase-diagnostic runs plus 3 ordinary benchmark runs) against the production M001 mechanism at HEAD `8113d264`. Three 60s/256 phase runs had maxima of 1709 ms, 561 ms, and 10 943 ms; the slowest request in every run correlated with foreground publication `COMMIT` 522 ms – 10.9 s and `wal_checkpoint_sequence_changed = true`. Three bounded matrix variants (60s/128, 60s/64, 30s/64) and the minimum-cadence 1s/64 stress run all left the foreground tail. M005 (`plans/closure/persistence/005-status.md`) machine-checks that corpus in `tests/tooling/test_persistence_m004_evidence.py` and corrects the measured-window counter interpretation. The periodic strategy was rejected; the landed 60s/256 mechanism remains. M003 has now also been physically evaluated and closed rejected: event assistance removed publication COMMIT tails but caused 1.88–3.30 second finalization gate waits in all three paired candidate runs. Its implementation was reverted; the retained runtime is timer-only on the M006 SQLite 3.53.2 baseline. Further checkpoint redesign requires a new bounded plan.

## 5. Target architecture

The target remains one SQLite authority.

The reviewed M003 event design used a private successful-COMMIT sequence plus a coalescing Tokio wake emitted only after COMMIT success and database-gate release. The implementation was rejected because target measurements showed it transfers storage stalls to foreground finalization gate wait; it is not part of the target runtime. The existing process-owned `checkpoint` task and 60-second timer remain. A custom SQLite WAL hook remains rejected because SQLite's automatic checkpoint mechanism owns/replaces the same hook slot, and the 1000-page fallback must remain intact.

Routine checkpointing continues to use the existing process-owned maintenance boundary and existing DB worker. M001's narrow periodic candidate was rejected by M004 rather than widened in place. M003's event-driven candidate was also rejected and reverted; its physical evidence constrains any future redesign to avoid foreground gate-wait tail transfer while retaining the automatic checkpoint as the hard safety ceiling.

Request publication/finalization continue to own their current transactions. Deterministic data preparation that does not require SQLite should happen before gate acquisition. Metrics flush should move existing owned data rather than recursively cloning it and should prepare its repeated UPSERT once per transaction.

## 6. Dependency graph

- Legacy Plan 239 physical diagnostic → hard evidence dependency for M001.
- Legacy Plan 240 design handoff → hard design constraint for M001.
- M001 closure evidence showing periodic insufficiency → hard evidence dependency for M003. M004/M005 supply and correct this evidence; M003's architecture review and dedicated `003` plan are now registered.
- M004 Pi/MMC target → operational evidence dependency for M001 production-default acceptance (now satisfied by M004 rejection evidence; the landed M001 mechanism is retained as additive-safe and its performance claim is unfulfilled).
- Current publication/finalization ownership contract → interface dependency for M002; already stable.
- M001 and M002 are otherwise independent and may be implemented in either order.
- M003's evidence, architecture, and M006 engine dependencies were met. The implementation plan is now closed rejected after paired target qualification; its runtime changes were reverted. No successor is currently eligible. A future checkpoint redesign needs a new bounded plan with a design that does not transfer storage stalls to foreground gate wait.

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

### Milestone 003 — Event-driven checkpoint coordination

Class: infrastructure

Status: closed — rejected by paired Pi/MMC acceptance gates; runtime changes reverted

Objective:

Design one process-owned coalesced checkpoint trigger that can intercept routine finite-burst WAL growth before the 1000-page automatic fallback without per-request task spawning or a second SQLite connection. The trigger must preserve the durability/safety semantics, the single-connection/gate/worker contract, and the existing maintenance-task boundary.

Dependencies:

- Hard: M001 closure evidence must explicitly show the periodic strategy is insufficient — supplied by the M004 closure (`plans/closure/persistence/004-status.md`, closed 2026-09-27 against HEAD `8113d264`). **Satisfied.**
- Architecture review: **satisfied for planning** by `plans/implementation/persistence/003-event-driven-checkpoint-coordination.md`. Selected boundary is a successful-COMMIT-derived coalescing wake feeding the existing single process-owned checkpoint task; SQLite WAL hooks, per-request tasks, and second connections are rejected.
- Hard: M006 SQLite NOOP/WAL-reset safety baseline. **Satisfied** by `plans/closure/persistence/006-status.md`; implementation baseline is accepted closure `abfdb18b`.
- Own implementation plan at local number `003`: `plans/implementation/persistence/003-event-driven-checkpoint-coordination.md`. **Closed with rejected disposition**; attempted runtime changes were reverted in `29bcbb4e`.
- Operational: focus on the same Pi/MMC target class; paired M006-baseline/M003-candidate physical evidence required at closure.

Deliverable boundary:

Paired Pi/MMC qualification is complete. The event-assisted candidate removed publication-COMMIT tails but transferred 1.88–3.30 second stalls into foreground finalization gate wait, violating the no-tail-transfer gate. M003 is closed rejected and its runtime implementation is absent from the retained tree.

Exit conditions:

Closure evidence is `plans/closure/persistence/003-status.md`. Any future checkpoint redesign must be a new bounded milestone and must prove that storage work neither returns to foreground COMMIT nor transfers into foreground database-gate wait.


### Milestone 004 — Physical checkpoint qualification and final disposition

Class: polish

Objective:

Resolve M001's outstanding Pi/MMC condition with the already-landed qualification tooling, select keep/retune/reject for the periodic checkpoint strategy, and reconcile current planning/documentation state.

Dependencies:

- Hard/interface: M001 mechanism landed and conditionally closed; M002 closed.
- Operational: qualifying Linux/aarch64 Raspberry Pi-class MMC target.
- No dependency on routing-selection or provider-transport work.

Deliverable boundary:

- Physical 60-request phase evidence for current 60s/256 plus a small feature-gated threshold/cadence matrix.
- Ordinary physical benchmark/concurrency and backup/restart/recovery/shutdown evidence.
- At most a constant-only production retune of checkpoint cadence/soft threshold when target evidence clearly supports it.
- M004 closure deciding whether M003 remains unpromoted or becomes the next design milestone.
- Registry/roadmap/current-architecture reconciliation; historical closure records stay historical.

User or operator value:

Turn the landed conservative checkpoint mechanism into a target-qualified keep decision or produce the evidence needed for a separate event-driven design, without weakening durability or adding speculative complexity.

Exit conditions:

- M004 closure records exact target/build hashes, matrix results, phase/gate/WAL evidence, lifecycle checks, and final decision.
- If a periodic candidate is accepted, M001's outstanding condition is satisfied through the M004 closure and M003 remains not started.
- If periodic scheduling is insufficient, M003 is promoted for separate design/implementation planning; M004 does not implement it.
- Registry contains only actually-ready work in its dependency-ready table and current docs no longer overstate qualification.

**Closure (2026-09-27, HEAD `8113d264`):** periodic strategy rejected on the target class. **14 accepted** physical runs (11 phase-diagnostic + 3 ordinary benchmark) captured at `artifacts/qualification/m004/`. No allowed periodic candidate clears the plan §13 gates; landed 60s/256 mechanism retained as additive-safe (per plan §9); M003's evidence dependency satisfied. The closure's own evidence narration was corrected by M005 without editing the closed record. See `plans/closure/persistence/004-status.md` and `plans/closure/persistence/005-status.md`.


### Milestone 005 — M004 evidence and planning reconciliation corrective pass

Class: polish

Objective:

Correct M004's cumulative-vs-delta checkpoint narration, accepted-artifact count, M003 lifecycle/numbering, and current control-surface wording without changing runtime behavior or rewriting the immutable M004 closure.

Dependencies:

- Hard: M004 closure and committed `artifacts/qualification/m004/` evidence.
- No operational hardware dependency; this pass consumes committed evidence only.

Deliverable boundary:

- Test-only evidence guard over the committed M004 artifact corpus.
- Correct current roadmap/registry/architecture statements.
- M005 closure as the authoritative correction layer.
- M003 remains out of implementation scope; future event-driven planning retains local number 003.

Exit conditions:

- Current docs use measured-window `deltas`, not cumulative counters, for in-batch maintenance claims.
- Artifact census is machine-verified.
- M003 lifecycle no longer implies implementation readiness before its architecture review and dedicated `003` plan.
- Zero production Rust diff.

**Closure (HEAD `be370a61` + this record):** 14 accepted M004 artifacts
machine-checked by `tests/tooling/test_persistence_m004_evidence.py`; zero
in-batch checkpoint ticks in every 30 s/60 s phase run; three in-window ticks,
all `gate_busy`, in the 1s/64 stress run; M004's rejection outcome and no-retune
decision preserved; M003 returned to `proposed` with local number 003 reserved.
See `plans/closure/persistence/005-status.md`.


### Milestone 006 — SQLite NOOP and WAL-reset safety baseline

Class: invariant

Status: closed

Objective:

Upgrade the bundled SQLite stack before M003 so `wal_checkpoint(NOOP)` is truly observational and the runtime is on a WAL-reset-fixed engine, while preserving one connection/gate/worker, WAL/NORMAL, the 1000-page automatic fallback, schema 54, and all public behavior.

Dependencies:

- Hard: M005 closed; current M004/M005 evidence accepted.
- Interface: existing tokio-rusqlite `Connection` / rusqlite API boundary.
- No Pi/MMC dependency for implementation; M003 owns physical performance qualification.

Deliverable boundary:

- tokio-rusqlite 0.8.x migration with reviewed rusqlite/libsqlite3-sys lock.
- Bundled SQLite >= 3.51.3 (planned 3.53.2 baseline).
- Regression proof that NOOP does not checkpoint frames.
- No hooks feature, custom WAL hook, public config, schema change, second connection, or checkpoint-policy retune.

Exit conditions:

- dependency and MSRV qualification green;
- true-NOOP regression guard green;
- database/backup/recovery/lifecycle/full workspace green;
- exact engine and feature graph recorded;
- M003 hard dependency is satisfied; its blocked-to-ready promotion is recorded in a separate registry commit.

**Closure:** See `plans/closure/persistence/006-status.md`.


### Milestone 007 — Dedicated checkpointer qualification experiment

Class: infrastructure

Status: blocked

Objective:

Test, strictly under a new repository-only qualification feature, whether one checkpoint-only SQLite connection/worker can run PASSIVE checkpoints concurrently with the existing foreground SQLite authority and eliminate both the foreground COMMIT tail and M003's finalization gate-wait transfer.

Dependencies:

- Hard: M003 closed rejected with paired Pi/MMC tail-transfer evidence.
- Hard: M006 closed with SQLite 3.53.2 true-NOOP/WAL-reset-fixed baseline.
- Interface: existing process-owned checkpoint task and physical qualification diagnostics.
- Operational: qualifying Linux/aarch64 Pi 5-class ext4/MMC target required for closure.

Deliverable boundary:

- New non-default `qualification-dedicated-checkpointer` feature layered on `qualification-db-diagnostics`.
- Startup-only qualification toggle; disabled mode remains one connection.
- Exactly one feature-private checkpoint-only connection/worker when enabled.
- Same-binary disabled/enabled target comparison plus bounded steady-state WAL evidence.
- No production/default topology change and no production adoption decision.

Exit conditions:

- Local PASSIVE/write concurrency and lifecycle guards pass.
- Three paired control/candidate target runs plus fixed 300-request candidate convergence evidence are recorded.
- Candidate either clears the existing latency/integrity gates or is rejected truthfully.
- Positive evidence may justify proposing a separate ADR; it does not authorize a production second connection.

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
- M007 is the only authorized exception for experimenting with a second SQLite worker, and only behind its new non-default qualification feature. Production remains one connection/gate/worker.
- If a clean routing/persistence optimization requires a public API break, new dependency, or durable production concurrency owner, stop and re-plan.

## 11. Completion definition

This roadmap remains active while the foreground SQLite checkpoint tail is unresolved. M004 rejected timer-only scheduling, M003 rejected same-gate event-assisted scheduling, and M006 established the retained SQLite safety baseline. M007 is the evidence-only dedicated-checkpointer topology experiment; it does not alter production architecture. M007 has now been physically qualified on a Raspberry Pi 5/MMC and rejected: the dedicated candidate failed WAL progress and convergence gates. No production change was made. The workstream remains active because the foreground SQLite checkpoint tail is unresolved; any alternative requires a new bounded plan and must not transfer the tail to foreground gate/I/O wait.

## 12. Milestone status

| Milestone | Status | Implementation plan | Closure record | Blockers |
|---|---|---|---|---|
| 001 — bounded passive checkpoint scheduling and target qualification | conditionally closed (periodic strategy now disproven on target; mechanism retained as additive-safe) | plans/implementation/persistence/001-bounded-passive-checkpoint-scheduling-and-qualification.md | plans/closure/persistence/001-status.md | physical Pi/MMC condition resolved by M004 rejection; performance claim unfulfilled |
| 002 — single-gate tenure and metrics-flush allocation cleanup | closed | plans/implementation/persistence/002-single-gate-tenure-and-metrics-flush-allocation-cleanup.md | plans/closure/persistence/002-status.md | none |
| 003 — event-driven checkpoint coordination | closed — rejected by paired Pi/MMC acceptance gates; runtime changes reverted | plans/implementation/persistence/003-event-driven-checkpoint-coordination.md | plans/closure/persistence/003-status.md | no eligible successor; further design requires a new bounded plan |
| 004 — physical checkpoint qualification and final disposition | closed — periodic strategy insufficient on target; evidence narration corrected by M005 | plans/implementation/persistence/004-physical-checkpoint-qualification-and-final-disposition.md | plans/closure/persistence/004-status.md | none — historical closure remains immutable |
| 005 — M004 evidence and planning reconciliation corrective pass | closed | plans/implementation/persistence/005-m004-evidence-and-planning-reconciliation-corrective-pass.md | plans/closure/persistence/005-status.md | none — committed artifacts were sufficient |
| 006 — SQLite NOOP and WAL-reset safety baseline | closed | plans/implementation/persistence/006-sqlite-noop-and-wal-reset-safety-baseline.md | plans/closure/persistence/006-status.md | none |
| 007 — dedicated checkpointer qualification experiment | closed — rejected by Pi 5/MMC WAL progress/convergence gates; qualification-only implementation not adopted | plans/implementation/persistence/007-dedicated-checkpointer-qualification-experiment.md | plans/closure/persistence/007-pi5-qualification.md | none; no successor registered — any alternative checkpoint/storage design requires a new bounded plan |
