//! The MCP gateway and hosted runner are withdrawn. These tests prove the
//! routes are not mounted and that an enabled execution switch is rejected
//! without echoing a backend URL.

use std::sync::Arc;

use axum::{body::Body, http::Request};
use eggpool::{
    Config,
    db::{Database, DatabaseConfig, MigrationRunner},
    runtime_lifecycle::{ProcessRuntime, RuntimeGenerationFactory, RuntimeManager},
    server::{AppState, build_router},
};
use tempfile::tempdir;
use tower::ServiceExt;

async fn router() -> (tempfile::TempDir, axum::Router) {
    let root = tempdir().expect("temporary root");
    let database = Database::open(DatabaseConfig {
        path: root
            .path()
            .join("eggpool.sqlite3")
            .to_string_lossy()
            .into_owned(),
        ..DatabaseConfig::default()
    })
    .await
    .expect("database opens");
    MigrationRunner::new(&database)
        .run()
        .await
        .expect("schema migrates");
    let process = ProcessRuntime::new(database.clone()).expect("process runtime");
    let mut config = Config::default();
    config.server.api_key = Some("withdrawn-route-key".to_owned());
    config.database.path = root.path().join("eggpool.sqlite3").display().to_string();
    let candidate =
        RuntimeGenerationFactory::prepare(&process, config.clone(), "withdrawn-routes".into(), 1)
            .await
            .expect("generation prepares");
    let manager = Arc::new(RuntimeManager::new(
        candidate.transfer().expect("generation transfers"),
    ));
    let app = build_router(AppState::from_runtime(config, database, manager));
    (root, app)
}

async fn status_of(app: axum::Router, method: &str, path: &str) -> u16 {
    let request = Request::builder()
        .method(method)
        .uri(path)
        .header("authorization", "Bearer withdrawn-route-key")
        .body(Body::empty())
        .expect("request builds");
    app.oneshot(request)
        .await
        .expect("response")
        .status()
        .as_u16()
}

#[tokio::test]
async fn withdrawn_tool_routes_are_not_mounted() {
    let (_root, app) = router().await;
    assert_eq!(
        status_of(app.clone(), "POST", "/api/tools/v1/mcp").await,
        404
    );
    assert_eq!(status_of(app, "POST", "/api/agent/v1/run").await, 404);
}

#[test]
fn enabled_tool_switch_names_the_key_and_hides_the_url() {
    let secret = "http://user:super-secret-token@10.1.2.3:9/mcp";
    let mut config = Config::default();
    config.tools.base_url = secret.to_owned();
    config.tools.enabled = true;
    let message = config.validate().expect_err("enabled").to_string();
    assert!(message.contains("tools.enabled"));
    assert!(message.contains("tool execution belongs to the client"));
    assert!(!message.contains("super-secret-token"));
    assert!(!message.contains("10.1.2.3"));
}
