[![PyPI version](https://badge.fury.io/py/eggpool.svg)](https://pypi.org/project/eggpool/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![CI](https://github.com/eggstack/eggpool/actions/workflows/ci.yml/badge.svg)](https://github.com/eggstack/eggpool/actions/workflows/ci.yml)
[![PyPI Downloads](https://static.pepy.tech/personalized-badge/eggpool?period=total&units=INTERNATIONAL_SYSTEM&left_color=BLACK&right_color=GREEN&left_text=downloads)](https://pepy.tech/projects/eggpool)

# EggPool

A lightweight, LAN-hosted proxy that aggregates multiple AI provider accounts behind OpenAI Chat Completions- and Anthropic Messages-compatible paths.

## Features

- Single endpoint for OpenAI Chat Completions (`/v1/chat/completions`), Anthropic Messages (`/v1/messages`), and stateless Responses passthrough (`/v1/responses`)
- Transparent bidirectional protocol transcoding between OpenAI and Anthropic
- Canonical request/reasoning/response-event boundary for safe cross-surface translation
- Dynamic model discovery with load-based routing across multiple providers and accounts
- Provider/model wire-surface contracts with per-surface paths and auth shapes
- Request, token, latency, error, and cost tracking in SQLite
- Multi-page dashboard with 50 themes
- Model metadata enrichment from provider catalogs, OpenRouter, Artificial Analysis, and Hugging Face
- Thinking/reasoning capability-aware routing with configurable budget mapping
- Per-account outbound proxy support ([pproxy](https://pypi.org/project/pproxy/) — install with `uv sync --extra proxy`)
- Optional `orjson` backend for faster JSON handling (`uv sync --extra fast`)
- Designed for lightweight deployments (Raspberry Pi, SBCs)

For full details on features, architecture, and design decisions, see [architecture/README.md](architecture/README.md).

## Quick Start

```bash
# Install (one-shot)
curl -fsSL https://raw.githubusercontent.com/eggstack/eggpool/main/scripts/install.sh | bash

# Interactive onboarding — connect providers, validate, start
eggpool onboard

# Install as a systemd service
sudo env "PATH=$PATH" "$(command -v eggpool)" deploy systemd --install
```

See [Deployment](docs/deployment.md) for alternative install methods (pipx, manual, production) and the full deployment guide.

## First-Time Setup

After installation, `eggpool onboard` walks you through:

1. **Connecting providers** — add API keys for your AI providers (OpenAI, Anthropic, OpenRouter, etc.)
2. **Configuration validation** — `check-config` verifies your setup
3. **Starting the server** — launch in daemon mode or as a systemd service

```bash
# Connect a specific provider
eggpool connect groq

# List available providers
eggpool connect list

# Validate configuration
eggpool check-config

# Start the server (daemon mode)
eggpool serve

# Start in foreground for debugging
eggpool serve --verbose
```

### Printing an Agent Config

Generate configuration for your coding agent:

```bash
# OpenCode
eggpool configsetup opencode

# Claude Code
eggpool configsetup claude-code

# Aider (writes .env.eggpool)
eggpool configsetup aider --model openai/gpt-4 --write

# Codex (Responses wire API)
eggpool configsetup codex --print-secret
```

See [Agent Configuration](docs/agent-configuration.md) for all supported targets and options.

### LAN Access

By default, EggPool binds to localhost. To expose it on your LAN:

1. Set a server API key first: `eggpool onboard` (or set `[server].api_key` in config)
2. Change the bind address: `[server].host = "0.0.0.0"` in `~/.config/eggpool/config.toml`
3. Restart: `eggpool restart`

See [Firewall](docs/firewall.md) for restricting access to your LAN.

## CLI Commands

| Command | Description |
|---------|-------------|
| `eggpool serve` | Start the proxy server (daemon mode; `--verbose` for foreground) |
| `eggpool stop` | Stop the running server |
| `eggpool restart` | Fully restart the server (stop then start) |
| `eggpool rehash` | Apply supported config changes live without restart (`--json` for structured output) |
| `eggpool onboard` | Interactive onboarding wizard |
| `eggpool connect` | Add a provider account interactively |
| `eggpool connect list` | List supported providers |
| `eggpool logout` | Remove a configured provider account |
| `eggpool check-config` | Validate configuration |
| `eggpool migrate` | Run database migrations |
| `eggpool models refresh` | Refresh the model catalog |
| `eggpool accounts list` | List configured provider accounts |
| `eggpool accounts status` | Show account status (provider, priority, weight, enabled) |
| `eggpool accounts explain` | Show per-account routing eligibility for a model |
| `eggpool stats transcoding` | Show protocol transcoding statistics |
| `eggpool stats repair-costs` | Dry-run/apply repair for suspicious historical request costs |
| `eggpool stats recompute-costs` | Recompute `cost_microdollars` on historical requests |
| `eggpool stats explain-dashboard` | Show EXPLAIN QUERY PLAN for dashboard queries |
| `eggpool modelinfo show` | Show enriched model metadata |
| `eggpool modelinfo list` | List model-info entries |
| `eggpool modelinfo refresh` | Trigger model-info source refresh |
| `eggpool modelinfo aliases` | Show model aliases |
| `eggpool modelinfo repair` | Repair legacy canonical model-info detail blocks |
| `eggpool dashboard public` | Print dashboard public URL |
| `eggpool runtime-status` | Print runtime health summary |
| `eggpool backup` | Create a timestamped backup |
| `eggpool recover` | Restore from a backup archive |
| `eggpool db vacuum` | Vacuum the SQLite database |
| `eggpool set` | Set a config value |
| `eggpool edit` | Edit config in $EDITOR |
| `eggpool getkey` | Print the server API key |
| `eggpool newkey` | Generate and write a new server API key |
| `eggpool init-config` | Initialize config from template |
| `eggpool version` | Show installed version |
| `eggpool croncheck` | Fast-path cron watchdog check (stdlib-only) |
| `eggpool ensure-running` | Ensure server is running (stdlib-only) |
| `eggpool deploy systemd` | Print/install systemd unit |
| `eggpool deploy cron` | Install watchdog cron (non-systemd) |
| `eggpool deploy backup-cron` | Install daily backup cron job |
| `eggpool deploy logrotate` | Print/install logrotate config |
| `eggpool deploy all` | Print every deployment snippet in sequence |
| `eggpool configsetup` | Generate config snippets for coding agents (see [Agent Configuration](docs/agent-configuration.md)) |
| `eggpool update [VERSION]` | Check for latest, or install an exact PyPI release (`v` prefix accepted) |
| `eggpool uninstall` | Uninstall EggPool from this machine |

All commands accept `--config /path/to/config.toml`. Config resolution: `--config` > `$EGGPOOL_CONFIG` > `~/.config/eggpool/config.toml` > `./config.toml`.

Full deploy commands reference: [docs/deployment.md](docs/deployment.md#deploy-commands-reference)

## Configuration

Configuration lives in a single TOML file. API keys are loaded from environment variables or `.env`.

```toml
# Example provider configuration
[providers.opencode-go]
id = "opencode-go"
base_url = "https://opencode.ai/zen/go/v1"
protocols = ["openai", "anthropic"]

[[providers.opencode-go.accounts]]
name = "personal"
api_key = "sk-your-opencode-go-key"
```

Use `eggpool connect` for interactive provider setup. See [docs/providers.md](docs/providers.md) for the full provider catalog, configuration details, and troubleshooting.

### Key Config Sections

| Section | Purpose |
|---------|---------|
| `[server]` | Bind address, port (default 11300), API key, logging, threads |
| `[upstream]` | Upstream API base URL, timeouts, connection pool |
| `[database]` | SQLite path, WAL mode, WAL size limit |
| `[models]` | Catalog refresh, exposure mode, model collapse, withdrawal policy |
| `[routing]` | Routing strategy, retry limits, quota mode, same-tier fairness, bounded wire negotiation |
| `[dashboard]` | Dashboard toggle, theme, refresh interval |
| `[providers.*]` | Provider configs with accounts and routing priority |
| `[network]` | Outbound transport and proxy settings |
| `[transcoder]` | Protocol transcoding between OpenAI and Anthropic |
| `[metrics]` | Observability write buffering (`immediate` / `balanced` / `low_wear`) |
| `[security]` | Redacted error-detail persistence |
| `[backup]` | Opt-in automatic daily backups |
| `[limits]` | Spend ceilings per account (5h / weekly / monthly microdollars) |
| `[pricing]` | Pricing catalog sources and missing-rate fallback |
| `[model_info]` | Multi-source model metadata enrichment |
| `[maintenance]` | Bounded maintenance budget, SQLite hygiene, contention guard |

Provider surfaces can be declared under `[providers.<id>.wire_surfaces.<surface>]`
when one provider exposes different endpoint paths or auth headers. Existing
`protocols`, `openai_path`, `responses_path`, and `anthropic_path` settings remain
valid and are synthesized into equivalent candidates.

EggPool keeps a bounded, in-memory preference for the last successful declared
wire surface per provider/model. The preference is refreshed by ordinary
successful requests and discarded naturally on restart or candidate-definition
changes; it never stores credentials or upstream response bodies. Negotiation is
reactive and only a separately classified, deterministic pre-handoff
auth/surface/schema failure may authorize an alternate-surface attempt on the
same account. Bare or unknown 401 responses do not disable credentials or
trigger failover; explicit credential failures affect only the selected account.
Alternate-wire and account retries share the same upstream-submission budget.

Full config reference: [`config.example.toml`](config.example.toml) | [docs/providers.md](docs/providers.md)

### Live Config Changes

`eggpool rehash` applies provider/account/routing/model-override changes without a restart. Disruptive changes (host, port, database path) require `eggpool restart`.

See [Live Configuration Rehash](docs/live-config-rehash.md) for the full reload flow and supported fields.

## Documentation

| Topic | Link |
|-------|------|
| Deployment (install, systemd, production) | [docs/deployment.md](docs/deployment.md) |
| Provider catalog & configuration | [docs/providers.md](docs/providers.md) |
| API endpoints | [docs/api-reference.md](docs/api-reference.md) |
| Agent configuration (OpenCode, Claude Code, Aider, etc.) | [docs/agent-configuration.md](docs/agent-configuration.md) |
| Stateless Responses support | [docs/stateless-responses.md](docs/stateless-responses.md) |
| Protocol transcoding | [docs/transcoding.md](docs/transcoding.md) |
| Backup & restore | [docs/backup-restore.md](docs/backup-restore.md) |
| Release procedure | [docs/releasing.md](docs/releasing.md) |
| Per-account outbound proxy | [docs/proxy.md](docs/proxy.md) |
| Model context limits | [docs/model-limits.md](docs/model-limits.md) |
| Thinking & reasoning | [docs/thinking.md](docs/thinking.md) |
| Raspberry Pi setup | [docs/raspberry-pi.md](docs/raspberry-pi.md) |
| Copyable SBC configuration | [config.sbc.example.toml](config.sbc.example.toml) |
| Configuration profiles | [docs/config-profiles.md](docs/config-profiles.md) |
| Firewall configuration | [docs/firewall.md](docs/firewall.md) |
| Filesystem layout | [docs/filesystem-layout.md](docs/filesystem-layout.md) |
| Network & DNS diagnostics | [docs/network-diagnostics.md](docs/network-diagnostics.md) |
| OpenCode stream stability | [docs/opencode-stream-stability.md](docs/opencode-stream-stability.md) |
| Model-info OpenRouter debugging | [docs/model-info-openrouter-debug.md](docs/model-info-openrouter-debug.md) |
| Live Configuration Rehash | [docs/live-config-rehash.md](docs/live-config-rehash.md) |
| Dispatch stability runbook | [docs/operations/dispatch-stability.md](docs/operations/dispatch-stability.md) |
| Database recovery runbook | [docs/runbooks/database-recovery.md](docs/runbooks/database-recovery.md) |
| Architecture overview | [architecture/README.md](architecture/README.md) |

## Development

```bash
uv sync --extra dev      # install dependencies

# Reproduce the exact CI environment (without local coverage tooling)
uv sync --frozen --extra ci

# Before-push check (matches CI job)
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
uv run pytest tests/smoke/ -q --tb=short --maxfail=1

# Optional `orjson` backend for the JSON helper (transcoding hot paths)
uv sync --extra fast     # or: uv pip install 'eggpool[fast]'
```

### CI

One GitHub Actions job on every PR:

| Job | Python | What it does |
|-----|--------|-------------|
| `check` | 3.11 | ruff format + ruff check + pyright + `pytest tests/smoke/` |

See `AGENTS.md` for focused test subset commands.

## License

MIT
