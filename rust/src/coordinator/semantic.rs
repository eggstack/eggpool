//! C009 semantic model-router selector dispatch.
//!
//! This module ports the D007 deferred semantic selector integration that
//! intentionally waited for M7. It compiles one deterministic bounded prompt
//! from the admitted canonical request, dispatches it as a typed internal
//! concrete-model request through the same [`FiniteCoordinator`] lifecycle,
//! and falls back deterministically without leaking prompts or bodies.
//!
//! ## Boundaries
//!
//! - No HTTP loopback, RPC framework, or new web stack. Internal dispatch
//!   constructs a [`FiniteRequest`] directly and calls one coordinator entry
//!   point.
//! - Recursion is structurally and runtime guarded: the selector model must
//!   not be a virtual alias, concrete route targets must not be virtual, and
//!   internal selector requests never re-enter virtual resolution (depth is
//!   exactly one).
//! - Budgets are bounded and frozen: `selector_timeout_s` covers the initial
//!   request plus at most one repair, `max_input_bytes` bounds the variable
//!   semantic text, `max_response_bytes` (16 KiB) bounds route-ID parsing, and
//!   `repair_attempts` is 0 or 1.
//! - Affinity is **not** committed here. The caller (endpoints layer) owns
//!   [`ModelRouterAffinity`] resolution and commits only validated selections
//!   per the D007 contract. This module only returns a validated
//!   [`ModelSelection`].
//! - Observability is secret-free: diagnostics carry outcome labels, attempt
//!   numbers, byte counts, and elapsed times only. No prompt text, response
//!   body, session header, or credential ever enters diagnostics or `Debug`.

use std::{
    collections::{BTreeMap, BTreeSet},
    sync::atomic::{AtomicU64, Ordering},
    time::{Duration, Instant},
};

use bytes::Bytes;
use http::HeaderMap;
use serde_json::{Map, Value, json};
use thiserror::Error;

use crate::{
    model_router::CompiledModelRouter,
    request::StaticRoutingFacts,
    wire::ir::{CanonicalBlockKind, CanonicalRequest, CanonicalRole, ClientSurface},
};

use super::{FiniteCoordinator, FiniteRequest};

/// Maximum selector response body accepted for route-ID parsing (Python parity).
pub const SELECTOR_MAX_RESPONSE_BYTES: usize = 16 * 1024;

/// Truncation marker used by the Python oracle (`"\n[… ]\n"`).
const TRUNCATION_MARKER: &str = "\n[… ]\n";

/// Fallback reason vocabulary (Python `FallbackReason` parity).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SelectorFallback {
    Timeout,
    Unavailable,
    InvalidOutput,
    RepairFailed,
}

impl SelectorFallback {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Timeout => "timeout",
            Self::Unavailable => "unavailable",
            Self::InvalidOutput => "invalid_output",
            Self::RepairFailed => "repair_failed",
        }
    }
}

/// Decision source vocabulary (Python `ModelSelection.source` parity).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SelectionSource {
    Selector,
    Default,
}

impl SelectionSource {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Selector => "selector",
            Self::Default => "default",
        }
    }
}

/// One validated semantic route decision.
#[derive(Debug, Clone, PartialEq)]
pub struct ModelSelection {
    pub virtual_model: String,
    pub route_id: String,
    pub route_label: String,
    pub concrete_model: String,
    pub source: SelectionSource,
    pub selector_attempts: u32,
    pub selector_latency_ms: Option<f64>,
    pub fallback_reason: Option<SelectorFallback>,
    pub repair_attempted: bool,
    pub repair_succeeded: bool,
}

/// Secret-free selector diagnostics for one `select` call.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SelectorDiagnostics {
    pub attempts: u32,
    pub fallback: Option<SelectorFallback>,
    pub repair_attempted: bool,
    pub repair_succeeded: bool,
    pub source_selector: bool,
    pub response_bytes: usize,
    pub elapsed_ms: i64,
}

#[derive(Debug, Error)]
pub enum SemanticError {
    #[error("selector model {model:?} is a virtual alias; recursion refused")]
    RecursiveSelector { model: String },
    #[error("selector concrete target {model:?} is a virtual alias; recursion refused")]
    RecursiveTarget { model: String },
    #[error("unsupported selector client surface")]
    UnsupportedSurface,
}

static SELECTOR_REQUEST_COUNTER: AtomicU64 = AtomicU64::new(1);

fn next_selector_request_id(virtual_model: &str) -> String {
    let counter = SELECTOR_REQUEST_COUNTER.fetch_add(1, Ordering::Relaxed);
    format!(
        "selector-{virtual_model}-{}-{counter}",
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|duration| duration.as_nanos())
            .unwrap_or(counter as u128),
    )
}

/// Normalize transport whitespace without changing Unicode/code points.
///
/// CRLF/CR become LF, horizontal ASCII whitespace collapses to one space,
/// repeated blank lines are bounded to one, and outer edges are trimmed.
pub fn normalize_selector_text(value: &str) -> String {
    let normalized = value.replace("\r\n", "\n").replace('\r', "\n");
    let mut lines = Vec::new();
    for line in normalized.split('\n') {
        let mut collapsed = String::with_capacity(line.len());
        let mut in_space = false;
        for character in line.chars() {
            if matches!(character, '\t' | '\u{000B}' | '\u{000C}' | ' ') {
                if !in_space {
                    collapsed.push(' ');
                }
                in_space = true;
            } else {
                collapsed.push(character);
                in_space = false;
            }
        }
        lines.push(collapsed.trim().to_owned());
    }
    let mut result: Vec<String> = Vec::with_capacity(lines.len());
    let mut previous_blank = false;
    for line in lines {
        let blank = line.is_empty();
        if blank && previous_blank {
            continue;
        }
        result.push(line);
        previous_blank = blank;
    }
    result.join("\n").trim().to_owned()
}

/// Truncate on UTF-8 boundaries while retaining head and tail.
pub fn truncate_utf8(value: &str, max_bytes: usize) -> String {
    let encoded = value.as_bytes();
    if encoded.len() <= max_bytes {
        return value.to_owned();
    }
    let marker = TRUNCATION_MARKER.as_bytes();
    if max_bytes <= marker.len() {
        return truncate_head_only(value, max_bytes);
    }
    let available = max_bytes - marker.len();
    let head_budget = available * 3 / 4;
    let tail_budget = available - head_budget;
    let head = decode_prefix(encoded, head_budget);
    let tail = decode_suffix(encoded, tail_budget);
    let mut result = format!("{head}{TRUNCATION_MARKER}{tail}");
    while result.len() > max_bytes {
        result.pop();
    }
    result
}

fn truncate_head_only(value: &str, max_bytes: usize) -> String {
    let bytes = value.as_bytes();
    let mut end = max_bytes.min(bytes.len());
    while end > 0 && !value.is_char_boundary(end) {
        end -= 1;
    }
    String::from_utf8_lossy(&bytes[..end]).into_owned()
}

fn decode_prefix(bytes: &[u8], budget: usize) -> String {
    let mut end = budget.min(bytes.len());
    let text = String::from_utf8_lossy(bytes);
    while end > 0 && !text.is_char_boundary(end) {
        end -= 1;
    }
    // `from_utf8_lossy` never panics; re-slice on the lossy view would hide
    // partial code points. Decode the raw prefix with replacement instead.
    String::from_utf8_lossy(&bytes[..end]).into_owned()
}

fn decode_suffix(bytes: &[u8], budget: usize) -> String {
    let start = bytes.len().saturating_sub(budget);
    let mut adjusted = start;
    let text = String::from_utf8_lossy(bytes);
    while adjusted < bytes.len() && !text.is_char_boundary(adjusted) {
        adjusted += 1;
    }
    String::from_utf8_lossy(&bytes[adjusted..]).into_owned()
}

fn message_texts(request: &CanonicalRequest, role: CanonicalRole) -> Vec<String> {
    request
        .messages
        .iter()
        .filter(|message| message.role == role)
        .map(|message| message.text())
        .filter(|text| !text.is_empty())
        .collect()
}

fn feature_flags(request: &CanonicalRequest) -> Vec<&'static str> {
    let mut flags = Vec::new();
    if !request.tools.is_empty() {
        flags.push("tools");
    }
    let mut has_image = false;
    let mut has_document = false;
    let mut has_audio = false;
    for message in &request.messages {
        for block in &message.content {
            match block.kind {
                CanonicalBlockKind::Image => has_image = true,
                CanonicalBlockKind::Document => has_document = true,
                CanonicalBlockKind::Audio => has_audio = true,
                _ => {}
            }
        }
    }
    if has_image {
        flags.push("image");
    }
    if has_document {
        flags.push("pdf");
    }
    if has_audio {
        flags.push("audio");
    }
    if request.reasoning.requested.is_some() {
        flags.push("reasoning");
    }
    flags
}

/// Build the bounded-independent semantic text before byte truncation.
pub fn build_semantic_view(request: &CanonicalRequest) -> String {
    let mut parts = Vec::new();
    let mut system = message_texts(request, CanonicalRole::System);
    system.extend(message_texts(request, CanonicalRole::Developer));
    if !system.is_empty() {
        parts.push(format!(
            "system: {}",
            normalize_selector_text(&system.join("\n"))
        ));
    }
    let users: Vec<String> = request
        .messages
        .iter()
        .filter(|message| message.role == CanonicalRole::User)
        .map(|message| message.text())
        .collect();
    let last_user = users
        .into_iter()
        .next_back()
        .filter(|text| !text.trim().is_empty());
    if let Some(user) = last_user {
        parts.push(format!("user: {}", normalize_selector_text(&user)));
    } else {
        let assistants = message_texts(request, CanonicalRole::Assistant);
        if let Some(last) = assistants.into_iter().next_back() {
            parts.push(format!("context: {}", normalize_selector_text(&last)));
        }
    }
    let flags = feature_flags(request);
    if !flags.is_empty() {
        parts.push(format!("features: {}", flags.join(",")));
    }
    parts.join("\n")
}

/// Complete internal Chat Completions-shaped selector payload.
#[derive(Debug, Clone)]
pub struct SelectorPrompt {
    pub payload: Map<String, Value>,
    pub static_prefix: String,
    pub variable_text: String,
}

/// Compile one deterministic selector request without I/O or routing.
pub fn compile_selector_prompt(
    router: &CompiledModelRouter,
    request: &CanonicalRequest,
    client_surface: ClientSurface,
) -> Result<SelectorPrompt, SemanticError> {
    match client_surface {
        ClientSurface::ChatCompletions | ClientSurface::Messages | ClientSurface::Responses => {}
    }
    let static_prefix = String::from_utf8(router.static_policy.to_vec())
        .unwrap_or_else(|_| String::from("model-router/v1|choose id;reply id only"));
    let variable_text = truncate_utf8(
        &build_semantic_view(request),
        router.max_input_bytes as usize,
    );
    let mut messages = vec![json!({"role": "system", "content": static_prefix})];
    if !variable_text.is_empty() {
        messages.push(json!({"role": "user", "content": variable_text}));
    }
    let mut payload = Map::new();
    payload.insert("model".into(), Value::String(router.selector_model.clone()));
    payload.insert("messages".into(), Value::Array(messages));
    payload.insert("stream".into(), Value::Bool(false));
    payload.insert("max_tokens".into(), json!(16));
    Ok(SelectorPrompt {
        payload,
        static_prefix,
        variable_text,
    })
}

/// Build a fixed repair request while retaining the initial context.
pub fn compile_repair_prompt(
    router: &CompiledModelRouter,
    initial: &SelectorPrompt,
) -> Map<String, Value> {
    let mut route_ids: Vec<&String> = router.route_by_id.keys().collect();
    route_ids.sort_by_key(|key| key.parse::<usize>().unwrap_or(usize::MAX));
    let route_ids = route_ids
        .into_iter()
        .map(String::as_str)
        .collect::<Vec<_>>()
        .join("|");
    let mut messages = vec![json!({"role": "system", "content": initial.static_prefix})];
    if !initial.variable_text.is_empty() {
        messages.push(json!({"role": "user", "content": initial.variable_text}));
    }
    messages.push(json!({"role": "user", "content": format!("invalid;reply only:{route_ids}")}));
    let mut payload = Map::new();
    payload.insert("model".into(), Value::String(router.selector_model.clone()));
    payload.insert("messages".into(), Value::Array(messages));
    payload.insert("stream".into(), Value::Bool(false));
    payload.insert("max_tokens".into(), json!(16));
    payload
}

/// Return an exact route ID from a bounded OpenAI-chat response body.
pub fn parse_route_id(
    body: Option<&[u8]>,
    router: &CompiledModelRouter,
    max_response_bytes: usize,
) -> Option<String> {
    let body = body?;
    if body.len() > max_response_bytes {
        return None;
    }
    let response: Value = serde_json::from_slice(body).ok()?;
    let object = response.as_object()?;
    let choices = object.get("choices")?.as_array()?;
    if choices.len() != 1 {
        return None;
    }
    let choice = choices.first()?.as_object()?;
    let message = choice.get("message")?.as_object()?;
    let content = message.get("content")?;
    let text = if let Some(text) = content.as_str() {
        text.to_owned()
    } else {
        let blocks = content.as_array()?;
        let mut chunks = String::new();
        for block in blocks {
            let block = block.as_object()?;
            if block.get("type")?.as_str()? != "text" {
                return None;
            }
            chunks.push_str(block.get("text")?.as_str()?);
        }
        chunks
    };
    let candidate = text.trim().to_owned();
    router
        .route_by_id
        .contains_key(&candidate)
        .then_some(candidate)
}

/// Bounded semantic selector that dispatches through the finite coordinator.
///
/// The selector owns prompt compilation, one bounded internal dispatch plus at
/// most one repair, deterministic fallback, and recursion refusal. Affinity
/// commit remains the caller's responsibility.
#[derive(Clone)]
pub struct SemanticSelector {
    coordinator: FiniteCoordinator,
    known_providers: BTreeSet<String>,
    max_response_bytes: usize,
    is_virtual: std::sync::Arc<dyn Fn(&str) -> bool + Send + Sync>,
}

impl std::fmt::Debug for SemanticSelector {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("SemanticSelector")
            .field("known_providers", &self.known_providers)
            .field("max_response_bytes", &self.max_response_bytes)
            .finish()
    }
}

impl SemanticSelector {
    pub fn new(coordinator: FiniteCoordinator, known_providers: BTreeSet<String>) -> Self {
        Self {
            coordinator,
            known_providers,
            max_response_bytes: SELECTOR_MAX_RESPONSE_BYTES,
            is_virtual: std::sync::Arc::new(|_| false),
        }
    }

    pub fn with_virtual_check(
        mut self,
        check: impl Fn(&str) -> bool + Send + Sync + 'static,
    ) -> Self {
        self.is_virtual = std::sync::Arc::new(check);
        self
    }

    pub fn with_max_response_bytes(mut self, limit: usize) -> Self {
        self.max_response_bytes = limit.max(1);
        self
    }

    /// Resolve one compiled router using bounded internal selector calls.
    ///
    /// The timeout covers the initial request and the optional repair. A
    /// cancellation from the parent task is not converted into a default
    /// decision; an internal timeout is converted only after the
    /// coordinator's cleanup has completed.
    pub async fn select(
        &self,
        router: &CompiledModelRouter,
        request: &CanonicalRequest,
        client_surface: ClientSurface,
    ) -> ModelSelection {
        let started = Instant::now();
        // Structural + runtime recursion refusal before any dispatch.
        if (self.is_virtual)(router.selector_model.as_str()) {
            return self.fallback(
                router,
                started,
                0,
                SelectorFallback::Unavailable,
                false,
                false,
            );
        }
        for route in router.routes.iter() {
            if (self.is_virtual)(route.model.as_str()) {
                return self.fallback(
                    router,
                    started,
                    0,
                    SelectorFallback::Unavailable,
                    false,
                    false,
                );
            }
        }
        let prompt = match compile_selector_prompt(router, request, client_surface) {
            Ok(prompt) => prompt,
            Err(_) => {
                return self.fallback(
                    router,
                    started,
                    0,
                    SelectorFallback::Unavailable,
                    false,
                    false,
                );
            }
        };
        let timeout = Duration::try_from_secs_f64(router.selector_timeout_s)
            .unwrap_or(Duration::from_secs(2));
        match tokio::time::timeout(timeout, self.select_inner(router, &prompt, started)).await {
            Ok(selection) => selection,
            Err(_) => self.fallback(router, started, 1, SelectorFallback::Timeout, false, false),
        }
    }

    async fn select_inner(
        &self,
        router: &CompiledModelRouter,
        prompt: &SelectorPrompt,
        started: Instant,
    ) -> ModelSelection {
        let attempts: u32 = 1;
        // Initial bounded internal dispatch through the same lifecycle.
        let initial = self.execute_selector(router, &prompt.payload).await;
        let (status, body) = match initial {
            Ok((status, body)) => (status, body),
            Err(_) => {
                return self.fallback(
                    router,
                    started,
                    attempts,
                    SelectorFallback::Unavailable,
                    false,
                    false,
                );
            }
        };
        if !(200..300).contains(&status) {
            return self.fallback(
                router,
                started,
                attempts,
                SelectorFallback::Unavailable,
                false,
                false,
            );
        }
        if let Some(route_id) = parse_route_id(Some(&body), router, self.max_response_bytes) {
            if let Some(route) = router.route_for_id(&route_id) {
                // Concrete target must not be virtual (structural guard).
                if (self.is_virtual)(route.model.as_str()) {
                    return self.fallback(
                        router,
                        started,
                        attempts,
                        SelectorFallback::Unavailable,
                        false,
                        false,
                    );
                }
                return self.selection(
                    router,
                    &route_id,
                    &route.label,
                    &route.model,
                    SelectionSource::Selector,
                    attempts,
                    started,
                    None,
                    false,
                    false,
                );
            }
        }
        // 2xx but invalid: exactly one repair with the same bounded context.
        if router.repair_attempts > 0 {
            let attempts = 2;
            let repair = compile_repair_prompt(router, prompt);
            let repaired = self.execute_selector(router, &repair).await;
            let (status, body) = match repaired {
                Ok(value) => value,
                Err(_) => {
                    return self.fallback(
                        router,
                        started,
                        attempts,
                        SelectorFallback::Unavailable,
                        true,
                        false,
                    );
                }
            };
            if !(200..300).contains(&status) {
                return self.fallback(
                    router,
                    started,
                    attempts,
                    SelectorFallback::Unavailable,
                    true,
                    false,
                );
            }
            if let Some(route_id) = parse_route_id(Some(&body), router, self.max_response_bytes) {
                if let Some(route) = router.route_for_id(&route_id) {
                    if (self.is_virtual)(route.model.as_str()) {
                        return self.fallback(
                            router,
                            started,
                            attempts,
                            SelectorFallback::Unavailable,
                            true,
                            false,
                        );
                    }
                    return self.selection(
                        router,
                        &route_id,
                        &route.label,
                        &route.model,
                        SelectionSource::Selector,
                        attempts,
                        started,
                        None,
                        true,
                        true,
                    );
                }
            }
            return self.fallback(
                router,
                started,
                attempts,
                SelectorFallback::RepairFailed,
                true,
                false,
            );
        }
        self.fallback(
            router,
            started,
            attempts,
            SelectorFallback::InvalidOutput,
            false,
            false,
        )
    }

    /// Typed internal dispatch without HTTP loopback.
    ///
    /// Builds a non-streaming Chat `FiniteRequest` for the selector payload
    /// and executes it through the shared finite coordinator. Returns the
    /// upstream status and raw body bytes (bounded by the coordinator's own
    /// provider-body limit).
    async fn execute_selector(
        &self,
        router: &CompiledModelRouter,
        payload: &Map<String, Value>,
    ) -> Result<(u16, Bytes), SelectorDispatchError> {
        let mut body_payload = payload.clone();
        // The selector payload model must exactly match the compiled
        // selector model; a virtual payload model is refused without I/O.
        let supplied = body_payload
            .get("model")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_owned();
        if supplied != router.selector_model {
            return Err(SelectorDispatchError::ModelMismatch);
        }
        if (self.is_virtual)(supplied.as_str()) {
            return Err(SelectorDispatchError::Recursive);
        }
        // Strip a provider qualifier for dispatch: the upstream does not
        // understand EggPool's namespace. The qualifier survives as a routing
        // pin, matching the Python `provider_id` context field.
        let (dispatch_model, provider_pin) =
            crate::catalog::ModelCatalogCache::parse_model_provider(
                &supplied,
                &self.known_providers,
            );
        if dispatch_model != supplied {
            body_payload.insert("model".into(), Value::String(dispatch_model.clone()));
        }
        let body = serde_json::to_vec(&Value::Object(body_payload))
            .map_err(|_| SelectorDispatchError::Encode)?;
        let mut request = FiniteRequest::new(
            next_selector_request_id(&router.virtual_model),
            Bytes::from(body),
            HeaderMap::new(),
            ClientSurface::ChatCompletions,
            StaticRoutingFacts {
                known_provider_ids: self.known_providers.clone(),
                requested_protocol: Some("openai".into()),
                transcode_protocols: Vec::new(),
                catalog_stale_after_s: None,
                capability_policy: BTreeMap::new(),
                now: 0,
            },
        )
        .map_err(|_| SelectorDispatchError::Admission)?;
        request.routing_facts.provider_id = provider_pin;
        let execution = self
            .coordinator
            .execute(request)
            .await
            .map_err(|_| SelectorDispatchError::Dispatch)?;
        let status = execution.response.status.as_u16();
        let body = execution.response.body.clone();
        // Converge retained C006 ownership before reading the route ID so a
        // selector dispatch never strands a converted claim. The execution
        // is already past handoff; mark start then complete as delivered.
        execution.mark_started();
        let _ = execution.complete(super::DownstreamResult::Delivered).await;
        Ok((status, body))
    }

    #[allow(clippy::too_many_arguments)]
    fn selection(
        &self,
        router: &CompiledModelRouter,
        route_id: &str,
        route_label: &str,
        concrete_model: &str,
        source: SelectionSource,
        attempts: u32,
        started: Instant,
        fallback: Option<SelectorFallback>,
        repair_attempted: bool,
        repair_succeeded: bool,
    ) -> ModelSelection {
        ModelSelection {
            virtual_model: router.virtual_model.clone(),
            route_id: route_id.to_owned(),
            route_label: route_label.to_owned(),
            concrete_model: concrete_model.to_owned(),
            source,
            selector_attempts: attempts,
            selector_latency_ms: Some(started.elapsed().as_secs_f64() * 1000.0),
            fallback_reason: fallback,
            repair_attempted,
            repair_succeeded,
        }
    }

    fn fallback(
        &self,
        router: &CompiledModelRouter,
        started: Instant,
        attempts: u32,
        reason: SelectorFallback,
        repair_attempted: bool,
        repair_succeeded: bool,
    ) -> ModelSelection {
        let default = default_route(router);
        ModelSelection {
            virtual_model: router.virtual_model.clone(),
            route_id: default.route_id.clone(),
            route_label: default.label.clone(),
            concrete_model: default.model.clone(),
            source: SelectionSource::Default,
            selector_attempts: attempts,
            selector_latency_ms: Some(started.elapsed().as_secs_f64() * 1000.0),
            fallback_reason: Some(reason),
            repair_attempted,
            repair_succeeded,
        }
    }

    /// Secret-free diagnostics for the last selection are derived by the
    /// caller from [`ModelSelection`]; this helper keeps the mapping exact.
    pub fn diagnostics(selection: &ModelSelection, response_bytes: usize) -> SelectorDiagnostics {
        SelectorDiagnostics {
            attempts: selection.selector_attempts,
            fallback: selection.fallback_reason,
            repair_attempted: selection.repair_attempted,
            repair_succeeded: selection.repair_succeeded,
            source_selector: selection.source == SelectionSource::Selector,
            response_bytes,
            elapsed_ms: selection
                .selector_latency_ms
                .map(|latency| latency as i64)
                .unwrap_or(0),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum SelectorDispatchError {
    ModelMismatch,
    Recursive,
    Encode,
    Admission,
    Dispatch,
}

fn default_route(router: &CompiledModelRouter) -> &crate::model_router::CompiledModelRoute {
    router
        .routes
        .iter()
        .find(|route| route.model == router.default_model)
        .expect("compiled router default_model has a route")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normalize_collapses_whitespace_and_blank_lines() {
        assert_eq!(
            normalize_selector_text("  hello\t world \r\n\r\n\n  next "),
            "hello world\n\nnext"
        );
    }

    #[test]
    fn truncate_is_bounded_and_utf8_safe() {
        let value = "é".repeat(100);
        let truncated = truncate_utf8(&value, 32);
        assert!(truncated.len() <= 32);
        assert!(!truncated.is_empty());
    }

    #[test]
    fn parse_route_id_requires_exact_single_choice() {
        use crate::config::{ModelRouteConfig, ModelRouterConfig};
        let config = ModelRouterConfig {
            selector_model: "selector".into(),
            default_model: "model-a".into(),
            routes: [(
                "a".into(),
                ModelRouteConfig {
                    model: "model-a".into(),
                    description: "route a".into(),
                },
            )]
            .into_iter()
            .collect(),
            ..Default::default()
        };
        let router = crate::model_router::compile_model_router("virtual", &config).expect("router");
        let body = br#"{"choices":[{"message":{"content":"0"}}]}"#;
        assert_eq!(
            parse_route_id(Some(body), &router, SELECTOR_MAX_RESPONSE_BYTES),
            Some("0".to_owned())
        );
        let bad = br#"{"choices":[{"message":{"content":"9"}}]}"#;
        assert_eq!(
            parse_route_id(Some(bad), &router, SELECTOR_MAX_RESPONSE_BYTES),
            None
        );
    }
}
