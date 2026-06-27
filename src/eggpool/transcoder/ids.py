"""Tool-call-id translation map for cross-protocol request/response bodies.

Phase 2 populates the map during body translation; this module provides
the data structure and helper methods.
"""

from __future__ import annotations


class ToolCallIdMap:
    """Bidirectional map between client and upstream tool-call IDs.

    IDs are opaque strings.  The map is populated lazily during body
    translation and is not needed when the client and upstream share
    the same protocol.
    """

    def __init__(self) -> None:
        self._client_to_upstream: dict[str, str] = {}
        self._upstream_to_client: dict[str, str] = {}
        self._counter: int = 0

    def register(
        self,
        client_id: str,
        upstream_id: str,
    ) -> None:
        """Register a mapping in both directions."""
        self._client_to_upstream[client_id] = upstream_id
        self._upstream_to_client[upstream_id] = client_id

    def to_upstream(self, client_id: str) -> str | None:
        """Translate a client-side ID to its upstream equivalent."""
        return self._client_to_upstream.get(client_id)

    def to_client(self, upstream_id: str) -> str | None:
        """Translate an upstream ID to its client-side equivalent."""
        return self._upstream_to_client.get(upstream_id)

    def generate_upstream_id(self) -> str:
        """Generate a unique upstream tool-call ID."""
        self._counter += 1
        return f"tcu_{self._counter}"

    def __len__(self) -> int:
        return len(self._client_to_upstream)

    def __bool__(self) -> bool:
        return bool(self._client_to_upstream)
