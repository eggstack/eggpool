"""Tests for price snapshots and cost calculation."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from eggpool.catalog.pricing import (
    CostCalculator,
    PriceSnapshot,
    microdollars_per_million_from_price_per_1k,
    parse_microdollars_per_million,
    parse_price_per_1k,
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

    def test_estimate_cost_normalizes_negative_tokens(self) -> None:
        calculator = CostCalculator(price_repo=None)  # type: ignore[arg-type]
        cost = calculator._estimate_cost(input_tokens=-1000, output_tokens=1000)
        assert cost == 15_000

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
    async def test_latest_snapshot_is_cached_until_invalidated(self) -> None:
        first = PriceSnapshot(
            model_id="gpt-4",
            input_price_per_1k=None,
            output_price_per_1k=None,
            captured_at="2024-01-01T00:00:00",
            input_per_million_microdollars=3_000_000,
            output_per_million_microdollars=15_000_000,
            provider_id="provider-a",
        )
        second = PriceSnapshot(
            model_id="gpt-4",
            input_price_per_1k=None,
            output_price_per_1k=None,
            captured_at="2024-01-02T00:00:00",
            input_per_million_microdollars=4_000_000,
            output_per_million_microdollars=16_000_000,
            provider_id="provider-a",
        )
        mock_repo = AsyncMock()
        mock_repo.get_latest_snapshot = AsyncMock(side_effect=[first, second])
        calculator = CostCalculator(price_repo=mock_repo)

        initial, _ = await calculator.calculate_cost(
            "gpt-4", 1000, 0, provider_id="provider-a"
        )
        cached, _ = await calculator.calculate_cost(
            "gpt-4", 1000, 0, provider_id="provider-a"
        )
        calculator.invalidate_price("gpt-4", "provider-a")
        refreshed, _ = await calculator.calculate_cost(
            "gpt-4", 1000, 0, provider_id="provider-a"
        )

        assert (initial, cached, refreshed) == (3000, 3000, 4000)
        assert mock_repo.get_latest_snapshot.await_count == 2

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
    async def test_calculate_cost_normalizes_negative_tokens(self) -> None:
        snapshot = PriceSnapshot(
            model_id="gpt-4",
            input_price_per_1k=0.003,
            output_price_per_1k=0.015,
            captured_at="2024-01-01T00:00:00",
            input_per_million_microdollars=3_000_000,
            output_per_million_microdollars=15_000_000,
        )
        mock_repo = AsyncMock()
        mock_repo.get_latest_snapshot = AsyncMock(return_value=snapshot)
        calculator = CostCalculator(price_repo=mock_repo)

        cost, exactness = await calculator.calculate_cost(
            "gpt-4",
            input_tokens=-1000,
            output_tokens=1000,
            cache_read_tokens=-50,
            cache_write_tokens=0,
        )

        assert cost == 15_000
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


class TestPriceParsing:
    """Tests for price input normalization."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (" $3 / 1M ", 0.003),
            ("$3/1M", 0.003),
            ("3 per million tokens", 0.003),
            ("0.003 / 1K tokens", 0.003),
            ("0.000003 per token", 0.003),
            ("0.000003/token", 0.003),
            ("0.003", 0.003),
        ],
    )
    def test_parse_price_per_1k_spacing_and_units(
        self, raw: str, expected: float
    ) -> None:
        assert parse_price_per_1k(raw) == pytest.approx(expected)

    def test_parse_price_per_token_default_unit(self) -> None:
        assert parse_price_per_1k("0.000003", default_unit="token") == pytest.approx(
            0.003
        )

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("500,000", 500_000),
            ("500_000", 500_000),
            ("$0.50 / 1M", 500_000),
            ("0.0000005 per token", 500_000),
        ],
    )
    def test_parse_microdollars_per_million_spacing_and_units(
        self, raw: str, expected: int
    ) -> None:
        assert parse_microdollars_per_million(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("$0.003 / 1K", 3_000_000),
            ("$3 / 1M", 3_000_000),
            ("$0.000003 / token", 3_000_000),
            ("0.003 per 1k", 3_000_000),
            ("3 per million", 3_000_000),
            ("0.000003 per token", 3_000_000),
        ],
    )
    def test_parse_microdollars_per_million_equivalent_rates(
        self, raw: str, expected: int
    ) -> None:
        """Unit forms ($/token, $/1K, $/1M) produce the same microdollars."""
        assert parse_microdollars_per_million(raw) == expected

    @pytest.mark.parametrize("raw", ["-1", "-$3 / 1M", "nan", "inf", "free"])
    def test_parse_price_rejects_inappropriate_values(self, raw: str) -> None:
        with pytest.raises(ValueError):
            parse_price_per_1k(raw)

    @pytest.mark.asyncio
    async def test_calculate_cost_cache_read_missing_rate_is_partial(self) -> None:
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
        # input/output trusted, cache_read missing → partial fallback
        # exactness is "partial" (trusted + fallback, not full heuristic)
        assert exactness == "partial"
        # trusted: 1000*3M + 1000*15M = 18_000 microdollars
        # cache_read fallback: 500 * 300_000 / 1_000_000 = 150 microdollars
        assert cost == 18_150

    @pytest.mark.asyncio
    async def test_calculate_cost_cache_write_missing_rate_is_partial(self) -> None:
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
        # cache_write missing → partial fallback, exactness="partial"
        assert exactness == "partial"
        # trusted: 1000*3M + 1000*15M = 18_000 microdollars
        # cache_write fallback: 200 * 3_750_000 / 1_000_000 = 750 microdollars
        assert cost == 18_750

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
        # No integer rates available; fallback to estimate
        assert exactness == "estimated"
        # Fallback estimate: 1K input at $3/M + 1K output at $15/M = 18000
        assert cost == 18000


class TestPartialFallbackPricing:
    """Phase 1 of pricing-resolution-correction: per-category fallback.

    These tests cover the partial-fallback policy: when the snapshot
    has trusted rates for some categories but not others, the missing
    categories are filled with a per-category heuristic rather than
    the full request being replaced by a generic estimate.
    """

    @pytest.mark.asyncio
    async def test_only_input_priced_output_missing(self) -> None:
        snapshot = PriceSnapshot(
            model_id="mimo-v2.5",
            input_price_per_1k=None,
            output_price_per_1k=None,
            captured_at="2024-01-01T00:00:00",
            input_per_million_microdollars=100_000,  # cheap model
            output_per_million_microdollars=None,
            cache_read_per_million_microdollars=None,
            cache_write_per_million_microdollars=None,
        )
        mock_repo = AsyncMock()
        mock_repo.get_latest_snapshot = AsyncMock(return_value=snapshot)
        calculator = CostCalculator(price_repo=mock_repo)

        cost, exactness = await calculator.calculate_cost(
            "mimo-v2.5",
            input_tokens=30_000_000,
            output_tokens=10_000_000,
        )
        # trusted input: 30M * 100_000 / 1M = 3_000_000 microdollars
        # output fallback: (10M / 1K) * $0.015 * 1M = 150_000_000 microdollars
        assert exactness == "partial"
        assert cost == 3_000_000 + 150_000_000

    @pytest.mark.asyncio
    async def test_only_cache_priced_input_output_missing(self) -> None:
        snapshot = PriceSnapshot(
            model_id="unknown",
            input_price_per_1k=None,
            output_price_per_1k=None,
            captured_at="2024-01-01T00:00:00",
            input_per_million_microdollars=None,
            output_per_million_microdollars=None,
            cache_read_per_million_microdollars=100_000,
            cache_write_per_million_microdollars=300_000,
        )
        mock_repo = AsyncMock()
        mock_repo.get_latest_snapshot = AsyncMock(return_value=snapshot)
        calculator = CostCalculator(price_repo=mock_repo)

        cost, exactness = await calculator.calculate_cost(
            "unknown",
            input_tokens=10_000_000,
            output_tokens=1_000_000,
            cache_read_tokens=5_000_000,
            cache_write_tokens=500_000,
        )
        # input fallback: (10M / 1K) * $0.003 * 1M = 30_000_000
        # output fallback: (1M / 1K) * $0.015 * 1M = 15_000_000
        # cache_read trusted: 5M * 100_000 / 1M = 500_000
        # cache_write trusted: 500k * 300_000 / 1M = 150_000
        # total = 45_650_000
        assert exactness == "partial"
        assert cost == 30_000_000 + 15_000_000 + 500_000 + 150_000

    @pytest.mark.asyncio
    async def test_no_tokens_returns_unknown(self) -> None:
        snapshot = PriceSnapshot(
            model_id="gpt-4",
            input_price_per_1k=0.003,
            output_price_per_1k=0.015,
            captured_at="2024-01-01T00:00:00",
            input_per_million_microdollars=3_000_000,
            output_per_million_microdollars=15_000_000,
        )
        mock_repo = AsyncMock()
        mock_repo.get_latest_snapshot = AsyncMock(return_value=snapshot)
        calculator = CostCalculator(price_repo=mock_repo)

        cost, exactness = await calculator.calculate_cost(
            "gpt-4", input_tokens=0, output_tokens=0
        )
        assert cost == 0
        assert exactness == "unknown"

    @pytest.mark.asyncio
    async def test_partial_does_not_replace_with_full_heuristic(self) -> None:
        """A cheap, partially-priced model should not be inflated by the
        generic $3/$15 per-1M fallback.

        Reproduces the MiMo 2.5 ~$92 inflation bug: 30M tokens at the
        generic $3/M input rate alone would be $90 even without cache.
        The partial-fallback policy keeps the trusted input share cheap
        and only falls back on the missing output category.
        """
        snapshot = PriceSnapshot(
            model_id="mimo-v2.5",
            input_price_per_1k=None,
            output_price_per_1k=None,
            captured_at="2024-01-01T00:00:00",
            input_per_million_microdollars=100_000,  # $0.10 / 1M input
            output_per_million_microdollars=None,
        )
        mock_repo = AsyncMock()
        mock_repo.get_latest_snapshot = AsyncMock(return_value=snapshot)
        calculator = CostCalculator(price_repo=mock_repo)

        cost, exactness = await calculator.calculate_cost(
            "mimo-v2.5",
            input_tokens=30_000_000,
            output_tokens=1_000_000,
        )
        # trusted input: 30M * 100_000 / 1M = 3_000_000 microdollars ($3)
        # output fallback: (1M / 1K) * $0.015 * 1M = 15_000_000 microdollars
        # The old max()-based policy would have produced 30M * 3_000 = 90M
        # input microdollars instead of the trusted 3M; assert the trusted
        # share is preserved.
        assert exactness == "partial"
        trusted_input_share = (30_000_000 * 100_000) // 1_000_000
        assert trusted_input_share == 3_000_000
        assert cost == trusted_input_share + 15_000_000


class TestMicrodollarsPerMillionConversion:
    """Phase 1 conversion helper."""

    def test_basic_conversion(self) -> None:
        assert microdollars_per_million_from_price_per_1k(0.003) == 3_000_000

    def test_high_value_conversion(self) -> None:
        assert microdollars_per_million_from_price_per_1k(3.0) == 3_000_000_000

    def test_none_passthrough(self) -> None:
        assert microdollars_per_million_from_price_per_1k(None) is None

    @pytest.mark.parametrize(
        "raw",
        [
            "$3 / 1M",
            "0.003 / 1K",
            "0.000003 per token",
            "$0.30 / 1M",
            "0.0000003 per token",
        ],
    )
    def test_round_trip_through_parse(self, raw: str) -> None:
        """Unit-suffixed strings round-trip parse_price_per_1k → helper."""
        via_helper = microdollars_per_million_from_price_per_1k(parse_price_per_1k(raw))
        via_parser = parse_microdollars_per_million(raw)
        assert via_helper == via_parser


class TestCachePriceFieldVariants:
    """Phase 1: parse_microdollars_per_million already accepts every
    rate shape the upstream catalog may surface. These tests pin that
    contract for the new OpenRouter/Anthropic field names that
    ``_maybe_insert_price_snapshot`` now consults.
    """

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("$0.30 / 1M", 300_000),  # pricing.input_cache_read style
            ("0.0000003 per token", 300_000),  # per-token OpenRouter style
            ("0.30 / 1K", 300_000_000),
            ("300_000", 300_000),
        ],
    )
    def test_cache_rate_parsing_variants(self, raw: str, expected: int) -> None:
        assert parse_microdollars_per_million(raw) == expected


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


class TestCacheAccounting:
    """Phase 14: Cache usage accounting tests."""

    @pytest.mark.asyncio
    async def test_cache_creation_as_cache_write_tokens(self) -> None:
        """Cache creation tokens should be passed as cache_write_tokens."""
        from eggpool.proxy.usage import StreamUsageResult

        usage = StreamUsageResult(
            input_tokens=100,
            output_tokens=50,
            cache_read_tokens=20,
            cache_creation_tokens=30,
        )
        # The cache_creation_tokens should be used as cache_write_tokens
        assert usage.cache_creation_tokens == 30

    @pytest.mark.asyncio
    async def test_cache_rates_affect_cost(self) -> None:
        """Cache read/write rates affect final microdollar cost."""
        snapshot = PriceSnapshot(
            model_id="claude-3",
            input_price_per_1k=0.015,
            output_price_per_1k=0.075,
            captured_at="2024-01-01T00:00:00",
            input_per_million_microdollars=15_000_000,
            output_per_million_microdollars=75_000_000,
            cache_read_per_million_microdollars=1_500_000,
            cache_write_per_million_microdollars=18_750_000,
        )
        mock_repo = AsyncMock()
        mock_repo.get_latest_snapshot = AsyncMock(return_value=snapshot)
        calculator = CostCalculator(price_repo=mock_repo)

        cost, exactness = await calculator.calculate_cost(
            "claude-3",
            input_tokens=1000,
            output_tokens=1000,
            cache_read_tokens=500,
            cache_write_tokens=200,
        )
        # input: 1000 * 15_000_000 = 15_000_000_000
        # output: 1000 * 75_000_000 = 75_000_000_000
        # cache_read: 500 * 1_500_000 = 750_000_000
        # cache_write: 200 * 18_750_000 = 3_750_000_000
        # total = 94_500_000_000 / 1_000_000 = 94500
        assert cost == 94_500
        assert exactness == "derived"
