"""Process-owned background database writable probe.

Removes SQLite write activity from the ``/readyz`` request path by
executing a real writable probe on a bounded cadence and exposing a
cheap cached snapshot for readiness handlers.

The probe is **process-owned** — it survives generation swaps (rehash)
and is started once after database initialization, stopped before
database close.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


class ProbeStatus(enum.Enum):
    """Current status of the writable probe."""

    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    STALE = "stale"
    STOPPED = "stopped"


@dataclass
class ProbeSnapshot:
    """Immutable snapshot of the probe's current state.

    Returned to readiness handlers; never exposes raw SQL or secrets.
    """

    status: ProbeStatus
    last_attempt_at: float | None
    last_success_at: float | None
    last_failure_at: float | None
    last_error_class: str | None
    last_error_message: str | None
    last_probe_duration_ms: float | None
    consecutive_failures: int
    configured_interval_s: float
    freshness_deadline_s: float
    worker_running: bool
    probe_count: int = 0
    success_count: int = 0
    failure_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dictionary for diagnostics."""
        return {
            "status": self.status.value,
            "last_attempt_at": self.last_attempt_at,
            "last_success_at": self.last_success_at,
            "last_failure_at": self.last_failure_at,
            "last_error_class": self.last_error_class,
            "last_error_message": self.last_error_message,
            "last_probe_duration_ms": self.last_probe_duration_ms,
            "consecutive_failures": self.consecutive_failures,
            "configured_interval_s": self.configured_interval_s,
            "freshness_deadline_s": self.freshness_deadline_s,
            "worker_running": self.worker_running,
            "probe_count": self.probe_count,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
        }


class DatabaseWritableProbe:
    """Process-owned background probe that checks database writeability.

    The probe runs on a periodic cadence, performing a real write
    transaction (insert + rollback) and caching the result. The
    ``/readyz`` handler reads the cached snapshot without performing
    any write.

    Lifecycle:
        1. Construct after database initialization.
        2. Call ``start()`` to launch the background worker.
        3. Call ``stop()`` before database close.
    """

    def __init__(
        self,
        db: Any,
        *,
        interval_s: float = 10.0,
        freshness_s: float = 30.0,
        timeout_s: float = 5.0,
        initial_probe: bool = True,
    ) -> None:
        self._db = db
        self._interval_s = interval_s
        self._freshness_s = freshness_s
        self._timeout_s = timeout_s
        self._initial_probe = initial_probe
        self._task: asyncio.Task[None] | None = None
        self._running = False

        # Cached state (guarded by _lock for cross-task safety)
        self._lock = asyncio.Lock()
        self._status: ProbeStatus = ProbeStatus.UNKNOWN
        self._last_attempt_at: float | None = None
        self._last_success_at: float | None = None
        self._last_failure_at: float | None = None
        self._last_error_class: str | None = None
        self._last_error_message: str | None = None
        self._last_probe_duration_ms: float | None = None
        self._consecutive_failures: int = 0
        self._probe_count: int = 0
        self._success_count: int = 0
        self._failure_count: int = 0

    async def start(self) -> None:
        """Start the background probe worker task."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(
            self._run_worker(), name="eggpool:db_writable_probe"
        )
        logger.info(
            "DatabaseWritableProbe started (interval=%.1fs, freshness=%.1fs)",
            self._interval_s,
            self._freshness_s,
        )

    async def stop(self) -> None:
        """Stop the background probe worker with bounded cleanup."""
        self._running = False
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                async with asyncio.timeout(5.0):
                    await self._task
            except (asyncio.CancelledError, TimeoutError):
                pass
        self._task = None
        async with self._lock:
            self._status = ProbeStatus.STOPPED
        logger.info("DatabaseWritableProbe stopped")

    async def _run_worker(self) -> None:
        """Background worker that performs periodic writable probes."""
        if self._initial_probe:
            await self._do_probe()

        while self._running:
            try:
                await asyncio.sleep(self._interval_s)
            except asyncio.CancelledError:
                break
            if not self._running:
                break
            await self._do_probe()

    async def _do_probe(self) -> None:
        """Execute a single writable probe with bounded timeout."""
        t0 = time.monotonic()
        try:
            result = await asyncio.wait_for(
                self._db.probe_writable(),
                timeout=self._timeout_s,
            )
            duration_ms = (time.monotonic() - t0) * 1000
            now = time.time()
            async with self._lock:
                self._last_attempt_at = now
                self._last_probe_duration_ms = duration_ms
                self._probe_count += 1
                if result:
                    self._last_success_at = now
                    self._last_error_class = None
                    self._last_error_message = None
                    self._consecutive_failures = 0
                    self._success_count += 1
                    self._status = ProbeStatus.HEALTHY
                else:
                    self._last_failure_at = now
                    self._consecutive_failures += 1
                    self._failure_count += 1
                    self._status = ProbeStatus.UNHEALTHY
        except TimeoutError:
            duration_ms = (time.monotonic() - t0) * 1000
            now = time.time()
            async with self._lock:
                self._last_attempt_at = now
                self._last_failure_at = now
                self._last_probe_duration_ms = duration_ms
                self._last_error_class = "TimeoutError"
                self._last_error_message = f"probe timed out after {self._timeout_s}s"
                self._consecutive_failures += 1
                self._failure_count += 1
                self._probe_count += 1
                self._status = ProbeStatus.UNHEALTHY
            logger.warning(
                "DatabaseWritableProbe timed out after %.1fs", self._timeout_s
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            duration_ms = (time.monotonic() - t0) * 1000
            now = time.time()
            error_class = type(exc).__qualname__
            error_msg = str(exc)[:200]
            async with self._lock:
                self._last_attempt_at = now
                self._last_failure_at = now
                self._last_probe_duration_ms = duration_ms
                self._last_error_class = error_class
                self._last_error_message = error_msg
                self._consecutive_failures += 1
                self._failure_count += 1
                self._probe_count += 1
                self._status = ProbeStatus.UNHEALTHY
            logger.warning(
                "DatabaseWritableProbe failed: %s: %s", error_class, error_msg
            )

    async def snapshot(self) -> ProbeSnapshot:
        """Return a snapshot of the current probe state.

        If the probe is healthy but stale (result older than
        freshness_deadline), the status is reported as STALE.
        """
        async with self._lock:
            status = self._status
            now = time.time()

            # Check staleness: healthy probe result older than freshness deadline
            if status == ProbeStatus.HEALTHY and self._last_success_at is not None:
                age = now - self._last_success_at
                if age > self._freshness_s:
                    status = ProbeStatus.STALE

            return ProbeSnapshot(
                status=status,
                last_attempt_at=self._last_attempt_at,
                last_success_at=self._last_success_at,
                last_failure_at=self._last_failure_at,
                last_error_class=self._last_error_class,
                last_error_message=self._last_error_message,
                last_probe_duration_ms=self._last_probe_duration_ms,
                consecutive_failures=self._consecutive_failures,
                configured_interval_s=self._interval_s,
                freshness_deadline_s=self._freshness_s,
                worker_running=self._running,
                probe_count=self._probe_count,
                success_count=self._success_count,
                failure_count=self._failure_count,
            )

    async def force_probe(self) -> ProbeSnapshot:
        """Execute an immediate probe and return the updated snapshot.

        Used by diagnostic CLI commands. Blocks until the probe
        completes or times out.
        """
        await self._do_probe()
        return await self.snapshot()

    def force_probe_nowait(self) -> None:
        """Schedule an immediate probe without awaiting its result.

        Used by the database recovery controller to refresh the
        readiness snapshot after a successful recovery cycle.  The
        probe runs on the worker task and updates the cached
        snapshot asynchronously.  No-op when the probe is not
        running.
        """
        if not self._running:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._do_probe(), name="eggpool:db_writable_probe_forced")
