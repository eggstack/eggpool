# opencode-go-aggregator

A lightweight, LAN-hosted proxy that aggregates multiple OpenCode Go subscriptions behind a single endpoint.

## Features

- Transparently proxies OpenCode Go model requests
- Supports OpenAI-compatible and Anthropic-compatible upstream request paths
- Dynamically discovers currently available models
- Routes requests across subscriptions based on estimated quota utilization
- Tracks request, token, model, latency, error, and estimated-cost statistics in SQLite
- Exposes a read-only dashboard for historical and current usage
- Runs on a Raspberry Pi with Ubuntu using a single-process ASGI deployment

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) for dependency management

## Quick Start

```bash
# Install dependencies
uv sync --extra dev

# Copy and edit configuration
cp config.example.toml config.toml
cp .env.example .env

# Edit config.toml with your settings (accounts, upstream URL, etc.)
# Edit .env with your API keys

# Validate configuration
uv run go-aggregator check-config --config config.toml

# Run database migrations
uv run go-aggregator migrate --config config.toml

# Start the server
uv run go-aggregator serve --config config.toml
```

## CLI Commands

| Command | Description |
|---------|-------------|
| `go-aggregator serve` | Start the aggregation proxy server (default command) |
| `go-aggregator check-config` | Validate the configuration file |
| `go-aggregator migrate` | Run database migrations |

All commands accept `--config /path/to/config.toml` (defaults to `config.toml`).

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
- `[dashboard]` — Dashboard toggle, retention, refresh interval
- `[security]` — Allowed hosts, CORS, header redaction
- `[[accounts]]` — One entry per OpenCode Go subscription

## Development

```bash
# Install with dev dependencies
uv sync --extra dev

# Run linter
uv run ruff check src/ tests/

# Auto-fix lint issues
uv run ruff check --fix src/ tests/

# Run formatter
uv run ruff format src/ tests/

# Run type checker
uv run pyright src/

# Run tests
uv run pytest

# Run tests with coverage
uv run coverage run -m pytest
uv run coverage report
```

## Project Structure

```
src/go_aggregator/
├── __init__.py          # Package version
├── __main__.py          # python -m go_aggregator
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
│   ├── repositories.py  # Data access layer (Account, Request, Reservation, Attempt, Usage, Price repos)
│   └── schema/
│       ├── 0001_initial.sql
│       ├── 0002_indexes.sql
│       ├── 0003_request_attempts.sql
│       └── 0004_integration_hardening.sql
├── request/
│   └── coordinator.py   # Central request lifecycle orchestrator
├── accounts/            # Account registry and state
├── catalog/             # Model catalog, pricing, and estimation
├── routing/             # Quota-aware routing and eligibility
├── proxy/               # Transparent proxy and streaming
├── retry/               # Error classification and failover
├── health/              # Circuit breaker and health tracking
├── quota/               # Quota estimation, reservations, scoring
├── stats/               # Statistics queries and service
├── api/                 # API endpoint handlers and error shaping
└── dashboard/           # Server-rendered HTML dashboard

tests/
├── unit/                # Unit tests
├── integration/         # Integration tests (mocked upstreams)
└── contract/            # Contract tests (response format)
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

## Known Limitations

- Usage is authoritative only for traffic that passes through the proxy.
- OpenCode may not expose exact subscription reset windows.
- Interrupted streams may not contain terminal usage.
- Published prices may not perfectly match upstream subscription accounting.
- Accounts used outside the proxy require manual offsets for accurate balancing.
- Model metadata and protocol behavior can change without notice.
- Aggregating multiple subscriptions may not be an explicitly supported OpenCode deployment pattern.
- LAN-only deployment reduces but does not eliminate security obligations.
- Configuration changes require service restart (live reload disabled for correctness).

## License

MIT

## Deployment

See `docs/deployment.md` for production deployment instructions.

Quick start for development:

```bash
uv run go-aggregator serve --config config.toml
```

For production (systemd):

```bash
sudo systemctl enable --now gorouter
```

Configuration changes require a service restart:

```bash
sudo systemctl restart gorouter
```
