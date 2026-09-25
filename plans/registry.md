# EggPool Active Planning Registry

This file is the compact control surface for active interim planning.
Detailed requirements and completed history remain in source roadmaps,
implementation plans, `plans/closure/`, flat legacy plans, and Git history.
Links only — do not duplicate milestone requirements here.

Canonical direction:

- `plans/000-long-term-specification.md`
- `plans/001-terminology-and-domain-model.md`
- `plans/002-long-term-roadmap.md`
- `plans/003-planning-process.md`

Legacy archive (pre-251, immutable, top level): `plans/001-*` through
`plans/250-*` plus `python_hotpath_dispatch_compression_optimization.md`.
Most recently closed: Server transport M001 (EggServe 0.4.0 adoption and
requalification, `plans/closure/server-transport/001-status.md`). Legacy
archive latest: Plan 250 (EggServe 0.3.0 direct-Tower migration,
`7879cbf9`). Plans 244–245, 215–220, 241 remain historical per their own
closure passes; the `146-*` duplicate pair is a known numbering accident.

## Status vocabulary

- **proposed** — roadmap or plan exists but is not approved for execution.
- **ready** — dependencies and interfaces satisfied; may be handed off.
- **active** — implementation or closure work in progress.
- **blocked** — a named dependency or evidence requirement prevents progress.
- **closing** — implementation landed; closure evidence being gathered.
- **closed** — closure record accepted.
- **conditionally closed** — substantial work landed, named correctness or
  operational evidence remains (condition + risk + exact future evidence in
  the closure record).
- **superseded** — replaced by another document.
- **archived** — no longer active; retained for traceability.

## Active subsystem roadmaps

| Subsystem | Status | Roadmap | Current milestone | Dependencies or blockers |
|---|---|---|---|---|
| — | — | — | — | none registered |

## Dependency-ready implementation plans

| Subsystem | Milestone | Status | Implementation plan | Dependencies / handoff note |
|---|---|---|---|---|
| — | — | — | — | none registered |

## Blocked work

| Subsystem | Milestone | Blocker |
|---|---|---|
| — | — | none registered |

## Recently closed

| Subsystem / plan | Disposition | Evidence |
|---|---|---|
| Plan 250 — EggServe 0.3.0 direct-Tower migration (legacy flat) | closed | `plans/250-eggserve-0.3.0-direct-tower-migration-and-requalification.md`, `7879cbf9` |
| Planning governance M001 | closing → closed on acceptance of `plans/closure/planning-governance/001-status.md` | Plan 251 authorizes; bootstrap files + verification in closure record |
| Server transport M001 — EggServe 0.4.0 adoption and requalification | closed | `plans/closure/server-transport/001-status.md`, implementation `409491ea` |

## Unblock audit

Server transport M001 closed with no promotions: at close time no registered
plan listed it as a hard or interface dependency. Future subsystem roadmaps (see Plan 251
candidate decomposition) register here only when ready to be reasoned
about — do not bulk-generate rows to populate the table.
