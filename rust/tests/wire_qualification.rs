//! Integrated W010 qualification against the committed Python W001 oracle.

use eggpool::request::{AdmissionOptions, admit_request};
use eggpool::wire::ir::{CanonicalEvent, CanonicalEventType, ClientSurface};
use eggpool::wire::{
    ConfiguredWireProfile, FiniteResponseOutcome, LossPolicy, StreamTerminalOutcome,
    TerminalEvidence, WireCodecId, WireProfileDefinition, WireProfileFlags, WireRuntime,
    WireRuntimeContext, WireRuntimeError, WireSurface,
};
use serde_json::{Value, json};

const W001_OBSERVATIONS: &str =
    include_str!("../../migration-rs/fixtures/canonical-wire/w001-python-observations.json");

fn oracle() -> Value {
    serde_json::from_str(W001_OBSERVATIONS).expect("committed W001 fixture is valid JSON")
}

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
        path_template: "/fixture".into(),
        stream_path_template: Some("/fixture".into()),
        priority: 0,
    }
}

fn context(client: ClientSurface, upstream: WireSurface) -> WireRuntimeContext {
    let mut context =
        WireRuntimeContext::new(client, profile(upstream), "fixture-model", "fixture-model");
    context.profile_flags = WireProfileFlags::for_surfaces(client, upstream);
    context.provider_id = Some("fixture-provider".into());
    context.provider_kind = Some("fixture".into());
    context
}

fn source_requests() -> [(ClientSurface, Value); 3] {
    [
        (
            ClientSurface::ChatCompletions,
            json!({
                "model": "fixture-model",
                "messages": [
                    {"role": "system", "content": "You are synthetic."},
                    {"role": "developer", "content": "Preserve semantics."},
                    {"role": "user", "content": [
                        {"type": "text", "text": "Hello, 世界."},
                        {"type": "image_url", "image_url": {
                            "url": "data:image/png;base64,AAEC", "detail": "high"
                        }},
                        {"type": "file", "file": {"file_id": "file_fixture_1"}}
                    ]},
                    {"role": "assistant", "content": "I will use the tool.", "tool_calls": [{
                        "id": "call_fixture_1", "type": "function", "function": {
                            "name": "lookup", "arguments": "{\"q\":\"synthetic\"}"
                        }
                    }]},
                    {"role": "tool", "tool_call_id": "call_fixture_1", "content": "result"}
                ],
                "stream": true,
                "max_tokens": 32,
                "temperature": 0,
                "top_p": 0.5,
                "tools": [{"type": "function", "function": {
                    "name": "lookup", "parameters": {"type": "object"}
                }}],
                "tool_choice": {"type": "function", "function": {"name": "lookup"}},
                "parallel_tool_calls": false,
                "reasoning_effort": "medium",
                "response_format": {"type": "json_object"},
                "cache_control": {"type": "ephemeral"}
            }),
        ),
        (
            ClientSurface::Responses,
            json!({
                "model": "fixture-model",
                "instructions": "You are synthetic.",
                "input": [{"type": "message", "role": "user", "content": [
                    {"type": "input_text", "text": "Hello, 世界."}
                ]}],
                "stream": true,
                "max_output_tokens": 32,
                "reasoning": {"effort": "low"},
                "text": {"format": {"type": "json_schema", "name": "answer"}}
            }),
        ),
        (
            ClientSurface::Messages,
            json!({
                "model": "fixture-model",
                "system": [{"type": "text", "text": "You are synthetic."}],
                "messages": [
                    {"role": "user", "content": [
                        {"type": "text", "text": "Hello, 世界."},
                        {"type": "image", "source": {
                            "type": "base64", "media_type": "image/png", "data": "AAEC"
                        }}
                    ]},
                    {"role": "assistant", "content": [
                        {"type": "thinking", "thinking": "briefly"},
                        {"type": "tool_use", "id": "call_fixture_1", "name": "lookup", "input": {}}
                    ]},
                    {"role": "user", "content": [{
                        "type": "tool_result", "tool_use_id": "call_fixture_1", "content": "result"
                    }]}
                ],
                "stream": true,
                "max_tokens": 32,
                "thinking": {"type": "enabled", "budget_tokens": 128},
                "tools": [{"name": "lookup", "input_schema": {"type": "object"}}]
            }),
        ),
    ]
}

fn response_payload(surface: WireSurface) -> Value {
    match surface {
        WireSurface::OpenaiChatCompletions => json!({
            "id": "resp-chat", "model": "fixture-model",
            "choices": [{"message": {
                "role": "assistant", "content": "synthetic answer",
                "reasoning_content": "synthetic reasoning",
                "tool_calls": [{"id": "call_fixture_1", "function": {
                    "name": "lookup", "arguments": "{\"ok\":true}"
                }}]
            }, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14,
                "prompt_tokens_details": {"cached_tokens": 2}}
        }),
        WireSurface::OpenaiResponses => json!({
            "id": "resp-responses", "model": "fixture-model", "status": "completed",
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": "synthetic answer"}]},
                {"type": "reasoning", "summary": [{"type": "summary_text", "text": "synthetic reasoning"}]},
                {"type": "function_call", "call_id": "call_fixture_1", "name": "lookup",
                    "arguments": "{\"ok\":true}"}
            ],
            "usage": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14}
        }),
        WireSurface::AnthropicMessages => json!({
            "id": "resp-anthropic", "model": "fixture-model",
            "content": [
                {"type": "text", "text": "synthetic answer"},
                {"type": "thinking", "thinking": "synthetic reasoning"},
                {"type": "tool_use", "id": "call_fixture_1", "name": "lookup", "input": {"ok": true}}
            ],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 4,
                "cache_read_input_tokens": 2, "cache_creation_input_tokens": 1}
        }),
        WireSurface::GeminiInteractions => json!({
            "interaction": {"id": "resp-interactions", "model": "fixture-model",
                "status": "completed", "steps": [{"type": "model_output", "content": [
                    {"type": "text", "text": "synthetic answer"}
                ]}], "usage": {"total_input_tokens": 10,
                    "total_output_tokens": 4, "total_tokens": 14}}
        }),
        WireSurface::GeminiGenerateContent => json!({
            "responseId": "resp-generate", "modelVersion": "fixture-model",
            "candidates": [{"content": {"parts": [
                {"text": "synthetic answer"},
                {"functionCall": {"name": "lookup", "args": {"ok": true}}}
            ]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 4,
                "totalTokenCount": 14}
        }),
    }
}

fn stream_records(surface: WireSurface, line_ending: &[u8]) -> Vec<u8> {
    let mut output = Vec::new();
    let mut record = |event: Option<&str>, payload: Value| {
        if let Some(event) = event {
            output.extend_from_slice(b"event: ");
            output.extend_from_slice(event.as_bytes());
            output.extend_from_slice(line_ending);
        }
        output.extend_from_slice(b"id: fixture-1");
        output.extend_from_slice(line_ending);
        output.extend_from_slice(b": synthetic comment");
        output.extend_from_slice(line_ending);
        if payload == Value::String("[DONE]".into()) {
            output.extend_from_slice(b"data: [DONE]");
        } else {
            output.extend_from_slice(b"data: ");
            output.extend_from_slice(payload.to_string().as_bytes());
        }
        output.extend_from_slice(line_ending);
        output.extend_from_slice(line_ending);
    };

    match surface {
        WireSurface::OpenaiChatCompletions => {
            record(
                None,
                json!({"id":"stream-chat","model":"fixture-model",
                "choices":[{"delta":{"content":"hi"},"finish_reason":null}]}),
            );
            record(
                None,
                json!({"id":"stream-chat","choices":[{"delta":{},
                "finish_reason":"stop"}]}),
            );
            record(None, Value::String("[DONE]".into()));
        }
        WireSurface::OpenaiResponses => {
            record(
                Some("response.created"),
                json!({"type":"response.created",
                "response":{"id":"stream-responses","model":"fixture-model"}}),
            );
            record(
                Some("response.output_text.delta"),
                json!({
                "type":"response.output_text.delta","delta":"hi"}),
            );
            record(
                Some("response.completed"),
                json!({"type":"response.completed",
                "response":{"id":"stream-responses","usage":{"input_tokens":2,
                "output_tokens":1,"total_tokens":3}}}),
            );
        }
        WireSurface::AnthropicMessages => {
            record(
                Some("message_start"),
                json!({"type":"message_start",
                "message":{"id":"stream-anthropic","model":"fixture-model"}}),
            );
            record(
                Some("content_block_delta"),
                json!({"type":"content_block_delta",
                "index":0,"delta":{"type":"text_delta","text":"hi"}}),
            );
            record(
                Some("message_delta"),
                json!({"type":"message_delta",
                "delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":1}}),
            );
            record(Some("message_stop"), json!({"type":"message_stop"}));
        }
        WireSurface::GeminiInteractions => {
            record(
                Some("interaction.created"),
                json!({"event_type":"interaction.created",
                "interaction":{"id":"stream-interactions","model":"fixture-model"}}),
            );
            record(
                Some("step.delta"),
                json!({"event_type":"step.delta",
                "delta":{"type":"text","text":"hi"}}),
            );
            record(
                Some("interaction.completed"),
                json!({"event_type":"interaction.completed",
                "interaction":{"status":"completed","usage":{"total_input_tokens":2,
                "total_output_tokens":1,"total_tokens":3}}}),
            );
        }
        WireSurface::GeminiGenerateContent => {
            record(
                None,
                json!({"candidates":[{"content":{"parts":[{"text":"hi"}]}}]}),
            );
            record(
                None,
                json!({"candidates":[{"finishReason":"STOP"}],
                "usageMetadata":{"promptTokenCount":2,"candidatesTokenCount":1,
                "totalTokenCount":3}}),
            );
        }
    }
    output
}

fn event_type_name(event: &CanonicalEvent) -> &'static str {
    match event.event_type {
        CanonicalEventType::ResponseStart => "response_start",
        CanonicalEventType::ContentStart => "content_start",
        CanonicalEventType::TextDelta => "text_delta",
        CanonicalEventType::ReasoningStart => "reasoning_start",
        CanonicalEventType::ReasoningDelta => "reasoning_delta",
        CanonicalEventType::ReasoningStop => "reasoning_stop",
        CanonicalEventType::ToolCallStart => "tool_call_start",
        CanonicalEventType::ToolCallArgumentsDelta => "tool_call_arguments_delta",
        CanonicalEventType::ToolCallStop => "tool_call_stop",
        CanonicalEventType::ContentStop => "content_stop",
        CanonicalEventType::Usage => "usage",
        CanonicalEventType::ResponseComplete => "response_complete",
        CanonicalEventType::ResponseIncomplete => "response_incomplete",
        CanonicalEventType::Error => "error",
    }
}

fn evidence_name(evidence: Option<TerminalEvidence>) -> Option<&'static str> {
    Some(match evidence? {
        TerminalEvidence::OpenaiDone => "openai_done",
        TerminalEvidence::ResponsesCompleted => "responses_completed",
        TerminalEvidence::ResponsesIncomplete => "responses_incomplete",
        TerminalEvidence::ResponsesFailed => "responses_failed",
        TerminalEvidence::AnthropicMessageStop => "anthropic_message_stop",
        TerminalEvidence::GeminiCompleted => "gemini_completed",
        TerminalEvidence::GeminiIncomplete => "gemini_incomplete",
        TerminalEvidence::ProviderError => "provider_error",
    })
}

#[test]
fn committed_python_observations_match_integrated_rust_runtime() {
    let expected = oracle();
    let runtime = WireRuntime::embedded().expect("embedded registry");

    for surface in WireSurface::ALL {
        let profile_id = surface.as_str();
        let inventory = &expected["wire_profile_inventory"][profile_id];
        let definition = runtime.registry().get(surface).expect("profile present");
        assert_eq!(
            definition.request_codec.as_str(),
            inventory["request_codec"]
        );
        assert_eq!(
            definition.response_codec.as_str(),
            inventory["response_codec"]
        );
        assert_eq!(definition.stream_codec.as_str(), inventory["stream_codec"]);

        let body = serde_json::to_vec(&response_payload(surface)).expect("fixture JSON");
        let result = runtime
            .decode_finite_response(
                &body,
                200,
                &context(ClientSurface::ChatCompletions, surface),
                true,
            )
            .expect("finite provider response is classified");
        let FiniteResponseOutcome::Success(response) = result.outcome else {
            panic!(
                "fixture response must be successful for {surface_id}",
                surface_id = profile_id
            );
        };
        let kinds: Vec<_> = response
            .output
            .iter()
            .map(|block| block.kind.as_str())
            .collect();
        let expected_kinds: Vec<_> = expected["responses"][profile_id]["kinds"]
            .as_array()
            .expect("response kinds")
            .iter()
            .map(|kind| kind.as_str().expect("kind string"))
            .collect();
        assert_eq!(kinds, expected_kinds);
        assert_eq!(
            response.finish_reason.as_deref(),
            expected["responses"][profile_id]["finish"].as_str()
        );

        let bytes = stream_records(surface, b"\n");
        let mut stream = runtime
            .stream(&context(ClientSurface::ChatCompletions, surface))
            .expect("stream runtime");
        let mut events = Vec::new();
        for byte in &bytes {
            events.extend(
                stream
                    .push(std::slice::from_ref(byte))
                    .expect("fragmented stream")
                    .events,
            );
        }
        let finalization = stream.finalize().expect("stream EOF classification");
        events.extend(finalization.events);
        let event_types: Vec<_> = events.iter().map(event_type_name).collect();
        let expected_events: Vec<_> = expected["streams"][profile_id]["event_types"]
            .as_array()
            .expect("stream event types")
            .iter()
            .map(|kind| kind.as_str().expect("event type string"))
            .collect();
        assert_eq!(event_types, expected_events);
        assert_eq!(
            evidence_name(finalization.terminal.evidence),
            expected["streams"][profile_id]["terminal_kind"].as_str()
        );
        assert!(finalization.terminal.saw_terminal_event);
        assert_eq!(
            finalization.terminal.outcome,
            StreamTerminalOutcome::Success
        );

        let mut crlf_stream = runtime
            .stream(&context(ClientSurface::ChatCompletions, surface))
            .expect("CRLF stream runtime");
        let mut crlf_events = Vec::new();
        for byte in stream_records(surface, b"\r\n") {
            crlf_events.extend(
                crlf_stream
                    .push(std::slice::from_ref(&byte))
                    .expect("CRLF fragment")
                    .events,
            );
        }
        crlf_events.extend(crlf_stream.finalize().expect("CRLF EOF").events);
        assert_eq!(
            crlf_events.iter().map(event_type_name).collect::<Vec<_>>(),
            event_types
        );
    }
}

#[test]
fn every_client_surface_and_selected_profile_pair_is_bounded_and_semantic() {
    let runtime = WireRuntime::embedded().expect("embedded registry");
    for (client, value) in source_requests() {
        let raw = serde_json::to_vec(&value).expect("source fixture JSON");
        for upstream in WireSurface::ALL {
            match runtime.prepare_request(&raw, &context(client, upstream)) {
                Ok(prepared) => {
                    assert_eq!(prepared.identity.profile, upstream);
                    assert_eq!(prepared.identity.canonical_model_id, "fixture-model");
                    assert_eq!(prepared.canonical.model, "fixture-model");
                    assert!(prepared.metadata.message_count > 0);
                    assert!(prepared.bytes.output_bytes > 0);
                    assert_eq!(prepared.bytes.input_bytes, raw.len());
                    assert!(prepared.stream.requested);
                }
                Err(WireRuntimeError::RequestAdaptation(error)) => assert!(matches!(
                    error.reason,
                    eggpool::wire::CodecReasonCode::UnsupportedSemanticFeature
                        | eggpool::wire::CodecReasonCode::LossRejected
                )),
                Err(error) => panic!("{client:?} -> {upstream:?}: {error:?}"),
            }
        }
    }
}

#[test]
fn finite_and_stream_responses_adapt_to_every_public_client_surface() {
    let runtime = WireRuntime::embedded().expect("embedded registry");
    for client in [
        ClientSurface::ChatCompletions,
        ClientSurface::Responses,
        ClientSurface::Messages,
    ] {
        for upstream in WireSurface::ALL {
            let context = context(client, upstream);
            let body = serde_json::to_vec(&response_payload(upstream)).expect("response JSON");
            let finite = runtime
                .decode_finite_response(&body, 200, &context, true)
                .unwrap_or_else(|error| panic!("finite {upstream:?} -> {client:?}: {error:?}"));
            assert!(matches!(finite.outcome, FiniteResponseOutcome::Success(_)));
            assert!(finite.client_body.is_some());

            let mut stream = runtime.stream(&context).expect("stream runtime");
            let bytes = stream_records(upstream, b"\n");
            let mut events = Vec::new();
            for byte in &bytes {
                events.extend(
                    stream
                        .push(std::slice::from_ref(byte))
                        .expect("stream event")
                        .events,
                );
            }
            events.extend(stream.finalize().expect("stream finalization").events);
            for event in events.iter() {
                let encoded = stream
                    .encode_client_event(event)
                    .expect("client event adaptation");
                if event.event_type == CanonicalEventType::ResponseComplete {
                    assert!(!encoded.is_empty());
                }
            }
        }
    }
}

#[test]
fn malformed_limits_eof_and_provider_errors_never_become_success() {
    let runtime = WireRuntime::embedded().expect("embedded registry");
    let context = context(
        ClientSurface::ChatCompletions,
        WireSurface::OpenaiChatCompletions,
    );

    for body in [
        b"{".as_slice(),
        b"[]".as_slice(),
        br#"{"messages":[]}"#,
        br#"{"model":"  "}"#,
    ] {
        assert!(matches!(
            runtime.prepare_request(body, &context),
            Err(WireRuntimeError::ClientAdmission(_))
        ));
    }

    let too_large = WireRuntimeContext {
        max_request_body_bytes: 8,
        ..context.clone()
    };
    assert!(matches!(
        runtime.prepare_request(br#"{"model":"fixture-model"}"#, &too_large),
        Err(WireRuntimeError::ClientAdmission(error))
            if error.reason == eggpool::wire::CodecReasonCode::ResourceLimitViolation
    ));

    let error = runtime
        .decode_finite_response(
            br#"{"error":{"type":"rate_limit","message":"synthetic retry"}}"#,
            429,
            &context,
            true,
        )
        .expect("provider error evidence");
    assert!(matches!(
        error.outcome,
        FiniteResponseOutcome::ProviderError(_)
    ));
    assert!(error.client_body.is_none());

    let malformed = runtime
        .decode_finite_response(b"{", 200, &context, true)
        .expect("malformed provider evidence");
    assert!(matches!(
        malformed.outcome,
        FiniteResponseOutcome::Malformed { .. }
    ));

    let mut partial = runtime.stream(&context).expect("stream runtime");
    partial
        .push(b"data: {\"choices\":[{\"delta\":{\"content\":\"partial\"}}]}")
        .expect("partial payload");
    let finalization = partial.finalize().expect("partial EOF");
    assert_eq!(
        finalization.terminal.outcome,
        StreamTerminalOutcome::EofAfterPartialBody
    );
    assert!(!finalization.terminal.saw_terminal_event);

    let mut oversized = eggpool::wire::StreamEventDecoder::with_limit(
        eggpool::wire::StreamAdapterKind::OpenaiChatSse,
        32,
    );
    let mut oversized_body = b"data: ".to_vec();
    oversized_body.extend_from_slice(&[b'x'; 64]);
    assert!(matches!(
        oversized.push(&oversized_body),
        Err(eggpool::wire::StreamError::Framing(
            eggpool::wire::SseDecodeError::FrameTooLarge { .. }
        ))
    ));
    assert!(matches!(
        oversized.finish(),
        Err(eggpool::wire::StreamError::Framing(
            eggpool::wire::SseDecodeError::FrameTooLarge { .. }
        ))
    ));
}

#[test]
fn canonical_m5_bridge_and_diagnostics_are_pure_and_redacted() {
    let runtime = WireRuntime::embedded().expect("embedded registry");
    let value = json!({
        "model": "fixture-model",
        "messages": [{"role": "user", "content": "synthetic-secret-sentinel"}],
        "tools": [{"type": "function", "function": {
            "name": "lookup", "description": "private schema", "parameters": {"type": "object"}
        }}]
    });
    let raw = serde_json::to_vec(&value).expect("request JSON");
    let context = context(
        ClientSurface::ChatCompletions,
        WireSurface::AnthropicMessages,
    );
    let prepared = runtime
        .prepare_request(&raw, &context)
        .expect("request admission");
    let facts = prepared
        .admission
        .routing_facts(&eggpool::request::StaticRoutingFacts {
            known_provider_ids: std::collections::BTreeSet::from(["fixture-provider".into()]),
            requested_protocol: Some("openai".into()),
            now: 42,
            ..Default::default()
        });
    assert_eq!(facts.canonical_model_id, "fixture-model");
    let debug = format!("{prepared:?} {:?}", prepared.body);
    assert!(!debug.contains("synthetic-secret-sentinel"));
    assert!(!debug.contains("private schema"));
    assert!(!debug.contains("fixture raw content"));

    let provider_error = runtime
        .decode_finite_response(
            br#"{"error":{"type":"invalid_api_key","message":"synthetic-secret-sentinel"}}"#,
            401,
            &context,
            true,
        )
        .expect("provider error evidence");
    assert!(!format!("{provider_error:?}").contains("synthetic-secret-sentinel"));

    let admitted = admit_request(
        &raw,
        AdmissionOptions {
            client_surface: ClientSurface::ChatCompletions,
            ..Default::default()
        },
    )
    .expect("same request admits independently");
    let identity = admitted.affinity_identity(Some("synthetic-secret-sentinel"));
    assert!(!format!("{identity:?}").contains("synthetic-secret-sentinel"));
}

#[test]
fn usage_zero_missing_and_cache_status_match_the_python_contract() {
    let expected = oracle();
    let cases = [
        (
            "openai_reported_cache",
            json!({"prompt_tokens":10,"completion_tokens":2,
            "prompt_tokens_details":{"cached_tokens":3}}),
            eggpool::wire::UsageProtocol::Openai,
        ),
        (
            "anthropic_reported_cache",
            json!({"input_tokens":10,"output_tokens":2,
            "cache_read_input_tokens":3,"cache_creation_input_tokens":1}),
            eggpool::wire::UsageProtocol::Anthropic,
        ),
        (
            "explicit_zero",
            json!({"prompt_tokens":0,"completion_tokens":0,"total_tokens":0,
            "prompt_tokens_details":{"cached_tokens":0}}),
            eggpool::wire::UsageProtocol::Openai,
        ),
        (
            "missing_fields",
            json!({"prompt_tokens":2}),
            eggpool::wire::UsageProtocol::Openai,
        ),
        (
            "unknown_shape",
            json!([]),
            eggpool::wire::UsageProtocol::Openai,
        ),
    ];
    for (name, usage, protocol) in cases {
        let actual = eggpool::wire::normalize_usage(Some(&usage), protocol).expect("usage shape");
        let expected_case = &expected["usage"][name];
        assert_eq!(actual.input_tokens, expected_case["input_tokens"].as_u64());
        assert_eq!(
            actual.output_tokens,
            expected_case["output_tokens"].as_u64()
        );
        assert_eq!(actual.total_tokens, expected_case["total_tokens"].as_u64());
        assert_eq!(
            actual.cached_input_tokens,
            expected_case["cached_input_tokens"].as_u64()
        );
        assert_eq!(
            actual.cache_read_input_tokens,
            expected_case["cache_read_input_tokens"].as_u64()
        );
        assert_eq!(
            actual.cache_creation_input_tokens,
            expected_case["cache_creation_input_tokens"].as_u64()
        );
    }
}

#[test]
fn profile_identity_is_immutable_and_loss_policy_remains_explicit() {
    let runtime = WireRuntime::embedded().expect("embedded registry");
    let mut mismatched_context = context(
        ClientSurface::ChatCompletions,
        WireSurface::OpenaiChatCompletions,
    );
    mismatched_context
        .selected_profile
        .definition
        .response_codec = WireCodecId::AnthropicMessages;
    assert!(matches!(
        runtime.prepare_request(br#"{"model":"fixture-model"}"#, &mismatched_context),
        Err(WireRuntimeError::ProfileMismatch { .. })
    ));

    let mut strict_context = context(
        ClientSurface::ChatCompletions,
        WireSurface::AnthropicMessages,
    );
    strict_context.adaptation_policy.loss_policy = LossPolicy::Reject;
    let request = json!({"model":"fixture-model","messages":[{"role":"user","content":"hi"}],
        "reasoning_effort":"high"});
    let raw = serde_json::to_vec(&request).expect("request JSON");
    let result = runtime.prepare_request(&raw, &strict_context);
    assert!(matches!(
        result,
        Err(WireRuntimeError::RequestAdaptation(error))
            if error.reason == eggpool::wire::CodecReasonCode::LossRejected
    ));
}
