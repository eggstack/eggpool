//! Canonical, provider-independent wire semantics.
//!
//! The neutral implementation lives in the `eggpool-wire` workspace crate;
//! the kernel modules below are facades re-exporting that single source of
//! truth, while `adapters` and `runtime` remain EggPool-owned.

pub mod adaptation;
pub mod adapters;
pub mod additional_codecs;
pub mod codec;
pub mod codecs;
pub mod decode;
pub mod ir;
pub mod registry;
pub mod runtime;
pub mod stream;

pub use adaptation::{
    AdaptationOutcome, AdaptationPolicy, CapabilityDisposition, LossPolicy, MAX_ADAPTATION_NOTICES,
    NativeSummaryFacts, NeutralCapabilityStatus, NeutralThinkingCapability,
    ReasoningCapabilityPolicy, apply_adaptation_policy, native_summary_notices,
    reasoning_capability_notices_neutral, request_notices, stable_tool_call_id,
    supports_deferred_tool_search,
};
pub use adapters::{
    SurfaceConfigFacts, compaction_capabilities_from_surface_config, configured_profiles,
    configured_profiles_from_facts, native_preservation_notices, reasoning_capability_notices,
    thinking_requirement_from_intent, validate_provider_references,
    validate_provider_references_neutral,
};
pub use additional_codecs::{
    GeminiGenerateContentCodec, GeminiInteractionsCodec, OpenAiResponsesCodec,
};
pub use codec::{
    AdaptationCode, AdaptationNotice, BuiltinCodec, CodecError, CodecOutput, CodecReasonCode,
    CompatibilityPath, DecodedProviderPayload, StreamAdapterKind, WireCodec, WireCodecId,
    builtin_codec, compatibility_path,
};
pub use codecs::{AnthropicMessagesCodec, OpenAiChatCodec, builtin_codec_instance};
pub use decode::{
    DecodeError, DecodeLimits, MediaLimitError, canonical_request_from_object_with_limits,
    canonical_request_from_value_with_limits,
};
pub use registry::{
    CodecFamily, CompactionCapabilities, ConfiguredWireProfile, WireHint, WireProfileDefinition,
    WireProfileId, WireProfileRegistry, WireRegistryError, WireSurface, WireSurfaceName,
};
pub use runtime::{
    AdaptationKind, AdaptationSummary, CompactResponse, CompactResponseOutcome,
    DEFAULT_MAX_PROVIDER_BODY_BYTES, EncodedWireBody, FiniteResponse, FiniteResponseOutcome,
    PreparedRequest, ProfileMismatchReason, SemanticContentMetadata, StreamFinalization,
    StreamIntent, StreamPushResult, WireByteFacts, WireProfileFlags, WireRuntime,
    WireRuntimeContext, WireRuntimeError, WireRuntimeIdentity, WireStream,
};
pub use stream::{
    ClientStreamEncoder, MAX_SSE_FRAME_BYTES, SSEFrame, SseDecodeError, SseDecodeResult,
    SseDecoder, SseFrame, StreamError, StreamEventDecoder, StreamForwardingMode,
    StreamTerminalOutcome, StreamTerminalSummary, TerminalEvidence, UsageProtocol,
    decode_stream_event, encode_client_event, normalize_usage,
};
