# Subsystem Roadmaps

Roadmaps translate canonical EggPool direction into coherent,
dependency-aware workstreams. They are not coding-agent checklists — that is
`plans/implementation/`. Each roadmap stays useful across several milestones
and repository revisions; commit-specific mechanics belong in implementation
plans.

Candidate subsystems (see Plan 251; create on demand only):
`request-admission-wire`, `routing-selection`, `provider-transport`,
`coordinator-lifecycle`, `runtime-lifecycle-reload`, `persistence`,
`server-transport`, `operations-cli`, `deployment-packaging`,
`planning-governance`.

## Naming

```text
<subsystem>-roadmap.md
```

Kebab-case, stable across roadmap/implementation/closure/registry. No dates.

## Required roadmap structure

```markdown
# <Subsystem> Roadmap

Status: proposed | active | closing | closed | superseded

Long-term references:

- `plans/000-long-term-specification.md#...`
- `plans/001-terminology-and-domain-model.md#...`
- `plans/002-long-term-roadmap.md#...`

Related ADRs:

- `plans/adrs/ADR-NNNN-...md` (or "None required")

## 1. Purpose and ownership boundary

What the subsystem owns, consumes, and must not own (cite `rust/src/...` owners).

## 2. Work classification

### Invariants

- ...

### Capabilities

- ...

### Infrastructure

- ...

### Polish

- ...

## 3. Non-goals

- ...

## 4. Current state

Repo evidence, contracts, compat paths, known gaps. Avoid fragile line numbers.

## 5. Target architecture

End-state module, storage, protocol, ownership, lifecycle model.

## 6. Dependency graph

Classify each edge hard / interface / soft / operational.

## 7. Milestones

### Milestone 1 — Title

Class: invariant | capability | infrastructure | polish

Objective:

Dependencies:

Deliverable boundary:

User or operator value:

Exit conditions:

Deferred work:

## 8. Cross-cutting requirements

Storage/migration; protocol/compat; security/auth; concurrency/cancellation/recovery; observability; performance; docs/ops.

## 9. Verification strategy

Subsystem integration, property, contention, restart, migration, end-to-end evidence. Rust suites run serial (`--test-threads=1`); note the focused-target index per the development skill.

## 10. Risks and decision points

Unresolved decisions; which require ADRs.

## 11. Completion definition

What must be true before the roadmap closes.

## 12. Milestone status

| Milestone | Status | Implementation plan | Closure record | Blockers |
|---|---|---|---|---|
| 1 | not started | — | — | — |
```

## Roadmap rules

MUST link canonical requirements (not duplicate them); define ownership
before milestones; distinguish infrastructure from completed capability;
expose dependencies/decision points; preserve completed history; link each
active milestone to one plan + later one closure; state non-goals; stay at
subsystem level, not file-by-file checklists. Create only roadmaps ready to
be reasoned about.
