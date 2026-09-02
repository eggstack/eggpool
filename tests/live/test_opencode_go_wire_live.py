"""Opt-in live OpenCode Go wire-surface acceptance.

The suite deliberately uses the public EggPool ASGI endpoints and a real
OpenCode Go HTTP client. It records only :class:`OutboundObservation` values;
credentials and raw request/response bodies never enter test diagnostics.

Run manually with ``EGGPOOL_E2E_OPENCODE_GO_API_KEY`` set:

    uv run pytest tests/live/test_opencode_go_wire_live.py -m live_opencode_go -v
"""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import pytest_asyncio

from tests.helpers.real_runtime import (
    ModelSpec,
    ProviderSpec,
    RuntimeAppSpec,
    build_runtime_app,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from fastapi import FastAPI

pytestmark = [
    pytest.mark.live,
    pytest.mark.live_provider,
    pytest.mark.live_opencode_go,
    pytest.mark.skipif(
        not os.environ.get("EGGPOOL_E2E_OPENCODE_GO_API_KEY"),
        reason="EGGPOOL_E2E_OPENCODE_GO_API_KEY is not set",
    ),
]

_OPENCODE_GO_BASE = "https://opencode.ai/zen/go/v1"
_SURFACE_CONFIG = {
    "openai_chat_completions": {
        "path_template": "/chat/completions",
        "priority": 100,
        "auth": {"mode": "bearer"},
    },
    "openai_responses": {
        "path_template": "/responses",
        "priority": 90,
        "auth": {"mode": "bearer"},
    },
    "anthropic_messages": {
        "path_template": "/messages",
        "priority": 100,
        "auth": {"mode": "api_key", "header": "x-api-key"},
    },
}

_MODEL_SURFACES = {
    "muse-spark-1.2-contributor": "openai_responses",
    "gpt-5.6-luna": "openai_responses",
    "minimax-m3": "anthropic_messages",
    "mimo-v2.5": "openai_chat_completions",
}
_MODEL_PROTOCOLS = {
    "muse-spark-1.2-contributor": "openai",
    "gpt-5.6-luna": "openai",
    "minimax-m3": "anthropic",
    "mimo-v2.5": "openai",
}


def _thinking_capability(efforts: list[str]) -> dict[str, Any]:
    return {
        "thinking": {
            "status": "supported",
            "source": "provider_catalog",
            "native_protocols": ["openai"],
            "supported_efforts": efforts,
            "effort_to_budget_tokens": {
                effort: 1024 if effort in {"minimal", "low"} else 4096
                for effort in efforts
            },
        }
    }


def _live_spec(observer: list[Any]) -> RuntimeAppSpec:
    models = tuple(
        ModelSpec(
            model_id=model_id,
            protocol=_MODEL_PROTOCOLS[model_id],
            capabilities=(
                _thinking_capability(["minimal", "low", "medium", "high", "xhigh"])
                if model_id == "muse-spark-1.2-contributor"
                else _thinking_capability(["low", "medium", "high"])
                if model_id == "mimo-v2.5"
                else {},
            ),
        )
        for model_id in _MODEL_SURFACES
    )
    return RuntimeAppSpec(
        account_names=("live-account",),
        models=models,
        providers=(
            ProviderSpec(
                provider_id="opencode-go",
                base_url=_OPENCODE_GO_BASE,
                protocols=("openai", "anthropic"),
                static_models=models,
                account_names=("live-account",),
                wire_surfaces=_SURFACE_CONFIG,
            ),
        ),
        outbound_observer=observer.append,
        wire_runtime_enabled=True,
    )


@pytest_asyncio.fixture()
async def live_app(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[tuple[FastAPI, list[Any]], None]:
    api_key = os.environ["EGGPOOL_E2E_OPENCODE_GO_API_KEY"]
    # The fixture uses this variable only inside the isolated temporary app.
    monkeypatch.setenv("REAL_RUNTIME_KEY", api_key)
    observations: list[Any] = []
    result = await build_runtime_app(_live_spec(observations), tmp_path=tmp_path)
    try:
        yield result.application, observations
    finally:
        await result.runtime_manager.shutdown()
        await result.db.disconnect()
        await result.httpx_client.aclose()


def _headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _payload(model_id: str, *, stream: bool = False) -> dict[str, Any]:
    protocol = _MODEL_PROTOCOLS[model_id]
    if protocol == "anthropic":
        return {
            "model": model_id,
            "max_tokens": 16,
            "stream": stream,
            "messages": [{"role": "user", "content": "Say live ok."}],
        }
    if _MODEL_SURFACES[model_id] == "openai_responses":
        return {
            "model": model_id,
            "input": "Say live ok.",
            "store": False,
            "max_output_tokens": 16,
            "stream": stream,
        }
    return {
        "model": model_id,
        "max_tokens": 16,
        "stream": stream,
        "messages": [{"role": "user", "content": "Say live ok."}],
    }


def _endpoint(model_id: str) -> str:
    protocol = _MODEL_PROTOCOLS[model_id]
    if protocol == "anthropic":
        return "/v1/messages"
    if _MODEL_SURFACES[model_id] == "openai_responses":
        return "/v1/responses"
    return "/v1/chat/completions"


@pytest.mark.asyncio()
async def test_opencode_go_current_surface_matrix(
    live_app: tuple[FastAPI, list[Any]],
) -> None:
    """Current representative models route through their documented paths."""
    app, observations = live_app
    api_key = os.environ["EGGPOOL_E2E_OPENCODE_GO_API_KEY"]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        timeout=httpx.Timeout(180.0),
    ) as client:
        for model_id, expected_surface in _MODEL_SURFACES.items():
            start = len(observations)
            response = await client.post(
                _endpoint(model_id),
                headers=_headers(api_key),
                json=_payload(model_id),
            )
            assert response.status_code == 200, response.text
            second = await client.post(
                _endpoint(model_id),
                headers=_headers(api_key),
                json=_payload(model_id),
            )
            assert second.status_code == 200, second.text

            model_observations = [
                item for item in observations[start:] if item.model_id == model_id
            ]
            assert len(model_observations) == 2
            assert all(
                item.path
                == {
                    "openai_responses": "/responses",
                    "anthropic_messages": "/messages",
                    "openai_chat_completions": "/chat/completions",
                }[expected_surface]
                for item in model_observations
            )
            assert all(
                item.wire_surface == expected_surface for item in model_observations
            )
            assert [item.attempt_ordinal for item in model_observations] == [1, 1]
            assert all(
                item.wire_selection_source is not None for item in model_observations
            )
            health = app.state.health_manager.get_health_stats("live-account")
            assert health["is_healthy"] is True


@pytest.mark.asyncio()
async def test_opencode_go_streams_have_native_terminal_evidence(
    live_app: tuple[FastAPI, list[Any]],
) -> None:
    """One stream per surface family completes in the public grammar."""
    app, observations = live_app
    api_key = os.environ["EGGPOOL_E2E_OPENCODE_GO_API_KEY"]
    stream_cases = (
        ("muse-spark-1.2-contributor", "response.completed"),
        ("mimo-v2.5", "[DONE]"),
        ("minimax-m3", "message_stop"),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        timeout=httpx.Timeout(180.0),
    ) as client:
        for model_id, terminal in stream_cases:
            async with asyncio.timeout(180.0):
                async with client.stream(
                    "POST",
                    _endpoint(model_id),
                    headers=_headers(api_key),
                    json=_payload(model_id, stream=True),
                ) as response:
                    assert response.status_code == 200, await response.aread()
                    body = b"".join([chunk async for chunk in response.aiter_bytes()])
            assert terminal.encode() in body
            assert any(
                item.model_id == model_id and item.streaming for item in observations
            )

    pending = await app.state.db.fetch_all(
        "SELECT id FROM requests WHERE status = 'pending'"
    )
    assert pending == []


@pytest.mark.asyncio()
async def test_opencode_go_reasoning_shapes_are_surface_native(
    live_app: tuple[FastAPI, list[Any]],
) -> None:
    """Reasoning controls do not fabricate Anthropic budgets on Responses."""
    app, observations = live_app
    api_key = os.environ["EGGPOOL_E2E_OPENCODE_GO_API_KEY"]
    transport = httpx.ASGITransport(app=app)
    cases = (
        ("muse-spark-1.2-contributor", {"reasoning"}),
        ("mimo-v2.5", {"reasoning_effort"}),
    )
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        timeout=httpx.Timeout(180.0),
    ) as client:
        for model_id, expected_fields in cases:
            payload = _payload(model_id)
            if model_id == "muse-spark-1.2-contributor":
                payload["reasoning"] = {"effort": "low"}
            else:
                payload["reasoning_effort"] = "low"
            response = await client.post(
                _endpoint(model_id), headers=_headers(api_key), json=payload
            )
            assert response.status_code in {200, 400}, response.text
            item = next(
                item for item in reversed(observations) if item.model_id == model_id
            )
            assert expected_fields.issubset(set(item.semantic_fields))
            assert "thinking" not in item.semantic_fields
            assert app.state.health_manager.is_account_healthy("live-account")


@pytest.mark.asyncio()
async def test_opencode_go_invalid_key_isolated_from_valid_account(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit invalid credential does not poison the valid account."""
    good_key = os.environ["EGGPOOL_E2E_OPENCODE_GO_API_KEY"]
    monkeypatch.setenv("REAL_RUNTIME_KEY", good_key)
    monkeypatch.setenv("EGGPOOL_E2E_BAD_KEY", "e2e-invalid-key")
    observations: list[Any] = []
    models = (ModelSpec(model_id="mimo-v2.5", protocol="openai"),)
    spec = RuntimeAppSpec(
        account_names=("bad-account", "good-account"),
        models=models,
        providers=(
            ProviderSpec(
                provider_id="opencode-go",
                base_url=_OPENCODE_GO_BASE,
                protocols=("openai",),
                static_models=models,
                account_names=("bad-account", "good-account"),
                account_api_key_envs={
                    "bad-account": "EGGPOOL_E2E_BAD_KEY",
                    "good-account": "REAL_RUNTIME_KEY",
                },
                wire_surfaces=_SURFACE_CONFIG,
            ),
        ),
        outbound_observer=observations.append,
        wire_runtime_enabled=True,
    )
    result = await build_runtime_app(spec, tmp_path=tmp_path)
    try:
        transport = httpx.ASGITransport(app=result.application)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            timeout=httpx.Timeout(180.0),
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                headers=_headers(good_key),
                json=_payload("mimo-v2.5"),
            )
            if not any(item.account_id == "bad-account" for item in observations):
                pytest.skip("routing selected the valid account before the bad account")
            assert response.status_code == 200, response.text
            follow_up = await client.post(
                "/v1/chat/completions",
                headers=_headers(good_key),
                json=_payload("mimo-v2.5"),
            )
            assert follow_up.status_code == 200, follow_up.text

        bad_health = result.health_manager.get_health_stats("bad-account")
        good_health = result.health_manager.get_health_stats("good-account")
        assert bad_health["health_state"] == "authentication_failed"
        assert good_health["is_healthy"] is True
    finally:
        await result.runtime_manager.shutdown()
        await result.db.disconnect()
        await result.httpx_client.aclose()
