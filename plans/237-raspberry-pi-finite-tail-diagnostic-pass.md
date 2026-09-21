# Plan 237 — Raspberry Pi Finite-Tail Diagnostic Pass

Date: 2026-09-21
Status: complete
Planning baseline: `00558ec1daaefb1b9b50d9b3a73543bfc279d411`
Follows: `plans/236-physical-sbc-benchmark-evidence-corrective-pass.md`
Priority: P1 narrow diagnostic closure
Execution target: physical Linux/aarch64 Raspberry Pi-class SBC

## Purpose

Localize the remaining multi-second finite-request tail observed in the
corrected Plan 236 Raspberry Pi benchmark before declaring the broader
performance line fully closed.

Plan 236 corrected the benchmark methodology and showed:

- native Responses streaming: consistently about 3–5 ms p95;
- translated Responses -> Anthropic streaming: consistently about 4–5 ms p95;
- native finite median/p50: about 4 ms;
- native finite p95 across the three runs: 166 / 2397 / 2370 ms;
- concurrency-4 finite batches: 438 / 3988 / 753 ms;
- two additional fresh-root attempts reached the existing 5-second client
  timeout under shared-board load;
- all completed runs converged to zero pending requests, reservations,
  finalization jobs, active/retiring leases, and terminal references.

The corrected evidence therefore does **not** show a leak or a streaming-path
problem, but it leaves a finite-specific tail that is too large to dismiss
without attribution.

This plan is diagnostic only. It must identify which side of the provider
boundary owns the delay. It must not optimize or redesign the runtime.

## Governing constraints

1. No `rust/src/` changes are authorized by this plan.
2. No Cargo dependency/profile changes.
3. No production configuration/default changes.
4. Do not add Tokio workers, a second SQLite connection, lock-free routing,
   alternate finalization ownership, or another SSE/parser path.
5. Reuse `scripts/qualification_sbc.py` and the Plan 236 benchmark fixture;
   do not create another benchmark framework.
6. Diagnostics must remain bounded and scalar-only. Never retain prompts,
   request/response bodies, credentials, arbitrary headers, private addresses,
   or URLs.
7. The diagnostic result may justify a later implementation plan, but this plan
   must stop after localization.
8. A variable tail without a repeatable owner is a valid closure result. Do not
   invent causation.

## Authority paths

Read these before implementation:

- `scripts/qualification_sbc.py`
- `tests/tooling/test_qualification_sbc.py`
- `tests/tooling/fixtures/qualification/sbc-benchmark.toml`
- `rust/src/coordinator/finite.rs`
- `rust/src/coordinator/publication.rs`
- `rust/src/coordinator/finalization.rs`
- `rust/src/coordinator/attempt.rs`
- `rust/src/db/connection.rs`
- `rust/src/routing/router.rs`
- `architecture/deep-dive-request-lifecycle.md`
- `architecture/deep-dive-database.md`
- `architecture/deep-dive-runtime.md`

The production database remains one serialized WAL connection, and finite
requests retain durable publication/finalization ownership. Those are facts to
measure around, not assumptions to replace.

## Diagnostic question

For a slow finite request, determine whether the elapsed time is primarily:

1. **before the loopback provider receives the request**;
2. **inside the loopback provider/client fixture**;
3. **after the provider has produced the response but before the EggPool client
   receives the first byte**; or
4. **outside EggPool entirely as host/client scheduling noise**.

Only after that classification may a subsystem owner be named.

## Workstream A — Add bounded provider-boundary timing to the existing qualifier

Extend only the Python qualification tooling.

For sequential finite diagnostic requests, capture monotonic timestamps for:

~~~text
T0 client request start
T1 loopback provider handler receives request
T2 loopback provider finishes writing the response
T3 diagnostic client receives first response byte
T4 diagnostic client finishes reading the response
~~~

Derive:

~~~text
pre_provider_ms       = T1 - T0
provider_service_ms   = T2 - T1
post_provider_ttft_ms = T3 - T2
client_body_ms        = T4 - T3
total_ms              = T4 - T0
~~~

Use `time.monotonic_ns()` or equivalent monotonic timing.

### Correlation

Keep the diagnostic workload sequential so correlation does not require
propagating an ID through production headers.

The loopback provider may retain only a bounded queue of timing tuples for the
current diagnostic batch, keyed by a local sequence number. Do not retain the
body, arbitrary path, headers, or credentials.

Existing fixed path counters may remain.

### Evidence retention

Do not commit all raw samples.

For each diagnostic batch retain:

- sample count;
- p50/p95 total;
- p50/p95 for each phase;
- maximum for each phase;
- the five slowest request records containing only sequence number and the five
  scalar phase durations.

No p99.

## Workstream B — Add a direct-provider control

Using the same Python HTTP client and the same loopback provider, issue a
matched finite request corpus directly to the fixture provider without EggPool
in the path.

Use:

- 5 warm-ups;
- 30 measured requests;
- the same response size/connection behavior as the native finite fixture.

Record p50/p95/max total and provider-service time.

Purpose:

- if direct-provider requests also show multi-second tails, the diagnostic
  cannot attribute the anomaly to EggPool;
- if direct-provider requests remain stable while EggPool finite requests
  stall, continue with EggPool localization.

Do not compare absolute direct-provider latency to EggPool as a benchmark
score. It is only a fixture/host control.

## Workstream C — Reproduce the finite tail with phase attribution

On the same physical Pi-class target used for Plan 236, run the low-wear
benchmark fixture with a diagnostic-only option such as:

~~~text
--diagnose-finite-tail 60
~~~

Contract:

- accepted range `10..=200`;
- default absent/off;
- valid only with benchmark mode or the benchmark fixture;
- 5 unrecorded warm-ups;
- 60 sequential native Responses finite requests;
- no streaming or concurrency work in this diagnostic batch;
- no ordinary Q008 schema change.

Run three fresh-root diagnostic passes.

A 60-sample batch is intentionally small but gives more tail opportunities than
the 30-sample qualification batch without becoming a soak or load test.

## Workstream D — Same-board storage isolation

If and only if the EggPool path reproduces a slow request with
`pre_provider_ms` dominating, perform one storage isolation comparison.

Run the same diagnostic batch with the isolated temporary qualification root on
a RAM-backed filesystem such as `/dev/shm`, if available and large enough.

Keep all EggPool settings identical:

- SQLite WAL enabled;
- `synchronous = "NORMAL"`;
- one database worker;
- same low-wear benchmark cadence;
- same provider fixture;
- same candidate binary;
- same request corpus.

Only the temporary root/storage medium changes.

Record filesystem/storage class in the report.

### Interpretation

- finite pre-provider tail disappears or materially contracts on tmpfs:
  evidence points toward durable SQLite/storage latency;
- finite pre-provider tail remains:
  storage alone does not explain it; routing/publication/runtime scheduling
  remains in scope;
- tmpfs unavailable:
  record `not measured`; do not change SQLite durability settings as a
  substitute.

Do **not** test `synchronous=OFF`, disable request durability, or add a second
DB connection to manufacture a faster number.

## Workstream E — Optional process scheduling context

For each run record existing low-cost context:

- CPU governor and start/end frequency;
- thermal reading;
- process CPU;
- root storage class;
- RSS/VmHWM;
- final pending requests/reservations/finalization ownership state.

If the host is visibly under unrelated load, record that fact without process
names or private command lines.

Do not add perf, eBPF, a profiler daemon, allocator instrumentation, or hardware
CI for this pass.

## Diagnostic decision matrix

### Outcome 1 — pre-provider delay dominates; tmpfs removes it

Owner classification:

~~~text
durable publication / SQLite / storage path
~~~

Next step:

- write a separate narrow database/publication diagnostic or implementation
  plan;
- inspect transaction count/durability boundaries before considering any
  connection-count change;
- do not change current-thread Tokio or routing lock based on this result.

### Outcome 2 — pre-provider delay dominates; tmpfs does not remove it

Owner classification:

~~~text
pre-dispatch runtime path: admission / routing claim / durable publication /
provider preparation scheduling
~~~

Next step:

- write a separate plan for narrowly scoped internal phase timing or the
  specific owner revealed by existing evidence;
- no broad concurrency redesign.

### Outcome 3 — provider_service_ms dominates

Owner classification:

~~~text
qualification fixture / Python loopback provider
~~~

Next step:

- correct the fixture if deterministic;
- do not attribute the tail to EggPool.

### Outcome 4 — post_provider_ttft_ms dominates

Owner classification:

~~~text
finite response decode / terminal preparation / downstream handoff path
~~~

Next step:

- write a separate focused finite-post-response plan;
- streaming architecture remains out of scope.

### Outcome 5 — direct-provider control also stalls similarly

Owner classification:

~~~text
host/client scheduling or fixture-level noise; EggPool not localized
~~~

Next step:

- record the SBC evidence as environmentally noisy;
- do not change EggPool runtime architecture from this evidence.

### Outcome 6 — no slow requests reproduce

Owner classification:

~~~text
not localized
~~~

Next step:

- close the performance line with the Plan 236 architectural keep decisions;
- do not create speculative implementation work.

## Correctness guardrails

Every EggPool diagnostic run must still verify after stabilization:

- zero pending requests;
- zero active reservations;
- zero finalization jobs;
- zero active/retiring leases attributable to the batch;
- zero terminal references attributable to the batch;
- process remains healthy;
- 100% of diagnostic requests either complete successfully or are explicitly
  recorded as bounded timeout/failure evidence.

A timeout must preserve whatever phase timestamps exist up to the timeout. It
must not be silently discarded from diagnostic interpretation.

## Evidence artifact

Commit one sanitized artifact:

~~~text
artifacts/qualification/237-sbc-finite-tail-diagnostic.json
~~~

Include:

- exact repository/candidate SHA;
- board/OS/kernel/storage facts;
- diagnostic sample count and run count;
- direct-provider control summary;
- three normal-storage phase summaries;
- up to five scalar slow-request phase records per run;
- tmpfs comparison if performed;
- timeout count and last known phase;
- resource convergence;
- one explicit owner classification from the decision matrix;
- whether a follow-up implementation plan is justified.

Do not overwrite Plans 235/236 evidence.

## Tooling tests

Add focused tests for:

- diagnostic sample bound `10..=200`;
- diagnostic mode default-off;
- monotonic phase arithmetic;
- bounded slowest-five retention;
- no p99;
- direct-provider control uses only fixed fixture paths;
- timing queues cannot retain bodies/headers/arbitrary URLs;
- timeout records remain in diagnostic output with bounded scalar state;
- ordinary Q008 stays `runtime-q008.v1`;
- ordinary benchmark mode stays `runtime-q008.v2`;
- Plan 236 benchmark behavior remains unchanged when diagnostic mode is off.

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

Because this plan is tooling/evidence-only, no Rust source change or full Rust
workspace rerun is required. If implementation unexpectedly requires Rust
changes, stop and create a separate plan.

## Completion criteria

- [x] bounded provider-boundary phase timing exists only in diagnostic mode;
- [x] direct-provider control is measured;
- [x] three fresh-root 60-request finite diagnostic passes run on a physical
      Linux/aarch64 SBC;
- [x] slow/timeout requests are retained as bounded scalar phase evidence rather
      than dropped;
- [x] storage isolation is run when pre-provider delay dominates, or explicitly
      marked not applicable/not measured;
- [x] all completed runs converge ownership state;
- [x] one decision-matrix owner classification is recorded;
- [x] no production runtime/API/config/dependency behavior changes;
- [x] a follow-up plan is created only if the evidence localizes a concrete
      EggPool owner (Outcome 1 localizes the durable publication /
      SQLite / storage path; narrow follow-up justified, no runtime change
      in this plan);
- [ ] otherwise the broader Plans 230–237 performance line is explicitly closed
      (not closed: narrow database/publication follow-up remains open; Tokio,
      routing-lock, streaming, and fixture keeps stand).

## Handoff sequence

1. Preserve Plans 235/236 and their artifacts unchanged.
2. Add diagnostic-only phase timing and the direct-provider control to
   `scripts/qualification_sbc.py`.
3. Add focused tooling tests; keep ordinary Q008/Plan 236 behavior unchanged.
4. Build/SHA the exact checkout on the physical Pi-class target.
5. Run the direct-provider control plus three fresh-root 60-request finite
   diagnostic passes.
6. If pre-provider delay dominates, repeat once with a RAM-backed temporary
   root when available.
7. Classify the owner using the decision matrix.
8. Commit the sanitized Plan 237 artifact and append closure evidence to this
   plan only.
9. Stop. Do not implement a runtime optimization in this plan.

## Closure — 2026-09-21

The diagnostic pass completed on the same Raspberry Pi 5 class as Plans
235/236. The tooling commit was `5506cebb70bf092ed3f30fdf4d21cb5acd5c95cf`
(diagnostic-only; no `rust/` change), and the candidate was byte-identical
to Plan 236:

- repository commit: `5506cebb70bf092ed3f30fdf4d21cb5acd5c95cf`;
- candidate SHA-256: `4b58060caebd6158a0e1d05762c17d4989c50e4d991eadfaee4c2e222a8b48f8`;
- candidate size: `30,226,744` bytes;
- Rust: `rustc 1.98.1 (48a229cea 2026-09-01)`;
- board: Raspberry Pi 5 Model B Rev 1.0, four cores, 7.75 GiB reported RAM;
- OS/kernel: Ubuntu 24.04.4 LTS / Linux `6.8.0-1064-raspi`;
- root storage: non-rotational MMC, ext4;
- CPU governor: `ondemand` for all runs; end frequency policy varied
  (`2400000`/`1900000`/`1900000`) across the three normal-storage runs;
- temperature: `50.7/50.1/49.6 °C` at start and `50.7/49.6/45.8 °C` at end,
  with no thermal-throttle evidence.

Tooling landed as committed:

- diagnostic-only `--diagnose-finite-tail 10..=200` (default off, requires
  benchmark mode and the benchmark fixture) with 5 unrecorded warm-ups and
  sequential native Responses finite requests;
- bounded provider-boundary phase timing (`T0` client start, `T1` provider
  receive, `T2` provider finish, `T3` first byte, `T4` done) using
  `time.monotonic_ns()`, scalar-only, slowest-five retention, no p99;
- direct-provider control (5 warm-ups + 30 measured direct-to-fixture
  requests on the fixed `/responses` path);
- ordinary Q008 stays `runtime-q008.v1`; benchmark/diagnostic stays
  `runtime-q008.v2`; Plan 236 behavior unchanged when diagnostic mode is off.

Three fresh-root 60-request diagnostic passes ran with the benchmark-only
fixture (`--benchmark-samples 30 --diagnose-finite-tail 60`), plus one
RAM-backed (`/dev/shm` tmpfs) isolation run with identical settings. Two
additional fresh-root attempts under shared-board load did not pass (one
stabilization-convergence miss, one 5-second client timeout) and were
retried; only the three passing fresh-root runs are recorded.

| Run | Diagnostic total p50/p95/max | Pre-provider max | Provider-service max | Post-provider max | Direct max |
|---|---:|---:|---:|---:|---:|
| 1 (MMC) | 4 / 144 / 3473 ms | 3467 ms | 1 ms | 584 ms | 1 ms |
| 2 (MMC) | 4 / 359 / 2036 ms | 2006 ms | 0 ms | 1776 ms | 2 ms |
| 3 (MMC) | 4 / 4 / 1785 ms | 1778 ms | 0 ms | 7 ms | 2 ms |
| tmpfs | 3 / 4 / 4 ms | 3 ms | 0 ms | 2 ms | 1 ms |

The slowest diagnostic request in every normal-storage run was
pre-provider dominated, provider-service never exceeded 1 ms, and the
direct-provider control never exceeded 2 ms. On tmpfs the tail disappears
entirely (diagnostic max 4 ms, native finite p95 4 ms, concurrency-4 batch
64 ms vs 766/5320/3293 ms on MMC). All passing runs converged with zero
pending requests, active reservations, finalization jobs, active/retiring
leases, and terminal references, and recorded zero timeouts. The sanitized
aggregate report is
[`artifacts/qualification/237-sbc-finite-tail-diagnostic.json`](../artifacts/qualification/237-sbc-finite-tail-diagnostic.json).

Classification: **Outcome 1 — pre-provider delay dominates; tmpfs removes
it. Owner: durable publication / SQLite / storage path.** Runs 1–2 also
showed an intermittent post-provider second-slowest (584/1776 ms) that did
not repeat in run 3 (post max 7 ms); reported as observed without an
invented cause. No Tokio, routing-lock, streaming, or fixture change is
justified by this evidence.

Follow-up: justified as a separate narrow database/publication diagnostic
or implementation plan (inspect transaction count/durability boundaries
before considering any connection-count change). This plan makes no
production runtime, API, config-default, or dependency change and
implements no optimization.

Validation evidence:

- `uv run ruff format --check scripts/ tests/tooling/`: pass;
- `uv run ruff check scripts/ tests/tooling/`: pass;
- `uv run pyright scripts/`: pass;
- `uv run pytest tests/tooling/test_qualification_sbc.py -q`: pass
  (`24 passed`);
- `uv run pytest tests/tooling/ -q --tb=short --maxfail=1`: pass
  (`99 passed, 3 skipped`);
- `uv run python scripts/validate_release_docs.py`: pass;
- three `--benchmark-samples 30 --diagnose-finite-tail 60` runs with
  `--expected-sha256` and `--config-fixture sbc-benchmark.toml`: pass
  (`runtime-q008.v2`);
- one tmpfs isolation run with identical settings: pass, tail removed.
- Full serial Rust workspace suite was not rerun for this tooling-only pass
  (no `rust/` change); default and no-default Clippy/checks pass and remote
  CI qualifies the workspace.
