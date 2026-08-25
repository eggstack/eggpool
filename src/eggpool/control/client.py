"""Control socket client for ``eggpool rehash``.

Sends a ``reload_config`` command over the Unix-domain socket and
returns the structured :class:`ControlResponse`.  Designed for
short-lived CLI connections: connect, send one request, read one
response, disconnect.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from typing import TYPE_CHECKING

from eggpool import jsonx
from eggpool.control.server import (
    COMMAND_TIMEOUT_S,
    PROTOCOL_VERSION,
    ControlRequest,
    ControlResponse,
    control_socket_path,
)

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


class ControlClientError(Exception):
    """Base class for control client failures."""


class ControlClientTimeoutError(ControlClientError):
    """The control socket request timed out."""


class ControlClientConnectionError(ControlClientError):
    """Could not connect to the control socket."""


class ControlClientProtocolError(ControlClientError):
    """The server returned an invalid or incompatible response."""


class ControlClient:
    """Async client for the EggPool control socket.

    Args:
        socket_path: Override for the socket path.  Defaults to
            :func:`control_socket_path`.
        timeout_s: Maximum seconds to wait for a response.
    """

    def __init__(
        self,
        *,
        socket_path: Path | None = None,
        timeout_s: float = COMMAND_TIMEOUT_S,
    ) -> None:
        self._socket_path = socket_path or control_socket_path()
        self._timeout_s = timeout_s

    async def reload(self, validated_digest: str) -> ControlResponse:
        """Send a ``reload_config`` command and return the response.

        Args:
            validated_digest: SHA-256 content digest of the validated
                config file.

        Raises:
            ControlClientConnectionError: Cannot connect to the socket.
            ControlClientTimeoutError: Response exceeded the timeout.
            ControlClientProtocolError: Server response was malformed.
        """
        request = ControlRequest(
            protocol_version=PROTOCOL_VERSION,
            request_id=uuid.uuid4().hex,
            command="reload_config",
            validated_digest=validated_digest,
        )
        return await self._send_and_receive(request)

    async def _send_and_receive(
        self,
        request: ControlRequest,
    ) -> ControlResponse:
        """Open a connection, send a request, and read the response."""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(self._socket_path)),
                timeout=self._timeout_s,
            )
        except TimeoutError as exc:
            raise ControlClientTimeoutError(
                f"connection to {self._socket_path} timed out"
            ) from exc
        except OSError as exc:
            raise ControlClientConnectionError(
                f"cannot connect to {self._socket_path}: {exc}"
            ) from exc

        try:
            payload = {
                "protocol_version": request.protocol_version,
                "request_id": request.request_id,
                "command": request.command,
                "validated_digest": request.validated_digest,
            }
            writer.write(jsonx.dumps_bytes(payload) + b"\n")
            await writer.drain()

            raw_line = await asyncio.wait_for(
                reader.readline(),
                timeout=self._timeout_s,
            )
        except TimeoutError as exc:
            raise ControlClientTimeoutError(
                "timed out waiting for server response"
            ) from exc
        except ValueError as exc:
            # asyncio raises a bare ValueError when the response line
            # exceeds the stream limit (no separator found); surface it
            # as the typed protocol error instead.
            raise ControlClientProtocolError(
                f"malformed response framing: {exc}"
            ) from exc
        except OSError as exc:
            raise ControlClientConnectionError(f"communication error: {exc}") from exc
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

        if not raw_line or raw_line.isspace():
            raise ControlClientProtocolError("empty response from server")

        return _parse_response(raw_line)


def _to_str_list(value: object) -> list[str]:
    """Coerce a JSON value to a list of strings."""
    if isinstance(value, list):
        raw: list[object] = value  # type: ignore[assignment]
        return [str(item) for item in raw]
    return []


def _parse_response(raw_line: bytes) -> ControlResponse:
    """Parse and validate a raw response line from the server."""
    try:
        payload: dict[str, object] = jsonx.loads(raw_line)
    except Exception as exc:
        raise ControlClientProtocolError(f"invalid JSON response: {exc}") from exc

    proto = payload.get("protocol_version")
    if proto != PROTOCOL_VERSION:
        raise ControlClientProtocolError(f"unsupported protocol version: {proto}")

    gen_raw = payload.get("generation")
    gen_val = int(gen_raw) if gen_raw is not None else None  # type: ignore[arg-type]

    try:
        return ControlResponse(
            protocol_version=int(proto),  # type: ignore[arg-type]
            request_id=str(payload.get("request_id", "")),
            ok=bool(payload.get("ok", False)),
            stage=str(payload.get("stage", "")),
            generation=gen_val,
            changed_sections=tuple(_to_str_list(payload.get("changed_sections"))),
            warnings=tuple(_to_str_list(payload.get("warnings"))),
            restart_required=tuple(_to_str_list(payload.get("restart_required"))),
            retirement_pending=bool(payload.get("retirement_pending", False)),
            message=str(payload.get("message", "")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ControlClientProtocolError(f"malformed response payload: {exc}") from exc
