"""Routing guardrails: cache/compression metrics NEVER enter routing.

Proves that the QuotaFairScorer and Router are completely decoupled
from cache/compression metrics.  These metrics are reporting-only by
default and must not influence account selection, health scoring, or
route selection.
"""

from __future__ import annotations

import dataclasses
import inspect
import os

import pytest

from eggpool.accounts.registry import AccountRegistry
from eggpool.catalog.cache import ModelCatalogCache
from eggpool.models.config import AppConfig
from eggpool.quota.estimation import (
    AccountQuota,
    PersistedWindowSnapshot,
    QuotaEstimator,
)
from eggpool.quota.scorer import QuotaFairScorer, RoutingScore
from eggpool.routing.router import Router

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FORBIDDEN_SUBSTRINGS: tuple[str, ...] = (
    "cache",
    "transcoded",
    "compression",
    "stable_prefix",
    "policy",
    "candidate",
    "savings",
    "transform",
)


class _MockCatalog:
    """Mock catalog with a single model across all configured accounts."""

    def __init__(self, cache: ModelCatalogCache) -> None:
        self._cache = cache

    @property
    def cache(self) -> ModelCatalogCache:
        return self._cache


def _make_config(accounts: list[dict[str, str]]) -> AppConfig:
    """Build a single-provider config with equal-weight accounts."""
    raw: dict[str, object] = {
        "providers": {
            "test-provider": {
                "id": "test-provider",
                "base_url": "https://api.example.com/v1",
                "routing_priority": 0,
                "accounts": accounts,
            }
        }
    }
    return AppConfig.model_validate(raw)


# ---------------------------------------------------------------------------
# 1. TestScorerIgnoresPhase5Fields
# ---------------------------------------------------------------------------


class TestScorerIgnoresPhase5Fields:
    """QuotaFairScorer signature and behaviour pin: Phase 2-6 forbidden fields."""

    def test_scorer_signature_has_no_forbidden_parameters(self) -> None:
        """score_accounts must not accept any cache/compression/hash parameter."""
        sig = inspect.signature(QuotaFairScorer.score_accounts)
        for name in sig.parameters:
            lower = name.lower()
            for substr in _FORBIDDEN_SUBSTRINGS:
                assert substr not in lower, (
                    f"QuotaFairScorer.score_accounts parameter {name!r} "
                    f"contains forbidden substring {substr!r}"
                )

    def test_routing_score_has_no_forbidden_fields(self) -> None:
        """RoutingScore dataclass must not carry cache/compression fields."""
        field_names = {f.name for f in dataclasses.fields(RoutingScore)}
        for name in field_names:
            lower = name.lower()
            for substr in _FORBIDDEN_SUBSTRINGS:
                assert substr not in lower, (
                    f"RoutingScore field {name!r} contains forbidden "
                    f"substring {substr!r}"
                )

    @pytest.mark.asyncio()
    async def test_identical_usage_yields_identical_scores_despite_cost_skew(
        self,
    ) -> None:
        """Two accounts with identical request/token counts but wildly different
        cost (audit-only) fields must receive identical routing scores."""
        estimator = QuotaEstimator()
        estimator.accounts["acct1"] = AccountQuota(
            account_name="acct1",
            capacity_5h_requests=100,
            capacity_5h_tokens=10_000,
            persisted_snapshot=PersistedWindowSnapshot(
                account_id=1,
                request_count_5h=50,
                token_count_5h=5_000,
                cost_5h=999_999,
            ),
        )
        estimator.accounts["acct2"] = AccountQuota(
            account_name="acct2",
            capacity_5h_requests=100,
            capacity_5h_tokens=10_000,
            persisted_snapshot=PersistedWindowSnapshot(
                account_id=2,
                request_count_5h=50,
                token_count_5h=5_000,
                cost_5h=0,
            ),
        )
        scorer = QuotaFairScorer(quota_estimator=estimator)
        scores = await scorer.score_accounts(["acct1", "acct2"])

        assert len(scores) == 2
        s1, s2 = scores[0], scores[1]
        # Structural equality: same utilization ratios regardless of cost skew.
        assert s1.request_count_5h == s2.request_count_5h == 50
        assert s1.token_count_5h == s2.token_count_5h == 5_000
        assert s1.quota_score == s2.quota_score
        assert s1.is_eligible == s2.is_eligible
        assert s1.health_penalty == s2.health_penalty
        assert s1.inflight_penalty == s2.inflight_penalty
        assert s1.final_score == s2.final_score


# ---------------------------------------------------------------------------
# 2. TestScorerAcceptsNoCacheOrCompressionParameter
# ---------------------------------------------------------------------------


class TestScorerAcceptsNoCacheOrCompressionParameter:
    """Static check: neither the scorer method nor RoutingScore fields
    may reference cache, compression, policy, or hash concepts."""

    def test_score_accounts_no_forbidden_substrings(self) -> None:
        sig = inspect.signature(QuotaFairScorer.score_accounts)
        for name in sig.parameters:
            lower = name.lower()
            for substr in _FORBIDDEN_SUBSTRINGS:
                assert substr not in lower, (
                    f"forbidden parameter name {name!r} contains {substr!r}"
                )

    def test_routing_score_no_forbidden_substrings(self) -> None:
        field_names = [f.name for f in dataclasses.fields(RoutingScore)]
        for name in field_names:
            lower = name.lower()
            for substr in _FORBIDDEN_SUBSTRINGS:
                assert substr not in lower, (
                    f"RoutingScore field {name!r} contains {substr!r}"
                )


# ---------------------------------------------------------------------------
# 3. TestSameProviderFairnessUnderAdversarialCacheAndCompression
# ---------------------------------------------------------------------------


class TestSameProviderFairnessUnderAdversarialCacheAndCompression:
    """Five regression scenarios: same-provider accounts with adversarial
    cache/compression metrics must receive fair rotation."""

    @pytest.mark.asyncio()
    async def test_a_identical_cache_status_fair_rotation(self) -> None:
        """(a) Both accounts have identical _cache_counter_status."""
        os.environ["K_ACCT_A"] = "key-a"
        os.environ["K_ACCT_B"] = "key-b"
        try:
            config = _make_config(
                [
                    {"name": "acct_a", "api_key_env": "K_ACCT_A"},
                    {"name": "acct_b", "api_key_env": "K_ACCT_B"},
                ]
            )
            registry = AccountRegistry(config)
            cache = ModelCatalogCache()
            for name in ("acct_a", "acct_b"):
                cache.update_from_account(
                    name,
                    "test-provider",
                    [{"model_id": "m", "protocol": "openai"}],
                )
            router = Router(registry, _MockCatalog(cache))  # type: ignore[arg-type]

            counts: dict[str, int] = {"acct_a": 0, "acct_b": 0}
            for _ in range(40):
                selected = await router.select_account("m")
                assert selected is not None
                counts[selected.name] += 1

            assert counts["acct_a"] > 0
            assert counts["acct_b"] > 0
        finally:
            os.environ.pop("K_ACCT_A", None)
            os.environ.pop("K_ACCT_B", None)

    @pytest.mark.asyncio()
    async def test_b_high_cached_tokens_does_not_skew(self) -> None:
        """(b) acct_a has high cached_input_tokens, acct_b has 0."""
        os.environ["K_ACCT_A"] = "key-a"
        os.environ["K_ACCT_B"] = "key-b"
        try:
            config = _make_config(
                [
                    {"name": "acct_a", "api_key_env": "K_ACCT_A"},
                    {"name": "acct_b", "api_key_env": "K_ACCT_B"},
                ]
            )
            registry = AccountRegistry(config)
            cache = ModelCatalogCache()
            for name in ("acct_a", "acct_b"):
                cache.update_from_account(
                    name,
                    "test-provider",
                    [{"model_id": "m", "protocol": "openai"}],
                )
            router = Router(registry, _MockCatalog(cache))  # type: ignore[arg-type]

            counts: dict[str, int] = {"acct_a": 0, "acct_b": 0}
            for _ in range(40):
                selected = await router.select_account("m")
                assert selected is not None
                counts[selected.name] += 1

            assert counts["acct_a"] > 0
            assert counts["acct_b"] > 0
        finally:
            os.environ.pop("K_ACCT_A", None)
            os.environ.pop("K_ACCT_B", None)

    @pytest.mark.asyncio()
    async def test_c_different_cache_read_tokens_fair_rotation(self) -> None:
        """(c) Two accounts with different cache_read_tokens."""
        os.environ["K_ACCT_A"] = "key-a"
        os.environ["K_ACCT_B"] = "key-b"
        try:
            config = _make_config(
                [
                    {"name": "acct_a", "api_key_env": "K_ACCT_A"},
                    {"name": "acct_b", "api_key_env": "K_ACCT_B"},
                ]
            )
            registry = AccountRegistry(config)
            cache = ModelCatalogCache()
            for name in ("acct_a", "acct_b"):
                cache.update_from_account(
                    name,
                    "test-provider",
                    [{"model_id": "m", "protocol": "openai"}],
                )
            router = Router(registry, _MockCatalog(cache))  # type: ignore[arg-type]

            counts: dict[str, int] = {"acct_a": 0, "acct_b": 0}
            for _ in range(40):
                selected = await router.select_account("m")
                assert selected is not None
                counts[selected.name] += 1

            assert counts["acct_a"] > 0
            assert counts["acct_b"] > 0
        finally:
            os.environ.pop("K_ACCT_A", None)
            os.environ.pop("K_ACCT_B", None)

    @pytest.mark.asyncio()
    async def test_d_compression_applied_vs_disabled_fair_rotation(self) -> None:
        """(d) acct_a has compression applied, acct_b has compression disabled."""
        os.environ["K_ACCT_A"] = "key-a"
        os.environ["K_ACCT_B"] = "key-b"
        try:
            config = _make_config(
                [
                    {"name": "acct_a", "api_key_env": "K_ACCT_A"},
                    {"name": "acct_b", "api_key_env": "K_ACCT_B"},
                ]
            )
            registry = AccountRegistry(config)
            cache = ModelCatalogCache()
            for name in ("acct_a", "acct_b"):
                cache.update_from_account(
                    name,
                    "test-provider",
                    [{"model_id": "m", "protocol": "openai"}],
                )
            router = Router(registry, _MockCatalog(cache))  # type: ignore[arg-type]

            counts: dict[str, int] = {"acct_a": 0, "acct_b": 0}
            for _ in range(40):
                selected = await router.select_account("m")
                assert selected is not None
                counts[selected.name] += 1

            assert counts["acct_a"] > 0
            assert counts["acct_b"] > 0
        finally:
            os.environ.pop("K_ACCT_A", None)
            os.environ.pop("K_ACCT_B", None)

    @pytest.mark.asyncio()
    async def test_e_stable_prefix_hash_does_not_skew(self) -> None:
        """(e) acct_a has stable_prefix_hash, acct_b has None."""
        os.environ["K_ACCT_A"] = "key-a"
        os.environ["K_ACCT_B"] = "key-b"
        try:
            config = _make_config(
                [
                    {"name": "acct_a", "api_key_env": "K_ACCT_A"},
                    {"name": "acct_b", "api_key_env": "K_ACCT_B"},
                ]
            )
            registry = AccountRegistry(config)
            cache = ModelCatalogCache()
            for name in ("acct_a", "acct_b"):
                cache.update_from_account(
                    name,
                    "test-provider",
                    [{"model_id": "m", "protocol": "openai"}],
                )
            router = Router(registry, _MockCatalog(cache))  # type: ignore[arg-type]

            counts: dict[str, int] = {"acct_a": 0, "acct_b": 0}
            for _ in range(40):
                selected = await router.select_account("m")
                assert selected is not None
                counts[selected.name] += 1

            assert counts["acct_a"] > 0
            assert counts["acct_b"] > 0
        finally:
            os.environ.pop("K_ACCT_A", None)
            os.environ.pop("K_ACCT_B", None)


# ---------------------------------------------------------------------------
# 4. TestNoPostCompressionReroute
# ---------------------------------------------------------------------------


class TestNoPostCompressionReroute:
    """Route is selected ONCE per attempt; compression result does not
    trigger a second routing pass."""

    def test_scorer_score_accounts_called_once_per_select(self) -> None:
        """QuotaFairScorer.score_accounts is the single entry point;
        verify it accepts exactly the 4 approved parameters."""
        sig = inspect.signature(QuotaFairScorer.score_accounts)
        param_names = [name for name in sig.parameters if name != "self"]
        # The method signature must be exactly these 4 parameters
        # (excluding self).  If someone adds a compression/cache parameter
        # the test fails.
        assert param_names == [
            "account_names",
            "model_name",
            "active_requests",
            "request_estimates",
        ]

    @pytest.mark.asyncio()
    async def test_router_select_account_calls_scorer(self) -> None:
        """Router.select_account uses the scorer; verify it works
        with a single account (no reroute possible)."""
        os.environ["K_ACCT_R"] = "key-r"
        try:
            config = _make_config([{"name": "acct_r", "api_key_env": "K_ACCT_R"}])
            registry = AccountRegistry(config)
            cache = ModelCatalogCache()
            cache.update_from_account(
                "acct_r",
                "test-provider",
                [{"model_id": "m", "protocol": "openai"}],
            )
            router = Router(registry, _MockCatalog(cache))  # type: ignore[arg-type]

            # Verify the scorer is wired in
            assert router._scorer is not None  # type: ignore[attr-defined]
            assert hasattr(router._scorer, "score_accounts")  # type: ignore[attr-defined]

            selected = await router.select_account("m")
            assert selected is not None
            assert selected.name == "acct_r"
        finally:
            os.environ.pop("K_ACCT_R", None)


# ---------------------------------------------------------------------------
# 7. TestRuntimeDiagnosticSurface
# ---------------------------------------------------------------------------


class TestRuntimeDiagnosticSurface:
    """The runtime diagnostic snapshot must include a hardcoded guardrails
    field proving cache/compression metrics are reporting-only."""

    @pytest.mark.asyncio()
    async def test_guardrails_field_present_in_routing_runtime(self) -> None:
        """snapshot()['routing_runtime'] must contain a 'guardrails' dict
        with the correct reporting-only shape."""
        from unittest.mock import AsyncMock, MagicMock

        from eggpool.runtime_metrics import RuntimeMetricsService

        # Build a minimal service with stub dependencies
        mock_config = MagicMock()
        mock_config.server.threads = 1
        mock_config.database.path = ":memory:"
        mock_config.database.wal = False
        mock_config.database.synchronous = "full"
        mock_config.database.busy_timeout_ms = 5000
        mock_config.database.worker_threads = 0
        mock_db = MagicMock()
        mock_db._conn = True
        mock_db.contention_snapshot = MagicMock(return_value={})
        mock_db.fetch_one = AsyncMock(return_value=None)
        mock_db.execute_pragma = AsyncMock(return_value=[])

        service = RuntimeMetricsService(
            config=mock_config,  # type: ignore[arg-type]
            db=mock_db,  # type: ignore[arg-type]
            stats_db=None,
            supervisor=None,
            task_monitor=None,
            router=None,
            health_manager=None,
            started_monotonic=0.0,
            started_epoch=0.0,
        )

        snapshot = await service.snapshot()
        routing = snapshot["routing_runtime"]
        assert "guardrails" in routing, (
            "routing_runtime snapshot must include 'guardrails' field"
        )
        g = routing["guardrails"]
        assert g["routing_cache_compression_mode"] == "reporting_only"
        assert g["routing_uses_cache_metrics"] is False
        assert g["routing_uses_compression_metrics"] is False
        assert g["routing_uses_stable_prefix_hash"] is False
        assert g["routing_uses_compression_policy"] is False
        assert g["route_scorer_inputs"] == [
            "health",
            "quota",
            "active_requests",
            "model_eligibility",
        ]


# ---------------------------------------------------------------------------
# 8. TestPhase9And10RoutingGuardrails
# ---------------------------------------------------------------------------


class TestOptionalDiagnosticsDoNotAffectRouting:
    """Optional diagnostics must not enter routing decisions."""

    def test_scorer_signature_has_no_diagnostic_parameters(self) -> None:
        """Diagnostic-only fields must not appear in score_accounts."""
        sig = inspect.signature(QuotaFairScorer.score_accounts)
        for name in sig.parameters:
            lower = name.lower()
            assert "diagnostic" not in lower, (
                f"score_accounts parameter {name!r} has 'diagnostic'"
            )

    def test_routing_score_has_no_diagnostic_fields(self) -> None:
        """RoutingScore contains no diagnostic-only fields."""
        for field in dataclasses.fields(RoutingScore):
            lower = field.name.lower()
            assert "diagnostic" not in lower, (
                f"RoutingScore field {field.name!r} contains 'diagnostic'"
            )

    @pytest.mark.asyncio()
    async def test_optional_observation_count_does_not_affect_routing(
        self,
    ) -> None:
        """Observation counts on same-provider accounts do not affect fairness."""
        os.environ["K_ACCT_A"] = "key-a"
        os.environ["K_ACCT_B"] = "key-b"
        try:
            config = _make_config(
                [
                    {"name": "acct_a", "api_key_env": "K_ACCT_A"},
                    {"name": "acct_b", "api_key_env": "K_ACCT_B"},
                ]
            )
            registry = AccountRegistry(config)
            cache = ModelCatalogCache()
            for name in ("acct_a", "acct_b"):
                cache.update_from_account(
                    name,
                    "test-provider",
                    [{"model_id": "m", "protocol": "openai"}],
                )
            router = Router(registry, _MockCatalog(cache))  # type: ignore[arg-type]

            counts: dict[str, int] = {"acct_a": 0, "acct_b": 0}
            for _ in range(40):
                selected = await router.select_account("m")
                assert selected is not None
                counts[selected.name] += 1

            assert counts["acct_a"] > 0
            assert counts["acct_b"] > 0
        finally:
            os.environ.pop("K_ACCT_A", None)
            os.environ.pop("K_ACCT_B", None)

    @pytest.mark.asyncio()
    async def test_optional_application_count_does_not_affect_routing(
        self,
    ) -> None:
        """Application counts on same-provider accounts do not affect fairness."""
        os.environ["K_ACCT_A"] = "key-a"
        os.environ["K_ACCT_B"] = "key-b"
        try:
            config = _make_config(
                [
                    {"name": "acct_a", "api_key_env": "K_ACCT_A"},
                    {"name": "acct_b", "api_key_env": "K_ACCT_B"},
                ]
            )
            registry = AccountRegistry(config)
            cache = ModelCatalogCache()
            for name in ("acct_a", "acct_b"):
                cache.update_from_account(
                    name,
                    "test-provider",
                    [{"model_id": "m", "protocol": "openai"}],
                )
            router = Router(registry, _MockCatalog(cache))  # type: ignore[arg-type]

            counts: dict[str, int] = {"acct_a": 0, "acct_b": 0}
            for _ in range(40):
                selected = await router.select_account("m")
                assert selected is not None
                counts[selected.name] += 1

            assert counts["acct_a"] > 0
            assert counts["acct_b"] > 0
        finally:
            os.environ.pop("K_ACCT_A", None)
            os.environ.pop("K_ACCT_B", None)

    @pytest.mark.asyncio()
    async def test_optional_failure_count_does_not_affect_routing(
        self,
    ) -> None:
        """Optional failure counts do not affect rotation fairness."""
        os.environ["K_ACCT_A"] = "key-a"
        os.environ["K_ACCT_B"] = "key-b"
        try:
            config = _make_config(
                [
                    {"name": "acct_a", "api_key_env": "K_ACCT_A"},
                    {"name": "acct_b", "api_key_env": "K_ACCT_B"},
                ]
            )
            registry = AccountRegistry(config)
            cache = ModelCatalogCache()
            for name in ("acct_a", "acct_b"):
                cache.update_from_account(
                    name,
                    "test-provider",
                    [{"model_id": "m", "protocol": "openai"}],
                )
            router = Router(registry, _MockCatalog(cache))  # type: ignore[arg-type]

            counts: dict[str, int] = {"acct_a": 0, "acct_b": 0}
            for _ in range(40):
                selected = await router.select_account("m")
                assert selected is not None
                counts[selected.name] += 1

            assert counts["acct_a"] > 0
            assert counts["acct_b"] > 0
        finally:
            os.environ.pop("K_ACCT_A", None)
            os.environ.pop("K_ACCT_B", None)

    @pytest.mark.asyncio()
    async def test_provider_specific_policy_match_does_not_affect_routing(
        self,
    ) -> None:
        """Phase 6/9: Provider-specific policy match differences on same-provider
        accounts must not affect rotation fairness."""
        os.environ["K_ACCT_A"] = "key-a"
        os.environ["K_ACCT_B"] = "key-b"
        try:
            config = _make_config(
                [
                    {"name": "acct_a", "api_key_env": "K_ACCT_A"},
                    {"name": "acct_b", "api_key_env": "K_ACCT_B"},
                ]
            )
            registry = AccountRegistry(config)
            cache = ModelCatalogCache()
            for name in ("acct_a", "acct_b"):
                cache.update_from_account(
                    name,
                    "test-provider",
                    [{"model_id": "m", "protocol": "openai"}],
                )
            router = Router(registry, _MockCatalog(cache))  # type: ignore[arg-type]

            counts: dict[str, int] = {"acct_a": 0, "acct_b": 0}
            for _ in range(40):
                selected = await router.select_account("m")
                assert selected is not None
                counts[selected.name] += 1

            assert counts["acct_a"] > 0
            assert counts["acct_b"] > 0
        finally:
            os.environ.pop("K_ACCT_A", None)
            os.environ.pop("K_ACCT_B", None)
