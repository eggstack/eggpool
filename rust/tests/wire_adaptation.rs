use eggpool::catalog::{CapabilityStatus, ThinkingCapability};
use eggpool::wire::ir::CanonicalRequest;
use eggpool::wire::ir::ClientSurface;
use eggpool::wire::{
    AdaptationPolicy, CapabilityDisposition, CodecReasonCode, ConfiguredWireProfile,
    GeminiGenerateContentCodec, GeminiInteractionsCodec, LossPolicy, OpenAiChatCodec,
    OpenAiResponsesCodec, ReasoningCapabilityPolicy, WireCodec, WireCodecId, WireProfileDefinition,
    WireSurface, reasoning_capability_notices, stable_tool_call_id,
};
use serde_json::json;

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

fn request(codec: &OpenAiChatCodec, body: serde_json::Value) -> CanonicalRequest {
    codec
        .decode_client_request(&body, ClientSurface::ChatCompletions)
        .expect("canonical request")
        .value
}

#[test]
fn one_policy_rejects_every_family_loss_without_raw_content() {
    let source = OpenAiChatCodec;
    let request = request(
        &source,
        json!({
            "model": "model-a",
            "messages": [{"role":"user","content":"return JSON"}],
            "response_format": {"type":"json_schema", "json_schema": {
                "name":"answer", "strict":true, "schema":{"type":"object"}
            }},
            "reasoning_effort": "high",
            "metadata": {"safe":"x"}
        }),
    );
    let policy = AdaptationPolicy {
        loss_policy: LossPolicy::Reject,
        ..AdaptationPolicy::default()
    };

    let cases: Vec<Box<dyn WireCodec>> = vec![
        Box::new(eggpool::wire::AnthropicMessagesCodec),
        Box::new(OpenAiResponsesCodec),
        Box::new(GeminiInteractionsCodec),
        Box::new(GeminiGenerateContentCodec),
    ];
    let profiles = [
        profile(
            WireSurface::AnthropicMessages,
            WireCodecId::AnthropicMessages,
        ),
        profile(WireSurface::OpenaiResponses, WireCodecId::OpenaiResponses),
        profile(
            WireSurface::GeminiInteractions,
            WireCodecId::GeminiInteractions,
        ),
        profile(
            WireSurface::GeminiGenerateContent,
            WireCodecId::GeminiGenerateContent,
        ),
    ];
    for (codec, profile) in cases.into_iter().zip(profiles) {
        let error = codec
            .encode_request_with_policy(&request, &profile, &policy)
            .expect_err("material adaptation must be rejected");
        assert_eq!(error.reason, CodecReasonCode::LossRejected);
        assert!(
            !error
                .field
                .as_deref()
                .unwrap_or_default()
                .contains("return JSON")
        );
    }
}

#[test]
fn warn_policy_is_bounded_and_structural() {
    let source = OpenAiChatCodec;
    let request = request(
        &source,
        json!({
            "model": "model-a",
            "messages": [{"role":"user","content":"secret prompt"}],
            "response_format": {"type":"json_schema", "json_schema": {
                "name":"private", "schema":{"type":"object", "description":"secret schema"}
            }},
            "reasoning": {"enabled": true}
        }),
    );
    let output = eggpool::wire::AnthropicMessagesCodec
        .encode_request(
            &request,
            &profile(
                WireSurface::AnthropicMessages,
                WireCodecId::AnthropicMessages,
            ),
        )
        .expect("warn policy should preserve the encoded request");
    assert!(!output.notices.is_empty());
    assert!(output.notices.len() <= 32);
    for warning in output.notices {
        assert!(!warning.code.0.contains("secret"));
        assert!(
            !warning
                .field
                .as_deref()
                .unwrap_or_default()
                .contains("private")
        );
        assert!(
            !warning
                .field
                .as_deref()
                .unwrap_or_default()
                .contains("prompt")
        );
    }
}

#[test]
fn explicit_disable_is_not_silently_dropped() {
    let source = OpenAiChatCodec;
    let request = request(
        &source,
        json!({
            "model":"model-a",
            "messages":[{"role":"user","content":"hello"}],
            "reasoning": false
        }),
    );
    let encoded = source
        .encode_request(
            &request,
            &profile(WireSurface::OpenaiChatCompletions, WireCodecId::OpenaiChat),
        )
        .expect("native request")
        .value;
    assert_eq!(encoded["reasoning_effort"], "none");

    let encoded = OpenAiResponsesCodec
        .encode_request(
            &request,
            &profile(WireSurface::OpenaiResponses, WireCodecId::OpenaiResponses),
        )
        .expect("Responses request")
        .value;
    assert_eq!(encoded["reasoning"]["effort"], "none");
}

#[test]
fn malformed_and_duplicate_tool_identity_is_rejected_at_admission() {
    let codec = OpenAiChatCodec;
    let missing = codec.decode_client_request(
        &json!({
            "model":"model-a",
            "messages":[{"role":"assistant","content":null,"tool_calls":[
                {"id":"", "type":"function", "function":{"name":"lookup","arguments":"{}"}}
            ]}]
        }),
        ClientSurface::ChatCompletions,
    );
    assert_eq!(
        missing.expect_err("empty call id must fail").reason,
        CodecReasonCode::MalformedSourceRequest
    );

    let duplicate = codec.decode_client_request(
        &json!({
            "model":"model-a",
            "messages":[{"role":"assistant","content":null,"tool_calls":[
                {"id":"call-1", "type":"function", "function":{"name":"a","arguments":"{}"}},
                {"id":"call-1", "type":"function", "function":{"name":"b","arguments":"{}"}}
            ]}]
        }),
        ClientSurface::ChatCompletions,
    );
    assert_eq!(
        duplicate.expect_err("duplicate call id must fail").reason,
        CodecReasonCode::MalformedSourceRequest
    );
}

#[test]
fn provider_without_ids_gets_repeatable_compatibility_identity() {
    let codec = GeminiGenerateContentCodec;
    let payload = json!({
        "candidates":[{"content":{"role":"model","parts":[
            {"functionCall":{"name":"lookup","args":{"q":"egg"}}}
        ]},"finishReason":"STOP"}]
    });
    let first = codec
        .decode_response(&payload, 200)
        .expect("response")
        .value;
    let second = codec
        .decode_response(&payload, 200)
        .expect("response")
        .value;
    let eggpool::wire::DecodedProviderPayload::Response(first) = first else {
        panic!("response expected")
    };
    let eggpool::wire::DecodedProviderPayload::Response(second) = second else {
        panic!("response expected")
    };
    assert_eq!(first.output[0].call_id, second.output[0].call_id);
    assert_eq!(
        first.output[0].call_id.as_deref(),
        Some(stable_tool_call_id("lookup", "{\"q\":\"egg\"}", 0).as_str())
    );
}

#[test]
fn capability_status_is_an_explicit_pure_input() {
    let source = OpenAiChatCodec;
    let request = request(
        &source,
        json!({
            "model":"model-a",
            "messages":[{"role":"user","content":"think"}],
            "reasoning_effort":"high"
        }),
    );
    let capability = ThinkingCapability {
        status: CapabilityStatus::Unsupported,
        effort: CapabilityStatus::Unsupported,
        ..ThinkingCapability::default()
    };
    let policy = ReasoningCapabilityPolicy {
        unsupported: CapabilityDisposition::AllowWithWarning,
        ..ReasoningCapabilityPolicy::default()
    };
    let notices = reasoning_capability_notices(
        &request,
        &capability,
        &policy,
        WireSurface::OpenaiChatCompletions,
    )
    .expect("allow-with-warning is pure");
    assert_eq!(notices.len(), 1);
    assert_eq!(notices[0].code.0, "reasoning_capability_uncertain");
    assert_eq!(capability.status, CapabilityStatus::Unsupported);
}
