//! R013 qualification for wire-policy safety, acceptance, and request authority.

use std::{
    collections::BTreeMap,
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};

use axum::body::Body;
#[cfg(feature = "test-support")]
use eggpool::reload::ReloadTestFault;
use eggpool::{
    Config,
    config::{
        AccountConfig, ProviderAuthConfig, ProviderConfig, ProviderStaticModelConfig,
        ProviderWireSurfaceConfig,
    },
    coordinator::{
        NegotiationRole, WireCandidate, WireResolver, WireResolverConfig, WireResolverConfigError,
    },
    db::{Database, DatabaseConfig, MigrationRunner},
    reload::ReloadResultCategory,
    runtime_lifecycle::{ProcessRuntime, RuntimeGenerationFactory, RuntimeManager},
    server::{AppState, build_router},
    wire::{ConfiguredWireProfile, WireCodecId, WireProfileDefinition, WireSurface},
};
use http::Request;
use serde_json::json;
use tempfile::TempDir;
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::TcpListener,
};
use tower::ServiceExt;

fn toml_for(config: &Config) -> Vec<u8> {
    toml::to_string(config)
        .expect("configuration serializes")
        .into_bytes()
}

fn config_accepts(config: &Config) -> bool {
    Config::from_toml_bytes("config.toml", &toml_for(config)).is_ok()
}

#[test]
fn wire_configuration_matches_python_bounds_exactly() {
    let mut config = Config::default();

    config.routing.wire_negotiation.max_concurrent_per_provider = 1;
    assert!(config_accepts(&config));
    config.routing.wire_negotiation.max_concurrent_per_provider = 8;
    assert!(config_accepts(&config));
    for value in [0, 9] {
        config.routing.wire_negotiation.max_concurrent_per_provider = value;
        assert!(!config_accepts(&config));
    }
    config = Config::default();

    for field in ["min_negotiation_interval_s", "rejection_cooldown_s"] {
        if field == "min_negotiation_interval_s" {
            config.routing.wire_negotiation.min_negotiation_interval_s = 0.0;
        } else {
            config.routing.wire_negotiation.rejection_cooldown_s = 0.0;
        }
        assert!(config_accepts(&config));
        if field == "min_negotiation_interval_s" {
            config.routing.wire_negotiation.min_negotiation_interval_s = 1800.0;
        } else {
            config.routing.wire_negotiation.rejection_cooldown_s = 1800.0;
        }
        assert!(config_accepts(&config));
        if field == "min_negotiation_interval_s" {
            config.routing.wire_negotiation.min_negotiation_interval_s = -f64::EPSILON;
        } else {
            config.routing.wire_negotiation.rejection_cooldown_s = -f64::EPSILON;
        }
        assert!(!config_accepts(&config));
        if field == "min_negotiation_interval_s" {
            config.routing.wire_negotiation.min_negotiation_interval_s = 1801.0;
        } else {
            config.routing.wire_negotiation.rejection_cooldown_s = 1801.0;
        }
        assert!(!config_accepts(&config));
        config = Config::default();
    }

    config.routing.wire_negotiation.learned_preference_ttl_s = f64::EPSILON;
    assert!(config_accepts(&config));
    config.routing.wire_negotiation.learned_preference_ttl_s = 604_800.0;
    assert!(config_accepts(&config));
    config.routing.wire_negotiation.learned_preference_ttl_s = 0.0;
    assert!(!config_accepts(&config));
    config.routing.wire_negotiation.learned_preference_ttl_s = 604_801.0;
    assert!(!config_accepts(&config));

    config = Config::default();
    config.routing.wire_negotiation.cache_max_entries = 1;
    assert!(config_accepts(&config));
    config.routing.wire_negotiation.cache_max_entries = 65_536;
    assert!(config_accepts(&config));
    for value in [0, 65_537] {
        config.routing.wire_negotiation.cache_max_entries = value;
        assert!(!config_accepts(&config));
    }
}

#[test]
fn programmatic_invalid_wire_policy_fails_closed_without_panicking() {
    let invalid_values = [-1.0, 0.0, f64::MAX, f64::INFINITY, f64::NAN];
    for value in invalid_values {
        let config = eggpool::config::WireNegotiationConfig {
            learned_preference_ttl_s: value,
            ..Default::default()
        };
        assert!(matches!(
            WireResolverConfig::from_config(&config),
            Err(WireResolverConfigError::Duration { .. })
        ));
    }
}

#[tokio::test]
async fn invalid_startup_and_live_staging_return_errors_instead_of_panicking() {
    let database = Database::open(DatabaseConfig::default())
        .await
        .expect("database opens");
    let mut config = Config::default();
    config.routing.wire_negotiation.min_negotiation_interval_s = f64::MAX;

    assert!(ProcessRuntime::new_with_config(database.clone(), &config).is_err());
    let process = ProcessRuntime::new(database.clone());
    assert!(process.stage_wire_resolver_policy(&config).is_err());
    database.close().await.expect("database closes");
}

fn policy(
    cache_capacity: usize,
    max_provider_state: usize,
    max_metric_labels: usize,
    concurrency: usize,
) -> WireResolverConfig {
    WireResolverConfig {
        cache_capacity,
        max_provider_state,
        max_metric_labels,
        max_concurrent_per_provider: concurrency,
        learned_ttl: Duration::from_secs(60),
        rejection_ttl: Duration::from_secs(60),
        min_negotiation_interval: Duration::ZERO,
        ..WireResolverConfig::default()
    }
}

fn chat_profile(path: &str, priority: u32) -> ConfiguredWireProfile {
    ConfiguredWireProfile {
        definition: WireProfileDefinition {
            surface: WireSurface::OpenaiChatCompletions,
            request_codec: WireCodecId::OpenaiChat,
            response_codec: WireCodecId::OpenaiChat,
            stream_codec: WireCodecId::OpenaiChatSse,
        },
        path_template: path.to_owned(),
        stream_path_template: None,
        priority,
    }
}

#[tokio::test]
async fn rollback_restores_policy_and_bounds_before_returning() {
    let resolver = WireResolver::new(policy(2, 1, 1, 1));
    let now = Instant::now();
    let first = resolver.begin_negotiation("p0", "m", "f", now).await;
    let second = resolver.begin_negotiation("p1", "m", "f", now).await;
    assert_eq!(first.role(), NegotiationRole::Leader);
    assert_eq!(second.role(), NegotiationRole::Leader);

    let mut stage = resolver.stage_config(policy(16, 16, 16, 4));
    assert_eq!(resolver.config().cache_capacity, 2);
    stage.commit();
    for index in 0..16 {
        let model = format!("model-{index}");
        let resolved = resolver.resolve(
            "provider",
            &model,
            vec![WireCandidate::new(chat_profile("/chat", 0), "static")],
            now,
        );
        resolver.accept(
            "provider",
            &model,
            &resolved.fingerprint,
            WireSurface::OpenaiChatCompletions,
            now,
        );
    }
    assert_eq!(resolver.snapshot().entries, 16);

    stage.rollback();
    let restored = resolver.config();
    assert_eq!(restored.cache_capacity, 2);
    assert_eq!(restored.max_provider_state, 1);
    assert_eq!(restored.max_metric_labels, 1);
    assert_eq!(restored.max_concurrent_per_provider, 1);
    assert!(resolver.snapshot().entries <= 2);

    first.finish(eggpool::coordinator::NegotiationResult::Rejected, now);
    second.finish(eggpool::coordinator::NegotiationResult::Rejected, now);
    assert!(resolver.snapshot().provider_gates <= 1);
    let next = resolver.begin_negotiation("p2", "m", "f", now).await;
    assert_eq!(next.role(), NegotiationRole::Leader);
    next.finish(eggpool::coordinator::NegotiationResult::Rejected, now);
}

#[tokio::test]
async fn rejected_reload_preserves_authority_and_accepted_reload_publishes_once() {
    let directory = tempfile::tempdir().expect("temporary directory");
    let database = open_migrated_database(&directory).await;
    let mut config = Config::default();
    config.routing.wire_negotiation.cache_max_entries = 7;
    let process = ProcessRuntime::new_with_config(database.clone(), &config)
        .expect("process policy prepares");
    let candidate =
        RuntimeGenerationFactory::prepare(&process, config.clone(), "r013-initial".to_owned(), 1)
            .await
            .expect("generation prepares");
    let manager = RuntimeManager::new(candidate.transfer().expect("generation transfers"));
    let reload = process.reload_service(manager.clone());
    let resolver = process.wire_profile_resolver();
    let before = resolver.config();

    let mut restart = config.clone();
    restart.server.port += 1;
    let result = reload
        .reload_bytes("config.toml", toml_for(&restart), None)
        .await;
    assert_eq!(result.category, ReloadResultCategory::RestartRequired);
    assert_eq!(manager.publication_epoch(), 0);
    assert_eq!(resolver.config(), before);

    let mut invalid = config.clone();
    invalid.routing.wire_negotiation.cache_max_entries = 65_537;
    let result = reload
        .reload_bytes("config.toml", toml_for(&invalid), None)
        .await;
    assert_eq!(result.category, ReloadResultCategory::ValidationFailed);
    assert_eq!(resolver.config(), before);
    assert!(!process.diagnostics(&manager).publication.admission_closed);

    let mut accepted = config;
    accepted.routing.wire_negotiation.enabled = false;
    let result = reload
        .reload_bytes("config.toml", toml_for(&accepted), None)
        .await;
    assert_eq!(result.category, ReloadResultCategory::Applied);
    assert_eq!(manager.publication_epoch(), 1);
    assert!(!resolver.config().enabled);
    assert!(!process.diagnostics(&manager).publication.admission_closed);

    let _ = process
        .task_supervisor()
        .shutdown_with_timeout(Duration::from_secs(1))
        .await;
    let _ = manager
        .close_for_shutdown(Duration::from_secs(1), false)
        .await;
    database.close().await.expect("database closes");
}

#[tokio::test]
async fn retirement_backlog_rejection_preserves_process_wire_authority() {
    let directory = tempfile::tempdir().expect("temporary directory");
    let database = open_migrated_database(&directory).await;
    let config = Config::default();
    let process = ProcessRuntime::new_with_config(database.clone(), &config)
        .expect("process policy prepares");
    let candidate = RuntimeGenerationFactory::prepare(
        &process,
        config.clone(),
        "r013-backlog-initial".to_owned(),
        1,
    )
    .await
    .expect("generation prepares");
    let manager = RuntimeManager::new(candidate.transfer().expect("generation transfers"));
    let reload = process.reload_service(manager.clone());
    let resolver = process.wire_profile_resolver();
    let before_policy = resolver.config();
    let mut leases = Vec::new();
    let mut next = config;

    for generation in 0..eggpool::runtime_lifecycle::MAX_RETIRING_GENERATIONS {
        leases.push(manager.acquire().await.expect("active lease"));
        next.server.max_request_body_bytes += generation as u64 + 1;
        let result = reload
            .reload_bytes("config.toml", toml_for(&next), None)
            .await;
        assert_eq!(result.category, ReloadResultCategory::Applied);
    }
    assert_eq!(
        manager.retiring_slot_count(),
        eggpool::runtime_lifecycle::MAX_RETIRING_GENERATIONS
    );

    next.server.max_request_body_bytes += 1;
    let result = reload
        .reload_bytes("config.toml", toml_for(&next), None)
        .await;
    assert_eq!(result.category, ReloadResultCategory::RetirementBacklog);
    assert_eq!(
        result.active_generation_id,
        1 + u64::from(eggpool::runtime_lifecycle::MAX_RETIRING_GENERATIONS as u32)
    );
    assert_eq!(
        manager.publication_epoch(),
        eggpool::runtime_lifecycle::MAX_RETIRING_GENERATIONS as u64
    );
    assert_eq!(resolver.config(), before_policy);
    assert!(!manager.admission_closed());

    drop(leases);
    manager.drain_retirements().await;
    let _ = process
        .task_supervisor()
        .shutdown_with_timeout(Duration::from_secs(1))
        .await;
    let _ = manager
        .close_for_shutdown(Duration::from_secs(1), false)
        .await;
    database.close().await.expect("database closes");
}

#[cfg(feature = "test-support")]
#[tokio::test]
async fn reload_fault_and_rejection_matrix_preserves_old_authority() {
    let directory = tempfile::tempdir().expect("temporary directory");
    let database = open_migrated_database(&directory).await;
    let config = Config::default();
    let process = ProcessRuntime::new_with_config(database.clone(), &config)
        .expect("process policy prepares");
    let candidate = RuntimeGenerationFactory::prepare(
        &process,
        config.clone(),
        "r013-matrix-initial".to_owned(),
        1,
    )
    .await
    .expect("generation prepares");
    let manager = RuntimeManager::new(candidate.transfer().expect("generation transfers"));
    let reload = process.reload_service(manager.clone());
    let resolver = process.wire_profile_resolver();
    let before_policy = resolver.config();

    let unchanged = |result: &eggpool::reload::ReloadResult| {
        assert_eq!(result.active_generation_id, 1);
        assert_eq!(manager.publication_epoch(), 0);
        assert_eq!(resolver.config(), before_policy);
        assert!(!manager.admission_closed());
        assert!(!process.diagnostics(&manager).reload.in_progress);
    };

    let result = reload
        .reload_bytes("config.toml", toml_for(&config), None)
        .await;
    assert_eq!(result.category, ReloadResultCategory::Noop);
    unchanged(&result);

    let mut restart = config.clone();
    restart.server.port += 1;
    let result = reload
        .reload_bytes("config.toml", toml_for(&restart), None)
        .await;
    assert_eq!(result.category, ReloadResultCategory::RestartRequired);
    unchanged(&result);

    let mut mixed = config.clone();
    mixed.server.port += 1;
    mixed.server.max_request_body_bytes += 1;
    let result = reload
        .reload_bytes("config.toml", toml_for(&mixed), None)
        .await;
    assert_eq!(result.category, ReloadResultCategory::RestartRequired);
    unchanged(&result);

    let result = reload
        .reload_bytes("config.toml", b"[server\n".to_vec(), None)
        .await;
    assert_eq!(result.category, ReloadResultCategory::ValidationFailed);
    unchanged(&result);

    let mut invalid_wire = config.clone();
    invalid_wire.routing.wire_negotiation.cache_max_entries = 65_537;
    let result = reload
        .reload_bytes("config.toml", toml_for(&invalid_wire), None)
        .await;
    assert_eq!(result.category, ReloadResultCategory::ValidationFailed);
    unchanged(&result);

    let result = reload
        .reload_bytes(
            "config.toml",
            toml_for(&config),
            Some("stale-digest".to_owned()),
        )
        .await;
    assert_eq!(result.category, ReloadResultCategory::StaleDigest);
    unchanged(&result);

    let candidate_failure = Config {
        providers: BTreeMap::from([(
            "bad".to_owned(),
            ProviderConfig {
                id: "bad".to_owned(),
                // Pass config's lightweight URL checks but fail the provider
                // transport URI parser during candidate construction.
                base_url: "http://[::1".to_owned(),
                ..ProviderConfig::default()
            },
        )]),
        ..Config::default()
    };
    let result = reload
        .reload_bytes("config.toml", toml_for(&candidate_failure), None)
        .await;
    assert_eq!(result.category, ReloadResultCategory::Aborted);
    assert_eq!(result.reason_code, "candidate_prepare_failed");
    unchanged(&result);

    for fault in [
        ReloadTestFault::TaskPreflight,
        ReloadTestFault::TaskCommit,
        ReloadTestFault::PersistenceBegin,
        ReloadTestFault::PersistenceApply,
        ReloadTestFault::PersistenceCommit,
    ] {
        reload.inject_test_fault(fault);
        let mut changed = config.clone();
        changed.server.max_request_body_bytes += 1;
        let result = reload
            .reload_bytes("config.toml", toml_for(&changed), None)
            .await;
        assert_eq!(result.category, ReloadResultCategory::Aborted);
        unchanged(&result);
    }

    let mut accepted = config;
    accepted.routing.wire_negotiation.enabled = false;
    let result = reload
        .reload_bytes("config.toml", toml_for(&accepted), None)
        .await;
    assert_eq!(result.category, ReloadResultCategory::Applied);
    assert_eq!(manager.publication_epoch(), 1);
    assert!(!resolver.config().enabled);

    let _ = process
        .task_supervisor()
        .shutdown_with_timeout(Duration::from_secs(1))
        .await;
    let _ = manager
        .close_for_shutdown(Duration::from_secs(1), false)
        .await;
    database.close().await.expect("database closes");
}

struct LocalProvider {
    observed_paths: Arc<Mutex<Vec<String>>>,
    task: tokio::task::JoinHandle<()>,
    base_url: String,
}

impl LocalProvider {
    async fn start(expected_requests: usize) -> Self {
        let listener = TcpListener::bind(("127.0.0.1", 0))
            .await
            .expect("provider listener");
        let port = listener.local_addr().expect("provider address").port();
        let observed_paths = Arc::new(Mutex::new(Vec::new()));
        let observed = Arc::clone(&observed_paths);
        let task = tokio::spawn(async move {
            for _ in 0..expected_requests {
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
                    if request.windows(4).any(|window| window == b"\r\n\r\n") {
                        break;
                    }
                }
                let first_line = request
                    .split(|byte| *byte == b'\n')
                    .next()
                    .map(String::from_utf8_lossy)
                    .unwrap_or_default();
                let path = first_line
                    .split_whitespace()
                    .nth(1)
                    .unwrap_or_default()
                    .to_owned();
                observed
                    .lock()
                    .expect("provider paths lock")
                    .push(path.clone());
                let (status, body) = if path == "/messages" {
                    (
                        200,
                        serde_json::to_vec(&json!({
                            "id": "r013-anthropic",
                            "model": "local-model",
                            "content": [{"type": "text", "text": "ok"}],
                            "stop_reason": "end_turn",
                            "usage": {"input_tokens": 1, "output_tokens": 1}
                        }))
                        .expect("anthropic response"),
                    )
                } else {
                    (200, serde_json::to_vec(&json!({
                        "id": "r013-openai",
                        "model": "local-model",
                        "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
                    }))
                    .expect("openai response"))
                };
                let head = format!(
                    "HTTP/1.1 {status} OK\r\ncontent-length: {}\r\nconnection: close\r\n\r\n",
                    body.len()
                );
                let _ = socket.write_all(head.as_bytes()).await;
                let _ = socket.write_all(&body).await;
            }
        });
        Self {
            observed_paths,
            task,
            base_url: format!("http://127.0.0.1:{port}"),
        }
    }
}

async fn open_migrated_database(directory: &TempDir) -> Database {
    let database = Database::open(DatabaseConfig {
        path: directory
            .path()
            .join("runtime.sqlite3")
            .to_string_lossy()
            .into_owned(),
        ..DatabaseConfig::default()
    })
    .await
    .expect("database opens");
    MigrationRunner::new(&database)
        .run()
        .await
        .expect("migrations run");
    database
}

fn local_config(base_url: String) -> Config {
    Config {
        providers: BTreeMap::from([(
            "local".to_owned(),
            ProviderConfig {
                id: "local".to_owned(),
                base_url,
                protocols: vec!["openai".to_owned(), "anthropic".to_owned()],
                accounts: vec![AccountConfig {
                    name: "local-account".to_owned(),
                    ..AccountConfig::default()
                }],
                auth: ProviderAuthConfig {
                    mode: "none".to_owned(),
                    ..ProviderAuthConfig::default()
                },
                static_models: vec![ProviderStaticModelConfig {
                    id: "local-model".to_owned(),
                    protocol: Some("openai".to_owned()),
                    ..ProviderStaticModelConfig::default()
                }],
                wire_surfaces: BTreeMap::from([
                    (
                        "openai_chat_completions".to_owned(),
                        ProviderWireSurfaceConfig {
                            path_template: "/chat/completions".to_owned(),
                            priority: 0,
                            ..ProviderWireSurfaceConfig::default()
                        },
                    ),
                    (
                        "anthropic_messages".to_owned(),
                        ProviderWireSurfaceConfig {
                            path_template: "/messages".to_owned(),
                            priority: 1,
                            ..ProviderWireSurfaceConfig::default()
                        },
                    ),
                ]),
                ..ProviderConfig::default()
            },
        )]),
        ..Config::default()
    }
}

#[tokio::test]
async fn real_axum_inference_observes_accepted_policy_and_not_rejected_policy() {
    let provider = LocalProvider::start(3).await;
    let directory = tempfile::tempdir().expect("temporary directory");
    let database = open_migrated_database(&directory).await;
    database
        .call(|connection| {
            connection.execute(
                "INSERT INTO accounts (id, name, api_key_env, enabled, weight, provider_id) VALUES (1, 'local-account', 'UNUSED', 1, 1.0, 'local')",
                [],
            )?;
            connection.execute(
                "INSERT INTO models (model_id, protocol, provider_id, resolution_status) VALUES ('local-model', 'openai', 'local', 'resolved')",
                [],
            )?;
            Ok(())
        })
        .await
        .expect("provider rows insert");
    let config = local_config(provider.base_url.clone());
    let process = ProcessRuntime::new_with_config(database.clone(), &config)
        .expect("process policy prepares");
    let resolver = process.wire_profile_resolver();
    let profiles = vec![
        chat_profile("/chat/completions", 0),
        ConfiguredWireProfile {
            definition: WireProfileDefinition {
                surface: WireSurface::AnthropicMessages,
                request_codec: WireCodecId::AnthropicMessages,
                response_codec: WireCodecId::AnthropicMessages,
                stream_codec: WireCodecId::AnthropicMessagesSse,
            },
            path_template: "/messages".to_owned(),
            stream_path_template: None,
            priority: 0,
        },
    ];
    let seeded = resolver.resolve(
        "local",
        "local-model",
        profiles
            .into_iter()
            .map(|profile| WireCandidate::new(profile, "static"))
            .collect(),
        Instant::now(),
    );
    resolver.accept(
        "local",
        "local-model",
        &seeded.fingerprint,
        WireSurface::AnthropicMessages,
        Instant::now(),
    );

    let candidate =
        RuntimeGenerationFactory::prepare(&process, config.clone(), "r013-local".to_owned(), 1)
            .await
            .expect("generation prepares");
    let manager = RuntimeManager::new(candidate.transfer().expect("generation transfers"));
    let reload = process.reload_service(manager.clone());
    let app = build_router(AppState::from_runtime(
        config.clone(),
        database.clone(),
        Arc::new(manager.clone()),
    ));
    let body = serde_json::to_vec(&json!({
        "model": "local-model",
        "messages": [{"role": "user", "content": "hello"}]
    }))
    .expect("request body");
    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/v1/chat/completions")
                .header("content-type", "application/json")
                .body(Body::from(body.clone()))
                .expect("request builds"),
        )
        .await
        .expect("baseline response");
    assert_eq!(response.status(), http::StatusCode::OK);

    let mut invalid = config.clone();
    invalid.routing.wire_negotiation.enabled = false;
    invalid.routing.wire_negotiation.cache_max_entries = 65_537;
    assert_eq!(
        reload
            .reload_bytes("config.toml", toml_for(&invalid), None)
            .await
            .category,
        ReloadResultCategory::ValidationFailed
    );
    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/v1/chat/completions")
                .header("content-type", "application/json")
                .body(Body::from(body.clone()))
                .expect("request builds"),
        )
        .await
        .expect("rejected-reload response");
    assert_eq!(response.status(), http::StatusCode::OK);

    let mut accepted = config;
    accepted.routing.wire_negotiation.enabled = false;
    assert_eq!(
        reload
            .reload_bytes("config.toml", toml_for(&accepted), None)
            .await
            .category,
        ReloadResultCategory::Applied
    );
    let response = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/v1/chat/completions")
                .header("content-type", "application/json")
                .body(Body::from(body))
                .expect("request builds"),
        )
        .await
        .expect("accepted-reload response");
    assert_eq!(response.status(), http::StatusCode::OK);
    assert_eq!(manager.publication_epoch(), 1);
    assert!(!resolver.config().enabled);

    let paths = provider
        .observed_paths
        .lock()
        .expect("provider paths lock")
        .clone();
    assert_eq!(paths, vec!["/messages", "/messages", "/chat/completions"]);

    let _ = process
        .task_supervisor()
        .shutdown_with_timeout(Duration::from_secs(1))
        .await;
    let _ = manager
        .close_for_shutdown(Duration::from_secs(1), false)
        .await;
    database.close().await.expect("database closes");
    provider.task.await.expect("provider joins");
}
