//! Wire terminal-summary classification and retained finalization facts.

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum StreamEofClass {
    Complete,
    Compatibility,
    TerminalFailure,
    TerminalIncomplete,
    EmptyEof,
    PrematureEof,
    MalformedEof,
}

pub(crate) fn classify_eof(
    summary: &crate::wire::StreamTerminalSummary,
    compat_allowed: bool,
) -> StreamEofClass {
    match summary.outcome {
        StreamTerminalOutcome::Success => StreamEofClass::Complete,
        StreamTerminalOutcome::Incomplete => match summary.evidence {
            Some(TerminalEvidence::ResponsesFailed) | Some(TerminalEvidence::ProviderError) => {
                StreamEofClass::TerminalFailure
            }
            _ => StreamEofClass::TerminalIncomplete,
        },
        StreamTerminalOutcome::ProviderError => StreamEofClass::TerminalFailure,
        StreamTerminalOutcome::Malformed => StreamEofClass::MalformedEof,
        StreamTerminalOutcome::EofBeforeBody => StreamEofClass::EmptyEof,
        StreamTerminalOutcome::EofAfterPartialBody => {
            if compat_allowed && summary.saw_usage_completion {
                StreamEofClass::Compatibility
            } else {
                StreamEofClass::PrematureEof
            }
        }
    }
}

pub(crate) fn completion_compat_allowed(policy: &str) -> bool {
    matches!(policy, "compatible" | "permissive_observe")
}

pub(crate) fn is_event_stream(headers: &HeaderMap) -> bool {
    headers
        .get(http::header::CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .is_some_and(|value| value.to_ascii_lowercase().contains("text/event-stream"))
}

pub(crate) fn protocol_for_surface(surface: WireSurface) -> &'static str {
    match surface {
        WireSurface::AnthropicMessages => "anthropic",
        WireSurface::GeminiInteractions | WireSurface::GeminiGenerateContent => "gemini",
        WireSurface::OpenaiChatCompletions | WireSurface::OpenaiResponses => "openai",
    }
}

pub(crate) fn provider_error_signal(error: &ProviderErrorEvidence) -> Option<&'static str> {
    let text = format!(
        "{} {}",
        error.error_type.as_deref().unwrap_or_default(),
        error.message.as_deref().unwrap_or_default()
    )
    .to_ascii_lowercase();
    if text.contains("invalid") && (text.contains("key") || text.contains("credential")) {
        Some("credential_invalid")
    } else if text.contains("wire") && text.contains("auth") {
        Some("wire_auth_mismatch")
    } else if text.contains("unsupported") && text.contains("surface") {
        Some("wire_surface_unsupported")
    } else if text.contains("schema") {
        Some("wire_schema_mismatch")
    } else if text.contains("model") && (text.contains("not found") || text.contains("absent")) {
        Some("model_absent")
    } else if text.contains("rate") || text.contains("too many") {
        Some("rate_limited")
    } else if text.contains("quota") {
        Some("quota_exhausted")
    } else {
        None
    }
}

pub(crate) fn bounded_request_id(value: Option<String>) -> Option<String> {
    value.map(|value| {
        let mut bounded: String = value.chars().take(128).collect();
        bounded.retain(|character| !character.is_control());
        bounded
    })
}

pub(crate) fn bounded_i64(value: Option<u64>) -> i64 {
    value
        .unwrap_or(0)
        .min(i64::MAX as u64)
        .try_into()
        .unwrap_or(i64::MAX)
}

pub(crate) fn bounded_usize(value: usize) -> i64 {
    value.min(i64::MAX as usize) as i64
}

pub(crate) fn duration_i64(value: Duration) -> i64 {
    value.as_millis().min(i64::MAX as u128) as i64
}

pub(crate) fn cache_status(status: CacheCounterStatus) -> &'static str {
    match status {
        CacheCounterStatus::Reported => "reported",
        CacheCounterStatus::NotReported => "not_reported",
        CacheCounterStatus::UnknownFormat => "unknown_format",
    }
}

pub(crate) fn category_label(category: FailureCategory) -> &'static str {
    match category {
        FailureCategory::BadRequest => "bad_request",
        FailureCategory::Authentication => "authentication",
        FailureCategory::Quota => "quota",
        FailureCategory::RateLimit => "rate_limit",
        FailureCategory::Temporary => "temporary",
        FailureCategory::TransientTransport => "transient_transport",
        FailureCategory::ModelUnavailable => "model_unavailable",
        FailureCategory::WireRejected => "wire_rejected",
        FailureCategory::Cancelled => "cancelled",
        FailureCategory::Fatal => "fatal",
    }
}
use std::{
    sync::Mutex,
    time::{Duration, Instant},
};

use bytes::Bytes;
use http::{HeaderMap, StatusCode};

use crate::coordinator::{
    FailureCategory, FailureDecisionEngine, FailureEffects, FailureObservation, FailureSource,
    FinalizationData, FinalizationOutcome, StreamingCoordinatorError, WireResolver,
};
use crate::routing::RoutingRouter;
use crate::wire::ir::{CacheCounterStatus, CanonicalUsage, ProviderErrorEvidence};
use crate::wire::{StreamForwardingMode, StreamTerminalOutcome, TerminalEvidence, WireSurface};

use super::{
    AttemptStreamFacts, ForwardedTerminal, OUTCOME_COMPLETED_CANONICAL,
    OUTCOME_COMPLETED_COMPATIBILITY, OUTCOME_EMPTY_EOF, OUTCOME_IDLE_TIMEOUT,
    OUTCOME_MALFORMED_EOF, OUTCOME_PREMATURE_EOF_BEFORE_BODY, OUTCOME_PREMATURE_EOF_MIDSTREAM,
    OUTCOME_TERMINAL_FAILURE, OUTCOME_TERMINAL_INCOMPLETE, OUTCOME_UPSTREAM_MIDSTREAM_ERROR,
    PendingStreamFinalizationParts, StreamChunkError, StreamPhase,
};

pub(crate) fn store_idle_timeout(
    parts: &mut PendingStreamFinalizationParts,
    facts: &AttemptStreamFacts,
    router: &RoutingRouter,
    engine: &Mutex<FailureDecisionEngine>,
) -> Option<Result<Bytes, StreamChunkError>> {
    let observation = transport_observation(parts, facts, "stream_idle", "stream_idle");
    let (effects, first) = match decide(engine, &observation) {
        Ok(value) => value,
        Err(_) => {
            return store_local_midstream(
                parts,
                facts,
                "StreamIdleTimeout",
                StreamChunkError::UpstreamTransport,
            );
        }
    };
    if first {
        apply_effects(router, parts, &effects);
    }
    // One terminal event, one outcome counter: an idle timeout is not also an
    // upstream midstream transport error.
    parts.diagnostics_record(
        OUTCOME_IDLE_TIMEOUT,
        facts.attempt_number,
        parts.stream_bytes(),
        parts.elapsed(),
    );
    let mut data = effects_midstream_data(parts, facts, &effects, "StreamIdleTimeout");
    data.error_detail = Some(OUTCOME_IDLE_TIMEOUT.to_owned());
    store_terminal(parts, data, StreamChunkError::IdleTimeout)
}

pub(crate) fn store_midstream_transport(
    parts: &mut PendingStreamFinalizationParts,
    facts: &AttemptStreamFacts,
    router: &RoutingRouter,
    engine: &Mutex<FailureDecisionEngine>,
    diagnostic_class: &'static str,
) -> Option<Result<Bytes, StreamChunkError>> {
    let observation = transport_observation(parts, facts, "stream_body", diagnostic_class);
    let (effects, first) = match decide(engine, &observation) {
        Ok(value) => value,
        Err(_) => {
            return store_local_midstream(
                parts,
                facts,
                "UpstreamTransport",
                StreamChunkError::UpstreamTransport,
            );
        }
    };
    if first {
        apply_effects(router, parts, &effects);
    }
    parts.diagnostics_record(
        OUTCOME_UPSTREAM_MIDSTREAM_ERROR,
        facts.attempt_number,
        parts.stream_bytes(),
        parts.elapsed(),
    );
    let data = effects_midstream_data(parts, facts, &effects, "UpstreamTransport");
    store_terminal(parts, data, StreamChunkError::UpstreamTransport)
}

pub(crate) fn store_translation_error(
    parts: &mut PendingStreamFinalizationParts,
    facts: &AttemptStreamFacts,
    router: &RoutingRouter,
    engine: &Mutex<FailureDecisionEngine>,
) -> Option<Result<Bytes, StreamChunkError>> {
    let identity = parts.identity.clone();
    let observation = {
        let mut observation = FailureObservation::response(
            identity.attempt_id,
            facts.attempt_number,
            StatusCode::INTERNAL_SERVER_ERROR,
        );
        observation.source = FailureSource::LocalPreparation;
        observation.status = None;
        observation.response_started = true;
        observation.downstream_started = true;
        observation.provider_id = Some(identity.provider_id.clone());
        observation.account_name = Some(identity.account_name.clone());
        observation.model_id = Some(identity.model_id.clone());
        observation.upstream_model_id = Some(identity.upstream_model_id.clone());
        observation.client_protocol = identity.client_protocol.clone();
        observation.upstream_protocol = identity.upstream_protocol.clone();
        observation.wire_surface = Some(facts.wire_surface.as_str().into());
        observation.transport_phase = Some("stream_translation".into());
        observation.error_class = Some("stream_translation".into());
        observation.credential_configured = true;
        observation.dispatch_phase = "stream_translation".into();
        observation
    };
    let (effects, first) = match decide(engine, &observation) {
        Ok(value) => value,
        Err(_) => {
            return store_local_midstream(
                parts,
                facts,
                "StreamTranslation",
                StreamChunkError::Translation,
            );
        }
    };
    if first {
        apply_effects(router, parts, &effects);
    }
    parts.diagnostics_record(
        OUTCOME_UPSTREAM_MIDSTREAM_ERROR,
        facts.attempt_number,
        parts.stream_bytes(),
        parts.elapsed(),
    );
    let data = effects_midstream_data(parts, facts, &effects, "StreamTranslation");
    store_terminal(parts, data, StreamChunkError::Translation)
}

fn store_local_midstream(
    parts: &mut PendingStreamFinalizationParts,
    facts: &AttemptStreamFacts,
    error_class: &str,
    // The reported error must match the fault: a translation failure reported
    // as an upstream transport error blames the provider for a local bug.
    error: StreamChunkError,
) -> Option<Result<Bytes, StreamChunkError>> {
    let data = local_midstream_data(parts, facts, error_class);
    parts.diagnostics_record(
        OUTCOME_UPSTREAM_MIDSTREAM_ERROR,
        facts.attempt_number,
        parts.stream_bytes(),
        parts.elapsed(),
    );
    store_terminal(parts, data, error)
}

/// End the stream on a provider terminal event that has already been forwarded
/// downstream.
///
/// The client holds the provider's authoritative end of the response, so the
/// stream closes here instead of waiting for transport EOF. A provider that
/// holds the socket open after its terminal must never turn a complete
/// delivery into a midstream failure or a health penalty.
pub(crate) fn store_forwarded_terminal(
    parts: &mut PendingStreamFinalizationParts,
    facts: &AttemptStreamFacts,
    router: &RoutingRouter,
    wire_resolver: &WireResolver,
    terminal: ForwardedTerminal,
) -> Option<Result<Bytes, StreamChunkError>> {
    let usage = parts
        .stream
        .as_mut()
        .and_then(|stream| stream.wire.as_mut())
        .and_then(|wire| wire.usage());
    // No further canonical event can arrive on this path, so a client-stream
    // terminator the encoder still holds back belongs to this ending rather
    // than to transport EOF, which this path never reaches.
    let terminator = flush_client_terminator(parts);
    match terminal {
        ForwardedTerminal::Completed => {
            wire_resolver.accept(
                &parts.identity.provider_id,
                &parts.identity.model_id,
                &facts.candidate_fingerprint,
                facts.wire_surface,
                Instant::now(),
            );
            if let Some(claim) = parts.claim.as_ref() {
                router.record_success(claim);
                if let Some(usage) = usage.as_ref() {
                    router.record_usage(claim, usage);
                }
            }
            parts.diagnostics_record(
                OUTCOME_COMPLETED_CANONICAL,
                facts.attempt_number,
                parts.stream_bytes(),
                parts.elapsed(),
            );
            let data = success_terminal_data(parts, facts, usage);
            parts.phase = StreamPhase::Closed;
            parts.release_transport();
            parts.terminal_stored = true;
            parts.data = data;
            parts.exhausted = true;
            terminator.map(Ok)
        }
        ForwardedTerminal::Incomplete | ForwardedTerminal::Failed => {
            let failed = matches!(terminal, ForwardedTerminal::Failed);
            parts.diagnostics_record(
                if failed {
                    OUTCOME_TERMINAL_FAILURE
                } else {
                    OUTCOME_TERMINAL_INCOMPLETE
                },
                facts.attempt_number,
                parts.stream_bytes(),
                parts.elapsed(),
            );
            let data = responses_terminal_data(parts, facts, usage, failed);
            parts.phase = StreamPhase::Closed;
            parts.release_transport();
            parts.terminal_stored = true;
            parts.data = data;
            parts.exhausted = true;
            // The provider terminal event was already forwarded; the stream
            // ends cleanly from the caller's view.
            terminator.map(Ok)
        }
    }
}

/// Client bytes a translated client-stream encoder is still holding back.
///
/// The Messages grammar requires `message_delta` to precede `message_stop`, so
/// the encoder records the stop reason and emits that pair only once no further
/// canonical event can arrive. Both stream endings have to release it: a
/// provider terminal already forwarded downstream, and transport EOF.
fn flush_client_terminator(
    parts: &mut PendingStreamFinalizationParts,
) -> Option<axum::body::Bytes> {
    parts
        .stream
        .as_mut()
        .and_then(|stream| stream.wire.as_mut())
        .and_then(|wire| wire.flush_client_stream().ok())
        .filter(|bytes| !bytes.is_empty())
}

pub(crate) fn store_eof(
    parts: &mut PendingStreamFinalizationParts,
    facts: &AttemptStreamFacts,
    router: &RoutingRouter,
    wire_resolver: &WireResolver,
) -> Option<Result<Bytes, StreamChunkError>> {
    // Drain the M6 framing tail first; events in the final partial record
    // (e.g. a trailing usage frame) still encode in order. Transport EOF
    // alone never decides success: classification below is authoritative.
    let mut tail_bytes = Vec::new();
    let mut tail_usage: Option<CanonicalUsage> = None;
    let summary = if let Some(stream) = parts.stream.as_mut()
        && let Some(wire) = stream.wire.as_mut()
    {
        if wire.forwarding_mode() == StreamForwardingMode::NativeObserved {
            match wire.finalize_native() {
                Ok(summary) => {
                    tail_usage = summary.usage.clone();
                    Some(summary)
                }
                Err(_) => None,
            }
        } else {
            match wire.finalize() {
                Ok(finalization) => {
                    tail_usage = finalization.usage.clone();
                    for event in &finalization.events {
                        if let Ok(bytes) = wire.encode_client_event_stateful(event)
                            && !bytes.is_empty()
                        {
                            stream.events_forwarded = stream.events_forwarded.saturating_add(1);
                            tail_bytes.extend_from_slice(&bytes);
                        }
                    }
                    // No further canonical event can arrive, so a client-stream
                    // terminator the encoder is still holding belongs here.
                    if let Ok(bytes) = wire.flush_client_stream()
                        && !bytes.is_empty()
                    {
                        tail_bytes.extend_from_slice(&bytes);
                    }
                    if !tail_bytes.is_empty() {
                        stream.client_bytes = stream.client_bytes.saturating_add(tail_bytes.len());
                    }
                    Some(finalization.terminal.clone())
                }
                Err(_) => None,
            }
        }
    } else {
        None
    };
    let is_passthrough = parts
        .stream
        .as_ref()
        .is_some_and(|stream| stream.wire.is_none());
    let compat = parts
        .stream
        .as_ref()
        .map(|stream| completion_compat_allowed(&stream.completion_policy))
        .unwrap_or(false);
    // Legacy non-SSE pass-through ends complete at EOF (Python parity);
    // SSE completion rules apply to event-stream responses only. An empty
    // passthrough body is not success: require observed bytes first.
    let classification = if is_passthrough {
        if parts.stream_bytes() > 0 {
            StreamEofClass::Complete
        } else {
            StreamEofClass::EmptyEof
        }
    } else {
        match summary.as_ref() {
            None => StreamEofClass::MalformedEof,
            Some(summary) => classify_eof(summary, compat),
        }
    };
    // Malformed provider chunks skipped mid-stream force malformed even
    // when the M6 tail otherwise looks clean.
    let classification = match (&classification, parts.malformed_count()) {
        (StreamEofClass::Complete, count) if count > 0 => StreamEofClass::MalformedEof,
        (StreamEofClass::Compatibility, count) if count > 0 => StreamEofClass::MalformedEof,
        (other, _) => other.clone(),
    };
    let downstream_started = parts.handoff.started();
    match classification {
        StreamEofClass::Complete | StreamEofClass::Compatibility => {
            let compat = matches!(classification, StreamEofClass::Compatibility);
            wire_resolver.accept(
                &parts.identity.provider_id,
                &parts.identity.model_id,
                &facts.candidate_fingerprint,
                facts.wire_surface,
                Instant::now(),
            );
            if let Some(claim) = parts.claim.as_ref() {
                router.record_success(claim);
                if let Some(usage) = tail_usage.as_ref() {
                    router.record_usage(claim, usage);
                }
            }
            let outcome = if compat {
                OUTCOME_COMPLETED_COMPATIBILITY
            } else {
                OUTCOME_COMPLETED_CANONICAL
            };
            parts.diagnostics_record(
                outcome,
                facts.attempt_number,
                parts.stream_bytes(),
                parts.elapsed(),
            );
            let data = success_terminal_data(parts, facts, tail_usage);
            parts.phase = StreamPhase::Closed;
            parts.release_transport();
            parts.terminal_stored = true;
            parts.data = data;
            if !tail_bytes.is_empty() {
                parts.exhausted = true;
                return Some(Ok(Bytes::from(tail_bytes)));
            }
            parts.exhausted = true;
            None
        }
        StreamEofClass::TerminalFailure | StreamEofClass::TerminalIncomplete => {
            let failed = matches!(classification, StreamEofClass::TerminalFailure);
            parts.diagnostics_record(
                if failed {
                    OUTCOME_TERMINAL_FAILURE
                } else {
                    OUTCOME_TERMINAL_INCOMPLETE
                },
                facts.attempt_number,
                parts.stream_bytes(),
                parts.elapsed(),
            );
            let data = responses_terminal_data(parts, facts, tail_usage, failed);
            parts.phase = StreamPhase::Closed;
            parts.release_transport();
            parts.terminal_stored = true;
            parts.data = data;
            if !tail_bytes.is_empty() {
                parts.exhausted = true;
                return Some(Ok(Bytes::from(tail_bytes)));
            }
            parts.exhausted = true;
            // The provider terminal event was already forwarded; the
            // stream ends cleanly from the caller's view.
            None
        }
        StreamEofClass::EmptyEof | StreamEofClass::PrematureEof | StreamEofClass::MalformedEof => {
            let (outcome, error) = match classification {
                StreamEofClass::EmptyEof => (OUTCOME_EMPTY_EOF, StreamChunkError::EmptyEof),
                StreamEofClass::MalformedEof => {
                    (OUTCOME_MALFORMED_EOF, StreamChunkError::MalformedEof)
                }
                _ => {
                    if downstream_started {
                        (
                            OUTCOME_PREMATURE_EOF_MIDSTREAM,
                            StreamChunkError::PrematureEof,
                        )
                    } else {
                        (
                            OUTCOME_PREMATURE_EOF_BEFORE_BODY,
                            StreamChunkError::PrematureEof,
                        )
                    }
                }
            };
            parts.diagnostics_record(
                outcome,
                facts.attempt_number,
                parts.stream_bytes(),
                parts.elapsed(),
            );
            let data = eof_failure_data(parts, facts, tail_usage, outcome, downstream_started);
            store_terminal(parts, data, error)
        }
    }
}

fn store_terminal(
    parts: &mut PendingStreamFinalizationParts,
    data: FinalizationData,
    error: StreamChunkError,
) -> Option<Result<Bytes, StreamChunkError>> {
    parts.phase = StreamPhase::Closed;
    parts.release_transport();
    parts.terminal_stored = true;
    parts.data = data;
    parts.exhausted = true;
    Some(Err(error))
}

fn transport_observation(
    parts: &PendingStreamFinalizationParts,
    facts: &AttemptStreamFacts,
    dispatch_phase: &str,
    diagnostic_class: &str,
) -> FailureObservation {
    let mut observation = FailureObservation::response(
        parts.identity.attempt_id,
        facts.attempt_number,
        StatusCode::INTERNAL_SERVER_ERROR,
    );
    observation.source = FailureSource::Transport;
    observation.status = None;
    observation.response_started = true;
    observation.downstream_started = true;
    observation.provider_id = Some(parts.identity.provider_id.clone());
    observation.account_name = Some(parts.identity.account_name.clone());
    observation.model_id = Some(parts.identity.model_id.clone());
    observation.upstream_model_id = Some(parts.identity.upstream_model_id.clone());
    observation.client_protocol = parts.identity.client_protocol.clone();
    observation.upstream_protocol = parts.identity.upstream_protocol.clone();
    observation.wire_surface = Some(facts.wire_surface.as_str().into());
    observation.transport_phase = Some(dispatch_phase.into());
    observation.error_class = Some(diagnostic_class.into());
    observation.credential_configured = true;
    observation.dispatch_phase = dispatch_phase.into();
    observation
}

fn decide(
    engine: &Mutex<FailureDecisionEngine>,
    observation: &FailureObservation,
) -> Result<(FailureEffects, bool), StreamingCoordinatorError> {
    engine
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .decide(observation)
        .map_err(|error| StreamingCoordinatorError::Effects(error.to_string()))
}

fn apply_effects(
    router: &RoutingRouter,
    parts: &PendingStreamFinalizationParts,
    effects: &FailureEffects,
) {
    if let Some(claim) = parts.claim.as_ref() {
        router.apply_failure_effects(
            claim,
            effects.apply_account_penalty,
            effects.quarantine_model,
            &effects.model_effect,
            effects.backoff_reason.as_deref(),
            effects.backoff_until,
            effects.circuit_penalty,
        );
    }
}

fn effects_midstream_data(
    parts: &PendingStreamFinalizationParts,
    facts: &AttemptStreamFacts,
    effects: &FailureEffects,
    error_class: &str,
) -> FinalizationData {
    let usage = parts.midstream_usage();
    FinalizationData {
        outcome: FinalizationOutcome::MidstreamError,
        // Midstream failures must never masquerade as 200 OK in durable
        // success-rate queries; the synthetic status stays absent.
        status_code: None,
        input_tokens: bounded_i64(usage.as_ref().and_then(|usage| usage.input_tokens)),
        output_tokens: bounded_i64(usage.as_ref().and_then(|usage| usage.output_tokens)),
        cache_read_tokens: bounded_i64(
            usage
                .as_ref()
                .and_then(|usage| usage.cache_read_input_tokens),
        ),
        cache_write_tokens: bounded_i64(usage.as_ref().and_then(|usage| {
            usage
                .cache_write_input_tokens
                .or(usage.cache_creation_input_tokens)
        })),
        reasoning_tokens: bounded_i64(usage.as_ref().and_then(|usage| usage.reasoning_tokens)),
        cost_microdollars: 0,
        cache_counter_status: Some(
            usage
                .as_ref()
                .map(|usage| cache_status(usage.cache_counter_status).to_owned())
                .unwrap_or_else(|| "not_reported".to_owned()),
        ),
        latency_ms: duration_i64(parts.elapsed()),
        first_byte_ms: parts.first_byte_ms(),
        bytes_received: bounded_usize(facts.request_bytes),
        bytes_emitted: bounded_usize(parts.client_bytes()),
        upstream_request_id: bounded_request_id(parts.upstream_request_id()),
        error_class: Some(error_class.to_owned()),
        error_detail: Some(effects.client_outcome.clone()),
        release_reason: Some("attempt_failed".into()),
        retry_category: Some(category_label(effects.category).into()),
        is_retry_outcome: false,
        downstream_started: parts.handoff.started(),
        transcoded: facts.transcoded,
        upstream_protocol: Some(facts.upstream_protocol.clone()),
        ..FinalizationData::default()
    }
}

fn local_midstream_data(
    parts: &PendingStreamFinalizationParts,
    facts: &AttemptStreamFacts,
    error_class: &str,
) -> FinalizationData {
    FinalizationData {
        outcome: FinalizationOutcome::MidstreamError,
        // See effects_midstream_data: never record 200 for a failure.
        status_code: None,
        cost_microdollars: 0,
        cache_counter_status: Some("not_reported".to_owned()),
        latency_ms: duration_i64(parts.elapsed()),
        first_byte_ms: parts.first_byte_ms(),
        bytes_received: bounded_usize(facts.request_bytes),
        bytes_emitted: bounded_usize(parts.client_bytes()),
        upstream_request_id: bounded_request_id(parts.upstream_request_id()),
        error_class: Some(error_class.to_owned()),
        error_detail: Some("midstream failure without retry".to_owned()),
        release_reason: Some("attempt_failed".into()),
        retry_category: Some("never".into()),
        downstream_started: parts.handoff.started(),
        transcoded: facts.transcoded,
        upstream_protocol: Some(facts.upstream_protocol.clone()),
        ..FinalizationData::default()
    }
}

fn success_terminal_data(
    parts: &PendingStreamFinalizationParts,
    facts: &AttemptStreamFacts,
    usage: Option<CanonicalUsage>,
) -> FinalizationData {
    let usage = usage.unwrap_or_default();
    FinalizationData {
        outcome: FinalizationOutcome::Completed,
        status_code: Some(StatusCode::OK.as_u16()),
        input_tokens: bounded_i64(usage.input_tokens),
        output_tokens: bounded_i64(usage.output_tokens),
        cache_read_tokens: bounded_i64(usage.cache_read_input_tokens),
        cache_write_tokens: bounded_i64(
            usage
                .cache_write_input_tokens
                .or(usage.cache_creation_input_tokens),
        ),
        reasoning_tokens: bounded_i64(usage.reasoning_tokens),
        cost_microdollars: 0,
        cache_counter_status: Some(cache_status(usage.cache_counter_status).into()),
        cached_input_tokens: usage
            .cached_input_tokens
            .map(|value| bounded_i64(Some(value))),
        cache_read_input_tokens: usage
            .cache_read_input_tokens
            .map(|value| bounded_i64(Some(value))),
        cache_creation_input_tokens: usage
            .cache_creation_input_tokens
            .map(|value| bounded_i64(Some(value))),
        cache_write_input_tokens: usage
            .cache_write_input_tokens
            .or(usage.cache_creation_input_tokens)
            .map(|value| bounded_i64(Some(value))),
        input_tokens_reported: usage.input_tokens.map(|value| bounded_i64(Some(value))),
        output_tokens_reported: usage.output_tokens.map(|value| bounded_i64(Some(value))),
        total_tokens_reported: usage.total_tokens.map(|value| bounded_i64(Some(value))),
        transcoded: facts.transcoded,
        upstream_protocol: Some(facts.upstream_protocol.clone()),
        latency_ms: duration_i64(parts.elapsed()),
        first_byte_ms: parts.first_byte_ms(),
        bytes_received: bounded_usize(facts.request_bytes),
        bytes_emitted: bounded_usize(parts.client_bytes()),
        upstream_request_id: bounded_request_id(parts.upstream_request_id()),
        release_reason: Some("completed".into()),
        downstream_started: parts.handoff.started(),
        ..FinalizationData::default()
    }
}

fn responses_terminal_data(
    parts: &PendingStreamFinalizationParts,
    facts: &AttemptStreamFacts,
    usage: Option<CanonicalUsage>,
    failed: bool,
) -> FinalizationData {
    let mut data = success_terminal_data(parts, facts, usage);
    data.outcome = FinalizationOutcome::MidstreamError;
    // Terminal midstream failures are not 200 successes.
    data.status_code = None;
    data.error_class = Some("ResponsesTerminalEvent".into());
    data.error_detail = Some(if failed {
        "terminal_failure".into()
    } else {
        "terminal_incomplete".into()
    });
    data.release_reason = Some("attempt_failed".into());
    data.retry_category = Some("never".into());
    data
}

fn eof_failure_data(
    parts: &PendingStreamFinalizationParts,
    facts: &AttemptStreamFacts,
    usage: Option<CanonicalUsage>,
    outcome: &str,
    downstream_started: bool,
) -> FinalizationData {
    let mut data = success_terminal_data(parts, facts, usage);
    data.outcome = FinalizationOutcome::MidstreamError;
    // EOF failures are not 200 successes.
    data.status_code = None;
    data.error_class = Some(match outcome {
        OUTCOME_EMPTY_EOF => "EmptyEof".into(),
        OUTCOME_MALFORMED_EOF => "MalformedEof".into(),
        _ => "PrematureEof".into(),
    });
    data.error_detail = Some(outcome.to_owned());
    data.release_reason = Some("attempt_failed".into());
    data.retry_category = Some("never".into());
    data.is_retry_outcome = false;
    data.downstream_started = downstream_started;
    data
}
