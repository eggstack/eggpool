"""Tests for shared constants and accounting helpers."""

from __future__ import annotations

from decimal import Decimal

from eggpool.constants import SQLITE_INTEGER_MAX, clamp_sqlite_aggregate


def test_clamp_sqlite_aggregate_preserves_large_integer_precision() -> None:
    value = 2**53 + 1

    assert clamp_sqlite_aggregate(value) == value


def test_clamp_sqlite_aggregate_handles_decimal_without_float_rounding() -> None:
    value = Decimal("9007199254740993")

    assert clamp_sqlite_aggregate(value) == 9007199254740993
    assert clamp_sqlite_aggregate(Decimal("1e100")) == SQLITE_INTEGER_MAX
