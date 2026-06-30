"""Anthropic → OpenAI body transcoder."""

from __future__ import annotations

import json
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

DROPPED_FIELDS = (
    "top_k",
    "thinking",
    "cache_control",
    "context_management",
    "container",
    "mcp_servers",
)


def _parse_tool_input(raw: Any, warnings: list[dict[str, Any]]) -> dict[str, Any]:
    """Parse ``tool_calls[].function.arguments`` into a JSON object.

    Invalid JSON is wrapped as ``{"__raw_arguments__": "<raw>"}`` and a
    ``malformed_tool_arguments`` warning is appended.
    """
    if not isinstance(raw, str):
        return {"__raw_arguments__": str(raw)}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        warnings.append(
            {
                "kind": "malformed_tool_arguments",
                "field": "tool_calls[].function.arguments",
                "raw": raw,
            }
        )
        return {"__raw_arguments__": raw}
    if isinstance(parsed, dict):
        return cast("dict[str, Any]", parsed)
    warnings.append(
        {
            "kind": "malformed_tool_arguments",
            "field": "tool_calls[].function.arguments",
            "raw": raw,
            "reason": "not_object",
        }
    )
    return {"__raw_arguments__": raw}


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
        id_map = context.id_map

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
                continue

            if isinstance(content, list):
                tool_call_accumulator: list[dict[str, Any]] = []
                tool_result_messages: list[dict[str, Any]] = []
                text_parts: list[str] = []

                for part in iter_objects(content):
                    part_type = part.get("type")
                    if part_type == "tool_use":
                        upstream_id = str(part.get("id", ""))
                        name = str(part.get("name", ""))
                        input_obj = as_object(part.get("input")) or {}
                        openai_id = id_map.generate_openai_id()
                        if upstream_id:
                            id_map.register(openai_id, upstream_id)
                            if upstream_id != openai_id:
                                warnings.append(
                                    {
                                        "kind": "tool_call_id_translated",
                                        "field": "messages[].content[].tool_use.id",
                                        "from": upstream_id,
                                        "to": openai_id,
                                    }
                                )
                        tool_call_accumulator.append(
                            {
                                "id": openai_id,
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": json.dumps(input_obj),
                                },
                            }
                        )
                    elif part_type == "tool_result":
                        tool_use_id = str(part.get("tool_use_id", ""))
                        client_id = id_map.to_client(tool_use_id)
                        if client_id is None:
                            client_id = id_map.generate_openai_id()
                            if tool_use_id:
                                id_map.register(client_id, tool_use_id)
                                warnings.append(
                                    {
                                        "kind": "tool_call_id_translated",
                                        "field": (
                                            "messages[].content[]"
                                            ".tool_result.tool_use_id"
                                        ),
                                        "from": tool_use_id,
                                        "to": client_id,
                                    }
                                )
                        result_content = part.get("content", "")
                        if isinstance(result_content, list):
                            joined_text = "\n".join(extract_text_blocks(result_content))
                            if has_non_text_blocks(result_content):
                                warnings.append(
                                    {
                                        "kind": "dropped_field",
                                        "field": (
                                            "messages[].content[]"
                                            ".tool_result.content[non-text]"
                                        ),
                                        "reason": "openai_unsupported",
                                    }
                                )
                            result_text = joined_text
                        else:
                            result_text = str(result_content)
                        if part.get("is_error") is True:
                            warnings.append(
                                {
                                    "kind": "tool_result_error_passthrough",
                                    "field": (
                                        "messages[].content[].tool_result.is_error"
                                    ),
                                }
                            )
                        tool_result_messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": client_id,
                                "content": result_text,
                            }
                        )
                    elif part_type == "text":
                        text_parts.append(str(part.get("text", "")))
                    else:
                        if part_type in ("image", "document", "audio"):
                            warnings.append(
                                {
                                    "kind": "non_text_content_dropped",
                                    "field": f"messages[{role}].content[non-text]",
                                    "type": part_type,
                                }
                            )
                        else:
                            warnings.append(
                                {
                                    "kind": "dropped_field",
                                    "field": f"messages[{role}].content[non-text]",
                                    "reason": "openai_unsupported",
                                }
                            )

                if tool_call_accumulator:
                    assistant_msg: dict[str, Any] = {
                        "role": "assistant",
                        "content": "\n".join(text_parts) if text_parts else "",
                        "tool_calls": tool_call_accumulator,
                    }
                    messages.append(assistant_msg)
                elif text_parts:
                    messages.append(
                        {
                            "role": role,
                            "content": "\n".join(text_parts),
                        }
                    )

                messages.extend(tool_result_messages)
                continue

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

        tools_raw = payload.get("tools")
        if isinstance(tools_raw, list):
            translated_tools: list[dict[str, Any]] = []
            for tool in iter_objects(tools_raw):
                if "cache_control" in tool:
                    warnings.append(
                        {
                            "kind": "cache_control_dropped",
                            "field": "tools[].cache_control",
                        }
                    )
                function: dict[str, Any] = {}
                if tool.get("name") is not None:
                    function["name"] = tool["name"]
                if tool.get("description") is not None:
                    function["description"] = tool["description"]
                if tool.get("input_schema") is not None:
                    function["parameters"] = tool["input_schema"]
                translated_tools.append(
                    {
                        "type": "function",
                        "function": function,
                    }
                )
            if translated_tools:
                out["tools"] = translated_tools

        tool_choice_raw = payload.get("tool_choice")
        if tool_choice_raw is not None:
            translated_choice = _translate_anthropic_tool_choice(
                tool_choice_raw, warnings
            )
            if translated_choice is not None:
                out["tool_choice"] = translated_choice

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
        id_map = context.id_map

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

        tool_calls_raw = message.get("tool_calls")
        if isinstance(tool_calls_raw, list):
            for call in iter_objects(tool_calls_raw):
                call_id = str(call.get("id", ""))
                function = as_object(call.get("function")) or {}
                name = str(function.get("name", ""))
                arguments_raw = function.get("arguments", "")
                parsed_input = _parse_tool_input(arguments_raw, warnings)
                upstream_id = id_map.generate_anthropic_id()
                if call_id:
                    id_map.register(call_id, upstream_id)
                    if call_id != upstream_id:
                        warnings.append(
                            {
                                "kind": "tool_call_id_translated",
                                "field": "choices[].message.tool_calls[].id",
                                "from": call_id,
                                "to": upstream_id,
                            }
                        )
                content_blocks.append(
                    {
                        "type": "tool_use",
                        "id": upstream_id,
                        "name": name,
                        "input": parsed_input,
                    }
                )

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


def _translate_anthropic_tool_choice(
    tool_choice: Any,
    warnings: list[dict[str, Any]],
) -> Any:
    """Translate an Anthropic ``tool_choice`` value into OpenAI shape."""
    if not isinstance(tool_choice, dict):
        warnings.append(
            {
                "kind": "invalid_tool_choice",
                "field": "tool_choice",
            }
        )
        return None

    choice_obj = cast("dict[str, Any]", tool_choice)
    choice_type = choice_obj.get("type")
    if choice_type == "auto":
        return "auto"
    if choice_type == "any":
        return "required"
    if choice_type == "tool":
        name = choice_obj.get("name", "")
        if not name:
            warnings.append(
                {
                    "kind": "invalid_tool_choice",
                    "field": "tool_choice.name",
                }
            )
            return None
        return {"type": "function", "function": {"name": str(name)}}
    if choice_type == "none":
        return "none"
    warnings.append(
        {
            "kind": "invalid_tool_choice",
            "field": "tool_choice",
            "from": choice_type,
        }
    )
    return None
