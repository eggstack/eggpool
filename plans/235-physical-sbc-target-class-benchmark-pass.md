# Plan 235 — Physical SBC Target-Class Benchmark Pass

Date: 2026-09-21
Status: complete
Planning baseline: `b4fea290f1c46209e8c275936f87c1826ee1a4d8`
Parent closure: `plans/234-sbc-performance-and-release-footprint-closure.md`
Priority: P2 evidence-only target-class characterization
Execution target: physical Linux/aarch64 Raspberry Pi-class or comparable SBC

## Purpose

Fill the one material evidence gap left by Plan 234: obtain a small, real
target-class performance characterization of the current native EggPool runtime
on a physical Linux/aarch64 SBC.

This is not a new optimization campaign. The expected outcome is a concise
measurement record that either confirms the current simple runtime architecture
or identifies one specific bottleneck worthy of a separate follow-up plan.

Production code changes are not expected or authorized by this plan.

## Why this plan exists

Plans 230–234 closed the residual allocation campaign and retained the
current-thread Tokio runtime, single SQLite gate, routing selection lock, bounded
stream handoff, and current release profile. Plan 234 could not measure
loopback latency percentiles, process CPU, peak RSS, or representative SBC
behavior on its available host.

The repository already contains a physical-SBC-aware qualification runner:

- `scripts/qualification_sbc.py`
- `tests/tooling/test_qualification_sbc.py`
- `tests/tooling/fixtures/qualification/sbc.toml`

Reuse that runner and its physical-board attestation. Do not create a second
benchmark framework.

## Target requirements

The measured host must satisfy the existing `qualification_sbc.py` hardware
gate:

- Linux;
- aarch64/arm64;
- readable device-tree board model.

A Raspberry Pi 4/5 is preferred, but another genuine aarch64 SBC is acceptable
when the report records the exact board model. Hosted ARM VMs, workstation
emulation, Rosetta/translation, and cloud ARM instances must not be reported as
SBC evidence.

Record:

- board model;
- CPU core count/frequency governor when exposed;
- RAM;
- root storage class/filesystem;
- kernel/OS;
- thermal reading when exposed;
- Rust version;
- EggPool commit SHA;
- candidate SHA-256 and binary size.

Use Ethernet where practical, but the benchmark provider itself remains
loopback-only so LAN quality is not part of the timing result.

## Scope

This plan measures four local runtime paths:

1. native Responses finite;
2. native Responses streaming;
3. one cross-surface translated stream supported by the existing fixture;
4. bounded request/routing work under modest concurrency.

Remote-compaction memory behavior may be added only if it can use the existing
fixture with a small tooling-only extension. Do not widen this pass merely to
force compact coverage. If compact cannot be exercised cleanly, record it as
`not measured`; the structural ownership proof from Plan 232 remains valid.

## Workstream A — Qualify the exact candidate first

Use either:

- an on-device `cargo build --manifest-path rust/Cargo.toml --locked --release`;
  or
- the already-qualified Linux aarch64 release candidate copied byte-for-byte to
  the SBC.

Record how the candidate was produced. If copied, verify its SHA-256 before
execution.

Run the existing tooling contract before benchmarking:

~~~bash
uv run pytest tests/tooling/test_qualification_sbc.py -q

uv run python scripts/qualification_sbc.py \
  --binary rust/target/release/eggpool \
  --candidate-origin on-device-release-build \
  --output artifacts/qualification/235-sbc-functional.json
~~~

Use `q005-qualified-aarch64-copy` instead when that is the true candidate
origin.

The ordinary Q008-style qualification must pass before interpreting performance
numbers. If it fails functionally, stop and record the defect; do not benchmark
a broken candidate.

## Workstream B — Add only the missing bounded sampling capability

The current SBC runner already owns:

- physical-board attestation;
- loopback provider;
- isolated temporary root/config/database;
- request timing;
- procfs CPU/RSS/thread/FD sampling;
- runtime-status and durable-state checks.

Extend this runner rather than adding another harness.

Add an optional CLI flag such as:

~~~text
--benchmark-samples N
~~~

with these rules:

- default `0`: current qualification behavior remains byte/semantically
  unchanged;
- accepted range `1..=100`;
- recommended Plan 235 value: `30`;
- benchmark mode runs only after the normal functional workload is healthy;
- no credentials, request bodies, response bodies, addresses, or hostnames enter
  the report;
- retain only aggregate scalar timing/resource results, not one record per
  request.

Also add `VmHWM`/peak RSS to the existing procfs resource snapshot if it is
available. Use the existing `_proc_value` helper; do not introduce a profiler
or allocator dependency.

For each measured batch, calculate from the existing request timer:

- sample count;
- p50 elapsed;
- p95 elapsed;
- minimum;
- maximum;
- p50 TTFT when meaningful;
- p95 TTFT when meaningful.

Do **not** report p99 from a 30-sample batch.

Measure process CPU for the complete batch using the existing proc CPU tick
helper before/after the batch rather than a background profiler.

A small tooling-only change to `scripts/qualification_sbc.py` and its pytest
contract is permitted. No `rust/src/` file should change.

## Workstream C — Fixed benchmark corpus

Use the existing secret-free loopback fixture and models.

### C1. Native Responses finite

Model: `q008-responses`.

After 5 unrecorded warm-up requests, run 30 measured sequential requests.

This primarily characterizes:

- endpoint/admission;
- model/account routing;
- publication/SQLite lifecycle;
- Eggfetch loopback dispatch;
- finite response decoding/finalization.

### C2. Native Responses streaming

Model: `q008-responses`, `stream=true`.

After 5 unrecorded warm-ups, run 30 measured sequential streams and require
`response.completed` terminal evidence every time.

This exercises the Plan 233 native observation/fold path while forwarding the
original SSE bytes.

### C3. Translated streaming

Use one deterministic cross-surface path already representable by
`tests/tooling/fixtures/qualification/sbc.toml`; prefer a Responses client
request routed to the fixture's Anthropic Messages surface if the current
routing/capability contract accepts it.

Run 5 warm-ups plus 30 measured requests.

If the fixture cannot represent a valid cross-surface route without broad
configuration work, record `not measured` and do not weaken routing or
capability checks merely to obtain a number.

### C4. Modest concurrent finite batch

Run 32 native finite Responses requests with client concurrency fixed at 4.

This is only a contention observation for the existing current-thread runtime,
routing selection lock, and SQLite gate. It is not a throughput stress test.

Record:

- total batch elapsed time;
- completed/failed count;
- requests/second as descriptive arithmetic;
- batch CPU;
- RSS/peak RSS before and after;
- final active requests/reservations/finalization jobs.

Do not add concurrency levels 8/16/64 unless a specific anomaly appears. The
point of this plan is to stay small.

## Workstream D — Stabilization/resource snapshots

Use the existing `resource_sample` machinery at these points:

1. after functional warm-up and before benchmark work;
2. after C1;
3. after C2;
4. after C3 when measured;
5. immediately after C4;
6. after a fixed 3-second post-workload stabilization.

Record:

- RSS;
- peak RSS/VmHWM;
- VMS;
- CPU;
- FD count;
- thread count;
- DB and WAL size;
- runtime task count;
- active/retiring leases;
- finalization jobs;
- request/attempt/reservation counts.

The final stabilization sample must show zero pending requests and zero active
reservations. If it does not, treat that as a correctness finding, not merely a
performance number.

## Workstream E — Repeatability

Run the benchmark mode three times from a fresh isolated temporary root.

Do not average away large differences. Record each run's aggregate p50/p95,
batch throughput, CPU, and peak RSS, then give a simple median-of-runs summary.

Also record thermal readings at run start/end when available. If the board
shows obvious thermal throttling or frequency-governor changes during a run,
mark that run `thermally affected` rather than treating it as a clean
comparison.

No long soak is required.

## Interpretation rules

This plan has no universal latency or throughput pass threshold.

The evidence is intended to answer:

- Does the current runtime remain lightweight on actual SBC hardware?
- Is there obvious retained RSS/resource growth after the workload?
- Does modest concurrency expose severe serialization or queueing?
- Does native streaming appear materially cheaper than translated streaming?
- Is one retained architectural boundary clearly dominating local work?

Do not infer that the current-thread runtime, SQLite gate, or routing lock needs
replacement merely because CPU usage is below 100% of all cores.

A follow-up architecture plan is justified only when the measurements show a
repeatable, localized bottleneck with a plausible owner. If no such bottleneck
appears, the correct result is to keep the current architecture.

## Evidence output

Write one sanitized machine-readable report:

~~~text
artifacts/qualification/235-sbc-target-benchmark.json
~~~

and append a concise closure section to this plan with:

- exact commit/candidate SHA;
- board/environment;
- three-run summary table;
- p50/p95 finite/native-stream/translated-stream measurements;
- concurrency-4 batch throughput;
- baseline/final/peak RSS;
- batch CPU;
- final DB/WAL and ownership state;
- thermal caveats;
- dimensions marked `not measured`;
- keep/investigate decisions for current-thread Tokio, SQLite gate, routing
  lock, and streaming handoff.

Do not include raw request/response content or credentials.

## Validation after tooling-only changes

~~~bash
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run pytest tests/tooling/test_qualification_sbc.py -q
git diff --check
~~~

If only the qualification tooling/report/plan changes, the full Rust workspace
suite is not required.

If any `rust/src/`, `rust/Cargo.toml`, or runtime configuration contract is
changed, stop this plan and write a separate corrective implementation plan
instead of folding the runtime change into benchmarking.

## Explicit non-goals

Do not add:

- Criterion or another benchmark crate;
- a benchmark daemon;
- hardware CI;
- a soak/load-test framework;
- live provider benchmarking;
- production telemetry;
- new runtime metrics solely for this pass;
- a second SQLite connection;
- Tokio worker threads;
- lock-free routing;
- another SSE parser;
- dependency/profile changes;
- performance SLAs or CI thresholds.

Do not change defaults to improve benchmark numbers.

## Completion criteria

- [ ] exact candidate is SHA-verified and ordinary physical-SBC qualification passes;
- [ ] benchmark runs on a physically attested Linux/aarch64 SBC;
- [ ] 30-sample native finite and native Responses streaming batches are recorded;
- [ ] translated streaming is measured or explicitly `not measured` with reason;
- [ ] one fixed concurrency-4 finite batch is recorded;
- [ ] three fresh-root runs are completed;
- [ ] p50/p95, batch CPU, RSS/VmHWM, and DB/WAL/resource convergence are recorded;
- [ ] no pending requests or active reservations remain after stabilization;
- [ ] thermal/governor caveats are recorded where available;
- [ ] sanitized JSON evidence and concise plan closure are committed;
- [ ] current-thread/SQLite/routing-lock/stream-handoff decisions are explicitly
      `keep` or `investigate separately`;
- [ ] no production runtime/API/capability change is made in this plan.

## Handoff sequence

1. Check out exact current `main` and record its SHA.
2. Build or SHA-verify the Linux aarch64 candidate.
3. Make the narrow optional sampling extension to
   `scripts/qualification_sbc.py` and its tooling tests.
4. Run the ordinary physical-SBC qualification once.
5. Run the benchmark mode three times at `--benchmark-samples 30`.
6. Review resource convergence and thermal context.
7. Commit the sanitized report and append this plan's closure evidence.
8. Stop. If evidence suggests a runtime change, write a new narrowly scoped
   plan rather than implementing it here.

## Closure — 2026-09-21

The physical target-class pass completed on a Raspberry Pi 5. The exact
on-device release candidate was qualified before benchmark mode and was used
for all three fresh-root runs:

- repository commit: `27c21945913f3fb4783982c64d6e3776a0cec999`;
- candidate SHA-256: `4b58060caebd6158a0e1d05762c17d4989c50e4d991eadfaee4c2e222a8b48f8`;
- candidate size: `30,226,744` bytes;
- Rust: `rustc 1.98.1 (48a229cea 2026-09-01)`;
- board: Raspberry Pi 5 Model B Rev 1.0, four cores, 7.75 GiB reported RAM;
- OS/kernel: Ubuntu 24.04.4 LTS / Linux `6.8.0-1064-raspi`;
- root storage: non-rotational MMC, ext4;
- CPU governor: `ondemand` for all runs; observed end frequency policy varied
  from `1500000` to `2300000` across runs;
- temperature: `48.5 °C` median at start and `46.9 °C` median at end, with no
  thermal-throttle evidence.

The ordinary Q008 qualification passed, then `--benchmark-samples 30` passed
three times with the expected five warm-ups per sequential batch. Median of
the three runs:

| Batch | p50 elapsed | p95 elapsed | p50 TTFT | p95 TTFT | CPU | Result |
|---|---:|---:|---:|---:|---:|---|
| Native Responses finite | 3 ms | 1791 ms | 3 ms | 1790 ms | 110 ms batch CPU | measured |
| Native Responses streaming | 3 ms | 4 ms | 2 ms | 3 ms | 70 ms batch CPU | 30 terminal-complete streams |
| Responses → Anthropic streaming | — | — | — | — | — | `not measured` |

The translated fixture path returned HTTP 200 during warm-up but did not emit
Anthropic `message_stop` evidence, so it was recorded as `not measured`; no
routing or capability checks were weakened. The fixed concurrency-4 batch
completed `32/32` requests in a median `1853 ms` (`17.268` requests/s), with
median `80 ms` process CPU and p50/p95 request timing of `7/1803 ms`.

Across runs, baseline RSS was `19,013,632` bytes median, final stabilized RSS
was `19,886,080` bytes median, and peak `VmHWM` was `20,316,160` bytes median.
Final database/WAL sizes were consistently `712,704` / `4,260,112` bytes.
The three-second stabilization samples reported zero pending requests, active
reservations, finalization jobs, active leases, retiring leases, and terminal
references in every run. The sanitized aggregate report is
[`artifacts/qualification/235-sbc-target-benchmark.json`](../artifacts/qualification/235-sbc-target-benchmark.json).

Decisions: keep the Tokio `current_thread` runtime, single SQLite gate,
routing selection lock, and streaming handoff. The run-to-run finite and
concurrency tails were variable rather than a repeatable localized bottleneck,
and resource ownership converged cleanly; no follow-up architecture plan is
justified by this evidence.

Validation evidence:

- `uv run python scripts/qualification_sbc.py ...` ordinary qualification:
  pass;
- three `--benchmark-samples 30` runs with `--expected-sha256`: pass;
- `cargo fmt --manifest-path rust/Cargo.toml --all -- --check`: pass;
- default and `--no-default-features` Clippy/checks: pass;
- serial `CARGO_BUILD_JOBS=1 cargo test --manifest-path rust/Cargo.toml
  --workspace --all-targets -- --test-threads=1`: pass;
- `uv sync --frozen`, Ruff, Pyright, and tooling tests: pass (`82 passed,
  3 skipped`).
