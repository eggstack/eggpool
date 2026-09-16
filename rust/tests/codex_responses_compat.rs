//! Deterministic Codex Responses compatibility contracts.
//!
//! Fixture provenance: OpenAI Codex 508a006d7aaa485ac0367c9e45c69ebb948af518
//! and OpenCodex bridge reference e4a8539b957b7ae7cd278666f0364eb0f82d4ac3.
//! Deferred `tool_search` fixtures are additionally provenanced from OpenAI
//! Codex 4701aa4b4239c70063ab6f2fcb835324f9c109f4
//! (`codex-rs/tools/src/tool_spec.rs`, `tool_discovery.rs`,
//! `core/src/tools/handlers/tool_search.rs`, `tool_search_spec.rs`,
//! `core/src/context_manager/normalize.rs`) and the public Responses
//! `tool_search` guide (client `execution`, `query`/`limit` schema,
//! `tool_search_call`/`tool_search_output` lifecycle). These commits are
//! audit markers only; none is a runtime dependency.
//!
//! Conformance maintenance policy: when a new Codex tool/item/event appears,
//! classify it as (1) native-preservation only, (2) provider-neutral portable
//! semantic, or (3) unsupported/non-portable. Only category 2 expands the
//! canonical translation. Update fixture provenance only when
//! compatibility-sensitive source behavior changes; do not churn the pinned
//! commit for unrelated upstream UI/features. Eggpool never executes
//! searches, plugins, MCP tools, or newly discovered tools itself.

use eggpool::request::{AdmissionOptions, admit_request};
use eggpool::wire::ir::{CanonicalToolKind, ClientSurface};
use eggpool::wire::{
    ClientStreamEncoder, ConfiguredWireProfile, SseDecoder, StreamAdapterKind, StreamEventDecoder,
    StreamForwardingMode, StreamTerminalOutcome, WireCodecId, WireProfileDefinition, WireRuntime,
    WireRuntimeContext, WireSurface,
};
use serde_json::{Value, json};

fn profile(surface: WireSurface) -> ConfiguredWireProfile {
    let codec = match surface {
        WireSurface::OpenaiChatCompletions => WireCodecId::OpenaiChat,
        WireSurface::OpenaiResponses => WireCodecId::OpenaiResponses,
        WireSurface::AnthropicMessages => WireCodecId::AnthropicMessages,
        WireSurface::GeminiInteractions => WireCodecId::GeminiInteractions,
        WireSurface::GeminiGenerateContent => WireCodecId::GeminiGenerateContent,
    };
    let stream_codec = match codec {
        WireCodecId::OpenaiChat => WireCodecId::OpenaiChatSse,
        WireCodecId::OpenaiResponses => WireCodecId::OpenaiResponsesSse,
        WireCodecId::AnthropicMessages => WireCodecId::AnthropicMessagesSse,
        WireCodecId::GeminiInteractions => WireCodecId::GeminiInteractionsSse,
        WireCodecId::GeminiGenerateContent => WireCodecId::GeminiGenerateContentSse,
        other => other,
    };
    ConfiguredWireProfile {
        definition: WireProfileDefinition {
            surface,
            request_codec: codec,
            response_codec: codec,
            stream_codec,
        },
        path_template: "/wire".into(),
        stream_path_template: None,
        priority: 0,
    }
}

fn context(client: ClientSurface, upstream: WireSurface) -> WireRuntimeContext {
    WireRuntimeContext::new(client, profile(upstream), "eggpool-model", "provider-model")
}

fn codex_request(input: Value) -> Vec<u8> {
    serde_json::to_vec(&input).expect("fixture JSON")
}

fn response_payloads(bytes: &[u8]) -> Vec<Value> {
    let mut decoder = SseDecoder::default();
    let mut frames = decoder.feed(bytes).expect("Responses SSE");
    frames.extend(decoder.finish().expect("Responses SSE EOF").frames);
    frames
        .iter()
        .filter_map(|frame| serde_json::from_str(&frame.data).ok())
        .collect()
}

#[test]
fn current_codex_request_fixtures_keep_native_history_and_reject_state() {
    let body = codex_request(json!({
        "model":"eggpool-model",
        "instructions":"Use the declared tools.",
        "input":[
            {"type":"message","role":"user","content":[{"type":"input_text","text":"apply it"}]},
            {"type":"custom_tool_call","id":"item-custom-1","call_id":"call-custom-1","name":"apply_patch","input":"diff --git a/a b/a"},
            {"type":"custom_tool_call_output","call_id":"call-custom-1","output":"applied"},
            {"type":"function_call","call_id":"call-function-1","name":"lookup","arguments":"{\"q\":\"egg\"}"},
            {"type":"function_call_output","call_id":"call-function-1","output":"found"}
        ],
        "tools":[
            {"type":"custom","name":"apply_patch","description":"Apply a patch"},
            {"type":"function","name":"lookup","parameters":{"type":"object","properties":{"q":{"type":"string"}}}}
        ],
        "include":["reasoning.encrypted_content"],
        "reasoning":{"effort":"high","summary":"auto"},
        "prompt_cache_key":"fixture-cache",
        "service_tier":"priority",
        "text":{"verbosity":"low"},
        "future_extension":{"enabled":true}
    }));
    let admitted = admit_request(
        &body,
        AdmissionOptions {
            client_surface: ClientSurface::Responses,
            ..AdmissionOptions::default()
        },
    )
    .expect("Codex request is admitted");
    assert_eq!(admitted.canonical.tools.len(), 2);
    assert_eq!(
        admitted.canonical.tools[0].kind,
        CanonicalToolKind::Freeform
    );
    let tool_kinds: Vec<_> = admitted
        .canonical
        .messages
        .iter()
        .flat_map(|message| message.content.iter().map(|block| block.tool_kind))
        .collect();
    assert!(tool_kinds.contains(&CanonicalToolKind::Freeform));
    assert!(tool_kinds.contains(&CanonicalToolKind::Function));
    assert_eq!(
        admitted
            .native_preservation
            .as_ref()
            .unwrap()
            .summary
            .native_input_items,
        0
    );
    assert_eq!(
        admitted
            .native_preservation
            .as_ref()
            .unwrap()
            .summary
            .native_tool_definitions,
        0
    );

    let runtime = WireRuntime::embedded().expect("registry");
    let native_context = WireRuntimeContext::new(
        ClientSurface::Responses,
        profile(WireSurface::OpenaiResponses),
        "eggpool-model",
        "eggpool-model",
    );
    let prepared = runtime
        .prepare_request(&body, &native_context)
        .expect("native request");
    assert_eq!(prepared.body.bytes, body);
    assert!(prepared.body.value.is_none());
    assert_eq!(prepared.adaptation.warning_count, 0);

    let rewritten = runtime
        .prepare_request(
            &body,
            &context(ClientSurface::Responses, WireSurface::OpenaiResponses),
        )
        .expect("native alias rewrite");
    assert_eq!(
        rewritten.body.value.as_ref().unwrap()["model"],
        "provider-model"
    );
    assert_eq!(
        rewritten.body.value.as_ref().unwrap()["future_extension"]["enabled"],
        true
    );

    let stateful = runtime
        .prepare_request(
            br#"{"model":"eggpool-model","input":"hello","store":true}"#,
            &context(ClientSurface::Responses, WireSurface::OpenaiResponses),
        )
        .expect_err("stateful Responses must fail before provider I/O");
    assert!(matches!(
        stateful,
        eggpool::wire::WireRuntimeError::ClientAdmission(error)
            if error.reason == eggpool::wire::CodecReasonCode::UnsupportedSemanticFeature
    ));
}

#[test]
fn custom_tools_use_a_deterministic_function_wrapper_on_chat() {
    let body = codex_request(json!({
        "model":"eggpool-model",
        "input":"make the change",
        "tools":[{"type":"custom","name":"apply_patch","description":"Apply a patch"}]
    }));
    let runtime = WireRuntime::embedded().expect("registry");
    let prepared = runtime
        .prepare_request(
            &body,
            &context(ClientSurface::Responses, WireSurface::OpenaiChatCompletions),
        )
        .expect("translated request");
    let value = prepared.body.value.expect("encoded request");
    assert_eq!(value["tools"][0]["type"], "function");
    assert_eq!(value["tools"][0]["function"]["name"], "apply_patch");
    assert_eq!(
        value["tools"][0]["function"]["parameters"]["required"],
        json!(["input"])
    );
    assert!(
        prepared
            .notices
            .iter()
            .any(|notice| notice.code.0 == "freeform_tool_wrapped_as_function")
    );

    let continuation = codex_request(json!({
        "model":"eggpool-model",
        "input":[
            {"type":"custom_tool_call","call_id":"call-custom-1","name":"apply_patch","input":"diff --git a/a b/a"},
            {"type":"custom_tool_call_output","call_id":"call-custom-1","output":"applied"}
        ],
        "tools":[{"type":"custom","name":"apply_patch"}]
    }));
    let prepared = runtime
        .prepare_request(
            &continuation,
            &context(ClientSurface::Responses, WireSurface::OpenaiChatCompletions),
        )
        .expect("translated continuation");
    let messages = prepared.body.value.expect("encoded continuation")["messages"]
        .as_array()
        .expect("messages")
        .clone();
    assert_eq!(
        messages[0]["tool_calls"][0]["function"]["arguments"],
        json!({"input":"diff --git a/a b/a"}).to_string()
    );
    assert_eq!(messages[1]["tool_call_id"], "call-custom-1");
    assert_eq!(messages[1]["content"], "applied");
}

#[test]
fn custom_tool_stream_requires_done_item_and_reconstructs_custom_call() {
    let tool = eggpool::wire::ir::CanonicalTool {
        kind: CanonicalToolKind::Freeform,
        name: "apply_patch".into(),
        description: None,
        parameters: Default::default(),
        cache_control: None,
        defer_loading: None,
    };
    let upstream = [
        json!({"choices":[{"delta":{"tool_calls":[{"id":"provider-call-1","index":0,"function":{"name":"apply_patch"}}]}}]}),
        json!({"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\"input\":\"patch\"}"}}]}}]}),
        json!({"choices":[{"delta":{},"finish_reason":"tool_calls"}]}),
    ]
    .into_iter()
    .map(|payload| format!("data: {payload}\n\n"))
    .chain(std::iter::once("data: [DONE]\n\n".into()))
    .collect::<String>()
    .into_bytes();
    let mut decoder = StreamEventDecoder::new(StreamAdapterKind::OpenaiChatSse);
    let mut events = decoder.push(&upstream).expect("provider stream");
    let (tail, summary) = decoder.finish().expect("stream finalization");
    events.extend(tail);
    assert_eq!(summary.outcome, StreamTerminalOutcome::Success);

    let mut encoder = ClientStreamEncoder::new_with_tools(ClientSurface::Responses, &[tool]);
    let mut downstream = Vec::new();
    for event in &events {
        downstream.extend(encoder.encode(event).expect("Responses encoding"));
    }
    let text = String::from_utf8(downstream).expect("UTF-8 SSE");
    assert!(text.contains("response.custom_tool_call_input.delta"));
    assert!(text.contains("\"type\":\"custom_tool_call\""));
    assert!(text.contains("\"input\":\"patch\""));
    assert!(!text.contains("\"type\":\"function_call\""));
    assert!(text.contains("response.output_item.done"));
}

#[test]
fn malformed_freeform_wrapper_is_not_forwarded_as_json_text() {
    let tool = eggpool::wire::ir::CanonicalTool {
        kind: CanonicalToolKind::Freeform,
        name: "apply_patch".into(),
        description: None,
        parameters: Default::default(),
        cache_control: None,
        defer_loading: None,
    };
    let mut encoder = ClientStreamEncoder::new_with_tools(ClientSurface::Responses, &[tool]);
    encoder
        .encode(&eggpool::wire::ir::CanonicalEvent {
            event_type: eggpool::wire::ir::CanonicalEventType::ToolCallStart,
            response_id: None,
            model: None,
            index: Some(0),
            delta: None,
            call_id: Some("call-1".into()),
            name: Some("apply_patch".into()),
            arguments: None,
            finish_reason: None,
            usage: None,
            error_type: None,
            error_message: None,
        })
        .expect("start");
    let error = encoder
        .encode(&eggpool::wire::ir::CanonicalEvent {
            event_type: eggpool::wire::ir::CanonicalEventType::ToolCallStop,
            response_id: None,
            model: None,
            index: Some(0),
            delta: None,
            call_id: Some("call-1".into()),
            name: Some("apply_patch".into()),
            arguments: Some("not a wrapper".into()),
            finish_reason: None,
            usage: None,
            error_type: None,
            error_message: None,
        })
        .expect_err("malformed wrapper must fail");
    assert_eq!(
        error.reason,
        eggpool::wire::CodecReasonCode::MalformedProviderEvent
    );
}

#[test]
fn translated_streams_select_mode_and_reject_premature_eof() {
    let runtime = WireRuntime::embedded().expect("registry");
    let chat_context = context(ClientSurface::Responses, WireSurface::OpenaiChatCompletions);
    let stream = runtime.stream(&chat_context).expect("translated stream");
    assert_eq!(stream.forwarding_mode(), StreamForwardingMode::Translated);

    let native = runtime
        .stream(&context(
            ClientSurface::Responses,
            WireSurface::OpenaiResponses,
        ))
        .expect("native stream");
    assert_eq!(
        native.forwarding_mode(),
        StreamForwardingMode::NativeObserved
    );

    let mut decoder = StreamEventDecoder::new(StreamAdapterKind::OpenaiResponsesSse);
    decoder
        .push(b"event: response.created\ndata: {\"type\":\"response.created\"}\n\n")
        .expect("created");
    let (_, summary) = decoder.finish().expect("EOF classification");
    assert_eq!(summary.outcome, StreamTerminalOutcome::EofAfterPartialBody);
}

#[test]
fn translated_parallel_tool_calls_accumulate_by_source_index() {
    let upstream = [
        json!({"choices":[{"delta":{"tool_calls":[
            {"index":0,"id":"call_z","type":"function","function":{"name":"lookup_a","arguments":""}},
            {"index":1,"id":"call_a","type":"function","function":{"name":"lookup_b","arguments":""}}
        ]}}]}),
        json!({"choices":[{"delta":{"tool_calls":[{"index":1,"function":{"arguments":"{\"q\":\"b"}}]}}]}),
        json!({"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\"q\":\"a"}}]}}]}),
        json!({"choices":[{"delta":{"tool_calls":[{"index":1,"function":{"arguments":"\"}"}}]}}]}),
        json!({"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\"}"}}]}}]}),
        json!({"choices":[{"delta":{},"finish_reason":"tool_calls"}]}),
    ]
    .into_iter()
    .map(|payload| format!("data: {payload}\n\n"))
    .chain(std::iter::once("data: [DONE]\n\n".into()))
    .collect::<String>()
    .into_bytes();

    let mut decoder = StreamEventDecoder::new(StreamAdapterKind::OpenaiChatSse);
    let mut events = decoder.push(&upstream).expect("provider stream");
    let (tail, summary) = decoder.finish().expect("stream finalization");
    events.extend(tail);
    assert_eq!(summary.outcome, StreamTerminalOutcome::Success);

    let mut encoder = ClientStreamEncoder::new(ClientSurface::Responses);
    let mut downstream = Vec::new();
    for event in &events {
        downstream.extend(encoder.encode(event).expect("Responses encoding"));
    }
    let payloads = response_payloads(&downstream);
    let done_items: Vec<&Value> = payloads
        .iter()
        .filter(|payload| payload["type"] == "response.output_item.done")
        .collect();
    assert_eq!(done_items.len(), 2);

    let mut calls = done_items
        .iter()
        .map(|payload| {
            let item = &payload["item"];
            (
                item["call_id"].as_str().expect("call ID"),
                item["id"].as_str().expect("item ID"),
                payload["output_index"].as_u64().expect("output index"),
                item["name"].as_str().expect("tool name"),
                item["arguments"].as_str().expect("arguments"),
                item["status"].as_str().expect("status"),
            )
        })
        .collect::<Vec<_>>();
    calls.sort_by_key(|call| call.2);
    assert_eq!(calls[0].0, "call_z");
    assert_eq!(calls[0].3, "lookup_a");
    assert_eq!(calls[0].4, r#"{"q":"a"}"#);
    assert_eq!(calls[0].5, "completed");
    assert_eq!(calls[1].0, "call_a");
    assert_eq!(calls[1].3, "lookup_b");
    assert_eq!(calls[1].4, r#"{"q":"b"}"#);
    assert_eq!(calls[1].5, "completed");
    assert!(!calls[0].1.is_empty());
    assert!(!calls[1].1.is_empty());
    assert_ne!(calls[0].0, calls[0].1);
    assert_ne!(calls[1].0, calls[1].1);
    assert_ne!(calls[0].1, calls[1].1);
    assert_ne!(calls[0].2, calls[1].2);

    let terminal_index = payloads
        .iter()
        .position(|payload| payload["type"] == "response.completed")
        .expect("successful terminal");
    assert_eq!(
        payloads
            .iter()
            .filter(|payload| payload["type"] == "response.completed")
            .count(),
        1
    );
    assert_eq!(
        payloads[..terminal_index]
            .iter()
            .filter(|payload| payload["type"] == "response.output_item.done")
            .count(),
        2
    );
}

#[test]
fn next_turn_tool_outputs_keep_call_pairing_when_order_is_reversed() {
    let continuation = codex_request(json!({
        "model":"eggpool-model",
        "input":[
            {"type":"function_call_output","call_id":"call_a","output":"result-for-b"},
            {"type":"function_call_output","call_id":"call_z","output":"result-for-a"}
        ]
    }));
    let admitted = admit_request(
        &continuation,
        AdmissionOptions {
            client_surface: ClientSurface::Responses,
            ..AdmissionOptions::default()
        },
    )
    .expect("reversed tool outputs are admitted");

    let outputs = admitted
        .canonical
        .messages
        .iter()
        .map(|message| {
            (
                message
                    .tool_call_id
                    .as_deref()
                    .expect("tool output call ID"),
                message.content[0].text.as_deref().expect("tool output"),
            )
        })
        .collect::<Vec<_>>();
    assert_eq!(
        outputs,
        [("call_a", "result-for-b"), ("call_z", "result-for-a")]
    );
}

// Plan 201: deferred `tool_search` compatibility. Provenance: Codex
// 4701aa4b4239c70063ab6f2fcb835324f9c109f4. Only client-executed search
// (`execution: client` with a defined `call_id`) is portable; hosted/server
// search stays native-only. Eggpool never executes the search itself.

fn tool_search_declaration() -> Value {
    json!({
        "type": "tool_search",
        "execution": "client",
        "description": "Searches over deferred tool metadata with BM25.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query for deferred tools."},
                "limit": {"type": "number", "description": "Maximum number of tools to return. Defaults to 8."}
            },
            "required": ["query"],
            "additionalProperties": false
        }
    })
}

#[test]
fn deferred_tool_search_native_declaration_and_history_are_preserved() {
    let body = codex_request(json!({
        "model": "eggpool-model",
        "input": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "find it"}]},
            {"type": "tool_search_call", "id": "tsc_1", "call_id": "call-search-1", "execution": "client", "status": "completed", "arguments": {"query": "create calendar event", "limit": 8}},
            {"type": "tool_search_output", "execution": "client", "call_id": "call-search-1", "status": "completed", "tools": [
                {"type": "function", "name": "get_shipping_eta", "description": "Look up shipping ETA.", "defer_loading": true, "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"], "additionalProperties": false}}
            ]}
        ],
        "tools": [tool_search_declaration()],
    }));
    let admitted = admit_request(
        &body,
        AdmissionOptions {
            client_surface: ClientSurface::Responses,
            ..AdmissionOptions::default()
        },
    )
    .expect("client tool_search is admitted");
    assert_eq!(admitted.canonical.tools.len(), 1);
    assert_eq!(
        admitted.canonical.tools[0].kind,
        CanonicalToolKind::DeferredSearch
    );
    assert_eq!(admitted.canonical.tools[0].name, "tool_search");
    // Portable search does not count as a native cross-surface blocker.
    assert_eq!(
        admitted
            .native_preservation
            .as_ref()
            .unwrap()
            .summary
            .native_tool_definitions,
        0
    );
    assert_eq!(
        admitted
            .native_preservation
            .as_ref()
            .unwrap()
            .summary
            .native_input_items,
        0
    );

    let runtime = WireRuntime::embedded().expect("registry");
    let native = WireRuntimeContext::new(
        ClientSurface::Responses,
        profile(WireSurface::OpenaiResponses),
        "eggpool-model",
        "eggpool-model",
    );
    let prepared = runtime
        .prepare_request(&body, &native)
        .expect("native request");
    assert_eq!(prepared.body.bytes.as_ref(), body.as_slice());
}

#[test]
fn deferred_tool_search_uses_a_deterministic_function_wrapper_on_chat() {
    let body = codex_request(json!({
        "model": "eggpool-model",
        "input": "find the shipping tool",
        "tools": [tool_search_declaration()]
    }));
    let runtime = WireRuntime::embedded().expect("registry");
    let prepared = runtime
        .prepare_request(
            &body,
            &context(ClientSurface::Responses, WireSurface::OpenaiChatCompletions),
        )
        .expect("translated request");
    let value = prepared.body.value.expect("encoded request");
    assert_eq!(value["tools"][0]["type"], "function");
    assert_eq!(value["tools"][0]["function"]["name"], "tool_search");
    let params = &value["tools"][0]["function"]["parameters"];
    assert_eq!(params["required"], json!(["query"]));
    assert!(params["properties"].get("query").is_some());
    assert!(
        prepared
            .notices
            .iter()
            .any(|notice| notice.code.0 == "deferred_tool_search_wrapped_as_function")
    );

    // Next-turn output becomes an ordinary function result, not user text.
    let continuation = codex_request(json!({
        "model": "eggpool-model",
        "input": [
            {"type": "tool_search_call", "call_id": "call-search-1", "execution": "client", "status": "completed", "arguments": {"query": "shipping", "limit": 1}},
            {"type": "tool_search_output", "execution": "client", "call_id": "call-search-1", "status": "completed", "tools": [
                {"type": "function", "name": "get_shipping_eta", "description": "Look up shipping ETA.", "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}}}}
            ]}
        ],
        "tools": [tool_search_declaration()]
    }));
    let prepared = runtime
        .prepare_request(
            &continuation,
            &context(ClientSurface::Responses, WireSurface::OpenaiChatCompletions),
        )
        .expect("translated continuation");
    let encoded = prepared.body.value.expect("encoded continuation");
    let messages = encoded["messages"].as_array().expect("messages");
    // Assistant search call projects as a function call with the bounded
    // query object preserved verbatim.
    assert_eq!(
        messages[0]["tool_calls"][0]["function"]["name"],
        "tool_search"
    );
    let args: Value = serde_json::from_str(
        messages[0]["tool_calls"][0]["function"]["arguments"]
            .as_str()
            .unwrap(),
    )
    .expect("wrapper arguments are JSON");
    assert_eq!(args["query"], "shipping");
    // Search output projects as a tool result paired by call_id.
    assert_eq!(messages[1]["tool_call_id"], "call-search-1");
    let tools: Value =
        serde_json::from_str(messages[1]["content"].as_str().unwrap()).expect("tools JSON");
    assert_eq!(tools[0]["name"], "get_shipping_eta");
}

#[test]
fn deferred_tool_search_stream_reconstructs_call_with_stable_ids() {
    let tool = eggpool::wire::ir::CanonicalTool {
        kind: CanonicalToolKind::DeferredSearch,
        name: "tool_search".into(),
        description: None,
        parameters: eggpool::wire::ir::default_tool_search_parameters(),
        cache_control: None,
        defer_loading: None,
    };
    // Upstream calls the wrapper function; arguments arrive split across
    // deltas and interleaved by source index in the parallel test below.
    let upstream = [
        json!({"choices":[{"delta":{"tool_calls":[{"id":"provider-call-9","index":0,"function":{"name":"tool_search"}}]}}]}),
        json!({"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\"query\":\"cal"}}]}}]}),
        json!({"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"endar\",\"limit\":1}"}}]}}]}),
        json!({"choices":[{"delta":{},"finish_reason":"tool_calls"}]}),
    ]
    .into_iter()
    .map(|payload| format!("data: {payload}\n\n"))
    .chain(std::iter::once("data: [DONE]\n\n".into()))
    .collect::<String>()
    .into_bytes();
    let mut decoder = StreamEventDecoder::new(StreamAdapterKind::OpenaiChatSse);
    let mut events = decoder.push(&upstream).expect("provider stream");
    let (tail, summary) = decoder.finish().expect("stream finalization");
    events.extend(tail);
    assert_eq!(summary.outcome, StreamTerminalOutcome::Success);

    let mut encoder =
        ClientStreamEncoder::new_with_tools(ClientSurface::Responses, std::slice::from_ref(&tool));
    let mut downstream = Vec::new();
    for event in &events {
        downstream.extend(encoder.encode(event).expect("Responses encoding"));
    }
    let text = String::from_utf8(downstream).expect("UTF-8 SSE");
    assert!(text.contains("\"type\":\"tool_search_call\""));
    assert!(text.contains("\"execution\":\"client\""));
    assert!(text.contains("\"call_id\":\"provider-call-9\""));
    assert!(text.contains("\"query\":\"calendar\""));
    assert!(!text.contains("\"type\":\"function_call\""));
    assert!(text.contains("response.output_item.done"));

    let payloads = response_payloads(&downstream_with_done(&tool, &events));
    let done = payloads
        .iter()
        .find(|payload| payload["type"] == "response.output_item.done")
        .expect("authoritative done");
    let item = &done["item"];
    assert_eq!(item["type"], "tool_search_call");
    assert_eq!(item["call_id"], "provider-call-9");
    assert_ne!(
        item["id"].as_str().expect("item ID"),
        item["call_id"].as_str().expect("call ID"),
        "item ID and call_id stay distinct"
    );
    assert!(!item["id"].as_str().unwrap().is_empty());
}

fn downstream_with_done(
    tool: &eggpool::wire::ir::CanonicalTool,
    events: &[eggpool::wire::ir::CanonicalEvent],
) -> Vec<u8> {
    let mut encoder =
        ClientStreamEncoder::new_with_tools(ClientSurface::Responses, std::slice::from_ref(tool));
    let mut out = Vec::new();
    for event in events {
        out.extend(encoder.encode(event).expect("encode"));
    }
    out
}

#[test]
fn deferred_parallel_calls_do_not_cross_contaminate_wrapper_state() {
    let function = eggpool::wire::ir::CanonicalTool {
        kind: CanonicalToolKind::Function,
        name: "lookup".into(),
        description: None,
        parameters: Default::default(),
        cache_control: None,
        defer_loading: None,
    };
    let freeform = eggpool::wire::ir::CanonicalTool {
        kind: CanonicalToolKind::Freeform,
        name: "apply_patch".into(),
        description: None,
        parameters: Default::default(),
        cache_control: None,
        defer_loading: None,
    };
    let deferred = eggpool::wire::ir::CanonicalTool {
        kind: CanonicalToolKind::DeferredSearch,
        name: "tool_search".into(),
        description: None,
        parameters: eggpool::wire::ir::default_tool_search_parameters(),
        cache_control: None,
        defer_loading: None,
    };
    let tools = [&function, &freeform, &deferred]
        .into_iter()
        .cloned()
        .collect::<Vec<_>>();
    let upstream = [
        json!({"choices":[{"delta":{"tool_calls":[
            {"index":0,"id":"call-func-1","type":"function","function":{"name":"lookup","arguments":""}},
            {"index":1,"id":"call-free-1","type":"function","function":{"name":"apply_patch","arguments":""}},
            {"index":2,"id":"call-search-1","type":"function","function":{"name":"tool_search","arguments":""}}
        ]}}]}),
        json!({"choices":[{"delta":{"tool_calls":[{"index":2,"function":{"arguments":"{\"query\":\""}}]}}]}),
        json!({"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\"q\":\"a"}}]}}]}),
        json!({"choices":[{"delta":{"tool_calls":[{"index":1,"function":{"arguments":"{\"input\":\""}}]}}]}),
        json!({"choices":[{"delta":{"tool_calls":[{"index":2,"function":{"arguments":"docs\",\"limit\":"}}]}}]}),
        json!({"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\"}"}}]}}]}),
        json!({"choices":[{"delta":{"tool_calls":[{"index":1,"function":{"arguments":"patch\"}"}}]}}]}),
        json!({"choices":[{"delta":{"tool_calls":[{"index":2,"function":{"arguments":"2}"}}]}}]}),
        json!({"choices":[{"delta":{},"finish_reason":"tool_calls"}]}),
    ]
    .into_iter()
    .map(|payload| format!("data: {payload}\n\n"))
    .chain(std::iter::once("data: [DONE]\n\n".into()))
    .collect::<String>()
    .into_bytes();
    let mut decoder = StreamEventDecoder::new(StreamAdapterKind::OpenaiChatSse);
    let mut events = decoder.push(&upstream).expect("provider stream");
    let (tail, summary) = decoder.finish().expect("finalization");
    events.extend(tail);
    assert_eq!(summary.outcome, StreamTerminalOutcome::Success);

    let mut encoder = ClientStreamEncoder::new_with_tools(ClientSurface::Responses, &tools);
    let mut downstream = Vec::new();
    for event in &events {
        downstream.extend(encoder.encode(event).expect("encode"));
    }
    let payloads = response_payloads(&downstream);
    let done: Vec<&Value> = payloads
        .iter()
        .filter(|payload| payload["type"] == "response.output_item.done")
        .collect();
    assert_eq!(done.len(), 3);
    let mut kinds: Vec<&str> = done
        .iter()
        .map(|payload| payload["item"]["type"].as_str().unwrap())
        .collect();
    kinds.sort_unstable();
    assert_eq!(
        kinds,
        ["custom_tool_call", "function_call", "tool_search_call"]
    );
    let search = done
        .iter()
        .find(|payload| payload["item"]["type"] == "tool_search_call")
        .unwrap();
    assert_eq!(search["item"]["call_id"], "call-search-1");
    assert_eq!(search["item"]["execution"], "client");
    let ids: Vec<&str> = done
        .iter()
        .map(|payload| payload["item"]["id"].as_str().unwrap())
        .collect();
    assert_eq!(ids.len(), 3);
    assert_ne!(ids[0], ids[1]);
    assert_ne!(ids[1], ids[2]);
    assert_ne!(ids[0], ids[2]);
}

#[test]
fn malformed_tool_search_wrapper_fails_closed() {
    let tool = eggpool::wire::ir::CanonicalTool {
        kind: CanonicalToolKind::DeferredSearch,
        name: "tool_search".into(),
        description: None,
        parameters: eggpool::wire::ir::default_tool_search_parameters(),
        cache_control: None,
        defer_loading: None,
    };
    let mut encoder = ClientStreamEncoder::new_with_tools(ClientSurface::Responses, &[tool]);
    encoder
        .encode(&eggpool::wire::ir::CanonicalEvent {
            event_type: eggpool::wire::ir::CanonicalEventType::ToolCallStart,
            response_id: None,
            model: None,
            index: Some(0),
            delta: None,
            call_id: Some("call-search-1".into()),
            name: Some("tool_search".into()),
            arguments: None,
            finish_reason: None,
            usage: None,
            error_type: None,
            error_message: None,
        })
        .expect("start");
    // Missing required `query` must not reach Codex as a native call.
    let error = encoder
        .encode(&eggpool::wire::ir::CanonicalEvent {
            event_type: eggpool::wire::ir::CanonicalEventType::ToolCallStop,
            response_id: None,
            model: None,
            index: Some(0),
            delta: None,
            call_id: Some("call-search-1".into()),
            name: Some("tool_search".into()),
            arguments: Some("{\"limit\":0}".into()),
            finish_reason: None,
            usage: None,
            error_type: None,
            error_message: None,
        })
        .expect_err("malformed search arguments must fail");
    assert_eq!(
        error.reason,
        eggpool::wire::CodecReasonCode::MalformedProviderEvent
    );

    // Admission also fails closed on malformed history.
    let malformed = codex_request(json!({
        "model": "eggpool-model",
        "input": [
            {"type": "tool_search_call", "call_id": "call-search-1", "execution": "client", "status": "completed", "arguments": {"limit": 0}}
        ],
        "tools": [tool_search_declaration()]
    }));
    let error = admit_request(
        &malformed,
        AdmissionOptions {
            client_surface: ClientSurface::Responses,
            ..AdmissionOptions::default()
        },
    )
    .expect_err("malformed history must fail");
    assert!(matches!(
        error,
        eggpool::request::AdmissionError::InvalidField { .. }
    ));
}

#[test]
fn ordinary_function_named_tool_search_is_never_reclassified_by_name() {
    // An ordinary function named `tool_search` without declaration metadata
    // stays a function end to end.
    let body = codex_request(json!({
        "model": "eggpool-model",
        "input": "run it",
        "tools": [{"type": "function", "name": "tool_search", "parameters": {"type": "object", "properties": {"q": {"type": "string"}}}}]
    }));
    let admitted = admit_request(
        &body,
        AdmissionOptions {
            client_surface: ClientSurface::Responses,
            ..AdmissionOptions::default()
        },
    )
    .expect("ordinary function is admitted");
    assert_eq!(admitted.canonical.tools.len(), 1);
    assert_eq!(
        admitted.canonical.tools[0].kind,
        CanonicalToolKind::Function
    );

    let runtime = WireRuntime::embedded().expect("registry");
    let prepared = runtime
        .prepare_request(
            &body,
            &context(ClientSurface::Responses, WireSurface::OpenaiChatCompletions),
        )
        .expect("translated request");
    let value = prepared.body.value.expect("encoded");
    assert_eq!(value["tools"][0]["function"]["name"], "tool_search");
    assert!(
        !prepared
            .notices
            .iter()
            .any(|notice| notice.code.0 == "deferred_tool_search_wrapped_as_function")
    );

    // Upstream calling that ordinary function must come back as a function
    // call, not a `tool_search_call`, because translation identity is
    // declaration-scoped.
    let upstream = [
        json!({"choices":[{"delta":{"tool_calls":[{"id":"call-ord-1","index":0,"function":{"name":"tool_search"}}]}}]}),
        json!({"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\"q\":\"egg\"}"}}]}}]}),
        json!({"choices":[{"delta":{},"finish_reason":"tool_calls"}]}),
    ]
    .into_iter()
    .map(|payload| format!("data: {payload}\n\n"))
    .chain(std::iter::once("data: [DONE]\n\n".into()))
    .collect::<String>()
    .into_bytes();
    let mut decoder = StreamEventDecoder::new(StreamAdapterKind::OpenaiChatSse);
    let mut events = decoder.push(&upstream).expect("stream");
    let (tail, _) = decoder.finish().expect("finish");
    events.extend(tail);
    let ordinary = eggpool::wire::ir::CanonicalTool {
        kind: CanonicalToolKind::Function,
        name: "tool_search".into(),
        description: None,
        parameters: Default::default(),
        cache_control: None,
        defer_loading: None,
    };
    let mut encoder = ClientStreamEncoder::new_with_tools(ClientSurface::Responses, &[ordinary]);
    let mut downstream = Vec::new();
    for event in &events {
        downstream.extend(encoder.encode(event).expect("encode"));
    }
    let text = String::from_utf8(downstream).expect("utf8");
    assert!(text.contains("\"type\":\"function_call\""));
    assert!(!text.contains("tool_search_call"));
}

#[test]
fn hosted_tool_search_is_rejected_before_provider_dispatch() {
    // Bare/hosted `tool_search` has no portable function wrapper. It must
    // fail before provider I/O rather than silently disappearing (which
    // would change model behavior).
    let body = codex_request(json!({
        "model": "eggpool-model",
        "input": "list orders",
        "tools": [{"type": "tool_search"}]
    }));
    let runtime = WireRuntime::embedded().expect("registry");
    let error = runtime
        .prepare_request(
            &body,
            &context(ClientSurface::Responses, WireSurface::OpenaiChatCompletions),
        )
        .expect_err("hosted search must not route cross-surface");
    assert!(matches!(
        error,
        eggpool::wire::WireRuntimeError::RequestAdaptation(error)
            if error.reason == eggpool::wire::CodecReasonCode::UnsupportedSemanticFeature
    ));

    // Hosted history is likewise native-only.
    let history = codex_request(json!({
        "model": "eggpool-model",
        "input": [
            {"type": "tool_search_call", "execution": "server", "call_id": null, "status": "completed", "arguments": {"paths": ["crm"]}},
            {"type": "tool_search_output", "execution": "server", "call_id": null, "status": "completed", "tools": []}
        ],
        "tools": [{"type": "tool_search"}]
    }));
    let error = runtime
        .prepare_request(
            &history,
            &context(ClientSurface::Responses, WireSurface::OpenaiChatCompletions),
        )
        .expect_err("hosted history must not route cross-surface");
    assert!(matches!(
        error,
        eggpool::wire::WireRuntimeError::RequestAdaptation(_)
    ));
}

#[test]
fn unknown_future_native_tool_event_still_preserved_on_responses_route() {
    let body = codex_request(json!({
        "model": "eggpool-model",
        "input": "hello",
        "tools": [tool_search_declaration()],
        "future_tool_event": {"type": "tool_search_future", "execution": "client"}
    }));
    let runtime = WireRuntime::embedded().expect("registry");
    let native = runtime
        .prepare_request(
            &body,
            &context(ClientSurface::Responses, WireSurface::OpenaiResponses),
        )
        .expect("native request");
    // Same-surface forwarding rewrites only the EggPool-owned model field;
    // unknown/current tool-search-adjacent extensions survive byte-for-byte
    // under the native observe-and-forward path.
    let value: Value = serde_json::from_slice(&native.body.bytes).expect("json");
    assert_eq!(value["future_tool_event"]["type"], "tool_search_future");

    let rewritten = runtime
        .prepare_request(
            &body,
            &WireRuntimeContext::new(
                ClientSurface::Responses,
                profile(WireSurface::OpenaiResponses),
                "eggpool-model",
                "provider-model",
            ),
        )
        .expect("alias rewrite");
    let rewritten_value = rewritten.body.value.expect("rewritten");
    assert_eq!(rewritten_value["model"], "provider-model");
    assert_eq!(
        rewritten_value["future_tool_event"]["type"],
        "tool_search_future"
    );
}
