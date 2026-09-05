use std::collections::BTreeMap;

use eggpool::{
    Config,
    config::{ModelWirePreference, ProviderWireSurfaceConfig},
    wire::{
        CodecFamily, CodecReasonCode, CompatibilityPath, WireCodecId, WireProfileRegistry,
        WireSurface, builtin_codec, compatibility_path,
    },
};
use serde_json::json;

#[test]
fn embedded_registry_matches_every_w001_profile_and_codec_family() {
    let registry = WireProfileRegistry::embedded().expect("packaged registry");
    assert_eq!(
        registry.profile_ids(),
        vec![
            "openai_chat_completions",
            "openai_responses",
            "anthropic_messages",
            "gemini_interactions",
            "gemini_generate_content",
        ]
    );

    let expected = [
        (
            WireSurface::OpenaiChatCompletions,
            WireCodecId::OpenaiChat,
            WireCodecId::OpenaiChat,
            WireCodecId::OpenaiChatSse,
            CodecFamily::OpenaiChat,
        ),
        (
            WireSurface::OpenaiResponses,
            WireCodecId::OpenaiResponses,
            WireCodecId::OpenaiResponses,
            WireCodecId::OpenaiResponsesSse,
            CodecFamily::OpenaiResponses,
        ),
        (
            WireSurface::AnthropicMessages,
            WireCodecId::AnthropicMessages,
            WireCodecId::AnthropicMessages,
            WireCodecId::AnthropicMessagesSse,
            CodecFamily::AnthropicMessages,
        ),
        (
            WireSurface::GeminiInteractions,
            WireCodecId::GeminiInteractions,
            WireCodecId::GeminiInteractions,
            WireCodecId::GeminiInteractionsSse,
            CodecFamily::GeminiInteractions,
        ),
        (
            WireSurface::GeminiGenerateContent,
            WireCodecId::GeminiGenerateContent,
            WireCodecId::GeminiGenerateContent,
            WireCodecId::GeminiGenerateContentSse,
            CodecFamily::GeminiGenerateContent,
        ),
    ];
    for (surface, request, response, stream, family) in expected {
        let definition = registry.get(surface).expect("profile present");
        assert_eq!(definition.request_codec, request);
        assert_eq!(definition.response_codec, response);
        assert_eq!(definition.stream_codec, stream);
        assert_eq!(definition.codec_family().unwrap(), family);
        assert_eq!(builtin_codec(family).family(), family);
    }
}

#[test]
fn registry_rejects_unknown_missing_extra_and_duplicate_definitions() {
    let unknown = r#"
        [profiles.not_a_surface]
        request_codec = "openai_chat"
        response_codec = "openai_chat"
        stream_codec = "openai_chat_sse"
    "#;
    assert!(WireProfileRegistry::from_toml(unknown).is_err());

    let missing = r#"
        [profiles.openai_chat_completions]
        request_codec = "openai_chat"
        response_codec = "openai_chat"
    "#;
    assert!(WireProfileRegistry::from_toml(missing).is_err());

    let extra = r#"
        [profiles.openai_chat_completions]
        request_codec = "openai_chat"
        response_codec = "openai_chat"
        stream_codec = "openai_chat_sse"
        path = "/chat/completions"
    "#;
    assert!(WireProfileRegistry::from_toml(extra).is_err());

    let duplicate = r#"
        [profiles.openai_chat_completions]
        request_codec = "openai_chat"
        response_codec = "openai_chat"
        stream_codec = "openai_chat_sse"
        [profiles.openai_chat_completions]
        request_codec = "openai_chat"
        response_codec = "openai_chat"
        stream_codec = "openai_chat_sse"
    "#;
    assert!(WireProfileRegistry::from_toml(duplicate).is_err());
}

#[test]
fn registry_rejects_unknown_codec_and_hint_profile() {
    let codec = r#"
        [profiles.openai_chat_completions]
        request_codec = "not_registered"
        response_codec = "openai_chat"
        stream_codec = "openai_chat_sse"
    "#;
    assert!(WireProfileRegistry::from_toml(codec).is_err());

    let hint = r#"
        [profiles.openai_chat_completions]
        request_codec = "openai_chat"
        response_codec = "openai_chat"
        stream_codec = "openai_chat_sse"
        [[hints]]
        provider_id = "example"
        model_id = "model"
        preferred_surface = "openai_responses"
        verified_on = "2026-09-05"
        source = "test"
    "#;
    assert!(WireProfileRegistry::from_toml(hint).is_err());
}

#[test]
fn equivalent_toml_formatting_has_the_same_profile_identity() {
    let first = r#"
        [profiles.openai_chat_completions]
        request_codec = "openai_chat"
        response_codec = "openai_chat"
        stream_codec = "openai_chat_sse"
        [profiles.openai_responses]
        request_codec = "openai_responses"
        response_codec = "openai_responses"
        stream_codec = "openai_responses_sse"
    "#;
    let second = r#"
        [profiles.openai_responses]
        stream_codec="openai_responses_sse"
        response_codec="openai_responses"
        request_codec="openai_responses"
        [profiles.openai_chat_completions]
        stream_codec="openai_chat_sse"
        request_codec="openai_chat"
        response_codec="openai_chat"
    "#;
    assert_eq!(
        WireProfileRegistry::from_toml(first).unwrap(),
        WireProfileRegistry::from_toml(second).unwrap()
    );
}

#[test]
fn configured_provider_profiles_are_exact_and_priority_ordered() {
    let registry = WireProfileRegistry::embedded().unwrap();
    let surfaces = BTreeMap::from([
        (
            "openai_chat_completions".into(),
            ProviderWireSurfaceConfig {
                path_template: "/chat/completions".into(),
                priority: 100,
                ..Default::default()
            },
        ),
        (
            "openai_responses".into(),
            ProviderWireSurfaceConfig {
                path_template: "/responses".into(),
                priority: 90,
                ..Default::default()
            },
        ),
    ]);
    let profiles = registry.configured_profiles(&surfaces).unwrap();
    assert_eq!(profiles[0].definition.surface, WireSurface::OpenaiResponses);
    assert_eq!(profiles[0].path_template, "/responses");
    assert_eq!(
        profiles[1].definition.surface,
        WireSurface::OpenaiChatCompletions
    );

    let mut invalid = surfaces;
    invalid.insert(
        "unknown_surface".into(),
        ProviderWireSurfaceConfig::default(),
    );
    assert!(registry.configured_profiles(&invalid).is_err());
}

#[test]
fn config_rejects_provider_and_model_references_to_unknown_profiles() {
    let file = tempfile::NamedTempFile::new().unwrap();
    std::fs::write(
        file.path(),
        r#"
            [providers.example]
            id = "example"
            base_url = "https://example.test/v1"
            protocols = ["openai"]
            [providers.example.wire_surfaces.unknown_surface]
            path_template = "/unknown"
        "#,
    )
    .unwrap();
    assert!(Config::from_toml(file.path()).is_err());

    let file = tempfile::NamedTempFile::new().unwrap();
    std::fs::write(
        file.path(),
        r#"
            [providers.example]
            id = "example"
            base_url = "https://example.test/v1"
            protocols = ["openai"]
            [providers.example.model_wire.model]
            preferred_surface = "openai_responses"
            fixed = false
        "#,
    )
    .unwrap();
    assert!(Config::from_toml(file.path()).is_err());
}

#[test]
fn compatibility_and_codec_error_codes_are_stable() {
    assert_eq!(
        compatibility_path(
            eggpool::wire::ir::ClientSurface::ChatCompletions,
            WireSurface::OpenaiChatCompletions
        ),
        CompatibilityPath::Native
    );
    assert_eq!(
        compatibility_path(
            eggpool::wire::ir::ClientSurface::ChatCompletions,
            WireSurface::AnthropicMessages
        ),
        CompatibilityPath::CanonicalAdaptation
    );

    let error = eggpool::wire::CodecError::new(CodecReasonCode::LossRejected);
    assert_eq!(
        serde_json::to_value(&error).unwrap(),
        json!({
            "reason": "loss_rejected",
            "field": null,
            "source_surface": null,
            "target_surface": null
        })
    );
}

#[test]
fn wire_codec_ids_have_one_closed_family_mapping() {
    let ids = [
        WireCodecId::OpenaiChat,
        WireCodecId::OpenaiChatSse,
        WireCodecId::OpenaiResponses,
        WireCodecId::OpenaiResponsesSse,
        WireCodecId::AnthropicMessages,
        WireCodecId::AnthropicMessagesSse,
        WireCodecId::GeminiInteractions,
        WireCodecId::GeminiInteractionsSse,
        WireCodecId::GeminiGenerateContent,
        WireCodecId::GeminiGenerateContentSse,
    ];
    let families: Vec<_> = ids.iter().map(|id| id.family()).collect();
    assert_eq!(families[0], CodecFamily::OpenaiChat);
    assert_eq!(families[1], CodecFamily::OpenaiChat);
    assert_eq!(families[2], CodecFamily::OpenaiResponses);
    assert_eq!(families[3], CodecFamily::OpenaiResponses);
    assert_eq!(families[4], CodecFamily::AnthropicMessages);
    assert_eq!(families[5], CodecFamily::AnthropicMessages);
    assert_eq!(families[6], CodecFamily::GeminiInteractions);
    assert_eq!(families[7], CodecFamily::GeminiInteractions);
    assert_eq!(families[8], CodecFamily::GeminiGenerateContent);
    assert_eq!(families[9], CodecFamily::GeminiGenerateContent);
    assert!(ids.iter().all(|id| !id.as_str().is_empty()));
}

#[test]
fn registry_has_no_runtime_preference_surface() {
    let registry = WireProfileRegistry::embedded().unwrap();
    assert!(
        registry
            .hints()
            .iter()
            .all(|hint| !hint.provider_id.is_empty())
    );
    assert_eq!(registry.profile_ids().len(), 5);
    let preference = ModelWirePreference {
        preferred_surface: "openai_responses".into(),
        fixed: false,
    };
    assert!(!preference.fixed);
}
