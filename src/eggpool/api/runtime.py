"""Runtime and operations metrics API endpoint.

- GET /api/stats/runtime  (always auth-gated)
"""

from __future__ import annotations

from typing import Any

from fastapi import Request  # noqa: TCH002 — FastAPI needs runtime access
from fastapi.responses import JSONResponse


async def handle_runtime(request: Request) -> JSONResponse:
    """Return a best-effort runtime/operations metrics snapshot.

    This endpoint is always auth-gated even when the dashboard is
    public, because process IDs, memory usage, DB paths, and
    background task names are operational details.
    """
    runtime_metrics = request.app.state.runtime_metrics
    snapshot = await runtime_metrics.snapshot()
    return JSONResponse(content=snapshot)


def register_runtime_routes(
    app: Any,
    *,
    require_auth: bool = False,
) -> None:
    """Attach the runtime metrics route to a FastAPI app.

    The runtime endpoint is **always** auth-gated regardless of the
    ``require_auth`` parameter, because it exposes sensitive
    operational details (PID, memory, DB path, process topology).
    """
    from fastapi import Depends

    from eggpool.auth import require_auth as _require_auth

    app.add_api_route(
        path="/api/stats/runtime",
        endpoint=handle_runtime,
        methods=["GET"],
        dependencies=[Depends(_require_auth)],
    )


__all__ = [
    "handle_runtime",
    "register_runtime_routes",
]
