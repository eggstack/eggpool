# Model-Info OpenRouter Debugging

This document walks an operator through the live verification flow for
the model-info OpenRouter enrichment.  It complements the broader
[architecture](../architecture/README.md) and the
[OpenRouter polish closeout plan](../plans/model_info_openrouter_polish_closeout_plan.md).

## One-shot verification script

The repository ships a single-command helper at
`scripts/debug_model_info_openrouter.sh` that issues the refresh,
reads the detail, and dumps the source-health snapshot:

```bash
./scripts/debug_model_info_openrouter.sh                       # defaults to minimax-m3
./scripts/debug_model_info_openrouter.sh gpt-4o                # override model
EGGPOOL_DB=/var/lib/eggpool/usage.sqlite3 \
    EGGPOOL_BASE_URL=https://gateway.example \
    ./scripts/debug_model_info_openrouter.sh                   # remote instance
```

Required tools: `curl`, `python3` (for `json.tool`), `sqlite3`.  The
script never edits state — the only mutation it triggers is the
read-only `force=1` refresh.

## What the script prints

```bash
==> Refreshing model-info for minimax-m3 (force=1)
    POST http://127.0.0.1:8000/api/model-info/refresh?model_id=minimax-m3&force=1
{
    "status": "ok",
    "scope": "model",
    "sources_attempted": ["provider_catalog", "openrouter"],
    "sources_matched": ["provider_catalog", "openrouter"],
    "source_diagnostics": {
        "openrouter": {
            "miss_reason": "matched",
            "matched_source_model_id": "minimax/minimax-m3",
            "alias_selection": "exact_case",
            ...
        }
    }
}

==> Reading detail for minimax-m3
    GET http://127.0.0.1:8000/api/model-info/minimax-m3
{
    "status": "partial",
    "detail": {
        "display_name": "MiniMax: MiniMax M3",
        "display_name_source": "openrouter",
        "limits": {"external_context": 1048576, "external_output": 512000, ...},
        "external_ids": {"openrouter": "minimax/minimax-m3"},
        "pricing": {"openrouter": {"input_price_per_1k": 0.3e-6, ...}}
    },
    "observations": [
        {"source": "openrouter", "source_model_id": "minimax/minimax-m3", "confidence": 0.5}
    ]
}

==> model_info_source_health snapshot
source         enabled  last_success_at            failure_count  last_payload_count
-------------- -------- -------------------------- -------------- -------------------
openrouter     1        2026-07-04T14:23:01Z       0              347
provider_catalog 1      2026-07-04T14:23:01Z       0              12
```

## Expected outcomes (Phase 1 polish)

The following invariants are pinned by the
`tests/unit/test_model_info_openrouter_enrichment.py` and
`tests/unit/test_model_info_alias_resolution.py` suites and by the
acceptance criteria in the polish closeout plan:

| Surface | Expected |
|---------|----------|
| `source_diagnostics.openrouter.miss_reason` | `"matched"` |
| `source_diagnostics.openrouter.matched_source_model_id` | `"minimax/minimax-m3"` |
| `source_diagnostics.openrouter.alias_selection` | `"exact_case"` or `"case_folded"` |
| `source_diagnostics.openrouter.alias_rows` | one row per alias candidate with `match_kind` |
| `detail.status` | `"partial"` |
| `detail.sparse` | `false` |
| `detail.display_name` | populated when provider lacks one (e.g. `"MiniMax: MiniMax M3"`) |
| `detail.display_name_source` | `"openrouter"` when promoted |
| `detail.limits.external_context` | `1048576` for `minimax-m3` |
| `detail.limits.external_output` | `512000` for `minimax-m3` |
| `detail.external_ids.openrouter` | `"minimax/minimax-m3"` |
| `detail.pricing.openrouter` | present with advisory per-1k pricing |
| `observations[].source_model_id` | real OpenRouter id (`minimax/minimax-m3`), not local id |
| `observations[]._synthetic` | not present in production handler path |
| `model_info_source_health.openrouter.last_payload_count` | `> 0` after a successful fetch |
| `model_info_source_health.openrouter.failure_count` | `0` after a successful fetch |

## Alias ambiguity regressions

The Phase 1 polish closes three false-ambiguity holes created by
case-insensitive alias lookup.  When you see
`miss_reason = "ambiguous_aliases"` or an empty `alias_candidates`
list, inspect `model_info_aliases` for that model:

```bash
sqlite3 usage.sqlite3 <<'SQL'
.headers on
.mode column
SELECT model_id, source, alias, provider_id, confidence, active
FROM model_info_aliases
WHERE lower(model_id) = lower('minimax-m3')
ORDER BY model_id;
SQL
```

Verifying the polish invariants:

1. **Duplicate case-variant aliases pointing to the same OpenRouter id**
   must collapse to a single match.  Look for two rows whose
   `alias` is identical but whose `model_id` differs only in case.
2. **Exact-case alias rows win over case-folded conflicting rows.**
   When two folded rows disagree on the source id, the exact-case
   row's alias must appear in `source_diagnostics.openrouter.matched_source_model_id`.
3. **Folded-case conflicting aliases with no exact-case row produce a
   clear `miss_reason = "ambiguous_aliases"`.**  When you see this
   diagnostic, no match is returned and the operator can clean up the
   alias table manually.

## When the read path fails

The Phase 2 polish keeps the API truthful when the repository read
fails.  The detail endpoint will respond with:

```json
{
    "observations": [],
    "observations_error": "OperationalError"
}
```

No synthetic observation rows (`_synthetic: true`) are returned on the
read-failure path.  The legacy synthesis path is retained only for
direct test-double callers of `_detail_response()`.