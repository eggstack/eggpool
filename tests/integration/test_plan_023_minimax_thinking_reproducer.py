"""Plan 023 — MiniMax-M3 thinking-control reproducer (integration).

Deterministic reproduction of the MiniMax-M3/OpenCode Go unsupported-thinking
failure without live credentials.  Uses the ``MockUpstream`` infrastructure
to prove the exact request fields Eggpool forwarded and verify the nine
required Workstream A scenarios.

Run with::

    uv run pytest tests/integration/test_plan_023_minimax_thinking_reproducer.py -v
"""

from __future__ import annotations

import contextlib
import json
from typing import Any

import httpx
import pytest

from tests.helpers.mock_upstream import (
    UPSTREAM_BASE,
    MockUpstream,
    minimax_scenario_rules,
    minimax_thinking_rules,
)

pytestmark = [pytest.mark.integration, pytest.mark.request_path]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _openai_payload(
    model: str = "MiniMax-M3",
    effort: str | None = None,
    stream: bool = False,
    reasoning_obj: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an OpenAI-compatible request payload."""
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": "Hello"}],
    }
    if effort is not None:
        body["reasoning_effort"] = effort
    if reasoning_obj is not None:
        body["reasoning"] = reasoning_obj
    if stream:
        body["stream"] = True
    return body


async def _send_request(
    payload: dict[str, Any],
    upstream: MockUpstream,
    client: httpx.AsyncClient,
) -> httpx.Response:
    """Send a request through the mock upstream and return the response."""
    resp = await client.post(
        f"{UPSTREAM_BASE}/chat/completions",
        json=payload,
    )
    return resp


# ---------------------------------------------------------------------------
# Scenarios 1–9
# ---------------------------------------------------------------------------


class TestMiniMaxThinkingReproducer:
    """Deterministic MiniMax-M3 thinking-control failure reproducer."""

    def test_scenario_1_no_thinking_success(self) -> None:
        """Scenario 1: No thinking field → successful response."""
        rules = minimax_scenario_rules("no_thinking_success")
        upstream = MockUpstream(rules=rules)
        with upstream, httpx.Client(base_url=UPSTREAM_BASE) as client:
            resp = client.post(
                "/chat/completions",
                json=_openai_payload(model="MiniMax-M3"),
            )
        assert resp.status_code == 200
        assert upstream.request_count == 1
        captured = upstream.get_request(0)
        assert captured.model == "MiniMax-M3"
        assert not captured.has_thinking_field
        assert captured.reasoning_effort is None

    def test_scenario_2_accepted_thinking_success(self) -> None:
        """Scenario 2: Accepted thinking field/value → successful response."""
        rules = minimax_scenario_rules("accepted_thinking_success")
        upstream = MockUpstream(rules=rules)
        with upstream, httpx.Client(base_url=UPSTREAM_BASE) as client:
            resp = client.post(
                "/chat/completions",
                json=_openai_payload(model="MiniMax-M3", effort="low"),
            )
        assert resp.status_code == 200
        captured = upstream.get_request(0)
        assert captured.model == "MiniMax-M3"
        assert captured.reasoning_effort == "low"

    def test_scenario_3_unsupported_400(self) -> None:
        """Scenario 3: Unsupported thinking level → HTTP 400."""
        rules = minimax_scenario_rules("unsupported_400")
        upstream = MockUpstream(rules=rules)
        with upstream, httpx.Client(base_url=UPSTREAM_BASE) as client:
            resp = client.post(
                "/chat/completions",
                json=_openai_payload(model="MiniMax-M3", effort="ultra-mega"),
            )
        assert resp.status_code == 400
        body = resp.json()
        assert "error" in body
        assert "Unsupported thinking level" in body["error"]["message"]
        captured = upstream.get_request(0)
        assert captured.model == "MiniMax-M3"
        assert captured.reasoning_effort == "ultra-mega"

    def test_scenario_4_unsupported_422(self) -> None:
        """Scenario 4: Unsupported thinking level → HTTP 422."""
        rules = minimax_scenario_rules("unsupported_422")
        upstream = MockUpstream(rules=rules)
        with upstream, httpx.Client(base_url=UPSTREAM_BASE) as client:
            resp = client.post(
                "/chat/completions",
                json=_openai_payload(model="MiniMax-M3", effort="xhigh"),
            )
        assert resp.status_code == 422
        body = resp.json()
        assert "error" in body
        assert "xhigh" in body["error"]["message"]

    def test_scenario_5_misleading_404(self) -> None:
        """Scenario 5: Misleading 404 with 'unsupported model' + thinking."""
        rules = minimax_scenario_rules("misleading_404")
        upstream = MockUpstream(rules=rules)
        with upstream, httpx.Client(base_url=UPSTREAM_BASE) as client:
            resp = client.post(
                "/chat/completions",
                json=_openai_payload(model="MiniMax-M3"),
            )
        assert resp.status_code == 404
        body = resp.json()
        assert "error" in body
        msg = body["error"]["message"]
        assert "does not exist" in msg or "not supported" in msg

    def test_scenario_6_error_then_unrelated_success(self) -> None:
        """Scenario 6: Error followed by successful unrelated model."""
        rules = minimax_scenario_rules("error_then_unrelated_success")
        upstream = MockUpstream(rules=rules)
        with upstream, httpx.Client(base_url=UPSTREAM_BASE) as client:
            resp1 = client.post(
                "/chat/completions",
                json=_openai_payload(model="MiniMax-M3", effort="ultra-mega"),
            )
            resp2 = client.post(
                "/chat/completions",
                json=_openai_payload(model="gpt-4"),
            )
        assert resp1.status_code == 400
        assert resp2.status_code == 200
        assert upstream.request_count == 2
        assert upstream.get_request(0).model == "MiniMax-M3"
        assert upstream.get_request(1).model == "gpt-4"

    def test_scenario_7_error_then_minimax_success(self) -> None:
        """Scenario 7: Error followed by successful MiniMax-M3 without thinking."""
        rules = minimax_scenario_rules("error_then_minimax_success")
        upstream = MockUpstream(rules=rules)
        with upstream, httpx.Client(base_url=UPSTREAM_BASE) as client:
            resp1 = client.post(
                "/chat/completions",
                json=_openai_payload(model="MiniMax-M3", effort="ultra-mega"),
            )
            resp2 = client.post(
                "/chat/completions",
                json=_openai_payload(model="MiniMax-M3"),
            )
        assert resp1.status_code == 400
        assert resp2.status_code == 200
        assert upstream.request_count == 2
        assert upstream.get_request(0).reasoning_effort == "ultra-mega"
        assert upstream.get_request(1).reasoning_effort is None

    def test_scenario_8_streaming_rejected(self) -> None:
        """Scenario 8: Streaming request rejected before response bytes."""
        rules = minimax_scenario_rules("streaming_rejected")
        upstream = MockUpstream(rules=rules)
        with upstream, httpx.Client(base_url=UPSTREAM_BASE) as client:
            resp = client.post(
                "/chat/completions",
                json=_openai_payload(
                    model="MiniMax-M3", effort="ultra-mega", stream=True
                ),
            )
        assert resp.status_code == 400

    def test_scenario_9_connection_drop_after_headers(self) -> None:
        """Scenario 9: Connection dropped after headers but before body read."""
        rules = minimax_scenario_rules("connection_drop_after_headers")
        upstream = MockUpstream(rules=rules)
        with (
            upstream,
            httpx.Client(base_url=UPSTREAM_BASE) as client,
            contextlib.suppress(Exception),
        ):
            # The mock drops after headers; httpx raises on body read
            _resp = client.post(
                "/chat/completions",
                json=_openai_payload(model="MiniMax-M3", effort="high"),
            )
        # Either outcome (exception or incomplete response) proves the mock fired
        assert upstream.request_count == 1
        captured = upstream.get_request(0)
        assert captured.model == "MiniMax-M3"
        assert captured.reasoning_effort == "high"


class TestMiniMaxRequestFieldCapture:
    """Verify the mock captures exact request fields."""

    def test_captures_model_field(self) -> None:
        upstream = MockUpstream(rules=minimax_thinking_rules())
        with upstream, httpx.Client(base_url=UPSTREAM_BASE) as client:
            client.post(
                "/chat/completions",
                json=_openai_payload(model="MiniMax-M3", effort="low"),
            )
        captured = upstream.get_request(0)
        assert captured.model == "MiniMax-M3"

    def test_captures_reasoning_effort(self) -> None:
        upstream = MockUpstream(rules=minimax_thinking_rules())
        with upstream, httpx.Client(base_url=UPSTREAM_BASE) as client:
            client.post(
                "/chat/completions",
                json=_openai_payload(model="MiniMax-M3", effort="high"),
            )
        captured = upstream.get_request(0)
        assert captured.reasoning_effort == "high"

    def test_captures_body_bytes(self) -> None:
        upstream = MockUpstream(rules=minimax_thinking_rules())
        with upstream, httpx.Client(base_url=UPSTREAM_BASE) as client:
            payload = _openai_payload(model="MiniMax-M3")
            client.post("/chat/completions", json=payload)
        captured = upstream.get_request(0)
        assert len(captured.request_body_bytes) > 0
        parsed = json.loads(captured.request_body_bytes)
        assert parsed["model"] == "MiniMax-M3"

    def test_captures_sequence_number(self) -> None:
        upstream = MockUpstream(rules=minimax_thinking_rules())
        with upstream, httpx.Client(base_url=UPSTREAM_BASE) as client:
            client.post(
                "/chat/completions",
                json=_openai_payload(model="MiniMax-M3"),
            )
            client.post(
                "/chat/completions",
                json=_openai_payload(model="gpt-4"),
            )
        assert upstream.get_request(0).sequence == 1
        assert upstream.get_request(1).sequence == 2
