//! Bounded request admission and pure bridges into M5 request facts.

use std::collections::{BTreeMap, BTreeSet};

use serde_json::{Map, Value};
use thiserror::Error;

use crate::{
    model_router::{
        AffinityIdentityInput, ConversationPrefix, ConversationTextFragment,
        session_identity_from_header,
    },
    routing::RoutingRequestFacts,
    wire::decode::is_client_tool_search_declaration,
    wire::ir::{CanonicalRequest, ClientSurface},
};

use super::limits::{
    DEFAULT_MAX_REQUEST_BODY_BYTES, estimate_context_input_tokens, estimate_reservation_tokens,
};

const MAX_JSON_DEPTH: usize = 64;
const MAX_NATIVE_EXTENSION_FIELDS: usize = 32;

#[derive(Debug, Clone, Copy)]
pub struct AdmissionOptions {
    pub max_body_bytes: usize,
    pub client_surface: ClientSurface,
    pub extra_context_tokens: u64,
}

impl Default for AdmissionOptions {
    fn default() -> Self {
        Self {
            max_body_bytes: DEFAULT_MAX_REQUEST_BODY_BYTES,
            client_surface: ClientSurface::ChatCompletions,
            extra_context_tokens: 0,
        }
    }
}

#[derive(Debug, Error)]
pub enum AdmissionError {
    #[error("request body exceeds configured limit")]
    BodyTooLarge { length: usize, limit: usize },
    #[error("request JSON is invalid")]
    InvalidJson,
    #[error("request JSON top level must be an object")]
    TopLevelNotObject,
    #[error("request model must be a non-empty string")]
    InvalidModel,
    #[error("request field {field} has an invalid shape")]
    InvalidField { field: &'static str },
    #[error("request contains too many {kind}")]
    CollectionLimit { kind: &'static str },
    #[error("request nesting exceeds the bounded limit")]
    DepthLimit,
    #[error("request contains unsupported content form: {kind}")]
    UnsupportedContent { kind: String },
    #[error("request media/document limit exceeded")]
    MediaLimit { kind: &'static str },
    #[error("request limit is invalid for {field}")]
    InvalidLimit { field: &'static str },
    #[error("request length arithmetic overflowed")]
    LengthOverflow,
    #[error("stateful Responses feature is not supported: {field}")]
    StatefulResponsesFeature { field: &'static str },
}

/// Bounded facts about Responses-native syntax that has no canonical
/// projection.  The original parsed value is retained separately for native
/// same-surface forwarding; this summary never contains request content.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct NativeFeatureSummary {
    pub native_input_items: usize,
    pub native_tool_definitions: usize,
    pub extension_fields: Vec<String>,
    pub extensions_truncated: bool,
}

impl NativeFeatureSummary {
    pub fn has_cross_surface_blocker(&self) -> bool {
        self.native_input_items > 0 || self.native_tool_definitions > 0
    }
}

/// Source-native request data retained only for the bounded request lifetime.
///
/// Debug output intentionally reports shape and size only; the parsed value
/// can contain prompts, encrypted reasoning, tool arguments, and extensions.
#[derive(Clone, PartialEq)]
pub struct NativeRequestPreservation {
    pub source_surface: ClientSurface,
    pub parsed: Value,
    pub summary: NativeFeatureSummary,
}

impl std::fmt::Debug for NativeRequestPreservation {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("NativeRequestPreservation")
            .field("source_surface", &self.source_surface)
            .field("parsed_is_object", &self.parsed.is_object())
            .field(
                "parsed_bytes",
                &serde_json::to_vec(&self.parsed).ok().map(|v| v.len()),
            )
            .field("summary", &self.summary)
            .finish()
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct AdmittedRequest {
    pub canonical: CanonicalRequest,
    pub native_preservation: Option<NativeRequestPreservation>,
    pub raw_body_bytes: usize,
    pub reservation_tokens: u64,
    pub context_tokens: u64,
}

/// The bounded, already-parsed request boundary used by the HTTP coordinator.
///
/// The value is kept private to the native runtime so callers cannot bypass
/// the body-size and depth checks.  It is consumed by admission after endpoint
/// classification, which lets model resolution mutate the one parsed tree
/// without reparsing serialized bytes.
#[derive(Debug)]
pub(crate) struct ParsedRequestBody {
    pub(crate) raw_body: bytes::Bytes,
    pub(crate) value: Value,
}

impl ParsedRequestBody {
    pub(crate) fn object(&self) -> Result<&Map<String, Value>, AdmissionError> {
        self.value
            .as_object()
            .ok_or(AdmissionError::TopLevelNotObject)
    }

    pub(crate) fn object_mut(&mut self) -> Result<&mut Map<String, Value>, AdmissionError> {
        self.value
            .as_object_mut()
            .ok_or(AdmissionError::TopLevelNotObject)
    }

    pub(crate) fn into_parts(self) -> (bytes::Bytes, Value) {
        (self.raw_body, self.value)
    }
}

/// Bounded facts for one remote-compaction operation.
///
/// Compaction is a distinct model-facing operation whose output replaces
/// retained history; it is not an ordinary assistant completion. The struct
/// carries only bounded semantic facts needed for routing/encoding, while the
/// source-native compact JSON is preserved separately when same-surface
/// forwarding is legal. No prompt, replacement history, or summary text is
/// retained beyond the bounded request lifetime.
#[derive(Debug, Clone, PartialEq)]
pub struct CompactAdmittedRequest {
    pub canonical: CanonicalRequest,
    pub native_preservation: NativeRequestPreservation,
    pub raw_body_bytes: usize,
    pub reservation_tokens: u64,
    pub context_tokens: u64,
}

impl CompactAdmittedRequest {
    pub fn routing_facts(&self, inputs: &StaticRoutingFacts) -> RoutingRequestFacts {
        routing_request_facts_from_parts(&self.canonical, self.reservation_tokens, inputs)
    }

    pub fn affinity_identity(&self, explicit_session: Option<&str>) -> AffinityIdentityInput {
        affinity_identity_input(&self.canonical, explicit_session)
    }
}

impl AdmittedRequest {
    pub fn routing_facts(&self, inputs: &StaticRoutingFacts) -> RoutingRequestFacts {
        routing_request_facts(self, inputs)
    }

    pub fn affinity_identity(&self, explicit_session: Option<&str>) -> AffinityIdentityInput {
        affinity_identity_input(&self.canonical, explicit_session)
    }
}

#[derive(Debug, Clone, Default)]
pub struct StaticRoutingFacts {
    pub known_provider_ids: BTreeSet<String>,
    pub requested_protocol: Option<String>,
    pub transcode_protocols: Vec<String>,
    pub catalog_stale_after_s: Option<i64>,
    pub capability_policy: BTreeMap<String, String>,
    pub now: i64,
}

pub fn admit_request(
    raw_body: &[u8],
    options: AdmissionOptions,
) -> Result<AdmittedRequest, AdmissionError> {
    let parsed = parse_request_body(
        bytes::Bytes::copy_from_slice(raw_body),
        options.max_body_bytes,
    )?;
    admit_parsed_request(parsed, options)
}

/// Parse and depth-check one bounded request body exactly once.
pub(crate) fn parse_request_body(
    raw_body: bytes::Bytes,
    max_body_bytes: usize,
) -> Result<ParsedRequestBody, AdmissionError> {
    if raw_body.len() > max_body_bytes {
        return Err(AdmissionError::BodyTooLarge {
            length: raw_body.len(),
            limit: max_body_bytes,
        });
    }
    let value = parse_once(&raw_body)?;
    Ok(ParsedRequestBody { raw_body, value })
}

/// Admit a body after it has crossed the bounded parse/depth boundary.
pub(crate) fn admit_parsed_request(
    parsed: ParsedRequestBody,
    options: AdmissionOptions,
) -> Result<AdmittedRequest, AdmissionError> {
    let (raw_body, value) = parsed.into_parts();
    let object = value.as_object().ok_or(AdmissionError::TopLevelNotObject)?;
    let canonical = canonical_request_from_object(object, options.client_surface)?;
    let reservation_tokens = estimate_reservation_tokens(&raw_body);
    let context_tokens =
        estimate_context_input_tokens(&raw_body, &value, options.extra_context_tokens);
    let native_summary = (options.client_surface == ClientSurface::Responses)
        .then(|| native_feature_summary(object));
    let native_preservation = if options.client_surface == ClientSurface::Responses {
        Some(NativeRequestPreservation {
            source_surface: options.client_surface,
            parsed: value,
            summary: native_summary.expect("Responses summary was just computed"),
        })
    } else {
        None
    };
    Ok(AdmittedRequest {
        canonical,
        native_preservation,
        raw_body_bytes: raw_body.len(),
        reservation_tokens,
        context_tokens,
    })
}

/// Convert an already parsed object through the same canonical decoder used
/// by admission. Network callers should use [`admit_request`] so the body
/// bound and one-parse contract remain explicit.
///
/// EggPool-owned wrapper: enforces the stateless Responses product policy
/// first (preserving pre-extraction error precedence), then delegates to the
/// pure wire-kernel structural decoder with [`DecodeLimits::current()`].
pub fn canonical_request_from_value(
    value: &Value,
    surface: ClientSurface,
) -> Result<CanonicalRequest, AdmissionError> {
    use crate::wire::decode::{DecodeLimits, canonical_request_from_value_with_limits};
    if surface == ClientSurface::Responses
        && let Some(object) = value.as_object()
    {
        validate_responses_stateless_policy(object)?;
    }
    canonical_request_from_value_with_limits(value, surface, DecodeLimits::current())
        .map_err(map_decode_error)
}

fn map_decode_error(error: crate::wire::decode::DecodeError) -> AdmissionError {
    use crate::wire::decode::DecodeError as D;
    match error {
        D::TopLevelNotObject => AdmissionError::TopLevelNotObject,
        D::InvalidModel => AdmissionError::InvalidModel,
        D::InvalidField { field } => AdmissionError::InvalidField { field },
        D::CollectionLimit { kind } => AdmissionError::CollectionLimit { kind },
        D::DepthLimit => AdmissionError::DepthLimit,
        D::UnsupportedContent { kind } => AdmissionError::UnsupportedContent { kind },
        D::MediaLimit { kind } => AdmissionError::MediaLimit { kind },
        D::InvalidLimit { field } => AdmissionError::InvalidLimit { field },
        D::LengthOverflow => AdmissionError::LengthOverflow,
        D::StatefulResponsesFeature { field } => AdmissionError::StatefulResponsesFeature { field },
    }
}

use crate::wire::decode::DecodeLimits;

/// Return true when a Responses `input` array contains a v2
/// `compaction_trigger` item. The trigger is a native-only compaction signal;
/// it must never be treated as user text or silently converted through a
/// codec that cannot represent its semantics.
pub fn has_compaction_trigger(object: &Map<String, Value>) -> bool {
    object
        .get("input")
        .and_then(Value::as_array)
        .is_some_and(|items| {
            items.iter().any(|item| {
                item.as_object()
                    .and_then(|item| item.get("type"))
                    .and_then(Value::as_str)
                    == Some("compaction_trigger")
            })
        })
}

/// Admit one bounded remote-compaction request.
///
/// The compact endpoint shares the stateless Responses contract
/// (`previous_response_id`, `store = true`, conversation references, and
/// `background = true` remain rejected) and the same body size, depth, and
/// collection limits as ordinary Responses admission. The operation requires
/// an `input` history payload, is always finite (`stream = true` is
/// rejected), and never accepts a `compaction_trigger` item: the trigger
/// belongs on `POST /v1/responses`, while this endpoint *is* the compaction
/// operation.
pub fn admit_compact_request(
    raw_body: &[u8],
    options: AdmissionOptions,
) -> Result<CompactAdmittedRequest, AdmissionError> {
    let parsed = parse_request_body(
        bytes::Bytes::copy_from_slice(raw_body),
        options.max_body_bytes,
    )?;
    admit_compact_parsed_request(parsed, options)
}

/// Admit a compact request after it has crossed the bounded parse/depth
/// boundary. The endpoint uses this consuming form so model resolution can
/// mutate the parsed request without serializing and parsing it again.
pub(crate) fn admit_compact_parsed_request(
    parsed: ParsedRequestBody,
    options: AdmissionOptions,
) -> Result<CompactAdmittedRequest, AdmissionError> {
    let (raw_body, value) = parsed.into_parts();
    if raw_body.len() > options.max_body_bytes {
        return Err(AdmissionError::BodyTooLarge {
            length: raw_body.len(),
            limit: options.max_body_bytes,
        });
    }
    let object = value.as_object().ok_or(AdmissionError::TopLevelNotObject)?;
    validate_responses_stateless_policy(object)?;
    if has_compaction_trigger(object) {
        return Err(AdmissionError::InvalidField {
            field: "input.compaction_trigger",
        });
    }
    match object.get("stream") {
        None | Some(Value::Null) | Some(Value::Bool(false)) => {}
        Some(Value::Bool(true)) => {
            return Err(AdmissionError::InvalidField { field: "stream" });
        }
        Some(_) => {
            return Err(AdmissionError::InvalidField { field: "stream" });
        }
    }
    let has_input = match object.get("input") {
        None | Some(Value::Null) => false,
        Some(Value::String(text)) => !text.trim().is_empty(),
        Some(Value::Array(items)) => !items.is_empty(),
        Some(_) => {
            return Err(AdmissionError::InvalidField { field: "input" });
        }
    };
    if !has_input {
        return Err(AdmissionError::InvalidField { field: "input" });
    }
    let canonical = canonical_request_from_object(object, ClientSurface::Responses)?;
    if canonical.stream {
        return Err(AdmissionError::InvalidField { field: "stream" });
    }
    let reservation_tokens = estimate_reservation_tokens(&raw_body);
    let context_tokens =
        estimate_context_input_tokens(&raw_body, &value, options.extra_context_tokens);
    let summary = native_feature_summary(object);
    Ok(CompactAdmittedRequest {
        canonical,
        native_preservation: NativeRequestPreservation {
            source_surface: ClientSurface::Responses,
            parsed: value,
            summary,
        },
        raw_body_bytes: raw_body.len(),
        reservation_tokens,
        context_tokens,
    })
}

pub fn routing_request_facts(
    admitted: &AdmittedRequest,
    inputs: &StaticRoutingFacts,
) -> RoutingRequestFacts {
    routing_request_facts_from_parts(&admitted.canonical, admitted.reservation_tokens, inputs)
}

fn routing_request_facts_from_parts(
    canonical: &CanonicalRequest,
    reservation_tokens: u64,
    inputs: &StaticRoutingFacts,
) -> RoutingRequestFacts {
    let mut facts =
        RoutingRequestFacts::from_model_id(&canonical.model, &inputs.known_provider_ids);
    facts.requested_protocol = inputs.requested_protocol.clone();
    facts.client_protocol = Some(canonical.client_surface.protocol().into());
    facts.request_surface = canonical.client_surface.as_str().into();
    facts.transcode_protocols = inputs.transcode_protocols.clone();
    facts.projected_tokens = reservation_tokens.min(i64::MAX as u64) as i64;
    facts.catalog_stale_after_s = inputs.catalog_stale_after_s;
    facts.thinking = thinking_requirement_from_intent(&canonical.reasoning);
    facts.capability_policy = inputs.capability_policy.clone();
    facts.now = inputs.now;
    facts
}

/// EggPool-owned adapter from the neutral wire-kernel thinking facts into
/// routing state. The kernel (`wire::ir::ThinkingFacts`) never imports
/// routing; this conversion delegates to the single seam in
/// `crate::wire::adapters`.
pub fn thinking_requirement_from_intent(
    intent: &crate::wire::ir::ReasoningIntent,
) -> Option<crate::routing::ThinkingRequirement> {
    crate::wire::adapters::thinking_requirement_from_intent(intent)
}

pub fn affinity_identity_input(
    request: &CanonicalRequest,
    explicit_session: Option<&str>,
) -> AffinityIdentityInput {
    if let Some(identity) = session_identity_from_header(explicit_session) {
        return AffinityIdentityInput::explicit(request.client_surface.as_str(), identity);
    }
    let system_developer = request
        .conversation_prefix()
        .into_iter()
        .map(|(role, text)| ConversationTextFragment::new(role.as_str(), text))
        .collect();
    AffinityIdentityInput::automatic(
        request.client_surface.as_str(),
        ConversationPrefix::new(system_developer, request.first_user_text()),
    )
}

fn parse_once(raw_body: &[u8]) -> Result<Value, AdmissionError> {
    let value: Value = serde_json::from_slice(raw_body).map_err(|_| AdmissionError::InvalidJson)?;
    validate_value_depth(&value, 0)?;
    Ok(value)
}

fn validate_value_depth(value: &Value, depth: usize) -> Result<(), AdmissionError> {
    if depth >= MAX_JSON_DEPTH {
        return Err(AdmissionError::DepthLimit);
    }
    match value {
        Value::Array(items) => items
            .iter()
            .try_for_each(|item| validate_value_depth(item, depth + 1)),
        Value::Object(items) => items
            .values()
            .try_for_each(|item| validate_value_depth(item, depth + 1)),
        Value::Null | Value::Bool(_) | Value::Number(_) | Value::String(_) => Ok(()),
    }
}

pub(crate) fn canonical_request_from_object(
    object: &Map<String, Value>,
    surface: ClientSurface,
) -> Result<CanonicalRequest, AdmissionError> {
    // EggPool wrapper preserving pre-extraction error precedence: stateless
    // product policy first, then the pure kernel structural decoder.
    if surface == ClientSurface::Responses {
        validate_responses_stateless_policy(object)?;
    }
    crate::wire::decode::canonical_request_from_object_with_limits(
        object,
        surface,
        DecodeLimits::current(),
    )
    .map_err(map_decode_error)
}

/// Enforce EggPool's product-level stateless Responses contract at the
/// request boundary.  Omitted `store` has the same stateless meaning as false.
pub fn validate_responses_stateless_policy(
    object: &Map<String, Value>,
) -> Result<(), AdmissionError> {
    for field in ["previous_response_id", "conversation"] {
        if object.get(field).is_some_and(|value| !value.is_null()) {
            return Err(AdmissionError::StatefulResponsesFeature { field });
        }
    }
    match object.get("store") {
        None | Some(Value::Bool(false)) => {}
        Some(Value::Bool(true)) => {
            return Err(AdmissionError::StatefulResponsesFeature { field: "store" });
        }
        Some(_) => return Err(AdmissionError::InvalidField { field: "store" }),
    }
    if let Some(background) = object.get("background") {
        match background {
            Value::Bool(true) => {
                return Err(AdmissionError::StatefulResponsesFeature {
                    field: "background",
                });
            }
            Value::Bool(false) | Value::Null => {}
            _ => {
                return Err(AdmissionError::InvalidField {
                    field: "background",
                });
            }
        }
    }
    Ok(())
}

fn is_portable_tool_search_input(item: &Map<String, Value>) -> bool {
    let kind = item.get("type").and_then(Value::as_str);
    let is_client = item.get("execution").and_then(Value::as_str) == Some("client");
    let has_call_id = item
        .get("call_id")
        .and_then(Value::as_str)
        .is_some_and(|call_id| !call_id.trim().is_empty());
    matches!(kind, Some("tool_search_call" | "tool_search_output")) && is_client && has_call_id
}

fn native_feature_summary(object: &Map<String, Value>) -> NativeFeatureSummary {
    let native_input_items = object
        .get("input")
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter(|item| {
                    let Some(item) = item.as_object() else {
                        return true;
                    };
                    let kind = item.get("type").and_then(Value::as_str);
                    if matches!(
                        kind,
                        Some(
                            "message"
                                | "function_call"
                                | "function_call_output"
                                | "custom_tool_call"
                                | "custom_tool_call_output"
                        )
                    ) {
                        return false;
                    }
                    // Client-executed search has a portable canonical form;
                    // hosted/server search stays native-only.
                    if is_portable_tool_search_input(item) {
                        return false;
                    }
                    true
                })
                .count()
        })
        .unwrap_or(0);
    let native_tool_definitions = object
        .get("tools")
        .and_then(Value::as_array)
        .map(|tools| {
            tools
                .iter()
                .filter(|tool| {
                    let Some(tool) = tool.as_object() else {
                        return true;
                    };
                    let kind = tool.get("type").and_then(Value::as_str);
                    if matches!(kind, Some("function" | "custom")) {
                        return false;
                    }
                    if kind == Some("tool_search") && is_client_tool_search_declaration(tool) {
                        return false;
                    }
                    true
                })
                .count()
        })
        .unwrap_or(0);
    let known_fields = [
        "model",
        "input",
        "instructions",
        "stream",
        "store",
        "background",
        "previous_response_id",
        "conversation",
        "max_output_tokens",
        "max_completion_tokens",
        "max_tokens",
        "temperature",
        "top_p",
        "stop",
        "tools",
        "tool_choice",
        "parallel_tool_calls",
        "response_format",
        "text",
        "reasoning",
        "reasoning_effort",
        "thinking",
        "thinking_budget",
        "metadata",
        "cache_control",
    ];
    let extension_field_count = object
        .keys()
        .filter(|field| !known_fields.contains(&field.as_str()))
        .count()
        + object
            .get("text")
            .and_then(Value::as_object)
            .map(|text| {
                text.keys()
                    .filter(|field| field.as_str() != "format")
                    .count()
            })
            .unwrap_or(0);
    let mut extension_fields: Vec<String> = object
        .keys()
        .filter(|field| !known_fields.contains(&field.as_str()))
        .take(MAX_NATIVE_EXTENSION_FIELDS)
        .cloned()
        .collect();
    if let Some(text) = object.get("text").and_then(Value::as_object) {
        extension_fields.extend(
            text.keys()
                .filter(|field| field.as_str() != "format")
                .map(|field| format!("text.{field}")),
        );
    }
    extension_fields.truncate(MAX_NATIVE_EXTENSION_FIELDS);
    NativeFeatureSummary {
        native_input_items,
        native_tool_definitions,
        extension_fields,
        extensions_truncated: extension_field_count > MAX_NATIVE_EXTENSION_FIELDS,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parsed_compact_admission_matches_slice_compatibility_helper() {
        let body = bytes::Bytes::from_static(
            br#"{"model":"compact-model","input":"history","store":false}"#,
        );
        let options = AdmissionOptions::default();
        let from_slice = admit_compact_request(body.as_ref(), options).expect("slice admission");
        let parsed = parse_request_body(body, options.max_body_bytes).expect("parsed body");
        let from_parsed = admit_compact_parsed_request(parsed, options).expect("parsed admission");
        assert_eq!(from_slice, from_parsed);
    }

    #[test]
    fn compact_routing_facts_do_not_depend_on_native_preservation() {
        let body = bytes::Bytes::from_static(
            br#"{"model":"compact-model","input":"history","reasoning":{"effort":"high"}}"#,
        );
        let compact = admit_compact_request(body.as_ref(), AdmissionOptions::default())
            .expect("compact admission");
        let equivalent = AdmittedRequest {
            canonical: compact.canonical.clone(),
            native_preservation: Some(compact.native_preservation.clone()),
            raw_body_bytes: compact.raw_body_bytes,
            reservation_tokens: compact.reservation_tokens,
            context_tokens: compact.context_tokens,
        };
        let inputs = StaticRoutingFacts::default();
        assert_eq!(
            compact.routing_facts(&inputs),
            equivalent.routing_facts(&inputs)
        );
    }
}
