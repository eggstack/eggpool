"""Integration tests for vision / image input transcoding.

Tests end-to-end encode→decode pipelines for vision content in both
directions, including base64 images, URLs, documents, and size limits.
"""

from __future__ import annotations

from eggpool.transcoder.anthropic_to_openai import AnthropicToOpenAI
from eggpool.transcoder.context import TranscodeContext
from eggpool.transcoder.openai_to_anthropic import OpenAIToAnthropic
from eggpool.transcoder.policy import TranscoderFeatures

_TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4"
    "nGP4z8BQDwAEgAF/pooBPQAAAABJRU5ErkJggg=="
)
_TINY_PNG_DATA_URI = f"data:image/png;base64,{_TINY_PNG_B64}"


def _features(**kwargs: bool) -> TranscoderFeatures:
    defaults = {"vision": True}
    defaults.update(kwargs)
    return TranscoderFeatures(**defaults)


# ---------------------------------------------------------------------------
# OpenAI → Anthropic vision (non-streaming round-trip)
# ---------------------------------------------------------------------------


class TestVisionOpenAIToAnthropicRoundTrip:
    def test_request_with_image_then_text_response(self) -> None:
        """OpenAI request with image_url encodes to Anthropic image, then
        a text-only response decodes back to OpenAI."""
        ctx = TranscodeContext(
            request_id="integ-vision-1",
            client_protocol="openai",
            upstream_protocol="anthropic",
        )
        transcoder = OpenAIToAnthropic()

        request = {
            "model": "claude-3",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Describe this"},
                        {"type": "image_url", "image_url": {"url": _TINY_PNG_DATA_URI}},
                    ],
                },
            ],
        }
        upstream, warnings = transcoder.encode_request(
            request, ctx, features=_features()
        )
        msg = upstream["messages"][0]
        assert msg["content"][0] == {"type": "text", "text": "Describe this"}
        assert msg["content"][1]["type"] == "image"
        assert msg["content"][1]["source"]["type"] == "base64"

        # Decode a text response
        response = {
            "id": "msg-abc",
            "content": [{"type": "text", "text": "I see a small red square."}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 100, "output_tokens": 10},
        }
        client_response, _ = transcoder.decode_response(response, ctx)
        assert (
            client_response["choices"][0]["message"]["content"]
            == "I see a small red square."
        )

    def test_url_image_round_trip(self) -> None:
        ctx = TranscodeContext(
            request_id="integ-vision-2",
            client_protocol="openai",
            upstream_protocol="anthropic",
        )
        transcoder = OpenAIToAnthropic()

        url = "https://example.com/photo.jpg"
        request = {
            "model": "claude-3",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": url}},
                        {"type": "text", "text": "What is this?"},
                    ],
                },
            ],
        }
        upstream, _ = transcoder.encode_request(request, ctx, features=_features())
        assert upstream["messages"][0]["content"][0] == {
            "type": "image",
            "source": {"type": "url", "url": url},
        }

    def test_disabled_vision_preserves_v1_behaviour(self) -> None:
        ctx = TranscodeContext(
            request_id="integ-vision-3",
            client_protocol="openai",
            upstream_protocol="anthropic",
        )
        transcoder = OpenAIToAnthropic()

        request = {
            "model": "claude-3",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Look"},
                        {"type": "image_url", "image_url": {"url": _TINY_PNG_DATA_URI}},
                    ],
                },
            ],
        }
        result, warnings = transcoder.encode_request(request, ctx, features=None)
        # Image dropped, only text
        assert result["messages"][0]["content"] == "Look"
        assert any(
            w.get("field") == "messages[user].content[non-text]" for w in warnings
        )


# ---------------------------------------------------------------------------
# Anthropic → OpenAI vision (non-streaming round-trip)
# ---------------------------------------------------------------------------


class TestVisionAnthropicToOpenAIRoundTrip:
    def test_request_with_image_then_text_response(self) -> None:
        """Anthropic request with image encodes to OpenAI image_url, then
        a text response decodes back."""
        ctx = TranscodeContext(
            request_id="integ-vision-4",
            client_protocol="anthropic",
            upstream_protocol="openai",
        )
        transcoder = AnthropicToOpenAI()

        request = {
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
        upstream, _ = transcoder.encode_request(request, ctx, features=_features())
        img = upstream["messages"][0]["content"][1]
        assert img["type"] == "image_url"
        assert img["image_url"]["url"] == _TINY_PNG_DATA_URI

        response = {
            "id": "chatcmpl-123",
            "object": "chat.completion",
            "model": "gpt-4",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "A tiny red square."},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 50, "completion_tokens": 5, "total_tokens": 55},
        }
        client_response, _ = transcoder.decode_response(response, ctx)
        assert client_response["content"] == [
            {"type": "text", "text": "A tiny red square."}
        ]

    def test_pdf_document_round_trip(self) -> None:
        ctx = TranscodeContext(
            request_id="integ-vision-5",
            client_protocol="anthropic",
            upstream_protocol="openai",
        )
        transcoder = AnthropicToOpenAI()

        request = {
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
                                "data": _TINY_PNG_B64,
                            },
                        },
                    ],
                },
            ],
        }
        upstream, _ = transcoder.encode_request(request, ctx, features=_features())
        file_part = upstream["messages"][0]["content"][0]
        assert file_part["type"] == "file"
        assert file_part["file"]["filename"] == "document.pdf"
        assert file_part["file"]["file_data"].startswith("data:application/pdf;base64,")

    def test_document_url_dropped(self) -> None:
        ctx = TranscodeContext(
            request_id="integ-vision-6",
            client_protocol="anthropic",
            upstream_protocol="openai",
        )
        transcoder = AnthropicToOpenAI()

        request = {
            "model": "gpt-4",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "url",
                                "url": "https://example.com/doc.pdf",
                            },
                        },
                    ],
                },
            ],
        }
        _, warnings = transcoder.encode_request(request, ctx, features=_features())
        assert any(w.get("kind") == "document_url_dropped" for w in warnings)

    def test_non_pdf_document_dropped(self) -> None:
        ctx = TranscodeContext(
            request_id="integ-vision-7",
            client_protocol="anthropic",
            upstream_protocol="openai",
        )
        transcoder = AnthropicToOpenAI()

        request = {
            "model": "gpt-4",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "text/html",
                                "data": _TINY_PNG_B64,
                            },
                        },
                    ],
                },
            ],
        }
        _, warnings = transcoder.encode_request(request, ctx, features=_features())
        assert any(w.get("kind") == "document_unsupported_media" for w in warnings)
