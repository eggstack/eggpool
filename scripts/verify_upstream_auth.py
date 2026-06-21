"""Upstream authentication verifier for EggPool deployment.

This script bypasses the proxy and calls the upstream endpoints
directly. It supports two modes:

1. **Legacy mode**: Read ``GOROUTER_UPSTREAM_BASE_URL`` and related
   environment variables (original behavior).

2. **Config mode** (``--config config.toml --provider <id>`` or
   ``--all``): Read provider contracts from a TOML configuration
   file and verify each provider/account using its declared auth,
   paths, and model-list endpoint.

The script is **not** part of automated CI execution. It must
only be run by an operator who has valid upstream credentials.

Never prints keys, bodies, prompts, or completions.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import tomllib
from dataclasses import dataclass
from typing import Any

import httpx

DEFAULT_TIMEOUT = 30.0

OPENAI_FAMILY = "openai"
ANTHROPIC_FAMILY = "anthropic"


@dataclass
class _AuthCheckResult:
    provider_id: str
    account_name: str
    family: str
    ok: bool
    status_code: int | None
    request_id: str | None
    resolved_url: str
    auth_shape: str
    detail: str


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        sys.stderr.write(f"Missing required environment variable: {name}\n")
        raise SystemExit(2)
    return value


def _build_auth_headers(
    mode: str, header: str, scheme: str, key: str
) -> dict[str, str]:
    """Build auth headers from contract config."""
    if mode == "none":
        return {}
    if mode in ("api_key", "raw_authorization"):
        return {header: key}
    # bearer mode (default)
    return {header: f"{scheme} {key}"}


def _compose_url(base_url: str, path: str) -> str:
    """Compose absolute URL from base and path."""
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


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


def _make_client() -> httpx.Client:
    """Build a default ``httpx.Client``."""
    return httpx.Client(timeout=DEFAULT_TIMEOUT)


def _run_single_check(
    client: httpx.Client,
    provider_id: str,
    account_name: str,
    family: str,
    request: httpx.Request,
    resolved_url: str,
    auth_shape: str,
) -> _AuthCheckResult:
    started = time.time()
    try:
        response = client.send(request)
    except httpx.HTTPError as exc:
        elapsed = (time.time() - started) * 1000.0
        return _AuthCheckResult(
            provider_id=provider_id,
            account_name=account_name,
            family=family,
            ok=False,
            status_code=None,
            request_id=None,
            resolved_url=resolved_url,
            auth_shape=auth_shape,
            detail=f"transport_error: {type(exc).__name__} elapsed_ms={elapsed:.0f}",
        )

    elapsed = (time.time() - started) * 1000.0
    request_id = _extract_request_id(response.headers)
    status_ok = response.status_code < 400
    detail = (
        f"status={response.status_code} elapsed_ms={elapsed:.0f} "
        f"request_id={request_id or '-'}"
    )
    return _AuthCheckResult(
        provider_id=provider_id,
        account_name=account_name,
        family=family,
        ok=status_ok,
        status_code=response.status_code,
        request_id=request_id,
        resolved_url=resolved_url,
        auth_shape=auth_shape,
        detail=detail,
    )


def _verify_config_provider(
    client: httpx.Client,
    provider_cfg: dict[str, Any],
    api_key: str,
    account_name: str,
    openai_model: str | None,
    anthropic_model: str | None,
    verbose: bool,
) -> list[_AuthCheckResult]:
    """Verify a single provider/account using its contract config."""
    provider_id = provider_cfg.get("id", "unknown")
    base_url = provider_cfg.get("base_url", "")
    auth_cfg = provider_cfg.get("auth", {})
    auth_mode = auth_cfg.get("mode", "bearer")
    auth_header = auth_cfg.get("header", "Authorization")
    auth_scheme = auth_cfg.get("scheme", "Bearer")

    auth_headers = _build_auth_headers(auth_mode, auth_header, auth_scheme, api_key)
    auth_shape = (
        f"{auth_header}: {auth_scheme} ***"
        if auth_mode == "bearer"
        else f"{auth_header}: ***"
    )

    results: list[_AuthCheckResult] = []

    # Verify model listing endpoint
    models_cfg: dict[str, Any] = provider_cfg.get("models_endpoint") or {}  # type: ignore[assignment]
    models_method: str = provider_cfg.get(
        "models_method", models_cfg.get("method", "GET")
    )  # type: ignore[assignment]
    models_path: str = provider_cfg.get(
        "models_path", models_cfg.get("path", "/models")
    )  # type: ignore[assignment]

    if models_method != "DISABLED" and models_path:
        models_url = _compose_url(base_url, models_path)
        headers = {**auth_headers, "Accept": "application/json"}
        try:
            if models_method.upper() == "POST":
                models_body: dict[str, Any] = models_cfg.get("body") or {}  # type: ignore[assignment]
                req = httpx.Request(
                    "POST", models_url, headers=headers, json=models_body
                )
            else:
                req = httpx.Request("GET", models_url, headers=headers)
            result = _run_single_check(
                client,
                provider_id,
                account_name,
                "models",
                req,
                models_url,
                auth_shape,
            )
            results.append(result)
            if verbose:
                marker = "OK" if result.ok else "FAIL"
                sys.stdout.write(
                    f"  [{marker}] {provider_id}/{account_name} models: "
                    f"{result.detail}\n"
                )
                sys.stdout.write(f"    resolved_url={result.resolved_url}\n")
                sys.stdout.write(f"    auth={result.auth_shape}\n")
        except Exception as exc:
            results.append(
                _AuthCheckResult(
                    provider_id=provider_id,
                    account_name=account_name,
                    family="models",
                    ok=False,
                    status_code=None,
                    request_id=None,
                    resolved_url=models_url,
                    auth_shape=auth_shape,
                    detail=f"error: {exc}",
                )
            )

    # Verify OpenAI chat endpoint
    openai_path = provider_cfg.get("openai_path")
    if openai_path and openai_model:
        chat_url = _compose_url(base_url, openai_path)
        headers = {**auth_headers, "Content-Type": "application/json"}
        payload = {
            "model": openai_model,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "ping"}],
        }
        req = httpx.Request("POST", chat_url, headers=headers, json=payload)
        result = _run_single_check(
            client,
            provider_id,
            account_name,
            OPENAI_FAMILY,
            req,
            chat_url,
            auth_shape,
        )
        results.append(result)
        if verbose:
            marker = "OK" if result.ok else "FAIL"
            sys.stdout.write(
                f"  [{marker}] {provider_id}/{account_name} openai: {result.detail}\n"
            )
            sys.stdout.write(f"    resolved_url={result.resolved_url}\n")

    # Verify Anthropic messages endpoint
    anthropic_path = provider_cfg.get("anthropic_path")
    if anthropic_path and anthropic_model:
        msg_url = _compose_url(base_url, anthropic_path)
        headers = {
            **auth_headers,
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        }
        payload = {
            "model": anthropic_model,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "ping"}],
        }
        req = httpx.Request("POST", msg_url, headers=headers, json=payload)
        result = _run_single_check(
            client,
            provider_id,
            account_name,
            ANTHROPIC_FAMILY,
            req,
            msg_url,
            auth_shape,
        )
        results.append(result)
        if verbose:
            marker = "OK" if result.ok else "FAIL"
            sys.stdout.write(
                f"  [{marker}] {provider_id}/{account_name} anthropic: "
                f"{result.detail}\n"
            )
            sys.stdout.write(f"    resolved_url={result.resolved_url}\n")

    return results


def _run_legacy_checks(
    base_url: str,
    key: str,
    openai_model: str,
    anthropic_model: str,
) -> list[_AuthCheckResult]:
    """Run the original env-var-based verification."""
    expected_bearer = f"Bearer {key}"
    auth_shape = "Authorization: Bearer ***"

    # OpenAI check
    openai_url = f"{base_url.rstrip('/')}/v1/chat/completions"
    openai_headers = {
        "authorization": expected_bearer,
        "content-type": "application/json",
    }
    openai_payload = {
        "model": openai_model,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "ping"}],
    }
    openai_req = httpx.Request(
        "POST", openai_url, headers=openai_headers, json=openai_payload
    )

    # Anthropic check
    anthropic_url = f"{base_url.rstrip('/')}/v1/messages"
    anthropic_headers = {
        "authorization": expected_bearer,
        "content-type": "application/json",
        "anthropic-version": "2023-06-01",
    }
    anthropic_payload = {
        "model": anthropic_model,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "ping"}],
    }
    anthropic_req = httpx.Request(
        "POST",
        anthropic_url,
        headers=anthropic_headers,
        json=anthropic_payload,
    )

    results: list[_AuthCheckResult] = []
    c = _make_client()
    try:
        results.append(
            _run_single_check(
                c,
                "legacy",
                "default",
                OPENAI_FAMILY,
                openai_req,
                openai_url,
                auth_shape,
            )
        )
        results.append(
            _run_single_check(
                c,
                "legacy",
                "default",
                ANTHROPIC_FAMILY,
                anthropic_req,
                anthropic_url,
                auth_shape,
            )
        )
    finally:
        c.close()
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Upstream auth verifier for EggPool")
    parser.add_argument("--config", help="Path to config.toml")
    parser.add_argument("--provider", help="Verify a specific provider ID")
    parser.add_argument(
        "--all", action="store_true", help="Verify all providers in config"
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    parser.add_argument("--openai-model", help="OpenAI model for chat probe")
    parser.add_argument("--anthropic-model", help="Anthropic model for chat probe")
    args = parser.parse_args()

    if args.config:
        # Config mode
        try:
            with open(args.config, "rb") as f:
                config = tomllib.load(f)
        except FileNotFoundError:
            sys.stderr.write(f"Config file not found: {args.config}\n")
            return 2
        except tomllib.TOMLDecodeError as exc:
            sys.stderr.write(f"Invalid TOML: {exc}\n")
            return 2

        providers = config.get("providers", {})
        if not providers:
            sys.stderr.write("No providers found in config\n")
            return 2

        # Determine which providers to verify
        if args.provider:
            if args.provider not in providers:
                sys.stderr.write(f"Provider {args.provider!r} not found in config\n")
                return 2
            provider_ids = [args.provider]
        elif args.all:
            provider_ids = list(providers.keys())
        else:
            sys.stderr.write("Specify --provider <id> or --all\n")
            return 2

        all_results: list[_AuthCheckResult] = []
        c = _make_client()
        try:
            for pid in provider_ids:
                prov = providers[pid]
                for acct in prov.get("accounts", []):
                    acct_name = acct.get("name", "default")
                    api_key = acct.get("api_key") or os.environ.get(
                        acct.get("api_key_env", "")
                    )
                    if not api_key:
                        sys.stdout.write(
                            f"  [SKIP] {pid}/{acct_name}: no API key available\n"
                        )
                        continue

                    results = _verify_config_provider(
                        c,
                        prov,
                        api_key,
                        acct_name,
                        args.openai_model,
                        args.anthropic_model,
                        args.verbose,
                    )
                    all_results.extend(results)
        finally:
            c.close()

        # Summary
        failed = [r for r in all_results if not r.ok]
        if not args.verbose:
            for r in all_results:
                marker = "OK" if r.ok else "FAIL"
                sys.stdout.write(
                    f"  [{marker}] {r.provider_id}/{r.account_name} "
                    f"{r.family}: {r.detail}\n"
                )

        if failed:
            sys.stdout.write(f"\n{len(failed)}/{len(all_results)} checks failed\n")
            return 1
        sys.stdout.write(f"\nAll {len(all_results)} checks passed\n")
        return 0

    else:
        # Legacy mode (env vars)
        base_url = _require_env("GOROUTER_UPSTREAM_BASE_URL")
        key = _require_env("GOROUTER_TEST_UPSTREAM_KEY")
        openai_model = os.environ.get("GOROUTER_OPENAI_MODEL", "gpt-4")
        anthropic_model = os.environ.get(
            "GOROUTER_ANTHROPIC_MODEL", "claude-3-5-sonnet"
        )

        results = _run_legacy_checks(base_url, key, openai_model, anthropic_model)

        failed = [r for r in results if not r.ok]
        for r in results:
            marker = "OK" if r.ok else "FAIL"
            sys.stdout.write(f"  [{marker}] {r.family}: {r.detail}\n")
        return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
