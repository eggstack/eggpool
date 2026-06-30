"""Transcoder infrastructure — protocol translation between OpenAI and Anthropic."""

from __future__ import annotations

from eggpool.transcoder.context import TranscodeContext
from eggpool.transcoder.errors import UpstreamErrorEnvelope
from eggpool.transcoder.ids import ToolCallIdMap
from eggpool.transcoder.policy import TranscoderFeatures, TranscoderPolicy
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
        "non_text_content_dropped",
        # Phase 6.2 (vision)
        "image_unsupported_format",
        "image_too_large",
        "pdf_too_large",
        "document_url_dropped",
        "document_unsupported_media",
        # Phase 6.3 (thinking)
        "thinking_signature_dropped",
        "reasoning_content_dropped",
        # Phase 6.4 (structured outputs)
        "response_format_to_system_prompt",
        # Phase 6.5 (anthropic primitives)
        "top_k_dropped",
    }
)

__all__ = [
    "BodyTranscoder",
    "LOSS_WARNING_KINDS",
    "PROTOCOL_REQUIRED_STATIC_HEADERS",
    "StreamingTranscoder",
    "TranscodeContext",
    "TranscoderFeatures",
    "TranscoderPolicy",
    "ToolCallIdMap",
    "UpstreamErrorEnvelope",
    "canonicalise_usage",
    "select_streaming_transcoder",
    "select_transcoder",
]
