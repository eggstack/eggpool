"""Quota management package for account-based rate limiting.

.. deprecated::
    ReservationManager is retained for backward compatibility only.
    SQLite reservations and QuotaEstimator in-memory tracking are canonical.
"""

from __future__ import annotations

from eggpool.quota.estimation import QuotaEstimator
from eggpool.quota.reservation import ReservationManager

__all__ = ["QuotaEstimator", "ReservationManager"]
