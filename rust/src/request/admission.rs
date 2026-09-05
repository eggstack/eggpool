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
        CanonicalRole, CanonicalTool, CanonicalToolChoice, ClientSurface, MediaSource, Presence,
        ReasoningIntent, ReasoningMode, RequestPresence, ToolChoiceMode,
    },
};

use super::limits::{
    DEFAULT_MAX_REQUEST_BODY_BYTES, LimitError, MAX_IMAGE_BYTES, MAX_PDF_BYTES,
    estimate_context_input_tokens, estimate_reservation_tokens, requested_output_tokens,
    validate_base64,
};

const MAX_JSON_DEPTH: usize = 64;
const MAX_MESSAGES: usize = 1_024;
const MAX_CONTENT_BLOCKS: usize = 2_048;
const MAX_TOOLS: usize = 256;
const MAX_METADATA: usize = 128;

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
}

#[derive(Debug, Clone, PartialEq)]
pub struct AdmittedRequest {
    pub canonical: CanonicalRequest,
    pub raw_body_bytes: usize,
    pub reservation_tokens: u64,
    pub context_tokens: u64,
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
    if raw_body.len() > options.max_body_bytes {
        return Err(AdmissionError::BodyTooLarge {
            length: raw_body.len(),
            limit: options.max_body_bytes,
        });
    }
    let value = parse_once(raw_body)?;
    let object = value.as_object().ok_or(AdmissionError::TopLevelNotObject)?;
    let canonical = canonical_request_from_object(object, options.client_surface)?;
    let reservation_tokens = estimate_reservation_tokens(raw_body);
    let context_tokens =
        estimate_context_input_tokens(raw_body, &value, options.extra_context_tokens);
    Ok(AdmittedRequest {
        canonical,
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

fn canonical_request_from_object(
    object: &Map<String, Value>,
    surface: ClientSurface,
) -> Result<CanonicalRequest, AdmissionError> {
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
    let tools = decode_tools(object.get("tools"))?;
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
    let cache_control = object.get("cache_control").cloned();
    let metadata = decode_metadata(object.get("metadata"))?;
    let parallel_tool_calls = optional_bool(object, "parallel_tool_calls")?;
    validate_media_limits(&messages)?;
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
    if protocol == "anthropic" {
        if let Some(system) = object.get("system") {
            messages.push(CanonicalMessage {
                role: CanonicalRole::System,
                content: decode_content(system, CanonicalRole::System, surface)?,
                tool_call_id: None,
                name: None,
                refusal: None,
            });
        }
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
        .map(|item| {
            let object = item.as_object().ok_or(AdmissionError::InvalidField {
                field: "messages[]",
            })?;
            if surface == ClientSurface::Responses {
                match object.get("type").and_then(Value::as_str) {
                    Some("function_call") => {
                        return Ok(CanonicalMessage {
                            role: CanonicalRole::Assistant,
                            content: vec![decode_response_function_call(object)?],
                            tool_call_id: None,
                            name: None,
                            refusal: None,
                        });
                    }
                    Some("function_call_output") => {
                        return Ok(CanonicalMessage {
                            role: CanonicalRole::Tool,
                            content: vec![decode_response_function_output(object)?],
                            tool_call_id: None,
                            name: None,
                            refusal: None,
                        });
                    }
                    Some("message") | None => {}
                    Some(_) => {
                        return Err(AdmissionError::UnsupportedContent {
                            kind: "input item".into(),
                        });
                    }
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
            Ok(CanonicalMessage {
                role,
                content,
                tool_call_id: string_value(object.get("tool_call_id"))?,
                name: string_value(object.get("name"))?,
                refusal: string_value(object.get("refusal"))?,
            })
        })
        .collect()
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
            Ok(CanonicalContentBlock::text(string_field(object, "text")?))
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
            data_uri_block(url, CanonicalBlockKind::Image, MAX_IMAGE_BYTES)
        }
        "image" | "input_image" => {
            decode_media_block(object, CanonicalBlockKind::Image, MAX_IMAGE_BYTES)
        }
        "file" | "document" | "input_file" => decode_document_block(object),
        "input_audio" | "audio" => Ok(CanonicalContentBlock {
            kind: CanonicalBlockKind::Audio,
            text: None,
            media: Some(MediaSource {
                media_type: None,
                data: object
                    .get("data")
                    .and_then(Value::as_str)
                    .map(str::to_owned),
                uri: None,
            }),
            call_id: None,
            name: None,
            arguments: None,
            tool_input: None,
            is_error: false,
            signature: None,
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
            is_error: false,
            signature: object
                .get("signature")
                .and_then(Value::as_str)
                .map(str::to_owned),
        }),
        "tool_use" => Ok(tool_call_block(object, true)?),
        "tool_result" => Ok(CanonicalContentBlock {
            kind: CanonicalBlockKind::ToolResult,
            text: content_text(object.get("content"))?,
            media: None,
            call_id: string_value(object.get("tool_use_id"))?,
            name: None,
            arguments: None,
            tool_input: None,
            is_error: object
                .get("is_error")
                .and_then(Value::as_bool)
                .unwrap_or(false),
            signature: None,
        }),
        "refusal" if role == CanonicalRole::Assistant => Ok(CanonicalContentBlock {
            kind: CanonicalBlockKind::Refusal,
            text: string_value(object.get("refusal").or_else(|| object.get("text")))?,
            media: None,
            call_id: None,
            name: None,
            arguments: None,
            tool_input: None,
            is_error: false,
            signature: None,
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
    let data = source
        .get("data")
        .and_then(Value::as_str)
        .map(str::to_owned);
    let uri = source
        .get("url")
        .and_then(Value::as_str)
        .or_else(|| object.get("url").and_then(Value::as_str))
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
    } else if uri.is_none() {
        return Err(AdmissionError::InvalidField {
            field: "media.source",
        });
    }
    Ok(media_block(kind, uri.as_deref(), media_type, data))
}

fn decode_document_block(
    object: &Map<String, Value>,
) -> Result<CanonicalContentBlock, AdmissionError> {
    if let Some(file) = object.get("file").and_then(Value::as_object) {
        if let Some(file_data) = file.get("file_data").and_then(Value::as_str) {
            return data_uri_block(file_data, CanonicalBlockKind::Document, MAX_PDF_BYTES);
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
) -> CanonicalContentBlock {
    CanonicalContentBlock {
        kind,
        text: None,
        media: Some(MediaSource {
            media_type,
            data,
            uri: uri.map(str::to_owned),
        }),
        call_id: None,
        name: None,
        arguments: None,
        tool_input: None,
        is_error: false,
        signature: None,
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
        ));
    }
    if uri.starts_with("http://") || uri.starts_with("https://") {
        return Ok(media_block(kind, Some(uri), None, None));
    }
    Err(AdmissionError::UnsupportedContent {
        kind: "media URI".into(),
    })
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
        is_error: false,
        signature: None,
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
        is_error: false,
        signature: None,
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
        is_error: false,
        signature: None,
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
        is_error: false,
        signature: None,
    })
}

fn decode_tools(value: Option<&Value>) -> Result<Vec<CanonicalTool>, AdmissionError> {
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
        .map(|item| {
            let object = item
                .as_object()
                .ok_or(AdmissionError::InvalidField { field: "tools[]" })?;
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
            Ok(CanonicalTool {
                name,
                description: string_value(function.get("description"))?,
                parameters,
            })
        })
        .collect()
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
    for message in messages {
        for block in &message.content {
            if let Some(media) = &block.media {
                if let Some(data) = &media.data {
                    let limit = if block.kind == CanonicalBlockKind::Image {
                        MAX_IMAGE_BYTES
                    } else if block.kind == CanonicalBlockKind::Document {
                        MAX_PDF_BYTES
                    } else {
                        u64::MAX
                    };
                    if limit != u64::MAX && data.len() as u64 > limit.saturating_mul(2) {
                        return Err(AdmissionError::MediaLimit {
                            kind: if block.kind == CanonicalBlockKind::Image {
                                "image"
                            } else {
                                "document"
                            },
                        });
                    }
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
    requested_output_tokens(&value, protocol, surface.as_str()).map_err(|_| {
        AdmissionError::InvalidLimit {
            field: output_key(surface, protocol),
        }
    })
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
