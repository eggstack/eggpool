"""Bounded, protocol-neutral incremental Server-Sent Events framing."""

from __future__ import annotations

import codecs
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

from eggpool.jsonx import loads as jsonx_loads

MAX_SSE_FRAME_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class SSEFrame:
    """One assembled SSE event; framing never interprets its payload."""

    event: str | None
    data: str
    fields: tuple[tuple[str, str], ...] | None = None
    is_comment_only: bool = False
    byte_count: int = 0


class DecodedSSEFrame:
    """An SSE frame with a lazy, shared JSON-object parse cache."""

    __slots__ = ("frame", "_json_cache", "_json_parsed")

    def __init__(self, frame: SSEFrame) -> None:
        self.frame = frame
        self._json_cache: dict[str, Any] | None = None
        self._json_parsed = False

    def json_object(
        self,
        parser: Callable[[str], Any] = jsonx_loads,
    ) -> dict[str, Any] | None:
        """Parse a JSON object at most once; malformed data returns ``None``."""
        if not self._json_parsed:
            self._json_parsed = True
            try:
                value = parser(self.frame.data)
            except (ValueError, TypeError):
                value = None
            if isinstance(value, dict):
                self._json_cache = value
        return self._json_cache


@dataclass(frozen=True, slots=True)
class SSEDecodeResult:
    """Evidence produced when an :class:`SSEDecoder` reaches EOF."""

    frames: tuple[DecodedSSEFrame, ...]
    incomplete_frame: bool
    invalid_utf8_replacements: int
    discarded_frame_count: int


class SSEDecoder:
    """Incrementally decode UTF-8 and assemble bounded SSE events."""

    def __init__(self, *, max_frame_bytes: int = MAX_SSE_FRAME_BYTES) -> None:
        self._max_frame_bytes = max_frame_bytes
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._line_buffer = ""
        self._pending_cr = False
        self._fields: list[tuple[str, str]] = []
        self._data_lines: list[str] = []
        self._event: str | None = None
        self._frame_bytes = 0
        self._discarding_line = False
        self._discarding_frame = False
        self._discarded_frames = 0
        self._invalid_utf8 = 0
        self._structural_errors = 0

    @staticmethod
    def _utf8_len(value: str) -> int:
        return len(value) if value.isascii() else len(value.encode("utf-8"))

    def feed(self, chunk: bytes) -> list[DecodedSSEFrame]:
        """Consume bytes and return all complete frames found in ``chunk``."""
        text = self._decoder.decode(chunk)
        self._invalid_utf8 += text.count("\ufffd")
        if self._pending_cr:
            text = ("\n" if not text.startswith("\n") else "") + text
            self._pending_cr = False
        if text.endswith("\r"):
            text = text[:-1]
            self._pending_cr = True
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = (self._line_buffer + text).split("\n")
        self._line_buffer = lines.pop()
        frames: list[DecodedSSEFrame] = []
        for line in lines:
            if self._discarding_line:
                self._discarding_line = False
                continue
            if not line:
                frame = self._emit_frame()
                if frame is not None:
                    frames.append(frame)
                continue
            self._process_line(line)
        if self._utf8_len(self._line_buffer) > self._max_frame_bytes:
            self._structural_errors += 1
            self._discarding_line = True
            self._discarding_frame = True
            self._line_buffer = ""
            self._reset_frame()
        return frames

    def _process_line(self, line: str) -> None:
        if self._discarding_frame:
            return
        line_bytes = self._utf8_len(line) + 1
        self._frame_bytes += line_bytes
        if self._frame_bytes > self._max_frame_bytes:
            self._structural_errors += 1
            self._discarding_frame = True
            self._reset_frame()
            return
        if line.startswith(":"):
            self._fields.append(("", line[1:]))
            return
        if ":" in line:
            name, value = line.split(":", 1)
            value = value[1:] if value.startswith(" ") else value
        else:
            name, value = line, ""
        self._fields.append((name, value))
        if name == "event":
            self._event = value
        elif name == "data":
            self._data_lines.append(value)

    def _reset_frame(self) -> None:
        self._fields.clear()
        self._data_lines.clear()
        self._event = None
        self._frame_bytes = 0

    def _emit_frame(self) -> DecodedSSEFrame | None:
        if self._discarding_frame:
            self._discarded_frames += 1
            self._discarding_frame = False
            self._reset_frame()
            return None
        if not self._fields:
            self._reset_frame()
            return None
        fields = tuple(self._fields)
        frame = SSEFrame(
            event=self._event,
            data="\n".join(self._data_lines),
            fields=fields,
            is_comment_only=bool(fields) and all(not name for name, _ in fields),
            byte_count=self._frame_bytes,
        )
        self._reset_frame()
        return DecodedSSEFrame(frame)

    def finish(self) -> SSEDecodeResult:
        """Drain the decoder and report whether EOF interrupted a frame."""
        remainder = self._decoder.decode(b"", True)
        self._invalid_utf8 += remainder.count("\ufffd")
        if remainder:
            self._line_buffer += remainder.replace("\r\n", "\n").replace("\r", "\n")
        if self._pending_cr:
            self._line_buffer += "\n"
            self._pending_cr = False
        incomplete = bool(self._line_buffer or self._fields or self._discarding_line)
        frames = self.feed(b"")
        if self._line_buffer and not self._discarding_line:
            self._process_line(self._line_buffer)
            self._line_buffer = ""
        frame = self._emit_frame()
        if frame is not None:
            frames.append(frame)
        self._line_buffer = ""
        return SSEDecodeResult(
            frames=tuple(frames),
            incomplete_frame=incomplete,
            invalid_utf8_replacements=self._invalid_utf8,
            discarded_frame_count=self._discarded_frames,
        )

    @property
    def structural_error_count(self) -> int:
        return self._structural_errors

    @property
    def line_buffer(self) -> str:
        return self._line_buffer
