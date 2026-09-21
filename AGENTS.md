# AGENTS.md

## Skills

Load the matching skill before task work (in `.opencode/skills/`):

- `architecture` — runtime ownership, boundaries, request/wire invariants
- `development` — full lint/test matrix, focused targets per subsystem
- `deployment` — release artifact, systemd, installer ownership
- `documentation` — doc map, accuracy rules, what not to duplicate here
- `plan` — `plans/` append-only lifecycle, numbering, closure passes

For runtime behavior changes, start at `architecture/README.md` + the
`architecture/overview.md` review index + the relevant deep dive. Do not copy
deep-dive detail into `AGENTS.md`.

## Layout

- Runtime (authority): `rust/src/` (`main.rs`/`cli.rs`/`lib.rs` entry, `runtime.rs` CLI adapter, `server/` thin HTTP adapters, `coordinator/` + `coordinator/streaming/` request lifecycle, `request/` admission, `wire/` protocol codecs, `routing/` + `accounts/` + `catalog/` + `quota/` + `health/` selection, `model_router.rs` affinity, `providers/` transport, `runtime_lifecycle/` generations + `reload.rs` + `task_supervisor.rs`, `operations/` local lifecycle, `db/` + `rust/assets/db/migrations/` v1–v54).
- Reusable policy crates: `rust/crates/eggpool-model-routing/` (`policy.rs`, `identity.rs`; neutral validation/compilation only; selector execution and affinity cache stay in `rust/src/`) and `rust/crates/eggpool-client-config/` (portable Codex/OpenCode projection, profiles, `epc1` tokens, V1/V2 renderers, TOML/JSONC-preserving mutation, variant selection, ownership captures; EggPool `Config`/catalog/DB/key/endpoint/CLI/file IO stays in `rust/src/operations/integrations.rs`, including read-only `configremote` export and authenticated `GET /api/integrations/v1/profile`). Desktop helper: `rust/crates/eggpool-connect/` (narrow `eggpool-connect` binary over the portable crate: plan/install/verify/backups/restore/remove with byte-exact backups, atomic writes, and automatic rollback; no Axum/SQLite/Eggress, no proxy/agent/daemon). Reviewed bootstraps: `packaging/connect/eggpool-connect.sh` + `eggpool-connect.ps1` (version-pinned download, SHA-256 against release SHA256SUMS, no mutation logic). Helper release tooling: `scripts/build_connect_artifacts.py` + `scripts/inspect_connect_artifact.py`, manifest `connect_artifacts` section (proxy `artifacts` stays exactly three). A Windows helper never implies Windows proxy support.
- Tooling only (never a runtime fallback): repo-root `pyproject.toml`, `scripts/`, `tests/tooling/`. `scripts/qualification_sbc.py` is the sole physical-SBC qualification/characterization runner; its optional `--benchmark-samples 1..=100` mode remains loopback-only, aggregate-only, and non-CI. Native runtime tests live in `rust/tests/` (note: `coordinator_c012` does not exist; streaming files are `coordinator.rs`, `execution.rs`, `terminal.rs`, `timeout.rs`, `types.rs`, `diagnostics.rs`).
- Config examples: `config.example.toml`, `config.sbc.example.toml`. Config resolution: `--config` > `$EGGPOOL_CONFIG` > `~/.config/eggpool/config.toml` > `./config.toml`; API keys from environment/`.env`, never committed.
- Plans: `plans/` is append-only history (~200 files, mostly closed). See the `plan` skill before adding one. `.agents/` holds no custom agent definitions.

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

Plan 235's physical-SBC evidence is manual and descriptive: qualify a
SHA-verified Linux/aarch64 release candidate, then repeat
`qualification_sbc.py --benchmark-samples 30` three times from fresh roots.
Plan 236 corrects that pass tooling/evidence-only: benchmark runs must use
`tests/tooling/fixtures/qualification/sbc-benchmark.toml` (low-wear
steady-state cadence), translated streams require downstream
`response.completed` plus fixture Messages-path proof, default mode stays on
`runtime-q008.v1` while benchmark mode uses `runtime-q008.v2`, and Plan 236's
corrected timing supersedes Plan 235's timing/translated interpretation.
Plan 237 adds diagnostic-only `--diagnose-finite-tail 10..=200` (requires
benchmark mode; sequential native-finite phase timing plus a direct-provider
control, scalar-only, no p99): on Pi 5 the slowest request in all three
60-sample runs was pre-provider dominated with a stable direct control, and a
single tmpfs run removed the tail entirely (Outcome 1: durable publication /
SQLite / storage path; narrow follow-up only, no Tokio/routing/streaming
change). Hosted ARM, emulation, and cloud ARM results are not target-class evidence;
record unavailable dimensions as `not measured`.

Notes: Rust tests must run serial (`--test-threads=1`). `uv sync --dev` for local
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
- `rust/src/error.rs` owns HTTP/status mappings — read it before adding variants,
  keep context explicit, retain causes.
- `runtime.rs` adapts CLI to operations; reusable lifecycle lives in
  `operations/lifecycle.rs`, compact proxy/provider health aggregation in
  `operations/status.rs` (shared readiness with `readyz`, no outbound probes,
  secret-free). Keep `server/*` thin (no coordinator retries/finalization).
- No Python runtime fallbacks; no new restart/reload key lists; no buffering
  arbitrary native streams. Credentials, prompts, raw bodies, cache keys stay
  out of persistence/logs/diagnostics.
- Inference admission is owned by `coordinator/endpoints.rs`: the production
  server makes one endpoint execution call, and finite/streaming selection,
  depth validation, virtual/provider-qualified model mutation, and final
  `from_admitted` construction reuse one parsed body. Native no-rewrite
  dispatch retains the ingress `Bytes` backing allocation.
- Attempt preparation may borrow generation/request data only synchronously;
  `PreparedUpstreamAttempt` is fully owned before `submit_once` is awaited.
  `ProviderClientPool` publishes an immutable nested provider/account topology
  and closes it atomically; do not reintroduce per-request topology mutexes or
  allocated tuple lookup keys.
- The residual performance campaign in Plans 230–234 is evidence-gated. Keep the
  single SQLite gate, streaming mpsc bridge, Tokio `current_thread` runtime,
  and routing selection lock unless comparable loopback measurements justify a
  narrowly scoped change. Compact production execution may use its private
  single-owner admission representation, but public `FiniteRequest` and
  `CompactAdmittedRequest` shapes remain compatibility surfaces. Native
  Responses observation must share the canonical SSE decoder and fold bounded
  terminal/usage facts without buffering arbitrary native streams.
- `--no-default-features` must still compile/test; it keeps direct/non-SSH
  proxy paths and rejects SSH proxy config as `TransportError::ProxyConfiguration`
  before dialing. Default SSH is the root `ssh` capability forwarded to
  Eggress 1.0.7; there is no Eggpool SSH executor fallback.
- Cancellation-path tests must synchronize on an observable fixture boundary or
  invariant under a bounded timeout. Do not use fixed millisecond sleeps or
  yield-count loops to guess that a detached worker, proxy handshake, or pool
  waiter has reached a state; the provider recovery request is itself the
  release condition when it is the first direct observable.
- `deny.toml` + `cargo deny` is the license/advisory/source policy; `Cargo.toml`/`Cargo.lock`
  changes also need the locked release build + serial suite above.
- Provider transport is exact-pinned to `eggfetch-core =0.1.7` with
  `native-http1,tls-rustls`; do not substitute Eggfetch's `http1` alias or
  `standard-http1`, and keep the high-level URL/retry/redirect/Basic-auth,
  built-in proxy, and HTTP/2/3 features disabled. `operations/update.rs` is a
  separate Hyper/Rustls owner.
- Branch `main`, imperative commits. Never commit secrets, API keys, or `.env`.
