"""Failure effects decision table tests.

Validates the pure classifier against every row of the effects matrix,
covering 400, 401, 402, 403 quota/non-quota, model-like and generic
404, 408, 409, 422, 429, all relevant 5xx statuses, transport
exceptions, client cancellation, midstream failure, capability errors,
finalization failures, and database failures.
"""

from __future__ import annotations

from eggpool.failure.classifier import classify_failure_effects
from eggpool.failure.observation import FailureObservation
from eggpool.failure.signal import FailureSignal


def _obs(
    *,
    source: str = "upstream_http",
    status_code: int | None = None,
    error_class: str | None = None,
    provider_id: str | None = "openai",
    account_name: str | None = "acct-1",
    model_id: str | None = "gpt-4o",
    upstream_model_id: str | None = None,
    client_protocol: str = "openai",
    upstream_protocol: str = "openai",
    response_signal: FailureSignal | None = None,
    retry_after_s: float | None = None,
    response_started: bool = True,
    downstream_started: bool = False,
    alternate_wire_available: bool = False,
    dispatch_phase: str = "response_status",
) -> FailureObservation:
    return FailureObservation(
        source=source,
        status_code=status_code,
        error_class=error_class,
        provider_id=provider_id,
        account_name=account_name,
        model_id=model_id,
        upstream_model_id=upstream_model_id,
        client_protocol=client_protocol,
        upstream_protocol=upstream_protocol,
        response_signal=response_signal,
        retry_after_s=retry_after_s,
        response_started=response_started,
        downstream_started=downstream_started,
        alternate_wire_available=alternate_wire_available,
        dispatch_phase=dispatch_phase,
    )


class TestFailureEffectsMatrix:
    """Full status/body/error-class matrix tests."""

    # --- Client validation (request-local) ---

    def test_client_validation(self) -> None:
        obs = _obs(source="client_validation")
        fx = classify_failure_effects(obs)
        assert fx.retry is False
        assert fx.account_effect == "none"
        assert fx.model_effect == "none"
        assert fx.circuit_penalty is False
        assert fx.persist_backoff is False
        assert fx.release_probe_only is True
        assert fx.evidence_class == "client_validation"

    # --- Context limit / capability rejection ---

    def test_context_limit_from_signal(self) -> None:
        obs = _obs(
            response_signal=FailureSignal.CONTEXT_LIMIT_EXCEEDED,
            status_code=400,
        )
        fx = classify_failure_effects(obs)
        assert fx.retry is False
        assert fx.account_effect == "none"
        assert fx.model_effect == "none"
        assert fx.circuit_penalty is False
        assert fx.release_probe_only is True

    def test_context_limit_from_error_class(self) -> None:
        obs = _obs(error_class="ContextLimitExceeded", status_code=400)
        fx = classify_failure_effects(obs)
        assert fx.account_effect == "none"
        assert fx.model_effect == "none"
        assert fx.release_probe_only is True

    def test_unsupported_thinking_control(self) -> None:
        obs = _obs(
            response_signal=FailureSignal.UNSUPPORTED_REQUEST_CONTROL,
            status_code=400,
        )
        fx = classify_failure_effects(obs)
        assert fx.account_effect == "none"
        assert fx.model_effect == "none"
        assert fx.release_probe_only is True

    def test_capability_rejection_from_error_class(self) -> None:
        obs = _obs(error_class="CapabilityError", status_code=400)
        fx = classify_failure_effects(obs)
        assert fx.account_effect == "none"
        assert fx.model_effect == "none"
        assert fx.release_probe_only is True

    # --- HTTP 400 ---

    def test_http_400_validation(self) -> None:
        obs = _obs(status_code=400)
        fx = classify_failure_effects(obs)
        assert fx.retry is False
        assert fx.retry_scope == "none"
        assert fx.client_outcome == "client_error"
        assert fx.account_effect == "none"
        assert fx.model_effect == "none"
        assert fx.circuit_penalty is False
        assert fx.persist_backoff is False

    # --- HTTP 401 ---

    def test_http_401_without_evidence_is_not_auth_failure(self) -> None:
        obs = _obs(status_code=401)
        fx = classify_failure_effects(obs)
        assert fx.retry is False
        assert fx.retry_scope == "none"
        assert fx.client_outcome == "client_error"
        assert fx.account_effect == "none"
        assert fx.circuit_penalty is False
        assert fx.persist_backoff is False
        assert fx.evidence_class == "http_401_ambiguous"

    def test_explicit_credential_invalid_disables_only_account(self) -> None:
        obs = _obs(
            status_code=401,
            response_signal=FailureSignal.CREDENTIAL_INVALID,
        )
        fx = classify_failure_effects(obs)
        assert fx.retry_action == "other_account_same_wire"
        assert fx.retry_scope == "other_account"
        assert fx.account_effect == "disable_auth"
        assert fx.wire_effect == "none"

    def test_wire_auth_mismatch_retries_same_account_on_alternate(self) -> None:
        obs = _obs(
            status_code=401,
            response_signal=FailureSignal.WIRE_AUTH_MISMATCH,
            alternate_wire_available=True,
        )
        fx = classify_failure_effects(obs)
        assert fx.retry_action == "alternate_wire_same_account"
        assert fx.retry_scope == "same_account_other_wire"
        assert fx.wire_effect == "reject_candidate"
        assert fx.account_effect == "none"
        assert fx.circuit_penalty is False

    def test_wire_rejection_after_handoff_cannot_retry(self) -> None:
        obs = _obs(
            status_code=400,
            response_signal=FailureSignal.WIRE_SCHEMA_MISMATCH,
            alternate_wire_available=True,
            downstream_started=True,
        )
        fx = classify_failure_effects(obs)
        assert fx.retry is False
        assert fx.retry_action == "none"
        assert fx.wire_effect == "none"

    def test_http_401_model_absent_opencode_go(self) -> None:
        """OpenCode Go returns 401 for model-not-found errors.

        The body contains 'Model X is not supported' which the signal
        extractor recognises as MODEL_ABSENT.  The classifier must treat
        this as a transient client error rather than a terminal auth
        failure so the account is not permanently disabled.
        """
        obs = _obs(
            status_code=401,
            response_signal=FailureSignal.MODEL_ABSENT,
        )
        fx = classify_failure_effects(obs)
        assert fx.retry is True
        assert fx.retry_scope == "other_account"
        assert fx.client_outcome == "client_error"
        assert fx.account_effect == "none"
        assert fx.model_effect == "none"
        assert fx.circuit_penalty is False
        assert fx.persist_backoff is False

    # --- HTTP 402 ---

    def test_http_402_quota(self) -> None:
        obs = _obs(status_code=402)
        fx = classify_failure_effects(obs)
        assert fx.retry is True
        assert fx.retry_scope == "other_account"
        assert fx.account_effect == "quota"
        assert fx.model_effect == "none"
        assert fx.persist_backoff is True
        assert fx.backoff_reason == "quota_exhausted"
        assert fx.backoff_until is not None

    # --- HTTP 403 quota signal ---

    def test_http_403_quota_signal(self) -> None:
        obs = _obs(
            status_code=403,
            response_signal=FailureSignal.QUOTA_EXHAUSTED,
        )
        fx = classify_failure_effects(obs)
        assert fx.retry_scope == "other_account"
        assert fx.account_effect == "quota"
        assert fx.persist_backoff is True
        assert fx.backoff_reason == "quota_exhausted"

    # --- HTTP 403 auth signal ---

    def test_http_403_auth_signal(self) -> None:
        obs = _obs(
            status_code=403,
            response_signal=FailureSignal.AUTHENTICATION_FAILED,
        )
        fx = classify_failure_effects(obs)
        assert fx.account_effect == "disable_auth"
        assert fx.circuit_penalty is True

    # --- HTTP 403 no evidence ---

    def test_http_403_no_evidence(self) -> None:
        obs = _obs(status_code=403)
        fx = classify_failure_effects(obs)
        assert fx.retry is False
        assert fx.account_effect == "none"
        assert fx.model_effect == "none"
        assert fx.circuit_penalty is False
        assert fx.persist_backoff is False
        assert fx.release_probe_only is True

    # --- HTTP 404 model-specific ---

    def test_http_404_model_absent_runtime(self) -> None:
        obs = _obs(
            status_code=404,
            response_signal=FailureSignal.MODEL_ABSENT,
        )
        fx = classify_failure_effects(obs)
        assert fx.retry is True
        assert fx.retry_scope == "other_account"
        assert fx.account_effect == "none"
        assert fx.model_effect == "quarantine"
        assert fx.persist_backoff is True
        assert fx.backoff_reason == "model_unavailable"
        assert fx.backoff_until is not None  # bounded

    def test_http_404_model_absent_authoritative(self) -> None:
        obs = _obs(
            status_code=404,
            response_signal=FailureSignal.MODEL_ABSENT,
            source="provider_catalog",
        )
        fx = classify_failure_effects(obs)
        assert fx.retry is True
        assert fx.model_effect == "terminal_withdrawal"
        assert fx.backoff_until is None  # terminal

    def test_http_404_model_unavailable_error_class(self) -> None:
        obs = _obs(status_code=404, error_class="ModelUnavailable")
        fx = classify_failure_effects(obs)
        assert fx.retry is True
        assert fx.model_effect == "quarantine"

    # --- HTTP 404 generic ---

    def test_http_404_generic(self) -> None:
        obs = _obs(status_code=404)
        fx = classify_failure_effects(obs)
        assert fx.retry is False
        assert fx.account_effect == "none"
        assert fx.model_effect == "none"
        assert fx.persist_backoff is False
        assert fx.release_probe_only is True

    def test_http_404_with_alternate_wire_rejects_candidate(self) -> None:
        obs = _obs(status_code=404, alternate_wire_available=True)
        fx = classify_failure_effects(obs)
        assert fx.retry_action == "alternate_wire_same_account"
        assert fx.wire_effect == "reject_candidate"

    # --- HTTP 408 ---

    def test_http_408_timeout(self) -> None:
        obs = _obs(status_code=408)
        fx = classify_failure_effects(obs)
        assert fx.retry is True
        assert fx.retry_scope == "other_account"
        assert fx.client_outcome == "timeout"
        # 408 is a per-model transient timeout; the account itself
        # remains eligible so a single timed-out request does not
        # block sibling models on the same account.
        assert fx.account_effect == "failure"
        assert fx.model_effect == "quarantine"
        assert fx.circuit_penalty is True
        assert fx.persist_backoff is True
        assert fx.backoff_reason == "connect_timeout"

    # --- HTTP 409 / 422 ---

    def test_http_409_provider_specific(self) -> None:
        obs = _obs(status_code=409)
        fx = classify_failure_effects(obs)
        assert fx.retry is False
        assert fx.account_effect == "none"
        assert fx.model_effect == "none"
        assert fx.release_probe_only is True

    def test_http_409_quota_signal_retries(self) -> None:
        obs = _obs(
            status_code=409,
            response_signal=FailureSignal.QUOTA_EXHAUSTED,
        )
        fx = classify_failure_effects(obs)
        assert fx.retry is True
        assert fx.account_effect == "quota"

    def test_http_422_provider_specific(self) -> None:
        obs = _obs(status_code=422)
        fx = classify_failure_effects(obs)
        assert fx.retry is False
        assert fx.account_effect == "none"
        assert fx.model_effect == "none"
        assert fx.release_probe_only is True

    def test_http_422_quota_signal_retries(self) -> None:
        obs = _obs(
            status_code=422,
            response_signal=FailureSignal.RATE_LIMITED,
        )
        fx = classify_failure_effects(obs)
        assert fx.retry is True
        assert fx.account_effect == "rate_limit"

    # --- HTTP 429 ---

    def test_http_429_rate_limited(self) -> None:
        obs = _obs(status_code=429)
        fx = classify_failure_effects(obs)
        assert fx.retry is True
        assert fx.retry_scope == "other_account"
        assert fx.account_effect == "rate_limit"
        assert fx.model_effect == "none"
        assert fx.circuit_penalty is False
        assert fx.persist_backoff is True
        assert fx.backoff_reason == "rate_limited"
        assert fx.backoff_until is not None
        assert fx.retry_action == "other_account_same_wire"
        assert fx.wire_effect == "none"

    def test_http_429_with_retry_after(self) -> None:
        obs = _obs(status_code=429, retry_after_s=120.0)
        fx = classify_failure_effects(obs)
        assert fx.backoff_until is not None

    # --- HTTP 5xx ---

    def test_http_500_server_error(self) -> None:
        obs = _obs(status_code=500)
        fx = classify_failure_effects(obs)
        assert fx.retry is True
        assert fx.retry_scope == "other_account"
        assert fx.client_outcome == "upstream_error"
        # 5xx is now scoped to per-model quarantine, not account-wide
        # cooldown, so a single bad model on an account does not block
        # routing for sibling models that share the same account.
        assert fx.account_effect == "failure"
        assert fx.model_effect == "quarantine"
        assert fx.circuit_penalty is True
        assert fx.persist_backoff is True
        assert fx.backoff_reason == "upstream_server_error"

    def test_http_502_bad_gateway(self) -> None:
        obs = _obs(status_code=502)
        fx = classify_failure_effects(obs)
        assert fx.retry is True
        assert fx.account_effect == "failure"
        assert fx.model_effect == "quarantine"
        assert fx.circuit_penalty is True

    def test_http_503_service_unavailable(self) -> None:
        obs = _obs(status_code=503)
        fx = classify_failure_effects(obs)
        assert fx.retry is True
        assert fx.account_effect == "failure"
        assert fx.model_effect == "quarantine"
        assert fx.circuit_penalty is True

    def test_http_504_gateway_timeout(self) -> None:
        obs = _obs(status_code=504)
        fx = classify_failure_effects(obs)
        assert fx.retry is True
        assert fx.account_effect == "failure"
        assert fx.model_effect == "quarantine"
        assert fx.circuit_penalty is True

    # --- Client cancellation ---

    def test_client_cancellation(self) -> None:
        obs = _obs(
            source="stream",
            response_signal=None,
            response_started=False,
        )
        fx = classify_failure_effects(obs)
        assert fx.retry is False
        assert fx.account_effect == "none"
        assert fx.model_effect == "none"
        assert fx.circuit_penalty is False
        assert fx.persist_backoff is False
        assert fx.release_probe_only is True

    # --- Midstream transport failure ---

    def test_midstream_transport_failure(self) -> None:
        obs = _obs(
            source="stream",
            response_signal=FailureSignal.TRANSPORT_FAILURE,
            response_started=True,
        )
        fx = classify_failure_effects(obs)
        assert fx.retry is False
        assert fx.account_effect == "failure"
        assert fx.model_effect == "none"
        assert fx.circuit_penalty is True
        assert fx.persist_backoff is True
        assert fx.backoff_reason == "connection_failure"

    # --- Transport failure ---

    def test_transport_failure(self) -> None:
        obs = _obs(source="transport")
        fx = classify_failure_effects(obs)
        assert fx.retry is True
        assert fx.account_effect == "failure"
        assert fx.model_effect == "none"
        assert fx.circuit_penalty is True
        assert fx.persist_backoff is True
        assert fx.backoff_reason == "connection_failure"

    # --- Finalization/database failures ---

    def test_finalization_failure(self) -> None:
        obs = _obs(source="finalization")
        fx = classify_failure_effects(obs)
        assert fx.retry is False
        assert fx.account_effect == "none"
        assert fx.model_effect == "none"
        assert fx.circuit_penalty is False
        assert fx.persist_backoff is False
        assert fx.release_probe_only is True

    def test_database_failure(self) -> None:
        obs = _obs(source="database")
        fx = classify_failure_effects(obs)
        assert fx.retry is False
        assert fx.account_effect == "none"
        assert fx.model_effect == "none"
        assert fx.circuit_penalty is False
        assert fx.persist_backoff is False
        assert fx.release_probe_only is True

    # --- Unknown fallback ---

    def test_unknown_status_code(self) -> None:
        obs = _obs(status_code=999)
        fx = classify_failure_effects(obs)
        assert fx.retry is False
        assert fx.account_effect == "none"
        assert fx.model_effect == "none"
        assert fx.circuit_penalty is False
        assert fx.persist_backoff is False
        assert fx.release_probe_only is True
        assert fx.evidence_class == "unknown_fallback"


class TestMandatoryDefault:
    """Unknown validation produces zero shared-state effects."""

    def test_no_status_no_signal(self) -> None:
        obs = _obs(source="upstream_http", status_code=None, response_signal=None)
        fx = classify_failure_effects(obs)
        assert fx.account_effect == "none"
        assert fx.model_effect == "none"
        assert fx.circuit_penalty is False
        assert fx.persist_backoff is False
        assert fx.release_probe_only is True

    def test_no_error_class(self) -> None:
        obs = _obs(error_class=None, status_code=500)
        fx = classify_failure_effects(obs)
        assert fx.account_effect != "none"  # 5xx does penalize
        assert fx.circuit_penalty is True
