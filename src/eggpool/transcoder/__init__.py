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

LOSS_WARNING_KINDS: frozenset[str] = frozenset(
    {
        # Phase 2 (text-only)
        "dropped_field",
        "value_clamped",
        "missing_field",
        "lossy_mapping",
        "inserted_field",
        "streaming_transcoder",
        # Phase 6.1 (tools)
        "tool_call_id_translated",
        "parallel_tool_calls_collapsed",
        "tool_result_image_dropped",
        "malformed_tool_arguments",
        "invalid_tool_choice",
        "unsupported_tool_type",
        "empty_tool_use_block",
        "tool_call_id_changed",
        "tool_result_error_passthrough",
        "cache_control_dropped",
        "pause_turn",
        "refusal",
        "non_text_content_dropped",
        "tool_result_inferred",
    }
)

__all__ = [
    "BodyTranscoder",
    "LOSS_WARNING_KINDS",
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
