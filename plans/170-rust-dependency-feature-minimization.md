# Plan 170 — Rust Dependency and Feature-Set Minimization

Date: 2026-09-11
Status: complete (verified 2026-09-11)
Parent roadmap: `plans/168-rust-production-cleanup-roadmap.md`
Priority: P1/P2 maintenance, binary/build/attack-surface reduction
Execution target: GPT-5.6 Luna/Sol or comparable implementation model

## Purpose

Audit every direct Rust dependency and explicitly enabled Cargo feature against current EggPool production behavior, then remove only demonstrably unnecessary dependencies/features. The objective is a smaller, clearer dependency authority for a LAN/SBC-oriented native proxy without reducing compatibility or replacing working subsystems.

The highest-value target is Eggress because EggPool currently enables a broad pproxy-compatible feature set and directly depends on several Eggress components. However, current provider transport directly uses those components and explicit SSH-chain handling, so this is a provenance/feature-graph audit rather than an instruction to delete Eggress.

## Current dependency observations

The present stack includes server/runtime (Axum, Tower/Tower HTTP, Tokio, tracing), provider HTTP (Hyper, hyper-util, hyper-rustls, Rustls, webpki roots, bytes/http/http-body-util), persistence (`tokio-rusqlite` with bundled SQLite and backup), serialization/config (Serde, serde_json `preserve_order`, TOML), operations/platform (Clap, nix, zip, sha2, getrandom, arc-swap), and Eggress transport/component crates.

`rust/src/providers/transport.rs` directly imports and executes Eggress core target types, pproxy parser/translator, config compiler, chain executor, URI hop specifications, and SSH session cache. It also directly uses `tower_service::Service`. Those dependencies must not be classified as redundant merely because `eggress-embed` is present.

## Governing constraints

1. Preserve all currently documented/supported provider proxy URI forms and chaining behavior unless a separate explicit compatibility decision deprecates one.
2. Preserve SSH proxy chains if current docs/config/tests advertise or exercise them.
3. Preserve TLS certificate verification and production trust behavior.
4. Preserve direct HTTP/HTTPS transport, connection pooling, timeout/error classification, account isolation, and coordinator retry ownership.
5. Preserve bundled SQLite portability and backup/restore behavior unless a separate measured plan changes it.
6. Preserve JSON ordering wherever it affects request fidelity, stable output, hashes, or compatibility; remove `preserve_order` only with proof.
7. Do not add replacement dependencies merely to reduce direct-dependency count.
8. Do not restructure into a Cargo workspace or multiple crates.
9. Dev dependencies may be removed only when their last meaningful deterministic regression no longer needs them.
10. Keep `unsafe_code = "forbid"`.

## Workstream A — Build an ephemeral dependency/feature provenance map

Use Cargo as authority:

```bash
cargo tree --manifest-path rust/Cargo.toml -e features
cargo tree --manifest-path rust/Cargo.toml --duplicates
cargo metadata --manifest-path rust/Cargo.toml --format-version 1
```

For each direct dependency and explicitly selected feature, identify production source use, build-time use, test-only use, packaging/platform necessity, or transitive feature activation required by a supported contract. Do not commit a permanent inventory generator.

Pay particular attention to direct dependencies present because a trait/type must be named versus truly redundant declarations.

## Workstream B — Audit the Eggress production feature matrix

Map actual supported `proxy_url` grammar, docs/examples, transport code, and tests to:

- `common`;
- `pproxy-compat`;
- `extended`;
- `pproxy-legacy`;
- `legacy-crypto`;
- `ssh`;
- direct `eggress-config`, `eggress-server`, `eggress-uri`, `eggress-pproxy-compat`, `eggress-core`, and `eggress-transport-ssh` dependencies;
- dev-only Shadowsocks/Trojan crates.

For every candidate feature removal:

1. prove no supported URI/protocol requires it;
2. build with the feature removed;
3. run focused provider-transport/proxy tests;
4. inspect the resolved feature graph to confirm it did not remain transitively enabled;
5. preserve error classification and bounded connection/shutdown behavior.

If a feature is required by a narrow path such as SSH chaining, retain it and record that reason. Do not remove `pproxy-compat` while EggPool accepts pproxy-style account proxy URLs.

## Workstream C — Audit HTTP/TLS feature flags

Review:

- `hyper-rustls`: `http1`, `ring`, `tls12`, `webpki-tokio`;
- `hyper-util`: `client-legacy`, `http1`, `tokio`;
- `rustls`: `ring`, `tls12`;
- Tokio: `io-util`, `macros`, `net`, `process`, `rt`, `signal`, `sync`, `time`;
- Tower/Tower HTTP.

Trace each feature to current callers. Retain compatibility-sensitive TLS 1.2 unless product support explicitly excludes it. Do not enable HTTP/2 or replace ring/Rustls as cleanup.

Check whether `webpki-roots` is consumed directly or only transitively. Remove a direct dependency only if code does not name it and required root behavior remains deterministic.

## Workstream D — Audit persistence/config/operations features

Verify:

- `tokio-rusqlite` `bundled` is intentional for qualified Linux/macOS portability;
- `backup` is required by backup/restore operations;
- `serde_json/preserve_order` has a concrete compatibility/fidelity requirement;
- `nix` features `signal`, `term`, and `user` each have live callers;
- `zip` with defaults disabled covers updater/release archive behavior;
- arc-swap, sha2, getrandom, toml, thiserror, tracing and other direct dependencies have current ownership.

Delete only unused features/dependencies. Do not hand-roll platform, crypto, parser, or archive behavior to remove a small crate.

## Workstream E — Audit build/dev dependencies

Justify build dependencies through `build.rs`/asset generation. Verify whether Shadowsocks/Trojan protocol crates are still needed for deterministic proxy peers. If they protect supported behavior, retain them; if retained tests no longer name them, remove them and orphaned fixtures together. Do not replace deterministic fixtures with live-network tests.

## Workstream F — Measure effect without creating benchmark infrastructure

Before/after accepted removals, record informational `Cargo.lock` package count and local release-binary size when practical. These are diagnostics, not thresholds. Prefer ownership/attack-surface simplification over churn for trivial byte savings.

## Required verification

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --test provider_transport -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked --release
cargo tree --manifest-path rust/Cargo.toml -e features
```

For each removed Eggress feature/protocol dependency, run the specific proxy URI/chaining tests proving the retained surface. If `Cargo.lock` changes, verify locked builds and current local packaging smoke.

## Acceptance criteria

- Every remaining direct dependency and explicit feature has an identified current owner/use.
- Unused direct dependencies/features are removed and `Cargo.lock` reflects the actual graph.
- No supported provider/proxy URI, SSH chain, TLS mode, backup path, CLI operation, updater behavior, or package target is accidentally lost.
- Strict Clippy and the complete Rust suite pass.
- Reduction adds no replacement framework/subsystem.
- Docs/config are changed only if supported behavior intentionally changes.

## Handoff note

A valid outcome may retain most current Eggress features if each maps to a supported compatibility contract. Success means the graph is justified and minimal, not that a predetermined crate count is reached.

## Closure evidence

Plan 170 was implemented without changing provider behavior or introducing a
replacement dependency. The audit removed:

- the unused direct `tower-http` dependency and its orphaned lockfile package;
- the no-op `eggress-embed` `common` feature declaration, which only activated
  Eggress's empty `common` feature path.

The remaining direct crates and explicit features were retained with these
owners:

- `eggress-core`, `eggress-config`, `eggress-pproxy-compat`, `eggress-uri`,
  `eggress-server`, and `eggress-transport-ssh` are named by the provider
  transport's direct target, parser/translator, chain executor, and SSH
  session-cache paths;
- `eggress-embed` retains `pproxy-compat`, `extended`, `pproxy-legacy`,
  `legacy-crypto`, and `ssh` for pproxy-compatible HTTP/SOCKS,
  Shadowsocks/Trojan/extended protocols, legacy methods/plugins, and SSH
  chains. The deterministic Shadowsocks, Trojan, and SSH fixtures remain
  available under `test-support`;
- Hyper/Rustls retains HTTP/1.1, `ring`, TLS 1.2, and webpki roots for provider
  transport and updater verification. `hyper-util` retains its legacy client,
  HTTP/1.1, and Tokio adapters;
- Tokio, `nix`, bundled/backup SQLite, `serde_json` ordering, archive support,
  and the remaining build/dev dependencies each have live runtime, packaging,
  or deterministic regression owners.

Informational measurements were 381 Cargo.lock packages and a 29,960,240-byte
release binary before the change; the resulting graph has 380 packages and a
29,960,184-byte release binary.

Verification on the final implementation tree:

```text
cargo fmt --manifest-path rust/Cargo.toml --all -- --check       pass
cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings  pass
cargo test --manifest-path rust/Cargo.toml --test provider_transport -- --test-threads=1  30 passed
cargo test --manifest-path rust/Cargo.toml --features test-support --test provider_transport -- --test-threads=1  35 passed
cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1  469 passed, 52 suites
cargo build --manifest-path rust/Cargo.toml --locked --release       pass
uv sync --frozen                                                   pass
uv run ruff format --check scripts/ tests/tooling/                  42 files formatted
uv run ruff check scripts/ tests/tooling/                            pass
uv run pyright scripts/                                             0 errors
uv run pytest tests/tooling/ -q --tb=short --maxfail=1              76 passed
uv run python scripts/validate_cutover_docs.py                      pass
uv run python scripts/validate_m12_retirement.py                    pass
```

The resolved `cargo tree --manifest-path rust/Cargo.toml -e features` contains
no `tower-http` package or `eggress-embed` `common` feature after the change;
the duplicate graph and `git diff --check` also pass.
