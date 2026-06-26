"""Network diagnostics API endpoint.

- GET /api/network/diagnostics  (always auth-gated)
"""

from __future__ import annotations

from typing import Any

from fastapi import Request  # noqa: TCH002 — FastAPI needs runtime access
from fastapi.responses import JSONResponse


async def handle_network_diagnostics(request: Request) -> JSONResponse:
    """Return a sanitized network diagnostics snapshot.

    Exposes outbound client lifecycle and DNS cache behavior without
    leaking API keys, auth headers, request bodies, or full URLs.
    Always auth-gated regardless of ``dashboard.public``.
    """
    runtime_metrics = request.app.state.runtime_metrics
    snapshot = await runtime_metrics.snapshot()
    outbound = snapshot.get("outbound_client", {})
    dns = snapshot.get("dns_cache", {})

    result: dict[str, Any] = {
        "outbound_clients": {
            "builds_total": outbound.get("build_count", 0),
            "request_count": outbound.get("request_count", 0),
            "error_count": outbound.get("error_count", 0),
            "has_client": outbound.get("has_client", False),
        },
        "dns_cache": {
            "enabled": dns.get("enabled", False),
            "max_entries": dns.get("max_entries"),
            "entries": dns.get("size", 0),
            "hits_total": dns.get("hits", 0),
            "misses_total": dns.get("misses", 0),
            "negative_hits_total": dns.get("negative_hits", 0),
            "stale_hits_total": dns.get("stale_hits", 0),
            "evictions_total": dns.get("evictions", 0),
            "resolutions_total": (dns.get("misses", 0) + dns.get("hits", 0)),
            "errors_total": sum(dns.get("resolution_errors", {}).values())
            if isinstance(dns.get("resolution_errors"), dict)
            else 0,
        },
        "hosts": _build_host_entries(dns),
    }
    return JSONResponse(content=result)


def _build_host_entries(dns: dict[str, Any]) -> list[dict[str, Any]]:
    """Build per-host diagnostics entries from DNS cache snapshot."""
    by_host: dict[str, dict[str, int]] = dns.get("by_host", {})
    entries: list[dict[str, Any]] = []
    for label, counters in sorted(by_host.items()):
        parts = label.split("/", 1)
        host = parts[0]
        family = parts[1] if len(parts) > 1 else "any"
        hits = counters.get("hits", 0)
        misses = counters.get("misses", 0)
        total = hits + misses
        entries.append(
            {
                "host": host,
                "family": family,
                "hits": hits,
                "misses": misses,
                "hit_rate": round(hits / total, 4) if total > 0 else 0.0,
            }
        )
    return entries


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
