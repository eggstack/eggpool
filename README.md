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
- Streaming finalizer is shielded from ASGI cancellation so client disconnects do not leak requests as `pending`
- Periodic stale-request finalizer background task force-finalizes requests whose finalizer never ran, preventing 503 saturation from leaked state
- Designed for lightweight deployments such as Raspberry Pis

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) for dependency management

## Quick Start

### Option 1: One-shot install (recommended for personal use)

```bash
curl -fsSL https://raw.githubusercontent.com/eggstack/eggpool/main/scripts/install.sh | bash
eggpool onboard
sudo env "PATH=$PATH" "$(command -v eggpool)" deploy systemd --install
```

The installer script:

- Clones the repo to `~/eggpool` (or uses an existing clone) and resets `PROJECT_DIR` to the actual install location
- Installs `uv` if missing and finds a Python 3.11+ interpreter
- Detects an existing `eggpool` on PATH and refuses to silently reinstall — pass `--force` or `--upgrade` for intentional updates
- Installs `eggpool` as a global command (`pipx install` if pipx is present, otherwise `uv tool install .`)
- Persists `~/.local/bin` on PATH via `uv tool update-shell`
- Seeds `~/.config/eggpool/config.toml` from the example template without overwriting an existing file
- Prints the resolved config path, validates the configuration, and offers to run onboarding

After install, the watchdog path (systemd unit, crontab entry, or `eggpool serve --daemon`) keeps the server alive across reboots. Use `sudo` only for the deploy step so the unit runs as your user, not as root.

### Option 2: pipx install

```bash
pipx install eggpool
eggpool onboard
sudo env "PATH=$PATH" "$(command -v eggpool)" deploy systemd --install
```

`pipx` installs `eggpool` into its own venv and exposes the `eggpool` command on your PATH. This produces the same end state as Option 1 minus the bundled config seeding.

If `eggpool` is not on PATH after the install, restart your shell or run:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

### Option 3: Manual install (from a clone)

```bash
git clone https://github.com/eggstack/eggpool.git && cd eggpool
uv sync --extra dev
uv tool install .
uv tool update-shell
export PATH="$HOME/.local/bin:$PATH"

cp config.example.toml config.toml
cp .env.example .env

# Edit config.toml for providers/accounts and .env for keys.
# check-config rejects placeholder values such as "your-api-key".
set -a; source .env; set +a
eggpool check-config
eggpool migrate
eggpool serve
```

### Option 4: Interactive setup (developer workflow)

```bash
uv run eggpool onboard        # wizard: connect providers, validate, start
uv run eggpool connect        # add a single provider
uv run eggpool connect list   # list supported providers
```

### Cron fallback for non-systemd systems

```bash
eggpool deploy cron --install
```

This writes a `@reboot` + `*/5 * * * *` `eggpool ensure-running` block to the invoking user's crontab (or `SUDO_USER`'s crontab under sudo). Use `--interval N` to change the poll cadence. The block is bracketed by `# BEGIN EggPool watchdog` / `# END EggPool watchdog` markers so uninstall only strips the eggpool-owned lines. See [docs/deployment.md](docs/deployment.md) for the full design.

### Backup and uninstall

EggPool ships lifecycle commands that mirror the install flow:

```bash
# Backup config + .env + database to ~/backups/eggpool/
eggpool backup

# Restore from a specific archive (or omit the path for an interactive menu)
eggpool recover ~/backups/eggpool/eggpool-backup-20260624-120000.zip

# Uninstall: detects pipx / uv tool / source / manual and cleans up
eggpool uninstall --yes

# Also remove systemd unit, logrotate config, and cron blocks
eggpool uninstall --yes --deploy-artifacts
```

The uninstall command removes the binary, active config, `.env`, database, and `eggpool` shell-rc entries. Pass `--deploy-artifacts` to also remove the systemd unit, logrotate config, watchdog + backup cron blocks, and the personal backup script. Existing backups under `~/backups/eggpool/` are always left in place. See [docs/backup-restore.md](docs/backup-restore.md) for the full backup/restore workflow.

## CLI Commands

| Command | Description |
|---------|-------------|
| `eggpool help` | Show help message and available commands |
| `eggpool version` | Print the installed version |
| `eggpool serve` | Start the aggregation proxy server (default command). Use `--daemon` to detach into the background (see [Daemon Mode](#daemon-mode)). |
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
| `eggpool ensure-running` | Repair: start the server if it is not running; no-op when already alive |
| `eggpool runtime-status` | Print compact runtime health summary from running server |
| `eggpool models refresh` | Refresh the model catalog from upstream |
| `eggpool accounts status` | Show configured account status |
| `eggpool accounts list` | List configured provider accounts |
| `eggpool dashboard public` | Toggle dashboard public access |
| `eggpool db vacuum` | Vacuum the database to reclaim space |
| `eggpool init-config` | Write bundled config.example.toml to current directory or TARGET |
| `eggpool deploy systemd` | Print systemd unit; `--install` writes it (personal by default; `--production` for the dedicated-system layout; `--as-root` for a root-owned personal unit) |
| `eggpool deploy cron` | Print / install / uninstall the **watchdog** crontab (`@reboot` + `*/N * * * *` `ensure-running`). `--interval N` (1-59, default 5) |
| `eggpool deploy backup-cron` | Print / install / uninstall the daily backup cron (personal user cron or production `/etc/cron.d/`) |
| `eggpool deploy logrotate` | Print / install the logrotate config (validated via `logrotate -d`) |
| `eggpool deploy all` | Print / install systemd + logrotate + watchdog cron (backup-cron is separate) |
| `eggpool backup` | Create a timestamped `.zip` backup (default `~/backups/eggpool/`) |
| `eggpool recover [path]` | Restore from a backup archive (interactive if no path) |
| `eggpool uninstall` | Remove binary, config, database, and shell PATH entries; `--deploy-artifacts` also removes the systemd unit, logrotate config, watchdog + backup cron blocks, and backup script |

All commands accept `--config /path/to/config.toml` (resolution: `--config` > `$EGGPOOL_CONFIG` > `~/.config/eggpool/config.toml` > `./config.toml`).
Running `eggpool` with no arguments prints the help message.
Configuration changes require a service restart; live reload is intentionally
not supported.

## Daemon Mode

For personal / SBC deployments where you want to start EggPool and get your
shell back, `eggpool serve` accepts a `--daemon` flag that spawns a detached
supervisor in the background and returns promptly:

```bash
eggpool --config config.toml serve --daemon
```

The daemon parent only validates the config and refuses to start a second
instance. The detached child runs the normal foreground `serve` command
(including the Granian supervisor + worker); the `--daemon` flag is **not**
forwarded to the child, so child behavior is identical to running `eggpool
serve` directly.

Flags:

- `--daemon` — spawn the detached supervisor and return the shell. Without this flag, `serve` blocks on Granian and prints logs to the terminal.
- `--log-file PATH` — redirect the supervisor's stdout/stderr to `PATH`. Defaults to `~/.local/state/eggpool/eggpool.log` (resolvable via `eggpool.runtime_paths.default_log_file()`; honors `$EGGPOOL_LOG_FILE`). The default is intentional: a background start that fails silently is hard to diagnose, so a log file beats `/dev/null`.
- `--quiet` — with `--daemon` and no `--log-file`, send the supervisor's stdout/stderr to `/dev/null`. Has no effect without `--daemon`; the foreground command always streams to the terminal.
- `--as-root` — allow daemonizing when the effective UID is 0. Refused by default to prevent accidentally starting a personal deployment as root. Pass this flag for intentional system-wide installs.

The child's stdin is closed (`subprocess.DEVNULL`) and stdout/stderr are
redirected to the configured log file (or `/dev/null` when `--quiet` is set
without `--log-file`). The child detaches via `start_new_session=True` so it
survives shell exit and signals to the parent CLI do not propagate to it.

Systemd units should **not** use `--daemon`. The systemd unit already
manages the process lifecycle; run foreground `serve` and let systemd
own the supervisor PID, journal logs, and restart policy.

The cron watchdog command `eggpool ensure-running` continues to work the
same way; `serve --daemon` is the explicit operator-facing one-shot for
starting the server detached (e.g. from an interactive shell after a
fresh install).

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
- Overview with request counts, error rates, costs, token usage, and a System Health row surfacing pending-request and reservation leaks
- Reliability page (`/reliability`) with attempt success/retry breakdown, `retry_category` distribution, pending health, and operational events
- Routing page (`/routing`) with per-`(model, provider)` decision aggregates, account selection counts, and exclusion taxonomy (suppressive vs advisory)
- Runtime page (`/runtime`) with process topology, memory, background task status, database health, and in-flight request counts
- Traces page (`/traces`) with auth-gated recent request metadata (no error_detail, no client_ip)
- Account and model breakdowns with filtering, exactness columns, cache/reasoning ratios, and cost-per-1k-tokens
- Latency metrics including time-to-first-token (TTFT) and connect/read/coordinator-overhead phase breakdown
- Provider health monitoring with ping statistics
- Bandwidth heatmap (GitHub-style contribution graph)
- Timeseries page (`/timeseries`) with a stacked-bar grouped usage chart (default `provider_model`, configurable to `provider`/`model`/`account`), per-bucket detail table, and a controls form for period, bucket, group_by, metric, and top-N limit. Backed by `/api/timeseries/grouped`. Top-N series beyond the limit fold into an `Other` bucket so bucket totals stay loss-less
- Interactive theme selector with 50+ [Halloy](https://themes.halloy.chat/) themes

### Dashboard Tooltips

Heatmap cells, sortable column headers, topbar controls (refresh, theme,
period), and account/model status badges expose short descriptions on hover
through a pure-CSS `[data-tooltip]` system. The bubble re-themes
automatically via the existing `--card-bg`, `--card-border`, and
`--page-text` CSS variables; every tooltip target also sets `aria-label`
so screen readers announce the same text. The native `<title>` element is
preserved on SVG cells as a fallback. The system is JavaScript-free and
survives the overview page auto-refresh `innerHTML` swap.

Static assets (CSS, JavaScript, favicon) are served from `/static/` with
appropriate cache headers.

JSON stats endpoints are available under `/api/stats/*`, including summary,
accounts, models, timeseries, errors, latency, pings, bandwidth, attempts,
retries, routing, routing-selections, routing-exclusions, operational,
pending-health, runtime, recent-requests, recent/{request_id},
`/api/stats/update`, and `/api/events`. The recent-requests, recent/{request_id}, pending-health,
and runtime endpoints are always auth-gated (even when the dashboard is
public) because they expose per-request metadata (model, prompt volume,
error class), operational state (pending reservations, reserved cost),
or process-level details (PID, memory, DB path, background task names).
The `/api/timeseries/grouped` endpoint backs the `/timeseries` chart and
returns the documented grouped-usage payload (`series`, `buckets`,
`bucket_totals`, `points`). All other stats endpoints inherit the
dashboard's public/auth setting.

### Observability surfaces

- **Attempt analytics** (`/api/stats/attempts`, `/api/stats/retries`): per-attempt
  aggregates including latency percentiles, byte totals, retry rate, and the
  `retry_category` distribution (quota_exceeded, transient, auth_failure,
  etc.). Useful for distinguishing "did the retry fix it?" from "is the
  retry happening too late?"
- **Routing analytics** (`/api/stats/routing`, `/api/stats/routing-selections`,
  `/api/stats/routing-exclusions`): per-`(model, provider)` decision aggregates,
  account-level selection counts, and per-`(account, reason)` exclusion
  counts parsed from the `exclude_reasons_json` array.
- **Latency phases** (key `phases` in `/api/stats/latency`): decomposes each
  request into `upstream_connect_ms` (DNS/TCP/TLS/send), `upstream_read_ms`
  (TTFB minus connect), and `coordinator_overhead_ms` (routing, retry math,
  DB writes, JSON encode, FastAPI plumbing). Lets you tell whether slowness
  is in the network, the upstream, or EggPool itself.
- **Operational health** (`/api/stats/operational`): summary and recent rows
  for the `crash_recovery`, `stale_request_finalizer`, and
  `reservation_reconcile` safety-net events. If you see "Crash recovery:
  marked N stale requests" in logs, this endpoint reflects what was caught.
- **Pending health** (`/api/stats/pending-health`): instantaneous snapshot of
  pending request count, oldest pending age, stale-pending count (>15 min),
  active reservations, total reserved microdollars, and oldest reservation
  age. Powers the Overview System Health row and the Reliability page.
  Auth-gated.
- **Per-request trace** (`/api/stats/recent/{request_id}`): parent request row,
  full attempt chain, and per-attempt routing decisions. Returns account name,
  model, protocol, status, error class (never raw error_detail), and timing.
  Auth-gated.
- **Recent request metadata** (`/api/stats/recent-requests`): bounded list
  of recent request rows with metadata only (no body, no auth headers,
  no error_detail). Auth-gated.
- **Cost/cache/reasoning exactness** (extended fields on `/api/stats/accounts`
  and `/api/stats/models`): per-account and per-model `exact_count`,
  `partial_count`, `derived_count`, `estimated_count`, `cache_read_ratio`,
  `cache_write_ratio`, `reasoning_output_ratio`, `estimated_cost_fraction`,
  `avg_cost_per_request`, `avg_cost_per_1k_tokens`. Lets you see which
  accounts/models/providers report exact usage versus partially-priced
  versus locally estimated cost.
- **Pricing provenance** (`/api/stats/pricing-provenance`): per-`(model,
  provider)` snapshot breakdown of `source_detail` (operator override vs.
  upstream metadata vs. OpenRouter catalog vs. curated alias) and
  `source_confidence` (exact external ID vs. curated alias vs. unknown).
  Used by the dashboard to render the cost-exactness badge and the
  high-spend estimated warning.
- **Update checker** (`/api/stats/update`): the server checks PyPI at startup
  and approximately every 24 hours for newer eggpool releases. When an update
  is found, the dashboard footer shows a non-intrusive indicator with the
  current and latest versions and a copyable `eggpool update` command. The
  JSON snapshot is also available at `GET /api/stats/update`.

## Configuration

Configuration uses a single TOML file. API keys are loaded from environment variables.

See `config.example.toml` for all available options.

### Key Sections

- `[server]` — Bind address, port (default 11300), API key, logging, `threads` (Granian event-loop threads; default 1, max 64)
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

### Local Quota Mode (Upstream-Authoritative Suppression)

By default, EggPool uses **upstream-authoritative suppression** for routing
eligibility. Local cost estimates are advisory — they influence which account
is preferred but cannot, by themselves, exclude an account from routing. Only
upstream-observed failures (429, 402, 5xx, auth) and explicit operator
disablement can suppress an account.

This is the safe default for subscription aggregators: a stretched local
estimate cannot brick routing. To restore the legacy behavior where locally
over-quota accounts are hard-excluded, opt in via `[routing]`:

```toml
[routing]
local_quota_mode = "score_only"   # default; advisory only
# local_quota_mode = "hard_cap"   # opt-in; local over-quota excludes accounts
```

When `hard_cap` is enabled, a warning is logged at startup because it can
intentionally produce local 503s under estimate drift. See
`plans/upstream-authoritative-suppression.md` for the full design.

Active upstream-derived backoffs persist across restarts in the
`account_backoffs` table. The `/api/backoffs` endpoint and the dashboard
accounts table expose the current state during incidents.

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

### Low-wear metrics buffering

EggPool buffers lossy analytics writes (timeseries, bandwidth, token/cost aggregates) in memory and flushes them periodically to reduce microSD wear. Correctness-critical state (requests, reservations, routing) is never buffered.

Three write modes are available:

- **`immediate`** (default for debugging): existing direct-write behavior.
- **`balanced`** (default): buffers analytics with 30s flush intervals.
- **`low_wear`**: 120s flush intervals, coarser 300s buckets, 5% trace sampling, aggregate-only mode — designed for microSD / Raspberry Pi.

Example low-wear configuration:

```toml
[metrics]
write_mode = "low_wear"
flush_interval_s = 120
timeseries_bucket_s = 300
trace_sample_rate = 0.05
aggregate_only = true
```

Buffered analytics may lose at most `flush_interval_s` seconds of data after abrupt power loss. For sustained multi-session use on flash media, a high-endurance microSD or USB SSD is recommended.

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

See `docs/deployment.md` for full deployment instructions.

### Quick start (personal use)

```bash
curl -fsSL https://raw.githubusercontent.com/eggstack/eggpool/main/scripts/install.sh | bash
eggpool onboard
sudo env "PATH=$PATH" "$(command -v eggpool)" deploy systemd --install
```

The systemd unit runs as the invoking user (resolved from `SUDO_USER`
under sudo), not as root. The `--install` flag writes the unit,
enables the service, and starts it — all in one command. It detects
your install method and config paths automatically.

For systems without systemd, use the cron watchdog instead:

```bash
eggpool deploy cron --install
```

`eggpool deploy cron` is the **watchdog** (not the backup); backups
live under `eggpool deploy backup-cron`.

### Configuration path resolution

Every CLI command resolves `--config` against this precedence (single
source of truth: `eggpool.deploy_user.resolve_config_path()`):

1. `--config PATH` (highest)
2. `$EGGPOOL_CONFIG` environment variable
3. `~/.config/eggpool/config.toml` (XDG default for installed copies)
4. `./config.toml` (CWD fallback for source checkouts)

After install the print-out always names the resolved path, so you can
drop `--config` once you've exported `$EGGPOOL_CONFIG` in your shell rc.

### Filesystem Layout (Personal Use)

For personal installations the default locations follow the XDG Base
Directory specification (overridable via `$XDG_CONFIG_HOME`,
`$XDG_DATA_HOME`, `$XDG_STATE_HOME`):

```
~/.config/eggpool/
├── config.toml          # Main configuration
└── .env                 # Environment variables (optional)

~/.local/share/eggpool/
└── usage.sqlite3        # SQLite database (+ -wal / -shm)

~/.local/state/eggpool/
├── eggpool.pid          # Live PID (owner: supervisor)
├── eggpool.log          # Daemon log (serve --daemon default destination)
└── cron.log             # Watchdog cron log
```

The path resolvers (`default_config_dir()`, `default_data_dir()`,
`default_state_dir()`, `default_config_path()`, `default_env_path()`)
live in `src/eggpool/deploy_user.py` and are the single source of truth.

### Production (separate user, hardened)

For public-facing deployments, see the Production Deployment section
in `docs/deployment.md`. `eggpool deploy systemd --install --production`
automates the full system layout (dedicated `eggpool` user, `/etc/eggpool`
config dir, `/var/lib/eggpool` data dir, hardened systemd unit).
Without `--production` the systemd installer runs as your personal user.

### Configuration changes

Configuration changes require a service restart; the unit
intentionally does not advertise any reload action:

```bash
sudo systemctl restart eggpool
sudo systemctl status eggpool
sudo journalctl -u eggpool -n 100 --no-pager
```

### Watchdog cron

For systems without systemd, install the watchdog (not the backup)
into the invoking user's crontab:

```bash
eggpool deploy cron --install               # default 5-minute interval
eggpool deploy cron --install --interval 10 # change the poll cadence
eggpool deploy cron --uninstall             # remove the BEGIN/END-marked block
```

The generated block uses absolute binary, config, and log paths so it
does not depend on cron PATH. `ensure-running` is the stdlib-only
fast-path CLI and does not import the heavy application graph on every
cron tick, so a 5-minute cadence is cheap on Raspberry Pi-class
hardware. Backups are a separate command: `eggpool deploy backup-cron --install`.

### Cleanup

```bash
eggpool uninstall --yes                       # binary, config, data, PATH
eggpool uninstall --yes --deploy-artifacts    # also systemd, logrotate, cron
```

`--deploy-artifacts` walks the install method (pipx / uv tool /
source / manual), previews PATH edits before writing them, and only
removes system-level deploy artifacts you confirm: the systemd unit,
the logrotate config, the watchdog and backup cron blocks, and the
personal backup script.

### Process model

`eggpool serve` runs as a single supervisor process that launches a
single Granian worker (`workers=1`) under the same canonical process
name `eggpool`. You will see two `eggpool` entries in `ps` / `top` /
`pgrep`: the supervisor and the worker. The supervisor owns the PID
file; the FastAPI lifespan does not.

The PID file path is resolved by `eggpool.runtime_paths.default_pid_file()` in this order:

1. `$EGGPOOL_PID_FILE` (if set)
2. `$XDG_RUNTIME_DIR/eggpool.pid` (if `XDG_RUNTIME_DIR` is set)
3. `~/.local/state/eggpool/eggpool.pid` (parent auto-created)
4. `/tmp/eggpool-<UID>.pid` UID-scoped fallback

This is a behavior change for installations that previously wrote to
`/tmp/eggpool.pid` without `XDG_RUNTIME_DIR` set; those will now write
to `~/.local/state/eggpool/eggpool.pid` (when the state dir is
writable) or to `/tmp/eggpool-<UID>.pid`.

`eggpool serve` refuses to start a second instance: it first checks
the PID file and then probes `GET /v1/healthz` over `127.0.0.1`. A
running instance (live PID or 200 from the probe) causes the new
`serve` to exit non-zero so a stale PID file is never overwritten
silently. Stale PID files (PID not running) are cleared automatically
before startup.

Cron watchdog entries should call `eggpool ensure-running`, which
atomically checks-and-starts without ever spawning a duplicate
instance. `croncheck` remains available as a pure status probe for
monitoring and scripts.

For low-resource devices, the Granian worker's event-loop thread
count is the primary tuning knob. The default is one thread, which
keeps the Pi footprint to one supervisor process plus one worker
process plus one event-loop thread. Raise it on capable hardware:

```toml
[server]
threads = 4
```

The worker is named `eggpool` in `ps` / `top` (via Granian's
`process_name`), so it does not appear as a generic `python` entry.

### Runtime Diagnostics

`eggpool runtime-status` calls the local `/api/stats/runtime` endpoint and
prints a compact terminal summary of process topology, memory usage,
background task health, database file/WAL sizes, and in-flight request
counts. Use it to diagnose daemon/systemd/cron deployments without
inspecting logs.

The `/api/stats/runtime` endpoint and the `/runtime` dashboard page expose
the same data. Both are always auth-gated regardless of `dashboard.public`
because they reveal operational details (PID, memory, DB path, process
count). The endpoint is best-effort: probes that fail on a given platform
(e.g., `/proc` on macOS) return `null` for the affected field.

See [CHANGELOG](CHANGELOG.md) for release history.
