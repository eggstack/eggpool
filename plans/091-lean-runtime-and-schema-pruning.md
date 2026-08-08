# Plan 091 — Lean Runtime and Schema Pruning

Date: 2026-08-07
Status: complete
Parent roadmap: `plans/086-sbc-routing-and-storage-efficiency-roadmap.md`
Depends on:

- `plans/087-weighted-routing-semantics.md`
- `plans/088-pending-claim-load-publication.md`
- `plans/089-catalog-and-ping-write-reduction.md`
- `plans/090-finalization-roundtrip-reduction.md`

Planning baseline: `d6c49dea5ed800bfcd22d95fe8c7943a29590125`

## Purpose

Perform a narrow deletion/pruning pass after the routing and storage corrections have landed. Remove stale selection-lock scaffolding, reduce common-path imports/construction for disabled optional planes where this is straightforward, make an evidence-based decision about the dormant dispatch persistence writer, and establish a hard guardrail against further growth of the already-wide core `requests` schema.

This plan is intentionally reductive. It must not create replacement frameworks for anything it removes.

## Required reading

- `plans/086-sbc-routing-and-storage-efficiency-roadmap.md`
- Plans 087–090 and their recorded completion notes
- `AGENTS.md`
- `src/eggpool/request/coordinator.py`
- `src/eggpool/request/dispatch_writer.py`
- `src/eggpool/request/dispatch_intent.py`
- dispatch persistence repository/helpers
- `src/eggpool/app.py`
- `src/eggpool/runtime_manager.py`
- `src/eggpool/runtime_tasks.py`
- dashboard/model-info/metrics imports and startup construction paths
- `src/eggpool/request/finalizer.py`
- migrations defining the `requests` table
- existing performance/dispatch-writer tests and Plan 085 closure measurements

## Governing constraints

1. Delete only code proven unnecessary for the supported single-worker runtime.
2. Do not remove live rehash, request finalization ownership, crash recovery, protocol transcoding, dashboard functionality, or optional diagnostics merely because they are complex.
3. Do not add feature flags to preserve dead code paths.
4. Do not replace eager imports with a home-grown dependency injection/import framework.
5. Do not migrate the historical `requests` table solely to reduce column count.
6. Do not add a generic diagnostics sidecar now unless an active code change in this plan actually needs to persist new optional diagnostic data.
7. If the dispatch writer is retained, the reason must be based on existing repository evidence or a short manual comparison using existing tests/tools; do not create a permanent benchmark framework.

## Workstream A — Remove obsolete selection-lock scaffolding

After Plan 088 establishes the pending-claim invariant:

1. remove `RequestCoordinator._select_lock` if it has no remaining production use;
2. remove stale comments/docstrings claiming a broad `_select_lock` spans durable persistence and runtime publication;
3. rename no symbols unless necessary for correctness;
4. make the surviving `_selection_claim_lock` documentation match the final two-phase/provisional-load ownership model;
5. remove legacy timing aliases or compatibility comments only if no dashboard/API compatibility depends on them.

Search the full repository for `_select_lock`, `selection lock`, `selection_claim_lock`, and the old ordering language before deletion.

## Workstream B — Common-path optional import/construction audit

Inspect `src/eggpool/app.py`, runtime construction, and module import chains under the lean default profile.

Focus on optional planes that are disabled by default, including:

- model-info enrichment;
- routing trace writer/guard;
- detailed dispatch spans;
- readiness writable probe;
- event-loop lag monitor;
- automatic backups;
- update checker;
- DNS cache;
- dispatch persistence writer;
- optional compression/cache diagnostics beyond the always-supported transcoder core.

For each subsystem, distinguish:

- imported module only;
- lightweight config/type import required for validation;
- constructed runtime object;
- background task/client/socket/queue allocation.

Make only low-risk improvements with clear benefit:

- move implementation-only imports into the enabled branch when this avoids loading a large optional module tree;
- avoid constructing service objects that are used only by disabled dashboard/diagnostic routes;
- keep small shared config types/effectively-free imports eager when laziness would make code harder to follow;
- preserve startup error behavior for enabled features.

Do not chase individual kilobytes with fragile lazy-import tricks.

## Workstream C — DispatchPersistenceWriter evidence gate

The writer is disabled by default and implements a substantial process-owned queue/microbatching/cancellation/diagnostic path in parallel with direct transactional dispatch persistence.

Before deciding, inspect:

- every production configuration/reference to `dispatch_writer.enabled`;
- README/deployment docs for any recommended enablement;
- tests that compare direct persistence versus writer behavior;
- Plan 085 resource/performance notes;
- existing performance fixtures/reproducers for evidence that direct persistence is inadequate at the project's intended concurrency.

Decision rule:

### Remove the writer if all are true

- the shipped/default/SBC profiles keep it disabled;
- no supported feature requires it;
- no retained measurement demonstrates a material benefit at realistic intended load;
- direct persistence already keeps DB I/O outside the selection claim lock;
- removing it does not weaken correctness or failure isolation.

If removing it:

1. delete writer/intent-specific production code that has no other owner;
2. remove `DispatchWriterConfig` and `[dispatch_writer]` example/config handling;
3. remove writer-only diagnostics/runtime-metrics fields;
4. remove writer-only tests while preserving direct dispatch-persistence coverage;
5. remove stale architecture/docs references;
6. do not replace it with another batching queue.

### Retain the writer only if evidence is concrete

If existing evidence shows a material benefit that matters for intended SBC concurrency:

1. keep it opt-in and disabled by default;
2. document the exact use case and evidence succinctly;
3. remove any stale writer diagnostics/config knobs that are not required to operate it;
4. do not add new benchmarking or auto-tuning machinery.

Record the decision and evidence in this plan's closure notes.

## Workstream D — Core request schema freeze

The `requests` table already carries correctness/accounting fields plus many optional diagnostic fields for reasoning, segmentation, compression, cache behavior, and source metadata. A broad migration to split existing columns would create risk without an immediate operational payoff.

Establish the following policy in the closest architecture/data-model documentation:

- the existing core request schema is frozen for optional diagnostics;
- new columns are acceptable only for durable correctness/accounting facts required by request lifecycle, billing/usage truth, routing repair, or externally visible compatibility;
- future feature-specific diagnostics should use an existing sparse diagnostic/event table or a narrowly scoped sidecar table keyed by request id;
- disabled optional features should create no sidecar row;
- sidecar data must obey the existing retention/redaction policy;
- do not create a generic EAV/property store.

No database migration is required merely to state this rule.

If Plans 087–090 unexpectedly need new optional diagnostic fields, redesign those additions to use existing diagnostics rather than extending `requests`.

## Workstream E — Remove stale compatibility comments/paths exposed by the pass

While touching the named modules, remove only obviously obsolete scaffolding such as:

- comments referring to completed milestone/plan phases instead of current invariants;
- backward-compatible wrappers with no production/test caller after repository-wide search;
- duplicate app-state mirrors that are no longer consumed after runtime-manager migration, but only when all callers are proven absent;
- stale comments that claim database worker default is two when config default is one.

Do not perform broad documentation rewriting. Do not remove compatibility behavior merely because its comments are old.

## Workstream F — Focused tests

Required verification depends on the deletion decision, but at minimum cover:

1. canonical direct dispatch persistence still creates valid request/reservation/attempt identity;
2. persistence failure still releases Plan 088 provisional ownership and health probe;
3. lean-default startup leaves optional disabled subsystem objects absent/`None` as documented;
4. enabled optional subsystems touched by lazy import changes still construct and fail clearly when misconfigured;
5. rehash of supported fields still works with the final ownership shape;
6. no stale `_select_lock` path remains;
7. if dispatch writer is removed, config rejects or clearly reports the removed field according to normal strict-config behavior and docs/examples no longer advertise it;
8. if dispatch writer is retained, default startup still constructs none of its queue/task state;
9. request-schema migration history remains valid and no cosmetic request-table migration was added.

Do not add import-time snapshot tests across the whole package. A small direct assertion about disabled construction is sufficient.

## Verification

Run focused startup/runtime/coordinator/config/reload tests, then:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
uv run eggpool --config config.example.toml check-config
uv run eggpool --config config.sbc.example.toml check-config
```

If the dispatch writer decision is uncertain from retained evidence, run one short existing local performance fixture/reproducer in both modes. Record the command and result; do not add a new permanent benchmark.

## Acceptance criteria

- [x] stale unused `_select_lock` state is removed.
- [x] selection-lock documentation describes the final Plan 088 provisional-load invariant accurately.
- [x] lean-default optional subsystem construction remains absent/`None` where the audit found no safe common-path import reduction.
- [x] no new lazy-loading framework or dependency-injection layer is introduced.
- [x] dispatch writer is removed because no concrete intended-load benefit exists.
- [x] all writer config/runtime/test/docs scaffolding is deleted cleanly and direct persistence remains canonical.
- [x] architecture/data-model documentation freezes `requests` against further optional-diagnostic column growth.
- [x] no cosmetic migration is added to split historical request fields.
- [x] stale database-worker-default and milestone comments touched by this pass are corrected.
- [x] supported rehash and request finalization behavior remain intact.
- [x] focused and smoke verification pass.

## Closure notes

Dispatch writer decision: remove it. Both shipped profiles kept
`dispatch_writer` disabled, no supported feature required it, and direct
per-request persistence already ran outside `_selection_claim_lock`. The
existing performance fixture was run before deletion with:

```text
uv run pytest tests/perf/test_dispatch_baseline.py -m performance -s -q --tb=short --maxfail=1
16 passed in 3.01s
```

The fixture proved fewer SQLite transactions for synthetic 10/25/50-request
writer batches, but it used in-memory SQLite and did not demonstrate a
user-visible or intended SBC-load benefit. No shipped profile recommended the
writer, so that isolated transaction-count result did not satisfy the removal
exception. The writer, intent contracts, writer-only repository, config,
runtime metrics, tests, examples, and architecture references were removed;
direct transactional persistence remains canonical.

The pass also removed unused `_select_lock` state and legacy lock-span aliases,
updated the claim-lock invariant documentation, froze the historical
`requests` schema against optional-diagnostic column growth, and corrected the
database checker to recognize migration v52. No migration was added.

Local verification completed before commit:

- focused ownership/config/runtime/reload tests: 59 passed;
- database checker tests: 19 passed;
- retained dispatch performance tests: 6 passed;
- Ruff format/check, Pyright, smoke suite: passed;
- both shipped example configurations: `check-config` passed;
- the broad unit/integration/contract superset reached 6,314 passing tests before its known
  fixture failure; after correction, a second run passed the affected test and remained
  CPU-active beyond 30 minutes without emitting a result, so it was stopped as a
  non-CI superset. The required CI-equivalent checks below completed successfully.
- corrected the synthetic-cache route test to use the fixture's event loop via async
  `httpx.ASGITransport`; the focused route/metrics/span set passed 30 tests.

## Rejection conditions

Do not close this plan if:

- live rehash is removed as a shortcut;
- optional features stop working when explicitly enabled;
- a generic plugin/import/dependency framework is introduced;
- the request table is migrated solely for aesthetics;
- dispatch writer is retained only because deleting code feels risky, with no evidence/use case recorded;
- dispatch writer is removed despite existing evidence that direct persistence materially fails intended-load requirements;
- direct persistence regains DB I/O under the selection claim lock;
- CI or dependency scope expands.

## Implementation sequence for GPT-5.6 Luna

1. Read completion notes for Plans 087–090 and search all selection-lock references.
2. Remove stale `_select_lock` state/comments after proving no caller remains.
3. Audit lean-default import/construction paths and make only clear low-risk lazy/conditional changes.
4. Audit every dispatch-writer production/config/test/doc reference and existing evidence.
5. Apply the decision rule: delete the writer cleanly if unsupported by evidence; otherwise retain it narrowly and prune unnecessary surface.
6. Add the request-schema freeze rule to architecture/data-model docs without migrating existing rows.
7. Remove stale adjacent comments/wrappers only when repository-wide caller search proves them dead.
8. Run focused startup/coordinator/reload/config tests.
9. Run lint/type/smoke/config gates.
10. Record the dispatch-writer decision, evidence, commands, and outcomes; mark complete only after the lean default and supported opt-in paths are proven.
