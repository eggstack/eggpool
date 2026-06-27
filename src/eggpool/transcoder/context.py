"""TranscodeContext — per-request transcoder state."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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

    def is_native(self) -> bool:
        """True if no transcoding is required for this request."""
        return self.client_protocol == self.upstream_protocol
