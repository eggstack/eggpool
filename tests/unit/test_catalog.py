"""Tests for model catalog cache and normalizer."""

from __future__ import annotations

from go_aggregator.catalog.cache import ModelCatalogCache
from go_aggregator.catalog.normalizer import (
    normalize_anthropic_models,
    normalize_models,
    normalize_openai_models,
)


def test_normalize_openai_models() -> None:
    raw = {
        "data": [
            {"id": "gpt-4", "name": "GPT-4", "context_window": 8192},
            {"id": "gpt-3.5-turbo", "name": "GPT-3.5"},
        ]
    }
    models = normalize_openai_models(raw)
    assert len(models) == 2
    assert models[0]["model_id"] == "gpt-4"
    # Fail-closed: normalizer no longer assigns protocol; resolved by catalog
    assert models[0]["protocol"] is None
    assert models[0]["capabilities"]["context_window"] == 8192


def test_normalize_anthropic_models() -> None:
    raw = {
        "data": [
            {"id": "claude-3-opus", "display_name": "Claude 3 Opus"},
            {"id": "claude-3-sonnet"},
        ]
    }
    models = normalize_anthropic_models(raw)
    assert len(models) == 2
    assert models[0]["model_id"] == "claude-3-opus"
    assert models[0]["protocol"] == "anthropic"


def test_normalize_models_auto_detect() -> None:
    # Anthropic format
    anthropic = {"type": "list", "data": [{"id": "claude-3"}]}
    models = normalize_models(anthropic)
    assert models[0]["protocol"] == "anthropic"

    # OpenAI format - normalizer no longer assigns protocol; fail-closed
    openai = {"data": [{"id": "gpt-4"}]}
    models = normalize_models(openai)
    assert models[0]["protocol"] is None


def test_cache_update_from_account() -> None:
    cache = ModelCatalogCache()
    models = [
        {"model_id": "gpt-4", "protocol": "openai"},
        {"model_id": "claude-3", "protocol": "anthropic"},
    ]
    cache.update_from_account("account1", models)
    assert cache.model_count == 2
    assert cache.get_supporting_accounts("gpt-4") == {"account1"}


def test_cache_multi_account_support() -> None:
    cache = ModelCatalogCache()
    cache.update_from_account("account1", [{"model_id": "gpt-4", "protocol": "openai"}])
    cache.update_from_account("account2", [{"model_id": "gpt-4", "protocol": "openai"}])
    assert cache.get_supporting_accounts("gpt-4") == {
        "account1",
        "account2",
    }


def test_cache_exposure_union() -> None:
    cache = ModelCatalogCache()
    cache.update_from_account(
        "acct1",
        [
            {"model_id": "gpt-4", "protocol": "openai"},
            {"model_id": "claude-3", "protocol": "anthropic"},
        ],
    )

    # Union mode: expose if any eligible account supports it
    models = cache.get_models_for_exposure("union", {"acct1"})
    assert len(models) == 2


def test_cache_exposure_intersection() -> None:
    cache = ModelCatalogCache()
    cache.update_from_account(
        "acct1",
        [
            {"model_id": "gpt-4", "protocol": "openai"},
            {"model_id": "claude-3", "protocol": "anthropic"},
        ],
    )
    cache.update_from_account("acct2", [{"model_id": "gpt-4", "protocol": "openai"}])

    # Intersection: only models supported by all eligible accounts
    models = cache.get_models_for_exposure("intersection", {"acct1", "acct2"})
    assert len(models) == 1
    assert models[0]["model_id"] == "gpt-4"


def test_cache_model_available() -> None:
    cache = ModelCatalogCache()
    cache.update_from_account("acct1", [{"model_id": "gpt-4", "protocol": "openai"}])
    assert cache.is_model_available("gpt-4", {"acct1"}) is True
    assert cache.is_model_available("gpt-4", {"acct2"}) is False
    assert cache.is_model_available("unknown", {"acct1"}) is False


def test_cache_mark_unavailable() -> None:
    cache = ModelCatalogCache()
    cache.update_from_account("acct1", [{"model_id": "gpt-4", "protocol": "openai"}])
    cache.mark_model_unavailable("acct1", "gpt-4")
    assert cache.get_supporting_accounts("gpt-4") == set()


def test_cache_refresh_removes_withdrawn_account_support() -> None:
    cache = ModelCatalogCache()
    cache.update_from_account("acct1", [{"model_id": "gpt-4", "protocol": "openai"}])
    cache.update_from_account("acct2", [{"model_id": "gpt-4", "protocol": "openai"}])

    cache.update_from_account("acct1", [])

    assert cache.get_supporting_accounts("gpt-4") == {"acct2"}
    assert cache.get_models_for_exposure("union", {"acct1"}) == []


def test_cache_staleness() -> None:
    cache = ModelCatalogCache()
    assert cache.is_stale(60) is True  # Never refreshed
    cache.update_from_account("acct1", [{"model_id": "gpt-4", "protocol": "openai"}])
    assert cache.is_stale(3600) is False  # Just refreshed
