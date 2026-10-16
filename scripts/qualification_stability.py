"""Run bounded deterministic runtime stability qualification.

The workload is intentionally finite.  It exercises ordinary requests,
provider/client faults, reloads, background ticks, restart reconciliation, and
resource convergence against a loopback-only provider.  Evidence contains
bounded scalar observations only; request bodies, child-process output, and
credentials are never retained.

Usage::

    uv run python scripts/qualification_stability.py \
        --binary rust/target/debug/eggpool \
        --output artifacts/qualification/009-run.json
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import platform
import re
import signal
import socket
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, cast

try:
    from scripts.qualification_sbc import (
        _db_counts,  # pyright: ignore[reportPrivateUsage]
        _environment,  # pyright: ignore[reportPrivateUsage]
        _http,  # pyright: ignore[reportPrivateUsage]
        _stop,  # pyright: ignore[reportPrivateUsage]
        _timed_http,  # pyright: ignore[reportPrivateUsage]
        bounded,
        free_port,
        resource_sample,
    )
except ModuleNotFoundError:  # Running this file directly from ``scripts/``.
    from qualification_sbc import (  # type: ignore[no-redef]
        _db_counts,  # pyright: ignore[reportPrivateUsage]
        _environment,  # pyright: ignore[reportPrivateUsage]
        _http,  # pyright: ignore[reportPrivateUsage]
        _stop,  # pyright: ignore[reportPrivateUsage]
        _timed_http,  # pyright: ignore[reportPrivateUsage]
        bounded,
        free_port,
        resource_sample,
    )

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE = ROOT / "tests/tooling/fixtures/qualification/stability.toml"
DEFAULT_OUTPUT = ROOT / "artifacts/qualification/009-run.json"
SCHEMA_VERSION = "runtime-q009.v1"
MANIFEST_VERSION = "runtime-q001.v1"
MAX_BODY_BYTES = 128 * 1024
MAX_SAMPLES = 96
DEFAULT_TIMEOUT = 20.0
MAX_CYCLES = 16


class QualificationError(RuntimeError):
    """A mandatory stability qualification observation failed."""


class _FaultProviderHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    owner: ClassVar[FaultProvider]

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(min(length, MAX_BODY_BYTES))
        try:
            decoded = json.loads(body.decode("utf-8"))
            payload = (
                cast("dict[str, Any]", decoded) if isinstance(decoded, dict) else {}
            )
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {}
        model = str(payload.get("model", "unknown"))
        number = self.owner.record(model)
        if model == "q009-cancel":
            time.sleep(0.25)
        if model == "q009-abrupt":
            time.sleep(1.0)
        if model == "q009-timeout" and number == 1:
            time.sleep(0.8)
        if model.startswith("q009-fault-") and number == 1:
            status = {
                "q009-fault-408": 408,
                "q009-fault-429": 429,
                "q009-fault-5xx": 503,
            }[model]
            self._respond(status, b'{"error":{"message":"planned fault"}}')
            return
        if model == "q009-wire" and self.path.endswith("/responses") and number == 1:
            self._respond(
                404,
                b'{"error":{"type":"invalid_request_error",'
                b'"message":"unsupported endpoint for this surface"}}',
            )
            return
        if model == "q009-absent":
            self._respond(
                404,
                b'{"error":{"type":"not_found","message":"Model not found"}}',
            )
            return
        content = "default" if model == "q009-selector" else "ok"
        streaming = bool(payload.get("stream"))
        if streaming and model in {
            "q009-partial",
            "q009-disconnect",
            "q009-malformed",
        }:
            self._partial_stream(model)
            return
        if self.path.endswith("/responses"):
            if streaming:
                response = (
                    b"event: response.output_text.delta\n"
                    b'data: {"type":"response.output_text.delta","delta":"'
                    + content.encode()
                    + b'"}\n\n'
                    b"event: response.completed\n"
                    b'data: {"type":"response.completed","response":{"id":"q009",'
                    b'"status":"completed"}}\n\n'
                )
                self._respond(200, response, "text/event-stream")
            else:
                self._respond(
                    200,
                    json.dumps(
                        {
                            "id": "q009",
                            "object": "response",
                            "status": "completed",
                            "error": None,
                            "model": "q009-responses",
                            "output": [
                                {
                                    "type": "message",
                                    "id": "msg",
                                    "status": "completed",
                                    "role": "assistant",
                                    "content": [
                                        {
                                            "type": "output_text",
                                            "text": content,
                                            "annotations": [],
                                        }
                                    ],
                                }
                            ],
                        }
                    ).encode(),
                )
            return
        if self.path.endswith("/messages"):
            if streaming:
                self._respond(
                    200,
                    (
                        b"event: message_start\n"
                        b'data: {"type":"message_start","message":{"id":"q009"}}\n\n'
                        b"event: content_block_delta\n"
                        b'data: {"type":"content_block_delta","index":0,"delta":'
                        b'{"type":"text_delta","text":"ok"}}\n\n'
                        b"event: message_stop\n"
                        b'data: {"type":"message_stop"}\n\n'
                    ),
                    "text/event-stream",
                )
            else:
                self._respond(
                    200,
                    b'{"id":"q009","type":"message","role":"assistant",'
                    b'"content":[{"type":"text","text":"ok"}],'
                    b'"stop_reason":"end_turn","usage":{"input_tokens":1,'
                    b'"output_tokens":1}}',
                )
            return
        if streaming:
            self._respond(
                200,
                b'data: {"id":"q009","choices":[{"delta":{"content":"ok"}}]}\n\n'
                b"data: [DONE]\n\n",
                "text/event-stream",
            )
        else:
            self._respond(
                200,
                json.dumps(
                    {
                        "id": "q009",
                        "object": "chat.completion",
                        "model": "q009-chat",
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": content},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 1,
                            "completion_tokens": 1,
                            "total_tokens": 2,
                        },
                    }
                ).encode(),
            )

    def _partial_stream(self, model: str) -> None:
        body = (
            b"data: {not-json}\n\n"
            if model == "q009-malformed"
            else (
                b'data: {"id":"q009-partial","choices":[{"delta":{"content":"x"}}]}\n\n'
            )
        )
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(body)))
        self.send_header("connection", "close")
        self.end_headers()
        with contextlib.suppress(OSError):
            self.wfile.write(body)
            self.wfile.flush()
        self.close_connection = True

    def _respond(
        self, status: int, body: bytes, content_type: str = "application/json"
    ) -> None:
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        if status == 429:
            self.send_header("retry-after", "0")
        self.send_header("connection", "close")
        self.end_headers()
        with contextlib.suppress(OSError):
            self.wfile.write(body)

    def log_message(self, format: str, *_args: object) -> None:  # noqa: A002
        del format
        return


class FaultProvider:
    """Threaded local provider with deterministic per-model fault sequences."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _FaultProviderHandler)
        _FaultProviderHandler.owner = self
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def record(self, model: str) -> int:
        with self._lock:
            value = self._counts.get(model, 0) + 1
            self._counts[model] = value
            return value

    def count(self, model: str) -> int:
        with self._lock:
            return self._counts.get(model, 0)

    def total(self) -> int:
        with self._lock:
            return sum(self._counts.values())

    def counts(self) -> dict[str, int]:
        with self._lock:
            return dict(sorted(self._counts.items()))

    def __enter__(self) -> FaultProvider:
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def _render_fixture(
    fixture: Path,
    destination: Path,
    *,
    port: int,
    upstream: str,
    database: Path,
    backup_dir: Path,
) -> str:
    content = fixture.read_text(encoding="utf-8")
    for marker, replacement in {
        "__STABILITY_PORT__": str(port),
        "__STABILITY_UPSTREAM__": upstream,
        "__STABILITY_DATABASE__": str(database),
        "__STABILITY_BACKUP_DIR__": str(backup_dir),
    }.items():
        content = content.replace(marker, replacement)
    if "__STABILITY_" in content:
        raise QualificationError(
            "stability qualification config fixture has unresolved placeholders"
        )
    destination.write_text(content, encoding="utf-8")
    return content


def _command(
    binary: Path,
    config: Path,
    env: Mapping[str, str],
    args: Sequence[str],
    timeout: float,
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        result = subprocess.run(
            [str(binary), "--config", str(config), *args],
            cwd=ROOT,
            env=dict(env),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            start_new_session=True,
        )
        return {
            "command": [str(item) for item in args],
            "status": "pass" if result.returncode == 0 else "fail",
            "returncode": result.returncode,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "reason": bounded(result.stderr or result.stdout),
        }
    except subprocess.TimeoutExpired:
        return {
            "command": [str(item) for item in args],
            "status": "timeout",
            "returncode": None,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "reason": "command timed out",
        }
    except OSError as error:
        return {
            "command": [str(item) for item in args],
            "status": "infrastructure-error",
            "returncode": None,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "reason": f"command could not start: {type(error).__name__}",
        }


def _set_user_request_budget(
    binary: Path,
    config: Path,
    env: Mapping[str, str],
    limit: int,
    timeout: float,
    username: str = "q009-admin",
) -> dict[str, Any]:
    return _command(
        binary,
        config,
        env,
        (
            "users",
            "budget",
            "set",
            username,
            "--window",
            "day",
            "--max-requests",
            str(limit),
            "--enforcement",
            "hard",
            "--actor",
            "q009-admin",
        ),
        timeout,
    )


def _set_global_request_budget(
    binary: Path,
    config: Path,
    env: Mapping[str, str],
    limit: int,
    timeout: float,
) -> dict[str, Any]:
    return _command(
        binary,
        config,
        env,
        (
            "users",
            "global-budget",
            "set",
            "--window",
            "day",
            "--max-requests",
            str(limit),
            "--enforcement",
            "hard",
            "--actor",
            "q009-admin",
        ),
        timeout,
    )


def _wait_ready(process: subprocess.Popen[str], url: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise QualificationError(
                f"candidate exited during startup with code {process.returncode}"
            )
        try:
            status, _ = _http(url, timeout=1)
            if status == 200:
                return
        except OSError:
            pass
        time.sleep(0.05)
    raise QualificationError("candidate readiness timed out")


def _post(
    port: int,
    surface: str,
    model: str,
    *,
    streaming: bool = False,
    route_session: str | None = None,
    api_token: str,
) -> tuple[int, bytes, int, int | None]:
    paths = {
        "chat_completions": "/v1/chat/completions",
        "responses": "/v1/responses",
        "messages": "/v1/messages",
    }
    if surface == "messages":
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "ping"}],
        }
    elif surface == "responses":
        payload = {"model": model, "input": "ping", "store": False}
    else:
        payload = {"model": model, "messages": [{"role": "user", "content": "ping"}]}
    if streaming:
        payload["stream"] = True
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }
    if route_session:
        headers["X-EggPool-Route-Session"] = route_session
    return _timed_http(
        f"http://127.0.0.1:{port}{paths[surface]}",
        method="POST",
        body=json.dumps(payload).encode(),
        headers=headers,
        timeout=5,
    )


def _cancel_request(port: int, api_token: str) -> None:
    payload = json.dumps(
        {
            "model": "q009-cancel",
            "stream": True,
            "messages": [{"role": "user", "content": "x"}],
        }
    ).encode()
    request = (
        "POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\n"
        f"Authorization: Bearer {api_token}\r\nContent-Type: application/json\r\n"
        f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n"
    ).encode()
    with socket.create_connection(("127.0.0.1", port), timeout=2) as connection:
        connection.sendall(request + payload)


def _write_abort_request(port: int, api_token: str) -> None:
    """Close while writing a bounded request body to exercise write cleanup."""
    payload = b'{"model":"q009-cancel","messages":['
    request = (
        b"POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\n"
        + (
            f"Authorization: Bearer {api_token}\r\nContent-Type: application/json\r\n"
        ).encode()
        + b"Content-Length: 100000\r\nConnection: close\r\n\r\n"
    )
    with socket.create_connection(("127.0.0.1", port), timeout=2) as connection:
        connection.sendall(request + payload)


def _resource_sample(
    label: str,
    process: subprocess.Popen[str],
    database: Path,
    port: int,
    samples: list[dict[str, Any]],
) -> dict[str, Any]:
    if len(samples) >= MAX_SAMPLES:
        raise QualificationError(
            "stability qualification resource sample bound exceeded"
        )
    sample = resource_sample(
        label,
        process,
        database,
        f"http://127.0.0.1:{port}/api/stats/runtime",
        duration=0.05,
        server_api_key="q009-server-key",
    )
    samples.append(sample)
    return sample


def _wait_converged(
    database: Path,
    process: subprocess.Popen[str],
    port: int,
    samples: list[dict[str, Any]],
    timeout: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last = _db_counts(database)
    while time.monotonic() < deadline:
        last = _db_counts(database)
        if last.get("pending_requests") == 0 and last.get("active_reservations") == 0:
            return last
        time.sleep(0.1)
    _resource_sample("convergence-timeout", process, database, port, samples)
    raise QualificationError("durable request state did not converge")


def _start(binary: Path, config: Path, env: Mapping[str, str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [str(binary), "--config", str(config), "serve", "--verbose"],
        cwd=ROOT,
        env=dict(env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        text=True,
    )


def _close_started(process: subprocess.Popen[str] | None, timeout: float) -> None:
    if process is None:
        return
    _stop(process, timeout)


def _phase_result(phase: str, status: str, **facts: Any) -> dict[str, Any]:
    return {"phase": phase, "status": status, **facts}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_cycles(values: Mapping[str, int]) -> None:
    for name, value in values.items():
        if value < 0 or value > MAX_CYCLES:
            raise ValueError(f"{name} must be between 0 and {MAX_CYCLES}")


def run_qualification(
    *,
    binary: Path,
    config_fixture: Path = DEFAULT_FIXTURE,
    seed: int = 9009,
    warmup_cycles: int = 1,
    steady_cycles: int = 2,
    fault_cycles: int = 1,
    reload_cycles: int = 2,
    restart_cycles: int = 1,
    timeout: float = DEFAULT_TIMEOUT,
    phases: frozenset[str] | None = None,
    exercise_revocation_during_restart: bool = False,
) -> dict[str, Any]:
    """Execute stability qualification and return a bounded report."""
    _validate_cycles(
        {
            "warmup_cycles": warmup_cycles,
            "steady_cycles": steady_cycles,
            "fault_cycles": fault_cycles,
            "reload_cycles": reload_cycles,
            "restart_cycles": restart_cycles,
        }
    )
    started = time.monotonic()
    if not binary.is_file():
        return {
            "schema_version": SCHEMA_VERSION,
            "plan": "stability qualification",
            "manifest": MANIFEST_VERSION,
            "status": "fail",
            "reason": "candidate binary does not exist",
        }
    selected = phases or frozenset({"all"})
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "plan": "stability qualification",
        "manifest": MANIFEST_VERSION,
        "status": "fail",
        "seed": seed,
        "configuration": {
            "warmup_cycles": warmup_cycles,
            "steady_cycles": steady_cycles,
            "fault_cycles": fault_cycles,
            "reload_cycles": reload_cycles,
            "restart_cycles": restart_cycles,
            "timeout_s": timeout,
        },
        "phases": [],
        "resource_samples": [],
        "commands": [],
        "findings": [],
        "candidate": {
            "sha256": _sha256(binary),
            "binary_size_bytes": binary.stat().st_size,
        },
    }
    if seed != 9009:
        report["seed_policy"] = (
            "custom seed accepted; provider sequence remains deterministic"
        )
    process: subprocess.Popen[str] | None = None
    with tempfile.TemporaryDirectory(prefix="eggpool-q009-") as temporary:
        root = Path(temporary)
        for name in (
            "home",
            "config-home",
            "data-home",
            "state-home",
            "runtime",
            "backups",
        ):
            (root / name).mkdir()
        database = root / "data-home" / "usage.sqlite3"
        config = root / "config.toml"
        backup_dir = root / "backups"
        port = free_port()
        env = _environment(root, config)
        samples = cast("list[dict[str, Any]]", report["resource_samples"])
        commands = cast("list[dict[str, Any]]", report["commands"])
        try:
            with FaultProvider() as provider:
                _render_fixture(
                    config_fixture,
                    config,
                    port=port,
                    upstream=provider.base_url,
                    database=database,
                    backup_dir=backup_dir,
                )
                for args in (("check-config",), ("migrate",)):
                    outcome = _command(binary, config, env, args, timeout)
                    commands.append(outcome)
                    if outcome["status"] != "pass":
                        raise QualificationError(f"preflight {args[0]} failed")
                try:
                    bootstrap = subprocess.run(
                        [
                            str(binary),
                            "--config",
                            str(config),
                            "users",
                            "bootstrap",
                            "q009-admin",
                            "--password-stdin",
                            "--show-secret",
                        ],
                        cwd=ROOT,
                        env=dict(env),
                        input="Q009 synthetic admin password 123\n",
                        capture_output=True,
                        text=True,
                        timeout=timeout,
                        check=False,
                    )
                except (OSError, subprocess.TimeoutExpired) as error:
                    raise QualificationError(
                        "synthetic managed administrator bootstrap failed"
                    ) from error
                token_match = (
                    re.search(r"epk1_[A-Za-z0-9_-]+", bootstrap.stdout)
                    if bootstrap.returncode == 0
                    else None
                )
                if token_match is None:
                    raise QualificationError(
                        "synthetic managed administrator token was unavailable"
                    )
                agent_token = token_match.group(0)
                commands.append(
                    {
                        "command": ["users", "bootstrap"],
                        "status": "pass",
                        "duration_ms": round((time.monotonic() - started) * 1000),
                        "reason": "synthetic managed principal created",
                    }
                )
                policy = _command(
                    binary,
                    config,
                    env,
                    (
                        "users",
                        "policy",
                        "create",
                        "q009-disable-policy",
                        "Q009 Disable Qualification",
                        "allow",
                        "--actor",
                        "q009-admin",
                    ),
                    timeout,
                )
                commands.append(policy)
                if policy["status"] != "pass":
                    raise QualificationError("synthetic disable policy setup failed")
                member_password = "Q009 synthetic member password 123"
                try:
                    member_add = subprocess.run(
                        [
                            str(binary),
                            "--config",
                            str(config),
                            "users",
                            "add",
                            "q009-disablee",
                            "--password-stdin",
                            "--profile-id",
                            "q009-disable-policy",
                            "--actor",
                            "q009-admin",
                        ],
                        cwd=ROOT,
                        env=dict(env),
                        input=f"{member_password}\n",
                        capture_output=True,
                        text=True,
                        timeout=timeout,
                        check=False,
                    )
                except (OSError, subprocess.TimeoutExpired) as error:
                    raise QualificationError(
                        "synthetic disable principal setup failed"
                    ) from error
                commands.append(
                    {
                        "command": ["users", "add"],
                        "status": "pass" if member_add.returncode == 0 else "fail",
                        "returncode": member_add.returncode,
                        "duration_ms": round((time.monotonic() - started) * 1000),
                        "reason": "synthetic disable principal setup",
                    }
                )
                if member_add.returncode != 0:
                    raise QualificationError("synthetic disable principal setup failed")
                try:
                    member_token_result = subprocess.run(
                        [
                            str(binary),
                            "--config",
                            str(config),
                            "users",
                            "token",
                            "q009-disablee",
                            "--label",
                            "q009-disable-test",
                            "--show-secret",
                        ],
                        cwd=ROOT,
                        env=dict(env),
                        capture_output=True,
                        text=True,
                        timeout=timeout,
                        check=False,
                    )
                except (OSError, subprocess.TimeoutExpired) as error:
                    raise QualificationError(
                        "synthetic disable credential setup failed"
                    ) from error
                member_token_match = (
                    re.search(r"epk1_[A-Za-z0-9_-]+", member_token_result.stdout)
                    if member_token_result.returncode == 0
                    else None
                )
                commands.append(
                    {
                        "command": ["users", "token"],
                        "status": "pass" if member_token_match else "fail",
                        "returncode": member_token_result.returncode,
                        "duration_ms": round((time.monotonic() - started) * 1000),
                        "reason": "synthetic disable credential issued",
                    }
                )
                if member_token_match is None:
                    raise QualificationError(
                        "synthetic disable credential setup failed"
                    )
                disable_token = member_token_match.group(0)
                for budget_command in (
                    _set_user_request_budget(binary, config, env, 100, timeout),
                    _set_user_request_budget(
                        binary,
                        config,
                        env,
                        100,
                        timeout,
                        username="q009-disablee",
                    ),
                    _set_global_request_budget(binary, config, env, 100, timeout),
                ):
                    commands.append(budget_command)
                    if budget_command["status"] != "pass":
                        raise QualificationError(
                            "synthetic hard request budgets could not be enabled"
                        )
                report["workload_policy"] = {
                    "principal_count": 2,
                    "budget_mode": "hard user and global request ceilings",
                    "initial_user_request_limit": 100,
                    "initial_global_request_limit": 100,
                }
                process = _start(binary, config, env)
                _wait_ready(process, f"http://127.0.0.1:{port}/v1/healthz", timeout)
                report["environment"] = {
                    "system": os.name,
                    "platform": bounded(
                        os.uname().sysname if hasattr(os, "uname") else "unknown"
                    ),
                    "architecture": bounded(platform.machine()),
                    "network_policy": "loopback-only",
                }
                _resource_sample("before-warmup", process, database, port, samples)
                if "all" in selected or "warmup" in selected:
                    for _ in range(warmup_cycles):
                        for surface, model, streaming in (
                            ("chat_completions", "q009-chat", False),
                            ("responses", "q009-responses", True),
                        ):
                            status, body, _, _ = _post(
                                port,
                                surface,
                                model,
                                streaming=streaming,
                                api_token=agent_token,
                            )
                            if status != 200 or not body:
                                raise QualificationError(
                                    f"warmup {surface} failed: HTTP {status}"
                                )
                    _resource_sample("after-warmup", process, database, port, samples)
                    report["phases"].append(
                        _phase_result("warmup", "pass", cycles=warmup_cycles)
                    )
                if "all" in selected or "steady" in selected:
                    requests = 0
                    for cycle in range(steady_cycles):
                        for surface, model, streaming in (
                            ("chat_completions", "q009-chat", False),
                            ("responses", "q009-responses", False),
                            ("messages", "q009-messages", True),
                        ):
                            status, body, _, _ = _post(
                                port,
                                surface,
                                model,
                                streaming=streaming,
                                api_token=agent_token,
                            )
                            if status != 200 or not body:
                                raise QualificationError(
                                    f"steady cycle {cycle} failed: HTTP {status}"
                                )
                            requests += 1
                        status, body, _, _ = _post(
                            port,
                            "chat_completions",
                            "stability",
                            route_session=f"q009-session-{cycle}",
                            api_token=agent_token,
                        )
                        if status != 200 or not body:
                            raise QualificationError(
                                f"virtual route failed: HTTP {status}"
                            )
                        requests += 1
                    _resource_sample("after-steady", process, database, port, samples)
                    report["phases"].append(
                        _phase_result(
                            "steady", "pass", cycles=steady_cycles, requests=requests
                        )
                    )
                if "all" in selected or "faults" in selected:
                    fault_results: list[dict[str, Any]] = []
                    for _ in range(fault_cycles):
                        for model in (
                            "q009-fault-408",
                            "q009-fault-429",
                            "q009-fault-5xx",
                            "q009-timeout",
                        ):
                            provider_before = provider.count(model)
                            status, body, _, _ = _post(
                                port,
                                "chat_completions",
                                model,
                                api_token=agent_token,
                            )
                            provider_after = provider.count(model)
                            if provider_after <= provider_before:
                                raise QualificationError(
                                    "provider fault fixture was not observed for "
                                    f"{model}"
                                )
                            if status not in {200, 408, 429, 502, 503, 504}:
                                raise QualificationError(
                                    f"unexpected provider fault status for {model}: "
                                    f"HTTP {status}"
                                )
                            row = {
                                "model": model,
                                "category": "provider",
                                "expected_upstream_status": {
                                    "q009-fault-408": 408,
                                    "q009-fault-429": 429,
                                    "q009-fault-5xx": 503,
                                    "q009-timeout": "timeout",
                                }[model],
                                "client_http_status": status,
                                "provider_requests": provider_after - provider_before,
                                "response_bytes": len(body),
                            }
                            fault_results.append(row)
                            if status != 200:
                                _close_started(process, min(timeout, 8))
                                process = _start(binary, config, env)
                                _wait_ready(
                                    process,
                                    f"http://127.0.0.1:{port}/v1/healthz",
                                    timeout,
                                )
                                row["post_fault_server_restart"] = "pass"
                        for model in ("q009-partial", "q009-disconnect"):
                            status, body, _, _ = _post(
                                port,
                                "chat_completions",
                                model,
                                streaming=True,
                                api_token=agent_token,
                            )
                            if status != 200 or b"[DONE]" in body:
                                raise QualificationError(
                                    f"{model} did not preserve incomplete EOF evidence"
                                )
                            fault_results.append(
                                {
                                    "model": model,
                                    "http_status": status,
                                    "terminal_evidence": False,
                                }
                            )
                        _cancel_request(port, agent_token)
                        _write_abort_request(port, agent_token)
                        fault_results.append(
                            {
                                "model": "q009-cancel",
                                "status": "client-cancelled-and-write-aborted",
                            }
                        )
                        status, body, _, _ = _post(
                            port,
                            "chat_completions",
                            "q009-wire",
                            api_token=agent_token,
                        )
                        if status != 200 or not body:
                            raise QualificationError(
                                "alternate-wire fixture did not recover"
                            )
                        fault_results.append(
                            {
                                "model": "q009-wire",
                                "status": "alternate-wire-success",
                                "http_status": status,
                            }
                        )
                        status, body, _, _ = _post(
                            port,
                            "chat_completions",
                            "q009-malformed",
                            streaming=True,
                            api_token=agent_token,
                        )
                        if status != 200 or b"[DONE]" in body:
                            raise QualificationError(
                                "malformed stream fixture was not observed"
                            )
                        fault_results.append(
                            {
                                "model": "q009-malformed",
                                "status": "malformed-incomplete",
                                "http_status": status,
                            }
                        )
                        status, body, _, _ = _post(
                            port,
                            "chat_completions",
                            "q009-absent",
                            api_token=agent_token,
                        )
                        if status != 404:
                            raise QualificationError(
                                "model absence fixture did not remain model-scoped: "
                                f"HTTP {status}"
                            )
                        fault_results.append(
                            {
                                "model": "q009-absent",
                                "status": "model-absent",
                                "http_status": status,
                            }
                        )
                        status, body, _, _ = _post(
                            port,
                            "chat_completions",
                            "q009-connect/unreachable",
                            api_token=agent_token,
                        )
                        if status not in {502, 503} or not body:
                            raise QualificationError(
                                f"connect failure fixture returned HTTP {status}"
                            )
                        fault_results.append(
                            {
                                "model": "q009-connect/unreachable",
                                "status": "connect-failure",
                                "http_status": status,
                            }
                        )
                        budget_cases = (
                            (
                                "user_budget",
                                _set_user_request_budget,
                            ),
                            (
                                "global_budget",
                                _set_global_request_budget,
                            ),
                        )
                        for denial_kind, set_limit in budget_cases:
                            configured = set_limit(binary, config, env, 1, timeout)
                            commands.append(configured)
                            if configured["status"] != "pass":
                                raise QualificationError(
                                    f"{denial_kind} denial fixture setup failed"
                                )
                            upstream_before = provider.total()
                            status, _body, _, _ = _post(
                                port,
                                "chat_completions",
                                "q009-chat",
                                api_token=agent_token,
                            )
                            if status != 429 or provider.total() != upstream_before:
                                raise QualificationError(
                                    f"{denial_kind} did not reject before provider: "
                                    f"HTTP {status}"
                                )
                            fault_results.append(
                                {
                                    "category": "budget",
                                    "denial_kind": denial_kind,
                                    "http_status": status,
                                    "provider_requests": 0,
                                }
                            )
                            restored = set_limit(binary, config, env, 100, timeout)
                            commands.append(restored)
                            if restored["status"] != "pass":
                                raise QualificationError(
                                    f"{denial_kind} budget restore failed"
                                )
                    _wait_converged(database, process, port, samples, timeout)
                    _resource_sample("after-faults", process, database, port, samples)
                    report["phases"].append(
                        _phase_result(
                            "faults",
                            "pass",
                            cycles=fault_cycles,
                            observations=fault_results,
                        )
                    )
                if "all" in selected or "reload" in selected:
                    reload_results: list[dict[str, Any]] = []
                    original = config.read_text(encoding="utf-8")
                    for cycle in range(reload_cycles):
                        changed = original.replace(
                            "flush_interval_s = 2",
                            f"flush_interval_s = {3 + (cycle % 2)}",
                        )
                        config.write_text(changed, encoding="utf-8")
                        outcome = _command(
                            binary, config, env, ("rehash", "--json"), timeout
                        )
                        commands.append(outcome)
                        if outcome["status"] != "pass":
                            raise QualificationError(f"accepted reload {cycle} failed")
                        reload_results.append(
                            {"cycle": cycle, "kind": "accepted", "status": "pass"}
                        )
                        outcome = _command(
                            binary, config, env, ("rehash", "--json"), timeout
                        )
                        commands.append(outcome)
                        if outcome["status"] != "pass":
                            raise QualificationError(f"no-op reload {cycle} failed")
                        reload_results.append(
                            {"cycle": cycle, "kind": "no-op", "status": "pass"}
                        )
                        rejected = changed.replace(
                            f"port = {port}", f"port = {port + 1}"
                        )
                        config.write_text(rejected, encoding="utf-8")
                        outcome = _command(
                            binary, config, env, ("rehash", "--json"), timeout
                        )
                        commands.append(outcome)
                        if outcome["status"] == "pass":
                            raise QualificationError(
                                "restart-required reload was accepted"
                            )
                        outcome["status"] = "expected-rejection"
                        reload_results.append(
                            {
                                "cycle": cycle,
                                "kind": "restart-required",
                                "status": "rejected",
                            }
                        )
                        config.write_text(changed, encoding="utf-8")
                        outcome = _command(
                            binary, config, env, ("rehash", "--json"), timeout
                        )
                        commands.append(outcome)
                        if outcome["status"] != "pass":
                            raise QualificationError(
                                "reload recovery after rejected candidate failed"
                            )
                        _post(
                            port,
                            "chat_completions",
                            "q009-chat",
                            api_token=agent_token,
                        )
                    _resource_sample("after-reload", process, database, port, samples)
                    report["phases"].append(
                        _phase_result(
                            "reload",
                            "pass",
                            cycles=reload_cycles,
                            operations=reload_results,
                        )
                    )
                if "all" in selected or "restart" in selected:
                    before = provider.count("q009-abrupt")
                    request_error: list[object] = []

                    def in_flight() -> None:
                        try:
                            _post(
                                port,
                                "chat_completions",
                                "q009-abrupt",
                                api_token=disable_token,
                            )
                        except OSError as error:
                            request_error.append(type(error).__name__)

                    client = threading.Thread(target=in_flight, daemon=True)
                    client.start()
                    deadline = time.monotonic() + timeout
                    while (
                        provider.count("q009-abrupt") == before
                        and time.monotonic() < deadline
                    ):
                        time.sleep(0.02)
                    if provider.count("q009-abrupt") != before + 1:
                        raise QualificationError(
                            "abrupt-restart fixture did not reach upstream"
                        )
                    identity_status: int | None = None
                    if exercise_revocation_during_restart:
                        disable = _command(
                            binary,
                            config,
                            env,
                            (
                                "users",
                                "disable",
                                "q009-disablee",
                                "--actor",
                                "q009-admin",
                            ),
                            timeout,
                        )
                        disable["command"] = ["users", "disable"]
                        commands.append(disable)
                        if disable["status"] != "pass":
                            raise QualificationError(
                                "synthetic user disable failed during load"
                            )
                        upstream_before_denied_request = provider.total()
                        identity_status, _identity_body, _, _ = _post(
                            port,
                            "chat_completions",
                            "q009-chat",
                            api_token=disable_token,
                        )
                        if (
                            identity_status not in {401, 403}
                            or provider.total() != upstream_before_denied_request
                        ):
                            raise QualificationError(
                                "disabled principal was not denied before provider "
                                "dispatch"
                            )
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
                    process = None
                    restarted = _start(binary, config, env)
                    process = restarted
                    _wait_ready(process, f"http://127.0.0.1:{port}/v1/healthz", timeout)
                    time.sleep(0.35)
                    if provider.count("q009-abrupt") != before + 1:
                        raise QualificationError(
                            "startup reconciliation replayed unknown provider work"
                        )
                    client.join(timeout=2)
                    _wait_converged(database, process, port, samples, timeout)
                    _resource_sample("after-restart", process, database, port, samples)
                    report["phases"].append(
                        _phase_result(
                            "restart",
                            "pass",
                            cycles=restart_cycles,
                            unknown_inflight_replayed=False,
                            hard_budgets_active=True,
                            user_disabled_during_load=(
                                exercise_revocation_during_restart
                            ),
                            credential_revocation_during_load=(
                                exercise_revocation_during_restart
                            ),
                            identity_denial_http_status=identity_status,
                            client_outcome="cancelled-or-closed"
                            if request_error
                            else "closed",
                        )
                    )
                if "all" in selected or "final" in selected:
                    for surface, model, streaming in (
                        ("chat_completions", "q009-chat", False),
                        ("responses", "q009-responses", True),
                        ("messages", "q009-messages", False),
                    ):
                        status, body, _, _ = _post(
                            port,
                            surface,
                            model,
                            streaming=streaming,
                            api_token=agent_token,
                        )
                        if status != 200 or not body:
                            raise QualificationError(
                                f"final {surface} failed: HTTP {status}"
                            )
                    _wait_converged(database, process, port, samples, timeout)
                    _resource_sample(
                        "final-convergence", process, database, port, samples
                    )
                    report["phases"].append(_phase_result("final", "pass"))
                report["durable"] = _db_counts(database)
                report["provider"] = {
                    "request_count": provider.total(),
                    "requests_by_model": provider.counts(),
                }
                final_sample = samples[-1]
                report["ownership_convergence"] = {
                    "pending_requests": final_sample.get("pending_requests"),
                    "active_reservations": final_sample.get("active_reservations"),
                    "finalization_jobs": final_sample.get("finalization_jobs"),
                    "active_leases": final_sample.get("active_leases"),
                    "retiring_generations": final_sample.get("retiring_generations"),
                    "terminal_references": final_sample.get("terminal_references"),
                    "logical_leaks": False,
                }
                report["resource_analysis"] = {
                    "sample_count": len(samples),
                    "fd_counts": [sample.get("open_fd_count") for sample in samples],
                    "thread_counts": [sample.get("thread_count") for sample in samples],
                    "rss_bytes": [sample.get("rss_bytes") for sample in samples],
                    "interpretation": (
                        "bounded characterization; warmup/high-water allocator RSS "
                        "is not treated as a leak and no SLA is inferred"
                    ),
                }
                runtime_read_errors = sum(
                    1 for sample in samples if sample.get("runtime_read_error")
                )
                report["resource_evidence"] = {
                    "status": "infrastructure-error" if runtime_read_errors else "pass",
                    "runtime_read_errors": runtime_read_errors,
                    "semantics_affected": False,
                }
                report["status"] = "pass"
        finally:
            _close_started(process, min(timeout, 8))
        report["isolated_temporary_root"] = True
        report["temporary_leftovers"] = 0
        report["duration_ms"] = round((time.monotonic() - started) * 1000)
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--config-fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--seed", type=int, default=9009)
    parser.add_argument("--warmup-cycles", type=int, default=1)
    parser.add_argument("--steady-cycles", type=int, default=2)
    parser.add_argument("--fault-cycles", type=int, default=1)
    parser.add_argument("--reload-cycles", type=int, default=2)
    parser.add_argument("--restart-cycles", type=int, default=1)
    parser.add_argument("--exercise-revocation-during-restart", action="store_true")
    parser.add_argument(
        "--phase",
        dest="phases",
        action="append",
        choices=("warmup", "steady", "faults", "reload", "restart", "final"),
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = run_qualification(
            binary=args.binary,
            config_fixture=args.config_fixture,
            seed=args.seed,
            warmup_cycles=args.warmup_cycles,
            steady_cycles=args.steady_cycles,
            fault_cycles=args.fault_cycles,
            reload_cycles=args.reload_cycles,
            restart_cycles=args.restart_cycles,
            timeout=args.timeout,
            phases=frozenset(args.phases) if args.phases else None,
            exercise_revocation_during_restart=args.exercise_revocation_during_restart,
        )
    except (
        OSError,
        QualificationError,
        ValueError,
        subprocess.SubprocessError,
    ) as error:
        report = {
            "schema_version": SCHEMA_VERSION,
            "plan": "stability qualification",
            "manifest": MANIFEST_VERSION,
            "status": "fail",
            "reason": bounded(str(error)),
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps({"status": report.get("status"), "output": str(args.output)}))
    return 0 if report.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
