"""Reservation management for atomic quota tracking.

.. deprecated::
    This in-memory ReservationManager is no longer used for routing or scoring.
    SQLite reservations (via ReservationRepository) and QuotaEstimator's
    in-memory reservation cost tracking are the canonical mechanisms.
    This module is retained only for backward-compatible imports and unit tests.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from go_aggregator.db.connection import Database


@dataclass
class Reservation:
    """Represents a quota reservation for a request."""

    reservation_id: str
    account_name: str
    estimated_tokens: int
    estimated_cost_microdollars: int
    request_id: str
    created_at: float
    expires_at: float
    released: bool = False
    released_at: float | None = None
    release_reason: str = ""

    def is_expired(self, current_time: float | None = None) -> bool:
        """Check if reservation has expired."""
        if current_time is None:
            current_time = time.time()
        return current_time > self.expires_at

    def release(self, reason: str = "", timestamp: float | None = None) -> None:
        """Mark reservation as released."""
        if self.released:
            return
        self.released = True
        self.released_at = timestamp or time.time()
        self.release_reason = reason


@dataclass
class ReservationManager:
    """Manages atomic reservations for quota-aware routing."""

    reservation_ttl_seconds: float = 300.0  # 5 minutes default TTL
    _reservations: dict[str, Reservation] = field(
        default_factory=dict[str, Reservation]
    )
    _account_reservations: dict[str, list[str]] = field(
        default_factory=dict[str, list[str]]
    )

    def create_reservation(
        self,
        account_name: str,
        estimated_tokens: int,
        estimated_cost_microdollars: int,
        request_id: str,
        ttl_seconds: float | None = None,
    ) -> Reservation:
        """Create a new reservation for an account."""
        reservation_id = str(uuid.uuid4())
        now = time.time()
        ttl = ttl_seconds or self.reservation_ttl_seconds

        reservation = Reservation(
            reservation_id=reservation_id,
            account_name=account_name,
            estimated_tokens=estimated_tokens,
            estimated_cost_microdollars=estimated_cost_microdollars,
            request_id=request_id,
            created_at=now,
            expires_at=now + ttl,
        )

        self._reservations[reservation_id] = reservation
        if account_name not in self._account_reservations:
            self._account_reservations[account_name] = []
        self._account_reservations[account_name].append(reservation_id)

        return reservation

    def release_reservation(
        self, reservation_id: str, reason: str = "completed"
    ) -> Reservation | None:
        """Release a reservation."""
        reservation = self._reservations.get(reservation_id)
        if reservation is None:
            return None

        reservation.release(reason)
        return reservation

    def release_account_reservations(
        self, account_name: str, reason: str = "completed"
    ) -> int:
        """Release all reservations for an account."""
        reservation_ids = self._account_reservations.get(account_name, [])
        released_count = 0
        for rid in reservation_ids:
            reservation = self._reservations.get(rid)
            if reservation and not reservation.released:
                reservation.release(reason)
                released_count += 1
        return released_count

    def get_account_reservations(self, account_name: str) -> list[Reservation]:
        """Get all active reservations for an account."""
        reservation_ids = self._account_reservations.get(account_name, [])
        return [
            self._reservations[rid]
            for rid in reservation_ids
            if rid in self._reservations
            and not self._reservations[rid].released
            and not self._reservations[rid].is_expired()
        ]

    def get_account_reserved_usage(self, account_name: str) -> tuple[int, int]:
        """Get total reserved usage for an account (tokens, cost)."""
        reservations = self.get_account_reservations(account_name)
        total_tokens = sum(r.estimated_tokens for r in reservations)
        total_cost = sum(r.estimated_cost_microdollars for r in reservations)
        return total_tokens, total_cost

    def reconcile_reservations(self, timestamp: float | None = None) -> int:
        """Reconcile expired reservations and return count of cleaned up."""
        if timestamp is None:
            timestamp = time.time()

        cleaned_count = 0
        for reservation in list(self._reservations.values()):
            if reservation.is_expired(timestamp) and not reservation.released:
                reservation.release("expired")
                cleaned_count += 1

        return cleaned_count

    def cleanup_old_reservations(self, max_age_seconds: float = 86400.0) -> int:
        """Remove old reservations from memory."""
        now = time.time()
        cutoff = now - max_age_seconds
        to_remove: list[str] = []

        for rid, reservation in self._reservations.items():
            if reservation.created_at < cutoff:
                to_remove.append(rid)

        for rid in to_remove:
            reservation = self._reservations.pop(rid)
            if reservation.account_name in self._account_reservations:
                self._account_reservations[reservation.account_name] = [
                    r
                    for r in self._account_reservations[reservation.account_name]
                    if r != rid
                ]

        return len(to_remove)

    async def persist_reservations(self, db: Database) -> None:
        """Persist reservations to database.

        .. deprecated::
            This method references obsolete schema columns and is no
            longer compatible with the current database.  Use
            ``ReservationRepository`` instead.
        """
        raise NotImplementedError(
            "persist_reservations() is deprecated and incompatible "
            "with the current schema; use ReservationRepository instead"
        )

    async def load_reservations(self, db: Database) -> None:
        """Load active reservations from database.

        .. deprecated::
            This method references obsolete schema columns and is no
            longer compatible with the current database.  Use
            ``ReservationRepository`` instead.
        """
        raise NotImplementedError(
            "load_reservations() is deprecated and incompatible "
            "with the current schema; use ReservationRepository instead"
        )
