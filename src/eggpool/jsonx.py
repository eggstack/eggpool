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
import os
from typing import Any

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
    assert _RESOLVED_ORJSON is not None
    _orjson = _RESOLVED_ORJSON

    def loads(data: JsonInput) -> Any:
        """Decode a JSON document to a Python value."""
        if isinstance(data, str):
            return _orjson.loads(data.encode("utf-8"))
        # ``data`` is bytes/bytearray/memoryview; normalise to bytes
        # for the orjson C entry point.  Reject other types at runtime.
        if not isinstance(data, (bytes, bytearray, memoryview)):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise TypeError(
                f"jsonx.loads expected bytes/str, got {type(data).__name__}"
            )
        return _orjson.loads(bytes(data))

    def dumps_bytes(obj: Any) -> bytes:
        """Encode ``obj`` as compact UTF-8 JSON bytes."""
        # ``orjson`` returns ``bytes``; ``OPT_NON_STR_KEYS`` mirrors stdlib
        # ``json`` for dicts with non-string keys so callers do not see a
        # new failure mode after switching backends.
        return _orjson.dumps(obj, option=_orjson.OPT_NON_STR_KEYS)

    def dumps_str(obj: Any) -> str:
        """Encode ``obj`` as a compact JSON string.

        ``orjson`` always returns bytes; decode UTF-8 for DB / log fields
        that still require a Python ``str``.
        """
        return dumps_bytes(obj).decode("utf-8")

else:

    def loads(data: JsonInput) -> Any:
        """Decode a JSON document to a Python value."""
        if isinstance(data, (bytes, bytearray, memoryview)):
            return json.loads(bytes(data))
        return json.loads(data)

    def dumps_bytes(obj: Any) -> bytes:
        """Encode ``obj`` as compact UTF-8 JSON bytes."""
        return json.dumps(
            obj,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

    def dumps_str(obj: Any) -> str:
        """Encode ``obj`` as a compact JSON string."""
        return json.dumps(
            obj,
            ensure_ascii=False,
            separators=(",", ":"),
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
