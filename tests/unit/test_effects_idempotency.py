"""Effects idempotency tests.

Validates that the EffectsApplier applies effects exactly once per
attempt key and that retried finalizations do not double-penalize.
"""

from __future__ import annotations

from eggpool.failure.applier import EffectsApplier
from eggpool.failure.classifier import classify_failure_effects
from eggpool.failure.observation import FailureObservation
from eggpool.failure.quarantine import ModelQuarantine
from eggpool.failure.signal import FailureSignal
from eggpool.health.health_manager import HealthManager


def _obs(
    *,
    source: str = "upstream_http",
    status_code: int | None = 429,
    account_name: str = "acct-1",
    model_id: str = "gpt-4o",
    provider_id: str = "openai",
    response_signal: FailureSignal | None = None,
) -> FailureObservation:
    return FailureObservation(
        source=source,
        status_code=status_code,
        error_class=None,
        provider_id=provider_id,
        account_name=account_name,
        model_id=model_id,
        upstream_model_id=None,
        client_protocol="openai",
        upstream_protocol="openai",
        response_signal=response_signal,
        retry_after_s=None,
        response_started=True,
    )


class TestEffectsIdempotency:
    """apply_once must be idempotent for the same attempt key."""

    def test_first_application_succeeds(self) -> None:
        applier = EffectsApplier()
        obs = _obs()
        effects = classify_failure_effects(obs)
        record = applier.apply_once("attempt-1", obs, effects)
        assert record is not None
        assert record.attempt_key == "attempt-1"

    def test_second_application_returns_none(self) -> None:
        applier = EffectsApplier()
        obs = _obs()
        effects = classify_failure_effects(obs)
        first = applier.apply_once("attempt-1", obs, effects)
        second = applier.apply_once("attempt-1", obs, effects)
        assert second is None
        assert first is not None

    def test_different_attempt_keys_apply_independently(self) -> None:
        applier = EffectsApplier()
        obs = _obs()
        effects = classify_failure_effects(obs)
        r1 = applier.apply_once("attempt-1", obs, effects)
        r2 = applier.apply_once("attempt-2", obs, effects)
        assert r1 is not None
        assert r2 is not None

    def test_is_applied(self) -> None:
        applier = EffectsApplier()
        obs = _obs()
        effects = classify_failure_effects(obs)
        assert applier.is_applied("attempt-1") is False
        applier.apply_once("attempt-1", obs, effects)
        assert applier.is_applied("attempt-1") is True

    def test_health_manager_not_double_penalized(self) -> None:
        hm = HealthManager()
        applier = EffectsApplier(health_manager=hm)
        obs = _obs()
        effects = classify_failure_effects(obs)
        applier.apply_once("attempt-1", obs, effects)
        health_before = hm.get_account_health("acct-1").consecutive_failures
        applier.apply_once("attempt-1", obs, effects)
        health_after = hm.get_account_health("acct-1").consecutive_failures
        assert health_before == health_after

    def test_quarantine_not_double_counted(self) -> None:
        quarantine = ModelQuarantine()
        applier = EffectsApplier(quarantine=quarantine)
        obs = _obs(status_code=404, response_signal=FailureSignal.MODEL_ABSENT)
        effects = classify_failure_effects(obs)
        applier.apply_once("attempt-1", obs, effects)
        entry = quarantine.get_entry(
            provider_id="openai",
            account_id="acct-1",
            canonical_model_id="gpt-4o",
            upstream_model_id=None,
            upstream_protocol="openai",
        )
        assert entry is not None
        count_before = entry.observation_count
        applier.apply_once("attempt-1", obs, effects)
        entry = quarantine.get_entry(
            provider_id="openai",
            account_id="acct-1",
            canonical_model_id="gpt-4o",
            upstream_model_id=None,
            upstream_protocol="openai",
        )
        assert entry is not None
        assert entry.observation_count == count_before


class TestClearOnSuccess:
    """Success clears bounded quarantine."""

    def test_clear_on_success(self) -> None:
        quarantine = ModelQuarantine()
        applier = EffectsApplier(quarantine=quarantine)
        obs = _obs(status_code=404, response_signal=FailureSignal.MODEL_ABSENT)
        effects = classify_failure_effects(obs)
        applier.apply_once("attempt-1", obs, effects)
        assert (
            quarantine.is_model_quarantined(
                provider_id="openai",
                account_id="acct-1",
                canonical_model_id="gpt-4o",
                upstream_model_id=None,
                upstream_protocol="openai",
            )
            is True
        )
        applier.clear_on_success(
            provider_id="openai",
            account_id="acct-1",
            canonical_model_id="gpt-4o",
            upstream_model_id=None,
            upstream_protocol="openai",
        )
        assert (
            quarantine.is_model_quarantined(
                provider_id="openai",
                account_id="acct-1",
                canonical_model_id="gpt-4o",
                upstream_model_id=None,
                upstream_protocol="openai",
            )
            is False
        )
