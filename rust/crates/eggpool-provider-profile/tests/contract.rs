//! Contract-level tests for the shared provider-profile types.

use eggpool_provider_profile::{
    CatalogEvidence, CredentialProof, CredentialStatus, ProfileValidationError, ProviderAuthMode,
    ProviderProfile, ProviderProfileError, ProviderProfileRegistry, WireSurface, compose_url,
    validate_path, validate_profile,
};

fn registry() -> ProviderProfileRegistry {
    ProviderProfileRegistry::embedded().expect("embedded provider profiles parse and validate")
}

fn opencode() -> ProviderProfile {
    registry()
        .require("opencode-go")
        .expect("OpenCode Go is bundled")
        .clone()
}

#[test]
fn embedded_registry_parses_and_validates_every_profile() {
    let registry = registry();
    assert!(!registry.is_empty(), "bundled profiles are present");
    assert_eq!(registry.len(), registry.ids().count());
    for profile in registry.profiles() {
        validate_profile(&profile.id, profile)
            .unwrap_or_else(|error| panic!("{} validates: {error}", profile.id));
    }
}

#[test]
fn provider_ids_are_stable_across_the_extraction() {
    // Stable bundled provider ID set. A provider-profile refactor must not
    // rename, add, or drop a bundled provider id.
    let mut expected = vec![
        "alibaba",
        "anthropic",
        "custom-compatible",
        "deepinfra",
        "deepseek",
        "fireworks",
        "gemini",
        "gemini-native",
        "groq",
        "lmstudio-local",
        "llamacpp-local",
        "localai-local",
        "minimax",
        "minimax-cn",
        "mistral",
        "ollama-local",
        "openai",
        "opencode-go",
        "openrouter",
        "siliconflow",
        "together",
        "vllm-local",
        "xai",
    ];
    expected.sort_unstable();
    let registry = registry();
    let actual: Vec<&str> = registry.ids().collect();
    assert_eq!(actual, expected);
}

#[test]
fn every_profile_declares_an_absolute_base_url_and_known_auth_mode() {
    for profile in registry().profiles() {
        assert!(
            profile.base_url.starts_with("https://") || profile.base_url.starts_with("http://"),
            "{} carries an absolute http(s) base URL",
            profile.id
        );
        assert!(
            !profile.base_url.ends_with('/'),
            "{} base URL carries no trailing slash",
            profile.id
        );
        assert!(
            !profile.protocols.is_empty(),
            "{} declares at least one protocol",
            profile.id
        );
    }
}

#[test]
fn auth_profiles_carry_structure_and_never_values() {
    let profile = opencode();
    assert_eq!(profile.auth.mode, ProviderAuthMode::Bearer);
    assert_eq!(profile.auth.header, "Authorization");
    assert_eq!(profile.auth.scheme, "Bearer");

    // Serialization exposes mode/header/scheme only; there is no field in
    // which a credential could be smuggled.
    let serialized = serde_json::to_string(&profile.auth).expect("auth profile serializes");
    assert!(serialized.contains("Authorization"));
    assert_eq!(
        serde_json::from_str::<serde_json::Value>(&serialized)
            .expect("auth profile round-trips")
            .as_object()
            .expect("auth profile is an object")
            .keys()
            .cloned()
            .collect::<std::collections::BTreeSet<_>>(),
        std::collections::BTreeSet::from([
            "additional".to_owned(),
            "header".to_owned(),
            "mode".to_owned(),
            "scheme".to_owned(),
        ]),
        "an auth profile exposes structure only"
    );

    // The whole profile serializes the same way: no credential-bearing field
    // exists anywhere in the contract.
    let profile_json = serde_json::to_value(&profile).expect("profile serializes");
    let field_names = collect_field_names(&profile_json);
    for forbidden in [
        "api_key",
        "api_key_env",
        "value_env",
        "credential",
        "secret",
        "password",
        "token",
    ] {
        assert!(
            !field_names.contains(forbidden),
            "serialized profile carries no `{forbidden}` field"
        );
    }
    assert!(field_names.contains("header"));
    assert!(field_names.contains("scheme"));
}

#[test]
fn static_headers_reject_credential_headers_with_literal_values() {
    let mut profile = ProviderProfile {
        id: "fixture".into(),
        base_url: "https://fixture.test/v1".into(),
        protocols: vec!["openai".into()],
        ..ProviderProfile::default()
    };
    profile
        .headers
        .push(eggpool_provider_profile::ProviderStaticHeaderProfile {
            name: "anthropic-version".into(),
            value: Some("2023-06-01".into()),
        });
    validate_profile("fixture", &profile).expect("a non-credential static header is allowed");

    profile
        .headers
        .push(eggpool_provider_profile::ProviderStaticHeaderProfile {
            name: "Authorization".into(),
            value: Some("Bearer literal-secret".into()),
        });
    assert!(matches!(
        validate_profile("fixture", &profile),
        Err(ProfileValidationError::StaticHeader(_))
    ));
}

#[test]
fn embedded_data_has_no_field_that_could_carry_a_secret() {
    let text = eggpool_provider_profile::EMBEDDED_PROVIDER_PROFILES_TOML;
    for forbidden in [
        "api_key",
        "api_key_env",
        "value_env",
        "secret",
        "password",
        "credential",
        "token",
    ] {
        assert!(
            !contains_key(text, forbidden),
            "embedded provider-profile data declares no `{forbidden}` field"
        );
    }
}

#[test]
fn composition_invariants_hold_for_every_declared_surface() {
    for profile in registry().profiles() {
        for (surface, entry) in &profile.wire_surfaces {
            validate_path(&entry.path_template)
                .unwrap_or_else(|error| panic!("{} {surface:?}: {error}", profile.id));
            let url = compose_url(&profile.base_url, &entry.path_template);
            assert!(
                url.starts_with(&profile.base_url),
                "{} {surface:?} composes under the provider base URL",
                profile.id
            );
            assert!(
                !url.contains("//v1/v1") && !url.contains("//v1//"),
                "{} {surface:?} composes without duplicating a path separator",
                profile.id
            );
        }
    }
}

#[test]
fn path_templates_reject_malformed_values() {
    for bad in [
        "",
        "chat/completions",
        "/chat?x=1",
        "/chat#frag",
        "/{unknown}/x",
        " /chat",
    ] {
        assert!(
            validate_path(bad).is_err(),
            "{bad:?} is rejected as a path template"
        );
    }
    assert!(validate_path("/models/{model}:generateContent").is_ok());
}

#[test]
fn verification_policy_never_equates_a_public_catalog_with_a_credential() {
    let profile = opencode();
    let policy = profile.verification_policy();

    // OpenCode Go serves a public catalog, so the credential stays unverified.
    assert_eq!(
        policy.credential_proof,
        CredentialProof::DeferredUntilInference
    );
    assert_eq!(
        policy.status_before_any_request(),
        CredentialStatus::Unverified
    );
    assert_eq!(
        policy.assess(CatalogEvidence::PublicStatic),
        CredentialStatus::Unverified
    );
    assert!(!policy.catalog_implies_credential(CatalogEvidence::PublicStatic));
    assert!(!policy.proves_credential_without_inference());

    // Even an evidence-qualified authenticated-metadata endpoint cannot be
    // verified by a catalog that answered without a credential.
    let qualified =
        eggpool_provider_profile::ProviderVerificationPolicy::authenticated_metadata("/v1/models");
    assert!(qualified.proves_credential_without_inference());
    assert_eq!(
        qualified.assess(CatalogEvidence::PublicStatic),
        CredentialStatus::Unverified
    );
    assert_eq!(
        qualified.assess(CatalogEvidence::Authenticated),
        CredentialStatus::Verified
    );
}

#[test]
fn no_credential_profile_reports_not_applicable() {
    let local = registry()
        .require("ollama-local")
        .expect("ollama-local is bundled")
        .clone();
    assert_eq!(local.auth.mode, ProviderAuthMode::None);
    let policy = local.verification_policy();
    assert_eq!(policy.credential_proof, CredentialProof::NotRequired);
    assert_eq!(
        policy.status_before_any_request(),
        CredentialStatus::NotApplicable
    );
    assert_eq!(
        policy.assess(CatalogEvidence::PublicStatic),
        CredentialStatus::NotApplicable
    );
}

#[test]
fn malformed_documents_fail_closed() {
    /// A rejection predicate for one malformed document.
    type Rejection<'a> = (&'a str, fn(&ProfileValidationError) -> bool);

    let cases: &[Rejection<'_>] = &[
        (
            r#"
            [providers.broken]
            id = "other"
            base_url = "https://fixture.test/v1"
            protocols = ["openai"]
            "#,
            |error| matches!(error, ProfileValidationError::IdKeyMismatch { .. }),
        ),
        (
            r#"
            [providers.broken]
            id = "broken"
            base_url = "https://fixture.test/v1/"
            protocols = ["openai"]
            "#,
            |error| matches!(error, ProfileValidationError::BaseUrl),
        ),
        (
            r#"
            [providers.broken]
            id = "broken"
            base_url = "https://fixture.test/v1"
            protocols = []
            "#,
            |error| matches!(error, ProfileValidationError::Protocols),
        ),
        (
            r#"
            [providers.broken]
            id = "broken"
            base_url = "https://fixture.test/v1"
            protocols = ["openai"]
            [providers.broken.wire_surfaces.openai_responses]
            path_template = "/responses"
            [providers.broken.model_wire."model-a"]
            preferred_surface = "anthropic_messages"
            fixed = false
            "#,
            |error| matches!(error, ProfileValidationError::UnknownSurface { .. }),
        ),
    ];
    for (document, rejects) in cases {
        match ProviderProfileRegistry::from_toml(document) {
            Err(ProviderProfileError::Invalid { source, .. }) => assert!(
                rejects(&source),
                "unexpected rejection {source} for:\n{document}"
            ),
            other => panic!("document must fail closed, got {other:?}"),
        }
    }
}

#[test]
fn unknown_fields_are_rejected_by_the_parser() {
    let document = r#"
        [providers.broken]
        id = "broken"
        base_url = "https://fixture.test/v1"
        protocols = ["openai"]
        unknown_key = true
        "#;
    assert!(
        matches!(
            ProviderProfileRegistry::from_toml(document),
            Err(ProviderProfileError::Parse(_))
        ),
        "an unknown field fails closed"
    );
}

#[test]
fn secret_reference_fields_are_rejected_by_the_parser() {
    for document in [
        r#"
        [providers.broken]
        id = "broken"
        base_url = "https://fixture.test/v1"
        protocols = ["openai"]
        api_key_env = "FIXTURE_API_KEY"
        "#,
        r#"
        [providers.broken]
        id = "broken"
        base_url = "https://fixture.test/v1"
        protocols = ["openai"]
        [[providers.broken.headers]]
        name = "x-api-key"
        value_env = "FIXTURE_API_KEY"
        "#,
    ] {
        assert!(
            ProviderProfileRegistry::from_toml(document).is_err(),
            "a secret-reference field must fail closed:\n{document}"
        );
    }
}

#[test]
fn derived_surfaces_match_the_runtime_normalization_shape() {
    // MiniMax declares the legacy Anthropic path and no explicit surface
    // table; the derived projection must still resolve the Messages URL.
    let minimax = registry()
        .require("minimax")
        .expect("minimax is bundled")
        .clone();
    assert!(minimax.wire_surfaces.is_empty());
    assert_eq!(
        minimax.surface_path_template(WireSurface::AnthropicMessages),
        Some("/v1/messages")
    );
    assert_eq!(
        minimax
            .surface_url(WireSurface::AnthropicMessages)
            .as_deref(),
        Some("https://api.minimax.io/anthropic/v1/messages")
    );

    // OpenAI declares a Responses path alongside the derived chat surface.
    let openai = registry()
        .require("openai")
        .expect("openai is bundled")
        .clone();
    assert_eq!(
        openai.surface_path_template(WireSurface::OpenaiResponses),
        Some("/responses")
    );
    assert_eq!(
        openai.resolved_wire_surfaces().len(),
        2,
        "openai serves chat completions and responses"
    );
}

/// Every object field name appearing anywhere in a serialized profile.
fn collect_field_names(value: &serde_json::Value) -> std::collections::BTreeSet<String> {
    fn walk(value: &serde_json::Value, names: &mut std::collections::BTreeSet<String>) {
        match value {
            serde_json::Value::Object(map) => map.iter().for_each(|(name, value)| {
                names.insert(name.to_ascii_lowercase());
                walk(value, names);
            }),
            serde_json::Value::Array(items) => items.iter().for_each(|item| walk(item, names)),
            _ => {}
        }
    }
    let mut names = std::collections::BTreeSet::new();
    walk(value, &mut names);
    names
}

/// Whether a TOML document declares `key` as a key (not inside prose).
fn contains_key(document: &str, key: &str) -> bool {
    let parsed: toml::Value = document.parse().expect("embedded data is valid TOML");
    fn walk(value: &toml::Value, key: &str) -> bool {
        match value {
            toml::Value::Table(table) => {
                if table.keys().any(|name| name == key) {
                    return true;
                }
                table.values().any(|value| walk(value, key))
            }
            toml::Value::Array(items) => items.iter().any(|item| walk(item, key)),
            _ => false,
        }
    }
    walk(&parsed, key)
}
