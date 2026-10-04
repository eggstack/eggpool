# Persistence Milestone 009 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/persistence/009-m008-documentation-reconciliation-corrective-pass.md`

Source subsystem roadmap:

- `plans/subsystems/persistence-roadmap.md#milestone-009--m008-documentation-reconciliation-corrective-pass`

Repository baseline reviewed: `f02b5b8484a0d25ad29d7bdf0dec5c178b703d26`

Implementation commits:

- `4bff728c` — reconcile current persistence lifecycle documentation, add the focused regression guard, and activate M009.
- This closure commit — mark M009 closed and audit dependency readiness.

## 1. Executive finding

M009 is closed. Current persistence roadmap and architecture documentation now state that M007 and M008 were rejected on Pi 5/MMC evidence, production remains one SQLite connection/gate/worker on WAL/NORMAL, and no storage split is authorized by those outcomes. A targeted guard pins these statements and preserves links to both physical closure records. M008 measurements, accepted closure evidence, and qualification artifacts were not changed.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| M008 roadmap body agrees with its closed/rejected table, closure, and registry | `plans/subsystems/persistence-roadmap.md` M008 milestone body and §12 | pass | Rejection and production WAL/NORMAL disposition stated. |
| No current architecture page describes M007 as pending | `architecture/deep-dive-database.md`; `architecture/deep-dive-background.md` | pass | Both now link the M007 and M008 closure records. |
| Production single-connection WAL/NORMAL invariant remains explicit | Both persistence deep dives and roadmap | pass | No runtime or storage behavior changed. |
| Lifecycle regression guard covers current-authority wording and links | `tests/tooling/test_persistence_current_docs.py` | pass | Focused result: 3 passed. |
| Current-authority persistence lifecycle sweep completed | `plans/registry.md`, `plans/subsystems/persistence-roadmap.md`, `architecture/deep-dive-database.md`, `architecture/deep-dive-background.md` | pass | Other M007/M008 references were historical evidence or explicitly qualification behavior, not pending lifecycle claims. |
| M008 evidence and historical records remain immutable | Diff name audit; M008 closure and artifacts absent from changes | pass | No closure, plan 008, or qualification artifact was edited. |
| Zero production/source/schema change | `git diff --name-only` for implementation range | pass | Only Markdown and one tooling regression test changed. |

## 3. Production implementation evidence

No production Rust, Cargo metadata, migration, embedded asset, API, configuration, or qualification artifact changed. Documentation corrections are in the persistence roadmap and database/background deep dives. The targeted Python test asserts M008's rejected roadmap status, M007/M008 closure discoverability, the production invariant, and removal of stale present-tense registered-experiment wording.

## 4. Verification executed

### Commands run

```bash
uv run --frozen ruff format tests/tooling/test_persistence_current_docs.py
uv run --frozen ruff format --check tests/tooling/
uv run --frozen ruff check tests/tooling/test_persistence_current_docs.py
uv run --frozen pytest tests/tooling/test_persistence_current_docs.py -q
uv run --frozen pytest tests/tooling/ -q --tb=short --maxfail=1
git diff --check
```

### Results

- Ruff formatting check: pass, 22 files already formatted.
- Focused Ruff check: pass.
- Focused pytest: 3 passed.
- Full tooling pytest: 164 passed, 3 skipped in 24.72 seconds.
- `git diff --check`: pass.
- `uv.lock` was modified as an incidental local effect of the tooling invocation and restored; it is not part of the implementation.

## 5. Invariant review

- One production SQLite connection/gate/worker on WAL/NORMAL: preserved; no production source changed.
- M007 and M008 remain qualification-only, rejected outcomes: documented and guarded.
- No M008 metrics or accepted evidence were rewritten: confirmed by changed-file review.
- M009 does not select a future persistence architecture: preserved.

## 6. Failure and recovery review

Not applicable to runtime behavior; this milestone changes current-authority documentation and a local static guard only. The guard has no database, filesystem mutation, network, or runtime side effects.

## 7. Migration and compatibility review

No schema, migration, backup, configuration, API, or compatibility change.

## 8. Security review

No credential, request, provider, or other sensitive content was added. The regression guard reads only fixed repository documentation paths.

## 9. Documentation and operations

Updated `plans/subsystems/persistence-roadmap.md`, `architecture/deep-dive-database.md`, and `architecture/deep-dive-background.md`. Added `tests/tooling/test_persistence_current_docs.py`. M008's physical corpus and closure remain the authoritative evidence sources.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| none | No unresolved M009 finding | None | None |

## 11. Roadmap disposition

Milestone closed. M009 reconciles the current documentation and does not alter M010's hard dependency. M010 is dependency-ready and may proceed; its soft relationship to M009 is satisfied. There is no other registered blocked Persistence work to promote. Production storage remains unchanged and any split still requires an accepted ADR and separate implementation and physical-qualification plans.

## 12. Registry updates

`plans/registry.md` removes M009 from active work, records this closure, and retains M010 as ready with its production-split restriction. `plans/subsystems/persistence-roadmap.md` marks M009 closed and records the closure link. Applied in the same closure commit.
