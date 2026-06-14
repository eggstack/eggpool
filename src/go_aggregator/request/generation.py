"""Immutable runtime generation for atomic config reload.

.. deprecated::
    Hot-reload via SIGHUP is not supported. Configuration changes require a
    service restart. This module is retained for backward-compatible imports
    only. No signal handler is wired.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from go_aggregator.accounts.registry import AccountRegistry
    from go_aggregator.catalog.service import CatalogService
    from go_aggregator.health.health_manager import HealthManager
    from go_aggregator.quota.estimation import QuotaEstimator
    from go_aggregator.routing.router import Router


@dataclass(frozen=True)
class RuntimeGeneration:
    """An immutable snapshot of all runtime state.

    On config reload, a new generation is constructed and atomically
    swapped into app.state. In-flight requests continue using the old
    generation until they complete.
    """

    generation_id: int
    created_at: float = field(default_factory=time.time)
    registry: AccountRegistry | None = None
    catalog: CatalogService | None = None
    router: Router | None = None
    health_manager: HealthManager | None = None
    quota_estimator: QuotaEstimator | None = None
    _inflight_count: int = field(default=0, repr=False)

    def increment_inflight(self) -> None:
        """Track active requests (not frozen for this counter)."""
        # Using object.__setattr__ to bypass frozen
        object.__setattr__(self, "_inflight_count", self._inflight_count + 1)

    def decrement_inflight(self) -> None:
        object.__setattr__(self, "_inflight_count", self._inflight_count - 1)

    @property
    def in_flight(self) -> int:
        return self._inflight_count
