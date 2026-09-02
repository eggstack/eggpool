"""Runtime bridges between selected wire codecs and public client surfaces."""

from __future__ import annotations

from typing import TYPE_CHECKING

from eggpool.jsonx import loads as jsonx_loads
from eggpool.wire.codecs.compat import AnthropicMessagesCodec, OpenAIChatCodec
from eggpool.wire.codecs.defaults import OpenAIResponsesCodec
from eggpool.wire.registry import build_wire_codec

if TYPE_CHECKING:
    from collections.abc import Mapping

    from eggpool.proxy.sse import DecodedSSEFrame, SSEDecodeResult
    from eggpool.wire.codecs.base import CanonicalCodec
    from eggpool.wire.ir import CanonicalRequest
    from eggpool.wire.types import WireProfile


def client_codec(surface: str) -> CanonicalCodec:
    """Return the codec for one of EggPool's public client surfaces."""
    if surface == "messages":
        return AnthropicMessagesCodec()
    if surface == "responses":
        return OpenAIResponsesCodec()
    return OpenAIChatCodec()


def selected_wire_surface_for_client(surface: str) -> str:
    """Map a public client surface to its native built-in wire surface."""
    if surface == "messages":
        return "anthropic_messages"
    if surface == "responses":
        return "openai_responses"
    return "openai_chat_completions"


def needs_wire_codec_adaptation(
    *, client_surface: str, selected_surface: str | None
) -> bool:
    """Return whether the selected profile needs canonical adaptation."""
    return (
        selected_surface is not None
        and selected_surface != selected_wire_surface_for_client(client_surface)
    )


def encode_selected_request(
    request: CanonicalRequest,
    profile: WireProfile,
    *,
    capability: Mapping[str, object] | None = None,
) -> Mapping[str, object]:
    """Encode canonical source intent using the selected profile codec."""
    codec = build_wire_codec(profile.request_codec)
    return codec.encode_request(request, profile=profile, capability=capability)


def adapt_response(
    payload: Mapping[str, object],
    profile: WireProfile,
    *,
    client_surface: str,
) -> Mapping[str, object]:
    """Decode a selected upstream response and encode the client grammar."""
    upstream = build_wire_codec(profile.response_codec).decode_response(payload)
    return client_codec(client_surface).encode_response(upstream)


class CanonicalWireStreamingAdapter:
    """Incrementally translate selected-profile events into client SSE."""

    def __init__(self, profile: WireProfile, *, client_surface: str) -> None:
        self.client_protocol = "anthropic" if client_surface == "messages" else "openai"
        self.upstream_protocol = self.client_protocol
        self._upstream = build_wire_codec(profile.stream_codec)
        self._client: CanonicalCodec = client_codec(client_surface)
        self._saw_terminal_event = False

    @property
    def saw_terminal_event(self) -> bool:
        return self._saw_terminal_event

    @property
    def usage(self) -> object:
        """Compatibility property; the coordinator owns usage observation."""
        from eggpool.proxy.usage import StreamUsageResult

        return StreamUsageResult()

    def translate_frame(self, frame: DecodedSSEFrame) -> list[bytes]:
        if frame.frame.is_comment_only:
            return []
        data: object = frame.frame.data
        if data != "[DONE]":
            parsed = frame.json_object(jsonx_loads)
            if parsed is not None:
                data = parsed
        frame_mapping: dict[str, object] = {"data": data}
        if frame.frame.event is not None:
            frame_mapping["event"] = frame.frame.event
        events = self._upstream.decode_stream_frame(frame_mapping)
        output: list[bytes] = []
        for event in events:
            if event.type in {"response_complete", "response_incomplete", "error"}:
                self._saw_terminal_event = True
            encoded = self._client.encode_event(event)
            if encoded:
                output.append(encoded)
        return output

    def finish(self, completion: SSEDecodeResult | None = None) -> list[bytes]:
        del completion
        # A clean transport EOF without a codec terminal event is deliberately
        # silent. The coordinator's EOF classifier owns failure semantics.
        return []

    def feed(self, chunk: bytes) -> list[bytes]:
        del chunk
        raise RuntimeError("Use the shared SSE decoder and translate_frame")

    def flush(self) -> list[bytes]:
        return []


__all__ = [
    "CanonicalWireStreamingAdapter",
    "adapt_response",
    "client_codec",
    "encode_selected_request",
    "needs_wire_codec_adaptation",
    "selected_wire_surface_for_client",
]
