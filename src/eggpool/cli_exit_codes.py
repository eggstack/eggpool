"""Stable exit codes for ``eggpool rehash``.

The closure-pass plan (§6.2) defines stable exit codes that scripts
and deployment tooling can rely on.  Documenting them in one place
keeps the CLI and any future tooling aligned.

Mapping:

- ``0`` -- applied, no-op, or ignored-only success
- ``1`` -- validation / configuration failure
- ``2`` -- restart required (mixed live + restart-required change)
- ``3`` -- control-plane unavailable (socket not reachable)
- ``4`` -- reload conflict / busy (another reload in progress)
- ``5`` -- candidate preparation or publication failure
- ``6`` -- digest mismatch between CLI preflight and server read
"""

from __future__ import annotations

from typing import Final

EXIT_OK: Final[int] = 0
EXIT_VALIDATION: Final[int] = 1
EXIT_RESTART_REQUIRED: Final[int] = 2
EXIT_CONTROL_UNAVAILABLE: Final[int] = 3
EXIT_RELOAD_BUSY: Final[int] = 4
EXIT_PREPARATION_FAILED: Final[int] = 5
EXIT_DIGEST_MISMATCH: Final[int] = 6

# Stage names emitted by ReloadManager / ControlResponse that map to
# each exit code.  ``restart_required`` is a sentinel not a stage;
# any non-ok response whose ``restart_required`` list is non-empty
# is treated as exit code 2.
_STAGE_TO_EXIT: Final[dict[str, int]] = {
    "validation": EXIT_VALIDATION,
    "diff": EXIT_VALIDATION,
    "preparation": EXIT_PREPARATION_FAILED,
    "reconciliation": EXIT_PREPARATION_FAILED,
    "commit": EXIT_PREPARATION_FAILED,
    "activation": EXIT_PREPARATION_FAILED,
    "retirement": EXIT_OK,
    "idle": EXIT_OK,
}

# Sentinel messages emitted when the digest preflight disagrees with
# the server-side read of the same file (a TOCTOU race).
_DIGEST_MISMATCH_SENTINELS: Final[frozenset[str]] = frozenset(
    {
        "digest_mismatch",
        "digest mismatch",
    }
)


def exit_code_for_failure(
    *,
    stage: str,
    restart_required: tuple[str, ...] | list[str],
    message: str,
) -> int:
    """Return the exit code for a non-ok :class:`ControlResponse`.

    The function inspects the failure stage, the ``restart_required``
    list, and the message text in priority order:

    1. If any field requires restart, return
       :data:`EXIT_RESTART_REQUIRED` (operator must restart).
    2. If the message indicates a digest mismatch, return
       :data:`EXIT_DIGEST_MISMATCH`.
    3. Otherwise look up ``stage`` in the stage-to-exit-code table.

    A successful reload is exit code ``0`` and handled by the caller
    before this helper is consulted.
    """
    if restart_required:
        return EXIT_RESTART_REQUIRED
    if any(sentinel in message.lower() for sentinel in _DIGEST_MISMATCH_SENTINELS):
        return EXIT_DIGEST_MISMATCH
    return _STAGE_TO_EXIT.get(stage, EXIT_VALIDATION)


__all__ = [
    "EXIT_OK",
    "EXIT_VALIDATION",
    "EXIT_RESTART_REQUIRED",
    "EXIT_CONTROL_UNAVAILABLE",
    "EXIT_RELOAD_BUSY",
    "EXIT_PREPARATION_FAILED",
    "EXIT_DIGEST_MISMATCH",
    "exit_code_for_failure",
]
