---
name: architecture
description: Architecture principles and design decisions for the native Rust EggPool runtime.
---

# Architecture Skill

Read `architecture/README.md` and the relevant Rust deep dive before changing
runtime behavior. The repository-root `pyproject.toml`, `scripts/`, and
`tests/tooling/` are release/development tooling only.

## Core principles

- `rust/src/` owns current production behavior and `rust/assets/` owns embedded
  runtime data.
- Keep endpoint handling, coordinator, routing, persistence, provider
  transport, wire adaptation, and operations as explicit boundaries.
- Build complete immutable runtime-generation candidates before publication.
- Keep credentials, raw bodies, prompts, cache keys, and provider bodies out of
  persistence and diagnostics.
- Use the shared SQLite transaction/recovery contract; fail closed on commit or
  ownership ambiguity.
- Preserve the canonical wire intent and never chain translated payloads.

## Verification pointers

- CLI/config/errors: `rust/src/cli.rs`, `rust/src/config.rs`, `rust/src/error.rs`
- Request path: `rust/src/request/`, `rust/src/coordinator/`
- Providers/wire: `rust/src/providers/`, `rust/src/wire/`
- Runtime/reload: `rust/src/runtime_lifecycle.rs`, `rust/src/reload.rs`
- Operations: `rust/src/operations/`
- Database/assets: `rust/src/db/`, `rust/assets/`
