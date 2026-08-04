# Plan 075 — SBC Resource Profile and Verification Consolidation

Date: 2026-08-04
Status: ready for implementation
Parent roadmap: `plans/070-failure-resilience-router-recovery-and-sbc-simplification-roadmap.md`
Depends on: `plans/074-restart-safe-runtime-and-database-simplification.md`
Planning baseline: `e73db213e7e381043cda3cfb8a3dd8109f3f39ca`

## Purpose

Make EggPool's default documented SBC deployment materially quieter and smaller without reducing routing, retry, accounting, streaming, rehash, dashboard, or provider compatibility features.

This phase follows correctness and ownership simplification so it optimizes the retained architecture rather than preserving components that earlier phases remove.

The work must reduce idle wakeups, SQLite writes, sockets, background queues, retained diagnostic samples, and mandatory dependencies while preserving the existing small CI/release posture.

## Confirmed findings

### 1. The default operational profile is not the minimum-footprint profile

Current defaults or ordinary generation setup enable/provision several facilities:

- two SQLite worker connections;
- writable readiness probe every 10 seconds;
- sampled routing trace writer;
- detailed dispatch spans;
- dashboard and request/event retention;
- model-info/background catalog work;
- event-loop lag and cadence diagnostics;
- multiple minute/hour/day periodic tasks;
- high provider HTTP connection ceilings;
- mandatory `pproxy` dependency even when no account proxy is configured.

Each item is individually bounded, but together they create unnecessary activity on a small Raspberry Pi-class host.

### 2. Some disabled features still instantiate process-owned machinery

Routing trace and related observability objects can be created even when effective mode is off. Timer loops may wake only to discover there is no work.

A disabled optional feature should not own a task, writer, queue, connection, or periodic wakeup.

### 3. Periodic tasks duplicate natural event boundaries

Some work can be triggered by startup, catalog refresh, rehash, successful request, or daily maintenance rather than independent minute/hour timers.

Examples to review:

- usage-window hydration/aging;
- disabled-model pruning;
- model-info backfill;
- metrics retention cleanup;
- readiness writable probes;
- stale active-request sweep, which Plan 074 removes.

### 4. HTTP connection ceilings are high for the target environment

`max_connections=100` and `max_keepalive=20` are safe generic client defaults but permit more sockets and buffers than a modest SBC deployment normally needs.

HTTPX limits are ceilings rather than immediate allocation, but lower defaults reduce worst-case pressure and better communicate intended concurrency.

### 5. Dispatch span compatibility precedence is defective

The deprecated `metrics.detailed_span_sample_rate` has a non-null default and can override the newer `[metrics.dispatch_spans].sample_rate`. An operator can set the new value to zero and still receive the deprecated default behavior.

### 6. Packaging can resolve untested future dependency majors

The lock file protects repository development, but published package lower/upper bounds should avoid silently selecting a future incompatible major release. Bounds should remain conservative and not require a version-matrix CI job.

### 7. CI is already appropriately small

The existing mandatory gate is one Ubuntu/Python 3.11 job with formatting, lint, type checks, and smoke tests. Release is manual with a clean-wheel check.

The desired change is test and planning consolidation, not reducing below this gate and not adding performance/failure matrices.

## Scope

Primary files:

- `config.example.toml` and a new focused SBC example;
- `src/eggpool/models/config.py`;
- application/generation factories;
- runtime/background task registration;
- routing trace, metrics, model-info, readiness, catalog, and maintenance setup;
- `src/eggpool/providers/client_pool.py`;
- proxy support imports/configuration;
- `pyproject.toml` and lock update if dependency metadata changes;
- tests and documentation tied to removed mechanisms;
- `.github/workflows/ci.yml` only to confirm it remains unchanged unless a stale reference must be deleted.

## Explicitly out of scope

- rewriting EggPool in Rust;
- switching ASGI to RSGI;
- replacing SQLite;
- removing the dashboard;
- removing live rehash;
- removing accounting or quota routing;
- reducing correctness-critical durable writes;
- adding adaptive autotuning, dynamic concurrency control, or a performance daemon;
- adding a profile framework with inheritance/merging;
- adding a benchmark CI gate;
- adding a platform/Python matrix;
- automating releases;
- deleting focused robustness regressions from Plans 071–074.

## Governing decisions

1. Provide one copyable SBC example, not a profile abstraction.
2. Correctness-critical request/attempt/reservation/finalization writes remain immediate.
3. Diagnostic/enrichment work may be sampled, buffered, event-driven, or disabled.
4. Disabled optional components are not instantiated.
5. One canonical event loop remains the supported runtime.
6. Default connection ceilings should fit modest local concurrency.
7. Existing operators can opt back into richer diagnostics through configuration.
8. Optional proxy support should load only when configured.
9. Dependency bounds protect supported compatibility without creating a matrix.
10. CI remains one Python 3.11 smoke-oriented job.
11. Measurements are short local diagnostics, not acceptance percentages or retained evidence.

## Phase A — Add a simple SBC configuration example

### Required artifact

Add a file such as `config.sbc.example.toml` or an equivalently clear name. It should remain a normal valid EggPool configuration, not a generated profile.

Recommended baseline settings:

```toml
[server]
threads = 1
access_log = false

[database]
worker_threads = 1

[database.dispatch_writer]
enabled = false

[readiness_probe]
enabled = false

[routing.trace]
mode = "off"

[metrics]
write_mode = "low_wear"
flush_interval_s = 120
aggregate_only = true

[metrics.dispatch_spans]
sample_rate = 0.0

[model_info]
enabled = false
```

Use actual current configuration keys and validation rules; do not copy invalid illustrative keys. Where a feature has no current disable switch, add the smallest explicit switch only if construction cannot be skipped from another existing setting.

### Documentation

Explain the tradeoffs:

- `worker_threads=1` means dashboard reads share the data-plane connection;
- disabling writable probes makes readiness rely on startup integrity and ordinary DB lifecycle state;
- trace/span/model-info data is unavailable;
- low-wear metrics can lose the configured buffered analytics interval after abrupt power loss;
- none of these options weaken durable request/accounting correctness;
- dashboard can remain enabled with reduced enrichment.

Keep the ordinary example feature-rich enough for development. Do not silently force every existing installation into the SBC choices.

### Acceptance criteria

- the SBC example passes config parsing/check-config;
- it starts with one event loop and one SQLite worker connection;
- traces/spans/model-info/writable probe are genuinely dormant;
- routing, retries, accounting, streaming, dashboard basics, and rehash remain usable;
- no new profile loader or inheritance logic is added.

## Phase B — Construct optional components only when enabled

### Required audit

For each optional facility, verify construction, task start, queue allocation, and shutdown behavior:

- routing trace writer/guard;
- dispatch span recorder;
- model-info writer/backfill;
- metrics coalescer and detailed analytics;
- compression/synthetic-cache enrichment;
- readiness writable probe;
- catalog refresh timer when refresh is disabled;
- dashboard-only read connection when dashboard is disabled or `worker_threads=1`;
- database recovery controller after Plan 074 simplification;
- optional proxy transport/client objects.

### Required changes

1. If disabled at startup, do not create the writer/task/queue.
2. Rehash from disabled to enabled creates and registers the component transactionally.
3. Rehash from enabled to disabled drains/closes it once and removes it from active generation ownership.
4. Shutdown iterates only actually constructed components.
5. Call sites accept `None`/no-op through one simple branch; do not create no-op task objects to preserve shape.
6. Avoid global singleton construction for disabled diagnostics.
7. Preserve generation rollback: a failed rehash candidate closes any newly constructed optional component and leaves the active generation unchanged.

### Acceptance criteria

- `routing.trace.mode="off"` creates no trace queue/writer/drain task.
- span rate zero avoids per-request detailed recorder allocation/work.
- disabled model-info creates no writer/backfill task.
- disabled readiness probe creates no timer or write activity.
- rehash toggles each supported component without leak or duplicate task.
- generation rollback remains correct.

## Phase C — Reduce and fold periodic work

### Task inventory

Create one temporary implementation checklist of process-owned periodic tasks, their current interval, owner, and natural event trigger. Do not retain a runtime registry dashboard solely for this plan.

### Required reductions

Evaluate these changes:

1. Remove the mutating stale-request periodic task per Plan 074.
2. Run usage-window hydration at startup/rehash and maintain windows during ordinary updates rather than rescanning every minute.
3. Run disabled-model pruning as part of successful catalog refresh rather than an independent timer.
4. Run model-info backfill at startup/catalog refresh when enabled rather than a redundant cadence.
5. Run retention cleanup on a daily low-frequency cadence using existing metrics cleanup configuration.
6. Preserve catalog refresh only when `refresh_interval_s > 0`.
7. Preserve backup cadence only when backups are enabled; do not add per-minute backup checks if next due time is known.
8. Event-loop lag/cadence diagnostics should be disabled in the SBC example and avoid one-second wakeups when disabled.
9. Writable readiness probe remains optional. If enabled, consider a less aggressive SBC documented interval such as 60 seconds with suitable freshness; do not weaken the ordinary operator-configured behavior silently.
10. Combine tasks only when they share owner and cadence naturally. Do not create one giant scheduler abstraction.

### Acceptance criteria

- no removed feature leaves an orphan timer;
- disabled tasks have zero periodic wakeups;
- ordinary catalog and retention behavior remains correct;
- startup and rehash initialize required state before admission/commit;
- task registration code becomes smaller, not replaced by a generic scheduling framework.

## Phase D — Lower resource ceilings conservatively

### HTTP client defaults

Change default upstream limits to a modest range appropriate for SBC operation, unless a repository measurement proves another value:

- `max_connections`: 32;
- `max_keepalive`: 8;
- retain configurable values and existing validation;
- preserve provider/account-specific proxy clients only where proxy configuration requires them.

Do not pre-create one client per account when accounts share the same provider transport configuration. Continue using provider-level pooling, with account-specific clients only for distinct proxy settings or another concrete transport difference.

### Queue and sample capacities

Review defaults, but change only where idle or worst-case memory is material:

- routing trace queue is irrelevant when off;
- dispatch writer remains disabled by default;
- detailed span windows should not allocate when sample rate zero;
- finalization/cleanup capacities remain bounded correctness controls and should not be reduced merely for aesthetics;
- dashboard/metrics rolling samples should remain bounded and may use smaller SBC-example settings without changing general defaults.

### SQLite connections

- The SBC example uses one worker connection.
- The ordinary default may remain two if dashboard isolation materially benefits normal deployments.
- Do not add a connection pool.

### Acceptance criteria

- provider clients are reused by transport identity.
- default worst-case socket ceiling is reduced.
- account proxy differences still receive isolated clients.
- no correctness owner is evicted because of an arbitrary memory reduction.
- idle RSS and file-descriptor count do not increase.

## Phase E — Make proxy support optional and lazy

### Required changes

1. Inspect whether `pproxy` is required for core no-proxy operation.
2. Move it to an optional dependency group such as `proxy` if packaging supports that cleanly.
3. Avoid importing it during normal startup when no proxy listener/account proxy requires it.
4. When proxy configuration requires the dependency but it is absent:
   - `check-config` reports one clear actionable error;
   - startup fails before traffic admission;
   - no partial generation is published.
5. Preserve direct HTTP/SOCKS proxy URL support already handled by HTTPX without forcing `pproxy`, if applicable.
6. Keep installation documentation explicit:

```bash
pip install 'eggpool[proxy]'
```

Use the actual project install tool/extra syntax.

### Acceptance criteria

- a no-proxy EggPool install imports and serves without `pproxy` installed.
- configured features requiring `pproxy` fail clearly during validation.
- proxy-enabled behavior remains covered by focused existing tests.
- no dynamic package installation at runtime is added.

## Phase F — Fix observability configuration precedence

### Required changes

1. Change deprecated `metrics.detailed_span_sample_rate` to an optional field whose unset state is distinguishable from an explicit value.
2. The new `[metrics.dispatch_spans].sample_rate` is authoritative by default.
3. The deprecated field overrides only when explicitly present in parsed configuration.
4. Emit one bounded deprecation warning when the old field is explicitly used.
5. Preserve `0.0` as a valid explicit disable value for either field.
6. Remove the deprecated field in a future major release only; do not create a compatibility subsystem now.
7. Ensure a zero rate avoids constructing/using detailed span machinery per Phase B.

### Acceptance criteria

- new sample rate zero genuinely disables detailed spans.
- absence of the deprecated field cannot override the new field.
- explicit deprecated value remains compatible and warns once.
- configuration serialization/check-config remains truthful.

## Phase G — Add conservative dependency bounds

### Required changes

Review direct runtime dependencies in `pyproject.toml`.

For each dependency:

- retain a realistic tested lower bound;
- add a conservative next-major upper bound when the dependency follows meaningful major compatibility boundaries;
- avoid pinning patch versions in package metadata;
- update `uv.lock` after metadata changes;
- document any intentionally unbounded dependency with a reason.

Do not add Python-version or dependency-version CI matrices. The lock file and existing Python 3.11 smoke gate remain the repository verification baseline.

Optional dependencies should not be imported by core paths.

### Acceptance criteria

- a clean package install cannot silently resolve an obviously unsupported future major for bounded dependencies.
- the repository lock remains reproducible.
- core install excludes optional proxy machinery.
- no version-matrix CI is introduced.

## Phase H — Consolidate tests and planning ceremony

### Test consolidation

After Plans 071–074 remove duplicate mechanisms:

1. Delete tests that assert behavior of removed classifiers, recovery controllers, stale sweep, history dedupe, or parallel cleanup registries.
2. Preserve focused capability tests for:
   - classification;
   - rerouting;
   - no retry after handoff;
   - attempt-scoped effects;
   - bounded backoff/recovery;
   - startup fail-closed integrity;
   - transaction ownership;
   - durable finalization idempotency;
   - rehash component lifecycle;
   - SBC config parsing/startup.
3. Merge plan-numbered or narrowly duplicated tests into existing capability modules.
4. Prefer parameterization for equivalent status/body/exception shapes.
5. Do not retain fault permutations whose only purpose was a deleted in-process recovery state machine.
6. Keep live, performance, and long-duration checks manual.

### Planning closure

- Update Roadmap 070 and Plans 071–075 statuses only after actual focused verification.
- Record exact commands run, not evidence bundles.
- Do not create a corrective plan for trivial documentation drift found during implementation; fix it in the owning plan.
- A follow-up plan is warranted only for a concrete deferred defect outside the current phase scope.

### CI and release

Keep mandatory CI at:

- one Ubuntu runner;
- Python 3.11;
- frozen install;
- ruff format/check;
- pyright for `src/` and `scripts/`;
- smoke tests.

Keep release manual with:

- repository checks;
- build;
- clean environment wheel install/import/CLI/config smoke;
- explicit publish.

Do not add:

- coverage threshold;
- multi-Python matrix;
- OS matrix;
- performance gate;
- fault/chaos gate;
- automated publish;
- retained evidence archive;
- mandatory SBC soak.

### Acceptance criteria

- deleted runtime mechanisms have no stale tests or docs.
- capability regressions remain.
- total test apparatus is simpler after consolidation.
- CI workflow remains one job and no broader than before.
- manual release documentation remains accurate.

## Phase I — Short local resource validation

Run one compact comparison on representative hardware or a constrained local environment.

Measure without establishing merge thresholds:

- idle RSS after startup;
- idle thread count;
- idle file descriptor/socket count;
- SQLite write count or WAL growth during 10–15 minutes idle;
- periodic wakeup/task count from logs or a simple profiler;
- small mixed non-stream/stream request run;
- dashboard basic load when enabled;
- rehash disabled-to-enabled optional component transitions.

Compare ordinary config with the SBC example.

Record a short human-readable note in the owning plan closure or commit message. Do not add benchmark code, telemetry upload, or CI artifacts unless an existing script already supports the measurement and remains generally useful.

### Acceptance criteria

- the SBC example shows fewer active diagnostic tasks/writers and lower idle write activity.
- correctness smoke behavior is unchanged.
- any regression found is fixed in the owning implementation, not hidden by a threshold waiver.

## Verification

Focused checks:

```bash
uv run eggpool check-config --config config.sbc.example.toml
uv run ruff format src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
uv run pytest <affected config/factory/runtime/client/rehash tests> -q --tb=short --maxfail=1
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
```

Packaging checks after dependency metadata changes:

```bash
uv lock
uv build
# install the wheel in a clean environment and run import/CLI/check-config smoke
```

Run proxy-extra smoke separately only when proxy packaging changes. Do not add it as a new mandatory CI job unless the existing single job can cover a small import/config case without broadening into a matrix.

## Recommended implementation sequence

1. Add and validate the SBC config example.
2. gate optional component construction and rehash lifecycle.
3. remove/fold redundant periodic tasks.
4. lower HTTP connection defaults and inspect queue/sample allocation.
5. make proxy support optional/lazy.
6. fix dispatch-span precedence.
7. add conservative dependency bounds and update lock.
8. delete obsolete tests/docs and consolidate capability regressions.
9. run focused checks, smoke, build, and clean-wheel smoke.
10. run one short local resource comparison.
11. close Roadmap 070 and Plans 071–075 truthfully.

## Plan acceptance criteria

- [ ] A valid copyable SBC configuration example exists.
- [ ] The SBC example uses one event loop and one SQLite worker connection.
- [ ] Disabled traces, spans, model-info, writable probes, and other optional facilities create no task/writer/queue.
- [ ] Rehash can enable/disable supported optional components without leak or duplicate ownership.
- [ ] Redundant periodic tasks are removed or folded into natural event boundaries.
- [ ] Default upstream connection ceilings are reduced conservatively.
- [ ] Provider clients remain shared except for concrete transport/proxy differences.
- [ ] Core installation works without `pproxy`; proxy-required config fails clearly when the extra is absent.
- [ ] New dispatch span configuration is authoritative unless the deprecated field is explicitly set.
- [ ] Explicit zero sampling disables detailed spans.
- [ ] Direct runtime dependencies have conservative compatible bounds and the lock is updated.
- [ ] Obsolete mechanism tests/docs are deleted and capability regressions remain.
- [ ] CI remains one Python 3.11 formatting/lint/type/smoke job.
- [ ] Release remains manual.
- [ ] A short local comparison confirms reduced idle machinery/write pressure without a new performance gate.
- [ ] No profile framework, adaptive tuner, RSGI/Rust rewrite, connection pool, benchmark gate, matrix, automated publish, or evidence system is introduced.

## Rejection conditions

Do not close this plan if:

- the SBC example is not accepted by check-config;
- a disabled feature still owns a background task or writer without concrete necessity;
- rehash leaks or duplicates optional components;
- resource work removes correctness-critical writes or terminal ownership;
- lower connection ceilings break configurable higher values;
- no-proxy core import still requires optional proxy machinery;
- deprecated span defaults can override an explicit new zero value;
- dependency bounds require a matrix CI expansion;
- focused robustness tests from earlier plans are deleted merely to reduce counts;
- CI becomes smaller than the current smoke correctness floor or larger through new matrices/gates;
- local measurements become mandatory percentage thresholds.

## Definition of done

Plan 075 is complete when EggPool ships a truthful low-footprint SBC example, disabled facilities are genuinely dormant, redundant timers and excess connection ceilings are reduced, optional proxy support is lazy, observability configuration precedence is corrected, dependency metadata is conservative, obsolete tests/plans are consolidated, and the existing single smoke CI plus manual release process remains the full mandatory verification surface.