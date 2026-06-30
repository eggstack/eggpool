"""OpenAI → Anthropic body transcoder."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, cast

from eggpool.transcoder.json_helpers import (
    as_object,
    extract_text_blocks,
    has_non_text_blocks,
    iter_objects,
    token_count_from,
)

if TYPE_CHECKING:
    from eggpool.transcoder.context import TranscodeContext

STOP_REASON_MAP: dict[str, str] = {
    "end_turn": "stop",
    "max_tokens": "length",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
    "refusal": "content_filter",
    "pause_turn": "tool_calls",
    "model_context_window_exceeded": "length",
}

ERROR_TYPE_MAP: dict[str, str] = {
    "invalid_request_error": "invalid_request_error",
    "authentication_error": "invalid_api_key",
    "permission_error": "insufficient_quota",
    "not_found_error": "invalid_request_error",
    "request_too_large": "invalid_request_error",
    "rate_limit_error": "rate_limit_exceeded",
    "api_error": "api_error",
    "overloaded_error": "api_error",
    "billing_error": "insufficient_quota",
    "timeout_error": "timeout",
    "conflict_error": "invalid_request_error",
    "internal_error": "api_error",
}

DROPPED_FIELDS = (
    "top_p",
    "frequency_penalty",
    "presence_penalty",
    "n",
    "logprobs",
    "top_logprobs",
    "response_format",
    "seed",
    "user",
    "tools",
    "tool_choice",
    "functions",
    "function_call",
    "parallel_tool_calls",
    "stream_options",
    "logit_bias",
)


class OpenAIToAnthropic:
    """Translates OpenAI requests/responses to/from Anthropic format."""

    client_protocol = "openai"
    upstream_protocol = "anthropic"

    def encode_request(
        self,
        payload: dict[str, Any],
        context: TranscodeContext,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        warnings: list[dict[str, Any]] = []
        out: dict[str, Any] = {}

        system_parts: list[str] = []
        messages: list[dict[str, Any]] = []

        for msg in iter_objects(payload.get("messages", [])):
            role = str(msg.get("role", ""))
            content = msg.get("content", "")

            if role in ("system", "developer"):
                if isinstance(content, str):
                    system_parts.append(content)
                elif isinstance(content, list):
                    system_parts.extend(extract_text_blocks(content))
                continue

            if role == "tool":
                warnings.append(
                    {
                        "kind": "dropped_field",
                        "field": "messages[tool]",
                        "reason": "anthropic_unsupported",
                    }
                )
                continue

            if isinstance(content, str):
                messages.append({"role": role, "content": content})
            elif isinstance(content, list):
                text_parts = extract_text_blocks(content)
                if has_non_text_blocks(content):
                    warnings.append(
                        {
                            "kind": "dropped_field",
                            "field": f"messages[{role}].content[non-text]",
                            "reason": "anthropic_unsupported",
                        }
                    )
                messages.append({"role": role, "content": "\n".join(text_parts)})
            else:
                messages.append({"role": role, "content": str(content)})

        if system_parts:
            out["system"] = "\n\n".join(system_parts)

        if not messages:
            messages.append({"role": "user", "content": "(empty)"})

        out["messages"] = messages

        model = payload.get("model")
        if model is not None:
            out["model"] = model

        temperature = payload.get("temperature")
        if temperature is not None:
            if temperature > 1.0:
                warnings.append(
                    {
                        "kind": "value_clamped",
                        "field": "temperature",
                        "from": temperature,
                        "to": 1.0,
                    }
                )
                out["temperature"] = 1.0
            else:
                out["temperature"] = temperature

        max_tokens = payload.get("max_tokens")
        if max_tokens is None:
            max_tokens = payload.get("max_completion_tokens")
        if max_tokens is None:
            max_tokens = 4096
            warnings.append(
                {
                    "kind": "missing_field",
                    "field": "max_tokens",
                    "default": max_tokens,
                }
            )
        out["max_tokens"] = max_tokens

        stop = payload.get("stop")
        if stop is not None:
            if isinstance(stop, str):
                out["stop_sequences"] = [stop]
            elif isinstance(stop, list):
                stop_values = cast("list[object]", stop)
                out["stop_sequences"] = [str(s) for s in stop_values]

        for field in DROPPED_FIELDS:
            if field in payload:
                warnings.append(
                    {
                        "kind": "dropped_field",
                        "field": field,
                        "reason": "anthropic_unsupported",
                    }
                )

        return out, warnings

    def decode_response(
        self,
        payload: dict[str, Any],
        context: TranscodeContext,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        warnings: list[dict[str, Any]] = []

        content_blocks = payload.get("content", [])
        text_parts = extract_text_blocks(content_blocks)
        content_text = "".join(text_parts)

        stop_reason = str(payload.get("stop_reason", "end_turn"))
        finish_reason = STOP_REASON_MAP.get(stop_reason, "stop")
        if stop_reason in ("stop_sequence", "pause_turn"):
            warnings.append(
                {
                    "kind": "lossy_mapping",
                    "field": "stop_reason",
                    "from": stop_reason,
                    "to": finish_reason,
                }
            )

        usage = as_object(payload.get("usage"))
        prompt_tokens = token_count_from(usage, "input_tokens")
        completion_tokens = token_count_from(usage, "output_tokens")
        cache_read_tokens = token_count_from(usage, "cache_read_input_tokens")
        cache_creation_tokens = token_count_from(
            usage,
            "cache_creation_input_tokens",
        )

        out: dict[str, Any] = {
            "id": payload.get("id", f"chatcmpl-{context.request_id}"),
            "object": "chat.completion",
            "created": int(time.time()),
            "model": payload.get("model", ""),
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": content_text,
                    },
                    "finish_reason": finish_reason,
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }

        if cache_read_tokens > 0 or cache_creation_tokens > 0:
            prompt_tokens_details: dict[str, int] = {}
            if cache_read_tokens > 0:
                prompt_tokens_details["cached_tokens"] = cache_read_tokens
            if cache_creation_tokens > 0:
                prompt_tokens_details["cache_creation_tokens"] = cache_creation_tokens
            out["usage"]["prompt_tokens_details"] = prompt_tokens_details

        return out, warnings

    def reencode_error(
        self,
        upstream_status: int,
        upstream_payload: dict[str, Any] | None,
        context: TranscodeContext,
    ) -> tuple[int, dict[str, Any], list[dict[str, Any]]]:
        warnings: list[dict[str, Any]] = []

        if upstream_payload is None:
            return (
                upstream_status,
                {
                    "error": {
                        "message": "Unknown error",
                        "type": "api_error",
                        "code": None,
                    }
                },
                warnings,
            )

        error_type_raw = upstream_payload.get("type", "api_error")
        error_obj = upstream_payload.get("error", {})
        if isinstance(error_obj, dict):
            error_map = as_object(error_obj) or {}
            error_type = str(error_map.get("type", error_type_raw))
            message = str(error_map.get("message", str(error_map)))
        else:
            error_type = str(error_type_raw)
            message = str(error_obj)

        mapped_type = ERROR_TYPE_MAP.get(error_type, "invalid_request_error")

        out: dict[str, Any] = {
            "error": {
                "message": message,
                "type": mapped_type,
                "code": error_type,
                "param": None,
            }
        }

        return upstream_status, out, warnings
