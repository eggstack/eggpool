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
- Per-account outbound proxy support ([pproxy](https://pypi.org/project/pproxy/) — SOCKS5, HTTP, Shadowsocks)
- Tracks requests, tokens, latency, errors, and cost provenance in SQLite (`provider_reported`, trusted local `derived`/`partial`, bounded `estimated`)
- Multi-page dashboard with 50+ themes, reliability, routing, and runtime views
- Model metadata enrichment from provider catalogs, OpenRouter, Artificial Analysis, and Hugging Face
- Provider-neutral cache observability and a cache-preserving compression stack (segmentation, transcoder cache stability, observe/safe suffix compression, per-policy overrides, optional synthetic cache controls, closed-loop threshold tuning)
- Thinking/reasoning capability-aware routing with configurable budget mapping
- Designed for lightweight deployments (Raspberry Pi, SBCs)

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
| `eggpool stop` | Stop the running server |
| `eggpool restart` | Fully restart the server (stop then start) |
| `eggpool rehash` | Restart to apply config changes |
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
| `eggpool update` | Check for and install updates |
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
| `[models]` | Catalog refresh, exposure mode, model collapse, withdrawal policy |
| `[routing]` | Routing strategy, retry limits, quota mode, same-tier fairness |
| `[dashboard]` | Dashboard toggle, theme, refresh interval |
| `[providers.*]` | Provider configs with accounts and routing priority |
| `[network]` | Outbound transport, DNS cache |
| `[model_info]` | Optional model metadata refresh, aliases, overrides, and external source settings |
| `[transcoder]` | Protocol transcoding between OpenAI and Anthropic formats |
| `[compression]` | Compression mode (`observe`/`safe`), per-policy overrides, tuning bounds |
| `[cache]` | Synthetic cache controls (post-route, dry-run by default) |

The catalog refresh is **non-destructive by default**: failed, empty, or partial upstream responses never silently de-pool a healthy account. Set `[models].catalog_withdrawal_policy` (`preserve_until_health` default, `confirmed_once`, `confirmed_twice`) to opt into destructive behavior on authoritative refreshes. See `architecture/README.md` § Catalog Refresh Semantics.

Full config reference: [`config.example.toml`](config.example.toml) | [docs/providers.md](docs/providers.md)

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

Phase 6 feature flags (`[transcoder.features]`) — all **off** by default:

- `tools` — bidirectional tool calling translation
- `vision` — image/document content parts
- `thinking` — extended thinking ↔ reasoning_content
- `structured_outputs` — `response_format` / `json_schema` coercion
- `anthropic_primitives` — `top_k`, `cache_control`, `context_management`, `container`, `mcp_servers`

See [docs/transcoding.md](docs/transcoding.md) for the full translation table and known limitations.

## Cache observability & safe suffix compression

EggPool ships a cache-preserving compression stack that is **observational and routing-isolated by default**. Routing is load-based (request count + token count + active count + health); no cache, compression, synthetic-cache, or tuning field is ever consumed by `QuotaFairScorer`. A hardcoded runtime diagnostic (`GET /api/stats/runtime.guardrails`) and `tests/unit/test_routing_guardrails.py` pin the invariant.

The stack exposes the following surfaces:

- **Cache observability** — every finalized request records a `cache_counter_status` of `reported` / `not_reported` / `unknown_format` plus parsed cache-token counts. Dashboard: "Runtime → Cache observability"; JSON: `GET /api/stats/cache-observability`.
- **Canonical request segmentation** — every finalized request is annotated into `stable_prefix` / `semi_stable_context` / `volatile_suffix` regions without mutating the payload, with concrete JSON `content_path` values that resolve to actual string leaves. Dashboard: "Runtime → Segmentation"; JSON: `GET /api/stats/canonical-request-segmentation`.
- **Transcoder cache stability** — cross-protocol requests carry a `cache_boundary_tracker` recording whether `cache_control` annotations were preserved, relocated, or dropped, plus a deterministic SHA-256 of the provider-visible stable prefix. JSON: `GET /api/stats/cache-stability`.
- **Safe suffix compression** — when `[compression] mode = "safe"`, six deterministic transforms (`fold_repeated_lines`, `compact_logs`, `compact_search_results`, `compact_stack_traces`, `elide_base64_blobs`, `minify_machine_json`) fold eligible `volatile_suffix` content. `stable_prefix` is recomputed SHA-256 verified after every transform; any mismatch degrades to the original payload with `failed_fallback=True`. Default mode is `observe` (reporting only). Dashboard: "Runtime → Compression"; JSON: `GET /api/stats/compression-observability`, `/api/stats/compression-runtime`.
- **Per-policy overrides** — `[[compression.policies]]` rows target clients, protocols, models, or transcoding paths without changing the global config. Dashboard: "Runtime → Compression policy"; JSON: `GET /api/stats/compression-policies`.
- **Synthetic cache controls** — opt-in post-route `cache_control` annotations for providers that support explicit cache boundaries (Anthropic-style). Disabled by default; dry-run by default. Stable-prefix only; native `cache_control` preserved byte-for-byte; only `ttl = "ephemeral"` is currently accepted.
- **Closed-loop threshold tuning** — advisory recommendations for `min_candidate_tokens`, `min_savings_tokens`, `max_compression_latency_ms`. `mode = "recommend"` writes recommendations; `mode = "apply"` is accepted at config but does not currently register runtime overrides.

Headers: `x-eggpool-compression: off|observe|safe` (when `header_override = true`) and `x-eggpool-cache-policy: preserve` to opt out per request.

Privacy invariants: no raw prompt, tool output, system message, request body, auth header, or provider API key is ever shown or persisted in any cache, compression, or synthetic-cache surface.

Full operator guide: [docs/cache-compression.md](docs/cache-compression.md). Copy-pasteable profiles: [docs/cache-compression-profiles.md](docs/cache-compression-profiles.md). Symptom-to-cause troubleshooting: [docs/cache-compression-troubleshooting.md](docs/cache-compression-troubleshooting.md).

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
| `GET` | `/api/stats/cache-observability` | Cache counter status coverage |
| `GET` | `/api/stats/canonical-request-segmentation` | Segmentation status and per-region token estimates |
| `GET` | `/api/stats/cache-stability` | Transcoder cache boundary tracker counters |
| `GET` | `/api/stats/compression-observability` | Observe-mode opportunity, per-policy roll-ups |
| `GET` | `/api/stats/compression-runtime` | Safe-mode applied/fallback counts and latency |
| `GET` | `/api/stats/compression-policies` | Per-policy roll-up table |
| `GET` | `/api/stats/compression-tuning` | Threshold tuning recommendations |
| `GET` | `/api/stats/runtime` | Runtime metrics + hardcoded routing guardrails |

When `[dashboard].enabled = true`, a multi-page dashboard is served at `/` with request stats, latency metrics, provider health, model-info detail pages, and more. Stats API available under `/api/stats/*`.

### Model-info observability

- The dashboard `/models` page renders a degraded-state notice above the table if the model-info service is unattached (no `app.state.model_info`) or if `get_summary_map()` raises an exception. The exception's full traceback is logged under `eggpool.dashboard.routes` — the page never embeds the traceback text in HTML.
- The dashboard `/models/{model_id:path}` detail page distinguishes "no canonical row exists" (empty-state copy) from "lookup failed" (degraded-state notice) when the service throws.
- `/api/stats/runtime` includes a `model_info` section with `enabled`, `canonical_count`, `catalog_model_count`, `provider_model_count`, `due_count`, and a `source_health` dict (no raw source payloads). Failures surface as `*_error` keys and `probe_errors`, never as raised exceptions.

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
| Cache & compression operator guide | [docs/cache-compression.md](docs/cache-compression.md) |
| Cache & compression profiles | [docs/cache-compression-profiles.md](docs/cache-compression-profiles.md) |
| Cache & compression troubleshooting | [docs/cache-compression-troubleshooting.md](docs/cache-compression-troubleshooting.md) |
| Thinking & reasoning | [docs/thinking.md](docs/thinking.md) |
| Architecture overview | [architecture/README.md](architecture/README.md) |

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