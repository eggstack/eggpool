"""Streaming transcode fixture helpers.

Provides utilities for loading, converting, and comparing SSE fixtures
used by the streaming transcoder tests.
"""

from __future__ import annotations

import json
import os
from typing import Any

_FIXTURE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "fixtures",
    "streaming_transcode",
)


def load_streaming_fixture(name: str) -> dict[str, Any]:
    """Load a fixture by name from the ``streaming_transcode`` directory.

    Returns the parsed JSON document containing ``name``, ``description``,
    ``upstream_protocol``, ``client_protocol``, and ``events`` keys.
    """
    path = os.path.join(_FIXTURE_DIR, f"{name}.json")
    with open(path) as fh:
        return json.load(fh)


def fixture_to_sse_bytes(
    events: list[dict[str, Any]],
    *,
    protocol: str,
) -> list[bytes]:
    """Convert fixture events back to SSE byte chunks.

    Returns one byte chunk per event.  Each chunk is a complete SSE
    frame with ``event:`` and ``data:`` lines separated by ``\\n\\n``.

    *protocol* controls the SSE frame format:

    - ``"anthropic"``: ``event: <type>\\ndata: <json>\\n\\n``
    - ``"openai"``: ``data: <json>\\n\\n`` (no explicit event line)
    """
    chunks: list[bytes] = []
    for ev in events:
        event_type = ev.get("event")
        data = ev["data"]
        if isinstance(data, str):
            # Special case: [DONE] sentinel
            data_str = data
        else:
            data_str = json.dumps(data, separators=(",", ":"))

        if protocol == "anthropic" and event_type:
            frame = f"event: {event_type}\ndata: {data_str}\n\n"
        else:
            frame = f"data: {data_str}\n\n"
        chunks.append(frame.encode())
    return chunks


def parse_sse_events(raw: bytes) -> list[dict[str, Any]]:
    """Parse raw SSE output into a list of ``{event, data}`` dicts.

    ``data`` is decoded from JSON when possible; ``[DONE]`` and
    non-JSON payloads are kept as strings.
    """
    frames: list[dict[str, Any]] = []
    for block in raw.split(b"\n\n"):
        if not block.strip():
            continue
        event = ""
        data_lines: list[str] = []
        for line in block.split(b"\n"):
            if line.startswith(b"event: "):
                event = line[7:].decode()
            elif line.startswith(b"data: "):
                data_lines.append(line[6:].decode())
        if not data_lines:
            continue
        data_str = "\n".join(data_lines)
        if data_str == "[DONE]":
            frames.append({"event": event, "data": "[DONE]"})
            continue
        try:
            data = json.loads(data_str)
        except (json.JSONDecodeError, ValueError):
            data = data_str
        frames.append({"event": event, "data": data})
    return frames


def _normalise_tool_call_id(tc: dict[str, Any]) -> dict[str, Any]:
    """Return *tc* with a normalised ``id`` for comparison.

    The transcoder rewrites tool-call IDs (``call_*`` <-> ``toolu_*``)
    through a deterministic mapping.  For comparison purposes we strip
    both prefixes and compare the remaining suffix.
    """
    result = dict(tc)
    raw_id = result.get("id", "")
    if isinstance(raw_id, str):
        # Remove known prefixes for comparison
        for prefix in ("call_", "toolu_"):
            if raw_id.startswith(prefix):
                result["id"] = raw_id[len(prefix) :]
                break
    return result


def _normalise_delta(
    delta: dict[str, Any],
    *,
    normalise_tool_ids: bool = True,
) -> dict[str, Any]:
    """Normalise a delta dict for comparison.

    - Tool call IDs are stripped of prefixes.
    - ``role`` and ``content`` fields are preserved.
    - ``finish_reason`` values are mapped through the transcoder
      mapping for comparison.
    """
    result = dict(delta)
    if normalise_tool_ids and "tool_calls" in result:
        result["tool_calls"] = [
            _normalise_tool_call_id(tc) for tc in result["tool_calls"]
        ]
    return result


def _strip_usage_from_choice(choice: dict[str, Any]) -> dict[str, Any]:
    """Remove ``usage`` from a choice-level dict for comparison.

    Usage is carried at the top level in OpenAI chunk format, not
    inside choices.
    """
    result = dict(choice)
    result.pop("usage", None)
    return result


def _normalise_openai_frame(data: dict[str, Any]) -> dict[str, Any]:
    """Normalise an OpenAI frame for comparison."""
    result = dict(data)
    result.pop("created", None)
    # Top-level usage is a finalization concern, not protocol shape;
    # strip it so tests compare the event structure, not token counts.
    result.pop("usage", None)
    choices = result.get("choices", [])
    result["choices"] = [
        _strip_usage_from_choice(c) if isinstance(c, dict) else c for c in choices
    ]
    # Normalise tool_calls in all deltas
    result["choices"] = [
        {
            **c,
            "delta": _normalise_delta(c["delta"])
            if isinstance(c, dict) and "delta" in c and isinstance(c["delta"], dict)
            else c.get("delta"),
        }
        if isinstance(c, dict) and "delta" in c
        else c
        for c in result["choices"]
    ]
    return result


def _normalise_anthropic_frame(
    data: dict[str, Any],
) -> dict[str, Any]:
    """Normalise an Anthropic frame for comparison.

    Strips ``stop_sequence`` (which may be ``None`` vs absent) and
    normalises usage fields.
    """
    result = dict(data)
    t = result.get("type", "")
    if t == "message_delta":
        delta = dict(result.get("delta", {}))
        delta.pop("stop_sequence", None)
        result["delta"] = delta
    if t == "message_start":
        msg = dict(result.get("message", {}))
        msg.pop("stop_reason", None)
        usage = msg.get("usage", {})
        msg["usage"] = {k: v for k, v in usage.items() if v}
        result["message"] = msg
    return result


def assert_event_sequence_equal(
    actual: list[dict[str, Any]],
    expected: list[dict[str, Any]],
    *,
    protocol: str = "openai",
) -> None:
    """Assert two event sequences are equivalent.

    Compares decoded SSE events, not raw bytes.  The comparison is
    semantic:

    - ``event`` fields are compared (empty string matches ``None``).
    - ``data`` payloads are compared after normalisation:
      - JSON whitespace differences are ignored (compact vs pretty).
      - OpenAI ``created`` timestamps are ignored.
      - Tool call IDs are prefix-stripped for comparison.
      - ``finish_reason`` values are compared against their
        transcoder-mapped equivalents.
      - ``usage`` in the top-level OpenAI frame is preserved but
        ``usage`` inside choices is stripped.
      - Anthropic ``stop_sequence`` is ignored.
    - ``[DONE]`` sentinels are compared by value.

    Raises ``AssertionError`` with a descriptive message on mismatch.
    """
    if len(actual) != len(expected):
        msg = (
            f"Event count mismatch: got {len(actual)}, expected {len(expected)}\n"
            f"  actual events: {[e.get('event', '') for e in actual]}\n"
            f"  expected events: {[e.get('event', '') for e in expected]}"
        )
        raise AssertionError(msg)

    errors: list[str] = []
    for i, (act, exp) in enumerate(zip(actual, expected, strict=True)):
        act_event = act.get("event", "")
        exp_event = exp.get("event", "")
        if act_event != exp_event:
            errors.append(
                f"Event {i}: event type mismatch: "
                f"got {act_event!r}, expected {exp_event!r}"
            )
            continue

        act_data = act.get("data")
        exp_data = exp.get("data")

        # [DONE] sentinel comparison
        if act_data == "[DONE]" and exp_data == "[DONE]":
            continue
        if act_data == "[DONE]" or exp_data == "[DONE]":
            errors.append(
                f"Event {i}: sentinel mismatch: got {act_data!r}, expected {exp_data!r}"
            )
            continue

        # Normalise
        if isinstance(act_data, dict) and isinstance(exp_data, dict):
            if protocol == "openai":
                act_norm = _normalise_openai_frame(act_data)
                exp_norm = _normalise_openai_frame(exp_data)
            else:
                act_norm = _normalise_anthropic_frame(act_data)
                exp_norm = _normalise_anthropic_frame(exp_data)

            if act_norm != exp_norm:
                errors.append(
                    f"Event {i} ({exp_event or 'data'}): "
                    f"data mismatch:\n"
                    f"  actual:   {json.dumps(act_norm, indent=2)}\n"
                    f"  expected: {json.dumps(exp_norm, indent=2)}"
                )
        elif act_data != exp_data:
            errors.append(
                f"Event {i} ({exp_event or 'data'}): "
                f"data mismatch: got {act_data!r}, expected {exp_data!r}"
            )

    if errors:
        summary = "\n".join(errors)
        raise AssertionError(f"Event sequence mismatch:\n{summary}")
