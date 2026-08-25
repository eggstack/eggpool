# Deep Dive: Model Info Sidecar

Back to [Overview](overview.md)

## Purpose

Multi-source model metadata enrichment with tiered identity matching. Provides persistent model metadata (limits, capabilities, pricing, benchmarks) beyond what the catalog cache provides.

## Architecture

```
┌─────────────────────────────────────┐
│         ModelInfoService             │
│  Orchestrates multi-source enrichment│
└──────────────┬──────────────────────┘
               │
    ┌──────────▼──────────┐
    │ Source Adapters      │
    │ • Provider Catalog   │ (in-memory, no network)
    │ • OpenRouter         │ (HTTP fetch)
    │ • Artificial Analysis│ (HTTP fetch, API key)
    │ • Hugging Face       │ (HTTP fetch)
    └──────────┬──────────┘
               │
    ┌──────────▼──────────┐
    │ Identity Matching   │
    │ 7-tier resolver     │
    └──────────┬──────────┘
               │
    ┌──────────▼──────────┐
    │ SQLite Sidecar      │
    │ model_info_canonical│
    │ model_info_observations│
    │ model_info_aliases  │
    │ model_info_source_health│
    └─────────────────────┘
```

## Key Modules

### `model_info/service.py` — ModelInfoService

Orchestrates multi-source enrichment:
- `refresh_due_models()` — bulk refresh cycle
- `refresh_model_info()` — single-model refresh
- `get_summary_map()` — dashboard summary
- `health_snapshot()` — runtime diagnostics
- `seed_configured_aliases()` — startup alias seeding

### `model_info/repository.py`

SQLite-backed model info persistence. All sidecar tables.

### `model_info/identity.py`

`resolve_openrouter_record()` — matches source model IDs to local IDs. Exact matching only (no fuzzy).

### `model_info/matching.py`

Tiered identity matching (7 tiers):

| Tier | Method | Description |
|------|--------|-------------|
| 0 | `configured_exact_alias` | Operator-configured aliases |
| 1 | `exact_source_id` | Raw model_id or split variant |
| 2 | `normalized_exact` | NFKC + casefold + separator strip |
| 2b | `deployment_suffix_normalized_exact` | Strip deployment suffixes |
| 2c | `release_suffix_normalized_exact` | Strip date/release suffixes |
| 3 | `regex_rule` | Conservative family patterns |
| 4 | `similarity_guarded` | difflib.SequenceMatcher (disabled by default) |

Normalization: NFKC + `.casefold()` + separator stripping + duplicate-vendor collapse. `MiniMax-M3`, `minimax-m3`, `MiniMax M3` all normalize to `minimaxm3`.

### `model_info/normalization.py`

Model name normalization.

### `model_info/dedup.py`

Deduplication logic.

### `model_info/presentation.py`

Dashboard/API presentation helpers:
- Status labels (`sparse_new` → `sparse`)
- Dashboard filter aliases
- ISO timestamp formatting
- Compact raw-payload-free summaries

### `model_info/scheduler.py`

Refresh scheduling based on status, first-seen age, and config TTLs.

### `model_info/types.py`

Data types.

### `model_info/sources/`

External source adapters:
- `openrouter.py` — OpenRouter `/models` catalog
- `artificial_analysis.py` — Benchmark data (throughput, latency, pricing)
- `huggingface.py` — Model card metadata and pipeline tags
- `provider_catalog.py` — In-memory catalog entries (no network)

## Source Adapter Pattern

`ModelInfoSource` protocol:
- `name` — source identifier
- `priority` — resolution priority
- `fetch_all()` — bulk fetch
- `fetch_one(model_id)` — single model fetch

## Identity Resolution

Local model IDs matched to source records via:
1. Configured exact aliases
2. Exact source model ID
3. Normalized exact match
4. Deployment suffix stripping
5. Release suffix stripping
6. Regex family patterns
7. Similarity matching (disabled by default)

Non-exact matches persist evidence rows in `model_info_match_evidence`.

## Refresh Lifecycle

1. Startup: `seed_configured_aliases()` inserts `[model_info.aliases]` entries
2. Background: successful catalog refreshes reconcile model-info state; due
   external-source work is handled by the model-info service without a
   separate high-frequency scheduler
3. Catalog refresh: reconciliation runs after successful catalog refreshes
4. External sources: fetched once per cycle, matched via identity resolution
5. Single-model: `POST /api/model-info/refresh?model_id=<id>` for immediate refresh

## API Endpoints

| Endpoint | Purpose |
|----------|---------|
| `GET /api/model-info` | Summary list |
| `GET /api/model-info/{model_id}` | Per-model detail |
| `GET /api/model-info/{model_id}/matches` | Match evidence |
| `GET /api/model-info/{model_id}/aliases` | Alias list |
| `GET /api/model-info/sources` | Source health |
| `POST /api/model-info/refresh` | Manual refresh |

## Key Invariants

- Source adapters never break startup, catalog refresh, or routing
- Identity matching is exact only (no fuzzy matching by default)
- Non-exact matches persist evidence rows
- Source health tracks cooldown backoff
- `ModelInfoService` initialized after catalog load
- `/v1/models` enrichment is optional (`include_in_models_endpoint`)
- Dashboard status pills: fresh/partial/sparse/stale/conflict/unmatched
