"""ParsedUpstreamResponse — single-decode lifecycle for non-stream responses.

Workstream E of Plan 028.  For non-streaming upstream responses the
coordinator currently re-parses the response body independently for
usage extraction, error transcoding, normalized-usage construction, and
success-response transcoding.  ``ParsedUpstreamResponse`` decodes the
body **once** and provides typed accessors so every consumer reads from
the same decoded representation.

Design rules
~~~~~~~~~~~~
- Parse only when a consumer needs JSON.
- Parse at most once (lazy, memoised).
- Preserve invalid/non-object distinction — callers check
  ``parse_status`` before accessing ``parsed_json``.
- Preserve original bytes for pass-through.
- Encode only when response transcoding/adaptation changes the body.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from eggpool.jsonx import loads as jsonx_loads

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ParsedUpstreamResponse:
    """Single-decode representation of a non-streaming upstream response.

    Attributes
    ----------
    status_code:
        HTTP status code from the upstream.
    headers:
        Response headers as a list of ``(name, value)`` tuples,
        already filtered through ``filter_response_headers``.
    raw_body:
        Unmodified response bytes.
    parsed_json:
        Decoded JSON object (``dict`` or ``list``) when parsing
        succeeds; ``None`` otherwise.  Lazily computed on first
        access.
    parse_status:
        One of ``"not_attempted"``, ``"parsed"``, ``"invalid_json"``,
        or ``"non_object"``.  Updated atomically with ``parsed_json``.
    """

    status_code: int
    headers: list[tuple[str, str]]
    raw_body: bytes

    # Lazily populated — ``None`` means "not yet attempted".
    _parsed_json: Any | None = field(default=None, repr=False, compare=False)
    _parse_status: Literal["not_attempted", "parsed", "invalid_json", "non_object"] = (
        field(default="not_attempted", repr=False, compare=False)
    )

    @property
    def parsed_json(self) -> Any:  # noqa: ANN401
        """Return the decoded JSON, parsing on first access.

        Returns ``None`` when the body is not valid JSON or when the
        parsed value is neither a ``dict`` nor a ``list`` — callers
        should check ``parse_status`` for the exact reason.
        """
        if self._parse_status == "not_attempted":
            self._attempt_parse()
        return self._parsed_json

    @property
    def parsed_dict(self) -> dict[str, Any] | None:
        """Return the parsed JSON as a ``dict`` when it is one, else ``None``."""
        obj = self.parsed_json
        if isinstance(obj, dict):
            return cast("dict[str, Any]", obj)
        return None

    @property
    def parse_status(
        self,
    ) -> Literal["not_attempted", "parsed", "invalid_json", "non_object"]:
        """Return the parse outcome without triggering a parse."""
        if self._parse_status == "not_attempted":
            self._attempt_parse()
        return self._parse_status

    @property
    def is_success(self) -> bool:
        """Return ``True`` for 2xx status codes."""
        return 200 <= self.status_code < 300

    @property
    def is_error(self) -> bool:
        """Return ``True`` for 4xx/5xx status codes."""
        return self.status_code >= 400

    def _attempt_parse(self) -> None:
        """Parse the body exactly once, updating state atomically."""
        try:
            obj = jsonx_loads(self.raw_body)
        except (ValueError, TypeError):
            self._parse_status = "invalid_json"
            self._parsed_json = None
            return

        if isinstance(obj, (dict, list)):
            self._parsed_json = obj
            self._parse_status = "parsed"
        else:
            self._parsed_json = None
            self._parse_status = "non_object"

    def header_value(self, name: str) -> str | None:
        """Return the first matching header value (case-insensitive)."""
        lower = name.lower()
        for h_name, h_value in self.headers:
            if h_name.lower() == lower:
                return h_value
        return None


def build_parsed_upstream_response(
    status_code: int,
    headers: list[tuple[str, str]],
    raw_body: bytes,
) -> ParsedUpstreamResponse:
    """Construct a ``ParsedUpstreamResponse`` from raw upstream data.

    This is the canonical factory called by the coordinator after
    ``response.aread()`` completes.  Parsing is deferred until a
    consumer actually needs the decoded JSON.
    """
    return ParsedUpstreamResponse(
        status_code=status_code,
        headers=headers,
        raw_body=raw_body,
    )
