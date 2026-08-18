"""Focused tests for the Responses passthrough introduced by Plan 143.

The Responses surface is a stateless same-protocol OpenAI passthrough.
These tests verify the four narrow guarantees Plan 143 requires:

* stateless request validation rejects stateful Responses features
  (``previous_response_id``, ``conversation``, ``store = true``,
  ``background = true``) with a 400 before any upstream I/O;
* provider eligibility excludes accounts whose provider has not
  declared ``responses_path`` while leaving Chat Completions eligibility
  unchanged;
* the Responses URL is composed from ``responses_path`` via
  ``compose_provider_url()`` — there is no second URL joiner;
* Chat Completions-specific transforms (``stream_options.include_usage``
  injection) are skipped for the Responses surface;
* the streaming observer recognises ``response.completed`` as the
  terminal success event and ``response.failed`` as a terminal
  failure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from eggpool.api.errors import openai_error_response
from eggpool.api.proxy_request import (
    ProxyEndpointConfig,
    _validate_responses_stateless,
)
from eggpool.models.config import ProviderConfig
from eggpool.providers.contract import compose_provider_url
from eggpool.proxy.sse_observer import IncrementalSSEObserver
from eggpool.request.stream_completion import classify_stream_eof

# ---------------------------------------------------------------------------
# G1. Provider config/path
# ---------------------------------------------------------------------------


class TestResponsesPathConfig:
    """``responses_path`` declares Responses eligibility for a provider."""

    def test_default_is_none_means_not_eligible(self) -> None:
        cfg = ProviderConfig(id="no-responses", base_url="https://api.example.com/v1")
        assert cfg.responses_path is None

    def test_responses_path_composes_correctly(self) -> None:
        cfg = ProviderConfig(
            id="openai",
            base_url="https://api.openai.com/v1",
            responses_path="/responses",
        )
        assert compose_provider_url(cfg, cfg.responses_path or "") == (
            "https://api.openai.com/v1/responses"
        )

    def test_responses_path_with_custom_base(self) -> None:
        cfg = ProviderConfig(
            id="custom",
            base_url="https://example.com",
            responses_path="/v1/responses",
        )
        assert compose_provider_url(cfg, cfg.responses_path or "") == (
            "https://example.com/v1/responses"
        )

    def test_duplicate_version_prefix_rejected(self) -> None:
        from eggpool.errors import ConfigError

        with pytest.raises(ConfigError, match="duplicate version"):
            ProviderConfig(
                id="dup",
                base_url="https://api.example.com/v1",
                responses_path="/v1/responses",
            )

    def test_existing_chat_path_unchanged(self) -> None:
        cfg = ProviderConfig(
            id="unchanged",
            base_url="https://api.example.com/v1",
            responses_path="/responses",
        )
        assert cfg.openai_path == "/chat/completions"
        assert cfg.anthropic_path == "/messages"


# ---------------------------------------------------------------------------
# G2. Stateless admission
# ---------------------------------------------------------------------------


class TestStatelessResponsesValidation:
    """Stateful Responses fields are rejected before durable selection."""

    @pytest.mark.parametrize(
        "field_name,forbidden_value",
        [
            ("previous_response_id", "resp_abc123"),
            ("conversation", {"id": "conv_xyz"}),
            ("store", True),
            ("background", True),
        ],
    )
    def test_stateful_field_is_rejected(
        self, field_name: str, forbidden_value: object
    ) -> None:
        payload = {"model": "gpt-5", "input": "hi"}
        payload[field_name] = forbidden_value
        message = _validate_responses_stateless(payload)
        assert message is not None
        assert "not supported" in message

    def test_store_false_is_accepted(self) -> None:
        payload = {"model": "gpt-5", "input": "hi", "store": False}
        assert _validate_responses_stateless(payload) is None

    def test_empty_conversation_is_accepted(self) -> None:
        payload = {"model": "gpt-5", "input": "hi", "conversation": {}}
        assert _validate_responses_stateless(payload) is None

    def test_empty_previous_response_id_is_accepted(self) -> None:
        payload = {
            "model": "gpt-5",
            "input": "hi",
            "previous_response_id": "",
        }
        assert _validate_responses_stateless(payload) is None

    def test_minimal_stateless_payload_is_accepted(self) -> None:
        payload = {"model": "gpt-5", "input": "hi"}
        assert _validate_responses_stateless(payload) is None

    def test_responses_endpoint_config_uses_openai_error(self) -> None:
        cfg = ProxyEndpointConfig(
            protocol="openai",
            request_label="responses request",
            error_response=openai_error_response,
            not_found_error_type="invalid_request_error",
            service_error_type="server_error",
            request_surface="responses",
        )
        assert cfg.request_surface == "responses"
        assert cfg.protocol == "openai"
        assert cfg.error_response is openai_error_response


# ---------------------------------------------------------------------------
# G3. Provider eligibility filter
# ---------------------------------------------------------------------------


@dataclass
class _FakeState:
    name: str
    enabled: bool = True

    def is_eligible(self) -> bool:
        return self.enabled


@dataclass
class _FakeCatalog:
    responses_path_by_account: dict[str, str | None] = field(default_factory=dict)

    def get_provider_for_account(self, account_name: str) -> str | None:
        return self.responses_path_by_account.get(account_name, "unknown")

    def is_account_model_available(
        self,
        account_name: str,
        model_id: str,
        *,
        max_age_s: float | None = None,
        protocol: str | None = None,
    ) -> bool:
        return True

    def get_fresh_supporting_accounts(
        self, model_id: str, stale_after_s: float | None
    ) -> set[str]:
        return set()

    def get_supporting_accounts(self, model_id: str) -> set[str]:
        return set()

    def get_provider_model_entry(
        self, model_id: str, provider_id: str | None
    ) -> dict[str, Any] | None:
        return None

    def get_model_for_account(
        self, model_id: str, account_name: str
    ) -> dict[str, Any] | None:
        return None


def _account_with_responses_path(name: str, has_responses: bool) -> _FakeState:
    state = _FakeState(name=name)
    return state


def test_responses_eligibility_excludes_providers_without_responses_path() -> None:
    """A provider without ``responses_path`` cannot serve ``POST /v1/responses``."""
    from eggpool.routing.eligibility import get_eligible_accounts

    states = [
        _account_with_responses_path("chat-only", True),
        _account_with_responses_path("responses-capable", True),
    ]
    catalog = _FakeCatalog()
    catalog.responses_path_by_account = {
        "chat-only": "openai-chat",
        "responses-capable": "openai-responses",
    }

    def account_supports_request_surface(
        account_name: str, request_surface: str
    ) -> bool:
        if request_surface == "chat_completions":
            return True
        provider = catalog.responses_path_by_account.get(account_name)
        return provider == "openai-responses"

    eligible = get_eligible_accounts(
        states,
        model_id="m",
        catalog=catalog,  # type: ignore[arg-type]
        request_surface="responses",
        account_supports_request_surface=account_supports_request_surface,
    )
    assert [s.name for s in eligible] == ["responses-capable"]

    # Chat Completions still sees both providers.
    eligible_chat = get_eligible_accounts(
        states,
        model_id="m",
        catalog=catalog,  # type: ignore[arg-type]
        request_surface="chat_completions",
        account_supports_request_surface=account_supports_request_surface,
    )
    assert sorted(s.name for s in eligible_chat) == [
        "chat-only",
        "responses-capable",
    ]


def test_account_registry_supports_request_surface_predicate() -> None:
    """The registry exposes ``account_supports_request_surface`` for routing."""
    from eggpool.accounts.registry import AccountRegistry
    from eggpool.models.config import (
        AccountConfig,
        AppConfig,
        ProviderAuthConfig,
        ProviderConfig,
        ServerConfig,
    )

    config = AppConfig(
        server=ServerConfig(),
        providers={
            "openai": ProviderConfig(
                id="openai",
                base_url="https://api.openai.com/v1",
                protocols=["openai"],
                responses_path="/responses",
                accounts=[
                    AccountConfig(
                        name="primary",
                        enabled=True,
                        api_key="sk-test",
                    )
                ],
            ),
            "ollama": ProviderConfig(
                id="ollama",
                base_url="http://localhost:11434/v1",
                protocols=["openai"],
                auth=ProviderAuthConfig(mode="none"),
                accounts=[AccountConfig(name="local", enabled=True)],
            ),
        },
    )
    registry = AccountRegistry(config)
    assert registry.account_supports_request_surface("primary", "responses") is True
    assert (
        registry.account_supports_request_surface("primary", "chat_completions") is True
    )
    assert registry.account_supports_request_surface("local", "responses") is False
    assert (
        registry.account_supports_request_surface("local", "chat_completions") is True
    )


# ---------------------------------------------------------------------------
# G4. URL selection
# ---------------------------------------------------------------------------


class TestResponsesUrlResolution:
    """Responses URL is selected from ``responses_path`` via the canonical
    ``compose_provider_url()`` joiner."""

    def test_responses_url_uses_responses_path(self) -> None:
        from eggpool.request.upstream_helpers import get_upstream_url

        cfg = ProviderConfig(
            id="openai",
            base_url="https://api.openai.com/v1",
            responses_path="/responses",
        )
        config = type("_Cfg", (), {"providers": {"openai": cfg}})()

        url = get_upstream_url(
            "openai", "openai", config=config, request_surface="responses"
        )
        assert url == "https://api.openai.com/v1/responses"

    def test_chat_url_uses_openai_path(self) -> None:
        from eggpool.request.upstream_helpers import get_upstream_url

        cfg = ProviderConfig(
            id="openai",
            base_url="https://api.openai.com/v1",
            responses_path="/responses",
        )
        config = type("_Cfg", (), {"providers": {"openai": cfg}})()

        url = get_upstream_url(
            "openai", "openai", config=config, request_surface="chat_completions"
        )
        assert url == "https://api.openai.com/v1/chat/completions"

    def test_anthropic_url_unchanged(self) -> None:
        from eggpool.request.upstream_helpers import get_upstream_url

        cfg = ProviderConfig(
            id="anthropic",
            base_url="https://api.anthropic.com/v1",
            anthropic_path="/messages",
        )
        config = type("_Cfg", (), {"providers": {"anthropic": cfg}})()

        url = get_upstream_url(
            "anthropic", "anthropic", config=config, request_surface="responses"
        )
        assert url == "https://api.anthropic.com/v1/messages"

    def test_missing_responses_path_raises(self) -> None:
        from eggpool.request.upstream_helpers import get_upstream_url

        cfg = ProviderConfig(
            id="chat-only",
            base_url="https://api.example.com/v1",
        )
        config = type("_Cfg", (), {"providers": {"chat-only": cfg}})()

        with pytest.raises(RuntimeError, match="responses"):
            get_upstream_url(
                "openai",
                "chat-only",
                config=config,
                request_surface="responses",
            )


# ---------------------------------------------------------------------------
# G5. Streaming completion
# ---------------------------------------------------------------------------


class TestResponsesStreamingCompletion:
    """Responses terminal events are recognised by the SSE observer."""

    def test_response_completed_marks_stream_complete(self) -> None:
        observer = IncrementalSSEObserver(
            protocol="openai", request_surface="responses"
        )
        # Minimal Responses-style sequence.
        observer.observe(b"event: response.created\ndata: {}\n\n")
        observer.observe(b"event: response.output_text.delta\ndata: {}\n\n")
        observer.observe(b"event: response.completed\ndata: {}\n\n")
        observer.finish()

        snapshot = observer.completion_snapshot
        assert snapshot.saw_terminal_event is True
        assert snapshot.terminal_kind == "openai_done"

    def test_eof_without_terminal_event_is_not_complete(self) -> None:
        observer = IncrementalSSEObserver(
            protocol="openai", request_surface="responses"
        )
        observer.observe(b"event: response.created\ndata: {}\n\n")
        observer.observe(b"event: response.output_text.delta\ndata: {}\n\n")
        observer.finish()

        snapshot = observer.completion_snapshot
        assert snapshot.saw_terminal_event is False

    def test_response_failed_marks_terminal_failure(self) -> None:
        observer = IncrementalSSEObserver(
            protocol="openai", request_surface="responses"
        )
        observer.observe(b"event: response.created\ndata: {}\n\n")
        observer.observe(b"event: response.failed\ndata: {}\n\n")
        observer.finish()

        snapshot = observer.completion_snapshot
        # ``response.failed`` is treated as a terminal failure, not
        # canonical success; the strict-completion policy below
        # classifies the stream as malformed.
        assert snapshot.saw_terminal_event is True
        assert snapshot.terminal_kind == "openai_responses_failed"

    def test_classify_stream_eof_treats_responses_completed_as_complete(self) -> None:
        observer = IncrementalSSEObserver(
            protocol="openai", request_surface="responses"
        )
        observer.observe(b"event: response.created\ndata: {}\n\n")
        observer.observe(b"event: response.completed\ndata: {}\n\n")
        observer.finish()
        snapshot = observer.completion_snapshot
        decision = classify_stream_eof(
            protocol="openai",
            policy="strict",
            snapshot=snapshot,
            downstream_started=True,
        )
        assert decision.classification == "complete"

    def test_classify_stream_eof_treats_eof_without_terminal_as_premature(
        self,
    ) -> None:
        observer = IncrementalSSEObserver(
            protocol="openai", request_surface="responses"
        )
        observer.observe(b"event: response.created\ndata: {}\n\n")
        observer.observe(b"event: response.output_text.delta\ndata: {}\n\n")
        observer.finish()
        snapshot = observer.completion_snapshot
        decision = classify_stream_eof(
            protocol="openai",
            policy="strict",
            snapshot=snapshot,
            downstream_started=True,
        )
        assert decision.classification == "premature_eof"

    def test_response_incomplete_also_marks_completion(self) -> None:
        """Some Responses providers emit ``response.incomplete`` for
        partial responses. The observer treats it as terminal evidence."""
        observer = IncrementalSSEObserver(
            protocol="openai", request_surface="responses"
        )
        observer.observe(b"event: response.created\ndata: {}\n\n")
        observer.observe(b"event: response.incomplete\ndata: {}\n\n")
        observer.finish()
        snapshot = observer.completion_snapshot
        assert snapshot.saw_terminal_event is True
        assert snapshot.terminal_kind == "openai_done"

    def test_response_completion_requires_surface_flag(self) -> None:
        """Without ``request_surface=responses``, terminal events are not
        recognised and the stream falls through to the Chat markerless
        default."""
        observer = IncrementalSSEObserver(protocol="openai")
        observer.observe(b"event: response.completed\ndata: {}\n\n")
        observer.finish()
        snapshot = observer.completion_snapshot
        assert snapshot.saw_terminal_event is False


# ---------------------------------------------------------------------------
# Chat Completions transforms skipped for the Responses surface
# ---------------------------------------------------------------------------


class TestStreamOptionsTransform:
    """``stream_options.include_usage`` is a Chat Completions transform."""

    def test_stream_options_injected_for_chat_completions(self) -> None:
        from eggpool.request.provider_bound_request import ProviderBoundRequest
        from eggpool.request.transform_pipeline import (
            TransformContext,
            run_transform_pipeline,
        )

        bound = ProviderBoundRequest(
            client_bytes=b"{}",
            client_payload={"model": "gpt-5", "stream": True},
            client_protocol="openai",
            model_id="gpt-5",
        )
        bound.set_provider_payload(dict(bound.client_payload))
        ctx = TransformContext(
            upstream_protocol="openai",
            request_id="req-1",
            proxy_context=type(
                "_P",
                (),
                {
                    "streaming": True,
                    "request_surface": "chat_completions",
                    "client_metadata": {},
                },
            )(),
        )
        result = run_transform_pipeline(
            bound,
            ctx,
            # Only run the stream_options adapter for this focused test.
            [
                (
                    __import__(
                        "eggpool.request.transform_pipeline",
                        fromlist=["_make_stream_options_adapter"],
                    )._make_stream_options_adapter()
                )
            ],
        )
        assert "stream_options" in bound.provider_payload
        assert bound.provider_payload["stream_options"]["include_usage"] is True
        assert result.transformed is True

    def test_stream_options_skipped_for_responses(self) -> None:
        from eggpool.request.provider_bound_request import ProviderBoundRequest
        from eggpool.request.transform_pipeline import (
            TransformContext,
            run_transform_pipeline,
        )

        bound = ProviderBoundRequest(
            client_bytes=b"{}",
            client_payload={"model": "gpt-5", "stream": True},
            client_protocol="openai",
            model_id="gpt-5",
        )
        bound.set_provider_payload(dict(bound.client_payload))
        ctx = TransformContext(
            upstream_protocol="openai",
            request_id="req-1",
            proxy_context=type(
                "_P",
                (),
                {
                    "streaming": True,
                    "request_surface": "responses",
                    "client_metadata": {},
                },
            )(),
        )
        run_transform_pipeline(
            bound,
            ctx,
            [
                (
                    __import__(
                        "eggpool.request.transform_pipeline",
                        fromlist=["_make_stream_options_adapter"],
                    )._make_stream_options_adapter()
                )
            ],
        )
        assert "stream_options" not in bound.provider_payload


# ---------------------------------------------------------------------------
# Output limits — ``max_output_tokens`` recognised for Responses
# ---------------------------------------------------------------------------


class TestResponsesOutputLimits:
    def test_max_output_tokens_recognised_for_responses(self) -> None:
        from eggpool.request.limits import requested_output_tokens

        payload = {"max_output_tokens": 4096}
        assert (
            requested_output_tokens(payload, "openai", request_surface="responses")
            == 4096
        )

    def test_max_output_tokens_ignored_for_chat_completions(self) -> None:
        from eggpool.request.limits import requested_output_tokens

        payload = {"max_output_tokens": 4096}
        # ``max_output_tokens`` is a Responses-only key; Chat Completions
        # requires ``max_completion_tokens`` or ``max_tokens``.
        assert (
            requested_output_tokens(
                payload, "openai", request_surface="chat_completions"
            )
            is None
        )
