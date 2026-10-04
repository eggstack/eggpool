# Persistence Milestone 011 — Post-M010 Roadmap and Documentation Reconciliation

Status: ready

Repository baseline: `9c1d86bc17a2a91b2ed8441bf23bd3e80b6603ed`

Source roadmap:

- `plans/subsystems/persistence-roadmap.md#milestone-011--post-m010-roadmap-and-documentation-reconciliation`

Corrective lineage:

- M009 closure: `plans/closure/persistence/009-status.md`
- M010 implementation plan: `plans/implementation/persistence/010-control-outbox-analytics-storage-architecture-investigation.md`
- M010 closure: `plans/closure/persistence/010-status.md`
- M010 implementation: `dfa2a2abfd70fc25d5d332d75ed5c6ba54117f22`
- M010 closure transition: `9c1d86bc17a2a91b2ed8441bf23bd3e80b6603ed`

Long-term requirements:

- `plans/003-planning-process.md` — current authority must reflect accepted closure; corrective passes use a new local milestone and preserve immutable historical records
- `plans/000-long-term-specification.md` — persistence integrity and evidence-gated performance claims

Applicable ADRs:

- None. M011 is documentation/planning reconciliation only.
- M010 proposed no ADR-0002, and M011 must not create or imply one.

Primary class: polish

## 1. Objective

Reconcile the remaining persistence roadmap lifecycle drift after M009 and M010 closed.

The authoritative registry, milestone-status table, implementation plans, and closure records are already correct. The residual defect is confined to current roadmap prose:

1. the detailed M009 milestone body still says `Status: active` although M009 is closed;
2. the detailed M010 milestone body still says `Status: closing` although M010 is closed/rejected;
3. the detailed M008 milestone body contains two adjacent status declarations expressing the same rejected disposition, creating redundant authority.

M011 must make the detailed milestone bodies agree with the accepted closures and add a narrow regression guard so a later closure cannot leave a stale body status behind.

## 2. Why prior verification missed this

M009 explicitly guarded M007/M008 lifecycle wording, and M010 closure correctly updated the registry, completion definition, dependency graph, and milestone-status table. Neither pass asserted that every detailed milestone body in the active persistence roadmap had exactly one lifecycle status consistent with the table/closure record.

The defect is therefore a documentation-control-surface gap, not an implementation or evidence failure.

M011 should extend the current persistence documentation guard rather than add a broad repository-wide grep.

## 3. Scope

### Required roadmap corrections

In `plans/subsystems/persistence-roadmap.md`:

- change M009 detailed-body status from `active` to `closed`;
- change M010 detailed-body status from `closing` to `closed — rejected under the current history/outage contract` or equivalent wording consistent with `plans/closure/persistence/010-status.md`;
- reduce M008 to one authoritative detailed-body status line;
- ensure the M008/M009/M010 detailed bodies, milestone table, dependency graph, and completion definition agree;
- add M011's own final status/closure linkage when M011 closes.

### Current-document sweep

Audit only current-authority persistence surfaces for lifecycle contradictions introduced by M009/M010 closure:

- `plans/subsystems/persistence-roadmap.md`;
- `plans/registry.md`;
- `architecture/deep-dive-database.md`;
- `architecture/deep-dive-background.md`;
- `architecture/persistence-control-projection-investigation.md`.

Correct only present-tense contradictions.

### Immutable history

Do not rewrite:

- `plans/closure/persistence/001-status.md` through `010-status.md`;
- M007/M008 physical qualification artifacts;
- historical implementation-plan rationale;
- legacy Plans 237–242;
- registry unblock-audit paragraphs that are explicitly historical snapshots.

Historical text may contain earlier `ready`, `active`, `blocked`, or `closing` states and must remain historical unless it is falsely presented as current authority.

## 4. Non-regressing invariants

- Zero production Rust/Cargo/schema/migration/API/config behavior change.
- Zero physical qualification artifact change.
- Zero accepted closure-record rewrite.
- M010 remains rejected under the current history/outage contract.
- No ADR-0002 is created or implied.
- Production remains one authoritative SQLite database/connection/gate/worker on WAL/NORMAL.
- The persistence roadmap remains top-level `active` because the foreground checkpoint/storage tail is still unresolved.
- No successor architecture or product policy is invented by M011.

## 5. Ordered work packages

### Work package A — Detailed milestone-body reconciliation

Correct M008/M009/M010 detailed milestone status declarations.

Required end state:

- M008 has exactly one detailed-body status and it is closed/rejected;
- M009 detailed body is closed;
- M010 detailed body is closed/rejected;
- status wording is compatible with each accepted closure record.

Do not modify the technical evidence, benchmark numbers, prototype findings, or exit-condition history.

### Work package B — Current-authority consistency sweep

Compare current lifecycle statements across:

- roadmap detailed bodies;
- roadmap milestone table;
- roadmap dependency graph;
- roadmap completion definition;
- registry active/ready/active-implementation/blocked/recently-closed tables;
- current architecture pages.

Correct only contradictions in current state.

The intended post-M011 control surface is:

- Persistence roadmap: `active`;
- M001–M010: closed/conditionally closed according to accepted records;
- M011: closed after this pass;
- dependency-ready persistence rows: none;
- active persistence implementation rows: none;
- blocked persistence rows: none;
- no registered successor;
- future split work remains unready until finite history retention and prolonged analytics-outage admission policy are explicitly defined.

### Work package C — Extend the targeted lifecycle guard

Extend the existing:

`tests/tooling/test_persistence_current_docs.py`

rather than creating a second overlapping test file.

At minimum assert:

- M008 detailed body has exactly one `Status:` declaration before M009;
- M009 detailed body says closed, not active/ready/closing;
- M010 detailed body says closed/rejected, not ready/active/closing;
- M009 and M010 closure links are present in the roadmap milestone table;
- registry contains no Persistence dependency-ready, active-implementation, or blocked row after M011 closure;
- persistence roadmap remains top-level active;
- current production WAL/NORMAL invariant remains present.

The guard must scope parsing to current persistence documents/sections and must not fail because historical registry narratives or immutable plans contain old lifecycle words.

### Work package D — Close M011 and audit successors

Write:

`plans/closure/persistence/011-status.md`

Closure must:

- record the exact status lines reconciled;
- prove zero production/source/schema diff;
- record focused and full tooling results;
- confirm no historical closure/artifact was changed;
- audit dependency-ready/active/blocked persistence work;
- state that no successor is registered;
- state that future split work remains gated on an explicit retention/outage product contract.

## 6. Expected production changes

None.

M011 is Markdown plus a targeted tooling-test adjustment.

If implementation discovers a required production Rust, Cargo, migration, database, API, config, dashboard asset, release-workflow, or qualification-artifact change, stop and open a separate corrective plan.

## 7. Storage, protocol, migration, concurrency, and failure effects

None.

This pass does not:

- open SQLite;
- alter checkpoint behavior;
- alter WAL mode;
- add a database connection/task;
- change backup/restore;
- change request publication/finalization;
- change retention;
- change dashboard freshness;
- change failure/recovery semantics.

The only failure behavior added is a static tooling assertion that rejects future current-document lifecycle drift.

## 8. Security and privacy

No new sensitive data.

The tooling guard reads fixed repository documentation paths only.

Do not copy physical artifact payloads or runtime identities into the guard; closure links and lifecycle status text are sufficient.

## 9. Required verification

~~~bash
uv run --frozen ruff format --check tests/tooling/
uv run --frozen ruff check tests/tooling/test_persistence_current_docs.py
uv run --frozen pytest tests/tooling/test_persistence_current_docs.py -q
uv run --frozen pytest tests/tooling/ -q --tb=short --maxfail=1
uv run --frozen python scripts/validate_release_docs.py
git diff --check
~~~

No Rust build is required if the final implementation diff is strictly Markdown plus `tests/tooling/test_persistence_current_docs.py`.

If any Rust/Cargo/migration/runtime file changes, stop and re-plan rather than silently broadening M011.

## 10. Acceptance criteria

M011 closes only when:

- M008 has one and only one detailed-body status declaration;
- M009 detailed body agrees with its closed closure record;
- M010 detailed body agrees with its closed/rejected closure record;
- roadmap table/dependency graph/completion definition remain consistent;
- registry shows no ready/active/blocked persistence implementation work after closure;
- current architecture pages do not contradict M010's rejected split disposition;
- production WAL/NORMAL and single-authority invariants remain explicit;
- targeted lifecycle guard passes;
- full tooling suite passes;
- no production source/schema/config/API/artifact change exists;
- no immutable closure record or physical artifact changed;
- no ADR-0002 exists;
- successor audit records that the persistence tail remains unresolved but no implementation is currently dependency-ready.

## 11. Stop conditions

Stop and report instead of widening scope if:

- the roadmap/registry disagreement reflects a real unclosed implementation requirement;
- M010 closure evidence conflicts with its implementation/prototype;
- a current architecture page claims behavior different from production code;
- fixing the inconsistency requires changing persistence architecture or product policy;
- an accepted closure record would need rewriting;
- a new successor must be designed to make the docs coherent.

Those findings require a new bounded plan.

## 12. Closure evidence required

`plans/closure/persistence/011-status.md` must contain:

- baseline and implementation commit;
- exact current-authority files changed;
- before/after lifecycle statuses for M008/M009/M010;
- guard changes and focused result;
- full tooling result;
- release-doc validator result;
- zero-production-diff proof;
- immutable-history/artifact confirmation;
- ADR inventory confirmation;
- severity-tagged residual findings;
- roadmap disposition;
- successor/unblock audit.

## 13. Handoff note

This is a terminal documentation corrective, not another persistence experiment.

Do not use M011 to choose a retention policy, reopen the control/outbox/analytics split, retune SQLite, remove qualification instrumentation, or create ADR-0002. The technical state is already settled through M010; M011 makes the current planning surface accurately reflect it.
