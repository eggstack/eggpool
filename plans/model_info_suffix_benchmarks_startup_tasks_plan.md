# Model-Info Suffix Matching, Benchmark Enrichment, and Startup Task Plan

## Context

Live testing now shows a materially improved model-info path:

- OpenRouter source health is healthy and fetched 343 records.
- Most catalog-visible models have OpenRouter observations.
- Most models now show `partial`, not `sparse`, proving the normalized/tiered identity work is functioning.
- The remaining sparse models are concentrated in naming variants such as `MiniMax-M2.7-highspeed` and a few provider models that still lack a resolvable OpenRouter match.
- All model-info rows still have `benchmark_count = 0`, and summaries say `Public benchmark metadata unavailable`.
- `model_info_source_health` only shows `provider_catalog` and `openrouter`; there is no `artificial_analysis` row, so benchmark enrichment is not running or not configured.
- Background task status shows several tasks such as `update_checker`, `checkpoint`, and `model_info_refresh` as never run. These likely need explicit startup/first-run behavior rather than waiting for the first interval.

This plan combines the next targeted fixes:

1. Safe deployment-suffix model matching, especially `-highspeed` variants.
2. Benchmark-source diagnostics and Artificial Analysis matching/enrichment audit.
3. Canonical provenance/detail consistency when external IDs exist.
4. Startup/first-run behavior for important background tasks.

## Live evidence

Current source health:

```text
source            enabled  last_success_at      failure_count  last_payload_count
openrouter        1        2026-07-08 14:17:25  0              343
provider_catalog  1        2026-07-08 17:16:55  0
```

There is no `artificial_analysis` row.

Most models are now `partial` with `openrouter` provenance and an `external_ids.openrouter` value. Example:

```text
minimax-m3   partial  sparse=0  sources=[provider_catalog, openrouter]  openrouter_id=minimax/minimax-m3  benchmark_count=0
```

Remaining highspeed rows:

```text
MiniMax-M2.1-highspeed  sparse_new  sources=[provider_catalog]  openrouter_id=NULL
MiniMax-M2.5-highspeed  sparse_new  sources=[provider_catalog]  openrouter_id=NULL
MiniMax-M2.7-highspeed  sparse_new  sources=[provider_catalog]  openrouter_id=NULL
```

Highspeed aliases are provider-catalog-only:

```text
MiniMax-M2.7-highspeed  provider_catalog  MiniMax-M2.7-Highspeed
MiniMax-M2.7-highspeed  provider_catalog  minimax/MiniMax-M2.7-highspeed
```

OpenRouter has the base model IDs, not the highspeed variants:

```text
minimax/minimax-m2.1
minimax/minimax-m2.5
minimax/minimax-m2.7
```

## Root-cause assessment

### 1. OpenRouter identity matching is mostly fixed

The presence of OpenRouter observations for most models means the dashboard and normalized/tiered matching path are now generally working.

### 2. Highspeed variants need a deployment-suffix alias tier

`highspeed` is a deployment/presentation suffix for MiniMax in this provider context, not a distinct benchmark/source model identity. Eggpool should be able to strip such suffixes conservatively and resolve to the base source model only when the stripped candidate is unique.

### 3. Benchmark metadata cannot land while Artificial Analysis is absent

The model-info classifier keeps models `partial` if benchmark/family/release information is missing. Artificial Analysis is disabled by default in config and is the source that currently parses benchmark observations. If AA is disabled, missing an API key, or failing to match, `benchmark_count` remains zero and summaries will continue to say benchmark metadata is unavailable.

### 4. AA matching is not yet tiered like OpenRouter

The service fetches AA into an index and calls a legacy `_resolve_aa_record()` path. It should either use the same tiered resolver as OpenRouter or at minimum emit diagnostics proving whether AA is disabled, unfetched, fetched-but-empty, fetched-but-unmatched, or matched-without-benchmarks.

### 5. Background tasks reporting “never ran” harms operator trust

Tasks such as `update_checker`, `checkpoint`, and `model_info_refresh` are operational health tasks. If the dashboard shows them as never run after startup, the system appears unhealthy even if they are merely waiting for their first interval. Selected tasks should run once on startup after their dependencies are ready, or the scheduler should explicitly record `next_run_at`/`startup_deferred` state.

## Goals

1. Resolve safe deployment suffix variants such as `MiniMax-M2.7-highspeed` to the base OpenRouter model when unique.
2. Preserve safety: never strip semantic tier/capability suffixes like `pro`, `mini`, `flash`, `lite`, `instruct`, `chat`, `reasoning`, `thinking`, or version/date tokens.
3. Add tests that prove `highspeed` collapses and unsafe variants do not.
4. Audit and improve Artificial Analysis benchmark source handling.
5. Add diagnostics that explicitly explain why benchmarks are unavailable.
6. Ensure key background tasks run once on startup or record an explicit deferred state.
7. Add tests for startup first-run behavior and background task status display.

## Non-goals

- Do not make `highspeed` stripping globally unconditional for all sources/providers if ambiguity exists.
- Do not collapse semantic model variants such as `pro`, `mini`, `flash`, `lite`, `instruct`, or date/version suffixes.
- Do not require Artificial Analysis API access in normal CI.
- Do not change model routability based on benchmark metadata.
- Do not make OpenRouter pricing authoritative for local cost accounting.

## Phase 1: Add safe deployment-suffix normalization

### Design

Add a small deployment-suffix variant generator in `src/eggpool/model_info/normalization.py` or `matching.py`.

Suggested safe suffix set:

```python
DEPLOYMENT_SUFFIX_TOKENS = frozenset({
    "highspeed",
    "fast",
    "turbo",
    "speed",
    "lowlatency",
    "lowlat",
})
```

Suggested unsafe suffix set that must not be stripped automatically:

```python
SEMANTIC_VARIANT_TOKENS = frozenset({
    "pro",
    "mini",
    "flash",
    "lite",
    "max",
    "plus",
    "instruct",
    "chat",
    "reasoning",
    "thinking",
    "preview",
    "code",
    "coder",
    "omni",
})
```

Important nuance: some tokens such as `max`, `plus`, `preview`, and `code` may be provider SKU names and should remain semantic by default. Do not strip them unless a future provider-specific rule explicitly permits it.

### Implementation approach

Add a function:

```python
def generate_deployment_suffix_variants(value: str) -> tuple[str, ...]:
    """Return conservative base-name variants with deployment suffixes removed."""
```

Behavior:

1. Tokenize the raw model string with separator awareness.
2. If the final token is in `DEPLOYMENT_SUFFIX_TOKENS`, remove it and return the base variant.
3. Support source IDs by applying to the model segment only:
   - `minimax/MiniMax-M2.7-highspeed` -> `minimax/MiniMax-M2.7`
4. Do not remove more than one suffix in the first pass unless tests prove safety.
5. Do not remove a suffix if the remaining base contains no numeric/version/family token. This avoids matching generic names accidentally.
6. Return raw variant strings and normalized variant keys for diagnostics.

Examples:

```text
MiniMax-M2.7-highspeed -> MiniMax-M2.7
minimax/MiniMax-M2.7-highspeed -> minimax/MiniMax-M2.7
MiniMax-M2.7-fast -> MiniMax-M2.7
MiniMax-M2.7-pro -> no variant
qwen3.7-plus -> no variant
mimo-v2.5-pro -> no variant
kimi-k2.7-code -> no variant
hy3-preview -> no variant
```

### Integrate into tiered matching

Add a new tier after normalized exact and before regex or as part of normalized exact candidate expansion:

```text
Tier 2b: deployment_suffix_normalized_exact
```

Resolver behavior:

1. Generate local deployment-suffix-stripped variants.
2. Normalize each variant.
3. Look up candidates by normalized key.
4. Accept only if exactly one candidate remains after normal vendor/family tie-breaks.
5. Set:
   - `match_method = deployment_suffix_normalized_exact`
   - confidence around `0.70` to `0.80`
   - diagnostics containing `stripped_suffix`, `base_variant`, candidate count, and rejected candidates.
6. Persist discovered alias/evidence the same way other non-exact matches are persisted.

### Tests

Add or extend `tests/unit/test_model_info_tiered_matching.py` and `tests/unit/test_model_info_matching_safety.py`.

Required positive tests:

```text
MiniMax-M2.1-highspeed -> minimax/minimax-m2.1
MiniMax-M2.5-highspeed -> minimax/minimax-m2.5
MiniMax-M2.7-highspeed -> minimax/minimax-m2.7
MiniMax-M2.7-fast -> minimax/minimax-m2.7
```

Required safety tests:

```text
MiniMax-M2.7-pro does not strip to MiniMax-M2.7
mimo-v2.5-pro does not strip to mimo-v2.5
qwen3.7-plus does not strip to qwen3.7
kimi-k2.7-code does not strip to kimi-k2.7
hy3-preview does not strip to hy3
deepseek-v4-flash does not strip to deepseek-v4
```

Required ambiguity test:

```text
MiniMax-M2.7-highspeed does not auto-bind if both minimax/minimax-m2.7 and another candidate share the stripped normalized key or vendor/family tie-break cannot select one.
```

## Phase 2: Add live-shaped highspeed fixture test

Extend the checked-in OpenRouter fixture or add a small fixture:

```text
tests/fixtures/model_info/openrouter_minimax_highspeed_sample.json
```

Include source models:

```text
minimax/minimax-m2.1
minimax/minimax-m2.5
minimax/minimax-m2.7
```

Provider catalog fixture should include:

```text
MiniMax-M2.1-highspeed
MiniMax-M2.5-highspeed
MiniMax-M2.7-highspeed
```

Add a service-level test:

```python
async def test_fresh_db_highspeed_variants_enrich_to_base_minimax_models():
    ...
    result = await service.refresh_due_models()
    assert matched rows include highspeed variants
    assert canonical.external_ids.openrouter for highspeed rows points to base minimax IDs
    assert aliases/evidence record deployment_suffix_normalized_exact
```

Acceptance:

- highspeed rows move from `sparse_new` to `partial`.
- provenance includes `openrouter`.
- match evidence records method and base variant.

## Phase 3: Audit and improve Artificial Analysis benchmark source

### Current state

Artificial Analysis is disabled by default:

```python
artificial_analysis = ModelInfoSourceConfig(enabled=False, priority=50)
```

The adapter parses benchmark observations from:

- `intelligence_index` or `score`
- `speed_index`
- `quality_index`
- generic `benchmarks[]`

But live source health has no `artificial_analysis` row, meaning one of these is true:

- disabled in config;
- missing API key and source not constructed;
- source constructed but never run;
- fetch failed before source-health row was created;
- fetch is not due/visible.

### Tasks

1. Add explicit source-state diagnostics in `/api/model-info/sources` or model-info health snapshot:
   - configured/enabled
   - constructed/available
   - requires_api_key
   - api_key_present boolean, never value
   - last_success/error
   - last_payload_count
   - matched_count in last refresh cycle if available
2. Ensure disabled sources appear in diagnostics as disabled, not absent.
3. Ensure missing API key appears as `configured_enabled_but_missing_key` if the source requires one.
4. Document that benchmark metadata requires enabling `model_info.sources.artificial_analysis` and configuring an API key/base URL if needed.

### Move AA matching onto tiered matching

Refactor AA path in `refresh_due_models()` and `refresh_model_info()`:

1. Build `aa_candidate_index = build_candidate_index("artificial_analysis", aa_indexed.values())`.
2. Resolve with `resolve_source_record_tiered(source="artificial_analysis", ...)`.
3. Use the same local display/provider context and known provider namespaces as OpenRouter.
4. Persist observations and match evidence for non-exact AA matches.
5. Include AA diagnostics:
   - `artificial_analysis_attempted`
   - `artificial_analysis_matched`
   - `artificial_analysis_missed`
   - matched_by_method/missed_by_reason per source.

### Tests

Add tests with AA fixture payloads:

1. `test_artificial_analysis_disabled_appears_in_source_diagnostics`
2. `test_artificial_analysis_enabled_missing_key_reports_missing_key`
3. `test_artificial_analysis_tiered_matching_enriches_benchmarks`
4. `test_artificial_analysis_fetch_success_no_match_records_source_health_and_miss_reason`
5. `test_benchmark_observation_moves_model_toward_fresh_status`

Do not require live AA access in CI.

## Phase 4: Fix provenance/detail drift

### Observed drift

Some rows show `detail.external_ids.openrouter` while `provenance.sources` only lists `provider_catalog`. Examples from live output include some MiniMax casing/provider variants.

This likely means old external IDs were preserved from an earlier merge or alias evidence, while the latest canonical rebuild only credits sources that matched in the current cycle.

### Desired behavior

`detail.external_ids.<source>` and `provenance.sources` should not disagree silently.

Options:

1. If `existing_detail.external_ids.openrouter` is preserved and still considered valid, provenance should retain `openrouter` with a provenance reason such as `preserved_external_id`.
2. If a source did not match this cycle and existing external ID should not be trusted, remove or quarantine it.

Preferred behavior:

- Preserve known external IDs but mark them as preserved/stale in provenance.
- Do not show `sources=[provider_catalog]` when detail still exposes `external_ids.openrouter`.

### Implementation

In `build_canonical_detail()` or service merge logic:

1. Inspect `existing_detail.external_ids`.
2. If preserving external ID for a source not in current observation payloads, add to provenance:

```json
{
  "sources": ["provider_catalog", "openrouter"],
  "source_states": {
    "openrouter": "preserved_external_id"
  }
}
```

or at minimum include `openrouter` in `sources` while keeping a flag that this cycle did not refresh it.

3. Add tests for preserving external IDs without losing provenance consistency.

## Phase 5: Background task startup/first-run behavior

### Current issue

Dashboard shows tasks such as:

- `update_checker`
- `checkpoint`
- `model_info_refresh`

as never run. These are important enough that either they should run once at startup or the dashboard should indicate they are intentionally waiting for their first interval.

### Audit task registration

Inspect the background-task supervisor/scheduler and list for each task:

- task name
- startup delay
- interval
- dependencies
- first run behavior
- dashboard fields shown
- last_run_at / next_run_at / overdue computation

### Desired behavior

For selected safe tasks:

```text
model_info_refresh: run once after catalog/model-info service startup and migrations are complete.
update_checker: run once after app startup if enabled; failures should be non-fatal.
checkpoint: run once after DB ready if checkpointing is enabled and safe.
```

If a task should not run immediately, dashboard should show:

```text
not yet due; first scheduled run at <timestamp>
```

not simply `never ran`.

### Implementation options

#### Option A: Add `run_on_startup` flag to background task registration

Task metadata:

```python
@dataclass
class BackgroundTaskSpec:
    name: str
    interval_s: int
    startup_delay_s: int = 0
    run_on_startup: bool = False
    ...
```

Supervisor behavior:

1. Register task with `next_run_at = now + startup_delay_s`.
2. If `run_on_startup`, schedule a first execution after dependencies are ready.
3. Record `last_run_at`, `last_success_at`, `last_error_at`, and `last_result_summary`.
4. Dashboard displays first-run state explicitly.

#### Option B: Explicit startup calls in app lifecycle

During app startup after migrations/catalog/model-info service construction:

```python
await update_checker_once()
await checkpoint_once()
await model_info_refresh_once()
```

Then register periodic tasks normally.

Preferred: Option A if the supervisor architecture supports it cleanly. It is more general and avoids scattered one-off startup calls.

### Safety notes

- `model_info_refresh` should run only after catalog discovery/backfill has created due canonical rows.
- It should not block startup indefinitely. Use timeout and non-fatal logging.
- `update_checker` should be network-failure tolerant.
- `checkpoint` should avoid excessive disk/SQLite pressure at startup; if the DB is already clean, it should be cheap/no-op.

### Tests

1. `test_background_task_run_on_startup_executes_once`
2. `test_model_info_refresh_run_on_startup_after_catalog_ready`
3. `test_update_checker_startup_failure_is_nonfatal_and_recorded`
4. `test_checkpoint_startup_run_records_last_run`
5. `test_dashboard_shows_next_run_for_not_yet_due_task_instead_of_never_ran`
6. `test_startup_first_run_does_not_mark_task_overdue_immediately`

## Phase 6: Source and task diagnostics in dashboard/API

### Model-info diagnostics

Extend `/api/model-info/sources` to include disabled/configured-but-missing states for known sources:

```json
{
  "source": "artificial_analysis",
  "enabled": false,
  "configured": true,
  "constructed": false,
  "requires_api_key": true,
  "api_key_present": false,
  "reason": "disabled"
}
```

When enabled but no API key:

```json
{
  "source": "artificial_analysis",
  "enabled": true,
  "constructed": false,
  "requires_api_key": true,
  "api_key_present": false,
  "reason": "missing_api_key"
}
```

### Background task diagnostics

Dashboard should distinguish:

- `never_run_not_due`
- `never_run_startup_deferred`
- `never_run_overdue`
- `last_success`
- `last_error`

This avoids making a healthy newly-started process look broken.

## Phase 7: Verification commands

After patching, run against fresh DB:

```bash
BASE="http://127.0.0.1:11300"
DB="usage.sqlite3"

curl -sS "$BASE/api/model-info/sources" | python3 -m json.tool
curl -sS "$BASE/api/stats/runtime" | python3 -m json.tool
sqlite3 "$DB" <<'SQL'
.headers on
.mode column

SELECT source, enabled, last_success_at, last_error_at,
       last_error_class, last_error_message, failure_count, last_payload_count
FROM model_info_source_health
ORDER BY source;

SELECT model_id, status, sparse,
       json_extract(provenance_json, '$.sources') AS sources,
       json_extract(detail_json, '$.external_ids.openrouter') AS openrouter_id,
       COALESCE(json_array_length(json_extract(detail_json, '$.benchmarks')), 0) AS benchmark_count,
       substr(summary, 1, 140) AS summary
FROM model_info_canonical
ORDER BY status, model_id;

SELECT model_id, provider_id, source, alias, confidence, active,
       match_method, discovered_by
FROM model_info_aliases
WHERE lower(model_id) LIKE '%highspeed%'
ORDER BY model_id, source, alias;
SQL
```

Expected after suffix patch:

```text
MiniMax-M2.1-highspeed partial/openrouter_id=minimax/minimax-m2.1
MiniMax-M2.5-highspeed partial/openrouter_id=minimax/minimax-m2.5
MiniMax-M2.7-highspeed partial/openrouter_id=minimax/minimax-m2.7
```

Expected after AA diagnostic patch:

- `artificial_analysis` appears in `/api/model-info/sources` even if disabled.
- If disabled, reason is explicit.
- If enabled and key is missing, reason is explicit.
- If enabled and fetched, payload count and matched/missed counts are visible.

Expected after startup-task patch:

- `update_checker`, `checkpoint`, and `model_info_refresh` no longer display as opaque `never ran` after startup.
- Either they have run once or dashboard shows first scheduled run/deferred state.

## Acceptance criteria

1. `MiniMax-M2.1-highspeed`, `MiniMax-M2.5-highspeed`, and `MiniMax-M2.7-highspeed` resolve to their base OpenRouter MiniMax source IDs when unique.
2. Unsafe semantic variants remain distinct and are covered by tests.
3. Artificial Analysis source state is visible even when disabled/missing key.
4. AA matching uses the tiered resolver or has equivalent normalized matching and diagnostics.
5. Benchmark observations, when available in fixtures, merge into canonical detail and affect status/summary as expected.
6. Detail/provenance no longer disagree silently when external IDs are preserved.
7. Key background tasks either run once on startup or display a clear not-yet-due/deferred state.
8. Tests cover suffix matching, AA diagnostics/matching, provenance consistency, and startup task first-run behavior.

## Suggested commit sequence

1. `Add deployment-suffix model-info matching tier`
2. `Add highspeed model-info fixture tests`
3. `Add Artificial Analysis source diagnostics and tiered matching`
4. `Fix model-info provenance consistency for preserved external IDs`
5. `Run selected background tasks once on startup`
6. `Improve background task first-run dashboard state`
7. `Document benchmark source configuration and diagnostics`

## Suggested final commit message

```text
Polish model-info suffix matching benchmarks and startup tasks
```

## Notes for implementer

Keep the deployment-suffix tier conservative. It should fix known provider presentation aliases such as MiniMax highspeed without collapsing genuinely different model SKUs. For benchmarks, do not assume AA is available by default; the immediate need is to make disabled/missing/fetched/unmatched states explicit so operators can see why every model remains `partial`.
