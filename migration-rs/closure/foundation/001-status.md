# F001 Closure — Rust Workspace and Build Scaffold

Status: closed

Recommendation: closed

Implementation commit: [`573e081f`](https://github.com/eggstack/eggpool/commit/573e081f)

Plan: [Foundation Milestone F001](../../implementation/foundation/001-rust-workspace-and-build-scaffold.md)

## Requirement-to-evidence matrix

| Requirement | Evidence | Result |
|---|---|---|
| One isolated, non-published Cargo package under `rust/` | `rust/Cargo.toml` declares one package, `publish = false`, and one library/binary target; `cargo metadata --no-deps` reports `publish: []` and target directory `rust/target` | Pass |
| Edition/MSRV policy is explicit | Manifest declares edition 2024 and `rust-version = "1.85"`; `rust/README.md` explains that Rust 1.85 is the edition-2024 MSRV | Pass |
| Binary name is `eggpool` and version comes from package metadata | Cargo target metadata names the binary `eggpool`; `--version` prints `eggpool 0.7.4`; `version` prints `0.7.4`; source uses `env!("CARGO_PKG_VERSION")` | Pass |
| Minimal process bootstrap exists | `src/main.rs` uses a current-thread Tokio runtime; `src/runtime.rs` initializes `tracing-subscriber` and dispatches only the scaffold commands | Pass |
| Help/version probes do not start a server | `rust/target/debug/eggpool --help`, `--version`, and `version` exit successfully; no server or network dependency exists in the package | Pass |
| Top-level errors are typed and operator-safe | `AppError`/`BootstrapError` use `thiserror`; invalid arguments exit 2 with Clap usage text; unit coverage verifies no debug type name leaks | Pass |
| Dependency graph remains narrow | `cargo tree --depth 1` contains only `clap`, `thiserror`, `tokio`, `tracing`, and `tracing-subscriber` as direct dependencies; no Axum/Hyper/Serde/SQLite/Eggress stack was introduced | Pass |
| Python installation and behavior remain untouched | `uv run eggpool --help` resolves the existing Click CLI and `uv run eggpool version` prints `0.7.4`; Rust artifacts are confined to ignored `rust/target/` | Pass |

## Differential and compatibility results

F002's black-box harness is not yet implemented, so no formal cross-process
differential corpus is claimed here. The required narrow oracle smoke passed:

- Python `uv run eggpool --help` resolved the existing command hierarchy.
- Python `uv run eggpool version` printed `0.7.4`.
- Rust `--version` and `version` both printed the same package version.

Rust's top-level `--version` flag is a migration probe supplied by Clap; the
Python CLI's version surface remains the `version` subcommand. This is confined
to the side-by-side candidate and is not a cutover claim; F003 owns full CLI
parity.

## Verification commands actually run

All commands were run from the repository root with the explicit manifest path
where applicable:

```text
cargo fmt --manifest-path rust/Cargo.toml -- --check          PASS
cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings  PASS
cargo test --manifest-path rust/Cargo.toml                    PASS (3 tests)
cargo build --manifest-path rust/Cargo.toml                   PASS
rust/target/debug/eggpool --help                              PASS
rust/target/debug/eggpool --version                           PASS: eggpool 0.7.4
rust/target/debug/eggpool version                             PASS: 0.7.4
rust/target/debug/eggpool --definitely-invalid                PASS: exit 2, concise Clap error
uv run eggpool --help                                         PASS: existing Python CLI
uv run eggpool version                                        PASS: 0.7.4
cargo tree --manifest-path rust/Cargo.toml --depth 1           PASS
cargo metadata --manifest-path rust/Cargo.toml --no-deps --format-version 1  PASS
```

The scaffold's three unit tests cover help/version parser recognition, Cargo
package-version exposure, and safe top-level error formatting.

## Migration boundary evidence

- Database, config, API, SSR, provider, installer, and systemd behavior were
  not changed; those areas are explicitly out of scope for F001.
- Building and probing created only `rust/target/` artifacts, which is ignored
  by `.gitignore`. No tracked Python/config/database file was modified.
- The package has no provider/network/database code and no long-lived server,
  so restart, contention, cancellation, and durable-state behavior remain
  deferred to later milestones.
- `#![forbid(unsafe_code)]` and the manifest Rust lint policy forbid unsafe Rust
  in the scaffold.

## Known limitations and unresolved findings

The Rust candidate intentionally implements no production command, config
loading, HTTP server, database access, provider access, installer integration,
or deployment behavior. These are planned for F002 onward and are not closure
defects for F001.

Unresolved findings by severity: none.

## Planning follow-through

F001 is removed from the dependency-ready section of `migration-rs/registry.md`
and recorded in its completed section. F002 is now ready for handoff because
the stable explicit binary path and invocation convention exist. F003 and F004
remain blocked on F002; F005 remains blocked on F002 and its F004 interface.
