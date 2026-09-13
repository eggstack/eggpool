---
name: development
description: Development, formatting, linting, type checking, and testing for the Rust runtime and Python tooling.
---

# Development Workflow

## Current runtime

Run from the repository root:

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked
cargo build --manifest-path rust/Cargo.toml --locked --release

# Shared semantic model-routing crate (Rust 1.81-compatible boundary)
cargo check --manifest-path rust/crates/eggpool-model-routing/Cargo.toml
cargo test --manifest-path rust/crates/eggpool-model-routing/Cargo.toml
```

Strict Clippy is a repository invariant across all Rust targets. Do not add a
baseline allowlist or broad suppression; resolve new warnings locally and use
narrow, justified allowances only when the intentional API or test shape is
clearer and safer.

For adapter changes, run the CLI contract, operations O002–O010, health, and
coordinator publication/boundary targets before the full workspace suite. Keep
the server modules thin: HTTP handlers must delegate inference lifecycle work
to the coordinator, and lifecycle workflows must compose the existing process
safety primitives.

For streaming coordinator changes, run the focused C008 publication, boundary,
finalization, and wire suites before the workspace suite:

```bash
cargo test --manifest-path rust/Cargo.toml --test coordinator_c008 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_boundaries -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_publication -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_stream -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_runtime -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_qualification -- --test-threads=1
```

Keep post-handoff execution single-owner and incremental while refactoring;
transparent upstream replay is only valid before `StreamingExecution` is
returned.

Configuration changes must use `config_reload_policy::classify_transition`.
Mutation paths should carry the redacted transition into apply logic, while
`reload.rs` remains authoritative for server-side revalidation and generation
publication. Add deterministic transition coverage for no-op, live,
restart-required, mixed, invalid, and secret-redaction cases.

For native dependency or feature changes, Cargo is the authority. Review both
the source/build/test owners and the resolved graph before removing a direct
crate or feature:

```bash
cargo tree --manifest-path rust/Cargo.toml -e features
cargo tree --manifest-path rust/Cargo.toml --duplicates
```

Keep supported Eggress proxy URI/chaining, TLS verification, and bundled
SQLite/backup behavior qualified when changing their feature sets.

## Tooling

Python is retained for release/validation scripts and their tests only:

```bash
uv sync --dev
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
```

Focused release checks include the catalog, package boundary, release
workflow, retirement boundary, and quick-installer qualification validators.
Do not add a Python application fallback or import the retired application.
