//! Closed codec contract for the M6 canonical wire boundary.
//!
//! This file freezes the interface used by later codec slices.  It deliberately
//! contains no HTTP client, credential, retry, or runtime-preference state.

use serde::{Deserialize, Serialize};
use serde_json::Value;

use super::adaptation::{AdaptationPolicy, apply_adaptation_policy};
use super::registry::{CodecFamily, ConfiguredWireProfile, WireSurface};
use crate::wire::ir::{
    CanonicalEvent, CanonicalRequest, CanonicalResponse, ClientSurface, ProviderErrorEvidence,
};
use crate::wire::stream::{SseFrame, decode_stream_event, encode_client_event};

/// Python-owned codec identifiers accepted by the static registry.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub enum WireCodecId {
    #[serde(rename = "openai_chat")]
    OpenaiChat,
    #[serde(rename = "openai_chat_sse")]
    OpenaiChatSse,
    #[serde(rename = "openai_responses")]
    OpenaiResponses,
    #[serde(rename = "openai_responses_sse")]
    OpenaiResponsesSse,
    #[serde(rename = "anthropic_messages")]
    AnthropicMessages,
    #[serde(rename = "anthropic_messages_sse")]
    AnthropicMessagesSse,
    #[serde(rename = "gemini_interactions")]
    GeminiInteractions,
    #[serde(rename = "gemini_interactions_sse")]
    GeminiInteractionsSse,
    #[serde(rename = "gemini_generate_content")]
    GeminiGenerateContent,
    #[serde(rename = "gemini_generate_content_sse")]
    GeminiGenerateContentSse,
}

impl WireCodecId {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::OpenaiChat => "openai_chat",
            Self::OpenaiChatSse => "openai_chat_sse",
            Self::OpenaiResponses => "openai_responses",
            Self::OpenaiResponsesSse => "openai_responses_sse",
            Self::AnthropicMessages => "anthropic_messages",
            Self::AnthropicMessagesSse => "anthropic_messages_sse",
            Self::GeminiInteractions => "gemini_interactions",
            Self::GeminiInteractionsSse => "gemini_interactions_sse",
            Self::GeminiGenerateContent => "gemini_generate_content",
            Self::GeminiGenerateContentSse => "gemini_generate_content_sse",
        }
    }

    pub const fn family(self) -> CodecFamily {
        match self {
            Self::OpenaiChat | Self::OpenaiChatSse => CodecFamily::OpenaiChat,
            Self::OpenaiResponses | Self::OpenaiResponsesSse => CodecFamily::OpenaiResponses,
            Self::AnthropicMessages | Self::AnthropicMessagesSse => CodecFamily::AnthropicMessages,
            Self::GeminiInteractions | Self::GeminiInteractionsSse => {
                CodecFamily::GeminiInteractions
            }
            Self::GeminiGenerateContent | Self::GeminiGenerateContentSse => {
                CodecFamily::GeminiGenerateContent
            }
        }
    }
}

/// Stable machine-readable codec outcome reasons.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CodecReasonCode {
    MalformedSourceRequest,
    MalformedProviderResponse,
    MalformedProviderEvent,
    UnsupportedWireProfile,
    UnsupportedSemanticFeature,
    LossRejected,
    ResourceLimitViolation,
}

/// Stable semantic code for a non-fatal adaptation notice.
///
/// The code is deliberately a string rather than a provider error message:
/// callers can aggregate it safely without retaining prompts, schemas, or
/// provider payloads.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AdaptationCode(pub String);

/// A bounded semantic adaptation notice.  It contains no body or credential.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AdaptationNotice {
    pub code: AdaptationCode,
    pub reason: CodecReasonCode,
    pub field: Option<String>,
    pub source_surface: Option<WireSurface>,
    pub target_surface: Option<WireSurface>,
}

impl AdaptationNotice {
    pub fn new(
        code: impl Into<String>,
        reason: CodecReasonCode,
        field: Option<impl Into<String>>,
        source_surface: Option<WireSurface>,
        target_surface: Option<WireSurface>,
    ) -> Self {
        Self {
            code: AdaptationCode(code.into()),
            reason,
            field: field.map(Into::into),
            source_surface,
            target_surface,
        }
    }
}

/// Typed codec failure with stable reason and optional structural field.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CodecError {
    pub reason: CodecReasonCode,
    pub field: Option<String>,
    pub source_surface: Option<WireSurface>,
    pub target_surface: Option<WireSurface>,
}

impl CodecError {
    pub const fn new(reason: CodecReasonCode) -> Self {
        Self {
            reason,
            field: None,
            source_surface: None,
            target_surface: None,
        }
    }
}

/// Successful codec output plus typed warnings/loss metadata.
#[derive(Debug, Clone, PartialEq)]
pub struct CodecOutput<T> {
    pub value: T,
    pub notices: Vec<AdaptationNotice>,
}

impl<T> CodecOutput<T> {
    pub const fn new(value: T) -> Self {
        Self {
            value,
            notices: Vec::new(),
        }
    }
}

/// A valid provider error envelope is evidence, not a codec parse failure.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DecodedProviderPayload {
    Response(Box<CanonicalResponse>),
    Error(ProviderErrorEvidence),
}

/// W008 supplies the concrete incremental stream adapter for this identity.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum StreamAdapterKind {
    OpenaiChatSse,
    OpenaiResponsesSse,
    AnthropicMessagesSse,
    GeminiInteractionsSse,
    GeminiGenerateContentSse,
}

/// Whether a client/upstream pair is native or requires the canonical bridge.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CompatibilityPath {
    Native,
    CanonicalAdaptation,
}

pub fn compatibility_path(
    client_surface: ClientSurface,
    upstream_surface: WireSurface,
) -> CompatibilityPath {
    let native = matches!(
        (client_surface, upstream_surface),
        (
            ClientSurface::ChatCompletions,
            WireSurface::OpenaiChatCompletions
        ) | (ClientSurface::Responses, WireSurface::OpenaiResponses)
            | (ClientSurface::Messages, WireSurface::AnthropicMessages)
    );
    if native {
        CompatibilityPath::Native
    } else {
        CompatibilityPath::CanonicalAdaptation
    }
}

/// Pure operations implemented by each later built-in surface codec.
pub trait WireCodec {
    fn codec_id(&self) -> WireCodecId;
    fn surface(&self) -> WireSurface;

    fn decode_client_request(
        &self,
        value: &Value,
        client_surface: ClientSurface,
    ) -> Result<CodecOutput<CanonicalRequest>, CodecError>;

    fn encode_request(
        &self,
        request: &CanonicalRequest,
        profile: &ConfiguredWireProfile,
    ) -> Result<CodecOutput<Value>, CodecError>;

    /// Apply the shared W006 loss policy to request adaptation notices.
    fn encode_request_with_policy(
        &self,
        request: &CanonicalRequest,
        profile: &ConfiguredWireProfile,
        policy: &AdaptationPolicy,
    ) -> Result<CodecOutput<Value>, CodecError> {
        apply_adaptation_policy(self.encode_request(request, profile)?, policy)
    }

    fn decode_response(
        &self,
        payload: &Value,
        status: u16,
    ) -> Result<CodecOutput<DecodedProviderPayload>, CodecError>;

    fn encode_response(
        &self,
        response: &CanonicalResponse,
        client_surface: ClientSurface,
    ) -> Result<CodecOutput<Value>, CodecError>;

    /// Apply the shared W006 loss policy to response adaptation notices.
    fn encode_response_with_policy(
        &self,
        response: &CanonicalResponse,
        client_surface: ClientSurface,
        policy: &AdaptationPolicy,
    ) -> Result<CodecOutput<Value>, CodecError> {
        apply_adaptation_policy(self.encode_response(response, client_surface)?, policy)
    }

    fn stream_adapter(&self) -> StreamAdapterKind;

    fn decode_stream_event(
        &self,
        _frame: &Value,
    ) -> Result<CodecOutput<Vec<CanonicalEvent>>, CodecError> {
        decode_stream_event(self.stream_adapter(), _frame)
    }

    /// Decode one already-framed SSE record without owning byte framing.
    fn decode_stream_frame(
        &self,
        frame: &SseFrame,
    ) -> Result<CodecOutput<Vec<CanonicalEvent>>, CodecError> {
        let mut value = serde_json::Map::new();
        value.insert("data".into(), serde_json::Value::String(frame.data.clone()));
        if let Some(event) = &frame.event {
            value.insert("event".into(), serde_json::Value::String(event.clone()));
        }
        self.decode_stream_event(&serde_json::Value::Object(value))
    }

    /// Encode one canonical event in a client streaming grammar.
    ///
    /// An empty byte vector is an intentional no-op for events that have no
    /// faithful representation on the selected client surface.  Material
    /// loss remains a typed error in the same way as finite adaptation.
    fn encode_stream_event(
        &self,
        event: &CanonicalEvent,
        client_surface: ClientSurface,
    ) -> Result<Vec<u8>, CodecError> {
        encode_client_event(client_surface, event)
    }
}

/// Closed family mapping used before concrete W004-W008 implementations land.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BuiltinCodec {
    OpenaiChat,
    OpenaiResponses,
    AnthropicMessages,
    GeminiInteractions,
    GeminiGenerateContent,
}

impl BuiltinCodec {
    pub const fn family(self) -> CodecFamily {
        match self {
            Self::OpenaiChat => CodecFamily::OpenaiChat,
            Self::OpenaiResponses => CodecFamily::OpenaiResponses,
            Self::AnthropicMessages => CodecFamily::AnthropicMessages,
            Self::GeminiInteractions => CodecFamily::GeminiInteractions,
            Self::GeminiGenerateContent => CodecFamily::GeminiGenerateContent,
        }
    }
}

pub fn builtin_codec(family: CodecFamily) -> BuiltinCodec {
    match family {
        CodecFamily::OpenaiChat => BuiltinCodec::OpenaiChat,
        CodecFamily::OpenaiResponses => BuiltinCodec::OpenaiResponses,
        CodecFamily::AnthropicMessages => BuiltinCodec::AnthropicMessages,
        CodecFamily::GeminiInteractions => BuiltinCodec::GeminiInteractions,
        CodecFamily::GeminiGenerateContent => BuiltinCodec::GeminiGenerateContent,
    }
}
