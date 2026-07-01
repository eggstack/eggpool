"""Generalizable guards against catastrophic cost-inflation bugs.

These tests cover the defense-in-depth set:

* Pricing unit disambiguation uses sibling context (any provider, not
  model-specific).
* EWMA outlier rejection prevents poisoning from a single bad
  observation.
* Anthropic input-token extraction falls back to vendor-agnostic
  alternative paths.
* Per-request cost-per-token sanity check downgrades inflated
  ``derived`` exactness to ``estimated``.

Each guard is exercised through the public API so a regression in any
layer of the cost pipeline surfaces here rather than at a specific
model fixture.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from eggpool.catalog.pricing import (
    CostCalculator,
    PriceSnapshot,
)
from eggpool.catalog.pricing_resolver import resolve_pricing_from_metadata
from eggpool.proxy.usage import AnthropicStreamUsageExtractor
from eggpool.quota.estimation import QuotaEstimator


class TestSiblingUnitDisambiguation:
    """Pricing unit resolution must use sibling context, not magic numbers."""

    def test_minimax_bare_value_with_per_million_sibling(self) -> None:
        """Siblings that look like per-million steer the whole dict to per-million."""
        result = resolve_pricing_from_metadata(
            model_id="minimax-m3",
            provider_id="minimax",
            model_info={
                "source_metadata": {
                    "pricing": {
                        "prompt": "0.0002",
                        "completion": "0.0011",
                    }
                }
            },
            override_values={},
        )
        assert result is not None
        # 0.0002/M → $0.20/1M → 0.0002/1K
        assert result.input_price_per_1k == pytest.approx(0.0000002)
        assert result.output_price_per_1k == pytest.approx(0.0000011)

    def test_openrouter_siblings_steer_to_per_token(self) -> None:
        """Tiny siblings stay per-token — no regression for OpenRouter."""
        result = resolve_pricing_from_metadata(
            model_id="mimo-v2.5",
            provider_id="opencode-go",
            model_info={
                "source_metadata": {
                    "pricing": {
                        "prompt": "0.000000105",
                        "completion": "0.00000028",
                    }
                }
            },
            override_values={},
        )
        assert result is not None
        # per-token → × 1000 → $0.000105/1K
        assert result.input_price_per_1k == pytest.approx(0.000105)
        assert result.output_price_per_1k == pytest.approx(0.00028)

    def test_explicit_unit_suffix_in_sibling_wins_over_magnitude(self) -> None:
        """An explicit per-million suffix overrides the magnitude heuristic."""
        result = resolve_pricing_from_metadata(
            model_id="mixed-vendor",
            provider_id="unknown",
            model_info={
                "source_metadata": {
                    "pricing": {
                        # Tiny value, but the sibling explicitly says per-million.
                        "prompt": "0.0005",
                        "completion": "2 / 1M",
                    }
                }
            },
            override_values={},
        )
        assert result is not None
        # The explicit per-million suffix steers both fields to per-million.
        assert result.input_price_per_1k == pytest.approx(0.0000005)
        assert result.output_price_per_1k == pytest.approx(0.002)

    def test_single_value_defaults_to_per_million(self) -> None:
        """Single-value dict with no scale consensus defaults to per-million.

        Per-million is the conservative direction; the legacy
        magic-number heuristic would have produced per-token for
        values < 0.001 and per-million for values >= 0.001, splitting
        the cost interpretation across an arbitrary boundary.
        """
        result = resolve_pricing_from_metadata(
            model_id="solo",
            provider_id="unknown",
            model_info={
                "source_metadata": {
                    "pricing": {
                        "prompt": "0.5",
                    }
                }
            },
            override_values={},
        )
        assert result is not None
        # per-million default → 0.5 / 1000 = 0.0005 / 1K
        assert result.input_price_per_1k == pytest.approx(0.0005)

    def test_three_sibling_consensus(self) -> None:
        """Three siblings in agreement pick the consistent unit."""
        result = resolve_pricing_from_metadata(
            model_id="three-siblings",
            provider_id="unknown",
            model_info={
                "source_metadata": {
                    "pricing": {
                        "prompt": "0.1",
                        "completion": "0.5",
                        "request": "1.0",
                    }
                }
            },
            override_values={},
        )
        assert result is not None
        # All three are >= 0.001 → per-million.
        assert result.input_price_per_1k == pytest.approx(0.0001)


class TestEWMAOutlierRejection:
    """EWMA updates from a single bad observation must not poison the estimate."""

    def test_outlier_observation_does_not_pollute_ewma(self) -> None:
        estimator = QuotaEstimator()
        # Five legitimate observations establish a baseline.
        for _ in range(5):
            estimator.record_usage(
                "acct", tokens=1000, cost_microdollars=15_000, model_id="m"
            )
        baseline_estimate = estimator.global_model_ewma["m"].estimate_cost_per_token
        baseline_samples = estimator.global_model_ewma["m"].sample_count

        # A 1,000,000x-inflated observation: simulates a misread
        # dollars/M value being treated as dollars/token.
        estimator.record_usage(
            "acct", tokens=1000, cost_microdollars=15_000_000_000, model_id="m"
        )

        post_estimate = estimator.global_model_ewma["m"].estimate_cost_per_token
        post_samples = estimator.global_model_ewma["m"].sample_count

        # The outlier was rejected — sample count and estimate unchanged.
        assert post_samples == baseline_samples
        assert post_estimate == baseline_estimate

    def test_outlier_account_bucket_also_protected(self) -> None:
        """The per-account EWMA bucket is guarded the same way."""
        estimator = QuotaEstimator()
        for _ in range(5):
            estimator.record_usage(
                "acct", tokens=1000, cost_microdollars=15_000, model_id="m"
            )
        bucket = estimator.account_model_ewma["acct"]
        baseline_estimate = bucket["m"].estimate_cost_per_token
        baseline_samples = bucket["m"].sample_count

        estimator.record_usage(
            "acct",
            tokens=1000,
            cost_microdollars=15_000_000_000,
            model_id="m",
        )

        assert bucket["m"].sample_count == baseline_samples
        assert bucket["m"].estimate_cost_per_token == baseline_estimate

    def test_first_observation_never_rejected(self) -> None:
        """The first sample for a new model is always accepted (no baseline)."""
        estimator = QuotaEstimator()
        estimator.record_usage(
            "acct", tokens=1000, cost_microdollars=10_000_000, model_id="new"
        )
        assert "new" in estimator.global_model_ewma
        assert estimator.global_model_ewma["new"].sample_count == 1

    def test_legitimate_price_change_admitted(self) -> None:
        """A 5x price change (within the band) folds into the EWMA."""
        estimator = QuotaEstimator()
        for _ in range(10):
            estimator.record_usage(
                "acct", tokens=1000, cost_microdollars=15_000, model_id="m"
            )
        baseline = estimator.global_model_ewma["m"].estimate_cost_per_token

        # 5x jump — within the 100x outlier band, should be admitted.
        estimator.record_usage(
            "acct", tokens=1000, cost_microdollars=75_000, model_id="m"
        )
        post = estimator.global_model_ewma["m"].estimate_cost_per_token
        assert post != baseline

    def test_outlier_still_recorded_on_quota_windows(self) -> None:
        """Outliers are excluded from EWMA but still flow into usage windows.

        The accounting total must reflect the real spend, even when the
        rolling estimate is shielded from contamination. Otherwise an
        outlier rejection would silently underreport cost.
        """
        estimator = QuotaEstimator()
        for _ in range(5):
            estimator.record_usage(
                "acct", tokens=1000, cost_microdollars=15_000, model_id="m"
            )
        estimator.record_usage(
            "acct", tokens=1000, cost_microdollars=15_000_000_000, model_id="m"
        )
        # 5 legitimate + 1 outlier. Daily window should include all six.
        tokens, cost = estimator.accounts["acct"].daily_window.get_usage()
        assert tokens == 6000
        assert cost == 15_000_075_000


class TestAnthropicInputTokenFallbacks:
    """Vendor-agnostic extraction of input tokens from non-canonical events."""

    def test_canonical_message_start_still_works(self) -> None:
        extractor = AnthropicStreamUsageExtractor()
        result = extractor.extract(
            {
                "type": "message_start",
                "message": {"usage": {"input_tokens": 1234}},
            }
        )
        assert result is not None
        assert result.input_tokens == 1234

    def test_message_start_without_usage_marks_input_unseen(self) -> None:
        extractor = AnthropicStreamUsageExtractor()
        # No usage block at all — must not raise, must defer to delta.
        result = extractor.extract({"type": "message_start", "message": {}})
        assert result is None
        # Subsequent message_delta carries input tokens via fallback.
        delta = extractor.extract(
            {
                "type": "message_delta",
                "usage": {"output_tokens": 50, "input_tokens": 999},
            }
        )
        assert delta is not None
        assert delta.input_tokens == 999
        assert delta.output_tokens == 50

    def test_message_delta_with_input_tokens_fallback(self) -> None:
        """Vendor emits input tokens only on the closing event."""
        extractor = AnthropicStreamUsageExtractor()
        # No message_start at all.
        delta = extractor.extract(
            {
                "type": "message_delta",
                "usage": {"output_tokens": 200, "input_tokens": 800},
            }
        )
        assert delta is not None
        assert delta.input_tokens == 800
        assert delta.output_tokens == 200

    def test_message_delta_with_prompt_tokens_alias(self) -> None:
        """OpenAI-style alias is accepted on the closing event."""
        extractor = AnthropicStreamUsageExtractor()
        delta = extractor.extract(
            {
                "type": "message_delta",
                "usage": {"output_tokens": 200, "prompt_tokens": 800},
            }
        )
        assert delta is not None
        assert delta.input_tokens == 800

    def test_input_tokens_already_seen_not_overwritten(self) -> None:
        """A second delta does not clobber the canonical input count."""
        extractor = AnthropicStreamUsageExtractor()
        extractor.extract(
            {
                "type": "message_start",
                "message": {"usage": {"input_tokens": 500}},
            }
        )
        # Second delta with no input field — must not fall back to 0.
        delta = extractor.extract(
            {"type": "message_delta", "usage": {"output_tokens": 25}}
        )
        assert delta is not None
        assert delta.input_tokens == 0  # This delta contributes no input
        assert delta.output_tokens == 25


class TestDerivedCostPerTokenSanity:
    """An inflated derived cost must downgrade exactness, not persist as-is."""

    @pytest.mark.asyncio
    async def test_implausible_per_token_rate_downgrades_to_estimated(self) -> None:
        """A snapshot rate that implies >$1/token must not be reported as derived."""
        snapshot = PriceSnapshot(
            model_id="victim",
            input_price_per_1k=None,
            output_price_per_1k=None,
            captured_at="2024-01-01T00:00:00",
            # 1 billion microdollars/M input rate → 1000 input tokens
            # × 1_000_000_000 / 1_000_000 = 1_000_000 microdollars.
            # Combined with 1_000_000 from output, total 2_000_000
            # over 2000 tokens = 1000 microdollars/token — exactly at
            # the trust ceiling, not yet triggering.
            input_per_million_microdollars=1_000_000_000,
            output_per_million_microdollars=1_000_000_000,
        )
        mock_repo = AsyncMock()
        mock_repo.get_latest_snapshot = AsyncMock(return_value=snapshot)
        calculator = CostCalculator(price_repo=mock_repo)

        cost, exactness = await calculator.calculate_cost(
            "victim", input_tokens=1000, output_tokens=1000
        )
        assert cost == 2_000_000
        assert exactness == "derived"

        # Now push the implicit rate above the trust ceiling. 2.5
        # billion microdollars/M × 2000 output tokens = 5_000_000
        # microdollars → 2500 microdollars/token total — well above
        # the 1000 ceiling, must downgrade.
        inflated_snapshot = PriceSnapshot(
            model_id="victim",
            input_price_per_1k=None,
            output_price_per_1k=None,
            captured_at="2024-01-01T00:00:00",
            input_per_million_microdollars=0,
            output_per_million_microdollars=2_500_000_000,
        )
        mock_repo.get_latest_snapshot = AsyncMock(return_value=inflated_snapshot)
        calculator.invalidate_price("victim")
        cost, exactness = await calculator.calculate_cost(
            "victim", input_tokens=1000, output_tokens=2000
        )
        assert cost > 0
        assert exactness == "estimated"

    @pytest.mark.asyncio
    async def test_normal_rate_stays_derived(self) -> None:
        snapshot = PriceSnapshot(
            model_id="normal",
            input_price_per_1k=None,
            output_price_per_1k=None,
            captured_at="2024-01-01T00:00:00",
            input_per_million_microdollars=3_000_000,
            output_per_million_microdollars=15_000_000,
        )
        mock_repo = AsyncMock()
        mock_repo.get_latest_snapshot = AsyncMock(return_value=snapshot)
        calculator = CostCalculator(price_repo=mock_repo)

        cost, exactness = await calculator.calculate_cost(
            "normal", input_tokens=1000, output_tokens=1000
        )
        # 3000 + 15000 = 18000 microdollars; 2 microdollars/token total.
        assert cost == 18_000
        assert exactness == "derived"
