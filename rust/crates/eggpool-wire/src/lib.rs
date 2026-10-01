//! Neutral sans-I/O wire kernel for EggPool and sibling semantic producers.
//!
//! This crate owns the canonical request/response/event types, the pure
//! structural decoder, adaptation policy, finite protocol codecs, the
//! wire-profile registry data types, and the streaming state machines. It has
//! no credential, environment, filesystem, network, clock, random, async
//! runtime, database, or logging side effects.
//!
//! EggPool-owned joining logic (request admission, profile selection from
//! config, routing/catalog adaptation, runtime objects, transport, and HTTP
//! status mapping) stays in the `eggpool` root package, which consumes this
//! crate as the single source of truth through `src/wire/` facades.
//!
//! # Three-layer model
//!
//! * **Semantic IR** ([`ir`]): provider-neutral meaning — canonical requests,
//!   responses, and events. [`ir::CanonicalRequest::from_canonical`] gives
//!   semantic producers an origin with no source wire surface. Codecs
//!   translate between wire grammars and this layer; it never carries opaque
//!   provider JSON.
//! * **Provenance** ([`provenance`]): bounded source-native residue with no
//!   canonical projection (source identity, counts, structural field paths).
//!   Separate from the IR, redaction-safe in `Debug`, same-request lifetime,
//!   no persistence contract. Cross-surface encoding never consults it.
//! * **Adaptation and fidelity** ([`adaptation`], [`fidelity`]): the shared
//!   decision engine (`request_notices`, `native_summary_notices`, loss
//!   policy) plus the preflight [`TranslationPlan`](fidelity::TranslationPlan)
//!   projection. The planner and the encoders agree by construction.
//!
//! # Codec coverage
//!
//! Five finite surfaces ([`profile::WireSurface`]) with request/response
//! codecs ([`codecs`], [`additional_codecs`]) and five streaming dialects
//! ([`codec::StreamAdapterKind`]) with incremental decoders ([`stream`]).
//! Protocol-only conformance vectors live in [`conformance`].
//!
//! # Limits and guarantees
//!
//! * MSRV 1.89. No `unsafe` code (`unsafe_code = "forbid"`).
//! * No I/O: pure functions over caller-supplied bytes and values. Byte
//!   forwarding stays caller-owned; observation stays in the kernel.
//! * Attacker-controlled retained state is capped: adaptation notices
//!   ([`adaptation::MAX_ADAPTATION_NOTICES`]), SSE frames
//!   ([`stream::MAX_SSE_FRAME_BYTES`]), provenance fragments and bytes
//!   ([`provenance::MAX_PROVENANCE_FRAGMENTS`],
//!   [`provenance::MAX_PROVENANCE_TOTAL_BYTES`]), and tool-call count and
//!   argument bytes ([`MAX_ACTIVE_CANONICAL_TOOL_CALLS`],
//!   [`MAX_CANONICAL_TOOL_CALL_ARGUMENT_BYTES`],
//!   [`MAX_CANONICAL_TOOL_CALL_TOTAL_BYTES`]).
//! * Unsupported semantics stay blockers: unknown or provider-native fields
//!   are never silently dropped on a path that claims exactness, and
//!   provider-owned signatures, encrypted reasoning, IDs, and terminal
//!   events are never synthesized.
//!
//! # Comparison boundary
//!
//! This is a protocol kernel, not an SDK, provider client, router, or agent
//! framework: no HTTP clients, auth, catalogs, retries, routing, tool
//! execution, MCP, storage, or server APIs live here.
//!
//! # Semantic producer example
//!
//! A sibling application can begin with its own semantic request and use the
//! kernel without claiming client-wire provenance or constructing EggPool
//! runtime metadata:
//!
//! ```no_run
//! use eggpool_wire::{CanonicalToolCallAccumulator, RequestEncodeOptions,
//!     encode_request_for_surface, ir::{CanonicalMessage, CanonicalRole, CanonicalRequest},
//!     codec::StreamAdapterKind, profile::WireSurface, stream::StreamEventDecoder};
//! let message = CanonicalMessage { role: CanonicalRole::User, content: vec![],
//!     tool_call_id: None, name: None, refusal: None };
//! let request = CanonicalRequest::from_canonical("model", vec![message]);
//! let body = encode_request_for_surface(&request, WireSurface::OpenaiChatCompletions,
//!     &RequestEncodeOptions::default()).expect("encoding succeeds").value;
//! // The application owns HTTP and feeds response bytes to StreamEventDecoder.
//! let mut decoder = StreamEventDecoder::new(StreamAdapterKind::OpenaiChatSse);
//! let mut calls = CanonicalToolCallAccumulator::new();
//! let response_bytes: &[u8] = b"";
//! for event in decoder.push(response_bytes).expect("decode succeeds") {
//!     let _ = calls.push(&event).expect("accumulation succeeds");
//! }
//! let (tail, _) = decoder.finalize_events().expect("stream framing succeeds");
//! for event in tail { let _ = calls.push(&event).expect("accumulation succeeds"); }
//! # let _ = body;
//! ```
//!
//! `eggpool-wire 0.1.0` is available on crates.io. This milestone makes no
//! release or semver-stability change; the initial sibling integration is
//! expected to pin an immutable EggPool Git revision.

pub mod adaptation;
pub mod additional_codecs;
pub mod codec;
pub mod codecs;
pub mod conformance;
pub mod decode;
pub mod fidelity;
pub mod ir;
pub mod profile;
pub mod provenance;
pub mod stream;
pub mod tool_calls;

pub use codec::RequestEncodeOptions;
pub use conformance::{StreamConformanceVector, sse_split_points, stream_conformance_vectors};
pub use fidelity::{
    AdaptationEffect, AdaptationEffectClass, Fidelity, TranslationPlan, classify_notice,
    effects_for_notices, fidelity_for_response_notices, plan_canonical_request_translation,
    plan_request_translation,
};

/// Encode canonical semantics for one built-in wire surface without runtime
/// path, priority, or provider configuration.
pub fn encode_request_for_surface(
    request: &ir::CanonicalRequest,
    target: profile::WireSurface,
    options: &RequestEncodeOptions,
) -> Result<codec::CodecOutput<serde_json::Value>, codec::CodecError> {
    let registry = profile::WireProfileRegistry::embedded().map_err(|_| codec::CodecError {
        reason: codec::CodecReasonCode::UnsupportedWireProfile,
        field: Some("builtin_registry".into()),
        source_surface: request.origin.source_surface(),
        target_surface: Some(target),
    })?;
    let definition = registry
        .get(target)
        .cloned()
        .ok_or_else(|| codec::CodecError {
            reason: codec::CodecReasonCode::UnsupportedWireProfile,
            field: Some("target_surface".into()),
            source_surface: request.origin.source_surface(),
            target_surface: Some(target),
        })?;
    let profile = profile::ConfiguredWireProfile {
        definition: definition.clone(),
        path_template: String::new(),
        stream_path_template: None,
        priority: 0,
    };
    let codec = codecs::builtin_codec_instance(definition.request_codec).ok_or_else(|| {
        codec::CodecError {
            reason: codec::CodecReasonCode::UnsupportedWireProfile,
            field: Some("request_codec".into()),
            source_surface: request.origin.source_surface(),
            target_surface: Some(target),
        }
    })?;
    codec.encode_request_with_options(request, &profile, options)
}
pub use provenance::{
    MAX_PROVENANCE_DEPTH, MAX_PROVENANCE_FRAGMENTS, MAX_PROVENANCE_NAME_BYTES,
    MAX_PROVENANCE_TOTAL_BYTES, ProvenanceCompleteness, ProvenanceFragment, ProvenanceShape,
    TruncationReason, WireProvenance, may_restore_exact, may_restore_exact_for,
};
pub use tool_calls::{
    CanonicalToolCallAccumulator, CompletedToolCall, MAX_ACTIVE_CANONICAL_TOOL_CALLS,
    MAX_CANONICAL_TOOL_CALL_ARGUMENT_BYTES, MAX_CANONICAL_TOOL_CALL_TOTAL_BYTES,
    ToolCallAccumulatorError, ToolCallIdentity,
};

#[cfg(test)]
mod external_consumer_tests {
    use super::*;
    use crate::ir::CanonicalRequest;

    #[test]
    fn semantic_requests_encode_for_every_builtin_surface() {
        let request = CanonicalRequest::from_canonical("model", Vec::new());
        assert_eq!(request.origin.source_surface(), None);
        for surface in profile::WireSurface::ALL {
            encode_request_for_surface(&request, surface, &RequestEncodeOptions::default())
                .unwrap_or_else(|error| panic!("{surface:?}: {error:?}"));
        }
    }

    #[test]
    fn openai_stream_usage_is_opt_in_and_stream_only() {
        let mut request = CanonicalRequest::from_canonical("model", Vec::new());
        let defaults = encode_request_for_surface(
            &request,
            profile::WireSurface::OpenaiChatCompletions,
            &RequestEncodeOptions::default(),
        )
        .unwrap();
        assert!(defaults.value.get("stream_options").is_none());
        let options = RequestEncodeOptions {
            include_stream_usage: true,
        };
        let non_stream = encode_request_for_surface(
            &request,
            profile::WireSurface::OpenaiChatCompletions,
            &options,
        )
        .unwrap();
        assert!(non_stream.value.get("stream_options").is_none());
        request.stream = true;
        let enabled = encode_request_for_surface(
            &request,
            profile::WireSurface::OpenaiChatCompletions,
            &options,
        )
        .unwrap();
        assert_eq!(enabled.value["stream_options"]["include_usage"], true);
        for surface in profile::WireSurface::ALL
            .into_iter()
            .filter(|surface| *surface != profile::WireSurface::OpenaiChatCompletions)
        {
            let ignored = encode_request_for_surface(&request, surface, &options).unwrap();
            assert!(ignored.value.get("stream_options").is_none());
        }
    }

    #[test]
    fn notices_and_errors_retain_only_real_wire_origin() {
        use std::collections::BTreeMap;

        let mut semantic = CanonicalRequest::from_canonical("model", Vec::new());
        semantic.metadata = BTreeMap::from([("private-key".into(), "value".into())]);
        let notices =
            adaptation::request_notices(&semantic, profile::WireSurface::GeminiInteractions)
                .unwrap();
        assert!(!notices.is_empty());
        assert!(notices.iter().all(|notice| notice.source_surface.is_none()));
        let fidelity = plan_canonical_request_translation(
            &semantic,
            profile::WireSurface::GeminiInteractions,
            None,
        )
        .unwrap();
        assert_eq!(fidelity.source, None);
        semantic.reasoning.budget_tokens = Some(0);
        let error = encode_request_for_surface(
            &semantic,
            profile::WireSurface::OpenaiChatCompletions,
            &RequestEncodeOptions::default(),
        )
        .expect_err("invalid semantic reasoning input is rejected");
        assert_eq!(error.source_surface, None);

        for (surface, payload, expected) in [
            (
                ir::ClientSurface::ChatCompletions,
                serde_json::json!({"model":"model", "messages":[]}),
                profile::WireSurface::OpenaiChatCompletions,
            ),
            (
                ir::ClientSurface::Responses,
                serde_json::json!({"model":"model", "input":[]}),
                profile::WireSurface::OpenaiResponses,
            ),
            (
                ir::ClientSurface::Messages,
                serde_json::json!({"model":"model", "messages":[]}),
                profile::WireSurface::AnthropicMessages,
            ),
        ] {
            let mut decoded = decode::canonical_request_from_value_with_limits(
                &payload,
                surface,
                decode::DecodeLimits::current(),
            )
            .expect("valid client request");
            decoded.metadata.insert("projection".into(), "value".into());
            assert_eq!(decoded.origin.source_surface(), Some(expected));
            let decoded_notices =
                adaptation::request_notices(&decoded, profile::WireSurface::GeminiInteractions)
                    .unwrap();
            assert!(
                decoded_notices
                    .iter()
                    .all(|notice| notice.source_surface == Some(expected))
            );
            let decoded_fidelity = plan_canonical_request_translation(
                &decoded,
                profile::WireSurface::GeminiInteractions,
                None,
            )
            .unwrap();
            assert_eq!(decoded_fidelity.source, Some(expected));
        }
    }
}
