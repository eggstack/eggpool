"""Shared validate-and-rehash helper for CLI commands that mutate config.

The closure-pass plan (§7.4) calls for one CLI helper used by every
config-mutating command (``connect``, ``logout``, future ``config
set``, etc.) so the validate-and-apply path cannot drift.  Commands
that only write to the config file can call :func:`validate_and_rehash`
to validate locally, connect to the running server's control socket,
and apply the change live when supported.

Behavior:

- local validation runs first and fails closed (no restart, no reload);
- if the server is reachable, the helper sends a ``reload_config``
  command through the control socket;
- if the server is not reachable, the helper logs that the change
  will apply on the next start and returns ``False`` (caller decides
  whether to fall back to a hard restart);
- mixed live + restart-required changes are rejected by the server
  before any state mutates;
- the CLI exit-code constants from :mod:`eggpool.cli_exit_codes` are
  used for failure reporting.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from eggpool.cli_exit_codes import (
    EXIT_CONTROL_UNAVAILABLE,
    EXIT_RESTART_REQUIRED,
    EXIT_VALIDATION,
)
from eggpool.config_validation import (
    ConfigValidationError,
    ConfigValidationResult,
    validate_config_file,
)

if TYPE_CHECKING:
    from eggpool.control.server import ControlResponse

logger = logging.getLogger(__name__)


def validate_config_or_exit(
    config_path: str,
    *,
    echo_failure: object | None = None,
) -> ConfigValidationResult:
    """Run the local validation contract and exit non-zero on failure.

    Used as the first stage of :func:`validate_and_rehash` and any
    other command that wants the same fail-closed local validation.
    ``echo_failure`` is an optional callable (typically
    :func:`click.echo`) used to print the failure message; if ``None``
    the failure is logged via the module logger.
    """
    try:
        return validate_config_file(config_path)
    except ConfigValidationError as exc:
        message = f"Error: configuration validation failed: {exc}"
        if callable(echo_failure):
            echo_failure(message)
        else:
            logger.error(message)
        raise SystemExit(EXIT_VALIDATION) from None


def try_live_rehash(
    config_path: str,
    *,
    echo: object | None = None,
    echo_err: object | None = None,
) -> tuple[bool, str]:
    """Connect to the running server's control socket and request reload.

    Returns ``(True, message)`` when the reload was applied (or
    no-op), ``(False, message)`` when the server is unreachable or
    refused the reload.  The caller decides whether to fall back to
    a hard restart on ``False``.

    Uses :func:`click.echo` (or any provided callable) for output so
    CLI commands keep their usual formatting.
    """

    async def _send(content_digest: str) -> ControlResponse:
        from eggpool.control.client import (  # noqa: PLC0415
            ControlClient,
        )

        client = ControlClient()
        return await client.reload(content_digest)

    validation = validate_config_or_exit(config_path, echo_failure=echo_err)
    try:
        result: ControlResponse = asyncio.run(_send(validation.content_digest))
    except Exception as exc:  # noqa: BLE001 - all control failures are non-fatal
        message = (
            f"Control socket unavailable ({exc!r}). "
            "The change will apply on next start, or run "
            "`eggpool restart` to apply it now."
        )
        if callable(echo_err):
            echo_err(message)
        else:
            logger.info(message)
        return False, message

    if result.ok:
        sections = ", ".join(result.changed_sections)
        message = (
            f"Live reload applied: generation={result.generation}, sections={sections}"
        )
        if result.retirement_pending:
            message += (
                "; old generation is draining — active requests will complete "
                "on their original configuration"
            )
        if callable(echo):
            echo(message)
        return True, message

    if result.restart_required:
        message = (
            "Restart required for: "
            + ", ".join(result.restart_required)
            + ". Run `eggpool restart` to apply."
        )
        if callable(echo_err):
            echo_err(message)
        else:
            logger.info(message)
        return False, message

    if callable(echo_err):
        echo_err(result.message)
    return False, result.message


def validate_and_rehash(
    config_path: str,
    *,
    echo: object | None = None,
    echo_err: object | None = None,
) -> None:
    """Validate locally, then attempt a live rehash.

    A convenience wrapper that performs the canonical validate-and-reload
    flow used by :func:`eggpool.cli_full.rehash`.  Returns ``None`` on
    success and raises ``SystemExit`` with the appropriate code on
    failure so callers (e.g. ``connect``, ``logout``) can rely on
    standard CLI exit semantics.

    For commands that need to differentiate "server not running" from
    "server refused reload", use :func:`try_live_rehash` directly.
    """
    validation = validate_config_or_exit(config_path, echo_failure=echo_err)

    async def _send() -> ControlResponse:
        from eggpool.control.client import (  # noqa: PLC0415
            ControlClient,
        )

        client = ControlClient()
        return await client.reload(validation.content_digest)

    try:
        result: ControlResponse = asyncio.run(_send())
    except Exception as exc:  # noqa: BLE001
        message = (
            f"Control socket unavailable ({exc!r}). "
            "Use `eggpool restart` to apply the configuration change."
        )
        if callable(echo_err):
            echo_err(message)
        else:
            logger.error(message)
        # Match `eggpool rehash`: an unreachable control socket is a
        # distinct condition (server down), not a validation failure.
        raise SystemExit(EXIT_CONTROL_UNAVAILABLE) from None

    if result.ok:
        message = f"Live reload applied (generation={result.generation})."
        if callable(echo):
            echo(message)
        return

    if result.restart_required:
        message = "Restart required for: " + ", ".join(result.restart_required)
        if callable(echo_err):
            echo_err(message)
        else:
            logger.error(message)
        raise SystemExit(EXIT_RESTART_REQUIRED) from None

    if callable(echo_err):
        echo_err(result.message)
    raise SystemExit(EXIT_VALIDATION)


__all__ = [
    "validate_and_rehash",
    "validate_config_or_exit",
    "try_live_rehash",
]
