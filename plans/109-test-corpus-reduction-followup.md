# Plan 109 — Test Corpus Reduction Follow-up

Date: 2026-08-11
Status: complete
Parent roadmap: `plans/103-sbc-protocol-parity-and-runtime-efficiency-roadmap.md`
Planning baseline: `de3eeea5936c964ffa33b7939c791e98d35cfcbb`
Depends on:

- `plans/104-local-exposure-and-log-redaction.md`
- `plans/105-openai-anthropic-transcode-parity.md`
- `plans/106-provider-native-prompt-cache-translation.md`
- `plans/107-request-memory-and-body-limit-reduction.md`
- `plans/108-compression-cache-surface-simplification.md`

## Purpose

Perform a second, materially more targeted reduction of EggPool's retained offline test corpus after Plans 104–108 settle the production surface.

Plan 100 correctly simplified some historical test scaffolding, but the repository still collected 8,370 tests at Roadmap 093 closure. For a local/SBC proxy with a 14-test ordinary smoke gate, that retained corpus remains disproportionately large and can impose meaningful local iteration/maintenance cost even though CI does not run it by default.

This plan must reduce semantic duplication without weakening coverage for the failure modes that justified EggPool's robustness architecture.

The goal is **not** an arbitrary test count. The goal is fewer tests and fixtures per distinct production invariant, especially in optional/transcode/compression/observability surfaces where historical phase-by-phase work accumulated repeated permutations.

## Governing constraints

1. Keep `.github/workflows/ci.yml` materially unchanged: one Python 3.11 job with Ruff format/lint, Pyright, and `tests/smoke/`.
2. Do not remove the 14-test smoke behaviors identified in `AGENTS.md` unless a smoke test is directly replaced by a stronger equivalent while preserving the same ordinary CI coverage.
3. Do not add coverage thresholds, test-count floors/ceilings, test sharding, selection services, scheduled full-suite jobs, benchmark jobs, soak jobs, hardware jobs, or release jobs.
4. Do not add property-based/randomized test frameworks solely to replace explicit cases.
5. Preserve deterministic concurrency tests for transaction ownership, pending claims, finalization ownership, rehash generations, and handoff/retry boundaries.
6. Preserve regression coverage for previously observed high-severity failures, including provider errors poisoning later requests, malformed/unsupported thinking controls, ambiguous database outcomes, premature EOF, and invalid rehash publication.
7. Prefer externally meaningful contract/convergence assertions over every intermediate enum/state field.
8. Prefer compact parameterization only when cases would require the same production fix.
9. Do not preserve deleted production compatibility wrappers solely because tests reference them.
10. Do not run or require the full retained suite on every implementation commit.
11. Full-suite execution at the end is optional/manual confidence evidence only.
12. Do not create a permanent inventory manifest or test policy framework.

## Protected coverage that must survive

### Routing/account failure isolation

Retain direct or stronger coverage for:

- account/provider eligibility and priority-tier selection;
- weighted request/token load semantics;
- pending claim publication/conversion/release;
- already-attempted-account exclusion;
- upstream-authoritative suppression and bounded recovery;
- one failed/malformed upstream request cannot poison later proxy requests;
- authentication/quota/rate/model/transport/protocol failure classification and effects.

### Streaming lifecycle

Retain:

- retry only before downstream handoff;
- first-byte/idle timeout classification;
- premature EOF;
- malformed stream completion;
- client cancellation;
- tool-call/result streaming adaptation;
- accepted-stream finalization and post-handoff no-retry behavior.

### Database/finalization correctness

Retain:

- task-owned transactions;
- child task cannot inherit SQL ownership;
- commit/rollback ambiguity fails closed;
- request/attempt/reservation atomic convergence;
- duplicate/idempotent finalization;
- startup crash reconciliation;
- supported migration upgrade/fresh-schema compatibility and checksums.

### Rehash/runtime generations

Retain:

- valid rehash publication;
- invalid config rejected without mutating active generation;
- generation lease/retirement;
- process-transition compensation where still supported;
- finalization supervisor ownership/saturation invariants.

### Protocol compatibility

Retain, after Plans 105–106:

- OpenAI↔Anthropic basic request/response translation;
- structured-output native mapping and explicit lossy cases;
- strict tool mapping;
- parallel-tool-disable mapping;
- reasoning/thinking capability rejection/mapping;
- prompt-cache boundary translation and explicit TTL/tool-definition mismatches;
- same-protocol passthrough contracts;
- generic-compatible-provider conservative capability behavior.

### Local security/config basics

Retain:

- API auth boundary and non-loopback policy from Plan 104;
- trusted-proxy attribution;
- no credential/raw malformed tool payload logging;
- request body-size limit from Plan 107;
- `check-config`/startup agreement.

## Workstream A — Capture immediate pre-consolidation baseline

After Plans 104–108 are complete, record:

```bash
uv run pytest --collect-only -q
```

Record only:

- total collected tests;
- largest test files/clusters by rough collection count if easy to determine;
- current smoke count.

The Plan 100 historical count of 8,370 is context, not a gate. Plans 104–108 may legitimately add focused high-value regressions before this reduction starts.

Do not create a script or committed artifact solely to calculate test counts.

## Workstream B — Build a temporary semantic-cluster inventory

Use existing pytest collection output and `rg` to identify large/repetitive clusters in these priority areas:

1. transcoder unknown-field/loss-policy permutations;
2. provider capability/static-model permutations;
3. prompt-cache translation/synthetic-cache permutations;
4. compression policy/tuning permutations after Plan 108 removes dormant modes;
5. optional metrics/dashboard/observability field-shape tests;
6. historical phase/workstream/intermediate-state tests;
7. duplicate failure-classification tests repeated at unit and integration levels;
8. request payload freeze/thaw/copy implementation-detail tests invalidated by Plan 107 ownership simplification;
9. repeated config parsing cases where Pydantic validation and one focused behavioral test already prove the contract;
10. duplicate provider URL/header contract cases that differ only by constants already covered by table-driven contract tests.

For each cluster classify tests as:

- canonical semantic contract;
- stronger higher-level regression;
- redundant same-fix permutation;
- obsolete implementation detail;
- historical/deleted feature behavior;
- protected unique failure mode.

The inventory is temporary working notes; summarize deleted/consolidated clusters in the plan closure only.

## Workstream C — Transcoder/capability matrix reduction

Plans 105–106 will likely add new native compatibility cases. Consolidate the older corpus around the final capability contract.

Rules:

- keep one case per materially different source→target semantic mapping;
- keep warn and reject paths only where they exercise different production branches/outcomes;
- remove separate tests for every field ordering/name/value when the translator is structurally table-driven and one boundary case proves the same branch;
- consolidate known-capable versus generic-unknown target cases instead of repeating every provider ID;
- preserve provider-specific cases only when upstream contracts genuinely differ;
- preserve streaming-specific tests when streaming code differs from body translation.

Do not build a giant `pytest.mark.parametrize` Cartesian product of protocol × feature × provider × loss-policy × stream mode.

A useful rule: if two failing cases would be fixed by changing the same mapping table branch and have no distinct external contract, they are candidates for consolidation.

## Workstream D — Compression/cache test reduction after surface deletion

Plan 108 should remove/reject dormant tuning/config paths. Delete tests whose only purpose is to preserve those removed paths.

Then consolidate retained optional-feature coverage around:

- disabled path remains inert/cheap;
- safe supported compression transform behavior;
- static-prefix override resolved validation;
- retained tuning/diagnostic mode, if any;
- native cache boundaries take precedence over synthetic behavior;
- one retained synthetic no-source-intent path if still supported;
- cache/compression boundary protection.

Delete:

- tests for removed future/apply modes;
- repeated target/bound/cooldown field permutations no longer read by production;
- synthetic cache placement permutations that native translation supersedes;
- observability-format permutations that do not affect user-visible API/schema.

## Workstream E — Request payload ownership implementation-detail reduction

After Plan 107, delete tests that assert the old recursive physical representation rather than the supported ownership invariant.

Protected replacement contracts:

- canonical payload cannot be mutated by provider transforms;
- first mutation establishes provider ownership/copy isolation;
- native no-transform path reuses original bytes;
- transformed path serializes final provider payload correctly;
- post-handoff buffer release does not break retry/finalization/streaming.

Delete tests whose only assertion is that internal payload types are `MappingProxyType`, tuples, or pass through old `_freeze`/`_thaw` helpers after those helpers are removed.

Do not add one test per deleted private helper.

## Workstream F — Observability/dashboard proportionality

Optional local observability should be tested for contract, not exhaustive formatting internals.

Retain:

- stable API/schema fields actually consumed by the dashboard/operator tools;
- empty/sparse data behavior;
- bounded retention/filtering where relevant;
- privacy/redaction invariants;
- one representative formatting/rendering path if user-visible.

Consolidate/delete:

- every intermediate dictionary key ordering;
- repeated label/formatting variants that Pydantic/type checking already constrain;
- internal span/warning shape fields with no supported external consumer;
- historical metric field tests for removed optional tuning modes.

## Workstream G — Unit/integration duplication audit

For a production invariant covered both by low-level unit tests and a deterministic integration/real-runtime test:

- keep the integration test when it exercises the actual seam and remains fast/deterministic;
- keep a unit test only for branches that would be difficult to diagnose or trigger at integration level;
- remove unit tests that merely reassert the integration setup's internal steps;
- do not delete all low-level tests for critical database/stream ownership just because one smoke path exists.

Focus this audit on the Plan 104–108 changed modules and the largest test clusters; do not sweep every repository file indefinitely.

## Workstream H — Deterministic concurrency simplification

Search for repeated iteration/stress loops:

```bash
rg -n 'for .*range\((10|20|25|50|100|1000)|sleep\(|repeat|soak|stress' tests
```

For race invariants:

- prefer `asyncio.Event`, barriers, explicit injected gates, and task ordering;
- retain one/few deterministic schedules per distinct race;
- remove repeated loop-count variants when they are not finding a distinct branch;
- do not replace deterministic coordination with sleeps.

The existing high-concurrency manual reproducer may remain as a diagnostic script; it does not need duplicated retained tests.

## Workstream I — Fixture/helper deletion

After test deletion:

- remove fixtures/helpers imported only by deleted tests;
- collapse provider/transcoder builders that differ only by obsolete feature flags;
- keep one real-runtime upstream mock toolkit rather than parallel historical variants;
- keep helpers local when used by one file;
- do not create a new general test framework as a consolidation exercise.

Run `rg`/static import checks before deleting shared helpers.

## Workstream J — Test naming/taxonomy cleanup only when touched

Remove historical plan/phase terminology from active test names only when those tests are already being edited for semantic consolidation.

Prefer names like:

```text
test_failed_request_does_not_poison_next_dispatch
test_parallel_tool_disable_maps_openai_to_anthropic
test_provider_transform_cannot_mutate_canonical_payload
```

Do not churn thousands of test names merely for style.

## Workstream K — Verification while deleting

For each semantic cluster:

1. identify surviving canonical tests;
2. run them before deletion when practical;
3. delete/consolidate redundant cases/helpers;
4. rerun surviving canonical tests;
5. run directly adjacent integration tests if helper boundaries changed.

At end run:

```bash
uv run pytest --collect-only -q
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short --maxfail=1
uv run eggpool --config config.example.toml check-config
uv run eggpool --config config.sbc.example.toml check-config
```

Then run a curated union of protected routing/failure-isolation/streaming/database/rehash/transcode/config tests.

One full retained-suite run is optional if practical. If attempted and a stale test fails, classify whether it represents a real supported invariant before changing production code. Do not restore obsolete runtime behavior solely to satisfy historical tests.

## Information-only closure metrics

Record:

- immediate pre-Plan109 collected count;
- final collected count;
- number/names of semantic clusters consolidated;
- significant fixture/helper deletions;
- protected high-severity regressions explicitly checked.

Do not record or enforce:

- future maximum test count;
- minimum deletion percentage;
- coverage percentage threshold;
- runtime threshold.

The final collection count must be lower than the immediate pre-Plan109 baseline unless the plan discovers that every candidate case is a distinct protected contract; if that exceptional outcome occurs, do not mark the plan complete without documenting why the review's identified redundant clusters were not actually redundant.

## Documentation

Update `AGENTS.md`/development skill only if test directories, commands, or supported helpers materially change.

Do not create a new testing-policy document. Existing planning policy already states that completed plans must not create permanent plan-numbered test suites/evidence formats.

## Closure evidence

- Immediate pre-consolidation collection: **8,370 tests**.
- Final collection: **8,233 tests** (**137 fewer**); no count floor, ceiling, or
  future percentage target was introduced.
- Consolidated clusters: duplicate safe-compression replay/production suites;
  the broad cache/compression replay matrix and its now-unused harness;
  retained soak traffic; stale provider-payload representation coverage; and
  repeated 100-iteration reload schedules.
- Retained fixture support is limited to the sanitization linter. Focused
  compression, cache-boundary, transcoder, routing, privacy, streaming,
  database, and reload contracts remain in their owning suites.
- A stale upstream-authoritative suppression scaffold was repaired with the
  generation-owned finalization supervisor. The transcoder warning catalogue
  was corrected for four Plan 106 cache-loss kinds.
- Protected union: **1,061 passed**.
- CI-equivalent checks: Ruff format/check, Pyright, 14 smoke tests, and both
  example/SBC `check-config` commands passed.
- Implementation commit: `9f1b898`.

## Acceptance criteria

- [x] Immediate pre-consolidation collection count is recorded for information only.
- [x] Consolidation is organized around semantic contracts, not arbitrary file size or a target test count.
- [x] Final retained collection count is lower than the immediate pre-Plan109 baseline, with no future count gate established.
- [x] High-value routing/account failure-isolation coverage remains.
- [x] A failed/malformed provider request poisoning subsequent requests remains directly covered.
- [x] Pre-handoff retry/post-handoff no-retry and streaming completion/cancellation/EOF coverage remains.
- [x] Database transaction ownership, ambiguity/fail-closed, finalization convergence, startup reconciliation, and migration compatibility remain covered.
- [x] Valid/invalid rehash generation publication and finalization-supervisor ownership remain covered.
- [x] Plan 105 structured-output/strict-tool/parallel-tool/reasoning contracts remain covered.
- [x] Plan 106 native prompt-cache mapping and explicit lossy cases remain covered.
- [x] Plan 104 auth/non-loopback/redaction regressions remain covered.
- [x] Plan 107 request ownership/original-byte/body-limit/buffer-release contracts remain covered.
- [x] Tests for removed Plan 108 dormant compression/tuning surfaces are deleted rather than preserving dead production compatibility.
- [x] Old recursive freeze/thaw representation tests are removed if those internals are removed; supported ownership invariants remain.
- [x] Duplicate protocol/provider permutations are reduced without replacing them with unreadable Cartesian parameter matrices.
- [x] Optional dashboard/observability tests are proportionate to their supported external contract.
- [x] Repeated race/soak iteration tests are replaced by deterministic schedules or removed where canonical deterministic coverage already exists.
- [x] Orphaned fixtures/helpers are deleted.
- [x] The 14-test ordinary smoke behaviors remain represented.
- [x] `.github/workflows/ci.yml` remains materially unchanged in shape.
- [x] No coverage/test-count/shard/soak/benchmark/hardware/release infrastructure is added.
- [x] Surviving focused protected-contract suites pass.
- [x] Ruff, Pyright, smoke tests, and both config checks pass.

## Rejection conditions

Reject the implementation if:

- tests are deleted primarily to hit a numerical target;
- any previously observed high-severity routing/failure-isolation/stream/database/rehash regression loses all direct or stronger coverage;
- native transcode/cache regressions added by Plans 105–106 are removed merely because they increase count;
- one giant parameter matrix replaces simpler diagnosable semantic tests;
- deterministic race tests are replaced with sleeps or random retries;
- production compatibility code is restored solely to make stale tests pass;
- ordinary CI is weakened below format/lint/type/smoke;
- ordinary CI is expanded with full-suite/coverage/benchmark/soak/hardware gates;
- a new testing framework/dependency is added mainly to reduce file count.

## GPT-5.6 Luna implementation sequence

1. Read Plan 103, completed Plans 104–108, this plan, `AGENTS.md`, current smoke suite, and test/development guidance.
2. Capture immediate collection count and identify largest semantic clusters without committing an inventory artifact.
3. Mark the protected coverage list before deleting anything.
4. Start with tests for production surfaces removed by Plan 108 and old Plan 107 physical payload representation.
5. Consolidate transcode/cache capability matrices around final semantic mappings.
6. Reduce optional observability/dashboard formatting permutations.
7. Audit unit/integration duplication in changed modules and keep the strongest deterministic contract coverage.
8. Replace repeated race loops with deterministic scheduling only where semantically equivalent.
9. Remove orphaned helpers/fixtures.
10. Run surviving cluster tests after each deletion batch.
11. Run final collection count, protected focused union, and ordinary repository gate; optional full suite only if practical.
12. Record implementation SHA, before/after information-only counts, clusters removed/consolidated, protected regressions checked, and exact verification results in this plan.
13. Stop; do not create a permanent testing-reduction program.
