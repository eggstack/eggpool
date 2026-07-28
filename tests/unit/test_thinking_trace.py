"""Thinking trace updates tests."""

from __future__ import annotations

from eggpool.catalog.capabilities import (
    ThinkingRequestIntent,
    classify_thinking_request,
)


class TestThinkingRequestIntent:
    """Tests for ThinkingRequestIntent construction and usage."""

    def test_from_openai_reasoning_effort(self) -> None:
        body = {"model": "gpt-4o", "reasoning_effort": "high"}
        req = classify_thinking_request(body, "openai")
        assert req.required is True
        assert req.requested_effort == "high"
        assert "reasoning_effort" in req.fields

        intent = ThinkingRequestIntent(
            requested_effort=req.requested_effort,
            requested_effort_original=req.requested_effort,
            requested_budget_tokens=req.requested_budget_tokens,
            request_fields=tuple(req.fields),
            has_historical_reasoning_content=False,
            client_requests_new_reasoning=True,
            client_protocol=req.client_protocol,
        )
        assert intent.requested_effort == "high"
        assert intent.client_requests_new_reasoning is True
        assert intent.has_historical_reasoning_content is False

    def test_from_anthropic_thinking_budget(self) -> None:
        body = {"model": "claude-3", "thinking": {"budget_tokens": 4096}}
        req = classify_thinking_request(body, "anthropic")
        assert req.required is True
        assert req.requested_budget_tokens == 4096
        assert "thinking" in req.fields

        intent = ThinkingRequestIntent(
            requested_effort=req.requested_effort,
            requested_effort_original=req.requested_effort,
            requested_budget_tokens=req.requested_budget_tokens,
            request_fields=tuple(req.fields),
            has_historical_reasoning_content=False,
            client_requests_new_reasoning=True,
            client_protocol=req.client_protocol,
        )
        assert intent.requested_budget_tokens == 4096
        assert intent.client_requests_new_reasoning is True

    def test_historical_reasoning_content_only(self) -> None:
        body = {
            "model": "gpt-4o",
            "messages": [
                {
                    "role": "assistant",
                    "reasoning_content": "I thought about this...",
                    "content": "The answer is 42.",
                },
            ],
        }
        req = classify_thinking_request(body, "openai")
        assert req.required is True
        assert "reasoning_content" in req.fields

        intent = ThinkingRequestIntent(
            requested_effort=req.requested_effort,
            requested_effort_original=req.requested_effort,
            requested_budget_tokens=req.requested_budget_tokens,
            request_fields=tuple(req.fields),
            has_historical_reasoning_content=True,
            client_requests_new_reasoning=False,
            client_protocol=req.client_protocol,
        )
        assert intent.has_historical_reasoning_content is True
        assert intent.client_requests_new_reasoning is False

    def test_no_thinking_controls(self) -> None:
        body = {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}
        req = classify_thinking_request(body, "openai")
        assert req.required is False

        intent = ThinkingRequestIntent(
            requested_effort=req.requested_effort,
            requested_effort_original=req.requested_effort,
            requested_budget_tokens=req.requested_budget_tokens,
            request_fields=tuple(req.fields),
            has_historical_reasoning_content=False,
            client_requests_new_reasoning=False,
            client_protocol=req.client_protocol,
        )
        assert intent.client_requests_new_reasoning is False
        assert intent.request_fields == ()
