# Persistence Milestone 008 — Rollback-Journal PERSIST Qualification and Write-Amplification Attribution

Status: active

Repository baseline: `849dfd5fd26afc027fc1f00316dea70d62973a52`

Source roadmap:

- `plans/subsystems/persistence-roadmap.md#milestone-008--rollback-journal-persist-qualification-and-write-amplification-attribution`

Evidence lineage:

- `plans/closure/persistence/003-status.md` — same-gate event-assisted WAL checkpointing rejected after 1.88–3.30 s finalization gate-wait transfer
- `plans/closure/persistence/004-status.md` and `005-status.md` — periodic same-connection checkpointing rejected/corrected on Raspberry Pi 5/MMC
- `plans/closure/persistence/006-status.md` — SQLite 3.53.2 true-NOOP/WAL-reset-fixed engine baseline
- `plans/closure/persistence/007-pi5-qualification.md` — dedicated PASSIVE checkpointer rejected: foreground latency became low, but WAL progress/convergence failed
- `artifacts/qualification/m007-pi5-2026-10-03/` — paired physical control/candidate corpus

Long-term requirements:

- `plans/000-long-term-specification.md` — persistence integrity, bounded resources, fail-closed ownership, evidence-gated performance changes
- `plans/002-long-term-roadmap.md` — persistence/publication bounds
- `plans/003-planning-process.md` — bounded milestone, operational evidence, closure, and ADR thresholds

External research references:

- SQLite WAL performance/checkpoint semantics — `https://sqlite.org/wal.html`
- SQLite `journal_mode=PERSIST`, `journal_size_limit`, and `synchronous=EXTRA` — `https://sqlite.org/pragma.html`
- SQLite rollback-journal atomic-commit behavior — `https://sqlite.org/atomiccommit.html`
- SQLite temporary/rollback journal behavior — `https://sqlite.org/tempfiles.html`

Applicable ADRs:

- No ADR is required for M008 because the alternate journal mode is repository-only qualification behavior on isolated benchmark databases. Default/release behavior remains WAL + NORMAL on one connection/gate/worker.
- A positive M008 result MUST NOT change production defaults directly. Shipping PERSIST changes the durable journal/durability/concurrency contract and requires a separate ADR or explicit architecture decision plus a separate production milestone.
- A rejected M008 result MUST NOT directly implement a control-database/outbox/analytics-database split. That architecture crosses durable storage ownership and requires a separately bounded research/ADR plan.

Primary class: infrastructure

## 1. Objective

Determine whether EggPool's serialized one-connection workload performs better on the physical Raspberry Pi/MMC target when SQLite's foreground WAL/checkpoint cycle is removed entirely and replaced, for qualification only, by rollback-journal `PERSIST` with `synchronous=EXTRA`.

M008 tests a different journaling architecture, not another WAL checkpoint schedule.

The experiment must:

- preserve exactly one SQLite connection, one EggPool database gate, and one tokio-rusqlite worker in control and candidate modes;
- use the same feature-built release binary for paired control/candidate runs;
- leave control mode on the current production WAL + NORMAL + automatic-checkpoint behavior;
- force only the candidate onto `journal_mode=PERSIST` + `synchronous=EXTRA`;
- run only against runner-owned isolated database files;
- collect publication/finalization request and COMMIT latency with the existing Plan-239/M007 phase instrumentation;
- collect a separate, explicitly non-acceptance worker-I/O attribution cohort so measurement overhead does not contaminate the latency corpus;
- prove backup, recovery, restart, reload/rehash, shutdown, integrity, and bounded journal-file behavior;
- decide only whether PERSIST merits a later production ADR/adoption plan.

## 2. Why this milestone is ready

All hard design/evidence dependencies are closed.

M007 materially narrowed the problem. Its dedicated checkpointer candidate kept 60-request foreground p95 at 3–4 ms and maxima at 8–17 ms with negligible EggPool gate wait, proving that moving checkpoint work away from foreground COMMIT can remove the request tail. The same candidate nevertheless exceeded the 1,000-frame gate and failed sustained WAL progress: the 300-request report finished at 2,107 log frames / 257 checkpointed with a roughly 68 MB WAL. The remaining problem is therefore not principally Tokio wake-up, EggPool gate acquisition, or a missing PASSIVE scheduler.

EggPool also serializes ordinary database access through one application connection/gate/worker. WAL's principal reader/writer concurrency advantage is consequently less central inside EggPool than in a multi-connection workload, while EggPool still pays WAL checkpoint I/O on the MMC target.

SQLite provides a bounded alternative already present in the bundled engine:

- PERSIST uses rollback journaling but retains the journal file and invalidates its header rather than deleting it after every transaction;
- rollback journal mode has no WAL checkpoint/catch-up phase;
- `synchronous=EXTRA` is the conservative rollback-journal durability setting;
- non-WAL journal modes are not persistent across connection reopen, so the candidate can be reapplied at startup without creating a new durable public configuration contract.

No new crate or SQLite fork is required.

## 3. Experiment architecture

### 3.1 Same topology, different journaling algorithm

Control and candidate both use:

- one `AsyncConnection`;
- one `Arc<Semaphore>` EggPool gate;
- one tokio-rusqlite worker;
- the same publication/finalization transactions;
- the same schema 54 database;
- the same provider/request workload.

Control:

~~~text
journal_mode = WAL
synchronous = NORMAL
wal_autocheckpoint = current production value (1000-page fallback)
existing M001 checkpoint maintenance remains present
~~~

Candidate:

~~~text
journal_mode = PERSIST
synchronous = EXTRA
no WAL checkpoint operation is applicable
same one connection / gate / worker
~~~

The candidate must not add another SQLite handle, background worker, writer, task, queue, or journal-management service.

### 3.2 Repository-only feature and startup toggle

Add a new non-default feature:

~~~toml
qualification-persist-journal = ["qualification-db-diagnostics"]
~~~

Add one startup-only candidate toggle:

~~~text
EGGPOOL_QUALIFICATION_PERSIST_JOURNAL=1
~~~

Rules:

- the feature alone changes no effective storage behavior;
- absent/false toggle is the paired WAL/NORMAL control;
- enabled candidate is permitted only for a file-backed writable database;
- candidate startup fails closed unless the ordinary configuration requests the current expected `wal=true` and `synchronous="NORMAL"` baseline;
- candidate mode sets PERSIST instead of first establishing WAL; it must not create a transient WAL merely as a setup step;
- candidate mode then sets and verifies `synchronous=EXTRA`;
- invalid toggle values fail closed;
- the toggle is not part of Config, not reloadable, and not documented as operator tuning;
- `database.wal` and `database.synchronous` public semantics remain unchanged;
- `qualification-dedicated-checkpointer` enabled mode is incompatible and must fail closed if combined with the M008 candidate.

### 3.3 Isolated-database safety rule

The SBC runner's M008 mode must require `--diagnostic-database-dir` (or an equivalent existing runner-owned isolation proof) and create a fresh temporary child database for every invocation.

Do not run the candidate against the operator's configured production database.

The same binary may be reused across invocations; the same database file must not be reused across control/candidate phases because WAL mode is persistent and M008 is comparing startup configurations, not conversion residue.

A restart/lifecycle check may reopen the candidate's own isolated database with the M008 toggle still enabled and must verify effective PERSIST/EXTRA again.

### 3.4 Candidate checkpoint-task behavior

The process-owned checkpoint task remains supervised so lifecycle topology is not changed.

When M008 candidate mode is active:

- checkpoint maintenance must return before issuing `wal_checkpoint(NOOP)` or `wal_checkpoint(PASSIVE)`;
- it must not poll WAL state, run a rollback-journal substitute checkpoint, or add a new maintenance loop;
- cancellation/shutdown ownership remains unchanged;
- qualification output must state that WAL checkpoint maintenance is not applicable in candidate mode.

Control mode remains exactly the current M001 timer/threshold path.

### 3.5 Journal-size behavior

Do not add a new M008 journal-size tuning knob.

The candidate inherits the configured `journal_size_limit` if one already exists. The canonical SBC fixture should retain its existing value so M008 changes only journal mode + synchronous policy.

The runner records:

- main database bytes;
- rollback-journal bytes when present;
- WAL/SHM bytes when present;
- before/after each fixed workload window.

The candidate must not leave an active WAL/SHM storage path during its measured workload.

PERSIST journal growth is evidence, not something to tune during M008. If the journal grows pathologically, close rejected or open a separately justified follow-up rather than searching limits.

## 4. Write-amplification attribution

M008 needs evidence for the possible post-rejection architecture decision, but that evidence must not contaminate the primary latency comparison.

### 4.1 Separate attribution cohort

Primary 3x control + 3x candidate acceptance runs MUST NOT enable new per-transaction I/O reads.

Add a second qualification-only toggle under the M008 feature, preferred name:

~~~text
EGGPOOL_QUALIFICATION_WORKER_IO_ATTRIBUTION=1
~~~

It is used only in separate supplemental runs.

### 4.2 Linux worker-thread I/O proxy

On Linux, while executing a named database transaction on the tokio-rusqlite worker, read the worker thread's kernel I/O counters from `/proc/thread-self/io` immediately before BEGIN and after COMMIT/rollback.

Record only the delta of the scalar `write_bytes` counter.

Requirements:

- no unsafe/FFI solely for attribution;
- no file path appears in serialized diagnostics;
- no request/account/provider/model identity is recorded;
- values are aggregated by existing `TransactionKind` (Publication, Finalization, Other);
- output is bounded: count, sum, max, and a fixed histogram or bounded average per kind;
- counter rollback/parse failure fails the attribution run rather than fabricating zero;
- non-Linux builds compile normally and must fail closed if the attribution toggle is explicitly requested;
- this is a kernel-attributed worker-write proxy, not a claim about physical NAND write amplification.

### 4.3 Attribution interpretation

Run one fixed 60-request attribution phase in control and one in candidate from the same binary/target.

Closure must report, at minimum:

- worker `write_bytes` per completed Publication transaction;
- worker `write_bytes` per completed Finalization transaction;
- totals per 60-request phase;
- candidate/control ratio;
- database/journal/WAL file-size deltas;
- row/transaction counts used as denominators.

These values are diagnostic. They do not override the latency/integrity acceptance gates.

If M008 is rejected and a future control/outbox/analytics split is researched, this evidence is the input for deciding whether publication, finalization, routing-decision history, or another durable family is the dominant write source.

## 5. Current implementation evidence

Authority paths:

- `rust/src/config.rs::DatabaseConfig`
  - public `wal: bool`, `synchronous: String`, and optional `journal_size_limit`;
  - default remains WAL/NORMAL.
- `rust/src/db/connection.rs`
  - one ordinary `AsyncConnection`, gate, worker;
  - `Database::configure` establishes WAL when requested and applies synchronous mode;
  - named transaction instrumentation already distinguishes Publication / Finalization / Other;
  - qualification snapshots already expose effective journal mode, synchronous mode, page size, and WAL autocheckpoint state.
- `rust/src/task_supervisor.rs`
  - one process-owned checkpoint task.
- `rust/src/runtime_lifecycle/process.rs`
  - process-level database/task lifecycle ownership.
- `scripts/qualification_sbc.py`
  - physical board/storage attestation;
  - isolated diagnostic DB support;
  - 60-request publication/finalization phase diagnostics;
  - same-binary M007 paired mode;
  - lifecycle, convergence, resource, and checksum evidence.
- `rust/tests/database_compatibility.rs`
  - file-backed journal/checkpoint compatibility guards.
- `plans/closure/persistence/007-pi5-qualification.md`
  - current target-class falsification evidence.

The public `wal=false` option is not an M008 implementation seam: current `Database::configure` simply refrains from setting WAL and does not define PERSIST. M008 must not silently reinterpret that public option.

## 6. Non-regressing invariants

### Ordinary/default runtime

- One SQLite connection/gate/worker.
- WAL + NORMAL default.
- Existing 1000-page SQLite automatic fallback.
- Existing M001 timer/256-frame maintenance behavior.
- No M008 environment parsing or branch in builds without the feature beyond compile-time elimination.
- No schema migration.
- No public Config/CLI/HTTP/Rust API change.
- No dependency change.
- Publication/finalization transaction boundaries and response timing unchanged.
- Backup/recovery/restart/reload/shutdown semantics unchanged.

### M008 control

- Same feature-built binary as candidate.
- Toggle disabled.
- Effective behavior byte-for-byte/current-semantics equivalent to ordinary WAL/NORMAL qualification behavior.
- Existing checkpoint diagnostics remain applicable.

### M008 candidate

- Exactly one SQLite connection/gate/worker.
- PERSIST + EXTRA asserted after startup.
- No WAL checkpoint operation.
- No second connection.
- No WAL hook.
- No locking-mode change.
- No MEMORY/OFF journal.
- No relaxed synchronous mode.
- No early response before durable transaction completion.
- No unbounded queue or maintenance retry loop.
- Every candidate file belongs to runner-owned isolated qualification storage.

## 7. Ordered work packages

### Work package A — Feature/toggle and effective-pragmas seam

Implement the non-default feature and startup-only candidate toggle.

Required guards:

- ordinary/default feature graph unchanged;
- feature + toggle off => WAL/NORMAL;
- feature + toggle on => PERSIST/EXTRA on file-backed writable DB;
- in-memory/read-only candidate activation rejected;
- non-default public DB config baseline rejected for candidate rather than silently overwritten;
- invalid values rejected;
- M007 enabled candidate + M008 candidate rejected;
- effective qualification snapshot reports actual journal/synchronous state.

### Work package B — PERSIST-aware maintenance/lifecycle behavior

Make the existing checkpoint task harmless in M008 candidate mode without creating a replacement scheduler.

Required tests:

- candidate maintenance issues no NOOP/PASSIVE WAL pragma;
- control still exercises the current maintenance boundary;
- cancellation and shutdown complete with the existing task owner;
- restart with candidate toggle reapplies PERSIST/EXTRA;
- restart without the M008 candidate returns to the ordinary configured WAL behavior;
- no worker/task count increase.

### Work package C — Local rollback-journal correctness probes

Use temporary file-backed databases.

Prove:

- PERSIST transaction commit survives close/reopen;
- rollback after injected body failure preserves pre-transaction state;
- injected COMMIT failure remains fail-closed under the existing transaction wrapper;
- `PRAGMA quick_check` and foreign-key checks are clean;
- backup and isolated restore are valid;
- candidate can transition from a fresh database into PERSIST without transient WAL artifacts in the measured phase;
- current schema-54 migrations work;
- no regression in reservation/publication/finalization idempotency.

Do not use MEMORY/OFF or `locking_mode=EXCLUSIVE`.

### Work package D — Worker-I/O attribution

Add the feature-only Linux `/proc/thread-self/io` scalar collector described in §4.

Required tests:

- parser accepts realistic proc format and ignores unrelated fields;
- monotonic `write_bytes` delta;
- malformed/missing field fails the attribution mode;
- Publication/Finalization buckets remain bounded and do not expose identity;
- toggle disabled performs no proc read;
- non-Linux explicit attribution request fails clearly;
- attribution output is absent from ordinary runtime JSON.

### Work package E — SBC runner extension

Add explicit runner modes, preferred CLI shape:

~~~text
--diagnose-persist-journal {control,candidate}
--diagnose-persist-journal-steady-state
--diagnose-worker-io-attribution
~~~

Rules:

- requires `--diagnose-publication-phases`;
- requires `--diagnostic-database-dir`;
- control and candidate record the same release-binary SHA-256;
- candidate sets only the M008 startup toggle;
- attribution flag is prohibited in primary latency acceptance runs;
- steady-state mode only valid for candidate;
- M007 and M008 modes are mutually exclusive;
- reports include effective pragmas, file-size facts, transaction-phase summaries, convergence, resource use, and lifecycle outcomes;
- candidate report explicitly marks WAL checkpoint metrics not applicable.

Update tooling tests for parser validation, mode compatibility, env projection, isolation, report fields, and failure behavior.

### Work package F — Physical paired qualification

Build one release binary on the target with `qualification-persist-journal`.

Record binary checksum and physical attestation.

Primary acceptance corpus, attribution disabled:

- 3 × 60-request WAL/NORMAL control;
- 3 × 60-request PERSIST/EXTRA candidate.

Extended candidate corpus:

- one five-window 300-request sequential run;
- existing concurrency-4 workload;
- stabilization/convergence;
- backup;
- isolated recovery;
- restart/reconciliation;
- rehash/reload;
- graceful shutdown;
- quick/integrity and foreign-key checks as supported by the existing runner.

Supplemental attribution corpus, excluded from latency acceptance:

- 1 × fixed 60-request control with worker-I/O attribution;
- 1 × fixed 60-request candidate with worker-I/O attribution.

Do not retune journal mode, synchronous level, journal-size limit, checkpoint cadence, or transaction shape during this corpus.

### Work package G — Decision and documentation

Write `plans/closure/persistence/008-status.md`.

There are three valid dispositions:

1. **positive qualification** — candidate clears all gates; production remains WAL/NORMAL; open a separate ADR/adoption plan only after explicit architecture review;
2. **rejected** — candidate misses latency, integrity, lifecycle, or bounded-storage gates; no journal-mode adoption; write-amplification evidence may justify a separate control/outbox/analytics architecture research plan;
3. **inconclusive** — target/evidence contamination prevents a truthful decision; production remains unchanged and the exact missing evidence is recorded.

Do not implement a two-database split in M008.

## 8. Storage, migration, and compatibility effects

No schema migration.

No production database migration.

No change to default Config semantics.

The qualification candidate changes journal mode only on isolated test databases. Since non-WAL modes are not retained as a durable replacement for WAL across ordinary reopen, candidate startup must reassert PERSIST and ordinary configured startup must continue to establish WAL when `wal=true`.

A future production PERSIST adoption, if ever approved, requires separate review of:

- existing WAL database transition;
- crash/power-loss durability;
- external reader behavior;
- backup/recovery;
- journal-size limits;
- downgrade/rollback;
- observability/tooling assumptions.

M008 does not decide those production migration details.

## 9. Security and privacy

Qualification output remains scalar/aggregate.

Never serialize:

- database path;
- SQL text;
- request/proxy IDs;
- account/provider/model identifiers;
- prompts/bodies;
- credentials;
- raw journal/WAL contents;
- procfs contents other than the derived scalar delta.

The runner may retain board/filesystem/storage-class attestation as existing qualification artifacts do.

## 10. Required focused tests

At minimum:

- `database_compatibility`;
- coordinator publication;
- coordinator finalization;
- operations backup/recovery;
- runtime lifecycle/reload;
- feature/toggle matrix;
- PERSIST/EXTRA effective pragma guard;
- candidate checkpoint-maintenance not-applicable guard;
- rollback/COMMIT-failure behavior;
- restart reapplication;
- worker-I/O parser/aggregation;
- SBC tooling mode/validation tests.

Use existing test names where possible rather than creating parallel semantic fixtures.

## 11. Required verification commands

~~~bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo test --manifest-path rust/Cargo.toml --test database_compatibility -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test database_compatibility --features qualification-persist-journal -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_publication -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test operations_o006 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r008 -- --test-threads=1
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --features qualification-persist-journal -- -D warnings
CARGO_BUILD_JOBS=1 cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
CARGO_BUILD_JOBS=1 cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked --release
cargo build --manifest-path rust/Cargo.toml --locked --release --features qualification-persist-journal
cargo +1.89.0 check --manifest-path rust/Cargo.toml --workspace --all-targets
uv run --frozen ruff format --check scripts/ tests/tooling/
uv run --frozen ruff check scripts/ tests/tooling/
uv run --frozen pyright scripts/
uv run --frozen pytest tests/tooling/ -q --tb=short --maxfail=1
git diff --check
~~~

If the exact lifecycle test target has moved, use the current equivalent and record the substitution in closure evidence rather than dropping the gate.

## 12. Physical acceptance criteria

### Isolation and effective configuration

All must pass:

- one connection/gate/worker in control and candidate;
- same binary SHA for paired corpus;
- fresh isolated DB per invocation;
- control effective WAL/NORMAL;
- candidate effective PERSIST/EXTRA;
- candidate uses no WAL checkpoint operation;
- no M007 dedicated checkpointer;
- no public config/schema/dependency change.

### Correctness/lifecycle

All must pass:

- publication/finalization semantic suites;
- no database busy/integrity errors caused by the candidate;
- rollback/commit-failure behavior;
- quick/integrity + foreign-key checks;
- backup and isolated recovery;
- restart/reconciliation;
- reload/rehash;
- graceful shutdown;
- final pending requests = 0;
- final active reservations = 0.

### Every candidate 60-request primary run

All three runs must satisfy:

- p95 total request latency < 100 ms;
- max total request latency < 500 ms;
- max publication COMMIT < 500 ms;
- max finalization COMMIT < 500 ms;
- max foreground DB-gate wait < 50 ms;
- no multi-second request/database phase;
- zero integrity or lifecycle failures.

The closure must still report p50 and compare control/candidate distributions. There is intentionally no separate median acceptance threshold: M008 is testing whether a more even per-COMMIT cost can replace rare multi-second tails, and the p95/max gates bound how much steady-state cost is acceptable.

### Extended 300-request candidate

All must pass:

- each fixed 60-request window remains below the 100 ms p95 / 500 ms max request gates;
- no monotonic runaway rollback-journal growth across all five windows;
- journal maximum/final bytes recorded;
- no WAL/SHM workload path appears;
- convergence reaches zero pending requests/reservations;
- concurrency-4 and lifecycle corpus green;
- peak RSS/thread count recorded;
- no medium-or-higher correctness/integrity finding.

### Attribution cohort

Attribution runs are valid only if:

- latency gates are not derived from them;
- worker `write_bytes` counters are available and monotonic;
- publication/finalization counts match the expected workload;
- control and candidate are the same binary/target/storage class;
- candidate/control byte ratios and per-transaction averages are recorded truthfully.

There is no write-byte pass/fail threshold in M008. This evidence guides the next architecture decision.

## 13. Stop conditions

Stop and report rather than widen scope if:

- PERSIST requires a second connection, worker, or process;
- candidate needs `locking_mode=EXCLUSIVE`;
- candidate only passes by lowering synchronous below EXTRA;
- candidate requires MEMORY/OFF, DELETE/TRUNCATE tuning, WAL2, or a SQLite fork;
- journal-size tuning becomes necessary to rescue the candidate;
- a schema/index rewrite is required;
- public `database.wal` or `database.synchronous` semantics would need reinterpretation;
- the candidate database cannot be isolated from operator state;
- the checkpoint task cannot be made non-applicable without changing ordinary behavior;
- procfs attribution would require unsafe/FFI or pollute ordinary builds;
- latency merely changes from rare multi-second WAL checkpoint stalls to repeated >500 ms rollback-journal commits;
- backup/recovery/restart/integrity semantics regress;
- physical Pi/MMC evidence is unavailable at closure;
- implementation expands into production adoption or a two-database architecture.

## 14. Explicitly rejected/deferred alternatives

Not part of M008:

- another PASSIVE/FULL/RESTART/TRUNCATE WAL checkpoint scheduling pass;
- raising `wal_autocheckpoint`;
- threshold/cadence search;
- WAL2: conceptually relevant but not SQLite trunk/stable production policy and changes storage compatibility;
- DELETE/TRUNCATE rollback-mode comparison matrix;
- `locking_mode=EXCLUSIVE`;
- a second SQLite connection;
- a control/outbox/analytics database split;
- dropping observability indexes without measured attribution.

If PERSIST is rejected, the next research question is whether separating correctness state from replayable analytics/history can reduce critical-path page/index writes. That must be a new bounded architecture/ADR plan informed by M008 attribution evidence.

## 15. Documentation updates on implementation/closure

Update as applicable:

- `architecture/deep-dive-database.md`;
- `architecture/deep-dive-background.md` if checkpoint-task candidate behavior needs explanation;
- `plans/subsystems/persistence-roadmap.md`;
- `plans/registry.md`;
- qualification runner comments/docs.

Do not rewrite immutable M003–M007 closure records.

Do not advertise the M008 toggle as an operator setting.

## 16. Closure evidence required

`plans/closure/persistence/008-status.md` must contain:

- implementation commit(s);
- exact SQLite/rusqlite/tokio-rusqlite versions;
- feature/toggle activation matrix;
- proof ordinary/default and feature-control builds remain WAL/NORMAL;
- proof one connection/gate/worker in both M008 modes;
- exact effective pragmas;
- physical board/filesystem/storage attestation;
- release-binary SHA-256;
- three control + three candidate 60-request tables;
- p50/p95/max request and publication/finalization COMMIT/gate summaries;
- 300-request five-window candidate table;
- DB/journal/WAL/SHM size facts;
- worker-I/O attribution tables and limitations;
- backup/recovery/restart/rehash/shutdown/integrity evidence;
- resource/thread evidence;
- default/no-default/feature-specific tests;
- strict Clippy/fmt/release/tooling results;
- local versus hosted evidence labels;
- severity-tagged unresolved findings;
- positive/rejected/inconclusive recommendation;
- explicit statement that production remains WAL/NORMAL;
- explicit successor decision: ADR/adoption research after positive evidence, or separately bounded control/outbox/analytics architecture research after rejection if warranted.

## 17. Handoff notes

Do not optimize PERSIST before measuring it.

The primary question is:

> On the same Raspberry Pi/MMC workload, does paying rollback-journal durability work on each transaction produce an acceptably bounded distribution while eliminating the multi-second WAL checkpoint tail?

The secondary question is:

> If it does not, which named transaction class is responsible for the greatest kernel-attributed SQLite-worker write volume?

Keep those questions separate. The primary paired corpus decides the journal-mode hypothesis. The supplemental attribution corpus only decides what evidence should drive the next architecture design.
