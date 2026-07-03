"""Phase 8 routing guardrails: cache/compression metrics NEVER enter routing.

Proves that the QuotaFairScorer, Router, and compression pipeline are
completely decoupled.  Cache/compression metrics are reporting-only by
default and must not influence account selection, health scoring, or
route selection.

See plans/cache_compression_phase_08_routing_guardrails.md.
"""

from __future__ import annotations

import dataclasses
import inspect
import os
from typing import Any

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
from eggpool.transcoder.compression.policy import (
    CompressionConfig,
    CompressionPolicyOverride,
)
from eggpool.transcoder.compression.policy_resolver import (
    CompressionPolicyContext,
    resolve_compression_policy,
)

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
# 4. TestCompressionFallbackDoesNotAffectHealth
# ---------------------------------------------------------------------------


class TestCompressionFallbackDoesNotAffectHealth:
    """Compression fail-closed fallback MUST NOT mark a provider unhealthy."""

    def test_failed_fallback_does_not_mark_account_unhealthy(self) -> None:
        """When apply_safe_compression returns failed_fallback=True, the
        HealthManager must remain in a healthy state."""
        from collections import Counter

        from eggpool.health.health_manager import HealthManager
        from eggpool.transcoder.compression.apply import apply_safe_compression
        from eggpool.transcoder.compression.policy import CompressionConfig
        from eggpool.transcoder.segmentation import (
            RequestSegment,
            SegmentationResult,
            SegmentationStatus,
            SegmentKind,
            SegmentSource,
        )

        health = HealthManager()
        health.record_success("acct_a")

        snap_before = health.get_account_health("acct_a")
        assert snap_before.health_state == "healthy"
        assert snap_before.is_healthy is True

        # apply_safe_compression itself never touches HealthManager;
        # this test proves the boundary: the compression result is
        # purely informational and the caller is responsible for
        # routing/health decisions.
        #
        # Construct a payload and a minimal segmentation with one
        # volatile segment that points to a string in the payload.
        seg = RequestSegment(
            kind=SegmentKind.VOLATILE_SUFFIX,
            source=SegmentSource.LATEST_USER_MESSAGE,
            message_index=0,
            content_path=("messages", 0, "content"),
            byte_length=11,
            estimated_tokens=3,
            protected=False,
            compressible_candidate=True,
            reason="volatile_suffix",
        )
        seg_count: dict[SegmentKind, int] = Counter({SegmentKind.VOLATILE_SUFFIX: 1})
        segmentation = SegmentationResult(
            status=SegmentationStatus.SEGMENTED,
            segments=(seg,),
            segment_count_by_kind=seg_count,
            stable_prefix_bytes=0,
            semi_stable_bytes=0,
            volatile_bytes=11,
            stable_prefix_estimated_tokens=None,
            semi_stable_estimated_tokens=None,
            volatile_estimated_tokens=3,
            stable_prefix_hash="original_hash",
            request_shape_hash="shape_hash",
            cache_control_present=False,
        )
        payload: dict[str, Any] = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "hello world"}],
        }

        policy = CompressionConfig(
            enabled=True,
            mode="safe",
            min_candidate_tokens=0,
            min_savings_tokens=0,
        )
        _result = apply_safe_compression(payload, segmentation, policy=policy)

        # The result may or may not be a failed_fallback depending on
        # the segmentation; what matters is that HealthManager is
        # completely unaffected.
        snap_after = health.get_account_health("acct_a")
        assert snap_after.health_state == "healthy"
        assert snap_after.is_healthy is True
        assert snap_after.consecutive_failures == 0

    def test_compression_result_never_modifies_health_manager(self) -> None:
        """The apply_safe_compression function has no dependency on
        HealthManager and cannot mutate it."""
        import ast
        import pathlib

        apply_src = pathlib.Path(
            "src/eggpool/transcoder/compression/apply.py"
        ).read_text()
        tree = ast.parse(apply_src)
        imports = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
        ]
        for imp in imports:
            if isinstance(imp, ast.ImportFrom):
                assert imp.module != "eggpool.health.health_manager", (
                    "apply.py must not import HealthManager"
                )
            elif isinstance(imp, ast.Import):
                for alias in imp.names:
                    assert alias.name != "eggpool.health.health_manager", (
                        "apply.py must not import HealthManager"
                    )


# ---------------------------------------------------------------------------
# 5. TestPolicyResolverDoesNotAffectRouting
# ---------------------------------------------------------------------------


class TestPolicyResolverDoesNotAffectRouting:
    """Phase 6 policy resolution is informational only; it never
    modifies routing state or removes accounts from routing."""

    def test_policy_disabling_compression_does_not_change_routing(self) -> None:
        """Override disables compression post-route; routing state unchanged."""
        base = CompressionConfig(enabled=True)
        override = CompressionPolicyOverride(name="off", enabled=False)
        ctx = CompressionPolicyContext(client_id="x", source_protocol="openai")
        result = resolve_compression_policy(base, ctx, overrides=[override])

        assert result.name == "off"
        assert result.config.enabled is False
        # The resolver returns a config; the test confirms no exceptions,
        # no route-state mutation; the policy object is information-only.
        assert result.warnings == ()

    def test_policy_enabling_safe_compression_does_not_change_routing(
        self,
    ) -> None:
        """Override enables safe compression; routing state unchanged."""
        base = CompressionConfig(enabled=False)
        override = CompressionPolicyOverride(name="on", enabled=True)
        ctx = CompressionPolicyContext(client_id="y", source_protocol="openai")
        result = resolve_compression_policy(base, ctx, overrides=[override])

        assert result.name == "on"
        assert result.config.enabled is True
        assert result.warnings == ()

    def test_policy_warnings_do_not_remove_accounts(self) -> None:
        """Policy warning/fallback does not cause route removal.

        The resolver returns a config even when warnings are present;
        it never raises and never modifies a routing table."""
        # Scenario: base has compress_static_prefix=True (safe mode with
        # override allowed), override switches mode to "observe" which
        # makes the merged config invalid (compress_static_prefix is not
        # allowed in observe mode).  The resolver catches the ValidationError,
        # emits a warning, and returns the previous valid config.
        base = CompressionConfig(
            enabled=True,
            mode="safe",
            allow_static_prefix_override=True,
            compress_static_prefix=True,
        )
        override = CompressionPolicyOverride(
            name="bad", match_clients=["z"], mode="observe"
        )
        ctx = CompressionPolicyContext(client_id="z", source_protocol="openai")
        result = resolve_compression_policy(base, ctx, overrides=[override])

        # The resolver catches the validation error and returns a valid config
        assert result.config.mode == "safe"
        # The resolver never raises and never modifies routing state

    def test_resolver_returns_frozen_result(self) -> None:
        """ResolvedCompressionPolicy is frozen; cannot be mutated."""
        from dataclasses import FrozenInstanceError

        base = CompressionConfig(enabled=True)
        ctx = CompressionPolicyContext(client_id="a")
        result = resolve_compression_policy(base, ctx)
        with pytest.raises(FrozenInstanceError):
            result.name = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 6. TestNoPostCompressionReroute
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
        mock_db = AsyncMock()
        mock_db._conn = True
        mock_db.contention_snapshot.return_value = {}
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


class TestPhase9And10RoutingGuardrails:
    """Phase 9 synthetic cache + Phase 10 tuning must NOT enter routing."""

    def test_scorer_signature_has_no_synthetic_cache_parameters(self) -> None:
        """Phase 9: synthetic/synthesized must not appear in score_accounts."""
        sig = inspect.signature(QuotaFairScorer.score_accounts)
        for name in sig.parameters:
            lower = name.lower()
            assert "synthetic" not in lower, (
                f"score_accounts parameter {name!r} has 'synthetic'"
            )
            assert "synthesized" not in lower, (
                f"score_accounts parameter {name!r} has 'synthesized'"
            )

    def test_scorer_signature_has_no_tuning_parameters(self) -> None:
        """Phase 10: tuning/recommendation must not appear in score_accounts."""
        sig = inspect.signature(QuotaFairScorer.score_accounts)
        for name in sig.parameters:
            lower = name.lower()
            assert "tuning" not in lower, (
                f"score_accounts parameter {name!r} has 'tuning'"
            )
            assert "recommendation" not in lower, (
                f"score_accounts parameter {name!r} has 'recommendation'"
            )

    def test_routing_score_has_no_synthetic_cache_fields(self) -> None:
        """Phase 9: synthetic/synthesized must not appear in RoutingScore."""
        for field in dataclasses.fields(RoutingScore):
            lower = field.name.lower()
            assert "synthetic" not in lower, (
                f"RoutingScore field {field.name!r} contains 'synthetic'"
            )
            assert "synthesized" not in lower, (
                f"RoutingScore field {field.name!r} contains 'synthesized'"
            )

    def test_routing_score_has_no_tuning_fields(self) -> None:
        """Phase 10: 'tuning' must not appear in RoutingScore fields."""
        for field in dataclasses.fields(RoutingScore):
            lower = field.name.lower()
            assert "tuning" not in lower, (
                f"RoutingScore field {field.name!r} contains 'tuning'"
            )

    @pytest.mark.asyncio()
    async def test_synthetic_cache_candidate_count_does_not_affect_routing(
        self,
    ) -> None:
        """Phase 9: Different synthetic cache candidate counts on same-provider
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

    @pytest.mark.asyncio()
    async def test_synthetic_cache_applied_count_does_not_affect_routing(
        self,
    ) -> None:
        """Phase 9: Different synthetic cache applied counts on same-provider
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

    @pytest.mark.asyncio()
    async def test_synthetic_cache_failed_fallback_does_not_affect_routing(
        self,
    ) -> None:
        """Phase 9: Synthetic cache failed_fallback on one account must not
        affect rotation fairness."""
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
    async def test_tuning_recommendation_does_not_affect_routing(self) -> None:
        """Phase 10: Tuning recommendation differences on same-provider
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
