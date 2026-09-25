---
name: plan
description: EggPool planning process — canonical docs, ADRs, subsystem roadmaps, milestone implementation plans, closure records, registry.
---

# Plan Maintenance

`plans/` separates durable direction from interim execution planning. Flat
files `plans/001-*`…`plans/250-*` are the pre-251 legacy archive:
append-only, immutable, left in place. Post-251 work follows the hierarchy
in `plans/README.md`. Do not rewrite or delete closed records to reflect
later work — add a new corrective/closure pass.

## Document hierarchy

```text
Long-term specification and terminology (000, 001 — stable)
        |
        v
Architecture decision records (adrs/ — immutable once accepted)
        |
        v
Master long-term roadmap (002 — sequencing)
        |
        v
Subsystem roadmaps (subsystems/ — workstreams)
        |
        v
Milestone implementation plans (implementation/<subsystem>/ — agent handoff)
        |
        v
Implementation and verification
        |
        v
Closure records (closure/<subsystem>/ — completion gate)
        |
        v
Archive (archive/ — post-251 traceability; pre-251 flat files stay top level)
```

`plans/registry.md` is the compact control surface (active roadmaps, ready
plans, blocked work, recent closures). Link; never duplicate requirements.

Interim plans MUST reference canonical docs rather than duplicating them.
Repository evidence overrides interim plans on mechanics; long-term
invariants override both.

## Lifecycle and status vocabulary

Post-251 documents use: `proposed` → `ready` → `active` → `closing` →
`closed`, with `blocked`, `conditionally closed`, `superseded`, `archived`
as needed. Keep the status line in every plan/roadmap/closure header.
Legacy flat headers (`draft`, `ready for implementation`, `implementation
handoff`, `complete`, `closure`) MUST NOT be used for new hierarchy docs.

- Numbering: canonical `000–003` fixed; ADRs `ADR-NNNN` global
  (monotonic, never reused); implementation/closure `NNN` local to the
  subsystem (closure reuses its plan's number).
- Global flat `NNN-slug.md` numbering ended at 250 (the `146-*` duplicate
  is a known historical accident — do not replicate it).
- One commit = one status change in `registry.md`. A commit message saying
  “closed” is not closure evidence — the closure record is the gate.
- Closing a milestone REQUIRES auditing blocked work: promote newly-ready
  plans (or record why still blocked) in the same commit.

## Work classification

Every roadmap/plan item declares one primary class:

- **Invariant** — must always remain true (e.g. generation-owned execution,
  single SQLite gate, fail-closed reload). Needs guards/property tests.
- **Capability** — user/operator-visible behavior. Needs end-to-end
  acceptance evidence.
- **Infrastructure** — internal machinery. MUST NOT be presented as
  completed capability until a consumer path exists.
- **Polish** — ergonomics, diagnostics, perf, cleanup, docs. Normally after
  correctness closure.

## Dependency model

Each milestone declares dependencies as **hard** (cannot begin before
close), **interface** (may proceed against an agreed contract/test double),
**soft** (parallel possible, integration depends), or **operational** (can
land; deploy/release needs external evidence). Ready = all hard closed +
all interface contracts stable.

## Creating a subsystem roadmap

Path: `plans/subsystems/<subsystem>-roadmap.md`. Full template:
`plans/subsystems/README.md`. Required: status, long-term refs, ADRs,
purpose/ownership boundary, classification, non-goals, current state (no
fragile line numbers), target architecture, typed dependency graph,
milestones (class, objective, deps, deliverable boundary, exit conditions),
cross-cutting requirements, verification strategy, risks/decision points,
completion definition, milestone status table. Create only roadmaps ready to
be reasoned about — never bulk-generate.

## Writing an implementation plan

Path: `plans/implementation/<subsystem>/NNN-short-title.md`. Full template:
`plans/implementation/README.md`. MUST be independently executable, bounded,
tied to a repository baseline, with: source roadmap, long-term refs, ADRs,
primary class, objective + non-goals, current evidence, non-regressing
invariants, production changes, ordered work packages (intent, changes,
acceptance evidence), failure/cancellation/restart/contention semantics,
compat/migration, required tests, exact verification commands, doc updates,
acceptance criteria, stop conditions, closure evidence required, handoff
notes. Prefer vertical slices with a consumer. Register in `registry.md`
before handoff.

## Writing a closure record

Path: `plans/closure/<subsystem>/NNN-status.md`. Full template:
`plans/closure/README.md`. MUST include: implementation commits,
requirement-to-evidence matrix, commands run + results (label local vs CI
truthfully), invariant/security/migration/failure reviews, docs/ops
evidence, severity-tagged unresolved findings, disposition (`closed` |
`conditionally closed` | `corrective pass required` | `blocked`), registry
updates. MUST NOT mark `closed` on compilation alone, unrun tests without
justified substitute, infrastructure-only “capability”, unimplemented
security/migration, broken `--no-default-features` parity, or remaining
high-severity defects.

## Corrective passes

A corrective pass is a NEW implementation plan (same subsystem, new local
number) referencing the original plan + closure, listing unclosed findings,
explaining why verification missed them, and adding regression tests/guards.
Never reopen unrelated closed scope without evidence. Repeated correctives
⇒ revise the roadmap/sizing.

## Writing an ADR

Path: `plans/adrs/ADR-NNNN-short-title.md`. Template + threshold:
`plans/adrs/README.md`. Required for reload/restart boundary changes,
durable dependency selection, auth semantics, generation/lease/fencing
semantics, new protocols, public compat contracts. Accepted ADRs are
immutable — supersede, don’t rewrite.

## Archive workflow

Post-251 completions move under `plans/archive/` preserving relative
structure (`archive/subsystems/…`, `archive/implementation/<subsystem>/…`,
`archive/closure/<subsystem>/…`); update inbound links; add an archival
note; prefer `git mv`. Pre-251 flat files are never moved. Canonical docs +
accepted ADRs are never archived merely because implementation completed.

## Before writing a plan

- Read the matching skill (`architecture`, `development`, `deployment`,
  `documentation`), the review index in `architecture/overview.md`, and
  `plans/003-planning-process.md`.
- Cite authority paths (`rust/src/...`, `rust/tests/...`, `scripts/...`) with
  exact filenames. Streaming files are `coordinator.rs`, `execution.rs`,
  `terminal.rs`, `timeout.rs`, `types.rs`, `diagnostics.rs` — there is no
  `contract.rs` and no `coordinator_c012` test target.
- Keep credentials, prompts, raw bodies, and cache keys out of the plan.

## Checks

```bash
ls plans/ | tail -n 5
git status --short
git diff --check
```
