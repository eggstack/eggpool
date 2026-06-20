"""Fetch models from upstream per-account."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, cast

import httpx

from eggpool.catalog.normalizer import iter_model_items

logger = logging.getLogger(__name__)


@dataclass
class FetchResult:
    """Result of a catalog fetch including timing data for ping measurement."""

    response: dict[str, Any]
    latency_ms: int
    status_code: int | None
    error: str | None
    model_count: int


def _failed_fetch(
    *,
    latency_ms: int,
    status_code: int | None,
    error: str,
) -> FetchResult:
    """Build a consistent failed catalog fetch result."""
    return FetchResult(
        response={},
        latency_ms=latency_ms,
        status_code=status_code,
        error=error,
        model_count=0,
    )


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
        try:
            data_value: object = response.json()
        except ValueError:
            logger.warning(
                "Account %r: models response was not valid JSON",
                account_name,
            )
            return _failed_fetch(
                latency_ms=latency_ms,
                status_code=status_code,
                error="Invalid JSON response",
            )
        if not isinstance(data_value, dict):
            logger.warning(
                "Account %r: models response has an invalid shape",
                account_name,
            )
            return _failed_fetch(
                latency_ms=latency_ms,
                status_code=status_code,
                error="Invalid model catalog response",
            )
        data = cast("dict[str, Any]", data_value)
        model_items_value: object = data.get("data")
        if not isinstance(model_items_value, list):
            logger.warning(
                "Account %r: models response has an invalid shape",
                account_name,
            )
            return _failed_fetch(
                latency_ms=latency_ms,
                status_code=status_code,
                error="Invalid model catalog response",
            )
        model_count = sum(1 for _item in iter_model_items(data))
        if model_items_value and model_count == 0:
            logger.warning(
                "Account %r: models response contains no valid models",
                account_name,
            )
            return _failed_fetch(
                latency_ms=latency_ms,
                status_code=status_code,
                error="Invalid model catalog response",
            )
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
        return _failed_fetch(
            latency_ms=latency_ms,
            status_code=exc.response.status_code,
            error=f"HTTP {exc.response.status_code}",
        )
    except httpx.RequestError as exc:
        latency_ms = int((time.monotonic() - start) * 1000)
        logger.warning(
            "Account %r: request error fetching models: %s",
            account_name,
            exc,
        )
        return _failed_fetch(
            latency_ms=latency_ms,
            status_code=None,
            error=str(exc),
        )
