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
import errno
import logging
import os
import re
import socket as _socket
import stat
import struct as _struct
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, cast

from eggpool import jsonx

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

PROTOCOL_VERSION: Final = 1
CONTROL_SOCKET_NAME: Final = "eggpool.sock"
# Linux and macOS reject Unix-domain socket paths above roughly 108 bytes;
# leave a small margin for platform-specific handling.
CONTROL_SOCKET_PATH_MAX_BYTES: Final = 103
MAX_REQUEST_SIZE: Final = 65536
COMMAND_TIMEOUT_S: Final = 30.0
MAX_REQUEST_ID_LEN: Final = 256
_VALID_COMMANDS: Final = frozenset({"reload_config"})
_HEX64_RE: Final = re.compile(r"^[0-9a-f]{64}$")


class ControlProtocolError(Exception):
    """A protocol-level error in the control socket."""


class ControlServerError(Exception):
    """An operational error in the control socket server."""


class _OversizedRequestError(Exception):
    """Internal marker: the request line exceeded ``MAX_REQUEST_SIZE``."""


class _TrailingRequestDataError(Exception):
    """Internal marker: more than one frame arrived on a connection."""


@dataclass(frozen=True)
class ControlRequest:
    """Parsed inbound control request."""

    protocol_version: int
    request_id: str
    command: str
    validated_digest: str | None = None
    params: dict[str, Any] | None = None


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
    # Plan 020 Workstream D3: canonical finalization fields in control response.
    finalization_status: str | None = None
    finalization_next_step: str | None = None
    finalization_attempt_count: int | None = None
    finalization_failure_count: int | None = None
    finalization_retry_attempt_count: int | None = None
    finalization_last_error_step: str | None = None
    finalization_last_error_class: str | None = None
    finalization_last_error_message: str | None = None

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
        # Plan 020 Workstream D3: canonical finalization fields.
        if self.finalization_status is not None:
            d["finalization_status"] = self.finalization_status
        if self.finalization_next_step is not None:
            d["finalization_next_step"] = self.finalization_next_step
        if self.finalization_attempt_count is not None:
            d["finalization_attempt_count"] = self.finalization_attempt_count
        if self.finalization_failure_count is not None:
            d["finalization_failure_count"] = self.finalization_failure_count
        if self.finalization_retry_attempt_count is not None:
            d["finalization_retry_attempt_count"] = (
                self.finalization_retry_attempt_count
            )
        if self.finalization_last_error_step is not None:
            d["finalization_last_error_step"] = self.finalization_last_error_step
        if self.finalization_last_error_class is not None:
            d["finalization_last_error_class"] = self.finalization_last_error_class
        if self.finalization_last_error_message is not None:
            d["finalization_last_error_message"] = self.finalization_last_error_message
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
        self._socket_identity: tuple[int, int, int] | None = None

    async def start(self) -> None:
        """Bind the socket and begin accepting connections."""
        path_bytes = os.fsencode(str(self._path))
        if len(path_bytes) > CONTROL_SOCKET_PATH_MAX_BYTES:
            raise ControlServerError(
                "control socket path is too long "
                f"({len(path_bytes)} bytes; max {CONTROL_SOCKET_PATH_MAX_BYTES}): "
                f"{self._path}. Set EGGPOOL_RUNTIME_DIR to a shorter directory."
            )
        _ensure_runtime_dir(self._path.parent)
        _clean_stale_socket(self._path)
        try:
            self._server = await asyncio.start_unix_server(
                self._handle_connection,
                str(self._path),
                limit=MAX_REQUEST_SIZE + 1,
            )
        except OSError as exc:
            raise ControlServerError(
                f"failed to bind control socket {self._path}: {exc}"
            ) from exc
        self._socket_identity = _stat_socket_identity(self._path)
        try:
            _restrict_socket_permissions(self._path)
        except ControlServerError:
            # Tear down the server and unlink the socket on permission failure.
            self._server.close()
            await self._server.wait_closed()
            self._server = None
            _remove_socket_file(self._path, expected_identity=self._socket_identity)
            self._socket_identity = None
            raise
        logger.info("control server listening on %s", self._path)

    async def stop(self) -> None:
        """Shut down the server and remove the socket file."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        _remove_socket_file(self._path, expected_identity=self._socket_identity)
        self._socket_identity = None
        logger.info("control server stopped")

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle a single short-lived connection."""
        peer = writer.get_extra_info("peername", "?")
        # SO_PEERCRED: reject connections from a different UID where
        # supported.  Plan 016 Workstream G1: the helper fails closed
        # on missing sockets, insufficient peer-cred data, or
        # ``OSError``; when it raises we MUST NOT process the request
        # and MUST close the connection cleanly without sending any
        # response (the peer is untrusted).
        try:
            _reject_unmatched_peer_uid(writer)
        except ControlPeerCredentialError as exc:
            logger.warning("Control connection from %s rejected: %s", peer, exc.reason)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return
        try:
            try:
                raw_line = await asyncio.wait_for(
                    reader.readline(),
                    timeout=COMMAND_TIMEOUT_S,
                )
            except ValueError as exc:
                # ``StreamReader.readline`` raises ``ValueError`` once the
                # buffered line exceeds the stream limit, so oversized
                # requests are rejected before the line is fully materialized.
                raise _OversizedRequestError from exc
            await asyncio.sleep(0)
            if getattr(reader, "_buffer", b""):
                raise _TrailingRequestDataError
            response = await self._process_request(raw_line)
            resp_bytes = jsonx.dumps_bytes(response.to_dict()) + b"\n"
            writer.write(resp_bytes)
            await asyncio.wait_for(writer.drain(), timeout=COMMAND_TIMEOUT_S)
        except TimeoutError:
            logger.warning("control request timed out from %s", peer)
            err_resp = _error_response(
                "",
                "request timed out",
                stage="timeout",
            )
            writer.write(jsonx.dumps_bytes(err_resp.to_dict()) + b"\n")
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.drain(), timeout=COMMAND_TIMEOUT_S)
        except _OversizedRequestError:
            logger.warning("oversized control request from %s", peer)
            err_resp = _error_response(
                "",
                f"request exceeds {MAX_REQUEST_SIZE} byte limit",
                stage="parse",
            )
            writer.write(jsonx.dumps_bytes(err_resp.to_dict()) + b"\n")
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.drain(), timeout=COMMAND_TIMEOUT_S)
        except _TrailingRequestDataError:
            err_resp = _error_response(
                "",
                "multiple requests in one connection are not supported",
                stage="parse",
            )
            writer.write(jsonx.dumps_bytes(err_resp.to_dict()) + b"\n")
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.drain(), timeout=COMMAND_TIMEOUT_S)
        except Exception:
            logger.exception("unhandled error on control connection from %s", peer)
            err_resp = _error_response(
                "",
                "internal server error",
                stage="error",
            )
            writer.write(jsonx.dumps_bytes(err_resp.to_dict()) + b"\n")
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.drain(), timeout=COMMAND_TIMEOUT_S)
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
        params = payload.get("params")
        if params is not None and not isinstance(params, dict):
            return _error_response(
                "",
                "params must be a JSON object",
                stage="parse",
            )
        proto = payload.get("protocol_version")
        if proto != PROTOCOL_VERSION:
            return _error_response(
                _safe_request_id(payload.get("request_id")),
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
        if len(request_id) > MAX_REQUEST_ID_LEN or any(
            ord(char) < 0x20 or char.isspace() for char in request_id
        ):
            return _error_response(
                request_id if len(request_id) <= MAX_REQUEST_ID_LEN else "",
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
            params=cast("dict[str, Any] | None", params),
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
    result_category = {
        "parse": "parse_error",
        "timeout": "timeout",
    }.get(stage, "internal_error")
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
        result_category=result_category,
    )


def _safe_request_id(value: object) -> str:
    """Return a bounded request ID suitable for an error response."""
    if not isinstance(value, str) or not value or len(value) > MAX_REQUEST_ID_LEN:
        return ""
    if any(ord(char) < 0x20 or char.isspace() for char in value):
        return ""
    return value


def _remove_socket_file(
    path: Path,
    *,
    expected_identity: tuple[int, int, int] | None = None,
) -> None:
    """Remove only the expected socket entry, never following a symlink."""
    try:
        if expected_identity is None:
            return
        current = _stat_socket_identity(path)
        if current is None:
            return
        if current[:2] != expected_identity[:2]:
            logger.warning("Socket identity changed; not removing %s", path)
            return
        if not stat.S_ISSOCK(current[2]) or path.is_symlink():
            logger.warning("Refusing to remove non-socket control path %s", path)
            return
        path.unlink(missing_ok=True)
    except OSError:
        return


def _stat_socket_identity(path: Path) -> tuple[int, int, int] | None:
    """Capture (device, inode, mode) for a path. Returns None if unavailable."""
    try:
        st = path.lstat()
        return (st.st_dev, st.st_ino, st.st_mode)
    except OSError:
        return None


def _clean_stale_socket(path: Path) -> None:
    """Remove a stale socket file at *path*.

    Only removes for positive stale signals:
    - ECONNREFUSED: socket exists but no server listening
    - ENOENT: path disappeared during probe

    Fails closed for EACCES, EPERM, timeout, unknown errors,
    regular files, and symlinks.

    Uses inode identity checks to detect pathname replacement races
    between the probe and the unlink.
    """
    identity_before = _stat_socket_identity(path)
    if identity_before is None:
        return

    # Fail closed: refuse to clean symlinks.
    if path.is_symlink():
        logger.warning("Refusing to clean stale symlink at %s", path)
        return

    # Fail closed: refuse to clean non-socket files.
    if not stat.S_ISSOCK(identity_before[2]):
        logger.warning("Refusing to clean non-socket at %s", path)
        return

    try:
        owner_uid = path.lstat().st_uid
    except OSError:
        return
    if owner_uid != os.geteuid():
        logger.warning("Refusing to clean foreign-owned socket at %s", path)
        return

    # Probe the socket.
    try:
        probe = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        try:
            probe.settimeout(1.0)
            probe.connect(str(path))
            # Connection succeeded — a live server is listening.
            raise ControlServerError(f"control socket already in use: {path}")
        except TimeoutError:
            logger.warning("Socket probe timed out for %s; not removing", path)
            return
        except OSError as exc:
            if exc.errno == errno.ECONNREFUSED:
                pass  # stale, safe to remove
            elif exc.errno == errno.ENOENT:
                return  # already gone, nothing to do
            elif exc.errno in (errno.EACCES, errno.EPERM):
                logger.warning("Permission error probing %s; not removing", path)
                return
            else:
                logger.warning(
                    "Unexpected error probing %s: %s; not removing", path, exc
                )
                return
        finally:
            probe.close()
    except ControlServerError:
        raise
    except Exception as exc:
        logger.warning("Could not probe socket %s: %s; not removing", path, exc)
        return

    # Verify identity has not changed during the probe (TOCTOU guard).
    identity_after = _stat_socket_identity(path)
    if identity_after is None or identity_before[:2] != identity_after[:2]:
        logger.warning("Socket identity changed during probe; not removing %s", path)
        return

    # Positive stale signal confirmed — safe to remove.
    try:
        # Re-verify identity at the last possible moment so a file
        # swapped in between the probe and the unlink is never evicted.
        identity_final = _stat_socket_identity(path)
        if identity_final is None or identity_final[:2] != identity_after[:2]:
            logger.warning(
                "Socket identity changed before unlink; not removing %s", path
            )
            return
        # The path may disappear after the final identity check if another
        # process cleaned it first. Treat that race as successful cleanup;
        # never follow a replacement symlink because unlink acts on the
        # directory entry itself.
        path.unlink(missing_ok=True)
        logger.info("Removed stale control socket %s", path)
    except OSError as exc:
        logger.warning("Could not remove stale socket %s: %s", path, exc)


def _restrict_socket_permissions(path: Path) -> None:
    """Set socket mode to 0o600 (owner-only).

    Raises:
        ControlServerError: If chmod fails or mode is wrong after setting.
    """
    try:
        before = path.lstat()
    except OSError as exc:
        raise ControlServerError(
            f"failed to inspect control socket {path}: {exc}"
        ) from exc
    if not stat.S_ISSOCK(before.st_mode) or before.st_uid != os.geteuid():
        raise ControlServerError(f"control socket {path} is not an owner-owned socket")

    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError as exc:
        raise ControlServerError(
            f"failed to restrict socket permissions on {path}: {exc}"
        ) from exc

    try:
        after = path.lstat()
    except OSError as exc:
        raise ControlServerError(
            f"failed to verify socket permissions on {path}: {exc}"
        ) from exc

    if not stat.S_ISSOCK(after.st_mode) or after.st_uid != os.geteuid():
        raise ControlServerError(
            f"control socket {path} was replaced during permission setup"
        )
    actual = stat.S_IMODE(after.st_mode)

    if actual != (stat.S_IRUSR | stat.S_IWUSR):
        raise ControlServerError(
            f"socket {path} has mode {oct(actual)}, expected 0o600"
        )


def _verify_runtime_dir(path: Path) -> None:
    """Verify the runtime directory is safe for control sockets.

    Checks that:
    - The directory is not group/world writable.
    - The directory is owned by the current UID.

    Raises ControlServerError if the directory is unsafe.
    """
    try:
        st = path.lstat()
    except OSError as exc:
        raise ControlServerError(
            f"Cannot stat runtime directory {path}: {exc}"
        ) from exc

    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise ControlServerError(f"Runtime path {path} is not a private directory")

    mode = stat.S_IMODE(st.st_mode)
    # The socket directory itself must not disclose or permit traversal by
    # other users. A sticky shared parent is acceptable because it is not the
    # directory used for the control socket.
    unsafe_bits = stat.S_IRWXG | stat.S_IRWXO
    if mode & unsafe_bits:
        raise ControlServerError(
            f"Runtime directory {path} is not owner-only "
            f"(mode {oct(mode)}); refusing to bind control socket"
        )

    if st.st_uid != os.geteuid():
        raise ControlServerError(
            f"Runtime directory {path} is owned by UID {st.st_uid}, "
            f"expected {os.geteuid()}"
        )


def _ensure_runtime_dir(path: Path) -> None:
    """Create and verify the private directory used for the control socket."""
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise ControlServerError(
            f"Cannot create runtime directory {path}: {exc}"
        ) from exc
    _verify_runtime_dir(path)


class ControlPeerCredentialError(Exception):
    """Raised when a control connection fails peer-credential validation.

    Plan 016 Workstream G1: when ``SO_PEERCRED`` is supported, the
    server must verify the peer's UID matches its own.  On
    insufficient data, on the absence of a socket handle, or on
    ``OSError`` from the kernel the server MUST treat the connection
    as untrusted and terminate the handler cleanly.  Previously the
    helper silently returned, allowing the request to proceed.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _reject_unmatched_peer_uid(writer: asyncio.StreamWriter) -> None:
    """Reject connections from a different UID using SO_PEERCRED where available.

    On Linux, ``SO_PEERCRED`` provides the peer's UID at connection time.
    If the peer UID does not match the server UID the connection is
    closed immediately by raising :class:`ControlPeerCredentialError`.
    On platforms where ``SO_PEERCRED`` is unavailable (e.g. macOS),
    the check is silently skipped — the socket's ``0o600``
    permissions already restrict access to the file owner.

    Plan 016 Workstream G1: when ``SO_PEERCRED`` is supported, the
    helper fails closed.  Any failure to read peer credentials
    (missing socket, insufficient data, ``OSError``, ``ValueError``)
    raises :class:`ControlPeerCredentialError` so the handler
    terminates without processing the request.
    """
    sock: _socket.socket | None = writer.get_extra_info("socket")
    if sock is None:
        raise ControlPeerCredentialError("no socket handle available")

    peercred_attr = getattr(_socket, "SO_PEERCRED", None)
    if peercred_attr is None:
        return  # SO_PEERCRED unavailable — socket perms are the gate

    try:
        size = _struct.calcsize("3i")
        creds_bytes: bytes = sock.getsockopt(_socket.SOL_SOCKET, peercred_attr, size)
    except (OSError, ValueError, _struct.error) as exc:
        raise ControlPeerCredentialError(f"SO_PEERCRED read failed: {exc!r}") from exc

    if len(creds_bytes) != size:
        raise ControlPeerCredentialError("SO_PEERCRED returned insufficient data")
    try:
        _pid, uid, _gid = _struct.unpack("3i", creds_bytes)
    except _struct.error as exc:
        raise ControlPeerCredentialError(f"SO_PEERCRED unpack failed: {exc!r}") from exc

    server_uid = os.getuid()
    if uid != server_uid:
        logger.warning(
            "Rejecting control connection from UID %d (server UID %d)",
            uid,
            server_uid,
        )
        raise ControlPeerCredentialError(f"peer UID {uid} != server UID {server_uid}")
