//! Personal tokens no longer receive Eggsearch discovery or invocation
//! scopes. Historical rows are not rewritten.

use clap::Parser;
use eggpool::{
    Cli,
    db::{Database, DatabaseConfig, MigrationRunner},
    identity,
};
use tempfile::tempdir;

async fn database() -> (tempfile::TempDir, Database) {
    let directory = tempdir().expect("temporary grant directory");
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
    identity::mark_fresh_uninitialized(&database)
        .await
        .expect("fresh identity");
    (directory, database)
}

#[tokio::test]
async fn issued_token_omits_tool_execution_scopes() {
    let (_directory, database) = database().await;
    identity::bootstrap_admin(&database, "admin", "tool grant replacement password")
        .await
        .expect("administrator");
    let issued = identity::issue_api_token(&database, "admin", "coding client")
        .await
        .expect("token");
    let scopes: String = database
        .call(|connection| {
            connection.query_row(
                "SELECT scopes FROM user_credentials WHERE label = 'coding client'",
                [],
                |row| row.get(0),
            )
        })
        .await
        .expect("scopes");
    assert_eq!(scopes, "inference,discovery,integrations");
    assert!(!scopes.contains("tools:discover"));
    assert!(!scopes.contains("tools:invoke"));
    assert!(issued.token.starts_with("epk1_"));
    database.close().await.expect("database closes");
}

#[test]
fn withdrawn_tool_commands_are_absent_and_audit_export_parses() {
    assert!(Cli::try_parse_from(["eggpool", "tools", "doctor"]).is_err());
    assert!(Cli::try_parse_from(["eggpool", "users", "tool-policy", "show", "admin"]).is_err());
    assert!(Cli::try_parse_from(["eggpool", "audit", "export", "--limit", "10"]).is_ok());
    assert!(Cli::try_parse_from(["eggpool", "check-config", "--local"]).is_ok());
    assert!(
        Cli::try_parse_from(["eggpool", "users", "token", "--tool-discovery", "admin"]).is_err()
    );
}
