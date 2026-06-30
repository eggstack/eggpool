"""Lightweight in-memory fairness rotor for same-tier account rotation."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eggpool.accounts.state import AccountRuntimeState
    from eggpool.quota.scorer import RoutingScore


_ROTOR_HARD_CAP = 4096


@dataclass(frozen=True, slots=True)
class FairnessKey:
    """Immutable key identifying a fairness rotation group."""

    provider_id: str | None
    model_id: str
    protocol: str | None
    priority: int
    client_protocol: str | None = None


@dataclass(frozen=True, slots=True)
class FairnessDecision:
    """Metadata about a fairness rotation decision."""

    mode: str
    applied: bool
    key: str
    candidate_count: int
    scope: str = "provider_model_protocol"
    selected_index: int | None = None
    selected_account_name: str | None = None
    reason: str = ""

    def to_dict(self) -> dict[str, object]:
        """Serialize to a JSON-friendly dict for score_components_json."""
        return {
            "mode": self.mode,
            "applied": self.applied,
            "scope": self.scope,
            "key": self.key,
            "candidate_count": self.candidate_count,
            "selected_index": self.selected_index,
            "selected_account_name": self.selected_account_name,
            "reason": self.reason,
        }


def _make_key_string(key: FairnessKey) -> str:
    """Render a FairnessKey into a canonical string for trace storage."""
    parts = [
        f"provider={key.provider_id or '*'}",
        f"model={key.model_id}",
        f"protocol={key.protocol or '*'}",
        f"tier={key.priority}",
    ]
    if key.client_protocol is not None:
        parts.append(f"client_protocol={key.client_protocol}")
    return "|".join(parts)


class FairnessRotor:
    """In-memory round-robin rotor for same-tier peer accounts.

    Maintains a per-key position counter.  The rotor is lock-free except
    for the brief position read/update under ``_lock``.  Restart resets
    all positions to zero — durable round-robin state is explicitly out
    of scope for this pass.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._positions: dict[str, int] = {}

    async def rotate(
        self,
        key: FairnessKey,
        candidates: list[tuple[AccountRuntimeState, RoutingScore]],
        *,
        scope: str = "provider_model_protocol",
    ) -> tuple[list[tuple[AccountRuntimeState, RoutingScore]], FairnessDecision]:
        """Rotate *candidates* by the current rotor position for *key*.

        Returns the rotated list and a :class:`FairnessDecision` trace.
        Candidates are sorted by account name before rotation so the
        ordering is deterministic and independent of config insertion.
        """
        key_str = _make_key_string(key)
        n = len(candidates)

        if n == 0:
            return candidates, FairnessDecision(
                mode="round_robin",
                applied=False,
                key=key_str,
                candidate_count=0,
                scope=scope,
                reason="no_candidates",
            )

        if n == 1:
            return candidates, FairnessDecision(
                mode="round_robin",
                applied=False,
                key=key_str,
                candidate_count=1,
                scope=scope,
                selected_index=0,
                selected_account_name=candidates[0][0].name,
                reason="single_candidate",
            )

        sorted_cands = sorted(candidates, key=lambda pair: pair[0].name)

        async with self._lock:
            position = self._positions.get(key_str, 0)
            if len(self._positions) >= _ROTOR_HARD_CAP:
                self._positions.clear()
            self._positions[key_str] = (position + 1) % n

        start = position % n
        rotated = sorted_cands[start:] + sorted_cands[:start]

        return rotated, FairnessDecision(
            mode="round_robin",
            applied=True,
            key=key_str,
            candidate_count=n,
            scope=scope,
            selected_index=0,
            selected_account_name=rotated[0][0].name,
            reason="ok",
        )
