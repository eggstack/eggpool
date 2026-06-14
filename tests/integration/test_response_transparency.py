"""Section 10: Preserve raw non-streaming responses."""

from __future__ import annotations

from go_aggregator.request.coordinator import PreparedProxyResponse


class TestRawResponseRendering:
    """Tests that non-streaming responses preserve upstream bytes."""

    def test_body_is_raw_bytes(self) -> None:
        """PreparedProxyResponse body should be raw bytes."""
        body = b'{"id":"chatcmpl-123","object":"chat.completion"}'
        result = PreparedProxyResponse(
            status_code=200,
            headers={"content-type": "application/json"},
            body=body,
        )
        assert result.body == body
        assert isinstance(result.body, bytes)

    def test_non_json_body_preserved(self) -> None:
        """Non-JSON upstream error bodies should be preserved as bytes."""
        body = b"This is not JSON, just plain text error"
        result = PreparedProxyResponse(
            status_code=500,
            headers={"content-type": "text/plain"},
            body=body,
        )
        assert result.body == body

    def test_binary_body_preserved(self) -> None:
        """Binary bodies should be preserved."""
        body = bytes(range(256))
        result = PreparedProxyResponse(
            status_code=200,
            headers={"content-type": "application/octet-stream"},
            body=body,
        )
        assert result.body == body

    def test_json_whitespace_preserved(self) -> None:
        """JSON whitespace should be byte-identical."""
        body = b'{  "key":  "value"  }'
        result = PreparedProxyResponse(
            status_code=200,
            headers={"content-type": "application/json"},
            body=body,
        )
        assert result.body == body

    def test_content_type_passthrough(self) -> None:
        """Upstream content-type header should be preserved."""
        headers = {
            "content-type": "application/json; charset=utf-8",
            "x-request-id": "req-123",
        }
        result = PreparedProxyResponse(
            status_code=200,
            headers=headers,
            body=b"{}",
        )
        assert result.headers["content-type"] == "application/json; charset=utf-8"
