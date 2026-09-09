//! C009 public inference endpoint adapter.
//!
//! Thin boundary between Axum handlers and the qualified M7 coordinators.
//! Handlers must not contain routing, retry, or finalization loops: they
//! build/admit through M6, derive M5 facts, invoke exactly one coordinator
//! entry point, and hand back the finite/stream response object.
//!
//! This module owns:
//!
//! - client-surface mapping for the three production inference routes;
//! - protocol-shaped error envelopes (OpenAI vs Anthropic);
//! - Responses stateless validation (Python parity);
//! - provider-qualified model parsing (`model/provider`);
//! - exact virtual-alias resolution before concrete parsing;
//! - semantic-selector dispatch with recursion guard, bounded budgets,
//!   deterministic fallback, and affinity commit per D007;
//! - single finite/stream coordinator invocation.
//!
//! Authentication, request-body ceilings, content-type handling, and SSE
//! framing remain the handler/server boundary. No secret, prompt, response
//! body, or session identity enters diagnostics or `Debug` beyond lengths
//! and counts.

use std::{
    collections::{BTreeMap, BTreeSet},
    sync::Arc,
};

use bytes::Bytes;
use http::{HeaderMap, HeaderValue, StatusCode};
use serde_json::{Map, Value, json};
use thiserror::Error;

use crate::{
    accounts::{AccountRegistry, CredentialStore},
    catalog::{CatalogService, ModelCatalogCache, ModelInput, ProtocolResolutionStatus},
    config::{Config, ProviderConfig},
    db::{Account, Database},
    model_router::{
        AffinitySelection, CompiledModelRouter, ModelRouterAffinity, ModelRouterRegistry,
    },
    providers::ProviderClientPool,
    quota::{AccountQuota, QuotaEstimator},
    request::StaticRoutingFacts,
    routing::{EligibilityPolicy, RoutingRouter},
    wire::{
        ConfiguredWireProfile, WireCodecId, WireProfileDefinition, WireProfileRegistry,
        WireRuntime, WireSurface, ir::ClientSurface,
    },
};

use super::{
    AttemptBuilder, DurableFinalizer, FinalizationSupervisor, FiniteCoordinator, FiniteExecution,
    FiniteRequest, PublicationService, RetryPolicy, StreamRequest, StreamingCoordinator,
    StreamingExecution, WireResolver,
    semantic::{SelectionSource, SemanticSelector},
};

/// Maximum client error body (matches finite/streaming coordinator bound).
const MAX_CLIENT_ERROR_BYTES: usize = 512;

/// Thin endpoint error mapped to a protocol-shaped HTTP response.
#[derive(Debug, Error)]
pub enum EndpointError {
    #[error("Invalid JSON")]
    InvalidJson,
    #[error("Missing model field")]
    MissingModel,
    #[error("{0}")]
    StatelessViolation(String),
    #[error("Invalid stream value: must be a boolean")]
    InvalidStream,
    #[error("Request could not be admitted")]
    Admission,
    #[error("No eligible account was available")]
    NoEligibleRoute,
    #[error("Unknown provider {provider_id:?}")]
    MissingProvider { provider_id: String },
    #[error("No configured wire profile for provider {provider_id:?}")]
    MissingWireProfile { provider_id: String },
    #[error("Request body too large")]
    BodyTooLarge,
    #[error("Duplicate request identity")]
    PublicationConflict,
    #[error("Routing claim failed")]
    Claim,
    #[error("Provider attempt failed")]
    Attempt,
    #[error("Finalization failed")]
    Finalization,
}

impl EndpointError {
    pub fn status(&self) -> StatusCode {
        match self {
            Self::InvalidJson
            | Self::MissingModel
            | Self::StatelessViolation(_)
            | Self::InvalidStream
            | Self::Admission => StatusCode::BAD_REQUEST,
            Self::BodyTooLarge => StatusCode::PAYLOAD_TOO_LARGE,
            Self::NoEligibleRoute
            | Self::MissingProvider { .. }
            | Self::MissingWireProfile { .. }
            | Self::Claim
            | Self::Attempt
            | Self::Finalization => StatusCode::SERVICE_UNAVAILABLE,
            Self::PublicationConflict => StatusCode::CONFLICT,
        }
    }
}

/// Render a protocol-shaped error body (Python parity).
pub fn endpoint_error_body(surface: ClientSurface, message: &str) -> Vec<u8> {
    let value = if surface == ClientSurface::Messages {
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
    body.into_iter().take(MAX_CLIENT_ERROR_BYTES).collect()
}

/// Validate the stateless Responses contract (Python parity).
///
/// Returns a rejection message when stateful fields are present; `None`
/// means the payload is stateless and may continue.
pub fn validate_responses_stateless(payload: &Map<String, Value>) -> Option<String> {
    if payload
        .get("previous_response_id")
        .is_some_and(|value| !value.is_null())
    {
        return Some(
            "EggPool's /v1/responses is stateless only; previous_response_id is not supported."
                .into(),
        );
    }
    if payload
        .get("conversation")
        .is_some_and(|value| !value.is_null())
    {
        return Some(
            "EggPool's /v1/responses is stateless only; conversation references are not supported."
                .into(),
        );
    }
    match payload.get("store") {
        Some(Value::Bool(false)) => {}
        Some(Value::Bool(true)) => {
            return Some(
                "EggPool's /v1/responses is stateless only; store=true is not supported.".into(),
            );
        }
        None => {
            return Some(
                "EggPool's /v1/responses is stateless only; store=false must be explicitly set."
                    .into(),
            );
        }
        Some(_) => {
            return Some(
                "EggPool's /v1/responses is stateless only; store must be explicitly false.".into(),
            );
        }
    }
    if payload.get("background") == Some(&Value::Bool(true)) {
        return Some(
            "EggPool's /v1/responses is stateless only; background=true is not supported.".into(),
        );
    }
    None
}

/// Parse `model/provider` into `(model_id, provider_id)`.
///
/// Splits on the final `/`. The suffix is a provider only when it matches a
/// known provider; otherwise the input is returned unchanged so routing can
/// produce a proper no-eligible outcome with the original model string.
pub fn parse_provider_qualified_model(
    model: &str,
    known_providers: &BTreeSet<String>,
) -> (String, Option<String>) {
    let normalized = model.trim().to_owned();
    if !normalized.contains('/') {
        return (normalized, None);
    }
    let (base, candidate) = normalized.rsplit_once('/').expect("split checked");
    if base.is_empty() || candidate.is_empty() {
        return (normalized, None);
    }
    if !known_providers.contains(candidate) {
        return (normalized, None);
    }
    (base.to_owned(), Some(candidate.to_owned()))
}

/// Shared inference dependencies for one immutable generation.
///
/// Cloned into Axum state; all interior coordination state (router claims,
/// wire resolver, affinity, finalization supervisor) is already shared and
/// bounded by its owning module.
#[derive(Clone)]
pub struct InferenceState {
    finite: FiniteCoordinator,
    streaming: StreamingCoordinator,
    registry: ModelRouterRegistry,
    affinity: Arc<ModelRouterAffinity>,
    known_providers: BTreeSet<String>,
    max_body_bytes: usize,
    router: RoutingRouter,
    catalog_service: Option<Arc<CatalogService>>,
}

impl std::fmt::Debug for InferenceState {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("InferenceState")
            .field("known_providers", &self.known_providers)
            .field("max_body_bytes", &self.max_body_bytes)
            .field("virtual_models", &self.registry.len())
            .finish()
    }
}

impl InferenceState {
    pub fn from_parts(
        finite: FiniteCoordinator,
        streaming: StreamingCoordinator,
        registry: ModelRouterRegistry,
        affinity: Arc<ModelRouterAffinity>,
        known_providers: BTreeSet<String>,
        max_body_bytes: usize,
        router: RoutingRouter,
    ) -> Self {
        Self::from_parts_with_catalog_service(
            finite,
            streaming,
            registry,
            affinity,
            known_providers,
            max_body_bytes,
            router,
            None,
        )
    }

    #[allow(clippy::too_many_arguments)]
    pub fn from_parts_with_catalog_service(
        finite: FiniteCoordinator,
        streaming: StreamingCoordinator,
        registry: ModelRouterRegistry,
        affinity: Arc<ModelRouterAffinity>,
        known_providers: BTreeSet<String>,
        max_body_bytes: usize,
        router: RoutingRouter,
        catalog_service: Option<Arc<CatalogService>>,
    ) -> Self {
        Self {
            finite,
            streaming,
            registry,
            affinity,
            known_providers,
            max_body_bytes: max_body_bytes.max(1),
            router,
            catalog_service,
        }
    }

    pub fn registry(&self) -> &ModelRouterRegistry {
        &self.registry
    }

    pub fn affinity(&self) -> &ModelRouterAffinity {
        &self.affinity
    }

    pub fn affinity_handle(&self) -> Arc<ModelRouterAffinity> {
        Arc::clone(&self.affinity)
    }

    pub fn known_providers(&self) -> &BTreeSet<String> {
        &self.known_providers
    }

    pub fn active_request_count(&self, account: &str) -> i64 {
        self.router.active_request_count(account)
    }

    pub fn finite_coordinator(&self) -> FiniteCoordinator {
        self.finite.clone()
    }

    pub fn streaming_coordinator(&self) -> StreamingCoordinator {
        self.streaming.clone()
    }

    pub fn finalization_supervisor(&self) -> FinalizationSupervisor {
        self.finite.finalization_supervisor()
    }

    pub fn wire_resolver(&self) -> WireResolver {
        self.finite.wire_resolver()
    }

    pub fn router_handle(&self) -> RoutingRouter {
        self.router.clone()
    }

    pub fn catalog_model_ids(&self) -> Vec<String> {
        self.router.catalog_model_ids()
    }

    pub fn max_body_bytes(&self) -> usize {
        self.max_body_bytes
    }

    pub fn catalog_service(&self) -> Option<Arc<CatalogService>> {
        self.catalog_service.clone()
    }
}

/// Virtual-router resolution facts (secret-free).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VirtualResolution {
    pub virtual_model: String,
    pub route_id: String,
    pub route_label: String,
    pub concrete_model: String,
    pub decision_source: String,
    pub affinity_hit: bool,
}

/// One resolved inference request: concrete body plus optional virtual facts.
#[derive(Debug, Clone)]
pub struct ResolvedInference {
    pub concrete_body: Bytes,
    pub concrete_model: String,
    pub provider_id: Option<String>,
    pub virtual_resolution: Option<VirtualResolution>,
    pub selector_attempts: u32,
    pub selector_latency_ms: Option<f64>,
}

/// Client-visible inference outcome for metrics (secret-free).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct InferenceOutcome {
    pub status: u16,
    pub virtual_model: bool,
    pub affinity_hit: bool,
}

/// Map a finite coordinator error to a thin endpoint error.
fn map_finite_error(error: super::FiniteCoordinatorError) -> EndpointError {
    use super::FiniteCoordinatorError as Finite;
    match error {
        Finite::Admission(_) | Finite::InvalidFacts => EndpointError::Admission,
        Finite::NoEligibleRoute => EndpointError::NoEligibleRoute,
        Finite::MissingProvider { provider_id } => EndpointError::MissingProvider { provider_id },
        Finite::MissingWireProfile { provider_id } => {
            EndpointError::MissingWireProfile { provider_id }
        }
        Finite::Publication(error) => map_publication_error(error),
        Finite::Claim(_) => EndpointError::Claim,
        Finite::Attempt(_) => EndpointError::Attempt,
        Finite::Finalization(_) => EndpointError::Finalization,
        Finite::Effects(_) => EndpointError::Attempt,
    }
}

fn map_stream_error(error: super::StreamingCoordinatorError) -> EndpointError {
    use super::StreamingCoordinatorError as Stream;
    match error {
        Stream::Admission(_) | Stream::InvalidFacts => EndpointError::Admission,
        Stream::NoEligibleRoute => EndpointError::NoEligibleRoute,
        Stream::MissingProvider { provider_id } => EndpointError::MissingProvider { provider_id },
        Stream::MissingWireProfile { provider_id } => {
            EndpointError::MissingWireProfile { provider_id }
        }
        Stream::Publication(error) => map_publication_error(error),
        Stream::Claim(_) => EndpointError::Claim,
        Stream::Attempt(_) => EndpointError::Attempt,
        Stream::Finalization(_) => EndpointError::Finalization,
        Stream::Effects(_) => EndpointError::Attempt,
    }
}

fn map_publication_error(error: super::PublicationError) -> EndpointError {
    match error {
        super::PublicationError::DuplicateConflict { .. } => EndpointError::PublicationConflict,
        _ => EndpointError::Attempt,
    }
}

/// Extract and validate the model field from a parsed payload.
fn model_field(payload: &Map<String, Value>) -> Result<String, EndpointError> {
    match payload.get("model").and_then(Value::as_str) {
        Some(model) if !model.trim().is_empty() => Ok(model.to_owned()),
        _ => Err(EndpointError::MissingModel),
    }
}

/// Validate the stream flag shape (must be a boolean when present).
fn stream_flag(payload: &Map<String, Value>) -> Result<bool, EndpointError> {
    match payload.get("stream") {
        None => Ok(false),
        Some(Value::Null) => Ok(false),
        Some(Value::Bool(value)) => Ok(*value),
        Some(_) => Err(EndpointError::InvalidStream),
    }
}

fn static_routing_facts(
    known_providers: &BTreeSet<String>,
    surface: ClientSurface,
) -> StaticRoutingFacts {
    StaticRoutingFacts {
        known_provider_ids: known_providers.clone(),
        requested_protocol: Some(surface.protocol().to_owned()),
        transcode_protocols: Vec::new(),
        catalog_stale_after_s: None,
        capability_policy: BTreeMap::new(),
        now: 0,
    }
}

fn rewrite_model_field(body: &[u8], concrete_model: &str) -> Result<Bytes, EndpointError> {
    let mut value: Value = serde_json::from_slice(body).map_err(|_| EndpointError::InvalidJson)?;
    let object = value.as_object_mut().ok_or(EndpointError::InvalidJson)?;
    object.insert("model".into(), Value::String(concrete_model.to_owned()));
    serde_json::to_vec(&value)
        .map(Bytes::from)
        .map_err(|_| EndpointError::InvalidJson)
}

/// Resolve virtual aliases before concrete parsing.
///
/// Returns the concrete body/model plus optional virtual facts. The selector
/// runs through the same finite lifecycle with recursion refusal; affinity
/// is committed only for validated selections per D007.
async fn resolve_concrete(
    state: &InferenceState,
    surface: ClientSurface,
    raw_body: Bytes,
    payload: &Map<String, Value>,
    session_header: Option<&str>,
    proxy_request_id: &str,
) -> Result<ResolvedInference, EndpointError> {
    let model_value = model_field(payload)?;
    // Exact virtual-alias match comes before concrete provider parsing.
    let Some(router) = state.registry.get(model_value.trim()) else {
        let (model_id, provider_id) =
            parse_provider_qualified_model(&model_value, &state.known_providers);
        // Strip any provider qualifier for the dispatched body: the upstream
        // does not understand EggPool's namespace. The qualifier survives as
        // a routing pin, matching the Python `provider_id` context field.
        let concrete_body = if model_id != model_value.trim() {
            rewrite_model_field(&raw_body, &model_id)?
        } else {
            raw_body
        };
        return Ok(ResolvedInference {
            concrete_body,
            concrete_model: model_id,
            provider_id,
            virtual_resolution: None,
            selector_attempts: 0,
            selector_latency_ms: None,
        });
    };
    resolve_virtual(
        state,
        surface,
        raw_body,
        payload,
        &router,
        session_header,
        proxy_request_id,
    )
    .await
}

async fn resolve_virtual(
    state: &InferenceState,
    surface: ClientSurface,
    raw_body: Bytes,
    _payload: &Map<String, Value>,
    router: &CompiledModelRouter,
    session_header: Option<&str>,
    proxy_request_id: &str,
) -> Result<ResolvedInference, EndpointError> {
    // Build the early canonical view for the semantic prompt and affinity
    // identity. Admission here is bounded by the same body ceiling.
    let admitted = crate::request::admit_request(
        &raw_body,
        crate::request::AdmissionOptions {
            max_body_bytes: state.max_body_bytes,
            client_surface: surface,
            ..Default::default()
        },
    )
    .map_err(|_| EndpointError::Admission)?;
    let _ = proxy_request_id;
    let identity_input = admitted.affinity_identity(session_header);
    let identity = identity_input.session_identity();
    let known = state.known_providers.clone();
    let registry_virtual = state.registry.clone();
    let selector = SemanticSelector::new(state.finite.clone(), known)
        .with_virtual_check(move |model| registry_virtual.is_virtual(model));
    let canonical = admitted.canonical.clone();
    if router.sticky {
        if let Some(identity) = identity {
            let resolution = state
                .affinity
                .resolve(router, &identity, || {
                    let selector = selector.clone();
                    let router = router.clone();
                    let canonical = canonical.clone();
                    async move {
                        Ok(
                            selector_affinity_selection(&selector, &router, &canonical, surface)
                                .await,
                        )
                    }
                })
                .await
                .map_err(|_| EndpointError::Attempt)?;
            let source = match resolution.decision.source {
                crate::model_router::AffinityDecisionSource::Selector => SelectionSource::Selector,
                crate::model_router::AffinityDecisionSource::Default => SelectionSource::Default,
            };
            return finish_virtual_resolution(
                state,
                &raw_body,
                router,
                resolution.decision.concrete_model.clone(),
                resolution.decision.route_id.clone(),
                resolution.decision.route_label.clone(),
                source,
                resolution.cache_hit,
                resolution.decision.selector_attempts_for_metrics(),
            );
        }
    }
    // Non-sticky, no session identity, or sticky=false bypass: direct select.
    let selection = selector.select(router, &canonical, surface).await;
    finish_virtual_resolution(
        state,
        &raw_body,
        router,
        selection.concrete_model.clone(),
        selection.route_id.clone(),
        selection.route_label.clone(),
        selection.source,
        false,
        selection.selector_attempts,
    )
    .map(|mut resolved| {
        resolved.selector_latency_ms = selection.selector_latency_ms;
        resolved
    })
}

async fn selector_affinity_selection(
    selector: &SemanticSelector,
    router: &CompiledModelRouter,
    canonical: &crate::wire::ir::CanonicalRequest,
    surface: ClientSurface,
) -> AffinitySelection {
    let selection = selector.select(router, canonical, surface).await;
    AffinitySelection {
        virtual_model: selection.virtual_model.clone(),
        route_id: selection.route_id.clone(),
        route_label: selection.route_label.clone(),
        concrete_model: selection.concrete_model.clone(),
        source: match selection.source {
            SelectionSource::Selector => crate::model_router::AffinityDecisionSource::Selector,
            SelectionSource::Default => crate::model_router::AffinityDecisionSource::Default,
        },
    }
}

#[allow(clippy::too_many_arguments)]
fn finish_virtual_resolution(
    state: &InferenceState,
    raw_body: &[u8],
    router: &CompiledModelRouter,
    concrete_model: String,
    route_id: String,
    route_label: String,
    source: SelectionSource,
    affinity_hit: bool,
    selector_attempts: u32,
) -> Result<ResolvedInference, EndpointError> {
    // Aliases never semantic-failover after submission: the concrete target
    // must not itself be virtual.
    if state.registry.is_virtual(&concrete_model) {
        return Err(EndpointError::Admission);
    }
    let Some(route) = router.route_for_id(&route_id) else {
        return Err(EndpointError::Admission);
    };
    if route.label != route_label || route.model != concrete_model {
        return Err(EndpointError::Admission);
    }
    let concrete_body = rewrite_model_field(raw_body, &concrete_model)?;
    let (_, provider_id) = parse_provider_qualified_model(&concrete_model, &state.known_providers);
    // Strip any provider qualifier from the dispatched model: the upstream
    // does not understand EggPool's namespace.
    let (dispatch_model, _) =
        parse_provider_qualified_model(&concrete_model, &state.known_providers);
    let concrete_body = if dispatch_model != concrete_model {
        rewrite_model_field(&concrete_body, &dispatch_model)?
    } else {
        concrete_body
    };
    Ok(ResolvedInference {
        concrete_body,
        concrete_model: dispatch_model,
        provider_id,
        virtual_resolution: Some(VirtualResolution {
            virtual_model: router.virtual_model.clone(),
            route_id,
            route_label,
            concrete_model,
            decision_source: source.as_str().to_owned(),
            affinity_hit,
        }),
        selector_attempts,
        selector_latency_ms: None,
    })
}

/// Execute one finite inference request through the thin endpoint path.
pub async fn execute_finite(
    state: &InferenceState,
    surface: ClientSurface,
    raw_body: Bytes,
    incoming_headers: HeaderMap,
    session_header: Option<String>,
    proxy_request_id: String,
) -> Result<(FiniteExecution, Option<VirtualResolution>), EndpointError> {
    if raw_body.len() > state.max_body_bytes {
        return Err(EndpointError::BodyTooLarge);
    }
    let value: Value = serde_json::from_slice(&raw_body).map_err(|_| EndpointError::InvalidJson)?;
    let payload = value.as_object().ok_or(EndpointError::InvalidJson)?.clone();
    if surface == ClientSurface::Responses {
        if let Some(rejection) = validate_responses_stateless(&payload) {
            return Err(EndpointError::StatelessViolation(rejection));
        }
    }
    if stream_flag(&payload)? {
        return Err(EndpointError::Admission);
    }
    let resolved = resolve_concrete(
        state,
        surface,
        raw_body.clone(),
        &payload,
        session_header.as_deref(),
        &proxy_request_id,
    )
    .await?;
    let mut request = FiniteRequest::new(
        proxy_request_id,
        resolved.concrete_body.clone(),
        incoming_headers,
        surface,
        static_routing_facts(&state.known_providers, surface),
    )
    .map_err(|_| EndpointError::Admission)?;
    // The resolved concrete model must match admission; a mismatch is a
    // fail-closed programming error, never silent failover.
    if request.admitted.canonical.model != resolved.concrete_model {
        return Err(EndpointError::Admission);
    }
    // Preserve an explicit provider qualifier (`model/provider`) as a routing
    // pin. Admission carries the stripped canonical model; the qualifier
    // survives only here, matching the Python `provider_id` context field.
    request.routing_facts.provider_id = resolved.provider_id.clone();
    let execution = state
        .finite
        .execute(request)
        .await
        .map_err(map_finite_error)?;
    Ok((execution, resolved.virtual_resolution))
}

/// Execute one streaming inference request through the thin endpoint path.
pub async fn execute_stream(
    state: &InferenceState,
    surface: ClientSurface,
    raw_body: Bytes,
    incoming_headers: HeaderMap,
    session_header: Option<String>,
    proxy_request_id: String,
) -> Result<(StreamingExecution, Option<VirtualResolution>), EndpointError> {
    if raw_body.len() > state.max_body_bytes {
        return Err(EndpointError::BodyTooLarge);
    }
    let value: Value = serde_json::from_slice(&raw_body).map_err(|_| EndpointError::InvalidJson)?;
    let payload = value.as_object().ok_or(EndpointError::InvalidJson)?.clone();
    if surface == ClientSurface::Responses {
        if let Some(rejection) = validate_responses_stateless(&payload) {
            return Err(EndpointError::StatelessViolation(rejection));
        }
    }
    if !stream_flag(&payload)? {
        return Err(EndpointError::Admission);
    }
    let resolved = resolve_concrete(
        state,
        surface,
        raw_body.clone(),
        &payload,
        session_header.as_deref(),
        &proxy_request_id,
    )
    .await?;
    let mut request = StreamRequest::new(
        proxy_request_id,
        resolved.concrete_body.clone(),
        incoming_headers,
        surface,
        static_routing_facts(&state.known_providers, surface),
    )
    .map_err(|_| EndpointError::Admission)?;
    if request.admitted.canonical.model != resolved.concrete_model {
        return Err(EndpointError::Admission);
    }
    request.routing_facts.provider_id = resolved.provider_id.clone();
    let execution = state
        .streaming
        .execute(request)
        .await
        .map_err(map_stream_error)?;
    Ok((execution, resolved.virtual_resolution))
}

/// Build a production [`InferenceState`] from validated config and open DB.
///
/// Seeds the catalog from static models plus durable model rows, builds the
/// account registry from durable accounts, and wires the finite/streaming
/// coordinators with the configured retry budget. Failures to resolve
/// provider wire profiles are fail-closed.
pub(crate) async fn build_inference_state_with_shared(
    config: &Config,
    database: &Database,
    client_pool: ProviderClientPool,
    wire_resolver: WireResolver,
    affinity: Arc<ModelRouterAffinity>,
    model_registry: ModelRouterRegistry,
    provider_profiles: BTreeMap<String, Vec<ConfiguredWireProfile>>,
) -> Result<InferenceState, String> {
    build_inference_state_with_shared_and_accounts(
        config,
        database,
        client_pool,
        wire_resolver,
        affinity,
        model_registry,
        provider_profiles,
        None,
    )
    .await
}

/// Variant used by the reload transaction after it has preflighted the
/// durable account identities but before the SQLite acceptance transaction.
/// New account ids are therefore part of the candidate snapshot without
/// mutating the active database during candidate construction.
#[allow(clippy::too_many_arguments)]
pub(crate) async fn build_inference_state_with_shared_and_accounts(
    config: &Config,
    database: &Database,
    client_pool: ProviderClientPool,
    wire_resolver: WireResolver,
    affinity: Arc<ModelRouterAffinity>,
    model_registry: ModelRouterRegistry,
    provider_profiles: BTreeMap<String, Vec<ConfiguredWireProfile>>,
    durable_accounts_override: Option<Vec<Account>>,
) -> Result<InferenceState, String> {
    let credentials = CredentialStore::from_config(config);
    let durable_accounts: Vec<Account> = match durable_accounts_override {
        Some(accounts) => accounts,
        None => database
            .call(|connection| {
                let mut statement = connection
                    .prepare(
                        "SELECT id, name, api_key_env, enabled, weight, provider_id FROM accounts",
                    )
                    .map_err(|error| {
                        tokio_rusqlite::rusqlite::Error::ToSqlConversionFailure(Box::new(error))
                    })?;
                let rows = statement
                    .query_map([], |row| {
                        Ok(Account {
                            id: row.get(0)?,
                            name: row.get(1)?,
                            api_key_env: row.get(2)?,
                            enabled: row.get(3)?,
                            weight: row.get(4)?,
                            provider_id: row.get(5)?,
                        })
                    })
                    .map_err(|error| {
                        tokio_rusqlite::rusqlite::Error::ToSqlConversionFailure(Box::new(error))
                    })?;
                let mut accounts = Vec::new();
                for row in rows {
                    accounts.push(row.map_err(|error| {
                        tokio_rusqlite::rusqlite::Error::ToSqlConversionFailure(Box::new(error))
                    })?);
                }
                Ok(accounts)
            })
            .await
            .map_err(|error| format!("inference account load failed: {error}"))?,
    };
    let registry = AccountRegistry::from_config(config, &durable_accounts, &credentials)
        .map_err(|error| format!("inference registry failed: {error}"))?;
    let mut catalog = ModelCatalogCache::default();
    catalog.set_config(config);
    let seeded = catalog.seed_static_models(config).unwrap_or(0);
    let _ = seeded;
    // Hydrate provider mapping plus any static models for routing.
    for (provider_id, provider) in &config.providers {
        for account in &provider.accounts {
            catalog.set_account_provider(&account.name, provider_id);
        }
    }
    // Seed durable model rows so routing has candidates without refresh.
    let durable_models: Vec<(String, String, String)> = database
        .call(|connection| {
            let mut statement = connection
                .prepare("SELECT model_id, protocol, provider_id FROM models")
                .map_err(|error| {
                    tokio_rusqlite::rusqlite::Error::ToSqlConversionFailure(Box::new(error))
                })?;
            let rows = statement
                .query_map([], |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, String>(1)?,
                        row.get::<_, String>(2)?,
                    ))
                })
                .map_err(|error| {
                    tokio_rusqlite::rusqlite::Error::ToSqlConversionFailure(Box::new(error))
                })?;
            let mut models = Vec::new();
            for row in rows {
                models.push(row.map_err(|error| {
                    tokio_rusqlite::rusqlite::Error::ToSqlConversionFailure(Box::new(error))
                })?);
            }
            Ok(models)
        })
        .await
        .unwrap_or_default();
    for (model_id, protocol, provider_id) in &durable_models {
        let mut input = ModelInput::new(model_id.clone());
        input.protocol = Some(protocol.clone());
        input.protocol_source = Some("durable".into());
        input.resolution_status = ProtocolResolutionStatus::Resolved;
        // Attach to every account of the recorded provider.
        if let Some(provider) = config.providers.get(provider_id) {
            for account in &provider.accounts {
                let _ = catalog.update_from_account(
                    &account.name,
                    provider_id,
                    std::slice::from_ref(&input),
                    true,
                    true,
                );
            }
        }
    }
    let mut quotas = Vec::new();
    for account in registry.all() {
        quotas.push(AccountQuota::new(account.account_name.clone()));
    }
    let estimator = QuotaEstimator::new(quotas);
    let shared_catalog = Arc::new(std::sync::Mutex::new(catalog));
    let catalog_service = Arc::new(CatalogService::with_shared_cache(
        config.clone(),
        registry.clone(),
        database.clone(),
        client_pool.clone(),
        credentials.clone(),
        Arc::clone(&shared_catalog),
    ));
    let router = RoutingRouter::with_shared_catalog(
        registry,
        shared_catalog,
        estimator,
        None,
        EligibilityPolicy::from_config(config),
    );
    let wire = WireRuntime::embedded().map_err(|error| format!("wire runtime failed: {error}"))?;
    let attempts = AttemptBuilder::new(client_pool, wire.clone());
    let publication = PublicationService::new(database.clone());
    let supervisor = FinalizationSupervisor::new(DurableFinalizer::new(database.clone()));
    let mut providers: BTreeMap<String, ProviderConfig> = BTreeMap::new();
    for (provider_id, provider) in &config.providers {
        providers.insert(provider_id.clone(), provider.clone());
    }
    let retry_policy = RetryPolicy {
        max_attempts: config
            .routing
            .max_retries_before_stream
            .max(1)
            .saturating_add(1),
        ..RetryPolicy::default()
    };
    let finite = FiniteCoordinator::new(
        router.clone(),
        publication.clone(),
        attempts.clone(),
        wire.clone(),
        wire_resolver.clone(),
        provider_profiles.clone(),
        providers.clone(),
        credentials.clone(),
        supervisor.clone(),
        retry_policy,
    );
    let streaming = StreamingCoordinator::new(
        router.clone(),
        publication,
        attempts,
        wire,
        wire_resolver,
        provider_profiles,
        providers.clone(),
        credentials,
        supervisor,
        retry_policy,
    );
    let known_providers: BTreeSet<String> = config.providers.keys().cloned().collect();
    Ok(InferenceState::from_parts_with_catalog_service(
        finite,
        streaming,
        model_registry,
        affinity,
        known_providers,
        config.server.max_request_body_bytes as usize,
        router,
        Some(catalog_service),
    ))
}

/// Compile all immutable provider wire candidates before allocating the
/// generation's client pool.  The fallback preserves the M7 behavior for
/// simple single-protocol fixtures that omit explicit wire surfaces.
pub(crate) fn compile_provider_profiles(
    config: &Config,
) -> Result<BTreeMap<String, Vec<ConfiguredWireProfile>>, String> {
    let wire_registry = WireProfileRegistry::embedded()
        .map_err(|error| format!("wire registry failed: {error}"))?;
    let mut provider_profiles: BTreeMap<String, Vec<ConfiguredWireProfile>> = BTreeMap::new();
    for (provider_id, provider) in &config.providers {
        let profiles = wire_registry
            .configured_profiles(&provider.wire_surfaces)
            .map_err(|error| format!("wire profiles for {provider_id:?} failed: {error}"))?;
        if profiles.is_empty() {
            let fallback = fallback_profiles(provider_id, provider);
            if !fallback.is_empty() {
                provider_profiles.insert(provider_id.clone(), fallback);
            }
        } else {
            provider_profiles.insert(provider_id.clone(), profiles);
        }
    }
    Ok(provider_profiles)
}

fn fallback_profiles(provider_id: &str, provider: &ProviderConfig) -> Vec<ConfiguredWireProfile> {
    let _ = provider_id;
    let mut profiles = Vec::new();
    for protocol in &provider.protocols {
        let surface = match protocol.as_str() {
            "anthropic" => WireSurface::AnthropicMessages,
            _ => WireSurface::OpenaiChatCompletions,
        };
        let (request_codec, response_codec, stream_codec) = match surface {
            WireSurface::OpenaiChatCompletions => (
                WireCodecId::OpenaiChat,
                WireCodecId::OpenaiChat,
                WireCodecId::OpenaiChatSse,
            ),
            WireSurface::AnthropicMessages => (
                WireCodecId::AnthropicMessages,
                WireCodecId::AnthropicMessages,
                WireCodecId::AnthropicMessagesSse,
            ),
            _ => continue,
        };
        profiles.push(ConfiguredWireProfile {
            definition: WireProfileDefinition {
                surface,
                request_codec,
                response_codec,
                stream_codec,
            },
            path_template: if surface == WireSurface::AnthropicMessages {
                provider.anthropic_path.clone()
            } else {
                provider.openai_path.clone()
            },
            stream_path_template: None,
            priority: 0,
        });
    }
    profiles
}

/// Build an Axum-ready finite response triple from an execution.
pub fn build_stream_response_headers(
    status: StatusCode,
    headers: &[(http::HeaderName, HeaderValue)],
    proxy_request_id: &str,
    attempt_count: u32,
) -> HeaderMap {
    let mut outgoing = HeaderMap::new();
    for (name, value) in headers {
        outgoing.insert(name.clone(), value.clone());
    }
    if let Ok(value) = HeaderValue::try_from(proxy_request_id) {
        outgoing.insert("x-proxy-request-id", value);
    }
    if let Ok(value) = HeaderValue::try_from(attempt_count.to_string()) {
        outgoing.insert("x-proxy-attempt-count", value);
    }
    let _ = status;
    outgoing
}

static PROXY_REQUEST_COUNTER: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(1);

/// Generate one proxy request ID (unique, opaque, secret-free).
pub fn new_proxy_request_id() -> String {
    let counter = PROXY_REQUEST_COUNTER.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or(counter as u128);
    format!("proxy-{nanos:x}-{counter:x}")
}

// Extend affinity decisions with selector attempt counts for metrics without
// exposing per-session history.
trait AffinityMetrics {
    fn selector_attempts_for_metrics(&self) -> u32;
}

impl AffinityMetrics for crate::model_router::AffinityDecision {
    fn selector_attempts_for_metrics(&self) -> u32 {
        // The Rust affinity cache stores only the validated decision, not the
        // selector attempt breakdown; count a sticky hit as zero new attempts
        // and a miss as one logical selection. The exact attempt/repair
        // breakdown is carried on `ModelSelection` for direct selections.
        0
    }
}
