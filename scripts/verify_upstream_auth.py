"""Direct upstream authentication verifier for GoRouter deployment.

This script bypasses the proxy and calls the upstream
OpenAI-compatible and Anthropic-compatible endpoints directly
using the same ``Authorization: Bearer`` header that GoRouter
emits. It is intended for two operational purposes:

1. Confirm that a configured key actually authenticates against
   each upstream endpoint family.
2. Distinguish upstream authentication / model compatibility
   failures from GoRouter-side proxy defects during live testing.

The script is **not** part of automated CI execution. It must
only be run by an operator who has explicitly set the
``GOROUTER_UPSTREAM_BASE_URL``, ``GOROUTER_TEST_UPSTREAM_KEY``,
``GOROUTER_OPENAI_MODEL``, and ``GOROUTER_ANTHROPIC_MODEL``
environment variables.

Required environment:

    GOROUTER_UPSTREAM_BASE_URL
        e.g. https://api.example.com
    GOROUTER_TEST_UPSTREAM_KEY
        the upstream key to verify
    GOROUTER_OPENAI_MODEL
        an OpenAI-protocol model id, e.g. gpt-4
    GOROUTER_ANTHROPIC_MODEL
        an Anthropic-protocol model id, e.g. claude-3-5-sonnet

The script:
- Sends one minimal non-streaming OpenAI-compatible request
  directly upstream using ``Authorization: Bearer``.
- Sends one minimal non-streaming Anthropic-compatible request
  directly upstream using the same authentication scheme.
- Reports only status code, request ID if present, and endpoint
  family.
- Never prints the key, body, prompt, or completion.
- Returns nonzero if either family rejects authentication.

Model examples are illustrative; the operator must supply
current, real model IDs known to be advertised by the upstream
catalog.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass

import httpx

DEFAULT_TIMEOUT = 30.0

OPENAI_PATH = "/v1/chat/completions"
ANTHROPIC_PATH = "/v1/messages"

OPENAI_FAMILY = "openai"
ANTHROPIC_FAMILY = "anthropic"


@dataclass
class _AuthCheckResult:
    family: str
    ok: bool
    status_code: int | None
    request_id: str | None
    detail: str


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        sys.stderr.write(f"Missing required environment variable: {name}\n")
        raise SystemExit(2)
    return value


def _build_openai_request(
    base_url: str, key: str, model: str
) -> tuple[httpx.Request, dict[str, str]]:
    headers = {
        "authorization": f"Bearer {key}",
        "content-type": "application/json",
    }
    payload = {
        "model": model,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "ping"}],
    }
    return (
        httpx.Request(
            "POST",
            f"{base_url.rstrip('/')}{OPENAI_PATH}",
            headers=headers,
            json=payload,
        ),
        headers,
    )


def _build_anthropic_request(
    base_url: str, key: str, model: str
) -> tuple[httpx.Request, dict[str, str]]:
    headers = {
        "authorization": f"Bearer {key}",
        "content-type": "application/json",
        "anthropic-version": "2023-06-01",
    }
    payload = {
        "model": model,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "ping"}],
    }
    return (
        httpx.Request(
            "POST",
            f"{base_url.rstrip('/')}{ANTHROPIC_PATH}",
            headers=headers,
            json=payload,
        ),
        headers,
    )


def _extract_request_id(headers: httpx.Headers) -> str | None:
    for name in (
        "x-request-id",
        "x-amzn-requestid",
        "request-id",
        "anthropic-request-id",
    ):
        value = headers.get(name)
        if value:
            return value
    return None


def _run_check(
    client: httpx.Client,
    family: str,
    request: httpx.Request,
    expected_header: str,
) -> _AuthCheckResult:
    started = time.time()
    try:
        response = client.send(request)
    except httpx.HTTPError as exc:
        elapsed = (time.time() - started) * 1000.0
        return _AuthCheckResult(
            family=family,
            ok=False,
            status_code=None,
            request_id=None,
            detail=f"transport_error: {type(exc).__name__} elapsed_ms={elapsed:.0f}",
        )

    elapsed = (time.time() - started) * 1000.0
    request_id = _extract_request_id(response.headers)
    auth_ok = _has_exactly_one_bearer(request, expected_header)
    status_ok = response.status_code < 400
    ok = status_ok and auth_ok
    detail = (
        f"status={response.status_code} elapsed_ms={elapsed:.0f} "
        f"request_id={request_id or '-'}"
    )
    return _AuthCheckResult(
        family=family,
        ok=ok,
        status_code=response.status_code,
        request_id=request_id,
        detail=detail,
    )


def _has_exactly_one_bearer(request: httpx.Request, expected_header: str) -> bool:
    """Check that the request emitted exactly one ``Authorization`` header
    and no ``x-api-key`` header.

    Returns True when only the expected bearer header is present.
    """
    auth_count = 0
    has_api_key = False
    for name in request.headers:
        if name.lower() == "authorization":
            auth_count += 1
            if request.headers.get(name) != expected_header:
                return False
        if name.lower() == "x-api-key":
            has_api_key = True
    return auth_count == 1 and not has_api_key


def _make_client() -> httpx.Client:
    """Build a default ``httpx.Client``.

    Tests monkeypatch this function to inject a transport without
    enabling HTTPX debug logging. The default implementation never
    installs any debug hooks.
    """
    return httpx.Client(timeout=DEFAULT_TIMEOUT)


def _run_all_checks(
    base_url: str,
    key: str,
    openai_model: str,
    anthropic_model: str,
) -> list[_AuthCheckResult]:
    """Execute the verification flow with environment-derived inputs.

    The OpenAI/Anthropic checks share a single ``httpx.Client`` so
    connection pooling can be exercised. The returned list contains
    one result per endpoint family.
    """
    expected_bearer = f"Bearer {key}"

    openai_request, openai_headers = _build_openai_request(base_url, key, openai_model)
    anthropic_request, anthropic_headers = _build_anthropic_request(
        base_url, key, anthropic_model
    )

    # Sanity: the constructed request must carry exactly the bearer
    # header and never an ``x-api-key`` (the proxy only injects
    # ``Authorization``). This is a defense-in-depth check that runs
    # before any network I/O.
    if openai_headers.get("authorization") != expected_bearer:
        sys.stderr.write("openai header build mismatch\n")
        raise SystemExit(2)
    if "x-api-key" in openai_headers:
        sys.stderr.write("openai unexpectedly built an x-api-key header\n")
        raise SystemExit(2)
    if anthropic_headers.get("authorization") != expected_bearer:
        sys.stderr.write("anthropic header build mismatch\n")
        raise SystemExit(2)
    if "x-api-key" in anthropic_headers:
        sys.stderr.write("anthropic unexpectedly built an x-api-key header\n")
        raise SystemExit(2)

    results: list[_AuthCheckResult] = []
    client = _make_client()
    try:
        results.append(
            _run_check(client, OPENAI_FAMILY, openai_request, expected_bearer)
        )
        results.append(
            _run_check(client, ANTHROPIC_FAMILY, anthropic_request, expected_bearer)
        )
    finally:
        client.close()
    return results


def main() -> int:
    base_url = _require_env("GOROUTER_UPSTREAM_BASE_URL")
    key = _require_env("GOROUTER_TEST_UPSTREAM_KEY")
    openai_model = _require_env("GOROUTER_OPENAI_MODEL")
    anthropic_model = _require_env("GOROUTER_ANTHROPIC_MODEL")

    results = _run_all_checks(
        base_url=base_url,
        key=key,
        openai_model=openai_model,
        anthropic_model=anthropic_model,
    )

    failed = [r for r in results if not r.ok]
    for r in results:
        marker = "OK" if r.ok else "FAIL"
        sys.stdout.write(f"  [{marker}] {r.family}: {r.detail}\n")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
