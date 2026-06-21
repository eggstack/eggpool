"""Fetch models from upstream per-account."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import httpx

from eggpool.catalog.normalizer import iter_model_items

if TYPE_CHECKING:
    from eggpool.models.config import ProviderConfig

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
    *,
    provider_cfg: ProviderConfig | None = None,
) -> FetchResult:
    """Fetch the model list from an upstream account with timing data.

    Supports both GET and POST methods for different providers.
    When ``provider_cfg`` is provided, uses its contract for auth
    headers, model-list body, query params, and absolute URL composition.
    Returns a FetchResult with response data, timing, and error info.
    """
    if provider_cfg is not None:
        from eggpool.providers.contract import (
            build_upstream_headers,
            compose_provider_url,
        )

        auth_headers = build_upstream_headers(provider_cfg, api_key)
        endpoint = provider_cfg.models_endpoint
        if endpoint is not None and endpoint.method == "DISABLED":
            return FetchResult(
                response={},
                latency_ms=0,
                status_code=None,
                error=None,
                model_count=0,
            )
        method = provider_cfg.models_method
        path = provider_cfg.models_path
        url = compose_provider_url(provider_cfg, path)
        headers = {**auth_headers, "Accept": "application/json"}
        body = endpoint.body if endpoint is not None else None
        query = endpoint.query if endpoint is not None else {}
    else:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        }
        method = models_method
        path = models_path
        url = None  # Use relative path with httpx client
        body = None
        query = {}
    start = time.monotonic()
    try:
        if method.upper() == "POST":
            response = await client.post(
                path if url is None else url,
                headers=headers,
                json=body if body is not None else {},
                params=query or None,
            )
        else:
            response = await client.get(
                path if url is None else url,
                headers=headers,
                params=query or None,
            )
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
