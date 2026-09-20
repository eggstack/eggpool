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
    wire::ir::{
        CanonicalBlockKind, CanonicalContentBlock, CanonicalMessage, CanonicalRequest,
        CanonicalRole, CanonicalTool, CanonicalToolChoice, CanonicalToolKind, ClientSurface,
        MAX_TOOL_SEARCH_DESCRIPTION_BYTES, MAX_TOOL_SEARCH_TOOLS, MediaSource, Presence,
        ReasoningIntent, ReasoningMode, RequestPresence, TOOL_SEARCH_TOOL_NAME, ToolChoiceMode,
        default_tool_search_parameters,
    },
};

use super::limits::{
    DEFAULT_MAX_REQUEST_BODY_BYTES, LimitError, MAX_AUDIO_BYTES, MAX_CACHE_MARKER_BYTES,
    MAX_IMAGE_BYTES, MAX_MEDIA_AGGREGATE_BYTES, MAX_MEDIA_ITEMS, MAX_MEDIA_URI_BYTES,
    MAX_PDF_BYTES, estimate_context_input_tokens, estimate_reservation_tokens,
    requested_output_tokens, valid_reference, validate_base64, validate_media_type,
};

const MAX_JSON_DEPTH: usize = 64;
const MAX_MESSAGES: usize = 1_024;
const MAX_CONTENT_BLOCKS: usize = 2_048;
const MAX_TOOLS: usize = 256;
const MAX_METADATA: usize = 128;
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
        let admitted = AdmittedRequest {
            canonical: self.canonical.clone(),
            native_preservation: Some(self.native_preservation.clone()),
            raw_body_bytes: self.raw_body_bytes,
            reservation_tokens: self.reservation_tokens,
            context_tokens: self.context_tokens,
        };
        routing_request_facts(&admitted, inputs)
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
pub fn canonical_request_from_value(
    value: &Value,
    surface: ClientSurface,
) -> Result<CanonicalRequest, AdmissionError> {
    validate_value_depth(value, 0)?;
    value
        .as_object()
        .ok_or(AdmissionError::TopLevelNotObject)
        .and_then(|object| canonical_request_from_object(object, surface))
}

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
    let mut facts =
        RoutingRequestFacts::from_model_id(&admitted.canonical.model, &inputs.known_provider_ids);
    facts.requested_protocol = inputs.requested_protocol.clone();
    facts.client_protocol = Some(admitted.canonical.client_surface.protocol().into());
    facts.request_surface = admitted.canonical.client_surface.as_str().into();
    facts.transcode_protocols = inputs.transcode_protocols.clone();
    facts.projected_tokens = admitted.reservation_tokens.min(i64::MAX as u64) as i64;
    facts.catalog_stale_after_s = inputs.catalog_stale_after_s;
    facts.thinking = admitted.canonical.reasoning.to_thinking_requirement();
    facts.capability_policy = inputs.capability_policy.clone();
    facts.now = inputs.now;
    facts
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
    if surface == ClientSurface::Responses {
        validate_responses_stateless_policy(object)?;
    }
    let model = string_field(object, "model")?.trim().to_owned();
    if model.is_empty() {
        return Err(AdmissionError::InvalidModel);
    }
    let protocol = surface.protocol();
    let messages = decode_messages(object, protocol, surface)?;
    let stream = bool_field(object, "stream", false)?;
    let max_output_tokens = output_limit(object, surface, protocol)?;
    let temperature = number_field(object, "temperature")?;
    let top_p = number_field(object, "top_p")?;
    let stop = stop_values(object, surface)?;
    let tools = decode_tools(object.get("tools"), surface)?;
    let tool_choice = decode_tool_choice(object.get("tool_choice"))?;
    let response_format = if surface == ClientSurface::Responses {
        object
            .get("response_format")
            .or_else(|| {
                object
                    .get("text")
                    .and_then(Value::as_object)
                    .and_then(|text| text.get("format"))
            })
            .map_or(Ok(None), |value| {
                value
                    .as_object()
                    .cloned()
                    .map(Some)
                    .ok_or(AdmissionError::InvalidField {
                        field: "text.format",
                    })
            })?
    } else {
        mapping_field(object, "response_format")?
    };
    let reasoning = decode_reasoning(object)?;
    let cache_control = bounded_marker(object.get("cache_control"))?;
    let metadata = decode_metadata(object.get("metadata"))?;
    let parallel_tool_calls = optional_bool(object, "parallel_tool_calls")?;
    validate_media_limits(&messages)?;
    validate_tool_identity(&tools, &messages)?;
    Ok(CanonicalRequest {
        model,
        client_surface: surface,
        messages,
        stream,
        max_output_tokens,
        temperature,
        top_p,
        stop,
        tools,
        tool_choice,
        response_format,
        reasoning,
        cache_control,
        metadata,
        parallel_tool_calls: parallel_tool_calls.value().copied(),
        presence: RequestPresence {
            stream: optional_bool(object, "stream")?,
            max_output_tokens: Presence::from_object(
                object,
                output_key(surface, protocol),
                |value| value.as_u64(),
            ),
            temperature: Presence::from_object(object, "temperature", |value| value.as_f64()),
            top_p: Presence::from_object(object, "top_p", |value| value.as_f64()),
            stop: Presence::from_object(object, stop_key(surface), decode_stop_presence),
            response_format: Presence::from_object(object, "response_format", |value| {
                value.as_object().cloned()
            }),
            parallel_tool_calls,
        },
    })
}

fn decode_messages(
    object: &Map<String, Value>,
    protocol: &str,
    surface: ClientSurface,
) -> Result<Vec<CanonicalMessage>, AdmissionError> {
    if surface == ClientSurface::Responses {
        let mut messages = Vec::new();
        if let Some(instructions) = object.get("instructions") {
            messages.push(CanonicalMessage {
                role: CanonicalRole::System,
                content: vec![CanonicalContentBlock::text(instructions.as_str().ok_or(
                    AdmissionError::InvalidField {
                        field: "instructions",
                    },
                )?)],
                tool_call_id: None,
                name: None,
                refusal: None,
            });
        }
        if let Some(input) = object.get("input") {
            if let Some(text) = input.as_str() {
                messages.push(CanonicalMessage {
                    role: CanonicalRole::User,
                    content: vec![CanonicalContentBlock::text(text)],
                    tool_call_id: None,
                    name: None,
                    refusal: None,
                });
                return Ok(messages);
            }
            messages.extend(decode_message_array(input, protocol, surface)?);
        }
        return Ok(messages);
    }
    let mut messages = Vec::new();
    if protocol == "anthropic"
        && let Some(system) = object.get("system")
    {
        messages.push(CanonicalMessage {
            role: CanonicalRole::System,
            content: decode_content(system, CanonicalRole::System, surface)?,
            tool_call_id: None,
            name: None,
            refusal: None,
        });
    }
    let raw = object.get("messages").or_else(|| object.get("contents"));
    if let Some(value) = raw {
        messages.extend(decode_message_array(value, protocol, surface)?);
    }
    Ok(messages)
}

fn decode_message_array(
    value: &Value,
    protocol: &str,
    surface: ClientSurface,
) -> Result<Vec<CanonicalMessage>, AdmissionError> {
    let items = value
        .as_array()
        .ok_or(AdmissionError::InvalidField { field: "messages" })?;
    if items.len() > MAX_MESSAGES {
        return Err(AdmissionError::CollectionLimit { kind: "messages" });
    }
    items
        .iter()
        .map(|item| -> Result<Option<CanonicalMessage>, AdmissionError> {
            let object = item.as_object().ok_or(AdmissionError::InvalidField {
                field: "messages[]",
            })?;
            if surface == ClientSurface::Responses {
                match object.get("type").and_then(Value::as_str) {
                    Some("function_call") => {
                        return Ok(Some(CanonicalMessage {
                            role: CanonicalRole::Assistant,
                            content: vec![decode_response_function_call(object)?],
                            tool_call_id: None,
                            name: None,
                            refusal: None,
                        }));
                    }
                    Some("function_call_output") => {
                        return Ok(Some(CanonicalMessage {
                            role: CanonicalRole::Tool,
                            content: vec![decode_response_function_output(object)?],
                            tool_call_id: string_value(object.get("call_id"))?,
                            name: None,
                            refusal: None,
                        }));
                    }
                    Some("custom_tool_call") => {
                        return Ok(Some(CanonicalMessage {
                            role: CanonicalRole::Assistant,
                            content: vec![decode_response_custom_tool_call(object)?],
                            tool_call_id: None,
                            name: None,
                            refusal: None,
                        }));
                    }
                    Some("custom_tool_call_output") => {
                        return Ok(Some(CanonicalMessage {
                            role: CanonicalRole::Tool,
                            content: vec![decode_response_custom_tool_output(object)?],
                            tool_call_id: string_value(object.get("call_id"))?,
                            name: None,
                            refusal: None,
                        }));
                    }
                    Some("tool_search_call") => {
                        // Only client-executed search has a portable
                        // semantic. Hosted/server search stays native-only so
                        // cross-surface routes fail before provider dispatch
                        // instead of silently dropping the tool.
                        let is_client =
                            object.get("execution").and_then(Value::as_str) == Some("client");
                        if !is_client {
                            return Ok(None);
                        }
                        return Ok(Some(CanonicalMessage {
                            role: CanonicalRole::Assistant,
                            content: vec![decode_response_tool_search_call(object)?],
                            tool_call_id: None,
                            name: None,
                            refusal: None,
                        }));
                    }
                    Some("tool_search_output") => {
                        let is_client =
                            object.get("execution").and_then(Value::as_str) == Some("client");
                        if !is_client {
                            return Ok(None);
                        }
                        return Ok(Some(CanonicalMessage {
                            role: CanonicalRole::Tool,
                            content: vec![decode_response_tool_search_output(object)?],
                            tool_call_id: string_value(object.get("call_id"))?,
                            name: None,
                            refusal: None,
                        }));
                    }
                    Some("message") | None => {}
                    Some(_) => return Ok(None),
                }
            }
            let role = decode_role(object.get("role"), protocol)?;
            let content_value = object
                .get("content")
                .or_else(|| object.get("parts"))
                .ok_or(AdmissionError::InvalidField {
                    field: "messages[].content",
                })?;
            let mut content = decode_content(content_value, role, surface)?;
            if let Some(calls) = object.get("tool_calls") {
                let calls = calls.as_array().ok_or(AdmissionError::InvalidField {
                    field: "tool_calls",
                })?;
                for call in calls {
                    content.push(decode_openai_tool_call(call)?);
                }
            }
            if let Some(kind) = object.get("type").and_then(Value::as_str) {
                match kind {
                    "function_call" => content.push(decode_response_function_call(object)?),
                    "function_call_output" => {
                        content.push(decode_response_function_output(object)?)
                    }
                    _ => {}
                }
            }
            let tool_call_id = string_value(object.get("tool_call_id"))?;
            if role == CanonicalRole::Tool {
                let Some(call_id) = tool_call_id.as_deref() else {
                    return Err(AdmissionError::InvalidField {
                        field: "messages[].tool_call_id",
                    });
                };
                let mut converted = Vec::with_capacity(content.len());
                for block in content {
                    if block.kind == CanonicalBlockKind::Text {
                        converted.push(CanonicalContentBlock {
                            kind: CanonicalBlockKind::ToolResult,
                            text: block.text,
                            media: None,
                            call_id: Some(call_id.to_owned()),
                            name: None,
                            arguments: None,
                            tool_input: None,
                            tool_kind: CanonicalToolKind::Function,
                            is_error: false,
                            signature: None,
                            cache_control: None,
                            prompt_cache_breakpoint: None,
                        });
                    } else if matches!(
                        block.kind,
                        CanonicalBlockKind::Image | CanonicalBlockKind::Document
                    ) && converted
                        .iter()
                        .rposition(|candidate| candidate.kind == CanonicalBlockKind::ToolResult)
                        .is_some_and(|index| converted[index].media.is_none())
                    {
                        let index = converted
                            .iter()
                            .rposition(|candidate| candidate.kind == CanonicalBlockKind::ToolResult)
                            .expect("tool result index was just checked");
                        converted[index].media = block.media;
                    } else {
                        converted.push(block);
                    }
                }
                content = converted;
            }
            Ok(Some(CanonicalMessage {
                role,
                content,
                tool_call_id,
                name: string_value(object.get("name"))?,
                refusal: string_value(object.get("refusal"))?,
            }))
        })
        .collect::<Result<Vec<_>, _>>()
        .map(|messages| messages.into_iter().flatten().collect())
}

fn decode_role(value: Option<&Value>, protocol: &str) -> Result<CanonicalRole, AdmissionError> {
    let role = value
        .and_then(Value::as_str)
        .ok_or(AdmissionError::InvalidField {
            field: "messages[].role",
        })?;
    match (role, protocol) {
        ("system", _) => Ok(CanonicalRole::System),
        ("developer", _) => Ok(CanonicalRole::Developer),
        ("user", _) => Ok(CanonicalRole::User),
        ("assistant", _) | ("model", "gemini") => Ok(CanonicalRole::Assistant),
        ("tool", _) => Ok(CanonicalRole::Tool),
        _ => Err(AdmissionError::InvalidField {
            field: "messages[].role",
        }),
    }
}

fn decode_content(
    value: &Value,
    role: CanonicalRole,
    surface: ClientSurface,
) -> Result<Vec<CanonicalContentBlock>, AdmissionError> {
    // OpenAI assistant messages commonly use `content: null` when the
    // message carries tool calls.  It is an explicit empty content value, not
    // a malformed content shape.
    if value.is_null() {
        return Ok(Vec::new());
    }
    if let Some(text) = value.as_str() {
        return Ok(vec![CanonicalContentBlock::text(text)]);
    }
    let blocks = value
        .as_array()
        .ok_or(AdmissionError::InvalidField { field: "content" })?;
    if blocks.len() > MAX_CONTENT_BLOCKS {
        return Err(AdmissionError::CollectionLimit {
            kind: "content blocks",
        });
    }
    blocks
        .iter()
        .map(|block| decode_content_block(block, role, surface))
        .collect()
}

fn decode_content_block(
    value: &Value,
    role: CanonicalRole,
    _surface: ClientSurface,
) -> Result<CanonicalContentBlock, AdmissionError> {
    let object = value
        .as_object()
        .ok_or(AdmissionError::InvalidField { field: "content[]" })?;
    let kind = object
        .get("type")
        .and_then(Value::as_str)
        .ok_or(AdmissionError::InvalidField {
            field: "content[].type",
        })?;
    match kind {
        "text" | "input_text" | "output_text" => {
            let mut block = CanonicalContentBlock::text(string_field(object, "text")?);
            block.cache_control = bounded_marker(object.get("cache_control"))?;
            block.prompt_cache_breakpoint = bounded_marker(object.get("prompt_cache_breakpoint"))?;
            Ok(block)
        }
        "image_url" => {
            let image = object
                .get("image_url")
                .and_then(Value::as_object)
                .ok_or(AdmissionError::InvalidField { field: "image_url" })?;
            let url =
                image
                    .get("url")
                    .and_then(Value::as_str)
                    .ok_or(AdmissionError::InvalidField {
                        field: "image_url.url",
                    })?;
            let mut block = data_uri_block(url, CanonicalBlockKind::Image, MAX_IMAGE_BYTES)?;
            if let Some(media) = block.media.as_mut() {
                media.detail =
                    bounded_detail(image.get("detail").or_else(|| image.get("quality")))?;
            }
            block.prompt_cache_breakpoint = bounded_marker(object.get("prompt_cache_breakpoint"))?;
            block.cache_control = bounded_marker(object.get("cache_control"))?;
            Ok(block)
        }
        "input_image" => {
            if let Some(url) = object.get("image_url").and_then(Value::as_str) {
                let mut block = data_uri_block(url, CanonicalBlockKind::Image, MAX_IMAGE_BYTES)?;
                if let Some(media) = block.media.as_mut() {
                    media.detail =
                        bounded_detail(object.get("detail").or_else(|| object.get("quality")))?;
                }
                block.prompt_cache_breakpoint =
                    bounded_marker(object.get("prompt_cache_breakpoint"))?;
                block.cache_control = bounded_marker(object.get("cache_control"))?;
                return Ok(block);
            }
            let mut block = decode_media_block(object, CanonicalBlockKind::Image, MAX_IMAGE_BYTES)?;
            if let Some(media) = block.media.as_mut() {
                media.detail =
                    bounded_detail(object.get("detail").or_else(|| object.get("quality")))?;
            }
            block.prompt_cache_breakpoint = bounded_marker(object.get("prompt_cache_breakpoint"))?;
            block.cache_control = bounded_marker(object.get("cache_control"))?;
            Ok(block)
        }
        "image" => {
            let mut block = decode_media_block(object, CanonicalBlockKind::Image, MAX_IMAGE_BYTES)?;
            if let Some(media) = block.media.as_mut() {
                media.detail =
                    bounded_detail(object.get("detail").or_else(|| object.get("quality")))?;
            }
            block.prompt_cache_breakpoint = bounded_marker(object.get("prompt_cache_breakpoint"))?;
            block.cache_control = bounded_marker(object.get("cache_control"))?;
            Ok(block)
        }
        "file" | "document" | "input_file" => {
            let mut block = decode_document_block(object)?;
            block.cache_control = bounded_marker(object.get("cache_control"))?;
            block.prompt_cache_breakpoint = bounded_marker(object.get("prompt_cache_breakpoint"))?;
            Ok(block)
        }
        "input_audio" | "audio" => Ok(CanonicalContentBlock {
            kind: CanonicalBlockKind::Audio,
            text: None,
            media: Some(MediaSource {
                media_type: object
                    .get("media_type")
                    .or_else(|| object.get("mime_type"))
                    .and_then(Value::as_str)
                    .map(str::to_owned),
                data: object
                    .get("data")
                    .or_else(|| {
                        object
                            .get("input_audio")
                            .and_then(Value::as_object)
                            .and_then(|value| value.get("data"))
                    })
                    .and_then(Value::as_str)
                    .map(str::to_owned),
                uri: None,
                detail: None,
                file_id: None,
            }),
            call_id: None,
            name: None,
            arguments: None,
            tool_input: None,
            tool_kind: CanonicalToolKind::Function,
            is_error: false,
            signature: None,
            cache_control: None,
            prompt_cache_breakpoint: None,
        }),
        "thinking" | "reasoning" | "reasoning_content" => Ok(CanonicalContentBlock {
            kind: CanonicalBlockKind::Reasoning,
            text: object
                .get("thinking")
                .or_else(|| object.get("text"))
                .and_then(Value::as_str)
                .map(str::to_owned),
            media: None,
            call_id: None,
            name: None,
            arguments: None,
            tool_input: None,
            tool_kind: CanonicalToolKind::Function,
            is_error: false,
            signature: object
                .get("signature")
                .and_then(Value::as_str)
                .map(str::to_owned),
            cache_control: bounded_marker(object.get("cache_control"))?,
            prompt_cache_breakpoint: bounded_marker(object.get("prompt_cache_breakpoint"))?,
        }),
        "tool_use" => Ok(tool_call_block(object, true)?),
        "tool_result" => Ok(CanonicalContentBlock {
            kind: CanonicalBlockKind::ToolResult,
            text: content_text(object.get("content"))?,
            media: decode_tool_result_media(object.get("content"))?,
            call_id: string_value(object.get("tool_use_id"))?,
            name: None,
            arguments: None,
            tool_input: None,
            tool_kind: CanonicalToolKind::Function,
            is_error: object
                .get("is_error")
                .and_then(Value::as_bool)
                .unwrap_or(false),
            signature: None,
            cache_control: None,
            prompt_cache_breakpoint: None,
        }),
        "refusal" if role == CanonicalRole::Assistant => Ok(CanonicalContentBlock {
            kind: CanonicalBlockKind::Refusal,
            text: string_value(object.get("refusal").or_else(|| object.get("text")))?,
            media: None,
            call_id: None,
            name: None,
            arguments: None,
            tool_input: None,
            tool_kind: CanonicalToolKind::Function,
            is_error: false,
            signature: None,
            cache_control: None,
            prompt_cache_breakpoint: None,
        }),
        other => Err(AdmissionError::UnsupportedContent { kind: other.into() }),
    }
}

fn decode_media_block(
    object: &Map<String, Value>,
    kind: CanonicalBlockKind,
    limit: u64,
) -> Result<CanonicalContentBlock, AdmissionError> {
    let source = object
        .get("source")
        .and_then(Value::as_object)
        .unwrap_or(object);
    let source_type = source.get("type").and_then(Value::as_str);
    let media_type = source
        .get("media_type")
        .and_then(Value::as_str)
        .map(str::to_owned);
    if let Some(media_type) = media_type.as_deref()
        && !validate_media_type(media_type)
    {
        return Err(AdmissionError::InvalidField {
            field: "media.media_type",
        });
    }
    let data = source
        .get("data")
        .and_then(Value::as_str)
        .map(str::to_owned);
    let uri = source
        .get("url")
        .and_then(Value::as_str)
        .or_else(|| object.get("url").and_then(Value::as_str))
        .map(str::to_owned);
    let file_id = source
        .get("file_id")
        .or_else(|| source.get("fileId"))
        .and_then(Value::as_str)
        .map(str::to_owned);
    if source_type == Some("base64") || data.is_some() {
        let encoded = data.as_deref().ok_or(AdmissionError::InvalidField {
            field: "media.data",
        })?;
        validate_base64(
            encoded,
            limit,
            if kind == CanonicalBlockKind::Image {
                "image"
            } else {
                "document"
            },
        )
        .map_err(|error| media_error(error, kind))?;
    } else if uri.is_none() && file_id.is_none() {
        return Err(AdmissionError::InvalidField {
            field: "media.source",
        });
    }
    if let Some(uri) = uri.as_deref()
        && !valid_reference(uri)
    {
        return Err(AdmissionError::InvalidField { field: "media.url" });
    }
    if let Some(file_id) = file_id.as_deref()
        && !valid_reference(file_id)
    {
        return Err(AdmissionError::InvalidField {
            field: "media.file_id",
        });
    }
    Ok(media_block(kind, uri.as_deref(), media_type, data, file_id))
}

fn decode_document_block(
    object: &Map<String, Value>,
) -> Result<CanonicalContentBlock, AdmissionError> {
    if let Some(file) = object.get("file").and_then(Value::as_object) {
        if let Some(file_data) = file.get("file_data").and_then(Value::as_str) {
            return data_uri_block(file_data, CanonicalBlockKind::Document, MAX_PDF_BYTES);
        }
        if let Some(file_id) = file.get("file_id").and_then(Value::as_str) {
            if !valid_reference(file_id) {
                return Err(AdmissionError::InvalidField {
                    field: "file.file_id",
                });
            }
            return Ok(media_block(
                CanonicalBlockKind::Document,
                None,
                Some("application/pdf".into()),
                None,
                Some(file_id.into()),
            ));
        }
    }
    decode_media_block(object, CanonicalBlockKind::Document, MAX_PDF_BYTES)
}

fn media_error(error: LimitError, kind: CanonicalBlockKind) -> AdmissionError {
    match error {
        LimitError::EncodedPayloadTooLarge { .. } => AdmissionError::MediaLimit {
            kind: if kind == CanonicalBlockKind::Image {
                "image"
            } else {
                "document"
            },
        },
        LimitError::InvalidBase64 => AdmissionError::InvalidField {
            field: "media.data",
        },
        LimitError::InvalidPositiveInteger { .. } => AdmissionError::InvalidField {
            field: "media.data",
        },
    }
}

fn media_block(
    kind: CanonicalBlockKind,
    uri: Option<&str>,
    media_type: Option<String>,
    data: Option<String>,
    file_id: Option<String>,
) -> CanonicalContentBlock {
    CanonicalContentBlock {
        kind,
        text: None,
        media: Some(MediaSource {
            media_type,
            data,
            uri: uri.map(str::to_owned),
            detail: None,
            file_id,
        }),
        call_id: None,
        name: None,
        arguments: None,
        tool_input: None,
        tool_kind: CanonicalToolKind::Function,
        is_error: false,
        signature: None,
        cache_control: None,
        prompt_cache_breakpoint: None,
    }
}

fn data_uri_block(
    uri: &str,
    kind: CanonicalBlockKind,
    limit: u64,
) -> Result<CanonicalContentBlock, AdmissionError> {
    if let Some((media_type, encoded)) = uri
        .strip_prefix("data:")
        .and_then(|value| value.split_once(";base64,"))
    {
        if !validate_media_type(media_type) {
            return Err(AdmissionError::InvalidField {
                field: "media.media_type",
            });
        }
        validate_base64(
            encoded,
            limit,
            if kind == CanonicalBlockKind::Image {
                "image"
            } else {
                "document"
            },
        )
        .map_err(|error| media_error(error, kind))?;
        return Ok(media_block(
            kind,
            None,
            Some(media_type.to_owned()),
            Some(encoded.to_owned()),
            None,
        ));
    }
    if uri.starts_with("http://") || uri.starts_with("https://") {
        if !valid_reference(uri) {
            return Err(AdmissionError::InvalidField { field: "media.url" });
        }
        return Ok(media_block(kind, Some(uri), None, None, None));
    }
    Err(AdmissionError::UnsupportedContent {
        kind: "media URI".into(),
    })
}

fn bounded_detail(value: Option<&Value>) -> Result<Option<String>, AdmissionError> {
    let Some(value) = value else {
        return Ok(None);
    };
    let Some(detail) = value.as_str() else {
        return Err(AdmissionError::InvalidField {
            field: "image.detail",
        });
    };
    if !matches!(detail, "auto" | "low" | "medium" | "high") {
        return Err(AdmissionError::InvalidField {
            field: "image.detail",
        });
    }
    Ok(Some(detail.to_owned()))
}

fn bounded_marker(value: Option<&Value>) -> Result<Option<Value>, AdmissionError> {
    let Some(value) = value else {
        return Ok(None);
    };
    if value.is_null() {
        return Ok(None);
    }
    if serde_json::to_vec(value)
        .map(|encoded| encoded.len() > MAX_CACHE_MARKER_BYTES)
        .unwrap_or(true)
    {
        return Err(AdmissionError::MediaLimit {
            kind: "cache marker",
        });
    }
    Ok(Some(value.clone()))
}

fn decode_openai_tool_call(value: &Value) -> Result<CanonicalContentBlock, AdmissionError> {
    let object = value.as_object().ok_or(AdmissionError::InvalidField {
        field: "tool_calls[]",
    })?;
    let function = object
        .get("function")
        .and_then(Value::as_object)
        .unwrap_or(object);
    Ok(CanonicalContentBlock {
        kind: CanonicalBlockKind::ToolCall,
        text: None,
        media: None,
        call_id: string_value(object.get("id"))?,
        name: string_value(function.get("name"))?,
        arguments: string_value(function.get("arguments"))?,
        tool_input: None,
        tool_kind: CanonicalToolKind::Function,
        is_error: false,
        signature: None,
        cache_control: None,
        prompt_cache_breakpoint: None,
    })
}

fn tool_call_block(
    object: &Map<String, Value>,
    anthropic: bool,
) -> Result<CanonicalContentBlock, AdmissionError> {
    let input = object.get("input").and_then(Value::as_object).cloned();
    Ok(CanonicalContentBlock {
        kind: CanonicalBlockKind::ToolCall,
        text: None,
        media: None,
        call_id: string_value(object.get(if anthropic { "id" } else { "call_id" }))?,
        name: string_value(object.get("name"))?,
        arguments: None,
        tool_input: input,
        tool_kind: CanonicalToolKind::Function,
        is_error: false,
        signature: None,
        cache_control: None,
        prompt_cache_breakpoint: None,
    })
}

fn decode_response_function_call(
    object: &Map<String, Value>,
) -> Result<CanonicalContentBlock, AdmissionError> {
    Ok(CanonicalContentBlock {
        kind: CanonicalBlockKind::ToolCall,
        text: None,
        media: None,
        call_id: string_value(object.get("call_id"))?,
        name: string_value(object.get("name"))?,
        arguments: string_value(object.get("arguments"))?,
        tool_input: None,
        tool_kind: CanonicalToolKind::Function,
        is_error: false,
        signature: None,
        cache_control: None,
        prompt_cache_breakpoint: None,
    })
}

fn decode_response_function_output(
    object: &Map<String, Value>,
) -> Result<CanonicalContentBlock, AdmissionError> {
    Ok(CanonicalContentBlock {
        kind: CanonicalBlockKind::ToolResult,
        text: string_value(object.get("output"))?,
        media: None,
        call_id: string_value(object.get("call_id"))?,
        name: None,
        arguments: None,
        tool_input: None,
        tool_kind: CanonicalToolKind::Function,
        is_error: false,
        signature: None,
        cache_control: None,
        prompt_cache_breakpoint: None,
    })
}

fn decode_response_custom_tool_call(
    object: &Map<String, Value>,
) -> Result<CanonicalContentBlock, AdmissionError> {
    Ok(CanonicalContentBlock {
        kind: CanonicalBlockKind::ToolCall,
        text: None,
        media: None,
        call_id: string_value(object.get("call_id"))?,
        name: string_value(object.get("name"))?,
        arguments: string_value(object.get("input"))?,
        tool_input: None,
        tool_kind: CanonicalToolKind::Freeform,
        is_error: false,
        signature: None,
        cache_control: None,
        prompt_cache_breakpoint: None,
    })
}

fn decode_response_custom_tool_output(
    object: &Map<String, Value>,
) -> Result<CanonicalContentBlock, AdmissionError> {
    Ok(CanonicalContentBlock {
        kind: CanonicalBlockKind::ToolResult,
        text: string_value(object.get("output"))?,
        media: None,
        call_id: string_value(object.get("call_id"))?,
        name: None,
        arguments: None,
        tool_input: None,
        tool_kind: CanonicalToolKind::Freeform,
        is_error: false,
        signature: None,
        cache_control: None,
        prompt_cache_breakpoint: None,
    })
}

fn is_client_tool_search_declaration(object: &Map<String, Value>) -> bool {
    object.get("type").and_then(Value::as_str) == Some("tool_search")
        && object.get("execution").and_then(Value::as_str) == Some("client")
}

fn decode_tool_search_declaration(
    object: &Map<String, Value>,
) -> Result<CanonicalTool, AdmissionError> {
    if let Some(name) = object.get("name").and_then(Value::as_str)
        && name != TOOL_SEARCH_TOOL_NAME
    {
        return Err(AdmissionError::InvalidField {
            field: "tools[].name",
        });
    }
    let description = string_value(object.get("description"))?;
    if description
        .as_deref()
        .is_some_and(|description| description.len() > MAX_TOOL_SEARCH_DESCRIPTION_BYTES)
    {
        return Err(AdmissionError::InvalidField {
            field: "tools[].description",
        });
    }
    let parameters = object
        .get("parameters")
        .map(|value| {
            value
                .as_object()
                .cloned()
                .ok_or(AdmissionError::InvalidField {
                    field: "tools[].parameters",
                })
        })
        .transpose()?
        .unwrap_or_else(default_tool_search_parameters);
    if serde_json::to_vec(&parameters)
        .map(|encoded| encoded.len() > 16 * 1024)
        .unwrap_or(true)
    {
        return Err(AdmissionError::InvalidField {
            field: "tools[].parameters",
        });
    }
    Ok(CanonicalTool {
        kind: CanonicalToolKind::DeferredSearch,
        name: TOOL_SEARCH_TOOL_NAME.to_owned(),
        description,
        parameters,
        cache_control: None,
        defer_loading: None,
    })
}

/// Decode one client-executed `tool_search_call` history item.
///
/// Provenance: OpenAI Codex `4701aa4b4239c70063ab6f2fcb835324f9c109f4`
/// (`codex-rs/tools/src/tool_spec.rs`, `tool_search_spec.rs`,
/// `core/src/tools/handlers/tool_search.rs`) and the public Responses
/// `tool_search` guide. Only `execution == "client"` with a defined
/// `call_id` is portable; hosted/server search stays native-only and is
/// handled by the caller returning `None`.
fn decode_response_tool_search_call(
    object: &Map<String, Value>,
) -> Result<CanonicalContentBlock, AdmissionError> {
    let call_id = string_value(object.get("call_id"))?.ok_or(AdmissionError::InvalidField {
        field: "tool_call.id",
    })?;
    if call_id.trim().is_empty() {
        return Err(AdmissionError::InvalidField {
            field: "tool_call.id",
        });
    }
    if let Some(name) = object.get("name").and_then(Value::as_str)
        && name != TOOL_SEARCH_TOOL_NAME
    {
        return Err(AdmissionError::InvalidField {
            field: "tool_call.name",
        });
    }
    let arguments_value = object
        .get("arguments")
        .ok_or(AdmissionError::InvalidField {
            field: "tool_call.arguments",
        })?;
    // Current Codex sends an object; accept a JSON string defensively but
    // still validate the bounded `query`/`limit` shape.
    let arguments_string = if let Some(object) = arguments_value.as_object() {
        crate::wire::ir::validate_tool_search_arguments_value(&Value::Object(object.clone()))
            .ok_or(AdmissionError::InvalidField {
                field: "tool_call.arguments",
            })?
    } else if let Some(text) = arguments_value.as_str() {
        crate::wire::ir::validate_tool_search_arguments_string(text).ok_or(
            AdmissionError::InvalidField {
                field: "tool_call.arguments",
            },
        )?
    } else {
        return Err(AdmissionError::InvalidField {
            field: "tool_call.arguments",
        });
    };
    Ok(CanonicalContentBlock {
        kind: CanonicalBlockKind::ToolCall,
        text: None,
        media: None,
        call_id: Some(call_id),
        name: Some(TOOL_SEARCH_TOOL_NAME.to_owned()),
        arguments: Some(arguments_string),
        tool_input: None,
        tool_kind: CanonicalToolKind::DeferredSearch,
        is_error: false,
        signature: None,
        cache_control: None,
        prompt_cache_breakpoint: None,
    })
}

/// Decode one client-executed `tool_search_output` history item.
///
/// The discovered `tools` array is preserved as bounded JSON text so a later
/// function-capable upstream sees the search result as an ordinary function
/// result. Ordering relative to other calls/results is preserved by the
/// caller; this helper never converts the output to user text.
fn decode_response_tool_search_output(
    object: &Map<String, Value>,
) -> Result<CanonicalContentBlock, AdmissionError> {
    let call_id = string_value(object.get("call_id"))?.ok_or(AdmissionError::InvalidField {
        field: "tool_result.call_id",
    })?;
    if call_id.trim().is_empty() {
        return Err(AdmissionError::InvalidField {
            field: "tool_result.call_id",
        });
    }
    let tools =
        object
            .get("tools")
            .and_then(Value::as_array)
            .ok_or(AdmissionError::InvalidField {
                field: "tool_result.output",
            })?;
    if tools.len() > MAX_TOOL_SEARCH_TOOLS {
        return Err(AdmissionError::CollectionLimit {
            kind: "tool_search_output.tools",
        });
    }
    for tool in tools {
        if !tool.is_object() {
            return Err(AdmissionError::InvalidField {
                field: "tool_result.output",
            });
        }
    }
    let encoded = serde_json::to_string(tools).map_err(|_| AdmissionError::InvalidField {
        field: "tool_result.output",
    })?;
    if encoded.len() > crate::wire::ir::MAX_TOOL_SEARCH_OUTPUT_BYTES {
        return Err(AdmissionError::CollectionLimit {
            kind: "tool_search_output.tools",
        });
    }
    Ok(CanonicalContentBlock {
        kind: CanonicalBlockKind::ToolResult,
        text: Some(encoded),
        media: None,
        call_id: Some(call_id),
        name: None,
        arguments: None,
        tool_input: None,
        tool_kind: CanonicalToolKind::DeferredSearch,
        is_error: false,
        signature: None,
        cache_control: None,
        prompt_cache_breakpoint: None,
    })
}

fn decode_tools(
    value: Option<&Value>,
    surface: ClientSurface,
) -> Result<Vec<CanonicalTool>, AdmissionError> {
    let Some(value) = value else {
        return Ok(Vec::new());
    };
    let items = value
        .as_array()
        .ok_or(AdmissionError::InvalidField { field: "tools" })?;
    if items.len() > MAX_TOOLS {
        return Err(AdmissionError::CollectionLimit { kind: "tools" });
    }
    items
        .iter()
        .map(|item| -> Result<Option<CanonicalTool>, AdmissionError> {
            let object = item
                .as_object()
                .ok_or(AdmissionError::InvalidField { field: "tools[]" })?;
            if surface == ClientSurface::Responses
                && let Some(kind) = object.get("type")
            {
                let kind = kind.as_str().ok_or(AdmissionError::InvalidField {
                    field: "tools[].type",
                })?;
                if kind == "custom" {
                    return Ok(Some(CanonicalTool {
                        kind: CanonicalToolKind::Freeform,
                        name: string_field(object, "name")?,
                        description: string_value(object.get("description"))?,
                        parameters: Map::new(),
                        cache_control: None,
                        defer_loading: None,
                    }));
                }
                if kind == "tool_search" {
                    if is_client_tool_search_declaration(object) {
                        return decode_tool_search_declaration(object).map(Some);
                    }
                    return Ok(None);
                }
                if kind != "function" {
                    return Ok(None);
                }
            }
            let function = object
                .get("function")
                .and_then(Value::as_object)
                .unwrap_or(object);
            let name = string_field(function, "name")?;
            let parameters = function
                .get("parameters")
                .or_else(|| function.get("input_schema"))
                .and_then(Value::as_object)
                .cloned()
                .unwrap_or_default();
            Ok(Some(CanonicalTool {
                kind: CanonicalToolKind::Function,
                name,
                description: string_value(function.get("description"))?,
                parameters,
                cache_control: bounded_marker(function.get("cache_control"))?,
                defer_loading: function.get("defer_loading").and_then(Value::as_bool),
            }))
        })
        .collect::<Result<Vec<_>, _>>()
        .map(|tools| tools.into_iter().flatten().collect())
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

fn validate_tool_identity(
    tools: &[CanonicalTool],
    messages: &[CanonicalMessage],
) -> Result<(), AdmissionError> {
    let mut names = BTreeSet::new();
    for tool in tools {
        if tool.name.trim().is_empty() || !names.insert(tool.name.clone()) {
            return Err(AdmissionError::InvalidField {
                field: "tools.name",
            });
        }
    }
    let mut calls = BTreeSet::new();
    for message in messages {
        for block in &message.content {
            match block.kind {
                CanonicalBlockKind::ToolCall => {
                    let Some(call_id) = block.call_id.as_deref() else {
                        return Err(AdmissionError::InvalidField {
                            field: "tool_call.id",
                        });
                    };
                    if call_id.trim().is_empty() || !calls.insert(call_id.to_owned()) {
                        return Err(AdmissionError::InvalidField {
                            field: "tool_call.id",
                        });
                    }
                    if block.name.as_deref().is_none_or(str::is_empty) {
                        return Err(AdmissionError::InvalidField {
                            field: "tool_call.name",
                        });
                    }
                }
                CanonicalBlockKind::ToolResult
                    if block
                        .call_id
                        .as_deref()
                        .is_none_or(|call_id| call_id.trim().is_empty()) =>
                {
                    return Err(AdmissionError::InvalidField {
                        field: "tool_result.call_id",
                    });
                }
                _ => {}
            }
        }
    }
    Ok(())
}

fn decode_tool_choice(
    value: Option<&Value>,
) -> Result<Option<CanonicalToolChoice>, AdmissionError> {
    let Some(value) = value else {
        return Ok(None);
    };
    if let Some(mode) = value.as_str() {
        return match mode {
            "auto" => Ok(Some(CanonicalToolChoice {
                mode: ToolChoiceMode::Auto,
                function_name: None,
            })),
            "required" => Ok(Some(CanonicalToolChoice {
                mode: ToolChoiceMode::Required,
                function_name: None,
            })),
            "none" => Ok(Some(CanonicalToolChoice {
                mode: ToolChoiceMode::None,
                function_name: None,
            })),
            _ => Err(AdmissionError::InvalidField {
                field: "tool_choice",
            }),
        };
    }
    let object = value.as_object().ok_or(AdmissionError::InvalidField {
        field: "tool_choice",
    })?;
    if object.get("type").and_then(Value::as_str) == Some("any") {
        return Ok(Some(CanonicalToolChoice {
            mode: ToolChoiceMode::Required,
            function_name: None,
        }));
    }
    let function = object
        .get("function")
        .and_then(Value::as_object)
        .unwrap_or(object);
    let name = string_value(function.get("name"))?.ok_or(AdmissionError::InvalidField {
        field: "tool_choice.name",
    })?;
    Ok(Some(CanonicalToolChoice {
        mode: ToolChoiceMode::Function,
        function_name: Some(name),
    }))
}

fn decode_reasoning(object: &Map<String, Value>) -> Result<ReasoningIntent, AdmissionError> {
    if let Some(value) = object.get("reasoning_effort") {
        let effort = value.as_str().ok_or(AdmissionError::InvalidField {
            field: "reasoning_effort",
        })?;
        if effort.trim().is_empty() {
            return Err(AdmissionError::InvalidField {
                field: "reasoning_effort",
            });
        }
        return Ok(ReasoningIntent {
            requested: Some(true),
            mode: ReasoningMode::Effort,
            effort: Some(effort.into()),
            budget_tokens: None,
            explicit_disable: false,
        });
    }
    if let Some(value) = object.get("reasoning") {
        if let Some(enabled) = value.as_bool() {
            return Ok(if enabled {
                ReasoningIntent {
                    requested: Some(true),
                    mode: ReasoningMode::Toggle,
                    ..ReasoningIntent::default()
                }
            } else {
                ReasoningIntent::disabled()
            });
        }
        let reasoning = value
            .as_object()
            .ok_or(AdmissionError::InvalidField { field: "reasoning" })?;
        if let Some(effort) = reasoning.get("effort") {
            let effort = effort.as_str().ok_or(AdmissionError::InvalidField {
                field: "reasoning.effort",
            })?;
            return Ok(ReasoningIntent {
                requested: Some(true),
                mode: ReasoningMode::Effort,
                effort: Some(effort.into()),
                budget_tokens: None,
                explicit_disable: false,
            });
        }
        if let Some(enabled) = reasoning.get("enabled").and_then(Value::as_bool) {
            return Ok(if enabled {
                ReasoningIntent {
                    requested: Some(true),
                    mode: ReasoningMode::Toggle,
                    ..ReasoningIntent::default()
                }
            } else {
                ReasoningIntent::disabled()
            });
        }
        return Err(AdmissionError::InvalidField { field: "reasoning" });
    }
    if let Some(value) = object.get("thinking") {
        let thinking = value
            .as_object()
            .ok_or(AdmissionError::InvalidField { field: "thinking" })?;
        match thinking.get("type").and_then(Value::as_str) {
            Some("disabled") | Some("none") => return Ok(ReasoningIntent::disabled()),
            Some("adaptive") => {
                return Ok(ReasoningIntent {
                    requested: Some(true),
                    mode: ReasoningMode::Adaptive,
                    ..ReasoningIntent::default()
                });
            }
            _ => {}
        }
        if let Some(budget) = thinking.get("budget_tokens") {
            return positive_budget(budget, "thinking.budget_tokens").map(ReasoningIntent::fixed);
        }
        if thinking.get("type").and_then(Value::as_str) == Some("enabled") {
            return Ok(ReasoningIntent {
                requested: Some(true),
                mode: ReasoningMode::Toggle,
                ..ReasoningIntent::default()
            });
        }
        return Err(AdmissionError::InvalidField { field: "thinking" });
    }
    if let Some(value) = object.get("thinking_budget") {
        return positive_budget(value, "thinking_budget").map(ReasoningIntent::fixed);
    }
    Ok(ReasoningIntent::default())
}

fn positive_budget(value: &Value, _field: &'static str) -> Result<u64, AdmissionError> {
    let budget = value.as_u64().ok_or(AdmissionError::InvalidLimit {
        field: "thinking_budget",
    })?;
    if budget == 0 {
        return Err(AdmissionError::InvalidLimit {
            field: "thinking_budget",
        });
    }
    Ok(budget)
}

fn validate_media_limits(messages: &[CanonicalMessage]) -> Result<(), AdmissionError> {
    let mut media_count = 0_usize;
    let mut aggregate = 0_u64;
    for message in messages {
        for block in &message.content {
            if matches!(
                block.kind,
                CanonicalBlockKind::Image
                    | CanonicalBlockKind::Document
                    | CanonicalBlockKind::Audio
            ) {
                media_count = media_count.saturating_add(1);
                if media_count > MAX_MEDIA_ITEMS {
                    return Err(AdmissionError::CollectionLimit { kind: "media" });
                }
            }
            if let Some(media) = &block.media {
                if let Some(uri) = &media.uri
                    && uri.len() > MAX_MEDIA_URI_BYTES
                {
                    return Err(AdmissionError::MediaLimit { kind: "media URI" });
                }
                if let Some(file_id) = &media.file_id
                    && file_id.len() > MAX_MEDIA_URI_BYTES
                {
                    return Err(AdmissionError::MediaLimit {
                        kind: "file reference",
                    });
                }
                if let Some(data) = &media.data {
                    let limit = if block.kind == CanonicalBlockKind::Image {
                        MAX_IMAGE_BYTES
                    } else if block.kind == CanonicalBlockKind::Document {
                        MAX_PDF_BYTES
                    } else if block.kind == CanonicalBlockKind::Audio {
                        MAX_AUDIO_BYTES
                    } else {
                        u64::MAX
                    };
                    if limit != u64::MAX {
                        let decoded = super::limits::decoded_base64_len(data).ok_or(
                            AdmissionError::InvalidField {
                                field: "media.data",
                            },
                        )?;
                        aggregate = aggregate.saturating_add(decoded);
                        if decoded > limit || aggregate > MAX_MEDIA_AGGREGATE_BYTES {
                            return Err(AdmissionError::MediaLimit {
                                kind: if block.kind == CanonicalBlockKind::Image {
                                    "image"
                                } else if block.kind == CanonicalBlockKind::Document {
                                    "document"
                                } else {
                                    "audio"
                                },
                            });
                        }
                    }
                }
                if let Some(media_type) = &media.media_type
                    && !validate_media_type(media_type)
                {
                    return Err(AdmissionError::InvalidField {
                        field: "media.media_type",
                    });
                }
                if let Some(detail) = &media.detail
                    && !matches!(detail.as_str(), "auto" | "low" | "medium" | "high")
                {
                    return Err(AdmissionError::MediaLimit {
                        kind: "image detail",
                    });
                }
            }
        }
    }
    Ok(())
}

fn string_field(
    object: &Map<String, Value>,
    field: &'static str,
) -> Result<String, AdmissionError> {
    object
        .get(field)
        .and_then(Value::as_str)
        .map(str::to_owned)
        .ok_or(if field == "model" {
            AdmissionError::InvalidModel
        } else {
            AdmissionError::InvalidField { field }
        })
}

fn string_value(value: Option<&Value>) -> Result<Option<String>, AdmissionError> {
    match value {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(value)) => Ok(Some(value.clone())),
        Some(_) => Err(AdmissionError::InvalidField { field: "string" }),
    }
}

fn bool_field(
    object: &Map<String, Value>,
    field: &'static str,
    default: bool,
) -> Result<bool, AdmissionError> {
    match object.get(field) {
        None | Some(Value::Null) => Ok(default),
        Some(Value::Bool(value)) => Ok(*value),
        Some(_) => Err(AdmissionError::InvalidField { field }),
    }
}

fn optional_bool(
    object: &Map<String, Value>,
    field: &str,
) -> Result<Presence<bool>, AdmissionError> {
    match object.get(field) {
        None => Ok(Presence::Missing),
        Some(Value::Null) => Ok(Presence::Null),
        Some(Value::Bool(value)) => Ok(Presence::Value(*value)),
        Some(_) => Err(AdmissionError::InvalidField { field: "boolean" }),
    }
}

fn number_field(
    object: &Map<String, Value>,
    field: &'static str,
) -> Result<Option<f64>, AdmissionError> {
    match object.get(field) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::Number(value)) => {
            let number = value
                .as_f64()
                .ok_or(AdmissionError::InvalidField { field })?;
            if !number.is_finite() {
                return Err(AdmissionError::InvalidField { field });
            }
            Ok(Some(number))
        }
        Some(_) => Err(AdmissionError::InvalidField { field }),
    }
}

fn mapping_field(
    object: &Map<String, Value>,
    field: &'static str,
) -> Result<Option<Map<String, Value>>, AdmissionError> {
    match object.get(field) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::Object(value)) => Ok(Some(value.clone())),
        Some(_) => Err(AdmissionError::InvalidField { field }),
    }
}

fn output_key(surface: ClientSurface, _protocol: &str) -> &'static str {
    if surface == ClientSurface::Responses {
        "max_output_tokens"
    } else {
        "max_tokens"
    }
}
fn stop_key(surface: ClientSurface) -> &'static str {
    if surface == ClientSurface::Messages {
        "stop_sequences"
    } else {
        "stop"
    }
}

fn output_limit(
    object: &Map<String, Value>,
    surface: ClientSurface,
    protocol: &str,
) -> Result<Option<u64>, AdmissionError> {
    let value = Value::Object(object.clone());
    let resolved = requested_output_tokens(&value, protocol, surface.as_str()).map_err(|_| {
        AdmissionError::InvalidLimit {
            field: output_key(surface, protocol),
        }
    })?;
    if resolved.is_none() {
        let keys = if surface == ClientSurface::Responses {
            ["max_output_tokens", "max_completion_tokens", "max_tokens"]
        } else if protocol == "anthropic" {
            ["max_tokens", "", ""]
        } else {
            ["max_completion_tokens", "max_tokens", ""]
        };
        if keys
            .iter()
            .filter(|key| !key.is_empty())
            .any(|key| object.get(*key).and_then(Value::as_u64) == Some(0))
        {
            return Ok(Some(0));
        }
    }
    Ok(resolved)
}

fn stop_values(
    object: &Map<String, Value>,
    surface: ClientSurface,
) -> Result<Option<Vec<String>>, AdmissionError> {
    let key = stop_key(surface);
    let Some(value) = object.get(key) else {
        return Ok(None);
    };
    if value.is_null() {
        return Ok(None);
    }
    let values = if let Some(value) = value.as_str() {
        vec![value.to_owned()]
    } else {
        value
            .as_array()
            .ok_or(AdmissionError::InvalidField { field: key })?
            .iter()
            .map(|item| {
                item.as_str()
                    .map(str::to_owned)
                    .ok_or(AdmissionError::InvalidField { field: key })
            })
            .collect::<Result<Vec<_>, _>>()?
    };
    Ok((!values.is_empty()).then_some(values))
}

fn decode_stop_presence(value: &Value) -> Option<Vec<String>> {
    if let Some(value) = value.as_str() {
        return Some(vec![value.into()]);
    }
    value
        .as_array()?
        .iter()
        .map(|value| value.as_str().map(str::to_owned))
        .collect()
}

fn decode_metadata(value: Option<&Value>) -> Result<BTreeMap<String, String>, AdmissionError> {
    let Some(value) = value else {
        return Ok(BTreeMap::new());
    };
    let object = value
        .as_object()
        .ok_or(AdmissionError::InvalidField { field: "metadata" })?;
    if object.len() > MAX_METADATA {
        return Err(AdmissionError::CollectionLimit { kind: "metadata" });
    }
    object
        .iter()
        .map(|(key, value)| {
            Ok((
                key.clone(),
                value
                    .as_str()
                    .ok_or(AdmissionError::InvalidField { field: "metadata" })?
                    .to_owned(),
            ))
        })
        .collect()
}

fn content_text(value: Option<&Value>) -> Result<Option<String>, AdmissionError> {
    match value {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(text)) => Ok(Some(text.clone())),
        Some(Value::Array(items)) => {
            let mut result = String::new();
            for item in items {
                let object = item.as_object().ok_or(AdmissionError::InvalidField {
                    field: "tool_result.content",
                })?;
                if object.get("type").and_then(Value::as_str) == Some("text") {
                    result.push_str(object.get("text").and_then(Value::as_str).unwrap_or(""));
                }
            }
            Ok(Some(result))
        }
        Some(_) => Err(AdmissionError::InvalidField {
            field: "tool_result.content",
        }),
    }
}

fn decode_tool_result_media(value: Option<&Value>) -> Result<Option<MediaSource>, AdmissionError> {
    let Some(Value::Array(items)) = value else {
        return Ok(None);
    };
    for item in items {
        let object = item.as_object().ok_or(AdmissionError::InvalidField {
            field: "tool_result.content",
        })?;
        let block = match object.get("type").and_then(Value::as_str) {
            Some("image_url") => {
                let image = object
                    .get("image_url")
                    .and_then(Value::as_object)
                    .ok_or(AdmissionError::InvalidField { field: "image_url" })?;
                let url = image.get("url").and_then(Value::as_str).ok_or(
                    AdmissionError::InvalidField {
                        field: "image_url.url",
                    },
                )?;
                data_uri_block(url, CanonicalBlockKind::Image, MAX_IMAGE_BYTES)?
            }
            Some("image") | Some("input_image") => {
                decode_media_block(object, CanonicalBlockKind::Image, MAX_IMAGE_BYTES)?
            }
            Some("document") | Some("file") | Some("input_file") => decode_document_block(object)?,
            _ => continue,
        };
        return Ok(block.media);
    }
    Ok(None)
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
}
