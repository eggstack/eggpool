# Plan 236 — Physical SBC Benchmark Evidence Corrective Pass

Date: 2026-09-21
Status: complete
Planning baseline: `4208a3ca3ef131aa29ba3b00b217bbe4ffe23735`
Corrects: `plans/235-physical-sbc-target-class-benchmark-pass.md`
Priority: P1 narrow qualification/evidence correction
Execution target: physical Linux/aarch64 Raspberry Pi-class SBC

## Purpose

Correct the three evidence defects discovered after Plan 235 without reopening
the EggPool runtime or the completed Plans 230–234 performance architecture.

The Plan 235 physical Raspberry Pi 5 execution established useful resource and
ownership evidence, but its performance closure is not authoritative yet
because:

1. the translated Responses-to-Anthropic stream benchmark checked for the
   upstream Anthropic `message_stop` marker in the downstream Responses body;
2. the timing batches ran under the ordinary Q008 qualification fixture, whose
   intentionally aggressive 1-second model refresh, 2-second metrics flush,
   and 2-second automatic backup cadence can contaminate latency tails;
3. benchmark additions changed the ordinary Q008 report schema/shape even when
   `--benchmark-samples` is absent, contrary to Plan 235's default-off
   compatibility requirement.

This corrective pass is tooling/evidence-only. No production runtime change is
authorized.

## Authority paths

Primary implementation/evidence owners:

- `scripts/qualification_sbc.py`
- `tests/tooling/test_qualification_sbc.py`
- `tests/tooling/fixtures/qualification/sbc.toml`
- new benchmark-only fixture:
  `tests/tooling/fixtures/qualification/sbc-benchmark.toml`
- `config.sbc.example.toml`
- `rust/tests/wire_qualification.rs`
- `docs/raspberry-pi.md`
- `architecture/deep-dive-deployment.md`

Historical evidence remains:

- `plans/235-physical-sbc-target-class-benchmark-pass.md`
- `artifacts/qualification/235-sbc-target-benchmark.json`

Do not rewrite Plan 235. Plan 236 closure must state that its corrected
performance measurements supersede Plan 235's timing interpretation while
preserving Plan 235 as historical evidence.

## Corrective finding A — translated terminal validation is on the wrong surface

The benchmark helper sends a **Responses client request** for `q008-messages`.
The fixture pins that model to the Anthropic Messages upstream surface.

Therefore:

~~~text
client:   OpenAI Responses
upstream: Anthropic Messages
provider terminal input: message_stop
client terminal output:   response.completed
~~~

The benchmark currently requires `message_stop` in the downstream body. That
tests the provider-side grammar after EggPool has already translated back to
the client surface, so a valid translated stream is incorrectly classified as
`not measured`.

### Required correction

For the translated benchmark:

- continue sending the request through the Responses client endpoint;
- continue selecting `q008-messages`, whose fixture wire profile is
  `anthropic_messages`;
- require downstream `response.completed` terminal evidence;
- prove that the loopback provider actually received the request on the
  Messages path.

The last condition prevents an accidental native Responses route from making
the test appear translated.

A small fixed counter in the loopback fixture is acceptable, for example fixed
buckets for:

- `/chat/completions`;
- `/responses`;
- `/messages`.

Do not retain arbitrary URLs, request bodies, headers, prompts, or response
content.

The existing runtime qualification in `rust/tests/wire_qualification.rs`
remains the semantic authority for cross-surface stream encoding. Do not change
Rust wire code to satisfy this benchmark.

## Corrective finding B — qualification cadence contaminates benchmark timing

The ordinary qualification fixture deliberately exercises lifecycle/background
work:

~~~text
models.refresh_interval_s = 1
metrics.flush_interval_s = 2
backup.enabled = true
backup.interval_s = 2
backup.startup_delay_s = 1
~~~

That is useful Q008 coverage but is not representative of EggPool's documented
low-wear SBC steady-state profile. It overlaps the 30-request timing batches and
is a plausible source of the multi-second finite/concurrency tail variance
recorded in Plan 235.

### Required correction

Leave
`tests/tooling/fixtures/qualification/sbc.toml` unchanged for ordinary Q008.

Add
`tests/tooling/fixtures/qualification/sbc-benchmark.toml` for benchmark runs.
It should preserve the same synthetic provider, account, models, wire surfaces,
loopback bind, request bounds, WAL behavior, and secret-safe fixture keys, but
mirror the relevant steady-state settings from `config.sbc.example.toml`:

~~~toml
[server]
access_log = false
threads = 1

[database]
busy_timeout_ms = 10000
wal = true
synchronous = "NORMAL"
worker_threads = 1
journal_size_limit = 67108864

[models]
startup_refresh = true
refresh_interval_s = 300

[model_info]
enabled = false
startup_refresh = false

[metrics]
write_mode = "low_wear"
flush_interval_s = 120
max_buffered_events = 250
aggregate_only = true
event_loop_lag_enabled = false
cleanup_interval_s = 86400

[routing.trace]
mode = "off"
sample_rate = 0.0

[backup]
enabled = false
interval_s = 86400
~~~

Use the exact current config schema and `config.sbc.example.toml` as
authority; do not add unsupported fields merely to match this conceptual
excerpt.

Benchmark reports must record the relevant background cadence/enablement
scalars so later readers can distinguish Q008 lifecycle stress from target-class
steady-state timing.

Do not alter production defaults.

## Corrective finding C — ordinary Q008 default behavior drifted

Plan 235 required `--benchmark-samples` to default to zero while ordinary
qualification behavior remained unchanged. The implementation instead changed
the global report schema from `runtime-q008.v1` to `runtime-q008.v2` and
made benchmark-oriented observations part of the ordinary report path.

### Required correction

Separate the ordinary qualification report contract from the optional benchmark
extension.

Preferred shape:

~~~text
benchmark_samples == 0
    -> ordinary Q008 report contract: runtime-q008.v1

benchmark_samples > 0
    -> benchmark-extended report contract: runtime-q008.v2
~~~

At minimum, default mode must not emit benchmark-only fields such as benchmark
sections, benchmark-only peak/thermal/frequency projections, or other fields
introduced solely by Plan 235.

Use the pre-Plan-235 implementation at
`a2b840f5a24f1e91727c65990a04be39e4bbfbf0` as the compatibility reference
for default-mode report structure.

Do not force consumers of ordinary Q008 to adopt a benchmark schema.

## Implementation shape

Keep the correction small.

### 1. Benchmark case description

Prefer one internal typed/helper description for each benchmark case containing:

- client surface;
- model;
- stream flag;
- expected **client-side** terminal marker;
- expected fixture upstream path when a translated route is required.

Do not scatter surface assumptions across the execution loop.

### 2. Resource sampling

Ordinary qualification keeps its pre-Plan-235 shape.

Benchmark mode may additionally capture:

- `VmHWM`;
- start/end thermal reading;
- start/end CPU frequency policy;
- repository commit SHA;
- benchmark cadence facts.

Reuse the current procfs helpers. No profiler, allocator hook, or new
dependency.

### 3. Existing benchmark dimensions

Do not expand the corpus. Retain:

- 5 warm-ups + 30 native Responses finite requests;
- 5 warm-ups + 30 native Responses streams;
- 5 warm-ups + 30 Responses-client / Anthropic-upstream translated streams;
- one 32-request concurrency-4 native finite batch;
- three fresh-root physical-SBC runs;
- 3-second resource convergence check.

Do not add p99.

## Physical rerun

After the tooling correction lands, rerun on the same class of physically
attested Linux/aarch64 target. Raspberry Pi 5 is preferred for comparability
with Plan 235.

Build the corrected checkout on-device:

~~~bash
cargo build --manifest-path rust/Cargo.toml --locked --release
sha256sum rust/target/release/eggpool
~~~

First run the ordinary qualifier with the existing Q008 fixture and **without**
benchmark mode:

~~~bash
uv run python scripts/qualification_sbc.py \
  --binary rust/target/release/eggpool \
  --candidate-origin on-device-release-build \
  --output artifacts/qualification/236-sbc-functional.json
~~~

Verify that it passes and emits the ordinary Q008 contract.

Then run the benchmark three times from fresh temporary roots using the
benchmark-only fixture:

~~~bash
uv run python scripts/qualification_sbc.py \
  --binary rust/target/release/eggpool \
  --candidate-origin on-device-release-build \
  --expected-sha256 <candidate-sha256> \
  --config-fixture tests/tooling/fixtures/qualification/sbc-benchmark.toml \
  --benchmark-samples 30 \
  --output artifacts/qualification/236-sbc-benchmark-run-1.json
~~~

Repeat as run 2 and run 3.

## Corrected evidence artifact

Commit one sanitized aggregate record:

~~~text
artifacts/qualification/236-sbc-target-benchmark-corrected.json
~~~

Do not delete or overwrite
`artifacts/qualification/235-sbc-target-benchmark.json`.

The corrected aggregate must contain:

- exact repository/candidate SHA;
- physical board attestation;
- benchmark fixture/cadence facts;
- three individual run summaries;
- median-of-runs summary;
- native finite p50/p95 and TTFT;
- native stream p50/p95 and TTFT;
- translated Responses-to-Anthropic p50/p95 and TTFT, or a precise corrected
  `not measured` reason;
- proof that translated samples reached the fixture Messages path;
- concurrency-4 elapsed/throughput/p50/p95;
- batch CPU;
- baseline/final/peak RSS;
- final DB/WAL state;
- final pending request/reservation/finalization ownership state;
- thermal/frequency caveats.

If the corrected finite tails remain highly variable, report them as such.
Do not invent a runtime cause without evidence.

## Evidence interpretation

Plan 235 already demonstrated clean resource convergence on a Pi 5. The
corrective pass should answer only the remaining questions:

1. What do request-path tails look like without artificial 1–2 second
   qualification background cadence?
2. What is the actual cost of the Responses-client / Anthropic-upstream
   translated stream path?
3. Does the corrected evidence provide any repeatable reason to revisit
   current-thread Tokio, the single SQLite gate, routing selection lock, or
   streaming handoff?

The default decision remains **keep** unless corrected measurements show a
repeatable localized bottleneck.

Any runtime architecture finding must become a new plan. Do not fix it here.

## Tests and validation

Add focused tooling tests proving:

- `--benchmark-samples` still accepts only `1..=100`;
- ordinary/default mode selects the v1 Q008 contract;
- benchmark mode selects the extended v2 contract;
- ordinary fixture retains its aggressive lifecycle cadence;
- benchmark fixture retains the low-wear/steady-state cadence;
- translated benchmark expects downstream `response.completed`;
- translated benchmark requires/proves the fixture Messages upstream path;
- p50/p95 calculation remains deterministic and no p99 is emitted;
- credentials and bodies remain absent from reports.

Run:

~~~bash
uv sync --frozen
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run pytest tests/tooling/test_qualification_sbc.py -q
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
git diff --check
~~~

Because this plan is tooling/evidence-only, a full Rust suite is not required
unless Rust source changes unexpectedly.

The physical target must additionally pass the ordinary Q008 qualifier before
the corrected benchmark numbers are accepted.

## Documentation/evidence cleanup

Do not modify the completed Plan 235 file.

Current documentation may continue to describe the optional target-class
command, but update it if necessary so benchmark examples explicitly use the
benchmark-only fixture.

Plan 236 closure must state:

- Plan 235's resource-convergence evidence remains valid;
- Plan 235's translated-stream result and finite/concurrency timing
  interpretation are superseded by Plan 236;
- whether corrected measurements still support the four architectural
  `keep` decisions.

Do not erase the historical numbers.

## Stop conditions

Stop and write a separate implementation plan if correction would require:

- any `rust/src/` change;
- a Cargo dependency/profile change;
- a production config/default change;
- weakening routing or capability checks;
- a second benchmark framework;
- hardware CI;
- a profiler/allocator dependency;
- a second SSE parser;
- runtime concurrency changes.

## Completion criteria

- [x] ordinary Q008 default report compatibility is restored and tested;
- [x] ordinary Q008 fixture is unchanged;
- [x] benchmark-only fixture reflects current low-wear SBC steady-state cadence;
- [x] translated stream validates downstream `response.completed` and proves
      Anthropic Messages upstream routing;
- [x] ordinary physical-SBC qualification passes;
- [x] three corrected 30-sample benchmark runs complete on a physical
      Linux/aarch64 SBC;
- [x] corrected aggregate evidence is committed under the Plan 236 artifact
      path;
- [x] resource ownership converges after every run;
- [x] Plan 235 timing/translated evidence is explicitly superseded, not erased;
- [x] current-thread/SQLite/routing-lock/stream-handoff decisions are
      re-evaluated from corrected evidence;
- [x] no production runtime/API/capability behavior changes.

## Handoff sequence

1. Preserve Plan 235 and its artifact unchanged.
2. Restore ordinary Q008 default report compatibility.
3. Add the benchmark-only low-wear fixture.
4. Correct translated client-terminal validation and add upstream-path proof.
5. Run tooling tests.
6. Build/SHA the exact corrected checkout on the physical SBC.
7. Run ordinary Q008 once.
8. Run three corrected benchmark passes with 30 samples.
9. Commit the corrected aggregate artifact and append closure evidence to this
   Plan 236 only.
10. Stop; create a separate plan for any actual runtime bottleneck.

## Closure — 2026-09-21

The corrective pass completed on the same Raspberry Pi 5 class as Plan 235.
The corrected checkout was committed as `3290256a` before the physical runs,
then built on-device with no Rust changes (candidate byte-identical to Plan
235):

- repository commit: `3290256a5bbd10b680c6ac70cd50666626416250`;
- candidate SHA-256: `4b58060caebd6158a0e1d05762c17d4989c50e4d991eadfaee4c2e222a8b48f8`;
- candidate size: `30,226,744` bytes;
- Rust: `rustc 1.98.1 (48a229cea 2026-09-01)`;
- board: Raspberry Pi 5 Model B Rev 1.0, four cores, 7.75 GiB reported RAM;
- OS/kernel: Ubuntu 24.04.4 LTS / Linux `6.8.0-1064-raspi`;
- root storage: non-rotational MMC, ext4;
- CPU governor: `ondemand` for all runs; observed end frequency policy varied
  from `1800000` to `1900000` across runs;
- temperature: `49.0–50.1 °C` at start and `46.3–48.0 °C` at end, with no
  thermal-throttle evidence.

Tooling corrections landed as committed:

- ordinary/default mode emits the Q008 `runtime-q008.v1` contract with no
  benchmark-only fields (no `peak_rss_bytes`, no repository SHA, no
  end-of-run thermal/frequency projection); benchmark mode emits the extended
  `runtime-q008.v2` contract with cadence facts and the benchmark section;
- ordinary fixture `sbc.toml` unchanged (1s refresh, 2s flush, 2s backup);
- new `sbc-benchmark.toml` mirrors the low-wear steady-state profile (300s
  refresh, 120s flush, backup disabled, trace off);
- translated benchmark requires downstream `response.completed` and proves
  `35` fixture `/messages` observations per batch (5 warm-ups + 30 samples).

The ordinary Q008 qualification passed (`runtime-q008.v1`, 24 functional
checks), then `--benchmark-samples 30` with the benchmark-only fixture passed
three times from fresh roots. Median of the three runs:

| Batch | p50 elapsed | p95 elapsed | p50 TTFT | p95 TTFT | CPU | Result |
|---|---:|---:|---:|---:|---:|---|
| Native Responses finite | 4 ms | 2370 ms | 3 ms | 2370 ms | 100 ms batch CPU | measured |
| Native Responses streaming | 3 ms | 4 ms | 3 ms | 3 ms | 90 ms batch CPU | 30 terminal-complete streams |
| Responses → Anthropic streaming | 4 ms | 4 ms | 3 ms | 4 ms | 90 ms batch CPU | measured, Messages-path proof |

The translated path now measures at native-streaming cost in every run. Finite
p95 (`166/2397/2370 ms`) and concurrency-4 batches (`438/3988/753 ms`,
`73.051/8.024/42.509` requests/s) remain variable run to run even without the
aggressive Q008 background cadence; this is reported as observed without an
invented runtime cause. All runs converged with zero pending requests, active
reservations, finalization jobs, active leases, retiring leases, and terminal
references. Baseline RSS was `17,227,776` bytes median, final stabilized RSS
`18,235,392` bytes median, peak `VmHWM` `18,235,392` bytes median. Final
database/WAL sizes were `868,352` / `4,231,272` bytes median. The sanitized
aggregate report is
[`artifacts/qualification/236-sbc-target-benchmark-corrected.json`](../artifacts/qualification/236-sbc-target-benchmark-corrected.json).

Supersession statement: Plan 235's resource-convergence evidence remains
valid. Plan 235's translated-stream result (`not measured`) and its
finite/concurrency timing interpretation are superseded by the corrected Plan
236 measurements above; Plan 235 and its artifact are preserved unchanged as
historical evidence.

Decisions: keep the Tokio `current_thread` runtime, single SQLite gate,
routing selection lock, and streaming handoff. The corrected evidence shows
the translated path costs no more than the native streaming path, and the
remaining tail variance is not a repeatable localized bottleneck; no
follow-up architecture plan is justified. No production runtime, API,
capability, config-default, or dependency behavior was changed.

Validation evidence:

- `uv run ruff format --check scripts/ tests/tooling/`: pass;
- `uv run ruff check scripts/ tests/tooling/`: pass;
- `uv run pyright scripts/`: pass;
- `uv run pytest tests/tooling/test_qualification_sbc.py -q`: pass (`16 passed`);
- `uv run pytest tests/tooling/ -q --tb=short --maxfail=1`: pass (`91 passed,
  3 skipped`);
- `uv run python scripts/validate_release_docs.py`: pass;
- `cargo fmt --manifest-path rust/Cargo.toml --all -- --check`: pass;
- `cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets
  -- -D warnings` (default and `--no-default-features`): pass;
- `cargo check --manifest-path rust/Cargo.toml --workspace --all-targets
  --no-default-features`: pass;
- ordinary qualification on the Pi: pass (`runtime-q008.v1`);
- three `--benchmark-samples 30` runs with `--expected-sha256` and
  `--config-fixture sbc-benchmark.toml`: pass (`runtime-q008.v2`).
- Full serial Rust workspace suite was not rerun for this tooling-only pass
  (no `rust/` change); default and no-default Clippy/checks pass and remote
  CI qualifies the workspace.
