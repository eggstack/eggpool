"""Tests for catalog fetcher with provider-aware endpoints."""

from __future__ import annotations

import httpx
import pytest

from eggpool.catalog.fetcher import FetchResult, fetch_models_for_account


@pytest.mark.asyncio
async def test_fetch_models_get_default() -> None:
    """Default fetch uses GET /models."""
    mock_response = httpx.Response(
        200,
        json={"data": [{"id": "gpt-4"}]},
        request=httpx.Request("GET", "https://example.com/models"),
    )
    transport = httpx.MockTransport(lambda request: mock_response)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://example.com"
    ) as client:
        result = await fetch_models_for_account(client, "test-key", "acct1")
    assert isinstance(result, FetchResult)
    assert result.response == {"data": [{"id": "gpt-4"}]}
    assert result.status_code == 200
    assert result.error is None
    assert result.model_count == 1
    assert result.latency_ms >= 0


@pytest.mark.asyncio
async def test_fetch_models_post_method() -> None:
    """POST method sends POST request to the configured path."""
    captured_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(
            200,
            json={"data": [{"id": "claude-3"}]},
            request=request,
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://example.com"
    ) as client:
        result = await fetch_models_for_account(
            client,
            "test-key",
            "acct1",
            models_method="POST",
            models_path="/v1/models",
        )
    assert isinstance(result, FetchResult)
    assert result.response == {"data": [{"id": "claude-3"}]}
    assert result.status_code == 200
    assert result.model_count == 1
    assert len(captured_requests) == 1
    assert captured_requests[0].method == "POST"
    assert str(captured_requests[0].url) == "https://example.com/v1/models"


@pytest.mark.asyncio
async def test_fetch_models_custom_path() -> None:
    """Custom path is used for the request."""
    captured_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(
            200,
            json={"data": []},
            request=request,
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://example.com"
    ) as client:
        result = await fetch_models_for_account(
            client,
            "test-key",
            "acct1",
            models_path="/api/models",
        )
    assert isinstance(result, FetchResult)
    assert result.model_count == 0
    assert str(captured_requests[0].url) == "https://example.com/api/models"


@pytest.mark.asyncio
async def test_fetch_models_http_error_returns_empty() -> None:
    """HTTP errors are caught and return empty response."""
    transport = httpx.MockTransport(
        lambda request: httpx.Response(403, text="Forbidden", request=request)
    )
    async with httpx.AsyncClient(
        transport=transport, base_url="https://example.com"
    ) as client:
        result = await fetch_models_for_account(client, "test-key", "acct1")
    assert isinstance(result, FetchResult)
    assert result.response == {}
    assert result.status_code == 403
    assert result.error == "HTTP 403"
    assert result.model_count == 0


@pytest.mark.asyncio
async def test_fetch_models_request_error_returns_empty() -> None:
    """Request errors are caught and return empty response."""

    def _raise(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    transport = httpx.MockTransport(_raise)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://example.com"
    ) as client:
        result = await fetch_models_for_account(client, "test-key", "acct1")
    assert isinstance(result, FetchResult)
    assert result.response == {}
    assert result.status_code is None
    assert result.error is not None
    assert result.model_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "json_data", "expected_error"),
    [
        (b"not json", None, "Invalid JSON response"),
        (None, [], "Invalid model catalog response"),
        (None, {"data": {}}, "Invalid model catalog response"),
        (None, {"data": [None, {"id": 123}]}, "Invalid model catalog response"),
    ],
)
async def test_fetch_models_rejects_malformed_success_responses(
    content: bytes | None,
    json_data: object,
    expected_error: str,
) -> None:
    """A malformed 200 response is a failed refresh, not an empty catalog."""

    def handler(request: httpx.Request) -> httpx.Response:
        if content is not None:
            return httpx.Response(200, content=content, request=request)
        return httpx.Response(200, json=json_data, request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://example.com",
    ) as client:
        result = await fetch_models_for_account(client, "test-key", "acct1")

    assert result.response == {}
    assert result.status_code == 200
    assert result.error == expected_error
    assert result.model_count == 0
