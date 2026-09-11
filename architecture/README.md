# Architecture

This directory is the current design index. It describes the native Rust
runtime shipped by EggPool; migration plans and historical evidence remain
under `migration-rs/`.

## Runtime shape

`rust/src/main.rs` and `rust/src/cli.rs` own the executable and command tree.
The process owns configuration, SQLite, readiness, task supervision, runtime
generations, provider clients, routing, request coordination, wire adaptation,
operations, and graceful shutdown. The repository-root `pyproject.toml` and
the scripts under `scripts/` are development/release tooling only.

`RuntimeManager` publishes immutable active and retiring generation slots. A
generation contains provider clients, catalog, router, coordinator, health,
statistics, and generation-leased background work. Rehash builds a complete
candidate and swaps it atomically; request leases keep in-flight work on the
generation it acquired. The model-router registry is generation-owned, while
bounded affinity is process-owned and never stores raw request or credential
data.

## Request lifecycle

`rust/src/coordinator/` owns endpoint detection, bounded request preparation,
model/account routing, durable request and attempt state, provider dispatch,
response adaptation, retry classification, and terminal finalization.
Canonical wire intent is captured before provider adaptation in
`rust/src/wire/ir.rs`; all alternate targets encode from that source rather
than chaining translated payloads. Native stream adapters require provider
terminal evidence and never synthesize a terminal event from transport EOF.

## Subsystem ownership

| Subsystem | Current implementation |
|---|---|
| CLI, configuration, errors | `rust/src/cli.rs`, `rust/src/config.rs`, `rust/src/error.rs` |
| Request and coordinator | `rust/src/request/`, `rust/src/coordinator/` |
| Routing, quota, health | `rust/src/routing/`, `rust/src/quota/`, `rust/src/health/` |
| Providers and wire surfaces | `rust/src/providers/`, `rust/src/wire/` |
| SQLite and migrations | `rust/src/db/`, `rust/assets/db/migrations/` |
| Runtime and reload | `rust/src/runtime_lifecycle.rs`, `rust/src/reload.rs` |
| Dashboard and operations | `rust/src/server.rs`, `rust/src/operations/` |

See the corresponding deep dive for details:

- [Core](deep-dive-core.md)
- [Request lifecycle](deep-dive-request-lifecycle.md)
- [Transcoding](deep-dive-transcoder.md)
- [Routing](deep-dive-routing.md)
- [Providers](deep-dive-providers.md)
- [Database](deep-dive-database.md)
- [Runtime](deep-dive-runtime.md)
- [Health](deep-dive-health.md)
- [Background work](deep-dive-background.md)
- [Dashboard](deep-dive-dashboard.md)
- [Catalog](deep-dive-catalog.md)
- [Model info](deep-dive-model-info.md)
- [Control plane](deep-dive-control.md)
- [Data models](deep-dive-models.md)
- [Integrations](deep-dive-integrations.md)
- [Security](deep-dive-security.md)
- [Observability](deep-dive-observability.md)
- [Retry](deep-dive-retry.md)
- [Metrics](deep-dive-metrics.md)
- [Lifecycle](deep-dive-lifecycle.md)
- [Deployment](deep-dive-deployment.md)

## Configuration and deployment

Configuration resolves in this order: explicit `--config`,
`$EGGPOOL_CONFIG`, the XDG user config path, then `./config.toml`. API keys
come from the environment or the adjacent `.env`. Live reload policy is owned
by `rust/src/config_reload_policy.rs`; unsupported or disruptive changes fail
closed and require restart.

The current release is a native Rust wheel with embedded runtime assets. The
supported release target classes are Linux x86_64, Linux aarch64, and macOS
arm64. Historical Python wheels remain immutable external artifacts and are
available only for explicit catalogued exact-version compatibility transitions.

## Source development

Run current runtime checks from the repository root:

```bash
cargo fmt --manifest-path rust/Cargo.toml -- --check
cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked
```

Use the Python tooling environment only for release validators and tooling
tests. Do not import, run, or recreate the retired application source tree.
