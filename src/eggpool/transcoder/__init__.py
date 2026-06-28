"""Transcoder infrastructure — protocol translation between OpenAI and Anthropic."""

from __future__ import annotations

from eggpool.transcoder.context import TranscodeContext
from eggpool.transcoder.errors import UpstreamErrorEnvelope
from eggpool.transcoder.ids import ToolCallIdMap
from eggpool.transcoder.policy import TranscoderPolicy
from eggpool.transcoder.protocol import BodyTranscoder, select_transcoder
from eggpool.transcoder.static_headers import PROTOCOL_REQUIRED_STATIC_HEADERS
from eggpool.transcoder.streaming import (
    StreamingTranscoder,
    select_streaming_transcoder,
)
from eggpool.transcoder.usage import canonicalise_usage

__all__ = [
    "BodyTranscoder",
    "PROTOCOL_REQUIRED_STATIC_HEADERS",
    "StreamingTranscoder",
    "TranscodeContext",
    "TranscoderPolicy",
    "ToolCallIdMap",
    "UpstreamErrorEnvelope",
    "canonicalise_usage",
    "select_streaming_transcoder",
    "select_transcoder",
]
