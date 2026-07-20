from __future__ import annotations

import asyncio
import contextlib
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
    """

    active_generation_id: int | None
    config_digest: str
    generation_config_values: dict[str, Any]
    service_identities: dict[str, int]  # name -> id() of service object
    active_lease_count: int
    active_task_count: int
    open_file_descriptor_count: int | None
    asyncio_task_count: int
    runtime_manager_diags: dict[str, Any]

    @classmethod
    def capture(
        cls,
        runtime_manager: Any,
        *,
        app_state: Any = None,
    ) -> RuntimeSnapshot:
        """Capture current runtime state.

        Args:
            runtime_manager: The RuntimeManager instance.
            app_state: Optional app.state for mirror comparison.
        """
        try:
            active = runtime_manager.active_snapshot()
            gen_id = active.generation_id
            digest = active.config_digest
            config_vals = {
                "strategy": getattr(active.config.routing, "strategy", None),
                "local_quota_mode": getattr(
                    active.config.routing, "local_quota_mode", None
                ),
            }
            services = {
                "router": id(active.router),
                "coordinator": id(active.coordinator),
                "health_manager": id(active.health_manager),
                "catalog": id(active.catalog),
                "client_pool": id(active.client_pool),
            }
        except Exception:
            gen_id = None
            digest = ""
            config_vals = {}
            services = {}

        # Count active leases
        lease_count = 0
        with contextlib.suppress(Exception):
            slot = runtime_manager._active
            if slot is not None:
                lease_count = slot.active_leases

        # Count asyncio tasks (EggPool-owned filter would be ideal but approximate)
        task_count = len(asyncio.all_tasks())

        # File descriptors (portable best-effort)
        fd_count = None
        with contextlib.suppress(Exception):
            fd_count = resource.getrlimit(resource.RLIMIT_NOFILE)[0]

        # Runtime manager diagnostics
        diags: dict[str, Any] = {}
        with contextlib.suppress(Exception):
            diags = runtime_manager.diagnostics()

        return cls(
            active_generation_id=gen_id,
            config_digest=digest,
            generation_config_values=config_vals,
            service_identities=services,
            active_lease_count=lease_count,
            active_task_count=0,  # placeholder
            open_file_descriptor_count=fd_count,
            asyncio_task_count=task_count,
            runtime_manager_diags=diags,
        )

    def assert_same_generation(self, other: RuntimeSnapshot) -> list[str]:
        """Compare two snapshots for generation identity.

        Returns list of differences.
        """
        diffs: list[str] = []
        if self.active_generation_id != other.active_generation_id:
            left_id = self.active_generation_id
            right_id = other.active_generation_id
            diffs.append(f"generation_id: {left_id} != {right_id}")
        if self.config_digest != other.config_digest:
            left_d = self.config_digest[:12]
            right_d = other.config_digest[:12]
            diffs.append(f"config_digest: {left_d} != {right_d}")
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

    def assert_value_equal(self, other: RuntimeSnapshot) -> list[str]:
        """Compare all captured values. Returns list of differences."""
        diffs: list[str] = []
        diffs.extend(self.assert_same_generation(other))
        diffs.extend(self.assert_same_services(other))
        if self.active_lease_count != other.active_lease_count:
            left_lc = self.active_lease_count
            right_lc = other.active_lease_count
            diffs.append(f"lease_count: {left_lc} != {right_lc}")
        if self.generation_config_values != other.generation_config_values:
            left_cv = self.generation_config_values
            right_cv = other.generation_config_values
            diffs.append(f"config_values: {left_cv} != {right_cv}")
        return diffs
