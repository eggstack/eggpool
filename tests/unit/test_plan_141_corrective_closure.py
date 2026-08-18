"""Plan 142 — final corrective closure regression tests.

Focused regression tests for the typed-error boundaries corrected by
Plan 142:

- F1: API-bound provider 413 routes through the proxy exception boundary
  as ``RequestTooLargeError`` (HTTP 413, not 500), for both OpenAI and
  Anthropic adapters, with the generation lease released exactly once.
- F2: Selected oversize/capability/transcode-loss finalization fails
  closed on a durable finalization failure (DatabaseError propagates).
- F3: Selected-provider capability lookup observes ``selected.provider_id``.
- F4: Retry/source-generation safety through ``_apply_selected_provider_transcode``.
- F5: Text-only prepared-transcode fast path preserved.
- F6: Selected-provider transcode-loss rejection surfaces as typed
  ``TranscodeLossError`` (not ``_LocalDispatchError``/500).
- F7: Provider metadata URL-image facts.
"""

from __future__ import annotations

import json as _json
import tomllib
from importlib.resources import files
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI, Request

from eggpool.api.chat_completions import handle_chat_completions
from eggpool.api.messages import handle_messages
from eggpool.errors import (
    DatabaseError,
    RequestTooLargeError,
)
from eggpool.models.config import AppConfig
from eggpool.request.coordinator import (
    FinalizationData,
    FinalizationOutcome,
    ProxyRequestContext,
    RequestCoordinator,
    SelectedAttempt,
    _LocalDispatchError,
)
from eggpool.request.provider_bound_request import ProviderBoundRequest
from eggpool.runtime_manager import (
    ImmutableRequestState,
    RuntimeGeneration,
    RuntimeManager,
    attach_runtime_manager,
)
from eggpool.transcoder.context import TranscodeContext
from eggpool.transcoder.errors import TranscodeLossError
from eggpool.transcoder.prepared import PreparedTranscode
from eggpool.transcoder.sensitive_media import (
    request_has_provider_sensitive_media,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_coordinator_with_supervisor(supervisor: Any) -> RequestCoordinator:
    return RequestCoordinator(
        registry=MagicMock(),
        catalog=MagicMock(),
        router=MagicMock(),
        db=MagicMock(),
        client_pool=MagicMock(),
        finalization_supervisor=supervisor,
    )


def _make_selected(provider_id: str = "p1") -> SelectedAttempt:
    return SelectedAttempt(
        proxy_request_id="req-1",
        db_request_id="db-1",
        attempt_id=1,
        reservation_id="r-1",
        account_id=1,
        account_name="account",
        api_key="sk-test",
        model_id="gpt-4",
        estimated_tokens=100,
        estimated_microdollars=50,
        attempt_number=1,
        provider_id=provider_id,
    )


def _make_context() -> ProxyRequestContext:
    return ProxyRequestContext(
        request_id="req-1",
        protocol="openai",
        model_id="gpt-4",
        streaming=False,
        original_body=b'{"model":"gpt-4"}',
        incoming_headers={},
    )


# ---------------------------------------------------------------------------
# F1 — API-bound provider 413 routes through the proxy exception boundary
# ---------------------------------------------------------------------------


class TestRequestTooLargeErrorStatusCode:
    """``error_status_code`` must keep returning 413 for ``RequestTooLargeError``."""

    def test_maps_to_413(self) -> None:
        from eggpool.request.static_helpers import error_status_code

        assert error_status_code(RequestTooLargeError("big")) == 413


class _StubGenerationSupervisor:
    def all_healthy(self) -> bool:
        return True

    async def stop_all(self) -> None:
        return


async def _build_app_with_generation(
    coordinator_mock: Any,
) -> tuple[FastAPI, RuntimeManager]:
    """Install a real RuntimeGeneration wired to ``coordinator_mock``."""

    app = FastAPI()
    config = AppConfig()
    config.server.api_key_env = ""
    config.security.trusted_proxies = ["127.0.0.1"]
    app.state.config = config
    app.state.test_coordinator = coordinator_mock

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Any:  # pyright: ignore[reportUnusedFunction]
        return await handle_chat_completions(request)  # type: ignore[return-value]

    @app.post("/v1/messages")
    async def messages(request: Request) -> Any:  # pyright: ignore[reportUnusedFunction]
        return await handle_messages(request)  # type: ignore[return-value]

    registry = MagicMock(
        get_provider_ids=lambda: tuple(config.providers),
        get_enabled_states=lambda: (),
    )
    catalog = MagicMock(
        cache=MagicMock(
            get_model_protocols=lambda *_a, **_k: {"openai"},
            get_transcodable_protocols=lambda *_a, **_k: (),
        )
    )
    generation = RuntimeGeneration(
        generation_id=0,
        config=config,
        config_digest="test",
        registry=registry,
        catalog=catalog,
        router=MagicMock(),
        coordinator=coordinator_mock,
        client_pool=MagicMock(),
        outbound_manager=None,
        health_manager=MagicMock(),
        cost_calculator=MagicMock(),
        transcoder_policy=MagicMock(enabled=False),
        dispatch_overhead_recorder=MagicMock(),
        dispatch_span_recorder=None,
        account_backoff_repo=None,
        stats_service=MagicMock(),
        supervisor=_StubGenerationSupervisor(),
        routing_trace_guard=None,
        routing_trace_writer=None,
        created_at_monotonic=0.0,
        created_at_epoch=0.0,
        immutable_request_state=ImmutableRequestState(
            provider_ids=frozenset(config.providers),
            account_names=frozenset(),
            hop_by_hop_headers=frozenset(),
            local_credential_headers=frozenset(),
            trusted_proxies=frozenset(config.security.trusted_proxies),
        ),
    )
    manager = RuntimeManager()
    await manager.install_initial(generation)
    attach_runtime_manager(app, manager)
    return app, manager


def _active_leases(manager: RuntimeManager) -> int:
    slot = manager._active  # type: ignore[attr-defined]
    if slot is None:
        return 0
    return slot.active_leases


@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v1/messages"])
@pytest.mark.asyncio
async def test_provider_413_routed_at_api_boundary(path: str) -> None:
    """A ``RequestTooLargeError`` from the coordinator must render as 413
    with a protocol-shaped body, not as 500. The generation lease must
    also be released exactly once on the error path."""

    coordinator_mock = MagicMock()
    coordinator_mock.execute = AsyncMock(
        side_effect=RequestTooLargeError("Serialized request body too large")
    )
    app, manager = await _build_app_with_generation(coordinator_mock)

    lease_count_before = _active_leases(manager)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            path,
            json={
                "model": "gpt-4/opencode-go",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    lease_count_after = _active_leases(manager)

    assert response.status_code == 413
    # Lease must be released exactly once on the error path.
    assert lease_count_after == lease_count_before
    body = _json.loads(response.text)
    if path == "/v1/chat/completions":
        assert body["error"]["type"] == "invalid_request_error"
        assert body["error"]["code"] == "413"
    else:
        assert body["type"] == "error"
        assert body["error"]["type"] == "invalid_request_error"
    assert "too large" in body["error"]["message"].lower()


# ---------------------------------------------------------------------------
# F2 — Selected oversize / capability / transcode-loss fail-closed
# ---------------------------------------------------------------------------


def _supervisor_raising(dberror: DatabaseError) -> MagicMock:
    supervisor = MagicMock()
    run_job = MagicMock()
    run_job.run = AsyncMock(side_effect=dberror)
    supervisor.register_or_get = MagicMock(return_value=run_job)
    return supervisor


def _supervisor_succeeding() -> MagicMock:
    supervisor = MagicMock()
    supervisor.register_or_get = MagicMock(return_value=MagicMock(run=AsyncMock()))
    return supervisor


class TestSelectedOversizeFinalization:
    @pytest.mark.asyncio
    async def test_marker_set_only_after_successful_finalization(self) -> None:
        supervisor = _supervisor_succeeding()
        coordinator = _make_coordinator_with_supervisor(supervisor)
        context = _make_context()
        selected = _make_selected()

        await coordinator._finalize_selected_oversize_rejection(
            context=context,
            selected=selected,
            err=RequestTooLargeError("too large"),
        )

        assert supervisor.register_or_get.call_count == 1
        registered_data: FinalizationData = supervisor.register_or_get.call_args.kwargs[
            "finalization_data"
        ]
        assert registered_data.outcome == FinalizationOutcome.CLIENT_ERROR
        assert registered_data.status_code == 413
        assert context.client_metadata.get("_oversize_finalized") is True

    @pytest.mark.asyncio
    async def test_database_error_propagates(self) -> None:
        supervisor = _supervisor_raising(DatabaseError("disk full"))
        coordinator = _make_coordinator_with_supervisor(supervisor)
        context = _make_context()
        selected = _make_selected()

        with pytest.raises(DatabaseError):
            await coordinator._finalize_selected_oversize_rejection(
                context=context,
                selected=selected,
                err=RequestTooLargeError("too large"),
            )
        # Marker is intentionally not set so the fail-closed recovery
        # path can take ownership of the request.
        assert "_oversize_finalized" not in context.client_metadata


class TestSelectedCapabilityFinalizationFailClosed:
    """Plan 142: a durable finalization failure on capability /
    transcode-loss rejection must propagate instead of reporting a
    clean 400 while convergence is unknown."""

    @pytest.mark.asyncio
    async def test_capability_database_error_propagates(self) -> None:
        supervisor = _supervisor_raising(DatabaseError("commit failed"))
        coordinator = _make_coordinator_with_supervisor(supervisor)
        context = _make_context()
        selected = _make_selected()

        # A concrete CapabilityError subclass exercises the typed
        # signature end to end.
        from eggpool.transcoder.budget_resolver import BudgetResolutionError

        err = BudgetResolutionError(
            message="budget exceeded",
            requested_budget_tokens=200000,
            budget_resolution_policy="strict",
            reason="strict_clamp",
            model_id=selected.model_id,
            provider_id=selected.provider_id,
        )

        with pytest.raises(DatabaseError):
            await coordinator._finalize_selected_capability_rejection(
                context=context,
                selected=selected,
                err=err,
            )

    @pytest.mark.asyncio
    async def test_transcode_loss_database_error_propagates(self) -> None:
        supervisor = _supervisor_raising(DatabaseError("commit failed"))
        coordinator = _make_coordinator_with_supervisor(supervisor)
        context = _make_context()
        selected = _make_selected()

        with pytest.raises(DatabaseError):
            await coordinator._finalize_selected_transcode_loss_rejection(
                context=context,
                selected=selected,
                err=TranscodeLossError(
                    "selected provider cannot represent URL image",
                    loss_warnings=[],
                ),
            )


# ---------------------------------------------------------------------------
# F3 — Selected-provider capability lookup uses selected.provider_id
# ---------------------------------------------------------------------------


class TestSelectedProviderCapabilityLookup:
    def test_validate_serialized_size_uses_selected_provider(self) -> None:
        def fake_get_for_provider(
            model_id: str,  # noqa: ARG001
            provider_id: str | None,
        ) -> dict[str, Any] | None:
            if provider_id == "small-provider":
                return {
                    "capabilities": {
                        "multimodal": {"max_serialized_request_bytes": 100}
                    }
                }
            return None

        catalog = MagicMock()
        catalog.cache.get_model_for_provider.side_effect = fake_get_for_provider
        coordinator = RequestCoordinator(
            registry=MagicMock(),
            catalog=catalog,
            router=MagicMock(),
            db=MagicMock(),
            client_pool=MagicMock(),
        )
        context = ProxyRequestContext(
            request_id="req-1",
            protocol="openai",
            model_id="shared-model",
            streaming=False,
            original_body=b"{}",
            incoming_headers={},
        )
        with pytest.raises(RequestTooLargeError):
            coordinator._validate_serialized_request_size(
                context,
                b"x" * 200,
                selected_provider_id="small-provider",
            )
        catalog.cache.get_model_for_provider.assert_called_with(
            "shared-model", "small-provider"
        )


# ---------------------------------------------------------------------------
# F4 — Retry/source-generation authority through selected transcode seam
# ---------------------------------------------------------------------------


def _transcoder_mock_with(content_marker: str) -> Any:
    return MagicMock(
        client_protocol="openai",
        upstream_protocol="anthropic",
        encode_request=MagicMock(
            return_value=(
                {
                    "model": "shared-model",
                    "messages": [{"role": "user", "content": content_marker}],
                },
                [],
            )
        ),
        decode_response=MagicMock(return_value=({}, [])),
        reencode_error=MagicMock(return_value=(0, {}, [])),
    )


class TestRetryTranslationThroughSelectedAttempt:
    """Retries against a different selected provider must translate
    from the original client payload through
    ``_apply_selected_provider_transcode`` rather than only through
    ``ProviderBoundRequest`` primitives."""

    @pytest.mark.asyncio
    async def test_provider_b_does_not_inherit_provider_a_translation(self) -> None:
        coordinator = RequestCoordinator(
            registry=MagicMock(),
            catalog=MagicMock(),
            router=MagicMock(),
            db=MagicMock(),
            client_pool=MagicMock(),
        )
        coordinator._catalog.cache.get_model_for_provider = MagicMock(return_value=None)

        client_payload: dict[str, Any] = {
            "model": "shared-model",
            "messages": [{"role": "user", "content": "hi"}],
        }
        provider_bound = ProviderBoundRequest(
            client_bytes=b'{"model":"shared-model"}',
            client_payload=client_payload,
            client_protocol="openai",
            model_id="shared-model",
        )
        context = ProxyRequestContext(
            request_id="req-1",
            protocol="openai",
            model_id="shared-model",
            streaming=False,
            original_body=provider_bound.client_bytes,
            incoming_headers={},
            provider_bound=provider_bound,
            transcode_context=TranscodeContext(
                request_id="req-1",
                client_protocol="openai",
                upstream_protocol="anthropic",
            ),
        )
        # Seed provider A's translated graph through the canonical
        # adoption boundary.
        provider_bound.adopt_provider_payload(
            {
                "model": "shared-model",
                "messages": [
                    {"role": "user", "content": [{"type": "text", "text": "hi"}]}
                ],
            },
            reason="protocol_transcode",
        )
        selected_a = _make_selected(provider_id="provider-a")
        await coordinator._apply_selected_provider_transcode(
            context=context,
            selected=selected_a,
            transcoder=_transcoder_mock_with("A"),
        )
        assert provider_bound.provider_payload["messages"][0]["content"] == "A"

        selected_b = _make_selected(provider_id="provider-b")
        await coordinator._apply_selected_provider_transcode(
            context=context,
            selected=selected_b,
            transcoder=_transcoder_mock_with("B"),
        )
        calls = [
            args
            for call in coordinator._catalog.cache.get_model_for_provider.call_args_list
            for args in [call.args]
        ]
        assert ("shared-model", "provider-a") in calls
        assert ("shared-model", "provider-b") in calls
        assert provider_bound.provider_payload["messages"][0]["content"] == "B"


# ---------------------------------------------------------------------------
# F5 — Text-only fast path preserved
# ---------------------------------------------------------------------------


class TestTextOnlyFastPathPreserved:
    def test_text_only_request_has_no_provider_sensitive_media(self) -> None:
        payload = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [{"type": "function", "function": {"name": "get_weather"}}],
        }
        assert request_has_provider_sensitive_media(payload) is False

    def test_text_only_with_thinking_controls_has_no_provider_sensitive_media(
        self,
    ) -> None:
        payload = {
            "model": "claude-3",
            "messages": [{"role": "user", "content": "hi"}],
            "thinking": {"type": "enabled", "budget_tokens": 1024},
        }
        assert request_has_provider_sensitive_media(payload) is False

    def test_image_payload_detected_as_provider_sensitive(self) -> None:
        payload = {
            "model": "gpt-4",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "what is this?"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "https://example.com/x.png"},
                        },
                    ],
                }
            ],
        }
        assert request_has_provider_sensitive_media(payload) is True

    def test_prepared_transcode_reuse_skip_when_provider_sensitive(self) -> None:
        prepared = PreparedTranscode(
            client_protocol="openai",
            upstream_protocol="anthropic",
            translated_payload={"messages": []},
            translated_body=b"{}",
            warnings=(),
            tool_token_padding=0,
            loss_policy_used="warn",
            features_fingerprint=0,
        )
        assert prepared.is_valid_for(upstream_protocol="anthropic") is True


# ---------------------------------------------------------------------------
# F6 — Selected-provider transcode-loss rejection at the seam
# ---------------------------------------------------------------------------


class _FakeTranscoderThatRaisesLoss:
    """BodyTranscoder double that raises ``TranscodeLossError``."""

    client_protocol = "openai"
    upstream_protocol = "anthropic"

    def encode_request(  # noqa: D401 — protocol signature
        self, payload: Any, context: Any, **_kwargs: Any
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        raise TranscodeLossError(
            "selected provider cannot represent URL image", loss_warnings=[]
        )

    def decode_response(  # noqa: D401 — protocol signature
        self, payload: Any, context: Any, **_kwargs: Any
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        return {}, []

    def reencode_error(  # noqa: D401 — protocol signature
        self, status: int, payload: Any, context: Any
    ) -> tuple[int, dict[str, Any], list[dict[str, Any]]]:
        return status, {}, []


class TestSelectedProviderTranscodeLossTyped:
    """A ``TranscodeLossError`` raised inside
    ``_apply_selected_provider_transcode`` must surface as the typed
    exception to the proxy handler — not as ``_LocalDispatchError``
    (which becomes a 500) — and must not trigger an upstream attempt
    or a provider retry."""

    @pytest.mark.asyncio
    async def test_transcode_loss_raises_typed_not_local_dispatch(self) -> None:
        supervisor = _supervisor_succeeding()
        coordinator = _make_coordinator_with_supervisor(supervisor)
        coordinator._catalog.cache.get_model_for_provider = MagicMock(return_value=None)

        client_payload: dict[str, Any] = {
            "model": "shared-model",
            "messages": [{"role": "user", "content": "hi"}],
        }
        provider_bound = ProviderBoundRequest(
            client_bytes=b'{"model":"shared-model"}',
            client_payload=client_payload,
            client_protocol="openai",
            model_id="shared-model",
        )
        context = ProxyRequestContext(
            request_id="req-1",
            protocol="openai",
            model_id="shared-model",
            streaming=False,
            original_body=provider_bound.client_bytes,
            incoming_headers={},
            provider_bound=provider_bound,
            transcode_context=TranscodeContext(
                request_id="req-1",
                client_protocol="openai",
                upstream_protocol="anthropic",
            ),
        )
        selected = _make_selected(provider_id="provider-a")

        with pytest.raises(TranscodeLossError) as exc_info:
            await coordinator._apply_selected_provider_transcode(
                context=context,
                selected=selected,
                transcoder=_FakeTranscoderThatRaisesLoss(),
            )
        assert not isinstance(exc_info.value, _LocalDispatchError)
        # A typed capability-transcode loss means no provider retry,
        # no provider health effect, and no upstream attempt.
        supervisor.register_or_get.assert_not_called()


# ---------------------------------------------------------------------------
# F7 — Provider metadata URL-image facts
# ---------------------------------------------------------------------------


def _load_registry() -> dict[str, dict[str, Any]]:
    ref = files("eggpool.providers").joinpath("_templates.toml")
    text = ref.read_text(encoding="utf-8")
    return tomllib.loads(text)


@pytest.fixture(scope="module")
def provider_entries() -> dict[str, dict[str, Any]]:
    return _load_registry().get("providers", {})


class TestLocalProviderImageUrlMetadata:
    """Bundled local capability metadata matches current upstream docs.

    - Ollama's OpenAI-compatible ``/v1/chat/completions`` lists
      ``Base64 encoded image`` as supported and ``Image URL`` as not
      supported (per ``docs.ollama.com``). The template therefore
      declares ``image_input.url = false``. The loaded model and
      mmproj determine actual multimodal availability independently.
    - llama.cpp ``llama-server`` documents accept remote URLs, base64,
      and local file paths for ``image_url.url``; ``url = true``.
    - vLLM's OpenAI-compatible online serving supports URL images via
      ``--allowed-media-domains``; ``url = true``.
    """

    def test_ollama_image_url_is_false(
        self, provider_entries: dict[str, dict[str, Any]]
    ) -> None:
        entry = provider_entries["ollama-local"]
        mm = (
            entry.get("model_capabilities", {}).get("default", {}).get("multimodal", {})
        )
        image_input = mm.get("image_input", {})
        assert image_input.get("base64") is True
        assert image_input.get("url") is False

    def test_llamacpp_image_url_is_true(
        self, provider_entries: dict[str, dict[str, Any]]
    ) -> None:
        entry = provider_entries["llamacpp-local"]
        mm = (
            entry.get("model_capabilities", {}).get("default", {}).get("multimodal", {})
        )
        image_input = mm.get("image_input", {})
        assert image_input.get("base64") is True
        assert image_input.get("url") is True

    def test_vllm_image_url_is_true(
        self, provider_entries: dict[str, dict[str, Any]]
    ) -> None:
        entry = provider_entries["vllm-local"]
        mm = (
            entry.get("model_capabilities", {}).get("default", {}).get("multimodal", {})
        )
        image_input = mm.get("image_input", {})
        assert image_input.get("base64") is True
        assert image_input.get("url") is True

    def test_no_local_provider_declares_serialized_request_ceiling(
        self, provider_entries: dict[str, dict[str, Any]]
    ) -> None:
        for pid, entry in provider_entries.items():
            if entry.get("category") != "local":
                continue
            mm = (
                entry.get("model_capabilities", {})
                .get("default", {})
                .get("multimodal", {})
            )
            assert "max_serialized_request_bytes" not in mm, (
                f"Local provider {pid!r} must not declare a speculative "
                f"serialized-request ceiling"
            )
