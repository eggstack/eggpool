//! Pure sans-I/O structural wire decoder boundary.
//!
//! This module owns protocol-structure decoding and bounded media/tool/content
//! shape validation. It never performs raw-body size/depth admission
//! ownership, stateless Responses product policy, token/context estimation,
//! generation/resource ownership, or routing/affinity projection — those stay
//! EggPool-owned in the request admission layer.
//!
//! EggPool adapters pass [`DecodeLimits::current()`] so acceptance stays
//! exactly equal to the pre-extraction constants. Do not broaden or tighten
//! acceptance here.

use std::collections::{BTreeMap, BTreeSet};

use serde_json::{Map, Value};
use thiserror::Error;

use crate::ir::{
    CanonicalBlockKind, CanonicalContentBlock, CanonicalMessage, CanonicalRequest, CanonicalRole,
    CanonicalTool, CanonicalToolChoice, CanonicalToolKind, ClientSurface,
    MAX_TOOL_SEARCH_DESCRIPTION_BYTES, MAX_TOOL_SEARCH_OUTPUT_BYTES, MAX_TOOL_SEARCH_TOOLS,
    MediaSource, Presence, ReasoningIntent, ReasoningMode, RequestPresence, TOOL_SEARCH_TOOL_NAME,
    ToolChoiceMode, default_tool_search_parameters, validate_tool_search_arguments_string,
    validate_tool_search_arguments_value,
};

// --- Protocol-structure limits (exact pre-extraction values) ---

pub const MAX_JSON_DEPTH: usize = 64;
pub const MAX_MESSAGES: usize = 1_024;
pub const MAX_CONTENT_BLOCKS: usize = 2_048;
pub const MAX_TOOLS: usize = 256;
pub const MAX_METADATA: usize = 128;
pub const MAX_NATIVE_EXTENSION_FIELDS: usize = 32;

// Media limits (exact pre-extraction values from `request::limits`).
pub const MAX_IMAGE_BYTES: u64 = 5 * 1024 * 1024;
pub const MAX_PDF_BYTES: u64 = 32 * 1024 * 1024;
pub const MAX_AUDIO_BYTES: u64 = 32 * 1024 * 1024;
pub const MAX_MEDIA_ITEMS: usize = 64;
pub const MAX_MEDIA_AGGREGATE_BYTES: u64 = 40 * 1024 * 1024;
pub const MAX_MEDIA_URI_BYTES: usize = 8 * 1024;
pub const MAX_MEDIA_TYPE_BYTES: usize = 128;
pub const MAX_CACHE_MARKER_BYTES: usize = 1024;

/// Explicit decode limits/options for protocol parsing.
///
/// EggPool adapters must pass values exactly equal to the current constants;
/// this struct exists only to make the boundary explicit for extraction.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DecodeLimits {
    pub max_json_depth: usize,
    pub max_messages: usize,
    pub max_content_blocks: usize,
    pub max_tools: usize,
    pub max_metadata: usize,
    pub max_image_bytes: u64,
    pub max_pdf_bytes: u64,
    pub max_audio_bytes: u64,
    pub max_media_items: usize,
    pub max_media_aggregate_bytes: u64,
    pub max_media_uri_bytes: usize,
    pub max_media_type_bytes: usize,
    pub max_cache_marker_bytes: usize,
}

impl DecodeLimits {
    /// Exact pre-extraction acceptance. Do not change without a compat plan.
    pub const fn current() -> Self {
        Self {
            max_json_depth: MAX_JSON_DEPTH,
            max_messages: MAX_MESSAGES,
            max_content_blocks: MAX_CONTENT_BLOCKS,
            max_tools: MAX_TOOLS,
            max_metadata: MAX_METADATA,
            max_image_bytes: MAX_IMAGE_BYTES,
            max_pdf_bytes: MAX_PDF_BYTES,
            max_audio_bytes: MAX_AUDIO_BYTES,
            max_media_items: MAX_MEDIA_ITEMS,
            max_media_aggregate_bytes: MAX_MEDIA_AGGREGATE_BYTES,
            max_media_uri_bytes: MAX_MEDIA_URI_BYTES,
            max_media_type_bytes: MAX_MEDIA_TYPE_BYTES,
            max_cache_marker_bytes: MAX_CACHE_MARKER_BYTES,
        }
    }
}

impl Default for DecodeLimits {
    fn default() -> Self {
        Self::current()
    }
}

/// Structural decode failure without EggPool admission/product policy.
///
/// Variants mirror the protocol-relevant subset of the EggPool admission
/// error so error precedence maps 1:1.
#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum DecodeError {
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

// --- Pure base64/media validators (moved from `request::limits`) ---

pub fn decoded_base64_len(encoded: &str) -> Option<u64> {
    if encoded.is_empty() || !encoded.len().is_multiple_of(4) {
        return None;
    }
    let padding = encoded
        .as_bytes()
        .iter()
        .rev()
        .take_while(|byte| **byte == b'=')
        .count();
    if padding > 2 || encoded.as_bytes()[..encoded.len() - padding].contains(&b'=') {
        return None;
    }
    if !encoded
        .as_bytes()
        .iter()
        .all(|byte| byte.is_ascii_alphanumeric() || matches!(*byte, b'+' | b'/' | b'='))
    {
        return None;
    }
    let quartets = (encoded.len() / 4) as u64;
    quartets
        .checked_mul(3)
        .and_then(|length| length.checked_sub(padding as u64))
}

pub fn base64_definitely_exceeds(encoded: &str, limit_bytes: u64) -> bool {
    if !encoded.len().is_multiple_of(4) {
        return false;
    }
    let minimum_decoded = (encoded.len() as u64 / 4)
        .checked_mul(3)
        .and_then(|value| value.checked_sub(2))
        .unwrap_or(u64::MAX);
    minimum_decoded > limit_bytes
}

#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum MediaLimitError {
    #[error("invalid base64 payload")]
    InvalidBase64,
    #[error("base64 payload exceeds {kind} limit")]
    EncodedPayloadTooLarge { kind: &'static str },
}

pub fn validate_base64(
    encoded: &str,
    limit_bytes: u64,
    kind: &'static str,
) -> Result<u64, MediaLimitError> {
    if base64_definitely_exceeds(encoded, limit_bytes) {
        return Err(MediaLimitError::EncodedPayloadTooLarge { kind });
    }
    let Some(decoded_len) = decoded_base64_len(encoded) else {
        return Err(MediaLimitError::InvalidBase64);
    };
    if decoded_len > limit_bytes {
        return Err(MediaLimitError::EncodedPayloadTooLarge { kind });
    }
    Ok(decoded_len)
}

pub fn validate_media_type_with_limit(media_type: &str, max_type_bytes: usize) -> bool {
    !media_type.is_empty()
        && media_type.len() <= max_type_bytes
        && media_type.is_ascii()
        && !media_type.chars().any(char::is_whitespace)
        && media_type.split_once('/').is_some_and(|(kind, subtype)| {
            !kind.is_empty()
                && !subtype.is_empty()
                && kind.chars().all(|c| c.is_ascii_alphanumeric() || c == '-')
                && subtype
                    .chars()
                    .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '+' | '-'))
        })
}

pub fn validate_media_type(media_type: &str) -> bool {
    validate_media_type_with_limit(media_type, MAX_MEDIA_TYPE_BYTES)
}

pub fn valid_reference_with_limit(value: &str, max_uri_bytes: usize) -> bool {
    value.len() <= max_uri_bytes
        && ((value.starts_with("http://") || value.starts_with("https://"))
            || value.starts_with("file_")
            || value.starts_with("file-")
            || value.starts_with("urn:"))
}

pub fn valid_reference(value: &str) -> bool {
    valid_reference_with_limit(value, MAX_MEDIA_URI_BYTES)
}

/// Validate JSON depth without retaining the value.
pub fn validate_value_depth(
    value: &Value,
    depth: usize,
    max_depth: usize,
) -> Result<(), DecodeError> {
    if depth >= max_depth {
        return Err(DecodeError::DepthLimit);
    }
    match value {
        Value::Array(items) => items
            .iter()
            .try_for_each(|item| validate_value_depth(item, depth + 1, max_depth)),
        Value::Object(items) => items
            .values()
            .try_for_each(|item| validate_value_depth(item, depth + 1, max_depth)),
        Value::Null | Value::Bool(_) | Value::Number(_) | Value::String(_) => Ok(()),
    }
}

/// Decode entry point used by finite codecs: depth-check then structural
/// decode with explicit limits. Stateless Responses product policy stays
/// EggPool-owned and is enforced by the EggPool wrapper, not here.
pub fn canonical_request_from_value_with_limits(
    value: &Value,
    surface: crate::ir::ClientSurface,
    limits: DecodeLimits,
) -> Result<crate::ir::CanonicalRequest, DecodeError> {
    validate_value_depth(value, 0, limits.max_json_depth)?;
    value
        .as_object()
        .ok_or(DecodeError::TopLevelNotObject)
        .and_then(|object| canonical_request_from_object_with_limits(object, surface, limits))
    // Note: full structural decoder body is linked in below.
}

#[allow(clippy::too_many_lines)]
pub fn canonical_request_from_object_with_limits(
    object: &Map<String, Value>,
    surface: crate::ir::ClientSurface,
    limits: DecodeLimits,
) -> Result<crate::ir::CanonicalRequest, DecodeError> {
    structural_canonical_request_from_object(object, surface, limits)
}

// --- Pure structural decoder (moved from the EggPool admission layer) ---
//
// Byte/value-equivalent to the pre-extraction structural path, parameterized
// by [`DecodeLimits`]. The stateless Responses product-policy check is
// intentionally absent here; the EggPool wrapper enforces it.

#[allow(clippy::too_many_lines)]
pub fn structural_canonical_request_from_object(
    object: &Map<String, Value>,
    surface: ClientSurface,
    limits: DecodeLimits,
) -> Result<CanonicalRequest, DecodeError> {
    let model = string_field(object, "model")?.trim().to_owned();
    if model.is_empty() {
        return Err(DecodeError::InvalidModel);
    }
    let protocol = surface.protocol();
    let messages = decode_messages(object, protocol, surface, limits)?;
    let stream = bool_field(object, "stream", false)?;
    let max_output_tokens = output_limit(object, surface, protocol)?;
    let temperature = number_field(object, "temperature")?;
    let top_p = number_field(object, "top_p")?;
    let stop = stop_values(object, surface)?;
    let tools = decode_tools(object.get("tools"), surface, limits)?;
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
                    .ok_or(DecodeError::InvalidField {
                        field: "text.format",
                    })
            })?
    } else {
        mapping_field(object, "response_format")?
    };
    let reasoning = decode_reasoning(object)?;
    let cache_control = bounded_marker(object.get("cache_control"), limits)?;
    let metadata = decode_metadata(object.get("metadata"), limits)?;
    let parallel_tool_calls = optional_bool(object, "parallel_tool_calls")?;
    validate_media_limits(&messages, limits)?;
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
    limits: DecodeLimits,
) -> Result<Vec<CanonicalMessage>, DecodeError> {
    if surface == ClientSurface::Responses {
        let mut messages = Vec::new();
        if let Some(instructions) = object.get("instructions") {
            messages.push(CanonicalMessage {
                role: CanonicalRole::System,
                content: vec![CanonicalContentBlock::text(instructions.as_str().ok_or(
                    DecodeError::InvalidField {
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
            messages.extend(decode_message_array(input, protocol, surface, limits)?);
        }
        return Ok(messages);
    }
    let mut messages = Vec::new();
    if protocol == "anthropic"
        && let Some(system) = object.get("system")
    {
        messages.push(CanonicalMessage {
            role: CanonicalRole::System,
            content: decode_content(system, CanonicalRole::System, surface, limits)?,
            tool_call_id: None,
            name: None,
            refusal: None,
        });
    }
    let raw = object.get("messages").or_else(|| object.get("contents"));
    if let Some(value) = raw {
        messages.extend(decode_message_array(value, protocol, surface, limits)?);
    }
    Ok(messages)
}

fn decode_message_array(
    value: &Value,
    protocol: &str,
    surface: ClientSurface,
    limits: DecodeLimits,
) -> Result<Vec<CanonicalMessage>, DecodeError> {
    let items = value
        .as_array()
        .ok_or(DecodeError::InvalidField { field: "messages" })?;
    if items.len() > limits.max_messages {
        return Err(DecodeError::CollectionLimit { kind: "messages" });
    }
    items
        .iter()
        .map(|item| -> Result<Option<CanonicalMessage>, DecodeError> {
            let object = item.as_object().ok_or(DecodeError::InvalidField {
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
                .ok_or(DecodeError::InvalidField {
                    field: "messages[].content",
                })?;
            let mut content = decode_content(content_value, role, surface, limits)?;
            if let Some(calls) = object.get("tool_calls") {
                let calls = calls.as_array().ok_or(DecodeError::InvalidField {
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
                    return Err(DecodeError::InvalidField {
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

fn decode_role(value: Option<&Value>, protocol: &str) -> Result<CanonicalRole, DecodeError> {
    let role = value
        .and_then(Value::as_str)
        .ok_or(DecodeError::InvalidField {
            field: "messages[].role",
        })?;
    match (role, protocol) {
        ("system", _) => Ok(CanonicalRole::System),
        ("developer", _) => Ok(CanonicalRole::Developer),
        ("user", _) => Ok(CanonicalRole::User),
        ("assistant", _) | ("model", "gemini") => Ok(CanonicalRole::Assistant),
        ("tool", _) => Ok(CanonicalRole::Tool),
        _ => Err(DecodeError::InvalidField {
            field: "messages[].role",
        }),
    }
}

fn decode_content(
    value: &Value,
    role: CanonicalRole,
    surface: ClientSurface,
    limits: DecodeLimits,
) -> Result<Vec<CanonicalContentBlock>, DecodeError> {
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
        .ok_or(DecodeError::InvalidField { field: "content" })?;
    if blocks.len() > limits.max_content_blocks {
        return Err(DecodeError::CollectionLimit {
            kind: "content blocks",
        });
    }
    blocks
        .iter()
        .map(|block| decode_content_block(block, role, surface, limits))
        .collect()
}

fn decode_content_block(
    value: &Value,
    role: CanonicalRole,
    _surface: ClientSurface,
    limits: DecodeLimits,
) -> Result<CanonicalContentBlock, DecodeError> {
    let object = value
        .as_object()
        .ok_or(DecodeError::InvalidField { field: "content[]" })?;
    let kind = object
        .get("type")
        .and_then(Value::as_str)
        .ok_or(DecodeError::InvalidField {
            field: "content[].type",
        })?;
    match kind {
        "text" | "input_text" | "output_text" => {
            let mut block = CanonicalContentBlock::text(string_field(object, "text")?);
            block.cache_control = bounded_marker(object.get("cache_control"), limits)?;
            block.prompt_cache_breakpoint =
                bounded_marker(object.get("prompt_cache_breakpoint"), limits)?;
            Ok(block)
        }
        "image_url" => {
            let image = object
                .get("image_url")
                .and_then(Value::as_object)
                .ok_or(DecodeError::InvalidField { field: "image_url" })?;
            let url =
                image
                    .get("url")
                    .and_then(Value::as_str)
                    .ok_or(DecodeError::InvalidField {
                        field: "image_url.url",
                    })?;
            let mut block = data_uri_block(
                url,
                CanonicalBlockKind::Image,
                limits.max_image_bytes,
                limits,
            )?;
            if let Some(media) = block.media.as_mut() {
                media.detail =
                    bounded_detail(image.get("detail").or_else(|| image.get("quality")))?;
            }
            block.prompt_cache_breakpoint =
                bounded_marker(object.get("prompt_cache_breakpoint"), limits)?;
            block.cache_control = bounded_marker(object.get("cache_control"), limits)?;
            Ok(block)
        }
        "input_image" => {
            if let Some(url) = object.get("image_url").and_then(Value::as_str) {
                let mut block = data_uri_block(
                    url,
                    CanonicalBlockKind::Image,
                    limits.max_image_bytes,
                    limits,
                )?;
                if let Some(media) = block.media.as_mut() {
                    media.detail =
                        bounded_detail(object.get("detail").or_else(|| object.get("quality")))?;
                }
                block.prompt_cache_breakpoint =
                    bounded_marker(object.get("prompt_cache_breakpoint"), limits)?;
                block.cache_control = bounded_marker(object.get("cache_control"), limits)?;
                return Ok(block);
            }
            let mut block = decode_media_block(
                object,
                CanonicalBlockKind::Image,
                limits.max_image_bytes,
                limits,
            )?;
            if let Some(media) = block.media.as_mut() {
                media.detail =
                    bounded_detail(object.get("detail").or_else(|| object.get("quality")))?;
            }
            block.prompt_cache_breakpoint =
                bounded_marker(object.get("prompt_cache_breakpoint"), limits)?;
            block.cache_control = bounded_marker(object.get("cache_control"), limits)?;
            Ok(block)
        }
        "image" => {
            let mut block = decode_media_block(
                object,
                CanonicalBlockKind::Image,
                limits.max_image_bytes,
                limits,
            )?;
            if let Some(media) = block.media.as_mut() {
                media.detail =
                    bounded_detail(object.get("detail").or_else(|| object.get("quality")))?;
            }
            block.prompt_cache_breakpoint =
                bounded_marker(object.get("prompt_cache_breakpoint"), limits)?;
            block.cache_control = bounded_marker(object.get("cache_control"), limits)?;
            Ok(block)
        }
        "file" | "document" | "input_file" => {
            let mut block = decode_document_block(object, limits)?;
            block.cache_control = bounded_marker(object.get("cache_control"), limits)?;
            block.prompt_cache_breakpoint =
                bounded_marker(object.get("prompt_cache_breakpoint"), limits)?;
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
            cache_control: bounded_marker(object.get("cache_control"), limits)?,
            prompt_cache_breakpoint: bounded_marker(object.get("prompt_cache_breakpoint"), limits)?,
        }),
        "tool_use" => Ok(tool_call_block(object, true)?),
        "tool_result" => Ok(CanonicalContentBlock {
            kind: CanonicalBlockKind::ToolResult,
            text: content_text(object.get("content"))?,
            media: decode_tool_result_media(object.get("content"), limits)?,
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
        other => Err(DecodeError::UnsupportedContent { kind: other.into() }),
    }
}

fn decode_media_block(
    object: &Map<String, Value>,
    kind: CanonicalBlockKind,
    limit: u64,
    limits: DecodeLimits,
) -> Result<CanonicalContentBlock, DecodeError> {
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
        && !validate_media_type_with_limit(media_type, limits.max_media_type_bytes)
    {
        return Err(DecodeError::InvalidField {
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
        let encoded = data.as_deref().ok_or(DecodeError::InvalidField {
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
        return Err(DecodeError::InvalidField {
            field: "media.source",
        });
    }
    if let Some(uri) = uri.as_deref()
        && !valid_reference_with_limit(uri, limits.max_media_uri_bytes)
    {
        return Err(DecodeError::InvalidField { field: "media.url" });
    }
    if let Some(file_id) = file_id.as_deref()
        && !valid_reference_with_limit(file_id, limits.max_media_uri_bytes)
    {
        return Err(DecodeError::InvalidField {
            field: "media.file_id",
        });
    }
    Ok(media_block(kind, uri.as_deref(), media_type, data, file_id))
}

fn decode_document_block(
    object: &Map<String, Value>,
    limits: DecodeLimits,
) -> Result<CanonicalContentBlock, DecodeError> {
    if let Some(file) = object.get("file").and_then(Value::as_object) {
        if let Some(file_data) = file.get("file_data").and_then(Value::as_str) {
            return data_uri_block(
                file_data,
                CanonicalBlockKind::Document,
                limits.max_pdf_bytes,
                limits,
            );
        }
        if let Some(file_id) = file.get("file_id").and_then(Value::as_str) {
            if !valid_reference_with_limit(file_id, limits.max_media_uri_bytes) {
                return Err(DecodeError::InvalidField {
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
    decode_media_block(
        object,
        CanonicalBlockKind::Document,
        limits.max_pdf_bytes,
        limits,
    )
}

fn media_error(error: MediaLimitError, kind: CanonicalBlockKind) -> DecodeError {
    match error {
        MediaLimitError::EncodedPayloadTooLarge { .. } => DecodeError::MediaLimit {
            kind: if kind == CanonicalBlockKind::Image {
                "image"
            } else {
                "document"
            },
        },
        MediaLimitError::InvalidBase64 => DecodeError::InvalidField {
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
    limits: DecodeLimits,
) -> Result<CanonicalContentBlock, DecodeError> {
    if let Some((media_type, encoded)) = uri
        .strip_prefix("data:")
        .and_then(|value| value.split_once(";base64,"))
    {
        if !validate_media_type_with_limit(media_type, limits.max_media_type_bytes) {
            return Err(DecodeError::InvalidField {
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
        if !valid_reference_with_limit(uri, limits.max_media_uri_bytes) {
            return Err(DecodeError::InvalidField { field: "media.url" });
        }
        return Ok(media_block(kind, Some(uri), None, None, None));
    }
    Err(DecodeError::UnsupportedContent {
        kind: "media URI".into(),
    })
}

fn bounded_detail(value: Option<&Value>) -> Result<Option<String>, DecodeError> {
    let Some(value) = value else {
        return Ok(None);
    };
    let Some(detail) = value.as_str() else {
        return Err(DecodeError::InvalidField {
            field: "image.detail",
        });
    };
    if !matches!(detail, "auto" | "low" | "medium" | "high") {
        return Err(DecodeError::InvalidField {
            field: "image.detail",
        });
    }
    Ok(Some(detail.to_owned()))
}

fn bounded_marker(
    value: Option<&Value>,
    limits: DecodeLimits,
) -> Result<Option<Value>, DecodeError> {
    let Some(value) = value else {
        return Ok(None);
    };
    if value.is_null() {
        return Ok(None);
    }
    if serde_json::to_vec(value)
        .map(|encoded| encoded.len() > limits.max_cache_marker_bytes)
        .unwrap_or(true)
    {
        return Err(DecodeError::MediaLimit {
            kind: "cache marker",
        });
    }
    Ok(Some(value.clone()))
}

fn decode_openai_tool_call(value: &Value) -> Result<CanonicalContentBlock, DecodeError> {
    let object = value.as_object().ok_or(DecodeError::InvalidField {
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
) -> Result<CanonicalContentBlock, DecodeError> {
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
) -> Result<CanonicalContentBlock, DecodeError> {
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
) -> Result<CanonicalContentBlock, DecodeError> {
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
) -> Result<CanonicalContentBlock, DecodeError> {
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
) -> Result<CanonicalContentBlock, DecodeError> {
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

/// Decide whether a tool declaration is the portable client-executed
/// `tool_search` form.
///
/// This is the single semantic owner for the predicate shared by canonical
/// structural tool decoding and EggPool native preservation/feature
/// summarization. A declaration is portable only when `type == "tool_search"`
/// with `execution == "client"`; server-owned, missing, null, wrong-type, or
/// non-`tool_search` declarations stay native-only and must not become
/// portable client declarations.
///
/// Pure over the already parsed JSON object: no I/O, no allocation beyond
/// the caller's value, no admission ownership. Preservation lifetime and
/// stateless product policy remain EggPool-owned; this helper only answers
/// the classification question.
pub fn is_client_tool_search_declaration(object: &Map<String, Value>) -> bool {
    object.get("type").and_then(Value::as_str) == Some("tool_search")
        && object.get("execution").and_then(Value::as_str) == Some("client")
}

fn decode_tool_search_declaration(
    object: &Map<String, Value>,
) -> Result<CanonicalTool, DecodeError> {
    if let Some(name) = object.get("name").and_then(Value::as_str)
        && name != TOOL_SEARCH_TOOL_NAME
    {
        return Err(DecodeError::InvalidField {
            field: "tools[].name",
        });
    }
    let description = string_value(object.get("description"))?;
    if description
        .as_deref()
        .is_some_and(|description| description.len() > MAX_TOOL_SEARCH_DESCRIPTION_BYTES)
    {
        return Err(DecodeError::InvalidField {
            field: "tools[].description",
        });
    }
    let parameters = object
        .get("parameters")
        .map(|value| {
            value.as_object().cloned().ok_or(DecodeError::InvalidField {
                field: "tools[].parameters",
            })
        })
        .transpose()?
        .unwrap_or_else(default_tool_search_parameters);
    if serde_json::to_vec(&parameters)
        .map(|encoded| encoded.len() > 16 * 1024)
        .unwrap_or(true)
    {
        return Err(DecodeError::InvalidField {
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
) -> Result<CanonicalContentBlock, DecodeError> {
    let call_id = string_value(object.get("call_id"))?.ok_or(DecodeError::InvalidField {
        field: "tool_call.id",
    })?;
    if call_id.trim().is_empty() {
        return Err(DecodeError::InvalidField {
            field: "tool_call.id",
        });
    }
    if let Some(name) = object.get("name").and_then(Value::as_str)
        && name != TOOL_SEARCH_TOOL_NAME
    {
        return Err(DecodeError::InvalidField {
            field: "tool_call.name",
        });
    }
    let arguments_value = object.get("arguments").ok_or(DecodeError::InvalidField {
        field: "tool_call.arguments",
    })?;
    // Current Codex sends an object; accept a JSON string defensively but
    // still validate the bounded `query`/`limit` shape.
    let arguments_string = if let Some(object) = arguments_value.as_object() {
        validate_tool_search_arguments_value(&Value::Object(object.clone())).ok_or(
            DecodeError::InvalidField {
                field: "tool_call.arguments",
            },
        )?
    } else if let Some(text) = arguments_value.as_str() {
        validate_tool_search_arguments_string(text).ok_or(DecodeError::InvalidField {
            field: "tool_call.arguments",
        })?
    } else {
        return Err(DecodeError::InvalidField {
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
) -> Result<CanonicalContentBlock, DecodeError> {
    let call_id = string_value(object.get("call_id"))?.ok_or(DecodeError::InvalidField {
        field: "tool_result.call_id",
    })?;
    if call_id.trim().is_empty() {
        return Err(DecodeError::InvalidField {
            field: "tool_result.call_id",
        });
    }
    let tools = object
        .get("tools")
        .and_then(Value::as_array)
        .ok_or(DecodeError::InvalidField {
            field: "tool_result.output",
        })?;
    if tools.len() > MAX_TOOL_SEARCH_TOOLS {
        return Err(DecodeError::CollectionLimit {
            kind: "tool_search_output.tools",
        });
    }
    for tool in tools {
        if !tool.is_object() {
            return Err(DecodeError::InvalidField {
                field: "tool_result.output",
            });
        }
    }
    let encoded = serde_json::to_string(tools).map_err(|_| DecodeError::InvalidField {
        field: "tool_result.output",
    })?;
    if encoded.len() > MAX_TOOL_SEARCH_OUTPUT_BYTES {
        return Err(DecodeError::CollectionLimit {
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
    limits: DecodeLimits,
) -> Result<Vec<CanonicalTool>, DecodeError> {
    let Some(value) = value else {
        return Ok(Vec::new());
    };
    let items = value
        .as_array()
        .ok_or(DecodeError::InvalidField { field: "tools" })?;
    if items.len() > limits.max_tools {
        return Err(DecodeError::CollectionLimit { kind: "tools" });
    }
    items
        .iter()
        .map(|item| -> Result<Option<CanonicalTool>, DecodeError> {
            let object = item
                .as_object()
                .ok_or(DecodeError::InvalidField { field: "tools[]" })?;
            if surface == ClientSurface::Responses
                && let Some(kind) = object.get("type")
            {
                let kind = kind.as_str().ok_or(DecodeError::InvalidField {
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
                cache_control: bounded_marker(function.get("cache_control"), limits)?,
                defer_loading: function.get("defer_loading").and_then(Value::as_bool),
            }))
        })
        .collect::<Result<Vec<_>, _>>()
        .map(|tools| tools.into_iter().flatten().collect())
}

fn validate_tool_identity(
    tools: &[CanonicalTool],
    messages: &[CanonicalMessage],
) -> Result<(), DecodeError> {
    let mut names = BTreeSet::new();
    for tool in tools {
        if tool.name.trim().is_empty() || !names.insert(tool.name.clone()) {
            return Err(DecodeError::InvalidField {
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
                        return Err(DecodeError::InvalidField {
                            field: "tool_call.id",
                        });
                    };
                    if call_id.trim().is_empty() || !calls.insert(call_id.to_owned()) {
                        return Err(DecodeError::InvalidField {
                            field: "tool_call.id",
                        });
                    }
                    if block.name.as_deref().is_none_or(str::is_empty) {
                        return Err(DecodeError::InvalidField {
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
                    return Err(DecodeError::InvalidField {
                        field: "tool_result.call_id",
                    });
                }
                _ => {}
            }
        }
    }
    Ok(())
}

fn decode_tool_choice(value: Option<&Value>) -> Result<Option<CanonicalToolChoice>, DecodeError> {
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
            _ => Err(DecodeError::InvalidField {
                field: "tool_choice",
            }),
        };
    }
    let object = value.as_object().ok_or(DecodeError::InvalidField {
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
    let name = string_value(function.get("name"))?.ok_or(DecodeError::InvalidField {
        field: "tool_choice.name",
    })?;
    Ok(Some(CanonicalToolChoice {
        mode: ToolChoiceMode::Function,
        function_name: Some(name),
    }))
}

fn decode_reasoning(object: &Map<String, Value>) -> Result<ReasoningIntent, DecodeError> {
    if let Some(value) = object.get("reasoning_effort") {
        let effort = value.as_str().ok_or(DecodeError::InvalidField {
            field: "reasoning_effort",
        })?;
        if effort.trim().is_empty() {
            return Err(DecodeError::InvalidField {
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
            .ok_or(DecodeError::InvalidField { field: "reasoning" })?;
        if let Some(effort) = reasoning.get("effort") {
            let effort = effort.as_str().ok_or(DecodeError::InvalidField {
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
        return Err(DecodeError::InvalidField { field: "reasoning" });
    }
    if let Some(value) = object.get("thinking") {
        let thinking = value
            .as_object()
            .ok_or(DecodeError::InvalidField { field: "thinking" })?;
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
        return Err(DecodeError::InvalidField { field: "thinking" });
    }
    if let Some(value) = object.get("thinking_budget") {
        return positive_budget(value, "thinking_budget").map(ReasoningIntent::fixed);
    }
    Ok(ReasoningIntent::default())
}

fn positive_budget(value: &Value, _field: &'static str) -> Result<u64, DecodeError> {
    let budget = value.as_u64().ok_or(DecodeError::InvalidLimit {
        field: "thinking_budget",
    })?;
    if budget == 0 {
        return Err(DecodeError::InvalidLimit {
            field: "thinking_budget",
        });
    }
    Ok(budget)
}

fn validate_media_limits(
    messages: &[CanonicalMessage],
    limits: DecodeLimits,
) -> Result<(), DecodeError> {
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
                if media_count > limits.max_media_items {
                    return Err(DecodeError::CollectionLimit { kind: "media" });
                }
            }
            if let Some(media) = &block.media {
                if let Some(uri) = &media.uri
                    && uri.len() > limits.max_media_uri_bytes
                {
                    return Err(DecodeError::MediaLimit { kind: "media URI" });
                }
                if let Some(file_id) = &media.file_id
                    && file_id.len() > limits.max_media_uri_bytes
                {
                    return Err(DecodeError::MediaLimit {
                        kind: "file reference",
                    });
                }
                if let Some(data) = &media.data {
                    let limit = if block.kind == CanonicalBlockKind::Image {
                        limits.max_image_bytes
                    } else if block.kind == CanonicalBlockKind::Document {
                        limits.max_pdf_bytes
                    } else if block.kind == CanonicalBlockKind::Audio {
                        limits.max_audio_bytes
                    } else {
                        u64::MAX
                    };
                    if limit != u64::MAX {
                        let decoded =
                            decoded_base64_len(data).ok_or(DecodeError::InvalidField {
                                field: "media.data",
                            })?;
                        aggregate = aggregate.saturating_add(decoded);
                        if decoded > limit || aggregate > limits.max_media_aggregate_bytes {
                            return Err(DecodeError::MediaLimit {
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
                    && !validate_media_type_with_limit(media_type, limits.max_media_type_bytes)
                {
                    return Err(DecodeError::InvalidField {
                        field: "media.media_type",
                    });
                }
                if let Some(detail) = &media.detail
                    && !matches!(detail.as_str(), "auto" | "low" | "medium" | "high")
                {
                    return Err(DecodeError::MediaLimit {
                        kind: "image detail",
                    });
                }
            }
        }
    }
    Ok(())
}

fn string_field(object: &Map<String, Value>, field: &'static str) -> Result<String, DecodeError> {
    object
        .get(field)
        .and_then(Value::as_str)
        .map(str::to_owned)
        .ok_or(if field == "model" {
            DecodeError::InvalidModel
        } else {
            DecodeError::InvalidField { field }
        })
}

fn string_value(value: Option<&Value>) -> Result<Option<String>, DecodeError> {
    match value {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(value)) => Ok(Some(value.clone())),
        Some(_) => Err(DecodeError::InvalidField { field: "string" }),
    }
}

fn bool_field(
    object: &Map<String, Value>,
    field: &'static str,
    default: bool,
) -> Result<bool, DecodeError> {
    match object.get(field) {
        None | Some(Value::Null) => Ok(default),
        Some(Value::Bool(value)) => Ok(*value),
        Some(_) => Err(DecodeError::InvalidField { field }),
    }
}

fn optional_bool(object: &Map<String, Value>, field: &str) -> Result<Presence<bool>, DecodeError> {
    match object.get(field) {
        None => Ok(Presence::Missing),
        Some(Value::Null) => Ok(Presence::Null),
        Some(Value::Bool(value)) => Ok(Presence::Value(*value)),
        Some(_) => Err(DecodeError::InvalidField { field: "boolean" }),
    }
}

fn number_field(
    object: &Map<String, Value>,
    field: &'static str,
) -> Result<Option<f64>, DecodeError> {
    match object.get(field) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::Number(value)) => {
            let number = value.as_f64().ok_or(DecodeError::InvalidField { field })?;
            if !number.is_finite() {
                return Err(DecodeError::InvalidField { field });
            }
            Ok(Some(number))
        }
        Some(_) => Err(DecodeError::InvalidField { field }),
    }
}

fn mapping_field(
    object: &Map<String, Value>,
    field: &'static str,
) -> Result<Option<Map<String, Value>>, DecodeError> {
    match object.get(field) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::Object(value)) => Ok(Some(value.clone())),
        Some(_) => Err(DecodeError::InvalidField { field }),
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

fn decode_requested_output_tokens(
    object: &Map<String, Value>,
    protocol: &str,
    surface: ClientSurface,
) -> Result<Option<u64>, DecodeError> {
    let keys: &[&str] = if surface == ClientSurface::Responses {
        &["max_output_tokens", "max_completion_tokens", "max_tokens"]
    } else if protocol == "anthropic" {
        &["max_tokens"]
    } else {
        &["max_completion_tokens", "max_tokens"]
    };
    for key in keys {
        if let Some(candidate) = object.get(*key) {
            if candidate.is_null() {
                continue;
            }
            let Some(number) = candidate.as_u64() else {
                return Err(DecodeError::InvalidLimit {
                    field: output_key(surface, protocol),
                });
            };
            return Ok((number > 0).then_some(number));
        }
    }
    Ok(None)
}

fn output_limit(
    object: &Map<String, Value>,
    surface: ClientSurface,
    protocol: &str,
) -> Result<Option<u64>, DecodeError> {
    let resolved = decode_requested_output_tokens(object, protocol, surface)?;
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
) -> Result<Option<Vec<String>>, DecodeError> {
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
            .ok_or(DecodeError::InvalidField { field: key })?
            .iter()
            .map(|item| {
                item.as_str()
                    .map(str::to_owned)
                    .ok_or(DecodeError::InvalidField { field: key })
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

fn decode_metadata(
    value: Option<&Value>,
    limits: DecodeLimits,
) -> Result<BTreeMap<String, String>, DecodeError> {
    let Some(value) = value else {
        return Ok(BTreeMap::new());
    };
    let object = value
        .as_object()
        .ok_or(DecodeError::InvalidField { field: "metadata" })?;
    if object.len() > limits.max_metadata {
        return Err(DecodeError::CollectionLimit { kind: "metadata" });
    }
    object
        .iter()
        .map(|(key, value)| {
            Ok((
                key.clone(),
                value
                    .as_str()
                    .ok_or(DecodeError::InvalidField { field: "metadata" })?
                    .to_owned(),
            ))
        })
        .collect()
}

fn content_text(value: Option<&Value>) -> Result<Option<String>, DecodeError> {
    match value {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(text)) => Ok(Some(text.clone())),
        Some(Value::Array(items)) => {
            let mut result = String::new();
            for item in items {
                let object = item.as_object().ok_or(DecodeError::InvalidField {
                    field: "tool_result.content",
                })?;
                if object.get("type").and_then(Value::as_str) == Some("text") {
                    result.push_str(object.get("text").and_then(Value::as_str).unwrap_or(""));
                }
            }
            Ok(Some(result))
        }
        Some(_) => Err(DecodeError::InvalidField {
            field: "tool_result.content",
        }),
    }
}

fn decode_tool_result_media(
    value: Option<&Value>,
    limits: DecodeLimits,
) -> Result<Option<MediaSource>, DecodeError> {
    let Some(Value::Array(items)) = value else {
        return Ok(None);
    };
    for item in items {
        let object = item.as_object().ok_or(DecodeError::InvalidField {
            field: "tool_result.content",
        })?;
        let block =
            match object.get("type").and_then(Value::as_str) {
                Some("image_url") => {
                    let image = object
                        .get("image_url")
                        .and_then(Value::as_object)
                        .ok_or(DecodeError::InvalidField { field: "image_url" })?;
                    let url = image.get("url").and_then(Value::as_str).ok_or(
                        DecodeError::InvalidField {
                            field: "image_url.url",
                        },
                    )?;
                    data_uri_block(
                        url,
                        CanonicalBlockKind::Image,
                        limits.max_image_bytes,
                        limits,
                    )?
                }
                Some("image") | Some("input_image") => decode_media_block(
                    object,
                    CanonicalBlockKind::Image,
                    limits.max_image_bytes,
                    limits,
                )?,
                Some("document") | Some("file") | Some("input_file") => {
                    decode_document_block(object, limits)?
                }
                _ => continue,
            };
        return Ok(block.media);
    }
    Ok(None)
}

#[cfg(test)]
mod tests {
    use super::is_client_tool_search_declaration;
    use serde_json::{Map, Value, json};

    fn object(value: Value) -> Map<String, Value> {
        value.as_object().cloned().expect("test value is an object")
    }

    #[test]
    fn client_tool_search_classifier_matrix() {
        let cases: &[(&str, Value, bool)] = &[
            (
                "client execution is portable",
                json!({"type": "tool_search", "execution": "client"}),
                true,
            ),
            (
                "server execution stays native-only",
                json!({"type": "tool_search", "execution": "server"}),
                false,
            ),
            (
                "missing execution stays native-only",
                json!({"type": "tool_search"}),
                false,
            ),
            (
                "null execution stays native-only",
                json!({"type": "tool_search", "execution": null}),
                false,
            ),
            (
                "wrong-type execution stays native-only",
                json!({"type": "tool_search", "execution": 1}),
                false,
            ),
            (
                "empty execution stays native-only",
                json!({"type": "tool_search", "execution": ""}),
                false,
            ),
            (
                "non-tool_search declaration is not classified",
                json!({"type": "function", "execution": "client", "name": "lookup"}),
                false,
            ),
            (
                "missing type is not classified",
                json!({"execution": "client"}),
                false,
            ),
            (
                "null type is not classified",
                json!({"type": null, "execution": "client"}),
                false,
            ),
            (
                "client declaration with extra fields stays portable",
                json!({"type": "tool_search", "execution": "client", "description": "search"}),
                true,
            ),
        ];
        for (name, value, expected) in cases {
            let object = object(value.clone());
            assert_eq!(
                is_client_tool_search_declaration(&object),
                *expected,
                "case: {name}"
            );
        }
    }
}
