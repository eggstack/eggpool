# Deep Dive: Core Runtime

Back to [Architecture](README.md). See also the review index in
[overview.md](overview.md).

## Ownership

The native executable is bootstrapped by `rust/src/main.rs` and
`rust/src/cli.rs`. `rust/src/main.rs` runs Tokio's `current_thread` runtime
and maps `AppError` to `ExitCode` via `AppError::exit_code`.
`rust/src/lib.rs` declares the module tree (`accounts`, `catalog`, `cli`,
`config`, `config_reload_policy`, `coordinator`, `db`, `error`, `health`,
`model_router`, `operations`, `providers`, `quota`, `reload`, `request`,
`routing`, `runtime`, `runtime_lifecycle`, `server`, `task_supervisor`,
`version`, `wire`), re-exports the CLI/config/error boundary
(`Cli`, `Command`, `Config`, `AppError`, `BootstrapError`), and forbids
`unsafe_code`. Configuration, validation, error mapping, path resolution,
interactive onboarding, deployment, update, and process control are Rust-owned
operations. Python is retained only for release and validation tooling.

## Configuration

`rust/src/config.rs` parses TOML (`from_toml`/`validate`), resolves defaults,
validates provider/account and wire-surface definitions, and rejects unsafe
cross-field combinations. Configuration resolution is explicit `--config` >
`$EGGPOOL_CONFIG` > `~/.config/eggpool/config.toml` (`$XDG_CONFIG_HOME`-aware)
> `./config.toml` (`resolve_config_path`). Secrets come from the environment
or the adjacent `.env` (`$EGGPOOL_ENV` override via `resolve_env_path`) and
are never included in metadata-only diagnostics.
`[integrations].advertise_base_url` is live-reloadable profile output only and
never changes the listen socket.

`rust/src/config_reload_policy.rs` exposes the pure `classify_transition`
authority and its redacted `ConfigTransition` result for live,
restart-required, and unchanged candidates. Atomic text mutations carry that
result into apply logic; `rust/src/reload.rs` independently revalidates and
reclassifies the on-disk candidate before it builds and publishes a complete
generation atomically. A mixed transition is wholly restart-required.
The repository-root configuration examples are the canonical build inputs:
`config.example.toml` and `config.sbc.example.toml`. `rust/build.rs` (via
`rust/build_support.rs`) tracks both as inputs, embeds the default example
for `eggpool init-config`, and embeds the `rust/assets/db/migrations/` chain
with checksums; there is no second Rust-local copy to synchronize.
`[server].threads` remains accepted for compatibility and diagnostics
(validated `1..=64`, restart-required), but the executable uses Tokio's
`current_thread` runtime and does not use that field to select worker threads.

## CLI, errors, version

`rust/src/cli.rs` owns the command tree (`serve`, `connect`, `logout`,
`check-config`, `edit`, `getkey`, `newkey`, `configsetup`, `configremote`,
`deploy`, `accounts`, `dashboard`, `db`, `models`, `modelinfo`, `stats`,
`onboard`, `croncheck`, `ensure-running`, `migrate`, `stop`, `restart`,
`init-config`, `help`, `recover`, `uninstall`, `update`,
`install-provenance`, `set`, `rehash`, `runtime-status`, `status`, `backup`,
`version`). `rust/src/runtime.rs` owns command dispatch, stable exit-code
adaptation, human output, and JSON output. CLI-only prompts remain in the
runtime adapter.
The reusable local process workflow is `rust/src/operations/lifecycle.rs`;
`rust/src/operations/status.rs` owns compact proxy/provider health
aggregation shared with `readyz` (see [Control](deep-dive-control.md));
`rust/src/server/*` stays thin with no coordinator retries/finalization.

`rust/src/error.rs` owns the top-level typed hierarchy only:
`AppError::Cli`/`Bootstrap` with `exit_code()`, and `BootstrapError`
(`Output`, `NotImplemented`, `Config`, `Server`, `ServeUnsupported`,
`Command { code, detail }`, `Interrupted`). `Command` carries its stable
exit code, `Interrupted` exits 130, everything else exits 1. HTTP/status
mappings for server surfaces live with their adapters, not here; errors
retain structured context without credential values or raw request bodies.
The server maps local validation, capability, model, upstream, and
transport failures to their public contracts.

`rust/src/version.rs` exposes `PACKAGE_VERSION` from `CARGO_PKG_VERSION`
(currently `0.8.0`).

## Dependency and feature authority

`rust/Cargo.toml` (package `eggpool`, currently `0.8.0`) plus its locked
resolved graph is the native dependency authority. Exact pins:
`eggserve-server =0.4.0` (`tower` feature), the Eggress `1.0.8` family
(normal path via `eggress-outbound` directly), and
`eggfetch-core =0.2.0` (`native-http1`, `tls-rustls`). Feature gates:
default `ssh` (root capability forwarded to `eggress-outbound/ssh` plus the
compat crate's SSH translation support); `--no-default-features` still
compiles/tests, keeps direct/non-SSH proxy paths, and rejects SSH proxy
config as `TransportError::ProxyConfiguration` before dialing;
test-only `test-support`; non-default dependency-free
`qualification-db-diagnostics` (qualification tooling only, never a
release/package capability). `deny.toml` + `cargo deny check` gates
licenses/advisories/sources/duplicates; `unsafe_code = "forbid"` is a repo
invariant. `rust/src/db/qualification.rs` and
`rust/src/operations/terminal.rs` belong to their own deep dives
([Database](deep-dive-database.md), [Control](deep-dive-control.md)),
not this file.

## Serialization and security

The Rust runtime owns JSON serialization, request-size limits, redaction,
header filtering, and constant-time local API-key comparison. Wire bodies are
bounded before parsing and canonical request data is retained only for the
duration and accounting needs of the request lifecycle.

## Developer boundary

Build and test the current application with Cargo:

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked
```

`uv` is used only to run validators and tests under `scripts/` and
`tests/tooling/`; it is not a production runtime dependency.
