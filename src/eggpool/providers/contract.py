"""Provider contract rendering — centralized URL composition and auth headers."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eggpool.models.config import ProviderConfig, ProviderStaticHeaderConfig


def compose_provider_url(provider: ProviderConfig, endpoint_path: str) -> str:
    """Compose an absolute URL from provider base_url and an endpoint path.

    Strips trailing slashes from base_url and leading slashes from
    endpoint_path, then joins them with a single slash.
    """
    base = provider.base_url.rstrip("/")
    path = endpoint_path.strip("/")
    return f"{base}/{path}"


def build_auth_headers(provider: ProviderConfig, api_key: str) -> dict[str, str]:
    """Build upstream authentication headers from provider contract config.

    Returns an empty dict when auth mode is ``none``.
    """
    auth = provider.auth
    if auth.mode == "none":
        return {}
    if auth.mode in ("api_key", "raw_authorization"):
        return {auth.header: api_key}
    # bearer mode (default)
    return {auth.header: f"{auth.scheme} {api_key}"}


def resolve_static_header_value(header: ProviderStaticHeaderConfig) -> str | None:
    """Resolve a static header value from inline value or env var."""
    if header.value is not None:
        return header.value
    if header.value_env is not None:
        return os.environ.get(header.value_env)
    return None


def build_static_headers(provider: ProviderConfig) -> dict[str, str]:
    """Build the set of static provider headers, resolving env vars."""
    result: dict[str, str] = {}
    for header in provider.headers:
        value = resolve_static_header_value(header)
        if value is not None:
            result[header.name] = value
    return result


def build_upstream_headers(
    provider: ProviderConfig,
    api_key: str,
) -> dict[str, str]:
    """Build all upstream headers: auth + static provider headers."""
    # Authentication is authoritative. ProviderConfig rejects a static
    # header with the same name, but applying auth last is defense in depth
    # for programmatically mutated config objects.
    headers = build_static_headers(provider)
    headers.update(build_auth_headers(provider, api_key))
    return headers
