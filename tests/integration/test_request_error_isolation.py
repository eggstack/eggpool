"""Request error isolation tests.

Validates that request-local failures (capability rejection, context
limit, unsupported thinking, client cancellation) produce zero
shared-state effects, and that provider failures are properly isolated.
"""

from __future__ import annotations

import pytest

from eggpool.failure.applier import EffectsApplier
from eggpool.failure.classifier import classify_failure_effects
from eggpool.failure.observation import FailureObservation
from eggpool.failure.quarantine import ModelQuarantine
from eggpool.failure.signal import FailureSignal
from eggpool.health.health_manager import HealthManager


def _obs(**kwargs: object) -> FailureObservation:
    defaults = dict(
        source="upstream_http",
        status_code=None,
        error_class=None,
        provider_id="openai",
        account_name="acct-1",
        model_id="gpt-4o",
        upstream_model_id=None,
        client_protocol="openai",
        upstream_protocol="openai",
        response_signal=None,
        retry_after_s=None,
        response_started=True,
    )
    defaults.update(kwargs)
    return FailureObservation(**defaults)  # type: ignore[arg-type]


class TestRequestLocalFailureIsolation:
    """Request-local failures must not change shared state."""

    @pytest.mark.parametrize(
        ("source", "signal", "error_class"),
        [
            ("client_validation", None, None),
            ("upstream_http", FailureSignal.CONTEXT_LIMIT_EXCEEDED, None),
            ("upstream_http", FailureSignal.UNSUPPORTED_REQUEST_CONTROL, None),
            ("upstream_http", FailureSignal.GENERIC_CLIENT_VALIDATION, None),
            ("upstream_http", None, "ContextLimitExceeded"),
            ("upstream_http", None, "CapabilityError"),
            ("finalization", None, None),
            ("database", None, None),
        ],
    )
    def test_no_shared_state_change(
        self,
        source: str,
        signal: FailureSignal | None,
        error_class: str | None,
    ) -> None:
        hm = HealthManager()
        quarantine = ModelQuarantine()
        applier = EffectsApplier(health_manager=hm, quarantine=quarantine)

        obs = _obs(source=source, response_signal=signal, error_class=error_class)
        effects = classify_failure_effects(obs)
        applier.apply_once("attempt-1", obs, effects)

        # Health manager should remain healthy
        assert hm.is_account_healthy("acct-1") is True
        # No quarantine entry should exist
        assert quarantine.list_entries() == []

    def test_client_cancellation_no_penalty(self) -> None:
        hm = HealthManager()
        applier = EffectsApplier(health_manager=hm)

        obs = _obs(
            source="stream",
            response_signal=None,
            response_started=False,
            status_code=None,
        )
        effects = classify_failure_effects(obs)
        applier.apply_once("attempt-1", obs, effects)

        assert hm.is_account_healthy("acct-1") is True

    def test_probe_slot_released_on_request_local(self) -> None:
        hm = HealthManager()
        applier = EffectsApplier(health_manager=hm)

        health = hm.get_account_health("acct-1")
        health.circuit_breaker.failure_threshold = 1
        health.circuit_breaker.recovery_timeout = 0
        health.circuit_breaker.record_failure()
        assert hm.try_acquire_request("acct-1", "gpt-4o") is True

        obs = _obs(source="client_validation")
        effects = classify_failure_effects(obs)
        applier.apply_once("attempt-1", obs, effects)

        # release_probe_only should be True
        assert effects.release_probe_only is True
        assert hm.try_acquire_request("acct-1", "gpt-4o") is True


class TestProviderFailureIsolation:
    """Provider failures affect only the failing provider/account."""

    def test_upstream_500_penalizes_only_failing_model(self) -> None:
        """A 5xx on a single model must not blanket-disable the account.

        Regression test for the routing-failure isolation invariant: an
        upstream 500 for ``gpt-4o`` quarantines the (account, model)
        pair but leaves sibling models on the same account routable so
        the operator does not need a process restart to recover from a
        single broken model.
        """
        hm = HealthManager()
        quarantine = ModelQuarantine()
        applier = EffectsApplier(health_manager=hm, quarantine=quarantine)

        obs = _obs(status_code=500, account_name="acct-failing")
        effects = classify_failure_effects(obs)
        applier.apply_once("attempt-1", obs, effects)

        # The account itself stays healthy — only the specific (account,
        # model) pair is suppressed.  Sibling models on the same
        # account keep routing.
        assert hm.is_account_healthy("acct-failing") is True
        assert hm.is_account_healthy("acct-healthy") is True
        assert hm.is_model_healthy("acct-failing", "gpt-4o") is False
        assert hm.is_model_healthy("acct-failing", "gpt-4o-mini") is True
        assert hm.is_model_healthy("acct-healthy", "gpt-4o") is True
        # Quarantine observation is recorded so the bounded state
        # machine tracks repeated failures.
        assert (
            quarantine.is_model_quarantined(
                provider_id="openai",
                account_id="acct-failing",
                canonical_model_id="gpt-4o",
                upstream_model_id=None,
                upstream_protocol="openai",
            )
            is True
        )
        # Same model on a healthy account is not affected.
        assert (
            quarantine.is_model_quarantined(
                provider_id="openai",
                account_id="acct-healthy",
                canonical_model_id="gpt-4o",
                upstream_model_id=None,
                upstream_protocol="openai",
            )
            is False
        )

    def test_explicit_auth_failure_disables_only_failing_account(self) -> None:
        hm = HealthManager()
        applier = EffectsApplier(health_manager=hm)

        obs = _obs(
            status_code=401,
            account_name="acct-bad",
            response_signal=FailureSignal.CREDENTIAL_INVALID,
        )
        effects = classify_failure_effects(obs)
        applier.apply_once("attempt-1", obs, effects)

        assert hm.is_account_healthy("acct-bad") is False
        assert hm.is_account_healthy("acct-good") is True

    def test_model_404_quarantines_only_exact_key(self) -> None:
        quarantine = ModelQuarantine()
        applier = EffectsApplier(quarantine=quarantine)

        obs = _obs(
            status_code=404,
            response_signal=FailureSignal.MODEL_ABSENT,
            account_name="acct-1",
            provider_id="openai",
        )
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

        # Other account on same provider
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
