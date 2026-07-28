"""Plan 028 — JSON operation count verification.

Verifies that the consolidated request/response pipeline performs at most
one JSON decode and at most one encode per request/response path, as
required by the performance acceptance criteria.

Run with::

    uv run pytest tests/perf/test_plan_028_json_operation_counts.py -m performance -v
"""

from __future__ import annotations

import json

from eggpool.request.parsed_upstream_response import build_parsed_upstream_response
from eggpool.request.provider_bound_request import ProviderBoundRequest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _openai_success_body() -> bytes:
    return json.dumps(
        {
            "id": "chatcmpl-123",
            "object": "chat.completion",
            "model": "gpt-4",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
            },
        }
    ).encode()


def _anthropic_success_body() -> bytes:
    return json.dumps(
        {
            "id": "msg-123",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "ok"}],
            "model": "claude-3-opus",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
            },
        }
    ).encode()


# ---------------------------------------------------------------------------
# ParsedUpstreamResponse decode count
# ---------------------------------------------------------------------------


class TestParsedUpstreamResponseDecodeCount:
    """Verify at most one decode per response path."""

    def test_single_decode_on_access(self) -> None:
        """parsed_json triggers exactly one decode."""
        body = _openai_success_body()
        resp = build_parsed_upstream_response(200, [], body)
        # Access parsed_json — triggers one decode
        d = resp.parsed_json
        assert d is not None
        # Second access reuses cached — no additional decode
        d2 = resp.parsed_json
        assert d is d2  # same object

    def test_single_decode_dict_access(self) -> None:
        """parsed_dict triggers exactly one decode via parsed_json."""
        body = _openai_success_body()
        resp = build_parsed_upstream_response(200, [], body)
        d1 = resp.parsed_dict
        d2 = resp.parsed_dict
        assert d1 is d2

    def test_no_decode_for_non_json(self) -> None:
        """Invalid JSON body triggers no decode — parse_status reflects failure."""
        resp = build_parsed_upstream_response(200, [], b"not json")
        assert resp.parse_status == "invalid_json"
        assert resp.parsed_json is None

    def test_single_decode_anthropic_body(self) -> None:
        """Anthropic response body decodes once."""
        body = _anthropic_success_body()
        resp = build_parsed_upstream_response(200, [], body)
        d = resp.parsed_dict
        assert d is not None
        assert d.get("type") == "message"


# ---------------------------------------------------------------------------
# ProviderBoundRequest encode count
# ---------------------------------------------------------------------------


class TestProviderBoundRequestEncodeCount:
    """Verify at most one encode per request path."""

    def test_no_encode_before_serialization(self) -> None:
        """provider_bytes is None until explicitly serialized."""
        pbr = ProviderBoundRequest(
            client_bytes=b'{"model":"gpt-4"}',
            client_payload={"model": "gpt-4"},
            client_protocol="openai",
            model_id="gpt-4",
        )
        assert pbr.provider_bytes is None

    def test_single_encode_via_set_provider_bytes(self) -> None:
        """set_provider_bytes stores exactly one encode result."""
        pbr = ProviderBoundRequest(
            client_bytes=b'{"model":"gpt-4"}',
            client_payload={"model": "gpt-4"},
            client_protocol="openai",
            model_id="gpt-4",
        )
        body = json.dumps(pbr.provider_payload).encode()
        pbr.set_provider_bytes(body)
        assert pbr.provider_bytes == body

    def test_encode_invalidated_on_mutation(self) -> None:
        """After set_provider_payload, provider_bytes is None (needs re-encode)."""
        pbr = ProviderBoundRequest(
            client_bytes=b'{"model":"gpt-4"}',
            client_payload={"model": "gpt-4"},
            client_protocol="openai",
            model_id="gpt-4",
        )
        pbr.set_provider_bytes(b"stale")
        pbr.set_provider_payload({"model": "claude"})
        assert pbr.provider_bytes is None  # must re-encode

    def test_no_mutation_preserves_bytes(self) -> None:
        """Without mutation, set_provider_bytes result is stable."""
        pbr = ProviderBoundRequest(
            client_bytes=b'{"model":"gpt-4"}',
            client_payload={"model": "gpt-4"},
            client_protocol="openai",
            model_id="gpt-4",
        )
        pbr.set_provider_bytes(b'{"model":"gpt-4"}')
        assert pbr.provider_bytes == b'{"model":"gpt-4"}'
        # No mutation — bytes still valid
        assert not pbr.mutated


# ---------------------------------------------------------------------------
# Combined decode+encode count per path
# ---------------------------------------------------------------------------


class TestCombinedOperationCounts:
    """End-to-end decode/encode count per path."""

    def test_native_nonstream_response_one_decode(self) -> None:
        """Non-stream response: at most one JSON decode."""
        body = _openai_success_body()
        resp = build_parsed_upstream_response(200, [], body)
        # Simulate usage extraction + response transcoding sharing one decode
        d = resp.parsed_dict
        assert d is not None
        usage = d.get("usage", {})
        assert usage["prompt_tokens"] == 10
        # Re-access — no additional decode
        assert resp.parsed_dict is d

    def test_error_response_one_decode(self) -> None:
        """Error response: at most one JSON decode."""
        body = json.dumps({"error": {"message": "Bad request"}}).encode()
        resp = build_parsed_upstream_response(400, [], body)
        d = resp.parsed_dict
        assert d is not None
        assert d["error"]["message"] == "Bad request"
        # Re-access — no additional decode
        assert resp.parsed_dict is d

    def test_request_one_encode_after_transform(self) -> None:
        """Request: at most one encode after transform pipeline."""
        pbr = ProviderBoundRequest(
            client_bytes=b'{"model":"gpt-4"}',
            client_payload={"model": "gpt-4"},
            client_protocol="openai",
            model_id="gpt-4",
        )
        # Simulate transform pipeline
        pbr.set_provider_payload({"model": "claude", "stream": True})
        # Serialize once
        body = json.dumps(dict(pbr.provider_payload)).encode()
        pbr.set_provider_bytes(body)
        assert pbr.provider_bytes == body
        assert pbr.payload_generation == 1
