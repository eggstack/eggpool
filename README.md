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
- Provider-neutral cache observability — records whether upstreams report `cache_read` / `cache_creation` (Anthropic) or `prompt_tokens_details.cached_tokens` (OpenAI) and exposes a dashboard hit ratio that never silently mixes zero with missing
- Canonical request segmentation — every finalized request is annotated into `stable_prefix` / `semi_stable_context` / `volatile_suffix` regions without mutating the payload, giving later compression phases a safe way to identify cache-continuity boundaries and compressible candidates
- Transcoder cache stability — every cross-protocol request carries a bounded `cache_boundary_tracker` that records whether `cache_control` annotations were preserved, relocated, or dropped, plus deterministic SHA-256 of the provider-visible stable prefix so downstream phases can compare cache-equivalent bodies without re-parsing
- Safe suffix compression — when `[compression] mode = "safe"`, deterministic transforms fold repeated lines, compact logs/search/stack traces, elide base64 blobs, and minify machine JSON inside `volatile_suffix` regions, preserving every `stable_prefix` segment byte-for-byte (recomputed SHA-256 verified) and degrading to the original payload on any mismatch

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

## Cache observability

Every finalized request is annotated with a `cache_counter_status` of `reported`, `not_reported`, or `unknown_format`, plus the parsed cache-token counts the upstream actually surfaced. The status lets you tell apart three cases:

- **`reported`** — upstream payload included cache fields (Anthropic `cache_read_input_tokens` / `cache_creation_input_tokens`, OpenAI `prompt_tokens_details.cached_tokens`); counts are recorded.
- **`not_reported`** — payload parsed cleanly but no cache fields were present (the canonical OpenAI shape, or providers that omit the breakdown).
- **`unknown_format`** — payload could not be parsed, or returned a shape EggPool does not recognize. The cache state is ambiguous and must not be assumed to be zero.

Observability is reporting-only: `QuotaFairScorer` still routes on request count + token count + cost (audit) + active count + health, never on cache fields. The dashboard renders a coverage card under "Runtime → Cache observability" and the JSON API exposes the breakdown at `GET /api/stats/cache-observability`.

## Canonical request segmentation

Every finalized request is annotated with a `segmentation_status` of `segmented`, `empty_request`, or `parse_failure`, plus structural segments of three kinds:

- **`stable_prefix`** — system / developer prompts, tool schemas, and provider-native `cache_control` blocks. Marked `protected=True` so later phases can identify cache-continuity boundaries.
- **`semi_stable_context`** — assistant messages, prior user turns, and short follow-ups. The conservative default for ambiguous content.
- **`volatile_suffix`** — tool results, command output, search results, and the latest user turn when it carries log / command / search markers. Marked `compressible_candidate=True` so later compression phases have a candidate set without re-parsing the request.

Segmentation is observational: request bodies, route scoring, and eligibility are unchanged. The dashboard renders a coverage card under "Runtime → Segmentation" and the JSON API exposes the breakdown at `GET /api/stats/canonical-request-segmentation`.

## Safe suffix compression

When `[compression] mode = "safe"`, EggPool applies deterministic transforms to eligible `volatile_suffix` segments and re-verifies the `stable_prefix` hash on the mutated payload. The default mode is `observe` (Phase 4 — reporting only); set `mode = "safe"` to actually mutate.

Six transforms are available:

- **`fold_repeated_lines`** — replaces adjacent identical lines with a single representative plus a count marker
- **`compact_logs`** — preserves command text, exit code, first/last N lines, and diagnostic patterns (error, failed, panic, etc.) from large tool/log output
- **`compact_search_results`** — preserves file path, line number, and matched line for each retained match while collapsing duplicate matches and limiting excessive context
- **`compact_stack_traces`** — folds repeated identical stack frames with count markers while preserving the first occurrence of each unique trace shape and the final active error path
- **`elide_base64_blobs`** — replaces large opaque base64/data-URI blobs with a placeholder noting detected blob type and original size
- **`minify_machine_json`** — strips insignificant whitespace from machine-generated JSON payloads in volatile-suffix segments

**Eligibility**: only `volatile_suffix` segments; candidates must exceed `min_candidate_tokens` (default 2048) and `min_savings_tokens` (default 1024).

**Cache safety**: protected `stable_prefix` segments are never touched. Pre/post `stable_prefix_hash` (SHA-256) is recomputed over the stable-prefix segments; on any mismatch the request is sent uncompressed with a `stable_prefix_hash_mismatch` warning.

**Latency budget**: `max_compression_latency_ms` (default 25) bounds the applier budget; over-budget runs append `latency_budget_exceeded` warnings.

**Per-request headers**: `x-eggpool-compression: off|observe|safe` (when `header_override = true`) and `x-eggpool-cache-policy: preserve` to opt out for cache-equivalent flows.

**Observability**: dashboard renders under "Runtime → Compression"; JSON API at `GET /api/stats/compression-observability`. Migration 0043 adds 13 columns + 2 indexes to `requests`.

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
