# Plan 085 — SBC Runtime Measurement and Roadmap Closure

Date: 2026-08-05
Status: ready for implementation
Parent roadmap: `plans/077-sbc-lifecycle-simplification-and-runtime-correctness-roadmap.md`
Depends on:

- `plans/078-runtime-invariant-and-request-boundary-corrections.md`
- `plans/079-quarantine-durability-and-generation-publication.md`
- `plans/080-generation-finalization-ownership-alignment.md`
- `plans/081-terminal-ownership-consolidation.md`
- `plans/082-database-fail-closed-simplification.md`
- `plans/083-lean-defaults-and-conditional-subsystem-construction.md`
- `plans/084-legacy-path-dependency-and-ci-pruning.md`

Planning baseline: `cd8967799e6613f3a5965af8cd15ce3c5269aaa8`

## Purpose

Validate that the completed simplification work improves or preserves EggPool's behavior on its intended deployment class, correct only demonstrated regressions, and close Roadmap 077 truthfully.

This is a measurement and closure plan, not a new optimization phase. It must use short manual commands and existing diagnostics/reproducers. It must not add a benchmark framework, permanent CI gate, dashboard analytics project, retained evidence schema, or long soak campaign.

## Target environment

Preferred representative target:

- Raspberry Pi 4/5 or comparable ARM64 SBC;
- 2–8 GB RAM;
- Linux with systemd;
- microSD or modest local SSD;
- Python 3.11+;
- one Granian runtime thread;
- SQLite WAL;
- lightweight/default generated configuration;
- at least two configured provider accounts if safely available for failover checks.

If representative hardware is unavailable, use the closest Linux ARM64 or constrained VM/container environment and explicitly label the limitation. Do not claim Raspberry Pi results from an unconstrained developer workstation.

## Governing decisions

1. Measure before proposing further optimization.
2. Compare the final implementation with the planning baseline or the closest preserved pre-roadmap release/config.
3. Use the same host, Python, provider/config, request corpus, and measurement method for comparisons.
4. Separate local proxy overhead from upstream network/model latency.
5. Do not place live-provider secrets or response content in retained output.
6. No numeric threshold becomes a CI gate.
7. Correctness regressions block closure even if resource use improves.
8. Small unexplained regressions must be investigated; not every noisy measurement justifies code change.
9. Any corrective patch must remain within this roadmap's architecture and avoid reopening removed compatibility paths.
10. Close only checklist items supported by code/tests/measurements.

## Workstream A — Establish comparable builds and configurations

Prepare:

1. baseline checkout/build at `cd8967799e6613f3a5965af8cd15ce3c5269aaa8` or the last release before Plan 077 implementation;
2. final checkout/build after Plans 078–084;
3. one lightweight config valid for both versions, adapting field names only where compatibility requires;
4. one full-feature diagnostic config for conditional-construction comparison;
5. a fresh database for cold-start measurements and a copied representative database for warm-start measurements.

Record:

- commit SHA;
- EggPool version;
- Python version;
- OS/kernel/architecture;
- total RAM and CPU count;
- storage type;
- config digest or redacted config summary;
- whether optional `orjson` is installed;
- whether dashboard/model-info/traces/backups are enabled.

Do not commit credentials, full environment files, or database contents.

## Workstream B — Correctness closure gate

Before measuring, run the repository gate on the final implementation:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
uv run eggpool --config config.example.toml check-config
uv run eggpool --config config.sbc.example.toml check-config
```

Run the focused suites recorded by Plans 078–084 or a curated union of their affected files.

Required manual functional checks on the target:

1. non-streaming request success;
2. streaming request success with truthful completion marker handling;
3. client cancellation/disconnect followed by continued proxy health;
4. one request-local provider validation/capability failure followed by success;
5. pre-handoff transport failover to a distinct account, if two safe accounts are available;
6. no reroute after ASGI response start;
7. rehash of a supported account/routing field while one request is active;
8. failed rehash candidate leaves current traffic operational;
9. restart after a simulated/preserved pending durable row reconciles correctly;
10. runtime-status/readiness reflect healthy and failed-closed states truthfully.

Use existing fake/local reproducers where live-provider testing is unnecessary.

Any correctness failure blocks resource closure and must be corrected before proceeding.

## Workstream C — Idle resource measurements

Measure after startup reaches readiness and a short stabilization period. Use the same fixed stabilization duration for baseline and final, such as 60 seconds; do not run a long soak.

Collect at least three samples/runs for:

- resident set size (RSS);
- virtual memory only as secondary context;
- process/thread count;
- asyncio/background task count from EggPool's bounded runtime snapshot;
- open file descriptor count;
- established/idle outbound socket count;
- idle CPU over a fixed interval;
- SQLite WAL growth and write count/size over a fixed idle window where practical;
- startup external requests by category;
- time from process start to readiness.

Suggested standard Linux tools:

```bash
/usr/bin/time -v eggpool serve --verbose --config <config>
ps -o pid,rss,vsz,nlwp,pcpu,etime,cmd -p <pid>
ls /proc/<pid>/fd | wc -l
ss -ntp
pidstat -p <pid> 1 60
```

Use alternatives if tools are unavailable. Do not add them as project dependencies.

Expected qualitative result:

- final lightweight default has fewer background tasks, clients/sockets, periodic writes, and equal or lower idle RSS than the baseline lightweight-equivalent configuration.

A small RSS increase may be accepted only when directly explained by a correctness fix and offset by reduced active/background state. Record the explanation.

## Workstream D — Request-path measurements

Measure local overhead separately from upstream latency using existing fields:

- `local_pre_upstream`;
- `dispatch_overhead`;
- upstream connect/header timings;
- TTFT where available;
- total response latency only as contextual data.

Use a small fixed request corpus:

1. native OpenAI non-streaming;
2. native OpenAI streaming;
3. Anthropic endpoint or protocol-transcoded equivalent;
4. one tool-bearing request if transcoding tools are supported/enabled;
5. one capability rejection that never dispatches upstream;
6. one pre-handoff transport-failure/failover case with a fake/local upstream if practical.

Run enough requests to reduce startup noise without creating a soak, for example 30–100 per case depending on upstream cost and local fake availability.

Report median and p95 for local proxy metrics. Do not make claims from one request.

Acceptance intent:

- no material regression in local pre-upstream or dispatch p95 without an explained correctness tradeoff;
- no reappearance of increasing dispatch overhead during a short sustained run;
- disabled instrumentation has negligible request-path work;
- stream throughput/TTFT remains dominated by upstream behavior rather than local processing.

Do not introduce hard universal millisecond thresholds because SBC class and request shape vary.

## Workstream E — Rehash peak-resource measurements

Rehash temporarily overlaps active and candidate generations. Compare baseline/final for:

- peak RSS during a supported rehash;
- transient socket/client count;
- retiring generation duration;
- terminal-reference count and convergence;
- candidate abort cleanup after an injected safe preparation failure;
- time to apply a supported rehash.

Required scenarios:

1. idle supported rehash;
2. rehash while one non-streaming or short streaming request is active;
3. failed candidate hydration/construction;
4. config with optional subsystems disabled;
5. full-feature config for comparison.

Expected result:

- disabled resources are not duplicated;
- old generation remains alive only while legitimate request/terminal references exist;
- failed candidates close all constructed resources;
- no leaked sockets/tasks remain after retirement.

## Workstream F — SQLite write/wear measurements

For a fixed small request count and fixed idle interval, compare:

- primary database size;
- WAL size before/after checkpoint;
- number of correctness rows;
- number of diagnostic/analytics rows;
- routing trace rows;
- model-info/raw observation rows;
- periodic readiness/maintenance writes;
- metrics buffer flush count.

Validate:

- correctness rows remain present and terminally accurate;
- default lightweight mode has no routing trace/model-info/readiness-probe writes;
- analytics loss window matches documented buffering;
- restart after abrupt termination does not lose correctness-critical request state;
- low-wear mode does not create unbounded in-memory buffering.

Do not simulate destructive power loss on valuable hardware without a disposable database/filesystem.

## Workstream G — Dependency/package measurements

Record baseline/final:

- production package count from the lock/environment;
- built wheel and sdist size;
- installed environment size where easy to measure;
- whether removal of `granian[pname]` changed installed packages;
- import/startup time for lightweight CLI paths such as `eggpool --help`, `check-config`, and watchdog-related commands.

These are descriptive, not gates. A dependency change is successful only if functionality and supported platform installation remain intact.

## Workstream H — Correct only demonstrated regressions

If measurement finds a regression:

1. reproduce it at least twice under the same conditions;
2. identify the responsible subsystem/commit with existing diagnostics or a narrow profiler;
3. prefer configuration/default or deletion fixes over caching/new background machinery;
4. add one focused regression test only when the defect is deterministic and testable;
5. avoid broad micro-optimization unrelated to the finding;
6. rerun the affected measurement and correctness gate.

Permitted narrow tools:

- Python `cProfile`/`py-spy` if already available externally;
- existing dispatch span diagnostics;
- `tracemalloc` in a one-off script;
- `strace`/`perf` externally where permitted.

Do not add profiler dependencies to EggPool or CI.

## Workstream I — Roadmap reconciliation and closure

Update:

- Plan 077 status/checklist;
- Plans 078–085 status and exact verification notes;
- `AGENTS.md`/architecture docs if implementation diverged from planned ownership;
- README/deployment resource guidance with measured qualitative facts;
- changelog for behavioral/config compatibility changes.

Create one concise closure section in this plan containing:

- final commit SHA;
- target environment;
- commands run;
- correctness result;
- baseline versus final table for the core measurements;
- known limitations;
- any deferred non-blocking item.

Do not create a separate evidence repository, JSON schema, benchmark dashboard, or plan series unless a real unresolved correctness blocker remains.

## Minimum closure table

Use a compact table with at least:

| Metric | Baseline | Final | Interpretation |
|---|---:|---:|---|
| Idle RSS | | | |
| Idle threads | | | |
| Idle known async tasks | | | |
| Idle outbound sockets | | | |
| SQLite/WAL growth per idle window | | | |
| Startup to readiness | | | |
| Native non-stream local pre-upstream p50/p95 | | | |
| Native stream dispatch p50/p95 | | | |
| Supported rehash peak RSS | | | |
| Wheel size | | | |
| Production dependency count | | | |

If a value cannot be collected, write `not measured` and explain why. Do not fabricate or estimate.

## Acceptance criteria

- [ ] Final lint, type, focused, smoke, and config-validation gates pass.
- [ ] Representative request, stream, cancellation, failure-isolation, rehash, and restart-repair checks pass.
- [ ] Measurement environment and method are recorded precisely enough to compare runs.
- [ ] Final lightweight runtime has fewer or equal idle tasks, sockets, and periodic writes than the baseline.
- [ ] Idle RSS is lower/equal or any increase is measured and justified.
- [ ] Local request overhead has no unexplained material p95 regression.
- [ ] Rehash does not duplicate disabled resources or leak retired-generation resources.
- [ ] Default low-wear mode preserves correctness-critical durability.
- [ ] Package/dependency changes do not break supported installation or CLI startup.
- [ ] Any demonstrated regression is corrected narrowly and remeasured.
- [ ] Plan 077 checklist/status is reconciled truthfully.
- [ ] No permanent benchmark/soak/CI infrastructure is added.

## Rejection conditions

Do not close this plan if:

- correctness checks fail but resource numbers improve;
- measurements compare different configs/hosts without disclosure;
- upstream latency is presented as local proxy overhead;
- one sample is used to claim a performance improvement;
- disabled subsystems still produce unexplained tasks/sockets/writes;
- retiring generations leak resources after references converge;
- a benchmark framework or CI gate is added for convenience;
- unmeasured values are guessed;
- roadmap boxes are checked by inference rather than evidence.

## Implementation sequence for GPT-5.6 Luna

1. Verify Plans 078–084 are complete and inspect their recorded commands.
2. Prepare comparable baseline/final builds and redacted configs.
3. Run the full correctness closure gate.
4. Collect idle measurements with at least three comparable runs.
5. Collect small request-path and rehash measurements.
6. Inspect SQLite write/wear and package/dependency changes.
7. Correct only reproducible, roadmap-scoped regressions.
8. Rerun affected correctness and measurement checks.
9. Populate the closure table and limitations.
10. Reconcile every roadmap/plan status and stop; do not open speculative follow-up work.