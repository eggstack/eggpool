[![PyPI version](https://badge.fury.io/py/eggpool.svg)](https://pypi.org/project/eggpool/)
[![Crates.io version](https://img.shields.io/crates/v/eggpool.svg)](https://crates.io/crates/eggpool)
[![Rust runtime](https://img.shields.io/badge/runtime-Rust-orange.svg)](https://www.rust-lang.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![CI](https://github.com/eggstack/eggpool/actions/workflows/ci.yml/badge.svg)](https://github.com/eggstack/eggpool/actions/workflows/ci.yml)
[![PyPI Downloads](https://static.pepy.tech/personalized-badge/eggpool?period=total&units=INTERNATIONAL_SYSTEM&left_color=BLACK&right_color=GREEN&left_text=downloads)](https://pepy.tech/projects/eggpool)
[![Crates.io Downloads](https://img.shields.io/crates/d/eggpool.svg)](https://crates.io/crates/eggpool)

# EggPool

A lightweight, LAN-hosted proxy that aggregates multiple AI provider accounts behind OpenAI Chat Completions, OpenAI Responses, and Anthropic Messages-compatible paths.

## Features

- Client endpoints for OpenAI Chat Completions (`/v1/chat/completions`), stateless OpenAI Responses (`/v1/responses`), and Anthropic Messages (`/v1/messages`), plus a bounded native-only remote-compaction operation (`/v1/responses/compact`)
- Transparent bidirectional protocol transcoding between OpenAI and Anthropic, plus native Gemini wire codecs
- Load-based routing across multiple providers and accounts with dynamic model discovery, quota awareness, and health gating
- Optional sticky model-router aliases for virtual-model selection
- Request, token, latency, error, and cost tracking in SQLite, with a multi-page dashboard
- Model metadata enrichment from provider catalogs, OpenRouter, Artificial Analysis, and Hugging Face, including thinking/reasoning capability metadata
- Per-account outbound proxy support, including SSH proxies
- Live config reload (`eggpool rehash`) for provider/routing changes; host, port, and database changes need `eggpool restart`
- Designed for lightweight deployments (Raspberry Pi, SBCs)

## Quick Start

```bash
# Install and activate in this Bash/zsh session (verified binary; no Python required)
bash -o pipefail -c 'curl -fsSL https://raw.githubusercontent.com/eggstack/eggpool/main/scripts/install.sh | bash' && export PATH="$HOME/.local/bin:$PATH"

# Or via your package manager: `uv tool install eggpool`, `pipx install eggpool`,
# or `cargo install eggpool` (Rust 1.89+). Targets: Linux x86_64, Linux aarch64, macOS arm64.
# Interactive onboarding — connect providers, validate, start
eggpool onboard

# Install as a systemd service
sudo env "PATH=$PATH" "$(command -v eggpool)" deploy systemd --install
```

See [Deployment](docs/deployment.md) for systemd, cron, and production setup,
and [standalone binaries](docs/rust-release-deployment.md) for install internals.

The curl install is binary-first: it fetches the qualified GitHub raw
executable for your target (SHA-256 verified before execution) and commits it
to `~/.local/bin/eggpool` as a `standalone-rust` installation that uses
native `eggpool update` thereafter — it never invokes uv, pipx, pip, Python,
Cargo, or a source build. Windows and other unqualified targets are
unsupported. The command above adds the directory to this invoking shell
after a successful install; running the bare `curl ... | bash` form persists
future-shell PATH configuration but cannot change its parent shell. To use a
custom destination, set `EGGPOOL_INSTALL_BIN_DIR` inside the installer Bash
process and export that same path in the invoking shell, for example:

```bash
bash -o pipefail -c 'export EGGPOOL_INSTALL_BIN_DIR="$HOME/opt/eggpool/bin"; curl -fsSL https://raw.githubusercontent.com/eggstack/eggpool/main/scripts/install.sh | bash' && export PATH="$HOME/opt/eggpool/bin:$PATH"
```

To update or switch to one exact catalogued release and to roll back, see
[Upgrading](docs/upgrading.md):

```bash
eggpool update         # latest catalogued release, same owner
eggpool update 0.8.0   # exact historical-version switch
```

`eggpool install-provenance` shows the package manager or standalone update authority.

## First Request

`serve` runs as a daemon (`--verbose` stays in the foreground). List your models, then call one:

```bash
eggpool serve
eggpool status

export EGGPOOL_API_KEY="$(eggpool getkey)"

# Liveness / readiness (no key needed)
curl http://127.0.0.1:11300/v1/healthz
# {"status":"ok"}

# Model ids available to your accounts
curl -H "Authorization: Bearer $EGGPOOL_API_KEY" \
  http://127.0.0.1:11300/v1/models

# Chat completion (use any model id from /v1/models)
curl -H "Authorization: Bearer $EGGPOOL_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"Hello!"}]}' \
  http://127.0.0.1:11300/v1/chat/completions
```

Inference (`/v1/*` except `/v1/healthz` and `/v1/readyz`), integration
(`/api/integrations/*`), and status endpoints (`/api/status`,
`/api/stats/runtime`, `/api/stats/update`) require the server key as
`Authorization: Bearer <key>` (OpenAI-style) or `x-api-key: <key>`
(Anthropic-style); without it they return `401`. To pin a provider, qualify
the model as `model/provider` (e.g. `"gpt-4o/openai"`); `eggpool accounts
explain --model <id>` shows per-account eligibility. Full endpoint list:
[API reference](docs/api-reference.md).

## Coding Agents

```bash
eggpool configsetup opencode --apply  # managed OpenCode install with drift detection
eggpool configsetup codex --model <eggpool-model-or-alias>
eggpool configsetup claude-code
eggpool configremote codex            # secret-free remote profile for other machines
```

Generated configs reference `EGGPOOL_API_KEY` without embedding the key (`export EGGPOOL_API_KEY="$(eggpool getkey)"`). See [Agent Configuration](docs/agent-configuration.md) for all targets, the managed `--apply`/`--sync`/`--check`/`--remove` lifecycle, remote profiles, and the transactional `eggpool-connect` desktop helper.

## Dashboard & LAN Access

By default EggPool binds `0.0.0.0:11300` and serves a public read-only dashboard at `http://<lan-ip>:11300/`. To share with desktops on your LAN, set `[integrations].advertise_base_url = "http://<lan-ip>:11300/v1"`, run `eggpool rehash` (advertised URL is live-reloadable profile output; it never changes the listen socket), then export a profile with `eggpool configremote codex`. Lock it down with `[server].host = "127.0.0.1"` (local only) or `eggpool dashboard public --off` (key required on the dashboard too). See [Firewall](docs/firewall.md).

## CLI Essentials

| Command | Description |
|---------|-------------|
| `eggpool onboard` | Interactive onboarding wizard |
| `eggpool connect` / `eggpool connect list` | Add a provider account / list supported providers |
| `eggpool check-config` | Validate configuration |
| `eggpool serve` / `stop` / `restart` / `rehash` | Run, stop, restart, or live-reload the server |
| `eggpool status` | Concise proxy/provider health summary (`--json` supported) |
| `eggpool models refresh` | Refresh the model catalog |
| `eggpool accounts status` | Show account status (provider, priority, weight, enabled) |
| `eggpool backup` / `recover` | Create / restore timestamped backups |
| `eggpool update [VERSION]` | Install the latest or one exact catalogued release |

Full command list: [CLI reference](docs/cli-reference.md).

## Configuration

One TOML file (`--config` > `$EGGPOOL_CONFIG` > `~/.config/eggpool/config.toml` > `./config.toml`). Provider/upstream API keys come from environment variables or `.env`, never the file; the EggPool server key itself lives under `[server]` (`api_key`, or `api_key_env` to own it via environment — `eggpool newkey` rotates it). Prefer `eggpool connect` over hand-editing; the commented [config.example.toml](rust/config.example.toml) (plus [config.sbc.example.toml](rust/config.sbc.example.toml) for SBCs) documents every section. Config changes are validated before apply and classified as live-reloadable (`rehash`) or restart-required — see [Live Configuration Rehash](docs/live-config-rehash.md) and [Providers](docs/providers.md).

## Documentation

| Guide | Link |
|-------|------|
| Deployment (systemd, cron, production) | [docs/deployment.md](docs/deployment.md) |
| Provider catalog & configuration | [docs/providers.md](docs/providers.md) |
| Agent configuration | [docs/agent-configuration.md](docs/agent-configuration.md) |
| API endpoints | [docs/api-reference.md](docs/api-reference.md) |
| Stateless Responses | [docs/stateless-responses.md](docs/stateless-responses.md) |
| Protocol transcoding | [docs/transcoding.md](docs/transcoding.md) |
| Semantic model routing | [docs/model-routing.md](docs/model-routing.md) |
| Backup & restore | [docs/backup-restore.md](docs/backup-restore.md) |
| Upgrading & rollback | [docs/upgrading.md](docs/upgrading.md) |
| Per-account outbound proxy | [docs/proxy.md](docs/proxy.md) |
| Thinking & reasoning | [docs/thinking.md](docs/thinking.md) |
| Raspberry Pi setup | [docs/raspberry-pi.md](docs/raspberry-pi.md) |
| Live Configuration Rehash | [docs/live-config-rehash.md](docs/live-config-rehash.md) |
| Filesystem layout | [docs/filesystem-layout.md](docs/filesystem-layout.md) |

More guides: [configuration reference](docs/configuration.md), [model context limits](docs/model-limits.md), [firewall](docs/firewall.md), [performance profiles](docs/deployment.md#performance-profiles), [network diagnostics](docs/network-diagnostics.md), [OpenCode stream stability](docs/opencode-stream-stability.md), [model-info OpenRouter debugging](docs/model-info-openrouter-debug.md), [live wire-surface verification](docs/live-wire-e2e.md), [Codex compatibility smoke](docs/codex-compatibility-smoke.md), [standalone binaries](docs/rust-release-deployment.md), [release procedure](docs/releasing.md), [dashboard qualification](docs/rust-dashboard-qualification.md), [migration history](docs/migration-history.md), [database recovery](docs/runbooks/database-recovery.md), [architecture overview](architecture/README.md).

## Development

```bash
# CI parity (mirrors .github/workflows/ci.yml `check` job)
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
# (CI runs no-default only for `check`/`clippy`, never `test`)
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings

# Local-only extras (not CI steps): locked build, no-default test,
# dev tooling env (`uv sync --frozen` for CI parity)
cargo build --manifest-path rust/Cargo.toml --locked
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
uv sync --dev

uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
```

See `AGENTS.md` for focused test targets, subsystem guidance, and the optional physical-SBC qualification pass.

## License

MIT
