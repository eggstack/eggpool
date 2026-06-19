# EggPool

A lightweight, LAN-hosted proxy that aggregates OpenCode Go and compatible
provider accounts behind one OpenAI/Anthropic-compatible endpoint.

## Features

- Transparently proxies OpenCode Go model requests
- Supports OpenAI-compatible and Anthropic-compatible upstream request paths
- Dynamically discovers currently available models
- Routes requests across subscriptions based on estimated quota utilization
- Tracks request, token, model, latency, error, and estimated-cost statistics in SQLite
- Exposes a self-updating single-page dashboard for current usage at a glance
- Runs on a Raspberry Pi with Ubuntu using a single-process ASGI deployment

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) for dependency management

## Quick Start

### Option 1: Automated install

```bash
curl -fsSL https://raw.githubusercontent.com/eggstack/eggpool/main/scripts/install.sh | bash
```

The script:

- Downloads the repository if not running from a clone
- Installs `uv` if missing
- Verifies Python 3.12+
- Installs dependencies
- Copies example configuration files
- Attempts configuration validation

Validation fails until `.env` contains real, non-placeholder keys. Edit
`config.toml` and `.env`, then run the validation and migration commands below.

### Option 2: Manual install

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
uv run eggpool --config config.toml check-config

# Run database migrations
uv run eggpool --config config.toml migrate

# Start the server
uv run eggpool --config config.toml serve
```

The validated baseline sequence is: `uv run eggpool --help`,
`check-config` with exported environment variables, and `migrate` against the
configured SQLite path.

## CLI Commands

| Command | Description |
|---------|-------------|
| `eggpool serve` | Start the aggregation proxy server (default command) |
| `eggpool check-config` | Validate the configuration file |
| `eggpool migrate` | Run database migrations |
| `eggpool models refresh` | Refresh the model catalog from upstream (syncs accounts first) |
| `eggpool accounts status` | Show configured account status and key environment variables |
| `eggpool accounts list` | List configured provider accounts and API key backends |
| `eggpool db vacuum` | Reclaim SQLite space via the lock-owned `Database.vacuum()` helper |
| `eggpool connect` | Interactive provider connection setup |
| `eggpool connect list` | List available providers for connection |
| `eggpool logout` | Remove a configured provider account |
| `eggpool rehash` | Reload configuration in the running server |

All commands accept `--config /path/to/config.toml` (defaults to `config.toml`).
Configuration changes require a process restart; live reload is intentionally
not supported.

## Operational Scripts

Scripts under `scripts/`:

- `scripts/install.sh` — quick install script for local development setup
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

JSON stats endpoints are available under `/api/stats/*`, including summary,
accounts, models, timeseries, errors, latency, pings, bandwidth, and `/api/events`.

## Configuration

Configuration uses a single TOML file. API keys are loaded from environment variables.

See `config.example.toml` for all available options.

### Key Sections

- `[server]` — Bind address, port, API key environment variable, logging
- `[upstream]` — Upstream API base URL, timeouts, connection pool
- `[database]` — SQLite path, WAL mode, synchronous mode
- `[models]` — Catalog refresh interval, exposure mode
- `[routing]` — Routing strategy, retry limits, penalties
- `[limits]` — Quota windows (5-hour, weekly, monthly)
- `[dashboard]` — Dashboard toggle, theme, retention, refresh interval
- `[security]` — Allowed hosts, CORS, header redaction
- `[[accounts]]` — One entry per OpenCode Go subscription

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
│   └── body.py              # Bounded request body reading
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
└── security/            # Header redaction and security utilities

scripts/                 # Operational scripts
├── install.sh           # Quick install script for local development
├── check_database.py    # Read-only database invariant checker
├── smoke_test.py        # Deployment smoke test for a running proxy
└── verify_upstream_auth.py  # Direct-upstream auth verifier (operator-only)

tests/
├── unit/                # Unit tests
├── integration/         # Integration tests (mocked upstreams)
├── contract/            # Contract tests (response format)
└── fixtures/            # Test data and schema baselines
```

## Implementation Status

- [x] Phase 0: Repository and tooling foundation
- [x] Phase 1: Configuration, database, and application lifecycle
- [x] Phase 2: Account registry and model discovery
- [x] Phase 3: Non-streaming transparent proxy
- [x] Phase 4: Streaming proxy
- [x] Phase 5: Usage extraction and price accounting
- [x] Phase 6: Quota-aware routing and reservations
- [x] Phase 7: Retry, failover, and health management
- [x] Phase 8: Statistics API and dashboard
- [x] Phase 9: Deployment hardening
- [x] Phase 10: Integration hardening and correct request lifecycle
- [x] Phase 11: Quota lifecycle and failover correctness
- [x] Phase 12: Executable correctness pass
- [x] Phase 13: Attempt lifecycle and transaction hardening
- [x] Phase 14: Deployment blockers and operational hardening
- [x] Phase 15: Concurrency and accounting correctness
- [x] Phase 17: Deployment readiness corrections
- [x] Phase 18: Final cleanup before live testing

## Known Limitations

- Usage is proxy-observed; only traffic routed through the proxy is tracked.
- Weekly and monthly quota windows are rolling approximations unless OpenCode exposes authoritative subscription resets.
- Interrupted streams may not contain terminal usage.
- Published prices may not perfectly match upstream subscription accounting.
- Context-tiered prices are conservatively estimated until pricing-rule support is added.
- Accounts used outside the proxy require manual offsets for accurate balancing.
- Model metadata and protocol behavior can change without notice.
- Both `/v1/chat/completions` (OpenAI) and `/v1/messages` (Anthropic) endpoints are required because mixed protocol catalogs resolve per-model.
- The dashboard and stats routes require the local API key by default; set `dashboard.public = true` for unauthenticated access.
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
