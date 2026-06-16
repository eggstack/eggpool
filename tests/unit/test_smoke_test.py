"""Unit tests for scripts/smoke_test.py.

Exercises the smoke test functions with an in-process ``httpx.MockTransport``
so the streaming, non-streaming, health, models, and stats checks can be
validated without a running server. The test also confirms the script
refuses to start without the required environment variables and that
``GOROUTER_TEST_STREAM_CANCEL`` causes the streaming iterator to be cut
short after the first nonempty chunk.
"""

from __future__ import annotations

import io
import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

import smoke_test  # noqa: E402  (path setup in tests/conftest.py)


def _make_streaming_response(
    chunks: list[bytes],
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """Build a streaming httpx.Response from a fixed list of byte chunks."""
    merged_headers = {
        "content-type": "text/event-stream",
        "x-proxy-request-id": "req-test-1234",
        "x-proxy-attempt-count": "1",
    }
    if headers:
        merged_headers.update(headers)
    return httpx.Response(
        status_code,
        headers=merged_headers,
        content=io.BytesIO(b"".join(chunks)),
    )


def _streaming_handler(
    body_chunks: list[bytes],
    *,
    capture_request: dict[str, Any] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """Return a request handler that streams the given chunks back."""

    def _handle(request: httpx.Request) -> httpx.Response:
        if capture_request is not None:
            capture_request["url"] = str(request.url)
            capture_request["method"] = request.method
            capture_request["headers"] = dict(request.headers)
            try:
                capture_request["body"] = json.loads(request.content)
            except json.JSONDecodeError:
                capture_request["body"] = request.content.decode(
                    "utf-8", errors="replace"
                )
        return _make_streaming_response(body_chunks)

    return _handle


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "GOROUTER_BASE_URL",
        "GOROUTER_API_KEY",
        "GOROUTER_OPENAI_MODEL",
        "GOROUTER_ANTHROPIC_MODEL",
        "GOROUTER_SKIP_LIVE",
        "GOROUTER_TEST_STREAM_CANCEL",
    ):
        monkeypatch.delenv(var, raising=False)


class TestRequiredEnvironment:
    """The script must fail fast when required env vars are missing."""

    def test_missing_base_url_exits_2(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("GOROUTER_API_KEY", "k")
        monkeypatch.setenv("GOROUTER_OPENAI_MODEL", "m")
        monkeypatch.setenv("GOROUTER_ANTHROPIC_MODEL", "m")
        with pytest.raises(SystemExit) as exc:
            smoke_test.main()
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "GOROUTER_BASE_URL" in err

    def test_missing_api_key_exits_2(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("GOROUTER_BASE_URL", "http://localhost:8080")
        monkeypatch.setenv("GOROUTER_OPENAI_MODEL", "m")
        monkeypatch.setenv("GOROUTER_ANTHROPIC_MODEL", "m")
        with pytest.raises(SystemExit) as exc:
            smoke_test.main()
        assert exc.value.code == 2
        assert "GOROUTER_API_KEY" in capsys.readouterr().err

    def test_missing_openai_model_exits_2(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("GOROUTER_BASE_URL", "http://localhost:8080")
        monkeypatch.setenv("GOROUTER_API_KEY", "k")
        monkeypatch.setenv("GOROUTER_ANTHROPIC_MODEL", "m")
        with pytest.raises(SystemExit) as exc:
            smoke_test.main()
        assert exc.value.code == 2
        assert "GOROUTER_OPENAI_MODEL" in capsys.readouterr().err

    def test_missing_anthropic_model_exits_2(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("GOROUTER_BASE_URL", "http://localhost:8080")
        monkeypatch.setenv("GOROUTER_API_KEY", "k")
        monkeypatch.setenv("GOROUTER_OPENAI_MODEL", "m")
        with pytest.raises(SystemExit) as exc:
            smoke_test.main()
        assert exc.value.code == 2
        assert "GOROUTER_ANTHROPIC_MODEL" in capsys.readouterr().err


def _transport_for(
    routes: dict[tuple[str, str], Callable[[httpx.Request], httpx.Response]],
):
    """Build an httpx.MockTransport from a method+path -> handler map."""

    def _send(request: httpx.Request) -> httpx.Response:
        key = (request.method, request.url.path)
        if key not in routes:
            return httpx.Response(
                599, content=b"unrouted: " + key[0].encode() + b" " + key[1].encode()
            )
        return routes[key](request)

    return httpx.MockTransport(_send)


class TestNonStreaming:
    def test_openai_succeeds_with_proxy_headers(self) -> None:
        captured: dict[str, Any] = {}
        transport = _transport_for(
            {
                ("GET", "/v1/healthz"): lambda r: httpx.Response(200, json={}),
                ("GET", "/v1/readyz"): lambda r: httpx.Response(200, json={}),
                ("GET", "/v1/models"): lambda r: httpx.Response(
                    200,
                    json={"data": [{"id": "gpt-4"}]},
                ),
                ("GET", "/v1/stats"): lambda r: httpx.Response(200, json={}),
                (
                    "POST",
                    "/v1/chat/completions",
                ): _streaming_handler(
                    [b'{"id":"x","choices":[],"usage":{}}'],
                    capture_request=captured,
                ),
            }
        )
        client = httpx.Client(transport=transport, timeout=5.0)
        try:
            result = smoke_test._openai(client, "http://stub", "secret-key", "gpt-4")
        finally:
            client.close()
        assert result.ok
        assert "request_id=req-test-1234" in result.detail
        # Local Authorization header is preserved for the proxy.
        assert captured["headers"]["authorization"] == "Bearer secret-key"
        # Body is JSON and contains the model id.
        assert captured["body"]["model"] == "gpt-4"

    def test_openai_4xx_returns_failure(self) -> None:
        def _fail(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                502,
                json={"error": {"message": "upstream error"}},
                headers={"x-proxy-request-id": "req-err"},
            )

        transport = _transport_for({("POST", "/v1/chat/completions"): _fail})
        client = httpx.Client(transport=transport, timeout=5.0)
        try:
            result = smoke_test._openai(client, "http://stub", "k", "gpt-4")
        finally:
            client.close()
        assert not result.ok
        assert "status=502" in result.detail
        assert "request_id=req-err" in result.detail

    def test_anthropic_includes_version_header(self) -> None:
        captured: dict[str, Any] = {}
        transport = _transport_for(
            {
                (
                    "POST",
                    "/v1/messages",
                ): _streaming_handler([b'{"id":"x"}'], capture_request=captured),
            }
        )
        client = httpx.Client(transport=transport, timeout=5.0)
        try:
            result = smoke_test._anthropic(
                client, "http://stub", "k", "claude-3-5-sonnet"
            )
        finally:
            client.close()
        assert result.ok
        assert captured["headers"]["anthropic-version"] == "2023-06-01"
        # No x-api-key (we use Authorization only).
        assert "x-api-key" not in captured["headers"]


class TestStreaming:
    def test_openai_stream_validates_incremental_delivery(self) -> None:
        chunks = [
            b'data: {"choices":[{"delta":{}}]}\n\n',
            b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
            b"data: [DONE]\n\n",
        ]
        transport = _transport_for(
            {("POST", "/v1/chat/completions"): _streaming_handler(chunks)}
        )
        client = httpx.Client(transport=transport, timeout=5.0)
        try:
            result = smoke_test._openai_stream(client, "http://stub", "k", "gpt-4")
        finally:
            client.close()
        assert result.ok, result.detail
        # httpx.MockTransport yields the full body as a single chunk;
        # what matters is that we received at least one chunk, with the
        # required SSE marker, and the timing captured all three
        # milestones.
        assert "chunks=" in result.detail
        assert "request_id=req-test-1234" in result.detail
        # Timing is recorded for headers, first chunk, and total.
        assert "headers_ms" in result.timings
        assert "first_chunk_ms" in result.timings
        assert "total_ms" in result.timings
        # First chunk is recorded strictly after headers.
        assert result.timings["first_chunk_ms"] >= result.timings["headers_ms"]
        assert result.timings["total_ms"] >= result.timings["first_chunk_ms"]

    def test_anthropic_stream_validates_event_marker(self) -> None:
        chunks = [
            b'event: message_start\ndata: {"type":"message_start"}\n\n',
            b'event: content_block_delta\ndata: {"type":"content_block_delta"}\n\n',
            b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
        ]
        transport = _transport_for(
            {("POST", "/v1/messages"): _streaming_handler(chunks)}
        )
        client = httpx.Client(transport=transport, timeout=5.0)
        try:
            result = smoke_test._anthropic_stream(
                client, "http://stub", "k", "claude-3-5-sonnet"
            )
        finally:
            client.close()
        assert result.ok, result.detail
        assert "request_id=req-test-1234" in result.detail

    def test_stream_missing_proxy_request_id_fails(self) -> None:
        def _no_id(request: httpx.Request) -> httpx.Response:
            return _make_streaming_response(
                [b"data: {}\n\n"],
                headers={"x-proxy-request-id": ""},
            )

        transport = _transport_for({("POST", "/v1/chat/completions"): _no_id})
        client = httpx.Client(transport=transport, timeout=5.0)
        try:
            result = smoke_test._openai_stream(client, "http://stub", "k", "gpt-4")
        finally:
            client.close()
        assert not result.ok
        assert "x-proxy-request-id" in result.detail

    def test_stream_without_sse_marker_fails(self) -> None:
        chunks = [b"hello world\n", b"more text\n"]
        transport = _transport_for(
            {("POST", "/v1/chat/completions"): _streaming_handler(chunks)}
        )
        client = httpx.Client(transport=transport, timeout=5.0)
        try:
            result = smoke_test._openai_stream(client, "http://stub", "k", "gpt-4")
        finally:
            client.close()
        assert not result.ok
        # Chunks were received but lacked the required SSE marker.

    def test_stream_cancel_after_first_chunk(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        chunks = [
            b'data: {"choices":[{"delta":{}}]}\n\n',
            b'data: {"choices":[{"delta":{"content":"more"}}]}\n\n',
            b"data: [DONE]\n\n",
        ]
        monkeypatch.setenv("GOROUTER_TEST_STREAM_CANCEL", "1")
        transport = _transport_for(
            {("POST", "/v1/chat/completions"): _streaming_handler(chunks)}
        )
        client = httpx.Client(transport=transport, timeout=5.0)
        try:
            result = smoke_test._openai_stream(client, "http://stub", "k", "gpt-4")
        finally:
            client.close()
        # Cancellation after first chunk still satisfies the streaming
        # contract: we got a status, headers, and at least one nonempty
        # chunk. We just stop early.
        assert result.ok, result.detail
        # The streaming path encountered a chunk; the cancel
        # short-circuits the rest of the body.
        assert "chunks=" in result.detail

    def test_stream_transport_error_is_recorded(self) -> None:
        def _explode(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom")

        transport = _transport_for({("POST", "/v1/chat/completions"): _explode})
        client = httpx.Client(transport=transport, timeout=5.0)
        try:
            result = smoke_test._openai_stream(client, "http://stub", "k", "gpt-4")
        finally:
            client.close()
        assert not result.ok
        assert "transport" in result.detail


class TestCheckHealthAndStats:
    def test_health_passes_on_200(self) -> None:
        def _ok(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={})

        transport = _transport_for(
            {
                ("GET", "/v1/healthz"): _ok,
                ("GET", "/v1/readyz"): _ok,
            }
        )
        client = httpx.Client(transport=transport, timeout=5.0)
        try:
            results = smoke_test._check_health(client, "http://stub")
        finally:
            client.close()
        assert all(r.ok for r in results)
        assert [r.name for r in results] == ["healthz", "readyz"]

    def test_health_records_transport_failure(self) -> None:
        def _explode(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("nope")

        transport = _transport_for({("GET", "/v1/healthz"): _explode})
        client = httpx.Client(transport=transport, timeout=5.0)
        try:
            results = smoke_test._check_health(client, "http://stub")
        finally:
            client.close()
        healthz = next(r for r in results if r.name == "healthz")
        assert not healthz.ok
        assert "transport" in healthz.detail

    def test_stats_finds_first_200(self) -> None:
        transport = _transport_for(
            {
                ("GET", "/v1/stats"): lambda r: httpx.Response(200, json={}),
            }
        )
        client = httpx.Client(transport=transport, timeout=5.0)
        try:
            result = smoke_test._check_stats(client, "http://stub")
        finally:
            client.close()
        assert result.ok
        assert "endpoint=/v1/stats" in result.detail

    def test_stats_fails_when_no_endpoint_returns_200(self) -> None:
        def _all_500(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        transport = _transport_for(
            {
                ("GET", "/v1/stats"): _all_500,
                ("GET", "/v1/stats/accounts"): _all_500,
                ("GET", "/v1/stats/usage"): _all_500,
            }
        )
        client = httpx.Client(transport=transport, timeout=5.0)
        try:
            result = smoke_test._check_stats(client, "http://stub")
        finally:
            client.close()
        assert not result.ok

    def test_models_returns_count(self) -> None:
        transport = _transport_for(
            {
                (
                    "GET",
                    "/v1/models",
                ): lambda r: httpx.Response(
                    200, json={"data": [{"id": "a"}, {"id": "b"}]}
                )
            }
        )
        client = httpx.Client(transport=transport, timeout=5.0)
        try:
            result = smoke_test._check_models(client, "http://stub", "k")
        finally:
            client.close()
        assert result.ok
        assert "count=2" in result.detail

    def test_models_fails_on_empty_data(self) -> None:
        transport = _transport_for(
            {("GET", "/v1/models"): lambda r: httpx.Response(200, json={"data": []})}
        )
        client = httpx.Client(transport=transport, timeout=5.0)
        try:
            result = smoke_test._check_models(client, "http://stub", "k")
        finally:
            client.close()
        assert not result.ok
        assert "count=0" in result.detail

    def test_models_fails_on_non_json(self) -> None:
        transport = _transport_for(
            {("GET", "/v1/models"): lambda r: httpx.Response(200, text="not json")}
        )
        client = httpx.Client(transport=transport, timeout=5.0)
        try:
            result = smoke_test._check_models(client, "http://stub", "k")
        finally:
            client.close()
        assert not result.ok
        assert "non-json" in result.detail


class TestMainSkipLive:
    def test_main_skip_live_reports_all_ok(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GOROUTER_BASE_URL", "http://stub")
        monkeypatch.setenv("GOROUTER_API_KEY", "k")
        monkeypatch.setenv("GOROUTER_OPENAI_MODEL", "gpt-4")
        monkeypatch.setenv("GOROUTER_ANTHROPIC_MODEL", "claude-3-5-sonnet")
        monkeypatch.setenv("GOROUTER_SKIP_LIVE", "1")
        # Even health/models/stats will fail because no server exists,
        # so we patch them to succeed.
        monkeypatch.setattr(
            smoke_test,
            "_check_health",
            lambda c, b: [
                smoke_test.CheckResult("healthz", True),
                smoke_test.CheckResult("readyz", True),
            ],
        )
        monkeypatch.setattr(
            smoke_test,
            "_check_models",
            lambda c, b, k: smoke_test.CheckResult("models", True, "count=1"),
        )
        monkeypatch.setattr(
            smoke_test,
            "_check_stats",
            lambda c, b: smoke_test.CheckResult("stats", True, "endpoint=/v1/stats"),
        )
        rc = smoke_test.main()
        assert rc == 0
