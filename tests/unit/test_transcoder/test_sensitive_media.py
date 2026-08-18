"""Tests for the provider-sensitive media detector."""

from __future__ import annotations

from eggpool.transcoder.sensitive_media import (
    request_has_provider_sensitive_media,
)


class TestTextOnly:
    def test_empty_payload(self) -> None:
        assert request_has_provider_sensitive_media({}) is False

    def test_text_only_openai(self) -> None:
        payload = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "hello"}],
        }
        assert request_has_provider_sensitive_media(payload) is False

    def test_text_only_anthropic(self) -> None:
        payload = {
            "model": "claude-3",
            "messages": [{"role": "user", "content": "hello"}],
            "system": "you are helpful",
        }
        assert request_has_provider_sensitive_media(payload) is False


class TestImageContent:
    def test_openai_image_url_part(self) -> None:
        payload = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "what is this?"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "https://example.com/img.png"},
                        },
                    ],
                }
            ]
        }
        assert request_has_provider_sensitive_media(payload) is True

    def test_anthropic_image_source(self) -> None:
        payload = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "what is this?"},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": "AAAA",
                            },
                        },
                    ],
                }
            ]
        }
        assert request_has_provider_sensitive_media(payload) is True

    def test_anthropic_document_source(self) -> None:
        payload = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": "AAAA",
                            },
                        },
                    ],
                }
            ]
        }
        assert request_has_provider_sensitive_media(payload) is True


class TestAudioContent:
    def test_openai_input_audio(self) -> None:
        payload = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {
                                "data": "AAAA",
                                "format": "wav",
                            },
                        }
                    ],
                }
            ]
        }
        assert request_has_provider_sensitive_media(payload) is True


class TestToolResultMedia:
    def test_tool_result_with_image_part(self) -> None:
        payload = {
            "messages": [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "img", "arguments": "{}"},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_1",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": "https://example.com/img.png"},
                        }
                    ],
                },
            ]
        }
        assert request_has_provider_sensitive_media(payload) is True

    def test_tool_result_with_text_only(self) -> None:
        payload = {
            "messages": [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "get", "arguments": "{}"},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_1",
                    "content": "just a string",
                },
            ]
        }
        assert request_has_provider_sensitive_media(payload) is False


class TestNonMapping:
    def test_none_payload(self) -> None:
        assert request_has_provider_sensitive_media(None) is False

    def test_list_payload(self) -> None:
        assert request_has_provider_sensitive_media(["messages"]) is False
