"""Update-check API endpoint.

- GET /api/stats/update  (always auth-gated)

The endpoint exposes the periodic PyPI probe state to the dashboard.
Because the payload can advertise a newer eggpool version, the
endpoint is auth-gated regardless of the public/private dashboard
setting — operators should not see actionable version metadata on a
public dashboard.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request  # noqa: TCH002 — FastAPI needs runtime access
from fastapi.responses import JSONResponse

from eggpool.update_checker import UpdateInfo


async def handle_update(request: Request) -> JSONResponse:
    """Return the latest periodic PyPI check result.

    The payload shape matches :class:`UpdateInfo.to_dict` — empty
    strings and ``update_available=False`` mean "no update advertised".
    The dashboard contract is to render nothing in that case.
    """
    update_checker = getattr(request.app.state, "update_checker", None)
    if update_checker is None:
        snapshot: dict[str, Any] = UpdateInfo().to_dict()
    else:
        snapshot = update_checker.snapshot().to_dict()
    return JSONResponse(content=snapshot)


def register_update_routes(
    app: Any,
    *,
    require_auth: bool = False,
) -> None:
    """Attach the update-check route to a FastAPI app.

    Always auth-gated regardless of the ``require_auth`` flag because
    the payload can carry actionable version metadata (newer release,
    install method).  The parameter is kept for parity with the
    runtime-metrics registration helper.
    """
    from fastapi import Depends

    from eggpool.auth import require_auth as _require_auth

    del require_auth  # Always auth-gated; parameter kept for parity

    app.add_api_route(
        path="/api/stats/update",
        endpoint=handle_update,
        methods=["GET"],
        dependencies=[Depends(_require_auth)],
    )


__all__ = [
    "handle_update",
    "register_update_routes",
]
