"""Tests for model catalog cache and normalizer."""

from __future__ import annotations

from go_aggregator.catalog.cache import ModelCatalogCache, parse_model_id
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
    cache.update_from_account("account1", "opencode-go", models)
    assert cache.model_count == 2
    assert cache.get_supporting_accounts("gpt-4") == {"account1"}


def test_cache_multi_account_support() -> None:
    cache = ModelCatalogCache()
    cache.update_from_account(
        "account1", "opencode-go", [{"model_id": "gpt-4", "protocol": "openai"}]
    )
    cache.update_from_account(
        "account2", "opencode-go", [{"model_id": "gpt-4", "protocol": "openai"}]
    )
    assert cache.get_supporting_accounts("gpt-4") == {
        "account1",
        "account2",
    }


def test_cache_exposure_union() -> None:
    cache = ModelCatalogCache()
    cache.update_from_account(
        "acct1",
        "opencode-go",
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
        "opencode-go",
        [
            {"model_id": "gpt-4", "protocol": "openai"},
            {"model_id": "claude-3", "protocol": "anthropic"},
        ],
    )
    cache.update_from_account(
        "acct2", "opencode-go", [{"model_id": "gpt-4", "protocol": "openai"}]
    )

    # Intersection: only models supported by all eligible accounts
    models = cache.get_models_for_exposure("intersection", {"acct1", "acct2"})
    assert len(models) == 1
    assert models[0]["model_id"] == "gpt-4"


def test_cache_model_available() -> None:
    cache = ModelCatalogCache()
    cache.update_from_account(
        "acct1", "opencode-go", [{"model_id": "gpt-4", "protocol": "openai"}]
    )
    assert cache.is_model_available("gpt-4", {"acct1"}) is True
    assert cache.is_model_available("gpt-4", {"acct2"}) is False
    assert cache.is_model_available("unknown", {"acct1"}) is False


def test_cache_mark_unavailable() -> None:
    cache = ModelCatalogCache()
    cache.update_from_account(
        "acct1", "opencode-go", [{"model_id": "gpt-4", "protocol": "openai"}]
    )
    cache.mark_model_unavailable("acct1", "gpt-4")
    assert cache.get_supporting_accounts("gpt-4") == set()


def test_cache_refresh_removes_withdrawn_account_support() -> None:
    cache = ModelCatalogCache()
    cache.update_from_account(
        "acct1", "opencode-go", [{"model_id": "gpt-4", "protocol": "openai"}]
    )
    cache.update_from_account(
        "acct2", "opencode-go", [{"model_id": "gpt-4", "protocol": "openai"}]
    )

    cache.update_from_account("acct1", "opencode-go", [])

    assert cache.get_supporting_accounts("gpt-4") == {"acct2"}
    assert cache.get_models_for_exposure("union", {"acct1"}) == []


def test_cache_staleness() -> None:
    cache = ModelCatalogCache()
    assert cache.is_stale(60) is True  # Never refreshed
    cache.update_from_account(
        "acct1", "opencode-go", [{"model_id": "gpt-4", "protocol": "openai"}]
    )
    assert cache.is_stale(3600) is False  # Just refreshed


# ===================================================================
# parse_model_id tests
# ===================================================================


def test_parse_model_id_without_suffix() -> None:
    base, provider = parse_model_id("gpt-4")
    assert base == "gpt-4"
    assert provider is None


def test_parse_model_id_with_suffix() -> None:
    base, provider = parse_model_id("gpt-4/opencode-go", {"opencode-go"})
    assert base == "gpt-4"
    assert provider == "opencode-go"


def test_parse_model_id_with_multiple_slashes() -> None:
    base, provider = parse_model_id("model/with/slashes/provider", {"provider"})
    assert base == "model/with/slashes"
    assert provider == "provider"


def test_parse_model_id_slash_not_matching_provider() -> None:
    """A slash-bearing unsuffixed ID must not be misparsed."""
    base, provider = parse_model_id("vendor/model-name", {"opencode-go"})
    assert base == "vendor/model-name"
    assert provider is None


def test_parse_model_id_no_known_providers() -> None:
    """Without known_providers, treat last segment as provider (legacy)."""
    base, provider = parse_model_id("gpt-4/opencode-go")
    assert base == "gpt-4"
    assert provider == "opencode-go"


def test_parse_model_id_rejects_empty_provider_suffix() -> None:
    base, provider = parse_model_id("gpt-4/")
    assert base == "gpt-4/"
    assert provider is None


def test_parse_model_id_rejects_empty_model_prefix() -> None:
    base, provider = parse_model_id("/opencode-go")
    assert base == "/opencode-go"
    assert provider is None


# ===================================================================
# Provider tracking tests
# ===================================================================


def test_update_from_account_tracks_provider() -> None:
    cache = ModelCatalogCache()
    cache.update_from_account(
        "acct1", "provider-a", [{"model_id": "gpt-4", "protocol": "openai"}]
    )
    assert cache.get_provider_for_account("acct1") == "provider-a"


def test_set_account_provider() -> None:
    cache = ModelCatalogCache()
    cache.set_account_provider("acct1", "my-provider")
    assert cache.get_provider_for_account("acct1") == "my-provider"


def test_get_provider_for_account_unknown() -> None:
    cache = ModelCatalogCache()
    assert cache.get_provider_for_account("nonexistent") is None


def test_update_from_account_overwrites_provider() -> None:
    cache = ModelCatalogCache()
    cache.update_from_account(
        "acct1", "provider-a", [{"model_id": "gpt-4", "protocol": "openai"}]
    )
    cache.update_from_account(
        "acct1", "provider-b", [{"model_id": "gpt-4", "protocol": "openai"}]
    )
    assert cache.get_provider_for_account("acct1") == "provider-b"


# ===================================================================
# Provider-suffixed model exposure tests
# ===================================================================


def test_provider_suffixed_models_union_single_provider() -> None:
    cache = ModelCatalogCache()
    cache.update_from_account(
        "acct1",
        "provider-a",
        [
            {"model_id": "gpt-4", "protocol": "openai"},
            {"model_id": "claude-3", "protocol": "anthropic"},
        ],
    )
    result = cache.get_provider_suffixed_models("union", {"acct1"})
    ids = {m["model_id"] for m in result}
    assert ids == {"claude-3/provider-a", "gpt-4/provider-a"}
    # Check base_model_id is preserved
    for m in result:
        assert m["base_model_id"] is not None
        assert m["provider_id"] is not None


def test_provider_suffixed_models_union_multi_provider() -> None:
    cache = ModelCatalogCache()
    cache.update_from_account(
        "acct1",
        "provider-a",
        [{"model_id": "gpt-4", "protocol": "openai"}],
    )
    cache.update_from_account(
        "acct2",
        "provider-b",
        [{"model_id": "gpt-4", "protocol": "openai"}],
    )
    result = cache.get_provider_suffixed_models("union", {"acct1", "acct2"})
    ids = {m["model_id"] for m in result}
    assert ids == {"gpt-4/provider-a", "gpt-4/provider-b"}


def test_provider_suffixed_models_intersection() -> None:
    cache = ModelCatalogCache()
    cache.update_from_account(
        "acct1",
        "provider-a",
        [
            {"model_id": "gpt-4", "protocol": "openai"},
            {"model_id": "claude-3", "protocol": "anthropic"},
        ],
    )
    cache.update_from_account(
        "acct2",
        "provider-a",
        [{"model_id": "gpt-4", "protocol": "openai"}],
    )
    # Intersection within provider-a: only gpt-4 (acct2 doesn't have claude-3)
    result = cache.get_provider_suffixed_models("intersection", {"acct1", "acct2"})
    ids = {m["model_id"] for m in result}
    assert ids == {"gpt-4/provider-a"}


def test_provider_suffixed_models_filters_unresolved() -> None:
    cache = ModelCatalogCache()
    cache.update_from_account(
        "acct1",
        "provider-a",
        [
            {"model_id": "gpt-4", "protocol": "openai"},
            {"model_id": "unknown", "protocol": None},
        ],
    )
    result = cache.get_provider_suffixed_models("union", {"acct1"})
    ids = {m["model_id"] for m in result}
    assert "gpt-4/provider-a" in ids
    assert "unknown/provider-a" not in ids


def test_provider_suffixed_models_empty_eligible() -> None:
    cache = ModelCatalogCache()
    cache.update_from_account(
        "acct1",
        "provider-a",
        [{"model_id": "gpt-4", "protocol": "openai"}],
    )
    result = cache.get_provider_suffixed_models("union", set())
    assert result == []


def test_provider_suffixed_models_available_accounts() -> None:
    cache = ModelCatalogCache()
    cache.update_from_account(
        "acct1",
        "provider-a",
        [{"model_id": "gpt-4", "protocol": "openai"}],
    )
    cache.update_from_account(
        "acct2",
        "provider-a",
        [{"model_id": "gpt-4", "protocol": "openai"}],
    )
    result = cache.get_provider_suffixed_models("union", {"acct1", "acct2"})
    assert len(result) == 1
    assert result[0]["available_accounts"] == ["acct1", "acct2"]


def test_provider_suffixed_models_cross_provider_independent() -> None:
    """A model available in one provider but not another generates only
    the suffixed ID for the provider that has it."""
    cache = ModelCatalogCache()
    cache.update_from_account(
        "acct1",
        "provider-a",
        [
            {"model_id": "gpt-4", "protocol": "openai"},
            {"model_id": "claude-3", "protocol": "anthropic"},
        ],
    )
    cache.update_from_account(
        "acct2",
        "provider-b",
        [{"model_id": "gpt-4", "protocol": "openai"}],
    )
    result = cache.get_provider_suffixed_models("union", {"acct1", "acct2"})
    ids = {m["model_id"] for m in result}
    # claude-3 only in provider-a, gpt-4 in both
    assert "claude-3/provider-a" in ids
    assert "claude-3/provider-b" not in ids
    assert "gpt-4/provider-a" in ids
    assert "gpt-4/provider-b" in ids
