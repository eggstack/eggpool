# Cache-Compression Operator Guide

EggPool ships a layered, opt-in cache-preserving deterministic compression stack. This guide explains what each layer does, what is safe to turn on by default, what is experimental, and what never affects routing. Profiles, dashboard interpretation, troubleshooting, and rollout guidance live alongside this document.

- **Profiles** — `docs/cache-compression-profiles.md`
- **Troubleshooting** — `docs/cache-compression-troubleshooting.md`

## Operator model in ten steps

1. **Observe cache counters.** Providers may report cached-token counters. Unknown is not zero.
2. **Segment requests.** EggPool classifies stable prefix, semi-stable context, and volatile suffix without mutating payloads.
3. **Preserve provider cacheability.** Transcoding and compression should not disturb stable prefixes or native cache controls.
4. **Observe compression opportunities.** Observe mode estimates deterministic suffix-compression savings without mutation.
5. **Apply safe suffix compression.** Safe mode mutates only eligible volatile suffix string leaves and fails closed if the stable prefix changes.
6. **Apply policy controls.** Operators can scope behavior by client, protocol, model, provider, or policy name.
7. **Inspect runtime views.** Dashboard/API expose counters and warnings without raw prompts.
8. **Keep routing separate.** Cache, compression, synthetic cache, and tuning metrics never affect same-provider routing.
9. **Synthetic cache controls.** Optional, dry-run-first, Anthropic-only, post-route provider-bound mutation.
10. **Threshold tuning.** Recommendation-only. No automatic runtime override today.

## Phase map

| Phase | Layer | Default | Mutates requests? | Affects routing? |
|-------|-------|---------|--------------------|------------------|
| 1 | Cache token observability | on | no | no |
| 2 | Canonical request segmentation | on | no | no |
| 3 | Transcoder cache stability | on (when transcoding is on) | no | no |
| 4 | Observe-mode compression accounting | on when `[compression] enabled = true` | no | no |
| 5 | Safe suffix compression | off (`mode = "observe"` default) | yes — volatile suffix only | no |
| 6 | Compression policy overrides | off (`policies = []`) | yes — overlay only | no |
| 7 | Dashboard/runtime views | on when dashboard enabled | no | no |
| 8 | Routing guardrails | always on (hardcoded diagnostic) | no | no |
| 9 | Synthetic cache controls | off (`enabled = false`) | yes — provider-bound stable prefix only, dry-run default | no |
| 10 | Closed-loop threshold tuning | off (`enabled = false`, `mode = "recommend"`) | no | no |
| 11 | Replay fixtures + regression harness | test-only | no | no |
| 12 | Operator docs, profiles, rollout | docs only | no | no |

## What is safe by default

With the shipped defaults, the entire stack is observability-only. No request body, header, or route is altered:

- **Phase 1 cache counters** are recorded but never affect quota scoring.
- **Phase 2 segmentation** annotates durable columns without inspecting prompts.
- **Phase 3 cache stability** records boundary events on the in-memory `CacheBoundaryTracker` without changing transcoded bodies.
- **Phase 4 observe mode** runs the analyzer on every request but never mutates.
- **Phase 5 safe compression** defaults to `mode = "observe"` so even when `[compression] enabled = true`, the applier is a no-op.
- **Phase 6 policy overrides** default to `policies = []` so the resolver is bypassed.
- **Phase 9 synthetic cache controls** default to `enabled = false`, so the post-route selector never runs.
- **Phase 10 threshold tuning** defaults to `enabled = false`; the recommendation engine is dormant until operators opt in.
- **Phase 11 replay fixtures** are test-only; they never touch the production code path.

Phase 7 dashboard/runtime views and Phase 8 routing guardrails are read-only surfaces.

## What is experimental

These features ship behind explicit operator opt-in:

- **Phase 5 `mode = "safe"`** — actually mutates eligible `volatile_suffix` segments. Even then, the applier fails closed on any stable-prefix mismatch (the request is sent uncompressed with `failed_fallback=True`).
- **Phase 9 synthetic cache `apply` mode** — adds `cache_control` annotations to provider-bound Anthropic requests. Dry-run is the default when enabled. Apply mode requires a matching `[[compression.policies]]` row by default (`require_policy = true`) and is gated by `_validate_synthetic_cache_diff` structural-diff safety.
- **Phase 10 `mode = "apply"`** — accepted at config time but currently behaves like `recommend`. No production code path registers runtime overrides; a future supervised background task must call `build_runtime_override()` then `registry.register()` before apply mode takes effect.

## What never affects routing

`QuotaFairScorer.score_accounts` accepts exactly four canonical inputs:

- `account_names`
- `model_name`
- `active_requests`
- `request_estimates`

It returns `RoutingScore` instances with no cache, compression, synthetic-cache, or tuning field. This is pinned by `tests/unit/test_routing_guardrails.py` (19 tests across 7 classes) and by `inspect.getsource` checks in `tests/unit/test_replay_fixtures_regression.py::TestRoutingNonInterference`.

`GET /api/stats/runtime` exposes a `guardrails` dict with hardcoded constants:

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

The replay harness (`tests/fixtures/cache_compression/`) uses seven sentinel strings (`SYSTEM_POLICY_SENTINEL_DO_NOT_COMPRESS`, `TOOL_SCHEMA_SENTINEL_DO_NOT_COMPRESS`, `VOLATILE_LOG_LINE`, `STACK_TRACE_SENTINEL`, `SYNTHETIC_BASE64_BLOB`, `LONG_USER_INSTRUCTION`, `LATEST_USER_SENTINEL`) so a sanitization linter can prove no real prompt text leaked in. The harness supports two replay shapes (client-shape and provider-bound) — see `tests/fixtures/cache_compression/README.md` § Replay shape semantics for the contract and `tests/unit/test_replay_fixtures_regression.py::TestProviderBoundSyntheticReplay` for the pin.

## Config validation notes

These are enforced at config load time:

- **Synthetic cache `ttl`** — only `"ephemeral"` is currently accepted. `"5m"` and `"1h"` are reserved and rejected at config load.
- **Static-prefix compression** — `compress_static_prefix = true` in any non-default policy override is rejected unless `allow_static_prefix_override = true` is set globally. This prevents an operator from accidentally mutating the stable prefix by editing one row.
- **Tuning `mode = "apply"`** — accepted but currently dormant. Recommendations are tagged `recommendation_only` regardless of mode.
- **`compress_static_prefix = false`** — the normal setting. The whole cache-compression stack is designed around the invariant that stable prefixes never change.
- **Context-limit precedence** — context-limit checks happen before compression. Compression cannot rescue over-limit requests; the request will be rejected with `ContextLimitExceededError` regardless of compression mode.
- **Default `max_breakpoints`** — capped at 4 (Anthropic's documented limit). Larger values are rejected by the validator.

## Recommended deployment shape

EggPool is designed for local/LAN routing for coding agents, often on small SBCs, with multiple same-provider subscriptions, provider transcoding, SQLite accounting, and conservative request shaping. The baseline config that ships in `config.example.toml` matches this:

- Compression off / observe-only.
- Synthetic cache controls disabled and dry-run.
- Tuning recommendations off.
- Routing load-based (request count + token count + active count + health).

See `docs/cache-compression-profiles.md` for six copy-pasteable profiles ranging from baseline-disabled to apply-mode synthetic cache.

## Rollout guide (summary)

A conservative staged rollout:

1. **Baseline disabled** — confirm only Phase 1-4 observability is recorded.
2. **Observe-only compression** for 24–48 hours. Inspect candidate rate, estimated savings, analyzer latency, suppression reasons.
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

After editing, run `eggpool rehash` to restart the supervisor with the new config.

## See also

- `docs/cache-compression-profiles.md` — six copy-pasteable config profiles
- `docs/cache-compression-troubleshooting.md` — symptom-to-cause guide and dashboard interpretation
- `docs/transcoding.md` — protocol transcoding operator guide (cache stability layer is documented there)
- `architecture/README.md` § Routing Guardrails and Non-Interference (Phase 8) — invariant and test pins
- `plans/cache_compression_phase_12_operator_docs_profiles.md` — Phase 12 design plan