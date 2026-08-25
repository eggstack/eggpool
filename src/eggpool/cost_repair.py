"""Repair suspicious historical request costs with current guardrails."""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from eggpool.catalog.pricing import (
    CostCalculator,
    PriceRepository,
    choose_bounded_estimated_cost,
    cost_per_token_is_implausible,
    total_billable_tokens,
)
from eggpool.constants import MAX_REQUEST_COST_MICRODOLLARS

if TYPE_CHECKING:
    from eggpool.db.connection import Database

logger = logging.getLogger(__name__)

_TRUSTED_LOCAL_EXACTNESS = frozenset({"derived", "partial", "exact"})
_REQUEST_CAP_SUSPICION_THRESHOLD = MAX_REQUEST_COST_MICRODOLLARS * 9 // 10

# Material ratio threshold above which the repair tool flags a row
# whose canonical ``cost_microdollars`` matches its
# ``reserved_microdollars`` while a lower ``local_cost_microdollars``
# exists. Mirrors the alarming-ratios surfaced in the runtime log
# events ``cost.reservation_fallback_suppressed`` so operators have
# one consistent definition of "obviously wrong".
_RESERVATION_OVER_LOCAL_SUSPICION_RATIO = 4


@dataclass(frozen=True)
class RepairSummary:
    """Aggregate counts returned by :func:`repair_request_costs`."""

    scanned: int
    suspicious: int
    repaired: int
    skipped_provider_reported: int
    unchanged: int
    old_total_microdollars: int
    proposed_total_microdollars: int
    changed_rows: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    breakdown_rows: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])


def _repair_reason(row: dict[str, Any]) -> str | None:
    provider_cost = row.get("provider_cost_microdollars")
    if provider_cost is not None:
        return None

    old_cost = int(row.get("cost_microdollars") or 0)
    total_tokens = total_billable_tokens(
        int(row.get("input_tokens") or 0),
        int(row.get("output_tokens") or 0),
        int(row.get("cache_read_tokens") or 0),
        int(row.get("cache_write_tokens") or 0),
    )
    if old_cost >= _REQUEST_CAP_SUSPICION_THRESHOLD:
        return "near_request_cap"

    reserved = int(row.get("reserved_microdollars") or 0)
    exactness = str(row.get("exactness") or "unknown")

    # New suspicion class for the reservation-fallback
    # canonicalization regression: the canonical ``cost_microdollars``
    # is exactly the reservation value, ``local_cost_microdollars``
    # (persisted by the finalizer) carries a non-zero but plausible
    # local estimate, and the reservation is materially larger than
    # the local estimate. Skipping rows where the local estimate is
    # zero, missing, or higher than the canonical keeps the repair
    # strictly destructive in the failure direction.
    local_cost = row.get("local_cost_microdollars")
    if (
        local_cost is not None
        and int(local_cost) > 0
        and reserved > 0
        and old_cost == reserved
        and int(local_cost) < old_cost
        and old_cost > max(int(local_cost) * _RESERVATION_OVER_LOCAL_SUSPICION_RATIO, 0)
        and exactness == "estimated"
    ):
        return "reservation_fallback_overrode_lower_local_estimate"

    if cost_per_token_is_implausible(old_cost, total_tokens):
        return "implausible_cost_per_token"

    if exactness == "estimated" and reserved > 0 and old_cost > max(reserved * 4, 0):
        return "estimated_far_above_reservation"
    return None


def canonicalize_repaired_cost(
    *,
    local_cost_microdollars: int,
    local_cost_exactness: str,
    reserved_microdollars: int,
    may_have_billable_work: bool,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> tuple[int, str, str]:
    """Pick the canonical repaired value using the same precedence as the finalizer.

    Trusted local exactness (``derived``, ``partial``, ``exact``) wins
    outright. Otherwise both the local estimate and the reservation
    are routed through :func:`choose_bounded_estimated_cost` so the
    repair honors the lower-plausible rule instead of falling back
    to the reservation unconditionally. Provider-reported cost is
    never mapped here — the calling repair loop already skips those
    rows.

    Returns ``(cost_microdollars, exactness, provenance)`` where the
    exactness is always ``"estimated"`` (or ``"unknown"`` for the
    no-billable-work path) regardless of which underlying estimate
    was selected; the ``provenance`` string is informational and
    suitable for log lines / audit rows.
    """
    if local_cost_exactness in _TRUSTED_LOCAL_EXACTNESS:
        return local_cost_microdollars, local_cost_exactness, "trusted_local"
    if not may_have_billable_work:
        return 0, "unknown", "no_billable_work"
    chosen, provenance = choose_bounded_estimated_cost(
        local_estimate_microdollars=local_cost_microdollars,
        reservation_microdollars=reserved_microdollars,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
    )
    if provenance == "unknown":
        return chosen, "unknown", provenance
    return chosen, "estimated", provenance


async def repair_request_costs(
    db: Database,
    *,
    provider_filter: str | None = None,
    since: str | None = None,
    limit: int | None = None,
    dry_run: bool = True,
    batch_size: int = 500,
) -> RepairSummary:
    """Repair suspicious historical request costs.

    Suspicion is generic and provider-neutral:
    near request-cap totals, implausible cost-per-token, estimated
    rows whose canonical cost far exceeds the reservation estimate,
    and the ``reservation_fallback_overrode_lower_local_estimate``
    class of failure where the persisted ``local_cost_microdollars``
    is plausible but the canonical value equals the inflated
    reservation.  Provider-reported cost rows are skipped.
    """
    where_clauses = ["r.status != 'pending'"]
    params: list[Any] = []
    if provider_filter:
        where_clauses.append(
            "(COALESCE(r.provider_id, '') LIKE ? OR COALESCE(a.name, '') LIKE ?)"
        )
        pattern = f"%{provider_filter}%"
        params.extend([pattern, pattern])
    if since:
        where_clauses.append("r.started_at >= ?")
        params.append(since)
    limit_clause = " LIMIT ? " if limit is not None else ""
    if limit is not None:
        params.append(int(limit))

    rows = await db.fetch_all(
        "SELECT r.id, r.model_id, r.provider_id, a.name AS account_name, "
        "r.status, r.input_tokens, r.output_tokens, r.cache_read_tokens, "
        "r.cache_write_tokens, r.cost_microdollars, r.exactness, "
        "r.provider_cost_microdollars, r.reserved_microdollars, "
        "r.local_cost_microdollars, r.local_cost_exactness, "
        "r.upstream_protocol "
        "FROM requests r "
        "LEFT JOIN accounts a ON a.id = r.account_id "
        f"WHERE {' AND '.join(where_clauses)} "
        "ORDER BY r.cost_microdollars DESC, r.started_at DESC, r.id DESC"
        f"{limit_clause}",
        tuple(params),
    )

    calculator = CostCalculator(price_repo=PriceRepository(db))
    suspicious = 0
    repaired = 0
    unchanged = 0
    skipped_provider_reported = 0
    old_total = 0
    proposed_total = 0
    changes: list[dict[str, Any]] = []
    updates: list[tuple[int, int, str, int, str, str]] = []

    for row in rows:
        old_cost = int(row["cost_microdollars"] or 0)
        old_total += old_cost
        # Seed ``proposed_total`` with ``old_cost`` so the final value
        # is the sum of every row's new cost. Deltas are applied only
        # for changed rows; a future reordering of the early
        # ``continue`` branches must preserve this invariant or risk
        # double-counting provider-reported rows.
        proposed_total += old_cost

        if row["provider_cost_microdollars"] is not None:
            skipped_provider_reported += 1
            continue

        reason = _repair_reason(dict(row))
        if reason is None:
            continue
        suspicious += 1

        model_id = str(row["model_id"] or "")
        provider_id = str(row["provider_id"] or "")
        input_tokens = int(row["input_tokens"] or 0)
        output_tokens = int(row["output_tokens"] or 0)
        cache_read_tokens = int(row["cache_read_tokens"] or 0)
        cache_write_tokens = int(row["cache_write_tokens"] or 0)
        reserved = int(row["reserved_microdollars"] or 0)
        # Prefer the persisted `local_cost_microdollars` /
        # `local_cost_exactness` columns so the repair is
        # deterministic — the row already carries the finalizer's
        # result for the same tokens — and only fall back to
        # recomputing through the calculator when those columns are
        # absent (older request rows from before
        # 0033_request_provider_local_cost).
        local_cost_raw = row["local_cost_microdollars"]
        local_exactness_raw = row["local_cost_exactness"]
        if local_cost_raw is not None and local_exactness_raw is not None:
            local_cost = int(local_cost_raw)
            local_exactness = (
                str(local_exactness_raw) if local_exactness_raw else "unknown"
            )
        else:
            local_cost, local_exactness = await calculator.calculate_cost(
                model_id=model_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
                provider_id=provider_id or None,
                # Mirror the finalizer: OpenAI-protocol rows store
                # prompt tokens inclusive of cached tokens.
                input_tokens_include_cache=(
                    str(row["upstream_protocol"] or "") == "openai"
                ),
            )
        total_tokens = (
            input_tokens + output_tokens + cache_read_tokens + cache_write_tokens
        )
        may_have_billable_work = total_tokens > 0 or old_cost > 0 or reserved > 0
        new_cost, new_exactness, provenance = canonicalize_repaired_cost(
            local_cost_microdollars=local_cost,
            local_cost_exactness=local_exactness,
            reserved_microdollars=reserved,
            may_have_billable_work=may_have_billable_work,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
        )
        if provenance != "trusted_local":
            logger.info(
                "cost.repair.provenance "
                "request_id=%s provider=%s model=%s "
                "old_cost_microdollars=%s new_cost_microdollars=%s "
                "local_cost_microdollars=%s reservation_microdollars=%s "
                "local_cost_exactness=%s reason=%s provenance=%s",
                int(row["id"]),
                provider_id,
                model_id,
                old_cost,
                new_cost,
                local_cost,
                reserved,
                local_exactness,
                reason,
                provenance,
            )
        if new_cost == old_cost and new_exactness == str(row["exactness"] or "unknown"):
            unchanged += 1
            continue

        delta = new_cost - old_cost
        proposed_total += delta
        repaired += 1
        change = {
            "request_id": int(row["id"]),
            "model_id": model_id,
            "provider_id": provider_id,
            "account_name": row["account_name"],
            "old_cost_microdollars": old_cost,
            "new_cost_microdollars": new_cost,
            "delta_microdollars": delta,
            "old_exactness": str(row["exactness"] or "unknown"),
            "new_exactness": new_exactness,
            "reason": reason,
        }
        changes.append(change)
        updates.append(
            (
                int(row["id"]),
                new_cost,
                new_exactness,
                old_cost,
                str(row["exactness"] or "unknown"),
                reason,
            )
        )

    changes.sort(key=lambda row: abs(int(row["delta_microdollars"])), reverse=True)

    if updates and not dry_run:
        async with db.transaction():
            for batch_start in range(0, len(updates), batch_size):
                batch = updates[batch_start : batch_start + batch_size]
                await db.execute_many(
                    "UPDATE requests SET cost_microdollars = ?, exactness = ? "
                    "WHERE id = ?",
                    [
                        (cost, exactness, request_id)
                        for request_id, cost, exactness, _, _, _ in batch
                    ],
                )
                await db.execute_many(
                    "INSERT INTO request_cost_repairs "
                    "(request_id, old_cost_microdollars, new_cost_microdollars, "
                    "old_exactness, new_exactness, reason, provider_filter, "
                    "since_date) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            request_id,
                            old_cost,
                            cost,
                            old_exactness,
                            exactness,
                            reason,
                            provider_filter,
                            since,
                        )
                        for (
                            request_id,
                            cost,
                            exactness,
                            old_cost,
                            old_exactness,
                            reason,
                        ) in batch
                    ],
                )

    grouped: dict[tuple[str, str | None, str], dict[str, Any]] = defaultdict(
        lambda: {
            "request_count": 0,
            "old_total_microdollars": 0,
            "new_total_microdollars": 0,
            "delta_microdollars": 0,
        }
    )
    for row in changes:
        key = (
            str(row["provider_id"]),
            cast("str | None", row["account_name"]),
            str(row["model_id"]),
        )
        entry = grouped[key]
        entry["provider_id"] = key[0]
        entry["account_name"] = key[1]
        entry["model_id"] = key[2]
        entry["request_count"] += 1
        entry["old_total_microdollars"] += int(row["old_cost_microdollars"])
        entry["new_total_microdollars"] += int(row["new_cost_microdollars"])
        entry["delta_microdollars"] += int(row["delta_microdollars"])

    breakdown_rows = sorted(
        grouped.values(),
        key=lambda row: abs(int(row["delta_microdollars"])),
        reverse=True,
    )

    return RepairSummary(
        scanned=len(rows),
        suspicious=suspicious,
        repaired=repaired,
        skipped_provider_reported=skipped_provider_reported,
        unchanged=unchanged,
        old_total_microdollars=old_total,
        proposed_total_microdollars=proposed_total,
        changed_rows=changes[:20],
        breakdown_rows=breakdown_rows[:20],
    )
