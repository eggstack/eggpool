//! Finite-response coordinator.
//!
//! This module owns the finite retry loop because response classification,
//! response-start monotonicity, and failed-attempt cleanup must be decided
//! together. Streaming has a separate coordinator boundary.

use std::{
    collections::{BTreeMap, BTreeSet},
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};

use bytes::Bytes;
use http::{HeaderMap, HeaderName, HeaderValue, StatusCode};
use serde_json::json;
use thiserror::Error;
use tokio::runtime::Handle;

use crate::{
    accounts::CredentialStore,
    config::ProviderConfig,
    providers::TransportError,
    request::{
        AdmissionError, AdmittedRequest, CompactAdmittedRequest, StaticRoutingFacts, admit_request,
    },
    routing::{RoutingRequestFacts, RoutingRouter, SelectionClaim},
    wire::{
        ConfiguredWireProfile, FiniteResponseOutcome, WireRuntime, WireRuntimeContext, WireSurface,
        ir::{CacheCounterStatus, CanonicalUsage, ClientSurface, ProviderErrorEvidence},
    },
};

use super::{
    AttemptBuilder, AttemptError, AttemptPreparation, FailureCategory, FailureDecisionEngine,
    FailureEffects, FailureObservation, FailureSource, FinalizationCommand, FinalizationData,
    FinalizationError, FinalizationIdentity, FinalizationOutcome, FinalizationResult,
    FinalizationSupervisor, PublicationError, PublicationInput, PublicationOutcome,
    PublicationService, RetryPolicy, WireResolver,
};

const MAX_CLIENT_ERROR_BYTES: usize = 512;

/// The last upstream response received before terminal exhaustion. Python's
/// `_handle_exhausted` prefers this real response over a synthetic envelope,
/// so C007 retains it to preserve pass-through status/body semantics when
/// retries are exhausted by account depletion or the attempt ceiling.
#[derive(Clone, Debug)]
struct LastUpstream {
    status: StatusCode,
    headers: HeaderMap,
    body: Bytes,
    effects: FailureEffects,
    upstream_request_id: Option<String>,
    headers_elapsed: Duration,
}

/// A monotonic fact marking the point at which downstream response start was
/// sent or attempted.  It is intentionally process-local and cannot be reset
/// during retries.
#[derive(Clone, Debug, Default)]
pub struct ResponseHandoffState {
    started: Arc<std::sync::atomic::AtomicBool>,
}

impl ResponseHandoffState {
    pub fn mark_started(&self) {
        self.started
            .store(true, std::sync::atomic::Ordering::Release);
    }

    pub fn started(&self) -> bool {
        self.started.load(std::sync::atomic::Ordering::Acquire)
    }
}

/// Filtered finite response headers retained in order, including duplicates.
/// A vector is used instead of a map because HTTP header multiplicity is part
/// of the client contract.
pub type ClientResponseHeaders = Vec<(HeaderName, HeaderValue)>;

/// The client-visible finite response produced after M6 adaptation.
#[derive(Clone, Debug)]
pub struct FiniteClientResponse {
    pub status: StatusCode,
    pub headers: ClientResponseHeaders,
    pub body: Bytes,
}

/// How the caller's downstream response write ended.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DownstreamResult {
    Delivered,
    WriteFailed,
    Cancelled,
}

/// A response plus retained C006 ownership.  The caller marks the handoff
/// immediately before sending response start, then calls [`Self::complete`]
/// after the finite body write.  Dropping the value schedules an interrupted
/// terminal command so cancellation cannot strand a converted claim.
pub struct FiniteExecution {
    pub response: FiniteClientResponse,
    completion: PendingFinalization,
}

impl std::fmt::Debug for FiniteExecution {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("FiniteExecution")
            .field("response", &self.response)
            .field("handoff_started", &self.completion.handoff().started())
            .finish()
    }
}

impl FiniteExecution {
    pub fn mark_started(&self) {
        self.completion.handoff().mark_started();
    }

    pub fn handoff_started(&self) -> bool {
        self.completion.handoff().started()
    }

    /// Return the bounded scalar terminal usage event before ownership is
    /// consumed by [`Self::complete`]. Bodies, headers, and credentials never
    /// cross into the process metrics buffer.
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
        event.streamed = false;
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

    /// Transfer terminal ownership to the retained C006 supervisor before the
    /// first cancellation-sensitive await.  A write failure after handoff is
    /// terminal and can never re-enter the upstream retry loop.
    pub async fn complete(
        mut self,
        downstream: DownstreamResult,
    ) -> Result<FinalizationResult, FinalizationError> {
        if downstream == DownstreamResult::Delivered {
            self.mark_started();
        }
        self.completion.complete(downstream).await
    }
}

#[derive(Debug, Error)]
pub enum FiniteCoordinatorError {
    #[error("client request admission failed: {0}")]
    Admission(#[from] AdmissionError),
    #[error("no eligible account was available for finite request")]
    NoEligibleRoute,
    #[error("finite request selected unknown provider {provider_id:?}")]
    MissingProvider { provider_id: String },
    #[error("finite request has no configured wire profile for provider {provider_id:?}")]
    MissingWireProfile { provider_id: String },
    #[error("finite request publication failed: {0}")]
    Publication(#[from] PublicationError),
    #[error("finite routing claim failed: {0}")]
    Claim(#[from] crate::routing::ClaimError),
    #[error("finite provider attempt failed: {0}")]
    Attempt(#[from] AttemptError),
    #[error("finite finalization failed: {0}")]
    Finalization(#[from] FinalizationError),
    #[error("finite failure effect ledger failed: {0}")]
    Effects(String),
    #[error("finite request facts do not match admitted model or surface")]
    InvalidFacts,
}

/// Request input for C007.  Admission is performed once here; the result is
/// then reused by the attempt builder rather than decoding the body again.
///
/// `operation` distinguishes an ordinary assistant completion (`Generate`)
/// from a remote-compaction operation (`Compact`) whose output replaces
/// retained history. Compact requests carry their bounded compact admission
/// alongside the canonical admitted view so the coordinator can prepare the
/// native compact path while reusing the normal routing, publication,
/// retry, and finalization ownership.
#[derive(Debug, Clone)]
pub struct FiniteRequest {
    pub proxy_request_id: String,
    pub raw_body: Bytes,
    pub incoming_headers: HeaderMap,
    pub request_id: Option<String>,
    pub correlation_id: Option<String>,
    pub client_surface: ClientSurface,
    pub admitted: AdmittedRequest,
    pub routing_facts: RoutingRequestFacts,
    pub operation: super::endpoints::InferenceOperation,
    pub compact_admission: Option<crate::request::CompactAdmittedRequest>,
}

enum FiniteAdmission {
    Generate(AdmittedRequest),
    Compact(Option<CompactAdmittedRequest>),
}

struct FiniteExecutionInput {
    proxy_request_id: String,
    raw_body: Bytes,
    incoming_headers: HeaderMap,
    request_id: Option<String>,
    correlation_id: Option<String>,
    client_surface: ClientSurface,
    routing_facts: RoutingRequestFacts,
    operation: super::endpoints::InferenceOperation,
    admission: FiniteAdmission,
}

impl FiniteExecutionInput {
    fn from_public(request: FiniteRequest) -> Self {
        let FiniteRequest {
            proxy_request_id,
            raw_body,
            incoming_headers,
            request_id,
            correlation_id,
            client_surface,
            admitted,
            routing_facts,
            operation,
            compact_admission,
        } = request;
        let admission = match operation {
            super::endpoints::InferenceOperation::Generate => FiniteAdmission::Generate(admitted),
            super::endpoints::InferenceOperation::Compact => {
                FiniteAdmission::Compact(compact_admission)
            }
        };
        Self {
            proxy_request_id,
            raw_body,
            incoming_headers,
            request_id,
            correlation_id,
            client_surface,
            routing_facts,
            operation,
            admission,
        }
    }

    fn compact(
        proxy_request_id: String,
        raw_body: Bytes,
        incoming_headers: HeaderMap,
        compact: CompactAdmittedRequest,
        routing_facts: RoutingRequestFacts,
    ) -> Self {
        Self {
            proxy_request_id,
            raw_body,
            incoming_headers,
            request_id: None,
            correlation_id: None,
            client_surface: ClientSurface::Responses,
            routing_facts,
            operation: super::endpoints::InferenceOperation::Compact,
            admission: FiniteAdmission::Compact(Some(compact)),
        }
    }

    fn canonical(&self) -> Option<&crate::wire::ir::CanonicalRequest> {
        match &self.admission {
            FiniteAdmission::Generate(admitted) => Some(&admitted.canonical),
            FiniteAdmission::Compact(Some(compact)) => Some(&compact.canonical),
            FiniteAdmission::Compact(None) => None,
        }
    }
}

impl FiniteRequest {
    pub fn new(
        proxy_request_id: impl Into<String>,
        raw_body: Bytes,
        incoming_headers: HeaderMap,
        client_surface: ClientSurface,
        mut routing_inputs: StaticRoutingFacts,
    ) -> Result<Self, AdmissionError> {
        let admitted = admit_request(
            &raw_body,
            crate::request::AdmissionOptions {
                client_surface,
                ..crate::request::AdmissionOptions::default()
            },
        )?;
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
            operation: super::endpoints::InferenceOperation::Generate,
            compact_admission: None,
        })
    }

    /// Build one bounded remote-compaction request. The compact endpoint
    /// shares the stateless Responses contract and body bounds with ordinary
    /// Responses admission but is always finite, requires an `input` history
    /// payload, and never accepts a `compaction_trigger` item.
    pub fn new_compact(
        proxy_request_id: impl Into<String>,
        raw_body: Bytes,
        incoming_headers: HeaderMap,
        mut routing_inputs: StaticRoutingFacts,
    ) -> Result<Self, AdmissionError> {
        let compact = crate::request::admit_compact_request(
            &raw_body,
            crate::request::AdmissionOptions {
                client_surface: ClientSurface::Responses,
                ..crate::request::AdmissionOptions::default()
            },
        )?;
        if routing_inputs.requested_protocol.is_none() {
            routing_inputs.requested_protocol = Some(ClientSurface::Responses.protocol().into());
        }
        let routing_facts = compact.routing_facts(&routing_inputs);
        let admitted = AdmittedRequest {
            canonical: compact.canonical.clone(),
            native_preservation: Some(compact.native_preservation.clone()),
            raw_body_bytes: compact.raw_body_bytes,
            reservation_tokens: compact.reservation_tokens,
            context_tokens: compact.context_tokens,
        };
        Ok(Self {
            proxy_request_id: proxy_request_id.into(),
            raw_body,
            incoming_headers,
            request_id: None,
            correlation_id: None,
            client_surface: ClientSurface::Responses,
            admitted,
            routing_facts,
            operation: super::endpoints::InferenceOperation::Compact,
            compact_admission: Some(compact),
        })
    }

    /// Build a compact request from the final parsed admission. Production
    /// endpoint code uses this constructor so compact admission is not
    /// reparsed after model resolution; [`Self::new_compact`] remains the
    /// slice-compatible compatibility constructor.
    pub fn from_compact_admitted(
        proxy_request_id: impl Into<String>,
        raw_body: Bytes,
        incoming_headers: HeaderMap,
        compact: crate::request::CompactAdmittedRequest,
        mut routing_facts: RoutingRequestFacts,
    ) -> Result<Self, FiniteCoordinatorError> {
        if compact.canonical.client_surface != ClientSurface::Responses
            || compact.canonical.stream
            || routing_facts.canonical_model_id != compact.canonical.model
            || routing_facts.request_surface != ClientSurface::Responses.as_str()
            || compact.native_preservation.source_surface != ClientSurface::Responses
        {
            return Err(FiniteCoordinatorError::InvalidFacts);
        }
        if routing_facts.requested_protocol.is_none() {
            routing_facts.requested_protocol = Some(ClientSurface::Responses.protocol().into());
        }
        let admitted = AdmittedRequest {
            canonical: compact.canonical.clone(),
            native_preservation: Some(compact.native_preservation.clone()),
            raw_body_bytes: compact.raw_body_bytes,
            reservation_tokens: compact.reservation_tokens,
            context_tokens: compact.context_tokens,
        };
        Ok(Self {
            proxy_request_id: proxy_request_id.into(),
            raw_body,
            incoming_headers,
            request_id: None,
            correlation_id: None,
            client_surface: ClientSurface::Responses,
            admitted,
            routing_facts,
            operation: super::endpoints::InferenceOperation::Compact,
            compact_admission: Some(compact),
        })
    }

    pub fn from_admitted(
        proxy_request_id: impl Into<String>,
        raw_body: Bytes,
        incoming_headers: HeaderMap,
        client_surface: ClientSurface,
        admitted: AdmittedRequest,
        routing_facts: RoutingRequestFacts,
    ) -> Result<Self, FiniteCoordinatorError> {
        if admitted.canonical.client_surface != client_surface
            || admitted.canonical.model != routing_facts.canonical_model_id
            || routing_facts.request_surface != client_surface.as_str()
        {
            return Err(FiniteCoordinatorError::InvalidFacts);
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
            operation: super::endpoints::InferenceOperation::Generate,
            compact_admission: None,
        })
    }

    /// Compact operation label for safe diagnostics (no content).
    pub fn operation(&self) -> super::endpoints::InferenceOperation {
        self.operation
    }
}

/// End-to-end finite coordinator for one immutable migration generation.
#[derive(Clone)]
pub struct FiniteCoordinator {
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
    max_provider_body_bytes: usize,
}

impl std::fmt::Debug for FiniteCoordinator {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("FiniteCoordinator")
            .field("providers", &self.providers.keys().collect::<Vec<_>>())
            .field("retry_policy", &self.retry_policy)
            .field("max_provider_body_bytes", &self.max_provider_body_bytes)
            .finish()
    }
}

#[allow(clippy::too_many_arguments)]
impl FiniteCoordinator {
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
            max_provider_body_bytes: crate::wire::DEFAULT_MAX_PROVIDER_BODY_BYTES,
        }
    }

    pub fn with_max_provider_body_bytes(mut self, limit: usize) -> Self {
        self.max_provider_body_bytes = limit.max(1);
        self
    }

    pub fn finalization_supervisor(&self) -> FinalizationSupervisor {
        self.finalization.clone()
    }

    pub fn wire_resolver(&self) -> WireResolver {
        self.wire_resolver.clone()
    }

    /// Execute the finite upstream lifecycle.  Only pre-handoff failures enter
    /// this loop; terminal results carry a retained completion owner back to
    /// the caller.
    pub async fn execute(
        &self,
        request: FiniteRequest,
    ) -> Result<FiniteExecution, FiniteCoordinatorError> {
        self.execute_input(FiniteExecutionInput::from_public(request))
            .await
    }

    /// Execute a production compact request without constructing the public
    /// compatibility `FiniteRequest` view. The compact admission remains the
    /// sole owner of the preserved source-native JSON tree for this path.
    pub(crate) async fn execute_compact_admitted(
        &self,
        proxy_request_id: String,
        raw_body: Bytes,
        incoming_headers: HeaderMap,
        compact: CompactAdmittedRequest,
        routing_facts: RoutingRequestFacts,
    ) -> Result<FiniteExecution, FiniteCoordinatorError> {
        self.execute_input(FiniteExecutionInput::compact(
            proxy_request_id,
            raw_body,
            incoming_headers,
            compact,
            routing_facts,
        ))
        .await
    }

    async fn execute_input(
        &self,
        request: FiniteExecutionInput,
    ) -> Result<FiniteExecution, FiniteCoordinatorError> {
        let Some(canonical) = request.canonical() else {
            return Err(FiniteCoordinatorError::InvalidFacts);
        };
        if canonical.client_surface != request.client_surface
            || request.routing_facts.canonical_model_id != canonical.model
        {
            return Err(FiniteCoordinatorError::InvalidFacts);
        }

        let mut attempt_number = 1_u32;
        let mut excluded_accounts = BTreeSet::new();
        let mut preferred_account = None;
        let mut attempted_wires: BTreeMap<String, BTreeSet<WireSurface>> = BTreeMap::new();
        let mut last_identity: Option<FinalizationIdentity> = None;
        let mut last_upstream: Option<LastUpstream> = None;
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
                // No eligible account remains. Prefer the last real upstream
                // response (Python `_handle_exhausted` pass-through) over a
                // synthetic envelope when at least one dispatch returned.
                if let (Some(identity), Some(last)) = (last_identity.clone(), last_upstream.clone())
                {
                    let response = self.client_response(
                        last.status,
                        &last.headers,
                        last.body.clone(),
                        &request.proxy_request_id,
                        attempt_number.saturating_sub(1).max(1),
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
                    );
                    return Ok(self.pending_terminal(identity, None, response, data));
                }
                return match last_identity {
                    Some(identity) => Ok(self.pending_terminal(
                        identity,
                        None,
                        self.error_response(
                            request.client_surface,
                            &request.proxy_request_id,
                            attempt_number.saturating_sub(1).max(1),
                            StatusCode::SERVICE_UNAVAILABLE,
                            "no eligible provider remained after retry",
                        ),
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
                    )),
                    None => Err(FiniteCoordinatorError::NoEligibleRoute),
                };
            };

            let provider_id = claim.provider_id().to_owned();
            let Some(provider) = self.providers.get(&provider_id).cloned() else {
                claim.rollback_claim()?;
                return Err(FiniteCoordinatorError::MissingProvider { provider_id });
            };
            let mut profiles = self
                .provider_profiles
                .get(&provider_id)
                .cloned()
                .unwrap_or_default();
            if profiles.is_empty() {
                claim.rollback_claim()?;
                return Err(FiniteCoordinatorError::MissingWireProfile { provider_id });
            }
            // Compact operations filter to natively compact-capable Responses
            // surfaces before submission. Accounts without a qualified compact
            // target are skipped without upstream I/O; when none remain the
            // loop converges to no-eligible-route rather than a lossy
            // translated compaction.
            let is_compact = request.operation == super::endpoints::InferenceOperation::Compact;
            if is_compact {
                profiles.retain(|profile| {
                    profile.definition.surface == crate::wire::WireSurface::OpenaiResponses
                        && provider
                            .wire_surfaces
                            .get(profile.definition.surface.as_str())
                            .is_some_and(|surface| {
                                crate::wire::CompactionCapabilities::from_surface_config(surface)
                                    .native_v1_supported()
                            })
                });
                if profiles.is_empty() {
                    claim.rollback_claim()?;
                    excluded_accounts.insert(claim.account_name().to_owned());
                    preferred_account = None;
                    continue;
                }
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
                false,
                i64::from(attempt_number),
            );
            let published = match self.publication.publish(claim, publication_input).await? {
                PublicationOutcome::Published(value) => value,
                PublicationOutcome::AlreadyPublished(_) => {
                    return Err(FiniteCoordinatorError::Publication(
                        PublicationError::DuplicateConflict {
                            proxy_request_id: request.proxy_request_id,
                        },
                    ));
                }
            };
            last_identity = Some(published.identity.clone());
            let identity = published.identity.clone();
            let borrowed_input = AttemptPreparation {
                identity: &identity,
                provider: &provider,
                account_api_key: self.credentials.get(&identity.account_name),
                incoming_headers: &request.incoming_headers,
                request_id: request.request_id.as_deref(),
                correlation_id: request.correlation_id.as_deref(),
                raw_body: &request.raw_body,
                client_surface: request.client_surface,
                profile: &candidate.profile,
                stream: false,
                candidate_fingerprint: &resolution.fingerprint,
            };
            // Compact operations prepare through the native compact path
            // (source-native preservation plus EggPool-owned model rewrite);
            // unsupported targets fail here, before submission.
            let prepared = if is_compact {
                let FiniteAdmission::Compact(Some(compact_admission)) = &request.admission else {
                    return Err(FiniteCoordinatorError::InvalidFacts);
                };
                match self
                    .attempts
                    .prepare_compact_borrowed(borrowed_input, compact_admission)
                {
                    Ok(value) => value,
                    Err(error) => {
                        let response = self.error_response(
                            request.client_surface,
                            &request.proxy_request_id,
                            attempt_number,
                            StatusCode::BAD_REQUEST,
                            "compact request could not be prepared for the selected provider",
                        );
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
                            response,
                            data,
                        ));
                    }
                }
            } else {
                let FiniteAdmission::Generate(admitted) = &request.admission else {
                    return Err(FiniteCoordinatorError::InvalidFacts);
                };
                match self.attempts.prepare_borrowed(borrowed_input, admitted) {
                    Ok(value) => value,
                    Err(error) => {
                        let response = self.error_response(
                            request.client_surface,
                            &request.proxy_request_id,
                            attempt_number,
                            StatusCode::BAD_REQUEST,
                            "request could not be prepared for the selected provider",
                        );
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
                            response,
                            data,
                        ));
                    }
                }
            };

            let mut upstream = match self.attempts.submit_once(prepared).await {
                Ok(value) => value,
                Err(error) => {
                    let is_body_too_large = matches!(
                        &error,
                        AttemptError::Transport(TransportError::ResponseBodyTooLarge)
                    );
                    let (source, category_hint) = match &error {
                        AttemptError::Transport(TransportError::ResponseBodyTooLarge) => (
                            FailureSource::ProviderResponse,
                            Some(FailureCategory::Fatal),
                        ),
                        AttemptError::Transport(_) => (FailureSource::Transport, None),
                        _ => (FailureSource::LocalPreparation, None),
                    };
                    let observation = self.observation(
                        &identity,
                        &candidate.profile,
                        attempt_number,
                        source,
                        None,
                        category_hint,
                        None,
                        alternate_wire_available,
                        "transport",
                    );
                    let (effects, first) = self.decide(&observation)?;
                    if first {
                        self.apply_effects(&published.claim, &effects);
                    }
                    if self.should_retry(&effects) {
                        self.cleanup_failed_attempt(
                            &published,
                            self.retry_cleanup_data(
                                &identity,
                                upstream_protocol,
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
                            &mut attempt_number,
                            &mut preferred_account,
                            &mut excluded_accounts,
                            &identity,
                            &effects,
                        );
                        continue;
                    }
                    let status = if is_body_too_large {
                        StatusCode::BAD_GATEWAY
                    } else if source == FailureSource::LocalPreparation {
                        StatusCode::BAD_REQUEST
                    } else {
                        StatusCode::BAD_GATEWAY
                    };
                    let response = self.error_response(
                        request.client_surface,
                        &request.proxy_request_id,
                        attempt_number,
                        status,
                        effects.client_outcome.as_str(),
                    );
                    let data = self.failure_data(
                        &identity,
                        upstream_protocol,
                        &effects,
                        None,
                        None,
                        request_start.elapsed(),
                        request_bytes,
                        0,
                    );
                    return Ok(self.pending_terminal(
                        identity,
                        Some(published.claim),
                        response,
                        data,
                    ));
                }
            };
            let body = match upstream
                .body
                .read_to_bytes(self.max_provider_body_bytes)
                .await
            {
                Ok(body) => body,
                Err(error) => {
                    let is_body_too_large = matches!(error, TransportError::ResponseBodyTooLarge);
                    let source = if is_body_too_large {
                        FailureSource::ProviderResponse
                    } else {
                        FailureSource::Transport
                    };
                    let category_hint = is_body_too_large.then_some(FailureCategory::Fatal);
                    let observation = self.observation(
                        &identity,
                        &candidate.profile,
                        attempt_number,
                        source,
                        None,
                        category_hint,
                        None,
                        alternate_wire_available,
                        "body_read",
                    );
                    let (effects, first) = self.decide(&observation)?;
                    if first {
                        self.apply_effects(&published.claim, &effects);
                    }
                    if self.should_retry(&effects) {
                        self.cleanup_failed_attempt(
                            &published,
                            self.retry_cleanup_data(
                                &identity,
                                upstream_protocol,
                                &effects,
                                None,
                                upstream.upstream_request_id.clone(),
                                upstream.headers_elapsed,
                                request_bytes,
                                0,
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
                    let status = if is_body_too_large {
                        StatusCode::BAD_GATEWAY
                    } else if source == FailureSource::LocalPreparation {
                        StatusCode::BAD_REQUEST
                    } else {
                        StatusCode::BAD_GATEWAY
                    };
                    let response = self.error_response(
                        request.client_surface,
                        &request.proxy_request_id,
                        attempt_number,
                        status,
                        effects.client_outcome.as_str(),
                    );
                    let data = self.failure_data(
                        &identity,
                        upstream_protocol,
                        &effects,
                        None,
                        upstream.upstream_request_id,
                        upstream.headers_elapsed,
                        request_bytes,
                        0,
                    );
                    return Ok(self.pending_terminal(
                        identity,
                        Some(published.claim),
                        response,
                        data,
                    ));
                }
            };
            let context = self.response_context(
                request.client_surface,
                candidate.profile.clone(),
                &identity,
                &provider,
            );
            // Compact operations validate the native replacement-history
            // result without canonicalizing it into an ordinary completion.
            // A semantic validation failure is never a successful provider
            // response. Retry, health, usage, and finalization ownership are
            // otherwise identical to normal generation.
            if is_compact {
                let decoded = match self.wire.decode_compact_response(
                    &body,
                    upstream.status.as_u16(),
                    &context,
                ) {
                    Ok(value) => value,
                    Err(error) => {
                        let observation = self.observation(
                            &identity,
                            &candidate.profile,
                            attempt_number,
                            FailureSource::LocalPreparation,
                            Some(upstream.status),
                            Some(FailureCategory::Fatal),
                            None,
                            alternate_wire_available,
                            "response_adaptation",
                        );
                        let (effects, first) = self.decide(&observation)?;
                        if first {
                            self.apply_effects(&published.claim, &effects);
                        }
                        let error_class = match &error {
                            crate::wire::WireRuntimeError::BodyTooLarge => "ResponseBodyTooLarge",
                            _ => "ResponseAdaptation",
                        };
                        let response = self.error_response(
                            request.client_surface,
                            &request.proxy_request_id,
                            attempt_number,
                            StatusCode::INTERNAL_SERVER_ERROR,
                            "compact response could not be adapted for the client",
                        );
                        let data = self.local_failure_data(
                            &identity,
                            &candidate.profile,
                            StatusCode::INTERNAL_SERVER_ERROR,
                            error_class,
                            request_bytes,
                        );
                        let _ = error;
                        let _ = effects;
                        return Ok(self.pending_terminal(
                            published.identity,
                            Some(published.claim),
                            response,
                            data,
                        ));
                    }
                };
                match decoded.outcome {
                    crate::wire::CompactResponseOutcome::Success => {
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
                        let status = upstream.status;
                        let provider_bytes = body.len();
                        let upstream_request_id = upstream.upstream_request_id.clone();
                        let headers_elapsed = upstream.headers_elapsed;
                        let total_elapsed = request_start.elapsed();
                        let client_response = self.client_response(
                            status,
                            &upstream.headers,
                            client_body,
                            &request.proxy_request_id,
                            attempt_number,
                        );
                        let data = self.success_data(
                            &identity,
                            decoded.usage.as_ref(),
                            upstream.status,
                            headers_elapsed,
                            total_elapsed,
                            request_bytes,
                            provider_bytes,
                            upstream_request_id,
                        );
                        return Ok(self.pending_terminal(
                            identity,
                            Some(published.claim),
                            client_response,
                            data,
                        ));
                    }
                    crate::wire::CompactResponseOutcome::ProviderError(error) => {
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
                        let mut response = self.client_response(
                            upstream.status,
                            &upstream.headers,
                            body.clone(),
                            &request.proxy_request_id,
                            attempt_number,
                        );
                        let upstream_fault =
                            matches!(upstream.status.as_u16(), 408 | 425 | 429 | 500..=599);
                        if upstream_fault
                            && attempt_number >= self.retry_policy.max_attempts.max(1)
                            && let Ok(value) = HeaderValue::try_from("attempt_ceiling_reached")
                        {
                            response
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
                            response,
                            data,
                        ));
                    }
                    crate::wire::CompactResponseOutcome::Malformed { .. } => {
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
                        );
                        let (effects, first) = self.decide(&observation)?;
                        if first {
                            self.apply_effects(&published.claim, &effects);
                        }
                        // A 2xx compact body that fails replacement-history
                        // validation is terminal and never replayed; a lossy
                        // compact result must not break a long-running agent
                        // later.
                        let response = self.error_response(
                            request.client_surface,
                            &request.proxy_request_id,
                            attempt_number,
                            StatusCode::INTERNAL_SERVER_ERROR,
                            "provider returned a malformed compact response",
                        );
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
                            response,
                            data,
                        ));
                    }
                }
            }
            let decoded = match self.wire.decode_finite_response_for_request(
                &body,
                upstream.status.as_u16(),
                &context,
                true,
                canonical,
            ) {
                Ok(value) => value,
                Err(error) => {
                    let observation = self.observation(
                        &identity,
                        &candidate.profile,
                        attempt_number,
                        FailureSource::LocalPreparation,
                        Some(upstream.status),
                        Some(FailureCategory::Fatal),
                        None,
                        alternate_wire_available,
                        "response_adaptation",
                    );
                    let (effects, first) = self.decide(&observation)?;
                    if first {
                        self.apply_effects(&published.claim, &effects);
                    }
                    // M6 client-surface encoding (including loss rejection and
                    // client-body bounds) failed after a decodable upstream
                    // response. Python converges this as a client-error
                    // terminal via _LocalDispatchError; never retry.
                    let error_class = match &error {
                        crate::wire::WireRuntimeError::BodyTooLarge => "ResponseBodyTooLarge",
                        _ => "ResponseAdaptation",
                    };
                    let response = self.error_response(
                        request.client_surface,
                        &request.proxy_request_id,
                        attempt_number,
                        StatusCode::INTERNAL_SERVER_ERROR,
                        "provider response could not be adapted for the client",
                    );
                    let data = self.local_failure_data(
                        &identity,
                        &candidate.profile,
                        StatusCode::INTERNAL_SERVER_ERROR,
                        error_class,
                        request_bytes,
                    );
                    let _ = error;
                    let _ = effects;
                    return Ok(self.pending_terminal(
                        published.identity,
                        Some(published.claim),
                        response,
                        data,
                    ));
                }
            };

            match decoded.outcome {
                FiniteResponseOutcome::Success(_response) => {
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
                    let status = upstream.status;
                    let provider_bytes = body.len();
                    let upstream_request_id = upstream.upstream_request_id.clone();
                    let headers_elapsed = upstream.headers_elapsed;
                    let total_elapsed = request_start.elapsed();
                    let client_response = self.client_response(
                        status,
                        &upstream.headers,
                        client_body,
                        &request.proxy_request_id,
                        attempt_number,
                    );
                    let data = self.success_data(
                        &identity,
                        decoded.usage.as_ref(),
                        upstream.status,
                        headers_elapsed,
                        total_elapsed,
                        request_bytes,
                        provider_bytes,
                        upstream_request_id,
                    );
                    return Ok(self.pending_terminal(
                        identity,
                        Some(published.claim),
                        client_response,
                        data,
                    ));
                }
                FiniteResponseOutcome::ProviderError(error) => {
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
                        // The C005 classifier already enforces the attempt
                        // budget (`attempt_number < max_attempts` is required
                        // for `retry=true`), so reaching this branch means a
                        // further attempt is authorized. Budget exhaustion
                        // surfaces below as `retry=false` and is terminalized
                        // without replay.
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
                    // Immediate terminal provider error. Python distinguishes
                    // the non-retryable client-error pass-through
                    // (`_finalize_non_retryable`, CLIENT_ERROR) from an
                    // exhausted upstream fault (`_handle_exhausted`,
                    // UPSTREAM_ERROR). Upstream-fault statuses (rate pressure,
                    // timeouts, 5xx) stay UPSTREAM_ERROR even when the C005
                    // budget already capped `retry=false`; this also covers
                    // the attempt-ceiling case, which carries the Python
                    // `attempt_ceiling_reached` marker.
                    let provider_bytes = body.len();
                    let mut response = self.client_response(
                        upstream.status,
                        &upstream.headers,
                        body.clone(),
                        &request.proxy_request_id,
                        attempt_number,
                    );
                    let upstream_fault =
                        matches!(upstream.status.as_u16(), 408 | 425 | 429 | 500..=599);
                    if upstream_fault
                        && attempt_number >= self.retry_policy.max_attempts.max(1)
                        && let Ok(value) = HeaderValue::try_from("attempt_ceiling_reached")
                    {
                        response
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
                        response,
                        data,
                    ));
                }
                FiniteResponseOutcome::Malformed { .. } => {
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
                    );
                    let (effects, first) = self.decide(&observation)?;
                    if first {
                        self.apply_effects(&published.claim, &effects);
                    }
                    // A 2xx body that M6 cannot decode as the selected
                    // surface is a terminal client-error in Python
                    // (_LocalDispatchError stage response_adaptation), not a
                    // retryable upstream error. Converge with retained C006
                    // ownership and never replay.
                    let response = self.error_response(
                        request.client_surface,
                        &request.proxy_request_id,
                        attempt_number,
                        StatusCode::INTERNAL_SERVER_ERROR,
                        "provider returned a malformed finite response",
                    );
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
                        response,
                        data,
                    ));
                }
            }
        }
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
            profile.clone(),
            identity.model_id.clone(),
            identity.upstream_model_id.clone(),
        );
        context.provider_id = Some(identity.provider_id.clone());
        context.provider_kind = provider.kind.clone();
        context.max_provider_body_bytes = self.max_provider_body_bytes;
        // Resolve remote-compaction capabilities from the provider-owned
        // table so trigger validation and compact decoding observe the same
        // operator facts as attempt preparation.
        if let Some(surface) = provider
            .wire_surfaces
            .get(profile.definition.surface.as_str())
        {
            context = context.with_compaction(
                crate::wire::CompactionCapabilities::from_surface_config(surface),
            );
        }
        context
    }

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
    ) -> FailureObservation {
        let mut observation = FailureObservation::response(
            identity.attempt_id,
            attempt_number,
            status.unwrap_or(StatusCode::INTERNAL_SERVER_ERROR),
        );
        observation.source = source;
        observation.status = status.map(|value| value.as_u16());
        observation.category_hint = category_hint;
        observation.response_started = false;
        observation.downstream_started = false;
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
    ) -> Result<(FailureEffects, bool), FiniteCoordinatorError> {
        self.failure_engine
            .lock()
            .expect("finite failure engine lock")
            .decide(observation)
            .map_err(|error| FiniteCoordinatorError::Effects(error.to_string()))
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
    ) -> Result<FinalizationResult, FiniteCoordinatorError> {
        let handle = self
            .finalization
            .register(FinalizationCommand::FailedAttempt {
                identity: published.identity.clone(),
                data,
                claim: Some(published.claim.clone()),
            })?;
        Ok(handle.wait().await?)
    }

    fn pending_terminal(
        &self,
        identity: FinalizationIdentity,
        claim: Option<SelectionClaim>,
        response: FiniteClientResponse,
        data: FinalizationData,
    ) -> FiniteExecution {
        FiniteExecution {
            response,
            completion: PendingFinalization::new(self.finalization.clone(), identity, claim, data),
        }
    }

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
        let input = bounded_i64(usage.input_tokens);
        let output = bounded_i64(usage.output_tokens);
        FinalizationData {
            outcome: FinalizationOutcome::Completed,
            status_code: Some(status.as_u16()),
            input_tokens: input,
            output_tokens: output,
            cache_read_tokens: bounded_i64(usage.cache_read_input_tokens),
            cache_write_tokens: bounded_i64(
                usage
                    .cache_write_input_tokens
                    .or(usage.cache_creation_input_tokens),
            ),
            reasoning_tokens: bounded_i64(usage.reasoning_tokens),
            // M6 CanonicalUsage carries no cost provenance; leave cost unset
            // rather than fabricating a zero estimate. Python persists the
            // provider-reported cost only when the upstream surfaces one.
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
    ) -> FinalizationData {
        FinalizationData {
            outcome: FinalizationOutcome::UpstreamError,
            status_code: status.map(|value| value.as_u16()),
            cost_microdollars: 0,
            // No normalized usage exists on failure paths; persist the
            // Python safe default rather than NULL (the column is NOT NULL).
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
            downstream_started: effects.downstream_started,
            transcoded: identity.client_protocol != upstream_protocol,
            upstream_protocol: Some(upstream_protocol.into()),
            ..FinalizationData::default()
        }
    }

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
            // No normalized usage exists on local failure paths; persist
            // the Python safe default rather than NULL.
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

    fn client_response(
        &self,
        status: StatusCode,
        headers: &HeaderMap,
        body: Bytes,
        proxy_request_id: &str,
        attempt_number: u32,
    ) -> FiniteClientResponse {
        let mut headers = filter_response_headers(headers);
        if !headers
            .iter()
            .any(|(name, _)| name == http::header::CONTENT_TYPE)
        {
            headers.push((
                http::header::CONTENT_TYPE,
                HeaderValue::from_static("application/json"),
            ));
        }
        if let Ok(value) = HeaderValue::try_from(proxy_request_id) {
            headers.push((HeaderName::from_static("x-proxy-request-id"), value));
        }
        if let Ok(value) = HeaderValue::try_from(attempt_number.to_string()) {
            headers.push((HeaderName::from_static("x-proxy-attempt-count"), value));
        }
        FiniteClientResponse {
            status,
            headers,
            body,
        }
    }

    fn error_response(
        &self,
        client_surface: ClientSurface,
        proxy_request_id: &str,
        attempt_number: u32,
        status: StatusCode,
        message: &str,
    ) -> FiniteClientResponse {
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
        let body: Vec<u8> = body.into_iter().take(MAX_CLIENT_ERROR_BYTES).collect();
        self.client_response(
            status,
            &HeaderMap::new(),
            Bytes::from(body),
            proxy_request_id,
            attempt_number,
        )
    }
}

struct PendingFinalization {
    parts: Option<PendingFinalizationParts>,
}

struct PendingFinalizationParts {
    supervisor: FinalizationSupervisor,
    identity: FinalizationIdentity,
    claim: Option<SelectionClaim>,
    data: FinalizationData,
    handoff: ResponseHandoffState,
}

impl PendingFinalization {
    fn new(
        supervisor: FinalizationSupervisor,
        identity: FinalizationIdentity,
        claim: Option<SelectionClaim>,
        data: FinalizationData,
    ) -> Self {
        Self {
            parts: Some(PendingFinalizationParts {
                supervisor,
                identity,
                claim,
                data,
                handoff: ResponseHandoffState::default(),
            }),
        }
    }

    fn handoff(&self) -> &ResponseHandoffState {
        &self
            .parts
            .as_ref()
            .expect("pending finalization exists")
            .handoff
    }

    async fn complete(
        &mut self,
        downstream: DownstreamResult,
    ) -> Result<FinalizationResult, FinalizationError> {
        let mut parts = self.parts.take().expect("pending finalization exists");
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
        }
        parts.data.downstream_started = handoff_started;
        let command = FinalizationCommand::Request {
            identity: parts.identity,
            data: parts.data,
            claim: parts.claim,
        };
        let handle = parts.supervisor.register(command)?;
        handle.wait().await
    }
}

impl Drop for PendingFinalization {
    fn drop(&mut self) {
        let Some(parts) = self.parts.take() else {
            return;
        };
        let mut data = parts.data;
        data.outcome = FinalizationOutcome::Interrupted;
        data.error_class = Some("CoordinatorInterrupted".into());
        data.release_reason = Some("pending_response_dropped".into());
        data.downstream_started = parts.handoff.started();
        let command = FinalizationCommand::Request {
            identity: parts.identity,
            data,
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

const HOP_BY_HOP_HEADERS: &[&str] = &[
    "connection",
    "keep-alive",
    "proxy-connection",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "trailers",
    "transfer-encoding",
    "upgrade",
];

const RESPONSE_INTERNAL_HEADERS: &[&str] = &[
    "authorization",
    "proxy-authorization",
    "x-api-key",
    "content-encoding",
    "content-length",
];

pub fn filter_response_headers(headers: &HeaderMap) -> ClientResponseHeaders {
    let connection_tokens = headers
        .get_all(http::header::CONNECTION)
        .iter()
        .filter_map(|value| value.to_str().ok())
        .flat_map(|value| value.split(','))
        .map(|value| value.trim().to_ascii_lowercase())
        .collect::<BTreeSet<_>>();
    headers
        .iter()
        .filter(|(name, _)| {
            let name = name.as_str();
            !HOP_BY_HOP_HEADERS.contains(&name)
                && !RESPONSE_INTERNAL_HEADERS.contains(&name)
                && !connection_tokens.contains(name)
        })
        .map(|(name, value)| (name.clone(), value.clone()))
        .collect()
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
