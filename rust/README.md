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

## T002 direct provider transport

`eggpool::providers::ProviderHttpClient` is the migration transport boundary
for direct provider HTTP/HTTPS. It uses one cheap-to-clone Hyper HTTP/1.1
client per future provider scope, Rustls with explicit Mozilla webpki roots,
and a connection-lifetime semaphore that bounds physical connections while
idle sockets remain in the pool. Pool wait, connect, write, read, TLS, and
protocol failures are exposed as stable `TransportError` categories. Bodies
are consumed incrementally through `ProviderBody::next`; transport does not
buffer complete responses or inject provider credentials.

The direct client disables ambient proxy behavior by construction. Additional
DER roots are available only as an explicit constructor setting for
deterministic test CAs.

T005 qualification is complete. The transport boundary has no implicit
request retry: coordinator-owned retry/failover remains downstream, and
response bodies are still consumed incrementally. Run the neutral provider
transport qualification tests with:

```bash
cargo test --manifest-path rust/Cargo.toml --test provider_transport -- --test-threads=1
```

This is transport-only evidence; it does not claim provider inference
dispatch, routing, codecs, or production Rust cutover.

## T004 provider/account client pool

`eggpool::providers::ProviderClientPool` builds one direct Hyper/Rustls client
per configured provider and one dedicated Eggress-backed client for each
configured account with a resolved proxy. Direct accounts fall back to the
provider client; a configured proxy never falls back to direct transport.
The pool is immutable after construction, exposes a credential-free topology
snapshot, and is stored in the server application state. Pool construction is
generation-candidate work and fails closed before the server is exposed. The
server drops the pool after graceful shutdown, releasing direct and proxied
Hyper connection pools; routing, credentials, retries, and generation swaps
remain downstream work.

## F005 Axum read-plane server

The Rust candidate now has a development-only Axum server for the first
dashboard/read-plane slice. Build it, choose a port different from the Python
server, and run it with an existing compatible config:

```bash
cargo build --manifest-path rust/Cargo.toml
rust/target/debug/eggpool --config ./config.toml serve --verbose
```

The current Rust routes are `/v1/healthz`, `/v1/readyz`, `/`,
`/api/stats/summary`, and the dashboard resources under `/static/`. The
inference paths `/v1/chat/completions`, `/v1/messages`, and `/v1/responses`
are explicit placeholders for a later provider milestone. Python remains the
production server and should continue to run on its own port during migration.
The explicit `serve --verbose` form is the only supported Rust invocation at
this stage. Plain `serve` (Python's daemon mode), `--log-file`, `--quiet`, and
`--as-root` are parsed for CLI compatibility but fail explicitly because daemon
and root-gated lifecycle behavior belongs to the later runtime milestone.
Choose separate writable database paths for Python and Rust, or copy a source
fixture once and give each candidate its own writable copy; do not run both
implementations against the same writable SQLite file.

Copied dashboard resources are checked against the Python source tree by the
manifest test:

```bash
cargo test --manifest-path rust/Cargo.toml copied_asset_manifest_matches_the_frozen_python_source
```

## F004 SQLite compatibility baseline

`eggpool::db::Database` owns one `tokio-rusqlite` worker and a single
operation permit. Read calls and complete `BEGIN IMMEDIATE` transactions are
serialized on that connection; repositories never open pooled writers. The
database options can be built directly from the closed F003 config model with
`DatabaseConfig::from(&config.database)`.

The build script reads `src/eggpool/db/schema/*.sql` and `checksums.json`
through a structural JSON parser. It embeds those exact canonical files and
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

`version`, `--help`, `check-config`, and the development-only `serve` command
are implemented in Rust. Other commands and options are represented by the
full parser tree but currently exit with `not implemented in Rust candidate`;
this is an explicit migration-stage boundary and is not a final cutover
behavior. The Python `eggpool` executable remains the production command
throughout migration.
