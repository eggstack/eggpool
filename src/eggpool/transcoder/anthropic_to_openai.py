"""Anthropic → OpenAI body transcoder."""

from __future__ import annotations

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

FINISH_REASON_MAP: dict[str, str] = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "content_filter": "refusal",
}

ERROR_TYPE_MAP: dict[str, str] = {
    "invalid_request_error": "invalid_request_error",
    "invalid_api_key": "authentication_error",
    "insufficient_quota": "billing_error",
    "rate_limit_exceeded": "rate_limit_error",
    "api_error": "api_error",
    "timeout": "timeout_error",
}

DROPPED_FIELDS = ("top_k", "thinking", "tools", "tool_choice")


class AnthropicToOpenAI:
    """Translates Anthropic requests/responses to/from OpenAI format."""

    client_protocol = "anthropic"
    upstream_protocol = "openai"

    def encode_request(
        self,
        payload: dict[str, Any],
        context: TranscodeContext,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        warnings: list[dict[str, Any]] = []
        out: dict[str, Any] = {}

        messages: list[dict[str, Any]] = []

        system = payload.get("system")
        if system is not None:
            if isinstance(system, str):
                messages.append({"role": "system", "content": system})
            elif isinstance(system, list):
                parts = extract_text_blocks(system)
                if parts:
                    messages.append(
                        {
                            "role": "system",
                            "content": "\n\n".join(parts),
                        }
                    )

        for msg in iter_objects(payload.get("messages", [])):
            role = str(msg.get("role", ""))
            content = msg.get("content", "")

            if isinstance(content, str):
                messages.append({"role": role, "content": content})
            elif isinstance(content, list):
                text_parts = extract_text_blocks(content)
                if has_non_text_blocks(content):
                    warnings.append(
                        {
                            "kind": "dropped_field",
                            "field": f"messages[{role}].content[non-text]",
                            "reason": "openai_unsupported",
                        }
                    )
                messages.append({"role": role, "content": "\n".join(text_parts)})
            else:
                messages.append({"role": role, "content": str(content)})

        if not messages:
            warnings.append(
                {
                    "kind": "inserted_field",
                    "field": "messages",
                    "reason": "empty_messages",
                }
            )
            messages.append({"role": "user", "content": ""})

        out["messages"] = messages

        model = payload.get("model")
        if model is not None:
            out["model"] = model

        stop_sequences = payload.get("stop_sequences")
        if isinstance(stop_sequences, list):
            stop_values = cast("list[object]", stop_sequences)
            if len(stop_values) == 1:
                out["stop"] = stop_values[0]
            else:
                out["stop"] = list(stop_values)

        metadata = as_object(payload.get("metadata"))
        if metadata is not None:
            user_id = metadata.get("user_id")
            if user_id is not None:
                out["user"] = str(user_id)

        temperature = payload.get("temperature")
        if temperature is not None:
            out["temperature"] = temperature

        max_tokens = payload.get("max_tokens")
        if max_tokens is not None:
            out["max_tokens"] = max_tokens

        for field in DROPPED_FIELDS:
            if field in payload:
                warnings.append(
                    {
                        "kind": "dropped_field",
                        "field": field,
                        "reason": "openai_unsupported",
                    }
                )

        return out, warnings

    def decode_response(
        self,
        payload: dict[str, Any],
        context: TranscodeContext,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        warnings: list[dict[str, Any]] = []

        choices = list(iter_objects(payload.get("choices", [])))
        if not choices:
            return (
                _empty_anthropic_response(payload, context),
                warnings,
            )

        choice = choices[0]
        message = as_object(choice.get("message")) or {}
        finish_reason = str(choice.get("finish_reason", "stop"))

        stop_reason = FINISH_REASON_MAP.get(finish_reason, "end_turn")

        content_text = str(message.get("content", ""))
        refusal = message.get("refusal")
        if refusal:
            content_text = str(refusal)
            stop_reason = "refusal"

        content_blocks: list[dict[str, Any]] = []
        if content_text:
            content_blocks.append({"type": "text", "text": content_text})

        usage = as_object(payload.get("usage"))
        prompt_tokens = token_count_from(usage, "prompt_tokens")
        completion_tokens = token_count_from(usage, "completion_tokens")
        prompt_tokens_details = (
            as_object(usage.get("prompt_tokens_details")) if usage is not None else None
        )
        cache_read_tokens = token_count_from(prompt_tokens_details, "cached_tokens")
        cache_creation_tokens = token_count_from(
            prompt_tokens_details,
            "cache_creation_tokens",
        )

        out_usage: dict[str, int] = {
            "input_tokens": prompt_tokens,
            "output_tokens": completion_tokens,
        }
        if cache_read_tokens > 0:
            out_usage["cache_read_input_tokens"] = cache_read_tokens
        if cache_creation_tokens > 0:
            out_usage["cache_creation_input_tokens"] = cache_creation_tokens

        out: dict[str, Any] = {
            "id": payload.get("id", f"msg_{context.request_id}"),
            "type": "message",
            "role": "assistant",
            "content": content_blocks,
            "model": payload.get("model", ""),
            "stop_reason": stop_reason,
            "usage": out_usage,
        }

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
                    "type": "api_error",
                    "error": {"message": "Unknown error"},
                },
                warnings,
            )

        error_obj = upstream_payload.get("error", {})
        if isinstance(error_obj, str):
            error_type_str = "api_error"
            message = error_obj
        else:
            error_map = as_object(error_obj) or {}
            error_type_str = str(error_map.get("type", "api_error"))
            message = str(error_map.get("message", str(error_map)))

        mapped_type = ERROR_TYPE_MAP.get(error_type_str, "api_error")

        out: dict[str, Any] = {
            "type": mapped_type,
            "error": {"message": message},
        }

        return upstream_status, out, warnings


def _empty_anthropic_response(
    payload: dict[str, Any],
    context: TranscodeContext,
) -> dict[str, Any]:
    return {
        "id": payload.get("id", f"msg_{context.request_id}"),
        "type": "message",
        "role": "assistant",
        "content": [],
        "model": payload.get("model", ""),
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }
