use eggpool::request::{AdmissionError, AdmissionOptions, admit_request};
use eggpool::wire::ir::{CanonicalBlockKind, ClientSurface, MediaSource};
use eggpool::wire::{
    AdaptationPolicy, AnthropicMessagesCodec, ConfiguredWireProfile, DecodedProviderPayload,
    GeminiGenerateContentCodec, LossPolicy, OpenAiChatCodec, WireCodec, WireCodecId,
    WireProfileDefinition, WireSurface,
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

#[test]
fn admission_preserves_bounded_media_forms_without_dereferencing_them() {
    let body = serde_json::to_vec(&json!({
        "model": "media-model",
        "messages": [{"role":"user", "content":[
            {"type":"image_url", "image_url":{"url":"https://example.invalid/image.png", "detail":"high"}},
            {"type":"file", "file":{"file_id":"file_fixture_1"}}
        ]}]
    }))
    .unwrap();
    let admitted = admit_request(
        &body,
        AdmissionOptions {
            client_surface: ClientSurface::ChatCompletions,
            ..AdmissionOptions::default()
        },
    )
    .unwrap();
    let blocks = &admitted.canonical.messages[0].content;
    assert_eq!(blocks[0].kind, CanonicalBlockKind::Image);
    assert_eq!(
        blocks[0].media.as_ref().unwrap().detail.as_deref(),
        Some("high")
    );
    assert_eq!(
        blocks[0].media.as_ref().unwrap().uri.as_deref(),
        Some("https://example.invalid/image.png")
    );
    assert_eq!(blocks[1].kind, CanonicalBlockKind::Document);
    assert_eq!(
        blocks[1].media.as_ref().unwrap().file_id.as_deref(),
        Some("file_fixture_1")
    );
    assert!(!format!("{admitted:?}").contains("example.invalid"));
}

#[test]
fn malformed_and_oversized_media_is_rejected_before_encoding() {
    let malformed = json!({
        "model":"m",
        "messages":[{"role":"user","content":[
            {"type":"image_url", "image_url":{"url":"data:image/png;base64,not-base64"}}
        ]}]
    });
    assert!(matches!(
        admit_request(
            &serde_json::to_vec(&malformed).unwrap(),
            AdmissionOptions::default()
        ),
        Err(AdmissionError::InvalidField {
            field: "media.data"
        })
    ));

    let oversized = json!({
        "model":"m",
        "messages":[{"role":"user","content":[
            {"type":"image_url", "image_url":{"url":format!("data:image/png;base64,{}", "A".repeat(8 * 1024 * 1024))}}
        ]}]
    });
    assert!(matches!(
        admit_request(
            &serde_json::to_vec(&oversized).unwrap(),
            AdmissionOptions::default()
        ),
        Err(AdmissionError::MediaLimit { kind: "image" })
    ));
}

#[test]
fn cross_wire_media_preserves_inline_and_reference_semantics() {
    let source = OpenAiChatCodec;
    let request = source
        .decode_client_request(
            &json!({
                "model":"m",
                "messages":[{"role":"user","content":[
                    {"type":"text","text":"inspect"},
                    {"type":"image_url","image_url":{"url":"data:image/png;base64,AAEC", "detail":"low"}},
                    {"type":"file","file":{"file_data":"https://example.invalid/doc.pdf"}}
                ]}]
            }),
            ClientSurface::ChatCompletions,
        )
        .unwrap()
        .value;
    let anthropic = AnthropicMessagesCodec
        .encode_request(
            &request,
            &profile(
                WireSurface::AnthropicMessages,
                WireCodecId::AnthropicMessages,
            ),
        )
        .unwrap();
    assert_eq!(
        anthropic.value["messages"][0]["content"][1]["source"]["type"],
        "base64"
    );
    assert_eq!(
        anthropic.value["messages"][0]["content"][2]["source"]["type"],
        "url"
    );
    assert!(
        anthropic
            .notices
            .iter()
            .any(|notice| notice.code.0 == "image_detail_not_representable")
    );

    let openai = source
        .encode_request(
            &request,
            &profile(WireSurface::OpenaiChatCompletions, WireCodecId::OpenaiChat),
        )
        .unwrap()
        .value;
    assert_eq!(
        openai["messages"][0]["content"][1]["image_url"]["url"],
        "data:image/png;base64,AAEC"
    );
    assert_eq!(
        openai["messages"][0]["content"][2]["file"]["file_data"],
        "https://example.invalid/doc.pdf"
    );
}

#[test]
fn tool_result_media_stays_nested_when_target_supports_it() {
    let request = OpenAiChatCodec
        .decode_client_request(
            &json!({
                "model":"m",
                "messages":[{"role":"tool", "tool_call_id":"call_1", "content":[
                    {"type":"text", "text":"see image"},
                    {"type":"image_url", "image_url":{"url":"https://example.invalid/tool.png"}}
                ]}]
            }),
            ClientSurface::ChatCompletions,
        )
        .unwrap()
        .value;
    let encoded = AnthropicMessagesCodec
        .encode_request(
            &request,
            &profile(
                WireSurface::AnthropicMessages,
                WireCodecId::AnthropicMessages,
            ),
        )
        .unwrap()
        .value;
    assert_eq!(encoded["messages"][0]["content"][0]["type"], "tool_result");
    assert_eq!(
        encoded["messages"][0]["content"][0]["content"][1]["type"],
        "image"
    );
}

#[test]
fn cache_markers_are_relocated_or_rejected_by_target_policy() {
    let source = OpenAiChatCodec;
    let request = source
        .decode_client_request(
            &json!({
                "model":"m",
                "messages":[{"role":"user","content":[
                    {"type":"text","text":"stable", "prompt_cache_breakpoint":{"mode":"explicit"}}
                ]}]
            }),
            ClientSurface::ChatCompletions,
        )
        .unwrap()
        .value;
    let anthropic = AnthropicMessagesCodec
        .encode_request(
            &request,
            &profile(
                WireSurface::AnthropicMessages,
                WireCodecId::AnthropicMessages,
            ),
        )
        .unwrap();
    assert_eq!(
        anthropic.value["messages"][0]["content"][0]["cache_control"]["type"],
        "ephemeral"
    );

    let rejected = GeminiGenerateContentCodec.encode_request_with_policy(
        &request,
        &profile(
            WireSurface::GeminiGenerateContent,
            WireCodecId::GeminiGenerateContent,
        ),
        &AdaptationPolicy {
            loss_policy: LossPolicy::Reject,
            ..AdaptationPolicy::default()
        },
    );
    assert!(rejected.is_err());
}

#[test]
fn finite_response_media_is_not_textified() {
    let codec = OpenAiChatCodec;
    let decoded = codec
        .decode_response(
            &json!({
                "id":"c",
                "model":"m",
                "choices":[{"message":{"role":"assistant","content":[
                    {"type":"text","text":"see"},
                    {"type":"image_url","image_url":{"url":"https://example.invalid/out.png"}},
                    {"type":"file","file":{"file_data":"https://example.invalid/out.pdf"}}
                ]},"finish_reason":"stop"}]
            }),
            200,
        )
        .unwrap()
        .value;
    let DecodedProviderPayload::Response(response) = decoded else {
        panic!("response expected")
    };
    assert_eq!(response.output[1].kind, CanonicalBlockKind::Image);
    assert_eq!(response.output[2].kind, CanonicalBlockKind::Document);
    let rendered = codec
        .encode_response(&response, ClientSurface::ChatCompletions)
        .unwrap()
        .value;
    assert_eq!(
        rendered["choices"][0]["message"]["content"][1]["image_url"]["url"],
        "https://example.invalid/out.png"
    );
}

#[test]
fn media_debug_never_contains_inline_data() {
    let media = MediaSource {
        media_type: Some("image/png".into()),
        data: Some("SECRET_SENTINEL".into()),
        uri: None,
        detail: None,
        file_id: None,
    };
    assert!(!format!("{media:?}").contains("SECRET_SENTINEL"));
}
