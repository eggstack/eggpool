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
- Tracks requests, tokens, latency, errors, and cost provenance in SQLite (`provider_reported`, trusted local `derived`/`partial`, bounded `estimated`; reservation is advisory, not a floor)
- Multi-page dashboard with 50+ themes, reliability, routing, and runtime views
- Model metadata enrichment from provider catalogs, OpenRouter, Artificial Analysis, and Hugging Face
- Provider-neutral request shaping: cache reporting, safe suffix compression, policy-scoped overrides, optional synthetic cache controls, and advisory threshold tuning
- Thinking/reasoning capability-aware routing with configurable budget mapping
- High-concurrency stream stability: bounded retry queue, lock-contention diagnostics, and an OpenCode-specific operator playbook for sustained coding-agent streaming loads
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
| `eggpool serve` | Start the proxy server (daemon mode; `--verbose` for foreground) |
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
| `eggpool stats repair-costs` | Dry-run/apply repair for suspicious historical request costs (incl. reservation-fallback rows where canonical cost equals the inflated reservation while a smaller local estimate exists) |
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
| `[compression]` | Request shaping: `observe`/`safe`, stable thresholds, transform toggles, advanced policy overrides |
| `[cache]` | Synthetic cache controls (post-route, disabled by default, dry-run first) |

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

The streaming hot path is optimised for sustained concurrent coding-agent loads: only the coordinator runs an SSE observer (the transcoder no longer runs its own for usage extraction), `StreamingTranscoder.feed()`/`.flush()` are synchronous, translated output per upstream chunk is coalesced into a single yield, and frame helpers use compact JSON separators. See [docs/transcoding.md](docs/transcoding.md) for the full translation table, known limitations, and the streaming hot-path notes.

## Request shaping

EggPool’s request-shaping stack is opt-in and cache-preserving by default. With the shipped config, the public surface stays in reporting mode: no route changes, no stable-prefix mutation, and no synthetic cache annotations on the wire. Routing remains load-based; cache, compression, synthetic-cache, and tuning fields never enter `QuotaFairScorer`.

The detailed request-shaping summary and drill-down cards live on
`/cache`; `/runtime` keeps a compact relocation panel that points back
to that page.

The `/cache` page opens with six top-level operator summary cards:

| Summary card | Quiet default | Lights up when |
|--------------|---------------|----------------|
| Request changes | no changes | compression failed fallback > 0 |
| Provider cache counters | `—` | rows reported / classified ratio |
| EggPool cache annotations | `Off` | dry-run or applied counts or warning code count |
| Safety guardrail | `Clean` | compression/policy/annotation warnings or fallbacks |
| Tuning suggestions | `Off` | recommendation or override count > 0 |
| Routing isolation | `Isolated` | routing guardrail violation |

Raw `reporting_only` and other internal modes survive in the subtext,
not the primary metric. Advanced diagnostics stay collapsed unless a
warning or non-default state is present (segmentation parse failures,
synthetic-cache warnings or applied count, tuning recommendations,
routing guardrail violation, transcoding loss warnings, compression
warnings or policy warnings).

### Operator surfaces

| Surface | Default | Mutates requests? | Primary stats/API |
|---------|---------|-------------------|-------------------|
| Provider cache counters | on | no | `/api/stats/cache-observability` |
| Request segmentation | on | no | `/api/stats/canonical-request-segmentation` |
| Native cache preservation | on when transcoding | no | `/api/stats/cache-stability` |
| Compression | off unless `[compression].enabled = true` | no | `/api/stats/compression-observability` |
| Compression — safe-mode details | off (`mode = "observe"`) | volatile suffix only, fail-closed | `/api/stats/compression-runtime` |
| Policy overrides | off (`[[compression.policies]] = []`) | scoped overlay only | `/api/stats/compression-policies` |
| EggPool cache annotations | off (`enabled = false`, `dry_run = true`) | provider-bound stable prefix only | `/api/stats/synthetic-cache-observability` |
| Tuning suggestions | off (`enabled = false`) | no | `/api/stats/compression-tuning` |
| Request-shaping summary | on with dashboard | no | `/api/stats/request-shaping` |
| Routing isolation | always on | no | `/api/stats/runtime` |

### Stable config knobs

The default example now exposes only the normal operator knobs:

```toml
[compression]
enabled = false
mode = "observe"
min_candidate_tokens = 2048
min_savings_tokens = 1024
max_compression_latency_ms = 25.0

[compression.transforms]
fold_repeated_lines = true
compact_logs = true
compact_search_results = true
elide_base64_blobs = true
minify_machine_json = true
compact_stack_traces = true

[cache.synthetic_cache_controls]
enabled = false
dry_run = true
min_stable_tokens = 1024
```

Advanced knobs still exist for policy-scoped rollouts and advisory tuning, but they are intentionally pushed into the docs instead of the main example:

- `[[compression.policies]]` for scoped overrides
- `compression.placement`, `respect_cache_boundaries`, `header_*` for advanced routing-adjacent operator workflows
- `cache.synthetic_cache_controls.provider_kinds`, `ttl`, `max_breakpoints`, `placements`, `require_policy`
- `compression.tuning.*` for recommendation-only threshold guidance

### Safety rules

- Safe compression mutates only eligible `volatile_suffix` string leaves; no-op runs return the original payload by identity and applied runs use path-level copy-on-write (not a deep copy).
- Stable-prefix preservation is verified with `stable_prefix_content_hash`; any mismatch falls back to the original payload.
- Native provider cache annotations are preserved byte-for-byte.
- Synthetic cache controls are disabled by default and dry-run first when enabled.
- Context-limit checks happen before compression; compression never rescues an over-limit request.
- Routing stays load-based and reporting-only metrics never influence scorer inputs.

### Recommended rollout

1. Enable cache reporting and request segmentation only.
2. Run compression in `observe` mode for 24-48 hours.
3. Turn on safe compression for one client or policy.
4. Expand safe compression if fallbacks stay at zero.
5. Try synthetic cache controls in dry-run for Anthropic-compatible upstreams.
6. Move synthetic cache to apply mode only after the dry-run is clean.
7. Enable advisory tuning only if you want threshold suggestions.

### Further reading

See `/cache` for the operator workflow: requests being changed, provider cache counters, compression outcomes, safety guardrails, routing isolation. Advanced diagnostics stay collapsed unless warnings are present.

- Operator guide: [docs/cache-compression.md](docs/cache-compression.md)
- Copy-pasteable profiles: [docs/cache-compression-profiles.md](docs/cache-compression-profiles.md)
- Troubleshooting: [docs/cache-compression-troubleshooting.md](docs/cache-compression-troubleshooting.md)
- Architecture summary: [architecture/README.md](architecture/README.md)

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
| `GET` | `/api/model-info/sources` | Model-info source health — merges the live `model_info_source_health` snapshot with per-source diagnostics (`configured`, `constructed`, `requires_api_key`, `api_key_present`, `reason`) for `provider_catalog`, `openrouter`, `artificial_analysis`, and `huggingface` so operators can see exactly why a source has no `last_success_at` row |
| `POST` | `/api/model-info/refresh` | Trigger model-info refresh — `?model_id=<id>&source=<provider_catalog\|openrouter\|artificial_analysis\|huggingface>&force=1` for a single-model force refresh (auth-gated). `model_id` accepts provider-suffixed IDs (`gpt-4o/openai`); unknown source values return HTTP 400. The response carries `source_diagnostics` (`initialized`, `fetched`, `catalog_count`, `alias_candidates`, `matched_source_model_id`, `miss_reason`, `cache_retry`) so operators can see why a refresh matched or missed |
| `GET` | `/api/stats/cache-observability` | Cache counter status coverage |
| `GET` | `/api/stats/canonical-request-segmentation` | Segmentation status, not_collected / empty_request / parse_failure counts, and token estimates |
| `GET` | `/api/stats/cache-stability` | Transcoder cache boundary tracker counters |
| `GET` | `/api/stats/compression-observability` | Observe-mode opportunity, per-policy roll-ups |
| `GET` | `/api/stats/compression-runtime` | Safe-mode applied/fallback counts and latency |
| `GET` | `/api/stats/compression-policies` | Per-policy roll-up table |
| `GET` | `/api/stats/synthetic-cache-observability` | Synthetic cache candidate / applied / native-preserved counts |
| `GET` | `/api/stats/compression-tuning` | Threshold tuning recommendations |
| `GET` | `/api/stats/request-shaping` | Operator-facing request-shaping summary |
| `GET` | `/api/stats/runtime` | Runtime metrics + hardcoded routing guardrails; background task summaries expose supervisor-owned `mode`, `next_run_at`, `overdue_seconds`, plus `background_task_summary` (`registered` / `running` / `failed` / `overdue` / `never_run_not_due` / `never_run_overdue` / `last_error_count`). The two `never_run_*` counters separate tasks that have not reached their first tick from tasks that have actually missed their deadline, and each task carries a `first_run_state` label (`last_success` / `last_error` / `never_run_not_due` / `never_run_startup_deferred` / `never_run_overdue`) so a freshly started process never looks unhealthy just because the first 30- or 60-second tick has not yet fired |

When `[dashboard].enabled = true`, a multi-page dashboard is served at `/` with request stats, latency metrics, provider health, model-info detail pages, and more. Stats API available under `/api/stats/*`.

### Model-info observability

- The dashboard `/models` page renders a degraded-state notice above the table if the model-info service is unattached (no `app.state.model_info`) or if `get_summary_map()` raises an exception. The exception's full traceback is logged under `eggpool.dashboard.routes` — the page never embeds the traceback text in HTML. When the canonical summary map has rows but no rendered dashboard row matches any of them, a separate join-failure diagnostic appears (with up to five unmatched sample rows for grep-based debugging) — this catches the "API correct / dashboard empty" state caused by provider-suffixed ids not being normalized to canonical lookup keys. Catalog row construction failures (`get_provider_model_entries`, `get_models_for_exposure`) are logged with traceback and surfaced as `degraded_reason="fetch_error"` instead of silently dropping the table.
- The provider-scoped catalog accessor (`ModelCatalogCache.get_provider_model_entries()`) returns a deterministic `dict[(model_id, provider_id), dict]` view that excludes the deprecated placeholder, applies configured capability overrides when `cache._config` is attached, and emits shallow copies so mutations cannot leak back into the cache. Unresolved entries (`protocol=None`) are kept so the dashboard can render them with `available=False, catalog_status="unavailable"` rather than silently omitting them.
- The dashboard `/models/{model_id:path}` detail page distinguishes "no canonical row exists" (empty-state copy) from "lookup failed" (degraded-state notice) when the service throws.
- `/api/stats/runtime` includes a `model_info` section with `enabled`, `canonical_count`, `catalog_model_count`, `provider_model_count`, `due_count`, and a `source_health` dict (no raw source payloads). Failures surface as `*_error` keys and `probe_errors`, never as raised exceptions.
- OpenRouter source health reflects catalog availability, not local match success — a successful fetch with zero matches still updates `last_success_at` / `last_payload_count`. When a forced refresh finds configured aliases but no catalog match, the OpenRouter cache is invalidated and the fetch retried once (recorded as `cache_retry: true` under `source_diagnostics.openrouter`).
- `GET /api/model-info/{model_id}` returns `observations` rows read from `model_info_observations` (per source: `source_model_id`, `provider_id`, `observed_at`, `confidence`, display name, context window, modalities). Raw payloads are never returned. When no rows exist for a canonical model, the field is a synthetic placeholder flagged `_synthetic: true` so callers can distinguish real observation data from fallback synthesis. When the repository read fails the response returns `observations: []` plus an `observations_error: <ExcClass>` key instead of synthesising rows — operators see "no data" rather than fabricated external source ids.
- The dashboard model detail page mirrors those observation rows in an Observations panel. External `display_name_<source>` values (e.g. `MiniMax: MiniMax M3`) are promoted into `detail.display_name` only when the provider did not seed one; `detail.display_name_source` records the chosen source. Source-scoped advisory pricing (e.g. OpenRouter's `$/Mtok`) lives under `detail.pricing.<source>`, separate from authoritative local cost accounting. When the observation read fails the dashboard renders an "Observation read failed" panel with the error class name.
- Alias and observation lookups are case-insensitive at the repository layer (`lower(model_id) = lower(?)`), so provider casing drift (`MiniMax-M3` vs `minimax-m3`) does not break refresh or detail lookup. Manual refreshes reseed configured `[model_info.aliases]` before external matching, so newly added aliases apply without a process restart. Alias candidate selection is deterministic: exact-case rows win over case-folded rows; identical alias strings are deduplicated; conflicting folded aliases with no exact-case match produce an unambiguous no-match. The resolver exposes `source_diagnostics.openrouter.alias_rows` (one entry per candidate with `match_kind = "exact_case" | "case_folded"`) and `alias_selection` so operators can audit the choice.
- Deployment-suffix matching (tier 2b, opt-in via `matching.deployment_suffix_normalized_exact = true`) strips deployment-shaped tokens from `DEPLOYMENT_SUFFIX_TOKENS` (`highspeed`, `fast`, `turbo`, `speed`, `lowlatency`, `lowlat`) only when the original identifier carries a digit/family anchor, the candidate set is unique, and the original contains no semantic-variant token from `SEMANTIC_VARIANT_TOKENS` (`pro`, `mini`, `flash`, `lite`, `max`, `plus`, `instruct`, `chat`, `reasoning`, `thinking`, `preview`, `code`, `coder`, `omni`). So `MiniMax-M2.7-highspeed` resolves to `minimax/minimax-m2.7` while `claude-3-haiku-highspeed` keeps its semantic shape. Note that `preview`, `max`, and similar semantic tokens are deliberately **not** stripped even though they look like deployment markers. Artificial Analysis matching shares the same tiered resolver and the same match-evidence audit trail as OpenRouter.
- `build_canonical_detail()` credits `provenance.sources` for any `existing_detail.external_ids[*]` key that wasn't contributed this cycle, populating `provenance.source_states[<src>] = "preserved_external_id"`. Detail cycles therefore distinguish three source states explicitly: `contributed` (newly fetched this cycle), `preserved_external_id` (carried from the prior canonical row because the external ID persists in `external_ids`), and `absent` (no observation and no external ID).
- `scripts/debug_model_info_openrouter.sh` runs the live verification flow (force refresh → detail GET → source-health query) for any model id; the expected outcomes are documented in [docs/model-info-openrouter-debug.md](docs/model-info-openrouter-debug.md).

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
| OpenCode stream stability | [docs/opencode-stream-stability.md](docs/opencode-stream-stability.md) |
| Thinking & reasoning | [docs/thinking.md](docs/thinking.md) |
| Architecture overview | [architecture/README.md](architecture/README.md) |

## Development

```bash
uv sync --extra dev      # install dependencies
uv run pytest            # run all tests
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/

# Optional `orjson` backend for the JSON helper (transcoding hot paths)
uv sync --extra fast     # or: uv pip install 'eggpool[fast]'

# High-concurrency streaming reproducer (no real providers needed)
uv run python scripts/repro_high_concurrency_streams.py --concurrency 50 --cancel-rate 0.25
```

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
