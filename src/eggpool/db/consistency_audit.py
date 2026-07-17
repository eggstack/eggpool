"""Read-only database consistency audit helper (Workstream G7).

Checks lifecycle invariants without mutating the database. Uses the
read-only stats connection where possible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eggpool.db.connection import Database


@dataclass(frozen=True)
class AuditViolation:
    """A single consistency violation found during audit."""

    check_name: str
    description: str
    row_count: int = 0
    sample_ids: tuple[str, ...] = ()
    severity: str = "error"


@dataclass
class AuditResult:
    """Result of a consistency audit pass."""

    violations: list[AuditViolation] = field(default_factory=list[AuditViolation])
    checks_run: int = 0
    checks_passed: int = 0

    @property
    def passed(self) -> bool:
        return len(self.violations) == 0

    @property
    def failed_count(self) -> int:
        return len(self.violations)

    def to_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "checks_run": self.checks_run,
            "checks_passed": self.checks_passed,
            "violations": [
                {
                    "check_name": v.check_name,
                    "description": v.description,
                    "row_count": v.row_count,
                    "sample_ids": list(v.sample_ids),
                    "severity": v.severity,
                }
                for v in self.violations
            ],
        }


class ConsistencyAuditor:
    """Read-only database consistency auditor.

    Performs invariant checks without mutating the database. Uses the
    read-only stats connection where possible for minimal contention.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    async def run_full_audit(self) -> AuditResult:
        """Run all consistency checks and return aggregate result."""
        result = AuditResult()
        checks = [
            self.check_pending_without_attempt,
            self.check_active_reservation_for_non_pending,
            self.check_incomplete_attempt_for_terminal,
            self.check_duplicate_attempt_numbers,
            self.check_no_orphan_routing_traces,
        ]
        for check_fn in checks:
            result.checks_run += 1
            try:
                violation = await check_fn()
                if violation is None:
                    result.checks_passed += 1
                else:
                    result.violations.append(violation)
            except Exception as exc:
                result.violations.append(
                    AuditViolation(
                        check_name=check_fn.__name__,
                        description=f"Check raised exception: {exc}",
                        severity="error",
                    )
                )
        return result

    async def check_pending_without_attempt(self) -> AuditViolation | None:
        """Check for pending requests with no active or completed attempt."""
        rows = await self._db.fetch_all(
            """
            SELECT r.id
            FROM requests r
            WHERE r.status = 'pending'
              AND NOT EXISTS (
                  SELECT 1 FROM request_attempts a
                  WHERE a.request_id = r.id
                    AND a.completed_at IS NOT NULL
              )
            LIMIT 10
            """
        )
        if not rows:
            return None
        return AuditViolation(
            check_name="pending_without_attempt",
            description=f"Found {len(rows)} pending requests with no active attempt",
            row_count=len(rows),
            sample_ids=tuple(str(r["id"]) for r in rows),
        )

    async def check_active_reservation_for_non_pending(self) -> AuditViolation | None:
        """Check for active reservations on non-pending requests."""
        rows = await self._db.fetch_all(
            """
            SELECT resv.id, resv.request_id
            FROM reservations resv
            JOIN requests r ON r.id = resv.request_id
            WHERE resv.status = 'active'
              AND r.status != 'pending'
            LIMIT 10
            """
        )
        if not rows:
            return None
        return AuditViolation(
            check_name="active_reservation_for_non_pending",
            description=(
                f"Found {len(rows)} active reservations for non-pending requests"
            ),
            row_count=len(rows),
            sample_ids=tuple(str(r["id"]) for r in rows),
        )

    async def check_incomplete_attempt_for_terminal(self) -> AuditViolation | None:
        """Check for incomplete attempts on terminal requests."""
        rows = await self._db.fetch_all(
            """
            SELECT a.id, a.request_id
            FROM request_attempts a
            JOIN requests r ON r.id = a.request_id
            WHERE r.status IN (
                'completed', 'client_error', 'upstream_error',
                'midstream_error', 'client_cancelled', 'timeout',
                'interrupted'
              )
              AND a.completed_at IS NULL
            LIMIT 10
            """
        )
        if not rows:
            return None
        return AuditViolation(
            check_name="incomplete_attempt_for_terminal",
            description=(f"Found {len(rows)} incomplete attempts on terminal requests"),
            row_count=len(rows),
            sample_ids=tuple(str(r["id"]) for r in rows),
        )

    async def check_duplicate_attempt_numbers(self) -> AuditViolation | None:
        """Check for duplicate attempt numbers within the same request."""
        rows = await self._db.fetch_all(
            """
            SELECT request_id, attempt_number, COUNT(*) as cnt
            FROM request_attempts
            GROUP BY request_id, attempt_number
            HAVING cnt > 1
            LIMIT 10
            """
        )
        if not rows:
            return None
        return AuditViolation(
            check_name="duplicate_attempt_numbers",
            description=(f"Found {len(rows)} duplicate attempt number combinations"),
            row_count=len(rows),
            sample_ids=tuple(f"{r['request_id']}#{r['attempt_number']}" for r in rows),
        )

    async def check_no_orphan_routing_traces(self) -> AuditViolation | None:
        """Check for routing trace rows without a matching request."""
        rows = await self._db.fetch_all(
            """
            SELECT rd.id
            FROM routing_decisions rd
            WHERE NOT EXISTS (
                SELECT 1 FROM requests r
                WHERE r.id = rd.request_id
            )
            LIMIT 10
            """
        )
        if not rows:
            return None
        return AuditViolation(
            check_name="orphan_routing_traces",
            description=(
                f"Found {len(rows)} routing decisions without matching requests"
            ),
            row_count=len(rows),
            sample_ids=tuple(str(r["id"]) for r in rows),
            severity="warning",
        )
