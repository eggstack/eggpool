---
name: development
description: Development, formatting, linting, type checking, and testing for the Rust runtime and Python tooling.
---

# Development Workflow

## Current runtime

Run from the repository root:

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked
cargo build --manifest-path rust/Cargo.toml --locked --release
```

Strict Clippy is a repository invariant across all Rust targets. Do not add a
baseline allowlist or broad suppression; resolve new warnings locally and use
narrow, justified allowances only when the intentional API or test shape is
clearer and safer.

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
