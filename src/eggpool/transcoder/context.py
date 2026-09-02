"""TranscodeContext — per-request transcoder state."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from eggpool.transcoder.cache_stability import CacheBoundaryTracker
from eggpool.transcoder.ids import ToolCallIdMap

if TYPE_CHECKING:
    from collections.abc import Mapping

    from eggpool.wire.ir import CanonicalRequest, ReasoningIntent
    from eggpool.wire.types import WireProfile, WireSurfaceName


@dataclass(slots=True)
class TranscodeContext:
    """Per-request transcoder state.

    Carries loss-of-information warnings, per-request id maps, and a
    bounded tracker of cache-boundary events. One instance is
    constructed by handle_proxy_request and threaded through the
    coordinator for the lifetime of the request.
    """

    request_id: str
    client_protocol: str
    upstream_protocol: str

    # Wire-surface identity is intentionally separate from the historical
    # protocol-family labels above.  During migration the protocol fields
    # remain compatibility metadata for routing and legacy transcoders.
    client_surface: str = "chat_completions"
    selected_wire_surface: WireSurfaceName | None = None
    wire_profile: WireProfile | None = None
    canonical_request: CanonicalRequest | None = None
    reasoning_intent: ReasoningIntent | None = None
    transcode_required: bool = False
    semantic_adaptation_required: bool = False

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

    # Phase 3 cache-stability tracker. Records every cache_control
    # boundary that was preserved, relocated, or dropped during
    # translation. Append-only and bounded; never fatal.
    cache_boundary_tracker: CacheBoundaryTracker = field(
        default_factory=CacheBoundaryTracker
    )

    def is_native(self) -> bool:
        """True if no transcoding is required for this request."""
        return self.client_protocol == self.upstream_protocol

    def is_passthrough(self) -> bool:
        """Return whether the selected surface can preserve request bytes."""
        return (
            self.client_surface == self.selected_wire_surface
            and not self.semantic_adaptation_required
        )

    def ensure_canonical_request(
        self,
        payload: Mapping[str, Any],
    ) -> CanonicalRequest:
        """Capture the original semantic request exactly once.

        The compatibility transcoders still own mature field-level mapping,
        but they now attach their source request to the shared IR boundary.
        A later target encoder can therefore use the original intent rather
        than treating an earlier provider payload as its input.
        """
        if self.canonical_request is None:
            from eggpool.wire.ir import (
                CanonicalRequest,
                canonical_request_from_mapping,
                reasoning_intent_from_mapping,
            )

            surface = self.client_surface
            if surface == "chat_completions" and self.client_protocol == "anthropic":
                surface = "messages"
            if isinstance(payload.get("model"), str) and payload["model"].strip():
                self.canonical_request = canonical_request_from_mapping(
                    payload,
                    client_surface=surface,  # type: ignore[arg-type]
                    protocol=self.client_protocol,
                )
            else:
                # Some direct legacy transcoder callers exercise partial
                # payloads without a model.  Keep those tests/embedders
                # compatible while the API boundary enforces model identity.
                self.canonical_request = CanonicalRequest(
                    model="",
                    client_surface=surface,  # type: ignore[arg-type]
                    reasoning=reasoning_intent_from_mapping(payload),
                )
        if self.reasoning_intent is None:
            self.reasoning_intent = self.canonical_request.reasoning
        return self.canonical_request
