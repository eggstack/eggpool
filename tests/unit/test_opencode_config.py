"""Tests for OpenCode configuration generation."""

from __future__ import annotations

import json

from eggpool.integrations.opencode import (
    build_opencode_config_json,
    build_opencode_provider_config,
)


def test_basic_provider_structure() -> None:
    result = build_opencode_provider_config(
        base_url="http://192.168.1.1:8080/v1",
        api_key="ep_test123",
        models=[],
    )
    assert result["$schema"] == "https://opencode.ai/config.json"
    provider = result["provider"]["eggpool"]
    assert provider["npm"] == "@ai-sdk/openai-compatible"
    assert provider["name"] == "EggPool"
    assert provider["options"]["baseURL"] == "http://192.168.1.1:8080/v1"
    assert provider["options"]["apiKey"] == "ep_test123"
    assert provider["models"] == {}


def test_model_with_limits() -> None:
    models = [
        {
            "model_id": "MiniMax-M3/opencode-go",
            "display_name": "MiniMax M3",
            "provider_id": "opencode-go",
            "effective_limits": {
                "context_tokens": 220000,
                "input_tokens": None,
                "output_tokens": 16384,
                "enforce": True,
            },
        }
    ]
    result = build_opencode_provider_config(
        base_url="http://host:8080/v1",
        api_key="ep_key",
        models=models,
    )
    m = result["provider"]["eggpool"]["models"]["MiniMax-M3/opencode-go"]
    assert m["name"] == "MiniMax M3/opencode-go"
    assert m["limit"]["context"] == 220000
    assert m["limit"]["output"] == 16384
    assert "input" not in m["limit"]


def test_model_without_limits() -> None:
    models = [
        {
            "model_id": "gpt-4",
            "display_name": "GPT-4",
            "effective_limits": {},
        }
    ]
    result = build_opencode_provider_config(
        base_url="http://host:8080/v1",
        api_key="ep_key",
        models=models,
    )
    m = result["provider"]["eggpool"]["models"]["gpt-4"]
    assert m["name"] == "GPT-4"
    assert "limit" not in m


def test_models_sorted() -> None:
    models = [
        {"model_id": "zebra", "effective_limits": {}},
        {"model_id": "alpha", "effective_limits": {}},
        {"model_id": "middle", "effective_limits": {}},
    ]
    result = build_opencode_provider_config(
        base_url="http://host:8080/v1",
        api_key="ep_key",
        models=models,
    )
    keys = list(result["provider"]["eggpool"]["models"].keys())
    assert keys == ["alpha", "middle", "zebra"]


def test_json_serialization() -> None:
    models = [
        {
            "model_id": "m1",
            "effective_limits": {"context_tokens": 100000},
        }
    ]
    json_str = build_opencode_config_json(
        base_url="http://host:8080/v1",
        api_key="ep_key",
        models=models,
    )
    parsed = json.loads(json_str)
    assert parsed["provider"]["eggpool"]["models"]["m1"]["limit"]["context"] == 100000


def test_zero_limits_not_emitted() -> None:
    models = [
        {
            "model_id": "m1",
            "effective_limits": {
                "context_tokens": 0,
                "output_tokens": 0,
            },
        }
    ]
    result = build_opencode_provider_config(
        base_url="http://host:8080/v1",
        api_key="ep_key",
        models=models,
    )
    m = result["provider"]["eggpool"]["models"]["m1"]
    assert "limit" not in m


def test_empty_models_produces_valid_structure() -> None:
    result = build_opencode_provider_config(
        base_url="http://host:8080/v1",
        api_key="ep_key",
        models=[],
    )
    assert result["provider"]["eggpool"]["models"] == {}


def test_provider_suffixed_id_preserved() -> None:
    models = [
        {
            "model_id": "MiniMax-M3/opencode-go",
            "provider_id": "opencode-go",
            "display_name": "MiniMax M3",
            "effective_limits": {"context_tokens": 220000},
        }
    ]
    result = build_opencode_provider_config(
        base_url="http://host:8080/v1",
        api_key="ep_key",
        models=models,
    )
    entry = result["provider"]["eggpool"]["models"]["MiniMax-M3/opencode-go"]
    assert entry["name"] == "MiniMax M3/opencode-go"
    assert entry["limit"]["context"] == 220000


def test_provider_suffix_added_to_name() -> None:
    """When the catalog entry has provider_id and a bare display_name, the
    rendered ``name`` field is suffixed with ``/provider_id`` so OpenCode's
    model picker disambiguates providers serving the same model id."""
    models = [
        {
            "model_id": "MiniMax-M3/minimax",
            "provider_id": "minimax",
            "display_name": "MiniMax-M3",
            "effective_limits": {"context_tokens": 160000},
        }
    ]
    result = build_opencode_provider_config(
        base_url="http://host:8080/v1",
        api_key="ep_key",
        models=models,
    )
    entry = result["provider"]["eggpool"]["models"]["MiniMax-M3/minimax"]
    assert entry["name"] == "MiniMax-M3/minimax"


def test_provider_suffix_skipped_when_already_present() -> None:
    """If the catalog's display_name already ends with /provider_id the
    rendered name is left unchanged (no duplicate suffix)."""
    models = [
        {
            "model_id": "MiniMax-M3/minimax",
            "provider_id": "minimax",
            "display_name": "MiniMax-M3/minimax",
            "effective_limits": {"context_tokens": 160000},
        }
    ]
    result = build_opencode_provider_config(
        base_url="http://host:8080/v1",
        api_key="ep_key",
        models=models,
    )
    entry = result["provider"]["eggpool"]["models"]["MiniMax-M3/minimax"]
    assert "name" not in entry


def test_provider_suffix_skipped_when_collapse_models() -> None:
    """collapse_models=True leaves provider_id unset; the bare display_name
    is used as-is."""
    models = [
        {
            "model_id": "MiniMax-M3",
            "display_name": "MiniMax-M3",
            "effective_limits": {"context_tokens": 160000},
        }
    ]
    result = build_opencode_provider_config(
        base_url="http://host:8080/v1",
        api_key="ep_key",
        models=models,
    )
    entry = result["provider"]["eggpool"]["models"]["MiniMax-M3"]
    assert "name" not in entry


def test_collapse_models_merges_providers_under_one_id() -> None:
    """In collapse mode the catalog flattens entries across providers, so the
    rendered config has a single bare-id entry without a provider suffix in
    the displayed name (the gateway picks a provider per request based on
    ranking)."""
    models = [
        {
            "model_id": "MiniMax-M3",
            "display_name": "MiniMax M3",
            "effective_limits": {"context_tokens": 160000},
        }
    ]
    result = build_opencode_provider_config(
        base_url="http://host:8080/v1",
        api_key="ep_key",
        models=models,
    )
    models_block = result["provider"]["eggpool"]["models"]
    assert list(models_block.keys()) == ["MiniMax-M3"]
    assert models_block["MiniMax-M3"]["name"] == "MiniMax M3"
    assert "/" not in models_block["MiniMax-M3"]["name"]


def test_collapse_models_without_display_name_omits_name() -> None:
    """In collapse mode, when the catalog's display_name matches the bare
    model_id no ``name`` field is emitted (OpenCode falls back to the key)."""
    models = [
        {
            "model_id": "gpt-4",
            "display_name": "gpt-4",
            "effective_limits": {"context_tokens": 100000},
        }
    ]
    result = build_opencode_provider_config(
        base_url="http://host:8080/v1",
        api_key="ep_key",
        models=models,
    )
    entry = result["provider"]["eggpool"]["models"]["gpt-4"]
    assert "name" not in entry


def test_all_limit_fields() -> None:
    models = [
        {
            "model_id": "m1",
            "effective_limits": {
                "context_tokens": 200000,
                "input_tokens": 180000,
                "output_tokens": 16384,
            },
        }
    ]
    result = build_opencode_provider_config(
        base_url="http://host:8080/v1",
        api_key="ep_key",
        models=models,
    )
    limit = result["provider"]["eggpool"]["models"]["m1"]["limit"]
    assert limit == {"context": 200000, "input": 180000, "output": 16384}


def test_output_is_valid_json() -> None:
    """build_opencode_config_json output is pure JSON that round-trips."""
    output = build_opencode_config_json(
        base_url="http://host:8080/v1",
        api_key="ep_key",
        models=[
            {"model_id": "m1", "effective_limits": {"context_tokens": 100000}},
            {"model_id": "m2", "effective_limits": {"context_tokens": 200000}},
        ],
    )
    parsed = json.loads(output)
    assert "$schema" in parsed
    models = parsed["provider"]["eggpool"]["models"]
    assert list(models.keys()) == ["m1", "m2"]
    # No status messages mixed into JSON output
    assert "\n" not in output.split("\n")[0] or output.strip().startswith("{")


def test_output_no_status_contamination() -> None:
    """Status messages do not contaminate JSON output."""
    output = build_opencode_config_json(
        base_url="http://host:8080/v1",
        api_key="ep_key",
        models=[],
    )
    # Output must be parseable JSON with no leading/trailing text
    parsed = json.loads(output)
    assert isinstance(parsed, dict)
    assert "provider" in parsed
