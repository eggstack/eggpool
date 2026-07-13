"""Unit tests for period-aware bucket selection."""

from __future__ import annotations

from eggpool.dashboard.timeseries_buckets import (
    AUTO_BUCKET,
    VALID_BUCKETS_WITH_AUTO,
    default_bucket_for_period,
    resolve_bucket,
)


class TestDefaultBucketForPeriod:
    """``default_bucket_for_period`` maps each period preset to a default bucket."""

    def test_one_hour_stays_hourly(self) -> None:
        assert default_bucket_for_period("1h") == "hour"

    def test_twenty_four_hours_stays_hourly(self) -> None:
        assert default_bucket_for_period("24h") == "hour"

    def test_seven_days_stays_hourly(self) -> None:
        assert default_bucket_for_period("7d") == "hour"

    def test_thirty_days_flips_to_daily(self) -> None:
        assert default_bucket_for_period("30d") == "day"

    def test_unknown_period_falls_back_to_hour(self) -> None:
        assert default_bucket_for_period("garbage") == "hour"

    def test_none_period_falls_back_to_hour(self) -> None:
        assert default_bucket_for_period(None) == "hour"

    def test_empty_period_falls_back_to_hour(self) -> None:
        assert default_bucket_for_period("") == "hour"


class TestResolveBucket:
    """``resolve_bucket`` is the route-level front door for bucket params."""

    def test_auto_resolves_via_period(self) -> None:
        assert resolve_bucket(AUTO_BUCKET, "30d") == "day"

    def test_empty_resolves_via_period(self) -> None:
        assert resolve_bucket("", "30d") == "day"

    def test_none_resolves_via_period(self) -> None:
        assert resolve_bucket(None, "7d") == "hour"

    def test_explicit_hour_wins_over_auto(self) -> None:
        assert resolve_bucket("hour", "30d") == "hour"

    def test_explicit_day_wins_over_auto(self) -> None:
        assert resolve_bucket("day", "24h") == "day"

    def test_unknown_bucket_falls_back_to_hour(self) -> None:
        assert resolve_bucket("bogus", "30d") == "hour"

    def test_auto_with_unknown_period_returns_hour(self) -> None:
        assert resolve_bucket(AUTO_BUCKET, "fugue") == "hour"


class TestValidBucketsWithAuto:
    """The export surface for the route layer includes the auto sentinel."""

    def test_includes_known_buckets(self) -> None:
        assert "hour" in VALID_BUCKETS_WITH_AUTO
        assert "day" in VALID_BUCKETS_WITH_AUTO

    def test_includes_auto(self) -> None:
        assert AUTO_BUCKET in VALID_BUCKETS_WITH_AUTO
