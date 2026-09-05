//! Small canonical request/response/event representation.
//!
//! The types in this module are the source-owned semantic intent for every
//! later codec.  They intentionally do not retain an opaque input object or
//! provider credentials.  Debug output is structural and redacted because
//! message text and inline media are client-controlled data.

use std::{collections::BTreeMap, fmt};

use serde_json::{Map, Value, json};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ClientSurface {
    ChatCompletions,
    Responses,
    Messages,
}

pub type CanonicalSurface = ClientSurface;

impl ClientSurface {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::ChatCompletions => "chat_completions",
            Self::Responses => "responses",
            Self::Messages => "messages",
        }
    }

    pub const fn protocol(self) -> &'static str {
        match self {
            Self::Messages => "anthropic",
            Self::ChatCompletions | Self::Responses => "openai",
        }
    }
}

impl TryFrom<&str> for ClientSurface {
    type Error = &'static str;

    fn try_from(value: &str) -> Result<Self, Self::Error> {
        match value {
            "chat_completions" => Ok(Self::ChatCompletions),
            "responses" => Ok(Self::Responses),
            "messages" => Ok(Self::Messages),
            _ => Err("unsupported client surface"),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CanonicalRole {
    System,
    Developer,
    User,
    Assistant,
    Tool,
}

impl CanonicalRole {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::System => "system",
            Self::Developer => "developer",
            Self::User => "user",
            Self::Assistant => "assistant",
            Self::Tool => "tool",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CanonicalBlockKind {
    Text,
    Image,
    Document,
    Audio,
    Reasoning,
    ToolCall,
    ToolResult,
    Refusal,
}

impl CanonicalBlockKind {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Text => "text",
            Self::Image => "image",
            Self::Document => "document",
            Self::Audio => "audio",
            Self::Reasoning => "reasoning",
            Self::ToolCall => "tool_call",
            Self::ToolResult => "tool_result",
            Self::Refusal => "refusal",
        }
    }
}

#[derive(Clone, PartialEq, Eq)]
pub enum Presence<T> {
    Missing,
    Null,
    Value(T),
}

impl<T: fmt::Debug> fmt::Debug for Presence<T> {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Missing => formatter.write_str("Missing"),
            Self::Null => formatter.write_str("Null"),
            Self::Value(value) => formatter.debug_tuple("Value").field(value).finish(),
        }
    }
}

impl<T> Presence<T> {
    pub fn from_object(
        object: &Map<String, Value>,
        key: &str,
        decode: impl FnOnce(&Value) -> Option<T>,
    ) -> Self {
        match object.get(key) {
            None => Self::Missing,
            Some(Value::Null) => Self::Null,
            Some(value) => decode(value).map_or(Self::Null, Self::Value),
        }
    }

    pub fn value(&self) -> Option<&T> {
        match self {
            Self::Value(value) => Some(value),
            Self::Missing | Self::Null => None,
        }
    }
}

#[derive(Clone, PartialEq)]
pub struct MediaSource {
    pub media_type: Option<String>,
    pub data: Option<String>,
    pub uri: Option<String>,
}

impl fmt::Debug for MediaSource {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("MediaSource")
            .field("media_type", &self.media_type)
            .field("data_bytes", &self.data.as_ref().map(String::len))
            .field("uri_present", &self.uri.is_some())
            .finish()
    }
}

#[derive(Clone, PartialEq)]
pub struct CanonicalContentBlock {
    pub kind: CanonicalBlockKind,
    pub text: Option<String>,
    pub media: Option<MediaSource>,
    pub call_id: Option<String>,
    pub name: Option<String>,
    pub arguments: Option<String>,
    pub tool_input: Option<Map<String, Value>>,
    pub is_error: bool,
    pub signature: Option<String>,
}

impl CanonicalContentBlock {
    pub fn text(text: impl Into<String>) -> Self {
        Self {
            kind: CanonicalBlockKind::Text,
            text: Some(text.into()),
            media: None,
            call_id: None,
            name: None,
            arguments: None,
            tool_input: None,
            is_error: false,
            signature: None,
        }
    }
}

impl fmt::Debug for CanonicalContentBlock {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("CanonicalContentBlock")
            .field("kind", &self.kind)
            .field("text_bytes", &self.text.as_ref().map(String::len))
            .field("media", &self.media)
            .field("call_id_present", &self.call_id.is_some())
            .field("name", &self.name)
            .field("arguments_bytes", &self.arguments.as_ref().map(String::len))
            .field("tool_input_keys", &self.tool_input.as_ref().map(Map::len))
            .field("is_error", &self.is_error)
            .field("signature_present", &self.signature.is_some())
            .finish()
    }
}

#[derive(Clone, PartialEq)]
pub struct CanonicalMessage {
    pub role: CanonicalRole,
    pub content: Vec<CanonicalContentBlock>,
    pub tool_call_id: Option<String>,
    pub name: Option<String>,
    pub refusal: Option<String>,
}

impl CanonicalMessage {
    pub fn text(&self) -> String {
        self.content
            .iter()
            .filter(|block| block.kind == CanonicalBlockKind::Text)
            .filter_map(|block| block.text.as_deref())
            .collect()
    }
}

impl fmt::Debug for CanonicalMessage {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("CanonicalMessage")
            .field("role", &self.role)
            .field("content", &self.content)
            .field("tool_call_id_present", &self.tool_call_id.is_some())
            .field("name", &self.name)
            .field("refusal_bytes", &self.refusal.as_ref().map(String::len))
            .finish()
    }
}

#[derive(Clone, PartialEq)]
pub struct CanonicalTool {
    pub name: String,
    pub description: Option<String>,
    pub parameters: Map<String, Value>,
}

impl fmt::Debug for CanonicalTool {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("CanonicalTool")
            .field("name", &self.name)
            .field(
                "description_bytes",
                &self.description.as_ref().map(String::len),
            )
            .field(
                "parameter_keys",
                &self.parameters.keys().collect::<Vec<_>>(),
            )
            .finish()
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ToolChoiceMode {
    Auto,
    Required,
    None,
    Function,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CanonicalToolChoice {
    pub mode: ToolChoiceMode,
    pub function_name: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReasoningMode {
    Unspecified,
    Effort,
    FixedBudget,
    Adaptive,
    Toggle,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReasoningIntent {
    pub requested: Option<bool>,
    pub mode: ReasoningMode,
    pub effort: Option<String>,
    pub budget_tokens: Option<u64>,
    pub explicit_disable: bool,
}

impl Default for ReasoningIntent {
    fn default() -> Self {
        Self {
            requested: None,
            mode: ReasoningMode::Unspecified,
            effort: None,
            budget_tokens: None,
            explicit_disable: false,
        }
    }
}

impl ReasoningIntent {
    pub fn disabled() -> Self {
        Self {
            requested: Some(false),
            mode: ReasoningMode::Toggle,
            effort: None,
            budget_tokens: None,
            explicit_disable: true,
        }
    }

    pub fn fixed(budget_tokens: u64) -> Self {
        Self {
            requested: Some(true),
            mode: ReasoningMode::FixedBudget,
            effort: None,
            budget_tokens: Some(budget_tokens),
            explicit_disable: false,
        }
    }

    pub fn to_thinking_requirement(&self) -> Option<crate::routing::ThinkingRequirement> {
        self.requested
            .map(|requested| crate::routing::ThinkingRequirement {
                requested,
                requested_toggle: (self.mode == ReasoningMode::Toggle).then_some(requested),
                effort: self.effort.clone(),
                budget_tokens: self.budget_tokens,
                explicit_disable: self.explicit_disable,
            })
    }
}

#[derive(Clone, PartialEq)]
pub struct RequestPresence {
    pub stream: Presence<bool>,
    pub max_output_tokens: Presence<u64>,
    pub temperature: Presence<f64>,
    pub top_p: Presence<f64>,
    pub stop: Presence<Vec<String>>,
    pub response_format: Presence<Map<String, Value>>,
    pub parallel_tool_calls: Presence<bool>,
}

impl fmt::Debug for RequestPresence {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("RequestPresence")
            .field("stream", &self.stream)
            .field("max_output_tokens", &self.max_output_tokens)
            .field("temperature", &self.temperature)
            .field("top_p", &self.top_p)
            .field("stop", &self.stop)
            .field(
                "response_format",
                &presence_map_debug(&self.response_format),
            )
            .field("parallel_tool_calls", &self.parallel_tool_calls)
            .finish()
    }
}

fn presence_map_debug(presence: &Presence<Map<String, Value>>) -> String {
    match presence {
        Presence::Missing => "Missing".into(),
        Presence::Null => "Null".into(),
        Presence::Value(value) => format!("Value({} keys)", value.len()),
    }
}

#[derive(Clone, PartialEq)]
pub struct CanonicalRequest {
    pub model: String,
    pub client_surface: ClientSurface,
    pub messages: Vec<CanonicalMessage>,
    pub stream: bool,
    pub max_output_tokens: Option<u64>,
    pub temperature: Option<f64>,
    pub top_p: Option<f64>,
    pub stop: Option<Vec<String>>,
    pub tools: Vec<CanonicalTool>,
    pub tool_choice: Option<CanonicalToolChoice>,
    pub response_format: Option<Map<String, Value>>,
    pub reasoning: ReasoningIntent,
    pub cache_control: Option<Value>,
    pub metadata: BTreeMap<String, String>,
    pub parallel_tool_calls: Option<bool>,
    pub presence: RequestPresence,
}

impl fmt::Debug for CanonicalRequest {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("CanonicalRequest")
            .field("model", &self.model)
            .field("client_surface", &self.client_surface)
            .field("messages", &self.messages)
            .field("stream", &self.stream)
            .field("max_output_tokens", &self.max_output_tokens)
            .field("temperature", &self.temperature)
            .field("top_p", &self.top_p)
            .field("stop", &self.stop)
            .field("tools", &self.tools)
            .field("tool_choice", &self.tool_choice)
            .field(
                "response_format_keys",
                &self.response_format.as_ref().map(Map::len),
            )
            .field("reasoning", &self.reasoning)
            .field("cache_control_present", &self.cache_control.is_some())
            .field("metadata_keys", &self.metadata.keys().collect::<Vec<_>>())
            .field("parallel_tool_calls", &self.parallel_tool_calls)
            .field("presence", &self.presence)
            .finish()
    }
}

impl CanonicalRequest {
    pub fn first_user_text(&self) -> Option<String> {
        self.messages
            .iter()
            .find(|message| message.role == CanonicalRole::User)
            .map(CanonicalMessage::text)
            .filter(|text| !text.trim().is_empty())
    }

    pub fn conversation_prefix(&self) -> Vec<(CanonicalRole, String)> {
        self.messages
            .iter()
            .filter(|message| {
                matches!(
                    message.role,
                    CanonicalRole::System | CanonicalRole::Developer
                )
            })
            .map(|message| (message.role, message.text()))
            .collect()
    }

    /// Build the portable OpenAI-shaped JSON used by the pure body boundary.
    pub fn to_surface_value(&self, surface: ClientSurface) -> Value {
        match surface {
            ClientSurface::Messages => self.to_messages_value(),
            ClientSurface::Responses => self.to_responses_value(),
            ClientSurface::ChatCompletions => self.to_chat_value(),
        }
    }

    fn to_chat_value(&self) -> Value {
        let mut out = Map::new();
        out.insert("model".into(), Value::String(self.model.clone()));
        out.insert(
            "messages".into(),
            Value::Array(self.messages.iter().map(encode_chat_message).collect()),
        );
        add_common_fields(&mut out, self, "max_completion_tokens");
        encode_tools_and_choice(&mut out, self, false);
        Value::Object(out)
    }

    fn to_messages_value(&self) -> Value {
        let mut out = Map::new();
        out.insert("model".into(), Value::String(self.model.clone()));
        let mut messages = Vec::new();
        for message in &self.messages {
            if message.role == CanonicalRole::System {
                out.insert("system".into(), encode_content(&message.content, false));
            } else {
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
                item.insert("content".into(), encode_content(&message.content, false));
                messages.push(Value::Object(item));
            }
        }
        out.insert("messages".into(), Value::Array(messages));
        add_common_fields(&mut out, self, "max_tokens");
        encode_tools_and_choice(&mut out, self, true);
        if self.reasoning.requested == Some(true) {
            if self.reasoning.mode == ReasoningMode::Adaptive {
                out.insert("thinking".into(), json!({"type":"adaptive"}));
            } else if let Some(budget) = self.reasoning.budget_tokens {
                out.insert(
                    "thinking".into(),
                    json!({"type":"enabled", "budget_tokens":budget}),
                );
            }
        }
        Value::Object(out)
    }

    fn to_responses_value(&self) -> Value {
        let mut out = Map::new();
        out.insert("model".into(), Value::String(self.model.clone()));
        out.insert("stream".into(), Value::Bool(self.stream));
        out.insert("store".into(), Value::Bool(false));
        if let Some(message) = self
            .messages
            .iter()
            .find(|message| message.role == CanonicalRole::System)
        {
            out.insert("instructions".into(), Value::String(message.text()));
        }
        out.insert("input".into(), Value::Array(self.messages.iter().filter(|message| message.role != CanonicalRole::System).map(|message| {
            json!({"role": message.role.as_str(), "content": encode_content(&message.content, true)})
        }).collect()));
        add_common_fields(&mut out, self, "max_output_tokens");
        if !self.tools.is_empty() {
            out.insert("tools".into(), Value::Array(self.tools.iter().map(|tool| json!({
                "type":"function", "name":tool.name, "description":tool.description.clone().unwrap_or_default(),
                "parameters":Value::Object(tool.parameters.clone()), "strict":false
            })).collect()));
        }
        Value::Object(out)
    }
}

#[derive(Clone, PartialEq, Eq)]
pub struct CanonicalOutputBlock {
    pub kind: CanonicalBlockKind,
    pub text: Option<String>,
    pub call_id: Option<String>,
    pub name: Option<String>,
    pub arguments: Option<String>,
}

impl fmt::Debug for CanonicalOutputBlock {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("CanonicalOutputBlock")
            .field("kind", &self.kind)
            .field("text_bytes", &self.text.as_ref().map(String::len))
            .field("call_id_present", &self.call_id.is_some())
            .field("name", &self.name)
            .field("arguments_bytes", &self.arguments.as_ref().map(String::len))
            .finish()
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CacheCounterStatus {
    Reported,
    NotReported,
    UnknownFormat,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CanonicalUsage {
    pub input_tokens: Option<u64>,
    pub output_tokens: Option<u64>,
    pub total_tokens: Option<u64>,
    pub cached_input_tokens: Option<u64>,
    pub cache_read_input_tokens: Option<u64>,
    pub cache_creation_input_tokens: Option<u64>,
    pub cache_write_input_tokens: Option<u64>,
    pub reasoning_tokens: Option<u64>,
    pub cache_counter_status: CacheCounterStatus,
}

impl Default for CanonicalUsage {
    fn default() -> Self {
        Self {
            input_tokens: None,
            output_tokens: None,
            total_tokens: None,
            cached_input_tokens: None,
            cache_read_input_tokens: None,
            cache_creation_input_tokens: None,
            cache_write_input_tokens: None,
            reasoning_tokens: None,
            cache_counter_status: CacheCounterStatus::UnknownFormat,
        }
    }
}

#[derive(Clone, PartialEq, Eq)]
pub struct CanonicalResponse {
    pub response_id: Option<String>,
    pub model: Option<String>,
    pub output: Vec<CanonicalOutputBlock>,
    pub finish_reason: Option<String>,
    pub usage: Option<CanonicalUsage>,
    pub provider_error: Option<ProviderErrorEvidence>,
}

impl fmt::Debug for CanonicalResponse {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("CanonicalResponse")
            .field("response_id", &self.response_id)
            .field("model", &self.model)
            .field("output", &self.output)
            .field("finish_reason", &self.finish_reason)
            .field("usage", &self.usage)
            .field(
                "provider_error",
                &self.provider_error.as_ref().map(|error| {
                    (
                        error.status,
                        error.error_type.as_deref(),
                        error.message.as_ref().map(String::len),
                    )
                }),
            )
            .finish()
    }
}

#[derive(Clone, PartialEq, Eq)]
pub struct ProviderErrorEvidence {
    pub status: u16,
    pub error_type: Option<String>,
    pub message: Option<String>,
}

impl fmt::Debug for ProviderErrorEvidence {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("ProviderErrorEvidence")
            .field("status", &self.status)
            .field("error_type", &self.error_type)
            .field("message_bytes", &self.message.as_ref().map(String::len))
            .finish()
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CanonicalEventType {
    ResponseStart,
    ContentStart,
    TextDelta,
    ReasoningStart,
    ReasoningDelta,
    ReasoningStop,
    ToolCallStart,
    ToolCallArgumentsDelta,
    ToolCallStop,
    ContentStop,
    Usage,
    ResponseComplete,
    ResponseIncomplete,
    Error,
}

#[derive(Clone, PartialEq, Eq)]
pub struct CanonicalEvent {
    pub event_type: CanonicalEventType,
    pub response_id: Option<String>,
    pub model: Option<String>,
    pub index: Option<usize>,
    pub delta: Option<String>,
    pub call_id: Option<String>,
    pub name: Option<String>,
    pub arguments: Option<String>,
    pub finish_reason: Option<String>,
    pub usage: Option<CanonicalUsage>,
    pub error_type: Option<String>,
    pub error_message: Option<String>,
}

impl CanonicalEvent {
    pub const fn kind(&self) -> CanonicalEventType {
        self.event_type
    }
}

impl fmt::Debug for CanonicalEvent {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("CanonicalEvent")
            .field("event_type", &self.event_type)
            .field("response_id", &self.response_id)
            .field("model", &self.model)
            .field("index", &self.index)
            .field("delta_bytes", &self.delta.as_ref().map(String::len))
            .field("call_id_present", &self.call_id.is_some())
            .field("name", &self.name)
            .field("arguments_bytes", &self.arguments.as_ref().map(String::len))
            .field("finish_reason", &self.finish_reason)
            .field("usage", &self.usage)
            .field("error_type", &self.error_type)
            .field(
                "error_message_bytes",
                &self.error_message.as_ref().map(String::len),
            )
            .finish()
    }
}

fn add_common_fields(out: &mut Map<String, Value>, request: &CanonicalRequest, max_key: &str) {
    out.insert("stream".into(), Value::Bool(request.stream));
    if let Some(value) = request.max_output_tokens {
        out.insert(max_key.into(), value.into());
    }
    if let Some(value) = request.temperature {
        out.insert("temperature".into(), value.into());
    }
    if let Some(value) = request.top_p {
        out.insert("top_p".into(), value.into());
    }
    if let Some(values) = &request.stop {
        out.insert(
            "stop".into(),
            if values.len() == 1 {
                Value::String(values[0].clone())
            } else {
                Value::Array(values.iter().cloned().map(Value::String).collect())
            },
        );
    }
    if let Some(value) = &request.response_format {
        out.insert("response_format".into(), Value::Object(value.clone()));
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

fn encode_tools_and_choice(
    out: &mut Map<String, Value>,
    request: &CanonicalRequest,
    anthropic: bool,
) {
    if !request.tools.is_empty() {
        out.insert("tools".into(), Value::Array(request.tools.iter().map(|tool| {
            if anthropic { json!({"name":tool.name, "description":tool.description.clone().unwrap_or_default(), "input_schema":Value::Object(tool.parameters.clone())}) }
            else { json!({"type":"function", "function":{"name":tool.name, "description":tool.description, "parameters":Value::Object(tool.parameters.clone())}}) }
        }).collect()));
    }
    if let Some(choice) = &request.tool_choice {
        let value = match choice.mode {
            ToolChoiceMode::Function => {
                if anthropic {
                    json!({"type":"tool", "name":choice.function_name})
                } else {
                    json!({"type":"function", "function":{"name":choice.function_name}})
                }
            }
            ToolChoiceMode::Required => {
                if anthropic {
                    json!({"type":"any"})
                } else {
                    Value::String("required".into())
                }
            }
            ToolChoiceMode::Auto => Value::String("auto".into()),
            ToolChoiceMode::None => Value::String("none".into()),
        };
        out.insert("tool_choice".into(), value);
    }
}

fn encode_chat_message(message: &CanonicalMessage) -> Value {
    let mut item = Map::new();
    item.insert("role".into(), Value::String(message.role.as_str().into()));
    item.insert("content".into(), encode_content(&message.content, true));
    if let Some(id) = &message.tool_call_id {
        item.insert("tool_call_id".into(), Value::String(id.clone()));
    }
    let calls: Vec<Value> = message.content.iter().filter(|block| block.kind == CanonicalBlockKind::ToolCall).map(|block| json!({
        "id":block.call_id, "type":"function", "function":{"name":block.name, "arguments":block.arguments}
    })).collect();
    if !calls.is_empty() {
        item.insert("tool_calls".into(), Value::Array(calls));
    }
    if let Some(refusal) = &message.refusal {
        item.insert("refusal".into(), Value::String(refusal.clone()));
    }
    Value::Object(item)
}

fn encode_content(content: &[CanonicalContentBlock], openai: bool) -> Value {
    if content.len() == 1 && content[0].kind == CanonicalBlockKind::Text {
        return Value::String(content[0].text.clone().unwrap_or_default());
    }
    Value::Array(content.iter().filter_map(|block| match block.kind {
        CanonicalBlockKind::Text => Some(json!({"type":"text", "text":block.text.clone().unwrap_or_default()})),
        CanonicalBlockKind::Image if openai => Some(json!({
            "type":"image_url",
            "image_url":{"url":block.media.as_ref().map(|media| media.uri.clone().unwrap_or_else(|| format!(
                "data:{};base64,{}",
                media.media_type.as_deref().unwrap_or("application/octet-stream"),
                media.data.as_deref().unwrap_or_default(),
            ))).unwrap_or_default()}
        })),
        CanonicalBlockKind::Image => block.media.as_ref().map(|media| if let Some(data) = &media.data { json!({"type":"image", "source":{"type":"base64", "media_type":media.media_type.clone().unwrap_or_else(||"application/octet-stream".into()), "data":data}}) } else { json!({"type":"image", "source":{"type":"url", "url":media.uri}}) }),
        CanonicalBlockKind::Reasoning if openai => Some(json!({"type":"reasoning_content", "text":block.text.clone().unwrap_or_default()})),
        CanonicalBlockKind::Reasoning => Some(json!({"type":"thinking", "thinking":block.text.clone().unwrap_or_default()})),
        CanonicalBlockKind::ToolCall if !openai => Some(json!({"type":"tool_use", "id":block.call_id, "name":block.name, "input":Value::Object(block.tool_input.clone().unwrap_or_default())})),
        CanonicalBlockKind::ToolResult if !openai => Some(json!({"type":"tool_result", "tool_use_id":block.call_id, "content":block.text, "is_error":block.is_error})),
        CanonicalBlockKind::Refusal => Some(json!({"type":"refusal", "refusal":block.text.clone().unwrap_or_default()})),
        _ => None,
    }).collect())
}
