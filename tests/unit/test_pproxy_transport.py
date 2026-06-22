"""Tests for pproxy-backed HTTPX transport."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpcore
import httpx
import pytest

from eggpool.providers.pproxy_transport import (
    AsyncPProxyTransport,
    PProxyNetworkBackend,
)


@pytest.mark.asyncio
async def test_pproxy_transport_sends_http_request() -> None:
    requests: list[bytes] = []

    async def handle(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        requests.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Length: 13\r\n"
            b"Content-Type: text/plain\r\n"
            b"\r\n"
            b"through proxy"
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    try:
        server = await asyncio.start_server(handle, "127.0.0.1", 0)
    except PermissionError:
        pytest.skip("runner does not permit binding a loopback test server")
    try:
        socket = server.sockets[0]
        host, port = socket.getsockname()[:2]
        transport = AsyncPProxyTransport(
            "direct://",
            limits=httpx.Limits(
                max_connections=10,
                max_keepalive_connections=5,
            ),
        )
        async with httpx.AsyncClient(transport=transport) as client:
            response = await client.get(f"http://{host}:{port}/models")
    finally:
        server.close()
        await server.wait_closed()

    assert response.status_code == 200
    assert response.text == "through proxy"
    assert requests
    assert requests[0].startswith(b"GET /models HTTP/1.1\r\n")


@pytest.mark.asyncio
async def test_connect_closes_stream_when_socket_options_fail() -> None:
    backend = PProxyNetworkBackend("direct://")
    reader = asyncio.StreamReader()
    writer = MagicMock(spec=asyncio.StreamWriter)
    writer.wait_closed = AsyncMock()
    sock = MagicMock()
    sock.setsockopt.side_effect = OSError("invalid socket option")
    writer.get_extra_info.return_value = sock
    proxy = MagicMock()
    proxy.tcp_connect = AsyncMock(return_value=(reader, writer))
    backend._proxy = proxy

    with pytest.raises(httpcore.ConnectError, match="invalid socket option"):
        await backend.connect_tcp(
            "example.com",
            443,
            socket_options=[(1, 2, 3)],
        )

    writer.close.assert_called_once_with()
    writer.wait_closed.assert_awaited_once_with()
