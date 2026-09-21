# Plan 238 — Narrow Publication/Storage Diagnostic

Date: 2026-09-21
Status: implementation handoff
Planning baseline: `6b25f4fbbab1e216363852b749e267bf4b05d241`
Follows: `plans/237-raspberry-pi-finite-tail-diagnostic-pass.md`
Priority: P1 narrow publication/storage localization
Execution target: physical Linux/aarch64 Raspberry Pi-class SBC

## Purpose

Follow up Plan 237 Outcome 1 with one final narrow diagnostic before any
runtime optimization is proposed.

Plan 237 established that the remaining Raspberry Pi finite-request tail is
not a provider-service problem:

- three MMC-backed 60-request runs had worst-request totals of
  `3473 / 2036 / 1785 ms`;
- the corresponding worst pre-provider phases were
  `3467 / 2006 / 1778 ms`;
- provider service never exceeded `1 ms`;
- direct-provider control never exceeded `2 ms`;
- moving the **entire** isolated qualification root to tmpfs reduced the same
  diagnostic maximum to `4 ms`.

That is strong evidence for the local storage/durability side of EggPool's
finite path, but Plan 237 moved config, logs, runtime files, database, WAL, and
SHM together. It therefore does not yet prove that SQLite is the only owner.

Plan 238 must answer two narrower questions:

1. does moving **only the SQLite database/WAL/SHM** to tmpfs remove the tail
   while all other EggPool files stay on MMC?
2. if yes, does the slow request correlate with SQLite WAL checkpoint/reset
   behavior, ordinary publication/finalization database I/O, or a competing
   database task?

This plan remains diagnostic-only. It does not authorize a runtime fix.

---

## Governing constraints

1. **No `rust/src/` change is authorized by this plan.**
2. No Cargo dependency, feature, or release-profile change.
3. No public API or config-default change.
4. No second SQLite connection.
5. Do not change `synchronous = "NORMAL"`, disable WAL, weaken durability,
   increase worker count, add Tokio workers, or change routing/finalization
   ownership.
6. Do not add a second benchmark framework.
7. Reuse `scripts/qualification_sbc.py`, the Plan 236 benchmark fixture, and
   the Plan 237 provider-boundary timing.
8. Diagnostics stay bounded and scalar-only. Never retain prompts, bodies,
   credentials, arbitrary headers, URLs, or filesystem paths in committed
   evidence.
9. Do not mutate the WAL/checkpoint state merely to observe it during a
   measured batch.
10. Stop after localization. Any production change requires a new plan.

If implementing this plan appears to require Rust-side instrumentation, stop
and write a separate diagnostic plan rather than modifying runtime code here.

---

## Authority paths

Read these before implementation:

- `rust/src/coordinator/finite.rs`
- `rust/src/coordinator/publication.rs`
- `rust/src/coordinator/finalization.rs`
- `rust/src/server/inference.rs`
- `rust/src/db/connection.rs`
- `rust/src/task_supervisor.rs`
- `rust/src/operations/metrics.rs`
- `scripts/qualification_sbc.py`
- `tests/tooling/test_qualification_sbc.py`
- `tests/tooling/fixtures/qualification/sbc-benchmark.toml`
- `architecture/deep-dive-database.md`
- `architecture/deep-dive-request-lifecycle.md`
- `architecture/deep-dive-runtime.md`
- `artifacts/qualification/237-sbc-finite-tail-diagnostic.json`

---

## Structural facts to preserve

The implementation model must begin from the current finite lifecycle rather
than inferring a generic "SQLite is slow" story.

### Pre-provider path

For a successful first-attempt finite request,
`FiniteCoordinator::execute_input` currently performs:

~~~text
admission already complete
    -> routing select/claim
    -> PublicationService::publish
         -> publish_transaction
              -> Database::with_transaction
                   -> acquire single DB gate
                   -> BEGIN IMMEDIATE
                   -> request row
                   -> reservation row
                   -> request_attempt row
                   -> routing_decision row
                   -> COMMIT
         -> convert local claim
    -> prepare provider request
    -> provider submit
~~~

The publication rows are deliberately committed in **one SQLite transaction**.
Do not describe request/reservation/attempt/routing-decision inserts as four
independent commits.

The provider cannot receive the request before that publication transaction
has completed.

### Post-provider finite path

After the provider response is decoded, the Axum finite adapter does not return
the client response immediately. `finish_finite_execution`:

1. records the bounded usage metric;
2. awaits `FiniteExecution::complete`;
3. only then returns the finite HTTP response.

With the low-wear benchmark fixture, request usage is buffered rather than
flushed synchronously on every request.

Durable finalization itself is another single
`Database::with_transaction` that checks/transitions:

- request terminal state;
- request-attempt terminal state;
- reservation release;

and then releases runtime claim/quota ownership.

Therefore Plan 237's existing phase split has useful meaning:

~~~text
large pre_provider_ms
    -> publication / DB-gate / local preparation side

large post_provider_ttft_ms
    -> response decode / metrics enqueue / durable finalization / client handoff
~~~

Provider-service time is already measured independently.

### Database serialization

`Database::with_transaction`:

- acquires one process-wide SQLite semaphore;
- issues `BEGIN IMMEDIATE`;
- executes the caller transaction body;
- issues `COMMIT`;
- releases the gate only after the SQLite worker returns.

A slow publication can therefore represent either:

- waiting for the single DB gate;
- SQLite transaction body/write cost;
- COMMIT/durability/checkpoint cost.

This plan must not assume which one without evidence.

### Background database tasks

The task supervisor currently includes database-writing/checkpoint work.
Important scheduling facts include:

- process checkpoint task: runs immediately, then on its long interval;
- metrics flush: first delay is 5 seconds, then the configured interval;
- benchmark fixture metrics interval: 120 seconds;
- catalog refresh: 300 seconds in the benchmark fixture;
- retention cleanup: long interval;
- automatic backup: disabled in the benchmark fixture.

The measured diagnostic window must be quiescent with respect to those tasks
before attributing a stall to the foreground publication transaction.

### WAL behavior

`Database::configure` sets:

- WAL mode;
- `synchronous`;
- `journal_size_limit`;

but does **not** set `wal_autocheckpoint`.

Plan 237/236 WAL evidence was around the low-single-digit MiB range, close
enough to normal SQLite auto-checkpoint scale that checkpoint/reset correlation
must be investigated explicitly.

Do not assume auto-checkpoint is the cause merely from file size.

---

## Diagnostic questions

Plan 238 must answer:

1. Does the pre-provider tail reproduce when only the DB/WAL/SHM remain on
   MMC?
2. Does moving only the DB/WAL/SHM to tmpfs eliminate that tail?
3. During a slow request, does the WAL header's checkpoint sequence change?
4. Do any database-writing background task tick counters change during the
   measured batch?
5. Does the intermittent post-provider tail also contract when only the
   database files move to tmpfs?
6. Is the remaining evidence specific enough to justify a production
   publication/checkpoint/finalization change?

---

## Workstream A — Add a dedicated Plan 238 diagnostic mode

Extend only `scripts/qualification_sbc.py`.

Add a narrow option such as:

~~~text
--diagnose-publication-storage N
~~~

with this contract:

- default: absent/off;
- accepted range: `20..=200`;
- Plan 238 value: `60`;
- requires the physical SBC hardware gate;
- requires the benchmark fixture
  `tests/tooling/fixtures/qualification/sbc-benchmark.toml`;
- does **not** require running the full Plan 236 benchmark corpus first;
- when active, the report uses the existing extended
  `runtime-q008.v2` schema;
- ordinary/default qualification remains `runtime-q008.v1`;
- existing `--diagnose-finite-tail` behavior remains unchanged.

The new mode should run after normal functional health/admission checks but
**before** the standard benchmark batches so the WAL position is not inherited
from an arbitrary Plan 236 workload.

Do not silently reorder ordinary Q008 behavior when the new flag is absent.

---

## Workstream B — Establish a quiescent database-task window

Before the measured publication/storage batch:

1. complete the normal functional warm-up;
2. poll the authenticated `/api/stats/runtime` projection;
3. wait for the process-owned startup checkpoint opportunity to finish;
4. wait for the first metrics-flush opportunity to finish;
5. require all observed database-writing tasks to report `in_tick = false`;
6. capture baseline `tick_count` values.

At minimum track these fixed task names when present:

~~~text
checkpoint
metrics_flush
catalog_refresh
retention_cleanup
automatic_backup
~~~

Then begin the 60-request batch immediately.

At the end of the batch:

- capture task tick counts again;
- if any relevant task tick count changed during the measured window, mark the
  run `background_db_activity = true`;
- do not silently delete the slow request;
- do not use a contaminated run as the primary owner-classification run;
- repeat from a fresh root when necessary.

The runner may use a bounded wait (for example 15 seconds) to reach the
quiescent state. Failure to reach it is `not measured`, not a reason to alter
task schedules or production config.

This workstream is important because a slow foreground publication waiting
behind metrics/checkpoint work is a different finding from a slow publication
commit of its own.

---

## Workstream C — Preserve Plan 237 provider-boundary timing

Reuse the existing monotonic phases:

~~~text
T0 client request start
T1 loopback provider receives request
T2 loopback provider finishes response write
T3 diagnostic client receives first byte
T4 diagnostic client finishes response
~~~

Continue reporting:

~~~text
pre_provider_ms
provider_service_ms
post_provider_ttft_ms
client_body_ms
total_ms
~~~

Use:

- 5 unrecorded warm-ups;
- 60 measured sequential native Responses finite requests;
- one direct-provider control with 5 warm-ups + 30 measured requests.

Retain only:

- p50/p95/max per phase;
- completed/timeout/failure counts;
- the five slowest scalar-only records.

Do not report p99.

---

## Workstream D — Add bounded read-only WAL-header observation

Do not open a second SQLite connection on every request.

Instead, add a tiny file-level WAL snapshot helper that performs only:

- `stat` on the database, WAL, and SHM files;
- at most one bounded read of the first 32 bytes of the WAL file.

The committed projection may contain only scalar facts such as:

- WAL present;
- WAL size bytes;
- SHM size bytes;
- database size bytes;
- WAL header page size when valid;
- WAL checkpoint-sequence value when valid;
- whether checkpoint sequence changed since the previous sample;
- WAL size delta since the previous sample.

Do not retain the database path or raw header bytes.

### Sampling cadence

Take:

1. one WAL snapshot immediately before the measured batch;
2. one WAL snapshot after each measured finite request.

Use the previous post-request snapshot as the next request's baseline.
Do not read twice per request.

For each retained slowest-five request, append only:

~~~text
wal_bytes_before
wal_bytes_after
wal_bytes_delta
wal_checkpoint_sequence_before
wal_checkpoint_sequence_after
wal_checkpoint_sequence_changed
~~~

The report may additionally aggregate:

- count of measured requests with checkpoint-sequence change;
- count of slowest-five requests with checkpoint-sequence change;
- maximum request latency with and without checkpoint-sequence change.

### Important limitation

A WAL checkpoint sequence change is evidence of reset/checkpoint-cycle
activity, but absence of a sequence change does not prove that no storage
sync/writeback occurred.

Do not execute `PRAGMA wal_checkpoint` during the measured batch. It would
change the state being diagnosed.

---

## Workstream E — Database-only tmpfs isolation

Plan 237 moved the whole temporary root to tmpfs. Plan 238 must isolate the
database specifically.

Add tooling support to place **only** these files on a separate temporary
filesystem:

~~~text
usage.sqlite3
usage.sqlite3-wal
usage.sqlite3-shm
~~~

while keeping these on the normal MMC-backed qualification root:

- rendered config;
- stdout/stderr logs;
- runtime/PID/control files;
- XDG config/data/state directories other than the DB path;
- backup/recovery directories.

The rendered benchmark config must point `[database].path` at the isolated
database directory.

### Comparison matrix

Use the same candidate and same benchmark fixture for both classes:

| Class | Config/log/runtime root | SQLite DB/WAL/SHM |
|---|---|---|
| A | MMC/ext4 | MMC/ext4 |
| B | MMC/ext4 | tmpfs (`/dev/shm`) |

All other settings stay identical:

- WAL enabled;
- `synchronous = "NORMAL"`;
- one database worker/gate;
- same low-wear cadence;
- same provider fixture;
- same request corpus;
- same binary SHA.

Run three fresh-root Class A passes.

If the tail reproduces, run three fresh-root Class B passes.

If `/dev/shm` is unavailable or too small, record Class B as
`not measured`; do not substitute a durability change.

### Conditional inverse control

Only if Class B does **not** clarify the result, one inverse control is allowed:

| Class | Config/log/runtime root | SQLite DB/WAL/SHM |
|---|---|---|
| C | tmpfs | MMC/ext4 |

This distinguishes database storage from some other local filesystem owner.

Do not run Class C unless A/B remain ambiguous.

---

## Workstream F — Interpret publication and finalization separately

For each slow request, classify its dominant phase.

### Pre-provider-dominated

When:

~~~text
pre_provider_ms >> provider_service_ms and post_provider_ttft_ms
~~~

the only foreground durable request boundary before provider submission is the
publication transaction.

If Class B removes this tail, classify the owner as:

~~~text
publication SQLite/storage path
~~~

subject to the checkpoint/task-correlation refinements below.

### Post-provider-dominated

When:

~~~text
post_provider_ttft_ms >> pre_provider_ms and provider_service_ms
~~~

remember that finite response delivery awaits durable finalization.

If Class B removes this tail, classify it as:

~~~text
finite durable-finalization SQLite/storage path
~~~

Do not reopen streaming from this finding.

### Provider-dominated

If `provider_service_ms` or the direct-provider control becomes slow, the run
is not valid evidence for publication/storage ownership.

---

## Workstream G — Checkpoint/background correlation decision table

Apply this order.

### G1. Slow request + WAL checkpoint-sequence change + Class B removes tail

Classification:

~~~text
SQLite WAL checkpoint/reset activity is strongly correlated with the
foreground storage stall
~~~

Next step:

- write a separate implementation/qualification plan to test an explicit,
  controlled checkpoint policy or checkpoint placement;
- preserve WAL and `synchronous = "NORMAL"`;
- do not jump to a second DB connection.

### G2. Slow request + no checkpoint-sequence change + Class B removes tail

Classification:

~~~text
SQLite publication/finalization storage I/O is localized, but checkpoint is
not proven
~~~

Next step:

- write a separate narrow plan for gate-vs-transaction-vs-commit timing;
- only then consider transaction-shape changes.

### G3. Slow request coincides with database-task tick change

Classification:

~~~text
foreground request contended with supervised database maintenance
~~~

Next step:

- reproduce in a quiescent fresh-root window first;
- if repeatable, write a scheduling/maintenance-boundary plan;
- do not redesign publication from a contaminated run.

### G4. Class B does not improve, but Plan 237 all-root tmpfs did

Classification:

~~~text
non-database local filesystem path remains suspect
~~~

Next step:

- use Class C if needed;
- follow config/log/runtime file ownership, not SQLite architecture.

### G5. Class A fails to reproduce in three fresh-root runs

Classification:

~~~text
not reproducible under controlled quiescent conditions
~~~

Next step:

- close the performance line with no production optimization.

---

## Workstream H — Static transaction-count evidence

Record the structural transaction map in the Plan 238 artifact so later work
does not rediscover it.

For a successful single-attempt native finite request under the low-wear
fixture, the expected foreground durable shape is:

~~~text
pre-provider:
  1 publication transaction
    - request
    - reservation
    - request_attempt
    - routing_decision

post-provider:
  1 durable finalization transaction
    - request terminal update
    - request_attempt terminal update
    - reservation release
~~~

The usage metrics coalescer is buffered under `write_mode = "low_wear"`; a
periodic metrics task is separate from the request's foreground transaction.

Do not add runtime transaction counters solely to confirm this structural fact.
Use source inspection plus quiescent task evidence.

Retries are out of scope for this corpus; every diagnostic request must use the
single successful first-attempt fixture route.

---

## Physical execution

Use the same Pi-class target and exact release candidate where practical.

Record:

- repository SHA;
- candidate SHA-256;
- binary size;
- board model;
- OS/kernel;
- CPU governor/frequency;
- thermal start/end;
- MMC/ext4 facts;
- tmpfs availability/capacity.

Build or verify the candidate:

~~~bash
cargo build --manifest-path rust/Cargo.toml --locked --release
sha256sum rust/target/release/eggpool
~~~

Example Class A command shape:

~~~bash
uv run python scripts/qualification_sbc.py \
  --binary rust/target/release/eggpool \
  --candidate-origin on-device-release-build \
  --expected-sha256 <candidate-sha256> \
  --config-fixture tests/tooling/fixtures/qualification/sbc-benchmark.toml \
  --diagnose-publication-storage 60 \
  --output artifacts/qualification/238-sbc-publication-storage-mmc-run-1.json
~~~

Example Class B may use a tooling-only argument such as:

~~~text
--diagnostic-database-dir /dev/shm/<private-temp-dir>
~~~

The runner must create and clean the directory itself and must not commit that
path into evidence.

Do not combine this diagnostic with the full Plan 236 benchmark corpus.

---

## Evidence artifact

Commit one sanitized aggregate:

~~~text
artifacts/qualification/238-sbc-publication-storage-diagnostic.json
~~~

Preserve Plans 235–237 artifacts unchanged.

The aggregate must contain:

- exact candidate/repository SHA;
- board/storage environment;
- structural foreground transaction map;
- task-quiescence criteria;
- task tick baseline/final deltas by fixed task name;
- three Class A run summaries;
- three Class B run summaries when available;
- Class C only when required;
- direct-provider controls;
- p50/p95/max phase timings;
- slowest-five scalar records;
- WAL size/checkpoint-sequence correlation;
- resource convergence;
- timeout/failure counts;
- explicit classification from G1–G5;
- whether a production follow-up plan is justified.

No raw WAL header bytes, database paths, request content, credentials, or
provider bodies may enter the artifact.

---

## Resource and correctness guardrails

Every accepted run must finish with:

- zero pending requests;
- zero active reservations;
- zero finalization jobs;
- zero active leases attributable to the batch;
- zero retiring leases attributable to the batch;
- zero terminal references attributable to the batch;
- process healthy;
- no hidden timed-out request.

Timeouts remain evidence. They may not be silently omitted from a run summary.

If a timeout occurs before provider receipt, preserve its scalar phase state and
WAL/task observations.

---

## Tooling tests

Add focused tests for:

- `--diagnose-publication-storage` range and default-off behavior;
- default Q008 remains `runtime-q008.v1`;
- Plan 236 benchmark remains `runtime-q008.v2`;
- Plan 237 `--diagnose-finite-tail` behavior is unchanged;
- Plan 238 mode does not require or run the normal benchmark corpus;
- quiescent-task snapshot uses only fixed task names;
- contaminated runs are marked rather than silently accepted;
- WAL reader consumes at most the fixed header bound;
- WAL projection retains scalars only;
- invalid/short WAL headers yield `unavailable`, not failure;
- checkpoint-sequence change arithmetic is deterministic;
- database-only tmpfs rendering changes only `[database].path`;
- committed diagnostic output contains no paths, bodies, credentials, URLs,
  raw WAL bytes, or p99;
- direct-provider control remains fixed-path and secret-free;
- slowest-five retention remains bounded.

Run:

~~~bash
uv sync --frozen
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run pytest tests/tooling/test_qualification_sbc.py -q
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
uv run python scripts/validate_release_docs.py
git diff --check
~~~

No Rust source change is expected. If `rust/src/`, `rust/Cargo.toml`, or
`rust/Cargo.lock` changes, stop Plan 238 rather than expanding its scope.

---

## Explicit non-goals

Do not use this plan to:

- add another SQLite connection;
- move the production DB to tmpfs;
- weaken durability;
- disable WAL;
- set `synchronous=OFF`;
- change `journal_size_limit`;
- change `wal_autocheckpoint`;
- introduce manual checkpoint scheduling;
- batch request publication/finalization;
- remove routing-decision persistence;
- return finite responses before durable finalization;
- add Tokio workers;
- change routing selection locking;
- add perf/eBPF/profiler dependencies;
- add hardware CI;
- establish performance SLAs.

Those are possible future decisions only if this plan produces targeted
evidence.

---

## Completion criteria

- [ ] Plan 238 diagnostic runs before the normal benchmark corpus and from a
      quiescent DB-task window;
- [ ] foreground publication/finalization transaction map is recorded;
- [ ] three fresh-root MMC database runs complete on a physical Linux/aarch64
      SBC;
- [ ] direct-provider control stays stable or the run is marked noisy;
- [ ] WAL header/size correlation is recorded without mutating checkpoint
      state;
- [ ] three database-only tmpfs comparison runs complete when tmpfs is
      available;
- [ ] config/log/runtime files remain on MMC during database-only tmpfs runs;
- [ ] background task tick changes are detected and contaminated runs are not
      used as primary evidence;
- [ ] pre-provider and post-provider tails are classified separately;
- [ ] all accepted runs converge durable/runtime ownership;
- [ ] one G1–G5 classification is recorded;
- [ ] no production runtime/API/config/dependency behavior changes;
- [ ] a production follow-up plan is written only if a specific storage or
      checkpoint owner is localized;
- [ ] otherwise the Plans 230–238 performance line is explicitly closed.

---

## Handoff sequence

1. Preserve Plans 235–237 and their evidence unchanged.
2. Add the Plan 238 diagnostic option and quiescent-task gate to
   `scripts/qualification_sbc.py`.
3. Add bounded WAL-header/file-size observation.
4. Add database-only tmpfs placement support.
5. Add focused tooling tests.
6. Build/SHA-verify the exact candidate on the physical Pi-class target.
7. Run three fresh-root Class A MMC passes.
8. If the tail reproduces, run three Class B database-only tmpfs passes.
9. Run Class C only if A/B remain ambiguous.
10. Aggregate the evidence and classify using G1–G5.
11. Commit the sanitized Plan 238 artifact and append closure evidence here.
12. Stop. Do not implement the production optimization in this plan.
