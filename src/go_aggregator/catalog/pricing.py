"""Price snapshot storage and derived cost calculation."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from go_aggregator.db.connection import Database

logger = logging.getLogger(__name__)


@dataclass
class PriceSnapshot:
    """Price information for a model at a point in time."""

    model_id: str
    input_price_per_1k: float | None  # Dollars per 1K input tokens
    output_price_per_1k: float | None  # Dollars per 1K output tokens
    captured_at: str


class PriceRepository:
    """Repository for model price snapshots."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def record_snapshot(
        self,
        model_id: str,
        input_price_per_1k: float | None,
        output_price_per_1k: float | None,
    ) -> None:
        """Record a price snapshot for a model."""
        await self._db.execute(
            """
            INSERT INTO model_price_snapshots
                (model_id, input_price_per_1k, output_price_per_1k)
            VALUES (?, ?, ?)
            """,
            (model_id, input_price_per_1k, output_price_per_1k),
        )
        await self._db.connection.commit()

    async def get_latest_snapshot(self, model_id: str) -> PriceSnapshot | None:
        """Get the most recent price snapshot for a model."""
        row = await self._db.fetch_one(
            """
            SELECT model_id, input_price_per_1k, output_price_per_1k, captured_at
            FROM model_price_snapshots
            WHERE model_id = ?
            ORDER BY captured_at DESC
            LIMIT 1
            """,
            (model_id,),
        )
        if row is None:
            return None
        return PriceSnapshot(
            model_id=row["model_id"],
            input_price_per_1k=row["input_price_per_1k"],
            output_price_per_1k=row["output_price_per_1k"],
            captured_at=row["captured_at"],
        )

    async def get_snapshots_since(
        self, model_id: str, since_hours: int = 24
    ) -> list[PriceSnapshot]:
        """Get price snapshots for a model since the given time."""
        rows = await self._db.fetch_all(
            """
            SELECT model_id, input_price_per_1k, output_price_per_1k, captured_at
            FROM model_price_snapshots
            WHERE model_id = ? AND captured_at > datetime('now', ? || ' hours')
            ORDER BY captured_at DESC
            """,
            (model_id, f"-{since_hours}"),
        )
        return [
            PriceSnapshot(
                model_id=row["model_id"],
                input_price_per_1k=row["input_price_per_1k"],
                output_price_per_1k=row["output_price_per_1k"],
                captured_at=row["captured_at"],
            )
            for row in rows
        ]


class CostCalculator:
    """Calculates derived costs from token usage and price snapshots."""

    def __init__(self, price_repo: PriceRepository) -> None:
        self._price_repo = price_repo

    async def calculate_cost(
        self,
        model_id: str,
        input_tokens: int,
        output_tokens: int,
    ) -> tuple[int, str]:
        """Calculate cost in microdollars from token usage.

        Returns:
            Tuple of (cost_microdollars, exactness_level)
        """
        snapshot = await self._price_repo.get_latest_snapshot(model_id)

        if snapshot is None:
            # No price data available - use estimation
            return self._estimate_cost(input_tokens, output_tokens), "estimated"

        if snapshot.input_price_per_1k is None or snapshot.output_price_per_1k is None:
            # Incomplete price data
            return self._estimate_cost(input_tokens, output_tokens), "estimated"

        # Calculate exact cost using price snapshot
        # Price is in dollars per 1K tokens
        # Convert to microdollars (1 dollar = 1,000,000 microdollars)
        input_cost = (input_tokens / 1000.0) * snapshot.input_price_per_1k
        output_cost = (output_tokens / 1000.0) * snapshot.output_price_per_1k
        total_cost = input_cost + output_cost

        # Convert to microdollars (integer)
        cost_microdollars = int(total_cost * 1_000_000)

        return cost_microdollars, "derived"

    def _estimate_cost(self, input_tokens: int, output_tokens: int) -> int:
        """Estimate cost when no price data is available.

        Uses rough estimates for common model tiers.
        """
        # Rough estimates in dollars per 1K tokens
        # These are fallback estimates - actual prices vary significantly
        estimated_input_price = 0.003  # $3 per 1M input tokens
        estimated_output_price = 0.015  # $15 per 1M output tokens

        input_cost = (input_tokens / 1000.0) * estimated_input_price
        output_cost = (output_tokens / 1000.0) * estimated_output_price
        total_cost = input_cost + output_cost

        return int(total_cost * 1_000_000)
