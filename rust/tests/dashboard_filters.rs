//! Dashboard filter contract: a submitted `(any …)` option means no filter.

use eggpool::db::{DashboardRepository, Database, DatabaseConfig, MigrationRunner};
use tempfile::TempDir;
use tokio_rusqlite::rusqlite::params;

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
            connection.execute(
                "INSERT INTO accounts (id, name, api_key_env) VALUES (2, 'account-b', 'KEY_B')",
                [],
            )?;
            connection.execute("INSERT INTO models (model_id) VALUES ('model-a')", [])?;
            for (request_id, account_id) in [(1, 1), (2, 2)] {
                connection.execute(
                    "INSERT INTO requests (id, account_id, model_id, status, started_at, completed_at) \
                     VALUES (?1, ?2, 'model-a', 'completed', datetime('now','-1 minute'), datetime('now','-1 minute'))",
                    params![request_id, account_id],
                )?;
            }
            Ok(())
        })
        .await
        .expect("fixture rows insert");
    database
}

#[tokio::test(flavor = "current_thread")]
async fn blank_timeseries_filters_mean_no_filter() {
    // The filter forms submit `(any account)` as an empty value, which
    // deserializes to `Some("")`. The SQL guards read
    // `?N IS NULL OR column = ?N`, so a blank value skipped the `IS NULL`
    // short-circuit and matched nothing: the page reported "no requests" for
    // a filter the operator never set.
    let directory = tempfile::tempdir().expect("fixture directory");
    let database = open_database(&directory, "blank").await;
    let repository = DashboardRepository::new(&database);

    let (rows, _) = repository
        .grouped_timeseries_json(
            "24h".into(),
            "hour".into(),
            "model".into(),
            Some(String::new()),
            Some(String::new()),
        )
        .await
        .expect("grouped series");
    assert!(!rows.is_empty(), "a blank account filter hid every series");

    let rows = repository
        .timeseries_json("24h".into(), "hour".into(), Some("  ".into()), None)
        .await
        .expect("plain series");
    assert!(
        !rows.is_empty(),
        "a whitespace account filter hid every point"
    );

    // A real account name still narrows the result.
    let (filtered, _) = repository
        .grouped_timeseries_json(
            "24h".into(),
            "hour".into(),
            "account".into(),
            Some("account-a".into()),
            None,
        )
        .await
        .expect("grouped series");
    assert_eq!(filtered.len(), 1, "account filter must scope the series");
    let account = filtered[0]
        .get("account_name")
        .and_then(serde_json::Value::as_str)
        .unwrap_or_default();
    assert_eq!(account, "account-a");

    database.close().await.expect("database closes");
}
