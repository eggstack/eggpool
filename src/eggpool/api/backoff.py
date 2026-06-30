"""Read-only API endpoint exposing active upstream backoffs.

Phase 8 of the ``upstream-authoritative-suppression`` plan exposes the
persisted ``account_backoffs`` rows so operators can see *why* an
account is suppressed and when the suppression expires. Local-estimate
quota overage is intentionally absent from this view; only provider-
observed failures populate the underlying table.
"""

from __future__ import annotations

import datetime as _dt
import math
import time
from typing import TYPE_CHECKING, Any, cast

from fastapi import Request  # noqa: TCH002 — FastAPI needs runtime access
from fastapi.responses import JSONResponse

if TYPE_CHECKING:
    from fastapi.responses import Response


def _iso_or_none(epoch: float | None) -> str | None:
    """Convert a POSIX epoch to an ISO 8601 UTC string, or return None."""
    if epoch is None:
        return None
    return _dt.datetime.fromtimestamp(float(epoch), tz=_dt.UTC).isoformat()


def _resolve_now(request: Request) -> float:
    """Resolve the optional reproducible ``now`` query parameter."""
    value = request.query_params.get("now")
    if value is None or value == "":
        return time.time()
    try:
        now = float(value)
    except ValueError as exc:
        raise ValueError("now must be a POSIX epoch timestamp") from exc
    if not math.isfinite(now):
        raise ValueError("now must be a finite POSIX epoch timestamp")
    return now


async def _load_account_names(db: Any, account_ids: set[int]) -> dict[int, str]:
    """Return account names keyed by ID for a non-empty account ID set."""
    ids = sorted(account_ids)
    placeholders = ",".join("?" for _ in ids)
    account_rows = await db.fetch_all(
        f"SELECT id, name FROM accounts WHERE id IN ({placeholders})",
        tuple(ids),
    )
    return {int(r["id"]): str(r["name"]) for r in cast("list[Any]", account_rows)}


def _render_backoff_entry(
    row: Any,
    *,
    account_name: str | None,
) -> dict[str, Any]:
    """Render a repository row as the stable public JSON shape."""
    backoff_until_epoch = row.get("backoff_until_epoch")
    if backoff_until_epoch is not None:
        backoff_until_epoch = float(backoff_until_epoch)
    return {
        "account_name": account_name,
        "model_id": row.get("model_id"),
        "reason": str(row.get("reason") or ""),
        "backoff_until": _iso_or_none(backoff_until_epoch),
        "consecutive_failures": int(row.get("consecutive_failures") or 0),
        "status_code": row.get("status_code"),
        "error_class": row.get("error_class"),
    }


async def handle_backoffs(request: Request) -> Response:
    """GET /api/backoffs.

    Returns the currently active upstream-derived backoffs. Each entry
    joins the persisted backoff row with its account name. The
    ``now`` parameter (POSIX epoch seconds) can be supplied by
    callers to make the snapshot reproducible in tests; the default
    uses wall-clock time.
    """
    repo = getattr(request.app.state, "account_backoff_repo", None)
    db = getattr(request.app.state, "db", None) or getattr(
        request.app.state, "stats_db", None
    )
    if repo is None or db is None:
        return JSONResponse(
            status_code=503,
            content={"error": "backoff repository unavailable"},
        )

    try:
        now = _resolve_now(request)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})

    try:
        rows = await repo.list_active(now=now)
    except Exception:
        return JSONResponse(
            status_code=500,
            content={"error": "failed to read backoffs"},
        )

    name_by_id: dict[int, str] = {}
    if rows:
        account_ids = {int(r["account_id"]) for r in rows}
        try:
            name_by_id = await _load_account_names(db, account_ids)
        except Exception:
            return JSONResponse(
                status_code=500,
                content={"error": "failed to read backoff account names"},
            )

    entries = [
        _render_backoff_entry(
            row,
            account_name=name_by_id.get(int(row["account_id"])),
        )
        for row in rows
    ]

    return JSONResponse(
        content={
            "now": _iso_or_none(now),
            "backoffs": entries,
        }
    )


def register_backoff_routes(app: Any, require_auth: bool = False) -> None:
    """Attach the ``/api/backoffs`` JSON endpoint to a FastAPI app.

    When ``require_auth`` is True the route is gated by the standard
    ``require_auth`` dependency.
    """
    from fastapi import Depends

    from eggpool.auth import require_auth as _require_auth

    dependencies = [Depends(_require_auth)] if require_auth else None
    app.add_api_route(
        path="/api/backoffs",
        endpoint=handle_backoffs,
        methods=["GET"],
        dependencies=dependencies,
    )


__all__ = ["handle_backoffs", "register_backoff_routes"]
