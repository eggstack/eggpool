"""Tests for price snapshots and cost calculation."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

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
        assert snapshot.cache_read_per_million_microdollars is None
        assert snapshot.cache_write_per_million_microdollars is None

    def test_price_snapshot_none_prices(self) -> None:
        snapshot = PriceSnapshot(
            model_id="unknown-model",
            input_price_per_1k=None,
            output_price_per_1k=None,
            captured_at="2024-01-01T00:00:00",
        )
        assert snapshot.input_price_per_1k is None
        assert snapshot.output_price_per_1k is None

    def test_price_snapshot_with_cache_fields(self) -> None:
        snapshot = PriceSnapshot(
            model_id="claude-3",
            input_price_per_1k=0.015,
            output_price_per_1k=0.075,
            captured_at="2024-01-01T00:00:00",
            input_per_million_microdollars=15_000_000,
            output_per_million_microdollars=75_000_000,
            cache_read_per_million_microdollars=1_500_000,
            cache_write_per_million_microdollars=18_750_000,
            source="config",
        )
        assert snapshot.cache_read_per_million_microdollars == 1_500_000
        assert snapshot.cache_write_per_million_microdollars == 18_750_000


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

    @pytest.mark.asyncio
    async def test_calculate_cost_no_snapshot(self) -> None:
        mock_repo = AsyncMock()
        mock_repo.get_latest_snapshot = AsyncMock(return_value=None)
        calculator = CostCalculator(price_repo=mock_repo)

        cost, exactness = await calculator.calculate_cost(
            "gpt-4", input_tokens=1000, output_tokens=500
        )
        assert exactness == "estimated"
        assert cost > 0

    @pytest.mark.asyncio
    async def test_calculate_cost_input_only(self) -> None:
        snapshot = PriceSnapshot(
            model_id="gpt-4",
            input_price_per_1k=0.003,
            output_price_per_1k=None,
            captured_at="2024-01-01T00:00:00",
            input_per_million_microdollars=3_000_000,
            output_per_million_microdollars=None,
        )
        mock_repo = AsyncMock()
        mock_repo.get_latest_snapshot = AsyncMock(return_value=snapshot)
        calculator = CostCalculator(price_repo=mock_repo)

        cost, exactness = await calculator.calculate_cost(
            "gpt-4", input_tokens=1000, output_tokens=0
        )
        # 1000 * 3_000_000 / 1_000_000 = 3000
        assert cost == 3000
        assert exactness == "derived"  # input_rate present, output tokens zero

    @pytest.mark.asyncio
    async def test_calculate_cost_complete_derived(self) -> None:
        snapshot = PriceSnapshot(
            model_id="gpt-4",
            input_price_per_1k=0.003,
            output_price_per_1k=0.015,
            captured_at="2024-01-01T00:00:00",
            input_per_million_microdollars=3_000_000,
            output_per_million_microdollars=15_000_000,
            cache_read_per_million_microdollars=300_000,
            cache_write_per_million_microdollars=3_750_000,
        )
        mock_repo = AsyncMock()
        mock_repo.get_latest_snapshot = AsyncMock(return_value=snapshot)
        calculator = CostCalculator(price_repo=mock_repo)

        cost, exactness = await calculator.calculate_cost(
            "gpt-4",
            input_tokens=1000,
            output_tokens=1000,
            cache_read_tokens=0,
            cache_write_tokens=0,
        )
        # (1000 * 3_000_000 + 1000 * 15_000_000) / 1_000_000
        # = 3000 + 15000 = 18000
        assert cost == 18_000
        assert exactness == "derived"

    @pytest.mark.asyncio
    async def test_calculate_cost_with_cache_tokens(self) -> None:
        snapshot = PriceSnapshot(
            model_id="gpt-4",
            input_price_per_1k=0.003,
            output_price_per_1k=0.015,
            captured_at="2024-01-01T00:00:00",
            input_per_million_microdollars=3_000_000,
            output_per_million_microdollars=15_000_000,
            cache_read_per_million_microdollars=300_000,
            cache_write_per_million_microdollars=3_750_000,
        )
        mock_repo = AsyncMock()
        mock_repo.get_latest_snapshot = AsyncMock(return_value=snapshot)
        calculator = CostCalculator(price_repo=mock_repo)

        cost, exactness = await calculator.calculate_cost(
            "gpt-4",
            input_tokens=1000,
            output_tokens=1000,
            cache_read_tokens=500,
            cache_write_tokens=200,
        )
        # input: 1000 * 3_000_000 = 3_000_000_000
        # output: 1000 * 15_000_000 = 15_000_000_000
        # cache_read: 500 * 300_000 = 150_000_000
        # cache_write: 200 * 3_750_000 = 750_000_000
        # total = 18_900_000_000 / 1_000_000 = 18900
        assert cost == 18_900
        assert exactness == "derived"

    @pytest.mark.asyncio
    async def test_calculate_cost_cache_read_no_rate_estimated(self) -> None:
        snapshot = PriceSnapshot(
            model_id="gpt-4",
            input_price_per_1k=0.003,
            output_price_per_1k=0.015,
            captured_at="2024-01-01T00:00:00",
            input_per_million_microdollars=3_000_000,
            output_per_million_microdollars=15_000_000,
            cache_read_per_million_microdollars=None,
            cache_write_per_million_microdollars=None,
        )
        mock_repo = AsyncMock()
        mock_repo.get_latest_snapshot = AsyncMock(return_value=snapshot)
        calculator = CostCalculator(price_repo=mock_repo)

        cost, exactness = await calculator.calculate_cost(
            "gpt-4",
            input_tokens=1000,
            output_tokens=1000,
            cache_read_tokens=500,
            cache_write_tokens=0,
        )
        # cache_read_rate is None (defaulting to 0), but cache_read_tokens > 0
        # exactness should be estimated
        assert exactness == "estimated"
        # cost uses zero cache rate
        assert cost == 18_000

    @pytest.mark.asyncio
    async def test_calculate_cost_cache_write_no_rate_estimated(self) -> None:
        snapshot = PriceSnapshot(
            model_id="gpt-4",
            input_price_per_1k=0.003,
            output_price_per_1k=0.015,
            captured_at="2024-01-01T00:00:00",
            input_per_million_microdollars=3_000_000,
            output_per_million_microdollars=15_000_000,
            cache_read_per_million_microdollars=300_000,
            cache_write_per_million_microdollars=None,
        )
        mock_repo = AsyncMock()
        mock_repo.get_latest_snapshot = AsyncMock(return_value=snapshot)
        calculator = CostCalculator(price_repo=mock_repo)

        cost, exactness = await calculator.calculate_cost(
            "gpt-4",
            input_tokens=1000,
            output_tokens=1000,
            cache_read_tokens=0,
            cache_write_tokens=200,
        )
        # cache_write_rate is None, but cache_write_tokens > 0
        assert exactness == "estimated"

    @pytest.mark.asyncio
    async def test_calculate_cost_fallback_prices_estimated(self) -> None:
        snapshot = PriceSnapshot(
            model_id="unknown",
            input_price_per_1k=0.003,
            output_price_per_1k=0.015,
            captured_at="2024-01-01T00:00:00",
            input_per_million_microdollars=None,
            output_per_million_microdollars=None,
        )
        mock_repo = AsyncMock()
        mock_repo.get_latest_snapshot = AsyncMock(return_value=snapshot)
        calculator = CostCalculator(price_repo=mock_repo)

        cost, exactness = await calculator.calculate_cost(
            "unknown", input_tokens=1000, output_tokens=1000
        )
        # No integer rates available; cost computed from zero rates
        assert exactness == "estimated"
        assert cost == 0


class TestMigrationConversionValues:
    """Verify the mathematical correctness of the conversion formulas."""

    @pytest.mark.parametrize(
        ("price_per_1k", "expected_microdollars"),
        [
            (0.003, 3_000_000),
            (3.0, 3_000_000_000),
            (15.0, 15_000_000_000),
        ],
    )
    def test_correct_conversion_formula(
        self, price_per_1k: float, expected_microdollars: int
    ) -> None:
        """Migration 0006 must multiply dollars/1K by 1_000_000_000."""
        result = int(price_per_1k * 1_000_000_000)
        assert result == expected_microdollars

    def test_wrong_conversion_was_1000(self) -> None:
        """The old migration 0005 multiplied by 1000 (wrong)."""
        price_per_1k = 0.003
        wrong_result = int(price_per_1k * 1000)
        correct_result = int(price_per_1k * 1_000_000_000)
        assert wrong_result == 3
        assert correct_result == 3_000_000
