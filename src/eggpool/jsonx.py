"""Centralized JSON backend helper.

Hot-path JSON serialization and parsing live behind a small API so the rest
of EggPool does not import ``json`` (stdlib) or ``orjson`` directly.  The
preferred backend is ``orjson`` for performance; a stdlib fallback keeps
lightweight installs (e.g. Raspberry Pi / SBC targets) working when the
``orjson`` wheel is not available or has been explicitly disabled.

Backend selection
-----------------

The active backend is chosen once at import time according to:

* the ``EGGPOOL_JSON_BACKEND`` environment variable
  (``"orjson"`` / ``"stdlib"`` / ``"auto"``),
* whether ``orjson`` is importable on this Python interpreter.

``auto`` (the default) prefers ``orjson`` and falls back to stdlib
silently.  ``orjson`` forces the fast backend and raises
``RuntimeError`` at first use if it cannot be imported.  ``stdlib``
forces the stdlib fallback even when ``orjson`` is installed.

The active backend is exposed via :data:`USING_ORJSON` so callers and
diagnostic surfaces (startup log, runtime stats) can report which
implementation is in effect.
"""

from __future__ import annotations

import importlib
import json
import logging
import math
import os
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

JsonInput = bytes | bytearray | memoryview | str


def _resolve_backend() -> tuple[str, Any]:
    """Return ``(name, orjson_module_or_None)`` for the active backend.

    The chosen backend name is one of ``"orjson"`` or ``"stdlib"``.
    When ``"orjson"`` is selected, the resolved module is returned so
    the import below can rebind it without triggering a second import.
    """
    override = os.environ.get("EGGPOOL_JSON_BACKEND", "auto").strip().lower()
    if override not in {"auto", "orjson", "stdlib"}:
        logger.warning(
            "EGGPOOL_JSON_BACKEND=%r is not a recognised value; "
            "expected auto|orjson|stdlib.  Falling back to auto.",
            override,
        )
        override = "auto"
    if override == "stdlib":
        return ("stdlib", None)
    try:
        module = importlib.import_module("orjson")
    except ImportError:
        if override == "orjson":
            raise RuntimeError(
                "EGGPOOL_JSON_BACKEND=orjson but orjson is not installed"
            ) from None
        return ("stdlib", None)
    return ("orjson", module)


_ACTIVE_BACKEND, _RESOLVED_ORJSON = _resolve_backend()
USING_ORJSON = _ACTIVE_BACKEND == "orjson"

if _ACTIVE_BACKEND == "orjson":
    if _RESOLVED_ORJSON is None:
        raise RuntimeError("orjson backend selected but module is not resolved")
    _orjson = _RESOLVED_ORJSON

    def loads(data: JsonInput) -> Any:
        """Decode a JSON document to a Python value."""
        if isinstance(data, str):
            return _orjson.loads(data)
        # ``data`` is bytes/bytearray/memoryview; normalise to bytes
        # for the orjson C entry point.  Reject other types at runtime.
        if not isinstance(data, (bytes, bytearray, memoryview)):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise TypeError(
                f"jsonx.loads expected bytes/str, got {type(data).__name__}"
            )
        return _orjson.loads(bytes(data))

    def dumps_bytes(
        obj: Any,
        *,
        sort_keys: bool = False,
        default: Callable[[Any], Any] | None = None,
    ) -> bytes:
        """Encode ``obj`` as compact UTF-8 JSON bytes."""
        # ``orjson`` returns ``bytes``; ``OPT_NON_STR_KEYS`` mirrors stdlib
        # ``json`` for dicts with non-string keys so callers do not see a
        # new failure mode after switching backends.
        options = _orjson.OPT_NON_STR_KEYS
        if sort_keys:
            options |= _orjson.OPT_SORT_KEYS
        return _orjson.dumps(obj, option=options, default=default)

    def dumps_str(
        obj: Any,
        *,
        sort_keys: bool = False,
        default: Callable[[Any], Any] | None = None,
    ) -> str:
        """Encode ``obj`` as a compact JSON string.

        ``orjson`` always returns bytes; decode UTF-8 for DB / log fields
        that still require a Python ``str``.
        """
        return dumps_bytes(obj, sort_keys=sort_keys, default=default).decode("utf-8")

else:
    # The stdlib fallback must mirror orjson's encoding envelope so the
    # two backends never disagree on the same payload:
    #
    # * integers outside orjson's accepted window ([-2^63, 2^64-1])
    #   raise instead of being silently emitted;
    # * non-finite floats become ``null`` (what orjson emits), never
    #   bare ``NaN`` / ``Infinity`` tokens, which are invalid JSON per
    #   RFC 8259.
    _INT64_MIN = -(2**63)
    _UINT64_MAX = 2**64 - 1
    _MAX_NESTING_DEPTH = 64

    def _scan_envelope(obj: Any) -> bool:
        """Validate *obj* against orjson's integer envelope.

        Raises ``ValueError`` for out-of-range integers and returns
        whether any non-finite float was seen (so the caller knows a
        sanitizing copy is required).
        """
        needs_sanitize = False
        stack: list[tuple[Any, int]] = [(obj, 0)]
        while stack:
            current, depth = stack.pop()
            if current is None or isinstance(current, (bool, str)):
                continue
            if isinstance(current, int):
                if not (_INT64_MIN <= current <= _UINT64_MAX):
                    raise ValueError("Integer exceeds 64-bit range")
                continue
            if isinstance(current, float):
                needs_sanitize = needs_sanitize or not math.isfinite(current)
                continue
            if isinstance(current, dict):
                if depth >= _MAX_NESTING_DEPTH:
                    raise ValueError("JSON exceeds maximum nesting depth")
                entries = cast("dict[Any, Any]", current)
                next_depth = depth + 1
                stack.extend(
                    (item, next_depth) for pair in entries.items() for item in pair
                )
            elif isinstance(current, (list, tuple)):
                if depth >= _MAX_NESTING_DEPTH:
                    raise ValueError("JSON exceeds maximum nesting depth")
                stack.extend(
                    (item, depth + 1)
                    for item in cast("list[Any] | tuple[Any, ...]", current)
                )
        return needs_sanitize

    def _sanitize_non_finite(obj: Any) -> Any:
        """Return a copy of *obj* with non-finite floats replaced by ``None``.

        Non-finite *dict keys* are also replaced (with ``None``, which
        ``json.dumps`` renders as the string ``"null"``).  This mirrors
        the orjson backend, which serialises non-str keys under
        ``OPT_NON_STR_KEYS`` and emits non-finite floats as ``null`` —
        keeping the two backends byte-identical on the same payload.
        """
        if isinstance(obj, float):
            return obj if math.isfinite(obj) else None
        if isinstance(obj, dict):
            entries = cast("dict[Any, Any]", obj)
            return {
                (
                    key if not isinstance(key, float) or math.isfinite(key) else None
                ): _sanitize_non_finite(value)
                for key, value in entries.items()
            }
        if isinstance(obj, list):
            items = cast("list[Any]", obj)
            return [_sanitize_non_finite(item) for item in items]
        if isinstance(obj, tuple):
            items = cast("tuple[Any, ...]", obj)
            return tuple(_sanitize_non_finite(item) for item in items)
        return obj

    def _prepare_stdlib(obj: Any) -> Any:
        """Return *obj* ready for :func:`json.dumps`, orjson-style."""
        if _scan_envelope(obj):
            return _sanitize_non_finite(obj)
        return obj

    def loads(data: JsonInput) -> Any:
        """Decode a JSON document to a Python value."""
        try:
            if isinstance(data, (bytes, bytearray, memoryview)):
                return json.loads(bytes(data))
            if not isinstance(data, str):  # pyright: ignore[reportUnnecessaryIsInstance]
                raise TypeError(
                    f"jsonx.loads expected bytes/str, got {type(data).__name__}"
                )
            return json.loads(data)
        except RecursionError:
            raise ValueError("JSON exceeds maximum nesting depth") from None

    def dumps_bytes(
        obj: Any,
        *,
        sort_keys: bool = False,
        default: Callable[[Any], Any] | None = None,
    ) -> bytes:
        """Encode ``obj`` as compact UTF-8 JSON bytes."""
        return json.dumps(
            _prepare_stdlib(obj),
            default=default,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=sort_keys,
        ).encode("utf-8")

    def dumps_str(
        obj: Any,
        *,
        sort_keys: bool = False,
        default: Callable[[Any], Any] | None = None,
    ) -> str:
        """Encode ``obj`` as a compact JSON string."""
        return json.dumps(
            _prepare_stdlib(obj),
            default=default,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=sort_keys,
        )


def active_backend() -> str:
    """Return the name of the active JSON backend (``"orjson"`` or ``"stdlib"``)."""
    return _ACTIVE_BACKEND


__all__ = [
    "USING_ORJSON",
    "active_backend",
    "dumps_bytes",
    "dumps_str",
    "loads",
]
