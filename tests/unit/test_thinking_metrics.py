"""Tests for thinking/reasoning observability metrics."""

from __future__ import annotations

import pytest
import pytest_asyncio

from eggpool.metrics.thinking import (
    ThinkingMetricEvent,
    ThinkingMetricsCounter,
    get_counter,
    record_thinking_event,
)


@pytest_asyncio.fixture(autouse=True)
async def reset_counter():
    """Reset the singleton counter between tests."""
    counter = get_counter()
    yield counter
    await counter.reset()


# ---------------------------------------------------------------------------
# ThinkingMetricsCounter basics
# ---------------------------------------------------------------------------


class TestThinkingMetricsCounter:
    @pytest.mark.asyncio
    async def test_increment_requested(self):
        counter = ThinkingMetricsCounter()
        await counter.increment_requested(client_protocol="openai")
        snapshot = await counter.snapshot()
        assert snapshot["total"] == 1
        assert "requested|openai" in snapshot["counters"]

    @pytest.mark.asyncio
    async def test_increment_transcoded(self):
        counter = ThinkingMetricsCounter()
        await counter.increment_transcoded(
            client_protocol="openai",
            upstream_protocol="anthropic",
            provider_id="anthropic-prod",
        )
        snapshot = await counter.snapshot()
        assert snapshot["total"] == 1
        key = "transcoded|openai|anthropic|anthropic-prod"
        assert snapshot["counters"][key] == 1

    @pytest.mark.asyncio
    async def test_increment_rejected(self):
        counter = ThinkingMetricsCounter()
        await counter.increment_rejected(
            client_protocol="openai",
            capability_status="unsupported",
        )
        snapshot = await counter.snapshot()
        assert snapshot["total"] == 1
        assert "rejected|openai|unsupported" in snapshot["counters"]

    @pytest.mark.asyncio
    async def test_increment_dropped(self):
        counter = ThinkingMetricsCounter()
        await counter.increment_dropped(
            client_protocol="anthropic",
            upstream_protocol="openai",
            reason="reasoning_content_dropped",
        )
        snapshot = await counter.snapshot()
        assert snapshot["total"] == 1
        key = "dropped|anthropic|openai|reasoning_content_dropped"
        assert snapshot["counters"][key] == 1

    @pytest.mark.asyncio
    async def test_increment_budget_clamped(self):
        counter = ThinkingMetricsCounter()
        await counter.increment_budget_clamped(
            client_protocol="openai",
            provider_id="openai",
        )
        snapshot = await counter.snapshot()
        assert snapshot["total"] == 1
        key = "budget_clamped|openai|openai"
        assert snapshot["counters"][key] == 1

    @pytest.mark.asyncio
    async def test_increment_stream_delta(self):
        counter = ThinkingMetricsCounter()
        await counter.increment_stream_delta(
            client_protocol="openai",
            upstream_protocol="anthropic",
        )
        snapshot = await counter.snapshot()
        assert snapshot["total"] == 1
        key = "stream_delta|openai|anthropic"
        assert snapshot["counters"][key] == 1

    @pytest.mark.asyncio
    async def test_increment_response_block(self):
        counter = ThinkingMetricsCounter()
        await counter.increment_response_block(
            client_protocol="openai",
            upstream_protocol="anthropic",
        )
        snapshot = await counter.snapshot()
        assert snapshot["total"] == 1
        key = "response_block|openai|anthropic"
        assert snapshot["counters"][key] == 1

    @pytest.mark.asyncio
    async def test_increment_unknown_capability(self):
        counter = ThinkingMetricsCounter()
        await counter.increment_unknown_capability(client_protocol="openai")
        snapshot = await counter.snapshot()
        assert snapshot["total"] == 1
        key = "unknown_capability|openai"
        assert snapshot["counters"][key] == 1

    @pytest.mark.asyncio
    async def test_increment_unsupported_capability(self):
        counter = ThinkingMetricsCounter()
        await counter.increment_unsupported_capability(client_protocol="openai")
        snapshot = await counter.snapshot()
        assert snapshot["total"] == 1
        key = "unsupported_capability|openai"
        assert snapshot["counters"][key] == 1

    @pytest.mark.asyncio
    async def test_label_breakdown(self):
        counter = ThinkingMetricsCounter()
        await counter.increment_requested(client_protocol="openai")
        await counter.increment_requested(client_protocol="anthropic")
        await counter.increment_transcoded(
            client_protocol="openai",
            upstream_protocol="anthropic",
            provider_id="anthropic-prod",
        )
        snapshot = await counter.snapshot()
        assert "requested" in snapshot["label_breakdown"]
        assert "transcoded" in snapshot["label_breakdown"]
        assert snapshot["label_breakdown"]["requested"]["requested|openai"] == 1
        assert snapshot["label_breakdown"]["requested"]["requested|anthropic"] == 1

    @pytest.mark.asyncio
    async def test_reset(self):
        counter = ThinkingMetricsCounter()
        await counter.increment_requested(client_protocol="openai")
        await counter.reset()
        snapshot = await counter.snapshot()
        assert snapshot["total"] == 0
        assert snapshot["counters"] == {}

    @pytest.mark.asyncio
    async def test_cumulative_totals(self):
        counter = ThinkingMetricsCounter()
        await counter.increment_requested(client_protocol="openai")
        await counter.increment_requested(client_protocol="openai")
        await counter.increment_requested(client_protocol="anthropic")
        snapshot = await counter.snapshot()
        assert snapshot["total"] == 3
        assert snapshot["counters"]["requested|openai"] == 2
        assert snapshot["counters"]["requested|anthropic"] == 1


# ---------------------------------------------------------------------------
# record_thinking_event convenience wrapper
# ---------------------------------------------------------------------------


class TestRecordThinkingEvent:
    @pytest.mark.asyncio
    async def test_requested_event(self):
        event = ThinkingMetricEvent(
            requested=True,
            client_protocol="openai",
            request_fields=["reasoning_effort"],
            requested_effort="medium",
            resolved_budget_tokens=None,
            budget_clamped=False,
            capability_status=None,
            capability_source=None,
            upstream_protocol=None,
            upstream_fields=[],
            decision="none",
        )
        await record_thinking_event(event)
        counter = get_counter()
        snapshot = await counter.snapshot()
        assert snapshot["total"] == 1
        assert "requested|openai" in snapshot["counters"]

    @pytest.mark.asyncio
    async def test_transcoded_event(self):
        event = ThinkingMetricEvent(
            requested=True,
            client_protocol="openai",
            request_fields=["reasoning_effort"],
            requested_effort="medium",
            resolved_budget_tokens=4096,
            budget_clamped=False,
            capability_status="supported",
            capability_source="provider_catalog",
            upstream_protocol="anthropic",
            upstream_fields=["thinking"],
            decision="transcoded",
        )
        await record_thinking_event(event)
        counter = get_counter()
        snapshot = await counter.snapshot()
        # requested + transcoded
        assert snapshot["total"] == 2

    @pytest.mark.asyncio
    async def test_rejected_event(self):
        event = ThinkingMetricEvent(
            requested=True,
            client_protocol="openai",
            request_fields=["reasoning_effort"],
            requested_effort="high",
            resolved_budget_tokens=None,
            budget_clamped=False,
            capability_status="unsupported",
            capability_source="provider_catalog",
            upstream_protocol=None,
            upstream_fields=[],
            decision="rejected",
        )
        await record_thinking_event(event)
        counter = get_counter()
        snapshot = await counter.snapshot()
        # requested + rejected
        assert snapshot["total"] == 2

    @pytest.mark.asyncio
    async def test_clamped_event(self):
        event = ThinkingMetricEvent(
            requested=True,
            client_protocol="openai",
            request_fields=["reasoning_effort"],
            requested_effort="high",
            resolved_budget_tokens=16384,
            budget_clamped=True,
            capability_status="supported",
            capability_source="provider_catalog",
            upstream_protocol="anthropic",
            upstream_fields=["thinking"],
            decision="clamped",
        )
        await record_thinking_event(event)
        counter = get_counter()
        snapshot = await counter.snapshot()
        # requested + budget_clamped (clamped decision is a no-op)
        assert snapshot["total"] == 2

    @pytest.mark.asyncio
    async def test_not_requested_event(self):
        event = ThinkingMetricEvent(
            requested=False,
            client_protocol="openai",
            request_fields=[],
            requested_effort=None,
            resolved_budget_tokens=None,
            budget_clamped=False,
            capability_status=None,
            capability_source=None,
            upstream_protocol="openai",
            upstream_fields=[],
            decision="none",
        )
        await record_thinking_event(event)
        counter = get_counter()
        snapshot = await counter.snapshot()
        assert snapshot["total"] == 0

    @pytest.mark.asyncio
    async def test_dropped_event(self):
        event = ThinkingMetricEvent(
            requested=True,
            client_protocol="anthropic",
            request_fields=["thinking"],
            requested_effort=None,
            resolved_budget_tokens=None,
            budget_clamped=False,
            capability_status="mixed",
            capability_source="provider_catalog",
            upstream_protocol="openai",
            upstream_fields=[],
            decision="dropped",
        )
        await record_thinking_event(event)
        counter = get_counter()
        snapshot = await counter.snapshot()
        # requested + dropped
        assert snapshot["total"] == 2

    @pytest.mark.asyncio
    async def test_unknown_capability_event(self):
        event = ThinkingMetricEvent(
            requested=True,
            client_protocol="openai",
            request_fields=["reasoning_effort"],
            requested_effort="low",
            resolved_budget_tokens=None,
            budget_clamped=False,
            capability_status="unknown",
            capability_source="none",
            upstream_protocol=None,
            upstream_fields=[],
            decision="unknown_capability",
        )
        await record_thinking_event(event)
        counter = get_counter()
        snapshot = await counter.snapshot()
        # requested + unknown_capability
        assert snapshot["total"] == 2

    @pytest.mark.asyncio
    async def test_unsupported_capability_event(self):
        event = ThinkingMetricEvent(
            requested=True,
            client_protocol="openai",
            request_fields=["reasoning_effort"],
            requested_effort="medium",
            resolved_budget_tokens=None,
            budget_clamped=False,
            capability_status="unsupported",
            capability_source="provider_catalog",
            upstream_protocol=None,
            upstream_fields=[],
            decision="unsupported_capability",
        )
        await record_thinking_event(event)
        counter = get_counter()
        snapshot = await counter.snapshot()
        # requested + unsupported_capability
        assert snapshot["total"] == 2

    @pytest.mark.asyncio
    async def test_passthrough_decision_no_increment(self):
        event = ThinkingMetricEvent(
            requested=True,
            client_protocol="openai",
            request_fields=["reasoning_effort"],
            requested_effort="medium",
            resolved_budget_tokens=None,
            budget_clamped=False,
            capability_status="supported",
            capability_source="provider_catalog",
            upstream_protocol="openai",
            upstream_fields=[],
            decision="passthrough",
        )
        await record_thinking_event(event)
        counter = get_counter()
        snapshot = await counter.snapshot()
        # requested only (passthrough is a no-op)
        assert snapshot["total"] == 1


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


class TestGetCounterSingleton:
    def test_singleton(self):
        c1 = get_counter()
        c2 = get_counter()
        assert c1 is c2
