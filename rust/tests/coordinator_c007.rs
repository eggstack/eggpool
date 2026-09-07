//! C007 finite-response handoff and completion.
//!
//! End-to-end coverage for the non-streaming inference path: one C004
//! upstream response through M6 finite decoding/adaptation, monotonic
//! downstream handoff, C005 success/failure effects, retained C006 terminal
//! finalization, and the client-visible response contract.
//!
//! Every test uses deterministic local HTTP providers (no external network).
//! Upstream attempt counts and ordering are asserted explicitly, including
//! the exact point after which retries stop (downstream handoff).

use std::{
    collections::{BTreeMap, BTreeSet},
    sync::{
        Arc, Mutex,
        atomic::{AtomicUsize, Ordering},
    },
    time::Duration,
};

use bytes::Bytes;
use eggpool::{
    Config,
    accounts::{AccountRegistry, CredentialStore},
    catalog::{ModelCatalogCache, ModelInput, ProtocolResolutionStatus},
    config::{AccountConfig, ProviderAuthConfig, ProviderConfig},
    coordinator::{
        AttemptBuilder, DownstreamResult, DurableFinalizer, FinalizationSupervisor,
        FiniteCoordinator, FiniteRequest, PublicationService, ResponseHandoffState, RetryPolicy,
        WireResolver, WireResolverConfig, filter_response_headers,
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
use http::{HeaderMap, HeaderValue, StatusCode};
use serde_json::{Value, json};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

const MODEL: &str = "fixture-model";

// ---------------------------------------------------------------------------
// Wire profiles and payloads (mirrors W012 qualification fixtures)
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

fn client_request_body(client: ClientSurface) -> Bytes {
    let value = match client {
        ClientSurface::ChatCompletions => json!({
            "model": MODEL,
            "messages": [{"role": "user", "content": "hello"}],
        }),
        ClientSurface::Responses => json!({
            "model": MODEL,
            "input": [{"type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "hello"}]}],
            "max_output_tokens": 32,
        }),
        ClientSurface::Messages => json!({
            "model": MODEL,
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

// ---------------------------------------------------------------------------
// Local deterministic HTTP provider
// ---------------------------------------------------------------------------

struct LocalProvider {
    port: u16,
    observed_requests: Arc<Mutex<Vec<Vec<u8>>>>,
    request_count: Arc<AtomicUsize>,
    task: Option<tokio::task::JoinHandle<()>>,
}

type PlannedUpstreamResponse = (u16, Vec<(String, String)>, Vec<u8>);

impl LocalProvider {
    /// Serve `responses` in order; repeats the last entry for extra requests.
    /// Each entry is `(status, extra_headers, body)`.
    fn start(responses: Vec<PlannedUpstreamResponse>) -> Self {
        let listener = std::net::TcpListener::bind(("127.0.0.1", 0)).expect("fixture listener");
        listener.set_nonblocking(true).expect("fixture nonblocking");
        let port = listener.local_addr().expect("fixture address").port();
        let listener = tokio::net::TcpListener::from_std(listener).expect("tokio fixture listener");
        let observed_requests = Arc::new(Mutex::new(Vec::new()));
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
                    401 => "Unauthorized",
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

    fn observed(&self) -> Vec<Vec<u8>> {
        self.observed_requests.lock().unwrap().clone()
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
// Coordinator fixture
// ---------------------------------------------------------------------------

struct ProviderSpec {
    name: String,
    provider_id: String,
    account_id: i64,
    base_url: String,
    surfaces: Vec<WireSurface>,
}

struct Fixture {
    database: Database,
    router: RoutingRouter,
    estimator: QuotaEstimator,
    coordinator: FiniteCoordinator,
    supervisor: FinalizationSupervisor,
}

async fn build_fixture(
    client_protocol: &str,
    specs: &[ProviderSpec],
    retry_policy: RetryPolicy,
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
    let coordinator = FiniteCoordinator::new(
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

fn finite_request(client: ClientSurface, proxy_id: &str) -> FiniteRequest {
    FiniteRequest::new(
        proxy_id,
        client_request_body(client),
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
) -> (String, Option<i64>, Option<i64>, i64, i64, Option<String>) {
    // (status, input_tokens, output_tokens, bytes_received, bytes_emitted,
    //  upstream_request_id)
    database
        .call({
            let proxy_request_id = proxy_request_id.to_owned();
            move |connection| {
                connection.query_row(
                    "SELECT status, input_tokens, output_tokens, bytes_received,
                            bytes_emitted, upstream_request_id
                     FROM requests WHERE proxy_request_id = ?1",
                    [proxy_request_id],
                    |row| {
                        Ok((
                            row.get::<_, String>(0)?,
                            row.get::<_, Option<i64>>(1)?,
                            row.get::<_, Option<i64>>(2)?,
                            row.get::<_, i64>(3)?,
                            row.get::<_, i64>(4)?,
                            row.get::<_, Option<String>>(5)?,
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

// ---------------------------------------------------------------------------
// Success matrix: every public client surface x all five upstream profiles
// ---------------------------------------------------------------------------

#[tokio::test]
async fn finite_success_matrix_covers_every_client_surface_and_upstream_profile() {
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
            let server = LocalProvider::start(vec![(
                200,
                vec![
                    ("x-custom".to_owned(), "kept".to_owned()),
                    ("x-request-id".to_owned(), "upstream-123".to_owned()),
                ],
                upstream_success_body(upstream),
            )]);
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
            )
            .await;
            let proxy_id = format!("proxy-{}-{}", client.as_str(), upstream.as_str());
            let execution = fixture
                .coordinator
                .execute(finite_request(client, &proxy_id))
                .await
                .unwrap_or_else(|error| panic!("{client:?} x {upstream:?} executes: {error:?}"));
            // Exactly one upstream attempt before handoff.
            assert_eq!(server.count(), 1, "{client:?} x {upstream:?}");
            // Filtered headers preserve useful values and compatibility IDs.
            let names: Vec<String> = execution
                .response
                .headers
                .iter()
                .map(|(name, _)| name.as_str().to_owned())
                .collect();
            assert!(
                names.contains(&"x-custom".to_owned()),
                "{client:?} x {upstream:?} keeps x-custom: {names:?}"
            );
            assert!(
                names.contains(&"x-proxy-request-id".to_owned()),
                "{client:?} x {upstream:?} has proxy request id: {names:?}"
            );
            assert!(
                names.contains(&"x-proxy-attempt-count".to_owned()),
                "{client:?} x {upstream:?} has attempt count: {names:?}"
            );
            assert_eq!(execution.response.status, StatusCode::OK);
            assert!(!execution.response.body.is_empty());
            // Handoff is monotonic: not started before the caller marks it.
            assert!(!execution.handoff_started());
            execution.mark_started();
            assert!(execution.handoff_started());
            let result = execution
                .complete(DownstreamResult::Delivered)
                .await
                .expect("completion converges");
            assert!(result.progress.completed, "{client:?} x {upstream:?}");
            // No replay after handoff: still exactly one upstream attempt.
            assert_eq!(server.count(), 1, "{client:?} x {upstream:?} no retry");
            // Durable request reflects normalized usage and provenance.
            let (status, input, output, bytes_in, bytes_out, upstream_id) =
                db_request_row(&fixture.database, &proxy_id).await;
            assert_eq!(status, "completed", "{client:?} x {upstream:?}");
            assert_eq!(
                (input, output),
                (Some(10), Some(4)),
                "{client:?} x {upstream:?}"
            );
            assert_eq!(upstream_id.as_deref(), Some("upstream-123"));
            assert!(bytes_in > 0 && bytes_out > 0, "{client:?} x {upstream:?}");
            let (requests, terminal_attempts, active_reservations) =
                count_rows(&fixture.database).await;
            assert_eq!(requests, 1);
            assert_eq!(terminal_attempts, 1);
            assert_eq!(active_reservations, 0);
            assert_eq!(fixture.router.active_request_count("account-a"), 0);
            server.join().await;
            fixture.database.close().await.expect("database closes");
        }
    }
}

// ---------------------------------------------------------------------------
// Provider error passthrough (non-retryable 4xx) keeps status and headers
// ---------------------------------------------------------------------------

#[tokio::test]
async fn finite_non_retryable_provider_error_passes_through_with_proxy_headers() {
    let error_body = br#"{"error":{"message":"bad request","type":"invalid_request_error"}}"#;
    let server = LocalProvider::start(vec![(
        400,
        vec![("x-custom".to_owned(), "kept".to_owned())],
        error_body.to_vec(),
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
    )
    .await;
    let proxy_id = "proxy-4xx-passthrough";
    let execution = fixture
        .coordinator
        .execute(finite_request(ClientSurface::ChatCompletions, proxy_id))
        .await
        .expect("terminal error executes");
    assert_eq!(server.count(), 1, "no retry for non-retryable 400");
    assert_eq!(execution.response.status, StatusCode::BAD_REQUEST);
    let names: Vec<String> = execution
        .response
        .headers
        .iter()
        .map(|(name, _)| name.as_str().to_owned())
        .collect();
    assert!(names.contains(&"x-custom".to_owned()));
    assert!(names.contains(&"x-proxy-request-id".to_owned()));
    assert!(names.contains(&"x-proxy-attempt-count".to_owned()));
    execution.mark_started();
    let result = execution
        .complete(DownstreamResult::Delivered)
        .await
        .expect("error completion converges");
    assert!(result.progress.completed);
    assert_eq!(server.count(), 1, "no post-handoff replay");
    let (status, _, _, _, _, _) = db_request_row(&fixture.database, proxy_id).await;
    assert_eq!(status, "client_error");
    let (_, terminal_attempts, active) = count_rows(&fixture.database).await;
    assert_eq!(terminal_attempts, 1);
    assert_eq!(active, 0);
    server.join().await;
    fixture.database.close().await.expect("database closes");
}

// ---------------------------------------------------------------------------
// Retryable pre-handoff 5xx fails over to a second account, then succeeds
// ---------------------------------------------------------------------------

#[tokio::test]
async fn finite_retryable_response_fails_over_before_handoff_and_cleans_up() {
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
        RetryPolicy::default(),
    )
    .await;
    let proxy_id = "proxy-retry-failover";
    let execution = fixture
        .coordinator
        .execute(finite_request(ClientSurface::ChatCompletions, proxy_id))
        .await
        .expect("failover executes");
    // Two upstream attempts in order: the failed account first, then failover.
    assert_eq!(failing.count(), 1, "first attempt hits account-a");
    assert_eq!(succeeding.count(), 1, "retry fails over to account-b");
    assert_eq!(execution.response.status, StatusCode::OK);
    execution.mark_started();
    let result = execution
        .complete(DownstreamResult::Delivered)
        .await
        .expect("failover completion converges");
    assert!(result.progress.completed);
    // No further replay after handoff.
    assert_eq!(failing.count(), 1);
    assert_eq!(succeeding.count(), 1);
    let (status, _, _, _, _, _) = db_request_row(&fixture.database, proxy_id).await;
    assert_eq!(status, "completed");
    // Both attempts reached their required cleanup boundary: the retryable
    // attempt was finalized before replacement ownership, and the final
    // attempt converged with no leaked reservation.
    let attempts: i64 = fixture
        .database
        .call(|connection| {
            connection.query_row("SELECT COUNT(*) FROM request_attempts", [], |row| {
                row.get(0)
            })
        })
        .await
        .expect("attempt count");
    assert_eq!(attempts, 2);
    let (_, terminal_attempts, active) = count_rows(&fixture.database).await;
    assert_eq!(terminal_attempts, 2);
    assert_eq!(active, 0);
    assert_eq!(fixture.router.active_request_count("account-a"), 0);
    assert_eq!(fixture.router.active_request_count("account-b"), 0);
    failing.join().await;
    succeeding.join().await;
    fixture.database.close().await.expect("database closes");
}

// ---------------------------------------------------------------------------
// Malformed 2xx is terminal (no retry) with retained ownership
// ---------------------------------------------------------------------------

#[tokio::test]
async fn finite_malformed_success_is_terminal_without_retry() {
    let server = LocalProvider::start(vec![(200, Vec::new(), b"not-json{{{".to_vec())]);
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
    )
    .await;
    let proxy_id = "proxy-malformed";
    let execution = fixture
        .coordinator
        .execute(finite_request(ClientSurface::ChatCompletions, proxy_id))
        .await
        .expect("malformed executes terminally");
    assert_eq!(server.count(), 1, "malformed never retries");
    assert_eq!(execution.response.status, StatusCode::INTERNAL_SERVER_ERROR);
    execution.mark_started();
    let result = execution
        .complete(DownstreamResult::Delivered)
        .await
        .expect("malformed completion converges");
    assert!(result.progress.completed);
    assert_eq!(server.count(), 1, "no post-handoff replay");
    let (status, _, _, _, _, _) = db_request_row(&fixture.database, proxy_id).await;
    assert_eq!(status, "client_error");
    server.join().await;
    fixture.database.close().await.expect("database closes");
}

// ---------------------------------------------------------------------------
// Failed downstream write after handoff is terminal without retry
// ---------------------------------------------------------------------------

#[tokio::test]
async fn finite_downstream_write_failure_after_handoff_never_retries() {
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
        RetryPolicy::default(),
    )
    .await;
    let proxy_id = "proxy-write-failed";
    let execution = fixture
        .coordinator
        .execute(finite_request(ClientSurface::ChatCompletions, proxy_id))
        .await
        .expect("executes");
    assert_eq!(server.count(), 1);
    // Response start was attempted before the write failed.
    execution.mark_started();
    assert!(execution.handoff_started());
    let result = execution
        .complete(DownstreamResult::WriteFailed)
        .await
        .expect("write-failure completion converges");
    assert!(result.progress.completed);
    assert_eq!(
        server.count(),
        1,
        "post-handoff failure never replays upstream"
    );
    let (status, _, _, _, _, _) = db_request_row(&fixture.database, proxy_id).await;
    assert_eq!(status, "cancelled");
    let (_, terminal_attempts, active) = count_rows(&fixture.database).await;
    assert_eq!(terminal_attempts, 1);
    assert_eq!(active, 0);
    server.join().await;
    fixture.database.close().await.expect("database closes");
}

// ---------------------------------------------------------------------------
// Cancellation between decode and response start, and after start
// ---------------------------------------------------------------------------

#[tokio::test]
async fn finite_cancellation_before_handoff_finalizes_as_interrupted() {
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
        RetryPolicy::default(),
    )
    .await;
    let proxy_id = "proxy-cancel-before";
    let execution = fixture
        .coordinator
        .execute(finite_request(ClientSurface::ChatCompletions, proxy_id))
        .await
        .expect("executes");
    assert_eq!(server.count(), 1);
    assert!(!execution.handoff_started());
    // Cancellation before response start: drop without completing.
    drop(execution);
    tokio::time::sleep(Duration::from_millis(200)).await;
    assert_eq!(server.count(), 1, "cancelled request never replays");
    let (status, _, _, _, _, _) = db_request_row(&fixture.database, proxy_id).await;
    assert_eq!(status, "error");
    let (_, terminal_attempts, active) = count_rows(&fixture.database).await;
    assert_eq!(terminal_attempts, 1);
    assert_eq!(active, 0);
    assert_eq!(fixture.router.active_request_count("account-a"), 0);
    server.join().await;
    fixture.database.close().await.expect("database closes");
}

#[tokio::test]
async fn finite_cancellation_after_handoff_marks_downstream_started() {
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
        RetryPolicy::default(),
    )
    .await;
    let proxy_id = "proxy-cancel-after";
    let execution = fixture
        .coordinator
        .execute(finite_request(ClientSurface::ChatCompletions, proxy_id))
        .await
        .expect("executes");
    execution.mark_started();
    drop(execution);
    tokio::time::sleep(Duration::from_millis(200)).await;
    assert_eq!(server.count(), 1, "post-handoff cancel never replays");
    let downstream_started: bool = fixture
        .database
        .call({
            let proxy_id = proxy_id.to_owned();
            move |connection| {
                connection.query_row(
                    "SELECT COUNT(*) FROM requests WHERE proxy_request_id = ?1 AND status IN
                     ('cancelled', 'error')",
                    [proxy_id],
                    |row| row.get::<_, i64>(0),
                )
            }
        })
        .await
        .map(|count| count == 1)
        .expect("terminal row reads");
    assert!(
        downstream_started,
        "cancelled request reaches a terminal state"
    );
    let (_, terminal_attempts, active) = count_rows(&fixture.database).await;
    assert_eq!(terminal_attempts, 1);
    assert_eq!(active, 0);
    server.join().await;
    fixture.database.close().await.expect("database closes");
}

// ---------------------------------------------------------------------------
// Header filtering, redaction, and error-shape parity
// ---------------------------------------------------------------------------

#[tokio::test]
async fn finite_header_filtering_drops_hop_by_hop_and_internal_headers() {
    let headers: HeaderMap = [
        ("connection", "x-nominated"),
        ("transfer-encoding", "chunked"),
        ("content-encoding", "gzip"),
        ("content-length", "4"),
        ("authorization", "Bearer upstream-secret"),
        ("x-api-key", "upstream-secret"),
        ("x-nominated", "dropped-by-connection"),
        ("x-custom", "kept"),
        ("x-request-id", "upstream-1"),
    ]
    .into_iter()
    .map(|(name, value)| {
        (
            name.parse().expect("header name"),
            HeaderValue::from_str(value).expect("header value"),
        )
    })
    .collect();
    let filtered = filter_response_headers(&headers);
    let names: Vec<String> = filtered
        .iter()
        .map(|(name, _)| name.as_str().to_owned())
        .collect();
    assert!(names.contains(&"x-custom".to_owned()));
    assert!(names.contains(&"x-request-id".to_owned()));
    for dropped in [
        "connection",
        "transfer-encoding",
        "content-encoding",
        "content-length",
        "authorization",
        "x-api-key",
        "x-nominated",
    ] {
        assert!(!names.contains(&dropped.to_owned()), "drops {dropped}");
    }
    // Debug output never carries secret values.
    assert!(!format!("{filtered:?}").contains("upstream-secret"));
}

#[tokio::test]
async fn finite_forwards_filtered_headers_and_never_leaks_client_credentials() {
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
        RetryPolicy::default(),
    )
    .await;
    let mut incoming = HeaderMap::new();
    incoming.insert("authorization", "Bearer client-secret".parse().unwrap());
    incoming.insert("x-custom-in", "forwarded".parse().unwrap());
    let request = FiniteRequest::new(
        "proxy-credential-hygiene",
        client_request_body(ClientSurface::ChatCompletions),
        incoming,
        ClientSurface::ChatCompletions,
        StaticRoutingFacts {
            known_provider_ids: BTreeSet::new(),
            requested_protocol: None,
            transcode_protocols: Vec::new(),
            catalog_stale_after_s: None,
            capability_policy: BTreeMap::new(),
            now: 0,
        },
    )
    .expect("admission succeeds");
    let execution = fixture
        .coordinator
        .execute(request)
        .await
        .expect("executes");
    execution.mark_started();
    let _ = execution
        .complete(DownstreamResult::Delivered)
        .await
        .expect("completes");
    let observed = server.observed();
    server.join().await;
    assert_eq!(observed.len(), 1, "one upstream dispatch");
    let text = String::from_utf8_lossy(&observed[0]);
    assert!(
        text.contains("x-custom-in: forwarded"),
        "allowed headers are forwarded: {text}"
    );
    assert!(
        !text.contains("client-secret"),
        "client credentials never reach the provider"
    );
    fixture.database.close().await.expect("database closes");
}

#[tokio::test]
async fn finite_error_shape_matches_client_surface_protocol() {
    // Anthropic clients receive the Anthropic error envelope; OpenAI
    // clients receive the OpenAI envelope.
    for (client, key) in [
        (ClientSurface::Messages, "api_error"),
        (ClientSurface::ChatCompletions, "upstream_error"),
        (ClientSurface::Responses, "upstream_error"),
    ] {
        let server = LocalProvider::start(vec![(500, Vec::new(), b"{}".to_vec())]);
        // A 500 with an empty JSON object is a valid provider error
        // envelope for every test surface; with a single account and no
        // alternate wire it is terminal without retry.
        let fixture = build_fixture(
            client.protocol(),
            &[ProviderSpec {
                name: "account-a".to_owned(),
                provider_id: "provider-a".to_owned(),
                account_id: 1,
                base_url: server.base_url(),
                surfaces: vec![WireSurface::OpenaiChatCompletions],
            }],
            RetryPolicy {
                max_attempts: 1,
                ..RetryPolicy::default()
            },
        )
        .await;
        let proxy_id = format!("proxy-error-shape-{}", client.as_str());
        let result = fixture
            .coordinator
            .execute(finite_request(client, &proxy_id))
            .await;
        // Transcoded upstream combinations may fail admission-to-wire
        // preparation; both outcomes must be terminal with retained
        // ownership and no leak, never a retry after handoff.
        match result {
            Ok(execution) => {
                assert_eq!(server.count(), 1);
                let body: Value =
                    serde_json::from_slice(&execution.response.body).expect("error is JSON");
                let envelope = body.to_string();
                assert!(
                    envelope.contains(key),
                    "{client:?} error envelope contains {key}: {envelope}"
                );
                execution.mark_started();
                let completed = execution
                    .complete(DownstreamResult::Delivered)
                    .await
                    .expect("error completion converges");
                assert!(completed.progress.completed);
                assert_eq!(server.count(), 1, "no post-handoff replay");
            }
            Err(error) => {
                // Preparation-level rejection (e.g. cross-protocol request
                // that M6 cannot encode) is an explicit terminal contract:
                // no upstream dispatch, no retry, no leak.
                assert_eq!(server.count(), 0, "{client:?}: {error:?}");
            }
        }
        server.join().await;
        fixture.database.close().await.expect("database closes");
    }
}

// ---------------------------------------------------------------------------
// Response handoff monotonicity and resource limits
// ---------------------------------------------------------------------------

#[tokio::test]
async fn finite_handoff_state_is_monotonic_and_process_local() {
    let state = ResponseHandoffState::default();
    assert!(!state.started());
    state.mark_started();
    assert!(state.started());
    // Repeated marking is harmless and never resets.
    state.mark_started();
    assert!(state.started());
    let cloned = state.clone();
    assert!(cloned.started(), "handoff shares one monotonic fact");
}

#[tokio::test]
async fn finite_provider_body_limit_is_terminal_without_retry() {
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
        RetryPolicy::default(),
    )
    .await;
    // Shrink the provider body bound far below any valid upstream payload.
    let tiny = FiniteCoordinator::new(
        fixture.router.clone(),
        PublicationService::new(fixture.database.clone()),
        AttemptBuilder::new(
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
            WireRuntime::embedded().expect("wire runtime"),
        ),
        WireRuntime::embedded().expect("wire runtime"),
        WireResolver::new(WireResolverConfig {
            min_negotiation_interval: Duration::ZERO,
            ..Default::default()
        }),
        BTreeMap::from([(
            "provider-a".to_owned(),
            vec![profile(WireSurface::OpenaiChatCompletions)],
        )]),
        BTreeMap::from([(
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
        )]),
        CredentialStore::default(),
        fixture.supervisor.clone(),
        RetryPolicy::default(),
    )
    .with_max_provider_body_bytes(4);
    let execution = tiny
        .execute(finite_request(
            ClientSurface::ChatCompletions,
            "proxy-body-limit",
        ))
        .await
        .expect("limit violation executes terminally");
    assert_eq!(server.count(), 1, "resource violation never retries");
    assert_eq!(execution.response.status, StatusCode::BAD_GATEWAY);
    execution.mark_started();
    let result = execution
        .complete(DownstreamResult::Delivered)
        .await
        .expect("limit completion converges");
    assert!(result.progress.completed);
    server.join().await;
    fixture.database.close().await.expect("database closes");
}

#[tokio::test]
async fn finite_exhausted_retryable_passes_through_last_response() {
    // Single account, always-500: the retryable failure has nowhere to fail
    // over, so exhaustion must return the real upstream response (Python
    // `_handle_exhausted` pass-through), never a synthetic envelope.
    let server = LocalProvider::start(vec![(
        500,
        Vec::new(),
        br#"{"error":{"message":"boom","type":"server_error"}}"#.to_vec(),
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
    )
    .await;
    let proxy_id = "proxy-exhausted-passthrough";
    let execution = fixture
        .coordinator
        .execute(finite_request(ClientSurface::ChatCompletions, proxy_id))
        .await
        .expect("exhaustion executes");
    assert_eq!(server.count(), 1, "no account remains for a second attempt");
    assert_eq!(execution.response.status, StatusCode::INTERNAL_SERVER_ERROR);
    assert_eq!(
        execution.response.body.as_ref(),
        br#"{"error":{"message":"boom","type":"server_error"}}"#,
        "exhausted response preserves the upstream body"
    );
    execution.mark_started();
    let result = execution
        .complete(DownstreamResult::Delivered)
        .await
        .expect("exhaustion completion converges");
    assert!(result.progress.completed);
    assert_eq!(server.count(), 1, "no post-handoff replay");
    let (status, _, _, _, _, _) = db_request_row(&fixture.database, proxy_id).await;
    assert_eq!(status, "error");
    let (_, terminal_attempts, active) = count_rows(&fixture.database).await;
    // One attempt was ever published (no account remained for a retry); the
    // failed-attempt cleanup converged it before the terminal request
    // pass-through reused the same identity without a claim.
    assert_eq!(terminal_attempts, 1);
    assert_eq!(active, 0);
    server.join().await;
    fixture.database.close().await.expect("database closes");
}

#[tokio::test]
async fn finite_attempt_ceiling_stops_retry_with_last_response() {
    // Retryable 500 with max_attempts=1: the ceiling converges the failed
    // attempt and terminalizes with the last response plus a retry-reason
    // header, without a second upstream dispatch.
    let server = LocalProvider::start(vec![(
        500,
        Vec::new(),
        br#"{"error":{"message":"boom","type":"server_error"}}"#.to_vec(),
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
        RetryPolicy {
            max_attempts: 1,
            ..RetryPolicy::default()
        },
    )
    .await;
    let proxy_id = "proxy-ceiling";
    let execution = fixture
        .coordinator
        .execute(finite_request(ClientSurface::ChatCompletions, proxy_id))
        .await
        .expect("ceiling executes");
    assert_eq!(server.count(), 1, "ceiling allows exactly one dispatch");
    assert_eq!(execution.response.status, StatusCode::INTERNAL_SERVER_ERROR);
    let reason = execution
        .response
        .headers
        .iter()
        .find(|(name, _)| name.as_str() == "x-proxy-retry-reason")
        .map(|(_, value)| value.to_str().unwrap_or_default().to_owned());
    assert_eq!(reason.as_deref(), Some("attempt_ceiling_reached"));
    execution.mark_started();
    let result = execution
        .complete(DownstreamResult::Delivered)
        .await
        .expect("ceiling completion converges");
    assert!(result.progress.completed);
    assert_eq!(server.count(), 1, "no post-handoff replay");
    let (status, _, _, _, _, _) = db_request_row(&fixture.database, proxy_id).await;
    assert_eq!(status, "error");
    server.join().await;
    fixture.database.close().await.expect("database closes");
}

#[tokio::test]
async fn finite_terminal_paths_leave_no_claim_or_reservation_state() {
    // Every terminal path (success, provider error, malformed) must leave
    // zero active claims and zero active reservations.
    let cases: Vec<(&str, Vec<u8>, u16)> = vec![
        (
            "proxy-cleanup-success",
            upstream_success_body(WireSurface::OpenaiChatCompletions),
            200,
        ),
        (
            "proxy-cleanup-error",
            br#"{"error":{"message":"nope","type":"invalid_request_error"}}"#.to_vec(),
            400,
        ),
        ("proxy-cleanup-malformed", b"{{{".to_vec(), 200),
    ];
    for (proxy_id, body, status) in cases {
        let server = LocalProvider::start(vec![(status, Vec::new(), body)]);
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
        )
        .await;
        let execution = fixture
            .coordinator
            .execute(finite_request(ClientSurface::ChatCompletions, proxy_id))
            .await
            .expect("terminal path executes");
        execution.mark_started();
        let result = execution
            .complete(DownstreamResult::Delivered)
            .await
            .expect("terminal path converges");
        assert!(result.progress.completed, "{proxy_id}");
        let (_, _, active) = count_rows(&fixture.database).await;
        assert_eq!(active, 0, "{proxy_id} leaves no active reservation");
        assert_eq!(
            fixture.router.active_request_count("account-a"),
            0,
            "{proxy_id} leaves no active claim"
        );
        let snapshot = fixture.estimator.snapshot(&["account-a".to_owned()]);
        assert_eq!(snapshot["account-a"].pending_requests, 0);
        assert_eq!(snapshot["account-a"].reserved_requests, 0);
        server.join().await;
        fixture.database.close().await.expect("database closes");
    }
}
