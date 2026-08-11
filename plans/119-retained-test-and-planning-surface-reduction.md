# Plan 119 — Retained Test and Planning Surface Reduction

Date: 2026-08-11
Status: ready
Parent roadmap: `plans/113-sbc-hotpath-reduction-and-protocol-clarity-roadmap.md`
Planning baseline: `6f4df9bd42b5ca336d3da5ef458ab1793e515185`
Depends on:

- `plans/114-provider-payload-copy-on-write.md`
- `plans/115-prepared-transcode-ownership-reduction.md`
- `plans/116-request-estimation-and-ingress-efficiency.md`
- `plans/117-provider-cache-dialect-correctness.md`
- `plans/118-optional-runtime-surface-and-dependency-reduction.md`

## Purpose

Reduce two forms of repository process overhead after the production surface has settled:

1. retained test duplication and implementation-detail coverage around the request/transcode/cache/compression/DNS surfaces changed by Plans 114–118;
2. planning-document escalation where small local corrective defects repeatedly produce roadmap → closure → corrective closure → final corrective closure chains.

Ordinary CI is already appropriately small and should remain unchanged. This plan does not weaken that gate and does not target an arbitrary test count.

The desired state is fewer tests per distinct production invariant and a planning convention proportional to change risk/size.

## Current baseline/context

Roadmap 103's Plan 109 recorded a reduction from 8,370 to 8,233 collected tests, while ordinary CI remained a 14-test smoke suite plus Ruff/Pyright. The retained corpus therefore still contains substantial historical/phase-by-phase duplication even though CI no longer pays that cost on each commit.

The project planning history also contains multiple generations of closure/corrective plans for narrow defects. Detailed planning remains useful for multi-file architectural work, but requiring the same machinery for a two-helper correctness fix creates maintenance overhead without guaranteeing defect prevention.

## Governing constraints

1. Keep `.github/workflows/ci.yml` materially unchanged: one Python 3.11 job, Ruff format/check, Pyright, and `tests/smoke/`.
2. Preserve the 14 smoke behaviors unless directly replaced by stronger equivalent tests with the same ordinary CI contract.
3. Do not add coverage thresholds, test-count floors/ceilings, test sharding, scheduled full-suite CI, benchmark CI, soak CI, hardware CI, mutation testing, fuzz infrastructure, or release gates.
4. Do not add a new test framework/dependency to reduce test count.
5. Do not delete tests primarily to reach a number.
6. Preserve direct or stronger regression coverage for previously observed high-severity failures.
7. Prefer behavioral contracts over private physical representation.
8. Prefer compact parameterization only when cases truly share one production branch/fix.
9. Do not replace readable tests with giant Cartesian matrices.
10. Deterministic concurrency tests must remain deterministic; do not replace explicit events/barriers with sleeps/random retries.
11. Do not restore deleted production behavior solely to satisfy stale tests.
12. Planning simplification must not eliminate detailed plans for genuinely multi-phase or high-risk changes.
13. Do not build a planning registry/service/policy engine. A short convention in existing contributor/agent guidance is enough.
14. Historical completed plans remain historical records; do not mass-delete or rewrite them.
15. Do not create another permanent test/planning inventory artifact.

## Protected coverage that must survive

### Routing and failure isolation

Retain direct or stronger coverage for:

- account/provider eligibility and priority-tier selection;
- weighted request/token load and pending-claim visibility;
- already-attempted-account exclusion;
- provider/account/model suppression and bounded recovery;
- transport/auth/quota/rate/model/request-specific failure classification;
- one malformed/provider-rejected request cannot poison later requests;
- local preparation/transcode errors do not penalize provider/account health.

### Streaming lifecycle

Retain:

- retry only before response handoff;
- ASGI response-start handoff truthfulness;
- first-byte/idle timeout classification;
- premature/malformed EOF;
- client cancellation;
- native and transcoded SSE terminal markers;
- tool-call/result streaming adaptation;
- post-handoff failure finalization with no retry.

### Database/finalization correctness

Retain:

- task-owned transaction semantics;
- child/unrelated task cannot execute inside another transaction;
- commit/rollback ambiguity fails closed;
- request/reservation/attempt durable convergence;
- duplicate/idempotent finalization;
- generation-owned terminal command ownership;
- startup crash reconciliation;
- supported migration/checksum/fresh-schema compatibility.

### Rehash/runtime generations

Retain:

- valid generation publication;
- invalid config leaves active generation unchanged;
- generation lease/retirement;
- retained stream lease lifetime;
- finalization supervisor capacity/ownership invariants.

### Protocol compatibility

Retain after Plans 114–118:

- OpenAI↔Anthropic basic request/response translation;
- tools/strict/parallel-tool behavior actually supported;
- structured-output mapping and explicit lossy cases;
- thinking/reasoning capability mapping/rejection;
- native cache semantics and provider-extension gating from Plan 117;
- same-protocol pass-through;
- generic compatible-provider conservative behavior;
- request payload canonical/provider isolation;
- native original-byte reuse/no-op streaming normalization;
- prepared-transcode reuse/recompute contract.

### Local security/config basics

Retain:

- server API auth/non-loopback rule;
- trusted proxy attribution;
- credential/request-content log redaction;
- body-size limit;
- check-config/startup agreement;
- deleted optional config fails clearly rather than silently changing behavior.

## Workstream A — Capture immediate collection baseline

After Plans 114–118 complete, run:

```bash
uv run pytest --collect-only -q
```

Record only:

- total collected tests;
- smoke count;
- rough largest touched-surface clusters if easy to determine.

Do not create a committed script or manifest to track test counts.

The final count should normally be lower because this plan explicitly removes obsolete representation/config tests, but no percentage or numeric target is acceptance criteria.

## Workstream B — Delete Plan 114/115 physical-representation tests

Remove/consolidate tests whose only purpose is asserting old internals such as:

- `MappingProxyType`/tuple physical freezing;
- recursive freeze/thaw helper output types;
- unconditional full-copy/deepcopy mechanics;
- old mutation-generation behavior where transform invocation implied mutation;
- private helper identities that no longer form the supported contract.

Retain compact behavioral ownership tests proving:

- canonical payload cannot be mutated by provider transforms;
- no-op streaming normalization preserves original-byte fast path;
- first real provider mutation is isolated;
- safe compression/adopted transforms do not leak mutation into canonical state;
- prepared transcode source remains unmodified;
- provider body generation/serialization remains correct.

Do not create one replacement test for every deleted private helper.

## Workstream C — Consolidate request-estimation/preflight tests

After Plan 116:

- keep one/few tests proving single canonical estimate reuse;
- keep boundary context-limit behavior;
- keep translated upstream-limit behavior;
- keep reservation estimate semantics;
- delete duplicate unit tests that simply reproduce the same recursive estimator behavior through multiple call sites;
- consolidate repeated tool-padding cases if they hit the same estimator branch.

Do not weaken tests for actual model-limit rejection.

## Workstream D — Consolidate cache dialect/capability matrices

After Plan 117, organize cache tests around semantic branches:

1. generic provider/unknown capability — no extension emitted;
2. verified provider extension — supported mapping emitted;
3. malformed source marker/control — bounded loss;
4. unsupported placement/TTL — explicit loss;
5. native source intent precedence;
6. same-protocol standard field pass-through;
7. privacy/redaction.

Delete repeated provider-name permutations where the capability state is identical.

Provider-specific tests survive only when provider contracts genuinely differ.

Avoid a protocol × provider × capability × loss-policy × content-placement Cartesian matrix.

## Workstream E — Delete tests for optional surfaces removed by Plan 118

If Plan 118 removes:

- compression tuning;
- reserved placement modes;
- synthetic cache controls;
- DNS cache;
- deprecated aliases;
- `pname` extra behavior;

then delete tests/fixtures/helpers whose sole purpose is preserving those removed surfaces.

Do not leave production compatibility wrappers solely because old tests import them.

For retained optional features, keep one disabled-path test and the smallest set of supported behavioral contracts.

## Workstream F — Observability/dashboard proportionality in touched surfaces

For metrics/runtime-status/dashboard fields touched by Plans 114–118:

Retain:

- fields actually consumed by a stable CLI/dashboard/operator view;
- empty/sparse data behavior;
- bounded retention/cardinality where relevant;
- privacy/redaction.

Delete/consolidate:

- exact internal dict ordering;
- every formatting variant;
- recommendation/DNS detail fields deleted from production;
- plan/workstream-numbered intermediate diagnostics;
- duplicate snapshots that would require the same production fix.

Do not perform an unrelated dashboard rewrite.

## Workstream G — Unit/integration duplication

For touched invariants covered by both unit and deterministic integration tests:

- prefer the integration test when it exercises the actual seam quickly and deterministically;
- retain a unit test only for important branches that are hard to isolate at integration level;
- delete unit tests that only reassert intermediate internal calls already proven by a stronger seam test;
- retain low-level database/stream ownership tests where failures would be difficult to diagnose solely from smoke/integration coverage.

## Workstream H — Fixture/helper cleanup

After test deletion:

- remove orphaned fixtures/builders/harnesses;
- keep helpers local if used by one file;
- collapse duplicate provider/transcode config builders created for obsolete feature flags;
- retain one deterministic upstream/mock toolkit rather than historical variants;
- remove stale plan-numbered/replay helpers that no longer model a supported runtime surface.

Use `rg` before deleting shared helpers.

## Workstream I — Planning convention proportionality

Update existing `AGENTS.md` or the repository's existing development/planning guidance with a short proportionality rule.

The rule should distinguish:

### Detailed roadmap + child plans appropriate when

- work spans multiple architectural boundaries;
- ordering/dependencies matter across several implementation phases;
- failure can corrupt durable state or cross request/process ownership boundaries;
- broad protocol/provider semantics are being redesigned;
- a handoff requires independent phased implementation.

### One focused plan appropriate when

- a bounded multi-file corrective pass has clear acceptance criteria;
- one subsystem/boundary changes but execution still benefits from written handoff detail.

### Direct implementation / issue / concise notes appropriate when

- defect is local to one/few helpers;
- expected fix is small and deterministic;
- no new architecture/schema/dependency is introduced;
- existing tests/gates adequately protect the boundary.

A completed roadmap should not automatically require a new "closure plan" merely to run its existing acceptance criteria. Closure evidence belongs in the implementing plan unless a genuinely new defect/phase is discovered.

Do not prescribe plan numbers, file counts, approval stages, or mandatory closure documents.

## Workstream J — Historical plan hygiene without mass deletion

Do not delete completed plans wholesale. They are useful history.

Only fix active navigation/reference problems created by the new planning convention, such as:

- stale README/AGENTS wording that says every change requires a detailed plan;
- active links pointing to obsolete planning procedures;
- duplicated templates that mandate closure/corrective plan chains.

Do not rename hundreds of historical files.

## Workstream K — Verification while reducing

For each semantic cluster:

1. identify the surviving canonical behavioral test(s);
2. run them before deletion where practical;
3. delete redundant/obsolete tests/helpers;
4. rerun survivors;
5. run adjacent integration tests when shared fixtures change.

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

Then run a curated protected union for routing/failure-isolation/stream/database/finalization/rehash/transcode/cache/request-ownership/config behavior.

A full retained-suite pass is optional/manual.

## Closure metrics

Record for information only:

- pre/post collection count;
- semantic clusters consolidated/deleted;
- orphaned fixtures/helpers removed;
- protected high-severity suites/behaviors explicitly run;
- planning guidance changed.

Do not create future count/coverage/runtime thresholds.

## Explicit acceptance criteria

- [ ] Immediate pre-reduction collection count is recorded for information only.
- [ ] Final retained corpus is smaller/simpler around touched surfaces unless every candidate is proven to represent a distinct protected behavior; any exception is documented.
- [ ] No numerical deletion/coverage target is introduced.
- [ ] Tests asserting obsolete MappingProxyType/tuple/freeze-thaw physical representation are removed after Plan 115.
- [ ] Behavioral client/provider payload isolation coverage remains.
- [ ] Native original-byte/no-op streaming normalization coverage remains.
- [ ] Prepared-transcode reuse/recompute/retry correctness remains covered.
- [ ] Context-limit/reservation/transcoded-limit coverage remains.
- [ ] Cache dialect tests distinguish generic unknown versus verified provider extension without duplicate provider-name matrices.
- [ ] Tests for optional production surfaces deleted in Plan 118 are deleted rather than preserving dead compatibility code.
- [ ] High-severity routing/provider-error-poisoning regressions remain directly or more strongly covered.
- [ ] Pre-handoff retry/post-handoff no-retry and streaming EOF/cancellation coverage remain.
- [ ] Database transaction ownership, ambiguity/fail-closed, finalization convergence, crash recovery, and migration coverage remain.
- [ ] Rehash generation publication/retirement/finalization ownership coverage remains.
- [ ] Auth/non-loopback/trusted-proxy/redaction/body-limit basics remain.
- [ ] Deterministic concurrency tests remain deterministic and do not regress to sleeps/random loops.
- [ ] Orphaned fixtures/helpers from deleted tests are removed.
- [ ] The 14 smoke behaviors remain represented.
- [ ] `.github/workflows/ci.yml` remains materially one Python 3.11 Ruff/Pyright/smoke job.
- [ ] No coverage, benchmark, soak, hardware, full-suite scheduled, sharding, or release CI is added.
- [ ] Existing planning/agent guidance contains a concise proportionality rule allowing small corrective fixes to avoid roadmap/closure-plan chains.
- [ ] Planning simplification does not forbid detailed roadmaps for genuinely multi-phase/high-risk work.
- [ ] No planning registry/service/template framework is introduced.
- [ ] Protected focused union and ordinary gate pass.

## Rejection conditions

Reject the implementation if:

- tests are deleted primarily to hit a count;
- a previously observed high-severity regression loses all direct/stronger coverage;
- implementation-detail tests are deleted without behavioral ownership replacement where the invariant matters;
- a giant parameter matrix replaces readable semantic tests;
- deterministic race tests become sleep/retry based;
- production dead code is restored solely for stale tests;
- ordinary CI is weakened below format/lint/type/smoke;
- ordinary CI expands with full-suite/coverage/performance/hardware gates;
- planning simplification becomes a new multi-stage policy framework;
- historical plans are mass-deleted/rewritten for cosmetic consistency.

## Handoff sequence

1. Read Plan 113, completed Plans 114–118 and their closure records, this plan, current smoke suite, AGENTS/development guidance, and touched test clusters.
2. Capture immediate collection baseline.
3. Mark protected coverage before deleting anything.
4. Delete obsolete physical-representation and removed-feature tests first.
5. Consolidate request-estimation/cache capability matrices around final semantic branches.
6. Reduce touched observability/dashboard formatting internals.
7. Audit unit/integration duplication and orphaned helpers.
8. Add the concise planning proportionality convention to existing guidance.
9. Run post-collection count, protected union, ordinary gate, and optional full suite only if practical.
10. Record implementation SHA, before/after information-only counts, clusters removed, protected regressions checked, planning-guidance change, and exact verification results.
11. Stop. Do not create a recurring test-reduction/planning-governance program.
