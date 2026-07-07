# Model-Info Identity Matching Closure Plan

## Context

The identity-normalization implementation pass landed the right architecture for the fresh-DB “all available models are sparse” failure:

- `src/eggpool/model_info/normalization.py` adds deterministic model-key normalization using NFKC, `.casefold()`, separator stripping, source-ID splitting, provider namespace stripping, and duplicate vendor-prefix collapse.
- `src/eggpool/model_info/matching.py` adds a tiered resolver with configured exact aliases, exact source IDs, normalized exact matching, curated regex rules, and guarded similarity.
- OpenRouter service integration now builds a candidate index and routes through `_resolve_openrouter_with_tiered_matching()`.
- Migration `0049_model_info_match_evidence.sql` adds alias/evidence columns and a match-evidence audit table.
- Repository methods now persist aliases with `match_method`/`diagnostics_json` and record match evidence.
- Tests now cover normalization, OpenRouter outbound request contracts, fixture-shaped tiered matching, ambiguity, and evidence persistence.

This is the first implementation that directly addresses naming drift between aggregator provider model IDs and third-party metadata source IDs. However, a few closure issues remain before this line should be considered production-safe.

## Remaining issues

### 1. Supervisor-owned periodic refresh still logs only writes

`ModelInfoService.run_periodic_refresh()` now logs useful no-match cycles, including OpenRouter attempted/matched/missed counts and method/reason summaries. But the production app path appears to register a supervisor-owned `_model_info_refresh_once()` in `src/eggpool/app.py`, and that wrapper still only logs when `refreshed > 0`.

This means the operator-critical case can still remain silent in production:

```text
openrouter_attempted=33
openrouter_matched=0
openrouter_missed=33
refreshed=0
```

The improved logging exists, but likely does not run under the supervised app task.

### 2. Provider namespace stripping is tested but not clearly wired in service integration

The tiered resolver supports `known_provider_namespaces` and tests prove that `opencode-go/minimax-m3` can strip to `minimax-m3` when `known_provider_namespaces={"opencode-go"}` is passed. But `_resolve_openrouter_with_tiered_matching()` currently appears to call `resolve_source_record_tiered()` with `provider_id=None` and without `known_provider_namespaces`.

For unsuffixed canonical rows like `minimax-m3`, normalized exact likely still works. For provider-suffixed traffic/canonical rows or provider-alias-only cases, the production service may not benefit from namespace stripping.

### 3. Fresh-DB tests are resolver/evidence tests, not a full service-chain test

The current integration-style tests directly call `resolve_source_record_tiered()` and manually persist evidence/observations. That is useful, but it does not fully prove the operational chain:

```text
fresh DB
real ModelCatalogCache with provider row
ModelInfoService.load_cache()
ModelInfoService.reconcile_catalog_snapshot()
ModelInfoService.refresh_model_info(force=True) or refresh_due_models()
canonical row transitions sparse -> partial
API/dashboard summary shows non-sparse
```

A true service-level regression test is needed.

### 4. Match evidence is persisted, but API/debug visibility is still limited

The repository can list match evidence, but there is no clear operator endpoint or dashboard detail section exposing it. Without this, live debugging still requires direct SQLite queries.

### 5. Matching config exists internally but not from runtime config

`ModelInfoMatchingConfig` has sensible defaults, but the service constructs it internally with no link to `ModelInfoConfig`. That is acceptable for the first pass, but thresholds/toggles cannot be tuned without code changes.

## Goals

1. Make supervised production refresh logging report no-match cycles and match-method summaries.
2. Wire provider namespace information into service-layer tiered matching.
3. Add a true service-level fresh-DB regression test that proves canonical rows move out of `sparse` without configured aliases.
4. Expose match evidence through a compact read-only API/debug surface or a documented script.
5. Add closure tests for logging, provider-suffixed IDs, evidence persistence through the service, and migration compatibility.
6. Keep matching conservative: no broad similarity enablement by default.

## Non-goals

- Do not change dashboard layout beyond optional compact evidence display.
- Do not enable similarity matching by default unless tests prove it is safe.
- Do not make OpenRouter authoritative for routability or cost accounting.
- Do not remove configured exact aliases; they remain highest priority.
- Do not add external dependencies for Levenshtein unless explicitly approved later.

## Phase 1: Move periodic refresh logging into the supervised production path

### Problem

The app registers a supervisor-owned model-info periodic task. That wrapper currently calls `refresh_due_models()` and logs only when `refreshed > 0`. The improved logging in `ModelInfoService.run_periodic_refresh()` may be unused.

### Implementation

Add a helper on the service or in `app.py`:

```python
def log_model_info_refresh_result(result: dict[str, object]) -> None:
    ...
```

Preferred location: `src/eggpool/model_info/service.py`, as a public/static helper or private module-level function:

```python
def log_refresh_result(result: dict[str, object], *, logger: logging.Logger) -> None:
    refreshed = _safe_int_count(result.get("refreshed"))
    total = _safe_int_count(result.get("total"))
    skipped = _safe_int_count(result.get("skipped"))
    or_attempted = _safe_int_count(result.get("openrouter_attempted"))
    or_matched = _safe_int_count(result.get("openrouter_matched"))
    or_missed = _safe_int_count(result.get("openrouter_missed"))
    matched_by_method = result.get("matched_by_method", {})
    missed_by_reason = result.get("missed_by_reason", {})

    if or_attempted > 0 and or_matched == 0:
        logger.warning(...)
    elif or_attempted > 0 or refreshed > 0:
        logger.info(...)
```

Then use it from both:

- `ModelInfoService.run_periodic_refresh()`
- app supervisor `_model_info_refresh_once()`

### Acceptance criteria

A periodic refresh with `openrouter_attempted > 0`, `openrouter_matched == 0`, and `refreshed == 0` logs a warning in the actual app-supervisor path.

### Tests

Add or update app/service tests:

1. `test_model_info_supervisor_refresh_logs_all_miss_cycle`
   - Fake `model_info.refresh_due_models()` returns:

```python
{
    "refreshed": 0,
    "total": 33,
    "skipped": 33,
    "openrouter_attempted": 33,
    "openrouter_matched": 0,
    "openrouter_missed": 33,
    "matched_by_method": {},
    "missed_by_reason": {"no_match": 33},
}
```

   - Assert log contains `openrouter_attempted=33` and `openrouter_matched=0`.

2. `test_model_info_supervisor_refresh_logs_matched_cycle_at_info`
   - Return `openrouter_matched > 0`.
   - Assert `INFO` log includes `matched_by_method`.

## Phase 2: Wire known provider namespaces into service matching

### Problem

The tiered resolver can strip known provider namespaces, but service integration currently does not clearly pass `known_provider_namespaces` or the actual `provider_id`.

### Implementation

Add helper in `ModelInfoService`:

```python
def _known_provider_namespaces(self) -> set[str]:
    namespaces: set[str] = set()
    for _model_id, provider_id in self._catalog._provider_models.keys():
        if provider_id:
            namespaces.add(str(provider_id))
    return namespaces
```

Add helper to determine provider IDs for a model:

```python
def _provider_ids_for_model(self, model_id: str) -> list[str]:
    ids = []
    for mid, pid in self._catalog._provider_models.keys():
        if mid.casefold() == model_id.casefold():
            ids.append(pid)
    return sorted(set(ids))
```

When calling `resolve_source_record_tiered()`:

```python
provider_ids = self._provider_ids_for_model(model_id)
provider_id = provider_ids[0] if len(provider_ids) == 1 else None
known_provider_namespaces = self._known_provider_namespaces()
```

Pass both:

```python
decision = await resolve_source_record_tiered(
    source="openrouter",
    model_id=model_id,
    provider_id=provider_id,
    display_name=display_name,
    repo=self._repo,
    candidate_index=candidate_index,
    config=self._matching_config,
    known_provider_namespaces=known_provider_namespaces,
)
```

Also set `alias_to_persist_provider_id` where possible so discovered aliases are scoped usefully.

### Safety

If a model exists under multiple providers, do not overfit a provider ID. Either pass `None` or include provider IDs only in diagnostics. Matching should still use model-name normalization first.

### Tests

1. `test_service_passes_known_provider_namespaces_for_suffixed_model`
   - Use a canonical/model ID shaped like `opencode-go/minimax-m3` if the service supports it.
   - Assert `minimax/minimax-m3` resolves via stripped candidate.

2. `test_service_discovered_alias_gets_provider_id_when_single_provider`
   - Catalog has one provider for `minimax-m3`.
   - After refresh, `model_info_aliases.provider_id = opencode-go` for the discovered OpenRouter alias.

3. `test_service_multi_provider_does_not_choose_arbitrary_provider_id`
   - Catalog has same model ID under two providers.
   - Discovered alias either has `provider_id = NULL` or deterministic safe behavior.

## Phase 3: Add true service-level fresh-DB regression test

### Required test

Add a test file or extend `tests/unit/test_model_info_tiered_integration.py`:

```python
async def test_fresh_db_service_refresh_enriches_minimax_without_configured_alias():
    db = Database(path=":memory:")
    await MigrationRunner(db).run()

    catalog = ModelCatalogCache()
    # seed provider catalog exactly as runtime would
    catalog._models["minimax-m3"] = {...}
    catalog._provider_models[("minimax-m3", "opencode-go")] = {...}

    client = RecordingClient(payload=openrouter_fixture)
    config = ModelInfoConfig(
        sources=ModelInfoSourcesConfig(
            provider_catalog=ModelInfoSourceConfig(enabled=True),
            openrouter=ModelInfoSourceConfig(enabled=True),
            artificial_analysis=ModelInfoSourceConfig(enabled=False),
            huggingface=ModelInfoSourceConfig(enabled=False),
        ),
        aliases=[],
    )

    service = ModelInfoService(config=config, db=db, catalog=catalog, outbound_client=client)
    await service.load_cache()
    await service.reconcile_catalog_snapshot(...)
    result = await service.refresh_model_info("minimax-m3", force=True)

    assert "openrouter" in result["sources_matched"]
    diag = result["source_diagnostics"]["openrouter"]
    assert diag["match_method"] in {"normalized_exact", "regex_rule"}

    canonical = await service.get_summary("minimax-m3")
    assert canonical is not None
    assert canonical.status in {"partial", "fresh"}
    assert canonical.sparse is False
    assert canonical.detail["external_ids"]["openrouter"] == "minimax/minimax-m3"

    evidence = await service.repo.list_match_evidence("minimax-m3", source="openrouter")
    assert evidence
```

If `reconcile_catalog_snapshot()` requires a `CatalogRefreshResult`, either:

- construct a minimal valid result; or
- call `refresh_provider_catalog_observations()` and `ensure_canonical()` directly if that more closely matches test fixture patterns.

### Add due-refresh variant

Also add:

```python
async def test_fresh_db_due_refresh_enriches_minimax_without_configured_alias():
    ...
    result = await service.refresh_due_models()
    assert result["openrouter_matched"] >= 1
    assert result["matched_by_method"]["normalized_exact"] >= 1
```

This protects the actual background path.

## Phase 4: Expose match evidence for debugging

### Option A: Add API field to model-info detail

Extend `GET /api/model-info/{model_id}` response with compact `match_evidence`:

```json
"match_evidence": [
  {
    "source": "openrouter",
    "alias": "minimax/minimax-m3",
    "match_method": "normalized_exact",
    "confidence": 0.85,
    "provider_id": "opencode-go",
    "last_seen_at": "..."
  }
]
```

Do not include full `diagnostics_json` by default unless `?debug=1` is passed.

### Option B: Add diagnostic endpoint

Add:

```text
GET /api/model-info/{model_id}/matches
```

This can return detailed match evidence and diagnostics without cluttering the normal detail endpoint.

Preferred first pass: add compact evidence to the detail endpoint and keep raw diagnostics internal. The detail endpoint already aggregates observations and provenance.

### Tests

1. `test_detail_response_includes_compact_match_evidence`
2. `test_detail_response_omits_raw_match_diagnostics_by_default`
3. `test_match_evidence_empty_when_no_evidence_rows`

## Phase 5: Migration and backward-compatibility checks

### Problem

Migration `0049` uses `ALTER TABLE model_info_aliases ADD COLUMN ...`. If an operator has already applied a partial dev migration or the schema is rebuilt in tests, duplicate-column behavior should be understood.

### Tasks

1. Verify migration runner applies `0049` once and records checksum correctly.
2. Verify a fresh DB builds all model-info tables including new columns.
3. Verify an upgraded DB with pre-existing alias rows retains them and default `diagnostics_json='{}'` applies.
4. Verify `upsert_alias()` still works against the new schema and does not clear match metadata unexpectedly for existing discovered rows.

### Tests

1. `test_migration_0049_adds_alias_metadata_columns`
2. `test_existing_alias_rows_survive_0049`
3. `test_upsert_alias_with_method_round_trip`
4. `test_legacy_upsert_alias_does_not_fail_after_0049`

## Phase 6: Matching safety hardening

### Review normalized exact ambiguity

The normalized exact tier can match if only one candidate shares a normalized key. That is correct, but add more tests for close variants:

- `gpt-5.5` must not bind to `gpt-5.5-mini`.
- `deepseek-v4` must not bind to `deepseek-v4-pro`.
- `claude-sonnet-4` must not bind to `claude-sonnet-4.5` unless exact candidate absent and similarity is explicitly enabled.
- `gemini-2.5-flash` must not bind to `gemini-2.5-pro`.

### Add version-token checks to normalized exact ambiguity path if needed

If normalized exact has multiple candidates and vendor tie-break leaves more than one, keep returning ambiguous. Do not pick by shortest string or first entry.

### Regex rule review

The current regex rules are useful but broad. Add tests ensuring they do not bind to the wrong variant when several vendor candidates exist.

## Phase 7: Live/fixture verification scripts

### Fixture tests

Add or expand checked-in fixture tests using:

```text
tests/fixtures/model_info/openrouter_models_sample.json
tests/fixtures/model_info/provider_catalog_sample_opencode_go.json
```

Test a small matrix:

```text
minimax-m3 -> minimax/minimax-m3
MiniMax-M3 -> minimax/minimax-m3
MiniMax: MiniMax M3 -> minimax/minimax-m3
opencode-go/minimax-m3 -> minimax/minimax-m3 when namespace stripping configured
```

### Optional live tests

If not already present, add:

```text
tests/live/test_model_info_openrouter_live.py
```

Gated by:

```python
pytestmark = pytest.mark.skipif(
    os.getenv("EGGPOOL_LIVE_MODEL_INFO_TESTS") != "1",
    reason="live model-info tests disabled",
)
```

Live assertions should be minimal and stable:

- fetch OpenRouter catalog successfully;
- payload count > 0;
- known source IDs exist only if stable enough;
- if a known ID disappears, failure message should say fixture/live allowlist needs update.

### Operator script

Extend `scripts/debug_model_info_openrouter.sh` to print:

- source health;
- canonical status/sparse/external IDs;
- aliases including `match_method`, `discovered_by`;
- match evidence rows;
- latest observations.

## Phase 8: Runtime config integration, optional

`ModelInfoMatchingConfig` currently exists internally. Add runtime config only if simple and low-risk.

Possible TOML shape:

```toml
[model_info.matching]
enabled = true
normalized_exact = true
regex_rules = true
similarity = false
similarity_threshold = 0.92
similarity_min_gap = 0.05
persist_discovered_aliases = true
```

If config model churn is too large for this closure pass, defer this and document current defaults.

## Suggested test command

Run the focused suite:

```bash
pytest tests/unit/test_model_info_normalization.py \
       tests/unit/test_model_info_tiered_matching.py \
       tests/unit/test_model_info_tiered_integration.py \
       tests/unit/test_model_info_openrouter_contract.py \
       tests/unit/test_model_info_openrouter_enrichment.py \
       tests/unit/test_model_info_alias_resolution.py
```

Then add the new service-level tests and run them explicitly:

```bash
pytest tests/unit/test_model_info_service_identity_matching.py
```

Run dashboard smoke tests to ensure status projection remains intact:

```bash
pytest tests/unit/test_dashboard_model_info_join.py tests/unit/test_dashboard.py
```

## Live verification checklist

Use a fresh DB and a config with OpenRouter model-info enabled.

```bash
BASE="${EGGPOOL_BASE_URL:-http://127.0.0.1:8000}"

curl -sS -X POST "$BASE/api/model-info/refresh?model_id=minimax-m3&force=1" \
  | python3 -m json.tool

curl -sS "$BASE/api/model-info/minimax-m3" \
  | python3 -m json.tool

sqlite3 usage.sqlite3 <<'SQL'
.headers on
.mode column

SELECT model_id, status, sparse,
       json_extract(provenance_json, '$.sources') AS sources,
       json_extract(detail_json, '$.external_ids.openrouter') AS openrouter_id,
       json_extract(detail_json, '$.display_name') AS display_name
FROM model_info_canonical
WHERE lower(model_id) = lower('minimax-m3');

SELECT model_id, provider_id, source, alias, confidence, active,
       match_method, discovered_by, diagnostics_json
FROM model_info_aliases
WHERE lower(model_id) = lower('minimax-m3')
ORDER BY source, alias;

SELECT model_id, provider_id, source, alias, match_method, confidence,
       diagnostics_json, created_at, last_seen_at
FROM model_info_match_evidence
WHERE lower(model_id) = lower('minimax-m3')
ORDER BY last_seen_at DESC;

SELECT source, model_id, source_model_id, provider_id, confidence, observed_at
FROM model_info_observations
WHERE lower(model_id) = lower('minimax-m3')
ORDER BY observed_at DESC;
SQL
```

Expected:

- refresh response has `sources_matched` containing `openrouter`;
- diagnostics have `match_method` of `normalized_exact` or `regex_rule`;
- canonical row has `status=partial` or better and `sparse=0`;
- `detail.external_ids.openrouter = minimax/minimax-m3`;
- alias/evidence rows show method/confidence;
- dashboard `/models` shows a non-sparse pill for `minimax-m3`.

Dashboard smoke:

```bash
curl -sS "$BASE/models" \
  | grep -Eo 'pill-(fresh|partial|sparse|stale|conflict|unmatched|unknown)' \
  | sort | uniq -c
```

Expected: not all available models are `pill-sparse` if external metadata is enabled and source fetch succeeds.

## Acceptance criteria

This closure pass is complete when:

1. Production supervisor-owned model-info refresh logs all-miss OpenRouter cycles.
2. Service-layer tiered matching passes known provider namespaces and provider context where available.
3. A true fresh-DB `ModelInfoService` test proves `minimax-m3` enriches to `minimax/minimax-m3` without configured aliases.
4. A due-refresh service test proves the background path can enrich and returns `matched_by_method` counts.
5. Match evidence is visible through a compact API/debug surface or documented script.
6. Migration `0049` is covered by fresh/upgrade tests.
7. Matching safety tests cover close model variants and ambiguity.
8. Live verification on a fresh DB shows at least representative models moving out of `sparse` when external metadata is available.

## Suggested commit sequence

1. `Share model-info refresh logging with supervisor task`
2. `Pass provider namespace context into tiered matcher`
3. `Add service-level fresh DB model-info matching tests`
4. `Expose compact model-info match evidence`
5. `Add migration and variant-safety tests`
6. `Update model-info debug script and docs`

## Suggested final commit message

```text
Close out model-info identity matching diagnostics and service tests
```

## Notes for implementer

Keep this pass narrow. The tiered resolver implementation itself is already present. The highest-value remaining work is proving the full service path works on a fresh DB and ensuring production logs reveal future no-match failures immediately.
