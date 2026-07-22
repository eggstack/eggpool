"""Unix-domain socket control server for live config rehash.

Exposes a single-shot newline-delimited JSON protocol on a UDS for
the ``eggpool rehash`` CLI command.  The server is designed for
short-lived connections: one request per connection, structured
response, then close.

Protocol version 1 wire format
------------------------------

Request (one JSON object per line)::

    {
      "protocol_version": 1,
      "request_id": "<uuid>",
      "command": "reload_config",
      "validated_digest": "<sha-256>"
    }

Response (one JSON object per line)::

    {
      "protocol_version": 1,
      "request_id": "<uuid>",
      "ok": true,
      "stage": "commit",
      "generation": 3,
      "changed_sections": ["routing", "accounts"],
      "warnings": [],
      "restart_required": [],
      "retirement_pending": false,
      "message": "rehash applied"
    }

Security model
--------------

The socket is created with mode ``0o600`` (owner-only read/write)
so unprivileged clients on a shared host cannot issue reload commands.
The socket is always cleaned up on server stop and at startup if stale.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import stat
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, cast

from eggpool import jsonx

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

PROTOCOL_VERSION: Final = 1
CONTROL_SOCKET_NAME: Final = "eggpool.sock"
MAX_REQUEST_SIZE: Final = 65536
COMMAND_TIMEOUT_S: Final = 30.0
MAX_REQUEST_ID_LEN: Final = 256
_VALID_COMMANDS: Final = frozenset({"reload_config"})
_HEX64_RE: Final = re.compile(r"^[0-9a-f]{64}$")


class ControlProtocolError(Exception):
    """A protocol-level error in the control socket."""


class ControlServerError(Exception):
    """An operational error in the control socket server."""


@dataclass(frozen=True)
class ControlRequest:
    """Parsed inbound control request."""

    protocol_version: int
    request_id: str
    command: str
    validated_digest: str | None = None


@dataclass(frozen=True)
class ControlResponse:
    """Structured outbound control response."""

    protocol_version: int
    request_id: str
    ok: bool
    stage: str
    generation: int | None
    changed_sections: tuple[str, ...]
    warnings: tuple[str, ...]
    restart_required: tuple[str, ...]
    retirement_pending: bool
    message: str
    # Phase 11: optional diagnostic fields (backward-compatible).
    result_category: str | None = None
    duration_s: float | None = None
    retiring_generation_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-compatible dict."""
        d: dict[str, Any] = {
            "protocol_version": self.protocol_version,
            "request_id": self.request_id,
            "ok": self.ok,
            "stage": self.stage,
            "generation": self.generation,
            "changed_sections": list(self.changed_sections),
            "warnings": list(self.warnings),
            "restart_required": list(self.restart_required),
            "retirement_pending": self.retirement_pending,
            "message": self.message,
        }
        # Phase 11: include optional diagnostic fields.
        if self.result_category is not None:
            d["result_category"] = self.result_category
        if self.duration_s is not None:
            d["duration_s"] = self.duration_s
        if self.retiring_generation_id is not None:
            d["retiring_generation_id"] = self.retiring_generation_id
        return d


def control_socket_path() -> Path:
    """Return the deterministic socket path under ``runtime_paths.runtime_dir()``."""
    from eggpool.runtime_paths import runtime_dir

    return runtime_dir() / CONTROL_SOCKET_NAME


ReloadHandler = Callable[[ControlRequest], Coroutine[Any, Any, ControlResponse]]


class ControlServer:
    """Async Unix-domain socket control server.

    Args:
        reload_handler: Async callable that receives a
            :class:`ControlRequest` and returns a
            :class:`ControlResponse`.
        path: Optional socket path override.  Defaults to
            :func:`control_socket_path`.
    """

    def __init__(
        self,
        reload_handler: ReloadHandler,
        *,
        path: Path | None = None,
    ) -> None:
        self._reload_handler = reload_handler
        self._path = path or control_socket_path()
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        """Bind the socket and begin accepting connections."""
        _clean_stale_socket(self._path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._server = await asyncio.start_unix_server(
                self._handle_connection,
                str(self._path),
            )
        except OSError as exc:
            raise ControlServerError(
                f"failed to bind control socket {self._path}: {exc}"
            ) from exc
        try:
            _restrict_socket_permissions(self._path)
        except ControlServerError:
            # Tear down the server and unlink the socket on permission failure.
            self._server.close()
            await self._server.wait_closed()
            self._server = None
            _remove_socket_file(self._path)
            raise
        logger.info("control server listening on %s", self._path)

    async def stop(self) -> None:
        """Shut down the server and remove the socket file."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        _clean_stale_socket(self._path)
        logger.info("control server stopped")

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle a single short-lived connection."""
        peer = writer.get_extra_info("peername", "?")
        # SO_PEERCRED: reject connections from a different UID where supported.
        _reject_unmatched_peer_uid(writer)
        try:
            raw_line = await asyncio.wait_for(
                reader.readline(),
                timeout=COMMAND_TIMEOUT_S,
            )
            response = await self._process_request(raw_line)
            resp_bytes = jsonx.dumps_bytes(response.to_dict()) + b"\n"
            writer.write(resp_bytes)
            await writer.drain()
        except TimeoutError:
            logger.warning("control request timed out from %s", peer)
            err_resp = _error_response(
                "",
                "request timed out",
                stage="timeout",
            )
            writer.write(jsonx.dumps_bytes(err_resp.to_dict()) + b"\n")
            with contextlib.suppress(Exception):
                await writer.drain()
        except Exception:
            logger.exception("unhandled error on control connection from %s", peer)
            err_resp = _error_response(
                "",
                "internal server error",
                stage="error",
            )
            writer.write(jsonx.dumps_bytes(err_resp.to_dict()) + b"\n")
            with contextlib.suppress(Exception):
                await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _process_request(self, raw_line: bytes) -> ControlResponse:
        """Parse, validate, and dispatch a single request line."""
        if not raw_line or raw_line.isspace():
            return _error_response("", "empty request", stage="parse")

        if len(raw_line) > MAX_REQUEST_SIZE:
            return _error_response(
                "",
                f"request exceeds {MAX_REQUEST_SIZE} byte limit",
                stage="parse",
            )

        try:
            raw_payload: Any = jsonx.loads(raw_line)
        except Exception:
            return _error_response(
                "",
                "invalid JSON",
                stage="parse",
            )

        if not isinstance(raw_payload, dict):
            return _error_response(
                "",
                "request must be a JSON object",
                stage="parse",
            )

        payload: dict[str, Any] = cast("dict[str, Any]", raw_payload)
        proto = payload.get("protocol_version")
        if proto != PROTOCOL_VERSION:
            return _error_response(
                str(payload.get("request_id", "")),
                f"unsupported protocol version: {proto}",
                stage="parse",
            )

        request_id = payload.get("request_id", "")
        if not isinstance(request_id, str) or not request_id:
            return _error_response(
                "",
                "missing or invalid request_id",
                stage="parse",
            )
        if len(request_id) > MAX_REQUEST_ID_LEN:
            return _error_response(
                request_id,
                "request_id exceeds maximum length",
                stage="parse",
            )

        command = payload.get("command", "")
        if not isinstance(command, str) or not command:
            return _error_response(
                request_id,
                "missing command",
                stage="parse",
            )
        if command not in _VALID_COMMANDS:
            return _error_response(
                request_id,
                f"unknown command: {command}",
                stage="parse",
            )

        validated_digest = payload.get("validated_digest")
        if validated_digest is not None and (
            not isinstance(validated_digest, str)
            or not _HEX64_RE.fullmatch(validated_digest)
        ):
            return _error_response(
                request_id,
                "invalid validated_digest: must be exactly 64 hex characters",
                stage="parse",
            )

        request = ControlRequest(
            protocol_version=proto,
            request_id=request_id,
            command=command,
            validated_digest=(
                validated_digest if isinstance(validated_digest, str) else None
            ),
        )

        try:
            return await self._reload_handler(request)
        except Exception:
            logger.exception("reload handler failed for request %s", request_id)
            return _error_response(
                request_id,
                "handler error",
                stage="handler",
            )


def _error_response(
    request_id: str,
    message: str,
    *,
    stage: str = "error",
) -> ControlResponse:
    """Build a standardised error response."""
    return ControlResponse(
        protocol_version=PROTOCOL_VERSION,
        request_id=request_id,
        ok=False,
        stage=stage,
        generation=None,
        changed_sections=(),
        warnings=(),
        restart_required=(),
        retirement_pending=False,
        message=message,
        result_category="internal_error",
    )


def _remove_socket_file(path: Path) -> None:
    """Unconditionally remove a socket file. Never raises."""
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


def _clean_stale_socket(path: Path) -> None:
    """Remove a stale socket file or symlink at *path*.

    Before removing an existing socket, probes it with a quick connect
    to avoid unlinking a socket owned by a live server:

    * Connection succeeds → server is live, raise ``ControlServerError``.
    * Connection refused → stale socket, safe to remove.
    * Other connection error → likely stale, remove.
    """
    import socket as _socket

    if not path.exists() and not path.is_symlink():
        return

    # Probe: is a live server listening on this socket?
    try:
        probe = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        try:
            probe.settimeout(1.0)
            probe.connect(str(path))
            # Connection succeeded — a live server is listening.
            raise ControlServerError(f"control socket already in use: {path}")
        except OSError:
            # Connection refused or other socket error — stale.
            pass
        finally:
            probe.close()
    except ControlServerError:
        raise
    except OSError as exc:
        logger.warning("could not probe socket %s: %s", path, exc)

    # Remove the stale socket or symlink.
    try:
        if path.is_symlink():
            path.unlink()
            logger.info("removed stale symlink %s", path)
        elif path.exists() and stat.S_ISSOCK(path.stat().st_mode):
            path.unlink()
            logger.info("removed stale control socket %s", path)
    except OSError as exc:
        logger.warning("could not clean stale socket %s: %s", path, exc)


def _restrict_socket_permissions(path: Path) -> None:
    """Set socket mode to 0o600 (owner-only).

    Raises:
        ControlServerError: If chmod fails or mode is wrong after setting.
    """
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError as exc:
        raise ControlServerError(
            f"failed to restrict socket permissions on {path}: {exc}"
        ) from exc

    try:
        actual = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        raise ControlServerError(
            f"failed to verify socket permissions on {path}: {exc}"
        ) from exc

    if actual != (stat.S_IRUSR | stat.S_IWUSR):
        raise ControlServerError(
            f"socket {path} has mode {oct(actual)}, expected 0o600"
        )


def _reject_unmatched_peer_uid(writer: asyncio.StreamWriter) -> None:
    """Reject connections from a different UID using SO_PEERCRED where available.

    On Linux, ``SO_PEERCRED`` provides the peer's UID at connection time.
    If the peer UID does not match the server UID the connection is
    closed immediately.  On platforms where ``SO_PEERCRED`` is unavailable
    (e.g. macOS), the check is silently skipped — the socket's ``0o600``
    permissions already restrict access to the file owner.
    """
    import socket as _socket
    import struct as _struct

    sock: _socket.socket | None = writer.get_extra_info("socket")
    if sock is None:
        return

    peercred_attr = getattr(_socket, "SO_PEERCRED", None)
    if peercred_attr is None:
        return

    try:
        creds = sock.getsockopt(_socket.SOL_SOCKET, peercred_attr)
        # struct ucred: pid_t pid, uid_t uid, gid_t gid
        # uid is at offset 4 (after pid)
        creds_bytes = creds if isinstance(creds, bytes) else b""
        _pid, uid, _gid = _struct.unpack("iii", creds_bytes)
    except (OSError, ValueError, _struct.error):
        return

    server_uid = os.getuid()
    if uid != server_uid:
        logger.warning(
            "Rejecting control connection from UID %d (server UID %d)",
            uid,
            server_uid,
        )
        writer.close()
