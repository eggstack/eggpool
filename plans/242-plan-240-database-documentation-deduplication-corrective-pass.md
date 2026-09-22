# Plan 242 — Plan 240 Database Documentation Deduplication Corrective Pass

Date: 2026-09-22
Status: implementation handoff
Planning baseline: `961c0158eadd0a536d1ae02772bd910bbd93a566` (`main`, Eggpool 0.8.0)
Corrects: documentation state introduced alongside `524fc95614f3699d8c7a180b1c36265a986a99a4`
Related plans:
- `239-sqlite-publication-commit-checkpoint-phase-diagnostic.md`
- `240-passive-sqlite-checkpoint-production-follow-up.md`
- `241-eggfetch-0.2.0-adoption-and-provider-transport-requalification.md`

Priority: P3 documentation-state cleanup

## Purpose

Correct a documentation duplication in
`architecture/deep-dive-database.md` without changing any runtime, database,
provider, dependency, qualification, or planning behavior.

The current database deep dive contains three consecutive copies of:

```text
## Passive-checkpoint production follow-up (Plan 240, design only)
```

The three bodies are near-duplicates describing the same Plan 239 finding and
Plan 240 design-only handoff. The duplication is documentation debt only, but it
makes the current SQLite authority ambiguous and should be collapsed to one
canonical section.

This corrective pass must preserve the append-only planning record. Do not
rewrite Plans 239, 240, or 241 to hide how the duplicate arose.

## Current finding

At planning baseline `961c0158`:

- `architecture/deep-dive-database.md` contains three consecutive
  `Passive-checkpoint production follow-up (Plan 240, design only)` sections.
- the repeated blocks all state compatible intent, but differ slightly in
  wording;
- no production SQLite behavior was changed by the duplicate;
- `plans/240-passive-sqlite-checkpoint-production-follow-up.md` remains the
  authoritative design handoff and is still `Status: proposed`;
- Plan 240 explicitly does **not** authorize a production SQLite default or
  runtime change;
- Plan 241 is complete and its Eggfetch 0.2.0 adoption does not need reopening.

A targeted scan of the other current-authority files touched by the Plan 241
implementation found no equivalent duplicated Plan 240 block and no stale live
Eggfetch 0.1.7 dependency statement. Historical 0.1.7 references in completed
plans remain valid history.

## Scope

### Required source edit

Edit only:

- `architecture/deep-dive-database.md`

Collapse the three consecutive Plan 240 subsections into one canonical
subsection.

The retained section should preserve all material invariants represented across
the three current copies:

- Plan 239 classified the physical Pi/MMC tail as I1: foreground SQLite
  automatic-checkpoint work.
- Plan 240 is a separate production-design handoff and does not itself authorize
  a runtime/default change.
- the current production authority remains one SQLite connection/gate and one
  DB worker;
- WAL mode and `synchronous = "NORMAL"` remain unchanged;
- existing publication/finalization ownership remains unchanged;
- the existing maintenance-task passive checkpoint boundary remains the current
  production policy;
- any future checkpoint scheduling proposal must stay on the existing DB worker
  unless separately evidenced, rather than adding a second writer;
- a future candidate must preserve durability and bound WAL growth across
  restart, reload, backup, restore, and recovery;
- qualification diagnostics remain outside ordinary release behavior;
- a production default change requires the reviewed design and focused loopback
  evidence called for by Plan 240.

Prefer one concise paragraph or two short paragraphs. Do not preserve repeated
wording merely to retain every sentence verbatim.

### Cross-document sanity scan

After the edit, inspect the current documentation/guidance files that were
touched by the Plan 241 implementation:

- `README.md`
- `rust/README.md`
- `AGENTS.md`
- `architecture/overview.md`
- `architecture/deep-dive-providers.md`
- `architecture/deep-dive-database.md`
- `.opencode/skills/architecture/SKILL.md`
- `.opencode/skills/development/SKILL.md`
- `.opencode/skills/documentation/SKILL.md`

This is a verification step, not authorization to rewrite them.

Confirm:

1. the Plan 240 database section exists only once;
2. current Eggfetch authority remains `eggfetch-core =0.2.0` with
   `native-http1,tls-rustls`;
3. historical plans remain historical and are not edited;
4. no duplicate current-authority subsection introduced by the same landing is
   visible in these files.

If a second directly adjacent duplicate created by the same documentation
landing is found, it may be removed only when it is mechanically equivalent
documentation with no policy distinction. Any semantic conflict or unrelated
staleness requires a new plan rather than scope expansion.

## Canonical wording target

The final section should be equivalent in substance to:

```markdown
## Passive-checkpoint production follow-up (Plan 240, design only)

Plan 239 classified the Pi MMC tail as I1 (foreground SQLite automatic-
checkpoint work). Plan 240 is the separate production-design handoff and
authorizes no runtime change: the single connection/gate and DB worker, WAL
mode with `synchronous = "NORMAL"`, existing publication/finalization
ownership, and the pre-existing maintenance-task
`PRAGMA wal_checkpoint(PASSIVE)` boundary remain the production policy. Any
future scheduling proposal must preserve durability, stay on the existing DB
worker unless separately evidenced, bound WAL growth across
restart/reload/backup/restore/recovery, keep qualification diagnostics out of
ordinary release builds, and arrive with the focused loopback evidence required
by Plan 240 before a default change is considered.
```

Exact prose may be tightened, but none of those invariants should be lost.

## Explicit non-goals

This pass does not authorize:

- changing `rust/src/db/`;
- changing SQLite pragmas or defaults;
- setting `wal_autocheckpoint`;
- adding, removing, or rescheduling maintenance tasks;
- adding a database connection, worker, or writer;
- changing publication/finalization ownership;
- changing backup, restore, reload, recovery, or shutdown behavior;
- changing qualification tooling or diagnostic features;
- changing Plan 240 status or implementing Plan 240;
- modifying Eggfetch 0.2.0 or provider transport;
- changing `rust/Cargo.toml` or `rust/Cargo.lock`;
- rewriting Plans 239–241;
- broad documentation reformatting.

If implementation requires any production Rust or Cargo change, stop: that is
outside this corrective pass.

## Verification

Because this is documentation-only, do not run the full Rust workspace merely
for ceremony. Use the repository documentation checks:

```bash
uv run python scripts/validate_release_docs.py
uv run python scripts/validate_runtime_package_boundary.py
git diff --check
```

Also run targeted textual checks:

```bash
# Must return exactly one heading.
rg -n '^## Passive-checkpoint production follow-up \(Plan 240, design only\)$' \
  architecture/deep-dive-database.md

# Review all current Plan 240 references after cleanup.
rg -n 'Plan 240|passive.checkpoint' \
  architecture/deep-dive-database.md AGENTS.md architecture/overview.md \
  .opencode/skills README.md rust/README.md

# Current non-historical dependency authority must remain 0.2.0.
rg -n 'eggfetch-core.*0\.2\.0|Eggfetch 0\.2\.0' \
  README.md rust/README.md AGENTS.md architecture .opencode/skills
```

A docs-only push may be ignored by ordinary CI under the repository's current
path filters; that is expected. Do not alter CI just to obtain a workflow run
for this cleanup.

## Acceptance criteria

- [ ] `architecture/deep-dive-database.md` contains exactly one Plan 240
      passive-checkpoint subsection.
- [ ] The remaining subsection preserves all current SQLite ownership,
      durability, WAL, recovery, and evidence invariants.
- [ ] Plan 240 remains a design-only/proposed handoff; no runtime change is
      implied.
- [ ] No Rust, Cargo, migration, configuration, or qualification-tooling file is
      changed.
- [ ] Plans 239, 240, and 241 remain untouched.
- [ ] Current Eggfetch documentation still identifies 0.2.0 as the live provider
      dependency.
- [ ] The targeted cross-document scan finds no second mechanically duplicated
      current-authority block from the same landing.
- [ ] `validate_release_docs.py` passes.
- [ ] `validate_runtime_package_boundary.py` passes.
- [ ] `git diff --check` passes.
- [ ] The final diff is a small documentation-only deletion/consolidation.

## Completion record

When the corrective edit lands, change this plan's status to `complete` and
append:

- implementation commit SHA;
- the final count of the Plan 240 heading in
  `architecture/deep-dive-database.md`;
- documentation validator results;
- `git diff --check` result;
- confirmation that no production/Cargo files changed;
- any additional duplicate found by the bounded cross-document scan.

Do not reopen Plan 241. This plan closes only the documentation-state defect
identified after its implementation.
