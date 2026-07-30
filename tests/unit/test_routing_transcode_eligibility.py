"""Pure unit tests for routing transcoding eligibility and scoring."""

from __future__ import annotations

from eggpool.accounts.state import AccountRuntimeState
from eggpool.catalog.cache import ModelCatalogCache
from eggpool.models.config import AppConfig
from eggpool.quota.scorer import QuotaFairScorer, RoutingScore
from eggpool.routing.eligibility import get_eligible_accounts

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_config_with_two_providers() -> AppConfig:
    """Config with two providers: one OpenAI-only, one Anthropic-only."""
    return AppConfig.model_validate(
        {
            "server": {"api_key_env": "TEST_KEY", "host": "127.0.0.1", "port": 0},
            "database": {"path": ":memory:"},
            "providers": {
                "openai-provider": {
                    "id": "openai-provider",
                    "base_url": "https://api.openai.com/v1",
                    "protocols": ["openai"],
                    "accounts": [{"name": "openai-acct", "api_key_env": "TEST_KEY"}],
                },
                "anthropic-provider": {
                    "id": "anthropic-provider",
                    "base_url": "https://api.anthropic.com",
                    "protocols": ["anthropic"],
                    "accounts": [{"name": "anthropic-acct", "api_key_env": "TEST_KEY"}],
                },
            },
            "dashboard": {"enabled": False},
            "models": {"startup_refresh": False},
        }
    )


def _make_config_with_three_providers() -> AppConfig:
    """Config with three providers supporting OpenAI, Anthropic, or both."""
    return AppConfig.model_validate(
        {
            "server": {"api_key_env": "TEST_KEY", "host": "127.0.0.1", "port": 0},
            "database": {"path": ":memory:"},
            "providers": {
                "openai-provider": {
                    "id": "openai-provider",
                    "base_url": "https://api.openai.com/v1",
                    "protocols": ["openai"],
                    "accounts": [{"name": "openai-acct", "api_key_env": "TEST_KEY"}],
                },
                "anthropic-provider": {
                    "id": "anthropic-provider",
                    "base_url": "https://api.anthropic.com",
                    "protocols": ["anthropic"],
                    "accounts": [{"name": "anthropic-acct", "api_key_env": "TEST_KEY"}],
                },
                "both-provider": {
                    "id": "both-provider",
                    "base_url": "https://api.example.com",
                    "protocols": ["openai", "anthropic"],
                    "accounts": [{"name": "both-acct", "api_key_env": "TEST_KEY"}],
                },
            },
            "dashboard": {"enabled": False},
            "models": {"startup_refresh": False},
        }
    )


def _build_cache(
    config: AppConfig,
    model_id: str,
    protocol: str,
    supporting_accounts: set[str],
) -> ModelCatalogCache:
    """Build a ModelCatalogCache with a single model and specified accounts.

    Sets the config so provider-level protocol lookups work, and maps
    each supporting account to its provider.
    """
    cache = ModelCatalogCache()
    cache.set_config(config)
    cache.load_model(
        model_id=model_id,
        display_name=model_id,
        protocol=protocol,
        capabilities={},
        source_metadata={},
    )
    for acct in supporting_accounts:
        cache.add_account_support(model_id, acct)
        provider_id = _account_provider_map(config).get(acct)
        if provider_id is not None:
            cache.set_account_provider(acct, provider_id)
    return cache


def _account_provider_map(config: AppConfig) -> dict[str, str]:
    """Build account_name -> provider_id from config."""
    mapping: dict[str, str] = {}
    for provider_id, provider_cfg in config.providers.items():
        for acct in provider_cfg.accounts:
            mapping[acct.name] = provider_id
    return mapping


# ---------------------------------------------------------------------------
# Eligibility tests
# ---------------------------------------------------------------------------


class TestEligibilityTranscode:
    """Test that eligibility filtering works with transcode_eligibility."""

    def test_native_protocol_account_included(self) -> None:
        """Accounts supporting the requested protocol directly are included."""

        config = _make_config_with_two_providers()
        cache = _build_cache(config, "gpt-4", "openai", {"openai-acct"})

        states = [
            AccountRuntimeState(name="openai-acct", enabled=True),
            AccountRuntimeState(name="anthropic-acct", enabled=True),
        ]
        # Only openai-acct supports the model
        result = get_eligible_accounts(
            states,
            "gpt-4",
            cache,
            protocol="openai",
            account_supports_protocol=lambda name, proto: (
                name == "openai-acct" and proto == "openai"
            ),
        )
        names = [s.name for s in result]
        assert "openai-acct" in names
        assert "anthropic-acct" not in names

    def test_transcode_eligibility_widens_candidates(self) -> None:
        """When transcode_eligibility is set, accounts supporting any
        protocol in the set are included.

        The catalog model is registered with protocol "openai" (matching
        the client), but the only supporting account's provider natively
        serves "anthropic".  Without transcode_eligibility the account is
        excluded because ``account_supports_protocol`` rejects it.  With
        transcode_eligibility containing "anthropic" the eligibility
        filter allows it through.
        """

        config = _make_config_with_two_providers()
        # Model cataloged as "openai" (matches client protocol) but
        # only supported by the anthropic account.
        cache = _build_cache(config, "claude-3", "openai", {"anthropic-acct"})

        states = [
            AccountRuntimeState(name="openai-acct", enabled=True),
            AccountRuntimeState(name="anthropic-acct", enabled=True),
        ]

        def _supports(name: str, proto: str) -> bool:
            return (name == "openai-acct" and proto == "openai") or (
                name == "anthropic-acct" and proto == "anthropic"
            )

        # Without transcode_eligibility, anthropic-acct is excluded
        # because _supports("anthropic-acct", "openai") is False.
        result_native = get_eligible_accounts(
            states,
            "claude-3",
            cache,
            protocol="openai",
            account_supports_protocol=_supports,
        )
        assert len(result_native) == 0

        # With transcode_eligibility, anthropic-acct is included because
        # _supports("anthropic-acct", "anthropic") is True and
        # "anthropic" is in the transcode_eligibility set.
        result_transcode = get_eligible_accounts(
            states,
            "claude-3",
            cache,
            protocol="openai",
            transcode_eligibility={"openai", "anthropic"},
            account_supports_protocol=_supports,
        )
        names = [s.name for s in result_transcode]
        assert "anthropic-acct" in names

    def test_transcode_eligibility_none_excludes_mismatched(
        self,
    ) -> None:
        """When transcode_eligibility is None, mismatched accounts are excluded."""

        config = _make_config_with_two_providers()
        cache = _build_cache(config, "claude-3", "anthropic", {"anthropic-acct"})

        states = [
            AccountRuntimeState(name="openai-acct", enabled=True),
            AccountRuntimeState(name="anthropic-acct", enabled=True),
        ]

        def _supports(name: str, proto: str) -> bool:
            return (name == "openai-acct" and proto == "openai") or (
                name == "anthropic-acct" and proto == "anthropic"
            )

        result = get_eligible_accounts(
            states,
            "claude-3",
            cache,
            protocol="openai",
            transcode_eligibility=None,
            account_supports_protocol=_supports,
        )
        # anthropic-acct doesn't support "openai" and
        # transcode_eligibility is None → no accounts match
        assert len(result) == 0


# ---------------------------------------------------------------------------
# Scorer tests (prefer_native)
# ---------------------------------------------------------------------------


class TestScorerPreferNative:
    """Test that prefer_native affects ranking order."""

    def test_prefer_native_ranks_native_above_transcode(self) -> None:
        """Native-protocol accounts rank above transcodable ones."""
        scorer = QuotaFairScorer(prefer_native=True)
        native = RoutingScore(
            account_name="native-acct",
            quota_score=0.5,
            weight=1.0,
            is_eligible=True,
            requires_transcode=False,
        )
        transcode = RoutingScore(
            account_name="transcode-acct",
            quota_score=0.5,
            weight=1.0,
            is_eligible=True,
            requires_transcode=True,
        )
        ranked = scorer.rank_accounts([transcode, native])
        assert ranked[0].account_name == "native-acct"
        assert ranked[1].account_name == "transcode-acct"

    def test_prefer_native_false_allows_transcode_ahead(
        self,
    ) -> None:
        """With prefer_native=False, transcodable accounts can outrank native ones."""
        scorer = QuotaFairScorer(prefer_native=False)
        native = RoutingScore(
            account_name="native-acct",
            quota_score=0.5,
            weight=1.0,
            is_eligible=True,
            requires_transcode=False,
        )
        transcode = RoutingScore(
            account_name="transcode-acct",
            quota_score=0.1,
            weight=1.0,
            is_eligible=True,
            requires_transcode=True,
        )
        ranked = scorer.rank_accounts([native, transcode])
        # transcode has lower score -> ranks first regardless
        assert ranked[0].account_name == "transcode-acct"

    def test_select_account_prefers_native(self) -> None:
        """select_account returns native account when scores are tied."""
        scorer = QuotaFairScorer(prefer_native=True, tiebreaker_range=0.0)
        native = RoutingScore(
            account_name="native-acct",
            quota_score=0.5,
            weight=1.0,
            is_eligible=True,
            requires_transcode=False,
        )
        transcode = RoutingScore(
            account_name="transcode-acct",
            quota_score=0.5,
            weight=1.0,
            is_eligible=True,
            requires_transcode=True,
        )
        selected = scorer.select_account([transcode, native])
        assert selected is not None
        assert selected.account_name == "native-acct"


# ---------------------------------------------------------------------------
# Cache helper tests
# ---------------------------------------------------------------------------


class TestCacheCountEligibleAccounts:
    """Test count_eligible_accounts_for_protocol."""

    def test_counts_accounts_with_matching_protocol(self) -> None:
        config = _make_config_with_three_providers()
        cache = _build_cache(config, "gpt-4", "openai", {"openai-acct", "both-acct"})

        count_openai = cache.count_eligible_accounts_for_protocol("gpt-4", "openai")
        assert count_openai == 2  # openai-acct and both-acct

        count_anthropic = cache.count_eligible_accounts_for_protocol(
            "gpt-4", "anthropic"
        )
        assert count_anthropic == 1  # only both-acct

    def test_returns_zero_for_unknown_protocol(self) -> None:
        config = _make_config_with_two_providers()
        cache = _build_cache(config, "gpt-4", "openai", {"openai-acct"})

        count = cache.count_eligible_accounts_for_protocol("gpt-4", "google")
        assert count == 0

    def test_returns_zero_for_unknown_model(self) -> None:
        config = _make_config_with_two_providers()
        cache = _build_cache(config, "gpt-4", "openai", {"openai-acct"})

        count = cache.count_eligible_accounts_for_protocol("unknown-model", "openai")
        assert count == 0


class TestCacheGetTranscodableProtocols:
    """Test get_transcodable_protocols returns correct protocol sets."""

    def test_returns_protocols_other_than_client(self) -> None:
        config = _make_config_with_three_providers()
        cache = _build_cache(config, "gpt-4", "openai", {"openai-acct", "both-acct"})

        result = cache.get_transcodable_protocols("gpt-4", client_protocol="openai")
        # both-provider has ["openai", "anthropic"],
        # minus client "openai" -> {"anthropic"}
        assert "anthropic" in result
        assert "openai" not in result

    def test_returns_empty_when_only_native_protocol(self) -> None:
        config = _make_config_with_two_providers()
        cache = _build_cache(config, "gpt-4", "openai", {"openai-acct"})

        result = cache.get_transcodable_protocols("gpt-4", client_protocol="openai")
        assert result == set()
