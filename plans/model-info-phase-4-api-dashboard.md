# Model Info Phase 4: API, `/v1/models` Enrichment, and Dashboard UI

## Objective

Expose model-info data through stable Eggpool-specific APIs, compact optional `/v1/models` enrichment, and the existing dashboard model page. This phase turns the sidecar metadata subsystem into visible operator value without bloating OpenAI-compatible responses or coupling UI rendering to source-specific internals.

At the end of this phase, users should be able to see which models have fresh, partial, sparse, conflicting, or stale metadata; inspect source provenance; and view short model summaries/tooltips in the dashboard.

## Current repo touchpoints

`src/eggpool/api/models.py` serializes OpenAI-compatible model objects and already includes an `eggpool` namespace for extension metadata.

`src/eggpool/app.py` implements `/v1/models` inline and calls `serialize_openai_model()` for each exposed catalog model.

`src/eggpool/dashboard/routes.py` registers dashboard pages and JSON endpoints. The `/models` page currently calls `stats.get_model_stats()` and passes rows into `render_models()`.

`src/eggpool/dashboard/render.py` renders dashboard HTML. It should be extended carefully with HTML escaping and no untrusted raw HTML.

`src/eggpool/api/stats.py`, `src/eggpool/api/runtime.py`, and existing dashboard API route registration provide patterns for auth-gated JSON endpoints.

## Design constraints

Do not expose large raw source payloads through `/v1/models`.

Do not break OpenAI-compatible clients that expect `id`, `object`, `created`, `owned_by`, and related fields.

Do not make model-info required for `/v1/models`. If the service is disabled or unavailable, omit enrichment.

Do not render unescaped source-provided text in dashboard HTML.

Do not expose API keys, request contents, or raw provider secrets in provenance or source health.

Do not block dashboard rendering on external network I/O. Dashboard reads persisted/cached model-info only.

## API shape

Add a new module:

```text
src/eggpool/api/model_info.py
```

Register routes from `create_app()` near other always auth-gated runtime/network/update APIs or under dashboard auth gating. The cleanest split:

Dashboard-public read routes should follow dashboard auth policy only if intended for public dashboard.

Manual refresh routes should always require auth.

Suggested endpoints:

```text
GET /api/model-info
GET /api/model-info/{model_id:path}
GET /api/model-info/sources
POST /api/model-info/refresh
```

If the project convention prefers `/v1` API prefix for all JSON APIs, use:

```text
GET /v1/model-info
GET /v1/model-info/{model_id:path}
GET /v1/model-info/sources
POST /v1/model-info/refresh
```

Pick one convention and keep dashboard JS aligned. Avoid duplicating both unless needed for compatibility.

## JSON contracts

Summary list response:

```json
{
  "object": "list",
  "data": [
    {
      "model_id": "minimax-m3",
      "status": "partial",
      "sparse": true,
      "summary": "New model detected; metadata sparse. Callable via configured provider.",
      "sources": ["provider_catalog", "openrouter"],
      "providers": ["minimax"],
      "last_seen_at": "2026-06-29T20:00:00Z",
      "last_refreshed_at": "2026-06-29T20:00:00Z",
      "next_refresh_at": "2026-06-30T02:00:00Z",
      "has_conflicts": false
    }
  ]
}
```

Detail response:

```json
{
  "model_id": "minimax-m3",
  "status": "partial",
  "sparse": true,
  "summary": "New model detected; metadata sparse. Callable via configured provider.",
  "detail": {
    "display_name": "MiniMax M3",
    "family": "MiniMax",
    "limits": {
      "effective_context": 220000,
      "external_context": 1000000
    },
    "modalities": ["text"],
    "supports_tools": true,
    "external_ids": {
      "openrouter": "minimax/minimax-m3"
    },
    "benchmarks": []
  },
  "provenance": {
    "display_name": {"source": "provider_catalog", "observed_at": "..."},
    "external_context": {"source": "openrouter", "observed_at": "..."}
  },
  "conflicts": {},
  "observations": [
    {
      "source": "provider_catalog",
      "source_model_id": "minimax-m3",
      "provider_id": "minimax",
      "observed_at": "...",
      "confidence": 1.0
    }
  ]
}
```

Source health response:

```json
{
  "object": "list",
  "data": [
    {
      "source": "openrouter",
      "enabled": true,
      "last_success_at": "...",
      "last_error_at": null,
      "last_error_class": null,
      "cooldown_until": null
    }
  ]
}
```

Manual refresh response:

```json
{
  "status": "ok",
  "requested": 12,
  "refreshed": 12,
  "skipped": 0,
  "errors": 0
}
```

Manual refresh should accept query/body options only if needed:

```text
?model_id=<id>
?source=provider_catalog|openrouter|all
?force=1
```

Keep this auth-gated regardless of dashboard.public.

## `/v1/models` enrichment

Extend `serialize_openai_model()` in `src/eggpool/api/models.py` to accept an optional compact model-info mapping or one compact summary:

```python
def serialize_openai_model(
    model: Mapping[str, Any],
    *,
    routing_priority: int | None = None,
    routing_priority_max: int | None = None,
    providers: list[str] | None = None,
    model_info: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
```

Add only compact fields under `eggpool["model_info"]`:

```python
if model_info is not None:
    eggpool_meta["model_info"] = {
        "status": model_info.get("status"),
        "sparse": model_info.get("sparse"),
        "summary": model_info.get("summary"),
        "sources": model_info.get("sources", []),
        "last_refreshed_at": model_info.get("last_refreshed_at"),
    }
```

Do not include:

```text
raw observations
full benchmark arrays
large provenance maps
conflict details
source health
```

In `/v1/models` route in `app.py`:

1. Read `config.model_info.include_in_models_endpoint`.
2. Read `app.state.model_info` if available.
3. Fetch summary map once before the loop.
4. For provider-suffixed models, resolve model-info by `base_model_id` if present, otherwise by `model_id`.
5. Pass compact summary into serializer.
6. If model-info service errors, log and omit enrichment.

This keeps `/v1/models` cheap and avoids per-model DB queries.

## Dashboard route integration

In `src/eggpool/dashboard/routes.py`, extend `handle_models()`:

Current flow:

```python
models = await stats.get_model_stats(...)
return render_models(models, ...)
```

New flow:

```python
models = await stats.get_model_stats(...)
model_info_map = {}
model_info_service = getattr(request.app.state, "model_info", None)
if model_info_service is not None:
    model_info_map = await model_info_service.get_compact_summary_map()
return render_models(models, model_info_map=model_info_map, ...)
```

Prefer `asyncio.gather()` to fetch stats and model-info summaries concurrently, but both are DB reads so avoid overcomplicating if the existing single SQLite connection serializes them anyway.

Add JSON handlers to `dashboard/routes.py` only if you choose dashboard-local route registration. Otherwise use `api/model_info.py`.

## Dashboard rendering

Extend `render_models()` in `src/eggpool/dashboard/render.py`.

For each model row:

Identify base model ID. Usage stats rows may include provider/model combinations; normalize using the same parse logic used elsewhere if available.

Look up compact model-info summary.

Add columns or inline elements:

```text
Info status
Sources
Last refreshed
```

A minimal first UI:

```html
<span class="pill pill-sparse" title="New model detected; metadata sparse. Sources: provider_catalog. Last checked: 12m ago.">sparse</span>
```

Status classes:

```text
pill-fresh
pill-partial
pill-sparse
pill-stale
pill-conflict
pill-unmatched
pill-source-unavailable
```

Tooltips should be plain `title` attributes in phase 4 unless the dashboard already has JS tooltip infrastructure. Avoid adding a heavy frontend dependency.

Add CSS to `dashboard/static/dashboard.css` for the status pills. Keep colors theme-compatible by using existing CSS variables where possible.

Do not render source-provided HTML. Escape all strings using existing dashboard helpers or `html.escape`.

## Detail page option

A full model detail page may be included in phase 4 if time permits, but the minimum acceptable phase 4 deliverable is summary/API/tooltip integration.

If adding a detail page:

Route:

```text
/model-info/{model_id:path}
```

Renderer:

```text
render_model_info_detail(info)
```

Sections:

```text
Canonical summary
Limits and capabilities
Provider aliases
Source observations
Conflicts
Refresh state
```

Avoid raw payload display unless hidden behind a query flag and auth-gated.

## Runtime/source visibility

Add model-info source health to a JSON endpoint rather than overloading runtime metrics initially.

Optional runtime page card:

```text
Model info sources:
  provider_catalog: ok
  openrouter: ok, last success 2h ago
  artificial_analysis: disabled
```

If adding runtime card, reuse `model_info.source_health_snapshot()` and keep it compact.

## Error handling

If model-info service is missing or disabled:

`GET /api/model-info` returns either `503` with `{"error":"model_info disabled"}` or an empty list with status metadata. Prefer explicit `503` for manual API and silent omission for dashboard `/v1/models` enrichment.

Dashboard models page should render normally without model-info.

`/v1/models` should never fail because model-info lookup failed.

Manual refresh should return non-2xx on auth failure and source/config errors, but source-specific failures during batch refresh can return `200` with `errors > 0` if the service completed the cycle.

## Tests

Serializer tests:

`test_serialize_openai_model_omits_model_info_when_none`

`test_serialize_openai_model_adds_compact_model_info_under_eggpool_namespace`

`test_serialize_openai_model_does_not_include_raw_observations`

`test_v1_models_uses_base_model_id_for_provider_suffixed_entries`

API tests:

`test_model_info_summary_endpoint_returns_list`

`test_model_info_detail_endpoint_returns_one_model`

`test_model_info_sources_endpoint_redacts_secrets`

`test_model_info_refresh_endpoint_requires_auth`

`test_model_info_disabled_endpoint_behavior`

Dashboard tests:

`test_models_page_renders_without_model_info_service`

`test_models_page_renders_status_pill_when_summary_exists`

`test_models_page_escapes_model_info_summary`

`test_models_page_handles_sparse_and_conflicting_status_classes`

App integration tests:

`test_v1_models_omits_enrichment_when_config_disabled`

`test_v1_models_omits_enrichment_on_model_info_error`

`test_dashboard_api_routes_registered_under_expected_auth_policy`

## Manual verification

Start Eggpool with model-info enabled.

Open `/models` dashboard page and confirm existing usage stats still render.

Confirm model-info pills/tooltips appear for rows with canonical summaries.

Call `/v1/models` and confirm compact `eggpool.model_info` appears only when enabled.

Set `include_in_models_endpoint = false` and confirm `/v1/models` omits model-info while dashboard endpoints still work.

Call model-info JSON endpoints and confirm no raw API keys or secrets appear.

Disable model-info entirely and confirm dashboard and `/v1/models` still work.

## Acceptance criteria

Eggpool exposes model-info summaries through a stable JSON endpoint.

Eggpool exposes per-model detail through a stable JSON endpoint.

Source health is visible without exposing secrets.

`/v1/models` can include compact model-info under the existing `eggpool` namespace when configured.

`/v1/models` remains compact and valid when model-info is disabled or unavailable.

Dashboard `/models` shows status pills and tooltips for model-info summaries.

Dashboard rendering escapes all source-provided text.

No external fetch occurs during dashboard or `/v1/models` request handling.
