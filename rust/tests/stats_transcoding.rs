//! The CLI transcoding command and the HTTP transcoding endpoint must report
//! the same counts for the same data.
//!
//! The CLI derived the counts from the request protocols and the dashboard
//! repository derived them from the persisted `transcoded` column, so the two
//! surfaces reported different totals for the same request. Both now read the
//! single owner in `eggpool::db::repositories`.

use eggpool::{
    db::{DashboardRepository, Database, DatabaseConfig, MigrationRunner},
    operations::operator,
};
use tempfile::TempDir;

async fn open_database(directory: &TempDir, name: &str) -> Database {
    let path = directory.path().join(format!("{name}.sqlite3"));
    let database = Database::open(DatabaseConfig {
        path: path.display().to_string(),
        ..DatabaseConfig::default()
    })
    .await
    .expect("database opens");
    MigrationRunner::new(&database)
        .run()
        .await
        .expect("schema migrates");
    database
        .call(|connection| {
            connection.execute(
                "INSERT INTO accounts (id, name, api_key_env) VALUES (1, 'account-a', 'KEY_A')",
                [],
            )?;
            connection.execute("INSERT INTO models (model_id) VALUES ('model-a')", [])?;
            // A native request: the client protocol is the upstream protocol.
            connection.execute(
                "INSERT INTO requests (id, account_id, model_id, provider_id, status, protocol, upstream_protocol, started_at) \
                 VALUES (1, 1, 'model-a', 'provider-a', 'completed', 'openai', 'openai', datetime('now','-2 minutes'))",
                [],
            )?;
            // A transcoded request: the client protocol differs from the
            // upstream protocol it was finally sent with.
            connection.execute(
                "INSERT INTO requests (id, account_id, model_id, provider_id, status, protocol, upstream_protocol, started_at) \
                 VALUES (2, 1, 'model-a', 'provider-a', 'completed', 'openai', 'anthropic', datetime('now','-1 minute'))",
                [],
            )?;
            // A pending request has no upstream protocol yet, so it is neither
            // a native nor a transcoded observation.
            connection.execute(
                "INSERT INTO requests (id, account_id, model_id, provider_id, status, protocol, started_at) \
                 VALUES (3, 1, 'model-a', 'provider-a', 'pending', 'openai', datetime('now','-1 minute'))",
                [],
            )?;
            Ok(())
        })
        .await
        .expect("fixture rows insert");
    database
}

#[tokio::test(flavor = "current_thread")]
async fn cli_and_http_report_identical_transcoding_counts() {
    let directory = tempfile::tempdir().expect("fixture directory");
    let database = open_database(&directory, "transcoding").await;

    let cli = operator::transcoding_stats(&database, "24h")
        .await
        .expect("cli transcoding stats");
    let http = DashboardRepository::new(&database)
        .transcoding_summary("24h")
        .await
        .expect("http transcoding summary");
    let published = DashboardRepository::new(&database)
        .observability_stats("24h")
        .await
        .expect("observability stats project");

    assert_eq!(
        cli, http,
        "the CLI and the HTTP endpoint report different transcoding counts"
    );
    assert_eq!(
        cli, published["transcoding"],
        "the CLI and the published dashboard projection report different counts"
    );
    assert_eq!(
        cli["total"],
        serde_json::json!(2),
        "pending rows are not observations"
    );
    assert_eq!(cli["native_count"], serde_json::json!(1));
    assert_eq!(cli["transcoded_count"], serde_json::json!(1));

    database.close().await.expect("database closes");
}

#[tokio::test(flavor = "current_thread")]
async fn the_per_direction_breakdown_reports_the_transcoded_direction() {
    let directory = tempfile::tempdir().expect("fixture directory");
    let database = open_database(&directory, "directions").await;

    let cli = operator::transcoding_stats(&database, "24h")
        .await
        .expect("cli transcoding stats");
    let published = DashboardRepository::new(&database)
        .observability_stats("24h")
        .await
        .expect("observability stats project");

    // The HTTP projection used to report a hardcoded empty breakdown.
    let per_direction = published["transcoding"]["per_direction"]
        .as_object()
        .expect("per-direction object");
    assert!(
        !per_direction.is_empty(),
        "the per-direction breakdown is empty for a transcoded request"
    );
    assert_eq!(
        per_direction.get("openai→anthropic"),
        Some(&serde_json::json!(1)),
        "unexpected per-direction breakdown: {per_direction:?}"
    );
    assert_eq!(
        cli["per_direction"], published["transcoding"]["per_direction"],
        "the CLI and the HTTP endpoint report different direction breakdowns"
    );

    database.close().await.expect("database closes");
}

/// The persisted `transcoded` column is a materialized copy of the protocol
/// comparison. The derivation reads the protocol columns, so a request whose
/// protocols differ is reported as transcoded even when the copy was never
/// written.
#[tokio::test(flavor = "current_thread")]
async fn the_transcoding_derivation_reads_the_request_protocols() {
    let directory = tempfile::tempdir().expect("fixture directory");
    let database = open_database(&directory, "protocols").await;
    database
        .call(|connection| {
            connection.execute(
                "INSERT INTO requests (id, account_id, model_id, provider_id, status, protocol, upstream_protocol, started_at) \
                 VALUES (4, 1, 'model-a', 'provider-a', 'completed', 'openai', 'anthropic', datetime('now','-1 minute'))",
                [],
            )?;
            Ok(())
        })
        .await
        .expect("fixture rows insert");

    let cli = operator::transcoding_stats(&database, "24h")
        .await
        .expect("cli transcoding stats");
    let published = DashboardRepository::new(&database)
        .observability_stats("24h")
        .await
        .expect("observability stats project");
    assert_eq!(cli["transcoded_count"], serde_json::json!(2));
    assert_eq!(cli["total"], serde_json::json!(3));
    assert_eq!(cli["native_count"], serde_json::json!(1));
    assert_eq!(
        published["transcoding"]["transcoded_count"],
        serde_json::json!(2),
        "the HTTP projection counts the persisted copy instead of the protocols"
    );

    database.close().await.expect("database closes");
}
