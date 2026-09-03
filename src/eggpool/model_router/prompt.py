"""Deterministic, bounded prompt construction for model-router selection."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from eggpool.wire.ir import CanonicalRequest, canonical_request_from_mapping

if TYPE_CHECKING:
    from eggpool.model_router.registry import CompiledModelRouter

_HORIZONTAL_WHITESPACE = re.compile(r"[\t\f\v ]+")
_MAX_RESPONSE_BYTES = 16 * 1024
_TRUNCATION_MARKER = "\n[… ]\n"


@dataclass(frozen=True, slots=True)
class SelectorPrompt:
    """The complete internal Chat Completions-shaped selector payload."""

    payload: dict[str, Any]
    static_prefix: str
    variable_text: str


def normalize_selector_text(value: str) -> str:
    """Normalize transport whitespace without changing Unicode/code points.

    CRLF and CR become LF, horizontal ASCII whitespace collapses to one space,
    repeated blank lines are bounded to one, and the outer edges are trimmed.
    Identifiers, punctuation, and non-ASCII characters are otherwise kept
    unchanged.
    """
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    lines = [
        _HORIZONTAL_WHITESPACE.sub(" ", line).strip() for line in normalized.split("\n")
    ]
    result: list[str] = []
    previous_blank = False
    for line in lines:
        blank = not line
        if blank and previous_blank:
            continue
        result.append(line)
        previous_blank = blank
    return "\n".join(result).strip()


def truncate_utf8(value: str, max_bytes: int) -> str:
    """Truncate *value* on UTF-8 boundaries while retaining head and tail."""
    if max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    marker = _TRUNCATION_MARKER.encode("utf-8")
    if max_bytes <= len(marker):
        return encoded[:max_bytes].decode("utf-8", errors="ignore")
    available = max_bytes - len(marker)
    head_budget = (available * 3) // 4
    tail_budget = available - head_budget
    head = encoded[:head_budget].decode("utf-8", errors="ignore")
    tail = encoded[-tail_budget:].decode("utf-8", errors="ignore")
    result = f"{head}{_TRUNCATION_MARKER}{tail}"
    # Decoding partial code points can only reduce the size, but retain the
    # bound as an invariant if the marker or allocation changes later.
    while len(result.encode("utf-8")) > max_bytes:
        result = result[:-1]
    return result


def _message_text(request: CanonicalRequest, role: str) -> list[str]:
    return [
        message.text()
        for message in request.messages
        if message.role == role and message.text()
    ]


def _feature_flags(request: CanonicalRequest) -> tuple[str, ...]:
    flags: list[str] = []
    if request.tools:
        flags.append("tools")
    kinds = {block.kind for message in request.messages for block in message.content}
    if "image" in kinds:
        flags.append("image")
    if "document" in kinds:
        flags.append("pdf")
    if "audio" in kinds:
        flags.append("audio")
    if request.reasoning.requested is not None:
        flags.append("reasoning")
    return tuple(flags)


def build_semantic_view(request: CanonicalRequest) -> str:
    """Build the bounded-independent semantic text before byte truncation."""
    system = _message_text(request, "system") + _message_text(request, "developer")
    users = _message_text(request, "user")
    assistants = _message_text(request, "assistant")
    parts: list[str] = []
    if system:
        system_text = normalize_selector_text("\n".join(system))
        parts.append(f"system: {system_text}")
    if users:
        parts.append(f"user: {normalize_selector_text(users[-1])}")
    elif assistants:
        # A minimal assistant fallback is useful for surfaces that carry no
        # user text, but assistant history is never included when user text
        # exists.
        parts.append(f"context: {normalize_selector_text(assistants[-1])}")
    flags = _feature_flags(request)
    if flags:
        parts.append(f"features: {','.join(flags)}")
    return "\n".join(parts)


def compile_selector_prompt(
    router: CompiledModelRouter,
    payload: Mapping[str, Any],
    *,
    client_surface: str = "chat_completions",
    protocol: str | None = None,
) -> SelectorPrompt:
    """Compile one deterministic selector request without I/O or routing."""
    if client_surface not in {"chat_completions", "messages", "responses"}:
        raise ValueError(f"unsupported selector client surface: {client_surface}")
    canonical = canonical_request_from_mapping(
        payload,
        client_surface=client_surface,  # type: ignore[arg-type]
        protocol=protocol,
    )
    static_prefix = router.static_policy.decode("utf-8")
    variable_text = truncate_utf8(
        build_semantic_view(canonical),
        router.max_input_bytes,
    )
    messages: list[dict[str, str]] = [
        {"role": "system", "content": static_prefix},
    ]
    if variable_text:
        messages.append({"role": "user", "content": variable_text})
    selector_payload: dict[str, Any] = {
        "model": router.selector_model,
        "messages": messages,
        "stream": False,
        "max_tokens": 16,
    }
    return SelectorPrompt(
        payload=selector_payload,
        static_prefix=static_prefix,
        variable_text=variable_text,
    )


def compile_repair_prompt(router: CompiledModelRouter) -> dict[str, Any]:
    """Build the fixed, bounded repair request for an invalid answer."""
    route_ids = "|".join(router.route_by_id)
    return {
        "model": router.selector_model,
        "messages": [
            {"role": "system", "content": router.static_policy.decode("utf-8")},
            {"role": "user", "content": f"invalid;reply only:{route_ids}"},
        ],
        "stream": False,
        "max_tokens": 16,
    }


def parse_route_id(
    body: bytes | None,
    router: CompiledModelRouter,
    *,
    max_response_bytes: int = _MAX_RESPONSE_BYTES,
) -> str | None:
    """Return an exact route ID from a bounded OpenAI-chat response body."""
    if body is None or len(body) > max_response_bytes:
        return None
    try:
        from eggpool.jsonx import loads

        response: object = loads(body)
    except (TypeError, ValueError):
        return None
    if not isinstance(response, Mapping):
        return None
    response_mapping = cast("Mapping[str, Any]", response)
    choices_obj = response_mapping.get("choices")
    if not isinstance(choices_obj, list):
        return None
    choices = cast("list[object]", choices_obj)
    if len(choices) != 1:
        return None
    choice: object = choices[0]
    if not isinstance(choice, Mapping):
        return None
    choice_mapping = cast("Mapping[str, Any]", choice)
    message = choice_mapping.get("message")
    if not isinstance(message, Mapping):
        return None
    message_mapping = cast("Mapping[str, Any]", message)
    content = message_mapping.get("content")
    text: str | None
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        content_blocks = cast("list[object]", content)
        chunks: list[str] = []
        for block in content_blocks:
            if not isinstance(block, Mapping):
                return None
            block_mapping = cast("Mapping[str, Any]", block)
            if block_mapping.get("type") != "text":
                return None
            block_text = block_mapping.get("text")
            if not isinstance(block_text, str):
                return None
            chunks.append(block_text)
        text = "".join(chunks)
    else:
        text = None
    if text is None:
        return None
    candidate = text.strip()
    if candidate not in router.route_by_id:
        return None
    return candidate


__all__ = [
    "SelectorPrompt",
    "build_semantic_view",
    "compile_repair_prompt",
    "compile_selector_prompt",
    "normalize_selector_text",
    "parse_route_id",
    "truncate_utf8",
]
