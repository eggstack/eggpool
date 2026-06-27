"""OpenAI → Anthropic body transcoder."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

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
)


def _extract_text_blocks(blocks: Any) -> list[str]:  # pyright: ignore[reportUnknownParameterType,reportUnknownArgumentType]
    result: list[str] = []
    for block in blocks:  # pyright: ignore[reportUnknownVariableType]
        if isinstance(block, dict) and block.get("type") == "text":  # pyright: ignore[reportUnknownMemberType]
            result.append(str(block.get("text", "")))  # pyright: ignore[reportUnknownMemberType,reportUnknownArgumentType]
    return result


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

        for msg in payload.get("messages", []):  # pyright: ignore[reportUnknownVariableType]
            role = str(msg.get("role", ""))  # pyright: ignore[reportUnknownMemberType]
            content = msg.get("content", "")  # pyright: ignore[reportUnknownMemberType]

            if role == "system":
                if isinstance(content, str):
                    system_parts.append(content)
                elif isinstance(content, list):
                    system_parts.extend(_extract_text_blocks(content))
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
                text_parts = _extract_text_blocks(content)
                has_non_text = any(
                    isinstance(b, dict) and b.get("type") != "text"  # pyright: ignore[reportUnknownMemberType,reportUnknownArgumentType]
                    for b in content  # pyright: ignore[reportUnknownVariableType]
                )
                if has_non_text:
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
                out["stop_sequences"] = [str(s) for s in stop]  # pyright: ignore[reportUnknownVariableType,reportUnknownArgumentType]

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
        text_parts = _extract_text_blocks(content_blocks)
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

        usage = payload.get("usage", {})
        prompt_tokens = int(usage.get("input_tokens", 0))  # pyright: ignore[reportUnknownMemberType]
        completion_tokens = int(usage.get("output_tokens", 0))  # pyright: ignore[reportUnknownMemberType]

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
            error_type = str(error_obj.get("type", error_type_raw))  # pyright: ignore[reportUnknownMemberType,reportUnknownArgumentType]
            message = str(error_obj.get("message", str(error_obj)))  # pyright: ignore[reportUnknownMemberType,reportUnknownArgumentType]
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
