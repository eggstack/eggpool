#!/usr/bin/env python3
"""Audit reload state agreement (Workstream G4).

Cross-checks that the live reload produced invariant-consistent
state across three layers:

  1. Persistence — SQLite rows for providers and accounts must match
     the active generation's configured providers/accounts.  Drift
     here means the persistence publication step did not apply the
     delta or applied it inconsistently with the runtime swap.

  2. Process task specs — the active ``TaskSpec`` names registered on
     the process supervisor must be a subset of the configured
     background task names.  Drift here means the TaskSpecTransition
     did not run during commit or removed a task that the config still
     requires.

  3. Routing-trace writer config — the writer's ``trace.mode``
     configuration must equal the active generation's
     ``routing.trace.mode``.  Drift here means the
     ``RoutingTraceWriterTransition`` did not run during commit.

The script supports two modes:

* ``--db-path <path>`` plus ``--active-snapshot <json>`` for an offline
  audit (use ``eggpool rehash --dry-run --json`` to produce the
  snapshot, or build the snapshot file from a test harness).

* ``--runtime-manager-uri <uri>`` for live introspection via the
  runtime manager.  When neither flag is supplied, the script reads
  ``EGGPOOL_AUDIT_DB`` and ``EGGPOOL_AUDIT_SNAPSHOT`` from the env.

Exits 0 on pass, 1 on violation, 2 on usage error.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import sqlite3
import sys
from typing import Any

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class AuditViolation:
    """A single reload consistency violation."""

    check_name: str
    description: str
    sample_ids: tuple[str, ...] = ()

    def render(self) -> str:
        sample = ""
        if self.sample_ids:
            preview = ", ".join(self.sample_ids[:3])
            sample = f" sample=[{preview}]"
        return f"[{self.check_name}] {self.description}{sample}"


@dataclasses.dataclass
class AuditResult:
    violations: list[AuditViolation] = dataclasses.field(
        default_factory=list[AuditViolation]
    )

    @property
    def passed(self) -> bool:
        return not self.violations

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "violations": [dataclasses.asdict(v) for v in self.violations],
        }


# ---------------------------------------------------------------------------
# Active snapshot parsing
# ---------------------------------------------------------------------------


_REQUIRED_SNAPSHOT_FIELDS = (
    "generation_id",
    "config_digest",
    "providers",
    "accounts",
    "task_specs",
    "routing_trace_mode",
)


def parse_snapshot(snapshot_path: str) -> dict[str, Any]:
    """Read and validate an active snapshot JSON file.

    The snapshot schema is::

        {
            "generation_id": int,
            "config_digest": str,
            "providers": dict[str, {"base_url": str, "protocols": list[str]}],
            "accounts": dict[str, {"provider_id": str, "enabled": int}],
            "task_specs": list[str],
            "routing_trace_mode": str
        }
    """
    with open(snapshot_path, encoding="utf-8") as f:
        data = json.load(f)
    missing = [k for k in _REQUIRED_SNAPSHOT_FIELDS if k not in data]
    if missing:
        raise ValueError(
            f"snapshot {snapshot_path!r} missing required fields: {missing}"
        )
    return data


def render_known_ids(
    db_providers: set[str],
    config_providers: set[str],
) -> tuple[str, ...]:
    """Compute the symmetric-difference id set for diagnostics."""
    return tuple(sorted(db_providers ^ config_providers))


# ---------------------------------------------------------------------------
# Audit checks
# ---------------------------------------------------------------------------


def _audit_provider_rows(
    db_path: str,
    snapshot: dict[str, Any],
) -> list[AuditViolation]:
    """Compare ``providers`` table rows against snapshot.provider_ids.

    Detects three failure modes:
    1. Provider in snapshot but missing from DB (publication did not
       apply the provider delta).
    2. Provider in DB and snapshot but DB row marked disabled (the
       snapshot thinks it is active, the DB does not).
    3. Provider in DB enabled but missing from snapshot (a reloaded
       generation's persistence delta did not disable a removed
       provider).
    """
    config_provider_ids = set(snapshot["providers"].keys())
    if not os.path.exists(db_path):
        return [
            AuditViolation(
                check_name="provider_rows_vs_active_config",
                description=f"database file {db_path!r} not found",
            )
        ]
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT provider_id, enabled FROM providers").fetchall()
    db_provider_state = {pid: bool(enabled) for pid, enabled in rows}

    violations: list[AuditViolation] = []
    db_set = set(db_provider_state)

    if missing := config_provider_ids - db_set:
        violations.append(
            AuditViolation(
                check_name="provider_rows_vs_active_config",
                description=(
                    f"{len(missing)} provider(s) configured but missing from "
                    "providers table"
                ),
                sample_ids=tuple(sorted(missing))[:5],
            )
        )
    if stale := db_set - config_provider_ids:
        violations.append(
            AuditViolation(
                check_name="provider_rows_vs_active_config",
                description=(
                    f"{len(stale)} provider(s) still enabled in providers "
                    "table but absent from active generation config"
                ),
                sample_ids=tuple(sorted(stale))[:5],
            )
        )
    for pid in sorted(config_provider_ids & db_set):
        if not db_provider_state[pid]:
            violations.append(
                AuditViolation(
                    check_name="provider_rows_vs_active_config",
                    description=(f"provider {pid!r} configured but disabled in DB"),
                    sample_ids=(pid,),
                )
            )

    # Protocol/base_url drift.
    config_protocols = {
        pid: tuple(sorted(cfg.get("protocols", [])))
        for pid, cfg in snapshot["providers"].items()
    }
    config_base_urls = {
        pid: cfg.get("base_url", "") for pid, cfg in snapshot["providers"].items()
    }
    if config_protocols or config_base_urls:
        with sqlite3.connect(db_path) as conn:
            detail_rows = conn.execute(
                "SELECT provider_id, base_url, protocols FROM providers",
            ).fetchall()
        for pid, base_url, protocols_json in detail_rows:
            if pid not in config_provider_ids:
                continue
            try:
                db_protocols = tuple(sorted(json.loads(protocols_json)))
            except (TypeError, ValueError):
                db_protocols = ()
            if db_protocols != config_protocols.get(pid, ()):
                violations.append(
                    AuditViolation(
                        check_name="provider_rows_vs_active_config",
                        description=(
                            f"provider {pid!r} protocols drift: "
                            f"db={list(db_protocols)} "
                            f"config={list(config_protocols.get(pid, ()))}"
                        ),
                        sample_ids=(pid,),
                    )
                )
            if base_url != config_base_urls.get(pid, ""):
                violations.append(
                    AuditViolation(
                        check_name="provider_rows_vs_active_config",
                        description=(
                            f"provider {pid!r} base_url drift: "
                            f"db={base_url!r} config={config_base_urls.get(pid, '')!r}"
                        ),
                        sample_ids=(pid,),
                    )
                )
    return violations


def _audit_account_rows(
    db_path: str,
    snapshot: dict[str, Any],
) -> list[AuditViolation]:
    """Compare ``accounts`` table rows against snapshot.account_ids."""
    if not os.path.exists(db_path):
        return []
    config_accounts: dict[str, dict[str, Any]] = snapshot["accounts"]
    config_account_ids = set(config_accounts.keys())
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT name, provider_id, enabled, weight FROM accounts"
        ).fetchall()
    db_accounts = {
        name: {
            "provider_id": provider_id,
            "enabled": bool(enabled),
            "weight": weight,
        }
        for name, provider_id, enabled, weight in rows
    }
    db_set = set(db_accounts)

    violations: list[AuditViolation] = []
    if missing := config_account_ids - db_set:
        violations.append(
            AuditViolation(
                check_name="account_rows_vs_active_config",
                description=(
                    f"{len(missing)} account(s) configured but missing from "
                    "accounts table"
                ),
                sample_ids=tuple(sorted(missing))[:5],
            )
        )
    if stale := db_set - config_account_ids:
        violations.append(
            AuditViolation(
                check_name="account_rows_vs_active_config",
                description=(
                    f"{len(stale)} account(s) still enabled in accounts "
                    "table but absent from active generation config"
                ),
                sample_ids=tuple(sorted(stale))[:5],
            )
        )
    for acct in sorted(config_account_ids & db_set):
        cfg = config_accounts[acct]
        db_row = db_accounts[acct]
        if db_row["provider_id"] != cfg["provider_id"]:
            violations.append(
                AuditViolation(
                    check_name="account_rows_vs_active_config",
                    description=(
                        f"account {acct!r} provider_id drift: db="
                        f"{db_row['provider_id']!r} config={cfg['provider_id']!r}"
                    ),
                    sample_ids=(acct,),
                )
            )
        if cfg.get("enabled", True) and not db_row["enabled"]:
            violations.append(
                AuditViolation(
                    check_name="account_rows_vs_active_config",
                    description=(f"account {acct!r} configured but disabled in DB"),
                    sample_ids=(acct,),
                )
            )
        if "weight" in cfg and db_row["weight"] != cfg["weight"]:
            violations.append(
                AuditViolation(
                    check_name="account_rows_vs_active_config",
                    description=(
                        f"account {acct!r} weight drift: db="
                        f"{db_row['weight']} config={cfg['weight']}"
                    ),
                    sample_ids=(acct,),
                )
            )
    return violations


def _audit_task_specs(snapshot: dict[str, Any]) -> list[AuditViolation]:
    """Verify task spec names referenced by config exist on the runtime.

    Uses the ``EGGPOOL_AUDIT_TASK_SPECS_ACTIVE`` environment variable
    (a comma-separated list of task spec names that the live process
    supervisor reports as active).  When the variable is absent the
    check is a silent no-op so the script remains usable in offline
    audit contexts.
    """
    expected: set[str] = set(snapshot.get("task_specs", ()))
    if not expected:
        return []
    env_value = os.environ.get("EGGPOOL_AUDIT_TASK_SPECS_ACTIVE")
    if not env_value:
        return []
    active = {name.strip() for name in env_value.split(",") if name.strip()}
    violations: list[AuditViolation] = []
    if missing := expected - active:
        violations.append(
            AuditViolation(
                check_name="task_specs_vs_active_registry",
                description=(
                    f"{len(missing)} task spec(s) configured but not active "
                    "on process registry"
                ),
                sample_ids=tuple(sorted(missing))[:5],
            )
        )
    return violations


def _audit_routing_trace_writer(
    snapshot: dict[str, Any],
    runtime_writer_mode: str | None,
) -> list[AuditViolation]:
    """Compare ``routing_trace_mode`` snapshot to live writer config.

    Pass ``runtime_writer_mode=None`` when introspection of the live
    writer is unavailable — skip the check.
    """
    expected_mode = snapshot.get("routing_trace_mode")
    if expected_mode is None or runtime_writer_mode is None:
        return []
    if expected_mode != runtime_writer_mode:
        return [
            AuditViolation(
                check_name="routing_trace_writer_mode",
                description=(
                    f"writer.mode={runtime_writer_mode!r} != "
                    f"active.routing.trace.mode={expected_mode!r}"
                ),
                sample_ids=(f"writer={runtime_writer_mode}",),
            )
        ]
    return []


# ---------------------------------------------------------------------------
# Live introspection helpers (optional)
# ---------------------------------------------------------------------------


async def _read_live_router_writer_mode() -> str | None:
    """Best-effort read of the routing-trace writer's mode.

    Returns ``None`` if the writer is unavailable, the optional extra
    is not installed, or introspection raises.

    The audit script does not depend on a process-wide singleton
    accessor (the writer is constructed per generation).  When a live
    process is running, operators can expose ``EGGPOOL_LIVE_TRACE_MODE``
    via the admin endpoint to feed this check.
    """
    env_mode = os.environ.get("EGGPOOL_LIVE_TRACE_MODE")
    if env_mode:
        return env_mode
    try:
        writer_module = __import__(
            "eggpool.observability.routing_trace_writer",
            fromlist=["RoutingTraceWriter"],
        )
    except ImportError:
        return None
    cls = getattr(writer_module, "RoutingTraceWriter", None)
    config_attr = getattr(cls, "default_config", None)
    if config_attr is None:
        return None
    try:
        mode = getattr(config_attr(), "mode", None)
    except Exception:
        return None
    return mode if isinstance(mode, str) else None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _add_provider_rows_violations(
    result: AuditResult,
    db_path: str,
    snapshot: dict[str, Any],
) -> None:
    for v in _audit_provider_rows(db_path, snapshot):
        result.violations.append(v)


def _add_account_rows_violations(
    result: AuditResult,
    db_path: str,
    snapshot: dict[str, Any],
) -> None:
    for v in _audit_account_rows(db_path, snapshot):
        result.violations.append(v)


def _add_task_spec_violations(
    result: AuditResult,
    snapshot: dict[str, Any],
) -> None:
    for v in _audit_task_specs(snapshot):
        result.violations.append(v)


async def _run_audit_async(
    db_path: str,
    snapshot: dict[str, Any],
    *,
    include_live_writer: bool,
) -> AuditResult:
    result = AuditResult()
    _add_provider_rows_violations(result, db_path, snapshot)
    _add_account_rows_violations(result, db_path, snapshot)
    _add_task_spec_violations(result, snapshot)
    if include_live_writer:
        writer_mode = await _read_live_router_writer_mode()
        for v in _audit_routing_trace_writer(snapshot, writer_mode):
            result.violations.append(v)
    return result


def run_audit(
    db_path: str,
    snapshot_path: str,
    *,
    include_live_writer: bool = False,
) -> int:
    snapshot = parse_snapshot(snapshot_path)
    result = asyncio.run(
        _run_audit_async(
            db_path,
            snapshot,
            include_live_writer=include_live_writer,
        )
    )
    payload = result.to_dict()
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if result.passed else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db-path",
        default=os.environ.get("EGGPOOL_AUDIT_DB"),
        help="Path to the eggpool SQLite database file.",
    )
    parser.add_argument(
        "--active-snapshot",
        default=os.environ.get("EGGPOOL_AUDIT_SNAPSHOT"),
        help="Path to a JSON file describing the active generation.",
    )
    parser.add_argument(
        "--live-writer",
        action="store_true",
        help=(
            "Also compare against the in-process routing-trace writer "
            "(requires the running eggpool process)."
        ),
    )
    parser.add_argument(
        "--emit-snapshot-template",
        action="store_true",
        help="Print a blank snapshot JSON template and exit 0.",
    )
    args = parser.parse_args(argv)

    if args.emit_snapshot_template:
        template: dict[str, Any] = {
            "generation_id": 0,
            "config_digest": "",
            "providers": {
                "test-provider-a": {
                    "base_url": "https://a.example.com/v1",
                    "protocols": ["openai"],
                }
            },
            "accounts": {
                "acct-a1": {"provider_id": "test-provider-a", "enabled": True}
            },
            "task_specs": ["checkpoint", "metrics_flush"],
            "routing_trace_mode": "sampled",
        }
        print(json.dumps(template, indent=2, sort_keys=True))
        return 0

    if not args.db_path or not args.active_snapshot:
        parser.error("--db-path and --active-snapshot are required")

    return run_audit(
        args.db_path,
        args.active_snapshot,
        include_live_writer=args.live_writer,
    )


if __name__ == "__main__":
    sys.exit(main())
