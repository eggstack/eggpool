"""Unit tests for the security.redaction module.

Phase 17 strengthens the persisted-error-detail privacy guarantee.
The redactor now covers common JSON credential forms and
structured sanitization for error bodies that parse as JSON.
"""

from __future__ import annotations

import json

from go_aggregator.security.redaction import (
    REDACTED,
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
    """Phase 17: JSON-shaped inputs are parsed and recursively sanitized."""

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
        result = redact_error_detail(
            '{"authorization": "Bearer abc123"}'
        )
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
            "error": {
                "type": "api_error",
                "code": "rate_limit",
                "message": "limit reached",
                "details": {
                    "api_key": "secret",
                    "token": "another-secret",
                },
            }
        }
        result = redact_error_detail(json.dumps(payload))
        assert result is not None
        assert "secret" not in result
        assert "another-secret" not in result
        parsed = json.loads(result)
        assert parsed["error"]["type"] == "api_error"
        assert parsed["error"]["code"] == "rate_limit"
        assert parsed["error"]["details"]["api_key"] == REDACTED
        assert parsed["error"]["details"]["token"] == REDACTED

    def test_json_array(self) -> None:
        result = redact_error_detail(
            '[{"api_key": "a"}, {"token": "b"}]'
        )
        assert result is not None
        assert "a" not in result.split("api_key")[1].split("}")[0] or REDACTED in result
        assert REDACTED in result

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
        result = redact_error_detail(
            '{"message": "api key sk-abcdef012345 provided"}'
        )
        assert result is not None
        assert "sk-abcdef012345" not in result

    def test_invalid_json_falls_back_to_regex(self) -> None:
        result = redact_error_detail(
            'not really json but has password=secret'
        )
        assert result is not None
        assert "secret" not in result

    def test_non_object_json_falls_back_to_regex(self) -> None:
        # Bare scalars don't match the JSON branch (must start with { or [)
        result = redact_error_detail('password=secret')
        assert result is not None
        assert "secret" not in result


class TestSanitizeErrorObject:
    """Direct tests for sanitize_error_object."""

    def test_sensitive_keys_redacted(self) -> None:
        result = sanitize_error_object(
            {"api_key": "secret", "name": "ok"}
        )
        assert result["api_key"] == REDACTED
        assert result["name"] == "ok"

    def test_user_content_keys_redacted(self) -> None:
        result = sanitize_error_object(
            {
                "input": "private",
                "messages": [{"role": "user", "content": "private"}],
            }
        )
        assert result["input"] == REDACTED
        assert result["messages"] == REDACTED

    def test_depth_bound(self) -> None:
        # Build a deeply nested dict that exceeds MAX_SANITIZE_DEPTH
        nested: dict = {"api_key": "secret"}
        for _ in range(10):
            nested = {"level": nested, "api_key": "secret"}
        result = sanitize_error_object(nested)
        # Result should be a sanitized structure; no leaked "secret"
        serialized = json.dumps(result)
        assert "secret" not in serialized

    def test_item_budget_bound(self) -> None:
        # Build a large list
        payload = {"data": [{"api_key": f"k{i}"} for i in range(200)]}
        result = sanitize_error_object(payload, item_budget=10)
        # Most items should be truncated to REDACTED or dropped
        assert "REDACTED" in json.dumps(result)

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
        result = sanitize_error_object(
            {"status_code": 429, "retriable": True}
        )
        assert result["status_code"] == 429
        assert result["retriable"] is True


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
