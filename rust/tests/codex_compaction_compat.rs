//! Deterministic remote-compaction compatibility contracts (Plan 199).
//!
//! Fixture provenance: OpenAI Codex `4701aa4b4239c70063ab6f2fcb835324f9c109f4`
//! and OpenCodex bridge reference `e4a8539b957b7ae7cd278666f0364eb0f82d4ac3`.
//! These commits are audit markers only; neither is a runtime dependency.
//!
//! The historical `POST /v1/responses/compact` surface is a bounded distinct
//! operation, not an ordinary Responses alias. Only native same-surface
//! forwarding is supported; translated fallbacks are explicitly unsupported
//! and fail before provider submission. v2 `compaction_trigger` items over
//! the normal Responses endpoint require explicit native v2 capability and
//! are otherwise rejected (never treated as user text).

use eggpool::coordinator::{FiniteRequest, InferenceOperation};
use eggpool::request::{AdmissionOptions, admit_compact_request, has_compaction_trigger};
use eggpool::wire::ir::ClientSurface;
use eggpool::wire::{
    CodecReasonCode, CompactionCapabilities, ConfiguredWireProfile, WireCodecId,
    WireProfileDefinition, WireRuntime, WireRuntimeContext, WireRuntimeError, WireSurface,
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

fn v1_context() -> WireRuntimeContext {
    WireRuntimeContext::new(
        ClientSurface::Responses,
        profile(WireSurface::OpenaiResponses),
        "eggpool-model",
        "provider-model",
    )
    .with_compaction(CompactionCapabilities {
        supports_remote_compaction_v1: true,
        compact_path_template: Some("/responses/compact".into()),
        supports_remote_compaction_v2: false,
    })
}

fn compact_body(input: Value) -> Vec<u8> {
    serde_json::to_vec(&input).expect("fixture JSON")
}

#[test]
fn compact_admits_ordinary_user_history_as_a_distinct_operation() {
    let body = compact_body(json!({
        "model": "eggpool-model",
        "instructions": "Summarize for continuation.",
        "input": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "first turn"}]},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "first answer"}]},
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "second turn"}]}
        ]
    }));
    let admitted = admit_compact_request(&body, AdmissionOptions::default())
        .expect("compact history is admitted");
    assert_eq!(admitted.canonical.model, "eggpool-model");
    assert_eq!(admitted.canonical.client_surface, ClientSurface::Responses);
    assert!(!admitted.canonical.stream);
    assert_eq!(admitted.raw_body_bytes, body.len());

    let request = FiniteRequest::new_compact(
        "proxy-compact-1",
        body.clone().into(),
        Default::default(),
        Default::default(),
    )
    .expect("compact coordinator request");
    assert_eq!(request.operation(), InferenceOperation::Compact);
    assert_eq!(request.operation().as_str(), "compact");
    assert_eq!(InferenceOperation::Generate.as_str(), "generate");
    assert!(request.compact_admission.is_some());
}

#[test]
fn compact_history_keeps_function_call_relationships() {
    let body = compact_body(json!({
        "model": "eggpool-model",
        "input": [
            {"type": "function_call", "call_id": "call-fn-1", "name": "lookup", "arguments": "{\"q\":\"egg\"}"},
            {"type": "function_call_output", "call_id": "call-fn-1", "output": "found"}
        ]
    }));
    let admitted =
        admit_compact_request(&body, AdmissionOptions::default()).expect("function history");
    let calls: Vec<_> = admitted
        .canonical
        .messages
        .iter()
        .flat_map(|message| message.content.iter())
        .filter_map(|block| block.call_id.clone())
        .collect();
    assert!(calls.contains(&"call-fn-1".to_owned()));
}

#[test]
fn compact_history_keeps_freeform_call_relationships() {
    let body = compact_body(json!({
        "model": "eggpool-model",
        "input": [
            {"type": "custom_tool_call", "call_id": "call-custom-1", "name": "apply_patch", "input": "diff --git a/a b/a"},
            {"type": "custom_tool_call_output", "call_id": "call-custom-1", "output": "applied"}
        ],
        "tools": [{"type": "custom", "name": "apply_patch", "description": "Apply a patch"}]
    }));
    let admitted =
        admit_compact_request(&body, AdmissionOptions::default()).expect("freeform history");
    assert_eq!(admitted.canonical.tools.len(), 1);
    let outputs: Vec<_> = admitted
        .canonical
        .messages
        .iter()
        .filter_map(|message| message.tool_call_id.clone())
        .collect();
    assert!(outputs.contains(&"call-custom-1".to_owned()));
}

#[test]
fn compact_admits_reasoning_controls_without_persisting_them() {
    let body = compact_body(json!({
        "model": "eggpool-model",
        "input": "summarize this thread",
        "reasoning": {"effort": "high"}
    }));
    let admitted =
        admit_compact_request(&body, AdmissionOptions::default()).expect("reasoning compact");
    assert_eq!(admitted.canonical.reasoning.effort.as_deref(), Some("high"));
    let debug = format!("{:?}", admitted);
    assert!(!debug.contains("summarize this thread"));
}

#[test]
fn compact_native_forwarding_rewrites_only_the_model() {
    let body = compact_body(json!({
        "model": "eggpool-model",
        "instructions": "Summarize.",
        "input": "history to compact",
        "future_extension": {"enabled": true}
    }));
    let admitted =
        admit_compact_request(&body, AdmissionOptions::default()).expect("compact admission");
    let runtime = WireRuntime::embedded().expect("registry");
    let prepared = runtime
        .prepare_compact_request(admitted, &body, &v1_context())
        .expect("native compact preparation");
    let value = prepared.body.value.expect("rewritten compact JSON");
    assert_eq!(value["model"], "provider-model");
    assert_eq!(value["future_extension"]["enabled"], true);
    assert_eq!(value["instructions"], "Summarize.");
}

#[test]
fn compact_native_forwarding_is_byte_exact_without_alias_rewrite() {
    let body = compact_body(json!({
        "model": "provider-model",
        "input": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "keep me"}]}
        ],
        "benign_extension": {"flag": true}
    }));
    let context = WireRuntimeContext::new(
        ClientSurface::Responses,
        profile(WireSurface::OpenaiResponses),
        "provider-model",
        "provider-model",
    )
    .with_compaction(CompactionCapabilities {
        supports_remote_compaction_v1: true,
        compact_path_template: Some("/responses/compact".into()),
        supports_remote_compaction_v2: false,
    });
    let admitted =
        admit_compact_request(&body, AdmissionOptions::default()).expect("compact admission");
    let runtime = WireRuntime::embedded().expect("registry");
    let prepared = runtime
        .prepare_compact_request(admitted, &body, &context)
        .expect("native compact preparation");
    assert!(prepared.body.value.is_none());
    assert_eq!(prepared.body.bytes.as_ref(), body.as_slice());
}

#[test]
fn compact_native_success_returns_replacement_material_unchanged() {
    let runtime = WireRuntime::embedded().expect("registry");
    let upstream = compact_body(json!({
        "id": "resp-compact-1",
        "object": "response",
        "model": "provider-model",
        "output": [
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "summary checkpoint"}]}
        ],
        "usage": {"input_tokens": 120, "output_tokens": 34, "total_tokens": 154}
    }));
    let decoded = runtime
        .decode_compact_response(&upstream, 200, &v1_context())
        .expect("compact decode");
    assert!(matches!(
        decoded.outcome,
        eggpool::wire::CompactResponseOutcome::Success
    ));
    let usage = decoded.usage.expect("compact usage");
    assert_eq!(usage.input_tokens, Some(120));
    assert_eq!(usage.output_tokens, Some(34));
    let client = decoded.client_body.expect("passthrough compact body");
    assert_eq!(client.bytes.as_ref(), upstream.as_slice());
}

#[test]
fn compact_native_structured_failure_is_not_success() {
    let runtime = WireRuntime::embedded().expect("registry");
    let upstream = compact_body(json!({
        "error": {"message": "upstream compact failed", "type": "server_error"}
    }));
    let decoded = runtime
        .decode_compact_response(&upstream, 500, &v1_context())
        .expect("compact decode");
    assert!(matches!(
        decoded.outcome,
        eggpool::wire::CompactResponseOutcome::ProviderError(_)
    ));
    assert!(decoded.client_body.is_none());
}

#[test]
fn compact_without_a_translated_fallback_fails_before_submission() {
    let body = compact_body(json!({"model": "eggpool-model", "input": "history"}));
    let admitted =
        admit_compact_request(&body, AdmissionOptions::default()).expect("compact admission");
    let runtime = WireRuntime::embedded().expect("registry");
    // Chat surface: no translated compaction fallback exists.
    let chat_context = WireRuntimeContext::new(
        ClientSurface::Responses,
        profile(WireSurface::OpenaiChatCompletions),
        "eggpool-model",
        "provider-model",
    );
    let error = runtime
        .prepare_compact_request(admitted.clone(), &body, &chat_context)
        .expect_err("non-native compact target must fail");
    assert!(matches!(
        error,
        WireRuntimeError::RequestAdaptation(error)
            if error.reason == CodecReasonCode::UnsupportedSemanticFeature
    ));
    // Responses surface without v1 capability: explicit rejection, no
    // summarization call is constructed.
    let no_capability = WireRuntimeContext::new(
        ClientSurface::Responses,
        profile(WireSurface::OpenaiResponses),
        "eggpool-model",
        "provider-model",
    );
    let error = runtime
        .prepare_compact_request(admitted, &body, &no_capability)
        .expect_err("missing v1 capability must fail");
    assert!(matches!(
        error,
        WireRuntimeError::RequestAdaptation(error)
            if error.reason == CodecReasonCode::UnsupportedSemanticFeature
    ));
}

#[test]
fn compact_enforces_bounds_and_a_finite_stateless_contract() {
    // Missing history payload.
    let missing = compact_body(json!({"model": "eggpool-model"}));
    assert!(
        admit_compact_request(&missing, AdmissionOptions::default()).is_err(),
        "compact requires input history"
    );
    // Finite-only.
    let streamed = compact_body(json!({
        "model": "eggpool-model",
        "input": "history",
        "stream": true
    }));
    assert!(
        admit_compact_request(&streamed, AdmissionOptions::default()).is_err(),
        "compact rejects stream:true"
    );
    // Stateless policy still applies.
    for payload in [
        json!({"model": "eggpool-model", "input": "history", "store": true}),
        json!({"model": "eggpool-model", "input": "history", "previous_response_id": "resp-1"}),
        json!({"model": "eggpool-model", "input": "history", "background": true}),
    ] {
        let body = compact_body(payload);
        assert!(
            admit_compact_request(&body, AdmissionOptions::default()).is_err(),
            "compact keeps stateful continuation out of scope"
        );
    }
    // Body ceiling applies before provider selection.
    let body = compact_body(json!({"model": "eggpool-model", "input": "history"}));
    let tiny = AdmissionOptions {
        max_body_bytes: 8,
        ..AdmissionOptions::default()
    };
    assert!(admit_compact_request(&body, tiny).is_err());
    // A 2xx non-object compact body is malformed, never success.
    let runtime = WireRuntime::embedded().expect("registry");
    let decoded = runtime
        .decode_compact_response(b"[1,2,3]", 200, &v1_context())
        .expect("compact decode");
    assert!(matches!(
        decoded.outcome,
        eggpool::wire::CompactResponseOutcome::Malformed { .. }
    ));
}

#[test]
fn compact_admission_keeps_prompts_out_of_diagnostics() {
    let marker = "super-secret-compact-marker-9271";
    let body = compact_body(json!({
        "model": "eggpool-model",
        "instructions": marker,
        "input": marker
    }));
    let admitted =
        admit_compact_request(&body, AdmissionOptions::default()).expect("compact admission");
    let debug = format!("{:?}", admitted);
    assert!(!debug.contains(marker));
    let preservation = format!("{:?}", admitted.native_preservation);
    assert!(!preservation.contains(marker));
}

#[test]
fn v2_trigger_is_rejected_until_native_v2_is_qualified() {
    let payload = json!({
        "model": "eggpool-model",
        "input": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hello"}]},
            {"type": "compaction_trigger", "trigger": "auto"}
        ]
    });
    let object = payload.as_object().expect("object");
    assert!(has_compaction_trigger(object));
    // Never treated as user text: canonical messages carry no trigger text.
    let body = compact_body(json!({
        "model": "eggpool-model",
        "input": "plain history"
    }));
    assert!(!has_compaction_trigger(
        serde_json::from_slice::<Value>(&body)
            .expect("json")
            .as_object()
            .expect("object")
    ));

    let raw = serde_json::to_vec(&payload).expect("json");
    let runtime = WireRuntime::embedded().expect("registry");
    let default_context = WireRuntimeContext::new(
        ClientSurface::Responses,
        profile(WireSurface::OpenaiResponses),
        "eggpool-model",
        "provider-model",
    );
    let error = runtime
        .prepare_request(&raw, &default_context)
        .expect_err("default providers do not advertise v2");
    assert!(matches!(
        error,
        WireRuntimeError::RequestAdaptation(error)
            if error.reason == CodecReasonCode::UnsupportedSemanticFeature
    ));
    // Translated targets reject the trigger as well (never converted to text).
    let chat_context = WireRuntimeContext::new(
        ClientSurface::Responses,
        profile(WireSurface::OpenaiChatCompletions),
        "eggpool-model",
        "provider-model",
    );
    let error = runtime
        .prepare_request(&raw, &chat_context)
        .expect_err("translated targets reject the trigger");
    assert!(matches!(
        error,
        WireRuntimeError::RequestAdaptation(error)
            if error.reason == CodecReasonCode::UnsupportedSemanticFeature
    ));
    // An explicitly v2-capable native surface preserves the trigger through
    // the existing source-native path.
    let v2_context = WireRuntimeContext::new(
        ClientSurface::Responses,
        profile(WireSurface::OpenaiResponses),
        "eggpool-model",
        "provider-model",
    )
    .with_compaction(CompactionCapabilities {
        supports_remote_compaction_v1: false,
        compact_path_template: None,
        supports_remote_compaction_v2: true,
    });
    let prepared = runtime
        .prepare_request(&raw, &v2_context)
        .expect("v2-capable native preserves the trigger");
    let preserved: Value = serde_json::from_slice(&prepared.body.bytes).expect("preserved JSON");
    assert_eq!(preserved["model"], "provider-model");
    assert!(
        has_compaction_trigger(preserved.as_object().expect("object")),
        "v2 trigger survives native preservation"
    );
}

#[test]
fn compaction_capabilities_default_to_unsupported() {
    let defaults = CompactionCapabilities::default();
    assert!(!defaults.native_v1_supported());
    assert!(!defaults.supports_remote_compaction_v2);
    let partial = CompactionCapabilities {
        supports_remote_compaction_v1: true,
        compact_path_template: None,
        supports_remote_compaction_v2: false,
    };
    assert!(
        !partial.native_v1_supported(),
        "v1 requires both the flag and a compact path"
    );
}
