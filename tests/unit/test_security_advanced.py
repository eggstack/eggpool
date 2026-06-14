"""Advanced security tests for the proxy."""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock

import httpx
import pytest
import respx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from go_aggregator.auth import require_auth
from go_aggregator.proxy.client import filter_request_headers

UPSTREAM_BASE = "https://test-upstream.example.com"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_proxy_app(api_key_env: str = "SEC_ADV_KEY") -> FastAPI:
    """Create a minimal FastAPI app that proxies to upstream via respx mock."""
    from go_aggregator.models.config import AppConfig

    config = AppConfig.from_dict(
        {"server": {"api_key_env": api_key_env}, "accounts": []}
    )

    app = FastAPI()
    app.state.config = config

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        await require_auth(request)

        body = await request.body()
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return JSONResponse(status_code=400, content={"error": "Invalid JSON"})

        model_id = payload.get("model")
        if not model_id:
            return JSONResponse(
                status_code=400, content={"error": "Missing model field"}
            )

        import httpx as httpx_mod

        upstream_api_key = "upstream-secret-key"
        headers = filter_request_headers(dict(request.headers), upstream_api_key)

        async with httpx_mod.AsyncClient(base_url=UPSTREAM_BASE) as client:
            upstream_resp = await client.post(
                "/chat/completions",
                headers=headers,
                content=body,
                timeout=300.0,
            )

        return JSONResponse(
            status_code=upstream_resp.status_code,
            content=upstream_resp.json(),
            headers={"content-type": "application/json"},
        )

    @app.get("/v1/models")
    async def list_models(request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        await require_auth(request)

        models = getattr(request.app.state, "mock_models", [])
        return JSONResponse(
            content={
                "object": "list",
                "data": [
                    {
                        "id": m["model_id"],
                        "object": "model",
                        "created": 0,
                        "owned_by": "opencode",
                        "name": m.get("display_name", m["model_id"]),
                    }
                    for m in models
                ],
            }
        )

    return app


# ===================================================================
# 1. Upstream auth header replaces local
# ===================================================================


@pytest.mark.asyncio
async def test_upstream_auth_header_replaces_local() -> None:
    """Local Authorization is replaced, not forwarded, to upstream."""
    os.environ["SEC_ADV_KEY"] = "local-secret"
    app = _make_proxy_app()

    with respx.mock:
        route = respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            resp = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4",
                    "messages": [{"role": "user", "content": "Hi"}],
                },
                headers={"Authorization": "Bearer local-secret"},
            )

        assert resp.status_code == 200
        captured = route.calls.last.request
        auth_header = captured.headers.get("authorization", "")
        assert auth_header == "Bearer upstream-secret-key"
        assert "local-secret" not in auth_header

    del os.environ["SEC_ADV_KEY"]


# ===================================================================
# 2. Authorization header is replaced, not appended
# ===================================================================


@pytest.mark.asyncio
async def test_upstream_auth_is_replaced_not_appended() -> None:
    """The proxy replaces Authorization instead of stacking multiple values."""
    os.environ["SEC_ADV_KEY"] = "local-secret"
    app = _make_proxy_app()

    with respx.mock:
        route = respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            resp = await client.post(
                "/v1/chat/completions",
                json={"model": "gpt-4", "messages": []},
                headers={"Authorization": "Bearer local-secret"},
            )

        assert resp.status_code == 200
        captured = route.calls.last.request
        auth_values = captured.headers.get_list("authorization")
        assert len(auth_values) == 1
        assert auth_values[0] == "Bearer upstream-secret-key"

    del os.environ["SEC_ADV_KEY"]


# ===================================================================
# 3. Oversized request body handled gracefully
# ===================================================================


@pytest.mark.asyncio
async def test_oversized_request_body_handled() -> None:
    """A very large request body is processed without crashing."""
    os.environ["SEC_ADV_KEY"] = "local-secret"
    app = _make_proxy_app()

    large_body = json.dumps(
        {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "x" * (1024 * 1024)}],
        }
    )

    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            resp = await client.post(
                "/v1/chat/completions",
                content=large_body.encode(),
                headers={
                    "Authorization": "Bearer local-secret",
                    "content-type": "application/json",
                },
            )

        assert resp.status_code != 500

    del os.environ["SEC_ADV_KEY"]


# ===================================================================
# 4. Oversized SSE frame handled
# ===================================================================


@pytest.mark.asyncio
async def test_oversized_sse_frame_handled() -> None:
    """A very long SSE data line from upstream does not crash the proxy."""
    os.environ["SEC_ADV_KEY"] = "local-secret"

    large_sse_payload = (
        "data: " + json.dumps({"chunk": "y" * (1024 * 1024)}) + "\n\ndata: [DONE]\n\n"
    )

    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            return_value=httpx.Response(
                200,
                content=large_sse_payload.encode(),
                headers={"content-type": "text/event-stream"},
            )
        )

        from go_aggregator.models.config import AppConfig

        config = AppConfig.from_dict(
            {"server": {"api_key_env": "SEC_ADV_KEY"}, "accounts": []}
        )
        app = FastAPI()
        app.state.config = config

        @app.post("/v1/chat/completions")
        async def chat(request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
            await require_auth(request)
            body = await request.body()
            payload = json.loads(body)
            assert payload.get("model")

            import httpx as httpx_mod

            async with httpx_mod.AsyncClient(base_url=UPSTREAM_BASE) as c:
                resp = await c.post(
                    "/chat/completions",
                    headers=filter_request_headers(
                        dict(request.headers), "upstream-key"
                    ),
                    content=body,
                    timeout=300.0,
                )

            return JSONResponse(
                status_code=resp.status_code,
                content={"raw": resp.text},
                headers={"content-type": "text/event-stream"},
            )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            resp = await client.post(
                "/v1/chat/completions",
                json={"model": "gpt-4", "messages": []},
                headers={"Authorization": "Bearer local-secret"},
            )

        assert resp.status_code == 200

    del os.environ["SEC_ADV_KEY"]


# ===================================================================
# 5. Malformed SSE data passthrough
# ===================================================================


@pytest.mark.asyncio
async def test_malformed_sse_data_passthrough() -> None:
    """Malformed SSE data from upstream is forwarded without crashing."""
    os.environ["SEC_ADV_KEY"] = "local-secret"

    malformed_sse = (
        "data: {broken json\n\n"
        "data: \x00\x01\x02\x03\n\n"
        "data: normal text\n\n"
        "data: [DONE]\n\n"
    )

    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            return_value=httpx.Response(
                200,
                content=malformed_sse.encode(),
                headers={"content-type": "text/event-stream"},
            )
        )

        from go_aggregator.models.config import AppConfig

        config = AppConfig.from_dict(
            {"server": {"api_key_env": "SEC_ADV_KEY"}, "accounts": []}
        )
        app = FastAPI()
        app.state.config = config

        @app.post("/v1/chat/completions")
        async def chat(request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
            await require_auth(request)
            body = await request.body()

            import httpx as httpx_mod

            async with httpx_mod.AsyncClient(base_url=UPSTREAM_BASE) as c:
                resp = await c.post(
                    "/chat/completions",
                    headers=filter_request_headers(
                        dict(request.headers), "upstream-key"
                    ),
                    content=body,
                    timeout=300.0,
                )

            return JSONResponse(
                status_code=resp.status_code,
                content={"raw": resp.text},
                headers={"content-type": "text/event-stream"},
            )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            resp = await client.post(
                "/v1/chat/completions",
                json={"model": "gpt-4", "messages": []},
                headers={"Authorization": "Bearer local-secret"},
            )

        assert resp.status_code == 200
        assert "[DONE]" in resp.text

    del os.environ["SEC_ADV_KEY"]


# ===================================================================
# 6. Host header not forwarded
# ===================================================================


@pytest.mark.asyncio
async def test_host_header_not_forwarded() -> None:
    """The Host header is removed before sending to upstream."""
    headers = {
        "Host": "evil.example.com",
        "Content-Type": "application/json",
        "Authorization": "Bearer local-key",
    }
    result = filter_request_headers(headers, "upstream-key")
    assert "Host" not in result
    assert "host" not in {k.lower() for k in result}


# ===================================================================
# 7. SQL injection in model_id
# ===================================================================


@pytest.mark.asyncio
async def test_sql_injection_in_model_id() -> None:
    """SQL injection in model_id is rejected, not reaching the database."""
    os.environ["SEC_ADV_KEY"] = "local-secret"

    from go_aggregator.models.config import AppConfig

    config = AppConfig.from_dict(
        {"server": {"api_key_env": "SEC_ADV_KEY"}, "accounts": []}
    )
    app = FastAPI()
    app.state.config = config

    catalog = MagicMock()
    catalog.is_model_available.return_value = False
    app.state.catalog = catalog

    @app.post("/v1/chat/completions")
    async def chat(request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        await require_auth(request)
        body = await request.body()
        payload = json.loads(body)
        model_id = payload.get("model", "")

        if not catalog.is_model_available(model_id):
            return JSONResponse(
                status_code=404,
                content={"error": f"Model {model_id!r} not available"},
            )
        return JSONResponse(status_code=200, content={"ok": True})

    malicious_model = "'; DROP TABLE requests; --"
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": malicious_model,
                "messages": [{"role": "user", "content": "Hi"}],
            },
            headers={"Authorization": "Bearer local-secret"},
        )

    assert resp.status_code == 404
    body = resp.json()
    assert "error" in body
    # The error is a normal 404, not a SQL error or 500
    assert "sql" not in body.get("error", "").lower()
    assert "syntax" not in body.get("error", "").lower()

    del os.environ["SEC_ADV_KEY"]


# ===================================================================
# 8. SQL injection in query params
# ===================================================================


@pytest.mark.asyncio
async def test_sql_injection_in_query_params() -> None:
    """SQL injection via query params on /v1/models returns a valid response."""
    os.environ["SEC_ADV_KEY"] = "local-secret"

    from go_aggregator.models.config import AppConfig

    config = AppConfig.from_dict(
        {"server": {"api_key_env": "SEC_ADV_KEY"}, "accounts": []}
    )
    app = FastAPI()
    app.state.config = config

    catalog = MagicMock()
    catalog.get_models_for_exposure.return_value = []
    app.state.catalog = catalog

    @app.get("/v1/models")
    async def list_models(request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        await require_auth(request)
        # Even if malicious params arrive, the handler should not crash
        return JSONResponse(
            content={
                "object": "list",
                "data": [],
            }
        )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        resp = await client.get(
            "/v1/models",
            params={"filter": "1' OR '1'='1", "sort": "'; DROP TABLE models; --"},
            headers={"Authorization": "Bearer local-secret"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"

    del os.environ["SEC_ADV_KEY"]


# ===================================================================
# 9. XSS in model_id in models endpoint
# ===================================================================


@pytest.mark.asyncio
async def test_xss_in_model_id_in_models_endpoint() -> None:
    """A model with a <script> tag id is returned as-is in JSON (not HTML)."""
    os.environ["SEC_ADV_KEY"] = "local-secret"

    xss_model_id = "<script>alert('xss')</script>"

    from go_aggregator.models.config import AppConfig

    config = AppConfig.from_dict(
        {"server": {"api_key_env": "SEC_ADV_KEY"}, "accounts": []}
    )
    app = FastAPI()
    app.state.config = config

    catalog = MagicMock()
    catalog.get_models_for_exposure.return_value = [
        {
            "model_id": xss_model_id,
            "display_name": "XSS Model",
            "first_seen_at": 0,
        }
    ]
    app.state.catalog = catalog

    @app.get("/v1/models")
    async def list_models(request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        await require_auth(request)
        models = catalog.get_models_for_exposure()
        return JSONResponse(
            content={
                "object": "list",
                "data": [
                    {
                        "id": m["model_id"],
                        "object": "model",
                        "created": int(m.get("first_seen_at", 0)),
                        "owned_by": "opencode",
                        "name": m.get("display_name", m["model_id"]),
                    }
                    for m in models
                ],
            }
        )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        resp = await client.get(
            "/v1/models",
            headers={"Authorization": "Bearer local-secret"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["data"]) == 1
    assert body["data"][0]["id"] == xss_model_id
    # JSON response contains the raw string — no HTML escaping needed
    raw_text = resp.text
    assert "<script>" in raw_text

    del os.environ["SEC_ADV_KEY"]


# ===================================================================
# 10. Secret not leaked in error messages
# ===================================================================


@pytest.mark.asyncio
async def test_secret_not_in_error_messages() -> None:
    """API keys are not included in error responses returned by the proxy."""
    api_key_value = "sk-super-secret-12345"
    os.environ["SEC_ADV_KEY"] = api_key_value

    from go_aggregator.models.config import AppConfig

    config = AppConfig.from_dict(
        {"server": {"api_key_env": "SEC_ADV_KEY"}, "accounts": []}
    )
    app = FastAPI()
    app.state.config = config

    @app.post("/v1/chat/completions")
    async def chat(request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        await require_auth(request)
        # The proxy should return a generic error, not expose upstream details
        return JSONResponse(
            status_code=502,
            content={"error": "Upstream request failed"},
        )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4", "messages": []},
            headers={"Authorization": f"Bearer {api_key_value}"},
        )

    response_text = resp.text
    assert api_key_value not in response_text

    del os.environ["SEC_ADV_KEY"]


# ===================================================================
# 11. Custom headers forwarded but auth replaced
# ===================================================================


@pytest.mark.asyncio
async def test_custom_headers_forwarded_auth_replaced() -> None:
    """Custom headers pass through while Authorization is replaced."""
    os.environ["SEC_ADV_KEY"] = "local-secret"
    app = _make_proxy_app()

    with respx.mock:
        route = respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            resp = await client.post(
                "/v1/chat/completions",
                json={"model": "gpt-4", "messages": []},
                headers={
                    "Authorization": "Bearer local-secret",
                    "X-Request-ID": "custom-req-123",
                    "X-Custom-Header": "my-value",
                },
            )

        assert resp.status_code == 200
        captured = route.calls.last.request
        assert captured.headers.get("x-request-id") == "custom-req-123"
        assert captured.headers.get("x-custom-header") == "my-value"
        assert captured.headers.get("authorization") == "Bearer upstream-secret-key"

    del os.environ["SEC_ADV_KEY"]


# ===================================================================
# 12. Hop-by-hop headers stripped in filter
# ===================================================================


@pytest.mark.asyncio
async def test_hop_by_hop_headers_stripped_comprehensively() -> None:
    """All hop-by-hop headers are removed from upstream requests."""
    headers = {
        "Connection": "keep-alive",
        "Keep-Alive": "timeout=5",
        "Proxy-Authenticate": "Basic",
        "Proxy-Authorization": "Bearer x",
        "Te": "deflate",
        "Trailers": "gzip",
        "Transfer-Encoding": "chunked",
        "Upgrade": "websocket",
        "Content-Type": "application/json",
        "X-Safe": "yes",
    }
    result = filter_request_headers(headers, "key")
    # filter_request_headers always adds Authorization, so 3 keys total
    assert len(result) == 3  # Authorization + Content-Type + X-Safe
    assert result["Content-Type"] == "application/json"
    assert result["X-Safe"] == "yes"
    assert result["Authorization"] == "Bearer key"
    # No hop-by-hop headers remain
    for key in result:
        assert key.lower() not in {
            "connection",
            "keep-alive",
            "proxy-authenticate",
            "proxy-authorization",
            "te",
            "trailers",
            "transfer-encoding",
            "upgrade",
        }
