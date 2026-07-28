# Test, CI, and Release Infrastructure Reduction

Date: 2026-07-28
Status: implementation handoff

Baseline:

- `78a536d33db136bd6297e47573190ed8be2d1b7e`

Policy relationship:

- This plan supersedes the CI partitioning, exhaustive evidence, plan-specific verification, and release-gating requirements in `plans/030-hardening-integration-soak-and-rollout-closure.md`, `plans/031-upstream-hardening-corrective-roadmap.md`, and `plans/038-exact-head-corrective-closure.md` where those requirements conflict with this plan.
- This plan does **not** supersede the product-correctness objectives of Plans 032–037. Real defects, behavioral regressions, the real Eggpool runtime harness, request-path correctness, error isolation, database recovery, and meaningful SBC runtime validation remain in scope.
- Historical plan and evidence files may remain for provenance, but they must no longer define permanent CI topology or mandatory per-commit ceremony.

## Operating model

Eggpool is a lightweight, privately deployed, LAN-oriented proxy intended primarily for Raspberry Pi and similar SBC systems. It is not a multi-tenant public SaaS, a regulated enterprise service, or a library requiring exhaustive cross-platform compatibility certification on every change.

The verification system must reflect that operating model:

1. Preserve correctness at the externally observable boundaries that matter to an operator.
2. Catch regressions in configuration, routing, transcoding, streaming, persistence, rehash, cancellation, and packaging.
3. Keep ordinary local iteration fast enough that developers actually run the checks.
4. Keep pull-request CI small, legible, and cheap.
5. Move target-specific performance and long-duration stability validation to explicit manual workflows on representative hardware.
6. Do not convert every implementation plan, closure pass, or historical defect matrix into permanent repository infrastructure.

## Objective

Reduce the test apparatus, GitHub Actions pipeline, release process, and verification documentation to the smallest system that reliably protects Eggpool's actual product behavior.

The completed state should have:

- one primary Python 3.12 CI check job;
- one narrow Python 3.11 compatibility smoke job;
- no plan-numbered CI jobs;
- no Python-version matrix for the full suite;
- no performance or soak execution on every pull request;
- no bespoke evidence-generation requirement for ordinary changes or releases;
- no CI-driven release publication;
- no tests that exist primarily to preserve a historical plan's implementation topology;
- a substantially smaller canonical behavioral suite;
- one real-runtime manual smoke/soak path for SBC-oriented validation;
- concise, accurate developer documentation containing only commands that remain supported.

## Non-goals

- Removing focused regression coverage for previously observed severe defects.
- Weakening configuration validation, secret redaction, migration safety, request-local error isolation, or protocol correctness.
- Deleting the reusable real Eggpool runtime harness created by Plan 033.
- Replacing meaningful integration tests with mocks that bypass Eggpool.
- Establishing a test-count quota or arbitrary coverage percentage.
- Adding another orchestration framework, test runner, release manager, build system, or evidence database.
- Supporting every OS, Python patch release, architecture, or deployment topology in CI.
- Treating shared GitHub-hosted runner timing as an SBC performance benchmark.
- Redesigning production request-path architecture as part of test cleanup.

## Current failure modes to remove

The implementer must begin from the current tree and verify each of these conditions before modifying files:

1. `.github/workflows/ci.yml` contains permanent jobs named after Plans 016/017, 018–021, 023, and 030.
2. Overlapping reload, runtime-manager, database, finalization, error-isolation, performance, and soak tests are executed repeatedly across jobs.
3. The full correctness suite is duplicated on Python 3.11 and 3.12 even though Eggpool's supported language baseline can be protected with a narrow 3.11 compatibility smoke.
4. `AGENTS.md` documents stale job counts and long lists of plan-specific test commands.
5. `scripts/audit_xfail_skips.py` implements a custom AST policy scanner with a line-number-sensitive allowlist.
6. Plan-numbered architecture tests use source/AST inspection to pin symbol ownership, function names, forbidden source strings, and module placement.
7. Some performance and soak tests claim Eggpool-level evidence while sending requests directly to a mock upstream.
8. Existing performance thresholds are too loose to detect material Eggpool regressions and are inappropriate on shared CI runners.
9. Existing soak thresholds can pass when resource metrics are unavailable or returned as zero.
10. `.github/workflows/release.yml` reruns the entire development suite on tags and masks arbitrary `gh release create` failures with `|| echo`.
11. Historical exact-head evidence, raw artifacts, and closure rules have become mandatory process surface despite providing little value for a privately operated SBC deployment.
12. The documented "pre-commit" command requires the entire test suite and custom audits before every commit, materially discouraging normal iteration.

Do not assume this inventory is exhaustive. Record additional duplication or low-signal infrastructure in the implementation commit message, but do not create a new permanent audit artifact.

## Required implementation sequence

Implement this plan in the order below. Earlier workstreams establish the reduced policy; later workstreams consolidate and delete obsolete infrastructure under that policy.

---

## Workstream A — Establish a disposable baseline

Before deleting or renaming tests, collect enough baseline information to prove the reduction is real and to identify accidental coverage loss.

Run from a clean checkout:

```bash
uv sync --frozen --extra dev
uv run pytest --collect-only -q
uv run pytest \
  -m "not slow and not performance and not soak and not extended_soak and not live and not reload" \
  --ignore=tests/integration/reload/ \
  -q --tb=short --durations=30
uv run pytest tests/integration/reload/ tests/integration/test_rehash*.py \
  -m "not slow" -q --tb=short --durations=30
```

Inspect the current workflow and record, in the implementation commit or pull-request description only:

- number of workflow jobs;
- expanded runner executions after matrices;
- collected test count;
- test files grouped by `test_plan_*`, ordinary unit, integration, performance, soak, live, and helper categories;
- wall-clock duration of the canonical non-slow suite;
- wall-clock duration of reload/control tests;
- the 30 slowest tests;
- obvious duplicate test files or scenarios;
- tests that use source inspection rather than behavior;
- tests that bypass Eggpool while claiming proxy, performance, or soak behavior.

This baseline is disposable working information. Do **not** add another `artifacts/plan-039-*` evidence hierarchy, schema validator, checksum registry, or exact-head report.

### Workstream A acceptance criteria

- [ ] Baseline commands were run from the current tree.
- [ ] The implementer can identify the current runner-execution count and slowest mandatory tests.
- [ ] Plan-numbered, structural, duplicated, direct-upstream, and target-specific tests are categorized before deletion.
- [ ] No permanent baseline script or evidence artifact is introduced.

---

## Workstream B — Replace CI with two legible jobs

Rewrite `.github/workflows/ci.yml` rather than incrementally preserving the current partitioned structure.

### B1. Primary `check` job

Use one Ubuntu runner with Python 3.12. The job should perform:

```bash
uv sync --frozen --extra dev
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
uv run pytest \
  -m "not slow and not performance and not soak and not extended_soak and not live and not network" \
  -q --tb=short --maxfail=1
```

The final pytest command may use a small number of explicit directory exclusions if necessary to avoid accidental collection of manual suites, but marker configuration should be preferred over long path lists.

Do not split lint, format, typecheck, unit, integration, reload, or request-path verification into separate jobs. Sequential execution in one job gives a single authoritative result and avoids repeated checkout, dependency installation, and artifact handling.

Retain pull-request cancellation through `concurrency` if desired. This is small, useful infrastructure.

### B2. Narrow `compat-311` job

Use one Ubuntu runner with Python 3.11. This job must not rerun the full test suite.

It should verify only the minimum-language compatibility surface:

- locked dependency installation;
- package import;
- configuration model parsing and validation;
- database creation/migration smoke;
- one OpenAI non-stream request through the real in-process Eggpool harness;
- one representative stream or protocol-transcode smoke;
- CLI help and `check-config` invocation;
- package build or wheel import when practical without duplicating the release process.

Create or consolidate a small canonical smoke target, for example:

```bash
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
```

The smoke directory should contain a small number of behavioral scenarios. Do not copy large unit or integration files into it.

### B3. Delete obsolete CI behavior

Remove:

- all Plan 016/017, 018–021, 023, 030, or later plan-numbered jobs;
- the full Python 3.11/3.12 test matrix;
- separate reload-control job;
- separate performance job;
- separate soak/audit job;
- artifact upload steps for `.pytest_cache` and generic `*.log` files;
- retention settings and job comments that describe historical phases;
- duplicated `uv sync` executions beyond the two jobs;
- custom skip/xfail audit execution;
- any requirement to attach plan-specific exact-head evidence to CI.

GitHub's normal job log is sufficient for this project. Do not upload artifacts merely because a test job failed.

### B4. CI completion targets

The reduced workflow must satisfy:

- exactly two normal runner executions per pull request;
- no matrix expansion;
- no job name containing `plan`, `closure`, `evidence`, `soak`, or `performance`;
- primary check target of under 10 minutes on a typical GitHub-hosted runner;
- compatibility smoke target of under 4 minutes;
- clear failure attribution from the single job log.

If the primary check remains over 10 minutes after test consolidation, continue Workstreams C–F rather than adding partitions.

### Workstream B acceptance criteria

- [ ] `.github/workflows/ci.yml` has exactly `check` and `compat-311` as normal PR jobs.
- [ ] No full-suite Python matrix remains.
- [ ] No plan-specific, performance, soak, or evidence job remains.
- [ ] No generic test-log artifact upload remains.
- [ ] Both jobs pass on a pull request or pushed branch.
- [ ] Workflow documentation exactly matches the workflow file.

---

## Workstream C — Replace historical test taxonomy with product behavior

The active test suite must be organized by what Eggpool promises, not by which planning phase introduced the code.

### C1. Canonical test areas

Use a small taxonomy:

- `tests/unit/` — deterministic pure or narrowly isolated behavior;
- `tests/integration/` — actual component wiring and temporary SQLite;
- `tests/smoke/` — tiny cross-version package/runtime compatibility surface;
- `tests/perf/` — manually invoked real-runtime performance checks;
- `tests/soak/` — manually invoked real-runtime duration/resource checks;
- `tests/live/` — opt-in provider/network checks.

Do not add additional phase, milestone, closure, evidence, architecture-audit, or workstream directories.

### C2. Remove plan numbers from active test names

For every `tests/**/test_plan_*.py` file:

1. Determine the actual behavioral invariant.
2. Merge the useful cases into an existing canonical file where one exists.
3. Otherwise rename the file after the product behavior, such as:
   - `test_provider_thinking_controls.py`;
   - `test_error_isolation.py`;
   - `test_request_finalization.py`;
   - `test_database_recovery.py`;
   - `test_provider_request_pipeline.py`;
   - `test_dispatch_writer.py`;
   - `test_runtime_rehash.py`.
4. Delete tests that only prove plan completion, evidence shape, source ownership, or closure process.
5. Update internal comments and docstrings to explain the regression or invariant without referring to a plan number.

Historical plan references may remain in git history and plan files. They should not remain in active test node IDs, CI job names, markers, or developer commands.

### C3. Minimize markers

Retain only markers with an execution-policy purpose:

- `integration` if needed for focused developer runs;
- `slow`;
- `performance`;
- `soak`;
- `extended_soak` if an extended manual mode remains;
- `live`;
- `network`.

Remove markers that merely mirror directories or past phases when they do not change execution policy. `request_path`, `dashboard`, and `reload` may remain only if developers demonstrably use them and they select a coherent non-overlapping capability subset. Otherwise prefer explicit paths or `-k` queries.

### Workstream C acceptance criteria

- [ ] No active test filename begins with `test_plan_`.
- [ ] No pytest marker exists solely for a completed plan or workstream.
- [ ] Test names describe product behavior or a concrete regression.
- [ ] `pytest --collect-only` reports no duplicate collection caused by compatibility wrappers or copied tests.
- [ ] Historical plan files remain readable without controlling active test execution.

---

## Workstream D — Preserve a compact correctness kernel

Before deleting duplicates, create a written mapping in the implementation pull request or commit description from each critical product boundary to its surviving tests. Do not create a permanent machine-readable registry.

The mandatory suite must retain focused coverage for the following areas.

### D1. Configuration and startup

Preserve tests for:

- valid default/minimal configuration;
- invalid configuration rejection before runtime mutation;
- secret redaction in errors and diagnostics;
- fresh database creation and migrations;
- startup/readiness behavior;
- representative old configuration compatibility if migration is supported.

Avoid testing every Pydantic field through separate tests when parametrization or model-level validation covers the same contract.

### D2. Routing and provider dispatch

Preserve tests for:

- account eligibility and deterministic fallback order;
- one account/provider failure followed by a successful alternative;
- provider-specific request headers and URLs;
- unsupported thinking-control handling before dispatch;
- request-local compatibility errors that do not corrupt health, quarantine, circuit, account, or database state.

Use parametrization for provider variants rather than near-identical files.

### D3. Protocol behavior

Preserve representative behavioral cases for:

- OpenAI non-stream request/response;
- Anthropic non-stream request/response;
- OpenAI-to-Anthropic transcoding;
- Anthropic-to-OpenAI transcoding;
- streaming content deltas and final termination;
- tool-call/tool-result representation;
- usage and finish-reason propagation;
- provider error translation.

Do not preserve a Cartesian matrix when cases differ only in inconsequential payload spelling. Use equivalence classes and parametrized fixtures.

### D4. Persistence and recovery

Preserve tests for:

- successful request accounting;
- failed request accounting;
- transaction rollback on deterministic failure;
- database connection invalidation and bounded recovery;
- readiness false during uncertain database state;
- no duplicate finalization or failure-effect application;
- clean subsequent request after a recovered error.

Retain only fault-injection cases representing meaningfully different outcomes. Do not keep separate tests for every historical workstream if they exercise the same transaction boundary.

### D5. Rehash and lifecycle

Preserve a small real-runtime set for:

- valid rehash applies supported configuration changes;
- invalid rehash leaves the active runtime unchanged;
- one in-flight request remains coherent across rehash;
- shutdown drains or terminates boundedly;
- cancelled streaming requests reach a terminal state and release runtime ownership.

Hundreds of reload permutations are not required. Consolidate equivalent transition cases into parametrized state-machine tests and a few integration scenarios.

### D6. Operator surface

Preserve tests for:

- CLI help and important command parsing;
- `check-config`;
- package import/build smoke;
- representative dashboard/API endpoint rendering;
- systemd unit generation or install rendering if it remains a supported feature.

Avoid snapshotting large HTML/source documents unless exact text is part of the public contract.

### Workstream D acceptance criteria

- [ ] Every listed correctness area has at least one surviving behavioral test.
- [ ] Every previously observed severe defect has a focused regression or is covered by a clearly identified generalized invariant test.
- [ ] No critical regression is protected only by source-text inspection.
- [ ] Provider/protocol variants use parametrization where behavior is equivalent.
- [ ] The mandatory suite uses the real Eggpool harness for cross-component claims.

---

## Workstream E — Delete structural and low-signal tests

Remove or rewrite tests in the following categories.

### E1. Source-layout and AST ownership tests

Delete tests that fail merely because:

- a class or function moved modules;
- a canonical symbol was renamed;
- an implementation was factored into multiple helpers;
- a source string such as `timeout=10` exists or does not exist;
- a forbidden function name appears;
- a source file contains words such as `idempotency`, `applied`, `temporary`, or `feature_flag`;
- a module import graph differs while public behavior remains correct.

Replace only the meaningful underlying claims with behavior. Examples:

- instead of checking that `EffectsApplier` is defined in one module, apply the same effects twice and assert one durable mutation;
- instead of checking that a legacy timeout string is absent, cancel a stream and assert finalization completes after the request task exits;
- instead of checking that one router method name exists, exercise deterministic account selection and fallback;
- instead of checking that a configuration default is mentioned in source, instantiate the configuration model and assert its effective behavior.

A narrow static test may remain only where static structure itself is a safety property that cannot reasonably be exercised behaviorally. Any exception must include a concise rationale in the test.

### E2. Evidence and documentation tests

Delete tests or scripts whose purpose is to validate:

- exact-head evidence Markdown;
- historical artifact tables;
- plan status transitions;
- presence of PASS labels, SHAs, timestamps, or checksums in closure documents;
- implementation/evidence two-commit choreography;
- documentation-only diff rules;
- names of completed plan files.

These are process conventions, not product correctness.

### E3. Redundant configuration and constant tests

Consolidate tests that separately assert the same default, enum value, marker, or constant across multiple files. One model-level test and one behavioral integration test are normally sufficient.

### E4. Direct-mock pseudo-integration tests

Delete or reclassify tests that use `httpx.Client(base_url=MockUpstream)` and then claim to measure or validate:

- Eggpool request latency;
- Eggpool concurrency;
- Eggpool streaming overhead;
- Eggpool memory growth;
- Eggpool resource plateau;
- dispatch-writer behavior;
- end-to-end error isolation.

Direct mock-upstream tests may remain as unit tests of the mock helper itself, but they must not be product gates.

### E5. Meaningless wall-clock thresholds

Remove mandatory tests with shared-runner thresholds so loose that they cannot detect the target regression, such as multi-second p95 allowances for local mocked requests. Keep deterministic operation-count or bounded-queue assertions when they directly protect an algorithmic invariant.

### Workstream E acceptance criteria

- [ ] No mandatory test pins module placement or historical symbol ownership.
- [ ] No mandatory test validates closure/evidence Markdown or commit choreography.
- [ ] No performance/soak claim bypasses the Eggpool runtime.
- [ ] No CI gate depends on loose shared-runner timing thresholds.
- [ ] Removed structural tests are replaced only when they represented a real behavioral invariant.

---

## Workstream F — Remove bespoke policy and audit machinery

### F1. Replace the skip/xfail auditor

Delete `scripts/audit_xfail_skips.py` and its tests if any.

Configure standard pytest behavior in `pyproject.toml`:

```toml
[tool.pytest.ini_options]
xfail_strict = true
addopts = "--strict-markers"
```

Merge these settings with existing pytest configuration rather than adding a second table.

Conditional `skipif` for an unavailable optional dependency, unsupported platform, or explicit live-test gate is acceptable. Unconditional skips should be removed or reviewed normally; they do not justify a custom AST policy engine.

### F2. Audit other custom verification scripts

Review scripts that exist primarily to validate tests, plan evidence, source ownership, reload consistency snapshots, or closure artifacts.

For each script:

- retain it only if it is an operator-facing diagnostic or a meaningful manually invoked runtime harness;
- replace it with an ordinary behavioral pytest test if that is simpler;
- delete it if it duplicates standard tooling or historical evidence policy;
- do not replace several deleted scripts with one larger framework.

Candidate scripts include, but are not limited to:

- exact-head/evidence validators;
- plan-specific artifact validators;
- skip/xfail auditors;
- source/AST consistency auditors;
- scripts whose only caller is a historical plan command in `AGENTS.md`.

### F3. Keep standard tooling only

The default static toolchain should remain:

- Ruff formatting;
- Ruff linting;
- Pyright for maintained Python code;
- Pytest.

Do not add mypy, pylint, tox, nox, coverage gates, mutation testing, benchmark frameworks, or a second task runner as part of simplification.

### Workstream F acceptance criteria

- [ ] `scripts/audit_xfail_skips.py` and its CI/document references are removed.
- [ ] `xfail_strict` and strict marker validation are configured through pytest.
- [ ] Obsolete plan/evidence/source-audit scripts are removed.
- [ ] No new verification framework or dependency is introduced.
- [ ] Operator-facing diagnostics remain available where they serve runtime troubleshooting.

---

## Workstream G — Keep performance and soak validation manual and real

Performance and long-running stability matter for an SBC service, but shared PR CI is the wrong execution environment.

### G1. One real-runtime harness

Use the Plan 033 real Eggpool harness or an actual subprocess server with:

- temporary SQLite database;
- configured provider/account/catalog state;
- loopback mock upstream server;
- requests entering Eggpool's ASGI or listening endpoint;
- streaming, cancellation, error, and rehash capability;
- process/resource sampling that explicitly reports unavailable metrics rather than substituting zero.

Do not maintain separate mock architectures for unit, performance, and soak tests when one reusable runtime harness can serve all three.

### G2. Manual profiles

Define only these supported profiles:

1. **Smoke** — approximately 1–3 minutes; suitable for local use after request-path changes.
2. **SBC soak** — approximately 20–30 minutes on representative Raspberry Pi/SBC hardware before releases involving request-path, database, streaming, or background-task changes.
3. **Extended** — optional diagnostic mode for investigating suspected long-running leaks; not a routine release gate.

Profiles should be arguments to one runner or a small set of pytest markers, not separate plan-specific scripts and artifact schemas.

### G3. Metrics

Collect only actionable metrics:

- completed requests and errors;
- p50/p95 Eggpool-added latency or dispatch overhead;
- RSS start/peak/end when supported;
- task/thread/file-descriptor counts when supported;
- SQLite size/WAL behavior;
- finalization registry/queue depth if exposed;
- background writer queue depth if enabled;
- success of post-error and post-rehash requests.

Fail explicitly when a required metric is unavailable. Do not record unavailable as zero.

### G4. Evidence policy

Do not require committed raw JSON, JSONL, Markdown evidence, checksums, exact-head tables, or CI artifact URLs for every run.

For a normal release, a concise human release note may record:

- hardware;
- Eggpool commit/version;
- profile and duration;
- requests/errors;
- notable resource deltas;
- pass/fail.

Raw output may be attached only when diagnosing a failure or comparing a specific optimization.

### G5. Performance contracts

Mandatory CI may retain deterministic, non-wall-clock assertions such as:

- bounded JSON encode/decode operations;
- bounded queue size;
- no unbounded collection growth for a fixed deterministic sequence;
- no duplicate serialization/finalization;
- batch size or transaction-count bounds.

Wall-clock latency and resource plateau assertions belong to manual real-runtime profiles.

### Workstream G acceptance criteria

- [ ] Performance and soak tests are absent from normal PR CI.
- [ ] Every surviving product-level performance/soak test routes through Eggpool.
- [ ] One documented manual smoke command exists.
- [ ] One documented SBC soak command exists.
- [ ] Unavailable resource metrics cannot silently pass as zero.
- [ ] No new required evidence artifact hierarchy is introduced.

---

## Workstream H — Simplify release to a manual operator procedure

Eggpool should not maintain CI publishing or a second exhaustive verification pipeline for releases.

### H1. Remove automated release workflow

Delete `.github/workflows/release.yml` unless there is a narrowly documented reason to retain a manual `workflow_dispatch` build helper.

The preferred final state is no release workflow. Publishing and GitHub release creation are manual operator actions.

Do not replace the workflow with:

- OIDC trusted publishing;
- multi-platform build matrices;
- signed provenance attestations;
- generated evidence bundles;
- automated changelog enforcement;
- release-candidate branches;
- duplicate full-suite gates.

These are disproportionate for this project.

### H2. Document one release checklist

Add a concise manual checklist to the existing deployment/development documentation. It should use ordinary commands and no dedicated release framework.

Recommended sequence:

```bash
# 1. Clean checkout and dependencies
uv sync --frozen --extra dev

# 2. Run the same primary check used by CI
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
uv run pytest \
  -m "not slow and not performance and not soak and not extended_soak and not live and not network" \
  -q --tb=short --maxfail=1

# 3. Build
rm -rf dist/
uv build

# 4. Install the built wheel in a clean temporary environment and smoke it
# Use the repository's documented uv/venv approach.
eggpool --help
eggpool check-config --config <known-good-test-config>

# 5. Publish manually using the project's chosen package index command.

# 6. Create and push the version tag, then create the GitHub release manually.
```

The final documentation must specify the actual package publish command used by the maintainer. Do not embed credentials in scripts or configuration.

### H3. Release failure behavior

Do not use `|| echo`, `|| true`, or equivalent masking around build, publish, tag, or GitHub release commands. A failed manual command must remain visibly failed.

### H4. SBC validation trigger

The manual SBC soak is required only when a release materially changes:

- request coordination;
- streaming;
- transcoding hot paths;
- persistence/recovery;
- rehash/runtime lifecycle;
- background writers/tasks;
- performance-sensitive serialization.

Documentation-only, dashboard styling, metadata text, or narrow CLI-help changes do not require a soak.

### Workstream H acceptance criteria

- [ ] Tag-triggered automated release workflow is removed.
- [ ] Release publication is explicitly manual.
- [ ] The release checklist reuses the primary CI check instead of inventing a second gate.
- [ ] A built-wheel clean-environment smoke is documented.
- [ ] No release command masks arbitrary failure.
- [ ] SBC soak applicability is risk-based, not mandatory for every release.

---

## Workstream I — Rewrite developer documentation around fast iteration

Update `AGENTS.md` and any development documentation so that supported commands match the reduced system exactly.

### I1. Default local edit loop

Document a fast focused loop:

```bash
uv run ruff format <changed paths>
uv run ruff check <changed paths>
uv run pytest <affected test paths> -q --tb=short --maxfail=1
```

Do not require the full repository suite before every commit.

### I2. Before-push check

Document one canonical before-push command matching the Python 3.12 CI job. A simple shell snippet is sufficient; do not add a task framework solely to shorten the command.

### I3. Focused capability commands

Keep only a few durable examples:

- one unit test file;
- one integration test file;
- smoke suite;
- optional manual performance smoke;
- optional SBC soak;
- live tests.

Remove:

- all Plan 016–039 focused command blocks;
- workstream-specific commands;
- exact-head evidence commands;
- artifact validation commands;
- stale CI job tables;
- claims that every commit requires the full suite;
- duplicate commands already documented elsewhere.

### I4. Planning policy

Add a short rule to planning/developer guidance:

> Completed implementation plans must not create permanent CI jobs, markers, evidence formats, or plan-numbered test suites. Regression tests must be merged into capability-based suites before a plan is closed.

This rule is important to prevent recurrence.

### Workstream I acceptance criteria

- [ ] `AGENTS.md` describes exactly two CI jobs.
- [ ] Default local workflow is focused and fast.
- [ ] Full canonical verification is described as before-push/release work, not per-edit work.
- [ ] No active documentation lists plan-specific test commands.
- [ ] Planning guidance prevents new permanent plan-numbered verification infrastructure.

---

## Workstream J — Validate the reduced system without rebuilding ceremony

Run the reduced gates on the final implementation tree.

Required commands:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
uv run pytest \
  -m "not slow and not performance and not soak and not extended_soak and not live and not network" \
  -q --tb=short --maxfail=1
```

Run the Python 3.11 smoke suite in a Python 3.11 environment:

```bash
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
```

Run one manual real-runtime smoke profile through Eggpool.

Inspect the final tree to confirm:

```bash
find tests -type f -name 'test_plan_*.py'
grep -R "plan-[0-9]\|Plan [0-9]" .github/workflows AGENTS.md pyproject.toml
```

The first command must return no active test files. The second may return historical explanatory text only if it is not an execution command or CI identifier; preferably it should return nothing in active workflow/developer configuration.

Do not create an exact-head evidence artifact. Record final test counts, durations, CI run link, and significant deletions in the implementation commit or pull-request description.

### Workstream J acceptance criteria

- [ ] Primary Python 3.12 check passes.
- [ ] Python 3.11 smoke passes.
- [ ] One real-runtime manual smoke passes.
- [ ] Reduced CI passes with two runner executions.
- [ ] No active plan-numbered test file or CI job remains.
- [ ] Release workflow is absent or reduced to an explicitly justified manual helper.
- [ ] Documentation matches actual commands and files.
- [ ] No closure/evidence artifact was required to prove completion.

---

## Suggested commit sequence

Use a small number of reviewable commits. Do not create one commit per test file or historical plan.

### Commit 1 — Policy and CI reduction

- Rewrite `.github/workflows/ci.yml` to `check` and `compat-311`.
- Add or consolidate `tests/smoke/`.
- Configure strict pytest markers/xfail behavior.
- Remove CI artifact upload and custom skip/xfail audit execution.

This commit should preserve existing tests initially where practical, even if many remain collected by the primary job.

### Commit 2 — Test consolidation and deletion

- Merge/rename plan-numbered behavioral tests.
- Delete structural, evidence, duplicate, direct-upstream pseudo-performance, and pseudo-soak tests.
- Remove obsolete helpers and audit scripts.
- Update markers and imports.

### Commit 3 — Manual runtime validation and release simplification

- Consolidate real-runtime performance/soak entry points.
- Delete automated release workflow.
- Document manual release and risk-based SBC validation.

### Commit 4 — Documentation cleanup

- Rewrite `AGENTS.md` and development documentation.
- Remove historical execution commands and stale job tables.
- Add the no-permanent-plan-infrastructure rule.

Commits 3 and 4 may be combined when the diff remains clear. Do not create a separate evidence-only commit.

## Small-model execution rules

1. Do not preserve a test solely because it is large, recent, or associated with a completed plan.
2. Do not delete a test until its actual behavioral claim is identified.
3. Prefer merging and parametrizing over copying or wrapping.
4. Preserve one focused regression for each severe real defect.
5. Do not weaken a meaningful behavioral assertion merely to reduce test count.
6. Delete implementation-topology assertions when behavior is already covered.
7. Do not add a new test framework, build framework, release framework, or evidence framework.
8. Do not create another CI job to solve a slow suite; simplify the suite.
9. Do not run performance or soak on shared PR runners.
10. Do not substitute direct mock-upstream traffic for an Eggpool request path.
11. Do not represent unavailable resource metrics as zero.
12. Do not make manual release commands silently succeed after failure.
13. Keep changes limited to tests, test helpers, scripts, workflows, release/development documentation, and narrowly necessary testability fixes.
14. If consolidation exposes a product defect, add the smallest behavioral regression and production fix; do not expand into unrelated refactoring.
15. Record final counts and durations in the commit/PR description, not a new artifact subsystem.

## Global acceptance criteria

### CI shape

- [ ] Normal pull requests execute no more than two GitHub-hosted runners.
- [ ] One Python 3.12 primary check covers static analysis and the canonical behavioral suite.
- [ ] One Python 3.11 smoke covers minimum-version compatibility.
- [ ] No full-suite matrix remains.
- [ ] No plan-numbered, performance, soak, evidence, or closure job remains.
- [ ] No generic log/cache artifact upload remains.
- [ ] Primary CI completes within the stated target after consolidation.

### Test architecture

- [ ] No active `test_plan_*.py` files remain.
- [ ] No test exists only to validate historical plans, evidence Markdown, source ownership, module placement, or commit choreography.
- [ ] Critical configuration, routing, protocol, streaming, persistence, recovery, rehash, cancellation, CLI, package, and operator-surface behavior remains covered.
- [ ] Severe historical defects retain focused behavioral regressions.
- [ ] Equivalent provider/protocol/fault cases are parametrized rather than duplicated.
- [ ] Cross-component claims enter the real Eggpool runtime.

### Tooling

- [ ] Ruff, Pyright, and Pytest are the only mandatory verification tools.
- [ ] Custom skip/xfail AST audit is removed.
- [ ] Standard pytest strict-marker and strict-xfail settings replace bespoke policy.
- [ ] Obsolete plan/evidence/source-audit scripts are removed.
- [ ] No replacement orchestration framework is added.

### Performance and soak

- [ ] PR CI does not execute performance or soak suites.
- [ ] Surviving performance/soak paths exercise Eggpool itself.
- [ ] Manual smoke and SBC soak profiles are documented and practical.
- [ ] Resource metrics fail or report unavailable explicitly rather than passing as zero.
- [ ] No committed exact-head performance/soak evidence is required for ordinary release.

### Release

- [ ] Automated tag-triggered release verification/publication is removed.
- [ ] Manual release steps are concise and reuse the canonical check.
- [ ] Built artifacts are smoke-tested from a clean environment.
- [ ] Publish and GitHub release failures cannot be masked.
- [ ] SBC soak is required only for risk-relevant changes.

### Documentation and maintenance

- [ ] `AGENTS.md` and workflow files agree exactly.
- [ ] Focused local testing is the documented default development loop.
- [ ] No plan-specific commands remain in active developer documentation.
- [ ] Planning policy forbids permanent plan-numbered CI/test infrastructure.
- [ ] Completion is recorded in ordinary commit/PR metadata without a new evidence artifact.

## Explicit rejection conditions

Do not mark this plan complete if any of the following remain:

- a CI job is named after a plan, phase, workstream, closure, or evidence pass;
- the full suite runs on both Python 3.11 and 3.12;
- performance or soak tests run on every pull request;
- direct mock-upstream traffic is described as Eggpool end-to-end behavior;
- a resource test silently treats unavailable metrics as zero;
- active tests pin module placement or source strings instead of behavior;
- `scripts/audit_xfail_skips.py` or an equivalent bespoke replacement remains mandatory;
- release automation reruns the full suite or masks command failure;
- `AGENTS.md` still requires the full suite before every commit;
- active test filenames or CI commands remain plan-numbered;
- critical historical defects lose all behavioral regression coverage;
- a new artifact/evidence/verification framework is introduced to document the reduction;
- the implementer responds to slow CI by adding more CI partitions rather than reducing redundant work.

## Definition of done

This effort is complete when an ordinary Eggpool change can be developed with focused local tests, pushed through two clear CI jobs, and released manually without traversing historical plan matrices, bespoke evidence validators, shared-runner pseudo-benchmarks, or duplicated full-suite gates—while the compact canonical suite still proves the product behaviors that matter on a privately operated SBC deployment.
