# Plan 126 — Provider-Backed SBC Characterization

Date: 2026-08-14
Status: ready
Parent roadmap: `plans/122-post-audit-correctness-and-sbc-simplification-roadmap.md`
Planning baseline: `c17bb84af6d737a8408cbcce4d2746caedee36e8`
Depends on: Plans 123–125 complete or explicitly no-op
Priority: P1 evidence/operational validation
Execution target: GPT-5.6 Luna or comparable implementation model

## Purpose

Obtain one short, real provider-backed characterization of EggPool on a
representative Raspberry Pi/SBC-class deployment after the current correctness
and request-memory corrections land.

Plan 120 recorded useful deterministic ownership/resource closure, but the
available copyable SBC profile had zero providers/accounts. As a result, actual
provider request traffic, cross-protocol preparation, concurrent streams,
provider socket behavior, request-cycle RSS/CPU, and live failure-isolation were
explicitly not measured.

This plan fills that evidence gap without creating a benchmark suite, soak
framework, hardware CI, or performance SLA.

## Governing constraints

1. Use an actual representative SBC when available. Do not present workstation
   results as Raspberry Pi/SBC results.
2. Use the normal/SBC configuration and real supported provider accounts; do not
   create artificial production settings solely to improve numbers.
3. Do not commit provider credentials, API keys, account names if sensitive,
   prompts, response content, cache keys, or private network topology.
4. Use existing `eggpool runtime-status --json`, standard OS tools, and simple
   existing clients/scripts/curl only.
5. Do not add benchmark, load-test, soak, tracing, profiling-agent, metrics,
   hardware-CI, dashboard, or test infrastructure.
6. Three short repeated cycles are sufficient for obvious monotonic-growth
   detection. This is not a stability soak.
7. No hard pass/fail thresholds for RSS, CPU, local latency, socket count, WAL
   size, throughput, or TTFT.
8. Provider network latency must not be conflated with EggPool local preparation
   overhead.
9. Do not deliberately trigger provider abuse/rate-limit/credential lockout.
10. If hardware or safe credentials are unavailable, record exact unavailable
    dimensions as `not measured`; do not fabricate or extrapolate.

## Workstream A — Record target environment

Before traffic, record non-secret facts:

- board/model;
- architecture;
- kernel;
- RAM size;
- storage class when known (microSD/eMMC/USB SSD/NVMe; do not guess endurance);
- Python version;
- EggPool commit SHA;
- EggPool version;
- JSON backend (`stdlib`/`orjson`);
- config profile used;
- Granian workers/threads;
- SQLite worker count, WAL and synchronous mode;
- provider/account count without secrets;
- provider protocols exercised;
- provider connection/keepalive limits;
- enabled optional features relevant to the request path;
- whether dashboard is enabled;
- whether compression is off/observe/safe;
- whether model-info/readiness/routing traces/backups are enabled.

Do not commit the full local config if it contains provider/account identity.

## Workstream B — Establish clean stabilized baseline

Start EggPool normally and allow a short fixed stabilization window sufficient
for startup refresh/background initialization to complete.

Record once:

- RSS and VMS;
- process/thread count;
- open file descriptor count;
- open outbound socket count;
- background task count from existing runtime status if available;
- database file size;
- WAL file size;
- provider client-pool snapshot/limits if exposed;
- readiness/health result;
- operational profile log line if already emitted.

Use `ps`, `/proc`, `ss`, `lsof`, `du/stat`, and existing CLI/status only. Do not
write a collector daemon.

Avoid repeatedly querying status if those reads materially contend with the
single SQLite connection; Plan 120 already observed that diagnostic reads can
perturb the system. Prefer sparse before/during/after snapshots.

## Workstream C — Fixed representative request corpus

Use synthetic/non-sensitive prompts. Keep the corpus short and repeatable.

### Request A — small native non-streaming baseline

- client protocol matches upstream protocol;
- short text-only request;
- no compression-specific behavior;
- ordinary supported model.

Purpose: establish basic provider routing and accounting with minimal local
transformation.

### Request B — large native streaming coding-agent-shaped request

- native OpenAI-compatible upstream when available;
- large synthetic message history below configured body/model limits;
- several nested tool definitions;
- `stream=true`;
- already-correct stream usage options when supported.

Purpose: exercise original-byte/no-op behavior, request ownership, streaming,
usage observation, and buffer release without cross-protocol translation.

### Request C — large cross-protocol streaming request

- OpenAI client→Anthropic target or Anthropic client→OpenAI target actually
  supported by configured providers;
- synthetic messages and tools;
- one supported reasoning/thinking control after Plan 123;
- provider-native cache control only if verified and intentionally exercised;
- no media for the primary run.

Purpose: exercise PreparedTranscode, provider adaptation, serialization,
streaming SSE translation, and cleanup.

### Request D — optional bounded media request

Only if the selected providers/models support image/PDF translation safely and
Plan 124 changed media validation:

- one small synthetic image or PDF well below limits;
- cross-protocol path if supported.

Purpose: confirm Plan 124 did not break media translation. Do not use
multi-megabyte private files solely to maximize RSS.

### Request E — small concurrent stream set

- 2–4 simultaneous supported streams;
- use Request B/C-sized or smaller synthetic prompts;
- enough duration to observe active sockets/tasks and post-completion cleanup.

Purpose: detect obvious resource retention or pool behavior issues. This is not
throughput/load testing.

## Workstream D — Resource observations for B/C/E

For the large native and cross-protocol requests, capture where practical:

- RSS immediately before request;
- RSS during/just after local preparation/upload;
- RSS while stream is active;
- RSS after terminal finalization and short stabilization;
- CPU utilization during local preparation;
- existing EggPool local-pre-upstream/dispatch timing fields;
- open outbound sockets;
- thread count;
- DB/WAL size after completion.

For concurrent streams, record:

- active stream count;
- open sockets;
- RSS during set;
- RSS after all streams finish and stabilization;
- background/finalization task count if existing status exposes it.

Do not interpret CPython RSS retention as proof of leaked Python objects. Use
existing deterministic lifecycle tests as source of truth for reference release;
this plan only checks for obvious monotonic process-level growth.

## Workstream E — Repeat-cycle cleanup check

Repeat Requests B and C up to three times each using the same synthetic request
shape.

Look for:

- monotonically increasing active task count;
- monotonically increasing outbound socket count after stabilization;
- monotonically increasing request/finalization ownership objects exposed by
  existing diagnostics;
- uncontrolled WAL growth inconsistent with configured checkpoint/normal write
  behavior;
- obvious monotonic RSS growth paired with retained tasks/sockets/references;
- provider pool rebuilding per request instead of reusing clients.

Do not fail the plan because RSS does not return to the original baseline after
CPython allocates arenas. Correlate with owned resources before declaring a leak.

If a reproducible leak is found, stop characterization and record a bounded
follow-up defect; do not expand this plan into a general performance roadmap.

## Workstream F — Live failure-isolation spot check

Use one safe request-specific failure that will not damage provider credentials
or intentionally consume rate limits. Preferred examples:

- unsupported local capability/transcode request rejected before dispatch;
- invalid model/request shape that produces a normal bounded upstream/client
  error;
- deterministic local/mock provider error if the real provider path cannot be
  exercised safely.

Then immediately send Request A or another known-valid request through the same
running process.

Verify:

- process remains healthy/ready;
- valid request succeeds/routes normally;
- no restart/database reset is required;
- local capability/transcode failure did not suppress unrelated provider/account
  health;
- provider-specific failure, if used, remains scoped according to current
  retry/backoff rules.

Do not intentionally invalidate credentials, exhaust quota, or flood 429s.

## Workstream G — Write/wear observations for Plan 127

Record enough DB/WAL context to inform the durable-lifecycle decision:

- number of durable request/attempt/reservation mutations expected for one
  completed request from current architecture/source inspection;
- WAL size before/after the small request corpus;
- low-wear metrics mode/flush interval;
- whether diagnostic routing trace writes are disabled/sampled/all;
- whether dashboard/status reads visibly contend with the single connection.

These are contextual facts, not flash-endurance estimates. Do not extrapolate
microSD lifetime from this short run.

## Workstream H — Documentation of results

Append a concise closure section to this plan containing:

- environment record;
- which Requests A–E were measured;
- which dimensions were not measured and why;
- a compact before/during/after observation table;
- failure-isolation result;
- any obvious resource anomaly;
- exact EggPool commit/config profile;
- no secrets or request/response content.

Do not create a permanent dashboard, benchmark report, CSV pipeline, or plotting
artifact unless the existing plan file cannot reasonably hold the observations.

## No-code expectation

This plan is primarily validation. Production code changes are **not expected**.

If characterization reveals a small regression directly caused by Plans 123–125,
fix only if:

- deterministic reproduction exists;
- the change is small and within those plans' existing contracts;
- a focused regression test can be added;
- ordinary gate can be rerun.

Otherwise record the defect separately and stop.

## Verification

Before live traffic, run:

```bash
uv run eggpool --config <redacted-local-sbc-config> check-config
```

After characterization, if no code changed, no full test suite is required.
Confirm current HEAD ordinary CI/gate status from implementation records or run
the smoke/config checks locally if convenient.

If production code changed, run the full ordinary gate from Roadmap 122.

## Explicit acceptance criteria

- [ ] Representative SBC environment and exact EggPool commit are recorded
  without secrets.
- [ ] Clean stabilized idle RSS/threads/FDs/sockets/DB/WAL baseline is recorded.
- [ ] Request A native non-streaming is exercised with a real provider when
  hardware/credentials permit.
- [ ] Request B large native streaming is exercised and before/during/after
  observations are recorded when a suitable native provider exists.
- [ ] Request C large cross-protocol streaming is exercised and observations are
  recorded when suitable providers exist.
- [ ] Request D media is exercised only if relevant/supported; otherwise marked
  not measured/not applicable.
- [ ] Request E uses only 2–4 concurrent streams and does not become a load test.
- [ ] Up to three repeated B/C cycles show no obvious monotonic retained
  task/socket/resource growth, or a concrete defect is recorded.
- [ ] One safe request-specific failure followed by a valid request demonstrates
  live failure isolation, or the live dimension is explicitly unavailable and
  deterministic coverage is referenced.
- [ ] Write/WAL observations sufficient to inform Plan 127 are recorded without
  flash-endurance extrapolation.
- [ ] Provider/network latency is not presented as EggPool local overhead.
- [ ] No benchmark/soak/profiling/hardware-CI/performance-threshold infrastructure
  is added.
- [ ] Unavailable dimensions are explicitly `not measured`; no workstation
  extrapolation is presented as SBC data.
- [ ] Results are appended to this plan and the plan is marked complete; no
  separate closure plan is created.

## Rejection conditions

Reject execution if it:

- commits secrets/private prompts/provider response content;
- uses a workstation and labels the numbers Raspberry Pi/SBC results;
- creates a permanent benchmark/load/soak harness;
- establishes numerical CI gates or SLAs from one device;
- deliberately abuses provider rate limits/credentials;
- changes pool/SQLite/config defaults merely to improve the characterization;
- treats CPython retained RSS alone as proof of a memory leak;
- expands an incidental defect into broad optimization work.

## Handoff sequence

1. Read Roadmap 122, completed Plans 123–125, Plan 120 closure, `AGENTS.md`, and
   current SBC config.
2. Prepare a secret-safe provider-backed SBC config and verify it.
3. Record environment and stabilized idle baseline.
4. Run Requests A–C, optional D, then bounded E.
5. Repeat B/C up to three cycles and observe cleanup.
6. Perform one safe failure-isolation sequence.
7. Record DB/WAL/write-path context for Plan 127.
8. Append concise results/not-measured dimensions to this file.
9. Stop; do not create performance infrastructure or a closure-plan chain.
