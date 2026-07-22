"""Canonical reload diagnostics: result categories, counters, and finalization.

Defines the typed result model used by the reload manager internal state,
control protocol response, CLI formatting, runtime diagnostic endpoint,
operational event persistence, and tests.

Every admitted reload reaches one terminal finalizer that produces a
:class:`ReloadDiagnosticResult`.  The result carries structured counters,
retirement status derived from the runtime manager, and bounded warnings
so operators, CLI clients, and dashboard consumers agree on what changed,
which generation is active, and where a failure occurred.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eggpool.config_reload_policy import ConfigValidationWarning


class ReloadResultCategory(enum.Enum):
    """Terminal outcome category for an admitted reload operation.

    Categories distinguish success sub-types (committed, no-op,
    ignored-only) from failure modes so operators and CLI consumers
    can branch on a stable enum rather than free-form strings.
    """

    SUCCESS_COMMITTED = "success_committed"
    SUCCESS_NOOP = "success_noop"
    SUCCESS_IGNORED_ONLY = "success_ignored_only"
    REJECTED_BUSY = "rejected_busy"
    REJECTED_VALIDATION = "rejected_validation"
    REJECTED_RESTART_REQUIRED = "rejected_restart_required"
    FAILED_CANDIDATE_PREPARE = "failed_candidate_prepare"
    FAILED_PERSISTENCE_PREPARE = "failed_persistence_prepare"
    FAILED_PROCESS_TRANSITION_PREPARE = "failed_process_transition_prepare"
    FAILED_COMMIT = "failed_commit"
    FAILED_PUBLICATION = "failed_publication"
    FAILED_PROCESS_TRANSITION_APPLY = "failed_process_transition_apply"
    FAILED_PERSISTENCE_COMMIT = "failed_persistence_commit"
    ABORTED_CANCELLED = "aborted_cancelled"
    ABORTED_SHUTDOWN = "aborted_shutdown"
    COMPENSATION_FAILED = "compensation_failed"
    INTERNAL_ERROR = "internal_error"


class ReloadTerminalStage(enum.Enum):
    """Terminal transaction stage for diagnostics.

    Maps from the Phase 6 :class:`TransactionState` to a stable
    terminal stage that the control protocol and CLI can display.
    """

    VALIDATION = "validation"
    DIFF = "diff"
    PREPARATION = "preparation"
    RECONCILIATION = "reconciliation"
    COMMIT = "commit"
    RETIREMENT = "retirement"
    IDLE = "idle"


@dataclass(frozen=True)
class ReloadCounters:
    """Precise counter semantics for reload operations.

    Do not overload one ``reload_count`` with ambiguous meaning.
    Each counter has a single, documented purpose.
    """

    total_requests: int = 0
    admitted_operations: int = 0
    busy_rejections: int = 0
    committed_reloads: int = 0
    noop_outcomes: int = 0
    ignored_only_outcomes: int = 0
    validation_rejections: int = 0
    restart_required_rejections: int = 0
    prepare_failures: int = 0
    commit_failures: int = 0
    cancellations: int = 0
    compensation_failures: int = 0
    retirement_failures: int = 0


@dataclass(frozen=True)
class ReloadRetirementStatus:
    """Retirement fields derived from the runtime manager after commit.

    Reflects actual tracked retirement tasks rather than inferring
    pending status from result success.
    """

    retirement_pending: bool
    retiring_generation_id: int | None = None
    retirement_failed: bool = False


@dataclass(frozen=True)
class ReloadDiagnosticResult:
    """Canonical result of a complete reload transaction.

    Used by:
    - reload manager internal state (``_last_reload_result``);
    - control protocol response;
    - CLI formatting;
    - runtime diagnostic endpoint;
    - operational event persistence;
    - tests.

    Fields are frozen and secret-free.  Warning count and message
    length are bounded by construction.
    """

    request_id: str
    category: ReloadResultCategory
    terminal_stage: ReloadTerminalStage
    admitted_at: float | None = None
    started_at: float | None = None
    completed_at: float | None = None
    duration_s: float = 0.0
    old_generation_id: int | None = None
    old_generation_digest: str | None = None
    candidate_generation_id: int | None = None
    candidate_generation_digest: str | None = None
    active_generation_id: int | None = None
    active_generation_digest: str | None = None
    changed_sections: tuple[str, ...] = ()
    ignored_sections: tuple[str, ...] = ()
    restart_required_sections: tuple[str, ...] = ()
    semantic_noop: bool = False
    publication_occurred: bool = False
    persistence_committed: bool = False
    process_transitions_applied: bool = False
    compensation_attempted: bool = False
    compensation_succeeded: bool = False
    candidate_cleanup_attempted: bool = False
    candidate_cleanup_succeeded: bool = False
    retirement: ReloadRetirementStatus = field(
        default_factory=lambda: ReloadRetirementStatus(retirement_pending=False)
    )
    error_code: str | None = None
    error_class: str | None = None
    message: str = ""
    warnings: tuple[ConfigValidationWarning, ...] = ()
    warning_messages: tuple[str, ...] = ()
    counters: ReloadCounters = field(default_factory=ReloadCounters)
    operational_event_recorded: bool = False


def classify_result_category(
    *,
    ok: bool,
    stage: ReloadTerminalStage,
    is_noop: bool = False,
    is_ignored_only: bool = False,
    is_restart_required: bool = False,
    is_cancelled: bool = False,
    is_shutdown: bool = False,
    is_compensation_failed: bool = False,
    is_publication_failed: bool = False,
    is_process_transition_prepare_failed: bool = False,
    is_process_transition_apply_failed: bool = False,
    is_persistence_commit_failed: bool = False,
    error_class: str | None = None,
) -> ReloadResultCategory:
    """Derive the result category from outcome flags.

    This is the single source of truth for category classification.
    Callers set boolean flags; this function maps them to a stable
    enum value.
    """
    if not ok:
        if is_cancelled:
            return ReloadResultCategory.ABORTED_CANCELLED
        if is_shutdown:
            return ReloadResultCategory.ABORTED_SHUTDOWN
        if is_compensation_failed:
            return ReloadResultCategory.COMPENSATION_FAILED
        if is_restart_required:
            return ReloadResultCategory.REJECTED_RESTART_REQUIRED
        # Granular failure categories take priority over stage-based
        # fallback when the caller can identify the specific barrier.
        if is_publication_failed:
            return ReloadResultCategory.FAILED_PUBLICATION
        if is_process_transition_prepare_failed:
            return ReloadResultCategory.FAILED_PROCESS_TRANSITION_PREPARE
        if is_process_transition_apply_failed:
            return ReloadResultCategory.FAILED_PROCESS_TRANSITION_APPLY
        if is_persistence_commit_failed:
            return ReloadResultCategory.FAILED_PERSISTENCE_COMMIT
        # Prioritize stage over error class: the stage reflects where
        # the failure occurred (e.g. ReloadPreparationError at VALIDATION
        # stage is a validation failure, not a preparation failure).
        if stage == ReloadTerminalStage.VALIDATION:
            return ReloadResultCategory.REJECTED_VALIDATION
        if stage == ReloadTerminalStage.DIFF:
            return ReloadResultCategory.REJECTED_RESTART_REQUIRED
        if stage == ReloadTerminalStage.PREPARATION:
            return ReloadResultCategory.FAILED_CANDIDATE_PREPARE
        if stage == ReloadTerminalStage.RECONCILIATION:
            return ReloadResultCategory.FAILED_PERSISTENCE_PREPARE
        if stage in (ReloadTerminalStage.COMMIT, ReloadTerminalStage.RETIREMENT):
            return ReloadResultCategory.FAILED_COMMIT
        # Fall back to error class mapping for unrecognised stages.
        if error_class == "ReloadPreparationError":
            return ReloadResultCategory.FAILED_CANDIDATE_PREPARE
        if error_class == "ReloadReconciliationError":
            return ReloadResultCategory.FAILED_PERSISTENCE_PREPARE
        if error_class == "ReloadCommitError":
            return ReloadResultCategory.FAILED_COMMIT
        return ReloadResultCategory.INTERNAL_ERROR

    if is_noop:
        return ReloadResultCategory.SUCCESS_NOOP
    if is_ignored_only:
        return ReloadResultCategory.SUCCESS_IGNORED_ONLY
    return ReloadResultCategory.SUCCESS_COMMITTED


def stage_from_error_class(error_class: str | None) -> ReloadTerminalStage:
    """Map an error class name to the correct terminal stage.

    This replaces the incorrect mapping that previously sent all
    preparation/commit failures to VALIDATION.
    """
    if error_class == "ReloadPreparationError":
        return ReloadTerminalStage.PREPARATION
    if error_class == "ReloadReconciliationError":
        return ReloadTerminalStage.RECONCILIATION
    if error_class == "ReloadCommitError":
        return ReloadTerminalStage.COMMIT
    return ReloadTerminalStage.VALIDATION


__all__ = [
    "ReloadCounters",
    "ReloadDiagnosticResult",
    "ReloadResultCategory",
    "ReloadRetirementStatus",
    "ReloadTerminalStage",
    "classify_result_category",
    "stage_from_error_class",
]
