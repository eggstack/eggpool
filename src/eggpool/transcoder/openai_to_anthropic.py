"""OpenAI → Anthropic body transcoder."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any, cast

from eggpool.transcoder.json_helpers import (
    as_object,
    decode_base64_payload,
    extract_text_blocks,
    has_non_text_blocks,
    iter_objects,
    split_base64_data_uri,
    token_count_from,
)

if TYPE_CHECKING:
    from eggpool.catalog.capabilities import ThinkingCapability
    from eggpool.transcoder.context import TranscodeContext
    from eggpool.transcoder.policy import TranscoderFeatures

_ANTHROPIC_IMAGE_SIZE_LIMIT = 5 * 1024 * 1024  # 5 MB
_ANTHROPIC_PDF_SIZE_LIMIT = 32 * 1024 * 1024  # 32 MB

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
    "seed",
    "user",
    "functions",
    "function_call",
    "logit_bias",
)

_PAUSE_TURN_FUNCTION_NAME = "__eggpool_pause_turn__"


def _parse_tool_arguments(raw: Any, warnings: list[dict[str, Any]]) -> dict[str, Any]:
    """Parse a JSON string into a dict, wrapping invalid JSON with a marker.

    On failure, appends a ``malformed_tool_arguments`` warning and
    returns ``{"__raw_arguments__": "<raw string>"}``.
    """
    if not isinstance(raw, str):
        return {"__raw_arguments__": str(raw)}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        warnings.append(
            {
                "kind": "malformed_tool_arguments",
                "field": "function.arguments",
                "raw": raw,
            }
        )
        return {"__raw_arguments__": raw}
    if isinstance(parsed, dict):
        return cast("dict[str, Any]", parsed)
    warnings.append(
        {
            "kind": "malformed_tool_arguments",
            "field": "function.arguments",
            "raw": raw,
            "reason": "not_object",
        }
    )
    return {"__raw_arguments__": raw}


def _translate_openai_content_to_anthropic(
    content: list[dict[str, Any]],
    *,
    vision_enabled: bool,
    warnings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Translate an OpenAI content-parts list to Anthropic content blocks.

    When ``vision_enabled`` is ``True``, ``image_url`` parts are
    translated to Anthropic ``image`` blocks.  Otherwise they are
    dropped with a warning (v1 behaviour).
    """
    blocks: list[dict[str, Any]] = []
    for part in content:
        part_type = part.get("type")
        if part_type == "text":
            text = part.get("text", "")
            if text:
                blocks.append({"type": "text", "text": str(text)})
        elif part_type == "image_url":
            if not vision_enabled:
                # Warning emitted by the caller with role context
                continue
            image_url_obj = as_object(part.get("image_url")) or {}
            url = str(image_url_obj.get("url", ""))
            if url.startswith("data:"):
                data_uri = split_base64_data_uri(url)
                if data_uri is None:
                    warnings.append(
                        {
                            "kind": "image_unsupported_format",
                            "field": "content[image_url]",
                        }
                    )
                    continue
                media_type, encoded = data_uri
                decoded = decode_base64_payload(encoded)
                if decoded is None:
                    warnings.append(
                        {
                            "kind": "image_unsupported_format",
                            "field": "content[image_url]",
                        }
                    )
                    continue
                size = len(decoded)
                if size > _ANTHROPIC_IMAGE_SIZE_LIMIT:
                    warnings.append(
                        {
                            "kind": "image_too_large",
                            "field": "content[image_url]",
                            "size_bytes": size,
                            "limit_bytes": _ANTHROPIC_IMAGE_SIZE_LIMIT,
                        }
                    )
                    continue
                blocks.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": encoded,
                        },
                    }
                )
            elif url.startswith("http://") or url.startswith("https://"):
                blocks.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "url",
                            "url": url,
                        },
                    }
                )
            else:
                warnings.append(
                    {
                        "kind": "image_unsupported_format",
                        "field": "content[image_url]",
                        "reason": "unknown_url_scheme",
                    }
                )
        elif part_type == "input_audio":
            warnings.append(
                {
                    "kind": "dropped_field",
                    "field": "content[input_audio]",
                    "reason": "anthropic_unsupported",
                }
            )
        elif part_type == "file":
            warnings.append(
                {
                    "kind": "dropped_field",
                    "field": "content[file]",
                    "reason": "anthropic_unsupported",
                }
            )
        else:
            warnings.append(
                {
                    "kind": "dropped_field",
                    "field": f"content[{part_type}]",
                    "reason": "anthropic_unsupported",
                }
            )
    return blocks


class OpenAIToAnthropic:
    """Translates OpenAI requests/responses to/from Anthropic format."""

    client_protocol = "openai"
    upstream_protocol = "anthropic"

    def encode_request(
        self,
        payload: dict[str, Any],
        context: TranscodeContext,
        *,
        features: TranscoderFeatures | None = None,
        thinking_capability: ThinkingCapability | None = None,
        budget_defaults: dict[str, int] | None = None,
        budget_resolution_policy: str = "lenient",
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        warnings: list[dict[str, Any]] = []
        out: dict[str, Any] = {}

        system_parts: list[str] = []
        messages: list[dict[str, Any]] = []
        id_map = context.id_map

        stream_options_raw = payload.get("stream_options")
        if isinstance(stream_options_raw, dict):
            stream_options = cast("dict[str, Any]", stream_options_raw)
            include_usage = bool(stream_options.get("include_usage", False))
            context.request_include_usage = include_usage
            warnings.append(
                {
                    "kind": "dropped_field",
                    "field": "stream_options",
                    "reason": "anthropic_unsupported",
                }
            )

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
                tool_call_id = str(msg.get("tool_call_id", ""))
                tool_use_id = id_map.to_upstream(tool_call_id)
                if tool_use_id is None:
                    tool_use_id = id_map.generate_anthropic_id()
                    id_map.register(tool_call_id, tool_use_id)
                    if tool_call_id:
                        warnings.append(
                            {
                                "kind": "tool_call_id_translated",
                                "field": "messages[tool].tool_call_id",
                                "from": tool_call_id,
                                "to": tool_use_id,
                            }
                        )

                if isinstance(content, str):
                    tool_result_content: Any = content
                elif isinstance(content, list):
                    text_parts = extract_text_blocks(content)
                    if has_non_text_blocks(content):
                        warnings.append(
                            {
                                "kind": "tool_result_image_dropped",
                                "field": "messages[tool].content",
                            }
                        )
                    tool_result_content = "\n".join(text_parts)
                else:
                    tool_result_content = str(content)

                tool_result_block: dict[str, Any] = {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": tool_result_content,
                }
                if msg.get("is_error") is True:
                    tool_result_block["is_error"] = True

                messages.append(
                    {
                        "role": "user",
                        "content": [tool_result_block],
                    }
                )
                continue

            if role == "assistant":
                tool_calls_raw = msg.get("tool_calls")
                tool_calls: list[dict[str, Any]] = []
                if isinstance(tool_calls_raw, list):
                    for call in iter_objects(tool_calls_raw):
                        tool_calls.append(call)

                content_blocks: list[dict[str, Any]] = []

                reasoning_content = msg.get("reasoning_content")
                if (
                    reasoning_content
                    and isinstance(reasoning_content, str)
                    and features is not None
                    and features.thinking
                ):
                    content_blocks.append(
                        {"type": "thinking", "thinking": reasoning_content}
                    )
                elif reasoning_content:
                    warnings.append(
                        {
                            "kind": "reasoning_content_dropped",
                            "field": "messages[assistant].reasoning_content",
                        }
                    )

                if isinstance(content, str):
                    if content:
                        content_blocks.append({"type": "text", "text": content})
                elif isinstance(content, list):
                    text_parts = extract_text_blocks(content)
                    for part in text_parts:
                        content_blocks.append({"type": "text", "text": part})
                    if has_non_text_blocks(content):
                        warnings.append(
                            {
                                "kind": "dropped_field",
                                "field": "messages[assistant].content[non-text]",
                                "reason": "anthropic_unsupported",
                            }
                        )
                else:
                    if content:
                        content_blocks.append({"type": "text", "text": str(content)})

                for call in tool_calls:
                    call_id = str(call.get("id", ""))
                    function = as_object(call.get("function")) or {}
                    name = str(function.get("name", ""))
                    arguments_raw = function.get("arguments", "")
                    parsed_input = _parse_tool_arguments(arguments_raw, warnings)
                    upstream_id = id_map.generate_anthropic_id()
                    if call_id:
                        id_map.register(call_id, upstream_id)
                        if call_id != upstream_id:
                            warnings.append(
                                {
                                    "kind": "tool_call_id_translated",
                                    "field": "messages[assistant].tool_calls[].id",
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

                if content_blocks:
                    if len(content_blocks) == 1 and content_blocks[0]["type"] == "text":
                        messages.append(
                            {
                                "role": role,
                                "content": str(content_blocks[0]["text"]),
                            }
                        )
                    else:
                        messages.append({"role": role, "content": content_blocks})
                else:
                    messages.append({"role": role, "content": ""})
                continue

            if isinstance(content, str):
                messages.append({"role": role, "content": content})
            elif isinstance(content, list):
                vision_enabled = features is not None and features.vision
                has_image_parts = any(
                    p.get("type") == "image_url" for p in iter_objects(content)
                )
                if has_image_parts and not vision_enabled:
                    warnings.append(
                        {
                            "kind": "dropped_field",
                            "field": f"messages[{role}].content[non-text]",
                            "reason": "anthropic_unsupported",
                        }
                    )
                inner_warnings: list[dict[str, Any]] = (
                    [] if not vision_enabled else warnings
                )
                anthropic_blocks = _translate_openai_content_to_anthropic(
                    cast("list[dict[str, Any]]", content),
                    vision_enabled=vision_enabled,
                    warnings=inner_warnings,
                )
                has_non_text = any(b.get("type") != "text" for b in anthropic_blocks)
                text_only = [
                    b["text"] for b in anthropic_blocks if b.get("type") == "text"
                ]
                if has_non_text:
                    messages.append({"role": role, "content": anthropic_blocks or ""})
                elif text_only:
                    messages.append({"role": role, "content": "\n".join(text_only)})
                else:
                    messages.append({"role": role, "content": ""})
            else:
                messages.append({"role": role, "content": str(content)})

        if not messages:
            messages.append({"role": "user", "content": "(empty)"})

        out["messages"] = messages

        model = payload.get("model")
        if model is not None:
            out["model"] = model

        if payload.get("stream") is True:
            out["stream"] = True

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

        reasoning_effort = payload.get("reasoning_effort")
        if reasoning_effort is not None and features is not None and features.thinking:
            from eggpool.transcoder.budget_resolver import (
                BudgetResolutionError,
                resolve_thinking_budget,
            )

            try:
                resolution = resolve_thinking_budget(
                    model_id=payload.get("model", "unknown"),
                    provider_id=None,
                    requested_effort=str(reasoning_effort),
                    capability=thinking_capability,
                    budget_defaults=budget_defaults,
                    budget_resolution_policy=budget_resolution_policy,
                )
                out["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": resolution.budget_tokens,
                }
                warnings.extend(resolution.warnings)
            except BudgetResolutionError as exc:
                warnings.append(
                    {
                        "kind": "budget_rejected",
                        "reason": str(exc),
                        "model_id": payload.get("model", "unknown"),
                    }
                )
                raise
        elif reasoning_effort is not None:
            warnings.append(
                {
                    "kind": "dropped_field",
                    "field": "reasoning_effort",
                    "reason": "thinking_disabled",
                }
            )

        response_format = payload.get("response_format")
        if response_format is not None and isinstance(response_format, dict):
            rf_obj = cast("dict[str, Any]", response_format)
            if features is not None and features.structured_outputs:
                rf_type = str(rf_obj.get("type", ""))
                schema_text = ""
                if rf_type == "json_object":
                    schema_text = (
                        "\n\nRespond with a valid JSON object. "
                        "Do not include any text outside the JSON."
                    )
                elif rf_type == "json_schema":
                    json_schema = as_object(rf_obj.get("json_schema")) or {}
                    schema_obj = as_object(json_schema.get("schema")) or {}
                    strict = bool(json_schema.get("strict", False))
                    schema_text = (
                        "\n\nRespond with a JSON object that matches this schema: "
                        + json.dumps(schema_obj)
                        + ". Do not include any text outside the JSON."
                    )
                    if strict:
                        schema_text += " Be precise; do not omit required fields."
                if schema_text:
                    system_parts.append(schema_text)
                    warnings.append(
                        {
                            "kind": "response_format_to_system_prompt",
                            "field": "response_format",
                            "type": rf_type,
                        }
                    )
            else:
                warnings.append(
                    {
                        "kind": "dropped_field",
                        "field": "response_format",
                        "reason": "anthropic_unsupported",
                    }
                )

        if system_parts:
            out["system"] = "\n\n".join(system_parts)

        tools_raw = payload.get("tools")
        if isinstance(tools_raw, list):
            translated_tools: list[dict[str, Any]] = []
            for tool in iter_objects(tools_raw):
                tool_type = tool.get("type", "function")
                if tool_type == "function":
                    function = as_object(tool.get("function")) or {}
                    name = str(function.get("name", ""))
                    description = function.get("description")
                    parameters = function.get("parameters", {})
                    translated_tool: dict[str, Any] = {
                        "name": name,
                        "input_schema": parameters,
                    }
                    if description is not None:
                        translated_tool["description"] = description
                    if function.get("strict") is not None:
                        warnings.append(
                            {
                                "kind": "dropped_field",
                                "field": "tools[].function.strict",
                                "reason": "anthropic_unsupported",
                            }
                        )
                    translated_tools.append(translated_tool)
                else:
                    warnings.append(
                        {
                            "kind": "unsupported_tool_type",
                            "field": "tools[]",
                            "type": tool_type,
                        }
                    )
            if translated_tools:
                out["tools"] = translated_tools

        tool_choice_raw = payload.get("tool_choice")
        if tool_choice_raw is not None:
            translated_choice = _translate_openai_tool_choice(tool_choice_raw, warnings)
            if translated_choice is not None:
                out["tool_choice"] = translated_choice

        parallel_raw = payload.get("parallel_tool_calls")
        if parallel_raw is False:
            warnings.append(
                {
                    "kind": "parallel_tool_calls_collapsed",
                    "field": "parallel_tool_calls",
                    "reason": "anthropic_unsupported",
                }
            )

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
        *,
        features: TranscoderFeatures | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        warnings: list[dict[str, Any]] = []
        id_map = context.id_map

        content_blocks = payload.get("content", [])
        text_parts = extract_text_blocks(content_blocks)
        content_text = "".join(text_parts)

        reasoning_content: str | None = None
        tool_calls: list[dict[str, Any]] = []
        if isinstance(content_blocks, list):
            for block in iter_objects(content_blocks):
                block_type = block.get("type")
                if block_type == "tool_use":
                    upstream_id = str(block.get("id", ""))
                    name = str(block.get("name", ""))
                    input_obj = as_object(block.get("input")) or {}
                    openai_id = id_map.generate_openai_id()
                    if upstream_id:
                        id_map.register(openai_id, upstream_id)
                        if upstream_id != openai_id:
                            warnings.append(
                                {
                                    "kind": "tool_call_id_translated",
                                    "field": "content[].tool_use.id",
                                    "from": upstream_id,
                                    "to": openai_id,
                                }
                            )
                    tool_calls.append(
                        {
                            "id": openai_id,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(input_obj),
                            },
                        }
                    )
                elif block_type == "thinking":
                    thinking_text = str(block.get("thinking", ""))
                    if features is not None and features.thinking:
                        reasoning_content = thinking_text
                        if block.get("signature"):
                            warnings.append(
                                {
                                    "kind": "thinking_signature_dropped",
                                    "field": "content[].thinking.signature",
                                }
                            )
                    else:
                        warnings.append(
                            {
                                "kind": "reasoning_content_dropped",
                                "field": "content[].thinking",
                            }
                        )
                elif block_type == "redacted_thinking":
                    warnings.append(
                        {
                            "kind": "dropped_field",
                            "field": "content[redacted_thinking]",
                            "reason": "anthropic_unsupported",
                        }
                    )

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

        if stop_reason == "tool_use" and not tool_calls:
            warnings.append(
                {
                    "kind": "empty_tool_use_block",
                    "field": "content[].tool_use",
                }
            )

        if stop_reason == "pause_turn":
            sentinel_id = f"call_pause_turn_{context.request_id}"
            tool_calls.append(
                {
                    "id": sentinel_id,
                    "type": "function",
                    "function": {
                        "name": _PAUSE_TURN_FUNCTION_NAME,
                        "arguments": "{}",
                    },
                }
            )
            warnings.append(
                {
                    "kind": "pause_turn",
                    "field": "stop_reason",
                    "to": "tool_calls",
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

        message: dict[str, Any] = {
            "role": "assistant",
            "content": content_text,
        }
        if reasoning_content is not None:
            message["reasoning_content"] = reasoning_content
        if tool_calls:
            message["tool_calls"] = tool_calls

        out: dict[str, Any] = {
            "id": payload.get("id", f"chatcmpl-{context.request_id}"),
            "object": "chat.completion",
            "created": int(time.time()),
            "model": payload.get("model", ""),
            "choices": [
                {
                    "index": 0,
                    "message": message,
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


def _translate_openai_tool_choice(
    tool_choice: Any,
    warnings: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Translate an OpenAI ``tool_choice`` value into Anthropic shape."""
    if isinstance(tool_choice, str):
        if tool_choice == "none":
            return {"type": "none"}
        if tool_choice == "auto":
            return None
        if tool_choice == "required":
            return {"type": "any"}
        warnings.append(
            {
                "kind": "invalid_tool_choice",
                "field": "tool_choice",
                "from": tool_choice,
            }
        )
        return None

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
    if choice_type == "function":
        function = as_object(choice_obj.get("function")) or {}
        name = str(function.get("name", "")).strip()
        if not name:
            warnings.append(
                {
                    "kind": "invalid_tool_choice",
                    "field": "tool_choice.function.name",
                }
            )
            return None
        return {"type": "tool", "name": name}
    if choice_type == "auto":
        return None
    if choice_type == "none":
        return {"type": "none"}
    if choice_type == "required":
        return {"type": "any"}
    warnings.append(
        {
            "kind": "invalid_tool_choice",
            "field": "tool_choice",
            "from": choice_type,
        }
    )
    return None
