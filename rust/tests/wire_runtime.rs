use std::collections::BTreeSet;

use eggpool::wire::ir::ClientSurface;
use eggpool::{
    request::StaticRoutingFacts,
    wire::{
        ConfiguredWireProfile, FiniteResponseOutcome, StreamTerminalOutcome, WireCodecId,
        WireProfileDefinition, WireProfileFlags, WireRuntime, WireRuntimeContext, WireRuntimeError,
        WireSurface,
    },
};
use serde_json::{Value, json};

fn profile(surface: WireSurface) -> ConfiguredWireProfile {
    let (request_codec, response_codec, stream_codec) = match surface {
        WireSurface::OpenaiChatCompletions => (
            WireCodecId::OpenaiChat,
            WireCodecId::OpenaiChat,
            WireCodecId::OpenaiChatSse,
        ),
        WireSurface::OpenaiResponses => (
            WireCodecId::OpenaiResponses,
            WireCodecId::OpenaiResponses,
            WireCodecId::OpenaiResponsesSse,
        ),
        WireSurface::AnthropicMessages => (
            WireCodecId::AnthropicMessages,
            WireCodecId::AnthropicMessages,
            WireCodecId::AnthropicMessagesSse,
        ),
        WireSurface::GeminiInteractions => (
            WireCodecId::GeminiInteractions,
            WireCodecId::GeminiInteractions,
            WireCodecId::GeminiInteractionsSse,
        ),
        WireSurface::GeminiGenerateContent => (
            WireCodecId::GeminiGenerateContent,
            WireCodecId::GeminiGenerateContent,
            WireCodecId::GeminiGenerateContentSse,
        ),
    };
    ConfiguredWireProfile {
        definition: WireProfileDefinition {
            surface,
            request_codec,
            response_codec,
            stream_codec,
        },
        path_template: "/selected".into(),
        stream_path_template: Some("/selected".into()),
        priority: 0,
    }
}

fn context(surface: WireSurface, client: ClientSurface) -> WireRuntimeContext {
    let mut context =
        WireRuntimeContext::new(client, profile(surface), "source-model", "upstream-model");
    context.provider_id = Some("provider-a".into());
    context.provider_kind = Some("fixture".into());
    context
}

#[test]
fn one_facade_prepares_every_selected_profile_without_changing_source_ir() {
    let runtime = WireRuntime::embedded().expect("embedded registry");
    let body =
        br#"{"model":"source-model","messages":[{"role":"user","content":"hello"}],"stream":true}"#;
    for surface in WireSurface::ALL {
        let context = context(surface, ClientSurface::ChatCompletions);
        let prepared = runtime
            .prepare_request(body, &context)
            .expect("prepared request");
        assert_eq!(prepared.identity.profile, surface);
        assert_eq!(prepared.identity.canonical_model_id, "source-model");
        assert_eq!(prepared.identity.upstream_model_id, "upstream-model");
        assert_eq!(prepared.canonical.model, "source-model");
        assert!(prepared.body.value.is_some());
        assert_eq!(prepared.metadata.message_count, 1);
        assert_eq!(prepared.metadata.text_block_count, 1);
        assert!(prepared.stream.requested);
        assert_eq!(prepared.bytes.input_bytes, body.len());
        assert_eq!(prepared.bytes.output_bytes, prepared.body.bytes.len());
    }
}

#[test]
fn native_same_surface_request_uses_caller_bytes_without_reencoding() {
    let runtime = WireRuntime::embedded().expect("embedded registry");
    let body = br#" { "model": "source-model", "messages": [] } "#;
    let mut context = context(
        WireSurface::OpenaiChatCompletions,
        ClientSurface::ChatCompletions,
    );
    context.upstream_model_id = "source-model".into();
    context.profile_flags = WireProfileFlags::for_surfaces(
        ClientSurface::ChatCompletions,
        WireSurface::OpenaiChatCompletions,
    );
    let prepared = runtime
        .prepare_request(body, &context)
        .expect("prepared request");
    assert_eq!(prepared.body.value, None);
    assert_eq!(prepared.body.bytes.as_ref(), body);
    assert_eq!(prepared.adaptation.warning_count, 0);
}

#[test]
fn selected_profile_mismatch_is_typed_and_cannot_fall_back() {
    let runtime = WireRuntime::embedded().expect("embedded registry");
    let mut context = context(
        WireSurface::OpenaiChatCompletions,
        ClientSurface::ChatCompletions,
    );
    context.selected_profile.definition.response_codec = WireCodecId::AnthropicMessages;
    let error = runtime
        .prepare_request(br#"{"model":"source-model"}"#, &context)
        .expect_err("changed definition must fail");
    assert!(matches!(
        error,
        WireRuntimeError::ProfileMismatch {
            reason: eggpool::wire::ProfileMismatchReason::DefinitionChanged,
            ..
        }
    ));
}

fn finite_payload(surface: WireSurface) -> Value {
    match surface {
        WireSurface::OpenaiChatCompletions => json!({
            "id":"chat-1", "model":"upstream-model",
            "choices":[{"message":{"role":"assistant","content":"done"},"finish_reason":"stop"}]
        }),
        WireSurface::OpenaiResponses => json!({
            "id":"response-1", "model":"upstream-model", "status":"completed",
            "output":[{"type":"message","content":[{"type":"output_text","text":"done"}]}],
            "usage":{"input_tokens":2,"output_tokens":1,"total_tokens":3}
        }),
        WireSurface::AnthropicMessages => json!({
            "id":"message-1", "model":"upstream-model",
            "content":[{"type":"text","text":"done"}], "stop_reason":"end_turn",
            "usage":{"input_tokens":2,"output_tokens":1}
        }),
        WireSurface::GeminiInteractions => json!({
            "interaction":{"id":"interaction-1","model":"upstream-model","status":"completed",
            "steps":[{"type":"model_output","content":[{"text":"done"}]}],
            "usage":{"total_input_tokens":2,"total_output_tokens":1,"total_tokens":3}}
        }),
        WireSurface::GeminiGenerateContent => json!({
            "responseId":"gemini-1", "modelVersion":"upstream-model",
            "candidates":[{"content":{"role":"model","parts":[{"text":"done"}]},"finishReason":"STOP"}],
            "usageMetadata":{"promptTokenCount":2,"candidatesTokenCount":1,"totalTokenCount":3}
        }),
    }
}

#[test]
fn finite_results_separate_success_provider_error_and_malformed_response() {
    let runtime = WireRuntime::embedded().expect("embedded registry");
    for surface in WireSurface::ALL {
        let context = context(surface, ClientSurface::ChatCompletions);
        let body = serde_json::to_vec(&finite_payload(surface)).unwrap();
        let result = runtime
            .decode_finite_response(&body, 200, &context, true)
            .expect("finite response");
        assert!(matches!(result.outcome, FiniteResponseOutcome::Success(_)));
        if surface != WireSurface::OpenaiChatCompletions {
            assert_eq!(
                result.usage.as_ref().and_then(|usage| usage.input_tokens),
                Some(2)
            );
        }
        assert_eq!(
            result.client_body.as_ref().unwrap().bytes.len(),
            result.bytes.output_bytes
        );

        let error_body = br#"{"error":{"type":"rate_limit","message":"retry"}}"#;
        let error = runtime
            .decode_finite_response(error_body, 429, &context, true)
            .expect("provider error evidence");
        assert!(matches!(
            error.outcome,
            FiniteResponseOutcome::ProviderError(_)
        ));
        assert!(error.client_body.is_none());

        let malformed = runtime
            .decode_finite_response(b"{", 200, &context, true)
            .expect("malformed response evidence");
        assert!(matches!(
            malformed.outcome,
            FiniteResponseOutcome::Malformed { .. }
        ));
    }
}

fn stream_fixture(surface: WireSurface) -> Vec<u8> {
    match surface {
        WireSurface::OpenaiChatCompletions => {
            b"data: {\"choices\":[{\"delta\":{\"content\":\"hi\"}}]}\n\ndata: [DONE]\n\n".to_vec()
        }
        WireSurface::OpenaiResponses => {
            b"event: response.output_text.delta\ndata: {\"type\":\"response.output_text.delta\",\"delta\":\"hi\"}\n\nevent: response.completed\ndata: {\"type\":\"response.completed\",\"response\":{\"usage\":{\"input_tokens\":2}}}\n\n".to_vec()
        }
        WireSurface::AnthropicMessages => {
            b"event: content_block_delta\ndata: {\"type\":\"content_block_delta\",\"index\":0,\"delta\":{\"type\":\"text_delta\",\"text\":\"hi\"}}\n\nevent: message_stop\ndata: {\"type\":\"message_stop\"}\n\n".to_vec()
        }
        WireSurface::GeminiInteractions => {
            b"event: step.delta\ndata: {\"event_type\":\"step.delta\",\"delta\":{\"type\":\"text\",\"text\":\"hi\"}}\n\nevent: interaction.completed\ndata: {\"event_type\":\"interaction.completed\",\"interaction\":{\"status\":\"completed\"}}\n\n".to_vec()
        }
        WireSurface::GeminiGenerateContent => {
            b"data: {\"candidates\":[{\"content\":{\"parts\":[{\"text\":\"hi\"}]}}]}\n\ndata: {\"candidates\":[{\"finishReason\":\"STOP\"}]}\n\n".to_vec()
        }
    }
}

#[test]
fn stream_instances_are_independent_and_expose_terminal_usage_evidence() {
    let runtime = WireRuntime::embedded().expect("embedded registry");
    for surface in WireSurface::ALL {
        let context = context(surface, ClientSurface::ChatCompletions);
        let mut stream = runtime.stream(&context).expect("stream runtime");
        let bytes = stream_fixture(surface);
        let mut events = Vec::new();
        for (index, byte) in bytes.iter().enumerate() {
            let pushed = stream
                .push(std::slice::from_ref(byte))
                .expect("stream push");
            assert_eq!(pushed.bytes.bytes_observed, index + 1);
            events.extend(pushed.events);
        }
        let finalization = stream.finalize().expect("stream finalize");
        events.extend(finalization.events);
        assert!(!events.is_empty());
        assert_eq!(
            finalization.terminal.outcome,
            StreamTerminalOutcome::Success
        );
        assert_eq!(finalization.bytes.bytes_observed, bytes.len());
        let encoded = stream
            .encode_client_event(&events[0])
            .expect("client event");
        assert!(!encoded.is_empty());
    }
}

#[test]
fn m5_bridges_and_selector_style_payload_are_pure_and_redacted() {
    let runtime = WireRuntime::embedded().expect("embedded registry");
    let context = context(
        WireSurface::OpenaiChatCompletions,
        ClientSurface::ChatCompletions,
    );
    let prepared = runtime
        .prepare_request(
            br#"{"model":"source-model","messages":[{"role":"user","content":"reply route id only"}]}"#,
            &context,
        )
        .expect("selector-style request");
    let mut facts = StaticRoutingFacts {
        known_provider_ids: BTreeSet::from(["provider-a".into()]),
        requested_protocol: Some("openai".into()),
        now: 42,
        ..StaticRoutingFacts::default()
    };
    facts.transcode_protocols.push("anthropic".into());
    let routing = runtime.routing_facts(&prepared.admission, &facts);
    assert_eq!(routing.canonical_model_id, "source-model");
    assert_eq!(routing.request_surface, "chat_completions");
    let affinity = runtime.affinity_identity(&prepared.canonical, None);
    assert!(affinity.session_identity().is_some());
    assert!(format!("{prepared:?}").contains("text_bytes"));
    assert!(!format!("{prepared:?}").contains("reply route id only"));
}

#[test]
fn runtime_and_context_are_shareable_without_mutable_global_codec_state() {
    let runtime = WireRuntime::embedded().expect("embedded registry");
    let context = context(
        WireSurface::AnthropicMessages,
        ClientSurface::ChatCompletions,
    );
    std::thread::scope(|scope| {
        for _ in 0..8 {
            let runtime = runtime.clone();
            let context = context.clone();
            scope.spawn(move || {
                let prepared = runtime
                    .prepare_request(br#"{"model":"source-model","messages":[]}"#, &context)
                    .expect("independent preparation");
                assert_eq!(prepared.identity.profile, WireSurface::AnthropicMessages);
            });
        }
    });
}
