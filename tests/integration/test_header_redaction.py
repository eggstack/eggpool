"""Tests for _HeaderRedactionMiddleware in eggpool.app."""

from __future__ import annotations

import pytest

from eggpool.app import _HeaderRedactionMiddleware


@pytest.mark.asyncio
class TestHeaderRedactionMiddleware:
    async def test_ignores_non_ascii_configuration_without_crashing(self) -> None:
        middleware = _HeaderRedactionMiddleware(
            app=lambda *_args: None, headers_to_redact=["x-sécret"]
        )
        assert middleware._redact == {b"x-s?cret"}  # pyright: ignore[reportPrivateUsage]

    async def test_strips_configured_headers(self) -> None:
        """Configured headers are removed from response."""

        async def _app(scope: object, receive: object, send: object) -> None:
            await send(  # type: ignore[union-attr]
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        [b"content-type", b"application/json"],
                        [b"x-request-id", b"abc-123"],
                        [b"x-secret", b"should-be-gone"],
                    ],
                }
            )

        middleware = _HeaderRedactionMiddleware(
            app=_app, headers_to_redact=["x-secret"]
        )
        sent: list[dict[str, object]] = []

        async def _send(message: dict[str, object]) -> None:
            sent.append(message)

        scope: dict[str, object] = {"type": "http"}
        await middleware(scope, None, _send)  # type: ignore[arg-type]

        assert len(sent) == 1
        headers = sent[0]["headers"]  # type: ignore[index]
        names = [h[0] for h in headers]
        assert b"x-secret" not in names
        assert b"content-type" in names
        assert b"x-request-id" in names

    async def test_passes_through_non_configured_headers(self) -> None:
        """Headers not in the redact list are kept unchanged."""

        async def _app(scope: object, receive: object, send: object) -> None:
            await send(  # type: ignore[union-attr]
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        [b"content-type", b"application/json"],
                        [b"cache-control", b"no-cache"],
                    ],
                }
            )

        middleware = _HeaderRedactionMiddleware(
            app=_app, headers_to_redact=["x-secret"]
        )
        sent: list[dict[str, object]] = []

        async def _send(message: dict[str, object]) -> None:
            sent.append(message)

        scope: dict[str, object] = {"type": "http"}
        await middleware(scope, None, _send)  # type: ignore[arg-type]

        headers = sent[0]["headers"]  # type: ignore[index]
        names = [h[0] for h in headers]
        assert b"content-type" in names
        assert b"cache-control" in names

    async def test_handles_non_http_scope(self) -> None:
        """Non-HTTP scopes are passed through without interception."""
        called = False

        async def _app(scope: object, receive: object, send: object) -> None:
            nonlocal called
            called = True

        middleware = _HeaderRedactionMiddleware(
            app=_app, headers_to_redact=["x-secret"]
        )
        await middleware({"type": "lifespan"}, None, None)  # type: ignore[arg-type]
        assert called

    async def test_streaming_body_chunks_pass_through(self) -> None:
        """Body chunks are forwarded unchanged by _filtered_send."""

        async def _app(scope: object, receive: object, send: object) -> None:
            await send(  # type: ignore[union-attr]
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [[b"x-secret", b"gone"]],
                }
            )
            await send({"type": "http.response.body", "body": b"chunk1"})  # type: ignore[union-attr]
            await send({"type": "http.response.body", "body": b"chunk2"})  # type: ignore[union-attr]

        middleware = _HeaderRedactionMiddleware(
            app=_app, headers_to_redact=["x-secret"]
        )
        sent: list[dict[str, object]] = []

        async def _send(message: dict[str, object]) -> None:
            sent.append(message)

        scope: dict[str, object] = {"type": "http"}
        await middleware(scope, None, _send)  # type: ignore[arg-type]

        assert len(sent) == 3
        # Headers redacted
        assert sent[0]["type"] == "http.response.start"
        assert sent[0]["headers"] == []  # type: ignore[index]
        # Body chunks untouched
        assert sent[1]["body"] == b"chunk1"  # type: ignore[index]
        assert sent[2]["body"] == b"chunk2"  # type: ignore[index]

    async def test_case_insensitive_redaction(self) -> None:
        """Header matching is case-insensitive."""

        async def _app(scope: object, receive: object, send: object) -> None:
            await send(  # type: ignore[union-attr]
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        [b"X-AUTH-TOKEN", b"secret"],
                        [b"x-auth-token", b"also-secret"],
                    ],
                }
            )

        middleware = _HeaderRedactionMiddleware(
            app=_app, headers_to_redact=["X-Auth-Token"]
        )
        sent: list[dict[str, object]] = []

        async def _send(message: dict[str, object]) -> None:
            sent.append(message)

        scope: dict[str, object] = {"type": "http"}
        await middleware(scope, None, _send)  # type: ignore[arg-type]

        headers = sent[0]["headers"]  # type: ignore[index]
        names = [h[0] for h in headers]
        assert b"X-AUTH-TOKEN" not in names
        assert b"x-auth-token" not in names

    async def test_empty_redact_list_keeps_all(self) -> None:
        """Empty redact list preserves all headers."""

        async def _app(scope: object, receive: object, send: object) -> None:
            await send(  # type: ignore[union-attr]
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        [b"x-secret", b"still-here"],
                    ],
                }
            )

        middleware = _HeaderRedactionMiddleware(app=_app, headers_to_redact=[])
        sent: list[dict[str, object]] = []

        async def _send(message: dict[str, object]) -> None:
            sent.append(message)

        scope: dict[str, object] = {"type": "http"}
        await middleware(scope, None, _send)  # type: ignore[arg-type]

        headers = sent[0]["headers"]  # type: ignore[index]
        assert len(headers) == 1
        assert headers[0][0] == b"x-secret"
