# Plan 129 — Retained Test and Planning Surface Reduction

Date: 2026-08-14
Status: ready
Parent roadmap: `plans/122-post-audit-correctness-and-sbc-simplification-roadmap.md`
Planning baseline: `c17bb84af6d737a8408cbcce4d2746caedee36e8`
Depends on: Plans 123–128 final dispositions
Priority: P2 maintenance simplification
Execution target: GPT-5.6 Luna or comparable implementation model

## Purpose

Reduce EggPool's retained test and active planning maintenance surface after the
current correctness/architecture work settles, without weakening the already
appropriately small ordinary CI gate.

Plan 119 reduced a pre-existing corpus but the repository still retains thousands
of tests across historical phase/unit/integration/contract/perf/soak/support
clusters. Many are valuable regressions; many others encode implementation
shapes or planning-era variants that make refactoring and agent navigation more
expensive without protecting a distinct product invariant.

This plan is deletion/consolidation work. It does not target a numerical test
count and does not remove strong regressions merely because the corpus is large.

It also reduces *active navigation burden* from completed planning chains while
respecting the repository convention that historical completed plans remain
records. Git history already preserves every version; active docs/skills should
not force implementation models to traverse unrelated old closure chains.

## Governing constraints

1. Keep `.github/workflows/ci.yml` materially unchanged: one Python 3.11 job,
   Ruff format/check, Pyright, and `tests/smoke/`.
2. Preserve the existing smoke behavioral contract unless a stronger equivalent
   test replaces a case in the same smoke suite.
3. No test-count target, coverage target, execution-time target, or deletion
   percentage.
4. Do not add test sharding, scheduled full suite, coverage CI, benchmark CI,
   hardware CI, mutation testing, fuzz infrastructure, or a second test runner.
5. Preserve direct or stronger coverage for every known high-severity regression.
6. Prefer seam/behavior tests over private helper/layout/physical representation.
7. Deterministic concurrency tests remain barrier/Event driven; do not replace
   them with sleeps/retries/random loops.
8. Do not restore removed production behavior solely because stale tests expect
   it.
9. Do not mass-delete or rewrite historical completed plans for cosmetic
   consistency.
10. Do not build a plan registry, archive service, metadata database, or new
    planning framework.
11. Prefer deleting orphaned fixtures/helpers immediately when their last test is
    deleted.
12. Stop when the protected behavior union is compact and active navigation is
    truthful.

## Workstream A — Capture information-only current baseline

Run:

```bash
uv run pytest --collect-only -q
```

Record:

- total collected tests;
- smoke count;
- rough counts for `unit`, `integration`, `contract`, `perf`, `soak`, `live` if
  easy to derive from collection/path inventory;
- largest current clusters around request lifecycle/finalization/transcode/DB/
  rehash.

This is informational only. Do not commit a recurring count script/manifest.

Also inventory active planning/navigation references in:

- `AGENTS.md`;
- `.opencode/skills/architecture` and development guidance;
- `architecture/README.md`;
- README/docs that point implementers at historical plan chains;
- `plans/` only for identifying current versus completed records.

## Workstream B — Define protected behavioral coverage before deletion

Create a temporary working checklist, not a new permanent manifest.

### Routing/failure isolation

Retain direct or stronger coverage for:

- provider/model/account eligibility and priority tiers;
- same-tier fairness/weight behavior actually supported;
- request/token/active pressure visibility;
- already-attempted account exclusion;
- auth/quota/rate/server/transport/model/request-specific failure classification;
- bounded suppression/backoff and recovery;
- malformed/provider-rejected/local capability failure cannot poison later
  requests;
- half-open probe ownership if still part of current health architecture.

### Streaming lifecycle

Retain:

- retry only before response handoff;
- downstream-start truthfulness;
- first-byte/idle timeout classification;
- premature/malformed EOF;
- client cancellation;
- native and transcoded terminal SSE behavior;
- tool-call/result streaming adaptation;
- post-handoff failure finalization/no retry;
- stream generation lease lifetime.

### Protocol compatibility

Retain:

- OpenAI Chat Completions↔Anthropic Messages basic request/response translation;
- native same-protocol pass-through;
- tools and structured output behavior actually supported;
- reasoning/thinking semantics from Plan 123;
- prompt-cache provider capability gating;
- request canonical/provider isolation;
- PreparedTranscode reuse/recompute/retry;
- media translation/limits retained after Plan 124.

### Database/finalization

Coverage follows Plan 127/128 final contract.

If durable in-flight lifecycle is retained, keep:

- task-owned transaction semantics;
- commit/rollback ambiguity fail-closed;
- durable request/attempt/reservation convergence;
- finalization ownership/idempotence;
- startup crash recovery.

If simplified, remove tests for intentionally deleted crash-recovery semantics
and keep:

- task-owned DB semantics for surviving writes;
- ambiguity fail-closed for surviving correctness writes;
- process-local pressure/release;
- completed accounting/history;
- restart with no stale in-flight pressure.

### Rehash/runtime

Retain:

- valid generation publication;
- invalid config leaves active generation unchanged;
- generation lease/retirement;
- stream/finalization ownership retained by final architecture;
- control socket/rehash operator behavior that previously regressed.

### Security/config basics

Retain:

- API auth/non-loopback rule;
- trusted proxy attribution;
- credential/request-content redaction;
- body limit;
- config validation/startup agreement;
- removed config fields reject clearly.

## Workstream C — Consolidate implementation-detail request ownership tests

After Plans 123/124:

Delete or consolidate tests whose only purpose is asserting:

- exact internal helper call sequence no longer part of the supported contract;
- physical dict/list identity beyond the path-level isolation invariant;
- historical `deepcopy`/freeze/thaw implementations already removed;
- plan-number/workstream-specific counters with no operator consumer;
- duplicate no-op provider-payload cases that hit the same production branch.

Retain compact tests proving:

- source payload is not mutated;
- no-op native path reuses accepted bytes;
- cross-protocol source remains unchanged;
- changed provider generation is isolated;
- PreparedTranscode source remains unchanged;
- retry reuses frozen bytes;
- buffers can be released after handoff.

Do not retain one test per deleted private helper.

## Workstream D — Consolidate reasoning/capability matrices

After Plan 123, organize reasoning tests by semantic branch rather than model or
provider name permutations:

1. reasoning disabled (`none`/verified equivalent);
2. verified explicit effort→budget mapping;
3. legacy low/medium/high fallback where retained;
4. valid effort with unknown target mapping under reject;
5. valid effort with unknown target mapping under warn/loss;
6. invalid effort/client value;
7. native pass-through;
8. local failure has no provider penalty.

Keep provider-specific named cases only when the provider contract truly differs.
Avoid effort × provider × protocol × policy Cartesian matrices.

## Workstream E — Consolidate DB/finalization tests around final Plan 127 contract

This is expected to be one of the largest opportunities.

If Plan 127 retains durability:

- keep direct ambiguity/transaction/finalization/crash-recovery regressions;
- delete historical intermediate phase tests that would require the same
  production fix;
- prefer one deterministic integration seam plus a few low-level DB branch tests
  over mirrored unit suites for every state transition.

If Plan 128 simplifies durability:

- delete all tests whose only contract is explicitly removed pending identity or
  crash-recovery behavior;
- delete orphaned fixtures for removed finalization/compensation structures;
- retain strong completed-accounting/process-local-pressure/restart behavior;
- ensure no stale helper remains solely to keep old tests importable.

Do not preserve obsolete production compatibility wrappers for test convenience.

## Workstream F — Unit/integration/contract duplication

For each protected invariant covered in multiple layers:

- prefer deterministic integration/contract tests when they exercise the real
  seam quickly and provide better regression value;
- retain unit tests for low-level branches that are difficult to isolate or where
  failure diagnosis would otherwise be poor;
- delete unit tests that merely mock every collaborator and assert call order
  already proven by a seam test;
- delete integration duplicates that differ only in fixture spelling/provider
  names but hit the same production branch;
- keep live/network tests opt-in and sparse.

Do not create giant parameterized matrices that are harder to understand than the
original duplication.

## Workstream G — Perf/soak/live/support taxonomy cleanup

Inspect retained `tests/perf`, `tests/soak`, `tests/live`, `tests/support`, and
historical reproducer helpers.

Delete or move to concise manual docs when a test exists only as:

- one-time profiling evidence for already-closed work;
- a plan-era reproducer for deleted behavior;
- a soak/performance threshold not used by CI or current operators;
- a duplicate of current deterministic behavior tests;
- an unused fixture/toolkit.

Retain manual diagnostics that are demonstrably useful for known hard-to-reproduce
stream/provider issues, such as an existing high-concurrency stream reproducer,
without making them CI gates.

No new taxonomy/framework.

## Workstream H — Fixture/helper cleanup

After each cluster reduction:

- `rg` every fixture/helper before deletion;
- remove orphaned provider/config builders;
- remove plan-numbered helpers with no current semantic consumer;
- localize helpers used by only one file when doing so improves readability;
- retain one canonical deterministic upstream/mock toolkit rather than parallel
  historical variants;
- simplify fixture scope/teardown in line with Plan 125 findings.

Avoid a broad fixture rewrite unrelated to deleted tests.

## Workstream I — Active planning/navigation simplification

Historical completed plans may remain in `plans/`, but active guidance should
send implementers to current architecture and relevant active plan first.

Update existing guidance so:

- `AGENTS.md` continues to state proportional planning rules;
- architecture docs are the source of current behavior, not completed plan chains;
- current active roadmap/child plan references are clear while work is active;
- completed roadmap closure does not automatically create another closure plan;
- narrow defects can be implemented directly or with one focused plan according
  to existing proportionality guidance;
- no active doc says to read dozens of historical plans before touching a local
  subsystem;
- historical plans are clearly labeled historical reference if a navigation page
  exposes them.

Do not move/rename hundreds of plan files. Do not build an archive index unless a
small existing README/navigation file already needs one.

## Workstream J — Collection and protected-union verification

After reduction:

```bash
uv run pytest --collect-only -q
```

Record post count for information only and summarize major clusters removed.

Run a curated protected union covering the checklist above. It is acceptable for
the union to be several focused commands; do not create a permanent wrapper
script solely for this plan.

Then ordinary gate:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short --maxfail=1
uv run eggpool --config config.example.toml check-config
uv run eggpool --config config.sbc.example.toml check-config
```

A full retained-suite execution is optional/manual. If shared fixture changes are
broad enough to justify it, run once and record it; do not turn it into CI.

## Explicit acceptance criteria

- [ ] Pre/post collection counts are recorded for information only; no numerical
  target is introduced.
- [ ] Protected behavioral checklist is established before deletion.
- [ ] Known high-severity routing/provider-error-poisoning regressions retain
  direct or stronger coverage.
- [ ] Streaming pre/post-handoff, EOF/timeout/cancellation and generation-lifetime
  regressions remain covered.
- [ ] Plan 123 reasoning semantics and Plan 124 ownership/media contracts remain
  covered without provider-name Cartesian duplication.
- [ ] DB/finalization coverage matches the final Plan 127/128 product contract;
  intentionally removed semantics do not retain stale tests/helpers.
- [ ] Rehash generation publication/retirement and local security/config basics
  retain direct coverage.
- [ ] Unit/integration/contract duplicates are reduced where one failure would
  require the same production fix.
- [ ] Orphaned fixtures/helpers/reproducers for deleted behavior are removed.
- [ ] Useful manual diagnostic/live reproducers remain opt-in and are not promoted
  to CI.
- [ ] Active architecture/AGENTS guidance no longer requires traversing historical
  closure-plan chains for ordinary work.
- [ ] Historical completed plans are not mass-renamed/rewritten/deleted for
  cosmetic consistency.
- [ ] `.github/workflows/ci.yml` remains materially one Python 3.11
  Ruff/Pyright/smoke job.
- [ ] No coverage/perf/soak/hardware/full-suite scheduled/sharded CI is added.
- [ ] Curated protected union and ordinary gate pass.
- [ ] Implementation SHA, pre/post information-only counts, clusters removed,
  protected union commands/results, and planning-navigation changes are appended
  to this plan; no separate closure plan is created.

## Rejection conditions

Reject implementation if it:

- deletes tests to reach a target count;
- removes all direct coverage for a previously observed serious regression;
- replaces readable tests with a giant Cartesian parameter matrix;
- makes deterministic concurrency tests timing-dependent;
- restores dead production behavior to satisfy stale tests;
- weakens ordinary CI below Ruff/Pyright/smoke;
- adds full-suite/coverage/performance/hardware CI;
- creates a planning registry/archive service/policy framework;
- mass-deletes historical plans solely to make the directory smaller.

## Handoff sequence

1. Read Roadmap 122, final dispositions of Plans 123–128, this plan,
   `AGENTS.md`, current test tree, and architecture navigation.
2. Capture information-only collection baseline and protected behavior checklist.
3. Reduce request/reasoning ownership duplication first.
4. Reduce DB/finalization tests according to final durability contract.
5. Audit unit/integration/contract and perf/soak/live duplication.
6. Remove orphaned fixtures/helpers as clusters disappear.
7. Simplify active planning/navigation guidance without rewriting history.
8. Record post collection count, run protected union and ordinary gate.
9. Append closure evidence to this file and stop.
