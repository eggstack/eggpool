"""HTTPX transport support for pproxy outbound connections."""

from __future__ import annotations

import asyncio
import contextlib
import select
import ssl
from collections.abc import AsyncIterable, AsyncIterator, Generator, Iterable
from typing import Any, Protocol, cast

import httpcore
import httpx
import pproxy

SOCKET_OPTION = httpcore.SOCKET_OPTION


class PProxyConnection(Protocol):
    """Typed subset of the untyped pproxy connection object we need."""

    async def tcp_connect(
        self,
        host: str,
        port: int,
        local_addr: str | None = None,
        lbind: str | None = None,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]: ...


class AsyncPProxyTransport(httpx.AsyncBaseTransport):
    """HTTPX transport that opens TCP streams through a pproxy URI."""

    def __init__(
        self,
        proxy_uri: str,
        *,
        limits: httpx.Limits,
        http1: bool = True,
        http2: bool = False,
        retries: int = 0,
        ssl_context: ssl.SSLContext | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> None:
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl_context,
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            http1=http1,
            http2=http2,
            retries=retries,
            network_backend=PProxyNetworkBackend(
                proxy_uri,
                socket_options=socket_options,
            ),
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        assert isinstance(request.stream, httpx.AsyncByteStream)
        req = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        with map_httpcore_exceptions():
            resp = await self._pool.handle_async_request(req)

        assert isinstance(resp.stream, AsyncIterable)
        typed_resp = cast("Any", resp)
        return httpx.Response(
            status_code=resp.status,
            headers=resp.headers,
            stream=AsyncPProxyResponseStream(resp.stream),
            extensions=cast("dict[str, Any]", typed_resp.extensions),
        )

    async def aclose(self) -> None:
        with map_httpcore_exceptions():
            await self._pool.aclose()


class AsyncPProxyResponseStream(httpx.AsyncByteStream):
    """Map httpcore response streams onto HTTPX response streams."""

    def __init__(self, stream: AsyncIterable[bytes]) -> None:
        self._stream = stream

    async def __aiter__(self) -> AsyncIterator[bytes]:
        with map_httpcore_exceptions():
            async for part in self._stream:
                yield part

    async def aclose(self) -> None:
        aclose = getattr(self._stream, "aclose", None)
        if aclose is not None:
            await aclose()


class PProxyNetworkBackend(httpcore.AsyncNetworkBackend):
    """httpcore network backend that delegates TCP connects to pproxy."""

    def __init__(
        self,
        proxy_uri: str,
        *,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> None:
        self._proxy_uri = proxy_uri
        pproxy_connection = cast("Any", pproxy).Connection
        self._proxy = cast("PProxyConnection", pproxy_connection(proxy_uri))
        self._socket_options = tuple(socket_options or ())

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        options = tuple(socket_options or self._socket_options)
        writer: asyncio.StreamWriter | None = None
        try:
            connect = self._proxy.tcp_connect(host, port, local_addr=local_address)
            reader, writer = await asyncio.wait_for(connect, timeout=timeout)
            _apply_socket_options(writer, options)
        except TimeoutError as exc:
            if writer is not None:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
            raise httpcore.ConnectTimeout(exc) from exc
        except OSError as exc:
            if writer is not None:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
            raise httpcore.ConnectError(exc) from exc
        except Exception as exc:
            if writer is not None:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
            raise httpcore.ConnectError(
                f"pproxy connection via {self._proxy_uri!r} failed: {exc}"
            ) from exc
        return PProxyNetworkStream(reader, writer)

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError("pproxy transport does not support UDS upstreams")

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class PProxyNetworkStream(httpcore.AsyncNetworkStream):
    """httpcore stream wrapper around asyncio streams returned by pproxy."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self._reader = reader
        self._writer = writer

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        try:
            return await asyncio.wait_for(
                self._reader.read(max_bytes),
                timeout=timeout,
            )
        except TimeoutError as exc:
            raise httpcore.ReadTimeout(exc) from exc
        except (OSError, ssl.SSLError) as exc:
            raise httpcore.ReadError(exc) from exc

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        if not buffer:
            return
        try:
            self._writer.write(buffer)
            await asyncio.wait_for(self._writer.drain(), timeout=timeout)
        except TimeoutError as exc:
            raise httpcore.WriteTimeout(exc) from exc
        except (OSError, ssl.SSLError) as exc:
            raise httpcore.WriteError(exc) from exc

    async def aclose(self) -> None:
        self._writer.close()
        with contextlib.suppress(Exception):
            await self._writer.wait_closed()

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.AsyncNetworkStream:
        try:
            await self._writer.start_tls(
                ssl_context,
                server_hostname=server_hostname,
                ssl_handshake_timeout=timeout,
            )
        except asyncio.CancelledError:
            await self.aclose()
            raise
        except TimeoutError as exc:
            await self.aclose()
            raise httpcore.ConnectTimeout(exc) from exc
        except (OSError, ssl.SSLError) as exc:
            await self.aclose()
            raise httpcore.ConnectError(exc) from exc
        return self

    def get_extra_info(self, info: str) -> Any:
        if info == "ssl_object":
            return self._writer.get_extra_info("ssl_object")
        if info == "client_addr":
            return self._writer.get_extra_info("sockname")
        if info == "server_addr":
            return self._writer.get_extra_info("peername")
        if info == "socket":
            return self._writer.get_extra_info("socket")
        if info == "is_readable":
            return _is_socket_readable(self._writer.get_extra_info("socket"))
        return None


@contextlib.contextmanager
def map_httpcore_exceptions() -> Generator[None]:
    """Map httpcore exceptions into the HTTPX exception hierarchy."""
    try:
        yield
    except Exception as exc:
        for from_exc, to_exc in HTTPCORE_EXC_MAP.items():
            if isinstance(exc, from_exc):
                raise to_exc(str(exc)) from exc
        raise


HTTPCORE_EXC_MAP: dict[type[Exception], type[httpx.HTTPError]] = {
    httpcore.ConnectTimeout: httpx.ConnectTimeout,
    httpcore.ReadTimeout: httpx.ReadTimeout,
    httpcore.WriteTimeout: httpx.WriteTimeout,
    httpcore.PoolTimeout: httpx.PoolTimeout,
    httpcore.TimeoutException: httpx.TimeoutException,
    httpcore.ConnectError: httpx.ConnectError,
    httpcore.ReadError: httpx.ReadError,
    httpcore.WriteError: httpx.WriteError,
    httpcore.NetworkError: httpx.NetworkError,
    httpcore.ProxyError: httpx.ProxyError,
    httpcore.LocalProtocolError: httpx.LocalProtocolError,
    httpcore.RemoteProtocolError: httpx.RemoteProtocolError,
    httpcore.ProtocolError: httpx.ProtocolError,
    httpcore.UnsupportedProtocol: httpx.UnsupportedProtocol,
}


def _apply_socket_options(
    writer: asyncio.StreamWriter,
    socket_options: Iterable[SOCKET_OPTION],
) -> None:
    sock = writer.get_extra_info("socket")
    if sock is None:
        return
    for option in socket_options:
        sock.setsockopt(*option)


def _is_socket_readable(sock: Any | None) -> bool:
    if sock is None or sock.fileno() < 0:
        return True
    if not hasattr(select, "poll"):
        ready, _, _ = select.select([sock], [], [], 0)
        return bool(ready)
    poller = select.poll()
    poller.register(sock.fileno(), select.POLLIN)
    return bool(poller.poll(0))
