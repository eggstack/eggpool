use std::collections::BTreeSet;

use eggpool::{
    request::{
        AdmissionError, AdmissionOptions, StaticRoutingFacts, admit_request,
        affinity_identity_input, base64_definitely_exceeds, encode_compact_json,
        estimate_context_input_tokens, estimate_input_tokens, estimate_reservation_tokens,
        validate_base64,
    },
    routing::ThinkingRequirement,
    wire::ir::{
        CanonicalBlockKind, CanonicalRole, ClientSurface, Presence, ReasoningMode, ToolChoiceMode,
    },
};
use serde_json::json;

fn admit(value: serde_json::Value, surface: ClientSurface) -> eggpool::request::AdmittedRequest {
    let bytes = serde_json::to_vec(&value).expect("JSON");
    admit_request(
        &bytes,
        AdmissionOptions {
            client_surface: surface,
            ..AdmissionOptions::default()
        },
    )
    .expect("admission")
}

#[test]
fn minimal_requests_are_admitted_without_retaining_the_body() {
    let request = admit(
        json!({"model":"fixture-model"}),
        ClientSurface::ChatCompletions,
    );
    assert_eq!(request.canonical.model, "fixture-model");
    assert!(request.canonical.messages.is_empty());
    assert_eq!(request.raw_body_bytes, 25);
    assert_eq!(request.reservation_tokens, 1_000);
    assert_eq!(request.context_tokens, 1_000);
}

#[test]
fn rich_chat_request_preserves_roles_tools_media_and_controls() {
    let request = admit(
        json!({
            "model":"fixture-model",
            "messages":[
                {"role":"system","content":"stable"},
                {"role":"developer","content":"precise"},
                {"role":"user","content":[
                    {"type":"text","text":"Hello, 世界"},
                    {"type":"image_url","image_url":{"url":"data:image/png;base64,AAEC"}}
                ]},
                {"role":"assistant","content":"call","tool_calls":[
                    {"id":"call-1","type":"function","function":{"name":"lookup","arguments":"{\"q\":\"x\"}"}}
                ]},
                {"role":"tool","tool_call_id":"call-1","content":"result"}
            ],
            "stream":true,
            "max_tokens":32,
            "temperature":0,
            "top_p":0.5,
            "stop":["<END>"],
            "tools":[{"type":"function","function":{"name":"lookup","description":"find","parameters":{"type":"object"}}}],
            "tool_choice":{"type":"function","function":{"name":"lookup"}},
            "parallel_tool_calls":false,
            "reasoning_effort":"medium",
            "response_format":{"type":"json_schema"}
        }),
        ClientSurface::ChatCompletions,
    );
    let messages = &request.canonical.messages;
    assert_eq!(
        messages
            .iter()
            .map(|message| message.role)
            .collect::<Vec<_>>(),
        vec![
            CanonicalRole::System,
            CanonicalRole::Developer,
            CanonicalRole::User,
            CanonicalRole::Assistant,
            CanonicalRole::Tool,
        ]
    );
    assert_eq!(messages[2].content[1].kind, CanonicalBlockKind::Image);
    assert_eq!(
        messages[2].content[1]
            .media
            .as_ref()
            .unwrap()
            .data
            .as_deref(),
        Some("AAEC")
    );
    assert_eq!(messages[3].content[1].call_id.as_deref(), Some("call-1"));
    assert_eq!(request.canonical.tools[0].name, "lookup");
    assert_eq!(
        request.canonical.tool_choice.as_ref().unwrap().mode,
        ToolChoiceMode::Function
    );
    assert_eq!(request.canonical.reasoning.mode, ReasoningMode::Effort);
    assert_eq!(
        request.canonical.reasoning.effort.as_deref(),
        Some("medium")
    );
}

#[test]
fn responses_and_messages_keep_surface_specific_intent() {
    let responses = admit(
        json!({
        "model":"fixture-model", "input":"Hello, 世界", "stream":true,
        "max_output_tokens":32, "reasoning":{"effort":"low"},
        "text":{"format":{"type":"json_schema","name":"answer"}}
        }),
        ClientSurface::Responses,
    );
    assert_eq!(responses.canonical.client_surface, ClientSurface::Responses);
    assert_eq!(responses.canonical.messages[0].role, CanonicalRole::User);
    assert_eq!(responses.canonical.reasoning.effort.as_deref(), Some("low"));
    assert_eq!(
        responses
            .canonical
            .response_format
            .as_ref()
            .and_then(|format| format.get("type"))
            .and_then(|value| value.as_str()),
        Some("json_schema")
    );

    let messages = admit(
        json!({
            "model":"fixture-model",
            "system":[{"type":"text","text":"system"}],
            "messages":[{"role":"user","content":[{"type":"text","text":"hello"}]},
                         {"role":"assistant","content":[{"type":"thinking","thinking":"briefly"}]}],
            "thinking":{"type":"enabled","budget_tokens":128},
            "max_tokens":32
        }),
        ClientSurface::Messages,
    );
    assert_eq!(messages.canonical.messages[0].role, CanonicalRole::System);
    assert_eq!(
        messages.canonical.reasoning.mode,
        ReasoningMode::FixedBudget
    );
    assert_eq!(messages.canonical.reasoning.budget_tokens, Some(128));
}

#[test]
fn presence_distinguishes_missing_null_false_and_zero() {
    let request = admit(
        json!({
            "model":"fixture-model", "temperature":0, "top_p":null,
            "stream":false, "max_tokens":0
        }),
        ClientSurface::ChatCompletions,
    );
    assert_eq!(request.canonical.presence.temperature, Presence::Value(0.0));
    assert_eq!(request.canonical.presence.top_p, Presence::Null);
    assert_eq!(request.canonical.presence.stream, Presence::Value(false));
    assert_eq!(
        request.canonical.presence.max_output_tokens,
        Presence::Value(0)
    );
    let missing = admit(
        json!({"model":"fixture-model"}),
        ClientSurface::ChatCompletions,
    );
    assert_eq!(missing.canonical.presence.temperature, Presence::Missing);
    assert_eq!(missing.canonical.presence.stream, Presence::Missing);
}

#[test]
fn malformed_and_oversized_requests_fail_before_canonicalization() {
    assert!(matches!(
        admit_request(b"{", AdmissionOptions::default()),
        Err(AdmissionError::InvalidJson)
    ));
    assert!(matches!(
        admit_request(b"[]", AdmissionOptions::default()),
        Err(AdmissionError::TopLevelNotObject)
    ));
    assert!(matches!(
        admit_request(br#"{"messages":[]}"#, AdmissionOptions::default()),
        Err(AdmissionError::InvalidModel)
    ));
    assert!(matches!(
        admit_request(
            b"{}",
            AdmissionOptions {
                max_body_bytes: 1,
                ..AdmissionOptions::default()
            }
        ),
        Err(AdmissionError::BodyTooLarge { .. })
    ));
    let invalid_role =
        serde_json::to_vec(&json!({"model":"m","messages":[{"role":"alien","content":"x"}]}))
            .unwrap();
    assert!(matches!(
        admit_request(&invalid_role, AdmissionOptions::default()),
        Err(AdmissionError::InvalidField {
            field: "messages[].role"
        })
    ));
}

#[test]
fn limits_are_bounded_and_base64_rejects_obvious_oversize_without_decoding() {
    assert_eq!(estimate_input_tokens(b""), 1_000);
    assert_eq!(estimate_reservation_tokens(&vec![b'x'; 500_000]), 128_000);
    assert!(estimate_context_input_tokens(b"x", &json!({"text":"x"}), 0) >= 1_000);
    let encoded = "A".repeat(8 * 1024 * 1024);
    assert!(base64_definitely_exceeds(&encoded, 5 * 1024 * 1024));
    assert!(validate_base64(&encoded, 5 * 1024 * 1024, "image").is_err());
    assert_eq!(
        validate_base64("AAEC", 5 * 1024 * 1024, "image").unwrap(),
        3
    );
    assert!(validate_base64("not-base64", 5 * 1024 * 1024, "image").is_err());
}

#[test]
fn request_body_encoding_is_compact_and_redacted_debug_has_no_client_content() {
    let value = json!({"model":"m","messages":[{"role":"user","content":"secret-sentinel"}]});
    let encoded = encode_compact_json(&value).expect("encode");
    assert_eq!(
        encoded.bytes.as_ref(),
        br#"{"model":"m","messages":[{"role":"user","content":"secret-sentinel"}]}"#
    );
    let request = admit(value, ClientSurface::ChatCompletions);
    let debug = format!("{request:?}");
    assert!(!debug.contains("secret-sentinel"));
}

#[test]
fn m5_routing_and_affinity_bridges_are_pure_and_bounded() {
    let request = admit(
        json!({
            "model":"fixture-model/opencode-go", "messages":[
                {"role":"system","content":"stable system"},
                {"role":"developer","content":"developer"},
                {"role":"user","content":"first question"}
            ], "reasoning_effort":"high"
        }),
        ClientSurface::ChatCompletions,
    );
    let mut inputs = StaticRoutingFacts {
        known_provider_ids: BTreeSet::from(["opencode-go".into()]),
        requested_protocol: Some("openai".into()),
        transcode_protocols: vec!["anthropic".into()],
        now: 42,
        ..StaticRoutingFacts::default()
    };
    inputs
        .capability_policy
        .insert("unsupported_control".into(), "reject".into());
    let facts = request.routing_facts(&inputs);
    assert_eq!(facts.canonical_model_id, "fixture-model");
    assert_eq!(facts.provider_id.as_deref(), Some("opencode-go"));
    assert_eq!(facts.client_protocol.as_deref(), Some("openai"));
    assert_eq!(facts.request_surface, "chat_completions");
    assert_eq!(facts.projected_tokens, request.reservation_tokens as i64);
    assert_eq!(
        facts
            .thinking
            .as_ref()
            .and_then(|thinking| thinking.effort.as_deref()),
        Some("high")
    );
    assert_eq!(facts.now, 42);
    assert!(matches!(
        facts.thinking,
        Some(ThinkingRequirement {
            requested: true,
            ..
        })
    ));

    let automatic = affinity_identity_input(&request.canonical, None);
    assert!(automatic.session_identity().is_some());
    let explicit = affinity_identity_input(&request.canonical, Some("fixture-session"));
    assert!(explicit.session_identity().is_some());
    assert!(!format!("{explicit:?}").contains("fixture-session"));
}

#[test]
fn explicit_disable_and_fixed_budget_do_not_collapse() {
    let disabled = admit(
        json!({"model":"m","reasoning":{"enabled":false}}),
        ClientSurface::Responses,
    );
    assert_eq!(disabled.canonical.reasoning.requested, Some(false));
    assert!(disabled.canonical.reasoning.explicit_disable);
    let fixed = admit(
        json!({"model":"m","thinking_budget":128}),
        ClientSurface::ChatCompletions,
    );
    assert_eq!(fixed.canonical.reasoning.mode, ReasoningMode::FixedBudget);
    assert_eq!(fixed.canonical.reasoning.budget_tokens, Some(128));
}

#[test]
fn alternate_surface_values_are_built_from_the_same_unchanged_canonical_source() {
    let request = admit(
        json!({
            "model":"m",
            "messages":[{"role":"user","content":[
                {"type":"text","text":"hello"},
                {"type":"image_url","image_url":{"url":"data:image/png;base64,AAEC"}}
            ]}]
        }),
        ClientSurface::ChatCompletions,
    );
    let original = request.canonical.clone();
    let chat = request
        .canonical
        .to_surface_value(ClientSurface::ChatCompletions);
    let messages = request.canonical.to_surface_value(ClientSurface::Messages);
    assert_eq!(request.canonical, original);
    assert_eq!(
        chat["messages"][0]["content"][1]["image_url"]["url"],
        "data:image/png;base64,AAEC"
    );
    assert_eq!(
        messages["messages"][0]["content"][1]["source"]["data"],
        "AAEC"
    );
}
