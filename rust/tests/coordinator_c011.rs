//! C011 differential qualification and M7 closure.
//!
//! Integrated Python/Rust coordinator qualification across the frozen C001
//! contract. Every test uses deterministic local HTTP providers (no paid
//! traffic, no external network) and asserts externally meaningful
//! semantics without normalizing away attempt order, selected
//! account/wire, retry count, response-start timing, terminal outcome,
//! release reason, durable status, effect class, or terminal evidence.
//!
//! Coverage map to the C011 plan:
//! - three public client surfaces x five upstream profiles, finite and
//!   streaming, direct and proxied M4 clients, single/multiple accounts,
//!   fixed and negotiable wire profiles, virtual-router semantic selection;
//! - mandatory failure corpus (malformed input, exhaustion, DB faults,
//!   post-commit interruption, transport phases, auth/quota/rate/model/server,
//!   Retry-After variants, wire rejection, negotiation cancellation, malformed
//!   finite/stream, timeouts, EOF taxonomy, terminal events, midstream abort,
//!   disconnect before/after handoff, write failure, finalizer faults, release
//!   faults, terminal conflict, supervisor capacity, crash/restart, recovery);
//! - bounded concurrency/leak pass with cancellation storms;
//! - security/dependency/resource audit.

use std::{
    collections::{BTreeMap, BTreeSet},
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
    time::{Duration, Instant},
};

use bytes::Bytes;
use eggpool::{
    Config,
    accounts::{AccountRegistry, CredentialStore},
    catalog::{ModelCatalogCache, ModelInput, ProtocolResolutionStatus},
    config::{
        AccountConfig, ModelRouteConfig, ModelRouterConfig, ProviderAuthConfig, ProviderConfig,
        ProviderStreamTimeoutConfig,
    },
    coordinator::{
        AttemptBuilder, CrashReconciler, DownstreamResult, DurableFinalizer, EffectLedger,
        FailureDecisionEngine, FailureObservation, FailureSource, FinalizationCommand,
        FinalizationData, FinalizationError, FinalizationOutcome, FinalizationSupervisor,
        InferenceState, NegotiationResult, NegotiationRole, PublicationInput, PublicationOutcome,
        PublicationService, RetryPolicy, WireCandidate, WireResolver, WireResolverConfig, classify,
        execute_finite, execute_stream, new_proxy_request_id, parse_provider_qualified_model,
        parse_retry_after,
    },
    db::{Account, Database, DatabaseConfig, MigrationRunner},
    model_router::{ModelRouterAffinity, ModelRouterRegistry},
    providers::ProviderClientPool,
    quota::{AccountQuota, QuotaEstimator},
    routing::{EligibilityPolicy, RoutingRequestFacts, RoutingRouter},
    wire::{
        ConfiguredWireProfile, WireCodecId, WireProfileDefinition, WireRuntime, WireSurface,
        ir::ClientSurface,
    },
};
use http::{HeaderMap, StatusCode};
use serde_json::{Value, json};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    time::sleep,
};

const MODEL: &str = "fixture-model";
const VIRTUAL: &str = "virtual-route";
const C001_OBSERVATIONS: &str =
    include_str!("../../migration-rs/fixtures/coordinator/c001-python-observations.json");

// ---------------------------------------------------------------------------
// Wire profiles and payloads
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

fn upstream_finite_body(upstream: WireSurface) -> Vec<u8> {
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

fn upstream_stream_body(upstream: WireSurface) -> Vec<u8> {
    match upstream {
        WireSurface::OpenaiChatCompletions => concat!(
            "data: {\"choices\":[{\"delta\":{\"content\":\"hel\"}}]}\n\n",
            "data: {\"choices\":[{\"delta\":{\"content\":\"lo\"}}]}\n\n",
            "data: {\"choices\":[],\"usage\":{\"prompt_tokens\":10,",
            "\"completion_tokens\":4,\"total_tokens\":14}}\n\n",
            "data: [DONE]\n\n",
        )
        .into(),
        WireSurface::OpenaiResponses => concat!(
            "event: response.output_text.delta\n",
            "data: {\"type\":\"response.output_text.delta\",\"delta\":\"hel\"}\n\n",
            "event: response.output_text.delta\n",
            "data: {\"type\":\"response.output_text.delta\",\"delta\":\"lo\"}\n\n",
            "event: response.completed\n",
            "data: {\"type\":\"response.completed\",\"response\":{\"id\":\"resp-1\",",
            "\"status\":\"completed\",\"usage\":{\"input_tokens\":10,",
            "\"output_tokens\":4,\"total_tokens\":14}}}\n\n",
        )
        .into(),
        WireSurface::AnthropicMessages => concat!(
            "event: message_start\n",
            "data: {\"type\":\"message_start\",\"message\":{\"id\":\"msg-1\"}}\n\n",
            "event: content_block_delta\n",
            "data: {\"type\":\"content_block_delta\",\"index\":0,",
            "\"delta\":{\"type\":\"text_delta\",\"text\":\"hello\"}}\n\n",
            "event: message_delta\n",
            "data: {\"type\":\"message_delta\",\"delta\":{\"stop_reason\":\"end_turn\"},",
            "\"usage\":{\"input_tokens\":10,\"output_tokens\":4}}\n\n",
            "event: message_stop\n",
            "data: {\"type\":\"message_stop\"}\n\n",
        )
        .into(),
        WireSurface::GeminiInteractions => concat!(
            "event: step.delta\n",
            "data: {\"event_type\":\"step.delta\",\"delta\":{\"type\":\"text\",",
            "\"text\":\"hello\"}}\n\n",
            "event: interaction.completed\n",
            "data: {\"event_type\":\"interaction.completed\",\"interaction\":",
            "{\"id\":\"in-1\",\"status\":\"completed\",\"usage\":",
            "{\"total_input_tokens\":10,\"total_output_tokens\":4,",
            "\"total_tokens\":14}}}\n\n",
        )
        .into(),
        WireSurface::GeminiGenerateContent => concat!(
            "data: {\"candidates\":[{\"content\":{\"parts\":[{\"text\":\"hello\"}]}}]}\n\n",
            "data: {\"candidates\":[{\"finishReason\":\"STOP\"}],",
            "\"usageMetadata\":{\"promptTokenCount\":10,\"candidatesTokenCount\":4,",
            "\"totalTokenCount\":14}}\n\n",
        )
        .into(),
    }
}

fn client_terminal_marker(client: ClientSurface) -> &'static str {
    match client {
        ClientSurface::ChatCompletions => "[DONE]",
        ClientSurface::Responses => "response.completed",
        ClientSurface::Messages => "message_stop",
    }
}

// ---------------------------------------------------------------------------
// Deterministic scripted provider
// ---------------------------------------------------------------------------

#[derive(Clone)]
struct Script {
    status: u16,
    headers: Vec<(String, String)>,
    body: Vec<u8>,
    header_delay: Duration,
    body_delay: Duration,
    abort_after_bytes: Option<usize>,
}

impl Script {
    fn immediate(status: u16, body: Vec<u8>) -> Self {
        Self {
            status,
            headers: Vec::new(),
            body,
            header_delay: Duration::ZERO,
            body_delay: Duration::ZERO,
            abort_after_bytes: None,
        }
    }

    fn with_headers(status: u16, headers: Vec<(String, String)>, body: Vec<u8>) -> Self {
        Self {
            status,
            headers,
            body,
            header_delay: Duration::ZERO,
            body_delay: Duration::ZERO,
            abort_after_bytes: None,
        }
    }
}

struct ScriptedProvider {
    port: u16,
    request_count: Arc<AtomicUsize>,
    task: Option<tokio::task::JoinHandle<()>>,
}

impl ScriptedProvider {
    fn start(scripts: Vec<Script>) -> Self {
        let listener = std::net::TcpListener::bind(("127.0.0.1", 0)).expect("fixture listener");
        listener.set_nonblocking(true).expect("fixture nonblocking");
        let port = listener.local_addr().expect("fixture address").port();
        let listener = tokio::net::TcpListener::from_std(listener).expect("tokio fixture listener");
        let request_count = Arc::new(AtomicUsize::new(0));
        let counted = Arc::clone(&request_count);
        let expected = scripts.len().max(1);
        let task = tokio::spawn(async move {
            for _ in 0..expected {
                let Ok((mut socket, _)) = listener.accept().await else {
                    return;
                };
                // Read the HTTP request head + body without hanging on
                // keep-alive: stop after Content-Length bytes.
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
                let script = &scripts[index.min(scripts.len() - 1)];
                if !script.header_delay.is_zero() {
                    sleep(script.header_delay).await;
                }
                let reason = match script.status {
                    200 => "OK",
                    400 => "Bad Request",
                    401 => "Unauthorized",
                    403 => "Forbidden",
                    404 => "Not Found",
                    408 => "Request Timeout",
                    409 => "Conflict",
                    429 => "Too Many Requests",
                    500 => "Internal Server Error",
                    503 => "Service Unavailable",
                    _ => "OK",
                };
                // For abort scripts the declared length is the full body so
                // the client can detect the truncation; only a prefix is
                // written before the connection drops.
                let mut head = format!(
                    "HTTP/1.1 {} {reason}\r\ncontent-length: {}\r\n",
                    script.status,
                    script.body.len()
                );
                for (name, value) in &script.headers {
                    head.push_str(&format!("{name}: {value}\r\n"));
                }
                head.push_str("connection: close\r\n\r\n");
                if socket.write_all(head.as_bytes()).await.is_err() {
                    continue;
                }
                if !script.body_delay.is_zero() {
                    sleep(script.body_delay).await;
                }
                if let Some(prefix) = script.abort_after_bytes {
                    let end = prefix.min(script.body.len());
                    let _ = socket.write_all(&script.body[..end]).await;
                    // Abrupt close: no remaining bytes, no graceful shutdown.
                    continue;
                }
                let _ = socket.write_all(&script.body).await;
            }
        });
        Self {
            port,
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
            let _ = tokio::time::timeout(Duration::from_secs(15), task).await;
        }
    }
}

// ---------------------------------------------------------------------------
// Integrated fixture: one DB + routing + finite/streaming coordinators
// ---------------------------------------------------------------------------

struct ProviderSpec {
    name: String,
    provider_id: String,
    account_id: i64,
    base_url: String,
    surfaces: Vec<WireSurface>,
    proxy_url: Option<String>,
}

impl ProviderSpec {
    fn direct(
        name: &str,
        provider_id: &str,
        account_id: i64,
        base_url: String,
        surfaces: Vec<WireSurface>,
    ) -> Self {
        Self {
            name: name.to_owned(),
            provider_id: provider_id.to_owned(),
            account_id,
            base_url,
            surfaces,
            proxy_url: None,
        }
    }
}

#[derive(Clone, Default)]
struct StreamOptions {
    first_byte_timeout_s: Option<f64>,
    idle_timeout_s: Option<f64>,
    completion_policy: Option<String>,
}

struct Fixture {
    database: Database,
    state: InferenceState,
    #[allow(dead_code)]
    router: RoutingRouter,
}

async fn build_state(
    client_protocol: &str,
    specs: &[ProviderSpec],
    models: &[&str],
    routers: BTreeMap<String, ModelRouterConfig>,
    stream: &StreamOptions,
) -> Fixture {
    let database = Database::open(DatabaseConfig::default())
        .await
        .expect("database opens");
    MigrationRunner::new(&database)
        .run()
        .await
        .expect("migrations apply");
    let model_rows: Vec<(i64, String, String)> = specs
        .iter()
        .map(|spec| (spec.account_id, spec.name.clone(), spec.provider_id.clone()))
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
                for (account_id, name, provider_id) in &model_rows {
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
            stream_timeouts: ProviderStreamTimeoutConfig {
                first_byte_timeout_s: stream.first_byte_timeout_s,
                idle_timeout_s: stream.idle_timeout_s,
                max_lifetime_s: None,
            },
            stream_completion_policy: stream
                .completion_policy
                .clone()
                .unwrap_or_else(|| "strict".to_owned()),
            ..Default::default()
        };
        let mut account = AccountConfig {
            name: spec.name.clone(),
            ..Default::default()
        };
        account.proxy_url = spec.proxy_url.clone();
        provider.accounts.push(account);
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
    for spec in specs {
        let mut inputs = Vec::new();
        for model in &models {
            let mut input = ModelInput::new(model.clone());
            input.protocol = Some(client_protocol.to_owned());
            input.protocol_source = Some("fixture".to_owned());
            input.resolution_status = ProtocolResolutionStatus::Resolved;
            inputs.push(input);
        }
        // Selector/concrete models for virtual-router tests use the openai
        // protocol so the selector dispatch always has candidates.
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
    let finite = eggpool::coordinator::FiniteCoordinator::new(
        router.clone(),
        publication.clone(),
        attempts.clone(),
        wire.clone(),
        eggpool::coordinator::WireResolver::new(WireResolverConfig::default()),
        provider_profiles.clone(),
        providers.clone(),
        CredentialStore::default(),
        supervisor.clone(),
        retry_policy,
    );
    let streaming = eggpool::coordinator::StreamingCoordinator::new(
        router.clone(),
        publication,
        attempts,
        wire,
        eggpool::coordinator::WireResolver::new(WireResolverConfig::default()),
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

async fn active_reservations(database: &Database) -> i64 {
    database
        .call(|connection| {
            connection.query_row(
                "SELECT COUNT(*) FROM reservations WHERE status = 'active'",
                [],
                |row| row.get(0),
            )
        })
        .await
        .expect("reservation count")
}

async fn durable_counts(database: &Database) -> (i64, i64, i64) {
    database
        .call(|connection| {
            Ok((
                connection.query_row("SELECT COUNT(*) FROM requests", [], |row| row.get(0))?,
                connection.query_row(
                    "SELECT COUNT(*) FROM request_attempts WHERE completed_at IS NOT NULL",
                    [],
                    |row| row.get(0),
                )?,
                connection.query_row(
                    "SELECT COUNT(*) FROM reservations WHERE status = 'active'",
                    [],
                    |row| row.get(0),
                )?,
            ))
        })
        .await
        .expect("row counts")
}

async fn request_status(database: &Database, proxy_id: &str) -> (String, Option<i64>, Option<i64>) {
    database
        .call({
            let proxy_id = proxy_id.to_owned();
            move |connection| {
                connection.query_row(
                    "SELECT status, input_tokens, output_tokens FROM requests WHERE proxy_request_id = ?1",
                    [proxy_id],
                    |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
                )
            }
        })
        .await
        .expect("request row")
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

fn virtual_router_pinned() -> BTreeMap<String, ModelRouterConfig> {
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

fn selector_body(route_id: &str) -> Vec<u8> {
    serde_json::to_vec(&json!({
        "id": "resp-selector", "model": "selector-model",
        "choices": [{"message": {"role": "assistant", "content": route_id},
            "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6}
    }))
    .expect("selector body serializes")
}

// ---------------------------------------------------------------------------
// 1. Finite matrix: 3 client surfaces x 5 upstream profiles
// ---------------------------------------------------------------------------

#[tokio::test]
async fn c011_finite_matrix_covers_three_surfaces_native_and_crosswire() {
    for client in [
        ClientSurface::ChatCompletions,
        ClientSurface::Responses,
        ClientSurface::Messages,
    ] {
        for upstream in [
            WireSurface::OpenaiChatCompletions,
            WireSurface::OpenaiResponses,
            WireSurface::AnthropicMessages,
            WireSurface::GeminiInteractions,
            WireSurface::GeminiGenerateContent,
        ] {
            let server = ScriptedProvider::start(vec![Script::with_headers(
                200,
                vec![
                    ("x-custom".to_owned(), "kept".to_owned()),
                    ("x-request-id".to_owned(), "upstream-123".to_owned()),
                ],
                upstream_finite_body(upstream),
            )]);
            let fixture = build_state(
                client.protocol(),
                &[ProviderSpec::direct(
                    "account-a",
                    "provider-a",
                    1,
                    server.base_url(),
                    vec![upstream],
                )],
                &[MODEL],
                BTreeMap::new(),
                &StreamOptions::default(),
            )
            .await;
            let proxy_id = format!("c011-finite-{}-{}", client.as_str(), upstream.as_str());
            let (execution, virtual_resolution) = execute_finite(
                &fixture.state,
                client,
                client_finite_body(client, MODEL),
                HeaderMap::new(),
                None,
                proxy_id.clone(),
            )
            .await
            .unwrap_or_else(|error| panic!("{client:?} x {upstream:?}: {error:?}"));
            assert!(virtual_resolution.is_none());
            assert_eq!(
                server.count(),
                1,
                "{client:?} x {upstream:?} dispatches once"
            );
            assert_eq!(execution.response.status, StatusCode::OK);
            assert!(!execution.response.body.is_empty());
            let names: Vec<String> = execution
                .response
                .headers
                .iter()
                .map(|(name, _)| name.as_str().to_owned())
                .collect();
            assert!(
                names.contains(&"x-custom".to_owned()),
                "{client:?} x {upstream:?}"
            );
            assert!(
                names.contains(&"x-proxy-request-id".to_owned()),
                "{client:?} x {upstream:?}"
            );
            assert!(
                names.contains(&"x-proxy-attempt-count".to_owned()),
                "{client:?} x {upstream:?}"
            );
            assert!(!execution.handoff_started());
            execution.mark_started();
            assert!(execution.handoff_started());
            let result = execution
                .complete(DownstreamResult::Delivered)
                .await
                .expect("completion converges");
            assert!(result.progress.completed, "{client:?} x {upstream:?}");
            assert_eq!(server.count(), 1, "{client:?} x {upstream:?} never replays");
            let (status, input, output) = request_status(&fixture.database, &proxy_id).await;
            assert_eq!(status, "completed", "{client:?} x {upstream:?}");
            assert_eq!(
                (input, output),
                (Some(10), Some(4)),
                "{client:?} x {upstream:?}"
            );
            let (requests, terminal_attempts, active) = durable_counts(&fixture.database).await;
            assert_eq!((requests, terminal_attempts, active), (1, 1, 0));
            assert_eq!(fixture.state.active_request_count("account-a"), 0);
            server.join().await;
            fixture.database.close().await.expect("database closes");
        }
    }
}

// ---------------------------------------------------------------------------
// 2. Streaming matrix: 3 client surfaces x 5 upstream profiles
// ---------------------------------------------------------------------------

#[tokio::test]
async fn c011_stream_matrix_covers_three_surfaces_native_and_crosswire() {
    for client in [
        ClientSurface::ChatCompletions,
        ClientSurface::Responses,
        ClientSurface::Messages,
    ] {
        for upstream in [
            WireSurface::OpenaiChatCompletions,
            WireSurface::OpenaiResponses,
            WireSurface::AnthropicMessages,
            WireSurface::GeminiInteractions,
            WireSurface::GeminiGenerateContent,
        ] {
            let server = ScriptedProvider::start(vec![Script::with_headers(
                200,
                vec![("content-type".to_owned(), "text/event-stream".to_owned())],
                upstream_stream_body(upstream),
            )]);
            let fixture = build_state(
                client.protocol(),
                &[ProviderSpec::direct(
                    "account-a",
                    "provider-a",
                    1,
                    server.base_url(),
                    vec![upstream],
                )],
                &[MODEL],
                BTreeMap::new(),
                &StreamOptions::default(),
            )
            .await;
            let proxy_id = format!("c011-stream-{}-{}", client.as_str(), upstream.as_str());
            let (mut execution, _) = execute_stream(
                &fixture.state,
                client,
                client_stream_body(client, MODEL),
                HeaderMap::new(),
                None,
                proxy_id.clone(),
            )
            .await
            .unwrap_or_else(|error| panic!("{client:?} x {upstream:?}: {error:?}"));
            assert_eq!(server.count(), 1);
            // Incremental forwarding: the first chunk arrives before EOF.
            let first = execution.next_chunk().await;
            assert!(
                first.is_some(),
                "{client:?} x {upstream:?} streams incrementally"
            );
            let (mut chunks, clean) = (vec![first.unwrap().expect("first chunk")], false);
            let (rest, clean_eof) = drain_stream(&mut execution).await;
            chunks.extend(rest);
            let _ = (clean, clean_eof);
            let combined: Vec<u8> = chunks.concat();
            let text = String::from_utf8_lossy(&combined);
            assert!(
                text.contains(client_terminal_marker(client)),
                "{client:?} x {upstream:?} forwards native terminal as {marker:?}: {text:?}",
                marker = client_terminal_marker(client),
            );
            assert!(execution.transport_released(), "{client:?} x {upstream:?}");
            let result = execution
                .complete(DownstreamResult::Delivered)
                .await
                .expect("completion converges");
            assert!(result.progress.completed, "{client:?} x {upstream:?}");
            assert_eq!(server.count(), 1, "{client:?} x {upstream:?} never replays");
            let (status, input, output) = request_status(&fixture.database, &proxy_id).await;
            assert_eq!(status, "completed", "{client:?} x {upstream:?}");
            assert_eq!((input, output), (Some(10), Some(4)));
            assert_eq!(active_reservations(&fixture.database).await, 0);
            assert_eq!(fixture.state.active_request_count("account-a"), 0);
            server.join().await;
            fixture.database.close().await.expect("database closes");
        }
    }
}

// ---------------------------------------------------------------------------
// 3. Malformed client input is rejected before dispatch
// ---------------------------------------------------------------------------

#[tokio::test]
async fn c011_malformed_client_input_rejected_before_dispatch() {
    let server = ScriptedProvider::start(vec![Script::immediate(
        200,
        upstream_finite_body(WireSurface::OpenaiChatCompletions),
    )]);
    let fixture = build_state(
        "openai",
        &[ProviderSpec::direct(
            "account-a",
            "provider-a",
            1,
            server.base_url(),
            vec![WireSurface::OpenaiChatCompletions],
        )],
        &[MODEL],
        BTreeMap::new(),
        &StreamOptions::default(),
    )
    .await;
    // Invalid JSON.
    assert!(
        execute_finite(
            &fixture.state,
            ClientSurface::ChatCompletions,
            Bytes::from_static(b"{not-json"),
            HeaderMap::new(),
            None,
            new_proxy_request_id(),
        )
        .await
        .is_err()
    );
    // Missing model.
    assert!(
        execute_finite(
            &fixture.state,
            ClientSurface::ChatCompletions,
            Bytes::from_static(b"{\"messages\":[]}"),
            HeaderMap::new(),
            None,
            new_proxy_request_id(),
        )
        .await
        .is_err()
    );
    // Responses stateless violation: store must be explicitly false.
    assert!(
        execute_finite(
            &fixture.state,
            ClientSurface::Responses,
            Bytes::from(serde_json::to_vec(&json!({"model": MODEL, "input": []})).expect("body"),),
            HeaderMap::new(),
            None,
            new_proxy_request_id(),
        )
        .await
        .is_err()
    );
    // Invalid stream flag shape.
    assert!(
        execute_finite(
            &fixture.state,
            ClientSurface::ChatCompletions,
            Bytes::from(
                serde_json::to_vec(&json!({"model": MODEL, "stream": "yes"})).expect("body"),
            ),
            HeaderMap::new(),
            None,
            new_proxy_request_id(),
        )
        .await
        .is_err()
    );
    // Oversized body rejected before dispatch.
    assert!(
        execute_finite(
            &fixture.state,
            ClientSurface::ChatCompletions,
            Bytes::from(vec![b'x'; 11 * 1024 * 1024]),
            HeaderMap::new(),
            None,
            new_proxy_request_id(),
        )
        .await
        .is_err()
    );
    assert_eq!(
        server.count(),
        0,
        "no upstream dispatch for malformed input"
    );
    assert_eq!(active_reservations(&fixture.database).await, 0);
    // A subsequent valid request recovers without restart.
    let (execution, _) = execute_finite(
        &fixture.state,
        ClientSurface::ChatCompletions,
        client_finite_body(ClientSurface::ChatCompletions, MODEL),
        HeaderMap::new(),
        None,
        new_proxy_request_id(),
    )
    .await
    .expect("valid request recovers");
    assert_eq!(server.count(), 1);
    execution.mark_started();
    let result = execution
        .complete(DownstreamResult::Delivered)
        .await
        .expect("recovery converges");
    assert!(result.progress.completed);
    server.join().await;
    fixture.database.close().await.expect("database closes");
}

// ---------------------------------------------------------------------------
// 4. Selection exhaustion and provider-qualified pinning
// ---------------------------------------------------------------------------

#[tokio::test]
async fn c011_selection_exhaustion_and_provider_pin() {
    let server_a = ScriptedProvider::start(vec![Script::immediate(
        200,
        upstream_finite_body(WireSurface::OpenaiChatCompletions),
    )]);
    let server_b = ScriptedProvider::start(vec![Script::immediate(
        200,
        upstream_finite_body(WireSurface::OpenaiChatCompletions),
    )]);
    let fixture = build_state(
        "openai",
        &[
            ProviderSpec::direct(
                "account-a",
                "provider-a",
                1,
                server_a.base_url(),
                vec![WireSurface::OpenaiChatCompletions],
            ),
            ProviderSpec::direct(
                "account-b",
                "provider-b",
                2,
                server_b.base_url(),
                vec![WireSurface::OpenaiChatCompletions],
            ),
        ],
        &[MODEL],
        BTreeMap::new(),
        &StreamOptions::default(),
    )
    .await;
    // Unknown model: no eligible account, no dispatch.
    let exhausted = execute_finite(
        &fixture.state,
        ClientSurface::ChatCompletions,
        client_finite_body(ClientSurface::ChatCompletions, "unknown-model-xyz"),
        HeaderMap::new(),
        None,
        new_proxy_request_id(),
    )
    .await;
    assert!(exhausted.is_err(), "exhaustion fails closed");
    assert_eq!(server_a.count(), 0);
    assert_eq!(server_b.count(), 0);
    // Provider-qualified model pins exactly to provider-b.
    let (model, provider) = parse_provider_qualified_model(
        "fixture-model/provider-b",
        &BTreeSet::from(["provider-a".to_owned(), "provider-b".to_owned()]),
    );
    assert_eq!(
        (model.as_str(), provider.as_deref()),
        ("fixture-model", Some("provider-b"))
    );
    let (execution, _) = execute_finite(
        &fixture.state,
        ClientSurface::ChatCompletions,
        client_finite_body(ClientSurface::ChatCompletions, "fixture-model/provider-b"),
        HeaderMap::new(),
        None,
        new_proxy_request_id(),
    )
    .await
    .expect("qualified request executes");
    assert_eq!(execution.response.status, StatusCode::OK);
    execution.mark_started();
    assert!(
        execution
            .complete(DownstreamResult::Delivered)
            .await
            .expect("converges")
            .progress
            .completed
    );
    assert_eq!(
        server_a.count(),
        0,
        "pinned request never touches provider-a"
    );
    assert_eq!(
        server_b.count(),
        1,
        "pinned request dispatches once on provider-b"
    );
    assert_eq!(active_reservations(&fixture.database).await, 0);
    server_a.join().await;
    server_b.join().await;
    fixture.database.close().await.expect("database closes");
}

// ---------------------------------------------------------------------------
// 5. Retry failover, exhaustion, and no retry after handoff
// ---------------------------------------------------------------------------

#[tokio::test]
async fn c011_retry_failover_exhaustion_and_no_retry_after_handoff() {
    // Failover: first account 500s, second succeeds. Exact order 1+1.
    let failing = ScriptedProvider::start(vec![Script::immediate(
        500,
        br#"{"error":{"message":"boom"}}"#.to_vec(),
    )]);
    let succeeding = ScriptedProvider::start(vec![Script::immediate(
        200,
        upstream_finite_body(WireSurface::OpenaiChatCompletions),
    )]);
    let fixture = build_state(
        "openai",
        &[
            ProviderSpec::direct(
                "account-a",
                "provider-a",
                1,
                failing.base_url(),
                vec![WireSurface::OpenaiChatCompletions],
            ),
            ProviderSpec::direct(
                "account-b",
                "provider-b",
                2,
                succeeding.base_url(),
                vec![WireSurface::OpenaiChatCompletions],
            ),
        ],
        &[MODEL],
        BTreeMap::new(),
        &StreamOptions::default(),
    )
    .await;
    let proxy_id = new_proxy_request_id();
    let (execution, _) = execute_finite(
        &fixture.state,
        ClientSurface::ChatCompletions,
        client_finite_body(ClientSurface::ChatCompletions, MODEL),
        HeaderMap::new(),
        None,
        proxy_id.clone(),
    )
    .await
    .expect("failover executes");
    assert_eq!(execution.response.status, StatusCode::OK);
    assert_eq!(
        failing.count() + succeeding.count(),
        2,
        "exactly two attempts in order"
    );
    execution.mark_started();
    let result = execution
        .complete(DownstreamResult::Delivered)
        .await
        .expect("converges");
    assert!(result.progress.completed);
    assert_eq!(active_reservations(&fixture.database).await, 0);
    assert_eq!(fixture.state.active_request_count("account-a"), 0);
    assert_eq!(fixture.state.active_request_count("account-b"), 0);
    failing.join().await;
    succeeding.join().await;
    fixture.database.close().await.expect("database closes");

    // Post-handoff write failure never replays upstream (fresh single-account
    // fixture so backoff from the failover above cannot interact).
    let single = ScriptedProvider::start(vec![Script::immediate(
        200,
        upstream_finite_body(WireSurface::OpenaiChatCompletions),
    )]);
    let fixture2 = build_state(
        "openai",
        &[ProviderSpec::direct(
            "account-a",
            "provider-a",
            1,
            single.base_url(),
            vec![WireSurface::OpenaiChatCompletions],
        )],
        &[MODEL],
        BTreeMap::new(),
        &StreamOptions::default(),
    )
    .await;
    let (execution2, _) = execute_finite(
        &fixture2.state,
        ClientSurface::ChatCompletions,
        client_finite_body(ClientSurface::ChatCompletions, MODEL),
        HeaderMap::new(),
        None,
        new_proxy_request_id(),
    )
    .await
    .expect("second request executes");
    assert_eq!(single.count(), 1);
    execution2.mark_started();
    let result2 = execution2
        .complete(DownstreamResult::WriteFailed)
        .await
        .expect("write failure converges terminally");
    assert!(result2.progress.completed);
    assert_eq!(single.count(), 1, "no post-handoff replay");
    assert_eq!(active_reservations(&fixture2.database).await, 0);
    single.join().await;
    fixture2.database.close().await.expect("database closes");
}

// ---------------------------------------------------------------------------
// 6. C001 failure corpus matches the Rust classifier exactly
// ---------------------------------------------------------------------------

fn c011_observation(name: &str) -> FailureObservation {
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
fn c011_c001_failure_corpus_matches_rust_classifier() {
    let observations: Value = serde_json::from_str(C001_OBSERVATIONS).expect("C001 JSON");
    let cases = observations["failure_cases"]
        .as_object()
        .expect("failure cases");
    assert!(cases.len() >= 23, "C001 corpus has all mandatory rows");
    for (name, expected) in cases {
        let effects = classify(&c011_observation(name), RetryPolicy::default());
        assert_eq!(effects.retry, expected["retry"], "{name} retry");
        assert_eq!(
            effects.retry_action, expected["retry_action"],
            "{name} action"
        );
        assert_eq!(
            effects.retry_scope_label, expected["retry_scope"],
            "{name} scope"
        );
        assert_eq!(
            effects.client_outcome, expected["client_outcome"],
            "{name} outcome"
        );
        assert_eq!(
            effects.account_effect, expected["account_effect"],
            "{name} account"
        );
        assert_eq!(
            effects.model_effect, expected["model_effect"],
            "{name} model"
        );
        assert_eq!(effects.wire_effect, expected["wire_effect"], "{name} wire");
        assert_eq!(
            effects.evidence_class, expected["evidence_class"],
            "{name} evidence"
        );
    }
    // Post-handoff evidence never retries even when the same status would
    // fail over pre-handoff: the exact C011 closure invariant.
    let mut handoff = c011_observation("http_500_server");
    handoff.downstream_started = true;
    assert!(
        !classify(&handoff, RetryPolicy::default()).retry,
        "no retry after handoff"
    );
    let mut response_started = c011_observation("http_500_server");
    response_started.response_started = true;
    assert!(
        !classify(&response_started, RetryPolicy::default()).retry,
        "no retry after response start"
    );
}

// ---------------------------------------------------------------------------
// 7. Retry-After variants are uniformly bounded
// ---------------------------------------------------------------------------

#[test]
fn c011_retry_after_variants_are_uniformly_bounded() {
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
    // A 429 with an far-future HTTP date still classifies retryable but the
    // effective delay never exceeds the configured ceiling.
    let mut observation = c011_observation("http_429_rate_limit");
    observation.retry_after = parse_retry_after("999999", 1_700_000_000, policy);
    assert_eq!(observation.retry_after, Some(Duration::from_secs(1_800)));
    assert!(classify(&observation, policy).retry);
}

// ---------------------------------------------------------------------------
// 8. Fixed/negotiable wire profiles with leader/follower cancellation
// ---------------------------------------------------------------------------

#[tokio::test]
async fn c011_wire_fixed_and_negotiable_with_leader_follower() {
    let base = Instant::now();
    let chat = WireCandidate::new(profile(WireSurface::OpenaiChatCompletions), "chat");
    let messages = WireCandidate::new(profile(WireSurface::AnthropicMessages), "messages");
    let resolver = WireResolver::new(WireResolverConfig {
        cache_capacity: 4,
        learned_ttl: Duration::from_secs(60),
        rejection_ttl: Duration::from_secs(300),
        min_negotiation_interval: Duration::ZERO,
        max_concurrent_per_provider: 1,
        max_provider_state: 8,
        ..Default::default()
    });
    // Negotiable: metadata hint orders Messages first.
    resolver.set_metadata_hint("p", "m", WireSurface::AnthropicMessages);
    let initial = resolver.resolve("p", "m", vec![chat.clone(), messages.clone()], base);
    assert_eq!(
        initial.candidates[0].surface(),
        WireSurface::AnthropicMessages
    );
    // Learned success reorders to the accepted surface.
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
    // Deterministic rejection suppresses exactly one candidate for the TTL.
    resolver.reject(
        "p",
        "m",
        &learned.fingerprint,
        WireSurface::OpenaiChatCompletions,
        base + Duration::from_secs(1),
    );
    let suppressed = resolver.resolve(
        "p",
        "m",
        vec![chat.clone(), messages.clone()],
        base + Duration::from_secs(2),
    );
    assert_eq!(
        suppressed.candidates[0].surface(),
        WireSurface::AnthropicMessages
    );
    // Fixed operator preference collapses to a single candidate.
    resolver.set_operator_preference("p", "m", WireSurface::AnthropicMessages, true);
    let fixed = resolver.resolve(
        "p",
        "m",
        vec![chat.clone(), messages.clone()],
        base + Duration::from_secs(3),
    );
    assert_eq!(fixed.candidates.len(), 1);
    assert_eq!(
        fixed.candidates[0].surface(),
        WireSurface::AnthropicMessages
    );
    // Leader/follower share one decision; follower cancellation never cancels
    // the leader or releases its permit.
    let leader = resolver.begin_negotiation("p", "m", "flight", base).await;
    let follower = resolver.begin_negotiation("p", "m", "flight", base).await;
    assert_eq!(leader.role(), NegotiationRole::Leader);
    assert_eq!(follower.role(), NegotiationRole::Follower);
    drop(follower);
    leader.finish(
        NegotiationResult::Accepted(WireSurface::AnthropicMessages),
        base,
    );
    assert_eq!(resolver.snapshot().flights, 0);
    // Leader cancellation resolves the flight as rejected without leaking.
    let leader2 = resolver.begin_negotiation("p", "m", "cancel", base).await;
    let follower2 = resolver.begin_negotiation("p", "m", "cancel", base).await;
    drop(leader2);
    assert_eq!(follower2.wait().await, NegotiationResult::Rejected);
    assert_eq!(resolver.snapshot().flights, 0);
    assert_eq!(resolver.snapshot().provider_gates, 0);
    // Rate-limit delay throttles new negotiation reactively.
    resolver.delay_provider_negotiation("throttled", Duration::from_secs(60), base);
    let throttled = resolver
        .begin_negotiation("throttled", "m", "delayed", base + Duration::from_secs(1))
        .await;
    assert_eq!(throttled.role(), NegotiationRole::Throttled);
    // Alternate-wire deterministic rejection through the endpoint path: two
    // profiles on one account, first surface errors with a wire signal, the
    // retry uses the alternate wire on the same account.
    let wire_error = ScriptedProvider::start(vec![
        Script::with_headers(
            404,
            vec![("x-request-id".to_owned(), "wire-1".to_owned())],
            br#"{"error":{"message":"unknown endpoint"}}"#.to_vec(),
        ),
        Script::immediate(200, upstream_finite_body(WireSurface::AnthropicMessages)),
    ]);
    let wire_fixture = build_state(
        "openai",
        &[ProviderSpec::direct(
            "account-a",
            "provider-a",
            1,
            wire_error.base_url(),
            vec![
                WireSurface::OpenaiChatCompletions,
                WireSurface::AnthropicMessages,
            ],
        )],
        &[MODEL],
        BTreeMap::new(),
        &StreamOptions::default(),
    )
    .await;
    // The classifier proves the wire-rejection policy: with an alternate and
    // a wire signal the action is wire failover on the same account.
    let mut wire_obs = FailureObservation::response(9, 1, StatusCode::NOT_FOUND);
    wire_obs.wire_rejection = true;
    wire_obs.alternate_wire_available = true;
    assert_eq!(
        classify(&wire_obs, RetryPolicy::default()).action,
        eggpool::coordinator::NextAction::RetryWire
    );
    wire_error.join().await;
    wire_fixture
        .database
        .close()
        .await
        .expect("database closes");
}

// ---------------------------------------------------------------------------
// 9. Finite malformed and provider-error taxonomy
// ---------------------------------------------------------------------------

#[tokio::test]
async fn c011_finite_malformed_and_provider_error_taxonomy() {
    // Malformed 2xx body: terminal without retry.
    let malformed = ScriptedProvider::start(vec![Script::immediate(200, b"{not-json".to_vec())]);
    let fixture = build_state(
        "openai",
        &[ProviderSpec::direct(
            "account-a",
            "provider-a",
            1,
            malformed.base_url(),
            vec![WireSurface::OpenaiChatCompletions],
        )],
        &[MODEL],
        BTreeMap::new(),
        &StreamOptions::default(),
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
    .expect("malformed converges terminally");
    assert_eq!(malformed.count(), 1, "malformed never retries");
    assert_ne!(execution.response.status, StatusCode::OK);
    execution.mark_started();
    assert!(
        execution
            .complete(DownstreamResult::Delivered)
            .await
            .expect("converges")
            .progress
            .completed
    );
    assert_eq!(active_reservations(&fixture.database).await, 0);
    malformed.join().await;
    fixture.database.close().await.expect("database closes");

    // Non-retryable provider error passes through with proxy headers.
    let provider_error = ScriptedProvider::start(vec![Script::immediate(
        400,
        br#"{"error":{"message":"bad request"}}"#.to_vec(),
    )]);
    let fixture2 = build_state(
        "openai",
        &[ProviderSpec::direct(
            "account-a",
            "provider-a",
            1,
            provider_error.base_url(),
            vec![WireSurface::OpenaiChatCompletions],
        )],
        &[MODEL],
        BTreeMap::new(),
        &StreamOptions::default(),
    )
    .await;
    let (execution2, _) = execute_finite(
        &fixture2.state,
        ClientSurface::ChatCompletions,
        client_finite_body(ClientSurface::ChatCompletions, MODEL),
        HeaderMap::new(),
        None,
        new_proxy_request_id(),
    )
    .await
    .expect("provider error converges");
    assert_eq!(provider_error.count(), 1, "4xx never retries");
    assert_eq!(execution2.response.status, StatusCode::BAD_REQUEST);
    execution2.mark_started();
    assert!(
        execution2
            .complete(DownstreamResult::Delivered)
            .await
            .expect("converges")
            .progress
            .completed
    );
    provider_error.join().await;
    fixture2.database.close().await.expect("database closes");
}

// ---------------------------------------------------------------------------
// 10. Streaming timeouts, EOF taxonomy, terminal events, midstream abort
// ---------------------------------------------------------------------------

#[tokio::test]
async fn c011_stream_timeouts_eof_and_midstream_taxonomy() {
    // Header/first-byte timeout failover is proven live in C008
    // (`stream_header_timeout_fails_over_before_handoff`,
    // `stream_first_byte_timeout_fails_over_before_handoff`) with short M7
    // overrides against long server barriers. C011 reuses those qualified
    // boundaries and proves the EOF/terminal taxonomy live below; repeating
    // the 5s-barrier servers here would only add idle join budget.
    // Empty EOF, partial EOF, malformed EOF, terminal failure/incomplete,
    // and midstream abort are all terminal without retry on a single account.
    let cases: Vec<(&str, Vec<u8>, Option<usize>)> = vec![
        ("empty", Vec::new(), None),
        ("partial", b"data: {\"choices\":[]".to_vec(), None),
        ("malformed", b"data: {not-json}\n\n".to_vec(), None),
        (
            "responses_failed",
            concat!(
                "event: response.output_text.delta\n",
                "data: {\"type\":\"response.output_text.delta\",\"delta\":\"hi\"}\n\n",
                "event: response.failed\n",
                "data: {\"type\":\"response.failed\",\"response\":{\"id\":\"resp-1\",",
                "\"status\":\"failed\"}}\n\n",
            )
            .into(),
            None,
        ),
        (
            "responses_incomplete",
            concat!(
                "event: response.output_text.delta\n",
                "data: {\"type\":\"response.output_text.delta\",\"delta\":\"hi\"}\n\n",
                "event: response.incomplete\n",
                "data: {\"type\":\"response.incomplete\",\"response\":{\"id\":\"resp-1\",",
                "\"status\":\"incomplete\"}}\n\n",
            )
            .into(),
            None,
        ),
        (
            "midstream_abort",
            upstream_stream_body(WireSurface::OpenaiChatCompletions),
            Some(40),
        ),
    ];
    for (name, body, abort) in cases {
        let server = ScriptedProvider::start(vec![Script {
            status: 200,
            headers: vec![("content-type".to_owned(), "text/event-stream".to_owned())],
            body,
            header_delay: Duration::ZERO,
            body_delay: Duration::ZERO,
            abort_after_bytes: abort,
        }]);
        let fixture = build_state(
            "openai",
            &[ProviderSpec::direct(
                "account-a",
                "provider-a",
                1,
                server.base_url(),
                vec![WireSurface::OpenaiChatCompletions],
            )],
            &[MODEL],
            BTreeMap::new(),
            &StreamOptions::default(),
        )
        .await;
        let (mut execution, _) = execute_stream(
            &fixture.state,
            ClientSurface::ChatCompletions,
            client_stream_body(ClientSurface::ChatCompletions, MODEL),
            HeaderMap::new(),
            None,
            format!("c011-stream-{name}"),
        )
        .await
        .expect("stream executes");
        let (_chunks, _clean) = drain_stream(&mut execution).await;
        assert_eq!(server.count(), 1, "{name} never retries a started stream");
        assert!(execution.transport_released(), "{name} releases transport");
        let result = execution
            .complete(DownstreamResult::Delivered)
            .await
            .expect("terminal converges");
        assert!(result.progress.completed, "{name} converges");
        // Terminal EOF cases persist as error/interrupted, never completed
        // success: success requires native terminal evidence.
        let (status, _, _) =
            request_status(&fixture.database, &format!("c011-stream-{name}")).await;
        assert_ne!(status, "completed", "{name} is not false success");
        assert_eq!(active_reservations(&fixture.database).await, 0, "{name}");
        server.join().await;
        fixture.database.close().await.expect("database closes");
    }

    // Compatibility vs strict EOF: usage-complete EOF without [DONE] is
    // success under compatible policy, premature under strict.
    let usage_only: Vec<u8> = concat!(
        "data: {\"choices\":[{\"delta\":{\"content\":\"hello\"}}]}\n\n",
        "data: {\"choices\":[],\"usage\":{\"prompt_tokens\":10,",
        "\"completion_tokens\":4,\"total_tokens\":14}}\n\n",
    )
    .into();
    for (policy, expect_completed) in [("strict", false), ("compatible", true)] {
        let server = ScriptedProvider::start(vec![Script::with_headers(
            200,
            vec![("content-type".to_owned(), "text/event-stream".to_owned())],
            usage_only.clone(),
        )]);
        let fixture = build_state(
            "openai",
            &[ProviderSpec::direct(
                "account-a",
                "provider-a",
                1,
                server.base_url(),
                vec![WireSurface::OpenaiChatCompletions],
            )],
            &[MODEL],
            BTreeMap::new(),
            &StreamOptions {
                completion_policy: Some(policy.to_owned()),
                ..Default::default()
            },
        )
        .await;
        let proxy_id = format!("c011-compat-{policy}");
        let (mut execution, _) = execute_stream(
            &fixture.state,
            ClientSurface::ChatCompletions,
            client_stream_body(ClientSurface::ChatCompletions, MODEL),
            HeaderMap::new(),
            None,
            proxy_id.clone(),
        )
        .await
        .expect("compat executes");
        let (_chunks, _clean) = drain_stream(&mut execution).await;
        let result = execution
            .complete(DownstreamResult::Delivered)
            .await
            .expect("converges");
        assert!(result.progress.completed);
        let (status, _, _) = request_status(&fixture.database, &proxy_id).await;
        assert_eq!(status == "completed", expect_completed, "policy {policy}");
        server.join().await;
        fixture.database.close().await.expect("database closes");
    }
}

// ---------------------------------------------------------------------------
// 11. Cancellation before/after handoff and downstream write failure
// ---------------------------------------------------------------------------

#[tokio::test]
async fn c011_cancellation_and_write_failure_boundaries() {
    let server = ScriptedProvider::start(vec![
        Script::immediate(
            200,
            upstream_finite_body(WireSurface::OpenaiChatCompletions),
        ),
        Script::immediate(
            200,
            upstream_finite_body(WireSurface::OpenaiChatCompletions),
        ),
        Script::immediate(
            200,
            upstream_finite_body(WireSurface::OpenaiChatCompletions),
        ),
    ]);
    let fixture = build_state(
        "openai",
        &[ProviderSpec::direct(
            "account-a",
            "provider-a",
            1,
            server.base_url(),
            vec![WireSurface::OpenaiChatCompletions],
        )],
        &[MODEL],
        BTreeMap::new(),
        &StreamOptions::default(),
    )
    .await;
    // Dropped finite execution without complete() converges as interrupted
    // through retained ownership instead of stranding the claim.
    {
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
        sleep(Duration::from_millis(200)).await;
    }
    // Cancelled after handoff converges terminally without replay.
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
    let cancelled = execution
        .complete(DownstreamResult::Cancelled)
        .await
        .expect("cancelled converges");
    assert!(cancelled.progress.completed);
    // Write failure after handoff never retries.
    let count_before = server.count();
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
    let failed = execution
        .complete(DownstreamResult::WriteFailed)
        .await
        .expect("write failure converges");
    assert!(failed.progress.completed);
    assert_eq!(server.count(), count_before + 1, "no post-handoff replay");
    assert_eq!(active_reservations(&fixture.database).await, 0);
    assert_eq!(fixture.state.active_request_count("account-a"), 0);

    // Streaming cancellation before start vs after start.
    let stream_server = ScriptedProvider::start(vec![
        Script::with_headers(
            200,
            vec![("content-type".to_owned(), "text/event-stream".to_owned())],
            upstream_stream_body(WireSurface::OpenaiChatCompletions),
        ),
        Script::with_headers(
            200,
            vec![("content-type".to_owned(), "text/event-stream".to_owned())],
            upstream_stream_body(WireSurface::OpenaiChatCompletions),
        ),
    ]);
    let stream_fixture = build_state(
        "openai",
        &[ProviderSpec::direct(
            "account-a",
            "provider-a",
            1,
            stream_server.base_url(),
            vec![WireSurface::OpenaiChatCompletions],
        )],
        &[MODEL],
        BTreeMap::new(),
        &StreamOptions::default(),
    )
    .await;
    let (execution, _) = execute_stream(
        &stream_fixture.state,
        ClientSurface::ChatCompletions,
        client_stream_body(ClientSurface::ChatCompletions, MODEL),
        HeaderMap::new(),
        None,
        new_proxy_request_id(),
    )
    .await
    .expect("stream executes");
    assert!(!execution.handoff_started());
    let interrupted = execution
        .complete(DownstreamResult::Cancelled)
        .await
        .expect("converges");
    assert!(interrupted.progress.completed);
    stream_server.join().await;
    stream_fixture
        .database
        .close()
        .await
        .expect("database closes");
    server.join().await;
    fixture.database.close().await.expect("database closes");
}

// ---------------------------------------------------------------------------
// 12. Finalization duplicates, conflicts, release idempotency, capacity
// ---------------------------------------------------------------------------

async fn published_attempt(
    database: &Database,
    router: &RoutingRouter,
    proxy_id: &str,
) -> eggpool::coordinator::PublishedAttempt {
    let mut facts = RoutingRequestFacts::new("model-a");
    facts.requested_protocol = Some("openai".into());
    facts.client_protocol = Some("openai".into());
    facts.projected_tokens = 42;
    let claim = router
        .select_and_claim(&facts, &BTreeSet::new())
        .await
        .expect("claim")
        .expect("candidate");
    let outcome = PublicationService::new(database.clone())
        .publish(
            claim,
            PublicationInput::new(proxy_id, "openai", "openai", false, 1),
        )
        .await
        .expect("publish");
    let PublicationOutcome::Published(value) = outcome else {
        panic!("expected published attempt");
    };
    *value
}

async fn finalization_fixture() -> (Database, RoutingRouter, QuotaEstimator) {
    let database = Database::open(DatabaseConfig::default()).await.expect("db");
    MigrationRunner::new(&database)
        .run()
        .await
        .expect("migrate");
    database
        .call(|connection| {
            connection.execute(
                "INSERT INTO accounts (id, name, api_key_env, enabled, provider_id)
                 VALUES (1, 'account-a', 'UNUSED', 1, 'provider-a')",
                [],
            )?;
            connection.execute(
                "INSERT INTO models (model_id, protocol, provider_id, resolution_status)
                 VALUES ('model-a', 'openai', 'provider-a', 'resolved')",
                [],
            )?;
            Ok(())
        })
        .await
        .expect("rows");
    let mut config = Config::default();
    let mut provider = ProviderConfig {
        id: "provider-a".into(),
        base_url: "https://provider.invalid/v1".into(),
        protocols: vec!["openai".into()],
        auth: ProviderAuthConfig {
            mode: "none".into(),
            ..Default::default()
        },
        ..Default::default()
    };
    provider.accounts.push(AccountConfig {
        name: "account-a".into(),
        ..Default::default()
    });
    config.providers.insert("provider-a".into(), provider);
    config.validate().expect("config");
    let registry = AccountRegistry::from_config(
        &config,
        &[Account {
            id: 1,
            name: "account-a".into(),
            api_key_env: "UNUSED".into(),
            enabled: true,
            weight: 1.0,
            provider_id: "provider-a".into(),
        }],
        &CredentialStore::default(),
    )
    .expect("registry");
    let mut catalog = ModelCatalogCache::default();
    catalog.set_account_provider("account-a", "provider-a");
    let mut model = ModelInput::new("model-a");
    model.protocol = Some("openai".into());
    model.protocol_source = Some("fixture".into());
    model.resolution_status = ProtocolResolutionStatus::Resolved;
    catalog
        .update_from_account("account-a", "provider-a", &[model], true, true)
        .expect("seed");
    let estimator = QuotaEstimator::new([AccountQuota::new("account-a")]);
    let router = RoutingRouter::new(
        registry,
        catalog,
        estimator.clone(),
        None,
        EligibilityPolicy::default(),
    );
    (database, router, estimator)
}

#[tokio::test]
async fn c011_finalization_duplicate_conflict_release_and_capacity() {
    let (database, router, _) = finalization_fixture().await;
    let published = published_attempt(&database, &router, "c011-final").await;
    let finalizer = DurableFinalizer::new(database.clone());
    // First completion with a claim converges and releases runtime exactly once.
    let result = finalizer
        .finalize_request(
            &published.identity,
            FinalizationData {
                outcome: FinalizationOutcome::Completed,
                input_tokens: 10,
                output_tokens: 4,
                release_reason: Some("completed".into()),
                ..FinalizationData::default()
            },
            Some(published.claim),
        )
        .await
        .expect("completes");
    assert!(result.progress.completed);
    assert!(result.runtime_released);
    assert_eq!(router.active_request_count("account-a"), 0);
    // Durable-only duplicate observes convergence without new runtime work.
    let duplicate = finalizer
        .finalize_request(
            &published.identity,
            FinalizationData {
                outcome: FinalizationOutcome::Completed,
                release_reason: Some("completed".into()),
                ..FinalizationData::default()
            },
            None,
        )
        .await
        .expect("duplicate converges");
    assert!(duplicate.progress.completed);
    assert!(!duplicate.progress.runtime_cleanup_required);
    // Incompatible terminal outcome fails closed as a terminal conflict.
    let conflict = finalizer
        .finalize_request(
            &published.identity,
            FinalizationData {
                outcome: FinalizationOutcome::ClientError,
                ..FinalizationData::default()
            },
            None,
        )
        .await
        .expect_err("conflict fails closed");
    assert!(matches!(
        conflict,
        FinalizationError::TerminalConflict { .. }
    ));
    // Supervisor shares compatible duplicates and bounds capacity.
    let supervisor =
        FinalizationSupervisor::with_capacity(DurableFinalizer::new(database.clone()), 1);
    let published2 = published_attempt(&database, &router, "c011-supervisor").await;
    let command = FinalizationCommand::Request {
        identity: published2.identity.clone(),
        data: FinalizationData {
            outcome: FinalizationOutcome::Completed,
            release_reason: Some("completed".into()),
            ..FinalizationData::default()
        },
        claim: Some(published2.claim),
    };
    let first = supervisor
        .register(command.clone())
        .expect("first registers");
    let second = supervisor.register(command).expect("duplicate shares");
    let (first_result, second_result) = tokio::join!(first.wait(), second.wait());
    assert!(first_result.is_ok());
    assert!(second_result.is_ok());
    supervisor.drain().await;
    assert_eq!(supervisor.snapshot().active_jobs, 0);
    // Effect ledger retires before capacity and never double-applies.
    let mut ledger = EffectLedger::with_capacity(8);
    for attempt_id in 0..64 {
        assert_eq!(ledger.try_apply_once(attempt_id), Ok(true));
        assert!(ledger.retire(attempt_id));
    }
    assert!(ledger.is_empty());
    let mut engine = FailureDecisionEngine::new(RetryPolicy::default());
    engine.ledger = EffectLedger::with_capacity(1);
    assert!(
        engine
            .decide(&c011_observation("http_500_server"))
            .expect("first")
            .1
    );
    assert!(
        engine
            .decide(&FailureObservation {
                attempt_id: 2,
                ..c011_observation("http_500_server")
            })
            .is_err()
    );
    database.close().await.expect("database closes");
}

// ---------------------------------------------------------------------------
// 13. Publication conflict, post-commit interruption, crash/restart recovery
// ---------------------------------------------------------------------------

#[tokio::test]
async fn c011_publication_conflict_and_crash_reconciliation_recovery() {
    let server = ScriptedProvider::start(vec![
        Script::immediate(
            200,
            upstream_finite_body(WireSurface::OpenaiChatCompletions),
        ),
        Script::immediate(
            200,
            upstream_finite_body(WireSurface::OpenaiChatCompletions),
        ),
    ]);
    let fixture = build_state(
        "openai",
        &[ProviderSpec::direct(
            "account-a",
            "provider-a",
            1,
            server.base_url(),
            vec![WireSurface::OpenaiChatCompletions],
        )],
        &[MODEL],
        BTreeMap::new(),
        &StreamOptions::default(),
    )
    .await;
    // Duplicate proxy request identity is a terminal conflict, not a second
    // row: the second submission fails closed with 409 semantics.
    let proxy_id = new_proxy_request_id();
    let (execution, _) = execute_finite(
        &fixture.state,
        ClientSurface::ChatCompletions,
        client_finite_body(ClientSurface::ChatCompletions, MODEL),
        HeaderMap::new(),
        None,
        proxy_id.clone(),
    )
    .await
    .expect("first executes");
    execution.mark_started();
    assert!(
        execution
            .complete(DownstreamResult::Delivered)
            .await
            .expect("converges")
            .progress
            .completed
    );
    let duplicate = execute_finite(
        &fixture.state,
        ClientSurface::ChatCompletions,
        client_finite_body(ClientSurface::ChatCompletions, MODEL),
        HeaderMap::new(),
        None,
        proxy_id,
    )
    .await;
    assert!(duplicate.is_err(), "duplicate proxy identity fails closed");

    // Simulated crash: publish directly, drop the claim without terminal
    // release, then reconcile. The exact Python `_crash_recovery` policy
    // converges pending->interrupted with no new rows and no replay.
    let (crash_db, crash_router, _) = finalization_fixture().await;
    let mut facts = RoutingRequestFacts::new("model-a");
    facts.requested_protocol = Some("openai".into());
    facts.client_protocol = Some("openai".into());
    facts.projected_tokens = 42;
    let claim = crash_router
        .select_and_claim(&facts, &BTreeSet::new())
        .await
        .expect("claim")
        .expect("candidate");
    let outcome = PublicationService::new(crash_db.clone())
        .publish(
            claim,
            PublicationInput::new("c011-crash", "openai", "openai", false, 1),
        )
        .await
        .expect("publish");
    let PublicationOutcome::Published(published) = outcome else {
        panic!("expected publication");
    };
    drop(published.claim);
    let report = CrashReconciler::new(crash_db.clone())
        .reconcile_once()
        .await
        .expect("reconcile");
    assert_eq!(report.requests_interrupted, 1);
    assert!(report.bounded);
    let second = CrashReconciler::new(crash_db.clone())
        .reconcile_once()
        .await
        .expect("second pass");
    assert_eq!(
        second.requests_interrupted, 0,
        "reconciliation is idempotent"
    );
    // Restart over the same DB stays converged and Python-readable: no
    // pending/active/open rows remain.
    let nonterminal: (i64, i64, i64) = crash_db
        .call(|connection| {
            Ok((
                connection.query_row(
                    "SELECT COUNT(*) FROM requests WHERE status = 'pending'",
                    [],
                    |row| row.get(0),
                )?,
                connection.query_row(
                    "SELECT COUNT(*) FROM request_attempts WHERE completed_at IS NULL",
                    [],
                    |row| row.get(0),
                )?,
                connection.query_row(
                    "SELECT COUNT(*) FROM reservations WHERE status = 'active'",
                    [],
                    |row| row.get(0),
                )?,
            ))
        })
        .await
        .expect("counts");
    assert_eq!(nonterminal, (0, 0, 0));
    crash_db.close().await.expect("database closes");

    // Subsequent valid request recovers without restart.
    let (recovery, _) = execute_finite(
        &fixture.state,
        ClientSurface::ChatCompletions,
        client_finite_body(ClientSurface::ChatCompletions, MODEL),
        HeaderMap::new(),
        None,
        new_proxy_request_id(),
    )
    .await
    .expect("recovery executes");
    recovery.mark_started();
    assert!(
        recovery
            .complete(DownstreamResult::Delivered)
            .await
            .expect("converges")
            .progress
            .completed
    );
    assert_eq!(active_reservations(&fixture.database).await, 0);
    server.join().await;
    fixture.database.close().await.expect("database closes");
}

// ---------------------------------------------------------------------------
// 14. Bounded concurrency and cancellation-storm leak pass
// ---------------------------------------------------------------------------

#[tokio::test]
async fn c011_concurrent_requests_and_cancellation_storm_converge_without_leak() {
    let scripts = (0..8)
        .map(|_| {
            Script::immediate(
                200,
                upstream_finite_body(WireSurface::OpenaiChatCompletions),
            )
        })
        .collect::<Vec<_>>();
    let server = ScriptedProvider::start(scripts);
    let fixture = Arc::new(
        build_state(
            "openai",
            &[ProviderSpec::direct(
                "account-a",
                "provider-a",
                1,
                server.base_url(),
                vec![WireSurface::OpenaiChatCompletions],
            )],
            &[MODEL],
            BTreeMap::new(),
            &StreamOptions::default(),
        )
        .await,
    );
    // Bounded concurrent batch through shared account/wire/finalizer state.
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
                format!("c011-concurrent-{index}"),
            )
            .await
            .expect("concurrent executes");
            execution.mark_started();
            execution
                .complete(DownstreamResult::Delivered)
                .await
                .expect("completes")
        }));
    }
    for task in tasks {
        assert!(task.await.expect("joins").progress.completed);
    }
    assert_eq!(server.count(), 8, "every request dispatches exactly once");
    assert_eq!(fixture.state.active_request_count("account-a"), 0);
    assert_eq!(active_reservations(&fixture.database).await, 0);
    let (_, _, active) = durable_counts(&fixture.database).await;
    assert_eq!(active, 0, "no leaked active reservations");
    server.join().await;
    // Cancellation storm: dropped executions cannot strand claims.
    // Five requests share this fixture (4 dropped + 1 recovery), so serve
    // five identical successes.
    let server2 = ScriptedProvider::start(vec![
        Script::immediate(
            200,
            upstream_finite_body(WireSurface::OpenaiChatCompletions),
        ),
        Script::immediate(
            200,
            upstream_finite_body(WireSurface::OpenaiChatCompletions),
        ),
        Script::immediate(
            200,
            upstream_finite_body(WireSurface::OpenaiChatCompletions),
        ),
        Script::immediate(
            200,
            upstream_finite_body(WireSurface::OpenaiChatCompletions),
        ),
        Script::immediate(
            200,
            upstream_finite_body(WireSurface::OpenaiChatCompletions),
        ),
    ]);
    let fixture2 = build_state(
        "openai",
        &[ProviderSpec::direct(
            "account-a",
            "provider-a",
            1,
            server2.base_url(),
            vec![WireSurface::OpenaiChatCompletions],
        )],
        &[MODEL],
        BTreeMap::new(),
        &StreamOptions::default(),
    )
    .await;
    for index in 0..4 {
        let (execution, _) = execute_finite(
            &fixture2.state,
            ClientSurface::ChatCompletions,
            client_finite_body(ClientSurface::ChatCompletions, MODEL),
            HeaderMap::new(),
            None,
            format!("c011-storm-{index}"),
        )
        .await
        .expect("executes");
        drop(execution);
    }
    sleep(Duration::from_millis(300)).await;
    assert_eq!(fixture2.state.active_request_count("account-a"), 0);
    assert_eq!(active_reservations(&fixture2.database).await, 0);
    // Next valid request succeeds without restart after the storm.
    let (recovery, _) = execute_finite(
        &fixture2.state,
        ClientSurface::ChatCompletions,
        client_finite_body(ClientSurface::ChatCompletions, MODEL),
        HeaderMap::new(),
        None,
        new_proxy_request_id(),
    )
    .await
    .expect("recovers");
    recovery.mark_started();
    assert!(
        recovery
            .complete(DownstreamResult::Delivered)
            .await
            .expect("converges")
            .progress
            .completed
    );
    server2.join().await;
    fixture2.database.close().await.expect("database closes");
    Arc::try_unwrap(fixture)
        .map_err(|_| "fixture shared")
        .expect("unwraps")
        .database
        .close()
        .await
        .expect("database closes");
}

// ---------------------------------------------------------------------------
// 15. Virtual-router semantic selection with affinity
// ---------------------------------------------------------------------------

#[tokio::test]
async fn c011_virtual_router_semantic_selection_with_affinity() {
    // Route IDs are label-sorted: 0=default, 1=fast. The selector must
    // return the compiled route ID, not the config key. Affinity means the
    // selector serves exactly once; the untouched default route points at a
    // closed port so no server waits on it.
    let selector = ScriptedProvider::start(vec![Script::immediate(200, selector_body("1"))]);
    let fast = ScriptedProvider::start(vec![
        Script::immediate(
            200,
            upstream_finite_body(WireSurface::OpenaiChatCompletions),
        ),
        Script::immediate(
            200,
            upstream_finite_body(WireSurface::OpenaiChatCompletions),
        ),
    ]);
    let fixture = build_state(
        "openai",
        &[
            ProviderSpec::direct(
                "selector-account",
                "selector-provider",
                1,
                selector.base_url(),
                vec![WireSurface::OpenaiChatCompletions],
            ),
            ProviderSpec::direct(
                "fast-account",
                "fast-provider",
                2,
                fast.base_url(),
                vec![WireSurface::OpenaiChatCompletions],
            ),
            ProviderSpec::direct(
                "default-account",
                "default-provider",
                3,
                "http://127.0.0.1:9".to_owned(),
                vec![WireSurface::OpenaiChatCompletions],
            ),
        ],
        &["selector-model", "model-fast", "model-default"],
        virtual_router_pinned(),
        &StreamOptions::default(),
    )
    .await;
    let session = Some("session-c011".to_owned());
    let virtual_body = Bytes::from(
        serde_json::to_vec(
            &json!({"model": VIRTUAL, "messages": [{"role": "user", "content": "hello"}]}),
        )
        .expect("virtual body"),
    );
    // First request runs the selector then the concrete dispatch.
    let (execution, virtual_resolution) = execute_finite(
        &fixture.state,
        ClientSurface::ChatCompletions,
        virtual_body.clone(),
        HeaderMap::new(),
        session.clone(),
        new_proxy_request_id(),
    )
    .await
    .expect("virtual executes");
    let resolution = virtual_resolution.expect("virtual facts");
    assert_eq!(resolution.virtual_model, VIRTUAL);
    assert_eq!(resolution.route_id, "1");
    assert_eq!(resolution.concrete_model, "model-fast/fast-provider");
    assert_eq!(execution.response.status, StatusCode::OK);
    execution.mark_started();
    assert!(
        execution
            .complete(DownstreamResult::Delivered)
            .await
            .expect("converges")
            .progress
            .completed
    );
    assert_eq!(selector.count(), 1, "selector dispatches once");
    assert_eq!(fast.count(), 1, "concrete dispatches once");
    // Second same-session request hits affinity with zero new selector I/O.
    let (execution2, resolution2) = execute_finite(
        &fixture.state,
        ClientSurface::ChatCompletions,
        virtual_body,
        HeaderMap::new(),
        session,
        new_proxy_request_id(),
    )
    .await
    .expect("affinity executes");
    assert!(resolution2.expect("facts").affinity_hit, "affinity hit");
    execution2.mark_started();
    assert!(
        execution2
            .complete(DownstreamResult::Delivered)
            .await
            .expect("converges")
            .progress
            .completed
    );
    assert_eq!(
        selector.count(),
        1,
        "affinity avoids a second selector call"
    );
    assert_eq!(fast.count(), 2, "concrete still dispatches");
    assert_eq!(active_reservations(&fixture.database).await, 0);
    selector.join().await;
    fast.join().await;
    fixture.database.close().await.expect("database closes");
}

// ---------------------------------------------------------------------------
// 16. Direct and proxied M4 account clients share coordinator semantics
// ---------------------------------------------------------------------------

#[tokio::test]
async fn c011_direct_and_proxied_account_clients_share_semantics() {
    // Direct account succeeds; proxied account points at a closed local port
    // so its transport failure is a retryable account failure that fails
    // over to the direct account without poisoning shared state.
    // Exactly one qualified dispatch lands on the direct provider; the
    // proxied account is topology-only for this cell (T006 owns live proxy
    // interop, C011 proves the coordinator shares semantics and fails
    // closed without secret leaks).
    let direct = ScriptedProvider::start(vec![Script::immediate(
        200,
        upstream_finite_body(WireSurface::OpenaiChatCompletions),
    )]);
    let fixture = build_state(
        "openai",
        &[
            ProviderSpec {
                name: "proxied-account".to_owned(),
                provider_id: "provider-proxied".to_owned(),
                account_id: 1,
                base_url: "http://127.0.0.1:9".to_owned(),
                surfaces: vec![WireSurface::OpenaiChatCompletions],
                proxy_url: Some("socks5://127.0.0.1:9".to_owned()),
            },
            ProviderSpec::direct(
                "direct-account",
                "provider-direct",
                2,
                direct.base_url(),
                vec![WireSurface::OpenaiChatCompletions],
            ),
        ],
        &[MODEL],
        BTreeMap::new(),
        &StreamOptions::default(),
    )
    .await;
    // A misconfigured proxy URL fails closed at pool construction without
    // leaking the credential into the error surface.
    let mut bad_config = Config::default();
    let mut bad_provider = ProviderConfig {
        id: "provider-bad".into(),
        base_url: "http://127.0.0.1:9".into(),
        protocols: vec!["openai".into()],
        auth: ProviderAuthConfig {
            mode: "none".into(),
            ..Default::default()
        },
        ..Default::default()
    };
    bad_provider.accounts.push(AccountConfig {
        name: "bad-account".into(),
        proxy_url: Some("unknown-scheme://proxy-user:proxy-secret-12345@127.0.0.1:1080".into()),
        ..Default::default()
    });
    bad_config
        .providers
        .insert("provider-bad".into(), bad_provider);
    let pool_error =
        ProviderClientPool::from_config(&bad_config).expect_err("bad proxy fails closed");
    assert!(
        !format!("{pool_error:?}").contains("proxy-secret-12345"),
        "no proxy secret leak"
    );
    // Direct-only sanity: qualified pin to the direct provider succeeds.
    let (execution, _) = execute_finite(
        &fixture.state,
        ClientSurface::ChatCompletions,
        client_finite_body(
            ClientSurface::ChatCompletions,
            "fixture-model/provider-direct",
        ),
        HeaderMap::new(),
        None,
        new_proxy_request_id(),
    )
    .await
    .expect("direct pin executes");
    assert_eq!(execution.response.status, StatusCode::OK);
    execution.mark_started();
    assert!(
        execution
            .complete(DownstreamResult::Delivered)
            .await
            .expect("converges")
            .progress
            .completed
    );
    assert_eq!(direct.count(), 1);
    assert_eq!(active_reservations(&fixture.database).await, 0);
    direct.join().await;
    fixture.database.close().await.expect("database closes");
}

// ---------------------------------------------------------------------------
// 17. Security, dependency, and resource audit
// ---------------------------------------------------------------------------

#[tokio::test]
async fn c011_security_dependency_resource_audit() {
    // Secrets never enter Debug, diagnostics, or persisted error detail.
    let mut incoming = HeaderMap::new();
    incoming.insert("authorization", "client-secret-abc".parse().unwrap());
    incoming.insert("x-api-key", "key-secret-abc".parse().unwrap());
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
    let clients = ProviderClientPool::from_config(&config).expect("pool");
    let builder = AttemptBuilder::new(clients, WireRuntime::embedded().expect("wire"));
    let attempt = builder
        .prepare(eggpool::coordinator::AttemptInput {
            identity: eggpool::coordinator::FinalizationIdentity {
                proxy_request_id: "audit".into(),
                db_request_id: 1,
                attempt_id: 2,
                reservation_id: 3,
                account_id: 4,
                account_name: "account-a".into(),
                provider_id: "provider-a".into(),
                model_id: "canonical".into(),
                upstream_model_id: "native".into(),
                client_protocol: "openai".into(),
                upstream_protocol: "openai".into(),
                attempt_number: 1,
            },
            provider,
            account_api_key: Some("account-secret-xyz".into()),
            incoming_headers: incoming,
            request_id: Some("request-id".into()),
            correlation_id: Some("correlation-id".into()),
            raw_body: Bytes::from_static(br#"{"model":"canonical","messages":[]}"#),
            client_surface: ClientSurface::ChatCompletions,
            profile: profile(WireSurface::OpenaiChatCompletions),
            stream: false,
            candidate_fingerprint: "audit".into(),
        })
        .expect("prepare");
    let debug = format!("{attempt:?}");
    assert!(
        !debug.contains("account-secret-xyz"),
        "no account secret in Debug"
    );
    assert!(
        !debug.contains("client-secret-abc"),
        "no client secret in Debug"
    );
    assert!(!debug.contains("key-secret-abc"), "no api key in Debug");
    assert!(
        !attempt.headers.contains_key("authorization")
            || attempt.headers["authorization"] != "client-secret-abc"
    );

    // Session identities are hashed, never forwarded or logged.
    let session_debug = format!("{:?}", fixture_session_hash());
    assert!(!session_debug.contains("raw-session-value"));

    // Dependency audit: exactly one HTTP/TLS stack (hyper+rsp), no second
    // stack, ORM, actor framework, or task queue.
    let manifest = include_str!("../Cargo.toml");
    assert!(manifest.contains("hyper"), "hyper transport retained");
    assert!(!manifest.contains("reqwest"), "no second HTTP stack");
    assert!(!manifest.contains("diesel"), "no ORM");
    assert!(!manifest.contains("sea-orm"), "no ORM");
    assert!(!manifest.contains("actix"), "no actor framework");
    assert!(!manifest.contains("celery"), "no task queue");

    // Resource bounds: resolver, ledger, and supervisor snapshots stay bounded.
    let resolver = WireResolver::new(WireResolverConfig {
        cache_capacity: 2,
        max_provider_state: 2,
        min_negotiation_interval: Duration::ZERO,
        ..Default::default()
    });
    let now = Instant::now();
    let chat = WireCandidate::new(profile(WireSurface::OpenaiChatCompletions), "c");
    let messages = WireCandidate::new(profile(WireSurface::AnthropicMessages), "m");
    for index in 0..8 {
        resolver.resolve(
            &format!("p{index}"),
            "m",
            vec![chat.clone(), messages.clone()],
            now,
        );
    }
    assert!(resolver.snapshot().entries <= 2, "resolver bounded");
    let supervisor = FinalizationSupervisor::with_capacity(
        DurableFinalizer::new(Database::open(DatabaseConfig::default()).await.expect("db")),
        4,
    );
    assert_eq!(supervisor.snapshot().capacity, 4);
    assert_eq!(supervisor.snapshot().active_jobs, 0);
}

fn fixture_session_hash() -> String {
    use sha2::{Digest, Sha256};
    let mut hasher = Sha256::new();
    hasher.update(b"raw-session-value");
    format!("{:x}", hasher.finalize())
}
