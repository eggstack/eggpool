"""Fetch models from upstream per-account."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)


@dataclass
class FetchResult:
    """Result of a catalog fetch including timing data for ping measurement."""

    response: dict[str, Any]
    latency_ms: int
    status_code: int | None
    error: str | None
    model_count: int


async def fetch_models_for_account(
    client: httpx.AsyncClient,
    api_key: str,
    account_name: str,
    models_method: str = "GET",
    models_path: str = "/models",
) -> FetchResult:
    """Fetch the model list from an upstream account with timing data.

    Supports both GET and POST methods for different providers.
    Returns a FetchResult with response data, timing, and error info.
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    start = time.monotonic()
    try:
        if models_method.upper() == "POST":
            response = await client.post(models_path, headers=headers, json={})
        else:
            response = await client.get(models_path, headers=headers)
        latency_ms = int((time.monotonic() - start) * 1000)
        status_code = response.status_code
        response.raise_for_status()
        data = response.json()
        model_count = len(data.get("data", []))
        return FetchResult(
            response=data,
            latency_ms=latency_ms,
            status_code=status_code,
            error=None,
            model_count=model_count,
        )
    except httpx.HTTPStatusError as exc:
        latency_ms = int((time.monotonic() - start) * 1000)
        logger.warning(
            "Account %r: HTTP %d fetching models",
            account_name,
            exc.response.status_code,
        )
        return FetchResult(
            response={},
            latency_ms=latency_ms,
            status_code=exc.response.status_code,
            error=f"HTTP {exc.response.status_code}",
            model_count=0,
        )
    except httpx.RequestError as exc:
        latency_ms = int((time.monotonic() - start) * 1000)
        logger.warning(
            "Account %r: request error fetching models: %s",
            account_name,
            exc,
        )
        return FetchResult(
            response={},
            latency_ms=latency_ms,
            status_code=None,
            error=str(exc),
            model_count=0,
        )
