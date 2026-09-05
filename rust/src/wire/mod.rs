//! Canonical, provider-independent wire semantics.

pub mod adaptation;
pub mod additional_codecs;
pub mod codec;
pub mod codecs;
pub mod ir;
pub mod registry;

pub use adaptation::{
    AdaptationOutcome, AdaptationPolicy, CapabilityDisposition, LossPolicy, MAX_ADAPTATION_NOTICES,
    ReasoningCapabilityPolicy, apply_adaptation_policy, reasoning_capability_notices,
    request_notices, stable_tool_call_id,
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
pub use registry::{
    CodecFamily, ConfiguredWireProfile, WireHint, WireProfileDefinition, WireProfileId,
    WireProfileRegistry, WireRegistryError, WireSurface, WireSurfaceName,
};
