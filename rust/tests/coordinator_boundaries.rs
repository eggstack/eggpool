use std::time::{Duration, Instant};

use bytes::Bytes;
use eggpool::{
    Config,
    config::{AccountConfig, ProviderAuthConfig, ProviderConfig},
    coordinator::{
        AttemptBuilder, AttemptInput, FailureObservation, FinalizationIdentity, NextAction,
        RetryPolicy, RetryScope, WireCandidate, WireResolver, WireResolverConfig, classify,
        parse_retry_after,
    },
    providers::ProviderClientPool,
    wire::{
        ConfiguredWireProfile, WireCodecId, WireProfileDefinition, WireRuntime, WireSurface,
        ir::ClientSurface,
    },
};

fn profile(surface: WireSurface, priority: u32) -> ConfiguredWireProfile {
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
        path_template: "/v1/{model}/dispatch".into(),
        stream_path_template: Some("/v1/{model}/stream".into()),
        priority,
    }
}

#[tokio::test]
async fn resolver_learns_accepts_rejects_and_shares_a_leader() {
    let resolver = WireResolver::new(WireResolverConfig {
        max_concurrent_per_provider: 1,
        ..Default::default()
    });
    let now = Instant::now();
    let candidates = vec![
        WireCandidate::new(profile(WireSurface::OpenaiChatCompletions, 10), "a"),
        WireCandidate::new(profile(WireSurface::AnthropicMessages, 20), "b"),
    ];
    let resolved = resolver.resolve("provider", "model", candidates.clone(), now);
    assert_eq!(
        resolved.candidates[0].surface(),
        WireSurface::OpenaiChatCompletions
    );
    resolver.accept(
        "provider",
        "model",
        &resolved.fingerprint,
        WireSurface::AnthropicMessages,
        now,
    );
    let learned = resolver.resolve("provider", "model", candidates.clone(), now);
    assert_eq!(
        learned.candidates[0].surface(),
        WireSurface::AnthropicMessages
    );
    resolver.reject(
        "provider",
        "model",
        &learned.fingerprint,
        WireSurface::AnthropicMessages,
        now,
    );
    let rejected = resolver.resolve("provider", "model", candidates, now);
    assert_eq!(
        rejected.candidates[0].surface(),
        WireSurface::OpenaiChatCompletions
    );

    let leader = resolver
        .begin_negotiation("provider", "model", &resolved.fingerprint, now)
        .await;
    let follower = resolver
        .begin_negotiation("provider", "model", &resolved.fingerprint, now)
        .await;
    assert_eq!(leader.role(), eggpool::coordinator::NegotiationRole::Leader);
    assert_eq!(
        follower.role(),
        eggpool::coordinator::NegotiationRole::Follower
    );
    leader.finish(
        eggpool::coordinator::NegotiationResult::Accepted(WireSurface::OpenaiChatCompletions),
        now,
    );
    assert_eq!(
        follower.wait().await,
        eggpool::coordinator::NegotiationResult::Accepted(WireSurface::OpenaiChatCompletions)
    );
}

#[test]
fn failure_classifier_enforces_handoff_and_retry_after_bounds() {
    let mut observation = FailureObservation::response(1, 1, http::StatusCode::TOO_MANY_REQUESTS);
    observation.retry_after = parse_retry_after("999999", 0, RetryPolicy::default());
    assert_eq!(observation.retry_after, Some(Duration::from_secs(1_800)));
    assert_eq!(
        classify(&observation, RetryPolicy::default()).action,
        NextAction::WaitRateLimit
    );
    observation.response_started = true;
    assert_eq!(
        classify(&observation, RetryPolicy::default()).retry_scope,
        RetryScope::None
    );
}

#[test]
fn attempt_preparation_expands_path_and_never_debugs_credentials() {
    let mut config = Config::default();
    let provider = ProviderConfig {
        id: "provider-a".into(),
        base_url: "https://provider.invalid".into(),
        auth: ProviderAuthConfig {
            mode: "bearer".into(),
            ..Default::default()
        },
        accounts: vec![AccountConfig {
            name: "account-a".into(),
            ..Default::default()
        }],
        ..Default::default()
    };
    config
        .providers
        .insert("provider-a".into(), provider.clone());
    let clients = ProviderClientPool::from_config(&config).expect("client pool");
    let builder = AttemptBuilder::new(clients, WireRuntime::embedded().expect("registry"));
    let identity = FinalizationIdentity {
        proxy_request_id: "request".into(),
        db_request_id: 1,
        attempt_id: 2,
        reservation_id: 3,
        account_id: 4,
        account_name: "account-a".into(),
        provider_id: "provider-a".into(),
        model_id: "model-a".into(),
        client_protocol: "openai".into(),
        upstream_protocol: "openai".into(),
        attempt_number: 1,
    };
    let attempt = builder
        .prepare(AttemptInput {
            identity,
            provider,
            account_api_key: Some("super-secret".into()),
            raw_body: Bytes::from_static(br#"{"model":"model-a","messages":[]}"#),
            client_surface: ClientSurface::ChatCompletions,
            profile: profile(WireSurface::OpenaiChatCompletions, 0),
            stream: true,
            candidate_fingerprint: "candidate".into(),
        })
        .expect("preparation");
    assert_eq!(attempt.path, "/v1/model-a/stream");
    assert_eq!(
        attempt.headers.get("authorization").unwrap(),
        "Bearer super-secret"
    );
    assert!(!format!("{attempt:?}").contains("super-secret"));
}
