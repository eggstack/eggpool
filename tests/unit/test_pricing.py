"""Tests for price snapshots and cost calculation."""

from __future__ import annotations

from go_aggregator.catalog.pricing import (
    CostCalculator,
    PriceSnapshot,
)


class TestPriceSnapshot:
    """Tests for PriceSnapshot dataclass."""

    def test_price_snapshot_creation(self) -> None:
        snapshot = PriceSnapshot(
            model_id="gpt-4",
            input_price_per_1k=0.03,
            output_price_per_1k=0.06,
            captured_at="2024-01-01T00:00:00",
        )
        assert snapshot.model_id == "gpt-4"
        assert snapshot.input_price_per_1k == 0.03
        assert snapshot.output_price_per_1k == 0.06

    def test_price_snapshot_none_prices(self) -> None:
        snapshot = PriceSnapshot(
            model_id="unknown-model",
            input_price_per_1k=None,
            output_price_per_1k=None,
            captured_at="2024-01-01T00:00:00",
        )
        assert snapshot.input_price_per_1k is None
        assert snapshot.output_price_per_1k is None


class TestCostCalculator:
    """Tests for cost calculation."""

    def test_estimate_cost_no_prices(self) -> None:
        calculator = CostCalculator(price_repo=None)  # type: ignore[arg-type]
        cost = calculator._estimate_cost(input_tokens=1000, output_tokens=500)

        # Should return estimated cost
        assert cost > 0
        # Input: 1000 tokens * $3/1M = $0.003 = 3000 microdollars
        # Output: 500 tokens * $15/1M = $0.0075 = 7500 microdollars
        # Allow for floating point imprecision
        assert 10499 <= cost <= 10501

    def test_estimate_cost_zero_tokens(self) -> None:
        calculator = CostCalculator(price_repo=None)  # type: ignore[arg-type]
        cost = calculator._estimate_cost(input_tokens=0, output_tokens=0)
        assert cost == 0
