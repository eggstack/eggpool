# Architecture

This directory is the current design index. It describes the native Rust
runtime shipped by EggPool. Historical migration context is limited to the
concise [migration history](../docs/migration-history.md) pointer; Git history
is the archival authority.

Agent entry: `AGENTS.md` is the workflow index, `.opencode/skills/` holds task
guidance (`architecture`, `development`, `deployment`, `documentation`,
`plan`), and `plans/` is append-only history. The review index in
`overview.md` maps each module to its authority paths and deep dive.

## Runtime shape

`rust/src/main.rs` and `rust/src/cli.rs` own the executable and command tree.
The process owns configuration, SQLite, readiness, task supervision, runtime
generations, provider clients, routing, request coordination, wire adaptation,
operations, and graceful shutdown. The repository-root `pyproject.toml` and
the scripts under `scripts/` are development/release tooling only.

Downstream TCP/HTTP/1 transport and connection drain are owned by the exact
EggServe runtime (`eggserve-server =0.2.1`). Its `TowerToEggserve` bridge in
`eggserve-core =0.2.2` enters EggPool's existing Axum router. EggPool retains
authentication, generation-owned body limits, application routing, inference,
provider transport, persistence, and process lifecycle.

`rust/src/runtime_lifecycle/` is the lifecycle package. `process.rs` owns
process-lifetime resources, `generation.rs` builds immutable candidates and
owns generation close, `lease.rs` owns slots and request/finalization leases,
`manager.rs` owns atomic publication and bounded retirement, `recovery.rs`
owns startup reconciliation, and `diagnostics.rs` owns bounded projections
and redaction helpers. `mod.rs` is only the compatibility facade and public
re-export surface.

`RuntimeManager` publishes immutable active and retiring generation slots. A
generation contains provider clients, catalog, router, coordinator, health,
statistics, and generation-leased background work. Rehash builds a complete
candidate and swaps it atomically; request leases keep in-flight work on the
generation it acquired. The model-router registry is generation-owned, while
bounded affinity is process-owned and never stores raw request or credential
data.

The shared `eggpool-model-routing` crate is a Rust 1.81-compatible neutral
boundary for policy validation/compilation, deterministic route IDs and
fingerprints, and hashed conversation identities. EggPool adapts TOML config
into its policy types and keeps selector execution, provider/account routing,
and the Tokio affinity cache in the application.

The shared `eggpool-client-config` crate is the portable client-configuration
boundary for Codex/OpenCode projection, connection profiles, `epc1` tokens,
renderers, mutation primitives, ownership types, and validation. EggPool
adapts `Config`/catalog/database facts into its portable types and keeps
server key resolution, endpoint choice, CLI delivery, HTTP serving, and
runtime paths in `rust/src/operations/integrations.rs`. The narrow
`eggpool-connect` desktop binary in `rust/crates/eggpool-connect/` links the
same crate to implement `plan`/`install`/`verify`/`backups`/`restore`/`remove`
with an explicit transaction state machine, byte-exact backups, atomic writes,
and automatic rollback; it owns only receiving-machine detection, credential
prompting, profile fetching, and local mutation, with no Axum, SQLite,
Eggress, proxy, agent, or daemon functionality.

The CLI adapter in `rust/src/runtime.rs` owns dispatch, prompts, presentation,
and stable exit-code mapping. Reusable local process workflows are composed by
`rust/src/operations/lifecycle.rs` over the primitive safety services in
`process.rs`, `paths.rs`, and `control.rs`.

## Request lifecycle

`rust/src/coordinator/` owns endpoint detection, bounded request preparation,
model/account routing, durable request and attempt state, provider dispatch,
response adaptation, retry classification, and terminal finalization.
The production endpoint boundary parses and depth-checks each bounded body
once, classifies finite versus streaming from that parsed value, and mutates
the same tree for provider-qualified or virtual model rewrites. Direct native
no-rewrite dispatch retains the ingress `Bytes` allocation. Borrowed attempt
preparation ends at the synchronous wire/header boundary; the prepared
provider attempt is fully owned before any network await.
Streaming is decomposed under `rust/src/coordinator/streaming/`: `coordinator.rs`
owns pre-handoff selection, dispatch, timeout, and retry decisions;
`execution.rs` owns the single post-handoff body/cancellation owner;
`terminal.rs` owns terminal classification and finalization data helpers;
`timeout.rs`, `types.rs`, and `diagnostics.rs` hold the pure policy, contract,
and bounded-observation pieces. The `mod.rs` facade preserves the public
`coordinator::streaming` imports. Canonical wire intent is captured before
provider adaptation in `rust/src/wire/ir.rs`; all alternate targets encode
from that source rather than chaining translated payloads. Native stream
adapters require provider terminal evidence and never synthesize a terminal
event from transport EOF. Responses same-surface streams use an incremental
observer/fold sink plus raw-byte forwarding so unknown valid events survive
unchanged without constructing an unused canonical-event batch;
cross-surface Responses streams use bounded per-stream encoder state for
message, reasoning, and function-call item completion.

Remote compaction keeps the public `FiniteRequest` compatibility shape, but the
production endpoint hands `CompactAdmittedRequest` directly to a private finite
execution input. This prevents a second source-native JSON tree while sharing
the ordinary finite retry, publication, wire, and finalization loop.

## Subsystem ownership

| Subsystem | Current implementation |
|---|---|
| CLI, configuration, errors | `rust/src/cli.rs`, `rust/src/config.rs`, `rust/src/config_reload_policy.rs`, `rust/src/error.rs` |
| Request and coordinator | `rust/src/request/`, `rust/src/coordinator/` |
| Semantic model routing | `rust/crates/eggpool-model-routing/`, `rust/src/model_router.rs` |
| Portable client config | `rust/crates/eggpool-client-config/`, `rust/src/operations/integrations.rs` (EggPool adapter), `rust/crates/eggpool-connect/` (transactional desktop helper) |
| Provider/account routing, quota, health | `rust/src/routing/`, `rust/src/quota/`, `rust/src/health/` |
| Providers and wire surfaces | `rust/src/providers/`, `rust/src/wire/` (immutable provider/account topology; dispatch-oriented wire preparation) |
| SQLite and migrations | `rust/src/db/`, `rust/assets/db/migrations/` |
| Runtime and reload | `rust/src/runtime_lifecycle/`, `rust/src/reload.rs` |
| HTTP server and control-plane adapters | `rust/src/server/mod.rs`, `rust/src/server/{middleware,health,inference,dashboard}.rs` (`health.rs` also serves the authenticated compact `GET /api/status` snapshot and the authenticated versioned `GET /api/integrations/v1/profile`, and shares readiness evaluation with `readyz`) |
| Operations and local lifecycle | `rust/src/operations/`, especially `config_mutation.rs`, `lifecycle.rs`, `process.rs`, `paths.rs`, `control.rs`, and `status.rs` (compact proxy/provider health aggregation; `runtime.rs` only renders) |

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
by `rust/src/config_reload_policy.rs`. Its pure `classify_transition` contract
returns one redacted typed result for unchanged, live-reloadable, and
restart-required candidates. Operator mutations carry that result into apply
logic, while `rust/src/reload.rs` revalidates and reclassifies on the server
before generation construction. Unsupported or disruptive changes fail closed
and require restart.

The repository-root `config.example.toml` and `config.sbc.example.toml` are
the canonical human-edited examples. `rust/build.rs` tracks them as inputs and
embeds the default example used by `eggpool init-config`; there is no second
Rust-local copy to keep synchronized. The `[server].threads` key remains a
restart-required compatibility/diagnostic field and does not select Tokio
workers.

The current release is a native Rust wheel with embedded runtime assets. The
supported release target classes are Linux x86_64, Linux aarch64, and macOS
arm64. Historical Python wheels remain immutable external artifacts and are
available only for explicit catalogued exact-version compatibility transitions.

## Source development

Run current runtime checks from the repository root:

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked

# Reduced feature-boundary qualification
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --no-default-features
```

Native dependency and feature changes are reviewed from Cargo's resolved
authority, not from a hand-maintained inventory:

```bash
cargo deny --manifest-path rust/Cargo.toml check
cargo tree --manifest-path rust/Cargo.toml -e features
cargo tree --manifest-path rust/Cargo.toml --duplicates
```

The root `deny.toml` checks RustSec advisories, the reviewed third-party
license allowlist, registry/git sources, and duplicate-version warnings across
the declared feature and contributor/build graph. It is a low-noise policy
gate, not a substitute for strict Clippy, serial tests, release builds, or
owner-specific Eggfetch/Eggress/Hyper/Rustls/SQLite qualification. The dedicated
dependency workflow runs on Cargo/policy changes, weekly, and by manual
dispatch; ordinary source-only CI remains network-light.

The 2026 residual-efficiency closure (Plans 230–234) keeps the single SQLite
gate, current-thread runtime, routing selection lock, and streaming handoff.
Plan 235 adds only an opt-in, tooling-owned physical-SBC characterization to
the existing qualification runner; Plan 236 corrects that pass tooling-only
(benchmark-only low-wear fixture, downstream `response.completed` plus Messages-path
proof for translated streams, `runtime-q008.v1` default vs `runtime-q008.v2`
benchmark contracts). Plan 237 adds diagnostic-only finite-tail phase timing
plus a direct-provider control and localizes the remaining finite tail to the
pre-provider durable publication / SQLite / storage path (slowest request
pre-provider dominated in all three Pi 5 runs with a stable direct control;
one tmpfs run removes the tail). None adds runtime metrics, a
benchmark dependency, hardware CI, or a performance threshold. Release
qualification retains Maturin `--strip false`; a stripped or ThinLTO experiment
must pass target qualification before changing the shipped profile.

Plan 238 extends the tooling-only path with a separate publication/storage
diagnostic. It waits for the fixed database-task quiescence window, samples
only bounded database/WAL/SHM file scalars and one WAL header per measured
request, and optionally moves only those SQLite files to a temporary
filesystem. It does not alter SQLite durability, production storage, or any
runtime ownership boundary.

Use the Python tooling environment only for release validators and tooling
tests. Do not import, run, or recreate the retired application source tree.
