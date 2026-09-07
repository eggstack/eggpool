//! C008 streaming handoff, timeouts, cancellation, and terminal policy.
//!
//! End-to-end coverage for the streaming inference path: M7-owned
//! response-header/first-byte/idle timers, incremental M6 stream decoding with
//! per-chunk client adaptation, downstream handoff monotonicity, terminal
//! evidence classification (EOF is never success), midstream failure handling,
//! client cancellation at every phase, and retained C006 convergence.
//!
//! Every test uses deterministic local HTTP providers (no external network).
//! Timer tests use short M7 policies against long server barriers; attempt
//! counts and the exact no-retry-after-handoff boundary are asserted
//! explicitly.

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
    config::{AccountConfig, ProviderAuthConfig, ProviderConfig, ProviderStreamTimeoutConfig},
    coordinator::{
        AttemptBuilder, DownstreamResult, DurableFinalizer, FinalizationSupervisor,
        OUTCOME_CLIENT_CANCELLED, OUTCOME_COMPLETED_CANONICAL, OUTCOME_COMPLETED_COMPATIBILITY,
        OUTCOME_EMPTY_EOF, OUTCOME_FIRST_BYTE_TIMEOUT, OUTCOME_IDLE_TIMEOUT, OUTCOME_MALFORMED_EOF,
        OUTCOME_PREMATURE_EOF_BEFORE_BODY, OUTCOME_PREMATURE_EOF_MIDSTREAM,
        OUTCOME_RESPONSE_HEADER_TIMEOUT, OUTCOME_TERMINAL_FAILURE, OUTCOME_TERMINAL_INCOMPLETE,
        OUTCOME_UPSTREAM_MIDSTREAM_ERROR, PublicationService, ResponseHandoffState, RetryPolicy,
        StreamChunkError, StreamPhase, StreamRequest, StreamTimeoutPolicy, StreamingCoordinator,
        StreamingCoordinatorError, StreamingExecution, WireResolver, WireResolverConfig,
    },
    db::{Account, Database, DatabaseConfig, MigrationRunner},
    providers::ProviderClientPool,
    quota::{AccountQuota, QuotaEstimator},
    request::StaticRoutingFacts,
    routing::{EligibilityPolicy, RoutingRouter},
    wire::{
        ConfiguredWireProfile, WireCodecId, WireProfileDefinition, WireRuntime, WireSurface,
        ir::ClientSurface,
    },
};
use http::{HeaderMap, StatusCode};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    time::sleep,
};

const MODEL: &str = "fixture-model";

// ---------------------------------------------------------------------------
// Wire profiles and streaming payloads
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

fn stream_client_body(client: ClientSurface) -> Bytes {
    let value = match client {
        ClientSurface::ChatCompletions => serde_json::json!({
            "model": MODEL,
            "stream": true,
            "messages": [{"role": "user", "content": "hello"}],
        }),
        ClientSurface::Responses => serde_json::json!({
            "model": MODEL,
            "stream": true,
            "input": [{"type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "hello"}]}],
            "max_output_tokens": 32,
        }),
        ClientSurface::Messages => serde_json::json!({
            "model": MODEL,
            "stream": true,
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 32,
        }),
    };
    Bytes::from(serde_json::to_vec(&value).expect("request serializes"))
}

fn client_terminal_marker(client: ClientSurface) -> &'static str {
    match client {
        ClientSurface::ChatCompletions => "[DONE]",
        ClientSurface::Responses => "response.completed",
        ClientSurface::Messages => "message_stop",
    }
}

/// Successful SSE byte sequences per upstream profile (usage 10/4 + native
/// terminal evidence), mirroring the M6 `wire_runtime` stream fixtures.
fn upstream_success_stream(upstream: WireSurface) -> Vec<u8> {
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

/// Payload + usage chunks with no terminal event (compatibility probe).
fn usage_without_terminal() -> Vec<u8> {
    concat!(
        "data: {\"choices\":[{\"delta\":{\"content\":\"hello\"}}]}\n\n",
        "data: {\"choices\":[],\"usage\":{\"prompt_tokens\":10,",
        "\"completion_tokens\":4,\"total_tokens\":14}}\n\n",
    )
    .into()
}

fn responses_failed_stream() -> Vec<u8> {
    concat!(
        "event: response.output_text.delta\n",
        "data: {\"type\":\"response.output_text.delta\",\"delta\":\"hi\"}\n\n",
        "event: response.failed\n",
        "data: {\"type\":\"response.failed\",\"response\":{\"id\":\"resp-1\",",
        "\"status\":\"failed\",\"error\":{\"message\":\"boom\",",
        "\"type\":\"server_error\"}}}\n\n",
    )
    .into()
}

fn responses_incomplete_stream() -> Vec<u8> {
    concat!(
        "event: response.output_text.delta\n",
        "data: {\"type\":\"response.output_text.delta\",\"delta\":\"hi\"}\n\n",
        "event: response.incomplete\n",
        "data: {\"type\":\"response.incomplete\",\"response\":{\"id\":\"resp-1\",",
        "\"status\":\"incomplete\"}}\n\n",
    )
    .into()
}

fn gemini_incomplete_stream() -> Vec<u8> {
    concat!(
        "data: {\"candidates\":[{\"content\":{\"parts\":[{\"text\":\"hello\"}]}}]}\n\n",
        "data: {\"candidates\":[{\"finishReason\":\"MAX_TOKENS\"}],",
        "\"usageMetadata\":{\"promptTokenCount\":10,\"candidatesTokenCount\":4,",
        "\"totalTokenCount\":14}}\n\n",
    )
    .into()
}

// ---------------------------------------------------------------------------
// Deterministic local providers: finite stubs + scripted SSE streams
// ---------------------------------------------------------------------------

async fn read_http_request(socket: &mut tokio::net::TcpStream) -> Vec<u8> {
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
    request
}

/// Minimal finite stub for pre-handoff error-status tests.
struct FiniteStub {
    port: u16,
    request_count: Arc<AtomicUsize>,
    task: Option<tokio::task::JoinHandle<()>>,
}

type PlannedFiniteResponse = (u16, Vec<(String, String)>, Vec<u8>);

impl FiniteStub {
    fn start(responses: Vec<PlannedFiniteResponse>) -> Self {
        let listener = std::net::TcpListener::bind(("127.0.0.1", 0)).expect("fixture listener");
        listener.set_nonblocking(true).expect("fixture nonblocking");
        let port = listener.local_addr().expect("fixture address").port();
        let listener = tokio::net::TcpListener::from_std(listener).expect("tokio fixture listener");
        let request_count = Arc::new(AtomicUsize::new(0));
        let counted = Arc::clone(&request_count);
        let expected = responses.len().max(1);
        let task = tokio::spawn(async move {
            for _ in 0..expected {
                let Ok((mut socket, _)) = listener.accept().await else {
                    return;
                };
                let request = read_http_request(&mut socket).await;
                let _ = request;
                let index = counted.fetch_add(1, Ordering::SeqCst);
                let (status, headers, body) = &responses[index.min(responses.len() - 1)];
                let reason = match status {
                    200 => "OK",
                    400 => "Bad Request",
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

#[derive(Clone, Copy, PartialEq, Eq)]
enum Framing {
    /// `transfer-encoding: chunked` with framed chunks.
    Chunked,
    /// Raw bytes until connection close (clean EOF by close).
    Raw,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Finish {
    /// Proper terminal framing (zero chunk) or clean close.
    CleanEof,
    /// Partial chunk bytes then an abrupt close (transport error).
    Abort,
}

struct SseScript {
    header_delay: Duration,
    extra_headers: Vec<(String, String)>,
    framing: Framing,
    chunks: Vec<(Duration, Vec<u8>)>,
    finish: Finish,
}

impl SseScript {
    fn sse(body: Vec<u8>) -> Self {
        Self {
            header_delay: Duration::ZERO,
            extra_headers: vec![("content-type".to_owned(), "text/event-stream".to_owned())],
            framing: Framing::Raw,
            chunks: vec![(Duration::ZERO, body)],
            finish: Finish::CleanEof,
        }
    }
}

/// Scripted SSE provider serving one scripted connection per script entry.
struct StreamingProvider {
    port: u16,
    request_count: Arc<AtomicUsize>,
    task: Option<tokio::task::JoinHandle<()>>,
}

impl StreamingProvider {
    fn start(scripts: Vec<SseScript>) -> Self {
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
                let _ = read_http_request(&mut socket).await;
                let index = counted.fetch_add(1, Ordering::SeqCst);
                let script = &scripts[index.min(scripts.len() - 1)];
                if !script.header_delay.is_zero() {
                    sleep(script.header_delay).await;
                }
                let mut head = String::from("HTTP/1.1 200 OK\r\n");
                for (name, value) in &script.extra_headers {
                    head.push_str(&format!("{name}: {value}\r\n"));
                }
                if script.framing == Framing::Chunked {
                    head.push_str("transfer-encoding: chunked\r\n");
                }
                head.push_str("connection: close\r\n\r\n");
                if socket.write_all(head.as_bytes()).await.is_err() {
                    continue;
                }
                for (delay, bytes) in &script.chunks {
                    if !delay.is_zero() {
                        sleep(*delay).await;
                    }
                    let framed = match script.framing {
                        Framing::Chunked => {
                            let mut framed = format!("{:x}\r\n", bytes.len()).into_bytes();
                            framed.extend_from_slice(bytes);
                            framed.extend_from_slice(b"\r\n");
                            framed
                        }
                        Framing::Raw => bytes.clone(),
                    };
                    if socket.write_all(&framed).await.is_err() {
                        break;
                    }
                }
                match (script.framing, script.finish) {
                    (Framing::Chunked, Finish::CleanEof) => {
                        let _ = socket.write_all(b"0\r\n\r\n").await;
                    }
                    (Framing::Chunked, Finish::Abort) => {
                        let _ = socket.write_all(b"9\r\nabc").await;
                    }
                    (Framing::Raw, _) => {}
                }
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
// Coordinator fixture
// ---------------------------------------------------------------------------

struct ProviderSpec {
    name: String,
    provider_id: String,
    account_id: i64,
    base_url: String,
    surfaces: Vec<WireSurface>,
}

#[derive(Clone)]
struct StreamOptions {
    first_byte_timeout_s: Option<f64>,
    idle_timeout_s: Option<f64>,
    completion_policy: String,
}

impl Default for StreamOptions {
    fn default() -> Self {
        Self {
            first_byte_timeout_s: None,
            idle_timeout_s: None,
            completion_policy: "strict".to_owned(),
        }
    }
}

struct Fixture {
    database: Database,
    router: RoutingRouter,
    estimator: QuotaEstimator,
    coordinator: StreamingCoordinator,
    supervisor: FinalizationSupervisor,
}

async fn build_fixture(
    client_protocol: &str,
    specs: &[ProviderSpec],
    retry_policy: RetryPolicy,
    stream: &StreamOptions,
) -> Fixture {
    let database = Database::open(DatabaseConfig::default())
        .await
        .expect("database opens");
    MigrationRunner::new(&database)
        .run()
        .await
        .expect("migrations apply");
    database
        .call({
            let specs: Vec<(i64, String, String)> = specs
                .iter()
                .map(|spec| (spec.account_id, spec.name.clone(), spec.provider_id.clone()))
                .collect();
            let client_protocol = client_protocol.to_owned();
            move |connection| {
                for (account_id, name, provider_id) in &specs {
                    connection.execute(
                        "INSERT INTO accounts (id, name, api_key_env, enabled, provider_id)
                         VALUES (?1, ?2, 'UNUSED', 1, ?3)",
                        tokio_rusqlite::rusqlite::params![account_id, name, provider_id],
                    )?;
                }
                connection.execute(
                    "INSERT INTO models (model_id, protocol, provider_id, resolution_status)
                     VALUES (?1, ?2, ?3, 'resolved')",
                    tokio_rusqlite::rusqlite::params![MODEL, client_protocol, specs[0].2],
                )?;
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
            stream_completion_policy: stream.completion_policy.clone(),
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
    config.validate().expect("fixture config validates");
    let registry = AccountRegistry::from_config(&config, &accounts, &CredentialStore::default())
        .expect("registry builds");
    for spec in specs {
        let mut model = ModelInput::new(MODEL);
        model.protocol = Some(client_protocol.to_owned());
        model.protocol_source = Some("fixture".to_owned());
        model.resolution_status = ProtocolResolutionStatus::Resolved;
        catalog
            .update_from_account(&spec.name, &spec.provider_id, &[model], true, true)
            .expect("catalog model");
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
    let coordinator = StreamingCoordinator::new(
        router.clone(),
        publication,
        attempts,
        wire,
        WireResolver::new(WireResolverConfig {
            min_negotiation_interval: Duration::ZERO,
            ..Default::default()
        }),
        provider_profiles,
        providers,
        CredentialStore::default(),
        supervisor.clone(),
        retry_policy,
    );
    Fixture {
        database,
        router,
        estimator,
        coordinator,
        supervisor,
    }
}

fn stream_request(client: ClientSurface, proxy_id: &str) -> StreamRequest {
    StreamRequest::new(
        proxy_id,
        stream_client_body(client),
        HeaderMap::new(),
        client,
        StaticRoutingFacts {
            known_provider_ids: BTreeSet::new(),
            requested_protocol: None,
            transcode_protocols: Vec::new(),
            catalog_stale_after_s: None,
            capability_policy: BTreeMap::new(),
            now: 0,
        },
    )
    .expect("admission succeeds")
}

async fn db_request_row(
    database: &Database,
    proxy_request_id: &str,
) -> (String, Option<i64>, Option<i64>, i64, i64) {
    database
        .call({
            let proxy_request_id = proxy_request_id.to_owned();
            move |connection| {
                connection.query_row(
                    "SELECT status, input_tokens, output_tokens, bytes_received,
                            bytes_emitted
                     FROM requests WHERE proxy_request_id = ?1",
                    [proxy_request_id],
                    |row| {
                        Ok((
                            row.get::<_, String>(0)?,
                            row.get::<_, Option<i64>>(1)?,
                            row.get::<_, Option<i64>>(2)?,
                            row.get::<_, i64>(3)?,
                            row.get::<_, i64>(4)?,
                        ))
                    },
                )
            }
        })
        .await
        .expect("request row reads")
}

async fn count_rows(database: &Database) -> (i64, i64, i64) {
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

async fn attempt_rows(database: &Database) -> i64 {
    database
        .call(|connection| {
            connection.query_row("SELECT COUNT(*) FROM request_attempts", [], |row| {
                row.get(0)
            })
        })
        .await
        .expect("attempt count")
}

/// Pull a stream to its terminal: clean `None` or the first chunk error.
async fn drain_stream(
    execution: &mut StreamingExecution,
) -> (Vec<Bytes>, Option<StreamChunkError>) {
    let mut chunks = Vec::new();
    loop {
        match execution.next_chunk().await {
            Some(Ok(bytes)) => chunks.push(bytes),
            Some(Err(error)) => return (chunks, Some(error)),
            None => return (chunks, None),
        }
    }
}

async fn settle(supervisor: &FinalizationSupervisor) {
    sleep(Duration::from_millis(300)).await;
    supervisor.drain().await;
}

// ---------------------------------------------------------------------------
// Success matrix: every public client surface x all five upstream profiles
// ---------------------------------------------------------------------------

#[tokio::test]
async fn stream_success_matrix_covers_every_client_surface_and_upstream_profile() {
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
            let body = upstream_success_stream(upstream);
            // Split mid-frame to prove incremental framing: no complete SSE
            // record is available until the second TCP write arrives.
            let split = body.len() / 2;
            let server = StreamingProvider::start(vec![SseScript {
                header_delay: Duration::ZERO,
                extra_headers: vec![("content-type".to_owned(), "text/event-stream".to_owned())],
                framing: Framing::Raw,
                chunks: vec![
                    (Duration::ZERO, body[..split].to_vec()),
                    (Duration::from_millis(50), body[split..].to_vec()),
                ],
                finish: Finish::CleanEof,
            }]);
            let fixture = build_fixture(
                client.protocol(),
                &[ProviderSpec {
                    name: "account-a".to_owned(),
                    provider_id: "provider-a".to_owned(),
                    account_id: 1,
                    base_url: server.base_url(),
                    surfaces: vec![upstream],
                }],
                RetryPolicy::default(),
                &StreamOptions::default(),
            )
            .await;
            let proxy_id = format!("proxy-{}-{}", client.as_str(), upstream.as_str());
            let mut execution = fixture
                .coordinator
                .execute(stream_request(client, &proxy_id))
                .await
                .unwrap_or_else(|error| panic!("{client:?} x {upstream:?} executes: {error:?}"));
            assert!(execution.is_stream(), "{client:?} x {upstream:?}");
            assert_eq!(execution.headers.status, StatusCode::OK);
            assert_eq!(execution.phase(), StreamPhase::DownstreamPending);
            let names: Vec<String> = execution
                .headers
                .headers
                .iter()
                .map(|(name, _)| name.as_str().to_owned())
                .collect();
            assert!(names.contains(&"x-proxy-request-id".to_owned()));
            assert!(names.contains(&"x-proxy-attempt-count".to_owned()));
            assert!(!execution.handoff_started());
            execution.mark_started();
            assert!(execution.handoff_started());
            assert_eq!(execution.phase(), StreamPhase::Streaming);
            let (chunks, error) = drain_stream(&mut execution).await;
            assert_eq!(error, None, "{client:?} x {upstream:?} ends cleanly");
            // One returned chunk per provider pull (each pull's canonical
            // events concatenate); the mid-frame split above already proves
            // incremental framing across TCP writes, and non-buffering is
            // proven by `stream_first_chunk_arrives_before_eof`.
            assert!(
                !chunks.is_empty(),
                "{client:?} x {upstream:?} forwards client bytes"
            );
            let joined: Vec<u8> = chunks.iter().flat_map(|chunk| chunk.to_vec()).collect();
            let text = String::from_utf8_lossy(&joined);
            assert!(
                text.contains(client_terminal_marker(client)),
                "{client:?} x {upstream:?} ends with its terminal marker: {text}"
            );
            assert!(execution.transport_released(), "body released at terminal");
            assert_eq!(execution.phase(), StreamPhase::Closed);
            let result = execution
                .complete(DownstreamResult::Delivered)
                .await
                .expect("completion converges");
            assert!(result.progress.completed, "{client:?} x {upstream:?}");
            assert_eq!(server.count(), 1, "exactly one upstream attempt");
            let (status, input, output, bytes_in, bytes_out) =
                db_request_row(&fixture.database, &proxy_id).await;
            assert_eq!(status, "completed", "{client:?} x {upstream:?}");
            assert_eq!((input, output), (Some(10), Some(4)));
            assert!(bytes_in > 0 && bytes_out > 0);
            let (requests, terminal_attempts, active) = count_rows(&fixture.database).await;
            assert_eq!((requests, terminal_attempts, active), (1, 1, 0));
            assert_eq!(fixture.router.active_request_count("account-a"), 0);
            assert_eq!(
                fixture
                    .coordinator
                    .diagnostic_count(OUTCOME_COMPLETED_CANONICAL),
                1
            );
            server.join().await;
            fixture.database.close().await.expect("database closes");
        }
    }
}

// ---------------------------------------------------------------------------
// Response-header timeout: retryable before handoff, terminal when exhausted
// ---------------------------------------------------------------------------

#[tokio::test]
async fn stream_header_timeout_fails_over_before_handoff() {
    let slow = StreamingProvider::start(vec![SseScript {
        header_delay: Duration::from_secs(5),
        extra_headers: vec![("content-type".to_owned(), "text/event-stream".to_owned())],
        framing: Framing::Raw,
        chunks: vec![(
            Duration::ZERO,
            upstream_success_stream(WireSurface::OpenaiChatCompletions),
        )],
        finish: Finish::CleanEof,
    }]);
    let fast = StreamingProvider::start(vec![SseScript::sse(upstream_success_stream(
        WireSurface::OpenaiChatCompletions,
    ))]);
    let mut fixture = build_fixture(
        "openai",
        &[
            ProviderSpec {
                name: "account-a".to_owned(),
                provider_id: "provider-a".to_owned(),
                account_id: 1,
                base_url: slow.base_url(),
                surfaces: vec![WireSurface::OpenaiChatCompletions],
            },
            ProviderSpec {
                name: "account-b".to_owned(),
                provider_id: "provider-b".to_owned(),
                account_id: 2,
                base_url: fast.base_url(),
                surfaces: vec![WireSurface::OpenaiChatCompletions],
            },
        ],
        RetryPolicy::default(),
        &StreamOptions::default(),
    )
    .await;
    fixture.coordinator = fixture
        .coordinator
        .with_header_timeout_override(Some(Duration::from_millis(150)));
    let proxy_id = "proxy-header-timeout-failover";
    let mut execution = fixture
        .coordinator
        .execute(stream_request(ClientSurface::ChatCompletions, proxy_id))
        .await
        .expect("failover executes");
    assert!(execution.is_stream());
    assert_eq!(slow.count(), 1, "first attempt hits the slow account");
    assert_eq!(fast.count(), 1, "header timeout fails over");
    execution.mark_started();
    let (chunks, error) = drain_stream(&mut execution).await;
    assert_eq!(error, None);
    assert!(!chunks.is_empty());
    let result = execution
        .complete(DownstreamResult::Delivered)
        .await
        .expect("failover completion converges");
    assert!(result.progress.completed);
    assert_eq!(slow.count(), 1);
    assert_eq!(fast.count(), 1);
    assert_eq!(
        fixture
            .coordinator
            .diagnostic_count(OUTCOME_RESPONSE_HEADER_TIMEOUT),
        1
    );
    let (status, _, _, _, _) = db_request_row(&fixture.database, proxy_id).await;
    assert_eq!(status, "completed");
    // Both attempts reached their required cleanup boundary: the timed-out
    // attempt converged before replacement ownership.
    assert_eq!(attempt_rows(&fixture.database).await, 2);
    let (_, terminal_attempts, active) = count_rows(&fixture.database).await;
    assert_eq!(terminal_attempts, 2);
    assert_eq!(active, 0);
    slow.join().await;
    fast.join().await;
    fixture.database.close().await.expect("database closes");
}

#[tokio::test]
async fn stream_header_timeout_without_failover_is_terminal() {
    let slow = StreamingProvider::start(vec![SseScript {
        header_delay: Duration::from_secs(5),
        extra_headers: vec![("content-type".to_owned(), "text/event-stream".to_owned())],
        framing: Framing::Raw,
        chunks: vec![(
            Duration::ZERO,
            upstream_success_stream(WireSurface::OpenaiChatCompletions),
        )],
        finish: Finish::CleanEof,
    }]);
    let mut fixture = build_fixture(
        "anthropic",
        &[ProviderSpec {
            name: "account-a".to_owned(),
            provider_id: "provider-a".to_owned(),
            account_id: 1,
            base_url: slow.base_url(),
            surfaces: vec![WireSurface::OpenaiChatCompletions],
        }],
        RetryPolicy::default(),
        &StreamOptions::default(),
    )
    .await;
    fixture.coordinator = fixture
        .coordinator
        .with_header_timeout_override(Some(Duration::from_millis(150)));
    let proxy_id = "proxy-header-timeout-terminal";
    let execution = fixture
        .coordinator
        .execute(stream_request(ClientSurface::Messages, proxy_id))
        .await
        .expect("exhaustion executes terminally");
    assert!(!execution.is_stream(), "no stream without upstream headers");
    // Exhaustion parity with the finite coordinator: a retryable pre-handoff
    // failure with no eligible account left and no upstream response
    // converges to the synthetic exhaustion envelope, not the timeout shape.
    assert_eq!(execution.headers.status, StatusCode::SERVICE_UNAVAILABLE);
    let body = execution.error_body.clone().expect("terminal envelope");
    let envelope = String::from_utf8_lossy(&body);
    assert!(
        envelope.contains("api_error"),
        "Messages clients keep their error shape: {envelope}"
    );
    assert_eq!(slow.count(), 1, "no retry without an eligible account");
    assert_eq!(
        fixture
            .coordinator
            .diagnostic_count(OUTCOME_RESPONSE_HEADER_TIMEOUT),
        1
    );
    execution.mark_started();
    let result = execution
        .complete(DownstreamResult::Delivered)
        .await
        .expect("terminal completion converges");
    assert!(result.progress.completed);
    let (status, _, _, _, _) = db_request_row(&fixture.database, proxy_id).await;
    assert_eq!(status, "error");
    let (_, terminal_attempts, active) = count_rows(&fixture.database).await;
    assert_eq!(terminal_attempts, 1);
    assert_eq!(active, 0);
    slow.join().await;
    fixture.database.close().await.expect("database closes");
}

// ---------------------------------------------------------------------------
// First-byte timeout: headers accepted, no body byte, still pre-handoff
// ---------------------------------------------------------------------------

#[tokio::test]
async fn stream_first_byte_timeout_fails_over_before_handoff() {
    let stalled = StreamingProvider::start(vec![SseScript {
        header_delay: Duration::ZERO,
        extra_headers: vec![("content-type".to_owned(), "text/event-stream".to_owned())],
        framing: Framing::Raw,
        chunks: vec![(
            Duration::from_secs(5),
            upstream_success_stream(WireSurface::OpenaiChatCompletions),
        )],
        finish: Finish::CleanEof,
    }]);
    let fast = StreamingProvider::start(vec![SseScript::sse(upstream_success_stream(
        WireSurface::OpenaiChatCompletions,
    ))]);
    let mut fixture = build_fixture(
        "openai",
        &[
            ProviderSpec {
                name: "account-a".to_owned(),
                provider_id: "provider-a".to_owned(),
                account_id: 1,
                base_url: stalled.base_url(),
                surfaces: vec![WireSurface::OpenaiChatCompletions],
            },
            ProviderSpec {
                name: "account-b".to_owned(),
                provider_id: "provider-b".to_owned(),
                account_id: 2,
                base_url: fast.base_url(),
                surfaces: vec![WireSurface::OpenaiChatCompletions],
            },
        ],
        RetryPolicy::default(),
        &StreamOptions::default(),
    )
    .await;
    fixture.coordinator = fixture
        .coordinator
        .with_first_byte_timeout_override(Some(Duration::from_millis(150)));
    let proxy_id = "proxy-first-byte-failover";
    let mut execution = fixture
        .coordinator
        .execute(stream_request(ClientSurface::ChatCompletions, proxy_id))
        .await
        .expect("failover executes");
    assert!(execution.is_stream());
    assert_eq!(stalled.count(), 1);
    assert_eq!(fast.count(), 1, "first-byte timeout fails over");
    execution.mark_started();
    let (_, error) = drain_stream(&mut execution).await;
    assert_eq!(error, None);
    let result = execution
        .complete(DownstreamResult::Delivered)
        .await
        .expect("failover completion converges");
    assert!(result.progress.completed);
    assert_eq!(
        fixture
            .coordinator
            .diagnostic_count(OUTCOME_FIRST_BYTE_TIMEOUT),
        1
    );
    assert_eq!(attempt_rows(&fixture.database).await, 2);
    stalled.join().await;
    fast.join().await;
    fixture.database.close().await.expect("database closes");
}

// ---------------------------------------------------------------------------
// Active streams with no idle policy must not time out; idle stalls are
// terminal midstream failures with no post-handoff retry
// ---------------------------------------------------------------------------

#[tokio::test]
async fn stream_active_flow_with_no_idle_timeout_succeeds() {
    let paced = upstream_success_stream(WireSurface::OpenaiChatCompletions);
    let third = paced.len() / 3;
    let server = StreamingProvider::start(vec![SseScript {
        header_delay: Duration::ZERO,
        extra_headers: vec![("content-type".to_owned(), "text/event-stream".to_owned())],
        framing: Framing::Raw,
        chunks: vec![
            (Duration::ZERO, paced[..third].to_vec()),
            (Duration::from_millis(150), paced[third..2 * third].to_vec()),
            (Duration::from_millis(150), paced[2 * third..].to_vec()),
        ],
        finish: Finish::CleanEof,
    }]);
    let fixture = build_fixture(
        "openai",
        &[ProviderSpec {
            name: "account-a".to_owned(),
            provider_id: "provider-a".to_owned(),
            account_id: 1,
            base_url: server.base_url(),
            surfaces: vec![WireSurface::OpenaiChatCompletions],
        }],
        RetryPolicy::default(),
        &StreamOptions::default(),
    )
    .await;
    let proxy_id = "proxy-paced-no-idle";
    let mut execution = fixture
        .coordinator
        .execute(stream_request(ClientSurface::ChatCompletions, proxy_id))
        .await
        .expect("paced stream executes");
    execution.mark_started();
    let (chunks, error) = drain_stream(&mut execution).await;
    assert_eq!(error, None, "no idle policy means no idle timeout");
    assert!(chunks.len() >= 2);
    let result = execution
        .complete(DownstreamResult::Delivered)
        .await
        .expect("paced completion converges");
    assert!(result.progress.completed);
    assert_eq!(
        fixture
            .coordinator
            .diagnostic_count(OUTCOME_COMPLETED_CANONICAL),
        1
    );
    assert_eq!(
        fixture.coordinator.diagnostic_count(OUTCOME_IDLE_TIMEOUT),
        0
    );
    server.join().await;
    fixture.database.close().await.expect("database closes");
}

#[tokio::test]
async fn stream_idle_timeout_is_terminal_without_retry() {
    let stalled = StreamingProvider::start(vec![SseScript {
        header_delay: Duration::ZERO,
        extra_headers: vec![("content-type".to_owned(), "text/event-stream".to_owned())],
        framing: Framing::Raw,
        chunks: vec![
            (
                Duration::ZERO,
                b"data: {\"choices\":[{\"delta\":{\"content\":\"hi\"}}]}\n\n".to_vec(),
            ),
            (Duration::from_secs(5), b"data: [DONE]\n\n".to_vec()),
        ],
        finish: Finish::CleanEof,
    }]);
    // A second eligible account proves the post-handoff no-retry boundary.
    let standby = StreamingProvider::start(vec![SseScript::sse(upstream_success_stream(
        WireSurface::OpenaiChatCompletions,
    ))]);
    let mut fixture = build_fixture(
        "openai",
        &[
            ProviderSpec {
                name: "account-a".to_owned(),
                provider_id: "provider-a".to_owned(),
                account_id: 1,
                base_url: stalled.base_url(),
                surfaces: vec![WireSurface::OpenaiChatCompletions],
            },
            ProviderSpec {
                name: "account-b".to_owned(),
                provider_id: "provider-b".to_owned(),
                account_id: 2,
                base_url: standby.base_url(),
                surfaces: vec![WireSurface::OpenaiChatCompletions],
            },
        ],
        RetryPolicy::default(),
        &StreamOptions::default(),
    )
    .await;
    fixture.coordinator = fixture
        .coordinator
        .with_idle_timeout_override(Some(Duration::from_millis(150)));
    let proxy_id = "proxy-idle-timeout";
    let mut execution = fixture
        .coordinator
        .execute(stream_request(ClientSurface::ChatCompletions, proxy_id))
        .await
        .expect("stalled stream executes");
    execution.mark_started();
    let (chunks, error) = drain_stream(&mut execution).await;
    assert_eq!(chunks.len(), 1, "first chunk forwards before the stall");
    assert_eq!(error, Some(StreamChunkError::IdleTimeout));
    assert!(execution.transport_released());
    let result = execution
        .complete(DownstreamResult::Delivered)
        .await
        .expect("idle completion converges");
    assert!(result.progress.completed);
    assert_eq!(stalled.count(), 1);
    assert_eq!(
        standby.count(),
        0,
        "post-handoff idle timeout never replays upstream"
    );
    assert_eq!(
        fixture.coordinator.diagnostic_count(OUTCOME_IDLE_TIMEOUT),
        1
    );
    assert_eq!(
        fixture
            .coordinator
            .diagnostic_count(OUTCOME_UPSTREAM_MIDSTREAM_ERROR),
        1
    );
    let (status, _, _, _, _) = db_request_row(&fixture.database, proxy_id).await;
    assert_eq!(status, "error");
    let (_, terminal_attempts, active) = count_rows(&fixture.database).await;
    assert_eq!(terminal_attempts, 1);
    assert_eq!(active, 0);
    stalled.join().await;
    standby.join().await;
    fixture.database.close().await.expect("database closes");
}

// ---------------------------------------------------------------------------
// EOF classification: complete, compatibility, empty, partial, malformed
// ---------------------------------------------------------------------------

#[tokio::test]
async fn stream_compatibility_eof_succeeds_when_policy_allows() {
    let server = StreamingProvider::start(vec![SseScript::sse(usage_without_terminal())]);
    let fixture = build_fixture(
        "openai",
        &[ProviderSpec {
            name: "account-a".to_owned(),
            provider_id: "provider-a".to_owned(),
            account_id: 1,
            base_url: server.base_url(),
            surfaces: vec![WireSurface::OpenaiChatCompletions],
        }],
        RetryPolicy::default(),
        &StreamOptions {
            completion_policy: "compatible".to_owned(),
            ..StreamOptions::default()
        },
    )
    .await;
    let proxy_id = "proxy-compat-eof";
    let mut execution = fixture
        .coordinator
        .execute(stream_request(ClientSurface::ChatCompletions, proxy_id))
        .await
        .expect("compat stream executes");
    execution.mark_started();
    let (chunks, error) = drain_stream(&mut execution).await;
    assert_eq!(
        error, None,
        "usage-complete EOF is success under compatible"
    );
    assert!(!chunks.is_empty());
    let result = execution
        .complete(DownstreamResult::Delivered)
        .await
        .expect("compat completion converges");
    assert!(result.progress.completed);
    assert_eq!(
        fixture
            .coordinator
            .diagnostic_count(OUTCOME_COMPLETED_COMPATIBILITY),
        1
    );
    let (status, input, output, _, _) = db_request_row(&fixture.database, proxy_id).await;
    assert_eq!(status, "completed");
    assert_eq!((input, output), (Some(10), Some(4)));
    server.join().await;
    fixture.database.close().await.expect("database closes");
}

#[tokio::test]
async fn stream_strict_policy_rejects_usage_only_eof() {
    let server = StreamingProvider::start(vec![SseScript::sse(usage_without_terminal())]);
    let fixture = build_fixture(
        "openai",
        &[ProviderSpec {
            name: "account-a".to_owned(),
            provider_id: "provider-a".to_owned(),
            account_id: 1,
            base_url: server.base_url(),
            surfaces: vec![WireSurface::OpenaiChatCompletions],
        }],
        RetryPolicy::default(),
        &StreamOptions::default(),
    )
    .await;
    let proxy_id = "proxy-strict-eof";
    let mut execution = fixture
        .coordinator
        .execute(stream_request(ClientSurface::ChatCompletions, proxy_id))
        .await
        .expect("strict stream executes");
    // Downstream never starts: EOF classification must report before-body.
    let (chunks, error) = drain_stream(&mut execution).await;
    assert!(!chunks.is_empty(), "payload still forwards before EOF");
    assert_eq!(error, Some(StreamChunkError::PrematureEof));
    let result = execution
        .complete(DownstreamResult::Delivered)
        .await
        .expect("strict completion converges");
    assert!(result.progress.completed);
    assert_eq!(
        fixture
            .coordinator
            .diagnostic_count(OUTCOME_PREMATURE_EOF_BEFORE_BODY),
        1
    );
    let (status, _, _, _, _) = db_request_row(&fixture.database, proxy_id).await;
    assert_eq!(status, "error");
    assert_eq!(server.count(), 1, "EOF failure never retries");
    server.join().await;
    fixture.database.close().await.expect("database closes");
}

#[tokio::test]
async fn stream_empty_eof_is_terminal() {
    let server = StreamingProvider::start(vec![SseScript {
        header_delay: Duration::ZERO,
        extra_headers: vec![("content-type".to_owned(), "text/event-stream".to_owned())],
        framing: Framing::Chunked,
        chunks: vec![],
        finish: Finish::CleanEof,
    }]);
    let fixture = build_fixture(
        "openai",
        &[ProviderSpec {
            name: "account-a".to_owned(),
            provider_id: "provider-a".to_owned(),
            account_id: 1,
            base_url: server.base_url(),
            surfaces: vec![WireSurface::OpenaiChatCompletions],
        }],
        RetryPolicy::default(),
        &StreamOptions::default(),
    )
    .await;
    let proxy_id = "proxy-empty-eof";
    let mut execution = fixture
        .coordinator
        .execute(stream_request(ClientSurface::ChatCompletions, proxy_id))
        .await
        .expect("empty stream executes");
    execution.mark_started();
    let (chunks, error) = drain_stream(&mut execution).await;
    assert!(chunks.is_empty());
    assert_eq!(error, Some(StreamChunkError::EmptyEof));
    let result = execution
        .complete(DownstreamResult::Delivered)
        .await
        .expect("empty completion converges");
    assert!(result.progress.completed);
    assert_eq!(fixture.coordinator.diagnostic_count(OUTCOME_EMPTY_EOF), 1);
    let (status, _, _, _, _) = db_request_row(&fixture.database, proxy_id).await;
    assert_eq!(status, "error");
    server.join().await;
    fixture.database.close().await.expect("database closes");
}

#[tokio::test]
async fn stream_partial_eof_after_start_is_midstream() {
    let server = StreamingProvider::start(vec![SseScript::sse(
        b"data: {\"choices\":[{\"delta\":{\"content\":\"hi\"}}]}\n\n".to_vec(),
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
        RetryPolicy::default(),
        &StreamOptions::default(),
    )
    .await;
    let proxy_id = "proxy-partial-eof";
    let mut execution = fixture
        .coordinator
        .execute(stream_request(ClientSurface::ChatCompletions, proxy_id))
        .await
        .expect("partial stream executes");
    execution.mark_started();
    let (chunks, error) = drain_stream(&mut execution).await;
    assert_eq!(chunks.len(), 1);
    assert_eq!(error, Some(StreamChunkError::PrematureEof));
    let result = execution
        .complete(DownstreamResult::Delivered)
        .await
        .expect("partial completion converges");
    assert!(result.progress.completed);
    assert_eq!(
        fixture
            .coordinator
            .diagnostic_count(OUTCOME_PREMATURE_EOF_MIDSTREAM),
        1
    );
    let (status, _, _, _, _) = db_request_row(&fixture.database, proxy_id).await;
    assert_eq!(status, "error");
    server.join().await;
    fixture.database.close().await.expect("database closes");
}

#[tokio::test]
async fn stream_malformed_sse_is_terminal_not_success() {
    let server = StreamingProvider::start(vec![SseScript {
        header_delay: Duration::ZERO,
        extra_headers: vec![("content-type".to_owned(), "text/event-stream".to_owned())],
        framing: Framing::Raw,
        chunks: vec![
            (Duration::ZERO, b"data: not-json{{{\n\n".to_vec()),
            (Duration::from_millis(20), b"data: [DONE]\n\n".to_vec()),
        ],
        finish: Finish::CleanEof,
    }]);
    let fixture = build_fixture(
        "openai",
        &[ProviderSpec {
            name: "account-a".to_owned(),
            provider_id: "provider-a".to_owned(),
            account_id: 1,
            base_url: server.base_url(),
            surfaces: vec![WireSurface::OpenaiChatCompletions],
        }],
        RetryPolicy::default(),
        &StreamOptions::default(),
    )
    .await;
    let proxy_id = "proxy-malformed-eof";
    let mut execution = fixture
        .coordinator
        .execute(stream_request(ClientSurface::ChatCompletions, proxy_id))
        .await
        .expect("malformed stream executes");
    execution.mark_started();
    let (_, error) = drain_stream(&mut execution).await;
    // A skipped malformed chunk poisons an otherwise terminal stream: EOF
    // can never become false success.
    assert_eq!(error, Some(StreamChunkError::MalformedEof));
    let result = execution
        .complete(DownstreamResult::Delivered)
        .await
        .expect("malformed completion converges");
    assert!(result.progress.completed);
    assert_eq!(
        fixture.coordinator.diagnostic_count(OUTCOME_MALFORMED_EOF),
        1
    );
    let (status, _, _, _, _) = db_request_row(&fixture.database, proxy_id).await;
    assert_eq!(status, "error");
    assert_eq!(server.count(), 1);
    server.join().await;
    fixture.database.close().await.expect("database closes");
}

#[tokio::test]
async fn stream_invalid_utf8_is_terminal_not_success() {
    let server = StreamingProvider::start(vec![SseScript {
        header_delay: Duration::ZERO,
        extra_headers: vec![("content-type".to_owned(), "text/event-stream".to_owned())],
        framing: Framing::Raw,
        chunks: vec![(Duration::ZERO, b"data: \xff\xfe broken\n\n".to_vec())],
        finish: Finish::CleanEof,
    }]);
    let fixture = build_fixture(
        "openai",
        &[ProviderSpec {
            name: "account-a".to_owned(),
            provider_id: "provider-a".to_owned(),
            account_id: 1,
            base_url: server.base_url(),
            surfaces: vec![WireSurface::OpenaiChatCompletions],
        }],
        RetryPolicy::default(),
        &StreamOptions::default(),
    )
    .await;
    let proxy_id = "proxy-invalid-utf8";
    let mut execution = fixture
        .coordinator
        .execute(stream_request(ClientSurface::ChatCompletions, proxy_id))
        .await
        .expect("utf8 stream executes");
    execution.mark_started();
    let (_, error) = drain_stream(&mut execution).await;
    assert!(
        matches!(
            error,
            Some(StreamChunkError::PrematureEof) | Some(StreamChunkError::MalformedEof)
        ),
        "undecodable bytes are terminal, never success: {error:?}"
    );
    let result = execution
        .complete(DownstreamResult::Delivered)
        .await
        .expect("utf8 completion converges");
    assert!(result.progress.completed);
    let (status, _, _, _, _) = db_request_row(&fixture.database, proxy_id).await;
    assert_eq!(status, "error");
    server.join().await;
    fixture.database.close().await.expect("database closes");
}

// ---------------------------------------------------------------------------
// Provider terminal events: Responses failed/incomplete, Gemini incomplete
// ---------------------------------------------------------------------------

#[tokio::test]
async fn stream_responses_failed_is_forwarded_terminal() {
    let server = StreamingProvider::start(vec![SseScript::sse(responses_failed_stream())]);
    let fixture = build_fixture(
        "openai",
        &[ProviderSpec {
            name: "account-a".to_owned(),
            provider_id: "provider-a".to_owned(),
            account_id: 1,
            base_url: server.base_url(),
            surfaces: vec![WireSurface::OpenaiResponses],
        }],
        RetryPolicy::default(),
        &StreamOptions::default(),
    )
    .await;
    let proxy_id = "proxy-responses-failed";
    let mut execution = fixture
        .coordinator
        .execute(stream_request(ClientSurface::Responses, proxy_id))
        .await
        .expect("failed stream executes");
    execution.mark_started();
    let (chunks, error) = drain_stream(&mut execution).await;
    // The provider terminal event was already forwarded downstream, so the
    // stream ends cleanly from the caller's view while finalizing terminal.
    assert_eq!(error, None);
    assert!(!chunks.is_empty());
    assert_eq!(execution.phase(), StreamPhase::Closed);
    let result = execution
        .complete(DownstreamResult::Delivered)
        .await
        .expect("failed completion converges");
    assert!(result.progress.completed);
    assert_eq!(
        fixture
            .coordinator
            .diagnostic_count(OUTCOME_TERMINAL_FAILURE),
        1
    );
    assert_eq!(server.count(), 1, "provider terminal events never retry");
    let (status, _, _, _, _) = db_request_row(&fixture.database, proxy_id).await;
    assert_eq!(status, "error");
    let (_, terminal_attempts, active) = count_rows(&fixture.database).await;
    assert_eq!(terminal_attempts, 1);
    assert_eq!(active, 0);
    server.join().await;
    fixture.database.close().await.expect("database closes");
}

#[tokio::test]
async fn stream_responses_incomplete_is_forwarded_terminal() {
    let server = StreamingProvider::start(vec![SseScript::sse(responses_incomplete_stream())]);
    let fixture = build_fixture(
        "openai",
        &[ProviderSpec {
            name: "account-a".to_owned(),
            provider_id: "provider-a".to_owned(),
            account_id: 1,
            base_url: server.base_url(),
            surfaces: vec![WireSurface::OpenaiResponses],
        }],
        RetryPolicy::default(),
        &StreamOptions::default(),
    )
    .await;
    let proxy_id = "proxy-responses-incomplete";
    let mut execution = fixture
        .coordinator
        .execute(stream_request(ClientSurface::Responses, proxy_id))
        .await
        .expect("incomplete stream executes");
    execution.mark_started();
    // Native terminal evidence is visible mid-stream before EOF.
    let first = execution
        .next_chunk()
        .await
        .expect("first chunk arrives")
        .expect("first chunk decodes");
    assert!(!first.is_empty());
    assert_eq!(execution.phase(), StreamPhase::TerminalEvidence);
    let (_, error) = drain_stream(&mut execution).await;
    assert_eq!(error, None);
    let result = execution
        .complete(DownstreamResult::Delivered)
        .await
        .expect("incomplete completion converges");
    assert!(result.progress.completed);
    assert_eq!(
        fixture
            .coordinator
            .diagnostic_count(OUTCOME_TERMINAL_INCOMPLETE),
        1
    );
    let (status, _, _, _, _) = db_request_row(&fixture.database, proxy_id).await;
    assert_eq!(status, "error");
    server.join().await;
    fixture.database.close().await.expect("database closes");
}

#[tokio::test]
async fn stream_gemini_incomplete_is_terminal() {
    let server = StreamingProvider::start(vec![SseScript::sse(gemini_incomplete_stream())]);
    let fixture = build_fixture(
        "openai",
        &[ProviderSpec {
            name: "account-a".to_owned(),
            provider_id: "provider-a".to_owned(),
            account_id: 1,
            base_url: server.base_url(),
            surfaces: vec![WireSurface::GeminiGenerateContent],
        }],
        RetryPolicy::default(),
        &StreamOptions::default(),
    )
    .await;
    let proxy_id = "proxy-gemini-incomplete";
    let mut execution = fixture
        .coordinator
        .execute(stream_request(ClientSurface::ChatCompletions, proxy_id))
        .await
        .expect("gemini stream executes");
    execution.mark_started();
    let (chunks, error) = drain_stream(&mut execution).await;
    assert!(!chunks.is_empty(), "payload forwards before the terminal");
    assert_eq!(error, None, "provider terminal ends the stream cleanly");
    let result = execution
        .complete(DownstreamResult::Delivered)
        .await
        .expect("gemini completion converges");
    assert!(result.progress.completed);
    assert_eq!(
        fixture
            .coordinator
            .diagnostic_count(OUTCOME_TERMINAL_INCOMPLETE),
        1
    );
    assert_eq!(server.count(), 1, "no retry on provider terminal evidence");
    let (status, _, _, _, _) = db_request_row(&fixture.database, proxy_id).await;
    assert_eq!(status, "error");
    server.join().await;
    fixture.database.close().await.expect("database closes");
}

// ---------------------------------------------------------------------------
// Midstream transport failure: terminal, effects applied once, no retry
// ---------------------------------------------------------------------------

#[tokio::test]
async fn stream_midstream_transport_error_never_retries() {
    let failing = StreamingProvider::start(vec![SseScript {
        header_delay: Duration::ZERO,
        extra_headers: vec![("content-type".to_owned(), "text/event-stream".to_owned())],
        framing: Framing::Chunked,
        chunks: vec![(
            Duration::ZERO,
            b"data: {\"choices\":[{\"delta\":{\"content\":\"hi\"}}]}\n\n".to_vec(),
        )],
        finish: Finish::Abort,
    }]);
    let standby = StreamingProvider::start(vec![SseScript::sse(upstream_success_stream(
        WireSurface::OpenaiChatCompletions,
    ))]);
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
                base_url: standby.base_url(),
                surfaces: vec![WireSurface::OpenaiChatCompletions],
            },
        ],
        RetryPolicy::default(),
        &StreamOptions::default(),
    )
    .await;
    let proxy_id = "proxy-midstream-error";
    let mut execution = fixture
        .coordinator
        .execute(stream_request(ClientSurface::ChatCompletions, proxy_id))
        .await
        .expect("failing stream executes");
    execution.mark_started();
    let (chunks, error) = drain_stream(&mut execution).await;
    assert_eq!(chunks.len(), 1, "first chunk forwards before the reset");
    assert_eq!(error, Some(StreamChunkError::UpstreamTransport));
    assert!(execution.transport_released());
    let result = execution
        .complete(DownstreamResult::Delivered)
        .await
        .expect("midstream completion converges");
    assert!(result.progress.completed);
    assert_eq!(failing.count(), 1);
    assert_eq!(
        standby.count(),
        0,
        "midstream failure never fails over after handoff"
    );
    assert_eq!(
        fixture
            .coordinator
            .diagnostic_count(OUTCOME_UPSTREAM_MIDSTREAM_ERROR),
        1
    );
    let (status, _, _, _, _) = db_request_row(&fixture.database, proxy_id).await;
    assert_eq!(status, "error");
    let (_, terminal_attempts, active) = count_rows(&fixture.database).await;
    assert_eq!(terminal_attempts, 1);
    assert_eq!(active, 0);
    failing.join().await;
    standby.join().await;
    fixture.database.close().await.expect("database closes");
}

// ---------------------------------------------------------------------------
// Pre-handoff error statuses: finite terminal, retry only before handoff
// ---------------------------------------------------------------------------

#[tokio::test]
async fn stream_pre_handoff_provider_error_is_terminal_without_stream() {
    let error_body = br#"{"error":{"message":"bad request","type":"invalid_request_error"}}"#;
    let server = FiniteStub::start(vec![(400, Vec::new(), error_body.to_vec())]);
    let fixture = build_fixture(
        "openai",
        &[ProviderSpec {
            name: "account-a".to_owned(),
            provider_id: "provider-a".to_owned(),
            account_id: 1,
            base_url: server.base_url(),
            surfaces: vec![WireSurface::OpenaiChatCompletions],
        }],
        RetryPolicy::default(),
        &StreamOptions::default(),
    )
    .await;
    let proxy_id = "proxy-stream-400";
    let execution = fixture
        .coordinator
        .execute(stream_request(ClientSurface::ChatCompletions, proxy_id))
        .await
        .expect("terminal error executes");
    assert!(!execution.is_stream());
    assert_eq!(execution.headers.status, StatusCode::BAD_REQUEST);
    assert_eq!(execution.error_body.as_deref(), Some(error_body.as_slice()));
    assert_eq!(server.count(), 1, "non-retryable error never retries");
    execution.mark_started();
    let result = execution
        .complete(DownstreamResult::Delivered)
        .await
        .expect("error completion converges");
    assert!(result.progress.completed);
    let (status, _, _, _, _) = db_request_row(&fixture.database, proxy_id).await;
    assert_eq!(status, "client_error");
    server.join().await;
    fixture.database.close().await.expect("database closes");
}

#[tokio::test]
async fn stream_retryable_status_fails_over_before_handoff() {
    let failing = FiniteStub::start(vec![(
        500,
        Vec::new(),
        br#"{"error":{"message":"boom","type":"server_error"}}"#.to_vec(),
    )]);
    let succeeding = StreamingProvider::start(vec![SseScript::sse(upstream_success_stream(
        WireSurface::OpenaiChatCompletions,
    ))]);
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
        RetryPolicy::default(),
        &StreamOptions::default(),
    )
    .await;
    let proxy_id = "proxy-stream-failover";
    let mut execution = fixture
        .coordinator
        .execute(stream_request(ClientSurface::ChatCompletions, proxy_id))
        .await
        .expect("failover executes");
    assert!(execution.is_stream());
    assert_eq!(failing.count(), 1);
    assert_eq!(succeeding.count(), 1, "retryable status fails over");
    execution.mark_started();
    let (_, error) = drain_stream(&mut execution).await;
    assert_eq!(error, None);
    let result = execution
        .complete(DownstreamResult::Delivered)
        .await
        .expect("failover completion converges");
    assert!(result.progress.completed);
    let (status, _, _, _, _) = db_request_row(&fixture.database, proxy_id).await;
    assert_eq!(status, "completed");
    assert_eq!(attempt_rows(&fixture.database).await, 2);
    let (_, terminal_attempts, active) = count_rows(&fixture.database).await;
    assert_eq!(terminal_attempts, 2);
    assert_eq!(active, 0);
    failing.join().await;
    succeeding.join().await;
    fixture.database.close().await.expect("database closes");
}

#[tokio::test]
async fn stream_exhaustion_passes_through_last_upstream_error() {
    let error_body = br#"{"error":{"message":"boom","type":"server_error"}}"#;
    let server = FiniteStub::start(vec![(500, Vec::new(), error_body.to_vec())]);
    let fixture = build_fixture(
        "openai",
        &[ProviderSpec {
            name: "account-a".to_owned(),
            provider_id: "provider-a".to_owned(),
            account_id: 1,
            base_url: server.base_url(),
            surfaces: vec![WireSurface::OpenaiChatCompletions],
        }],
        RetryPolicy::default(),
        &StreamOptions::default(),
    )
    .await;
    let proxy_id = "proxy-stream-exhausted";
    let execution = fixture
        .coordinator
        .execute(stream_request(ClientSurface::ChatCompletions, proxy_id))
        .await
        .expect("exhaustion executes");
    assert!(!execution.is_stream());
    assert_eq!(execution.headers.status, StatusCode::INTERNAL_SERVER_ERROR);
    assert_eq!(
        execution.error_body.as_deref(),
        Some(error_body.as_slice()),
        "exhaustion preserves the last upstream response"
    );
    assert_eq!(server.count(), 1, "no account remains for a retry");
    execution.mark_started();
    let result = execution
        .complete(DownstreamResult::Delivered)
        .await
        .expect("exhaustion completion converges");
    assert!(result.progress.completed);
    let (status, _, _, _, _) = db_request_row(&fixture.database, proxy_id).await;
    assert_eq!(status, "error");
    let (_, terminal_attempts, active) = count_rows(&fixture.database).await;
    assert_eq!(terminal_attempts, 1);
    assert_eq!(active, 0);
    server.join().await;
    fixture.database.close().await.expect("database closes");
}

// ---------------------------------------------------------------------------
// Cancellation and downstream outcomes at every phase
// ---------------------------------------------------------------------------

#[tokio::test]
async fn stream_cancellation_before_start_finalizes_interrupted() {
    let server = StreamingProvider::start(vec![SseScript::sse(upstream_success_stream(
        WireSurface::OpenaiChatCompletions,
    ))]);
    let fixture = build_fixture(
        "openai",
        &[ProviderSpec {
            name: "account-a".to_owned(),
            provider_id: "provider-a".to_owned(),
            account_id: 1,
            base_url: server.base_url(),
            surfaces: vec![WireSurface::OpenaiChatCompletions],
        }],
        RetryPolicy::default(),
        &StreamOptions::default(),
    )
    .await;
    let proxy_id = "proxy-cancel-before";
    let execution = fixture
        .coordinator
        .execute(stream_request(ClientSurface::ChatCompletions, proxy_id))
        .await
        .expect("executes");
    assert_eq!(server.count(), 1);
    assert!(!execution.handoff_started());
    drop(execution);
    settle(&fixture.supervisor).await;
    assert_eq!(server.count(), 1, "cancelled request never replays");
    assert_eq!(
        fixture
            .coordinator
            .diagnostic_count(OUTCOME_CLIENT_CANCELLED),
        0,
        "pre-handoff cancellation is interrupted, not client-cancelled"
    );
    let (status, _, _, _, _) = db_request_row(&fixture.database, proxy_id).await;
    assert_eq!(status, "error");
    let (_, terminal_attempts, active) = count_rows(&fixture.database).await;
    assert_eq!(terminal_attempts, 1);
    assert_eq!(active, 0);
    assert_eq!(fixture.router.active_request_count("account-a"), 0);
    server.join().await;
    fixture.database.close().await.expect("database closes");
}

#[tokio::test]
async fn stream_cancellation_after_start_finalizes_cancelled() {
    let server = StreamingProvider::start(vec![SseScript {
        header_delay: Duration::ZERO,
        extra_headers: vec![("content-type".to_owned(), "text/event-stream".to_owned())],
        framing: Framing::Raw,
        chunks: vec![
            (
                Duration::ZERO,
                b"data: {\"choices\":[{\"delta\":{\"content\":\"hi\"}}]}\n\n".to_vec(),
            ),
            (Duration::from_secs(5), b"data: [DONE]\n\n".to_vec()),
        ],
        finish: Finish::CleanEof,
    }]);
    let fixture = build_fixture(
        "openai",
        &[ProviderSpec {
            name: "account-a".to_owned(),
            provider_id: "provider-a".to_owned(),
            account_id: 1,
            base_url: server.base_url(),
            surfaces: vec![WireSurface::OpenaiChatCompletions],
        }],
        RetryPolicy::default(),
        &StreamOptions::default(),
    )
    .await;
    let proxy_id = "proxy-cancel-after";
    let mut execution = fixture
        .coordinator
        .execute(stream_request(ClientSurface::ChatCompletions, proxy_id))
        .await
        .expect("executes");
    execution.mark_started();
    let first = execution
        .next_chunk()
        .await
        .expect("first chunk arrives")
        .expect("first chunk decodes");
    assert!(!first.is_empty());
    drop(execution);
    settle(&fixture.supervisor).await;
    assert_eq!(server.count(), 1, "post-handoff cancel never replays");
    assert_eq!(
        fixture
            .coordinator
            .diagnostic_count(OUTCOME_CLIENT_CANCELLED),
        1
    );
    let (status, _, _, _, _) = db_request_row(&fixture.database, proxy_id).await;
    assert_eq!(status, "cancelled");
    let (_, terminal_attempts, active) = count_rows(&fixture.database).await;
    assert_eq!(terminal_attempts, 1);
    assert_eq!(active, 0);
    assert_eq!(fixture.router.active_request_count("account-a"), 0);
    let snapshot = fixture.estimator.snapshot(&["account-a".to_owned()]);
    assert_eq!(snapshot["account-a"].pending_requests, 0);
    assert_eq!(snapshot["account-a"].reserved_requests, 0);
    server.join().await;
    fixture.database.close().await.expect("database closes");
}

#[tokio::test]
async fn stream_downstream_write_failure_after_handoff_never_retries() {
    let server = StreamingProvider::start(vec![SseScript::sse(upstream_success_stream(
        WireSurface::OpenaiChatCompletions,
    ))]);
    let fixture = build_fixture(
        "openai",
        &[ProviderSpec {
            name: "account-a".to_owned(),
            provider_id: "provider-a".to_owned(),
            account_id: 1,
            base_url: server.base_url(),
            surfaces: vec![WireSurface::OpenaiChatCompletions],
        }],
        RetryPolicy::default(),
        &StreamOptions::default(),
    )
    .await;
    let proxy_id = "proxy-write-failed";
    let mut execution = fixture
        .coordinator
        .execute(stream_request(ClientSurface::ChatCompletions, proxy_id))
        .await
        .expect("executes");
    execution.mark_started();
    let (chunks, error) = drain_stream(&mut execution).await;
    assert!(!chunks.is_empty());
    assert_eq!(error, None);
    let result = execution
        .complete(DownstreamResult::WriteFailed)
        .await
        .expect("write-failure completion converges");
    assert!(result.progress.completed);
    assert_eq!(server.count(), 1, "post-handoff failure never replays");
    assert_eq!(
        fixture
            .coordinator
            .diagnostic_count(OUTCOME_CLIENT_CANCELLED),
        1
    );
    let (status, _, _, _, _) = db_request_row(&fixture.database, proxy_id).await;
    assert_eq!(status, "cancelled");
    let (_, terminal_attempts, active) = count_rows(&fixture.database).await;
    assert_eq!(terminal_attempts, 1);
    assert_eq!(active, 0);
    server.join().await;
    fixture.database.close().await.expect("database closes");
}

#[tokio::test]
async fn stream_cancellation_during_finalization_handoff_preserves_terminal() {
    let server = StreamingProvider::start(vec![SseScript::sse(upstream_success_stream(
        WireSurface::OpenaiChatCompletions,
    ))]);
    let fixture = build_fixture(
        "openai",
        &[ProviderSpec {
            name: "account-a".to_owned(),
            provider_id: "provider-a".to_owned(),
            account_id: 1,
            base_url: server.base_url(),
            surfaces: vec![WireSurface::OpenaiChatCompletions],
        }],
        RetryPolicy::default(),
        &StreamOptions::default(),
    )
    .await;
    let proxy_id = "proxy-finalization-handoff";
    let mut execution = fixture
        .coordinator
        .execute(stream_request(ClientSurface::ChatCompletions, proxy_id))
        .await
        .expect("executes");
    execution.mark_started();
    let (_, error) = drain_stream(&mut execution).await;
    assert_eq!(error, None);
    // Drop after the natural terminal but before `complete`: the stored
    // completed terminal must survive, not become interrupted.
    drop(execution);
    settle(&fixture.supervisor).await;
    let (status, input, output, _, _) = db_request_row(&fixture.database, proxy_id).await;
    assert_eq!(status, "completed");
    assert_eq!((input, output), (Some(10), Some(4)));
    let (_, terminal_attempts, active) = count_rows(&fixture.database).await;
    assert_eq!(terminal_attempts, 1);
    assert_eq!(active, 0);
    server.join().await;
    fixture.database.close().await.expect("database closes");
}

// ---------------------------------------------------------------------------
// Bounds, phases, policy, and request guards
// ---------------------------------------------------------------------------

#[tokio::test]
async fn stream_handoff_state_is_monotonic_and_process_local() {
    let state = ResponseHandoffState::default();
    assert!(!state.started());
    state.mark_started();
    assert!(state.started());
    state.mark_started();
    assert!(state.started());
    let cloned = state.clone();
    assert!(cloned.started(), "handoff shares one monotonic fact");
}

#[tokio::test]
async fn stream_phase_progresses_through_lifecycle() {
    let server = StreamingProvider::start(vec![SseScript::sse(upstream_success_stream(
        WireSurface::OpenaiChatCompletions,
    ))]);
    let fixture = build_fixture(
        "openai",
        &[ProviderSpec {
            name: "account-a".to_owned(),
            provider_id: "provider-a".to_owned(),
            account_id: 1,
            base_url: server.base_url(),
            surfaces: vec![WireSurface::OpenaiChatCompletions],
        }],
        RetryPolicy::default(),
        &StreamOptions::default(),
    )
    .await;
    let proxy_id = "proxy-phases";
    let mut execution = fixture
        .coordinator
        .execute(stream_request(ClientSurface::ChatCompletions, proxy_id))
        .await
        .expect("executes");
    assert_eq!(execution.phase(), StreamPhase::DownstreamPending);
    execution.mark_started();
    assert_eq!(execution.phase(), StreamPhase::Streaming);
    let (_, error) = drain_stream(&mut execution).await;
    assert_eq!(error, None);
    assert_eq!(execution.phase(), StreamPhase::Closed);
    assert!(execution.transport_released());
    assert!(execution.provider_bytes_observed() > 0);
    assert!(execution.client_bytes_emitted() > 0);
    let result = execution
        .complete(DownstreamResult::Delivered)
        .await
        .expect("phase completion converges");
    assert!(result.progress.completed);
    server.join().await;
    fixture.database.close().await.expect("database closes");
}

#[tokio::test]
async fn stream_first_chunk_arrives_before_eof() {
    // The second half of the stream (including terminal evidence and EOF)
    // waits well beyond this assertion: a first chunk proves the coordinator
    // forwards incrementally instead of buffering the complete stream.
    let body = upstream_success_stream(WireSurface::OpenaiChatCompletions);
    let split = body.len() / 3;
    let server = StreamingProvider::start(vec![SseScript {
        header_delay: Duration::ZERO,
        extra_headers: vec![("content-type".to_owned(), "text/event-stream".to_owned())],
        framing: Framing::Raw,
        chunks: vec![
            (Duration::ZERO, body[..split].to_vec()),
            (Duration::from_secs(2), body[split..].to_vec()),
        ],
        finish: Finish::CleanEof,
    }]);
    let fixture = build_fixture(
        "openai",
        &[ProviderSpec {
            name: "account-a".to_owned(),
            provider_id: "provider-a".to_owned(),
            account_id: 1,
            base_url: server.base_url(),
            surfaces: vec![WireSurface::OpenaiChatCompletions],
        }],
        RetryPolicy::default(),
        &StreamOptions::default(),
    )
    .await;
    let proxy_id = "proxy-incremental";
    let mut execution = fixture
        .coordinator
        .execute(stream_request(ClientSurface::ChatCompletions, proxy_id))
        .await
        .expect("executes");
    execution.mark_started();
    let started = std::time::Instant::now();
    let first = tokio::time::timeout(Duration::from_secs(5), execution.next_chunk())
        .await
        .expect("first chunk is timely")
        .expect("stream continues")
        .expect("first chunk decodes");
    assert!(!first.is_empty());
    assert!(
        started.elapsed() < Duration::from_secs(1),
        "first chunk arrives before EOF is available"
    );
    let (_, error) = drain_stream(&mut execution).await;
    assert_eq!(error, None);
    let result = execution
        .complete(DownstreamResult::Delivered)
        .await
        .expect("incremental completion converges");
    assert!(result.progress.completed);
    server.join().await;
    fixture.database.close().await.expect("database closes");
}

#[tokio::test]
async fn stream_non_sse_body_passes_through_and_completes() {
    // Legacy Python parity: a non-event-stream body forwards raw chunks and
    // ends complete at EOF.
    let server = StreamingProvider::start(vec![SseScript {
        header_delay: Duration::ZERO,
        extra_headers: vec![("content-type".to_owned(), "application/json".to_owned())],
        framing: Framing::Raw,
        chunks: vec![(Duration::ZERO, br#"{"partial":true}"#.to_vec())],
        finish: Finish::CleanEof,
    }]);
    let fixture = build_fixture(
        "openai",
        &[ProviderSpec {
            name: "account-a".to_owned(),
            provider_id: "provider-a".to_owned(),
            account_id: 1,
            base_url: server.base_url(),
            surfaces: vec![WireSurface::OpenaiChatCompletions],
        }],
        RetryPolicy::default(),
        &StreamOptions::default(),
    )
    .await;
    let proxy_id = "proxy-passthrough";
    let mut execution = fixture
        .coordinator
        .execute(stream_request(ClientSurface::ChatCompletions, proxy_id))
        .await
        .expect("passthrough executes");
    assert!(execution.is_stream());
    let content_type = execution
        .headers
        .headers
        .iter()
        .find(|(name, _)| name.as_str() == "content-type")
        .map(|(_, value)| value.to_str().unwrap_or_default().to_owned())
        .unwrap_or_default();
    assert!(
        content_type.contains("application/json"),
        "passthrough preserves the upstream content type: {content_type}"
    );
    execution.mark_started();
    let (chunks, error) = drain_stream(&mut execution).await;
    assert_eq!(error, None);
    let joined: Vec<u8> = chunks.iter().flat_map(|chunk| chunk.to_vec()).collect();
    assert_eq!(joined, br#"{"partial":true}"#);
    let result = execution
        .complete(DownstreamResult::Delivered)
        .await
        .expect("passthrough completion converges");
    assert!(result.progress.completed);
    let (status, _, _, _, _) = db_request_row(&fixture.database, proxy_id).await;
    assert_eq!(status, "completed");
    server.join().await;
    fixture.database.close().await.expect("database closes");
}

#[tokio::test]
async fn stream_request_rejects_non_stream_bodies() {
    let raw = Bytes::from(
        serde_json::to_vec(&serde_json::json!({
            "model": MODEL,
            "messages": [{"role": "user", "content": "hello"}],
        }))
        .expect("body serializes"),
    );
    let result = StreamRequest::new(
        "proxy-not-stream",
        raw,
        HeaderMap::new(),
        ClientSurface::ChatCompletions,
        StaticRoutingFacts {
            known_provider_ids: BTreeSet::new(),
            requested_protocol: None,
            transcode_protocols: Vec::new(),
            catalog_stale_after_s: None,
            capability_policy: BTreeMap::new(),
            now: 0,
        },
    );
    assert!(
        matches!(result, Err(StreamingCoordinatorError::InvalidFacts)),
        "streaming requires stream intent: {result:?}"
    );
}

#[test]
fn stream_timeout_policy_follows_provider_config() {
    let provider = ProviderConfig {
        read_timeout_s: 300.0,
        stream_timeouts: ProviderStreamTimeoutConfig {
            first_byte_timeout_s: Some(1.5),
            idle_timeout_s: Some(2.5),
            max_lifetime_s: Some(60.0),
        },
        ..Default::default()
    };
    let policy = StreamTimeoutPolicy::from_provider(&provider);
    assert_eq!(policy.header_timeout, Some(Duration::from_secs(300)));
    assert_eq!(policy.first_byte_timeout, Some(Duration::from_millis(1500)));
    assert_eq!(policy.idle_timeout, Some(Duration::from_millis(2500)));
    // No whole-stream deadline exists: `max_lifetime_s` is accepted by config
    // but never becomes a coordinator timer.
    let unset = StreamTimeoutPolicy::from_provider(&ProviderConfig::default());
    assert_eq!(unset.first_byte_timeout, None);
    assert_eq!(unset.idle_timeout, None);
}
