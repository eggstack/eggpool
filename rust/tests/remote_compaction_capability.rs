//! Remote-compaction capability contract (Request Admission and Wire M008).
//!
//! `POST /v1/responses/compact` is a public, finite-only operation that is
//! forwarded natively to a provider wire surface advertising remote
//! compaction v1. ADR-0003 makes the missing-capability case explicit: a
//! request that finds no eligible provider surface advertising the capability
//! fails with a typed capability outcome instead of the ordinary
//! no-eligible-account outcome, so an operator can tell a missing capability
//! from a missing account.
//!
//! Every test uses deterministic local HTTP providers; no paid traffic.

use std::collections::{BTreeMap, BTreeSet};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

use bytes::Bytes;
use eggpool::config::{ProviderConfig, ProviderWireSurfaceConfig};
use eggpool::coordinator::{
    EndpointError, endpoint_error_body_for, execute_compact_finite, new_proxy_request_id,
    remote_compaction_capable,
};
use eggpool::health::HealthManager;
use eggpool::{
    Config,
    accounts::{AccountRegistry, CredentialStore},
    catalog::{ModelCatalogCache, ModelInput, ProtocolResolutionStatus},
    config::{AccountConfig, ProviderAuthConfig},
    coordinator::{
        AttemptBuilder, DownstreamResult, DurableFinalizer, FinalizationSupervisor,
        FiniteCoordinator, InferenceState, PublicationService, RetryPolicy, StreamingCoordinator,
        WireResolver, WireResolverConfig,
    },
    db::{Account, Database, DatabaseConfig, MigrationRunner},
    model_router::ModelRouterAffinity,
    providers::ProviderClientPool,
    quota::{AccountQuota, QuotaEstimator},
    routing::{EligibilityPolicy, RoutingRouter},
    wire::{ConfiguredWireProfile, WireCodecId, WireProfileDefinition, WireRuntime, WireSurface},
};
use http::{HeaderMap, StatusCode};
use serde_json::{Value, json};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

const MODEL: &str = "fixture-model";
const PROVIDER: &str = "provider-a";
const ACCOUNT: &str = "account-a";

/// A credential-shaped marker and an account-identifying marker. Neither may
/// appear in a capability failure body.
const SECRET_MARKER: &str = "sk-compact-capability-marker-9271";
const ACCOUNT_MARKER: &str = "account-capability-marker-4417";

// ---------------------------------------------------------------------------
// Local deterministic HTTP upstream
// ---------------------------------------------------------------------------

/// A deterministic local upstream that records the request line it served.
struct Upstream {
    port: u16,
    requests: Arc<AtomicUsize>,
    observed: Arc<Mutex<Vec<String>>>,
    shutdown: Option<tokio::sync::oneshot::Sender<()>>,
    task: Option<tokio::task::JoinHandle<()>>,
}

impl Upstream {
    fn start(status: u16, body: Vec<u8>) -> Self {
        let listener = std::net::TcpListener::bind(("127.0.0.1", 0)).expect("fixture listener");
        listener.set_nonblocking(true).expect("fixture nonblocking");
        let port = listener.local_addr().expect("fixture address").port();
        let listener = tokio::net::TcpListener::from_std(listener).expect("tokio fixture listener");
        let requests = Arc::new(AtomicUsize::new(0));
        let observed = Arc::new(Mutex::new(Vec::new()));
        let counted = Arc::clone(&requests);
        let recorded = Arc::clone(&observed);
        let (shutdown, mut stop) = tokio::sync::oneshot::channel();
        let task = tokio::spawn(async move {
            loop {
                let accepted = tokio::select! {
                    biased;
                    _ = &mut stop => break,
                    accepted = listener.accept() => accepted,
                };
                let Ok((mut socket, _)) = accepted else {
                    continue;
                };
                counted.fetch_add(1, Ordering::SeqCst);
                let request_line = read_request_line(&mut socket).await;
                recorded.lock().expect("observed lock").push(request_line);
                let head = format!(
                    "HTTP/1.1 {} {}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                    status,
                    reason(status),
                    body.len()
                );
                let _ = socket.write_all(head.as_bytes()).await;
                let _ = socket.write_all(&body).await;
                let _ = socket.flush().await;
            }
        });
        Self {
            port,
            requests,
            observed,
            shutdown: Some(shutdown),
            task: Some(task),
        }
    }

    fn base_url(&self) -> String {
        format!("http://127.0.0.1:{}", self.port)
    }

    fn count(&self) -> usize {
        self.requests.load(Ordering::SeqCst)
    }

    fn request_lines(&self) -> Vec<String> {
        self.observed.lock().expect("observed lock").clone()
    }

    async fn join(mut self) {
        if let Some(shutdown) = self.shutdown.take() {
            let _ = shutdown.send(());
        }
        if let Some(task) = self.task.take() {
            let _ = task.await;
        }
    }
}

fn reason(status: u16) -> &'static str {
    match status {
        200 => "OK",
        500 => "Internal Server Error",
        _ => "Unknown",
    }
}

/// Read one HTTP request and return its request line.
///
/// The body length comes from the declared `Content-Length`, so the reader
/// never guesses at a fixed byte count or a fixed delay.
async fn read_request_line(socket: &mut tokio::net::TcpStream) -> String {
    let mut buffer = Vec::new();
    let mut chunk = [0_u8; 4096];
    let head_end = loop {
        let read = match socket.read(&mut chunk).await {
            Ok(0) => return String::new(),
            Ok(read) => read,
            Err(_) => return String::new(),
        };
        buffer.extend_from_slice(&chunk[..read]);
        if let Some(index) = buffer.windows(4).position(|window| window == b"\r\n\r\n") {
            break index + 4;
        }
    };
    let head = String::from_utf8_lossy(&buffer[..head_end]).into_owned();
    let declared: usize = head
        .lines()
        .find_map(|line| {
            let (name, value) = line.split_once(':')?;
            name.trim()
                .eq_ignore_ascii_case("content-length")
                .then(|| value.trim().parse().ok())?
        })
        .unwrap_or(0);
    while buffer.len() < head_end + declared {
        let read = match socket.read(&mut chunk).await {
            Ok(0) => break,
            Ok(read) => read,
            Err(_) => break,
        };
        buffer.extend_from_slice(&chunk[..read]);
    }
    head.lines().next().unwrap_or_default().to_owned()
}

// ---------------------------------------------------------------------------
// Fixture
// ---------------------------------------------------------------------------

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
        path_template: "/responses".into(),
        stream_path_template: None,
        priority: 0,
    }
}

struct Fixture {
    database: Database,
    state: InferenceState,
}

/// One configured provider: its identity, its account, the models it serves,
/// and whether its Responses surface advertises remote compaction v1.
struct ProviderSpec {
    provider_id: String,
    account_name: String,
    account_id: i64,
    models: Vec<String>,
    compact: bool,
}

impl ProviderSpec {
    fn new(
        provider_id: &str,
        account_name: &str,
        account_id: i64,
        models: &[&str],
        compact: bool,
    ) -> Self {
        Self {
            provider_id: provider_id.to_owned(),
            account_name: account_name.to_owned(),
            account_id,
            models: models.iter().map(|model| (*model).to_owned()).collect(),
            compact,
        }
    }
}

/// Build one installation from the given providers.
///
/// Every provider owns one account and an OpenAI Responses wire surface. A
/// provider with `compact = true` advertises remote compaction v1 together
/// with a compact path template.
async fn build_fixture(base_url: &str, specs: &[ProviderSpec]) -> Fixture {
    let database = Database::open(DatabaseConfig::default())
        .await
        .expect("database opens");
    MigrationRunner::new(&database)
        .run()
        .await
        .expect("migrations apply");
    let rows: Vec<(i64, String, String, Vec<String>)> = specs
        .iter()
        .map(|spec| {
            (
                spec.account_id,
                spec.account_name.clone(),
                spec.provider_id.clone(),
                spec.models.clone(),
            )
        })
        .collect();
    database
        .call(move |connection| {
            for (account_id, account_name, provider_id, models) in &rows {
                connection.execute(
                    "INSERT INTO accounts (id, name, api_key_env, enabled, provider_id)
                     VALUES (?1, ?2, 'UNUSED', 1, ?3)",
                    tokio_rusqlite::rusqlite::params![account_id, account_name, provider_id],
                )?;
                for model in models {
                    connection.execute(
                        "INSERT INTO models (model_id, protocol, provider_id, resolution_status)
                         VALUES (?1, 'openai', ?2, 'resolved')",
                        tokio_rusqlite::rusqlite::params![model, provider_id],
                    )?;
                }
            }
            Ok(())
        })
        .await
        .expect("fixture rows insert");

    let mut config = Config::default();
    let mut accounts = Vec::new();
    let mut quotas = Vec::new();
    let mut catalog = ModelCatalogCache::default();
    let mut provider_profiles: BTreeMap<String, Vec<ConfiguredWireProfile>> = BTreeMap::new();

    for spec in specs {
        let mut provider = ProviderConfig {
            id: spec.provider_id.clone(),
            base_url: base_url.to_owned(),
            protocols: vec!["openai".to_owned()],
            auth: ProviderAuthConfig {
                mode: "none".to_owned(),
                ..Default::default()
            },
            ..Default::default()
        };
        provider.accounts.push(AccountConfig {
            name: spec.account_name.clone(),
            ..Default::default()
        });
        let mut surface = ProviderWireSurfaceConfig {
            path_template: "/responses".to_owned(),
            ..Default::default()
        };
        if spec.compact {
            surface.supports_remote_compaction_v1 = true;
            surface.compact_path_template = Some("/responses/compact".to_owned());
        }
        provider
            .wire_surfaces
            .insert("openai_responses".to_owned(), surface);

        config.providers.insert(spec.provider_id.clone(), provider);
        provider_profiles.insert(
            spec.provider_id.clone(),
            vec![profile(WireSurface::OpenaiResponses)],
        );
        accounts.push(Account {
            id: spec.account_id,
            name: spec.account_name.clone(),
            api_key_env: "UNUSED".to_owned(),
            enabled: true,
            weight: 1.0,
            provider_id: spec.provider_id.clone(),
        });
        quotas.push(AccountQuota::new(spec.account_name.clone()));
        catalog.set_account_provider(&spec.account_name, &spec.provider_id);
        let models: Vec<ModelInput> = spec
            .models
            .iter()
            .map(|model| {
                let mut input = ModelInput::new(model.clone());
                input.protocol = Some("openai".to_owned());
                input.protocol_source = Some("fixture".to_owned());
                input.resolution_status = ProtocolResolutionStatus::Resolved;
                input
            })
            .collect();
        catalog
            .update_from_account(&spec.account_name, &spec.provider_id, &models, true, true)
            .expect("catalog models");
    }
    config.validate().expect("fixture config validates");

    let registry = AccountRegistry::from_config(&config, &accounts, &CredentialStore::default())
        .expect("registry builds");
    let estimator = QuotaEstimator::new(quotas);
    let health = HealthManager::new();
    for account in registry.all() {
        health.register_account(account.account_id, &account.account_name);
    }
    let router = RoutingRouter::new(
        registry,
        catalog,
        estimator,
        Some(health),
        EligibilityPolicy::default(),
    );
    let clients = ProviderClientPool::from_config(&config).expect("client pool");
    let wire = WireRuntime::embedded().expect("wire runtime");
    let attempts = AttemptBuilder::new(clients, wire.clone());
    let publication = PublicationService::new(database.clone());
    let supervisor = FinalizationSupervisor::new(DurableFinalizer::new(database.clone()));
    let providers = config.providers.clone();
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
    let model_registry = config
        .compile_model_router_registry()
        .expect("router registry");
    let known_providers: BTreeSet<String> = config.providers.keys().cloned().collect();
    let state = InferenceState::from_parts(
        finite,
        streaming,
        model_registry,
        Arc::new(ModelRouterAffinity::new()),
        known_providers,
        10 * 1024 * 1024,
        router,
    );
    Fixture { database, state }
}

fn compact_body() -> Bytes {
    serde_json::to_vec(&json!({
        "model": MODEL,
        "instructions": "Summarize the retained history.",
        "input": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "first turn"}]},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "first answer"}]},
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "second turn"}]}
        ]
    }))
    .expect("compact body serializes")
    .into()
}

// ---------------------------------------------------------------------------
// Capability classification
// ---------------------------------------------------------------------------

#[test]
fn the_capability_predicate_follows_the_operator_facts() {
    let mut provider = ProviderConfig {
        id: PROVIDER.to_owned(),
        ..Default::default()
    };
    let profiles = BTreeMap::from([(
        PROVIDER.to_owned(),
        vec![profile(WireSurface::OpenaiResponses)],
    )]);

    // No provider at all: there is no surface on which the capability could
    // be advertised.
    assert!(!remote_compaction_capable(&BTreeMap::new(), &profiles));

    // A Responses surface that advertises nothing is not compact-capable.
    let mut providers = BTreeMap::from([(PROVIDER.to_owned(), provider.clone())]);
    assert!(!remote_compaction_capable(&providers, &profiles));

    // The flag without a compact path is not a native v1 capability.
    provider.wire_surfaces.insert(
        "openai_responses".to_owned(),
        ProviderWireSurfaceConfig {
            path_template: "/responses".to_owned(),
            supports_remote_compaction_v1: true,
            compact_path_template: None,
            ..Default::default()
        },
    );
    providers = BTreeMap::from([(PROVIDER.to_owned(), provider.clone())]);
    assert!(!remote_compaction_capable(&providers, &profiles));

    // The flag with a compact path is a native v1 capability.
    provider.wire_surfaces.insert(
        "openai_responses".to_owned(),
        ProviderWireSurfaceConfig {
            path_template: "/responses".to_owned(),
            supports_remote_compaction_v1: true,
            compact_path_template: Some("/responses/compact".to_owned()),
            ..Default::default()
        },
    );
    providers = BTreeMap::from([(PROVIDER.to_owned(), provider.clone())]);
    assert!(remote_compaction_capable(&providers, &profiles));

    // The capability only counts on the Responses surface: a chat-only
    // provider cannot serve the compact operation.
    let mut chat_provider = provider.clone();
    chat_provider.wire_surfaces.clear();
    chat_provider.wire_surfaces.insert(
        "openai_chat_completions".to_owned(),
        ProviderWireSurfaceConfig {
            path_template: "/chat/completions".to_owned(),
            supports_remote_compaction_v1: true,
            compact_path_template: Some("/chat/completions".to_owned()),
            ..Default::default()
        },
    );
    let chat_profiles = BTreeMap::from([(
        PROVIDER.to_owned(),
        vec![profile(WireSurface::OpenaiChatCompletions)],
    )]);
    assert!(!remote_compaction_capable(
        &BTreeMap::from([(PROVIDER.to_owned(), chat_provider)]),
        &chat_profiles
    ));

    // A provider without a configured profile has no reachable surface.
    assert!(!remote_compaction_capable(&providers, &BTreeMap::new()));
}

#[test]
fn the_capability_outcome_is_distinguishable_from_the_selection_failure() {
    let capability = EndpointError::RemoteCompactionUnsupported;
    let selection = EndpointError::NoEligibleRoute;
    assert_eq!(capability.status(), StatusCode::NOT_IMPLEMENTED);
    assert_eq!(selection.status(), StatusCode::SERVICE_UNAVAILABLE);
    assert_ne!(capability.status(), selection.status());

    let capability_body: Value = serde_json::from_slice(&endpoint_error_body_for(
        eggpool::wire::ir::ClientSurface::Responses,
        &capability,
    ))
    .expect("capability body is JSON");
    let selection_body: Value = serde_json::from_slice(&endpoint_error_body_for(
        eggpool::wire::ir::ClientSurface::Responses,
        &selection,
    ))
    .expect("selection body is JSON");
    assert_eq!(
        capability_body["error"]["code"],
        "remote_compaction_unsupported"
    );
    assert!(
        selection_body["error"]["code"].is_null(),
        "the ordinary selection failure keeps the generic envelope"
    );
}

#[test]
fn the_capability_failure_body_names_only_the_configuration_keys() {
    let body = endpoint_error_body_for(
        eggpool::wire::ir::ClientSurface::Responses,
        &EndpointError::RemoteCompactionUnsupported,
    );
    let text = String::from_utf8_lossy(&body);
    assert!(text.contains("supports_remote_compaction_v1"));
    assert!(text.contains("compact_path_template"));
    for forbidden in [
        SECRET_MARKER,
        ACCOUNT_MARKER,
        PROVIDER,
        ACCOUNT,
        MODEL,
        "sk-",
    ] {
        assert!(
            !text.contains(forbidden),
            "the capability failure body must not disclose {forbidden:?}"
        );
    }
}

// ---------------------------------------------------------------------------
// Endpoint behavior
// ---------------------------------------------------------------------------

#[tokio::test]
async fn a_compact_request_without_a_capable_surface_fails_with_the_capability_outcome() {
    let upstream = Upstream::start(200, br#"{"id":"compact-1","output":[]}"#.to_vec());
    let fixture = build_fixture(
        &upstream.base_url(),
        &[ProviderSpec::new(PROVIDER, ACCOUNT, 1, &[MODEL], false)],
    )
    .await;

    let error = execute_compact_finite(
        &fixture.state,
        compact_body(),
        HeaderMap::new(),
        None,
        new_proxy_request_id(),
    )
    .await
    .expect_err("no eligible surface advertises the capability");
    assert!(
        matches!(error, EndpointError::RemoteCompactionUnsupported),
        "the capability outcome is reported, not the generic selection failure"
    );
    assert_eq!(error.status(), StatusCode::NOT_IMPLEMENTED);
    assert_eq!(
        upstream.count(),
        0,
        "the capability outcome dispatches nothing upstream"
    );

    // No provider attempt, no quota reservation, and no durable attribution
    // row exist for a capability failure.
    for table in ["request_attempts", "requests", "reservations"] {
        let count = fixture
            .database
            .call(move |connection| {
                let mut statement = connection.prepare(&format!("SELECT COUNT(*) FROM {table}"))?;
                statement.query_row([], |row| row.get::<_, i64>(0))
            })
            .await
            .unwrap_or_else(|error| panic!("{table} count reads: {error}"));
        assert_eq!(count, 0, "a capability failure creates no {table} row");
    }

    fixture.database.close().await.expect("database closes");
    upstream.join().await;
}

#[tokio::test]
async fn a_compact_request_against_a_capable_surface_reaches_the_upstream() {
    let upstream = Upstream::start(200, br#"{"id":"compact-1","output":[]}"#.to_vec());
    let fixture = build_fixture(
        &upstream.base_url(),
        &[ProviderSpec::new(PROVIDER, ACCOUNT, 1, &[MODEL], true)],
    )
    .await;

    let (execution, _virtual) = execute_compact_finite(
        &fixture.state,
        compact_body(),
        HeaderMap::new(),
        None,
        new_proxy_request_id(),
    )
    .await
    .expect("a capable surface serves the compact operation");
    assert_eq!(execution.response.status, StatusCode::OK);
    assert_eq!(
        upstream.count(),
        1,
        "the compact operation reaches the upstream"
    );
    let lines = upstream.request_lines();
    assert!(
        lines.iter().any(|line| line.contains("/responses/compact")),
        "the configured compact path template is used: {lines:?}"
    );
    execution.mark_started();
    execution
        .complete(DownstreamResult::Delivered)
        .await
        .expect("compact finalization completes");

    fixture.database.close().await.expect("database closes");
    upstream.join().await;
}

#[tokio::test]
async fn a_missing_capability_is_reported_even_when_no_account_is_routable() {
    let upstream = Upstream::start(200, br#"{"id":"compact-1","output":[]}"#.to_vec());
    // The installation owns a provider configuration but no capable surface,
    // and the requested model is served by nobody. The missing capability is
    // the actionable fact, so the typed capability outcome is reported rather
    // than the ordinary selection failure.
    let fixture = build_fixture(
        &upstream.base_url(),
        &[ProviderSpec::new(PROVIDER, ACCOUNT, 1, &[MODEL], false)],
    )
    .await;
    let body = serde_json::to_vec(&json!({
        "model": "model-without-an-account",
        "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "history"}]}]
    }))
    .expect("body serializes")
    .into();

    let error = execute_compact_finite(
        &fixture.state,
        body,
        HeaderMap::new(),
        None,
        new_proxy_request_id(),
    )
    .await
    .expect_err("no provider surface advertises the capability");
    assert!(
        matches!(error, EndpointError::RemoteCompactionUnsupported),
        "the installation-level capability answer is reported even without a routable account"
    );
    assert_eq!(error.status(), StatusCode::NOT_IMPLEMENTED);
    assert_eq!(
        upstream.count(),
        0,
        "the capability outcome dispatches nothing"
    );

    fixture.database.close().await.expect("database closes");
    upstream.join().await;
}

#[tokio::test]
async fn the_ordinary_selection_failure_still_reports_no_eligible_account() {
    let upstream = Upstream::start(200, br#"{"id":"compact-1","output":[]}"#.to_vec());
    // A capable installation whose account cannot serve the model: the
    // capability exists, so the failure is the ordinary selection failure.
    let fixture = build_fixture(
        &upstream.base_url(),
        &[ProviderSpec::new(PROVIDER, ACCOUNT, 1, &[MODEL], true)],
    )
    .await;
    let body = serde_json::to_vec(&json!({
        "model": "model-without-an-account",
        "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "history"}]}]
    }))
    .expect("body serializes")
    .into();

    let error = execute_compact_finite(
        &fixture.state,
        body,
        HeaderMap::new(),
        None,
        new_proxy_request_id(),
    )
    .await
    .expect_err("no account serves the model");
    assert!(
        matches!(error, EndpointError::NoEligibleRoute),
        "a missing account stays the ordinary selection failure"
    );
    assert_eq!(error.status(), StatusCode::SERVICE_UNAVAILABLE);
    assert_eq!(
        upstream.count(),
        0,
        "a selection failure dispatches nothing"
    );

    fixture.database.close().await.expect("database closes");
    upstream.join().await;
}

#[tokio::test]
async fn a_routable_provider_without_the_capability_reports_the_capability_outcome() {
    let upstream = Upstream::start(200, br#"{"id":"compact-1","output":[]}"#.to_vec());
    // The installation owns a capable provider, so the installation-level
    // answer is "capable". The only provider that serves the requested model
    // is not capable, so the route-level answer is "not capable" and the
    // ordinary selection failure would be indistinguishable from a missing
    // account.
    let fixture = build_fixture(
        &upstream.base_url(),
        &[
            ProviderSpec::new(
                "provider-capable",
                "account-capable",
                1,
                &["other-model"],
                true,
            ),
            ProviderSpec::new(PROVIDER, ACCOUNT, 2, &[MODEL], false),
        ],
    )
    .await;

    let error = execute_compact_finite(
        &fixture.state,
        compact_body(),
        HeaderMap::new(),
        None,
        new_proxy_request_id(),
    )
    .await
    .expect_err("the routable provider cannot serve the operation");
    assert!(
        matches!(error, EndpointError::RemoteCompactionUnsupported),
        "a route-level capability miss reports the capability outcome"
    );
    assert_eq!(error.status(), StatusCode::NOT_IMPLEMENTED);
    assert_eq!(
        upstream.count(),
        0,
        "the capability outcome dispatches nothing upstream"
    );

    fixture.database.close().await.expect("database closes");
    upstream.join().await;
}

// ---------------------------------------------------------------------------
// Streaming asymmetry
// ---------------------------------------------------------------------------

#[test]
fn the_streaming_coordinator_carries_no_compaction_capabilities() {
    // ADR-0003 records the finite-versus-streaming response-context
    // asymmetry as intentional: the compact operation is finite-only, so the
    // streaming coordinator must never attach compaction capabilities.
    let streaming = include_str!("../src/coordinator/streaming/coordinator.rs");
    assert!(
        !streaming.contains("with_compaction"),
        "the streaming coordinator must not attach compaction capabilities"
    );
    let finite = include_str!("../src/coordinator/finite.rs");
    assert!(
        finite.contains("with_compaction"),
        "the finite response context resolves compaction capabilities"
    );
}
