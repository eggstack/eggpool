# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false

"""Small wire-independent request, response, and event representations.

The IR deliberately models the semantic subset EggPool can replay when a
request changes wire surface.  It is not a vendor schema and it is not a
place to retain unknown provider fields.  Surface codecs own conversion to
and from their concrete JSON/SSE grammar.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias, cast

CanonicalSurface: TypeAlias = Literal["chat_completions", "responses", "messages"]
CanonicalRole: TypeAlias = Literal["system", "developer", "user", "assistant", "tool"]
CanonicalBlockType: TypeAlias = Literal[
    "text",
    "image",
    "document",
    "audio",
    "reasoning",
    "tool_call",
    "tool_result",
    "refusal",
]
CanonicalEventType: TypeAlias = Literal[
    "response_start",
    "content_start",
    "text_delta",
    "reasoning_start",
    "reasoning_delta",
    "reasoning_stop",
    "tool_call_start",
    "tool_call_arguments_delta",
    "tool_call_stop",
    "content_stop",
    "usage",
    "response_complete",
    "response_incomplete",
    "error",
]


@dataclass(frozen=True, slots=True)
class CanonicalUsage:
    """Normalized token counters shared by all response codecs."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0

    def to_dict(self) -> dict[str, int]:
        """Serialize counters for the existing accounting path."""
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "cache_read_tokens": self.cache_read_tokens,
        }


@dataclass(frozen=True, slots=True)
class ReasoningIntent:
    """Client reasoning preference, before a target surface is selected.

    An effort label is intentionally kept as an effort label.  No codec may
    infer a token budget from it unless the selected target capability
    supplies an explicit mapping.
    """

    requested: bool | None = None
    mode: Literal["unspecified", "effort", "fixed_budget", "adaptive", "toggle"] = (
        "unspecified"
    )
    effort: str | None = None
    budget_tokens: int | None = None

    def __post_init__(self) -> None:
        if self.requested is False and (
            self.effort is not None or self.budget_tokens is not None
        ):
            raise ValueError("Disabled reasoning cannot carry effort or budget")
        if self.budget_tokens is not None:
            if isinstance(self.budget_tokens, bool) or self.budget_tokens < 1:
                raise ValueError("Reasoning budget must be a positive integer")
            if self.mode not in {"fixed_budget", "unspecified"}:
                raise ValueError("Numeric reasoning budget requires fixed_budget mode")
        if self.effort is not None and not self.effort.strip():
            raise ValueError("Reasoning effort must be non-empty when supplied")

    @classmethod
    def from_openai_effort(cls, effort: str) -> ReasoningIntent:
        """Create an effort intent without mapping it to a budget.

        ``none`` is an OpenAI effort value, so it remains an effort intent.
        A target may only reinterpret it as a binary disable when an
        explicit verified mapping says that is faithful.
        """
        return cls(requested=True, mode="effort", effort=effort)

    @classmethod
    def disabled(cls) -> ReasoningIntent:
        return cls(requested=False, mode="toggle")

    @classmethod
    def fixed(cls, budget_tokens: int) -> ReasoningIntent:
        return cls(requested=True, mode="fixed_budget", budget_tokens=budget_tokens)

    @classmethod
    def adaptive(cls) -> ReasoningIntent:
        return cls(requested=True, mode="adaptive")


@dataclass(frozen=True, slots=True)
class CanonicalContentBlock:
    """One portable content or tool block."""

    kind: CanonicalBlockType
    text: str | None = None
    media_type: str | None = None
    data: str | None = None
    uri: str | None = None
    call_id: str | None = None
    name: str | None = None
    arguments: str | None = None
    tool_input: Mapping[str, Any] | None = None
    is_error: bool = False
    signature: str | None = None


@dataclass(frozen=True, slots=True)
class CanonicalMessage:
    """Chronological message with semantically typed blocks."""

    role: CanonicalRole
    content: tuple[CanonicalContentBlock, ...] = ()
    tool_call_id: str | None = None
    name: str | None = None
    refusal: str | None = None

    def text(self) -> str:
        """Return ordinary text content joined in source order."""
        return "".join(
            block.text or "" for block in self.content if block.kind == "text"
        )


@dataclass(frozen=True, slots=True)
class CanonicalTool:
    """Portable function tool declaration."""

    name: str
    description: str | None = None
    parameters: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CanonicalToolChoice:
    """Portable subset of automatic, required, and named tool choice."""

    mode: Literal["auto", "required", "none", "function"]
    function_name: str | None = None


@dataclass(frozen=True, slots=True)
class CanonicalRequest:
    """Minimal replayable request intent shared by surface codecs."""

    model: str
    messages: tuple[CanonicalMessage, ...] = ()
    stream: bool = False
    max_output_tokens: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    stop: tuple[str, ...] | None = None
    tools: tuple[CanonicalTool, ...] = ()
    tool_choice: CanonicalToolChoice | None = None
    response_format: Mapping[str, Any] | None = None
    reasoning: ReasoningIntent = field(default_factory=ReasoningIntent)
    client_surface: CanonicalSurface = "chat_completions"
    metadata: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class CanonicalOutputBlock:
    """One ordered response output block."""

    kind: Literal["text", "reasoning", "tool_call", "refusal"]
    text: str | None = None
    call_id: str | None = None
    name: str | None = None
    arguments: str | None = None


@dataclass(frozen=True, slots=True)
class CanonicalResponse:
    """Common non-streaming response semantics."""

    model: str | None = None
    output: tuple[CanonicalOutputBlock, ...] = ()
    finish_reason: str | None = None
    usage: CanonicalUsage | None = None
    request_id: str | None = None


@dataclass(frozen=True, slots=True)
class CanonicalEvent:
    """Bounded incremental event vocabulary for streaming codecs."""

    type: CanonicalEventType
    response_id: str | None = None
    model: str | None = None
    index: int | None = None
    delta: str | None = None
    call_id: str | None = None
    name: str | None = None
    arguments: str | None = None
    finish_reason: str | None = None
    usage: CanonicalUsage | None = None
    error_type: str | None = None
    error_message: str | None = None

    @property
    def kind(self) -> CanonicalEventType:
        """Alias used by callers that call event categories ``kind``."""
        return self.type


def reasoning_intent_from_mapping(payload: Mapping[str, Any]) -> ReasoningIntent:
    """Extract reasoning intent without target-specific interpretation."""
    effort = payload.get("reasoning_effort")
    if isinstance(effort, str):
        return ReasoningIntent.from_openai_effort(effort)

    reasoning = payload.get("reasoning")
    if isinstance(reasoning, Mapping):
        effort_value = reasoning.get("effort")
        if isinstance(effort_value, str):
            return ReasoningIntent.from_openai_effort(effort_value)
        enabled = reasoning.get("enabled")
        if isinstance(enabled, bool):
            return ReasoningIntent(requested=enabled, mode="toggle")
    elif isinstance(reasoning, bool):
        return ReasoningIntent(requested=reasoning, mode="toggle")

    thinking = payload.get("thinking")
    if isinstance(thinking, Mapping):
        thinking_type = thinking.get("type")
        if isinstance(thinking_type, str) and thinking_type in {"disabled", "none"}:
            return ReasoningIntent.disabled()
        budget = thinking.get("budget_tokens")
        if isinstance(budget, int) and not isinstance(budget, bool):
            return ReasoningIntent.fixed(budget)
        if thinking_type == "adaptive":
            return ReasoningIntent.adaptive()
        if thinking_type == "enabled":
            return ReasoningIntent(requested=True, mode="toggle")

    budget_value = payload.get("thinking_budget")
    if isinstance(budget_value, int) and not isinstance(budget_value, bool):
        return ReasoningIntent.fixed(budget_value)
    return ReasoningIntent()


def canonical_request_from_mapping(
    payload: Mapping[str, Any],
    *,
    client_surface: CanonicalSurface = "chat_completions",
    protocol: str | None = None,
) -> CanonicalRequest:
    """Decode the portable subset of Chat, Messages, Responses, or Gemini.

    Unsupported vendor fields are intentionally not copied.  Existing
    surface transcoders remain responsible for their established loss-policy
    diagnostics; this decoder is the semantic boundary used for future
    alternate-surface encoders and reasoning ownership.
    """
    selected_protocol = protocol or _protocol_for_surface(client_surface)
    messages = _decode_messages(payload, selected_protocol)
    if not messages and isinstance(payload.get("input"), str):
        messages = (
            CanonicalMessage(
                role="user",
                content=(CanonicalContentBlock("text", text=str(payload["input"])),),
            ),
        )
    model = payload.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("Canonical request requires a non-empty model")
    stop = _stop_values(payload.get("stop", payload.get("stop_sequences")))
    max_output = _first_int(
        payload.get("max_output_tokens"),
        payload.get("max_completion_tokens"),
        payload.get("max_tokens"),
    )
    return CanonicalRequest(
        model=model,
        messages=messages,
        stream=bool(payload.get("stream", False)),
        max_output_tokens=max_output,
        temperature=_number(payload.get("temperature")),
        top_p=_number(payload.get("top_p")),
        stop=stop,
        tools=_decode_tools(payload.get("tools")),
        tool_choice=_decode_tool_choice(payload.get("tool_choice")),
        response_format=_mapping_or_none(payload.get("response_format")),
        reasoning=reasoning_intent_from_mapping(payload),
        client_surface=client_surface,
    )


def canonical_request_to_mapping(
    request: CanonicalRequest,
    *,
    surface: CanonicalSurface,
) -> dict[str, object]:
    """Encode the portable request subset for a concrete client surface."""
    if surface == "messages":
        return _encode_anthropic_request(request)
    if surface == "responses":
        return _encode_responses_request(request)
    return _encode_openai_request(request)


def _protocol_for_surface(surface: CanonicalSurface) -> str:
    return "anthropic" if surface == "messages" else "openai"


def _decode_messages(
    payload: Mapping[str, Any], protocol: str
) -> tuple[CanonicalMessage, ...]:
    result: list[CanonicalMessage] = []
    if protocol == "anthropic":
        system = payload.get("system")
        if system is not None:
            result.append(CanonicalMessage("system", _decode_content(system, "system")))
    raw_messages = payload.get("messages")
    if not isinstance(raw_messages, Sequence) or isinstance(raw_messages, (str, bytes)):
        raw_messages = payload.get("contents")
    if not isinstance(raw_messages, Sequence) or isinstance(raw_messages, (str, bytes)):
        return tuple(result)
    for raw in raw_messages:
        if not isinstance(raw, Mapping):
            continue
        role_value = raw.get("role", "user")
        role = _normalize_role(role_value, protocol)
        if role is None:
            continue
        content_value = raw.get("content", raw.get("parts", ""))
        blocks = list(_decode_content(content_value, role))
        if protocol == "openai":
            tool_calls = raw.get("tool_calls")
            if isinstance(tool_calls, Sequence) and not isinstance(
                tool_calls, (str, bytes)
            ):
                for raw_call in tool_calls:
                    if not isinstance(raw_call, Mapping):
                        continue
                    function = _mapping_or_none(raw_call.get("function")) or raw_call
                    blocks.append(
                        CanonicalContentBlock(
                            "tool_call",
                            call_id=_string_or_none(raw_call.get("id")),
                            name=_string_or_none(function.get("name")),
                            arguments=_string_or_none(function.get("arguments")),
                        )
                    )
        result.append(
            CanonicalMessage(
                role=role,
                content=tuple(blocks),
                tool_call_id=_string_or_none(raw.get("tool_call_id")),
                name=_string_or_none(raw.get("name")),
                refusal=_string_or_none(raw.get("refusal")),
            )
        )
    return tuple(result)


def _normalize_role(value: object, protocol: str) -> CanonicalRole | None:
    if not isinstance(value, str):
        return None
    if value == "developer" and protocol == "anthropic":
        return "system"
    if value in {"system", "developer", "user", "assistant", "tool"}:
        return cast("CanonicalRole", value)
    if value == "model" and protocol == "gemini":
        return "assistant"
    return None


def _decode_content(value: Any, role: str) -> tuple[CanonicalContentBlock, ...]:
    if isinstance(value, str):
        return (CanonicalContentBlock("text", text=value),)
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return (
            CanonicalContentBlock("text", text="" if value is None else str(value)),
        )
    blocks: list[CanonicalContentBlock] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            continue
        kind = raw.get("type")
        if isinstance(kind, str) and kind in {"text", "input_text", "output_text"}:
            blocks.append(CanonicalContentBlock("text", text=str(raw.get("text", ""))))
        elif kind == "image_url":
            image = _mapping_or_none(raw.get("image_url")) or {}
            url = _string_or_none(image.get("url"))
            blocks.append(CanonicalContentBlock("image", uri=url))
        elif isinstance(kind, str) and kind in {"image", "input_image"}:
            source = _mapping_or_none(raw.get("source")) or raw
            source_type = source.get("type")
            blocks.append(
                CanonicalContentBlock(
                    "image",
                    media_type=_string_or_none(source.get("media_type")),
                    data=_string_or_none(source.get("data"))
                    if source_type == "base64"
                    else None,
                    uri=_string_or_none(source.get("url")),
                )
            )
        elif isinstance(kind, str) and kind in {"file", "document", "input_file"}:
            source = _mapping_or_none(raw.get("source")) or raw
            blocks.append(
                CanonicalContentBlock(
                    "document",
                    media_type=_string_or_none(source.get("media_type")),
                    data=_string_or_none(source.get("data")),
                    uri=_string_or_none(source.get("url")),
                )
            )
        elif isinstance(kind, str) and kind in {"input_audio", "audio"}:
            blocks.append(
                CanonicalContentBlock("audio", data=_string_or_none(raw.get("data")))
            )
        elif isinstance(kind, str) and kind in {
            "thinking",
            "reasoning",
            "reasoning_content",
        }:
            blocks.append(
                CanonicalContentBlock(
                    "reasoning",
                    text=_string_or_none(raw.get("thinking", raw.get("text"))),
                    signature=_string_or_none(raw.get("signature")),
                )
            )
        elif kind == "tool_use":
            blocks.append(
                CanonicalContentBlock(
                    "tool_call",
                    call_id=_string_or_none(raw.get("id")),
                    name=_string_or_none(raw.get("name")),
                    tool_input=_mapping_or_none(raw.get("input")),
                )
            )
        elif kind == "tool_result":
            result = raw.get("content", "")
            blocks.append(
                CanonicalContentBlock(
                    "tool_result",
                    text=_content_text(result),
                    call_id=_string_or_none(raw.get("tool_use_id")),
                    is_error=raw.get("is_error") is True,
                )
            )
        elif kind == "refusal" or (role == "assistant" and "refusal" in raw):
            blocks.append(
                CanonicalContentBlock(
                    "refusal", text=_string_or_none(raw.get("refusal", raw.get("text")))
                )
            )
    return tuple(blocks)


def _decode_tools(value: Any) -> tuple[CanonicalTool, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    tools: list[CanonicalTool] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            continue
        function = _mapping_or_none(raw.get("function")) or raw
        name = _string_or_none(function.get("name"))
        parameters = _mapping_or_none(
            function.get("parameters", function.get("input_schema"))
        )
        if name:
            tools.append(
                CanonicalTool(
                    name, _string_or_none(function.get("description")), parameters or {}
                )
            )
    return tuple(tools)


def _decode_tool_choice(value: object) -> CanonicalToolChoice | None:
    if isinstance(value, str) and value in {"auto", "required", "none"}:
        return CanonicalToolChoice(cast("Literal['auto', 'required', 'none']", value))
    if isinstance(value, Mapping):
        if value.get("type") == "any":
            return CanonicalToolChoice("required")
        function = _mapping_or_none(value.get("function")) or value
        name = _string_or_none(function.get("name"))
        if name:
            return CanonicalToolChoice("function", name)
    return None


def _encode_openai_request(request: CanonicalRequest) -> dict[str, object]:
    out: dict[str, object] = {"model": request.model, "messages": []}
    messages = cast("list[dict[str, object]]", out["messages"])
    for message in request.messages:
        item: dict[str, object] = {
            "role": message.role,
            "content": _encode_openai_content(message.content),
        }
        if message.tool_call_id is not None:
            item["tool_call_id"] = message.tool_call_id
        calls = [block for block in message.content if block.kind == "tool_call"]
        if calls:
            item["tool_calls"] = [
                {
                    "id": block.call_id or "",
                    "type": "function",
                    "function": {
                        "name": block.name or "",
                        "arguments": block.arguments or "",
                    },
                }
                for block in calls
            ]
        if message.refusal is not None:
            item["refusal"] = message.refusal
        messages.append(item)
    _add_common_request_fields(out, request, max_key="max_completion_tokens")
    if request.tools:
        out["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": dict(tool.parameters),
                },
            }
            for tool in request.tools
        ]
    if request.tool_choice is not None:
        out["tool_choice"] = _encode_tool_choice(request.tool_choice)
    return out


def _encode_anthropic_request(request: CanonicalRequest) -> dict[str, object]:
    out: dict[str, object] = {"model": request.model, "messages": []}
    messages = cast("list[dict[str, object]]", out["messages"])
    for message in request.messages:
        if message.role == "system":
            out["system"] = _encode_anthropic_content(message.content)
            continue
        item: dict[str, object] = {
            "role": "user" if message.role == "tool" else message.role,
            "content": _encode_anthropic_content(message.content),
        }
        messages.append(item)
    _add_common_request_fields(out, request, max_key="max_tokens")
    if request.tools:
        out["tools"] = [
            {
                "name": tool.name,
                "description": tool.description or "",
                "input_schema": dict(tool.parameters),
            }
            for tool in request.tools
        ]
    if request.tool_choice is not None:
        out["tool_choice"] = _encode_anthropic_tool_choice(request.tool_choice)
    if request.reasoning.requested is True:
        if request.reasoning.mode == "adaptive":
            out["thinking"] = {"type": "adaptive"}
        elif request.reasoning.budget_tokens is not None:
            out["thinking"] = {
                "type": "enabled",
                "budget_tokens": request.reasoning.budget_tokens,
            }
    return out


def _encode_responses_request(request: CanonicalRequest) -> dict[str, object]:
    out: dict[str, object] = {
        "model": request.model,
        "stream": request.stream,
        "store": False,
    }
    system = next(
        (message for message in request.messages if message.role == "system"), None
    )
    if system is not None:
        out["instructions"] = system.text()
    input_items: list[dict[str, object]] = []
    for message in request.messages:
        if message.role == "system":
            continue
        input_items.append(
            {"role": message.role, "content": _encode_openai_content(message.content)}
        )
    out["input"] = input_items
    _add_common_request_fields(out, request, max_key="max_output_tokens")
    if request.tools:
        out["tools"] = [
            {
                "type": "function",
                "name": tool.name,
                "description": tool.description or "",
                "parameters": dict(tool.parameters),
                "strict": False,
            }
            for tool in request.tools
        ]
    return out


def _add_common_request_fields(
    out: dict[str, object], request: CanonicalRequest, *, max_key: str
) -> None:
    out["stream"] = request.stream
    if request.max_output_tokens is not None:
        out[max_key] = request.max_output_tokens
    if request.temperature is not None:
        out["temperature"] = request.temperature
    if request.top_p is not None:
        out["top_p"] = request.top_p
    if request.stop is not None:
        out["stop"] = request.stop[0] if len(request.stop) == 1 else list(request.stop)
    if request.response_format is not None:
        out["response_format"] = dict(request.response_format)


def _encode_openai_content(content: Sequence[CanonicalContentBlock]) -> object:
    if len(content) == 1 and content[0].kind == "text":
        return content[0].text or ""
    result: list[dict[str, object]] = []
    for block in content:
        if block.kind == "text":
            result.append({"type": "text", "text": block.text or ""})
        elif block.kind == "image":
            result.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": block.uri
                        or (
                            "data:"
                            f"{block.media_type or 'application/octet-stream'};"
                            f"base64,{block.data}"
                            if block.data
                            else ""
                        )
                    },
                }
            )
        elif block.kind == "reasoning":
            result.append({"type": "reasoning_content", "text": block.text or ""})
        elif block.kind == "refusal":
            result.append({"type": "refusal", "refusal": block.text or ""})
    return result


def _encode_anthropic_content(content: Sequence[CanonicalContentBlock]) -> object:
    if len(content) == 1 and content[0].kind == "text":
        return content[0].text or ""
    result: list[dict[str, object]] = []
    for block in content:
        if block.kind == "text":
            result.append({"type": "text", "text": block.text or ""})
        elif block.kind == "image":
            if block.data is not None:
                result.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": block.media_type
                            or "application/octet-stream",
                            "data": block.data,
                        },
                    }
                )
            elif block.uri is not None:
                result.append(
                    {"type": "image", "source": {"type": "url", "url": block.uri}}
                )
        elif block.kind == "reasoning":
            result.append({"type": "thinking", "thinking": block.text or ""})
        elif block.kind == "tool_call":
            result.append(
                {
                    "type": "tool_use",
                    "id": block.call_id or "",
                    "name": block.name or "",
                    "input": dict(block.tool_input or {}),
                }
            )
        elif block.kind == "tool_result":
            result.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.call_id or "",
                    "content": block.text or "",
                    "is_error": block.is_error,
                }
            )
    return result


def _encode_tool_choice(choice: CanonicalToolChoice) -> object:
    if choice.mode == "function":
        return {"type": "function", "function": {"name": choice.function_name or ""}}
    return choice.mode


def _encode_anthropic_tool_choice(choice: CanonicalToolChoice) -> object:
    if choice.mode == "function":
        return {"type": "tool", "name": choice.function_name or ""}
    return {"type": "any" if choice.mode == "required" else choice.mode}


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return "".join(
            str(item.get("text", ""))
            for item in value
            if isinstance(item, Mapping) and item.get("type") == "text"
        )
    return "" if value is None else str(value)


def _mapping_or_none(value: Any) -> Mapping[str, Any] | None:
    return cast("Mapping[str, Any]", value) if isinstance(value, Mapping) else None


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _number(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _first_int(*values: object) -> int | None:
    for value in values:
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _stop_values(value: object) -> tuple[str, ...] | None:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        values = tuple(item for item in value if isinstance(item, str))
        return values or None
    return None
