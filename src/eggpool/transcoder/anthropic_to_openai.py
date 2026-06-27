"""Anthropic → OpenAI body transcoder."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

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


def _extract_text_blocks(blocks: Any) -> list[str]:  # pyright: ignore[reportUnknownParameterType,reportUnknownArgumentType]
    result: list[str] = []
    for block in blocks:  # pyright: ignore[reportUnknownVariableType]
        if isinstance(block, dict) and block.get("type") == "text":  # pyright: ignore[reportUnknownMemberType]
            result.append(str(block.get("text", "")))  # pyright: ignore[reportUnknownMemberType,reportUnknownArgumentType]
    return result


def _has_non_text_blocks(blocks: Any) -> bool:  # pyright: ignore[reportUnknownParameterType,reportUnknownArgumentType]
    return any(
        isinstance(b, dict) and b.get("type") != "text"  # pyright: ignore[reportUnknownMemberType,reportUnknownArgumentType]
        for b in blocks  # pyright: ignore[reportUnknownVariableType]
    )


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
                parts = _extract_text_blocks(system)
                if parts:
                    messages.append(
                        {
                            "role": "system",
                            "content": "\n\n".join(parts),
                        }
                    )

        for msg in payload.get("messages", []):  # pyright: ignore[reportUnknownVariableType]
            role = str(msg.get("role", ""))  # pyright: ignore[reportUnknownMemberType]
            content = msg.get("content", "")  # pyright: ignore[reportUnknownMemberType]

            if isinstance(content, str):
                messages.append({"role": role, "content": content})
            elif isinstance(content, list):
                text_parts = _extract_text_blocks(content)
                if _has_non_text_blocks(content):
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
        if stop_sequences is not None:
            if len(stop_sequences) == 1:
                out["stop"] = stop_sequences[0]
            else:
                out["stop"] = list(stop_sequences)

        metadata = payload.get("metadata")
        if isinstance(metadata, dict):
            user_id = metadata.get("user_id")  # pyright: ignore[reportUnknownMemberType,reportUnknownVariableType]
            if user_id is not None:
                out["user"] = str(user_id)  # pyright: ignore[reportUnknownArgumentType]

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

        choices = payload.get("choices", [])
        if not choices:
            return (
                _empty_anthropic_response(payload, context),
                warnings,
            )

        choice = choices[0]
        message = choice.get("message", {})
        finish_reason = str(choice.get("finish_reason", "stop"))  # pyright: ignore[reportUnknownMemberType]

        stop_reason = FINISH_REASON_MAP.get(finish_reason, "end_turn")

        content_text = str(message.get("content", ""))  # pyright: ignore[reportUnknownMemberType]
        refusal = message.get("refusal")  # pyright: ignore[reportUnknownMemberType]
        if refusal:
            content_text = str(refusal)
            stop_reason = "refusal"

        content_blocks: list[dict[str, Any]] = []
        if content_text:
            content_blocks.append({"type": "text", "text": content_text})

        usage = payload.get("usage", {})
        prompt_tokens = int(usage.get("prompt_tokens", 0))  # pyright: ignore[reportUnknownMemberType]
        completion_tokens = int(usage.get("completion_tokens", 0))  # pyright: ignore[reportUnknownMemberType]

        out: dict[str, Any] = {
            "id": payload.get("id", f"msg_{context.request_id}"),
            "type": "message",
            "role": "assistant",
            "content": content_blocks,
            "model": payload.get("model", ""),
            "stop_reason": stop_reason,
            "usage": {
                "input_tokens": prompt_tokens,
                "output_tokens": completion_tokens,
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
            error_type_str = str(error_obj.get("type", "api_error"))  # pyright: ignore[reportUnknownMemberType]
            message = str(error_obj.get("message", str(error_obj)))  # pyright: ignore[reportUnknownMemberType]

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
