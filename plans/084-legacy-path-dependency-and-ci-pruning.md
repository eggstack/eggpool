# Plan 084 — Legacy Path, Dependency, and CI Pruning

Date: 2026-08-05
Status: complete
Parent roadmap: `plans/077-sbc-lifecycle-simplification-and-runtime-correctness-roadmap.md`
Depends on:

- `plans/081-terminal-ownership-consolidation.md`
- `plans/082-database-fail-closed-simplification.md`
- `plans/083-lean-defaults-and-conditional-subsystem-construction.md`

Planning baseline: `cd8967799e6613f3a5965af8cd15ce3c5269aaa8`

## Purpose

Delete production fallbacks, milestone scaffolding, dependency extras, and verification work that no longer protect a supported EggPool execution path.

This plan is a pruning pass after the canonical terminal, database, and lightweight-construction architectures have landed. It must reduce source/test surface and installation/CI work without removing supported proxy features or replacing mature core libraries.

## Governing decisions

1. Delete obsolete paths rather than preserving permanent compatibility for internal tests.
2. Do not remove a public CLI/config/API behavior without confirming release compatibility.
3. Do not replace FastAPI/Starlette, HTTPX/httpcore, Pydantic, aiosqlite, or Click in this roadmap.
4. Optional `orjson` and `pproxy` remain optional.
5. CI remains one job on Python 3.11.
6. Documentation/plan-only changes should not install and run the full code gate.
7. Local developer tooling may remain richer than CI, but CI installs only what it runs.
8. Remove tests that solely exercise deleted compatibility branches; preserve high-value fault-boundary tests.
9. Remove historical milestone comments/import scaffolding from production modules when they no longer explain current behavior.
10. Do not turn this into a broad style, naming, or file-layout rewrite.

## Workstream A — Inventory supported versus legacy paths

Before deletion, inspect production startup and current documentation for each candidate:

- API request path without `RuntimeManager`;
- coordinator path without a finalization/terminal supervisor;
- `request/finalization_queue.py` compatibility adapter;
- coordinator retained terminal aliases/helpers after Plan 081;
- same-process database recovery controller/config after Plan 082;
- `ProxyRequestContext.upstream_body` compatibility mirror;
- unused `SelectionClaim`/diagnostic/span imports marked as milestone scaffolding;
- deprecated metrics fields and aliases;
- duplicate dispatch-writer configuration surfaces;
- app-state generation mirrors;
- old configuration normalization paths;
- test-only multi-loop database behavior;
- retired live-rehash ownership callbacks;
- obsolete operational scripts and evidence collectors.

Classify each as:

1. supported public behavior;
2. shipped configuration compatibility;
3. internal embedder behavior documented as supported;
4. test-only fallback;
5. unreachable/dead code;
6. historical documentation only.

Only categories 4–6 are automatic deletion candidates. For categories 2–3, preserve a bounded compatibility adapter only when release policy requires it.

## Workstream B — Remove production fallback execution paths

### Runtime manager fallback

If normal application startup always installs `RuntimeManager`, remove the request handler branch that reads a legacy coordinator directly from `app.state`.

Tests must construct a minimal real runtime manager/generation fixture instead of exercising an unsupported path.

If a documented library embedder creates the app without a runtime manager, retain one explicit public constructor path and document it. Do not infer support from old tests alone.

### Terminal supervisor fallback

After Plan 081, production terminal submission must always use the canonical supervisor. Remove direct finalizer execution used only by lightweight tests.

Replace affected tests with a small supervisor fixture. Do not create a second “simple mode.”

### Finalization queue

Delete `request/finalization_queue.py` and imports if no supported external integration remains. If one release of compatibility is required:

- reduce it to a clearly deprecated thin adapter into the canonical supervisor;
- no retry/backoff/drop/ownership behavior may remain in the adapter;
- add one deprecation test, not a parallel suite;
- schedule physical deletion in the next breaking release note.

### Provider-bound payload mirror

Audit all reads/writes of `ProxyRequestContext.upstream_body`.

If `ProviderBoundRequest` is authoritative on every production dispatch path:

- remove the mirror;
- update tests to assert provider-bound serialization;
- preserve original client bytes separately;
- ensure no body is serialized more than once.

If one compatibility caller remains, isolate it behind one property and mark removal explicitly rather than continuing dual authority throughout the coordinator.

## Workstream C — Remove milestone and diagnostics scaffolding

Clean production modules of:

- unused imports retained with `# noqa`/type-ignore solely for completed milestones;
- comments such as “Milestone B scaffolding” when the type is unused;
- plan-number commentary that no longer describes current architecture;
- duplicate aliases and compatibility counters with no dashboard/API consumer;
- private helper seams used only by deleted tests.

Keep useful rationale and current invariants. Historical plans remain in `plans/` and need not be reproduced in source comments.

Do not mass-edit all comments. Limit changes to touched modules and confirmed dead scaffolding.

## Workstream D — Prune configuration compatibility carefully

### Dispatch writer surface

Confirm the canonical field is top-level `[dispatch_writer]`.

If nested `[database.dispatch_writer]` is no longer parsed or documented as supported:

- remove stale schema/docs/tests;
- reject it as an unknown field under existing strict config validation;
- do not add a migration framework.

If a released version accepted the nested field and compatibility is required:

- normalize it in one location;
- emit one bounded deprecation warning;
- reject conflicting top-level/nested values;
- remove all duplicate runtime consumers;
- document the intended removal release.

### Deprecated metrics/config fields

For each deprecated field, inspect actual released usage and current examples.

- remove fields whose compatibility window has elapsed;
- otherwise retain one parse-time normalization and warning;
- no runtime branch should continue checking both old and new fields after normalization.

## Workstream E — Slim dependency installation without replacing core libraries

### Runtime dependencies

Audit actual imports/features for:

- `granian[pname]` versus base `granian`;
- transitive extras enabled unintentionally;
- direct dependencies already guaranteed transitively but imported by EggPool;
- optional `orjson` and `pproxy` import boundaries.

Rules:

- switch `granian[pname]` to base `granian` only if process-title naming is unused and installation/startup tests pass on Linux/aarch64-supported wheels;
- keep direct dependencies when EggPool imports their public API, even if another package currently pulls them transitively;
- do not rely on undeclared transitive dependencies;
- keep optional dependency imports lazy and produce the existing clear config/startup error when enabled but missing;
- do not add pure-Python replacements for working core libraries.

Record before/after locked production package count and wheel/download size where readily available. Do not make those CI gates.

### Developer and CI extras

Split `pyproject.toml` extras so CI installs only the tools it runs, for example:

- `ci`: ruff, pyright, pytest, pytest-asyncio, respx as required by smoke;
- `dev`: `ci` plus pytest-cov/coverage and any local-only tools.

Because Python extras cannot directly include another extra in every installer form, list dependencies explicitly or use dependency groups supported by `uv`. Choose the simplest lock-compatible arrangement.

Remove duplicate `pytest-cov` plus direct `coverage[toml]` installation if only one declaration is needed for local coverage.

Update `uv.lock` and contributor documentation.

## Workstream F — Keep CI small and skip non-code changes

Retain one `check` job with:

- ruff format check;
- ruff lint;
- pyright;
- smoke tests.

Add workflow path filtering so a change limited to these areas does not run the full job:

- `plans/**`;
- Markdown documentation that cannot affect packaging/runtime;
- repository metadata with no executable/config effect.

Be conservative: changes to `pyproject.toml`, `uv.lock`, configuration examples, scripts, deployment files, package data, or workflow files must still run CI.

Use either `paths-ignore` or a small changed-path condition. Prefer native workflow filtering; do not add a third-party path-filter action.

Keep push and pull-request coverage for code changes. Do not add multiple jobs to optimize a ten-minute workflow.

## Workstream G — Prune tests with the deleted branches

Delete tests whose only purpose is to verify:

- no-runtime-manager production fallback;
- no-supervisor production fallback;
- same-process database recovery states;
- multi-loop lock rebinding;
- retired duplicate config consumers;
- coordinator retained terminal registries removed by Plan 081;
- obsolete milestone scaffolding.

Preserve or consolidate tests covering:

- account failover and distinct-account ceiling;
- local versus provider failure isolation;
- ASGI response-start handoff;
- cancellation;
- premature EOF;
- terminal component convergence;
- fail-closed database ambiguity;
- startup reconciliation;
- rehash candidate rollback/retirement;
- lightweight disabled-subsystem construction;
- exact-version update;
- config validation.

Measure test reduction by deleted/merged files or test count, but do not set a numeric quota that encourages deletion of valuable coverage.

## Workstream H — Documentation and package verification

Update:

- `AGENTS.md` development/CI dependency commands;
- release/deployment docs;
- architecture ownership and canonical path descriptions;
- config reference/deprecations;
- README feature claims if defaults changed;
- `CHANGELOG.md` with compatibility removals/deprecations where appropriate.

Build and inspect wheel/sdist:

```bash
uv build
```

Confirm plans/tests/dev artifacts remain excluded as intended and required bundled config/static assets remain present.

## Focused verification

Required cases:

1. production app startup installs the canonical runtime manager and terminal supervisor;
2. request handlers have no unsupported direct app-state coordinator path;
3. terminal work has no unsupported direct-finalizer path;
4. deleted finalization queue has no imports/consumers;
5. provider-bound payload remains single-serialization and authoritative;
6. canonical dispatch-writer config validates; conflicting/obsolete surface follows the documented compatibility decision;
7. optional extras fail clearly only when enabled but missing;
8. base Granian works if the `pname` extra is removed;
9. CI extra installs and runs all four existing checks;
10. dev extra retains local coverage tooling;
11. workflow does not run for a plan-only change and still runs for config/package changes;
12. wheel/sdist contents are correct;
13. focused tests and smoke pass.

Suggested commands:

```bash
uv lock --check
uv sync --frozen --extra ci
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
uv sync --frozen --extra dev
uv build
```

Run affected unit/integration files before the full smoke gate.

## Implementation notes

- Removed the unsupported request-time `app.state.coordinator` fallback and
  the direct terminal-finalizer fallback. Test fixtures now install a real
  `RuntimeManager` and `RequestFinalizationSupervisor`.
- Removed the unused `request/selection_claim.py` module and its dedicated
  unit test. Selection locking and bounded diagnostics remain in their active
  coordinator/diagnostics implementations.
- Removed the provider-payload mirror from `ProxyRequestContext`; provider
  serialization now has one authoritative `ProviderBoundRequest` path.
- Removed the unreleased same-process `[database.recovery]` configuration
  surface. Startup crash reconciliation remains the supported recovery
  boundary.
- Verified that the serve path uses Granian process naming: the base package
  exits at startup without `granian[pname]`, so that runtime extra remains.
  Split `ci` and `dev` extras, refreshed `uv.lock`, and added native workflow
  path filtering.
- Updated request, runtime, deployment, architecture, contributor, and
  troubleshooting documentation to describe the canonical paths.

The broad local test run exercised the affected suites and reached the later
manual/performance portion after all discovered stale-fixture failures had
been corrected; the exact CI gate below is the release check.

## Acceptance criteria

- [x] Unsupported runtime-manager and terminal-supervisor fallbacks are removed.
- [x] Coordinator/finalization compatibility ownership paths removed by prior plans no longer exist.
- [x] Provider-bound request serialization has one authority or one isolated compatibility adapter.
- [x] Dead milestone imports/comments/counters are pruned from touched production modules.
- [x] Duplicate/deprecated config fields have one normalization path or are removed according to release policy.
- [x] Core runtime libraries remain unchanged unless a measured, low-risk extra reduction is proven.
- [x] Granian's `pname` extra was audited; it remains because process naming is
  used by the serve path, and the base package was rejected by startup tests.
- [x] CI installs only required CI tools; local coverage remains available through dev tooling.
- [x] CI remains one job and skips plan/document-only changes.
- [x] Tests for deleted branches are removed while high-value boundary coverage remains.
- [x] Wheel/sdist contents remain correct.
- [x] Smoke passes.
- [x] Net source/test complexity decreases.

## Rejection conditions

Do not close this plan if:

- a production fallback remains solely because an old unit test uses it;
- a supported external/library behavior is removed without documentation;
- undeclared transitive dependencies are relied upon;
- a core library is replaced speculatively;
- CI gains jobs, matrices, coverage gates, or third-party path machinery;
- test count is reduced by deleting failure-boundary coverage;
- packaging omits required assets or includes plans/tests unintentionally.

## Implementation sequence for GPT-5.6 Luna

1. Build the supported/legacy inventory and cite each production consumer.
2. Remove runtime/terminal fallbacks and migrate tests.
3. Remove payload/config/scaffolding duplication with targeted tests.
4. Audit runtime dependency extras; change only proven unused extras.
5. Split CI/dev dependency installation and update the lock.
6. Add native CI path filtering.
7. Delete/merge obsolete tests while preserving boundary coverage.
8. Build package artifacts and inspect contents.
9. Run the exact CI gate and record outcomes.
10. Mark complete only after showing net deletion/simplification.

## Closure verification (2026-08-06)

`uv build` produced a 1,264,031-byte final wheel versus 1,269,730 bytes at the
planning baseline, while the production dependency graph remained 19 packages.
The CI-only extra was restored and the final ruff, pyright, smoke, config, and
focused gates passed. The only stale path found during manual performance
execution was a test fixture that omitted the now-required generation-owned
finalization supervisor; it was corrected in the existing test and remeasured.
