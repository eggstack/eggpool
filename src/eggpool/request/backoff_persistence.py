"""Backoff persistence helpers extracted from RequestCoordinator."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eggpool.db.connection import Database
    from eggpool.db.repositories import AccountBackoffRepository

logger = logging.getLogger(__name__)


async def persist_backoff(
    *,
    account_backoff_repo: AccountBackoffRepository | None,
    account_id_cache: dict[str, int],
    db: Database,
    account_name: str,
    model_id: str | None,
    reason: str,
    status_code: int | None,
    error_class: str | None,
    backoff_until: float | None,
    consecutive_failures: int,
) -> None:
    """Write the authoritative backoff to ``account_backoff_repo``.

    Silently skips when no repository was injected (e.g. legacy
    tests) or when the reason has no policy (e.g. client 4xx).
    """
    if account_backoff_repo is None:
        return
    from eggpool.health.backoff import is_backoff_reason

    if not is_backoff_reason(reason):
        return
    account_id = account_id_cache.get(account_name)
    if account_id is None:
        try:
            from eggpool.db.repositories import AccountRepository

            account_repo = AccountRepository(db)
            account_id = await account_repo.get_id_by_name(account_name)
        except Exception:
            logger.exception(
                "Failed to resolve account_id for backoff persistence (account=%r)",
                account_name,
            )
            return
        if account_id is None:
            return
        account_id_cache[account_name] = account_id
    try:
        await account_backoff_repo.upsert_failure(
            account_id=account_id,
            model_id=model_id,
            reason=reason,
            status_code=status_code,
            error_class=error_class,
            backoff_until=backoff_until,
            consecutive_failures=consecutive_failures,
        )
    except Exception:
        logger.exception(
            "Failed to persist backoff (account=%r reason=%r)",
            account_name,
            reason,
        )


async def clear_backoff(
    *,
    account_backoff_repo: AccountBackoffRepository | None,
    account_id_cache: dict[str, int],
    db: Database,
    account_name: str,
    model_id: str | None = None,
    reasons: list[str] | None = None,
) -> None:
    """Remove persisted backoff rows for a successful request.

    Errors are logged and swallowed so the request lifecycle
    continues; the in-memory health manager is the source of
    truth for the current process and the repository is purely
    durable state.
    """
    if account_backoff_repo is None:
        return
    account_id = account_id_cache.get(account_name)
    if account_id is None:
        try:
            from eggpool.db.repositories import AccountRepository

            account_repo = AccountRepository(db)
            account_id = await account_repo.get_id_by_name(account_name)
        except Exception:
            logger.exception(
                "Failed to resolve account_id for backoff cleanup (account=%r)",
                account_name,
            )
            return
        if account_id is None:
            return
        account_id_cache[account_name] = account_id
    try:
        await account_backoff_repo.clear_success(
            account_id=account_id,
            model_id=model_id,
            reasons=reasons,
        )
    except Exception:
        logger.exception(
            "Failed to clear backoff rows (account=%r)",
            account_name,
        )
