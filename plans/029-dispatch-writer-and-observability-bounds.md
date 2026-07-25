# Dispatch Writer and Observability Bounds

Date: 2026-07-25
Status: implementation handoff

Parent roadmap:

- `plans/022-upstream-error-isolation-and-hotpath-hardening-roadmap.md`

Depends on:

- `plans/023-error-isolation-reproducer-and-invariant-baseline.md`
- `plans/028-provider-payload-lifecycle-hotpath-consolidation.md`

## Objective

Make dispatch persistence diagnostics, detailed request instrumentation, and batching behavior safe for indefinite process lifetime. Replace unbounded historical sample lists, correct mislabeled timing measurements, ensure percentile snapshot cost is bounded by configuration rather than uptime, and tune the optional dispatch writer for latency-sensitive proxy traffic without weakening durable selection semantics.

This phase also establishes an explicit production sampling policy for detailed dispatch spans and prevents observability from becoming a measurable hot-path bottleneck.

## Current defects to close

The dispatch persistence writer currently retains batch sizes, batch timing, transaction timing, and queue-depth samples in ordinary lists. Those lists grow for the lifetime of the process, snapshots sort the full history, and transaction timing can be appended once per result rather than once per transaction. The field named batch wait may record persistence duration rather than actual queue/batch accumulation wait.

Other request recorders are bounded, but detailed span capture defaults to full sampling and acquires a threading lock for every recorded span. At high request rates, diagnostic work can become a material share of local dispatch latency.

## Scope

### In scope

- Bounded dispatch-writer sample storage.
- Correct queue wait, batch formation wait, transaction duration, and end-to-end intent latency metrics.
- Constant-bounded percentile snapshot computation.
- Accurate one-sample-per-event semantics.
- Adaptive batching policy and low-pressure fast path.
- Queue saturation and backpressure diagnostics.
- Production span sampling defaults and generation-aware configuration.
- Optional per-loop or sharded recording if justified by measurement.
- Memory, CPU, latency, and correctness soak tests.
- Dashboard/diagnostic compatibility.

### Out of scope

- Making dispatch writer mandatory without evidence.
- Replacing SQLite.
- Removing correctness-critical dispatch persistence.
- Dropping diagnostic visibility to obtain benchmark improvements.
- Unbounded high-cardinality labels.
- A general telemetry framework rewrite.

## Workstream A — Define metric semantics precisely

Document and implement distinct timings:

- `enqueue_wait_ms`: time waiting for queue capacity before accepted.
- `queue_age_ms`: time from successful enqueue to writer claim.
- `batch_formation_wait_ms`: time from first claimed intent to batch close.
- `transaction_ms`: one duration per SQLite batch transaction.
- `result_delivery_ms`: time from commit completion to all result futures signaled.
- `intent_end_to_end_ms`: time from submission to result/exception delivery.
- `batch_size`: one sample per committed or failed batch.
- `queue_depth_at_submit`, `queue_depth_at_claim`, and `queue_depth_after_commit` where useful.

Do not reuse one sample array for multiple meanings. Rename diagnostics and dashboard fields when current names are inaccurate, with a compatibility alias/deprecation window if externally exposed.

Every timer must use a monotonic clock. Units must be stored and exposed consistently.

## Workstream B — Bound all sample storage

Replace ordinary lists with one of:

- bounded `deque(maxlen=N)` for small rolling-window snapshots;
- fixed-width histograms for high-volume distributions;
- bounded reservoir sampling when approximate long-window distribution is required.

Preferred approach:

- Rolling deques for operator-facing recent-window p50/p95/p99.
- Cumulative scalar counters for totals.
- Fixed bucket histograms for long-run distribution if needed.

Configuration must bound:

- samples per metric;
- number of metric series;
- retained recent batch/error history;
- high-watermark event history.

No diagnostic collection may scale with total requests, batches, accounts, models, or uptime without an explicit bounded cardinality policy.

## Workstream C — Correct one-sample-per-event accounting

Required corrections:

- Append `transaction_ms` once per batch transaction, not once per result in the batch.
- Append `batch_size` once per attempted batch.
- Record batch formation wait separately from persistence time.
- Count failed batches and failed intents separately.
- Count cancellations before claim, after claim/before commit, and after commit separately.
- Ensure queue-depth sampling does not disproportionately sample only busy or successful paths unless documented.
- Ensure result zip/length mismatches produce an invariant error and fail all unresolved futures deterministically.

Add table-driven tests asserting exact sample counts after known sequences.

## Workstream D — Make snapshots bounded and non-blocking

Snapshot requirements:

- Runtime bounded by configured window/bucket count.
- Copy only bounded data under lock.
- Sort outside the lock when deques are used.
- Do not await database or queue operations.
- Stable schema and deterministic ordering.
- Snapshot failure never affects dispatch.
- Empty and partial windows represented explicitly.

If snapshot consumers poll frequently, add a short-lived cached aggregate snapshot or incremental histogram summary. Cache invalidation must be based on sample generation, not wall-clock assumptions alone.

Performance tests must prove snapshot p95 remains stable after 10,000, 100,000, and 1,000,000 synthetic batches.

## Workstream E — Revisit dispatch writer loop ownership

The writer documents a single-loop model while bridging submissions via event-loop calls. Make ownership explicit and test it.

Requirements:

- Writer owns one loop and one drain task.
- Submission from the owning loop uses a direct low-overhead path where safe.
- Submission from another thread/loop uses the writer's captured loop, not the caller's loop, for queue mutation.
- Futures are completed thread-safely on the correct waiter context.
- Writer startup occurs once after the process loop is known.
- Rehash/runtime generation swaps do not start duplicate process writers.
- Shutdown closes the queue and resolves every submitted future exactly once.

If multi-loop Granian mode remains unsupported, fail configuration validation or readiness clearly rather than relying only on warnings when dispatch writer is enabled.

## Workstream F — Tune adaptive batching

The current maximum batch wait can be large relative to desired proxy dispatch latency. Define an adaptive policy.

Suggested behavior:

- Queue empty/low pressure: persist the first intent immediately or after a very small coalescing delay of 0–2 ms.
- Moderate pressure: drain currently queued work and allow a short bounded wait to reach a useful batch size.
- High pressure: batch up to maximum size without extra wait.
- Near queue saturation: prioritize drain and expose pressure state.

Configuration should separate:

- low-pressure coalescing delay;
- maximum high-pressure batch wait;
- maximum batch size;
- queue capacity;
- submit timeout.

Acceptance targets must be based on Plan 023/028 baselines:

- serial request dispatch should not incur tens of milliseconds of artificial wait;
- concurrent bursts should reduce transaction count meaningfully;
- p95 queue age stays within configured bound;
- throughput gain does not strand cancellations or corrupt ordering.

Ordering must be documented. If intents are persisted in queue order, tests must enforce it. If batching allows independent results, identity correctness remains mandatory.

## Workstream G — Backpressure and failure policy

Queue saturation must have deterministic behavior:

- No dropped correctness-critical intent.
- Submission either waits within configured bound or returns typed saturation error before runtime publication.
- Saturation error triggers selection compensation and no provider health effect.
- Queue closure/shutdown resolves all futures.
- Batch transaction failure fails every affected intent with the same transaction identity while allowing later batches after database recovery policy permits.
- Database invalidation joins Plan 027 recovery and pauses/retries safely.

Expose:

- current/max queue depth;
- occupancy ratio;
- oldest intent age;
- saturation count;
- submit timeout count;
- failed batches/intents;
- recovery pause state;
- drain throughput.

## Workstream H — Production dispatch-span sampling

Set a production-safe default for detailed spans, while retaining coarse always-on metrics.

Recommended policy:

- Coarse `local_pre_upstream` and total dispatch overhead remain bounded and always on.
- Fine-grained spans use deterministic request-level sampling, not an independent decision per span.
- One sampled request records all relevant spans, preserving a coherent trace.
- Sampling rate configurable, with a conservative production default such as 1–10% based on measured overhead.
- Diagnostic mode may use 100% sampling.
- Sampling decision is stable for a request ID and does not require random number generation per span.

Refactor current counter-based per-span sampling if it causes partial traces or uneven span coverage.

Add counters for sampled and unsampled requests so operators can interpret distributions.

## Workstream I — Lock and recorder contention

Measure the lock cost of:

- dispatch overhead recorder;
- local pre-upstream recorder;
- fine-grained span recorder;
- stream diagnostics;
- writer diagnostics.

If significant under high concurrency, consider:

- per-loop buffers merged at snapshot time;
- lock-free single-loop append for process-loop-only recorders;
- sharded locks;
- fixed histograms with atomic/GIL-safe integer increments.

Any change must preserve thread safety for supported topology. Do not remove locks based only on CPython assumptions if alternate loops/threads are supported by configuration.

## Workstream J — Bound cardinality

Audit labels and dictionaries keyed by:

- provider;
- model;
- account;
- protocol direction;
- error class;
- span name;
- outcome.

Rules:

- Span names and outcome categories must be finite enums/constants.
- Error classes normalized to bounded known labels plus `other`.
- Provider/model/account series use catalog/config bounds and must be pruned on generation retirement or capped with overflow aggregation.
- Raw exception messages never become keys.
- Request IDs never become retained metric keys.

Add long-running tests with model/provider churn and repeated rehash to prove stale series are released or bounded.

## Workstream K — Configuration and compatibility

Add settings under existing relevant sections rather than creating overlapping top-level controls.

Possible additions:

```toml
[database.dispatch_writer]
sample_window = 2048
low_pressure_batch_wait_ms = 0.5
high_pressure_batch_wait_ms = 5.0

[metrics.dispatch_spans]
sample_rate = 0.05
window_size = 512
```

Defaults must preserve correctness and use safe memory bounds. Validate relationships such as low wait <= high wait and batch size <= queue capacity.

If existing diagnostic JSON fields are renamed, support old fields for one documented compatibility period or version the snapshot schema. Avoid silently changing units.

## Workstream L — Tests and benchmarks

Suggested files:

- `tests/unit/test_plan_029_dispatch_writer_metrics.py`
- `tests/unit/test_plan_029_dispatch_writer_batching.py`
- `tests/unit/test_plan_029_dispatch_writer_loop_ownership.py`
- `tests/unit/test_plan_029_dispatch_writer_backpressure.py`
- `tests/unit/test_plan_029_span_sampling.py`
- `tests/unit/test_plan_029_metric_cardinality.py`
- `tests/perf/test_plan_029_snapshot_scaling.py`
- `tests/perf/test_plan_029_writer_latency_throughput.py`
- `tests/soak/test_plan_029_writer_resource_bounds.py`
- `tests/soak/test_plan_029_rehash_metric_churn.py`

Required workload matrix:

- Serial single intents.
- Bursts of 2, 4, 8, 32, 64, and 256 intents.
- Sustained mixed concurrency.
- Queue saturation.
- Cancellation before claim and after commit.
- Batch transaction failure and recovery.
- Single-loop normal topology.
- Cross-thread submission where supported.
- Rehash churn.
- One million synthetic batches for metrics-only soak.
- Extended proxy soak with writer enabled and disabled.

## Acceptance criteria

### Bounded memory and snapshot cost

- [ ] Every diagnostic sample structure has an explicit maximum size.
- [ ] One million batches do not increase retained sample count beyond configuration.
- [ ] RSS reaches a plateau after warm-up within the test tolerance.
- [ ] Snapshot p95 at one million batches is within 10% or a documented fixed margin of snapshot p95 at ten thousand batches.
- [ ] Snapshot work holds locks only for bounded copy/update operations.
- [ ] Metric cardinality remains bounded during provider/model churn and rehash.

### Metric correctness

- [ ] Queue wait, queue age, batch formation wait, transaction time, result delivery, and end-to-end latency are distinct.
- [ ] Transaction duration is sampled once per batch.
- [ ] Batch size is sampled once per batch.
- [ ] Failed batches and failed intents are counted separately.
- [ ] Cancellation stage counters are exact.
- [ ] Units and field names are documented and test-pinned.

### Writer correctness

- [ ] Writer has one owner loop and one drain task.
- [ ] Cross-thread/loop submission, if supported, mutates the queue on the owner loop.
- [ ] Every submitted future resolves exactly once.
- [ ] Shutdown leaves no queued unresolved intent.
- [ ] Saturation never drops a correctness-critical intent silently.
- [ ] Database recovery interaction follows Plan 027.
- [ ] Runtime publication and compensation remain consistent with durable outcomes.

### Batching performance

- [ ] Low-pressure serial path adds no artificial wait above the configured low-pressure bound.
- [ ] High-pressure bursts reduce transaction count relative to per-request persistence.
- [ ] Queue age p95 remains within configured bounds.
- [ ] Dispatch local-pre-upstream p50/p95 is no worse than the documented baseline noise threshold.
- [ ] Throughput improvement is demonstrated with exact profile/configuration.
- [ ] Writer disabled path remains unchanged.

### Instrumentation overhead

- [ ] Fine-grained sampling is request-coherent.
- [ ] Production default is less than full sampling unless measurements prove full capture negligible.
- [ ] Coarse metrics remain always on and bounded.
- [ ] Sampled/unsampled counts are exposed.
- [ ] Detailed instrumentation overhead is quantified at 0%, default, and 100% sampling.
- [ ] No prompt, response, API key, request ID series, or raw error text is retained.

### Verification

- [ ] Plans 023 and 028 focused performance baselines remain reproducible.
- [ ] Plan 029 focused tests pass on Python 3.11 and 3.12.
- [ ] Performance and soak markers pass under documented profiles.
- [ ] Standard non-slow suite passes.
- [ ] Ruff format, Ruff check, Pyright, and xfail/skip audit pass.

## Closure evidence

Commit an exact-head artifact containing sample-structure bounds, one-million-batch RSS and snapshot-latency results, writer-enabled/disabled latency-throughput comparisons, exact sample-count validation, instrumentation overhead at three sampling rates, queue saturation behavior, and rehash/cardinality plateau evidence. Update this plan to completed only after the evidence references the verified implementation tree.
