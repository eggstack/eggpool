"""Typed contracts for concrete wire-surface codecs."""

from __future__ import annotations

from eggpool.wire.codecs.base import (
    CanonicalCodec,
    CodecAlias,
    CodecRegistry,
    PassthroughCodec,
    WireCodecError,
)
from eggpool.wire.codecs.compat import AnthropicMessagesCodec, OpenAIChatCodec
from eggpool.wire.codecs.defaults import (
    GeminiGenerateContentCodec,
    GeminiInteractionsCodec,
    OpenAIResponsesCodec,
)

__all__ = [
    "CanonicalCodec",
    "CodecAlias",
    "CodecRegistry",
    "PassthroughCodec",
    "WireCodecError",
    "AnthropicMessagesCodec",
    "OpenAIChatCodec",
    "OpenAIResponsesCodec",
    "GeminiInteractionsCodec",
    "GeminiGenerateContentCodec",
]
