"""Request-local parsed payload container for hot-path optimization."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ParsedRequestPayload:
    """Caches the parsed original body and derived request state.

    Created once in handle_proxy_request() and threaded through the
    request lifecycle to avoid repeated JSON parsing.

    Invariants:
    - original_bytes is always the raw client bytes (never mutated)
    - parsed_dict is the parsed JSON (lazy, computed once)
    - derived values are computed on demand and cached
    - upstream_body mutations create a separate dict/bytes
    """

    original_bytes: bytes
    _parsed_dict: dict[str, Any] | None = field(default=None, repr=False)
    _parse_failed: bool = field(default=False, repr=False)

    # Cached derived state
    _model_id: str | None = field(default=None, repr=False)
    _provider_id: str | None = field(default=None, repr=False)
    _streaming: bool | None = field(default=None, repr=False)
    _estimated_reservation_tokens: int | None = field(default=None, repr=False)
    _thinking_requirement: Any | None = field(default=None, repr=False)

    @property
    def parsed_dict(self) -> dict[str, Any] | None:
        """Parse the original body once, return cached result."""
        if self._parsed_dict is None and not self._parse_failed:
            try:
                self._parsed_dict = json.loads(self.original_bytes)
            except (json.JSONDecodeError, ValueError):
                self._parse_failed = True
        return self._parsed_dict

    @property
    def model_id(self) -> str | None:
        if self._model_id is None and self.parsed_dict is not None:
            self._model_id = self.parsed_dict.get("model")
        return self._model_id

    @property
    def streaming(self) -> bool | None:
        if self._streaming is None and self.parsed_dict is not None:
            self._streaming = self.parsed_dict.get("stream", False)
        return self._streaming

    def invalidate_transformed(self) -> None:
        """Invalidate cached state after body transformation (transcode/compress).

        Call this when upstream_body is set to a different value.
        Does NOT invalidate original_bytes or parsed_dict.
        """
        self._model_id = None
        self._provider_id = None
        self._streaming = None
