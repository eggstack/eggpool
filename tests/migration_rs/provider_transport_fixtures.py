"""Small deterministic provider-transport fixtures for the Rust migration.

The fixtures intentionally record structure rather than request data.  They
are suitable for paired Python/Rust observations and keep all listeners,
threads, sockets, and waits bounded by context managers.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import http.server
import select
import socket
import socketserver
import ssl
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit

import httpx

if TYPE_CHECKING:
    from types import TracebackType


@dataclass(frozen=True)
class TransportResponse:
    """An HTTP response with bounded timing and framing controls."""

    status: int = 200
    body: bytes = b"ok"
    headers: tuple[tuple[str, str], ...] = (("content-type", "text/plain"),)
    delay_before_headers_s: float = 0.0
    chunks: tuple[bytes, ...] = ()
    delay_between_chunks_s: float = 0.0
    close_without_response: bool = False
    malformed_bytes: bytes | None = None


@dataclass(frozen=True)
class UpstreamRequestObservation:
    """Sanitized request facts captured by the HTTP upstream."""

    method: str
    path: str
    header_names: tuple[str, ...]
    body_length: int
    connection_id: int


class _RecordingHTTPServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], owner: RecordingHTTPServer) -> None:
        super().__init__(address, _RecordingHandler)
        self.owner = owner

    def get_request(self) -> tuple[socket.socket, tuple[str, int]]:
        request, address = super().get_request()
        return request, address


class _TLSRecordingHTTPServer(_RecordingHTTPServer):
    def __init__(
        self,
        address: tuple[str, int],
        owner: RecordingHTTPServer,
        certificate: str,
        private_key: str,
    ) -> None:
        self._certificate = certificate
        self._private_key = private_key
        super().__init__(address, owner)

    def get_request(self) -> tuple[socket.socket, tuple[str, int]]:
        request, address = super().get_request()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self._certificate, self._private_key)
        try:
            return context.wrap_socket(request, server_side=True), address
        except BaseException:
            request.close()
            raise


class RecordingHTTPServer:
    """Threaded HTTP/1.1 upstream with request and connection accounting."""

    def __init__(
        self,
        routes: dict[
            tuple[str, str],
            TransportResponse,
        ],
        *,
        tls_certificate: str | None = None,
        tls_private_key: str | None = None,
    ) -> None:
        if (tls_certificate is None) != (tls_private_key is None):
            raise ValueError("TLS requires both certificate and private key")
        self.routes = {
            (method.upper(), path): response
            for (method, path), response in routes.items()
        }
        self.requests: list[UpstreamRequestObservation] = []
        self.connections_opened = 0
        self._next_connection_id = 0
        self._lock = threading.Lock()
        server_type = (
            _TLSRecordingHTTPServer
            if tls_certificate is not None and tls_private_key is not None
            else _RecordingHTTPServer
        )
        if server_type is _TLSRecordingHTTPServer:
            self._server = server_type(
                ("127.0.0.1", 0),
                self,
                tls_certificate or "",
                tls_private_key or "",
            )
        else:
            self._server = server_type(("127.0.0.1", 0), self)
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def scheme(self) -> str:
        return "https" if isinstance(self._server, _TLSRecordingHTTPServer) else "http"

    @property
    def base_url(self) -> str:
        return f"{self.scheme}://localhost:{self.port}"

    def _connection_started(self) -> int:
        with self._lock:
            self.connections_opened += 1
            self._next_connection_id += 1
            return self._next_connection_id

    def _record_request(self, observation: UpstreamRequestObservation) -> None:
        with self._lock:
            self.requests.append(observation)

    def __enter__(self) -> RecordingHTTPServer:
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="migration-transport-upstream",
            daemon=True,
        )
        self._thread.start()
        return self

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


class _RecordingHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        super().setup()
        self.connection_id = self.server.owner._connection_started()  # type: ignore[attr-defined]

    def do_GET(self) -> None:  # noqa: N802
        self._handle_request()

    def do_POST(self) -> None:  # noqa: N802
        self._handle_request()

    def do_PUT(self) -> None:  # noqa: N802
        self._handle_request()

    def do_PATCH(self) -> None:  # noqa: N802
        self._handle_request()

    def log_message(self, message_format: str, *args: object) -> None:
        del message_format, args

    def _handle_request(self) -> None:
        length = int(self.headers.get("content-length", "0"))
        if length:
            self.rfile.read(length)
        path = urlsplit(self.path).path or "/"
        observation = UpstreamRequestObservation(
            method=self.command,
            path=path,
            header_names=tuple(sorted(name.casefold() for name in self.headers)),
            body_length=length,
            connection_id=self.connection_id,
        )
        self.server.owner._record_request(observation)  # type: ignore[attr-defined]
        response = self.server.owner.routes.get(  # type: ignore[attr-defined]
            (self.command, path), TransportResponse(status=404, body=b"not found")
        )
        if response.delay_before_headers_s:
            time.sleep(response.delay_before_headers_s)
        if response.close_without_response:
            self.close_connection = True
            return
        if response.malformed_bytes is not None:
            self.connection.sendall(response.malformed_bytes)
            self.close_connection = True
            return
        self.send_response(response.status)
        for name, value in response.headers:
            self.send_header(name, value)
        chunks = response.chunks or (response.body,)
        if response.chunks:
            self.send_header("transfer-encoding", "chunked")
        else:
            self.send_header("content-length", str(len(response.body)))
        self.end_headers()
        if response.chunks:
            for chunk in chunks:
                self.wfile.write(f"{len(chunk):x}\r\n".encode())
                self.wfile.write(chunk + b"\r\n")
                self.wfile.flush()
                if response.delay_between_chunks_s:
                    time.sleep(response.delay_between_chunks_s)
            self.wfile.write(b"0\r\n\r\n")
        else:
            self.wfile.write(response.body)
        self.wfile.flush()


@dataclass(frozen=True)
class ProxyObservation:
    """Proxy-side facts that do not retain credentials or request bodies."""

    protocol: str
    target_host: str | None
    target_port: int | None
    target_address_kind: str | None
    header_names: tuple[str, ...]
    authenticated: bool


def _relay(left: socket.socket, right: socket.socket) -> None:
    left.settimeout(5.0)
    right.settimeout(5.0)
    sockets = (left, right)
    while True:
        readable, _, _ = select.select(sockets, (), (), 5.0)
        if not readable:
            return
        for source in readable:
            data = source.recv(65536)
            if not data:
                return
            destination = right if source is left else left
            destination.sendall(data)


class _ThreadingTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class HTTPConnectProxy:
    """Minimal HTTP CONNECT proxy with optional Basic authentication."""

    def __init__(
        self, *, username: str | None = None, password: str | None = None
    ) -> None:
        if (username is None) != (password is None):
            raise ValueError("HTTP proxy auth requires both username and password")
        self.username = username
        self.password = password
        self.observations: list[ProxyObservation] = []
        self._lock = threading.Lock()
        self._server = _ThreadingTCPServer(("127.0.0.1", 0), _HTTPConnectHandler)
        self._server.owner = self  # type: ignore[attr-defined]
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def uri(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def record(self, observation: ProxyObservation) -> None:
        with self._lock:
            self.observations.append(observation)

    def __enter__(self) -> HTTPConnectProxy:
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


class _HTTPConnectHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        request = self.request
        data = bytearray()
        while b"\r\n\r\n" not in data and len(data) <= 16_384:
            chunk = request.recv(4096)
            if not chunk:
                return
            data.extend(chunk)
        lines = data.split(b"\r\n")
        request_line = lines[0].decode("latin-1").split(" ")
        if len(request_line) != 3 or request_line[0].upper() != "CONNECT":
            request.sendall(
                b"HTTP/1.1 405 Method Not Allowed\r\nConnection: close\r\n\r\n"
            )
            return
        host, separator, raw_port = request_line[1].partition(":")
        if not separator:
            return
        try:
            port = int(raw_port)
        except ValueError:
            return
        headers = {
            name.casefold(): value.strip()
            for name, _, value in (
                line.decode("latin-1").partition(":") for line in lines[1:]
            )
            if name and _
        }
        owner: HTTPConnectProxy = self.server.owner  # type: ignore[attr-defined]
        expected = None
        if owner.username is not None and owner.password is not None:
            expected = (
                "Basic "
                + base64.b64encode(
                    f"{owner.username}:{owner.password}".encode()
                ).decode()
            )
        authenticated = (
            expected is None or headers.get("proxy-authorization") == expected
        )
        owner.record(
            ProxyObservation(
                protocol="http_connect",
                target_host=host,
                target_port=port,
                target_address_kind="authority",
                header_names=tuple(sorted(headers)),
                authenticated=authenticated,
            )
        )
        if not authenticated:
            request.sendall(
                b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                b"Proxy-Authenticate: Basic\r\nConnection: close\r\n\r\n"
            )
            return
        try:
            upstream = socket.create_connection((host, port), timeout=2.0)
        except OSError:
            request.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
            return
        with upstream:
            request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            with contextlib.suppress(OSError):
                _relay(request, upstream)


class SOCKS5Proxy:
    """Minimal SOCKS5 CONNECT proxy with optional username/password auth."""

    def __init__(
        self, *, username: str | None = None, password: str | None = None
    ) -> None:
        if (username is None) != (password is None):
            raise ValueError("SOCKS5 auth requires both username and password")
        self.username = username
        self.password = password
        self.observations: list[ProxyObservation] = []
        self._lock = threading.Lock()
        self._server = _ThreadingTCPServer(("127.0.0.1", 0), _SOCKS5Handler)
        self._server.owner = self  # type: ignore[attr-defined]
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def uri(self) -> str:
        return f"socks5://127.0.0.1:{self.port}"

    def record(self, observation: ProxyObservation) -> None:
        with self._lock:
            self.observations.append(observation)

    def __enter__(self) -> SOCKS5Proxy:
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def _read_exact(connection: socket.socket, length: int) -> bytes:
    data = bytearray()
    while len(data) < length:
        chunk = connection.recv(length - len(data))
        if not chunk:
            raise ConnectionError("fixture peer closed early")
        data.extend(chunk)
    return bytes(data)


class _SOCKS5Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        connection = self.request
        owner: SOCKS5Proxy = self.server.owner  # type: ignore[attr-defined]
        try:
            version, method_count = _read_exact(connection, 2)
            methods = _read_exact(connection, method_count)
            if version != 5:
                return
            needs_auth = owner.username is not None and owner.password is not None
            method = (
                2
                if needs_auth and 2 in methods
                else 0
                if not needs_auth and 0 in methods
                else 255
            )
            connection.sendall(bytes((5, method)))
            if method == 255:
                return
            authenticated = True
            if method == 2:
                auth_version, user_length = _read_exact(connection, 2)
                user = _read_exact(connection, user_length)
                password_length = _read_exact(connection, 1)[0]
                password = _read_exact(connection, password_length)
                authenticated = (
                    auth_version == 1
                    and user == (owner.username or "").encode()
                    and password == (owner.password or "").encode()
                )
                connection.sendall(bytes((1, 0 if authenticated else 1)))
                if not authenticated:
                    return
            request_header = _read_exact(connection, 4)
            if request_header[:3] != b"\x05\x01\x00":
                return
            address_kind = {1: "ipv4", 3: "domain", 4: "ipv6"}.get(request_header[3])
            if address_kind == "ipv4":
                host = socket.inet_ntoa(_read_exact(connection, 4))
            elif address_kind == "ipv6":
                host = socket.inet_ntop(socket.AF_INET6, _read_exact(connection, 16))
            elif address_kind == "domain":
                host = _read_exact(connection, _read_exact(connection, 1)[0]).decode()
            else:
                return
            port = int.from_bytes(_read_exact(connection, 2), "big")
            try:
                upstream = socket.create_connection((host, port), timeout=2.0)
            except OSError:
                connection.sendall(b"\x05\x05\x00\x01\x00\x00\x00\x00\x00\x00")
                return
            connection.sendall(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")
            owner.record(
                ProxyObservation(
                    protocol="socks5",
                    target_host=host,
                    target_port=port,
                    target_address_kind=address_kind,
                    header_names=(),
                    authenticated=authenticated,
                )
            )
            with upstream, contextlib.suppress(OSError):
                _relay(connection, upstream)
        except (ConnectionError, OSError, UnicodeError):
            return


@dataclass(frozen=True)
class TransportErrorObservation:
    """Stable transport failure facts independent of HTTPX class wording."""

    category: str
    stage: str
    network_path: str
    proxy_endpoint: str | None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "category": self.category,
            "stage": self.stage,
            "network_path": self.network_path,
            "proxy_endpoint": self.proxy_endpoint,
        }


def redact_proxy_uri(uri: str) -> str:
    """Retain only scheme and endpoint identity; drop userinfo/fragment."""
    try:
        parsed = urlsplit(uri)
    except ValueError:
        return "[REDACTED]"
    endpoint = parsed.netloc.rpartition("@")[2]
    if not endpoint:
        return "[REDACTED]"
    return urlunsplit((parsed.scheme, endpoint, parsed.path, parsed.query, ""))


def observe_transport_error(
    error: BaseException,
    *,
    stage: str,
    proxy_uri: str | None = None,
) -> TransportErrorObservation:
    """Normalize an HTTPX/asyncio failure without retaining its message."""
    class_categories = (
        (httpx.PoolTimeout, "pool_timeout"),
        (httpx.ConnectTimeout, "connect_timeout"),
        (httpx.WriteTimeout, "write_timeout"),
        (httpx.ReadTimeout, "read_timeout"),
        (httpx.ProxyError, "proxy_error"),
        (httpx.RemoteProtocolError, "protocol_error"),
        (httpx.LocalProtocolError, "protocol_error"),
        (httpx.ConnectError, "connect_error"),
        (httpx.WriteError, "write_error"),
        (httpx.ReadError, "read_error"),
    )
    category = next(
        (
            name
            for error_type, name in class_categories
            if isinstance(error, error_type)
        ),
        "cancelled"
        if isinstance(error, (asyncio.CancelledError,))
        else "transport_error",
    )
    return TransportErrorObservation(
        category=category,
        stage=stage,
        network_path="proxied" if proxy_uri is not None else "direct",
        proxy_endpoint=redact_proxy_uri(proxy_uri) if proxy_uri is not None else None,
    )


__all__ = [
    "HTTPConnectProxy",
    "ProxyObservation",
    "RecordingHTTPServer",
    "SOCKS5Proxy",
    "TransportErrorObservation",
    "TransportResponse",
    "UpstreamRequestObservation",
    "observe_transport_error",
    "redact_proxy_uri",
]
