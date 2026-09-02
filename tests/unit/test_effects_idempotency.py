"""Effects idempotency tests.

Validates that the EffectsApplier applies effects exactly once per
attempt key and that retried finalizations do not double-penalize.
"""

from __future__ import annotations

from eggpool.failure.applier import EffectsApplier, FailureEffectProgress
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


# ---------------------------------------------------------------------------
# Failure isolation: per-model failures must not advance the account-wide
# circuit breaker.
# ---------------------------------------------------------------------------


class TestPerModelFailureIsolation:
    """Plan: per-model 5xx must not advance the account circuit breaker.

    The failure applier used to advance ``circuit_breaker.record_failure``
    for HTTP 5xx even when ``model_effect="quarantine"`` was set.  This
    caused a single broken model to black-hole sibling models on the
    same account once five per-model 5xxes tripped the breaker.  The
    fix: when ``model_effect != "none"`` the applier must skip the
    ``record_failure`` call so sibling models keep routing.
    """

    def test_http_500_with_per_model_quarantine_does_not_advance_breaker(
        self,
    ) -> None:
        hm = HealthManager()
        applier = EffectsApplier(health_manager=hm)
        obs = _obs(status_code=500, account_name="opencode-acct", model_id="muse-spark")
        effects = classify_failure_effects(obs)
        # Sanity check: the classifier does set model_effect="quarantine"
        # for HTTP 5xx so this is the exact code path under test.
        assert effects.model_effect == "quarantine"
        assert effects.account_effect == "failure"

        applier.apply_once("attempt-1", obs, effects)

        # The model is quarantined (per-model disable is correct).
        assert hm.is_model_healthy("opencode-acct", "muse-spark") is False
        # The account itself stays healthy and the circuit breaker is
        # NOT advanced because the failure is per-model scoped.
        health = hm.get_account_health("opencode-acct")
        assert health.is_healthy is True
        assert health.circuit_breaker.state.value == "closed"
        # Sibling models on the same account keep routing.
        assert hm.is_model_healthy("opencode-acct", "minimax-m3") is True
        assert hm.is_model_healthy("opencode-acct", "qwen3.7-max") is True

    def test_account_wide_failure_still_advances_breaker(self) -> None:
        """Account-wide failures (no per-model suppression) must still
        advance the circuit breaker.  Transport failures and other
        failures that lack ``model_effect="quarantine"`` keep the
        historical account-wide penalty.
        """
        hm = HealthManager()
        applier = EffectsApplier(health_manager=hm)
        # transport failure: model_effect stays "none" by default.
        obs = _obs(
            source="transport",
            account_name="opencode-acct",
            model_id="muse-spark",
            status_code=None,
        )
        effects = classify_failure_effects(obs)
        assert effects.model_effect == "none"

        applier.apply_once("attempt-1", obs, effects)

        # Account circuit breaker IS advanced for transport failures.
        health = hm.get_account_health("opencode-acct")
        assert health.circuit_breaker.state.value == "closed"
        # The breaker has counted one failure (may move toward OPEN at
        # the configured threshold).
        stats = health.circuit_breaker.get_stats()
        assert stats["failure_count"] == 1


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

    def test_circuit_failure_is_recorded_once(self) -> None:
        hm = HealthManager()
        applier = EffectsApplier(health_manager=hm)
        # Transport failures carry ``model_effect="none"`` (no per-model
        # suppression) so the applier advances the breaker exactly
        # once.  HTTP 5xx now flows through the per-model quarantine
        # path and explicitly skips the breaker advance — see
        # ``TestPerModelFailureIsolation`` below.
        obs = _obs(source="transport", status_code=None)
        effects = classify_failure_effects(obs)
        progress = FailureEffectProgress("request-1:1")
        applier.apply_once("request-1:1", obs, effects, progress=progress)
        applier.apply_once("request-1:1", obs, effects, progress=progress)
        stats = hm.get_account_health("acct-1").circuit_breaker.get_stats()
        assert stats["failure_count"] == 1

    def test_completed_progress_can_be_retired(self) -> None:
        applier = EffectsApplier()
        obs = _obs()
        effects = classify_failure_effects(obs)
        for index in range(200):
            key = f"request-{index}:1"
            applier.apply_once(key, obs, effects)
        assert len(applier._compat_progress) <= 128  # noqa: SLF001
        applier.retire("request-199:1")
        assert applier.is_applied("request-199:1") is False

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
