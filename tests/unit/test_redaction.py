"""Unit tests for the security.redaction module.

Phase 18 switches the structured sanitization policy from
blocklist to allowlist. The redactor retains only a small set
of diagnostic JSON fields, drops arbitrary provider payload
keys (e.g. ``payload``, ``body``, ``context``, ``data``,
``details``, ``debug``), collapses top-level arrays to
``[REDACTED]`` (fail-closed), and bounds the helper output.
"""

from __future__ import annotations

import json

from eggpool.security.redaction import (
    MAX_REDACTED_ERROR_DETAIL_CHARS,
    REDACTED,
    SAFE_JSON_KEYS,
    SENSITIVE_JSON_KEYS,
    USER_CONTENT_JSON_KEYS,
    redact_error_detail,
    sanitize_error_object,
)


class TestRedactErrorDetailRegex:
    """Original regex-based redaction patterns still apply."""

    def test_none_and_empty_passthrough(self) -> None:
        assert redact_error_detail(None) is None
        assert redact_error_detail("") == ""

    def test_authorization_header(self) -> None:
        result = redact_error_detail("Authorization: Bearer secret-token-123")
        assert "secret-token-123" not in (result or "")
        assert REDACTED in (result or "")

    def test_bearer_token(self) -> None:
        result = redact_error_detail("token was Bearer abc.def.ghi")
        assert "abc.def.ghi" not in (result or "")
        assert REDACTED in (result or "")

    def test_sk_key(self) -> None:
        result = redact_error_detail("api key is sk-abcdef012345")
        assert "sk-abcdef012345" not in (result or "")

    def test_password_keyvalue(self) -> None:
        result = redact_error_detail("password=hunter2")
        assert "hunter2" not in (result or "")

    def test_secret_keyvalue(self) -> None:
        result = redact_error_detail("secret=topsecret")
        assert "topsecret" not in (result or "")

    def test_api_key_keyvalue(self) -> None:
        result = redact_error_detail("api_key=supersecret")
        assert "supersecret" not in (result or "")

    def test_prompt_field(self) -> None:
        result = redact_error_detail('"prompt": "private text"')
        assert "private text" not in (result or "")
        assert REDACTED in (result or "")

    def test_completion_field(self) -> None:
        result = redact_error_detail('"completion": "private text"')
        assert "private text" not in (result or "")
        assert REDACTED in (result or "")

    def test_url_userinfo(self) -> None:
        result = redact_error_detail("https://admin:secretpass@example.com/api")
        assert "secretpass" not in (result or "")

    def test_sensitive_query(self) -> None:
        result = redact_error_detail("https://api.example.com?access_token=abc")
        assert "abc" not in (result or "")


class TestRedactErrorDetailJson:
    """Phase 18: allowlist-based structured sanitization."""

    def test_json_with_api_key_redacted(self) -> None:
        result = redact_error_detail('{"api_key": "secret"}')
        assert result is not None
        assert "secret" not in result
        assert REDACTED in result

    def test_json_with_password_redacted(self) -> None:
        result = redact_error_detail('{"password": "secret"}')
        assert result is not None
        assert "secret" not in result

    def test_json_with_authorization_redacted(self) -> None:
        result = redact_error_detail('{"authorization": "Bearer abc123"}')
        assert result is not None
        assert "abc123" not in result

    def test_json_with_token_redacted(self) -> None:
        result = redact_error_detail('{"token": "secret"}')
        assert result is not None
        assert "secret" not in result

    def test_json_with_messages_redacted(self) -> None:
        result = redact_error_detail(
            '{"messages": [{"role": "user", "content": "private"}]}'
        )
        assert result is not None
        assert "private" not in result

    def test_json_with_input_redacted(self) -> None:
        result = redact_error_detail('{"input": "private prompt"}')
        assert result is not None
        assert "private prompt" not in result

    def test_json_nested(self) -> None:
        payload = {
            "type": "api_error",
            "code": "rate_limit",
            "message": "limit reached",
            "details": {
                "api_key": "secret",
                "token": "another-secret",
            },
        }
        result = redact_error_detail(json.dumps(payload))
        assert result is not None
        assert "secret" not in result
        assert "another-secret" not in result
        parsed = json.loads(result)
        assert parsed["type"] == "api_error"
        assert parsed["code"] == "rate_limit"
        # ``details`` is not allowlisted, so the entire object is
        # dropped along with the credentials inside it.
        assert "details" not in parsed

    def test_json_array_fail_closed(self) -> None:
        result = redact_error_detail('[{"api_key": "a"}, {"token": "b"}]')
        assert result == REDACTED

    def test_json_empty_array_fail_closed(self) -> None:
        result = redact_error_detail("[]")
        assert result == REDACTED

    def test_mixed_case_keys_redacted(self) -> None:
        result = redact_error_detail('{"API_KEY": "secret"}')
        assert result is not None
        assert "secret" not in result

    def test_bearer_in_json_string_redacted(self) -> None:
        result = redact_error_detail(
            '{"message": "Authorization: Bearer sk-supersecret"}'
        )
        assert result is not None
        assert "sk-supersecret" not in result

    def test_sk_key_in_json_redacted(self) -> None:
        result = redact_error_detail('{"message": "api key sk-abcdef012345 provided"}')
        assert result is not None
        assert "sk-abcdef012345" not in result

    def test_invalid_json_falls_back_to_regex(self) -> None:
        result = redact_error_detail("not really json but has password=secret")
        assert result is not None
        assert "secret" not in result

    def test_non_object_json_falls_back_to_regex(self) -> None:
        # Bare scalars don't match the JSON branch (must start with { or [)
        result = redact_error_detail("password=secret")
        assert result is not None
        assert "secret" not in result


class TestSanitizeErrorObject:
    """Direct tests for sanitize_error_object."""

    def test_sensitive_keys_redacted(self) -> None:
        result = sanitize_error_object({"api_key": "secret", "type": "api_error"})
        assert result["api_key"] == REDACTED
        assert result["type"] == "api_error"

    def test_user_content_keys_redacted(self) -> None:
        result = sanitize_error_object(
            {
                "input": "private",
                "messages": [{"role": "user", "content": "private"}],
                "type": "api_error",
            }
        )
        assert result["input"] == REDACTED
        assert result["messages"] == REDACTED
        assert result["type"] == "api_error"

    def test_depth_bound(self) -> None:
        # Build a deeply nested dict that exceeds MAX_SANITIZE_DEPTH.
        # Even with the allowlist, deep recursion must collapse to
        # [REDACTED] before the original secret value can escape.
        nested: dict = {"api_key": "secret"}
        for _ in range(10):
            nested = {"level": nested, "api_key": "secret"}
        result = sanitize_error_object(nested)
        serialized = json.dumps(result)
        assert "secret" not in serialized

    def test_item_budget_bound(self) -> None:
        # ``data`` is not on the allowlist, so the entire payload is
        # dropped before the item budget can expand. The structural
        # bound still applies for objects/arrays on the allowlist.
        result = sanitize_error_object({"data": [{"api_key": "k0"}] * 200})
        assert "data" not in result
        assert "k0" not in json.dumps(result)

    def test_byte_budget_bound(self) -> None:
        result = sanitize_error_object({"message": "x"}, byte_budget=1)
        assert result == REDACTED

    def test_allowlist_budget_bound(self) -> None:
        # Lists nested under an allowlisted key still honor the
        # item budget and collapse to REDACTED on exhaustion.
        result = sanitize_error_object({"trace_id": ["a"] * 200}, item_budget=4)
        # A few list entries are processed before the budget runs
        # out; the remaining items must be REDACTED. The list must
        # not contain more than the budgeted entries plus REDACTEDs.
        assert result["trace_id"][-1] == REDACTED
        assert all(entry in ("a", REDACTED) for entry in result["trace_id"])
        # No raw original material should remain.
        assert result["trace_id"].count("a") <= 3

    def test_truncates_long_string(self) -> None:
        result = sanitize_error_object({"message": "x" * 5000})
        assert isinstance(result["message"], str)
        assert len(result["message"]) < 5000

    def test_truncates_long_key(self) -> None:
        long_key = "a" * 200
        result = sanitize_error_object({long_key: "value"})
        # Key should be truncated
        serialized = json.dumps(result)
        assert "a" * 200 not in serialized

    def test_preserves_scalar_values(self) -> None:
        result = sanitize_error_object({"status_code": 429, "type": "rate_limit"})
        assert result["status_code"] == 429
        assert result["type"] == "rate_limit"


class TestSensitiveKeySets:
    """The exported sets must include the keys documented in the plan."""

    def test_sensitive_keys_contains_core_set(self) -> None:
        for key in (
            "authorization",
            "api_key",
            "apikey",
            "password",
            "secret",
            "token",
            "access_token",
            "refresh_token",
        ):
            assert key in SENSITIVE_JSON_KEYS

    def test_user_content_keys_contains_core_set(self) -> None:
        for key in ("prompt", "completion", "input", "messages"):
            assert key in USER_CONTENT_JSON_KEYS

    def test_safe_json_keys_contains_diagnostic_set(self) -> None:
        for key in (
            "type",
            "code",
            "status",
            "status_code",
            "error_type",
            "kind",
            "param",
            "message",
            "request_id",
            "trace_id",
        ):
            assert key in SAFE_JSON_KEYS


class TestAllowlistPolicy:
    """Phase 18: the structured policy is allowlist-based."""

    def test_payload_with_private_source_code_is_dropped(self) -> None:
        result = redact_error_detail(
            json.dumps(
                {
                    "type": "invalid_request",
                    "message": "bad token sk-secret",
                    "payload": "private source code body",
                }
            )
        )
        assert result is not None
        assert "payload" not in result
        assert "private source code body" not in result
        parsed = json.loads(result)
        assert parsed == {
            "type": "invalid_request",
            "message": "bad token [REDACTED]",
        }

    def test_data_and_details_are_dropped(self) -> None:
        result = redact_error_detail(
            json.dumps(
                {
                    "type": "api_error",
                    "data": {"api_key": "sk-private"},
                    "details": {"context": "private debug info"},
                    "debug": "internal trace",
                }
            )
        )
        assert result is not None
        parsed = json.loads(result)
        assert parsed == {"type": "api_error"}
        assert "data" not in parsed
        assert "details" not in parsed
        assert "debug" not in parsed

    def test_nested_unknown_keys_are_dropped(self) -> None:
        result = redact_error_detail(
            json.dumps(
                {
                    "type": "api_error",
                    "error": {
                        "type": "nested",
                        "code": "rate_limit",
                        "context": "private context",
                    },
                }
            )
        )
        assert result is not None
        parsed = json.loads(result)
        # The outer ``error`` is not allowlisted, so it and its
        # contents are dropped entirely.
        assert "error" not in parsed
        assert parsed == {"type": "api_error"}

    def test_safe_diagnostic_keys_are_retained(self) -> None:
        result = redact_error_detail(
            json.dumps(
                {
                    "type": "api_error",
                    "code": "rate_limit",
                    "status": 429,
                    "status_code": 429,
                    "error_type": "rate_limit_error",
                    "kind": "rate_limit",
                    "param": "max_tokens",
                    "request_id": "req-abc",
                    "trace_id": "trace-xyz",
                }
            )
        )
        assert result is not None
        parsed = json.loads(result)
        assert parsed == {
            "type": "api_error",
            "code": "rate_limit",
            "status": 429,
            "status_code": 429,
            "error_type": "rate_limit_error",
            "kind": "rate_limit",
            "param": "max_tokens",
            "request_id": "req-abc",
            "trace_id": "trace-xyz",
        }

    def test_message_is_redacted_and_bounded(self) -> None:
        long_message = "sk-supersecret " * 500
        result = redact_error_detail(
            json.dumps({"type": "api_error", "message": long_message})
        )
        assert result is not None
        assert len(result) <= MAX_REDACTED_ERROR_DETAIL_CHARS
        assert "sk-supersecret" not in result
        parsed = json.loads(result)
        assert parsed["type"] == "api_error"
        # Per-string bound was already applied (truncation uses the
        # ``MAX_STRING_BYTES`` constant plus a small ``...`` suffix,
        # which keeps the per-string length bounded).
        assert len(parsed["message"]) <= 1024 + 3

    def test_prompt_completion_messages_input_markers_are_absent(self) -> None:
        result = redact_error_detail(
            json.dumps(
                {
                    "type": "api_error",
                    "code": "rate_limit",
                    "message": "ok",
                    "prompt": "private prompt",
                    "completion": "private completion",
                    "messages": "private messages",
                    "input": "private input",
                    "api_key": "private-api-key",
                    "token": "private-token",
                    "authorization": "Bearer private-auth",
                }
            )
        )
        assert result is not None
        for marker in (
            "private prompt",
            "private completion",
            "private messages",
            "private input",
            "private-api-key",
            "private-token",
            "private-auth",
        ):
            assert marker not in result, f"Marker {marker!r} present in {result!r}"
        parsed = json.loads(result)
        assert parsed["prompt"] == REDACTED
        assert parsed["completion"] == REDACTED
        assert parsed["messages"] == REDACTED
        assert parsed["input"] == REDACTED
        assert parsed["api_key"] == REDACTED
        assert parsed["token"] == REDACTED
        assert parsed["authorization"] == REDACTED

    def test_top_level_array_is_fail_closed(self) -> None:
        result = redact_error_detail(json.dumps([{"api_key": "k1"}, {"token": "k2"}]))
        assert result == REDACTED
        # A scalar list element is also replaced.
        result2 = redact_error_detail(json.dumps(["private", "data"]))
        assert result2 == REDACTED

    def test_helper_output_is_bounded(self) -> None:
        huge = "x" * 10000
        result = redact_error_detail(huge)
        assert result is not None
        assert len(result) <= MAX_REDACTED_ERROR_DETAIL_CHARS
        # Bound is also applied to the JSON branch.
        json_result = redact_error_detail(
            json.dumps({"type": "api_error", "message": "x" * 10000})
        )
        assert json_result is not None
        assert len(json_result) <= MAX_REDACTED_ERROR_DETAIL_CHARS

    def test_max_redacted_error_detail_chars_constant(self) -> None:
        assert MAX_REDACTED_ERROR_DETAIL_CHARS == 2048
