use eggpool::wire::ir::ClientSurface;
use eggpool::wire::{
    AnthropicMessagesCodec, ConfiguredWireProfile, DecodedProviderPayload,
    GeminiGenerateContentCodec, GeminiInteractionsCodec, OpenAiChatCodec, OpenAiResponsesCodec,
    WireCodec, WireCodecId, WireProfileDefinition, WireSurface, builtin_codec_instance,
};
use serde_json::{Value, json};

fn profile(surface: WireSurface, request_codec: WireCodecId) -> ConfiguredWireProfile {
    ConfiguredWireProfile {
        definition: WireProfileDefinition {
            surface,
            request_codec,
            response_codec: request_codec,
            stream_codec: match request_codec {
                WireCodecId::OpenaiChat => WireCodecId::OpenaiChatSse,
                WireCodecId::AnthropicMessages => WireCodecId::AnthropicMessagesSse,
                other => other,
            },
        },
        path_template: "/wire".into(),
        stream_path_template: None,
        priority: 0,
    }
}

#[test]
fn closed_codec_dispatch_selects_the_two_landed_finite_codecs() {
    assert_eq!(
        builtin_codec_instance(WireCodecId::OpenaiChat)
            .expect("Chat codec")
            .codec_id(),
        WireCodecId::OpenaiChat
    );
    assert_eq!(
        builtin_codec_instance(WireCodecId::AnthropicMessages)
            .expect("Messages codec")
            .codec_id(),
        WireCodecId::AnthropicMessages
    );
    assert!(builtin_codec_instance(WireCodecId::OpenaiChatSse).is_none());
}

#[test]
fn chat_codec_round_trips_native_controls_and_tool_linkage() {
    let codec = OpenAiChatCodec;
    let input = json!({
        "model": "model-a",
        "messages": [
            {"role": "system", "content": "Be concise."},
            {"role": "user", "content": "Call lookup."},
            {"role": "assistant", "content": null, "tool_calls": [
                {"id": "call-1", "type": "function", "function": {
                    "name": "lookup", "arguments": "{\"q\":\"egg\"}"
                }}
            ]},
            {"role": "tool", "tool_call_id": "call-1", "content": "result"}
        ],
        "stream": false,
        "max_tokens": 0,
        "temperature": 0.0,
        "top_p": null,
        "tools": [{"type": "function", "function": {
            "name": "lookup", "description": "Find a value",
            "parameters": {"type": "object"}
        }}],
        "tool_choice": {"type": "function", "function": {"name": "lookup"}},
        "reasoning_effort": "high"
    });
    let decoded = codec
        .decode_client_request(&input, ClientSurface::ChatCompletions)
        .expect("request should decode")
        .value;
    assert_eq!(
        decoded.messages[2].content[0].call_id.as_deref(),
        Some("call-1")
    );
    assert_eq!(decoded.messages[3].tool_call_id.as_deref(), Some("call-1"));
    assert_eq!(
        decoded.presence.max_output_tokens,
        eggpool::wire::ir::Presence::Value(0)
    );
    assert_eq!(decoded.presence.top_p, eggpool::wire::ir::Presence::Null);

    let encoded = codec
        .encode_request(
            &decoded,
            &profile(WireSurface::OpenaiChatCompletions, WireCodecId::OpenaiChat),
        )
        .expect("request should encode")
        .value;
    assert_eq!(encoded["messages"][2]["tool_calls"][0]["id"], "call-1");
    assert_eq!(encoded["messages"][3]["tool_call_id"], "call-1");
    assert_eq!(encoded["max_completion_tokens"], 0);
    assert_eq!(encoded["top_p"], Value::Null);
    assert_eq!(encoded["reasoning_effort"], "high");

    let response = codec
        .decode_response(
            &json!({
                "id": "chatcmpl-1", "object": "chat.completion", "model": "model-a",
                "choices": [{"index": 0, "message": {
                    "role": "assistant", "content": "done"
                }, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}
            }),
            200,
        )
        .expect("native response should decode")
        .value;
    let DecodedProviderPayload::Response(response) = response else {
        panic!("expected success response")
    };
    assert_eq!(response.output[0].text.as_deref(), Some("done"));
    assert_eq!(response.finish_reason.as_deref(), Some("stop"));
    let rendered = codec
        .encode_response(&response, ClientSurface::ChatCompletions)
        .expect("native response should encode")
        .value;
    assert_eq!(rendered["choices"][0]["message"]["content"], "done");
    assert_eq!(rendered["usage"]["total_tokens"], 3);
}

#[test]
fn anthropic_codec_preserves_system_blocks_thinking_and_tool_results() {
    let codec = AnthropicMessagesCodec;
    let input = json!({
        "model": "claude-test",
        "system": [{"type": "text", "text": "Be useful."}],
        "messages": [
            {"role": "assistant", "content": [{
                "type": "tool_use", "id": "toolu-1", "name": "lookup", "input": {"q": "egg"}
            }]},
            {"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": "toolu-1", "content": "result", "is_error": false
            }]}
        ],
        "max_tokens": 128,
        "thinking": {"type": "enabled", "budget_tokens": 64},
        "tools": [{"name": "lookup", "input_schema": {"type": "object"}}],
        "tool_choice": {"type": "any"}
    });
    let decoded = codec
        .decode_client_request(&input, ClientSurface::Messages)
        .expect("request should decode")
        .value;
    assert_eq!(
        decoded.messages[0].role,
        eggpool::wire::ir::CanonicalRole::System
    );
    assert_eq!(
        decoded.messages[1].content[0].call_id.as_deref(),
        Some("toolu-1")
    );
    assert_eq!(
        decoded.messages[2].content[0].kind,
        eggpool::wire::ir::CanonicalBlockKind::ToolResult
    );
    assert_eq!(decoded.reasoning.budget_tokens, Some(64));

    let encoded = codec
        .encode_request(
            &decoded,
            &profile(
                WireSurface::AnthropicMessages,
                WireCodecId::AnthropicMessages,
            ),
        )
        .expect("request should encode")
        .value;
    assert_eq!(encoded["system"], "Be useful.");
    assert_eq!(encoded["messages"][0]["content"][0]["id"], "toolu-1");
    assert_eq!(
        encoded["messages"][1]["content"][0]["tool_use_id"],
        "toolu-1"
    );
    assert_eq!(encoded["thinking"]["budget_tokens"], 64);

    let mut adapted = decoded;
    adapted.response_format = Some(json!({"type": "json_schema"}).as_object().unwrap().clone());
    adapted.parallel_tool_calls = Some(true);
    adapted.reasoning = eggpool::wire::ir::ReasoningIntent {
        requested: Some(true),
        mode: eggpool::wire::ir::ReasoningMode::Effort,
        effort: Some("high".into()),
        budget_tokens: None,
        explicit_disable: false,
    };
    let encoded = codec
        .encode_request(
            &adapted,
            &profile(
                WireSurface::AnthropicMessages,
                WireCodecId::AnthropicMessages,
            ),
        )
        .expect("request should encode");
    assert_eq!(encoded.notices.len(), 3);
    let encoded = encoded.value;
    assert!(encoded.get("parallel_tool_calls").is_none());
    assert!(encoded.get("thinking").is_none());
}

#[test]
fn cross_wire_request_and_response_use_canonical_identity() {
    let chat = OpenAiChatCodec;
    let messages = AnthropicMessagesCodec;
    let request = chat
        .decode_client_request(
            &json!({
                "model": "model-a",
                "messages": [{"role": "assistant", "content": null, "tool_calls": [
                    {"id": "call-1", "type": "function", "function": {
                        "name": "lookup", "arguments": "{\"q\":\"egg\"}"
                    }}
                ]}]
            }),
            ClientSurface::ChatCompletions,
        )
        .expect("request should decode")
        .value;
    let anthropic_request = messages
        .encode_request(
            &request,
            &profile(
                WireSurface::AnthropicMessages,
                WireCodecId::AnthropicMessages,
            ),
        )
        .expect("cross-wire request should encode")
        .value;
    assert_eq!(
        anthropic_request["messages"][0]["content"][0]["id"],
        "call-1"
    );
    assert_eq!(
        anthropic_request["messages"][0]["content"][0]["input"]["q"],
        "egg"
    );

    let response = messages
        .decode_response(
            &json!({
                "id": "msg-1", "type": "message", "role": "assistant",
                "model": "claude-test", "stop_reason": "tool_use",
                "content": [
                    {"type": "thinking", "thinking": "briefly"},
                    {"type": "tool_use", "id": "toolu-1", "name": "lookup", "input": {"q": "egg"}}
                ],
                "usage": {"input_tokens": 4, "output_tokens": 3,
                    "cache_read_input_tokens": 2, "cache_creation_input_tokens": 1}
            }),
            200,
        )
        .expect("response should decode")
        .value;
    let DecodedProviderPayload::Response(response) = response else {
        panic!("expected success response")
    };
    assert_eq!(response.output[1].call_id.as_deref(), Some("toolu-1"));
    assert_eq!(
        response.usage.as_ref().unwrap().cached_input_tokens,
        Some(3)
    );
    let chat_response = chat
        .encode_response(&response, ClientSurface::ChatCompletions)
        .expect("cross-wire response should encode")
        .value;
    assert_eq!(chat_response["choices"][0]["finish_reason"], "tool_calls");
    assert_eq!(
        chat_response["choices"][0]["message"]["tool_calls"][0]["id"],
        "toolu-1"
    );
    assert_eq!(chat_response["usage"]["prompt_tokens"], Value::from(4));
}

#[test]
fn valid_provider_errors_are_evidence_and_malformed_success_is_rejected() {
    let chat = OpenAiChatCodec;
    let error = chat
        .decode_response(
            &json!({"error": {"type": "invalid_api_key", "message": "bad key"}}),
            401,
        )
        .expect("provider error envelope should decode")
        .value;
    match error {
        DecodedProviderPayload::Error(error) => {
            assert_eq!(error.status, 401);
            assert_eq!(error.error_type.as_deref(), Some("invalid_api_key"));
            assert_eq!(error.message.as_deref(), Some("bad key"));
        }
        DecodedProviderPayload::Response(_) => panic!("expected provider error evidence"),
    }

    let malformed = chat.decode_response(&json!({"model": "model-a"}), 200);
    assert_eq!(
        malformed.expect_err("missing choices must fail").reason,
        eggpool::wire::CodecReasonCode::MalformedProviderResponse
    );
}

#[test]
fn unsupported_media_and_invalid_tool_arguments_fail_explicitly() {
    let chat = OpenAiChatCodec;
    let request = chat
        .decode_client_request(
            &json!({
                "model": "model-a",
                "messages": [{"role": "user", "content": [{
                    "type": "file", "source": {"type": "url", "url": "https://example.invalid/file.pdf"}
                }]}]
            }),
            ClientSurface::ChatCompletions,
        )
        .expect("admission keeps the document in canonical IR")
        .value;
    let encoded = chat
        .encode_request(
            &request,
            &profile(WireSurface::OpenaiChatCompletions, WireCodecId::OpenaiChat),
        )
        .expect("supported OpenAI file references remain explicit");
    assert_eq!(
        encoded.value["messages"][0]["content"][0]["file"]["file_data"],
        "https://example.invalid/file.pdf"
    );

    let messages = AnthropicMessagesCodec;
    let malformed = messages.encode_response(
        &eggpool::wire::ir::CanonicalResponse {
            response_id: Some("msg-1".into()),
            model: Some("claude-test".into()),
            output: vec![eggpool::wire::ir::CanonicalOutputBlock {
                kind: eggpool::wire::ir::CanonicalBlockKind::ToolCall,
                text: None,
                media: None,
                call_id: Some("toolu-1".into()),
                name: Some("lookup".into()),
                arguments: Some("not-json".into()),
            }],
            finish_reason: Some("tool_use".into()),
            usage: None,
            provider_error: None,
        },
        ClientSurface::Messages,
    );
    assert_eq!(
        malformed
            .expect_err("invalid arguments must not become empty input")
            .reason,
        eggpool::wire::CodecReasonCode::MalformedProviderResponse
    );
}

#[test]
fn responses_codec_preserves_native_items_controls_and_usage() {
    let codec = OpenAiResponsesCodec;
    let input = json!({
        "model": "model-a",
        "instructions": "Be concise.",
        "input": [
            {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "Call lookup."}
            ]},
            {"type": "function_call", "call_id": "call-1", "name": "lookup", "arguments": "{\"q\":\"egg\"}"},
            {"type": "function_call_output", "call_id": "call-1", "output": "result"}
        ],
        "stream": false,
        "max_output_tokens": 0,
        "reasoning": {"effort": "low"},
        "text": {"format": {"type": "json_schema"}}
    });
    let decoded = codec
        .decode_client_request(&input, ClientSurface::Responses)
        .expect("Responses input should decode")
        .value;
    assert_eq!(decoded.messages.len(), 4);
    assert_eq!(
        decoded.messages[2].content[0].call_id.as_deref(),
        Some("call-1")
    );
    assert_eq!(
        decoded.messages[3].role,
        eggpool::wire::ir::CanonicalRole::Tool
    );
    let encoded = codec
        .encode_request(
            &decoded,
            &profile(WireSurface::OpenaiResponses, WireCodecId::OpenaiResponses),
        )
        .expect("Responses request should encode")
        .value;
    assert_eq!(encoded["instructions"], "Be concise.");
    assert_eq!(encoded["input"][1]["type"], "function_call");
    assert_eq!(encoded["input"][2]["type"], "function_call_output");
    assert_eq!(encoded["max_output_tokens"], 0);

    let decoded_response = codec
        .decode_response(
            &json!({
                "id": "resp-1", "model": "model-a", "status": "completed",
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": "done"}]},
                    {"type": "reasoning", "summary": [{"type": "summary_text", "text": "briefly"}]},
                    {"type": "function_call", "call_id": "call-1", "name": "lookup", "arguments": "{}"}
                ],
                "usage": {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3}
            }),
            200,
        )
        .expect("Responses response should decode")
        .value;
    let DecodedProviderPayload::Response(response) = decoded_response else {
        panic!("expected response")
    };
    assert_eq!(response.output.len(), 3);
    assert_eq!(response.output[2].call_id.as_deref(), Some("call-1"));
    assert_eq!(response.usage.as_ref().unwrap().total_tokens, Some(3));
    let rendered = codec
        .encode_response(&response, ClientSurface::Responses)
        .expect("Responses response should encode")
        .value;
    assert_eq!(rendered["output"][1]["type"], "reasoning");
}

#[test]
fn generate_content_codec_maps_parts_tools_reasoning_and_schema() {
    let codec = GeminiGenerateContentCodec;
    let request = OpenAiChatCodec
        .decode_client_request(
            &json!({
                "model": "model-a",
                "messages": [
                    {"role": "system", "content": "Be concise."},
                    {"role": "user", "content": "Call lookup."},
                    {"role": "assistant", "content": null, "tool_calls": [{
                        "id": "call-1", "type": "function", "function": {
                            "name": "lookup", "arguments": "{\"q\":\"egg\"}"
                        }
                    }]}
                ],
                "max_tokens": 256,
                "temperature": 0.2,
                "top_p": 0.5,
                "stop": ["DONE"],
                "response_format": {"type": "json_schema", "json_schema": {"schema": {"type": "object"}}}
            }),
            ClientSurface::ChatCompletions,
        )
        .expect("source request should decode")
        .value;
    let encoded = codec
        .encode_request(
            &request,
            &profile(
                WireSurface::GeminiGenerateContent,
                WireCodecId::GeminiGenerateContent,
            ),
        )
        .expect("generateContent request should encode")
        .value;
    assert_eq!(
        encoded["systemInstruction"]["parts"][0]["text"],
        "Be concise."
    );
    assert_eq!(encoded["contents"][1]["role"], "model");
    assert_eq!(encoded["generationConfig"]["maxOutputTokens"], 256);
    assert_eq!(
        encoded["generationConfig"]["responseMimeType"],
        "application/json"
    );
    assert_eq!(
        encoded["contents"][1]["parts"][0]["functionCall"]["name"],
        "lookup"
    );

    let decoded = codec
        .decode_response(
            &json!({
                "responseId": "resp-1", "modelVersion": "gemini-test",
                "candidates": [{"content": {"role": "model", "parts": [
                    {"text": "done"}, {"text": "thinking", "thought": true},
                    {"functionCall": {"name": "lookup", "args": {"ok": true}}}
                ]}, "finishReason": "STOP"}],
                "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 1, "totalTokenCount": 3}
            }),
            200,
        )
        .expect("generateContent response should decode")
        .value;
    let DecodedProviderPayload::Response(response) = decoded else {
        panic!("expected response")
    };
    assert_eq!(
        response.output[1].kind,
        eggpool::wire::ir::CanonicalBlockKind::Reasoning
    );
    assert_eq!(
        response.output[2].kind,
        eggpool::wire::ir::CanonicalBlockKind::ToolCall
    );
    let native = codec
        .encode_native_response(&response)
        .expect("native generateContent response should encode")
        .value;
    assert_eq!(
        native["candidates"][0]["content"]["parts"][2]["functionCall"]["name"],
        "lookup"
    );
}

#[test]
fn gemini_interactions_and_all_profiles_have_concrete_dispatch() {
    let interactions = GeminiInteractionsCodec;
    let request = OpenAiChatCodec
        .decode_client_request(
            &json!({"model": "model-a", "messages": [{"role": "user", "content": "hello"}]}),
            ClientSurface::ChatCompletions,
        )
        .expect("source request should decode")
        .value;
    let encoded = interactions
        .encode_request(
            &request,
            &profile(
                WireSurface::GeminiInteractions,
                WireCodecId::GeminiInteractions,
            ),
        )
        .expect("Interactions request should encode")
        .value;
    assert_eq!(encoded["input"], "hello");
    let response = interactions
        .decode_response(
            &json!({"interaction": {"id": "int-1", "model": "model-a", "status": "completed", "steps": [{"type": "model_output", "content": [{"text": "done"}]}], "usage": {"total_input_tokens": 1, "total_output_tokens": 1, "total_tokens": 2}}}),
            200,
        )
        .expect("Interactions response should decode")
        .value;
    let DecodedProviderPayload::Response(response) = response else {
        panic!("expected response")
    };
    assert_eq!(response.output[0].text.as_deref(), Some("done"));
    assert_eq!(
        interactions
            .encode_native_response(&response)
            .unwrap()
            .value["object"],
        "interaction"
    );

    for id in [
        WireCodecId::OpenaiResponses,
        WireCodecId::GeminiInteractions,
        WireCodecId::GeminiGenerateContent,
    ] {
        assert_eq!(builtin_codec_instance(id).unwrap().codec_id(), id);
    }
}

#[test]
fn provider_errors_and_blocked_gemini_responses_never_become_success() {
    let responses = OpenAiResponsesCodec;
    let error = responses
        .decode_response(
            &json!({"error": {"type": "invalid_request_error", "message": "bad"}}),
            400,
        )
        .expect("provider error should be evidence")
        .value;
    assert!(matches!(error, DecodedProviderPayload::Error(_)));

    let gemini = GeminiGenerateContentCodec;
    let blocked = gemini.decode_response(
        &json!({"candidates": [], "promptFeedback": {"blockReason": "SAFETY"}}),
        200,
    );
    assert_eq!(
        blocked.unwrap_err().reason,
        eggpool::wire::CodecReasonCode::UnsupportedSemanticFeature
    );
    let malformed = gemini.decode_response(
        &json!({"candidates": [{"content": {"parts": [{"functionCall": {"args": {}}}]}}]}),
        200,
    );
    assert_eq!(
        malformed.unwrap_err().reason,
        eggpool::wire::CodecReasonCode::MalformedProviderResponse
    );
}
