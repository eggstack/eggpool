# Plan 251 — Adopt CodeGG-style hierarchical planning (registry, ADRs, subsystems, closure gates)

Date: 2026-09-25
Status: complete
Planning baseline: `7879cbf9` (main, post-Plan 250 EggServe 0.3.0 migration)
Priority: P2 process/governance (no runtime change)

Related:

- CodeGG convention source: https://github.com/dbowm91/codegg (local: `/Users/davidbowman/projects/codegg`), `plans/README.md`, `plans/003-planning-process.md`, `plans/registry.md`, `.opencode/skills/planning/SKILL.md`
- Current convention: `.opencode/skills/plan/SKILL.md` (39-line flat append-only record)
- Review index: `architecture/overview.md`, `architecture/README.md`
- Predecessor history: `plans/001-*` through `plans/250-*` (flat, append-only, untouched by this plan)
- AGENTS.md “Skills” + “Plans” + “Conventions agents miss” sections

## Purpose

Transition EggPool from the flat `plans/NNN-slug.md` append-only record to the
CodeGG hierarchical convention — canonical long-term docs, ADRs, subsystem
roadmaps, bounded milestone implementation plans, evidence-gated closure
records, archive, and a compact `registry.md` control surface — without
rewriting or moving any of the ~254 existing flat plan files.

This is a planning-governance bootstrap only. It authorizes no runtime,
config, dependency, or doc-boundary change. All existing reload/restart,
error-mapping, admission, and transport ownership rules stay as-is.

## Convention delta (review finding)

| Dimension | EggPool today | CodeGG target (adopted) |
|---|---|---|
| Layout | flat `plans/NNN-slug.md` (~254 files) | `000/001/002/003` canonical + `adrs/` + `subsystems/` + `implementation/<subsystem>/` + `closure/<subsystem>/` + `archive/` + `registry.md` + `README.md` |
| Status vocabulary | `draft → ready for implementation → implementation handoff → complete/completed → closure/corrective-pass` (free text in header) | `proposed / ready / active / blocked / closing / closed / conditionally closed / superseded / archived` in plan header + registry row |
| Numbering | global `NNN` (next: 251; `146-*` duplicate is a known accident) | canonical `000–003` fixed; ADR `ADR-NNNN` global; implementation/closure `NNN` local to subsystem |
| Control surface | none (list the directory) | `plans/registry.md`: active roadmaps, dependency-ready plans, blocked work, recent closures — links only, no duplicated requirements |
| Durable decisions | embedded in narrative plans | `adrs/ADR-NNNN-*.md`, immutable once accepted; supersede, don’t rewrite |
| Work classification | none enforced | every roadmap/plan item declares one primary class: invariant / capability / infrastructure / polish |
| Dependencies | prose “Related” lists | typed per milestone: hard / interface / soft / operational; ready only when hard deps closed + interface contracts stable |
| Completion gate | plan header flipped to `complete` | closure record (`closure/<subsystem>/NNN-status.md`) with requirement→evidence matrix, commands run, invariant/security/migration review, disposition; commit message alone is not evidence |
| Corrective work | new flat plan referencing old filename | new implementation plan in same subsystem, new local number, referencing original plan + closure + why verification missed it |
| Skill | `.opencode/skills/plan/SKILL.md` (lifecycle + checks only) | expanded skill mirroring CodeGG planning skill, adapted to EggPool paths/commands |

## Non-goals (this plan does not)

- Rewrite, move, or renumber `plans/001-*`…`plans/250-*` or `python_hotpath_dispatch_compression_optimization.md`; Git history + flat files remain the archive.
- Invent new runtime architecture, subsystems beyond the initial decomposition below, or canonical requirements not grounded in `architecture/` + shipped behavior.
- Copy CodeGG product content (daemon/TUI/ACP/Eggwork content) into EggPool docs.
- Change CI, lint/test matrix, release, or deployment ownership.

## Bootstrap file list (execution below)

New files only (all under `plans/` except the skill):

1. `plans/README.md` — hierarchy diagram, directory roles, lifecycle, naming, core rule (adapted from CodeGG `plans/README.md`).
2. `plans/000-long-term-specification.md` — EggPool end-state + invariants grounded in `architecture/overview.md` (proxy role, wire surfaces, generation leases, single SQLite gate, fail-closed reload).
3. `plans/001-terminology-and-domain-model.md` — normative terms (generation, lease, admission, publication, finalization, rehash vs reload vs restart).
4. `plans/002-long-term-roadmap.md` — dependency-ordered capability roadmap skeleton referencing existing deep-dives; marks Plan 250 line closed, no new runtime scope.
5. `plans/003-planning-process.md` — normative governance (document classes, classification, dependency model, sizing, handoff contract, corrective passes, registry rules, anti-patterns), EggPool-adapted.
6. `plans/registry.md` — initial control surface: legacy flat history noted as immutable; Plan 250 recorded closed; this Plan 251 as active→closing; subsystem roadmap(s) proposed; no fake ready milestones.
7. `plans/adrs/README.md` — ADR template + threshold adapted to EggPool (reload/restart boundary, wire/transport, storage, auth).
8. `plans/subsystems/README.md` — roadmap template.
9. `plans/implementation/README.md` — milestone plan template with EggPool verification commands.
10. `plans/closure/README.md` — closure template with EggPool “must not mark closed when” rules.
11. `plans/archive/README.md` — archive workflow; notes flat legacy files stay in place (not moved) as the pre-251 archive.
12. `plans/subsystems/planning-governance-roadmap.md` — first roadmap under the new convention owning this transition (status: active during bootstrap, closed by the closure record below).
13. `plans/implementation/planning-governance/001-planning-convention-bootstrap.md` — milestone plan for this bootstrap (status: active).
14. `plans/closure/planning-governance/001-status.md` — closure record for the bootstrap milestone (written after verification).
15. `.opencode/skills/plan/SKILL.md` — expanded to the new lifecycle (replaces 39-line version; keeps skill name `plan` per AGENTS.md).

Initial subsystem decomposition (candidate, not all created now):

- `request-admission-wire` (admission, canonical IR, codecs)
- `routing-selection` (routing/quota/health/accounts/catalog/affinity)
- `provider-transport` (pool, eggfetch, Eggress proxy)
- `coordinator-lifecycle` (attempts, publication, finalization, streaming)
- `runtime-lifecycle-reload` (generations, rehash/reload/restart, supervision)
- `persistence` (SQLite gate, migrations v1–v54, backup/restore)
- `server-transport` (EggServe/Axum boundary, middleware, shutdown)
- `operations-cli` (lifecycle, status, integrations, dashboard)
- `deployment-packaging` (release, systemd, installers, connect helper)

Only the `planning-governance` roadmap is created by this plan. Further
subsystem roadmaps are created on demand per the new process (ready to be
reasoned about, not bulk-generated).

## Authority order (new, per CodeGG §6 adapted)

1. `plans/000-*` + `plans/001-*` canonical specification/terminology
2. accepted ADRs
3. subsystem roadmap
4. milestone implementation plan
5. current repository evidence (`rust/src/...`, `rust/tests/...`, `scripts/...`)

Repository evidence overrides interim plans on mechanics; long-term invariants
override both. Agents preserve unrelated user changes and record discrepancies
rather than silently enlarging scope.

## Acceptance criteria

- All 15 files above exist with EggPool-grounded content; no CodeGG product content copied.
- Flat legacy plans untouched: `git status` shows only new files plus the skill edit (and this plan file itself).
- `plans/registry.md` links active/closed state truthfully (250 closed, 251 closing with closure record).
- Closure record `plans/closure/planning-governance/001-status.md` contains requirement→evidence matrix and commands run.
- Verification passes: `ls plans/`, `git diff --check`, `git status --short`.

## Stop conditions

- Stop and report (do not improvise) if any step requires editing a closed flat plan, inventing runtime requirements, or copying CodeGG domain content as EggPool direction.
- If subsystem decomposition is disputed, land templates + registry and leave further roadmaps `proposed`, rather than blocking the bootstrap.

## Closure evidence required

- File list + `git status --short` output.
- `git diff --check` clean.
- Registry + roadmap status lines consistent with the closure disposition.
- Follow-up work (if any) registered as `proposed`, not silently opened.
