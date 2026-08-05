# Plan 083 — Lean Defaults and Conditional Subsystem Construction

Date: 2026-08-05
Status: complete
Parent roadmap: `plans/077-sbc-lifecycle-simplification-and-runtime-correctness-roadmap.md`
Depends on:

- `plans/081-terminal-ownership-consolidation.md`
- `plans/082-database-fail-closed-simplification.md`

Planning baseline: `cd8967799e6613f3a5965af8cd15ce3c5269aaa8`

## Purpose

Make EggPool's ordinary installation and runtime construction match its stated lightweight local/SBC deployment target.

The repository already ships `config.sbc.example.toml`, which disables optional diagnostics, model enrichment, readiness writes, routing traces, the dispatch writer, and detailed event-loop instrumentation. The ordinary defaults remain heavier, and the generation factory constructs a broad service graph before all feature-disable decisions have eliminated their runtime cost.

This plan must reduce idle tasks, sockets, external fetches, SQLite writes, and generation-swap peak memory without removing operator-accessible features. Full diagnostics and enrichment remain opt-in.

## Governing decisions

1. Lightweight local deployment is the default onboarding outcome.
2. Correctness-critical request/attempt/reservation persistence remains immediate and durable.
3. Lossy analytics may be buffered or disabled by default.
4. Disabled optional subsystems must not construct their clients, queues, repositories, callbacks, or background tasks.
5. One canonical configuration profile is generated; do not build a profile inheritance framework.
6. Existing explicit configuration continues to override defaults.
7. Dashboard availability may remain enabled, but unsafe/public exposure must be an explicit operator decision.
8. No feature is removed merely to lower idle cost; it may be lazy/conditional.
9. Do not add dynamic auto-tuning, hardware detection, or an adaptive resource manager.
10. Measure construction/task reductions locally; final target-device measurement belongs to Plan 085.

## Workstream A — Define the shipped lightweight default

### Onboarding output

Audit `eggpool onboard`, generated configuration templates, bundled `_share` examples, install script behavior, and documentation.

The default generated configuration should use the equivalent of the existing SBC-safe choices:

```toml
[server]
threads = 1
access_log = false

[database]
worker_threads = 1
wal = true
synchronous = "NORMAL"

[routing.trace]
mode = "off"
sample_rate = 0.0

[dispatch_writer]
enabled = false

[readiness_probe]
enabled = false

[model_info]
enabled = false

[metrics]
write_mode = "low_wear"
aggregate_only = true
event_loop_lag_enabled = false

[metrics.dispatch_spans]
sample_rate = 0.0
```

Use current validated field names and values. Do not duplicate defaults in several uncoordinated dictionaries; route onboarding through one canonical lightweight template/helper.

### Dashboard and bind defaults

Preserve LAN usefulness while avoiding accidental exposure.

Required decision:

- onboarding must explicitly ask whether the dashboard/API should bind to LAN (`0.0.0.0`) or loopback;
- if noninteractive installation cannot ask, default to loopback unless the current install contract explicitly promises LAN binding;
- dashboard `public=true` must not silently mean unauthenticated access on every interface when a server API key exists.

Do not add user accounts or a dashboard authentication system. Reuse the existing API-key/auth boundary.

### Optional choices

Onboarding may present a small number of clear opt-ins:

- model metadata enrichment;
- detailed routing/dispatch diagnostics;
- automatic in-process backups;
- outbound per-account proxy support.

Do not expose every low-level tuning field interactively.

## Workstream B — Construct disabled subsystems as `None`

Audit `RuntimeGenerationFactory.prepare()` and process startup. For each optional subsystem, determine whether disabled configuration still constructs any of:

- network client/manager;
- DNS backend;
- repository wrapper;
- queue/writer;
- recorder/window/deque;
- callback closure;
- task supervisor entry;
- startup refresh work;
- dashboard service dependency.

At minimum audit:

- model-info service and external sources;
- routing trace guard/writer;
- dispatch span recorder;
- local pre-upstream/detail recorders;
- compression tuning registry and compression analysis;
- request segmentation consumers;
- synthetic cache analysis;
- dispatch persistence writer;
- readiness writable probe;
- event-loop lag monitor;
- automatic backup task;
- DNS cache backend;
- update checker/background PyPI probe;
- dashboard telemetry cache.

### Construction rule

When a feature is disabled and no correctness path depends on it:

- represent it as `None` or a module-level immutable no-op only where call-site churn would be excessive;
- do not allocate queues, clients, deques, or tasks;
- do not perform startup database reads for it;
- do not register callbacks;
- do not schedule refresh/cleanup work;
- keep hot-path branches simple and precomputed per generation.

Prefer `None` plus generation-time precomputation over rich no-op objects that reproduce the same object graph.

### Model-info isolation

When `model_info.enabled=false`:

- do not construct external source clients;
- do not perform startup refresh;
- do not create periodic refresh tasks;
- do not store raw observations;
- `/api/model-info*` should return the existing disabled/not-available response, not construct the service lazily on every request;
- `/v1/models` continues using provider catalog data.

### Request shaping isolation

With shipped defaults, compression/cache shaping is reporting-only or disabled. Determine the minimum objects required for protocol correctness.

When no compression, synthetic cache, tuning, or segmentation consumer is active:

- skip request segmentation;
- skip compression analysis;
- skip tuning registry construction;
- skip related metrics windows;
- preserve native/transcoded request behavior exactly.

Do not remove the feature. Explicitly enabled configurations must construct the same functional path.

### Observability isolation

When routing traces/spans/event-loop lag are disabled:

- avoid recorder/writer construction;
- avoid deterministic sampling hashes beyond any already-required request ID;
- avoid background flush tasks;
- return bounded zero/disabled snapshots from runtime APIs.

Do not retain a queue merely so the dashboard can show zero.

## Workstream C — Reduce default SQLite write pressure

Correctness-critical writes remain unchanged.

For default lossy analytics:

- use low-wear buffering;
- aggregate request time-series into coarser buckets;
- do not persist routing traces;
- do not persist detailed spans;
- retain bounded summary data needed by the dashboard;
- ensure abrupt power loss can lose only the documented analytics interval, never request/reservation correctness state.

Audit periodic cleanup and retention defaults. Use conservative SBC values without adding a settings wizard.

Avoid a database write from `/readyz`; continue using cached state. With the writable probe disabled, readiness should reflect startup/fatal database state and optionally a less frequent maintenance validation, not perform endpoint writes.

## Workstream D — Reduce default connection/task counts

Review default HTTP pool sizing against the intended concurrency.

Preferred default target, unless existing measurements/tests show a regression:

- provider `max_connections=16`;
- provider `max_keepalive=4`;
- background outbound client `max_connections<=8`;
- background keepalive `<=2`.

Keep explicit operator values unchanged. The exact chosen values must be documented and checked against concurrent streaming smoke/reproducer behavior.

Audit all generation/process background tasks and create a startup diagnostic count by category for development/runtime status. Do not add a permanent task monitoring framework; a bounded snapshot of known supervisors is sufficient.

Target default idle tasks:

- control/supervisor necessities;
- catalog refresh only if required by provider discovery;
- metrics flush only when buffering is enabled;
- no model-info, trace, lag, backup, or readiness-probe task unless opted in.

## Workstream E — Rehash construction pressure

A rehash temporarily holds active and candidate generations. Conditional construction must apply to candidates so disabled features are not duplicated.

Required behavior:

- candidate builds only enabled generation-owned resources;
- process-owned resources are never duplicated;
- candidate abort closes only resources actually constructed;
- generation diff marks changes to disabled features without constructing them prematurely;
- restart-required fields remain restart-required;
- rehash of account/routing/model override changes does not start unnecessary external refreshes before publication.

Do not add incremental object mutation to avoid construction. A clean, smaller generation rebuild is preferred.

## Workstream F — Configuration and documentation

Update:

- `config.example.toml` to make low-cost behavior clear and keep advanced options commented/opt-in;
- `config.sbc.example.toml` if canonical defaults make entries redundant, while retaining it as an explicit copyable profile;
- bundled `_share` examples;
- onboarding documentation;
- deployment guide;
- architecture ownership/task inventory;
- runtime status documentation.

Avoid maintaining two divergent complete default files. The SBC example should be generated/validated from the same field set or explicitly tested for drift.

## Focused verification

Required cases:

1. noninteractive/default onboarding produces a valid lightweight config;
2. explicit operator opt-ins remain preserved;
3. disabled model-info constructs no service/client/task;
4. disabled routing trace constructs no queue/writer/guard;
5. zero dispatch-span sampling constructs no detailed recorder;
6. disabled readiness probe schedules no write task;
7. disabled event-loop lag schedules no monitor;
8. disabled backup schedules no backup task;
9. no shaping consumer skips segmentation/compression analysis;
10. explicit shaping enablement preserves current behavior;
11. default startup task/client counts are lower than a full-feature config;
12. candidate rehash does not duplicate disabled resources;
13. both shipped config examples pass `check-config`;
14. one representative concurrent streaming test passes with reduced pool defaults;
15. smoke passes.

Use dependency-construction spies and task supervisor snapshots, not RSS thresholds in unit tests.

Suggested commands:

```bash
uv run ruff format src/eggpool/generation_factory.py src/eggpool/app.py src/eggpool/onboard.py src/eggpool/models/config.py tests/unit tests/integration
uv run ruff check src/eggpool/generation_factory.py src/eggpool/app.py src/eggpool/onboard.py src/eggpool/models/config.py tests/unit tests/integration
uv run pyright src/eggpool/generation_factory.py src/eggpool/app.py src/eggpool/onboard.py src/eggpool/models/config.py
uv run eggpool --config config.example.toml check-config
uv run eggpool --config config.sbc.example.toml check-config
uv run pytest <affected config/factory/onboarding/task tests> -q --tb=short --maxfail=1
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
```

Adjust `onboard.py` path to the actual onboarding module after inspection.

## Acceptance criteria

- [x] Default onboarding creates a lightweight single-loop, low-write configuration.
- [x] LAN/public dashboard exposure is an explicit decision rather than an accidental default.
- [x] Disabled optional subsystems do not construct clients, queues, writers, callbacks, repositories, or tasks beyond a justified immutable no-op.
- [x] Model-info disabled mode performs no external refresh work.
- [x] Disabled request-shaping consumers skip segmentation/analysis.
- [x] Default lossy analytics write pressure is reduced while correctness persistence remains immediate.
- [x] Default connection pools are bounded for SBC use and preserve representative concurrent streaming.
- [x] Rehash candidates do not duplicate disabled resources.
- [x] Shipped configuration examples remain valid and non-divergent.
- [x] Focused tests and smoke pass.
- [x] No hardware detector, adaptive tuner, profile inheritance, or new background framework is added.

## Rejection conditions

Do not close this plan if:

- lightweight behavior remains available only through a manually discovered example;
- disabled features still create network clients or scheduled tasks;
- request correctness writes are buffered;
- dashboard exposure becomes less explicit;
- reduced pool limits break representative concurrent streams;
- configuration logic is duplicated across onboarding and examples without a shared source;
- conditional construction adds more abstraction than it removes.

## Implementation sequence for GPT-5.6 Luna

1. Inventory default config, onboarding output, process tasks, and generation resources.
2. Define one canonical lightweight generated configuration.
3. Add construction-spy tests for disabled subsystems.
4. Make factory/startup construction conditional feature by feature.
5. Remove default background writes/tasks and reduce pools.
6. Verify rehash candidate construction/abort.
7. Reconcile examples and documentation.
8. Run focused checks, config validation, and smoke.
9. Record before/after task/client counts in plan completion notes.

## Completion notes

- Ordinary and SBC examples now validate with loopback/low-wear lean defaults;
  LAN binding and optional diagnostics are explicit.
- Generation construction leaves disabled model-info, pricing, update-checker,
  tracing, shaping, and related client/recorder resources absent. Candidate
  task registration excludes process-owned schedules.
- Local verification: `ruff format --check`, `ruff check`, `pyright`, and the
  smoke suite pass; focused config/factory/onboarding/reload/startup tests and
  catalog/model-info tests also pass.
- No target-device measurements were added; those remain part of Plan 085.
