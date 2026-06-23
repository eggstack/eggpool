"""Upstream provider authentication utilities."""

from __future__ import annotations


def render_auth_headers(
    *,
    mode: str,
    header: str,
    scheme: str,
    api_key: str,
) -> dict[str, str]:
    """Render upstream auth headers from provider contract primitives."""
    if mode == "none":
        return {}
    if mode in {"api_key", "raw_authorization"}:
        return {header: api_key}
    return {header: f"{scheme} {api_key}"}


def has_auth_scheme_prefix(api_key: str, scheme: str) -> bool:
    """Return whether a key already starts with its configured auth scheme.

    Splitting on arbitrary whitespace catches values such as ``Bearer\tkey``
    as well as the more usual ``Bearer key``. A bare scheme is also rejected:
    prepending the configured scheme would still produce an invalid header.
    """
    parts = api_key.strip().split(maxsplit=1)
    return bool(parts) and parts[0].casefold() == scheme.casefold()
