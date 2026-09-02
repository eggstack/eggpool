"""Upstream request dispatch helpers extracted from RequestCoordinator."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import httpx

    from eggpool.request.coordinator import ProxyRequestContext

logger = logging.getLogger(__name__)


async def send_upstream_request(
    client: httpx.AsyncClient,
    request: httpx.Request,
    context: ProxyRequestContext,
    selected: Any | None = None,  # noqa: ANN401 — avoids a coordinator cycle
    *,
    local_pre_upstream_recorder: Any | None = None,  # noqa: ANN401
    dispatch_overhead_recorder: Any | None = None,  # noqa: ANN401
    outbound_observer: Any | None = None,  # noqa: ANN401
) -> httpx.Response:
    """Send an upstream request and capture shared dispatch timing.

    Timing boundaries:

    - ``context.request_received_monotonic_ns``: earliest ASGI
      handler entry after auth / body-limit middleware.  Set by
      ``handle_proxy_request``.
    - ``context.started_monotonic_ns``: captured when the
      ``ProxyRequestContext`` is built (after auth, body_read,
      json_parse, model_parse, context_limit, transcode_preflight,
      compression policy, segmentation, compression apply,
      context_build).  ``context.local_pre_upstream_ms`` and the
      coordinator ``DispatchOverheadRecorder`` use this as their
      origin.
    - this function: the dispatch boundary.  ``local_pre_upstream_ms``
      is computed from ``request_received_monotonic_ns`` so the
      operator can see the full EggPool-side window; the existing
      coarse ``dispatch_overhead`` recorder still reflects only the
      coordinator-internal slice (preserved for backward compatibility).

    ``DispatchOverheadRecorder`` is recorded immediately before
    ``client.send`` so the rolling-window distribution reflects
    coordinator-side latency only.  Operators who need a total
    local pre-upstream metric should read ``context.local_pre_upstream_ms``
    instead.
    """
    if context.request_received_monotonic_ns is not None:
        context.local_pre_upstream_ms = max(
            0,
            int(
                (time.perf_counter_ns() - context.request_received_monotonic_ns)
                // 1_000_000
            ),
        )
        if local_pre_upstream_recorder is not None:
            local_pre_upstream_recorder.record_ms(context.local_pre_upstream_ms)
    if dispatch_overhead_recorder is not None:
        dispatch_overhead_recorder.record_ns(
            time.perf_counter_ns() - context.started_monotonic_ns
        )
    connect_start = time.monotonic()
    response = await client.send(request, stream=True)
    if outbound_observer is not None and selected is not None:
        try:
            from eggpool.observability.outbound import build_outbound_observation

            outbound_observer(
                build_outbound_observation(request, response, context, selected)
            )
        except Exception:
            # Observability must never change request behavior or retry policy.
            logger.debug("Outbound observation hook failed", exc_info=True)
    context.upstream_connect_ms = int((time.monotonic() - connect_start) * 1000)
    # Compute upstream_headers_ms using the same monotonic clock as elapsed_ms
    context.upstream_headers_ms = max(
        0, int((time.monotonic() - context.started_monotonic) * 1000)
    )
    return response
