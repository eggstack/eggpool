# Foundation Milestone F001 — Rust Workspace and Build Scaffold

Status: ready for handoff

Repository baseline: `0bb5aaf419e60eadebaf3cce341a2ae4e3852e6c`

Source roadmap: `migration-rs/subsystems/foundation-roadmap.md#F001`

Long-term requirements: `migration-rs/000-long-term-specification.md` sections 2, 6, 8, 9.

Applicable ADRs: ADR-0001, ADR-0002.

Primary class: infrastructure

## 1. Objective

Create the minimal side-by-side Rust production scaffold under `rust/` so later parity work has a stable build/runtime boundary without altering Python EggPool installation or behavior.

## 2. Why this milestone is ready

No hard implementation dependency exists. The source location, runtime/HTTP direction, side-by-side policy, and single-package preference are already decided by accepted ADRs.

## 3. Current implementation evidence

Python remains the only production implementation. The root uses `pyproject.toml`, Python package entry points, and existing Python CI/tests. No Rust migration package is present at the planning baseline.

## 4. Invariants that must not regress

- `uv run eggpool ...` and installed Python EggPool remain untouched.
- no Python dependency is removed.
- no database/config file is modified by merely building or probing Rust.
- the Rust package is non-published during migration.
- one principal Cargo package is used unless implementation evidence forces a split.
- unsafe Rust is not required for the scaffold.

## 5. Scope

### In scope

- `rust/Cargo.toml`, `rust/src/main.rs`, `rust/src/lib.rs`;
- MSRV/toolchain policy documented in Rust-local README or manifest comments;
- initial modules for errors/version/runtime bootstrap only as needed;
- minimal `tracing` initialization;
- minimal Clap parser sufficient for `--help`/`version` scaffolding while F003 owns full CLI parity;
- minimal Tokio runtime bootstrap;
- Rust-local formatting/lint/test commands;
- `.gitignore` additions for `rust/target` if needed;
- a small migration developer README explaining explicit-path invocation.

### Explicitly out of scope

Axum server routes, SQLite, config schema port, provider HTTP, Eggress, SSR, routing, protocol codecs, deployment changes, installer changes, crates.io publishing, broad CI matrices.

## 6. Required production changes

The initial package SHOULD use Rust edition 2024 if the selected MSRV supports it consistently across intended targets; otherwise use edition 2021 and document the reason. The package may be named `eggpool` and `publish = false`; its binary name must be `eggpool` so later CLI parity does not inherit an artificial command-name difference.

Dependencies should be only what F001 immediately needs, expected to be approximately `tokio`, `clap`, `tracing`, `tracing-subscriber`, and `thiserror`. Do not add Axum/Hyper/Serde/SQLite/Eggress until the milestone that uses them unless F002 interface code genuinely requires one.

The binary must print version information from Cargo/package metadata rather than duplicating a manually maintained Rust version string.

## 7. Ordered work packages

### A — Create isolated Cargo package

Create manifest/source layout and verify building from repository root via `cargo --manifest-path rust/Cargo.toml ...`.

Acceptance: debug build produces only Rust-local artifacts and does not mutate Python env/config/data.

### B — Minimal process bootstrap

Add typed top-level error handling, tracing initialization, Tokio main, and minimal Clap command dispatch.

Acceptance: `rust/target/debug/eggpool --help` and a version probe exit deterministically without starting a server.

### C — Developer/verification surface

Document exact build/fmt/clippy/test commands and explicit-path rules for migration harnesses.

Acceptance: a new agent can build/probe Rust without installing it globally.

## 8. Failure, cancellation, restart, contention semantics

No long-lived server exists yet. Startup/config errors in the scaffold must result in a non-zero exit without panic backtraces by default. Ctrl-C/runtime lifecycle semantics are deferred until an HTTP server exists.

## 9. Compatibility and migration

No user-facing migration occurs. Python remains canonical. Do not edit `pyproject.toml` to point at Rust and do not modify installer/systemd behavior.

## 10. Required tests

- parser smoke for help/version;
- unit test for package version exposure if logic exists;
- top-level error formatting does not expose debug internals by default.

## 11. Required verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml -- --check
cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml
cargo build --manifest-path rust/Cargo.toml
rust/target/debug/eggpool --help
```

Also run a narrow Python smoke confirming the existing command still resolves through the Python environment.

## 12. Documentation updates

Create `rust/README.md` with migration-only build/run guidance and pointers back to `migration-rs/`.

## 13. Acceptance criteria

- Rust builds reproducibly from the side-by-side directory.
- the generated binary is named `eggpool` but is not installed over Python.
- Python packaging/tests are unaffected.
- dependency graph contains no unused migration-future stack.
- all required commands pass.

## 14. Stop conditions

Stop if repository tooling forces replacing root Python packaging, if a dependency requires unsupported MSRV/platform policy, or if the scaffold expands into API/database/provider implementation.

## 15. Closure evidence required

Commit SHA, manifest/dependency summary, exact verification command outputs, proof Python CLI still runs, and any toolchain/MSRV decision.

## 16. Handoff notes

Prefer the smallest coherent scaffold. Do not create a Cargo workspace with many empty crates as anticipatory architecture.
