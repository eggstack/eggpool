//! Pre-handoff route, dispatch, timeout, retry, and response-handoff logic.

// ---------------------------------------------------------------------------
// Coordinator
// ---------------------------------------------------------------------------

use std::{
    collections::{BTreeMap, BTreeSet},
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};

use bytes::Bytes;
use http::{HeaderMap, HeaderName, HeaderValue, StatusCode};
use serde_json::json;
use tokio::time::timeout;

const MAX_CLIENT_ERROR_BYTES: usize = 512;

use crate::{
    accounts::CredentialStore,
    config::ProviderConfig,
    routing::{RoutingRouter, SelectionClaim},
    wire::ir::{CanonicalUsage, ClientSurface},
    wire::{ConfiguredWireProfile, WireRuntime, WireRuntimeContext, WireSurface},
};

use super::{
    ActiveStream, AttemptStreamFacts, LastUpstream, OUTCOME_FIRST_BYTE_TIMEOUT,
    OUTCOME_RESPONSE_HEADER_TIMEOUT, PendingStreamFinalization, StreamClientHeaders,
    StreamDiagnostics, StreamDiagnosticsSnapshot, StreamPhase, StreamRequest, StreamTimeoutPolicy,
    StreamingCoordinatorError, StreamingExecution, bounded_i64, bounded_request_id, bounded_usize,
    cache_status, category_label, duration_i64, is_event_stream, protocol_for_surface,
    provider_error_signal,
};
use crate::coordinator::{
    AttemptBuilder, AttemptError, AttemptPreparation, ClientResponseHeaders, FailureCategory,
    FailureDecisionEngine, FailureEffects, FailureObservation, FailureSource, FinalizationCommand,
    FinalizationData, FinalizationIdentity, FinalizationOutcome, FinalizationResult,
    FinalizationSupervisor, NextAction, PublicationError, PublicationInput, PublicationOutcome,
    PublicationService, PublishedAttempt, RetryPolicy, RetryScope, WireResolver,
    filter_response_headers,
};

/// End-to-end streaming coordinator for one immutable runtime generation.
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
            let attempt_input = AttemptPreparation {
                identity: &identity,
                provider: &provider,
                account_api_key: self.credentials.get(&identity.account_name),
                incoming_headers: &request.incoming_headers,
                request_id: request.request_id.as_deref(),
                correlation_id: request.correlation_id.as_deref(),
                raw_body: &request.raw_body,
                client_surface: request.client_surface,
                profile: &candidate.profile,
                stream: true,
                candidate_fingerprint: &resolution.fingerprint,
            };
            let prepared = match self
                .attempts
                .prepare_borrowed(attempt_input, &request.admitted)
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
                let decoded = self.wire.decode_finite_response_for_request(
                    &body,
                    upstream.status.as_u16(),
                    &context,
                    true,
                    &request.admitted.canonical,
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
            let wire_stream = match self
                .wire
                .stream_for_request(&context, &request.admitted.canonical)
            {
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
        published: &PublishedAttempt,
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
        let mut observation = self.observation(
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
        if let AttemptError::Transport(transport_error) = &error {
            observation.error_class = Some(transport_error.diagnostic_class().into());
        }
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
        published: &PublishedAttempt,
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
                NextAction::RetryAccount | NextAction::RetryWire
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
        if effects.retry_scope == RetryScope::Wire {
            *preferred_account = Some(identity.account_name.clone());
        } else {
            excluded_accounts.insert(identity.account_name.clone());
            *preferred_account = None;
        }
    }

    async fn cleanup_failed_attempt(
        &self,
        published: &PublishedAttempt,
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
