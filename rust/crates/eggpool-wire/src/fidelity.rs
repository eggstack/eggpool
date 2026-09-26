//! Preflight translation fidelity over the shared adaptation decision engine.
//!
//! This module answers "what would encoding this request to `target` cost?"
//! without transporting or mutating anything. [`plan_request_translation`]
//! calls the existing [`request_notices`](crate::adaptation::request_notices)
//! and
//! [`native_summary_notices`](crate::adaptation::native_summary_notices)
//! helpers — the same functions every `encode_request` implementation calls
//! before encoding — and projects their outcomes onto the [`Fidelity`]
//! vocabulary with [`AdaptationEffect`] details. No semantic check is
//! reimplemented here; the mapping table is [`classify_notice`].
//!
//! [`request_notices`](crate::adaptation::request_notices) is the shared
//! semantic core, but two codecs add target-specific behavior beside it: the
//! Responses codec attaches a client-surface-dependent metadata notice (or
//! rejection), and every codec runs structural block validation that rejects
//! rather than warns (audio/document/tool-call shape). The planner closes
//! that gap with a dry-run `encode_request` through the closed codec
//! dispatch using an internally synthesized profile — observing the full
//! codec behavior instead of reimplementing it. Planner/encoder agreement
//! therefore holds strictly by construction: an empty notice list plans
//! [`Fidelity::Exact`], notices plan their classified fidelity with one
//! effect per notice in encoder order, and any encoding `Err`
//! (blocker/validation/structural) plans [`Fidelity::Unsupported`] with a
//! single [`AdaptationEffectClass::Blocked`] effect carrying the error
//! reason/field.
//!
//! # Fidelity semantics (normative)
//!
//! | Fidelity | Meaning |
//! |---|---|
//! | [`Fidelity::Exact`] | No notice. The target grammar carries every canonical field. |
//! | [`Fidelity::WireNormalized`] | Representation-only shaping (e.g. an image `detail` hint the target cannot spell). Meaning is preserved; the wire form is normalized. |
//! | [`Fidelity::SemanticallyEquivalent`] | A different grammar construct carries the same intent (freeform/deferred tools wrapped as functions, collapsed tool ordering). |
//! | [`Fidelity::Lossy`] | Material semantic content is dropped or degraded (metadata, reasoning controls, cache markers, structured-output shape, media, native extensions). Never collapsed into a cosmetic warning: [`Fidelity::requires_notice`] is true and the effect class is [`AdaptationEffectClass::Omitted`] (dropped) or [`AdaptationEffectClass::Approximated`] (remapped with intent preserved). |
//! | [`Fidelity::Unsupported`] | A blocker or validation error. Nothing is encoded. Exactly one [`AdaptationEffectClass::Blocked`] effect carries the reason/field. |
//!
//! Combination precedence (documented here, not as an `Ord` impl):
//! `Unsupported > Lossy > SemanticallyEquivalent > WireNormalized > Exact`.
//! Any mixed lossy+equivalent plan is [`Fidelity::Lossy`]; any blocker makes
//! the plan [`Fidelity::Unsupported`]. There is deliberately no `Ord`
//! implementation so callers cannot compare ordinals incorrectly; use the
//! explicit predicates [`Fidelity::is_exact`], [`Fidelity::is_supported`],
//! and [`Fidelity::requires_notice`].
//!
//! # No fabrication
//!
//! [`AdaptationEffectClass::Synthesized`] is never emitted on the request
//! path: the planner invents no value. Provider-owned signatures, encrypted
//! reasoning payloads, provider IDs, and terminal events are never
//! synthesized to satisfy a target grammar; such gaps surface as blockers
//! ([`Fidelity::Unsupported`]) instead. `Synthesized` is reserved for
//! deterministic compatibility identities (see
//! [`stable_tool_call_id`](crate::adaptation::stable_tool_call_id)) in
//! response/stream paths.
//!
//! # Codec-specific structural validation
//!
//! `request_notices` owns the shared semantic validation (reasoning,
//! structured-output, and tool-identity checks). Individual codecs add
//! target-specific behavior beside it — the Responses metadata notice (or
//! rejection for Responses-sourced metadata) and per-codec structural block
//! validation (audio, some document shapes, malformed tool-call arguments)
//! — and that behavior is unchanged by this module. The planner observes it
//! through the dry-run encode described above, so a planner result never
//! claims [`Fidelity::Exact`] where actual encoding warns or rejects.
//!
//! # Compatibility
//!
//! [`AdaptationNotice`](crate::codec::AdaptationNotice) generation is
//! untouched: codecs keep emitting the same stable codes, and
//! [`apply_adaptation_policy`](crate::adaptation::apply_adaptation_policy)
//! keeps enforcing the same
//! [`LossPolicy`](crate::adaptation::LossPolicy)`::Warn`/`Reject` semantics.
//! Effects are a projection of notices — effect codes equal notice codes in
//! order — never a replacement. There is intentionally no reverse mapping
//! from effects back to notices: reconstructing a notice would require
//! fabricating its reason and source/target surfaces.

use crate::adaptation::{NativeSummaryFacts, native_summary_notices, request_notices};
use crate::codec::{AdaptationNotice, CodecError, CodecOutput, CodecReasonCode, WireCodecId};
use crate::ir::CanonicalRequest;
use crate::profile::{ConfiguredWireProfile, WireProfileDefinition, WireSurface};

/// Preflight fidelity of translating canonical semantics to a target surface.
///
/// See the [module-level semantics](self) table. Material loss is never
/// collapsed into a cosmetic level: anything below `WireNormalized` /
/// `SemanticallyEquivalent` degrades meaning and requires notice.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Fidelity {
    /// No adaptation notice; the target carries every canonical field.
    Exact,
    /// Representation-only normalization; meaning preserved.
    WireNormalized,
    /// Same intent carried by a different grammar construct.
    SemanticallyEquivalent,
    /// Material semantic content dropped or degraded.
    Lossy,
    /// A blocker or validation error; nothing is encoded.
    Unsupported,
}

impl Fidelity {
    /// True only for [`Fidelity::Exact`].
    pub const fn is_exact(self) -> bool {
        matches!(self, Self::Exact)
    }

    /// False only for [`Fidelity::Unsupported`]. A supported plan may still
    /// carry loss notices; check [`Fidelity::requires_notice`].
    pub const fn is_supported(self) -> bool {
        !matches!(self, Self::Unsupported)
    }

    /// True when the translation drops, degrades, rewrites, or blocks
    /// content — i.e. anything that is not [`Fidelity::Exact`].
    pub const fn requires_notice(self) -> bool {
        !matches!(self, Self::Exact)
    }
}

/// Typed class of one adaptation effect.
///
/// Redaction-safe by construction: an effect carries a stable machine
/// code and an optional structural field path, never prompt, body, schema,
/// or credential content.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum AdaptationEffectClass {
    /// Same semantics, different grammar (wrapped tools, normalized detail).
    Rewritten,
    /// Semantic content dropped on the target (metadata, reasoning controls).
    Omitted,
    /// Remapped while preserving the intent level. Currently reserved: the
    /// request codecs drop unsupported reasoning controls rather than
    /// remapping them, so this class documents the taxonomy slot without
    /// claiming a remap happened.
    Approximated,
    /// A deterministic compatibility identity (never provider-owned data).
    /// Never emitted on the request path; reserved for response/stream paths.
    Synthesized,
    /// A blocker or validation error; nothing is encoded.
    Blocked,
}

/// One typed adaptation effect: a stable code plus an optional field path.
///
/// `code` equals the originating [`AdaptationNotice`](crate::codec::AdaptationNotice)
/// code for notice-derived effects, or the snake-case codec reason (e.g.
/// `"unsupported_semantic_feature"`) for [`AdaptationEffectClass::Blocked`]
/// effects. No prompt, body, schema, or credential content is retained.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AdaptationEffect {
    pub class: AdaptationEffectClass,
    pub code: String,
    pub field: Option<String>,
}

impl AdaptationEffect {
    pub fn new(
        class: AdaptationEffectClass,
        code: impl Into<String>,
        field: Option<impl Into<String>>,
    ) -> Self {
        Self {
            class,
            code: code.into(),
            field: field.map(Into::into),
        }
    }
}

/// Preflight result for translating canonical semantics between surfaces.
///
/// Computable without transport or mutation. `effects` are ordered to match
/// the underlying adaptation notices one-for-one.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TranslationPlan {
    pub source: WireSurface,
    pub target: WireSurface,
    pub fidelity: Fidelity,
    pub effects: Vec<AdaptationEffect>,
}

/// Single code-to-`(fidelity, effect class)` table shared by the request
/// planner and the response/stream helpers.
///
/// Unknown (future) codes conservatively map to `(Lossy, Omitted)`: an
/// unrecognized notice is material until proven cosmetic. No code maps to
/// `Unsupported` — unsupported arises only from `Err` (blocker/validation)
/// outcomes, which become a single [`AdaptationEffectClass::Blocked`] effect.
pub fn classify_notice(code: &str) -> (Fidelity, AdaptationEffectClass) {
    use AdaptationEffectClass::{Omitted, Rewritten};
    use Fidelity::{Lossy, SemanticallyEquivalent, WireNormalized};
    match code {
        // Same intent, different grammar construct.
        "freeform_tool_wrapped_as_function"
        | "deferred_tool_search_wrapped_as_function"
        | "tool_order_collapsed"
        | "reasoning_capability_uncertain" => (SemanticallyEquivalent, Rewritten),
        // Representation-only shaping; meaning preserved.
        "image_detail_not_representable" => (WireNormalized, Rewritten),
        // Content preserved but its refusal marking is lost on the target.
        "refusal_not_representable" => (Lossy, Rewritten),
        // Material drops. Reasoning controls are dropped (not remapped), so
        // they are Omitted; Approximated stays reserved for a future remap
        // that provably preserves the intent level.
        "metadata_not_representable"
        | "reasoning_effort_not_representable"
        | "reasoning_budget_not_representable"
        | "reasoning_control_not_representable"
        | "reasoning_control_dropped_by_capability"
        | "structured_output_not_representable"
        | "structured_schema_not_representable"
        | "tool_call_id_not_representable"
        | "audio_not_representable"
        | "document_not_representable"
        | "cache_control_unsupported_placement"
        | "cache_control_invalid_shape"
        | "cache_breakpoint_invalid_shape"
        | "cache_boundary_not_representable"
        | "cache_control_unsupported_by_target"
        | "provider_extension_not_representable"
        | "parallel_tool_calls_not_representable"
        | "native_extension_not_representable"
        | "native_extensions_truncated" => (Lossy, Omitted),
        // Conservative default for future codes: material until proven cosmetic.
        _ => (Lossy, Omitted),
    }
}

/// Project adaptation notices onto ordered, redaction-safe effects.
///
/// Effect codes equal notice codes in order; fields are cloned verbatim
/// (structural paths only — notices never carry content).
pub fn effects_for_notices(notices: &[AdaptationNotice]) -> Vec<AdaptationEffect> {
    notices
        .iter()
        .map(|notice| {
            let (_, class) = classify_notice(&notice.code.0);
            AdaptationEffect {
                class,
                code: notice.code.0.clone(),
                field: notice.field.clone(),
            }
        })
        .collect()
}

fn fidelity_for_notice_list(notices: &[AdaptationNotice]) -> Fidelity {
    let mut fidelity = Fidelity::Exact;
    for notice in notices {
        let (next, _) = classify_notice(&notice.code.0);
        fidelity = combine(fidelity, next);
    }
    fidelity
}

/// Combine two fidelity levels by documented severity:
/// `Unsupported > Lossy > SemanticallyEquivalent > WireNormalized > Exact`.
fn combine(current: Fidelity, next: Fidelity) -> Fidelity {
    fn rank(fidelity: Fidelity) -> u8 {
        match fidelity {
            Fidelity::Exact => 0,
            Fidelity::WireNormalized => 1,
            Fidelity::SemanticallyEquivalent => 2,
            Fidelity::Lossy => 3,
            Fidelity::Unsupported => 4,
        }
    }
    if rank(next) > rank(current) {
        next
    } else {
        current
    }
}

/// Fidelity for response/stream encode notices, sharing [`classify_notice`]
/// with the request planner.
///
/// Empty notices are [`Fidelity::Exact`]; otherwise the severest classified
/// notice wins. `Err` outcomes from response encoding are blockers and map
/// to [`Fidelity::Unsupported`] at the call site (same rule as the request
/// planner); this helper only folds the notice list.
pub fn fidelity_for_response_notices(notices: &[AdaptationNotice]) -> Fidelity {
    fidelity_for_notice_list(notices)
}

fn reason_as_str(reason: CodecReasonCode) -> &'static str {
    match reason {
        CodecReasonCode::MalformedSourceRequest => "malformed_source_request",
        CodecReasonCode::MalformedProviderResponse => "malformed_provider_response",
        CodecReasonCode::MalformedProviderEvent => "malformed_provider_event",
        CodecReasonCode::UnsupportedWireProfile => "unsupported_wire_profile",
        CodecReasonCode::UnsupportedSemanticFeature => "unsupported_semantic_feature",
        CodecReasonCode::LossRejected => "loss_rejected",
        CodecReasonCode::ResourceLimitViolation => "resource_limit_violation",
    }
}

fn blocked_plan(source: WireSurface, target: WireSurface, error: &CodecError) -> TranslationPlan {
    TranslationPlan {
        source,
        target,
        fidelity: Fidelity::Unsupported,
        effects: vec![AdaptationEffect {
            class: AdaptationEffectClass::Blocked,
            code: reason_as_str(error.reason).to_owned(),
            field: error.field.clone(),
        }],
    }
}

/// Preflight the translation of a canonical request from `source` to `target`.
///
/// Calls [`request_notices`](crate::adaptation::request_notices) plus, when
/// `native_summary` is present,
/// [`native_summary_notices`](crate::adaptation::native_summary_notices) —
/// the same functions the finite codecs call before encoding — then closes
/// the codec-specific gap with a dry-run `encode_request` through the closed
/// codec dispatch (internally synthesized profile; pure, no transport), and
/// projects the merged notices through [`classify_notice`]. No semantic
/// check is reimplemented: the shared engine decides, the dry run observes.
///
/// An empty notice list plans [`Fidelity::Exact`]; notices plan their
/// classified fidelity with one effect per notice in encoder order; and an
/// `Err` (blocker/validation/structural) plans [`Fidelity::Unsupported`]
/// with a single [`AdaptationEffectClass::Blocked`] effect carrying the error
/// reason/field.
///
/// The `Result` is infallible today (blockers become `Unsupported` plans, not
/// `Err`); it is retained so future fallible inputs do not reshape the API.
/// No value is fabricated on this path: `Synthesized` is never emitted.
pub fn plan_request_translation(
    request: &CanonicalRequest,
    source: WireSurface,
    target: WireSurface,
    native_summary: Option<&NativeSummaryFacts>,
) -> Result<TranslationPlan, CodecError> {
    let mut notices = match request_notices(request, target) {
        Ok(notices) => notices,
        Err(error) => return Ok(blocked_plan(source, target, &error)),
    };
    if let Some(summary) = native_summary {
        match native_summary_notices(summary, target) {
            Ok(mut extra) => notices.append(&mut extra),
            Err(error) => return Ok(blocked_plan(source, target, &error)),
        }
    }
    let shared_len = notices.len();
    let encoded = match dry_run_encode_request(request, target) {
        Ok(output) => output.notices,
        Err(error) => return Ok(blocked_plan(source, target, &error)),
    };
    // The dry run starts from the same shared engine on the same input, so
    // its output deterministically begins with the notices above; adopt any
    // codec-specific tail verbatim so effects track encoding exactly.
    if encoded.len() >= shared_len && encoded[..shared_len] == notices[..] {
        notices = encoded;
    } else {
        // Future codecs must keep shared notices as a prefix; fall back to
        // appending unseen extras rather than misordering.
        for extra in encoded {
            if !notices.contains(&extra) {
                notices.push(extra);
            }
        }
    }
    Ok(TranslationPlan {
        source,
        target,
        fidelity: fidelity_for_notice_list(&notices),
        effects: effects_for_notices(&notices),
    })
}

/// Dry-run the target surface's finite codec with an internally synthesized
/// profile.
///
/// Pure and transport-free: the profile carries only the closed codec
/// identity the dispatch needs. This observes codec-specific notices and
/// structural rejections (Responses metadata rule, block validation) without
/// reimplementing any semantic check.
fn dry_run_encode_request(
    request: &CanonicalRequest,
    target: WireSurface,
) -> Result<CodecOutput<serde_json::Value>, CodecError> {
    let codec_id = match target {
        WireSurface::OpenaiChatCompletions => WireCodecId::OpenaiChat,
        WireSurface::OpenaiResponses => WireCodecId::OpenaiResponses,
        WireSurface::AnthropicMessages => WireCodecId::AnthropicMessages,
        WireSurface::GeminiInteractions => WireCodecId::GeminiInteractions,
        WireSurface::GeminiGenerateContent => WireCodecId::GeminiGenerateContent,
    };
    let codec = crate::codecs::builtin_codec_instance(codec_id)
        .expect("every wire surface has a finite request codec");
    let profile = ConfiguredWireProfile {
        definition: WireProfileDefinition {
            surface: target,
            request_codec: codec_id,
            response_codec: codec_id,
            stream_codec: codec_id,
        },
        path_template: "m004-planner".into(),
        stream_path_template: None,
        priority: 0,
    };
    codec.encode_request(request, &profile)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::adaptation::{AdaptationPolicy, LossPolicy};
    use crate::adaptation::{NativeSummaryFacts, apply_adaptation_policy};
    use crate::codec::{AdaptationCode, CodecOutput, CodecReasonCode, WireCodec, WireCodecId};
    use crate::codecs::{AnthropicMessagesCodec, OpenAiChatCodec};
    use crate::decode::{DecodeLimits, canonical_request_from_value_with_limits};
    use crate::ir::{
        CanonicalBlockKind, CanonicalContentBlock, CanonicalMessage, CanonicalRole, CanonicalTool,
        CanonicalToolKind, ClientSurface, MediaSource, Presence, ReasoningIntent, ReasoningMode,
    };
    use crate::profile::{ConfiguredWireProfile, WireProfileDefinition};
    use serde_json::{Map, Value, json};

    fn test_profile(surface: WireSurface, codec: WireCodecId) -> ConfiguredWireProfile {
        ConfiguredWireProfile {
            definition: WireProfileDefinition {
                surface,
                request_codec: codec,
                response_codec: codec,
                stream_codec: codec,
            },
            path_template: "/m004-planner".into(),
            stream_path_template: None,
            priority: 0,
        }
    }

    fn decode_chat(value: Value) -> CanonicalRequest {
        canonical_request_from_value_with_limits(
            &value,
            ClientSurface::ChatCompletions,
            DecodeLimits::current(),
        )
        .expect("fixture decodes")
    }

    fn minimal_request() -> CanonicalRequest {
        decode_chat(json!({
            "model": "model-a",
            "messages": [{"role": "user", "content": "hello"}],
        }))
    }

    fn reasoning_effort_request() -> CanonicalRequest {
        decode_chat(json!({
            "model": "model-a",
            "messages": [{"role": "user", "content": "think"}],
            "reasoning_effort": "medium",
        }))
    }

    fn freeform_tool_request() -> CanonicalRequest {
        let mut request = minimal_request();
        request.tools.push(CanonicalTool {
            kind: CanonicalToolKind::Freeform,
            name: "doodle".into(),
            description: Some("freeform tool".into()),
            parameters: Map::new(),
            cache_control: None,
            defer_loading: None,
        });
        request
    }

    fn deferred_tool_request() -> CanonicalRequest {
        let mut request = minimal_request();
        request.tools.push(CanonicalTool {
            kind: CanonicalToolKind::DeferredSearch,
            name: "tool_search".into(),
            description: Some("search".into()),
            parameters: crate::ir::default_tool_search_parameters(),
            cache_control: None,
            defer_loading: None,
        });
        request
    }

    fn image_detail_request() -> CanonicalRequest {
        let mut request = minimal_request();
        request.messages.push(CanonicalMessage {
            role: CanonicalRole::User,
            content: vec![CanonicalContentBlock {
                kind: CanonicalBlockKind::Image,
                text: None,
                media: Some(MediaSource {
                    media_type: Some("image/png".into()),
                    data: Some("aGk=".into()),
                    uri: None,
                    detail: Some("high".into()),
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
            }],
            tool_call_id: None,
            name: None,
            refusal: None,
        });
        request
    }

    fn audio_request() -> CanonicalRequest {
        let mut request = minimal_request();
        request.messages.push(CanonicalMessage {
            role: CanonicalRole::User,
            content: vec![CanonicalContentBlock {
                kind: CanonicalBlockKind::Audio,
                text: None,
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
            }],
            tool_call_id: None,
            name: None,
            refusal: None,
        });
        request
    }

    fn document_request() -> CanonicalRequest {
        let mut request = minimal_request();
        request.messages.push(CanonicalMessage {
            role: CanonicalRole::User,
            content: vec![CanonicalContentBlock {
                kind: CanonicalBlockKind::Document,
                text: None,
                media: Some(MediaSource {
                    media_type: Some("application/pdf".into()),
                    data: Some("aGk=".into()),
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
            }],
            tool_call_id: None,
            name: None,
            refusal: None,
        });
        request
    }

    fn cache_marker_request() -> CanonicalRequest {
        let mut request = minimal_request();
        request.cache_control = Some(json!({"type": "ephemeral"}));
        request
    }

    fn metadata_request() -> CanonicalRequest {
        let mut request = minimal_request();
        request.metadata.insert("tenant".into(), "acme".into());
        request
    }

    fn responses_client_metadata_request() -> CanonicalRequest {
        // Synthetic Responses-sourced metadata: the Responses codec rejects
        // (rather than warns) metadata it cannot round-trip natively, so the
        // Responses-target plan must be Unsupported while other targets
        // follow the shared engine.
        let mut request = metadata_request();
        request.client_surface = ClientSurface::Responses;
        request
    }

    fn parallel_tool_calls_request() -> CanonicalRequest {
        let mut request = minimal_request();
        request.parallel_tool_calls = Some(false);
        request.presence.parallel_tool_calls = Presence::Value(false);
        request
    }

    fn structured_output_request() -> CanonicalRequest {
        decode_chat(json!({
            "model": "model-a",
            "messages": [{"role": "user", "content": "emit json"}],
            "response_format": {"type": "json_object"},
        }))
    }

    fn mixed_tool_and_text_request() -> CanonicalRequest {
        let mut request = minimal_request();
        request.tools.push(CanonicalTool {
            kind: CanonicalToolKind::Function,
            name: "lookup".into(),
            description: None,
            parameters: Map::new(),
            cache_control: None,
            defer_loading: None,
        });
        request.messages.push(CanonicalMessage {
            role: CanonicalRole::Assistant,
            content: vec![
                CanonicalContentBlock::text("partial"),
                CanonicalContentBlock {
                    kind: CanonicalBlockKind::ToolCall,
                    text: None,
                    media: None,
                    call_id: Some("call-1".into()),
                    name: Some("lookup".into()),
                    arguments: Some("{}".into()),
                    tool_input: None,
                    tool_kind: CanonicalToolKind::Function,
                    is_error: false,
                    signature: None,
                    cache_control: None,
                    prompt_cache_breakpoint: None,
                },
            ],
            tool_call_id: None,
            name: None,
            refusal: None,
        });
        request
    }

    fn corpus() -> Vec<(&'static str, CanonicalRequest)> {
        vec![
            ("minimal", minimal_request()),
            ("reasoning_effort", reasoning_effort_request()),
            ("freeform_tool", freeform_tool_request()),
            ("deferred_tool", deferred_tool_request()),
            ("image_detail", image_detail_request()),
            ("audio", audio_request()),
            ("document", document_request()),
            ("cache_marker", cache_marker_request()),
            ("metadata", metadata_request()),
            (
                "responses_client_metadata",
                responses_client_metadata_request(),
            ),
            ("parallel_tool_calls", parallel_tool_calls_request()),
            ("structured_output", structured_output_request()),
            ("mixed_tool_text", mixed_tool_and_text_request()),
        ]
    }

    fn all_targets() -> Vec<(WireSurface, WireCodecId, Box<dyn WireCodec>)> {
        vec![
            (
                WireSurface::OpenaiChatCompletions,
                WireCodecId::OpenaiChat,
                Box::new(OpenAiChatCodec),
            ),
            (
                WireSurface::OpenaiResponses,
                WireCodecId::OpenaiResponses,
                Box::new(crate::additional_codecs::OpenAiResponsesCodec),
            ),
            (
                WireSurface::AnthropicMessages,
                WireCodecId::AnthropicMessages,
                Box::new(AnthropicMessagesCodec),
            ),
            (
                WireSurface::GeminiInteractions,
                WireCodecId::GeminiInteractions,
                Box::new(crate::additional_codecs::GeminiInteractionsCodec),
            ),
            (
                WireSurface::GeminiGenerateContent,
                WireCodecId::GeminiGenerateContent,
                Box::new(crate::additional_codecs::GeminiGenerateContentCodec),
            ),
        ]
    }

    #[test]
    fn fidelity_predicates_match_variants() {
        assert!(Fidelity::Exact.is_exact());
        assert!(Fidelity::Exact.is_supported());
        assert!(!Fidelity::Exact.requires_notice());
        for fidelity in [
            Fidelity::WireNormalized,
            Fidelity::SemanticallyEquivalent,
            Fidelity::Lossy,
        ] {
            assert!(!fidelity.is_exact());
            assert!(fidelity.is_supported());
            assert!(fidelity.requires_notice());
        }
        assert!(!Fidelity::Unsupported.is_exact());
        assert!(!Fidelity::Unsupported.is_supported());
        assert!(Fidelity::Unsupported.requires_notice());
    }

    #[test]
    fn classify_notice_covers_every_known_code() {
        let cases: &[(&str, Fidelity, AdaptationEffectClass)] = &[
            (
                "freeform_tool_wrapped_as_function",
                Fidelity::SemanticallyEquivalent,
                AdaptationEffectClass::Rewritten,
            ),
            (
                "deferred_tool_search_wrapped_as_function",
                Fidelity::SemanticallyEquivalent,
                AdaptationEffectClass::Rewritten,
            ),
            (
                "tool_order_collapsed",
                Fidelity::SemanticallyEquivalent,
                AdaptationEffectClass::Rewritten,
            ),
            (
                "image_detail_not_representable",
                Fidelity::WireNormalized,
                AdaptationEffectClass::Rewritten,
            ),
            (
                "metadata_not_representable",
                Fidelity::Lossy,
                AdaptationEffectClass::Omitted,
            ),
            (
                "reasoning_effort_not_representable",
                Fidelity::Lossy,
                AdaptationEffectClass::Omitted,
            ),
            (
                "reasoning_budget_not_representable",
                Fidelity::Lossy,
                AdaptationEffectClass::Omitted,
            ),
            (
                "reasoning_control_not_representable",
                Fidelity::Lossy,
                AdaptationEffectClass::Omitted,
            ),
            (
                "structured_output_not_representable",
                Fidelity::Lossy,
                AdaptationEffectClass::Omitted,
            ),
            (
                "structured_schema_not_representable",
                Fidelity::Lossy,
                AdaptationEffectClass::Omitted,
            ),
            (
                "tool_call_id_not_representable",
                Fidelity::Lossy,
                AdaptationEffectClass::Omitted,
            ),
            (
                "audio_not_representable",
                Fidelity::Lossy,
                AdaptationEffectClass::Omitted,
            ),
            (
                "document_not_representable",
                Fidelity::Lossy,
                AdaptationEffectClass::Omitted,
            ),
            (
                "cache_control_unsupported_placement",
                Fidelity::Lossy,
                AdaptationEffectClass::Omitted,
            ),
            (
                "cache_boundary_not_representable",
                Fidelity::Lossy,
                AdaptationEffectClass::Omitted,
            ),
            (
                "parallel_tool_calls_not_representable",
                Fidelity::Lossy,
                AdaptationEffectClass::Omitted,
            ),
            (
                "native_extension_not_representable",
                Fidelity::Lossy,
                AdaptationEffectClass::Omitted,
            ),
            (
                "native_extensions_truncated",
                Fidelity::Lossy,
                AdaptationEffectClass::Omitted,
            ),
            (
                "refusal_not_representable",
                Fidelity::Lossy,
                AdaptationEffectClass::Rewritten,
            ),
        ];
        for (code, fidelity, class) in cases {
            assert_eq!(classify_notice(code), (*fidelity, *class), "code {code}");
        }
        // Unknown future codes are conservatively material.
        assert_eq!(
            classify_notice("future_unknown_code"),
            (Fidelity::Lossy, AdaptationEffectClass::Omitted)
        );
    }

    #[test]
    fn fidelity_combination_prefers_severest_level() {
        assert_eq!(
            fidelity_for_notice_list(&[]),
            Fidelity::Exact,
            "empty notices are exact"
        );
        let notice = |code: &str| {
            AdaptationNotice::new(
                code,
                CodecReasonCode::UnsupportedSemanticFeature,
                None::<String>,
                None,
                None,
            )
        };
        // Equivalent + normalized mixes stay non-lossy at the severer level.
        assert_eq!(
            fidelity_for_notice_list(&[
                notice("image_detail_not_representable"),
                notice("freeform_tool_wrapped_as_function"),
            ]),
            Fidelity::SemanticallyEquivalent
        );
        // Any mixed lossy + equivalent profiles as lossy.
        assert_eq!(
            fidelity_for_notice_list(&[
                notice("freeform_tool_wrapped_as_function"),
                notice("metadata_not_representable"),
            ]),
            Fidelity::Lossy
        );
        // Image detail alone is wire-normalized, never lossy.
        assert_eq!(
            fidelity_for_notice_list(&[notice("image_detail_not_representable")]),
            Fidelity::WireNormalized
        );
        // Metadata dropped is lossy, never a cosmetic warning level.
        assert_eq!(
            fidelity_for_notice_list(&[notice("metadata_not_representable")]),
            Fidelity::Lossy
        );
    }

    #[test]
    fn planner_agrees_with_encoder_across_corpus_and_surfaces() {
        for (name, request) in corpus() {
            for (target, codec_id, codec) in all_targets() {
                let profile = test_profile(target, codec_id);
                let encoded = codec.encode_request(&request, &profile);
                let plan = plan_request_translation(
                    &request,
                    WireSurface::OpenaiChatCompletions,
                    target,
                    None,
                )
                .expect("planner is infallible");
                assert_eq!(plan.source, WireSurface::OpenaiChatCompletions);
                assert_eq!(plan.target, target);
                match encoded {
                    Ok(output) => {
                        // Effect codes equal notice codes in order.
                        let effect_codes: Vec<&str> = plan
                            .effects
                            .iter()
                            .map(|effect| effect.code.as_str())
                            .collect();
                        let notice_codes: Vec<&str> = output
                            .notices
                            .iter()
                            .map(|notice| notice.code.0.as_str())
                            .collect();
                        assert_eq!(
                            effect_codes, notice_codes,
                            "fixture {name} on {target:?}: effects track notices"
                        );
                        // Fidelity matches the shared table over the same notices.
                        assert_eq!(
                            plan.fidelity,
                            fidelity_for_response_notices(&output.notices),
                            "fixture {name} on {target:?}: fidelity folds notices"
                        );
                        if output.notices.is_empty() {
                            assert_eq!(
                                plan.fidelity,
                                Fidelity::Exact,
                                "fixture {name} on {target:?}"
                            );
                        }
                        // LossPolicy compat: Warn passes through, Reject
                        // rejects exactly when notices are non-empty.
                        let warned = apply_adaptation_policy(
                            CodecOutput {
                                value: output.value.clone(),
                                notices: output.notices.clone(),
                            },
                            &AdaptationPolicy::warn(),
                        );
                        assert!(warned.is_ok(), "fixture {name} on {target:?}");
                        let rejected = apply_adaptation_policy(
                            CodecOutput {
                                value: output.value,
                                notices: output.notices,
                            },
                            &AdaptationPolicy::reject(),
                        );
                        assert_eq!(
                            rejected.is_ok(),
                            plan.fidelity == Fidelity::Exact,
                            "fixture {name} on {target:?}: Reject iff non-exact"
                        );
                    }
                    Err(error) => {
                        // Strict agreement: any encoding rejection
                        // (shared-validation blocker, native blocker via the
                        // dry run's shared core, codec-structural rejection,
                        // or Responses metadata rejection) plans Unsupported
                        // with a single Blocked effect. The planner never
                        // claims exactness where encoding refuses.
                        assert_eq!(
                            plan.fidelity,
                            Fidelity::Unsupported,
                            "fixture {name} on {target:?}: encoder rejected ({error:?})"
                        );
                        assert_eq!(plan.effects.len(), 1);
                        assert_eq!(plan.effects[0].class, AdaptationEffectClass::Blocked);
                    }
                }
            }
        }
    }

    #[test]
    fn planner_native_summary_blockers_are_unsupported() {
        let request = minimal_request();
        let blocker = NativeSummaryFacts {
            native_input_items: 1,
            native_tool_definitions: 0,
            extension_fields: Vec::new(),
            extensions_truncated: false,
        };
        let plan = plan_request_translation(
            &request,
            WireSurface::OpenaiResponses,
            WireSurface::OpenaiChatCompletions,
            Some(&blocker),
        )
        .expect("planner is infallible");
        assert_eq!(plan.fidelity, Fidelity::Unsupported);
        assert_eq!(plan.effects.len(), 1);
        assert_eq!(plan.effects[0].class, AdaptationEffectClass::Blocked);
        assert_eq!(plan.effects[0].field.as_deref(), Some("input.native_item"));

        // Same-surface Responses target with a blocker summary stays exact:
        // native forwarding never consults the cross-surface policy.
        let plan = plan_request_translation(
            &request,
            WireSurface::OpenaiResponses,
            WireSurface::OpenaiResponses,
            Some(&blocker),
        )
        .expect("planner is infallible");
        assert_eq!(plan.fidelity, Fidelity::Exact);
        assert!(plan.effects.is_empty());

        // Extension fields degrade to lossy, never exact.
        let extensions = NativeSummaryFacts {
            native_input_items: 0,
            native_tool_definitions: 0,
            extension_fields: vec!["extra".into()],
            extensions_truncated: true,
        };
        let plan = plan_request_translation(
            &request,
            WireSurface::OpenaiResponses,
            WireSurface::AnthropicMessages,
            Some(&extensions),
        )
        .expect("planner is infallible");
        assert_eq!(plan.fidelity, Fidelity::Lossy);
        let codes: Vec<&str> = plan
            .effects
            .iter()
            .map(|effect| effect.code.as_str())
            .collect();
        assert_eq!(
            codes,
            vec![
                "native_extension_not_representable",
                "native_extensions_truncated"
            ]
        );
    }

    #[test]
    fn planner_validation_errors_are_unsupported_with_blocked_effect() {
        // Duplicate tool names fail shared validation in request_notices.
        let mut request = minimal_request();
        for _ in 0..2 {
            request.tools.push(CanonicalTool {
                kind: CanonicalToolKind::Function,
                name: "dup".into(),
                description: None,
                parameters: Map::new(),
                cache_control: None,
                defer_loading: None,
            });
        }
        let plan = plan_request_translation(
            &request,
            WireSurface::OpenaiChatCompletions,
            WireSurface::OpenaiChatCompletions,
            None,
        )
        .expect("planner is infallible");
        assert_eq!(plan.fidelity, Fidelity::Unsupported);
        assert_eq!(plan.effects.len(), 1);
        assert_eq!(plan.effects[0].class, AdaptationEffectClass::Blocked);
        assert_eq!(plan.effects[0].code, "malformed_source_request");
        assert_eq!(plan.effects[0].field.as_deref(), Some("tools.name"));
    }

    #[test]
    fn response_fidelity_helper_shares_the_table() {
        assert_eq!(fidelity_for_response_notices(&[]), Fidelity::Exact);
        let notice = AdaptationNotice::new(
            "refusal_not_representable",
            CodecReasonCode::UnsupportedSemanticFeature,
            Some("output.refusal"),
            None,
            Some(WireSurface::AnthropicMessages),
        );
        assert_eq!(
            fidelity_for_response_notices(std::slice::from_ref(&notice)),
            Fidelity::Lossy
        );
        let effects = effects_for_notices(&[notice]);
        assert_eq!(effects.len(), 1);
        assert_eq!(effects[0].code, "refusal_not_representable");
        assert_eq!(effects[0].class, AdaptationEffectClass::Rewritten);
    }

    #[test]
    fn request_path_never_synthesizes_values() {
        // No corpus plan and no classified known code emits Synthesized, so
        // provider-owned signatures/encrypted reasoning/IDs are never
        // fabricated to satisfy a target grammar.
        for (_, request) in corpus() {
            for (target, _, _) in all_targets() {
                let plan = plan_request_translation(
                    &request,
                    WireSurface::OpenaiChatCompletions,
                    target,
                    None,
                )
                .expect("planner is infallible");
                assert!(
                    plan.effects
                        .iter()
                        .all(|effect| effect.class != AdaptationEffectClass::Synthesized),
                    "no Synthesized effects on the request path"
                );
            }
        }
        for code in [
            "freeform_tool_wrapped_as_function",
            "deferred_tool_search_wrapped_as_function",
            "tool_order_collapsed",
            "image_detail_not_representable",
            "metadata_not_representable",
            "reasoning_effort_not_representable",
            "structured_output_not_representable",
            "tool_call_id_not_representable",
            "audio_not_representable",
            "document_not_representable",
            "cache_boundary_not_representable",
            "parallel_tool_calls_not_representable",
            "native_extension_not_representable",
            "native_extensions_truncated",
            "refusal_not_representable",
        ] {
            let (_, class) = classify_notice(code);
            assert_ne!(class, AdaptationEffectClass::Synthesized, "code {code}");
        }
    }

    #[test]
    fn loss_policy_compat_is_unchanged_on_planner_notices() {
        // Warn passes everything through; Reject fails on any notice with
        // the first notice's code/field — the pre-existing behavior.
        let notices = vec![
            AdaptationNotice {
                code: AdaptationCode("metadata_not_representable".into()),
                reason: CodecReasonCode::UnsupportedSemanticFeature,
                field: Some("metadata".into()),
                source_surface: Some(WireSurface::OpenaiChatCompletions),
                target_surface: Some(WireSurface::AnthropicMessages),
            },
            AdaptationNotice {
                code: AdaptationCode("tool_order_collapsed".into()),
                reason: CodecReasonCode::UnsupportedSemanticFeature,
                field: Some("messages.content".into()),
                source_surface: Some(WireSurface::OpenaiChatCompletions),
                target_surface: Some(WireSurface::OpenaiChatCompletions),
            },
        ];
        let value = Value::Null;
        let warned = apply_adaptation_policy(
            CodecOutput {
                value: value.clone(),
                notices: notices.clone(),
            },
            &AdaptationPolicy {
                loss_policy: LossPolicy::Warn,
                max_notices: 32,
            },
        )
        .expect("warn passes");
        assert_eq!(warned.notices, notices);
        let rejected =
            apply_adaptation_policy(CodecOutput { value, notices }, &AdaptationPolicy::reject())
                .expect_err("reject fails on notices");
        assert_eq!(rejected.reason, CodecReasonCode::LossRejected);
        assert_eq!(rejected.field.as_deref(), Some("metadata"));

        // Planner effects project the same codes in the same order.
        let effects = effects_for_notices(&warned.notices);
        let codes: Vec<&str> = effects.iter().map(|effect| effect.code.as_str()).collect();
        assert_eq!(
            codes,
            vec!["metadata_not_representable", "tool_order_collapsed"]
        );
        assert_eq!(
            fidelity_for_response_notices(&warned.notices),
            Fidelity::Lossy
        );
    }

    #[test]
    fn reasoning_intent_modes_receive_expected_fidelity() {
        // Effort survives on OpenAI surfaces exactly; budget survives on
        // Anthropic exactly. Crossed, both degrade to lossy (dropped).
        let effort = reasoning_effort_request();
        let plan = plan_request_translation(
            &effort,
            WireSurface::OpenaiChatCompletions,
            WireSurface::OpenaiChatCompletions,
            None,
        )
        .expect("planner is infallible");
        assert_eq!(plan.fidelity, Fidelity::Exact);

        let plan = plan_request_translation(
            &effort,
            WireSurface::OpenaiChatCompletions,
            WireSurface::AnthropicMessages,
            None,
        )
        .expect("planner is infallible");
        assert_eq!(plan.fidelity, Fidelity::Lossy);

        let mut budget = minimal_request();
        budget.reasoning = ReasoningIntent {
            requested: Some(true),
            mode: ReasoningMode::FixedBudget,
            effort: None,
            budget_tokens: Some(1024),
            explicit_disable: false,
        };
        let plan = plan_request_translation(
            &budget,
            WireSurface::OpenaiChatCompletions,
            WireSurface::AnthropicMessages,
            None,
        )
        .expect("planner is infallible");
        assert_eq!(plan.fidelity, Fidelity::Exact);

        let plan = plan_request_translation(
            &budget,
            WireSurface::OpenaiChatCompletions,
            WireSurface::OpenaiChatCompletions,
            None,
        )
        .expect("planner is infallible");
        assert_eq!(plan.fidelity, Fidelity::Lossy);
    }
}
