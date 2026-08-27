"""Upstream provider authentication utilities."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eggpool.models.config import ProviderAdditionalAuthConfig


def render_auth_header(
    *,
    mode: str,
    header: str,
    scheme: str,
    api_key: str,
) -> str:
    """Render a single upstream auth header value from primitives.

    Returns an empty string when ``mode == "none"`` so callers can
    coalesce a list of additional headers without checking modes.
    """
    if mode == "none":
        return ""
    if mode in {"api_key", "raw_authorization"}:
        return api_key
    return f"{scheme} {api_key}"


def render_auth_headers(
    *,
    mode: str,
    header: str,
    scheme: str,
    api_key: str,
    additional: list[ProviderAdditionalAuthConfig] | None = None,
) -> dict[str, str]:
    """Render upstream auth headers from provider contract primitives.

    When ``additional`` is supplied, every entry is rendered with the
    same ``api_key`` and merged into the result. ``mode = "none"``
    still renders the additional entries because each carries its own
    ``mode``; only the primary header is suppressed.
    """
    result: dict[str, str] = {}
    if mode != "none":
        result[header] = render_auth_header(
            mode=mode, header=header, scheme=scheme, api_key=api_key
        )
    for entry in additional or ():
        value = render_auth_header(
            mode=entry.mode, header=entry.header, scheme=entry.scheme, api_key=api_key
        )
        if value:
            result[entry.header] = value
    return result


def has_auth_scheme_prefix(api_key: str, scheme: str) -> bool:
    """Return whether a key already starts with its configured auth scheme.

    Splitting on arbitrary whitespace catches values such as ``Bearer\tkey``
    as well as the more usual ``Bearer key``. A bare scheme is also rejected:
    prepending the configured scheme would still produce an invalid header.
    """
    parts = api_key.strip().split(maxsplit=1)
    return bool(parts) and parts[0].casefold() == scheme.casefold()
