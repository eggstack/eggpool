# EggPool Rust migration scaffold

This is the non-published, side-by-side Rust candidate described by the
[migration plan](../migration-rs/implementation/foundation/001-rust-workspace-and-build-scaffold.md).
Python remains the canonical production implementation; this package must not
replace the installed `eggpool` command.

## Toolchain policy

The package uses Rust edition 2024 and declares Rust 1.85 as its MSRV, the
first stable toolchain with edition-2024 support. The current development
toolchain may be newer, but code should remain compatible with the declared
MSRV and intended deployment targets.

## Explicit-path development

Run commands from the repository root and always pass the manifest path:

```bash
cargo fmt --manifest-path rust/Cargo.toml -- --check
cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml
cargo build --manifest-path rust/Cargo.toml
rust/target/debug/eggpool --help
rust/target/debug/eggpool --version
rust/target/debug/eggpool version
```

The build output is confined to `rust/target/`. Do not use `cargo install` or
copy the binary into a global/user executable directory during migration.
Later parity work and black-box invocation conventions are tracked in the
[`migration-rs` guide](../migration-rs/README.md).

## F004 SQLite compatibility baseline

`eggpool::db::Database` owns one `tokio-rusqlite` worker and a single
operation permit. Read calls and complete `BEGIN IMMEDIATE` transactions are
serialized on that connection; repositories never open pooled writers. The
database options can be built directly from the closed F003 config model with
`DatabaseConfig::from(&config.database)`.

The build script reads `src/eggpool/db/schema/*.sql` and `checksums.json`
directly. It embeds those exact canonical files into the candidate and
validates their SHA-256 values before applying them. Rust uses the existing
`_migrations` ledger and accepts the historical no-extension ledger names in
`tests/fixtures/schema/pre_phase17_v11.sql`; it does not renumber or rewrite
migrations. A failed transaction explicitly rolls back. Rollback failure, or a
commit failure whose rollback cannot prove the connection clean, closes
admission and the worker; a commit failure with a verified rollback remains a
typed, usable failure just as in the Python oracle.

F004 currently exposes typed account, model, request, provider-ping, and
usage-rollup repositories for the first read plane. Full request finalization,
quota reservations, catalog maintenance, backups, and runtime recovery remain
unported and belong to later milestones. Python remains the production
implementation.

## F003 config and CLI compatibility

The migration candidate resolves configuration in the same order as Python:
an explicit `--config` path, `$EGGPOOL_CONFIG`, the XDG user config path when
it exists, and finally `./config.toml`. It validates the supported TOML shape,
defaults, legacy flat accounts, provider/auth/proxy forms, wire surfaces,
model routers, and cross-field safety rules without printing credential values.

Useful migration-only probes are:

```bash
rust/target/debug/eggpool --config ./config.toml check-config
rust/target/debug/eggpool --help
rust/target/debug/eggpool serve --help
```

`version`, `--help`, and `check-config` are implemented in Rust. Every other
command and option is represented by the full parser tree but currently exits
with `not implemented in Rust candidate`; this is an explicit migration-stage
boundary and is not a final cutover behavior. The Python `eggpool` executable
remains the production command throughout migration.
