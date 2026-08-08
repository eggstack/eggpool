# Cache-Compression Troubleshooting

Symptom-to-cause guide for the cache-preserving deterministic compression stack. Each section lists the dashboard / API signals, the most likely root cause, and the next diagnostic step.

Companion docs:

- `docs/cache-compression.md` — operator model, privacy, config validation
- `docs/cache-compression-profiles.md` — six copy-pasteable profiles

## Quick links

- [Compression never applies](#symptom-compression-never-applies)
- [Observe mode sees candidates, safe mode does not mutate](#symptom-observe-mode-sees-candidates-but-safe-mode-does-not-mutate)
- [Synthetic cache shows `provider_unsupported`](#symptom-synthetic-cache-controls-show-provider_unsupported)
- [Synthetic cache shows `policy_required`](#symptom-synthetic-cache-controls-show-policy_required)
- [Synthetic cache shows `no_candidates`](#symptom-synthetic-cache-controls-show-no_candidates)
- [`failed_fallback` increases](#symptom-failed_fallback)
- [Routing seems uneven](#symptom-routing-seems-uneven)
- [Tuning recommendations not appearing](#symptom-tuning-recommendations-not-appearing)
- [Dashboard cards show no data](#symptom-dashboard-cards-show-no-data)

---

## Dashboard interpretation reference

Before troubleshooting, confirm you are reading the right endpoint:

| Endpoint | What it answers |
|----------|-----------------|
| `/api/stats/request-shaping` | Current operator summary: compression mode, cache-reporting coverage, synthetic cache state, advisory tuning, and routing guardrails. |
| `/api/stats/cache-observability` | Are providers reporting cache counters? Coverage by `reported` / `not_reported` / `unknown_format`; provider cache hit rate (cache reads are hits; cache writes/creation are warmup, not hits); deprecated `cache_hit_ratio_known_only` alias; cached input tokens by provider/model. |
| `/api/stats/canonical-request-segmentation` | Are requests segmenting correctly? Status counts; avg stable/semi/volatile token estimates; top request-shape hashes. |
| `/api/stats/cache-stability` | Narrow summary only. Per-boundary preservation/drop detail lives on the in-memory `CacheBoundaryTracker` for live requests; this endpoint confirms the tracker is wired and reports durable counters where persisted. |
| `/api/stats/compression-observability` | Observe-mode opportunities plus policy/source warning rollups. |
| `/api/stats/compression-runtime` | What safe mode actually did: applied / failed_fallback counts, candidate counts, estimated + actual savings tokens, latency (avg/p50/p95/max), per-transform applied/tokens_saved, warnings rollup, cache_safety stable-prefix preserved/mismatch. |
| `/api/stats/compression-policies` | Per-policy rollup table with `<global>` sentinel first: request count, mode distribution, applied, failed_fallback, candidates, warnings. |
| `/api/stats/synthetic-cache-observability` | Synthetic cache plan / apply results: status counts (disabled / dry_run / applied / no_candidates / policy_required / provider_unsupported), warning counts, per-policy roll-up. |
| `/api/stats/compression-tuning` | Recommendation status, deltas, reason codes. `overrides` is empty in `recommend` mode today. |

### Provider cache counters

| Field | Meaning |
|-------|---------|
| `cache_counter_reported_requests` | Upstream payload included cache fields. |
| `cache_counter_unknown_requests` | Payload could not be parsed, or returned a shape EggPool does not recognize. The cache state is ambiguous; do NOT assume zero. |
| `cache_hit_ratio_known_only` | Deprecated compatibility alias for `provider_cache_hit_rate`. Computed as `cache_read_tokens / cache_eligible_input_tokens` (protocol-aware: OpenAI denominator is `prompt_tokens`; Anthropic denominator is `input + cache_read + cache_creation`). |

The `/cache` summary surfaces provider cache counters and EggPool cache
annotations on separate cards. Missing provider cache counters are
**not** cache misses and do not prove the upstream is uncached; they
just mean the upstream did not surface them. EggPool never disables
provider-side caching.

### Segmentation

| Field | Meaning |
|-------|---------|
| `by_status.segmented` | Request was segmented into stable_prefix / semi_stable_context / volatile_suffix. |
| `by_status.empty_request` | Request had no parseable content. |
| `by_status.parse_failure` | Payload could not be parsed. |
| `token_totals.stable_prefix` | Sum of estimated tokens in stable prefix regions. |
| `token_totals.semi_stable_context` | Sum of estimated tokens in semi-stable context. |
| `token_totals.volatile_suffix` | Sum of estimated tokens in volatile suffix. |
| `protected_requests` | Requests with at least one protected segment. |
| `compressible_candidate_requests` | Requests with at least one `compressible_candidate=True` segment. |

Two hashes coexist:

- `stable_prefix_shape_hash` (a.k.a. the legacy `stable_prefix_hash`) — structural descriptor hash.
- `stable_prefix_content_hash` — exact SHA-256 of canonical stable-prefix content, re-extracted from the payload via stable-prefix segment paths. Safe compression recomputes this on the transformed payload before sending upstream.

### Compression

| Field | Meaning |
|-------|---------|
| `observed_requests` | Number of requests the analyzer ran over. |
| `candidate_count` | Volatile-suffix candidates detected. |
| `eligible_count` | Candidates that exceeded `min_candidate_tokens` and `min_savings_tokens` after reason code analysis. |
| `suppressed_count` | `candidate_count - eligible_count`. |
| `estimated_savings_tokens` | Projected tokens saved if safe mode were applied. |
| `top_reason_codes` | Most common suppression reasons. |

Top reason codes:

| Code | Meaning |
|------|---------|
| `below_min_candidate_tokens` | Candidate was smaller than `min_candidate_tokens`. |
| `below_min_savings_tokens` | Projected savings were below `min_savings_tokens`. |
| `placement` | Segment was outside the configured placement (`suffix_only` / `after_cache_boundary` / `anywhere`). |
| `protected_cache_boundary` | Segment was within a protected cache boundary (`respect_cache_boundaries = true`). |
| `static_prefix` | Segment was in `stable_prefix` (only relevant if `compress_static_prefix = true`, which is rejected by default). |
| `latency_budget_exceeded` | Estimated runtime would exceed `max_compression_latency_ms`. |
| `transform_disabled` | The transform that would apply to this segment is disabled. |
| `empty_segment` | Segment contained no text. |

### Compression — safe-mode details

| Field | Meaning |
|-------|---------|
| `applied_count` | Number of requests actually mutated. |
| `failed_fallback_count` | Number of requests the applier backed out of because the stable-prefix hash did not match. |
| `candidate_count` | Number of volatile-suffix candidates the applier saw. |
| `estimated_savings_tokens` | Sum of projected savings across applied requests. |
| `actual_savings_tokens` | Sum of realized savings across applied requests. |
| `cache_safety.preserved_count` | Requests where the stable-prefix content hash was preserved. |
| `cache_safety.mismatch_count` | Requests where the hash did not match (should stay at zero). |
| `latency_ms.p50` / `p95` / `max` | Compression latency percentiles. |
| `transforms.<name>.applied` | Per-transform applied count. |
| `transforms.<name>.tokens_saved` | Per-transform realized token savings. |

Top runtime warnings:

| Warning | Meaning |
|---------|---------|
| `stable_prefix_hash_mismatch` | Stable-prefix content hash on the transformed payload did not match the original. Request sent uncompressed. |
| `latency_budget_exceeded` | Applier exceeded `max_compression_latency_ms`. Segment was not transformed. |
| `cache_safety_diff_failed` | Reserved for synthetic-cache structural-diff failures (see below). |

### Policy rollups

| Field | Meaning |
|-------|---------|
| `policy_counts[0]` (sentinel) | `<global>` row — counts that did not match any policy override. |
| `policy_counts[i].policy_source` | `"global"` or `"policy:<name>"`. |
| `policy_counts[i].requests` | Number of requests that matched this policy. |
| `policy_counts[i].warning_count` | Number of warnings emitted while resolving this policy. |

Match semantics: union OR across match fields; matched overrides are merged in file order; last-match-wins for scalars; field-by-field merge for `transforms`.

### EggPool cache annotations

Status codes:

| Status | Meaning |
|--------|---------|
| `disabled` | `[cache.synthetic_cache_controls] enabled = false`. No candidate selection. |
| `policy_required` | `require_policy = true` and no matching `[[compression.policies]]` row set `synthetic_cache_*` fields. |
| `provider_unsupported` | Selected provider kind is not in `provider_kinds` (default `["anthropic"]`). |
| `no_candidates` | Stable prefix was below `min_stable_tokens`, or placements excluded available stable blocks, or payload shape was unsupported. |
| `dry_run` | A plan was computed but no mutation occurred (because `dry_run = true`). |
| `applied` | Mutator added `cache_control` keys to the provider-bound payload. |
| `failed_fallback` | Structural-diff safety rejected an unexpected mutation; original payload was preserved. |

Warning codes:

| Warning | Meaning |
|---------|---------|
| `synthetic_cache_control_disabled` | `[cache.synthetic_cache_controls] enabled = false`. |
| `synthetic_cache_control_policy_required` | `require_policy = true` and no matching policy row. |
| `synthetic_cache_control_provider_unsupported` | Selected provider kind not in `provider_kinds`. |
| `synthetic_cache_control_no_stable_candidate` | No eligible stable-prefix container after segmentation. |
| `synthetic_cache_control_below_min_tokens` | Stable prefix total tokens below `min_stable_tokens`. |
| `synthetic_cache_control_limit_reached` | Would exceed `max_breakpoints`. |
| `synthetic_cache_control_synthesized` | Mutator added at least one `cache_control` key. |
| `synthetic_cache_control_existing_native_preserved` | Native `cache_control` already covered the candidate container. |
| `synthetic_cache_control_safety_diff_failed` | Structural-diff safety rejected an unexpected mutation. |
| `synthetic_cache_control_payload_not_mapping` | Payload was not a mapping; selector skipped. |

### Tuning suggestions

| Field | Meaning |
|-------|---------|
| `recommendations[i].status` | `"issued"` / `"cooldown"` / `"insufficient_data"`. |
| `recommendations[i].delta` | Per-threshold proposed change (`min_candidate_tokens`, `min_savings_tokens`, `max_compression_latency_ms`). |
| `recommendations[i].reason_codes` | Why this recommendation was issued (see `docs/cache-compression-profiles.md` § Profile 6). |
| `overrides` | Always empty in `recommend` mode today. `apply_runtime_override()` is reserved for a future lifecycle task. |

## Dispatch timing fields

Fine-grained timing spans are recorded per-request and exposed via
`/api/stats/runtime` under `dispatch_spans`. Each span reports
p50/p95/max in milliseconds over a rolling 200-sample window. Spans
are sparse: if a phase did not run, the span is absent from the
snapshot.

### Request-path spans

| Span key | What it measures |
|----------|-----------------|
| `body_read_ms` | `read_body_limited()` -- reading the raw request body |
| `json_parse_ms` | `jsonx_loads(body)` -- JSON deserialization (via `eggpool.jsonx`) |
| `model_parse_ms` | Provider-suffix parsing and known-provider lookup |
| `context_limit_ms` | Original body context-limit enforcement |
| `transcode_preflight_ms` | `_prepare_transcode_preflight()` and translated context-limit checks |
| `compression_policy_ms` | `resolve_compression_policy()` |
| `segmentation_ms` | `segment_request()` |
| `compression_analyze_ms` | `analyze_compression()` (observe mode only) |
| `compression_apply_ms` | `apply_safe_compression()` (safe mode only) |
| `context_build_ms` | `ProxyRequestContext` construction and model rewrite |
| `coordinator_pre_upstream_ms` | Full coordinator pre-upstream work (routing, persistence, dispatch) |

### Coordinator-internal spans

These spans measure work inside `RequestCoordinator._select_and_persist_attempt()`:

| Span key | What it measures |
|----------|-----------------|
| `thinking_classification_ms` | Thinking/reasoning requirement classification |
| `reservation_estimate_ms` | Token reservation estimation |
| `routing_plan_ms` | `Router.build_routing_plan()` |
| `account_lookup_ms` | Account ID resolution |
| `db_write_request_ms` | `INSERT INTO requests` |
| `db_write_reservation_ms` | `INSERT INTO reservations` |
| `db_write_attempt_ms` | `INSERT INTO request_attempts` |
| `routing_trace_build_ms` | Routing decision trace construction |
| `routing_trace_write_ms` | Routing decision trace persistence |
| `runtime_publication_ms` | Active request count and reservation publication |

### Interpreting compression spans

- **Observe mode**: `compression_analyze_ms` is populated; `compression_apply_ms` is absent or zero. The analyzer walks segments and estimates savings without mutating the payload.
- **Safe mode, no transform**: `compression_apply_ms` is populated (but typically small). The applier discovers no eligible transforms, skips copy-on-write, and returns the original payload. `compression_analyze_ms` is absent because the separate observe analyzer is not called.
- **Safe mode, transform applied**: `compression_apply_ms` is populated and includes discovery, path-level copy, and apply stages.
- **Compression disabled**: neither `compression_analyze_ms` nor `compression_apply_ms` appear. `segmentation_ms` may also be absent if no compression or observability feature requires segmentation.

### Interpreting coordinator spans

- `selection_claim_wait` and `selection_claim_held` are the authoritative timing spans for the two narrow claim-lock acquisitions. SQLite request/reservation/attempt creation runs outside the lock. Pure computation (thinking classification, reservation estimation, routing plan) also runs outside the lock.
- `routing_plan_ms` reflects the cost of `Router.build_routing_plan()`. This runs outside the lock on the precomputed `ProxyRequestContext`.
- `routing_trace_build_ms` and `routing_trace_write_ms` are absent when `[routing.trace].mode = "off"` or the sample was not selected.

---

## Common symptoms

### Symptom: compression never applies

`applied_count` stays at zero even though `candidate_count > 0`.

**Check:**

1. `[compression] enabled = true` and `mode = "safe"` (not `"observe"`).
2. Request has volatile suffix candidates — confirm `candidate_count > 0` in `/api/stats/compression-observability`.
3. Candidates exceed `min_candidate_tokens` and `min_savings_tokens` — check `top_reason_codes`.
4. At least one transform is enabled under `[compression.transforms]`.
5. Stable-prefix preservation did not fail closed — `failed_fallback_count` should be zero.
6. Context-limit check did not reject before compression — check the request log for `ContextLimitExceededError`.
7. A matching `[[compression.policies]]` row did not set `mode = "observe"` or `enabled = false`.

**Most likely root cause:** `mode = "observe"` in either `[compression]` or a matching policy row.

### Symptom: observe mode sees candidates but safe mode does not mutate

`candidate_count` is healthy but `applied_count` is zero.

**Check:**

1. The matching policy's `mode` is `"safe"`, not `"observe"`.
2. Candidate paths resolve to string leaves (the analyzer requires string content for transforms).
3. Latency budget not exceeded — `latency_budget_exceeded` warning absent in `/api/stats/compression-runtime`.
4. Placement is `suffix_only` (or `after_cache_boundary` if that matches your shape) and the candidate segment is in the volatile suffix.
5. Transform-specific thresholds — e.g., `compact_logs` requires log markers; `elide_base64_blobs` requires base64 markers. The analyzer emits `transform_disabled` if no transform matches the candidate shape.
6. Per-policy `min_candidate_tokens` and `min_savings_tokens` are not higher than the global ones.

**Most likely root cause:** `mode = "observe"` left in a policy override (Profile 2 → Profile 3 migration); or a transform-specific reason code that the global config ignores.

### Symptom: synthetic cache controls show `provider_unsupported`

`status_counts.provider_unsupported > 0` in `/api/stats/synthetic-cache-observability`.

**Check:**

1. Selected provider kind is Anthropic. Confirm via `eggpool accounts explain --model <id> --provider <anthropic-provider>`.
2. Target protocol is Anthropic after routing/transcoding. Check `[transcoder] prefer_native = true` and confirm the routed account's `provider.protocols` include `"anthropic"`.
3. Request actually routes to an Anthropic provider. Inspect the request log or `routing_decisions` for the model and provider.
4. `provider_kinds` includes `"anthropic"` (default).
5. Policy matches post-route provider kind — `match_provider_kinds = ["anthropic"]` is a post-route matcher and only fires after `RequestCoordinator._apply_synthetic_cache_controls` sees the post-transcode `upstream_protocol`.

**Most likely root cause:** the request is not actually routing to an Anthropic provider, or `match_provider_kinds` was set in a policy that does not match the resolved provider kind.

### Symptom: synthetic cache controls show `policy_required`

`status_counts.policy_required > 0`.

**Check:**

1. `require_policy = true` means a matching `[[compression.policies]]` row must set `synthetic_cache_*` fields.
2. Provider-specific matchers (`match_provider_ids`, `match_provider_kinds`, `match_models`) only fire post-route. Pre-route resolution (which would say `policy_required`) does NOT see those fields.
3. Client / model / protocol matches use exact strings or glob patterns from `match_clients` / `match_protocols` / `match_requested_models`.

**Most likely root cause:** the policy row exists but its `synthetic_cache_controls = true` line is missing, or the match fields do not match the request. Re-run `eggpool check-config` to surface Pydantic validation errors.

### Symptom: synthetic cache controls show `no_candidates`

`status_counts.no_candidates > 0`.

**Check:**

1. Stable prefix is below `min_stable_tokens`. Inspect `/api/stats/canonical-request-segmentation` — `token_totals.stable_prefix` is the projected total.
2. Placements exclude available stable blocks. Default is `("system", "tools")` — system blocks and tool schemas only. If your stable prefix lives in developer messages, the selector will skip.
3. Native `cache_control` annotations already cover the candidate containers — the selector skips rather than duplicates.
4. Payload shape is unsupported — segmentation emitted `parse_failure` or `empty_request`. Inspect the request status.

**Most likely root cause:** `min_stable_tokens` is set higher than the actual stable prefix size, or the stable prefix is in a placement not listed under `placements`.

### Symptom: `failed_fallback`

`failed_fallback_count > 0` in `/api/stats/compression-runtime`, or `status_counts.failed_fallback > 0` in `/api/stats/synthetic-cache-observability`.

**Check:**

1. **Compression:** `stable_prefix_hash_mismatch` warning is present in `/api/stats/compression-runtime` warnings. A transform mutated stable-prefix content. This should be zero; growth indicates a path-resolution bug in the segmenter or a transform that crossed a protected boundary.
2. **Synthetic cache:** `synthetic_cache_control_safety_diff_failed` warning is present. `_validate_synthetic_cache_diff` rejected an unexpected mutation:
   - cache_control added on a non-candidate container
   - non-cache_control additions
   - removals or changes

**Action:**

- Treat any growth in `failed_fallback_count` as a bug report. The fail-closed path preserves the original payload and emits a structured warning, so the request still succeeds — but the operator should investigate before continuing in apply mode.
- File a regression test under `tests/unit/test_replay_fixtures_regression.py` if a new payload shape reproduces the failure.

### Symptom: routing seems uneven

One account or one provider is getting a disproportionate share of requests.

**Important:** cache, compression, synthetic-cache, and tuning metrics do NOT affect routing. Confirm via:

1. `GET /api/stats/runtime` — `routing_runtime.guardrails.routing_uses_cache_metrics`, `routing_uses_compression_metrics`, `routing_uses_synthetic_cache`, `routing_uses_compression_tuning` should all read `false`.
2. `tests/unit/test_routing_guardrails.py` — same-provider fairness under adversarial cache/compression metrics is asserted across 5 scenarios.

**Likely root causes:**

- Health state. Inspect `eggpool accounts status` and `/api/backoffs` for cooldown / circuit-open accounts.
- Active request count. Accounts with low active count are picked more often by the `QuotaFairScorer` rotor.
- Quota windows. Per-window utilization (`util_5h`, `util_7d`, `util_30d`) drives the score. Inspect `/api/stats/routing/eligibility` or `eggpool accounts explain --gates --model <id>`.
- Model eligibility. The routed model must be supported by the candidate account. Confirm via `eggpool accounts explain --model <id> --provider <id>`.
- Catalog staleness. If `models_endpoint.method = "DISABLED"` but `static_models` is missing for a provider, the account may not surface the requested model.
- Account refresh staleness. Failed / partial / empty refreshes do not de-pool an account under default `catalog_withdrawal_policy = "preserve_until_health"`, so a stale-but-healthy account may keep traffic.
- `routing_priority` and `weight`. Higher `routing_priority` tiers are preferred; lower tiers are only reached via `exclude_accounts` retry paths. Within the selected eligible tier, weight is a relative request/token capacity hint (`2.0` is approximately twice the capacity of `1.0`); it is not cost weighting or a hard limit.

**What is NOT a routing cause:**

- Provider cache hit rate, even on a per-account basis.
- Compression savings, even on a per-account basis.
- Synthetic-cache apply rate.
- Tuning recommendation state.

A future cache-aware routing mode would require an explicit `routing.cache_aware = true` config flag plus per-provider support detection, a cost model using cached-token prices, backtesting, per-client opt-in, and dashboard warnings. The current routing guardrails deliberately do NOT implement it.

### Symptom: tuning recommendations not appearing

`recommendations` is empty or only shows `insufficient_data`.

**Check:**

1. `[compression.tuning] enabled = true`.
2. `mode = "recommend"` (not `"off"`).
3. Enough traffic to fill `min_window_requests` (default 50). Recommendations are only emitted when the window has at least this many finalized requests.
4. `update_interval_s` (default 300) has elapsed since the last recommendation. Cooldown is `cooldown_s` (default 900).
5. `persist_recommendations = true` (default) — false would skip persistence and only write to in-memory state.

**Most likely root cause:** insufficient traffic for `min_window_requests`, or `[compression.tuning]` is `enabled = false`.

### Symptom: dashboard cards show no data

Cards under `/cache` render but show empty / `0` counts.

**Check:**

1. The cache-compression stack runs only on data-plane requests (`/v1/chat/completions`, `/v1/messages`). Health checks and dashboard fetches do not produce request records.
2. Confirm via `GET /api/stats/cache-observability` — `total_requests` should be non-zero.
3. If `[dashboard] public = true`, the dashboard still requires an API key header (`Authorization: Bearer ...` or `x-api-key: ...`) for runtime metrics (always auth-gated regardless of public / private). Query-string authentication (`?api_key=...`) is **not** supported.
4. Time window — the cards render the live snapshot, not a rolling period. Use the JSON endpoints with the `?period=` parameter for historical rollups.

**Most likely root cause:** the dashboard is showing a fresh install with no data-plane traffic yet. Send a few requests through EggPool and refresh.

---

## Diagnostic commands

```bash
# Validate config without starting the service
eggpool check-config

# Inspect per-account routing eligibility for a model
eggpool accounts explain --model claude-sonnet-4 --provider <id> --gates

# Per-provider compression stats
curl -s -H "x-api-key: $EGGPOOL_API_KEY" "http://127.0.0.1:11300/api/stats/compression-observability" | jq

# Per-policy rollup
curl -s -H "x-api-key: $EGGPOOL_API_KEY" "http://127.0.0.1:11300/api/stats/compression-policies" | jq

# Synthetic cache status
curl -s -H "x-api-key: $EGGPOOL_API_KEY" "http://127.0.0.1:11300/api/stats/synthetic-cache-observability" | jq

# Routing guardrails
curl -s -H "x-api-key: $EGGPOOL_API_KEY" "http://127.0.0.1:11300/api/stats/runtime" | jq '.routing_runtime.guardrails'

# Cache counters
curl -s -H "x-api-key: $EGGPOOL_API_KEY" "http://127.0.0.1:11300/api/stats/cache-observability" | jq

# Segmentation rollup
curl -s -H "x-api-key: $EGGPOOL_API_KEY" "http://127.0.0.1:11300/api/stats/canonical-request-segmentation" | jq

# Tuning recommendations
curl -s -H "x-api-key: $EGGPOOL_API_KEY" "http://127.0.0.1:11300/api/stats/compression-tuning" | jq
```

## See also

- `docs/cache-compression.md` § Dashboard cards / API endpoints overview
- `docs/cache-compression-profiles.md` § Profile combinations and migration
- `architecture/README.md` § Routing Guardrails and Non-Interference — invariant documentation
- `tests/unit/test_routing_guardrails.py` — regression tests for the routing invariant
- `tests/unit/test_replay_fixtures_regression.py` — fail-closed fallback regression
