[![PyPI version](https://badge.fury.io/py/eggpool.svg)](https://pypi.org/project/eggpool/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![CI](https://github.com/eggstack/eggpool/actions/workflows/ci.yml/badge.svg)](https://github.com/eggstack/eggpool/actions/workflows/ci.yml)

# EggPool

A lightweight, LAN-hosted proxy that aggregates multiple AI provider accounts behind one OpenAI/Anthropic-compatible endpoint.

## Features

- Proxies model requests across multiple providers and accounts behind a single endpoint
- OpenAI- and Anthropic-compatible upstream request paths, with transparent bidirectional protocol transcoding
- Dynamically discovers available models; routes by quota utilization (load-based, never cost-based)
- Optional per-account outbound proxy support ([pproxy](https://pypi.org/project/pproxy/) — install with `uv sync --extra proxy`; SOCKS5, HTTP, Shadowsocks)
- Tracks requests, tokens, latency, errors, and cost provenance in SQLite (`provider_reported`, trusted local `derived`/`partial`, bounded `estimated`; reservation is advisory, not a floor)
- Multi-page dashboard with 50+ themes, reliability, routing, and runtime views
- Model metadata enrichment from provider catalogs, OpenRouter, Artificial Analysis, and Hugging Face
- Provider-neutral request shaping: cache reporting, safe suffix compression, policy-scoped overrides, optional synthetic cache controls, and advisory threshold tuning
- Single-decode provider payload lifecycle: selected-provider transforms share one immutable client snapshot, generation-aware provider payload, and final serialization cache
- Thinking/reasoning capability-aware routing with configurable budget mapping
- High-concurrency stream stability: bounded terminal-finalization supervision, lock-contention diagnostics, and an OpenCode-specific operator playbook for sustained coding-agent streaming loads
- Protocol-aware stream completion: clean EOF is classified from upstream `[DONE]`/`message_stop` evidence; truncated streams are never recorded as successful
- Isolated upstream dispatch: only typed HTTPX transport failures retry across distinct accounts before response handoff; local preparation and response-adaptation faults terminate safely without provider penalties
- Dispatch timing: distinct `local_pre_upstream` (full EggPool-side) and `dispatch_overhead` (coordinator-internal) metrics with cadence drift diagnostics for background tasks
- Selection hot path: generation-hydrated account identities keep SQLite outside the claim lock, while provisional request/token load is visible to later scorers before persistence and routing plans carry quarantine exclusions directly into sampled diagnostic traces
- Durable dispatch write pipeline: process-owned microbatching writer for dispatch intents with bounded queue, adaptive batching, binary success/exception persistence semantics, durable-identity validation, and diagnostics
- Amortized quota-window maintenance: ordered observations expire incrementally, while rare out-of-order timestamps remain correct through a bounded slow path
- Bounded observability: coarse metrics by default, opt-in request-coherent spans, bounded rolling-window metrics, and constant-bounded snapshot cost regardless of uptime
- Error isolation: provider-specific validation errors (e.g. unsupported MiniMax-M3 thinking through OpenCode Go) are contained to a single request — no account/model/circuit/quarantine penalties, no restart or database deletion required
- Attempt-scoped failure decisions: one bounded canonical decision carries retry, provider evidence, health/quarantine, circuit, backoff, and probe-convergence effects; retained attempt ownership prevents identical failures from colliding or replaying
- Generation-owned terminal ownership: selected request finalization, failed-attempt cleanup, and post-commit claim compensation are reconciled by one bounded supervisor with kind-qualified identities, typed progress, one global capacity, one retry timer, and one drain; the owning generation retains one terminal reference per accepted command until durable and required runtime convergence
- Truthful stream handoff: `downstream_started` is marked when the proxy forwards ASGI `http.response.start`, before body iteration; an empty started stream is post-handoff with `bytes_emitted = 0`, and cannot be retried
- Restart-safe crash recovery: startup reconciliation repairs durable requests and reservations left by a prior process; normal runtime cleanup remains owned by the selected attempt
- Database recovery: fail-closed startup integrity and restart-safe crash reconciliation; an indeterminate runtime SQLite state closes admission and lets systemd restart the worker
- Bounded model quarantine: TTL-based suspected/quarantined state with corroboration thresholds and automatic recovery
- Self-healing provider health: every nonterminal account/model suppression is capped at 30 minutes, durable hints are bounded during hydration, and half-open probes always converge
- Designed for lightweight deployments (Raspberry Pi, SBCs)

The ordinary install is already a lightweight local profile: it binds to
loopback, uses one SQLite worker, low-wear analytics buffering, 16 provider
connections with 4 keepalives, and no model-info, routing-trace, readiness,
backup, DNS-cache, or background PyPI task. `eggpool onboard` asks whether to
bind to the LAN; noninteractive onboarding keeps loopback. Optional features
remain explicit configuration choices.

For a copyable SBC profile with an explicit LAN bind and provider discovery
cadence, see [config.sbc.example.toml](config.sbc.example.toml).

Resource behavior is intentionally profile-driven rather than governed by a
universal RSS threshold. Use `eggpool runtime-status --json` after startup to
inspect the bounded task inventory, local dispatch timings, database/WAL
state, and generation-retirement ownership on the target host.

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
| `eggpool serve` | Start the proxy server (daemon mode; `--verbose` for foreground) |
| `eggpool stop` | Stop the running server |
| `eggpool restart` | Fully restart the server (stop then start) |
| `eggpool rehash` | Apply supported config changes live (provider/account/routing/model-override changes apply without restart; `--json` for standardized 9-key output; D3 release validation enforces redaction of secret-shaped strings in event payloads and CLI output) |
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
| `eggpool runtime-status` | Print runtime health summary |
| `eggpool backup` | Create a timestamped backup |
| `eggpool recover` | Restore from a backup archive |
| `eggpool deploy systemd` | Print/install systemd unit |
| `eggpool deploy cron` | Install watchdog cron (non-systemd) |
| `eggpool deploy backup-cron` | Install daily backup cron job |
| `eggpool deploy logrotate` | Print/install logrotate config |
| `eggpool deploy all` | Print every deployment snippet in sequence |
| `eggpool update [VERSION]` | Check for latest, or install an exact PyPI release (`v` prefix accepted) |
| `eggpool uninstall` | Uninstall EggPool from this machine |

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
| `[dispatch_writer]` | Optional process-owned durable dispatch writer |
| `[models]` | Catalog refresh, exposure mode, model collapse, withdrawal policy |
| `[routing]` | Routing strategy, retry limits, quota mode, same-tier fairness |
| `[dashboard]` | Dashboard toggle, theme, refresh interval |
| `[providers.*]` | Provider configs with accounts and routing priority |
| `[network]` | Outbound transport, DNS cache |
| `[model_info]` | Optional model metadata refresh, aliases, overrides, and external source settings |
| `[update_checker]` | Optional in-process PyPI release probe for dashboard status |
| `[transcoder]` | Protocol transcoding between OpenAI and Anthropic formats |

Fatal SQLite uncertainty closes the worker; the supervisor restarts it and
startup reconciliation repairs durable leftovers.
| `[compression]` | Request shaping: `observe`/`safe`, stable thresholds, transform toggles, advanced policy overrides |
| `[cache]` | Synthetic cache controls (post-route, disabled by default, dry-run first) |
| `[maintenance]` | Bounded maintenance budget, SQLite hygiene, contention guard |

The catalog refresh is **non-destructive by default**: failed, empty, or partial upstream responses never silently de-pool a healthy account. Set `[models].catalog_withdrawal_policy` (`preserve_until_health` default, `confirmed_once`, `confirmed_twice`) to opt into destructive behavior on authoritative refreshes. See `architecture/README.md` § Catalog Refresh Semantics.

The default five-minute discovery cadence is not a five-minute full catalog
rewrite. Semantic model/provider rows are updated only when their metadata or
support relationships change; successful refresh freshness is kept in compact
per-account state. Diagnostic ping failures and success/failure transitions are
durable immediately, while steady successful latency samples are retained at a
coarse internal cadence.

Full config reference: [`config.example.toml`](config.example.toml) | [docs/providers.md](docs/providers.md)

Account `weight` is a positive, relative capacity/share hint within an
eligible priority tier. `1.0` is the baseline; `2.0` approximately doubles an
account's effective request/token capacity and `0.5` approximately halves it.
Weight is load-based (never cost-based), does not override priority or health
eligibility, and does not promise exact request ratios when request sizes or
provider capacity histories differ. See [Provider configuration](docs/providers.md#account-weight).

## Protocol transcoding

When `[transcoder] enabled = true`, EggPool bridges OpenAI Chat Completions and Anthropic Messages bidirectionally so a single client ecosystem (e.g. OpenCode, which speaks only OpenAI) can reach Anthropic-only upstreams and vice versa.

What gets translated:

- Request bodies (text + tool-use + vision + thinking + structured outputs)
- Streaming SSE events (including tool-call deltas and thinking deltas)
- Non-retryable error envelopes
- Usage and cost fields (preserved exactly as the upstream reported them)

What is dropped with a structured warning log:

- OpenAI fields with no Anthropic equivalent (`logit_bias`, `presence_penalty`, `top_logprobs`, etc.)
- Anthropic fields with no OpenAI equivalent (`top_k`, `cache_control`)

Feature flags (`[transcoder.features]`) — all **off** by default:

- `tools` — bidirectional tool calling translation
- `vision` — image/document content parts
- `thinking` — extended thinking ↔ reasoning_content
- `structured_outputs` — `response_format` / `json_schema` coercion
- `anthropic_primitives` — `top_k`, `cache_control`, `context_management`, `container`, `mcp_servers`

The streaming hot path is optimised for sustained concurrent coding-agent loads. A single bounded SSE decoder feeds completion tracking, usage extraction, and frame-level translation; shared frames lazily cache JSON parsing, while native pass-through avoids translation work. See [docs/transcoding.md](docs/transcoding.md) for the full translation table, known limitations, and streaming performance notes.

Streaming completion is determined by the upstream protocol, not by the absence
of a transport exception. OpenAI streams require `data: [DONE]` and Anthropic
streams require `event: message_stop`. Provider-specific markerless behavior,
when needed, is configured with `[providers.<id>].stream_completion_policy`;
the default is `strict`. EOF classified inside the handed-off stream iterator
is terminal for the selected attempt: premature EOF is never retried after
response handoff at ASGI `http.response.start`, including when no downstream
body byte has arrived yet.
Terminal finalization carries this response-lifecycle handoff fact explicitly;
`bytes_emitted` remains payload accounting and never decides whether response
status or headers can still change. Retained runtime leases independently
converge usage, health, and account-runtime outcomes, including when durable
finalization observes an already-terminal request.

Non-streaming responses are adapted before a request can be durably marked
`COMPLETED`. Native protocol responses retain bounded pass-through behavior,
including non-JSON bodies where usage extraction is optional; required
transcoded responses that cannot be adapted become truthful local errors.

Provider health suppression is bounded to 1,800 seconds for quota, rate-limit,
transport, server, protocol, and runtime model-absence observations. Successful
traffic and expiry restore only the matching transient account/model state.
Authentication failures and authoritative catalog withdrawals remain terminal;
corrected credentials during validated `eggpool rehash`, explicit operator
enable/reset, or authoritative model reappearance are their recovery paths.
Rehash clears only the changed account's auth state. `/api/backoffs` exposes
the active provider-derived hints; malformed or stale durable rows have no
routing effect.

Model-quarantine hydration is fail-closed. A successful empty durable read is
distinct from a read or row-conversion failure: startup remains unready and a
failed rehash candidate is rejected rather than publishing unknown quarantine
state. Authoritative catalog reappearance clears the exact durable
provider/account/model/upstream/protocol identity before in-memory suppression
and matching backoff; a durable failure preserves the current suppression.

## Request shaping

EggPool includes an opt-in, cache-preserving request-shaping stack. With shipped defaults the entire stack is **reporting-only**: no request body, header, or route is altered. Routing remains load-based — cache, compression, synthetic-cache, and tuning fields never enter `QuotaFairScorer`.

The stack covers provider cache counters, request segmentation, native cache preservation, optional compression (observe → safe), optional synthetic cache annotations, and advisory tuning. The dashboard `/cache` page provides operator summary cards and drill-down tables; advanced diagnostics stay collapsed unless a warning is present.

| Operator guide | [docs/cache-compression.md](docs/cache-compression.md) |
|---|---|
| Copy-pasteable profiles | [docs/cache-compression-profiles.md](docs/cache-compression-profiles.md) |
| Troubleshooting | [docs/cache-compression-troubleshooting.md](docs/cache-compression-troubleshooting.md) |
| Architecture | [architecture/README.md](architecture/README.md) |

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
| `GET` | `/api/model-info/sources` | Model-info source health and diagnostics per source |
| `POST` | `/api/model-info/refresh` | Trigger model-info refresh (auth-gated; supports `?model_id=&source=&force=1`) |
| `GET` | `/api/stats/cache-observability` | Cache counter status coverage |
| `GET` | `/api/stats/canonical-request-segmentation` | Segmentation status, not_collected / empty_request / parse_failure counts, and token estimates |
| `GET` | `/api/stats/cache-stability` | Transcoder cache boundary tracker counters |
| `GET` | `/api/stats/compression-observability` | Observe-mode opportunity, per-policy roll-ups |
| `GET` | `/api/stats/compression-runtime` | Safe-mode applied/fallback counts and latency |
| `GET` | `/api/stats/compression-policies` | Per-policy roll-up table |
| `GET` | `/api/stats/synthetic-cache-observability` | Synthetic cache candidate / applied / native-preserved counts |
| `GET` | `/api/stats/compression-tuning` | Threshold tuning recommendations |
| `GET` | `/api/stats/request-shaping` | Operator-facing request-shaping summary |
| `GET` | `/api/stats/runtime` | Runtime metrics, routing guardrails, background task summaries, stream diagnostics, and the bounded `finalization_supervisor` snapshot |
| `GET` | `/api/stats/summary` | Aggregate request stats (counts, tokens, cost, latency) |
| `GET` | `/api/stats/thinking` | Thinking/reasoning decision counter snapshot |
| `GET` | `/api/stats/update` | PyPI update check status |
| `GET` | `/api/stats/routing/eligibility` | Per-account routing eligibility diagnostics |
| `GET` | `/api/events` | Operational event log |
| `GET` | `/api/model-info/{model_id}/matches` | Match evidence diagnostics for one model |
| `GET` | `/api/network/diagnostics` | Network and DNS diagnostics |

When `[dashboard].enabled = true`, a multi-page dashboard is served at `/` with request stats, latency metrics, provider health, model-info detail pages, and more. Stats API available under `/api/stats/*`.

### Model-info observability

The dashboard `/models` page shows enriched model metadata from provider catalogs, OpenRouter, Artificial Analysis, and Hugging Face. It surfaces degraded-state notices when the model-info service is unavailable, and join-failure diagnostics when catalog rows don't match canonical lookups. The `/api/stats/runtime` endpoint includes a `model_info` section with source health. See [docs/model-info-openrouter-debug.md](docs/model-info-openrouter-debug.md) for the live verification flow.

## Documentation

| Topic | Link |
|-------|------|
| Deployment (install, systemd, production) | [docs/deployment.md](docs/deployment.md) |
| Provider catalog & configuration | [docs/providers.md](docs/providers.md) |
| Backup & restore | [docs/backup-restore.md](docs/backup-restore.md) |
| Release procedure | [docs/releasing.md](docs/releasing.md) |
| Per-account outbound proxy | [docs/proxy.md](docs/proxy.md) |
| Model context limits | [docs/model-limits.md](docs/model-limits.md) |
| Raspberry Pi setup | [docs/raspberry-pi.md](docs/raspberry-pi.md) |
| Copyable SBC configuration | [config.sbc.example.toml](config.sbc.example.toml) |
| Firewall configuration | [docs/firewall.md](docs/firewall.md) |
| Filesystem layout | [docs/filesystem-layout.md](docs/filesystem-layout.md) |
| Network & DNS diagnostics | [docs/network-diagnostics.md](docs/network-diagnostics.md) |
| Protocol transcoding | [docs/transcoding.md](docs/transcoding.md) |
| Cache & compression operator guide | [docs/cache-compression.md](docs/cache-compression.md) |
| Cache & compression profiles | [docs/cache-compression-profiles.md](docs/cache-compression-profiles.md) |
| Cache & compression troubleshooting | [docs/cache-compression-troubleshooting.md](docs/cache-compression-troubleshooting.md) |
| OpenCode stream stability | [docs/opencode-stream-stability.md](docs/opencode-stream-stability.md) |
| Model-info OpenRouter debugging | [docs/model-info-openrouter-debug.md](docs/model-info-openrouter-debug.md) |
| Thinking & reasoning | [docs/thinking.md](docs/thinking.md) |
| Architecture overview | [architecture/README.md](architecture/README.md) |
| Live Configuration Rehash | [docs/live-config-rehash.md](docs/live-config-rehash.md) |

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

## Agent Configuration

`eggpool configsetup` generates configuration snippets for popular coding agents:

| Target | Command | Output | `--write` default | Model |
|--------|---------|--------|-------------------|-------|
| OpenCode | `eggpool configsetup opencode` | JSON provider config | N/A (clipboard) | auto |
| Claude Code | `eggpool configsetup claude-code` | JSON snippet | N/A (clipboard) | N/A |
| Aider | `eggpool configsetup aider` | Shell env exports | `.env.eggpool` | recommended |
| Codex | `eggpool configsetup codex` | TOML provider block | N/A (printed) | recommended |
| Qwen Code | `eggpool configsetup qwen-code` | JSON provider block | N/A (printed) | optional |
| Kilo | `eggpool configsetup kilo` | JSON provider block | N/A (printed) | optional |
| Continue | `eggpool configsetup continue` | YAML model block | `~/.continue/eggpool.yaml` | usually yes |
| Cline | `eggpool configsetup cline` | JSON profile | `cline-eggpool.json` | recommended |
| Roo Code | `eggpool configsetup roo-code` | JSON profile | `roo-eggpool.json` | recommended |
| Goose | `eggpool configsetup goose` | Shell env exports | N/A (printed) | recommended |
| OpenHands | `eggpool configsetup openhands` | Shell env exports | N/A (printed) | recommended |

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
