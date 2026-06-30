"""Tests for model catalog cache and normalizer."""

from __future__ import annotations

import time

from eggpool.catalog.cache import ModelCatalogCache, parse_model_id
from eggpool.catalog.normalizer import (
    normalize_anthropic_models,
    normalize_models,
    normalize_openai_models,
)


def test_hydrate_account_age_uses_its_provider_metadata() -> None:
    """A fresh provider must not refresh another provider's account age."""
    cache = ModelCatalogCache()
    now = time.time()
    cache.load_model(
        "shared-model",
        None,
        "openai",
        {},
        {},
        last_seen_at=now,
    )
    cache.set_provider_model_entry(
        "shared-model",
        "stale-provider",
        {"protocol": "openai", "last_seen_at": now - 120},
    )
    cache.set_provider_model_entry(
        "shared-model",
        "fresh-provider",
        {"protocol": "openai", "last_seen_at": now - 5},
    )
    cache.set_account_provider("stale-account", "stale-provider")
    cache.set_account_provider("fresh-account", "fresh-provider")
    cache.add_account_support("shared-model", "stale-account")
    cache.add_account_support("shared-model", "fresh-account")

    cache.hydrate_account_refresh_ages()

    assert cache.is_account_stale("stale-account", 60)
    assert not cache.is_account_stale("fresh-account", 60)


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


def test_normalize_models_auto_detect_anthropic_pagination_keys() -> None:
    """Responses with Anthropic pagination keys are detected as Anthropic."""
    raw = {"first_id": "model-a", "has_more": True, "data": [{"id": "model-a"}]}
    models = normalize_models(raw)
    assert models[0]["protocol"] == "anthropic"


def test_normalize_models_auto_detect_display_name_without_object() -> None:
    """Items with display_name and no object field signal Anthropic shape."""
    raw = {"data": [{"id": "model-a", "display_name": "Model A"}]}
    models = normalize_models(raw)
    assert models[0]["protocol"] == "anthropic"


def test_normalize_models_auto_detect_openai_object_field_not_anthropic() -> None:
    """Items with object field are OpenAI-shaped even if they have display_name."""
    raw = {"data": [{"id": "model-a", "display_name": "Model A", "object": "model"}]}
    models = normalize_models(raw)
    assert models[0]["protocol"] is None


def test_normalize_models_auto_detect_empty_data_falls_back_to_openai() -> None:
    """Empty data list should not trigger Anthropic detection."""
    raw = {"data": []}
    models = normalize_models(raw)
    assert models == []


def test_normalizers_skip_malformed_rows_without_losing_valid_models() -> None:
    raw = {
        "data": [
            None,
            "not-an-object",
            {"id": 123},
            {"id": "  "},
            {"id": "valid-model", "name": 123, "title": "Valid"},
        ]
    }

    models = normalize_openai_models(raw)

    assert [model["model_id"] for model in models] == ["valid-model"]
    assert models[0]["display_name"] == "Valid"


def test_normalizers_reject_non_list_data() -> None:
    assert normalize_openai_models({"data": {"id": "nested"}}) == []
    assert normalize_anthropic_models({"data": "not-a-list"}) == []


def test_cache_update_from_account() -> None:
    cache = ModelCatalogCache()
    models = [
        {"model_id": "gpt-4", "protocol": "openai"},
        {"model_id": "claude-3", "protocol": "anthropic"},
    ]
    cache.update_from_account("account1", "opencode-go", models)
    assert cache.model_count == 2
    assert cache.get_supporting_accounts("gpt-4") == {"account1"}


def test_cache_canonical_provider_entry_reuses_global_metadata() -> None:
    cache = ModelCatalogCache()
    cache.update_from_account(
        "account1", "opencode-go", [{"model_id": "gpt-4", "protocol": "openai"}]
    )

    assert cache.get_provider_model_entry("gpt-4", "opencode-go") is cache.get_model(
        "gpt-4"
    )


def test_cache_later_provider_keeps_distinct_metadata() -> None:
    cache = ModelCatalogCache()
    cache.update_from_account(
        "account1", "provider-a", [{"model_id": "gpt-4", "protocol": "openai"}]
    )
    cache.update_from_account(
        "account2", "provider-b", [{"model_id": "gpt-4", "protocol": "openai"}]
    )

    assert cache.get_provider_model_entry("gpt-4", "provider-a") is cache.get_model(
        "gpt-4"
    )
    assert cache.get_provider_model_entry("gpt-4", "provider-b") is not cache.get_model(
        "gpt-4"
    )


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
    """Default update is non-destructive: an empty refresh must not
    silently de-pool a healthy account. The destructive path requires
    ``authoritative=True, allow_withdrawals=True``."""
    cache = ModelCatalogCache()
    cache.update_from_account(
        "acct1", "opencode-go", [{"model_id": "gpt-4", "protocol": "openai"}]
    )
    cache.update_from_account(
        "acct2", "opencode-go", [{"model_id": "gpt-4", "protocol": "openai"}]
    )

    # Default (non-destructive): empty refresh preserves prior support.
    result = cache.update_from_account("acct1", "opencode-go", [])
    assert cache.get_supporting_accounts("gpt-4") == {"acct1", "acct2"}
    assert result.preserved_support == 1
    assert result.withdrawn_support == 0

    # Explicit destructive path removes support.
    result = cache.update_from_account(
        "acct1",
        "opencode-go",
        [],
        authoritative=True,
        allow_withdrawals=True,
    )
    assert cache.get_supporting_accounts("gpt-4") == {"acct2"}
    assert result.withdrawn_support == 1
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


def test_parse_model_id_strips_whitespace() -> None:
    """Regression test (L8): leading/trailing whitespace must be
    normalized away before parsing.
    """
    base, provider = parse_model_id("  gpt-4/opencode-go  ", {"opencode-go"})
    assert base == "gpt-4"
    assert provider == "opencode-go"

    base, provider = parse_model_id("claude-opus-4\n", {"opencode-go"})
    assert base == "claude-opus-4"
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


def test_provider_suffixed_models_do_not_leak_protocols() -> None:
    """An unresolved provider entry must not borrow another provider's protocol."""
    cache = ModelCatalogCache()
    cache.update_from_account(
        "acct1",
        "provider-a",
        [{"model_id": "shared-model", "protocol": "openai"}],
    )
    cache.update_from_account(
        "acct2",
        "provider-b",
        [{"model_id": "shared-model", "protocol": None}],
    )

    suffixed = cache.get_provider_suffixed_models("union", {"acct1", "acct2"})
    suffixed_ids = {model["model_id"] for model in suffixed}
    assert suffixed_ids == {"shared-model/provider-a"}

    unsuffixed = cache.get_models_for_exposure("union", {"acct1", "acct2"})
    assert [model["model_id"] for model in unsuffixed] == ["shared-model"]


def test_unsuffixed_exposure_skips_unresolved_only_provider() -> None:
    """Do not expose a model when the only visible provider entry is unresolved."""
    cache = ModelCatalogCache()
    cache.update_from_account(
        "acct1",
        "provider-a",
        [{"model_id": "shared-model", "protocol": "openai"}],
    )
    cache.update_from_account(
        "acct2",
        "provider-b",
        [{"model_id": "shared-model", "protocol": None}],
    )

    exposed = cache.get_models_for_exposure("union", {"acct2"})
    assert exposed == []


# ---------------------------------------------------------------------------
# Effective/discovered limits integration
# ---------------------------------------------------------------------------


def test_effective_limits_survive_update_from_account() -> None:
    """Provider-specific effective limits persist through update_from_account."""
    cache = ModelCatalogCache()
    cache.update_from_account(
        "acct1",
        "provider-a",
        [
            {
                "model_id": "gpt-4",
                "protocol": "openai",
                "effective_limits": {
                    "context_tokens": 8192,
                    "input_tokens": None,
                    "output_tokens": 4096,
                    "enforce": True,
                    "context_source": "provider_override",
                    "input_source": "unknown",
                    "output_source": "provider_override",
                },
                "discovered_limits": {
                    "context_tokens": 128000,
                    "input_tokens": None,
                    "output_tokens": None,
                },
            }
        ],
    )
    entries = cache.get_provider_model_entries()
    entry = entries[("gpt-4", "provider-a")]
    assert entry["effective_limits"]["context_tokens"] == 8192
    assert entry["effective_limits"]["output_tokens"] == 4096
    assert entry["discovered_limits"]["context_tokens"] == 128000


def test_two_providers_retain_different_limits() -> None:
    """Two providers can store different effective limits for one base model."""
    cache = ModelCatalogCache()
    cache.update_from_account(
        "acct1",
        "provider-a",
        [
            {
                "model_id": "m1",
                "protocol": "openai",
                "effective_limits": {"context_tokens": 100000},
            }
        ],
    )
    cache.update_from_account(
        "acct2",
        "provider-b",
        [
            {
                "model_id": "m1",
                "protocol": "openai",
                "effective_limits": {"context_tokens": 500000},
            }
        ],
    )
    entries = cache.get_provider_model_entries()
    assert entries[("m1", "provider-a")]["effective_limits"]["context_tokens"] == 100000
    assert entries[("m1", "provider-b")]["effective_limits"]["context_tokens"] == 500000


def test_suffixed_exposure_returns_exact_provider_limits() -> None:
    """Suffixed model exposure carries the provider's exact effective limits."""
    cache = ModelCatalogCache()
    cache.update_from_account(
        "acct1",
        "provider-a",
        [
            {
                "model_id": "m1",
                "protocol": "openai",
                "effective_limits": {"context_tokens": 100000},
            }
        ],
    )
    cache.update_from_account(
        "acct2",
        "provider-b",
        [
            {
                "model_id": "m1",
                "protocol": "openai",
                "effective_limits": {"context_tokens": 500000},
            }
        ],
    )
    result = cache.get_provider_suffixed_models("union", {"acct1", "acct2"})
    by_id = {m["model_id"]: m for m in result}
    assert by_id["m1/provider-a"]["effective_limits"]["context_tokens"] == 100000
    assert by_id["m1/provider-b"]["effective_limits"]["context_tokens"] == 500000


def test_unsuffixed_exposure_returns_conservative_minimum() -> None:
    """Unsuffixed exposure uses the conservative minimum across providers."""
    cache = ModelCatalogCache()
    cache.update_from_account(
        "acct1",
        "provider-a",
        [
            {
                "model_id": "m1",
                "protocol": "openai",
                "effective_limits": {"context_tokens": 100000},
            }
        ],
    )
    cache.update_from_account(
        "acct2",
        "provider-b",
        [
            {
                "model_id": "m1",
                "protocol": "openai",
                "effective_limits": {"context_tokens": 500000},
            }
        ],
    )
    result = cache.get_models_for_exposure("union", {"acct1", "acct2"})
    assert len(result) == 1
    assert result[0]["effective_limits"]["context_tokens"] == 100000


def test_account_iteration_order_does_not_change_exposed_limit() -> None:
    """Conservative merge is order-independent."""
    cache = ModelCatalogCache()
    # Insert in one order
    cache.update_from_account(
        "acct1",
        "provider-a",
        [
            {
                "model_id": "m1",
                "protocol": "openai",
                "effective_limits": {"context_tokens": 500000},
            }
        ],
    )
    cache.update_from_account(
        "acct2",
        "provider-b",
        [
            {
                "model_id": "m1",
                "protocol": "openai",
                "effective_limits": {"context_tokens": 100000},
            }
        ],
    )
    result1 = cache.get_models_for_exposure("union", {"acct1", "acct2"})[0][
        "effective_limits"
    ]["context_tokens"]

    # Reverse insertion order
    cache2 = ModelCatalogCache()
    cache2.update_from_account(
        "acct2",
        "provider-b",
        [
            {
                "model_id": "m1",
                "protocol": "openai",
                "effective_limits": {"context_tokens": 100000},
            }
        ],
    )
    cache2.update_from_account(
        "acct1",
        "provider-a",
        [
            {
                "model_id": "m1",
                "protocol": "openai",
                "effective_limits": {"context_tokens": 500000},
            }
        ],
    )
    result2 = cache2.get_models_for_exposure("union", {"acct1", "acct2"})[0][
        "effective_limits"
    ]["context_tokens"]

    assert result1 == result2 == 100000


def test_stale_provider_removal_updates_conservative_limits() -> None:
    """Removing a provider updates the conservative minimum correctly."""
    cache = ModelCatalogCache()
    cache.update_from_account(
        "acct1",
        "provider-a",
        [
            {
                "model_id": "m1",
                "protocol": "openai",
                "effective_limits": {"context_tokens": 100000},
            }
        ],
    )
    cache.update_from_account(
        "acct2",
        "provider-b",
        [
            {
                "model_id": "m1",
                "protocol": "openai",
                "effective_limits": {"context_tokens": 500000},
            }
        ],
    )
    # Both visible
    result = cache.get_models_for_exposure("union", {"acct1", "acct2"})
    assert result[0]["effective_limits"]["context_tokens"] == 100000

    # Remove provider-a (low limit)
    cache.mark_account_models_unavailable("acct1")
    result = cache.get_models_for_exposure("union", {"acct2"})
    assert result[0]["effective_limits"]["context_tokens"] == 500000


# ── collapse_models / routing_priority extension catalog tests ──────────
# These cover plan lines 386-391: catalog-layer behavior for
# ``collapse_models`` switching and the new ``providers`` /
# ``routing_priority_max`` extension fields on collapsed entries.


def test_collapsed_entry_includes_providers_list() -> None:
    """``get_models_for_exposure`` annotates each collapsed entry with
    the sorted list of contributing provider IDs so the API layer can
    surface ``eggpool.providers``."""
    cache = ModelCatalogCache()
    cache.update_from_account(
        "a1",
        "opencode-go",
        [{"model_id": "gpt-4", "protocol": "openai"}],
    )
    cache.update_from_account(
        "b1",
        "minimax",
        [{"model_id": "gpt-4", "protocol": "openai"}],
    )
    cache.update_from_account(
        "c1",
        "generalcompute",
        [{"model_id": "gpt-4", "protocol": "openai"}],
    )
    result = cache.get_models_for_exposure("union", {"a1", "b1", "c1"})
    assert len(result) == 1
    # Providers list is sorted lexicographically for stable output.
    assert result[0]["providers"] == ["generalcompute", "minimax", "opencode-go"]


def test_collapsed_entry_providers_excludes_uneligible() -> None:
    """``providers`` lists only those whose accounts are eligible."""
    cache = ModelCatalogCache()
    cache.update_from_account(
        "a1",
        "opencode-go",
        [{"model_id": "gpt-4", "protocol": "openai"}],
    )
    cache.update_from_account(
        "b1",
        "minimax",
        [{"model_id": "gpt-4", "protocol": "openai"}],
    )
    result = cache.get_models_for_exposure("union", {"a1"})
    # Only opencode-go is eligible; minimax must not appear in providers.
    assert result[0]["providers"] == ["opencode-go"]


def test_expose_mode_intersection_with_collapsing() -> None:
    """Plan line 391: ``expose_mode = intersection`` is still respected
    under collapse. The collapsed entry only appears when every eligible
    account supports the model."""
    cache = ModelCatalogCache()
    cache.update_from_account(
        "a1",
        "opencode-go",
        [{"model_id": "gpt-4", "protocol": "openai"}],
    )
    # minimax only supports claude-3, not gpt-4
    cache.update_from_account(
        "b1",
        "minimax",
        [{"model_id": "claude-3", "protocol": "anthropic"}],
    )
    result = cache.get_models_for_exposure("intersection", {"a1", "b1"})
    ids = {m["model_id"] for m in result}
    # Neither model is supported by every eligible account.
    assert "gpt-4" not in ids
    assert "claude-3" not in ids


def test_expose_mode_healthy_union_with_collapsing() -> None:
    """Plan line 391: ``expose_mode = healthy_union`` still applies to
    the collapsed entry (it is a union of all providers supporting the
    model)."""
    cache = ModelCatalogCache()
    cache.update_from_account(
        "a1",
        "opencode-go",
        [{"model_id": "gpt-4", "protocol": "openai"}],
    )
    cache.update_from_account(
        "b1",
        "minimax",
        [{"model_id": "gpt-4", "protocol": "openai"}],
    )
    result = cache.get_models_for_exposure("healthy_union", {"a1", "b1"})
    assert len(result) == 1
    assert result[0]["model_id"] == "gpt-4"
    assert set(result[0]["providers"]) == {"minimax", "opencode-go"}


def test_suffixed_exposure_does_not_emit_providers_list() -> None:
    """``get_provider_suffixed_models`` must not add a ``providers`` list
    on suffixed entries; that field is only meaningful for collapsed
    entries."""
    cache = ModelCatalogCache()
    cache.update_from_account(
        "a1",
        "opencode-go",
        [{"model_id": "gpt-4", "protocol": "openai"}],
    )
    cache.update_from_account(
        "b1",
        "minimax",
        [{"model_id": "gpt-4", "protocol": "openai"}],
    )
    result = cache.get_provider_suffixed_models("union", {"a1", "b1"})
    assert len(result) == 2
    for entry in result:
        assert "providers" not in entry


# ---------------------------------------------------------------------------
# Cache prune tests
# ---------------------------------------------------------------------------


def test_prune_unused_drops_orphan_models() -> None:
    """A model supported by no account and no provider must be removed.

    With the non-destructive default, an empty refresh preserves prior
    support, so prune_unused leaves the rows alone. The destructive
    path (authoritative + allow_withdrawals) is what the prune pass
    is meant to clean up.
    """
    cache = ModelCatalogCache()
    cache.update_from_account(
        "acct1",
        "provider-a",
        [
            {"model_id": "gpt-4", "protocol": "openai"},
            {"model_id": "withdrawn", "protocol": "openai"},
        ],
    )
    # Simulate the model being withdrawn upstream with the explicit
    # destructive flags so the cache can converge with the live catalog.
    cache.update_from_account(
        "acct1",
        "provider-a",
        [],
        authoritative=True,
        allow_withdrawals=True,
    )

    pruned = cache.prune_unused()

    # Both models lose their only account and their only provider row,
    # so both must be pruned.
    assert pruned == 2
    assert cache.get_model("withdrawn") is None
    assert cache.get_supporting_accounts_for_model("withdrawn") == set()
    assert cache.get_model("gpt-4") is None
    assert cache.get_supporting_accounts_for_model("gpt-4") == set()


def test_prune_unused_keeps_models_with_remaining_support() -> None:
    """A model with at least one supporting account is not pruned.

    The destructive path leaves ``shared`` with a remaining account
    (``acct2`` on provider-b) so the prune pass has nothing to do.
    """
    cache = ModelCatalogCache()
    cache.update_from_account(
        "acct1",
        "provider-a",
        [{"model_id": "shared", "protocol": "openai"}],
    )
    cache.update_from_account(
        "acct2",
        "provider-b",
        [{"model_id": "shared", "protocol": "openai"}],
    )
    cache.update_from_account(
        "acct1",
        "provider-a",
        [],
        authoritative=True,
        allow_withdrawals=True,
    )

    pruned = cache.prune_unused()

    assert pruned == 0
    assert cache.has_model("shared")
    assert cache.get_supporting_accounts_for_model("shared") == {"acct2"}


def test_prune_unused_keeps_models_with_provider_entries() -> None:
    """A model with an active per-provider row is preserved."""
    cache = ModelCatalogCache()
    cache.update_from_account(
        "acct1",
        "provider-a",
        [{"model_id": "shared", "protocol": "openai"}],
    )
    # Manually mark the account as unref'd but leave the provider row.
    cache.mark_account_models_unavailable("acct1")
    cache.set_provider_model_entry(
        "shared",
        "provider-a",
        {"protocol": "openai", "last_seen_at": 0.0},
    )

    pruned = cache.prune_unused()

    assert pruned == 0
    assert cache.has_model("shared")


def test_prune_unused_noop_when_cache_empty() -> None:
    """Pruning an empty cache returns 0 and is safe."""
    cache = ModelCatalogCache()
    assert cache.prune_unused() == 0
