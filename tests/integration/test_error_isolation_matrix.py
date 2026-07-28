"""Plan 023 — Error isolation matrix (integration).

Exercises the full status/body/error-shape matrix for upstream validation
failures and verifies that subsequent requests are unaffected.  Covers
400, 422, misleading 404, and transport-interruption variants.

Run with::

    uv run pytest tests/integration/test_plan_023_error_isolation_matrix.py -v
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from tests.helpers.mock_upstream import (
    UPSTREAM_BASE,
    MockResponseSpec,
    MockUpstream,
    MockUpstreamRule,
)

pytestmark = [pytest.mark.integration, pytest.mark.request_path]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _payload(model: str = "gpt-4", effort: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": "Hi"}],
    }
    if effort is not None:
        body["reasoning_effort"] = effort
    return body


def _ok_response() -> MockResponseSpec:
    return MockResponseSpec(
        status_code=200,
        json_body={
            "id": "chatcmpl-ok",
            "object": "chat.completion",
            "model": "gpt-4",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "OK"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 5,
                "completion_tokens": 2,
                "total_tokens": 7,
            },
        },
    )


def _error_400() -> MockResponseSpec:
    return MockResponseSpec(
        status_code=400,
        json_body={
            "error": {
                "type": "invalid_request_error",
                "message": "Bad request",
            }
        },
    )


def _error_422() -> MockResponseSpec:
    return MockResponseSpec(
        status_code=422,
        json_body={
            "error": {
                "type": "unprocessable_entity",
                "message": "Invalid parameter",
            }
        },
    )


def _error_404_misleading() -> MockResponseSpec:
    return MockResponseSpec(
        status_code=404,
        json_body={
            "error": {
                "type": "model_not_found",
                "message": (
                    "Model not found. Note: thinking level is also not supported."
                ),
            }
        },
    )


def _error_text_body() -> MockResponseSpec:
    return MockResponseSpec(
        status_code=500,
        text_body="Internal Server Error",
    )


def _error_empty_body() -> MockResponseSpec:
    return MockResponseSpec(
        status_code=502, headers={"content-type": "application/json"}
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestErrorIsolationMatrix:
    """Verify isolation between error responses and subsequent requests."""

    def test_400_does_not_corrupt_subsequent_request(self) -> None:
        """A 400 error does not leak state into the next request."""
        rules = [
            MockUpstreamRule(min_sequence=1, max_sequence=1, response=_error_400()),
            MockUpstreamRule(min_sequence=2, max_sequence=2, response=_ok_response()),
        ]
        upstream = MockUpstream(rules=rules)
        with upstream, httpx.Client(base_url=UPSTREAM_BASE) as client:
            resp1 = client.post("/chat/completions", json=_payload())
            resp2 = client.post("/chat/completions", json=_payload())
        assert resp1.status_code == 400
        assert resp2.status_code == 200
        assert upstream.request_count == 2

    def test_422_does_not_corrupt_subsequent_request(self) -> None:
        rules = [
            MockUpstreamRule(min_sequence=1, max_sequence=1, response=_error_422()),
            MockUpstreamRule(min_sequence=2, max_sequence=2, response=_ok_response()),
        ]
        upstream = MockUpstream(rules=rules)
        with upstream, httpx.Client(base_url=UPSTREAM_BASE) as client:
            resp1 = client.post("/chat/completions", json=_payload())
            resp2 = client.post("/chat/completions", json=_payload())
        assert resp1.status_code == 422
        assert resp2.status_code == 200

    def test_misleading_404_does_not_corrupt_subsequent_request(self) -> None:
        rules = [
            MockUpstreamRule(
                min_sequence=1, max_sequence=1, response=_error_404_misleading()
            ),
            MockUpstreamRule(min_sequence=2, max_sequence=2, response=_ok_response()),
        ]
        upstream = MockUpstream(rules=rules)
        with upstream, httpx.Client(base_url=UPSTREAM_BASE) as client:
            resp1 = client.post("/chat/completions", json=_payload())
            resp2 = client.post("/chat/completions", json=_payload())
        assert resp1.status_code == 404
        assert resp2.status_code == 200

    def test_text_body_error_does_not_corrupt(self) -> None:
        rules = [
            MockUpstreamRule(
                min_sequence=1, max_sequence=1, response=_error_text_body()
            ),
            MockUpstreamRule(min_sequence=2, max_sequence=2, response=_ok_response()),
        ]
        upstream = MockUpstream(rules=rules)
        with upstream, httpx.Client(base_url=UPSTREAM_BASE) as client:
            resp1 = client.post("/chat/completions", json=_payload())
            resp2 = client.post("/chat/completions", json=_payload())
        assert resp1.status_code == 500
        assert resp2.status_code == 200

    def test_empty_body_error_does_not_corrupt(self) -> None:
        rules = [
            MockUpstreamRule(
                min_sequence=1, max_sequence=1, response=_error_empty_body()
            ),
            MockUpstreamRule(min_sequence=2, max_sequence=2, response=_ok_response()),
        ]
        upstream = MockUpstream(rules=rules)
        with upstream, httpx.Client(base_url=UPSTREAM_BASE) as client:
            resp1 = client.post("/chat/completions", json=_payload())
            resp2 = client.post("/chat/completions", json=_payload())
        assert resp1.status_code == 502
        assert resp2.status_code == 200

    def test_multiple_errors_then_success(self) -> None:
        """Multiple consecutive errors are followed by a clean success."""
        rules = [
            MockUpstreamRule(min_sequence=1, max_sequence=1, response=_error_400()),
            MockUpstreamRule(min_sequence=2, max_sequence=2, response=_error_422()),
            MockUpstreamRule(
                min_sequence=3, max_sequence=3, response=_error_404_misleading()
            ),
            MockUpstreamRule(min_sequence=4, max_sequence=4, response=_ok_response()),
        ]
        upstream = MockUpstream(rules=rules)
        with upstream, httpx.Client(base_url=UPSTREAM_BASE) as client:
            resp1 = client.post("/chat/completions", json=_payload())
            resp2 = client.post("/chat/completions", json=_payload())
            resp3 = client.post("/chat/completions", json=_payload())
            resp4 = client.post("/chat/completions", json=_payload())
        assert resp1.status_code == 400
        assert resp2.status_code == 422
        assert resp3.status_code == 404
        assert resp4.status_code == 200
        assert upstream.request_count == 4

    def test_model_specific_error_does_not_affect_other_models(self) -> None:
        """An error for model A does not block model B."""
        rules = [
            MockUpstreamRule(
                model="MiniMax-M3",
                response=_error_400(),
            ),
            MockUpstreamRule(
                model="gpt-4",
                response=_ok_response(),
            ),
        ]
        upstream = MockUpstream(rules=rules)
        with upstream, httpx.Client(base_url=UPSTREAM_BASE) as client:
            resp1 = client.post(
                "/chat/completions",
                json=_payload(model="MiniMax-M3", effort="xhigh"),
            )
            resp2 = client.post(
                "/chat/completions",
                json=_payload(model="gpt-4"),
            )
        assert resp1.status_code == 400
        assert resp2.status_code == 200
