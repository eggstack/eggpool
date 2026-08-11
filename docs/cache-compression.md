# Cache-Compression Operator Guide

EggPool ships an opt-in, cache-preserving request-shaping stack. This guide explains the stable operator surfaces, the safe defaults, the advanced knobs that are intentionally demoted from the main example config, and the routing guardrails that keep request shaping out of account selection.

- **Profiles** — `docs/cache-compression-profiles.md`
- **Troubleshooting** — `docs/cache-compression-troubleshooting.md`

## Operator model

1. **Provider cache counters** records whether upstreams surfaced cache counters.
2. **Request segmentation** classifies stable prefix, semi-stable context, and volatile suffix without mutating payloads.
3. **Native cache preservation** records how transcoding handled provider-native cache annotations.
4. **Compression** estimates savings in `observe` mode.
5. **Compression — safe-mode details** mutates only eligible volatile suffix string leaves and fails closed if the stable prefix changes.
6. **Policy overrides** scope compression or EggPool cache annotation behavior by client, protocol, model, provider, or policy name.
7. **EggPool cache annotations** add provider-bound cache annotations only after a dry-run-first rollout.
8. **Tuning suggestions** surfaces bounded threshold suggestions without changing live behavior by default.
9. **Runtime visibility** exposes summary cards and drill-down tables without raw prompt content.
10. **Routing isolation** ensure request-shaping metrics never affect same-provider routing.

## Public surfaces

| Surface | Default | Mutates requests? | Normal operator use |
|---------|---------|-------------------|---------------------|
| Provider cache counters | on | no | yes |
| Request segmentation | on | no | yes |
| Native cache preservation | on with transcoding | no | yes |
| Compression | off unless `[compression].enabled = true` | no | yes |
| Compression — safe-mode details | off (`mode = "observe"`) | volatile suffix only, fail-closed | yes |
| Policy overrides | off | scoped overlay only | sometimes |
| EggPool cache annotations | off (`enabled = false`, `dry_run = true`) | provider-bound stable prefix only | maybe |
| Tuning suggestions | off | no | rarely |
| Replay fixtures | test-only | no | developer-only |

## What is safe by default

With the shipped defaults, the entire stack is reporting-only. No request body, header, or route is altered:

- Cache counters are recorded but never affect quota scoring.
- Segmentation annotates durable columns without inspecting prompts.
- Native cache-preservation tracking records boundary events without changing transcoded bodies.
- Observe mode records opportunities but never mutates payloads.
- Safe compression defaults to `mode = "observe"`, so enabling `[compression]` alone does not activate the applier.
- Policy overrides default to `[]`.
- Synthetic cache controls default to `enabled = false`.
- Advisory tuning defaults to `enabled = false`.
- Replay fixtures stay in test-only paths.

## UI terminology

The dashboard `/cache` page uses these operator-facing labels. If you have bookmarked or are accustomed to older labels, use this mapping:

| Dashboard label | Previous name | Config surface |
|----------------|---------------|----------------|
| Provider cache counters | Cache reporting | `[cache]` (upstream-provided counters) |
| Compression | Compression opportunities | `[compression]` |
| Compression — safe-mode details | Safe compression | `[compression]` (mode = "safe") |
| EggPool cache annotations | Synthetic cache controls | `[cache.synthetic_cache_controls]` |
| Tuning suggestions | Advisory tuning | `[compression.tuning]` |
| Routing isolation | Routing guardrails | `[routing]` (always-on) |
| Request changes | Request shaping summary | `/api/stats/request-shaping` |

Config keys and API field names are unchanged. Only the dashboard labels and operator-facing prose were updated.

The `/cache` summary cards now read `Clean` / `Isolated` on a quiet
default install instead of raw `reporting_only` or `—`. The advanced
diagnostics disclosure stays collapsed unless a warning or non-default
runtime state is present (segmentation parse failures, synthetic-cache
warnings or applied count, tuning recommendations, routing guardrail
violation, transcoding loss warnings, compression warnings or policy
warnings). See `architecture/README.md` § Cache page cards for the
structured `CacheAdvancedState` helper that drives the open/closed
decision.

Provider cache counter coverage is reported on its own card; EggPool
cache annotations are reported on a separate card. The two concepts
are explicitly NOT conflated: missing provider cache counters are not
cache misses and do not prove the upstream is uncached. EggPool never
disables provider-side caching.

## Safe-mode single-pass observation

When `mode = "safe"`, the compression pipeline runs a single
`apply_safe_compression()` pass that derives observation metrics directly
from the applier. The observe-mode `analyze_compression()` is no longer
called as a separate pre-apply step. This avoids redundant segment
walking, duplicate transform eligibility checks, and double
serialization of the same payload.

The dashboard can distinguish observe mode from safe mode via the
observation's `mode` and `source` fields:

| Mode | `mode` | `source` |
|------|--------|----------|
| Observe-only | `"observe"` | `"analyzer"` |
| Safe, derived from apply | `"safe"` | `"safe_apply"` |

### Path-level copy-on-write

Safe compression uses path-level copy-on-write instead of deep-copying
the entire payload. Requests where no transform applies do not copy the
payload at all -- the original object is forwarded unchanged. When
transforms do apply, only the dictionaries and lists on the paths to
mutated leaves are copied; unchanged subtrees are preserved by
reference. Stable-prefix content hash verification remains exact and
unchanged. At provider binding, the transformed graph crosses the trusted
`adopt_provider_payload()` boundary, so this sharing is not lost to a second
recursive ownership pass. Later provider transforms still copy shared child
paths before mutating them.

## What is experimental

These features ship behind explicit operator opt-in:

- **Safe compression `mode = "safe"`** — actually mutates eligible `volatile_suffix` segments. The applier still fails closed on any stable-prefix mismatch.
- **Synthetic cache apply mode** — adds `cache_control` annotations to provider-bound Anthropic requests. Dry-run is the default and the structural-diff guard rejects unexpected payload changes.
- **Advisory tuning** — recommendation-only threshold guidance; it never changes runtime policy.

## What never affects routing

`QuotaFairScorer.score_accounts` accepts exactly four canonical inputs:

- `account_names`
- `model_name`
- `active_requests`
- `request_estimates`

It returns `RoutingScore` instances with no cache, compression, synthetic-cache, or tuning field. This is pinned by the focused routing guardrail tests; cache/compression behavior does not rely on a replay-matrix test.

`GET /api/stats/runtime` exposes a `guardrails` dict with hardcoded constants. The `/cache` dashboard page renders the operator-facing summary and per-surface drill-down tables, backed by `GET /api/stats/request-shaping` and the per-surface endpoints:

```json
{
  "routing_cache_compression_mode": "reporting_only",
  "routing_uses_cache_metrics": false,
  "routing_uses_compression_metrics": false,
  "routing_uses_stable_prefix_hash": false,
  "routing_uses_compression_policy": false,
  "routing_uses_synthetic_cache": false,
  "routing_uses_compression_tuning": false,
  "route_scorer_inputs": ["account_names", "model_name", "active_requests", "request_estimates"]
}
```

These flags are constants; they are never derived from request content.

## Privacy and content safety

What is **never** shown or persisted in any cache, compression, or synthetic-cache surface:

- Raw prompts
- Raw tool outputs
- System messages (content)
- Request bodies (apart from durable hashes)
- Auth headers or `Authorization:` values
- Provider API keys or `sk-...` strings
- Provider response bodies in cache/compression summaries

What **is** shown:

- Token counts and byte counts
- Hashes (request shape hash, stable-prefix structural descriptor hash, exact stable-prefix content hash, transcoder cache boundary snapshot)
- Structural JSON paths (e.g., `("messages", 4, "content", 0, "text")`)
- Warning codes (e.g., `stable_prefix_hash_mismatch`, `synthetic_cache_control_dry_run`)
- Policy names and policy source (`global` / `policy:<name>`)
- Status counters (mode counts, status_counts, warning_counts)

The replay fixtures (`tests/fixtures/cache_compression/`) use sentinel strings so the sanitization linter can prove no real prompt text leaked in. The supported request-shaping contracts are exercised by focused unit tests; the fixture tree is not a second full behavior matrix.

## Config surface

The default example intentionally exposes only the stable request-shaping knobs:

| Field | Stability | Normal use? | Notes |
|-------|-----------|-------------|-------|
| `compression.enabled` | stable | yes | master switch |
| `compression.mode` | stable | yes | `observe` or `safe` |
| `compression.min_candidate_tokens` | stable | yes | candidate threshold |
| `compression.min_savings_tokens` | stable | yes | savings threshold |
| `compression.max_compression_latency_ms` | stable | yes | analyzer/apply latency budget |
| `compression.transforms.*` | stable | sometimes | deterministic transform toggles |
| `compression.policies.*` | advanced | sometimes | scoped overrides |
| `cache.synthetic_cache_controls.*` | experimental | maybe | Anthropic-oriented, dry-run first |
| `compression.tuning.*` | experimental/advisory | rarely | recommendation-only today |
| `compression.placement` / `compress_static_prefix` / `allow_static_prefix_override` / `header_*` | advanced or dangerous | no | keep defaults unless a rollout doc explicitly says otherwise |

## Config validation notes

These are enforced at config load time:

- **Synthetic cache `ttl`** — only `"ephemeral"` is currently accepted. `"5m"` and `"1h"` are reserved and rejected at config load.
- **Static-prefix compression** — `compress_static_prefix = true` in any non-default policy override is rejected unless `allow_static_prefix_override = true` is set globally. This prevents an operator from accidentally mutating the stable prefix by editing one row.
- **Tuning** — recommendation-only; `mode = "apply"` is rejected.
- **`compress_static_prefix = false`** — the normal setting. The whole cache-compression stack is designed around the invariant that stable prefixes never change.
- **Context-limit precedence** — context-limit checks happen before compression. Compression cannot rescue over-limit requests; the request will be rejected with `ContextLimitExceededError` regardless of compression mode.
- **Default `max_breakpoints`** — capped at 4 (Anthropic's documented limit). Larger values are rejected by the validator.

## Recommended deployment shape

EggPool is designed for local/LAN routing for coding agents, often on small SBCs, with multiple same-provider subscriptions, provider transcoding, SQLite accounting, and conservative request shaping. The baseline config that ships in `config.example.toml` matches this:

- Compression off / observe-only.
- Synthetic cache controls disabled and dry-run.
- Tuning recommendations off.
- Routing load-based (request count + token count + active count + health).

See `docs/cache-compression-profiles.md` for copy-pasteable profiles ranging from baseline-disabled to synthetic cache apply and advisory tuning.

## Rollout guide (summary)

A conservative staged rollout:

1. **Baseline disabled** — confirm only cache reporting and segmentation are visible.
2. **Observe-only compression** for 24-48 hours. Inspect candidate rate, estimated savings, analyzer latency, suppression reasons.
3. **Safe suffix compression** for one client/policy. Inspect stable-prefix preserved count and failed fallback count.
4. **Expand safe compression** to additional clients if stable.
5. **Synthetic cache dry-run** for Anthropic providers only. Inspect candidate/applied dry-run counts and native-preserved warnings.
6. **Synthetic apply mode** for one Anthropic provider/client only if dry-run is clean.
7. **Tuning recommendation-only** mode if operator wants threshold advice.

Full rollout, dashboard interpretation, and troubleshooting guides are in `docs/cache-compression-profiles.md` and `docs/cache-compression-troubleshooting.md`.

## Rollback

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

After editing, run `eggpool restart` (or `systemctl restart eggpool`) to apply the new config.

## See also

- `docs/cache-compression-profiles.md` — six copy-pasteable config profiles
- `docs/cache-compression-troubleshooting.md` — symptom-to-cause guide and dashboard interpretation
- `docs/transcoding.md` — protocol transcoding operator guide (cache stability layer is documented there)
- `architecture/README.md` § Request Shaping Overview and § Routing Guardrails and Non-Interference
