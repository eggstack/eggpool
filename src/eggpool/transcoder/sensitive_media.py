"""Helpers for inspecting request payloads for cross-protocol content.

The provider-sensitive media detector is the single source of truth for
whether a request payload contains media or tool-result content whose
cross-protocol translation depends on the *selected* provider's
multimodal capability row.  The coordinator uses this signal to
force a final recompute after provider selection even when the
preflight translation is otherwise reusable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from eggpool.jsonx import loads as jsonx_loads
from eggpool.transcoder.json_helpers import JsonObject, iter_objects

if TYPE_CHECKING:
    from collections.abc import Iterable

_PROVIDER_SENSITIVE_PART_TYPES: frozenset[str] = frozenset(
    {
        "image_url",
        "image",
        "input_image",
        "input_audio",
        "audio",
        "file",
        "document",
        "pdf",
    }
)


def _iter_nested_objects(value: object) -> Iterable[JsonObject]:
    """Yield all mappings nested in a tool argument value."""
    if isinstance(value, dict):
        obj = cast("dict[str, object]", value)
        yield cast("JsonObject", obj)
        for child in list(obj.values()):
            yield from _iter_nested_objects(child)
    elif isinstance(value, list):
        for child in cast("list[object]", value):
            yield from _iter_nested_objects(child)


def _iter_content_blocks(payload: object) -> Iterable[JsonObject]:
    """Yield content-block mappings from message or tool-result shapes."""
    if not isinstance(payload, dict):
        return
    as_dict = cast("dict[str, Any]", payload)
    messages_obj = as_dict.get("messages")
    if isinstance(messages_obj, list):
        messages_list = cast("list[object]", messages_obj)
        for message in messages_list:
            if not isinstance(message, dict):
                continue
            message_obj = cast("dict[str, Any]", message)
            content_obj = message_obj.get("content")
            if isinstance(content_obj, list):
                for part in iter_objects(content_obj):
                    yield part
            elif isinstance(content_obj, dict):
                yield cast("JsonObject", content_obj)
            tool_calls_obj = message_obj.get("tool_calls")
            if isinstance(tool_calls_obj, list):
                tool_calls_list = cast("list[object]", tool_calls_obj)
                for tool_call in tool_calls_list:
                    if not isinstance(tool_call, dict):
                        continue
                    tool_call_obj = cast("dict[str, object]", tool_call)
                    inner_obj = tool_call_obj.get("content")
                    if isinstance(inner_obj, list):
                        for part in iter_objects(inner_obj):
                            yield part
                    function_obj = tool_call_obj.get("function")
                    if isinstance(function_obj, dict):
                        function_dict = cast("dict[str, object]", function_obj)
                        arguments_obj = function_dict.get("arguments")
                        if isinstance(arguments_obj, str):
                            try:
                                arguments_obj = jsonx_loads(arguments_obj)
                            except (TypeError, ValueError):
                                arguments_obj = None
                        yield from _iter_nested_objects(arguments_obj)
                    yield from _iter_nested_objects(tool_call_obj.get("input"))
    system_obj = as_dict.get("system")
    if isinstance(system_obj, list):
        for part in iter_objects(system_obj):
            yield part


def _has_provider_sensitive_part(block: JsonObject) -> bool:
    block_type = block.get("type")
    if isinstance(block_type, str) and block_type in _PROVIDER_SENSITIVE_PART_TYPES:
        return True
    # Anthropic image and document blocks carry their source under
    # ``source`` rather than a ``type`` discriminator.
    return "source" in block and (
        "image" in block or "document" in block or "type" not in block
    )


def _tool_result_has_sensitive_content(payload: object) -> bool:
    """Return True when any tool-result block carries non-text content."""
    if not isinstance(payload, dict):
        return False
    as_dict = cast("dict[str, Any]", payload)
    messages_obj = as_dict.get("messages")
    if not isinstance(messages_obj, list):
        return False
    messages_list = cast("list[object]", messages_obj)
    for message in messages_list:
        if not isinstance(message, dict):
            continue
        message_obj = cast("dict[str, Any]", message)
        content_obj = message_obj.get("content")
        if isinstance(content_obj, list):
            for part in iter_objects(content_obj):
                if part.get("type") == "tool_result":
                    inner_obj = part.get("content")
                    if isinstance(inner_obj, list):
                        for sub in iter_objects(inner_obj):
                            if _has_provider_sensitive_part(sub):
                                return True
    return False


def request_has_provider_sensitive_media(payload: object) -> bool:
    """Return True if the request payload contains media/tool-result content.

    ``True`` means the preflight translation cannot be safely reused
    across providers with different multimodal capabilities. The
    coordinator must force a final recompute after provider selection
    so the selected provider's source form/file/audio support is
    applied at translation time.
    """
    if not isinstance(payload, dict):
        return False
    as_dict = cast("dict[str, Any]", payload)
    for block in _iter_content_blocks(as_dict):
        if _has_provider_sensitive_part(block):
            return True
    return bool(_tool_result_has_sensitive_content(as_dict))
