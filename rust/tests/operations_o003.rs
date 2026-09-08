//! O003 lifecycle and runtime-status boundary coverage.

use std::{fs, sync::Arc};

use axum::{body::Body, http::Request};
use eggpool::{
    Config,
    db::{Database, DatabaseConfig, MigrationRunner},
    operations::{
        paths::RuntimePaths,
        process::{ProcessError, acquire_start_guard},
    },
    runtime_lifecycle::{ProcessRuntime, RuntimeGenerationFactory, RuntimeManager},
    server::{AppState, build_router},
};
use http_body_util::BodyExt;
use tower::ServiceExt;

fn paths(root: &std::path::Path) -> RuntimePaths {
    RuntimePaths {
        config_path: root.join("config.toml"),
        config_dir: root.join("config"),
        data_dir: root.join("data"),
        state_dir: root.join("state"),
        env_path: None,
        runtime_dir: root.join("runtime"),
        pid_file: root.join("state/eggpool.pid"),
        log_file: root.join("state/eggpool.log"),
        control_socket: root.join("runtime/eggpool.sock"),
    }
}

#[test]
fn watchdog_guard_is_create_new_and_recovers_dead_owner() {
    let root = tempfile::tempdir().expect("temp root");
    let runtime_paths = paths(root.path());
    fs::create_dir_all(&runtime_paths.state_dir).expect("state directory");
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;

        fs::set_permissions(&runtime_paths.state_dir, fs::Permissions::from_mode(0o700))
            .expect("private state directory");
    }
    let first = acquire_start_guard(&runtime_paths).expect("first guard");
    assert!(matches!(
        acquire_start_guard(&runtime_paths),
        Err(ProcessError::LockHeld)
    ));
    drop(first);
    let second = acquire_start_guard(&runtime_paths).expect("released guard");
    drop(second);
    fs::write(
        runtime_paths.state_dir.join("eggpool.ensure-running.lock"),
        b"99999999",
    )
    .expect("stale guard");
    let recovered = acquire_start_guard(&runtime_paths).expect("stale guard recovers");
    drop(recovered);
    assert!(
        !runtime_paths
            .state_dir
            .join("eggpool.ensure-running.lock")
            .exists()
    );
}

#[tokio::test]
async fn runtime_status_is_always_authenticated_and_bounded_projection() {
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
    config.server.api_key = Some("o003-status-key".to_owned());
    config.database.path = root.path().join("usage.sqlite3").display().to_string();
    let candidate =
        RuntimeGenerationFactory::prepare(&process, config.clone(), "o003-status".to_owned(), 1)
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
                .uri("/api/stats/runtime")
                .body(Body::empty())
                .expect("request builds"),
        )
        .await
        .expect("request completes");
    assert_eq!(unauthorized.status(), 401);

    let response = app
        .oneshot(
            Request::builder()
                .uri("/api/stats/runtime")
                .header("authorization", "Bearer o003-status-key")
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
    let value: serde_json::Value = serde_json::from_slice(&bytes).expect("valid status JSON");
    assert_eq!(value["server"]["configured_server_threads"], 1);
    assert!(value["runtime_manager"].is_null());
    assert_eq!(value["db"]["is_memory_db"], false);
    database.close().await.expect("database closes");
}
