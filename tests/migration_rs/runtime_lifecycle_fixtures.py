"""Deterministic R001 observations from the Python runtime lifecycle.

This module is a bounded migration oracle.  It imports the production runtime,
reload, policy, and task-inventory contracts and projects only stable facts
that a later Rust lifecycle can consume.  It deliberately does not serialize
configuration values, timestamps, request bodies, or credentials.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from eggpool.config_reload_policy import (
    _FIELD_DISPOSITION,
    _SECRET_FIELD_NAMES,
    _disposition_for,
    _iter_tracked_fields,
    compute_diff,
    sanitize_text_for_audit,
)
from eggpool.models.config import AppConfig
from eggpool.reload_diagnostics import (
    ReloadResultCategory,
    ReloadTerminalStage,
    classify_result_category,
)
from eggpool.reload_transaction import (
    _VALID_TRANSITIONS,
    ReloadAcceptanceState,
    TransactionState,
)
from eggpool.runtime_manager import (
    _RUNTIME_OWNED_APP_STATE_ATTRS,
    CandidateOwnershipState,
    PendingSwapState,
    ProcessRuntime,
    RuntimeGeneration,
    SlotState,
)
from eggpool.runtime_task_inventory import (
    RUNTIME_TASK_INVENTORY,
    RuntimeTaskSpec,
    inventory_for_config,
)

ROOT = Path(__file__).parents[2]
FIXTURE_PATH = (
    ROOT
    / "migration-rs"
    / "fixtures"
    / "runtime-lifecycle"
    / "r001-python-observations.json"
)

SCHEMA_VERSION = "m8-runtime-lifecycle-r001-observations/v1"


def _enum_values(enum_type: Any) -> list[str]:  # noqa: ANN401
    return [member.value for member in enum_type]


def _task_projection(spec: RuntimeTaskSpec) -> dict[str, object]:
    return {
        "name": spec.name,
        "ownership": spec.ownership.value,
        "default_enabled": spec.enabled,
        "interval_s": spec.interval_s,
        "initial_delay_s": spec.initial_delay_s,
        "run_immediately": spec.run_immediately,
        "timeout_s": spec.timeout_s,
        "description": spec.description,
        "reloadable_fields": list(spec.reloadable_fields),
        "generation_dependencies": list(spec.generation_dependencies),
        "process_dependencies": list(spec.process_dependencies),
        "callback_kind": spec.callback_kind,
    }


def _resolved_tasks(config: AppConfig) -> list[dict[str, object]]:
    return [
        {
            "name": spec.name,
            "enabled": spec.enabled,
            "interval_s": spec.interval_s,
            "initial_delay_s": spec.initial_delay_s,
            "run_immediately": spec.run_immediately,
        }
        for spec in inventory_for_config(config, include_update_checker=True)
    ]


def _config_with_changes(config: AppConfig, **updates: Any) -> AppConfig:  # noqa: ANN401
    return config.model_copy(update=updates)


def _diff_projection(old: AppConfig, new: AppConfig) -> dict[str, object]:
    diff = compute_diff(old, new)
    return {
        "paths": [change.path for change in diff.changes],
        "dispositions": [change.disposition.value for change in diff.changes],
        "sections": sorted({change.section for change in diff.changes}),
        "restart_required": [change.path for change in diff.restart_required],
        "live": [change.path for change in diff.live],
        "changes": [
            {
                "path": change.path,
                "disposition": change.disposition.value,
                "old_display": change.old_display,
                "new_display": change.new_display,
                "section": change.section,
                "secret": change.secret,
            }
            for change in diff.changes
        ],
    }


def _reload_case(
    name: str,
    *,
    category: ReloadResultCategory,
    terminal_stage: ReloadTerminalStage,
    legacy_stage: str,
    accepted: bool,
    publication: bool,
    generation_delta: int,
    active_unchanged: bool,
    retirement_pending: bool | str,
    changed_sections: tuple[str, ...] = (),
    restart_required_paths: tuple[str, ...] = (),
) -> dict[str, object]:
    return {
        "name": name,
        "category": category.value,
        "terminal_stage": terminal_stage.value,
        "legacy_wire_stage": legacy_stage,
        "accepted": accepted,
        "publication_occurred": publication,
        "generation_delta": generation_delta,
        "active_generation_unchanged": active_unchanged,
        "retirement_pending": retirement_pending,
        "changed_sections": list(changed_sections),
        "restart_required_paths": list(restart_required_paths),
    }


def _candidate_abort_probe() -> dict[str, object]:
    """Exercise candidate abort with only local callbacks and no real resources."""

    async def _run() -> tuple[str, ...]:
        from eggpool.runtime_manager import RuntimeGenerationCandidate

        candidate = RuntimeGenerationCandidate(generation_id=7)
        closed: list[str] = []

        def _close(name: str) -> None:
            closed.append(name)

        for name in ("client_pool", "outbound_manager", "supervisor"):
            candidate.register_resource(name, lambda name=name: _close(name))
        diagnostics = await candidate.abort(
            RuntimeError("fixture failure"), failure_stage="build"
        )
        assert diagnostics.resource_types_closed == (
            "supervisor",
            "outbound_manager",
            "client_pool",
        )
        return tuple(closed)

    return {
        "registered_order": ["client_pool", "outbound_manager", "supervisor"],
        "closed_order": list(asyncio.run(_run())),
        "primary_failure_stage": "build",
        "cleanup_errors_preserve_primary_failure": True,
        "abort_is_idempotent": True,
        "process_owned_resources_touched": False,
    }


def _ownership_inventory() -> list[dict[str, object]]:
    return [
        {
            "name": "database_and_repositories",
            "ownership": "process-owned",
            "authority": "ProcessRuntime.db/stats_db",
            "lifetime": "process",
        },
        {
            "name": "provider_client_pool",
            "ownership": "generation-owned",
            "authority": "RuntimeGeneration.client_pool",
            "lifetime": "generation retirement",
        },
        {
            "name": "outbound_client_manager",
            "ownership": "generation-owned",
            "authority": "RuntimeGeneration.outbound_manager",
            "lifetime": "generation retirement",
        },
        {
            "name": "account_registry",
            "ownership": "generation-owned",
            "authority": "RuntimeGeneration.registry",
            "lifetime": "generation retirement",
        },
        {
            "name": "catalog_service",
            "ownership": "generation-owned",
            "authority": "RuntimeGeneration.catalog",
            "lifetime": "generation retirement",
        },
        {
            "name": "router_and_coordinator",
            "ownership": "generation-owned",
            "authority": "RuntimeGeneration.router/coordinator",
            "lifetime": "generation retirement",
        },
        {
            "name": "finalization_supervisor",
            "ownership": "generation-owned",
            "authority": "RuntimeGeneration.finalization_supervisor",
            "lifetime": "after leases and terminal references drain",
        },
        {
            "name": "model_router_affinity",
            "ownership": "process-owned",
            "authority": "ProcessRuntime.model_router_affinity",
            "lifetime": "process; revalidated per generation",
        },
        {
            "name": "wire_profile_resolver",
            "ownership": "process-owned",
            "authority": "ProcessRuntime.wire_profile_resolver",
            "lifetime": "process; fingerprint-qualified",
        },
        {
            "name": "generation_task_supervisor",
            "ownership": "generation-owned",
            "authority": "RuntimeGeneration.supervisor",
            "lifetime": "generation retirement",
        },
        {
            "name": "process_task_supervisor",
            "ownership": "process-owned",
            "authority": "ProcessRuntime.process_supervisor",
            "lifetime": "process singleton",
        },
        {
            "name": "crash_reconciler",
            "ownership": "process-owned/startup-only",
            "authority": "startup lifecycle entry point",
            "lifetime": "one bounded startup pass",
        },
        {
            "name": "runtime_metrics_and_reload_diagnostics",
            "ownership": "process-owned",
            "authority": "RuntimeMetricsService and reload metadata",
            "lifetime": "process; bounded snapshots",
        },
        {
            "name": "server_constructor_state",
            "ownership": "startup-only/restart-required",
            "authority": "listener, middleware, route topology, auth, DB constructor",
            "lifetime": "process construction",
        },
        {
            "name": "candidate_resources_before_publication",
            "ownership": "candidate-owned",
            "authority": "RuntimeGenerationCandidate",
            "lifetime": "building/prepared until transfer or abort",
        },
        {
            "name": "request_and_stream_lease",
            "ownership": "request/lease-owned",
            "authority": "GenerationLease",
            "lifetime": "finite completion or stream disconnect",
        },
        {
            "name": "retained_finalization",
            "ownership": "retained-finalization-owned",
            "authority": "terminal reference on generation slot",
            "lifetime": "durable/runtime convergence",
        },
    ]


def _lifecycle_contract() -> dict[str, object]:
    transaction_transitions = {
        state.value: sorted(next_state.value for next_state in next_states)
        for state, next_states in _VALID_TRANSITIONS.items()
    }
    return {
        "candidate": {
            "states": _enum_values(CandidateOwnershipState),
            "legal_transitions": [
                ["building", "prepared"],
                ["prepared", "transferred"],
                ["building", "aborted"],
                ["prepared", "aborted"],
            ],
            "abort": _candidate_abort_probe(),
        },
        "generation_slot": {
            "states": _enum_values(SlotState),
            "legal_transitions": [
                ["active", "retiring"],
                ["retiring", "closing"],
                ["closing", "closed"],
                ["closing", "failed_close"],
            ],
            "retirement_ready_when": [
                "active_leases == 0",
                "terminal_references == 0",
            ],
        },
        "pending_swap": {
            "states": _enum_values(PendingSwapState),
            "legal_transitions": [
                ["prepared", "staged"],
                ["staged", "committed"],
                ["staged", "rolled_back"],
                ["committed", "finalized"],
            ],
            "stage_effects": [
                "lease_admission_gated",
                "candidate_slot_non_accepting",
                "active_slot_unchanged",
            ],
            "commit_effects": [
                "candidate_becomes_active",
                "old_slot_stops_accepting_leases",
                "publication_epoch_increments",
                "lease_admission_reopens",
            ],
            "rollback_effects": [
                "old_slot_restored",
                "candidate_slot_dropped",
                "lease_admission_reopens",
                "candidate_requires_abort",
            ],
        },
        "lease": {
            "claim_timeout_s": 30.0,
            "claim_predicate": (
                "shutdown_in_progress OR "
                "(NOT lease_admission_gated AND active_slot_accepting)"
            ),
            "claim_and_increment_are_one_critical_section": True,
            "release_is_exactly_once": True,
            "stream_release_boundary": "iterator finally after handler return",
        },
        "publication_gate": {
            "pre_gate_work": [
                "config parsing and validation",
                "digest check and diff",
                "candidate construction",
                "persistence delta preparation",
                "task diff preflight",
            ],
            "inside_gate": [
                "stage candidate",
                "apply bounded mandatory acceptance changes",
                "commit runtime pointer",
                "commit task-spec state",
            ],
            "forbidden_inside_gate": [
                "provider/network calls",
                "catalog refresh",
                "backup",
                "unbounded work",
            ],
        },
        "close_order": [
            "generation supervisor stop_all",
            "generation cleanup callbacks",
            "finalization supervisor shutdown",
            "provider client pool close",
            "outbound client manager aclose",
        ],
        "shutdown": {
            "stops_new_publication_and_acquisition": True,
            "adopts_unfinalized_committed_swap_old_slot": True,
            "forced_close_abandons_terminal_references_only_on_process_shutdown": True,
            "live_rehash_preserves_accepted_work": True,
            "manager_join_deadline_s": 10.0,
            "retirement_drain_deadline_s": 5.0,
        },
        "transaction": {
            "states": _enum_values(TransactionState),
            "transitions": transaction_transitions,
            "acceptance_states": _enum_values(ReloadAcceptanceState),
            "accepted_reload_cannot_abort": True,
        },
    }


def _reload_contract(config: AppConfig) -> dict[str, object]:
    server_live = config.server.model_copy(
        update={"max_request_body_bytes": config.server.max_request_body_bytes + 1}
    )
    server_restart = config.server.model_copy(update={"port": config.server.port + 1})
    mixed_server = config.server.model_copy(
        update={
            "port": config.server.port + 1,
            "max_request_body_bytes": config.server.max_request_body_bytes + 1,
        }
    )
    pricing = config.pricing.catalogs.openrouter.model_copy(
        update={"api_key": "fixture-placeholder-not-retained"}
    )
    pricing_catalogs = config.pricing.catalogs.model_copy(
        update={"openrouter": pricing}
    )
    pricing_config = config.pricing.model_copy(update={"catalogs": pricing_catalogs})

    cases = [
        _reload_case(
            "identical_digest_noop",
            category=ReloadResultCategory.SUCCESS_NOOP,
            terminal_stage=ReloadTerminalStage.IDLE,
            legacy_stage="commit",
            accepted=True,
            publication=False,
            generation_delta=0,
            active_unchanged=True,
            retirement_pending=False,
        ),
        _reload_case(
            "valid_live_only_change",
            category=ReloadResultCategory.SUCCESS_COMMITTED,
            terminal_stage=ReloadTerminalStage.RETIREMENT,
            legacy_stage="retirement",
            accepted=True,
            publication=True,
            generation_delta=1,
            active_unchanged=False,
            retirement_pending="true_if_old_work_remains",
            changed_sections=("server",),
        ),
        _reload_case(
            "restart_required_change",
            category=ReloadResultCategory.REJECTED_RESTART_REQUIRED,
            terminal_stage=ReloadTerminalStage.DIFF,
            legacy_stage="diff",
            accepted=False,
            publication=False,
            generation_delta=0,
            active_unchanged=True,
            retirement_pending=False,
            changed_sections=("server",),
            restart_required_paths=("server.port",),
        ),
        _reload_case(
            "mixed_live_and_restart_required_change",
            category=ReloadResultCategory.REJECTED_RESTART_REQUIRED,
            terminal_stage=ReloadTerminalStage.DIFF,
            legacy_stage="diff",
            accepted=False,
            publication=False,
            generation_delta=0,
            active_unchanged=True,
            retirement_pending=False,
            changed_sections=("server",),
            restart_required_paths=("server.port",),
        ),
        _reload_case(
            "invalid_config",
            category=ReloadResultCategory.REJECTED_VALIDATION,
            terminal_stage=ReloadTerminalStage.VALIDATION,
            legacy_stage="validation",
            accepted=False,
            publication=False,
            generation_delta=0,
            active_unchanged=True,
            retirement_pending=False,
        ),
        _reload_case(
            "expected_digest_mismatch",
            category=ReloadResultCategory.REJECTED_VALIDATION,
            terminal_stage=ReloadTerminalStage.VALIDATION,
            legacy_stage="digest_check",
            accepted=False,
            publication=False,
            generation_delta=0,
            active_unchanged=True,
            retirement_pending=False,
        ),
        _reload_case(
            "stale_caller_generation",
            category=ReloadResultCategory.REJECTED_VALIDATION,
            terminal_stage=ReloadTerminalStage.PREPARATION,
            legacy_stage="preparation",
            accepted=False,
            publication=False,
            generation_delta=0,
            active_unchanged=True,
            retirement_pending=False,
        ),
        _reload_case(
            "candidate_construction_failure",
            category=ReloadResultCategory.FAILED_CANDIDATE_PREPARE,
            terminal_stage=ReloadTerminalStage.PREPARATION,
            legacy_stage="preparation",
            accepted=False,
            publication=False,
            generation_delta=0,
            active_unchanged=True,
            retirement_pending=False,
        ),
        _reload_case(
            "preflight_process_transition_failure",
            category=ReloadResultCategory.FAILED_PROCESS_TRANSITION_PREPARE,
            terminal_stage=ReloadTerminalStage.PREPARATION,
            legacy_stage="preparation",
            accepted=False,
            publication=False,
            generation_delta=0,
            active_unchanged=True,
            retirement_pending=False,
        ),
        _reload_case(
            "cancellation_before_acceptance",
            category=ReloadResultCategory.ABORTED_CANCELLED,
            terminal_stage=ReloadTerminalStage.PREPARATION,
            legacy_stage="preparation",
            accepted=False,
            publication=False,
            generation_delta=0,
            active_unchanged=True,
            retirement_pending=False,
        ),
        _reload_case(
            "cancellation_during_or_after_acceptance",
            category=ReloadResultCategory.POST_COMMIT_FINALIZATION_PENDING,
            terminal_stage=ReloadTerminalStage.RETIREMENT,
            legacy_stage="retirement",
            accepted=True,
            publication=True,
            generation_delta=1,
            active_unchanged=False,
            retirement_pending=True,
        ),
        _reload_case(
            "successful_publication_retirement_pending",
            category=ReloadResultCategory.POST_COMMIT_FINALIZATION_PENDING,
            terminal_stage=ReloadTerminalStage.RETIREMENT,
            legacy_stage="retirement",
            accepted=True,
            publication=True,
            generation_delta=1,
            active_unchanged=False,
            retirement_pending=True,
        ),
        _reload_case(
            "successful_publication_already_drained",
            category=ReloadResultCategory.SUCCESS_COMMITTED,
            terminal_stage=ReloadTerminalStage.RETIREMENT,
            legacy_stage="retirement",
            accepted=True,
            publication=True,
            generation_delta=1,
            active_unchanged=False,
            retirement_pending=False,
        ),
    ]
    category_checks = {
        "success_noop": classify_result_category(
            ok=True,
            stage=ReloadTerminalStage.IDLE,
            is_noop=True,
        ).value,
        "success_committed": classify_result_category(
            ok=True,
            stage=ReloadTerminalStage.RETIREMENT,
        ).value,
        "retry_pending": classify_result_category(
            ok=True,
            stage=ReloadTerminalStage.RETIREMENT,
            finalization_status="retry_pending",
        ).value,
        "validation": classify_result_category(
            ok=False,
            stage=ReloadTerminalStage.VALIDATION,
        ).value,
    }
    return {
        "legacy_stages": [
            "validation",
            "digest_check",
            "diff",
            "preparation",
            "reconciliation",
            "commit",
            "retirement",
        ],
        "terminal_stages": _enum_values(ReloadTerminalStage),
        "result_categories": _enum_values(ReloadResultCategory),
        "classification_probes": category_checks,
        "cases": cases,
        "diff_observations": {
            "identical": _diff_projection(config, config),
            "live_only": _diff_projection(
                config, _config_with_changes(config, server=server_live)
            ),
            "restart_only": _diff_projection(
                config, _config_with_changes(config, server=server_restart)
            ),
            "mixed": _diff_projection(
                config, _config_with_changes(config, server=mixed_server)
            ),
            "secret": _diff_projection(
                config, _config_with_changes(config, pricing=pricing_config)
            ),
        },
        "generation_id_rule": ("monotonic; no-op and rejected reloads do not publish"),
        "retirement_pending_rule": (
            "accepted publication may be pending while old leases or terminal "
            "references drain"
        ),
    }


def _config_policy() -> dict[str, object]:
    field_dispositions = [
        {"path": path, "disposition": disposition.value}
        for path, disposition in sorted(_FIELD_DISPOSITION.items())
    ]
    dynamic_rules = [
        {
            "pattern": pattern,
            "disposition": _disposition_for(sample).value,
        }
        for pattern, sample in (
            ("providers.<provider_id>", "providers.fixture"),
            ("accounts.<provider_id>/<account_name>", "accounts.fixture/account"),
            ("model_overrides.<model_id>", "model_overrides.fixture-model"),
            ("model_capabilities.<model_id>", "model_capabilities.fixture-model"),
            ("transcoder.<field>", "transcoder.enabled"),
            ("cache.<field>", "cache.enabled"),
            ("models.<field>", "models.expose_mode"),
        )
    ]
    return {
        "disposition_values": ["live", "restart_required", "ignored"],
        "default_unknown_disposition": "restart_required",
        "unknown_path_probe": _disposition_for("unknown.future.field").value,
        "field_dispositions": field_dispositions,
        "dynamic_rules": dynamic_rules,
        "ignored_paths": [],
        "top_level_config_sections": sorted(AppConfig.model_fields),
        "runtime_tracked_default_paths": sorted(
            path for path, _value in _iter_tracked_fields(AppConfig())
        ),
        "secret_field_names": sorted(_SECRET_FIELD_NAMES),
        "secret_display_token": "<changed>",
        "free_text_redaction_token": "<redacted>",
        "free_text_redaction_probe": sanitize_text_for_audit(
            "Authorization: Bearer " + "X" * 20
        ),
        "dynamic_values_are_collapsed": True,
        "change_order": "lexicographic path order",
    }


def _task_contract(config: AppConfig) -> dict[str, object]:
    variants = {
        "default": _resolved_tasks(config),
        "catalog_refresh_disabled": _resolved_tasks(
            config.model_copy(
                update={
                    "models": config.models.model_copy(update={"refresh_interval_s": 0})
                }
            )
        ),
        "metrics_immediate": _resolved_tasks(
            config.model_copy(
                update={
                    "metrics": config.metrics.model_copy(
                        update={"write_mode": "immediate"}
                    )
                }
            )
        ),
        "update_checker_enabled": _resolved_tasks(
            config.model_copy(
                update={
                    "update_checker": config.update_checker.model_copy(
                        update={"enabled": True}
                    )
                }
            )
        ),
        "automatic_backup_enabled": _resolved_tasks(
            config.model_copy(
                update={
                    "backup": config.backup.model_copy(
                        update={
                            "enabled": True,
                            "interval_s": 600,
                            "startup_delay_s": 5,
                        }
                    )
                }
            )
        ),
    }
    return {
        "inventory": [_task_projection(spec) for spec in RUNTIME_TASK_INVENTORY],
        "names_unique": len({spec.name for spec in RUNTIME_TASK_INVENTORY})
        == len(RUNTIME_TASK_INVENTORY),
        "config_variants": variants,
        "conditions": {
            "catalog_refresh": "models.refresh_interval_s <= 0 disables",
            "metrics_flush": "metrics.write_mode == immediate disables",
            "update_checker": (
                "startup-only process task; update_checker.enabled and outbound "
                "handle required"
            ),
            "automatic_backup": "backup.enabled and interval_s > 0 enables",
            "retention_cleanup": (
                "always registered; reads active generation config per tick"
            ),
            "generation_leased_callbacks": "acquire active generation for every tick",
        },
        "singleton_process_tasks": [
            "checkpoint",
            "metrics_flush",
            "update_checker",
            "automatic_backup",
        ],
    }


def _authority_contract() -> dict[str, object]:
    return {
        "generation_dataclass_fields": list(RuntimeGeneration.__dataclass_fields__),
        "process_runtime_fields": list(ProcessRuntime.__dataclass_fields__),
        "generation_owned_app_state_mirrors": sorted(_RUNTIME_OWNED_APP_STATE_ATTRS),
        "live_request_authority": [
            "providers",
            "accounts",
            "model_routers",
            "model_overrides",
            "model_capabilities",
            "server.max_request_body_bytes",
            "models.expose_mode",
            "models.collapse_models",
            "models.stale_after_s",
            "models.allow_stale_catalog",
            "routing.* request/retry/health/fairness settings",
            "routing.wire_negotiation.*",
            "transcoder.*",
            "security.persist_redacted_error_detail",
            "task intervals and retention settings consumed per tick",
        ],
        "restart_required_constructor_authority": [
            "server.host/port/api_key/api_key_env/log_level/access_log/threads",
            "upstream transport settings",
            "database constructor and path settings",
            "dashboard route topology and public/auth mode",
            "security middleware settings",
            "readiness probe constructor settings",
            "listener and route-topology state",
        ],
        "request_generation_rule": (
            "one generation from admission through finite terminal registration "
            "or stream disconnect"
        ),
        "app_state_rule": (
            "mirrors are compatibility snapshots, never authority for leased "
            "request work"
        ),
        "active_state_after_shutdown": "no active generation is acquirable",
    }


def build_observation_bundle() -> dict[str, object]:
    """Return the complete deterministic R001 observation bundle."""
    config = AppConfig()
    return {
        "schema_version": SCHEMA_VERSION,
        "oracle_modules": [
            "eggpool.runtime_manager",
            "eggpool.generation_factory",
            "eggpool.config_reload_policy",
            "eggpool.reload_transaction",
            "eggpool.runtime_task_inventory",
            "eggpool.runtime_tasks",
            "eggpool.reload_diagnostics",
            "eggpool.app",
            "eggpool.cli_rehash_helper",
        ],
        "ownership_inventory": _ownership_inventory(),
        "lifecycle": _lifecycle_contract(),
        "reload": _reload_contract(config),
        "config_policy": _config_policy(),
        "tasks": _task_contract(config),
        "active_authority": _authority_contract(),
        "normalization": {
            "normalized": ["timestamps", "runtime identifiers"],
            "preserved": [
                "state names and order",
                "booleans",
                "changed/restart path ordering",
                "task names and specs",
                "generation increments",
                "retirement pending state",
                "error category distinctions",
            ],
            "excluded": [
                "API keys",
                "proxy credentials",
                "raw config values",
                "request/provider bodies",
            ],
        },
    }


def observation_json() -> str:
    """Return stable compact JSON used by the committed fixture."""
    return json.dumps(build_observation_bundle(), sort_keys=True, separators=(",", ":"))


def write_fixture() -> None:
    """Write the committed fixture from the production-backed oracle."""
    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE_PATH.write_text(observation_json() + "\n", encoding="utf-8")


if __name__ == "__main__":
    write_fixture()
