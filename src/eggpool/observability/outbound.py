"""Sanitized observation of requests crossing the upstream HTTP boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from eggpool import jsonx

if TYPE_CHECKING:
    import httpx

    from eggpool.request.coordinator import ProxyRequestContext, SelectedAttempt


@dataclass(frozen=True, slots=True)
class OutboundObservation:
    """Structural facts about one upstream response, without wire secrets."""

    provider_id: str
    account_id: str
    model_id: str
    wire_surface: str | None
    path: str
    status_code: int
    auth_scheme: str | None
    semantic_fields: tuple[str, ...]
    streaming: bool
    attempt_ordinal: int
    wire_selection_source: str | None
    candidate_surfaces: tuple[str, ...]


def build_outbound_observation(
    request: httpx.Request,
    response: httpx.Response,
    context: ProxyRequestContext,
    selected: SelectedAttempt,
) -> OutboundObservation:
    """Build a bounded, sanitized diagnostic record for a sent request.

    Only the request path, top-level JSON field names, auth mode, and
    selection metadata are retained. Header values and request/response
    content never enter the observation.
    """
    semantic_fields: tuple[str, ...] = ()
    try:
        payload: Any = jsonx.loads(request.content)
    except (TypeError, ValueError):
        payload = None
    if isinstance(payload, dict):
        payload_mapping = cast("dict[str, object]", payload)
        semantic_fields = tuple(sorted(payload_mapping))

    profile = context.wire_profile
    return OutboundObservation(
        provider_id=selected.provider_id,
        account_id=selected.account_name,
        model_id=context.model_id,
        wire_surface=(profile.surface if profile is not None else None),
        path=request.url.path,
        status_code=response.status_code,
        auth_scheme=(profile.auth.mode if profile is not None else None),
        semantic_fields=semantic_fields,
        streaming=context.streaming,
        attempt_ordinal=selected.attempt_number,
        wire_selection_source=context.wire_selection_source,
        candidate_surfaces=tuple(
            surface
            for surface in context.client_metadata.get("_wire_family_surfaces", ())
            if isinstance(surface, str)
        ),
    )
