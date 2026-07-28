"""Cross-provider quarantine isolation tests.

Validates that quarantine on one provider/account does not suppress
traffic on other providers/accounts/protocols.
"""

from __future__ import annotations

from eggpool.failure.applier import EffectsApplier
from eggpool.failure.classifier import classify_failure_effects
from eggpool.failure.observation import FailureObservation
from eggpool.failure.quarantine import ModelQuarantine
from eggpool.failure.signal import FailureSignal
from eggpool.health.health_manager import HealthManager


def _make_obs(
    *,
    provider_id: str,
    account_id: str,
    model_id: str = "gpt-4o",
    status_code: int | None = 404,
    response_signal: FailureSignal | None = FailureSignal.MODEL_ABSENT,
    upstream_protocol: str = "openai",
) -> FailureObservation:
    return FailureObservation(
        source="upstream_http",
        status_code=status_code,
        error_class=None,
        provider_id=provider_id,
        account_name=account_id,
        model_id=model_id,
        upstream_model_id=None,
        client_protocol="openai",
        upstream_protocol=upstream_protocol,
        response_signal=response_signal,
        retry_after_s=None,
        response_started=True,
    )


class TestCrossProviderIsolation:
    """One provider's quarantine does not affect other providers."""

    def test_openai_quarantine_does_not_affect_anthropic(self) -> None:
        quarantine = ModelQuarantine()
        applier = EffectsApplier(quarantine=quarantine)

        # Quarantine OpenAI
        obs = _make_obs(provider_id="openai", account_id="acct-openai")
        effects = classify_failure_effects(obs)
        applier.apply_once("attempt-1", obs, effects)

        # Anthropic should remain unaffected
        assert (
            quarantine.is_model_quarantined(
                provider_id="anthropic",
                account_id="acct-anthropic",
                canonical_model_id="gpt-4o",
                upstream_model_id=None,
                upstream_protocol="openai",
            )
            is False
        )

    def test_different_accounts_under_same_provider_are_independent(self) -> None:
        quarantine = ModelQuarantine()
        applier = EffectsApplier(quarantine=quarantine)

        obs = _make_obs(provider_id="openai", account_id="acct-1")
        effects = classify_failure_effects(obs)
        applier.apply_once("attempt-1", obs, effects)

        assert (
            quarantine.is_model_quarantined(
                provider_id="openai",
                account_id="acct-2",
                canonical_model_id="gpt-4o",
                upstream_model_id=None,
                upstream_protocol="openai",
            )
            is False
        )

    def test_different_protocols_under_same_provider_are_independent(self) -> None:
        quarantine = ModelQuarantine()
        applier = EffectsApplier(quarantine=quarantine)

        obs = _make_obs(
            provider_id="openai",
            account_id="acct-1",
            upstream_protocol="openai",
        )
        effects = classify_failure_effects(obs)
        applier.apply_once("attempt-1", obs, effects)

        assert (
            quarantine.is_model_quarantined(
                provider_id="openai",
                account_id="acct-1",
                canonical_model_id="gpt-4o",
                upstream_model_id=None,
                upstream_protocol="anthropic",
            )
            is False
        )

    def test_different_models_under_same_provider_are_independent(self) -> None:
        quarantine = ModelQuarantine()
        applier = EffectsApplier(quarantine=quarantine)

        obs = _make_obs(
            provider_id="openai",
            account_id="acct-1",
            model_id="gpt-4o",
        )
        effects = classify_failure_effects(obs)
        applier.apply_once("attempt-1", obs, effects)

        assert (
            quarantine.is_model_quarantined(
                provider_id="openai",
                account_id="acct-1",
                canonical_model_id="claude-3-opus",
                upstream_model_id=None,
                upstream_protocol="openai",
            )
            is False
        )


class TestHealthManagerIsolation:
    """Health manager effects are also properly scoped."""

    def test_openai_failure_does_not_disable_anthropic_account(self) -> None:
        hm = HealthManager()
        applier = EffectsApplier(health_manager=hm)

        obs = _make_obs(provider_id="openai", account_id="acct-openai")
        effects = classify_failure_effects(obs)
        applier.apply_once("attempt-1", obs, effects)

        assert hm.is_account_healthy("acct-anthropic") is True

    def test_success_clears_exact_account(self) -> None:
        hm = HealthManager()
        quarantine = ModelQuarantine()
        applier = EffectsApplier(health_manager=hm, quarantine=quarantine)

        obs = _make_obs(provider_id="openai", account_id="acct-1")
        effects = classify_failure_effects(obs)
        applier.apply_once("attempt-1", obs, effects)

        applier.clear_on_success(
            provider_id="openai",
            account_id="acct-1",
            canonical_model_id="gpt-4o",
            upstream_model_id=None,
            upstream_protocol="openai",
        )

        assert hm.is_model_healthy("acct-1", "gpt-4o") is True
