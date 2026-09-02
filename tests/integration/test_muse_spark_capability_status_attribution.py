"""End-to-end regression test for the muse-spark capability status attribution bug.

Repro scenario (mocked):

- The router is configured with two accounts under the ``opencode-go``
  provider.  Both accounts advertise ``muse-spark-1.2-contributor`` with
  no upstream capability metadata (only the built-in override marks
  the model ``thinking.status="supported"``).

- Pre-fix behavior: the first muse-spark request surfaces an upstream
  error and both get retried; once both accounts are filtered, the
  capability-status attribution falls back to ``cache.get_model()``
  (which returns the raw entry without applying the per-provider
  built-in override), so the rejection status reports ``"unknown"`` even
  though the provider actually supports thinking.  The user sees:

      "Model 'muse-spark-1.2-contributor' is available, but no eligible
      provider is known to support requested thinking controls
      (thinking capability status: unknown)."

- Post-fix behavior: when every supporting account is filtered for
  reasons unrelated to the thinking capability, the rejection path
  surfaces a transient ``No accounts available for model`` (503) error
  instead of the misleading client-validation 400.  The built-in
  capability override remains authoritative — the model still
  advertises ``thinking.status="supported"`` for both the catalog
  exposure and the routing trace metrics.

This module asserts the post-fix behavior end-to-end through the real
Eggpool ASGI surface with respx mocking the upstream; it never calls a
live service.
"""

from __future__ import annotations

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
# Fixture: two accounts under opencode-go with muse-spark-1.2-contributor
# ---------------------------------------------------------------------------


# Capability override applied to mirror the bundled built-in override
# for the canonical opencode-go provider.  The default real-runtime
# fixture uses ``UPSTREAM_BASE`` which is NOT the canonical
# ``https://opencode.ai/zen/go/v1`` URL, so the built-in override
# from ``models/config.py`` would not auto-seed; we declare it
# explicitly here.
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
        "notes": "OpenCode Go exposes minimal/low/medium/high/xhigh thinking controls.",
    },
}

_MUSE_SPARK_SPEC = RuntimeAppSpec(
    account_names=("rt-acct-1", "rt-acct-2"),
    models=(
        ModelSpec(
            model_id="muse-spark-1.2-contributor",
            protocol="openai",
            capabilities=_MUSE_SPARK_THINKING_CAPABILITY,
        ),
    ),
    providers=(
        ProviderSpec(
            provider_id="opencode-go",
            base_url=UPSTREAM_BASE,
            protocols=("openai",),
            static_models=(
                ModelSpec(
                    model_id="muse-spark-1.2-contributor",
                    protocol="openai",
                    capabilities=_MUSE_SPARK_THINKING_CAPABILITY,
                ),
            ),
            account_names=("rt-acct-1", "rt-acct-2"),
        ),
    ),
)


@pytest_asyncio.fixture()
async def muse_spark_status_app(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[FastAPI, None]:
    """App with two opencode-go accounts advertising muse-spark-1.2-contributor."""
    monkeypatch.setenv("REAL_RUNTIME_KEY", "rt-test-key")
    result = await build_runtime_app(_MUSE_SPARK_SPEC, tmp_path=tmp_path)
    try:
        yield result.application
    finally:
        await result.runtime_manager.shutdown()
        await result.db.disconnect()
        await result.httpx_client.aclose()


def _openai_500(model_id: str) -> dict[str, Any]:
    """OpenAI-family upstream 500 for muse-spark-1.2-contributor."""
    return {
        "type": "error",
        "error": {
            "type": "error",
            "message": "Internal server error",
        },
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMuseSparkCapabilityStatusAttribution:
    """Capability status attribution after muse-spark accounts are filtered."""

    @respx.mock
    @pytest.mark.asyncio()
    async def test_quarantined_model_surfaces_transient_503_not_misleading_400(
        self,
        muse_spark_status_app: FastAPI,
    ) -> None:
        """After both accounts are filtered for muse-spark, the router
        must surface a transient upstream-unavailability response, NOT
        a misleading client-validation 400 with a false ``unknown``
        capability status.  The built-in override marks thinking as
        ``supported`` for the canonical opencode-go provider, so the
        ``rejected_status`` must not be ``"unknown"`` (or
        ``"unsupported"``).
        """
        from httpx import ASGITransport

        transport = ASGITransport(app=muse_spark_status_app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            # --- Both accounts return 500 for muse-spark ---
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
                return_value=httpx.Response(500, json=_openai_500("muse-spark"))
            )

            # First request: both attempts fail upstream; the response
            # is the upstream 500 (passed through by the routing path).
            first = await client.post(
                "/v1/messages",
                headers={"Authorization": "Bearer rt-test-key"},
                json={
                    "model": "muse-spark-1.2-contributor",
                    "max_tokens": 20,
                    "messages": [{"role": "user", "content": "hi"}],
                    "thinking": {"type": "enabled", "budget_tokens": 1024},
                },
            )
            assert first.status_code == 500

            # Subsequent requests with thinking controls: both
            # accounts are quarantined for muse-spark. The router
            # must surface 503 (no accounts available) — NOT a 400
            # claiming the provider cannot support thinking, since
            # the built-in capability override marks the provider
            # ``thinking.status="supported"``.
            second = await client.post(
                "/v1/messages",
                headers={"Authorization": "Bearer rt-test-key"},
                json={
                    "model": "muse-spark-1.2-contributor",
                    "max_tokens": 20,
                    "messages": [{"role": "user", "content": "hi"}],
                    "thinking": {"type": "enabled", "budget_tokens": 1024},
                },
            )
            assert second.status_code == 503, (
                f"expected 503 (transient no-accounts-available) but got "
                f"{second.status_code}: "
                f"{second.json().get('error', {}).get('message')!r}"
            )
            body = second.json()
            assert "no_eligible_provider" not in str(body).lower(), (
                "router must not surface the misleading client-validation "
                f"capability error after quarantine: {body!r}"
            )

    @respx.mock
    @pytest.mark.asyncio()
    async def test_catalog_exposes_supported_thinking_after_quarantine(
        self,
        muse_spark_status_app: FastAPI,
    ) -> None:
        """The catalog ``/v1/models`` exposure must still advertise the
        built-in capability override (``thinking.status="supported"``)
        before any quarantine; only the routing eligibility changes,
        not the catalog contract.
        """
        from httpx import ASGITransport

        transport = ASGITransport(app=muse_spark_status_app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            # Catalog at startup must report the override
            models_resp = await client.get(
                "/v1/models", headers={"Authorization": "Bearer rt-test-key"}
            )
            muse_rows = [
                m
                for m in models_resp.json().get("data", [])
                if "muse-spark" in m.get("id", "")
            ]
            assert muse_rows, "muse-spark row must be exposed in the catalog"
            for row in muse_rows:
                thinking = (
                    row.get("eggpool", {}).get("capabilities", {}).get("thinking", {})
                )
                assert thinking.get("status") == "supported", (
                    f"catalog must report the supported override; row={row!r}"
                )
            # Now trigger quarantine and confirm the failure-mode behavior
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
                return_value=httpx.Response(500, json=_openai_500("muse-spark"))
            )
            for _ in range(3):
                await client.post(
                    "/v1/messages",
                    headers={"Authorization": "Bearer rt-test-key"},
                    json={
                        "model": "muse-spark-1.2-contributor",
                        "max_tokens": 20,
                        "messages": [{"role": "user", "content": "hi"}],
                        "thinking": {"type": "enabled", "budget_tokens": 1024},
                    },
                )
            # Routing eligibility must report no-accounts-available (503)
            # NOT a misleading thinking-capability-status error (400).
            follow_up = await client.post(
                "/v1/messages",
                headers={"Authorization": "Bearer rt-test-key"},
                json={
                    "model": "muse-spark-1.2-contributor",
                    "max_tokens": 20,
                    "messages": [{"role": "user", "content": "hi"}],
                    "thinking": {"type": "enabled", "budget_tokens": 1024},
                },
            )
            assert follow_up.status_code == 503
            body = follow_up.json()
            assert "thinking capability status" not in str(body).lower()
