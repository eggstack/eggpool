"""Unit tests for src/eggpool/stats/cache_metrics.py."""

from __future__ import annotations

import pytest

from eggpool.proxy.normalized_usage import CacheCounterStatus
from eggpool.stats.cache_metrics import (
    ProtocolShape,
    aggregate_cache_terms,
    derive_cache_metric_terms,
)

pytestmark = pytest.mark.dashboard


class TestDeriveCacheMetricTerms:
    """derive_cache_metric_terms maps protocol-aware read/write/denominator."""

    def test_openai_basic(self) -> None:
        """OpenAI: read=cached_tokens, write=cache_write, eligible=prompt_tokens."""
        terms = derive_cache_metric_terms(
            input_tokens=100,
            cache_read_tokens=20,
            cache_write_tokens=5,
            protocol="openai",
        )
        assert terms.cache_read_tokens == 20
        assert terms.cache_write_tokens == 5
        assert terms.cache_eligible_input_tokens == 100
        assert terms.protocol_shape == ProtocolShape.OPENAI
        assert terms.cache_read_clamped is False
        assert terms.cache_write_clamped is False

    def test_anthropic_basic(self) -> None:
        """Anthropic: eligible = input + read + creation."""
        terms = derive_cache_metric_terms(
            input_tokens=80,
            cache_read_tokens=20,
            cache_write_tokens=5,
            protocol="anthropic",
        )
        assert terms.cache_read_tokens == 20
        assert terms.cache_write_tokens == 5
        assert terms.cache_eligible_input_tokens == 105
        assert terms.protocol_shape == ProtocolShape.ANTHROPIC

    def test_unknown_with_granular_fields(self) -> None:
        """Unknown protocol with granular fields: eligible = read + write."""
        terms = derive_cache_metric_terms(
            input_tokens=0,
            cache_read_tokens=10,
            cache_write_tokens=3,
            protocol="unknown",
        )
        assert terms.cache_read_tokens == 10
        assert terms.cache_write_tokens == 3
        assert terms.cache_eligible_input_tokens == 13
        assert terms.protocol_shape == ProtocolShape.UNKNOWN

    def test_openai_clamp_read(self) -> None:
        """OpenAI: read clamped to input when read > input."""
        terms = derive_cache_metric_terms(
            input_tokens=10,
            cache_read_tokens=20,
            cache_write_tokens=5,
            protocol="openai",
        )
        assert terms.cache_read_tokens == 10
        assert terms.cache_read_clamped is True
        assert terms.cache_write_tokens == 5
        assert terms.cache_write_clamped is False

    def test_none_input(self) -> None:
        """None input: eligible=0 with no clamping flags."""
        terms = derive_cache_metric_terms(
            input_tokens=None,
            cache_read_tokens=10,
            cache_write_tokens=5,
            protocol="openai",
        )
        assert terms.cache_read_tokens == 10
        assert terms.cache_write_tokens == 5
        assert terms.cache_eligible_input_tokens == 0
        assert terms.cache_read_clamped is False
        assert terms.cache_write_clamped is False

    def test_none_tokens(self) -> None:
        """None read/write tokens treated as zero."""
        terms = derive_cache_metric_terms(
            input_tokens=100,
            cache_read_tokens=None,
            cache_write_tokens=None,
            protocol="openai",
        )
        assert terms.cache_read_tokens == 0
        assert terms.cache_write_tokens == 0
        assert terms.cache_eligible_input_tokens == 100

    def test_anthropic_none_input_has_tokens(self) -> None:
        """Anthropic with None input but granular tokens: eligible = read + write."""
        terms = derive_cache_metric_terms(
            input_tokens=None,
            cache_read_tokens=20,
            cache_write_tokens=5,
            protocol="anthropic",
        )
        assert terms.cache_eligible_input_tokens == 25

    def test_negative_tokens_clamped_to_zero(self) -> None:
        """Negative tokens are clamped to zero."""
        terms = derive_cache_metric_terms(
            input_tokens=100,
            cache_read_tokens=-5,
            cache_write_tokens=-3,
            protocol="openai",
        )
        assert terms.cache_read_tokens == 0
        assert terms.cache_write_tokens == 0
        assert terms.cache_eligible_input_tokens == 100

    def test_none_protocol_maps_to_unknown(self) -> None:
        """None protocol maps to UNKNOWN shape."""
        terms = derive_cache_metric_terms(
            input_tokens=100,
            cache_read_tokens=10,
            cache_write_tokens=5,
            protocol=None,
        )
        assert terms.protocol_shape == ProtocolShape.UNKNOWN


class TestAggregateCacheTerms:
    """aggregate_cache_terms computes totals and rates from mixed rows."""

    def test_three_mixed_rows(self) -> None:
        """Mixed OpenAI + Anthropic + unknown rows produce correct totals."""
        rows = [
            derive_cache_metric_terms(
                input_tokens=100,
                cache_read_tokens=20,
                cache_write_tokens=5,
                protocol="openai",
            ),
            derive_cache_metric_terms(
                input_tokens=80,
                cache_read_tokens=20,
                cache_write_tokens=5,
                protocol="anthropic",
            ),
            derive_cache_metric_terms(
                input_tokens=0,
                cache_read_tokens=10,
                cache_write_tokens=3,
                protocol="unknown",
            ),
        ]
        statuses = [
            CacheCounterStatus.REPORTED,
            CacheCounterStatus.REPORTED,
            CacheCounterStatus.NOT_REPORTED,
        ]
        agg = aggregate_cache_terms(rows, statuses)
        # reported rows: openai(20+5+100) + anthropic(20+5+105) = 25+130 eligible
        # read = 20+20 = 40, write = 5+5 = 10, eligible = 100+105 = 205
        assert agg.cache_read_tokens_canonical == 40
        assert agg.cache_write_tokens_canonical == 10
        assert agg.cache_eligible_input_tokens == 205
        assert agg.cache_counter_reported_requests == 2
        assert agg.cache_counter_not_reported_requests == 1
        assert agg.cache_counter_unknown_requests == 0
        assert agg.cache_benefited_requests == 2
        assert agg.cache_eligible_requests == 2
        assert agg.provider_cache_hit_rate == pytest.approx(40 / 205)
        assert agg.cache_write_rate == pytest.approx(10 / 205)
        assert agg.cache_benefited_request_rate == pytest.approx(1.0)
        assert agg.cache_counter_coverage_rate == pytest.approx(2 / 3)

    def test_all_not_reported(self) -> None:
        """All not-reported rows: rates are None, counts correct."""
        rows = [
            derive_cache_metric_terms(
                input_tokens=100,
                cache_read_tokens=None,
                cache_write_tokens=None,
                protocol="openai",
            ),
        ]
        statuses = [CacheCounterStatus.NOT_REPORTED]
        agg = aggregate_cache_terms(rows, statuses)
        assert agg.provider_cache_hit_rate is None
        assert agg.cache_write_rate is None
        assert agg.cache_benefited_request_rate is None
        assert agg.cache_counter_reported_requests == 0
        assert agg.cache_counter_not_reported_requests == 1

    def test_empty_rows(self) -> None:
        """Empty input: all counts zero, rates None."""
        agg = aggregate_cache_terms([], [])
        assert agg.cache_read_tokens_canonical == 0
        assert agg.provider_cache_hit_rate is None
        assert agg.cache_counter_coverage_rate is None

    def test_zero_eligible_denominator(self) -> None:
        """Reported row with zero eligible: rate is None."""
        rows = [
            derive_cache_metric_terms(
                input_tokens=0,
                cache_read_tokens=None,
                cache_write_tokens=None,
                protocol="openai",
            ),
        ]
        statuses = [CacheCounterStatus.REPORTED]
        agg = aggregate_cache_terms(rows, statuses)
        assert agg.provider_cache_hit_rate is None
        assert agg.cache_eligible_input_tokens == 0

    def test_inconsistent_row_non_openai(self) -> None:
        """Non-openai row where read > eligible counted as inconsistent."""
        rows = [
            derive_cache_metric_terms(
                input_tokens=0,
                cache_read_tokens=20,
                cache_write_tokens=5,
                protocol="unknown",
            ),
        ]
        statuses = [CacheCounterStatus.REPORTED]
        agg = aggregate_cache_terms(rows, statuses)
        # eligible = read + write = 25, read = 20, not inconsistent
        assert agg.inconsistent_cache_counter_rows == 0
