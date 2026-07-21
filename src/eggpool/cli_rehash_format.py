"""Helpers for consistent ``eggpool rehash`` JSON and human output.

The JSON contract guarantees every outcome contains:

- ``ok`` (bool)
- ``stage`` (str)
- ``exit_code`` (int)
- ``generation`` (int | None)
- ``changed_sections`` (list[str])
- ``warnings`` (list[str])
- ``restart_required`` (list[str])
- ``retirement_pending`` (bool)
- ``message`` (str)

Secret-bearing ``restart_required`` display strings are redacted before
emission so ``--json`` output is safe for CI logs and dashboards.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from eggpool.control.server import ControlResponse

# Secret-shaped substrings that must never appear in rendered output.
_SECRET_PATTERNS: tuple[str, ...] = ("api_key", "secret", "token", "password")

# Human-readable labels keyed by stage name.
_STAGE_LABELS: dict[str, str] = {
    "validation": "validation-failure",
    "diff": "restart-required",
    "preparation": "preparation-failure",
    "reconciliation": "preparation-failure",
    "commit": "no-op",
    "activation": "applied",
    "retirement": "applied",
    "idle": "applied",
    "reload_in_progress": "busy",
    "timeout": "control-unavailable",
    "error": "control-unavailable",
    "parse": "control-unavailable",
}

# Phase 11: human-readable labels for result categories.
_RESULT_CATEGORY_LABELS: dict[str, str] = {
    "success_committed": "applied",
    "success_noop": "no-op",
    "success_ignored_only": "ignored-only",
    "rejected_busy": "busy",
    "rejected_validation": "validation-failure",
    "rejected_restart_required": "restart-required",
    "failed_candidate_prepare": "preparation-failure",
    "failed_persistence_prepare": "persistence-failure",
    "failed_process_transition_prepare": "process-transition-failure",
    "failed_commit": "commit-failure",
    "failed_publication": "publication-failure",
    "failed_process_transition_apply": "process-transition-failure",
    "failed_persistence_commit": "persistence-failure",
    "aborted_cancelled": "cancelled",
    "aborted_shutdown": "shutdown",
    "compensation_failed": "compensation-failure",
    "internal_error": "internal-error",
}


def format_rehash_json(result: ControlResponse, exit_code: int) -> dict[str, Any]:
    """Build the canonical JSON dict for a ``ControlResponse``.

    Always includes every required key so ``--json`` consumers never
    see missing fields.
    """
    d: dict[str, Any] = {
        "ok": result.ok,
        "stage": result.stage,
        "exit_code": exit_code,
        "generation": result.generation,
        "changed_sections": list(result.changed_sections),
        "warnings": list(result.warnings),
        "restart_required": list(result.restart_required),
        "retirement_pending": result.retirement_pending,
        "message": result.message,
    }
    # Phase 11: include optional diagnostic fields.
    if result.result_category is not None:
        d["result_category"] = result.result_category
    if result.duration_s is not None:
        d["duration_s"] = result.duration_s
    return d


def _redact_message(message: str) -> str:
    """Return *message* with secret-bearing display strings replaced.

    Handles patterns like:
    - ``api_key: <old> -> <new>``
    - ``api_key: sk-xxx → sk-yyy``
    - Field display strings containing ``<old>`` / ``<new>`` tokens
    - Raw credential-shaped values (Bearer, sk-, key-, etc.) that may
      have leaked into an error message — the broader sanitizer lives in
      :func:`eggpool.config_reload_policy.sanitize_text_for_audit` and
      runs unconditionally here so CLI output never leaks secrets.
    """
    from eggpool.config_reload_policy import sanitize_text_for_audit

    sanitized = sanitize_text_for_audit(message) or message
    import re

    lower = sanitized.lower()
    if not any(p in lower for p in _SECRET_PATTERNS):
        return sanitized
    redacted = sanitized.replace("<old>", "<redacted>").replace("<new>", "<redacted>")
    redacted = re.sub(
        r"<redacted>\s*(?:->|→)\s*<redacted>",
        "<redacted>",
        redacted,
    )
    return redacted


def render_rehash_human(result: ControlResponse) -> tuple[str, str]:
    """Return ``(stdout_text, stderr_text)`` for human-readable output.

    Success messages go to stdout; failures, warnings, and diagnostic
    hints go to stderr.  Secret-bearing content is redacted.
    """
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []

    if result.ok:
        stdout_parts.append(f"\n{result.message}")
        if result.changed_sections:
            stdout_parts.append(
                f"  Changed sections: {', '.join(result.changed_sections)}"
            )
        if result.generation is not None:
            stdout_parts.append(f"  Generation: {result.generation}")
        if result.retirement_pending:
            stdout_parts.append(
                "  Old generation is draining; active requests will complete "
                "on their original configuration."
            )
        for warning in result.warnings:
            stdout_parts.append(f"  warning: {warning}")
    else:
        redacted_msg = _redact_message(result.message)
        stderr_parts.append(f"\n{redacted_msg}")
        if result.restart_required:
            stderr_parts.append("  Restart-required changes:")
            for field in result.restart_required:
                stderr_parts.append(f"    - {_redact_message(field)}")
        for warning in result.warnings:
            stderr_parts.append(f"  warning: {warning}")

    return "\n".join(stdout_parts), "\n".join(stderr_parts)


def human_busy_message() -> str:
    """Return the standard busy-stage human message."""
    return "A reload transaction is already in progress. Wait and retry."


def human_restart_required_message(restart_required: list[str]) -> str:
    """Return the standard restart-required human message."""
    fields = ", ".join(restart_required)
    return f"Restart required for: {fields}. Run `eggpool restart` to apply."


def human_validation_failure_message(exc: object) -> str:
    """Return the standard validation-failure human message."""
    return f"configuration validation failed: {exc}"


def human_control_unavailable_message() -> str:
    """Return the standard control-unavailable human message."""
    return "Control socket unavailable. Is the server running?"


def human_digest_mismatch_message() -> str:
    """Return the standard digest-mismatch human message."""
    return "Content digest mismatch: the server read a different config version."


def human_preparation_failure_message(message: str) -> str:
    """Return the standard preparation-failure human message."""
    return f"Reload preparation failed: {message}"


__all__ = [
    "format_rehash_json",
    "human_busy_message",
    "human_control_unavailable_message",
    "human_digest_mismatch_message",
    "human_preparation_failure_message",
    "human_restart_required_message",
    "human_validation_failure_message",
    "render_rehash_human",
]
