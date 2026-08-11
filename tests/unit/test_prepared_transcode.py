"""Tests for PreparedTranscode and single-encode invariant."""

from __future__ import annotations

import json

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
        assert prepared.features_fingerprint == "none"
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
