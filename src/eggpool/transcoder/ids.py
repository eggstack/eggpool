"""Tool-call-id translation map for cross-protocol request/response bodies.

Phase 2 populates the map during body translation; this module provides
the data structure and helper methods.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from eggpool.transcoder.context import TranscodeContext


def _uuid_hex() -> str:
    """Return a 24-char hex slice from a fresh uuid4 (Anthropic-shaped)."""
    return uuid.uuid4().hex[:24]


class ToolCallIdMap:
    """Bidirectional map between client and upstream tool-call IDs.

    IDs are opaque strings.  The map is populated lazily during body
    translation and is not needed when the client and upstream share
    the same protocol.
    """

    def __init__(self) -> None:
        self._client_to_upstream: dict[str, str] = {}
        self._upstream_to_client: dict[str, str] = {}

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

    def generate_openai_id(self) -> str:
        """Generate an OpenAI-shaped tool-call id (``call_<hex>``)."""
        return f"call_{_uuid_hex()}"

    def generate_anthropic_id(self) -> str:
        """Generate an Anthropic-shaped tool-call id (``toolu_<hex>``)."""
        return f"toolu_{_uuid_hex()}"

    def generate_upstream_id(self) -> str:
        """Backwards-compatible alias for :meth:`generate_anthropic_id`.

        Anthropic is the canonical "upstream" for tool calls in this
        codebase; this shim preserves the old method name so external
        callers keep working.
        """
        return self.generate_anthropic_id()

    def truncate(
        self,
        upstream_id: str,
        context: TranscodeContext | None = None,
    ) -> None:
        """Drop accumulated arguments buffer for ``upstream_id``.

        Phase 6.1 only needs the warning emission side: the streaming
        transcoder manages its own per-id argument buffers.  When
        ``context`` is supplied, append a structured loss warning so
        operators can audit truncated tools.
        """
        if context is None:
            return
        warning: dict[str, Any] = {
            "kind": "malformed_tool_arguments",
            "id": upstream_id,
        }
        context.loss_warnings.append(warning)

    def __len__(self) -> int:
        return len(self._client_to_upstream)

    def __bool__(self) -> bool:
        return bool(self._client_to_upstream)
