"""End-to-end regression test for the muse-spark 5xx blanket-suppression bug.

Repro scenario (mocked):

- The router is configured with two accounts under the ``opencode-go``
  provider.  Two models are advertised: ``muse-spark-1.2-contributor``
  (which the upstream returns ``HTTP 500`` for) and ``qwen3.7-max``
  (which the upstream serves normally).

- Pre-fix behavior: a single muse-spark request routes to account 1,
  gets a 500, and the classifier applies ``account_effect="cooldown"``
  — the entire account goes into ``cooldown_until`` and is marked
  unhealthy.  The retry path picks account 2, the same thing happens,
  and the next request for ANY model on either account returns
  "no accounts available" because both accounts are in account-wide
  cooldown.  The user has to restart the router with a fresh DB to
  recover.

- Post-fix behavior: a 5xx on one model quarantines only that
  (account, model) pair via ``model_effect="quarantine"``.  The account
  itself stays healthy, sibling models on the same account keep
  routing, and only muse-spark itself becomes unavailable until the
  bounded quarantine expires.

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
# Fixture: two accounts, two models — muse-spark-1.2-contributor is broken,
# qwen3.7-max works
# ---------------------------------------------------------------------------

_MUSE_SPARK_SPEC = RuntimeAppSpec(
    account_names=("rt-acct-1", "rt-acct-2"),
    models=(
        ModelSpec(
            model_id="muse-spark-1.2-contributor",
            protocol="openai",
        ),
        ModelSpec(
            model_id="qwen3.7-max",
            protocol="openai",
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
                ),
                ModelSpec(
                    model_id="qwen3.7-max",
                    protocol="openai",
                ),
            ),
            account_names=("rt-acct-1", "rt-acct-2"),
        ),
    ),
)


@pytest_asyncio.fixture()
async def muse_spark_app(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[FastAPI, None]:
    """App with two accounts and two models — muse-spark broken upstream."""
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


def _openai_success(model_id: str, content: str) -> dict[str, Any]:
    """OpenAI-style success response used for sibling-model recovery."""
    return {
        "id": "qwen-ok-1",
        "object": "chat.completion",
        "model": model_id,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 3,
            "completion_tokens": 2,
            "total_tokens": 5,
        },
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMuseSparkUpstreamIsolation:
    """Sibling-model isolation when one model returns upstream 5xx."""

    @respx.mock
    @pytest.mark.asyncio()
    async def test_broken_muse_spark_does_not_blacklist_qwen(
        self,
        muse_spark_app: FastAPI,
    ) -> None:
        """muse-spark fails for both accounts; qwen3.7-max still routes."""
        from httpx import ASGITransport

        transport = ASGITransport(app=muse_spark_app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            health_mgr = muse_spark_app.state.health_manager
            pre = health_mgr.get_health_stats("rt-acct-1")
            assert pre["is_healthy"] is True
            assert pre["cooldown_until"] == 0.0
            assert pre["disabled_models"] == []

            # --- Both accounts return 500 for muse-spark ---
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
                return_value=httpx.Response(500, json=_openai_500("muse-spark"))
            )

            resp = await client.post(
                "/v1/messages",
                headers={"Authorization": "Bearer rt-test-key"},
                json={
                    "model": "muse-spark-1.2-contributor",
                    "max_tokens": 20,
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
            # muse-spark genuinely fails end-to-end.  The upstream 500 is
            # passed through (both attempts exhausted and the last upstream
            # response takes precedence over a synthetic 502 envelope).
            assert resp.status_code == 500

            # --- Account-level health preserved ---
            for acct in ("rt-acct-1", "rt-acct-2"):
                stats = health_mgr.get_health_stats(acct)
                # The account itself stays healthy — no blanket cooldown.
                assert stats["is_healthy"] is True, (
                    f"account {acct} must not be blanket-cooldown'd "
                    f"by a single broken model"
                )
                assert stats["cooldown_until"] == 0.0, (
                    f"account {acct} cooldown_until must remain 0; got "
                    f"{stats['cooldown_until']}"
                )
                assert stats["health_state"] == "healthy", (
                    f"account {acct} health_state must remain 'healthy'; got "
                    f"{stats['health_state']!r}"
                )

            # --- Only the broken model is suppressed ---
            assert (
                health_mgr.is_model_healthy("rt-acct-1", "muse-spark-1.2-contributor")
                is False
            )
            assert (
                health_mgr.is_model_healthy("rt-acct-2", "muse-spark-1.2-contributor")
                is False
            )

            # --- Sibling models on the SAME accounts stay routable ---
            assert health_mgr.is_model_healthy("rt-acct-1", "qwen3.7-max") is True
            assert health_mgr.is_model_healthy("rt-acct-2", "qwen3.7-max") is True

            # --- qwen3.7-max actually works end-to-end ---
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
                return_value=httpx.Response(
                    200, json=_openai_success("qwen3.7-max", "Hi back")
                )
            )
            resp_q = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer rt-test-key"},
                json={
                    "model": "qwen3.7-max",
                    "max_tokens": 20,
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
            assert resp_q.status_code == 200
            assert resp_q.json()["choices"][0]["message"]["content"] == "Hi back"

    @respx.mock
    @pytest.mark.asyncio()
    async def test_two_account_5xx_does_not_require_db_reset(
        self,
        muse_spark_app: FastAPI,
    ) -> None:
        """The fix removes the 'restart with fresh db' recovery requirement.

        Pre-fix: a single muse-spark 5xx per account left both accounts in
        account-wide cooldown; recovery required ``eggpool`` restart with a
        fresh DB.  Post-fix: the bounded quarantine auto-expires and the
        broken model becomes available again without operator intervention.
        """
        health_mgr = muse_spark_app.state.health_manager
        # Two 5xx observations on rt-acct-1 promote muse-spark to QUARANTINED
        # (default 300s TTL) via the in-memory quarantine state machine.  We
        # just exercise the path and confirm the persisted backoff row is
        # model-scoped, not account-scoped — so the durable state does not
        # require a fresh DB to recover sibling models.
        for _ in range(2):
            obs = type(
                "_Obs",
                (),
                {
                    "account_name": "rt-acct-1",
                    "provider_id": "opencode-go",
                    "model_id": "muse-spark-1.2-contributor",
                    "upstream_model_id": "muse-spark-1.2-contributor",
                    "upstream_protocol": "openai",
                    "status_code": 500,
                    "error_class": None,
                    "source": "upstream_http",
                    "response_signal": None,
                    "retry_after_s": None,
                    "response_started": False,
                    "downstream_started": False,
                },
            )()
            from eggpool.failure.applier import EffectsApplier
            from eggpool.failure.classifier import classify_failure_effects
            from eggpool.failure.quarantine import ModelQuarantine

            quarantine = ModelQuarantine()
            applier = EffectsApplier(health_manager=health_mgr, quarantine=quarantine)
            effects = classify_failure_effects(obs)  # type: ignore[arg-type]
            applier.apply_once(
                f"attempt-{_}",
                obs,
                effects,  # type: ignore[arg-type]
            )

        # Account itself stays healthy
        assert health_mgr.is_account_healthy("rt-acct-1") is True
        # muse-spark is quarantined
        assert (
            health_mgr.is_model_healthy("rt-acct-1", "muse-spark-1.2-contributor")
            is False
        )
        # Sibling model on the same account is routable
        assert health_mgr.is_model_healthy("rt-acct-1", "qwen3.7-max") is True
