"""Canonical request fixtures for Plan 023 error-isolation reproducer.

Provides immutable request payloads covering every thinking-control variant
that EggPool clients may send.  Fixtures are returned as fresh copies on
every call so tests cannot accidentally mutate shared state.

Categories
----------

- OpenAI top-level ``reasoning_effort``: low, med, medium, high, xhigh,
  unknown string, null, omitted.
- OpenAI nested ``reasoning`` forms.
- Anthropic ``thinking`` with explicit ``budget_tokens``.
- Historical assistant ``reasoning_content``.
- Provider-qualified model IDs and collapsed model IDs.
- Native-protocol and transcoded paths.
- Streaming and non-streaming requests.
- Requests with tools, cache controls, and compression-enabled payloads.
"""

from __future__ import annotations

import copy
from typing import Any

# ---------------------------------------------------------------------------
# Model identifiers
# ---------------------------------------------------------------------------

MODEL_MINIMAX_M3 = "MiniMax-M3"
MODEL_MINIMAX_M3_QUALIFIED = "MiniMax-M3/opencode-go"
MODEL_GPT4 = "gpt-4"
MODEL_CLAUDE_SONNET = "claude-3-sonnet-20240229"
MODEL_CLAUDE_SONNET_QUALIFIED = "claude-3-sonnet-20240229/anthropic"


# ---------------------------------------------------------------------------
# Base message payloads
# ---------------------------------------------------------------------------


def _base_user_message() -> list[dict[str, Any]]:
    return [{"role": "user", "content": "Hello, what can you do?"}]


def _base_assistant_with_reasoning_content() -> list[dict[str, Any]]:
    return [
        {
            "role": "assistant",
            "content": "I can help with that.",
            "reasoning_content": "Let me think about this step by step.",
        },
        {"role": "user", "content": "Tell me more."},
    ]


# ---------------------------------------------------------------------------
# OpenAI top-level reasoning_effort variants
# ---------------------------------------------------------------------------


def openai_reasoning_effort_low(model: str = MODEL_MINIMAX_M3) -> dict[str, Any]:
    """OpenAI request with top-level reasoning_effort=low."""
    return {
        "model": model,
        "messages": _base_user_message(),
        "reasoning_effort": "low",
    }


def openai_reasoning_effort_medium(model: str = MODEL_MINIMAX_M3) -> dict[str, Any]:
    """OpenAI request with top-level reasoning_effort=medium."""
    return {
        "model": model,
        "messages": _base_user_message(),
        "reasoning_effort": "medium",
    }


def openai_reasoning_effort_high(model: str = MODEL_MINIMAX_M3) -> dict[str, Any]:
    """OpenAI request with top-level reasoning_effort=high."""
    return {
        "model": model,
        "messages": _base_user_message(),
        "reasoning_effort": "high",
    }


def openai_reasoning_effort_xhigh(model: str = MODEL_MINIMAX_M3) -> dict[str, Any]:
    """OpenAI request with top-level reasoning_effort=xhigh (unsupported)."""
    return {
        "model": model,
        "messages": _base_user_message(),
        "reasoning_effort": "xhigh",
    }


def openai_reasoning_effort_unknown(model: str = MODEL_MINIMAX_M3) -> dict[str, Any]:
    """OpenAI request with top-level reasoning_effort=ultra-mega (unknown)."""
    return {
        "model": model,
        "messages": _base_user_message(),
        "reasoning_effort": "ultra-mega",
    }


def openai_reasoning_effort_null(model: str = MODEL_MINIMAX_M3) -> dict[str, Any]:
    """OpenAI request with top-level reasoning_effort=null."""
    return {
        "model": model,
        "messages": _base_user_message(),
        "reasoning_effort": None,
    }


def openai_reasoning_effort_omitted(model: str = MODEL_MINIMAX_M3) -> dict[str, Any]:
    """OpenAI request with reasoning_effort omitted entirely."""
    return {
        "model": model,
        "messages": _base_user_message(),
    }


# ---------------------------------------------------------------------------
# OpenAI nested reasoning forms
# ---------------------------------------------------------------------------


def openai_nested_reasoning_low(model: str = MODEL_MINIMAX_M3) -> dict[str, Any]:
    """OpenAI nested reasoning object with effort=low."""
    return {
        "model": model,
        "messages": _base_user_message(),
        "reasoning": {"effort": "low"},
    }


def openai_nested_reasoning_high(model: str = MODEL_MINIMAX_M3) -> dict[str, Any]:
    """OpenAI nested reasoning object with effort=high."""
    return {
        "model": model,
        "messages": _base_user_message(),
        "reasoning": {"effort": "high"},
    }


def openai_nested_reasoning_xhigh(model: str = MODEL_MINIMAX_M3) -> dict[str, Any]:
    """OpenAI nested reasoning object with effort=xhigh (unsupported)."""
    return {
        "model": model,
        "messages": _base_user_message(),
        "reasoning": {"effort": "xhigh"},
    }


# ---------------------------------------------------------------------------
# Anthropic thinking with budget_tokens
# ---------------------------------------------------------------------------


def anthropic_thinking_low(model: str = MODEL_CLAUDE_SONNET) -> dict[str, Any]:
    """Anthropic request with thinking enabled (low budget)."""
    return {
        "model": model,
        "messages": [{"role": "user", "content": "Think step by step."}],
        "thinking": {"type": "enabled", "budget_tokens": 1024},
    }


def anthropic_thinking_high(model: str = MODEL_CLAUDE_SONNET) -> dict[str, Any]:
    """Anthropic request with thinking enabled (high budget)."""
    return {
        "model": model,
        "messages": [{"role": "user", "content": "Think deeply."}],
        "thinking": {"type": "enabled", "budget_tokens": 8192},
    }


def anthropic_thinking_disabled(model: str = MODEL_CLAUDE_SONNET) -> dict[str, Any]:
    """Anthropic request with thinking explicitly disabled."""
    return {
        "model": model,
        "messages": [{"role": "user", "content": "Just answer."}],
        "thinking": {"type": "disabled"},
    }


# ---------------------------------------------------------------------------
# Historical reasoning_content
# ---------------------------------------------------------------------------


def assistant_reasoning_content(model: str = MODEL_MINIMAX_M3) -> dict[str, Any]:
    """Request with historical assistant reasoning_content in message history."""
    return {
        "model": model,
        "messages": _base_assistant_with_reasoning_content(),
    }


# ---------------------------------------------------------------------------
# Provider-qualified model IDs
# ---------------------------------------------------------------------------


def provider_qualified_model() -> dict[str, Any]:
    """Request using a provider-qualified model ID."""
    return {
        "model": MODEL_MINIMAX_M3_QUALIFIED,
        "messages": _base_user_message(),
    }


def provider_qualified_model_with_thinking() -> dict[str, Any]:
    """Provider-qualified model with unsupported thinking level."""
    return {
        "model": MODEL_MINIMAX_M3_QUALIFIED,
        "messages": _base_user_message(),
        "reasoning_effort": "xhigh",
    }


# ---------------------------------------------------------------------------
# Streaming variants
# ---------------------------------------------------------------------------


def openai_streaming_reasoning_effort(
    model: str = MODEL_MINIMAX_M3,
    effort: str = "high",
) -> dict[str, Any]:
    """Streaming request with top-level reasoning_effort (effectively a flag)."""
    return {
        "model": model,
        "messages": _base_user_message(),
        "reasoning_effort": effort,
        "stream": True,
    }


def openai_streaming_nested_reasoning(
    model: str = MODEL_MINIMAX_M3,
    effort: str = "high",
) -> dict[str, Any]:
    """Streaming request with nested reasoning object."""
    return {
        "model": model,
        "messages": _base_user_message(),
        "reasoning": {"effort": effort},
        "stream": True,
    }


# ---------------------------------------------------------------------------
# Tool-use fixtures
# ---------------------------------------------------------------------------


def openai_with_tools(model: str = MODEL_MINIMAX_M3) -> dict[str, Any]:
    """Request with tool definitions (for compression/replay reuse)."""
    return {
        "model": model,
        "messages": _base_user_message(),
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get the weather for a location",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "location": {
                                "type": "string",
                                "description": "City name",
                            }
                        },
                        "required": ["location"],
                    },
                },
            }
        ],
    }


def openai_with_tools_and_thinking(model: str = MODEL_MINIMAX_M3) -> dict[str, Any]:
    """Request with tool definitions and unsupported thinking level."""
    return {
        "model": model,
        "messages": _base_user_message(),
        "reasoning_effort": "xhigh",
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get the weather for a location",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "location": {
                                "type": "string",
                                "description": "City name",
                            }
                        },
                        "required": ["location"],
                    },
                },
            }
        ],
    }


# ---------------------------------------------------------------------------
# Cache-control fixtures
# ---------------------------------------------------------------------------


def anthropic_with_cache_control(model: str = MODEL_CLAUDE_SONNET) -> dict[str, Any]:
    """Anthropic request with cache-control markers (synthetic cache reuse)."""
    return {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "Analyze this document.",
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            }
        ],
    }


# ---------------------------------------------------------------------------
# Immutable copy helper
# ---------------------------------------------------------------------------


def copy_fixture(fixture_fn: Any, **overrides: Any) -> dict[str, Any]:
    """Return a deep copy of a fixture with optional field overrides.

    Ensures tests never mutate shared state.
    """
    base = fixture_fn() if callable(fixture_fn) else copy.deepcopy(fixture_fn)
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# All OpenAI reasoning_effort variants (for parametrize)
# ---------------------------------------------------------------------------

OPENAI_REASONING_EFFORT_VARIANTS: list[tuple[str, dict[str, Any], str]] = [
    ("low", openai_reasoning_effort_low, "low"),
    ("medium", openai_reasoning_effort_medium, "medium"),
    ("high", openai_reasoning_effort_high, "high"),
    ("xhigh", openai_reasoning_effort_xhigh, "xhigh"),
    ("unknown_string", openai_reasoning_effort_unknown, "ultra-mega"),
    ("null", openai_reasoning_effort_null, "null"),
    ("omitted", openai_reasoning_effort_omitted, "omitted"),
]

NESTED_REASONING_VARIANTS: list[tuple[str, dict[str, Any], str]] = [
    ("nested_low", openai_nested_reasoning_low, "low"),
    ("nested_high", openai_nested_reasoning_high, "high"),
    ("nested_xhigh", openai_nested_reasoning_xhigh, "xhigh"),
]

ANTHROPIC_THINKING_VARIANTS: list[tuple[str, dict[str, Any], str]] = [
    ("thinking_low", anthropic_thinking_low, "1024"),
    ("thinking_high", anthropic_thinking_high, "8192"),
    ("thinking_disabled", anthropic_thinking_disabled, "disabled"),
]
