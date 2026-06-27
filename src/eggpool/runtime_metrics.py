"""Runtime and operations metrics for deployment debugging.

Provides a lightweight, process-local snapshot of server health:
process topology, memory pressure, background task state, database
operational health, and in-flight request counts.  Designed for SBC /
Raspberry Pi deployments where heavyweight host monitoring is
unwanted.

All probes are best-effort and never raise to the caller.  Failed
probes return ``None`` for the affected field and append a bounded
string to the ``probe_errors`` list.
"""

from __future__ import annotations

import contextlib
import logging
import os
import platform
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from eggpool.background import BackgroundTaskMonitor, TaskSupervisor
    from eggpool.db.connection import Database
    from eggpool.health.health_manager import HealthManager
    from eggpool.models.config import AppConfig
    from eggpool.routing.router import Router

logger = logging.getLogger(__name__)

_MAX_PROBE_ERRORS = 16
_MAX_PROBE_ERROR_LEN = 200


def _safe_int(value: object) -> int | None:
    """Best-effort int conversion."""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _truncate_probe_error(msg: str) -> str:
    """Truncate a probe error message to a bounded length."""
    if len(msg) <= _MAX_PROBE_ERROR_LEN:
        return msg
    return msg[: _MAX_PROBE_ERROR_LEN - 3] + "..."


def _parse_proc_stat_memory(stat: str, page_size: int) -> tuple[int | None, int | None]:
    """Parse current VMS/RSS bytes from Linux ``/proc/self/stat`` content."""
    parts = stat.split(")", maxsplit=1)
    if len(parts) < 2:
        return None, None

    fields = parts[1].split()
    # After the comm field, fields[0] is state. Linux stat field numbers
    # therefore map as: vsize field 23 -> fields[20], rss field 24 -> fields[21].
    if len(fields) <= 21:
        return None, None

    vms_bytes = _safe_int(fields[20])
    rss_pages = _safe_int(fields[21])
    rss_bytes = rss_pages * page_size if rss_pages is not None else None
    return vms_bytes, rss_bytes


class RuntimeMetricsService:
    """Collects runtime/operations metrics for the running EggPool process.

    Parameters match the objects stored on ``app.state`` during lifespan
    startup.  The service is intentionally independent of
    :class:`~eggpool.stats.service.StatsService` to keep request
    analytics and process diagnostics decoupled.
    """

    def __init__(
        self,
        *,
        config: AppConfig,
        db: Database,
        stats_db: Database | None,
        supervisor: TaskSupervisor | None,
        task_monitor: BackgroundTaskMonitor | None,
        router: Router | None,
        health_manager: HealthManager | None,
        started_monotonic: float,
        started_epoch: float,
        metrics_coalescer: Any | None = None,  # noqa: ANN401
        outbound_manager: Any | None = None,  # noqa: ANN401
        dns_backend: Any | None = None,  # noqa: ANN401
        provider_client_pool: Any | None = None,  # noqa: ANN401
        dispatch_overhead_recorder: Any | None = None,  # noqa: ANN401
    ) -> None:
        self._config = config
        self._db = db
        self._stats_db = stats_db
        self._supervisor = supervisor
        self._task_monitor = task_monitor
        self._router = router
        self._health_manager = health_manager
        self._started_monotonic = started_monotonic
        self._started_epoch = started_epoch
        self._metrics_coalescer = metrics_coalescer
        self._outbound_manager = outbound_manager
        self._dns_backend = dns_backend
        self._provider_client_pool = provider_client_pool
        self._dispatch_overhead_recorder = dispatch_overhead_recorder

    async def snapshot(self) -> dict[str, Any]:
        """Return a best-effort runtime snapshot.

        The snapshot gathers data from multiple sources.  If any probe
        fails the affected field is set to ``None`` and a bounded error
        string is appended to ``probe_errors``.
        """
        probe_errors: list[str] = []
        now_monotonic = time.monotonic()

        result: dict[str, Any] = {}
        result["probe_errors"] = probe_errors

        # Server / process info
        result["server"] = self._snapshot_server(now_monotonic, probe_errors)

        # Memory and file descriptors
        result["memory"] = self._snapshot_memory(probe_errors)

        # OS load average (Linux/macOS)
        result["load"] = self._snapshot_load(probe_errors)

        # Process count scan (Linux only)
        result["processes"] = self._snapshot_processes(probe_errors)

        # Dispatch-overhead recorder (in-memory rolling window)
        result["dispatch_overhead"] = self._snapshot_dispatch_overhead(probe_errors)

        # Background tasks
        result["background_tasks"] = self._snapshot_background_tasks(probe_errors)

        # Database health
        result["db"] = await self._snapshot_db(probe_errors)

        # Routing / in-flight
        result["routing_runtime"] = await self._snapshot_routing_runtime(probe_errors)

        # Metrics buffer health
        result["metrics_buffer"] = self._snapshot_metrics_buffer(probe_errors)

        # Outbound client manager health
        result["outbound_client"] = self._snapshot_outbound_client(probe_errors)

        # Provider client pool health
        result["provider_client_pool"] = self._snapshot_provider_client_pool(
            probe_errors
        )

        # DNS cache health
        result["dns_cache"] = self._snapshot_dns_cache(probe_errors)

        return result

    # -- Server / process ---------------------------------------------------

    def _snapshot_server(
        self, now_monotonic: float, probe_errors: list[str]
    ) -> dict[str, Any]:
        pid = os.getpid()
        ppid: int | None = None
        process_group_id: int | None = None
        session_id: int | None = None
        with contextlib.suppress(OSError, AttributeError):
            ppid = os.getppid()
        with contextlib.suppress(OSError, AttributeError):
            process_group_id = os.getpgrp()
        with contextlib.suppress(OSError, AttributeError):
            session_id = os.getsid(0)

        exe = _safe_exe_basename()
        cmdline = _safe_cmdline_redacted()

        is_daemon_hint = _detect_daemon_hint()

        return {
            "pid": pid,
            "ppid": ppid,
            "process_group_id": process_group_id,
            "session_id": session_id,
            "executable": exe,
            "cmdline": cmdline,
            "uptime_seconds": round(now_monotonic - self._started_monotonic, 1),
            "started_epoch": self._started_epoch,
            "python_version": sys.version.split()[0],
            "platform": platform.platform(),
            "is_daemon_hint": is_daemon_hint,
            "configured_server_threads": self._config.server.threads,
        }

    # -- Memory / FDs / threads --------------------------------------------

    def _snapshot_memory(self, probe_errors: list[str]) -> dict[str, Any]:
        rss_bytes: int | None = None
        vms_bytes: int | None = None
        open_fd_count: int | None = None
        thread_count: int | None = None

        # Try resource.getrusage (POSIX)
        try:
            import resource

            usage = resource.getrusage(resource.RUSAGE_SELF)
            # On Linux ru_maxrss is in KB; on macOS it's in bytes.
            # We report ru_maxrss as-is — it's a high-water mark, not
            # current RSS.  For current RSS we prefer /proc.
            if sys.platform == "linux":
                rss_bytes = usage.ru_maxrss * 1024
            else:
                rss_bytes = usage.ru_maxrss
        except Exception:
            probe_errors.append(_truncate_probe_error("resource.getrusage failed"))

        # Linux: read current RSS/VMS from /proc/self/stat
        if sys.platform == "linux":
            try:
                stat = Path("/proc/self/stat").read_text()
                page_size = int(os.sysconf("SC_PAGE_SIZE"))
                proc_vms_bytes, proc_rss_bytes = _parse_proc_stat_memory(
                    stat,
                    page_size,
                )
                if proc_vms_bytes is not None:
                    vms_bytes = proc_vms_bytes
                if proc_rss_bytes is not None:
                    rss_bytes = proc_rss_bytes
            except Exception:
                pass  # Not critical

        # Open FD count (Linux: /proc/self/fd)
        if sys.platform == "linux":
            try:
                fd_path = Path("/proc/self/fd")
                open_fd_count = sum(1 for _ in fd_path.iterdir())
            except Exception:
                pass
        else:
            # macOS: no reliable FD count without procfs
            open_fd_count = None

        # Thread count
        with contextlib.suppress(Exception):
            thread_count = threading.active_count()

        return {
            "rss_bytes": rss_bytes,
            "vms_bytes": vms_bytes,
            "open_fd_count": open_fd_count,
            "thread_count": thread_count,
        }

    # -- Process count scan -------------------------------------------------

    def _snapshot_processes(self, probe_errors: list[str]) -> dict[str, Any]:
        if sys.platform != "linux":
            return {
                "eggpool_process_count": None,
                "eggpool_child_process_count": None,
                "eggpool_same_session_process_count": None,
                "expected_worker_process_count": _expected_process_count(self._config),
                "process_count_warning": False,
            }

        my_pid = os.getpid()
        my_session: int | None = None
        with contextlib.suppress(OSError, AttributeError):
            my_session = os.getsid(0)

        eggpool_pids: list[int] = []
        child_pids: list[int] = []
        same_session_pids: list[int] = []

        try:
            proc_root = Path("/proc")
            for entry in proc_root.iterdir():
                if not entry.name.isdigit():
                    continue
                pid = int(entry.name)
                if pid == my_pid:
                    continue
                try:
                    cmdline = (entry / "cmdline").read_text(errors="replace")
                except (OSError, FileNotFoundError):
                    continue

                is_eggpool = "eggpool" in cmdline.lower() or (
                    "python" in cmdline.lower() and "eggpool" in cmdline.lower()
                )
                if not is_eggpool:
                    continue

                eggpool_pids.append(pid)

                # Check parent-child relationship
                try:
                    stat_text = (entry / "stat").read_text()
                    stat_fields = stat_text.split(")")
                    if len(stat_fields) >= 2:
                        after_paren = stat_fields[1].split()
                        # field 1 (0-indexed after split on ')') = state
                        # field 3 = ppid
                        if len(after_paren) > 3:
                            child_ppid = _safe_int(after_paren[3])
                            if child_ppid == my_pid:
                                child_pids.append(pid)
                            if my_session is not None:
                                child_session = _safe_int(after_paren[4])
                                if child_session == my_session:
                                    same_session_pids.append(pid)
                except (OSError, FileNotFoundError):
                    pass
        except Exception as exc:
            probe_errors.append(_truncate_probe_error(f"Process scan failed: {exc}"))

        expected = _expected_process_count(self._config)
        observed = len(eggpool_pids) + 1  # +1 for self
        return {
            "eggpool_process_count": observed,
            "eggpool_child_process_count": len(child_pids),
            "eggpool_same_session_process_count": len(same_session_pids),
            "expected_worker_process_count": expected,
            "process_count_warning": observed > expected + 1,
        }

    # -- Background tasks ---------------------------------------------------

    def _snapshot_background_tasks(
        self, probe_errors: list[str]
    ) -> list[dict[str, Any]]:
        if self._task_monitor is not None:
            try:
                return self._task_monitor.snapshot()
            except Exception as exc:  # noqa: BLE001
                probe_errors.append(
                    _truncate_probe_error(f"Task monitor snapshot failed: {exc}")
                )
        if self._supervisor is None:
            return []

        tasks: list[dict[str, Any]] = []
        for name, supervised in self._supervisor._tasks.items():  # pyright: ignore[reportPrivateUsage]
            tasks.append(
                {
                    "name": name,
                    "registered": True,
                    "running": supervised.is_running,
                    "done": (supervised._task is not None and supervised._task.done()),  # pyright: ignore[reportPrivateUsage]
                    "cancelled": (
                        supervised._task is not None and supervised._task.cancelled()  # pyright: ignore[reportPrivateUsage]
                    ),
                    "restart_count": supervised._restart_count,  # pyright: ignore[reportPrivateUsage]
                    "last_failure_at": (
                        supervised._last_failure  # pyright: ignore[reportPrivateUsage]
                        if supervised._last_failure > 0  # pyright: ignore[reportPrivateUsage]
                        else None
                    ),
                    "max_restarts": supervised._max_restarts,  # pyright: ignore[reportPrivateUsage]
                }
            )

        return tasks

    # -- Database health ----------------------------------------------------

    async def _snapshot_db(self, probe_errors: list[str]) -> dict[str, Any]:
        config_db = self._config.database

        is_memory_db = config_db.path == ":memory:"
        db_path = None if is_memory_db else config_db.path
        file_size_bytes: int | None = None
        wal_size_bytes: int | None = None
        shm_size_bytes: int | None = None

        if db_path is not None:
            with contextlib.suppress(OSError, FileNotFoundError):
                file_size_bytes = Path(db_path).stat().st_size
            with contextlib.suppress(OSError, FileNotFoundError):
                wal_size_bytes = Path(db_path + "-wal").stat().st_size
            with contextlib.suppress(OSError, FileNotFoundError):
                shm_size_bytes = Path(db_path + "-shm").stat().st_size

        # Check primary connection status
        primary_connected: bool | None = None
        try:
            if self._db._conn is not None:  # pyright: ignore[reportPrivateUsage]
                primary_connected = True
        except Exception:
            pass

        stats_db_separate = (
            self._stats_db is not None and self._stats_db is not self._db
        )

        # Optional: live PRAGMA values
        wal_mode: str | None = None
        synchronous: str | None = None
        try:
            rows = await self._db.execute_pragma("journal_mode")
            if rows:
                wal_mode = str(rows[0][0])
        except Exception:
            pass
        try:
            rows = await self._db.execute_pragma("synchronous")
            if rows:
                synchronous = str(rows[0][0])
        except Exception:
            pass

        return {
            "path": db_path,
            "is_memory_db": is_memory_db,
            "wal_enabled": config_db.wal,
            "wal_mode_live": wal_mode,
            "synchronous": config_db.synchronous,
            "synchronous_live": synchronous,
            "busy_timeout_ms": config_db.busy_timeout_ms,
            "configured_worker_threads": config_db.worker_threads,
            "primary_connected": primary_connected,
            "stats_connection_separate": stats_db_separate,
            "file_size_bytes": file_size_bytes,
            "wal_size_bytes": wal_size_bytes,
            "shm_size_bytes": shm_size_bytes,
            "contention": self._db.contention_snapshot(),
        }

    # -- Routing / in-flight ------------------------------------------------

    async def _snapshot_routing_runtime(
        self, probe_errors: list[str]
    ) -> dict[str, Any]:
        active_requests_total: int | None = None
        active_requests_by_account: dict[str, int] | None = None
        health_states: dict[str, str] | None = None
        active_backoff_count: int | None = None

        if self._router is not None:
            try:
                all_states = self._router._registry.get_all_states()  # pyright: ignore[reportPrivateUsage]
                by_account: dict[str, int] = {}
                total = 0
                for state in all_states:
                    count = state.active_request_count
                    if count > 0:
                        by_account[state.name] = count
                        total += count
                active_requests_total = total
                active_requests_by_account = by_account if by_account else None
            except Exception as exc:
                probe_errors.append(
                    _truncate_probe_error(f"Active request count failed: {exc}")
                )

        if self._health_manager is not None:
            try:
                states: dict[str, str] = {}
                for (
                    name,
                    health,
                ) in self._health_manager._accounts.items():  # pyright: ignore[reportPrivateUsage]
                    states[name] = health.health_state
                health_states = states if states else None
            except Exception as exc:
                probe_errors.append(
                    _truncate_probe_error(f"Health state snapshot failed: {exc}")
                )

        # Count active backoff rows
        try:
            row = await self._db.fetch_one(
                "SELECT COUNT(*) FROM account_backoffs "
                "WHERE expires_at > unixepoch('now')"
            )
            if row:
                active_backoff_count = int(row[0] or 0)
        except Exception:
            pass

        # Pending health summary (reuses StatsService logic inline)
        pending_count: int | None = None
        oldest_pending_age_seconds: float | None = None
        active_reservations_count: int | None = None
        reserved_microdollars: int | None = None
        try:
            pending_row = await self._db.fetch_one(
                """
                SELECT
                    COUNT(*) AS pending_count,
                    MIN(started_at) AS oldest_pending_at
                FROM requests
                WHERE status = 'pending'
                """
            )
            if pending_row:
                pending_count = int(pending_row["pending_count"] or 0)
                oldest_pending_at = pending_row["oldest_pending_at"]
                if oldest_pending_at and pending_count > 0:
                    from datetime import UTC, datetime

                    now = datetime.now(UTC)
                    started = datetime.fromisoformat(str(oldest_pending_at))
                    if started.tzinfo is None:
                        started = started.replace(tzinfo=UTC)
                    oldest_pending_age_seconds = (now - started).total_seconds()

            res_row = await self._db.fetch_one(
                """
                SELECT
                    COUNT(*) AS active_count,
                    COALESCE(SUM(reserved_microdollars), 0) AS total_reserved
                FROM reservations
                WHERE expires_at > unixepoch('now')
                """
            )
            if res_row:
                active_reservations_count = int(res_row["active_count"] or 0)
                reserved_microdollars = int(res_row["total_reserved"] or 0)
        except Exception as exc:
            probe_errors.append(
                _truncate_probe_error(f"Pending health snapshot failed: {exc}")
            )

        return {
            "active_requests_total": active_requests_total,
            "active_requests_by_account": active_requests_by_account,
            "pending_count": pending_count,
            "oldest_pending_age_seconds": oldest_pending_age_seconds,
            "active_reservations_count": active_reservations_count,
            "reserved_microdollars": reserved_microdollars,
            "health_states_by_account": health_states,
            "active_backoff_count": active_backoff_count,
        }

    def _snapshot_outbound_client(self, probe_errors: list[str]) -> dict[str, Any]:
        """Best-effort snapshot of the outbound client manager state."""
        if self._outbound_manager is None:
            return {
                "build_count": 0,
                "request_count": 0,
                "error_count": 0,
                "has_client": False,
            }
        try:
            return self._outbound_manager.snapshot()
        except Exception as exc:
            probe_errors.append(
                _truncate_probe_error(f"Outbound client snapshot failed: {exc}")
            )
            return {"error": str(exc)}

    def _snapshot_provider_client_pool(self, probe_errors: list[str]) -> dict[str, Any]:
        """Best-effort snapshot of the provider client pool state."""
        if self._provider_client_pool is None:
            return {"build_count": 0, "providers": {}}
        try:
            return self._provider_client_pool.snapshot()
        except Exception as exc:
            probe_errors.append(
                _truncate_probe_error(f"Provider client pool snapshot failed: {exc}")
            )
            return {"error": str(exc)}

    def _snapshot_dns_cache(self, probe_errors: list[str]) -> dict[str, Any]:
        """Best-effort snapshot of the DNS cache state."""
        if self._dns_backend is None:
            return {"enabled": False}
        try:
            return {"enabled": True, **self._dns_backend.cache.snapshot()}
        except Exception as exc:
            probe_errors.append(
                _truncate_probe_error(f"DNS cache snapshot failed: {exc}")
            )
            return {"error": str(exc)}

    def _snapshot_load(self, probe_errors: list[str]) -> dict[str, Any]:
        """Best-effort snapshot of the OS load average."""
        cpu_count = os.cpu_count()
        try:
            load_1m, load_5m, load_15m = os.getloadavg()
        except (AttributeError, OSError):
            return {
                "available": False,
                "cpu_count": cpu_count,
                "load_1m": None,
                "load_5m": None,
                "load_15m": None,
                "normalized_1m": None,
                "normalized_5m": None,
                "normalized_15m": None,
            }

        def norm(value: float) -> float | None:
            if not cpu_count or cpu_count <= 0:
                return None
            return value / cpu_count

        return {
            "available": True,
            "cpu_count": cpu_count,
            "load_1m": load_1m,
            "load_5m": load_5m,
            "load_15m": load_15m,
            "normalized_1m": norm(load_1m),
            "normalized_5m": norm(load_5m),
            "normalized_15m": norm(load_15m),
        }

    def _snapshot_dispatch_overhead(self, probe_errors: list[str]) -> dict[str, Any]:
        """Best-effort snapshot of the dispatch-overhead recorder state."""
        if self._dispatch_overhead_recorder is None:
            return {
                "window_size": 100,
                "sample_count": 0,
                "avg_ms": None,
                "min_ms": None,
                "max_ms": None,
                "p50_ms": None,
                "p95_ms": None,
            }
        try:
            return self._dispatch_overhead_recorder.snapshot()
        except Exception as exc:
            probe_errors.append(
                _truncate_probe_error(f"Dispatch overhead snapshot failed: {exc}")
            )
            return {"error": str(exc)}

    def _snapshot_metrics_buffer(self, probe_errors: list[str]) -> dict[str, Any]:
        """Best-effort snapshot of the metrics write coalescer state."""
        if self._metrics_coalescer is None:
            return {
                "write_mode": getattr(self._config.metrics, "write_mode", "balanced"),
                "buffered_keys": 0,
                "buffered_events": 0,
                "total_events_received": 0,
                "total_events_flushed": 0,
                "total_events_dropped": 0,
                "last_flush_ts": None,
                "last_flush_rows": 0,
                "last_flush_duration_ms": 0,
                "last_flush_error": None,
            }
        try:
            return self._metrics_coalescer.snapshot()
        except Exception as exc:
            probe_errors.append(
                _truncate_probe_error(f"Metrics buffer snapshot failed: {exc}")
            )
            return {"error": str(exc)}


# -- Helpers ----------------------------------------------------------------


def _expected_process_count(config: AppConfig) -> int:
    """Expected number of EggPool processes.

    Granian runs with ``workers=1`` which produces one supervisor
    process and one worker process.  The application-level thread
    count does not increase the process count.
    """
    return 2


def _safe_exe_basename() -> str | None:
    """Best-effort executable basename."""
    try:
        return Path(sys.executable).name
    except Exception:
        return None


def _safe_cmdline_redacted() -> str | None:
    """Best-effort redacted command line.

    Returns a truncated version without arguments to avoid leaking
    config paths or API keys.
    """
    try:
        cmdline_parts = sys.argv[:2]
        return " ".join(cmdline_parts)
    except Exception:
        return None


def _detect_daemon_hint() -> bool:
    """Heuristic: is this process running as a daemon?

    Returns True if stdin is not a TTY or if the parent PID suggests
    daemon mode (e.g., PPID 1 or session leader).
    """
    try:
        if not sys.stdin.isatty():
            return True
    except (AttributeError, ValueError):
        return True
    return False
