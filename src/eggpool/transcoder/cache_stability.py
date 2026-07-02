"""Cache-stability helpers for the Phase 3 transcoder.

This module is **observational only**. It never mutates wire bodies,
never changes routing, and never raises on malformed input. Its sole
job is to surface a structured, deterministic view of how
``cache_control`` annotations survive translation between OpenAI and
Anthropic request formats, and to support prefix-stable serialisation
of the parts of a body that the provider is allowed to cache.

The transcoder wires these helpers into :class:`TranscodeContext` so
every finalised request can report its cache-boundary annotations
without re-parsing the upstream payload. Routing (the
:class:`QuotaFairScorer`) does **not** consume this metadata — that
invariant is asserted by ``tests/unit/test_routing.py``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, cast

# Public annotation kinds emitted by the transcoder.
#
# * ``preserved`` — cache_control annotation kept at the same path on
#   both sides of the protocol translation.
# * ``preserved_relocated`` — cache_control annotation kept but
#   rewritten into a different target path that the target protocol
#   understands natively.
# * ``dropped_unsupported_target`` — the target protocol has no
#   equivalent cache_control primitive; the annotation was dropped
#   explicitly. Loss is intentional.
# * ``dropped_feature_disabled`` — the transcoder policy disabled
#   cache_control preservation for this request. Loss is policy-driven.
# * ``dropped_invalid_shape`` — cache_control annotation had a
#   non-conforming shape and could not be carried across. Loss is
#   defensive.
# * ``synthesized`` — reserved. Phase 3 never emits ``synthesized``;
#   it exists for forward-compatibility with Phase 4 work that may
#   add provider-native cache hints.
CACHE_BOUNDARY_KIND_PRESERVED: str = "preserved"
CACHE_BOUNDARY_KIND_PRESERVED_RELOCATED: str = "preserved_relocated"
CACHE_BOUNDARY_KIND_DROPPED_UNSUPPORTED_TARGET: str = "dropped_unsupported_target"
CACHE_BOUNDARY_KIND_DROPPED_FEATURE_DISABLED: str = "dropped_feature_disabled"
CACHE_BOUNDARY_KIND_DROPPED_INVALID_SHAPE: str = "dropped_invalid_shape"
CACHE_BOUNDARY_KIND_SYNTHESIZED: str = "synthesized"

# Hard cap on the number of annotations recorded per request. The
# cap is defensive: pathological payloads (e.g. tens of thousands of
# tool definitions) must not be able to grow the tracker without
# bound. 64 is well above any realistic cache-control fan-out and
# matches Anthropic's documented four-breakpoint model with a wide
# safety margin.
CACHE_BOUNDARY_ANNOTATION_CAP: int = 64


@dataclass(slots=True, frozen=True)
class CacheBoundaryAnnotation:
    """Structured record of a single cache_control event.

    Frozen so it can be hashed, compared, and serialised without
    surprising callers. ``source_path`` and ``target_path`` are dot
    paths rooted at the request body; ``None`` for ``target_path``
    means the annotation was dropped rather than relocated.
    """

    kind: str
    source_protocol: str
    target_protocol: str
    source_path: str
    target_path: str | None
    cache_control_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-friendly dict."""
        return asdict(self)


@dataclass(slots=True)
class CacheBoundaryTracker:
    """Append-only, bounded tracker of cache boundary events."""

    annotations: list[CacheBoundaryAnnotation] = field(  # pyright: ignore[reportUnknownVariableType]
        default_factory=list
    )
    dropped_count: int = 0

    def record(self, annotation: CacheBoundaryAnnotation) -> None:
        """Append an annotation, honouring the per-request cap.

        When the cap is reached, additional annotations are silently
        dropped and ``dropped_count`` is incremented. The transcoder
        surfaces ``dropped_count`` so operators can detect truncation.
        """
        if len(self.annotations) >= CACHE_BOUNDARY_ANNOTATION_CAP:
            self.dropped_count += 1
            return
        self.annotations.append(annotation)

    def to_list(self) -> list[dict[str, Any]]:
        """Serialise all annotations to JSON-friendly dicts."""
        return [annotation.to_dict() for annotation in self.annotations]


def extract_cache_control_type(cache_control: Any) -> str | None:
    """Return the ``type`` field of a cache_control annotation, if any.

    Returns ``None`` for any non-dict input, for dicts that lack a
    ``type`` key, or for non-string ``type`` values. The transcoder
    uses this helper to validate ``cache_control`` shapes before
    propagating them across the protocol boundary.
    """
    if not isinstance(cache_control, dict):
        return None
    type_value = cast("Any", cache_control).get("type")
    if not isinstance(type_value, str):
        return None
    return type_value


def extract_cache_boundaries(body: Any) -> list[tuple[str, str | None]]:
    """Walk a wire body and list every cache_control annotation.

    Returns ``(dot_path, cache_control_type)`` pairs in document
    order. The walk is intentionally structural — it only inspects
    containers it recognises (Anthropic-style and OpenAI-style
    requests). Inputs that the walk cannot classify yield an empty
    list rather than raising; the segmenter treats unrecognised
    payloads the same way.

    The walk handles:

    * Anthropic ``system`` as a string or list of blocks.
    * Anthropic ``tools[].cache_control``.
    * Anthropic ``messages[].content`` as a string or list of
      blocks (text/image/tool_use/tool_result), each of which may
      carry ``cache_control``.
    """
    if not isinstance(body, dict):
        return []
    body_dict = cast("dict[str, Any]", body)
    found: list[tuple[str, str | None]] = []

    system_raw = body_dict.get("system")
    if isinstance(system_raw, str):
        # System strings cannot carry a per-block cache_control, so
        # the walk records nothing here.
        pass
    elif isinstance(system_raw, list):
        for index, block in enumerate(cast("list[Any]", system_raw)):
            if not isinstance(block, dict):
                continue
            block_dict = cast("dict[str, Any]", block)
            cache_control = block_dict.get("cache_control")
            if cache_control is not None:
                cache_type = extract_cache_control_type(cache_control)
                found.append((f"system[{index}].cache_control", cache_type))

    tools_raw = body_dict.get("tools")
    if isinstance(tools_raw, list):
        for index, tool in enumerate(cast("list[Any]", tools_raw)):
            if not isinstance(tool, dict):
                continue
            tool_dict = cast("dict[str, Any]", tool)
            cache_control = tool_dict.get("cache_control")
            if cache_control is not None:
                cache_type = extract_cache_control_type(cache_control)
                found.append((f"tools[{index}].cache_control", cache_type))

    messages_raw = body_dict.get("messages")
    if isinstance(messages_raw, list):
        for message_index, message in enumerate(cast("list[Any]", messages_raw)):
            if not isinstance(message, dict):
                continue
            message_dict = cast("dict[str, Any]", message)
            content_raw = message_dict.get("content")
            if isinstance(content_raw, str):
                continue
            if not isinstance(content_raw, list):
                continue
            for block_index, block in enumerate(cast("list[Any]", content_raw)):
                if not isinstance(block, dict):
                    continue
                block_dict = cast("dict[str, Any]", block)
                cache_control = block_dict.get("cache_control")
                if cache_control is None:
                    continue
                cache_type = extract_cache_control_type(cache_control)
                found.append(
                    (
                        f"messages[{message_index}].content"
                        f"[{block_index}].cache_control",
                        cache_type,
                    )
                )

    return found


def stable_dumps(payload: Any) -> str:
    """Deterministic JSON serialisation for prefix hashing.

    Sort keys, no ASCII escaping, and a ``str`` default so timestamps
    and decimal-like objects serialise stably. This matches the
    segmenter's serialisation policy so two requests with the same
    structural descriptor produce the same hash regardless of dict
    ordering on the wire.
    """
    return json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        default=str,
        separators=(",", ":"),
    )


def stable_hash(payload: Any) -> str:
    """SHA-256 of :func:`stable_dumps`, returned as hex.

    Used to compare the provider-visible stable prefix of two
    structurally-equivalent bodies. Never hashes raw prompt text —
    callers must pre-process the payload to strip volatile fields
    (timestamps, request ids, tool-call ids) before passing it in.
    """
    return hashlib.sha256(stable_dumps(payload).encode("utf-8")).hexdigest()


def extract_provider_visible_prefix(body: Any) -> dict[str, Any] | None:
    """Return the prefix of a body that the provider is allowed to cache.

    The prefix contains everything *except* the volatile suffix:

    * ``messages[-1]`` for chat requests (the user turn that triggered
      the call) is dropped, since adding new turns must invalidate
      the cache.
    * The top-level ``stream`` flag is dropped, since streaming vs.
      non-streaming must invalidate the cache.

    If the body is not a dict or carries no recognisable structure,
    returns ``None``. The transcoder uses this prefix to compare
    cache-equivalent requests across protocol translations.
    """
    if not isinstance(body, dict):
        return None
    body_dict = cast("dict[str, Any]", body)
    prefix: dict[str, Any] = {}

    for key, value in body_dict.items():
        if key == "messages":
            if not isinstance(value, list):
                continue
            message_list = cast("list[Any]", value)
            if not message_list:
                continue
            prefix["messages"] = [
                cast("dict[str, Any]", message)
                for message in message_list[:-1]
                if isinstance(message, dict)
            ]
            continue
        if key == "stream":
            continue
        prefix[key] = value

    return prefix
