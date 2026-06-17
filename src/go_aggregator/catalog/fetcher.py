"""Fetch models from upstream per-account."""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


async def fetch_models_for_account(
    client: httpx.AsyncClient,
    api_key: str,
    account_name: str,
) -> dict[str, Any]:
    """Fetch the model list from an upstream account.

    Returns the raw JSON response or an empty dict on failure.
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    try:
        response = await client.get("/models", headers=headers)
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "Account %r: HTTP %d fetching models",
            account_name,
            exc.response.status_code,
        )
        return {}
    except httpx.RequestError as exc:
        logger.warning(
            "Account %r: request error fetching models: %s",
            account_name,
            exc,
        )
        return {}
