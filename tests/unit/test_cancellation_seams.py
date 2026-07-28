"""Cancellation seam unit tests.

Validates that the ``CancellationSeamRegistry`` fires exactly once at
the named point, is deterministic, and supports reset.
"""

from __future__ import annotations

import asyncio

import pytest

from tests.support.cancellation_seams import (
    CancellationPoint,
    CancellationSeamRegistry,
)

pytestmark = pytest.mark.asyncio


class TestCancellationSeamRegistry:
    def test_check_without_activation_does_not_raise(self) -> None:
        reg = CancellationSeamRegistry()
        reg.check(CancellationPoint.BEFORE_REQUEST_ROW)
        assert not reg.was_triggered(CancellationPoint.BEFORE_REQUEST_ROW)

    def test_check_with_activation_raises(self) -> None:
        reg = CancellationSeamRegistry()
        reg.activate(CancellationPoint.BEFORE_REQUEST_ROW)
        with pytest.raises(asyncio.CancelledError):
            reg.check(CancellationPoint.BEFORE_REQUEST_ROW)
        assert reg.was_triggered(CancellationPoint.BEFORE_REQUEST_ROW)

    def test_check_fires_exactly_once(self) -> None:
        reg = CancellationSeamRegistry()
        reg.activate(CancellationPoint.AFTER_DISPATCH_COMMIT)
        with pytest.raises(asyncio.CancelledError):
            reg.check(CancellationPoint.AFTER_DISPATCH_COMMIT)
        # Second check should NOT raise (seam is inert)
        reg.check(CancellationPoint.AFTER_DISPATCH_COMMIT)
        assert reg.call_count(CancellationPoint.AFTER_DISPATCH_COMMIT) == 2

    def test_deactivate_prevents_firing(self) -> None:
        reg = CancellationSeamRegistry()
        reg.activate(CancellationPoint.DURING_FINALIZATION_TRANSACTION)
        reg.deactivate(CancellationPoint.DURING_FINALIZATION_TRANSACTION)
        reg.check(CancellationPoint.DURING_FINALIZATION_TRANSACTION)
        assert not reg.was_triggered(CancellationPoint.DURING_FINALIZATION_TRANSACTION)

    def test_deactivate_all(self) -> None:
        reg = CancellationSeamRegistry()
        reg.activate(CancellationPoint.BEFORE_UPSTREAM_SEND)
        reg.activate(CancellationPoint.AFTER_UPSTREAM_HEADERS)
        reg.deactivate_all()
        reg.check(CancellationPoint.BEFORE_UPSTREAM_SEND)
        reg.check(CancellationPoint.AFTER_UPSTREAM_HEADERS)
        assert not reg.any_triggered()

    def test_any_triggered(self) -> None:
        reg = CancellationSeamRegistry()
        assert not reg.any_triggered()
        reg.activate(CancellationPoint.MIDSTREAM_AFTER_CHUNK)
        with pytest.raises(asyncio.CancelledError):
            reg.check(CancellationPoint.MIDSTREAM_AFTER_CHUNK)
        assert reg.any_triggered()

    def test_reset_clears_all_state(self) -> None:
        reg = CancellationSeamRegistry()
        reg.activate(CancellationPoint.AFTER_FINALIZATION_BEFORE_RELEASE)
        with pytest.raises(asyncio.CancelledError):
            reg.check(CancellationPoint.AFTER_FINALIZATION_BEFORE_RELEASE)
        reg.reset()
        assert not reg.any_triggered()
        assert reg.call_count(CancellationPoint.AFTER_FINALIZATION_BEFORE_RELEASE) == 0

    def test_call_count_tracking(self) -> None:
        reg = CancellationSeamRegistry()
        reg.check(CancellationPoint.BEFORE_REQUEST_ROW)
        reg.check(CancellationPoint.BEFORE_REQUEST_ROW)
        reg.check(CancellationPoint.BEFORE_REQUEST_ROW)
        assert reg.call_count(CancellationPoint.BEFORE_REQUEST_ROW) == 3

    def test_summary(self) -> None:
        reg = CancellationSeamRegistry()
        reg.activate(CancellationPoint.AFTER_SELECTION_CLAIM)
        with pytest.raises(asyncio.CancelledError):
            reg.check(CancellationPoint.AFTER_SELECTION_CLAIM)
        summary = reg.summary()
        assert "triggered" in summary
        assert CancellationPoint.AFTER_SELECTION_CLAIM in summary["triggered"]

    def test_all_cancellation_points_are_valid(self) -> None:
        """Ensure every CancellationPoint value can be activated and checked."""
        reg = CancellationSeamRegistry()
        for point in CancellationPoint:
            reg.activate(point)
        # All should be active
        for point in CancellationPoint:
            with pytest.raises(asyncio.CancelledError):
                reg.check(point)
        assert reg.any_triggered()

    def test_string_based_activation(self) -> None:
        """Seams can be activated by raw string as well as enum."""
        reg = CancellationSeamRegistry()
        reg.activate("before_request_row")
        with pytest.raises(asyncio.CancelledError):
            reg.check(CancellationPoint.BEFORE_REQUEST_ROW)
        assert reg.was_triggered("before_request_row")
