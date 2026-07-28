# Real Proxy-Path Performance and Dispatch-Writer Benchmark

Date: 2026-07-28
Status: implementation handoff

Parent roadmap:

- `plans/031-upstream-hardening-corrective-roadmap.md`

Depends on:

- `plans/033-real-eggpool-runtime-test-harness.md`
- `plans/035-provider-bound-request-pipeline-completion.md`

Implementation baseline:

- completion commit of Plan 035

## Objective

Replace the prior direct-HTTPX/respx performance claims with reproducible measurements through the actual Eggpool proxy runtime.

This phase must quantify request-path latency, throughput, JSON operations, SQLite transaction behavior, dispatch-writer behavior, finalization latency, and observability overhead using the same production components exercised in normal service. It must not claim a dispatch-writer-enabled result unless the writer is actually instantiated and used.

The phase may implement narrow performance fixes exposed by the benchmark, but it must not change request semantics or weaken correctness gates.

## Scope

### In scope

- Real ASGI proxy requests through the Plan 033 harness.
- Native and transcoded request profiles.
- Streaming and non-streaming profiles.
- Dispatch writer disabled and enabled configurations.
- Detailed span sampling at 0%, default, and 100%.
- Exact request/response JSON operation counts.
- Selection lock wait/hold time.
- SQLite lock wait and transaction duration.
- finalization completion latency.
- dispatch-writer queue age, batch formation, transaction, delivery, batch size, and saturation metrics.
- throughput and latency percentiles.
- RSS/task/thread/file-descriptor snapshots around bounded benchmark runs.
- committed raw baseline/final artifacts.
- narrow hot-path fixes directly evidenced by the measurements.

### Out of scope

- Long-duration leak proof; Plan 037 owns soak.
- Live internet/provider latency.
- Changing routing outcomes for speed.
- Disabling persistence, accounting, finalization, capability checks, or instrumentation required by production.
- Broad database schema changes.
- Making the dispatch writer default-on; the result may recommend but not perform that rollout.
- Dashboard redesign.

## Benchmark integrity rules

1. Every request enters Eggpool's ASGI endpoint.
2. Provider clients call deterministic local/mock upstreams behind the production client boundary.
3. Database persistence and finalization remain enabled.
4. Warm-up samples are excluded explicitly.
5. Raw per-run aggregates are stored, not only prose summaries.
6. Thresholds compare equivalent profiles on the same host/interpreter.
7. A test never labels an ordinary client request as “writer enabled” without asserting writer submission/commit counters increased.
8. JSON operation counts are exact for deterministic profiles.
9. Benchmarks do not monkeypatch away correctness-critical work.
10. Evidence reports measured values separately from pass/fail thresholds.

## Workstream A — Benchmark runner

Add:

- `scripts/benchmark_plan_036.py`

The runner must accept:

```text
--profile <name|all>
--requests <count>
--concurrency <count>
--rounds <count>
--output <json-path>
--python-label <text>
--commit <sha>
```

Recommended profiles:

- `native_nonstream_writer_off`
- `native_nonstream_writer_on`
- `native_stream_writer_off`
- `native_stream_writer_on`
- `transcoded_nonstream_writer_off`
- `transcoded_nonstream_writer_on`
- `transcoded_stream_writer_off`
- `transcoded_stream_writer_on`
- `thinking_cache_stream_combined`
- `validation_error_then_success`
- `db_recovery_after_invalidation`
- `span_sampling_0`
- `span_sampling_default`
- `span_sampling_100`

Output must be machine-readable JSON with schema version, environment, config, warm-up count, measured request count, concurrency, round count, percentiles, counters, and resource snapshots.

Do not scrape logs to build the artifact.

## Workstream B — Baseline protocol

The first Plan 036 commit should add only benchmark tooling/tests and no production optimization. Record that commit as the Plan 036 measurement baseline.

Run the full benchmark on that baseline and write:

- `artifacts/plan-036-baseline.json`

After any narrow fixes, run the identical command on the final implementation tree and write:

- `artifacts/plan-036-final.json`

Generate a human-readable comparison:

- `artifacts/plan-036-comparison.md`

The comparison must verify matching profile parameters. Reject comparisons with different request counts, concurrency, writer settings, Python versions, or material host-load differences unless explicitly labeled non-comparable.

## Workstream C — Real proxy profile construction

Use the Plan 033 harness with:

- temporary migrated SQLite;
- one provider/account for serial profiles;
- at least two accounts for concurrent/routing profiles;
- deterministic model catalog;
- minimal successful upstream response;
- streaming response with multiple chunks;
- optional transcoding provider;
- optional thinking/cache transformations;
- process-owned finalization and recovery enabled.

For writer-on profiles:

- configure `database.dispatch_writer.enabled = true` before runtime construction;
- assert the coordinator is wired with `use_dispatch_writer = true`;
- assert writer `submitted`, `committed_batches`, and `committed_intents` counters increase;
- assert direct persistence path counters do not increase for those intents if such diagnostics exist.

For writer-off profiles, assert the writer is absent or unused.

## Workstream D — Measurements

Record at minimum:

### Request latency

- p50, p90, p95, p99, max;
- local pre-upstream p50/p95/p99;
- coordinator dispatch overhead p50/p95/p99;
- time to first byte for streaming;
- stream completion latency;
- finalization completion latency after response end/cancel.

### Throughput

- requests per second;
- successful/error counts;
- stream chunks per second for streaming profile;
- writer intents per second and batches per second.

### Lock/database

- selection claim lock wait/held p50/p95/p99;
- SQLite transaction wait p50/p95/p99;
- transaction duration p50/p95/p99;
- finalizer transaction duration;
- recovery duration for recovery profile;
- database connection epoch delta.

### Dispatch writer

- enqueue wait;
- queue age;
- batch formation wait;
- transaction duration;
- result delivery;
- end-to-end intent latency;
- batch size distribution;
- queue max/occupancy;
- saturation/timeouts/failures;
- sample-window lengths.

### JSON/payload

- client request decodes;
- provider request encodes;
- provider response decodes;
- client response encodes;
- stream event decodes/encodes;
- prepared-transcode reuse count;
- segmentation reuse/invalidation counts.

### Resources

- RSS before/after;
- asyncio tasks before/after;
- threads before/after;
- file descriptors before/after where supported;
- finalization active jobs before/after;
- recorder sample lengths.

## Workstream E — Exact operation-count contracts

Add deterministic tests:

- `tests/perf/test_plan_036_proxy_json_operations.py`

Required exact counts should be derived from the completed Plan 035 lifecycle. Record expected phase counts in the test with comments.

At minimum:

1. Native non-stream, no transform.
2. Native stream with usage injection.
3. Transcoded non-stream with prepared translated mapping.
4. Thinking + synthetic cache + stream usage combined.
5. Local capability rejection before upstream.
6. Upstream error response re-rendered to client protocol.
7. Retry to a different provider.

The test must fail on one extra common-path decode or encode. Do not use per-20-request bounds that allow three operations per request without attribution.

## Workstream F — Relative latency gates

Add:

- `tests/perf/test_plan_036_proxy_latency_contract.py`
- `tests/perf/test_plan_036_dispatch_writer_profile.py`
- `tests/perf/test_plan_036_span_sampling_overhead.py`

Use multiple rounds and compare medians to reduce noise.

Recommended bounded CI shape:

- 2 warm-up rounds;
- 5 measured rounds;
- 100–500 requests per round depending on profile;
- fixed concurrency;
- compare median-of-round p50/p95.

Required gates:

### Correctness-preserving baseline gate

For writer-off native no-transform after any Plan 036 production fix:

- median p50 regression <= 10%;
- median p95 regression <= 15%;
- or absolute regression <= 0.5 ms when baseline values are below 5 ms.

If the environment is too noisy for a relative assertion, use a calibrated no-op ASGI control in the same run and compare Eggpool-over-control delta. Document the method; do not widen thresholds arbitrarily.

### Writer gate

Under concurrency high enough to form batches:

- writer-on commits fewer SQLite transactions than intents;
- median batch size > 1;
- queue age p95 remains below configured maximum batch wait plus bounded scheduler tolerance;
- no submit timeout/saturation under the defined load;
- end-to-end p95 does not exceed writer-off by more than 20% unless transaction reduction is explicitly prioritized and documented;
- all requests finalize correctly.

### Span sampling gate

Compare 0%, default, and 100% detailed spans:

- default sampling produces a sampled fraction within statistical tolerance of configuration;
- one request is either coherently sampled or unsampled;
- recorder windows remain bounded;
- default sampling overhead relative to 0% is <= 5% median p95 or <= 0.25 ms absolute;
- 100% result is recorded but need not meet production threshold.

## Workstream G — Narrow optimization policy

If the baseline identifies a material regression or hot spot, fixes may target:

- avoidable request/response JSON operations;
- lock scope;
- redundant repository lookups;
- writer batch timing/accounting;
- finalizer diagnostic work inside transaction;
- excessive per-request hashing/formatting/allocation in sampled paths;
- unnecessary task creation in the writer or finalizer.

For each fix:

1. Add a counter/span test proving the hot operation.
2. Make one narrow production change.
3. Rerun correctness suites from Plans 032–035.
4. Rerun the identical benchmark profile.
5. Keep the change only if correctness remains green and the measurement improves or removes an unbounded behavior.

Do not optimize by disabling accounting, tracing categories, database durability, or validation.

## Workstream H — CI partition

Add or update a bounded performance job that runs:

- exact JSON operation tests;
- short proxy latency contracts;
- writer profile contract;
- span sampling contract.

The full benchmark runner is an evidence command and need not execute in every PR. Store benchmark JSON as a CI artifact when the full scheduled/manual job runs.

Set stable environment controls where supported:

- `PYTHONHASHSEED=0`;
- `TZ=UTC`;
- fixed asyncio event loop policy;
- no live network;
- fixed upstream response size/chunk cadence;
- CPU/load metadata in artifact.

## Evidence artifact requirements

`artifacts/plan-036-comparison.md` must include:

- baseline and final commit/tree;
- exact runner commands;
- host/OS/CPU/Python information;
- profile configuration table;
- raw artifact checksums;
- p50/p95/p99 and throughput comparison;
- operation-count comparison;
- writer transaction/batch comparison;
- span sampling overhead comparison;
- resource before/after deltas;
- narrow fixes made and measured effect;
- known noise/limitations without converting them into unsupported claims.

## Implementation sequence

1. Add benchmark runner and result schema.
2. Add real proxy profile builders using Plan 033.
3. Add exact operation-count tests.
4. Add bounded latency/writer/sampling tests.
5. Commit tooling-only baseline.
6. Run and commit baseline JSON.
7. Analyze measured hot spots.
8. Implement only narrow evidenced fixes.
9. Rerun Plans 032–035 correctness suites.
10. Run identical final benchmark.
11. Commit final JSON and comparison markdown.
12. Add/adjust CI performance partition.
13. Record focused test results in the comparison artifact.

## Focused verification commands

```bash
uv run pytest \
  tests/perf/test_plan_036_proxy_json_operations.py \
  tests/perf/test_plan_036_proxy_latency_contract.py \
  tests/perf/test_plan_036_dispatch_writer_profile.py \
  tests/perf/test_plan_036_span_sampling_overhead.py \
  -m performance -q --tb=short

uv run python scripts/benchmark_plan_036.py \
  --profile all \
  --requests 1000 \
  --concurrency 50 \
  --rounds 5 \
  --output artifacts/plan-036-final.json \
  --commit "$(git rev-parse HEAD)"
```

## Acceptance criteria

### Benchmark fidelity

- [ ] Every measured request enters the real Eggpool proxy runtime.
- [ ] Temporary SQLite persistence and finalization are enabled.
- [ ] Writer-on profiles prove writer intents/batches actually occurred.
- [ ] Writer-off profiles prove direct persistence behavior.
- [ ] Raw machine-readable baseline and final artifacts are committed.
- [ ] Profile parameters are identical for baseline/final comparison.

### Operation counts

- [ ] Exact per-profile JSON operation contracts pass.
- [ ] Plan 035 single-payload lifecycle is visible in counts.
- [ ] Retry/provider-switch counts are attributed per attempt.
- [ ] No broad operation bounds conceal duplicate work.

### Performance

- [ ] Native writer-off common path meets relative/absolute regression gate.
- [ ] Writer-on demonstrates transaction reduction under batching load.
- [ ] Writer queue age and delivery remain bounded.
- [ ] Default span sampling overhead meets its gate.
- [ ] Finalization and database transaction percentiles are recorded.
- [ ] No optimization disables correctness-critical behavior.

### Quality and evidence

- [ ] Plans 032–035 focused correctness suites remain green.
- [ ] Performance tests pass on supported Python 3.11/3.12 matrix where CI permits.
- [ ] Ruff and Pyright are clean.
- [ ] Comparison artifact reports measured values, not approximations.
- [ ] CI job uploads raw benchmark artifacts for scheduled/manual full runs.

## Explicit rejection conditions

Do not mark this plan complete if:

- benchmarks call mock upstream directly rather than Eggpool;
- “dispatch writer enabled” is inferred from configuration without writer counters;
- benchmark setup mocks repositories/finalization out of the path;
- operation-count tests use loose aggregate bounds;
- baseline and final profiles differ materially;
- evidence contains hand-written placeholder latency values;
- a performance improvement comes from skipping persistence, health, finalization, validation, or required accounting;
- fixed thresholds are widened solely because a test failed without noise analysis.
