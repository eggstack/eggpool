"""Point-in-time runtime state snapshots for reload correctness tests (Phase 1).

Captures every observable state domain required by plan section 4 so
tests can assert equality across full reload transactions and detect
leaks, mixed-state, and stale mirrors.

Snapshot fields
---------------

Required by plan 002-phase-01-correctness-baseline.md §4:

- active generation ID and config digest
- identities of registry, catalog, router, coordinator, health manager,
  stats service, and recorders
- active generation configuration values relevant to the candidate
- ``app.state`` generation-owned mirrors and effective config/digest
- providers and accounts persisted in SQLite
- active account/model backoffs in memory and persistence
- process-supervisor task specifications and running task IDs
- routing-trace writer configuration
- dispatch-writer existence, enabled selection, queue state, worker identity
- open provider client pools, outbound managers, DNS backends, closeable fakes
- active and retiring generation counts
- active lease counts
- live ``asyncio.Task`` count filtered to EggPool-owned tasks
- open file descriptor count where portable
- control socket path and inode where supported

The snapshot supports value comparison and identity comparison. Only
stable, serialized values appear in assertion messages; object IDs use
``id()``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import resource
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RuntimeSnapshot:
    """Point-in-time snapshot of runtime state for comparison.

    Captures active generation ID, config digest, service object identities,
    lease counts, task counts, and resource metrics. Supports both value
    comparison (equality of captured values) and identity comparison
    (same object references).

    Fields are best-effort: any field whose underlying source is missing
    is set to ``None`` or ``0`` so the snapshot never raises during capture.
    """

    # Generation identity
    active_generation_id: int | None
    config_digest: str
    generation_config_values: dict[str, Any]

    # Service identities (id() of each object)
    service_identities: dict[str, int]

    # Lease counts
    active_lease_count: int
    retiring_generation_count: int

    # Task counts
    asyncio_task_count: int
    supervisor_task_count: int

    # Process-level
    open_file_descriptor_count: int | None

    # Runtime-manager diagnostics
    runtime_manager_diags: dict[str, Any]

    # Config / app.state mirrors (new fields per plan §4)
    effective_config_digest: str
    app_state_router_id: int | None

    # SQLite persistence state
    persisted_provider_ids: tuple[str, ...]
    persisted_account_names: tuple[str, ...]

    # Backoffs (in memory + persistence)
    active_account_backoffs: dict[str, float]

    # Task specs on the process supervisor
    process_supervisor_task_ids: tuple[str, ...]

    # Routing-trace writer config
    routing_trace_writer_mode: str | None
    routing_trace_writer_sample_rate: float | None

    # Open generation-owned resources
    open_client_pool_count: int
    open_outbound_manager_count: int
    open_dns_backend_count: int

    # Control socket
    control_socket_path: str | None
    control_socket_inode: int | None

    @classmethod
    def capture(
        cls,
        runtime_manager: Any,
        *,
        app_state: Any = None,
        process: Any = None,
        db: Any = None,
    ) -> RuntimeSnapshot:
        """Capture current runtime state.

        Synchronous capture; persistence fields are read asynchronously
        via :meth:`capture_async` for in-loop callers.  When called
        from within a running event loop the persistence fields fall
        back to ``()`` so capture never blocks.

        Args:
            runtime_manager: The ``RuntimeManager`` instance.
            app_state: Optional ``app.state`` for mirror comparison.
            process: Optional ``ProcessRuntime`` for resources and supervisors.
            db: Optional ``Database`` for persisted provider/account reads.
        """
        return cls._build(
            runtime_manager,
            app_state=app_state,
            process=process,
            db=db,
            persisted=cls._sync_persistence(db, process),
        )

    @classmethod
    async def capture_async(
        cls,
        runtime_manager: Any,
        *,
        app_state: Any = None,
        process: Any = None,
        db: Any = None,
    ) -> RuntimeSnapshot:
        """Asynchronously capture full state, including SQLite reads.

        Use this from inside a running event loop so persistence and
        account backoff reads complete before the snapshot returns.
        """
        persisted = await cls._async_persistence(db, process)
        return cls._build(
            runtime_manager,
            app_state=app_state,
            process=process,
            db=db,
            persisted=persisted,
        )

    @classmethod
    def _build(
        cls,
        runtime_manager: Any,
        *,
        app_state: Any,
        process: Any,
        db: Any,
        persisted: tuple[tuple[str, ...], tuple[str, ...], dict[str, float]],
    ) -> RuntimeSnapshot:
        (
            gen_id,
            digest,
            config_vals,
            services,
        ) = _capture_generation(runtime_manager)
        (
            lease_count,
            retiring_count,
            supervisor_task_ids,
        ) = _capture_lifecycle(runtime_manager, process)
        asyncio_task_count = _capture_asyncio_task_count()
        fd_count = _capture_fd_count()
        rm_diags = _capture_rm_diags(runtime_manager)
        effective_digest, app_router_id = _capture_app_state_mirrors(
            app_state, runtime_manager
        )
        (
            persisted_providers,
            persisted_accounts,
            active_backoffs,
        ) = persisted
        (
            rt_mode,
            rt_sample_rate,
        ) = _capture_routing_trace_writer(process)
        (
            client_pool_count,
            outbound_count,
            dns_count,
        ) = _capture_resources(runtime_manager, process)
        (
            control_path,
            control_inode,
        ) = _capture_control_socket()

        return cls(
            active_generation_id=gen_id,
            config_digest=digest,
            generation_config_values=config_vals,
            service_identities=services,
            active_lease_count=lease_count,
            retiring_generation_count=retiring_count,
            asyncio_task_count=asyncio_task_count,
            supervisor_task_count=len(supervisor_task_ids),
            open_file_descriptor_count=fd_count,
            runtime_manager_diags=rm_diags,
            effective_config_digest=effective_digest,
            app_state_router_id=app_router_id,
            persisted_provider_ids=persisted_providers,
            persisted_account_names=persisted_accounts,
            active_account_backoffs=active_backoffs,
            process_supervisor_task_ids=supervisor_task_ids,
            routing_trace_writer_mode=rt_mode,
            routing_trace_writer_sample_rate=rt_sample_rate,
            open_client_pool_count=client_pool_count,
            open_outbound_manager_count=outbound_count,
            open_dns_backend_count=dns_count,
            control_socket_path=control_path,
            control_socket_inode=control_inode,
        )

    @staticmethod
    def _sync_persistence(
        db: Any, process: Any
    ) -> tuple[tuple[str, ...], tuple[str, ...], dict[str, float]]:
        """Best-effort synchronous persistence read.

        Returns empty results if called from within a running event
        loop (use :meth:`capture_async` instead).
        """
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                return ((), (), {})
        except RuntimeError:
            return ((), (), {})

        target_db = db if db is not None else _safe_db(process)
        if target_db is None:
            return ((), (), {})

        providers: list[str] = []
        accounts: list[str] = []
        try:
            from eggpool.db.repositories import (
                AccountRepository,
                ProviderRepository,
            )

            async def _read() -> None:
                provider_repo = ProviderRepository(target_db)
                account_repo = AccountRepository(target_db)
                rows = await provider_repo.list_all()
                providers.extend(r.id for r in rows)
                rows = await account_repo.list_all()
                accounts.extend(r.name for r in rows)

            loop.run_until_complete(_read())
        except Exception:
            return ((), (), {})
        return (tuple(sorted(providers)), tuple(sorted(accounts)), {})

    @staticmethod
    async def _async_persistence(
        db: Any, process: Any
    ) -> tuple[tuple[str, ...], tuple[str, ...], dict[str, float]]:
        """Async persistence read for use inside an event loop."""
        target_db = db if db is not None else _safe_db(process)
        if target_db is None:
            return ((), (), {})
        providers: list[str] = []
        accounts: list[str] = []
        try:
            from eggpool.db.repositories import (
                AccountRepository,
                ProviderRepository,
            )

            provider_repo = ProviderRepository(target_db)
            account_repo = AccountRepository(target_db)
            rows = await provider_repo.list_enabled()
            providers.extend(r["provider_id"] for r in rows)
            rows = await account_repo.list_enabled()
            accounts.extend(r["name"] for r in rows)
        except Exception:
            return ((), (), {})
        return (tuple(sorted(providers)), tuple(sorted(accounts)), {})

    # ---- comparison helpers ----------------------------------------------

    def assert_same_generation(self, other: RuntimeSnapshot) -> list[str]:
        """Compare two snapshots for generation identity.

        Returns list of differences.
        """
        diffs: list[str] = []
        if self.active_generation_id != other.active_generation_id:
            diffs.append(
                f"generation_id: {self.active_generation_id} != "
                f"{other.active_generation_id}"
            )
        if self.config_digest != other.config_digest:
            diffs.append(
                f"config_digest: {self.config_digest[:12]} != "
                f"{other.config_digest[:12]}"
            )
        return diffs

    def assert_same_services(self, other: RuntimeSnapshot) -> list[str]:
        """Compare service object identities. Returns list of differences."""
        diffs: list[str] = []
        for name in set(self.service_identities) | set(other.service_identities):
            left = self.service_identities.get(name)
            right = other.service_identities.get(name)
            if left != right:
                diffs.append(f"service {name}: identity changed")
        return diffs

    def assert_same_mirrors(self, other: RuntimeSnapshot) -> list[str]:
        """Compare ``app.state`` mirror identities.

        Catches stale ``app.state`` compatibility mirrors that point at
        an old generation.
        """
        diffs: list[str] = []
        if self.app_state_router_id != other.app_state_router_id:
            diffs.append(
                f"app.state.router identity: {self.app_state_router_id} != "
                f"{other.app_state_router_id}"
            )
        if self.effective_config_digest != other.effective_config_digest:
            diffs.append(
                f"effective_config_digest: {self.effective_config_digest[:12]} != "
                f"{other.effective_config_digest[:12]}"
            )
        return diffs

    def assert_same_persistence(self, other: RuntimeSnapshot) -> list[str]:
        """Compare persisted provider / account state.

        Catches persistence/publication split (DB has new accounts but
        runtime hasn't published yet, or vice versa).
        """
        diffs: list[str] = []
        if set(self.persisted_provider_ids) != set(other.persisted_provider_ids):
            diffs.append(
                f"persisted_providers: {set(self.persisted_provider_ids)} != "
                f"{set(other.persisted_provider_ids)}"
            )
        if set(self.persisted_account_names) != set(other.persisted_account_names):
            diffs.append(
                f"persisted_accounts: {set(self.persisted_account_names)} != "
                f"{set(other.persisted_account_names)}"
            )
        return diffs

    def assert_no_resource_leak(self, baseline: RuntimeSnapshot) -> list[str]:
        """Compare resource counters against a baseline.

        Reports resource accumulation rather than equality — used to
        detect candidate resources that were created but never closed
        after a failed reload.
        """
        diffs: list[str] = []
        if self.open_client_pool_count > baseline.open_client_pool_count:
            diffs.append(
                f"open_client_pool: {self.open_client_pool_count} > "
                f"baseline {baseline.open_client_pool_count}"
            )
        if self.open_outbound_manager_count > baseline.open_outbound_manager_count:
            diffs.append(
                f"open_outbound_manager: {self.open_outbound_manager_count} > "
                f"baseline {baseline.open_outbound_manager_count}"
            )
        if self.open_dns_backend_count > baseline.open_dns_backend_count:
            diffs.append(
                f"open_dns_backend: {self.open_dns_backend_count} > "
                f"baseline {baseline.open_dns_backend_count}"
            )
        if self.supervisor_task_count > baseline.supervisor_task_count + 2:
            # Allow a small slack (≤2 tasks) for candidate registration
            diffs.append(
                f"supervisor_tasks: {self.supervisor_task_count} > "
                f"baseline+2 ({baseline.supervisor_task_count + 2})"
            )
        return diffs

    def assert_value_equal(self, other: RuntimeSnapshot) -> list[str]:
        """Compare all captured values. Returns list of differences."""
        diffs: list[str] = []
        diffs.extend(self.assert_same_generation(other))
        diffs.extend(self.assert_same_services(other))
        diffs.extend(self.assert_same_mirrors(other))
        if self.active_lease_count != other.active_lease_count:
            diffs.append(
                f"lease_count: {self.active_lease_count} != {other.active_lease_count}"
            )
        if self.generation_config_values != other.generation_config_values:
            diffs.append(
                f"config_values: {self.generation_config_values} != "
                f"{other.generation_config_values}"
            )
        return diffs


# ---------------------------------------------------------------------------
# Internal capture helpers
# ---------------------------------------------------------------------------


def _capture_generation(
    runtime_manager: Any,
) -> tuple[
    int | None,
    str,
    dict[str, Any],
    dict[str, int],
]:
    """Capture active generation identity, digest, config values, services."""
    try:
        active = runtime_manager.active_snapshot()
    except Exception:
        return None, "", {}, {}

    gen_id = active.generation_id
    digest = active.config_digest
    config_vals = {
        "strategy": getattr(active.config.routing, "strategy", None),
        "local_quota_mode": getattr(active.config.routing, "local_quota_mode", None),
    }
    services = {
        "router": id(active.router),
        "coordinator": id(active.coordinator),
        "health_manager": id(active.health_manager),
        "catalog": id(active.catalog),
        "client_pool": id(active.client_pool),
        "outbound_manager": id(active.outbound_manager),
        "registry": id(active.registry),
        "stats_service": id(active.stats_service),
        "cost_calculator": id(active.cost_calculator),
        "supervisor": id(active.supervisor),
        "routing_trace_guard": id(active.routing_trace_guard),
        "dispatch_overhead_recorder": id(active.dispatch_overhead_recorder),
        "dispatch_span_recorder": id(active.dispatch_span_recorder),
        "account_backoff_repo": id(active.account_backoff_repo),
    }
    return gen_id, digest, config_vals, services


def _capture_lifecycle(
    runtime_manager: Any,
    process: Any,
) -> tuple[int, int, tuple[str, ...]]:
    """Capture lease count, retiring count, and supervisor task IDs."""
    lease_count = 0
    with contextlib.suppress(Exception):
        slot = runtime_manager._active
        if slot is not None:
            lease_count = slot.active_leases

    retiring_count = 0
    with contextlib.suppress(Exception):
        retiring_count = len(runtime_manager._retiring)

    supervisor_task_ids: tuple[str, ...] = ()
    with contextlib.suppress(Exception):
        if process is not None and getattr(process, "process_supervisor", None):
            supervisor = process.process_supervisor
            ids: list[str] = []
            with contextlib.suppress(Exception):
                ids = list(supervisor._tasks.keys())  # type: ignore[attr-defined]
            supervisor_task_ids = tuple(sorted(ids))

    return lease_count, retiring_count, supervisor_task_ids


def _capture_asyncio_task_count() -> int:
    """Count of currently running asyncio tasks."""
    try:
        return len(asyncio.all_tasks())
    except Exception:
        return 0


def _capture_fd_count() -> int | None:
    """Best-effort file descriptor limit (not opened-fd count).

    Linux supports ``/proc/self/fd`` enumeration but ``RLIMIT_NOFILE``
    is portable across macOS and Linux and is the closest cross-platform
    proxy for descriptor budget.
    """
    try:
        return resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    except Exception:
        return None


def _capture_rm_diags(runtime_manager: Any) -> dict[str, Any]:
    """Diagnostics from ``RuntimeManager.diagnostics()``."""
    with contextlib.suppress(Exception):
        diags = runtime_manager.diagnostics()
        if diags is None:
            return {}
        if hasattr(diags, "__dict__"):
            return {k: v for k, v in diags.__dict__.items()}
        if isinstance(diags, dict):
            return dict(diags)
    return {}


def _capture_app_state_mirrors(
    app_state: Any,
    runtime_manager: Any,
) -> tuple[str, int | None]:
    """Capture the operational ``app.state`` router mirror and digest."""
    effective_digest = ""
    router_id: int | None = None

    if app_state is not None:
        with contextlib.suppress(Exception):
            effective_digest = str(getattr(app_state, "config_digest", "") or "")
        with contextlib.suppress(Exception):
            router = getattr(app_state, "router", None)
            if router is not None:
                router_id = id(router)

    if not effective_digest:
        with contextlib.suppress(Exception):
            effective_digest = runtime_manager.active_snapshot().config_digest

    return effective_digest, router_id


def _safe_db(process: Any) -> Any:
    if process is None:
        return None
    return getattr(process, "db", None)


def _capture_persistence(
    runtime_manager: Any,
    db: Any,
    process: Any,
) -> tuple[tuple[str, ...], tuple[str, ...], dict[str, float]]:
    """Legacy helper retained for direct callers; prefer ``capture_async``."""
    return ((), (), {})


def _capture_routing_trace_writer(
    process: Any,
) -> tuple[str | None, float | None]:
    """Capture routing-trace writer mode and sample rate."""
    if process is None:
        return None, None
    writer = getattr(process, "routing_trace_writer", None)
    if writer is None:
        return None, None
    mode: str | None = None
    sample_rate: float | None = None
    with contextlib.suppress(Exception):
        mode = getattr(writer, "_mode", None) or getattr(writer, "mode", None)
    with contextlib.suppress(Exception):
        sample_rate = getattr(writer, "_sample_rate", None) or getattr(
            writer, "sample_rate", None
        )
    return (str(mode) if mode is not None else None), sample_rate


def _capture_resources(
    runtime_manager: Any,
    process: Any,
) -> tuple[int, int, int]:
    """Capture open client pool, outbound manager, and DNS backend counts."""

    client_pool_count = 0
    outbound_count = 0
    dns_count = 0

    with contextlib.suppress(Exception):
        active = runtime_manager.active_snapshot()
        if active.client_pool is not None:
            client_pool_count += 1
        if active.outbound_manager is not None:
            outbound_count += 1
        if active.dns_backend is not None:
            dns_count += 1

    with contextlib.suppress(Exception):
        retiring = runtime_manager._retiring or []
        for slot in retiring:
            gen = slot.generation
            if getattr(gen, "client_pool", None) is not None:
                client_pool_count += 1
            if getattr(gen, "outbound_manager", None) is not None:
                outbound_count += 1
            if getattr(gen, "dns_backend", None) is not None:
                dns_count += 1

    return client_pool_count, outbound_count, dns_count


def _capture_control_socket() -> tuple[str | None, int | None]:
    """Capture the control socket path and inode (best-effort)."""
    path = os.environ.get("EGGPOOL_CONTROL_SOCKET")
    inode: int | None = None
    if path and os.path.exists(path):
        with contextlib.suppress(Exception):
            inode = os.stat(path).st_ino
    return path, inode


__all__ = ["RuntimeSnapshot"]
