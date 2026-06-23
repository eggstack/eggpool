[![PyPI version](https://badge.fury.io/py/eggpool.svg)](https://pypi.org/project/eggpool/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![CI](https://github.com/eggstack/eggpool/actions/workflows/ci.yml/badge.svg)](https://github.com/eggstack/eggpool/actions/workflows/ci.yml)

# EggPool

A lightweight, LAN-hosted proxy that aggregates multiple AI provider accounts
behind one OpenAI/Anthropic-compatible endpoint.

## Features

- Transparently proxies model requests across multiple providers
- Supports OpenAI-compatible and Anthropic-compatible upstream request paths
- Dynamically discovers currently available models from each provider
- Routes requests across accounts based on estimated quota utilization
- Per-account outbound proxy support via [pproxy](https://pypi.org/project/pproxy/) (SOCKS5, HTTP, or any pproxy URI)
- Tracks request, token, model, latency, error, and estimated-cost statistics in SQLite
- Multi-page dashboard with overview, accounts, models, latency, pings, events, timeseries, and bandwidth views
- 50+ themes from [Halloy](https://github.com/squidowl/halloy) and [Chart.js](https://www.chartjs.org/) v4 (MIT) for dashboard charts
- Designed for lightweight deployments such as Raspberry Pis

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) for dependency management

## Quick Start

### Option 1: pipx install (recommended)

```bash
pipx install eggpool
eggpool help
```

`pipx` installs `eggpool` into its own venv and exposes the
`eggpool` console script globally. The bundled themes and
provider templates ship inside the package — no extra files
required to start.

Then copy and edit configuration:

```bash
cp /path/to/your/eggpool-venv/lib/python*/site-packages/eggpool/_share/config.example.toml ~/.config/eggpool/config.toml
```

Or use the built-in helper:

```bash
eggpool init-config
```

### Option 2: Automated install

```bash
curl -fsSL https://raw.githubusercontent.com/eggstack/eggpool/main/scripts/install.sh | bash
```

The script:

- Downloads the repository if not running from a clone
- Installs `uv` if missing
- Verifies Python 3.11+
- Installs dependencies
- Copies example configuration files
- Attempts configuration validation

Validation fails until `.env` contains real, non-placeholder keys. Edit
`config.toml` and `.env`, then run the validation and migration commands below.

### Option 3: Manual install

```bash
# Install dependencies, including local development tools
uv sync --extra dev

# Copy and edit configuration
cp config.example.toml config.toml
cp .env.example .env

# Edit config.toml for providers/accounts and .env for keys.
# check-config rejects placeholder values such as "your-api-key".

# Validate configuration
set -a; source .env; set +a
uv run eggpool check-config

# Run database migrations
uv run eggpool migrate

# Start the server
uv run eggpool serve
```

### Option 4: Interactive setup

```bash
# Run the interactive onboarding wizard — connects one or more
# providers, validates configuration, and starts the server.
uv run eggpool onboard

# Or connect to a specific provider
uv run eggpool connect
uv run eggpool connect list
```

## CLI Commands

| Command | Description |
|---------|-------------|
| `eggpool help` | Show help message and available commands |
| `eggpool version` | Print the installed version |
| `eggpool serve` | Start the aggregation proxy server (default command) |
| `eggpool check-config` | Validate the configuration file |
| `eggpool migrate` | Run database migrations |
| `eggpool onboard` | Run the interactive onboarding setup (connect providers, start server) |
| `eggpool connect` | Connect to a new provider interactively |
| `eggpool connect list` | List available providers for connection |
| `eggpool logout` | Remove a configured provider account |
| `eggpool rehash` | Restart the server to apply configuration changes |
| `eggpool restart` | Fully restart the server (stop then start) |
| `eggpool stop` | Stop the running server |
| `eggpool set` | Set a server configuration value and restart |
| `eggpool getkey` | Print the current server API key |
| `eggpool newkey` | Generate a new server API key |
| `eggpool edit` | Open the configuration file in the default editor |
| `eggpool configsetup` | Print configuration snippets for code editors |
| `eggpool configsetup opencode` | Print OpenCode provider config JSON with model limits |
| `eggpool configsetup claude-code` | Print Claude Code config snippet |
| `eggpool update` | Check for updates and reinstall if newer |
| `eggpool croncheck` | Lightweight check: exit 0 if server is running, exit 1 if not |
| `eggpool models refresh` | Refresh the model catalog from upstream |
| `eggpool accounts status` | Show configured account status |
| `eggpool accounts list` | List configured provider accounts |
| `eggpool dashboard public` | Toggle dashboard public access |
| `eggpool db vacuum` | Vacuum the database to reclaim space |
| `eggpool init-config` | Write bundled config.example.toml to current directory or TARGET |
| `eggpool deploy systemd` | Print the systemd unit + install instructions |
| `eggpool deploy logrotate` | Print the logrotate config + install instructions |
| `eggpool deploy cron` | Print the daily-backup cron entry + install instructions |
| `eggpool deploy all` | Print every deployment snippet in sequence |

All commands accept `--config /path/to/config.toml` (defaults to `config.toml`).
Running `eggpool` with no arguments prints the help message.
Configuration changes require a service restart; live reload is intentionally
not supported.

## Operational Scripts

Scripts under `scripts/`:

- `scripts/install.sh` — quick install script for local development setup
- `scripts/install_prompt.py` — installation prompt helper
- `scripts/check_database.py` — read-only database invariant checker. See
  `docs/deployment.md` for the documented exit-code contract.
- `scripts/smoke_test.py` — deployment smoke test for the running
  proxy. Exercises health, models, stats, non-streaming, and
  streaming endpoints for both protocol families.
- `scripts/verify_upstream_auth.py` — direct-upstream authentication
  verifier. Bypasses EggPool to confirm the configured key works
  against each upstream endpoint family. Operator-only; not run in CI.

## API Endpoints

### Data Plane (require local API key)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/v1/models` | List available models |
| `POST` | `/v1/chat/completions` | OpenAI-compatible chat completions |
| `POST` | `/v1/messages` | Anthropic-compatible messages |

### Health

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/v1/healthz` | Liveness check |
| `GET` | `/v1/readyz` | Readiness check (database, accounts, catalog) |

### Dashboard and Stats

When `[dashboard].enabled = true`, the dashboard is served at `/`. It defaults
to the bundled `Cyber Red` theme and refreshes visible data in place using the
configured `[dashboard].refresh_interval_s`.

The dashboard includes:
- Overview with request counts, error rates, costs, and token usage
- Account and model breakdowns with filtering
- Latency metrics including time-to-first-token (TTFT)
- Provider health monitoring with ping statistics
- Bandwidth heatmap (GitHub-style contribution graph)
- Timeseries charts with auto-refresh
- Interactive theme selector with 50+ [Halloy](https://themes.halloy.chat/) themes

Static assets (CSS, JavaScript, favicon) are served from `/static/` with
appropriate cache headers.

JSON stats endpoints are available under `/api/stats/*`, including summary,
accounts, models, timeseries, errors, latency, pings, bandwidth, and `/api/events`.

## Configuration

Configuration uses a single TOML file. API keys are loaded from environment variables.

See `config.example.toml` for all available options.

### Key Sections

- `[server]` — Bind address, port (default 11300), API key, logging
- `[upstream]` — Upstream API base URL, timeouts, connection pool
- `[database]` — SQLite path, WAL mode, synchronous mode
- `[models]` — Catalog refresh interval, exposure mode, staleness settings, `collapse_models` flag
- `[routing]` — Routing strategy, retry limits, penalties
- `[limits]` — Quota windows (5-hour, weekly, monthly)
- `[dashboard]` — Dashboard toggle, theme, retention, refresh interval
- `[security]` — Allowed hosts, CORS, header redaction
- `[providers.*]` — Provider configurations with accounts and `routing_priority`
- `[proxies.*]` — Named outbound proxy definitions (pproxy URI syntax)
- `[model_overrides.*]` — Per-model protocol or path overrides

### Provider Configuration

Providers are configured under `[providers.<id>]` with nested `[[providers.<id>.accounts]]` entries:

```toml
[providers.opencode-go]
id = "opencode-go"
base_url = "https://opencode.ai/zen/go/v1"
protocols = ["openai", "anthropic"]

[[providers.opencode-go.accounts]]
name = "personal"
api_key = "sk-your-opencode-go-key"
```

Use `eggpool connect` for interactive provider setup instead of manual configuration. Each provider account is auto-labeled with `routing_priority = 0` on first `eggpool connect`, so operators can rebalance later by editing `[providers.<id>].routing_priority` and restarting the service. See [docs/providers.md](docs/providers.md) for the full provider catalog with status definitions, verification commands, and provider-specific notes.

### Routing Priority and Model Collapse

Two related knobs control how requests for the same base model fan out across
providers and how the model appears in `/v1/models`:

- `[providers.<id>].routing_priority` — non-negative integer (default `0`).
  Higher values are preferred. Accounts inside a tier are still load-balanced
  by the existing quota-fair scorer.
- `[models].collapse_models` — boolean (default `false`). When `false`, the
  catalog exposes one provider-suffixed entry per `(model_id, provider_id)`.
  When `true`, the same base model collapses to a single unsuffixed ID and is
  routed across every provider that supports it.

`eggpool configsetup opencode` output reflects the current `collapse_models`
setting: suffixed IDs when `false`, unsuffixed when `true`. See
[docs/providers.md](docs/providers.md) for the full worked example with three
providers and three priorities.

### Per-Account Outbound Proxy

Each account can route upstream traffic through a [pproxy](https://pypi.org/project/pproxy/)-compatible outbound proxy. This is useful for geo-routing, residential IP rotation, or isolating provider traffic by account.

Three mutually exclusive fields on each account control the proxy:

| Field | Description |
|-------|-------------|
| `proxy` | Reference a named entry from `[proxies.*]` |
| `proxy_url` | Inline pproxy URI (use when the URI has no credentials) |
| `proxy_url_env` | Environment variable name holding the pproxy URI (use when the URI contains credentials) |

**Quick example — inline SOCKS5 proxy:**

```toml
[[providers.opencode-go.accounts]]
name = "personal"
api_key = "sk-your-key"
proxy_url = "socks5://127.0.0.1:1080"
```

**Named proxy with env-var credentials:**

```toml
[proxies.residential-us]
url_env = "MY_RESIDENTIAL_PROXY_URL"

[[providers.opencode-go.accounts]]
name = "personal"
api_key = "sk-your-key"
proxy = "residential-us"
```

The `proxy` field references a `[proxies.<name>]` entry, keeping credentials out of the config file. See `docs/proxy.md` for the full pproxy URI syntax and more examples.

### Model Limits

EggPool supports configurable effective context limits for individual models on individual providers. This lets operators advertise a smaller context window than the provider physically supports, causing OpenCode to compact before reaching expensive long-context regimes.

**Global overrides** apply to all providers:

```toml
[model_overrides."model-id"]
max_context_tokens = 200000
max_output_tokens = 16384
```

**Provider-specific overrides** take precedence per field:

```toml
[providers.opencode-go.model_overrides."MiniMax-M3"]
max_context_tokens = 220000
max_output_tokens = 16384
enforce_context_limit = true
```

When the same model is served by multiple providers, unsuffixed model exposure uses the conservative minimum across all providers.

To generate an OpenCode configuration with explicit model limits:

```bash
eggpool configsetup opencode --json-only > opencode-config.json
```

Merge the generated provider definition into your OpenCode configuration. OpenCode must consume these model definitions for proactive compaction to work --- without them, OpenCode uses default context sizes and will not compact before the effective limit.

Model limit changes require a service restart.

## Development

```bash
# Install with dev dependencies
uv sync --extra dev

# Run linter (covers src/, tests/, and operational scripts/)
uv run ruff check src/ tests/ scripts/

# Auto-fix lint issues
uv run ruff check --fix src/ tests/ scripts/

# Run formatter
uv run ruff format src/ tests/ scripts/

# Run type checker (covers src/ and scripts/)
uv run pyright src/ scripts/

# Run tests
uv run pytest

# Run tests with coverage
uv run coverage run -m pytest
uv run coverage report
```

## Project Structure

```
src/eggpool/
├── __init__.py          # Package version
├── __main__.py          # python -m eggpool
├── app.py               # FastAPI application factory
├── cli.py               # Click CLI commands
├── auth.py              # Local API key authentication
├── constants.py         # Project-wide constants
├── errors.py            # Exception hierarchy
├── logging.py           # Structured logging setup
├── onboard.py           # Interactive onboarding setup
├── models/
│   ├── config.py        # Pydantic config models
│   ├── domain.py        # Internal domain objects
│   ├── api.py           # API response models
│   └── database.py      # Database row models
├── db/
│   ├── connection.py    # SQLite connection manager
│   ├── migrations.py    # Schema migration runner
│   ├── repositories.py  # Data access layer
│   └── schema/          # Ordered SQLite migrations + checksums
├── request/
│   ├── coordinator.py       # Central request lifecycle orchestrator
│   ├── attempt_finalizer.py # Per-attempt terminal lifecycle
│   ├── finalizer.py         # Idempotent request finalization
│   ├── body.py              # Bounded request body reading
│   └── limits.py            # Token estimation and context limit enforcement
├── accounts/            # Account registry and state
├── catalog/             # Model catalog, pricing, estimation, and protocols
├── routing/             # Quota-aware routing, eligibility, provider parsing
├── providers/           # ProviderClientPool, pproxy transport, connect CLI
├── proxy/               # Transparent proxy, streaming, and SSE observer
├── retry/               # Error classification and failover
├── health/              # Circuit breaker and health tracking
├── quota/               # Quota estimation, reservations, scoring
├── stats/               # Statistics queries and service
├── api/                 # API endpoint handlers and error shaping
├── background/          # Background task supervisor and cleanup
├── dashboard/           # Self-updating server-rendered HTML dashboard
│   ├── render.py        # HTML rendering functions
│   ├── routes.py        # Dashboard HTTP routes
│   ├── theme.py         # TOML theme to CSS variable translation
│   ├── escape.py        # HTML escaping utilities
│   └── static/          # CSS, JavaScript, and favicon
├── integrations/        # External tool config generation (OpenCode, Claude Code)
├── security/            # Header redaction and security utilities
├── deploy/              # Bundled systemd/logrotate/cron snippets for CLI output
└── _share/              # Bundled config examples and assets for pipx installs

scripts/                 # Operational scripts
├── install.sh           # Quick install script
├── install_prompt.py    # Installation prompt helper
├── check_database.py    # Read-only database invariant checker
├── smoke_test.py        # Deployment smoke test
└── verify_upstream_auth.py  # Direct-upstream auth verifier

themes/                  # 50+ Halloy-format .toml theme files

tests/
├── unit/                # Unit tests
├── integration/         # Integration tests (mocked upstreams)
├── contract/            # Contract tests (response format)
└── fixtures/            # Test data and schema baselines

docs/                    # Documentation
├── deployment.md        # Production deployment guide
├── raspberry-pi.md      # Raspberry Pi setup guide
├── backup-restore.md    # Backup and restore procedures
├── firewall.md          # Firewall configuration
├── filesystem-layout.md # Filesystem layout reference
├── model-limits.md      # Model context limit configuration
├── providers.md         # Provider catalog and configuration guide
└── proxy.md             # Per-account outbound proxy (pproxy)

config-examples/         # Editor-specific config snippets
├── opencode.jsonc       # OpenCode provider config (JSONC)
└── claude-code.env      # Claude Code environment variables

deploy/                  # Deployment files
├── eggpool.service      # systemd unit file
├── eggpool-logrotate.conf  # Logrotate configuration
└── env.example          # Example environment file
```

## Known Limitations

- Usage is proxy-observed; only traffic routed through the proxy is tracked.
- Weekly and monthly quota windows are rolling approximations unless providers expose authoritative subscription resets.
- Interrupted streams may not contain terminal usage data.
- Published prices may not perfectly match upstream subscription accounting.
- Context-tiered prices are conservatively estimated until pricing-rule support is added.
- Accounts used outside the proxy require manual offsets for accurate balancing.
- Model metadata and protocol behavior can change without notice.
- Both `/v1/chat/completions` (OpenAI) and `/v1/messages` (Anthropic) endpoints are required because mixed protocol catalogs resolve per-model.
- The dashboard and stats routes are public by default; set `dashboard.public = false` for authenticated access.
- LAN-only deployment reduces but does not eliminate security obligations.
- Configuration changes require service restart (live reload disabled for correctness).

## License

MIT

## Deployment

See `docs/deployment.md` for production deployment instructions.

For production (systemd):

```bash
sudo systemctl enable --now eggpool
```

Configuration changes require a service restart; the unit
intentionally does not advertise any reload action:

```bash
sudo systemctl restart eggpool
sudo systemctl status eggpool
sudo journalctl -u eggpool -n 100 --no-pager
```

See [CHANGELOG](CHANGELOG.md) for release history.
