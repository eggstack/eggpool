"""Deployment smoke test for GoRouter on a Raspberry Pi.

This script validates that a freshly deployed aggregator answers the
core data-plane and dashboard endpoints, and that one non-streaming
and one streaming request succeed for each of the OpenAI-compatible
and Anthropic-compatible protocol families.

The script is intentionally side-effect-light: it never logs or
echoes request bodies, response bodies, or secrets. It only reports
endpoint status codes and timing.

Required environment:
    GOROUTER_BASE_URL  e.g. http://192.168.1.20:8080
    GOROUTER_API_KEY   the local proxy API key (NOT an upstream key)
    GOROUTER_OPENAI_MODEL  an OpenAI-protocol model id, e.g. gpt-4
    GOROUTER_ANTHROPIC_MODEL  an Anthropic-protocol model id, e.g. claude-3-5-sonnet
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Any

import httpx

DEFAULT_TIMEOUT = 30.0


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""


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
        ok = resp.status_code in (200, 503) and (
            200 if resp.status_code == 200 else None
        ) is not None
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


def _chat_completion(
    client: httpx.Client,
    base: str,
    api_key: str,
    model: str,
    *,
    stream: bool,
) -> CheckResult:
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 8,
    }
    if stream:
        payload["stream"] = True
    try:
        resp = client.post(
            f"{base}/v1/chat/completions",
            headers={"authorization": f"Bearer {api_key}"},
            json=payload,
            timeout=DEFAULT_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        label = "openai_stream" if stream else "openai"
        return CheckResult(label, False, f"transport: {exc!r}")
    request_id = resp.headers.get("x-proxy-request-id", "")
    attempt = resp.headers.get("x-proxy-attempt-count", "")
    if resp.status_code >= 400:
        return CheckResult(
            "openai_stream" if stream else "openai",
            False,
            f"status={resp.status_code} request_id={request_id}",
        )
    return CheckResult(
        "openai_stream" if stream else "openai",
        True,
        f"status={resp.status_code} attempts={attempt} request_id={request_id}",
    )


def _anthropic_message(
    client: httpx.Client,
    base: str,
    api_key: str,
    model: str,
    *,
    stream: bool,
) -> CheckResult:
    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": 8,
        "messages": [{"role": "user", "content": "ping"}],
    }
    if stream:
        payload["stream"] = True
    try:
        resp = client.post(
            f"{base}/v1/messages",
            headers={
                "authorization": f"Bearer {api_key}",
                "anthropic-version": "2023-06-01",
                "x-api-key": api_key,
            },
            json=payload,
            timeout=DEFAULT_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        label = "anthropic_stream" if stream else "anthropic"
        return CheckResult(label, False, f"transport: {exc!r}")
    request_id = resp.headers.get("x-proxy-request-id", "")
    attempt = resp.headers.get("x-proxy-attempt-count", "")
    if resp.status_code >= 400:
        return CheckResult(
            "anthropic_stream" if stream else "anthropic",
            False,
            f"status={resp.status_code} request_id={request_id}",
        )
    return CheckResult(
        "anthropic_stream" if stream else "anthropic",
        True,
        f"status={resp.status_code} attempts={attempt} request_id={request_id}",
    )


def _check_stats(client: httpx.Client, base: str) -> CheckResult:
    for path in ("/v1/stats", "/v1/stats/accounts", "/v1/stats/usage"):
        try:
            resp = client.get(
                f"{base}{path}", timeout=DEFAULT_TIMEOUT
            )
        except httpx.HTTPError:
            continue
        if resp.status_code == 200:
            return CheckResult("stats", True, f"endpoint={path}")
    return CheckResult("stats", False, "no stats endpoint responded 200")


def main() -> int:
    base = _require_env("GOROUTER_BASE_URL").rstrip("/")
    api_key = _require_env("GOROUTER_API_KEY")
    openai_model = os.environ.get("GOROUTER_OPENAI_MODEL", "gpt-4")
    anthropic_model = os.environ.get("GOROUTER_ANTHROPIC_MODEL", "claude-3-5-sonnet")

    results: list[CheckResult] = []
    started = time.time()
    with httpx.Client(timeout=DEFAULT_TIMEOUT) as client:
        results.extend(_check_health(client, base))
        results.append(_check_models(client, base, api_key))
        results.append(_check_stats(client, base))

        if os.environ.get("GOROUTER_SKIP_LIVE") != "1":
            results.append(
                _chat_completion(
                    client, base, api_key, openai_model, stream=False
                )
            )
            results.append(
                _chat_completion(
                    client, base, api_key, openai_model, stream=True
                )
            )
            results.append(
                _anthropic_message(
                    client, base, api_key, anthropic_model, stream=False
                )
            )
            results.append(
                _anthropic_message(
                    client, base, api_key, anthropic_model, stream=True
                )
            )
        else:
            results.append(CheckResult("openai", True, "skipped"))
            results.append(CheckResult("openai_stream", True, "skipped"))
            results.append(CheckResult("anthropic", True, "skipped"))
            results.append(CheckResult("anthropic_stream", True, "skipped"))

    elapsed = time.time() - started
    failed = [r for r in results if not r.ok]
    sys.stdout.write(f"Smoke test completed in {elapsed:.1f}s\n")
    for r in results:
        marker = "OK" if r.ok else "FAIL"
        sys.stdout.write(f"  [{marker}] {r.name}: {r.detail}\n")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
