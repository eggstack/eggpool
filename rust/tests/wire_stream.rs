use eggpool::wire::ir::{CanonicalEventType, ClientSurface};
use eggpool::wire::{
    SseDecoder, StreamAdapterKind, StreamEventDecoder, StreamTerminalOutcome, TerminalEvidence,
    UsageProtocol, WireCodec, encode_client_event, normalize_usage,
};
use serde_json::{Value, json};

fn fixture(profile: StreamAdapterKind) -> Vec<u8> {
    let mut bytes = Vec::new();
    let mut record = |event: Option<&str>, value: Value| {
        if let Some(event) = event {
            bytes.extend_from_slice(format!("event: {event}\n").as_bytes());
        }
        bytes.extend_from_slice(b"id: fixture\n: comment\n");
        if value == Value::String("[DONE]".into()) {
            bytes.extend_from_slice(b"data: [DONE]\n\n");
        } else {
            bytes.extend_from_slice(b"data: ");
            bytes.extend_from_slice(value.to_string().as_bytes());
            bytes.extend_from_slice(b"\n\n");
        }
    };
    match profile {
        StreamAdapterKind::OpenaiChatSse => {
            record(
                None,
                json!({"id":"chat","model":"m","choices":[{"delta":{"content":"hi"},"finish_reason":null}]}),
            );
            record(
                None,
                json!({"id":"chat","choices":[{"delta":{},"finish_reason":"stop"}]}),
            );
            record(None, Value::String("[DONE]".into()));
        }
        StreamAdapterKind::OpenaiResponsesSse => {
            record(
                Some("response.created"),
                json!({"type":"response.created","response":{"id":"r","model":"m"}}),
            );
            record(
                Some("response.output_text.delta"),
                json!({"type":"response.output_text.delta","delta":"hi"}),
            );
            record(
                Some("response.completed"),
                json!({"type":"response.completed","response":{"id":"r","usage":{"input_tokens":2,"output_tokens":1,"total_tokens":3}}}),
            );
        }
        StreamAdapterKind::AnthropicMessagesSse => {
            record(
                Some("message_start"),
                json!({"type":"message_start","message":{"id":"a","model":"m"}}),
            );
            record(
                Some("content_block_delta"),
                json!({"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}),
            );
            record(
                Some("message_delta"),
                json!({"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":1}}),
            );
            record(Some("message_stop"), json!({"type":"message_stop"}));
        }
        StreamAdapterKind::GeminiInteractionsSse => {
            record(
                Some("interaction.created"),
                json!({"event_type":"interaction.created","interaction":{"id":"i","model":"m"}}),
            );
            record(
                Some("step.delta"),
                json!({"event_type":"step.delta","delta":{"type":"text","text":"hi"}}),
            );
            record(
                Some("interaction.completed"),
                json!({"event_type":"interaction.completed","interaction":{"status":"completed","usage":{"total_input_tokens":2,"total_output_tokens":1,"total_tokens":3}}}),
            );
        }
        StreamAdapterKind::GeminiGenerateContentSse => {
            record(
                None,
                json!({"candidates":[{"content":{"parts":[{"text":"hi"}]}}]}),
            );
            record(
                None,
                json!({"candidates":[{"finishReason":"STOP"}],"usageMetadata":{"promptTokenCount":2,"candidatesTokenCount":1,"totalTokenCount":3}}),
            );
        }
    }
    bytes
}

#[test]
fn sse_framing_is_chunk_boundary_independent_for_lf_crlf_multiline_and_utf8() {
    let source = b": comment\r\nevent: fixture\r\nid: id-1\r\ndata: first\r\ndata: second \xF0\x9F\x8C\x8D\r\n\r\n";
    let mut whole = SseDecoder::default();
    let expected = whole.feed(source).unwrap();
    let mut split = SseDecoder::default();
    let mut actual = Vec::new();
    for byte in source {
        actual.extend(split.feed(std::slice::from_ref(byte)).unwrap());
    }
    actual.extend(split.finish().unwrap().frames);
    assert_eq!(expected, actual);
    assert_eq!(actual[0].event.as_deref(), Some("fixture"));
    assert_eq!(actual[0].data, "first\nsecond 🌍");
    assert!(!actual[0].is_comment_only);
    assert!(
        actual[0]
            .fields
            .iter()
            .any(|(name, value)| name == "id" && value == "id-1")
    );
}

#[test]
fn oversized_carry_is_a_typed_bounded_failure() {
    let mut decoder = SseDecoder::new(16);
    let error = decoder.feed(b"data: 01234567890123456789").unwrap_err();
    assert!(matches!(
        error,
        eggpool::wire::SseDecodeError::FrameTooLarge { limit: 16 }
    ));
}

#[test]
fn all_provider_streams_are_incremental_and_require_native_terminal_evidence() {
    let cases = [
        (
            StreamAdapterKind::OpenaiChatSse,
            TerminalEvidence::OpenaiDone,
        ),
        (
            StreamAdapterKind::OpenaiResponsesSse,
            TerminalEvidence::ResponsesCompleted,
        ),
        (
            StreamAdapterKind::AnthropicMessagesSse,
            TerminalEvidence::AnthropicMessageStop,
        ),
        (
            StreamAdapterKind::GeminiInteractionsSse,
            TerminalEvidence::GeminiCompleted,
        ),
        (
            StreamAdapterKind::GeminiGenerateContentSse,
            TerminalEvidence::GeminiCompleted,
        ),
    ];
    for (adapter, evidence) in cases {
        let bytes = fixture(adapter);
        let mut decoder = StreamEventDecoder::new(adapter);
        let mut events = Vec::new();
        for byte in &bytes {
            events.extend(decoder.push(std::slice::from_ref(byte)).unwrap());
        }
        let (final_events, summary) = decoder.finish().unwrap();
        events.extend(final_events);
        assert!(
            events
                .iter()
                .any(|event| event.event_type == CanonicalEventType::TextDelta)
        );
        if adapter == StreamAdapterKind::GeminiGenerateContentSse {
            let kinds: Vec<_> = events.iter().map(|event| event.event_type).collect();
            assert_eq!(
                kinds,
                vec![
                    CanonicalEventType::TextDelta,
                    CanonicalEventType::Usage,
                    CanonicalEventType::ResponseComplete
                ]
            );
        }
        assert_eq!(summary.outcome, StreamTerminalOutcome::Success);
        assert_eq!(summary.evidence, Some(evidence));
        assert!(summary.saw_terminal_event);
        assert_eq!(summary.bytes_observed, bytes.len());
        assert_eq!(summary.parser_error_count, 0);
    }
}

#[test]
fn eof_without_terminal_is_not_success_and_final_unterminated_event_is_flushed() {
    let mut decoder = StreamEventDecoder::new(StreamAdapterKind::OpenaiChatSse);
    let events = decoder
        .push(b"data: {\"choices\":[{\"delta\":{\"content\":\"partial\"}}]}")
        .unwrap();
    assert!(events.is_empty());
    let (final_events, summary) = decoder.finish().unwrap();
    assert_eq!(final_events.len(), 1);
    assert_eq!(summary.outcome, StreamTerminalOutcome::EofAfterPartialBody);
    assert!(!summary.saw_terminal_event);
}

#[test]
fn malformed_event_is_typed_and_provider_error_is_not_dropped() {
    let mut malformed = StreamEventDecoder::new(StreamAdapterKind::OpenaiChatSse);
    let error = malformed.push(b"data: {not-json}\n\n").unwrap_err();
    assert!(matches!(
        error,
        eggpool::wire::StreamError::MalformedEvent(_)
    ));

    let mut provider_error = StreamEventDecoder::new(StreamAdapterKind::AnthropicMessagesSse);
    let events = provider_error
        .push(b"event: error\ndata: {\"type\":\"error\",\"error\":{\"type\":\"overloaded\",\"message\":\"retry\"}}\n\n")
        .unwrap();
    assert_eq!(events[0].event_type, CanonicalEventType::Error);
    let (_, summary) = provider_error.finish().unwrap();
    assert_eq!(summary.outcome, StreamTerminalOutcome::ProviderError);
    assert_eq!(summary.evidence, Some(TerminalEvidence::ProviderError));
}

#[test]
fn usage_preserves_explicit_zero_and_missing_fields() {
    let mut decoder = StreamEventDecoder::new(StreamAdapterKind::OpenaiResponsesSse);
    decoder
        .push(b"event: response.completed\ndata: {\"type\":\"response.completed\",\"response\":{\"usage\":{\"input_tokens\":0,\"output_tokens\":0,\"total_tokens\":0,\"prompt_tokens_details\":{\"cached_tokens\":0}}}}\n\n")
        .unwrap();
    let (_, summary) = decoder.finish().unwrap();
    let usage = summary.usage.unwrap();
    assert_eq!(usage.input_tokens, Some(0));
    assert_eq!(usage.output_tokens, Some(0));
    assert_eq!(usage.cache_read_input_tokens, Some(0));
    assert_eq!(
        usage.cache_counter_status,
        eggpool::wire::ir::CacheCounterStatus::Reported
    );
    assert!(summary.saw_usage_completion);
}

#[test]
fn usage_normalization_distinguishes_omitted_unknown_and_zero() {
    assert!(normalize_usage(None, UsageProtocol::Openai).is_none());
    assert_eq!(
        normalize_usage(Some(&json!([])), UsageProtocol::Openai)
            .unwrap()
            .cache_counter_status,
        eggpool::wire::ir::CacheCounterStatus::UnknownFormat
    );
    let zero = normalize_usage(
        Some(&json!({
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "prompt_tokens_details": {"cached_tokens": 0}
        })),
        UsageProtocol::Openai,
    )
    .unwrap();
    assert_eq!(zero.input_tokens, Some(0));
    assert_eq!(zero.cache_read_input_tokens, Some(0));
}

#[test]
fn client_event_encoding_preserves_surface_terminal_and_tool_grammar() {
    let mut text = eggpool::wire::ir::CanonicalEvent {
        event_type: CanonicalEventType::TextDelta,
        response_id: Some("r".into()),
        model: Some("m".into()),
        index: Some(0),
        delta: Some("hi".into()),
        call_id: None,
        name: None,
        arguments: None,
        finish_reason: None,
        usage: None,
        error_type: None,
        error_message: None,
    };
    assert!(
        String::from_utf8(encode_client_event(ClientSurface::ChatCompletions, &text).unwrap())
            .unwrap()
            .contains("hi")
    );
    text.event_type = CanonicalEventType::ResponseComplete;
    assert_eq!(
        encode_client_event(ClientSurface::ChatCompletions, &text).unwrap(),
        b"data: [DONE]\n\n"
    );
    text.event_type = CanonicalEventType::ToolCallArgumentsDelta;
    text.call_id = Some("call-1".into());
    text.delta = Some("{\"q\":1}".into());
    let anthropic =
        String::from_utf8(encode_client_event(ClientSurface::Messages, &text).unwrap()).unwrap();
    assert!(anthropic.contains("input_json_delta"));
    assert!(!anthropic.contains("call-1"));
}

#[test]
fn finite_codec_trait_exposes_stream_decode_and_encode() {
    let codec = eggpool::wire::OpenAiChatCodec;
    let decoded = codec
        .decode_stream_event(&json!({"data":"[DONE]"}))
        .unwrap();
    assert_eq!(
        decoded.value[0].event_type,
        CanonicalEventType::ResponseComplete
    );
    let event = decoded.value[0].clone();
    assert_eq!(
        codec
            .encode_stream_event(&event, ClientSurface::ChatCompletions)
            .unwrap(),
        b"data: [DONE]\n\n"
    );
}
