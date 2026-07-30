# Plan 054 — Test Suite and Verification Reduction

Date: 2026-07-30
Status: closed at 3b8976d5
Parent roadmap: `plans/045-upstream-streaming-hardening-hotpath-roadmap.md`
Applies to: Plans 045 through 053 and future corrective plans
Planning baseline: `c3915389d8167c122f5654f60c0ca9363860b48e`

## Objective

Reduce Eggpool's test, CI, runtime-validation, and closure apparatus to match the actual product: a small Python proxy intended primarily for private LAN/SBC deployment, with manual releases and no public-internet multi-tenant security posture.

The goal is not to remove correctness checks indiscriminately. The goal is to stop paying the iteration cost of duplicated tests, exhaustive matrices, long stability harnesses, evidence schemas, and per-push full-suite execution that provide little additional protection for this deployment model.

This plan is authoritative over broader testing, performance, soak, evidence, and closure requirements in Plans 045–053. Where an earlier plan asks for a larger matrix, repeated fault campaign, timing gate, retained evidence bundle, or exact-head verification ritual, this plan supersedes it.

## Current overengineering to remove

At the planning baseline:

1. Ordinary CI has a primary Python 3.12 job running format, lint, strict typing, and the entire historical non-slow suite, plus a separate Python 3.11 smoke job.
2. The pytest configuration has many historical markers for slow, performance, live, network, extended soak, request-path, dashboard, reload, integration, cache replay, baseline performance, slow-writer bursts, resource plateaus, stability assertions, workload profiles, and database consistency.
3. `AGENTS.md` describes a large runtime-validation subsystem with JSON evidence output, workload gates, p95/p99 ratios, RSS gates, quiescence polling, database audits, cleanup result reconciliation, and a real-process test for the validation runner itself.
4. `docs/releasing.md` requires the entire non-slow suite and broadly requires a 300-second SBC stability profile for request-path, streaming, database, writer, reload, concurrency, or dependency changes.
5. Plans 046–053 originally added further Cartesian protocol/policy matrices, every-seam cancellation tests, exhaustive stream fragmentation, fixed timing thresholds, long profiles, and closure evidence tables.
6. Much of this validates the validation system rather than Eggpool's user-visible behavior.

This complexity materially slows changes and encourages agents to add more fixtures, counters, fault seams, and documentation instead of correcting narrow defects.

## Target verification model

Eggpool should have four simple layers.

### Layer 1 — Changed-code checks

During iteration:

```text
ruff format/check on changed paths
pyright when typed source/scripts change
focused pytest file or test name for the behavior being changed
```

This is the default developer and agent loop.

### Layer 2 — One small CI job

Ordinary GitHub CI should use one Python version and one job:

```text
ruff format --check
ruff check
pyright
pytest tests/smoke/
```

Use Python 3.11, the minimum supported version, as the canonical CI runtime. This catches accidental use of newer syntax/APIs while avoiding a version matrix. Python 3.12 remains supported through normal local use and optional release smoke, not a second per-push job.

CI must not run:

- the full historical non-slow suite;
- live/network tests;
- performance or soak tests;
- coverage thresholds;
- benchmark comparisons;
- artifact/evidence uploads;
- package publication;
- operating-system or Python matrices.

Target ordinary CI duration: comfortably below ten minutes, preferably below five on a warm dependency cache. Runtime is a design signal, not a hard evidence artifact.

### Layer 3 — Focused manual verification

For a substantial implementation phase, run:

- the tests directly covering touched behavior;
- `tests/smoke/`;
- any one integration file whose layer interaction could invalidate the unit result.

Do not run the full suite by reflex. Run broader tests only when the changed ownership boundary actually spans them.

### Layer 4 — Manual release smoke

Before manual PyPI publication:

- run the small CI gate locally;
- build wheel/sdist;
- install the wheel in a clean temporary environment;
- prove import, CLI help, and `check-config`;
- optionally run the full remaining suite when the release contains broad refactoring and the maintainer judges it useful.

A full suite is not a mandatory release ceremony. A target-SBC run is risk-based and optional, not a universal gate.

## Correctness floor that must remain

The reduction must retain concise checks for these product-critical behaviors:

1. package import and CLI startup;
2. valid and invalid configuration parsing;
3. database creation/migration and one basic durable request lifecycle;
4. one healthy route/account selection and one fallback/error-isolation path;
5. one OpenAI request and one Anthropic request through real Eggpool wiring;
6. one successful streaming path with canonical completion;
7. one incomplete/premature stream that is not marked successful;
8. one request-local upstream validation failure followed by a healthy unrelated request;
9. one representative protocol transcode in each direction when the corresponding transcoder changes;
10. one basic rehash transaction when reload code changes;
11. bounded parser/buffer behavior for a representative malformed input;
12. manual build/install smoke before publication.

This is a behavioral floor, not a requirement that every item live in CI forever. Stable items may be covered by focused/manual tests if the smoke suite would otherwise grow.

## Test ownership rule

For each behavior, keep one authoritative test at the lowest layer that can prove it.

- Pure decision logic belongs in unit tests.
- Database/repository transitions belong in focused persistence tests.
- Cross-component request lifecycle belongs in one integration/smoke test.
- Do not repeat the same assertion at unit, integration, real-process, soak, and release layers.
- A regression test should fail for the actual defect, not for a private helper signature or evidence-schema field.
- Prefer deleting a redundant higher-layer duplicate once the lower-layer behavior and one canonical integration path are covered.

## Matrix-reduction rules

Avoid full cross-products. Use representative and pairwise coverage.

Examples:

- If streaming and non-streaming share request control adaptation, unit-test all control field shapes once and run one real request-path mode.
- If OpenAI and Anthropic endpoints call the same finalizer, do not duplicate every cancellation case across both endpoints.
- If policy modes share a table-driven adapter, cover each policy in unit tests and only one policy end-to-end unless dispatch behavior differs.
- If chunk partitioning uses one incremental buffer, cover a normal split, a terminal-marker split, and an incomplete EOF; do not split at every byte boundary.
- If Python 3.11 is the minimum supported runtime, do not run the same full suite on 3.12 in CI.
- If native and transcoded streaming use the same completion classifier, test classifier decisions once and one transcode integration path for false-terminal prevention.

## Security scope

Eggpool still needs basic defensive correctness because malformed local/provider input can crash or wedge an SBC. Retain:

- bounded line/event/body buffers;
- invalid JSON and malformed SSE do not crash the process;
- credentials are not printed or persisted;
- API authentication behavior already used by the private deployment;
- database state cannot be trivially stranded by request cancellation.

Do not build or preserve testing aimed primarily at:

- hostile public multi-tenant isolation;
- internet-scale denial-of-service resistance;
- exhaustive parser fuzzing;
- distributed attack simulations;
- penetration-test evidence;
- high-cardinality security telemetry;
- formal threat-model matrices.

No new security scanner, fuzz service, dependency audit gate, or adversarial CI job is required by this plan.

## Workstream A — Inventory the current suite

Create a temporary local inventory, not a committed evidence artifact, with:

- test file path;
- rough behavior owned;
- approximate runtime from one local run where practical;
- duplicate/overlapping tests;
- production code still exercised;
- marker usage;
- fixture/harness dependencies.

Classify each test file or coherent group:

- `KEEP-SMOKE`: small canonical CI behavior;
- `KEEP-FOCUSED`: useful when the owned module changes, but not ordinary CI;
- `COLLAPSE`: duplicate matrix that should become a smaller parameterized test;
- `DELETE`: obsolete, implementation-detail, duplicate, or validation-of-validation coverage;
- `MANUAL`: live/network/performance reproduction retained only as an explicit operator tool.

Do not commit a long inventory document. The implementation commit or handoff may summarize the major deletions.

## Workstream B — Define the canonical smoke suite

Keep `tests/smoke/` as the only ordinary CI test target. Do not create another marker-based selection language or orchestration script.

The smoke suite should remain small and readable. Prefer roughly 10–30 test functions, with parameterization only where it reduces duplication.

Required smoke behaviors after Plans 045–053 land:

- import/version/CLI help;
- minimal config parse and invalid config rejection;
- database migration/startup;
- one non-stream request through real Eggpool wiring;
- one stream with valid terminal completion;
- one upstream validation/control rejection followed by a healthy unrelated request;
- one premature EOF recorded as incomplete;
- one lightweight rehash/config transaction smoke if reload remains central.

Do not put dashboard permutations, provider catalogs, performance baselines, resource plateaus, long concurrency, or detailed protocol matrices in smoke CI.

## Workstream C — Collapse and delete redundant tests

Apply these deletion priorities:

1. tests for removed or replaced implementations;
2. tests that assert internal helper call counts without guarding a current defect;
3. duplicate protocol/mode/policy permutations over shared code;
4. repeated dashboard/rendering snapshots for equivalent components;
5. tests of diagnostic/evidence formatting not used by operators;
6. tests that validate the runtime-validation runner rather than Eggpool;
7. long historical regression matrices whose underlying implementation was simplified;
8. tests that only prove a marker, script wrapper, manifest, checksum, or artifact layout;
9. repeated fault-seam tests beyond representative ownership boundaries;
10. performance tests with unstable absolute/percentile thresholds.

When deleting tests, also delete orphaned fixtures, mock providers, test-only hooks, counters, environment variables, and documentation. Reducing file count without removing support machinery is not sufficient.

Do not delete unique behavior coverage merely to hit a numeric quota. The acceptance target is simpler ownership and faster iteration, not a vanity percentage.

## Workstream D — Remove validation-of-validation infrastructure

Review and simplify or delete:

- `scripts/run_dispatch_stability_soak.py`;
- `tests/integration/test_runtime_validation_process_smoke.py`;
- JSON result schemas and gates used only by that runner;
- p95/p99 late/early ratio logic;
- workload-shape evidence requirements;
- RSS-required gate logic;
- quiescence evidence payloads;
- offline database audit bundled into the runner;
- process cleanup result structures used only to test the runner;
- associated AGENTS/releasing documentation.

Preferred outcome:

- delete the elaborate runner if it has no routine operator use;
- retain `scripts/repro_high_concurrency_streams.py` or a similarly simple manual reproducer when it directly helps diagnose real streaming problems;
- if a runtime smoke script remains, keep it under roughly one straightforward command, print a concise summary, exit nonzero on request failure or undrained active state, and do not emit a versioned evidence schema.

No replacement framework is required.

## Workstream E — Simplify pytest markers

Reduce marker taxonomy to the smallest set still useful. A reasonable end state is no more than:

- `unit`;
- `integration`;
- `slow`;
- `live` or `network` (prefer one unless distinction is operationally useful);
- `performance`;
- `soak`.

Remove markers that exist mainly to support historical verification architecture, including where no longer needed:

- `extended_soak`;
- `request_path`;
- `dashboard`;
- `reload`;
- `cache_compression_replay_full`;
- `perf_baseline`;
- `slow_writer_burst`;
- `resource_plateau`;
- `stability_assertion`;
- `workload_profile`;
- `db_consistency`.

Tests can still be selected by path and name. Do not replace removed markers with another tagging system.

## Workstream F — Reduce GitHub Actions

Replace the two-job workflow with one job on Python 3.11.

Required steps only:

```yaml
- checkout
- setup uv / Python 3.11
- uv sync --frozen --extra dev
- ruff format --check src/ tests/ scripts/
- ruff check src/ tests/ scripts/
- pyright src/ scripts/
- pytest tests/smoke/ -q --tb=short --maxfail=1
```

Keep concurrency cancellation if useful. Remove the Python 3.12 primary full-suite job and the separate 3.11 compatibility job rather than replacing them with a matrix.

Do not add caching complexity beyond the setup action's normal behavior. Do not add test sharding, changed-file detection, reusable workflows, artifact collection, scheduled full-suite jobs, or branch-specific policy logic.

## Workstream G — Simplify development and release documentation

Update `AGENTS.md`, `README.md`, and `docs/releasing.md` so the documented default matches the reduced model.

Before-push/default development:

```text
ruff format/check
pyright when applicable
focused tests
pytest tests/smoke/
```

Manual release:

```text
small CI gate
build
clean-wheel import/CLI/check-config smoke
explicit uv publish
manual tag/release
```

Remove language implying that:

- the entire non-slow suite is always mandatory;
- 300-second SBC validation is mandatory for broad classes of changes;
- runtime-validation JSON is authoritative evidence;
- unavailable metrics must block release;
- an exact status commit must be reverified.

Optional broader testing may be described in one short paragraph, not a gate table.

## Workstream H — Apply the verification budget to Plans 046–053

Use the per-phase budgets in Plan 045. In particular:

- Plan 046: table-driven control logic plus two representative request-path cases;
- Plan 047: representative double-finalization and cancellation boundaries, not every seam;
- Plan 048: representative terminal fragmentation/EOF, not every-byte partitions;
- Plan 049: timeout versus EOF classification before any timer framework;
- Plan 050: deterministic parse/encode ownership checks, not a production telemetry system;
- Plan 051: one shared parser and representative transcode parity, not benchmark architecture selection;
- Plan 052: two confirmed hot-path removals, not broad contention profiling;
- Plan 053: focused regressions plus compact smoke, no long soak or evidence bundle.

Delete test requirements that no longer correspond to implementation code after simplification.

## Small-model execution sequence

Implement in these bounded steps:

1. Change CI to one Python 3.11 smoke job only after ensuring the current smoke directory covers import/config/DB/request basics.
2. Add only the small missing smoke cases needed for the confirmed Plans 045–053 defects.
3. Update developer/release documentation to stop requiring the full suite and soak runner.
4. Inventory tests by ownership and delete obvious obsolete/duplicate groups in small commits.
5. Remove orphaned fixtures/hooks/markers after each deletion group.
6. Simplify or delete the runtime-validation runner and its self-tests.
7. Run the reduced CI gate and focused tests for every deleted group.
8. Stop when remaining tests have clear ownership; do not continue reorganizing for aesthetic purity.

Each deletion commit should be understandable without a separate evidence document.

## Acceptance criteria

### CI and development loop

- [ ] GitHub Actions has one ordinary CI job.
- [ ] The job uses one Python version, preferably minimum-supported Python 3.11.
- [ ] CI runs format, lint, type checking, and `tests/smoke/` only.
- [ ] No full historical non-slow suite runs on every push/PR.
- [ ] No Python/OS matrix, coverage gate, performance gate, soak, live network, artifact upload, or release publication exists in ordinary CI.
- [ ] The documented local loop uses changed-path checks and focused tests.

### Smoke correctness floor

- [ ] Smoke proves import/CLI, config validation, database startup/migration, one request, and one stream.
- [ ] Smoke includes one request-local failure followed by a successful unrelated request.
- [ ] Smoke includes one premature EOF/incomplete-stream result after Plans 048–049 land.
- [ ] Smoke remains compact and finishes quickly without sleeps measured in seconds where deterministic synchronization is possible.

### Test-suite reduction

- [ ] Every remaining test group has a clear current behavior owner.
- [ ] Duplicate unit/integration/real-process/soak assertions are collapsed to one authoritative layer plus at most one canonical integration path.
- [ ] Exhaustive Cartesian matrices are replaced with representative/table-driven coverage.
- [ ] Obsolete test-only hooks, fixtures, counters, and documentation are removed with deleted tests.
- [ ] Marker taxonomy is materially smaller and no replacement tagging framework is added.
- [ ] No production code remains solely to support a historical evidence or validation harness.

### Runtime/release verification

- [ ] The elaborate runtime-validation/evidence apparatus is deleted or reduced to a simple optional manual smoke.
- [ ] `docs/releasing.md` no longer mandates the full non-slow suite or 300-second SBC soak for broad change categories.
- [ ] Manual release retains clean-wheel import/CLI/config smoke and explicit operator publication.
- [ ] No status-only closure commit, exact-head rerun ceremony, or retained evidence bundle is required.

### Plans 045–053

- [ ] Plan 054 is referenced as the authoritative verification amendment.
- [ ] Implementers follow the reduced per-phase budgets rather than the original exhaustive sections.
- [ ] No phase adds a new generic test runner, property/fuzz framework, benchmark service, fault-injection framework, or CI workflow.

## Rejection conditions

Do not close Plan 054 if:

- CI still runs the entire non-slow suite on every push;
- two jobs are retained merely to prove Python 3.11 and 3.12 separately;
- a scheduled/nightly full-suite workflow is added as compensation;
- removed matrices are replaced by another marker, manifest, registry, or generated evidence layer;
- the runtime-validation runner remains complex because tests exist to validate it;
- test counts fall but fixture/harness/support-code complexity remains;
- unique routing, persistence, stream completion, or request-isolation coverage is removed without a replacement at the correct layer;
- performance thresholds remain mandatory on shared CI;
- manual release becomes more automated or complex;
- private SBC deployment is used to justify removing basic crash, bounded-buffer, credential-redaction, or database-convergence checks.

## Handoff record

The implementation handoff should contain only:

- commit SHA(s);
- CI job before/after summary;
- main test groups deleted/collapsed;
- approximate ordinary CI runtime before/after when readily available;
- smoke test count and result;
- remaining optional manual/live/performance commands;
- any test group retained because its ownership could not safely be collapsed.

Do not create a committed audit spreadsheet, evidence JSON, checksum manifest, or status registry entry for this reduction.

## Definition of done

Plan 054 is complete when Eggpool's ordinary CI is one fast Python 3.11 format/lint/type/smoke job; the full historical suite is no longer a per-push gate; redundant matrices, markers, validation-of-validation tests, and long evidence/soak machinery are removed or demoted to simple optional manual tools; release remains manual; and the remaining tests provide a small, intelligible correctness floor for configuration, database lifecycle, routing, request isolation, streaming completion, transcoding, and reload behavior.