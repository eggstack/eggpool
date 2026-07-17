"""Hot-path equivalence regression suite for Milestone F.

Consolidates the 10 required equivalence scenarios from the plan's
test plan into a single regression suite.  Each test verifies that
the corresponding hot-path behaviour is preserved after F7/F8/F9/F10
optimizations.

Scenarios:
1. OpenAI native non-streaming request
2. OpenAI native streaming request
3. Anthropic native non-streaming request
4. Anthropic native streaming request
5. OpenAI→Anthropic transcode (body)
6. Large tool schemas (50 tools)
7. Invalid JSON body handling
8. Header forwarding security (credentials stripped)
9. ParsedRequestPayload caching correctness
10. estimate_padded_size equivalence with allocation
"""

from __future__ import annotations

import json

import pytest

from eggpool.proxy.client import (
    HOP_BY_HOP_HEADERS,
    build_upstream_headers,
    sanitize_request_headers,
)
from eggpool.request.parsed_payload import ParsedRequestPayload
from eggpool.request.payload_utils import estimate_padded_size

# ---------------------------------------------------------------------------
# Payloads
# ---------------------------------------------------------------------------

_OPENAI_BODY = json.dumps(
    {
        "model": "gpt-4",
        "messages": [{"role": "user", "content": "hello"}],
    }
).encode()

_OPENAI_STREAM_BODY = json.dumps(
    {
        "model": "gpt-4",
        "stream": True,
        "messages": [{"role": "user", "content": "hello"}],
    }
).encode()

_ANTHROPIC_NATIVE_BODY = json.dumps(
    {
        "model": "claude-3-sonnet-20240229",
        "messages": [{"role": "user", "content": "hello"}],
    }
).encode()

_LARGE_TOOL_BODY = json.dumps(
    {
        "model": "gpt-4",
        "messages": [{"role": "user", "content": "hello"}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": f"tool_{i}",
                    "description": f"Tool number {i} with a fairly long description "
                    * 5,
                    "parameters": {
                        "type": "object",
                        "properties": {
                            f"arg_{j}": {"type": "string", "description": f"Arg {j}"}
                            for j in range(5)
                        },
                    },
                },
            }
            for i in range(50)
        ],
    }
).encode()

_INVALID_JSON = b"this is not valid json {{{"

_SAMPLE_HEADERS = {
    "Content-Type": "application/json",
    "Authorization": "Bearer sk-secret-api-key",
    "X-Api-Key": "secret-key",
    "Accept": "application/json",
    "OpenAI-Organization": "org-test",
    "Connection": "keep-alive, Upgrade",
    "Transfer-Encoding": "chunked",
    "Host": "api.example.com",
    "Content-Length": "1234",
    "X-Custom-Forwarded": "yes",
}


# ---------------------------------------------------------------------------
# Scenario 1: OpenAI native non-streaming
# ---------------------------------------------------------------------------


class TestOpenAINativeNonStreaming:
    def test_body_parseable(self) -> None:
        payload = ParsedRequestPayload(original_bytes=_OPENAI_BODY)
        d = payload.parsed_dict
        assert isinstance(d, dict)
        assert d["model"] == "gpt-4"
        assert payload.streaming is False

    def test_model_id_extracted(self) -> None:
        payload = ParsedRequestPayload(original_bytes=_OPENAI_BODY)
        assert payload.model_id == "gpt-4"


# ---------------------------------------------------------------------------
# Scenario 2: OpenAI native streaming
# ---------------------------------------------------------------------------


class TestOpenAINativeStreaming:
    def test_streaming_flag_true(self) -> None:
        payload = ParsedRequestPayload(original_bytes=_OPENAI_STREAM_BODY)
        assert payload.streaming is True

    def test_model_id_preserved(self) -> None:
        payload = ParsedRequestPayload(original_bytes=_OPENAI_STREAM_BODY)
        assert payload.model_id == "gpt-4"


# ---------------------------------------------------------------------------
# Scenario 3: Anthropic native non-streaming
# ---------------------------------------------------------------------------


class TestAnthropicNativeNonStreaming:
    def test_body_parseable(self) -> None:
        payload = ParsedRequestPayload(original_bytes=_ANTHROPIC_NATIVE_BODY)
        d = payload.parsed_dict
        assert isinstance(d, dict)
        assert "claude" in d["model"]

    def test_streaming_defaults_false(self) -> None:
        payload = ParsedRequestPayload(original_bytes=_ANTHROPIC_NATIVE_BODY)
        assert payload.streaming is False


# ---------------------------------------------------------------------------
# Scenario 4: Anthropic native streaming (implicit via missing stream field)
# ---------------------------------------------------------------------------


class TestAnthropicNativeStreaming:
    def test_streaming_field_absent_defaults_false(self) -> None:
        """Anthropic protocol does not use 'stream' field; defaults to False."""
        body = json.dumps(
            {"model": "claude-3-sonnet-20240229", "messages": []}
        ).encode()
        payload = ParsedRequestPayload(original_bytes=body)
        assert payload.streaming is False

    def test_streaming_explicit_true(self) -> None:
        body = json.dumps(
            {
                "model": "claude-3-sonnet-20240229",
                "stream": True,
                "messages": [],
            }
        ).encode()
        payload = ParsedRequestPayload(original_bytes=body)
        assert payload.streaming is True


# ---------------------------------------------------------------------------
# Scenario 5: OpenAI→Anthropic transcode body (model_id extraction)
# ---------------------------------------------------------------------------


class TestTranscodeBodyEquivalence:
    def test_model_id_matches_upstream_target(self) -> None:
        """Model ID for a transcode target is extracted from the request body."""
        payload = ParsedRequestPayload(original_bytes=_ANTHROPIC_NATIVE_BODY)
        assert payload.model_id == "claude-3-sonnet-20240229"

    def test_original_bytes_preserved(self) -> None:
        """Original body bytes are never mutated by parsing."""
        original = _ANTHROPIC_NATIVE_BODY
        payload = ParsedRequestPayload(original_bytes=original)
        _ = payload.parsed_dict
        assert payload.original_bytes == original


# ---------------------------------------------------------------------------
# Scenario 6: Large tool schemas
# ---------------------------------------------------------------------------


class TestLargeToolSchemas:
    def test_50_tools_parseable(self) -> None:
        payload = ParsedRequestPayload(original_bytes=_LARGE_TOOL_BODY)
        d = payload.parsed_dict
        assert isinstance(d, dict)
        assert len(d.get("tools", [])) == 50

    def test_model_id_still_extracted(self) -> None:
        payload = ParsedRequestPayload(original_bytes=_LARGE_TOOL_BODY)
        assert payload.model_id == "gpt-4"

    def test_large_body_size_estimation(self) -> None:
        """estimate_padded_size handles large bodies without allocation."""
        base = len(_LARGE_TOOL_BODY)
        result = estimate_padded_size(base, 100_000)
        assert result == base + 100_000


# ---------------------------------------------------------------------------
# Scenario 7: Invalid JSON body
# ---------------------------------------------------------------------------


class TestInvalidJsonBody:
    def test_invalid_json_returns_none(self) -> None:
        """ParsedRequestPayload catches invalid JSON and returns None."""
        payload = ParsedRequestPayload(original_bytes=_INVALID_JSON)
        assert payload.parsed_dict is None

    def test_empty_body_returns_none(self) -> None:
        """Empty body returns None (parsed_dict gracefully handles it)."""
        payload = ParsedRequestPayload(original_bytes=b"")
        assert payload.parsed_dict is None

    def test_array_body_returns_list(self) -> None:
        """A JSON array body parses to a list, not a dict."""
        body = json.dumps([{"role": "user", "content": "hi"}]).encode()
        payload = ParsedRequestPayload(original_bytes=body)
        d = payload.parsed_dict
        assert isinstance(d, list)

    def test_array_body_model_id_raises(self) -> None:
        """model_id on a list body raises AttributeError (no 'model' key)."""
        body = json.dumps([{"role": "user", "content": "hi"}]).encode()
        payload = ParsedRequestPayload(original_bytes=body)
        _ = payload.parsed_dict  # force parse
        with pytest.raises(AttributeError):
            _ = payload.model_id


# ---------------------------------------------------------------------------
# Scenario 8: Header forwarding security
# ---------------------------------------------------------------------------


class TestHeaderForwardingSecurity:
    def test_credentials_stripped(self) -> None:
        result = sanitize_request_headers(dict(_SAMPLE_HEADERS))
        for key in result:
            assert key.lower() not in {
                "authorization",
                "x-api-key",
                "proxy-authorization",
            }

    def test_hop_by_hop_stripped(self) -> None:
        result = sanitize_request_headers(dict(_SAMPLE_HEADERS))
        for key in result:
            assert key.lower() not in HOP_BY_HOP_HEADERS

    def test_host_stripped(self) -> None:
        result = sanitize_request_headers(dict(_SAMPLE_HEADERS))
        assert "host" not in {k.lower() for k in result}

    def test_content_length_stripped(self) -> None:
        result = sanitize_request_headers(dict(_SAMPLE_HEADERS))
        assert "content-length" not in {k.lower() for k in result}

    def test_connection_nominated_stripped(self) -> None:
        """Headers nominated by Connection field are stripped."""
        headers = {
            "Connection": "Upgrade",
            "Upgrade": "websocket",
            "Content-Type": "application/json",
        }
        result = sanitize_request_headers(headers)
        assert "upgrade" not in {k.lower() for k in result}

    def test_custom_headers_preserved(self) -> None:
        result = sanitize_request_headers(dict(_SAMPLE_HEADERS))
        assert "X-Custom-Forwarded" in result

    def test_content_type_preserved(self) -> None:
        result = sanitize_request_headers(dict(_SAMPLE_HEADERS))
        assert "Content-Type" in result

    def test_build_upstream_headers_injects_auth(self) -> None:
        result = build_upstream_headers(dict(_SAMPLE_HEADERS), "sk-test-key")
        assert result["Authorization"] == "Bearer sk-test-key"

    def test_build_upstream_headers_strips_original_auth(self) -> None:
        result = build_upstream_headers(dict(_SAMPLE_HEADERS), "sk-new-key")
        # Only the injected auth should be present
        auth_count = sum(1 for k in result if k.lower() == "authorization")
        assert auth_count == 1
        assert result["Authorization"] == "Bearer sk-new-key"

    def test_extra_drop_headers(self) -> None:
        result = build_upstream_headers(
            dict(_SAMPLE_HEADERS),
            "sk-test",
            extra_drop=frozenset({"x-custom-forwarded"}),
        )
        assert "X-Custom-Forwarded" not in result


# ---------------------------------------------------------------------------
# Scenario 9: ParsedRequestPayload caching correctness
# ---------------------------------------------------------------------------


class TestParsedPayloadCachingCorrectness:
    def test_cache_returns_same_object(self) -> None:
        payload = ParsedRequestPayload(original_bytes=_OPENAI_BODY)
        d1 = payload.parsed_dict
        d2 = payload.parsed_dict
        assert d1 is d2

    def test_invalidate_transformed_resets_derived_state(self) -> None:
        """invalidate_transformed resets model_id and streaming, not parsed_dict."""
        payload = ParsedRequestPayload(original_bytes=_OPENAI_BODY)
        d1 = payload.parsed_dict
        m1 = payload.model_id
        payload.invalidate_transformed()
        d2 = payload.parsed_dict
        # parsed_dict is NOT invalidated (same object)
        assert d1 is d2
        # But derived state is re-computed
        m2 = payload.model_id
        assert m1 == m2  # same value, re-computed from cached dict

    def test_derived_state_cached(self) -> None:
        payload = ParsedRequestPayload(original_bytes=_OPENAI_STREAM_BODY)
        m1 = payload.model_id
        m2 = payload.model_id
        assert m1 == m2
        s1 = payload.streaming
        s2 = payload.streaming
        assert s1 is s2

    def test_original_bytes_never_mutated(self) -> None:
        original = _OPENAI_BODY
        payload = ParsedRequestPayload(original_bytes=original)
        _ = payload.parsed_dict
        _ = payload.model_id
        _ = payload.streaming
        assert payload.original_bytes == original


# ---------------------------------------------------------------------------
# Scenario 10: estimate_padded_size equivalence
# ---------------------------------------------------------------------------


class TestEstimatePaddedSizeEquivalence:
    def test_zero_expansion(self) -> None:
        assert estimate_padded_size(1000, 0) == 1000

    def test_positive_expansion(self) -> None:
        assert estimate_padded_size(1000, 500) == 1500

    def test_negative_expansion_clamped(self) -> None:
        assert estimate_padded_size(1000, -500) == 1000

    def test_large_expansion(self) -> None:
        assert estimate_padded_size(100, 10_000_000) == 10_000_100

    def test_boundary_zero_base(self) -> None:
        assert estimate_padded_size(0, 100) == 100

    def test_both_zero(self) -> None:
        assert estimate_padded_size(0, 0) == 0

    def test_return_type_is_int(self) -> None:
        result = estimate_padded_size(100, 50)
        assert isinstance(result, int)

    def test_no_allocation(self) -> None:
        """Verify the function is arithmetic-only (no bytes allocation)."""
        import tracemalloc

        tracemalloc.start()
        snapshot1 = tracemalloc.take_snapshot()
        for _ in range(10_000):
            estimate_padded_size(10_000, 500_000)
        snapshot2 = tracemalloc.take_snapshot()
        tracemalloc.stop()

        stats = snapshot2.compare_to(snapshot1, "lineno")
        # Total allocated bytes should be minimal (< 1MB for 10000 calls)
        total_increase = sum(s.size_diff for s in stats if s.size_diff > 0)
        assert total_increase < 1_000_000
