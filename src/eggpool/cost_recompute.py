"""Recompute historical request costs from current price snapshots.

This module is the implementation behind ``eggpool stats recompute-costs``.
It exists so the CLI command stays thin and so the recompute logic has a
focused unit-test surface. The walk is best-effort: rows missing the
necessary token counts or a matching price snapshot are skipped and
counted in ``skipped_no_snapshot`` / ``skipped_missing_tokens``.

The recompute reuses the same :class:`CostCalculator` that the request
finalizer uses, so the new values reflect exactly what *would* have
been recorded had the original request been finalized against the
current price snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from eggpool.catalog.pricing import CostCalculator, PriceRepository

if TYPE_CHECKING:
    from eggpool.db.connection import Database


@dataclass(frozen=True)
class RecomputeSummary:
    """Aggregate counts returned by :func:`recompute_request_costs`."""

    scanned: int
    updated: int
    skipped_unchanged: int
    skipped_no_snapshot: int
    skipped_missing_tokens: int
    cost_total_microdollars: int
    new_cost_total_microdollars: int
    changed_rows: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])


async def recompute_request_costs(
    db: Database,
    *,
    limit: int | None = None,
    dry_run: bool = True,
    batch_size: int = 500,
) -> RecomputeSummary:
    """Walk historical requests and recompute cost_microdollars.

    Each row's tokens are joined against the latest price snapshot for
    its ``(model_id, provider_id)`` pair via :class:`CostCalculator`.
    Rows with no snapshot are skipped silently so the command is safe
    to run before the catalog has fully populated.
    """
    limit_clause = " LIMIT ? " if limit is not None else ""
    params: list[Any] = []
    if limit is not None:
        params.append(int(limit))
    rows = await db.fetch_all(
        f"SELECT id, model_id, original_model_id, provider_id, "
        f"input_tokens, output_tokens, cache_read_tokens, "
        f"cache_write_tokens, reasoning_tokens, "
        f"cost_microdollars "
        f"FROM requests "
        f"WHERE status != 'pending' "
        f"ORDER BY started_at DESC, id DESC{limit_clause}",
        tuple(params),
    )
    calculator = CostCalculator(price_repo=PriceRepository(db))

    cost_total = 0
    new_total = 0
    updated = 0
    skipped_unchanged = 0
    skipped_no_snapshot = 0
    skipped_missing_tokens = 0
    changes: list[dict[str, Any]] = []
    updates: list[tuple[int, int]] = []

    for row in rows:
        old_cost = int(row["cost_microdollars"] or 0)
        cost_total += old_cost
        model_key = str(row["original_model_id"] or row["model_id"])
        provider_id = str(row["provider_id"] or "opencode-go")
        input_tokens = int(row["input_tokens"] or 0)
        output_tokens = int(row["output_tokens"] or 0)
        cache_read = int(row["cache_read_tokens"] or 0)
        cache_write = int(row["cache_write_tokens"] or 0)
        if (
            input_tokens == 0
            and output_tokens == 0
            and cache_read == 0
            and cache_write == 0
        ):
            skipped_missing_tokens += 1
            continue

        new_cost, exactness = await calculator.calculate_cost(
            model_id=model_key,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            provider_id=provider_id,
        )
        new_total += new_cost
        if new_cost == old_cost:
            skipped_unchanged += 1
            continue

        # "estimated" rows were originally priced by the heuristic
        # fallback (no snapshot). Treat them as effectively
        # "no snapshot" today so we don't churn estimated costs based
        # on a snapshot we still don't have.
        snapshot = await calculator._price_repo.get_latest_snapshot(  # pyright: ignore[reportPrivateUsage]
            model_key, provider_id=provider_id
        )
        if snapshot is None:
            skipped_no_snapshot += 1
            new_total -= new_cost
            continue

        delta = new_cost - old_cost
        changes.append(
            {
                "request_id": int(row["id"]),
                "model_id": model_key,
                "provider_id": provider_id,
                "old_cost_microdollars": old_cost,
                "new_cost_microdollars": new_cost,
                "delta_microdollars": delta,
                "exactness": exactness,
            }
        )
        updated += 1
        updates.append((int(row["id"]), new_cost))

    if updates and not dry_run:
        async with db.transaction():
            for batch_start in range(0, len(updates), batch_size):
                batch = updates[batch_start : batch_start + batch_size]
                await db.execute_many(
                    "UPDATE requests SET cost_microdollars = ? WHERE id = ?",
                    [(cost, rid) for rid, cost in batch],
                )

    return RecomputeSummary(
        scanned=len(rows),
        updated=updated,
        skipped_unchanged=skipped_unchanged,
        skipped_no_snapshot=skipped_no_snapshot,
        skipped_missing_tokens=skipped_missing_tokens,
        cost_total_microdollars=cost_total,
        new_cost_total_microdollars=new_total,
        changed_rows=changes,
    )
