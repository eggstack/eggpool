//! Content-free gateway audit export. Administration and logical inference
//! rows are capped, selector children stay out, and prompt text stored in
//! unrelated columns cannot appear.

use std::sync::Arc;

use axum::{body::Body, http::Request};
use eggpool::{
    Config,
    db::{Database, DatabaseConfig, MigrationRunner},
    identity,
    runtime_lifecycle::{ProcessRuntime, RuntimeGenerationFactory, RuntimeManager},
    server::{AppState, build_router},
    web_session,
};
use http_body_util::BodyExt;
use tempfile::tempdir;
use tower::ServiceExt;

async fn database() -> (tempfile::TempDir, Database) {
    let directory = tempdir().expect("temporary audit directory");
    let database = Database::open(DatabaseConfig {
        path: directory
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
    (directory, database)
}

fn assert_allowlisted(body: &str, sentinel: &str) {
    assert!(!body.contains(sentinel), "{body}");
    for line in body.lines().filter(|line| !line.is_empty()) {
        let value: serde_json::Value = serde_json::from_str(line).expect("json line");
        let object = value.as_object().expect("object");
        for key in object.keys() {
            assert!(
                identity::GATEWAY_AUDIT_KEYS.contains(&key.as_str()),
                "unexpected key {key}"
            );
        }
    }
}

#[tokio::test]
async fn export_is_capped_content_free_and_omits_selector_children() {
    let (_directory, database) = database().await;
    let sentinel = "PROMPT-SENTINEL do not export this body";
    database
        .call({
            let sentinel = sentinel.to_owned();
            move |connection| {
                connection.execute(
                    "INSERT INTO accounts (id, name, api_key_env, enabled, provider_id)\
                     VALUES (1, 'audit-account', 'UNUSED', 1, 'provider-a')",
                    [],
                )?;
                connection.execute(
                    "INSERT INTO models (model_id, protocol, provider_id, resolution_status)\
                     VALUES ('model-a', 'openai', 'provider-a', 'resolved')",
                    [],
                )?;
                connection.execute(
                    "INSERT INTO user_admin_audit(actor_id, action, target_kind, target_id)\
                     VALUES ('admin-a', 'issue_token', 'credential', 'credential-a')",
                    [],
                )?;
                for (proxy_id, kind, detail) in [
                    ("logical-a", "logical", sentinel.as_str()),
                    ("selector-a", "internal_selector", sentinel.as_str()),
                    ("logical-b", "logical", "budget_exceeded"),
                ] {
                    connection.execute(
                        "INSERT INTO requests\
                         (account_id, model_id, provider_id, protocol, proxy_request_id, user_id,\
                          credential_id, request_kind, status, input_tokens, output_tokens,\
                          upstream_latency_ms, error_class, error_detail, error_message)\
                         VALUES (1, 'model-a', 'provider-a', 'openai', ?1, 'user-a', 'cred-a', ?2,\
                                 'completed', 3, 4, 12.4, ?3, ?4, ?4)",
                        (proxy_id, kind, detail, sentinel.as_str()),
                    )?;
                }
                Ok(())
            }
        })
        .await
        .expect("fixture rows");

    let export = identity::gateway_audit_export(&database, 1)
        .await
        .expect("export");
    assert!(export.truncated);
    assert_eq!(export.limit, 1);
    assert_eq!(export.administration.len(), 1);
    assert_eq!(export.inference.len(), 1);
    assert_ne!(
        export.inference[0].proxy_request_id.as_deref(),
        Some("selector-a")
    );
    let body = export.to_json_lines();
    assert_allowlisted(&body, sentinel);
    assert!(body.contains("\"truncated\":true"));
    assert!(identity::normalize_audit_limit(0).is_err());
    assert!(identity::normalize_audit_limit(2001).is_err());
    database.close().await.expect("database closes");
}

async fn app(database: Database, config: Config) -> axum::Router {
    let process = ProcessRuntime::new(database.clone()).expect("process runtime");
    let candidate =
        RuntimeGenerationFactory::prepare(&process, config.clone(), "audit-export".into(), 1)
            .await
            .expect("generation prepares");
    let manager = Arc::new(RuntimeManager::new(
        candidate.transfer().expect("generation transfers"),
    ));
    build_router(AppState::from_runtime(config, database, manager))
}

async fn body_of(app: axum::Router, request: Request<Body>) -> (u16, String) {
    let response = app.oneshot(request).await.expect("response");
    let status = response.status().as_u16();
    let bytes = response
        .into_body()
        .collect()
        .await
        .expect("body")
        .to_bytes();
    (status, String::from_utf8_lossy(&bytes).into_owned())
}

#[tokio::test]
async fn administrator_session_can_export_and_other_principals_cannot() {
    let (_directory, database) = database().await;
    identity::mark_fresh_uninitialized(&database)
        .await
        .expect("fresh identity");
    let (admin, admin_token) =
        identity::bootstrap_admin(&database, "admin", "audit export admin password")
            .await
            .expect("administrator");
    identity::enable_managed(&database, &admin.id)
        .await
        .expect("managed access");
    identity::create_member(
        &database,
        &admin.id,
        "member",
        "audit export member password",
    )
    .await
    .expect("member");
    let member_token = identity::issue_api_token(&database, "member", "member client")
        .await
        .expect("member token");
    let admin_session = web_session::issue(&database, &admin.id)
        .await
        .expect("admin session");
    let member = identity::get_user_by_name(&database, "member")
        .await
        .expect("member lookup")
        .expect("member exists");
    let member_session = web_session::issue(&database, &member.id)
        .await
        .expect("member session");
    let mut config = Config::default();
    config.server.api_key = Some("audit-export-server-key".to_owned());
    config.security.allow_insecure_web_auth = true;
    let router = app(database, config).await;

    let (admin_status, admin_body) = body_of(
        router.clone(),
        Request::builder()
            .uri("/api/admin/audit/export?limit=5")
            .header(
                "cookie",
                format!("eggpool_session={}", admin_session.session_token),
            )
            .body(Body::empty())
            .expect("request"),
    )
    .await;
    assert_eq!(admin_status, 200, "{admin_body}");
    assert_allowlisted(&admin_body, "audit export admin password");
    assert!(admin_body.contains("\"kind\":\"export\""));
    assert!(admin_body.contains("issue_token"));

    let (member_status, _) = body_of(
        router.clone(),
        Request::builder()
            .uri("/api/admin/audit/export")
            .header(
                "cookie",
                format!("eggpool_session={}", member_session.session_token),
            )
            .body(Body::empty())
            .expect("request"),
    )
    .await;
    assert_eq!(member_status, 403);

    for token in [admin_token.token.as_str(), member_token.token.as_str()] {
        let (status, _) = body_of(
            router.clone(),
            Request::builder()
                .uri("/api/admin/audit/export")
                .header("authorization", format!("Bearer {token}"))
                .body(Body::empty())
                .expect("request"),
        )
        .await;
        assert_ne!(status, 200);
        assert!(status == 401 || status == 403, "{status}");
    }
}
