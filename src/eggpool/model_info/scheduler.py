"""Scheduler for model-info refresh timing and prioritization."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eggpool.model_info.types import ModelInfoStatus
    from eggpool.models.config import ModelInfoConfig


@dataclass(frozen=True)
class RefreshDecision:
    """A single model's refresh decision."""

    model_id: str
    due: bool
    priority: int
    reason: str
    next_refresh_at: datetime


class ModelInfoRefreshScheduler:
    """Computes refresh timing based on model status, age, and config."""

    def __init__(self, config: ModelInfoConfig) -> None:
        self._config = config

    def next_refresh_for(
        self,
        *,
        status: ModelInfoStatus,
        first_seen_at: datetime,
        last_refreshed_at: datetime | None,
        now: datetime,
        has_conflicts: bool = False,
        source_cooldown_until: datetime | None = None,
    ) -> datetime:
        """Compute the next refresh time for a model based on its state."""
        age = now - first_seen_at
        accelerated_window = timedelta(days=self._config.sparse_new_accelerated_days)

        if status == "sparse_new":
            if age < timedelta(seconds=self._config.sparse_new_initial_ttl_s):
                return now + timedelta(seconds=self._config.sparse_new_initial_ttl_s)
            if age < accelerated_window:
                return now + timedelta(seconds=self._config.sparse_new_later_ttl_s)
            return now + timedelta(seconds=self._config.partial_ttl_s)

        if status == "conflicting":
            return now + timedelta(seconds=self._config.conflict_ttl_s)

        if status == "source_unavailable":
            if source_cooldown_until is not None and source_cooldown_until > now:
                return source_cooldown_until
            return now + timedelta(seconds=self._config.partial_ttl_s)

        if status == "partial":
            return now + timedelta(seconds=self._config.partial_ttl_s)

        if status == "fresh":
            return now + timedelta(seconds=self._config.known_ttl_s)

        # withdrawn, unmatched, manual_override, stale
        return now + timedelta(seconds=self._config.known_ttl_s)

    def rank_due_work(
        self,
        candidates: list[tuple[str, ModelInfoStatus, datetime, datetime | None]],
        now: datetime,
    ) -> list[RefreshDecision]:
        """Rank due models by priority and compute next refresh times.

        Args:
            candidates: List of (model_id, status, first_seen_at, last_refreshed_at).
            now: Current timestamp.

        Returns:
            Sorted list of RefreshDecision with due=True models first,
            ordered by priority (lower = more urgent).
        """
        decisions: list[RefreshDecision] = []
        for model_id, status, first_seen_at, last_refreshed_at in candidates:
            next_refresh_at = self.next_refresh_for(
                status=status,
                first_seen_at=first_seen_at,
                last_refreshed_at=last_refreshed_at,
                now=now,
            )
            due = next_refresh_at <= now
            priority = _status_priority(status)
            reason = _refresh_reason(status, due, first_seen_at, now)
            decisions.append(
                RefreshDecision(
                    model_id=model_id,
                    due=due,
                    priority=priority,
                    reason=reason,
                    next_refresh_at=next_refresh_at,
                )
            )
        # Sort: due first, then by priority (lower = more urgent),
        # then by next_refresh_at
        decisions.sort(key=lambda d: (not d.due, d.priority, d.next_refresh_at))
        return decisions


def _status_priority(status: ModelInfoStatus) -> int:
    """Map status to a numeric priority (lower = more urgent)."""
    from eggpool.model_info.types import STATUS_PRIORITY

    return STATUS_PRIORITY.get(status, 9)


def _refresh_reason(
    status: ModelInfoStatus,
    due: bool,
    first_seen_at: datetime,
    now: datetime,
) -> str:
    """Generate a human-readable reason for the refresh decision."""
    if not due:
        return f"Status '{status}' — not yet due"
    age_days = (now - first_seen_at).days
    if status == "sparse_new":
        return f"Newly discovered ({age_days}d ago), accelerated refresh"
    if status == "conflicting":
        return "Conflicting metadata, periodic re-check"
    if status == "source_unavailable":
        return "Source was unavailable, retry"
    if status == "partial":
        return f"Partial metadata ({age_days}d old), periodic refresh"
    if status == "fresh":
        return f"Known model ({age_days}d old), TTL refresh"
    return f"Status '{status}', due for refresh"
