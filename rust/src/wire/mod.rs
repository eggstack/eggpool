//! Canonical, provider-independent wire semantics.

pub mod additional_codecs;
pub mod codec;
pub mod codecs;
pub mod ir;
pub mod registry;

pub use additional_codecs::{
    GeminiGenerateContentCodec, GeminiInteractionsCodec, OpenAiResponsesCodec,
};
pub use codec::{
    AdaptationNotice, BuiltinCodec, CodecError, CodecOutput, CodecReasonCode, CompatibilityPath,
    DecodedProviderPayload, StreamAdapterKind, WireCodec, WireCodecId, builtin_codec,
    compatibility_path,
};
pub use codecs::{AnthropicMessagesCodec, OpenAiChatCodec, builtin_codec_instance};
pub use registry::{
    CodecFamily, ConfiguredWireProfile, WireHint, WireProfileDefinition, WireProfileId,
    WireProfileRegistry, WireRegistryError, WireSurface, WireSurfaceName,
};
