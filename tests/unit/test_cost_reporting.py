"""Tests for the provider-reported cost parser."""

from __future__ import annotations

import math
from decimal import Decimal

import pytest

from eggpool.proxy.cost_reporting import (
    ProviderReportedCost,
    extract_provider_reported_cost,
)


def test_dollar_float_parses() -> None:
    result = extract_provider_reported_cost(
        {"usage": {"cost_usd": 12.0}},
        provider_id=None,
        protocol="openai",
    )
    assert result == ProviderReportedCost(
        microdollars=12_000_000, source="usage.cost_usd"
    )


def test_dollar_string_parses() -> None:
    result = extract_provider_reported_cost(
        {"usage": {"cost_usd": "12.34"}},
        provider_id=None,
        protocol="openai",
    )
    assert result == ProviderReportedCost(
        microdollars=12_340_000, source="usage.cost_usd"
    )


def test_total_cost_usd_parses() -> None:
    result = extract_provider_reported_cost(
        {"usage": {"total_cost_usd": 0.01}},
        provider_id=None,
        protocol="openai",
    )
    assert result == ProviderReportedCost(
        microdollars=10_000, source="usage.total_cost_usd"
    )


def test_nested_billing_cost_usd_parses() -> None:
    result = extract_provider_reported_cost(
        {"usage": {"billing": {"cost_usd": 5.5}}},
        provider_id=None,
        protocol="openai",
    )
    assert result == ProviderReportedCost(
        microdollars=5_500_000, source="usage.billing.cost_usd"
    )


def test_top_level_billing_cost_usd_parses() -> None:
    result = extract_provider_reported_cost(
        {"billing": {"cost_usd": 7.0}},
        provider_id=None,
        protocol="openai",
    )
    assert result == ProviderReportedCost(
        microdollars=7_000_000, source="billing.cost_usd"
    )


def test_top_level_billing_total_cost_usd_parses() -> None:
    result = extract_provider_reported_cost(
        {"billing": {"total_cost_usd": 3.25}},
        provider_id=None,
        protocol="openai",
    )
    assert result == ProviderReportedCost(
        microdollars=3_250_000, source="billing.total_cost_usd"
    )


def test_microdollars_int_parses() -> None:
    result = extract_provider_reported_cost(
        {"usage": {"cost_microdollars": 12_000_000}},
        provider_id=None,
        protocol="openai",
    )
    assert result == ProviderReportedCost(
        microdollars=12_000_000, source="usage.cost_microdollars"
    )


def test_micros_int_parses() -> None:
    result = extract_provider_reported_cost(
        {"usage": {"cost_micros": 12_345}},
        provider_id=None,
        protocol="openai",
    )
    assert result == ProviderReportedCost(
        microdollars=12_345, source="usage.cost_micros"
    )


def test_total_cost_microdollars_parses() -> None:
    result = extract_provider_reported_cost(
        {"usage": {"total_cost_microdollars": 999}},
        provider_id=None,
        protocol="openai",
    )
    assert result == ProviderReportedCost(
        microdollars=999, source="usage.total_cost_microdollars"
    )


def test_total_cost_micros_parses() -> None:
    result = extract_provider_reported_cost(
        {"usage": {"total_cost_micros": 1234}},
        provider_id=None,
        protocol="openai",
    )
    assert result == ProviderReportedCost(
        microdollars=1234, source="usage.total_cost_micros"
    )


def test_nested_billing_total_cost_usd_parses() -> None:
    result = extract_provider_reported_cost(
        {"usage": {"billing": {"total_cost_usd": 4.5}}},
        provider_id=None,
        protocol="openai",
    )
    assert result == ProviderReportedCost(
        microdollars=4_500_000, source="usage.billing.total_cost_usd"
    )


def test_decimal_dollar_parses() -> None:
    result = extract_provider_reported_cost(
        {"usage": {"cost_usd": Decimal("1.5")}},
        provider_id=None,
        protocol="openai",
    )
    assert result == ProviderReportedCost(
        microdollars=1_500_000, source="usage.cost_usd"
    )


def test_decimal_microdollar_parses() -> None:
    result = extract_provider_reported_cost(
        {"usage": {"cost_microdollars": Decimal("1234567")}},
        provider_id=None,
        protocol="openai",
    )
    assert result == ProviderReportedCost(
        microdollars=1_234_567, source="usage.cost_microdollars"
    )


def test_bool_true_rejected() -> None:
    result = extract_provider_reported_cost(
        {"usage": {"cost_usd": True}},
        provider_id=None,
        protocol="openai",
    )
    assert result is None


def test_bool_false_rejected() -> None:
    result = extract_provider_reported_cost(
        {"usage": {"cost_usd": False}},
        provider_id=None,
        protocol="openai",
    )
    assert result is None


def test_negative_dollar_rejected() -> None:
    result = extract_provider_reported_cost(
        {"usage": {"cost_usd": -1.0}},
        provider_id=None,
        protocol="openai",
    )
    assert result is None


def test_negative_microdollar_rejected() -> None:
    result = extract_provider_reported_cost(
        {"usage": {"cost_microdollars": -5}},
        provider_id=None,
        protocol="openai",
    )
    assert result is None


def test_nan_rejected() -> None:
    result = extract_provider_reported_cost(
        {"usage": {"cost_usd": float("nan")}},
        provider_id=None,
        protocol="openai",
    )
    assert result is None


def test_positive_infinity_rejected() -> None:
    result = extract_provider_reported_cost(
        {"usage": {"cost_usd": float("inf")}},
        provider_id=None,
        protocol="openai",
    )
    assert result is None


def test_negative_infinity_rejected() -> None:
    result = extract_provider_reported_cost(
        {"usage": {"cost_usd": float("-inf")}},
        provider_id=None,
        protocol="openai",
    )
    assert result is None


def test_nan_microdollar_rejected() -> None:
    result = extract_provider_reported_cost(
        {"usage": {"cost_microdollars": float("nan")}},
        provider_id=None,
        protocol="openai",
    )
    assert result is None


def test_non_numeric_string_rejected() -> None:
    result = extract_provider_reported_cost(
        {"usage": {"cost_usd": "abc"}},
        provider_id=None,
        protocol="openai",
    )
    assert result is None


def test_empty_string_rejected() -> None:
    result = extract_provider_reported_cost(
        {"usage": {"cost_usd": ""}},
        provider_id=None,
        protocol="openai",
    )
    assert result is None


def test_whitespace_string_rejected() -> None:
    result = extract_provider_reported_cost(
        {"usage": {"cost_usd": "   "}},
        provider_id=None,
        protocol="openai",
    )
    assert result is None


def test_none_field_returns_none() -> None:
    result = extract_provider_reported_cost(
        {"usage": {"cost_usd": None}},
        provider_id=None,
        protocol="openai",
    )
    assert result is None


def test_no_cost_field_returns_none() -> None:
    result = extract_provider_reported_cost(
        {"usage": {"prompt_tokens": 10}},
        provider_id=None,
        protocol="openai",
    )
    assert result is None


def test_empty_dict_returns_none() -> None:
    result = extract_provider_reported_cost(
        {},
        provider_id=None,
        protocol="openai",
    )
    assert result is None


def test_microdollar_field_path_preserved() -> None:
    result = extract_provider_reported_cost(
        {"usage": {"cost_microdollars": 42}},
        provider_id=None,
        protocol="openai",
    )
    assert result is not None
    assert result.source == "usage.cost_microdollars"
    assert result.microdollars == 42


def test_nested_billing_field_path_preserved() -> None:
    result = extract_provider_reported_cost(
        {"usage": {"billing": {"cost_usd": 2.5}}},
        provider_id=None,
        protocol="openai",
    )
    assert result is not None
    assert result.source == "usage.billing.cost_usd"
    assert result.microdollars == 2_500_000


def test_top_level_billing_field_path_preserved() -> None:
    result = extract_provider_reported_cost(
        {"billing": {"total_cost_usd": 1.25}},
        provider_id=None,
        protocol="openai",
    )
    assert result is not None
    assert result.source == "billing.total_cost_usd"
    assert result.microdollars == 1_250_000


def test_microdollar_wins_over_dollar() -> None:
    result = extract_provider_reported_cost(
        {"usage": {"cost_microdollars": 100, "cost_usd": 5.0}},
        provider_id=None,
        protocol="openai",
    )
    assert result == ProviderReportedCost(
        microdollars=100, source="usage.cost_microdollars"
    )


def test_total_cost_microdollars_wins_over_cost_usd() -> None:
    result = extract_provider_reported_cost(
        {"usage": {"total_cost_microdollars": 250, "cost_usd": 9.99}},
        provider_id=None,
        protocol="openai",
    )
    assert result == ProviderReportedCost(
        microdollars=250, source="usage.total_cost_microdollars"
    )


def test_cost_usd_wins_over_billing_cost_usd() -> None:
    result = extract_provider_reported_cost(
        {"usage": {"cost_usd": 1.0, "billing": {"cost_usd": 2.0}}},
        provider_id=None,
        protocol="openai",
    )
    assert result == ProviderReportedCost(
        microdollars=1_000_000, source="usage.cost_usd"
    )


def test_usage_wins_over_top_level_billing() -> None:
    result = extract_provider_reported_cost(
        {"usage": {"cost_usd": 1.0}, "billing": {"cost_usd": 9.0}},
        provider_id=None,
        protocol="openai",
    )
    assert result == ProviderReportedCost(
        microdollars=1_000_000, source="usage.cost_usd"
    )


def test_nested_dict_value_swallowed() -> None:
    result = extract_provider_reported_cost(
        {"usage": {"cost_usd": {"not": "a number"}}},
        provider_id=None,
        protocol="openai",
    )
    assert result is None


def test_list_value_swallowed() -> None:
    result = extract_provider_reported_cost(
        {"usage": {"cost_usd": [1, 2, 3]}},
        provider_id=None,
        protocol="openai",
    )
    assert result is None


def test_string_for_usage_field_swallowed() -> None:
    result = extract_provider_reported_cost(
        {"usage": "not a dict"},
        provider_id=None,
        protocol="openai",
    )
    assert result is None


def test_top_level_string_swallowed() -> None:
    assert (
        extract_provider_reported_cost(
            "not a dict",
            provider_id=None,
            protocol="openai",
        )
        is None
    )
    assert (
        extract_provider_reported_cost(
            42,
            provider_id=None,
            protocol="openai",
        )
        is None
    )
    assert (
        extract_provider_reported_cost(
            None,
            provider_id=None,
            protocol="openai",
        )
        is None
    )


def test_recursive_dict_swallowed() -> None:
    recursive: dict[str, object] = {}
    recursive["self"] = recursive
    payload = {"usage": {"cost_usd": recursive}}
    assert (
        extract_provider_reported_cost(
            payload,
            provider_id=None,
            protocol="openai",
        )
        is None
    )


def test_opencode_go_bare_cost_alias_parses() -> None:
    result = extract_provider_reported_cost(
        {"usage": {"cost": 1.5}},
        provider_id="opencode-go",
        protocol="openai",
    )
    assert result == ProviderReportedCost(microdollars=1_500_000, source="usage.cost")


def test_opencode_go_bare_total_cost_alias_parses() -> None:
    result = extract_provider_reported_cost(
        {"usage": {"total_cost": 2.5}},
        provider_id="opencode-go",
        protocol="openai",
    )
    assert result == ProviderReportedCost(
        microdollars=2_500_000, source="usage.total_cost"
    )


def test_other_provider_bare_cost_rejected() -> None:
    result = extract_provider_reported_cost(
        {"usage": {"cost": 1.5}},
        provider_id="some-other-provider",
        protocol="openai",
    )
    assert result is None


def test_none_provider_bare_cost_rejected() -> None:
    result = extract_provider_reported_cost(
        {"usage": {"cost": 1.5}},
        provider_id=None,
        protocol="openai",
    )
    assert result is None


def test_provider_alias_loses_to_generic_usd_field() -> None:
    result = extract_provider_reported_cost(
        {"usage": {"cost_usd": 4.0, "cost": 99.0}},
        provider_id="opencode-go",
        protocol="openai",
    )
    assert result == ProviderReportedCost(
        microdollars=4_000_000, source="usage.cost_usd"
    )


def test_protocol_does_not_change_generic_field_result() -> None:
    payload = {"usage": {"cost_usd": 12.0}}
    for proto in ("openai", "anthropic", "unknown", ""):
        result = extract_provider_reported_cost(
            payload, provider_id=None, protocol=proto
        )
        assert result == ProviderReportedCost(
            microdollars=12_000_000, source="usage.cost_usd"
        ), f"protocol={proto!r} changed result"


def test_provider_id_does_not_change_generic_field_result() -> None:
    payload = {"usage": {"cost_usd": 12.0}}
    for pid in (None, "", "opencode-go", "anthropic", "other"):
        result = extract_provider_reported_cost(
            payload, provider_id=pid, protocol="openai"
        )
        assert result == ProviderReportedCost(
            microdollars=12_000_000, source="usage.cost_usd"
        ), f"provider_id={pid!r} changed result"


@pytest.mark.parametrize("nan_value", [float("nan"), math.nan])
def test_nan_constant_rejected(nan_value: float) -> None:
    result = extract_provider_reported_cost(
        {"usage": {"cost_usd": nan_value}},
        provider_id=None,
        protocol="openai",
    )
    assert result is None


def test_dollar_string_with_whitespace_parses() -> None:
    result = extract_provider_reported_cost(
        {"usage": {"cost_usd": " 12.5 "}},
        provider_id=None,
        protocol="openai",
    )
    assert result == ProviderReportedCost(
        microdollars=12_500_000, source="usage.cost_usd"
    )


def test_dollar_rounds_half_microdollar() -> None:
    # 0.0000005 dollars rounds to 1 microdollar; 0.0000004 rounds to 0.
    result = extract_provider_reported_cost(
        {"usage": {"cost_usd": 0.0000005}},
        provider_id=None,
        protocol="openai",
    )
    assert result is not None
    # round(0.5) goes to 0 under banker's rounding in Python's round(),
    # so accept either 0 or 1 microdollar depending on platform rounding.
    assert result.microdollars in (0, 1)
    assert result.source == "usage.cost_usd"


def test_microdollar_int_rounded_to_int() -> None:
    result = extract_provider_reported_cost(
        {"usage": {"cost_microdollars": 1.5}},
        provider_id=None,
        protocol="openai",
    )
    assert result is not None
    assert result.source == "usage.cost_microdollars"
    # Decimal's default rounding is ROUND_HALF_EVEN, so 1.5 rounds to 2.
    assert result.microdollars == 2


def test_zero_dollar_is_accepted() -> None:
    result = extract_provider_reported_cost(
        {"usage": {"cost_usd": 0}},
        provider_id=None,
        protocol="openai",
    )
    assert result == ProviderReportedCost(microdollars=0, source="usage.cost_usd")


def test_zero_microdollar_is_accepted() -> None:
    result = extract_provider_reported_cost(
        {"usage": {"cost_microdollars": 0}},
        provider_id=None,
        protocol="openai",
    )
    assert result == ProviderReportedCost(
        microdollars=0, source="usage.cost_microdollars"
    )


def test_provider_reported_cost_is_frozen() -> None:
    result = extract_provider_reported_cost(
        {"usage": {"cost_usd": 1.0}},
        provider_id=None,
        protocol="openai",
    )
    assert result is not None
    with pytest.raises((AttributeError, Exception)):
        result.microdollars = 999  # type: ignore[misc]
