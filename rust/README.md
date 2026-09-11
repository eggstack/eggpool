# EggPool Rust application

This directory owns the current EggPool application and runtime. The
publication manifest in `../packaging/pypi/pyproject.toml` builds this binary
as a platform-specific wheel; the repository-root `pyproject.toml` is
tooling-only.

## W002 canonical request boundary

`eggpool::request` owns pure bounded admission, overflow-safe request/media/
document/token estimates, and compact JSON body preparation. It parses a
request once, retains only bounded canonical data plus accounting estimates,
and never selects accounts or submits provider traffic. `eggpool::wire::ir`
contains the source-owned request, response, usage, provider-error, and stream
event semantics used by later static wire codecs. M5 routing and model-router
affinity are reached only through pure adapters supplied with caller-owned
static facts.

## Toolchain policy

The package uses Rust edition 2024 and declares Rust 1.85 as its MSRV, the
first stable toolchain with edition-2024 support. The current development
toolchain may be newer, but code should remain compatible with the declared
MSRV and intended deployment targets.

## Source-development flow

Run commands from the repository root and always pass the manifest path:

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked
rust/target/debug/eggpool --help
rust/target/debug/eggpool version
```

The build output is confined to `rust/target/`. Do not use `cargo install` or
copy the binary into a global/user executable directory for routine
development. Use the built binary directly, or build a local wheel through
`packaging/pypi/pyproject.toml` when qualifying package installation.

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

T006 extended proxy qualification is complete. The transport boundary has no implicit
request retry: coordinator-owned retry/failover remains downstream, and
response bodies are still consumed incrementally. Run the neutral provider
transport qualification tests with:

```bash
cargo test --manifest-path rust/Cargo.toml --test provider_transport -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --features test-support --test provider_transport -- --test-threads=1
```

The feature-enabled command runs deterministic local Shadowsocks, SSR, Trojan,
and OpenSSH peers. The Trojan CA override is test-only; SSH uses the same
production `ProviderHttpClient::new_with_proxy` path as configured accounts.
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

## Runtime and server

Build and run the application with an existing compatible config:

```bash
cargo build --manifest-path rust/Cargo.toml
rust/target/debug/eggpool --config ./config.toml serve --verbose
```

The runtime is Rust-only. Current development and qualification must use a
disposable configuration/database when isolation is needed; never share a
writable SQLite database between independent processes.

## F004 SQLite compatibility baseline

`eggpool::db::Database` owns one `tokio-rusqlite` worker and a single
operation permit. Read calls and complete `BEGIN IMMEDIATE` transactions are
serialized on that connection; repositories never open pooled writers. The
database options can be built directly from the closed F003 config model with
`DatabaseConfig::from(&config.database)`.

The build script reads the Rust-owned `rust/assets/db/migrations/*.sql` and
`checksums.json` through a structural JSON parser. It embeds those exact
canonical files and validates their SHA-256 values before applying them. Rust uses the existing
`_migrations` ledger and accepts the historical no-extension ledger names in
`tests/fixtures/schema/pre_phase17_v11.sql`; it does not renumber or rewrite
migrations. A failed transaction explicitly rolls back. Rollback failure, or a
commit failure whose rollback cannot prove the connection clean, closes
admission and the worker; a commit failure with a verified rollback remains a
typed, usable failure just as in the Python oracle.

The Rust repositories and runtime own account, model, request, provider-ping,
usage, finalization, quota, catalog, backup, and recovery behavior. Historical
compatibility fixtures live under `migration-rs/fixtures/` and are not read by
the production runtime.

## F003 config and CLI compatibility

The migration candidate resolves configuration in the same order as Python:
an explicit `--config` path, `$EGGPOOL_CONFIG`, the XDG user config path when
it exists, and finally `./config.toml`. It validates the supported TOML shape,
defaults, legacy flat accounts, provider/auth/proxy forms, wire surfaces,
model routers, and cross-field safety rules without printing credential values.

Useful source-development probes are:

```bash
rust/target/debug/eggpool --config ./config.toml check-config
rust/target/debug/eggpool --help
rust/target/debug/eggpool serve --help
```

`version`, `--help`, `check-config`, and the complete operational command tree
are implemented in Rust. Historical Python releases remain external,
explicit exact-version package-manager targets only; they are not a source or
runtime fallback for this checkout.
