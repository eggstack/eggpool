"""EWMA (Exponentially Weighted Moving Average) model cost estimator."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from go_aggregator.db.connection import Database

logger = logging.getLogger(__name__)


@dataclass
class ModelCostEstimate:
    """Cost estimate for a model using EWMA."""

    model_id: str
    ewma_input_cost_per_1k: float = 0.0
    ewma_output_cost_per_1k: float = 0.0
    sample_count: int = 0
    last_updated: float = field(default_factory=time.time)


class EWMACostEstimator:
    """Estimates model costs using Exponentially Weighted Moving Average.

    This provides a running estimate of costs based on observed usage,
    which is useful when explicit price snapshots are not available.
    """

    def __init__(
        self,
        db: Database,
        alpha: float = 0.1,
        min_samples: int = 5,
    ) -> None:
        """Initialize the EWMA cost estimator.

        Args:
            db: Database connection
            alpha: EWMA smoothing factor (0-1). Lower values = more smoothing.
            min_samples: Minimum samples before providing estimates
        """
        self._db = db
        self._alpha = alpha
        self._min_samples = min_samples
        self._estimates: dict[str, ModelCostEstimate] = {}

    async def record_observation(
        self,
        model_id: str,
        input_tokens: int,
        output_tokens: int,
        cost_microdollars: int,
    ) -> None:
        """Record a cost observation for EWMA calculation.

        Args:
            model_id: Model identifier
            input_tokens: Number of input tokens
            output_tokens: Number of output tokens
            cost_microdollars: Actual or derived cost in microdollars
        """
        if input_tokens == 0 and output_tokens == 0:
            return

        # Get or create estimate
        estimate = self._estimates.get(model_id)
        if estimate is None:
            estimate = ModelCostEstimate(model_id=model_id)
            self._estimates[model_id] = estimate

        # Calculate per-1k-token costs for this observation
        if input_tokens > 0:
            observed_input_cost = (cost_microdollars / input_tokens) * 1000
        else:
            observed_input_cost = 0.0

        if output_tokens > 0:
            observed_output_cost = (cost_microdollars / output_tokens) * 1000
        else:
            observed_output_cost = 0.0

        # Update EWMA
        if estimate.sample_count == 0:
            # First observation
            estimate.ewma_input_cost_per_1k = observed_input_cost
            estimate.ewma_output_cost_per_1k = observed_output_cost
        else:
            # Apply EWMA formula: new = alpha * observed + (1 - alpha) * old
            estimate.ewma_input_cost_per_1k = (
                self._alpha * observed_input_cost
                + (1 - self._alpha) * estimate.ewma_input_cost_per_1k
            )
            estimate.ewma_output_cost_per_1k = (
                self._alpha * observed_output_cost
                + (1 - self._alpha) * estimate.ewma_output_cost_per_1k
            )

        estimate.sample_count += 1
        estimate.last_updated = time.time()

    def get_estimate(self, model_id: str) -> ModelCostEstimate | None:
        """Get the current EWMA cost estimate for a model.

        Returns None if not enough samples have been collected.
        """
        estimate = self._estimates.get(model_id)
        if estimate is None or estimate.sample_count < self._min_samples:
            return None
        return estimate

    def estimate_cost(
        self,
        model_id: str,
        input_tokens: int,
        output_tokens: int,
    ) -> int | None:
        """Estimate cost in microdollars using EWMA.

        Returns None if not enough data for estimation.
        """
        estimate = self.get_estimate(model_id)
        if estimate is None:
            return None

        input_cost = (input_tokens / 1000.0) * estimate.ewma_input_cost_per_1k
        output_cost = (output_tokens / 1000.0) * estimate.ewma_output_cost_per_1k
        total_cost = input_cost + output_cost

        return int(total_cost)

    async def load_from_database(self) -> None:
        """Load historical cost data from database to initialize EWMA."""
        # Query recent requests with valid costs
        rows = await self._db.fetch_all(
            """
            SELECT model_id, input_tokens, output_tokens, cost_microdollars
            FROM requests
            WHERE cost_microdollars > 0
              AND (input_tokens > 0 OR output_tokens > 0)
              AND completed_at > datetime('now', '-7 days')
            ORDER BY completed_at DESC
            LIMIT 1000
            """,
        )

        # Process in chronological order (oldest first)
        for row in reversed(rows):
            await self.record_observation(
                model_id=row["model_id"],
                input_tokens=row["input_tokens"] or 0,
                output_tokens=row["output_tokens"] or 0,
                cost_microdollars=row["cost_microdollars"],
            )

        logger.info(
            "Loaded EWMA cost estimates for %d models from %d observations",
            len(self._estimates),
            len(rows),
        )
