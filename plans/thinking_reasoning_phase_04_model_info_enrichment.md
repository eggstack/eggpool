# Phase 4: Model-Info Capability Enrichment

## Objective

Extend EggPool's model-info aggregation path so thinking/reasoning capabilities can be populated from provider catalogs or external metadata sources when those sources expose explicit API-control support.

## Problem statement

Manual overrides are necessary, but EggPool should also ingest capability metadata where available. The challenge is that sources vary in precision. Some sources may explicitly document API fields such as Anthropic `thinking` or OpenAI `reasoning_effort`; others may only describe a model as a reasoning model. EggPool should not convert vague marketing language into an API-control guarantee.

## Implementation tasks

1. Inspect the existing model-info ingestion and aggregation code.
2. Add optional capability fields to the normalized model-info record.
3. Map explicit source data into `ModelCapabilities.thinking`.
4. Preserve source provenance and confidence where the existing model-info system supports it.
5. Add conflict handling for incompatible source claims.
6. Ensure manual config overrides from Phase 3 take precedence over model-info enrichment.
7. Add tests using synthetic model-info fixtures.

## Source classification

Treat source observations conservatively:

- Explicit API documentation for `thinking`, `reasoning_effort`, `reasoning_content`, or equivalent provider-native controls can set `status = supported`.
- Explicit documentation that the provider/model does not support the field can set `status = unsupported`.
- Descriptions such as \u201creasoning model,\u201d \u201cthinking model,\u201d or benchmark reasoning capability should not imply API-control support. Represent these as notes or leave API-control status `unknown`.
- If two sources disagree and neither is a manual override, preserve conflict information rather than silently choosing one.

## Suggested normalized detail block

```json
{
  "capabilities": {
    "thinking": {
      "status": "supported",
      "native_protocols": ["anthropic"],
      "budget_tokens_min": 1024,
      "budget_tokens_max": 32768,
      "source": "provider_catalog",
      "confidence": "high"
    }
  }
}
```

## Merge behavior

Discovered capability data should feed the same canonical schema used by config overrides and model listing.

Merge priority:

1. Provider catalog details.
2. External model-info details.
3. Manual global override.
4. Manual provider-scoped override.

If the existing model-info layer already has source scoring, use it. If not, keep this phase simple: provider-owned explicit metadata should outrank third-party explicit metadata, but both should be lower priority than manual overrides.

## Acceptance criteria

- Model-info records can carry optional thinking capability metadata.
- Explicit provider metadata can populate thinking support.
- Vague reasoning-model descriptions do not produce `supported` API-control status.
- Conflicts are represented or logged instead of being silently flattened.
- Manual overrides still win.
- Tests cover explicit support, explicit unsupported, vague marketing-only source, conflict, and manual override precedence.

## Risks

External source schemas may be unstable. Keep adapters source-specific and normalize only into the canonical schema after validation.

Overeager inference is the main correctness risk. Prefer `unknown` over false support.

## Completion check

Use a synthetic provider catalog fixture declaring Anthropic thinking support and another fixture with only \u201creasoning model\u201d prose. Confirm the first yields `supported` and the second remains `unknown` for API-control support.

## Implementation Status

**Completed.** All acceptance criteria met:

- [x] Model-info records can carry optional thinking capability metadata (`SourceModelRecord.thinking_capability`)
- [x] Explicit provider metadata can populate thinking support (`ProviderCatalogSource`)
- [x] Vague reasoning-model descriptions do not produce `supported` API-control status (only explicit `supported_parameters` evidence)
- [x] Conflicts are represented via `status = "conflicting"` with details in `notes`
- [x] Manual overrides still win (applied via existing `apply_capability_overrides` chain after enrichment)
- [x] Tests cover explicit support, explicit unsupported, vague source, conflict, and override precedence

### Files modified

- `src/eggpool/model_info/types.py` — `thinking_capability` field on `SourceModelRecord`
- `src/eggpool/model_info/sources/provider_catalog.py` — extracts thinking from catalog cache
- `src/eggpool/model_info/sources/openrouter.py` — `_extract_thinking_capability()` from `supported_parameters`
- `src/eggpool/model_info/service.py` — `_normalize_observation_payload`, `_merge_thinking_contributions`, `build_canonical_detail`, `_propagate_enriched_capabilities`
- `tests/unit/test_model_info_capability_enrichment.py` — 34 tests covering all acceptance criteria
