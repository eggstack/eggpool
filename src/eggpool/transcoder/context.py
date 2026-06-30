"""TranscodeContext — per-request transcoder state."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from eggpool.transcoder.ids import ToolCallIdMap


@dataclass(slots=True)
class TranscodeContext:
    """Per-request transcoder state.

    Carries loss-of-information warnings and per-request id maps. One
    instance is constructed by handle_proxy_request and threaded through
    the coordinator for the lifetime of the request.
    """

    request_id: str
    client_protocol: str
    upstream_protocol: str

    # The set of protocol-mismatch warnings observed during this
    # request. Each entry is a structured dict suitable for log emission.
    # Never fatal in v1; populated by phase-2 translators.
    loss_warnings: list[dict[str, Any]] = field(  # pyright: ignore[reportUnknownVariableType]
        default_factory=list
    )

    # Per-request tool-call id translation map. Lazily populated as
    # bodies / streaming chunks carry call_… ↔ toolu_… ids across
    # protocol boundaries. Empty when both sides share a protocol.
    id_map: ToolCallIdMap = field(default_factory=ToolCallIdMap)

    # Whether the client asked for ``stream_options.include_usage`` on
    # the originating request. The streaming transcoder reads this to
    # decide whether to forward upstream usage chunks.
    request_include_usage: bool = False

    def is_native(self) -> bool:
        """True if no transcoding is required for this request."""
        return self.client_protocol == self.upstream_protocol
