"""Black-box launchers, observations, and deterministic fixtures.

This module intentionally has no dependency on the implementation under test
other than the Python executable path and the Rust executable path.  It is
safe to import from ordinary Python tests and keeps all process, filesystem,
and network resources bounded by context managers.
"""

from __future__ import annotations

import contextlib
import hashlib
import html.parser
import http.server
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from types import TracebackType


class Implementation(StrEnum):
    """Implementation identity carried by every observation."""

    PYTHON = "python"
    RUST = "rust"


@dataclass(frozen=True)
class ImplementationIdentity:
    """The executable identity used for a black-box process."""

    implementation: Implementation
    executable: Path

    def metadata(self) -> dict[str, str]:
        return {
            "implementation": self.implementation.value,
            "executable": str(self.executable.resolve()),
        }


@dataclass(frozen=True)
class IsolatedEnvironment:
    """Temporary HOME/XDG roots and deterministic subprocess environment."""

    root: Path
    home: Path
    config_home: Path
    data_home: Path
    state_home: Path
    temp_home: tempfile.TemporaryDirectory[str] = field(repr=False, compare=False)

    @classmethod
    def create(cls) -> Self:
        temporary = tempfile.TemporaryDirectory(prefix="eggpool-migration-")
        root = Path(temporary.name)
        home = root / "home"
        config_home = root / "config"
        data_home = root / "data"
        state_home = root / "state"
        for directory in (home, config_home, data_home, state_home):
            directory.mkdir()
        return cls(root, home, config_home, data_home, state_home, temporary)

    def env(self, overrides: Mapping[str, str] | None = None) -> dict[str, str]:
        """Return a sanitized, deterministic environment for a child process."""
        inherited = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("EGGPOOL_")
            and key not in {"SERVER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"}
        }
        inherited.update(
            {
                "HOME": str(self.home),
                "XDG_CONFIG_HOME": str(self.config_home),
                "XDG_DATA_HOME": str(self.data_home),
                "XDG_STATE_HOME": str(self.state_home),
                "TMPDIR": str(self.root / "tmp"),
                "TZ": "UTC",
                "PYTHONHASHSEED": "0",
                "LC_ALL": "C",
                "LANG": "C",
            }
        )
        (self.root / "tmp").mkdir(parents=True, exist_ok=True)
        if overrides:
            inherited.update(overrides)
        return inherited

    def implementation_root(self, implementation: Implementation) -> Path:
        """Return a private root for one implementation's writable state."""
        root = self.root / implementation.value
        root.mkdir(parents=True, exist_ok=True)
        return root

    def database_path(
        self, implementation: Implementation, filename: str = "usage.sqlite3"
    ) -> Path:
        """Return a writable DB path that cannot collide across implementations."""
        return self.implementation_root(implementation) / filename

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.temp_home.cleanup()


def isolated_environment() -> IsolatedEnvironment:
    """Create an isolated environment for a migration test."""
    return IsolatedEnvironment.create()


@dataclass(frozen=True)
class CommandObservation:
    """Raw subprocess result before comparison normalization."""

    identity: ImplementationIdentity
    argv: tuple[str, ...]
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    pid: int
    duration_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.identity.metadata(),
            "argv": list(self.argv),
            "args": list(self.contract_args),
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "timed_out": self.timed_out,
            "pid": self.pid,
            "duration_ms": self.duration_ms,
        }

    @property
    def contract_args(self) -> tuple[str, ...]:
        """Return implementation-independent command arguments."""
        values = self.argv
        if self.identity.implementation is Implementation.PYTHON:
            marker = ("-m", "eggpool")
            if len(values) >= 3 and tuple(values[1:3]) == marker:
                return values[3:]
        return values[1:]


def _terminate_process(process: subprocess.Popen[bytes], timeout: float) -> None:
    """Terminate a process and its process group, bounded by ``timeout``."""
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=timeout)
        return
    except (ProcessLookupError, subprocess.TimeoutExpired):
        pass
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.wait(timeout=timeout)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        pass


class ProcessRunner:
    """Run subprocesses with process-group cleanup and useful diagnostics."""

    def run(
        self,
        identity: ImplementationIdentity,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float = 10.0,
    ) -> CommandObservation:
        started = time.monotonic()
        process = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        timed_out = False
        try:
            stdout_bytes, stderr_bytes = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            timed_out = True
            _terminate_process(process, timeout=min(timeout, 2.0))
            stdout_bytes, stderr_bytes = process.communicate()
            if not stdout_bytes and error.output:
                stdout_bytes = error.output
            if not stderr_bytes and error.stderr:
                stderr_bytes = error.stderr
        return CommandObservation(
            identity=identity,
            argv=tuple(str(value) for value in argv),
            exit_code=None if timed_out else process.returncode,
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
            timed_out=timed_out,
            pid=process.pid,
            duration_ms=round((time.monotonic() - started) * 1000),
        )

    def spawn(
        self,
        identity: ImplementationIdentity,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
    ) -> RunningProcess:
        process = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        return RunningProcess(identity, process)


class RunningProcess:
    """A server process that always has an explicit bounded teardown path."""

    def __init__(
        self,
        identity: ImplementationIdentity,
        process: subprocess.Popen[bytes],
    ) -> None:
        self.identity = identity
        self.process = process

    @property
    def pid(self) -> int:
        return self.process.pid

    def stop(self, timeout: float = 5.0) -> None:
        if self.process.poll() is None:
            _terminate_process(self.process, timeout)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.stop()


class Launcher:
    """Base class for explicit implementation launchers."""

    identity: ImplementationIdentity

    def command(self, args: Sequence[str]) -> list[str]:
        raise NotImplementedError

    def run(
        self,
        args: Sequence[str],
        *,
        environment: IsolatedEnvironment,
        timeout: float = 10.0,
    ) -> CommandObservation:
        return ProcessRunner().run(
            self.identity,
            self.command(args),
            cwd=environment.root,
            env=environment.env(),
            timeout=timeout,
        )

    def spawn(
        self,
        args: Sequence[str],
        *,
        environment: IsolatedEnvironment,
    ) -> RunningProcess:
        return ProcessRunner().spawn(
            self.identity,
            self.command(args),
            cwd=environment.root,
            env=environment.env(),
        )


class PythonLauncher(Launcher):
    """Launch the checked-out Python package without using the installed CLI."""

    def __init__(self, repository: Path | None = None) -> None:
        self.repository = (repository or Path(__file__).parents[2]).resolve()
        self.identity = ImplementationIdentity(
            Implementation.PYTHON, Path(sys.executable)
        )

    def command(self, args: Sequence[str]) -> list[str]:
        return [
            str(self.identity.executable),
            "-m",
            "eggpool",
            *[str(arg) for arg in args],
        ]

    def run(
        self,
        args: Sequence[str],
        *,
        environment: IsolatedEnvironment,
        timeout: float = 10.0,
    ) -> CommandObservation:
        env = environment.env({"PYTHONPATH": str(self.repository / "src")})
        return ProcessRunner().run(
            self.identity,
            self.command(args),
            cwd=self.repository,
            env=env,
            timeout=timeout,
        )

    def spawn(
        self,
        args: Sequence[str],
        *,
        environment: IsolatedEnvironment,
    ) -> RunningProcess:
        env = environment.env({"PYTHONPATH": str(self.repository / "src")})
        return ProcessRunner().spawn(
            self.identity,
            self.command(args),
            cwd=self.repository,
            env=env,
        )


class RustLauncher(Launcher):
    """Launch the Rust candidate by its explicit Cargo output path."""

    def __init__(
        self,
        repository: Path | None = None,
        executable: Path | None = None,
    ) -> None:
        self.repository = (repository or Path(__file__).parents[2]).resolve()
        candidate = (
            executable or self.repository / "rust" / "target" / "debug" / "eggpool"
        )
        self.identity = ImplementationIdentity(Implementation.RUST, candidate.resolve())

    def command(self, args: Sequence[str]) -> list[str]:
        if not self.identity.executable.is_file():
            raise FileNotFoundError(
                f"Rust candidate is not built at {self.identity.executable}; "
                "run cargo build --manifest-path rust/Cargo.toml"
            )
        return [str(self.identity.executable), *[str(arg) for arg in args]]


def assert_distinct_implementations(*launchers: Launcher) -> None:
    """Reject a comparison that could accidentally invoke one implementation twice."""
    if len(launchers) < 2:
        raise AssertionError("a differential comparison needs two implementations")
    identities = [launcher.identity for launcher in launchers]
    kinds = {identity.implementation for identity in identities}
    executables = {identity.executable.resolve() for identity in identities}
    if len(kinds) != len(identities):
        raise AssertionError(
            "differential launchers must have distinct implementation identities"
        )
    if len(executables) != len(identities):
        raise AssertionError(
            "differential launchers must have distinct executable paths"
        )


@dataclass(frozen=True)
class ConfigObservation:
    """Black-box config validation result with a stable error category."""

    command: CommandObservation
    valid: bool
    error_category: str | None

    def to_dict(self) -> dict[str, Any]:
        result = self.command.to_dict()
        result.update({"valid": self.valid, "error_category": self.error_category})
        return result


def _config_error_category(command: CommandObservation) -> str | None:
    if command.exit_code == 0:
        return None
    text = f"{command.stdout}\n{command.stderr}".lower()
    categories = (
        ("parse", ("toml", "parse")),
        ("schema", ("validation", "schema", "invalid")),
        ("auth", ("api key", "authentication", "credential")),
        ("filesystem", ("no such file", "could not read", "permission")),
    )
    for category, markers in categories:
        if any(marker in text for marker in markers):
            return category
    return "unknown"


def capture_config(
    launcher: Launcher,
    config_path: Path,
    environment: IsolatedEnvironment,
) -> ConfigObservation:
    command = launcher.run(
        ["--config", str(config_path), "check-config"],
        environment=environment,
    )
    return ConfigObservation(
        command, command.exit_code == 0, _config_error_category(command)
    )


@dataclass(frozen=True)
class SseFrame:
    """One SSE event preserving event/data grammar rather than JSON semantics."""

    event: str | None
    event_id: str | None
    data: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"event": self.event, "id": self.event_id, "data": list(self.data)}


def _parse_sse(body: str) -> tuple[SseFrame, ...]:
    frames: list[SseFrame] = []
    event: str | None = None
    event_id: str | None = None
    data: list[str] = []
    for line in body.splitlines():
        if not line:
            if event is not None or event_id is not None or data:
                frames.append(SseFrame(event, event_id, tuple(data)))
            event, event_id, data = None, None, []
            continue
        field_name, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field_name == "event":
            event = value
        elif field_name == "id":
            event_id = value
        elif field_name == "data":
            data.append(value)
    if event is not None or event_id is not None or data:
        frames.append(SseFrame(event, event_id, tuple(data)))
    return tuple(frames)


@dataclass(frozen=True)
class HttpObservation:
    """HTTP response observation with an explicit stable-header allowlist."""

    identity: ImplementationIdentity
    method: str
    path: str
    status: int
    headers: tuple[tuple[str, str], ...]
    body: str
    sse_frames: tuple[SseFrame, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            **self.identity.metadata(),
            "method": self.method,
            "path": self.path,
            "status": self.status,
            "headers": {key: value for key, value in self.headers},
            "body": self.body,
        }
        if self.sse_frames:
            result["sse_frames"] = [frame.to_dict() for frame in self.sse_frames]
        return result


_STABLE_RESPONSE_HEADERS = frozenset(
    {"cache-control", "content-type", "allow", "www-authenticate"}
)


def observe_http(
    identity: ImplementationIdentity,
    url: str,
    *,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    body: bytes | None = None,
    timeout: float = 10.0,
) -> HttpObservation:
    """Make one bounded HTTP request and retain only reviewed headers."""
    request = urllib.request.Request(
        url, data=body, headers=dict(headers or {}), method=method
    )
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        response_body = response.read().decode("utf-8", errors="replace")
        content_type = response.headers.get("content-type", "")
        selected = tuple(
            sorted(
                (key.casefold(), value)
                for key, value in response.headers.items()
                if key.casefold() in _STABLE_RESPONSE_HEADERS
            )
        )
        return HttpObservation(
            identity=identity,
            method=method,
            path=urlsplit(url).path or "/",
            status=response.status,
            headers=selected,
            body=response_body,
            sse_frames=(
                _parse_sse(response_body) if "text/event-stream" in content_type else ()
            ),
        )


@dataclass(frozen=True)
class HtmlObservation:
    """Raw HTML plus parser facts used for DOM-sensitive comparisons."""

    identity: ImplementationIdentity
    path: str
    body: str
    elements: tuple[tuple[str, tuple[tuple[str, str], ...]], ...]
    text: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.identity.metadata(),
            "path": self.path,
            "body": self.body,
            "elements": [
                {"tag": tag, "attributes": dict(attributes)}
                for tag, attributes in self.elements
            ],
            "text": list(self.text),
        }


class _HtmlFactsParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[tuple[str, tuple[tuple[str, str], ...]]] = []
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements.append(
            (tag, tuple(sorted((name, value or "") for name, value in attrs)))
        )

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        self.text.append(data)


def capture_html(
    identity: ImplementationIdentity, path: str, body: str
) -> HtmlObservation:
    parser = _HtmlFactsParser()
    parser.feed(body)
    return HtmlObservation(
        identity, path, body, tuple(parser.elements), tuple(parser.text)
    )


@dataclass(frozen=True)
class StaticObservation:
    """Static resource identity and hash; content is not silently normalized."""

    identity: ImplementationIdentity
    path: str
    content_type: str
    size: int
    sha256: str

    @classmethod
    def from_bytes(
        cls,
        identity: ImplementationIdentity,
        path: str,
        content_type: str,
        content: bytes,
    ) -> Self:
        return cls(
            identity,
            path,
            content_type,
            len(content),
            hashlib.sha256(content).hexdigest(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.identity.metadata(),
            "path": self.path,
            "content_type": self.content_type,
            "size": self.size,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class DatabaseObservation:
    """SQLite schema and selected durable effects, with no request payloads."""

    identity: ImplementationIdentity
    user_version: int
    tables: tuple[dict[str, Any], ...]
    indexes: tuple[dict[str, Any], ...]
    row_counts: tuple[tuple[str, int], ...]
    migration_checksums: tuple[tuple[str, str], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.identity.metadata(),
            "user_version": self.user_version,
            "tables": list(self.tables),
            "indexes": list(self.indexes),
            "row_counts": {key: value for key, value in self.row_counts},
            "migration_checksums": {
                key: value for key, value in self.migration_checksums
            },
        }


def capture_database(
    identity: ImplementationIdentity,
    database_path: Path,
    *,
    checksum_path: Path | None = None,
) -> DatabaseObservation:
    """Capture schema facts through SQLite's read API only."""
    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    try:
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        tables: list[dict[str, Any]] = []
        indexes: list[dict[str, Any]] = []
        row_counts: list[tuple[str, int]] = []
        names = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        for name in names:
            columns = [
                {
                    "name": row[1],
                    "type": row[2],
                    "not_null": bool(row[3]),
                    "primary_key": int(row[5]),
                    "default": row[4],
                }
                for row in connection.execute(f'PRAGMA table_info("{name}")')
            ]
            tables.append({"name": name, "columns": columns})
            row_counts.append(
                (
                    name,
                    int(
                        connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[
                            0
                        ]
                    ),
                )
            )
            for row in connection.execute(f'PRAGMA index_list("{name}")'):
                indexes.append({"table": name, "name": row[1], "unique": bool(row[2])})
        checksums: dict[str, str] = {}
        if checksum_path is not None and checksum_path.is_file():
            checksum_document = json.loads(checksum_path.read_text(encoding="utf-8"))
            checksums = checksum_document.get("files", checksum_document)
        return DatabaseObservation(
            identity,
            user_version,
            tuple(tables),
            tuple(sorted(indexes, key=lambda item: (item["table"], item["name"]))),
            tuple(row_counts),
            tuple(sorted(checksums.items())),
        )
    finally:
        connection.close()


def capture_startup_state(
    database_path: Path,
) -> dict[str, tuple[tuple[Any, ...], ...]]:
    """Capture startup-owned durable rows without exposing request contents."""
    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    try:
        return {
            "migrations": tuple(
                connection.execute(
                    "SELECT version, name, applied_at FROM _migrations ORDER BY version"
                )
            ),
            "accounts": tuple(
                connection.execute(
                    "SELECT id, name, api_key_env, enabled, weight, provider_id "
                    "FROM accounts ORDER BY id"
                )
            ),
        }
    finally:
        connection.close()


def _replace_explicit_paths(value: str, roots: Sequence[Path]) -> str:
    result = value
    for root in roots:
        for spelling in (str(root), root.as_posix()):
            result = result.replace(spelling, "<TEMP_ROOT>")
    return result


def normalize_observation(
    observation: Any, *, temp_roots: Sequence[Path] = ()
) -> dict[str, Any]:
    """Apply only the reviewed, type-specific normalization policy."""
    if isinstance(observation, ConfigObservation):
        result = observation.to_dict()
        result["stdout"] = _replace_explicit_paths(result["stdout"], temp_roots)
        result["stderr"] = _replace_explicit_paths(result["stderr"], temp_roots)
        result["argv"] = [
            _replace_explicit_paths(str(value), temp_roots) for value in result["argv"]
        ]
        result["args"] = [
            _replace_explicit_paths(str(value), temp_roots) for value in result["args"]
        ]
        result.pop("argv", None)
        result.pop("pid", None)
        result.pop("duration_ms", None)
        result.pop("executable", None)
        return result
    result = observation.to_dict()
    if isinstance(observation, CommandObservation):
        result["argv"] = [
            _replace_explicit_paths(value, temp_roots) for value in result["argv"]
        ]
        result["stdout"] = _replace_explicit_paths(result["stdout"], temp_roots)
        result["stderr"] = _replace_explicit_paths(result["stderr"], temp_roots)
        result["args"] = [
            _replace_explicit_paths(str(value), temp_roots) for value in result["args"]
        ]
        result.pop("argv", None)
        result.pop("pid", None)
        result.pop("duration_ms", None)
    elif isinstance(observation, HttpObservation):
        result["path"] = _replace_explicit_paths(result["path"], temp_roots)
        content_type = dict(observation.headers).get("content-type", "")
        if "json" in content_type and observation.body:
            with contextlib.suppress(json.JSONDecodeError):
                result["body"] = json.dumps(
                    json.loads(observation.body),
                    sort_keys=True,
                    separators=(",", ":"),
                )
        if "sse_frames" in result:
            result["sse_frames"] = result["sse_frames"]
    elif isinstance(observation, HtmlObservation):
        result["path"] = _replace_explicit_paths(result["path"], temp_roots)
    return result


def _comparison_value(value: dict[str, Any]) -> dict[str, Any]:
    """Remove only process identity after the distinct-launcher guard ran."""
    comparable = dict(value)
    comparable.pop("implementation", None)
    comparable.pop("executable", None)
    return comparable


def compare_observations(
    expected: Any,
    actual: Any,
    *,
    temp_roots: Sequence[Path] = (),
) -> None:
    """Compare normalized observations and produce a useful structural diff."""
    expected_value = _comparison_value(
        normalize_observation(expected, temp_roots=temp_roots)
    )
    actual_value = _comparison_value(
        normalize_observation(actual, temp_roots=temp_roots)
    )
    if expected_value == actual_value:
        return
    raise AssertionError(
        "differential observation mismatch:\n"
        f"expected={json.dumps(expected_value, sort_keys=True, indent=2)}\n"
        f"actual={json.dumps(actual_value, sort_keys=True, indent=2)}"
    )


@dataclass(frozen=True)
class StubResponse:
    status: int = 200
    body: bytes = b"{}"
    headers: tuple[tuple[str, str], ...] = (("content-type", "application/json"),)


@dataclass(frozen=True)
class StubRequest:
    method: str
    path: str
    body_length: int
    header_names: tuple[str, ...]


class StubHttpServer:
    """Local deterministic HTTP provider/server double.

    Request bytes are drained but never retained.  Routes are selected by
    method and path, making this suitable for future provider and API cases
    without real network traffic or provider cost.
    """

    def __init__(
        self,
        routes: Mapping[
            tuple[str, str], StubResponse | Callable[[StubRequest], StubResponse]
        ],
    ) -> None:
        self.routes = {
            (method.upper(), path): value for (method, path), value in routes.items()
        }
        self.requests: list[StubRequest] = []
        self._server: http.server.HTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("stub server is not running")
        return int(self._server.server_address[1])

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> Self:
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                self._handle()

            def do_POST(self) -> None:  # noqa: N802
                self._handle()

            def log_message(self, message_format: str, *args: Any) -> None:
                del message_format, args

            def _handle(self) -> None:
                length = int(self.headers.get("content-length", "0"))
                if length:
                    self.rfile.read(length)
                path = urlsplit(self.path).path
                request = StubRequest(
                    self.command,
                    path,
                    length,
                    tuple(sorted(key.casefold() for key in self.headers)),
                )
                owner.requests.append(request)
                response_value = owner.routes.get(
                    (self.command, path), StubResponse(404, b"{}")
                )
                response = (
                    response_value(request)
                    if callable(response_value)
                    else response_value
                )
                self.send_response(response.status)
                for key, value in response.headers:
                    self.send_header(key, value)
                self.send_header("content-length", str(len(response.body)))
                self.end_headers()
                self.wfile.write(response.body)

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def close(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._server = None
        self._thread = None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def wait_for_tcp(host: str, port: int, timeout: float = 10.0) -> None:
    """Wait for a TCP listener with a monotonic deadline."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.02)
    raise TimeoutError(f"listener {host}:{port} did not start within {timeout}s")


def allocate_tcp_port() -> int:
    """Reserve an ephemeral local port number for a short-lived test server."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


__all__ = [
    "CommandObservation",
    "ConfigObservation",
    "DatabaseObservation",
    "HtmlObservation",
    "HttpObservation",
    "Implementation",
    "ImplementationIdentity",
    "IsolatedEnvironment",
    "Launcher",
    "PythonLauncher",
    "RustLauncher",
    "SseFrame",
    "StaticObservation",
    "StubHttpServer",
    "StubRequest",
    "StubResponse",
    "assert_distinct_implementations",
    "allocate_tcp_port",
    "capture_config",
    "capture_database",
    "capture_html",
    "compare_observations",
    "isolated_environment",
    "normalize_observation",
    "observe_http",
    "wait_for_tcp",
]
