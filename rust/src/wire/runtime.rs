//! Caller-selected canonical wire runtime facade.
//!
//! This is the registry/runtime handoff. The registry is immutable and shareable; all
//! request and stream state is owned by the value returned for that operation.
//! No method in this module selects a profile, sends a request, retries, or
//! owns response handoff/finalization.

use std::{fmt, sync::Arc};

use bytes::Bytes;
use serde_json::Value;
use thiserror::Error;

use super::ir::{
    CanonicalBlockKind, CanonicalEvent, CanonicalRequest, CanonicalResponse, CanonicalToolKind,
    CanonicalUsage, ClientSurface, ProviderErrorEvidence,
};
use super::{
    AdaptationNotice, AdaptationPolicy, ClientStreamEncoder, CodecError, CodecReasonCode,
    ConfiguredWireProfile, DecodedProviderPayload, StreamAdapterKind, StreamError,
    StreamEventDecoder, StreamForwardingMode, StreamTerminalSummary, WireCodec, WireCodecId,
    WireProfileRegistry, WireSurface, apply_adaptation_policy, builtin_codec_instance,
    compatibility_path, encode_client_event, native_preservation_notices,
};
use crate::model_router::AffinityIdentityInput;
use crate::request::{
    AdmissionError, AdmissionOptions, AdmittedRequest, CompactAdmittedRequest,
    DEFAULT_MAX_REQUEST_BODY_BYTES, StaticRoutingFacts, admit_request, affinity_identity_input,
    encode_compact_json_bounded, has_compaction_trigger,
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
    pub stream_native_passthrough: bool,
}

impl WireProfileFlags {
    pub fn for_surfaces(client: ClientSurface, upstream: WireSurface) -> Self {
        let native = compatibility_path(client, upstream) == super::CompatibilityPath::Native;
        Self {
            supports_streaming: true,
            body_passthrough: native,
            stream_native_passthrough: native
                && client == ClientSurface::Responses
                && upstream == WireSurface::OpenaiResponses,
        }
    }
}

impl Default for WireProfileFlags {
    fn default() -> Self {
        Self {
            supports_streaming: true,
            body_passthrough: false,
            stream_native_passthrough: false,
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
    /// Remote-compaction capabilities for the selected provider surface.
    /// Defaults to unsupported so current custom providers keep the
    /// local-compaction contract until an operator opts in.
    pub compaction: super::CompactionCapabilities,
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
            compaction: super::CompactionCapabilities::default(),
        }
    }

    /// Attach remote-compaction capabilities resolved from the provider-owned
    /// `wire_surfaces` table for the selected surface.
    pub fn with_compaction(mut self, compaction: super::CompactionCapabilities) -> Self {
        self.compaction = compaction;
        self
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
            .field(
                "supports_compaction_v1",
                &self.compaction.supports_remote_compaction_v1,
            )
            .field(
                "supports_compaction_v2",
                &self.compaction.supports_remote_compaction_v2,
            )
            .field(
                "compact_path_present",
                &self.compaction.compact_path_template.is_some(),
            )
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

/// Compact-operation outcome. A semantic validation failure is a malformed
/// result, never a successful provider response.
#[derive(Debug, Clone, PartialEq)]
pub enum CompactResponseOutcome {
    Success,
    ProviderError(ProviderErrorEvidence),
    Malformed { error: CodecError },
}

/// Typed native compact upstream result. The successful client body carries
/// the validated replacement-history payload unchanged; it is never
/// canonicalized into an ordinary assistant completion.
#[derive(Debug, Clone, PartialEq)]
pub struct CompactResponse {
    pub identity: WireRuntimeIdentity,
    pub outcome: CompactResponseOutcome,
    pub metadata: Option<SemanticContentMetadata>,
    pub usage: Option<CanonicalUsage>,
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
        self.prepare_admitted_request(admission, raw_body, context)
    }

    /// Complete request preparation from an admission result that has already
    /// parsed and validated the client body.  The coordinator uses this bridge
    /// to preserve the one-parse M6 contract while deriving M5 routing facts.
    pub fn prepare_admitted_request(
        &self,
        admission: AdmittedRequest,
        raw_body: &[u8],
        context: &WireRuntimeContext,
    ) -> Result<PreparedRequest, WireRuntimeError> {
        self.validate_context(context)?;
        if admission.canonical.model != context.canonical_model_id {
            return Err(self.profile_error(context, ProfileMismatchReason::CanonicalModelMismatch));
        }
        if admission.canonical.stream && !context.profile_flags.supports_streaming {
            return Err(self.profile_error(context, ProfileMismatchReason::StreamingUnavailable));
        }
        // Current v2 `compaction_trigger` items over the normal Responses
        // endpoint require explicit native v2 capability. The trigger is a
        // native-only compaction signal: it is never treated as user text,
        // never passed through a codec that cannot represent it, and never
        // advertised until the complete path is qualified. The default
        // (unsupported) fails before provider submission.
        if context.client_surface == ClientSurface::Responses
            && let Some(preservation) = admission.native_preservation.as_ref()
            && let Some(object) = preservation.parsed.as_object()
            && has_compaction_trigger(object)
        {
            let native_v2 = context.selected_profile.definition.surface
                == WireSurface::OpenaiResponses
                && context.compaction.supports_remote_compaction_v2;
            if !native_v2 {
                return Err(WireRuntimeError::RequestAdaptation(CodecError {
                    reason: CodecReasonCode::UnsupportedSemanticFeature,
                    field: Some("input.compaction_trigger".into()),
                    source_surface: Some(WireSurface::OpenaiResponses),
                    target_surface: Some(context.selected_profile.definition.surface),
                }));
            }
        }

        let codec = self.request_codec(context)?;
        let model_rewrite_required = context.upstream_model_id != admission.canonical.model;
        let request = if !model_rewrite_required {
            admission.canonical.clone()
        } else {
            let mut request = admission.canonical.clone();
            request.model.clone_from(&context.upstream_model_id);
            request
        };
        let native_path = context.profile_flags.body_passthrough
            && compatibility_path(
                context.client_surface,
                context.selected_profile.definition.surface,
            ) == super::CompatibilityPath::Native;
        let native_responses = native_path
            && context.client_surface == ClientSurface::Responses
            && context.selected_profile.definition.surface == WireSurface::OpenaiResponses;
        let (body, notices) = if native_responses {
            let preservation = admission.native_preservation.as_ref().ok_or_else(|| {
                WireRuntimeError::RequestAdaptation(CodecError {
                    reason: CodecReasonCode::UnsupportedSemanticFeature,
                    field: Some("responses.native_preservation".into()),
                    source_surface: Some(WireSurface::OpenaiResponses),
                    target_surface: Some(WireSurface::OpenaiResponses),
                })
            })?;
            if !model_rewrite_required {
                (
                    EncodedWireBody {
                        value: None,
                        bytes: Bytes::copy_from_slice(raw_body),
                    },
                    Vec::new(),
                )
            } else {
                let mut value = preservation.parsed.clone();
                value
                    .as_object_mut()
                    .expect("admission only retains an object")
                    .insert(
                        "model".into(),
                        Value::String(context.upstream_model_id.clone()),
                    );
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
                    Vec::new(),
                )
            }
        } else if native_path && !model_rewrite_required {
            (
                EncodedWireBody {
                    value: None,
                    bytes: Bytes::copy_from_slice(raw_body),
                },
                Vec::new(),
            )
        } else {
            let mut notices = if let Some(preservation) = admission.native_preservation.as_ref() {
                native_preservation_notices(
                    preservation,
                    context.selected_profile.definition.surface,
                )
                .map_err(WireRuntimeError::RequestAdaptation)?
            } else {
                Vec::new()
            };
            let output = codec
                .encode_request(&request, &context.selected_profile)
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
            notices.extend(output.notices);
            let notices = apply_adaptation_policy(
                crate::wire::CodecOutput { value, notices },
                &context.adaptation_policy,
            )
            .map_err(WireRuntimeError::RequestAdaptation)?;
            (
                EncodedWireBody {
                    value: Some(notices.value),
                    bytes: encoded.bytes,
                },
                notices.notices,
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

    /// Prepare one remote-compaction request for a natively compact-capable
    /// upstream surface.
    ///
    /// Compaction is a distinct model-facing operation whose output replaces
    /// retained history; it is not an ordinary assistant completion. Only
    /// native same-surface forwarding is supported: the source-native compact
    /// JSON is preserved exactly except for the EggPool-owned model rewrite
    /// and dispatch-time auth. There is deliberately no translated fallback —
    /// unsupported targets fail here, before provider submission, rather than
    /// risk a lossy replacement history. The result is validated and bounded
    /// by [`Self::decode_compact_response`] without canonicalizing it into an
    /// ordinary completion.
    pub fn prepare_compact_request(
        &self,
        admission: CompactAdmittedRequest,
        raw_body: &[u8],
        context: &WireRuntimeContext,
    ) -> Result<PreparedRequest, WireRuntimeError> {
        self.validate_context(context)?;
        if context.client_surface != ClientSurface::Responses {
            return Err(WireRuntimeError::RequestAdaptation(CodecError {
                reason: CodecReasonCode::UnsupportedSemanticFeature,
                field: Some("compact.client_surface".into()),
                source_surface: Some(WireSurface::OpenaiResponses),
                target_surface: Some(context.selected_profile.definition.surface),
            }));
        }
        if context.selected_profile.definition.surface != WireSurface::OpenaiResponses {
            return Err(WireRuntimeError::RequestAdaptation(CodecError {
                reason: CodecReasonCode::UnsupportedSemanticFeature,
                field: Some("compact.upstream_surface".into()),
                source_surface: Some(WireSurface::OpenaiResponses),
                target_surface: Some(context.selected_profile.definition.surface),
            }));
        }
        if !context.compaction.native_v1_supported() {
            return Err(WireRuntimeError::RequestAdaptation(CodecError {
                reason: CodecReasonCode::UnsupportedSemanticFeature,
                field: Some("compact.remote_compaction_v1".into()),
                source_surface: Some(WireSurface::OpenaiResponses),
                target_surface: Some(context.selected_profile.definition.surface),
            }));
        }
        if admission.canonical.model != context.canonical_model_id {
            return Err(self.profile_error(context, ProfileMismatchReason::CanonicalModelMismatch));
        }
        let model_rewrite_required = context.upstream_model_id != admission.canonical.model;
        let body = if !model_rewrite_required {
            EncodedWireBody {
                value: None,
                bytes: Bytes::copy_from_slice(raw_body),
            }
        } else {
            let mut value = admission.native_preservation.parsed.clone();
            value
                .as_object_mut()
                .expect("compact admission only retains an object")
                .insert(
                    "model".into(),
                    Value::String(context.upstream_model_id.clone()),
                );
            let encoded = encode_compact_json_bounded(&value, context.max_encoded_body_bytes)
                .map_err(|error| match error {
                    crate::request::BodyEncodingError::TooLarge { .. } => {
                        WireRuntimeError::BodyTooLarge
                    }
                    crate::request::BodyEncodingError::Serialize(_) => {
                        WireRuntimeError::BodySerialization
                    }
                })?;
            EncodedWireBody {
                value: Some(value),
                bytes: encoded.bytes,
            }
        };
        let adapter = stream_adapter(context.selected_profile.definition.stream_codec)
            .map_err(|reason| self.profile_error(context, reason))?;
        let identity = WireRuntimeIdentity::from_context(context);
        let metadata = SemanticContentMetadata::request(&admission.canonical);
        let admitted = AdmittedRequest {
            canonical: admission.canonical.clone(),
            native_preservation: Some(admission.native_preservation.clone()),
            raw_body_bytes: admission.raw_body_bytes,
            reservation_tokens: admission.reservation_tokens,
            context_tokens: admission.context_tokens,
        };
        Ok(PreparedRequest {
            identity,
            canonical: admission.canonical.clone(),
            metadata,
            adaptation: AdaptationSummary::from_notices(&[]),
            notices: Vec::new(),
            bytes: WireByteFacts {
                input_bytes: raw_body.len(),
                output_bytes: body.bytes.len(),
                bytes_observed: raw_body.len(),
            },
            stream: StreamIntent {
                requested: false,
                adapter,
            },
            admission: admitted,
            body,
        })
    }

    /// Validate and bound one native compact result without canonicalizing it
    /// into an ordinary assistant completion.
    ///
    /// A 2xx body must be a bounded JSON object (the replacement-history
    /// material); it is returned to the client unchanged. Usage is extracted
    /// opportunistically from the standard Responses usage shape when present
    /// so successful compaction still records provider/model/account usage.
    /// Non-2xx bodies classify as provider errors through the selected
    /// surface codec. A semantic compact-result validation error is never
    /// treated as a successful provider response. No summary text,
    /// replacement history, credential, or raw body enters diagnostics beyond
    /// byte counts.
    pub fn decode_compact_response(
        &self,
        body: &[u8],
        status: u16,
        context: &WireRuntimeContext,
    ) -> Result<CompactResponse, WireRuntimeError> {
        self.validate_context(context)?;
        if body.len() > context.max_provider_body_bytes {
            return Err(WireRuntimeError::BodyTooLarge);
        }
        let identity = WireRuntimeIdentity::from_context(context);
        let bytes = WireByteFacts {
            input_bytes: body.len(),
            output_bytes: 0,
            bytes_observed: body.len(),
        };
        if !(200..300).contains(&status) {
            let evidence = self
                .compact_error_evidence(body, status, context)
                .unwrap_or(ProviderErrorEvidence {
                    status,
                    error_type: None,
                    message: None,
                });
            return Ok(CompactResponse {
                identity,
                outcome: CompactResponseOutcome::ProviderError(evidence),
                metadata: None,
                usage: None,
                client_body: None,
                bytes,
            });
        }
        let value: Value = match serde_json::from_slice(body) {
            Ok(value) => value,
            Err(_) => {
                return Ok(CompactResponse {
                    identity,
                    outcome: CompactResponseOutcome::Malformed {
                        error: provider_malformed_error(
                            context.selected_profile.definition.surface,
                        ),
                    },
                    metadata: None,
                    usage: None,
                    client_body: None,
                    bytes,
                });
            }
        };
        if !value.is_object() {
            return Ok(CompactResponse {
                identity,
                outcome: CompactResponseOutcome::Malformed {
                    error: provider_malformed_error(context.selected_profile.definition.surface),
                },
                metadata: None,
                usage: None,
                client_body: None,
                bytes,
            });
        }
        let usage = compact_usage(&value);
        let output_bytes = body.len();
        Ok(CompactResponse {
            identity,
            outcome: CompactResponseOutcome::Success,
            metadata: Some(SemanticContentMetadata::default()),
            usage,
            client_body: Some(EncodedWireBody {
                value: Some(value),
                bytes: Bytes::copy_from_slice(body),
            }),
            bytes: WireByteFacts {
                input_bytes: body.len(),
                output_bytes,
                bytes_observed: body.len(),
            },
        })
    }

    /// Decode structured provider-error evidence for a non-2xx compact body,
    /// returning `None` when the body carries no decodable error shape.
    fn compact_error_evidence(
        &self,
        body: &[u8],
        status: u16,
        context: &WireRuntimeContext,
    ) -> Option<ProviderErrorEvidence> {
        let value: Value = serde_json::from_slice(body).ok()?;
        let codec = self.response_codec(context).ok()?;
        match codec.decode_response(&value, status).ok()?.value {
            DecodedProviderPayload::Error(error) => Some(error),
            DecodedProviderPayload::Response(_) => None,
        }
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
        self.decode_finite_response_with_tools(body, status, context, encode_client, None)
    }

    pub fn decode_finite_response_for_request(
        &self,
        body: &[u8],
        status: u16,
        context: &WireRuntimeContext,
        encode_client: bool,
        request: &CanonicalRequest,
    ) -> Result<FiniteResponse, WireRuntimeError> {
        self.decode_finite_response_with_tools(
            body,
            status,
            context,
            encode_client,
            Some(&request.tools),
        )
    }

    fn decode_finite_response_with_tools(
        &self,
        body: &[u8],
        status: u16,
        context: &WireRuntimeContext,
        encode_client: bool,
        tools: Option<&[super::ir::CanonicalTool]>,
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
            DecodedProviderPayload::Response(mut response) => {
                if let Some(tools) = tools
                    && compatibility_path(
                        context.client_surface,
                        context.selected_profile.definition.surface,
                    ) == super::CompatibilityPath::CanonicalAdaptation
                {
                    classify_freeform_output(
                        &mut response,
                        tools,
                        context.selected_profile.definition.surface,
                    )?;
                }
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
                        let client_codec = self.client_response_codec(context)?;
                        let output = client_codec
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
        let codec = self.client_response_codec(context)?;
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
        self.stream_for_tools(context, &[])
    }

    pub fn stream_for_request(
        &self,
        context: &WireRuntimeContext,
        request: &CanonicalRequest,
    ) -> Result<WireStream, WireRuntimeError> {
        self.stream_for_tools(context, &request.tools)
    }

    fn stream_for_tools(
        &self,
        context: &WireRuntimeContext,
        tools: &[super::ir::CanonicalTool],
    ) -> Result<WireStream, WireRuntimeError> {
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
            mode: if context.profile_flags.stream_native_passthrough {
                StreamForwardingMode::NativeObserved
            } else {
                StreamForwardingMode::Translated
            },
            decoder: StreamEventDecoder::new(adapter),
            encoder: ClientStreamEncoder::new_with_tools(context.client_surface, tools),
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

    fn client_response_codec(
        &self,
        context: &WireRuntimeContext,
    ) -> Result<Box<dyn WireCodec>, WireRuntimeError> {
        let codec_id = match context.client_surface {
            ClientSurface::ChatCompletions => WireCodecId::OpenaiChat,
            ClientSurface::Responses => WireCodecId::OpenaiResponses,
            ClientSurface::Messages => WireCodecId::AnthropicMessages,
        };
        builtin_codec_instance(codec_id).ok_or_else(|| {
            self.profile_error(context, ProfileMismatchReason::ResponseCodecUnavailable)
        })
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
    pub mode: StreamForwardingMode,
    client_surface: ClientSurface,
    decoder: StreamEventDecoder,
    encoder: ClientStreamEncoder,
    bytes_observed: usize,
}

impl fmt::Debug for WireStream {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("WireStream")
            .field("identity", &self.identity)
            .field("adapter", &self.adapter)
            .field("mode", &self.mode)
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

    #[must_use]
    pub const fn forwarding_mode(&self) -> StreamForwardingMode {
        self.mode
    }

    /// Encode a translated event using the state owned by this stream. Native
    /// observed streams must forward their original bytes instead.
    pub fn encode_client_event_stateful(
        &mut self,
        event: &CanonicalEvent,
    ) -> Result<Bytes, WireRuntimeError> {
        self.encoder
            .encode(event)
            .map(Bytes::from)
            .map_err(WireRuntimeError::ResponseAdaptation)
    }

    /// Legacy stateless helper retained for codec qualification fixtures. The
    /// live coordinator uses [`Self::encode_client_event_stateful`].
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

fn classify_freeform_output(
    response: &mut CanonicalResponse,
    tools: &[super::ir::CanonicalTool],
    surface: WireSurface,
) -> Result<(), WireRuntimeError> {
    let freeform_names: std::collections::BTreeSet<&str> = tools
        .iter()
        .filter(|tool| tool.kind == CanonicalToolKind::Freeform)
        .map(|tool| tool.name.as_str())
        .collect();
    for block in &mut response.output {
        if block.kind != CanonicalBlockKind::ToolCall
            || !block
                .name
                .as_deref()
                .is_some_and(|name| freeform_names.contains(name))
        {
            continue;
        }
        let arguments = block.arguments.as_deref().unwrap_or_default();
        let parsed: Value = serde_json::from_str(arguments).map_err(|_| {
            WireRuntimeError::ResponseAdaptation(CodecError {
                reason: CodecReasonCode::MalformedProviderResponse,
                field: Some("tool_call.arguments.input".into()),
                source_surface: Some(surface),
                target_surface: Some(WireSurface::OpenaiResponses),
            })
        })?;
        let input = parsed
            .as_object()
            .filter(|object| object.len() == 1)
            .and_then(|object| object.get("input"))
            .and_then(Value::as_str)
            .ok_or_else(|| {
                WireRuntimeError::ResponseAdaptation(CodecError {
                    reason: CodecReasonCode::MalformedProviderResponse,
                    field: Some("tool_call.arguments.input".into()),
                    source_surface: Some(surface),
                    target_surface: Some(WireSurface::OpenaiResponses),
                })
            })?;
        block.tool_kind = CanonicalToolKind::Freeform;
        block.arguments = Some(input.to_owned());
    }
    Ok(())
}

fn provider_malformed_error(surface: WireSurface) -> CodecError {
    CodecError {
        reason: CodecReasonCode::MalformedProviderResponse,
        field: None,
        source_surface: Some(surface),
        target_surface: None,
    }
}

/// Extract standard Responses usage from a native compact result when the
/// upstream reports it, so successful compaction still records
/// provider/model/account usage. Returns `None` when no usage shape is
/// present rather than fabricating zero estimates.
fn compact_usage(value: &Value) -> Option<CanonicalUsage> {
    let usage = value.get("usage")?.as_object()?;
    let number = |key: &str| usage.get(key).and_then(Value::as_u64);
    let input = number("input_tokens");
    let output = number("output_tokens");
    let total = number("total_tokens");
    if input.is_none() && output.is_none() && total.is_none() {
        return None;
    }
    Some(CanonicalUsage {
        input_tokens: input,
        output_tokens: output,
        total_tokens: total,
        ..CanonicalUsage::default()
    })
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
        AdmissionError::StatefulResponsesFeature { field } => (
            CodecReasonCode::UnsupportedSemanticFeature,
            Some(field.into()),
        ),
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
