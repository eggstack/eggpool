"""Protocol-required static headers for cross-protocol transcoding.

When transcoding, certain headers must be present on the upstream
request even if the operator did not declare them.  This module
contains the canonical defaults.
"""

from __future__ import annotations

PROTOCOL_REQUIRED_STATIC_HEADERS: dict[str, dict[str, str]] = {
    "anthropic": {"anthropic-version": "2023-06-01"},
}
