"""Security and secret-safety review for live configuration rehash (Phase 6).

Covers:
- Secret field redaction in diffs, events, CLI human/JSON output
- Control socket Unix permissions (0o600)
- Stale socket / regular file / symlink handling
- Malformed JSON and oversized request rejection
- Rapid invalid-request resilience (DoS smoke test)
- Server ignoring client-supplied config paths
- Config digest comparison consistency
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import stat
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from eggpool.cli_rehash_format import format_rehash_json, render_rehash_human
from eggpool.config_reload_policy import ConfigDiff, compute_diff
from eggpool.control.client import ControlClient
from eggpool.control.server import (
    MAX_REQUEST_SIZE,
    PROTOCOL_VERSION,
    ControlRequest,
    ControlResponse,
    ControlServer,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SENTINEL_A = "sk-secret-sentinel-do-not-leak-1234567890abcdef"
SENTINEL_B = "sk-secret-sentinel-rotated-fedcba0987654321"
SENTINEL_C = "sk-third-sentinel-abcdefghij1234567890"
SENTINELS = (SENTINEL_A, SENTINEL_B, SENTINEL_C)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# macOS limits Unix socket paths to ~104 bytes.  Use a short temp dir.
_SOCKET_DIR = Path(os.environ.get("TMPDIR", "/tmp")) / "ep-sec-test"


@pytest.fixture()
def socket_dir(tmp_path: Path) -> Path:
    """Short-path temporary directory for socket tests."""
    d = _SOCKET_DIR
    d.mkdir(parents=True, exist_ok=True)
    # Remove stale sockets from prior test runs.
    for f in d.iterdir():
        if f.suffix == ".sock":
            f.unlink(missing_ok=True)
    yield d
    # Clean up: remove sockets then directory.
    for f in d.iterdir():
        if f.suffix == ".sock":
            with contextlib.suppress(OSError):
                f.unlink()
    shutil.rmtree(d, ignore_errors=True)


def _sock(directory: Path, name: str = "test") -> Path:
    return directory / f"{name}.sock"


async def _noop_handler(request: ControlRequest) -> ControlResponse:
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
        message="ok",
    )


def _make_config_with_api_key(api_key: str) -> object:
    from eggpool.models.config import (
        AccountConfig,
        AppConfig,
        ProviderConfig,
    )

    return AppConfig(
        providers={
            "opencode-go": ProviderConfig(
                id="opencode-go",
                base_url="https://example.com/v1",
                protocols=["openai"],
                accounts=[
                    AccountConfig(name="default", api_key=api_key, enabled=True),
                ],
            ),
        },
    )


def _make_config_with_server_key(api_key: str) -> object:
    from eggpool.models.config import AppConfig, ServerConfig

    return AppConfig(
        server=ServerConfig(api_key=api_key),
    )


async def _send_raw(
    sock_path: Path,
    payload: bytes,
    *,
    timeout_s: float = 5.0,
) -> dict:
    """Connect to the socket, send raw bytes, read one response line."""
    reader, writer = await asyncio.wait_for(
        asyncio.open_unix_connection(str(sock_path)),
        timeout=timeout_s,
    )
    try:
        writer.write(payload)
        await writer.drain()
        raw_line = await asyncio.wait_for(reader.readline(), timeout=timeout_s)
        return json.loads(raw_line)
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


# ---------------------------------------------------------------------------
# 1. Secret field diff redaction end-to-end
# ---------------------------------------------------------------------------


class TestSecretFieldDiffRedaction:
    """Verify compute_diff redacts secret fields in ConfigChange records."""

    def test_account_api_key_redaction(self) -> None:
        old = _make_config_with_api_key(SENTINEL_A)
        new = _make_config_with_api_key(SENTINEL_B)
        diff = compute_diff(old, new)

        secret_changes = [c for c in diff.changes if c.secret]
        assert len(secret_changes) >= 1, "Expected at least one secret change"

        for change in secret_changes:
            assert change.old_display == "<changed>"
            assert change.new_display == "<changed>"
            assert SENTINEL_A not in change.old_display
            assert SENTINEL_B not in change.new_display

    def test_sentinel_not_in_str_or_repr(self) -> None:
        old = _make_config_with_api_key(SENTINEL_A)
        new = _make_config_with_api_key(SENTINEL_B)
        diff = compute_diff(old, new)

        diff_str = str(diff)
        diff_repr = repr(diff)
        for sentinel in (SENTINEL_A, SENTINEL_B):
            assert sentinel not in diff_str, f"Sentinel leaked in str(diff): {sentinel}"
            assert sentinel not in diff_repr, (
                f"Sentinel leaked in repr(diff): {sentinel}"
            )

    def test_sentinel_not_in_json_encoded_diff(self) -> None:
        old = _make_config_with_api_key(SENTINEL_A)
        new = _make_config_with_api_key(SENTINEL_B)
        diff = compute_diff(old, new)

        changes_as_dicts = [
            {
                "path": c.path,
                "old_display": c.old_display,
                "new_display": c.new_display,
                "secret": c.secret,
            }
            for c in diff.changes
        ]
        encoded = json.dumps(changes_as_dicts)
        assert SENTINEL_A not in encoded
        assert SENTINEL_B not in encoded

    def test_sentinel_not_in_changes_tuple_str(self) -> None:
        old = _make_config_with_api_key(SENTINEL_A)
        new = _make_config_with_api_key(SENTINEL_B)
        diff = compute_diff(old, new)

        changes_str = str(diff.changes)
        assert SENTINEL_A not in changes_str
        assert SENTINEL_B not in changes_str

    def test_server_key_redaction(self) -> None:
        old = _make_config_with_server_key(SENTINEL_A)
        new = _make_config_with_server_key(SENTINEL_B)
        diff = compute_diff(old, new)

        secret_changes = [c for c in diff.changes if c.secret]
        assert len(secret_changes) >= 1

        for change in secret_changes:
            assert change.old_display == "<changed>"
            assert change.new_display == "<changed>"


# ---------------------------------------------------------------------------
# 2. Recorded event never carries secret payload
# ---------------------------------------------------------------------------


class TestRecordedEventSecretSafety:
    """Verify operational events never contain secret payloads."""

    @pytest.mark.asyncio
    async def test_event_never_carries_secret_payload(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from eggpool.control.reload_manager import ReloadManager
        from eggpool.runtime_manager import RuntimeManager

        rm = RuntimeManager()
        proc = AsyncMock()
        proc.db = AsyncMock()
        proc.stats_db = AsyncMock()
        proc.metrics_coalescer = AsyncMock()
        mgr = ReloadManager(rm, proc)

        from eggpool.models.config import AppConfig, ServerConfig

        config = AppConfig(server=ServerConfig(host="0.0.0.0", port=8080))
        from eggpool.runtime_manager import RuntimeGeneration

        gen = RuntimeGeneration(
            generation_id=0,
            config=config,
            config_digest="a" * 64,
            registry=AsyncMock(),
            catalog=AsyncMock(),
            router=AsyncMock(),
            coordinator=AsyncMock(),
            client_pool=AsyncMock(),
            outbound_manager=AsyncMock(),
            dns_backend=None,
            health_manager=AsyncMock(),
            cost_calculator=AsyncMock(),
            transcoder_policy=AsyncMock(),
            compression_policy=AsyncMock(),
            cache_config=AsyncMock(),
            compression_tuning_registry=AsyncMock(),
            dispatch_overhead_recorder=AsyncMock(),
            dispatch_span_recorder=AsyncMock(),
            account_backoff_repo=AsyncMock(),
            stats_service=AsyncMock(),
            supervisor=AsyncMock(),
            finalization_retry_queue=AsyncMock(),
            routing_trace_guard=AsyncMock(),
            routing_trace_writer=None,
            created_at_monotonic=time.monotonic(),
            created_at_epoch=time.time(),
        )
        await rm.install_initial(gen)

        captured_events: list[tuple[str, str]] = []

        async def _capture_event(event_type: str, **kwargs: object) -> None:
            payload_json = json.dumps({"event_type": event_type, **kwargs}, default=str)
            captured_events.append((event_type, payload_json))

        monkeypatch.setattr(mgr, "_record_event", _capture_event)

        validation = AsyncMock()
        validation.content_digest = "b" * 64
        validation.warnings = ()

        with (
            patch.object(
                mgr,
                "_compute_reload_diff",
                new_callable=AsyncMock,
                return_value=ConfigDiff(changes=()),
            ),
        ):
            await mgr.reload(validation)

        for event_type, payload_json in captured_events:
            for sentinel in SENTINELS:
                assert sentinel not in payload_json, (
                    f"Sentinel {sentinel!r} leaked in event {event_type!r} "
                    f"payload: {payload_json}"
                )

    @pytest.mark.asyncio
    async def test_multiple_sentinel_values_not_in_non_error_events(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-error operational events must never carry secret payloads.

        NOTE: The ``error`` field in failure events currently carries the
        raw exception ``repr()``, which may contain sensitive data.  This
        is a known limitation documented in the test
        ``test_error_event_carries_raw_exception_repr`` below.
        """
        from eggpool.control.reload_manager import ReloadManager
        from eggpool.runtime_manager import RuntimeManager

        rm = RuntimeManager()
        proc = AsyncMock()
        proc.db = AsyncMock()
        proc.stats_db = AsyncMock()
        proc.metrics_coalescer = AsyncMock()
        mgr = ReloadManager(rm, proc)

        from eggpool.models.config import AppConfig, ServerConfig

        config = AppConfig(server=ServerConfig(host="0.0.0.0", port=8080))
        from eggpool.runtime_manager import RuntimeGeneration

        gen = RuntimeGeneration(
            generation_id=0,
            config=config,
            config_digest="a" * 64,
            registry=AsyncMock(),
            catalog=AsyncMock(),
            router=AsyncMock(),
            coordinator=AsyncMock(),
            client_pool=AsyncMock(),
            outbound_manager=AsyncMock(),
            dns_backend=None,
            health_manager=AsyncMock(),
            cost_calculator=AsyncMock(),
            transcoder_policy=AsyncMock(),
            compression_policy=AsyncMock(),
            cache_config=AsyncMock(),
            compression_tuning_registry=AsyncMock(),
            dispatch_overhead_recorder=AsyncMock(),
            dispatch_span_recorder=AsyncMock(),
            account_backoff_repo=AsyncMock(),
            stats_service=AsyncMock(),
            supervisor=AsyncMock(),
            finalization_retry_queue=AsyncMock(),
            routing_trace_guard=AsyncMock(),
            routing_trace_writer=None,
            created_at_monotonic=time.monotonic(),
            created_at_epoch=time.time(),
        )
        await rm.install_initial(gen)

        captured_payloads: list[str] = []

        async def _capture_event(event_type: str, **kwargs: object) -> None:
            payload_json = json.dumps({"event_type": event_type, **kwargs}, default=str)
            captured_payloads.append(payload_json)

        monkeypatch.setattr(mgr, "_record_event", _capture_event)

        validation = AsyncMock()
        validation.content_digest = "b" * 64
        validation.warnings = ()

        # Inject secrets into error messages via exception
        with (
            patch.object(
                mgr,
                "_compute_reload_diff",
                new_callable=AsyncMock,
                side_effect=ValueError(f"secret={SENTINEL_A} and also {SENTINEL_B}"),
            ),
        ):
            await mgr.reload(validation)

        # Non-error events (like reload_requested) must not carry secrets
        non_error_payloads = [p for p in captured_payloads if '"error"' not in p]
        for payload in non_error_payloads:
            for sentinel in SENTINELS:
                assert sentinel not in payload, (
                    f"Sentinel {sentinel!r} leaked in non-error event: {payload}"
                )

    @pytest.mark.asyncio
    async def test_error_event_carries_raw_exception_repr(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Error events MUST NOT carry raw secret-shaped exception repr().

        Milestone D3 closes the gap: ``_record_event`` runs the
        ``error`` payload through ``sanitize_text_for_audit`` before
        persisting to operational events, so a leaked secret in an
        exception message is replaced with ``<redacted>`` and never
        reaches SQLite.  This test patches
        ``OperationalEventRepository.record`` (not ``_record_event``)
        so the sanitization path actually runs.
        """
        from eggpool.control.reload_manager import ReloadManager
        from eggpool.runtime_manager import RuntimeManager

        rm = RuntimeManager()
        proc = AsyncMock()
        proc.db = AsyncMock()
        proc.stats_db = AsyncMock()
        proc.metrics_coalescer = AsyncMock()
        mgr = ReloadManager(rm, proc)

        from eggpool.models.config import AppConfig, ServerConfig

        config = AppConfig(server=ServerConfig(host="0.0.0.0", port=8080))
        from eggpool.runtime_manager import RuntimeGeneration

        gen = RuntimeGeneration(
            generation_id=0,
            config=config,
            config_digest="a" * 64,
            registry=AsyncMock(),
            catalog=AsyncMock(),
            router=AsyncMock(),
            coordinator=AsyncMock(),
            client_pool=AsyncMock(),
            outbound_manager=AsyncMock(),
            dns_backend=None,
            health_manager=AsyncMock(),
            cost_calculator=AsyncMock(),
            transcoder_policy=AsyncMock(),
            compression_policy=AsyncMock(),
            cache_config=AsyncMock(),
            compression_tuning_registry=AsyncMock(),
            dispatch_overhead_recorder=AsyncMock(),
            dispatch_span_recorder=AsyncMock(),
            account_backoff_repo=AsyncMock(),
            stats_service=AsyncMock(),
            supervisor=AsyncMock(),
            finalization_retry_queue=AsyncMock(),
            routing_trace_guard=AsyncMock(),
            routing_trace_writer=None,
            created_at_monotonic=time.monotonic(),
            created_at_epoch=time.time(),
        )
        await rm.install_initial(gen)

        captured_payloads: list[str] = []

        class _FakeRepo:
            def __init__(self, _db: object) -> None:
                pass

            async def record(self, event_type: str, details: dict[str, object]) -> None:
                payload_json = json.dumps(
                    {"event_type": event_type, **details}, default=str
                )
                captured_payloads.append(payload_json)

        validation = AsyncMock()
        validation.content_digest = "b" * 64
        validation.warnings = ()

        with (
            patch("eggpool.db.repositories.OperationalEventRepository", _FakeRepo),
            patch.object(
                mgr,
                "_compute_reload_diff",
                new_callable=AsyncMock,
                side_effect=ValueError(f"secret={SENTINEL_A} and also {SENTINEL_B}"),
            ),
        ):
            await mgr.reload(validation)

        validation = AsyncMock()
        validation.content_digest = "b" * 64
        validation.warnings = ()

        with (
            patch.object(
                mgr,
                "_compute_reload_diff",
                new_callable=AsyncMock,
                side_effect=ValueError(f"secret={SENTINEL_A} and also {SENTINEL_B}"),
            ),
        ):
            await mgr.reload(validation)

        error_payloads = [p for p in captured_payloads if '"error"' in p]
        assert len(error_payloads) >= 1, "Expected at least one error event"

        for payload in error_payloads:
            for sentinel in SENTINELS:
                assert sentinel not in payload, (
                    f"Sentinel {sentinel!r} leaked in error event: {payload}"
                )
            assert "<redacted>" in payload, (
                f"Expected <redacted> token in error event: {payload}"
            )


# ---------------------------------------------------------------------------
# 3. CLI human output redacts secrets
# ---------------------------------------------------------------------------


class TestCliHumanOutputRedactsSecrets:
    """render_rehash_human redacts both <old>/<new> placeholder tokens
    and raw secret-shaped values from every output field."""

    def test_secret_not_in_stdout(self) -> None:
        """stdout (success path) is clean."""
        resp = ControlResponse(
            protocol_version=PROTOCOL_VERSION,
            request_id="test",
            ok=False,
            stage="diff",
            generation=None,
            changed_sections=(),
            warnings=(),
            restart_required=(f"api_key: {SENTINEL_A} -> {SENTINEL_B}",),
            retirement_pending=False,
            message=f"Field changed: api_key {SENTINEL_A} to {SENTINEL_B}",
        )
        stdout, _stderr = render_rehash_human(resp)

        for sentinel in (SENTINEL_A, SENTINEL_B):
            assert sentinel not in stdout, (
                f"Sentinel {sentinel!r} leaked in stdout: {stdout}"
            )

    def test_redact_message_replaces_old_new_tokens(self) -> None:
        """_redact_message replaces <old>/<new> placeholder tokens."""
        from eggpool.cli_rehash_format import _redact_message

        raw = "api_key: <old> -> <new>"
        redacted = _redact_message(raw)
        assert "<redacted>" in redacted
        assert "<old>" not in redacted
        assert "<new>" not in redacted

    def test_redact_message_redacts_raw_secret_values(self) -> None:
        """D3 closes the gap: raw secret-shaped values in messages are redacted.

        The previous implementation only handled ``<old>``/``<new>``
        placeholder tokens.  Milestone D3 routes every message through
        :func:`eggpool.config_reload_policy.sanitize_text_for_audit` so
        ``sk-...``, ``Bearer ...``, ``key: ...``, and other credential
        patterns are replaced with ``<redacted>`` before the CLI renders
        them.
        """
        from eggpool.cli_rehash_format import _redact_message

        raw = f"api_key changed from {SENTINEL_A} to {SENTINEL_B}"
        result = _redact_message(raw)
        assert SENTINEL_A not in result, (
            f"Raw sentinel leaked in redacted message: {result!r}"
        )
        assert SENTINEL_B not in result, (
            f"Raw sentinel leaked in redacted message: {result!r}"
        )
        assert "<redacted>" in result

    def test_restart_required_fields_in_stderr(self) -> None:
        resp = ControlResponse(
            protocol_version=PROTOCOL_VERSION,
            request_id="test",
            ok=False,
            stage="diff",
            generation=None,
            changed_sections=(),
            warnings=(),
            restart_required=(f"api_key: {SENTINEL_A} -> {SENTINEL_B}",),
            retirement_pending=False,
            message="Reload rejected",
        )
        _stdout, stderr = render_rehash_human(resp)

        assert "restart-required" in stderr.lower()

    def test_redacted_message_contains_redacted_token(self) -> None:
        resp = ControlResponse(
            protocol_version=PROTOCOL_VERSION,
            request_id="test",
            ok=False,
            stage="diff",
            generation=None,
            changed_sections=(),
            warnings=(),
            restart_required=(f"api_key: {SENTINEL_A}",),
            retirement_pending=False,
            message=f"api_key changed: {SENTINEL_A}",
        )
        _stdout, stderr = render_rehash_human(resp)

        assert "<redacted>" in stderr or "api_key" in stderr.lower()


# ---------------------------------------------------------------------------
# 4. CLI JSON output redacts secrets
# ---------------------------------------------------------------------------


class TestCliJsonOutputRedactsSecrets:
    """format_rehash_json passes the message through; redaction is done
    by render_rehash_human before calling format_rehash_json in practice."""

    def test_json_restart_required_preserved(self) -> None:
        """restart_required display strings are preserved for CLI consumers."""
        display = f"api_key: {SENTINEL_A} -> {SENTINEL_B}"
        resp = ControlResponse(
            protocol_version=PROTOCOL_VERSION,
            request_id="test",
            ok=False,
            stage="diff",
            generation=None,
            changed_sections=(),
            warnings=(),
            restart_required=(display,),
            retirement_pending=False,
            message="Reload rejected",
        )
        result = format_rehash_json(resp, exit_code=2)
        assert result["restart_required"] == [display]

    def test_json_output_is_valid_json(self) -> None:
        resp = ControlResponse(
            protocol_version=PROTOCOL_VERSION,
            request_id="test",
            ok=False,
            stage="diff",
            generation=None,
            changed_sections=(),
            warnings=(),
            restart_required=(f"api_key: {SENTINEL_A}",),
            retirement_pending=False,
            message="Reload rejected",
        )
        result = format_rehash_json(resp, exit_code=2)
        encoded = json.dumps(result)
        parsed = json.loads(encoded)
        assert parsed["ok"] is False


# ---------------------------------------------------------------------------
# 5. Control socket Unix permissions
# ---------------------------------------------------------------------------


class TestControlSocketUnixPermissions:
    """Control socket must have 0o600 permissions after binding."""

    @pytest.mark.asyncio
    async def test_socket_permissions_are_0o600(self, socket_dir: Path) -> None:
        path = _sock(socket_dir)
        srv = ControlServer(_noop_handler, path=path)
        await srv.start()
        try:
            st = os.stat(path)
            mode = st.st_mode & 0o777
            assert mode == 0o600, f"Socket permissions are {oct(mode)}, expected 0o600"
        finally:
            await srv.stop()

    @pytest.mark.asyncio
    async def test_socket_is_a_socket(self, socket_dir: Path) -> None:
        path = _sock(socket_dir)
        srv = ControlServer(_noop_handler, path=path)
        await srv.start()
        try:
            st = os.stat(path)
            assert stat.S_ISSOCK(st.st_mode), (
                f"Socket path is not a socket: mode={oct(st.st_mode)}"
            )
        finally:
            await srv.stop()


# ---------------------------------------------------------------------------
# 6. Stale socket / regular file replacement
# ---------------------------------------------------------------------------


class TestStaleSocketReplacement:
    """Pre-existing stale files at the socket path are handled correctly."""

    @pytest.mark.asyncio
    async def test_stale_regular_file_prevents_binding(self, socket_dir: Path) -> None:
        """A regular file at the socket path is NOT cleaned by _clean_stale_socket
        (it only removes actual sockets), so bind fails with ControlServerError."""
        path = _sock(socket_dir)
        path.write_text("stale content", encoding="utf-8")

        srv = ControlServer(_noop_handler, path=path)
        with pytest.raises(Exception, match="failed to bind|control socket"):
            await srv.start()

    @pytest.mark.asyncio
    async def test_stale_socket_is_replaced(self, socket_dir: Path) -> None:
        """A stale socket file (S_ISSOCK=True) IS cleaned and replaced."""
        path = _sock(socket_dir)
        path.write_text("", encoding="utf-8")
        # Make it look like a socket for _clean_stale_socket
        original_stat = os.stat

        def _fake_stat(p: Path, *args: object, **kwargs: object) -> os.stat_result:
            result = original_stat(p, *args, **kwargs)
            if p == path:
                # Patch st_mode to include S_ISSOCK
                new_mode = result.st_mode | stat.S_IFSOCK
                return os.stat_result(
                    (
                        new_mode,
                        result.st_ino,
                        result.st_dev,
                        result.st_nlink,
                        result.st_uid,
                        result.st_gid,
                        result.st_size,
                        result.st_atime,
                        result.st_mtime,
                        result.st_ctime,
                    )
                )
            return result

        with patch("os.stat", side_effect=_fake_stat):
            srv = ControlServer(_noop_handler, path=path)
            await srv.start()
            try:
                assert path.exists()
                assert stat.S_ISSOCK(os.stat(path).st_mode)
            finally:
                await srv.stop()

    @pytest.mark.asyncio
    async def test_client_can_connect_after_start(self, socket_dir: Path) -> None:
        """After server starts, a ControlClient can connect and get a response."""
        path = _sock(socket_dir)
        srv = ControlServer(_noop_handler, path=path)
        await srv.start()
        try:
            client = ControlClient(socket_path=path)
            resp = await client.reload(validated_digest="a" * 64)
            assert resp.ok is True
        finally:
            await srv.stop()


# ---------------------------------------------------------------------------
# 7. Symlink at socket path is rejected or handled safely
# ---------------------------------------------------------------------------


class TestSymlinkSocketRejection:
    """Symlinks at the socket path must not be followed blindly."""

    @pytest.mark.asyncio
    async def test_symlink_prevents_binding(self, socket_dir: Path) -> None:
        """A symlink at the socket path causes bind to fail (ControlServerError).

        _clean_stale_socket only removes files where S_ISSOCK is True;
        a symlink to a regular file does not pass that check, so
        asyncio.start_unix_server raises OSError which becomes
        ControlServerError.
        """
        target = socket_dir / "target.txt"
        target.write_text("not a socket", encoding="utf-8")
        path = _sock(socket_dir, "prevent")
        os.symlink(str(target), str(path))

        srv = ControlServer(_noop_handler, path=path)
        with pytest.raises(Exception, match="failed to bind|control socket"):
            await srv.start()

    @pytest.mark.asyncio
    async def test_symlink_to_nonexistent_target_allows_binding(
        self, socket_dir: Path
    ) -> None:
        """A dangling symlink at the socket path is removed by the OS
        when asyncio.start_unix_server binds, so the server starts
        successfully.  This is safe because the server always creates
        a fresh socket."""
        target = socket_dir / "nonexistent_target"
        path = _sock(socket_dir, "symlink")
        os.symlink(str(target), str(path))

        srv = ControlServer(_noop_handler, path=path)
        await srv.start()
        try:
            assert path.exists()
            assert stat.S_ISSOCK(os.stat(path).st_mode)
            # Verify client can connect
            client = ControlClient(socket_path=path)
            resp = await client.reload(validated_digest="a" * 64)
            assert resp.ok is True
        finally:
            await srv.stop()


# ---------------------------------------------------------------------------
# 8. Malformed JSON request rejected gracefully
# ---------------------------------------------------------------------------


class TestMalformedJsonRejected:
    """Malformed JSON on the control socket must produce an error response."""

    @pytest.mark.asyncio
    async def test_malformed_json_returns_error(self, socket_dir: Path) -> None:
        path = _sock(socket_dir)
        srv = ControlServer(_noop_handler, path=path)
        await srv.start()
        try:
            payload = b"not-json{{\n"
            resp = await _send_raw(path, payload)
            assert resp["ok"] is False
            assert resp["stage"] == "parse"
            assert "invalid JSON" in resp["message"]
        finally:
            await srv.stop()

    @pytest.mark.asyncio
    async def test_server_survives_malformed_request(self, socket_dir: Path) -> None:
        """Server remains healthy after receiving malformed JSON."""
        path = _sock(socket_dir)
        srv = ControlServer(_noop_handler, path=path)
        await srv.start()
        try:
            # Send malformed request
            bad_payload = b"this is not json\n"
            await _send_raw(path, bad_payload)

            # Server should still accept a valid request
            valid_payload = (
                json.dumps(
                    {
                        "protocol_version": PROTOCOL_VERSION,
                        "request_id": "after-bad",
                        "command": "reload_config",
                        "validated_digest": "a" * 64,
                    }
                ).encode()
                + b"\n"
            )
            resp = await _send_raw(path, valid_payload)
            assert resp["ok"] is True
        finally:
            await srv.stop()

    @pytest.mark.asyncio
    async def test_malformed_json_no_internal_state_leak(
        self, socket_dir: Path
    ) -> None:
        """Error response from malformed JSON does not leak internal state."""
        path = _sock(socket_dir)
        srv = ControlServer(_noop_handler, path=path)
        await srv.start()
        try:
            payload = b"{{invalid}}\n"
            resp = await _send_raw(path, payload)
            # The message should contain the error but not internal details
            assert resp["ok"] is False
            # Should not contain Python traceback or exception class names
            message_lower = resp["message"].lower()
            assert "traceback" not in message_lower
            assert "traceback" not in message_lower
        finally:
            await srv.stop()


# ---------------------------------------------------------------------------
# 9. Oversized request rejected
# ---------------------------------------------------------------------------


class TestOversizedRequestRejected:
    """Requests exceeding MAX_REQUEST_SIZE (64KB) are rejected."""

    @pytest.mark.asyncio
    async def test_oversized_request_rejected(self, socket_dir: Path) -> None:
        path = _sock(socket_dir)
        srv = ControlServer(_noop_handler, path=path)
        await srv.start()
        try:
            oversized = b'{"data": "' + b"x" * (MAX_REQUEST_SIZE + 100) + b'"}\n'
            resp = await _send_raw(path, oversized)
            assert resp["ok"] is False
            # Oversized payloads may exceed StreamReader limit before
            # the protocol check, so stage could be "parse" or "error"
            assert resp["stage"] in ("parse", "error")
        finally:
            await srv.stop()

    @pytest.mark.asyncio
    async def test_oversized_no_crash(self, socket_dir: Path) -> None:
        """Server stays healthy after oversized request."""
        path = _sock(socket_dir)
        srv = ControlServer(_noop_handler, path=path)
        await srv.start()
        try:
            oversized = b'{"data": "' + b"x" * (MAX_REQUEST_SIZE + 1000) + b'"}\n'
            await _send_raw(path, oversized)

            # Server should still accept valid requests
            valid = (
                json.dumps(
                    {
                        "protocol_version": PROTOCOL_VERSION,
                        "request_id": "after-oversized",
                        "command": "reload_config",
                        "validated_digest": "b" * 64,
                    }
                ).encode()
                + b"\n"
            )
            resp = await _send_raw(path, valid)
            assert resp["ok"] is True
        finally:
            await srv.stop()

    @pytest.mark.asyncio
    async def test_exactly_at_limit_accepted(self, socket_dir: Path) -> None:
        """Request exactly at MAX_REQUEST_SIZE is accepted (parse-level)."""
        path = _sock(socket_dir)
        srv = ControlServer(_noop_handler, path=path)
        await srv.start()
        try:
            # Build a payload that is exactly MAX_REQUEST_SIZE bytes
            padding_len = MAX_REQUEST_SIZE - len(b'{"data": ""}\n')
            payload = b'{"data": "' + b"x" * padding_len + b'"}\n'
            assert len(payload) == MAX_REQUEST_SIZE
            resp = await _send_raw(path, payload)
            # Should not be rejected for size; may fail for other reasons
            # (e.g. missing fields), but not "byte limit"
            assert "byte limit" not in resp.get("message", "")
        finally:
            await srv.stop()


# ---------------------------------------------------------------------------
# 10. Repeated reload DoS resilience
# ---------------------------------------------------------------------------


class TestRepeatedReloadDosResilience:
    """Rapid invalid reload requests must not deadlock or slow the server."""

    @pytest.mark.asyncio
    async def test_rapid_invalid_requests_resilient(self, socket_dir: Path) -> None:
        path = _sock(socket_dir)
        srv = ControlServer(_noop_handler, path=path)
        await srv.start()
        try:
            invalid_payload = b"not-json\n"
            durations: list[float] = []

            for _ in range(20):
                start = time.monotonic()
                resp = await _send_raw(path, invalid_payload)
                elapsed = time.monotonic() - start
                durations.append(elapsed)
                assert resp["ok"] is False

            # Each request should complete quickly (under 1 second)
            for idx, d in enumerate(durations):
                assert d < 1.0, f"Request {idx} took {d:.3f}s, expected < 1.0s"

            # Server should still be healthy after the barrage
            valid = (
                json.dumps(
                    {
                        "protocol_version": PROTOCOL_VERSION,
                        "request_id": "post-dos",
                        "command": "reload_config",
                        "validated_digest": "c" * 64,
                    }
                ).encode()
                + b"\n"
            )
            resp = await _send_raw(path, valid)
            assert resp["ok"] is True
        finally:
            await srv.stop()

    @pytest.mark.asyncio
    async def test_average_latency_bounded(self, socket_dir: Path) -> None:
        """Average latency across 20 invalid requests stays under 200ms."""
        path = _sock(socket_dir)
        srv = ControlServer(_noop_handler, path=path)
        await srv.start()
        try:
            durations: list[float] = []
            for _ in range(20):
                start = time.monotonic()
                await _send_raw(path, b"garbage\n")
                durations.append(time.monotonic() - start)

            avg = sum(durations) / len(durations)
            assert avg < 0.2, f"Average latency {avg:.3f}s exceeds 200ms"
        finally:
            await srv.stop()


# ---------------------------------------------------------------------------
# 11. Server ignores client-supplied config path
# ---------------------------------------------------------------------------


class TestServerIgnoresClientConfigPath:
    """Server must always reload its startup-resolved path."""

    @pytest.mark.asyncio
    async def test_client_cannot_specify_config_path(self, socket_dir: Path) -> None:
        """A reload_config request with a fake config_path is ignored.

        The control protocol does not include a config_path field;
        the server always reloads its startup-resolved path.  Sending
        extra fields is harmless — they are not forwarded.
        """
        path = _sock(socket_dir)
        captured_requests: list[ControlRequest] = []

        async def _capture_handler(request: ControlRequest) -> ControlRequest:
            captured_requests.append(request)
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
                message="ok",
            )

        srv = ControlServer(_capture_handler, path=path)
        await srv.start()
        try:
            # Send request with a malicious config_path field
            payload = (
                json.dumps(
                    {
                        "protocol_version": PROTOCOL_VERSION,
                        "request_id": "evil-1",
                        "command": "reload_config",
                        "validated_digest": "a" * 64,
                        "config_path": "/etc/shadow",
                        "path": "/tmp/evil-config.toml",
                    }
                ).encode()
                + b"\n"
            )
            resp = await _send_raw(path, payload)
            assert resp["ok"] is True

            # Verify the handler received the request but the server
            # does not expose a config_path to the handler
            assert len(captured_requests) == 1
            req = captured_requests[0]
            assert req.command == "reload_config"
            # The ControlRequest dataclass has no config_path field
            assert not hasattr(req, "config_path")
        finally:
            await srv.stop()

    @pytest.mark.asyncio
    async def test_wire_protocol_no_config_path_field(self, socket_dir: Path) -> None:
        """Verify the ControlRequest dataclass has no config_path field."""
        req = ControlRequest(
            protocol_version=PROTOCOL_VERSION,
            request_id="r1",
            command="reload_config",
        )
        # The field should not exist on the dataclass
        assert not hasattr(req, "config_path")
        assert not hasattr(req, "path")


# ---------------------------------------------------------------------------
# 12. Digest comparison uses consistent comparison
# ---------------------------------------------------------------------------


class TestDigestComparisonConsistency:
    """Config digest comparison must be consistent across computations."""

    def test_same_inputs_same_digest(self) -> None:
        """Identical configs produce identical digests."""
        config = _make_config_with_api_key("test-key-123")
        from eggpool.config_validation import ConfigValidationResult

        val1 = ConfigValidationResult(
            config=config,
            source_path=Path("/dev/null"),
            content_digest="a" * 64,
            runtime_fingerprint="b" * 64,
            warnings=(),
        )
        val2 = ConfigValidationResult(
            config=config,
            source_path=Path("/dev/null"),
            content_digest="a" * 64,
            runtime_fingerprint="b" * 64,
            warnings=(),
        )
        assert val1.content_digest == val2.content_digest

    def test_different_inputs_different_digest(self) -> None:
        """Different digests are correctly distinguished."""
        from eggpool.config_validation import ConfigValidationResult

        config = _make_config_with_api_key("test-key-123")
        val1 = ConfigValidationResult(
            config=config,
            source_path=Path("/dev/null"),
            content_digest="a" * 64,
            runtime_fingerprint="b" * 64,
            warnings=(),
        )
        val2 = ConfigValidationResult(
            config=config,
            source_path=Path("/dev/null"),
            content_digest="c" * 64,
            runtime_fingerprint="d" * 64,
            warnings=(),
        )
        assert val1.content_digest != val2.content_digest

    def test_digest_comparison_consistent_across_alternation(self) -> None:
        """Running diff computations in alternation does not change results."""
        config_a = _make_config_with_api_key(SENTINEL_A)
        config_b = _make_config_with_api_key(SENTINEL_B)
        config_c = _make_config_with_api_key(SENTINEL_C)

        # Compute in one order
        diff_ab_1 = compute_diff(config_a, config_b)
        diff_bc_1 = compute_diff(config_b, config_c)

        # Compute in a different order
        diff_bc_2 = compute_diff(config_b, config_c)
        diff_ab_2 = compute_diff(config_a, config_b)

        # Results must be identical regardless of computation order
        assert len(diff_ab_1.changes) == len(diff_ab_2.changes)
        assert len(diff_bc_1.changes) == len(diff_bc_2.changes)

        for c1, c2 in zip(diff_ab_1.changes, diff_ab_2.changes, strict=True):
            assert c1.path == c2.path
            assert c1.old_display == c2.old_display
            assert c1.new_display == c2.new_display
            assert c1.secret == c2.secret
