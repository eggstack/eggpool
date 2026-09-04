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
