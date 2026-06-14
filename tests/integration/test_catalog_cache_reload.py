"""Integration tests for cache reload protocol source consistency (Phase 14)."""

from __future__ import annotations

from go_aggregator.catalog.cache import ModelCatalogCache


def test_load_model_preserves_protocol_source() -> None:
    """Loading a model from DB preserves protocol_source."""
    cache = ModelCatalogCache()

    cache.load_model(
        model_id="gpt-4o",
        display_name="GPT-4o",
        protocol="openai",
        capabilities={},
        source_metadata={},
        protocol_source="exact_mapping",
    )

    model = cache.get_model("gpt-4o")
    assert model is not None
    assert model["protocol"] == "openai"
    assert model["protocol_source"] == "exact_mapping"


def test_load_model_default_protocol_source() -> None:
    """Loading a model without protocol_source defaults to None."""
    cache = ModelCatalogCache()

    cache.load_model(
        model_id="gpt-4o",
        display_name="GPT-4o",
        protocol="openai",
        capabilities={},
        source_metadata={},
    )

    model = cache.get_model("gpt-4o")
    assert model is not None
    assert model.get("protocol_source") is None


def test_update_from_account_preserves_source() -> None:
    """Updating from account preserves protocol_source in cache."""
    cache = ModelCatalogCache()

    cache.update_from_account(
        "acct-a",
        [
            {
                "model_id": "claude-3",
                "display_name": "Claude 3",
                "protocol": "anthropic",
                "protocol_source": "family_mapping",
                "capabilities": {},
                "source_metadata": {},
            },
        ],
    )

    model = cache.get_model("claude-3")
    assert model is not None
    assert model["protocol_source"] == "family_mapping"


def test_refresh_fallback_preserves_persisted_source() -> None:
    """When refresh has no resolution hints, persisted protocol is used."""
    cache = ModelCatalogCache()

    # Simulate a previously loaded model from DB
    cache.load_model(
        model_id="custom-model",
        display_name="Custom",
        protocol="openai",
        capabilities={},
        source_metadata={},
        protocol_source="persisted",
    )

    # Simulate a refresh that provides no resolution metadata
    cache.update_from_account(
        "acct-a",
        [
            {
                "model_id": "custom-model",
                "display_name": "Custom",
                "protocol": "openai",
                "protocol_source": "persisted",
                "capabilities": {},
                "source_metadata": {},
            },
        ],
    )

    model = cache.get_model("custom-model")
    assert model is not None
    assert model["protocol"] == "openai"
    assert model["protocol_source"] == "persisted"
