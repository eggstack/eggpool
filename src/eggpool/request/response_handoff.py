"""Request-owned state for the downstream ASGI response boundary."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class ResponseHandoffState:
    """Monotonic fact that the proxy response start was sent or attempted."""

    started: bool = False

    def mark_started(self) -> None:
        """Record response handoff; repeated marking is intentionally harmless."""
        self.started = True
