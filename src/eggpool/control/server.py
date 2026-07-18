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
import stat
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from eggpool import jsonx

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

PROTOCOL_VERSION: Final = 1
CONTROL_SOCKET_NAME: Final = "eggpool.sock"
MAX_REQUEST_SIZE: Final = 65536
COMMAND_TIMEOUT_S: Final = 30.0


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

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-compatible dict."""
        return {
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


def control_socket_path() -> Path:
    """Return the deterministic socket path from ``runtime_paths.state_dir()``."""
    from eggpool.runtime_paths import state_dir

    return state_dir() / CONTROL_SOCKET_NAME


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
        _restrict_socket_permissions(self._path)
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
            payload: dict[str, Any] = jsonx.loads(raw_line)
        except Exception as exc:
            return _error_response(
                "",
                f"invalid JSON: {exc}",
                stage="parse",
            )

        proto = payload.get("protocol_version")
        if proto != PROTOCOL_VERSION:
            return _error_response(
                payload.get("request_id", ""),
                f"unsupported protocol version: {proto}",
                stage="parse",
            )

        request_id = str(payload.get("request_id", ""))
        command = str(payload.get("command", ""))
        if not command:
            return _error_response(
                request_id,
                "missing command",
                stage="parse",
            )

        request = ControlRequest(
            protocol_version=proto,
            request_id=request_id,
            command=command,
            validated_digest=payload.get("validated_digest"),
        )

        try:
            return await self._reload_handler(request)
        except Exception as exc:
            logger.exception("reload handler failed for request %s", request_id)
            return _error_response(
                request_id,
                f"handler error: {exc}",
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
    )


def _clean_stale_socket(path: Path) -> None:
    """Remove a stale socket file or symlink at *path*.

    The function handles three cases:
    * Regular socket file (``S_ISSOCK``) – removed.
    * Dangling symlink (target missing) – removed so ``bind()`` can
      reclaim the path.
    * Symlink to a socket file – removed to avoid following a stale
      reference.
    """
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
    """Set socket mode to 0o600 (owner-only)."""
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        logger.debug("set control socket permissions to 0o600")
    except OSError as exc:
        logger.warning(
            "could not restrict socket permissions on %s: %s",
            path,
            exc,
        )
