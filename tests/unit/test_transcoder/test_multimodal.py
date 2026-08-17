"""Tests for Plan 134 — Multimodal transcoding and size enforcement."""

from __future__ import annotations

import base64

import pytest

from eggpool.catalog.capabilities import (
    MediaCapability,
    MultimodalCapabilities,
)
from eggpool.transcoder.anthropic_to_openai import AnthropicToOpenAI
from eggpool.transcoder.context import TranscodeContext
from eggpool.transcoder.errors import TranscodeLossError
from eggpool.transcoder.openai_to_anthropic import OpenAIToAnthropic
from eggpool.transcoder.policy import TranscoderFeatures

# A 1x1 red PNG (minimal valid image).
_TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4"
    "nGP4z8BQDwAEgAF/pooBPQAAAABJRU5ErkJggg=="
)
_TINY_PNG_DATA_URI = f"data:image/png;base64,{_TINY_PNG_B64}"

# A minimal valid PDF header for testing.
_TINY_PDF_B64 = base64.b64encode(b"%PDF-1.4 fake content for testing").decode()


def _make_context(
    client: str = "openai",
    upstream: str = "anthropic",
) -> TranscodeContext:
    return TranscodeContext(
        request_id="test-multimodal",
        client_protocol=client,
        upstream_protocol=upstream,
    )


def _features(**kwargs: bool) -> TranscoderFeatures:
    defaults = {"vision": True}
    defaults.update(kwargs)
    return TranscoderFeatures(**defaults)


def _mm_caps(
    *,
    image_base64: bool = True,
    image_url: bool = True,
    doc_base64: bool = True,
    doc_url: bool = False,
    non_text_tool_result: bool = False,
    max_serialized_request_bytes: int | None = None,
) -> MultimodalCapabilities:
    return MultimodalCapabilities(
        image_input=MediaCapability(base64=image_base64, url=image_url),
        document_input=MediaCapability(base64=doc_base64, url=doc_url),
        non_text_tool_result=non_text_tool_result,
        max_serialized_request_bytes=max_serialized_request_bytes,
    )


# ---------------------------------------------------------------------------
# Multimodal loss-policy enforcement
# ---------------------------------------------------------------------------


class TestMultimodalLossPolicy:
    """Verify that MULTIMODAL_LOSS_KINDS trigger TranscodeLossError in reject mode."""

    def test_unsupported_modality_rejects_openai_to_anthropic(self) -> None:
        transcoder = OpenAIToAnthropic()
        payload = {
            "model": "claude-3",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {"data": "abc"},
                        },
                    ],
                },
            ],
        }
        with pytest.raises(TranscodeLossError, match="protected boundary"):
            transcoder.encode_request(
                payload,
                _make_context(),
                features=_features(),
                loss_policy="reject",
            )

    def test_unsupported_modality_rejects_anthropic_to_openai(self) -> None:
        transcoder = AnthropicToOpenAI()
        payload = {
            "model": "gpt-4",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "audio",
                            "source": {
                                "type": "url",
                                "url": "https://example.com/audio.mp3",
                            },
                        },
                    ],
                },
            ],
        }
        with pytest.raises(TranscodeLossError, match="protected boundary"):
            transcoder.encode_request(
                payload,
                _make_context("anthropic", "openai"),
                features=_features(),
                loss_policy="reject",
            )

    def test_unsupported_modality_warns_in_warn_mode(self) -> None:
        transcoder = OpenAIToAnthropic()
        payload = {
            "model": "claude-3",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {"data": "abc"},
                        },
                    ],
                },
            ],
        }
        _, warnings = transcoder.encode_request(
            payload,
            _make_context(),
            features=_features(),
            loss_policy="warn",
        )
        assert any(w.get("kind") == "unsupported_modality" for w in warnings)

    def test_document_media_type_unsupported_rejects(self) -> None:
        transcoder = AnthropicToOpenAI()
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
                                "media_type": "image/png",
                                "data": _TINY_PNG_B64,
                            },
                        },
                    ],
                },
            ],
        }
        with pytest.raises(TranscodeLossError, match="protected boundary"):
            transcoder.encode_request(
                payload,
                _make_context("anthropic", "openai"),
                features=_features(),
                loss_policy="reject",
            )


# ---------------------------------------------------------------------------
# Capability-aware source form gating
# ---------------------------------------------------------------------------


class TestCapabilityAwareSourceFormGating:
    """Verify source form gating against MultimodalCapabilities."""

    def test_base64_image_gated_by_capability_openai_to_anthropic(self) -> None:
        transcoder = OpenAIToAnthropic()
        payload = {
            "model": "claude-3",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Describe this"},
                        {
                            "type": "image_url",
                            "image_url": {"url": _TINY_PNG_DATA_URI},
                        },
                    ],
                },
            ],
        }
        caps = _mm_caps(image_base64=False)
        _, warnings = transcoder.encode_request(
            payload,
            _make_context(),
            features=_features(),
            multimodal_capability=caps,
        )
        assert any(w.get("kind") == "unsupported_source_form" for w in warnings)

    def test_url_image_gated_by_capability_openai_to_anthropic(self) -> None:
        transcoder = OpenAIToAnthropic()
        payload = {
            "model": "claude-3",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Describe this"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "https://example.com/photo.jpg"},
                        },
                    ],
                },
            ],
        }
        caps = _mm_caps(image_url=False)
        _, warnings = transcoder.encode_request(
            payload,
            _make_context(),
            features=_features(),
            multimodal_capability=caps,
        )
        assert any(w.get("kind") == "unsupported_source_form" for w in warnings)

    def test_base64_image_gated_by_capability_anthropic_to_openai(self) -> None:
        transcoder = AnthropicToOpenAI()
        payload = {
            "model": "gpt-4",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Describe this"},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": _TINY_PNG_B64,
                            },
                        },
                    ],
                },
            ],
        }
        caps = _mm_caps(image_base64=False)
        _, warnings = transcoder.encode_request(
            payload,
            _make_context("anthropic", "openai"),
            features=_features(),
            multimodal_capability=caps,
        )
        assert any(w.get("kind") == "unsupported_source_form" for w in warnings)

    def test_url_image_gated_by_capability_anthropic_to_openai(self) -> None:
        transcoder = AnthropicToOpenAI()
        payload = {
            "model": "gpt-4",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Describe this"},
                        {
                            "type": "image",
                            "source": {
                                "type": "url",
                                "url": "https://example.com/photo.jpg",
                            },
                        },
                    ],
                },
            ],
        }
        caps = _mm_caps(image_url=False)
        _, warnings = transcoder.encode_request(
            payload,
            _make_context("anthropic", "openai"),
            features=_features(),
            multimodal_capability=caps,
        )
        assert any(w.get("kind") == "unsupported_source_form" for w in warnings)

    def test_base64_document_gated_by_capability_anthropic_to_openai(self) -> None:
        transcoder = AnthropicToOpenAI()
        payload = {
            "model": "gpt-4",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Read this"},
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": _TINY_PDF_B64,
                            },
                        },
                    ],
                },
            ],
        }
        caps = _mm_caps(doc_base64=False)
        _, warnings = transcoder.encode_request(
            payload,
            _make_context("anthropic", "openai"),
            features=_features(),
            multimodal_capability=caps,
        )
        assert any(w.get("kind") == "unsupported_source_form" for w in warnings)


# ---------------------------------------------------------------------------
# Tool-result media preservation
# ---------------------------------------------------------------------------


class TestToolResultMediaPreservation:
    """Verify media-bearing tool results are preserved when target supports them."""

    def test_image_tool_result_preserved_when_supported(self) -> None:
        transcoder = OpenAIToAnthropic()
        payload = {
            "model": "claude-3",
            "messages": [
                {
                    "role": "tool",
                    "tool_call_id": "call_123",
                    "content": [
                        {"type": "text", "text": "Here is the image:"},
                        {
                            "type": "image_url",
                            "image_url": {"url": _TINY_PNG_DATA_URI},
                        },
                    ],
                },
            ],
        }
        caps = _mm_caps(non_text_tool_result=True)
        result, warnings = transcoder.encode_request(
            payload,
            _make_context(),
            features=_features(),
            multimodal_capability=caps,
        )
        # Should NOT have media_tool_result_flattened warning
        assert not any(w.get("kind") == "media_tool_result_flattened" for w in warnings)
        # The tool result should contain media blocks
        user_msg = result["messages"][0]
        assert user_msg["role"] == "user"
        tool_result = user_msg["content"][0]
        assert tool_result["type"] == "tool_result"
        assert isinstance(tool_result["content"], list)
        # Should have text + image
        assert len(tool_result["content"]) == 2

    def test_image_tool_result_flattened_when_not_supported(self) -> None:
        transcoder = OpenAIToAnthropic()
        payload = {
            "model": "claude-3",
            "messages": [
                {
                    "role": "tool",
                    "tool_call_id": "call_123",
                    "content": [
                        {"type": "text", "text": "Here is the image:"},
                        {
                            "type": "image_url",
                            "image_url": {"url": _TINY_PNG_DATA_URI},
                        },
                    ],
                },
            ],
        }
        caps = _mm_caps(non_text_tool_result=False)
        _, warnings = transcoder.encode_request(
            payload,
            _make_context(),
            features=_features(),
            multimodal_capability=caps,
        )
        assert any(w.get("kind") == "media_tool_result_flattened" for w in warnings)

    def test_anthropic_image_tool_result_preserved_when_supported(self) -> None:
        transcoder = AnthropicToOpenAI()
        payload = {
            "model": "gpt-4",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_123",
                            "content": [
                                {"type": "text", "text": "Here is the result:"},
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "url",
                                        "url": "https://example.com/photo.jpg",
                                    },
                                },
                            ],
                        },
                    ],
                },
            ],
        }
        caps = _mm_caps(non_text_tool_result=True)
        _, warnings = transcoder.encode_request(
            payload,
            _make_context("anthropic", "openai"),
            features=_features(),
            multimodal_capability=caps,
        )
        assert not any(w.get("kind") == "media_tool_result_flattened" for w in warnings)

    def test_anthropic_image_tool_result_flattened_when_not_supported(self) -> None:
        transcoder = AnthropicToOpenAI()
        payload = {
            "model": "gpt-4",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_123",
                            "content": [
                                {"type": "text", "text": "Here is the result:"},
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "url",
                                        "url": "https://example.com/photo.jpg",
                                    },
                                },
                            ],
                        },
                    ],
                },
            ],
        }
        caps = _mm_caps(non_text_tool_result=False)
        _, warnings = transcoder.encode_request(
            payload,
            _make_context("anthropic", "openai"),
            features=_features(),
            multimodal_capability=caps,
        )
        assert any(w.get("kind") == "media_tool_result_flattened" for w in warnings)


# ---------------------------------------------------------------------------
# Same-protocol passthrough
# ---------------------------------------------------------------------------


class TestSameProtocolPassthrough:
    """Same-protocol multimodal passthrough remains untouched.

    When client and upstream protocols match, no transcoder is invoked.
    These tests verify the transcoder factory returns None for same-protocol
    pairs, confirming passthrough behavior.
    """

    def test_select_transcoder_returns_none_for_same_protocol(self) -> None:
        from eggpool.transcoder.protocol import select_transcoder

        assert (
            select_transcoder(client_protocol="openai", upstream_protocol="openai")
            is None
        )
        assert (
            select_transcoder(
                client_protocol="anthropic", upstream_protocol="anthropic"
            )
            is None
        )
