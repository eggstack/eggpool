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
| `go-aggregator models refresh` | Refresh the model catalog from upstream (also syncs configured accounts) |
| `go-aggregator accounts status` | Show configured account status and key environment variables |
| `go-aggregator db vacuum` | Reclaim SQLite space via the lock-owned `Database.vacuum()` helper |

All commands accept `--config /path/to/config.toml` (defaults to `config.toml`).

## Operational Scripts

Two scripts under `scripts/` are used as deployment release gates:

- `scripts/check_database.py` — read-only database invariant checker. See
  `docs/deployment.md` for the documented exit-code contract.
- `scripts/smoke_test.py` — deployment smoke test for the running
  proxy. Exercises health, models, stats, non-streaming, and
  streaming endpoints for both protocol families.
- `scripts/verify_upstream_auth.py` — direct-upstream authentication
  verifier. Bypasses GoRouter to confirm the configured key works
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
│       ├── 0004_integration_hardening.sql
│       ├── 0005_price_microdollars.sql
│       ├── 0006_correct_price_microdollars.sql
│       ├── 0007_price_cache_rates.sql
│       ├── 0008_proxy_request_identity.sql
│       ├── 0009_model_protocol_source.sql
│       ├── 0010_health_probe.sql
│       └── 0011_model_resolution_status.sql
├── request/
│   ├── coordinator.py       # Central request lifecycle orchestrator
│   ├── attempt_finalizer.py # Per-attempt terminal lifecycle
│   ├── finalizer.py         # Idempotent request finalization
│   └── body.py              # Bounded request body reading
├── accounts/            # Account registry and state
├── catalog/             # Model catalog, pricing, estimation, and protocols
├── routing/             # Quota-aware routing and eligibility
├── proxy/               # Transparent proxy, streaming, and SSE observer
├── retry/               # Error classification and failover
├── health/              # Circuit breaker and health tracking
├── quota/               # Quota estimation, reservations, scoring
├── stats/               # Statistics queries and service
├── api/                 # API endpoint handlers and error shaping
└── dashboard/           # Server-rendered HTML dashboard

scripts/                 # Operational release-gate scripts
├── check_database.py    # Read-only database invariant checker
├── smoke_test.py        # Deployment smoke test for a running proxy
└── verify_upstream_auth.py  # Direct-upstream auth verifier (operator-only)

tests/
├── unit/                # Unit tests
├── integration/         # Integration tests (mocked upstreams)
├── contract/            # Contract tests (response format)
└── fixtures/            # Test data, including the historical schema fixture
    └── schema/          # pre_phase17_v11.sql + checksums.json
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
- [x] Phase 12: Executable correctness pass
- [x] Phase 13: Attempt lifecycle and transaction hardening
- [x] Phase 14: Deployment blockers and operational hardening
- [x] Phase 15: Concurrency and accounting correctness
- [x] Phase 16: Final polish and release validation
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
- The dashboard is unauthenticated by default and intended for trusted LAN use only.
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

Configuration changes require a service restart; the unit
intentionally does not advertise any reload action:

```bash
sudo systemctl restart gorouter
sudo systemctl status gorouter
sudo journalctl -u gorouter -n 100 --no-pager
```
