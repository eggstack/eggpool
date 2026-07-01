"""Model-info JSON API endpoints.

Endpoints:
- GET  /api/model-info           — summary list
- GET  /api/model-info/{model_id} — per-model detail
- GET  /api/model-info/sources   — source health
- POST /api/model-info/refresh   — manual refresh (always auth-gated)
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import unquote

from fastapi import Request  # noqa: TCH002 — FastAPI needs runtime access
from fastapi.responses import JSONResponse

from eggpool.routing.provider import parse_model_provider

if TYPE_CHECKING:
    from fastapi.responses import Response


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_STATUS_DISPLAY: dict[str, str] = {
    "fresh": "fresh",
    "partial": "partial",
    "sparse_new": "sparse",
    "stale": "stale",
    "conflicting": "conflict",
    "unmatched": "unmatched",
    "source_unavailable": "source-unavailable",
    "manual_override": "manual",
    "withdrawn": "withdrawn",
}


def _compact_summary(info: Any) -> dict[str, Any]:
    """Build a compact summary dict from a CanonicalModelInfo."""
    sources: list[str] = []
    prov_raw = cast("dict[str, Any]", getattr(info, "provenance", {}))
    raw_sources = cast("list[object]", prov_raw.get("sources", []))
    for s in raw_sources:
        sources.append(str(s))

    detail_raw = cast("dict[str, Any]", getattr(info, "detail", {}))
    providers: list[str] = []
    raw_providers = cast("list[object]", detail_raw.get("providers", []))
    for p in raw_providers:
        providers.append(str(p))

    status_raw = getattr(info, "status", "")
    status_str = str(status_raw) if status_raw is not None else ""

    return {
        "model_id": getattr(info, "model_id", ""),
        "status": _STATUS_DISPLAY.get(status_str, status_str),
        "sparse": getattr(info, "sparse", False),
        "summary": getattr(info, "summary", "") or "",
        "sources": sources,
        "providers": providers,
        "last_seen_at": _iso(getattr(info, "last_seen_at", None)),
        "last_refreshed_at": _iso(getattr(info, "last_refreshed_at", None)),
        "next_refresh_at": _iso(getattr(info, "next_refresh_at", None)),
        "has_conflicts": bool(getattr(info, "conflicts", {})),
    }


def _detail_response(info: Any) -> dict[str, Any]:
    """Build a full detail dict from a CanonicalModelInfo.

    Reads from the normalized ``limits`` block when present, falling
    back to the legacy flat keys (``context_tokens`` and
    ``context_window_external``) for canonical rows written before
    Phase B shipped.
    """
    detail = cast("dict[str, Any]", getattr(info, "detail", {}))

    # Limits — prefer the nested limits block, fall back to legacy
    # flat keys for pre-Phase-B canonical rows.
    raw_limits = cast("dict[str, Any]", detail.get("limits", {}))
    limits: dict[str, Any] = {}
    ctx = raw_limits.get("effective_context")
    if ctx is None:
        ctx = detail.get("context_tokens")
    if ctx is not None:
        limits["effective_context"] = ctx
    ext_ctx = raw_limits.get("external_context")
    if ext_ctx is None:
        ext_ctx = detail.get("context_window_external")
    if ext_ctx is not None:
        limits["external_context"] = ext_ctx
    eff_out = raw_limits.get("effective_output")
    if eff_out is None:
        eff_out = detail.get("output_tokens_external") or detail.get(
            "max_output_tokens"
        )
    if eff_out is not None:
        limits["effective_output"] = eff_out
    ext_out = raw_limits.get("external_output")
    if ext_out is None:
        ext_out = detail.get("max_output_tokens")
    if ext_out is not None:
        limits["external_output"] = ext_out

    # Modalities
    modalities: list[str] = []
    raw_modalities = cast("list[object]", detail.get("modalities", []))
    for m in raw_modalities:
        modalities.append(str(m))
    modalities = sorted(set(modalities))

    # External IDs
    external_ids = cast("dict[str, Any]", detail.get("external_ids", {}))

    # Benchmarks
    benchmarks = cast("list[object]", detail.get("benchmarks", []))

    # Hugging Face metadata
    hf_metadata = cast("dict[str, Any]", detail.get("huggingface_metadata", {}))

    # Observations (compact, no raw payloads)
    observations = _build_observations(info)

    compact = _compact_summary(info)
    compact["detail"] = {
        "display_name": detail.get("display_name"),
        "family": detail.get("family"),
        "limits": limits if limits else {},
        "modalities": modalities,
        "supports_tools": detail.get("supports_tools"),
        "external_ids": external_ids,
        "benchmarks": benchmarks,
        "huggingface_metadata": hf_metadata if hf_metadata else {},
        "license": detail.get("license"),
        "release_date": detail.get("release_date"),
    }
    compact["provenance"] = _compact_provenance(info)
    compact["conflicts"] = getattr(info, "conflicts", {})
    compact["observations"] = observations
    return compact


def _compact_provenance(info: Any) -> dict[str, Any]:
    """Build compact provenance (no raw payloads)."""
    prov = getattr(info, "provenance", {})
    result: dict[str, Any] = {}
    if isinstance(prov, dict):
        for key in ("sources", "reconciled_at"):
            if key in prov:
                result[key] = prov[key]
    return result


def _build_observations(info: Any) -> list[dict[str, Any]]:
    """Build compact observation list from detail/provenance.

    This is derived from available metadata rather than raw DB
    observations to avoid leaking raw payloads.
    """
    detail = cast("dict[str, Any]", getattr(info, "detail", {}))
    prov_raw = cast("dict[str, Any]", getattr(info, "provenance", {}))
    sources: list[str] = []
    raw_sources = cast("list[object]", prov_raw.get("sources", []))
    for s in raw_sources:
        sources.append(str(s))

    providers_raw = cast("list[object]", detail.get("providers", []))
    providers: list[str] = []
    for p in providers_raw:
        providers.append(str(p))

    model_id = getattr(info, "model_id", "")
    last_seen = getattr(info, "last_seen_at", None)

    obs: list[dict[str, Any]] = []
    for source in sources:
        obs.append(
            {
                "source": source,
                "source_model_id": model_id,
                "provider_id": providers[0] if providers else None,
                "observed_at": _iso(last_seen),
                "confidence": 1.0,
            }
        )
    return obs


def _iso(dt: Any) -> str | None:
    """Format a datetime as ISO 8601 or return None."""
    if dt is None:
        return None
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.isoformat()
    return str(dt)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def handle_model_info_summary(request: Request) -> Response:
    """GET /api/model-info — summary list of all models."""
    model_info = getattr(request.app.state, "model_info", None)
    if model_info is None:
        return JSONResponse(
            status_code=503,
            content={"error": "model_info disabled"},
        )
    summary_map = await model_info.get_summary_map()
    data = [_compact_summary(info) for info in summary_map.values()]
    return JSONResponse(content={"object": "list", "data": data})


async def handle_model_info_detail(request: Request, model_id: str) -> Response:
    """GET /api/model-info/{model_id} — per-model detail."""
    model_info = getattr(request.app.state, "model_info", None)
    if model_info is None:
        return JSONResponse(
            status_code=503,
            content={"error": "model_info disabled"},
        )
    # URL-decode the path parameter (handles %2F → / for suffixed IDs)
    decoded_id = unquote(model_id)
    info = await model_info.get_summary(decoded_id)
    if info is None:
        return JSONResponse(
            status_code=404,
            content={"error": f"Model {decoded_id!r} not found"},
        )
    return JSONResponse(content=_detail_response(info))


async def handle_model_info_sources(request: Request) -> Response:
    """GET /api/model-info/sources — source health snapshot."""
    model_info = getattr(request.app.state, "model_info", None)
    if model_info is None:
        return JSONResponse(
            status_code=503,
            content={"error": "model_info disabled"},
        )
    snapshot = await model_info.repo.source_health_snapshot()
    data: list[dict[str, Any]] = []
    for source_name, health in snapshot.items():
        data.append(
            {
                "source": source_name,
                "enabled": health.get("enabled", False),
                "last_success_at": health.get("last_success_at"),
                "last_error_at": health.get("last_error_at"),
                "last_error_class": health.get("last_error_class"),
                "cooldown_until": health.get("cooldown_until"),
                "failure_count": health.get("failure_count", 0),
                "last_status_code": health.get("last_status_code"),
                "rate_limited_until": health.get("rate_limited_until"),
                "last_success_duration_ms": health.get("last_success_duration_ms"),
                "last_payload_count": health.get("last_payload_count"),
            }
        )
    return JSONResponse(content={"object": "list", "data": data})


async def handle_model_info_aliases(request: Request, model_id: str) -> Response:
    """GET /api/model-info/{model_id}/aliases — aliases for a model.

    Returns both the flat alias list (legacy shape) and a
    source-keyed list so callers can tell which source each alias
    is configured for.  Source-keyed entries include ``source``,
    ``alias``, ``provider_id``, ``confidence``, and ``active``.
    """
    model_info = getattr(request.app.state, "model_info", None)
    if model_info is None:
        return JSONResponse(
            status_code=503,
            content={"error": "model_info disabled"},
        )
    decoded_id = unquote(model_id)
    flat_aliases = await model_info.repo.get_aliases_for_model(decoded_id)
    source_rows = await model_info.repo.list_alias_rows_for_model(decoded_id)
    return JSONResponse(
        content={
            "model_id": decoded_id,
            "aliases": flat_aliases,
            "aliases_by_source": source_rows,
        }
    )


_ALLOWED_REFRESH_SOURCES: frozenset[str] = frozenset(
    {
        "",
        "all",
        "provider_catalog",
        "openrouter",
        "artificial_analysis",
        "huggingface",
    }
)


def _normalize_refresh_source(source_filter: str | None) -> str | None:
    """Validate the ``source`` query param and normalize ``all`` -> ``None``.

    Returns ``None`` when the filter is absent or empty (also when it
    equals ``"all"``).  Returns the literal filter string when it is
    one of the configured source names.  Raises ``ValueError`` for
    any other value so the caller can return HTTP 400.
    """
    if source_filter is None:
        return None
    if source_filter in _ALLOWED_REFRESH_SOURCES:
        return None if source_filter in ("", "all") else source_filter
    raise ValueError(f"unknown model-info source: {source_filter}")


async def handle_model_info_refresh(request: Request) -> Response:
    """POST /api/model-info/refresh — manual refresh.

    Always auth-gated. Accepts optional query params:
      ?model_id=<id>  — refresh a single model. Provider-suffixed
                        IDs (``gpt-4o/openai``) are accepted; the
                        suffix is stripped via
                        :func:`parse_model_provider` so the canonical
                        base model row is refreshed with the provider
                        value forwarded as the ``provider_id``
                        filter.
      ?source=provider_catalog|openrouter|artificial_analysis|huggingface
                     — restrict single-model refresh to one source
                        (in addition to the always-run provider
                        catalog).  ``all`` (or absent) means every
                        enabled source.
      ?force=1        — force refresh even if not due

    Unknown ``source`` values are rejected with HTTP 400.
    """
    model_info = getattr(request.app.state, "model_info", None)
    if model_info is None:
        return JSONResponse(
            status_code=503,
            content={"error": "model_info disabled"},
        )

    raw_model_id = request.query_params.get("model_id")
    source_filter = request.query_params.get("source")
    force = request.query_params.get("force") in {"1", "true", "yes"}

    try:
        source_arg = _normalize_refresh_source(source_filter)
    except ValueError as err:
        return JSONResponse(status_code=400, content={"error": str(err)})

    if raw_model_id:
        requested_model_id = unquote(raw_model_id)
        # Parse a provider suffix off the URL-decoded id so the
        # canonical base model row is refreshed and the provider
        # value is forwarded to the service for narrower source
        # matching.  Unknown suffix fragments fall through to the
        # base-id path so legacy callers that pass a literal model
        # id (e.g. ``gpt-4o/openrouter`` when ``openrouter`` is not
        # a configured provider) still work.
        config = getattr(request.app.state, "config", None)
        known_providers: set[str] | None = None
        if config is not None:
            providers_cfg: Any = None
            try:
                # ``getattr`` defensively against configs that raised
                # on attribute access; the cast narrows to ``dict``
                # so ``str(k)`` below is well-typed.
                providers_cfg = cast("dict[str, Any]", getattr(config, "providers", {}))
            except Exception:
                providers_cfg = None
            if isinstance(providers_cfg, dict):
                typed_providers_cfg = cast("dict[str, Any]", providers_cfg)
                known_providers = {str(k) for k in typed_providers_cfg}
        lookup_id, provider_suffix = parse_model_provider(
            requested_model_id, known_providers
        )
        result = await model_info.refresh_model_info(
            lookup_id,
            provider_id=provider_suffix,
            source=source_arg,
            force=force,
        )
        body: dict[str, Any] = {
            "status": "ok",
            "scope": "model",
            "requested_model_id": requested_model_id,
            "model_id": lookup_id,
            "provider_id": provider_suffix,
            "requested": result.get("requested", 0),
            "refreshed": result.get("refreshed", 0),
            "skipped": result.get("skipped", 0),
            "errors": result.get("errors", 0),
            "sources_attempted": result.get("sources_attempted", []),
            "sources_matched": result.get("sources_matched", []),
            "observations": result.get("observations", 0),
        }
        return JSONResponse(content=body)

    # Full refresh cycle
    if force:
        # force=1 without model_id: refresh a bounded batch of all
        # catalog models regardless of ``next_refresh_at``. Bounded so
        # a single endpoint hit does not block the event loop for
        # minutes on large fleets.
        batch_result = await model_info.force_refresh_batch(
            batch_size=model_info._config.max_models_per_cycle,
        )
        return JSONResponse(
            content={
                "status": "ok",
                "scope": "force_batch",
                "requested": batch_result.get("requested", 0),
                "refreshed": batch_result.get("refreshed", 0),
                "skipped": batch_result.get("skipped", 0),
                "errors": batch_result.get("errors", 0),
                "sources_attempted": batch_result.get("sources_attempted", []),
                "sources_matched": batch_result.get("sources_matched", []),
                "observations": batch_result.get("observations", 0),
            }
        )
    result = await model_info.refresh_due_models()
    refreshed = result.get("refreshed", 0)
    total = result.get("total", 0)
    skipped = result.get("skipped", 0)
    return JSONResponse(
        content={
            "status": "ok",
            "scope": "cycle",
            "requested": total,
            "refreshed": refreshed,
            "skipped": skipped,
            "errors": 0,
        }
    )


def register_model_info_routes(app: Any, require_auth: bool = False) -> None:
    """Attach model-info JSON routes to a FastAPI app.

    GET routes follow the same auth policy as the caller (dashboard-public
    or always-auth).  The POST refresh route is always auth-gated.
    """
    from fastapi import Depends

    from eggpool.auth import require_auth as _require_auth

    dependencies = [Depends(_require_auth)] if require_auth else None

    app.add_api_route(
        path="/api/model-info",
        endpoint=handle_model_info_summary,
        methods=["GET"],
        dependencies=dependencies,
    )
    app.add_api_route(
        path="/api/model-info/sources",
        endpoint=handle_model_info_sources,
        methods=["GET"],
        dependencies=dependencies,
    )
    app.add_api_route(
        path="/api/model-info/{model_id:path}",
        endpoint=handle_model_info_detail,
        methods=["GET"],
        dependencies=dependencies,
    )
    app.add_api_route(
        path="/api/model-info/{model_id:path}/aliases",
        endpoint=handle_model_info_aliases,
        methods=["GET"],
        dependencies=dependencies,
    )
    # Manual refresh is ALWAYS auth-gated regardless of dashboard.public
    app.add_api_route(
        path="/api/model-info/refresh",
        endpoint=handle_model_info_refresh,
        methods=["POST"],
        dependencies=[Depends(_require_auth)],
    )


__all__ = [
    "handle_model_info_aliases",
    "handle_model_info_detail",
    "handle_model_info_refresh",
    "handle_model_info_sources",
    "handle_model_info_summary",
    "register_model_info_routes",
]
