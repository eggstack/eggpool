# Cache-Compression Profiles

Six copy-pasteable configuration profiles. Each profile is a complete, self-contained TOML snippet. Copy the relevant sections into your `config.toml` and run `eggpool check-config` then `eggpool rehash`.

Profiles are ordered from safest (Profile 1) to most aggressive (Profile 6). Always start with Profile 1, walk up after inspecting the dashboard.

## Profile 1 — Baseline / disabled

**Purpose:** preserve current behavior while collecting only existing request stats (Phase 1-4 observability runs by default).

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

**Behavior:**

- No request mutation from compression or synthetic cache controls.
- Phase 1-4 observability (cache counters, segmentation, transcoder cache stability, observe-mode compression accounting) is recorded.
- Routing is unaffected.

**When to use:** new installs, production deployments that have not yet evaluated cache-compression, or while validating baseline traffic patterns.

## Profile 2 — Observe-only diagnostics

**Purpose:** learn whether safe suffix compression would help without mutating requests.

```toml
[compression]
enabled = true
mode = "observe"
placement = "suffix_only"
respect_cache_boundaries = true
compress_static_prefix = false
min_candidate_tokens = 2048
min_savings_tokens = 1024

[compression.transforms]
fold_repeated_lines = true
compact_logs = true
compact_search_results = true
elide_base64_blobs = true
minify_machine_json = true
compact_stack_traces = true
```

**Behavior:**

- Analyzer runs on every request and records a `CompressionObservation` (Phase 4).
- No request mutation. The applier (`apply_safe_compression`) is a no-op.
- Durable counters persist per request: `compression_status`, `compression_mode`, candidate / eligible / suppressed counts, savings estimates, reason codes.

**Dashboard fields to watch:**

- `observed_requests` — number of requests the analyzer ran over.
- `candidate_count` and `eligible_count` — how many volatile-suffix candidates exceeded the thresholds.
- `estimated_savings_tokens` — projected token savings (not actual).
- `top_reason_codes` — top suppression reasons (`below_min_candidate_tokens`, `below_min_savings_tokens`, `placement`, `protected_cache_boundary`, etc.).

**JSON endpoint:** `GET /api/stats/compression-observability` exposes the same data plus per-provider, per-account, per-model breakdowns.

**When to use:** first evaluation pass; deploy Profile 2 for 24-48 hours and inspect the candidate rate and suppression reasons before deciding whether to enable safe mode.

## Profile 3 — Safe suffix compression for coding agents

**Purpose:** reduce large volatile tool / log / search output while preserving stable prompts and cache controls.

```toml
[compression]
enabled = true
mode = "safe"
placement = "suffix_only"
respect_cache_boundaries = true
compress_static_prefix = false
min_candidate_tokens = 1024
min_savings_tokens = 512
max_compression_latency_ms = 25

[compression.transforms]
fold_repeated_lines = true
compact_logs = true
compact_search_results = true
elide_base64_blobs = true
minify_machine_json = true
compact_stack_traces = true
```

Optional policy scoped to Opencode-like clients:

```toml
[[compression.policies]]
name = "opencode-safe-suffix"
match_clients = ["opencode"]
enabled = true
mode = "safe"
placement = "suffix_only"
min_candidate_tokens = 1024
min_savings_tokens = 512
```

**Behavior:**

- Safe mode actually mutates eligible `volatile_suffix` segments.
- Six transforms are available (set per-transform booleans under `[compression.transforms]`).
- Stable prefix is recomputed and verified via `stable_prefix_content_hash`; on mismatch the request is sent uncompressed with `failed_fallback=True` and a `stable_prefix_hash_mismatch` warning.
- This profile does not alter route selection and does not compress system / tool schema prefixes.

**Dashboard fields to watch:**

- `applied_count` — number of requests actually compressed.
- `failed_fallback_count` — should stay at or near zero; growth indicates a stable-prefix bug.
- `actual_savings_tokens` and `applied_p95_savings_tokens` — actual savings (versus `estimated_savings_tokens` from Profile 2).
- `applied_stable_prefix_preserved_count` and `applied_failed_fallback_count` — cache safety counters.
- Per-transform applied counts and tokens_saved.

**When to use:** after 24-48 hours of Profile 2 confirms a useful candidate rate. Start with one client/policy (the optional `[[compression.policies]]` row) before expanding globally.

## Profile 4 — Anthropic synthetic cache dry-run

**Purpose:** discover whether provider-bound Anthropic requests have stable prefix blocks that could be annotated.

```toml
[cache.synthetic_cache_controls]
enabled = true
dry_run = true
require_policy = true
ttl = "ephemeral"
min_stable_tokens = 1024
max_breakpoints = 4
placements = ["system", "tools"]

[[compression.policies]]
name = "anthropic-cache-dry-run"
match_provider_kinds = ["anthropic"]
synthetic_cache_controls = true
synthetic_cache_dry_run = true
synthetic_cache_min_stable_tokens = 1024
synthetic_cache_max_breakpoints = 4
```

**Behavior:**

- Post-route only: the selector and mutator run inside `RequestCoordinator._apply_synthetic_cache_controls` after account selection and provider-bound transcoding.
- Provider-bound Anthropic payload only: the selector sees the post-transcode `upstream_protocol`, so OpenAI clients routed to Anthropic providers are supported.
- No mutation in dry-run. The plan (candidate containers, breakpoint positions, projected placement) is recorded but the wire body is unchanged.
- `require_policy = true` means a matching `[[compression.policies]]` row must set `synthetic_cache_*` fields, otherwise requests get `policy_required`.

**Dashboard fields to watch (`/api/stats/synthetic-cache-observability`):**

- `status_counts.dry_run` — number of requests the dry-run selector annotated a plan for.
- `status_counts.no_candidates` — stable prefix was below `min_stable_tokens` or placements excluded available stable blocks.
- `status_counts.provider_unsupported` — selected provider kind is not in `provider_kinds` (default `["anthropic"]`).
- `status_counts.policy_required` — no matching policy row set `synthetic_cache_*` fields.
- `warning_counts.synthetic_cache_control_existing_native_preserved` — native `cache_control` annotations already covered the candidate containers.
- `by_policy` roll-up — confirms the policy row is matching.

**When to use:** after Profile 3 is stable, if you want to start experimenting with provider cache hints. Always use dry-run first; inspect candidate counts before applying.

## Profile 5 — Anthropic synthetic cache apply mode

**Purpose:** opt-in provider-bound mutation after dry-run confidence.

```toml
[cache.synthetic_cache_controls]
enabled = true
dry_run = false
require_policy = true
ttl = "ephemeral"
min_stable_tokens = 1024
max_breakpoints = 4
placements = ["system", "tools"]

[[compression.policies]]
name = "anthropic-cache-apply"
match_provider_kinds = ["anthropic"]
synthetic_cache_controls = true
synthetic_cache_dry_run = false
synthetic_cache_min_stable_tokens = 1024
synthetic_cache_max_breakpoints = 4
```

**Behavior and warnings:**

- Apply mode mutates the provider-bound payload by adding `cache_control` keys only. No fields are removed, no values are changed, and the selector only annotates stable-prefix containers.
- Native `cache_control` annotations are preserved byte-for-byte; no duplicate breakpoints are added at the same path.
- Volatile content is never annotated. The selector only considers `stable_prefix` segments whose source is `SYSTEM`, `DEVELOPER`, or `TOOL_SCHEMA`.
- Structural-diff safety (`_validate_synthetic_cache_diff`) rejects any unexpected mutation and falls back to the original payload with `failed_fallback=True`. Any unexpected change triggers the fallback path.
- Leave disabled or dry-run if provider behavior or billing is uncertain. Apply mode is the only cache-compression feature that has a non-zero chance of affecting upstream-side cost behavior.

**Dashboard fields to watch:**

- `status_counts.applied` — number of requests the mutator actually annotated.
- `warning_counts.synthetic_cache_control_safety_diff_failed` — should stay at zero; growth indicates a bug.
- `warning_counts.synthetic_cache_control_synthesized` — number of `cache_control` keys the mutator added (matched against `applied_count` × placements).
- `by_policy` — confirms the apply policy is matching.

**When to use:** only after Profile 4 dry-run shows clean candidate/applied dry-run counts and no `synthetic_cache_control_safety_diff_failed` warnings for 24-48 hours. Scope to one provider/client first.

## Profile 6 — Threshold tuning recommendation-only

**Purpose:** surface suggested threshold changes but never alter runtime policy.

```toml
[compression.tuning]
enabled = true
mode = "recommend"
window_requests = 500
min_window_requests = 50
update_interval_s = 300
max_adjustment_pct = 25
cooldown_s = 900
persist_recommendations = true
```

**Behavior:**

- The tuning engine observes Phase 4-6 compression metrics and emits recommendations for `min_candidate_tokens`, `min_savings_tokens`, and `max_compression_latency_ms`.
- Recommendations are advisory only. `mode = "recommend"` (the default) writes suggestions to the `compression_tuning_recommendations` table and the dashboard only.
- Every suggestion is content-private (no prompt inspection), bounded (clamped to `[compression.tuning.bounds]`), rate-limited (`max_adjustment_pct` per step; `cooldown_seconds` suppresses the next recommendation), and immutable on every other compression knob.
- `mode = "apply"` is currently dormant. The in-memory `RuntimeCompressionPolicyOverrideRegistry` and `apply_runtime_override` helper exist for forward compatibility, but no production code path registers entries today. A future supervised background task must wire the lifecycle before apply mode takes effect.

**Dashboard fields to watch (`/api/stats/compression-tuning`):**

- `recommendations` — list of recent recommendations with `status`, `delta`, and `reason_codes`.
- `windows` — per-policy window metrics.
- `overrides` — list of currently registered runtime overrides (always empty in `recommend` mode today).

**Recommendation reason codes:**

| Reason | Meaning |
|--------|---------|
| `insufficient_data` | Not enough finalized requests in the window yet. |
| `high_latency_warning_rate` | Latency-budget warnings are exceeding the target. Suggest lowering `max_compression_latency_ms`. |
| `high_fallback_rate` | `failed_fallback` count is exceeding the target. Suggest raising `min_savings_tokens` or `min_candidate_tokens`. |
| `low_positive_savings_rate` | Eligible candidates are not actually saving enough tokens. Suggest raising `min_savings_tokens`. |
| `below_candidate_threshold_suppression_high` | Many requests suppressed at the candidate threshold. Consider lowering `min_candidate_tokens` (still bounded). |
| `below_savings_threshold_suppression_high` | Many requests suppressed at the savings threshold. Consider lowering `min_savings_tokens` (still bounded). |
| `strong_savings_low_latency` | Healthy regime. Engine may try slight tuning to find a better point. |
| `cooldown_active` | The next recommendation is suppressed because `cooldown_s` has not elapsed. |
| `bounded_by_min` | A suggested value was clamped to the configured minimum. |
| `bounded_by_max` | A suggested value was clamped to the configured maximum. |
| `recommendation_only` | Every recommendation is tagged with this regardless of mode. |
| `applied_runtime_override` | Reserved for future apply-mode lifecycle. |

**When to use:** after Phase 5 safe compression is stable and you want data-driven threshold suggestions. Always start with `mode = "recommend"`; never set `mode = "apply"` today.

## Profile combinations

Profiles are composable. A typical production setup runs:

- **Profile 3** (safe suffix compression) globally.
- **Profile 6** (tuning recommendation-only) to surface threshold advice.
- A scoped `[[compression.policies]]` row that disables compression for a specific client (e.g., a benchmarking client that needs wire-faithful bodies):

```toml
[[compression.policies]]
name = "benchmark-no-compress"
match_clients = ["benchmark"]
enabled = false
```

The `enabled = false` overlay stops the analyzer and applier entirely for matching requests.

## Migration between profiles

Profiles are config-only changes. No schema migration is required:

1. Edit `config.toml` to the new profile.
2. Run `eggpool check-config` to validate.
3. Run `eggpool rehash` to restart the supervisor.
4. Inspect the relevant `/api/stats/...` endpoint to confirm the new mode is active.

Rollback to Profile 1 (baseline disabled) at any time — see `docs/cache-compression.md` § Rollback.

## See also

- `docs/cache-compression.md` — main operator guide, dashboard interpretation, privacy section
- `docs/cache-compression-troubleshooting.md` — symptom-to-cause guide
- `architecture/README.md` § Safe-Mode Suffix Compression (Phase 5), Compression Policy Overrides (Phase 6), Synthetic Cache Controls (Phase 9), Closed-Loop Threshold Tuning (Phase 10)
- `config.example.toml` § Phase 9 / Phase 10 commented blocks — equivalent verbose config