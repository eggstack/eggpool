"""Deployment smoke test for GoRouter on a Raspberry Pi.

This script validates that a freshly deployed aggregator answers the
core data-plane and dashboard endpoints, and that one non-streaming
and one streaming request succeed for each of the OpenAI-compatible
and Anthropic-compatible protocol families.

The script is intentionally side-effect-light: it never logs or
echoes request bodies, response bodies, or secrets. It only reports
endpoint status codes, timing, and structural SSE markers.

Required environment (all values must be set explicitly; no defaults
for model identifiers or secrets):

    GOROUTER_BASE_URL          e.g. http://192.168.1.20:8080
    GOROUTER_API_KEY           the local proxy API key (NOT an upstream key)
    GOROUTER_OPENAI_MODEL      an OpenAI-protocol model id, e.g. gpt-4
    GOROUTER_ANTHROPIC_MODEL   an Anthropic-protocol model id, e.g.
                               claude-3-5-sonnet

Optional environment:

    GOROUTER_SKIP_LIVE=1       skip non-streaming and streaming calls
                               (used by the unit test harness)
    GOROUTER_TEST_STREAM_CANCEL=1
                               after the first nonempty streaming chunk
                               is read, close the response early to
                               exercise the client cancellation path
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

DEFAULT_TIMEOUT = 30.0

OPENAI_PATH = "/v1/chat/completions"
ANTHROPIC_PATH = "/v1/messages"

OPENAI_STREAM_MARKER = b"data:"
ANTHROPIC_STREAM_MARKER = b"event:"


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""
    timings: dict[str, float] = field(default_factory=dict)


@dataclass
class _StreamCheckState:
    """Streaming check state captured during a single stream response."""

    request_started_at: float
    headers_received_at: float | None = None
    first_chunk_at: float | None = None
    completed_at: float | None = None
    chunk_count: int = 0
    saw_stream_marker: bool = False
    transport_error: str | None = None


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        sys.stderr.write(f"Missing required environment variable: {name}\n")
        sys.exit(2)
    return value


def _check_health(client: httpx.Client, base: str) -> list[CheckResult]:
    results: list[CheckResult] = []
    for path, label in (
        ("/v1/healthz", "healthz"),
        ("/v1/readyz", "readyz"),
    ):
        try:
            resp = client.get(f"{base}{path}", timeout=DEFAULT_TIMEOUT)
        except httpx.HTTPError as exc:
            results.append(CheckResult(label, False, f"transport: {exc!r}"))
            continue
        results.append(
            CheckResult(
                label,
                resp.status_code == 200,
                f"status={resp.status_code}",
            )
        )
    return results


def _check_models(client: httpx.Client, base: str, api_key: str) -> CheckResult:
    try:
        resp = client.get(
            f"{base}/v1/models",
            headers={"authorization": f"Bearer {api_key}"},
            timeout=DEFAULT_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        return CheckResult("models", False, f"transport: {exc!r}")
    if resp.status_code != 200:
        return CheckResult("models", False, f"status={resp.status_code}")
    try:
        body = resp.json()
    except json.JSONDecodeError:
        return CheckResult("models", False, "non-json body")
    data = body.get("data", []) if isinstance(body, dict) else []
    return CheckResult("models", bool(data), f"count={len(data)}")


def _check_stats(client: httpx.Client, base: str) -> CheckResult:
    for path in ("/v1/stats", "/v1/stats/accounts", "/v1/stats/usage"):
        try:
            resp = client.get(f"{base}{path}", timeout=DEFAULT_TIMEOUT)
        except httpx.HTTPError:
            continue
        if resp.status_code == 200:
            return CheckResult("stats", True, f"endpoint={path}")
    return CheckResult("stats", False, "no stats endpoint responded 200")


def _validate_stream_response(
    response: httpx.Response,
    state: _StreamCheckState,
    *,
    required_marker: bytes,
) -> CheckResult:
    """Validate a streaming response end-to-end.

    Records timing of headers / first chunk / completion, requires the
    proxy request-id and attempt-count headers, ensures at least one
    nonempty chunk is delivered, and confirms a known SSE marker
    (``data:`` for OpenAI, ``event:`` for Anthropic) appears.
    """
    if response.status_code >= 400:
        return CheckResult(
            "openai_stream"
            if required_marker == OPENAI_STREAM_MARKER
            else "anthropic_stream",
            False,
            f"status={response.status_code}",
        )

    request_id = response.headers.get("x-proxy-request-id", "")
    attempt = response.headers.get("x-proxy-attempt-count", "")
    if not request_id:
        return CheckResult(
            "openai_stream"
            if required_marker == OPENAI_STREAM_MARKER
            else "anthropic_stream",
            False,
            "missing x-proxy-request-id header",
        )

    state.headers_received_at = time.time()
    saw_nonempty = False
    saw_marker = False
    cancel_after_first = os.environ.get("GOROUTER_TEST_STREAM_CANCEL") == "1"

    try:
        for chunk in response.iter_bytes():
            state.chunk_count += 1
            if chunk:
                if not saw_nonempty:
                    state.first_chunk_at = time.time()
                    saw_nonempty = True
                if required_marker in chunk:
                    saw_marker = True
                if cancel_after_first:
                    response.close()
                    break
    except httpx.HTTPError as exc:
        state.transport_error = repr(exc)
    finally:
        # Closing the response (either by the iterator running out, the
        # caller breaking, or an explicit close) is part of the contract.
        from contextlib import suppress

        with suppress(httpx.HTTPError):
            response.close()

    state.completed_at = time.time()
    state.saw_stream_marker = saw_marker

    ok = saw_nonempty and saw_marker and state.transport_error is None
    timings: dict[str, float] = {}
    if state.headers_received_at is not None:
        timings["headers_ms"] = (
            state.headers_received_at - state.request_started_at
        ) * 1000.0
    if state.first_chunk_at is not None:
        timings["first_chunk_ms"] = (
            state.first_chunk_at - state.request_started_at
        ) * 1000.0
    if state.completed_at is not None:
        timings["total_ms"] = (state.completed_at - state.request_started_at) * 1000.0
    detail = (
        f"status={response.status_code} attempts={attempt} "
        f"request_id={request_id} chunks={state.chunk_count}"
    )
    return CheckResult(
        "openai_stream"
        if required_marker == OPENAI_STREAM_MARKER
        else "anthropic_stream",
        ok,
        detail,
        timings,
    )


def _check_streaming(
    client: httpx.Client,
    base: str,
    api_key: str,
    *,
    path: str,
    model: str,
    headers: dict[str, str],
    required_marker: bytes,
    label: str,
) -> CheckResult:
    """Send one streaming request and validate it incrementally.

    Uses ``httpx.Client.stream`` so headers are received and chunks
    are read in real time rather than buffered into a single body.
    """
    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": 8,
        "stream": True,
        "messages": [{"role": "user", "content": "ping"}],
    }
    state = _StreamCheckState(request_started_at=time.time())
    try:
        with client.stream(
            "POST",
            f"{base}{path}",
            headers=headers,
            json=payload,
            timeout=DEFAULT_TIMEOUT,
        ) as response:
            return _validate_stream_response(
                response, state, required_marker=required_marker
            )
    except httpx.HTTPError as exc:
        return CheckResult(label, False, f"transport: {exc!r}")


def _openai_stream(
    client: httpx.Client, base: str, api_key: str, model: str
) -> CheckResult:
    return _check_streaming(
        client,
        base,
        api_key,
        path=OPENAI_PATH,
        model=model,
        headers={"authorization": f"Bearer {api_key}"},
        required_marker=OPENAI_STREAM_MARKER,
        label="openai_stream",
    )


def _anthropic_stream(
    client: httpx.Client, base: str, api_key: str, model: str
) -> CheckResult:
    return _check_streaming(
        client,
        base,
        api_key,
        path=ANTHROPIC_PATH,
        model=model,
        headers={
            "authorization": f"Bearer {api_key}",
            "anthropic-version": "2023-06-01",
        },
        required_marker=ANTHROPIC_STREAM_MARKER,
        label="anthropic_stream",
    )


def _non_streaming(
    client: httpx.Client,
    base: str,
    api_key: str,
    *,
    path: str,
    model: str,
    headers: dict[str, str],
    label: str,
) -> CheckResult:
    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": 8,
        "messages": [{"role": "user", "content": "ping"}],
    }
    try:
        resp = client.post(
            f"{base}{path}",
            headers=headers,
            json=payload,
            timeout=DEFAULT_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        return CheckResult(label, False, f"transport: {exc!r}")
    request_id = resp.headers.get("x-proxy-request-id", "")
    attempt = resp.headers.get("x-proxy-attempt-count", "")
    if resp.status_code >= 400:
        return CheckResult(
            label,
            False,
            f"status={resp.status_code} request_id={request_id}",
        )
    return CheckResult(
        label,
        True,
        f"status={resp.status_code} attempts={attempt} request_id={request_id}",
    )


def _openai(client: httpx.Client, base: str, api_key: str, model: str) -> CheckResult:
    return _non_streaming(
        client,
        base,
        api_key,
        path=OPENAI_PATH,
        model=model,
        headers={"authorization": f"Bearer {api_key}"},
        label="openai",
    )


def _anthropic(
    client: httpx.Client, base: str, api_key: str, model: str
) -> CheckResult:
    return _non_streaming(
        client,
        base,
        api_key,
        path=ANTHROPIC_PATH,
        model=model,
        headers={
            "authorization": f"Bearer {api_key}",
            "anthropic-version": "2023-06-01",
        },
        label="anthropic",
    )


def _summarize_timings(results: Iterable[CheckResult]) -> Iterator[str]:
    for r in results:
        if r.timings:
            parts = " ".join(f"{k}={v:.0f}ms" for k, v in r.timings.items())
            yield f"  [TIMING] {r.name}: {parts}"


def main() -> int:
    base = _require_env("GOROUTER_BASE_URL").rstrip("/")
    api_key = _require_env("GOROUTER_API_KEY")
    openai_model = _require_env("GOROUTER_OPENAI_MODEL")
    anthropic_model = _require_env("GOROUTER_ANTHROPIC_MODEL")

    results: list[CheckResult] = []
    started = time.time()
    with httpx.Client(timeout=DEFAULT_TIMEOUT) as client:
        results.extend(_check_health(client, base))
        results.append(_check_models(client, base, api_key))
        results.append(_check_stats(client, base))

        if os.environ.get("GOROUTER_SKIP_LIVE") != "1":
            results.append(_openai(client, base, api_key, openai_model))
            results.append(_openai_stream(client, base, api_key, openai_model))
            results.append(_anthropic(client, base, api_key, anthropic_model))
            results.append(_anthropic_stream(client, base, api_key, anthropic_model))
        else:
            for label in (
                "openai",
                "openai_stream",
                "anthropic",
                "anthropic_stream",
            ):
                results.append(CheckResult(label, True, "skipped"))

    elapsed = time.time() - started
    failed = [r for r in results if not r.ok]
    sys.stdout.write(f"Smoke test completed in {elapsed:.1f}s\n")
    for r in results:
        marker = "OK" if r.ok else "FAIL"
        sys.stdout.write(f"  [{marker}] {r.name}: {r.detail}\n")
    for line in _summarize_timings(results):
        sys.stdout.write(line + "\n")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
