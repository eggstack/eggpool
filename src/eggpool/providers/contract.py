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

    Raises :class:`ConfigError` if joining would produce a duplicate
    version prefix (e.g. ``base_url=https://api.example.com/v1`` with
    ``endpoint_path=/v1/chat/completions``), since that is always a
    configuration error regardless of which validator caught it first.
    """
    base = provider.base_url.rstrip("/")
    # A trailing slash can be semantically significant (especially for POST
    # endpoints), so remove only the leading separators needed for joining.
    path = endpoint_path.lstrip("/")
    # Defense in depth: refuse to assemble duplicate version prefixes
    # (``/v1/v1/...``, ``/api/v1/api/v1/...``) at the URL layer so any
    # caller — including ones that bypass the upstream validators —
    # cannot silently produce broken URLs.
    base_lower = base.lower()
    versioned_suffixes = ("/v1", "/api/v1", "/compatible-mode/v1")
    for suffix in versioned_suffixes:
        if base_lower.endswith(suffix) and path.lower().startswith(
            suffix.lstrip("/") + "/"
        ):
            raise ConfigError(
                f"Provider {provider.id!r} base_url ends with {suffix!r} but "
                f"endpoint_path {endpoint_path!r} starts with the same "
                f"version prefix; the composed URL would contain a duplicate "
                f"version segment."
            )
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
    *,
    protocol: str | None = None,
) -> dict[str, str]:
    """Build all upstream headers: auth + static provider headers.

    When ``protocol`` is supplied and the provider does not already
    declare protocol-required static headers, inject them from the
    built-in table in ``transcoder.static_headers``.
    """
    from eggpool.transcoder.static_headers import PROTOCOL_REQUIRED_STATIC_HEADERS

    headers = build_static_headers(provider)
    # Inject protocol-required static headers when not already declared
    if protocol is not None:
        required = PROTOCOL_REQUIRED_STATIC_HEADERS.get(protocol, {})
        existing_names = {name.casefold() for name in headers}
        for name, value in required.items():
            if name.casefold() not in existing_names:
                headers[name] = value
    auth_headers = build_auth_headers(provider, api_key)
    auth_names = {name.casefold() for name in auth_headers}
    headers = {
        name: value
        for name, value in headers.items()
        if name.casefold() not in auth_names
    }
    headers.update(auth_headers)
    return headers
