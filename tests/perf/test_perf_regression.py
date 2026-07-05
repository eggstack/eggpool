"""Performance regression guards for EggPool.

Verifies performance invariants and behavioral determinism for
core request-path components.  Run with::

    pytest tests/perf/test_perf_regression.py -m perf_regression -v
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from eggpool.routing.eligibility import get_eligible_accounts
from eggpool.transcoder.anthropic_to_openai import AnthropicToOpenAI
from eggpool.transcoder.context import TranscodeContext
from eggpool.transcoder.openai_to_anthropic import OpenAIToAnthropic
from eggpool.transcoder.segmentation import segment_request

pytestmark = pytest.mark.performance

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PERF_WARN_MS = 100.0  # log warnings above this threshold


def _openai_payload(
    model: str = "gpt-4",
    **extras: Any,
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": "Hello, world!"}],
    }
    base.update(extras)
    return base


def _anthropic_payload(
    model: str = "claude-3-sonnet-20240229",
    **extras: Any,
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "model": model,
        "max_tokens": 128,
        "messages": [{"role": "user", "content": "Hello, world!"}],
    }
    base.update(extras)
    return base


def _emit_regression(
    *,
    test_name: str,
    wall_ms: float,
    extras: dict[str, Any] | None = None,
) -> None:
    """Print a structured diagnostic for regression tests."""
    diag: dict[str, Any] = {
        "test": test_name,
        "wall_ms": round(wall_ms, 3),
    }
    if extras:
        diag.update(extras)
    print(f"\n  [REGRESSION] {json.dumps(diag, indent=2)}")
    if wall_ms > _PERF_WARN_MS:
        print(
            f"  [WARNING] {test_name} took {wall_ms:.1f} ms "
            f"(threshold: {_PERF_WARN_MS} ms)"
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.perf_baseline
async def test_segmentation_bounded_overhead() -> None:
    """Segmentation completes within reasonable time for various sizes.

    Timing is diagnostic output, not a hard pass/fail -- only gross
    regressions trigger a warning.
    """
    sizes = {
        "tiny": {"messages": [{"role": "user", "content": "Hi"}]},
        "small": _openai_payload(),
        "medium": _openai_payload(
            messages=[
                {"role": "system", "content": "You are helpful."},
                *[
                    {"role": "user", "content": f"Turn {i}: " + "x" * 500}
                    for i in range(10)
                ],
            ],
        ),
        "large": _openai_payload(
            messages=[
                {"role": "system", "content": "You are helpful."},
                *[
                    {"role": "user", "content": f"Turn {i}: " + "x" * 2000}
                    for i in range(50)
                ],
            ],
        ),
    }

    timings: dict[str, dict[str, Any]] = {}
    for label, payload in sizes.items():
        t0 = time.perf_counter()
        result = segment_request(payload, protocol="openai")
        elapsed_ms = (time.perf_counter() - t0) * 1000
        timings[label] = {
            "elapsed_ms": round(elapsed_ms, 3),
            "segment_count": len(result.segments),
            "status": result.status.value,
        }

    _emit_regression(
        test_name="segmentation_bounded_overhead",
        wall_ms=sum(t["elapsed_ms"] for t in timings.values()),
        extras={"payloads": timings},
    )

    for label, info in timings.items():
        assert info["status"] in ("segmented", "empty_request"), (
            f"Unexpected segmentation status for {label}: {info['status']}"
        )
        assert info["segment_count"] > 0, f"No segments for {label}"


@pytest.mark.asyncio
@pytest.mark.perf_baseline
async def test_routing_eligibility_deterministic() -> None:
    """Same inputs produce same routing decisions.

    Runs eligibility filtering multiple times with identical inputs
    and verifies the result is stable.
    """
    from eggpool.accounts.state import AccountRuntimeState
    from eggpool.catalog.cache import ModelCatalogCache

    catalog = ModelCatalogCache()
    catalog.load_model(
        model_id="gpt-4",
        display_name="GPT-4",
        protocol="openai",
        capabilities={},
        source_metadata={},
    )
    catalog.add_account_support("gpt-4", "acct-a")
    catalog.add_account_support("gpt-4", "acct-b")
    catalog.add_account_support("gpt-4", "acct-c")

    states = [
        AccountRuntimeState(
            name="acct-a",
            enabled=True,
            health_state="healthy",
        ),
        AccountRuntimeState(
            name="acct-b",
            enabled=True,
            health_state="healthy",
        ),
        AccountRuntimeState(
            name="acct-c",
            enabled=True,
            health_state="healthy",
        ),
    ]

    results: list[list[str]] = []
    for _ in range(10):
        eligible = get_eligible_accounts(
            states,
            model_id="gpt-4",
            catalog=catalog,
        )
        results.append([s.name for s in eligible])

    _emit_regression(
        test_name="routing_eligibility_deterministic",
        wall_ms=0,
        extras={
            "iterations": len(results),
            "result_count": len(results[0]),
        },
    )

    assert all(r == results[0] for r in results), (
        f"Non-deterministic routing: {results}"
    )
    assert len(results[0]) == 3


@pytest.mark.asyncio
@pytest.mark.perf_baseline
async def test_transcode_body_equivalence() -> None:
    """Verify transcoded bodies match expected JSON-normalized output.

    Tests both encode and decode directions for deterministic
    structural equivalence.
    """
    # OpenAI -> Anthropic
    o2a_ctx = TranscodeContext(
        request_id="regress-xcode-001",
        client_protocol="openai",
        upstream_protocol="anthropic",
    )
    o2a = OpenAIToAnthropic()

    openai_body = _openai_payload(
        messages=[
            {"role": "system", "content": "Be helpful."},
            {"role": "user", "content": "What is 2+2?"},
        ],
    )

    t0 = time.perf_counter()
    anthropic_upstream, o2a_warnings = o2a.encode_request(
        openai_body,
        o2a_ctx,
    )
    encode_ms = (time.perf_counter() - t0) * 1000

    # Structural assertions
    assert anthropic_upstream["model"] == "gpt-4"
    assert isinstance(anthropic_upstream["messages"], list)
    # System message is extracted into the system field
    assert "system" in anthropic_upstream or len(anthropic_upstream["messages"]) >= 2
    assert anthropic_upstream["max_tokens"] == 4096

    # Anthropic -> OpenAI
    a2o_ctx = TranscodeContext(
        request_id="regress-xcode-002",
        client_protocol="anthropic",
        upstream_protocol="openai",
    )
    a2o = AnthropicToOpenAI()

    anthropic_body = _anthropic_payload(
        messages=[{"role": "user", "content": "What is 2+2?"}],
    )

    t1 = time.perf_counter()
    openai_upstream, a2o_warnings = a2o.encode_request(
        anthropic_body,
        a2o_ctx,
    )
    encode_ms += (time.perf_counter() - t1) * 1000

    assert openai_upstream["model"] == "claude-3-sonnet-20240229"
    assert isinstance(openai_upstream["messages"], list)
    assert len(openai_upstream["messages"]) >= 1

    # Response round-trip: Anthropic upstream -> Anthropic client
    # (the decode_response converts upstream Anthropic -> client OpenAI)
    anthropic_upstream_response = {
        "id": "msg-regress-001",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": "4"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 8, "output_tokens": 1},
    }
    t2 = time.perf_counter()
    openai_client_response, _ = o2a.decode_response(
        anthropic_upstream_response,
        o2a_ctx,
    )
    decode_ms = (time.perf_counter() - t2) * 1000

    assert openai_client_response["choices"][0]["message"]["content"] == "4"

    # Decode an OpenAI upstream response back to Anthropic format
    openai_upstream_response = {
        "id": "chatcmpl-regress-002",
        "object": "chat.completion",
        "model": "gpt-4",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "4",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 8,
            "completion_tokens": 1,
            "total_tokens": 9,
        },
    }
    t3 = time.perf_counter()
    anthropic_client_response, _ = a2o.decode_response(
        openai_upstream_response,
        a2o_ctx,
    )
    decode_ms += (time.perf_counter() - t3) * 1000

    assert anthropic_client_response["content"][0]["text"] == "4"

    _emit_regression(
        test_name="transcode_body_equivalence",
        wall_ms=encode_ms + decode_ms,
        extras={
            "o2a_warnings": len(o2a_warnings),
            "a2o_warnings": len(a2o_warnings),
        },
    )
