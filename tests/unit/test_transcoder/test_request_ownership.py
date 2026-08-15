"""Read-only request ownership contracts for body transcoding."""

from __future__ import annotations

from copy import deepcopy
from types import MappingProxyType
from typing import Any

import pytest

import eggpool.transcoder.anthropic_to_openai as anthropic_to_openai_module
import eggpool.transcoder.openai_to_anthropic as openai_to_anthropic_module
from eggpool.transcoder.anthropic_to_openai import AnthropicToOpenAI
from eggpool.transcoder.context import TranscodeContext
from eggpool.transcoder.openai_to_anthropic import OpenAIToAnthropic
from eggpool.transcoder.policy import TranscoderFeatures


def _openai_context() -> TranscodeContext:
    return TranscodeContext(
        request_id="ownership-openai",
        client_protocol="openai",
        upstream_protocol="anthropic",
    )


def _anthropic_context() -> TranscodeContext:
    return TranscodeContext(
        request_id="ownership-anthropic",
        client_protocol="anthropic",
        upstream_protocol="openai",
    )


def test_openai_request_transcode_reads_mapping_without_mutating_source() -> None:
    payload: dict[str, Any] = {
        "model": "gpt-4",
        "messages": [
            {"role": "user", "content": "large history placeholder"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "lookup",
                            "arguments": '{"query":"eggpool"}',
                        },
                    }
                ],
            },
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                    },
                },
            }
        ],
        "max_tokens": 256,
    }
    source_before = deepcopy(payload)

    translated, _ = OpenAIToAnthropic().encode_request(
        MappingProxyType(payload),
        _openai_context(),
    )

    assert payload == source_before
    assert translated is not payload
    assert translated["messages"] is not payload["messages"]
    assert translated["tools"] is not payload["tools"]


def test_anthropic_request_transcode_reads_mapping_without_mutating_source() -> None:
    payload: dict[str, Any] = {
        "model": "claude-3",
        "system": "Be concise.",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "large history placeholder"},
                    {
                        "type": "tool_use",
                        "id": "toolu-1",
                        "name": "lookup",
                        "input": {"query": "eggpool"},
                    },
                ],
            }
        ],
        "tools": [
            {
                "name": "lookup",
                "description": "Look up a value.",
                "input_schema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                },
            }
        ],
        "max_tokens": 256,
    }
    source_before = deepcopy(payload)

    translated, _ = AnthropicToOpenAI().encode_request(
        MappingProxyType(payload),
        _anthropic_context(),
    )

    assert payload == source_before
    assert translated is not payload
    assert translated["messages"] is not payload["messages"]
    assert translated["tools"] is not payload["tools"]


def test_obviously_oversized_openai_image_skips_strict_decode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(openai_to_anthropic_module, "_ANTHROPIC_IMAGE_SIZE_LIMIT", 3)

    def fail_decode(_: str) -> bytes:
        pytest.fail("obviously oversized base64 should be rejected before decode")

    monkeypatch.setattr(
        openai_to_anthropic_module,
        "decode_base64_payload",
        fail_decode,
    )
    payload = {
        "model": "gpt-4",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64,AAAAAAAA",
                        },
                    }
                ],
            }
        ],
    }

    _, warnings = OpenAIToAnthropic().encode_request(
        payload,
        _openai_context(),
        features=TranscoderFeatures(vision=True),
    )

    assert any(warning.get("kind") == "image_too_large" for warning in warnings)


def test_obviously_oversized_anthropic_pdf_skips_strict_decode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(anthropic_to_openai_module, "_ANTHROPIC_PDF_SIZE_LIMIT", 3)

    def fail_decode(_: str) -> bytes:
        pytest.fail("obviously oversized base64 should be rejected before decode")

    monkeypatch.setattr(
        anthropic_to_openai_module,
        "decode_base64_payload",
        fail_decode,
    )
    payload = {
        "model": "gpt-4",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": "AAAAAAAA",
                        },
                    }
                ],
            }
        ],
    }

    _, warnings = AnthropicToOpenAI().encode_request(
        payload,
        _anthropic_context(),
        features=TranscoderFeatures(vision=True),
    )

    assert any(warning.get("kind") == "pdf_too_large" for warning in warnings)
