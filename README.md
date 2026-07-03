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
- Tracks requests, tokens, latency, errors, and cost provenance in SQLite (`provider_reported`, trusted local `derived`/`partial`, bounded `estimated`)
- Multi-page dashboard with 50+ themes, reliability, routing, and runtime views
- Model metadata enrichment from provider catalogs, OpenRouter, Artificial Analysis, and Hugging Face
- Designed for lightweight deployments (Raspberry Pi, SBCs)
- Transparent protocol transcoding between OpenAI and Anthropic request formats
- Thinking/reasoning capability-aware routing with configurable budget mapping
- Provider-neutral cache observability — records whether upstreams report `cache_read` / `cache_creation` (Anthropic) or `prompt_tokens_details.cached_tokens` (OpenAI) and exposes a dashboard hit ratio that never silently mixes zero with missing
- Canonical request segmentation — every finalized request is annotated into `stable_prefix` / `semi_stable_context` / `volatile_suffix` regions without mutating the payload, giving later compression phases a safe way to identify cache-continuity boundaries and compressible candidates. Segmentation `content_path` values are concrete JSON paths that resolve to actual string leaves of the request payload (not semantic role labels), with `resolve_path` and `resolve_text_path` helpers available for tests and debug assertions
- Transcoder cache stability — every cross-protocol request carries a bounded `cache_boundary_tracker` that records whether `cache_control` annotations were preserved, relocated, or dropped, plus deterministic SHA-256 of the provider-visible stable prefix so downstream phases can compare cache-equivalent bodies without re-parsing
- Safe suffix compression — when `[compression] mode = "safe"`, deterministic transforms fold repeated lines, compact logs/search/stack traces, elide base64 blobs, and minify machine JSON inside `volatile_suffix` regions, preserving every `stable_prefix` segment byte-for-byte (recomputed SHA-256 verified via exact content hash of the stable-prefix segments re-extracted from both original and transformed payloads) and degrading to the original payload on any mismatch. All six transforms emit unified markers via `markers.build_marker` with the format `[EggPool compression: <transform> | segment=<id> | lines=<n> | tokens=<n> | sha256=<digest>]`. Context-limit checks happen before compression, so compression cannot rescue over-limit requests
- Phase 9: synthetic provider cache controls (post-route, disabled by default, dry-run by default)
- Phase 10: closed-loop threshold tuning (recommendation-only)
- Phase 11: replay fixture harness + regression tests (`tests/fixtures/cache_compression/`, `tests/unit/test_replay_fixtures_*.py`)
- Phase 12: operator docs, profiles, and rollout guide ([docs/cache-compression.md](docs/cache-compression.md), [profiles](docs/cache-compression-profiles.md), [troubleshooting](docs/cache-compression-troubleshooting.md))

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
| `eggpool stats repair-costs` | Dry-run/apply repair for suspicious historical request costs |
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

Segmentation `content_path` values are concrete JSON paths resolving to the actual string leaves of the request payload — `("messages", i, "content")` for OpenAI string content, `("messages", i, "content", j, "text")` for OpenAI list content parts, `("system",)` for Anthropic string system, `("system", j, "text")` for Anthropic system blocks, `("messages", i, "content", j, ...)` for Anthropic content blocks, etc. Path-resolution helpers `resolve_path` and `resolve_text_path` are available for tests and debug assertions.

Segmentation is observational: request bodies, route scoring, and eligibility are unchanged. The dashboard renders a coverage card under "Runtime → Segmentation" and the JSON API exposes the breakdown at `GET /api/stats/canonical-request-segmentation`.

## Safe suffix compression

When `[compression] mode = "safe"`, EggPool applies deterministic transforms to eligible `volatile_suffix` segments and re-verifies the stable-prefix content hash on the mutated payload. The default mode is `observe` (Phase 4 — reporting only); set `mode = "safe"` to actually mutate.

Six transforms are available:

- **`fold_repeated_lines`** — replaces adjacent identical lines with a single representative plus a count marker
- **`compact_logs`** — preserves command text, exit code, first/last N lines, and diagnostic patterns (error, failed, panic, etc.) from large tool/log output
- **`compact_search_results`** — preserves file path, line number, and matched line for each retained match while collapsing duplicate matches and limiting excessive context
- **`compact_stack_traces`** — folds repeated identical stack frames with count markers while preserving the first occurrence of each unique trace shape and the final active error path
- **`elide_base64_blobs`** — replaces large opaque base64/data-URI blobs with a placeholder noting detected blob type and original size
- **`minify_machine_json`** — strips insignificant whitespace from machine-generated JSON payloads in volatile-suffix segments

**Eligibility**: only `volatile_suffix` segments; candidates must exceed `min_candidate_tokens` (default 2048) and `min_savings_tokens` (default 1024).

**Cache safety**: protected `stable_prefix` segments are never touched. `stable_prefix_content_hash` is an exact SHA-256 of canonical stable-prefix content (system, tools, cache_control blocks), re-extracted from both original and transformed payloads via stable-prefix segment paths. The structural descriptor hash `stable_prefix_shape_hash` (the legacy `stable_prefix_hash`) is also tracked separately. On mismatch, the request is sent uncompressed with `failed_fallback=True` and a `stable_prefix_hash_mismatch` warning. The fail-closed verification re-hashes the TRANSFORMED payload's stable-prefix content, not just immutable segment metadata, so it catches real path bugs that mutate stable-prefix content.

**Context-limit precedence**: context-limit checks happen before compression. Compression does NOT make otherwise over-limit requests fit within model limits.

**Latency budget**: `max_compression_latency_ms` (default 25) bounds the applier budget; over-budget runs append `latency_budget_exceeded` warnings.

**Per-request headers**: `x-eggpool-compression: off|observe|safe` (when `header_override = true`) and `x-eggpool-cache-policy: preserve` to opt out for cache-equivalent flows. All six transforms emit unified deterministic markers via `eggpool.transcoder.compression.markers.build_marker` with the format `[EggPool compression: <transform> | segment=<id> | lines=<n> | tokens=<n> | sha256=<digest>]`.

**Observability**: dashboard renders under "Runtime → Compression"; JSON API at `GET /api/stats/compression-observability`. Migration 0043 adds 13 columns + 2 indexes to `requests`.

## Compression policy overrides

Operators can target specific clients, protocols, models, or transcoding paths with `[[compression.policies]]` rows that overlay the global `[compression]` config without changing it for everyone else.

```toml
[compression]
mode = "observe"
enabled = true

[[compression.policies]]
name = "claude-code-safe"
match_clients = ["*claude*"]
enabled = true
mode = "safe"

[[compression.policies]]
name = "anthropic-no-compress"
match_protocols = ["anthropic"]
enabled = false
```

**Match fields** (union OR across fields; a request matches if ANY field matches):

- `match_clients` — list of glob patterns against `x-eggpool-client` header (`client_id`) or `User-Agent` (`client_name`). Supports `*foo`, `foo*`, `*foo*`, exact.
- `match_protocols` — exact-match list against the inbound source protocol (`openai` or `anthropic`).
- `match_requested_models` — list of glob patterns against the client-requested model id.
- `match_provider_ids`, `match_provider_kinds`, `match_models` — exact-match against routed provider info. **Pre-route these are no-ops** (provider info not yet known); these fields are reserved for post-route resolution.
- `match_transcoded` — `true` matches transcoded requests; `false` matches non-transcoded; `None` matches both.

**Overlay semantics**: matched overrides are merged on top of the global config in file order. Scalar fields use last-match-wins; `transforms` merge field-by-field (`None` keeps the base value, `True`/`False` wins). The reserved name `"default"` produces a catch-all override (fires on every request when no match fields are set).

**Static-prefix guard**: `compress_static_prefix = true` in any non-default override is rejected unless `allow_static_prefix_override = true` is set globally. This prevents an operator from accidentally enabling prefix mutation by editing one row.

**Pre-route scope**: resolution happens before account routing, so `match_provider_ids`, `match_provider_kinds`, and `match_models` cannot match (those fields are `None`). Use `match_clients`, `match_protocols`, `match_requested_models`, or `match_transcoded` for pre-route targeting.

**Fail-closed**: malformed overrides (extra fields, type errors, post-validation failures) are skipped with a structured warning and the previous config is preserved. Resolution never raises; the request is always served with a valid policy.

**Observability**: each request records `compression_policy_name` and `compression_policy_source` (`"global"` or `"policy:<name>"`). The stats roll-up at `/api/stats/compression-observability` adds `by_policy`, `by_policy_source`, and `policy_warning_count_total`. Migration 0044 adds 3 columns + 1 index to `requests`.

### Phase 7: Dashboard, runtime views, and operator diagnostics

Phases 1–6 produce data; Phase 7 makes it operationally usable in the dashboard and runtime API. Six focused JSON endpoints expose the per-phase roll-ups; four new runtime cards render the highlights; a static **routing-separation notice** always shows that cache/compression metrics are reporting-only and do not feed routing. No raw prompts, tool outputs, system messages, request bodies, or auth headers appear in any card or JSON response.

| Endpoint | Phase | What it answers |
|----------|-------|-----------------|
| `GET /api/stats/cache-observability` | 1 | Are providers reporting cache counters? Coverage by `reported` / `not_reported` / `unknown_format`; known-only cache hit ratio; cached input tokens by provider/model. |
| `GET /api/stats/canonical-request-segmentation` | 2 | Are requests segmenting correctly? Status counts; avg stable/semi/volatile token estimates; top request-shape hashes. |
| `GET /api/stats/cache-stability` | 3 | Narrow summary only. Per-boundary preservation/drop detail lives on the in-memory `CacheBoundaryTracker` for live requests; this endpoint confirms the tracker is wired and reports durable counters where persisted. |
| `GET /api/stats/compression-observability` | 4 + 6 | Observe-mode opportunity (candidates, estimated savings, suppress reasons). Plus Phase 6 `by_policy` / `by_policy_source` / `policy_warning_count_total` roll-ups. |
| `GET /api/stats/compression-runtime` | 5 | What safe mode actually did: applied / failed_fallback counts, candidate counts, estimated + actual savings tokens, latency (avg/p50/p95/max), per-transform applied/tokens_saved, warnings rollup, cache_safety stable-prefix preserved/mismatch. |
| `GET /api/stats/compression-policies` | 6 | Per-policy rollup table with `<global>` sentinel first: request count, mode distribution, applied, failed_fallback, candidates, warnings. |

Runtime dashboard additions:

- **Compression** — observe / apply / fallback / candidate counts; estimated vs actual savings; suppression reasons
- **Compression runtime** — mode strip (disabled / observe / safe), latency, per-transform table, warnings, cache_safety stable-prefix preserved/mismatch
- **Compression policy** — per-policy table with `<global>` sentinel row
- **Cache stability** — transcoded request count and a note that per-boundary detail lives on the in-memory tracker

All Phase 7 outputs are reporting-only — the `QuotaFairScorer` does not consume any cache or compression column. Routing remains load-based (request count + token count + active count + health). No new migrations; Phase 7 is view-only over the columns added in 0040–0044. See `plans/cache_compression_phase_07_dashboard_runtime_views.md` for the full design.

### Phase 8: Routing guardrails and non-interference guarantees

Phases 1–7 add cache/compression observability and policy controls. Phase 8 formalises the **routing invariant** that those metrics NEVER enter account scoring, health removal, or route reselection.

The guarantee rests on three pins:

1. **Hardcoded runtime diagnostic** — `GET /api/stats/runtime` exposes `routing_runtime.guardrails` with constant flags (`routing_cache_compression_mode: "reporting_only"`, `routing_uses_cache_metrics: false`, `routing_uses_compression_metrics: false`, `routing_uses_stable_prefix_hash: false`, `routing_uses_compression_policy: false`, plus the allowed scorer input list). The runtime dashboard renders these flags as a **Routing guardrails (Phase 8)** card next to the routing-separation notice.
2. **Static + behavioural test pin** — `tests/unit/test_routing_guardrails.py` (19 tests) asserts the `QuotaFairScorer.score_accounts` signature accepts no cache/compression parameter, that identical load with adversarial cache/compression metrics produces identical scores, that two same-provider accounts with skewed cache hits / compression savings / stable-prefix hashes still get fair rotation, that policy resolution does not mutate routing, and that compression fallbacks never affect provider health.
3. **Documentation invariant** — every phase doc (1–7) states that `QuotaFairScorer` does NOT consume the phase's columns. Phase 8 is the focused boundary.

Same-provider account fairness (e.g., multiple OpenAI subscriptions) is preserved because cache hit ratios or compression savings never enter the score. Compression failure (`failed_fallback=True`) is observational — it never marks an account unhealthy. Phase 6 policy overrides cannot reroute; they only adjust the analyzer / applier knobs for the already-selected route.

A future **cache-aware routing mode** would require an explicit `routing.cache_aware = true` config flag plus per-provider support detection, a cost model using cached-token prices, backtesting metrics, per-client opt-in, and dashboard warnings. Phase 8 deliberately does NOT implement it. See `plans/cache_compression_phase_08_routing_guardrails.md` for the full design.

### Phase 9: Synthetic provider cache controls (post-route)

Phase 9 layers opt-in synthetic `cache_control` annotations onto the provider-bound body for providers that support explicit cache boundary hints (initially Anthropic-style).  When enabled (and not in dry-run), the mutator annotates supported stable-prefix containers so the upstream cache can reuse them across requests.

Key invariants:

- **Post-route, provider-bound**: the selector and mutator run inside `RequestCoordinator._apply_synthetic_cache_controls` AFTER account selection and provider-bound transcoding.  OpenAI clients routed to Anthropic providers are supported because the selector sees the actual upstream protocol.
- **Disabled by default, dry-run by default**: opt-in via `[cache] synthetic_cache_controls.enabled = true`.  Dry-run is the default when enabled so operators can observe the plan without changing wire bodies.
- **Stable-prefix only**: only protected `stable_prefix` segments whose source is `SYSTEM`, `DEVELOPER`, or `TOOL_SCHEMA` are eligible.  Volatile suffix and compressed content are never annotated.
- **Native preservation**: existing native `cache_control` annotations are preserved byte-for-byte and never duplicated.  Path representation is normalized internally so candidates and native-preservation checks use the same tuple form.
- **TTL is explicit**: only `ttl = "ephemeral"` is currently accepted.  `5m` and `1h` are reserved and rejected at config load.
- **Structural-diff safety**: apply mode validates the mutated payload only differs by added `cache_control` keys at candidate containers.  Any unexpected change triggers `failed_fallback` and preserves the original payload.
- **Per-policy overrides**: Phase 6 `[[compression.policies]]` rows can set `synthetic_cache_*` fields (post-route); provider-specific matchers (`match_provider_ids`, `match_provider_kinds`) now fire because the resolver sees post-route context.  `_overlay_config()` skips synthetic-cache fields so a policy row containing only synthetic-cache overrides does not poison the compression config overlay.

### Phase 10: Closed-loop threshold tuning (recommendation-only)

Phase 10 adds an advisory recommendation engine that observes Phase 4-6 compression metrics and suggests bounded adjustments to the three tunable thresholds (`min_candidate_tokens`, `min_savings_tokens`, `max_compression_latency_ms`).

- **Currently recommendation-only**: `mode = "recommend"` (the default) writes recommendations to the `compression_tuning_recommendations` table and the dashboard only.  Request behaviour never changes.
- **`mode = "apply"` is accepted at config but does NOT currently register runtime overrides**: the in-memory `RuntimeCompressionPolicyOverrideRegistry` and `apply_runtime_override` helper exist for forward compatibility, but no production code path automatically calls `build_runtime_override()` then `registry.register()`.  A future supervised background task must wire this lifecycle before apply mode takes effect.
- The recommendation engine is content-private (no prompt inspection), bounded (every suggested value clamped to `[compression.tuning.bounds]`), rate-limited (`max_adjustment_pct` per step; `cooldown_seconds` suppresses the next recommendation), and immutable on every other compression knob (mode, enabled, static-prefix, transforms, synthetic cache knobs).

Both phases preserve routing non-interference: `QuotaFairScorer` does NOT consume synthetic cache or tuning fields.  Same-provider account fairness is preserved.

See `plans/cache_compression_phase_09_synthetic_cache_controls.md` and `plans/cache_compression_phase_10_closed_loop_threshold_tuning.md` for the full design.

### Phase 11: Replay fixtures and regression harness

Phase 11 ships a tiny replay fixture harness under `tests/fixtures/cache_compression/` and `tests/helpers/cache_compression_replay.py`.  It exists so that operators can pin down the high-risk Phase 2/3/5/9 behaviour without ever shipping a real prompt to disk.

- **17 sanitized JSON fixtures** across `openai/` (6), `anthropic/` (6), `transcode/` (2), `routing/` (2), and `stats/` (1).  All prompts use the documented sentinel strings (`SYSTEM_POLICY_SENTINEL_DO_NOT_COMPRESS`, `TOOL_SCHEMA_SENTINEL_DO_NOT_COMPRESS`, `VOLATILE_LOG_LINE`, `STACK_TRACE_SENTINEL`, `SYNTHETIC_BASE64_BLOB`, `LONG_USER_INSTRUCTION`, `LATEST_USER_SENTINEL`) so a linter can prove no real prompt text, bearer token, `sk-...` key, or `Authorization:` header slipped in.
- **Deterministic helpers**: `load_fixture`, `expand_repeats` (compact repeat spec), `safe_policy`/`observe_policy`/`disabled_policy`, `synthetic_cache_config`, `run_segmentation`/`run_compression`/`run_transcode`/`run_synthetic`, and a `ReplayBundle` dataclass that summarises the structural outcome (segmentation status, stable-prefix hash, compression transforms, synthetic cache status) without leaking raw payloads.
- **Two test files** ship by default: `tests/unit/test_replay_fixtures_regression.py` (12 test classes pinning stable-prefix preservation, volatile-only mutation, provider-bound synthetic cache, native cache_control preservation, fail-closed fallback, request-shape hashing, and routing non-interference) and `tests/unit/test_replay_fixtures_sanitization.py` (8 linter tests enforcing content privacy and fixture uniqueness).
- The harness is reporting-only: it never enters the routing layer, never persists anything to the production DB, and never logs raw request content on failure.  The `QuotaFairScorer` does NOT consume any Phase 11 fields; routing stays load-based.

Run the Phase 11 suite locally:

```bash
# Default smoke coverage (included in plain `uv run pytest`)
uv run pytest tests/unit/test_replay_fixtures_regression.py tests/unit/test_replay_fixtures_sanitization.py -v

# Full cache/compression replay matrix (slower; exhaustive modes × fixtures)
uv run pytest -m cache_compression_replay_full tests/unit/test_replay_fixtures_regression.py -v
```

See `plans/cache_compression_phase_11_replay_fixtures_regression_tests.md` and `architecture/README.md` § Replay Fixtures and Regression Harness (Phase 11) for the design.

### Phase 12 polish pass: replay-shape and default smoke coverage

The Phase 11 harness has a follow-up polish pass (Phase 12 polish) that hardens how transcode fixtures exercise provider-bound synthetic cache and promotes a small smoke subset into the default pytest run. There are no production runtime changes.

- **Provider-bound synthetic replay.** `run_full_replay()` now runs synthetic-cache against the **provider-bound** body (post-transcode) whenever `client_protocol != target_protocol`. A dedicated `run_provider_bound_synthetic_replay()` helper is available for callers that need an explicit provider-bound lifecycle.
- **`ReplayBundle.synthetic_cache_shape`.** A new field on the bundle records which replay shape was used (`disabled` / `client_bound` / `provider_bound` / `provider_bound_unavailable`); provider-bound observability is recorded on `provider_bound_segmentation_status`, `provider_bound_synthetic_cache_status`, and `provider_bound_synthetic_cache_candidate_count`.
- **Default smoke coverage.** A `TestReplaySmoke` class (`tests/unit/test_replay_fixtures_regression.py`) promotes six cheap invariants outside the `cache_compression_replay_full` mark: OpenAI prefix preservation, Anthropic nested-tool-result compression, provider-bound synthetic dry-run, native-cache preserve-apply, scoring guardrails, and a sentinel-linter smoke pass. They run on every `pytest` invocation.
- **`TestProviderBoundSyntheticReplay`.** A new test class pins the contract for transcode fixtures: dry-run must not mutate client or provider body, apply mode only mutates the provider body, native `cache_control` survives apply, and bundle fields never expose the provider-bound payload.
- **Routing invariant.** Unchanged. `QuotaFairScorer` still accepts only the four canonical inputs and does not consume any Phase 12 polish pass fields.

Replay shape semantics (client-shape vs provider-bound) are documented in `tests/fixtures/cache_compression/README.md` § Replay shape semantics. See `plans/cache_compression_phase_12_polish_pass.md` for the full plan.

### Phase 12: Operator guide, profiles, and rollout

Phase 12 turns the cache-preserving deterministic compression stack into a usable operator feature set. The runtime machinery is unchanged; the documentation surface catches up.

- **Operator guide** — [docs/cache-compression.md](docs/cache-compression.md) walks through the ten-step operator model (observe cache counters, segment, preserve cacheability, observe compression, apply safe suffix compression, apply policy controls, inspect runtime views, keep routing separate, opt into synthetic cache controls, opt into tuning recommendations). The guide also documents what is safe by default, what is experimental, what never affects routing, and the privacy invariants.
- **Profiles** — [docs/cache-compression-profiles.md](docs/cache-compression-profiles.md) ships six copy-pasteable config profiles (baseline / observe-only / safe suffix / synthetic cache dry-run / synthetic cache apply / tuning recommendation-only). Each profile lists the dashboard fields to watch and the JSON endpoint that surfaces them.
- **Troubleshooting** — [docs/cache-compression-troubleshooting.md](docs/cache-compression-troubleshooting.md) maps common symptoms (`compression never applies`, `observe mode sees candidates but safe mode does not mutate`, `provider_unsupported`, `policy_required`, `no_candidates`, `failed_fallback`, `routing seems uneven`, `tuning recommendations not appearing`) to root causes and the next diagnostic step. The dashboard interpretation reference explains every counter, status, and warning code on every Phase 1-11 endpoint.

**Privacy invariants:** no raw prompt, tool output, system message, request body, auth header, or provider API key is ever shown or persisted in any cache, compression, or synthetic-cache surface. The replay harness uses seven sentinel strings so a sanitization linter can prove no real prompt text leaked in.

**Routing invariant:** `QuotaFairScorer` still accepts only `account_names`, `model_name`, `active_requests`, `request_estimates`. `GET /api/stats/runtime.guardrails` reports hardcoded constants (`routing_uses_cache_metrics: false`, `routing_uses_compression_metrics: false`, `routing_uses_synthetic_cache: false`, `routing_uses_compression_tuning: false`). Cache-aware routing would require an explicit `routing.cache_aware = true` flag plus per-provider support detection and is deliberately not implemented.

**Rollback** is a documented config-only change (set `[compression] enabled = false`, `[cache.synthetic_cache_controls] enabled = false`, `[compression.tuning] enabled = false`, then `eggpool rehash`). No schema rollback is required.

See `plans/cache_compression_phase_12_operator_docs_profiles.md` for the design.

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

### Model-info observability

- The dashboard `/models` page renders a degraded-state notice above the table if the model-info service is unattached (no `app.state.model_info`) or if `get_summary_map()` raises an exception. The exception's full traceback is logged under `eggpool.dashboard.routes` — the page never embeds the traceback text in HTML.
- The dashboard `/models/{model_id:path}` detail page distinguishes "no canonical row exists" (empty-state copy) from "lookup failed" (degraded-state notice) when the service throws.
- `/api/stats/runtime` includes a `model_info` section with `enabled`, `canonical_count`, `catalog_model_count`, `provider_model_count`, `due_count`, and a `source_health` dict (no raw source payloads). Failures surface as `*_error` keys and `probe_errors`, never as raised exceptions.
- The force-refresh endpoint and `eggpool models refresh` CLI command are documented separately: catalog refresh (`models refresh`) updates provider listings and may invalidate canonical rows; model-info enrichment is refreshed via `POST /api/model-info/refresh`.

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
| Cache & compression operator guide | [docs/cache-compression.md](docs/cache-compression.md) |
| Cache & compression profiles | [docs/cache-compression-profiles.md](docs/cache-compression-profiles.md) |
| Cache & compression troubleshooting | [docs/cache-compression-troubleshooting.md](docs/cache-compression-troubleshooting.md) |
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
