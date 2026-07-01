"""Tests for usage canonicalisation."""

from __future__ import annotations

from eggpool.transcoder.usage import (
    CanonicalUsage,
    canonicalise_usage,
    merge_anthropic_usage,
    openai_usage_from_anthropic_usage,
)


def test_openai_usage() -> None:
    raw = {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}
    result = canonicalise_usage(raw, protocol="openai")
    assert result.prompt_tokens == 10
    assert result.completion_tokens == 20
    assert result.total_tokens == 30
    assert result.cache_creation_tokens == 0
    assert result.cache_read_tokens == 0


def test_openai_usage_missing_total() -> None:
    raw = {"prompt_tokens": 5, "completion_tokens": 15}
    result = canonicalise_usage(raw, protocol="openai")
    assert result.total_tokens == 20


def test_anthropic_usage() -> None:
    raw = {
        "input_tokens": 10,
        "output_tokens": 20,
        "cache_creation_input_tokens": 5,
        "cache_read_input_tokens": 3,
    }
    result = canonicalise_usage(raw, protocol="anthropic")
    assert result.prompt_tokens == 10
    assert result.completion_tokens == 20
    assert result.total_tokens == 30
    assert result.cache_creation_tokens == 5
    assert result.cache_read_tokens == 3


def test_anthropic_usage_minimal() -> None:
    raw = {"input_tokens": 8, "output_tokens": 12}
    result = canonicalise_usage(raw, protocol="anthropic")
    assert result.prompt_tokens == 8
    assert result.completion_tokens == 12
    assert result.total_tokens == 20
    assert result.cache_creation_tokens == 0
    assert result.cache_read_tokens == 0


def test_empty_usage() -> None:
    result = canonicalise_usage({}, protocol="openai")
    assert result.prompt_tokens == 0
    assert result.completion_tokens == 0
    assert result.total_tokens == 0


def test_malformed_openai_usage_zeroed() -> None:
    raw = {
        "prompt_tokens": "not-a-number",
        "completion_tokens": -7,
        "total_tokens": float("inf"),
    }
    result = canonicalise_usage(raw, protocol="openai")
    assert result == CanonicalUsage()


def test_malformed_anthropic_usage_zeroed() -> None:
    raw = {
        "input_tokens": None,
        "output_tokens": "nan",
        "cache_creation_input_tokens": -3,
        "cache_read_input_tokens": object(),
    }
    result = canonicalise_usage(raw, protocol="anthropic")
    assert result == CanonicalUsage()


def test_to_dict() -> None:
    u = CanonicalUsage(prompt_tokens=1, completion_tokens=2, total_tokens=3)
    d = u.to_dict()
    assert d == {
        "prompt_tokens": 1,
        "completion_tokens": 2,
        "total_tokens": 3,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
    }


def test_openai_usage_from_anthropic_usage_includes_cache_in_prompt() -> None:
    result = openai_usage_from_anthropic_usage(
        {
            "input_tokens": 850,
            "output_tokens": 25,
            "cache_read_input_tokens": 75_000,
            "cache_creation_input_tokens": 4_000,
        }
    )

    assert result["prompt_tokens"] == 79_850
    assert result["completion_tokens"] == 25
    assert result["total_tokens"] == 79_875
    assert result["prompt_tokens_details"] == {
        "cached_tokens": 75_000,
        "cache_creation_tokens": 4_000,
    }


def test_merge_anthropic_usage_combines_stream_start_and_delta() -> None:
    result = merge_anthropic_usage(
        {
            "input_tokens": 850,
            "cache_read_input_tokens": 75_000,
            "cache_creation_input_tokens": 4_000,
        },
        {"output_tokens": 25},
    )

    assert result == {
        "input_tokens": 850,
        "cache_read_input_tokens": 75_000,
        "cache_creation_input_tokens": 4_000,
        "output_tokens": 25,
    }
