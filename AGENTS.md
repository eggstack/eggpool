# AGENTS.md

## Skills

Load the matching skill before task work (in `.opencode/skills/`):

- `architecture` — runtime ownership, boundaries, request/wire invariants
- `development` — full lint/test matrix, focused targets per subsystem
- `deployment` — release artifact, systemd, installer ownership
- `documentation` — doc map, accuracy rules, what not to duplicate here
- `plan` — `plans/` lifecycle, numbering, closure passes

For runtime behavior changes, start at `architecture/README.md` + the
`architecture/overview.md` review index + the relevant deep dive. Do not copy
deep-dive detail into `AGENTS.md`.

Change-area index (all under `architecture/`; full module map is the
`overview.md` review index): routing/selection → `deep-dive-routing.md`;
providers/transport → `deep-dive-providers.md`; reload/restart →
`deep-dive-control.md` + `deep-dive-runtime.md`; deploy/backup/update →
`deep-dive-deployment.md` + `deep-dive-lifecycle.md`; wire/transcoding →
`deep-dive-transcoder.md`; integrations/connect → `deep-dive-integrations.md`.

## Layout

- Runtime (authority): `rust/src/` (`main.rs`/`cli.rs`/`lib.rs` entry, `runtime.rs` CLI adapter, `server/` thin HTTP adapters, `coordinator/` + `coordinator/streaming/` request lifecycle, `request/` admission, `wire/` protocol codecs, `routing/` + `accounts/` + `catalog/` + `quota/` + `health/` selection, `model_router.rs` affinity, `providers/` transport, `runtime_lifecycle/` generations + `reload.rs` + `task_supervisor.rs`, `operations/` local lifecycle, `db/` + `rust/assets/db/migrations/` v1–v54 immutable).
- Reusable policy crates: `rust/crates/eggpool-model-routing/` (neutral validation/compilation only; selector execution and affinity cache stay in `rust/src/`), `rust/crates/eggpool-client-config/` (portable Codex/OpenCode projection, profiles, `epc1` tokens, V1/V2 renderers, TOML/JSONC-preserving mutation; EggPool `Config`/catalog/DB/key/endpoint/CLI/file IO stays in `rust/src/operations/integrations.rs`), `rust/crates/eggpool-wire/` (neutral sans-I/O wire kernel; execution stays in `rust/src/`), `rust/crates/eggpool-connect/` (narrow `eggpool-connect` binary: plan/install/verify/backups/restore/remove with byte-exact backups, atomic writes, automatic rollback; no Axum/SQLite/Eggress, no proxy/agent/daemon).
- Tooling only (never a runtime fallback): repo-root `pyproject.toml`, `scripts/`, `tests/tooling/`. `scripts/qualification_sbc.py` is the sole physical-SBC runner (loopback-only, aggregate-only, non-CI). Native tests live in `rust/tests/` (serial; there is no `coordinator_c012`; streaming files are `coordinator.rs`, `execution.rs`, `terminal.rs`, `timeout.rs`, `types.rs`, `diagnostics.rs`).
- Config resolution: `--config` > `$EGGPOOL_CONFIG` > `~/.config/eggpool/config.toml` > `./config.toml`; API keys from environment/`.env`, never committed. Examples: `rust/config.example.toml`, `rust/config.sbc.example.toml`.
- Plans: `plans/` is append-only (`plans/README.md`, `plans/registry.md` is the control surface; pre-251 flat `001-*`…`250-*` are immutable history). See the `plan` skill before adding one. Past plan numbers in this file go stale — link the registry, never paste plan history here.

## Commands (run from repo root)

```bash
# Fast loop
cargo fmt --manifest-path rust/Cargo.toml --all
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1

# Before push (mirrors CI `check` job) + no-default guard
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
uv sync --frozen
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run pytest tests/tooling/ -q --tb=short --maxfail=1

# Focused: single target / single test (see development skill for subsystem index)
cargo test --manifest-path rust/Cargo.toml --test <target> -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets <test_name> -- --test-threads=1

# Dependency/feature changes only
cargo deny --manifest-path rust/Cargo.toml check
cargo build --manifest-path rust/Cargo.toml --locked --release
cargo tree --manifest-path rust/Cargo.toml -e features
cargo tree --manifest-path rust/Cargo.toml --duplicates
```

Notes: Rust tests must run serial (`--test-threads=1`). CI runs no-default
only for `check`/`clippy`, never `test` — keep the full
`--workspace --all-targets -- --test-threads=1` shape when running no-default
tests locally. `uv sync --dev` for local
tooling work, `uv sync --frozen` for CI parity. Ruff covers `scripts/` +
`tests/tooling/`; pyright strict covers `scripts/` only. CI skips docs-only
changes (`docs/`, `architecture/`, `plans/`, `.opencode/skills/`, `CHANGELOG.md`, `AGENTS.md`).

## Conventions agents miss

- `rust/src/config_reload_policy.rs::classify_transition` is the only
  reload-vs-restart authority. Mutation paths classify before atomic replace;
  server `rehash` reclassifies before publication. Mixed changes are wholly
  restart-required; diagnostics stay secret-free. `[integrations].advertise_base_url`
  is live-reloadable profile output only and never changes the listen socket;
  do not overload `[server].host` for client advertisement.
- `rust/src/error.rs` owns process exit codes only (`AppError`/`BootstrapError`).
  HTTP/status mappings live with their owners: `coordinator/endpoints.rs`
  (`EndpointError::status`), `coordinator/finite.rs` (provider-failure
  statuses), `server/middleware.rs` (generation errors) — read the owning
  module before adding variants, keep context explicit, retain causes.
- `runtime.rs` adapts CLI to operations; reusable lifecycle lives in
  `operations/lifecycle.rs`, compact proxy/provider health aggregation in
  `operations/status.rs` (shared readiness with `readyz`, no outbound probes,
  secret-free). Keep `server/*` thin (no coordinator retries/finalization).
- No Python runtime fallbacks; no new restart/reload key lists; no buffering
  arbitrary native streams. Credentials, prompts, raw bodies, cache keys stay
  out of persistence/logs/diagnostics.
- Inference admission is owned by `coordinator/endpoints.rs`: one endpoint
  execution call, one parsed body reused for finite/streaming selection, depth
  validation, model mutation, and `from_admitted` construction. Native
  no-rewrite dispatch retains the ingress `Bytes` backing allocation.
- Attempt preparation may borrow generation/request data only synchronously;
  `PreparedUpstreamAttempt` is fully owned before `submit_once` is awaited.
  `ProviderClientPool` publishes an immutable nested provider/account topology
  and closes it atomically; do not reintroduce per-request topology mutexes or
  allocated tuple lookup keys.
- Performance boundaries are evidence-gated: keep the single SQLite gate,
  streaming mpsc bridge, Tokio `current_thread` runtime, and routing selection
  lock unless comparable loopback measurements justify a narrowly scoped
  change. Public `FiniteRequest` / `CompactAdmittedRequest` shapes are
  compatibility surfaces. Native Responses observation shares the canonical SSE
  decoder and folds bounded terminal/usage facts without buffering streams.
- `--no-default-features` must still compile/test; it keeps direct/non-SSH
  proxy paths and rejects SSH proxy config as `TransportError::ProxyConfiguration`
  before dialing. Default SSH is the root `ssh` capability forwarded to
  Eggress 1.0.11 (`eggress-outbound/ssh` plus the compat crate's SSH
  translation support); there is no Eggpool SSH executor fallback.
- Cancellation-path tests must synchronize on an observable fixture boundary or
  invariant under a bounded timeout. Do not use fixed millisecond sleeps or
  yield-count loops to guess worker/proxy/pool-waiter state.
- `rust/src/wire/adapters.rs` is EggPool-owned seam code, explicitly not part of
  the extractable `eggpool-wire` kernel; kernel modules must never import
  runtime state. `wire_extraction_contract` + `wire_kernel_boundary` guard the
  seam — run both for any `rust/src/wire/` change.
- `deny.toml` + `cargo deny` is the license/advisory/source policy; `Cargo.toml`/`Cargo.lock`
  changes also need the locked release build + serial suite above.
- Provider transport is exact-pinned to `eggfetch-core =0.2.1` with
  `native-http1,tls-rustls` (not the `http1` alias or `standard-http1`),
  excluding high-level URL/retry/redirect/Basic-auth, built-in proxy, and
  HTTP/2/3; provider proxy dialing is exact-pinned Eggress `1.0.11`
  `eggress-outbound` via `connect_tcp_detailed` with a typed kind/stage
  adapter (no message-string classifier). `operations/update.rs` is a separate
  Hyper/Rustls owner. Downstream HTTP/1 is exact-pinned `eggserve-server =0.4.0`
  with `tower` (server-owned `TowerToEggserve` into the existing Axum router).
- Branch `main`, imperative commits. Never commit secrets, API keys, or `.env`.
