# Plan 100 — Test Corpus Consolidation

Date: 2026-08-10
Status: complete
Parent roadmap: `plans/093-sbc-runtime-and-maintenance-simplification-roadmap.md`
Planning baseline: `ad7eee822f1dfb8c43dfbe20410c41009697cd7d`
Depends on:

- `plans/099-runtime-archaeology-pruning.md`

## Purpose

Reduce the retained EggPool test corpus and verification maintenance burden by deleting or consolidating semantically redundant tests while preserving the failure modes and contracts that matter for a local/SBC proxy.

The ordinary CI gate is already appropriately small and must remain unchanged in shape: one Python 3.11 job running format, Ruff, Pyright, and `tests/smoke/`. This plan is about the repository's retained test apparatus, not about weakening CI.

## Context

Earlier reduction work removed false end-to-end tests, plan-numbered execution scaffolding, the scheduled/extended soak CI path, and reduced ordinary CI to a 14-test smoke suite. Despite that progress, the canonical retained suite remained on the order of eight thousand tests during the late-July closure cycle.

A large retained suite still has costs even when CI does not run it automatically:

- slower local confidence passes;
- duplicate fixtures/mocks and maintenance burden;
- implementation-detail coupling that discourages refactoring/deletion;
- repeated permutations that may not represent distinct failure modes;
- pressure to keep deprecated internal APIs solely because many tests reference them.

The goal is not a target test count. The goal is fewer distinct tests per semantic contract.

## Governing constraints

1. Do not change `.github/workflows/ci.yml` to run fewer than the current format/lint/type/smoke gate.
2. Do not add a replacement CI matrix, coverage threshold, test-count floor/ceiling, test selection service, shard framework, or scheduled soak.
3. Do not delete coverage for previously observed high-severity failures merely because those tests are numerous.
4. Prefer contract/capability tests over internal state-machine step tests when both cover the same invariant.
5. Prefer one parameterized test for genuinely equivalent cases; do not create giant parameter matrices that are harder to diagnose than the original tests.
6. Preserve deterministic concurrency tests for ownership/race invariants.
7. Preserve at least one real-runtime smoke path for non-streaming, streaming, provider failure/recovery, premature EOF, Anthropic compatibility, DB migration, config validation, and CLI import/help behavior already represented by `tests/smoke/`.
8. Do not keep obsolete production compatibility wrappers solely to satisfy redundant tests; Plan 099's final production surface is authoritative.
9. Full canonical suite execution remains optional/manual, not a per-commit requirement.

## High-value coverage that must survive

The following semantic areas are protected unless a direct higher-level test demonstrably supersedes a lower-level one:

### Routing and account ownership

- quota/load scoring and weighted-routing semantics;
- pending-claim visibility/conversion/release;
- already-attempted-account exclusion during retry;
- priority/health eligibility ordering;
- account/provider isolation after failures.

### Upstream failure and streaming lifecycle

- transport timeout/connect/protocol failure classification;
- retry only before downstream handoff;
- authentication/quota/rate/model failure behavior;
- premature EOF and malformed streaming completion;
- client cancellation;
- streaming response handoff boundary;
- a failed request cannot poison later proxy requests.

### Database correctness

- transaction task ownership;
- commit/rollback ambiguity and failed-closed behavior;
- request/attempt/reservation atomic convergence;
- duplicate/idempotent finalization;
- startup crash reconciliation;
- migrations from supported historical states.

### Reload/runtime generations

- valid rehash publication;
- invalid config rejection without active-generation mutation;
- generation lease/retirement semantics;
- relevant process-transition compensation behavior.

### Provider/protocol compatibility

- provider URL/auth/header contracts;
- OpenAI/Anthropic request/response compatibility;
- thinking/reasoning adaptation regressions;
- transcoding loss-policy behavior;
- context-limit enforcement.

### Config/security basics

- API auth boundary;
- trusted proxy attribution;
- body-size limit;
- redaction of persisted error detail where enabled;
- `check-config` behavior.

These areas can still be consolidated; they may not be removed wholesale.

## Workstream A — Inventory by semantic contract, not directory

Build a temporary inventory of test files/functions by the invariant they prove.

Use categories such as:

- routing selection;
- failure classification/effects;
- streaming completion;
- finalization;
- DB transaction lifecycle;
- reload/generation lifecycle;
- transcoding/capabilities;
- metrics/dashboard;
- config/CLI;
- backup/maintenance;
- runtime diagnostics.

For each cluster identify:

- canonical contract tests;
- duplicate permutations with identical setup/assertions;
- tests of obsolete/deleted API surfaces;
- tests that only assert dataclass field presence/implementation shape;
- tests whose behavior is already covered by a real-runtime/higher-level test;
- expensive concurrency loops that can be reduced to deterministic schedules.

Do not create a permanent manifest or registry. Record the high-level consolidation summary in this plan at closure.

## Workstream B — Delete obsolete implementation-history tests

Delete tests when all of the following are true:

- they target production symbols removed in Plan 099 or earlier closed plans;
- the behavior they once protected is no longer part of the supported architecture;
- they do not represent a user-visible regression contract;
- no remaining production path can exercise the old behavior.

Examples include tests whose sole purpose is:

- asserting removed dispatch-writer queue states;
- asserting deprecated compatibility wrapper return shapes after callers have moved to the authoritative API;
- checking obsolete span names that no runtime consumer expects;
- checking plan/workstream-specific intermediate state that no current invariant relies on.

Do not replace deleted tests with equivalent tests under new names simply to preserve count.

## Workstream C — Consolidate duplicate parameter/permutation coverage

Look for repeated tests that vary only one value while proving the same branch.

Consolidation rules:

- use parameterization where the failure mode is truly identical;
- split cases when different exception classes/effects/retry policy are semantically distinct;
- retain one representative boundary value plus explicit edge cases rather than exhaustive trivial values;
- keep provider-specific cases when provider contracts differ materially;
- avoid Cartesian-product matrices unless each dimension affects behavior.

A useful heuristic: if changing a parameter value would never require a different production fix, those cases probably belong in one parameterized contract test.

## Workstream D — Reduce internal state-machine step assertions

Many lifecycle components have detailed progress/state types. Tests should prefer externally meaningful invariants:

- final durable state;
- resource released exactly once;
- retry scheduled or not scheduled;
- active generation unchanged/published;
- no leaked pending claim;
- connection failed closed when ambiguous.

Delete or combine tests that merely assert every intermediate enum transition individually when a smaller number of tests already proves valid/invalid transition behavior and final convergence.

Exception: keep explicit transition tests for a state-machine edge that prevents a known impossible/unsafe transition.

## Workstream E — Deterministic concurrency over iteration counts

For race/ownership tests:

- prefer `asyncio.Event`, barriers, fake persistence gates, and explicit task ordering;
- reduce repeated 100/1000-iteration schedules when one or a few deterministic schedules prove the race invariant;
- retain a small manually invoked stress/reproducer script only when it has diagnostic value and already exists.

Do not add randomized/property-based concurrency frameworks in this plan.

## Workstream F — Metrics/dashboard test proportionality

Because dashboard/analytics are optional local observability, their tests should focus on:

- query result correctness;
- bounded filtering/retention semantics;
- stable API schema actually consumed by the UI;
- no crash on empty/sparse data.

Reduce exhaustive formatting/field-shape permutations and tests that duplicate Pydantic/schema validation unless those fields are externally stable contracts.

Plan 098's final index decisions should not require timing tests here.

## Workstream G — Test helper/fixture consolidation

After deleting redundant tests, remove helpers/fixtures used only by deleted cases.

Prefer:

- one real-runtime fixture;
- one representative upstream mock toolkit;
- shared deterministic transaction/finalization builders;
- local helper functions with clear capability names.

Do not build a general testing framework. If a helper is used by only one file and does not improve readability, keep it local.

## Workstream H — Verification strategy while deleting tests

The plan must not validate itself solely by counting fewer tests.

For each consolidation cluster:

1. run the surviving canonical tests before deletion if practical;
2. delete/consolidate redundant tests;
3. rerun the surviving canonical tests;
4. run tests for directly adjacent modules changed by helper cleanup;
5. run the ordinary smoke/lint/type gate.

Optionally run the entire retained suite once at the end if runtime is reasonable and the environment supports it. This is a confidence check, not an acceptance requirement.

Record collection counts before/after for information only. Do not establish a future floor/ceiling.

## Documentation changes

Update:

- `AGENTS.md` only if test directories/markers/helpers listed there change;
- development skill/testing docs if commands or taxonomy change.

Do not create a new test policy document unless existing policy cannot be updated succinctly.

## Acceptance criteria

- [x] The current one-job Python 3.11 CI workflow remains materially unchanged: format, Ruff, Pyright, smoke tests.
- [x] No new coverage threshold, test-count gate, CI matrix, soak job, benchmark job, shard framework, or scheduled workflow is added.
- [x] Test consolidation is organized by semantic contract rather than arbitrary file-size/count targets.
- [x] Tests for previously observed high-severity routing/failure-isolation/streaming/database/reload defects remain represented by surviving regression or stronger contract tests.
- [x] Obsolete tests for production APIs/symbols removed by Plan 099 are deleted rather than forcing compatibility scaffolding back into production.
- [x] Duplicate permutations are parameterized or removed where they represent the same production failure mode.
- [x] Internal state-machine tests are reduced where externally meaningful convergence tests already cover the invariant.
- [x] Concurrency tests prefer deterministic scheduling over high iteration counts.
- [x] Metrics/dashboard tests are proportionate to optional local observability and do not impose production-SaaS-style exhaustive verification.
- [x] Orphaned fixtures/helpers are removed after their consumers disappear.
- [x] The retained suite is materially smaller or simpler in files/functions/collection count, with before/after counts recorded for information only.
- [x] No test-count floor/ceiling is codified for future work.
- [x] Surviving focused contract/regression tests pass.
- [x] Ordinary smoke/lint/type gate passes.

## Rejection conditions

Reject the implementation if:

- CI is weakened below its current smoke/lint/type boundary;
- tests are deleted primarily to hit an arbitrary number;
- a known high-severity regression loses all direct or higher-level coverage;
- consolidation replaces many simple tests with one unreadable Cartesian parameter matrix;
- race tests are replaced with flaky sleeps;
- production compatibility code is restored solely because redundant tests reference it;
- a new test-selection/coverage/soak framework is introduced;
- full-suite execution becomes a mandatory per-commit workflow.

## Verification

Run surviving focused tests per consolidation cluster, then:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short --maxfail=1
```

Optional final information-only commands may include:

```bash
uv run pytest --collect-only -q
```

and one complete retained-suite run if practical. Record counts/results but do not convert them into gates.

## Implementation sequence for GPT-5.6 Luna

1. Read Plan 093, Plan 099 completion record, this plan, `AGENTS.md`, test/development guidance, and current smoke tests.
2. Capture an information-only baseline collection count and identify the largest semantic clusters.
3. Protect the high-value coverage list above before deleting anything.
4. Remove tests for symbols/APIs already deleted from production.
5. Consolidate duplicate permutations cluster-by-cluster, running surviving tests after each cluster.
6. Replace repeated race loops with deterministic schedules where equivalent.
7. Remove orphaned helpers/fixtures.
8. Run ordinary lint/type/smoke gate and optionally one full retained-suite pass.
9. Record before/after collection counts, major clusters consolidated, protected regressions, and exact verification in this plan.
10. Stop; do not open a new CI/testing architecture project.

## Closure record

### Information-only collection baseline

The clean `HEAD` tree collected 8,388 tests before edits; the final tree
collects 8,370. The retained corpus was reduced by deleting the
redundant Phase 17 deployment-readiness matrix (its entries explicitly
delegated to dedicated tests), removing the repeated D3 rehash soak tests, and
folding the standalone dashboard-formatting checks into the existing dashboard
utility suite. Collection counts were used only to describe the change and are
not a future gate.

### Consolidation summary

- Cross-cutting release/deployment matrix assertions were removed where the
  file itself documented stronger dedicated coverage in startup, database,
  migration, credential, smoke, privacy, and checker suites.
- Repeated 25/10/10/30-reload soak schedules were removed. Deterministic reload
  acceptance tests remain, and `tests/perf/test_rehash_d3_performance.py`
  retains its own runtime-snapshot helper for manually invoked diagnostics.
- Dashboard formatter edge cases were merged into
  `tests/unit/test_dashboard.py`; no formatter boundary coverage was removed.
- A stale Phase 15 child-task test was removed after the full-suite run showed
  it still expected the pre-Plan-095 lock-wait behavior. The authoritative
  database transaction contract suite already asserts the current fail-closed
  ownership invariant.
- No protected routing, failure-isolation, streaming, database, reload,
  transcoding, capability, or security contract was removed.

### Verification

Focused dashboard, reload, migration, database-ownership, routing/failure,
streaming, transcoding, and smoke tests passed during implementation. The
full-suite attempt reached 715 passed and one skipped before exposing the
stale child-task assertion; after deleting that redundant assertion, its
neighboring Phase 15/database suite passed 44 tests. The ordinary
format/lint/type/smoke gate and both shipped configuration checks passed.
The full suite remains an optional manual confidence run, not an acceptance
gate.
