# Phase 10 Plan: Closed-Loop Threshold Tuning for Compression Policy

Date: 2026-07-02

Parent roadmap: `plans/cache_preserving_deterministic_compression_roadmap.md`

Depends on:

- Phase 4 observe-mode compression accounting
- Phase 5 safe suffix compression
- Phase 6 policy controls
- Phase 7 dashboard/runtime views
- Phase 8 routing guardrails

## Summary

Phase 10 adds optional closed-loop tuning for compression thresholds. The goal is to let EggPool adjust conservative knobs such as `min_candidate_tokens`, `min_savings_tokens`, and `max_compression_latency_ms` based on recent local observations.

This is not model-learning, semantic compression, or routing optimization. It is bounded control-loop tuning of existing deterministic compression policy thresholds.

Core invariant:

> Closed-loop tuning may adjust whether safe deterministic suffix compression runs, but it must never change routing, never enable stable-prefix compression, never add new transforms, and never inspect raw prompt content.

The initial mode should be recommendation-only. Automatic application should require explicit opt-in.

## Non-goals

- Do not tune provider/account routing.
- Do not tune model selection.
- Do not tune provider cache-control synthesis.
- Do not use raw prompt text, embeddings, or learned classifiers.
- Do not enable compression if the operator disabled it globally or by policy.
- Do not override `compress_static_prefix=false`.
- Do not tune transform semantics; only tune thresholds/latency budget within safe bounds.

## Tunable parameters

Initial tunables:

- `min_candidate_tokens`
- `min_savings_tokens`
- `max_compression_latency_ms`
- optional transform enable/disable recommendation only, not auto-apply in the first pass

Do not auto-tune:

- `mode`
- `enabled`
- `placement`
- `respect_cache_boundaries`
- `compress_static_prefix`
- synthetic cache controls
- routing scorer weights

## Modes

Add a tuning mode under `[compression.tuning]`.

```toml
[compression.tuning]
enabled = false
mode = "recommend" # off | recommend | apply
window_requests = 500
min_window_requests = 50
update_interval_s = 300
max_adjustment_pct = 25
cooldown_s = 900
persist_recommendations = true
```

Mode semantics:

- `off`: no tuning analysis.
- `recommend`: compute suggested threshold changes and expose them in dashboard/API; do not change request behavior.
- `apply`: apply bounded threshold changes to the resolved policy at runtime; never persist into config file automatically.

Prefer `recommend` as the first implementation milestone, with `apply` behind explicit config and tests.

## Feedback signals

Use only metrics already produced by prior phases:

- observe candidate counts,
- estimated savings tokens,
- safe applied counts,
- actual savings tokens,
- below-threshold suppression counts,
- latency-budget warnings,
- fallback counts,
- stable-prefix mismatch warnings,
- per-transform applied/savings counts,
- resolved policy name/source.

Do not read raw prompts or transformed text.

## Tuning goals

Per resolved policy, compute local recommendations that aim for:

- high positive token savings for applied transforms,
- low latency budget violations,
- zero or near-zero fail-closed fallback rate,
- reduced no-op analyzer/apply overhead on small requests,
- stable behavior over time rather than oscillating thresholds.

Suggested default target guardrails:

```toml
[compression.tuning.targets]
max_latency_budget_warning_rate = 0.01
max_failed_fallback_rate = 0.001
min_positive_savings_rate = 0.8
min_median_savings_tokens = 512
max_p95_latency_ms = 25
```

## Algorithm

Keep the initial algorithm simple, explainable, and bounded.

For each policy window:

1. Gather recent requests for that policy.
2. Require `min_window_requests` before generating recommendations.
3. Compute:
   - candidate rate,
   - applied rate,
   - median/p95 compression latency,
   - median/p95 savings tokens,
   - below-min-candidate suppression count,
   - below-min-savings suppression count,
   - latency-budget warning rate,
   - failed-fallback rate.
4. If fallback rate exceeds target, recommend disabling safe application or increasing thresholds; never lower thresholds.
5. If latency warnings exceed target, recommend increasing `min_candidate_tokens`, increasing `min_savings_tokens`, or lowering `max_compression_latency_ms` only if the current budget is above policy default. Prefer increasing thresholds over lowering latency budget.
6. If candidate rate is high but savings rate is low, recommend increasing `min_savings_tokens`.
7. If many requests are suppressed by `below_min_candidate_tokens` but observe estimates show strong savings above `min_savings_tokens`, recommend lowering `min_candidate_tokens` by at most `max_adjustment_pct`.
8. If safe applied requests consistently show high savings and low latency, recommend modestly lowering thresholds to catch more candidates.
9. Apply cooldown to avoid oscillation.
10. Clamp every recommendation to policy-defined min/max bounds.

## Bounds

Add hard-coded or config-defined bounds:

```toml
[compression.tuning.bounds]
min_candidate_tokens_min = 256
min_candidate_tokens_max = 16384
min_savings_tokens_min = 128
min_savings_tokens_max = 8192
max_compression_latency_ms_min = 5
max_compression_latency_ms_max = 100
```

Never allow tuning to produce unsafe values.

## Data model

Add a recommendation model:

```python
@dataclass(frozen=True, slots=True)
class CompressionTuningRecommendation:
    policy_name: str
    status: str # insufficient_data | recommended | applied | suppressed
    window_request_count: int
    current: dict[str, int | float]
    recommended: dict[str, int | float]
    reason_codes: tuple[str, ...]
    metrics: dict[str, int | float]
    generated_at: datetime
```

Reason codes:

- `insufficient_data`
- `high_latency_warning_rate`
- `high_fallback_rate`
- `low_positive_savings_rate`
- `strong_savings_low_latency`
- `below_candidate_threshold_suppression_high`
- `below_savings_threshold_suppression_high`
- `cooldown_active`
- `bounded_by_min`
- `bounded_by_max`
- `recommendation_only`
- `applied_runtime_override`

## Runtime application model

If `mode = "apply"`, do not modify config files. Keep runtime overrides in memory:

```python
@dataclass(frozen=True, slots=True)
class RuntimeCompressionPolicyOverride:
    policy_name: str
    fields: Mapping[str, int | float]
    generated_at: datetime
    expires_at: datetime | None
    reason_codes: tuple[str, ...]
```

Policy resolution order should become:

1. global config,
2. matching static policy overrides,
3. runtime tuning override, if enabled and not expired.

Runtime tuning must not override safety fields such as mode, enabled, placement, respect_cache_boundaries, or compress_static_prefix.

## Implementation tasks

### 1. Add tuning config models

Add `CompressionTuningConfig`, target config, and bounds config.

Validate:

- mode in `off|recommend|apply`,
- windows positive,
- cooldown positive,
- max adjustment percentage reasonable,
- min bounds <= max bounds.

### 2. Add tuning service

Create `src/eggpool/transcoder/compression/tuning.py` or an equivalent stats service module.

Responsibilities:

- Query recent compression stats by policy.
- Compute recommendations.
- Optionally produce runtime overrides.
- Never inspect raw content.
- Never raise in request path.

Prefer running tuning in an existing background task if one exists. If not, compute recommendations lazily in dashboard/API first, then add background application later.

### 3. Add persistence

Recommendation persistence can be lightweight.

Options:

- Store latest recommendation JSON in a small table keyed by policy name.
- Or expose recommendations from in-memory background task state.

Prefer persistence if dashboard should survive restart:

```sql
CREATE TABLE IF NOT EXISTS compression_tuning_recommendations (
    policy_name TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    recommendation_json TEXT NOT NULL,
    generated_at TEXT NOT NULL
);
```

Do not persist raw request content.

### 4. Add dashboard/API

Add endpoint:

```text
GET /api/stats/compression-tuning
```

Response should show:

- mode,
- per-policy recommendation,
- current thresholds,
- recommended thresholds,
- reason codes,
- window metrics,
- whether runtime override is active.

Add dashboard card:

- “Compression tuning: off/recommend/apply.”
- “Recommended changes.”
- “Why.”
- “Safety blockers.”

### 5. Integrate runtime override with policy resolver

If `apply` mode is implemented:

- Add an in-memory override registry.
- Merge override after static policy resolution.
- Include runtime override metadata in resolved policy summary.
- Expire overrides after cooldown/window if no longer supported by metrics.

### 6. Tests

Add tests for recommendation-only mode first. Add apply-mode tests only when runtime override is implemented.

## Test plan

### Unit tests: recommendation algorithm

- Insufficient data returns `insufficient_data`.
- High fallback rate recommends raising thresholds or disabling application recommendation.
- High latency warning rate recommends raising thresholds.
- Strong savings/low latency recommends lowering thresholds within bounds.
- Low positive savings recommends raising `min_savings_tokens`.
- Recommendations are clamped by bounds.
- Cooldown suppresses oscillating changes.
- Max adjustment percent enforced.

### Unit tests: no unsafe tuning

- Does not tune `enabled`.
- Does not tune `mode`.
- Does not tune `placement`.
- Does not tune `respect_cache_boundaries`.
- Does not tune `compress_static_prefix`.
- Does not tune routing fields.

### Integration tests: policy resolver

- Recommend mode does not change resolved policy.
- Apply mode changes only allowed threshold fields.
- Runtime override expires or is replaced deterministically.
- Resolved policy records override metadata.

### API/dashboard tests

- Tuning endpoint returns stable empty state.
- Per-policy recommendation visible.
- No raw content exposed.
- Apply mode indicates active runtime override.

### Routing non-regression

- Tuning recommendations do not change provider/account selection.
- Apply-mode threshold changes do not trigger reroute.

## Acceptance criteria

- Tuning is disabled by default.
- Recommendation mode produces explainable per-policy threshold suggestions.
- Recommendations use only aggregate metrics, never raw prompts/tool output.
- Apply mode, if implemented, can only adjust bounded threshold fields.
- Tuning never enables stable-prefix compression.
- Tuning never changes routing.
- Dashboard/API expose recommendations and reasons.
- Tests cover bounds, cooldown, unsafe-field exclusion, and routing non-interference.
- Full tests, ruff, and pyright pass.

## Rollback notes

Disable tuning:

```toml
[compression.tuning]
enabled = false
mode = "off"
```

If apply mode causes unexpected behavior, switch to recommendation-only:

```toml
[compression.tuning]
enabled = true
mode = "recommend"
```

Runtime overrides should be in-memory or separately persisted so they can be cleared without editing operator config.