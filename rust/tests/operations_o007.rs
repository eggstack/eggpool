//! O007 operator services, command coverage, and bounded metrics flush.

use std::{fs, path::Path};

use clap::Parser;
use eggpool::{
    Cli, Config,
    db::{Database, DatabaseConfig, MigrationRunner},
    operations::{
        metrics::{MetricsWriteCoalescer, UsageMetricEvent},
        operator,
    },
};
use tempfile::TempDir;

async fn database(path: &Path) -> Database {
    let database = Database::open(DatabaseConfig {
        path: path.display().to_string(),
        ..Default::default()
    })
    .await
    .expect("database opens");
    MigrationRunner::new(&database)
        .run()
        .await
        .expect("canonical schema");
    database
}

fn config(directory: &TempDir) -> (Config, std::path::PathBuf) {
    let mut config = Config::default();
    let config_path = directory.path().join("config.toml");
    config.database.path = directory.path().join("usage.sqlite3").display().to_string();
    fs::write(&config_path, toml::to_string(&config).expect("config TOML")).expect("config file");
    (config, config_path)
}

#[tokio::test]
async fn model_info_stats_cost_and_explain_services_are_real_and_safe() {
    let directory = tempfile::tempdir().expect("temp root");
    let (config, _) = config(&directory);
    let database = database(&directory.path().join("usage.sqlite3")).await;
    database
        .with_transaction(|connection| {
            connection.execute(
                "INSERT INTO providers (provider_id,base_url,protocols) VALUES ('fixture','https://fixture.invalid','[\"openai\"]')",
                [],
            )?;
            connection.execute(
                "INSERT INTO accounts (name,api_key_env,enabled,weight,provider_id) VALUES ('fixture-account','FIXTURE_KEY',1,1.0,'fixture')",
                [],
            )?;
            connection.execute(
                "INSERT INTO models (model_id,protocol,provider_id) VALUES ('fixture-model','openai','fixture')",
                [],
            )?;
            connection.execute(
                "INSERT INTO model_price_snapshots (model_id,provider_id,input_price_per_1k,output_price_per_1k) VALUES ('fixture-model','fixture',1.0,2.0)",
                [],
            )?;
            connection.execute(
                "INSERT INTO requests (account_id,model_id,provider_id,status,input_tokens,output_tokens,cost_microdollars,exactness,protocol,upstream_protocol,started_at) VALUES (1,'fixture-model','fixture','completed',1000,1000,999999999,'estimated','openai','anthropic',datetime('now','-1 minute'))",
                [],
            )?;
            connection.execute(
                "INSERT INTO model_info_canonical (model_id,status,detail_json,provenance_json,conflicts_json) VALUES ('fixture-model','sparse_new','{}','{}','{}')",
                [],
            )?;
            connection.execute(
                "INSERT INTO model_info_aliases (model_id,provider_id,alias,source) VALUES ('fixture-model','fixture','fixture-alias','curated')",
                [],
            )?;
            Ok(())
        })
        .await
        .expect("seed operator rows");

    let aliases = operator::list_aliases(&database, "fixture-model", Some("curated"))
        .await
        .expect("aliases");
    assert_eq!(aliases.len(), 1);
    assert_eq!(
        operator::list_model_info(&database, Some("sparse_new"))
            .await
            .expect("model info")
            .len(),
        1
    );
    assert!(
        operator::show_model_info(&database, "FIXTURE-MODEL")
            .await
            .expect("show")
            .is_some()
    );

    let explain = operator::explain_dashboard(&database, "24h", "hour", "provider_model")
        .await
        .expect("dashboard explain");
    assert_eq!(explain.len(), 6);
    assert!(operator::validate_period("invalid").is_err());
    assert!(operator::validate_dashboard_options("24h", "week", "provider").is_err());

    let transcoding = operator::transcoding_stats(&database, "24h")
        .await
        .expect("transcoding stats");
    assert_eq!(transcoding["transcoded_count"], 1);

    let dry = operator::recompute_costs(&database, None, false)
        .await
        .expect("dry recompute");
    assert_eq!(dry.updated, 1);
    let still_original: i64 = database
        .call(|connection| {
            connection.query_row(
                "SELECT cost_microdollars FROM requests WHERE id=1",
                [],
                |row| row.get(0),
            )
        })
        .await
        .expect("original cost");
    assert_eq!(still_original, 999_999_999);

    let applied = operator::recompute_costs(&database, None, true)
        .await
        .expect("apply recompute");
    assert_eq!(applied.updated, 1);
    let repaired = operator::repair_costs(&database, None, None, None, true)
        .await
        .expect("repair");
    assert_eq!(repaired.skipped_provider_reported, 0);
    database.close().await.expect("close");
    let _ = config;
}

#[tokio::test]
async fn metrics_flush_coalesces_additively_and_rebuffers_on_capacity() {
    let directory = tempfile::tempdir().expect("temp root");
    let (mut config, _) = config(&directory);
    config.metrics.max_buffered_events = 2;
    let database = database(&directory.path().join("usage.sqlite3")).await;
    let coalescer = MetricsWriteCoalescer::new(&config.metrics, database.clone());
    assert!(
        UsageMetricEvent::now("fixture", "model", 7, "openai", "success")
            .bucket_start
            .contains(' ')
    );
    let mut first = UsageMetricEvent::now("fixture", "model", 7, "openai", "success");
    first.bucket_start = "2026-01-01T00:00:00Z".into();
    first.input_tokens = 3;
    assert!(coalescer.record_usage(first.clone()));
    assert!(coalescer.record_usage(first));
    assert_eq!(coalescer.snapshot().buffered_events, 2);
    assert_eq!(coalescer.flush().await.expect("flush"), 1);
    let row: (String, i64, i64) = database
        .call(|connection| {
            connection.query_row(
                "SELECT bucket_start,request_count,input_tokens FROM usage_rollups WHERE provider_id='fixture'",
                [],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
        })
        .await
        .expect("rollup row");
    assert_eq!(row, ("2026-01-01 00:00:00".into(), 2, 6));
    assert_eq!(coalescer.flush().await.expect("empty flush"), 0);
    database.close().await.expect("close");
}

#[test]
fn every_o007_parser_variant_has_a_real_operation_entrypoint() {
    for args in [
        ["eggpool", "accounts", "list"].as_slice(),
        ["eggpool", "accounts", "status"].as_slice(),
        ["eggpool", "accounts", "explain", "--model", "m"].as_slice(),
        ["eggpool", "models", "refresh"].as_slice(),
        ["eggpool", "modelinfo", "aliases", "m"].as_slice(),
        ["eggpool", "modelinfo", "list"].as_slice(),
        ["eggpool", "modelinfo", "refresh"].as_slice(),
        ["eggpool", "modelinfo", "repair"].as_slice(),
        ["eggpool", "modelinfo", "show", "m"].as_slice(),
        ["eggpool", "stats", "transcoding"].as_slice(),
        ["eggpool", "stats", "recompute-costs"].as_slice(),
        ["eggpool", "stats", "repair-costs"].as_slice(),
        ["eggpool", "stats", "explain-dashboard"].as_slice(),
    ] {
        Cli::try_parse_from(args).expect("O007 command parses");
    }
}

#[tokio::test]
async fn every_o007_dispatch_path_reaches_rust_behavior() {
    let directory = tempfile::tempdir().expect("temp root");
    let (config, config_path) = config(&directory);
    let commands: Vec<Vec<String>> = vec![
        vec!["accounts".into(), "list".into()],
        vec!["accounts".into(), "status".into()],
        vec![
            "accounts".into(),
            "explain".into(),
            "--model".into(),
            "m".into(),
        ],
        vec!["models".into(), "refresh".into()],
        vec!["modelinfo".into(), "aliases".into(), "m".into()],
        vec!["modelinfo".into(), "list".into()],
        vec!["modelinfo".into(), "refresh".into()],
        vec!["modelinfo".into(), "repair".into()],
        vec!["modelinfo".into(), "show".into(), "m".into()],
        vec!["stats".into(), "transcoding".into()],
        vec!["stats".into(), "recompute-costs".into()],
        vec!["stats".into(), "repair-costs".into()],
        vec!["stats".into(), "explain-dashboard".into()],
    ];
    for command in commands {
        let mut args = vec![
            "eggpool".to_owned(),
            "--config".to_owned(),
            config_path.display().to_string(),
        ];
        args.extend(command);
        let result = eggpool::run(args).await;
        if let Err(eggpool::AppError::Bootstrap(eggpool::BootstrapError::NotImplemented {
            command,
        })) = result
        {
            panic!("O007 dispatch fell through to NotImplemented: {command}");
        }
    }
    let _ = config;
}

type RollupRow = (
    String,
    i64,
    i64,
    i64,
    i64,
    i64,
    i64,
    Option<i64>,
    Option<i64>,
    i64,
    i64,
    i64,
);

#[allow(clippy::too_many_arguments)]
fn metric_event(
    bucket: &str,
    provider: &str,
    model: &str,
    status: &str,
    input_tokens: i64,
    output_tokens: i64,
    cost: i64,
    latency_ms: i64,
    first_byte_ms: Option<i64>,
) -> UsageMetricEvent {
    let mut event = UsageMetricEvent::now(provider, model, 7, "openai", status);
    event.bucket_start = bucket.into();
    event.input_tokens = input_tokens;
    event.output_tokens = output_tokens;
    event.cost_microdollars = cost;
    event.latency_ms = latency_ms;
    event.first_byte_ms = first_byte_ms;
    event.bytes_received = 11;
    event.bytes_emitted = 13;
    event
}

#[tokio::test]
async fn metrics_flush_writes_many_rows_additively_in_one_transaction() {
    let directory = tempfile::tempdir().expect("temp root");
    let (config, _) = config(&directory);
    let database = database(&directory.path().join("usage.sqlite3")).await;
    let coalescer = MetricsWriteCoalescer::new(&config.metrics, database.clone());
    assert!(coalescer.record_usage(metric_event(
        "2026-02-01T00:00:00Z",
        "fixture",
        "model-a",
        "success",
        10,
        20,
        100,
        10,
        Some(5)
    )));
    assert!(coalescer.record_usage(metric_event(
        "2026-02-01T00:00:00Z",
        "fixture",
        "model-a",
        "success",
        30,
        40,
        200,
        30,
        None
    )));
    assert!(coalescer.record_usage(metric_event(
        "2026-02-01T00:00:00Z",
        "fixture",
        "model-b",
        "error",
        1,
        2,
        7,
        50,
        Some(9)
    )));
    assert_eq!(coalescer.snapshot().buffered_rows, 2);
    assert_eq!(coalescer.flush().await.expect("flush"), 2);
    let snapshot = coalescer.snapshot();
    assert_eq!(snapshot.last_flush_rows, 2);
    assert_eq!(snapshot.total_flushed, 3);
    assert_eq!(snapshot.flush_failures, 0);

    let rows: Vec<RollupRow> = database
        .call(|connection| {
            let mut statement = connection.prepare(
                "SELECT model_id, request_count, error_count, input_tokens,\
                 output_tokens, cost_microdollars, latency_ms_sum,\
                 latency_ms_min, latency_ms_max, first_byte_ms_sum,\
                 first_byte_ms_count, bytes_received FROM usage_rollups ORDER BY model_id",
            )?;
            let rows = statement
                .query_map([], |row| {
                    Ok((
                        row.get(0)?,
                        row.get(1)?,
                        row.get(2)?,
                        row.get(3)?,
                        row.get(4)?,
                        row.get(5)?,
                        row.get(6)?,
                        row.get(7)?,
                        row.get(8)?,
                        row.get(9)?,
                        row.get(10)?,
                        row.get(11)?,
                    ))
                })?
                .collect::<Result<Vec<_>, _>>()?;
            Ok(rows)
        })
        .await
        .expect("rollup rows");
    assert_eq!(rows.len(), 2);
    let expected_a: RollupRow = (
        "model-a".to_owned(),
        2,
        0,
        40,
        60,
        300,
        40,
        Some(10),
        Some(30),
        5,
        1,
        22,
    );
    assert_eq!(rows[0], expected_a);
    let expected_b: RollupRow = (
        "model-b".to_owned(),
        1,
        1,
        1,
        2,
        7,
        50,
        Some(50),
        Some(50),
        9,
        1,
        11,
    );
    assert_eq!(rows[1], expected_b);
    database.close().await.expect("close");
}

#[tokio::test]
async fn metrics_failed_flush_rebuffers_exactly_and_keeps_concurrent_events() {
    let directory = tempfile::tempdir().expect("temp root");
    let (config, _) = config(&directory);
    let database = database(&directory.path().join("usage.sqlite3")).await;
    let coalescer = MetricsWriteCoalescer::new(&config.metrics, database.clone());
    assert!(coalescer.record_usage(metric_event(
        "2026-03-01T00:00:00Z",
        "fixture",
        "model-a",
        "success",
        5,
        5,
        5,
        5,
        None
    )));
    assert!(coalescer.record_usage(metric_event(
        "2026-03-01T00:00:00Z",
        "fixture",
        "model-a",
        "success",
        7,
        7,
        7,
        7,
        None
    )));
    assert!(coalescer.record_usage(metric_event(
        "2026-03-01T00:00:00Z",
        "fixture",
        "model-b",
        "success",
        1,
        1,
        1,
        1,
        None
    )));
    // A closed database fails the flush deterministically; the single owned
    // batch must come back without duplication or loss.
    database.close().await.expect("close");
    let failed = coalescer.flush().await.expect_err("flush fails");
    assert!(format!("{failed}").contains("database operation failed"));
    let snapshot = coalescer.snapshot();
    assert_eq!(snapshot.flush_failures, 1);
    assert_eq!(snapshot.total_flushed, 0);
    assert_eq!(snapshot.buffered_events, 3);
    assert_eq!(snapshot.buffered_rows, 2);
    // A retry against the same closed database is idempotent: still exactly
    // the same three events, never doubled.
    coalescer.flush().await.expect_err("retry fails");
    let snapshot = coalescer.snapshot();
    assert_eq!(snapshot.flush_failures, 2);
    assert_eq!(snapshot.buffered_events, 3);

    // An event recorded while a failing flush is in flight lands either in
    // the failed batch or in the live buffer; both paths must converge to
    // the exact total without loss or duplication.
    let concurrent = coalescer.clone();
    let flush = tokio::spawn(async move { concurrent.flush().await });
    assert!(coalescer.record_usage(metric_event(
        "2026-03-01T00:00:00Z",
        "fixture",
        "model-c",
        "success",
        2,
        2,
        2,
        2,
        None
    )));
    flush.await.expect("flush joins").expect_err("flush fails");
    let snapshot = coalescer.snapshot();
    assert_eq!(snapshot.buffered_events, 4);
    assert_eq!(snapshot.total_dropped, 0);
}

#[tokio::test]
async fn metrics_immediate_mode_flushes_through_the_same_boundary() {
    let directory = tempfile::tempdir().expect("temp root");
    let (mut config, _) = config(&directory);
    config.metrics.write_mode = "immediate".into();
    let database = database(&directory.path().join("usage.sqlite3")).await;
    let coalescer = MetricsWriteCoalescer::new(&config.metrics, database.clone());
    assert_eq!(coalescer.write_mode(), "immediate");
    // Immediate mode never buffers through the synchronous entry point.
    assert!(!coalescer.record_usage(metric_event(
        "2026-04-01T00:00:00Z",
        "fixture",
        "model-a",
        "success",
        4,
        4,
        4,
        4,
        None
    )));
    assert!(
        coalescer
            .record_usage_async(metric_event(
                "2026-04-01T00:00:00Z",
                "fixture",
                "model-a",
                "success",
                4,
                6,
                8,
                12,
                Some(3)
            ))
            .await
            .expect("immediate record")
    );
    let row: (i64, i64, i64) = database
        .call(|connection| {
            connection.query_row(
                "SELECT request_count, input_tokens, first_byte_ms_sum FROM usage_rollups WHERE provider_id='fixture'",
                [],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
        })
        .await
        .expect("rollup row");
    assert_eq!(row, (1, 4, 3));
    assert_eq!(coalescer.snapshot().total_flushed, 1);
    database.close().await.expect("close");
}
