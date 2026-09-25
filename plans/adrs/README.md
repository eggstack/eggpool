# Architecture Decision Records

Durable EggPool decisions affecting several milestones, subsystems, or public
contracts. Use an ADR when a question cannot be answered safely inside one
implementation plan without establishing a reusable contract.

## Naming

```text
ADR-NNNN-short-title.md
```

Numbers monotonically increasing from `0001`, never reused.

## Status lifecycle

```text
proposed -> accepted -> deprecated or superseded
         `-> rejected
```

Accepted ADRs are historical records. Never rewrite one to make a later
decision appear original — create a new ADR and mark the old one superseded.

## ADR template

```markdown
# ADR-NNNN: Title

Status: proposed

Date: YYYY-MM-DD

Decision owners: project maintainers

Related specification sections:

- `plans/000-long-term-specification.md#...`
- `plans/001-terminology-and-domain-model.md#...`

Affected subsystem roadmaps:

- `plans/subsystems/...`

## Context

Architectural problem, existing implementation, constraints, why now.

## Decision drivers

- ...

## Considered options

### Option A — Name

Description, benefits, costs, failure modes.

### Option B — Name

Description, benefits, costs, failure modes.

## Decision

Selected option precisely, with ownership/interface boundaries.

## Consequences

### Positive

- ...

### Negative

- ...

### Neutral or deferred

- ...

## Compatibility and migration

Storage, protocol, config, API, operational migration requirements.

## Security and reliability implications

Auth, secret handling, contention, cancellation, restart, recovery, DoS effects.

## Verification

Evidence required to prove implementations conform.

## Supersession

None.
```

## ADR threshold (EggPool)

Normally required for: reload-vs-restart boundary changes;
`classify_transition` key-list changes; new server/provider/storage
protocol; durable dependency selection (Eggfetch/Eggress/EggServe) or
feature-profile change; auth semantics; generation/lease/fencing semantics;
public compatibility contract; material non-goal change.

Usually unnecessary for: local refactors, internal naming,
implementation-specific structures, reversible optimizations preserving
contracts.
