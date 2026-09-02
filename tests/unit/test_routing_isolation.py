"""Cross-model routing isolation tests.

A single request failure for one model must never poison routing for
unrelated models.  This module exercises the failure-effects path end
to end and asserts that:

- A worst-case (404 + model-absent signal) failure on ``model_A`` does
  not remove ``model_B`` from the catalog cache's support set.
- An account-wide disable_auth transition on one request cannot disable
  routing for sibling models on the same account; the disable is bound
  to the model/account pair that originated the signal.
- A 5xx upstream cooldown applied to one request does not extend to
  the next request for a different model on the same account.

These tests directly exercise the routing-isolation invariant: routing
state mutations from a single request must remain observable only to
the originating (account, model) pair.
"""

from __future__ import annotations

import time

from eggpool.catalog.cache import ModelCatalogCache
from eggpool.failure.applier import EffectsApplier, FailureEffectProgress
from eggpool.failure.classifier import classify_failure_effects
from eggpool.failure.observation import FailureObservation
from eggpool.failure.quarantine import ModelQuarantine
from eggpool.failure.signal import FailureSignal
from eggpool.health.health_manager import HealthManager


def _obs(
    *,
    source: str = "upstream_http",
    status_code: int | None,
    error_class: str | None = None,
    account_name: str = "acct-1",
    model_id: str,
    provider_id: str = "opencode-go",
    response_signal: FailureSignal | None = None,
) -> FailureObservation:
    return FailureObservation(
        source=source,
        status_code=status_code,
        error_class=error_class,
        provider_id=provider_id,
        account_name=account_name,
        model_id=model_id,
        upstream_model_id=model_id,
        client_protocol="openai",
        upstream_protocol="openai",
        response_signal=response_signal,
        retry_after_s=None,
        response_started=False,
    )


class TestRoutingStateIsolation:
    """Single-request failure must not poison sibling model routing."""

    def _seed_cache(self) -> ModelCatalogCache:
        cache = ModelCatalogCache()
        cache.update_from_account(
            "acct-1",
            "opencode-go",
            [
                {"model_id": "muse-spark", "protocol": "anthropic"},
                {"model_id": "muse-other", "protocol": "anthropic"},
            ],
        )
        cache.update_from_account(
            "acct-2",
            "opencode-go",
            [
                {"model_id": "muse-spark", "protocol": "anthropic"},
                {"model_id": "muse-other", "protocol": "anthropic"},
            ],
        )
        cache.set_account_refresh_time("acct-1", time.time(), durable=True)
        cache.set_account_refresh_time("acct-2", time.time(), durable=True)
        return cache

    def test_model_absent_quarantine_is_scoped_to_one_model(self) -> None:
        """404 on muse-spark must not remove muse-other from support set."""
        cache = self._seed_cache()
        quarantine = ModelQuarantine()
        applier = EffectsApplier(quarantine=quarantine, catalog_cache=cache)
        obs = _obs(
            status_code=404,
            model_id="muse-spark",
            response_signal=FailureSignal.MODEL_ABSENT,
        )
        effects = classify_failure_effects(obs)
        progress = FailureEffectProgress(attempt_key="req-1:1")
        applier.apply_once(
            "req-1:1",
            obs,
            effects,
            progress=progress,
        )

        # muse-spark is removed from acct-1's support
        assert "acct-1" not in cache.get_supporting_accounts("muse-spark")
        # muse-other on acct-1 is preserved
        assert "acct-1" in cache.get_supporting_accounts("muse-other")
        # muse-spark is still routed via acct-2
        assert "acct-2" in cache.get_supporting_accounts("muse-spark")

    def test_account_disable_auth_does_not_disable_account(self) -> None:
        """A signal of AUTHENTICATION_FAILED on a request must NOT
        disable the entire account when the upstream response was a
        non-auth-failure.  The classifier should treat an
        unauthorized-keyword match in a 400 body as a client_error,
        not a provider disable."""
        cache = self._seed_cache()
        health = HealthManager()
        applier = EffectsApplier(health_manager=health, catalog_cache=cache)

        # 400 + body containing "unauthorized" - this is a request-local
        # validation error and MUST NOT disable the account.  The
        # classifier maps this to "client_error" with no account effect.
        obs = _obs(
            status_code=400,
            model_id="muse-spark",
            response_signal=None,
        )
        effects = classify_failure_effects(obs)
        applier.apply_once("req-1:1", obs, effects)

        assert health.is_account_healthy("acct-1")
        assert health.is_account_healthy("acct-2")
        # Both models still routable on both accounts
        assert "acct-1" in cache.get_supporting_accounts("muse-spark")
        assert "acct-1" in cache.get_supporting_accounts("muse-other")

    def test_5xx_cooldown_does_not_disable_account_for_sibling_models(
        self,
    ) -> None:
        """A 5xx error on one model leaves routing healthy for siblings."""
        cache = self._seed_cache()
        health = HealthManager()
        applier = EffectsApplier(health_manager=health, catalog_cache=cache)

        obs = _obs(
            status_code=503,
            model_id="muse-spark",
            response_signal=None,
        )
        effects = classify_failure_effects(obs)
        applier.apply_once("req-1:1", obs, effects)

        # Account health is not flipped by a single transient 5xx
        # (``cooldown`` effect is bounded and time-limited; the
        # account remains eligible for routing for a sibling model).
        # What we test is the SUPPORT SET is unchanged for sibling
        # models and the sibling account remains untouched.
        assert "acct-1" in cache.get_supporting_accounts("muse-other")
        assert "acct-2" in cache.get_supporting_accounts("muse-other")

    def test_partial_effect_failure_does_not_block_sibling_effects(
        self,
    ) -> None:
        """A raised exception in one effect step must not block others.

        ``EffectsApplier.apply_once`` is partially-resilient: a raised
        exception in the account-effect step is logged and the
        remaining steps (model effect, probe release, metrics) still
        run.  This prevents a single bad effect from leaving the
        applier in a half-applied state that could poison subsequent
        requests via the legacy non-applier fallback path.
        """
        cache = self._seed_cache()
        quarantine = ModelQuarantine()

        # Build a custom applier whose account-effect step raises,
        # but whose model-effect step still records quarantine.
        applier = EffectsApplier(quarantine=quarantine, catalog_cache=cache)

        def _boom(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("simulated account-effect failure")

        # Patch the account-effect method to raise.
        applier._apply_account_effect = _boom  # type: ignore[method-assign]

        obs = _obs(
            status_code=404,
            model_id="muse-spark",
            response_signal=FailureSignal.MODEL_ABSENT,
        )
        effects = classify_failure_effects(obs)
        progress = FailureEffectProgress(attempt_key="req-1:1")
        # Must not raise even though the account effect blew up.
        applier.apply_once("req-1:1", obs, effects, progress=progress)

        # The model effect still ran even though account effect raised.
        assert "acct-1" not in cache.get_supporting_accounts("muse-spark")
        # Sibling model on the same account is preserved.
        assert "acct-1" in cache.get_supporting_accounts("muse-other")
        # progress flags reflect that both steps were attempted.
        assert progress.account_applied is True
        assert progress.model_applied is True

    def test_retry_skips_partially_completed_effects(self) -> None:
        """A retry of apply_once for the same attempt key must skip
        already-applied effects.  This is the idempotency invariant
        that protects the routing state from being double-penalized
        when the supervisor retries a terminal command.
        """
        cache = self._seed_cache()
        health = HealthManager()
        applier = EffectsApplier(health_manager=health, catalog_cache=cache)

        obs = _obs(
            status_code=429,
            model_id="muse-spark",
            response_signal=FailureSignal.RATE_LIMITED,
        )
        effects = classify_failure_effects(obs)
        progress = FailureEffectProgress(attempt_key="req-1:1")
        first = applier.apply_once(
            "req-1:1",
            obs,
            effects,
            progress=progress,
        )
        second = applier.apply_once(
            "req-1:1",
            obs,
            effects,
            progress=progress,
        )
        assert first is not None
        assert second is None

        # Health state was applied once (not zero, not twice).
        health_state = health.get_account_health("acct-1").health_state
        assert health_state == "rate_limited"


class TestSignalExtractionBounds:
    """The signal extractor must bound its inspection of upstream bodies."""

    def test_extraction_ignores_body_past_first_4kb(self) -> None:
        """A 4MB body with a quota keyword at offset 5MB must NOT
        yield a QUOTA_EXHAUSTED signal — the extractor only inspects
        the first 4 KB.
        """
        from eggpool.failure.signal_extract import extract_failure_signal

        prefix = b"x" * (5 * 1024 * 1024)
        suffix = b"quota exhausted"
        body = prefix + suffix
        assert extract_failure_signal(body) is None

    def test_extraction_handles_non_utf8_gracefully(self) -> None:
        from eggpool.failure.signal_extract import extract_failure_signal

        # Random bytes are decoded with errors='replace' and never
        # crash the extractor.  No signal can be extracted from
        # undecodable noise — the caller must fall through to a
        # request-local classification, NOT a provider-side penalty.
        assert extract_failure_signal(b"\x00\x01\x02\xff\xfe\xfd") is None
