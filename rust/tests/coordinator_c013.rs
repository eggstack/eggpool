use std::{
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};

use bytes::Bytes;
use eggpool::{
    Config,
    config::{
        AccountConfig, ProviderAuthConfig, ProviderConfig, ProviderStaticHeaderConfig,
        ProviderWireSurfaceConfig,
    },
    coordinator::{
        AttemptBuilder, AttemptInput, EffectLedger, FailureObservation, FailureSource,
        FinalizationIdentity, NegotiationResult, NegotiationRole, RetryPolicy, WireCandidate,
        WireResolver, WireResolverConfig, classify, parse_retry_after,
    },
    providers::ProviderClientPool,
    wire::{
        ConfiguredWireProfile, WireCodecId, WireProfileDefinition, WireRuntime, WireSurface,
        ir::ClientSurface,
    },
};
use http::StatusCode;
use serde_json::Value;
use tokio::io::{AsyncReadExt, AsyncWriteExt};

const C001_OBSERVATIONS: &str =
    include_str!("../../tests/fixtures/coordinator/compatibility-observations.json");

fn profile(surface: WireSurface, priority: u32) -> ConfiguredWireProfile {
    let (request_codec, response_codec, stream_codec) = match surface {
        WireSurface::OpenaiChatCompletions => (
            WireCodecId::OpenaiChat,
            WireCodecId::OpenaiChat,
            WireCodecId::OpenaiChatSse,
        ),
        WireSurface::AnthropicMessages => (
            WireCodecId::AnthropicMessages,
            WireCodecId::AnthropicMessages,
            WireCodecId::AnthropicMessagesSse,
        ),
        _ => unreachable!("C013 fixture only needs Chat and Messages"),
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

fn observation(name: &str) -> FailureObservation {
    let (source, status, signal, alternate_wire) = match name {
        "client_validation" => (FailureSource::ClientValidation, Some(400), None, false),
        "local_preparation" => (FailureSource::LocalPreparation, None, None, false),
        "finalization_database" => (FailureSource::Database, None, None, false),
        "client_cancel_before_handoff" => (FailureSource::Cancellation, None, None, false),
        "connect_error" => (FailureSource::Transport, None, None, false),
        "proxy_error" => (FailureSource::Transport, None, None, false),
        "tls_error" => (FailureSource::Transport, None, None, false),
        "write_error" => (FailureSource::Transport, None, None, false),
        "read_error" => (FailureSource::Transport, None, None, false),
        "pool_timeout" => (FailureSource::Transport, None, None, false),
        "http_400" => (FailureSource::ProviderResponse, Some(400), None, false),
        "http_401_ambiguous" => (FailureSource::ProviderResponse, Some(401), None, false),
        "http_401_explicit_credential" => (
            FailureSource::ProviderResponse,
            Some(401),
            Some("credential_invalid"),
            false,
        ),
        "http_403_no_evidence" => (FailureSource::ProviderResponse, Some(403), None, false),
        "http_403_wire_auth_mismatch" => (
            FailureSource::ProviderResponse,
            Some(403),
            Some("wire_auth_mismatch"),
            true,
        ),
        "http_404_model_absent" => (
            FailureSource::ProviderResponse,
            Some(404),
            Some("model_absent"),
            false,
        ),
        "http_404_surface_mismatch" => (
            FailureSource::ProviderResponse,
            Some(404),
            Some("wire_surface_unsupported"),
            true,
        ),
        "http_408_timeout" => (FailureSource::ProviderResponse, Some(408), None, false),
        "http_409_conflict" => (FailureSource::ProviderResponse, Some(409), None, false),
        "http_429_rate_limit" => (
            FailureSource::ProviderResponse,
            Some(429),
            Some("rate_limited"),
            false,
        ),
        "http_500_server" => (FailureSource::ProviderResponse, Some(500), None, false),
        "http_503_server" => (FailureSource::ProviderResponse, Some(503), None, false),
        "post_handoff_500" => (FailureSource::ProviderResponse, Some(500), None, false),
        other => panic!("unknown C001 failure case {other}"),
    };
    let mut result =
        FailureObservation::response(1, 1, StatusCode::from_u16(status.unwrap_or(500)).unwrap());
    result.source = source;
    result.status = status;
    result.signal = signal.map(str::to_owned);
    result.provider_id = Some("provider-fixture".into());
    result.account_name = Some("account-fixture".into());
    result.model_id = Some("model-fixture".into());
    result.upstream_model_id = Some("model-fixture".into());
    result.client_protocol = "openai".into();
    result.upstream_protocol = "openai".into();
    result.candidate_fingerprint = Some("fixture".into());
    result.transport_phase = Some(name.into());
    result.error_class = Some(name.into());
    result.alternate_wire_available = alternate_wire;
    result.credential_configured = true;
    result.provider_model_presence = eggpool::coordinator::ProviderModelPresence::Known;
    result.retry_after = (name == "http_429_rate_limit").then(|| Duration::from_secs(12));
    result.downstream_started = name == "post_handoff_500";
    result
}

#[test]
fn c013_failure_effects_match_every_committed_c001_policy_case() {
    let observations: Value = serde_json::from_str(C001_OBSERVATIONS).expect("C001 JSON");
    for (name, expected) in observations["failure_cases"]
        .as_object()
        .expect("failure cases")
        .iter()
    {
        let effects = classify(&observation(name), RetryPolicy::default());
        let expected_effects = &expected;
        assert_eq!(effects.retry, expected_effects["retry"], "{name} retry");
        assert_eq!(
            effects.retry_action, expected_effects["retry_action"],
            "{name} action"
        );
        assert_eq!(
            effects.retry_scope_label, expected_effects["retry_scope"],
            "{name} scope"
        );
        assert_eq!(
            effects.client_outcome, expected_effects["client_outcome"],
            "{name} outcome"
        );
        assert_eq!(
            effects.account_effect, expected_effects["account_effect"],
            "{name} account"
        );
        assert_eq!(
            effects.model_effect, expected_effects["model_effect"],
            "{name} model"
        );
        assert_eq!(
            effects.wire_effect, expected_effects["wire_effect"],
            "{name} wire"
        );
        assert_eq!(
            effects.evidence_class, expected_effects["evidence_class"],
            "{name} evidence"
        );
        assert_eq!(
            effects.circuit_penalty, expected_effects["circuit_penalty"],
            "{name} circuit"
        );
        assert_eq!(
            effects.release_probe_only, expected_effects["release_probe_only"],
            "{name} probe"
        );
        assert_eq!(
            effects.persist_backoff, expected_effects["persist_backoff"],
            "{name} backoff"
        );
        assert_eq!(
            effects.backoff_reason.as_deref(),
            expected_effects["backoff_reason"].as_str(),
            "{name} reason"
        );
        let expected_backoff = expected_effects["backoff_until"]
            .as_f64()
            .map(|value| Duration::from_secs_f64(value - 1_700_000_000.0));
        assert_eq!(effects.backoff_until, expected_backoff, "{name} until");
    }
}

#[test]
fn c013_retry_after_parsing_preserves_python_semantics_and_bound() {
    let policy = RetryPolicy::default();
    assert_eq!(
        parse_retry_after("12", 1_700_000_000, policy),
        Some(Duration::from_secs(12))
    );
    assert_eq!(
        parse_retry_after("Sun, 14 Nov 2027 22:13:32 GMT", 1_700_000_000, policy),
        Some(Duration::from_secs(1_800))
    );
    assert_eq!(
        parse_retry_after("not-a-delay", 1_700_000_000, policy),
        None
    );
    assert_eq!(parse_retry_after("-1", 1_700_000_000, policy), None);
    assert_eq!(
        parse_retry_after("999999", 1_700_000_000, policy),
        Some(Duration::from_secs(1_800))
    );
}

#[test]
fn c013_effect_ledger_retires_before_capacity_and_decision_cannot_hide_overflow() {
    let mut ledger = EffectLedger::with_capacity(8);
    for attempt_id in 0..512 {
        assert_eq!(ledger.try_apply_once(attempt_id), Ok(true));
        assert!(ledger.retire(attempt_id));
    }
    assert!(ledger.is_empty());
    let mut engine = eggpool::coordinator::FailureDecisionEngine::new(RetryPolicy::default());
    engine.ledger = EffectLedger::with_capacity(1);
    let first = engine
        .decide(&observation("http_500_server"))
        .expect("first effect");
    assert!(first.1);
    let second = engine.decide(&FailureObservation {
        attempt_id: 2,
        ..observation("http_500_server")
    });
    assert!(
        second.is_err(),
        "capacity must fail before effect ownership"
    );
}

#[tokio::test]
async fn c013_wire_precedence_ttl_eviction_and_concurrency_are_bounded() {
    let base = Instant::now();
    let chat = WireCandidate::new(profile(WireSurface::OpenaiChatCompletions, 10), "chat");
    let messages = WireCandidate::new(profile(WireSurface::AnthropicMessages, 20), "messages");
    let resolver = WireResolver::new(WireResolverConfig {
        cache_capacity: 2,
        learned_ttl: Duration::from_secs(10),
        rejection_ttl: Duration::from_secs(300),
        min_negotiation_interval: Duration::ZERO,
        max_concurrent_per_provider: 1,
        max_provider_state: 2,
        ..Default::default()
    });
    resolver.set_metadata_hint("p", "m", WireSurface::AnthropicMessages);
    let initial = resolver.resolve("p", "m", vec![chat.clone(), messages.clone()], base);
    assert_eq!(
        initial.candidates[0].surface(),
        WireSurface::AnthropicMessages
    );
    resolver.accept(
        "p",
        "m",
        &initial.fingerprint,
        WireSurface::OpenaiChatCompletions,
        base,
    );
    let learned = resolver.resolve(
        "p",
        "m",
        vec![chat.clone(), messages.clone()],
        base + Duration::from_secs(1),
    );
    assert_eq!(
        learned.candidates[0].surface(),
        WireSurface::OpenaiChatCompletions
    );
    let ttl_resolver = WireResolver::new(WireResolverConfig {
        learned_ttl: Duration::from_secs(10),
        min_negotiation_interval: Duration::ZERO,
        ..Default::default()
    });
    let ttl_initial = ttl_resolver.resolve("ttl", "m", vec![chat.clone(), messages.clone()], base);
    ttl_resolver.accept(
        "ttl",
        "m",
        &ttl_initial.fingerprint,
        WireSurface::AnthropicMessages,
        base,
    );
    assert_eq!(
        ttl_resolver
            .resolve(
                "ttl",
                "m",
                vec![chat.clone(), messages.clone()],
                base + Duration::from_secs(1),
            )
            .candidates[0]
            .surface(),
        WireSurface::AnthropicMessages
    );
    assert_eq!(
        ttl_resolver
            .resolve(
                "ttl",
                "m",
                vec![chat.clone(), messages.clone()],
                base + Duration::from_secs(11),
            )
            .candidates[0]
            .surface(),
        WireSurface::OpenaiChatCompletions
    );
    resolver.set_operator_preference("p", "m", WireSurface::AnthropicMessages, true);
    let fixed = resolver.resolve(
        "p",
        "m",
        vec![chat.clone(), messages.clone()],
        base + Duration::from_secs(2),
    );
    assert_eq!(fixed.candidates.len(), 1);
    assert_eq!(
        fixed.candidates[0].surface(),
        WireSurface::AnthropicMessages
    );
    resolver.reject(
        "p",
        "m",
        &fixed.fingerprint,
        WireSurface::AnthropicMessages,
        base + Duration::from_secs(2),
    );
    let still_suppressed = resolver.resolve(
        "p",
        "m",
        vec![chat.clone(), messages.clone()],
        base + Duration::from_secs(3),
    );
    assert_eq!(
        still_suppressed.candidates[0].surface(),
        WireSurface::OpenaiChatCompletions
    );
    let after_cooldown = resolver.resolve(
        "p",
        "m",
        vec![chat.clone(), messages.clone()],
        base + Duration::from_secs(303),
    );
    assert_eq!(
        after_cooldown.candidates[0].surface(),
        WireSurface::AnthropicMessages
    );
    assert_ne!(
        resolver
            .resolve("p", "m", vec![chat.clone()], base)
            .fingerprint,
        resolver
            .resolve("p", "m", vec![chat.clone(), messages.clone()], base)
            .fingerprint
    );
    resolver.resolve("p", "m1", vec![chat.clone()], base);
    resolver.resolve("p", "m2", vec![messages.clone()], base);
    resolver.resolve("p", "m3", vec![chat.clone(), messages.clone()], base);
    assert!(resolver.snapshot().entries <= 2);
    resolver.delay_provider_negotiation("delay", Duration::from_secs(60), base);
    let delayed = resolver
        .begin_negotiation("delay", "m", "delayed", base + Duration::from_secs(1))
        .await;
    assert_eq!(delayed.role(), NegotiationRole::Throttled);
    let after_delay = resolver
        .begin_negotiation("delay", "m", "released", base + Duration::from_secs(61))
        .await;
    assert_eq!(after_delay.role(), NegotiationRole::Leader);
    after_delay.finish(NegotiationResult::Rejected, base + Duration::from_secs(61));

    let leader = resolver.begin_negotiation("p", "m", "flight", base).await;
    let follower = resolver.begin_negotiation("p", "m", "flight", base).await;
    assert_eq!(leader.role(), NegotiationRole::Leader);
    assert_eq!(follower.role(), NegotiationRole::Follower);
    leader.finish(
        NegotiationResult::Accepted(WireSurface::OpenaiChatCompletions),
        base,
    );
    assert_eq!(
        follower.wait().await,
        NegotiationResult::Accepted(WireSurface::OpenaiChatCompletions)
    );
    let cancelled_leader = resolver.begin_negotiation("p", "m", "cancel", base).await;
    let cancelled_follower = resolver.begin_negotiation("p", "m", "cancel", base).await;
    assert_eq!(cancelled_leader.role(), NegotiationRole::Leader);
    drop(cancelled_leader);
    assert_eq!(cancelled_follower.wait().await, NegotiationResult::Rejected);
    assert_eq!(resolver.snapshot().flights, 0);
    let saturated = resolver
        .begin_negotiation("p", "m2", "saturated", base)
        .await;
    let independent = resolver
        .begin_negotiation("q", "m2", "independent", base)
        .await;
    assert_eq!(saturated.role(), NegotiationRole::Leader);
    assert_eq!(independent.role(), NegotiationRole::Leader);
    independent.finish(NegotiationResult::Rejected, base);
    drop(saturated);
    assert_eq!(resolver.snapshot().flights, 0);
    assert_eq!(resolver.snapshot().provider_gates, 0);
}

#[tokio::test]
async fn c013_attempt_submission_observes_native_alias_and_one_request() {
    let listener = tokio::net::TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, 0))
        .await
        .expect("fixture listener");
    let port = listener.local_addr().expect("fixture address").port();
    let observed = Arc::new(Mutex::new(Vec::new()));
    let observed_by_server = Arc::clone(&observed);
    let server = tokio::spawn(async move {
        let (mut socket, _) = listener.accept().await.expect("request");
        let mut request = Vec::new();
        let mut buffer = [0_u8; 1024];
        loop {
            let count = socket.read(&mut buffer).await.expect("request bytes");
            request.extend_from_slice(&buffer[..count]);
            if request.windows(4).any(|window| window == b"\r\n\r\n") {
                break;
            }
        }
        let header_end = request
            .windows(4)
            .position(|window| window == b"\r\n\r\n")
            .unwrap()
            + 4;
        let head = String::from_utf8_lossy(&request[..header_end]);
        let content_length = head
            .lines()
            .find_map(|line| {
                line.strip_prefix("content-length:")
                    .or_else(|| line.strip_prefix("Content-Length:"))
            })
            .and_then(|value| value.trim().parse::<usize>().ok())
            .unwrap_or_default();
        while request.len() < header_end + content_length {
            let count = socket.read(&mut buffer).await.expect("body bytes");
            request.extend_from_slice(&buffer[..count]);
        }
        observed_by_server.lock().unwrap().push(request);
        socket
            .write_all(b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\nconnection: close\r\n\r\nok")
            .await
            .expect("response");
    });

    let mut config = Config::default();
    let mut provider = ProviderConfig {
        id: "provider-a".into(),
        base_url: format!("http://127.0.0.1:{port}"),
        auth: ProviderAuthConfig {
            mode: "bearer".into(),
            ..Default::default()
        },
        accounts: vec![AccountConfig {
            name: "account-a".into(),
            ..Default::default()
        }],
        headers: vec![ProviderStaticHeaderConfig {
            name: "x-provider-static".into(),
            value: Some("provider".into()),
            value_env: None,
        }],
        ..Default::default()
    };
    provider.wire_surfaces.insert(
        "openai_chat_completions".into(),
        ProviderWireSurfaceConfig {
            headers: vec![ProviderStaticHeaderConfig {
                name: "x-surface-static".into(),
                value: Some("surface".into()),
                value_env: None,
            }],
            ..Default::default()
        },
    );
    config
        .providers
        .insert("provider-a".into(), provider.clone());
    let clients = ProviderClientPool::from_config(&config).expect("client pool");
    let builder = AttemptBuilder::new(clients, WireRuntime::embedded().expect("wire runtime"));
    let identity = FinalizationIdentity {
        proxy_request_id: "request".into(),
        db_request_id: 1,
        attempt_id: 2,
        reservation_id: 3,
        account_id: 4,
        account_name: "account-a".into(),
        provider_id: "provider-a".into(),
        model_id: "canonical-alias".into(),
        upstream_model_id: "provider-native".into(),
        client_protocol: "openai".into(),
        upstream_protocol: "openai".into(),
        attempt_number: 1,
    };
    let mut incoming = http::HeaderMap::new();
    incoming.insert("x-allowed", "forwarded".parse().unwrap());
    incoming.insert("authorization", "client-secret".parse().unwrap());
    incoming.insert("connection", "x-denied".parse().unwrap());
    incoming.insert("x-denied", "connection-nominated".parse().unwrap());
    incoming.insert("x-eggpool-route-session", "raw-session".parse().unwrap());
    let attempt = builder
        .prepare(AttemptInput {
            identity,
            provider,
            account_api_key: Some("synthetic-secret-sentinel".into()),
            incoming_headers: incoming,
            request_id: Some("request-id".into()),
            correlation_id: Some("correlation-id".into()),
            raw_body: Bytes::from_static(br#"{"model":"canonical-alias","messages":[]}"#),
            client_surface: ClientSurface::ChatCompletions,
            profile: profile(WireSurface::OpenaiChatCompletions, 0),
            stream: false,
            candidate_fingerprint: "fixture".into(),
        })
        .expect("prepare");
    assert_eq!(attempt.path, "/v1/provider-native/dispatch");
    assert_eq!(
        attempt.headers["authorization"],
        "Bearer synthetic-secret-sentinel"
    );
    assert_eq!(attempt.headers["x-provider-static"], "provider");
    assert_eq!(attempt.headers["x-surface-static"], "surface");
    assert!(!attempt.headers.contains_key("x-denied"));
    assert!(!attempt.headers.contains_key("x-eggpool-route-session"));
    let mut evidence = builder.submit_once(attempt).await.expect("submit once");
    assert_eq!(evidence.status, StatusCode::OK);
    assert_eq!(
        evidence.body.read_to_bytes(64).await.unwrap(),
        Bytes::from_static(b"ok")
    );
    server.await.expect("server");
    let request = observed.lock().unwrap().pop().expect("observed request");
    let request_text = String::from_utf8_lossy(&request);
    assert!(request_text.starts_with("POST /v1/provider-native/dispatch HTTP/1.1"));
    assert!(request_text.contains("x-allowed: forwarded"));
    assert!(request_text.contains("x-request-id: request-id"));
    assert!(request_text.contains("x-correlation-id: correlation-id"));
    let body = String::from_utf8_lossy(
        &request[request
            .windows(4)
            .position(|window| window == b"\r\n\r\n")
            .unwrap()
            + 4..],
    );
    assert!(body.contains("provider-native"));
    assert!(!format!("{evidence:?}").contains("synthetic-secret-sentinel"));
}
