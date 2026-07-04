# Model-Info OpenRouter Polish Closeout Plan

## Context

The OpenRouter enrichment corrective pass substantially improved the model-info path:

- `refresh_model_info()` now reports `source_diagnostics` for OpenRouter attempts.
- OpenRouter source health is recorded after successful catalog fetches, even when no local model matches.
- Forced refresh can invalidate the OpenRouter TTL cache and retry when configured aliases exist but do not match the cached catalog.
- API detail now reads compact persisted observation rows from `model_info_observations` instead of fabricating external-source observations from provenance.
- Canonical detail now promotes external display names when provider detail lacks one and exposes source-scoped advisory pricing.
- A new `tests/unit/test_model_info_openrouter_enrichment.py` file covers many of the known failure modes.

The repo is in much better shape, but there are remaining polish issues before this line of work should be considered closed.

## Current repo state summary

Compared to the previous handoff plan commit, `HEAD` is several commits ahead and includes changes across model-info plus unrelated runtime/background/dashboard code. The model-info-specific changes are mostly aligned with the plan, but this is not a narrow patch set and should receive full test validation.

Important files touched by the corrective pass:

- `src/eggpool/model_info/service.py`
- `src/eggpool/model_info/repository.py`
- `src/eggpool/model_info/sources/openrouter.py`
- `src/eggpool/model_info/sources/base.py`
- `src/eggpool/api/model_info.py`
- `tests/unit/test_model_info_openrouter_enrichment.py`

Remaining concerns:

1. Case-insensitive alias lookup can now create false ambiguity.
2. API detail falls back to synthetic observations if compact DB observation retrieval fails.
3. Batch/due refresh behavior is less diagnostic than manual refresh.
4. OpenRouter diagnostics do not yet report exact-case versus case-insensitive alias candidates.
5. Broad unrelated changes need validation to avoid conflating model-info correctness with app/background regressions.

## Primary goals

1. Close the alias ambiguity hole introduced by case-insensitive alias lookup.
2. Make API detail observation fallback explicit and non-misleading.
3. Add regression tests for duplicate/case-variant aliases and exact-case preference.
4. Verify manual and scheduled refresh produce consistent canonical detail.
5. Run a focused plus broad test suite covering model-info, dashboard, background tasks, runtime metrics, and startup lifecycle.

## Non-goals

- Do not add fuzzy OpenRouter matching.
- Do not add new external sources.
- Do not change authoritative cost accounting to use OpenRouter pricing.
- Do not expand dashboard complexity beyond minimal source/status/detail visibility.

## Phase 1: Alias ambiguity polish

### Problem

`ModelInfoRepository.get_aliases_for_model()` now performs a case-insensitive lookup using `lower(model_id) = lower(?)`. This fixes casing drift, but it can return multiple alias rows for different stored-case variants of the same local model.

Example problematic state:

```text
model_id    source      alias
MiniMax-M3  openrouter  minimax/minimax-m3
minimax-m3  openrouter  minimax/minimax-m3
```

The current resolver treats `len(alias_strings) > 1` as ambiguous before deduplicating identical aliases. That can cause a false no-match even when every row points to the same OpenRouter source ID.

Worse state:

```text
model_id    source      alias
MiniMax-M3  openrouter  minimax/minimax-m3
minimax-m3  openrouter  some-other/vendor-id
```

This should be ambiguous when no exact-case row exists, but exact-case rows should win when available.

### Required behavior

Implement deterministic alias candidate selection with these rules:

1. Query rows with stored `model_id`, `alias`, `source`, `provider_id`, `confidence`, `active`, and `last_seen_at`.
2. Prefer rows where stored `model_id == requested_model_id` exactly.
3. If exact-case rows exist, ignore case-insensitive non-exact rows.
4. If no exact-case rows exist, use case-insensitive rows.
5. Deduplicate identical alias strings while preserving deterministic order.
6. If one unique alias remains, resolve it.
7. If multiple unique aliases remain:
   - If exactly one alias exists in the OpenRouter index, use it and report diagnostics that non-indexed aliases were ignored.
   - If multiple aliases exist in the OpenRouter index, treat as ambiguous and do not match.
   - If no aliases exist in the OpenRouter index, return no match with `alias_not_in_catalog`.

### Suggested implementation options

Option A: Add repository method returning alias rows.

```python
async def get_alias_rows_for_model(
    self,
    model_id: str,
    *,
    source: str | None = None,
    active_only: bool = True,
) -> list[dict[str, Any]]:
    ...
```

Then add a helper in `identity.py`:

```python
def choose_alias_candidates(
    requested_model_id: str,
    rows: list[dict[str, Any]],
) -> list[str]:
    exact = [r for r in rows if r["model_id"] == requested_model_id]
    selected = exact or rows
    return dedupe_preserving_order(str(r["alias"]) for r in selected)
```

Option B: Keep `get_aliases_for_model()` returning `list[str]`, but internally implement exact-case preference and de-duplication. This is smaller but hides diagnostic data.

Preferred: Option A, because `source_diagnostics` can then expose exact versus folded candidates.

### Diagnostics improvement

For OpenRouter diagnostics, include:

```json
{
  "alias_candidates": ["minimax/minimax-m3"],
  "alias_rows": [
    {
      "model_id": "minimax-m3",
      "alias": "minimax/minimax-m3",
      "source": "openrouter",
      "provider_id": "opencode-go",
      "confidence": 1.0,
      "match_kind": "exact_case"
    }
  ],
  "alias_selection": "exact_case"
}
```

Avoid returning raw payloads.

### Tests

Add tests to `tests/unit/test_model_info_openrouter_enrichment.py` or a smaller `test_model_info_alias_resolution.py`:

1. `test_duplicate_case_aliases_dedupe_to_single_match`
   - Store `MiniMax-M3 -> minimax/minimax-m3` and `minimax-m3 -> minimax/minimax-m3`.
   - Request `minimax-m3`.
   - Expect one effective alias and successful OpenRouter match.

2. `test_exact_case_alias_wins_over_case_folded_conflict`
   - Store `minimax-m3 -> minimax/minimax-m3` and `MiniMax-M3 -> other/vendor-id`.
   - Request `minimax-m3`.
   - Expect match to `minimax/minimax-m3`, not ambiguity.

3. `test_folded_conflicting_aliases_are_ambiguous_without_exact_case`
   - Store `MiniMax-M3 -> minimax/minimax-m3` and `MINIMAX-M3 -> other/vendor-id`.
   - Request `minimax-m3` when no exact `minimax-m3` row exists.
   - Expect no match and `miss_reason = ambiguous_aliases`.

4. `test_multiple_aliases_only_one_in_catalog_can_match`
   - If implementing the “one indexed alias wins” behavior, add a test where two aliases exist but only one appears in OpenRouter index.

## Phase 2: API observation fallback polish

### Problem

`handle_model_info_detail()` now correctly fetches compact observations from the repository, but if the DB read fails it logs a warning and calls `_detail_response(info, observations=None)`, which triggers the legacy synthetic observation fallback.

Synthetic observation rows can mislead operators because they fabricate:

- `source_model_id = info.model_id`
- `provider_id = first provider`
- `confidence = 1.0`
- `observed_at = info.last_seen_at`

The fallback is marked `_synthetic`, but it still returns potentially false source metadata.

### Required behavior

Change production API behavior to avoid synthetic observation rows when repository observation lookup fails.

Preferred behavior:

```json
{
  "observations": [],
  "observations_error": "read_failed"
}
```

Alternative acceptable behavior:

```json
{
  "observations": [
    {"source": "openrouter", "_synthetic": true, ...}
  ],
  "observations_warning": "synthetic_fallback"
}
```

Preferred: empty observations plus warning. It is better to show missing data than false external source IDs.

### Implementation sketch

1. Change `_detail_response()` signature:

```python
def _detail_response(
    info: Any,
    observations: list[dict[str, Any]] | None = None,
    observations_error: str | None = None,
) -> dict[str, Any]:
```

2. If `observations is None and observations_error is not None`, set:

```python
compact["observations"] = []
compact["observations_error"] = observations_error
```

3. Keep synthetic fallback only for direct unit-test helper usage, or remove it entirely if tests can be updated.

4. In `handle_model_info_detail()`, on exception:

```python
observations = []
observations_error = type(exc).__name__
```

Do not return synthetic rows in the handler path.

### Tests

1. `test_detail_handler_observation_read_failure_returns_empty_with_error`
   - Mock repo `list_compact_observations_for_model` to raise.
   - Assert `observations == []`.
   - Assert `observations_error` is present.
   - Assert no `_synthetic` rows appear.

2. Keep a smaller direct `_detail_response()` fallback test only if needed for test doubles.

## Phase 3: Scheduled refresh parity

### Problem

Manual force refresh now has detailed `source_diagnostics`, cache retry, and source-health improvements. Scheduled `refresh_due_models()` records source-health success and persists matches, but it lacks per-row diagnostics and does not retry OpenRouter cache on no-match.

This is acceptable for normal operation, but a regression could appear where manual refresh works and scheduled refresh silently misses because of alias/cache conditions.

### Required behavior

1. Confirm scheduled `refresh_due_models()` uses the same alias-resolution helper as manual refresh.
2. Add tests that due refresh with configured alias persists OpenRouter observation and updates canonical detail.
3. Do not add cache retry to every scheduled no-match unless necessary; avoid excess outbound calls.
4. Optionally add aggregate counters to `refresh_due_models()` result:

```json
{
  "openrouter_attempted": 1,
  "openrouter_matched": 1,
  "openrouter_missed": 0
}
```

### Tests

1. `test_refresh_due_models_enriches_minimax_m3_from_openrouter`
   - Seed canonical row due now.
   - Configure alias.
   - Mock OpenRouter catalog.
   - Run `refresh_due_models()`.
   - Assert OpenRouter observation exists.
   - Assert canonical detail includes display name, external context, external output, external ID.

2. `test_refresh_due_models_records_openrouter_health_when_no_match`
   - No matching alias or catalog ID.
   - Assert source health row exists with payload count.

## Phase 4: Live operator verification script

Add a small script or documented command block to make the original investigation easier to repeat.

Preferred location:

- `docs/model-info-openrouter-debug.md`, or
- `scripts/debug_model_info_openrouter.sh` if scripts directory exists and project convention supports it.

Suggested script behavior:

```bash
#!/usr/bin/env bash
set -euo pipefail
BASE="${EGGPOOL_BASE_URL:-http://127.0.0.1:8000}"
MODEL="${1:-minimax-m3}"
DB="${EGGPOOL_DB:-usage.sqlite3}"

curl -sS -X POST "$BASE/api/model-info/refresh?model_id=${MODEL}&force=1" | python3 -m json.tool
curl -sS "$BASE/api/model-info/${MODEL}" | python3 -m json.tool
sqlite3 "$DB" <<'SQL'
.headers on
.mode column
SELECT source, enabled, last_success_at, last_error_at, failure_count, last_payload_count
FROM model_info_source_health
ORDER BY source;
SQL
```

Include expected output notes:

- `source_diagnostics.openrouter.miss_reason = matched`
- `sources_matched` includes `openrouter`
- `detail.display_name` populated when provider lacks display name
- `detail.external_ids.openrouter = minimax/minimax-m3`
- OpenRouter observation has real `source_model_id`, not local model ID

## Phase 5: Dashboard sanity polish

### Verify current dashboard shape

The dashboard model list/detail should show:

- Status `partial` for OpenRouter-enriched models.
- Source list containing `provider_catalog` and `openrouter`.
- External context/output in detail view.
- Display name if provider lacks one and OpenRouter supplies it.
- Real observation rows if detail view includes observation metadata.

### Avoid over-expansion

Do not add too much to runtime cards or overview. The user explicitly wants dashboard runtime/config complexity reduced elsewhere. For model-info, keep only high-value fields:

- status
- sources
- external context/output
- display name/source
- last refresh
- observation source IDs in detail only

### Tests

Extend dashboard tests only if dashboard renders model-info details directly. Otherwise, API tests are sufficient and less brittle.

## Phase 6: Broad regression validation

Because the commits after the model-info plan also touched app lifecycle, background tasks, dashboard rendering, quota estimation, and runtime metrics, run more than model-info tests.

Minimum local test command:

```bash
pytest tests/unit/test_model_info_openrouter_enrichment.py \
       tests/unit/test_dashboard.py \
       tests/unit/test_dashboard_phase7.py \
       tests/unit/test_background.py \
       tests/unit/test_background_backup.py \
       tests/unit/test_runtime_metrics.py \
       tests/unit/test_pricing.py
```

If feasible, run the full unit suite:

```bash
pytest tests/unit
```

Then run any lightweight integration suite that does not require live providers:

```bash
pytest tests/integration/test_proxy_advanced.py
```

If the repo has lint/type commands, run the established project commands from README/AGENTS. Do not invent new required tooling.

## Phase 7: Live acceptance checks

After implementation and tests, run these against a real Eggpool instance with `usage.sqlite3`.

### 7.1 Manual refresh

```bash
BASE="${EGGPOOL_BASE_URL:-http://127.0.0.1:8000}"

curl -sS -X POST "$BASE/api/model-info/refresh?model_id=minimax-m3&force=1" \
  | python3 -m json.tool
```

Expected:

```json
{
  "sources_attempted": ["provider_catalog", "openrouter"],
  "sources_matched": ["provider_catalog", "openrouter"],
  "source_diagnostics": {
    "openrouter": {
      "miss_reason": "matched",
      "matched_source_model_id": "minimax/minimax-m3"
    }
  }
}
```

### 7.2 Detail API

```bash
curl -sS "$BASE/api/model-info/minimax-m3" | python3 -m json.tool
```

Expected:

- `status = partial`
- `sparse = false`
- `detail.display_name = MiniMax: MiniMax M3` if provider detail lacks a display name
- `detail.display_name_source = openrouter` when promoted from OpenRouter
- `detail.limits.external_context = 1048576`
- `detail.limits.external_output = 512000`
- `detail.external_ids.openrouter = minimax/minimax-m3`
- `detail.pricing.openrouter` exists with advisory pricing
- `observations[]` contains an OpenRouter row with `source_model_id = minimax/minimax-m3`
- no synthetic observation rows in normal handler path

### 7.3 Source health

```bash
sqlite3 usage.sqlite3 <<'SQL'
.headers on
.mode column
SELECT source, enabled, last_success_at, last_error_at, failure_count, last_payload_count
FROM model_info_source_health
ORDER BY source;
SQL
```

Expected:

- `openrouter` row exists after successful fetch.
- `last_payload_count` is greater than zero.
- `failure_count = 0` if fetch succeeded.

### 7.4 Alias ambiguity regression

Use SQLite or API alias endpoints to create/check case-variant aliases. Verify:

- duplicate identical aliases do not cause ambiguity
- exact-case alias wins over folded conflicting aliases
- folded conflicting aliases with no exact-case row produce explicit ambiguity, not silent no-match

## Acceptance criteria

This polish pass is complete when:

1. Duplicate case-variant aliases pointing to the same OpenRouter ID resolve successfully.
2. Exact-case alias rows take precedence over folded-case rows.
3. Folded-case conflicting aliases produce a clear `ambiguous_aliases` diagnostic.
4. API detail does not return fabricated external observation rows when DB observation lookup fails.
5. Scheduled due refresh and manual force refresh both persist OpenRouter observations and canonical detail for the `minimax-m3` fixture.
6. The new and existing model-info tests pass.
7. The dashboard/background/runtime tests touched by the broader commits pass.
8. Live `minimax-m3` verification shows populated display name, external limits, OpenRouter ID, advisory pricing, and truthful observation rows.

## Suggested final commit message

```text
Polish model-info OpenRouter alias and observation handling
```

## Notes for implementer

Keep the patch narrow. The previous commit set already included broad unrelated background/runtime work. This closeout should focus on alias-resolution determinism, API observation fallback clarity, and validation. Do not add new dashboard panels or new external source behavior unless directly required by the acceptance criteria.
