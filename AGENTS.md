# AGENTS.md

## Skills

Load the matching skill before task work (in `.opencode/skills/`):

- `architecture` — runtime ownership, boundaries, request/wire invariants
- `development` — full lint/test matrix, focused targets per subsystem
- `deployment` — release artifact, systemd, installer ownership
- `documentation` — doc map, accuracy rules, what not to duplicate here

For runtime behavior changes, start at `architecture/README.md` + the relevant
deep dive. Do not copy deep-dive detail into `AGENTS.md`.

## Layout

- Runtime (authority): `rust/src/` (`main.rs`/`cli.rs` entry, `runtime.rs` CLI adapter, `server/` thin HTTP adapters, `coordinator/` request lifecycle, `wire/` protocol codecs, `runtime_lifecycle/` generations, `operations/` local lifecycle, `db/` + `rust/assets/db/migrations/`).
- Reusable policy crate: `rust/crates/eggpool-model-routing/` (neutral validation/compilation only; selector execution and affinity cache stay in `rust/src/`).
- Tooling only (never a runtime fallback): repo-root `pyproject.toml`, `scripts/`, `tests/tooling/`. Native runtime tests live in `rust/tests/`.
- Config examples: `config.example.toml`, `config.sbc.example.toml`. Config resolution: `--config` > `$EGGPOOL_CONFIG` > `~/.config/eggpool/config.toml` > `./config.toml`; API keys from environment/`.env`, never committed.

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

# Focused: single target / single test
cargo test --manifest-path rust/Cargo.toml --test <target> -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets <test_name> -- --test-threads=1

# Dependency/feature changes only
cargo deny --manifest-path rust/Cargo.toml check
cargo build --manifest-path rust/Cargo.toml --locked --release
cargo tree --manifest-path rust/Cargo.toml -e features
cargo tree --manifest-path rust/Cargo.toml --duplicates
```

Notes: Rust tests must run serial (`--test-threads=1`). `uv sync --dev` for local
tooling work, `uv sync --frozen` for CI parity. Ruff covers `scripts/` +
`tests/tooling/`; pyright strict covers `scripts/` only. CI skips docs-only
changes (`docs/`, `architecture/`, `plans/`, `.opencode/skills/`, `CHANGELOG.md`, `AGENTS.md`).

## Conventions agents miss

- `rust/src/config_reload_policy.rs::classify_transition` is the only
  reload-vs-restart authority. Mutation paths classify before atomic replace;
  server `rehash` reclassifies before publication. Mixed changes are wholly
  restart-required; diagnostics stay secret-free.
- `rust/src/error.rs` owns HTTP/status mappings — read it before adding variants,
  keep context explicit, retain causes.
- `runtime.rs` adapts CLI to operations; reusable lifecycle lives in
  `operations/lifecycle.rs`. Keep `server/*` thin (no coordinator retries/finalization).
- No Python runtime fallbacks; no new restart/reload key lists; no buffering
  arbitrary native streams. Credentials, prompts, raw bodies, cache keys stay
  out of persistence/logs/diagnostics.
- `--no-default-features` must still compile/test; it keeps direct/non-SSH
  proxy paths and rejects SSH proxy config as `TransportError::ProxyConfiguration`
  before dialing (`eggress-ssh-fallback` is default-only compat).
- `deny.toml` + `cargo deny` is the license/advisory/source policy; `Cargo.toml`/`Cargo.lock`
  changes also need the locked release build + serial suite above.
- Branch `main`, imperative commits. Never commit secrets, API keys, or `.env`.
