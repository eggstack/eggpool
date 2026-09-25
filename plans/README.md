# EggPool Planning System

This directory separates durable architectural direction from temporary
execution planning. Flat files `plans/001-*` through `plans/250-*` (plus
`python_hotpath_dispatch_compression_optimization.md`) are the pre-251 legacy
archive: append-only, immutable, and left in place. Do not rewrite them to
reflect later work — add a new corrective/closure pass under the hierarchy
below. Git history remains the archive of record.

Adopted from the CodeGG convention (`plans/README.md`,
`plans/003-planning-process.md` in https://github.com/dbowm91/codegg),
adapted to EggPool paths and ownership. CodeGG product content
(daemon/TUI/ACP/Eggwork) is not EggPool direction and is not copied here.

## Canonical long-term documents

The following files define the intended product and architecture and MUST NOT
be edited as part of ordinary implementation work:

- `000-long-term-specification.md` — normative end-state specification and invariants.
- `001-terminology-and-domain-model.md` — normative language and identity model.
- `002-long-term-roadmap.md` — dependency-ordered long-term capability roadmap.
- `003-planning-process.md` — rules for deriving and managing interim plans.

Changes to the first three require an explicit architecture decision or user
direction, not an implementation convenience. Interim plans MUST reference
them rather than copying or silently revising their requirements.

## Planning hierarchy

```text
Long-term specification and terminology
        |
        v
Architecture decision records
        |
        v
Master long-term roadmap
        |
        v
Subsystem roadmaps
        |
        v
Milestone implementation plans
        |
        v
Implementation and verification
        |
        v
Closure records and archive
```

## Directory roles

- `adrs/` — durable architecture decisions. Accepted decisions are superseded, not rewritten.
- `subsystems/` — subsystem specifications and dependency-ordered roadmaps translating long-term direction into workstreams.
- `implementation/` — focused milestone plans handed to implementation agents. Operational; may evolve as code changes.
- `closure/` — verification, evidence, residual-risk, and completion records for implemented milestones. Gates completion.
- `archive/` — post-251 completed or superseded interim planning retained for traceability. Pre-251 flat files stay at top level as the legacy archive and are NOT moved.
- `registry.md` — compact index of active subsystem roadmaps, implementation plans, closure work, and dependencies. Links only; no duplicated requirements.

## Core rule

Long-term documents state **what EggPool is becoming and what must remain
true**. Interim documents state **what an agent should implement next against
a specific repository baseline**.

Implementation agents MUST NOT add commit-specific steps, transient file
lists, current test counts, or short-lived corrective work to the canonical
long-term documents.

## Planning lifecycle

1. Identify the relevant long-term specification sections and invariants.
2. Record any unresolved architectural decision in `adrs/`.
3. Create or update a subsystem roadmap in `subsystems/`.
4. Select one dependency-ready milestone.
5. Write a bounded handoff plan under `implementation/`.
6. Implement and verify the milestone.
7. Write a closure record under `closure/`.
8. Update `registry.md` and the subsystem roadmap status.
9. Move completed or superseded post-251 interim documents to `archive/` when they no longer represent active work.

No milestone is complete merely because code landed. Completion requires the
closure evidence defined by its implementation plan and subsystem roadmap.

## Required classification

Every subsystem roadmap and implementation plan MUST distinguish:

- **Invariant** — a property that must always remain true.
- **Capability** — user- or operator-visible behavior.
- **Infrastructure** — internal machinery required by capabilities.
- **Polish** — ergonomics, diagnostics, performance tuning, cleanup, or documentation.

Infrastructure and polish MUST NOT be presented as completed user capability
unless the user-visible acceptance criteria are actually satisfied.

## Naming conventions

- Canonical: `000-long-term-specification.md`, `001-terminology-and-domain-model.md`, `002-long-term-roadmap.md`, `003-planning-process.md`
- ADR: `adrs/ADR-NNNN-short-title.md`
- Subsystem roadmap: `subsystems/<subsystem>-roadmap.md`
- Milestone implementation plan: `implementation/<subsystem>/NNN-short-title.md` (number local to subsystem)
- Closure record: `closure/<subsystem>/NNN-status.md` (same number as plan)
- Archived document: retain original relative structure beneath `archive/`

Use stable EggPool subsystem names (see Plan 251 § Initial subsystem
decomposition). Do not encode dates in filenames unless the document is
inherently time-bound.

## Starting a new workstream

Begin with `subsystems/README.md`, then use the templates and rules in:

- `adrs/README.md`
- `implementation/README.md`
- `closure/README.md`

Register active work in `registry.md` before handing implementation plans to
agents. Load the `plan` skill (`.opencode/skills/plan/SKILL.md`) before
writing any plan.
