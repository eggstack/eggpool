//! Public streaming request, response, phase, and error contracts.

// ---------------------------------------------------------------------------
// Phase model
// ---------------------------------------------------------------------------

use std::time::Duration;

use bytes::Bytes;
use http::{HeaderMap, StatusCode};
use thiserror::Error;

use crate::{
    coordinator::{
        AttemptError, ClientResponseHeaders, FailureEffects, FinalizationError, PublicationError,
    },
    request::{AdmissionError, AdmittedRequest, StaticRoutingFacts, admit_request},
    routing::RoutingRequestFacts,
    wire::ir::ClientSurface,
};

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
pub(crate) struct LastUpstream {
    pub(crate) status: StatusCode,
    pub(crate) headers: HeaderMap,
    pub(crate) body: Bytes,
    pub(crate) effects: FailureEffects,
    pub(crate) upstream_request_id: Option<String>,
    pub(crate) headers_elapsed: Duration,
}
