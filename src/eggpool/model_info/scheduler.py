"""Scheduler for model-info refresh timing and prioritization."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eggpool.model_info.types import ModelInfoStatus
    from eggpool.models.config import ModelInfoConfig


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
