"""Tests for Plan 024 — Thinking metrics provider control counters."""

from __future__ import annotations

import pytest

from eggpool.metrics.thinking import (
    ThinkingMetricEvent,
    get_counter,
    record_thinking_event,
)


@pytest.fixture
async def _reset_counter() -> None:  # type: ignore[misc]
    counter = get_counter()
    await counter.reset()
    yield
    await counter.reset()


class TestProviderControlCounters:
    """Tests for provider_mapped/provider_dropped/provider_rejected counters."""

    @pytest.mark.asyncio
    async def test_increment_provider_mapped(self, _reset_counter: None) -> None:
        counter = get_counter()
        await counter.increment_provider_mapped(
            client_protocol="openai",
            provider_id="test-provider",
            model_id="test-model",
        )
        snap = await counter.snapshot()
        assert snap["total"] == 1
        key = "provider_mapped|openai|test-provider|test-model"
        assert snap["counters"][key] == 1

    @pytest.mark.asyncio
    async def test_increment_provider_dropped(self, _reset_counter: None) -> None:
        counter = get_counter()
        await counter.increment_provider_dropped(
            client_protocol="anthropic",
            provider_id="minimax",
            model_id="MiniMax-M3",
        )
        snap = await counter.snapshot()
        key = "provider_dropped|anthropic|minimax|MiniMax-M3"
        assert snap["counters"][key] == 1

    @pytest.mark.asyncio
    async def test_increment_provider_rejected(self, _reset_counter: None) -> None:
        counter = get_counter()
        await counter.increment_provider_rejected(
            client_protocol="openai",
            provider_id="test-provider",
            model_id="test-model",
        )
        snap = await counter.snapshot()
        key = "provider_rejected|openai|test-provider|test-model"
        assert snap["counters"][key] == 1


class TestRecordThinkingEventProviderDecisions:
    """Tests for record_thinking_event with provider control decisions."""

    @pytest.mark.asyncio
    async def test_provider_mapped_event(self, _reset_counter: None) -> None:
        event = ThinkingMetricEvent(
            requested=True,
            client_protocol="openai",
            request_fields=["reasoning_effort"],
            requested_effort="high",
            resolved_budget_tokens=None,
            budget_clamped=False,
            capability_status="supported",
            capability_source="provider_catalog",
            upstream_protocol="openai",
            upstream_fields=["reasoning_effort"],
            decision="provider_mapped",
        )
        await record_thinking_event(event)
        counter = get_counter()
        snap = await counter.snapshot()
        assert snap["total"] >= 1
        # Check that provider_mapped counter was incremented.
        found = any(k.startswith("provider_mapped|") for k in snap["counters"])
        assert found

    @pytest.mark.asyncio
    async def test_provider_rejected_event(self, _reset_counter: None) -> None:
        event = ThinkingMetricEvent(
            requested=True,
            client_protocol="openai",
            request_fields=["reasoning_effort"],
            requested_effort="high",
            resolved_budget_tokens=None,
            budget_clamped=False,
            capability_status="unsupported",
            capability_source="manual_override",
            upstream_protocol="openai",
            upstream_fields=[],
            decision="provider_rejected",
        )
        await record_thinking_event(event)
        counter = get_counter()
        snap = await counter.snapshot()
        found = any(k.startswith("provider_rejected|") for k in snap["counters"])
        assert found

    @pytest.mark.asyncio
    async def test_provider_dropped_event(self, _reset_counter: None) -> None:
        event = ThinkingMetricEvent(
            requested=True,
            client_protocol="anthropic",
            request_fields=["thinking"],
            requested_effort=None,
            resolved_budget_tokens=None,
            budget_clamped=False,
            capability_status="unsupported",
            capability_source="manual_override",
            upstream_protocol="anthropic",
            upstream_fields=[],
            decision="provider_dropped",
        )
        await record_thinking_event(event)
        counter = get_counter()
        snap = await counter.snapshot()
        found = any(k.startswith("provider_dropped|") for k in snap["counters"])
        assert found
