"""Tests for proxy header filtering."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.responses import JSONResponse
from starlette.requests import Request

from eggpool.api.proxy_request import (
    ProxyEndpointConfig,
    get_client_ip,
    handle_proxy_request,
)
from eggpool.metrics.model_router import ModelRouterMetrics
from eggpool.model_router.affinity import ModelRouterAffinity
from eggpool.model_router.config import ModelRouterConfig
from eggpool.model_router.registry import ModelRouterRegistry
from eggpool.models.config import AppConfig
from eggpool.proxy.client import (
    HOP_BY_HOP_HEADERS,
    filter_request_headers,
    filter_response_headers,
)
from eggpool.request.coordinator import PreparedProxyResponse


def _request_with_peer(peer: str, headers: list[tuple[bytes, bytes]]) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": headers,
        "client": (peer, 1234),
        "server": ("127.0.0.1", 11300),
        "scheme": "http",
    }
    return Request(scope, lambda: None)  # type: ignore[arg-type]


def test_forwarded_client_ip_ignored_from_untrusted_peer() -> None:
    request = _request_with_peer(
        "10.0.0.8",
        [(b"x-forwarded-for", b"198.51.100.4"), (b"x-real-ip", b"203.0.113.5")],
    )
    assert get_client_ip(request, trusted_proxies=("127.0.0.1",)) == "10.0.0.8"


def test_forwarded_client_ip_honored_from_trusted_peer() -> None:
    request = _request_with_peer(
        "127.0.0.1",
        [(b"x-forwarded-for", b"198.51.100.4, 127.0.0.1")],
    )
    assert get_client_ip(request, trusted_proxies=("127.0.0.1",)) == "198.51.100.4"


def test_forwarded_client_ip_uses_nearest_untrusted_hop() -> None:
    request = _request_with_peer(
        "127.0.0.1",
        [(b"x-forwarded-for", b"198.51.100.4, 10.0.0.8")],
    )
    assert get_client_ip(request, trusted_proxies=("127.0.0.1",)) == "10.0.0.8"


def test_malformed_forwarded_client_ip_falls_back_to_peer() -> None:
    request = _request_with_peer(
        "127.0.0.1",
        [(b"x-forwarded-for", b"\x00" + b"x" * 100)],
    )
    assert get_client_ip(request, trusted_proxies=("127.0.0.1",)) == "127.0.0.1"


def test_forwarded_client_ip_skips_empty_entries() -> None:
    request = _request_with_peer(
        "127.0.0.1",
        [(b"x-forwarded-for", b", 198.51.100.4")],
    )
    assert get_client_ip(request, trusted_proxies=("127.0.0.1",)) == "198.51.100.4"


def test_forwarded_ipv6_with_zone_id_passes_through() -> None:
    """IPv6 zone identifiers (RFC 6874) appear in ``%`` form in raw headers."""
    request = _request_with_peer(
        "::1",
        [(b"x-forwarded-for", b"fe80::1%eth0")],
    )
    assert get_client_ip(request, trusted_proxies=("::1",)) == "fe80::1%eth0"


def test_filter_request_headers_removes_auth() -> None:
    headers = {
        "Authorization": "Bearer local-key",
        "Content-Type": "application/json",
    }
    result = filter_request_headers(headers, "upstream-key")
    # Original dict is not modified
    assert "Authorization" in headers
    # Result has the upstream key
    assert result["Authorization"] == "Bearer upstream-key"
    assert result["Content-Type"] == "application/json"


def test_filter_request_headers_removes_hop_by_hop() -> None:
    headers = {
        "Connection": "keep-alive",
        "Transfer-Encoding": "chunked",
        "Content-Type": "application/json",
    }
    result = filter_request_headers(headers, "key")
    assert "Connection" not in result
    assert "Transfer-Encoding" not in result
    assert "Content-Type" in result


def test_filter_request_headers_removes_host() -> None:
    headers = {
        "Host": "example.com",
        "Content-Type": "application/json",
    }
    result = filter_request_headers(headers, "key")
    assert "Host" not in result


def test_filter_response_headers_removes_hop_by_hop() -> None:
    class MockHeaders:
        def __init__(self, h: list[tuple[bytes, bytes]]) -> None:
            self._raw = h

        @property
        def raw(self) -> list[tuple[bytes, bytes]]:
            return self._raw

    headers = MockHeaders(
        [
            (b"Content-Type", b"application/json"),
            (b"Connection", b"keep-alive"),
            (b"X-Custom", b"value"),
        ]
    )
    result = filter_response_headers(headers)  # type: ignore[arg-type]
    result_dict = {k.lower(): v for k, v in result}
    assert "content-type" in result_dict
    assert "connection" not in result_dict
    assert "x-custom" in result_dict


def test_hop_by_hop_headers_complete() -> None:
    expected = {
        "connection",
        "keep-alive",
        "proxy-connection",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
    assert expected == HOP_BY_HOP_HEADERS


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["chat_completions", "responses", "messages"])
@pytest.mark.parametrize(
    ("sticky", "expected_selector_calls", "expected_hits"),
    [(True, 1, 1), (False, 2, 0)],
)
async def test_sticky_virtual_request_reuses_concrete_target(
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
    sticky: bool,
    expected_selector_calls: int,
    expected_hits: int,
) -> None:
    router_config = ModelRouterConfig.model_validate(
        {
            "selector_model": "selector/local",
            "default_model": "model-default",
            "sticky": sticky,
            "routes": {
                "default": {"model": "model-default", "description": "Default"},
                "fast": {
                    "model": "model-fast/provider-a",
                    "description": "Fast",
                },
            },
        }
    )
    router_registry = ModelRouterRegistry.from_config({"virtual": router_config})
    affinity = ModelRouterAffinity()
    selector_calls = 0
    parent_models: list[str] = []

    class FakeCoordinator:
        async def execute(self, context: Any) -> PreparedProxyResponse:
            nonlocal selector_calls
            model_id = context.model_id
            if model_id == "selector/local":
                selector_calls += 1
                return PreparedProxyResponse(
                    status_code=200,
                    headers=[],
                    body=b'{"choices":[{"message":{"content":"1"}}]}',
                )
            parent_models.append(model_id)
            return PreparedProxyResponse(status_code=200, headers=[], body=b"ok")

    class FakeLease:
        def __init__(self) -> None:
            self.runtime = type(
                "Runtime",
                (),
                {
                    "coordinator": FakeCoordinator(),
                    "dispatch_span_recorder": None,
                    "model_router_registry": router_registry,
                    "immutable_request_state": type(
                        "ImmutableState",
                        (),
                        {
                            "provider_ids": frozenset({"provider-a"}),
                            "trusted_proxies": frozenset(),
                        },
                    )(),
                    "config": AppConfig(),
                    "catalog": type("Catalog", (), {"cache": object()})(),
                    "transcoder_policy": object(),
                },
            )()
            self.released = False

        async def release(self) -> None:
            self.released = True

    leases: list[FakeLease] = []

    async def acquire() -> FakeLease:
        lease = FakeLease()
        lease.runtime.coordinator = coordinator
        leases.append(lease)
        return lease

    coordinator = FakeCoordinator()
    metrics = ModelRouterMetrics()
    process = SimpleNamespace(
        model_router_affinity=affinity,
        model_router_metrics=metrics,
    )
    manager = SimpleNamespace(acquire=acquire)
    app = SimpleNamespace(
        state=SimpleNamespace(
            runtime_manager=manager,
            config=AppConfig(),
            process=process,
        )
    )

    monkeypatch.setattr(
        "eggpool.api.proxy_request._check_context_limits",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "eggpool.api.proxy_request._prepare_transcode_preflight",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "eggpool.api.proxy_request.classify_thinking_request",
        lambda *args: None,
    )
    monkeypatch.setattr(
        "eggpool.api.proxy_request.estimate_reservation_tokens",
        lambda body: 1,
    )

    is_messages = surface == "messages"
    is_responses = surface == "responses"
    if is_messages:
        request_path = "/v1/messages"
        request_payload = (
            b'{"model":"virtual","max_tokens":16,'
            b'"messages":[{"role":"user","content":"hello"}]}'
        )
    elif is_responses:
        request_path = "/v1/responses"
        request_payload = b'{"model":"virtual","store":false,"input":"hello"}'
    else:
        request_path = "/v1/chat/completions"
        request_payload = (
            b'{"model":"virtual","messages":[{"role":"user","content":"hello"}]}'
        )

    async def request_body() -> dict[str, object]:
        return {"type": "http.request", "body": request_payload, "more_body": False}

    endpoint = ProxyEndpointConfig(
        protocol="anthropic" if is_messages else "openai",
        request_label="test",
        error_response=lambda status_code, message, error_type="server_error": (
            JSONResponse(
                {"message": message, "type": error_type}, status_code=status_code
            )
        ),
        not_found_error_type="not_found",
        service_error_type="server_error",
        request_surface="responses" if is_responses else "chat_completions",
    )

    for _ in range(2):
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": request_path,
                "raw_path": request_path.encode(),
                "query_string": b"",
                "headers": [
                    (b"content-type", b"application/json"),
                    *(
                        [(b"x-eggpool-route-session", b"responses-session")]
                        if is_responses
                        else []
                    ),
                ],
                "client": ("127.0.0.1", 1234),
                "server": ("127.0.0.1", 11300),
                "scheme": "http",
                "app": app,
            },
            request_body,
        )
        response = await handle_proxy_request(request, endpoint)
        assert response.status_code == 200

    assert selector_calls == expected_selector_calls
    assert parent_models == ["model-fast", "model-fast"]
    assert affinity.stats.hits == expected_hits
    assert metrics.snapshot()["virtual_requests"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["chat_completions", "responses", "messages"])
@pytest.mark.parametrize("model_value", ["model-default", "model-fast/provider-a"])
async def test_feature_off_concrete_requests_bypass_router(
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
    model_value: str,
) -> None:
    """Feature-off requests must not touch semantic routing (Plan 166 §2)."""
    from eggpool.model_router import selector as selector_module

    router_registry = ModelRouterRegistry.empty()
    parent_models: list[str] = []

    class _RaisingAffinity:
        async def resolve(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("affinity must not be invoked feature-off")

    class _RaisingMetrics:
        def record_resolution(self, *args: Any, **kwargs: Any) -> None:
            raise AssertionError("metrics must not be invoked feature-off")

    async def _raising_select(self: Any, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("selector must not be invoked feature-off")

    monkeypatch.setattr(selector_module.ModelRouterSelector, "select", _raising_select)

    class FakeCoordinator:
        async def execute(self, context: Any) -> PreparedProxyResponse:
            parent_models.append(context.model_id)
            return PreparedProxyResponse(status_code=200, headers=[], body=b"ok")

    class FakeLease:
        def __init__(self) -> None:
            self.runtime = type(
                "Runtime",
                (),
                {
                    "coordinator": FakeCoordinator(),
                    "dispatch_span_recorder": None,
                    "model_router_registry": router_registry,
                    "immutable_request_state": type(
                        "ImmutableState",
                        (),
                        {
                            "provider_ids": frozenset({"provider-a"}),
                            "trusted_proxies": frozenset(),
                        },
                    )(),
                    "config": AppConfig(),
                    "catalog": type("Catalog", (), {"cache": object()})(),
                    "transcoder_policy": object(),
                },
            )()
            self.released = False

        async def release(self) -> None:
            self.released = True

    coordinator = FakeCoordinator()
    process = SimpleNamespace(
        model_router_affinity=_RaisingAffinity(),
        model_router_metrics=_RaisingMetrics(),
    )
    leases: list[FakeLease] = []

    async def _new_lease() -> FakeLease:
        lease = FakeLease()
        lease.runtime.coordinator = coordinator  # type: ignore[attr-defined]
        leases.append(lease)
        return lease

    async def acquire() -> FakeLease:
        return await _new_lease()

    manager = SimpleNamespace(acquire=acquire)
    app = SimpleNamespace(
        state=SimpleNamespace(
            runtime_manager=manager,
            config=AppConfig(),
            process=process,
        )
    )

    monkeypatch.setattr(
        "eggpool.api.proxy_request._check_context_limits",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "eggpool.api.proxy_request._prepare_transcode_preflight",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "eggpool.api.proxy_request.classify_thinking_request",
        lambda *args: None,
    )
    monkeypatch.setattr(
        "eggpool.api.proxy_request.estimate_reservation_tokens",
        lambda body: 1,
    )

    is_messages = surface == "messages"
    is_responses = surface == "responses"
    if is_messages:
        request_path = "/v1/messages"
        base_payload = (
            f'{{"model":{json.dumps(model_value)},"max_tokens":16,'
            '"messages":[{"role":"user","content":"hello"}]}'
        )
    elif is_responses:
        request_path = "/v1/responses"
        base_payload = (
            f'{{"model":{json.dumps(model_value)},"store":false,"input":"hello"}}'
        )
    else:
        request_path = "/v1/chat/completions"
        base_payload = (
            f'{{"model":{json.dumps(model_value)},'
            '"messages":[{"role":"user","content":"hello"}]}'
        )

    async def request_body() -> dict[str, object]:
        return {
            "type": "http.request",
            "body": base_payload.encode(),
            "more_body": False,
        }

    endpoint = ProxyEndpointConfig(
        protocol="anthropic" if is_messages else "openai",
        request_label="test",
        error_response=lambda status_code, message, error_type="server_error": (
            JSONResponse(
                {"message": message, "type": error_type}, status_code=status_code
            )
        ),
        not_found_error_type="not_found",
        service_error_type="server_error",
        request_surface="responses" if is_responses else "chat_completions",
    )

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": request_path,
            "raw_path": request_path.encode(),
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
            "client": ("127.0.0.1", 1234),
            "server": ("127.0.0.1", 11300),
            "scheme": "http",
            "app": app,
        },
        request_body,
    )
    response = await handle_proxy_request(request, endpoint)
    assert response.status_code == 200
    expected_model = model_value.split("/")[0]
    assert parent_models == [expected_model]


@pytest.mark.asyncio
async def test_feature_off_streaming_tools_and_error_path_bypass_router(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Streaming/tool and error shapes must also bypass routing feature-off."""
    from eggpool.model_router import selector as selector_module

    router_registry = ModelRouterRegistry.empty()

    class _RaisingAffinity:
        async def resolve(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("affinity must not be invoked feature-off")

    class _RaisingMetrics:
        def record_resolution(self, *args: Any, **kwargs: Any) -> None:
            raise AssertionError("metrics must not be invoked feature-off")

    async def _raising_select(self: Any, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("selector must not be invoked feature-off")

    monkeypatch.setattr(selector_module.ModelRouterSelector, "select", _raising_select)

    parent_models: list[str] = []

    class FakeCoordinator:
        async def execute(self, context: Any) -> PreparedProxyResponse:
            parent_models.append(context.model_id)
            return PreparedProxyResponse(status_code=200, headers=[], body=b"ok")

    coordinator = FakeCoordinator()

    class FakeLease:
        def __init__(self) -> None:
            self.runtime = type(
                "Runtime",
                (),
                {
                    "coordinator": coordinator,
                    "dispatch_span_recorder": None,
                    "model_router_registry": router_registry,
                    "immutable_request_state": type(
                        "ImmutableState",
                        (),
                        {
                            "provider_ids": frozenset(),
                            "trusted_proxies": frozenset(),
                        },
                    )(),
                    "config": AppConfig(),
                    "catalog": type("Catalog", (), {"cache": object()})(),
                    "transcoder_policy": object(),
                },
            )()
            self.released = False

        async def release(self) -> None:
            self.released = True

    async def acquire() -> FakeLease:
        return FakeLease()

    process = SimpleNamespace(
        model_router_affinity=_RaisingAffinity(),
        model_router_metrics=_RaisingMetrics(),
    )
    app = SimpleNamespace(
        state=SimpleNamespace(
            runtime_manager=SimpleNamespace(acquire=acquire),
            config=AppConfig(),
            process=process,
        )
    )

    monkeypatch.setattr(
        "eggpool.api.proxy_request._check_context_limits",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "eggpool.api.proxy_request._prepare_transcode_preflight",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "eggpool.api.proxy_request.classify_thinking_request",
        lambda *args: None,
    )
    monkeypatch.setattr(
        "eggpool.api.proxy_request.estimate_reservation_tokens",
        lambda body: 1,
    )

    endpoint = ProxyEndpointConfig(
        protocol="openai",
        request_label="test",
        error_response=lambda status_code, message, error_type="server_error": (
            JSONResponse(
                {"message": message, "type": error_type}, status_code=status_code
            )
        ),
        not_found_error_type="not_found",
        service_error_type="server_error",
    )

    async def _send(payload_bytes: bytes) -> Any:
        async def request_body() -> dict[str, object]:
            return {
                "type": "http.request",
                "body": payload_bytes,
                "more_body": False,
            }

        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/v1/chat/completions",
                "raw_path": b"/v1/chat/completions",
                "query_string": b"",
                "headers": [(b"content-type", b"application/json")],
                "client": ("127.0.0.1", 1234),
                "server": ("127.0.0.1", 11300),
                "scheme": "http",
                "app": app,
            },
            request_body,
        )
        return await handle_proxy_request(request, endpoint)

    streaming_tools = json.dumps(
        {
            "model": "model-default",
            "stream": True,
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [
                {
                    "type": "function",
                    "function": {"name": "lookup", "parameters": {"type": "object"}},
                }
            ],
        }
    ).encode()
    response = await _send(streaming_tools)
    assert response.status_code == 200
    assert parent_models == ["model-default"]

    missing_model = b'{"messages":[{"role":"user","content":"hello"}]}'
    response = await _send(missing_model)
    assert response.status_code == 400
    # Error path must not have produced a coordinator dispatch.
    assert parent_models == ["model-default"]


@pytest.mark.asyncio
async def test_lease_failure_log_uses_proxy_request_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    runtime_manager = SimpleNamespace(
        acquire=AsyncMock(side_effect=RuntimeError("unavailable"))
    )
    app = SimpleNamespace(state=SimpleNamespace(runtime_manager=runtime_manager))
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": [],
        "app": app,
    }
    request = Request(scope, lambda: None)  # type: ignore[arg-type]
    endpoint = ProxyEndpointConfig(
        protocol="openai",
        request_label="test",
        error_response=lambda status_code, message, error_type="server_error": (
            JSONResponse(
                {"message": message, "type": error_type}, status_code=status_code
            )
        ),
        not_found_error_type="not_found",
        service_error_type="server_error",
    )

    with caplog.at_level("WARNING", logger="eggpool.api.proxy_request"):
        response = await handle_proxy_request(request, endpoint)

    assert response.status_code == 503
    record = next(
        record
        for record in caplog.records
        if record.message == "Runtime lease acquisition failed; returning 503"
    )
    assert record.proxy_request_id == request.state.proxy_request_id
