# Real Runtime Soak and Resource Plateau Validation

Date: 2026-07-28
Status: implementation handoff

Parent roadmap:

- `plans/031-upstream-hardening-corrective-roadmap.md`

Depends on:

- `plans/033-real-eggpool-runtime-test-harness.md`
- `plans/034-runtime-error-isolation-finalization-recovery-matrix.md`
- `plans/035-provider-bound-request-pipeline-completion.md`
- `plans/036-real-proxy-performance-and-writer-benchmark.md`

Implementation baseline:

- completion commit of Plan 036

## Objective

Execute truthful long-running validation through the real Eggpool proxy runtime and prove that resources, ownership, diagnostic storage, database recovery, and dispatch latency plateau over time.

The prior Plan 030 soak was a short direct-mock loop described as “15–30 minute equivalent” and the evidence claimed two-hour and eight-hour results that were not executed by that test. This phase must use actual elapsed durations and actual request counts. No extrapolated or “equivalent” duration is acceptable.

## Scope

### In scope

- Real Eggpool ASGI proxy runtime from Plan 033.
- Temporary migrated SQLite database retained for the full run.
- Native, transcoded, streaming, non-streaming, transformed, validation-error, retry, cancellation, database-recovery, and rehash workloads.
- Dispatch writer disabled and enabled profiles.
- Short, standard, and extended modes.
- Periodic structured resource/state checkpoints.
- RSS, tasks, threads, file descriptors, queue/recorder bounds, finalization registry, pending durable state, database epochs/recovery, lock wait, and latency-window trends.
- Deterministic workload seed and reproducible schedule.
- Crash-safe raw evidence output.
- Narrow leak/long-running fixes directly exposed by the run.

### Out of scope

- Live provider network performance.
- Infinite/unbounded stress.
- Changing correctness policy for throughput.
- Treating the short/CI smoke mode as standard or extended evidence.
- General load-testing framework development beyond this repository's needs.
- Multi-host distributed load.
- Default-enabling the dispatch writer without a separate rollout decision.

## Required modes

Implement four explicit modes. Only three count as closure evidence.

| Mode | Minimum elapsed time | Minimum completed requests | Purpose | Closure evidence |
|---|---:|---:|---|---|
| `smoke` | 60 seconds | 500 | PR/CI wiring and fast leak sanity | no |
| `short` | 15 minutes | 5,000 | bounded local/CI scheduled run | yes |
| `standard` | 2 hours | 10,000 | production-like stability | yes |
| `extended` | 8 hours | 50,000 | long-running closure | yes |

A mode passes only when both elapsed time and request count minima are satisfied. If a run ends early, record it as incomplete, not passed.

The runner must report actual start/end timestamps, monotonic elapsed time, attempted/completed/failed/cancelled request counts, and workload counts by category.

## Workstream A — Soak runner

Add:

- `scripts/run_plan_037_soak.py`

Required arguments:

```text
--mode smoke|short|standard|extended
--profile writer-off|writer-on|both
--seed <integer>
--output-dir <path>
--checkpoint-interval-s <seconds>
--request-rate <optional target>
--concurrency <count>
--commit <sha>
```

The runner must:

1. Validate mode/profile values.
2. Create one or two isolated Plan 033 harnesses depending on profile.
3. Record environment/config at startup.
4. Warm up for a mode-specific period excluded from plateau comparison.
5. Execute a deterministic mixed workload until both duration and count criteria are met.
6. Record structured checkpoints periodically.
7. Enter a quiescent barrier before final checkpoint.
8. Close the harness and record teardown resource state.
9. Emit a machine-readable summary with pass/fail reasons.
10. Return nonzero on incomplete or failed runs.

Handle SIGINT/SIGTERM by writing a final `incomplete` checkpoint and shutting down cleanly. Do not convert interruption into a pass.

## Workstream B — Deterministic workload mix

Use a seeded schedule with approximate distribution:

- 35% native non-streaming success;
- 20% native streaming success;
- 15% transcoded success;
- 10% thinking + synthetic-cache + streaming transform combination;
- 5% local unsupported-control rejection;
- 5% retryable upstream failure followed by success on another account;
- 4% client cancellation at named lifecycle seams;
- 3% database invalidation/recovery scenarios;
- 3% accepted rehash operations.

Percentages may vary by at most one request per scheduling block. Record actual counts.

The workload must include at least:

- OpenCode Go MiniMax-M3 strict rejection;
- corrected/plain follow-up request;
- unrelated provider/model request;
- native MiniMax request;
- OpenAI and Anthropic client protocols;
- provider retry;
- stream with multiple chunks;
- cancellation after first chunk;
- database replacement/reconciliation;
- generation swap with retained finalization.

Do not inject a database fault concurrently with every request. Use deterministic fault windows and return to healthy service between cycles.

## Workstream C — Mode-specific recovery/rehash counts

Minimum successful cycles:

| Mode | Database recovery cycles | Rehash cycles | Cancellation cycles |
|---|---:|---:|---:|
| smoke | 1 | 1 | 5 |
| short | 5 | 5 | 50 |
| standard | 50 | 50 | 500 |
| extended | 200 | 200 | 2,000 |

Each recovery cycle must increment connection epoch exactly as expected and restore readiness before the next healthy request block.

Each rehash cycle must preserve listener/process identity and allow old-generation finalization to drain.

## Workstream D — Checkpoint schema

Write JSON Lines checkpoints, for example:

- `artifacts/plan-037-<mode>-<profile>-checkpoints.jsonl`

Each checkpoint must include:

### Identity/time

- schema version;
- commit/tree;
- mode/profile/seed;
- wall-clock timestamp;
- monotonic elapsed seconds;
- warm-up or measured phase;
- total attempted/completed/error/cancelled requests;
- per-workload counts.

### Latency/throughput

- interval and cumulative requests per second;
- request p50/p95/p99;
- local-pre-upstream p50/p95/p99;
- dispatch overhead p50/p95/p99;
- stream TTFT/completion p50/p95;
- finalization latency p50/p95;
- database lock-wait p50/p95;
- writer queue age/batch/transaction p50/p95 when enabled;
- snapshot collection duration.

### Resources

- RSS current and peak;
- Python allocated/traced memory if enabled without material overhead;
- asyncio task count by named category;
- thread count;
- file descriptor count;
- open SQLite/provider HTTP connections where exposed;
- GC collection counters.

### Ownership/state

- pending requests/attempts/reservations;
- router active counts;
- quota reservations;
- active health probes;
- finalization active jobs/history size;
- recovery state/attempt count/connection epoch;
- ambiguous operation count;
- writer queue depth/oldest age;
- routing/metrics writer queue depths;
- recorder sample lengths;
- metric series/cardinality count;
- quarantine entries by state, without sensitive identifiers.

### Errors

- error counts by typed category;
- unexpected exceptions;
- failed recovery/rehash/cancellation cycles;
- assertion/invariant violations since previous checkpoint.

Do not include raw API keys, authorization headers, prompts, completions, or unredacted database paths.

## Workstream E — Quiescent invariant checkpoints

At least every ten minutes for standard/extended modes, pause new workload admission and wait for a bounded quiescent state.

At quiescence assert:

- no pending request/attempt/reservation rows;
- router active counts return to zero/baseline;
- quota reservations return to zero/baseline;
- no active health probe slot remains;
- finalization supervisor active jobs is zero;
- writer queues drain to zero;
- no ambiguous database operation remains;
- database lifecycle is ready;
- readiness endpoint is healthy;
- a probe request succeeds.

If quiescence cannot be reached within the configured drain timeout, fail the run and record retained identities.

## Workstream F — Plateau calculations

Add a deterministic analyzer, either inside the runner or:

- `scripts/analyze_plan_037_soak.py`

Exclude warm-up and use equal-duration windows. For standard/extended runs, use at least:

- early measured window: first 20% after warm-up;
- middle window: middle 20%;
- late window: final 20% before teardown.

Calculate robust median/p95 values and linear slope where useful.

### Required resource gates

After warm-up:

- RSS late-window median <= early-window median + max(20 MiB, 15% of early median);
- RSS fitted positive slope <= 2 MiB/hour for standard/extended unless a documented one-time cache reaches a demonstrated bound before the late window;
- task count late median <= early median + 5;
- thread count late median <= early median + 2;
- file descriptor late median <= early median + 5;
- finalization active jobs at quiescence = 0;
- pending durable ownership at quiescence = 0;
- recorder lengths <= configured `maxlen` exactly;
- metric cardinality <= documented fixed/profile-derived bound;
- finalization terminal history <= configured bound;
- writer queue depth returns to zero at quiescence.

Platform-unavailable metrics must be labeled unavailable and cannot be silently recorded as zero. At least Linux CI/manual evidence must include RSS, tasks, threads, and descriptors.

### Required latency gates

- late-window request p95 <= max(early p95 * 1.25, early p95 + 2 ms) for deterministic local upstream;
- late-window local-pre-upstream p95 follows the same gate;
- database lock-wait p95 does not increase monotonically across all windows and late p95 <= max(early * 1.5, early + 2 ms);
- writer queue-age p95 remains within configured batching limit plus 10 ms scheduler tolerance;
- snapshot collection p95 late <= max(early * 1.25, early + 1 ms);
- throughput late median >= 90% of early median under unchanged target rate/concurrency.

If rate limiting intentionally caps throughput, compare service time and achieved target rather than raw maximum.

## Workstream G — Writer profiles

Run short, standard, and extended modes for both writer-off and writer-on unless an explicit platform limitation is documented.

Writer-on additional gates:

- transaction count < intent count;
- average batch size > 1 under concurrent blocks;
- no unbounded sample arrays;
- no queue saturation or submit timeout under target load;
- writer task survives database recovery and resumes after writes are admitted;
- no result future remains unresolved at quiescence;
- shutdown drains the writer exactly once.

Writer-off provides the control profile. Do not claim writer improvement solely from writer-on passing.

## Workstream H — Fault and cancellation discipline

Use existing event seams. Avoid randomized timing races that are impossible to reproduce.

For each injected fault/cancellation record:

- named seam/fault;
- request sequence number;
- expected outcome;
- actual outcome;
- finalization job identity hash;
- database epoch before/after where relevant;
- state-diff result.

A fault cycle is successful only when a subsequent real proxy request succeeds and quiescent invariants return to baseline.

## Workstream I — CI and manual execution

### PR CI

Run only `smoke` writer-off and a small writer-on smoke. Label the job `plan-037-soak-smoke`; do not publish it as long-running closure.

### Scheduled/manual CI

Add `workflow_dispatch` inputs for:

- mode;
- writer profile;
- seed;
- concurrency.

A scheduled standard run is recommended. Extended runs may be manual or scheduled on a dedicated runner.

Upload:

- checkpoint JSONL;
- summary JSON;
- analyzer markdown;
- sanitized application log;
- environment metadata.

Set artifact retention sufficient for closure review.

## Workstream J — Narrow leak-fix policy

When a run fails a plateau or quiescent invariant:

1. Identify the first window/checkpoint where divergence appears.
2. Correlate to named tasks, queues, registries, recorder lengths, or DB rows.
3. Add a focused deterministic regression test outside the long soak.
4. Apply one narrow fix.
5. Rerun the smoke and affected focused suites.
6. Restart the full mode from zero elapsed time; do not resume and combine partial runs.

Allowed fix areas:

- task lifecycle/shutdown;
- bounded recorder/history storage;
- writer queue/drain lifecycle;
- finalization supervisor collection;
- database recovery resource closure;
- provider client connection closure;
- rehash generation retirement;
- metric cardinality.

Do not broaden into unrelated feature work.

## Evidence artifacts

For each closure mode/profile commit:

- `artifacts/plan-037-short-writer-off-summary.json`
- `artifacts/plan-037-short-writer-on-summary.json`
- `artifacts/plan-037-standard-writer-off-summary.json`
- `artifacts/plan-037-standard-writer-on-summary.json`
- `artifacts/plan-037-extended-writer-off-summary.json`
- `artifacts/plan-037-extended-writer-on-summary.json`
- corresponding checkpoint JSONL files, or compressed CI artifacts with committed checksums/links;
- `artifacts/plan-037-soak-analysis.md`

The markdown must state actual elapsed durations and counts and must not use “equivalent.”

## Implementation sequence

1. Implement runner modes and schema.
2. Implement deterministic workload scheduler.
3. Add resource/state checkpoint collection.
4. Add quiescent barriers/invariant checks.
5. Add analyzer and plateau gates.
6. Add writer-off/on profile support.
7. Add smoke tests and CI job.
8. Run smoke until deterministic.
9. Run full short profiles.
10. Run full standard profiles.
11. Run full extended profiles.
12. Fix only evidenced leaks with focused regression tests, restarting affected runs.
13. Commit summaries/checksums and analysis.
14. Record final implementation/evidence SHAs for Plan 038.

## Focused verification commands

```bash
uv run python scripts/run_plan_037_soak.py \
  --mode smoke \
  --profile both \
  --seed 37001 \
  --concurrency 25 \
  --output-dir artifacts/plan-037-smoke \
  --commit "$(git rev-parse HEAD)"

uv run python scripts/run_plan_037_soak.py \
  --mode standard \
  --profile writer-off \
  --seed 37002 \
  --concurrency 50 \
  --output-dir artifacts/plan-037-standard-writer-off \
  --commit "$(git rev-parse HEAD)"
```

## Acceptance criteria

### Truthful execution

- [ ] Smoke, short, standard, and extended modes enforce actual time/count minima.
- [ ] Short, standard, and extended evidence records actual elapsed time and request count.
- [ ] Incomplete/interrupted runs cannot pass.
- [ ] Workload seed and actual category counts are recorded.
- [ ] No evidence uses simulated or “equivalent” duration.

### Runtime fidelity

- [ ] All workload requests enter the real Eggpool proxy.
- [ ] One migrated SQLite database persists for each entire run.
- [ ] Process-owned finalization/recovery/writers remain enabled as configured.
- [ ] Database recovery and rehash cycles occur at required counts.
- [ ] Writer-on/off profiles are both executed and distinguished.

### Plateau and ownership

- [ ] RSS, tasks, threads, descriptors meet plateau gates.
- [ ] Request/dispatch/database/snapshot latency meet late-window gates.
- [ ] Throughput remains within gate.
- [ ] Recorder/history/metric cardinality remain bounded.
- [ ] Every quiescent checkpoint has zero pending ownership and active finalization jobs.
- [ ] Writer queues/futures drain completely.
- [ ] Recovery cycles leave database ready with no ambiguous operation.

### Evidence and quality

- [ ] Raw checkpoint and summary artifacts are retained.
- [ ] Analyzer output is reproducible from raw checkpoints.
- [ ] CI smoke is clearly labeled non-closure.
- [ ] Standard and extended runs are tied to exact commit/tree and environment.
- [ ] Plans 032–036 focused suites remain green after any leak fix.
- [ ] `artifacts/plan-037-soak-analysis.md` contains no placeholder values.

## Explicit rejection conditions

Do not mark this plan complete if:

- a 250-request direct-upstream loop is described as a multi-hour soak;
- durations are extrapolated or called equivalent;
- only writer-off or only writer-on is measured without documented impossibility;
- platform-unavailable resource metrics are silently stored as zero;
- arbitrary sleeps are used to create cancellation/recovery races;
- partial runs are combined to satisfy duration;
- pending ownership is not checked at quiescent points;
- resource thresholds are widened after failure without root-cause analysis;
- raw artifacts are unavailable or not tied to the implementation tree.
