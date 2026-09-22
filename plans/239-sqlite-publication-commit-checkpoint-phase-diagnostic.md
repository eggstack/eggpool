# Plan 239 — SQLite Publication Commit/Checkpoint Phase Diagnostic

Date: 2026-09-21
Status: implementation handoff
Planning baseline: `d3609f75cd036bec910e3dca5a3551086c5c8af5`
Follows: `plans/238-narrow-publication-storage-diagnostic.md`
Priority: P1 final performance-localization pass
Execution target: physical Linux/aarch64 Raspberry Pi-class SBC

## Purpose

Resolve the last unanswered performance question from Plans 235–238 before any
production SQLite policy change is proposed.

Plan 238 established on a physical Raspberry Pi 5 that:

- normal MMC-backed native finite requests are usually single-digit
  milliseconds;
- each accepted 60-request MMC run observed three WAL checkpoint-sequence
  changes;
- the multi-second requests were the checkpoint-sequence-changing requests;
- those slow requests were overwhelmingly pre-provider;
- requests without a checkpoint-sequence change remained at 7–20 ms maximum;
- moving **only** SQLite DB/WAL/SHM to tmpfs retained the same checkpoint
  sequence changes but reduced all three runs to 3 ms p50 / 3 ms p95 / 4 ms
  maximum;
- direct-provider control stayed at 1–8 ms;
- no supervised database task ticked during the measured windows.

That localizes the tail to SQLite/WAL storage behavior during checkpoint/reset
cycles, but external timing still cannot distinguish:

1. waiting for EggPool's serialized database gate;
2. queueing on the tokio-rusqlite worker;
3. `BEGIN IMMEDIATE`;
4. the publication transaction body;
5. `COMMIT` and any SQLite automatic checkpoint work performed from that
   commit.

Plan 239 adds **qualification-only**, bounded transaction-phase observation and
a controlled `wal_autocheckpoint` experiment. It must stop after identifying
the owner and the safest production follow-up shape.

This is not the production optimization plan.

---

## Governing constraints

1. Preserve the public `Database::with_transaction` API and semantics.
2. Preserve the single SQLite connection and single serialized gate.
3. Preserve WAL and `synchronous = "NORMAL"`.
4. Do not weaken request publication/finalization durability.
5. Do not change normal config defaults or add a production
   `wal_autocheckpoint` setting in this plan.
6. Do not add Tokio workers, a second DB connection, batching, lock-free
   routing, or early finite-response delivery.
7. Qualification diagnostics must be compiled behind one **non-default** Cargo
   feature named `qualification-db-diagnostics`.
8. The ordinary release/default build must not expose the diagnostic records,
   consult diagnostic environment variables, or change runtime JSON shape.
9. The diagnostic feature must add no dependency.
10. Diagnostic records remain bounded, scalar-only, in memory, and contain no
    request IDs, account/provider/model names, SQL text, paths, bodies,
    credentials, headers, or URLs.
11. A diagnostic `wal_autocheckpoint` override is allowed only in the
    qualification-feature build, only at process startup, and only on the
    existing connection.
12. No runtime policy selected by this plan may silently become production
    behavior.
13. If the result is not repeatable on the physical target, close without a
    speculative production change.

---

## Authority paths

Read these before implementation:

- `rust/Cargo.toml`
- `rust/src/db/connection.rs`
- `rust/src/coordinator/publication.rs`
- `rust/src/coordinator/finalization.rs`
- `rust/src/coordinator/finite.rs`
- `rust/src/server/inference.rs`
- `rust/src/server/health.rs`
- `rust/src/runtime_lifecycle/process.rs`
- `rust/src/runtime_lifecycle/diagnostics.rs`
- `rust/src/task_supervisor.rs`
- `rust/tests/coordinator_publication.rs`
- `rust/tests/coordinator_finalization.rs`
- `rust/tests/database_compatibility.rs`
- `scripts/qualification_sbc.py`
- `tests/tooling/test_qualification_sbc.py`
- `tests/tooling/fixtures/qualification/sbc-benchmark.toml`
- `architecture/deep-dive-database.md`
- `architecture/deep-dive-request-lifecycle.md`
- `artifacts/qualification/238-sbc-publication-storage-diagnostic.json`

---

## Structural baseline

### Successful single-attempt finite request

The foreground durable shape remains:

~~~text
pre-provider:
  routing claim
  -> publication transaction
       -> acquire DB gate
       -> BEGIN IMMEDIATE
       -> request insert
       -> reservation insert
       -> request_attempt insert
       -> routing_decision insert
       -> COMMIT
  -> local claim conversion
  -> provider preparation
  -> provider submit

post-provider:
  response decode
  -> buffered usage metric enqueue
  -> durable finalization transaction
       -> acquire DB gate
       -> BEGIN IMMEDIATE
       -> request terminal update
       -> request_attempt terminal update
       -> reservation release
       -> COMMIT
  -> runtime claim/quota release
  -> finite HTTP response
~~~

For Plan 239's sequential success corpus there is exactly one foreground
publication transaction and one foreground finalization transaction per
request. Retries are out of scope.

### Current database implementation

`Database::with_transaction` owns:

~~~text
acquire single semaphore
-> submit closure to tokio-rusqlite worker
-> BEGIN IMMEDIATE
-> caller transaction body
-> COMMIT or ROLLBACK
-> worker returns
-> release semaphore
~~~

The current configuration sets WAL, `synchronous`, and
`journal_size_limit` but does not explicitly set `wal_autocheckpoint`.
Plan 239 must query and report the **effective** value from the bundled SQLite
connection rather than assuming the common default.

---

## Workstream A — Add a non-default qualification diagnostics feature

Add to `rust/Cargo.toml`:

~~~toml
qualification-db-diagnostics = []
~~~

Requirements:

- it is not in `default`;
- it pulls in no dependency;
- it is never enabled by release/package workflows;
- ordinary `cargo build --release` remains behaviorally unchanged;
- ordinary `/api/stats/runtime` retains its existing JSON shape;
- the feature is explicitly documented as repository qualification tooling,
  not an operator capability.

Build used for Plan 239 evidence:

~~~bash
cargo build --manifest-path rust/Cargo.toml --locked --release \
  --features qualification-db-diagnostics
~~~

Also build the ordinary release candidate during validation to prove the
feature is not accidentally selected.

---

## Workstream B — Add a bounded in-memory DB diagnostic collector

Under `#[cfg(feature = "qualification-db-diagnostics")]`, add one
process-owned collector associated with the existing `DatabaseInner`.

The collector must:

- use a fixed capacity of at most 256 records;
- keep a monotonically increasing scalar sequence number;
- evict oldest records when full;
- use no async queue, task, filesystem, or SQLite table;
- never block a database transaction on diagnostic delivery;
- retain only successful/failed phase timing scalars and a fixed transaction
  kind.

Allowed transaction kinds:

~~~text
publication
finalization
other
~~~

Only publication/finalization records are required for Plan 239 evidence.
Do not attach request identity.

### Required record shape

Use microseconds so ordinary fast phases are not rounded to zero:

~~~text
record_seq
kind
gate_wait_us
worker_queue_us
begin_us
body_us
commit_us
worker_return_us
total_us
success
~~~

Definitions:

- `gate_wait_us`: immediately before semaphore acquisition -> permit acquired;
- `worker_queue_us`: immediately before `AsyncConnection::call` -> closure
  begins executing on the SQLite worker;
- `begin_us`: duration of `BEGIN IMMEDIATE`;
- `body_us`: duration of the caller-supplied transaction closure;
- `commit_us`: duration of `COMMIT` on success;
- `worker_return_us`: worker closure completion -> caller receives the
  `AsyncConnection::call` result;
- `total_us`: before gate acquisition -> result ready and permit release
  boundary.

On rollback/error paths, preserve bounded timing for the phases that occurred;
do not fabricate `commit_us`.

Use `Instant`, never wall-clock timestamps.

### Existing API preservation

Do not change the signature or behavior of public
`Database::with_transaction`.

Preferred internal shape:

- retain `with_transaction` as the public compatibility entry point;
- factor the implementation through one private/internal transaction helper;
- add a crate-private named transaction entry point used by publication and
  finalization;
- compile diagnostic collection out when the feature is disabled.

Do not duplicate transaction/commit/rollback logic in a second implementation.

---

## Workstream C — Label only the two request-owned durable boundaries

Update only the internal publication/finalization call sites needed to identify
foreground request ownership:

- `PublicationService::publish_transaction` ->
  `publication`;
- `DurableFinalizer::finalize_durable` ->
  `finalization`.

Compensation/recovery/maintenance transactions may remain `other` or use the
ordinary public path.

The label is a fixed enum/value, not an arbitrary string.

Default builds must preserve the same SQL, transaction grouping, error
behavior, claim ownership, and call ordering.

---

## Workstream D — Report effective SQLite pragma facts from the same connection

When the qualification feature is enabled, capture the effective scalar
connection facts after `Database::configure`:

~~~text
journal_mode
synchronous
page_size
wal_autocheckpoint_pages
~~~

Rules:

- query through the existing SQLite connection;
- do not open a second connection;
- retain only these fixed scalar/enum values;
- do not retain the database path;
- ordinary builds do not add this work.

The Plan 239 artifact must report the observed effective
`wal_autocheckpoint_pages`; do not hard-code `1000` into evidence.

---

## Workstream E — Add a qualification-only startup override

Under the same non-default feature only, recognize:

~~~text
EGGPOOL_QUALIFICATION_WAL_AUTOCHECKPOINT_PAGES
~~~

Contract:

- absent: do not issue a `wal_autocheckpoint` pragma update;
- integer range: `0..=100000`;
- invalid value: startup fails clearly;
- applied once on the existing connection during database configuration;
- effective value is queried back and reported;
- the name/value is not part of `Config`, reload policy, CLI help, example
  config, or production docs;
- an ordinary build does not consult this environment variable.

Value `0` disables SQLite's automatic checkpoint trigger for the bounded
diagnostic process only. It does **not** change WAL mode or
`synchronous = "NORMAL"`.

The temporary-root process and database are destroyed after each run, so
unbounded long-lived WAL growth is not permitted or relevant to this
experiment.

---

## Workstream F — Expose diagnostics through the existing authenticated runtime projection

Do not create a new public route.

With `qualification-db-diagnostics` enabled only, extend the existing
authenticated `/api/stats/runtime` projection with a bounded field such as:

~~~json
{
  "database_qualification": {
    "effective": {
      "journal_mode": "wal",
      "synchronous": "...",
      "page_size": 4096,
      "wal_autocheckpoint_pages": 1000
    },
    "latest_record_seq": 123,
    "records": []
  }
}
~~~

Requirements:

- field absent entirely in ordinary/default builds;
- at most 256 records;
- no path, SQL, request identity, or arbitrary text;
- records sorted by monotonically increasing sequence;
- endpoint remains read-only;
- no reset/mutation endpoint;
- no new authentication exemption.

The Python harness captures `latest_record_seq` after warm-up/quiescence, runs
the measured batch, then retains only records with a larger sequence number.
This avoids adding a mutable reset API.

---

## Workstream G — Correlate request phases to transaction phases

Extend the Plan 238 diagnostic path in `scripts/qualification_sbc.py`.

For a sequential 60-request success batch:

1. perform existing warm-ups;
2. wait for the fixed database-task quiescence condition;
3. read the diagnostic baseline sequence;
4. execute 60 measured native Responses finite requests using the existing
   T0–T4 provider-boundary timing and WAL-header snapshots;
5. read the diagnostic records after the batch;
6. require exactly:
   - 60 publication records;
   - 60 finalization records;
   - no missing/duplicate foreground records.

Because requests are sequential and each HTTP response awaits finalization,
pair publication/finalization records by their observed order for this bounded
single-attempt corpus.

Reject the run as contaminated if:

- the expected 60/60 shape is not present;
- a supervised DB task tick changes;
- the provider control is noisy;
- any request retries/fails/times out;
- ownership does not converge.

### Correlated evidence

For the five slowest requests retain:

~~~text
request_sequence
pre_provider_ms
provider_service_ms
post_provider_ttft_ms
total_ms
wal_checkpoint_sequence_changed

publication:
  gate_wait_us
  worker_queue_us
  begin_us
  body_us
  commit_us
  worker_return_us
  total_us

finalization:
  same phase fields
~~~

Also compute:

~~~text
pre_provider_unattributed_us =
  max(pre_provider_us - publication_total_us, 0)

post_provider_unattributed_us =
  max(post_provider_ttft_us - finalization_total_us, 0)
~~~

This determines whether the DB transaction actually explains the external
request-phase tail.

Do not retain all per-request rows in the committed aggregate.

---

## Workstream H — Physical experiment matrix

Use a physical Pi 5-class Linux/aarch64 target and the same low-wear benchmark
fixture.

All runs keep:

- MMC/ext4 database placement;
- WAL enabled;
- `synchronous = "NORMAL"`;
- one database connection/gate;
- one DB worker;
- identical request corpus;
- background-task quiescence;
- same diagnostic-feature binary SHA within the matrix.

### H0 — Effective-default baseline

No qualification override.

Run three fresh roots, 60 measured requests each.

Required observations:

- effective `wal_autocheckpoint_pages`;
- publication/finalization phase summaries;
- WAL checkpoint-sequence correlation;
- three accepted runs.

### H1 — Automatic checkpoint disabled

Set:

~~~text
EGGPOOL_QUALIFICATION_WAL_AUTOCHECKPOINT_PAGES=0
~~~

Run three fresh roots, 60 measured requests each.

Purpose:

- determine whether the multi-second publication `COMMIT` tail disappears
  when automatic checkpointing is disabled;
- observe bounded WAL growth only;
- do not interpret this as a production recommendation.

Do not run an explicit checkpoint inside the measured request batch.

### H2 — Optional lower-threshold automatic checkpoint

Run only if H0 shows commit-dominated checkpoint-change stalls and H1 removes
them.

Set:

~~~text
EGGPOOL_QUALIFICATION_WAL_AUTOCHECKPOINT_PAGES=256
~~~

Run three fresh roots, 60 measured requests each.

Purpose:

- characterize whether smaller/more frequent automatic checkpoints trade the
  multi-second tail for lower bounded pauses;
- provide design evidence for the later production plan.

`256` is a diagnostic comparison point, not a proposed production default.

If the effective default is already <=256, skip H2 and record why.

### No other threshold sweep

Do not turn Plan 239 into parameter tuning. No 64/128/512/2048 grid.

---

## Workstream I — Decision matrix

### I1 — Publication commit dominates; H1 removes tail

Evidence pattern:

~~~text
slow request:
  publication.commit_us ~= pre_provider tail
  gate/body remain small
  WAL checkpoint sequence changes

H1:
  no checkpoint-sequence changes
  publication commit tail disappears
~~~

Classification:

~~~text
foreground SQLite automatic checkpoint work is the localized owner
~~~

Next action:

- write a separate production implementation plan;
- preferred design question is how to move bounded passive checkpoint work
  away from request commits while preserving WAL/NORMAL durability and bounded
  WAL growth;
- retain one DB connection unless new evidence requires otherwise.

### I2 — Gate wait dominates

Classification:

~~~text
serialized DB-gate contention owns the tail
~~~

Before proposing another connection:

- identify the competing transaction kind;
- prove it exists in a quiescent accepted run;
- write a separate plan scoped to that owner.

A second connection is not automatically justified.

### I3 — Transaction body dominates

Classification:

~~~text
publication row-write/body work owns the tail
~~~

Next action:

- inspect row/index/write amplification in a separate plan;
- do not change checkpoint policy merely because a sequence changed.

### I4 — BEGIN dominates

Classification:

~~~text
write-lock acquisition / SQLite transaction start owns the tail
~~~

Next action:

- determine the competing lock owner before any connection/concurrency change.

### I5 — COMMIT dominates but H1 does not remove the tail

Classification:

~~~text
durable commit/storage sync cost is localized, but automatic checkpoint is not
the sufficient cause
~~~

Next action:

- retain WAL/NORMAL;
- write a narrower storage/durability plan;
- do not disable durability or add a second writer.

### I6 — DB phases do not explain external pre-provider tail

Classification:

~~~text
Plan 238 correlation was real but the remaining delay is outside the measured
SQLite transaction
~~~

Next action:

- inspect claim conversion/provider preparation/runtime scheduling;
- no SQLite production change.

### I7 — Instrumented baseline cannot reproduce

Classification:

~~~text
not reproducible under the diagnostic build
~~~

Next action:

- run the ordinary Plan 238 baseline once more;
- if ordinary reproduces but diagnostic does not, treat instrumentation as
  perturbing the phenomenon and do not choose a production fix from Plan 239.

---

## Workstream J — Instrumentation overhead guard

The diagnostic build must not materially distort the normal fast path.

For H0 and one DB-only tmpfs control run:

- native finite p50/p95 should remain in the same single-digit class as Plan
  238;
- diagnostic collector capacity remains bounded;
- CPU/RSS are recorded descriptively;
- no per-request log writes are allowed.

If the qualification feature itself raises tmpfs p95 above 20 ms or otherwise
changes request behavior materially, classify the instrumentation as intrusive
and stop rather than trusting its phase attribution.

One tmpfs control run is sufficient for this guard; Plan 239 does not need
another three-run tmpfs campaign.

---

## Evidence artifact

Commit one sanitized aggregate:

~~~text
artifacts/qualification/239-sbc-db-phase-checkpoint-diagnostic.json
~~~

Preserve Plans 235–238 artifacts unchanged.

Required aggregate contents:

- repository SHA;
- diagnostic-feature candidate SHA-256 and binary size;
- ordinary release candidate SHA-256 and binary size;
- physical board/OS/kernel/storage facts;
- effective SQLite pragma facts for every experiment class;
- collector capacity and record schema version;
- H0 three-run summary;
- H1 three-run summary;
- H2 three-run summary only when executed;
- one tmpfs instrumentation-overhead control;
- publication/finalization p50/p95/max phase timings;
- slowest-five correlated scalar records;
- WAL checkpoint-sequence correlation;
- direct-provider control;
- task tick deltas;
- ownership convergence;
- timeout/failure counts;
- one I1–I7 classification;
- explicit recommendation for whether a **new production implementation plan**
  is justified.

Do not store raw per-request corpus, paths, SQL, request IDs, raw WAL bytes, or
p99.

---

## Rust tests

Add deterministic coverage for the feature-gated collector and unchanged
transaction behavior.

At minimum:

1. ordinary/default build compiles without qualification diagnostics;
2. `qualification-db-diagnostics` build compiles;
3. collector capacity is bounded and oldest records evict;
4. record sequence is monotonic;
5. record contains only fixed scalar fields;
6. successful transaction records gate/worker/begin/body/commit phases;
7. rollback/error record never fabricates a successful commit phase;
8. public `Database::with_transaction` result/error semantics are unchanged;
9. publication uses the fixed `publication` kind;
10. finalization uses the fixed `finalization` kind;
11. effective pragma snapshot comes from the existing connection;
12. invalid qualification override fails startup in feature builds;
13. ordinary builds ignore/do not compile the qualification override path;
14. runtime projection omits `database_qualification` in the ordinary build;
15. feature build projection is bounded and sanitized.

Focused targets:

~~~bash
cargo test --manifest-path rust/Cargo.toml --test database_compatibility -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_publication -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_boundaries -- --test-threads=1
~~~

Run the same relevant targets with:

~~~bash
--features qualification-db-diagnostics
~~~

when the target exercises the new collector/projection.

---

## Tooling tests

Extend `tests/tooling/test_qualification_sbc.py` for:

- Plan 239 mode requires physical SBC and benchmark fixture;
- ordinary Q008 schema remains unchanged;
- Plan 236/237/238 modes remain unchanged when Plan 239 mode is off;
- feature diagnostic records correlate exactly 60 publication + 60
  finalization records;
- missing/duplicate records reject the run;
- only records after baseline sequence are retained;
- phase arithmetic is deterministic;
- slowest-five correlation remains bounded;
- effective `wal_autocheckpoint_pages` is reported rather than assumed;
- H1 override value is scalar and secret-free;
- invalid overrides fail clearly;
- no p99, SQL, path, request identity, body, credential, URL, or arbitrary
  diagnostic text reaches the report.

---

## Validation

Because Plan 239 touches Rust internals and Cargo features, run the full native
validation rather than treating it as tooling-only:

~~~bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1

cargo check --manifest-path rust/Cargo.toml --workspace --all-targets \
  --features qualification-db-diagnostics
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets \
  --features qualification-db-diagnostics -- --test-threads=1

cargo build --manifest-path rust/Cargo.toml --locked --release
cargo build --manifest-path rust/Cargo.toml --locked --release \
  --features qualification-db-diagnostics

uv sync --frozen
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
uv run python scripts/validate_release_docs.py
git diff --check
~~~

Verify the release workflow/package builder never enables
`qualification-db-diagnostics`.

---

## Explicit non-goals

Plan 239 must not:

- ship the qualification feature in release artifacts;
- add a production SQLite tuning config;
- disable production auto-checkpointing;
- change production checkpoint cadence;
- expose a public diagnostic mutation endpoint;
- add another SQLite connection;
- split publication into multiple commits;
- merge publication/finalization into one transaction;
- remove routing-decision persistence;
- weaken `synchronous`;
- move the production DB to tmpfs;
- return finite responses before finalization;
- add a new metrics/logging dependency;
- write per-request diagnostic logs;
- change Tokio runtime flavor;
- alter routing or streaming architecture.

---

## Completion criteria

- [ ] non-default `qualification-db-diagnostics` feature exists with no new
      dependency and is absent from release builds;
- [ ] ordinary `Database::with_transaction` public API/semantics remain
      unchanged;
- [ ] publication/finalization phase records distinguish gate, worker queue,
      BEGIN, body, COMMIT, and return time;
- [ ] records are bounded/sanitized and absent in ordinary builds;
- [ ] effective `wal_autocheckpoint_pages` is queried from the same
      connection;
- [ ] three accepted H0 physical MMC runs complete;
- [ ] three accepted H1 auto-checkpoint-disabled physical MMC runs complete;
- [ ] H2 runs only when its predicate is satisfied;
- [ ] one tmpfs instrumentation-overhead control completes;
- [ ] all accepted runs are quiescent and converge request/reservation/
      finalization ownership;
- [ ] one I1–I7 owner classification is recorded;
- [ ] ordinary release/full CI behavior remains unchanged;
- [ ] a new production implementation plan is created only when Plan 239
      localizes a concrete safe change;
- [ ] otherwise Plans 230–239 are explicitly closed with the existing runtime
      architecture retained.

---

## Handoff sequence

1. Preserve Plans 235–238 and their evidence unchanged.
2. Add the non-default qualification feature and bounded in-memory collector.
3. Refactor `with_transaction` through one implementation and add fixed
   publication/finalization labels without duplicating transaction logic.
4. Add same-connection effective pragma capture and feature-only startup
   override.
5. Extend the existing authenticated runtime projection only in qualification
   builds.
6. Extend the SBC harness and tooling tests for record correlation.
7. Run full ordinary + feature-enabled Rust/tooling validation.
8. Build both ordinary and qualification-feature release binaries on the Pi.
9. Run H0 three times from fresh roots.
10. Run H1 three times from fresh roots.
11. Run H2 only if the decision predicate requires it.
12. Run one DB-only tmpfs instrumentation-overhead control.
13. Aggregate evidence and classify I1–I7.
14. Append closure evidence to Plan 239 and commit the sanitized artifact.
15. Stop. Do not implement the production checkpoint/storage policy in this
    plan.

---

## Closure pass — 2026-09-21

Status: complete.

- Added the non-default, dependency-free `qualification-db-diagnostics`
  feature with a bounded 256-record in-memory collector, named publication and
  finalization transaction phases, same-connection effective pragma capture,
  and the authenticated qualification-only runtime projection.
- Added the sequential 60-request phase corpus, scalar correlation checks,
  feature-only `wal_autocheckpoint` override, database-only tmpfs control, and
  tooling coverage. The diagnostic request timeout is 30 seconds so an
  accepted corpus is not truncated by the expected multi-second MMC tail.
- Built ordinary and feature release binaries on the physical Raspberry Pi 5.
  Ordinary SHA-256: `cc7e614b43cdeb877b9df025a69835d7a71045454f7f7a0c86be29cb6b779cb1`.
  Feature SHA-256: `1fde022cc183d9607a61e067a281b38fd086ac5a9d7cec5b02571a80af9ef6b9`.
- Accepted three H0 runs at the effective 1000-page threshold, three H1 runs
  with the feature-only override `0`, three H2 runs at `256` after the H0/H1
  predicate was satisfied, one database-only tmpfs run, and one ordinary
  release baseline. Every accepted phase run completed 60/60 requests with
  120 foreground records, quiescent fixed database tasks, and converged
  durable ownership.
- H0 publication `COMMIT` maxima were 3.65 s, 12.00 s, and 14.89 s. H1
  publication `COMMIT` maxima were 808, 592, and 687 microseconds with no
  checkpoint-sequence changes. H2 reduced publication tails but introduced
  smaller/frequent pauses, including finalization. The tmpfs control had a
  3 ms p95 and 4 ms maximum total request time.
- The sanitized aggregate is
  `artifacts/qualification/239-sbc-db-phase-checkpoint-diagnostic.json`.
  Classification is I1: foreground SQLite automatic-checkpoint work is the
  localized owner of the MMC tail.
- Created Plan 240 as the separate production design handoff. No production
  SQLite policy was changed in this plan.
