# Model Info Phase 5: Benchmark Sources, Model Cards, Detail UI, and Hardening

## Objective

Add richer model-information sources for benchmarks and model-card metadata, then harden conflict handling, provenance display, and source lifecycle behavior. This phase introduces optional Artificial Analysis and Hugging Face integrations, benchmark-specific canonical fields, detail-page rendering, and final verification that model-info remains advisory and non-disruptive.

At the end of this phase, Eggpool should provide a useful model-detail experience: compact summaries on model rows, source/provenance details, benchmark observations where available, clear sparse/new/conflict states, and robust behavior when external sources are missing, delayed, rate-limited, or disagree.

## Source goals

Artificial Analysis should be the preferred structured benchmark/performance source when configured. Treat it as optional and likely API-key-gated.

Hugging Face should enrich open-weight/open-source model metadata through model cards and datasets when safely matched. Treat it as model-card metadata, not verified benchmark truth unless consuming a structured leaderboard dataset with provenance.

OpenRouter remains a broad metadata/pricing-adjacent source from phase 3.

Provider-native observations remain the baseline source for all discovered models.

## Design constraints

Do not scrape web pages.

Do not make Artificial Analysis or Hugging Face required dependencies.

Do not insert benchmark-derived routing gates.

Do not imply sparse benchmark data means the model is unavailable or low quality.

Do not trust model-card claims as equivalent to independent benchmark results.

Do not expose long model-card text verbatim in the dashboard.

Do not use source-provided prose without escaping and summarizing.

## Optional benchmark table decision

If phase 4 detail APIs already need structured benchmark rows, add a relational table now:

```sql
CREATE TABLE IF NOT EXISTS model_info_benchmarks (
    model_id TEXT NOT NULL,
    source TEXT NOT NULL,
    benchmark_name TEXT NOT NULL,
    benchmark_version TEXT,
    score REAL,
    rank INTEGER,
    percentile REAL,
    observed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    source_model_id TEXT,
    notes TEXT,
    provenance_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (model_id, source, benchmark_name, benchmark_version),
    FOREIGN KEY (model_id) REFERENCES models(model_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_model_info_benchmarks_model
    ON model_info_benchmarks(model_id);

CREATE INDEX IF NOT EXISTS idx_model_info_benchmarks_name
    ON model_info_benchmarks(benchmark_name);
```

If dashboard sorting/filtering by benchmark is not planned yet, keep benchmark observations inside `detail_json.benchmarks` and defer the relational table. This plan recommends adding the table only if the detail UI needs table filtering or benchmark-specific API queries.

## Artificial Analysis adapter

Create `src/eggpool/model_info/sources/artificial_analysis.py`.

Implement the same `ModelInfoSource` protocol.

Configuration:

```toml
[model_info.sources.artificial_analysis]
enabled = false
priority = 50
ttl_seconds = 86400
api_key_env = "ARTIFICIAL_ANALYSIS_API_KEY"
base_url = "https://api.artificialanalysis.ai"
```

Do not assume endpoint shape in code comments unless verified during implementation. Keep URL/path configurable through `options` if the API path or version changes:

```toml
[model_info.sources.artificial_analysis.options]
models_path = "/v1/models"
benchmarks_path = "/v1/benchmarks"
```

Adapter responsibilities:

Fetch structured model/benchmark records.

Parse only documented stable fields.

Emit `SourceModelRecord` with `benchmarks` populated.

Record source_model_id and source timestamp.

Handle 401/403 as auth/source health errors, not app startup failures.

Handle 429 with source cooldown.

Do not store large raw benchmark payloads if `store_raw_observations = false`.

Benchmark normalization:

```text
benchmark_name: stable source benchmark label
benchmark_version: source version/date if available
score: numeric score if comparable
rank: leaderboard rank if available
percentile: percentile if source provides it
notes: short caveat, e.g. "Artificial Analysis intelligence index"
source: "artificial_analysis"
```

Canonical detail fields:

```json
{
  "benchmarks": [
    {
      "name": "Artificial Analysis Intelligence Index",
      "score": 72.1,
      "rank": 14,
      "source": "artificial_analysis",
      "observed_at": "..."
    }
  ],
  "benchmark_summary": {
    "source": "artificial_analysis",
    "label": "High general capability; benchmark source available",
    "observed_at": "..."
  }
}
```

Summary language should remain conservative:

```text
"Benchmark metadata available from Artificial Analysis; local runtime stats may differ."
```

Avoid overstating benchmark meaning, especially for new models or provider-hosted variants.

## Hugging Face adapter

Create `src/eggpool/model_info/sources/huggingface.py`.

Scope for phase 5:

Only attempt Hugging Face enrichment for models that appear likely open-weight/open-source by exact alias or configured mapping. Do not search the Hub by arbitrary model name in a way that could produce bad joins.

Configuration:

```toml
[model_info.sources.huggingface]
enabled = false
priority = 200
ttl_seconds = 604800
api_key_env = "HUGGINGFACE_TOKEN"
```

Optional aliases:

```toml
[[model_info.aliases]]
provider_id = "fireworks"
model_id = "llama-3.1-405b-instruct"
source = "huggingface"
source_model_id = "meta-llama/Llama-3.1-405B-Instruct"
confidence = "curated"
```

Adapter responsibilities:

Fetch model metadata through Hugging Face Hub API for exact source_model_id.

Parse license, tags, pipeline/task, library, downloads/likes if useful, model card presence, and card metadata.

Do not ingest long model card prose into canonical summary. Store compact metadata and a short derived note.

Emit model-card facts as source observations with `source = "huggingface"`.

Potential fields:

```text
license
tags
pipeline_tag
library_name
model_type
parameter_count if explicitly structured
base_model if explicitly structured
card_data keys if structured
```

Treat Hugging Face data as metadata, not independent benchmark truth, unless a structured leaderboard dataset is integrated separately.

## Leaderboard dataset option

If adding Open LLM Leaderboard or other Hugging Face-hosted benchmark datasets, implement it as a separate source adapter, not inside the model-card adapter:

```text
sources/hf_leaderboard.py
```

Reason: model cards and benchmark datasets have different provenance and reliability characteristics.

Only consume structured datasets through API/library endpoints. Do not scrape Spaces HTML.

Require exact source model ID matching or curated aliases.

## Identity and alias expansion

Phase 5 should refine alias management across all sources.

Add `[[model_info.aliases]]` to config if not already implemented.

Alias fields:

```toml
provider_id = "minimax"
model_id = "minimax-m3"
source = "openrouter"
source_model_id = "minimax/minimax-m3"
confidence = "curated" # exact | curated
notes = "Provider name differs from OpenRouter namespace"
```

Alias ingestion:

On startup, seed configured aliases into `model_info_aliases`.

Configured aliases should override auto-discovered aliases.

Ambiguous aliases should disable matching for that `(provider_id, model_id, source)` tuple and emit a warning.

Expose aliases on detail page so operators can debug missing external metadata.

## Reconciliation hardening

Add explicit field provenance for selected canonical fields:

```json
{
  "display_name": {
    "source": "provider_catalog",
    "confidence": 1.0,
    "observed_at": "..."
  },
  "benchmark_summary": {
    "source": "artificial_analysis",
    "confidence": 0.9,
    "observed_at": "..."
  },
  "license": {
    "source": "huggingface",
    "confidence": 0.7,
    "observed_at": "..."
  }
}
```

Add conflict categories:

```text
context_window
max_output_tokens
modality
pricing
family_identity
license
benchmark_identity
```

Conflict selection rules:

Provider/local effective limits remain selected for Eggpool display when the conflict concerns effective routing limits.

Artificial Analysis benchmark rows do not conflict with Hugging Face model-card claims unless both claim the same benchmark name/version with different numeric values.

Model-card license conflicts should be stored but should not affect routing or availability.

If source records disagree on family identity or model lineage, mark `conflicting` only if the display page would otherwise present one as canonical.

## Detail UI

If not already done in phase 4, add a model detail page:

```text
/model-info/{model_id:path}
```

Minimum sections:

Canonical summary:

```text
status, summary, sparse flag, last refreshed, next refresh
```

Provider/callability:

```text
provider IDs, aliases, effective local limits, discovered upstream limits
```

Benchmarks:

```text
source, benchmark name, score/rank/percentile, observed date, caveat
```

Metadata:

```text
modalities, tool support, family, release date, license, external IDs
```

Provenance:

```text
selected field -> source, confidence, observed date
```

Conflicts:

```text
field, source values, selected value, reason
```

Source observations:

```text
source, source_model_id, provider_id, observed_at, expires_at, confidence, raw hash
```

Do not display full raw JSON by default. If a debug view is needed, require auth and a query flag such as `?debug=1`, and cap size.

## Summary and tooltip improvements

Update deterministic summaries to use richer fields:

Cases:

New sparse model:

```text
"New model detected; metadata sparse. Eggpool will refresh external sources more frequently for now."
```

Benchmark available:

```text
"Benchmark metadata available from Artificial Analysis; local latency and reliability may differ."
```

Open-weight metadata available:

```text
"Open-weight model metadata available from Hugging Face; benchmark data not independently verified."
```

Conflict:

```text
"Metadata conflict detected for context window; Eggpool is using local/provider effective limits for display."
```

External source unavailable:

```text
"Cached metadata is available, but one or more external sources are currently unavailable."
```

Keep summaries short enough for dashboard tooltips. Detailed caveats belong on the detail page.

## Source health and rate-limit hardening

Extend source health to capture:

```text
failure_count
last_status_code
rate_limited_until
last_success_duration_ms
last_payload_count
```

If adding fields requires a migration, add them in phase 5.

Handle HTTP statuses:

401/403: mark source auth_failed, disable until config changes or manual refresh.

429: set cooldown from Retry-After if present; otherwise exponential backoff.

5xx/network: exponential backoff.

Malformed payload: source error, but do not poison existing observations.

Partial parse failures: record per-entry warnings and continue.

## Manual override support

Add field-level overrides if not already present:

```toml
[model_info.overrides."minimax-m3"]
summary = "Locally capped for cost/quality reasons."
context_note = "Configured local cap: 220k."
status = "manual_override"
```

Prefer conservative override scope:

```text
summary
family
display_name
notes
hide_benchmark_sources
```

Do not allow model-info overrides to change routing limits. Existing model/provider overrides already own effective context/pricing behavior.

External observations should still refresh underneath manual overrides.

## Tests

Artificial Analysis adapter tests:

`test_artificial_analysis_parses_benchmark_record`

`test_artificial_analysis_auth_error_records_source_health`

`test_artificial_analysis_rate_limit_sets_cooldown`

`test_artificial_analysis_disabled_without_api_key`

`test_artificial_analysis_benchmark_rows_are_provenanced`

Hugging Face adapter tests:

`test_huggingface_exact_alias_fetches_model_metadata`

`test_huggingface_refuses_unaliased_search_match`

`test_huggingface_parses_license_and_tags`

`test_huggingface_long_card_text_not_exposed_in_summary`

`test_huggingface_failure_preserves_cached_metadata`

Reconciliation tests:

`test_benchmark_summary_selected_from_artificial_analysis`

`test_model_card_metadata_does_not_override_benchmark_source`

`test_context_conflict_records_selected_provider_limit`

`test_license_conflict_does_not_affect_routing_status`

`test_manual_summary_override_preserves_external_observations`

Detail API/UI tests:

`test_model_info_detail_includes_benchmark_rows`

`test_model_info_detail_includes_aliases_and_conflicts`

`test_model_info_detail_escapes_source_text`

`test_model_info_detail_omits_raw_json_by_default`

`test_model_info_debug_raw_json_is_auth_gated_and_size_capped`

Source health tests:

`test_source_health_tracks_rate_limit`

`test_source_health_tracks_payload_count`

`test_source_health_redacts_error_messages_if_needed`

End-to-end tests:

`test_new_sparse_model_eventually_transitions_to_partial_when_external_data_missing`

`test_new_sparse_model_transitions_to_fresh_when_external_data_available`

`test_external_source_outage_does_not_change_v1_models_callability`

`test_router_does_not_consume_benchmark_metadata_for_eligibility`

## Manual verification

Configure a known OpenRouter alias and confirm metadata appears.

Configure Artificial Analysis with an API key and confirm benchmark observations populate for matched models.

Configure Hugging Face alias for an open model and confirm license/tags/card metadata appear.

Create an intentional context conflict and confirm the UI shows provider/local effective limit as selected.

Disable Artificial Analysis and confirm cached benchmark data remains visible as stale or partial rather than disappearing abruptly.

Trigger source 401/429/5xx conditions using mocked tests or local test server and confirm source health behavior.

Inspect `/v1/models` and confirm it remains compact.

Inspect dashboard model detail page and confirm no raw secrets or unescaped source text appear.

## Acceptance criteria

Artificial Analysis source can enrich matched models with benchmark observations when enabled.

Hugging Face source can enrich exactly matched open-weight models with model-card metadata when enabled.

All benchmark/model-card facts carry provenance.

Sparse-new models are not refreshed aggressively forever when external benchmark data never appears.

Conflicts are visible and field-wise, not destructive.

Manual overrides are field-level and do not stop external observation refresh.

Detail UI exposes canonical facts, benchmarks, aliases, provenance, conflicts, and refresh state.

Source health handles auth errors, rate limits, network errors, and malformed payloads without breaking routing or startup.

No web scraping is introduced.

No benchmark metadata is used as suppressive routing authority.
