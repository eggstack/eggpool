"""Provider contract rendering — centralized URL composition and auth headers."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from eggpool.errors import ConfigError
from eggpool.providers.auth import render_auth_headers

# Provider verification status tiers.  Used by the CLI and interactive
# connect flow to label each provider with a symbol and human description.
PROVIDER_STATUS_SYMBOLS: dict[str, str] = {
    "verified": "✓",
    "experimental": "~",
    "unverified": "?",
}

PROVIDER_STATUS_DESCRIPTIONS: dict[str, str] = {
    "verified": "live-tested and confirmed working",
    "experimental": "plausible but needs verification",
    "unverified": "not tested",
}

if TYPE_CHECKING:
    from eggpool.models.config import ProviderConfig, ProviderStaticHeaderConfig


def compose_provider_url(provider: ProviderConfig, endpoint_path: str) -> str:
    """Compose an absolute URL from provider base_url and an endpoint path.

    Strips trailing slashes from base_url and leading slashes from
    endpoint_path, then joins them with a single slash. A trailing slash on
    endpoint_path is preserved because it may be semantically significant.
    """
    base = provider.base_url.rstrip("/")
    # A trailing slash can be semantically significant (especially for POST
    # endpoints), so remove only the leading separators needed for joining.
    path = endpoint_path.lstrip("/")
    return f"{base}/{path}"


def build_auth_headers(provider: ProviderConfig, api_key: str) -> dict[str, str]:
    """Build upstream authentication headers from provider contract config.

    Returns an empty dict when auth mode is ``none``.
    """
    auth = provider.auth
    return render_auth_headers(
        mode=auth.mode,
        header=auth.header,
        scheme=auth.scheme,
        api_key=api_key,
    )


def resolve_static_header_value(header: ProviderStaticHeaderConfig) -> str | None:
    """Resolve a static header value from inline value or env var."""
    if header.value is not None:
        return header.value
    if header.value_env is not None:
        value = os.environ.get(header.value_env)
        if value is not None and any(char in value for char in ("\r", "\n", "\x00")):
            raise ConfigError(
                f"Static header {header.name!r} from environment variable "
                f"{header.value_env!r} contains CR, LF, or NUL"
            )
        return value
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
    auth_headers = build_auth_headers(provider, api_key)
    auth_names = {name.casefold() for name in auth_headers}
    headers = {
        name: value
        for name, value in headers.items()
        if name.casefold() not in auth_names
    }
    headers.update(auth_headers)
    return headers
