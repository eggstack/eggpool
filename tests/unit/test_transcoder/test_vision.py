"""Tests for Phase 6.2 — Vision / image input transcoding."""

from __future__ import annotations

import base64

from eggpool.transcoder.anthropic_to_openai import AnthropicToOpenAI
from eggpool.transcoder.context import TranscodeContext
from eggpool.transcoder.openai_to_anthropic import OpenAIToAnthropic
from eggpool.transcoder.policy import TranscoderFeatures

# A 1x1 red PNG (minimal valid image).
_TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4"
    "nGP4z8BQDwAEgAF/pooBPQAAAABJRU5ErkJggg=="
)
_TINY_PNG_DATA_URI = f"data:image/png;base64,{_TINY_PNG_B64}"
_TINY_PNG_BYTES = base64.b64decode(_TINY_PNG_B64)


def _make_context(
    client: str = "openai",
    upstream: str = "anthropic",
) -> TranscodeContext:
    return TranscodeContext(
        request_id="test-vision",
        client_protocol=client,
        upstream_protocol=upstream,
    )


def _features(**kwargs: bool) -> TranscoderFeatures:
    defaults = {"vision": True}
    defaults.update(kwargs)
    return TranscoderFeatures(**defaults)


# ---------------------------------------------------------------------------
# OpenAI → Anthropic vision
# ---------------------------------------------------------------------------


class TestOpenAIToAnthropicVision:
    def setup_method(self) -> None:
        self.transcoder = OpenAIToAnthropic()

    def test_base64_image_translated(self) -> None:
        payload = {
            "model": "claude-3",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Describe this image"},
                        {
                            "type": "image_url",
                            "image_url": {"url": _TINY_PNG_DATA_URI},
                        },
                    ],
                },
            ],
        }
        result, warnings = self.transcoder.encode_request(
            payload, _make_context(), features=_features()
        )
        msg = result["messages"][0]
        assert isinstance(msg["content"], list)
        assert len(msg["content"]) == 2
        assert msg["content"][0] == {"type": "text", "text": "Describe this image"}
        img = msg["content"][1]
        assert img["type"] == "image"
        assert img["source"]["type"] == "base64"
        assert img["source"]["media_type"] == "image/png"
        assert img["source"]["data"] == _TINY_PNG_DATA_URI

    def test_url_image_translated(self) -> None:
        url = "https://example.com/photo.jpg"
        payload = {
            "model": "claude-3",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "What is this?"},
                        {"type": "image_url", "image_url": {"url": url}},
                    ],
                },
            ],
        }
        result, _ = self.transcoder.encode_request(
            payload, _make_context(), features=_features()
        )
        msg = result["messages"][0]
        img = msg["content"][1]
        assert img["type"] == "image"
        assert img["source"] == {"type": "url", "url": url}

    def test_vision_disabled_drops_image_with_warning(self) -> None:
        payload = {
            "model": "claude-3",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Look"},
                        {
                            "type": "image_url",
                            "image_url": {"url": _TINY_PNG_DATA_URI},
                        },
                    ],
                },
            ],
        }
        result, warnings = self.transcoder.encode_request(
            payload, _make_context(), features=None
        )
        msg = result["messages"][0]
        # Image dropped, only text remains
        assert msg["content"] == "Look"
        image_warnings = [
            w for w in warnings if w.get("field") == "messages[user].content[non-text]"
        ]
        assert len(image_warnings) == 1

    def test_unsupported_image_format(self) -> None:
        payload = {
            "model": "claude-3",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": "ftp://bad.com/img.png"},
                        },
                    ],
                },
            ],
        }
        _, warnings = self.transcoder.encode_request(
            payload, _make_context(), features=_features()
        )
        assert any(w.get("kind") == "image_unsupported_format" for w in warnings)

    def test_input_audio_dropped(self) -> None:
        payload = {
            "model": "claude-3",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_audio", "input_audio": {"data": "abc"}},
                    ],
                },
            ],
        }
        _, warnings = self.transcoder.encode_request(
            payload, _make_context(), features=_features()
        )
        assert any(w.get("kind") == "dropped_field" for w in warnings)

    def test_file_type_dropped(self) -> None:
        payload = {
            "model": "claude-3",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "file",
                            "file": {"file_data": "data:application/pdf;base64,abc"},
                        },
                    ],
                },
            ],
        }
        _, warnings = self.transcoder.encode_request(
            payload, _make_context(), features=_features()
        )
        assert any(w.get("kind") == "dropped_field" for w in warnings)

    def test_multiple_images(self) -> None:
        payload = {
            "model": "claude-3",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": _TINY_PNG_DATA_URI}},
                        {
                            "type": "image_url",
                            "image_url": {"url": "https://example.com/a.jpg"},
                        },
                        {"type": "text", "text": "Compare these"},
                    ],
                },
            ],
        }
        result, _ = self.transcoder.encode_request(
            payload, _make_context(), features=_features()
        )
        content = result["messages"][0]["content"]
        assert len(content) == 3
        assert content[0]["type"] == "image"
        assert content[1]["type"] == "image"
        assert content[2]["type"] == "text"


# ---------------------------------------------------------------------------
# Anthropic → OpenAI vision
# ---------------------------------------------------------------------------


class TestAnthropicToOpenVision:
    def setup_method(self) -> None:
        self.transcoder = AnthropicToOpenAI()

    def test_base64_image_translated(self) -> None:
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
        result, warnings = self.transcoder.encode_request(
            payload, _make_context("anthropic", "openai"), features=_features()
        )
        msg = result["messages"][0]
        assert isinstance(msg["content"], list)
        img = msg["content"][1]
        assert img["type"] == "image_url"
        assert img["image_url"]["url"] == _TINY_PNG_DATA_URI

    def test_url_image_translated(self) -> None:
        url = "https://example.com/photo.jpg"
        payload = {
            "model": "gpt-4",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {"type": "url", "url": url},
                        },
                    ],
                },
            ],
        }
        result, _ = self.transcoder.encode_request(
            payload, _make_context("anthropic", "openai"), features=_features()
        )
        img = result["messages"][0]["content"][0]
        assert img["type"] == "image_url"
        assert img["image_url"]["url"] == url

    def test_pdf_document_translated(self) -> None:
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
                                "data": _TINY_PNG_B64,
                            },
                        },
                    ],
                },
            ],
        }
        result, _ = self.transcoder.encode_request(
            payload, _make_context("anthropic", "openai"), features=_features()
        )
        file_part = result["messages"][0]["content"][0]
        assert file_part["type"] == "file"
        assert file_part["file"]["filename"] == "document.pdf"
        assert file_part["file"]["file_data"].startswith("data:application/pdf;base64,")

    def test_vision_disabled_drops_image(self) -> None:
        payload = {
            "model": "gpt-4",
            "messages": [
                {
                    "role": "user",
                    "content": [
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
        _, warnings = self.transcoder.encode_request(
            payload, _make_context("anthropic", "openai"), features=None
        )
        assert any(w.get("kind") == "non_text_content_dropped" for w in warnings)

    def test_document_url_dropped(self) -> None:
        payload = {
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
        _, warnings = self.transcoder.encode_request(
            payload, _make_context("anthropic", "openai"), features=_features()
        )
        assert any(w.get("kind") == "document_url_dropped" for w in warnings)

    def test_non_pdf_document_dropped(self) -> None:
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
                                "media_type": "text/html",
                                "data": _TINY_PNG_B64,
                            },
                        },
                    ],
                },
            ],
        }
        _, warnings = self.transcoder.encode_request(
            payload, _make_context("anthropic", "openai"), features=_features()
        )
        assert any(w.get("kind") == "document_unsupported_media" for w in warnings)

    def test_text_only_content_preserved(self) -> None:
        payload = {
            "model": "gpt-4",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Hello"},
                        {"type": "text", "text": "World"},
                    ],
                },
            ],
        }
        result, _ = self.transcoder.encode_request(
            payload, _make_context("anthropic", "openai"), features=_features()
        )
        assert result["messages"][0]["content"] == "Hello\nWorld"

    def test_pdf_too_large_emits_warning(self) -> None:
        """pdf_too_large is emitted when a base64 PDF exceeds 32 MB."""

        # Create a base64 string that decodes to > 32 MB
        large_data = b"\x00" * (32 * 1024 * 1024 + 1)
        large_b64 = base64.b64encode(large_data).decode()
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
                                "data": large_b64,
                            },
                        },
                    ],
                },
            ],
        }
        _, warnings = self.transcoder.encode_request(
            payload, _make_context("anthropic", "openai"), features=_features()
        )
        assert any(w.get("kind") == "pdf_too_large" for w in warnings)
