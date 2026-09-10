# Q009 — Sustained Failure, Reload, Streaming, and Resource-Stability Qualification

Status: accepted after Q008 re-acceptance; closed 2026-09-10 (see [closure](../../closure/qualification/009-status.md))

Source roadmap: `migration-rs/subsystems/qualification-roadmap.md`

Repository baseline: planning baseline `00dd27fa103e3c663968ecd95d9289c60fca0601`; implement against current main after accepted Q008.

Primary class: invariant/polish

Hard dependency: accepted Q008 after Q011 corrective live-provider closure.

## Objective

Exercise the integrated Rust process long enough and across enough ownership transitions to reveal leaks, deadlocks, stranded durable state, replay defects, or monotonic resource growth that short unit/integration tests may miss. The workload remains deterministic and local; this is a stability qualification, not a throughput benchmark.

## Test topology

Use:

- isolated config/DB/runtime roots;
- deterministic loopback providers representing at least two accounts and multiple wire profiles;
- deterministic local proxy path for selected cells;
- finite and streaming client drivers;
- reviewed fault hooks/fixture servers for connect failure, disconnect, timeout, 408/429/5xx, deterministic wire rejection, malformed/partial stream, and client cancellation;
- real M8 background supervisor with bounded intervals adjusted through test configuration where allowed;
- O006/O007/O008 business callbacks through safe local fixtures.

No paid/live provider is used in Q009.

## Workload phases

Define a bounded, reproducible scenario rather than open-ended load.

### Phase 1 — warmup

- start server and reconcile startup;
- load deterministic catalog/accounts;
- run small finite/stream mix;
- wait for connection pools/allocators/task loops to stabilize;
- capture baseline resource/ownership snapshot.

### Phase 2 — steady mixed requests

Run low/moderate concurrency across:

- Chat, Responses, Messages;
- finite and streaming;
- direct and selected proxy path;
- concrete and virtual model-router request where deterministic;
- multiple accounts with normal fair selection.

### Phase 3 — bounded upstream/client faults

Inject reviewed classes without random fuzzing:

- transport connect/read/write failure;
- 408/429/5xx pre-handoff;
- deterministic alternate-wire rejection/success;
- explicit model absence fixture;
- client cancellation before response start;
- client disconnect after downstream start;
- malformed/partial SSE/EOF;
- provider disconnect midstream.

Assert retry/no-replay/finalization rules and convergence.

### Phase 4 — reload/background churn

While ordinary traffic continues at low rate:

- apply repeated accepted live rehashes across routing/provider/wire/task-reloadable fields;
- include no-op and rejected restart-required reloads;
- allow catalog/retention/checkpoint/metrics/update-checker/automatic-backup safe fixture ticks;
- verify generation retirement and singleton task ownership.

### Phase 5 — restart/recovery

- gracefully restart after converged traffic;
- exercise one reviewed abrupt process termination with durable pending fixture state;
- start and run C010 reconciliation;
- verify unknown in-flight provider work is not replayed;
- run final finite/stream success set.

### Phase 6 — final convergence

Stop issuing work, wait boundedly for expected drains, then capture final ownership/resource state before graceful shutdown.

## Resource/ownership sampling

At defined intervals record bounded scalars:

- RSS/virtual memory where available;
- CPU sample;
- open fds;
- OS thread count;
- DB and WAL size;
- active requests/claims/reservations;
- request attempts by terminal/pending status;
- finalization supervisor active jobs/capacity;
- active/retiring generations and leases;
- wire cache size/flights/provider gates/negotiation state;
- task supervisor running/in-tick/tick counts;
- local control clients/listener state;
- backup/update temp/lock file leftovers where observable.

Do not retain per-request bodies or unbounded per-request logs as evidence.

## Stability analysis

No arbitrary latency/RSS SLA is introduced. Closure analysis must distinguish:

- expected one-time warmup/high-water allocation;
- bounded cache growth up to configured capacity;
- allocator RSS that remains resident but corresponds to released logical ownership;
- true monotonic logical/resource growth.

The following are blockers:

- leaked claims/reservations/finalization jobs/generations/task loops/wire flights;
- fd/thread count that grows with completed bounded cycles without returning to stable range;
- DB/WAL growth inconsistent with configured retention/checkpoint behavior;
- deadlock/livelock/starvation;
- panic/crash from client/provider input;
- transparent replay after downstream handoff;
- restart requiring DB reset/manual state surgery;
- backup/update temporary/lock state preventing later valid operation after failures.

## Duration/repetition

Q001/Q009 implementation should choose a practical fixed qualification duration based on local execution cost, generally long enough for multiple background/reload/restart cycles rather than hours of high load. Record the chosen duration and cycle counts in closure.

A separate longer unattended soak may be recorded as supplemental evidence but is not required unless shorter qualification exposes time-dependent behavior.

## Harness

Add a deterministic stability runner that:

- takes workload seed/config but defaults to a fixed seed;
- exposes phase/cycle counts explicitly;
- fails fast on invariant violations while still attempting safe cleanup/evidence flush;
- writes bounded JSON resource/ownership samples;
- never requires root or live provider credentials;
- can run on development Linux/macOS and optionally the Q008 SBC at reduced workload for comparison.

## Required tests

Offline tests must prove:

- every phase can be independently reproduced;
- resource sampler failure does not alter server semantics and is reported as evidence infrastructure failure;
- assertion of leaked logical ownership fails the run;
- malformed provider/client fixtures cannot crash the harness/server process unexpectedly;
- evidence samples are bounded and secret-free;
- cleanup removes temporary roots/processes even after a phase failure.

## Verification

Run affected suites plus the Q009 stability command:

```text
cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1
uv run pytest tests/migration_rs -q --tb=short --maxfail=1
# Q002 aggregate deterministic runner
# Q009 stability runner with frozen phase/cycle configuration
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
git diff --check
```

After any defect fix, add a focused deterministic regression outside the long-run harness where practical.

## Non-goals

Q009 does not benchmark maximum RPS, stress paid providers, create distributed load generators, add production observability infrastructure, or tune performance without a demonstrated issue.

## Closure evidence

Write `migration-rs/closure/qualification/009-status.md` with:

- topology/seed/duration/cycle counts;
- phase result matrix;
- before/warm/final resource/ownership samples;
- DB/WAL/background-task convergence;
- restart/recovery/no-replay evidence;
- defects found and focused regressions;
- any supplemental SBC run;
- unresolved findings and registry transition.

## Acceptance criteria

Q009 closes only when the reviewed sustained workload converges without logical ownership leaks, durable corruption, replay, deadlock/crash, or unexplained monotonic resource growth, and no unresolved high/medium stability/resource finding remains.

Accepted Q009 promotes only Q010.
