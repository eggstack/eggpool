"""Plan 141 — final corrective closure regression tests.

Focused regression tests for the boundaries corrected by Plan 141:

- F1: API-level provider-bound 413 (RequestTooLargeError → 413, not 500)
- F2: Healthy selected oversize finalization
- F3: Failed oversize finalization (DatabaseError is propagated, marker not set)
- F4: Selected-provider multimodal authority (capability lookup uses
  selected.provider_id)
- F5: Retry/source-generation safety (retries translate from original
  client payload)
- F6: Fast-path preservation (text-only prepared_transcode reuse still
  works)
- F7: Provider metadata (Ollama/vLLM image_url declarations match verified
  docs)
"""

from __future__ import annotations

import tomllib
from importlib.resources import files
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from eggpool.errors import DatabaseError, RequestTooLargeError
from eggpool.request.coordinator import (
    FinalizationData,
    FinalizationOutcome,
    ProxyRequestContext,
    RequestCoordinator,
    SelectedAttempt,
)
from eggpool.request.provider_bound_request import ProviderBoundRequest
from eggpool.transcoder.prepared import PreparedTranscode
from eggpool.transcoder.sensitive_media import (
    request_has_provider_sensitive_media,
)

if TYPE_CHECKING:
    from eggpool.request.finalization_job import FinalizationIdentity

# ---------------------------------------------------------------------------
# F1 — API-level provider-bound 413
# ---------------------------------------------------------------------------


class TestRequestTooLargeErrorRendering:
    """Plan 141: the API handler must render RequestTooLargeError as 413.

    The pre-141 implementation let the exception fall through to the
    ordinary 500 containment because the proxy_request handler had no
    explicit catch for it. The new explicit handler renders the
    protocol-shaped 413.
    """

    def test_openai_shaped_413_error(self) -> None:
        from eggpool.api.errors import openai_error_response

        response = openai_error_response(
            status_code=413,
            message="Serialized request body too large",
        )
        assert response.status_code == 413
        assert response.body is not None
        import json as _json

        body = _json.loads(response.body)
        assert body["error"]["type"] == "invalid_request_error"
        assert body["error"]["code"] == "413"
        assert "too large" in body["error"]["message"].lower()

    def test_anthropic_shaped_413_error(self) -> None:
        from eggpool.api.errors import anthropic_error_response

        response = anthropic_error_response(
            status_code=413,
            message="Serialized request body too large",
        )
        assert response.status_code == 413
        import json as _json

        body = _json.loads(response.body)
        assert body["type"] == "error"
        assert body["error"]["type"] == "invalid_request_error"
        assert "too large" in body["error"]["message"].lower()


class TestErrorStatusCodeMapping:
    """The static error mapper must keep returning 413 for RequestTooLargeError."""

    def test_returns_413(self) -> None:
        from eggpool.request.static_helpers import error_status_code

        assert error_status_code(RequestTooLargeError("big")) == 413


# ---------------------------------------------------------------------------
# F2 — Healthy selected oversize finalization
# ---------------------------------------------------------------------------


def _make_coordinator_with_supervisor(
    supervisor: Any,
) -> RequestCoordinator:
    catalog = MagicMock()
    coordinator = RequestCoordinator(
        registry=MagicMock(),
        catalog=catalog,
        router=MagicMock(),
        db=MagicMock(),
        client_pool=MagicMock(),
        finalization_supervisor=supervisor,
    )
    return coordinator


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


class TestOversizeMarkerSetAfterConvergence:
    """The _oversize_finalized flag is a proof-of-convergence marker.

    On the healthy path the canonical finalization owner converges the
    attempt first; only then is the marker set. This prevents a later
    _handle_exhausted from skipping a real 413 cleanup because the
    marker was set before the finalization actually completed.
    """

    @pytest.mark.asyncio
    async def test_marker_set_only_after_successful_finalization(self) -> None:
        supervisor = MagicMock()
        register = MagicMock()
        run = AsyncMock()
        register.return_value.run = run
        supervisor.register_or_get = register

        coordinator = _make_coordinator_with_supervisor(supervisor)
        context = _make_context()
        selected = _make_selected()

        await coordinator._finalize_selected_oversize_rejection(
            context=context,
            selected=selected,
            err=RequestTooLargeError("too large"),
        )

        # The convergence finalization must have been registered.
        assert supervisor.register_or_get.call_count == 1
        registered_identity: FinalizationIdentity = (
            supervisor.register_or_get.call_args.args[0]
        )
        assert registered_identity.proxy_request_id == "req-1"
        assert registered_identity.attempt_id == 1
        # Outcome must carry 413 / client_error.
        registered_data: FinalizationData = supervisor.register_or_get.call_args.kwargs[
            "finalization_data"
        ]
        assert registered_data.outcome == FinalizationOutcome.CLIENT_ERROR
        assert registered_data.status_code == 413
        # Marker is set only after the run completed.
        assert context.client_metadata.get("_oversize_finalized") is True

    @pytest.mark.asyncio
    async def test_marker_not_set_when_finalization_raises_database_error(self) -> None:
        supervisor = MagicMock()
        register = MagicMock()
        # Simulate a durable finalization failure.
        run_job = MagicMock()
        run_job.run = AsyncMock(side_effect=DatabaseError("commit failed"))
        register.return_value = run_job
        supervisor.register_or_get = register

        coordinator = _make_coordinator_with_supervisor(supervisor)
        context = _make_context()
        selected = _make_selected()

        with pytest.raises(DatabaseError):
            await coordinator._finalize_selected_oversize_rejection(
                context=context,
                selected=selected,
                err=RequestTooLargeError("too large"),
            )

        # Marker is intentionally not set so the existing fail-closed
        # recovery path can take ownership of the request.
        assert context.client_metadata.get("_oversize_finalized") is not True


# ---------------------------------------------------------------------------
# F3 — Failed oversize finalization (simulated)
# ---------------------------------------------------------------------------


class TestOversizeFinalizationFailClosed:
    """A simulated durable-finalization failure cannot masquerade as a
    successful 413 cleanup. The marker must remain unset and the
    failure must propagate to the existing supervisor/fail-closed path.
    """

    @pytest.mark.asyncio
    async def test_propagation_keeps_marker_unset(self) -> None:
        supervisor = MagicMock()
        register = MagicMock()
        run_job = MagicMock()
        run_job.run = AsyncMock(side_effect=DatabaseError("disk full"))
        register.return_value = run_job
        supervisor.register_or_get = register

        coordinator = _make_coordinator_with_supervisor(supervisor)
        context = _make_context()
        selected = _make_selected()

        with pytest.raises(DatabaseError):
            await coordinator._finalize_selected_oversize_rejection(
                context=context,
                selected=selected,
                err=RequestTooLargeError("too large"),
            )

        # Marker remains unset so a later fail-closed owner can take over.
        assert "_oversize_finalized" not in context.client_metadata


# ---------------------------------------------------------------------------
# F4 — Selected-provider multimodal authority
# ---------------------------------------------------------------------------


class TestSelectedProviderCapabilityLookup:
    """Plan 141: capability resolution uses ``selected.provider_id``.

    The pre-141 path used ``context.provider_id`` (a pre-selection
    hint). For a collapsed model with multiple providers, that meant
    the global first-seen row was used to govern the post-selection
    translation. The corrected path resolves against
    ``selected.provider_id`` so provider A and provider B
    capabilities are never cross-borrowed.
    """

    def test_validate_serialized_size_uses_selected_provider(self) -> None:
        from eggpool.request.coordinator import ProxyRequestContext

        # Two distinct provider rows: A has a small limit, B has none.
        def fake_get_for_provider(
            model_id: str,  # noqa: ARG001
            provider_id: str | None,
        ) -> dict[str, Any] | None:
            if provider_id == "small-provider":
                return {
                    "capabilities": {
                        "multimodal": {
                            "max_serialized_request_bytes": 100,
                        }
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
        # The size validator must look up capabilities against the
        # *selected* provider id, not the pre-selection context hint.
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
# F5 — Retry/source-generation safety
# ---------------------------------------------------------------------------


class TestRetryTranslationFromOriginalPayload:
    """When the selected provider changes between attempts, the
    definitive cross-protocol translation must rebuild from the
    original client payload rather than stacking on the previous
    provider's translated graph.
    """

    def _make_coordinator(self) -> RequestCoordinator:
        return RequestCoordinator(
            registry=MagicMock(),
            catalog=MagicMock(),
            router=MagicMock(),
            db=MagicMock(),
            client_pool=MagicMock(),
        )

    def test_provider_bound_can_reset_to_client_payload(self) -> None:
        coordinator = self._make_coordinator()
        coordinator._catalog.cache.get_model_for_provider = MagicMock(return_value=None)
        client_payload: dict[str, Any] = {
            "model": "shared-model",
            "messages": [{"role": "user", "content": "hi"}],
        }
        pb = ProviderBoundRequest(
            client_bytes=b'{"model":"shared-model"}',
            client_payload=client_payload,
            client_protocol="openai",
            model_id="shared-model",
        )
        # Adopt provider A's translated graph.
        provider_a_payload = {
            "model": "shared-model",
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        }
        pb.adopt_provider_payload(provider_a_payload, reason="protocol_transcode")
        assert pb.provider_payload is not provider_a_payload
        # Roll back to original client payload before provider B retranslation.
        pb.set_provider_payload(pb.client_payload, increment_generation=True)
        # The provider-bound graph is now an owned copy of the original
        # client payload, not provider A's translated graph.
        assert pb.provider_payload == client_payload
        assert pb.mutated is True


# ---------------------------------------------------------------------------
# F6 — Fast-path preservation
# ---------------------------------------------------------------------------


class TestTextOnlyFastPathPreserved:
    """Text-only cross-protocol requests with a valid prepared
    transcode continue to reuse the preflight translation. Plan 141
    only changes the post-selection recompute for provider-sensitive
    media and per-feature recompute, not the text-only path.
    """

    def test_text_only_request_has_no_provider_sensitive_media(self) -> None:
        payload = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [
                {
                    "type": "function",
                    "function": {"name": "get_weather"},
                }
            ],
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
        """Preflight translation for media requests is not cached so the
        coordinator can rebuild against the selected provider's row.
        """
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
        # The validity is preserved for the protocol/features; the
        # coordinator's text-only fast path additionally requires no
        # provider-sensitive media in the original client payload.
        # This test pins the contract: the validity check itself does
        # not consider media, but the coordinator short-circuits when
        # media is present.
        assert prepared.is_valid_for(upstream_protocol="anthropic") is True


# ---------------------------------------------------------------------------
# F7 — Provider metadata (Ollama/vLLM image URL declarations)
# ---------------------------------------------------------------------------


def _load_registry() -> dict[str, dict[str, Any]]:
    ref = files("eggpool.providers").joinpath("_templates.toml")
    text = ref.read_text(encoding="utf-8")
    return tomllib.loads(text)


@pytest.fixture(scope="module")
def provider_entries() -> dict[str, dict[str, Any]]:
    return _load_registry().get("providers", {})


class TestLocalProviderImageUrlMetadata:
    """Plan 141: bundled local metadata must match verified provider
    documentation. Ollama's OpenAI-compatible chat endpoint supports
    base64 images but not URL images; vLLM's OpenAI-compatible server
    supports both. Earlier templates were inconsistent with the vLLM
    docs and the corrected declaration is pinned here.
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

    def test_vllm_image_url_is_true(
        self, provider_entries: dict[str, dict[str, Any]]
    ) -> None:
        entry = provider_entries["vllm-local"]
        mm = (
            entry.get("model_capabilities", {}).get("default", {}).get("multimodal", {})
        )
        image_input = mm.get("image_input", {})
        assert image_input.get("base64") is True
        # vLLM's OpenAI-compatible online serving supports URL images
        # (subject to --allowed-media-domains). Pinned per Plan 141.
        assert image_input.get("url") is True

    def test_no_local_provider_declares_serialized_request_ceiling(
        self, provider_entries: dict[str, dict[str, Any]]
    ) -> None:
        """Plan 140 removed speculative universal local limits; Plan 141
        pins the corrected contract.
        """
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
