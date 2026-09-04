# EggPool Rust Migration Planning System

Status: active

This directory governs the side-by-side migration of EggPool from the current Python implementation to a pure Rust implementation while preserving the existing EggPool product contract.

The Python implementation remains live and authoritative during migration. The Rust implementation is developed in this repository under `rust/` and is qualified against Python before any cutover. `migration-rs/` contains planning, architecture decisions, implementation handoffs, and closure evidence; production Rust source MUST NOT be placed here.

## Canonical long-term documents

- `000-long-term-specification.md` — normative migration end state and invariants.
- `001-terminology-and-domain-model.md` — shared migration terminology and ownership model.
- `002-long-term-roadmap.md` — dependency-ordered migration roadmap.
- `003-planning-process.md` — governance for plans, ADRs, implementation handoffs, and closure.

These documents define what the migration is trying to preserve and what the Rust end state must become. Ordinary implementation work MUST NOT weaken them for convenience.

## Planning hierarchy

```text
Canonical migration specification
        |
        v
Architecture decisions
        |
        v
Long-term migration roadmap
        |
        v
Subsystem roadmaps
        |
        v
Bounded implementation plans
        |
        v
Implementation + differential verification
        |
        v
Closure evidence / archive
```

## Directory roles

- `adrs/` — durable architectural decisions for the migration.
- `subsystems/` — coherent workstream roadmaps derived from the canonical migration documents.
- `implementation/` — bounded, repository-baseline-specific plans handed to implementation agents.
- `closure/` — requirement-to-evidence records for completed milestones.
- `archive/` — superseded or completed interim planning retained for traceability.
- `registry.md` — compact active-work control surface.

## Core migration rule

The migration changes implementation language and runtime, not EggPool's product identity.

Unless an explicit accepted ADR says otherwise, the following remain compatibility surfaces: configuration schema and path resolution, CLI commands and exit semantics, SQLite schema and migrations, API endpoints and payloads, routing and failure semantics, provider behavior, dashboard routes and rendered visual/DOM behavior, static assets, and operational workflows.

Incidental framework details such as Granian-specific `Server` headers, TCP packet boundaries, or ASGI implementation artifacts are not compatibility requirements unless a real EggPool consumer depends on them and that dependency is documented.

## Initial implementation location

The Rust implementation is expected to begin as:

```text
rust/
├── Cargo.toml
├── src/
│   ├── main.rs
│   └── lib.rs
└── ...
```

The initial Cargo package is non-published and produces an `eggpool` binary under `rust/target/...`. It MUST NOT overwrite or uninstall the Python EggPool executable during migration. Black-box tests invoke the Python and Rust implementations by explicit executable path.

## Safe dual-run workflow

Run the Python oracle and Rust candidate on different ports and with distinct
writable SQLite paths. The migration harness's implementation-specific roots
make this the default for paired configurations. When comparing one prepared
database state, create one source fixture and copy it to separate paths before
starting either implementation. Do not let concurrent Python and Rust server
processes write the same SQLite file; parity comes from equivalent inputs and
post-run observations, not from shared-writer races.

## Baseline

The migration planning baseline is EggPool `main` at `0bb5aaf419e60eadebaf3cce341a2ae4e3852e6c` (`docs: close Plan 167`, 2026-09-03).

Implementation plans MUST refresh their own repository baseline before handoff.
