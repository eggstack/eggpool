"""Deterministic mocked integration test for Muse Spark through EggPool.

The acceptance criteria from the bug report require that:

- The muse-spark-1.2-contributor model can be requested with thinking
  controls and routed through eggpool successfully (i.e. it does not
  surface the misleading "thinking capability status: unknown" 400).

- A failure on muse-spark-1.2-contributor must not black-hole sibling
  models on the same opencode-go subscription — only the
  (provider, account, model) tuple is quarantined.

- After the upstream returns 5xx for muse-spark-1.2-contributor, the
  next request must be a transient 503 (``No accounts available``),
  not a misleading 400 (``thinking capability status: unknown``).

This module runs the entire request flow through the real EggPool
ASGI surface with respx mocking the upstream; it never calls a live service.
The OpenCode Go Muse Spark contract is declared explicitly in the fixture,
matching the provider-scoped metadata or operator override required by
production for this mocked endpoint.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import pytest_asyncio
import respx

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
# Explicit provider capability metadata for the Muse Spark Contributor
# models. Production does not infer these controls from endpoint identity
# or model names, so the mocked provider advertises them directly.
# ---------------------------------------------------------------------------

_MUSE_SPARK_MODEL_IDS = (
    "muse-spark-1.2-contributor",
    "muse-spark-1.3-contributor",
)

_MUSE_SPARK_THINKING_CAPABILITY = {
    "thinking": {
        "status": "supported",
        "source": "provider_catalog",
        "native_protocols": ["openai"],
        "supported_efforts": ["minimal", "low", "medium", "high", "xhigh"],
        "effort_to_budget_tokens": {
            "minimal": 1024,
            "low": 1024,
            "medium": 4096,
            "high": 16384,
            "xhigh": 24576,
        },
        "control_contract": {
            "mode": "effort_or_budget",
            "request_fields": ["thinking", "thinking_budget", "reasoning_effort"],
            "accepted_efforts": ["minimal", "low", "medium", "high", "xhigh"],
            "effort_to_budget_tokens": {
                "minimal": 1024,
                "low": 1024,
                "medium": 4096,
                "high": 16384,
                "xhigh": 24576,
            },
            "explicit_budget_min": 1024,
            "explicit_budget_max": 24576,
            "source": "provider_catalog",
        },
        "notes": "OpenCode Go exposes minimal/low/medium/high/xhigh thinking controls.",
    },
}

_SIBLING_THINKING_CAPABILITY = {
    "thinking": {
        "status": "supported",
        "source": "provider_catalog",
        "native_protocols": ["openai"],
        "supported_efforts": ["low", "medium", "high"],
        "effort_to_budget_tokens": {
            "low": 1024,
            "medium": 4096,
            "high": 16384,
        },
        "notes": "Sibling opencode-go model exposes low/medium/high thinking controls.",
    },
}

_MUSE_WIRE_SURFACES = {
    "openai_responses": {
        "path_template": "/responses",
        "priority": 10,
        "auth": {"mode": "bearer"},
    },
}


_MUSE_SPARK_MODEL_SPECS = tuple(
    ModelSpec(
        model_id=model_id,
        protocol="openai",
        capabilities=_MUSE_SPARK_THINKING_CAPABILITY,
    )
    for model_id in _MUSE_SPARK_MODEL_IDS
)

_MUSE_SPEC = RuntimeAppSpec(
    account_names=("rt-acct-1", "rt-acct-2"),
    models=(
        *_MUSE_SPARK_MODEL_SPECS,
        ModelSpec(
            model_id="sibling-model",
            protocol="openai",
            capabilities=_SIBLING_THINKING_CAPABILITY,
        ),
    ),
    providers=(
        ProviderSpec(
            provider_id="opencode-go",
            base_url=UPSTREAM_BASE,
            protocols=("openai",),
            static_models=(
                *_MUSE_SPARK_MODEL_SPECS,
                ModelSpec(
                    model_id="sibling-model",
                    protocol="openai",
                    capabilities=_SIBLING_THINKING_CAPABILITY,
                ),
            ),
            account_names=("rt-acct-1", "rt-acct-2"),
            wire_surfaces=_MUSE_WIRE_SURFACES,
        ),
    ),
    wire_runtime_enabled=True,
)


@pytest_asyncio.fixture()
async def muse_app(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[FastAPI, None]:
    """App for the full muse-spark E2E isolation flow."""
    monkeypatch.setenv("REAL_RUNTIME_KEY", "rt-test-key")
    result = await build_runtime_app(_MUSE_SPEC, tmp_path=tmp_path)
    try:
        yield result.application
    finally:
        await result.runtime_manager.shutdown()
        await result.db.disconnect()
        await result.httpx_client.aclose()


def _responses_success(model_id: str, content: str) -> dict[str, Any]:
    return {
        "id": f"response-{model_id}-ok",
        "object": "response",
        "status": "completed",
        "model": model_id,
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": content}],
            }
        ],
        "usage": {
            "prompt_tokens": 3,
            "completion_tokens": 2,
            "total_tokens": 5,
        },
    }


def _openai_500() -> dict[str, Any]:
    return {
        "type": "error",
        "error": {"type": "error", "message": "Internal server error"},
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMuseSparkRouting:
    """End-to-end mocked routing for the Muse Spark Contributor models."""

    @respx.mock
    @pytest.mark.asyncio()
    @pytest.mark.parametrize("model_id", _MUSE_SPARK_MODEL_IDS)
    async def test_muse_spark_with_thinking_routes_successfully(
        self,
        muse_app: FastAPI,
        model_id: str,
    ) -> None:
        """Happy path — each Muse Spark model serves thinking end-to-end."""
        from httpx import ASGITransport

        transport = ASGITransport(app=muse_app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            route = respx.post(f"{UPSTREAM_BASE}/responses").mock(
                return_value=httpx.Response(
                    200,
                    json=_responses_success(model_id, "Hi!"),
                )
            )

            resp = await client.post(
                "/v1/messages",
                headers={"Authorization": "Bearer rt-test-key"},
                json={
                    "model": model_id,
                    "max_tokens": 64,
                    "messages": [{"role": "user", "content": "say hi"}],
                    "thinking": {
                        "type": "enabled",
                        "budget_tokens": 1024,
                    },
                },
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["content"][0]["text"] == "Hi!"
            assert body["model"] == model_id

            assert route.call_count == 1
            upstream_body = json.loads(route.calls.last.request.content)
            assert upstream_body["model"] == model_id
            assert "input" in upstream_body
            assert upstream_body["max_output_tokens"] == 64

    @respx.mock
    @pytest.mark.asyncio()
    async def test_muse_spark_5xx_quarantines_only_muse_spark(
        self,
        muse_app: FastAPI,
    ) -> None:
        """Acceptance criterion #1: failure isolation.

        When both opencode-go accounts return 5xx for muse-spark-1.2-
        contributor, sibling models on the same accounts must remain
        routable end-to-end.  This is the regression scenario the bug
        report described as ``5+ opencode-go subscriptions fail for all
        models when one model/provider breaks``.
        """
        from httpx import ASGITransport

        transport = ASGITransport(app=muse_app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            health_mgr = muse_app.state.health_manager

            # All upstream calls for muse-spark return 500.
            respx.post(f"{UPSTREAM_BASE}/responses").mock(
                return_value=httpx.Response(500, json=_openai_500())
            )

            muse_resp = await client.post(
                "/v1/messages",
                headers={
                    "Authorization": "Bearer rt-test-key",
                },
                json={
                    "model": "muse-spark-1.2-contributor",
                    "max_tokens": 64,
                    "messages": [{"role": "user", "content": "hi"}],
                    "thinking": {
                        "type": "enabled",
                        "budget_tokens": 1024,
                    },
                },
            )
            # muse-spark itself genuinely fails; the router passes the
            # upstream 500 through after exhausting both accounts.
            assert muse_resp.status_code == 500

            # --- Account-level isolation preserved ---
            for acct in ("rt-acct-1", "rt-acct-2"):
                stats = health_mgr.get_health_stats(acct)
                assert stats["is_healthy"] is True, (
                    f"account {acct} must NOT be blanket-cooldown'd by "
                    f"a single broken model"
                )
                assert stats["cooldown_until"] == 0.0
                assert stats["circuit_breaker"]["state"] == "closed", (
                    f"account {acct} circuit breaker must remain closed "
                    f"after a per-model 5xx; got {stats['circuit_breaker']!r}"
                )

            # --- Per-model quarantine applied for muse-spark ---
            for acct in ("rt-acct-1", "rt-acct-2"):
                assert (
                    health_mgr.is_model_healthy(acct, "muse-spark-1.2-contributor")
                    is False
                )

            # --- Sibling models on the SAME accounts stay routable ---
            for acct in ("rt-acct-1", "rt-acct-2"):
                assert health_mgr.is_model_healthy(acct, "sibling-model") is True

            # --- Sibling model actually works end-to-end ---
            respx.post(f"{UPSTREAM_BASE}/responses").mock(
                return_value=httpx.Response(
                    200, json=_responses_success("sibling-model", "Sibling OK")
                )
            )
            sibling_resp = await client.post(
                "/v1/messages",
                headers={
                    "Authorization": "Bearer rt-test-key",
                },
                json={
                    "model": "sibling-model",
                    "max_tokens": 64,
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
            assert sibling_resp.status_code == 200, sibling_resp.text
            assert sibling_resp.json()["content"][0]["text"] == "Sibling OK"

    @respx.mock
    @pytest.mark.asyncio()
    async def test_muse_spark_5xx_does_not_say_thinking_capability_status_unknown(
        self,
        muse_app: FastAPI,
    ) -> None:
        """Acceptance criterion #2: no misleading `` capability status
        unknown`` error after every supporting account is filtered.

        Pre-fix: the user-facing message was a 400 ``thinking capability
        status: unknown`` even though the provider actually supports
        thinking (the capability override is in the per-provider row,
        not the collapsed one).  Post-fix: the router reports a 503
        transient ``No accounts available`` so the operator can retry
        after the bounded quarantine expires.
        """
        from httpx import ASGITransport

        transport = ASGITransport(app=muse_app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            respx.post(f"{UPSTREAM_BASE}/responses").mock(
                return_value=httpx.Response(500, json=_openai_500())
            )

            # First request — both accounts fail.
            await client.post(
                "/v1/messages",
                headers={
                    "Authorization": "Bearer rt-test-key",
                },
                json={
                    "model": "muse-spark-1.2-contributor",
                    "max_tokens": 64,
                    "messages": [{"role": "user", "content": "hi"}],
                    "thinking": {
                        "type": "enabled",
                        "budget_tokens": 1024,
                    },
                },
            )

            # Second request — both accounts are quarantined.
            follow_up = await client.post(
                "/v1/messages",
                headers={
                    "Authorization": "Bearer rt-test-key",
                },
                json={
                    "model": "muse-spark-1.2-contributor",
                    "max_tokens": 64,
                    "messages": [{"role": "user", "content": "hi"}],
                    "thinking": {
                        "type": "enabled",
                        "budget_tokens": 1024,
                    },
                },
            )
            assert follow_up.status_code in (502, 503, 504), follow_up.text
            error_msg = str(follow_up.json().get("error", {})).lower()
            assert "thinking capability status" not in error_msg, (
                f"router must not surface a misleading "
                f"thinking-capability-status error after quarantine: "
                f"{follow_up.json()!r}"
            )
            assert (
                "no accounts" in error_msg
                or "no eligible" in error_msg
                or "unresolved" in error_msg
            ), (
                f"router must surface a transient upstream-unavailable "
                f"error after quarantine: "
                f"{follow_up.json()!r}"
            )
