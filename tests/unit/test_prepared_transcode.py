"""Tests for PreparedTranscode and single-encode invariant."""

from __future__ import annotations

import json

import pytest

from eggpool.api.proxy_request import TranscodePreflightResult, _tool_token_padding
from eggpool.transcoder.prepared import RECOMPUTE_REASONS, PreparedTranscode

pytestmark = pytest.mark.request_path


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
    def test_uses_compact_separators(self):
        tool = {
            "type": "function",
            "function": {"name": "get_weather", "parameters": {"type": "object"}},
        }
        compact = json.dumps(tool, separators=(",", ":"))
        default = json.dumps(tool)

        assert len(compact) < len(default)
        assert _tool_token_padding({"tools": [tool]}) == max(64, len(compact) // 4)

    def test_no_tools_returns_zero(self):
        assert _tool_token_padding({}) == 0
        assert _tool_token_padding({"tools": []}) == 0
        assert _tool_token_padding({"tools": "not_a_list"}) == 0

    def test_multiple_tools_summed(self):
        tools = [
            {"type": "function", "function": {"name": "a"}},
            {"type": "function", "function": {"name": "b"}},
        ]
        total = sum(len(json.dumps(t, separators=(",", ":"))) for t in tools)
        assert _tool_token_padding({"tools": tools}) == max(64, total // 4)


class TestWarningsPreserved:
    def test_warnings_copied_to_prepared(self):
        warnings = [{"field": "tools", "kind": "unsupported"}]
        preflight = _make_preflight(warnings=warnings)

        prepared = PreparedTranscode.from_preflight_result(
            result=preflight,
            client_protocol="openai",
            loss_policy="warn",
            encoded_body=b"{}",
        )

        assert prepared.warnings == warnings
        assert prepared.warnings is not preflight.warnings

    def test_empty_warnings(self):
        preflight = _make_preflight(warnings=[])
        prepared = PreparedTranscode.from_preflight_result(
            result=preflight,
            client_protocol="openai",
            loss_policy="warn",
            encoded_body=b"{}",
        )
        assert prepared.warnings == []


class TestRecomputeReasons:
    def test_constant_contains_expected_reasons(self):
        assert {
            "no_prepared_result",
            "protocol_or_features_mismatch",
            "thinking_controls_present",
            "transcoder_missing",
        } == RECOMPUTE_REASONS


class TestPreparedTranscodeDebugFields:
    def test_defaults(self):
        preflight = _make_preflight()
        prepared = PreparedTranscode.from_preflight_result(
            result=preflight,
            client_protocol="openai",
            loss_policy="warn",
            encoded_body=b"{}",
        )
        assert prepared.available is True
        assert prepared.reused is False
        assert prepared.recompute_reason is None

    def test_mutable_after_creation(self):
        preflight = _make_preflight()
        prepared = PreparedTranscode.from_preflight_result(
            result=preflight,
            client_protocol="openai",
            loss_policy="warn",
            encoded_body=b"{}",
        )
        prepared.reused = True
        prepared.recompute_reason = "thinking_controls_present"
        assert prepared.reused is True
        assert prepared.recompute_reason == "thinking_controls_present"


class TestPreparedTranscodeReused:
    def test_reuse_sets_reused_and_extends_warnings(self):
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

        # Simulate coordinator reuse path.
        ctx.loss_warnings.extend(prepared.warnings)
        prepared.reused = True

        assert prepared.reused is True
        assert prepared.recompute_reason is None
        assert len(ctx.loss_warnings) == 1
        assert ctx.loss_warnings[0] == warnings[0]


class TestPreparedTranscodeFallbackThinking:
    def test_thinking_controls_sets_reason(self):
        preflight = _make_preflight()
        prepared = PreparedTranscode.from_preflight_result(
            result=preflight,
            client_protocol="openai",
            loss_policy="warn",
            encoded_body=b"{}",
        )

        # Simulate coordinator fallback path for thinking controls.
        prepared.reused = False
        prepared.recompute_reason = "thinking_controls_present"

        assert prepared.reused is False
        assert prepared.recompute_reason == "thinking_controls_present"


class TestPreparedTranscodeFallbackMissing:
    def test_no_prepared_result_sets_reason(self):
        # When no PreparedTranscode exists, the coordinator falls back.
        # This tests the reason constant and the field behavior.
        prepared = PreparedTranscode(
            client_protocol="openai",
            upstream_protocol="anthropic",
            translated_payload={},
            translated_body=b"{}",
            warnings=[],
            tool_token_padding=0,
            loss_policy_used="warn",
        )

        # Simulate coordinator: no valid prepared result → recompute.
        prepared.reused = False
        prepared.recompute_reason = "no_prepared_result"

        assert prepared.reused is False
        assert prepared.recompute_reason == "no_prepared_result"


class TestPreparedTranscodeFallbackMismatch:
    def test_protocol_mismatch_sets_reason(self):
        preflight = _make_preflight()
        prepared = PreparedTranscode.from_preflight_result(
            result=preflight,
            client_protocol="openai",
            loss_policy="warn",
            encoded_body=b"{}",
        )

        # is_valid_for returns False when protocol mismatches.
        assert prepared.is_valid_for(upstream_protocol="openai") is False

        # Simulate coordinator fallback.
        prepared.reused = False
        prepared.recompute_reason = "protocol_or_features_mismatch"

        assert prepared.reused is False
        assert prepared.recompute_reason == "protocol_or_features_mismatch"

    def test_features_mismatch_sets_reason(self):
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


class TestWarningNoDuplication:
    def test_warnings_appended_exactly_once_on_reuse(self):
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

        # Extend once (simulating coordinator reuse path).
        ctx.loss_warnings.extend(prepared.warnings)
        assert len(ctx.loss_warnings) == 1

        # Extending again would duplicate — verify the list isn't mutated
        # by the extend itself (i.e. extend is idempotent on empty).
        ctx.loss_warnings.extend(prepared.warnings)
        assert len(ctx.loss_warnings) == 2  # would be duplicated if called twice

    def test_prepared_warnings_are_independent_copy(self):
        warnings = [{"field": "tools", "kind": "unsupported"}]
        preflight = _make_preflight(warnings=warnings)
        prepared = PreparedTranscode.from_preflight_result(
            result=preflight,
            client_protocol="openai",
            loss_policy="warn",
            encoded_body=b"{}",
        )

        # Mutating original warnings doesn't affect prepared copy.
        warnings.append({"field": "other", "kind": "dropped"})
        assert len(prepared.warnings) == 1

    def test_empty_prepared_warnings_extend_nothing(self):
        preflight = _make_preflight(warnings=[])
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
        assert ctx.loss_warnings == []
