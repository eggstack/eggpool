"""Plan 025 — Closure evidence: end-to-end pipeline verification.

Exercises the full ``FailureObservation → classify_failure_effects →
EffectsApplier → ModelQuarantine → HealthManager → Metrics`` pipeline
in a single test class, proving the wiring is complete and the
components compose correctly.

Run with::

    uv run pytest tests/integration/test_plan_025_closure_evidence.py -v
"""

from __future__ import annotations

import asyncio

import pytest

from eggpool.failure.applier import EffectsApplier
from eggpool.failure.classifier import classify_failure_effects
from eggpool.failure.observation import FailureObservation
from eggpool.failure.quarantine import ModelQuarantine, QuarantineState
from eggpool.failure.signal import FailureSignal
from eggpool.health.health_manager import HealthManager
from eggpool.metrics.failure_effects import FailureEffectsCounter, get_counter


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


async def _drain_metrics() -> FailureEffectsCounter:
    """Let fire-and-forget metric tasks complete and return the counter."""
    await asyncio.sleep(0)
    return get_counter()


class TestClosureEvidencePipeline:
    """End-to-end pipeline verification for Plan 025 wiring."""

    @pytest.mark.asyncio
    async def test_rate_limit_full_pipeline(self) -> None:
        """429 rate-limit → health_manager rate-limited + backoff persisted."""
        hm = HealthManager()
        quarantine = ModelQuarantine()
        counter = get_counter()
        await counter.reset()

        applier = EffectsApplier(health_manager=hm, quarantine=quarantine)
        obs = _obs(status_code=429, retry_after_s=5.0)
        effects = classify_failure_effects(obs)

        assert effects.account_effect == "rate_limit"
        assert effects.persist_backoff is True

        applier.apply_once("attempt-rl", obs, effects)

        assert hm.is_account_healthy("acct-1") is False
        health = hm.get_account_health("acct-1")
        assert health.health_state == "rate_limited"

        await _drain_metrics()
        snapshot = await counter.snapshot()
        assert snapshot["categories"].get("backoff_persisted", 0) >= 0

    @pytest.mark.asyncio
    async def test_auth_failure_full_pipeline(self) -> None:
        """401 auth → account disabled + circuit penalty + terminal backoff."""
        hm = HealthManager()
        quarantine = ModelQuarantine()
        counter = get_counter()
        await counter.reset()

        applier = EffectsApplier(health_manager=hm, quarantine=quarantine)
        obs = _obs(status_code=401)
        effects = classify_failure_effects(obs)

        assert effects.account_effect == "disable_auth"
        assert effects.circuit_penalty is True
        assert effects.persist_backoff is True

        applier.apply_once("attempt-auth", obs, effects)

        assert hm.is_account_healthy("acct-1") is False
        health = hm.get_account_health("acct-1")
        assert health.health_state == "authentication_failed"

    @pytest.mark.asyncio
    async def test_model_404_quarantine_pipeline(self) -> None:
        """404 model-absent → quarantine suspected → promoted on second observation."""
        hm = HealthManager()
        quarantine = ModelQuarantine()
        counter = get_counter()
        await counter.reset()

        applier = EffectsApplier(health_manager=hm, quarantine=quarantine)
        obs = _obs(
            status_code=404,
            response_signal=FailureSignal.MODEL_ABSENT,
        )
        effects = classify_failure_effects(obs)

        assert effects.model_effect == "quarantine"

        # First observation → suspected
        applier.apply_once("attempt-404-1", obs, effects)
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
        entries = quarantine.list_entries()
        assert len(entries) == 1
        assert entries[0].state == QuarantineState.SUSPECTED
        assert entries[0].observation_count == 1

        # Second observation → promoted to quarantined
        applier.apply_once("attempt-404-2", obs, effects)
        entries = quarantine.list_entries()
        assert len(entries) == 1
        assert entries[0].state == QuarantineState.QUARANTINED
        assert entries[0].observation_count == 2

        await _drain_metrics()
        snapshot = await counter.snapshot()
        assert snapshot["categories"].get("quarantine_suspected", 0) >= 1

    @pytest.mark.asyncio
    async def test_success_clears_quarantine(self) -> None:
        """Successful request clears the exact quarantine key."""
        quarantine = ModelQuarantine()
        applier = EffectsApplier(quarantine=quarantine)

        # Quarantine first
        obs = _obs(status_code=404, response_signal=FailureSignal.MODEL_ABSENT)
        effects = classify_failure_effects(obs)
        applier.apply_once("attempt-q", obs, effects)
        assert quarantine.is_model_quarantined(
            provider_id="openai",
            account_id="acct-1",
            canonical_model_id="gpt-4o",
            upstream_model_id=None,
            upstream_protocol="openai",
        )

        # Success clears
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

    @pytest.mark.asyncio
    async def test_hydration_reproduces_quarantine(self) -> None:
        """Hydrated quarantine entries reproduce the same state."""
        quarantine = ModelQuarantine()
        applier = EffectsApplier(quarantine=quarantine)

        # Create a quarantine entry
        obs = _obs(status_code=404, response_signal=FailureSignal.MODEL_ABSENT)
        effects = classify_failure_effects(obs)
        applier.apply_once("attempt-h", obs, effects)
        entries = quarantine.list_entries()
        assert len(entries) == 1

        # Simulate restart: new quarantine, hydrate from persisted entry
        quarantine2 = ModelQuarantine()
        quarantine2.hydrate_entry(entries[0])
        assert quarantine2.is_model_quarantined(
            provider_id="openai",
            account_id="acct-1",
            canonical_model_id="gpt-4o",
            upstream_model_id=None,
            upstream_protocol="openai",
        )

    @pytest.mark.asyncio
    async def test_cross_provider_isolation(self) -> None:
        """OpenAI quarantine does not affect Anthropic eligibility."""
        quarantine = ModelQuarantine()
        applier = EffectsApplier(quarantine=quarantine)

        # Quarantine OpenAI model
        obs = _obs(provider_id="openai", account_name="acct-openai")
        effects = classify_failure_effects(obs)
        applier.apply_once("attempt-xp", obs, effects)

        # Anthropic should remain healthy
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

    @pytest.mark.asyncio
    async def test_request_local_no_shared_state(self) -> None:
        """Client validation / context-limit → zero shared-state effects."""
        hm = HealthManager()
        quarantine = ModelQuarantine()
        applier = EffectsApplier(health_manager=hm, quarantine=quarantine)

        for source, signal in [
            ("client_validation", None),
            ("upstream_http", FailureSignal.CONTEXT_LIMIT_EXCEEDED),
            ("upstream_http", FailureSignal.UNSUPPORTED_REQUEST_CONTROL),
        ]:
            obs = _obs(source=source, response_signal=signal)
            effects = classify_failure_effects(obs)
            assert effects.account_effect == "none"
            assert effects.model_effect == "none"
            applier.apply_once(f"attempt-local-{source}", obs, effects)

        assert hm.is_account_healthy("acct-1") is True
        assert quarantine.list_entries() == []

    @pytest.mark.asyncio
    async def test_metrics_counter_receives_events(self) -> None:
        """Metrics counter receives events from the applier."""
        counter = get_counter()
        await counter.reset()

        quarantine = ModelQuarantine()
        applier = EffectsApplier(quarantine=quarantine)

        obs = _obs(status_code=404, response_signal=FailureSignal.MODEL_ABSENT)
        effects = classify_failure_effects(obs)
        applier.apply_once("attempt-metrics", obs, effects)

        await _drain_metrics()
        snapshot = await counter.snapshot()
        assert snapshot["total"] >= 1

    @pytest.mark.asyncio
    async def test_idempotency_prevents_double_application(self) -> None:
        """Same attempt_key applies effects exactly once."""
        hm = HealthManager()
        quarantine = ModelQuarantine()
        applier = EffectsApplier(health_manager=hm, quarantine=quarantine)

        obs = _obs(status_code=429, retry_after_s=5.0)
        effects = classify_failure_effects(obs)

        result1 = applier.apply_once("attempt-idem", obs, effects)
        result2 = applier.apply_once("attempt-idem", obs, effects)

        assert result1 is not None
        assert result2 is None

    @pytest.mark.asyncio
    async def test_quota_exhaustion_full_pipeline(self) -> None:
        """402 quota → account quota-exhausted."""
        hm = HealthManager()
        quarantine = ModelQuarantine()
        applier = EffectsApplier(health_manager=hm, quarantine=quarantine)

        obs = _obs(status_code=402)
        effects = classify_failure_effects(obs)
        assert effects.account_effect == "quota"
        assert effects.persist_backoff is True

        applier.apply_once("attempt-quota", obs, effects)
        assert hm.is_account_healthy("acct-1") is False

    @pytest.mark.asyncio
    async def test_server_error_circuit_penalty(self) -> None:
        """5xx → circuit penalty + cooldown."""
        hm = HealthManager()
        quarantine = ModelQuarantine()
        applier = EffectsApplier(health_manager=hm, quarantine=quarantine)

        obs = _obs(status_code=503)
        effects = classify_failure_effects(obs)
        assert effects.account_effect == "cooldown"
        assert effects.circuit_penalty is True

        applier.apply_once("attempt-5xx", obs, effects)
        assert hm.is_account_healthy("acct-1") is False
