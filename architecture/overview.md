# EggPool Architecture Overview

EggPool is a native Rust, LAN-hosted proxy that aggregates multiple LLM
provider accounts behind OpenAI Chat Completions, OpenAI Responses, and
Anthropic Messages-compatible paths. The shipped application and runtime live
under `rust/`; the PyPI wheel is defined by
`packaging/pypi/pyproject.toml` and contains the native executable plus
metadata/assets only.

## Current ownership

| Area | Rust authority |
|---|---|
| CLI, configuration, errors | `rust/src/cli.rs`, `rust/src/config.rs`, `rust/src/error.rs` |
| Request lifecycle and wire adaptation | `rust/src/request/`, `rust/src/coordinator/`, `rust/src/wire/` |
| Routing, quota, providers, health | `rust/src/routing/`, `rust/src/quota/`, `rust/src/providers/`, `rust/src/health/` |
| Database and migrations | `rust/src/db/`, `rust/assets/db/migrations/` |
| Runtime generations and reload | `rust/src/runtime_lifecycle.rs`, `rust/src/reload.rs`, `rust/src/server.rs` |
| Dashboard, operations, update | `rust/src/server.rs`, `rust/src/operations/` |
| Compatibility fixtures and qualification output | `tests/fixtures/`, `artifacts/qualification/` |

The repository-root `pyproject.toml` is tooling-only. Python utilities under
`scripts/` validate release/catalog/package contracts and qualification
artifacts; they are not imported by or required to start EggPool. Historical
Python source is recoverable from the immutable reference commit recorded in
`docs/migration-history.md`.

## Request lifecycle

1. The Rust HTTP server admits and bounds the request.
2. Routing selects an eligible provider account using quota and health state.
3. The coordinator persists the attempt before upstream dispatch.
4. The canonical wire boundary encodes the selected upstream surface.
5. The provider client pool sends the request and observes the response/stream.
6. Finalization records usage, releases reservations, and applies health effects.

Requests acquire a generation lease. Reload publishes a complete candidate
generation atomically; in-flight requests finish on their original generation.
Retries consume one shared upstream-submission budget, and transport EOF never
creates a synthetic stream terminal event.

## Source-development flow

Run Rust commands from the repository root with an explicit manifest:

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked
rust/target/debug/eggpool --help
```

Build output stays under `rust/target/`. Build a local package only through
`packaging/pypi/pyproject.toml`; do not use the root tooling manifest as a
publication or runtime entry point.

## Deep dives

The subsystem references in [README.md](README.md) describe the current
runtime and point to the detailed design documents. Those documents use Rust
source paths and retained neutral fixtures. Migration history is recoverable
from Git; see [migration-history.md](../docs/migration-history.md).
