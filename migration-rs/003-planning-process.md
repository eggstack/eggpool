# EggPool Rust Migration Planning and Handoff Process

Status: normative planning governance

This process follows the CodeGG planning model: stable long-term direction, explicit ADRs, subsystem roadmaps, bounded implementation plans, and closure records backed by evidence.

## 1. Authority order

When documents or repository evidence conflict, use this order:

1. `000-long-term-specification.md`;
2. `001-terminology-and-domain-model.md`;
3. accepted migration ADRs;
4. `002-long-term-roadmap.md`;
5. the relevant subsystem roadmap;
6. the active milestone implementation plan;
7. current repository evidence.

A material contradiction between levels 1-4 and repository reality must be reported rather than silently resolved in favor of easier implementation.

## 2. Document classes

Canonical documents define migration identity, invariants, terms, and macro ordering. They change only for an intentional architecture/product decision.

ADRs record durable choices that affect several milestones or compatibility surfaces. Accepted ADRs are superseded, not rewritten to hide history.

Subsystem roadmaps define one coherent workstream, its dependencies, milestones, non-goals, risks, and exit conditions.

Implementation plans are repository-baseline-specific handoff artifacts. They may evolve and be superseded as code changes.

Closure records determine whether a milestone is actually complete. A code commit alone is not closure.

## 3. Required work classification

Every roadmap milestone and implementation plan declares one primary class:

- **Invariant** — property that must remain true across migration strategies.
- **Capability** — user/operator/client-visible Rust behavior.
- **Infrastructure** — internal machinery required by later capabilities.
- **Polish** — diagnostics, ergonomics, performance, cleanup, or documentation.

Infrastructure landing is not proof of capability parity.

## 4. Dependency classes

Plans declare dependencies as:

- **hard** — cannot correctly begin until dependency closes;
- **interface** — may proceed against a stable written contract/test double;
- **soft** — parallel work is possible but final integration depends on it;
- **operational** — implementation may land but release/deployment requires external evidence.

## 5. Migration-specific planning requirements

Every implementation plan must identify:

- the Python oracle modules/tests/documents for the behavior being ported;
- which observations are exact vs semantic parity;
- permitted normalization rules;
- database/config/API/CLI/SSR compatibility effects;
- failure, cancellation, restart, and contention semantics where relevant;
- whether the plan creates or changes any Rust dependency;
- narrow and broad verification commands;
- closure evidence required.

Plans that port SSR behavior must identify static assets and DOM/escaping invariants. Plans that touch provider networking must identify direct-vs-Eggress transport behavior and secret redaction. Plans that touch durable state must include Python-to-Rust and, where safe, Rust-to-Python rollback evidence.

## 6. Milestone sizing

Prefer vertical slices that establish one verifiable compatibility boundary.

A milestone is too large when it combines several independently testable boundaries such as config + database + provider dispatch + finalization. A milestone is too small when it only renames symbols or adds scaffolding with no stable interface, unless that scaffold is a hard dependency for the next parity slice.

The foundation roadmap intentionally uses several small milestones because each establishes a reusable test or ownership boundary for all later work.

## 7. Implementation-agent contract

An agent must inspect current Python code before editing Rust. The plan is not a substitute for repository evidence.

Agents must:

- preserve unrelated Python changes;
- keep Rust under `rust/` unless an accepted ADR changes the location;
- avoid installing/replacing the Python `eggpool` executable during migration;
- add differential tests with each new compatibility surface;
- avoid normalizations that mask semantic differences;
- keep dependencies narrow;
- update migration docs when a discovered contract differs materially from the plan;
- report incomplete work and stop conditions explicitly.

## 8. Corrective passes

A failed closure creates a new corrective implementation plan. Do not retroactively mark an incomplete milestone as successful.

Corrective plans must enumerate unclosed requirements, explain why prior verification missed them, and add regression evidence that would have caught the defect.

## 9. Registry

`registry.md` is the active control surface. It lists active roadmaps, dependency-ready implementation plans, blockers, and latest closure status without duplicating plan detail.

Implementation plans must be registered before handoff and moved out of the active section after closure/supersession.

## 10. Required planning review

Before handoff, verify:

1. repository baseline is current;
2. the Python oracle is named;
3. exact vs semantic parity is clear;
4. unresolved architecture decisions have ADRs;
5. hard dependencies are closed;
6. non-goals prevent scope expansion;
7. compatibility and migration effects are explicit;
8. failure/cancellation/restart/contention are addressed where relevant;
9. tests cannot pass by over-normalizing differences;
10. closure criteria are externally meaningful.

## 11. Anti-patterns

Do not:

- rewrite Python in place;
- redesign EggPool while claiming migration parity;
- fork the database schema for Rust convenience;
- replace the dashboard design during SSR porting;
- add a local Eggress listener when an in-process outbound connector suffices;
- introduce both Reqwest and Hyper without evidence for two stacks;
- create many internal crates before a real boundary exists;
- equate `cargo check` with migration closure;
- mirror costly live provider traffic automatically;
- add extensive CI matrices before local deterministic parity tests justify them;
- hide mismatches behind broad JSON/HTML normalization.
