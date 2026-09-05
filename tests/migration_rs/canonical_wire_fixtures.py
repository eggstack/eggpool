"""Deterministic W001 observations built from the live Python wire boundary.

This module is intentionally a test oracle, not a second codec implementation.
It records bounded semantic values from production request, IR, registry, SSE,
usage, and codec modules.  The fixture inputs live in
``migration-rs/fixtures/canonical-wire`` so a later Rust implementation can
consume the same cases without importing the coordinator or network stack.
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from eggpool.constants import MAX_REQUEST_BODY_BYTES, MAX_SSE_FRAME_SIZE
from eggpool.jsonx import dumps_bytes, loads
from eggpool.proxy.normalized_usage import normalize_usage
from eggpool.proxy.sse import DecodedSSEFrame, SSEDecoder
from eggpool.proxy.sse_observer import IncrementalSSEObserver
from eggpool.request.body import encode_json_body
from eggpool.request.limits import (
    MAX_ESTIMATED_INPUT_TOKENS,
    estimate_context_input_tokens,
    estimate_input_tokens,
    estimate_reservation_tokens,
)
from eggpool.request.parsed_payload import ParsedRequestPayload
from eggpool.transcoder import LOSS_WARNING_KINDS
from eggpool.transcoder.errors import parse_upstream_error
from eggpool.wire.ir import (
    CanonicalRequest,
    canonical_request_from_mapping,
)
from eggpool.wire.registry import build_wire_codec, load_wire_registry
from eggpool.wire.types import ResolvedAuthShape, WireProfile

FIXTURE_DIR = Path(__file__).parents[2] / "migration-rs" / "fixtures" / "canonical-wire"
MATRIX_PATH = FIXTURE_DIR / "w001-fixture-matrix.json"
W011_SSE_UTF8_PATH = FIXTURE_DIR / "w011-sse-utf8-observations.json"

PUBLIC_SURFACES = ("chat_completions", "responses", "messages")
WIRE_PROFILES = (
    "openai_chat_completions",
    "openai_responses",
    "anthropic_messages",
    "gemini_interactions",
    "gemini_generate_content",
)

RICH_CHAT_REQUEST: dict[str, Any] = {
    "model": "fixture-model",
    "messages": [
        {"role": "system", "content": "You are a synthetic assistant."},
        {"role": "developer", "content": "Use compact, deterministic output."},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": 'Hello, 世界; escaped "quote".'},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,AAEC"},
                },
            ],
        },
        {
            "role": "assistant",
            "content": "I will use the tool.",
            "tool_calls": [
                {
                    "id": "call_fixture_1",
                    "type": "function",
                    "function": {
                        "name": "lookup",
                        "arguments": '{"q":"synthetic"}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_fixture_1",
            "content": "synthetic tool result",
        },
    ],
    "stream": True,
    "max_tokens": 32,
    "temperature": 0,
    "top_p": 0.5,
    "stop": ["<END>"],
    "tools": [
        {
            "type": "function",
            "function": {
                "name": "lookup",
                "description": "Find a synthetic record.",
                "parameters": {
                    "type": "object",
                    "properties": {"q": {"type": "string"}},
                    "required": ["q"],
                },
            },
        }
    ],
    "tool_choice": {"type": "function", "function": {"name": "lookup"}},
    "parallel_tool_calls": False,
    "reasoning_effort": "medium",
    "response_format": {
        "type": "json_schema",
        "json_schema": {"name": "answer", "strict": True, "schema": {"type": "object"}},
    },
    "cache_control": {"type": "ephemeral"},
}

REQUESTS: dict[str, dict[str, Any]] = {
    "chat_completions": RICH_CHAT_REQUEST,
    "responses": {
        "model": "fixture-model",
        "input": "Hello, 世界",
        "stream": True,
        "max_output_tokens": 32,
        "reasoning": {"effort": "low"},
        "text": {"format": {"type": "json_schema", "name": "answer"}},
    },
    "messages": {
        "model": "fixture-model",
        "system": [{"type": "text", "text": "You are synthetic."}],
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "Hello, 世界"}]},
            {
                "role": "assistant",
                "content": [{"type": "thinking", "thinking": "briefly"}],
            },
        ],
        "stream": True,
        "max_tokens": 32,
        "thinking": {"type": "enabled", "budget_tokens": 128},
        "tools": [
            {
                "name": "lookup",
                "description": "Find a synthetic record.",
                "input_schema": {"type": "object", "properties": {}},
            }
        ],
    },
}

PRESENCE_REQUEST: dict[str, Any] = {
    "model": "fixture-model",
    "messages": [{"role": "user", "content": "presence"}],
    "temperature": 0,
    "top_p": None,
    "stream": False,
    "max_tokens": 0,
}

RESPONSE_PAYLOADS: dict[str, dict[str, Any]] = {
    "openai_chat_completions": {
        "id": "resp-chat",
        "model": "fixture-model",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "synthetic answer",
                    "reasoning_content": "synthetic reasoning",
                    "tool_calls": [
                        {
                            "id": "call_fixture_1",
                            "function": {"name": "lookup", "arguments": '{"ok":true}'},
                        }
                    ],
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 4,
            "total_tokens": 14,
            "prompt_tokens_details": {"cached_tokens": 2},
        },
    },
    "openai_responses": {
        "id": "resp-responses",
        "model": "fixture-model",
        "status": "completed",
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "synthetic answer"}],
            },
            {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": "synthetic reasoning"}],
            },
            {
                "type": "function_call",
                "call_id": "call_fixture_1",
                "name": "lookup",
                "arguments": '{"ok":true}',
            },
        ],
        "usage": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
    },
    "anthropic_messages": {
        "id": "resp-anthropic",
        "model": "fixture-model",
        "content": [
            {"type": "text", "text": "synthetic answer"},
            {"type": "thinking", "thinking": "synthetic reasoning"},
            {
                "type": "tool_use",
                "id": "call_fixture_1",
                "name": "lookup",
                "input": {"ok": True},
            },
        ],
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": 10,
            "output_tokens": 4,
            "cache_read_input_tokens": 2,
            "cache_creation_input_tokens": 1,
        },
    },
    "gemini_interactions": {
        "interaction": {
            "id": "resp-interactions",
            "model": "fixture-model",
            "status": "completed",
            "steps": [
                {
                    "type": "model_output",
                    "content": [{"type": "text", "text": "synthetic answer"}],
                }
            ],
            "usage": {
                "total_input_tokens": 10,
                "total_output_tokens": 4,
                "total_tokens": 14,
            },
        }
    },
    "gemini_generate_content": {
        "responseId": "resp-generate",
        "modelVersion": "fixture-model",
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"text": "synthetic answer"},
                        {
                            "functionCall": {
                                "id": "call_fixture_1",
                                "name": "lookup",
                                "args": {"ok": True},
                            }
                        },
                    ]
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 10,
            "candidatesTokenCount": 4,
            "totalTokenCount": 14,
        },
    },
}

STREAM_RECORDS: dict[str, tuple[tuple[str | None, dict[str, Any] | str], ...]] = {
    "openai_chat_completions": (
        (
            None,
            {
                "id": "stream-chat",
                "model": "fixture-model",
                "choices": [{"delta": {"content": "hi"}, "finish_reason": None}],
            },
        ),
        (
            None,
            {
                "id": "stream-chat",
                "model": "fixture-model",
                "choices": [{"delta": {}, "finish_reason": "stop"}],
            },
        ),
        (None, "[DONE]"),
    ),
    "openai_responses": (
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
            "response.completed",
            {
                "type": "response.completed",
                "response": {
                    "id": "stream-responses",
                    "usage": {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
                },
            },
        ),
    ),
    "anthropic_messages": (
        (
            "message_start",
            {
                "type": "message_start",
                "message": {"id": "stream-anthropic", "model": "fixture-model"},
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
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 1},
            },
        ),
        ("message_stop", {}),
    ),
    "gemini_interactions": (
        (
            "interaction.created",
            {
                "event_type": "interaction.created",
                "interaction": {"id": "stream-interactions", "model": "fixture-model"},
            },
        ),
        (
            "step.delta",
            {"event_type": "step.delta", "delta": {"type": "text", "text": "hi"}},
        ),
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
    ),
    "gemini_generate_content": (
        (None, {"candidates": [{"content": {"parts": [{"text": "hi"}]}}]}),
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
    ),
}


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "value") and isinstance(value.value, str):
        return value.value
    return value


def _summary(request: CanonicalRequest) -> dict[str, Any]:
    return _jsonable(request)


def _profile(surface: str, definition: Any) -> WireProfile:
    path = {
        "openai_chat_completions": "/chat/completions",
        "openai_responses": "/responses",
        "anthropic_messages": "/messages",
        "gemini_interactions": "/interactions",
        "gemini_generate_content": "/models/{model}:streamGenerateContent",
    }[surface]
    return WireProfile(
        surface=surface,
        request_codec=definition.request_codec,
        response_codec=definition.response_codec,
        stream_codec=definition.stream_codec,
        path_template=path,
        stream_path_template=path,
        auth=ResolvedAuthShape("bearer", "Authorization", "Bearer"),
    )


def _request_observations(registry: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for surface in PUBLIC_SURFACES:
        payload = REQUESTS[surface]
        raw = encode_json_body(payload)
        parsed = ParsedRequestPayload(raw)
        parsed_value = parsed.parsed_dict
        assert parsed_value is not None
        request = canonical_request_from_mapping(
            parsed_value,
            client_surface=surface,
            protocol="anthropic" if surface == "messages" else "openai",
        )
        result[surface] = {
            "raw_body_bytes": len(raw),
            "parsed_model": parsed.model_id,
            "streaming": parsed.streaming,
            "canonical": _summary(request),
            "encoded_by_profile": {
                profile_name: _jsonable(
                    build_wire_codec(definition.request_codec).encode_request(
                        request,
                        profile=_profile(profile_name, definition),
                    )
                )
                for profile_name, definition in sorted(registry.profiles.items())
            },
        }
    presence_raw = encode_json_body(PRESENCE_REQUEST)
    presence = canonical_request_from_mapping(PRESENCE_REQUEST)
    result["presence"] = {
        "payload_keys": sorted(PRESENCE_REQUEST),
        "canonical": _summary(presence),
        "encoded_bytes": len(presence_raw),
    }
    invalid: list[dict[str, str]] = []
    for label, raw in (
        ("malformed_json", b"{"),
        ("wrong_top_level", b"[]"),
        ("missing_model", b'{"messages": []}'),
        ("blank_model", b'{"model":"  "}'),
    ):
        try:
            value = loads(raw)
            if not isinstance(value, dict):
                raise ValueError("top_level_not_object")
            canonical_request_from_mapping(value)
        except (TypeError, ValueError) as exc:
            invalid.append({"case": label, "reason": _error_code(str(exc), label)})
    result["invalid"] = invalid
    return result


def _error_code(message: str, fallback: str) -> str:
    lowered = message.casefold()
    if "model" in lowered:
        return "missing_or_invalid_model"
    if "object" in lowered or "json" in lowered:
        return "invalid_json_or_top_level"
    return fallback


def _response_observations(registry: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for profile_name, definition in sorted(registry.profiles.items()):
        codec = build_wire_codec(definition.response_codec)
        canonical = codec.decode_response(RESPONSE_PAYLOADS[profile_name])
        encoded = codec.encode_response(canonical)
        result[profile_name] = {
            "canonical": _summary(canonical),
            "encoded": _jsonable(encoded),
            "provider_error": _jsonable(
                parse_upstream_error(
                    429,
                    {"error": {"type": "rate_limit", "message": "synthetic retry"}},
                    protocol="anthropic"
                    if profile_name == "anthropic_messages"
                    else "openai",
                )
            ),
        }
    return result


def _frame_mapping(frame: DecodedSSEFrame) -> dict[str, object]:
    data: object = frame.frame.data
    if data != "[DONE]":
        parsed = frame.json_object()
        if parsed is not None:
            data = parsed
    result: dict[str, object] = {"data": data}
    if frame.frame.event is not None:
        result["event"] = frame.frame.event
    return result


def _stream_bytes(
    profile_name: str,
    *,
    line_ending: bytes = b"\n",
    records: tuple[tuple[str | None, dict[str, Any] | str], ...] | None = None,
) -> bytes:
    chunks: list[bytes] = []
    for event, data in records or STREAM_RECORDS[profile_name]:
        if event is not None:
            chunks.append(b"event: " + event.encode("ascii") + line_ending)
        if data == "[DONE]":
            chunks.append(b"data: [DONE]" + line_ending)
        else:
            chunks.append(b"id: fixture-1" + line_ending)
            chunks.append(b": synthetic comment" + line_ending)
            encoded = dumps_bytes(data)
            chunks.append(b"data: " + encoded + line_ending)
        chunks.append(line_ending)
    return b"".join(chunks)


def _stream_observation(profile_name: str, registry: Any) -> dict[str, Any]:
    definition = registry.profiles[profile_name]
    codec = build_wire_codec(definition.stream_codec)
    raw = _stream_bytes(profile_name)
    decoder = SSEDecoder()
    frames: list[DecodedSSEFrame] = []
    for byte in raw:
        frames.extend(decoder.feed(bytes((byte,))))
    eof = decoder.finish()
    frames.extend(eof.frames)
    events = [
        event
        for frame in frames
        for event in codec.decode_stream_frame(_frame_mapping(frame))
    ]

    observer = IncrementalSSEObserver(
        "anthropic" if profile_name == "anthropic_messages" else "openai",
        request_surface="responses"
        if profile_name == "openai_responses"
        else "chat_completions",
        wire_surface=profile_name,
    )
    observer.observe(raw)
    observer.finish()
    snapshot = observer.completion_snapshot
    crlf = _stream_bytes(profile_name, line_ending=b"\r\n")
    crlf_decoder = SSEDecoder()
    crlf_frames = [
        frame for byte in crlf for frame in crlf_decoder.feed(bytes((byte,)))
    ]
    crlf_frames.extend(crlf_decoder.finish().frames)
    split_event_types = [
        event.type
        for frame in crlf_frames
        for event in codec.decode_stream_frame(_frame_mapping(frame))
    ]
    return {
        "fixture_bytes": len(raw),
        "frame_count": len(frames),
        "frame_data_lines": [frame.frame.data.count("\n") + 1 for frame in frames],
        "event_types": [event.type for event in events],
        "chunk_split_event_types": split_event_types,
        "chunk_invariant": [event.type for event in events] == split_event_types,
        "terminal": {
            "saw_payload": snapshot.saw_payload,
            "saw_terminal_event": snapshot.saw_terminal_event,
            "terminal_kind": snapshot.terminal_kind,
            "saw_usage_completion": snapshot.saw_usage_completion,
            "incomplete_frame_at_eof": snapshot.incomplete_frame_at_eof,
            "parser_error_count": snapshot.parser_error_count,
            "bytes_observed": snapshot.bytes_observed,
        },
        "premature_eof_terminal": _premature_eof(profile_name),
        "oversized_unterminated": _oversized_unterminated(),
    }


def _premature_eof(profile_name: str) -> dict[str, Any]:
    raw = _stream_bytes(profile_name, records=STREAM_RECORDS[profile_name][:-1])
    decoder = SSEDecoder()
    for byte in raw:
        decoder.feed(bytes((byte,)))
    eof = decoder.finish()
    observer = IncrementalSSEObserver(
        "anthropic" if profile_name == "anthropic_messages" else "openai",
        request_surface="responses"
        if profile_name == "openai_responses"
        else "chat_completions",
        wire_surface=profile_name,
    )
    observer.observe(raw)
    observer.finish()
    return {
        "incomplete_frame": eof.incomplete_frame,
        "discarded": eof.discarded_frame_count,
        "saw_terminal_event": observer.completion_snapshot.saw_terminal_event,
    }


def _oversized_unterminated() -> dict[str, Any]:
    decoder = SSEDecoder(max_frame_bytes=32)
    decoder.feed(b"data: " + b"x" * 64)
    eof = decoder.finish()
    return {
        "incomplete_frame": eof.incomplete_frame,
        "discarded": eof.discarded_frame_count,
    }


def _sse_utf8_frame(frame: DecodedSSEFrame) -> dict[str, Any]:
    return {
        "event": frame.frame.event,
        "data": frame.frame.data,
        "fields": [list(field) for field in frame.frame.fields or ()],
        "is_comment_only": frame.frame.is_comment_only,
    }


def _sse_utf8_observe(
    raw: bytes,
    *,
    chunks: list[bytes],
    profile_name: str | None = None,
) -> dict[str, Any]:
    decoder = SSEDecoder()
    frames: list[DecodedSSEFrame] = []
    for chunk in chunks:
        frames.extend(decoder.feed(chunk))
    eof = decoder.finish()
    frames.extend(eof.frames)
    result: dict[str, Any] = {
        "frames": [_sse_utf8_frame(frame) for frame in frames],
        "frame_count": len(frames),
        "invalid_utf8_replacements": eof.invalid_utf8_replacements,
        "incomplete_frame": eof.incomplete_frame,
        "discarded_frame_count": eof.discarded_frame_count,
    }
    if profile_name is not None:
        observer = IncrementalSSEObserver(
            "openai",
            request_surface="chat_completions",
            wire_surface=profile_name,
        )
        for chunk in chunks:
            observer.observe(chunk)
        observer.finish()
        snapshot = observer.completion_snapshot
        result["observer"] = {
            "error_count": observer.error_count,
            "parser_error_count": snapshot.parser_error_count,
            "saw_payload": snapshot.saw_payload,
            "saw_terminal_event": snapshot.saw_terminal_event,
            "terminal_kind": snapshot.terminal_kind,
        }
    return result


def _sse_utf8_case(
    name: str,
    raw: bytes,
    *,
    profile_name: str | None = None,
    split_mode: str = "one_byte",
) -> dict[str, Any]:
    if split_mode == "every_split_point":
        chunks = [raw]
    else:
        chunks = [raw[index : index + 1] for index in range(len(raw))]
    expected = _sse_utf8_observe(raw, chunks=chunks, profile_name=profile_name)
    return {
        "name": name,
        "input_hex": raw.hex(),
        "feed_modes": ["whole", "one_byte"]
        if split_mode == "one_byte"
        else ["whole", "every_split_point", "one_byte"],
        "expected": expected,
    }


def build_w011_sse_utf8_observations() -> dict[str, Any]:
    """Build bounded Python-oracle cases for W011 UTF-8 EOF finalization."""
    cases = [
        _sse_utf8_case(
            "valid_2_byte_scalar_lf",
            b"data: \xc3\xa9\n\n",
            split_mode="every_split_point",
        ),
        _sse_utf8_case(
            "valid_3_byte_scalar_crlf",
            b"data: \xe4\xb8\x96\r\n\r\n",
            split_mode="every_split_point",
        ),
        _sse_utf8_case(
            "valid_4_byte_scalar_lf",
            b"data: \xf0\x9f\x8c\x8d\n\n",
            split_mode="every_split_point",
        ),
        _sse_utf8_case("eof_incomplete_2_prefix_1", b"data: \xc3"),
        _sse_utf8_case("eof_incomplete_3_prefix_1", b"data: \xe4"),
        _sse_utf8_case("eof_incomplete_3_prefix_2", b"data: \xe4\xb8"),
        _sse_utf8_case("eof_incomplete_4_prefix_1", b"data: \xf0"),
        _sse_utf8_case("eof_incomplete_4_prefix_2", b"data: \xf0\x9f"),
        _sse_utf8_case("eof_incomplete_4_prefix_3", b"data: \xf0\x9f\x8c"),
        _sse_utf8_case("invalid_continuation_after_prefix", b"data: \xc3A\n\n"),
        _sse_utf8_case("invalid_standalone_before_newline", b"data: \xff\n\n"),
        _sse_utf8_case("invalid_standalone_before_eof", b"data: \xff"),
        _sse_utf8_case(
            "invalid_data_line",
            b'data: {"choices":[]}\xff\n\n',
            profile_name="openai_chat_completions",
        ),
        _sse_utf8_case(
            "truncated_data_line_after_json_prefix",
            b'data: {"choices":[]}\xe2',
            profile_name="openai_chat_completions",
        ),
        _sse_utf8_case("invalid_comment_before_newline", b": ignored \xff\n\n"),
        _sse_utf8_case("truncated_comment_at_eof", b": ignored \xe2"),
    ]
    return {
        "schema_version": "m6-canonical-wire-w011-sse-utf8/v1",
        "purpose": (
            "Bounded Python oracle for invalid and truncated UTF-8 SSE finalization."
        ),
        "cases": cases,
    }


def _usage_observations() -> dict[str, Any]:
    cases = {
        "openai_reported_cache": (
            {
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 2,
                    "prompt_tokens_details": {"cached_tokens": 3},
                }
            },
            "openai",
        ),
        "anthropic_reported_cache": (
            {
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 2,
                    "cache_read_input_tokens": 3,
                    "cache_creation_input_tokens": 1,
                }
            },
            "anthropic",
        ),
        "explicit_zero": (
            {
                "usage": {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "prompt_tokens_details": {"cached_tokens": 0},
                }
            },
            "openai",
        ),
        "missing_fields": ({"usage": {"prompt_tokens": 2}}, "openai"),
        "unknown_shape": ({"usage": []}, "openai"),
        "missing_usage": ({}, "openai"),
    }
    result: dict[str, Any] = {}
    for name, (payload, protocol) in cases.items():
        result[name] = _jsonable(normalize_usage(payload, protocol=protocol))
    return result


def build_observation_bundle() -> dict[str, Any]:
    """Build the complete bounded W001 semantic observation."""
    registry = load_wire_registry()
    matrix = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
    profile_inventory = {
        name: {
            "request_codec": definition.request_codec,
            "response_codec": definition.response_codec,
            "stream_codec": definition.stream_codec,
        }
        for name, definition in sorted(registry.profiles.items())
    }
    return {
        "schema_version": "m6-canonical-wire-w001-observations/v1",
        "oracle_modules": [
            "request.parsed_payload",
            "request.body",
            "request.limits",
            "wire.ir",
            "wire.registry",
            "wire.codecs",
            "proxy.sse",
            "proxy.sse_observer",
            "proxy.normalized_usage",
            "transcoder.errors",
        ],
        "public_client_surfaces": list(PUBLIC_SURFACES),
        "wire_profile_inventory": profile_inventory,
        "requests": _request_observations(registry),
        "responses": _response_observations(registry),
        "streams": {
            name: _stream_observation(name, registry) for name in WIRE_PROFILES
        },
        "usage": _usage_observations(),
        "limits": {
            "max_request_body_bytes": MAX_REQUEST_BODY_BYTES,
            "max_sse_frame_bytes": MAX_SSE_FRAME_SIZE,
            "max_estimated_input_tokens": MAX_ESTIMATED_INPUT_TOKENS,
            "empty_input_tokens": estimate_input_tokens(b""),
            "reservation_large_body_tokens": estimate_reservation_tokens(
                b"x" * 500_000
            ),
            "context_rich_request_tokens": estimate_context_input_tokens(
                encode_json_body(RICH_CHAT_REQUEST), RICH_CHAT_REQUEST
            ),
        },
        "loss_reason_codes": sorted(LOSS_WARNING_KINDS),
        "stable_reason_codes": json.loads(MATRIX_PATH.read_text(encoding="utf-8"))[
            "loss_and_error_reason_codes"
        ],
        "m7_boundary": matrix["m7_boundary"],
    }


def observation_json() -> str:
    """Return the canonical compact JSON representation."""
    return json.dumps(build_observation_bundle(), sort_keys=True, separators=(",", ":"))


__all__ = [
    "FIXTURE_DIR",
    "MATRIX_PATH",
    "PUBLIC_SURFACES",
    "WIRE_PROFILES",
    "W011_SSE_UTF8_PATH",
    "build_observation_bundle",
    "build_w011_sse_utf8_observations",
    "observation_json",
]
