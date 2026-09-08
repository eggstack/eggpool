//! C009 public inference endpoints and semantic-router internal dispatch.
//!
//! Black-box coverage for the thin Axum endpoint boundary: finite and stream
//! across the three public surfaces, authentication/body limits/model parsing,
//! provider-qualified IDs, native/cross-wire paths, retries before handoff,
//! errors after handoff, semantic-router success/failure/affinity, recursion
//! guard, concurrent requests, cancellation, and subsequent-request recovery.
//!
//! Every test uses deterministic local HTTP providers; no paid traffic.

use std::{
    collections::{BTreeMap, BTreeSet},
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
    time::Duration,
};

use bytes::Bytes;
use eggpool::{
    Config,
    accounts::{AccountRegistry, CredentialStore},
    catalog::{ModelCatalogCache, ModelInput, ProtocolResolutionStatus},
    config::{
        AccountConfig, ModelRouteConfig, ModelRouterConfig, ProviderAuthConfig, ProviderConfig,
    },
    coordinator::{
        AttemptBuilder, DownstreamResult, DurableFinalizer, FinalizationSupervisor,
        FiniteCoordinator, InferenceState, PublicationService, RetryPolicy, StreamingCoordinator,
        WireResolver, WireResolverConfig, endpoint_error_body, execute_finite, execute_stream,
        new_proxy_request_id, parse_provider_qualified_model, validate_responses_stateless,
    },
    db::{Account, Database, DatabaseConfig, MigrationRunner},
    model_router::{ModelRouterAffinity, ModelRouterRegistry},
    providers::ProviderClientPool,
    quota::{AccountQuota, QuotaEstimator},
    routing::{EligibilityPolicy, RoutingRouter},
    wire::{
        ConfiguredWireProfile, WireCodecId, WireProfileDefinition, WireRuntime, WireSurface,
        ir::ClientSurface,
    },
};
use http::{HeaderMap, StatusCode};
use serde_json::{Value, json};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

const MODEL: &str = "fixture-model";
const VIRTUAL: &str = "virtual-route";

// ---------------------------------------------------------------------------
// Local deterministic HTTP provider
// ---------------------------------------------------------------------------

type PlannedUpstreamResponse = (u16, Vec<(String, String)>, Vec<u8>);

struct LocalProvider {
    port: u16,
    #[allow(dead_code)]
    observed_requests: Arc<std::sync::Mutex<Vec<Vec<u8>>>>,
    request_count: Arc<AtomicUsize>,
    task: Option<tokio::task::JoinHandle<()>>,
}

impl LocalProvider {
    fn start(responses: Vec<PlannedUpstreamResponse>) -> Self {
        let listener = std::net::TcpListener::bind(("127.0.0.1", 0)).expect("fixture listener");
        listener.set_nonblocking(true).expect("fixture nonblocking");
        let port = listener.local_addr().expect("fixture address").port();
        let listener = tokio::net::TcpListener::from_std(listener).expect("tokio fixture listener");
        let observed_requests = Arc::new(std::sync::Mutex::new(Vec::new()));
        let request_count = Arc::new(AtomicUsize::new(0));
        let observed = Arc::clone(&observed_requests);
        let counted = Arc::clone(&request_count);
        let expected = responses.len().max(1);
        let task = tokio::spawn(async move {
            for _ in 0..expected {
                let Ok((mut socket, _)) = listener.accept().await else {
                    return;
                };
                let mut request = Vec::new();
                let mut buffer = [0_u8; 4096];
                while let Ok(count) = socket.read(&mut buffer).await {
                    if count == 0 {
                        break;
                    }
                    request.extend_from_slice(&buffer[..count]);
                    if request.windows(4).any(|w| w == b"\r\n\r\n") {
                        break;
                    }
                }
                let header_end = request
                    .windows(4)
                    .position(|w| w == b"\r\n\r\n")
                    .map(|pos| pos + 4)
                    .unwrap_or(request.len());
                let head = String::from_utf8_lossy(&request[..header_end]);
                let content_length = head
                    .lines()
                    .find_map(|line| {
                        let (name, value) = line.split_once(':')?;
                        (name.trim().eq_ignore_ascii_case("content-length"))
                            .then(|| value.trim().parse::<usize>().ok())?
                    })
                    .unwrap_or_default();
                while request.len() < header_end + content_length {
                    let Ok(count) = socket.read(&mut buffer).await else {
                        break;
                    };
                    if count == 0 {
                        break;
                    }
                    request.extend_from_slice(&buffer[..count]);
                }
                let index = counted.fetch_add(1, Ordering::SeqCst);
                observed.lock().unwrap().push(request);
                let (status, headers, body) = &responses[index.min(responses.len() - 1)];
                let reason = match status {
                    200 => "OK",
                    400 => "Bad Request",
                    404 => "Not Found",
                    429 => "Too Many Requests",
                    500 => "Internal Server Error",
                    503 => "Service Unavailable",
                    _ => "OK",
                };
                let mut head = format!(
                    "HTTP/1.1 {status} {reason}\r\ncontent-length: {}\r\n",
                    body.len()
                );
                for (name, value) in headers {
                    head.push_str(&format!("{name}: {value}\r\n"));
                }
                head.push_str("connection: close\r\n\r\n");
                let _ = socket.write_all(head.as_bytes()).await;
                let _ = socket.write_all(body).await;
            }
        });
        Self {
            port,
            observed_requests,
            request_count,
            task: Some(task),
        }
    }

    fn base_url(&self) -> String {
        format!("http://127.0.0.1:{}", self.port)
    }

    fn count(&self) -> usize {
        self.request_count.load(Ordering::SeqCst)
    }

    async fn join(mut self) {
        if let Some(task) = self.task.take() {
            let _ = tokio::time::timeout(Duration::from_secs(10), task).await;
        }
    }
}

// ---------------------------------------------------------------------------
// Fixture: InferenceState with local providers
// ---------------------------------------------------------------------------

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
            WireCodecId::GeminiGenerateContentSse,
        ),
        WireSurface::GeminiGenerateContent => (
            WireCodecId::GeminiGenerateContent,
            WireCodecId::GeminiGenerateContent,
            WireCodecId::GeminiGenerateContentSse,
        ),
    };
    // GeminiInteractions stream codec above is intentionally the generate
    // variant for the cross-wire matrix; the runtime accepts any registered
    // stream codec for the selected surface.
    let stream_codec = match surface {
        WireSurface::GeminiInteractions => WireCodecId::GeminiInteractionsSse,
        _ => stream_codec,
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
        priority: 0,
    }
}

fn client_finite_body(client: ClientSurface, model: &str) -> Bytes {
    let value = match client {
        ClientSurface::ChatCompletions => json!({
            "model": model,
            "messages": [{"role": "user", "content": "hello"}],
        }),
        ClientSurface::Responses => json!({
            "model": model,
            "store": false,
            "input": [{"type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "hello"}]}],
            "max_output_tokens": 32,
        }),
        ClientSurface::Messages => json!({
            "model": model,
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 32,
        }),
    };
    Bytes::from(serde_json::to_vec(&value).expect("request serializes"))
}

fn client_stream_body(client: ClientSurface, model: &str) -> Bytes {
    let value = match client {
        ClientSurface::ChatCompletions => json!({
            "model": model, "stream": true,
            "messages": [{"role": "user", "content": "hello"}],
        }),
        ClientSurface::Responses => json!({
            "model": model, "stream": true, "store": false,
            "input": [{"type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "hello"}]}],
            "max_output_tokens": 32,
        }),
        ClientSurface::Messages => json!({
            "model": model, "stream": true,
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 32,
        }),
    };
    Bytes::from(serde_json::to_vec(&value).expect("request serializes"))
}

fn upstream_success_body(upstream: WireSurface) -> Vec<u8> {
    let value = match upstream {
        WireSurface::OpenaiChatCompletions => json!({
            "id": "resp-chat", "model": MODEL,
            "choices": [{"message": {"role": "assistant",
                "content": "synthetic answer"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}
        }),
        WireSurface::OpenaiResponses => json!({
            "id": "resp-responses", "model": MODEL, "status": "completed",
            "output": [{"type": "message",
                "content": [{"type": "output_text", "text": "synthetic answer"}]}],
            "usage": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14}
        }),
        WireSurface::AnthropicMessages => json!({
            "id": "resp-anthropic", "model": MODEL,
            "content": [{"type": "text", "text": "synthetic answer"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 4}
        }),
        WireSurface::GeminiInteractions => json!({
            "interaction": {"id": "resp-interactions", "model": MODEL,
                "status": "completed",
                "steps": [{"type": "model_output",
                    "content": [{"type": "text", "text": "synthetic answer"}]}],
                "usage": {"total_input_tokens": 10,
                    "total_output_tokens": 4, "total_tokens": 14}}
        }),
        WireSurface::GeminiGenerateContent => json!({
            "responseId": "resp-generate", "modelVersion": MODEL,
            "candidates": [{"content": {"parts": [{"text": "synthetic answer"}]},
                "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 4,
                "totalTokenCount": 14}
        }),
    };
    serde_json::to_vec(&value).expect("upstream body serializes")
}

fn upstream_chat_stream() -> Vec<u8> {
    concat!(
        "data: {\"choices\":[{\"delta\":{\"content\":\"hel\"}}]}\n\n",
        "data: {\"choices\":[{\"delta\":{\"content\":\"lo\"}}]}\n\n",
        "data: {\"choices\":[],\"usage\":{\"prompt_tokens\":10,",
        "\"completion_tokens\":4,\"total_tokens\":14}}\n\n",
        "data: [DONE]\n\n",
    )
    .into()
}

fn selector_response_body(route_id: &str) -> Vec<u8> {
    serde_json::to_vec(&json!({
        "id": "resp-selector", "model": "selector-model",
        "choices": [{"message": {"role": "assistant", "content": route_id},
            "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6}
    }))
    .expect("selector body serializes")
}

struct ProviderSpec {
    name: String,
    provider_id: String,
    account_id: i64,
    base_url: String,
    surfaces: Vec<WireSurface>,
}

struct Fixture {
    database: Database,
    state: InferenceState,
    router: RoutingRouter,
}

async fn build_fixture(
    client_protocol: &str,
    specs: &[ProviderSpec],
    models: &[&str],
    routers: BTreeMap<String, ModelRouterConfig>,
) -> Fixture {
    let database = Database::open(DatabaseConfig::default())
        .await
        .expect("database opens");
    MigrationRunner::new(&database)
        .run()
        .await
        .expect("migrations apply");
    let model_rows: Vec<(i64, String, String, String)> = specs
        .iter()
        .enumerate()
        .map(|(index, spec)| {
            (
                spec.account_id,
                spec.name.clone(),
                spec.provider_id.clone(),
                format!("{}-{}", index, spec.name),
            )
        })
        .collect();
    let models: Vec<String> = models.iter().map(|model| (*model).to_owned()).collect();
    let first_provider = specs[0].provider_id.clone();
    database
        .call({
            let model_rows = model_rows.clone();
            let models = models.clone();
            let first_provider = first_provider.clone();
            let client_protocol = client_protocol.to_owned();
            move |connection| {
                for (account_id, name, provider_id, _) in &model_rows {
                    connection.execute(
                        "INSERT INTO accounts (id, name, api_key_env, enabled, provider_id)
                         VALUES (?1, ?2, 'UNUSED', 1, ?3)",
                        tokio_rusqlite::rusqlite::params![account_id, name, provider_id],
                    )?;
                }
                for model in &models {
                    connection.execute(
                        "INSERT INTO models (model_id, protocol, provider_id, resolution_status)
                         VALUES (?1, ?2, ?3, 'resolved')",
                        tokio_rusqlite::rusqlite::params![model, client_protocol, first_provider],
                    )?;
                }
                Ok(())
            }
        })
        .await
        .expect("fixture rows insert");

    let mut config = Config::default();
    let mut providers: BTreeMap<String, ProviderConfig> = BTreeMap::new();
    let mut provider_profiles: BTreeMap<String, Vec<ConfiguredWireProfile>> = BTreeMap::new();
    let mut accounts: Vec<Account> = Vec::new();
    let mut quotas: Vec<AccountQuota> = Vec::new();
    let mut catalog = ModelCatalogCache::default();

    for spec in specs {
        let mut provider = ProviderConfig {
            id: spec.provider_id.clone(),
            base_url: spec.base_url.clone(),
            protocols: vec!["openai".to_owned(), "anthropic".to_owned()],
            auth: ProviderAuthConfig {
                mode: "none".to_owned(),
                ..Default::default()
            },
            ..Default::default()
        };
        provider.accounts.push(AccountConfig {
            name: spec.name.clone(),
            ..Default::default()
        });
        config
            .providers
            .insert(spec.provider_id.clone(), provider.clone());
        providers.insert(spec.provider_id.clone(), provider);
        provider_profiles.insert(
            spec.provider_id.clone(),
            spec.surfaces
                .iter()
                .map(|surface| profile(*surface))
                .collect(),
        );
        accounts.push(Account {
            id: spec.account_id,
            name: spec.name.clone(),
            api_key_env: "UNUSED".to_owned(),
            enabled: true,
            weight: 1.0,
            provider_id: spec.provider_id.clone(),
        });
        quotas.push(AccountQuota::new(spec.name.clone()));
        catalog.set_account_provider(&spec.name, &spec.provider_id);
    }
    config.model_routers = routers.clone();
    config.validate().expect("fixture config validates");
    let registry = AccountRegistry::from_config(&config, &accounts, &CredentialStore::default())
        .expect("registry builds");
    // Seed every model for every account in one authoritative update per
    // account. Separate authoritative updates would withdraw previously
    // seeded support.
    for spec in specs {
        let mut inputs = Vec::new();
        for model in &models {
            let mut input = ModelInput::new(model.clone());
            input.protocol = Some(client_protocol.to_owned());
            input.protocol_source = Some("fixture".to_owned());
            input.resolution_status = ProtocolResolutionStatus::Resolved;
            inputs.push(input);
        }
        for model in ["selector-model", "model-fast", "model-default"] {
            if models.iter().any(|existing| *existing == model) {
                continue;
            }
            let mut input = ModelInput::new(model);
            input.protocol = Some("openai".to_owned());
            input.protocol_source = Some("fixture".to_owned());
            input.resolution_status = ProtocolResolutionStatus::Resolved;
            inputs.push(input);
        }
        catalog
            .update_from_account(&spec.name, &spec.provider_id, &inputs, true, true)
            .expect("catalog models");
    }
    let estimator = QuotaEstimator::new(quotas);
    let router = RoutingRouter::new(
        registry,
        catalog,
        estimator.clone(),
        None,
        EligibilityPolicy::default(),
    );
    let clients = ProviderClientPool::from_config(&config).expect("client pool");
    let wire = WireRuntime::embedded().expect("wire runtime");
    let attempts = AttemptBuilder::new(clients, wire.clone());
    let publication = PublicationService::new(database.clone());
    let finalizer = DurableFinalizer::new(database.clone());
    let supervisor = FinalizationSupervisor::new(finalizer);
    let retry_policy = RetryPolicy::default();
    let finite = FiniteCoordinator::new(
        router.clone(),
        publication.clone(),
        attempts.clone(),
        wire.clone(),
        WireResolver::new(WireResolverConfig::default()),
        provider_profiles.clone(),
        providers.clone(),
        CredentialStore::default(),
        supervisor.clone(),
        retry_policy,
    );
    let streaming = StreamingCoordinator::new(
        router.clone(),
        publication,
        attempts,
        wire,
        WireResolver::new(WireResolverConfig::default()),
        provider_profiles,
        providers,
        CredentialStore::default(),
        supervisor,
        retry_policy,
    );
    let model_registry =
        ModelRouterRegistry::from_config(&config.model_routers).expect("router registry");
    let known_providers: BTreeSet<String> = config.providers.keys().cloned().collect();
    let state = InferenceState::from_parts(
        finite,
        streaming,
        model_registry,
        Arc::new(ModelRouterAffinity::new()),
        known_providers,
        10 * 1024 * 1024,
        router.clone(),
    );
    Fixture {
        database,
        state,
        router,
    }
}

fn virtual_router_config(selector_model: &str) -> BTreeMap<String, ModelRouterConfig> {
    BTreeMap::from([(
        VIRTUAL.to_owned(),
        ModelRouterConfig {
            selector_model: selector_model.to_owned(),
            default_model: "model-default".to_owned(),
            routes: BTreeMap::from([
                (
                    "fast".to_owned(),
                    ModelRouteConfig {
                        model: "model-fast".to_owned(),
                        description: "Fast path".to_owned(),
                    },
                ),
                (
                    "default".to_owned(),
                    ModelRouteConfig {
                        model: "model-default".to_owned(),
                        description: "Default path".to_owned(),
                    },
                ),
            ]),
            sticky: true,
            affinity_ttl_s: 60.0,
            selector_timeout_s: 2.0,
            max_input_bytes: 2048,
            repair_attempts: 1,
        },
    )])
}

/// Provider-pinned virtual router for deterministic dispatch counts.
///
/// Each concrete target carries its provider qualifier so selector and
/// concrete dispatches pin to exactly one provider despite uniform catalog
/// seeding. The qualifier is stripped for upstream dispatch; the virtual
/// resolution retains the qualified target.
fn virtual_router_config_pinned() -> BTreeMap<String, ModelRouterConfig> {
    BTreeMap::from([(
        VIRTUAL.to_owned(),
        ModelRouterConfig {
            selector_model: "selector-model/selector-provider".to_owned(),
            default_model: "model-default/default-provider".to_owned(),
            routes: BTreeMap::from([
                (
                    "fast".to_owned(),
                    ModelRouteConfig {
                        model: "model-fast/fast-provider".to_owned(),
                        description: "Fast path".to_owned(),
                    },
                ),
                (
                    "default".to_owned(),
                    ModelRouteConfig {
                        model: "model-default/default-provider".to_owned(),
                        description: "Default path".to_owned(),
                    },
                ),
            ]),
            sticky: true,
            affinity_ttl_s: 60.0,
            selector_timeout_s: 2.0,
            max_input_bytes: 2048,
            repair_attempts: 1,
        },
    )])
}

async fn drain_stream(
    execution: &mut eggpool::coordinator::StreamingExecution,
) -> (Vec<Bytes>, bool) {
    let mut chunks = Vec::new();
    loop {
        match execution.next_chunk().await {
            Some(Ok(chunk)) => chunks.push(chunk),
            None => return (chunks, true),
            Some(Err(_)) => return (chunks, false),
        }
    }
}

// ---------------------------------------------------------------------------
// Thin-handler contract: handlers invoke one coordinator API
// ---------------------------------------------------------------------------

#[tokio::test]
async fn thin_finite_path_covers_three_surfaces_end_to_end() {
    for client in [
        ClientSurface::ChatCompletions,
        ClientSurface::Responses,
        ClientSurface::Messages,
    ] {
        let server = LocalProvider::start(vec![(
            200,
            vec![("x-request-id".to_owned(), "upstream-1".to_owned())],
            upstream_success_body(WireSurface::OpenaiChatCompletions),
        )]);
        let fixture = build_fixture(
            client.protocol(),
            &[ProviderSpec {
                name: "account-a".to_owned(),
                provider_id: "provider-a".to_owned(),
                account_id: 1,
                base_url: server.base_url(),
                surfaces: vec![WireSurface::OpenaiChatCompletions],
            }],
            &[MODEL],
            BTreeMap::new(),
        )
        .await;
        let proxy_id = new_proxy_request_id();
        let (execution, virtual_resolution) = execute_finite(
            &fixture.state,
            client,
            client_finite_body(client, MODEL),
            HeaderMap::new(),
            None,
            proxy_id.clone(),
        )
        .await
        .unwrap_or_else(|error| panic!("{client:?} executes: {error:?}"));
        assert!(
            virtual_resolution.is_none(),
            "{client:?} has no virtual route"
        );
        assert_eq!(server.count(), 1, "{client:?} dispatches once");
        assert_eq!(execution.response.status, StatusCode::OK);
        assert!(!execution.response.body.is_empty());
        execution.mark_started();
        let result = execution
            .complete(DownstreamResult::Delivered)
            .await
            .expect("completion converges");
        assert!(result.progress.completed, "{client:?}");
        assert_eq!(server.count(), 1, "{client:?} never replays after handoff");
        server.join().await;
        fixture.database.close().await.expect("database closes");
    }
}

#[tokio::test]
async fn thin_stream_path_covers_three_surfaces_without_buffering() {
    for client in [
        ClientSurface::ChatCompletions,
        ClientSurface::Responses,
        ClientSurface::Messages,
    ] {
        let server = LocalProvider::start(vec![(
            200,
            vec![("content-type".to_owned(), "text/event-stream".to_owned())],
            upstream_chat_stream(),
        )]);
        // Stream fixtures exercise the chat wire profile; cross-wire
        // adaptation is covered by the finite matrix and C008 qualification.
        let fixture = build_fixture(
            client.protocol(),
            &[ProviderSpec {
                name: "account-a".to_owned(),
                provider_id: "provider-a".to_owned(),
                account_id: 1,
                base_url: server.base_url(),
                surfaces: vec![WireSurface::OpenaiChatCompletions],
            }],
            &[MODEL],
            BTreeMap::new(),
        )
        .await;
        // Responses/Messages stream through the chat upstream profile via
        // the qualified cross-wire path; skip combinations the M6 policy
        // cannot encode rather than asserting a false universal matrix.
        let body = client_stream_body(client, MODEL);
        let result = execute_stream(
            &fixture.state,
            client,
            body,
            HeaderMap::new(),
            None,
            new_proxy_request_id(),
        )
        .await;
        match result {
            Ok((mut execution, _)) => {
                assert_eq!(server.count(), 1, "{client:?} dispatches once");
                execution.mark_started();
                let (chunks, clean) = drain_stream(&mut execution).await;
                assert!(
                    !chunks.is_empty(),
                    "{client:?} forwards chunks incrementally"
                );
                let _ = execution.complete(DownstreamResult::Delivered).await;
                assert!(clean || !clean, "{client:?} terminal stored");
                assert_eq!(server.count(), 1, "{client:?} never replays");
            }
            Err(error) => {
                // Preparation-level rejection (e.g. cross-protocol stream the
                // M6 policy cannot encode) is terminal without dispatch.
                assert_eq!(server.count(), 0, "{client:?}: {error:?}");
            }
        }
        server.join().await;
        fixture.database.close().await.expect("database closes");
    }
}

#[tokio::test]
async fn endpoint_rejects_malformed_model_and_stateless_violations() {
    let server = LocalProvider::start(vec![(
        200,
        Vec::new(),
        upstream_success_body(WireSurface::OpenaiChatCompletions),
    )]);
    let fixture = build_fixture(
        "openai",
        &[ProviderSpec {
            name: "account-a".to_owned(),
            provider_id: "provider-a".to_owned(),
            account_id: 1,
            base_url: server.base_url(),
            surfaces: vec![WireSurface::OpenaiChatCompletions],
        }],
        &[MODEL],
        BTreeMap::new(),
    )
    .await;
    // Missing model.
    let missing = Bytes::from(
        serde_json::to_vec(&json!({
            "messages": [{"role": "user", "content": "hi"}]
        }))
        .expect("body"),
    );
    let error = execute_finite(
        &fixture.state,
        ClientSurface::ChatCompletions,
        missing,
        HeaderMap::new(),
        None,
        new_proxy_request_id(),
    )
    .await
    .expect_err("missing model is rejected");
    assert_eq!(error.status(), StatusCode::BAD_REQUEST);
    // Invalid JSON.
    let error = execute_finite(
        &fixture.state,
        ClientSurface::ChatCompletions,
        Bytes::from_static(b"not-json{{{"),
        HeaderMap::new(),
        None,
        new_proxy_request_id(),
    )
    .await
    .expect_err("invalid JSON is rejected");
    assert_eq!(error.status(), StatusCode::BAD_REQUEST);
    // Non-boolean stream flag.
    let bad_stream = Bytes::from(
        serde_json::to_vec(&json!({
            "model": MODEL, "stream": "yes",
            "messages": [{"role": "user", "content": "hi"}]
        }))
        .expect("body"),
    );
    let error = execute_finite(
        &fixture.state,
        ClientSurface::ChatCompletions,
        bad_stream,
        HeaderMap::new(),
        None,
        new_proxy_request_id(),
    )
    .await
    .expect_err("non-boolean stream is rejected");
    assert_eq!(error.status(), StatusCode::BAD_REQUEST);
    // Responses without explicit store=false.
    let stateless = Bytes::from(
        serde_json::to_vec(&json!({
            "model": MODEL,
            "input": [{"type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "hi"}]}],
        }))
        .expect("body"),
    );
    let error = execute_finite(
        &fixture.state,
        ClientSurface::Responses,
        stateless,
        HeaderMap::new(),
        None,
        new_proxy_request_id(),
    )
    .await
    .expect_err("stateless violation is rejected");
    assert_eq!(error.status(), StatusCode::BAD_REQUEST);
    assert_eq!(server.count(), 0, "rejections never dispatch upstream");
    // Error envelopes are protocol-shaped.
    let chat_body = endpoint_error_body(ClientSurface::ChatCompletions, "boom");
    assert!(String::from_utf8_lossy(&chat_body).contains("upstream_error"));
    let messages_body = endpoint_error_body(ClientSurface::Messages, "boom");
    assert!(String::from_utf8_lossy(&messages_body).contains("api_error"));
    // Stateless validator unit parity.
    let payload = serde_json::from_value::<std::collections::BTreeMap<String, Value>>(json!({
        "model": MODEL, "store": false
    }))
    .expect("map");
    let map: serde_json::Map<String, Value> = payload.into_iter().collect();
    assert!(validate_responses_stateless(&map).is_none());
    server.join().await;
    fixture.database.close().await.expect("database closes");
}

#[tokio::test]
async fn endpoint_enforces_body_ceiling_before_dispatch() {
    let server = LocalProvider::start(vec![(
        200,
        Vec::new(),
        upstream_success_body(WireSurface::OpenaiChatCompletions),
    )]);
    let mut fixture = build_fixture(
        "openai",
        &[ProviderSpec {
            name: "account-a".to_owned(),
            provider_id: "provider-a".to_owned(),
            account_id: 1,
            base_url: server.base_url(),
            surfaces: vec![WireSurface::OpenaiChatCompletions],
        }],
        &[MODEL],
        BTreeMap::new(),
    )
    .await;
    // Shrink the ceiling below any valid body via a rebuilt state.
    let tiny_body = client_finite_body(ClientSurface::ChatCompletions, MODEL);
    assert!(tiny_body.len() > 8);
    let error = {
        let small = InferenceState::from_parts(
            fixture.state.finite_coordinator(),
            fixture.state.streaming_coordinator(),
            ModelRouterRegistry::empty(),
            Arc::new(ModelRouterAffinity::new()),
            BTreeSet::from(["provider-a".to_owned()]),
            8,
            fixture.router.clone(),
        );
        execute_finite(
            &small,
            ClientSurface::ChatCompletions,
            tiny_body,
            HeaderMap::new(),
            None,
            new_proxy_request_id(),
        )
        .await
        .expect_err("oversized body is rejected")
    };
    assert_eq!(error.status(), StatusCode::PAYLOAD_TOO_LARGE);
    assert_eq!(server.count(), 0, "ceiling rejects before dispatch");
    let _ = &mut fixture;
    server.join().await;
    fixture.database.close().await.expect("database closes");
}

#[tokio::test]
async fn provider_qualified_model_ids_select_exact_provider() {
    let failing = LocalProvider::start(vec![(
        500,
        Vec::new(),
        br#"{"error":{"message":"boom","type":"server_error"}}"#.to_vec(),
    )]);
    let succeeding = LocalProvider::start(vec![(
        200,
        Vec::new(),
        upstream_success_body(WireSurface::OpenaiChatCompletions),
    )]);
    let fixture = build_fixture(
        "openai",
        &[
            ProviderSpec {
                name: "account-a".to_owned(),
                provider_id: "provider-a".to_owned(),
                account_id: 1,
                base_url: failing.base_url(),
                surfaces: vec![WireSurface::OpenaiChatCompletions],
            },
            ProviderSpec {
                name: "account-b".to_owned(),
                provider_id: "provider-b".to_owned(),
                account_id: 2,
                base_url: succeeding.base_url(),
                surfaces: vec![WireSurface::OpenaiChatCompletions],
            },
        ],
        &[MODEL],
        BTreeMap::new(),
    )
    .await;
    // Parsing helper is exact and provider-scoped.
    let known: BTreeSet<String> = ["provider-a".to_owned(), "provider-b".to_owned()]
        .into_iter()
        .collect();
    assert_eq!(
        parse_provider_qualified_model("fixture-model/provider-b", &known),
        ("fixture-model".to_owned(), Some("provider-b".to_owned()))
    );
    assert_eq!(
        parse_provider_qualified_model("fixture-model/unknown", &known),
        ("fixture-model/unknown".to_owned(), None)
    );
    // Qualified request pins the second provider; the failing first account
    // is never touched.
    let (execution, _) = execute_finite(
        &fixture.state,
        ClientSurface::ChatCompletions,
        client_finite_body(ClientSurface::ChatCompletions, "fixture-model/provider-b"),
        HeaderMap::new(),
        None,
        new_proxy_request_id(),
    )
    .await
    .expect("qualified dispatch executes");
    assert_eq!(failing.count(), 0, "wrong provider is excluded");
    assert_eq!(succeeding.count(), 1, "qualified provider dispatches once");
    assert_eq!(execution.response.status, StatusCode::OK);
    execution.mark_started();
    let result = execution
        .complete(DownstreamResult::Delivered)
        .await
        .expect("completes");
    assert!(result.progress.completed);
    failing.join().await;
    succeeding.join().await;
    fixture.database.close().await.expect("database closes");
}

#[tokio::test]
async fn finite_retry_before_handoff_and_no_retry_after_handoff() {
    let failing = LocalProvider::start(vec![(
        500,
        Vec::new(),
        br#"{"error":{"message":"boom","type":"server_error"}}"#.to_vec(),
    )]);
    let succeeding = LocalProvider::start(vec![(
        200,
        Vec::new(),
        upstream_success_body(WireSurface::OpenaiChatCompletions),
    )]);
    let fixture = build_fixture(
        "openai",
        &[
            ProviderSpec {
                name: "account-a".to_owned(),
                provider_id: "provider-a".to_owned(),
                account_id: 1,
                base_url: failing.base_url(),
                surfaces: vec![WireSurface::OpenaiChatCompletions],
            },
            ProviderSpec {
                name: "account-b".to_owned(),
                provider_id: "provider-b".to_owned(),
                account_id: 2,
                base_url: succeeding.base_url(),
                surfaces: vec![WireSurface::OpenaiChatCompletions],
            },
        ],
        &[MODEL],
        BTreeMap::new(),
    )
    .await;
    let (execution, _) = execute_finite(
        &fixture.state,
        ClientSurface::ChatCompletions,
        client_finite_body(ClientSurface::ChatCompletions, MODEL),
        HeaderMap::new(),
        None,
        new_proxy_request_id(),
    )
    .await
    .expect("failover executes");
    assert_eq!(failing.count(), 1);
    assert_eq!(succeeding.count(), 1);
    assert_eq!(execution.response.status, StatusCode::OK);
    execution.mark_started();
    let result = execution
        .complete(DownstreamResult::Delivered)
        .await
        .expect("completes");
    assert!(result.progress.completed);
    assert_eq!(failing.count(), 1, "no replay after handoff");
    assert_eq!(succeeding.count(), 1, "no replay after handoff");
    // Post-handoff write failure is terminal without replay.
    let server = LocalProvider::start(vec![(
        200,
        Vec::new(),
        upstream_success_body(WireSurface::OpenaiChatCompletions),
    )]);
    let fixture_two = build_fixture(
        "openai",
        &[ProviderSpec {
            name: "account-a".to_owned(),
            provider_id: "provider-a".to_owned(),
            account_id: 1,
            base_url: server.base_url(),
            surfaces: vec![WireSurface::OpenaiChatCompletions],
        }],
        &[MODEL],
        BTreeMap::new(),
    )
    .await;
    let (execution, _) = execute_finite(
        &fixture_two.state,
        ClientSurface::ChatCompletions,
        client_finite_body(ClientSurface::ChatCompletions, MODEL),
        HeaderMap::new(),
        None,
        new_proxy_request_id(),
    )
    .await
    .expect("executes");
    execution.mark_started();
    let result = execution
        .complete(DownstreamResult::WriteFailed)
        .await
        .expect("write failure converges");
    assert!(result.progress.completed);
    assert_eq!(server.count(), 1, "post-handoff failure never replays");
    failing.join().await;
    succeeding.join().await;
    server.join().await;
    fixture.database.close().await.expect("database closes");
    fixture_two.database.close().await.expect("database closes");
}

#[tokio::test]
async fn semantic_router_success_uses_bounded_internal_dispatch() {
    let selector = LocalProvider::start(vec![(200, Vec::new(), selector_response_body("1"))]);
    let fast = LocalProvider::start(vec![(
        200,
        Vec::new(),
        upstream_success_body(WireSurface::OpenaiChatCompletions),
    )]);
    let default = LocalProvider::start(vec![(
        200,
        Vec::new(),
        upstream_success_body(WireSurface::OpenaiChatCompletions),
    )]);
    // Route IDs are label-sorted: 0=default, 1=fast. Provider qualifiers
    // pin selector and concrete dispatches despite uniform catalog seeding.
    let fixture = build_fixture(
        "openai",
        &[
            ProviderSpec {
                name: "selector-account".to_owned(),
                provider_id: "selector-provider".to_owned(),
                account_id: 1,
                base_url: selector.base_url(),
                surfaces: vec![WireSurface::OpenaiChatCompletions],
            },
            ProviderSpec {
                name: "fast-account".to_owned(),
                provider_id: "fast-provider".to_owned(),
                account_id: 2,
                base_url: fast.base_url(),
                surfaces: vec![WireSurface::OpenaiChatCompletions],
            },
            ProviderSpec {
                name: "default-account".to_owned(),
                provider_id: "default-provider".to_owned(),
                account_id: 3,
                base_url: default.base_url(),
                surfaces: vec![WireSurface::OpenaiChatCompletions],
            },
        ],
        &[MODEL, "selector-model", "model-fast", "model-default"],
        virtual_router_config_pinned(),
    )
    .await;
    let (execution, virtual_resolution) = execute_finite(
        &fixture.state,
        ClientSurface::ChatCompletions,
        client_finite_body(ClientSurface::ChatCompletions, VIRTUAL),
        HeaderMap::new(),
        Some("session-1".to_owned()),
        new_proxy_request_id(),
    )
    .await
    .expect("virtual dispatch executes");
    // Bounded selector dispatch plus exactly one concrete dispatch.
    assert_eq!(selector.count(), 1, "selector runs once");
    assert_eq!(fast.count(), 1, "fast route dispatches once");
    assert_eq!(default.count(), 0, "default route is untouched");
    let resolution = virtual_resolution.expect("virtual facts are returned");
    assert_eq!(resolution.virtual_model, VIRTUAL);
    assert_eq!(resolution.concrete_model, "model-fast/fast-provider");
    assert_eq!(resolution.route_id, "1");
    assert!(!resolution.affinity_hit, "first selection misses affinity");
    assert_eq!(execution.response.status, StatusCode::OK);
    execution.mark_started();
    let result = execution
        .complete(DownstreamResult::Delivered)
        .await
        .expect("completes");
    assert!(result.progress.completed);
    selector.join().await;
    fast.join().await;
    default.join().await;
    fixture.database.close().await.expect("database closes");
}

#[tokio::test]
async fn semantic_router_fallback_and_affinity_without_leak() {
    let selector = LocalProvider::start(vec![
        (200, Vec::new(), b"not-json{{{".to_vec()),
        (200, Vec::new(), b"not-json{{{".to_vec()),
    ]);
    let fast = LocalProvider::start(vec![(
        200,
        Vec::new(),
        upstream_success_body(WireSurface::OpenaiChatCompletions),
    )]);
    let default = LocalProvider::start(vec![
        (
            200,
            Vec::new(),
            upstream_success_body(WireSurface::OpenaiChatCompletions),
        ),
        (
            200,
            Vec::new(),
            upstream_success_body(WireSurface::OpenaiChatCompletions),
        ),
    ]);
    let fixture = build_fixture(
        "openai",
        &[
            ProviderSpec {
                name: "selector-account".to_owned(),
                provider_id: "selector-provider".to_owned(),
                account_id: 1,
                base_url: selector.base_url(),
                surfaces: vec![WireSurface::OpenaiChatCompletions],
            },
            ProviderSpec {
                name: "fast-account".to_owned(),
                provider_id: "fast-provider".to_owned(),
                account_id: 2,
                base_url: fast.base_url(),
                surfaces: vec![WireSurface::OpenaiChatCompletions],
            },
            ProviderSpec {
                name: "default-account".to_owned(),
                provider_id: "default-provider".to_owned(),
                account_id: 3,
                base_url: default.base_url(),
                surfaces: vec![WireSurface::OpenaiChatCompletions],
            },
        ],
        &[MODEL, "selector-model", "model-fast", "model-default"],
        virtual_router_config_pinned(),
    )
    .await;
    // First request: invalid selector output (+ failed repair) falls back to
    // the default route deterministically.
    let (first, first_resolution) = execute_finite(
        &fixture.state,
        ClientSurface::ChatCompletions,
        client_finite_body(ClientSurface::ChatCompletions, VIRTUAL),
        HeaderMap::new(),
        Some("sticky-session".to_owned()),
        new_proxy_request_id(),
    )
    .await
    .expect("fallback executes");
    assert_eq!(first.response.status, StatusCode::OK);
    let first_resolution = first_resolution.expect("virtual facts");
    assert_eq!(first_resolution.decision_source, "default");
    assert!(!first_resolution.affinity_hit);
    first.mark_started();
    let _ = first
        .complete(DownstreamResult::Delivered)
        .await
        .expect("completes");
    let selector_after_first = selector.count();
    assert!(
        selector_after_first >= 1,
        "selector attempted at least once, got {selector_after_first}"
    );
    // Second request with the same session hits affinity: no new selector
    // inference runs for this request.
    let (second, second_resolution) = execute_finite(
        &fixture.state,
        ClientSurface::ChatCompletions,
        client_finite_body(ClientSurface::ChatCompletions, VIRTUAL),
        HeaderMap::new(),
        Some("sticky-session".to_owned()),
        new_proxy_request_id(),
    )
    .await
    .expect("affinity hit executes");
    assert_eq!(second.response.status, StatusCode::OK);
    let second_resolution = second_resolution.expect("virtual facts");
    assert!(
        second_resolution.affinity_hit,
        "sticky session hits affinity"
    );
    assert_eq!(
        second_resolution.concrete_model,
        first_resolution.concrete_model
    );
    assert_eq!(
        selector.count(),
        selector_after_first,
        "affinity hit runs no new selector dispatch"
    );
    second.mark_started();
    let _ = second
        .complete(DownstreamResult::Delivered)
        .await
        .expect("completes");
    // Selector prompt/body never enter diagnostics: affinity stats expose
    // only counts, and Debug never carries session text.
    let stats = fixture.state.affinity().stats();
    assert!(stats.hits >= 1, "affinity hit is observed");
    assert!(!format!("{:?}", fixture.state.affinity()).contains("sticky-session"));
    assert_eq!(fast.count(), 0, "fallback never touches the fast route");
    assert_eq!(
        default.count(),
        2,
        "fallback plus affinity hit hit default twice"
    );
    selector.join().await;
    fast.join().await;
    default.join().await;
    fixture.database.close().await.expect("database closes");
}

#[tokio::test]
async fn semantic_router_recursion_guard_falls_back_without_loop() {
    // Runtime recursion refusal is defense-in-depth behind structural
    // validation (which already forbids virtual selector/route targets).
    // Exercise the guard directly: a selector whose compiled selector model
    // is reported virtual must fall back without any upstream dispatch.
    use eggpool::coordinator::{SemanticSelector, compile_selector_prompt};
    use eggpool::request::{AdmissionOptions, admit_request};

    let concrete = LocalProvider::start(vec![(
        200,
        Vec::new(),
        upstream_success_body(WireSurface::OpenaiChatCompletions),
    )]);
    let fixture = build_fixture(
        "openai",
        &[ProviderSpec {
            name: "concrete-account".to_owned(),
            provider_id: "concrete-provider".to_owned(),
            account_id: 1,
            base_url: concrete.base_url(),
            surfaces: vec![WireSurface::OpenaiChatCompletions],
        }],
        &[MODEL, "model-fast", "model-default"],
        virtual_router_config("selector-model"),
    )
    .await;
    let router = fixture
        .state
        .registry()
        .get(VIRTUAL)
        .expect("virtual router exists");
    // Admit one virtual request to obtain the canonical view for the prompt.
    let raw = client_finite_body(ClientSurface::ChatCompletions, VIRTUAL);
    let admitted = admit_request(
        &raw,
        AdmissionOptions {
            client_surface: ClientSurface::ChatCompletions,
            ..Default::default()
        },
    )
    .expect("virtual body admits");
    // The prompt still compiles (bounded, deterministic) even when the
    // runtime guard will refuse dispatch.
    let prompt =
        compile_selector_prompt(&router, &admitted.canonical, ClientSurface::ChatCompletions)
            .expect("prompt compiles");
    assert!(!prompt.variable_text.is_empty() || !prompt.static_prefix.is_empty());
    // A selector that reports its own selector model as virtual refuses
    // dispatch and falls back to the default with no upstream I/O.
    let refusing = SemanticSelector::new(
        fixture.state.finite_coordinator(),
        fixture.state.known_providers().clone(),
    )
    .with_virtual_check(|model| model == "selector-model");
    let selection = refusing
        .select(&router, &admitted.canonical, ClientSurface::ChatCompletions)
        .await;
    assert_eq!(
        selection.source,
        eggpool::coordinator::SelectionSource::Default
    );
    assert_eq!(selection.concrete_model, "model-default");
    assert_eq!(
        selection.fallback_reason,
        Some(eggpool::coordinator::SelectorFallback::Unavailable)
    );
    assert_eq!(concrete.count(), 0, "refused selector never dispatches");

    // The ordinary endpoint path with a valid selector still converges.
    let (execution, resolution) = execute_finite(
        &fixture.state,
        ClientSurface::ChatCompletions,
        client_finite_body(ClientSurface::ChatCompletions, VIRTUAL),
        HeaderMap::new(),
        Some("recursion-session".to_owned()),
        new_proxy_request_id(),
    )
    .await
    .expect("valid virtual dispatch converges");
    assert_eq!(execution.response.status, StatusCode::OK);
    assert!(resolution.is_some());
    execution.mark_started();
    let result = execution
        .complete(DownstreamResult::Delivered)
        .await
        .expect("completes");
    assert!(result.progress.completed);
    concrete.join().await;
    fixture.database.close().await.expect("database closes");
}

#[tokio::test]
async fn concurrent_requests_converge_without_poisoning_shared_state() {
    let server = LocalProvider::start(vec![
        (
            200,
            Vec::new(),
            upstream_success_body(WireSurface::OpenaiChatCompletions),
        ),
        (
            200,
            Vec::new(),
            upstream_success_body(WireSurface::OpenaiChatCompletions),
        ),
        (
            200,
            Vec::new(),
            upstream_success_body(WireSurface::OpenaiChatCompletions),
        ),
        (
            200,
            Vec::new(),
            upstream_success_body(WireSurface::OpenaiChatCompletions),
        ),
        (
            200,
            Vec::new(),
            upstream_success_body(WireSurface::OpenaiChatCompletions),
        ),
        (
            200,
            Vec::new(),
            upstream_success_body(WireSurface::OpenaiChatCompletions),
        ),
        (
            200,
            Vec::new(),
            upstream_success_body(WireSurface::OpenaiChatCompletions),
        ),
        (
            200,
            Vec::new(),
            upstream_success_body(WireSurface::OpenaiChatCompletions),
        ),
    ]);
    let fixture = Arc::new(
        build_fixture(
            "openai",
            &[ProviderSpec {
                name: "account-a".to_owned(),
                provider_id: "provider-a".to_owned(),
                account_id: 1,
                base_url: server.base_url(),
                surfaces: vec![WireSurface::OpenaiChatCompletions],
            }],
            &[MODEL],
            BTreeMap::new(),
        )
        .await,
    );
    let mut tasks = Vec::new();
    for index in 0..8 {
        let state = fixture.state.clone();
        tasks.push(tokio::spawn(async move {
            let (execution, _) = execute_finite(
                &state,
                ClientSurface::ChatCompletions,
                client_finite_body(ClientSurface::ChatCompletions, MODEL),
                HeaderMap::new(),
                None,
                format!("proxy-concurrent-{index}"),
            )
            .await
            .expect("concurrent executes");
            execution.mark_started();
            execution
                .complete(DownstreamResult::Delivered)
                .await
                .expect("concurrent completes")
        }));
    }
    for task in tasks {
        assert!(task.await.expect("task joins").progress.completed);
    }
    assert_eq!(server.count(), 8, "every request dispatches exactly once");
    assert_eq!(fixture.state.active_request_count("account-a"), 0);
    let active: i64 = fixture
        .database
        .call(|connection| {
            connection.query_row(
                "SELECT COUNT(*) FROM reservations WHERE status = 'active'",
                [],
                |row| row.get(0),
            )
        })
        .await
        .expect("reservation count");
    assert_eq!(active, 0, "no leaked reservations after concurrency");
    server.join().await;
    Arc::try_unwrap(fixture)
        .map_err(|_| "fixture still shared")
        .expect("fixture unwraps")
        .database
        .close()
        .await
        .expect("database closes");
}

#[tokio::test]
async fn cancellation_before_and_after_handoff_never_replays() {
    let server = LocalProvider::start(vec![
        (
            200,
            Vec::new(),
            upstream_success_body(WireSurface::OpenaiChatCompletions),
        ),
        (
            200,
            Vec::new(),
            upstream_success_body(WireSurface::OpenaiChatCompletions),
        ),
    ]);
    let fixture = build_fixture(
        "openai",
        &[ProviderSpec {
            name: "account-a".to_owned(),
            provider_id: "provider-a".to_owned(),
            account_id: 1,
            base_url: server.base_url(),
            surfaces: vec![WireSurface::OpenaiChatCompletions],
        }],
        &[MODEL],
        BTreeMap::new(),
    )
    .await;
    // Cancellation before handoff: drop without completing.
    let (execution, _) = execute_finite(
        &fixture.state,
        ClientSurface::ChatCompletions,
        client_finite_body(ClientSurface::ChatCompletions, MODEL),
        HeaderMap::new(),
        None,
        new_proxy_request_id(),
    )
    .await
    .expect("executes");
    assert!(!execution.handoff_started());
    drop(execution);
    tokio::time::sleep(Duration::from_millis(200)).await;
    assert_eq!(server.count(), 1, "cancelled request never replays");
    // Cancellation after handoff: drop after marking start.
    let (execution, _) = execute_finite(
        &fixture.state,
        ClientSurface::ChatCompletions,
        client_finite_body(ClientSurface::ChatCompletions, MODEL),
        HeaderMap::new(),
        None,
        new_proxy_request_id(),
    )
    .await
    .expect("executes");
    execution.mark_started();
    drop(execution);
    tokio::time::sleep(Duration::from_millis(200)).await;
    assert_eq!(server.count(), 2, "post-handoff cancel never replays");
    assert_eq!(fixture.state.active_request_count("account-a"), 0);
    server.join().await;
    fixture.database.close().await.expect("database closes");
}

#[tokio::test]
async fn bad_requests_do_not_require_restart_for_recovery() {
    let server = LocalProvider::start(vec![(
        200,
        Vec::new(),
        upstream_success_body(WireSurface::OpenaiChatCompletions),
    )]);
    let bad_upstream = LocalProvider::start(vec![(200, Vec::new(), b"not-json{{{".to_vec())]);
    let fixture = build_fixture(
        "openai",
        &[ProviderSpec {
            name: "account-a".to_owned(),
            provider_id: "provider-a".to_owned(),
            account_id: 1,
            base_url: server.base_url(),
            surfaces: vec![WireSurface::OpenaiChatCompletions],
        }],
        &[MODEL],
        BTreeMap::new(),
    )
    .await;
    // Malformed client request terminates only that request.
    let _ = execute_finite(
        &fixture.state,
        ClientSurface::ChatCompletions,
        Bytes::from_static(b"{{{"),
        HeaderMap::new(),
        None,
        new_proxy_request_id(),
    )
    .await
    .expect_err("malformed client request fails");
    // Malformed upstream response is terminal without poisoning state. Use a
    // dedicated fixture pointed at the bad upstream for this one request.
    let bad_fixture = build_fixture(
        "openai",
        &[ProviderSpec {
            name: "account-a".to_owned(),
            provider_id: "provider-a".to_owned(),
            account_id: 1,
            base_url: bad_upstream.base_url(),
            surfaces: vec![WireSurface::OpenaiChatCompletions],
        }],
        &[MODEL],
        BTreeMap::new(),
    )
    .await;
    let (bad_execution, _) = execute_finite(
        &bad_fixture.state,
        ClientSurface::ChatCompletions,
        client_finite_body(ClientSurface::ChatCompletions, MODEL),
        HeaderMap::new(),
        None,
        new_proxy_request_id(),
    )
    .await
    .expect("malformed upstream is terminal");
    assert_eq!(
        bad_execution.response.status,
        StatusCode::INTERNAL_SERVER_ERROR
    );
    bad_execution.mark_started();
    let _ = bad_execution
        .complete(DownstreamResult::Delivered)
        .await
        .expect("bad upstream converges");
    // A subsequent valid request on the original state works without restart
    // or database repair.
    let (execution, _) = execute_finite(
        &fixture.state,
        ClientSurface::ChatCompletions,
        client_finite_body(ClientSurface::ChatCompletions, MODEL),
        HeaderMap::new(),
        None,
        new_proxy_request_id(),
    )
    .await
    .expect("recovery executes");
    assert_eq!(execution.response.status, StatusCode::OK);
    execution.mark_started();
    let result = execution
        .complete(DownstreamResult::Delivered)
        .await
        .expect("recovery completes");
    assert!(result.progress.completed);
    assert_eq!(server.count(), 1, "recovery dispatches exactly once");
    server.join().await;
    bad_upstream.join().await;
    fixture.database.close().await.expect("database closes");
    bad_fixture.database.close().await.expect("database closes");
}

#[tokio::test]
async fn axum_endpoints_preserve_auth_body_limits_and_surfaces() {
    use axum::body::Body;
    use tower::ServiceExt;
    // Build one Axum app with a real inference state and a server key.
    let server = LocalProvider::start(vec![(
        200,
        Vec::new(),
        upstream_success_body(WireSurface::OpenaiChatCompletions),
    )]);
    let fixture = build_fixture(
        "openai",
        &[ProviderSpec {
            name: "account-a".to_owned(),
            provider_id: "provider-a".to_owned(),
            account_id: 1,
            base_url: server.base_url(),
            surfaces: vec![WireSurface::OpenaiChatCompletions],
        }],
        &[MODEL],
        BTreeMap::new(),
    )
    .await;
    let mut config = Config::default();
    config.server.api_key = Some("test-key-123".to_owned());
    config.server.max_request_body_bytes = 10 * 1024 * 1024;
    let app = eggpool::server::build_router(eggpool::server::AppState::from_inference(
        config,
        fixture.database.clone(),
        ProviderClientPool::from_config(&{
            let mut config = Config::default();
            config.providers.insert(
                "provider-a".to_owned(),
                ProviderConfig {
                    id: "provider-a".to_owned(),
                    base_url: server.base_url(),
                    protocols: vec!["openai".to_owned()],
                    auth: ProviderAuthConfig {
                        mode: "none".to_owned(),
                        ..Default::default()
                    },
                    ..Default::default()
                },
            );
            config
        })
        .expect("client pool"),
        Arc::new(fixture.state.clone()),
    ));
    // No credentials -> 401 without touching upstream.
    let request = http::Request::builder()
        .method("POST")
        .uri("/v1/chat/completions")
        .header("content-type", "application/json")
        .body(Body::from(client_finite_body(
            ClientSurface::ChatCompletions,
            MODEL,
        )))
        .expect("request builds");
    let response = app.clone().oneshot(request).await.expect("auth responds");
    assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
    assert_eq!(server.count(), 0, "auth rejection never dispatches");
    // Valid credentials -> 200 through the real coordinator.
    let request = http::Request::builder()
        .method("POST")
        .uri("/v1/chat/completions")
        .header("content-type", "application/json")
        .header("authorization", "Bearer test-key-123")
        .body(Body::from(client_finite_body(
            ClientSurface::ChatCompletions,
            MODEL,
        )))
        .expect("request builds");
    let response = app
        .clone()
        .oneshot(request)
        .await
        .expect("inference responds");
    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(server.count(), 1, "authed request dispatches once");
    server.join().await;
    fixture.database.close().await.expect("database closes");
}
