//! Plan 202 status command and provider health summary coverage.
//!
//! Covers the CLI contract, pure provider/proxy aggregation, secret-free
//! guarantees, deterministic ordering, the authenticated `/api/status`
//! endpoint, and the latest-ping repository helper. The status surface is
//! read-only: no outbound provider requests, no circuit mutation, no quota
//! consumption.

use std::sync::Arc;

use axum::{body::Body, http::Request};
use clap::Parser;
use eggpool::{
    Cli, Command as EggpoolCommand, Config,
    db::{AccountRepository, Database, DatabaseConfig, MigrationRunner, PingRepository},
    health::{AccountHealthSnapshot, BackoffReason, CircuitState, CircuitStats},
    operations::status as status_service,
    runtime_lifecycle::{ProcessRuntime, RuntimeGenerationFactory, RuntimeManager},
    server::{AppState, build_router},
};
use http_body_util::BodyExt;
use tower::ServiceExt;

fn account_input(
    name: &str,
    provider: &str,
    enabled: bool,
    routable: bool,
    success: bool,
    failure: bool,
) -> status_service::AccountStatusInput {
    status_service::AccountStatusInput {
        account_name: name.to_owned(),
        provider_id: provider.to_owned(),
        enabled,
        has_usable_credentials: true,
        routable: enabled && routable,
        in_backoff: enabled && !routable,
        backoff_reason: if failure {
            Some(BackoffReason::RateLimited)
        } else {
            None
        },
        circuit_open: false,
        has_success_observation: success,
        has_failure_observation: failure,
        has_model_quarantine: false,
        auth_terminal: false,
    }
}

fn provider_input(
    id: &str,
    accounts: Vec<status_service::AccountStatusInput>,
) -> status_service::ProviderStatusInput {
    status_service::ProviderStatusInput {
        provider_id: id.to_owned(),
        accounts,
        ping: status_service::PingEvidence::never(),
        catalog_model_count: Some(2),
        stale_after_secs: 7200,
    }
}

#[test]
fn status_cli_parses_with_and_without_json() {
    let plain = Cli::try_parse_from(["eggpool", "status"]).expect("status parses");
    assert!(matches!(
        plain.command,
        Some(EggpoolCommand::Status { json: false })
    ));
    let json = Cli::try_parse_from(["eggpool", "status", "--json"]).expect("status --json parses");
    assert!(matches!(
        json.command,
        Some(EggpoolCommand::Status { json: true })
    ));
}

#[test]
fn provider_rows_are_one_per_provider_and_deterministically_ordered() {
    let mut providers = vec![
        status_service::aggregate_provider(&provider_input(
            "openrouter",
            vec![account_input("b1", "openrouter", true, true, true, false)],
        )),
        status_service::aggregate_provider(&provider_input(
            "minimax",
            vec![account_input("a1", "minimax", true, true, true, false)],
        )),
    ];
    status_service::sort_providers(&mut providers);
    let ids: Vec<&str> = providers
        .iter()
        .map(|provider| provider.provider_id.as_str())
        .collect();
    assert_eq!(ids, vec!["minimax", "openrouter"]);
}

#[test]
fn proxy_is_ready_when_all_active_providers_ready() {
    let providers = vec![status_service::aggregate_provider(&provider_input(
        "opencode-go",
        vec![account_input("a1", "opencode-go", true, true, true, false)],
    ))];
    assert_eq!(providers[0].status, status_service::ProviderStatus::Ready);
    let readiness = status_service::evaluate_readiness(1, 1, true, 5, 5, true, true);
    assert!(readiness.ready);
    let (status, ready, _) =
        status_service::aggregate_proxy(&readiness, &providers, false, false, false);
    assert_eq!(status, status_service::ProxyStatus::Ready);
    assert!(ready);
}

#[test]
fn proxy_is_degraded_but_successful_when_one_provider_degrades() {
    let mut failing = account_input("b1", "minimax", true, false, false, true);
    failing.in_backoff = true;
    let providers = vec![
        status_service::aggregate_provider(&provider_input(
            "opencode-go",
            vec![account_input("a1", "opencode-go", true, true, true, false)],
        )),
        status_service::aggregate_provider(&provider_input(
            "minimax",
            vec![
                account_input("a2", "minimax", true, true, true, false),
                failing,
            ],
        )),
    ];
    assert_eq!(
        providers[1].status,
        status_service::ProviderStatus::Degraded
    );
    let readiness = status_service::evaluate_readiness(2, 2, true, 5, 5, true, true);
    let (status, ready, reason) =
        status_service::aggregate_proxy(&readiness, &providers, false, false, false);
    assert_eq!(status, status_service::ProxyStatus::Degraded);
    assert!(ready);
    assert!(reason.is_some());
}

#[test]
fn intentionally_disabled_provider_does_not_degrade_proxy() {
    let providers = vec![
        status_service::aggregate_provider(&provider_input(
            "opencode-go",
            vec![account_input("a1", "opencode-go", true, true, true, false)],
        )),
        status_service::aggregate_provider(&provider_input(
            "local-test",
            vec![account_input(
                "z1",
                "local-test",
                false,
                false,
                false,
                false,
            )],
        )),
    ];
    assert_eq!(
        providers[1].status,
        status_service::ProviderStatus::Disabled
    );
    let readiness = status_service::evaluate_readiness(2, 1, true, 5, 5, true, true);
    let (status, ready, _) =
        status_service::aggregate_proxy(&readiness, &providers, false, false, false);
    assert_eq!(status, status_service::ProxyStatus::Ready);
    assert!(ready);
}

#[test]
fn proxy_is_unready_without_usable_catalog() {
    let readiness = status_service::evaluate_readiness(1, 1, true, 0, 0, true, true);
    assert!(!readiness.ready);
    let (status, ready, _) = status_service::aggregate_proxy(&readiness, &[], false, false, false);
    assert_eq!(status, status_service::ProxyStatus::Unready);
    assert!(!ready);
}

#[test]
fn raw_ping_error_text_never_leaves_the_service() {
    use std::time::{SystemTime, UNIX_EPOCH};
    let sentinel = "SECRET_SENTINEL_sk-live-9f8e7d6c5b4a";
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs())
        .unwrap_or(0);
    let ping = eggpool::db::Ping {
        provider_id: "minimax".to_owned(),
        account_name: "a1".to_owned(),
        probed_at: "2026-09-16 12:00:00".to_owned(),
        latency_ms: Some(84),
        status_code: Some(429),
        error: Some(format!("upstream said {sentinel} with body")),
        model_count: 0,
    };
    let evidence = status_service::ping_evidence_for_provider(Some(&ping), now);
    assert!(evidence.observed);
    assert!(!evidence.success);
    let summary = status_service::aggregate_provider(&status_service::ProviderStatusInput {
        provider_id: "minimax".to_owned(),
        accounts: vec![account_input("a1", "minimax", true, false, false, true)],
        ping: evidence,
        catalog_model_count: Some(11),
        stale_after_secs: u64::MAX / 2,
    });
    let serialized = serde_json::to_value(&summary).expect("provider serializes");
    let rendered = serialized.to_string();
    assert!(!rendered.contains(sentinel));
    assert!(!rendered.contains("upstream said"));
    assert_eq!(summary.reason_code.as_deref(), Some("rate_limited"));
}

#[test]
fn health_snapshot_mapping_marks_auth_terminal_as_unavailable() {
    let snapshot = AccountHealthSnapshot {
        account_id: Some(1),
        account_name: "a1".to_owned(),
        is_healthy: false,
        health_state: "authentication_failed".to_owned(),
        last_check: 10.0,
        last_success: None,
        last_failure: Some(9.0),
        last_failure_category: Some(BackoffReason::AuthenticationFailed),
        consecutive_failures: 3,
        consecutive_cooldowns: 0,
        disabled_until: None,
        disabled_reason: "authentication_failed".to_owned(),
        cooldown_until: 0.0,
        disabled_models: std::collections::BTreeMap::new(),
        terminal_models: std::collections::BTreeSet::new(),
        circuit: CircuitStats {
            state: CircuitState::Closed,
            failure_count: 3,
            success_count: 0,
            last_failure_at: Some(9.0),
            last_state_change: 9.0,
            probe_in_flight: false,
        },
    };
    let input = status_service::account_input_from_snapshot(
        "a1",
        "x",
        true,
        true,
        Some(&snapshot),
        10.0,
        false,
    );
    assert!(!input.routable);
    assert!(input.auth_terminal);
}

#[tokio::test]
async fn latest_ping_helper_returns_one_row_per_account_pair() {
    let directory = tempfile::tempdir().expect("temp root");
    let database = Database::open(DatabaseConfig {
        path: directory.path().join("usage.sqlite3").display().to_string(),
        ..DatabaseConfig::default()
    })
    .await
    .expect("database opens");
    MigrationRunner::new(&database)
        .run()
        .await
        .expect("migrations run");
    let repository = PingRepository::new(&database);
    repository
        .record(
            "a".to_owned(),
            "a1".to_owned(),
            Some(10),
            Some(200),
            None,
            3,
        )
        .await
        .expect("ping records");
    repository
        .record(
            "a".to_owned(),
            "a1".to_owned(),
            Some(12),
            Some(200),
            None,
            4,
        )
        .await
        .expect("ping records");
    repository
        .record(
            "b".to_owned(),
            "b1".to_owned(),
            Some(20),
            Some(500),
            Some("boom".to_owned()),
            0,
        )
        .await
        .expect("ping records");
    let grouped = repository.latest_grouped().await.expect("latest groups");
    assert_eq!(grouped.len(), 2);
    assert_eq!(
        grouped
            .get(&("a".to_owned(), "a1".to_owned()))
            .expect("latest a1")
            .model_count,
        4
    );
    database.close().await.expect("database closes");
}

#[tokio::test]
async fn status_endpoint_is_authenticated_bounded_and_secret_free() {
    let root = tempfile::tempdir().expect("temp root");
    let database = Database::open(DatabaseConfig {
        path: root.path().join("usage.sqlite3").display().to_string(),
        ..DatabaseConfig::default()
    })
    .await
    .expect("database opens");
    MigrationRunner::new(&database)
        .run()
        .await
        .expect("migrations run");
    // Seed a ping carrying sentinel secrets; the endpoint must never echo it.
    PingRepository::new(&database)
        .record(
            "opencode-go".to_owned(),
            "sentinel-account".to_owned(),
            Some(84),
            Some(500),
            Some("SECRET_SENTINEL_sk-live-ping-body".to_owned()),
            0,
        )
        .await
        .expect("ping records");
    let process = ProcessRuntime::new(database.clone());
    let mut config = Config::default();
    config.server.api_key = Some("status-endpoint-key".to_owned());
    config.database.path = root.path().join("usage.sqlite3").display().to_string();
    let candidate =
        RuntimeGenerationFactory::prepare(&process, config.clone(), "status-test".to_owned(), 1)
            .await
            .expect("generation prepares");
    let manager = Arc::new(RuntimeManager::new(
        candidate.transfer().expect("generation transfers"),
    ));
    let app = build_router(AppState::from_runtime(config, database.clone(), manager));

    let unauthorized = app
        .clone()
        .oneshot(
            Request::builder()
                .uri("/api/status")
                .body(Body::empty())
                .expect("request builds"),
        )
        .await
        .expect("request completes");
    assert_eq!(unauthorized.status(), 401);

    let response = app
        .oneshot(
            Request::builder()
                .uri("/api/status")
                .header("authorization", "Bearer status-endpoint-key")
                .body(Body::empty())
                .expect("request builds"),
        )
        .await
        .expect("request completes");
    assert_eq!(response.status(), 200);
    let bytes = response
        .into_body()
        .collect()
        .await
        .expect("body collects")
        .to_bytes();
    assert!(bytes.len() < 1024 * 1024);
    let value: serde_json::Value = serde_json::from_slice(&bytes).expect("valid status JSON");
    assert_eq!(value["schema_version"], 1);
    assert!(value["observed_at"].is_string());
    assert!(value["proxy"]["status"].is_string());
    assert!(value["providers"].is_array());
    assert!(value["runtime"].is_object());
    let rendered = value.to_string();
    assert!(!rendered.contains("SECRET_SENTINEL"));
    assert!(!rendered.contains("api_key"));
    // Deterministic provider ordering.
    let ids: Vec<&str> = value["providers"]
        .as_array()
        .expect("providers array")
        .iter()
        .filter_map(|row| row["provider_id"].as_str())
        .collect();
    let mut sorted = ids.clone();
    sorted.sort_unstable();
    assert_eq!(ids, sorted);
    database.close().await.expect("database closes");
}

#[tokio::test]
async fn integration_profile_is_authenticated_deterministic_and_secret_free() {
    use eggpool::config::{ProviderConfig, ProviderStaticModelConfig};

    let root = tempfile::tempdir().expect("temp root");
    let database = Database::open(DatabaseConfig {
        path: root.path().join("usage.sqlite3").display().to_string(),
        ..DatabaseConfig::default()
    })
    .await
    .expect("database opens");
    MigrationRunner::new(&database)
        .run()
        .await
        .expect("migrations run");
    let process = ProcessRuntime::new(database.clone());
    let mut config = Config::default();
    config.server.api_key = Some("integration-endpoint-key".to_owned());
    config.server.host = "0.0.0.0".to_owned();
    config.integrations.advertise_base_url = Some("https://pool.example.internal/v1".to_owned());
    config.database.path = root.path().join("usage.sqlite3").display().to_string();
    // Two static models out of order; the served projection must sort them.
    config.providers.insert(
        "test-provider".to_owned(),
        ProviderConfig {
            id: "test-provider".to_owned(),
            base_url: "https://example.invalid/v1".to_owned(),
            protocols: vec!["openai".to_owned()],
            accounts: vec![eggpool::config::AccountConfig {
                name: "default".to_owned(),
                api_key: Some("provider-secret-sentinel".to_owned()),
                api_key_env: String::new(),
                enabled: true,
                ..Default::default()
            }],
            static_models: vec![
                ProviderStaticModelConfig {
                    id: "b-model".to_owned(),
                    display_name: Some("B".to_owned()),
                    max_context_tokens: Some(50_000),
                    ..Default::default()
                },
                ProviderStaticModelConfig {
                    id: "a-model".to_owned(),
                    display_name: Some("A".to_owned()),
                    max_context_tokens: Some(100_000),
                    ..Default::default()
                },
            ],
            ..Default::default()
        },
    );
    config.validate().expect("test config validates");
    let durable: Vec<eggpool::db::AccountConfig> = config
        .providers
        .iter()
        .flat_map(|(provider_id, provider)| {
            provider
                .accounts
                .iter()
                .map(|account| eggpool::db::AccountConfig {
                    name: account.name.clone(),
                    api_key_env: account.api_key_env.clone(),
                    enabled: account.enabled,
                    weight: account.weight,
                    provider_id: provider_id.clone(),
                })
        })
        .collect();
    AccountRepository::new(&database)
        .sync_from_config(durable)
        .await
        .expect("accounts sync");
    let candidate = RuntimeGenerationFactory::prepare(
        &process,
        config.clone(),
        "integration-test".to_owned(),
        1,
    )
    .await
    .expect("generation prepares");
    let manager = Arc::new(RuntimeManager::new(
        candidate.transfer().expect("generation transfers"),
    ));
    let app = build_router(AppState::from_runtime(config, database.clone(), manager));

    // Unauthenticated is rejected even though the payload is sanitized.
    let unauthorized = app
        .clone()
        .oneshot(
            Request::builder()
                .uri("/api/integrations/v1/profile")
                .body(Body::empty())
                .expect("request builds"),
        )
        .await
        .expect("request completes");
    assert_eq!(unauthorized.status(), 401);

    let fetch = |app: axum::Router| async move {
        let response = app
            .oneshot(
                Request::builder()
                    .uri("/api/integrations/v1/profile")
                    .header("authorization", "Bearer integration-endpoint-key")
                    .body(Body::empty())
                    .expect("request builds"),
            )
            .await
            .expect("request completes");
        assert_eq!(response.status(), 200);
        let etag = response
            .headers()
            .get("etag")
            .and_then(|v| v.to_str().ok())
            .unwrap_or_default()
            .to_owned();
        let cache = response
            .headers()
            .get("cache-control")
            .and_then(|v| v.to_str().ok())
            .unwrap_or_default()
            .to_owned();
        let content_type = response
            .headers()
            .get("content-type")
            .and_then(|v| v.to_str().ok())
            .unwrap_or_default()
            .to_owned();
        let bytes = response
            .into_body()
            .collect()
            .await
            .expect("body collects")
            .to_bytes();
        (bytes, etag, cache, content_type)
    };

    let (first_bytes, first_etag, cache, content_type) = fetch(app.clone()).await;
    assert!(content_type.contains("application/json"));
    assert!(cache.contains("private"));
    assert!(!first_etag.is_empty());
    assert!(first_bytes.len() < 1024 * 1024);
    let first: serde_json::Value = serde_json::from_slice(&first_bytes).expect("valid profile");
    assert_eq!(first["schema_version"], 1);
    assert_eq!(first["base_url"], "https://pool.example.internal/v1");
    assert!(first["revision"].as_str().is_some_and(|r| r.len() == 64));
    let ids: Vec<&str> = first["models"]
        .as_array()
        .expect("models array")
        .iter()
        .filter_map(|m| m["public_id"].as_str())
        .collect();
    assert_eq!(ids.len(), 2);
    let mut sorted = ids.clone();
    sorted.sort_unstable();
    assert_eq!(ids, sorted);

    // Second fetch is byte-identical (deterministic revision, no refresh).
    let (second_bytes, second_etag, _, _) = fetch(app.clone()).await;
    assert_eq!(first_bytes, second_bytes);
    assert_eq!(first_etag, second_etag);

    // Secret-free: no provider key, source metadata, paths, or prompts.
    let rendered = String::from_utf8_lossy(&first_bytes);
    assert!(!rendered.contains("provider-secret-sentinel"));
    assert!(!rendered.contains("integration-endpoint-key"));
    assert!(!rendered.contains("source_metadata"));
    assert!(!rendered.contains("usage.sqlite3"));

    // Conditional GET with the revision returns 304.
    let not_modified = app
        .clone()
        .oneshot(
            Request::builder()
                .uri("/api/integrations/v1/profile")
                .header("authorization", "Bearer integration-endpoint-key")
                .header("if-none-match", first_etag)
                .body(Body::empty())
                .expect("request builds"),
        )
        .await
        .expect("request completes");
    assert_eq!(not_modified.status(), 304);

    // Standard /v1/models keeps its OpenAI-compatible shape.
    let models = app
        .oneshot(
            Request::builder()
                .uri("/v1/models")
                .header("authorization", "Bearer integration-endpoint-key")
                .body(Body::empty())
                .expect("request builds"),
        )
        .await
        .expect("request completes");
    assert_eq!(models.status(), 200);
    let bytes = models
        .into_body()
        .collect()
        .await
        .expect("body collects")
        .to_bytes();
    let value: serde_json::Value = serde_json::from_slice(&bytes).expect("valid models");
    assert_eq!(value["object"], "list");
    assert!(value["data"].is_array());
    for entry in value["data"].as_array().expect("data array") {
        assert!(entry.get("id").and_then(|v| v.as_str()).is_some());
        assert_eq!(entry.get("object").and_then(|v| v.as_str()), Some("model"));
        assert!(entry.get("capabilities").is_none());
    }
    database.close().await.expect("database closes");
}

#[tokio::test]
async fn public_dashboard_default_renders_without_api_key_and_keeps_sensitive_routes_authenticated()
{
    async fn harness(public: bool) -> (axum::Router, tempfile::TempDir) {
        let root = tempfile::tempdir().expect("temp root");
        let database = Database::open(DatabaseConfig {
            path: root.path().join("usage.sqlite3").display().to_string(),
            ..DatabaseConfig::default()
        })
        .await
        .expect("database opens");
        MigrationRunner::new(&database)
            .run()
            .await
            .expect("migrations run");
        let process = ProcessRuntime::new(database.clone());
        let mut config = Config::default();
        assert!(
            Config::default().dashboard.public,
            "canonical default is a public read-only dashboard"
        );
        config.server.api_key = Some("dashboard-default-key".to_owned());
        config.dashboard.public = public;
        config.database.path = root.path().join("usage.sqlite3").display().to_string();
        let candidate = RuntimeGenerationFactory::prepare(
            &process,
            config.clone(),
            "dashboard-test".to_owned(),
            1,
        )
        .await
        .expect("generation prepares");
        let manager = Arc::new(RuntimeManager::new(
            candidate.transfer().expect("generation transfers"),
        ));
        let app = build_router(AppState::from_runtime(config, database.clone(), manager));
        (app, root)
    }

    async fn get_status(app: &axum::Router, uri: &str) -> u16 {
        app.clone()
            .oneshot(
                Request::builder()
                    .uri(uri)
                    .body(Body::empty())
                    .expect("request builds"),
            )
            .await
            .expect("request completes")
            .status()
            .as_u16()
    }

    let (public, _public_root) = harness(true).await;
    // A normal browser request renders the dashboard without an API key.
    let overview = public
        .clone()
        .oneshot(
            Request::builder()
                .uri("/")
                .body(Body::empty())
                .expect("request builds"),
        )
        .await
        .expect("request completes");
    assert_eq!(overview.status(), 200);
    assert!(
        overview
            .headers()
            .get("content-type")
            .and_then(|value| value.to_str().ok())
            .unwrap_or_default()
            .contains("text/html"),
        "default GET / renders HTML, not an API-key error"
    );
    assert_eq!(get_status(&public, "/api/stats/summary").await, 200);
    // Inference and sensitive control surfaces stay authenticated.
    assert_eq!(get_status(&public, "/v1/models").await, 401);
    assert_eq!(
        get_status(&public, "/api/integrations/v1/profile").await,
        401
    );
    assert_eq!(get_status(&public, "/api/stats/runtime").await, 401);
    assert_eq!(get_status(&public, "/api/stats/update").await, 401);
    assert_eq!(get_status(&public, "/api/status").await, 401);

    // `dashboard public --off` restores authentication on ordinary pages/data.
    let (private, _private_root) = harness(false).await;
    assert_eq!(get_status(&private, "/").await, 401);
    assert_eq!(get_status(&private, "/api/stats/summary").await, 401);
    assert_eq!(get_status(&private, "/v1/models").await, 401);
}

#[tokio::test]
async fn integration_profile_does_not_inherit_dashboard_public_exemption() {
    let root = tempfile::tempdir().expect("temp root");
    let database = Database::open(DatabaseConfig {
        path: root.path().join("usage.sqlite3").display().to_string(),
        ..DatabaseConfig::default()
    })
    .await
    .expect("database opens");
    MigrationRunner::new(&database)
        .run()
        .await
        .expect("migrations run");
    let process = ProcessRuntime::new(database.clone());
    let mut config = Config::default();
    config.server.api_key = Some("dashboard-public-key".to_owned());
    config.dashboard.public = true;
    config.integrations.advertise_base_url = Some("https://pool.example.internal/v1".to_owned());
    config.database.path = root.path().join("usage.sqlite3").display().to_string();
    let candidate =
        RuntimeGenerationFactory::prepare(&process, config.clone(), "public-test".to_owned(), 1)
            .await
            .expect("generation prepares");
    let manager = Arc::new(RuntimeManager::new(
        candidate.transfer().expect("generation transfers"),
    ));
    let app = build_router(AppState::from_runtime(config, database.clone(), manager));
    let unauthorized = app
        .oneshot(
            Request::builder()
                .uri("/api/integrations/v1/profile")
                .body(Body::empty())
                .expect("request builds"),
        )
        .await
        .expect("request completes");
    assert_eq!(unauthorized.status(), 401);
    database.close().await.expect("database closes");
}
