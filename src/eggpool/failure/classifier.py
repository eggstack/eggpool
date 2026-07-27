"""Pure failure-effects classifier with table-driven decision logic.

The classifier is a single pure function that maps a
:class:`FailureObservation` to a :class:`FailureEffects`.  Every
coordinator, finalizer, and health call site must consume the output
rather than independently reclassifying status and error class.

Design rules:

* Unknown validation defaults to zero shared-state effects and only
  releases any acquired probe slot.
* Local capability rejection (``CapabilityError``, ``ContextLimitExceededError``)
  produces no account/model/circuit effect.
* Upstream unsupported thinking control changes no shared health state.
* Client cancellation changes no provider health state.
* Finalization/database errors never create provider backoff.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from eggpool.failure.effects import FailureEffects
from eggpool.failure.signal import FailureSignal

if TYPE_CHECKING:
    from eggpool.failure.observation import FailureObservation

# ---------------------------------------------------------------------------
# Evidence provenance labels (Workstream E)
# ---------------------------------------------------------------------------
EVIDENCE_RUNTIME_HTTP = "runtime_http"
EVIDENCE_PROVIDER_CATALOG = "provider_catalog"
EVIDENCE_MODEL_INFO = "model_info"
EVIDENCE_MANUAL_OVERRIDE = "manual_override"
EVIDENCE_OPERATOR_ACTION = "operator_action"
EVIDENCE_MIGRATION_LEGACY = "migration_legacy"

# Client-outcome mapping
_CLIENT_OUTCOME_MAP: dict[int | None, str] = {
    None: "upstream_error",
    # 4xx → client_error (except 408 which is timeout)
    400: "client_error",
    401: "client_error",
    402: "upstream_error",
    403: "client_error",
    404: "client_error",
    408: "timeout",
    409: "client_error",
    422: "client_error",
    429: "upstream_error",
}

# Backoff epoch helpers
_TERMINAL_BACKOFF = None  # auth failures: persist indefinitely
_NO_BACKOFF = None


def _now() -> float:
    return time.time()


def _classify_client_outcome(obs: FailureObservation) -> str:
    """Determine the client-visible outcome category."""
    if obs.source in ("client_validation",):
        return "client_error"
    if obs.source in ("finalization", "database"):
        return "upstream_error"
    if obs.source == "transport":
        return "service_unavailable"
    if obs.source == "stream":
        return "upstream_error"
    # upstream_http — map by status code
    if obs.status_code is not None:
        if obs.status_code == 408:
            return "timeout"
        if 400 <= obs.status_code < 500:
            return "client_error"
        if 500 <= obs.status_code < 600:
            return "upstream_error"
    return "upstream_error"


def classify_failure_effects(obs: FailureObservation) -> FailureEffects:
    """Pure classifier: maps a failure observation to typed effects.

    This is the single authoritative decision point for all
    retry/shared-state effects.  Call sites must not reclassify
    independently.

    Decision table (see plans/025-failure-effects-and-model-quarantine.md
    Workstream C for the full matrix):

    * Client validation → release_probe_only
    * Context limit / capability rejection → release_probe_only
    * Generic HTTP 400/409/422 → release_probe_only
    * HTTP 401 confirmed auth → disable_auth, circuit penalty, persist
    * HTTP 403 quota signal → quota, persist
    * HTTP 403 without auth/quota → release_probe_only
    * HTTP 402 quota → quota, persist
    * HTTP 404 model-like → quarantine, persist (bounded)
    * HTTP 404 authoritative → terminal_withdrawal, persist
    * HTTP 404 generic → release_probe_only
    * HTTP 408/timeout → cooldown, circuit penalty, persist
    * HTTP 429 → rate_limit, persist
    * HTTP 5xx → cooldown, circuit penalty, persist
    * Client cancellation → release_probe_only
    * Midstream transport → failure, circuit penalty, persist
    * Finalization/database → no provider effect
    """
    # ------------------------------------------------------------------
    # Client validation / request-local failures
    # ------------------------------------------------------------------
    if obs.source == "client_validation":
        return FailureEffects(
            retry=False,
            retry_scope="none",
            client_outcome="client_error",
            account_effect="none",
            model_effect="none",
            circuit_penalty=False,
            persist_backoff=False,
            backoff_reason=None,
            backoff_until=None,
            release_probe_only=True,
            evidence_class="client_validation",
        )

    # Context-limit / capability rejection
    if obs.response_signal in (
        FailureSignal.CONTEXT_LIMIT_EXCEEDED,
        FailureSignal.UNSUPPORTED_REQUEST_CONTROL,
        FailureSignal.GENERIC_CLIENT_VALIDATION,
    ):
        return FailureEffects(
            retry=False,
            retry_scope="none",
            client_outcome=_classify_client_outcome(obs),
            account_effect="none",
            model_effect="none",
            circuit_penalty=False,
            persist_backoff=False,
            backoff_reason=None,
            backoff_until=None,
            release_probe_only=True,
            evidence_class=(
                f"request_local_{obs.response_signal.value}"
                if obs.response_signal is not None
                else "request_local_unknown"
            ),
        )

    # Error-class based request-local detection
    ec = (obs.error_class or "").lower()
    if "contextlimitexceeded" in ec or "context_limit_exceeded" in ec:
        return FailureEffects(
            retry=False,
            retry_scope="none",
            client_outcome=_classify_client_outcome(obs),
            account_effect="none",
            model_effect="none",
            circuit_penalty=False,
            persist_backoff=False,
            backoff_reason=None,
            backoff_until=None,
            release_probe_only=True,
            evidence_class="request_local_context_limit",
        )
    if "capability" in ec or "unsupported" in ec:
        return FailureEffects(
            retry=False,
            retry_scope="none",
            client_outcome=_classify_client_outcome(obs),
            account_effect="none",
            model_effect="none",
            circuit_penalty=False,
            persist_backoff=False,
            backoff_reason=None,
            backoff_until=None,
            release_probe_only=True,
            evidence_class="request_local_capability",
        )

    # ------------------------------------------------------------------
    # Non-HTTP sources
    # ------------------------------------------------------------------
    if obs.source == "finalization" or obs.source == "database":
        return FailureEffects(
            retry=False,
            retry_scope="none",
            client_outcome="upstream_error",
            account_effect="none",
            model_effect="none",
            circuit_penalty=False,
            persist_backoff=False,
            backoff_reason=None,
            backoff_until=None,
            release_probe_only=True,
            evidence_class=f"provider_local_{obs.source}",
        )

    if obs.source == "transport":
        return FailureEffects(
            retry=False,
            retry_scope="none",
            client_outcome="service_unavailable",
            account_effect="failure",
            model_effect="none",
            circuit_penalty=True,
            persist_backoff=True,
            backoff_reason="connection_failure",
            backoff_until=_now() + 30.0,
            release_probe_only=False,
            evidence_class="transport_failure",
        )

    if (
        obs.source == "stream"
        and obs.response_signal == FailureSignal.TRANSPORT_FAILURE
    ):
        # Midstream transport failure — no retry after bytes received
        return FailureEffects(
            retry=False,
            retry_scope="none",
            client_outcome="upstream_error",
            account_effect="failure",
            model_effect="none",
            circuit_penalty=True,
            persist_backoff=True,
            backoff_reason="connection_failure",
            backoff_until=_now() + 30.0,
            release_probe_only=False,
            evidence_class="midstream_transport_failure",
        )

    # Client cancellation (any source)
    if (
        obs.source == "stream"
        and obs.response_signal is None
        and not obs.response_started
    ):
        return FailureEffects(
            retry=False,
            retry_scope="none",
            client_outcome="upstream_error",
            account_effect="none",
            model_effect="none",
            circuit_penalty=False,
            persist_backoff=False,
            backoff_reason=None,
            backoff_until=None,
            release_probe_only=True,
            evidence_class="client_cancellation",
        )

    # ------------------------------------------------------------------
    # HTTP status-based classification
    # ------------------------------------------------------------------
    sc = obs.status_code

    # HTTP 400 — generic bad request
    if sc == 400:
        return FailureEffects(
            retry=False,
            retry_scope="none",
            client_outcome="client_error",
            account_effect="none",
            model_effect="none",
            circuit_penalty=False,
            persist_backoff=False,
            backoff_reason=None,
            backoff_until=None,
            release_probe_only=True,
            evidence_class="http_400_validation",
        )

    # HTTP 401 — confirmed authentication failure
    if sc == 401:
        return FailureEffects(
            retry=False,
            retry_scope="other_account",
            client_outcome="client_error",
            account_effect="disable_auth",
            model_effect="none",
            circuit_penalty=True,
            persist_backoff=True,
            backoff_reason="authentication_failed",
            backoff_until=None,  # terminal
            release_probe_only=False,
            evidence_class="http_401_auth_failure",
        )

    # HTTP 402 — quota exhausted
    if sc == 402:
        return FailureEffects(
            retry=False,
            retry_scope="other_account",
            client_outcome="upstream_error",
            account_effect="quota",
            model_effect="none",
            circuit_penalty=False,
            persist_backoff=True,
            backoff_reason="quota_exhausted",
            backoff_until=_now() + 300.0,
            release_probe_only=False,
            evidence_class="http_402_quota_exhausted",
        )

    # HTTP 403 — ambiguous: check signal
    if sc == 403:
        if obs.response_signal == FailureSignal.QUOTA_EXHAUSTED:
            return FailureEffects(
                retry=False,
                retry_scope="other_account",
                client_outcome="client_error",
                account_effect="quota",
                model_effect="none",
                circuit_penalty=False,
                persist_backoff=True,
                backoff_reason="quota_exhausted",
                backoff_until=_now() + 300.0,
                release_probe_only=False,
                evidence_class="http_403_quota_signal",
            )
        if obs.response_signal == FailureSignal.AUTHENTICATION_FAILED:
            return FailureEffects(
                retry=False,
                retry_scope="other_account",
                client_outcome="client_error",
                account_effect="disable_auth",
                model_effect="none",
                circuit_penalty=True,
                persist_backoff=True,
                backoff_reason="authentication_failed",
                backoff_until=None,
                release_probe_only=False,
                evidence_class="http_403_auth_signal",
            )
        # 403 without auth/quota evidence → request-local
        return FailureEffects(
            retry=False,
            retry_scope="none",
            client_outcome="client_error",
            account_effect="none",
            model_effect="none",
            circuit_penalty=False,
            persist_backoff=False,
            backoff_reason=None,
            backoff_until=None,
            release_probe_only=True,
            evidence_class="http_403_no_evidence",
        )

    # HTTP 404 — model-specific vs generic
    if sc == 404:
        is_model_404 = obs.response_signal == FailureSignal.MODEL_ABSENT
        # Also detect by error class
        if not is_model_404:
            is_model_404 = "modelunavailable" in ec or "model_not_found" in ec

        if is_model_404:
            # Check if this is authoritative (provider catalog) or runtime-only
            if obs.source == "provider_catalog":
                return FailureEffects(
                    retry=False,
                    retry_scope="other_account",
                    client_outcome="client_error",
                    account_effect="none",
                    model_effect="terminal_withdrawal",
                    circuit_penalty=False,
                    persist_backoff=True,
                    backoff_reason="model_unavailable",
                    backoff_until=None,  # terminal
                    release_probe_only=False,
                    evidence_class=EVIDENCE_PROVIDER_CATALOG,
                )
            # Runtime-only model-like 404 → bounded quarantine
            return FailureEffects(
                retry=False,
                retry_scope="other_account",
                client_outcome="client_error",
                account_effect="none",
                model_effect="quarantine",
                circuit_penalty=False,
                persist_backoff=True,
                backoff_reason="model_unavailable",
                backoff_until=_now() + 300.0,
                release_probe_only=False,
                evidence_class="runtime_model_absent_404",
            )
        # Generic 404 (route not found)
        return FailureEffects(
            retry=False,
            retry_scope="none",
            client_outcome="client_error",
            account_effect="none",
            model_effect="none",
            circuit_penalty=False,
            persist_backoff=False,
            backoff_reason=None,
            backoff_until=None,
            release_probe_only=True,
            evidence_class="http_404_generic",
        )

    # HTTP 408 — timeout
    if sc == 408:
        return FailureEffects(
            retry=True,
            retry_scope="other_account",
            client_outcome="timeout",
            account_effect="cooldown",
            model_effect="none",
            circuit_penalty=True,
            persist_backoff=True,
            backoff_reason="connect_timeout",
            backoff_until=_now() + 30.0,
            release_probe_only=False,
            evidence_class="http_408_timeout",
        )

    # HTTP 409 / 422 — provider-specific client errors
    if sc in (409, 422):
        return FailureEffects(
            retry=False,
            retry_scope="none",
            client_outcome="client_error",
            account_effect="none",
            model_effect="none",
            circuit_penalty=False,
            persist_backoff=False,
            backoff_reason=None,
            backoff_until=None,
            release_probe_only=True,
            evidence_class=f"http_{sc}_provider_specific",
        )

    # HTTP 429 — rate limited
    if sc == 429:
        retry_after = obs.retry_after_s if obs.retry_after_s is not None else 60.0
        return FailureEffects(
            retry=True,
            retry_scope="other_account",
            client_outcome="upstream_error",
            account_effect="rate_limit",
            model_effect="none",
            circuit_penalty=False,
            persist_backoff=True,
            backoff_reason="rate_limited",
            backoff_until=_now() + retry_after,
            release_probe_only=False,
            evidence_class="http_429_rate_limited",
        )

    # HTTP 5xx
    if sc is not None and 500 <= sc < 600:
        return FailureEffects(
            retry=True,
            retry_scope="other_account",
            client_outcome="upstream_error",
            account_effect="cooldown",
            model_effect="none",
            circuit_penalty=True,
            persist_backoff=True,
            backoff_reason="upstream_server_error",
            backoff_until=_now() + 20.0,
            release_probe_only=False,
            evidence_class=f"http_{sc}_server_error",
        )

    # ------------------------------------------------------------------
    # Fallback: unknown — zero shared-state effects
    # ------------------------------------------------------------------
    return FailureEffects(
        retry=False,
        retry_scope="none",
        client_outcome=_classify_client_outcome(obs),
        account_effect="none",
        model_effect="none",
        circuit_penalty=False,
        persist_backoff=False,
        backoff_reason=None,
        backoff_until=None,
        release_probe_only=True,
        evidence_class="unknown_fallback",
    )
