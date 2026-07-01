[![PyPI version](https://badge.fury.io/py/eggpool.svg)](https://pypi.org/project/eggpool/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![CI](https://github.com/eggstack/eggpool/actions/workflows/ci.yml/badge.svg)](https://github.com/eggstack/eggpool/actions/workflows/ci.yml)

# EggPool

A lightweight, LAN-hosted proxy that aggregates multiple AI provider accounts behind one OpenAI/Anthropic-compatible endpoint.

## Features

- Proxies model requests across multiple providers and accounts behind a single endpoint
- Supports OpenAI-compatible and Anthropic-compatible upstream request paths
- Dynamically discovers available models; routes by quota utilization
- Per-account outbound proxy support ([pproxy](https://pypi.org/project/pproxy/) — SOCKS5, HTTP, Shadowsocks)
- Tracks requests, tokens, latency, errors, and estimated costs in SQLite
- Multi-page dashboard with 50+ themes, reliability, routing, and runtime views
- Model metadata enrichment from provider catalogs, OpenRouter, Artificial Analysis, and Hugging Face
- Designed for lightweight deployments (Raspberry Pi, SBCs)
- Transparent protocol transcoding between OpenAI and Anthropic request formats
- Thinking/reasoning capability-aware routing with configurable budget mapping

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

## CLI Reference

| Command | Description |
|---------|-------------|
| `eggpool serve` | Start the proxy server (`--daemon` to detach) |
| `eggpool onboard` | Interactive onboarding wizard |
| `eggpool connect` | Add a provider account interactively |
| `eggpool connect list` | List supported providers |
| `eggpool check-config` | Validate configuration |
| `eggpool migrate` | Run database migrations |
| `eggpool rehash` | Restart to apply config changes |
| `eggpool stop` | Stop the running server |
| `eggpool models refresh` | Refresh the model catalog |
| `eggpool stats transcoding` | Show protocol transcoding statistics |
| `eggpool accounts status` | Show configured account status (provider, priority, weight, enabled) |
| `eggpool accounts explain` | Show per-account routing eligibility for a model |
| `eggpool runtime-status` | Print runtime health summary |
| `eggpool backup` | Create a timestamped backup |
| `eggpool recover` | Restore from a backup archive |
| `eggpool deploy systemd` | Install/manage systemd service |
| `eggpool deploy cron` | Install watchdog cron (non-systemd) |
| `eggpool update` | Check for and install updates |

All commands accept `--config /path/to/config.toml`. Config resolution: `--config` > `$EGGPOOL_CONFIG` > `~/.config/eggpool/config.toml` > `./config.toml`.

Full command reference: [docs/deployment.md](docs/deployment.md#deploy-commands-reference)

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
| `[database]` | SQLite path, WAL mode |
| `[models]` | Catalog refresh, exposure mode, model collapse, withdrawal policy |
| `[routing]` | Routing strategy, retry limits, quota mode, same-tier fairness |
| `[dashboard]` | Dashboard toggle, theme, refresh interval |
| `[providers.*]` | Provider configs with accounts and routing priority |
| `[network]` | Outbound transport, DNS cache |
| `[model_info]` | Optional model metadata refresh, aliases, overrides, and external source settings |
| `[transcoder]` | Protocol transcoding between OpenAI and Anthropic formats |

The catalog refresh is **non-destructive by default**: failed, empty, or partial upstream responses never silently de-pool a healthy account. Set `[models].catalog_withdrawal_policy` (`preserve_until_health` default, `confirmed_once`, `confirmed_twice`) to opt into destructive behavior on authoritative refreshes. See `architecture/README.md` § Catalog Refresh Semantics.

Full config reference: [`config.example.toml`](config.example.toml) | [docs/providers.md](docs/providers.md)

## Protocol transcoding

When `[transcoder] enabled = true`, EggPool bridges OpenAI Chat Completions and Anthropic Messages bidirectionally so a single client ecosystem (e.g. OpenCode, which speaks only OpenAI) can reach Anthropic-only upstreams (e.g. MiniMax International at `api.minimax.io/anthropic`) and vice versa.

What gets translated:

- Request bodies (text + tool-use + vision + thinking + structured outputs)
- Streaming SSE events (including tool-call deltas and thinking deltas)
- Non-retryable error envelopes
- Usage and cost fields (preserved exactly as the upstream reported them)

What is dropped with a structured warning log:

- OpenAI fields with no Anthropic equivalent (`logit_bias`, `presence_penalty`, `top_logprobs`, etc.)
- Anthropic fields with no OpenAI equivalent (`top_k`, `cache_control`)

Phase 6 feature flags (`[transcoder.features]`) — all **off** by default:

- `tools` — bidirectional tool calling translation
- `vision` — image/document content parts
- `thinking` — extended thinking ↔ reasoning_content
- `structured_outputs` — `response_format` / `json_schema` coercion
- `anthropic_primitives` — `top_k`, `cache_control`, `context_management`, `container`, `mcp_servers`

See [docs/transcoding.md](docs/transcoding.md) for the full translation table and known limitations.

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/v1/models` | List available models |
| `POST` | `/v1/chat/completions` | OpenAI-compatible chat completions |
| `POST` | `/v1/messages` | Anthropic-compatible messages |
| `GET` | `/v1/healthz` | Liveness check |
| `GET` | `/v1/readyz` | Readiness check |
| `GET` | `/api/backoffs` | Active upstream-derived account backoffs (`?now=<epoch>` for reproducible snapshots) |
| `GET` | `/api/model-info` | Enriched model metadata summaries |
| `GET` | `/api/model-info/{model_id}` | Enriched metadata detail for one model |
| `GET` | `/api/model-info/{model_id}/aliases` | Source-keyed alias rows for one model |
| `GET` | `/api/model-info/sources` | Model-info source health |
| `POST` | `/api/model-info/refresh` | Trigger model-info refresh — `?model_id=<id>&source=<provider_catalog\|openrouter\|artificial_analysis\|huggingface>&force=1` for a single-model force refresh (auth-gated). `model_id` accepts provider-suffixed IDs (`gpt-4o/openai`); unknown source values return HTTP 400 |

When `[dashboard].enabled = true`, a multi-page dashboard is served at `/` with request stats, latency metrics, provider health, model-info detail pages, and more. Stats API available under `/api/stats/*`.

## Documentation

| Topic | Link |
|-------|------|
| Deployment (install, systemd, production) | [docs/deployment.md](docs/deployment.md) |
| Provider catalog & configuration | [docs/providers.md](docs/providers.md) |
| Backup & restore | [docs/backup-restore.md](docs/backup-restore.md) |
| Per-account outbound proxy | [docs/proxy.md](docs/proxy.md) |
| Model context limits | [docs/model-limits.md](docs/model-limits.md) |
| Raspberry Pi setup | [docs/raspberry-pi.md](docs/raspberry-pi.md) |
| Firewall configuration | [docs/firewall.md](docs/firewall.md) |
| Filesystem layout | [docs/filesystem-layout.md](docs/filesystem-layout.md) |
| Network & DNS diagnostics | [docs/network-diagnostics.md](docs/network-diagnostics.md) |
| Protocol transcoding | [docs/transcoding.md](docs/transcoding.md) |
| Thinking & reasoning | [docs/thinking.md](docs/thinking.md) |

## Development

```bash
uv sync --extra dev
uv run ruff check src/ tests/ scripts/
uv run ruff format src/ tests/ scripts/
uv run pyright src/ scripts/
uv run pytest
```

## Agent Configuration

`eggpool configsetup` generates configuration snippets for popular coding agents:

| Target | Command | Output | `--write` default | Model | Status |
|--------|---------|--------|-------------------|-------|--------|
| OpenCode | `eggpool configsetup opencode` | JSON provider config | N/A (clipboard) | auto | stable |
| Claude Code | `eggpool configsetup claude-code` | JSON snippet | N/A (clipboard) | N/A | stable |
| Aider | `eggpool configsetup aider` | Shell env exports | `.env.eggpool` | recommended | stable |
| Codex | `eggpool configsetup codex` | TOML provider block | N/A (printed) | recommended | version-sensitive |
| Qwen Code | `eggpool configsetup qwen-code` | JSON provider block | N/A (printed) | optional | verify schema |
| Kilo | `eggpool configsetup kilo` | JSON provider block | N/A (printed) | optional | verify schema |
| Continue | `eggpool configsetup continue` | YAML model block | `~/.continue/eggpool.yaml` | usually yes | stable fragment |
| Cline | `eggpool configsetup cline` | JSON profile | `cline-eggpool.json` | recommended | paste into UI |
| Roo Code | `eggpool configsetup roo-code` | JSON profile | `roo-code-eggpool.json` | recommended | paste into UI |
| Goose | `eggpool configsetup goose` | Shell env exports | N/A (printed) | recommended | verify env vars |
| OpenHands | `eggpool configsetup openhands` | Shell env exports | N/A (printed) | recommended | stable fragment |

Shared options: `--host`, `--base-url`, `--model`, `--write`, `--output`, `--force`, `--no-clipboard`, `--print-secret`.
Generated JSON, TOML, YAML, and shell snippets escape catalog/config values for
the target format, including provider-suffixed model IDs.

Examples:
```sh
eggpool configsetup aider --model openai/gpt-4 --write
eggpool configsetup continue --model claude-sonnet-4 --output ~/.continue/eggpool.yaml
eggpool configsetup cline --no-clipboard
```

## License

MIT
