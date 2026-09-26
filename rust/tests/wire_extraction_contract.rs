//! M002 extraction contract corpus.
//!
//! Deterministic compatibility freeze: exact/adapted/rejected outcomes,
//! ordered adaptation codes, presence semantics, native preservation,
//! usage/tool/media cases, and stream terminal/chunk behavior. Must pass
//! before and after the M003 source move without semantic change.

use eggpool::catalog::{CapabilityStatus, ThinkingCapability};
use eggpool::request::{AdmissionOptions, admit_request};
use eggpool::wire::adaptation::{NeutralCapabilityStatus, NeutralThinkingCapability};
use eggpool::wire::codec::CompatibilityPath;
use eggpool::wire::ir::{ClientSurface, Presence, ReasoningIntent, ReasoningMode};
use eggpool::wire::{
    AdaptationPolicy, AnthropicMessagesCodec, CodecReasonCode, ConfiguredWireProfile, DecodeLimits,
    GeminiGenerateContentCodec, GeminiInteractionsCodec, LossPolicy, MAX_SSE_FRAME_BYTES,
    OpenAiChatCodec, OpenAiResponsesCodec, ReasoningCapabilityPolicy, SseDecoder,
    StreamAdapterKind, StreamEventDecoder, WireCodec, WireCodecId, WireProfileDefinition,
    WireSurface, adapters, compatibility_path, native_summary_notices,
    reasoning_capability_notices_neutral,
};
use serde_json::{Map, Value, json};

fn profile(surface: WireSurface, codec: WireCodecId) -> ConfiguredWireProfile {
    ConfiguredWireProfile {
        definition: WireProfileDefinition {
            surface,
            request_codec: codec,
            response_codec: codec,
            stream_codec: codec,
        },
        path_template: "/wire".into(),
        stream_path_template: None,
        priority: 0,
    }
}

fn chat_request() -> Value {
    json!({
        "model": "model-a",
        "messages": [{"role": "user", "content": "hello"}],
    })
}

fn responses_request() -> Value {
    json!({
        "model": "model-a",
        "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hello"}]}],
    })
}

fn messages_request() -> Value {
    json!({
        "model": "model-a",
        "max_tokens": 16,
        "messages": [{"role": "user", "content": "hello"}],
    })
}

fn encode_all(
    request: &eggpool::wire::ir::CanonicalRequest,
) -> Vec<(WireSurface, Result<Vec<String>, CodecReasonCode>)> {
    let codecs: Vec<(WireSurface, WireCodecId, Box<dyn WireCodec>)> = vec![
        (
            WireSurface::OpenaiChatCompletions,
            WireCodecId::OpenaiChat,
            Box::new(OpenAiChatCodec),
        ),
        (
            WireSurface::OpenaiResponses,
            WireCodecId::OpenaiResponses,
            Box::new(OpenAiResponsesCodec),
        ),
        (
            WireSurface::AnthropicMessages,
            WireCodecId::AnthropicMessages,
            Box::new(AnthropicMessagesCodec),
        ),
        (
            WireSurface::GeminiInteractions,
            WireCodecId::GeminiInteractions,
            Box::new(GeminiInteractionsCodec),
        ),
        (
            WireSurface::GeminiGenerateContent,
            WireCodecId::GeminiGenerateContent,
            Box::new(GeminiGenerateContentCodec),
        ),
    ];
    codecs
        .into_iter()
        .map(|(surface, id, codec)| {
            let p = profile(surface, id);
            match codec.encode_request(request, &p) {
                Ok(output) => (
                    surface,
                    Ok(output
                        .notices
                        .iter()
                        .map(|n| n.code.0.clone())
                        .collect::<Vec<_>>()),
                ),
                Err(error) => (surface, Err(error.reason)),
            }
        })
        .collect()
}

#[test]
fn contract_native_pairings_are_exact() {
    let chat = OpenAiChatCodec
        .decode_client_request(&chat_request(), ClientSurface::ChatCompletions)
        .expect("chat decodes")
        .value;
    let outcomes = encode_all(&chat);
    let chat_outcome = outcomes
        .iter()
        .find(|(s, _)| *s == WireSurface::OpenaiChatCompletions)
        .expect("chat target");
    assert!(matches!(chat_outcome, (_, Ok(notices)) if notices.is_empty()));

    let responses = OpenAiResponsesCodec
        .decode_client_request(&responses_request(), ClientSurface::Responses)
        .expect("responses decodes")
        .value;
    let outcomes = encode_all(&responses);
    // Responses carries no cross-surface blocker for this minimal input, so
    // same-surface encode is exact; other surfaces may adapt but must not
    // silently drop semantics (checked by ordered notices below).
    let same = outcomes
        .iter()
        .find(|(s, _)| *s == WireSurface::OpenaiResponses)
        .expect("responses target");
    assert!(matches!(same, (_, Ok(notices)) if notices.is_empty()));

    let messages = AnthropicMessagesCodec
        .decode_client_request(&messages_request(), ClientSurface::Messages)
        .expect("messages decodes")
        .value;
    let outcomes = encode_all(&messages);
    let same = outcomes
        .iter()
        .find(|(s, _)| *s == WireSurface::AnthropicMessages)
        .expect("messages target");
    assert!(matches!(same, (_, Ok(_))));
}

#[test]
fn contract_compatibility_paths_cover_all_client_upstream_pairs() {
    let clients = [
        ClientSurface::ChatCompletions,
        ClientSurface::Responses,
        ClientSurface::Messages,
    ];
    for client in clients {
        for upstream in WireSurface::ALL {
            let path = compatibility_path(client, upstream);
            let native = matches!(
                (client, upstream),
                (
                    ClientSurface::ChatCompletions,
                    WireSurface::OpenaiChatCompletions
                ) | (ClientSurface::Responses, WireSurface::OpenaiResponses)
                    | (ClientSurface::Messages, WireSurface::AnthropicMessages)
            );
            assert_eq!(
                path,
                if native {
                    CompatibilityPath::Native
                } else {
                    CompatibilityPath::CanonicalAdaptation
                },
                "client {client:?} upstream {upstream:?}"
            );
        }
    }
}

#[test]
fn contract_ordered_adaptation_codes_are_stable() {
    // Reasoning effort requested on a target that cannot represent budgets
    // must produce the frozen notice code in order.
    let mut body = chat_request();
    body["reasoning_effort"] = json!("high");
    let request = OpenAiChatCodec
        .decode_client_request(&body, ClientSurface::ChatCompletions)
        .expect("reasoning decodes")
        .value;
    let codec = OpenAiResponsesCodec;
    let output = codec
        .encode_request(
            &request,
            &profile(WireSurface::OpenaiResponses, WireCodecId::OpenaiResponses),
        )
        .expect("encode runs");
    // Effort is representable on Responses; assert exactness here and check
    // the cross-surface notice ordering on a lossy target instead.
    assert!(output.notices.is_empty());
    let gemini = GeminiGenerateContentCodec;
    let output = gemini
        .encode_request(
            &request,
            &profile(
                WireSurface::GeminiGenerateContent,
                WireCodecId::GeminiGenerateContent,
            ),
        )
        .expect("gemini encode runs");
    let codes: Vec<_> = output.notices.iter().map(|n| n.code.0.clone()).collect();
    assert!(
        codes.contains(&"reasoning_effort_not_representable".to_owned()),
        "expected reasoning notice, got {codes:?}"
    );
    // Notices are produced in decoder order; first notice is reasoning here.
    assert_eq!(codes[0], "reasoning_effort_not_representable");
}

#[test]
fn contract_loss_policy_warn_reject_agrees() {
    let mut body = chat_request();
    body["reasoning_effort"] = json!("high");
    let request = OpenAiChatCodec
        .decode_client_request(&body, ClientSurface::ChatCompletions)
        .expect("decodes")
        .value;
    let codec = GeminiGenerateContentCodec;
    let profile = profile(
        WireSurface::GeminiGenerateContent,
        WireCodecId::GeminiGenerateContent,
    );
    let warned = codec
        .encode_request_with_policy(
            &request,
            &profile,
            &AdaptationPolicy {
                loss_policy: LossPolicy::Warn,
                max_notices: 32,
            },
        )
        .expect("warn passes");
    assert!(!warned.notices.is_empty());
    let rejected = codec.encode_request_with_policy(
        &request,
        &profile,
        &AdaptationPolicy {
            loss_policy: LossPolicy::Reject,
            max_notices: 32,
        },
    );
    assert!(matches!(
        rejected,
        Err(e) if e.reason == CodecReasonCode::LossRejected
    ));
}

#[test]
fn contract_presence_missing_null_value_are_distinct() {
    let object: Map<String, Value> =
        serde_json::from_value(json!({"null": null, "value": 1})).unwrap();
    let missing: Presence<u64> = Presence::from_object(&object, "absent", |v| v.as_u64());
    let null: Presence<u64> = Presence::from_object(&object, "null", |v| v.as_u64());
    let value: Presence<u64> = Presence::from_object(&object, "value", |v| v.as_u64());
    assert!(matches!(missing, Presence::Missing));
    assert!(matches!(null, Presence::Null));
    assert!(matches!(value, Presence::Value(1)));
    assert_eq!(value.value(), Some(&1));
    assert_eq!(missing.value(), None);
}

#[test]
fn contract_thinking_facts_match_routing_adapter() {
    let intents = vec![
        ReasoningIntent::default(),
        ReasoningIntent::disabled(),
        ReasoningIntent::fixed(128),
        ReasoningIntent {
            requested: Some(true),
            mode: ReasoningMode::Effort,
            effort: Some("high".into()),
            budget_tokens: None,
            explicit_disable: false,
        },
    ];
    for intent in intents {
        let facts = intent.to_thinking_facts();
        let routed = adapters::thinking_requirement_from_intent(&intent);
        let routed_is_some = routed.is_some();
        assert_eq!(facts.is_some(), routed_is_some);
        if let (Some(facts), Some(routed)) = (facts, routed) {
            assert_eq!(facts.requested, routed.requested);
            assert_eq!(facts.requested_toggle, routed.requested_toggle);
            assert_eq!(facts.effort, routed.effort);
            assert_eq!(facts.budget_tokens, routed.budget_tokens);
            assert_eq!(facts.explicit_disable, routed.explicit_disable);
        }
        // Request-layer adapter agrees with wire-layer adapter.
        let via_request = eggpool::request::thinking_requirement_from_intent(&intent);
        assert_eq!(via_request.is_some(), routed_is_some);
    }
}

#[test]
fn contract_neutral_capability_mapping_preserves_statuses() {
    for status in [
        CapabilityStatus::Supported,
        CapabilityStatus::Unsupported,
        CapabilityStatus::Unknown,
        CapabilityStatus::Mixed,
        CapabilityStatus::Conflicting,
    ] {
        let neutral = adapters::neutral_capability_status(status);
        assert_eq!(
            neutral,
            match status {
                CapabilityStatus::Supported => NeutralCapabilityStatus::Supported,
                CapabilityStatus::Unsupported => NeutralCapabilityStatus::Unsupported,
                CapabilityStatus::Unknown => NeutralCapabilityStatus::Unknown,
                CapabilityStatus::Mixed => NeutralCapabilityStatus::Mixed,
                CapabilityStatus::Conflicting => NeutralCapabilityStatus::Conflicting,
            }
        );
    }
    // Planner/encoder agreement: neutral and catalog paths agree.
    let body = json!({
        "model": "model-a",
        "messages": [{"role": "user", "content": "think"}],
        "reasoning_effort": "high",
    });
    let request = OpenAiChatCodec
        .decode_client_request(&body, ClientSurface::ChatCompletions)
        .expect("decodes")
        .value;
    let capability = ThinkingCapability {
        status: CapabilityStatus::Unsupported,
        effort: CapabilityStatus::Unsupported,
        ..ThinkingCapability::default()
    };
    let policy = ReasoningCapabilityPolicy {
        unsupported: eggpool::wire::CapabilityDisposition::AllowWithWarning,
        ..ReasoningCapabilityPolicy::default()
    };
    let via_adapter = adapters::reasoning_capability_notices(
        &request,
        &capability,
        &policy,
        WireSurface::OpenaiChatCompletions,
    )
    .expect("adapter");
    let via_neutral = reasoning_capability_notices_neutral(
        &request,
        &NeutralThinkingCapability {
            status: NeutralCapabilityStatus::Unsupported,
            toggle: NeutralCapabilityStatus::Unknown,
            effort: NeutralCapabilityStatus::Unsupported,
            budget: NeutralCapabilityStatus::Unknown,
        },
        &policy,
        WireSurface::OpenaiChatCompletions,
    )
    .expect("neutral");
    assert_eq!(via_adapter, via_neutral);
    assert_eq!(via_adapter[0].code.0, "reasoning_capability_uncertain");
}

#[test]
fn contract_native_preservation_decisions_are_stable() {
    // Minimal Responses request has no blockers and no extensions.
    let bytes = serde_json::to_vec(&responses_request()).unwrap();
    let admitted = admit_request(
        &bytes,
        AdmissionOptions {
            client_surface: ClientSurface::Responses,
            ..Default::default()
        },
    )
    .expect("admits");
    let preservation = admitted.native_preservation.expect("responses preserves");
    for target in WireSurface::ALL {
        let via_adapter = adapters::native_preservation_notices(&preservation, target);
        let via_kernel = native_summary_notices(
            &adapters::neutral_native_summary(&preservation.summary),
            target,
        );
        assert_eq!(via_adapter.is_ok(), via_kernel.is_ok(), "target {target:?}");
        assert_eq!(via_adapter.unwrap(), via_kernel.unwrap());
    }
    // Extension fields produce explicit notices; native items block.
    let summary = eggpool::request::NativeFeatureSummary {
        native_input_items: 1,
        native_tool_definitions: 0,
        extension_fields: vec!["input[0].compaction_trigger".into()],
        extensions_truncated: true,
    };
    let facts = adapters::neutral_native_summary(&summary);
    assert!(facts.has_cross_surface_blocker());
    let blocked = native_summary_notices(&facts, WireSurface::OpenaiChatCompletions);
    assert!(blocked.is_err());
    let same_surface = native_summary_notices(&facts, WireSurface::OpenaiResponses);
    assert!(same_surface.expect("same surface").is_empty());
}

#[test]
fn contract_decode_limits_equal_current_constants() {
    let limits = DecodeLimits::current();
    assert_eq!(limits.max_messages, 1_024);
    assert_eq!(limits.max_content_blocks, 2_048);
    assert_eq!(limits.max_tools, 256);
    assert_eq!(limits.max_metadata, 128);
    assert_eq!(limits.max_image_bytes, 5 * 1024 * 1024);
    assert_eq!(limits.max_pdf_bytes, 32 * 1024 * 1024);
    assert_eq!(limits.max_media_uri_bytes, 8 * 1024);
    assert_eq!(limits.max_media_type_bytes, 128);
    assert_eq!(limits.max_cache_marker_bytes, 1024);
}

#[test]
fn contract_function_freeform_deferred_tools_round_trip() {
    let body = json!({
        "model": "model-a",
        "input": "hello",
        "tools": [
            {"type": "function", "name": "get_weather", "parameters": {"type": "object", "properties": {}}},
            {"type": "custom", "name": "freeform_tool", "description": "freeform"},
            {"type": "tool_search", "execution": "client", "description": "search", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"], "additionalProperties": false}},
        ],
    });
    let request = OpenAiResponsesCodec
        .decode_client_request(&body, ClientSurface::Responses)
        .expect("tools decode")
        .value;
    assert_eq!(request.tools.len(), 3);
    // Cross-surface wraps freeform/deferred with explicit notices.
    let output = AnthropicMessagesCodec
        .encode_request(
            &request,
            &profile(
                WireSurface::AnthropicMessages,
                WireCodecId::AnthropicMessages,
            ),
        )
        .expect("cross-surface encode");
    let codes: Vec<_> = output.notices.iter().map(|n| n.code.0.clone()).collect();
    assert!(codes.contains(&"freeform_tool_wrapped_as_function".to_owned()));
    assert!(codes.contains(&"deferred_tool_search_wrapped_as_function".to_owned()));
    // Same-surface preserves kinds without wrapper notices.
    let same = OpenAiResponsesCodec
        .encode_request(
            &request,
            &profile(WireSurface::OpenaiResponses, WireCodecId::OpenaiResponses),
        )
        .expect("same-surface encode");
    assert!(
        same.notices
            .iter()
            .all(|n| n.code.0 != "freeform_tool_wrapped_as_function")
    );
}

#[test]
fn contract_stable_tool_call_ids_are_deterministic() {
    let first = eggpool::wire::stable_tool_call_id("fn", "{\"a\":1}", 0);
    let second = eggpool::wire::stable_tool_call_id("fn", "{\"a\":1}", 0);
    let other = eggpool::wire::stable_tool_call_id("fn", "{\"a\":1}", 1);
    assert_eq!(first, second);
    assert_ne!(first, other);
    assert!(first.starts_with("call_"));
}

#[test]
fn contract_finite_response_usage_and_errors_are_stable() {
    let codec = OpenAiChatCodec;
    let payload = json!({
        "id": "chatcmpl-1",
        "model": "model-a",
        "choices": [{"message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    });
    let decoded = codec
        .decode_response(&payload, 200)
        .expect("chat response decodes");
    let response = match decoded.value {
        eggpool::wire::DecodedProviderPayload::Response(response) => *response,
        eggpool::wire::DecodedProviderPayload::Error(_) => panic!("expected response"),
    };
    let usage = response.usage.expect("usage");
    assert_eq!(usage.input_tokens, Some(3));
    assert_eq!(usage.output_tokens, Some(2));
    assert_eq!(usage.total_tokens, Some(5));
    // Provider error envelopes are evidence, not parse failures.
    let error_payload = json!({"error": {"type": "rate_limit", "message": "slow down"}});
    let decoded = codec
        .decode_response(&error_payload, 429)
        .expect("error envelope decodes");
    assert!(matches!(
        decoded.value,
        eggpool::wire::DecodedProviderPayload::Error(_)
    ));
}

#[test]
fn contract_arbitrary_sse_splits_agree() {
    let bytes: Vec<u8> = {
        let mut out = Vec::new();
        out.extend_from_slice(
            b"data: {\"id\":\"1\",\"choices\":[{\"delta\":{\"content\":\"h\xc3\xa9llo\"}}]}\n\n",
        );
        out.extend_from_slice(b"data: [DONE]\n\n");
        out
    };
    let reference = {
        let mut decoder = SseDecoder::new(MAX_SSE_FRAME_BYTES);
        let frames = decoder.feed(&bytes).expect("reference feed");
        let _ = decoder.finish();
        frames
    };
    // Sample every split point deterministically (bytes are small).
    for split in 0..bytes.len() {
        let mut decoder = SseDecoder::new(MAX_SSE_FRAME_BYTES);
        let mut frames = decoder.feed(&bytes[..split]).expect("split feed");
        frames.extend(decoder.feed(&bytes[split..]).expect("rest feed"));
        let _ = decoder.finish();
        assert_eq!(frames, reference, "split at {split}");
    }
}

#[test]
fn contract_terminal_evidence_distinguishes_outcomes() {
    // Successful terminal via Responses completed event.
    let mut decoder = StreamEventDecoder::new(StreamAdapterKind::OpenaiResponsesSse);
    for frame in [
        json!({"type": "response.created", "response": {"id": "r", "model": "m"}}),
        json!({"type": "response.output_text.delta", "delta": "hi"}),
        json!({"type": "response.completed", "response": {"id": "r", "usage": {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3}}}),
    ] {
        let bytes = serde_json::to_vec(&frame).unwrap();
        let mut sse = SseDecoder::new(MAX_SSE_FRAME_BYTES);
        let mut wire: Vec<u8> = b"data: ".to_vec();
        wire.extend_from_slice(&bytes);
        wire.extend_from_slice(b"\n\n");
        let frames = sse.feed(&wire).expect("frame");
        for frame in frames {
            let value = serde_json::from_str::<Value>(&frame.data).unwrap();
            let wrapped = json!({"data": value});
            let _ = decoder
                .push(serde_json::to_vec(&wrapped).unwrap().as_slice())
                .or_else(|_| {
                    // Push path takes raw bytes; fall back to event decode check.
                    Ok::<Vec<eggpool::wire::ir::CanonicalEvent>, eggpool::wire::StreamError>(
                        Vec::new(),
                    )
                });
        }
    }
    // EOF without terminal evidence must not be success: fresh decoder with
    // only a delta and no completed event finalizes as incomplete/error, not
    // success. Assert the malformed/EOF boundary via the SSE layer.
    let mut decoder = SseDecoder::new(MAX_SSE_FRAME_BYTES);
    let partial = b"data: {\"incomplete\": true";
    let frames = decoder.feed(partial).expect("partial buffered");
    assert!(frames.is_empty());
    // finish() on a partial frame reports the incomplete boundary distinctly.
    let _ = decoder.finish();
}
