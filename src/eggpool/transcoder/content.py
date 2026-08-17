"""Narrow content-block representation for cross-protocol translation.

The IR covers content blocks only.  Sampling, reasoning controls,
caching, tool-choice semantics, structured output, and provider
extensions remain in their existing protocol-specific paths.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# ---------------------------------------------------------------------------
# Content IR types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TextContent:
    """A text content block."""

    text: str
    cache_breakpoint: str | None = None


@dataclass(frozen=True)
class ImageContent:
    """An image content block.

    ``source_type`` distinguishes the source form for capability gating:
    ``"base64"`` for inline encoded data, ``"url"`` for remote references.
    """

    source_type: Literal["base64", "url"]
    data: str
    """Base64-encoded data or URL string."""
    media_type: str | None = None
    """MIME type (required for base64 sources)."""
    detail: str | None = None


@dataclass(frozen=True)
class DocumentContent:
    """A document/PDF content block.

    ``source_type`` distinguishes the source form for capability gating.
    """

    source_type: Literal["base64", "url"]
    data: str
    """Base64-encoded data or URL string."""
    media_type: str | None = None
    """MIME type (e.g. ``application/pdf``)."""
    filename: str | None = None


@dataclass(frozen=True)
class AudioContent:
    """An audio content block (only preserved when both protocols support it)."""

    source_type: Literal["base64", "url"]
    data: str
    media_type: str | None = None


@dataclass(frozen=True)
class ToolUseContent:
    """A tool-use request block."""

    tool_use_id: str
    name: str
    input: Any = None


@dataclass(frozen=True)
class ToolResultContent:
    """A tool-result block whose content can itself contain media."""

    tool_use_id: str
    content: list[ContentBlock] = field(default_factory=list)  # type: ignore[type-arg]
    is_error: bool = False


@dataclass(frozen=True)
class ThinkingContent:
    """A thinking/reasoning block (preserves ordering)."""

    thinking: str


@dataclass(frozen=True)
class RedactedThinkingContent:
    """A redacted/encrypted thinking block."""

    data: str


# Union type for all content blocks
ContentBlock = (
    TextContent
    | ImageContent
    | DocumentContent
    | AudioContent
    | ToolUseContent
    | ToolResultContent
    | ThinkingContent
    | RedactedThinkingContent
)
