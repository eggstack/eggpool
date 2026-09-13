//! Single-owner post-handoff body streaming and cancellation lifecycle.

use std::{
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};

use bytes::Bytes;
use tokio::{runtime::Handle, time::timeout};

use crate::{
    routing::{RoutingRouter, SelectionClaim},
    wire::ir::{CanonicalEventType, CanonicalUsage},
    wire::{WireStream, WireSurface},
};

use crate::coordinator::{
    DownstreamResult, FailureDecisionEngine, FinalizationCommand, FinalizationData,
    FinalizationError, FinalizationIdentity, FinalizationOutcome, FinalizationResult,
    FinalizationSupervisor, ResponseHandoffState, WireResolver,
};

use super::{
    OUTCOME_CLIENT_CANCELLED, StreamChunkError, StreamClientHeaders, StreamDiagnostics,
    StreamPhase, bounded_i64, bounded_usize, cache_status, duration_i64, store_eof,
    store_idle_timeout, store_midstream_transport, store_translation_error,
};

#[derive(Debug, Clone)]
pub(crate) struct AttemptStreamFacts {
    pub(crate) attempt_number: u32,
    pub(crate) wire_surface: WireSurface,
    pub(crate) candidate_fingerprint: String,
    pub(crate) transcoded: bool,
    pub(crate) upstream_protocol: String,
    pub(crate) request_bytes: usize,
}

impl AttemptStreamFacts {
    pub(crate) fn terminal() -> Self {
        Self {
            attempt_number: 1,
            wire_surface: WireSurface::OpenaiChatCompletions,
            candidate_fingerprint: String::new(),
            transcoded: false,
            upstream_protocol: String::new(),
            request_bytes: 0,
        }
    }
}

/// Incremental body state for one live upstream stream.
///
/// Only scalar progress plus the current chunk exist here: provider bytes are
/// pushed through M6 and encoded per chunk, never accumulated.
pub(crate) struct ActiveStream {
    pub(crate) body: Option<crate::providers::ProviderBody>,
    pub(crate) wire: Option<WireStream>,
    pub(crate) pending_raw: Option<Bytes>,
    pub(crate) pending_client: Option<Bytes>,
    pub(crate) idle_timeout: Option<Duration>,
    pub(crate) provider_bytes: usize,
    pub(crate) client_bytes: usize,
    pub(crate) events_forwarded: u64,
    pub(crate) malformed_chunks: usize,
    pub(crate) saw_terminal_event: bool,
    pub(crate) completion_policy: String,
    pub(crate) first_byte_elapsed: Option<Duration>,
}

impl std::fmt::Debug for ActiveStream {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("ActiveStream")
            .field("body_present", &self.body.is_some())
            .field("has_wire", &self.wire.is_some())
            .field("provider_bytes", &self.provider_bytes)
            .field("client_bytes", &self.client_bytes)
            .field("events_forwarded", &self.events_forwarded)
            .field("malformed_chunks", &self.malformed_chunks)
            .finish()
    }
}

/// The caller-held streaming execution: response headers plus the incremental
/// body driver plus retained C006 ownership.
///
/// The caller marks [`Self::mark_started`] immediately before sending
/// response start downstream, pulls [`Self::next_chunk`] until it returns
/// `None` (clean terminal) or `Some(Err(_))` (failed terminal), writing each
/// chunk downstream, then awaits [`Self::complete`]. Dropping the value
/// without completing schedules an interrupted/cancelled retained command.
pub struct StreamingExecution {
    /// Filtered upstream headers with proxy compatibility IDs.
    pub headers: StreamClientHeaders,
    /// Finite error envelope for pre-handoff terminal outcomes. `None` for
    /// live streams; the body driver yields chunks instead.
    pub error_body: Option<Bytes>,
    pub(crate) facts: AttemptStreamFacts,
    pub(crate) completion: PendingStreamFinalization,
    pub(crate) router: RoutingRouter,
    pub(crate) wire_resolver: WireResolver,
    pub(crate) failure_engine: Arc<Mutex<FailureDecisionEngine>>,
}

impl std::fmt::Debug for StreamingExecution {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("StreamingExecution")
            .field("headers", &self.headers)
            .field("is_stream", &self.is_stream())
            .field("phase", &self.phase())
            .finish()
    }
}

impl StreamingExecution {
    /// Whether this execution carries a live upstream stream (`false` means a
    /// pre-handoff terminal error envelope in [`Self::error_body`]).
    pub fn is_stream(&self) -> bool {
        self.error_body.is_none()
    }

    /// Current lifecycle phase.
    pub fn phase(&self) -> StreamPhase {
        self.completion.phase()
    }

    /// Raw provider bytes observed so far (scalar diagnostic).
    pub fn provider_bytes_observed(&self) -> usize {
        self.completion
            .parts
            .as_ref()
            .map(|parts| {
                parts
                    .stream
                    .as_ref()
                    .map_or(0, |stream| stream.provider_bytes)
            })
            .unwrap_or(0)
    }

    /// Encoded client bytes emitted so far (scalar diagnostic).
    pub fn client_bytes_emitted(&self) -> usize {
        self.completion
            .parts
            .as_ref()
            .map(|parts| {
                parts
                    .stream
                    .as_ref()
                    .map_or(0, |stream| stream.client_bytes)
            })
            .unwrap_or(0)
    }

    /// Whether the M4 provider body has been released.
    pub fn transport_released(&self) -> bool {
        self.completion.parts.as_ref().is_none_or(|parts| {
            parts
                .stream
                .as_ref()
                .is_none_or(|stream| stream.body.is_none())
        })
    }

    pub fn mark_started(&self) {
        self.completion.handoff().mark_started();
    }

    pub fn handoff_started(&self) -> bool {
        self.completion.handoff().started()
    }

    /// Return the bounded scalar terminal usage event before stream ownership
    /// is consumed by [`Self::complete`]. The event carries no stream/body
    /// data and is safe to pass to the process-owned metrics coalescer.
    pub fn usage_metric_event(&self) -> Option<crate::operations::metrics::UsageMetricEvent> {
        let parts = self.completion.parts.as_ref()?;
        let data = &parts.data;
        let status = match data.outcome {
            FinalizationOutcome::Completed => "success",
            FinalizationOutcome::ClientError => "client_error",
            FinalizationOutcome::ClientCancelled => "cancelled",
            FinalizationOutcome::UpstreamError
            | FinalizationOutcome::MidstreamError
            | FinalizationOutcome::Timeout
            | FinalizationOutcome::Interrupted => "error",
        };
        let mut event = crate::operations::metrics::UsageMetricEvent::now(
            parts.identity.provider_id.clone(),
            parts.identity.model_id.clone(),
            parts.identity.account_id,
            parts.identity.client_protocol.clone(),
            status,
        );
        event.streamed = true;
        event.input_tokens = data.input_tokens;
        event.output_tokens = data.output_tokens;
        event.cache_read_tokens = data.cache_read_tokens;
        event.cache_write_tokens = data.cache_write_tokens;
        event.reasoning_tokens = data.reasoning_tokens;
        event.thinking_characters = data.thinking_characters;
        event.cost_microdollars = data.cost_microdollars;
        event.bytes_received = data.bytes_received;
        event.bytes_emitted = data.bytes_emitted;
        event.latency_ms = data.latency_ms;
        event.first_byte_ms = data.first_byte_ms;
        event.retry_count = i64::from(data.is_retry_outcome);
        Some(event)
    }

    /// Pull the next encoded client chunk.
    ///
    /// Returns `Some(Ok(bytes))` to write downstream, `None` at a clean
    /// terminal (completed, compatibility, or an already-forwarded Responses
    /// terminal event), and `Some(Err(_))` at a failed terminal (empty,
    /// premature, or malformed EOF, idle timeout, midstream transport or
    /// translation failure). Terminal ownership is stored on either terminal;
    /// the caller then awaits [`Self::complete`]. Failures never retry.
    pub async fn next_chunk(&mut self) -> Option<Result<Bytes, StreamChunkError>> {
        let Self {
            completion,
            facts,
            router,
            wire_resolver,
            failure_engine,
            ..
        } = self;
        let parts = completion.parts_mut()?;
        if parts.exhausted {
            return None;
        }
        if parts.stream.is_none() {
            parts.exhausted = true;
            return None;
        }
        loop {
            // Serve bytes already decoded at a previous terminal boundary.
            let stashed = parts
                .stream
                .as_mut()
                .and_then(|stream| stream.pending_client.take());
            if let Some(pending) = stashed {
                return Some(Ok(pending));
            }
            // Pull one raw provider chunk. The borrow of the stream ends
            // before any terminal helper runs, so terminal ownership can
            // take `parts` without conflicting borrows.
            enum Pull {
                Chunk(Bytes),
                Eof,
                Idle,
                Transport,
                Gone,
            }
            let pull = {
                let Some(stream) = parts.stream.as_mut() else {
                    parts.exhausted = true;
                    return None;
                };
                if let Some(pending) = stream.pending_raw.take() {
                    Pull::Chunk(pending)
                } else {
                    match stream.body.as_mut() {
                        None => Pull::Gone,
                        Some(body) => {
                            let idle = stream.idle_timeout;
                            match pull_body(body, idle).await {
                                PullBody::Chunk(chunk) => Pull::Chunk(chunk),
                                PullBody::Eof => Pull::Eof,
                                PullBody::Idle => Pull::Idle,
                                PullBody::Transport => Pull::Transport,
                                PullBody::Skip => continue,
                            }
                        }
                    }
                }
            };
            match pull {
                Pull::Gone => {
                    parts.exhausted = true;
                    return None;
                }
                Pull::Idle => {
                    return store_idle_timeout(parts, facts, router, failure_engine);
                }
                Pull::Transport => {
                    return store_midstream_transport(parts, facts, router, failure_engine);
                }
                Pull::Eof => {
                    return store_eof(parts, facts, router, wire_resolver);
                }
                Pull::Chunk(chunk) => {
                    enum Decoded {
                        Forward(Bytes, bool),
                        Skip,
                        TranslationError,
                    }
                    let decoded = {
                        let Some(stream) = parts.stream.as_mut() else {
                            parts.exhausted = true;
                            return None;
                        };
                        stream.provider_bytes = stream.provider_bytes.saturating_add(chunk.len());
                        match stream.decode_chunk(chunk) {
                            ChunkDecode::Forward(bytes, saw_terminal) => {
                                Decoded::Forward(bytes, saw_terminal)
                            }
                            ChunkDecode::Skip => Decoded::Skip,
                            ChunkDecode::TranslationError => Decoded::TranslationError,
                        }
                    };
                    match decoded {
                        Decoded::Skip => continue,
                        Decoded::TranslationError => {
                            return store_translation_error(parts, facts, router, failure_engine);
                        }
                        Decoded::Forward(bytes, saw_terminal) => {
                            let provider_bytes = parts
                                .stream
                                .as_ref()
                                .map_or(0, |stream| stream.provider_bytes);
                            parts.data.bytes_emitted = bounded_usize(provider_bytes);
                            if saw_terminal {
                                parts.phase = StreamPhase::TerminalEvidence;
                            }
                            return Some(Ok(bytes));
                        }
                    }
                }
            }
        }
    }
} // end `impl StreamingExecution`

/// Pull one item from the M4 body with the M7 idle timer applied.
enum PullBody {
    Chunk(Bytes),
    Eof,
    Idle,
    Transport,
    Skip,
}

async fn pull_body(body: &mut crate::providers::ProviderBody, idle: Option<Duration>) -> PullBody {
    let next = match idle {
        Some(limit) => match timeout(limit, body.next()).await {
            Ok(value) => value,
            Err(_) => return PullBody::Idle,
        },
        None => body.next().await,
    };
    match next {
        None => PullBody::Eof,
        Some(Err(_)) => PullBody::Transport,
        Some(Ok(chunk)) if chunk.is_empty() => PullBody::Skip,
        Some(Ok(chunk)) => PullBody::Chunk(chunk),
    }
}

/// Decoded form of one provider chunk: bytes to forward, a skip, or a
/// terminal translation failure.
enum ChunkDecode {
    Forward(Bytes, bool),
    Skip,
    TranslationError,
}

impl ActiveStream {
    /// Push one raw provider chunk through M6 and encode canonical events to
    /// the client surface. Never accumulates across calls.
    fn decode_chunk(&mut self, chunk: Bytes) -> ChunkDecode {
        let Some(wire) = self.wire.as_mut() else {
            // Legacy non-SSE pass-through: forward raw provider bytes.
            self.client_bytes = self.client_bytes.saturating_add(chunk.len());
            return ChunkDecode::Forward(chunk, false);
        };
        let pushed = match wire.push(&chunk) {
            Ok(pushed) => pushed,
            Err(_) => {
                // Malformed provider chunk: skip it without forwarding, keep
                // the bounded error count, and let EOF classification report
                // malformed. The stream stays terminal-false-success either
                // way.
                self.malformed_chunks = self.malformed_chunks.saturating_add(1);
                return ChunkDecode::Skip;
            }
        };
        let mut out = Vec::new();
        let mut saw_terminal = false;
        for event in &pushed.events {
            if matches!(
                event.event_type,
                CanonicalEventType::ResponseComplete
                    | CanonicalEventType::ResponseIncomplete
                    | CanonicalEventType::Error
            ) {
                saw_terminal = true;
            }
            match wire.encode_client_event(event) {
                Ok(bytes) if !bytes.is_empty() => {
                    self.events_forwarded = self.events_forwarded.saturating_add(1);
                    out.extend_from_slice(&bytes);
                }
                Ok(_) => {}
                Err(_) => return ChunkDecode::TranslationError,
            }
        }
        self.saw_terminal_event |= saw_terminal;
        if out.is_empty() {
            return ChunkDecode::Skip;
        }
        let bytes = Bytes::from(out);
        self.client_bytes = self.client_bytes.saturating_add(bytes.len());
        ChunkDecode::Forward(bytes, saw_terminal)
    }
}

impl StreamingExecution {
    /// Transfer terminal ownership to the retained C006 supervisor. A write
    /// failure or cancellation after handoff finalizes as cancelled without
    /// ever re-entering the upstream retry loop.
    pub async fn complete(
        mut self,
        downstream: DownstreamResult,
    ) -> Result<FinalizationResult, FinalizationError> {
        self.completion.complete(downstream).await
    }
}

pub(crate) struct PendingStreamFinalization {
    parts: Option<PendingStreamFinalizationParts>,
}

pub(crate) struct PendingStreamFinalizationParts {
    pub(crate) supervisor: FinalizationSupervisor,
    pub(crate) identity: FinalizationIdentity,
    pub(crate) claim: Option<SelectionClaim>,
    pub(crate) data: FinalizationData,
    pub(crate) handoff: ResponseHandoffState,
    pub(crate) stream: Option<ActiveStream>,
    pub(crate) started_at: Instant,
    pub(crate) terminal_stored: bool,
    pub(crate) exhausted: bool,
    pub(crate) phase: StreamPhase,
    pub(crate) diagnostics: Arc<Mutex<StreamDiagnostics>>,
}

impl PendingStreamFinalization {
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn new(
        supervisor: FinalizationSupervisor,
        identity: FinalizationIdentity,
        claim: Option<SelectionClaim>,
        data: FinalizationData,
        phase: StreamPhase,
        stream: Option<(ActiveStream, Instant)>,
        diagnostics: Arc<Mutex<StreamDiagnostics>>,
    ) -> Self {
        let (stream, started_at) = match stream {
            Some((stream, started_at)) => (Some(stream), started_at),
            None => (None, Instant::now()),
        };
        Self {
            parts: Some(PendingStreamFinalizationParts {
                supervisor,
                identity,
                claim,
                data,
                handoff: ResponseHandoffState::default(),
                stream,
                started_at,
                terminal_stored: false,
                exhausted: false,
                phase,
                diagnostics,
            }),
        }
    }

    fn handoff(&self) -> &ResponseHandoffState {
        &self
            .parts
            .as_ref()
            .expect("pending stream finalization exists")
            .handoff
    }

    fn phase(&self) -> StreamPhase {
        self.parts
            .as_ref()
            .map(|parts| {
                if parts.stream.is_some()
                    && parts.handoff.started()
                    && matches!(parts.phase, StreamPhase::DownstreamPending)
                {
                    StreamPhase::Streaming
                } else {
                    parts.phase
                }
            })
            .unwrap_or(StreamPhase::RetainedFinalization)
    }

    fn parts_mut(&mut self) -> Option<&mut PendingStreamFinalizationParts> {
        self.parts.as_mut()
    }

    async fn complete(
        &mut self,
        downstream: DownstreamResult,
    ) -> Result<FinalizationResult, FinalizationError> {
        let mut parts = self
            .parts
            .take()
            .expect("pending stream finalization exists");
        if parts.handoff.started()
            && matches!(parts.phase, StreamPhase::DownstreamPending)
            && parts.stream.is_some()
        {
            parts.phase = StreamPhase::Streaming;
        }
        let handoff_started = parts.handoff.started();
        if downstream != DownstreamResult::Delivered {
            parts.data.outcome = if handoff_started {
                FinalizationOutcome::ClientCancelled
            } else {
                FinalizationOutcome::Interrupted
            };
            parts.data.error_class = Some(if handoff_started {
                "DownstreamWriteFailed".into()
            } else {
                "DownstreamCancelledBeforeStart".into()
            });
            parts.data.release_reason = Some("downstream_write_failed".into());
            if handoff_started {
                parts.diagnostics_record(
                    OUTCOME_CLIENT_CANCELLED,
                    parts.attempt_number(),
                    parts.stream_bytes(),
                    parts.elapsed(),
                );
            }
        }
        parts.data.downstream_started = handoff_started;
        parts.data.latency_ms = duration_i64(parts.elapsed());
        parts.stream = None;
        parts.phase = StreamPhase::RetainedFinalization;
        let command = FinalizationCommand::Request {
            identity: parts.identity,
            data: parts.data,
            claim: parts.claim,
        };
        let handle = parts.supervisor.register(command)?;
        handle.wait().await
    }
}

impl PendingStreamFinalizationParts {
    pub(crate) fn elapsed(&self) -> Duration {
        self.started_at.elapsed()
    }

    pub(crate) fn stream_bytes(&self) -> usize {
        self.stream
            .as_ref()
            .map(|stream| stream.provider_bytes)
            .unwrap_or_else(|| self.data.bytes_emitted.max(0) as usize)
    }

    pub(crate) fn malformed_count(&self) -> usize {
        self.stream
            .as_ref()
            .map(|stream| stream.malformed_chunks)
            .unwrap_or(0)
    }

    pub(crate) fn first_byte_ms(&self) -> Option<i64> {
        self.stream
            .as_ref()
            .and_then(|stream| stream.first_byte_elapsed)
            .map(duration_i64)
            .or(self.data.first_byte_ms)
    }

    pub(crate) fn upstream_request_id(&self) -> Option<String> {
        self.data.upstream_request_id.clone()
    }

    pub(crate) fn midstream_usage(&self) -> Option<CanonicalUsage> {
        self.stream
            .as_ref()
            .and_then(|stream| stream.wire.as_ref())
            .and_then(|wire| wire.usage())
    }

    fn attempt_number(&self) -> u32 {
        u32::try_from(self.identity.attempt_number.max(1)).unwrap_or(1)
    }

    pub(crate) fn release_transport(&mut self) {
        if let Some(stream) = self.stream.as_mut() {
            stream.body = None;
        }
    }

    pub(crate) fn diagnostics_record(
        &self,
        outcome: &'static str,
        attempt: u32,
        bytes_emitted: usize,
        elapsed: Duration,
    ) {
        if let Ok(mut guard) = self.diagnostics.lock() {
            guard.record(outcome, attempt, bytes_emitted, duration_i64(elapsed));
        }
    }
}

impl Drop for PendingStreamFinalization {
    fn drop(&mut self) {
        let Some(mut parts) = self.parts.take() else {
            return;
        };
        // A stored natural terminal survives cancellation during the
        // finalization handoff; only an unterminated stream is reinterpreted
        // as interrupted/cancelled.
        if !parts.terminal_stored {
            let started = parts.handoff.started();
            parts.data.outcome = if started {
                FinalizationOutcome::ClientCancelled
            } else {
                FinalizationOutcome::Interrupted
            };
            parts.data.error_class = Some(if started {
                "DownstreamCancelledAfterStart".into()
            } else {
                "CoordinatorInterrupted".into()
            });
            parts.data.release_reason = Some(if started {
                "downstream_write_failed".into()
            } else {
                "pending_response_dropped".into()
            });
            if started {
                parts.diagnostics_record(
                    OUTCOME_CLIENT_CANCELLED,
                    parts.attempt_number(),
                    parts.stream_bytes(),
                    parts.elapsed(),
                );
            }
        }
        parts.data.downstream_started = parts.handoff.started();
        parts.data.latency_ms = duration_i64(parts.elapsed());
        if !parts.terminal_stored
            && let Some(usage) = parts.midstream_usage()
        {
            parts.data.input_tokens = bounded_i64(usage.input_tokens);
            parts.data.output_tokens = bounded_i64(usage.output_tokens);
            parts.data.cache_counter_status = Some(cache_status(usage.cache_counter_status).into());
        }
        parts.stream = None;
        let command = FinalizationCommand::Request {
            identity: parts.identity,
            data: parts.data,
            claim: parts.claim,
        };
        let supervisor = parts.supervisor;
        let Ok(handle) = Handle::try_current() else {
            return;
        };
        let Ok(finalization) = supervisor.register(command) else {
            return;
        };
        handle.spawn(async move {
            let _ = finalization.wait().await;
        });
    }
}
