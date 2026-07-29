# Test, CI, and Release Reduction Closure

Date: 2026-07-28
Status: closed (commit d75198b9 — see Plan 043)

Parent plan:

- `plans/039-test-ci-release-infrastructure-reduction.md`

Corrective baseline:

- `f3965c97de440c1da80411c28df5d15e5cd80771`

## Purpose

Plan 039 successfully reduced ordinary pull-request CI from a large plan-partitioned matrix to two jobs and removed automated release publication. It did not fully close the underlying repository complexity.

The remaining problem is no longer CI topology. It is test truthfulness and residual verification machinery:

- many plan-numbered files were mechanically renamed without consolidating their contents;
- active test modules still contain stale Plan 0xx headings, workstream language, and commands referring to deleted filenames;
- several files labeled integration or end-to-end send requests directly to `MockUpstream` and never enter Eggpool;
- some database-recovery “E2E” tests assert that private attributes or method names exist rather than inducing and observing behavior;
- the Python 3.11 smoke suite lacks a successful `check-config` case and a representative streaming/transcoding case;
- automated release was deleted without adding a concise manual-release procedure;
- the manual soak workflow still contains multiple jobs, a Python matrix, overlapping suites, long-lived artifact bundles, and historical evidence machinery;
- the canonical test count did not materially decrease and reportedly increased after the initial reduction.

This plan closes those residual issues without reopening the CI design or creating another evidence framework.

## Governing policy

The final system is optimized for a privately operated, LAN-hosted service on Raspberry Pi and similar SBC hardware.

Correctness is defined by observable operator and request behavior, not by historical plan completion, source layout, private attribute names, evidence schemas, or shared-runner timing.

The closure must preserve these boundaries:

1. Configuration must reject invalid input safely and redact secrets.
2. Provider routing and protocol adaptation must remain correct.
3. A provider-specific request error must remain request-local.
4. Streaming cancellation and finalization must not leak durable or runtime ownership.
5. SQLite transaction uncertainty must recover or fail closed.
6. Rehash must remain atomic and bounded.
7. Packaging must produce an installable artifact.
8. The normal development loop and pull-request CI must remain small enough to run routinely.

## Non-goals

- Reintroducing plan-numbered CI jobs.
- Adding a full-suite Python matrix.
- Adding coverage percentage gates.
- Replacing pytest, Ruff, Pyright, or uv.
- Building a new test orchestration framework.
- Creating a new exact-head evidence artifact.
- Creating a permanent test-to-requirement registry.
- Running performance or soak validation on every pull request.
- Certifying public-SaaS or multi-tenant production operation.
- Preserving a test solely because it is recent, large, or associated with a completed plan.
- Opportunistically redesigning the production request pipeline.

## Required end state

The implementation is complete when:

- `.github/workflows/ci.yml` still contains only `check` and `compat-311`;
- active tests contain no stale plan-numbered filenames or executable commands;
- no integration/E2E test bypasses Eggpool while claiming Eggpool behavior;
- fake topology assertions are deleted or replaced by behavioral assertions;
- the Python 3.11 smoke suite covers import, valid config validation, database migration, one non-stream request, and one streaming or cross-protocol request;
- manual release is documented with build and clean-wheel smoke steps;
- manual runtime validation has one simple local command and, at most, one simple manually dispatched workflow job;
- duplicate and dead test modules are removed rather than merely renamed;
- the canonical suite is smaller than the corrective baseline and no slower because of this closure;
- no new evidence, audit, report, manifest, checksum, or workflow framework is introduced.

---

## Workstream A — Establish a truthful disposable baseline

Before editing tests, measure the current system from a clean checkout at the corrective baseline or the current implementation head if `main` has advanced.

Run:

```bash
uv sync --frozen --extra dev
uv run pytest --collect-only -q
uv run pytest \
  -m "not slow and not performance and not soak and not extended_soak and not live and not network" \
  -q --tb=short --maxfail=1 --durations=40
uv run pytest tests/smoke/ -q --tb=short
```

Also run disposable inventory commands:

```bash
find tests -type f -name 'test_plan_*.py' -print
grep -RInE 'Plan [0-9]{3}|test_plan_[0-9]{3}|Workstream [A-Z]' tests

grep -RInE 'httpx\.(Client|AsyncClient)\(base_url=UPSTREAM_BASE' \
  tests/integration tests/contract tests/smoke

grep -RInE 'hasattr\(|__new__\(' tests/integration
```

Record only in the implementation commit or pull-request description:

- collected test count;
- canonical-suite duration;
- smoke-suite count and duration;
- every active file containing stale plan/workstream text;
- every integration/E2E file sending requests directly to a mock upstream;
- every integration/E2E test that asserts private attributes, symbol existence, or method names;
- empty test modules and modules collecting zero tests;
- the 40 slowest canonical tests;
- the largest parametrized families by collected node count.

Do not commit this inventory as an artifact. It is implementation working material.

### Workstream A acceptance criteria

- [ ] Baseline commands run successfully or failures are identified before cleanup.
- [ ] Current test count and duration are known.
- [ ] Every false-E2E candidate is listed before deletion or replacement.
- [ ] Empty and zero-collection modules are identified.
- [ ] No baseline artifact, schema, or validator is added.

---

## Workstream B — Create one minimal reusable real-runtime test fixture

Several active tests make cross-component claims but lack a reusable path through the Eggpool application. Consolidate the existing smoke setup into one small fixture instead of copying more setup code.

Preferred location:

- `tests/helpers/runtime_app.py`, or
- a narrowly scoped fixture module under `tests/support/` if that convention already exists.

Do not create a class hierarchy or plugin package. One async context manager or fixture factory is sufficient.

The helper must be able to provide:

- a temporary file-backed SQLite database by default;
- migrations applied through the production migration runner;
- a minimal valid `AppConfig`;
- one or more configured provider/accounts;
- an actual Eggpool ASGI application;
- an `httpx.AsyncClient` using `ASGITransport` to enter Eggpool endpoints;
- upstream HTTP interception through `respx` or the existing mock-upstream helper;
- deterministic catalog/account support setup;
- clean shutdown of clients and database resources;
- direct access to only those process services needed for behavioral assertions, such as the database, registry, health manager, or runtime manager.

The helper must not:

- duplicate the entire production lifespan manually when `create_app` or an existing factory can own it;
- mutate unrelated global state;
- expose dozens of private internals;
- include plan numbers in names or docstrings;
- create evidence files;
- add sleeps as ordering control when deterministic events or fault seams exist;
- become a second application-construction architecture.

Reuse this helper in `tests/smoke/test_request_smoke.py` and the focused integration regressions added in Workstreams C and D.

If the existing smoke fixture can be moved without increasing complexity, move it. If a production lifespan entry is currently impractical, retain the smallest explicit setup and document the missing ownership boundary in code comments without introducing a large abstraction.

### Workstream B acceptance criteria

- [ ] One reusable helper enters the actual Eggpool ASGI endpoint.
- [ ] The helper uses migrated temporary SQLite state.
- [ ] Upstream traffic is mocked after entering Eggpool, not instead of entering Eggpool.
- [ ] The smoke suite and integration regressions reuse the helper.
- [ ] No second test framework or large fixture hierarchy is introduced.
- [ ] Resource cleanup is deterministic and leaves no unclosed-client warnings.

---

## Workstream C — Remove stale plan-era content from active tests

Mechanical renaming is incomplete while active files still describe themselves as Plan 0xx closure evidence or contain commands using deleted filenames.

For all active test files:

1. Replace plan/workstream headings with the actual behavioral invariant.
2. Delete historical implementation narratives that do not help understand the test.
3. Replace commands referring to `test_plan_*.py` with the current filename, or remove self-run commands entirely when they add no value.
4. Rename classes and test IDs that expose plan numbers.
5. Remove comments such as “verified by Plan 027 unit tests” when the current test does not verify the stated behavior.
6. Delete empty or zero-collection modules.
7. Keep historical provenance in `plans/` and git history, not in active test execution surfaces.

Known required targets include, but are not limited to:

- `tests/integration/test_canonical_e2e_scenario.py`;
- `tests/integration/test_failure_effects_e2e.py`;
- `tests/integration/test_database_fault_recovery_e2e.py`;
- all mechanically renamed Plan 016–032 unit and reload files;
- docstrings containing commands with deleted `test_plan_*` paths.

For `test_canonical_e2e_scenario.py` specifically:

- if it still contains helpers but collects no tests, delete it;
- if a unique canonical request-local error regression is missing elsewhere, move only that useful scenario into a real-runtime behavioral file and delete the empty shell;
- do not retain the file merely as documentation.

Run after cleanup:

```bash
find tests -type f -name 'test_plan_*.py' -print
grep -RInE 'Plan [0-9]{3}|test_plan_[0-9]{3}|Workstream [A-Z]' tests
```

Both commands should produce no active test matches. Narrow historical comments that explain a real production incident may remain only when they do not refer to plan execution, obsolete paths, or closure evidence; prefer describing the defect directly.

### Workstream C acceptance criteria

- [ ] No active test filename contains a plan number.
- [ ] No active test command references a deleted plan-numbered path.
- [ ] No active test module presents itself as plan closure evidence.
- [ ] Empty and zero-collection test modules are deleted.
- [ ] Test names and docstrings describe observable behavior.

---

## Workstream D — Delete or replace false integration and E2E tests

An integration or E2E label is valid only when the request enters Eggpool or multiple actual Eggpool components interact.

### D1. Failure-effects coverage

Current direct-mock tests that send an `httpx.Client` directly to `UPSTREAM_BASE` do not prove Eggpool failure classification, state mutation, retry, quarantine, or request-local isolation.

Replace the current pseudo-E2E matrix with this layered structure:

1. Keep the pure table-driven unit tests for `classify_failure_effects()` and signal extraction.
2. Keep focused unit tests for `EffectsApplier` idempotency and `ModelQuarantine` state transitions.
3. Add a small real-runtime integration set containing only distinct cross-component equivalence classes:
   - unsupported thinking or provider validation error: client 400, upstream request count according to policy, no account/model/circuit/quarantine penalty, next unrelated request succeeds;
   - authentication or quota error: expected scoped account effect, next eligible account/provider can succeed where routing policy permits;
   - retryable 5xx or transport error: bounded retry/failover and no duplicate durable finalization;
   - successful request after prior failure clears only the intended transient state.
4. Assert actual Eggpool state where the behavior claims state isolation:
   - health/circuit snapshot;
   - account eligibility;
   - model quarantine state;
   - active reservations;
   - pending request/attempt rows;
   - next-request outcome.

Do not retain 25 status-code rows twice at the E2E layer. The pure classifier table owns exhaustive mapping. The integration layer owns representative wiring and state effects.

Delete any test whose only assertions are mock response status and mock request count unless it is explicitly a mock-upstream helper test.

### D2. Database recovery coverage

Delete integration tests that:

- create `DatabaseRecoveryController` with `__new__`;
- assert that a private field such as `_state`, `_recovery_attempts`, or `_admission_admitted` exists;
- assert only that `ConsistencyAuditor` has a named method;
- send requests directly to the mock upstream while claiming database recovery;
- describe a fault that is never injected.

Preserve the existing detailed unit tests for recovery-controller state transitions and transaction fault handling.

Add only a compact real-runtime integration set for cross-component behavior:

1. Inject one deterministic commit or rollback uncertainty at the production database seam.
2. Send a request through Eggpool.
3. Assert the request reaches a bounded terminal outcome.
4. Assert readiness is false while database state is uncertain when the seam permits observing that interval.
5. Assert recovery is single-flight for concurrent waiters.
6. Assert the next request succeeds after recovery, or remains bounded 503 when configured retries are exhausted.
7. Assert no duplicate request finalization, attempt, or reservation row is left behind.

Use deterministic barriers/events around the fault seam. Do not add arbitrary sleeps except a very short final event-loop yield where unavoidable.

### D3. Integration naming rules

- Use `_e2e` only for tests entering the externally exposed application endpoint and traversing the intended stack.
- Use `_integration` or ordinary behavioral names for component wiring below the external endpoint.
- A test of `MockUpstream` belongs under helper/unit tests, not Eggpool integration tests.
- A source-symbol or attribute-existence assertion belongs nowhere unless it is a documented public API compatibility contract.

### Workstream D acceptance criteria

- [ ] No integration/E2E test sends requests directly to `MockUpstream` while claiming Eggpool behavior.
- [ ] Exhaustive status mapping remains in pure table-driven unit tests rather than duplicated E2E matrices.
- [ ] Representative failure classes are verified through the real Eggpool endpoint.
- [ ] Request-local compatibility errors prove no unintended shared-state mutation.
- [ ] Database-recovery integration tests inject real faults and observe real recovery behavior.
- [ ] No integration test uses `__new__` or private-attribute existence as proof of behavior.
- [ ] No severe historical defect loses all regression coverage.

---

## Workstream E — Consolidate duplicate high-cardinality test families

The first Plan 039 implementation renamed files but did not materially reduce collection. This workstream performs a bounded consolidation pass without turning the effort into an indefinite suite rewrite.

Use baseline collection data to inspect:

- the 20 files with the largest collected node counts;
- the 40 slowest canonical tests;
- parametrized matrices that repeat the same execution path and assertion shape;
- adjacent reload files whose setup and assertions differ only by one fault position;
- provider/protocol tests that form unnecessary Cartesian products;
- compatibility wrappers collecting the same tests twice;
- fixtures that duplicate application/database construction.

Apply these rules:

1. Parametrize genuinely equivalent cases in one file.
2. Keep separate tests when they cross different transaction, cancellation, serialization, or ownership boundaries.
3. Remove duplicate E2E coverage when exhaustive pure-unit coverage already exists.
4. Use equivalence classes for protocol payload spelling; do not test every inconsequential permutation at every layer.
5. Merge reload tests by behavior only when their fault seam and expected ownership outcome are the same.
6. Do not preserve both a plan-era copy and a canonical copy.
7. Delete compatibility import wrappers that cause duplicate collection.
8. Delete helper-only test modules that contain no assertions about Eggpool behavior.

Required measurable outcome:

- the final collected test count must be lower than the corrective baseline;
- the final canonical suite duration must not increase materially;
- the implementation commit must state the baseline and final counts/durations;
- do not game the count by collapsing meaningful assertions into one opaque mega-test.

No arbitrary coverage percentage or target test count is required. The reduction must come from identified duplication, false claims, dead modules, and redundant Cartesian coverage.

### Workstream E acceptance criteria

- [ ] Final collection is smaller than baseline.
- [ ] No duplicate collection wrappers remain.
- [ ] At least the known false-E2E matrices and dead modules are removed or consolidated.
- [ ] Large retained matrices have a clear pure-function or protocol-contract justification.
- [ ] Canonical-suite wall time is no worse than baseline within ordinary runner variance.
- [ ] Failures remain attributable to focused tests rather than mega-tests.

---

## Workstream F — Complete the Python 3.11 compatibility smoke

Keep `compat-311` narrow. Do not rerun the full suite.

The smoke suite must cover exactly these capabilities:

1. Package import and public version availability.
2. Minimal valid `AppConfig` parsing.
3. Invalid config rejection.
4. Successful `eggpool check-config` against a temporary valid TOML file.
5. Fresh temporary SQLite database migration.
6. One OpenAI non-stream request through the actual Eggpool ASGI endpoint.
7. One representative streaming or cross-protocol request through Eggpool.
8. CLI `--help` success.

For item 7, prefer the lowest-complexity representative that exercises a distinct Python compatibility path:

- a short streaming OpenAI SSE response consumed through Eggpool; or
- one OpenAI-to-Anthropic or Anthropic-to-OpenAI request if a stable minimal fixture already exists.

Do not add a large feature matrix to smoke. One representative is sufficient.

The valid `check-config` smoke must assert exit code zero and must use a generated temporary config with no real credentials or network access.

Keep the workflow command unchanged unless a path correction is required:

```bash
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
```

Target runtime remains under four minutes on a normal hosted runner.

### Workstream F acceptance criteria

- [ ] Valid `check-config` returns zero in smoke.
- [ ] Invalid `check-config` remains covered.
- [ ] One non-stream request enters Eggpool.
- [ ] One streaming or cross-protocol request enters Eggpool.
- [ ] Smoke remains small and Python 3.11-only in `compat-311`.
- [ ] Smoke contains no live credentials or network dependency.

---

## Workstream G — Add a concise manual-release procedure

Automated release publication must remain deleted.

Create a short operator/developer document, preferably:

- `docs/releasing.md`.

Link it from `README.md` or `AGENTS.md` without copying the entire procedure into multiple files.

The procedure must contain:

### G1. Preconditions

- clean working tree;
- current `main` fetched;
- intended version set in `pyproject.toml`;
- version is greater than the latest published version;
- changelog or release notes prepared if the project uses them;
- canonical before-push check passes.

### G2. Build

```bash
rm -rf dist/
uv build
```

Verify wheel and source distribution exist.

### G3. Clean-artifact smoke

Use a temporary environment that does not import from the source checkout:

```bash
TMP_VENV="$(mktemp -d)/venv"
uv venv "$TMP_VENV"
uv pip install --python "$TMP_VENV/bin/python" dist/*.whl
cd "$(mktemp -d)"
"$TMP_VENV/bin/python" -c "import eggpool"
"$TMP_VENV/bin/eggpool" --help
"$TMP_VENV/bin/eggpool" check-config --config /path/to/minimal-valid-config.toml
```

The implementer may improve portability, but the smoke must prove import and CLI execution from the built wheel outside the repository directory.

### G4. Publish manually

Document the repository-approved manual command, such as `uv publish`, using token/keyring configuration without embedding credentials.

Publishing must be an explicit operator action. Do not add a GitHub Actions release workflow.

### G5. Tag and GitHub release

Document explicit manual tag and release steps. State clearly:

- package-index releases are immutable;
- a failed or incomplete published release requires a new version bump;
- never force-reuse an already published version;
- command failures must stop the process and must not be masked with `|| true` or `|| echo`.

### G6. Risk-based SBC validation

Document when to run the target-device runtime validation:

- request-path, streaming, database, writer, reload, concurrency, or dependency changes: run it;
- documentation-only or metadata-only release: not required;
- uncertainty: run the short profile on representative hardware.

### Workstream G acceptance criteria

- [ ] Manual release documentation exists and is linked.
- [ ] It reuses the canonical check rather than creating a second full gate.
- [ ] It builds wheel and sdist.
- [ ] It smoke-tests the wheel outside the source checkout.
- [ ] It documents manual publication without credentials in the repository.
- [ ] It states that published versions are immutable.
- [ ] No automated release workflow is restored.

---

## Workstream H — Collapse manual soak and runtime validation

The current `extended-soak.yml` is manually dispatched, but it remains a large historical validation pipeline. Reduce it to one practical target-oriented path.

### H1. Canonical local command

Choose one existing real-runtime script as the canonical entry point, preferably `scripts/run_dispatch_stability_soak.py` if it actually starts and exercises Eggpool rather than only direct helper calls.

If the existing script does not exercise Eggpool truthfully, replace or narrow it rather than wrapping it in another layer.

The command should support:

```bash
uv run python scripts/run_dispatch_stability_soak.py \
  --profile sbc-reference \
  --duration-seconds 300 \
  --output /tmp/eggpool-runtime-validation.json
```

Exact option names may follow the current script, but the interface must remain small.

Required behavior:

- enter the actual Eggpool request path;
- use a temporary file-backed SQLite database;
- exercise non-stream and streaming traffic;
- include at least one controlled provider error followed by a successful request;
- optionally include one rehash cycle when the profile requests it;
- measure elapsed duration, completed requests, errors, process RSS, tasks, threads, descriptors where supported, pending requests, and active reservations;
- report unsupported metrics as unavailable, never zero;
- fail nonzero on invariant failure;
- produce one JSON summary by default.

Do not require Markdown summaries, JSONL time series, manifests, checksum files, evidence reports, or exact-head metadata for ordinary use.

### H2. Workflow shape

Choose one of these acceptable outcomes:

**Preferred:** delete `.github/workflows/extended-soak.yml` and document local execution on the target SBC.

**Acceptable:** retain one `workflow_dispatch` workflow with:

- one Python 3.12 job;
- no matrix;
- one profile input;
- one duration input;
- one invocation of the canonical script;
- optional upload of the single JSON summary on failure or explicit operator request;
- no 90-day evidence bundle;
- no separate short/standard/long jobs;
- no duplicated pytest soak suites;
- no scheduled trigger;
- no PR or push trigger.

GitHub-hosted soak is convenience validation, not target-hardware proof.

### H3. Remove residual evidence machinery

Delete or simplify manual-only tests and utilities whose primary purpose is to produce:

- schema-versioned closure bundles;
- Markdown plus JSON plus JSONL duplicates;
- SHA-256 manifests for test output;
- exact-head evidence;
- historical workstream summaries;
- multiple overlapping duration gates that are not driven by actual elapsed time.

Retain focused resource-invariant tests when they provide fast deterministic value, but describe them as bounded integration tests rather than 30/60/120-minute soak evidence.

### H4. Metric correctness

Correct Linux RSS handling where necessary. `resource.getrusage(...).ru_maxrss` is reported in KiB on Linux and bytes on macOS; do not label the Linux value as bytes without conversion.

Resource checks must:

- distinguish current RSS from maximum resident set size;
- report metric availability explicitly;
- avoid treating an unavailable descriptor/RSS/task metric as zero;
- use stable quiescent invariants where possible instead of permissive percentage-only thresholds.

### Workstream H acceptance criteria

- [ ] One canonical local runtime-validation command exists.
- [ ] It enters Eggpool and uses file-backed SQLite.
- [ ] It emits one concise machine-readable summary by default.
- [ ] Manual workflow is deleted or reduced to one Python 3.12 job with no matrix.
- [ ] No scheduled, PR, or push trigger runs soak.
- [ ] Historical artifact bundles and checksum/report machinery are removed where not needed.
- [ ] Linux RSS units are correct.
- [ ] Unsupported resource metrics are explicit, not zero.
- [ ] Target-SBC execution is documented as the authoritative performance/stability check.

---

## Workstream I — Reduce marker and documentation residue

Review `pyproject.toml` markers and retain only markers that control a real execution policy or are actively useful for focused development.

Candidates requiring justification include:

- `cache_compression_replay_full`;
- `perf_baseline`;
- `slow_writer_burst`;
- `resource_plateau`;
- `stability_assertion`;
- `workload_profile`;
- `unit` when ordinary directory selection is sufficient.

Keep `slow`, `performance`, `soak`, `extended_soak` only if the corresponding retained suites still use them. Keep `live` and `network` for opt-in external dependencies. Keep capability markers such as `request_path`, `reload`, or `dashboard` only when they select a coherent maintained subset and are documented.

Remove markers that merely preserve historical taxonomy.

Update:

- `AGENTS.md`;
- `.opencode/skills/development/SKILL.md`;
- `README.md`;
- any architecture/deployment text that still describes removed evidence or soak machinery.

Documentation must consistently state:

- two ordinary CI jobs;
- focused local edit loop;
- canonical before-push check;
- manual release;
- manual/risk-based target-device runtime validation;
- no requirement to run full soak for ordinary changes.

Do not add another long CI table or plan-specific command list.

### Workstream I acceptance criteria

- [ ] Every retained marker has an execution-policy or focused-development use.
- [ ] Historical marker residue is removed.
- [ ] Documentation agrees with actual workflows and commands.
- [ ] Manual release and target-device validation are linked once and not duplicated extensively.
- [ ] No active documentation references removed plan-numbered test paths.

---

## Workstream J — Verify closure without recreating ceremony

Run from a clean final checkout.

### J1. Static checks

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
```

### J2. Canonical suite

```bash
uv run pytest \
  -m "not slow and not performance and not soak and not extended_soak and not live and not network" \
  -q --tb=short --maxfail=1 --durations=40
```

### J3. Python 3.11 smoke

In a Python 3.11 environment:

```bash
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
```

### J4. Focused real-runtime regressions

Run the final files covering:

- provider compatibility error isolation;
- representative failure effects;
- database fault/recovery;
- streaming or protocol smoke;
- rehash behavior affected by consolidation.

Use current behavioral filenames, not plan-numbered aliases.

### J5. Repository hygiene

```bash
find tests -type f -name 'test_plan_*.py' -print
grep -RInE 'Plan [0-9]{3}|test_plan_[0-9]{3}|Workstream [A-Z]' tests
grep -RInE 'httpx\.(Client|AsyncClient)\(base_url=UPSTREAM_BASE' \
  tests/integration tests/contract tests/smoke
grep -RInE 'hasattr\(|__new__\(' tests/integration
```

Expected results:

- no plan-numbered active test files;
- no obsolete plan/workstream execution text;
- no direct-mock request in integration/E2E tests unless the file explicitly tests the mock helper itself;
- no private-attribute existence assertion used as behavioral proof.

### J6. Packaging smoke

Follow `docs/releasing.md` through clean-wheel installation and CLI/config validation, stopping before actual publication.

### J7. Runtime validation

Run the short local real-runtime profile. Prefer representative SBC hardware when available. For closure of this plan, an ordinary development host may prove command correctness, but it must not be described as SBC performance evidence.

### J8. CI verification

Push the final implementation and confirm both ordinary jobs pass:

- `check`;
- `compat-311`.

Do not add a third closure job. Record workflow link, test counts, and durations in the commit or pull-request description only.

### Workstream J acceptance criteria

- [ ] Ruff format/check and Pyright pass.
- [ ] Canonical Python 3.12 suite passes.
- [ ] Python 3.11 smoke passes.
- [ ] Focused real-runtime error and recovery regressions pass.
- [ ] Final test collection is lower than baseline.
- [ ] Final canonical duration is no worse than baseline within normal variance.
- [ ] Clean-wheel smoke passes.
- [ ] Short manual runtime validation passes.
- [ ] Both existing CI jobs pass.
- [ ] No third CI or closure workflow is added.
- [ ] No evidence artifact is required.

---

## Suggested implementation commits

Use two or three reviewable commits rather than one commit per file.

### Commit 1 — Test truthfulness and consolidation

- add the minimal shared real-runtime fixture;
- delete empty and stale plan-era modules;
- remove plan/workstream content from active tests;
- replace false failure-effects/database E2E tests with focused real-runtime regressions;
- consolidate duplicate high-cardinality families;
- remove obsolete markers and helper residue.

### Commit 2 — Smoke, release, and runtime validation

- complete Python 3.11 smoke coverage;
- add concise manual-release documentation;
- reduce or delete the extended-soak workflow;
- simplify the canonical runtime-validation command;
- correct resource metric semantics;
- update developer documentation.

### Commit 3 — Narrow verification fixes only, if needed

Use only for defects found while running the required gates. Do not use it to add new scope or evidence machinery.

The final commit message or pull-request description should contain:

- baseline and final collected test counts;
- baseline and final canonical durations;
- smoke count/duration;
- files deleted, merged, or converted from false E2E to real runtime;
- CI run link and conclusions;
- clean-wheel smoke result;
- short runtime-validation result.

---

## Small-model execution rules

1. Do not modify `.github/workflows/ci.yml` except for a demonstrated correctness issue; its two-job shape is already correct.
2. Do not add a new CI job.
3. Do not add a new audit script merely to scan tests; use disposable grep/find commands.
4. Do not preserve an integration test that never enters Eggpool.
5. Do not label a direct mock-server test E2E.
6. Do not replace behavioral tests with `hasattr`, AST, source-string, or module-location assertions.
7. Keep exhaustive decision tables at the pure-function unit layer.
8. Keep only representative wiring cases at the integration layer.
9. Do not collapse distinct cancellation, transaction, or ownership boundaries into one opaque test.
10. Do not introduce arbitrary sleeps where deterministic seams exist.
11. Do not create Markdown/JSON/JSONL/checksum bundles for ordinary verification.
12. Do not restore automated publication.
13. Do not put credentials or tokens in documentation or tests.
14. Do not claim GitHub-hosted timing is Raspberry Pi evidence.
15. Do not increase the canonical collected test count.
16. Do not introduce a new task runner or build framework.
17. If a production defect is exposed, make the smallest fix and retain a focused behavioral regression.
18. If an unrelated architecture issue is discovered, document it in the commit/PR and stop scope expansion.

---

## Global acceptance criteria

### CI and iteration

- [ ] Ordinary CI remains exactly two jobs.
- [ ] No full-suite Python matrix returns.
- [ ] No performance, soak, plan, closure, or evidence job runs on PRs.
- [ ] Focused local iteration remains the documented default.

### Test truthfulness

- [ ] No active `test_plan_*` filename remains.
- [ ] No active test contains obsolete plan-specific commands.
- [ ] No E2E/integration test bypasses Eggpool while claiming Eggpool behavior.
- [ ] No integration test substitutes symbol/attribute existence for behavior.
- [ ] Empty and zero-collection modules are removed.
- [ ] Critical historical defects retain real regressions.

### Test reduction

- [ ] Final collection is lower than the corrective baseline.
- [ ] False-E2E matrices are removed or reduced to representative real-runtime cases.
- [ ] Exhaustive decision coverage is retained at the pure unit layer.
- [ ] Duplicate collection and compatibility wrappers are removed.
- [ ] Canonical runtime does not materially regress.

### Compatibility smoke

- [ ] Python 3.11 smoke validates a valid config successfully.
- [ ] Python 3.11 smoke covers non-stream and streaming/transcode request paths.
- [ ] Smoke remains isolated from live providers and credentials.

### Release

- [ ] Manual release procedure is concise and linked.
- [ ] Wheel and sdist build are documented.
- [ ] Wheel is tested from a clean environment outside the repository.
- [ ] Published-version immutability and version bump requirements are explicit.
- [ ] Automated release publication remains absent.

### Runtime validation

- [ ] One canonical local real-runtime command exists.
- [ ] It uses actual Eggpool routing/application state and file-backed SQLite.
- [ ] Manual workflow is absent or one simple Python 3.12 job.
- [ ] No manual soak matrix remains.
- [ ] Resource metrics are correctly labeled and unavailable values are explicit.
- [ ] SBC execution is authoritative for target performance claims.

### Maintenance

- [ ] Marker list is reduced to active execution policies.
- [ ] Documentation matches the final tree.
- [ ] No new evidence framework, registry, manifest, or validator is added.
- [ ] Completion is recorded in normal commit/PR metadata.

## Explicit rejection conditions

Do not close this plan if any of the following remain:

- an integration/E2E test sends requests directly to `UPSTREAM_BASE` and claims Eggpool state behavior;
- `test_canonical_e2e_scenario.py` or another active module collects no tests but remains as historical documentation;
- a database-recovery integration test only checks `hasattr`, private field names, or method existence;
- active tests contain commands for deleted `test_plan_*` files;
- final collected test count is unchanged or higher without a documented external reason;
- the Python 3.11 smoke lacks successful valid configuration validation;
- the Python 3.11 smoke lacks a stream or protocol representative;
- release publication is undocumented after deleting automation;
- `.github/workflows/extended-soak.yml` still contains short/standard/long jobs or a Python matrix;
- resource tests label Linux `ru_maxrss` as bytes without conversion;
- unavailable resource metrics are represented as zero;
- a new CI job, release framework, evidence artifact, checksum manifest, or test registry is added;
- GitHub-hosted timing is presented as Raspberry Pi performance proof;
- Plan 039 is declared complete without passing both `check` and `compat-311` on the final implementation.

## Definition of done

This closure is complete when Eggpool has a truthful, smaller behavioral test suite; two passing ordinary CI jobs; a genuinely useful Python 3.11 smoke; a concise manual release path; and one practical target-device runtime-validation command. The repository must no longer carry mock-only E2E claims, private-attribute architecture tests, empty plan shells, or a multi-job soak/evidence pipeline that exceeds the needs of a privately operated SBC service.