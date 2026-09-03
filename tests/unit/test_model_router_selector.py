"""Selector prompt and internal concrete-dispatch contracts."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from eggpool.model_router.prompt import (
    compile_repair_prompt,
    compile_selector_prompt,
    parse_route_id,
    truncate_utf8,
)
from eggpool.model_router.registry import compile_model_router
from eggpool.model_router.selector import ModelRouterSelector
from eggpool.request.coordinator import PreparedProxyResponse
from eggpool.request.internal_dispatch import prepare_internal_concrete_request


def _router(**overrides: Any):
    values: dict[str, Any] = {
        "selector_model": "selector-model",
        "default_model": "model-default",
        "routes": {
            "hard": {"model": "model-hard", "description": "Hard queries."},
            "default": {
                "model": "model-default",
                "description": "General purpose queries.",
            },
            "fast": {"model": "model-fast", "description": "Fast queries."},
        },
    }
    values.update(overrides)
    from eggpool.model_router.config import ModelRouterConfig

    return compile_model_router("virtual", ModelRouterConfig.model_validate(values))


def _response(content: object) -> bytes:
    return json.dumps(
        {"choices": [{"message": {"role": "assistant", "content": content}}]}
    ).encode()


def test_selector_prompt_is_deterministic_and_hides_targets() -> None:
    router = _router()
    prompt = compile_selector_prompt(
        router,
        {
            "model": "client-model",
            "messages": [
                {"role": "system", "content": "  system\t instruction  "},
                {"role": "assistant", "content": "old answer"},
                {"role": "user", "content": "latest\r\nquestion"},
            ],
        },
    )

    assert prompt.static_prefix == (
        "model-router/v1|choose id;reply id only|0=General purpose queries."
        "|1=Fast queries.|2=Hard queries."
    )
    assert prompt.payload == {
        "model": "selector-model",
        "messages": [
            {"role": "system", "content": prompt.static_prefix},
            {
                "role": "user",
                "content": "system: system instruction\nuser: latest\nquestion",
            },
        ],
        "stream": False,
        "max_tokens": 16,
    }
    assert "model-hard" not in prompt.static_prefix
    assert "old answer" not in prompt.variable_text


def test_prompt_extracts_features_but_not_tools_or_tool_results() -> None:
    prompt = compile_selector_prompt(
        _router(),
        {
            "model": "client-model",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "inspect this"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,SECRET"},
                        },
                    ],
                },
                {"role": "tool", "content": "private tool result"},
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {"name": "secret_tool", "parameters": {"x": 1}},
                }
            ],
            "reasoning_effort": "high",
        },
    )

    assert prompt.variable_text == "user: inspect this\nfeatures: tools,image,reasoning"
    assert "secret_tool" not in prompt.variable_text
    assert "private tool result" not in prompt.variable_text
    assert "base64" not in prompt.variable_text


@pytest.mark.parametrize("surface", ["chat_completions", "messages", "responses"])
def test_prompt_reads_surface_specific_system_and_user_text(surface: str) -> None:
    if surface == "messages":
        payload: dict[str, Any] = {
            "model": "client-model",
            "system": "Anthropic system",
            "messages": [{"role": "user", "content": "Anthropic user"}],
        }
    elif surface == "responses":
        payload = {"model": "client-model", "input": "Responses user"}
    else:
        payload = {
            "model": "client-model",
            "messages": [
                {"role": "developer", "content": "Developer system"},
                {"role": "user", "content": "Chat user"},
            ],
        }

    prompt = compile_selector_prompt(
        _router(),
        payload,
        client_surface=surface,
    )

    if surface == "chat_completions":
        assert "system:" in prompt.variable_text
    assert prompt.variable_text.endswith(
        "Anthropic user"
        if surface == "messages"
        else "Responses user"
        if surface == "responses"
        else "Chat user"
    )


def test_utf8_truncation_preserves_valid_head_and_tail() -> None:
    value = "前半分 " * 40 + " actual-error-at-end"
    truncated = truncate_utf8(value, 128)

    assert len(truncated.encode("utf-8")) <= 128
    truncated.encode("utf-8").decode("utf-8")
    assert truncated.startswith("前半分")
    assert truncated.endswith("actual-error-at-end")


@pytest.mark.parametrize(
    "content, expected",
    [
        (" 1 ", "1"),
        ([{"type": "text", "text": "\n2\n"}], "2"),
    ],
)
def test_selector_output_accepts_only_exact_route_ids(
    content: object, expected: str
) -> None:
    assert parse_route_id(_response(content), _router()) == expected


@pytest.mark.parametrize(
    "content", ["model-hard", "Hard queries.", "x1", "1.", "0 1", "`1`"]
)
def test_selector_output_rejects_non_exact_language(content: str) -> None:
    assert parse_route_id(_response(content), _router()) is None


def test_selector_output_is_bounded_before_json_parsing() -> None:
    body = _response("1") + b" " * 20_000
    assert parse_route_id(body, _router()) is None


def test_internal_context_is_concrete_non_streaming_and_normalized() -> None:
    context = prepare_internal_concrete_request(
        {
            "model": "selector-model/local",
            "messages": [{"role": "user", "content": "classify"}],
            "stream": True,
        },
        model_id="selector-model",
        known_provider_ids={"local"},
        request_id="child-id",
    )

    assert context.request_id == "child-id"
    assert context.model_id == "selector-model"
    assert context.provider_id == "local"
    assert context.streaming is False
    assert context.provider_bound is not None
    assert context.provider_bound.provider_payload["stream"] is False
    assert context.provider_bound.provider_payload["model"] == "selector-model"


class _FakeCoordinator:
    def __init__(
        self,
        responses: list[bytes] | None = None,
        *,
        statuses: list[int] | None = None,
    ) -> None:
        self.responses = list(responses or [])
        self.statuses = list(statuses or [])
        self.contexts: list[Any] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.wait = False

    async def execute(self, context: Any) -> PreparedProxyResponse:
        self.contexts.append(context)
        self.started.set()
        if self.wait:
            await self.release.wait()
        if not self.responses:
            raise RuntimeError("no fake response")
        status_code = self.statuses.pop(0) if self.statuses else 200
        return PreparedProxyResponse(
            status_code=status_code,
            headers=[],
            body=self.responses.pop(0),
        )


@pytest.mark.asyncio
async def test_selector_valid_first_attempt_uses_coordinator_and_child_request() -> (
    None
):
    coordinator = _FakeCoordinator([_response("1")])
    selection = await ModelRouterSelector(coordinator).select(
        _router(),
        {"model": "client-model", "messages": [{"role": "user", "content": "x"}]},
    )

    assert selection.concrete_model == "model-fast"
    assert selection.route_label == "fast"
    assert selection.source == "selector"
    assert selection.selector_attempts == 1
    assert selection.selector_latency_ms is not None
    assert len(coordinator.contexts) == 1
    assert coordinator.contexts[0].request_id != "client-id"


@pytest.mark.asyncio
async def test_selector_repairs_once_then_accepts() -> None:
    coordinator = _FakeCoordinator([_response("not-a-route"), _response("2")])
    payload = {
        "model": "client-model",
        "messages": [
            {"role": "system", "content": "classify carefully"},
            {"role": "user", "content": "x"},
            {"role": "tool", "content": "private tool result"},
        ],
        "tools": [
            {
                "type": "function",
                "function": {"name": "secret_tool", "parameters": {"x": 1}},
            }
        ],
    }
    selection = await ModelRouterSelector(coordinator).select(
        _router(),
        payload,
    )

    assert selection.concrete_model == "model-hard"
    assert selection.source == "selector"
    assert selection.selector_attempts == 2
    repair_payload = coordinator.contexts[1].provider_bound.client_payload
    assert repair_payload["messages"] == [
        {
            "role": "system",
            "content": coordinator.contexts[0].provider_bound.client_payload[
                "messages"
            ][0]["content"],
        },
        {
            "role": "user",
            "content": "system: classify carefully\nuser: x\nfeatures: tools",
        },
        {"role": "user", "content": "invalid;reply only:0|1|2"},
    ]
    assert "not-a-route" not in json.dumps(repair_payload)
    assert "secret_tool" not in json.dumps(repair_payload)
    assert "private tool result" not in json.dumps(repair_payload)


@pytest.mark.asyncio
async def test_selector_repair_reuses_the_initial_bounded_semantic_context() -> None:
    coordinator = _FakeCoordinator([_response("invalid"), _response("0")])
    user_text = "前半分 " * 500 + " actual-request-at-end"
    selection = await ModelRouterSelector(coordinator).select(
        _router(max_input_bytes=128),
        {
            "model": "client-model",
            "messages": [{"role": "user", "content": user_text}],
        },
    )

    assert selection.source == "selector"
    initial = coordinator.contexts[0].provider_bound.client_payload
    repair = coordinator.contexts[1].provider_bound.client_payload
    assert repair["messages"][0] == initial["messages"][0]
    assert repair["messages"][1] == initial["messages"][1]
    assert repair["messages"][2]["content"] == "invalid;reply only:0|1|2"
    assert len(repair["messages"][1]["content"].encode()) <= 128
    assert "invalid" not in repair["messages"][1]["content"]


@pytest.mark.asyncio
async def test_selector_non_2xx_response_falls_back_without_repair() -> None:
    coordinator = _FakeCoordinator([b'{"error":"unavailable"}'], statuses=[503])
    selection = await ModelRouterSelector(coordinator).select(
        _router(),
        {"model": "client-model", "messages": [{"role": "user", "content": "x"}]},
    )

    assert selection.source == "default"
    assert selection.selector_attempts == 1
    assert selection.fallback_reason == "unavailable"
    assert selection.repair_attempted is False
    assert len(coordinator.contexts) == 1


@pytest.mark.asyncio
async def test_selector_non_2xx_repair_response_is_unavailable() -> None:
    coordinator = _FakeCoordinator(
        [_response("invalid"), b'{"error":"unavailable"}'],
        statuses=[200, 502],
    )
    selection = await ModelRouterSelector(coordinator).select(
        _router(),
        {"model": "client-model", "messages": [{"role": "user", "content": "x"}]},
    )

    assert selection.source == "default"
    assert selection.selector_attempts == 2
    assert selection.fallback_reason == "unavailable"
    assert selection.repair_attempted is True
    assert selection.repair_succeeded is False


@pytest.mark.asyncio
async def test_selector_invalid_repair_or_disabled_repair_falls_back() -> None:
    coordinator = _FakeCoordinator([_response("not-a-route"), _response("still bad")])
    selection = await ModelRouterSelector(coordinator).select(
        _router(),
        {"model": "client-model", "messages": [{"role": "user", "content": "x"}]},
    )
    assert selection.concrete_model == "model-default"
    assert selection.source == "default"
    assert selection.selector_attempts == 2
    assert selection.fallback_reason == "repair_failed"
    assert selection.repair_attempted is True
    assert selection.repair_succeeded is False

    no_repair = _FakeCoordinator([_response("still bad")])
    selection = await ModelRouterSelector(no_repair).select(
        _router(repair_attempts=0),
        {"model": "client-model", "messages": [{"role": "user", "content": "x"}]},
    )
    assert selection.source == "default"
    assert selection.selector_attempts == 1
    assert selection.fallback_reason == "invalid_output"


@pytest.mark.asyncio
async def test_selector_failures_and_timeout_fall_back_without_default_dispatch() -> (
    None
):
    coordinator = _FakeCoordinator()
    selection = await ModelRouterSelector(coordinator).select(
        _router(),
        {"model": "client-model", "messages": [{"role": "user", "content": "x"}]},
    )
    assert selection.source == "default"
    assert selection.selector_attempts == 1
    assert selection.fallback_reason == "unavailable"

    coordinator = _FakeCoordinator()
    coordinator.wait = True
    task = asyncio.create_task(
        ModelRouterSelector(coordinator).select(
            _router(selector_timeout_s=0.05),
            {"model": "client-model", "messages": [{"role": "user", "content": "x"}]},
        )
    )
    await coordinator.started.wait()
    selection = await task
    assert selection.source == "default"
    assert selection.selector_attempts == 1
    assert selection.fallback_reason == "timeout"
    assert len(coordinator.contexts) == 1


@pytest.mark.asyncio
async def test_parent_cancellation_propagates_and_does_not_fallback() -> None:
    coordinator = _FakeCoordinator()
    coordinator.wait = True
    task = asyncio.create_task(
        ModelRouterSelector(coordinator).select(
            _router(),
            {"model": "client-model", "messages": [{"role": "user", "content": "x"}]},
        )
    )
    await coordinator.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(coordinator.contexts) == 1


def test_repair_prompt_is_static_and_bounded() -> None:
    initial = compile_selector_prompt(
        _router(),
        {"model": "client-model", "messages": [{"role": "user", "content": "x"}]},
    )
    payload = compile_repair_prompt(_router(), initial)
    assert payload["messages"][2]["content"] == "invalid;reply only:0|1|2"
    assert payload["messages"][1]["content"] == initial.variable_text
    assert len(json.dumps(payload).encode()) < 1024
