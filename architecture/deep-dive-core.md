# Deep Dive: Core Runtime

Back to [Architecture](README.md)

## Ownership

The native executable is bootstrapped by `rust/src/main.rs` and
`rust/src/cli.rs`. Configuration, validation, error mapping, path resolution,
interactive onboarding, deployment, update, and process control are Rust-owned
operations. Python is retained only for release and validation tooling.

## Configuration

`rust/src/config.rs` parses TOML, resolves defaults, validates provider/account
and wire-surface definitions, and rejects unsafe cross-field combinations.
Configuration resolution is explicit `--config`, `$EGGPOOL_CONFIG`, the XDG
user path, then `./config.toml`. Secrets come from the environment or adjacent
`.env` and are never included in metadata-only diagnostics.

`rust/src/config_reload_policy.rs` exposes the pure `classify_transition`
authority and its redacted `ConfigTransition` result for live,
restart-required, and unchanged candidates. Atomic text mutations carry that
result into apply logic; `rust/src/reload.rs` independently revalidates and
reclassifies the on-disk candidate before it builds and publishes a complete
generation atomically. A mixed transition is wholly restart-required.
The repository-root configuration examples are the canonical build inputs;
`rust/build.rs` embeds the default example for `eggpool init-config`.
`[server].threads` remains accepted for compatibility and diagnostics, but the
executable uses Tokio's `current_thread` runtime and does not use that field to
select worker threads.

## CLI and errors

`rust/src/cli.rs` owns the command tree. `rust/src/runtime.rs` owns command
dispatch, stable exit-code adaptation, human output, and JSON output. The
operations modules implement `serve`, `rehash`, `connect`,
`logout`, `update`, `deploy`, `backup`, `recover`, `uninstall`, and the
diagnostic commands.
The reusable local process workflow is `rust/src/operations/lifecycle.rs`;
CLI-only prompts remain in the runtime adapter.

`rust/src/error.rs` defines the typed error hierarchy and HTTP/status mapping.
Errors retain structured context without credential values or raw request
bodies. The server maps local validation, capability, model, upstream, and
transport failures to their public contracts.

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
