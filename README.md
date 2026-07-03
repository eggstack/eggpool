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

EggPool ships a layered, opt-in cache-preserving compression stack. With shipped defaults the entire stack is observability-only — no request body, header, or route is altered. The stack is also **routing-isolated by design**: routing is load-based (request count + token count + active count + upstream health); no cache, compression, synthetic-cache, or tuning field is ever consumed by `QuotaFairScorer`. A hardcoded runtime diagnostic (`GET /api/stats/runtime.guardrails` → `guardrails` dict) and `tests/unit/test_routing_guardrails.py` pin the invariant; the regression suite asserts that `score_accounts` accepts exactly `(account_names, model_name, active_requests, request_estimates)` and that `RoutingScore` carries no cache/compression field.

### Phase map

| Phase | Layer | Default | Mutates requests? | Affects routing? |
|-------|-------|---------|--------------------|------------------|
| 1 | Cache token observability | on | no | no |
| 2 | Canonical request segmentation | on | no | no |
| 3 | Transcoder cache stability | on (when transcoding is on) | no | no |
| 4 | Observe-mode compression accounting | on when `[compression] enabled = true` | no | no |
| 5 | Safe suffix compression | off (`mode = "observe"` default) | yes — `volatile_suffix` only, fail-closed | no |
| 6 | Compression policy overrides | off (`policies = []`) | yes — overlay only | no |
| 7 | Dashboard/runtime views | on when dashboard enabled | no | no |
| 8 | Routing guardrails | always on (hardcoded diagnostic) | no | no |
| 9 | Synthetic cache controls | off (`enabled = false`, dry-run) | yes — provider-bound stable prefix only | no |
| 10 | Closed-loop threshold tuning | off (`enabled = false`, `mode = "recommend"`) | no | no |
| 11 | Replay fixtures + regression harness | test-only | no | no |
| 12 | Operator docs, profiles, rollout | docs only | no | no |

### Surfaces

- **Cache observability (Phase 1)** — every finalized request records a `cache_counter_status` of `reported` / `not_reported` / `unknown_format` plus parsed cache-token counts (`cached_input_tokens` for OpenAI, `cache_read_input_tokens` / `cache_creation_input_tokens` for Anthropic). Dashboard: "Runtime → Cache observability"; JSON: `GET /api/stats/cache-observability`.
- **Canonical request segmentation (Phase 2)** — every finalized request is annotated into `stable_prefix` / `semi_stable_context` / `volatile_suffix` regions without mutating the payload. Each segment carries a concrete JSON `content_path` (e.g. `("messages", 4, "content", 0, "text")`) that resolves to an actual string leaf. A `stable_prefix_hash` (structural descriptor) and a `request_shape_hash` (coarse shape fingerprint) are recorded for grouping. Dashboard: "Runtime → Segmentation"; JSON: `GET /api/stats/canonical-request-segmentation`.
- **Transcoder cache stability (Phase 3)** — cross-protocol requests carry a `cache_boundary_tracker` (append-only, cap 64 per request) recording whether `cache_control` annotations were `preserved`, `preserved_relocated`, `dropped_unsupported_target`, `dropped_feature_disabled`, `dropped_invalid_shape`, or `synthesized`. Native `tools[].cache_control` is preserved byte-for-byte on the OpenAI→Anthropic path; Anthropic `cache_control` is dropped on the Anthropic→OpenAI path with a structured warning. JSON: `GET /api/stats/cache-stability`.
- **Observe-mode compression accounting (Phase 4)** — the analyzer runs on every request when `[compression] enabled = true` and records candidate counts, savings estimates, suppression reasons (`protected_cache_boundary`, `static_prefix`, `placement`, `below_min_candidate_tokens`, `below_min_savings_tokens`, `transform_disabled`, `empty_segment`, `latency_budget`), and latency. **Reporting only** — no payload mutation. Dashboard: "Runtime → Compression"; JSON: `GET /api/stats/compression-observability`.
- **Safe suffix compression (Phase 5)** — when `[compression] mode = "safe"`, six deterministic transforms fold eligible `volatile_suffix` content:
  - `fold_repeated_lines` — collapses adjacent repeated log/code lines
  - `compact_logs` — strips log noise (timestamps, levels, repeated prefixes)
  - `compact_search_results` — deduplicates search hits and trims verbose hits
  - `compact_stack_traces` — keeps the first frame per unique trace, drops duplicates
  - `elide_base64_blobs` — replaces inline base64 with a `sha256:` reference
  - `minify_machine_json` — strips whitespace from embedded machine JSON

  `stable_prefix_content_hash` is recomputed SHA-256-verified after every transform. **Any mismatch degrades to the original payload with `failed_fallback=True`** — the applier is fail-closed, so a transform bug or path-resolution error cannot poison the provider cache. Each transform injects a deterministic marker `[EggPool compression: <transform> | segment=<id> | lines=<n> | tokens=<n> | sha256=<digest>]`. Default mode is `observe` (reporting only). Dashboard: "Runtime → Compression"; JSON: `GET /api/stats/compression-observability`, `/api/stats/compression-runtime`.

  Six safety rails: (a) `volatile_suffix` only unless `compress_static_prefix = true`; (b) `compress_static_prefix = true` in any non-default policy requires global `allow_static_prefix_override = true`; (c) `min_candidate_tokens` / `min_savings_tokens` gate eligibility; (d) `max_compression_latency_ms` caps analyzer budget; (e) context-limit checks happen **before** compression — compression cannot rescue over-limit requests; (f) `failed_fallback` never increments provider error counters or writes `account_backoffs` rows (compression isolation is the same as cache isolation).
- **Per-policy overrides (Phase 6)** — `[[compression.policies]]` rows target clients, protocols, models, transcoding paths, providers, or provider kinds without changing the global config. Match fields union OR (any field firing activates the override). Matchers (`match_clients`, `match_requested_models`, `match_protocols`, `match_transcoded`, `match_provider_ids`, `match_provider_kinds`, `match_models`) support glob (`*foo`, `foo*`, `*foo*`, exact). Pre-route, only client/protocol/model/transcode matchers are active; provider-specific matchers are silently skipped until post-route resolution lands. A catch-all override is only valid when named `"default"`. Dashboard: "Runtime → Compression policy"; JSON: `GET /api/stats/compression-policies`.
- **Synthetic cache controls (Phase 9)** — opt-in post-route `cache_control` annotations for providers that support explicit cache boundaries (Anthropic-style). The selector runs **after** account selection and provider-bound transcoding, so it sees `context.upstream_protocol` (OpenAI clients routed to Anthropic providers are supported). Disabled by default; dry-run by default; `require_policy = true` by default in apply mode. Only `ttl = "ephemeral"` is currently accepted (5m/1h reserved and rejected at config load). Stable-prefix only; native `cache_control` preserved byte-for-byte; `cache_control` not duplicated on the same container. Structural-diff safety (`_validate_synthetic_cache_diff`) rejects mutations outside the candidate set — apply mode fails closed on unexpected payload diffs. JSON: `GET /api/stats/synthetic-cache-observability`.
- **Closed-loop threshold tuning (Phase 10)** — advisory recommendations for `min_candidate_tokens`, `min_savings_tokens`, `max_compression_latency_ms`. `mode = "recommend"` writes recommendations to the `compression_tuning_recommendations` table and the dashboard; `mode = "apply"` is accepted at config but **does not currently register runtime overrides** — a future supervised background task must call `build_runtime_override()` then `registry.register()` before apply mode takes effect. Recommendations are bounded (`[field_min, field_max]`), rate-limited (`max_adjustment_pct`, `cooldown_seconds`), and never touch `compress_static_prefix`, `mode`, transforms, or synthetic-cache knobs. JSON: `GET /api/stats/compression-tuning`.
- **Replay fixtures & regression harness (Phase 11)** — test-only. 17 sanitized JSON fixtures under `tests/fixtures/cache_compression/` (OpenAI, Anthropic, transcode, routing, stats) plus a `tests/helpers/cache_compression_replay.py` harness. Replay shape semantics: `disabled` (no synthetic cache config), `client_bound` (synthetic cache on client-shape payload), `provider_bound` (transcode first, synthetic cache on provider-bound body — mirrors production), `provider_bound_unavailable` (transcode produced no provider body). A sanitization linter enforces no bearer tokens, no `sk-...` keys, no oversized strings, and unique fixture names. The seven sentinel strings (`SYSTEM_POLICY_SENTINEL_DO_NOT_COMPRESS`, `TOOL_SCHEMA_SENTINEL_DO_NOT_COMPRESS`, `VOLATILE_LOG_LINE`, `STACK_TRACE_SENTINEL`, `SYNTHETIC_BASE64_BLOB`, `LONG_USER_INSTRUCTION`, `LATEST_USER_SENTINEL`) prove no real prompt text leaks in. Regression suite: `tests/unit/test_replay_fixtures_regression.py` (13 test classes) and `tests/unit/test_replay_fixtures_sanitization.py`. The harness runs in default `pytest` via the `TestReplaySmoke` cheap-invariant class; the full matrix is gated by the `cache_compression_replay_full` marker.

### Per-request headers

- `x-eggpool-compression: off|observe|safe` (when `[compression] header_override = true`) — overrides the configured mode for a single request.
- `x-eggpool-cache-policy: preserve` — opts the request out of any cache/compression/synthetic-cache mutation for cache-equivalent flows.

### Privacy invariants

No raw prompt, tool output, system message, request body, auth header, or provider API key is ever shown or persisted in any cache, compression, or synthetic-cache surface. What **is** shown: token/byte counts, hashes (request shape, stable-prefix structural descriptor, exact stable-prefix content, transcoder cache boundary snapshot), structural JSON paths, warning codes, policy names, and status counters. The replay harness uses sentinel strings so the sanitization linter can prove no real prompt text leaks into fixtures.

### Recommended rollout

1. **Baseline disabled** — confirm only Phase 1–4 observability is recorded.
2. **Observe-only compression** for 24–48 hours. Inspect candidate rate, estimated savings, analyzer latency, suppression reasons.
3. **Safe suffix compression** for one client/policy. Inspect `stable_prefix_preserved` count and `failed_fallback` count.
4. **Expand safe compression** to additional clients if stable.
5. **Synthetic cache dry-run** for Anthropic providers only. Inspect candidate/applied dry-run counts and native-preserved warnings.
6. **Synthetic apply mode** for one Anthropic provider/client only if dry-run is clean.
7. **Tuning recommendation-only** mode if the operator wants threshold advice.

### Rollback

Operator rollback is a documented config-only change. No schema rollback is required; added columns and audit tables are additive.

```toml
[compression]
enabled = false
mode = "observe"

[cache.synthetic_cache_controls]
enabled = false
dry_run = true

[compression.tuning]
enabled = false
mode = "recommend"
```

After editing, run `eggpool rehash` (or `systemctl restart eggpool`) to apply the new config.

### Further reading

- Full operator guide: [docs/cache-compression.md](docs/cache-compression.md)
- Copy-pasteable profiles (baseline / observe-only / safe suffix / synthetic cache dry-run / synthetic cache apply / tuning recommendation-only): [docs/cache-compression-profiles.md](docs/cache-compression-profiles.md)
- Symptom-to-cause troubleshooting: [docs/cache-compression-troubleshooting.md](docs/cache-compression-troubleshooting.md)
- Architecture & phase-by-phase design: [architecture/README.md](architecture/README.md) § Cache Token Observability through § Operator Documentation, Profiles, and Rollout (Phase 12)
- Routing guardrails invariant: [architecture/README.md](architecture/README.md) § Routing Guardrails and Non-Interference (Phase 8)

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
| `GET` | `/api/stats/synthetic-cache-observability` | Synthetic cache candidate / applied / native-preserved counts |
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