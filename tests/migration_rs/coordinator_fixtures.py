"""Deterministic C001 observations from the live Python coordinator boundary.

The module is an observation adapter, not a second coordinator.  It calls the
production failure, stream, wire, and lifecycle primitives and projects only
the bounded facts that a later Rust implementation must preserve.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

from eggpool.failure.classifier import classify_failure_effects
from eggpool.failure.observation import FailureObservation
from eggpool.failure.signal import FailureSignal
from eggpool.models.config import AppConfig
from eggpool.proxy.sse_observer import IncrementalSSEObserver, StreamCompletionSnapshot
from eggpool.request.finalization_job import (
    ClaimCompensationProgress,
    FailedAttemptCleanupProgress,
    FinalizationIdentity,
    FinalizationProgress,
    RuntimePublicationReceipt,
)
from eggpool.request.response_handoff import ResponseHandoffState
from eggpool.request.stream_completion import classify_stream_eof
from eggpool.retry.classification import RetryClassifier
from eggpool.wire.registry import resolve_provider_wire_profiles
from eggpool.wire.resolver import WireProfileResolver

FIXTURE_DIR = Path(__file__).parents[2] / "migration-rs" / "fixtures" / "coordinator"
MATRIX_PATH = FIXTURE_DIR / "c001-fixture-matrix.json"
OBSERVATION_PATH = FIXTURE_DIR / "c001-python-observations.json"

LIFECYCLE_STATES = (
    "admitted",
    "locally_claimed",
    "durable_attempt_published",
    "wire_selected",
    "dispatching",
    "upstream_headers_received",
    "downstream_started",
    "streaming",
    "terminal_command_registered",
    "durable_terminal",
    "runtime_released",
    "completed",
)

DURABLE_ROWS = {
    "requests": (
        "id",
        "account_id",
        "model_id",
        "status",
        "protocol",
        "streamed",
        "proxy_request_id",
        "provider_id",
        "first_attempt_at",
        "last_attempt_id",
        "completed_at",
        "status_code",
        "error_class",
        "retry_count",
        "input_tokens",
        "output_tokens",
        "cost_microdollars",
    ),
    "request_attempts": (
        "id",
        "request_id",
        "attempt_number",
        "account_id",
        "provider_id",
        "model_id",
        "protocol",
        "streamed",
        "status_code",
        "error_class",
        "upstream_request_id",
        "bytes_emitted",
        "bytes_received",
        "latency_ms",
        "retry_category",
        "release_reason",
        "is_retry_outcome",
        "completed_at",
    ),
    "reservations": (
        "id",
        "request_id",
        "account_id",
        "model_id",
        "reserved_microdollars",
        "estimated_tokens",
        "status",
        "expires_at",
        "released_at",
        "release_reason",
    ),
    "routing_decisions": (
        "id",
        "request_id",
        "attempt_number",
        "model_id",
        "provider_id",
        "protocol",
        "selected_account_id",
        "selected_account_name",
        "selected_tier",
        "selected_score",
        "eligible_count",
        "scored_count",
        "attempted_excluded_count",
        "exclude_reasons_json",
        "decision_made_at",
    ),
}

OWNERSHIP_COMPONENTS = (
    "pending_request_load",
    "pending_token_load",
    "active_request_count",
    "quota_reservation",
    "health_probe",
    "durable_request",
    "durable_attempt",
    "durable_reservation",
    "routing_decision",
    "wire_negotiation_flight",
    "retained_finalization_job",
)

FAILURE_CASES: tuple[dict[str, Any], ...] = (
    {"name": "client_validation", "source": "client_validation", "status": 400},
    {"name": "local_preparation", "source": "local_preparation", "status": None},
    {"name": "connect_error", "source": "transport", "error": "ConnectError"},
    {"name": "proxy_error", "source": "transport", "error": "ProxyError"},
    {"name": "tls_error", "source": "transport", "error": "TLSVerificationError"},
    {"name": "write_error", "source": "transport", "error": "WriteError"},
    {"name": "read_error", "source": "transport", "error": "ReadError"},
    {"name": "pool_timeout", "source": "transport", "error": "PoolTimeout"},
    {"name": "http_400", "source": "upstream_http", "status": 400},
    {"name": "http_401_ambiguous", "source": "upstream_http", "status": 401},
    {
        "name": "http_401_explicit_credential",
        "source": "upstream_http",
        "status": 401,
        "signal": FailureSignal.CREDENTIAL_INVALID,
    },
    {"name": "http_403_no_evidence", "source": "upstream_http", "status": 403},
    {
        "name": "http_403_wire_auth_mismatch",
        "source": "upstream_http",
        "status": 403,
        "signal": FailureSignal.WIRE_AUTH_MISMATCH,
        "alternate_wire": True,
    },
    {
        "name": "http_404_model_absent",
        "source": "upstream_http",
        "status": 404,
        "signal": FailureSignal.MODEL_ABSENT,
    },
    {
        "name": "http_404_surface_mismatch",
        "source": "upstream_http",
        "status": 404,
        "signal": FailureSignal.WIRE_SURFACE_UNSUPPORTED,
        "alternate_wire": True,
    },
    {"name": "http_408_timeout", "source": "upstream_http", "status": 408},
    {"name": "http_409_conflict", "source": "upstream_http", "status": 409},
    {
        "name": "http_429_rate_limit",
        "source": "upstream_http",
        "status": 429,
        "signal": FailureSignal.RATE_LIMITED,
    },
    {"name": "http_500_server", "source": "upstream_http", "status": 500},
    {"name": "http_503_server", "source": "upstream_http", "status": 503},
    {
        "name": "post_handoff_500",
        "source": "upstream_http",
        "status": 500,
        "downstream_started": True,
    },
    {"name": "finalization_database", "source": "database", "status": None},
    {"name": "client_cancel_before_handoff", "source": "cancellation", "status": None},
)

STREAM_CASES = (
    {
        "name": "openai_terminal",
        "protocol": "openai",
        "surface": "chat_completions",
        "policy": "strict",
        "raw": b'data: {"choices":[{"delta":{"content":"x"}}]}\n\ndata: [DONE]\n\n',
    },
    {
        "name": "anthropic_terminal",
        "protocol": "anthropic",
        "surface": "messages",
        "policy": "strict",
        "raw": b"event: message_stop\ndata: {}\n\n",
    },
    {
        "name": "responses_terminal_failure",
        "protocol": "openai",
        "surface": "responses",
        "policy": "strict",
        "raw": b'event: response.failed\ndata: {"type":"response.failed"}\n\n',
    },
    {
        "name": "responses_terminal_incomplete",
        "protocol": "openai",
        "surface": "responses",
        "policy": "strict",
        "raw": b'event: response.incomplete\ndata: {"type":"response.incomplete"}\n\n',
    },
    {
        "name": "empty_eof",
        "protocol": "openai",
        "surface": "chat_completions",
        "policy": "strict",
        "raw": b"",
    },
    {
        "name": "partial_eof",
        "protocol": "openai",
        "surface": "chat_completions",
        "policy": "strict",
        "raw": b'data: {"choices":[]',
    },
    {
        "name": "malformed_eof",
        "protocol": "openai",
        "surface": "chat_completions",
        "policy": "strict",
        "raw": b"data: {not-json}\n\n",
    },
    {
        "name": "compatibility_eof",
        "protocol": "openai",
        "surface": "chat_completions",
        "policy": "compatible",
        "raw": (
            b'data: {"choices":[],"usage":{"prompt_tokens":1,'
            b'"completion_tokens":0,"total_tokens":1}}\n\n'
        ),
    },
    {
        "name": "gemini_interactions_terminal",
        "protocol": "openai",
        "surface": "chat_completions",
        "wire_surface": "gemini_interactions",
        "policy": "strict",
        "raw": (
            b'data: {"event_type":"interaction.completed",'
            b'"interaction":{"status":"completed"}}\n\n'
        ),
    },
    {
        "name": "gemini_generate_terminal",
        "protocol": "openai",
        "surface": "chat_completions",
        "wire_surface": "gemini_generate_content",
        "policy": "strict",
        "raw": b'data: {"candidates":[{"finishReason":"STOP"}]}\n\n',
    },
    {
        "name": "gemini_incomplete_terminal",
        "protocol": "openai",
        "surface": "chat_completions",
        "wire_surface": "gemini_generate_content",
        "policy": "strict",
        "raw": b'data: {"candidates":[{"finishReason":"MAX_TOKENS"}]}\n\n',
    },
)


def _failure_observation(case: dict[str, Any]) -> FailureObservation:
    return FailureObservation(
        source=case["source"],
        status_code=case.get("status"),
        error_class=case.get("error"),
        provider_id="provider-fixture" if case["source"] == "upstream_http" else None,
        account_name="account-fixture"
        if case["source"] in {"transport", "upstream_http"}
        else None,
        model_id="model-fixture",
        upstream_model_id="model-fixture",
        client_protocol="openai",
        upstream_protocol="openai",
        response_signal=case.get("signal"),
        retry_after_s=12.0 if case.get("status") == 429 else None,
        response_started=False,
        downstream_started=bool(case.get("downstream_started", False)),
        credential_configured=True,
        alternate_wire_available=bool(case.get("alternate_wire", False)),
        provider_model_presence="known",
        dispatch_phase="response_status",
    )


def _failure_observations() -> list[dict[str, Any]]:
    with patch("eggpool.failure.classifier._now", return_value=1_700_000_000.0):
        result = []
        for case in FAILURE_CASES:
            effects = classify_failure_effects(_failure_observation(case))
            result.append(
                {
                    "name": case["name"],
                    "status_code": case.get("status"),
                    "source": case["source"],
                    "effects": {
                        "retry": effects.retry,
                        "retry_scope": effects.retry_scope,
                        "retry_action": effects.retry_action,
                        "client_outcome": effects.client_outcome,
                        "account_effect": effects.account_effect,
                        "model_effect": effects.model_effect,
                        "circuit_penalty": effects.circuit_penalty,
                        "persist_backoff": effects.persist_backoff,
                        "backoff_reason": effects.backoff_reason,
                        "backoff_until": effects.backoff_until,
                        "release_probe_only": effects.release_probe_only,
                        "evidence_class": effects.evidence_class,
                        "wire_effect": effects.wire_effect,
                    },
                }
            )
    return result


def _retry_after_observations() -> dict[str, float | None]:
    classifier = RetryClassifier()
    with patch("eggpool.retry.classification.time.time", return_value=1_700_000_000.0):
        return {
            "numeric_seconds": classifier.parse_retry_after(
                {"Retry-After": "12"}, default=None
            ),
            "http_date": classifier.parse_retry_after(
                {"Retry-After": "Sun, 14 Nov 2027 22:13:32 GMT"}, default=None
            ),
            "invalid": classifier.parse_retry_after(
                {"Retry-After": "not-a-delay"}, default=None
            ),
            "missing": classifier.parse_retry_after({}, default=None),
        }


def _stream_observation(case: dict[str, Any]) -> dict[str, Any]:
    observer = IncrementalSSEObserver(
        case["protocol"],
        request_surface=case["surface"],
        wire_surface=case.get(
            "wire_surface",
            "openai_responses" if case["surface"] == "responses" else None,
        ),
    )
    observer.observe(case["raw"])
    observer.finish()
    snapshot = observer.completion_snapshot
    decision = classify_stream_eof(
        protocol=case["protocol"],
        policy=case["policy"],
        snapshot=snapshot,
        downstream_started=False,
    )
    return {
        "name": case["name"],
        "snapshot": {
            "saw_payload": snapshot.saw_payload,
            "saw_terminal_event": snapshot.saw_terminal_event,
            "terminal_kind": snapshot.terminal_kind,
            "saw_usage_completion": snapshot.saw_usage_completion,
            "incomplete_frame_at_eof": snapshot.incomplete_frame_at_eof,
            "parser_error_count": snapshot.parser_error_count,
            "bytes_observed": snapshot.bytes_observed,
            "observer_error_count": observer.error_count,
            "frame_count": observer.frame_count,
        },
        "eof_classification": decision.classification,
        "downstream_started": decision.downstream_started,
    }


def _synthetic_snapshot(
    *,
    saw_payload: bool,
    saw_terminal_event: bool,
    terminal_kind: str | None,
    saw_usage_completion: bool = False,
    incomplete_frame_at_eof: bool = False,
    parser_error_count: int = 0,
) -> StreamCompletionSnapshot:
    return StreamCompletionSnapshot(
        saw_payload=saw_payload,
        saw_terminal_event=saw_terminal_event,
        terminal_kind=terminal_kind,
        saw_usage_completion=saw_usage_completion,
        incomplete_frame_at_eof=incomplete_frame_at_eof,
        parser_error_count=parser_error_count,
        bytes_observed=8,
    )


async def _wire_observation_async() -> dict[str, Any]:
    config = AppConfig.from_dict(
        {
            "providers": {
                "provider-fixture": {
                    "id": "provider-fixture",
                    "base_url": "https://provider.invalid/v1",
                    "protocols": ["openai", "anthropic"],
                    "auth": {"mode": "none"},
                    "wire_surfaces": {
                        "openai_chat_completions": {
                            "path_template": "/chat/completions"
                        },
                        "anthropic_messages": {"path_template": "/messages"},
                    },
                    "model_wire": {
                        "model-fixture": {
                            "preferred_surface": "openai_chat_completions",
                            "fixed": False,
                        }
                    },
                }
            }
        }
    )
    provider = config.providers["provider-fixture"]
    profiles = resolve_provider_wire_profiles(provider)
    resolver = WireProfileResolver(cache_max_entries=4)
    resolver.configure(
        cache_max_entries=4,
        max_concurrent_per_provider=1,
        min_negotiation_interval_s=1.0,
        rejection_cooldown_s=300.0,
        learned_preference_ttl_s=100.0,
    )
    initial = resolver.resolve(
        provider,
        "model-fixture",
        profiles=profiles,
        now_monotonic=100.0,
        now_epoch=1_700_000_000.0,
    )
    resolver.record_success(
        "provider-fixture",
        "model-fixture",
        initial.candidate_fingerprint,
        "anthropic_messages",
        now_monotonic=101.0,
        now_epoch=1_700_000_001.0,
    )
    learned = resolver.resolve(
        provider,
        "model-fixture",
        profiles=profiles,
        now_monotonic=102.0,
        now_epoch=1_700_000_002.0,
    )
    resolver.record_deterministic_rejection(
        "provider-fixture",
        "model-fixture",
        initial.candidate_fingerprint,
        "anthropic_messages",
        rejection_class="wire_surface_unsupported",
        cooldown_s=300.0,
        now_monotonic=103.0,
    )
    suppressed = resolver.is_suppressed(
        "provider-fixture",
        "model-fixture",
        initial.candidate_fingerprint,
        "anthropic_messages",
        now_monotonic=104.0,
    )
    leader = await resolver.begin_negotiation(initial, now_monotonic=105.0)
    follower = await resolver.begin_negotiation(initial, now_monotonic=105.0)
    await leader.__aenter__()
    accepted = await leader.accept("openai_chat_completions")
    shared = await follower.wait_for_acceptance()
    return {
        "candidate_fingerprint_length": len(initial.candidate_fingerprint),
        "initial_source": initial.selected_source,
        "initial_candidates": [profile.surface for profile in initial.candidates],
        "learned_source": learned.selected_source,
        "learned_candidates": [profile.surface for profile in learned.candidates],
        "rejection_suppressed": suppressed,
        "leader_role": leader.role,
        "follower_role": follower.role,
        "leader_result": accepted.result,
        "follower_result": shared.result,
        "snapshot": resolver.snapshot(),
    }


def _wire_observation() -> dict[str, Any]:
    return asyncio.run(_wire_observation_async())


def _ownership_observation() -> dict[str, Any]:
    identity = FinalizationIdentity(
        proxy_request_id="proxy-fixture",
        db_request_id="101",
        attempt_id=202,
        reservation_id="303",
        account_id=404,
        account_name="account-fixture",
        provider_id="provider-fixture",
        model_id="model-fixture",
        client_protocol="openai",
        upstream_protocol="anthropic",
        attempt_number=1,
    )
    receipt = RuntimePublicationReceipt(
        pending_request_added=True,
        pending_tokens_added=True,
        pending_load_converted=True,
        active_count_added=True,
        quota_reservation_added=True,
        health_probe_acquired=True,
    )
    handoff = ResponseHandoffState()
    before = handoff.started
    handoff.mark_started()
    handoff.mark_started()
    return {
        "identity": {
            "proxy_request_id": identity.proxy_request_id,
            "db_request_id": identity.db_request_id,
            "attempt_id": identity.attempt_id,
            "reservation_id": identity.reservation_id,
            "account_id": identity.account_id,
            "account_name": identity.account_name,
            "provider_id": identity.provider_id,
            "model_id": identity.model_id,
            "client_protocol": identity.client_protocol,
            "upstream_protocol": identity.upstream_protocol,
            "attempt_number": identity.attempt_number,
        },
        "publication_receipt": {
            "pending_request_added": receipt.pending_request_added,
            "pending_tokens_added": receipt.pending_tokens_added,
            "pending_load_converted": receipt.pending_load_converted,
            "pending_load_released": receipt.pending_load_released,
            "active_count_added": receipt.active_count_added,
            "quota_reservation_added": receipt.quota_reservation_added,
            "health_probe_acquired": receipt.health_probe_acquired,
        },
        "cleanup_progress_fields": list(FailedAttemptCleanupProgress().__slots__),
        "compensation_progress_fields": list(ClaimCompensationProgress().__slots__),
        "finalization_progress": [item.value for item in FinalizationProgress],
        "handoff": {"before": before, "after_repeated_mark": handoff.started},
    }


def build_observation_bundle() -> dict[str, Any]:
    """Build the complete bounded C001 semantic observation."""
    matrix = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
    return {
        "schema_version": "m7-coordinator-c001-observations/v1",
        "repository_baseline": "04820555479dc3ab86622d9c658c44c45c2c07e7",
        "oracle_modules": [
            "request.coordinator",
            "request.claim_lifecycle",
            "request.attempt_finalizer",
            "request.finalization_job",
            "request.finalizer",
            "request.provider_bound_request",
            "request.response_handoff",
            "request.stream_completion",
            "request.stream_diagnostics",
            "retry.classification",
            "failure.classifier",
            "failure.effects",
            "wire.resolver",
            "db.repositories",
        ],
        "lifecycle_states": list(LIFECYCLE_STATES),
        "durable_rows": {name: list(columns) for name, columns in DURABLE_ROWS.items()},
        "ownership_components": list(OWNERSHIP_COMPONENTS),
        "failure_cases": _failure_observations(),
        "retry_after": _retry_after_observations(),
        "wire": _wire_observation(),
        "streams": [_stream_observation(case) for case in STREAM_CASES],
        "synthetic_stream_decisions": {
            "gemini_incomplete": classify_stream_eof(
                protocol="openai",
                policy="strict",
                snapshot=_synthetic_snapshot(
                    saw_payload=True,
                    saw_terminal_event=True,
                    terminal_kind="gemini_incomplete",
                ),
                downstream_started=False,
            ).classification,
            "midstream_exception": "upstream_midstream_error",
        },
        "ownership": _ownership_observation(),
        "m7_boundary": matrix["m7_boundary"],
    }


def committed_observation(bundle: dict[str, Any]) -> dict[str, Any]:
    """Return the reviewed scalar projection committed for Rust consumers."""
    return {
        "schema_version": bundle["schema_version"],
        "lifecycle_states": bundle["lifecycle_states"],
        "durable_row_widths": {
            name: len(columns) for name, columns in bundle["durable_rows"].items()
        },
        "ownership_components": bundle["ownership_components"],
        "failure_cases": {
            case["name"]: {
                "status_code": case["status_code"],
                "retry": case["effects"]["retry"],
                "retry_action": case["effects"]["retry_action"],
                "retry_scope": case["effects"]["retry_scope"],
                "account_effect": case["effects"]["account_effect"],
                "model_effect": case["effects"]["model_effect"],
                "wire_effect": case["effects"]["wire_effect"],
                "evidence_class": case["effects"]["evidence_class"],
            }
            for case in bundle["failure_cases"]
        },
        "retry_after": bundle["retry_after"],
        "wire": {
            key: bundle["wire"][key]
            for key in (
                "candidate_fingerprint_length",
                "initial_source",
                "initial_candidates",
                "learned_source",
                "learned_candidates",
                "rejection_suppressed",
                "leader_role",
                "follower_role",
                "leader_result",
                "follower_result",
            )
        },
        "streams": {
            case["name"]: {
                "eof_classification": case["eof_classification"],
                "terminal_kind": case["snapshot"]["terminal_kind"],
                "saw_terminal_event": case["snapshot"]["saw_terminal_event"],
                "saw_usage_completion": case["snapshot"]["saw_usage_completion"],
                "observer_error_count": case["snapshot"]["observer_error_count"],
            }
            for case in bundle["streams"]
        },
        "synthetic_stream_decisions": bundle["synthetic_stream_decisions"],
        "ownership": {
            "cleanup_progress_fields": bundle["ownership"]["cleanup_progress_fields"],
            "compensation_progress_fields": bundle["ownership"][
                "compensation_progress_fields"
            ],
            "finalization_progress": bundle["ownership"]["finalization_progress"],
            "handoff": bundle["ownership"]["handoff"],
        },
        "m7_boundary": bundle["m7_boundary"],
    }


def observation_json() -> str:
    """Return the canonical compact JSON representation."""
    return json.dumps(build_observation_bundle(), sort_keys=True, separators=(",", ":"))


__all__ = [
    "LIFECYCLE_STATES",
    "MATRIX_PATH",
    "OBSERVATION_PATH",
    "build_observation_bundle",
    "committed_observation",
    "observation_json",
]
