"""Deterministic W012 cross-surface observations from the live Python codecs.

The artifact produced here is deliberately an observation adapter.  It calls
the registered production codecs for request/response/event conversion and
records their bounded values; it does not implement a second transcoder.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from eggpool.jsonx import dumps_bytes
from eggpool.proxy.sse import DecodedSSEFrame, SSEDecoder
from eggpool.proxy.sse_observer import IncrementalSSEObserver
from eggpool.request.body import encode_json_body
from eggpool.request.parsed_payload import ParsedRequestPayload
from eggpool.wire.codecs.compat import AnthropicMessagesCodec, OpenAIChatCodec
from eggpool.wire.codecs.defaults import OpenAIResponsesCodec
from eggpool.wire.ir import CanonicalEvent, canonical_request_from_mapping
from eggpool.wire.registry import build_wire_codec, load_wire_registry
from tests.migration_rs.canonical_wire_fixtures import (
    PRESENCE_REQUEST,
    PUBLIC_SURFACES,
    REQUESTS,
    RESPONSE_PAYLOADS,
    WIRE_PROFILES,
    _frame_mapping,
    _jsonable,
    _profile,
)

W012_REQUESTS: dict[str, dict[str, Any]] = json.loads(json.dumps(REQUESTS))
W012_REQUESTS["chat_completions"]["messages"][2]["content"][1] = {
    "type": "image",
    "source": {"type": "base64", "media_type": "image/png", "data": "AAEC"},
}

FIXTURE_PATH = (
    Path(__file__).parents[2]
    / "migration-rs"
    / "fixtures"
    / "canonical-wire"
    / "w012-cross-surface-observations.json"
)


def _event_projection(event: CanonicalEvent) -> dict[str, Any]:
    return _jsonable(
        {
            "type": event.type,
            "response_id": event.response_id,
            "model": event.model,
            "index": event.index,
            "delta": event.delta,
            "call_id": event.call_id,
            "name": event.name,
            "arguments": event.arguments,
            "finish_reason": event.finish_reason,
            "usage": event.usage,
            "error_type": event.error_type,
            "error_message": event.error_message,
        }
    )


def _frame_projection(raw: bytes) -> list[dict[str, Any]]:
    decoder = SSEDecoder()
    frames: list[DecodedSSEFrame] = []
    frames.extend(decoder.feed(raw))
    frames.extend(decoder.finish().frames)
    result: list[dict[str, Any]] = []
    for frame in frames:
        value: Any = frame.frame.data
        if value != "[DONE]":
            parsed = frame.json_object()
            if parsed is not None:
                value = parsed
        result.append(
            {
                "event": frame.frame.event,
                "data": value,
                "fields": [list(field) for field in frame.frame.fields or ()],
            }
        )
    return result


def _stream_records(
    profile_name: str,
) -> tuple[tuple[str | None, dict[str, Any] | str], ...]:
    if profile_name == "openai_chat_completions":
        return (
            (
                None,
                {
                    "id": "stream-chat",
                    "model": "fixture-model",
                    "choices": [
                        {
                            "delta": {"content": "hi", "reasoning_content": "think"},
                            "finish_reason": None,
                        }
                    ],
                },
            ),
            (
                None,
                {
                    "id": "stream-chat",
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_fixture_1",
                                        "type": "function",
                                        "function": {
                                            "name": "lookup",
                                            "arguments": '{"q":',
                                        },
                                    }
                                ]
                            },
                            "finish_reason": None,
                        }
                    ],
                },
            ),
            (
                None,
                {
                    "id": "stream-chat",
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "function": {"arguments": '"synthetic"}'},
                                    }
                                ]
                            },
                            "finish_reason": None,
                        }
                    ],
                },
            ),
            (
                None,
                {
                    "id": "stream-chat",
                    "choices": [{"delta": {}, "finish_reason": "stop"}],
                },
            ),
            (
                None,
                {
                    "id": "stream-chat",
                    "choices": [{"delta": {}, "finish_reason": None}],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 4,
                        "total_tokens": 14,
                    },
                },
            ),
            (None, "[DONE]"),
        )
    if profile_name == "openai_responses":
        return (
            (
                "response.created",
                {
                    "type": "response.created",
                    "response": {"id": "stream-responses", "model": "fixture-model"},
                },
            ),
            (
                "response.output_text.delta",
                {"type": "response.output_text.delta", "delta": "hi"},
            ),
            (
                "response.reasoning_summary_text.delta",
                {"type": "response.reasoning_summary_text.delta", "delta": "think"},
            ),
            (
                "response.output_item.added",
                {
                    "type": "response.output_item.added",
                    "item": {
                        "type": "function_call",
                        "call_id": "call_fixture_1",
                        "name": "lookup",
                        "arguments": "",
                    },
                },
            ),
            (
                "response.function_call_arguments.delta",
                {
                    "type": "response.function_call_arguments.delta",
                    "item_id": "call_fixture_1",
                    "delta": '{"ok":',
                },
            ),
            (
                "response.output_item.done",
                {
                    "type": "response.output_item.done",
                    "item": {"type": "function_call", "call_id": "call_fixture_1"},
                },
            ),
            (
                "response.completed",
                {
                    "type": "response.completed",
                    "response": {
                        "id": "stream-responses",
                        "usage": {
                            "input_tokens": 2,
                            "output_tokens": 1,
                            "total_tokens": 3,
                        },
                    },
                },
            ),
        )
    if profile_name == "anthropic_messages":
        return (
            (
                "message_start",
                {
                    "type": "message_start",
                    "message": {"id": "stream-anthropic", "model": "fixture-model"},
                },
            ),
            (
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "hi"},
                },
            ),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            (
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {
                        "type": "tool_use",
                        "id": "call_fixture_1",
                        "name": "lookup",
                        "input": {},
                    },
                },
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": '{"ok":true}',
                    },
                },
            ),
            ("content_block_stop", {"type": "content_block_stop", "index": 1}),
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"input_tokens": 2, "output_tokens": 1},
                },
            ),
            ("message_stop", {"type": "message_stop"}),
        )
    if profile_name == "gemini_interactions":
        return (
            (
                "interaction.created",
                {
                    "event_type": "interaction.created",
                    "interaction": {
                        "id": "stream-interactions",
                        "model": "fixture-model",
                    },
                },
            ),
            (
                "step.start",
                {
                    "event_type": "step.start",
                    "index": 0,
                    "step": {"type": "model_output"},
                },
            ),
            (
                "step.delta",
                {
                    "event_type": "step.delta",
                    "index": 0,
                    "delta": {"type": "text", "text": "hi"},
                },
            ),
            (
                "step.start",
                {
                    "event_type": "step.start",
                    "index": 1,
                    "step": {
                        "type": "function_call",
                        "id": "call_fixture_1",
                        "name": "lookup",
                    },
                },
            ),
            (
                "step.delta",
                {
                    "event_type": "step.delta",
                    "index": 1,
                    "delta": {"type": "arguments_delta", "arguments": '{"ok":true}'},
                },
            ),
            ("step.stop", {"event_type": "step.stop", "index": 1}),
            (
                "interaction.completed",
                {
                    "event_type": "interaction.completed",
                    "interaction": {
                        "status": "completed",
                        "usage": {
                            "total_input_tokens": 2,
                            "total_output_tokens": 1,
                            "total_tokens": 3,
                        },
                    },
                },
            ),
        )
    return (
        (
            None,
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": "hi"},
                                {"text": "think", "thought": True},
                                {
                                    "functionCall": {
                                        "id": "call_fixture_1",
                                        "name": "lookup",
                                        "args": {"ok": True},
                                    }
                                },
                            ]
                        }
                    }
                ]
            },
        ),
        (
            None,
            {
                "candidates": [{"finishReason": "STOP"}],
                "usageMetadata": {
                    "promptTokenCount": 2,
                    "candidatesTokenCount": 1,
                    "totalTokenCount": 3,
                },
            },
        ),
    )


def _sse_bytes(records: tuple[tuple[str | None, dict[str, Any] | str], ...]) -> bytes:
    chunks: list[bytes] = []
    for event, data in records:
        if event is not None:
            chunks.append(f"event: {event}\n".encode())
        chunks.append(b"id: fixture-1\n: synthetic comment\n")
        chunks.append(
            b"data: " + (b"[DONE]" if data == "[DONE]" else dumps_bytes(data)) + b"\n\n"
        )
    return b"".join(chunks)


def _stream_observation(profile_name: str, registry: Any) -> dict[str, Any]:
    definition = registry.profiles[profile_name]
    codec = build_wire_codec(definition.stream_codec)
    records = _stream_records(profile_name)
    raw = _sse_bytes(records)
    decoder = SSEDecoder()
    frames: list[DecodedSSEFrame] = []
    frames.extend(decoder.feed(raw))
    frames.extend(decoder.finish().frames)
    events = [
        event
        for frame in frames
        for event in codec.decode_stream_frame(_frame_mapping(frame))
    ]
    clients = {
        "chat_completions": OpenAIChatCodec(),
        "responses": OpenAIResponsesCodec(),
        "messages": AnthropicMessagesCodec(),
    }
    encoded: dict[str, list[dict[str, Any]]] = {}
    for client, client_codec in clients.items():
        client_bytes = b"".join(client_codec.encode_event(event) for event in events)
        encoded[client] = _frame_projection(client_bytes)
    protocol = "anthropic" if profile_name == "anthropic_messages" else "openai"
    observer = IncrementalSSEObserver(
        protocol, request_surface="chat_completions", wire_surface=profile_name
    )
    observer.observe(raw)
    observer.finish()
    snapshot = observer.completion_snapshot
    expected = {
        "event_sequence": [_event_projection(event) for event in events],
        "client_frames": encoded,
        "terminal": {
            "saw_payload": snapshot.saw_payload,
            "saw_terminal_event": snapshot.saw_terminal_event,
            "terminal_kind": snapshot.terminal_kind,
            "saw_usage_completion": any(event.type == "usage" for event in events),
            "incomplete_frame_at_eof": snapshot.incomplete_frame_at_eof,
            "parser_error_count": snapshot.parser_error_count,
        },
        "whole_bytes_hex": raw.hex(),
    }
    fragmented = codec_events_for_fragments(raw, codec)
    expected["fragmented_event_sequence"] = [
        _event_projection(event) for event in fragmented
    ]
    expected["chunk_invariant"] = (
        expected["event_sequence"] == expected["fragmented_event_sequence"]
    )
    return expected


def codec_events_for_fragments(raw: bytes, codec: Any) -> list[CanonicalEvent]:
    decoder = SSEDecoder()
    frames: list[DecodedSSEFrame] = []
    for byte in raw:
        frames.extend(decoder.feed(bytes((byte,))))
    frames.extend(decoder.finish().frames)
    return [
        event
        for frame in frames
        for event in codec.decode_stream_frame(_frame_mapping(frame))
    ]


def build_w012_observations() -> dict[str, Any]:
    """Build the bounded 15-cell request/response/stream oracle."""
    registry = load_wire_registry()
    requests: dict[str, Any] = {}
    for client in PUBLIC_SURFACES:
        raw = encode_json_body(W012_REQUESTS[client])
        parsed = ParsedRequestPayload(raw)
        parsed_value = parsed.parsed_dict
        assert parsed_value is not None
        request = canonical_request_from_mapping(
            parsed_value,
            client_surface=client,
            protocol="anthropic" if client == "messages" else "openai",
        )
        profiles: dict[str, Any] = {}
        for profile_name, definition in sorted(registry.profiles.items()):
            encoded = build_wire_codec(definition.request_codec).encode_request(
                request, profile=_profile(profile_name, definition)
            )
            value = _jsonable(encoded)
            target_surface = {
                "openai_chat_completions": "chat_completions",
                "openai_responses": "responses",
                "anthropic_messages": "messages",
                "gemini_interactions": "chat_completions",
                "gemini_generate_content": "chat_completions",
            }[profile_name]
            roundtrip = (
                canonical_request_from_mapping(
                    encoded,
                    client_surface=target_surface,
                    protocol="anthropic" if target_surface == "messages" else "openai",
                )
                if profile_name != "gemini_generate_content"
                else None
            )
            profiles[profile_name] = {
                "outcome": "success",
                "encoded": value,
                "encoded_bytes_hex": dumps_bytes(value).hex(),
                "canonical": _jsonable(roundtrip),
            }
        requests[client] = {
            "source_body_hex": raw.hex(),
            "canonical": _jsonable(request),
            "profiles": profiles,
        }

    responses: dict[str, Any] = {}
    client_codecs = {
        "chat_completions": OpenAIChatCodec(),
        "responses": OpenAIResponsesCodec(),
        "messages": AnthropicMessagesCodec(),
    }
    for profile_name, definition in sorted(registry.profiles.items()):
        upstream = build_wire_codec(definition.response_codec)
        decoded = upstream.decode_response(RESPONSE_PAYLOADS[profile_name])
        responses[profile_name] = {
            "provider_body_hex": dumps_bytes(RESPONSE_PAYLOADS[profile_name]).hex(),
            "canonical": _jsonable(decoded),
            "clients": {
                client: {
                    "outcome": "success",
                    "encoded": _jsonable(
                        encoded := client_codec.encode_response(decoded)
                    ),
                    "canonical": _jsonable(client_codec.decode_response(encoded)),
                }
                for client, client_codec in client_codecs.items()
            },
        }

    presence_raw = encode_json_body(PRESENCE_REQUEST)
    presence_parsed = ParsedRequestPayload(presence_raw).parsed_dict
    assert presence_parsed is not None
    presence_request = canonical_request_from_mapping(
        presence_parsed, client_surface="chat_completions", protocol="openai"
    )
    negative_cases = {
        "malformed_finite_json": {
            "input_hex": b"{".hex(),
            "status": 200,
            "outcome": "malformed",
        },
        "provider_error_envelope": {
            "input_hex": dumps_bytes(
                {"error": {"type": "overloaded", "message": "synthetic"}}
            ).hex(),
            "status": 503,
            "outcome": "provider_error",
        },
        "truncated_stream_no_false_success": {
            "input_hex": b'data: {"choices":[]}'.hex(),
            "outcome": "premature_eof_no_success",
        },
    }

    return {
        "schema_version": "m6-canonical-wire-w012-observations/v1",
        "oracle_modules": [
            "wire.ir",
            "wire.registry",
            "wire.codecs.compat",
            "wire.codecs.defaults",
            "proxy.sse",
            "proxy.sse_observer",
        ],
        "public_client_surfaces": list(PUBLIC_SURFACES),
        "wire_profiles": list(WIRE_PROFILES),
        "requests": requests,
        "responses": responses,
        "streams": {
            profile: _stream_observation(profile, registry) for profile in WIRE_PROFILES
        },
        "presence_cases": {
            "explicit_zero_and_null": {
                "source_body_hex": presence_raw.hex(),
                "canonical": _jsonable(presence_request),
            }
        },
        "negative_cases": negative_cases,
        "loss_cases": {
            "warn": [
                {
                    "feature": "tools",
                    "outcome": "success_with_ordered_structural_notice",
                },
                {
                    "feature": "reasoning",
                    "outcome": "success_with_ordered_structural_notice",
                },
                {
                    "feature": "structured_output",
                    "outcome": "success_with_ordered_structural_notice",
                },
                {
                    "feature": "media_document",
                    "outcome": "success_with_ordered_structural_notice",
                },
                {
                    "feature": "cache_controls",
                    "outcome": "success_with_ordered_structural_notice",
                },
                {
                    "feature": "provider_extensions",
                    "outcome": "success_with_ordered_structural_notice",
                },
            ],
            "reject": [
                {"feature": "tools", "outcome": "typed_loss_rejection"},
                {"feature": "reasoning", "outcome": "typed_loss_rejection"},
                {"feature": "structured_output", "outcome": "typed_loss_rejection"},
                {"feature": "media_document", "outcome": "typed_loss_rejection"},
                {"feature": "cache_controls", "outcome": "typed_loss_rejection"},
                {"feature": "provider_extensions", "outcome": "typed_loss_rejection"},
            ],
        },
        "w011_regression_fixture": "w011-sse-utf8-observations.json",
        "m7_boundary": [
            "provider_submission",
            "retry",
            "dynamic_wire_negotiation",
            "durable_finalization",
        ],
    }


def observation_json() -> str:
    """Return deterministic compact JSON for the committed W012 artifact."""
    return json.dumps(build_w012_observations(), sort_keys=True, separators=(",", ":"))


__all__ = ["FIXTURE_PATH", "build_w012_observations", "observation_json"]
