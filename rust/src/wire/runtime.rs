//! Caller-selected canonical wire runtime facade.
//!
//! This is the M6/M7 handoff.  The registry is immutable and shareable; all
//! request and stream state is owned by the value returned for that operation.
//! No method in this module selects a profile, sends a request, retries, or
//! owns response handoff/finalization.

use std::{fmt, sync::Arc};

use bytes::Bytes;
use serde_json::Value;
use thiserror::Error;

use super::ir::{
    CanonicalBlockKind, CanonicalEvent, CanonicalRequest, CanonicalResponse, CanonicalUsage,
    ClientSurface, ProviderErrorEvidence,
};
use super::{
    AdaptationNotice, AdaptationPolicy, CodecError, CodecReasonCode, ConfiguredWireProfile,
    DecodedProviderPayload, StreamAdapterKind, StreamError, StreamEventDecoder,
    StreamTerminalSummary, WireCodec, WireCodecId, WireProfileRegistry, WireSurface,
    builtin_codec_instance, compatibility_path, encode_client_event,
};
use crate::model_router::AffinityIdentityInput;
use crate::request::{
    AdmissionError, AdmissionOptions, AdmittedRequest, DEFAULT_MAX_REQUEST_BODY_BYTES,
    StaticRoutingFacts, admit_request, affinity_identity_input, encode_compact_json_bounded,
};
use crate::routing::RoutingRequestFacts;

pub const DEFAULT_MAX_PROVIDER_BODY_BYTES: usize = DEFAULT_MAX_REQUEST_BODY_BYTES;
const MAX_CONTEXT_FIELD_BYTES: usize = 512;
const MAX_PROVIDER_KIND_BYTES: usize = 64;

/// Static flags supplied with a selected profile.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct WireProfileFlags {
    pub supports_streaming: bool,
    pub body_passthrough: bool,
}

impl WireProfileFlags {
    pub fn for_surfaces(client: ClientSurface, upstream: WireSurface) -> Self {
        Self {
            supports_streaming: true,
            body_passthrough: compatibility_path(client, upstream)
                == super::CompatibilityPath::Native,
        }
    }
}

impl Default for WireProfileFlags {
    fn default() -> Self {
        Self {
            supports_streaming: true,
            body_passthrough: false,
        }
    }
}

/// Secret-free facts for one caller-selected upstream profile.
#[derive(Clone)]
pub struct WireRuntimeContext {
    pub provider_id: Option<String>,
    pub provider_kind: Option<String>,
    pub client_surface: ClientSurface,
    pub selected_profile: ConfiguredWireProfile,
    pub canonical_model_id: String,
    pub upstream_model_id: String,
    pub adaptation_policy: AdaptationPolicy,
    pub profile_flags: WireProfileFlags,
    pub max_request_body_bytes: usize,
    pub max_provider_body_bytes: usize,
    pub max_encoded_body_bytes: usize,
}

impl WireRuntimeContext {
    pub fn new(
        client_surface: ClientSurface,
        selected_profile: ConfiguredWireProfile,
        canonical_model_id: impl Into<String>,
        upstream_model_id: impl Into<String>,
    ) -> Self {
        let profile_flags =
            WireProfileFlags::for_surfaces(client_surface, selected_profile.definition.surface);
        Self {
            provider_id: None,
            provider_kind: None,
            client_surface,
            selected_profile,
            canonical_model_id: canonical_model_id.into(),
            upstream_model_id: upstream_model_id.into(),
            adaptation_policy: AdaptationPolicy::default(),
            profile_flags,
            max_request_body_bytes: DEFAULT_MAX_REQUEST_BODY_BYTES,
            max_provider_body_bytes: DEFAULT_MAX_PROVIDER_BODY_BYTES,
            max_encoded_body_bytes: DEFAULT_MAX_REQUEST_BODY_BYTES,
        }
    }
}

impl fmt::Debug for WireRuntimeContext {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("WireRuntimeContext")
            .field("provider_id", &self.provider_id)
            .field("provider_kind", &self.provider_kind)
            .field("client_surface", &self.client_surface)
            .field(
                "selected_profile",
                &self.selected_profile.definition.surface,
            )
            .field("canonical_model_id_bytes", &self.canonical_model_id.len())
            .field("upstream_model_id_bytes", &self.upstream_model_id.len())
            .field("adaptation_policy", &self.adaptation_policy)
            .field("profile_flags", &self.profile_flags)
            .field("max_request_body_bytes", &self.max_request_body_bytes)
            .field("max_provider_body_bytes", &self.max_provider_body_bytes)
            .field("max_encoded_body_bytes", &self.max_encoded_body_bytes)
            .finish()
    }
}

/// Stable identity attached to every result from the selected-profile facade.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WireRuntimeIdentity {
    pub provider_id: Option<String>,
    pub provider_kind: Option<String>,
    pub client_surface: ClientSurface,
    pub profile: WireSurface,
    pub canonical_model_id: String,
    pub upstream_model_id: String,
}

impl WireRuntimeIdentity {
    fn from_context(context: &WireRuntimeContext) -> Self {
        Self {
            provider_id: context.provider_id.clone(),
            provider_kind: context.provider_kind.clone(),
            client_surface: context.client_surface,
            profile: context.selected_profile.definition.surface,
            canonical_model_id: context.canonical_model_id.clone(),
            upstream_model_id: context.upstream_model_id.clone(),
        }
    }
}

/// Structural content facts safe for default diagnostics.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct SemanticContentMetadata {
    pub message_count: usize,
    pub block_count: usize,
    pub text_block_count: usize,
    pub reasoning_block_count: usize,
    pub tool_block_count: usize,
    pub media_block_count: usize,
    pub has_structured_output: bool,
}

impl SemanticContentMetadata {
    fn request(request: &CanonicalRequest) -> Self {
        let mut metadata = Self {
            message_count: request.messages.len(),
            has_structured_output: request.response_format.is_some(),
            ..Self::default()
        };
        for message in &request.messages {
            for block in &message.content {
                metadata.block_count += 1;
                match block.kind {
                    CanonicalBlockKind::Text | CanonicalBlockKind::Refusal => {
                        metadata.text_block_count += 1;
                    }
                    CanonicalBlockKind::Reasoning => metadata.reasoning_block_count += 1,
                    CanonicalBlockKind::ToolCall | CanonicalBlockKind::ToolResult => {
                        metadata.tool_block_count += 1
                    }
                    CanonicalBlockKind::Image
                    | CanonicalBlockKind::Document
                    | CanonicalBlockKind::Audio => metadata.media_block_count += 1,
                }
            }
        }
        metadata.tool_block_count = metadata
            .tool_block_count
            .saturating_add(request.tools.len());
        metadata
    }

    fn response(response: &CanonicalResponse) -> Self {
        let mut metadata = Self::default();
        for block in &response.output {
            metadata.block_count += 1;
            match block.kind {
                CanonicalBlockKind::Text | CanonicalBlockKind::Refusal => {
                    metadata.text_block_count += 1;
                }
                CanonicalBlockKind::Reasoning => metadata.reasoning_block_count += 1,
                CanonicalBlockKind::ToolCall | CanonicalBlockKind::ToolResult => {
                    metadata.tool_block_count += 1;
                }
                CanonicalBlockKind::Image
                | CanonicalBlockKind::Document
                | CanonicalBlockKind::Audio => metadata.media_block_count += 1,
            }
        }
        metadata
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AdaptationKind {
    Exact,
    Adapted,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct AdaptationSummary {
    pub kind: AdaptationKind,
    pub warning_count: usize,
}

impl AdaptationSummary {
    fn from_notices(notices: &[AdaptationNotice]) -> Self {
        Self {
            kind: if notices.is_empty() {
                AdaptationKind::Exact
            } else {
                AdaptationKind::Adapted
            },
            warning_count: notices.len(),
        }
    }
}

/// Byte accounting for one finite or incremental operation.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct WireByteFacts {
    pub input_bytes: usize,
    pub output_bytes: usize,
    pub bytes_observed: usize,
}

/// JSON body with a redacted debug representation.
#[derive(Clone, PartialEq)]
pub struct EncodedWireBody {
    pub value: Option<Value>,
    pub bytes: Bytes,
}

impl fmt::Debug for EncodedWireBody {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("EncodedWireBody")
            .field("value_present", &self.value.is_some())
            .field("bytes", &self.bytes.len())
            .finish()
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct StreamIntent {
    pub requested: bool,
    pub adapter: StreamAdapterKind,
}

/// The fully admitted request and its selected-profile provider body.
#[derive(Debug, Clone, PartialEq)]
pub struct PreparedRequest {
    pub identity: WireRuntimeIdentity,
    pub admission: AdmittedRequest,
    pub canonical: CanonicalRequest,
    pub body: EncodedWireBody,
    pub metadata: SemanticContentMetadata,
    pub adaptation: AdaptationSummary,
    pub notices: Vec<AdaptationNotice>,
    pub bytes: WireByteFacts,
    pub stream: StreamIntent,
}

#[derive(Debug, Clone, PartialEq)]
pub enum FiniteResponseOutcome {
    Success(Box<CanonicalResponse>),
    ProviderError(ProviderErrorEvidence),
    Malformed { error: CodecError },
}

/// Typed finite upstream result; M7 decides status/retry/finalization policy.
#[derive(Debug, Clone, PartialEq)]
pub struct FiniteResponse {
    pub identity: WireRuntimeIdentity,
    pub outcome: FiniteResponseOutcome,
    pub metadata: Option<SemanticContentMetadata>,
    pub usage: Option<CanonicalUsage>,
    pub adaptation: AdaptationSummary,
    pub notices: Vec<AdaptationNotice>,
    pub client_body: Option<EncodedWireBody>,
    pub bytes: WireByteFacts,
}

#[derive(Debug, Error)]
pub enum ProfileMismatchReason {
    #[error("selected profile is not present in the immutable registry")]
    NotRegistered,
    #[error("selected profile definition differs from the immutable registry")]
    DefinitionChanged,
    #[error("selected profile request codec is unavailable")]
    RequestCodecUnavailable,
    #[error("selected profile response codec is unavailable")]
    ResponseCodecUnavailable,
    #[error("selected profile stream codec is invalid")]
    StreamCodecUnavailable,
    #[error("context field is empty or exceeds its bound")]
    InvalidContext,
    #[error("admitted request model does not match selected canonical model")]
    CanonicalModelMismatch,
    #[error("streaming was requested for a profile without streaming support")]
    StreamingUnavailable,
}

#[derive(Debug, Error)]
pub enum WireRuntimeError {
    #[error("client admission failed: {0:?}")]
    ClientAdmission(CodecError),
    #[error("selected wire profile mismatch for {profile:?}: {reason}")]
    ProfileMismatch {
        profile: WireSurface,
        reason: ProfileMismatchReason,
    },
    #[error("request adaptation failed: {0:?}")]
    RequestAdaptation(CodecError),
    #[error("client response adaptation failed: {0:?}")]
    ResponseAdaptation(CodecError),
    #[error("encoded wire body could not be serialized")]
    BodySerialization,
    #[error("wire body exceeded its configured bound")]
    BodyTooLarge,
    #[error("stream runtime failed: {0}")]
    Stream(#[from] StreamError),
}

/// Immutable selected-profile facade shared by future runtime generations.
#[derive(Debug, Clone)]
pub struct WireRuntime {
    registry: Arc<WireProfileRegistry>,
}

impl WireRuntime {
    pub fn new(registry: WireProfileRegistry) -> Self {
        Self {
            registry: Arc::new(registry),
        }
    }

    pub fn with_registry(registry: Arc<WireProfileRegistry>) -> Self {
        Self { registry }
    }

    pub fn embedded() -> Result<Self, super::WireRegistryError> {
        Ok(Self::new(WireProfileRegistry::embedded()?))
    }

    pub fn registry(&self) -> &WireProfileRegistry {
        &self.registry
    }

    pub fn prepare_request(
        &self,
        raw_body: &[u8],
        context: &WireRuntimeContext,
    ) -> Result<PreparedRequest, WireRuntimeError> {
        self.validate_context(context)?;
        let admission = admit_request(
            raw_body,
            AdmissionOptions {
                max_body_bytes: context.max_request_body_bytes,
                client_surface: context.client_surface,
                ..AdmissionOptions::default()
            },
        )
        .map_err(|error| WireRuntimeError::ClientAdmission(map_admission_error(error, context)))?;
        if admission.canonical.model != context.canonical_model_id {
            return Err(self.profile_error(context, ProfileMismatchReason::CanonicalModelMismatch));
        }
        if admission.canonical.stream && !context.profile_flags.supports_streaming {
            return Err(self.profile_error(context, ProfileMismatchReason::StreamingUnavailable));
        }

        let codec = self.request_codec(context)?;
        let request = if context.upstream_model_id == admission.canonical.model {
            admission.canonical.clone()
        } else {
            let mut request = admission.canonical.clone();
            request.model.clone_from(&context.upstream_model_id);
            request
        };
        let native_passthrough = context.profile_flags.body_passthrough
            && compatibility_path(
                context.client_surface,
                context.selected_profile.definition.surface,
            ) == super::CompatibilityPath::Native
            && context.upstream_model_id == admission.canonical.model;
        let (body, notices) = if native_passthrough {
            (
                EncodedWireBody {
                    value: None,
                    bytes: Bytes::copy_from_slice(raw_body),
                },
                Vec::new(),
            )
        } else {
            let output = codec
                .encode_request_with_policy(
                    &request,
                    &context.selected_profile,
                    &context.adaptation_policy,
                )
                .map_err(WireRuntimeError::RequestAdaptation)?;
            let value = output.value;
            let encoded = encode_compact_json_bounded(&value, context.max_encoded_body_bytes)
                .map_err(|error| match error {
                    crate::request::BodyEncodingError::TooLarge { .. } => {
                        WireRuntimeError::BodyTooLarge
                    }
                    crate::request::BodyEncodingError::Serialize(_) => {
                        WireRuntimeError::BodySerialization
                    }
                })?;
            (
                EncodedWireBody {
                    value: Some(value),
                    bytes: encoded.bytes,
                },
                output.notices,
            )
        };
        let adapter = stream_adapter(context.selected_profile.definition.stream_codec)
            .map_err(|reason| self.profile_error(context, reason))?;
        let identity = WireRuntimeIdentity::from_context(context);
        let metadata = SemanticContentMetadata::request(&admission.canonical);
        Ok(PreparedRequest {
            identity,
            canonical: admission.canonical.clone(),
            metadata,
            adaptation: AdaptationSummary::from_notices(&notices),
            notices,
            bytes: WireByteFacts {
                input_bytes: raw_body.len(),
                output_bytes: body.bytes.len(),
                bytes_observed: raw_body.len(),
            },
            stream: StreamIntent {
                requested: admission.canonical.stream,
                adapter,
            },
            admission,
            body,
        })
    }

    pub fn routing_facts(
        &self,
        admitted: &AdmittedRequest,
        inputs: &StaticRoutingFacts,
    ) -> RoutingRequestFacts {
        admitted.routing_facts(inputs)
    }

    pub fn affinity_identity(
        &self,
        request: &CanonicalRequest,
        explicit_session: Option<&str>,
    ) -> AffinityIdentityInput {
        affinity_identity_input(request, explicit_session)
    }

    pub fn decode_finite_response(
        &self,
        body: &[u8],
        status: u16,
        context: &WireRuntimeContext,
        encode_client: bool,
    ) -> Result<FiniteResponse, WireRuntimeError> {
        self.validate_context(context)?;
        if body.len() > context.max_provider_body_bytes {
            return Err(WireRuntimeError::BodyTooLarge);
        }
        let identity = WireRuntimeIdentity::from_context(context);
        let codec = self.response_codec(context)?;
        let value: Value = match serde_json::from_slice(body) {
            Ok(value) => value,
            Err(_) => {
                let error = provider_malformed_error(context.selected_profile.definition.surface);
                return Ok(FiniteResponse {
                    identity,
                    outcome: FiniteResponseOutcome::Malformed { error },
                    metadata: None,
                    usage: None,
                    adaptation: AdaptationSummary::from_notices(&[]),
                    notices: Vec::new(),
                    client_body: None,
                    bytes: WireByteFacts {
                        input_bytes: body.len(),
                        output_bytes: 0,
                        bytes_observed: body.len(),
                    },
                });
            }
        };
        let decoded = match codec.decode_response(&value, status) {
            Ok(output) => output,
            Err(error) => {
                return Ok(FiniteResponse {
                    identity,
                    outcome: FiniteResponseOutcome::Malformed { error },
                    metadata: None,
                    usage: None,
                    adaptation: AdaptationSummary::from_notices(&[]),
                    notices: Vec::new(),
                    client_body: None,
                    bytes: WireByteFacts {
                        input_bytes: body.len(),
                        output_bytes: 0,
                        bytes_observed: body.len(),
                    },
                });
            }
        };
        let notices = decoded.notices;
        match decoded.value {
            DecodedProviderPayload::Error(error) => Ok(FiniteResponse {
                identity,
                outcome: FiniteResponseOutcome::ProviderError(error),
                metadata: None,
                usage: None,
                adaptation: AdaptationSummary::from_notices(&notices),
                notices,
                client_body: None,
                bytes: WireByteFacts {
                    input_bytes: body.len(),
                    output_bytes: 0,
                    bytes_observed: body.len(),
                },
            }),
            DecodedProviderPayload::Response(response) => {
                let metadata = SemanticContentMetadata::response(&response);
                let usage = response.usage.clone();
                let mut all_notices = notices;
                let client_body = if encode_client {
                    let encoded = if context.profile_flags.body_passthrough
                        && compatibility_path(
                            context.client_surface,
                            context.selected_profile.definition.surface,
                        ) == super::CompatibilityPath::Native
                    {
                        EncodedWireBody {
                            value: Some(value),
                            bytes: Bytes::copy_from_slice(body),
                        }
                    } else {
                        let output = codec
                            .encode_response_with_policy(
                                &response,
                                context.client_surface,
                                &context.adaptation_policy,
                            )
                            .map_err(WireRuntimeError::ResponseAdaptation)?;
                        all_notices.extend(output.notices);
                        let encoded = encode_compact_json_bounded(
                            &output.value,
                            context.max_encoded_body_bytes,
                        )
                        .map_err(|error| match error {
                            crate::request::BodyEncodingError::TooLarge { .. } => {
                                WireRuntimeError::BodyTooLarge
                            }
                            crate::request::BodyEncodingError::Serialize(_) => {
                                WireRuntimeError::BodySerialization
                            }
                        })?;
                        EncodedWireBody {
                            value: Some(output.value),
                            bytes: encoded.bytes,
                        }
                    };
                    Some(encoded)
                } else {
                    None
                };
                Ok(FiniteResponse {
                    identity,
                    outcome: FiniteResponseOutcome::Success(response),
                    metadata: Some(metadata),
                    usage,
                    adaptation: AdaptationSummary::from_notices(&all_notices),
                    notices: all_notices,
                    bytes: WireByteFacts {
                        input_bytes: body.len(),
                        output_bytes: client_body.as_ref().map_or(0, |body| body.bytes.len()),
                        bytes_observed: body.len(),
                    },
                    client_body,
                })
            }
        }
    }

    pub fn encode_client_response(
        &self,
        response: &CanonicalResponse,
        context: &WireRuntimeContext,
    ) -> Result<EncodedWireBody, WireRuntimeError> {
        self.validate_context(context)?;
        let codec = self.response_codec(context)?;
        let output = codec
            .encode_response_with_policy(
                response,
                context.client_surface,
                &context.adaptation_policy,
            )
            .map_err(WireRuntimeError::ResponseAdaptation)?;
        let encoded = encode_compact_json_bounded(&output.value, context.max_encoded_body_bytes)
            .map_err(|error| match error {
                crate::request::BodyEncodingError::TooLarge { .. } => {
                    WireRuntimeError::BodyTooLarge
                }
                crate::request::BodyEncodingError::Serialize(_) => {
                    WireRuntimeError::BodySerialization
                }
            })?;
        Ok(EncodedWireBody {
            value: Some(output.value),
            bytes: encoded.bytes,
        })
    }

    pub fn stream(&self, context: &WireRuntimeContext) -> Result<WireStream, WireRuntimeError> {
        self.validate_context(context)?;
        if !context.profile_flags.supports_streaming {
            return Err(self.profile_error(context, ProfileMismatchReason::StreamingUnavailable));
        }
        let adapter = stream_adapter(context.selected_profile.definition.stream_codec)
            .map_err(|reason| self.profile_error(context, reason))?;
        Ok(WireStream {
            identity: WireRuntimeIdentity::from_context(context),
            client_surface: context.client_surface,
            adapter,
            decoder: StreamEventDecoder::new(adapter),
            bytes_observed: 0,
        })
    }

    fn request_codec(
        &self,
        context: &WireRuntimeContext,
    ) -> Result<Box<dyn WireCodec>, WireRuntimeError> {
        builtin_codec_instance(context.selected_profile.definition.request_codec).ok_or_else(|| {
            self.profile_error(context, ProfileMismatchReason::RequestCodecUnavailable)
        })
    }

    fn response_codec(
        &self,
        context: &WireRuntimeContext,
    ) -> Result<Box<dyn WireCodec>, WireRuntimeError> {
        builtin_codec_instance(context.selected_profile.definition.response_codec).ok_or_else(
            || self.profile_error(context, ProfileMismatchReason::ResponseCodecUnavailable),
        )
    }

    fn profile_error(
        &self,
        context: &WireRuntimeContext,
        reason: ProfileMismatchReason,
    ) -> WireRuntimeError {
        WireRuntimeError::ProfileMismatch {
            profile: context.selected_profile.definition.surface,
            reason,
        }
    }

    fn validate_context(&self, context: &WireRuntimeContext) -> Result<(), WireRuntimeError> {
        let profile = &context.selected_profile;
        let surface = profile.definition.surface;
        let Some(registered) = self.registry.get(surface) else {
            return Err(self.profile_error(context, ProfileMismatchReason::NotRegistered));
        };
        if registered != &profile.definition {
            return Err(self.profile_error(context, ProfileMismatchReason::DefinitionChanged));
        }
        if context.canonical_model_id.trim().is_empty()
            || context.canonical_model_id.len() > MAX_CONTEXT_FIELD_BYTES
            || context.upstream_model_id.trim().is_empty()
            || context.upstream_model_id.len() > MAX_CONTEXT_FIELD_BYTES
            || profile.path_template.len() > MAX_CONTEXT_FIELD_BYTES
            || profile
                .stream_path_template
                .as_ref()
                .is_some_and(|path| path.len() > MAX_CONTEXT_FIELD_BYTES)
            || context
                .provider_id
                .as_ref()
                .is_some_and(|value| value.is_empty() || value.len() > MAX_CONTEXT_FIELD_BYTES)
            || context
                .provider_kind
                .as_ref()
                .is_some_and(|value| value.is_empty() || value.len() > MAX_PROVIDER_KIND_BYTES)
            || context.max_request_body_bytes == 0
            || context.max_provider_body_bytes == 0
            || context.max_encoded_body_bytes == 0
        {
            return Err(self.profile_error(context, ProfileMismatchReason::InvalidContext));
        }
        Ok(())
    }
}

/// Independent per-attempt stream state with no socket or downstream ownership.
pub struct WireStream {
    pub identity: WireRuntimeIdentity,
    pub adapter: StreamAdapterKind,
    client_surface: ClientSurface,
    decoder: StreamEventDecoder,
    bytes_observed: usize,
}

impl fmt::Debug for WireStream {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("WireStream")
            .field("identity", &self.identity)
            .field("adapter", &self.adapter)
            .field("client_surface", &self.client_surface)
            .field("decoder", &self.decoder)
            .finish()
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct StreamPushResult {
    pub events: Vec<CanonicalEvent>,
    pub bytes: WireByteFacts,
}

#[derive(Debug, Clone, PartialEq)]
pub struct StreamFinalization {
    pub identity: WireRuntimeIdentity,
    pub events: Vec<CanonicalEvent>,
    pub terminal: StreamTerminalSummary,
    pub usage: Option<CanonicalUsage>,
    pub bytes: WireByteFacts,
}

impl WireStream {
    pub fn push(&mut self, bytes: &[u8]) -> Result<StreamPushResult, WireRuntimeError> {
        self.bytes_observed = self.bytes_observed.saturating_add(bytes.len());
        let events = self.decoder.push(bytes)?;
        Ok(StreamPushResult {
            events,
            bytes: WireByteFacts {
                input_bytes: bytes.len(),
                output_bytes: 0,
                bytes_observed: self.bytes_observed,
            },
        })
    }

    pub fn finalize(&mut self) -> Result<StreamFinalization, WireRuntimeError> {
        let (events, terminal) = self.decoder.finalize_events()?;
        Ok(StreamFinalization {
            identity: self.identity.clone(),
            events,
            usage: terminal.usage.clone(),
            bytes: WireByteFacts {
                input_bytes: 0,
                output_bytes: 0,
                bytes_observed: terminal.bytes_observed,
            },
            terminal,
        })
    }

    pub fn usage(&self) -> Option<CanonicalUsage> {
        self.decoder.usage()
    }

    pub fn encode_client_event(&self, event: &CanonicalEvent) -> Result<Bytes, WireRuntimeError> {
        encode_client_event(self.client_surface, event)
            .map(Bytes::from)
            .map_err(WireRuntimeError::ResponseAdaptation)
    }
}

fn stream_adapter(codec: WireCodecId) -> Result<StreamAdapterKind, ProfileMismatchReason> {
    match codec {
        WireCodecId::OpenaiChatSse => Ok(StreamAdapterKind::OpenaiChatSse),
        WireCodecId::OpenaiResponsesSse => Ok(StreamAdapterKind::OpenaiResponsesSse),
        WireCodecId::AnthropicMessagesSse => Ok(StreamAdapterKind::AnthropicMessagesSse),
        WireCodecId::GeminiInteractionsSse => Ok(StreamAdapterKind::GeminiInteractionsSse),
        WireCodecId::GeminiGenerateContentSse => Ok(StreamAdapterKind::GeminiGenerateContentSse),
        WireCodecId::OpenaiChat
        | WireCodecId::OpenaiResponses
        | WireCodecId::AnthropicMessages
        | WireCodecId::GeminiInteractions
        | WireCodecId::GeminiGenerateContent => Err(ProfileMismatchReason::StreamCodecUnavailable),
    }
}

fn provider_malformed_error(surface: WireSurface) -> CodecError {
    CodecError {
        reason: CodecReasonCode::MalformedProviderResponse,
        field: None,
        source_surface: Some(surface),
        target_surface: None,
    }
}

fn map_admission_error(error: AdmissionError, context: &WireRuntimeContext) -> CodecError {
    let (reason, field) = match error {
        AdmissionError::BodyTooLarge { .. }
        | AdmissionError::CollectionLimit { .. }
        | AdmissionError::DepthLimit
        | AdmissionError::MediaLimit { .. }
        | AdmissionError::InvalidLimit { .. }
        | AdmissionError::LengthOverflow => (CodecReasonCode::ResourceLimitViolation, None),
        AdmissionError::UnsupportedContent { .. } => (
            CodecReasonCode::UnsupportedSemanticFeature,
            Some("content".into()),
        ),
        AdmissionError::InvalidField { field } => {
            (CodecReasonCode::MalformedSourceRequest, Some(field.into()))
        }
        AdmissionError::InvalidJson
        | AdmissionError::TopLevelNotObject
        | AdmissionError::InvalidModel => (CodecReasonCode::MalformedSourceRequest, None),
    };
    CodecError {
        reason,
        field,
        source_surface: Some(match context.client_surface {
            ClientSurface::ChatCompletions => WireSurface::OpenaiChatCompletions,
            ClientSurface::Responses => WireSurface::OpenaiResponses,
            ClientSurface::Messages => WireSurface::AnthropicMessages,
        }),
        target_surface: None,
    }
}
