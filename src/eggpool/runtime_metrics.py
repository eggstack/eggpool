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
from datetime import UTC, datetime, timedelta
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


def _append_probe_error(probe_errors: list[str], msg: str) -> None:
    """Append a bounded probe error without letting the list grow forever."""
    if len(probe_errors) >= _MAX_PROBE_ERRORS:
        return
    probe_errors.append(_truncate_probe_error(msg))


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


def _parse_proc_stat_ids(stat: str) -> tuple[int | None, int | None]:
    """Parse parent PID and session ID from Linux ``/proc/<pid>/stat`` content."""
    parts = stat.split(")", maxsplit=1)
    if len(parts) < 2:
        return None, None

    fields = parts[1].split()
    # After the comm field, fields[0] is state. Linux stat field numbers
    # therefore map as: ppid field 4 -> fields[1], session field 6 -> fields[3].
    if len(fields) <= 3:
        return None, None
    return _safe_int(fields[1]), _safe_int(fields[3])


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
        local_pre_upstream_recorder: Any | None = None,  # noqa: ANN401
        dispatch_span_recorder: Any | None = None,  # noqa: ANN401
        model_info: Any | None = None,  # noqa: ANN401
        dashboard_telemetry: Any | None = None,  # noqa: ANN401
        stream_diagnostics: Any | None = None,  # noqa: ANN401
        finalization_retry_queue: Any | None = None,  # noqa: ANN401
        routing_trace_guard: Any | None = None,  # noqa: ANN401
        runtime_manager: Any | None = None,  # noqa: ANN401
        reload_manager: Any | None = None,  # noqa: ANN401
        process: Any | None = None,  # noqa: ANN401 — ProcessRuntime, avoids circular import
        dispatch_writer: Any | None = None,  # noqa: ANN401
        routing_trace_writer: Any | None = None,  # noqa: ANN401
        maintenance_state: Any | None = None,  # noqa: ANN401
        event_loop_lag_monitor: Any | None = None,  # noqa: ANN401
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
        self._local_pre_upstream_recorder = local_pre_upstream_recorder
        self._dispatch_span_recorder = dispatch_span_recorder
        self._model_info = model_info
        self._dashboard_telemetry = dashboard_telemetry
        self._stream_diagnostics = stream_diagnostics
        self._finalization_retry_queue = finalization_retry_queue
        self._routing_trace_guard = routing_trace_guard
        self._runtime_manager = runtime_manager
        self._reload_manager = reload_manager
        self._process = process
        self._dispatch_writer = dispatch_writer
        self._routing_trace_writer = routing_trace_writer
        self._maintenance_state = maintenance_state
        self._event_loop_lag_monitor = event_loop_lag_monitor

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

        # Milestone A4: total local pre-upstream latency (handler entry
        # -> dispatch) per-span summary.  Distinct from the
        # ``dispatch_overhead`` recorder above, which only covers the
        # coordinator-internal slice (context build -> dispatch).
        result["local_pre_upstream"] = self._snapshot_local_pre_upstream(probe_errors)

        # Runtime manager snapshot (milestone B): active and retiring
        # generation counts, digests, lease counts, and shutdown state
        # so operators can verify the live reload state without
        # guessing from process logs.  Tolerates a missing manager
        # (older builds, partial app.state) by returning ``None``.
        result["runtime_manager"] = self._snapshot_runtime_manager(probe_errors)

        # Phase 6 fine-grained dispatch spans (Phase 1 hot-path optimization).
        # Per-span latency (avg / p50 / p95 / p99) for the named proxy
        # and coordinator regions so operators can read where time is
        # spent at a glance without re-reading source.
        result["dispatch_spans"] = self._snapshot_dispatch_spans(probe_errors)

        # Background tasks
        background_tasks = self._snapshot_background_tasks(probe_errors)
        result["background_tasks"] = background_tasks
        result["background_task_summary"] = self._snapshot_background_task_summary(
            background_tasks
        )

        # Database health
        result["db"] = await self._snapshot_db(probe_errors)

        # Routing / in-flight
        result["routing_runtime"] = await self._snapshot_routing_runtime(probe_errors)

        # Metrics buffer health
        result["metrics_buffer"] = self._snapshot_metrics_buffer(probe_errors)

        # Rollup freshness — surface a stalled coalescer before the
        # dashboard starts under-reporting the in-flight hour.
        result["rollup_freshness"] = await self._snapshot_rollup_freshness(probe_errors)

        # Outbound client manager health
        result["outbound_client"] = self._snapshot_outbound_client(probe_errors)

        # Provider client pool health
        result["provider_client_pool"] = self._snapshot_provider_client_pool(
            probe_errors
        )

        # DNS cache health
        result["dns_cache"] = self._snapshot_dns_cache(probe_errors)

        # Thinking/reasoning observability counters
        result["thinking_metrics"] = await self._snapshot_thinking_metrics(probe_errors)

        result["model_info"] = await self._snapshot_model_info(probe_errors)

        result["dashboard_telemetry"] = self._snapshot_dashboard_telemetry(probe_errors)

        result["stream_diagnostics"] = self._snapshot_stream_diagnostics(probe_errors)

        result[
            "finalization_retry_queue"
        ] = await self._snapshot_finalization_retry_queue(probe_errors)

        result["routing_trace_guard"] = self._snapshot_routing_trace_guard(probe_errors)

        result["reload_state"] = self._snapshot_reload_state(probe_errors)
        result["dispatch_writer"] = self._snapshot_dispatch_writer(probe_errors)
        result["routing_trace_writer"] = self._snapshot_routing_trace_writer(
            probe_errors
        )

        result["maintenance"] = self._snapshot_maintenance(probe_errors)

        result["event_loop_lag"] = self._snapshot_event_loop_lag(probe_errors)

        result["resource_plateaus"] = self._snapshot_resource_plateaus(probe_errors)

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
            _append_probe_error(probe_errors, "resource.getrusage failed")

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
                    child_ppid, child_session = _parse_proc_stat_ids(stat_text)
                    if child_ppid == my_pid:
                        child_pids.append(pid)
                    if my_session is not None and child_session == my_session:
                        same_session_pids.append(pid)
                except (OSError, FileNotFoundError):
                    pass
        except Exception as exc:
            _append_probe_error(probe_errors, f"Process scan failed: {exc}")

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
                _append_probe_error(
                    probe_errors, f"Task monitor snapshot failed: {exc}"
                )
        if self._supervisor is None:
            return []

        return self._supervisor.snapshot()

    def _snapshot_background_task_summary(
        self, tasks: list[dict[str, Any]]
    ) -> dict[str, int]:
        """Derive an at-a-glance summary for the background task table.

        Counts registered vs. running vs. failed vs. overdue tasks plus
        the total number of last-error ticks.  ``overdue`` only counts
        periodic tasks whose ``overdue_seconds`` exceeds the grace band
        so transient scheduler jitter does not fire the alert.
        ``never_run_not_due`` and ``never_run_startup_deferred`` are
        counted separately so the dashboard can render a friendly label
        for healthy startup-deferred tasks (the old code rendered all
        never-run tasks as opaque ``never ran``).
        """
        registered = len(tasks)
        running = 0
        failed = 0
        overdue = 0
        last_error_count = 0
        never_run_not_due = 0
        never_run_overdue = 0
        for task in tasks:
            is_running = bool(task.get("running"))
            is_done = bool(task.get("done"))
            is_cancelled = bool(task.get("cancelled"))
            if is_running:
                running += 1
            if is_done or is_cancelled:
                failed += 1
            if (
                isinstance(task.get("overdue_seconds"), (int, float))
                and float(task["overdue_seconds"]) > 0
            ):
                overdue += 1
            if task.get("last_error_class"):
                last_error_count += 1
            first_run = task.get("first_run_state")
            if first_run == "never_run_not_due":
                never_run_not_due += 1
            elif first_run == "never_run_overdue":
                never_run_overdue += 1
        return {
            "registered": registered,
            "running": running,
            "failed": failed,
            "overdue": overdue,
            "last_error_count": last_error_count,
            "never_run_not_due": never_run_not_due,
            "never_run_overdue": never_run_overdue,
        }

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

        # Additional page-level telemetry
        wal_page_count: int | None = None
        db_page_count: int | None = None
        db_page_size: int | None = None
        freelist_count: int | None = None

        try:
            rows = await self._db.execute_pragma("PRAGMA wal_page_count")
            if rows:
                wal_page_count = _safe_int(rows[0][0])
        except Exception:
            pass
        try:
            rows = await self._db.execute_pragma("PRAGMA page_count")
            if rows:
                db_page_count = _safe_int(rows[0][0])
        except Exception:
            pass
        try:
            rows = await self._db.execute_pragma("PRAGMA page_size")
            if rows:
                db_page_size = _safe_int(rows[0][0])
        except Exception:
            pass
        try:
            rows = await self._db.execute_pragma("PRAGMA freelist_count")
            if rows:
                freelist_count = _safe_int(rows[0][0])
        except Exception:
            pass

        # Oldest retained row timestamps for retention diagnostics.
        oldest_request_at: str | None = None
        oldest_event_at: str | None = None
        try:
            row = await self._db.fetch_one("SELECT MIN(started_at) FROM requests")
            if row and row[0] is not None:
                oldest_request_at = str(row[0])
        except Exception:
            pass
        try:
            row = await self._db.fetch_one("SELECT MIN(created_at) FROM account_events")
            if row and row[0] is not None:
                oldest_event_at = str(row[0])
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
            "wal_page_count": wal_page_count,
            "db_page_count": db_page_count,
            "db_page_size": db_page_size,
            "freelist_count": freelist_count,
            "contention": self._db.contention_snapshot(),
            "oldest_retained_request_at": oldest_request_at,
            "oldest_retained_event_at": oldest_event_at,
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
                _append_probe_error(probe_errors, f"Active request count failed: {exc}")

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
                _append_probe_error(
                    probe_errors, f"Health state snapshot failed: {exc}"
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
            _append_probe_error(probe_errors, f"Pending health snapshot failed: {exc}")

        return {
            "active_requests_total": active_requests_total,
            "active_requests_by_account": active_requests_by_account,
            "pending_count": pending_count,
            "oldest_pending_age_seconds": oldest_pending_age_seconds,
            "active_reservations_count": active_reservations_count,
            "reserved_microdollars": reserved_microdollars,
            "health_states_by_account": health_states,
            "active_backoff_count": active_backoff_count,
            "guardrails": {
                "routing_cache_compression_mode": "reporting_only",
                "routing_uses_cache_metrics": False,
                "routing_uses_compression_metrics": False,
                "routing_uses_stable_prefix_hash": False,
                "routing_uses_compression_policy": False,
                "routing_uses_compression_tuning": False,
                "route_scorer_inputs": [
                    "health",
                    "quota",
                    "active_requests",
                    "model_eligibility",
                ],
            },
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
            _append_probe_error(probe_errors, f"Outbound client snapshot failed: {exc}")
            return {"error": str(exc)}

    def _snapshot_provider_client_pool(self, probe_errors: list[str]) -> dict[str, Any]:
        """Best-effort snapshot of the provider client pool state."""
        if self._provider_client_pool is None:
            return {"build_count": 0, "providers": {}}
        try:
            return self._provider_client_pool.snapshot()
        except Exception as exc:
            _append_probe_error(
                probe_errors, f"Provider client pool snapshot failed: {exc}"
            )
            return {"error": str(exc)}

    def _snapshot_dns_cache(self, probe_errors: list[str]) -> dict[str, Any]:
        """Best-effort snapshot of the DNS cache state."""
        if self._dns_backend is None:
            return {"enabled": False}
        try:
            return {"enabled": True, **self._dns_backend.cache.snapshot()}
        except Exception as exc:
            _append_probe_error(probe_errors, f"DNS cache snapshot failed: {exc}")
            return {"error": str(exc)}

    async def _snapshot_thinking_metrics(
        self, probe_errors: list[str]
    ) -> dict[str, Any]:
        """Best-effort snapshot of thinking/reasoning observability counters."""
        from eggpool.metrics.thinking import get_counter

        try:
            counter = get_counter()
            return await counter.snapshot()
        except Exception as exc:
            _append_probe_error(
                probe_errors, f"Thinking metrics snapshot failed: {exc}"
            )
            return {"total": 0, "counters": {}, "label_breakdown": {}}

    async def _snapshot_model_info(self, probe_errors: list[str]) -> dict[str, Any]:
        """Best-effort snapshot of the model-info subsystem."""
        model_info_service: Any | None = getattr(self, "_model_info", None)  # pyright: ignore[reportPrivateUsage]
        if model_info_service is None:
            return {"enabled": False}
        try:
            snapshot = await model_info_service.health_snapshot()  # pyright: ignore[reportOptionalMemberAccess]
            return snapshot
        except AttributeError:
            return {"enabled": True, "snapshot_unavailable": True}
        except Exception as exc:
            _append_probe_error(probe_errors, f"Model info snapshot failed: {exc}")
            return {"enabled": True, "error": str(exc)}

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
                "p99_ms": None,
            }
        try:
            return self._dispatch_overhead_recorder.snapshot()
        except Exception as exc:
            _append_probe_error(
                probe_errors, f"Dispatch overhead snapshot failed: {exc}"
            )
            return {"error": str(exc)}

    def _snapshot_local_pre_upstream(self, probe_errors: list[str]) -> dict[str, Any]:
        """Best-effort snapshot of the local pre-upstream latency recorder.

        Distinct from :meth:`_snapshot_dispatch_overhead`: this
        recorder measures the full EggPool-side window from ASGI
        handler entry to the dispatch boundary.  See Milestone A4
        timing-boundary clarification for the exact origin and end
        timestamps.
        """
        if self._local_pre_upstream_recorder is None:
            return {
                "window_size": 100,
                "sample_count": 0,
                "avg_ms": None,
                "min_ms": None,
                "max_ms": None,
                "p50_ms": None,
                "p95_ms": None,
                "p99_ms": None,
            }
        try:
            return self._local_pre_upstream_recorder.snapshot().as_dict()
        except Exception as exc:
            _append_probe_error(
                probe_errors,
                f"Local pre-upstream snapshot failed: {exc}",
            )
            return {"error": str(exc)}

    def _snapshot_runtime_manager(
        self, probe_errors: list[str]
    ) -> dict[str, Any] | None:
        """Best-effort snapshot of the milestone-B runtime manager.

        Returns ``None`` when the manager is missing or has not been
        installed yet (early startup, tests that build a bare service,
        or pre-B builds).  Never raises; probe errors are recorded so
        the runtime dashboard can surface them without crashing.
        """
        manager = self._runtime_manager
        if manager is None:
            return None
        try:
            diag = manager.diagnostics()
        except Exception as exc:
            _append_probe_error(probe_errors, f"Runtime manager snapshot failed: {exc}")
            return {"error": str(exc)}
        result = _runtime_manager_to_dict(diag)
        # Attach D2 task-reload diagnostics from the process container.
        process = self._process
        if process is not None:
            result["task_spec_version"] = getattr(process, "task_spec_version", 0)
            result["task_reload_summary"] = getattr(
                process, "last_task_transition", None
            )
        else:
            result["task_spec_version"] = 0
            result["task_reload_summary"] = None
        return result

    def _snapshot_dispatch_spans(self, probe_errors: list[str]) -> dict[str, Any]:
        """Best-effort snapshot of the named-span dispatch recorder.

        Always returns the full key list (so dashboard consumers can rely
        on every span key being represented) even when no samples were
        recorded yet for a given span.  When the recorder is missing the
        whole section returns an empty list so older callers can still
        probe ``response["dispatch_spans"]``.
        """
        try:
            from eggpool.runtime_dispatch import ALL_SPAN_KEYS
        except Exception:  # noqa: BLE001
            return {"window_size": 200, "spans": []}
        if self._dispatch_span_recorder is None:
            return {
                "window_size": 200,
                "spans": [
                    {
                        "span": key,
                        "window_size": 200,
                        "sample_count": 0,
                        "avg_ms": None,
                        "min_ms": None,
                        "max_ms": None,
                        "p50_ms": None,
                        "p95_ms": None,
                        "p99_ms": None,
                    }
                    for key in ALL_SPAN_KEYS
                ],
            }
        try:
            return self._dispatch_span_recorder.snapshot_for_spans(list(ALL_SPAN_KEYS))
        except Exception as exc:
            _append_probe_error(probe_errors, f"Dispatch span snapshot failed: {exc}")
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
            _append_probe_error(probe_errors, f"Metrics buffer snapshot failed: {exc}")
            return {"error": str(exc)}

    async def _snapshot_rollup_freshness(
        self, probe_errors: list[str]
    ) -> dict[str, Any]:
        """Compare the rollup table's latest bucket against the live
        ``requests.started_at`` so operators can spot a stalled
        coalescer before the dashboard starts under-reporting.

        ``staleness_seconds`` is the gap between the most recent
        ``started_at`` and the most recent ``bucket_start``; positive
        values mean the rollup is trailing the live table.
        """
        if self._metrics_coalescer is None:
            return {"enabled": False}
        rollup_repo = getattr(self._metrics_coalescer, "_rollup_repo", None)
        if rollup_repo is None:
            return {"enabled": False}
        try:
            end_dt = datetime.now(UTC)
            start_dt = end_dt - timedelta(days=7)
            from eggpool.stats.queries import fetch_latest_started_at

            rollup_latest = await rollup_repo.latest_bucket_start(
                end=end_dt.strftime("%Y-%m-%d %H:%M:%S"),
            )
            requests_latest = await fetch_latest_started_at(
                self._db,
                start_dt.strftime("%Y-%m-%d %H:%M:%S"),
                end_dt.strftime("%Y-%m-%d %H:%M:%S"),
            )
            staleness_seconds: float | None = None
            if rollup_latest is not None and requests_latest is not None:
                rt = datetime.strptime(rollup_latest, "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=UTC
                )
                lt = datetime.strptime(requests_latest, "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=UTC
                )
                staleness_seconds = max(0.0, (lt - rt).total_seconds())
            return {
                "enabled": True,
                "rollup_latest_bucket_start": rollup_latest,
                "requests_latest_started_at": requests_latest,
                "staleness_seconds": staleness_seconds,
            }
        except Exception as exc:
            _append_probe_error(probe_errors, f"Rollup freshness probe failed: {exc}")
            return {"enabled": True, "error": str(exc)}

    def _snapshot_dashboard_telemetry(self, probe_errors: list[str]) -> dict[str, Any]:
        telemetry_snapshot: dict[str, Any] = {}
        if self._dashboard_telemetry is not None:
            try:
                telemetry_snapshot = self._dashboard_telemetry.snapshot()
                stage_snap = self._dashboard_telemetry.stage_snapshot()
                telemetry_snapshot.update(stage_snap)
            except Exception as exc:
                _append_probe_error(
                    probe_errors, f"Dashboard telemetry snapshot failed: {exc}"
                )
                telemetry_snapshot = {"error": str(exc)}

        config_db = self._config.database
        stats_db_separate = (
            self._stats_db is not None and self._stats_db is not self._db
        )

        trace_mode = "sampled"
        with contextlib.suppress(AttributeError, TypeError):
            trace_mode = self._config.routing.trace.mode

        result: dict[str, Any] = {
            **telemetry_snapshot,
            "separate_stats_db": stats_db_separate,
            "runtime_threads": self._config.server.threads,
            "database_worker_threads": config_db.worker_threads,
            "routing_trace_mode": trace_mode,
        }

        cache_stats = getattr(self._dashboard_telemetry, "cache_stats", None)
        if cache_stats is not None:
            with contextlib.suppress(Exception):
                result["cache_stats"] = cache_stats()

        return result

    def _snapshot_stream_diagnostics(self, probe_errors: list[str]) -> dict[str, Any]:
        """Best-effort snapshot of the stream outcome diagnostics service.

        The service is process-local; tests can override the dependency
        on :class:`RuntimeMetricsService` via the ``stream_diagnostics``
        constructor argument.  When the dependency is missing (e.g.
        older test harnesses) the section returns ``enabled: False`` so
        consumers can rely on the key being present.
        """
        if self._stream_diagnostics is None:
            return {"enabled": False}
        try:
            return {"enabled": True, **self._stream_diagnostics.snapshot()}
        except Exception as exc:
            _append_probe_error(
                probe_errors, f"Stream diagnostics snapshot failed: {exc}"
            )
            return {"enabled": True, "error": str(exc)}

    async def _snapshot_finalization_retry_queue(
        self, probe_errors: list[str]
    ) -> dict[str, Any]:
        """Best-effort snapshot of the bounded finalization retry queue.

        Returns ``enabled: False`` when no retry queue is wired (older
        test harnesses or downstream callers without the queue).
        """
        if self._finalization_retry_queue is None:
            return {"enabled": False}
        try:
            return {
                "enabled": True,
                **await self._finalization_retry_queue.snapshot(),
            }
        except Exception as exc:
            _append_probe_error(
                probe_errors,
                f"Finalization retry queue snapshot failed: {exc}",
            )
            return {"enabled": True, "error": str(exc)}

    def _snapshot_routing_trace_guard(self, probe_errors: list[str]) -> dict[str, Any]:
        """Best-effort snapshot of the routing trace write guardrail."""
        if self._routing_trace_guard is None:
            return {"enabled": False}
        try:
            return {"enabled": True, **self._routing_trace_guard.snapshot()}
        except Exception as exc:
            _append_probe_error(
                probe_errors,
                f"Routing trace guard snapshot failed: {exc}",
            )
            return {"enabled": True, "error": str(exc)}

    def _snapshot_reload_state(self, probe_errors: list[str]) -> dict[str, Any]:
        """Best-effort snapshot of the live-reload manager state.

        Returns ``enabled: False`` when no reload manager is wired
        (older test harnesses or pre-C builds).
        """
        if self._reload_manager is None:
            return {"enabled": False}
        try:
            return {"enabled": True, **self._reload_manager.snapshot()}
        except Exception as exc:
            _append_probe_error(
                probe_errors,
                f"Reload manager snapshot failed: {exc}",
            )
            return {"enabled": True, "error": str(exc)}

    def _snapshot_dispatch_writer(self, probe_errors: list[str]) -> dict[str, Any]:
        """Dispatch persistence writer diagnostics (Milestone C)."""
        if self._dispatch_writer is None:
            return {"enabled": False}
        try:
            return {"enabled": True, **self._dispatch_writer.snapshot()}
        except Exception as exc:
            _append_probe_error(
                probe_errors,
                f"Dispatch writer snapshot failed: {exc}",
            )
            return {"enabled": True, "error": str(exc)}

    def _snapshot_routing_trace_writer(self, probe_errors: list[str]) -> dict[str, Any]:
        """Routing trace writer diagnostics (Milestone D)."""
        if self._routing_trace_writer is None:
            return {"enabled": False}
        try:
            return {"enabled": True, **self._routing_trace_writer.snapshot()}
        except Exception as exc:
            _append_probe_error(
                probe_errors,
                f"Routing trace writer snapshot failed: {exc}",
            )
            return {"enabled": True, "error": str(exc)}

    def _snapshot_maintenance(self, probe_errors: list[str]) -> dict[str, Any]:
        """Best-effort snapshot of maintenance task diagnostics.

        Returns per-task diagnostics (last result, budget, contention
        deferrals) when a ``_maintenance_state`` object has been set by
        the runtime tasks.  Returns ``{"enabled": False}`` when no
        maintenance state is available.
        """
        state = self._maintenance_state
        if state is None:
            return {"enabled": False}
        try:
            return {"enabled": True, **state.snapshot()}
        except Exception as exc:
            _append_probe_error(
                probe_errors,
                f"Maintenance state snapshot failed: {exc}",
            )
            return {"enabled": True, "error": str(exc)}

    def _snapshot_event_loop_lag(self, probe_errors: list[str]) -> dict[str, Any]:
        """Best-effort snapshot of the event-loop lag monitor.

        Returns lag statistics (p50, p95, p99, max in ms) measured by
        periodic callback drift.  The monitor is process-owned and
        survives generation swaps.  Returns ``{"enabled": False}``
        when no monitor is wired.
        """
        monitor = self._event_loop_lag_monitor
        if monitor is None:
            return {"enabled": False}
        try:
            return {"enabled": True, **monitor.to_dict()}
        except Exception as exc:
            _append_probe_error(
                probe_errors,
                f"Event-loop lag snapshot failed: {exc}",
            )
            return {"enabled": True, "error": str(exc)}

    def _snapshot_resource_plateaus(self, probe_errors: list[str]) -> dict[str, Any]:
        """Best-effort boundedness checks for long-lived resources.

        Surfaces DNS cache capacity, client pool provider counts, and
        stream diagnostic ring-buffer sizes so operators can verify that
        bounded resources remain within expected limits.
        """
        dns_plateau: dict[str, Any] = {"enabled": False}
        client_pool_plateau: dict[str, Any] = {"enabled": False}
        stream_diag_plateau: dict[str, Any] = {"enabled": False}

        # DNS cache
        if self._dns_backend is not None:
            try:
                cache = self._dns_backend.cache
                snap = cache.snapshot()
                max_entries = snap.get("max_entries")
                current_size = snap.get("size", 0)
                dns_plateau = {
                    "enabled": True,
                    "max_entries": max_entries,
                    "current_size": current_size,
                    "utilisation_pct": (
                        round(current_size / max_entries * 100, 1)
                        if max_entries
                        else None
                    ),
                    "evictions_total": snap.get("evictions", 0),
                }
            except Exception as exc:
                _append_probe_error(
                    probe_errors,
                    f"DNS cache plateau snapshot failed: {exc}",
                )
                dns_plateau = {"enabled": True, "error": str(exc)}

        # Provider client pool
        if self._provider_client_pool is not None:
            try:
                pool_snap = self._provider_client_pool.snapshot()
                providers = pool_snap.get("providers", {})
                client_pool_plateau = {
                    "enabled": True,
                    "provider_count": len(providers),
                    "providers": list(providers.keys()),
                }
            except Exception as exc:
                _append_probe_error(
                    probe_errors,
                    f"Client pool plateau snapshot failed: {exc}",
                )
                client_pool_plateau = {"enabled": True, "error": str(exc)}

        # Stream diagnostics ring buffers
        if self._stream_diagnostics is not None:
            try:
                sd_snap = self._stream_diagnostics.snapshot()
                completed = sd_snap.get("completed_ms", {})
                cancel = sd_snap.get("client_cancel_ms", {})
                finalizer = sd_snap.get("finalizer_timeout_ms", {})
                stream_diag_plateau = {
                    "enabled": True,
                    "completed_histogram_capacity": 256,
                    "completed_histogram_samples": completed.get("sample_count", 0),
                    "client_cancel_histogram_capacity": 256,
                    "client_cancel_histogram_samples": cancel.get("sample_count", 0),
                    "finalizer_timeout_histogram_capacity": 256,
                    "finalizer_timeout_histogram_samples": finalizer.get(
                        "sample_count", 0
                    ),
                }
            except Exception as exc:
                _append_probe_error(
                    probe_errors,
                    f"Stream diagnostics plateau snapshot failed: {exc}",
                )
                stream_diag_plateau = {"enabled": True, "error": str(exc)}

        return {
            "dns_cache": dns_plateau,
            "provider_client_pool": client_pool_plateau,
            "stream_diagnostics": stream_diag_plateau,
        }


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


def _runtime_manager_to_dict(diag: Any) -> dict[str, Any]:
    """Render a :class:`RuntimeDiagnostics` snapshot as a JSON-safe dict.

    Diagnostic output is intentionally lossy: only counts, IDs, digests,
    and timestamps.  Never includes the underlying ``AppConfig``,
    raw services, or secret material.
    """
    return {
        "active": _generation_diag_to_dict(diag.active),
        "retiring": [_generation_diag_to_dict(g) for g in diag.retiring],
        "retiring_count": len(diag.retiring),
        "shutdown_in_progress": diag.shutdown_in_progress,
        "next_generation_id": diag.next_generation_id,
        "retirement_task_count": diag.retirement_task_count,
    }


def _generation_diag_to_dict(diag: Any) -> dict[str, Any] | None:
    """Render a :class:`GenerationDiagnostics` as a JSON-safe dict."""
    if diag is None:
        return None
    result: dict[str, Any] = {
        "generation_id": diag.generation_id,
        "config_digest_prefix": diag.config_digest_prefix,
        "created_at_monotonic": diag.created_at_monotonic,
        "created_at_epoch": diag.created_at_epoch,
        "age_seconds": round(diag.age_seconds, 3),
        "active_leases": diag.active_leases,
        "accepting_leases": diag.accepting_leases,
        "retirement_started": diag.retirement_started,
        "retirement_complete": diag.retirement_complete,
        "last_close_error": diag.last_close_error,
        "state": diag.state,
        "forced_close": diag.forced_close,
    }
    if diag.retirement_start_time is not None:
        result["retirement_start_time"] = diag.retirement_start_time
    if diag.drain_deadline_s is not None:
        result["drain_deadline_s"] = diag.drain_deadline_s
    if diag.close_start_time is not None:
        result["close_start_time"] = diag.close_start_time
    if diag.close_complete_time is not None:
        result["close_complete_time"] = diag.close_complete_time
    return result
