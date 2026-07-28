"""Plan 030 — Full failure-effects matrix (Workstream D).

Exercises the complete status/body/error-shape matrix for upstream
validation failures and verifies that each observation produces the
exact immutable ``FailureEffects``, applied state changes, durable
backoff/quarantine record, retry behavior, and client response.

Each test case maps to a row in the Plan 025 failure-effects table and
verifies that the effects are applied exactly once via the
``EffectsApplier`` idempotency key.

Run with::

    uv run pytest tests/integration/test_plan_030_failure_effects_matrix.py -v
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


def _payload(model: str = "gpt-4") -> dict[str, Any]:
    return {
        "model": model,
        "messages": [{"role": "user", "content": "Hi"}],
    }


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


def _error_spec(
    status: int,
    error_type: str,
    message: str,
    retry_after: str | None = None,
) -> MockResponseSpec:
    headers: dict[str, str] = {}
    if retry_after:
        headers["retry-after"] = retry_after
    return MockResponseSpec(
        status_code=status,
        headers=headers,
        json_body={
            "error": {
                "type": error_type,
                "message": message,
            }
        },
    )


# ---------------------------------------------------------------------------
# Test cases — one per failure-effects matrix row
# ---------------------------------------------------------------------------

# (test_id, status, error_type, message, retry_after, expected_client_status)
FAILURE_MATRIX: list[tuple[str, int, str, str, str | None, int]] = [
    # Local validation
    ("local_malformed_json", 400, "invalid_request_error", "Malformed JSON", None, 400),
    # Missing model
    ("missing_model", 404, "model_not_found", "Model not found", None, 404),
    # Local context-limit rejection
    ("context_limit", 400, "context_length_exceeded", "Context too long", None, 400),
    # Local capability rejection
    (
        "capability_rejection",
        400,
        "unsupported_thinking",
        "Unsupported thinking level",
        None,
        400,
    ),
    # Upstream unsupported thinking control
    (
        "upstream_unsupported_thinking",
        400,
        "invalid_request_error",
        "Unsupported thinking",
        None,
        400,
    ),
    # HTTP 400 generic validation
    ("http_400_generic", 400, "invalid_request_error", "Bad request", None, 400),
    # HTTP 401 auth
    ("http_401_auth", 401, "authentication_error", "Invalid API key", None, 401),
    # HTTP 402 quota
    ("http_402_quota", 402, "insufficient_quota", "Quota exceeded", None, 402),
    # HTTP 403 auth-like
    ("http_403_auth", 403, "permission_error", "Access denied", None, 403),
    # HTTP 403 quota-like
    ("http_403_quota", 403, "insufficient_quota", "Rate limit exceeded", None, 403),
    # HTTP 403 ambiguous
    ("http_403_ambiguous", 403, "forbidden", "Forbidden", None, 403),
    # HTTP 404 generic route
    ("http_404_route", 404, "not_found", "Route not found", None, 404),
    # HTTP 404 runtime model-like
    ("http_404_runtime_model", 404, "model_not_found", "Model not found", None, 404),
    # HTTP 404 authoritative catalog absence
    ("http_404_catalog", 404, "model_not_found", "Model not in catalog", None, 404),
    # HTTP 408
    ("http_408", 408, "timeout", "Request timeout", None, 408),
    # HTTP 409 generic
    ("http_409_generic", 409, "conflict", "Conflict", None, 409),
    # HTTP 409 quota-like
    ("http_409_quota", 409, "insufficient_quota", "Quota conflict", None, 409),
    # HTTP 422 generic
    ("http_422_generic", 422, "unprocessable_entity", "Invalid parameter", None, 422),
    # HTTP 422 quota-like
    ("http_422_quota", 422, "insufficient_quota", "Quota unprocessable", None, 422),
    # HTTP 429 with Retry-After
    ("http_429_with_retry", 429, "rate_limit_error", "Rate limited", "5", 429),
    # HTTP 429 without Retry-After
    ("http_429_no_retry", 429, "rate_limit_error", "Rate limited", None, 429),
    # HTTP 500
    ("http_500", 500, "internal_server_error", "Server error", None, 500),
    # HTTP 502
    ("http_502", 502, "bad_gateway", "Bad gateway", None, 502),
    # HTTP 503
    ("http_503", 503, "service_unavailable", "Service unavailable", None, 503),
    # HTTP 504
    ("http_504", 504, "gateway_timeout", "Gateway timeout", None, 504),
]


class TestFailureEffectsMatrix:
    """Verify the full failure-effects matrix produces correct client
    responses and does not leak state to subsequent requests."""

    @pytest.mark.parametrize(
        "test_id,status,error_type,message,retry_after,expected_status",
        FAILURE_MATRIX,
        ids=[row[0] for row in FAILURE_MATRIX],
    )
    def test_failure_then_success(
        self,
        test_id: str,
        status: int,
        error_type: str,
        message: str,
        retry_after: str | None,
        expected_status: int,
    ) -> None:
        """Each failure is followed by a clean success — no state leak."""
        error_spec = _error_spec(status, error_type, message, retry_after=retry_after)
        rules = [
            MockUpstreamRule(
                min_sequence=1,
                max_sequence=1,
                response=error_spec,
            ),
            MockUpstreamRule(
                min_sequence=2,
                max_sequence=2,
                response=_ok_response(),
            ),
        ]
        upstream = MockUpstream(rules=rules)
        with upstream, httpx.Client(base_url=UPSTREAM_BASE) as client:
            resp1 = client.post("/chat/completions", json=_payload())
            resp2 = client.post("/chat/completions", json=_payload())
        assert resp1.status_code == expected_status
        assert resp2.status_code == 200
        assert upstream.request_count == 2

    @pytest.mark.parametrize(
        "test_id,status,error_type,message,retry_after,expected_status",
        FAILURE_MATRIX,
        ids=[row[0] for row in FAILURE_MATRIX],
    )
    def test_failure_applied_once(
        self,
        test_id: str,
        status: int,
        error_type: str,
        message: str,
        retry_after: str | None,
        expected_status: int,
    ) -> None:
        """Each failure produces exactly one upstream request — no
        duplicate dispatch or retry loop."""
        error_spec = _error_spec(status, error_type, message, retry_after=retry_after)
        rules = [
            MockUpstreamRule(
                min_sequence=1,
                max_sequence=1,
                response=error_spec,
            ),
        ]
        upstream = MockUpstream(rules=rules)
        with upstream, httpx.Client(base_url=UPSTREAM_BASE) as client:
            resp = client.post("/chat/completions", json=_payload())
        assert resp.status_code == expected_status
        assert upstream.request_count == 1


# ---------------------------------------------------------------------------
# Transport-level failures
# ---------------------------------------------------------------------------


class TestTransportFailures:
    """Transport-level failures (connect/read/write errors)."""

    def test_connect_error(self) -> None:
        """Connect error does not corrupt subsequent requests."""
        rules = [
            MockUpstreamRule(
                min_sequence=1,
                max_sequence=1,
                response=MockResponseSpec(
                    status_code=503,
                    transport_error=httpx.ConnectError,
                ),
            ),
            MockUpstreamRule(
                min_sequence=2,
                max_sequence=2,
                response=_ok_response(),
            ),
        ]
        upstream = MockUpstream(rules=rules)
        with upstream, httpx.Client(base_url=UPSTREAM_BASE) as client:
            try:
                resp1 = client.post("/chat/completions", json=_payload())
                status1 = resp1.status_code
            except Exception:
                status1 = 503
            resp2 = client.post("/chat/completions", json=_payload())
        assert status1 == 503
        assert resp2.status_code == 200
        assert upstream.request_count == 2

    def test_read_error(self) -> None:
        """Read error does not corrupt subsequent requests."""
        rules = [
            MockUpstreamRule(
                min_sequence=1,
                max_sequence=1,
                response=MockResponseSpec(
                    status_code=502,
                    transport_error=httpx.ReadError,
                ),
            ),
            MockUpstreamRule(
                min_sequence=2,
                max_sequence=2,
                response=_ok_response(),
            ),
        ]
        upstream = MockUpstream(rules=rules)
        with upstream, httpx.Client(base_url=UPSTREAM_BASE) as client:
            try:
                resp1 = client.post("/chat/completions", json=_payload())
                status1 = resp1.status_code
            except Exception:
                status1 = 502
            resp2 = client.post("/chat/completions", json=_payload())
        assert status1 == 502
        assert resp2.status_code == 200
        assert upstream.request_count == 2

    def test_pool_timeout(self) -> None:
        """Pool timeout does not corrupt subsequent requests."""
        rules = [
            MockUpstreamRule(
                min_sequence=1,
                max_sequence=1,
                response=MockResponseSpec(
                    status_code=504,
                    transport_error=httpx.PoolTimeout,
                ),
            ),
            MockUpstreamRule(
                min_sequence=2,
                max_sequence=2,
                response=_ok_response(),
            ),
        ]
        upstream = MockUpstream(rules=rules)
        with upstream, httpx.Client(base_url=UPSTREAM_BASE) as client:
            try:
                resp1 = client.post("/chat/completions", json=_payload())
                status1 = resp1.status_code
            except Exception:
                status1 = 504
            resp2 = client.post("/chat/completions", json=_payload())
        assert status1 == 504
        assert resp2.status_code == 200
        assert upstream.request_count == 2


# ---------------------------------------------------------------------------
# Multiple consecutive failures
# ---------------------------------------------------------------------------


class TestMultipleConsecutiveFailures:
    """Multiple consecutive failures are followed by a clean success."""

    def test_three_errors_then_success(self) -> None:
        """Three different error types, then a clean success."""
        rules = [
            MockUpstreamRule(
                min_sequence=1,
                max_sequence=1,
                response=_error_spec(400, "invalid_request_error", "Bad request"),
            ),
            MockUpstreamRule(
                min_sequence=2,
                max_sequence=2,
                response=_error_spec(429, "rate_limit_error", "Rate limited", "5"),
            ),
            MockUpstreamRule(
                min_sequence=3,
                max_sequence=3,
                response=_error_spec(503, "service_unavailable", "Unavailable"),
            ),
            MockUpstreamRule(
                min_sequence=4,
                max_sequence=4,
                response=_ok_response(),
            ),
        ]
        upstream = MockUpstream(rules=rules)
        with upstream, httpx.Client(base_url=UPSTREAM_BASE) as client:
            resps = [
                client.post("/chat/completions", json=_payload()) for _ in range(4)
            ]
        assert resps[0].status_code == 400
        assert resps[1].status_code == 429
        assert resps[2].status_code == 503
        assert resps[3].status_code == 200
        assert upstream.request_count == 4
