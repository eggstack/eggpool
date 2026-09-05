//! Finite OpenAI Responses and Gemini wire codecs.
//!
//! These codecs use the W002 canonical request/response types as their only
//! semantic bridge. They do not own SSE framing, transport, retries,
//! negotiation, or request lifecycle state.

use std::collections::BTreeMap;

use serde_json::{Map, Value, json};

use super::adaptation::{client_wire_surface, notice, request_notices, stable_tool_call_id};
use super::codec::{
    CodecError, CodecOutput, CodecReasonCode, DecodedProviderPayload, StreamAdapterKind, WireCodec,
    WireCodecId,
};
use super::codecs::{encode_anthropic_response, encode_openai_response};
use super::ir::{
    CacheCounterStatus, CanonicalBlockKind, CanonicalContentBlock, CanonicalMessage,
    CanonicalOutputBlock, CanonicalRequest, CanonicalResponse, CanonicalRole, CanonicalToolChoice,
    CanonicalUsage, ClientSurface, MediaSource, ProviderErrorEvidence, ReasoningMode,
    ToolChoiceMode,
};
use super::registry::{CodecFamily, ConfiguredWireProfile, WireSurface};
use crate::request::{AdmissionError, canonical_request_from_value};

const MAX_PROVIDER_ERROR_MESSAGE_BYTES: usize = 4 * 1024;

#[derive(Debug, Clone, Copy, Default)]
pub struct OpenAiResponsesCodec;

#[derive(Debug, Clone, Copy, Default)]
pub struct GeminiInteractionsCodec;

#[derive(Debug, Clone, Copy, Default)]
pub struct GeminiGenerateContentCodec;

impl WireCodec for OpenAiResponsesCodec {
    fn codec_id(&self) -> WireCodecId {
        WireCodecId::OpenaiResponses
    }
    fn surface(&self) -> WireSurface {
        WireSurface::OpenaiResponses
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
        ensure_profile(profile, WireSurface::OpenaiResponses)?;
        encode_responses_request(request)
    }
    fn decode_response(
        &self,
        payload: &Value,
        status: u16,
    ) -> Result<CodecOutput<DecodedProviderPayload>, CodecError> {
        decode_responses_response(payload, status)
    }
    fn encode_response(
        &self,
        response: &CanonicalResponse,
        client_surface: ClientSurface,
    ) -> Result<CodecOutput<Value>, CodecError> {
        encode_for_client(response, client_surface)
    }
    fn stream_adapter(&self) -> StreamAdapterKind {
        StreamAdapterKind::OpenaiResponsesSse
    }
}

impl WireCodec for GeminiInteractionsCodec {
    fn codec_id(&self) -> WireCodecId {
        WireCodecId::GeminiInteractions
    }
    fn surface(&self) -> WireSurface {
        WireSurface::GeminiInteractions
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
        ensure_profile(profile, WireSurface::GeminiInteractions)?;
        encode_interactions_request(request)
    }
    fn decode_response(
        &self,
        payload: &Value,
        status: u16,
    ) -> Result<CodecOutput<DecodedProviderPayload>, CodecError> {
        decode_interactions_response(payload, status)
    }
    fn encode_response(
        &self,
        response: &CanonicalResponse,
        client_surface: ClientSurface,
    ) -> Result<CodecOutput<Value>, CodecError> {
        encode_for_client(response, client_surface)
    }
    fn stream_adapter(&self) -> StreamAdapterKind {
        StreamAdapterKind::GeminiInteractionsSse
    }
}

impl WireCodec for GeminiGenerateContentCodec {
    fn codec_id(&self) -> WireCodecId {
        WireCodecId::GeminiGenerateContent
    }
    fn surface(&self) -> WireSurface {
        WireSurface::GeminiGenerateContent
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
        ensure_profile(profile, WireSurface::GeminiGenerateContent)?;
        encode_generate_content_request(request)
    }
    fn decode_response(
        &self,
        payload: &Value,
        status: u16,
    ) -> Result<CodecOutput<DecodedProviderPayload>, CodecError> {
        decode_generate_content_response(payload, status)
    }
    fn encode_response(
        &self,
        response: &CanonicalResponse,
        client_surface: ClientSurface,
    ) -> Result<CodecOutput<Value>, CodecError> {
        encode_for_client(response, client_surface)
    }
    fn stream_adapter(&self) -> StreamAdapterKind {
        StreamAdapterKind::GeminiGenerateContentSse
    }
}

impl GeminiInteractionsCodec {
    /// Encode a canonical response in the native Interactions grammar.
    pub fn encode_native_response(
        &self,
        response: &CanonicalResponse,
    ) -> Result<CodecOutput<Value>, CodecError> {
        encode_interactions_response(response)
    }
}

impl GeminiGenerateContentCodec {
    /// Encode a canonical response in the native generateContent grammar.
    pub fn encode_native_response(
        &self,
        response: &CanonicalResponse,
    ) -> Result<CodecOutput<Value>, CodecError> {
        encode_generate_content_response(response)
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
        AdmissionError::UnsupportedContent { .. } => (
            CodecReasonCode::UnsupportedSemanticFeature,
            Some("content".to_owned()),
        ),
        AdmissionError::InvalidField { field } => (
            CodecReasonCode::MalformedSourceRequest,
            Some(field.to_owned()),
        ),
        AdmissionError::InvalidJson
        | AdmissionError::TopLevelNotObject
        | AdmissionError::InvalidModel => (CodecReasonCode::MalformedSourceRequest, None),
    };
    CodecError {
        reason,
        field,
        source_surface: Some(client_wire_surface(source)),
        target_surface: None,
    }
}

fn ensure_profile(
    profile: &ConfiguredWireProfile,
    expected: WireSurface,
) -> Result<(), CodecError> {
    let family = expected_family(expected);
    if profile.definition.surface != expected
        || profile.definition.request_codec.family() != family
        || profile.definition.response_codec.family() != family
    {
        return Err(error(
            CodecReasonCode::UnsupportedWireProfile,
            Some("profile"),
            None,
            Some(profile.definition.surface),
        ));
    }
    Ok(())
}

fn expected_family(surface: WireSurface) -> CodecFamily {
    match surface {
        WireSurface::OpenaiChatCompletions => CodecFamily::OpenaiChat,
        WireSurface::OpenaiResponses => CodecFamily::OpenaiResponses,
        WireSurface::AnthropicMessages => CodecFamily::AnthropicMessages,
        WireSurface::GeminiInteractions => CodecFamily::GeminiInteractions,
        WireSurface::GeminiGenerateContent => CodecFamily::GeminiGenerateContent,
    }
}

fn encode_responses_request(request: &CanonicalRequest) -> Result<CodecOutput<Value>, CodecError> {
    validate_request_blocks(request, WireSurface::OpenaiResponses)?;
    let mut notices = request_notices(request, WireSurface::OpenaiResponses)?;
    let mut out = Map::new();
    out.insert("model".into(), Value::String(request.model.clone()));
    out.insert("input".into(), Value::Array(Vec::new()));
    out.insert("stream".into(), Value::Bool(request.stream));
    out.insert("store".into(), Value::Bool(false));
    let input = out
        .get_mut("input")
        .and_then(Value::as_array_mut)
        .expect("input is an array");
    for message in &request.messages {
        if message.role == CanonicalRole::System {
            continue;
        }
        let mut message_blocks = Vec::new();
        let flush_message = |input: &mut Vec<Value>, blocks: &mut Vec<&CanonicalContentBlock>| {
            if !blocks.is_empty() {
                input.push(json!({
                    "type": "message",
                    "role": message.role.as_str(),
                    "content": responses_content(blocks.as_slice(), message.role),
                }));
                blocks.clear();
            }
        };
        for block in &message.content {
            match block.kind {
                CanonicalBlockKind::ToolCall => {
                    flush_message(input, &mut message_blocks);
                    input.push(json!({
                        "type":"function_call",
                        "call_id":block.call_id.clone().unwrap_or_default(),
                        "name":block.name.clone().unwrap_or_default(),
                        "arguments":block.arguments.clone().unwrap_or_else(|| block.tool_input.as_ref().map(compact_json).unwrap_or_default())
                    }));
                }
                CanonicalBlockKind::ToolResult => {
                    flush_message(input, &mut message_blocks);
                    input.push(json!({
                        "type":"function_call_output",
                        "call_id":block.call_id.clone().or_else(|| message.tool_call_id.clone()).unwrap_or_default(),
                        "output":block.text.clone().unwrap_or_default()
                    }));
                }
                _ => message_blocks.push(block),
            }
        }
        flush_message(input, &mut message_blocks);
    }
    if let Some(system) = request
        .messages
        .iter()
        .find(|message| message.role == CanonicalRole::System)
    {
        out.insert("instructions".into(), Value::String(system.text()));
    }
    add_openai_controls(&mut out, request);
    if !request.tools.is_empty() {
        out.insert("tools".into(), Value::Array(response_tools(request)));
    }
    if let Some(choice) = &request.tool_choice {
        out.insert("tool_choice".into(), openai_response_tool_choice(choice));
    }
    if let Some(format) = &request.response_format {
        out.insert(
            "text".into(),
            json!({"format":Value::Object(format.clone())}),
        );
    }
    if !request.metadata.is_empty() {
        if request.client_surface == ClientSurface::Responses {
            return Err(error(
                CodecReasonCode::UnsupportedSemanticFeature,
                Some("metadata"),
                Some(client_wire_surface(request.client_surface)),
                Some(WireSurface::OpenaiResponses),
            ));
        }
        notices.push(notice(
            "metadata_not_representable",
            "metadata",
            Some(client_wire_surface(request.client_surface)),
            Some(WireSurface::OpenaiResponses),
        ));
    }
    if let Some(reasoning) = reasoning_for_responses(request) {
        out.insert("reasoning".into(), reasoning);
    }
    Ok(CodecOutput {
        value: Value::Object(out),
        notices,
    })
}

fn responses_content(blocks: &[&CanonicalContentBlock], role: CanonicalRole) -> Vec<Value> {
    let content_type = if role == CanonicalRole::Assistant {
        "output_text"
    } else {
        "input_text"
    };
    blocks
        .iter()
        .filter_map(|block| match block.kind {
            CanonicalBlockKind::Text => {
                let mut value = json!({"type":content_type, "text":block.text.clone().unwrap_or_default()});
                if valid_prompt_breakpoint(block.prompt_cache_breakpoint.as_ref()) {
                    let marker = block.prompt_cache_breakpoint.as_ref().expect("validated marker");
                    value["prompt_cache_breakpoint"] = marker.clone();
                }
                Some(value)
            }
            CanonicalBlockKind::Image => block
                .media
                .as_ref()
                .and_then(|media| {
                    media
                        .uri
                        .clone()
                        .or_else(|| media.data.as_ref().map(|data| format!("data:{};base64,{}", media.media_type.as_deref().unwrap_or("application/octet-stream"), data)))
                        .map(|url| {
                            let mut value = json!({"type":"input_image", "image_url":url});
                            if let Some(detail) = &media.detail {
                                value["detail"] = Value::String(detail.clone());
                            }
                            if valid_prompt_breakpoint(block.prompt_cache_breakpoint.as_ref()) {
                                let marker = block.prompt_cache_breakpoint.as_ref().expect("validated marker");
                                value["prompt_cache_breakpoint"] = marker.clone();
                            }
                            value
                        })
                }),
            CanonicalBlockKind::Document => block.media.as_ref().map(|media| {
                let mut value = json!({
                    "type":"input_file",
                    "file": {
                        "filename":"document.pdf",
                        "file_data":media.uri.clone().or_else(|| media.file_id.clone()).or_else(|| media.data.as_ref().map(|data| format!("data:{};base64,{}", media.media_type.as_deref().unwrap_or("application/pdf"), data))).unwrap_or_default()
                    }
                });
                if valid_prompt_breakpoint(block.prompt_cache_breakpoint.as_ref()) {
                    let marker = block.prompt_cache_breakpoint.as_ref().expect("validated marker");
                    value["prompt_cache_breakpoint"] = marker.clone();
                }
                value
            }),
            CanonicalBlockKind::Refusal => {
                Some(json!({"type":"refusal", "refusal":block.text.clone().unwrap_or_default()}))
            }
            _ => None,
        })
        .collect()
}

fn response_tools(request: &CanonicalRequest) -> Vec<Value> {
    request.tools.iter().map(|tool| json!({"type":"function", "name":tool.name, "description":tool.description.clone().unwrap_or_default(), "parameters":Value::Object(tool.parameters.clone()), "strict":false})).collect()
}

fn openai_response_tool_choice(choice: &CanonicalToolChoice) -> Value {
    if choice.mode == ToolChoiceMode::Function {
        json!({"type":"function", "name":choice.function_name.clone().unwrap_or_default()})
    } else {
        Value::String(
            match choice.mode {
                ToolChoiceMode::Auto => "auto",
                ToolChoiceMode::Required => "required",
                ToolChoiceMode::None => "none",
                ToolChoiceMode::Function => unreachable!(),
            }
            .into(),
        )
    }
}

fn reasoning_for_responses(request: &CanonicalRequest) -> Option<Value> {
    if request.reasoning.requested == Some(false) {
        return Some(json!({"effort":"none"}));
    }
    if request.reasoning.requested != Some(true) {
        return None;
    }
    if request.reasoning.mode == ReasoningMode::Effort {
        if let Some(effort) = &request.reasoning.effort {
            return Some(json!({"effort":effort}));
        }
    }
    None
}

fn encode_interactions_request(
    request: &CanonicalRequest,
) -> Result<CodecOutput<Value>, CodecError> {
    validate_request_blocks(request, WireSurface::GeminiInteractions)?;
    let notices = request_notices(request, WireSurface::GeminiInteractions)?;
    let mut out = Map::new();
    out.insert("model".into(), Value::String(request.model.clone()));
    out.insert("input".into(), gemini_input(request)?);
    out.insert("stream".into(), Value::Bool(request.stream));
    out.insert("store".into(), Value::Bool(false));
    if let Some(system) = request
        .messages
        .iter()
        .find(|message| message.role == CanonicalRole::System)
    {
        out.insert("system_instruction".into(), Value::String(system.text()));
    }
    if !request.tools.is_empty() {
        out.insert("tools".into(), Value::Array(request.tools.iter().map(|tool| json!({"type":"function", "name":tool.name, "description":tool.description.clone().unwrap_or_default(), "parameters":Value::Object(tool.parameters.clone())})).collect()));
    }
    let mut generation = Map::new();
    add_generation_controls(&mut generation, request, false);
    add_gemini_reasoning(&mut generation, request);
    if !generation.is_empty() {
        out.insert("generation_config".into(), Value::Object(generation));
    }
    if let Some(format) = &request.response_format {
        out.insert("response_format".into(), Value::Object(format.clone()));
    }
    Ok(CodecOutput {
        value: Value::Object(out),
        notices,
    })
}

fn encode_generate_content_request(
    request: &CanonicalRequest,
) -> Result<CodecOutput<Value>, CodecError> {
    validate_request_blocks(request, WireSurface::GeminiGenerateContent)?;
    let notices = request_notices(request, WireSurface::GeminiGenerateContent)?;
    let contents: Result<Vec<Value>, CodecError> = request.messages.iter().filter(|message| message.role != CanonicalRole::System).map(|message| Ok(json!({"role":if message.role == CanonicalRole::Assistant {"model"} else {"user"}, "parts":gemini_parts(&message.content, false)?}))).collect();
    let mut out = Map::new();
    out.insert("contents".into(), Value::Array(contents?));
    if let Some(system) = request
        .messages
        .iter()
        .find(|message| message.role == CanonicalRole::System)
    {
        out.insert(
            "systemInstruction".into(),
            json!({"parts":[{"text":system.text()}]}),
        );
    }
    let mut generation = Map::new();
    add_generation_controls(&mut generation, request, true);
    if let Some(format) = &request.response_format {
        if matches!(
            format.get("type").and_then(Value::as_str),
            Some("json_object") | Some("json_schema")
        ) {
            generation.insert(
                "responseMimeType".into(),
                Value::String("application/json".into()),
            );
            if let Some(schema) = format
                .get("json_schema")
                .and_then(Value::as_object)
                .and_then(|schema| schema.get("schema"))
                .and_then(Value::as_object)
            {
                generation.insert("responseSchema".into(), Value::Object(schema.clone()));
            }
        }
    }
    if request.reasoning.requested == Some(false) {
        generation.insert("thinkingConfig".into(), json!({"thinkingBudget":0}));
    } else if request.reasoning.mode == ReasoningMode::FixedBudget {
        if let Some(budget) = request.reasoning.budget_tokens {
            generation.insert("thinkingConfig".into(), json!({"thinkingBudget":budget}));
        }
    }
    if !generation.is_empty() {
        out.insert("generationConfig".into(), Value::Object(generation));
    }
    if !request.tools.is_empty() {
        out.insert("tools".into(), json!([{"function_declarations":request.tools.iter().map(|tool| json!({"name":tool.name, "description":tool.description.clone().unwrap_or_default(), "parameters":Value::Object(tool.parameters.clone())})).collect::<Vec<_>>()}]));
    }
    if let Some(choice) = &request.tool_choice {
        let mut config = Map::new();
        config.insert(
            "mode".into(),
            Value::String(
                match choice.mode {
                    ToolChoiceMode::Auto => "AUTO",
                    ToolChoiceMode::None => "NONE",
                    ToolChoiceMode::Required | ToolChoiceMode::Function => "ANY",
                }
                .into(),
            ),
        );
        if choice.mode == ToolChoiceMode::Function {
            if let Some(name) = &choice.function_name {
                config.insert("allowedFunctionNames".into(), json!([name]));
            }
        }
        out.insert("toolConfig".into(), json!({"functionCallingConfig":config}));
    }
    Ok(CodecOutput {
        value: Value::Object(out),
        notices,
    })
}

fn add_openai_controls(out: &mut Map<String, Value>, request: &CanonicalRequest) {
    insert_presence_value(
        out,
        "max_output_tokens",
        request.max_output_tokens,
        &request.presence.max_output_tokens,
    );
    insert_presence_value(
        out,
        "temperature",
        request.temperature,
        &request.presence.temperature,
    );
    insert_presence_value(out, "top_p", request.top_p, &request.presence.top_p);
    if let Some(stop) = &request.stop {
        out.insert("stop".into(), encode_stop(stop));
    } else if matches!(request.presence.stop, super::ir::Presence::Null) {
        out.insert("stop".into(), Value::Null);
    }
}

fn add_generation_controls(out: &mut Map<String, Value>, request: &CanonicalRequest, camel: bool) {
    insert_presence_value(
        out,
        if camel {
            "maxOutputTokens"
        } else {
            "max_output_tokens"
        },
        request.max_output_tokens,
        &request.presence.max_output_tokens,
    );
    insert_presence_value(
        out,
        "temperature",
        request.temperature,
        &request.presence.temperature,
    );
    insert_presence_value(
        out,
        if camel { "topP" } else { "top_p" },
        request.top_p,
        &request.presence.top_p,
    );
    if let Some(stop) = &request.stop {
        out.insert(
            if camel {
                "stopSequences"
            } else {
                "stop_sequences"
            }
            .into(),
            Value::Array(stop.iter().cloned().map(Value::String).collect()),
        );
    } else if matches!(request.presence.stop, super::ir::Presence::Null) {
        out.insert(
            if camel {
                "stopSequences"
            } else {
                "stop_sequences"
            }
            .into(),
            Value::Null,
        );
    }
}

fn insert_presence_value<T: Into<Value> + Copy>(
    out: &mut Map<String, Value>,
    key: &str,
    value: Option<T>,
    presence: &super::ir::Presence<T>,
) {
    if let Some(value) = value.or_else(|| presence.value().copied()) {
        out.insert(key.into(), value.into());
    } else if matches!(presence, super::ir::Presence::Null) {
        out.insert(key.into(), Value::Null);
    }
}

fn add_gemini_reasoning(out: &mut Map<String, Value>, request: &CanonicalRequest) {
    if request.reasoning.requested == Some(false) {
        out.insert("thinking_summaries".into(), Value::String("none".into()));
    } else if request.reasoning.requested == Some(true) && request.reasoning.effort.is_some() {
        out.insert(
            "thinking_level".into(),
            Value::String(request.reasoning.effort.clone().unwrap_or_default()),
        );
        out.insert("thinking_summaries".into(), Value::String("auto".into()));
    }
}

fn encode_stop(stop: &[String]) -> Value {
    if stop.len() == 1 {
        Value::String(stop[0].clone())
    } else {
        Value::Array(stop.iter().cloned().map(Value::String).collect())
    }
}

fn gemini_input(request: &CanonicalRequest) -> Result<Value, CodecError> {
    let messages: Vec<&CanonicalMessage> = request
        .messages
        .iter()
        .filter(|message| message.role != CanonicalRole::System)
        .collect();
    if messages.len() == 1
        && messages[0].content.len() == 1
        && messages[0].content[0].kind == CanonicalBlockKind::Text
    {
        return Ok(Value::String(
            messages[0].content[0].text.clone().unwrap_or_default(),
        ));
    }
    Ok(Value::Array(messages.into_iter().map(|message| Ok(json!({"role":if message.role == CanonicalRole::Assistant {"model"} else {"user"}, "parts":gemini_parts(&message.content, true)?}))).collect::<Result<Vec<_>, CodecError>>()?))
}

fn gemini_parts(
    blocks: &[CanonicalContentBlock],
    preserve_call_ids: bool,
) -> Result<Vec<Value>, CodecError> {
    blocks.iter().map(|block| match block.kind {
        CanonicalBlockKind::Text => Ok(json!({"text":block.text.clone().unwrap_or_default()})),
        CanonicalBlockKind::Reasoning => Ok(json!({"text":block.text.clone().unwrap_or_default(), "thought":true})),
        CanonicalBlockKind::Image | CanonicalBlockKind::Document => {
            let media = block.media.as_ref().ok_or_else(|| error(CodecReasonCode::UnsupportedSemanticFeature, Some("content.image"), None, Some(WireSurface::GeminiGenerateContent)))?;
            let default_type = if block.kind == CanonicalBlockKind::Document { "application/pdf" } else { "application/octet-stream" };
            if let Some(data) = &media.data { Ok(json!({"inline_data":{"mime_type":media.media_type.clone().unwrap_or_else(||default_type.into()), "data":data}})) }
            else if let Some(uri) = &media.uri { Ok(json!({"file_data":{"mime_type":media.media_type.clone().unwrap_or_else(||default_type.into()), "file_uri":uri}})) }
            else if let Some(file_id) = &media.file_id { Ok(json!({"file_data":{"mime_type":media.media_type.clone().unwrap_or_else(||default_type.into()), "file_uri":file_id}})) }
            else { Err(error(CodecReasonCode::UnsupportedSemanticFeature, Some("content.image"), None, Some(WireSurface::GeminiGenerateContent))) }
        }
        CanonicalBlockKind::ToolCall => {
            let mut call = Map::new();
            call.insert(
                "name".into(),
                Value::String(block.name.clone().unwrap_or_default()),
            );
            call.insert("args".into(), tool_args(block)?);
            if preserve_call_ids {
                if let Some(call_id) = &block.call_id {
                    call.insert("id".into(), Value::String(call_id.clone()));
                }
            }
            Ok(json!({"functionCall":call}))
        }
        CanonicalBlockKind::ToolResult => {
            let mut response = Map::new();
            response.insert(
                "result".into(),
                Value::String(block.text.clone().unwrap_or_default()),
            );
            if preserve_call_ids {
                if let Some(call_id) = &block.call_id {
                    response.insert("id".into(), Value::String(call_id.clone()));
                }
            }
            Ok(json!({"functionResponse":{"name":block.name.clone().unwrap_or_default(), "response":response}}))
        }
        CanonicalBlockKind::Refusal => Ok(json!({"text":block.text.clone().unwrap_or_default()})),
        CanonicalBlockKind::Audio => Err(error(CodecReasonCode::UnsupportedSemanticFeature, Some("content"), None, Some(WireSurface::GeminiGenerateContent))),
    }).collect()
}

fn tool_args(block: &CanonicalContentBlock) -> Result<Value, CodecError> {
    if let Some(input) = &block.tool_input {
        return Ok(Value::Object(input.clone()));
    }
    let value: Value =
        serde_json::from_str(block.arguments.as_deref().unwrap_or("{}")).map_err(|_| {
            error(
                CodecReasonCode::MalformedSourceRequest,
                Some("tool_call.arguments"),
                None,
                Some(WireSurface::GeminiGenerateContent),
            )
        })?;
    if value.is_object() {
        Ok(value)
    } else {
        Err(error(
            CodecReasonCode::MalformedSourceRequest,
            Some("tool_call.arguments"),
            None,
            Some(WireSurface::GeminiGenerateContent),
        ))
    }
}

fn validate_request_blocks(
    request: &CanonicalRequest,
    target: WireSurface,
) -> Result<(), CodecError> {
    for message in &request.messages {
        for block in &message.content {
            if block.kind == CanonicalBlockKind::Audio
                || (block.kind == CanonicalBlockKind::Document
                    && target == WireSurface::GeminiInteractions)
            {
                return Err(error(
                    CodecReasonCode::UnsupportedSemanticFeature,
                    Some("content"),
                    Some(client_wire_surface(request.client_surface)),
                    Some(target),
                ));
            }
            if block.kind == CanonicalBlockKind::ToolCall
                && (block.call_id.is_none() || block.name.is_none())
            {
                return Err(error(
                    CodecReasonCode::MalformedSourceRequest,
                    Some("tool_call"),
                    Some(client_wire_surface(request.client_surface)),
                    Some(target),
                ));
            }
        }
    }
    Ok(())
}

fn decode_responses_response(
    payload: &Value,
    status: u16,
) -> Result<CodecOutput<DecodedProviderPayload>, CodecError> {
    if let Some(provider_error) = provider_error(payload, status, WireSurface::OpenaiResponses)? {
        return Ok(CodecOutput::new(DecodedProviderPayload::Error(
            provider_error,
        )));
    }
    let object = object(payload, WireSurface::OpenaiResponses)?;
    let output_values = object
        .get("output")
        .and_then(Value::as_array)
        .ok_or_else(|| response_error("output"))?;
    let mut output = Vec::new();
    for item in output_values {
        let item = item.as_object().ok_or_else(|| response_error("output[]"))?;
        match item.get("type").and_then(Value::as_str) {
            Some("function_call") => output.push(CanonicalOutputBlock {
                kind: CanonicalBlockKind::ToolCall,
                text: None,
                media: None,
                call_id: Some(required_string(item, "call_id", "output[].call_id")?),
                name: Some(required_string(item, "name", "output[].name")?),
                arguments: Some(required_string(item, "arguments", "output[].arguments")?),
            }),
            Some("reasoning") => {
                let summary = item
                    .get("summary")
                    .and_then(Value::as_array)
                    .ok_or_else(|| response_error("output[].summary"))?;
                for block in summary {
                    let block = block
                        .as_object()
                        .ok_or_else(|| response_error("output[].summary[]"))?;
                    if block.get("type").and_then(Value::as_str) != Some("summary_text") {
                        return Err(unsupported(
                            "output[].summary[].type",
                            WireSurface::OpenaiResponses,
                        ));
                    }
                    output.push(text_output(
                        CanonicalBlockKind::Reasoning,
                        required_string(block, "text", "output[].summary[].text")?,
                    ));
                }
            }
            Some("message") => {
                let content = item
                    .get("content")
                    .and_then(Value::as_array)
                    .ok_or_else(|| response_error("output[].content"))?;
                for block in content {
                    let block = block
                        .as_object()
                        .ok_or_else(|| response_error("output[].content[]"))?;
                    match block.get("type").and_then(Value::as_str) {
                        Some("output_text") => output.push(text_output(
                            CanonicalBlockKind::Text,
                            required_string(block, "text", "output[].content[].text")?,
                        )),
                        Some("refusal") => output.push(text_output(
                            CanonicalBlockKind::Refusal,
                            required_string(block, "refusal", "output[].content[].refusal")?,
                        )),
                        Some("output_image") | Some("output_file") => {
                            let is_file =
                                block.get("type").and_then(Value::as_str) == Some("output_file");
                            output.push(CanonicalOutputBlock {
                                kind: if is_file {
                                    CanonicalBlockKind::Document
                                } else {
                                    CanonicalBlockKind::Image
                                },
                                text: None,
                                media: Some(super::codecs::decode_response_media(
                                    block,
                                    if is_file { "file" } else { "image" },
                                )?),
                                call_id: None,
                                name: None,
                                arguments: None,
                            });
                        }
                        Some(_) => {
                            return Err(unsupported(
                                "output[].content[].type",
                                WireSurface::OpenaiResponses,
                            ));
                        }
                        None => return Err(response_error("output[].content[].type")),
                    }
                }
            }
            Some(_) => return Err(unsupported("output[].type", WireSurface::OpenaiResponses)),
            None => return Err(response_error("output[].type")),
        }
    }
    let response = CanonicalResponse {
        response_id: optional_string(object, "id")?,
        model: optional_string(object, "model")?,
        output,
        finish_reason: optional_string(object, "status")?,
        usage: decode_usage(object.get("usage"), UsageProtocol::Openai)?,
        provider_error: None,
    };
    Ok(CodecOutput::new(DecodedProviderPayload::Response(
        Box::new(response),
    )))
}

fn decode_interactions_response(
    payload: &Value,
    status: u16,
) -> Result<CodecOutput<DecodedProviderPayload>, CodecError> {
    if let Some(provider_error) = provider_error(payload, status, WireSurface::GeminiInteractions)?
    {
        return Ok(CodecOutput::new(DecodedProviderPayload::Error(
            provider_error,
        )));
    }
    let root = object(payload, WireSurface::GeminiInteractions)?;
    let interaction = root
        .get("interaction")
        .and_then(Value::as_object)
        .unwrap_or(root);
    let steps = interaction
        .get("steps")
        .and_then(Value::as_array)
        .ok_or_else(|| response_error("steps"))?;
    let mut output = Vec::new();
    let mut tool_ids = BTreeMap::new();
    let mut call_ordinal = 0;
    for raw_step in steps {
        let step = raw_step
            .as_object()
            .ok_or_else(|| response_error("steps[]"))?;
        match step.get("type").and_then(Value::as_str) {
            Some("model_output") | Some("thought") => {
                let content = step
                    .get("content")
                    .and_then(Value::as_array)
                    .ok_or_else(|| response_error("steps[].content"))?;
                for raw in content {
                    let block = raw
                        .as_object()
                        .ok_or_else(|| response_error("steps[].content[]"))?;
                    let kind = if step.get("type").and_then(Value::as_str) == Some("thought") {
                        CanonicalBlockKind::Reasoning
                    } else {
                        CanonicalBlockKind::Text
                    };
                    output.push(text_output(
                        kind,
                        required_string(block, "text", "steps[].content[].text")?,
                    ));
                }
            }
            Some("function_call") => {
                let name = optional_string(step, "name")?.unwrap_or_default();
                let arguments = json_text(step.get("arguments"));
                let call_id = optional_string(step, "id")?
                    .unwrap_or_else(|| stable_tool_call_id(&name, &arguments, call_ordinal));
                call_ordinal += 1;
                tool_ids.insert(name.clone(), call_id.clone());
                output.push(CanonicalOutputBlock {
                    kind: CanonicalBlockKind::ToolCall,
                    text: None,
                    media: None,
                    call_id: Some(call_id),
                    name: Some(name),
                    arguments: Some(arguments),
                });
            }
            Some("function_response") => {
                let name = optional_string(step, "name")?.unwrap_or_default();
                output.push(CanonicalOutputBlock {
                    kind: CanonicalBlockKind::ToolResult,
                    text: Some(json_text(step.get("response"))),
                    media: None,
                    call_id: tool_ids.get(&name).cloned(),
                    name: Some(name),
                    arguments: None,
                });
            }
            Some(_) => return Err(unsupported("steps[].type", WireSurface::GeminiInteractions)),
            None => return Err(response_error("steps[].type")),
        }
    }
    let response = CanonicalResponse {
        response_id: optional_string(interaction, "id")?,
        model: optional_string(interaction, "model")?,
        output,
        finish_reason: optional_string(interaction, "status")?,
        usage: decode_usage(interaction.get("usage"), UsageProtocol::Gemini)?,
        provider_error: None,
    };
    Ok(CodecOutput::new(DecodedProviderPayload::Response(
        Box::new(response),
    )))
}

fn decode_generate_content_response(
    payload: &Value,
    status: u16,
) -> Result<CodecOutput<DecodedProviderPayload>, CodecError> {
    if let Some(provider_error) =
        provider_error(payload, status, WireSurface::GeminiGenerateContent)?
    {
        return Ok(CodecOutput::new(DecodedProviderPayload::Error(
            provider_error,
        )));
    }
    let object = object(payload, WireSurface::GeminiGenerateContent)?;
    let candidates = object
        .get("candidates")
        .and_then(Value::as_array)
        .ok_or_else(|| response_error("candidates"))?;
    let candidate = candidates
        .first()
        .ok_or_else(|| unsupported("candidates", WireSurface::GeminiGenerateContent))?
        .as_object()
        .ok_or_else(|| response_error("candidates[]"))?;
    if object
        .get("promptFeedback")
        .and_then(Value::as_object)
        .and_then(|feedback| feedback.get("blockReason"))
        .is_some_and(|reason| !reason.is_null())
    {
        return Err(unsupported(
            "promptFeedback.blockReason",
            WireSurface::GeminiGenerateContent,
        ));
    }
    let mut output = Vec::new();
    let mut tool_ids = BTreeMap::new();
    let mut call_ordinal = 0;
    if let Some(content) = candidate.get("content") {
        let content = content
            .as_object()
            .ok_or_else(|| response_error("candidates[].content"))?;
        let parts = content
            .get("parts")
            .and_then(Value::as_array)
            .ok_or_else(|| response_error("candidates[].content.parts"))?;
        for raw in parts {
            let part = raw
                .as_object()
                .ok_or_else(|| response_error("candidates[].content.parts[]"))?;
            if let Some(text) = part.get("text") {
                let text = text
                    .as_str()
                    .ok_or_else(|| response_error("parts[].text"))?;
                output.push(text_output(
                    if part.get("thought") == Some(&Value::Bool(true)) {
                        CanonicalBlockKind::Reasoning
                    } else {
                        CanonicalBlockKind::Text
                    },
                    text.to_owned(),
                ));
            }
            if let Some(media) = part.get("inlineData").or_else(|| part.get("fileData")) {
                let media = media
                    .as_object()
                    .ok_or_else(|| response_error("parts[].media"))?;
                output.push(CanonicalOutputBlock {
                    kind: if media
                        .get("mimeType")
                        .and_then(Value::as_str)
                        .is_some_and(|mime| mime.starts_with("application/pdf"))
                    {
                        CanonicalBlockKind::Document
                    } else {
                        CanonicalBlockKind::Image
                    },
                    text: None,
                    media: Some(MediaSource {
                        media_type: media
                            .get("mimeType")
                            .and_then(Value::as_str)
                            .map(str::to_owned),
                        data: media.get("data").and_then(Value::as_str).map(str::to_owned),
                        uri: media
                            .get("fileUri")
                            .and_then(Value::as_str)
                            .map(str::to_owned),
                        detail: None,
                        file_id: None,
                    }),
                    call_id: None,
                    name: None,
                    arguments: None,
                });
            }
            if let Some(call) = part.get("functionCall") {
                let call = call
                    .as_object()
                    .ok_or_else(|| response_error("parts[].functionCall"))?;
                let args = call
                    .get("args")
                    .ok_or_else(|| response_error("parts[].functionCall.args"))?;
                if !args.is_object() {
                    return Err(response_error("parts[].functionCall.args"));
                }
                let name = required_string(call, "name", "parts[].functionCall.name")?;
                let arguments = compact_value(args);
                let call_id = stable_tool_call_id(&name, &arguments, call_ordinal);
                call_ordinal += 1;
                tool_ids.insert(name.clone(), call_id.clone());
                output.push(CanonicalOutputBlock {
                    kind: CanonicalBlockKind::ToolCall,
                    text: None,
                    media: None,
                    call_id: Some(call_id),
                    name: Some(name),
                    arguments: Some(arguments),
                });
            }
            if let Some(result) = part.get("functionResponse") {
                let result = result
                    .as_object()
                    .ok_or_else(|| response_error("parts[].functionResponse"))?;
                let response = result
                    .get("response")
                    .and_then(Value::as_object)
                    .ok_or_else(|| response_error("parts[].functionResponse.response"))?;
                output.push(CanonicalOutputBlock {
                    kind: CanonicalBlockKind::ToolResult,
                    text: Some(compact_value(
                        response.get("result").unwrap_or(&Value::Null),
                    )),
                    media: None,
                    call_id: optional_string(result, "id")?.or_else(|| {
                        result
                            .get("name")
                            .and_then(Value::as_str)
                            .and_then(|name| tool_ids.get(name).cloned())
                    }),
                    name: optional_string(result, "name")?,
                    arguments: None,
                });
            }
        }
    }
    let response = CanonicalResponse {
        response_id: optional_string(object, "responseId")?,
        model: optional_string(object, "modelVersion")?,
        output,
        finish_reason: optional_string(candidate, "finishReason")?,
        usage: decode_usage(object.get("usageMetadata"), UsageProtocol::Gemini)?,
        provider_error: None,
    };
    Ok(CodecOutput::new(DecodedProviderPayload::Response(
        Box::new(response),
    )))
}

fn encode_for_client(
    response: &CanonicalResponse,
    client_surface: ClientSurface,
) -> Result<CodecOutput<Value>, CodecError> {
    match client_surface {
        ClientSurface::ChatCompletions => encode_openai_response(response),
        ClientSurface::Messages => encode_anthropic_response(response),
        ClientSurface::Responses => encode_responses_response(response),
    }
}

fn encode_responses_response(
    response: &CanonicalResponse,
) -> Result<CodecOutput<Value>, CodecError> {
    let mut output = Vec::new();
    for block in &response.output {
        match block.kind {
            CanonicalBlockKind::Text => output.push(json!({"type":"message", "role":"assistant", "content":[{"type":"output_text", "text":block.text.clone().unwrap_or_default()}]})),
            CanonicalBlockKind::Refusal => output.push(json!({"type":"message", "role":"assistant", "content":[{"type":"refusal", "refusal":block.text.clone().unwrap_or_default()}]})),
            CanonicalBlockKind::Reasoning => output.push(json!({"type":"reasoning", "summary":[{"type":"summary_text", "text":block.text.clone().unwrap_or_default()}]})),
            CanonicalBlockKind::ToolCall => output.push(json!({"type":"function_call", "call_id":block.call_id.clone().unwrap_or_default(), "name":block.name.clone().unwrap_or_default(), "arguments":block.arguments.clone().unwrap_or_default()})),
            CanonicalBlockKind::Image | CanonicalBlockKind::Document => {
                let media = block.media.as_ref().ok_or_else(|| error(CodecReasonCode::UnsupportedSemanticFeature, Some("output.media"), None, Some(WireSurface::OpenaiResponses)))?;
                let value = media.uri.clone().or_else(|| media.file_id.clone()).or_else(|| media.data.as_ref().map(|data| format!("data:{};base64,{}", media.media_type.as_deref().unwrap_or("application/octet-stream"), data))).unwrap_or_default();
                output.push(json!({"type":"message", "role":"assistant", "content":[{"type":if block.kind == CanonicalBlockKind::Document { "output_file" } else { "output_image" }, "url":value}]}));
            }
            CanonicalBlockKind::ToolResult | CanonicalBlockKind::Audio => return Err(error(CodecReasonCode::UnsupportedSemanticFeature, Some("output"), None, Some(WireSurface::OpenaiResponses))),
        }
    }
    let mut result = Map::new();
    result.insert(
        "id".into(),
        Value::String(response.response_id.clone().unwrap_or_default()),
    );
    result.insert("object".into(), Value::String("response".into()));
    result.insert(
        "status".into(),
        Value::String(
            response
                .finish_reason
                .clone()
                .unwrap_or_else(|| "completed".into()),
        ),
    );
    result.insert(
        "model".into(),
        Value::String(response.model.clone().unwrap_or_default()),
    );
    result.insert("output".into(), Value::Array(output));
    if let Some(usage) = &response.usage {
        result.insert("usage".into(), json!({"input_tokens":usage.input_tokens, "output_tokens":usage.output_tokens, "total_tokens":usage.total_tokens}));
    }
    Ok(CodecOutput::new(Value::Object(result)))
}

fn encode_generate_content_response(
    response: &CanonicalResponse,
) -> Result<CodecOutput<Value>, CodecError> {
    let mut parts = Vec::new();
    for block in &response.output {
        match block.kind {
            CanonicalBlockKind::Text => parts.push(json!({"text":block.text.clone().unwrap_or_default()})),
            CanonicalBlockKind::Reasoning => parts.push(json!({"text":block.text.clone().unwrap_or_default(), "thought":true})),
            CanonicalBlockKind::ToolCall => parts.push(json!({"functionCall":{"name":block.name.clone().unwrap_or_default(), "args":serde_json::from_str::<Value>(block.arguments.as_deref().unwrap_or("{}")).map_err(|_| error(CodecReasonCode::MalformedProviderResponse, Some("output.functionCall.args"), None, Some(WireSurface::GeminiGenerateContent)))?}})),
            CanonicalBlockKind::Image | CanonicalBlockKind::Document => {
                let media = block.media.as_ref().ok_or_else(|| error(CodecReasonCode::UnsupportedSemanticFeature, Some("output.media"), None, Some(WireSurface::GeminiGenerateContent)))?;
                if let Some(data) = &media.data {
                    parts.push(json!({"inlineData":{"mimeType":media.media_type.clone().unwrap_or_else(||"application/octet-stream".into()), "data":data}}));
                } else if let Some(uri) = media.uri.as_ref().or(media.file_id.as_ref()) {
                    parts.push(json!({"fileData":{"mimeType":media.media_type.clone().unwrap_or_else(||"application/octet-stream".into()), "fileUri":uri}}));
                } else {
                    return Err(error(CodecReasonCode::UnsupportedSemanticFeature, Some("output.media"), None, Some(WireSurface::GeminiGenerateContent)));
                }
            }
            _ => return Err(error(CodecReasonCode::UnsupportedSemanticFeature, Some("output"), None, Some(WireSurface::GeminiGenerateContent))),
        }
    }
    let mut result = Map::new();
    result.insert("candidates".into(), json!([{"content":{"role":"model", "parts":parts}, "finishReason":response.finish_reason.clone().unwrap_or_else(||"STOP".into())}]));
    if let Some(usage) = &response.usage {
        result.insert("usageMetadata".into(), json!({"promptTokenCount":usage.input_tokens, "candidatesTokenCount":usage.output_tokens, "totalTokenCount":usage.total_tokens}));
    }
    Ok(CodecOutput::new(Value::Object(result)))
}

fn encode_interactions_response(
    response: &CanonicalResponse,
) -> Result<CodecOutput<Value>, CodecError> {
    let mut steps = Vec::new();
    for block in &response.output {
        match block.kind {
            CanonicalBlockKind::Text => steps.push(json!({"type":"model_output", "content":[{"type":"text", "text":block.text.clone().unwrap_or_default()}]})),
            CanonicalBlockKind::Reasoning => steps.push(json!({"type":"thought", "content":[{"type":"text", "text":block.text.clone().unwrap_or_default()}]})),
            CanonicalBlockKind::ToolCall => steps.push(json!({"type":"function_call", "id":block.call_id.clone().unwrap_or_default(), "name":block.name.clone().unwrap_or_default(), "arguments":serde_json::from_str::<Value>(block.arguments.as_deref().unwrap_or("{}")).map_err(|_| error(CodecReasonCode::MalformedProviderResponse, Some("output.function_call.arguments"), None, Some(WireSurface::GeminiInteractions)))?})),
            _ => return Err(error(CodecReasonCode::UnsupportedSemanticFeature, Some("output"), None, Some(WireSurface::GeminiInteractions))),
        }
    }
    let mut result = Map::new();
    result.insert(
        "id".into(),
        Value::String(response.response_id.clone().unwrap_or_default()),
    );
    result.insert("object".into(), Value::String("interaction".into()));
    result.insert(
        "model".into(),
        Value::String(response.model.clone().unwrap_or_default()),
    );
    result.insert(
        "status".into(),
        Value::String(
            response
                .finish_reason
                .clone()
                .unwrap_or_else(|| "completed".into()),
        ),
    );
    result.insert("steps".into(), Value::Array(steps));
    if let Some(usage) = &response.usage {
        result.insert("usage".into(), json!({"total_input_tokens":usage.input_tokens, "total_output_tokens":usage.output_tokens, "total_tokens":usage.total_tokens}));
    }
    Ok(CodecOutput::new(Value::Object(result)))
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
    let object = value.as_object().ok_or_else(|| response_error("usage"))?;
    let mut usage = CanonicalUsage {
        cache_counter_status: CacheCounterStatus::NotReported,
        ..CanonicalUsage::default()
    };
    match protocol {
        UsageProtocol::Openai => {
            usage.input_tokens = token(
                object
                    .get("input_tokens")
                    .or_else(|| object.get("prompt_tokens")),
            );
            usage.output_tokens = token(
                object
                    .get("output_tokens")
                    .or_else(|| object.get("completion_tokens")),
            );
            usage.total_tokens = token(object.get("total_tokens"));
        }
        UsageProtocol::Gemini => {
            usage.input_tokens = token(
                object
                    .get("total_input_tokens")
                    .or_else(|| object.get("promptTokenCount")),
            );
            usage.output_tokens = token(
                object
                    .get("total_output_tokens")
                    .or_else(|| object.get("candidatesTokenCount")),
            );
            usage.total_tokens = token(
                object
                    .get("total_tokens")
                    .or_else(|| object.get("totalTokenCount")),
            );
        }
    }
    if usage.total_tokens.is_none() {
        usage.total_tokens = sum_tokens(usage.input_tokens, usage.output_tokens);
    }
    Ok(Some(usage))
}

#[derive(Clone, Copy)]
enum UsageProtocol {
    Openai,
    Gemini,
}

fn provider_error(
    payload: &Value,
    status: u16,
    surface: WireSurface,
) -> Result<Option<ProviderErrorEvidence>, CodecError> {
    let Some(object) = payload.as_object() else {
        return if status >= 400 {
            Err(response_error("error"))
        } else {
            Ok(None)
        };
    };
    let Some(raw) = object.get("error") else {
        return if status >= 400 {
            Err(error(
                CodecReasonCode::MalformedProviderResponse,
                Some("error"),
                Some(surface),
                None,
            ))
        } else {
            Ok(None)
        };
    };
    let raw = raw.as_object().ok_or_else(|| {
        error(
            CodecReasonCode::MalformedProviderResponse,
            Some("error"),
            Some(surface),
            None,
        )
    })?;
    let error_type = optional_string(raw, "type")?;
    let message = optional_string(raw, "message")?;
    if error_type.is_none() && message.is_none() {
        return Err(error(
            CodecReasonCode::MalformedProviderResponse,
            Some("error"),
            Some(surface),
            None,
        ));
    }
    Ok(Some(ProviderErrorEvidence {
        status,
        error_type,
        message: message.map(|value| truncate(&value)),
    }))
}

fn object(payload: &Value, surface: WireSurface) -> Result<&Map<String, Value>, CodecError> {
    payload.as_object().ok_or_else(|| {
        error(
            CodecReasonCode::MalformedProviderResponse,
            Some("response"),
            Some(surface),
            None,
        )
    })
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
    let field = field.into();
    error(
        CodecReasonCode::MalformedProviderResponse,
        Some(&field),
        None,
        None,
    )
}
fn unsupported(field: &str, surface: WireSurface) -> CodecError {
    error(
        CodecReasonCode::UnsupportedSemanticFeature,
        Some(field),
        Some(surface),
        None,
    )
}
fn error(
    reason: CodecReasonCode,
    field: Option<&str>,
    source: Option<WireSurface>,
    target: Option<WireSurface>,
) -> CodecError {
    CodecError {
        reason,
        field: field.map(ToOwned::to_owned),
        source_surface: source,
        target_surface: target,
    }
}
fn text_output(kind: CanonicalBlockKind, text: String) -> CanonicalOutputBlock {
    CanonicalOutputBlock {
        kind,
        text: Some(text),
        media: None,
        call_id: None,
        name: None,
        arguments: None,
    }
}

fn valid_prompt_breakpoint(value: Option<&Value>) -> bool {
    value
        .and_then(Value::as_object)
        .and_then(|object| object.get("mode"))
        .and_then(Value::as_str)
        == Some("explicit")
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
fn compact_json(object: &Map<String, Value>) -> String {
    serde_json::to_string(object).unwrap_or_else(|_| "{}".into())
}
fn compact_value(value: &Value) -> String {
    serde_json::to_string(value).unwrap_or_else(|_| "{}".into())
}
fn json_text(value: Option<&Value>) -> String {
    value.map(compact_value).unwrap_or_else(|| "{}".into())
}
fn truncate(message: &str) -> String {
    if message.len() <= MAX_PROVIDER_ERROR_MESSAGE_BYTES {
        return message.to_owned();
    }
    let mut end = MAX_PROVIDER_ERROR_MESSAGE_BYTES;
    while !message.is_char_boundary(end) {
        end -= 1;
    }
    message[..end].to_owned()
}
