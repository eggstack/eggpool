"""Run the bounded M10 Q007 live-provider interoperability qualification.

The runner is opt-in and qualification-only.  It starts the supplied Rust
candidate with an isolated configuration and database, sends a fixed seven
request matrix, and writes only bounded semantic observations.  Provider
credentials are read from an environment variable (optionally populated from
an explicitly supplied dotenv file) and are never written to evidence.

Usage::

    uv run python scripts/qualification_live_provider.py \
        --binary rust/target/release/eggpool \
        --enable-live \
        --provider-key-env OPENCODE_GO_KEY_1 \
        --env-file .env \
        --output migration-rs/closure/qualification/007-run.json

The offline loopback mode is intended for deterministic regression tests and
never claims live-provider qualification::

    uv run python scripts/qualification_live_provider.py \
        --binary rust/target/debug/eggpool --offline-fake
"""

from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import os
import platform
import re
import signal
import socket
import sqlite3
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE = ROOT / "tests/migration_rs/fixtures/config/q007-live.toml"
DEFAULT_OUTPUT = ROOT / "migration-rs/closure/qualification/007-run.json"
MANIFEST_VERSION = "m10-q001.v1"
SCHEMA_VERSION = "m10-q007.v1"
SERVER_KEY = "q007-server-key"
FAKE_PROVIDER_KEY = "q007-provider-key"
OPENCODE_SESSION_HEADER = "x-opencode-session"
MAX_REQUESTS = 8
REQUEST_TIMEOUT = 60.0
MAX_DIAGNOSTIC_BYTES = 768

_MODEL_SURFACES: dict[str, str] = {
    "muse-spark-1.2-contributor": "openai_responses",
    "mimo-v2.5": "openai_chat_completions",
    "minimax-m3": "anthropic_messages",
}
_MODEL_PROTOCOLS: dict[str, str] = {
    "muse-spark-1.2-contributor": "openai",
    "mimo-v2.5": "openai",
    "minimax-m3": "anthropic",
}
_FAKE_MODELS = {
    "muse-spark-1.2-contributor": "openai_responses",
    "mimo-v2.5": "openai_chat_completions",
    "minimax-m3": "anthropic_messages",
}


class QualificationError(RuntimeError):
    """A mandatory Q007 observation failed."""


@dataclass(frozen=True)
class RequestCase:
    """One bounded request in the frozen Q007 matrix."""

    case_id: str
    model_id: str
    client_surface: str
    expected_surface: str
    streaming: bool
    cross_surface: bool = False


CASES: tuple[RequestCase, ...] = (
    RequestCase(
        "responses-finite",
        "muse-spark-1.2-contributor",
        "responses",
        "openai_responses",
        False,
    ),
    RequestCase(
        "chat-finite",
        "mimo-v2.5",
        "chat_completions",
        "openai_chat_completions",
        False,
    ),
    RequestCase(
        "messages-finite",
        "minimax-m3",
        "messages",
        "anthropic_messages",
        False,
    ),
    RequestCase(
        "messages-to-responses-finite",
        "muse-spark-1.2-contributor",
        "messages",
        "openai_responses",
        False,
        True,
    ),
    RequestCase(
        "responses-stream",
        "muse-spark-1.2-contributor",
        "responses",
        "openai_responses",
        True,
    ),
    RequestCase(
        "chat-stream",
        "mimo-v2.5",
        "chat_completions",
        "openai_chat_completions",
        True,
    ),
    RequestCase(
        "messages-stream",
        "minimax-m3",
        "messages",
        "anthropic_messages",
        True,
    ),
)


def bounded(value: str, secrets: Sequence[str] = ()) -> str:
    """Return compact diagnostics with credentials and proxy URLs removed."""
    text = " ".join(value.replace("\x00", " ").split())
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<redacted>")
    text = re.sub(r"(?i)bearer\s+\S+", "Bearer <redacted>", text)
    text = re.sub(r"(?i)(api[_-]?key|token|password)=\S+", r"\1=<redacted>", text)
    text = re.sub(r"(?i)(https?://)[^/@\s]+@", r"\1<redacted>@", text)
    text = re.sub(r"(?i)\bsk-[A-Za-z0-9._-]+", "<redacted>", text)
    return text[:MAX_DIAGNOSTIC_BYTES]


def validate_request_plan(cases: Sequence[RequestCase] = CASES) -> None:
    """Fail closed when the fixed request budget would be exceeded."""
    if not cases:
        raise QualificationError("Q007 request plan is empty")
    if len(cases) > MAX_REQUESTS:
        raise QualificationError(
            f"Q007 request plan has {len(cases)} requests; maximum is {MAX_REQUESTS}"
        )


def _port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse the small KEY=value dotenv subset used by EggPool deployments."""
    values: dict[str, str] = {}
    for number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise QualificationError(f"env file line {number} is not KEY=value")
        name, value = line.split("=", 1)
        name = name.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise QualificationError(f"env file line {number} has an invalid name")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[name] = value
    return values


def _render_config(
    fixture: Path,
    destination: Path,
    *,
    port: int,
    database: Path,
    upstream: str,
) -> None:
    content = fixture.read_text(encoding="utf-8")
    replacements = {
        "__Q007_PORT__": str(port),
        "__Q007_DATABASE__": str(database),
        "__Q007_UPSTREAM__": upstream,
    }
    for marker, value in replacements.items():
        content = content.replace(marker, value)
    if "__Q007_" in content:
        raise QualificationError("Q007 config fixture has unresolved placeholders")
    destination.write_text(content, encoding="utf-8")


def _payload(case: RequestCase) -> dict[str, Any]:
    if case.client_surface == "responses":
        return {
            "model": case.model_id,
            "input": "Say qualification ok.",
            "max_output_tokens": 16,
            "store": False,
            "stream": case.streaming,
        }
    if case.client_surface == "messages":
        return {
            "model": case.model_id,
            "messages": [{"role": "user", "content": "Say qualification ok."}],
            "max_tokens": 16,
            "stream": case.streaming,
        }
    return {
        "model": case.model_id,
        "messages": [{"role": "user", "content": "Say qualification ok."}],
        "max_tokens": 16,
        "stream": case.streaming,
    }


def _client_path(surface: str) -> str:
    return {
        "responses": "/v1/responses",
        "messages": "/v1/messages",
        "chat_completions": "/v1/chat/completions",
    }[surface]


def _expected_path(surface: str) -> str:
    return {
        "openai_responses": "/responses",
        "anthropic_messages": "/messages",
        "openai_chat_completions": "/chat/completions",
    }[surface]


def _usage_summary(body: bytes) -> dict[str, int | bool]:
    """Extract scalar usage facts without retaining a response body."""
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"reported": False}
    if not isinstance(value, dict):
        return {"reported": False}
    usage = value.get("usage")
    if not isinstance(usage, dict):
        return {"reported": False}
    result: dict[str, int | bool] = {"reported": True}
    for key in (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "input_tokens",
        "output_tokens",
    ):
        raw = usage.get(key)
        if isinstance(raw, int) and raw >= 0:
            result[key] = raw
    return result


def _error_summary(body: bytes) -> dict[str, str]:
    """Capture only an error type/message classification from a response."""
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict) or not isinstance(value.get("error"), dict):
        return {}
    error = value["error"]
    result: dict[str, str] = {}
    for key in ("type", "code", "message"):
        if isinstance(error.get(key), str):
            result[key] = bounded(error[key])
    return result


def _response_summary(body: bytes, *, streaming: bool) -> dict[str, Any]:
    markers = {
        "chat_completions": b"[DONE]",
        "responses": b"response.completed",
        "messages": b"message_stop",
    }
    if streaming:
        return {
            "body_bytes": len(body),
            "incremental_events": max(body.count(b"data:"), body.count(b"event:")),
            "terminal_evidence": any(marker in body for marker in markers.values()),
            "raw_body_retained": False,
            "usage": {"reported": b"usage" in body},
        }
    return {
        "body_bytes": len(body),
        "normalized_response": body.lstrip().startswith(b"{"),
        "raw_body_retained": False,
        "usage": _usage_summary(body),
    }


def _http(
    url: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = REQUEST_TIMEOUT,
) -> tuple[int, Mapping[str, str], bytes]:
    request = Request(url, data=body, headers=dict(headers or {}), method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, response.headers, response.read()
    except HTTPError as error:
        return error.code, error.headers, error.read(4096)
    except (OSError, URLError) as error:
        raise QualificationError(
            f"HTTP request failed: {type(error).__name__}"
        ) from error


def _wait_ready(process: subprocess.Popen[bytes], url: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise QualificationError(
                f"candidate exited during startup with code {process.returncode}"
            )
        try:
            status, _, _ = _http(url, timeout=1.0)
            if status == 200:
                return
        except QualificationError:
            pass
        time.sleep(0.1)
    raise QualificationError("candidate readiness timed out")


def _stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=10)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass


def _durable_snapshot(database: Path) -> dict[str, Any]:
    if not database.is_file():
        return {"database_exists": False}
    connection = sqlite3.connect(database)
    try:
        request_counts = connection.execute(
            "SELECT COUNT(*), "
            "COALESCE(SUM(status = 'completed'), 0), "
            "COALESCE(SUM(status = 'pending'), 0), "
            "COALESCE(SUM(input_tokens), 0), "
            "COALESCE(SUM(output_tokens), 0) FROM requests"
        ).fetchone()
        attempt_counts = connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(upstream_request_id IS NOT NULL), 0) "
            "FROM request_attempts"
        ).fetchone()
        active_reservations = connection.execute(
            "SELECT COUNT(*) FROM reservations WHERE status = 'active'"
        ).fetchone()[0]
        backoffs = connection.execute(
            "SELECT COUNT(*) FROM account_backoffs"
        ).fetchone()[0]
        return {
            "database_exists": True,
            "requests": int(request_counts[0]),
            "completed_requests": int(request_counts[1]),
            "pending_requests": int(request_counts[2]),
            "input_tokens": int(request_counts[3]),
            "output_tokens": int(request_counts[4]),
            "attempts": int(attempt_counts[0]),
            "attempts_with_upstream_request_id": int(attempt_counts[1]),
            "active_reservations": int(active_reservations),
            "account_backoffs": int(backoffs),
        }
    finally:
        connection.close()


class _FakeProviderHandler(http.server.BaseHTTPRequestHandler):
    provider: ClassVar[FakeProvider]

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.path.endswith("/models"):
            body = json.dumps(
                {"data": [{"id": model_id} for model_id in _FAKE_MODELS]}
            ).encode()
            self._respond(200, body, "application/json")
            return
        self._respond(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length", "0"))
        raw_body = self.rfile.read(length)
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        path = self.path
        surface = (
            "openai_responses"
            if path.endswith("/responses")
            else "anthropic_messages"
            if path.endswith("/messages")
            else "openai_chat_completions"
            if path.endswith("/chat/completions")
            else "unknown"
        )
        expected_key = self.headers.get("x-api-key") or self.headers.get(
            "authorization", ""
        ).removeprefix("Bearer ")
        self.provider.observations.append(
            {
                "method": self.command,
                "path": path,
                "surface": surface,
                "auth_shape": "api_key" if self.headers.get("x-api-key") else "bearer",
                "credential_valid": expected_key == FAKE_PROVIDER_KEY,
                "streaming": payload.get("stream") is True,
                "request_keys": sorted(payload),
                "input_kind": type(payload.get("input")).__name__,
                "header_names": sorted(self.headers.keys()),
                "model": payload.get("model"),
                "max_output_tokens": payload.get("max_output_tokens"),
                "store": payload.get("store"),
                "input_length": len(payload.get("input", ""))
                if isinstance(payload.get("input"), str)
                else None,
                "session_header_present": bool(
                    self.headers.get(OPENCODE_SESSION_HEADER)
                ),
            }
        )
        if expected_key != FAKE_PROVIDER_KEY:
            self._respond(
                401, b'{"error":{"type":"authentication_error"}}', "application/json"
            )
            return
        streaming = payload.get("stream") is True
        body = self.provider.body(surface, streaming)
        self._respond(
            200,
            body,
            "text/event-stream" if streaming else "application/json",
            request_id=f"q007-fake-{len(self.provider.observations)}",
        )

    def _respond(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        request_id: str | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        self.send_header("connection", "close")
        if request_id is not None:
            self.send_header("x-request-id", request_id)
        self.end_headers()
        self.wfile.write(body)


class FakeProvider:
    """Loopback provider with all Q007 wire families and no raw-body retention."""

    def __init__(self) -> None:
        self.observations: list[dict[str, Any]] = []
        self.server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), _FakeProviderHandler
        )
        _FakeProviderHandler.provider = self
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def body(self, surface: str, streaming: bool) -> bytes:
        if surface == "openai_responses":
            if streaming:
                return (
                    b"event: response.output_text.delta\n"
                    b'data: {"type":"response.output_text.delta","delta":"ok"}\n\n'
                    b"event: response.completed\n"
                    b'data: {"type":"response.completed","response":{"id":"q007-resp",'
                    b'"status":"completed","usage":{"input_tokens":1,'
                    b'"output_tokens":1,"total_tokens":2}}}\n\n'
                )
            return json.dumps(
                {
                    "id": "q007-resp",
                    "model": "q007-responses-model",
                    "status": "completed",
                    "error": None,
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": "ok"}],
                        }
                    ],
                    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                }
            ).encode()
        if surface == "anthropic_messages":
            if streaming:
                return (
                    b"event: message_start\n"
                    b'data: {"type":"message_start","message":{"id":"q007-msg"}}\n\n'
                    b"event: content_block_delta\n"
                    b'data: {"type":"content_block_delta","index":0,'
                    b'"delta":{"type":"text_delta","text":"ok"}}\n\n'
                    b"event: message_delta\n"
                    b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
                    b'"usage":{"input_tokens":1,"output_tokens":1}}\n\n'
                    b"event: message_stop\n"
                    b'data: {"type":"message_stop"}\n\n'
                )
            return json.dumps(
                {
                    "id": "q007-msg",
                    "model": "q007-anthropic-model",
                    "content": [{"type": "text", "text": "ok"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                }
            ).encode()
        if streaming:
            return (
                b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
                b'data: {"choices":[],"usage":{"prompt_tokens":1,'
                b'"completion_tokens":1,"total_tokens":2}}\n\n'
                b"data: [DONE]\n\n"
            )
        return json.dumps(
            {
                "id": "q007-chat",
                "model": "q007-chat-model",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            }
        ).encode()

    def __enter__(self) -> FakeProvider:
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def _case_record(
    case: RequestCase,
    *,
    status: int,
    headers: Mapping[str, str],
    body: bytes,
    request_id: bool | None,
    upstream: Mapping[str, Any] | None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "id": case.case_id,
        "model_id": case.model_id,
        "client_surface": case.client_surface,
        "expected_upstream_surface": case.expected_surface,
        "expected_upstream_path": _expected_path(case.expected_surface),
        "streaming": case.streaming,
        "cross_surface": case.cross_surface,
        "status": "pass" if status == 200 else "fail",
        "http_status": status,
        "request_id_present": request_id,
        "response": _response_summary(body, streaming=case.streaming),
        "content_type": headers.get("content-type", "").split(";", 1)[0],
        "raw_response_body_retained": False,
    }
    if status != 200:
        record["error"] = _error_summary(body)
    if upstream is not None:
        record["upstream_observation"] = dict(upstream)
    return record


def _latest_request_snapshot(database: Path) -> tuple[bool | None, dict[str, Any]]:
    connection = sqlite3.connect(database)
    try:
        row = connection.execute(
            "SELECT id, status, upstream_request_id, input_tokens, output_tokens "
            "FROM requests ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None, {}
        attempt = connection.execute(
            "SELECT status_code, error_class FROM request_attempts "
            "WHERE request_id = ? ORDER BY id DESC LIMIT 1",
            (row[0],),
        ).fetchone()
        snapshot: dict[str, Any] = {
            "durable_status": row[1],
            "input_tokens": int(row[3] or 0),
            "output_tokens": int(row[4] or 0),
        }
        if attempt is not None:
            snapshot["upstream_status"] = attempt[0]
            snapshot["attempt_error_class"] = attempt[1]
        return row[2] is not None, snapshot
    finally:
        connection.close()


def run_qualification(
    *,
    binary: Path,
    config_fixture: Path = DEFAULT_FIXTURE,
    output: Path | None = None,
    live: bool = False,
    provider_key_env: str = "EGGPOOL_E2E_OPENCODE_GO_API_KEY",
    env_file: Path | None = None,
    timeout: float = REQUEST_TIMEOUT,
) -> dict[str, Any]:
    """Run Q007 against a real provider or the deterministic fake provider."""
    validate_request_plan()
    if not live and provider_key_env == "":
        raise QualificationError("provider key environment name must not be empty")

    loaded_env: dict[str, str] = {}
    if env_file is not None:
        loaded_env = _parse_env_file(env_file)
    provider_key = os.environ.get(provider_key_env) or loaded_env.get(provider_key_env)
    if live and not provider_key:
        return {
            "schema": SCHEMA_VERSION,
            "manifest": MANIFEST_VERSION,
            "plan": "Q007",
            "status": "blocked",
            "reason": (
                f"credential environment variable {provider_key_env!r} is unavailable"
            ),
            "budget": {
                "maximum_requests": MAX_REQUESTS,
                "planned_requests": len(CASES),
            },
            "cells": [],
            "credentials": {"source": "environment", "name": provider_key_env},
        }
    if not binary.is_file():
        raise QualificationError("candidate binary does not exist")

    environment = {
        "os": platform.platform(aliased=True),
        "system": platform.system(),
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "rust": bounded(
            subprocess.run(
                ["rustc", "--version"], capture_output=True, text=True, check=False
            ).stdout
        ),
        "candidate_sha256": _sha256(binary),
        "transport": "direct",
        "provider": "OpenCode Go" if live else "loopback fake provider",
    }
    report: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "manifest": MANIFEST_VERSION,
        "plan": "Q007",
        "status": "fail",
        "environment": environment,
        "budget": {
            "maximum_requests": MAX_REQUESTS,
            "planned_requests": len(CASES),
            "max_output_tokens": 16,
            "automatic_retry_policy": "EggPool ordinary bounded policy only",
        },
        "planned_matrix": [
            {
                "id": case.case_id,
                "model_id": case.model_id,
                "client_surface": case.client_surface,
                "upstream_surface": case.expected_surface,
                "streaming": case.streaming,
                "cross_surface": case.cross_surface,
            }
            for case in CASES
        ],
        "cells": [],
        "proxy": {"status": "not-applicable", "reason": "no Q001 live proxy cell"},
        "credentials": {
            "source": "environment variable",
            "name": provider_key_env,
            "values_written_to_evidence": False,
        },
        "redaction_review": {
            "raw_response_bodies_persisted": False,
            "credentials_written_to_evidence": False,
            "proxy_credentials_written_to_evidence": False,
        },
    }
    with tempfile.TemporaryDirectory(prefix="eggpool-q007-") as temporary:
        root = Path(temporary)
        database = root / "usage.sqlite3"
        config = root / "config.toml"
        process: subprocess.Popen[bytes] | None = None
        fake_provider: FakeProvider | None = None
        output_stream: Any | None = None
        error_stream: Any | None = None
        stdout_path = root / "candidate.stdout"
        stderr_path = root / "candidate.stderr"
        started = time.monotonic()
        try:
            upstream = ""
            if live:
                upstream = "https://opencode.ai/zen/go/v1"
            else:
                fake_provider = FakeProvider()
                fake_provider.__enter__()
                upstream = fake_provider.base_url
            _render_config(
                config_fixture,
                config,
                port=_port(),
                database=database,
                upstream=upstream,
            )
            child_env = os.environ.copy()
            child_env.update(loaded_env)
            child_env["Q007_PROVIDER_API_KEY"] = (
                provider_key if live else FAKE_PROVIDER_KEY
            )
            child_env["SERVER_API_KEY"] = SERVER_KEY
            child_env.setdefault("RUST_LOG", "debug")
            output_stream = stdout_path.open("wb")
            error_stream = stderr_path.open("wb")
            process = subprocess.Popen(
                [str(binary.resolve()), "--config", str(config), "serve", "--verbose"],
                cwd=ROOT,
                env=child_env,
                stdin=subprocess.DEVNULL,
                stdout=output_stream,
                stderr=error_stream,
                start_new_session=True,
            )
            port_text = config.read_text(encoding="utf-8")
            port_match = re.search(r"^port = (\d+)$", port_text, re.MULTILINE)
            if port_match is None:
                raise QualificationError("Q007 config did not contain a server port")
            port = int(port_match.group(1))
            base_url = f"http://127.0.0.1:{port}"
            _wait_ready(process, f"{base_url}/v1/healthz", timeout)
            status, _, models_body = _http(
                f"{base_url}/v1/models",
                headers={"Authorization": f"Bearer {SERVER_KEY}"},
            )
            if status != 200:
                raise QualificationError(f"model catalog returned HTTP {status}")
            try:
                model_ids = {
                    item["id"]
                    for item in json.loads(models_body.decode("utf-8")).get("data", [])
                    if isinstance(item, dict) and isinstance(item.get("id"), str)
                }
            except (UnicodeDecodeError, AttributeError, json.JSONDecodeError):
                model_ids = set()
            expected_models = set(_FAKE_MODELS) if not live else set(_MODEL_SURFACES)
            if not expected_models <= model_ids:
                raise QualificationError(
                    "model catalog omitted a planned Q007 model: "
                    f"resolved={sorted(model_ids)}"
                )
            report["catalog"] = {
                "status": "pass",
                "expected_models": sorted(expected_models),
                "resolved_models": sorted(expected_models & model_ids),
            }
            for case in CASES:
                request_body = json.dumps(_payload(case)).encode()
                headers = {
                    "Authorization": f"Bearer {SERVER_KEY}",
                    "Content-Type": "application/json",
                    OPENCODE_SESSION_HEADER: f"q007-{case.case_id}",
                }
                status, response_headers, response_body = _http(
                    f"{base_url}{_client_path(case.client_surface)}",
                    method="POST",
                    body=request_body,
                    headers=headers,
                    timeout=timeout,
                )
                request_id, durable = _latest_request_snapshot(database)
                upstream = None
                if fake_provider is not None and fake_provider.observations:
                    upstream = fake_provider.observations[-1]
                record = _case_record(
                    case,
                    status=status,
                    headers=response_headers,
                    body=response_body,
                    request_id=request_id,
                    upstream=upstream,
                )
                record.update(durable)
                report["cells"].append(record)
                if status != 200:
                    raise QualificationError(f"{case.case_id} returned HTTP {status}")
                if case.streaming and not record["response"]["terminal_evidence"]:
                    raise QualificationError(f"{case.case_id} lacks terminal evidence")
            runtime_status, _, _ = _http(
                f"{base_url}/api/stats/runtime",
                headers={"Authorization": f"Bearer {SERVER_KEY}"},
            )
            report["runtime_status_http"] = runtime_status
        except (OSError, QualificationError) as error:
            report["reason"] = bounded(str(error), (provider_key or "",))
        finally:
            if process is not None:
                _stop(process)
            if output_stream is not None:
                output_stream.close()
            if error_stream is not None:
                error_stream.close()
            if stdout_path.exists():
                report["candidate_stdout"] = bounded(
                    stdout_path.read_text(encoding="utf-8", errors="replace"),
                    (provider_key or "",),
                )
            if stderr_path.exists():
                report["candidate_stderr"] = bounded(
                    stderr_path.read_text(encoding="utf-8", errors="replace"),
                    (provider_key or "",),
                )
            if fake_provider is not None:
                fake_provider.__exit__(None, None, None)
        report["duration_ms"] = round((time.monotonic() - started) * 1000)
        report["durable"] = _durable_snapshot(database)
        if report.get("durable", {}).get("database_exists"):
            if report["durable"].get("pending_requests") != 0:
                report["reason"] = "durable pending requests remain after shutdown"
            if report["durable"].get("active_reservations") != 0:
                report["reason"] = "active reservations remain after shutdown"
        if "reason" not in report and len(report["cells"]) == len(CASES):
            report["status"] = "pass"
        report["offline_fake"] = not live
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--config-fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--provider-key-env", default="EGGPOOL_E2E_OPENCODE_GO_API_KEY")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--enable-live", action="store_true")
    parser.add_argument("--offline-fake", action="store_true")
    parser.add_argument("--timeout", type=float, default=REQUEST_TIMEOUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.enable_live == args.offline_fake:
        raise SystemExit("Q007 requires exactly one of --enable-live or --offline-fake")
    print(
        json.dumps(
            {
                "plan": "Q007",
                "status": "planned",
                "requests": len(CASES),
                "models": sorted({case.model_id for case in CASES}),
                "surfaces": sorted({case.expected_surface for case in CASES}),
                "streaming_requests": sum(case.streaming for case in CASES),
            }
        )
    )
    try:
        report = run_qualification(
            binary=args.binary,
            config_fixture=args.config_fixture,
            output=args.output,
            live=args.enable_live,
            provider_key_env=args.provider_key_env,
            env_file=args.env_file,
            timeout=args.timeout,
        )
    except (OSError, QualificationError) as error:
        report = {
            "schema": SCHEMA_VERSION,
            "manifest": MANIFEST_VERSION,
            "plan": "Q007",
            "status": "fail",
            "reason": bounded(str(error)),
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(report, indent=2, sort_keys=True) + "\n"
    args.output.write_text(serialized, encoding="utf-8")
    print(json.dumps({"status": report.get("status"), "output": str(args.output)}))
    if report.get("status") == "pass":
        return 0
    if report.get("status") == "blocked":
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
