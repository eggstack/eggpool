"""Network diagnostics API endpoint.

- GET /api/network/diagnostics  (always auth-gated)
"""

from __future__ import annotations

from typing import Any

from fastapi import Request  # noqa: TCH002 — FastAPI needs runtime access
from fastapi.responses import JSONResponse


async def handle_network_diagnostics(request: Request) -> JSONResponse:
    """Return a sanitized network diagnostics snapshot.

    Exposes outbound client lifecycle and provider-pool behavior without
    leaking API keys, auth headers, request bodies, or full URLs.
    Always auth-gated regardless of ``dashboard.public``.
    """
    runtime_metrics = request.app.state.runtime_metrics
    snapshot = await runtime_metrics.snapshot()
    outbound = snapshot.get("outbound_client", {})
    provider_pool = snapshot.get("provider_client_pool", {})

    total_builds = outbound.get("build_count", 0) + provider_pool.get("build_count", 0)
    scopes: dict[str, int] = {}
    scopes.update(outbound.get("scopes", {}))
    provider_builds = provider_pool.get("providers", {})
    for pid, count in provider_builds.items():
        scopes[f"provider:{pid}"] = count

    result: dict[str, Any] = {
        "outbound_clients": {
            "builds_total": total_builds,
            "scopes": scopes,
            "request_count": outbound.get("request_count", 0),
            "error_count": outbound.get("error_count", 0),
            "has_client": outbound.get("has_client", False),
            "per_host_requests": outbound.get("per_host_requests", {}),
            "per_host_errors": outbound.get("per_host_errors", {}),
        },
    }
    return JSONResponse(content=result)


def register_network_routes(
    app: Any,
    *,
    require_auth: bool = False,
) -> None:
    """Attach the network diagnostics route to a FastAPI app.

    The network endpoint is **always** auth-gated regardless of the
    ``require_auth`` parameter, because it exposes operational
    infrastructure details.
    """
    from fastapi import Depends

    from eggpool.auth import require_auth as _require_auth

    app.add_api_route(
        path="/api/network/diagnostics",
        endpoint=handle_network_diagnostics,
        methods=["GET"],
        dependencies=[Depends(_require_auth)],
    )


__all__ = [
    "handle_network_diagnostics",
    "register_network_routes",
]
