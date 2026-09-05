//! Finite OpenAI Chat Completions and Anthropic Messages codecs.
//!
//! The codecs in this module are deliberately synchronous and side-effect
//! free.  They translate between the already-admitted JSON boundary and the
//! source-owned canonical IR.  Provider transport, retry, streaming framing,
//! and loss-policy orchestration remain outside this module.

use serde_json::{Map, Value, json};

use super::adaptation::{client_wire_surface, notice, request_notices};
use super::codec::{
    CodecError, CodecOutput, CodecReasonCode, DecodedProviderPayload, StreamAdapterKind, WireCodec,
    WireCodecId,
};
use super::ir::{
    CacheCounterStatus, CanonicalBlockKind, CanonicalContentBlock, CanonicalMessage,
    CanonicalOutputBlock, CanonicalRequest, CanonicalResponse, CanonicalRole, CanonicalToolChoice,
    CanonicalUsage, ClientSurface, ProviderErrorEvidence, ReasoningMode, ToolChoiceMode,
};
use super::registry::{ConfiguredWireProfile, WireSurface};
use crate::request::{AdmissionError, canonical_request_from_value};

const MAX_PROVIDER_ERROR_MESSAGE_BYTES: usize = 4 * 1024;

/// Concrete finite OpenAI Chat Completions codec.
#[derive(Debug, Clone, Copy, Default)]
pub struct OpenAiChatCodec;

/// Concrete finite Anthropic Messages codec.
#[derive(Debug, Clone, Copy, Default)]
pub struct AnthropicMessagesCodec;

/// Construct the finite codec selected by a non-streaming codec identifier.
///
/// SSE identifiers intentionally remain unimplemented until W008 owns the
/// incremental framing/event boundary.  Later family codecs extend this
/// closed dispatch function rather than introducing a second registry.
pub fn builtin_codec_instance(id: WireCodecId) -> Option<Box<dyn WireCodec>> {
    match id {
        WireCodecId::OpenaiChat => Some(Box::new(OpenAiChatCodec)),
        WireCodecId::AnthropicMessages => Some(Box::new(AnthropicMessagesCodec)),
        WireCodecId::OpenaiResponses => {
            Some(Box::new(super::additional_codecs::OpenAiResponsesCodec))
        }
        WireCodecId::GeminiInteractions => {
            Some(Box::new(super::additional_codecs::GeminiInteractionsCodec))
        }
        WireCodecId::GeminiGenerateContent => Some(Box::new(
            super::additional_codecs::GeminiGenerateContentCodec,
        )),
        WireCodecId::OpenaiResponsesSse
        | WireCodecId::OpenaiChatSse
        | WireCodecId::AnthropicMessagesSse
        | WireCodecId::GeminiInteractionsSse
        | WireCodecId::GeminiGenerateContentSse => None,
    }
}

impl WireCodec for OpenAiChatCodec {
    fn codec_id(&self) -> WireCodecId {
        WireCodecId::OpenaiChat
    }

    fn surface(&self) -> WireSurface {
        WireSurface::OpenaiChatCompletions
    }

    fn decode_client_request(
        &self,
        value: &Value,
        client_surface: ClientSurface,
    ) -> Result<CodecOutput<CanonicalRequest>, CodecError> {
        decode_request(value, client_surface)
    }

    fn encode_request(
        &self,
        request: &CanonicalRequest,
        profile: &ConfiguredWireProfile,
    ) -> Result<CodecOutput<Value>, CodecError> {
        ensure_profile(profile, self.surface())?;
        encode_openai_request(request)
    }

    fn decode_response(
        &self,
        payload: &Value,
        status: u16,
    ) -> Result<CodecOutput<DecodedProviderPayload>, CodecError> {
        decode_openai_response(payload, status)
    }

    fn encode_response(
        &self,
        response: &CanonicalResponse,
        client_surface: ClientSurface,
    ) -> Result<CodecOutput<Value>, CodecError> {
        match client_surface {
            ClientSurface::ChatCompletions => encode_openai_response(response),
            ClientSurface::Messages => encode_anthropic_response(response),
            ClientSurface::Responses => Err(codec_error(
                CodecReasonCode::UnsupportedWireProfile,
                Some("client_surface"),
                None,
                Some(WireSurface::OpenaiResponses),
            )),
        }
    }

    fn stream_adapter(&self) -> StreamAdapterKind {
        StreamAdapterKind::OpenaiChatSse
    }
}

impl WireCodec for AnthropicMessagesCodec {
    fn codec_id(&self) -> WireCodecId {
        WireCodecId::AnthropicMessages
    }

    fn surface(&self) -> WireSurface {
        WireSurface::AnthropicMessages
    }

    fn decode_client_request(
        &self,
        value: &Value,
        client_surface: ClientSurface,
    ) -> Result<CodecOutput<CanonicalRequest>, CodecError> {
        decode_request(value, client_surface)
    }

    fn encode_request(
        &self,
        request: &CanonicalRequest,
        profile: &ConfiguredWireProfile,
    ) -> Result<CodecOutput<Value>, CodecError> {
        ensure_profile(profile, self.surface())?;
        encode_anthropic_request(request)
    }

    fn decode_response(
        &self,
        payload: &Value,
        status: u16,
    ) -> Result<CodecOutput<DecodedProviderPayload>, CodecError> {
        decode_anthropic_response(payload, status)
    }

    fn encode_response(
        &self,
        response: &CanonicalResponse,
        client_surface: ClientSurface,
    ) -> Result<CodecOutput<Value>, CodecError> {
        match client_surface {
            ClientSurface::Messages => encode_anthropic_response(response),
            ClientSurface::ChatCompletions => encode_openai_response(response),
            ClientSurface::Responses => Err(codec_error(
                CodecReasonCode::UnsupportedWireProfile,
                Some("client_surface"),
                None,
                Some(WireSurface::OpenaiResponses),
            )),
        }
    }

    fn stream_adapter(&self) -> StreamAdapterKind {
        StreamAdapterKind::AnthropicMessagesSse
    }
}

fn decode_request(
    value: &Value,
    client_surface: ClientSurface,
) -> Result<CodecOutput<CanonicalRequest>, CodecError> {
    canonical_request_from_value(value, client_surface)
        .map(CodecOutput::new)
        .map_err(|error| map_admission_error(error, client_surface))
}

fn map_admission_error(error: AdmissionError, source: ClientSurface) -> CodecError {
    let (reason, field) = match error {
        AdmissionError::BodyTooLarge { .. }
        | AdmissionError::CollectionLimit { .. }
        | AdmissionError::DepthLimit
        | AdmissionError::MediaLimit { .. }
        | AdmissionError::InvalidLimit { .. }
        | AdmissionError::LengthOverflow => (CodecReasonCode::ResourceLimitViolation, None),
        AdmissionError::UnsupportedContent { .. } => {
            (CodecReasonCode::UnsupportedSemanticFeature, Some("content"))
        }
        AdmissionError::InvalidField { field } => {
            (CodecReasonCode::MalformedSourceRequest, Some(field))
        }
        AdmissionError::InvalidJson
        | AdmissionError::TopLevelNotObject
        | AdmissionError::InvalidModel => (CodecReasonCode::MalformedSourceRequest, None),
    };
    codec_error(reason, field, Some(client_wire_surface(source)), None)
}

fn ensure_profile(
    profile: &ConfiguredWireProfile,
    expected: WireSurface,
) -> Result<(), CodecError> {
    if profile.definition.surface != expected
        || profile.definition.request_codec.family() != expected_family(expected)
        || profile.definition.response_codec.family() != expected_family(expected)
    {
        return Err(codec_error(
            CodecReasonCode::UnsupportedWireProfile,
            Some("profile"),
            None,
            Some(profile.definition.surface),
        ));
    }
    Ok(())
}

fn expected_family(surface: WireSurface) -> super::registry::CodecFamily {
    match surface {
        WireSurface::OpenaiChatCompletions => super::registry::CodecFamily::OpenaiChat,
        WireSurface::AnthropicMessages => super::registry::CodecFamily::AnthropicMessages,
        WireSurface::OpenaiResponses => super::registry::CodecFamily::OpenaiResponses,
        WireSurface::GeminiInteractions => super::registry::CodecFamily::GeminiInteractions,
        WireSurface::GeminiGenerateContent => super::registry::CodecFamily::GeminiGenerateContent,
    }
}

fn encode_openai_request(request: &CanonicalRequest) -> Result<CodecOutput<Value>, CodecError> {
    validate_request_blocks(request, WireSurface::OpenaiChatCompletions)?;
    let notices = request_notices(request, WireSurface::OpenaiChatCompletions)?;
    let mut out = Map::new();
    out.insert("model".into(), Value::String(request.model.clone()));
    out.insert(
        "messages".into(),
        Value::Array(request.messages.iter().map(encode_openai_message).collect()),
    );
    add_common_request_fields(&mut out, request, "max_completion_tokens", false);
    if !request.tools.is_empty() {
        out.insert(
            "tools".into(),
            Value::Array(
                request
                    .tools
                    .iter()
                    .map(|tool| {
                        json!({
                            "type": "function",
                            "function": {
                                "name": tool.name,
                                "description": tool.description,
                                "parameters": Value::Object(tool.parameters.clone()),
                            }
                        })
                    })
                    .collect(),
            ),
        );
    }
    if let Some(choice) = &request.tool_choice {
        out.insert("tool_choice".into(), encode_openai_tool_choice(choice));
    }
    if request.reasoning.requested == Some(true)
        && request.reasoning.mode == ReasoningMode::Effort
        && request.reasoning.effort.is_some()
    {
        out.insert(
            "reasoning_effort".into(),
            Value::String(request.reasoning.effort.clone().unwrap_or_default()),
        );
    }
    if request.reasoning.explicit_disable {
        out.insert("reasoning_effort".into(), Value::String("none".into()));
    }
    Ok(CodecOutput {
        value: Value::Object(out),
        notices,
    })
}

fn encode_anthropic_request(request: &CanonicalRequest) -> Result<CodecOutput<Value>, CodecError> {
    validate_request_blocks(request, WireSurface::AnthropicMessages)?;
    let notices = request_notices(request, WireSurface::AnthropicMessages)?;
    let mut out = Map::new();
    out.insert("model".into(), Value::String(request.model.clone()));

    let mut system = Vec::new();
    let mut messages = Vec::with_capacity(request.messages.len());
    for message in &request.messages {
        if matches!(
            message.role,
            CanonicalRole::System | CanonicalRole::Developer
        ) {
            system.extend(message.content.iter().cloned());
            continue;
        }
        let mut item = Map::new();
        item.insert(
            "role".into(),
            Value::String(
                if message.role == CanonicalRole::Tool {
                    "user"
                } else {
                    message.role.as_str()
                }
                .into(),
            ),
        );
        item.insert(
            "content".into(),
            encode_anthropic_content(&message.content)?,
        );
        messages.push(Value::Object(item));
    }
    if !system.is_empty() {
        out.insert("system".into(), encode_anthropic_content(&system)?);
    }
    out.insert("messages".into(), Value::Array(messages));
    add_common_request_fields(&mut out, request, "max_tokens", true);
    if !request.tools.is_empty() {
        out.insert(
            "tools".into(),
            Value::Array(
                request
                    .tools
                    .iter()
                    .map(|tool| {
                        json!({
                            "name": tool.name,
                            "description": tool.description.clone().unwrap_or_default(),
                            "input_schema": Value::Object(tool.parameters.clone()),
                        })
                    })
                    .collect(),
            ),
        );
    }
    if let Some(choice) = &request.tool_choice {
        out.insert("tool_choice".into(), encode_anthropic_tool_choice(choice));
    }
    match request.reasoning.requested {
        Some(false) => {
            out.insert("thinking".into(), json!({"type": "disabled"}));
        }
        Some(true) if request.reasoning.mode == ReasoningMode::Adaptive => {
            out.insert("thinking".into(), json!({"type": "adaptive"}));
        }
        Some(true) if request.reasoning.budget_tokens.is_some() => {
            out.insert(
                "thinking".into(),
                json!({
                    "type": "enabled",
                    "budget_tokens": request.reasoning.budget_tokens
                }),
            );
        }
        _ => {}
    }
    if request.parallel_tool_calls.is_some() {
        out.remove("parallel_tool_calls");
    }
    Ok(CodecOutput {
        value: Value::Object(out),
        notices,
    })
}

fn add_common_request_fields(
    out: &mut Map<String, Value>,
    request: &CanonicalRequest,
    max_key: &str,
    anthropic: bool,
) {
    // The existing Python codecs deliberately materialize the stream boolean
    // on both finite request surfaces.  Presence remains available on the IR
    // for callers that need to distinguish an omitted source field.
    out.insert("stream".into(), Value::Bool(request.stream));
    if let Some(value) = request
        .max_output_tokens
        .or_else(|| request.presence.max_output_tokens.value().copied())
    {
        out.insert(max_key.into(), value.into());
    } else if matches!(
        request.presence.max_output_tokens,
        super::ir::Presence::Null
    ) {
        out.insert(max_key.into(), Value::Null);
    }
    if let Some(value) = request.temperature {
        out.insert("temperature".into(), value.into());
    } else if matches!(request.presence.temperature, super::ir::Presence::Null) {
        out.insert("temperature".into(), Value::Null);
    }
    if let Some(value) = request.top_p {
        out.insert("top_p".into(), value.into());
    } else if matches!(request.presence.top_p, super::ir::Presence::Null) {
        out.insert("top_p".into(), Value::Null);
    }
    if let Some(values) = &request.stop {
        out.insert("stop".into(), encode_stop(values));
    } else if matches!(request.presence.stop, super::ir::Presence::Null) {
        out.insert("stop".into(), Value::Null);
    }
    if let Some(value) = &request.response_format {
        if !anthropic {
            out.insert("response_format".into(), Value::Object(value.clone()));
        }
    } else if !anthropic && matches!(request.presence.response_format, super::ir::Presence::Null) {
        out.insert("response_format".into(), Value::Null);
    }
    if let Some(value) = request.parallel_tool_calls {
        out.insert("parallel_tool_calls".into(), Value::Bool(value));
    }
    if let Some(value) = &request.cache_control {
        out.insert("cache_control".into(), value.clone());
    }
    if !request.metadata.is_empty() {
        out.insert(
            "metadata".into(),
            Value::Object(
                request
                    .metadata
                    .iter()
                    .map(|(key, value)| (key.clone(), Value::String(value.clone())))
                    .collect(),
            ),
        );
    }
}

fn encode_stop(values: &[String]) -> Value {
    if values.len() == 1 {
        Value::String(values[0].clone())
    } else {
        Value::Array(values.iter().cloned().map(Value::String).collect())
    }
}

fn encode_openai_message(message: &CanonicalMessage) -> Value {
    let mut item = Map::new();
    item.insert("role".into(), Value::String(message.role.as_str().into()));
    item.insert(
        "content".into(),
        encode_openai_content_lossless(&message.content),
    );
    if let Some(id) = &message.tool_call_id {
        item.insert("tool_call_id".into(), Value::String(id.clone()));
    }
    let calls: Vec<Value> = message
        .content
        .iter()
        .filter(|block| block.kind == CanonicalBlockKind::ToolCall)
        .map(|block| {
            json!({
                "id": block.call_id,
                "type": "function",
                "function": {
                    "name": block.name,
                    "arguments": block.arguments.clone().unwrap_or_else(|| {
                        block.tool_input.as_ref().map(compact_json).unwrap_or_default()
                    }),
                }
            })
        })
        .collect();
    if !calls.is_empty() {
        item.insert("tool_calls".into(), Value::Array(calls));
    }
    if let Some(refusal) = &message.refusal {
        item.insert("refusal".into(), Value::String(refusal.clone()));
    }
    Value::Object(item)
}

fn encode_openai_content_lossless(content: &[CanonicalContentBlock]) -> Value {
    if content.len() == 1 && content[0].kind == CanonicalBlockKind::Text {
        return Value::String(content[0].text.clone().unwrap_or_default());
    }
    Value::Array(
        content
            .iter()
            .filter_map(|block| match block.kind {
                CanonicalBlockKind::Text => {
                    Some(json!({"type": "text", "text": block.text.clone().unwrap_or_default()}))
                }
                CanonicalBlockKind::Image => block.media.as_ref().map(|media| {
                    json!({
                        "type": "image_url",
                        "image_url": {"url": media.uri.clone().unwrap_or_else(|| format!(
                            "data:{};base64,{}",
                            media.media_type.as_deref().unwrap_or("application/octet-stream"),
                            media.data.as_deref().unwrap_or_default(),
                        ))}
                    })
                }),
                CanonicalBlockKind::Reasoning => {
                    Some(json!({"type": "reasoning_content", "text": block.text.clone().unwrap_or_default()}))
                }
                CanonicalBlockKind::Refusal => {
                    Some(json!({"type": "refusal", "refusal": block.text.clone().unwrap_or_default()}))
                }
                CanonicalBlockKind::ToolResult => {
                    Some(Value::String(block.text.clone().unwrap_or_default()))
                }
                CanonicalBlockKind::ToolCall
                | CanonicalBlockKind::Document
                | CanonicalBlockKind::Audio => None,
            })
            .collect(),
    )
}

fn encode_anthropic_content(content: &[CanonicalContentBlock]) -> Result<Value, CodecError> {
    if content.len() == 1 && content[0].kind == CanonicalBlockKind::Text {
        return Ok(Value::String(content[0].text.clone().unwrap_or_default()));
    }
    let mut result = Vec::with_capacity(content.len());
    for block in content {
        let value = match block.kind {
            CanonicalBlockKind::Text => {
                json!({"type": "text", "text": block.text.clone().unwrap_or_default()})
            }
            CanonicalBlockKind::Image => {
                let media = block.media.as_ref().ok_or_else(|| {
                    codec_error(
                        CodecReasonCode::UnsupportedSemanticFeature,
                        Some("content.image"),
                        None,
                        Some(WireSurface::AnthropicMessages),
                    )
                })?;
                if let Some(data) = &media.data {
                    json!({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media.media_type.clone().unwrap_or_else(|| "application/octet-stream".into()),
                            "data": data,
                        }
                    })
                } else if let Some(uri) = &media.uri {
                    json!({"type": "image", "source": {"type": "url", "url": uri}})
                } else {
                    return Err(codec_error(
                        CodecReasonCode::UnsupportedSemanticFeature,
                        Some("content.image"),
                        None,
                        Some(WireSurface::AnthropicMessages),
                    ));
                }
            }
            CanonicalBlockKind::Reasoning => {
                json!({"type": "thinking", "thinking": block.text.clone().unwrap_or_default()})
            }
            CanonicalBlockKind::ToolCall => {
                let input = tool_input_value(block)?;
                json!({
                    "type": "tool_use",
                    "id": block.call_id,
                    "name": block.name,
                    "input": input,
                })
            }
            CanonicalBlockKind::ToolResult => json!({
                "type": "tool_result",
                "tool_use_id": block.call_id,
                "content": block.text.clone().unwrap_or_default(),
                "is_error": block.is_error,
            }),
            CanonicalBlockKind::Refusal => json!({
                "type": "text",
                "text": block.text.clone().unwrap_or_default(),
            }),
            CanonicalBlockKind::Document | CanonicalBlockKind::Audio => {
                return Err(codec_error(
                    CodecReasonCode::UnsupportedSemanticFeature,
                    Some("content"),
                    None,
                    Some(WireSurface::AnthropicMessages),
                ));
            }
        };
        result.push(value);
    }
    Ok(Value::Array(result))
}

fn validate_request_blocks(
    request: &CanonicalRequest,
    target: WireSurface,
) -> Result<(), CodecError> {
    for message in &request.messages {
        for block in &message.content {
            if matches!(
                block.kind,
                CanonicalBlockKind::Document | CanonicalBlockKind::Audio
            ) {
                return Err(codec_error(
                    CodecReasonCode::UnsupportedSemanticFeature,
                    Some("content"),
                    Some(client_wire_surface(request.client_surface)),
                    Some(target),
                ));
            }
            if block.kind == CanonicalBlockKind::ToolCall {
                if block.call_id.is_none() || block.name.is_none() {
                    return Err(codec_error(
                        CodecReasonCode::MalformedSourceRequest,
                        Some("tool_call"),
                        Some(client_wire_surface(request.client_surface)),
                        Some(target),
                    ));
                }
                if target == WireSurface::AnthropicMessages
                    && block.tool_input.is_none()
                    && block.arguments.is_none()
                {
                    return Err(codec_error(
                        CodecReasonCode::MalformedSourceRequest,
                        Some("tool_call.arguments"),
                        Some(client_wire_surface(request.client_surface)),
                        Some(target),
                    ));
                }
            }
        }
    }
    Ok(())
}

fn encode_openai_tool_choice(choice: &CanonicalToolChoice) -> Value {
    match choice.mode {
        ToolChoiceMode::Function => {
            json!({"type": "function", "function": {"name": choice.function_name}})
        }
        ToolChoiceMode::Auto => Value::String("auto".into()),
        ToolChoiceMode::Required => Value::String("required".into()),
        ToolChoiceMode::None => Value::String("none".into()),
    }
}

fn encode_anthropic_tool_choice(choice: &CanonicalToolChoice) -> Value {
    match choice.mode {
        ToolChoiceMode::Function => json!({"type": "tool", "name": choice.function_name}),
        ToolChoiceMode::Required => json!({"type": "any"}),
        ToolChoiceMode::Auto => json!({"type": "auto"}),
        ToolChoiceMode::None => json!({"type": "none"}),
    }
}

fn decode_openai_response(
    payload: &Value,
    status: u16,
) -> Result<CodecOutput<DecodedProviderPayload>, CodecError> {
    if let Some(error) = decode_provider_error(payload, status, WireSurface::OpenaiChatCompletions)?
    {
        return Ok(CodecOutput::new(DecodedProviderPayload::Error(error)));
    }
    let object = response_object(payload, WireSurface::OpenaiChatCompletions)?;
    let choices = object
        .get("choices")
        .and_then(Value::as_array)
        .ok_or_else(|| response_error("choices"))?;
    if choices.is_empty() {
        return Err(response_error("choices"));
    }
    if choices.len() > 1 {
        return Err(codec_error(
            CodecReasonCode::UnsupportedSemanticFeature,
            Some("choices"),
            Some(WireSurface::OpenaiChatCompletions),
            None,
        ));
    }
    let choice = choices[0]
        .as_object()
        .ok_or_else(|| response_error("choices[]"))?;
    let message = choice
        .get("message")
        .and_then(Value::as_object)
        .ok_or_else(|| response_error("choices[].message"))?;
    let mut output = decode_openai_message_output(message)?;
    if let Some(calls) = message.get("tool_calls") {
        let calls = calls
            .as_array()
            .ok_or_else(|| response_error("tool_calls"))?;
        for call in calls {
            output.push(decode_openai_tool_call(call)?);
        }
    }
    let finish_reason = optional_string(choice, "finish_reason")?;
    let usage = decode_usage(object.get("usage"), UsageProtocol::Openai)?;
    let response = CanonicalResponse {
        response_id: optional_string(object, "id")?,
        model: optional_string(object, "model")?,
        output,
        finish_reason,
        usage,
        provider_error: None,
    };
    Ok(CodecOutput::new(DecodedProviderPayload::Response(
        Box::new(response),
    )))
}

fn decode_openai_message_output(
    message: &Map<String, Value>,
) -> Result<Vec<CanonicalOutputBlock>, CodecError> {
    let mut output = Vec::new();
    match message.get("content") {
        None | Some(Value::Null) => {}
        Some(Value::String(text)) => output.push(CanonicalOutputBlock {
            kind: CanonicalBlockKind::Text,
            text: Some(text.clone()),
            call_id: None,
            name: None,
            arguments: None,
        }),
        Some(Value::Array(blocks)) => {
            for block in blocks {
                let object = block
                    .as_object()
                    .ok_or_else(|| response_error("message.content[]"))?;
                let kind = object
                    .get("type")
                    .and_then(Value::as_str)
                    .ok_or_else(|| response_error("message.content[].type"))?;
                let text = match kind {
                    "text" => object
                        .get("text")
                        .and_then(Value::as_str)
                        .ok_or_else(|| response_error("message.content[].text"))?,
                    "reasoning_content" | "reasoning" => object
                        .get("text")
                        .and_then(Value::as_str)
                        .ok_or_else(|| response_error("message.content[].text"))?,
                    "refusal" => object
                        .get("refusal")
                        .and_then(Value::as_str)
                        .ok_or_else(|| response_error("message.content[].refusal"))?,
                    _ => {
                        return Err(codec_error(
                            CodecReasonCode::UnsupportedSemanticFeature,
                            Some("message.content[].type"),
                            Some(WireSurface::OpenaiChatCompletions),
                            None,
                        ));
                    }
                };
                let output_kind = match kind {
                    "reasoning_content" | "reasoning" => CanonicalBlockKind::Reasoning,
                    "refusal" => CanonicalBlockKind::Refusal,
                    _ => CanonicalBlockKind::Text,
                };
                output.push(CanonicalOutputBlock {
                    kind: output_kind,
                    text: Some(text.to_owned()),
                    call_id: None,
                    name: None,
                    arguments: None,
                });
            }
        }
        Some(_) => return Err(response_error("message.content")),
    }
    if let Some(refusal) = message.get("refusal") {
        let refusal = refusal
            .as_str()
            .ok_or_else(|| response_error("message.refusal"))?;
        output.push(CanonicalOutputBlock {
            kind: CanonicalBlockKind::Refusal,
            text: Some(refusal.to_owned()),
            call_id: None,
            name: None,
            arguments: None,
        });
    }
    Ok(output)
}

fn decode_openai_tool_call(value: &Value) -> Result<CanonicalOutputBlock, CodecError> {
    let object = value
        .as_object()
        .ok_or_else(|| response_error("tool_calls[]"))?;
    let id = required_string(object, "id", "tool_calls[].id")?;
    let function = object
        .get("function")
        .and_then(Value::as_object)
        .ok_or_else(|| response_error("tool_calls[].function"))?;
    let name = required_string(function, "name", "tool_calls[].function.name")?;
    let arguments = required_string(function, "arguments", "tool_calls[].function.arguments")?;
    Ok(CanonicalOutputBlock {
        kind: CanonicalBlockKind::ToolCall,
        text: None,
        call_id: Some(id),
        name: Some(name),
        arguments: Some(arguments),
    })
}

fn decode_anthropic_response(
    payload: &Value,
    status: u16,
) -> Result<CodecOutput<DecodedProviderPayload>, CodecError> {
    if let Some(error) = decode_provider_error(payload, status, WireSurface::AnthropicMessages)? {
        return Ok(CodecOutput::new(DecodedProviderPayload::Error(error)));
    }
    let object = response_object(payload, WireSurface::AnthropicMessages)?;
    let blocks = object
        .get("content")
        .and_then(Value::as_array)
        .ok_or_else(|| response_error("content"))?;
    let mut output = Vec::with_capacity(blocks.len());
    for block in blocks {
        let object = block
            .as_object()
            .ok_or_else(|| response_error("content[]"))?;
        let kind = object
            .get("type")
            .and_then(Value::as_str)
            .ok_or_else(|| response_error("content[].type"))?;
        match kind {
            "text" => output.push(CanonicalOutputBlock {
                kind: CanonicalBlockKind::Text,
                text: Some(required_string(object, "text", "content[].text")?),
                call_id: None,
                name: None,
                arguments: None,
            }),
            "thinking" => output.push(CanonicalOutputBlock {
                kind: CanonicalBlockKind::Reasoning,
                text: Some(required_string(object, "thinking", "content[].thinking")?),
                call_id: None,
                name: None,
                arguments: None,
            }),
            "tool_use" => {
                let input = object
                    .get("input")
                    .and_then(Value::as_object)
                    .ok_or_else(|| response_error("content[].input"))?;
                output.push(CanonicalOutputBlock {
                    kind: CanonicalBlockKind::ToolCall,
                    text: None,
                    call_id: Some(required_string(object, "id", "content[].id")?),
                    name: Some(required_string(object, "name", "content[].name")?),
                    arguments: Some(compact_json(input)),
                });
            }
            "redacted_thinking" => {
                return Err(codec_error(
                    CodecReasonCode::UnsupportedSemanticFeature,
                    Some("content[].type"),
                    Some(WireSurface::AnthropicMessages),
                    None,
                ));
            }
            _ => {
                return Err(codec_error(
                    CodecReasonCode::UnsupportedSemanticFeature,
                    Some("content[].type"),
                    Some(WireSurface::AnthropicMessages),
                    None,
                ));
            }
        }
    }
    let usage = decode_usage(object.get("usage"), UsageProtocol::Anthropic)?;
    let response = CanonicalResponse {
        response_id: optional_string(object, "id")?,
        model: optional_string(object, "model")?,
        output,
        finish_reason: optional_string(object, "stop_reason")?,
        usage,
        provider_error: None,
    };
    Ok(CodecOutput::new(DecodedProviderPayload::Response(
        Box::new(response),
    )))
}

pub(crate) fn encode_openai_response(
    response: &CanonicalResponse,
) -> Result<CodecOutput<Value>, CodecError> {
    validate_output_blocks(response, WireSurface::OpenaiChatCompletions)?;
    let mut message = Map::new();
    message.insert("role".into(), Value::String("assistant".into()));
    let text: String = response
        .output
        .iter()
        .filter(|block| block.kind == CanonicalBlockKind::Text)
        .filter_map(|block| block.text.as_deref())
        .collect();
    message.insert("content".into(), Value::String(text));
    let reasoning: String = response
        .output
        .iter()
        .filter(|block| block.kind == CanonicalBlockKind::Reasoning)
        .filter_map(|block| block.text.as_deref())
        .collect();
    if !reasoning.is_empty() {
        message.insert("reasoning_content".into(), Value::String(reasoning));
    }
    let refusals: String = response
        .output
        .iter()
        .filter(|block| block.kind == CanonicalBlockKind::Refusal)
        .filter_map(|block| block.text.as_deref())
        .collect();
    if !refusals.is_empty() {
        message.insert("refusal".into(), Value::String(refusals));
    }
    let calls: Vec<Value> = response
        .output
        .iter()
        .filter(|block| block.kind == CanonicalBlockKind::ToolCall)
        .map(|block| {
            json!({
                "id": block.call_id.clone().unwrap_or_default(),
                "type": "function",
                "function": {
                    "name": block.name.clone().unwrap_or_default(),
                    "arguments": block.arguments.clone().unwrap_or_default(),
                }
            })
        })
        .collect();
    if !calls.is_empty() {
        message.insert("tool_calls".into(), Value::Array(calls));
    }
    let mut choice = Map::new();
    choice.insert("index".into(), Value::from(0));
    choice.insert("message".into(), Value::Object(message));
    choice.insert(
        "finish_reason".into(),
        response
            .finish_reason
            .as_deref()
            .map_or(Value::Null, |reason| {
                Value::String(chat_finish_reason(reason))
            }),
    );
    let mut out = Map::new();
    out.insert(
        "id".into(),
        Value::String(response.response_id.clone().unwrap_or_default()),
    );
    out.insert("object".into(), Value::String("chat.completion".into()));
    out.insert(
        "model".into(),
        Value::String(response.model.clone().unwrap_or_default()),
    );
    out.insert("choices".into(), Value::Array(vec![Value::Object(choice)]));
    if let Some(usage) = &response.usage {
        out.insert("usage".into(), encode_openai_usage(usage));
    }
    Ok(CodecOutput::new(Value::Object(out)))
}

pub(crate) fn encode_anthropic_response(
    response: &CanonicalResponse,
) -> Result<CodecOutput<Value>, CodecError> {
    let mut content = Vec::new();
    let mut notices = Vec::new();
    for block in &response.output {
        match block.kind {
            CanonicalBlockKind::Text => content.push(json!({
                "type": "text",
                "text": block.text.clone().unwrap_or_default(),
            })),
            CanonicalBlockKind::Refusal => {
                notices.push(notice(
                    "refusal_not_representable",
                    "output.refusal",
                    None,
                    Some(WireSurface::AnthropicMessages),
                ));
                content.push(json!({
                    "type": "text",
                    "text": block.text.clone().unwrap_or_default(),
                }));
            }
            CanonicalBlockKind::Reasoning => content.push(json!({
                "type": "thinking",
                "thinking": block.text.clone().unwrap_or_default(),
            })),
            CanonicalBlockKind::ToolCall => {
                let arguments = block.arguments.as_deref().unwrap_or("{}");
                let input = serde_json::from_str::<Value>(arguments).map_err(|_| {
                    codec_error(
                        CodecReasonCode::MalformedProviderResponse,
                        Some("output.tool_call.arguments"),
                        None,
                        Some(WireSurface::AnthropicMessages),
                    )
                })?;
                if !input.is_object() {
                    return Err(codec_error(
                        CodecReasonCode::MalformedProviderResponse,
                        Some("output.tool_call.arguments"),
                        None,
                        Some(WireSurface::AnthropicMessages),
                    ));
                }
                content.push(json!({
                    "type": "tool_use",
                    "id": block.call_id.clone().unwrap_or_default(),
                    "name": block.name.clone().unwrap_or_default(),
                    "input": input,
                }));
            }
            CanonicalBlockKind::ToolResult => {
                return Err(codec_error(
                    CodecReasonCode::UnsupportedSemanticFeature,
                    Some("output.tool_result"),
                    None,
                    Some(WireSurface::AnthropicMessages),
                ));
            }
            CanonicalBlockKind::Image
            | CanonicalBlockKind::Document
            | CanonicalBlockKind::Audio => {
                return Err(codec_error(
                    CodecReasonCode::UnsupportedSemanticFeature,
                    Some("output.content"),
                    None,
                    Some(WireSurface::AnthropicMessages),
                ));
            }
        }
    }
    let mut out = Map::new();
    out.insert(
        "id".into(),
        Value::String(response.response_id.clone().unwrap_or_default()),
    );
    out.insert("type".into(), Value::String("message".into()));
    out.insert("role".into(), Value::String("assistant".into()));
    out.insert(
        "model".into(),
        Value::String(response.model.clone().unwrap_or_default()),
    );
    out.insert("content".into(), Value::Array(content));
    out.insert(
        "stop_reason".into(),
        response
            .finish_reason
            .as_deref()
            .map_or(Value::Null, |reason| {
                Value::String(anthropic_stop_reason(reason))
            }),
    );
    if let Some(usage) = &response.usage {
        out.insert("usage".into(), encode_anthropic_usage(usage));
    }
    Ok(CodecOutput {
        value: Value::Object(out),
        notices,
    })
}

fn validate_output_blocks(
    response: &CanonicalResponse,
    target: WireSurface,
) -> Result<(), CodecError> {
    for block in &response.output {
        if matches!(
            block.kind,
            CanonicalBlockKind::ToolResult
                | CanonicalBlockKind::Image
                | CanonicalBlockKind::Document
                | CanonicalBlockKind::Audio
        ) {
            return Err(codec_error(
                CodecReasonCode::UnsupportedSemanticFeature,
                Some("output.content"),
                None,
                Some(target),
            ));
        }
    }
    Ok(())
}

fn decode_provider_error(
    payload: &Value,
    status: u16,
    surface: WireSurface,
) -> Result<Option<ProviderErrorEvidence>, CodecError> {
    let Some(object) = payload.as_object() else {
        if status >= 400 {
            return Err(response_error("error"));
        }
        return Ok(None);
    };
    let Some(error_value) = object.get("error") else {
        if status >= 400 {
            return Err(codec_error(
                CodecReasonCode::MalformedProviderResponse,
                Some("error"),
                Some(surface),
                None,
            ));
        }
        return Ok(None);
    };
    let error = error_value.as_object().ok_or_else(|| {
        codec_error(
            CodecReasonCode::MalformedProviderResponse,
            Some("error"),
            Some(surface),
            None,
        )
    })?;
    let error_type = optional_string(error, "type")?;
    let message = optional_string(error, "message")?;
    if error_type.is_none() && message.is_none() {
        return Err(codec_error(
            CodecReasonCode::MalformedProviderResponse,
            Some("error"),
            Some(surface),
            None,
        ));
    }
    Ok(Some(ProviderErrorEvidence {
        status,
        error_type,
        message: message.map(|value| truncate_message(&value)),
    }))
}

fn response_object(
    payload: &Value,
    surface: WireSurface,
) -> Result<&Map<String, Value>, CodecError> {
    payload.as_object().ok_or_else(|| {
        codec_error(
            CodecReasonCode::MalformedProviderResponse,
            Some("response"),
            Some(surface),
            None,
        )
    })
}

#[derive(Clone, Copy)]
enum UsageProtocol {
    Openai,
    Anthropic,
}

fn decode_usage(
    value: Option<&Value>,
    protocol: UsageProtocol,
) -> Result<Option<CanonicalUsage>, CodecError> {
    let Some(value) = value else {
        return Ok(None);
    };
    if value.is_null() {
        return Ok(None);
    }
    let Some(object) = value.as_object() else {
        return Ok(Some(CanonicalUsage::default()));
    };
    let mut usage = CanonicalUsage {
        cache_counter_status: CacheCounterStatus::NotReported,
        ..CanonicalUsage::default()
    };
    match protocol {
        UsageProtocol::Openai => {
            usage.input_tokens = token(object.get("prompt_tokens"));
            usage.output_tokens = token(object.get("completion_tokens"));
            usage.total_tokens = token(object.get("total_tokens"));
            if let Some(details) = object
                .get("prompt_tokens_details")
                .and_then(Value::as_object)
            {
                usage.cache_read_input_tokens = token(details.get("cached_tokens"));
                usage.cached_input_tokens = usage.cache_read_input_tokens;
                usage.cache_write_input_tokens = token(details.get("cache_write_tokens"));
            }
            if usage.cache_read_input_tokens.is_some()
                || usage.cache_write_input_tokens.is_some()
                || object.contains_key("cached_tokens")
                || object.contains_key("cache_read_input_tokens")
            {
                usage.cache_counter_status = CacheCounterStatus::Reported;
                if usage.cache_read_input_tokens.is_none() {
                    usage.cache_read_input_tokens = token(object.get("cache_read_input_tokens"));
                    usage.cached_input_tokens = usage.cache_read_input_tokens;
                }
            }
            if let Some(details) = object
                .get("completion_tokens_details")
                .and_then(Value::as_object)
            {
                usage.reasoning_tokens = token(details.get("reasoning_tokens"));
            }
            if usage.total_tokens.is_none()
                || usage.total_tokens == Some(0)
                    && (usage.input_tokens.unwrap_or(0) != 0
                        || usage.output_tokens.unwrap_or(0) != 0)
            {
                usage.total_tokens = sum_tokens(usage.input_tokens, usage.output_tokens);
                if usage.total_tokens.is_none() && usage.cached_input_tokens.is_some() {
                    usage.total_tokens = usage.cached_input_tokens;
                }
            }
        }
        UsageProtocol::Anthropic => {
            usage.input_tokens = token(object.get("input_tokens"));
            usage.output_tokens = token(object.get("output_tokens"));
            usage.cache_read_input_tokens = token(object.get("cache_read_input_tokens"));
            usage.cache_creation_input_tokens = token(object.get("cache_creation_input_tokens"));
            if usage.cache_read_input_tokens.is_some()
                || usage.cache_creation_input_tokens.is_some()
            {
                usage.cache_counter_status = CacheCounterStatus::Reported;
                usage.cached_input_tokens = sum_tokens(
                    usage.cache_read_input_tokens,
                    usage.cache_creation_input_tokens,
                );
                usage.cache_write_input_tokens = usage.cache_creation_input_tokens;
            }
            usage.total_tokens = sum_tokens(
                sum_tokens(usage.input_tokens, usage.output_tokens),
                usage.cached_input_tokens,
            );
        }
    }
    Ok(Some(usage))
}

fn token(value: Option<&Value>) -> Option<u64> {
    value.and_then(Value::as_u64)
}

fn sum_tokens(left: Option<u64>, right: Option<u64>) -> Option<u64> {
    match (left, right) {
        (Some(left), Some(right)) => left.checked_add(right),
        (Some(value), None) | (None, Some(value)) => Some(value),
        (None, None) => None,
    }
}

fn encode_openai_usage(usage: &CanonicalUsage) -> Value {
    let mut out = Map::new();
    insert_token(&mut out, "prompt_tokens", usage.input_tokens);
    insert_token(&mut out, "completion_tokens", usage.output_tokens);
    insert_token(&mut out, "total_tokens", usage.total_tokens);
    let mut prompt_details = Map::new();
    insert_token(
        &mut prompt_details,
        "cached_tokens",
        usage.cache_read_input_tokens,
    );
    insert_token(
        &mut prompt_details,
        "cache_write_tokens",
        usage.cache_write_input_tokens,
    );
    if !prompt_details.is_empty() {
        out.insert(
            "prompt_tokens_details".into(),
            Value::Object(prompt_details),
        );
    }
    let mut completion_details = Map::new();
    insert_token(
        &mut completion_details,
        "reasoning_tokens",
        usage.reasoning_tokens,
    );
    if !completion_details.is_empty() {
        out.insert(
            "completion_tokens_details".into(),
            Value::Object(completion_details),
        );
    }
    Value::Object(out)
}

fn encode_anthropic_usage(usage: &CanonicalUsage) -> Value {
    let mut out = Map::new();
    insert_token(&mut out, "input_tokens", usage.input_tokens);
    insert_token(&mut out, "output_tokens", usage.output_tokens);
    insert_token(
        &mut out,
        "cache_read_input_tokens",
        usage.cache_read_input_tokens,
    );
    insert_token(
        &mut out,
        "cache_creation_input_tokens",
        usage.cache_creation_input_tokens,
    );
    Value::Object(out)
}

fn insert_token(out: &mut Map<String, Value>, key: &str, value: Option<u64>) {
    if let Some(value) = value {
        out.insert(key.into(), value.into());
    }
}

fn tool_input_value(block: &CanonicalContentBlock) -> Result<Value, CodecError> {
    if let Some(input) = &block.tool_input {
        return Ok(Value::Object(input.clone()));
    }
    let arguments = block.arguments.as_deref().unwrap_or("{}");
    let value = serde_json::from_str::<Value>(arguments).map_err(|_| {
        codec_error(
            CodecReasonCode::MalformedSourceRequest,
            Some("tool_call.arguments"),
            None,
            Some(WireSurface::AnthropicMessages),
        )
    })?;
    if value.is_object() {
        Ok(value)
    } else {
        Err(codec_error(
            CodecReasonCode::MalformedSourceRequest,
            Some("tool_call.arguments"),
            None,
            Some(WireSurface::AnthropicMessages),
        ))
    }
}

fn compact_json(value: &Map<String, Value>) -> String {
    serde_json::to_string(value).unwrap_or_else(|_| "{}".into())
}

fn optional_string(object: &Map<String, Value>, key: &str) -> Result<Option<String>, CodecError> {
    match object.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(value)) => Ok(Some(value.clone())),
        Some(_) => Err(response_error(key)),
    }
}

fn required_string(
    object: &Map<String, Value>,
    key: &str,
    field: &'static str,
) -> Result<String, CodecError> {
    object
        .get(key)
        .and_then(Value::as_str)
        .map(ToOwned::to_owned)
        .ok_or_else(|| response_error(field))
}

fn response_error(field: impl Into<String>) -> CodecError {
    CodecError {
        reason: CodecReasonCode::MalformedProviderResponse,
        field: Some(field.into()),
        source_surface: None,
        target_surface: None,
    }
}

fn codec_error(
    reason: CodecReasonCode,
    field: Option<&str>,
    source_surface: Option<WireSurface>,
    target_surface: Option<WireSurface>,
) -> CodecError {
    CodecError {
        reason,
        field: field.map(ToOwned::to_owned),
        source_surface,
        target_surface,
    }
}

fn chat_finish_reason(reason: &str) -> String {
    match reason {
        "end_turn" | "stop_sequence" => "stop".into(),
        "max_tokens" | "model_context_window_exceeded" => "length".into(),
        "tool_use" | "pause_turn" => "tool_calls".into(),
        "refusal" => "content_filter".into(),
        other => other.to_owned(),
    }
}

fn anthropic_stop_reason(reason: &str) -> String {
    match reason {
        "stop" => "end_turn".into(),
        "length" => "max_tokens".into(),
        "tool_calls" => "tool_use".into(),
        "content_filter" => "refusal".into(),
        other => other.to_owned(),
    }
}

fn truncate_message(message: &str) -> String {
    if message.len() <= MAX_PROVIDER_ERROR_MESSAGE_BYTES {
        return message.to_owned();
    }
    let mut end = MAX_PROVIDER_ERROR_MESSAGE_BYTES;
    while !message.is_char_boundary(end) {
        end -= 1;
    }
    message[..end].to_owned()
}
