"""Tests for /v1/models serialization."""

from __future__ import annotations

from eggpool.api.models import serialize_openai_model


def test_basic_model_serialization() -> None:
    model = {
        "model_id": "gpt-4",
        "display_name": "GPT-4",
        "first_seen_at": 1700000000.0,
    }
    result = serialize_openai_model(model)
    assert result["id"] == "gpt-4"
    assert result["object"] == "model"
    assert result["name"] == "GPT-4"
    assert "eggpool" not in result


def test_provider_suffixed_model_with_limits() -> None:
    model = {
        "model_id": "MiniMax-M3/opencode-go",
        "base_model_id": "MiniMax-M3",
        "provider_id": "opencode-go",
        "display_name": "MiniMax M3",
        "first_seen_at": 1700000000.0,
        "effective_limits": {
            "context_tokens": 220000,
            "input_tokens": None,
            "output_tokens": 16384,
            "enforce": True,
            "context_source": "provider_override",
            "input_source": "unknown",
            "output_source": "provider_override",
        },
    }
    result = serialize_openai_model(model)
    assert result["id"] == "MiniMax-M3/opencode-go"
    assert result["owned_by"] == "opencode-go"
    assert result["eggpool"]["base_model_id"] == "MiniMax-M3"
    assert result["eggpool"]["provider_id"] == "opencode-go"
    assert result["eggpool"]["limits"]["context"] == 220000
    assert result["eggpool"]["limits"]["output"] == 16384
    assert "input" not in result["eggpool"]["limits"]


def test_no_limits_when_all_unknown() -> None:
    model = {
        "model_id": "m1",
        "effective_limits": {
            "context_tokens": None,
            "input_tokens": None,
            "output_tokens": None,
            "enforce": True,
            "context_source": "unknown",
            "input_source": "unknown",
            "output_source": "unknown",
        },
    }
    result = serialize_openai_model(model)
    assert "eggpool" not in result


def test_unsuffixed_model_with_limits() -> None:
    model = {
        "model_id": "gpt-4",
        "display_name": "GPT-4",
        "first_seen_at": 0,
        "effective_limits": {
            "context_tokens": 128000,
            "input_tokens": None,
            "output_tokens": 4096,
            "enforce": True,
            "context_source": "conservative_provider_minimum",
            "input_source": "unknown",
            "output_source": "conservative_provider_minimum",
        },
    }
    result = serialize_openai_model(model)
    assert result["eggpool"]["limits"]["context"] == 128000
    assert result["eggpool"]["limits"]["output"] == 4096


def test_routing_priority_emitted_when_supplied() -> None:
    model = {
        "model_id": "minimax-m2.7/generalcompute",
        "base_model_id": "minimax-m2.7",
        "provider_id": "generalcompute",
        "display_name": "MiniMax M2.7",
    }
    result = serialize_openai_model(model, routing_priority=3)
    assert result["eggpool"]["routing_priority"] == 3
    assert result["eggpool"]["provider_id"] == "generalcompute"


def test_routing_priority_omitted_when_none() -> None:
    model = {
        "model_id": "gpt-4",
        "display_name": "GPT-4",
    }
    result = serialize_openai_model(model, routing_priority=None)
    assert "eggpool" not in result

    explicit_none = serialize_openai_model(model)
    assert "eggpool" not in explicit_none


def test_collapsed_entry_emits_providers_and_routing_priority_max() -> None:
    """Collapsed (unsuffixed) entries carry providers and routing_priority_max."""
    model = {
        "model_id": "minimax-m2.7",
        "display_name": "MiniMax M2.7",
    }
    result = serialize_openai_model(
        model,
        routing_priority_max=3,
        providers=["generalcompute", "minimax", "opencode-go"],
    )
    assert result["id"] == "minimax-m2.7"
    assert result["eggpool"]["providers"] == [
        "generalcompute",
        "minimax",
        "opencode-go",
    ]
    assert result["eggpool"]["routing_priority_max"] == 3


def test_collapsed_entry_omits_providers_when_none() -> None:
    """A collapsed entry without providers list stays clean."""
    model = {
        "model_id": "minimax-m2.7",
    }
    result = serialize_openai_model(model)
    assert "eggpool" not in result


def test_suffixed_entry_omits_collapsed_fields() -> None:
    """Provider-suffixed entries do not emit providers / routing_priority_max."""
    model = {
        "model_id": "minimax-m2.7/generalcompute",
        "base_model_id": "minimax-m2.7",
        "provider_id": "generalcompute",
    }
    result = serialize_openai_model(
        model,
        routing_priority=3,
        routing_priority_max=99,
        providers=["generalcompute", "minimax", "opencode-go"],
    )
    assert result["eggpool"]["routing_priority"] == 3
    assert "routing_priority_max" not in result["eggpool"]
    assert "providers" not in result["eggpool"]


# ---------------------------------------------------------------------------
# Capability exposure
# ---------------------------------------------------------------------------


def test_provider_scoped_model_with_thinking_capability() -> None:
    """Provider-scoped entry emits capabilities.thinking with client controls."""
    model = {
        "model_id": "claude-sonnet-4-20250514/anthropic",
        "base_model_id": "claude-sonnet-4-20250514",
        "provider_id": "anthropic",
        "display_name": "Claude Sonnet 4",
        "capabilities": {
            "thinking": {
                "status": "supported",
                "source": "provider_catalog",
                "native_protocols": ["anthropic"],
                "client_controls": {
                    "openai": {
                        "request_fields": ["reasoning_effort"],
                        "response_fields": ["reasoning_content"],
                        "stream_delta_fields": ["reasoning"],
                    },
                    "anthropic": {
                        "request_fields": ["thinking"],
                        "response_block_types": ["thinking"],
                    },
                },
                "effort_to_budget_tokens": {
                    "low": 1024,
                    "medium": 4096,
                    "high": 16384,
                },
            },
        },
    }
    result = serialize_openai_model(model, routing_priority=2)
    caps = result["eggpool"]["capabilities"]["thinking"]
    assert caps["status"] == "supported"
    assert caps["source"] == "provider_catalog"
    assert caps["native_protocols"] == ["anthropic"]
    assert caps["openai_request_fields"] == ["reasoning_effort"]
    assert caps["openai_response_fields"] == ["reasoning_content"]
    assert caps["openai_stream_delta_fields"] == ["reasoning"]
    assert caps["anthropic_request_fields"] == ["thinking"]
    assert caps["anthropic_response_block_types"] == ["thinking"]
    assert caps["effort_to_budget_tokens"] == {
        "low": 1024,
        "medium": 4096,
        "high": 16384,
    }


def test_collapsed_model_all_supported() -> None:
    """Collapsed entry with all providers supported omits per-provider breakdown."""
    model = {
        "model_id": "claude-sonnet-4-20250514",
        "capabilities": {
            "thinking": {
                "status": "supported",
                "source": "aggregate",
                "native_protocols": ["anthropic"],
            },
        },
    }
    result = serialize_openai_model(
        model,
        providers=["anthropic", "opencode-go"],
        routing_priority_max=2,
    )
    caps = result["eggpool"]["capabilities"]["thinking"]
    assert caps["status"] == "supported"
    assert "providers" not in caps


def test_collapsed_model_all_unknown() -> None:
    """Collapsed entry with all providers unknown shows unknown."""
    model = {
        "model_id": "some-model",
        "capabilities": {
            "thinking": {
                "status": "unknown",
                "source": "aggregate",
            },
        },
    }
    result = serialize_openai_model(
        model,
        providers=["p1", "p2"],
        routing_priority_max=1,
    )
    caps = result["eggpool"]["capabilities"]["thinking"]
    assert caps["status"] == "unknown"
    assert "providers" not in caps


def test_collapsed_model_mixed_status() -> None:
    """Collapsed entry with mixed status includes per-provider breakdown."""
    model = {
        "model_id": "minimax-m3",
        "capabilities": {
            "thinking": {
                "status": "mixed",
                "source": "aggregate",
                "native_protocols": ["anthropic"],
            },
        },
        "_provider_thinking_statuses": {
            "minimax": "supported",
            "openrouter": "unknown",
        },
    }
    result = serialize_openai_model(
        model,
        providers=["minimax", "openrouter"],
        routing_priority_max=1,
    )
    caps = result["eggpool"]["capabilities"]["thinking"]
    assert caps["status"] == "mixed"
    assert caps["providers"] == {
        "minimax": "supported",
        "openrouter": "unknown",
    }


def test_collapsed_model_unsupported_status() -> None:
    """Collapsed entry with all providers unsupported shows unsupported."""
    model = {
        "model_id": "legacy-model",
        "capabilities": {
            "thinking": {
                "status": "unsupported",
                "source": "aggregate",
            },
        },
    }
    result = serialize_openai_model(
        model,
        providers=["p1"],
        routing_priority_max=0,
    )
    caps = result["eggpool"]["capabilities"]["thinking"]
    assert caps["status"] == "unsupported"
    assert "providers" not in caps


def test_no_capabilities_when_empty() -> None:
    """Model with no capabilities omits eggpool.capabilities entirely."""
    model = {
        "model_id": "gpt-4",
        "display_name": "GPT-4",
    }
    result = serialize_openai_model(model)
    assert "eggpool" not in result


def test_unknown_capability_not_coerced_to_unsupported() -> None:
    """Unknown capability status stays unknown, not coerced to unsupported."""
    model = {
        "model_id": "some-model/provider",
        "base_model_id": "some-model",
        "provider_id": "provider",
        "capabilities": {
            "thinking": {
                "status": "unknown",
                "source": "unknown",
            },
        },
    }
    result = serialize_openai_model(model)
    caps = result["eggpool"]["capabilities"]["thinking"]
    assert caps["status"] == "unknown"
