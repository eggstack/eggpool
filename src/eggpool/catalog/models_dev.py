"""models.dev metadata helpers for provider catalog enrichment."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Protocol, cast

import httpx

logger = logging.getLogger(__name__)

MODELS_DEV_BASE_URL = "https://models.dev"
OPENCODE_GO_MODELS_DEV_PROVIDER_ID = "opencode-go"
if TYPE_CHECKING:
    from collections.abc import Mapping

    class ModelsDevHttpClient(Protocol):
        """HTTP client subset needed for models.dev catalog fetches."""

        async def get(
            self,
            url: str,
            *,
            headers: dict[str, str] | None = None,
        ) -> httpx.Response: ...


def _as_object(value: object) -> dict[str, Any]:
    """Return *value* as a shallow dict when it is object-shaped."""
    if not isinstance(value, dict):
        return {}
    return dict(cast("dict[str, Any]", value))


async def fetch_models_dev_provider_models(
    client: ModelsDevHttpClient,
    provider_id: str,
    *,
    base_url: str = MODELS_DEV_BASE_URL,
) -> dict[str, dict[str, Any]]:
    """Fetch provider-scoped model metadata from models.dev.

    The endpoint is advisory metadata. Callers should treat errors as
    non-fatal and continue with upstream/config catalog data.
    """
    url = f"{base_url.rstrip('/')}/api.json"
    try:
        response = await client.get(url, headers={"Accept": "application/json"})
        response.raise_for_status()
        payload: object = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning(
            "Failed to fetch models.dev metadata for provider %r: %s",
            provider_id,
            exc,
        )
        return {}

    root = _as_object(payload)
    provider = _as_object(root.get(provider_id))
    models = _as_object(provider.get("models"))
    result: dict[str, dict[str, Any]] = {}
    for model_id, value in models.items():
        if isinstance(value, dict):
            result[model_id] = dict(cast("dict[str, Any]", value))
    return result


def merge_models_dev_metadata(
    model: dict[str, Any],
    metadata: Mapping[str, Any],
) -> None:
    """Merge one models.dev model row into a normalized catalog model."""
    source_metadata_raw = model.get("source_metadata", {})
    source_metadata = (
        dict(cast("dict[str, Any]", source_metadata_raw))
        if isinstance(source_metadata_raw, dict)
        else {}
    )
    for key, value in metadata.items():
        if key not in source_metadata:
            source_metadata[key] = value
    source_metadata["metadata_source"] = "models.dev"
    source_metadata["models_dev_provider_id"] = OPENCODE_GO_MODELS_DEV_PROVIDER_ID
    model["source_metadata"] = source_metadata
