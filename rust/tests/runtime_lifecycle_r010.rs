//! R010 authority, live body admission, and bounded diagnostics contracts.

use std::sync::Arc;

use axum::{body::Body, http::StatusCode};
use eggpool::{
    Config,
    db::{Database, DatabaseConfig, MigrationRunner},
    reload::ReloadService,
    runtime_lifecycle::{ProcessRuntime, RuntimeGenerationFactory, RuntimeManager},
    server::{AppState, build_router},
};
use tempfile::TempDir;
use tower::ServiceExt;

async fn fixture() -> (TempDir, Database, ProcessRuntime, RuntimeManager, Config) {
    let directory = tempfile::tempdir().expect("temporary directory");
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
    let process = ProcessRuntime::new(database.clone());
    let config = Config::default();
    let candidate = RuntimeGenerationFactory::prepare(
        &process,
        config.clone(),
        "r010-initial-secret-free-digest".to_owned(),
        1,
    )
    .await
    .expect("generation prepares");
    let manager = RuntimeManager::new(candidate.transfer().expect("generation transfers"));
    (directory, database, process, manager, config)
}

#[tokio::test]
async fn dynamic_body_admission_rejects_before_m7_for_the_active_generation() {
    let (_directory, database, process, manager, _config) = fixture().await;
    let mut config = Config::default();
    config.server.max_request_body_bytes = 32;
    let next = RuntimeGenerationFactory::prepare(
        &process,
        config.clone(),
        "r010-small-limit".to_owned(),
        2,
    )
    .await
    .expect("small-limit generation prepares");
    let candidate = next;
    let mut staged = manager.stage(1, &candidate).expect("generation stages");
    staged.commit_pointer().expect("pointer commits");
    staged.accept().expect("generation accepts");
    let app = build_router(AppState::from_runtime(
        config,
        database.clone(),
        Arc::new(manager),
    ));
    let body = vec![b'x'; 33];
    let request = http::Request::builder()
        .method("POST")
        .uri("/v1/chat/completions")
        .body(Body::from(body))
        .expect("request builds");
    let response = app.oneshot(request).await.expect("response");
    assert_eq!(response.status(), StatusCode::PAYLOAD_TOO_LARGE);
    database.close().await.expect("database closes");
}

#[tokio::test]
async fn diagnostics_are_bounded_coherent_and_secret_free() {
    let (_directory, database, process, manager, _config) = fixture().await;
    let secret = "r010-api-key-secret-sentinel";
    let snapshot = process.diagnostics(&manager);
    let json = serde_json::to_string(&snapshot).expect("diagnostic JSON");
    let debug = format!("{snapshot:?}");
    assert!(snapshot.active_generation.digest_prefix.len() <= 12);
    assert!(snapshot.retiring_generations.len() <= 4);
    assert!(snapshot.tasks.len() <= 6);
    assert!(!json.contains(secret));
    assert!(!debug.contains(secret));
    assert!(!json.contains("api_key"));
    assert!(!json.contains("proxy"));
    database.close().await.expect("database closes");
}

#[tokio::test]
async fn reload_diagnostics_keep_only_the_last_bounded_result() {
    let (_directory, database, process, manager, _config) = fixture().await;
    let reload: ReloadService = process.reload_service(manager.clone());
    let mut changed = Config::default();
    changed.server.max_request_body_bytes += 1;
    let result = reload
        .reload_bytes(
            "config.toml",
            toml::to_string(&changed)
                .expect("config serializes")
                .into_bytes(),
            None,
        )
        .await;
    assert_eq!(
        result.category,
        eggpool::reload::ReloadResultCategory::Applied
    );
    let snapshot = process.diagnostics(&manager);
    assert_eq!(snapshot.counters.reload_attempts, 1);
    assert_eq!(snapshot.counters.reload_accepted, 1);
    assert_eq!(
        snapshot
            .reload
            .last_result
            .as_ref()
            .map(|r| r.category.as_str()),
        Some("applied")
    );
    assert_eq!(snapshot.publication.publication_epoch, 1);
    database.close().await.expect("database closes");
}

#[test]
fn production_state_source_audit_has_no_direct_generation_config_field() {
    let source = std::fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/src/server.rs"))
        .expect("server source");
    assert!(!source.contains("pub config: Config"));
    assert!(source.contains("async fn admit_inference_body"));
    assert!(source.contains("Extension<Arc<GenerationLease>>"));
}
