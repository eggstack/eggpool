"""Minimal protocol for request/response/event wire codecs.

Concrete codecs are deliberately small objects selected by the closed wire
profile registry.  This module contains no vendor schema and no import-by-name
mechanism; later codec implementations can reuse the existing transcoder
helpers while sharing these canonical boundaries.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping

    from eggpool.wire.ir import CanonicalEvent, CanonicalRequest, CanonicalResponse
    from eggpool.wire.types import WireProfile


class WireCodecError(ValueError):
    """A wire codec cannot represent or decode the requested semantics."""


class CanonicalCodec(Protocol):
    """Operations implemented by one concrete wire-surface codec."""

    codec_id: str

    def encode_request(
        self,
        request: CanonicalRequest,
        *,
        profile: WireProfile,
        capability: Mapping[str, object] | None = None,
    ) -> Mapping[str, object]: ...

    def decode_response(self, payload: Mapping[str, object]) -> CanonicalResponse: ...

    def decode_stream_frame(
        self,
        frame: Mapping[str, object],
    ) -> tuple[CanonicalEvent, ...]: ...

    def encode_response(self, response: CanonicalResponse) -> Mapping[str, object]: ...

    def encode_event(self, event: CanonicalEvent) -> bytes: ...


class PassthroughCodec:
    """Marker for native same-surface dispatch.

    Native traffic is intentionally handled by the existing request/response
    byte path.  A caller can use this marker to document that no canonical
    decode/re-encode should occur, without manufacturing a copy of the body.
    """

    codec_id = "passthrough"

    def should_passthrough(
        self,
        *,
        client_surface: str,
        selected_surface: str,
        semantic_adaptation_required: bool = False,
    ) -> bool:
        """Return whether the byte-preserving path is safe."""
        return client_surface == selected_surface and not semantic_adaptation_required


class CodecRegistry:
    """Small explicit registry used by tests and future concrete codecs."""

    def __init__(self) -> None:
        self._codecs: dict[str, CanonicalCodec] = {}

    def register(self, codec: CanonicalCodec) -> None:
        """Register one Python-owned codec, rejecting duplicate IDs."""
        if codec.codec_id in self._codecs:
            raise WireCodecError(f"Duplicate wire codec ID {codec.codec_id!r}")
        self._codecs[codec.codec_id] = codec

    def get(self, codec_id: str) -> CanonicalCodec | None:
        """Return a registered codec, if present."""
        return self._codecs.get(codec_id)

    def require(self, codec_id: str) -> CanonicalCodec:
        """Return a registered codec or fail closed."""
        codec = self.get(codec_id)
        if codec is None:
            raise WireCodecError(f"Wire codec {codec_id!r} is not registered")
        return codec

    def ids(self) -> tuple[str, ...]:
        """Return deterministic registered codec IDs."""
        return tuple(sorted(self._codecs))


__all__ = [
    "CanonicalCodec",
    "CodecRegistry",
    "PassthroughCodec",
    "WireCodecError",
]
