"""Quota management package for account-based rate limiting.

.. deprecated::
    ReservationManager is retained for backward compatibility only.
    SQLite reservations and QuotaEstimator in-memory tracking are canonical.
"""

from __future__ import annotations

from go_aggregator.quota.estimation import QuotaEstimator
from go_aggregator.quota.reservation import ReservationManager

__all__ = ["QuotaEstimator", "ReservationManager"]
