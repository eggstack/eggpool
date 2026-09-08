//! C008 streaming-handoff coordinator.
//!
//! This module owns the M7 streaming lifecycle around M4 transport and the M6
//! incremental stream runtime: response-header/first-byte/idle timeout policy,
//! downstream handoff, chunk forwarding/adaptation, cancellation, terminal
//! evidence, incomplete/malformed EOF, provider midstream errors, usage
//! completion, and retained C006 finalization.
//!
//! ## Phase model
//!
//! [`StreamPhase`] distinguishes waiting for upstream headers, headers
//! accepted with downstream not started, waiting for the first provider body
//! byte, downstream response started, streaming body in progress, terminal
//! provider evidence observed, EOF/failure/cancellation, and retained
//! finalization. Header/first-byte failures before the execution is returned
//! may retry through C005; every failure after the execution is returned is
//! terminal for this client request and never transparently replays.
//!
//! Returning [`StreamingExecution`] to the caller transfers handoff ownership:
//! the caller holds response headers that will be sent downstream, so no
//! post-return failure may re-enter the upstream retry loop. This matches the
//! Python oracle, whose retry window closes when the stream generator is
//! created (after the first-byte prefetch, which stays pre-handoff here).
//!
//! ## Timeout ownership
//!
//! M4 retains transport-level connect/write/read primitives;
//! [`StreamingCoordinator`] owns response-header, first-byte, and stream-idle
//! policy via [`StreamTimeoutPolicy`] using Tokio timers. There is deliberately
//! no whole-stream deadline: the frozen Python contract parses
//! `max_lifetime_s` for backward compatibility but never enforces an absolute
//! lifetime that would kill legitimately long active streams.
//!
//! Timeout cancellation drops the active M4 body (releasing the connection)
//! before terminal ownership transfers to C006.
//!
//! ## M6 integration
//!
//! Provider bytes feed incrementally into one M6 [`WireStream`](crate::wire::WireStream)
//! for the selected profile, and canonical events encode to the original
//! client surface per chunk. The complete stream is never buffered; only
//! scalar counters, the current chunk, and bounded M6 decoder state exist at
//! any time. Transport EOF is never success on its own: the M6 terminal
//! summary classifies complete, compatibility-complete, Responses
//! failed/incomplete, Gemini incomplete, provider error, malformed, empty,
//! and partial-premature EOF.
//!
//! A non-SSE upstream body (`content-type` without `text/event-stream`)
//! preserves the Python legacy pass-through: raw chunks forward unchanged and
//! EOF is complete. SSE completion rules apply to event-stream responses
//! only.
//!
//! ## Cancellation
//!
//! Dropping a [`StreamingExecution`] before terminal completion schedules an
//! interrupted/cancelled retained command so cancellation cannot strand a
//! converted claim. Once a natural terminal is stored, dropping registers
//! that stored terminal instead, so cancellation during the finalization
//! handoff cannot overwrite durable truth. Explicit downstream outcomes flow
//! through [`DownstreamResult`]; only pre-handoff transport/timeout failures
//! consult C005, and client-originated cancellation never retries.

use std::{
    collections::{BTreeMap, BTreeSet},
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};

use bytes::Bytes;
use http::{HeaderMap, HeaderName, HeaderValue, StatusCode};
use serde_json::json;
use thiserror::Error;
use tokio::{runtime::Handle, time::timeout};

use crate::{
    accounts::CredentialStore,
    config::{ProviderConfig, ProviderStreamTimeoutConfig},
    request::{AdmissionError, AdmittedRequest, StaticRoutingFacts, admit_request},
    routing::{RoutingRequestFacts, RoutingRouter, SelectionClaim},
    wire::{
        ConfiguredWireProfile, StreamTerminalOutcome, TerminalEvidence, WireRuntime,
        WireRuntimeContext, WireStream, WireSurface,
        ir::{
            CacheCounterStatus, CanonicalEventType, CanonicalUsage, ClientSurface,
            ProviderErrorEvidence,
        },
    },
};

use super::{
    AttemptBuilder, AttemptError, AttemptInput, ClientResponseHeaders, DownstreamResult,
    FailureCategory, FailureDecisionEngine, FailureEffects, FailureObservation, FailureSource,
    FinalizationCommand, FinalizationData, FinalizationError, FinalizationIdentity,
    FinalizationOutcome, FinalizationResult, FinalizationSupervisor, PublicationError,
    PublicationInput, PublicationOutcome, PublicationService, ResponseHandoffState, RetryPolicy,
    WireResolver, filter_response_headers,
};

const MAX_CLIENT_ERROR_BYTES: usize = 512;

// ---------------------------------------------------------------------------
// Timeout policy
// ---------------------------------------------------------------------------

/// M7-owned streaming timeout policy.
///
/// `None` preserves the historical transport behavior for that phase (no
/// coordinator timer). There is intentionally no whole-stream deadline field.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct StreamTimeoutPolicy {
    /// Time allowed for upstream response headers after dispatch.
    pub header_timeout: Option<Duration>,
    /// Time allowed for the first provider body byte after headers.
    pub first_byte_timeout: Option<Duration>,
    /// Inactivity allowed between provider body chunks once streaming.
    pub idle_timeout: Option<Duration>,
}

impl StreamTimeoutPolicy {
    /// Resolve the policy from one provider's configuration.
    ///
    /// The header timer uses the provider read timeout (the same guardrail
    /// the transport applies to header wait); first-byte/idle timers come
    /// from the explicit stream-timeout policy. `max_lifetime_s` is parsed
    /// for compatibility but never enforced.
    pub fn from_provider(provider: &ProviderConfig) -> Self {
        Self {
            header_timeout: duration_from_secs(provider.read_timeout_s),
            first_byte_timeout: provider
                .stream_timeouts
                .first_byte_timeout_s
                .and_then(duration_from_secs),
            idle_timeout: provider
                .stream_timeouts
                .idle_timeout_s
                .and_then(duration_from_secs),
        }
    }

    /// Test override for the provider stream-timeout configuration.
    pub fn test_config(
        first_byte_timeout_s: Option<f64>,
        idle_timeout_s: Option<f64>,
    ) -> ProviderStreamTimeoutConfig {
        ProviderStreamTimeoutConfig {
            first_byte_timeout_s,
            idle_timeout_s,
            max_lifetime_s: None,
        }
    }
}

fn duration_from_secs(seconds: f64) -> Option<Duration> {
    Duration::try_from_secs_f64(seconds)
        .ok()
        .filter(|duration| !duration.is_zero())
}

// ---------------------------------------------------------------------------
// Phase model
// ---------------------------------------------------------------------------

/// Observable streaming lifecycle phase.
///
/// The first three phases live inside [`StreamingCoordinator::execute`]; the
/// rest are observable on [`StreamingExecution::phase`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StreamPhase {
    /// Upstream dispatch sent; response headers pending.
    WaitingUpstreamHeaders,
    /// Upstream headers accepted; downstream response not started.
    HeadersAccepted,
    /// Headers accepted; first provider body byte pending.
    WaitingFirstByte,
    /// First byte observed (or empty EOF pending); execution returned but the
    /// caller has not marked downstream start.
    DownstreamPending,
    /// Downstream response started; body chunks flowing.
    Streaming,
    /// Native terminal evidence observed; draining to EOF.
    TerminalEvidence,
    /// EOF, failure, or cancellation observed; provider body released.
    Closed,
    /// Terminal command registered with the retained C006 supervisor.
    RetainedFinalization,
}

// ---------------------------------------------------------------------------
// Chunk errors and diagnostics
// ---------------------------------------------------------------------------

/// Terminal failure surfaced by [`StreamingExecution::next_chunk`].
///
/// Every variant is terminal for this client request: the provider body is
/// released, C006 ownership is stored, and the caller must not retry
/// upstream. Variants carry no bodies, secrets, or provider prose.
#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum StreamChunkError {
    #[error("provider stream exceeded its idle timeout")]
    IdleTimeout,
    #[error("provider stream ended before any body bytes")]
    EmptyEof,
    #[error("provider stream ended without terminal evidence")]
    PrematureEof,
    #[error("provider stream carried malformed SSE or undecodable events")]
    MalformedEof,
    #[error("provider stream transport failed midstream")]
    UpstreamTransport,
    #[error("provider stream event could not be encoded for the client")]
    Translation,
}

pub const OUTCOME_RESPONSE_HEADER_TIMEOUT: &str = "response_header_timeout";
pub const OUTCOME_FIRST_BYTE_TIMEOUT: &str = "first_byte_timeout";
pub const OUTCOME_IDLE_TIMEOUT: &str = "stream_idle_timeout";
pub const OUTCOME_COMPLETED_CANONICAL: &str = "stream_completed_canonical";
pub const OUTCOME_COMPLETED_COMPATIBILITY: &str = "stream_completed_compatibility";
pub const OUTCOME_EMPTY_EOF: &str = "empty_eof";
pub const OUTCOME_PREMATURE_EOF_BEFORE_BODY: &str = "premature_eof_before_body";
pub const OUTCOME_PREMATURE_EOF_MIDSTREAM: &str = "premature_eof_midstream";
pub const OUTCOME_MALFORMED_EOF: &str = "malformed_eof";
pub const OUTCOME_TERMINAL_FAILURE: &str = "stream_responses_terminal_failure";
pub const OUTCOME_TERMINAL_INCOMPLETE: &str = "stream_responses_terminal_incomplete";
pub const OUTCOME_UPSTREAM_MIDSTREAM_ERROR: &str = "upstream_midstream_error";
pub const OUTCOME_CLIENT_CANCELLED: &str = "client_cancelled";

const KNOWN_OUTCOMES: &[&str] = &[
    OUTCOME_RESPONSE_HEADER_TIMEOUT,
    OUTCOME_FIRST_BYTE_TIMEOUT,
    OUTCOME_IDLE_TIMEOUT,
    OUTCOME_COMPLETED_CANONICAL,
    OUTCOME_COMPLETED_COMPATIBILITY,
    OUTCOME_EMPTY_EOF,
    OUTCOME_PREMATURE_EOF_BEFORE_BODY,
    OUTCOME_PREMATURE_EOF_MIDSTREAM,
    OUTCOME_MALFORMED_EOF,
    OUTCOME_TERMINAL_FAILURE,
    OUTCOME_TERMINAL_INCOMPLETE,
    OUTCOME_UPSTREAM_MIDSTREAM_ERROR,
    OUTCOME_CLIENT_CANCELLED,
];

/// Scalar diagnostic event for a terminal streaming path.
///
/// Best-effort and secret-free: counts, durations, and labels only. No
/// request bodies, API keys, prompts, upstream chunks, or session identities.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct StreamDiagnosticEvent {
    pub outcome: &'static str,
    pub attempt: u32,
    pub bytes_emitted: usize,
    pub elapsed_ms: i64,
}

/// Bounded process-local streaming outcome counters owned by one coordinator.
#[derive(Debug, Default)]
pub struct StreamDiagnostics {
    counts: BTreeMap<&'static str, u64>,
    last: Option<StreamDiagnosticEvent>,
}

impl StreamDiagnostics {
    fn new() -> Self {
        let mut counts = BTreeMap::new();
        for outcome in KNOWN_OUTCOMES {
            counts.insert(*outcome, 0);
        }
        Self { counts, last: None }
    }

    fn record(
        &mut self,
        outcome: &'static str,
        attempt: u32,
        bytes_emitted: usize,
        elapsed_ms: i64,
    ) {
        *counts_entry(&mut self.counts, outcome) += 1;
        self.last = Some(StreamDiagnosticEvent {
            outcome,
            attempt,
            bytes_emitted,
            elapsed_ms,
        });
    }

    fn count(&self, outcome: &str) -> u64 {
        self.counts.get(outcome).copied().unwrap_or(0)
    }
}

fn counts_entry<'a>(
    counts: &'a mut BTreeMap<&'static str, u64>,
    outcome: &'static str,
) -> &'a mut u64 {
    if !KNOWN_OUTCOMES.contains(&outcome) {
        return counts.entry("unknown").or_insert(0);
    }
    counts.entry(outcome).or_insert(0)
}

/// Snapshot of streaming outcome counters for tests and operators.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StreamDiagnosticsSnapshot {
    pub outcomes: BTreeMap<String, u64>,
    pub last: Option<StreamDiagnosticEvent>,
}

// ---------------------------------------------------------------------------
// Request and response types
// ---------------------------------------------------------------------------

/// Client-visible streaming response headers.
///
/// Produced after upstream headers are accepted and filtered. The caller marks
/// [`StreamingExecution::mark_started`] immediately before sending response
/// start downstream.
#[derive(Clone, Debug)]
pub struct StreamClientHeaders {
    pub status: StatusCode,
    pub headers: ClientResponseHeaders,
}

/// Streaming request input. Admission runs once here; the admitted request is
/// reused by the attempt builder rather than decoding the body again.
#[derive(Debug, Clone)]
pub struct StreamRequest {
    pub proxy_request_id: String,
    pub raw_body: Bytes,
    pub incoming_headers: HeaderMap,
    pub request_id: Option<String>,
    pub correlation_id: Option<String>,
    pub client_surface: ClientSurface,
    pub admitted: AdmittedRequest,
    pub routing_facts: RoutingRequestFacts,
}

impl StreamRequest {
    pub fn new(
        proxy_request_id: impl Into<String>,
        raw_body: Bytes,
        incoming_headers: HeaderMap,
        client_surface: ClientSurface,
        mut routing_inputs: StaticRoutingFacts,
    ) -> Result<Self, StreamingCoordinatorError> {
        let admitted = admit_request(
            &raw_body,
            crate::request::AdmissionOptions {
                client_surface,
                ..crate::request::AdmissionOptions::default()
            },
        )
        .map_err(StreamingCoordinatorError::Admission)?;
        if !admitted.canonical.stream {
            return Err(StreamingCoordinatorError::InvalidFacts);
        }
        if routing_inputs.requested_protocol.is_none() {
            routing_inputs.requested_protocol = Some(client_surface.protocol().into());
        }
        let routing_facts = admitted.routing_facts(&routing_inputs);
        Ok(Self {
            proxy_request_id: proxy_request_id.into(),
            raw_body,
            incoming_headers,
            request_id: None,
            correlation_id: None,
            client_surface,
            admitted,
            routing_facts,
        })
    }

    pub fn from_admitted(
        proxy_request_id: impl Into<String>,
        raw_body: Bytes,
        incoming_headers: HeaderMap,
        client_surface: ClientSurface,
        admitted: AdmittedRequest,
        routing_facts: RoutingRequestFacts,
    ) -> Result<Self, StreamingCoordinatorError> {
        if !admitted.canonical.stream
            || admitted.canonical.client_surface != client_surface
            || admitted.canonical.model != routing_facts.canonical_model_id
            || routing_facts.request_surface != client_surface.as_str()
        {
            return Err(StreamingCoordinatorError::InvalidFacts);
        }
        Ok(Self {
            proxy_request_id: proxy_request_id.into(),
            raw_body,
            incoming_headers,
            request_id: None,
            correlation_id: None,
            client_surface,
            admitted,
            routing_facts,
        })
    }
}

#[derive(Debug, Error)]
pub enum StreamingCoordinatorError {
    #[error("client request admission failed: {0}")]
    Admission(#[from] AdmissionError),
    #[error("no eligible account was available for streaming request")]
    NoEligibleRoute,
    #[error("streaming request selected unknown provider {provider_id:?}")]
    MissingProvider { provider_id: String },
    #[error("streaming request has no configured wire profile for provider {provider_id:?}")]
    MissingWireProfile { provider_id: String },
    #[error("streaming request publication failed: {0}")]
    Publication(#[from] PublicationError),
    #[error("streaming routing claim failed: {0}")]
    Claim(#[from] crate::routing::ClaimError),
    #[error("streaming provider attempt failed: {0}")]
    Attempt(#[from] AttemptError),
    #[error("streaming finalization failed: {0}")]
    Finalization(#[from] FinalizationError),
    #[error("streaming failure effect ledger failed: {0}")]
    Effects(String),
    #[error("streaming request facts do not match admitted model, surface, or stream intent")]
    InvalidFacts,
}

/// The last upstream error response retained for pre-handoff exhaustion
/// pass-through. Python's `_handle_exhausted` prefers this real response over
/// a synthetic envelope.
#[derive(Clone, Debug)]
struct LastUpstream {
    status: StatusCode,
    headers: HeaderMap,
    body: Bytes,
    effects: FailureEffects,
    upstream_request_id: Option<String>,
    headers_elapsed: Duration,
}

// ---------------------------------------------------------------------------
// Coordinator
// ---------------------------------------------------------------------------

/// End-to-end streaming coordinator for one immutable migration generation.
#[derive(Clone)]
pub struct StreamingCoordinator {
    router: RoutingRouter,
    publication: PublicationService,
    attempts: AttemptBuilder,
    wire: WireRuntime,
    wire_resolver: WireResolver,
    provider_profiles: BTreeMap<String, Vec<ConfiguredWireProfile>>,
    providers: BTreeMap<String, ProviderConfig>,
    credentials: CredentialStore,
    finalization: FinalizationSupervisor,
    retry_policy: RetryPolicy,
    failure_engine: Arc<Mutex<FailureDecisionEngine>>,
    diagnostics: Arc<Mutex<StreamDiagnostics>>,
    max_provider_body_bytes: usize,
    forced_header_timeout: Option<Option<Duration>>,
    forced_first_byte_timeout: Option<Option<Duration>>,
    forced_idle_timeout: Option<Option<Duration>>,
}

impl std::fmt::Debug for StreamingCoordinator {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("StreamingCoordinator")
            .field("providers", &self.providers.keys().collect::<Vec<_>>())
            .field("retry_policy", &self.retry_policy)
            .field("max_provider_body_bytes", &self.max_provider_body_bytes)
            .finish()
    }
}

#[allow(clippy::too_many_arguments)]
impl StreamingCoordinator {
    pub fn new(
        router: RoutingRouter,
        publication: PublicationService,
        attempts: AttemptBuilder,
        wire: WireRuntime,
        wire_resolver: WireResolver,
        provider_profiles: BTreeMap<String, Vec<ConfiguredWireProfile>>,
        providers: BTreeMap<String, ProviderConfig>,
        credentials: CredentialStore,
        finalization: FinalizationSupervisor,
        retry_policy: RetryPolicy,
    ) -> Self {
        Self {
            router,
            publication,
            attempts,
            wire,
            wire_resolver,
            provider_profiles,
            providers,
            credentials,
            finalization,
            retry_policy,
            failure_engine: Arc::new(Mutex::new(FailureDecisionEngine::new(retry_policy))),
            diagnostics: Arc::new(Mutex::new(StreamDiagnostics::new())),
            max_provider_body_bytes: crate::wire::DEFAULT_MAX_PROVIDER_BODY_BYTES,
            forced_header_timeout: None,
            forced_first_byte_timeout: None,
            forced_idle_timeout: None,
        }
    }

    pub fn finalization_supervisor(&self) -> FinalizationSupervisor {
        self.finalization.clone()
    }

    pub fn wire_resolver(&self) -> WireResolver {
        self.wire_resolver.clone()
    }

    pub fn with_max_provider_body_bytes(mut self, limit: usize) -> Self {
        self.max_provider_body_bytes = limit.max(1);
        self
    }

    /// Force the response-header timer regardless of provider configuration.
    /// `None` disables the timer. Without an override the provider read
    /// timeout applies.
    pub fn with_header_timeout_override(mut self, value: Option<Duration>) -> Self {
        self.forced_header_timeout = Some(value);
        self
    }

    /// Force the first-byte timer regardless of provider configuration.
    pub fn with_first_byte_timeout_override(mut self, value: Option<Duration>) -> Self {
        self.forced_first_byte_timeout = Some(value);
        self
    }

    /// Force the idle timer regardless of provider configuration.
    pub fn with_idle_timeout_override(mut self, value: Option<Duration>) -> Self {
        self.forced_idle_timeout = Some(value);
        self
    }

    /// Snapshot the bounded outcome counters.
    pub fn diagnostics_snapshot(&self) -> StreamDiagnosticsSnapshot {
        let guard = self.diagnostics.lock().expect("stream diagnostics lock");
        StreamDiagnosticsSnapshot {
            outcomes: guard
                .counts
                .iter()
                .map(|(key, value)| ((*key).to_owned(), *value))
                .collect(),
            last: guard.last,
        }
    }

    pub fn diagnostic_count(&self, outcome: &str) -> u64 {
        self.diagnostics
            .lock()
            .expect("stream diagnostics lock")
            .count(outcome)
    }

    fn policy_for(&self, provider_id: &str) -> StreamTimeoutPolicy {
        let base = self
            .providers
            .get(provider_id)
            .map(StreamTimeoutPolicy::from_provider)
            .unwrap_or_default();
        StreamTimeoutPolicy {
            header_timeout: self.forced_header_timeout.unwrap_or(base.header_timeout),
            first_byte_timeout: self
                .forced_first_byte_timeout
                .unwrap_or(base.first_byte_timeout),
            idle_timeout: self.forced_idle_timeout.unwrap_or(base.idle_timeout),
        }
    }

    fn completion_policy_for(&self, provider_id: &str) -> String {
        self.providers
            .get(provider_id)
            .map(|provider| provider.stream_completion_policy.clone())
            .unwrap_or_else(|| "strict".to_owned())
    }

    fn record_outcome(
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

    /// Execute the streaming upstream lifecycle.
    ///
    /// Only pre-handoff failures (transport/header/first-byte/error-status
    /// before the execution is returned) enter the retry loop. The returned
    /// execution owns response headers plus, for 2xx event streams, the live
    /// incremental body driver. Every failure after return is terminal and
    /// never replays upstream.
    pub async fn execute(
        &self,
        request: StreamRequest,
    ) -> Result<StreamingExecution, StreamingCoordinatorError> {
        if request.admitted.canonical.client_surface != request.client_surface
            || request.routing_facts.canonical_model_id != request.admitted.canonical.model
            || !request.admitted.canonical.stream
        {
            return Err(StreamingCoordinatorError::InvalidFacts);
        }

        let mut attempt_number = 1_u32;
        let mut excluded_accounts = BTreeSet::new();
        let mut preferred_account = None;
        let mut attempted_wires: BTreeMap<String, BTreeSet<WireSurface>> = BTreeMap::new();
        let mut last_identity: Option<FinalizationIdentity> = None;
        let mut last_upstream: Option<LastUpstream> = None;
        // Last pre-handoff failure facts (effects, status, upstream
        // protocol). The exhaustion terminal below must reuse them so the
        // terminal command stays compatible with the already-persisted
        // failed-attempt row; C014 fails closed on incompatible facts.
        let mut last_failure: Option<(FailureEffects, Option<StatusCode>, String)> = None;
        let request_start = Instant::now();
        let request_bytes = request.raw_body.len();

        loop {
            let claim = if let Some(account_name) = preferred_account.as_deref() {
                self.router
                    .select_and_claim_for_account(&request.routing_facts, account_name)
                    .await?
            } else {
                self.router
                    .select_and_claim(&request.routing_facts, &excluded_accounts)
                    .await?
            };
            let Some(claim) = claim else {
                if let (Some(identity), Some(last)) = (last_identity.clone(), last_upstream.clone())
                {
                    let headers = self.client_headers(
                        last.status,
                        &last.headers,
                        &request.proxy_request_id,
                        attempt_number.saturating_sub(1).max(1),
                        true,
                    );
                    let upstream_protocol = identity.upstream_protocol.clone();
                    let data = self.failure_data(
                        &identity,
                        &upstream_protocol,
                        &last.effects,
                        Some(last.status),
                        last.upstream_request_id,
                        last.headers_elapsed,
                        request_bytes,
                        last.body.len(),
                        false,
                    );
                    return Ok(self.pending_terminal(
                        identity,
                        None,
                        headers,
                        Some(last.body),
                        data,
                        StreamPhase::Closed,
                    ));
                }
                return match last_identity {
                    Some(identity) if last_failure.is_some() => {
                        // A retryable pre-handoff failure was already cleaned
                        // up and persisted for this attempt. Reuse its facts
                        // so the terminal command converges instead of
                        // conflicting; the client still sees the synthetic
                        // exhaustion envelope above.
                        let (effects, status, upstream_protocol) =
                            last_failure.clone().expect("failure facts recorded");
                        let mut data = self.failure_data(
                            &identity,
                            &upstream_protocol,
                            &effects,
                            status,
                            None,
                            request_start.elapsed(),
                            request_bytes,
                            0,
                            false,
                        );
                        data.is_retry_outcome = false;
                        data.retry_category = Some("exhausted".into());
                        Ok(self.pending_terminal(
                            identity,
                            None,
                            StreamClientHeaders {
                                status: StatusCode::SERVICE_UNAVAILABLE,
                                headers: self.error_headers(
                                    &request.proxy_request_id,
                                    attempt_number.saturating_sub(1).max(1),
                                ),
                            },
                            Some(self.error_body(
                                request.client_surface,
                                "no eligible provider remained after retry",
                            )),
                            data,
                            StreamPhase::Closed,
                        ))
                    }
                    Some(identity) => Ok(self.pending_terminal(
                        identity,
                        None,
                        StreamClientHeaders {
                            status: StatusCode::SERVICE_UNAVAILABLE,
                            headers: self.error_headers(
                                &request.proxy_request_id,
                                attempt_number.saturating_sub(1).max(1),
                            ),
                        },
                        Some(self.error_body(
                            request.client_surface,
                            "no eligible provider remained after retry",
                        )),
                        FinalizationData {
                            outcome: FinalizationOutcome::UpstreamError,
                            status_code: Some(StatusCode::SERVICE_UNAVAILABLE.as_u16()),
                            error_class: Some("NoEligibleRoute".into()),
                            release_reason: Some("attempt_failed".into()),
                            downstream_started: false,
                            bytes_received: bounded_usize(request_bytes),
                            latency_ms: duration_i64(request_start.elapsed()),
                            cache_counter_status: Some("not_reported".to_owned()),
                            ..FinalizationData::default()
                        },
                        StreamPhase::Closed,
                    )),
                    None => Err(StreamingCoordinatorError::NoEligibleRoute),
                };
            };

            let provider_id = claim.provider_id().to_owned();
            let Some(provider) = self.providers.get(&provider_id).cloned() else {
                claim.rollback_claim()?;
                return Err(StreamingCoordinatorError::MissingProvider { provider_id });
            };
            let profiles = self
                .provider_profiles
                .get(&provider_id)
                .cloned()
                .unwrap_or_default();
            if profiles.is_empty() {
                claim.rollback_claim()?;
                return Err(StreamingCoordinatorError::MissingWireProfile { provider_id });
            }

            let candidates = self.attempts.prepare_candidates(profiles, "static");
            let resolution = self.wire_resolver.resolve(
                &provider_id,
                claim.canonical_model_id(),
                candidates,
                Instant::now(),
            );
            let attempted = attempted_wires
                .entry(claim.account_name().to_owned())
                .or_default();
            let Some(candidate) = resolution
                .candidates
                .iter()
                .find(|candidate| !attempted.contains(&candidate.surface()))
                .cloned()
            else {
                claim.rollback_claim()?;
                excluded_accounts.insert(claim.account_name().to_owned());
                preferred_account = None;
                continue;
            };
            attempted.insert(candidate.surface());
            let alternate_wire_available = resolution
                .candidates
                .iter()
                .any(|candidate| !attempted.contains(&candidate.surface()));
            let upstream_protocol = protocol_for_surface(candidate.surface());
            let publication_input = PublicationInput::new(
                request.proxy_request_id.clone(),
                request.client_surface.protocol(),
                upstream_protocol,
                true,
                i64::from(attempt_number),
            );
            let published = match self.publication.publish(claim, publication_input).await? {
                PublicationOutcome::Published(value) => value,
                PublicationOutcome::AlreadyPublished(_) => {
                    return Err(StreamingCoordinatorError::Publication(
                        PublicationError::DuplicateConflict {
                            proxy_request_id: request.proxy_request_id,
                        },
                    ));
                }
            };
            last_identity = Some(published.identity.clone());
            let identity = published.identity.clone();
            let account_key = self
                .credentials
                .get(&identity.account_name)
                .map(str::to_owned);
            let attempt_input = AttemptInput {
                identity: identity.clone(),
                provider: provider.clone(),
                account_api_key: account_key,
                incoming_headers: request.incoming_headers.clone(),
                request_id: request.request_id.clone(),
                correlation_id: request.correlation_id.clone(),
                raw_body: request.raw_body.clone(),
                client_surface: request.client_surface,
                profile: candidate.profile.clone(),
                stream: true,
                candidate_fingerprint: resolution.fingerprint.clone(),
            };
            let prepared = match self
                .attempts
                .prepare_admitted(attempt_input, request.admitted.clone())
            {
                Ok(value) => value,
                Err(error) => {
                    let headers = StreamClientHeaders {
                        status: StatusCode::BAD_REQUEST,
                        headers: self.error_headers(&request.proxy_request_id, attempt_number),
                    };
                    let data = self.local_failure_data(
                        &identity,
                        &candidate.profile,
                        StatusCode::BAD_REQUEST,
                        "LocalPreparation",
                        request_bytes,
                    );
                    let _ = error;
                    return Ok(self.pending_terminal(
                        published.identity,
                        Some(published.claim),
                        headers,
                        Some(self.error_body(
                            request.client_surface,
                            "request could not be prepared for the selected provider",
                        )),
                        data,
                        StreamPhase::Closed,
                    ));
                }
            };

            let policy = self.policy_for(&identity.provider_id);
            // Phase 1: waiting for upstream headers under the M7 header timer.
            // Cancelling the submit future drops the in-flight M4 send; the
            // transport owns connection cleanup from there.
            let mut upstream = match policy.header_timeout {
                Some(limit) => match timeout(limit, self.attempts.submit_once(prepared)).await {
                    Ok(Ok(response)) => response,
                    Ok(Err(error)) => {
                        let error = self
                            .pre_handoff_transport_terminal(
                                &published,
                                &identity,
                                &candidate.profile,
                                attempt_number,
                                error,
                                "response_headers",
                                alternate_wire_available,
                                request_start,
                                request_bytes,
                                &request,
                                &mut attempt_number,
                                &mut preferred_account,
                                &mut excluded_accounts,
                                &mut last_failure,
                            )
                            .await?;
                        if let Some(execution) = error {
                            return Ok(execution);
                        }
                        continue;
                    }
                    Err(_) => {
                        let error = self
                            .pre_handoff_timeout_terminal(
                                &published,
                                &identity,
                                &candidate.profile,
                                attempt_number,
                                OUTCOME_RESPONSE_HEADER_TIMEOUT,
                                "ResponseHeaderTimeout",
                                "response_headers",
                                alternate_wire_available,
                                request_start,
                                request_bytes,
                                &request,
                                &mut attempt_number,
                                &mut preferred_account,
                                &mut excluded_accounts,
                                &mut last_failure,
                            )
                            .await?;
                        if let Some(execution) = error {
                            return Ok(execution);
                        }
                        continue;
                    }
                },
                None => match self.attempts.submit_once(prepared).await {
                    Ok(response) => response,
                    Err(error) => {
                        let error = self
                            .pre_handoff_transport_terminal(
                                &published,
                                &identity,
                                &candidate.profile,
                                attempt_number,
                                error,
                                "response_headers",
                                alternate_wire_available,
                                request_start,
                                request_bytes,
                                &request,
                                &mut attempt_number,
                                &mut preferred_account,
                                &mut excluded_accounts,
                                &mut last_failure,
                            )
                            .await?;
                        if let Some(execution) = error {
                            return Ok(execution);
                        }
                        continue;
                    }
                },
            };
            // Phase 2: upstream headers accepted, downstream not started.
            if upstream.status.as_u16() >= 400 {
                let body = match upstream
                    .body
                    .read_to_bytes(self.max_provider_body_bytes)
                    .await
                {
                    Ok(body) => body,
                    Err(error) => {
                        let error = self
                            .pre_handoff_transport_terminal(
                                &published,
                                &identity,
                                &candidate.profile,
                                attempt_number,
                                AttemptError::Transport(error),
                                "error_response_read",
                                alternate_wire_available,
                                request_start,
                                request_bytes,
                                &request,
                                &mut attempt_number,
                                &mut preferred_account,
                                &mut excluded_accounts,
                                &mut last_failure,
                            )
                            .await?;
                        if let Some(execution) = error {
                            return Ok(execution);
                        }
                        continue;
                    }
                };
                let context = self.response_context(
                    request.client_surface,
                    candidate.profile.clone(),
                    &identity,
                    &provider,
                );
                let decoded = self.wire.decode_finite_response(
                    &body,
                    upstream.status.as_u16(),
                    &context,
                    true,
                );
                match decoded {
                    Ok(decoded) => match decoded.outcome {
                        crate::wire::FiniteResponseOutcome::Success(_) => {
                            self.wire_resolver.accept(
                                &identity.provider_id,
                                &identity.model_id,
                                &resolution.fingerprint,
                                candidate.surface(),
                                Instant::now(),
                            );
                            self.router.record_success(&published.claim);
                            let client_body = decoded
                                .client_body
                                .as_ref()
                                .map(|body| body.bytes.clone())
                                .unwrap_or_else(|| Bytes::from_static(b"{}"));
                            let headers = self.client_headers(
                                upstream.status,
                                &upstream.headers,
                                &request.proxy_request_id,
                                attempt_number,
                                true,
                            );
                            let data = self.success_data(
                                &identity,
                                decoded.usage.as_ref(),
                                upstream.status,
                                upstream.headers_elapsed,
                                request_start.elapsed(),
                                request_bytes,
                                body.len(),
                                upstream.upstream_request_id,
                            );
                            return Ok(self.pending_terminal(
                                identity,
                                Some(published.claim),
                                headers,
                                Some(client_body),
                                data,
                                StreamPhase::Closed,
                            ));
                        }
                        crate::wire::FiniteResponseOutcome::ProviderError(error) => {
                            let signal = provider_error_signal(&error);
                            let observation = self.observation(
                                &identity,
                                &candidate.profile,
                                attempt_number,
                                FailureSource::ProviderResponse,
                                Some(upstream.status),
                                None,
                                signal,
                                alternate_wire_available,
                                "response_status",
                                false,
                            );
                            let (effects, first) = self.decide(&observation)?;
                            if first {
                                self.apply_effects(&published.claim, &effects);
                            }
                            if effects.wire_effect == "reject_candidate" {
                                self.wire_resolver.reject(
                                    &identity.provider_id,
                                    &identity.model_id,
                                    &resolution.fingerprint,
                                    candidate.surface(),
                                    Instant::now(),
                                );
                            }
                            if self.should_retry(&effects) {
                                last_upstream = Some(LastUpstream {
                                    status: upstream.status,
                                    headers: upstream.headers.clone(),
                                    body: body.clone(),
                                    effects: effects.clone(),
                                    upstream_request_id: upstream.upstream_request_id.clone(),
                                    headers_elapsed: upstream.headers_elapsed,
                                });
                                self.cleanup_failed_attempt(
                                    &published,
                                    self.retry_cleanup_data(
                                        &identity,
                                        upstream_protocol,
                                        &effects,
                                        Some(upstream.status),
                                        upstream.upstream_request_id.clone(),
                                        upstream.headers_elapsed,
                                        request_bytes,
                                        body.len(),
                                    ),
                                )
                                .await?;
                                self.prepare_next(
                                    &mut attempt_number,
                                    &mut preferred_account,
                                    &mut excluded_accounts,
                                    &identity,
                                    &effects,
                                );
                                continue;
                            }
                            let provider_bytes = body.len();
                            let mut headers = self.client_headers(
                                upstream.status,
                                &upstream.headers,
                                &request.proxy_request_id,
                                attempt_number,
                                true,
                            );
                            let upstream_fault =
                                matches!(upstream.status.as_u16(), 408 | 425 | 429 | 500..=599);
                            if upstream_fault
                                && attempt_number >= self.retry_policy.max_attempts.max(1)
                                && let Ok(value) = HeaderValue::try_from("attempt_ceiling_reached")
                            {
                                headers
                                    .headers
                                    .push((HeaderName::from_static("x-proxy-retry-reason"), value));
                            }
                            let mut data = self.failure_data(
                                &identity,
                                upstream_protocol,
                                &effects,
                                Some(upstream.status),
                                upstream.upstream_request_id,
                                upstream.headers_elapsed,
                                request_bytes,
                                provider_bytes,
                                false,
                            );
                            if !effects.retry && !upstream_fault {
                                data.outcome = FinalizationOutcome::ClientError;
                                data.release_reason = Some("capability_rejected".into());
                                data.retry_category = Some("never".into());
                                data.is_retry_outcome = false;
                            }
                            return Ok(self.pending_terminal(
                                identity,
                                Some(published.claim),
                                headers,
                                Some(body),
                                data,
                                StreamPhase::Closed,
                            ));
                        }
                        crate::wire::FiniteResponseOutcome::Malformed { .. } => {
                            let observation = self.observation(
                                &identity,
                                &candidate.profile,
                                attempt_number,
                                FailureSource::ProviderResponse,
                                Some(upstream.status),
                                Some(FailureCategory::Fatal),
                                None,
                                alternate_wire_available,
                                "response_decode",
                                false,
                            );
                            let (effects, first) = self.decide(&observation)?;
                            if first {
                                self.apply_effects(&published.claim, &effects);
                            }
                            let headers = StreamClientHeaders {
                                status: StatusCode::INTERNAL_SERVER_ERROR,
                                headers: self
                                    .error_headers(&request.proxy_request_id, attempt_number),
                            };
                            let data = self.local_failure_data(
                                &identity,
                                &candidate.profile,
                                StatusCode::INTERNAL_SERVER_ERROR,
                                "MalformedResponse",
                                request_bytes,
                            );
                            let _ = effects;
                            return Ok(self.pending_terminal(
                                identity,
                                Some(published.claim),
                                headers,
                                Some(self.error_body(
                                    request.client_surface,
                                    "provider returned a malformed error response",
                                )),
                                data,
                                StreamPhase::Closed,
                            ));
                        }
                    },
                    Err(error) => {
                        let headers = StreamClientHeaders {
                            status: StatusCode::INTERNAL_SERVER_ERROR,
                            headers: self.error_headers(&request.proxy_request_id, attempt_number),
                        };
                        let data = self.local_failure_data(
                            &identity,
                            &candidate.profile,
                            StatusCode::INTERNAL_SERVER_ERROR,
                            "ResponseAdaptation",
                            request_bytes,
                        );
                        let _ = error;
                        return Ok(self.pending_terminal(
                            published.identity,
                            Some(published.claim),
                            headers,
                            Some(self.error_body(
                                request.client_surface,
                                "provider response could not be adapted for the client",
                            )),
                            data,
                            StreamPhase::Closed,
                        ));
                    }
                }
            }

            // Phase 3: waiting for the first provider body byte under the M7
            // first-byte timer. An empty EOF here still returns a live
            // execution; the body driver classifies it as `empty_eof` after
            // handoff, exactly like the Python generator path.
            let first_byte_start = Instant::now();
            enum PrefetchOutcome {
                Ready {
                    chunk: Option<Bytes>,
                    first_byte_elapsed: Option<Duration>,
                },
                Retry,
                Terminal(Box<StreamingExecution>),
            }
            let prefetch = {
                let pending_body = &mut upstream.body;
                let mut outcome = PrefetchOutcome::Ready {
                    chunk: None,
                    first_byte_elapsed: None,
                };
                loop {
                    if policy
                        .first_byte_timeout
                        .is_some_and(|limit| first_byte_start.elapsed() >= limit)
                    {
                        match self
                            .pre_handoff_timeout_terminal(
                                &published,
                                &identity,
                                &candidate.profile,
                                attempt_number,
                                OUTCOME_FIRST_BYTE_TIMEOUT,
                                "FirstByteTimeout",
                                "first_byte_prefetch",
                                alternate_wire_available,
                                request_start,
                                request_bytes,
                                &request,
                                &mut attempt_number,
                                &mut preferred_account,
                                &mut excluded_accounts,
                                &mut last_failure,
                            )
                            .await?
                        {
                            None => {
                                outcome = PrefetchOutcome::Retry;
                                break;
                            }
                            Some(execution) => {
                                outcome = PrefetchOutcome::Terminal(Box::new(execution));
                                break;
                            }
                        }
                    }
                    let next = match policy.first_byte_timeout {
                        Some(limit) => {
                            let remaining = limit.saturating_sub(first_byte_start.elapsed());
                            match timeout(remaining, pending_body.next()).await {
                                Ok(value) => value,
                                Err(_) => continue,
                            }
                        }
                        None => pending_body.next().await,
                    };
                    match next {
                        None => break,
                        Some(Err(error)) => {
                            match self
                                .pre_handoff_transport_terminal(
                                    &published,
                                    &identity,
                                    &candidate.profile,
                                    attempt_number,
                                    AttemptError::Transport(error),
                                    "first_byte_prefetch",
                                    alternate_wire_available,
                                    request_start,
                                    request_bytes,
                                    &request,
                                    &mut attempt_number,
                                    &mut preferred_account,
                                    &mut excluded_accounts,
                                    &mut last_failure,
                                )
                                .await?
                            {
                                None => {
                                    outcome = PrefetchOutcome::Retry;
                                    break;
                                }
                                Some(execution) => {
                                    outcome = PrefetchOutcome::Terminal(Box::new(execution));
                                    break;
                                }
                            }
                        }
                        Some(Ok(chunk)) if chunk.is_empty() => continue,
                        Some(Ok(chunk)) => {
                            outcome = PrefetchOutcome::Ready {
                                chunk: Some(chunk),
                                first_byte_elapsed: Some(request_start.elapsed()),
                            };
                            break;
                        }
                    }
                }
                outcome
            };
            let (prefetched, first_byte_elapsed) = match prefetch {
                PrefetchOutcome::Retry => continue,
                PrefetchOutcome::Terminal(execution) => return Ok(*execution),
                PrefetchOutcome::Ready {
                    chunk,
                    first_byte_elapsed,
                } => (chunk, first_byte_elapsed),
            };

            let context = self.response_context(
                request.client_surface,
                candidate.profile.clone(),
                &identity,
                &provider,
            );
            let wire_stream = match self.wire.stream(&context) {
                Ok(stream) => stream,
                Err(error) => {
                    let headers = StreamClientHeaders {
                        status: StatusCode::INTERNAL_SERVER_ERROR,
                        headers: self.error_headers(&request.proxy_request_id, attempt_number),
                    };
                    let data = self.local_failure_data(
                        &identity,
                        &candidate.profile,
                        StatusCode::INTERNAL_SERVER_ERROR,
                        "StreamUnavailable",
                        request_bytes,
                    );
                    let _ = error;
                    return Ok(self.pending_terminal(
                        published.identity,
                        Some(published.claim),
                        headers,
                        Some(self.error_body(
                            request.client_surface,
                            "streaming is unavailable for the selected provider",
                        )),
                        data,
                        StreamPhase::Closed,
                    ));
                }
            };
            let passthrough = !is_event_stream(&upstream.headers);
            let prefetched_len = prefetched.as_ref().map_or(0, Bytes::len);
            let headers = self.client_headers(
                upstream.status,
                &upstream.headers,
                &request.proxy_request_id,
                attempt_number,
                passthrough,
            );
            let completion_policy = self.completion_policy_for(&identity.provider_id);
            let transcoded = identity.client_protocol != protocol_for_surface(candidate.surface());
            let data = self.stream_open_data(
                upstream_protocol,
                transcoded,
                request_bytes,
                prefetched_len,
                first_byte_elapsed,
                upstream.upstream_request_id.clone(),
                request_start.elapsed(),
            );
            return Ok(self.pending_stream(
                identity,
                Some(published.claim),
                headers,
                data,
                ActiveStream {
                    body: Some(upstream.body),
                    wire: if passthrough { None } else { Some(wire_stream) },
                    pending_raw: prefetched,
                    pending_client: None,
                    idle_timeout: policy.idle_timeout,
                    provider_bytes: prefetched_len,
                    client_bytes: 0,
                    events_forwarded: 0,
                    malformed_chunks: 0,
                    saw_terminal_event: false,
                    completion_policy,
                    first_byte_elapsed,
                },
                AttemptStreamFacts {
                    attempt_number,
                    wire_surface: candidate.surface(),
                    candidate_fingerprint: resolution.fingerprint.clone(),
                    transcoded,
                    upstream_protocol: upstream_protocol.to_owned(),
                    request_bytes,
                },
                request_start,
            ));
        }
    }

    /// Pre-handoff transport failure: classify through C005, retry with
    /// failed-attempt cleanup or converge a terminal 502 error execution.
    /// Returns `Ok(None)` when the caller must `continue` the retry loop.
    #[allow(clippy::too_many_arguments)]
    async fn pre_handoff_transport_terminal(
        &self,
        published: &super::PublishedAttempt,
        identity: &FinalizationIdentity,
        profile: &ConfiguredWireProfile,
        attempt_number: u32,
        error: AttemptError,
        dispatch_phase: &str,
        alternate_wire_available: bool,
        request_start: Instant,
        request_bytes: usize,
        request: &StreamRequest,
        next_attempt: &mut u32,
        preferred_account: &mut Option<String>,
        excluded_accounts: &mut BTreeSet<String>,
        last_failure: &mut Option<(FailureEffects, Option<StatusCode>, String)>,
    ) -> Result<Option<StreamingExecution>, StreamingCoordinatorError> {
        let source = match &error {
            AttemptError::Transport(_) => FailureSource::Transport,
            _ => FailureSource::LocalPreparation,
        };
        let observation = self.observation(
            identity,
            profile,
            attempt_number,
            source,
            None,
            None,
            None,
            alternate_wire_available,
            dispatch_phase,
            false,
        );
        let (effects, first) = self.decide(&observation)?;
        *last_failure = Some((effects.clone(), None, identity.upstream_protocol.clone()));
        if first {
            self.apply_effects(&published.claim, &effects);
        }
        if self.should_retry(&effects) {
            self.cleanup_failed_attempt(
                published,
                self.retry_cleanup_data(
                    identity,
                    &identity.upstream_protocol,
                    &effects,
                    None,
                    None,
                    request_start.elapsed(),
                    request_bytes,
                    0,
                ),
            )
            .await?;
            self.prepare_next(
                next_attempt,
                preferred_account,
                excluded_accounts,
                identity,
                &effects,
            );
            return Ok(None);
        }
        let status = if source == FailureSource::LocalPreparation {
            StatusCode::BAD_REQUEST
        } else {
            StatusCode::BAD_GATEWAY
        };
        let headers = StreamClientHeaders {
            status,
            headers: self.error_headers(&request.proxy_request_id, attempt_number),
        };
        let data = self.failure_data(
            identity,
            &identity.upstream_protocol,
            &effects,
            None,
            None,
            request_start.elapsed(),
            request_bytes,
            0,
            false,
        );
        let _ = error;
        Ok(Some(self.pending_terminal(
            identity.clone(),
            Some(published.claim.clone()),
            headers,
            Some(self.error_body(request.client_surface, effects.client_outcome.as_str())),
            data,
            StreamPhase::Closed,
        )))
    }

    /// Pre-handoff M7 timer expiration: same retry/terminal shape as transport
    /// failures, with the timeout outcome recorded in diagnostics.
    #[allow(clippy::too_many_arguments)]
    async fn pre_handoff_timeout_terminal(
        &self,
        published: &super::PublishedAttempt,
        identity: &FinalizationIdentity,
        profile: &ConfiguredWireProfile,
        attempt_number: u32,
        outcome: &'static str,
        error_class: &str,
        dispatch_phase: &str,
        alternate_wire_available: bool,
        request_start: Instant,
        request_bytes: usize,
        request: &StreamRequest,
        next_attempt: &mut u32,
        preferred_account: &mut Option<String>,
        excluded_accounts: &mut BTreeSet<String>,
        last_failure: &mut Option<(FailureEffects, Option<StatusCode>, String)>,
    ) -> Result<Option<StreamingExecution>, StreamingCoordinatorError> {
        self.record_outcome(outcome, attempt_number, 0, request_start.elapsed());
        let observation = self.observation(
            identity,
            profile,
            attempt_number,
            FailureSource::Transport,
            None,
            None,
            None,
            alternate_wire_available,
            dispatch_phase,
            false,
        );
        let (effects, first) = self.decide(&observation)?;
        *last_failure = Some((effects.clone(), None, identity.upstream_protocol.clone()));
        if first {
            self.apply_effects(&published.claim, &effects);
        }
        if self.should_retry(&effects) {
            self.cleanup_failed_attempt(
                published,
                self.retry_cleanup_data(
                    identity,
                    &identity.upstream_protocol,
                    &effects,
                    None,
                    None,
                    request_start.elapsed(),
                    request_bytes,
                    0,
                ),
            )
            .await?;
            self.prepare_next(
                next_attempt,
                preferred_account,
                excluded_accounts,
                identity,
                &effects,
            );
            return Ok(None);
        }
        let headers = StreamClientHeaders {
            status: StatusCode::BAD_GATEWAY,
            headers: self.error_headers(&request.proxy_request_id, attempt_number),
        };
        let mut data = self.failure_data(
            identity,
            &identity.upstream_protocol,
            &effects,
            None,
            None,
            request_start.elapsed(),
            request_bytes,
            0,
            false,
        );
        data.error_class = Some(error_class.to_owned());
        data.error_detail = Some(outcome.to_owned());
        Ok(Some(self.pending_terminal(
            identity.clone(),
            Some(published.claim.clone()),
            headers,
            Some(self.error_body(request.client_surface, outcome)),
            data,
            StreamPhase::Closed,
        )))
    }

    fn response_context(
        &self,
        client_surface: ClientSurface,
        profile: ConfiguredWireProfile,
        identity: &FinalizationIdentity,
        provider: &ProviderConfig,
    ) -> WireRuntimeContext {
        let mut context = WireRuntimeContext::new(
            client_surface,
            profile,
            identity.model_id.clone(),
            identity.upstream_model_id.clone(),
        );
        context.provider_id = Some(identity.provider_id.clone());
        context.provider_kind = provider.kind.clone();
        context.max_provider_body_bytes = self.max_provider_body_bytes;
        context
    }

    #[allow(clippy::too_many_arguments)]
    fn observation(
        &self,
        identity: &FinalizationIdentity,
        profile: &ConfiguredWireProfile,
        attempt_number: u32,
        source: FailureSource,
        status: Option<StatusCode>,
        category_hint: Option<FailureCategory>,
        signal: Option<&str>,
        alternate_wire_available: bool,
        dispatch_phase: &str,
        downstream_started: bool,
    ) -> FailureObservation {
        let mut observation = FailureObservation::response(
            identity.attempt_id,
            attempt_number,
            status.unwrap_or(StatusCode::INTERNAL_SERVER_ERROR),
        );
        observation.source = source;
        observation.status = status.map(|value| value.as_u16());
        observation.category_hint = category_hint;
        observation.response_started = downstream_started;
        observation.downstream_started = downstream_started;
        observation.wire_rejection = signal.is_some();
        observation.provider_id = Some(identity.provider_id.clone());
        observation.account_name = Some(identity.account_name.clone());
        observation.model_id = Some(identity.model_id.clone());
        observation.upstream_model_id = Some(identity.upstream_model_id.clone());
        observation.client_protocol = identity.client_protocol.clone();
        observation.upstream_protocol = identity.upstream_protocol.clone();
        observation.wire_surface = Some(profile.definition.surface.as_str().into());
        observation.candidate_fingerprint = None;
        observation.transport_phase = Some(dispatch_phase.into());
        observation.error_class = Some(dispatch_phase.into());
        observation.alternate_wire_available = alternate_wire_available;
        observation.credential_configured = true;
        observation.dispatch_phase = dispatch_phase.into();
        if let Some(signal) = signal {
            observation = observation.signal(signal);
        }
        observation
    }

    fn decide(
        &self,
        observation: &FailureObservation,
    ) -> Result<(FailureEffects, bool), StreamingCoordinatorError> {
        self.failure_engine
            .lock()
            .expect("streaming failure engine lock")
            .decide(observation)
            .map_err(|error| StreamingCoordinatorError::Effects(error.to_string()))
    }

    fn apply_effects(&self, claim: &SelectionClaim, effects: &FailureEffects) {
        self.router.apply_failure_effects(
            claim,
            effects.apply_account_penalty,
            effects.quarantine_model,
            &effects.model_effect,
            effects.backoff_reason.as_deref(),
            effects.backoff_until,
            effects.circuit_penalty,
        );
    }

    fn should_retry(&self, effects: &FailureEffects) -> bool {
        effects.retry
            && matches!(
                effects.action,
                super::NextAction::RetryAccount | super::NextAction::RetryWire
            )
    }

    fn prepare_next(
        &self,
        attempt_number: &mut u32,
        preferred_account: &mut Option<String>,
        excluded_accounts: &mut BTreeSet<String>,
        identity: &FinalizationIdentity,
        effects: &FailureEffects,
    ) {
        *attempt_number = (*attempt_number).saturating_add(1);
        if effects.retry_scope == super::RetryScope::Wire {
            *preferred_account = Some(identity.account_name.clone());
        } else {
            excluded_accounts.insert(identity.account_name.clone());
            *preferred_account = None;
        }
    }

    async fn cleanup_failed_attempt(
        &self,
        published: &super::PublishedAttempt,
        data: FinalizationData,
    ) -> Result<FinalizationResult, StreamingCoordinatorError> {
        let handle = self
            .finalization
            .register(FinalizationCommand::FailedAttempt {
                identity: published.identity.clone(),
                data,
                claim: Some(published.claim.clone()),
            })?;
        Ok(handle.wait().await?)
    }

    #[allow(clippy::too_many_arguments)]
    fn pending_terminal(
        &self,
        identity: FinalizationIdentity,
        claim: Option<SelectionClaim>,
        headers: StreamClientHeaders,
        error_body: Option<Bytes>,
        data: FinalizationData,
        phase: StreamPhase,
    ) -> StreamingExecution {
        StreamingExecution {
            headers,
            error_body,
            facts: AttemptStreamFacts::terminal(),
            completion: PendingStreamFinalization::new(
                self.finalization.clone(),
                identity,
                claim,
                data,
                phase,
                None,
                self.diagnostics.clone(),
            ),
            router: self.router.clone(),
            wire_resolver: self.wire_resolver.clone(),
            failure_engine: Arc::clone(&self.failure_engine),
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn pending_stream(
        &self,
        identity: FinalizationIdentity,
        claim: Option<SelectionClaim>,
        headers: StreamClientHeaders,
        data: FinalizationData,
        stream: ActiveStream,
        facts: AttemptStreamFacts,
        started_at: Instant,
    ) -> StreamingExecution {
        StreamingExecution {
            headers,
            error_body: None,
            facts,
            completion: PendingStreamFinalization::new(
                self.finalization.clone(),
                identity,
                claim,
                data,
                StreamPhase::DownstreamPending,
                Some((stream, started_at)),
                self.diagnostics.clone(),
            ),
            router: self.router.clone(),
            wire_resolver: self.wire_resolver.clone(),
            failure_engine: Arc::clone(&self.failure_engine),
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn success_data(
        &self,
        identity: &FinalizationIdentity,
        usage: Option<&CanonicalUsage>,
        status: StatusCode,
        headers_elapsed: Duration,
        total_elapsed: Duration,
        request_bytes: usize,
        provider_bytes: usize,
        upstream_request_id: Option<String>,
    ) -> FinalizationData {
        let usage = usage.cloned().unwrap_or_default();
        FinalizationData {
            outcome: FinalizationOutcome::Completed,
            status_code: Some(status.as_u16()),
            input_tokens: bounded_i64(usage.input_tokens),
            output_tokens: bounded_i64(usage.output_tokens),
            cache_read_tokens: bounded_i64(usage.cache_read_input_tokens),
            cache_write_tokens: bounded_i64(
                usage
                    .cache_write_input_tokens
                    .or(usage.cache_creation_input_tokens),
            ),
            reasoning_tokens: bounded_i64(usage.reasoning_tokens),
            // M6 CanonicalUsage carries no cost provenance; leave cost unset
            // rather than fabricating a zero estimate.
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
            transcoded: identity.client_protocol != identity.upstream_protocol,
            upstream_protocol: Some(identity.upstream_protocol.clone()),
            latency_ms: duration_i64(total_elapsed),
            first_byte_ms: Some(duration_i64(headers_elapsed)),
            bytes_received: bounded_usize(request_bytes),
            bytes_emitted: bounded_usize(provider_bytes),
            upstream_request_id: bounded_request_id(upstream_request_id),
            release_reason: Some("completed".into()),
            ..FinalizationData::default()
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn failure_data(
        &self,
        identity: &FinalizationIdentity,
        upstream_protocol: &str,
        effects: &FailureEffects,
        status: Option<StatusCode>,
        upstream_request_id: Option<String>,
        elapsed: Duration,
        request_bytes: usize,
        provider_bytes: usize,
        downstream_started: bool,
    ) -> FinalizationData {
        FinalizationData {
            outcome: FinalizationOutcome::UpstreamError,
            status_code: status.map(|value| value.as_u16()),
            cost_microdollars: 0,
            cache_counter_status: Some("not_reported".to_owned()),
            latency_ms: duration_i64(elapsed),
            first_byte_ms: Some(duration_i64(elapsed)),
            bytes_received: bounded_usize(request_bytes),
            bytes_emitted: bounded_usize(provider_bytes),
            upstream_request_id: bounded_request_id(upstream_request_id),
            error_class: Some(effects.evidence_class.clone()),
            error_detail: Some(effects.client_outcome.clone()),
            release_reason: Some("attempt_failed".into()),
            retry_category: Some(category_label(effects.category).into()),
            is_retry_outcome: effects.retry,
            downstream_started,
            transcoded: identity.client_protocol != upstream_protocol,
            upstream_protocol: Some(upstream_protocol.into()),
            ..FinalizationData::default()
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn retry_cleanup_data(
        &self,
        identity: &FinalizationIdentity,
        upstream_protocol: &str,
        effects: &FailureEffects,
        status: Option<StatusCode>,
        upstream_request_id: Option<String>,
        elapsed: Duration,
        request_bytes: usize,
        provider_bytes: usize,
    ) -> FinalizationData {
        let mut data = self.failure_data(
            identity,
            upstream_protocol,
            effects,
            status,
            upstream_request_id,
            elapsed,
            request_bytes,
            provider_bytes,
            false,
        );
        data.release_reason = Some("attempt_retryable".into());
        data
    }

    fn local_failure_data(
        &self,
        identity: &FinalizationIdentity,
        profile: &ConfiguredWireProfile,
        status: StatusCode,
        error_class: &str,
        request_bytes: usize,
    ) -> FinalizationData {
        FinalizationData {
            outcome: FinalizationOutcome::ClientError,
            status_code: Some(status.as_u16()),
            error_class: Some(error_class.into()),
            cache_counter_status: Some("not_reported".to_owned()),
            release_reason: Some("capability_rejected".into()),
            bytes_received: bounded_usize(request_bytes),
            bytes_emitted: 0,
            transcoded: identity.client_protocol
                != protocol_for_surface(profile.definition.surface),
            upstream_protocol: Some(protocol_for_surface(profile.definition.surface).into()),
            ..FinalizationData::default()
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn stream_open_data(
        &self,
        upstream_protocol: &str,
        transcoded: bool,
        request_bytes: usize,
        prefetched_len: usize,
        first_byte_elapsed: Option<Duration>,
        upstream_request_id: Option<String>,
        elapsed: Duration,
    ) -> FinalizationData {
        FinalizationData {
            // Placeholder until the body driver stores the natural terminal.
            // Dropping or completing before then finalizes from this progress
            // as interrupted/cancelled, never as success.
            outcome: FinalizationOutcome::Interrupted,
            status_code: Some(StatusCode::OK.as_u16()),
            cost_microdollars: 0,
            cache_counter_status: Some("not_reported".to_owned()),
            latency_ms: duration_i64(elapsed),
            first_byte_ms: first_byte_elapsed.map(duration_i64),
            bytes_received: bounded_usize(request_bytes),
            bytes_emitted: bounded_usize(prefetched_len),
            upstream_request_id: bounded_request_id(upstream_request_id),
            error_class: Some("StreamNotTerminated".into()),
            release_reason: Some("pending_response_dropped".into()),
            downstream_started: false,
            transcoded,
            upstream_protocol: Some(upstream_protocol.into()),
            ..FinalizationData::default()
        }
    }

    fn client_headers(
        &self,
        status: StatusCode,
        headers: &HeaderMap,
        proxy_request_id: &str,
        attempt_number: u32,
        passthrough: bool,
    ) -> StreamClientHeaders {
        let mut filtered = filter_response_headers(headers);
        if !filtered
            .iter()
            .any(|(name, _)| name == http::header::CONTENT_TYPE)
        {
            filtered.push((
                http::header::CONTENT_TYPE,
                HeaderValue::from_static(if passthrough {
                    "application/json"
                } else {
                    "text/event-stream"
                }),
            ));
        }
        if let Ok(value) = HeaderValue::try_from(proxy_request_id) {
            filtered.push((HeaderName::from_static("x-proxy-request-id"), value));
        }
        if let Ok(value) = HeaderValue::try_from(attempt_number.to_string()) {
            filtered.push((HeaderName::from_static("x-proxy-attempt-count"), value));
        }
        StreamClientHeaders {
            status,
            headers: filtered,
        }
    }

    fn error_headers(&self, proxy_request_id: &str, attempt_number: u32) -> ClientResponseHeaders {
        let mut headers = ClientResponseHeaders::new();
        headers.push((
            http::header::CONTENT_TYPE,
            HeaderValue::from_static("application/json"),
        ));
        if let Ok(value) = HeaderValue::try_from(proxy_request_id) {
            headers.push((HeaderName::from_static("x-proxy-request-id"), value));
        }
        if let Ok(value) = HeaderValue::try_from(attempt_number.to_string()) {
            headers.push((HeaderName::from_static("x-proxy-attempt-count"), value));
        }
        headers
    }

    fn error_body(&self, client_surface: ClientSurface, message: &str) -> Bytes {
        let value = if client_surface == ClientSurface::Messages {
            json!({
                "type": "error",
                "error": {"type": "api_error", "message": message}
            })
        } else {
            json!({
                "error": {"message": message, "type": "upstream_error"}
            })
        };
        let body = serde_json::to_vec(&value)
            .unwrap_or_else(|_| b"{\"error\":{\"type\":\"upstream_error\"}}".to_vec());
        Bytes::from(
            body.into_iter()
                .take(MAX_CLIENT_ERROR_BYTES)
                .collect::<Vec<u8>>(),
        )
    }
}

// ---------------------------------------------------------------------------
// Live execution: incremental body driver with retained finalization
// ---------------------------------------------------------------------------

/// Facts about the selected attempt needed by the post-handoff body driver.
#[derive(Debug, Clone)]
struct AttemptStreamFacts {
    attempt_number: u32,
    wire_surface: WireSurface,
    candidate_fingerprint: String,
    transcoded: bool,
    upstream_protocol: String,
    request_bytes: usize,
}

impl AttemptStreamFacts {
    fn terminal() -> Self {
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
struct ActiveStream {
    body: Option<crate::providers::ProviderBody>,
    wire: Option<WireStream>,
    pending_raw: Option<Bytes>,
    pending_client: Option<Bytes>,
    idle_timeout: Option<Duration>,
    provider_bytes: usize,
    client_bytes: usize,
    events_forwarded: u64,
    malformed_chunks: usize,
    saw_terminal_event: bool,
    completion_policy: String,
    first_byte_elapsed: Option<Duration>,
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
    facts: AttemptStreamFacts,
    completion: PendingStreamFinalization,
    router: RoutingRouter,
    wire_resolver: WireResolver,
    failure_engine: Arc<Mutex<FailureDecisionEngine>>,
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

fn store_idle_timeout(
    parts: &mut PendingStreamFinalizationParts,
    facts: &AttemptStreamFacts,
    router: &RoutingRouter,
    engine: &Mutex<FailureDecisionEngine>,
) -> Option<Result<Bytes, StreamChunkError>> {
    let observation = transport_observation(parts, facts, "stream_idle");
    let (effects, first) = match decide(engine, &observation) {
        Ok(value) => value,
        Err(_) => return store_local_midstream(parts, facts, "StreamIdleTimeout"),
    };
    if first {
        apply_effects(router, parts, &effects);
    }
    parts.diagnostics_record(
        OUTCOME_IDLE_TIMEOUT,
        facts.attempt_number,
        parts.stream_bytes(),
        parts.elapsed(),
    );
    parts.diagnostics_record(
        OUTCOME_UPSTREAM_MIDSTREAM_ERROR,
        facts.attempt_number,
        parts.stream_bytes(),
        parts.elapsed(),
    );
    let mut data = effects_midstream_data(parts, facts, &effects, "StreamIdleTimeout");
    data.error_detail = Some(OUTCOME_IDLE_TIMEOUT.to_owned());
    store_terminal(parts, data, StreamChunkError::IdleTimeout)
}

fn store_midstream_transport(
    parts: &mut PendingStreamFinalizationParts,
    facts: &AttemptStreamFacts,
    router: &RoutingRouter,
    engine: &Mutex<FailureDecisionEngine>,
) -> Option<Result<Bytes, StreamChunkError>> {
    let observation = transport_observation(parts, facts, "stream_body");
    let (effects, first) = match decide(engine, &observation) {
        Ok(value) => value,
        Err(_) => return store_local_midstream(parts, facts, "UpstreamTransport"),
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

fn store_translation_error(
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
        Err(_) => return store_local_midstream(parts, facts, "StreamTranslation"),
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
) -> Option<Result<Bytes, StreamChunkError>> {
    let data = local_midstream_data(parts, facts, error_class);
    parts.diagnostics_record(
        OUTCOME_UPSTREAM_MIDSTREAM_ERROR,
        facts.attempt_number,
        parts.stream_bytes(),
        parts.elapsed(),
    );
    store_terminal(parts, data, StreamChunkError::UpstreamTransport)
}

fn store_eof(
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
        match wire.finalize() {
            Ok(finalization) => {
                tail_usage = finalization.usage.clone();
                for event in &finalization.events {
                    if matches!(
                        event.event_type,
                        CanonicalEventType::ResponseComplete
                            | CanonicalEventType::ResponseIncomplete
                            | CanonicalEventType::Error
                    ) {
                        stream.saw_terminal_event = true;
                    }
                    if let Ok(bytes) = wire.encode_client_event(event)
                        && !bytes.is_empty()
                    {
                        stream.events_forwarded = stream.events_forwarded.saturating_add(1);
                        tail_bytes.extend_from_slice(&bytes);
                    }
                }
                if !tail_bytes.is_empty() {
                    stream.client_bytes = stream.client_bytes.saturating_add(tail_bytes.len());
                }
                Some(finalization.terminal.clone())
            }
            Err(_) => None,
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
    // SSE completion rules apply to event-stream responses only.
    let classification = if is_passthrough {
        StreamEofClass::Complete
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
    observation.error_class = Some(dispatch_phase.into());
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
        .expect("streaming failure engine lock")
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
        status_code: Some(StatusCode::OK.as_u16()),
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
        bytes_emitted: bounded_usize(parts.stream_bytes()),
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
        status_code: Some(StatusCode::OK.as_u16()),
        cost_microdollars: 0,
        cache_counter_status: Some("not_reported".to_owned()),
        latency_ms: duration_i64(parts.elapsed()),
        first_byte_ms: parts.first_byte_ms(),
        bytes_received: bounded_usize(facts.request_bytes),
        bytes_emitted: bounded_usize(parts.stream_bytes()),
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
        bytes_emitted: bounded_usize(parts.stream_bytes()),
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

struct PendingStreamFinalization {
    parts: Option<PendingStreamFinalizationParts>,
}

struct PendingStreamFinalizationParts {
    supervisor: FinalizationSupervisor,
    identity: FinalizationIdentity,
    claim: Option<SelectionClaim>,
    data: FinalizationData,
    handoff: ResponseHandoffState,
    stream: Option<ActiveStream>,
    started_at: Instant,
    terminal_stored: bool,
    exhausted: bool,
    phase: StreamPhase,
    diagnostics: Arc<Mutex<StreamDiagnostics>>,
}

impl PendingStreamFinalization {
    #[allow(clippy::too_many_arguments)]
    fn new(
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
    fn elapsed(&self) -> Duration {
        self.started_at.elapsed()
    }

    fn stream_bytes(&self) -> usize {
        self.stream
            .as_ref()
            .map(|stream| stream.provider_bytes)
            .unwrap_or_else(|| self.data.bytes_emitted.max(0) as usize)
    }

    fn malformed_count(&self) -> usize {
        self.stream
            .as_ref()
            .map(|stream| stream.malformed_chunks)
            .unwrap_or(0)
    }

    fn first_byte_ms(&self) -> Option<i64> {
        self.stream
            .as_ref()
            .and_then(|stream| stream.first_byte_elapsed)
            .map(duration_i64)
            .or(self.data.first_byte_ms)
    }

    fn upstream_request_id(&self) -> Option<String> {
        self.data.upstream_request_id.clone()
    }

    fn midstream_usage(&self) -> Option<CanonicalUsage> {
        self.stream
            .as_ref()
            .and_then(|stream| stream.wire.as_ref())
            .and_then(|wire| wire.usage())
    }

    fn attempt_number(&self) -> u32 {
        u32::try_from(self.identity.attempt_number.max(1)).unwrap_or(1)
    }

    fn release_transport(&mut self) {
        if let Some(stream) = self.stream.as_mut() {
            stream.body = None;
        }
    }

    fn diagnostics_record(
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
        if !parts.terminal_stored {
            if let Some(usage) = parts.midstream_usage() {
                parts.data.input_tokens = bounded_i64(usage.input_tokens);
                parts.data.output_tokens = bounded_i64(usage.output_tokens);
                parts.data.cache_counter_status =
                    Some(cache_status(usage.cache_counter_status).into());
            }
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
        handle.spawn(async move {
            if let Ok(handle) = supervisor.register(command) {
                let _ = handle.wait().await;
            }
        });
    }
}

// ---------------------------------------------------------------------------
// EOF classification and shared helpers
// ---------------------------------------------------------------------------

/// Python `classify_stream_eof` outcome vocabulary, mapped from the closed M6
/// terminal summary. EOF is never automatically success.
#[derive(Debug, Clone, PartialEq, Eq)]
enum StreamEofClass {
    Complete,
    Compatibility,
    TerminalFailure,
    TerminalIncomplete,
    EmptyEof,
    PrematureEof,
    MalformedEof,
}

fn classify_eof(
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

fn completion_compat_allowed(policy: &str) -> bool {
    matches!(policy, "compatible" | "permissive_observe")
}

fn is_event_stream(headers: &HeaderMap) -> bool {
    headers
        .get(http::header::CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .is_some_and(|value| value.to_ascii_lowercase().contains("text/event-stream"))
}

fn protocol_for_surface(surface: WireSurface) -> &'static str {
    match surface {
        WireSurface::AnthropicMessages => "anthropic",
        WireSurface::GeminiInteractions | WireSurface::GeminiGenerateContent => "gemini",
        WireSurface::OpenaiChatCompletions | WireSurface::OpenaiResponses => "openai",
    }
}

fn provider_error_signal(error: &ProviderErrorEvidence) -> Option<&'static str> {
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

fn bounded_request_id(value: Option<String>) -> Option<String> {
    value.map(|value| {
        let mut bounded: String = value.chars().take(128).collect();
        bounded.retain(|character| !character.is_control());
        bounded
    })
}

fn bounded_i64(value: Option<u64>) -> i64 {
    value
        .unwrap_or(0)
        .min(i64::MAX as u64)
        .try_into()
        .unwrap_or(i64::MAX)
}

fn bounded_usize(value: usize) -> i64 {
    value.min(i64::MAX as usize) as i64
}

fn duration_i64(value: Duration) -> i64 {
    value.as_millis().min(i64::MAX as u128) as i64
}

fn cache_status(status: CacheCounterStatus) -> &'static str {
    match status {
        CacheCounterStatus::Reported => "reported",
        CacheCounterStatus::NotReported => "not_reported",
        CacheCounterStatus::UnknownFormat => "unknown_format",
    }
}

fn category_label(category: FailureCategory) -> &'static str {
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
