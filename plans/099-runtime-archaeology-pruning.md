# Plan 099 — Runtime Archaeology Pruning

Date: 2026-08-10
Status: complete
Parent roadmap: `plans/093-sbc-runtime-and-maintenance-simplification-roadmap.md`
Planning baseline: `ad7eee822f1dfb8c43dfbe20410c41009697cd7d`
Depends on:

- `plans/094-backup-io-and-sbc-profile.md`
- `plans/095-database-rollback-ownership.md`
- `plans/096-request-hotpath-allocation-reduction.md`
- `plans/097-request-persistence-roundtrip-reduction.md`
- `plans/098-analytics-index-write-amplification-audit.md`

## Purpose

Delete proven-dead runtime scaffolding, compatibility residue, obsolete observability constants, and test-only production seams that remain after repeated hardening/optimization phases, without redesigning active rehash, finalization, routing, or database ownership machinery.

This is a deletion/pruning plan. The success metric is a smaller and clearer ownership surface, not a new abstraction.

## Why this follows Plans 094–098

The preceding phases may remove or reshape backup, rollback, request-preparation, persistence, and index-related call paths. Pruning should happen after those changes so dead-code proof is based on the final runtime surfaces rather than temporary compatibility shims.

## Candidate areas

Candidates identified during review include, but are not limited to:

- `src/eggpool/runtime_dispatch.py`
  - span constants left behind by removed dispatch-writer/persistence phases;
  - exported names that are no longer recorded by production paths;
  - recorder compatibility paths that exist only for deleted callers.
- `src/eggpool/request/coordinator.py`
  - imports of span constants marked with `# noqa: F401` / pyright unused-import suppression;
  - stale comments/names referring to removed writer or lock arrangements;
  - helper functions with no call sites after earlier lifecycle consolidation.
- `src/eggpool/db/connection.py`
  - ownership/transaction markers proven unused by Plan 095's audit;
  - fault-injection attributes/methods that can be replaced by simpler localized test monkeypatching without weakening production visibility.
- runtime-generation/reload/finalization modules
  - compatibility fields or mirrors no supported path reads;
  - legacy wrappers retained after authoritative APIs replaced them.
- configuration models
  - deprecated knobs whose implementation has already been removed and which no longer have a supported compatibility contract.

These are **candidates**, not instructions to delete blindly.

## Governing constraints

1. Every deletion requires repository-wide call-site/search proof.
2. A name used by public configuration, CLI, external Python API, architecture contract, migration compatibility, or persisted data is not dead merely because current source has few references.
3. Do not redesign `ReloadTransaction`, runtime generations, `RequestFinalizationSupervisor`, failure effects, routing, or database lifecycle to reduce line count.
4. Do not remove defensive checks solely because tests cover the happy path.
5. Do not add a permanent dead-code dependency/tool such as vulture solely for this phase.
6. Do not delete diagnostic counters/fields that operators actively consume through `runtime-status`, `/api/stats/runtime`, or dashboard APIs without proving they are obsolete.
7. Prefer deleting entire dead compatibility branches over renaming/refactoring active code.
8. Preserve stable user-facing configuration where deprecation/removal would require a separate compatibility decision.

## Workstream A — Build a bounded deletion inventory

Search candidate modules for:

- unused-import suppressions (`F401`, `reportUnusedImport`);
- `deprecated`, `legacy`, `compatibility`, `Plan 0xx`, `Workstream`, removed writer/queue names;
- fields assigned in `__init__` but never read;
- methods referenced only from tests;
- wrappers whose only implementation delegates to a newer authoritative method;
- exported constants absent from production recorder call sites.

For each candidate record:

- symbol/file;
- production call sites;
- test-only call sites;
- documented/API compatibility status;
- keep/delete rationale.

The inventory belongs in this plan's closure record; do not add a permanent registry.

## Workstream B — Dispatch observability cleanup

Review `runtime_dispatch.py` and all `SPAN_*` imports/call sites.

Delete span keys when all are true:

- no production code records the span;
- no supported runtime API promises the key as stable output;
- no dashboard/stat consumer depends on the key being present with zero samples;
- the span corresponds to removed architecture such as the dormant dispatch writer.

Then remove:

- coordinator/proxy imports made unused by the deletion;
- `F401`/pyright suppression added solely to retain the unused import;
- stale `ALL_SPAN_KEYS` entries and documentation references.

Do not remove active coarse `DispatchOverheadRecorder` / local-pre-upstream metrics merely because detailed spans are disabled by default.

## Workstream C — Database/test seam cleanup

Use Plan 095's call-site results to identify transaction state/test hooks that are no longer needed.

Rules:

- retain production diagnostics needed to distinguish commit/rollback ambiguity;
- retain private seams only where deterministic testing cannot reasonably patch the existing callable boundary;
- remove class-level/instance-level fault-injection hooks that duplicate ordinary monkeypatchable methods and have no production semantics;
- remove unused `_transaction_depth`-style state if repository-wide search proves it is neither read nor needed for ContextVar lifecycle cleanup.

When replacing a test seam, prefer monkeypatching a private method such as `_commit_connection()` over adding another production configuration field.

Do not reduce failure-injection coverage for commit/rollback ambiguity; only simplify how tests invoke it.

## Workstream D — Compatibility wrapper audit

Inspect repository/service wrappers retaining older bool contracts or legacy method names.

Delete only if:

- no production call site remains;
- no supported external Python API/docs advertise the method;
- tests can move directly to the authoritative typed-return API;
- deletion does not make migrations or stored data unreadable.

Examples may include repository methods that simply wrap newer `*_returning()` methods. Do not mass-delete these based on naming alone; classify each.

## Workstream E — Comment and documentation archaeology

Remove stale implementation-history comments that describe deleted architecture when they no longer explain a current invariant.

Keep comments that explain *why* a non-obvious safety boundary exists, even if they mention the plan that introduced it; prefer rewriting them in present-tense architectural language.

Targets include:

- old Plan/Phase references embedded in hot production modules;
- comments describing the removed dispatch writer;
- stale lock ordering descriptions;
- architecture/AGENTS notes for APIs removed in this plan.

Do not perform broad prose cleanup unrelated to changed code.

## Workstream F — Optional dependency trim decision

Review `granian[pname]` / `setproctitle` only as a small packaging simplification candidate.

Decision rule:

- if EggPool actively relies on process-title naming for systemd/operator workflows, keep it;
- if `pname` provides cosmetic naming only and no docs/tests/runtime contract depend on it, switch the core dependency to plain `granian` and document the small packaging simplification;
- do not force this change if there is uncertainty or compatibility cost.

No other core dependency replacement belongs in this plan.

## Workstream G — Focused verification

For every deleted symbol/API:

1. repository-wide search shows no unexpected remaining references;
2. directly affected focused tests pass;
3. runtime import smoke succeeds;
4. CLI startup/check-config path still imports correctly;
5. rehash and finalization focused tests are run only if their modules were touched.

A broad canonical-suite run is not required solely because code was deleted; use focused tests plus the ordinary smoke gate.

## Acceptance criteria

- [ ] A bounded candidate inventory is completed before deletion, with keep/delete rationale for each symbol changed.
- [ ] Obsolete dispatch span constants/imports/suppressions are removed where no production/runtime API consumer exists.
- [ ] Active coarse dispatch/local-pre-upstream diagnostics remain intact.
- [ ] Transaction state/hooks proven unused by Plan 095 and repository-wide search are removed or simplified without weakening commit/rollback ambiguity tests.
- [ ] Test-only production fault-injection scaffolding is reduced where ordinary monkeypatchable method boundaries provide equivalent deterministic coverage.
- [ ] Legacy repository wrappers are removed only when no supported production/external caller remains.
- [ ] Stale architecture-history comments are rewritten/deleted without removing present-tense safety rationale.
- [ ] `granian[pname]` is either retained with a concrete operational reason or reduced to `granian` with proof that process-title behavior is nonessential; no forced dependency churn occurs.
- [ ] Live rehash, finalization ownership, routing/failure isolation, and database fail-closed semantics are behaviorally unchanged.
- [ ] No new dead-code analysis dependency, abstraction layer, compatibility shim, or replacement framework is introduced.
- [ ] Focused tests and ordinary smoke/lint/type gate pass.
- [ ] Net production code/config surface for the touched archaeology areas decreases.

## Rejection conditions

Reject the implementation if:

- symbols are deleted based only on static-linter output without checking runtime/config/API consumers;
- active rehash/finalization state-machine steps are collapsed merely to reduce line count;
- failure diagnostics needed for indeterminate database outcomes are removed;
- a public/config compatibility surface is deleted without explicit evidence it is unsupported;
- a dead-code dependency or generalized cleanup framework is added;
- dependency replacement expands beyond the optional Granian pname decision;
- the patch becomes a broad rename/style refactor that obscures semantic review.

## Verification

Run the smallest directly affected unit/integration tests, then:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short --maxfail=1
uv run eggpool --config config.example.toml check-config
uv run eggpool --config config.sbc.example.toml check-config
```

## Implementation sequence for GPT-5.6 Luna

1. Read Plan 093, Plans 094–098 completion records, this plan, `AGENTS.md`, and candidate modules.
2. Build the candidate inventory with repository-wide searches before editing.
3. Remove the highest-confidence dead spans/imports/comments first.
4. Apply Plan 095's ownership-state/test-seam cleanup only where proven safe.
5. Audit legacy wrappers and remove only unsupported ones.
6. Make the Granian pname keep/remove decision from actual usage; do not force a change.
7. Run focused tests after each deletion cluster.
8. Run ordinary lint/type/smoke/config gate.
9. Record symbols kept/deleted and exact verification in this plan.
10. Stop; do not refactor active lifecycle state machines for aesthetics.

## Closure record

### Bounded deletion inventory

| Candidate | Evidence and disposition |
|---|---|
| `SPAN_SELECTION_*`, `SPAN_DISPATCH_PERSISTENCE_*`, `SPAN_POST_COMMIT_*`, and `SPAN_CLAIM_ROLLBACK` | Kept. Repository-wide production search found active coordinator recording calls; these are runtime diagnostics, not dormant writer residue. |
| `SPAN_DB_WRITE_*`, `DispatchOverheadRecorder`, `LocalPreUpstreamRecorder` | Kept. Active coarse and row-level timing remain part of the runtime diagnostics contract. |
| `Database._transaction_depth`, `_transaction_state`, `_TransactionState`, and public `safe_rollback()` | Already removed by Plan 095; repository-wide search confirmed no remaining callers. |
| `Database.TEST_INJECT_BEFORE_COMMIT_CALL`, instance injection fields, and `set_test_inject_*()` methods | Deleted. All callers were tests; tests now patch `_commit_connection()` or the SQLite rollback callable through `tests/support/database_faults.py`. Production commit/rollback ambiguity handling is unchanged. |
| Repository `*_returning()` compatibility wrappers | Kept. `finalize_if_pending()` and `ReservationRepository.release()` still have supported callers; no unsupported wrapper was deleted. |
| `granian[pname]` | Kept. `CHANGELOG.md` records that the serve path uses process naming; dependency removal would change an operator-visible workflow. |
| Historical phase/plan comments in active observability and coordinator paths | Narrowly rewritten in `runtime_dispatch.py` and `coordinator.py` to describe present-tense behavior. Broader architecture history was retained where it explains current contracts. |

### Verification

- Focused database fault, reload, and dispatch-span suites: 59 passed.
- `uv run ruff format --check src/ tests/ scripts/`
- `uv run ruff check src/ tests/ scripts/`
- `uv run pyright src/ scripts/`
- `PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short --maxfail=1`
- `uv run eggpool --config config.example.toml check-config`
- `uv run eggpool --config config.sbc.example.toml check-config`

The complete local CI-equivalent and configuration gate passed before the
implementation was committed. No new dead-code dependency, migration, runtime
state machine, or permanent benchmark gate was added.
