# Persistence Milestone 007 — Dedicated Checkpointer Qualification Experiment

Status: active

Repository baseline: `3270f4b71b2a7dd5115d9b8e1666a789ac292676`

Source roadmap:

- `plans/subsystems/persistence-roadmap.md#milestone-007--dedicated-checkpointer-qualification-experiment`

Evidence lineage:

- `plans/closure/persistence/003-status.md` — same-gate event-assisted candidate rejected and reverted
- `plans/closure/persistence/004-status.md` — timer-only checkpoint strategy rejected on Pi/MMC
- `plans/closure/persistence/005-status.md` — M004 evidence correction
- `plans/closure/persistence/006-status.md` — SQLite 3.53.2 true-NOOP/WAL-reset-fixed baseline
- `artifacts/qualification/m003/`
- `artifacts/qualification/m004/`

Long-term requirements:

- `plans/000-long-term-specification.md` — persistence integrity, bounded resources, fail-closed ownership
- `plans/002-long-term-roadmap.md` — persistence/performance posture
- `plans/003-planning-process.md` — evidence-gated architecture and closure rules

External research references:

- SQLite WAL concurrency/performance — `https://sqlite.org/wal.html`
- SQLite checkpoint modes — `https://sqlite.org/c3ref/wal_checkpoint_v2.html`
- SQLite automatic checkpoint semantics — `https://sqlite.org/c3ref/wal_autocheckpoint.html`
- SQLite WAL hook ownership — `https://sqlite.org/c3ref/wal_hook.html`

Applicable ADRs:

- None required for this experiment because the second connection/worker is qualification-only and absent from ordinary builds/runtime behavior.
- A positive M007 result MUST NOT be promoted directly to production. A separate ADR or equivalent explicit architecture decision is required before any production milestone relaxes the one-connection/one-worker invariant.

Primary class: infrastructure

## 1. Objective

Determine whether moving PASSIVE checkpoint I/O onto one dedicated SQLite connection/worker can remove the physical Pi/MMC foreground checkpoint tail without transferring the stall into foreground database-gate wait, while leaving the shipped runtime topology unchanged.

M007 is a qualification experiment, not production adoption.

The experiment must:

- add a new non-default repository-only qualification feature;
- create a second SQLite connection/worker only when that feature is compiled and an explicit startup-only qualification toggle is enabled;
- expose that connection only through fixed NOOP/PASSIVE checkpoint operations;
- preserve the ordinary primary connection/gate/worker and its `wal_autocheckpoint=1000` safety ceiling;
- route a feature-only successful-COMMIT/coalesced wake to the existing process-owned checkpoint task;
- execute checkpoint work on the dedicated connection without acquiring the primary EggPool database gate;
- compare disabled and enabled topology using the same feature-built binary on the same target;
- prove bounded WAL progress, lifecycle safety, and resource cost before any ADR or production design is proposed.

## 2. Why this milestone is ready

All hard evidence dependencies are closed.

M004 proved timer-only same-connection maintenance misses finite bursts. M003 proved that waking checkpoint maintenance on the same connection/gate/worker removes the publication-COMMIT tail but transfers 1.88–3.30 second stalls into finalization gate wait. M006 moved the bundled engine to SQLite 3.53.2 and proved `wal_checkpoint(NOOP)` is observational.

SQLite's WAL contract makes a separate checkpointer the remaining bounded hypothesis:

- an application-initiated checkpoint may run from any writable connection to the same WAL database;
- PASSIVE checkpoints do as much work as possible without waiting for database readers or writers;
- SQLite explicitly recommends a separate thread/process when checkpoint sync work on the writer is undesirable;
- with `synchronous=NORMAL`, checkpoint is where the expensive sync/barrier work occurs.

Repository evidence also supports a bounded experiment:

- `rust/tests/database_compatibility.rs::second_connection_reports_bounded_busy_contention` already proves two independent handles can open the same file and writer contention is bounded.
- Server shutdown stops/joins supervised tasks before Database close.
- M003's successful-COMMIT/coalesced-wake implementation has already been tested once and can be reintroduced only behind the new experiment feature.

No new dependency is required.

## 3. Experiment architecture

### 3.1 Selected topology

One foreground SQLite connection/worker continues to own every repository and transaction.

One checkpoint-only SQLite connection/worker, present only in the M007 qualification build when explicitly enabled, owns:

- `PRAGMA wal_checkpoint(NOOP)`;
- `PRAGMA wal_checkpoint(PASSIVE)`;
- no migrations;
- no schema/application writes;
- no repository access;
- no backup/vacuum/retention calls;
- no general-purpose `call` or transaction interface.

The existing process-owned `checkpoint` task remains the sole checkpoint scheduler/lifecycle owner.

### 3.2 Same-connection variants are out of scope

Dropping only EggPool's semaphore while retaining the same tokio-rusqlite connection does not create checkpoint/write concurrency; both operations still serialize through that connection's one worker queue.

M003 already falsified same-gate event scheduling. M007 must use a distinct SQLite worker in qualification or it is not testing a new hypothesis.

### 3.3 Production architecture remains unchanged

M007 does not decide that EggPool should ship two SQLite connections. It establishes evidence for or against proposing that decision later.

Positive evidence requires a separate ADR/architecture decision and a separate production milestone. Negative evidence closes this topology without a production successor.

## 4. Current implementation evidence

Authority paths:

- `rust/src/db/connection.rs`
  - one `AsyncConnection`;
  - one `Arc<Semaphore>` gate;
  - timer-only `checkpoint_maintenance()`;
  - primary `wal_autocheckpoint=1000`;
  - M006 true-NOOP support.
- `rust/src/task_supervisor.rs`
  - one process-owned `checkpoint` task;
  - 60-second fallback;
  - current checkpoint callback uses the primary Database path.
- `rust/src/runtime_lifecycle/process.rs`
  - process-level Database/checkpoint callback owner.
- `rust/src/server/mod.rs`
  - task supervisor shuts down before Database close.
- `rust/src/db/qualification.rs`
  - bounded scalar-only diagnostic authority.
- `rust/tests/database_compatibility.rs`
  - same-file second-connection contention and M006 NOOP/PASSIVE guards.
- `scripts/qualification_sbc.py`
  - physical target attestation;
  - publication/finalization gate/worker/COMMIT timings;
  - WAL sequence/size facts;
  - checkpoint maintenance counters;
  - fixed 60-request phase diagnostics.

Current feature contract:

- `qualification-db-diagnostics` is Plan-239 instrumentation and must not itself add a second SQLite connection.

Therefore M007 needs a separate feature rather than broadening Plan 239's feature semantics.

## 5. Non-regressing invariants

### Ordinary build/runtime

- Exactly one SQLite connection, one EggPool gate, one SQLite worker.
- No successful-COMMIT notifier or event-driven checkpoint path.
- Existing 60-second / 256-frame M001 policy unchanged.
- Primary `wal_autocheckpoint=1000`.
- WAL + `synchronous=NORMAL`.
- Schema 54 unchanged.
- No public Config/env/CLI/HTTP/Rust API.
- No SQLite WAL hook.
- No new ordinary runtime task.
- No new dependency.
- Publication/finalization transaction boundaries unchanged.
- Backup/recovery/reload/restart/shutdown ownership unchanged.

### Qualification candidate

- At most one additional SQLite connection/worker.
- It is checkpoint-only and not exposed as a general Database/repository handle.
- PASSIVE is the only mutating checkpoint mode.
- FULL/RESTART/TRUNCATE are prohibited.
- It never acquires the primary EggPool gate.
- No unbounded queue or per-request task.
- It is process-owned and closes before the primary SQLite connection.
- The primary 1000-page automatic-checkpoint fallback stays enabled.

## 6. Required implementation changes

### 6.1 New feature and startup-only toggle

Add:

`qualification-dedicated-checkpointer = ["qualification-db-diagnostics"]`

The new feature:

- is non-default;
- is absent from release/package workflows;
- leaves `qualification-db-diagnostics` alone at one connection.

Add a startup-only qualification toggle, preferred name:

`EGGPOOL_QUALIFICATION_DEDICATED_CHECKPOINTER=1`

Rules:

- parsed only under the M007 feature;
- not part of Config;
- not reloadable;
- not documented as operator tuning;
- absent/false means exact current one-connection timer-only behavior;
- invalid values fail closed.

Feature compiled + toggle disabled is the paired control.

### 6.2 Feature-private dedicated connection

Open the experimental connection only after the primary file-backed writable connection has established WAL mode.

The dedicated connection must:

- open the same SQLite file;
- be writable only because checkpoint requires a writable handle;
- assert existing `journal_mode=wal` rather than changing it;
- use `synchronous=NORMAL`;
- set its own connection-local `wal_autocheckpoint=0`;
- leave the primary connection's `wal_autocheckpoint=1000`;
- expose only feature-private NOOP, PASSIVE, and close operations.

Prefer a dedicated private struct rather than a second general `Database` object.

Do not enable `query_only` unless focused evidence proves checkpoint remains legal; do not weaken checkpoint semantics to make that flag work.

### 6.3 Feature-only successful-COMMIT signal

Reintroduce the useful M003 event source only under M007:

- successful-COMMIT sequence;
- coalescing Tokio `Notify`;
- signal only after successful COMMIT and after the primary EggPool gate is released;
- cover implicit and explicit transaction APIs;
- no signal on BEGIN/body/rollback/COMMIT failure.

Do not repurpose the attempt-derived `transactions` counter.

When the M007 toggle is disabled, this signal must not alter checkpoint scheduling.

### 6.4 Existing checkpoint task remains sole scheduler

Do not add another runtime task.

M007 enabled mode makes the existing process-owned checkpoint task wait on:

- cancellation;
- coalesced successful-COMMIT wake;
- existing 60-second fallback.

Its callback uses the dedicated checkpoint connection.

Disabled mode remains the current timer-only primary-Database callback.

SQLite busy/incomplete checkpoint results do not trigger immediate retries. Retry occurs only on a later commit wake or timer fallback.

### 6.5 Dedicated maintenance policy

First candidate uses the existing 256-frame soft threshold.

On an eligible wake:

1. compare current successful-COMMIT sequence with the dedicated maintenance watermark;
2. unchanged => NotDue with no SQLite call;
3. run true NOOP on the dedicated connection;
4. below 256 frames => consume through the observed sequence and return BelowThreshold;
5. due => run PASSIVE on the dedicated connection;
6. record busy/incomplete/progress/failure and elapsed time;
7. never acquire the primary gate.

This is experiment behavior only; M007 does not define a production watermark contract.

### 6.6 Qualification observability

Extend bounded feature-only evidence with:

- dedicated topology enabled/disabled;
- dedicated `journal_mode`, `synchronous`, connection-local `wal_autocheckpoint`;
- primary `wal_autocheckpoint` (must remain 1000);
- event wake count;
- NOOP observations;
- PASSIVE attempts;
- PASSIVE progressed/completed;
- SQLite busy/incomplete outcomes;
- failures;
- maximum observed WAL log frames;
- maximum checkpointed frames;
- bounded checkpoint elapsed summary (count, max, p95 or fixed histogram);
- dedicated close result;
- process thread count when the OS exposes it.

No SQL text, path, request/provider/model/account identity, body, credential, or raw WAL content may be serialized.

Ordinary runtime JSON must not change.

## 7. Ordered work packages

### Work package A — Local two-connection checkpoint/write probe

Intent:

Prove the SQLite-native concurrency hypothesis before adding physical-runner complexity.

Required tests:

- one primary writer connection;
- one checkpoint-only connection to the same file;
- deterministic barriers/channels around concurrent foreground writes and PASSIVE work.

Prove:

- checkpoint does not acquire the primary EggPool gate;
- foreground writer can progress while checkpoint worker is active;
- PASSIVE returns bounded busy/incomplete/progress instead of waiting for foreground writer completion;
- both connections see WAL/NORMAL;
- primary auto-checkpoint remains 1000;
- close order is deterministic.

Stop if local behavior contradicts the hypothesis.

### Work package B — Isolated feature topology

Intent:

Build a removable experiment.

Required changes:

- Cargo feature;
- startup toggle;
- feature-private checkpoint connection;
- exact pragma checks;
- close-before-primary lifecycle.

Acceptance evidence:

- normal build = one connection;
- `qualification-db-diagnostics` alone = one connection;
- M007 feature + toggle disabled = one connection;
- enabled = exactly one additional SQLite worker;
- no repository/general SQL API can access it.

### Work package C — Event wake on the dedicated worker

Intent:

Catch finite bursts without reproducing M003's shared-gate serialization.

Required changes:

- feature-only successful-COMMIT sequence/Notify;
- existing checkpoint task event+timer wait only when enabled;
- dedicated maintenance callback.

Acceptance evidence:

- successful COMMIT wakes/coalesces;
- failure/rollback does not;
- burst commits create no unbounded queue;
- dedicated maintenance never acquires primary gate;
- still exactly one checkpoint process task.

### Work package D — Same-binary A/B qualification mode

Intent:

Isolate topology as the independent variable.

Extend `scripts/qualification_sbc.py` with one explicit M007 qualification mode.

Use one release binary built from one commit with `qualification-dedicated-checkpointer`:

- control: dedicated toggle disabled;
- candidate: same binary, toggle enabled.

Both use:

- primary auto-checkpoint 1000;
- 256-frame soft threshold;
- 60-second fallback;
- same fixture/target.

Artifacts must record topology state and dedicated diagnostics.

### Work package E — Paired Pi/MMC qualification

Required phase corpus:

- 3 control 60-request sequential phase runs;
- 3 candidate 60-request sequential phase runs.

Required candidate convergence/lifecycle corpus:

- ordinary concurrency-4 benchmark;
- one fixed 300-request sequential steady-state run in the same process, reported as five bounded 60-request windows;
- backup;
- recovery;
- restart;
- reload/rehash;
- graceful shutdown.

The extended run must show the checkpointer repeatedly makes progress and catches up after writes stop; WAL progress must not grow monotonically without bound.

### Work package F — Narrow 512-frame branch only if justified

Do not run another broad threshold matrix.

A candidate-only 512-frame follow-up is allowed only if 256:

- removes foreground gate transfer;
- has no multi-second foreground stall;
- but narrowly misses latency gates with evidence pointing to raw storage/checkpoint frequency rather than software serialization.

If 256 produces multi-second foreground latency or another transferred tail, reject rather than tune.

### Work package G — Disposition gate

Valid outcomes:

**Positive qualification:** close M007 with evidence, leave production unchanged, and propose a separate ADR before any production milestone.

**Negative qualification:** close M007 rejected; no automatic successor.

**Inconclusive target evidence:** close conditionally/blocked with exact missing evidence; no production inference.

## 8. Failure, cancellation, restart, contention semantics

### SQLite-level contention

PASSIVE is required because it does not wait for readers/writers. Busy/incomplete is an expected bounded outcome, not a reason for immediate retry.

### Foreground work

Dedicated maintenance never takes the primary EggPool gate. Any foreground gate-wait regression therefore requires separate explanation.

### Storage contention

The second worker can remove software serialization but not MMC I/O contention. Physical request latency remains authoritative.

### Notification coalescing

The successful-COMMIT sequence is feature-private truth. Notify is advisory/coalescing; no per-commit queue exists.

### Shutdown

Required order:

1. checkpoint task cancelled/joined;
2. dedicated checkpoint connection closed;
3. primary Database connection closed.

Do not claim async cancellation stops an already-running SQLite worker closure.

### Restart/reload

Event state is in-memory and need not survive restart. The dedicated connection is process-owned and the toggle is startup-only; reload must not duplicate it.

### Backup/recovery

Backup continues through the primary Database API only. The experimental connection is never a backup source.

## 9. Compatibility and migration

No production migration.

No schema change.

No public config/CLI/API.

No package/release feature selection.

No ordinary runtime topology change.

The experiment can be removed without changing database files or operator state.

## 10. Required tests

Focused:

- feature/toggle activation matrix;
- file-backed writable-only activation;
- primary/dedicated pragma assertions;
- successful-COMMIT-only wake;
- rollback/body/commit-failure suppression;
- coalescing;
- PASSIVE/write concurrency;
- SQLite busy/incomplete handling;
- no primary gate acquisition;
- deterministic connection close order;
- shutdown idle/running;
- reload does not duplicate ownership.

Existing regression targets:

- `database_compatibility`;
- coordinator publication/finalization;
- O006 backup/recovery;
- R006/R008 lifecycle;
- full default/no-default workspace.

Feature-specific tests run with `--features qualification-dedicated-checkpointer`.

Physical:

- same-binary 3x control + 3x candidate;
- fixed 300-request candidate convergence;
- optional 512 branch only under Work Package F predicate.

## 11. Required verification commands

~~~bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo test --manifest-path rust/Cargo.toml --test database_compatibility -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test database_compatibility --features qualification-dedicated-checkpointer -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_publication -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test operations_o006 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r008 -- --test-threads=1
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --features qualification-dedicated-checkpointer -- -D warnings
CARGO_BUILD_JOBS=1 cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
CARGO_BUILD_JOBS=1 cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked --release
cargo build --manifest-path rust/Cargo.toml --locked --release --features qualification-dedicated-checkpointer
cargo +1.89.0 check --manifest-path rust/Cargo.toml --workspace --all-targets
git diff --check
~~~

Closure must record exact physical qualification invocations and distinguish local evidence from hosted CI.

## 12. Documentation updates

Implementation/closure updates:

- `architecture/deep-dive-database.md`;
- `architecture/deep-dive-background.md`;
- `plans/subsystems/persistence-roadmap.md`;
- `plans/registry.md`;
- qualification-tool comments/docs as needed.

Do not rewrite M003/M004/M005/M006 closure records.

Do not expose the qualification toggle as an operator setting.

## 13. Acceptance criteria

M007 may close with a positive qualification result only if all of these hold.

### Isolation

- ordinary/default release = one connection/gate/worker;
- existing Plan-239 feature alone = one connection;
- M007 disabled mode = one connection;
- enabled mode adds exactly one checkpoint-only connection/worker;
- primary auto-checkpoint = 1000;
- dedicated connection auto-checkpoint = 0;
- no WAL hook or public surface change.

### Correctness/lifecycle

- successful-COMMIT-only wake proven;
- no unbounded queue/per-request task;
- one process checkpoint task;
- dedicated maintenance never acquires primary gate;
- PASSIVE busy/incomplete bounded;
- backup/recovery/restart/reload/shutdown green;
- dedicated worker closes before primary;
- no integrity/checkpoint failures.

### Every candidate 60-request run

- p95 total request latency < 100 ms;
- max total request latency < 500 ms;
- max publication COMMIT < 50 ms;
- max finalization COMMIT < 50 ms;
- max foreground DB-gate wait < 50 ms;
- no multi-second foreground phase;
- dedicated PASSIVE makes progress inside the measured batch;
- checkpoint failures = 0;
- maximum observed WAL log frames < 1000;
- direct-provider control ordinary;
- durable convergence reaches zero pending requests and active reservations.

### Extended 300-request run

- no monotonic unbounded WAL-frame growth;
- checkpoint progress occurs in multiple windows;
- after stabilization, progress catches the final WAL end or a bounded reader/writer-limited remainder is explicitly evidenced;
- no request violates the 500 ms maximum because of checkpoint I/O;
- thread/RSS overhead recorded and attributable;
- no high/medium integrity/lifecycle finding.

### Production disposition

Even after positive evidence:

- ordinary production topology remains unchanged;
- M007 does not ship the second connection;
- a separate ADR/architecture decision is required;
- no production follow-up becomes ready automatically.

## 14. Stop conditions

Stop and report rather than improvise if:

- PASSIVE on the second connection blocks foreground writers beyond the bounded hypothesis;
- the second connection needs repository/general SQL APIs;
- it becomes a second application writer;
- primary auto-checkpoint must be disabled/raised to pass;
- FULL/RESTART/TRUNCATE or a WAL hook appears necessary;
- `qualification-db-diagnostics` alone would gain a second connection;
- default/release builds gain the second connection;
- deterministic close order cannot be established;
- backup/recovery/integrity semantics change;
- latency merely moves into SQLite worker/raw-I/O stalls above gates;
- physical Pi/MMC evidence is unavailable at closure;
- scope expands into production adoption.

## 15. Closure evidence required

`plans/closure/persistence/007-implementation-status.md` contains the current
revised-scope implementation and disposition evidence; the earlier
`plans/closure/persistence/007-status.md` remains the pre-reactivation
assessment:

- implementation commit(s);
- exact SQLite stack/build baseline;
- proof normal + Plan-239-only builds stay one-connection;
- feature/toggle activation contract;
- local PASSIVE/write concurrency result;
- same-binary control/candidate SHA-256;
- target attestation;
- three control + three candidate phase tables;
- foreground gate/worker/body/COMMIT summaries;
- dedicated NOOP/PASSIVE/busy/failure/duration/frame counters;
- primary/dedicated effective pragmas;
- 300-request five-window WAL/checkpoint progression;
- ordinary concurrency/resource evidence;
- thread/RSS delta;
- backup/recovery/restart/reload/shutdown evidence;
- default/no-default/feature-specific test and Clippy results;
- local vs hosted-CI labels;
- severity-tagged residual findings;
- positive/rejected/inconclusive disposition;
- explicit ADR/follow-up decision with no automatic promotion.

## 16. Handoff notes

M007 tests topology, not another cadence search.

Start at 256 frames. The only permitted tuning branch is the narrow 512-frame follow-up in Work Package F after evidence first proves the dedicated worker eliminated software serialization.

Enabled qualification flow:

~~~text
primary COMMIT succeeds
    -> release primary EggPool gate
    -> feature-only successful-commit sequence + coalesced notify
    -> existing process checkpoint task wakes
    -> dedicated SQLite worker runs NOOP
    -> dedicated SQLite worker runs PASSIVE when >=256 frames
    -> foreground primary worker remains independently available
~~~

The falsification question is whether the MMC target can sustain checkpoint I/O concurrently enough to keep foreground latency inside the existing gates. If it cannot, stop iterating on scheduling and make an explicit architectural/product decision about the residual storage latency.
