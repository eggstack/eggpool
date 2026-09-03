# Model-Info OpenRouter Debugging

This document walks an operator through the live verification flow for
the model-info OpenRouter enrichment.  It complements the broader
[architecture](../architecture/README.md).

## Automatic lifecycle

When `[model_info].enabled = true` and `startup_refresh = true`, startup runs
one bounded external enrichment pass. The batch is capped by
`max_models_per_cycle`; external catalog sources are fetched once per pass.
After startup, the existing `catalog_refresh` task creates recurring
opportunities. `ModelInfoService` selects only rows whose `next_refresh_at` is
due, while source TTL and cooldown state provide the remaining bounds.

The manual endpoint and this script are forced diagnostic/recovery tools. They
are not required to populate ordinary dashboard metadata on a healthy
installation. If `[models].refresh_interval_s = 0`, there is no recurring
automatic opportunity after the bounded startup pass; use a manual refresh or
restart when enrichment is needed.

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
EGGPOOL_API_KEY=<server.api_key> \
    ./scripts/debug_model_info_openrouter.sh                   # authenticated deploy
```

Required tools: `curl`, `python3` (for `json.tool` and URL encoding),
`sqlite3`.

The script is **state-changing**: the first step is a `force=1` refresh
that fetches from external sources and updates the model-info canonical
rows, source health, aliases, observations, and match-evidence tables.
Run it on production only when you intend that side-effect.  The
`/api/model-info/refresh` endpoint is always auth-gated regardless of
`dashboard.public`, so set `EGGPOOL_API_KEY` (or `x-api-key`) on
deployments where `[server].api_key` is configured; otherwise the
script exits with a clear 401 message instead of silently failing.
The local SQLite inspection queries (source-health, match-evidence,
aliases) remain read-only.

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
            "match_method": "normalized_exact",
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
    "match_evidence": [
        {"source": "openrouter", "alias": "minimax/minimax-m3", "match_method": "normalized_exact", "confidence": 0.85}
    ],
    "observations": [
        {"source": "openrouter", "source_model_id": "minimax/minimax-m3", "confidence": 0.5}
    ]
}

==> Match evidence for minimax-m3
    GET http://127.0.0.1:8000/api/model-info/minimax-m3/matches
[
    {"source": "openrouter", "alias": "minimax/minimax-m3", "match_method": "normalized_exact", "confidence": 0.85}
]

==> model_info_source_health snapshot
source         enabled  last_success_at            failure_count  last_payload_count
-------------- -------- -------------------------- -------------- -------------------
openrouter     1        2026-07-04T14:23:01Z       0              347
provider_catalog 1      2026-07-04T14:23:01Z       0              12

==> model_info_match_evidence snapshot
model_id       source    alias               match_method      confidence  provider_id
-------------  --------  ------------------  ----------------  ----------  -----------
minimax-m3     openrouter  minimax/minimax-m3  normalized_exact  0.85

==> model_info_aliases with match_method
model_id       source      alias               match_method      discovered_by
-------------  ----------  ------------------  ----------------  -------------
minimax-m3     openrouter  minimax/minimax-m3  normalized_exact  openrouter
```

## Expected outcomes

The following invariants are pinned by the
`tests/unit/test_model_info_openrouter_enrichment.py` and
`tests/unit/test_model_info_alias_resolution.py` suites:

| Surface | Expected |
|---------|----------|
| `source_diagnostics.openrouter.miss_reason` | `"matched"` |
| `source_diagnostics.openrouter.matched_source_model_id` | `"minimax/minimax-m3"` |
| `source_diagnostics.openrouter.match_method` | `"normalized_exact"` or `"regex_rule"` |
| `source_diagnostics.openrouter.alias_rows` | one row per alias candidate with `match_kind` |
| `detail.status` | `"partial"` |
| `detail.sparse` | `false` |
| `detail.display_name` | populated when provider lacks one (e.g. `"MiniMax: MiniMax M3"`) |
| `detail.display_name_source` | `"openrouter"` when promoted |
| `detail.limits.external_context` | `1048576` for `minimax-m3` |
| `detail.limits.external_output` | `512000` for `minimax-m3` |
| `detail.external_ids.openrouter` | `"minimax/minimax-m3"` |
| `detail.pricing.openrouter` | present with advisory per-1k pricing |
| `detail.match_evidence[]` | non-empty list with `match_method`, `confidence`, `source` |
| `/api/model-info/{id}/matches` | returns match evidence (capped at 50 entries) |
| `/api/model-info/{id}/aliases` | returns both `aliases[]` and `aliases_by_source[]` (NOT shadowed by greedy detail route — registration order is pinned by `tests/unit/test_model_info_route_registration.py`) |
| `observations[].source_model_id` | real OpenRouter id (`minimax/minimax-m3`), not local id |
| `observations[]._synthetic` | not present in production handler path |
| `model_info_source_health.openrouter.last_payload_count` | `> 0` after a successful fetch |
| `model_info_source_health.openrouter.failure_count` | `0` after a successful fetch |
| `model_info_match_evidence` table | contains row with `match_method`, `confidence`, `diagnostics_json` |

## Alias ambiguity regressions

Case-insensitive alias lookup can create false-ambiguity holes. When you see
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

When the repository read fails, the detail endpoint responds with:

```json
{
    "observations": [],
    "observations_error": "OperationalError"
}
```

No synthetic observation rows (`_synthetic: true`) are returned on the
read-failure path.  The legacy synthesis path is retained only for
direct test-double callers of `_detail_response()`.
