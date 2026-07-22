"""Tests for the control socket server and client."""

from __future__ import annotations

import asyncio
import json
import os as _os
import shutil as _shutil
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from eggpool.control.client import (
    ControlClient,
    ControlClientConnectionError,
    ControlClientProtocolError,
    ControlClientTimeoutError,
    _parse_response,
    _to_str_list,
)
from eggpool.control.server import (
    MAX_REQUEST_SIZE,
    PROTOCOL_VERSION,
    ControlRequest,
    ControlResponse,
    ControlServer,
    _clean_stale_socket,
    _error_response,
    control_socket_path,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# macOS limits Unix socket paths to ~104 bytes.  tmp_path produces very long
# paths so we create a short-lived tempdir for socket tests.


@pytest.fixture()
def socket_dir() -> Path:
    """Return a short-path temporary directory for Unix socket files."""
    d = Path(_os.path.join(_os.environ.get("TMPDIR", "/tmp"), "ep-test"))
    d.mkdir(parents=True, exist_ok=True)
    for f in d.iterdir():
        if f.suffix == ".sock":
            f.unlink(missing_ok=True)
    yield d
    _shutil.rmtree(d, ignore_errors=True)


def _sock(directory: Path) -> Path:
    return directory / "eggpool.sock"


async def _noop_handler(request: ControlRequest) -> ControlResponse:
    """Minimal handler that returns a success response."""
    return ControlResponse(
        protocol_version=PROTOCOL_VERSION,
        request_id=request.request_id,
        ok=True,
        stage="commit",
        generation=1,
        changed_sections=("routing",),
        warnings=(),
        restart_required=(),
        retirement_pending=False,
        message="ok",
    )


def _make_request(
    *,
    request_id: str = "test-req-1",
    command: str = "reload_config",
    validated_digest: str | None = "a" * 64,
    protocol_version: int = PROTOCOL_VERSION,
) -> dict:
    """Build a raw request payload dict."""
    payload: dict = {
        "protocol_version": protocol_version,
        "request_id": request_id,
        "command": command,
    }
    if validated_digest is not None:
        payload["validated_digest"] = validated_digest
    return payload


class _Silence:
    """Context manager that suppresses all exceptions."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *args: object) -> None:
        return None


async def _send_raw(
    socket_path: Path,
    payload: dict,
    *,
    timeout_s: float = 5.0,
) -> dict:
    """Connect to the socket, send one JSON line, read one response line."""
    reader, writer = await asyncio.wait_for(
        asyncio.open_unix_connection(str(socket_path)),
        timeout=timeout_s,
    )
    try:
        writer.write(json.dumps(payload).encode() + b"\n")
        await writer.drain()
        raw_line = await asyncio.wait_for(reader.readline(), timeout=timeout_s)
        return json.loads(raw_line)
    finally:
        writer.close()
        with _Silence():
            await writer.wait_closed()


# ---------------------------------------------------------------------------
# ControlRequest / ControlResponse
# ---------------------------------------------------------------------------


class TestControlRequest:
    def test_frozen(self) -> None:
        req = ControlRequest(
            protocol_version=1, request_id="r1", command="reload_config"
        )
        with pytest.raises(AttributeError):
            req.request_id = "r2"  # type: ignore[misc]

    def test_defaults(self) -> None:
        req = ControlRequest(
            protocol_version=1, request_id="r1", command="reload_config"
        )
        assert req.validated_digest is None


class TestControlResponse:
    def test_to_dict(self) -> None:
        resp = ControlResponse(
            protocol_version=PROTOCOL_VERSION,
            request_id="abc",
            ok=True,
            stage="commit",
            generation=3,
            changed_sections=("routing", "accounts"),
            warnings=(),
            restart_required=(),
            retirement_pending=False,
            message="applied",
        )
        d = resp.to_dict()
        assert d["protocol_version"] == PROTOCOL_VERSION
        assert d["request_id"] == "abc"
        assert d["ok"] is True
        assert d["stage"] == "commit"
        assert d["generation"] == 3
        assert d["changed_sections"] == ["routing", "accounts"]
        assert d["warnings"] == []
        assert d["restart_required"] == []
        assert d["retirement_pending"] is False
        assert d["message"] == "applied"

    def test_roundtrip(self) -> None:
        resp = ControlResponse(
            protocol_version=1,
            request_id="xyz",
            ok=False,
            stage="parse",
            generation=None,
            changed_sections=(),
            warnings=("warn1",),
            restart_required=(),
            retirement_pending=True,
            message="bad",
        )
        d = resp.to_dict()
        raw = json.dumps(d).encode() + b"\n"
        parsed = json.loads(raw)
        assert parsed["protocol_version"] == 1
        assert parsed["generation"] is None
        assert parsed["warnings"] == ["warn1"]
        assert parsed["retirement_pending"] is True


# ---------------------------------------------------------------------------
# control_socket_path
# ---------------------------------------------------------------------------


class TestControlSocketPath:
    def test_returns_path(self) -> None:
        result = control_socket_path()
        assert isinstance(result, Path)

    def test_ends_with_sock(self) -> None:
        result = control_socket_path()
        assert result.name == "eggpool.sock"


# ---------------------------------------------------------------------------
# _clean_stale_socket
# ---------------------------------------------------------------------------


class TestCleanStaleSocket:
    def test_removes_socket(self, tmp_path: Path) -> None:
        """A socket-like file is removed when S_ISSOCK reports True."""
        sock = tmp_path / "test.sock"
        sock.write_text("", encoding="utf-8")
        with patch("stat.S_ISSOCK", return_value=True):
            _clean_stale_socket(sock)
        assert not sock.exists()

    def test_ignores_nonexistent(self, tmp_path: Path) -> None:
        _clean_stale_socket(tmp_path / "nope.sock")

    def test_ignores_regular_file(self, tmp_path: Path) -> None:
        f = tmp_path / "regular.txt"
        f.write_text("hello", encoding="utf-8")
        _clean_stale_socket(f)
        assert f.exists()


# ---------------------------------------------------------------------------
# _error_response
# ---------------------------------------------------------------------------


class TestErrorResponse:
    def test_defaults(self) -> None:
        resp = _error_response("req-1", "something broke")
        assert resp.ok is False
        assert resp.stage == "error"
        assert resp.request_id == "req-1"
        assert resp.message == "something broke"
        assert resp.generation is None
        assert resp.changed_sections == ()
        assert resp.retirement_pending is False

    def test_custom_stage(self) -> None:
        resp = _error_response("r", "msg", stage="parse")
        assert resp.stage == "parse"

    def test_protocol_version(self) -> None:
        resp = _error_response("", "")
        assert resp.protocol_version == PROTOCOL_VERSION


# ---------------------------------------------------------------------------
# ControlServer lifecycle
# ---------------------------------------------------------------------------


class TestControlServerLifecycle:
    @pytest.mark.asyncio
    async def test_start_and_stop(self, socket_dir: Path) -> None:
        srv = ControlServer(_noop_handler, path=_sock(socket_dir))
        await srv.start()
        assert srv._server is not None
        await srv.stop()
        assert srv._server is None

    @pytest.mark.asyncio
    async def test_creates_socket_file(self, socket_dir: Path) -> None:
        path = _sock(socket_dir)
        srv = ControlServer(_noop_handler, path=path)
        await srv.start()
        assert path.exists()
        await srv.stop()

    @pytest.mark.asyncio
    async def test_removes_socket_on_stop(self, socket_dir: Path) -> None:
        path = _sock(socket_dir)
        srv = ControlServer(_noop_handler, path=path)
        await srv.start()
        assert path.exists()
        await srv.stop()
        assert not path.exists()

    @pytest.mark.asyncio
    async def test_concurrent_connections(self, socket_dir: Path) -> None:
        path = _sock(socket_dir)
        srv = ControlServer(_noop_handler, path=path)
        await srv.start()
        try:
            tasks = []
            for i in range(5):
                payload = _make_request(request_id=f"concurrent-{i}")
                tasks.append(_send_raw(path, payload))
            results = await asyncio.gather(*tasks)
            assert all(r["ok"] for r in results)
            ids = {r["request_id"] for r in results}
            assert ids == {f"concurrent-{i}" for i in range(5)}
        finally:
            await srv.stop()


# ---------------------------------------------------------------------------
# ControlServer request validation
# ---------------------------------------------------------------------------


class TestControlServerValidation:
    @pytest.mark.asyncio
    async def test_handles_valid_request(self, socket_dir: Path) -> None:
        path = _sock(socket_dir)
        srv = ControlServer(_noop_handler, path=path)
        await srv.start()
        try:
            resp = await _send_raw(path, _make_request(request_id="valid-1"))
            assert resp["ok"] is True
            assert resp["request_id"] == "valid-1"
        finally:
            await srv.stop()

    @pytest.mark.asyncio
    async def test_rejects_empty_request(self, socket_dir: Path) -> None:
        path = _sock(socket_dir)
        srv = ControlServer(_noop_handler, path=path)
        await srv.start()
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(path)), timeout=5.0
            )
            try:
                writer.write(b"\n")
                await writer.drain()
                raw = await asyncio.wait_for(reader.readline(), timeout=5.0)
                resp = json.loads(raw)
                assert resp["ok"] is False
                assert resp["stage"] == "parse"
                assert "empty request" in resp["message"]
            finally:
                writer.close()
                with _Silence():
                    await writer.wait_closed()
        finally:
            await srv.stop()

    @pytest.mark.asyncio
    async def test_rejects_oversized_request(self, socket_dir: Path) -> None:
        path = _sock(socket_dir)
        srv = ControlServer(_noop_handler, path=path)
        await srv.start()
        try:
            oversized = b'{"data": "' + b"x" * (MAX_REQUEST_SIZE + 100) + b'"}\n'
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(path)), timeout=5.0
            )
            try:
                writer.write(oversized)
                await writer.drain()
                raw = await asyncio.wait_for(reader.readline(), timeout=5.0)
                resp = json.loads(raw)
                assert resp["ok"] is False
                # Oversized payloads exceed the StreamReader limit (64KB)
                # before the server's protocol check, so the error stage
                # is "error" (generic handler) rather than "parse".
                assert resp["stage"] in ("parse", "error")
            finally:
                writer.close()
                with _Silence():
                    await writer.wait_closed()
        finally:
            await srv.stop()

    @pytest.mark.asyncio
    async def test_process_request_rejects_oversized(self) -> None:
        """_process_request rejects payloads exceeding MAX_REQUEST_SIZE."""
        srv = ControlServer(_noop_handler, path=Path("/dev/null"))
        oversized_line = b'{"data": "' + b"x" * (MAX_REQUEST_SIZE + 100) + b'"}\n'
        resp = await srv._process_request(oversized_line)
        assert resp.ok is False
        assert resp.stage == "parse"
        assert "byte limit" in resp.message

    @pytest.mark.asyncio
    async def test_rejects_invalid_json(self, socket_dir: Path) -> None:
        path = _sock(socket_dir)
        srv = ControlServer(_noop_handler, path=path)
        await srv.start()
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(path)), timeout=5.0
            )
            try:
                writer.write(b"not json at all\n")
                await writer.drain()
                raw = await asyncio.wait_for(reader.readline(), timeout=5.0)
                resp = json.loads(raw)
                assert resp["ok"] is False
                assert resp["stage"] == "parse"
                assert "invalid JSON" in resp["message"]
            finally:
                writer.close()
                with _Silence():
                    await writer.wait_closed()
        finally:
            await srv.stop()

    @pytest.mark.asyncio
    async def test_rejects_wrong_protocol(self, socket_dir: Path) -> None:
        path = _sock(socket_dir)
        srv = ControlServer(_noop_handler, path=path)
        await srv.start()
        try:
            payload = _make_request(protocol_version=999)
            resp = await _send_raw(path, payload)
            assert resp["ok"] is False
            assert resp["stage"] == "parse"
            assert "unsupported protocol version" in resp["message"]
        finally:
            await srv.stop()

    @pytest.mark.asyncio
    async def test_rejects_missing_command(self, socket_dir: Path) -> None:
        path = _sock(socket_dir)
        srv = ControlServer(_noop_handler, path=path)
        await srv.start()
        try:
            payload = {"protocol_version": PROTOCOL_VERSION, "request_id": "r1"}
            resp = await _send_raw(path, payload)
            assert resp["ok"] is False
            assert resp["stage"] == "parse"
            assert "missing command" in resp["message"]
        finally:
            await srv.stop()

    @pytest.mark.asyncio
    async def test_rejects_non_object_json(self, socket_dir: Path) -> None:
        path = _sock(socket_dir)
        srv = ControlServer(_noop_handler, path=path)
        await srv.start()
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(path)), timeout=5.0
            )
            try:
                writer.write(b"[1, 2, 3]\n")
                await writer.drain()
                raw = await asyncio.wait_for(reader.readline(), timeout=5.0)
                resp = json.loads(raw)
                assert resp["ok"] is False
                assert resp["stage"] == "parse"
                assert "request must be a JSON object" in resp["message"]
            finally:
                writer.close()
                with _Silence():
                    await writer.wait_closed()
        finally:
            await srv.stop()

    @pytest.mark.asyncio
    async def test_rejects_unknown_command(self, socket_dir: Path) -> None:
        path = _sock(socket_dir)
        srv = ControlServer(_noop_handler, path=path)
        await srv.start()
        try:
            payload = _make_request(command="shutdown")
            resp = await _send_raw(path, payload)
            assert resp["ok"] is False
            assert resp["stage"] == "parse"
            assert "unknown command: shutdown" in resp["message"]
        finally:
            await srv.stop()

    @pytest.mark.asyncio
    async def test_rejects_empty_request_id(self, socket_dir: Path) -> None:
        path = _sock(socket_dir)
        srv = ControlServer(_noop_handler, path=path)
        await srv.start()
        try:
            payload = {
                "protocol_version": PROTOCOL_VERSION,
                "command": "reload_config",
                "request_id": "",
            }
            resp = await _send_raw(path, payload)
            assert resp["ok"] is False
            assert resp["stage"] == "parse"
            assert "missing or invalid request_id" in resp["message"]
        finally:
            await srv.stop()

    @pytest.mark.asyncio
    async def test_rejects_non_string_request_id(self, socket_dir: Path) -> None:
        path = _sock(socket_dir)
        srv = ControlServer(_noop_handler, path=path)
        await srv.start()
        try:
            payload = {
                "protocol_version": PROTOCOL_VERSION,
                "command": "reload_config",
                "request_id": 123,
            }
            resp = await _send_raw(path, payload)
            assert resp["ok"] is False
            assert resp["stage"] == "parse"
            assert "missing or invalid request_id" in resp["message"]
        finally:
            await srv.stop()

    @pytest.mark.asyncio
    async def test_rejects_too_long_request_id(self, socket_dir: Path) -> None:
        path = _sock(socket_dir)
        srv = ControlServer(_noop_handler, path=path)
        await srv.start()
        try:
            payload = _make_request(request_id="x" * 300)
            resp = await _send_raw(path, payload)
            assert resp["ok"] is False
            assert resp["stage"] == "parse"
            assert "request_id exceeds maximum length" in resp["message"]
        finally:
            await srv.stop()

    @pytest.mark.asyncio
    async def test_rejects_invalid_digest_format(self, socket_dir: Path) -> None:
        path = _sock(socket_dir)
        srv = ControlServer(_noop_handler, path=path)
        await srv.start()
        try:
            payload = _make_request(validated_digest="not-hex")
            resp = await _send_raw(path, payload)
            assert resp["ok"] is False
            assert resp["stage"] == "parse"
            assert "validated_digest" in resp["message"]
        finally:
            await srv.stop()

    @pytest.mark.asyncio
    async def test_rejects_short_digest(self, socket_dir: Path) -> None:
        path = _sock(socket_dir)
        srv = ControlServer(_noop_handler, path=path)
        await srv.start()
        try:
            payload = _make_request(validated_digest="abc123")
            resp = await _send_raw(path, payload)
            assert resp["ok"] is False
            assert resp["stage"] == "parse"
            assert "validated_digest" in resp["message"]
        finally:
            await srv.stop()

    @pytest.mark.asyncio
    async def test_rejects_non_string_digest(self, socket_dir: Path) -> None:
        path = _sock(socket_dir)
        srv = ControlServer(_noop_handler, path=path)
        await srv.start()
        try:
            payload = {
                "protocol_version": PROTOCOL_VERSION,
                "command": "reload_config",
                "request_id": "r1",
                "validated_digest": 123,
            }
            resp = await _send_raw(path, payload)
            assert resp["ok"] is False
            assert resp["stage"] == "parse"
            assert "validated_digest" in resp["message"]
        finally:
            await srv.stop()


# ---------------------------------------------------------------------------
# ControlServer handler dispatch
# ---------------------------------------------------------------------------


class TestControlServerHandler:
    @pytest.mark.asyncio
    async def test_calls_handler_with_correct_request(self, socket_dir: Path) -> None:
        handler = AsyncMock(
            return_value=ControlResponse(
                protocol_version=PROTOCOL_VERSION,
                request_id="h1",
                ok=True,
                stage="commit",
                generation=1,
                changed_sections=(),
                warnings=(),
                restart_required=(),
                retirement_pending=False,
                message="handled",
            )
        )
        path = _sock(socket_dir)
        srv = ControlServer(handler, path=path)
        await srv.start()
        try:
            resp = await _send_raw(path, _make_request(request_id="h1"))
            assert resp["ok"] is True
            call_args = handler.call_args
            req: ControlRequest = call_args[0][0]
            assert req.request_id == "h1"
            assert req.command == "reload_config"
            assert req.validated_digest == "a" * 64
        finally:
            await srv.stop()

    @pytest.mark.asyncio
    async def test_handler_exception_returns_error(self, socket_dir: Path) -> None:
        async def _failing_handler(request: ControlRequest) -> ControlResponse:
            raise RuntimeError("boom")

        path = _sock(socket_dir)
        srv = ControlServer(_failing_handler, path=path)
        await srv.start()
        try:
            resp = await _send_raw(path, _make_request(request_id="fail-1"))
            assert resp["ok"] is False
            assert resp["stage"] == "handler"
            assert resp["message"] == "handler error"
            assert "boom" not in resp["message"]
        finally:
            await srv.stop()


# ---------------------------------------------------------------------------
# ControlClient
# ---------------------------------------------------------------------------


class TestControlClient:
    @pytest.mark.asyncio
    async def test_reload_sends_correct_payload(self, socket_dir: Path) -> None:
        handler = AsyncMock(
            return_value=ControlResponse(
                protocol_version=PROTOCOL_VERSION,
                request_id="",
                ok=True,
                stage="commit",
                generation=1,
                changed_sections=(),
                warnings=(),
                restart_required=(),
                retirement_pending=False,
                message="ok",
            )
        )
        path = _sock(socket_dir)
        srv = ControlServer(handler, path=path)
        await srv.start()
        try:
            client = ControlClient(socket_path=path)
            await client.reload(validated_digest="deadbeef" * 8)
            call_args = handler.call_args
            req: ControlRequest = call_args[0][0]
            assert req.command == "reload_config"
            assert req.validated_digest == "deadbeef" * 8
            assert req.protocol_version == PROTOCOL_VERSION
        finally:
            await srv.stop()

    @pytest.mark.asyncio
    async def test_handles_connection_error(self, socket_dir: Path) -> None:
        sock_path = socket_dir / "no_server.sock"
        client = ControlClient(socket_path=sock_path)
        with pytest.raises(ControlClientConnectionError):
            await client.reload(validated_digest="a" * 64)

    @pytest.mark.asyncio
    async def test_handles_timeout(self, socket_dir: Path) -> None:
        async def _slow_handler(request: ControlRequest) -> ControlResponse:
            await asyncio.sleep(10)
            return ControlResponse(
                protocol_version=PROTOCOL_VERSION,
                request_id=request.request_id,
                ok=True,
                stage="commit",
                generation=1,
                changed_sections=(),
                warnings=(),
                restart_required=(),
                retirement_pending=False,
                message="slow",
            )

        path = _sock(socket_dir)
        srv = ControlServer(_slow_handler, path=path)
        await srv.start()
        try:
            client = ControlClient(socket_path=path, timeout_s=0.1)
            with pytest.raises(ControlClientTimeoutError):
                await client.reload(validated_digest="a" * 64)
        finally:
            await srv.stop()

    @pytest.mark.asyncio
    async def test_parses_valid_response(self, socket_dir: Path) -> None:
        path = _sock(socket_dir)
        srv = ControlServer(_noop_handler, path=path)
        await srv.start()
        try:
            client = ControlClient(socket_path=path)
            resp = await client.reload(validated_digest="b" * 64)
            assert resp.ok is True
            assert resp.stage == "commit"
            assert resp.generation == 1
            assert resp.changed_sections == ("routing",)
            assert resp.message == "ok"
        finally:
            await srv.stop()


# ---------------------------------------------------------------------------
# _parse_response and _to_str_list helpers
# ---------------------------------------------------------------------------


class TestClientHelpers:
    def test_to_str_list_with_list(self) -> None:
        assert _to_str_list(["a", "b", 42]) == ["a", "b", "42"]

    def test_to_str_list_with_non_list(self) -> None:
        assert _to_str_list("not a list") == []
        assert _to_str_list(None) == []

    def test_parse_response_valid(self) -> None:
        raw = (
            json.dumps(
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "request_id": "p1",
                    "ok": True,
                    "stage": "commit",
                    "generation": 5,
                    "changed_sections": ["routing"],
                    "warnings": [],
                    "restart_required": [],
                    "retirement_pending": False,
                    "message": "done",
                }
            ).encode()
            + b"\n"
        )
        resp = _parse_response(raw)
        assert resp.ok is True
        assert resp.generation == 5
        assert resp.changed_sections == ("routing",)

    def test_parse_response_empty_line(self) -> None:
        # _parse_response tries to JSON-parse the line; empty bytes
        # causes an invalid-JSON error, not an "empty response" error.
        with pytest.raises(ControlClientProtocolError, match="invalid JSON"):
            _parse_response(b"\n")

    def test_parse_response_wrong_protocol(self) -> None:
        raw = (
            json.dumps(
                {
                    "protocol_version": 99,
                    "request_id": "p2",
                    "ok": True,
                    "stage": "commit",
                    "generation": None,
                    "changed_sections": [],
                    "warnings": [],
                    "restart_required": [],
                    "retirement_pending": False,
                    "message": "x",
                }
            ).encode()
            + b"\n"
        )
        with pytest.raises(ControlClientProtocolError, match="unsupported protocol"):
            _parse_response(raw)

    def test_parse_response_invalid_json(self) -> None:
        with pytest.raises(ControlClientProtocolError, match="invalid JSON"):
            _parse_response(b"not json\n")


# ---------------------------------------------------------------------------
# Integration: server + client roundtrip
# ---------------------------------------------------------------------------


class TestIntegration:
    @pytest.mark.asyncio
    async def test_server_client_roundtrip(self, socket_dir: Path) -> None:
        path = _sock(socket_dir)
        srv = ControlServer(_noop_handler, path=path)
        await srv.start()
        try:
            client = ControlClient(socket_path=path)
            resp = await client.reload(validated_digest="c" * 64)
            assert resp.ok is True
            assert resp.generation == 1
            assert resp.changed_sections == ("routing",)
            assert resp.message == "ok"
        finally:
            await srv.stop()

    @pytest.mark.asyncio
    async def test_server_client_digest_exchange(self, socket_dir: Path) -> None:
        received_digest: list[str] = []

        async def _capture_handler(request: ControlRequest) -> ControlResponse:
            received_digest.append(request.validated_digest or "")
            return ControlResponse(
                protocol_version=PROTOCOL_VERSION,
                request_id=request.request_id,
                ok=True,
                stage="commit",
                generation=1,
                changed_sections=(),
                warnings=(),
                restart_required=(),
                retirement_pending=False,
                message="captured",
            )

        path = _sock(socket_dir)
        srv = ControlServer(_capture_handler, path=path)
        await srv.start()
        try:
            digest = "ff00ff00" * 8
            client = ControlClient(socket_path=path)
            resp = await client.reload(validated_digest=digest)
            assert resp.ok is True
            assert len(received_digest) == 1
            assert received_digest[0] == digest
        finally:
            await srv.stop()

    @pytest.mark.asyncio
    async def test_roundtrip_with_digest_none(self, socket_dir: Path) -> None:
        received_digest: list[str | None] = []

        async def _capture_handler(request: ControlRequest) -> ControlResponse:
            received_digest.append(request.validated_digest)
            return ControlResponse(
                protocol_version=PROTOCOL_VERSION,
                request_id=request.request_id,
                ok=True,
                stage="commit",
                generation=2,
                changed_sections=(),
                warnings=(),
                restart_required=(),
                retirement_pending=False,
                message="ok",
            )

        path = _sock(socket_dir)
        srv = ControlServer(_capture_handler, path=path)
        await srv.start()
        try:
            # reload() always sets validated_digest; test via raw connection
            req = ControlRequest(
                protocol_version=PROTOCOL_VERSION,
                request_id="nd-1",
                command="reload_config",
                validated_digest=None,
            )
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(path)), timeout=5.0
            )
            try:
                payload = {
                    "protocol_version": req.protocol_version,
                    "request_id": req.request_id,
                    "command": req.command,
                    "validated_digest": req.validated_digest,
                }
                writer.write(json.dumps(payload).encode() + b"\n")
                await writer.drain()
                raw = await asyncio.wait_for(reader.readline(), timeout=5.0)
                parsed = json.loads(raw)
                assert parsed["ok"] is True
                assert parsed["generation"] == 2
            finally:
                writer.close()
                with _Silence():
                    await writer.wait_closed()
            assert received_digest == [None]
        finally:
            await srv.stop()
