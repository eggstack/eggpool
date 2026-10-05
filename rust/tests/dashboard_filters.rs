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

/// A model whose per-status group rows straddle the projection's 100-row limit
/// used to be reported with only part of its traffic: the limit was applied to
/// `(entity, status)` groups instead of to whole entities.
#[tokio::test]
async fn observability_projection_never_reports_a_partially_counted_model() {
    let directory = tempfile::tempdir().expect("temporary dashboard database");
    let database = open_database(&directory, "truncation").await;
    // More `(model, status)` groups than the projection keeps. The split model
    // sorts last among the one-request groups, so a group-wise limit drops its
    // single `segmented` request while keeping its bulk `not_collected` group.
    let model_count = 150_i64;
    let bulk = 200_i64;
    let split_model = "model-zzz-split";
    database
        .call(move |connection| {
            for index in 0..model_count {
                let model = format!("model-{index:03}");
                connection.execute(
                    &format!("INSERT INTO models (model_id) VALUES ('{model}')"),
                    [],
                )?;
                connection.execute(
                    &format!(
                        "INSERT INTO requests (account_id, model_id, provider_id, upstream_protocol, status, segmentation_status, stable_prefix_estimated_tokens, volatile_estimated_tokens, started_at) \
                         VALUES (1, '{model}', 'provider-a', 'openai', 'completed', 'segmented', 10, 5, datetime('now','-1 minute'))"
                    ),
                    [],
                )?;
            }
            connection.execute(
                &format!("INSERT INTO models (model_id) VALUES ('{split_model}')"),
                [],
            )?;
            connection.execute(
                &format!(
                    "INSERT INTO requests (account_id, model_id, provider_id, upstream_protocol, status, segmentation_status, started_at) \
                     VALUES (1, '{split_model}', 'provider-a', 'openai', 'completed', 'segmented', datetime('now','-1 minute'))"
                ),
                [],
            )?;
            for _ in 0..bulk {
                connection.execute(
                    &format!(
                        "INSERT INTO requests (account_id, model_id, provider_id, upstream_protocol, status, segmentation_status, stable_prefix_estimated_tokens, volatile_estimated_tokens, started_at) \
                         VALUES (1, '{split_model}', 'provider-a', 'openai', 'completed', 'not_collected', 10, 5, datetime('now','-1 minute'))"
                    ),
                    [],
                )?;
            }
            Ok(())
        })
        .await
        .expect("fixture rows insert");

    let stats = DashboardRepository::new(&database)
        .observability_stats("24h")
        .await
        .expect("observability stats project");
    let per_model = &stats["canonical_request_segmentation"]["per_model_status"];
    let split = &per_model[split_model];
    assert_eq!(
        split["total_requests"].as_i64(),
        Some(bulk + 1),
        "a retained model is reported in full, not up to the group limit: {split}"
    );
    assert_eq!(split["not_collected"].as_i64(), Some(bulk));
    assert_eq!(split["segmented"].as_i64(), Some(1));
    assert_eq!(
        split["stable_prefix_estimated_tokens"].as_i64(),
        Some(10 * bulk)
    );

    // No retained entity may be a partial count: its statuses always sum back
    // to its total.
    for (model, entry) in per_model.as_object().expect("per_model_status object") {
        let statuses: i64 = [
            "segmented",
            "not_collected",
            "empty_request",
            "parse_failure",
        ]
        .iter()
        .map(|status| {
            entry
                .get(*status)
                .and_then(serde_json::Value::as_i64)
                .unwrap_or(0)
        })
        .sum();
        assert_eq!(
            entry["total_requests"].as_i64(),
            Some(statuses),
            "model {model} is reported partially"
        );
    }

    let per_provider = &stats["canonical_request_segmentation"]["per_provider_status"];
    let pair = &per_provider["provider-a->openai"];
    assert_eq!(pair["segmented"].as_i64(), Some(model_count + 1));
    assert_eq!(pair["not_collected"].as_i64(), Some(bulk));
    database.close().await.expect("database closes");
}
