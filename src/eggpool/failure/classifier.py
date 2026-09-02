"""Pure canonical classifier for retry and shared-state failure effects.

The classifier is deliberately small and closed: it accepts one normalized
observation and returns one immutable decision.  Response bodies are reduced
to :class:`FailureSignal` before they reach this module; no raw payload or
traceback is retained here.
"""

from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING

from eggpool.failure.effects import FailureEffects
from eggpool.failure.signal import FailureSignal
from eggpool.health.backoff import MAX_NONTERMINAL_BACKOFF_SECONDS

if TYPE_CHECKING:
    from eggpool.failure.observation import FailureObservation

EVIDENCE_RUNTIME_HTTP = "runtime_http"
EVIDENCE_PROVIDER_CATALOG = "provider_catalog"
EVIDENCE_MODEL_INFO = "model_info"
EVIDENCE_MANUAL_OVERRIDE = "manual_override"
EVIDENCE_OPERATOR_ACTION = "operator_action"
EVIDENCE_MIGRATION_LEGACY = "migration_legacy"


def _now() -> float:
    return time.time()


def _client_outcome(obs: FailureObservation) -> str:
    if obs.source in {"client_validation", "local_preparation", "transcoding"}:
        return "client_error"
    if obs.source in {"finalization", "database"}:
        return "upstream_error"
    if obs.source == "transport":
        return "service_unavailable"
    if obs.source in {"stream", "cancellation"}:
        return "upstream_error"
    if obs.status_code == 408:
        return "timeout"
    if obs.status_code is not None and 400 <= obs.status_code < 500:
        return "client_error"
    return "upstream_error"


def _decision(
    obs: FailureObservation,
    *,
    retry: bool = False,
    retry_action: str | None = None,
    wire_effect: str = "none",
    client_outcome: str | None = None,
    account_effect: str = "none",
    model_effect: str = "none",
    circuit_penalty: bool = False,
    persist_backoff: bool = False,
    backoff_reason: str | None = None,
    backoff_until: float | None = None,
    release_probe_only: bool = True,
    evidence_class: str,
    provider_attributable: bool = False,
) -> FailureEffects:
    """Construct a complete decision and derive its component metadata."""
    action = retry_action or ("other_account_same_wire" if retry else "none")
    if obs.downstream_started:
        action = "none"
    scope = {
        "none": "none",
        "alternate_wire_same_account": "same_account_other_wire",
        "other_account_same_wire": "other_account",
        "existing_route_retry": "other_account",
    }.get(action, "none")
    return FailureEffects(
        retry=action != "none",
        retry_scope=scope,
        client_outcome=client_outcome or _client_outcome(obs),
        account_effect=account_effect,
        model_effect=model_effect,
        circuit_penalty=circuit_penalty,
        persist_backoff=persist_backoff,
        backoff_reason=backoff_reason,
        backoff_until=backoff_until,
        release_probe_only=release_probe_only,
        evidence_class=evidence_class,
        circuit_transition="failure" if circuit_penalty else "none",
        probe_convergence=(
            "recorded"
            if account_effect in {"failure", "cooldown", "disable_auth"}
            else "released"
        ),
        provider_attributable=provider_attributable,
        source=obs.source,
        response_signal=obs.response_signal,
        retry_after_s=obs.retry_after_s,
        retry_action=action,
        wire_effect=wire_effect if action == "alternate_wire_same_account" else "none",
    )


def _bounded_retry_after(value: float | None) -> float:
    """Return a safe provider delay for the failure decision.

    Parsing happens at the HTTP boundary.  Missing, malformed, non-finite,
    and negative values use a short local fallback; a valid provider value is
    still bounded before it becomes a durable deadline.
    """
    if value is None or not math.isfinite(value) or value < 0.0:
        return 60.0
    return min(value, MAX_NONTERMINAL_BACKOFF_SECONDS)


def classify_failure_effects(obs: FailureObservation) -> FailureEffects:
    """Return the one canonical retry/effects decision for ``obs``."""
    ec = (obs.error_class or "").lower()

    # Request-local and operational failures never suppress a provider.
    if obs.source in {
        "client_validation",
        "local_preparation",
        "transcoding",
        "finalization",
        "database",
        "cancellation",
    }:
        local_evidence = (
            "client_validation"
            if obs.source == "client_validation"
            else f"{obs.source}_local"
        )
        return _decision(
            obs,
            client_outcome=(
                "client_error" if obs.source != "finalization" else "upstream_error"
            ),
            evidence_class=local_evidence,
        )
    if (
        obs.response_signal
        in {
            FailureSignal.CONTEXT_LIMIT_EXCEEDED,
            FailureSignal.UNSUPPORTED_REQUEST_CONTROL,
            FailureSignal.GENERIC_CLIENT_VALIDATION,
        }
        or "contextlimitexceeded" in ec
        or "context_limit_exceeded" in ec
    ):
        return _decision(
            obs,
            client_outcome="client_error",
            evidence_class="request_local_context_or_capability",
        )
    if "capability" in ec or "unsupported" in ec:
        return _decision(
            obs,
            client_outcome="client_error",
            evidence_class="request_local_capability",
        )

    # Cancellation is represented explicitly by newer callers and by the
    # legacy stream/no-response-start fact.
    if (
        obs.source == "stream"
        and obs.response_signal is None
        and not obs.response_started
    ):
        return _decision(obs, evidence_class="client_cancellation")

    if obs.source == "transport":
        return _decision(
            obs,
            retry=True,
            account_effect="failure",
            circuit_penalty=True,
            persist_backoff=True,
            backoff_reason="connection_failure",
            backoff_until=_now() + 30.0,
            release_probe_only=False,
            evidence_class="transport_failure",
            provider_attributable=True,
        )

    if (
        obs.source == "stream"
        and obs.response_signal == FailureSignal.TRANSPORT_FAILURE
    ):
        return _decision(
            obs,
            account_effect="failure",
            circuit_penalty=True,
            persist_backoff=True,
            backoff_reason="connection_failure",
            backoff_until=_now() + 30.0,
            release_probe_only=False,
            evidence_class="midstream_transport_failure",
            provider_attributable=True,
        )

    sc = obs.status_code
    signal = obs.response_signal
    signal_value = signal.value if signal is not None else "wire"

    if signal == FailureSignal.CREDENTIAL_INVALID:
        return _decision(
            obs,
            retry=True,
            retry_action="other_account_same_wire",
            client_outcome="client_error",
            account_effect="disable_auth",
            circuit_penalty=True,
            persist_backoff=True,
            backoff_reason="authentication_failed",
            release_probe_only=False,
            evidence_class="explicit_credential_invalid",
            provider_attributable=True,
        )

    # Alternate-wire transitions require deterministic rejection after the
    # request reached the upstream HTTP boundary. Never negotiate on an
    # ambiguous transport phase or after response handoff.
    if (
        signal
        in {
            FailureSignal.WIRE_AUTH_MISMATCH,
            FailureSignal.WIRE_SURFACE_UNSUPPORTED,
            FailureSignal.WIRE_SCHEMA_MISMATCH,
        }
        and obs.alternate_wire_available
        and obs.dispatch_phase == "response_status"
    ):
        return _decision(
            obs,
            retry=True,
            retry_action="alternate_wire_same_account",
            client_outcome="client_error",
            wire_effect="reject_candidate",
            evidence_class=f"{signal_value}_rejection",
        )

    if sc == 400:
        return _decision(
            obs, client_outcome="client_error", evidence_class="http_400_validation"
        )
    if sc == 401:
        # OpenCode Go returns HTTP 401 for model-not-found errors
        # (error.type="ModelError", "Model X is not supported").
        # The MODEL_ABSENT signal from the body pattern match takes
        # precedence over the status-code heuristic so we treat the
        # response as a transient client error rather than a terminal
        # authentication failure that would permanently disable the
        # account.
        if obs.response_signal == FailureSignal.MODEL_ABSENT:
            return _decision(
                obs,
                retry=True,
                client_outcome="client_error",
                evidence_class="http_401_model_absent",
            )
        return _decision(
            obs, client_outcome="client_error", evidence_class="http_401_ambiguous"
        )
    if sc == 402:
        return _decision(
            obs,
            retry=True,
            account_effect="quota",
            persist_backoff=True,
            backoff_reason="quota_exhausted",
            backoff_until=_now() + 300.0,
            release_probe_only=False,
            evidence_class="http_402_quota_exhausted",
            provider_attributable=True,
        )
    if sc == 403:
        if obs.response_signal == FailureSignal.QUOTA_EXHAUSTED:
            return _decision(
                obs,
                retry=True,
                client_outcome="client_error",
                account_effect="quota",
                persist_backoff=True,
                backoff_reason="quota_exhausted",
                backoff_until=_now() + 300.0,
                release_probe_only=False,
                evidence_class="http_403_quota_signal",
                provider_attributable=True,
            )
        if obs.response_signal == FailureSignal.CREDENTIAL_INVALID:
            return _decision(
                obs,
                retry=True,
                client_outcome="client_error",
                account_effect="disable_auth",
                circuit_penalty=True,
                persist_backoff=True,
                backoff_reason="authentication_failed",
                release_probe_only=False,
                evidence_class="http_403_auth_signal",
                provider_attributable=True,
            )
        return _decision(
            obs, client_outcome="client_error", evidence_class="http_403_no_evidence"
        )
    if sc == 404:
        model_like = obs.response_signal == FailureSignal.MODEL_ABSENT or (
            "modelunavailable" in ec or "model_not_found" in ec
        )
        if model_like:
            authoritative = obs.source == "provider_catalog"
            return _decision(
                obs,
                retry=True,
                client_outcome="client_error",
                model_effect=("terminal_withdrawal" if authoritative else "quarantine"),
                persist_backoff=True,
                backoff_reason="model_unavailable",
                backoff_until=None if authoritative else _now() + 300.0,
                release_probe_only=False,
                evidence_class=(
                    EVIDENCE_PROVIDER_CATALOG
                    if authoritative
                    else "runtime_model_absent_404"
                ),
                provider_attributable=True,
            )
        if obs.alternate_wire_available and obs.dispatch_phase == "response_status":
            return _decision(
                obs,
                retry=True,
                retry_action="alternate_wire_same_account",
                client_outcome="client_error",
                wire_effect="reject_candidate",
                evidence_class="http_404_wire_surface",
            )
        return _decision(
            obs, client_outcome="client_error", evidence_class="http_404_generic"
        )
    if sc == 408:
        return _decision(
            obs,
            retry=True,
            client_outcome="timeout",
            account_effect="failure",
            model_effect="quarantine",
            circuit_penalty=True,
            persist_backoff=True,
            backoff_reason="connect_timeout",
            backoff_until=_now() + 30.0,
            release_probe_only=False,
            evidence_class="http_408_timeout",
            provider_attributable=True,
        )
    if sc in (409, 422):
        if obs.response_signal in {
            FailureSignal.QUOTA_EXHAUSTED,
            FailureSignal.RATE_LIMITED,
        }:
            rate = obs.response_signal == FailureSignal.RATE_LIMITED
            return _decision(
                obs,
                retry=True,
                account_effect="rate_limit" if rate else "quota",
                persist_backoff=True,
                backoff_reason="rate_limited" if rate else "quota_exhausted",
                backoff_until=_now() + _bounded_retry_after(obs.retry_after_s),
                release_probe_only=False,
                evidence_class=f"http_{sc}_{'rate' if rate else 'quota'}_signal",
                provider_attributable=True,
            )
        return _decision(
            obs, client_outcome="client_error", evidence_class=f"http_{sc}_no_evidence"
        )
    if sc == 429:
        return _decision(
            obs,
            retry=True,
            account_effect="rate_limit",
            persist_backoff=True,
            backoff_reason="rate_limited",
            backoff_until=_now() + _bounded_retry_after(obs.retry_after_s),
            release_probe_only=False,
            evidence_class="http_429_rate_limited",
            provider_attributable=True,
        )
    if sc is not None and 500 <= sc < 600:
        return _decision(
            obs,
            retry=True,
            account_effect="failure",
            model_effect="quarantine",
            circuit_penalty=True,
            persist_backoff=True,
            backoff_reason="upstream_server_error",
            backoff_until=_now() + 20.0,
            release_probe_only=False,
            evidence_class=f"http_{sc}_server_error",
            provider_attributable=True,
        )
    return _decision(
        obs, client_outcome=_client_outcome(obs), evidence_class="unknown_fallback"
    )


def classify_failure(obs: FailureObservation) -> FailureEffects:
    """Canonical-name adapter for callers migrating from Plan 025."""
    return classify_failure_effects(obs)
