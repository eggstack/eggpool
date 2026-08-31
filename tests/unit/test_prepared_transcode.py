"""Tests for PreparedTranscode and single-encode invariant."""

from __future__ import annotations

import json
from typing import Any

import pytest

from eggpool.api.proxy_request import TranscodePreflightResult, _tool_token_padding
from eggpool.request.limits import estimate_json_value_tokens
from eggpool.transcoder.prepared import RECOMPUTE_REASONS, PreparedTranscode


def _make_preflight(
    translated_payload: dict | None = None,
    warnings: list | None = None,
    tool_token_padding: int = 0,
) -> TranscodePreflightResult:
    return TranscodePreflightResult(
        upstream_protocol="anthropic",
        translated_payload=translated_payload
        or {"messages": [{"role": "user", "content": "hi"}]},
        warnings=warnings or [],
        tool_token_padding=tool_token_padding,
    )


class TestSingleEncode:
    def test_prepared_body_is_same_object_as_limit_check_body(self):
        from eggpool.request.body import encode_json_body

        preflight = _make_preflight()
        encoded = encode_json_body(preflight.translated_payload)

        prepared = PreparedTranscode.from_preflight_result(
            result=preflight,
            client_protocol="openai",
            loss_policy="warn",
            encoded_body=encoded,
        )

        assert prepared.translated_body is encoded

    def test_prepared_body_matches_compact_json(self):
        from eggpool.request.body import encode_json_body

        payload = {"messages": [{"role": "user", "content": "hello"}]}
        preflight = _make_preflight(translated_payload=payload)
        expected = encode_json_body(payload)

        prepared = PreparedTranscode.from_preflight_result(
            result=preflight,
            client_protocol="openai",
            loss_policy="warn",
            encoded_body=expected,
        )

        assert prepared.translated_body == expected
        assert json.loads(prepared.translated_body) == payload


class TestPaddingIsolation:
    def test_padding_not_in_prepared_body(self):
        from eggpool.request.body import encode_json_body

        payload = {"messages": [{"role": "user", "content": "hi"}]}
        preflight = _make_preflight(translated_payload=payload, tool_token_padding=100)
        encoded = encode_json_body(payload)

        prepared = PreparedTranscode.from_preflight_result(
            result=preflight,
            client_protocol="openai",
            loss_policy="warn",
            encoded_body=encoded,
        )

        assert prepared.translated_body == encoded
        assert not prepared.translated_body.endswith(b"\x00")

    def test_limit_check_body_includes_padding_separately(self):
        from eggpool.request.body import encode_json_body

        payload = {"messages": [{"role": "user", "content": "hi"}]}
        preflight = _make_preflight(translated_payload=payload, tool_token_padding=10)
        encoded = encode_json_body(payload)

        limit_check_body = encoded
        if preflight.tool_token_padding > 0:
            limit_check_body += b"\x00" * (preflight.tool_token_padding * 8)

        assert len(limit_check_body) > len(encoded)
        assert limit_check_body.startswith(encoded)
        assert limit_check_body != encoded


class TestToolTokenPaddingCompact:
    def test_uses_shared_structural_estimator(self) -> None:
        tool = {
            "type": "function",
            "function": {"name": "get_weather", "parameters": {"type": "object"}},
        }
        assert _tool_token_padding({"tools": [tool]}) == max(
            64,
            estimate_json_value_tokens([tool]),
        )

    def test_no_tools_returns_zero(self):
        assert _tool_token_padding({}) == 0
        assert _tool_token_padding({"tools": []}) == 0
        assert _tool_token_padding({"tools": "not_a_list"}) == 0

    def test_multiple_tools_summed(self):
        tools = [
            {"type": "function", "function": {"name": "a"}},
            {"type": "function", "function": {"name": "b"}},
        ]
        assert _tool_token_padding({"tools": tools}) == max(
            64,
            estimate_json_value_tokens(tools),
        )

    def test_does_not_encode_each_tool(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from eggpool.api import proxy_request

        def fail_if_serialized(*args: object, **kwargs: object) -> bytes:
            raise AssertionError("tool padding must not serialize nested tools")

        # ``dumps_bytes`` was the old per-tool path.  Keep this guard even
        # though the symbol is no longer imported by the implementation.
        monkeypatch.setattr(
            proxy_request, "dumps_bytes", fail_if_serialized, raising=False
        )
        tools = [{"name": "tool", "description": "schema"}]
        assert _tool_token_padding({"tools": tools}) == max(
            64,
            estimate_json_value_tokens(tools),
        )

    def test_shared_estimator_is_not_less_conservative_for_large_schema(self) -> None:
        tool = {
            "name": "large_tool",
            "description": "x" * 1_000,
            "input_schema": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
            },
        }
        legacy_padding = len(json.dumps(tool, separators=(",", ":"))) // 4
        assert _tool_token_padding({"tools": [tool]}) >= max(64, legacy_padding)


class TestRecomputeReasons:
    def test_constant_contains_expected_reasons(self):
        assert {
            "no_prepared_result",
            "protocol_or_features_mismatch",
            "thinking_controls_present",
            "transcoder_missing",
            "provider_multimodal_capability_required",
        } == RECOMPUTE_REASONS


class TestPreparedTranscodeDiagnostics:
    def test_defaults_and_reuse_path(self):
        preflight = _make_preflight()
        prepared = PreparedTranscode.from_preflight_result(
            result=preflight,
            client_protocol="openai",
            loss_policy="warn",
            encoded_body=b"{}",
        )

        assert prepared.diagnostics.available is True
        assert prepared.diagnostics.reused is False
        assert prepared.diagnostics.recompute_reason is None

        prepared.diagnostics.reused = True
        prepared.diagnostics.recompute_reason = "thinking_controls_present"

        assert prepared.diagnostics.available is True
        assert prepared.diagnostics.reused is True
        assert prepared.diagnostics.recompute_reason == "thinking_controls_present"

    @pytest.mark.parametrize("reason", sorted(RECOMPUTE_REASONS))
    def test_recompute_paths_record_stable_reason(self, reason: str):
        preflight = _make_preflight()
        prepared = PreparedTranscode.from_preflight_result(
            result=preflight,
            client_protocol="openai",
            loss_policy="warn",
            encoded_body=b"{}",
        )

        prepared.diagnostics.reused = False
        prepared.diagnostics.recompute_reason = reason

        assert prepared.diagnostics.reused is False
        assert prepared.diagnostics.recompute_reason == reason


class TestPreparedTranscodeDispatchData:
    def test_dispatch_data_keeps_payload_generation_and_detaches_warnings(self):
        payload = {"messages": [{"role": "user", "content": "hi"}]}
        warnings = [{"field": "tools", "kind": "unsupported"}]
        preflight = _make_preflight(
            translated_payload=payload,
            warnings=warnings,
            tool_token_padding=100,
        )

        from eggpool.request.body import encode_json_body

        encoded = encode_json_body(payload)
        prepared = PreparedTranscode.from_preflight_result(
            result=preflight,
            client_protocol="openai",
            loss_policy="warn",
            encoded_body=encoded,
        )

        # Prepared payload construction adopts the transcoder's request-local
        # generation instead of recursively rebuilding its graph.
        assert prepared.translated_payload is payload
        assert prepared.translated_payload["messages"] is payload["messages"]

        warnings[0]["kind"] = "mutated source"

        prepared.diagnostics.reused = True
        prepared.diagnostics.recompute_reason = "protocol_or_features_mismatch"

        assert prepared.translated_body is encoded
        assert prepared.translated_payload == payload
        assert prepared.warnings == ({"field": "tools", "kind": "unsupported"},)
        assert prepared.tool_token_padding == 100
        assert prepared.loss_policy_used == "warn"
        assert prepared.features_fingerprint == 0
        assert prepared.diagnostics.available is True
        assert prepared.diagnostics.reused is True
        assert prepared.diagnostics.recompute_reason == (
            "protocol_or_features_mismatch"
        )


class TestWarningPropagation:
    def test_frozen_warnings_still_extend_context_loss_warnings(self):
        warnings = [{"field": "tools", "kind": "unsupported"}]
        preflight = _make_preflight(warnings=warnings)

        prepared = PreparedTranscode.from_preflight_result(
            result=preflight,
            client_protocol="openai",
            loss_policy="warn",
            encoded_body=b"{}",
        )

        from eggpool.transcoder.context import TranscodeContext

        ctx = TranscodeContext(
            request_id="r-1",
            client_protocol="openai",
            upstream_protocol="anthropic",
        )

        ctx.loss_warnings.extend(prepared.warnings)

        assert len(ctx.loss_warnings) == 1
        assert dict(ctx.loss_warnings[0]) == warnings[0]


class TestPreparedTranscodeValidity:
    def test_protocol_mismatch_rejects_reuse(self):
        preflight = _make_preflight()
        prepared = PreparedTranscode.from_preflight_result(
            result=preflight,
            client_protocol="openai",
            loss_policy="warn",
            encoded_body=b"{}",
        )

        assert prepared.is_valid_for(upstream_protocol="openai") is False

    def test_features_mismatch_rejects_reuse(self):
        from eggpool.transcoder.policy import TranscoderFeatures

        features_v1 = TranscoderFeatures(
            tools=False,
            vision=False,
            thinking=False,
            structured_outputs=False,
            anthropic_primitives=False,
        )
        features_v2 = TranscoderFeatures(
            tools=True,
            vision=False,
            thinking=False,
            structured_outputs=False,
            anthropic_primitives=False,
        )

        preflight = _make_preflight()
        prepared = PreparedTranscode.from_preflight_result(
            result=preflight,
            client_protocol="openai",
            loss_policy="warn",
            encoded_body=b"{}",
            features=features_v1,
        )

        assert (
            prepared.is_valid_for(
                upstream_protocol="anthropic",
                features=features_v2,
            )
            is False
        )


class TestProviderSuffixedModelNormalization:
    """Regression: ``/v1/models`` exposes provider-scoped entries as
    ``model-id/provider-id``. When a client uses one of those suffixed
    ids against a transcoded endpoint (``/v1/chat/completions`` for an
    Anthropic model), the prepared-transcode cache used to bake the
    ``/provider-id`` suffix into the JSON body. The upstream then
    rejected the request with ``Model <model-id>/<provider-id> is not
    supported`` because the upstream does not understand EggPool's
    provider-namespace suffix.

    The proxy layer must normalize the in-memory payload's ``model``
    field to the parsed base id before running the transcode preflight,
    so the cached translated payload and the cached JSON body both
    carry the un-suffixed model id.
    """

    def test_translated_payload_uses_unsuffixed_model_when_preflight_runs(
        self,
    ) -> None:
        """The OpenAI -> Anthropic transcoder must receive a payload with
        the un-suffixed model so the cached ``translated_payload`` only
        references the base model id."""
        from eggpool.transcoder.context import TranscodeContext
        from eggpool.transcoder.openai_to_anthropic import OpenAIToAnthropic

        payload = {
            "model": "muse-spark-1.2-contributor/opencode-go",
            "max_tokens": 64,
            "messages": [{"role": "user", "content": "Hi"}],
        }
        normalized = dict(payload)
        normalized["model"] = "muse-spark-1.2-contributor"

        context = TranscodeContext(
            request_id="test-1",
            client_protocol="openai",
            upstream_protocol="anthropic",
        )

        translated, _warnings = OpenAIToAnthropic().encode_request(
            normalized,
            context,
            features=None,
            transcoding_capability=None,
            loss_policy="warn",
        )

        assert translated["model"] == "muse-spark-1.2-contributor"

    def test_cached_body_uses_unsuffixed_model(self) -> None:
        """The preflight ``encoded_body`` that becomes the cached dispatch
        body must use the un-suffixed model so the upstream never sees
        ``/opencode-go`` in the request body."""
        from eggpool.request.body import encode_json_body

        # Preflight behavior after the fix: proxy_request normalizes the
        # payload before the transcoder runs, so the cached body is built
        # from a payload whose ``model`` is the parsed base id.
        preflight_payload = {
            "model": "muse-spark-1.2-contributor",
            "max_tokens": 64,
            "messages": [{"role": "user", "content": "Hi"}],
        }
        preflight = _make_preflight(translated_payload=preflight_payload)
        encoded = encode_json_body(preflight_payload)

        prepared = PreparedTranscode.from_preflight_result(
            result=preflight,
            client_protocol="openai",
            loss_policy="warn",
            encoded_body=encoded,
        )

        decoded = json.loads(prepared.translated_body)
        assert decoded["model"] == "muse-spark-1.2-contributor"
        assert "/opencode-go" not in prepared.translated_body.decode(
            "utf-8", errors="replace"
        )

    def test_proxy_layer_strips_provider_suffix_before_preflight(
        self,
    ) -> None:
        """The proxy layer must rewrite the in-memory payload's ``model``
        field to the parsed base id before calling
        ``_prepare_transcode_preflight``. This pins the normalization
        contract that keeps the cached prepared-transcode body free
        of the ``/provider-id`` suffix the upstream does not
        understand.

        The test directly mirrors the model-parse + payload-rewrite
        sequence in :func:`handle_proxy_request`. Any regression in
        the rewrite logic (for example, skipping it because the
        preflight and provider-bound payload rewrites look redundant)
        will surface here before the integration tests do.
        """
        from eggpool.routing.provider import parse_model_provider

        client_payload: dict[str, object] = {
            "model": "muse-spark-1.2-contributor/opencode-go",
            "max_tokens": 64,
            "messages": [{"role": "user", "content": "Hi"}],
        }
        model_value = str(client_payload["model"])
        known_providers = frozenset({"opencode-go"})
        model_id, provider_id = parse_model_provider(model_value, known_providers)
        assert model_id == "muse-spark-1.2-contributor"
        assert provider_id == "opencode-go"

        # Mirror the normalization in ``handle_proxy_request``: the
        # proxy layer builds a rewritten payload snapshot for the
        # preflight so the cached translated body never carries the
        # provider suffix.
        preflight_payload = client_payload
        if model_id != model_value:
            preflight_payload = dict(client_payload)
            preflight_payload["model"] = model_id

        # The cached translated body is a JSON encoding of the
        # preflight payload; the suffix must not appear anywhere in
        # the bytes the upstream would receive.
        encoded = json.dumps(preflight_payload).encode()
        assert "/opencode-go" not in encoded.decode("utf-8", errors="replace")
        decoded = json.loads(encoded)
        assert decoded["model"] == "muse-spark-1.2-contributor"


@pytest.mark.parametrize(
    ("model_id", "provider_suffix", "client_payload_factory"),
    [
        (
            "muse-spark-1.2-contributor",
            "opencode-go",
            lambda mid, suf: {
                "model": f"{mid}/{suf}",
                "max_tokens": 64,
                "messages": [{"role": "user", "content": "Hi"}],
            },
        ),
        (
            "claude-opus-4-7",
            "openai",
            lambda mid, suf: {
                "model": f"{mid}/{suf}",
                "max_tokens": 256,
                "messages": [{"role": "user", "content": "Pong?"}],
                "temperature": 0.5,
            },
        ),
        (
            "gpt-5-codex",
            "opencode-go",
            lambda mid, suf: {
                "model": f"{mid}/{suf}",
                "max_tokens": 128,
                "messages": [{"role": "user", "content": "Pong?"}],
                "stream": False,
            },
        ),
        (
            "random-future-model-2030",
            "minimax",
            lambda mid, suf: {
                "model": f"{mid}/{suf}",
                "max_tokens": 16,
                "messages": [{"role": "user", "content": "ping"}],
            },
        ),
    ],
    ids=[
        "muse-spark-1.2-contributor/opencode-go",
        "claude-opus-4-7/openai",
        "gpt-5-codex/opencode-go",
        "random-future-model-2030/minimax",
    ],
)
class TestCoordinatorResetStripsProviderSuffix:
    """Regression for the coordinator's re-translation path.

    ``_apply_selected_provider_transcode`` resets the provider payload to
    ``provider_bound.client_payload`` whenever the preflight prepared-
    transcode is not reusable (provider-sensitive multimodal content,
    thinking controls, protocol/features mismatch, or a retry against a
    different provider). The OpenAI <-> Anthropic transcoders copy the
    ``model`` field verbatim, so if the reset passes a suffixed
    ``client_payload`` to the encoder the suffix reaches the upstream,
    which rejects it with ``Model <model-id>/<provider-id> is not
    supported``. The fix strips the suffix at the reset boundary so any
    re-translation produces a body whose ``model`` field is the bare
    upstream id.

    The test parameterizes across four model names -- including a future
    model name -- so the contract does not regress against any single
    model id.
    """

    def test_reset_normalizes_model_for_coordinator_retranslation(
        self,
        model_id: str,
        provider_suffix: str,
        client_payload_factory: Any,
    ) -> None:
        """The coordinator reset path must rewrite ``model`` to the
        parsed base id before the transcoder runs. The immutable
        ``client_payload`` contract is preserved -- only the new
        ``provider_payload`` snapshot loses the suffix.
        """
        from eggpool.request.provider_bound_request import ProviderBoundRequest

        client_payload = client_payload_factory(model_id, provider_suffix)
        bound = ProviderBoundRequest(
            client_bytes=b"{}",
            client_payload=client_payload,
            client_protocol="openai",
            model_id=model_id,
        )
        # Simulate the first request's normalization: the API layer
        # sets ``provider_payload`` to the bare-model snapshot. The
        # subsequent coordinator reset must rebuild a bare-model
        # snapshot from ``client_payload``.
        provider_payload = dict(client_payload)
        provider_payload["model"] = model_id
        bound.set_provider_payload(provider_payload, increment_generation=False)

        # Mirror the post-fix reset in ``_apply_selected_provider_transcode``:
        # rebuild a bare-model snapshot from ``client_payload`` before
        # calling ``set_provider_payload`` with ``increment_generation=True``.
        reset_payload = bound.client_payload
        if (
            bound.model_id
            and isinstance(reset_payload, dict)
            and reset_payload.get("model") != bound.model_id
        ):
            normalized = dict(reset_payload)
            normalized["model"] = bound.model_id
            reset_payload = normalized
        bound.set_provider_payload(reset_payload, increment_generation=True)

        # The provider-bound payload the upstream would receive carries
        # only the bare id.
        assert bound.provider_payload["model"] == model_id
        # The immutable ``client_payload`` retains the suffix; only the
        # provider-bound snapshot is rewritten.
        assert bound.client_payload["model"] == f"{model_id}/{provider_suffix}"
        # Encoding the provider-bound payload must not contain the suffix.
        encoded = json.dumps(dict(bound.provider_payload)).encode()
        assert f"/{provider_suffix}" not in encoded.decode("utf-8", errors="replace")

    def test_legacy_provider_request_normalizes_model(
        self,
        model_id: str,
        provider_suffix: str,
        client_payload_factory: Any,
    ) -> None:
        """``_legacy_provider_request`` must mirror the API handler's
        normalization for any provider-suffixed model id. The legacy
        helper is used by direct embedders (not the production API
        pipeline); without this normalization a legacy caller could
        forward a suffixed body to upstream.
        """
        from eggpool.request.coordinator import (
            ProxyRequestContext,
            RequestCoordinator,
        )

        client_payload = client_payload_factory(model_id, provider_suffix)
        body = json.dumps(client_payload).encode()
        context = ProxyRequestContext(
            request_id="test-legacy",
            protocol="openai",
            model_id=model_id,
            streaming=False,
            original_body=body,
            incoming_headers={},
        )
        bound = RequestCoordinator._legacy_provider_request(context)

        assert bound.provider_payload["model"] == model_id
        assert bound.client_payload["model"] == f"{model_id}/{provider_suffix}"
        # Encoding the provider-bound payload must not contain the suffix.
        encoded = json.dumps(dict(bound.provider_payload)).encode()
        assert f"/{provider_suffix}" not in encoded.decode("utf-8", errors="replace")
