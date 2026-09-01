"""MiniMax/OpenCode Go request-local isolation regression test.

Proves that a provider-specific thinking-control rejection (CapabilityError
→ HTTP 400) through the real Eggpool ASGI endpoint does not poison shared
account, model, circuit, quarantine, or reservation state.  A subsequent
plain request succeeds, confirming request-local isolation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import pytest
import pytest_asyncio
import respx

from eggpool.jsonx import loads as jsonx_loads
from tests.helpers.real_runtime import (
    UPSTREAM_BASE,
    ModelSpec,
    ProviderSpec,
    RuntimeAppSpec,
    build_runtime_app,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from fastapi import FastAPI

pytestmark = [pytest.mark.integration]


# ---------------------------------------------------------------------------
# Fixture: App with MiniMax-M3 model and reject thinking control policy
# ---------------------------------------------------------------------------

_MINIMAX_SPEC = RuntimeAppSpec(
    account_names=("rt-acct-1",),
    models=(
        ModelSpec(
            model_id="MiniMax-M3",
            protocol="openai",
            capabilities={
                "thinking": {
                    "status": "supported",
                    "control_contract": {
                        "mode": "fixed",
                        "source": "manual_override",
                    },
                },
            },
        ),
        ModelSpec(model_id="minimax-m3", protocol="openai"),
    ),
    providers=(
        ProviderSpec(
            provider_id="opencode-go",
            base_url=UPSTREAM_BASE,
            protocols=("openai",),
            static_models=(
                ModelSpec(
                    model_id="MiniMax-M3",
                    protocol="openai",
                    capabilities={
                        "thinking": {
                            "status": "supported",
                            "control_contract": {
                                "mode": "fixed",
                                "source": "manual_override",
                            },
                        },
                    },
                ),
                # OpenCode Go's live catalog uses this lowercase ID. Keep
                # the exact provider-suffixed client form in the regression
                # path so native OpenAI dispatch is exercised.
                ModelSpec(
                    model_id="minimax-m3",
                    protocol="openai",
                ),
            ),
            account_names=("rt-acct-1",),
        ),
    ),
    transcoder_overrides={
        "provider_control_policy": {
            "unsupported_control": "reject",
        },
    },
)


@pytest_asyncio.fixture()
async def minimax_isolation_app(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[FastAPI, None]:
    """App configured for MiniMax-M3 with reject thinking control policy."""
    monkeypatch.setenv("REAL_RUNTIME_KEY", "rt-test-key")

    # Seed the catalog with provider-model entries so routing finds MiniMax-M3
    # under the opencode-go provider.  The factory handles all component wiring.
    result = await build_runtime_app(_MINIMAX_SPEC, tmp_path=tmp_path)

    yield result.application

    await result.db.disconnect()
    await result.httpx_client.aclose()


# ---------------------------------------------------------------------------
# Test: request-local isolation for thinking-control rejection
# ---------------------------------------------------------------------------


class TestMiniMaxOpenCodeGoRequestLocalIsolation:
    """A rejected thinking-control request does not poison shared state."""

    @respx.mock
    @pytest.mark.asyncio()
    async def test_rejection_is_request_local(
        self,
        minimax_isolation_app: FastAPI,
    ) -> None:
        """CapabilityError → HTTP 400; next plain request succeeds."""
        from httpx import ASGITransport

        transport = ASGITransport(app=minimax_isolation_app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            # --- Capture pre-request health/circuit state ---
            pre_health = await client.get("/v1/healthz")
            assert pre_health.status_code == 200

            health_mgr = minimax_isolation_app.state.health_manager
            pre_account_health = health_mgr.get_health_stats("rt-acct-1")
            assert pre_account_health["is_healthy"] is True
            assert pre_account_health["consecutive_failures"] == 0
            pre_circuit = pre_account_health["circuit_breaker"]
            assert pre_circuit["state"] == "closed"
            assert pre_circuit["failure_count"] == 0

            # --- Request 1: OpenAI request with unsupported reasoning_effort ---
            # The opencode-go provider contract for MiniMax-M3 is "fixed",
            # so reasoning_effort is unsupported and should be rejected locally.
            resp1 = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer rt-test-key"},
                json={
                    "model": "MiniMax-M3",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "reasoning_effort": "high",
                },
            )
            # Should get HTTP 400 (CapabilityError → 400)
            assert resp1.status_code == 400
            body1 = resp1.json()
            assert "error" in body1

            # --- Assert no shared state poisoning ---
            # Health endpoint should still be healthy
            post_health = await client.get("/v1/healthz")
            assert post_health.status_code == 200

            # Account health must remain unchanged
            post_account_health = health_mgr.get_health_stats("rt-acct-1")
            assert post_account_health["is_healthy"] is True
            assert post_account_health["consecutive_failures"] == 0
            assert post_account_health["disabled_reason"] == ""

            # Circuit breaker must remain closed
            post_circuit = post_account_health["circuit_breaker"]
            assert post_circuit["state"] == "closed"
            assert post_circuit["failure_count"] == 0

            # --- Request 2: Plain request without thinking controls ---
            upstream_response = {
                "id": "minimax-plain-1",
                "object": "chat.completion",
                "model": "MiniMax-M3",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "Hello back"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 2,
                    "total_tokens": 5,
                },
            }
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
                return_value=httpx.Response(200, json=upstream_response)
            )

            resp2 = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer rt-test-key"},
                json={
                    "model": "MiniMax-M3",
                    "messages": [{"role": "user", "content": "Hello"}],
                },
            )
            assert resp2.status_code == 200
            body2 = resp2.json()
            assert body2["choices"][0]["message"]["content"] == "Hello back"

            # --- Assert no durable leak ---
            db: Any = minimax_isolation_app.state.db
            pending = await db.fetch_all(
                "SELECT * FROM requests WHERE status = 'pending'"
            )
            assert len(pending) == 0

            active_resv = await db.fetch_all(
                "SELECT * FROM reservations WHERE status = 'active'"
            )
            assert len(active_resv) == 0

            # Health remains healthy after successful request
            final_health = health_mgr.get_health_stats("rt-acct-1")
            assert final_health["is_healthy"] is True
            assert final_health["consecutive_failures"] == 0

    @respx.mock
    @pytest.mark.asyncio()
    async def test_provider_suffixed_model_is_stripped_on_native_openai_path(
        self,
        minimax_isolation_app: FastAPI,
    ) -> None:
        """OpenCode Go must receive ``minimax-m3``, never its EggPool ID."""
        from httpx import ASGITransport

        upstream_response = {
            "id": "minimax-suffixed-1",
            "object": "chat.completion",
            "model": "minimax-m3",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "OK"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 3,
                "completion_tokens": 1,
                "total_tokens": 4,
            },
        }
        route = respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            return_value=httpx.Response(200, json=upstream_response)
        )

        transport = ASGITransport(app=minimax_isolation_app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer rt-test-key"},
                json={
                    "model": "minimax-m3/opencode-go",
                    "messages": [{"role": "user", "content": "Reply with OK"}],
                    "max_tokens": 8,
                },
            )

        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "OK"
        assert route.called
        assert jsonx_loads(route.calls[0].request.content)["model"] == "minimax-m3"
