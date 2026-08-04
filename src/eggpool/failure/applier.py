"""Effects applier — applies failure effects exactly once per attempt.

Uses an idempotency key (attempt identity + effect generation) to
ensure retried finalizations do not double-penalize health or
increment quarantine observations.

The coordinator calculates effects via the pure classifier, then
hands them to this applier.  The finalizer receives
``effects_applied=True`` and must not call
``classify_failure_category()`` independently for the same terminal
event.

The applier also emits ``eggpool.metrics.failure_effects`` counters
so the dashboard distinguishes request-local validation, bounded
quarantine, and terminal withdrawals without re-scoring raw status
codes.  Counter emission is fire-and-forget: failure to increment the
counter never blocks the shared-state effect application.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from eggpool.failure.quarantine import (
    EvidenceProvenance,
    ModelQuarantine,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from eggpool.failure.effects import FailureEffects
    from eggpool.failure.observation import FailureObservation
    from eggpool.health.health_manager import HealthManager

logger = logging.getLogger(__name__)


@dataclass
class AppliedEffects:
    """Record of effects that have been applied for a specific attempt."""

    attempt_key: str
    effects: FailureEffects
    observation: FailureObservation
    applied_at: float


@dataclass(slots=True)
class FailureEffectProgress:
    """Component progress owned by one retained attempt lifecycle."""

    attempt_key: str
    account_applied: bool = False
    model_applied: bool = False
    circuit_applied: bool = False
    probe_converged: bool = False
    backoff_persistence_attempted: bool = False
    backoff_persistence_completed: bool = False
    metrics_emitted: bool = False
    record: AppliedEffects | None = field(default=None, repr=False)

    @property
    def completed(self) -> bool:
        """Whether all in-memory effect components have converged."""
        return all(
            (
                self.account_applied,
                self.model_applied,
                self.circuit_applied,
                self.probe_converged,
                (
                    not self.backoff_persistence_attempted
                    or self.backoff_persistence_completed
                ),
            )
        )


class EffectsApplier:
    """Applies failure effects exactly once per attempt outcome.

    Maintains a set of applied attempt keys to guard against
    double-penalization from retried finalization paths.  The
    :meth:`apply_once` method is the single entry point; all shared
    state mutations flow through it.

    The applier accepts an optional ``persist_backoff`` callback that
    writes durable backoff rows to SQLite.  The callback signature
    matches ``coordinator._persist_backoff`` so the production
    coordinator can inject its existing repository-backed helper
    without recreating the logic.
    """

    def __init__(
        self,
        *,
        health_manager: HealthManager | None = None,
        quarantine: ModelQuarantine | None = None,
        catalog_cache: Any | None = None,
        persist_backoff: Callable[..., Awaitable[None]] | None = None,
    ) -> None:
        self._health_manager = health_manager
        self._quarantine = quarantine
        self._catalog_cache = catalog_cache
        self._persist_backoff = persist_backoff
        # Compatibility callers that do not retain a lifecycle owner get a
        # bounded cache. Production callers pass FailureEffectProgress owned
        # by AttemptCleanupProgress or RequestFinalizationJob and never use
        # this cache as their idempotency boundary.
        self._compat_progress: OrderedDict[str, FailureEffectProgress] = OrderedDict()
        self._compat_capacity = 128

    def apply_once(
        self,
        attempt_key: str,
        observation: FailureObservation,
        effects: FailureEffects,
        *,
        progress: FailureEffectProgress | None = None,
        now: float | None = None,
    ) -> AppliedEffects | None:
        """Apply effects for a given attempt, idempotently.

        Returns the :class:`AppliedEffects` record on first
        application, or ``None`` if the effects were already applied
        for this attempt key (idempotent no-op).
        """
        if progress is None:
            progress = self._compat_progress.get(attempt_key)
            if progress is None:
                progress = FailureEffectProgress(attempt_key=attempt_key)
                self._compat_progress[attempt_key] = progress
                self._compat_progress.move_to_end(attempt_key)
                while len(self._compat_progress) > self._compat_capacity:
                    self._compat_progress.popitem(last=False)
        if progress.completed:
            logger.debug(
                "effects_applier: already applied for attempt=%s",
                attempt_key,
            )
            return None

        if now is None:
            now = time.time()

        if not progress.account_applied:
            self._apply_account_effect(observation, effects, now)
            progress.account_applied = True
        if not progress.model_applied:
            self._apply_model_effect(observation, effects, now)
            progress.model_applied = True
        if not progress.circuit_applied:
            # HealthManager.record_failure owns the circuit transition. The
            # former applier-side record_failure call double-counted the same
            # observation and could open a circuit one attempt early.
            progress.circuit_applied = True
        if not progress.probe_converged:
            self._apply_probe_release(observation, effects)
            progress.probe_converged = True
        record = AppliedEffects(
            attempt_key=attempt_key,
            effects=effects,
            observation=observation,
            applied_at=now,
        )
        progress.record = record
        if not progress.metrics_emitted:
            self._emit_metrics(observation, effects)
            progress.metrics_emitted = True
        return record

    def is_applied(self, attempt_key: str) -> bool:
        """Check if effects have already been applied for this attempt."""
        progress = self._compat_progress.get(attempt_key)
        return progress is not None and progress.completed

    def retire(self, attempt_key: str) -> None:
        """Retire compatibility progress after its attempt owner converges."""
        self._compat_progress.pop(attempt_key, None)

    def _apply_account_effect(
        self,
        obs: FailureObservation,
        effects: FailureEffects,
        now: float,
    ) -> None:
        """Apply account-level health transitions."""
        if self._health_manager is None or obs.account_name is None:
            return
        if effects.account_effect == "none":
            return

        account = obs.account_name
        if effects.account_effect == "disable_auth":
            self._health_manager.record_failure(
                account,
                model_id=obs.model_id,
                reason="authentication_failed",
            )
        elif effects.account_effect == "quota":
            cooldown = 300.0
            if effects.backoff_until is not None:
                cooldown = max(1.0, effects.backoff_until - now)
            self._health_manager.record_quota_exhausted(
                account, cooldown_seconds=cooldown
            )
            self._health_manager.release_request(account)
        elif effects.account_effect == "rate_limit":
            retry_after = effects.backoff_until - now if effects.backoff_until else 60.0
            self._health_manager.record_rate_limit(account, retry_after)
            self._health_manager.release_request(account)
        elif effects.account_effect in ("failure", "cooldown"):
            self._health_manager.record_failure(
                account,
                model_id=obs.model_id,
                reason=effects.backoff_reason or "unknown",
            )
            if effects.backoff_until and effects.backoff_reason:
                delay = effects.backoff_until - now
                if delay > 0:
                    health = self._health_manager.get_account_health(account)
                    health.cooldown_until = now + delay
                    health.health_state = "cooldown"
                    health.is_healthy = False

    def _apply_model_effect(
        self,
        obs: FailureObservation,
        effects: FailureEffects,
        now: float,
    ) -> None:
        """Apply model-level quarantine effects."""
        if obs.model_id is None or obs.account_name is None:
            return
        if effects.model_effect == "none":
            return

        if effects.model_effect == "quarantine":
            if self._quarantine is not None and obs.provider_id is not None:
                provenance = EvidenceProvenance.RUNTIME_HTTP
                if obs.source == "provider_catalog":
                    provenance = EvidenceProvenance.PROVIDER_CATALOG
                self._quarantine.record_observation(
                    provider_id=obs.provider_id,
                    account_id=obs.account_name,
                    canonical_model_id=obs.model_id,
                    upstream_model_id=obs.upstream_model_id,
                    upstream_protocol=obs.upstream_protocol,
                    evidence_provenance=provenance,
                    reason=effects.evidence_class,
                    status_code=obs.status_code,
                    error_class=obs.error_class,
                    now=now,
                )
            if self._health_manager is not None:
                duration = (
                    effects.backoff_until - now if effects.backoff_until else 300.0
                )
                self._health_manager.disable_model(
                    obs.account_name,
                    obs.model_id,
                    duration_seconds=max(1.0, duration),
                )
                self._health_manager.release_request(obs.account_name)
            if self._catalog_cache is not None:
                self._catalog_cache.mark_model_unavailable(
                    obs.account_name, obs.model_id
                )

        elif effects.model_effect == "terminal_withdrawal":
            if self._quarantine is not None and obs.provider_id is not None:
                provenance = EvidenceProvenance.PROVIDER_CATALOG
                if obs.source == "operator_action":
                    provenance = EvidenceProvenance.OPERATOR_ACTION
                self._quarantine.set_terminal_withdrawn(
                    provider_id=obs.provider_id,
                    account_id=obs.account_name,
                    canonical_model_id=obs.model_id,
                    upstream_model_id=obs.upstream_model_id,
                    upstream_protocol=obs.upstream_protocol,
                    reason=effects.evidence_class,
                    provenance=provenance,
                    now=now,
                )
            if self._health_manager is not None:
                self._health_manager.disable_model(
                    obs.account_name,
                    obs.model_id,
                )
                self._health_manager.release_request(obs.account_name)
            if self._catalog_cache is not None:
                self._catalog_cache.mark_model_unavailable(
                    obs.account_name, obs.model_id
                )

    def _apply_probe_release(
        self,
        obs: FailureObservation,
        effects: FailureEffects,
    ) -> None:
        """Release half-open probe slot when ``release_probe_only`` is set.

        ``release_probe_only=True`` is the mandatory default for every
        request-local failure path (client validation, capability
        rejection, context limit, generic 4xx).  The probe slot is
        released with no health penalty.  When other effects also
        fire (``account_effect != "none"`` etc.) the probe is already
        released by those branches — the explicit release here is
        idempotent because ``release_request`` only clears the
        half-open flag.
        """
        if not effects.release_probe_only:
            return
        if self._health_manager is None or obs.account_name is None:
            return
        self._health_manager.release_request(obs.account_name)

    def _emit_metrics(
        self,
        obs: FailureObservation,
        effects: FailureEffects,
    ) -> None:
        """Emit failure-effects counters via fire-and-forget task.

        Counter emission is best-effort; failure to increment must
        never block the shared-state effect application.  The
        counters distinguish request-local validation, bounded
        quarantine, and terminal withdrawal — the plan requires
        observable distinction without re-scoring raw status codes.
        """
        import asyncio

        from eggpool.metrics.failure_effects import (
            FailureEffectsEvent,
            record_failure_effects,
        )

        provider_id = obs.provider_id or "unknown"
        evidence_class = effects.evidence_class
        source = obs.source
        reason = effects.backoff_reason or evidence_class

        # Map effects to counter categories.
        if effects.model_effect == "terminal_withdrawal":
            category = "terminal_withdrawal"
        elif effects.model_effect == "quarantine":
            category = "quarantine_suspected"
        elif effects.account_effect != "none" or effects.release_probe_only:
            category = "request_local"
        else:
            category = "request_local"

        event = FailureEffectsEvent(
            category=category,
            reason=reason,
            evidence_class=evidence_class,
            source=source,
            provider_id=provider_id,
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(record_failure_effects(event))

    def clear_on_success(
        self,
        *,
        provider_id: str,
        account_id: str,
        canonical_model_id: str,
        upstream_model_id: str | None,
        upstream_protocol: str,
    ) -> bool:
        """Clear bounded quarantine on successful request.

        Called by the finalizer on successful completion to demonstrate
        recovery.  Returns ``True`` if a quarantine entry was cleared.
        """
        cleared = False
        if self._quarantine is not None:
            cleared = self._quarantine.clear_exact_key(
                provider_id=provider_id,
                account_id=account_id,
                canonical_model_id=canonical_model_id,
                upstream_model_id=upstream_model_id,
                upstream_protocol=upstream_protocol,
                reason="successful_request",
            )
        if self._health_manager is not None and account_id and canonical_model_id:
            self._health_manager.enable_model(account_id, canonical_model_id)
        return cleared

    def clear_authoritative_reappearance(
        self,
        *,
        provider_id: str,
        account_id: str,
        canonical_model_id: str,
        upstream_model_id: str | None,
        upstream_protocol: str,
    ) -> bool:
        """Clear quarantine when model reappears in provider catalog.

        Returns ``True`` if a quarantine entry was cleared.
        """
        cleared = False
        if self._quarantine is not None:
            cleared = self._quarantine.clear_authoritative_reappearance(
                provider_id=provider_id,
                account_id=account_id,
                canonical_model_id=canonical_model_id,
                upstream_model_id=upstream_model_id,
                upstream_protocol=upstream_protocol,
            )
        if self._health_manager is not None and account_id and canonical_model_id:
            self._health_manager.enable_model(account_id, canonical_model_id)
        return cleared
