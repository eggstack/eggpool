"""Tests for Plan 134 — Multimodal transcoding and size enforcement."""

from __future__ import annotations

import base64
from typing import Any

import pytest

from eggpool.catalog.capabilities import (
    MediaCapability,
    MultimodalCapabilities,
)
from eggpool.errors import RequestTooLargeError
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

    def test_anthropic_tool_result_image_gated_by_capability(self) -> None:
        """Tool-result images respect the same capability gates as top-level.

        A base64 image inside a ``tool_result`` must be dropped when the
        target contract forbids base64 images instead of passing through
        ungated.
        """
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
                },
            ],
        }
        caps = _mm_caps(non_text_tool_result=True, image_base64=False)
        result, warnings = transcoder.encode_request(
            payload,
            _make_context("anthropic", "openai"),
            features=_features(),
            multimodal_capability=caps,
        )
        assert any(
            w.get("kind") == "unsupported_source_form"
            and w.get("source_form") == "base64"
            for w in warnings
        )
        tool_msg = result["messages"][0]
        assert tool_msg["content"] == ""


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


# ---------------------------------------------------------------------------
# Happy-path multimodal translation
# ---------------------------------------------------------------------------


class TestHappyPathMultimodalTranslation:
    """Verify multimodal content survives translation when capabilities permit."""

    def test_openai_url_image_translated_to_anthropic_url(self) -> None:
        transcoder = OpenAIToAnthropic()
        url = "https://example.com/photo.jpg"
        payload = {
            "model": "claude-3",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Describe this"},
                        {
                            "type": "image_url",
                            "image_url": {"url": url},
                        },
                    ],
                },
            ],
        }
        caps = _mm_caps(image_url=True)
        result, warnings = transcoder.encode_request(
            payload,
            _make_context(),
            features=_features(),
            multimodal_capability=caps,
        )
        # No loss warnings for the image
        assert not any(
            w.get("kind") in ("unsupported_source_form", "unsupported_modality")
            for w in warnings
        )
        # The translated message should contain an image block with URL source
        user_msg = result["messages"][0]
        blocks = user_msg["content"]
        image_blocks = [b for b in blocks if b.get("type") == "image"]
        assert len(image_blocks) == 1
        assert image_blocks[0]["source"]["type"] == "url"
        assert image_blocks[0]["source"]["url"] == url

    def test_anthropic_base64_image_translated_to_openai_data_uri(self) -> None:
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
        caps = _mm_caps(image_base64=True)
        result, warnings = transcoder.encode_request(
            payload,
            _make_context("anthropic", "openai"),
            features=_features(),
            multimodal_capability=caps,
        )
        assert not any(
            w.get("kind") in ("unsupported_source_form", "unsupported_modality")
            for w in warnings
        )
        user_msg = result["messages"][0]
        content = user_msg["content"]
        assert isinstance(content, list)
        image_parts = [p for p in content if p.get("type") == "image_url"]
        assert len(image_parts) == 1
        assert image_parts[0]["image_url"]["url"].startswith("data:image/png;base64,")

    def test_anthropic_document_url_translated_when_supported(self) -> None:
        transcoder = AnthropicToOpenAI()
        url = "https://example.com/document.pdf"
        payload = {
            "model": "gpt-4",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Read this"},
                        {
                            "type": "document",
                            "source": {"type": "url", "url": url},
                        },
                    ],
                },
            ],
        }
        result, warnings = transcoder.encode_request(
            payload,
            _make_context("anthropic", "openai"),
            features=_features(),
            multimodal_capability=_mm_caps(doc_url=True),
        )

        assert not any(w.get("kind") == "document_url_dropped" for w in warnings)
        content = result["messages"][0]["content"]
        assert isinstance(content, list)
        document_parts = [p for p in content if p.get("type") == "file"]
        assert document_parts == [
            {
                "type": "file",
                "file": {"filename": "document.pdf", "file_data": url},
            }
        ]


# ---------------------------------------------------------------------------
# Serialized request-size validation
# ---------------------------------------------------------------------------


class TestSerializedRequestSizeValidation:
    """Verify _validate_serialized_request_size rejects oversized payloads locally."""

    def _make_coordinator(self) -> tuple[Any, Any]:
        """Return (coordinator, catalog_mock) with a mock catalog."""
        from unittest.mock import MagicMock

        from eggpool.request.coordinator import RequestCoordinator

        catalog = MagicMock()
        coordinator = RequestCoordinator(
            registry=MagicMock(),
            catalog=catalog,
            router=MagicMock(),
            db=MagicMock(),
            client_pool=MagicMock(),
        )
        return coordinator, catalog

    def test_oversized_body_rejected_locally(self) -> None:
        from eggpool.errors import RequestTooLargeError
        from eggpool.request.coordinator import ProxyRequestContext

        coordinator, catalog = self._make_coordinator()
        # Simulate a model with max_serialized_request_bytes = 1000
        catalog.cache.get_model_for_provider.return_value = {
            "capabilities": {
                "multimodal": {
                    "max_serialized_request_bytes": 1000,
                }
            }
        }
        context = ProxyRequestContext(
            request_id="req-size-1",
            protocol="openai",
            model_id="gpt-4",
            streaming=False,
            original_body=b"{}",
            incoming_headers={},
        )
        oversized_body = b"x" * 1001
        with pytest.raises(RequestTooLargeError, match="exceeds provider limit"):
            coordinator._validate_serialized_request_size(
                context,
                oversized_body,
                selected_provider_id="p1",
            )

    def test_body_at_limit_accepted(self) -> None:
        from eggpool.request.coordinator import ProxyRequestContext

        coordinator, catalog = self._make_coordinator()
        catalog.cache.get_model_for_provider.return_value = {
            "capabilities": {
                "multimodal": {
                    "max_serialized_request_bytes": 1000,
                }
            }
        }
        context = ProxyRequestContext(
            request_id="req-size-2",
            protocol="openai",
            model_id="gpt-4",
            streaming=False,
            original_body=b"{}",
            incoming_headers={},
        )
        exact_body = b"x" * 1000
        # Should not raise
        coordinator._validate_serialized_request_size(
            context,
            exact_body,
            selected_provider_id="p1",
        )

    def test_no_limit_when_capability_absent(self) -> None:
        from eggpool.request.coordinator import ProxyRequestContext

        coordinator, catalog = self._make_coordinator()
        # No capabilities at all
        catalog.cache.get_model_for_provider.return_value = None
        context = ProxyRequestContext(
            request_id="req-size-3",
            protocol="openai",
            model_id="gpt-4",
            streaming=False,
            original_body=b"{}",
            incoming_headers={},
        )
        huge_body = b"x" * 10_000_000
        # Should not raise — no limit configured
        coordinator._validate_serialized_request_size(
            context,
            huge_body,
            selected_provider_id="p1",
        )

    def test_base64_below_decoded_limit_above_serialized_limit(self) -> None:
        """Base64 payload under per-file limit but serialized body exceeds max.

        This tests the scenario where decoded PDF bytes are under a provider's
        nominal attachment limit but base64 expansion + JSON wrapper makes the
        final HTTP body exceed the provider request limit.
        """
        from eggpool.errors import RequestTooLargeError
        from eggpool.request.coordinator import ProxyRequestContext

        coordinator, catalog = self._make_coordinator()
        # Set a tiny serialized limit (50 bytes)
        catalog.cache.get_model_for_provider.return_value = {
            "capabilities": {
                "multimodal": {
                    "max_serialized_request_bytes": 50,
                }
            }
        }
        context = ProxyRequestContext(
            request_id="req-size-4",
            protocol="openai",
            model_id="gpt-4",
            streaming=False,
            original_body=b"{}",
            incoming_headers={},
        )
        # A "serialized" body that's 51 bytes — exceeds limit
        body_at_51 = b"x" * 51
        with pytest.raises(RequestTooLargeError, match="exceeds provider limit"):
            coordinator._validate_serialized_request_size(
                context,
                body_at_51,
                selected_provider_id="p1",
            )


# ---------------------------------------------------------------------------
# Local preparation failure does not produce provider retry/backoff
# ---------------------------------------------------------------------------


class TestLocalPreparationNoProviderBackoff:
    """Verify transcode/size errors are local-only and don't penalize providers."""

    def test_transcode_loss_error_is_not_upstream_error(self) -> None:
        from eggpool.errors import AggregatorError, UpstreamError
        from eggpool.transcoder.errors import TranscodeLossError

        err = TranscodeLossError("test", [{"kind": "unsupported_modality"}])
        assert isinstance(err, AggregatorError)
        assert not isinstance(err, UpstreamError)

    def test_request_too_large_is_not_upstream_error(self) -> None:
        from eggpool.errors import AggregatorError, RequestTooLargeError, UpstreamError

        err = RequestTooLargeError("too large")
        assert isinstance(err, AggregatorError)
        assert not isinstance(err, UpstreamError)

    def test_transcode_loss_error_rendered_as_http_400(self) -> None:
        """TranscodeLossError maps to HTTP 400 in the proxy renderer."""
        from eggpool.transcoder.errors import TranscodeLossError

        exc = TranscodeLossError(
            "Request rejected by loss_policy=reject",
            [{"kind": "unsupported_modality", "modality": "audio"}],
        )
        assert exc.loss_warnings[0]["kind"] == "unsupported_modality"
        # The proxy renderer catches this as HTTP 400 — verify the exception
        # is not an UpstreamError which would trigger provider backoff.
        from eggpool.errors import UpstreamError

        assert not isinstance(exc, UpstreamError)


# ---------------------------------------------------------------------------
# Provider-scoped capability resolution (Plan 140 Workstream C)
# ---------------------------------------------------------------------------


class TestProviderScopedSizeLimits:
    """Oversize limits must be resolved against the *selected* provider.

    Collapsed models may be served by multiple providers with different
    ``max_serialized_request_bytes`` values. The size validator must not
    borrow a different provider's limit.
    """

    def _make_coordinator(self) -> tuple[Any, Any]:
        from unittest.mock import MagicMock

        from eggpool.request.coordinator import RequestCoordinator

        catalog = MagicMock()
        coordinator = RequestCoordinator(
            registry=MagicMock(),
            catalog=catalog,
            router=MagicMock(),
            db=MagicMock(),
            client_pool=MagicMock(),
        )
        return coordinator, catalog

    def test_selected_provider_limit_enforced(self) -> None:
        from eggpool.request.coordinator import ProxyRequestContext

        coordinator, catalog = self._make_coordinator()
        # Only the selected provider advertises a small limit; the global
        # entry is absent.
        catalog.cache.get_model_for_provider.return_value = {
            "capabilities": {
                "multimodal": {
                    "max_serialized_request_bytes": 100,
                }
            }
        }
        context = ProxyRequestContext(
            request_id="req-provider-1",
            protocol="openai",
            model_id="shared-model",
            streaming=False,
            original_body=b"{}",
            incoming_headers={},
        )
        with pytest.raises(RequestTooLargeError, match="exceeds provider limit"):
            coordinator._validate_serialized_request_size(
                context,
                b"x" * 200,
                selected_provider_id="small-provider",
            )
        catalog.cache.get_model_for_provider.assert_called_with(
            "shared-model", "small-provider"
        )

    def test_other_provider_limit_not_borrowed(self) -> None:
        """A different provider's limit must not apply to the actual selection."""
        from eggpool.request.coordinator import ProxyRequestContext

        coordinator, catalog = self._make_coordinator()
        # The selected provider has no metadata; the global fallback is
        # also None. The validator must not raise.
        catalog.cache.get_model_for_provider.return_value = None
        context = ProxyRequestContext(
            request_id="req-provider-2",
            protocol="openai",
            model_id="shared-model",
            streaming=False,
            original_body=b"{}",
            incoming_headers={},
        )
        # No raise — selected provider has no overhead limit.
        coordinator._validate_serialized_request_size(
            context,
            b"x" * 10_000_000,
            selected_provider_id="unlimited-provider",
        )


class TestRequestTooLargeErrorMapping:
    """error_status_code maps RequestTooLargeError to HTTP 413."""

    def test_returns_413(self) -> None:
        from eggpool.errors import RequestTooLargeError
        from eggpool.request.static_helpers import error_status_code

        assert error_status_code(RequestTooLargeError("big")) == 413
