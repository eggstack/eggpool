"""Tests for the narrow content-block IR types."""

from __future__ import annotations

import contextlib

from eggpool.transcoder.content import (
    AudioContent,
    ContentBlock,
    DocumentContent,
    ImageContent,
    RedactedThinkingContent,
    TextContent,
    ThinkingContent,
    ToolResultContent,
    ToolUseContent,
)


class TestTextContent:
    def test_basic_construction(self) -> None:
        tc = TextContent(text="hello")
        assert tc.text == "hello"
        assert tc.cache_breakpoint is None

    def test_with_cache_breakpoint(self) -> None:
        tc = TextContent(text="hello", cache_breakpoint="cp-1")
        assert tc.cache_breakpoint == "cp-1"

    def test_frozen(self) -> None:
        tc = TextContent(text="hello")
        with contextlib.suppress(AttributeError):
            tc.text = "changed"  # type: ignore[misc]
        assert tc.text == "hello"


class TestImageContent:
    def test_base64_source(self) -> None:
        ic = ImageContent(
            source_type="base64",
            data="aGVsbG8=",
            media_type="image/png",
        )
        assert ic.source_type == "base64"
        assert ic.data == "aGVsbG8="
        assert ic.media_type == "image/png"
        assert ic.detail is None

    def test_url_source(self) -> None:
        ic = ImageContent(
            source_type="url",
            data="https://example.com/img.png",
            media_type="image/png",
            detail="auto",
        )
        assert ic.source_type == "url"
        assert ic.data == "https://example.com/img.png"
        assert ic.detail == "auto"

    def test_frozen(self) -> None:
        ic = ImageContent(source_type="base64", data="x")
        with contextlib.suppress(AttributeError):
            ic.data = "y"  # type: ignore[misc]
        assert ic.data == "x"


class TestDocumentContent:
    def test_base64_pdf(self) -> None:
        dc = DocumentContent(
            source_type="base64",
            data="JVBERi0=",
            media_type="application/pdf",
            filename="doc.pdf",
        )
        assert dc.source_type == "base64"
        assert dc.media_type == "application/pdf"
        assert dc.filename == "doc.pdf"

    def test_url_source(self) -> None:
        dc = DocumentContent(
            source_type="url",
            data="https://example.com/doc.pdf",
        )
        assert dc.source_type == "url"
        assert dc.media_type is None
        assert dc.filename is None


class TestAudioContent:
    def test_base64(self) -> None:
        ac = AudioContent(
            source_type="base64",
            data="AAAA",
            media_type="audio/wav",
        )
        assert ac.source_type == "base64"
        assert ac.media_type == "audio/wav"

    def test_url(self) -> None:
        ac = AudioContent(
            source_type="url",
            data="https://example.com/audio.wav",
        )
        assert ac.source_type == "url"


class TestToolUseContent:
    def test_basic(self) -> None:
        tu = ToolUseContent(
            tool_use_id="call_123",
            name="get_weather",
            input={"q": "NYC"},
        )
        assert tu.tool_use_id == "call_123"
        assert tu.name == "get_weather"
        assert tu.input == {"q": "NYC"}

    def test_default_input(self) -> None:
        tu = ToolUseContent(tool_use_id="call_456", name="noop")
        assert tu.input is None


class TestToolResultContent:
    def test_empty_result(self) -> None:
        tr = ToolResultContent(tool_use_id="call_123")
        assert tr.content == []
        assert tr.is_error is False

    def test_with_text_content(self) -> None:
        tr = ToolResultContent(
            tool_use_id="call_123",
            content=[TextContent(text="result")],
        )
        assert len(tr.content) == 1
        assert isinstance(tr.content[0], TextContent)

    def test_with_nested_media(self) -> None:
        tr = ToolResultContent(
            tool_use_id="call_123",
            content=[
                TextContent(text="here is the image"),
                ImageContent(
                    source_type="base64",
                    data="aGVsbG8=",
                    media_type="image/png",
                ),
            ],
            is_error=True,
        )
        assert len(tr.content) == 2
        assert isinstance(tr.content[0], TextContent)
        assert isinstance(tr.content[1], ImageContent)
        assert tr.is_error is True

    def test_frozen_prevents_mutation(self) -> None:
        tr = ToolResultContent(
            tool_use_id="call_123",
            content=[TextContent(text="x")],
        )
        with contextlib.suppress(AttributeError):
            tr.content = []  # type: ignore[misc]
        assert len(tr.content) == 1


class TestThinkingContent:
    def test_basic(self) -> None:
        tc = ThinkingContent(thinking="reasoning here")
        assert tc.thinking == "reasoning here"


class TestRedactedThinkingContent:
    def test_basic(self) -> None:
        rtc = RedactedThinkingContent(data="encrypted_blob")
        assert rtc.data == "encrypted_blob"


class TestContentBlockUnion:
    def test_all_types_in_union(self) -> None:
        blocks: list[ContentBlock] = [
            TextContent(text="t"),
            ImageContent(source_type="base64", data="x"),
            DocumentContent(source_type="url", data="u"),
            AudioContent(source_type="base64", data="a"),
            ToolUseContent(tool_use_id="c1", name="f"),
            ToolResultContent(tool_use_id="c2"),
            ThinkingContent(thinking="r"),
            RedactedThinkingContent(data="e"),
        ]
        assert len(blocks) == 8

    def test_tool_result_nested_content_block(self) -> None:
        tr = ToolResultContent(
            tool_use_id="c1",
            content=[
                TextContent(text="text"),
                ImageContent(source_type="url", data="https://x.com/i.png"),
                AudioContent(source_type="base64", data="data"),
            ],
        )
        types = [type(b).__name__ for b in tr.content]
        assert types == ["TextContent", "ImageContent", "AudioContent"]
