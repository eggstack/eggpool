//! OpenCode Go surface, authentication, and model-to-wire fixtures.
//!
//! Evidence source: `https://opencode.ai/docs/go` endpoint table, reviewed
//! 2026-10-07. The table below is a transcription of that page's "Endpoints"
//! section: each row is one exact documented model id and the endpoint the
//! first-party documentation places it on.
//!
//! The catalog endpoint (`https://opencode.ai/zen/go/v1/models`) is public —
//! it answered `200` with an ordinary OpenAI-shaped model list when read
//! without a credential on the same date — so it is recorded as discovery
//! evidence only and never as credential proof.

use eggpool_provider_profile::{ProviderAuthMode, ProviderProfileRegistry, WireSurface};

/// Model id, wire surface, and the documented endpoint URL for one row of the
/// first-party OpenCode Go endpoint table.
type Row = (&'static str, WireSurface, &'static str);

const RESPONSES_URL: &str = "https://opencode.ai/zen/go/v1/responses";
const CHAT_URL: &str = "https://opencode.ai/zen/go/v1/chat/completions";
const MESSAGES_URL: &str = "https://opencode.ai/zen/go/v1/messages";

/// The complete reviewed OpenCode Go endpoint table.
const REVIEWED_TABLE: &[Row] = &[
    // Responses
    ("grok-4.7", WireSurface::OpenaiResponses, RESPONSES_URL),
    ("grok-4.6", WireSurface::OpenaiResponses, RESPONSES_URL),
    ("gpt-6-luna", WireSurface::OpenaiResponses, RESPONSES_URL),
    ("gpt-5.6-luna", WireSurface::OpenaiResponses, RESPONSES_URL),
    (
        "muse-spark-1.3-contributor",
        WireSurface::OpenaiResponses,
        RESPONSES_URL,
    ),
    (
        "muse-spark-1.2-contributor",
        WireSurface::OpenaiResponses,
        RESPONSES_URL,
    ),
    // Chat Completions
    (
        "glm-5.3-flash",
        WireSurface::OpenaiChatCompletions,
        CHAT_URL,
    ),
    ("glm-5.3", WireSurface::OpenaiChatCompletions, CHAT_URL),
    ("glm-5.2", WireSurface::OpenaiChatCompletions, CHAT_URL),
    ("kimi-k3", WireSurface::OpenaiChatCompletions, CHAT_URL),
    (
        "kimi-k2.7-code",
        WireSurface::OpenaiChatCompletions,
        CHAT_URL,
    ),
    ("kimi-k2.6", WireSurface::OpenaiChatCompletions, CHAT_URL),
    ("longcat-2.0", WireSurface::OpenaiChatCompletions, CHAT_URL),
    (
        "longcat-2.5-preview-free",
        WireSurface::OpenaiChatCompletions,
        CHAT_URL,
    ),
    (
        "deepseek-v4.1-flash",
        WireSurface::OpenaiChatCompletions,
        CHAT_URL,
    ),
    (
        "deepseek-v4-pro",
        WireSurface::OpenaiChatCompletions,
        CHAT_URL,
    ),
    (
        "deepseek-v4-flash",
        WireSurface::OpenaiChatCompletions,
        CHAT_URL,
    ),
    (
        "deepseek-v4-flash-vision-exp",
        WireSurface::OpenaiChatCompletions,
        CHAT_URL,
    ),
    (
        "mimo-v2.6-flash",
        WireSurface::OpenaiChatCompletions,
        CHAT_URL,
    ),
    (
        "mimo-v2.6-pro",
        WireSurface::OpenaiChatCompletions,
        CHAT_URL,
    ),
    ("mimo-v2.5", WireSurface::OpenaiChatCompletions, CHAT_URL),
    (
        "mimo-v2.5-pro",
        WireSurface::OpenaiChatCompletions,
        CHAT_URL,
    ),
    ("hy4-preview", WireSurface::OpenaiChatCompletions, CHAT_URL),
    ("hy3", WireSurface::OpenaiChatCompletions, CHAT_URL),
    ("space-bunny", WireSurface::OpenaiChatCompletions, CHAT_URL),
    // Anthropic Messages
    ("minimax-m3", WireSurface::AnthropicMessages, MESSAGES_URL),
    ("minimax-m2.7", WireSurface::AnthropicMessages, MESSAGES_URL),
    ("qwen3.8-max", WireSurface::AnthropicMessages, MESSAGES_URL),
    (
        "qwen3.8-flash",
        WireSurface::AnthropicMessages,
        MESSAGES_URL,
    ),
    ("qwen3.7-plus", WireSurface::AnthropicMessages, MESSAGES_URL),
];

fn registry() -> ProviderProfileRegistry {
    ProviderProfileRegistry::embedded().expect("embedded provider profiles parse and validate")
}

fn opencode() -> eggpool_provider_profile::ProviderProfile {
    registry()
        .require("opencode-go")
        .expect("OpenCode Go is bundled")
        .clone()
}

#[test]
fn base_url_and_per_surface_paths_match_first_party_documentation() {
    let profile = opencode();
    assert_eq!(profile.base_url, "https://opencode.ai/zen/go/v1");
    assert_eq!(
        profile
            .surface_url(WireSurface::OpenaiChatCompletions)
            .as_deref(),
        Some(CHAT_URL)
    );
    assert_eq!(
        profile.surface_url(WireSurface::OpenaiResponses).as_deref(),
        Some(RESPONSES_URL)
    );
    assert_eq!(
        profile
            .surface_url(WireSurface::AnthropicMessages)
            .as_deref(),
        Some(MESSAGES_URL)
    );
}

#[test]
fn per_surface_authentication_matches_first_party_documentation() {
    let profile = opencode();
    // The selected surface supplies the endpoint-specific auth shape.
    for surface in [
        WireSurface::OpenaiChatCompletions,
        WireSurface::OpenaiResponses,
    ] {
        let auth = profile
            .surface_auth(surface)
            .expect("surface declares an auth shape");
        assert_eq!(auth.mode, ProviderAuthMode::Bearer, "{surface:?}");
        assert_eq!(auth.header, "Authorization", "{surface:?}");
    }
    let messages = profile
        .surface_auth(WireSurface::AnthropicMessages)
        .expect("messages surface declares an auth shape");
    assert_eq!(messages.mode, ProviderAuthMode::ApiKey);
    assert_eq!(messages.header, "x-api-key");
}

#[test]
fn model_discovery_composes_under_the_provider_base() {
    let profile = opencode();
    let endpoint = profile.resolved_models_endpoint();
    assert_eq!(endpoint.method, "GET");
    assert!(endpoint.required, "catalog discovery is required");
    assert_eq!(
        eggpool_provider_profile::compose_url(&profile.base_url, &endpoint.path),
        "https://opencode.ai/zen/go/v1/models"
    );
}

#[test]
fn reviewed_table_is_covered_exhaustively_by_non_fixed_hints() {
    let profile = opencode();
    for (model_id, surface, url) in REVIEWED_TABLE {
        let preference = profile
            .model_wire_preference(model_id)
            .unwrap_or_else(|| panic!("{model_id} has a reviewed hint"));
        assert_eq!(preference.preferred_surface, *surface, "{model_id} surface");
        assert!(!preference.fixed, "{model_id} stays advisory");
        assert_eq!(
            profile.surface_url(*surface).as_deref(),
            Some(*url),
            "{model_id} dispatches to its documented endpoint"
        );
    }

    // Exhaustive in the other direction too: the registry declares no hint
    // beyond the reviewed table.
    let reviewed: std::collections::BTreeSet<&str> =
        REVIEWED_TABLE.iter().map(|(model, _, _)| *model).collect();
    let declared: std::collections::BTreeSet<&str> =
        profile.model_wire.keys().map(String::as_str).collect();
    assert_eq!(
        declared, reviewed,
        "hints and the reviewed first-party table stay identical"
    );
    assert_eq!(declared.len(), REVIEWED_TABLE.len());
}

#[test]
fn representative_lookups_return_the_documented_surface() {
    let registry = registry();
    for (model_id, surface) in [
        ("gpt-6-luna", WireSurface::OpenaiResponses),
        ("gpt-5.6-luna", WireSurface::OpenaiResponses),
        ("grok-4.7", WireSurface::OpenaiResponses),
        ("grok-4.6", WireSurface::OpenaiResponses),
        ("muse-spark-1.3-contributor", WireSurface::OpenaiResponses),
        ("glm-5.3-flash", WireSurface::OpenaiChatCompletions),
        ("kimi-k3", WireSurface::OpenaiChatCompletions),
        ("deepseek-v4-pro", WireSurface::OpenaiChatCompletions),
        ("mimo-v2.6-flash", WireSurface::OpenaiChatCompletions),
        ("hy4-preview", WireSurface::OpenaiChatCompletions),
        ("longcat-2.0", WireSurface::OpenaiChatCompletions),
        ("space-bunny", WireSurface::OpenaiChatCompletions),
        ("minimax-m3", WireSurface::AnthropicMessages),
        ("qwen3.8-max", WireSurface::AnthropicMessages),
    ] {
        let preference = registry
            .model_wire_preference("opencode-go", model_id)
            .unwrap_or_else(|| panic!("{model_id} resolves"));
        assert_eq!(preference.preferred_surface, surface, "{model_id}");
    }
}

#[test]
fn unknown_models_and_unknown_providers_stay_unresolved() {
    let registry = registry();
    for unknown in [
        // Model ids that appear in a public catalog or a sibling product but
        // are absent from the reviewed first-party endpoint table.
        "minimax-m2.5",
        "kimi-k3-turbo",
        "gpt-5.5-mini",
        "glm-5",
        "",
    ] {
        assert!(
            registry
                .model_wire_preference("opencode-go", unknown)
                .is_none(),
            "{unknown:?} has no fabricated hint"
        );
    }
    // A provider with no reviewed hints resolves nothing.
    assert!(
        registry
            .model_wire_preference("together", "llama-3.3-70b")
            .is_none()
    );
    assert!(
        registry
            .model_wire_preference("not-a-provider", "gpt-6-luna")
            .is_none()
    );
}

#[test]
fn ambiguous_support_without_a_hint_is_not_defaulted_to_chat_completions() {
    // OpenCode Go serves three surfaces; an unresolved model must not be
    // silently treated as Chat Completions.
    let profile = opencode();
    assert!(
        profile
            .surface_path_template(WireSurface::OpenaiChatCompletions)
            .is_some()
    );
    assert!(
        profile.model_wire_preference("unreviewed-model").is_none(),
        "an ambiguous model stays unresolved rather than defaulting"
    );
}

#[test]
fn opencode_go_is_not_classified_as_credential_proven() {
    use eggpool_provider_profile::{CatalogEvidence, CredentialProof, CredentialStatus};

    let profile = opencode();
    // The profile records that its catalog needs no credential.
    assert_eq!(profile.verify.models_require_authentication, Some(false));
    assert_eq!(
        profile.verify.catalog_authentication(),
        eggpool_provider_profile::CatalogAuthentication::Public
    );

    let policy = profile.verification_policy();
    assert_eq!(
        policy.credential_proof,
        CredentialProof::DeferredUntilInference
    );
    assert_eq!(
        policy.assess(CatalogEvidence::PublicStatic),
        CredentialStatus::Unverified
    );
    assert!(!policy.catalog_implies_credential(CatalogEvidence::PublicStatic));
}
