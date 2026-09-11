# Plan 168 — Rust Production Cleanup Roadmap

Date: 2026-09-11
Status: ready for handoff
Priority: P1 maintenance / post-migration consolidation
Execution target: GPT-5.6 Luna/Sol or comparable implementation model

## Purpose

EggPool has completed the Python-to-Rust production migration through M12/P007. The current application, runtime assets, provider transport, database ownership, CLI, packaging payload, and deployment behavior are Rust-owned. The repository now needs a bounded post-migration cleanup pass so the active tree reflects that reality instead of continuing to carry migration-era lint debt, unnecessarily broad dependency features, and migration planning/release scaffolding as first-class current architecture.

This roadmap is **not M13** and must not reopen parity work. It is ordinary product-maintenance work on the accepted Rust production tree.

The cleanup has three implementation plans:

1. Plan 169 — eliminate the strict-Clippy baseline and make Rust static quality an ordinary CI invariant.
2. Plan 170 — audit and minimize the Rust dependency/feature graph without reducing supported production behavior.
3. Plan 171 — extract still-live release/rollback contracts from migration-era paths, then retire the active migration scaffold and stale migration navigation.

## Current findings

### Rust quality gate

Accepted migration closure still records 66 Clippy errors and one warning under:

```bash
cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
```

Ordinary CI runs `cargo fmt` and `cargo test` but does not run Clippy. Now that Rust is the only production runtime, this should be resolved rather than preserved as migration-era accepted debt.

### Dependency/feature graph

The native stack is broadly coherent: Axum/Tower, Hyper/Rustls, Tokio, bundled SQLite, Serde/TOML, tracing, and Eggress for account-specific outbound proxying. The broadest feature surface is Eggress: `eggress-embed` enables `common`, `pproxy-compat`, `extended`, `pproxy-legacy`, `legacy-crypto`, and `ssh`; `eggress-server` enables `extended`, `legacy-crypto`, `pproxy-legacy`, and `ssh`; several Eggress component crates are also direct dependencies.

This cannot be reduced by assumption. `rust/src/providers/transport.rs` directly uses Eggress core/config/pproxy/URI/server/SSH components and a distinct SSH-chain path. The audit must derive the minimum feature set from supported proxy syntax and executable source paths, then prove behavior before removal.

### Migration scaffold

The migration registry reports M4–M12 closed and no dependency-ready migration plan. Nevertheless, `migration-rs/` remains a large historical system and root tooling metadata still calls itself `migration-tooling-only`.

The directory cannot simply be deleted yet. Current release documentation still invokes `migration-rs/closure/cutover/` artifacts for release rehearsal and publication verification, while retained scripts use `cutover`/`m12` names for durable release, exact-version rollback, package authority, and artifact contracts.

The target state is: durable compatibility/release contracts live under neutral current paths; Git history is the migration archive; no current runtime/release path depends on `migration-rs/`.

## Governing constraints

1. Do not change provider routing, retry, finalization, wire transcoding, account isolation, SQLite schema semantics, or runtime-generation ownership merely to simplify cleanup.
2. Do not remove a dependency or Cargo feature solely because a text search appears empty; confirm the resolved feature graph and compile/test the reduced graph.
3. Preserve supported per-account proxy behavior, including pproxy-compatible URI/chaining behavior and SSH where currently documented/tested.
4. Do not weaken TLS verification or replace Rustls/webpki behavior.
5. Do not replace one dependency with a new framework for trivial savings.
6. Do not introduce a workspace/crate split as cleanup.
7. Preserve exact-version rollback safety, package-manager ownership detection, release artifact validation, historical catalog semantics, and public-release verification.
8. Migration history may leave the active tree only after all live consumers have been extracted and a repository-wide reference audit is clean.
9. Git history is the archival authority; do not create a second large archive directory.
10. Keep CI proportionate to a LAN/SBC application: format, tests, strict Clippy, bounded tooling checks; no benchmark/coverage/fuzz infrastructure in this cleanup.
11. Plans 169–171 are ordinary implementation plans; do not create a new migration registry.

## Sequence

### Phase A — Plan 169

Dependency-ready immediately. Classify and correct strict-Clippy findings, using narrow justified allowances only when necessary. Add strict Clippy to CI only after the tree is clean.

Exit: `cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings` passes and CI enforces it.

### Phase B — Plan 170

May proceed after or alongside Plan 169, but final verification should run against the clean lint baseline.

Exit: every direct dependency/non-default feature has a current justification; unnecessary dependencies/features are removed; supported provider/proxy/TLS/runtime behavior remains qualified.

### Phase C — Plan 171

Start after production/release boundaries stop moving. Move live compatibility/release artifacts and validators to neutral paths/names, verify them, then remove migration-only docs/scaffolding.

Exit: no current code, test, workflow, release procedure, packaging path, installer/updater path, or contributor navigation depends on `migration-rs/`.

## Aggregate verification

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked --release
uv sync --frozen
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
git diff --check
```

Run release/compatibility validators under their final neutral names/paths after Plan 171. Use focused provider-transport tests for every Eggress feature reduction.

## Non-goals

No new provider protocols, Windows support, package channel, HTTP/2, TLS stack, proxy stack, Rust rewrite of every Python tooling script, or new planning framework. Retained Python tooling may remain where it is the smallest safe release/compatibility implementation.

## Completion definition

The repository should read and behave like a mature Rust application: strict Rust linting is green and enforced, dependencies/features are justified by current behavior, release/rollback tooling has neutral ownership, historical migration machinery is no longer active navigation, and supported install/update/provider/state contracts remain intact.
