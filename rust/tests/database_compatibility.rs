use std::{
    fs,
    path::PathBuf,
    process::Command,
    thread,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use eggpool::db::{
    AccountConfig, AccountRepository, Database, DatabaseConfig, MigrationRunner, ModelRepository,
    RequestRepository,
};

fn temporary_database_path(label: &str) -> PathBuf {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("clock is after epoch")
        .as_nanos();
    std::env::temp_dir().join(format!(
        "eggpool-f004-{label}-{}-{nanos}.sqlite3",
        std::process::id()
    ))
}

async fn open_database(path: &std::path::Path) -> Database {
    Database::open(DatabaseConfig {
        path: path.to_string_lossy().into_owned(),
        ..DatabaseConfig::default()
    })
    .await
    .expect("database opens")
}

#[tokio::test(flavor = "current_thread")]
async fn fresh_database_migrates_idempotently_and_preserves_pragmas() {
    let path = temporary_database_path("fresh");
    let database = open_database(&path).await;
    let first = MigrationRunner::new(&database)
        .run()
        .await
        .expect("migrations apply");
    assert_eq!(first.applied_versions.len(), 54);
    assert_eq!(first.applied_this_run.len(), 54);
    database
        .quick_check()
        .await
        .expect("fresh database is valid");

    let pragmas = database
        .call(|connection| {
            Ok((
                connection.query_row("PRAGMA journal_mode", [], |row| row.get::<_, String>(0))?,
                connection.query_row("PRAGMA foreign_keys", [], |row| row.get::<_, i64>(0))?,
                connection.query_row("PRAGMA busy_timeout", [], |row| row.get::<_, i64>(0))?,
                connection.query_row("PRAGMA synchronous", [], |row| row.get::<_, i64>(0))?,
            ))
        })
        .await
        .expect("pragmas readable");
    assert_eq!(pragmas.0.to_ascii_lowercase(), "wal");
    assert_eq!(pragmas.1, 1);
    assert_eq!(pragmas.2, 5_000);
    assert_eq!(pragmas.3, 1);

    let second = MigrationRunner::new(&database)
        .run()
        .await
        .expect("startup is idempotent");
    assert!(second.applied_this_run.is_empty());
    database.close().await.expect("database closes");
    fs::remove_file(&path).expect("temporary database removed");
}

#[tokio::test(flavor = "current_thread")]
async fn python_historical_fixture_upgrades_and_repositories_round_trip() {
    let path = temporary_database_path("historical");
    let database = open_database(&path).await;
    database
        .call(|connection| {
            connection.execute_batch(include_str!(
                "../../tests/fixtures/schema/pre_phase17_v11.sql"
            ))
        })
        .await
        .expect("Python historical fixture loads");

    let state = MigrationRunner::new(&database)
        .run()
        .await
        .expect("current migrations apply");
    assert_eq!(state.applied_this_run.first(), Some(&12));
    assert_eq!(state.applied_this_run.last(), Some(&54));

    let account = AccountRepository::new(&database)
        .get_by_name("historical-account")
        .await
        .expect("account query succeeds")
        .expect("historical account remains visible");
    assert_eq!(account.provider_id, "opencode-go");
    assert!(account.enabled);

    let request = RequestRepository::new(&database)
        .get_by_id(1)
        .await
        .expect("request query succeeds")
        .expect("historical request remains visible");
    assert_eq!(request.status, "success");
    assert_eq!(request.input_tokens, 100);
    assert_eq!(request.output_tokens, 50);

    let ids = AccountRepository::new(&database)
        .sync_from_config(vec![AccountConfig::new("rust-account", "RUST_TEST_KEY")])
        .await
        .expect("Rust account write succeeds");
    assert_eq!(ids.len(), 1);
    let rust_request = RequestRepository::new(&database)
        .create_pending(
            ids[0].1,
            "historical-model".to_owned(),
            "openai".to_owned(),
            "opencode-go".to_owned(),
            "rust-request".to_owned(),
            false,
        )
        .await
        .expect("Rust request write succeeds");
    assert!(
        RequestRepository::new(&database)
            .complete(rust_request, "success".to_owned(), 3, 2, 7)
            .await
            .expect("Rust completion succeeds")
    );
    assert_eq!(
        RequestRepository::new(&database)
            .get_by_id(rust_request)
            .await
            .expect("readback succeeds")
            .unwrap()
            .status,
        "success"
    );

    database.close().await.expect("database closes");
    let python_readback = Command::new("python3")
        .args([
            "-c",
            "import sqlite3, sys; connection = sqlite3.connect(sys.argv[1]); print(connection.execute(\"SELECT status, input_tokens, output_tokens FROM requests WHERE id = ?\", (int(sys.argv[2]),)).fetchone())",
        ])
        .arg(&path)
        .arg(rust_request.to_string())
        .output()
        .expect("Python is available for the compatibility readback");
    assert!(python_readback.status.success());
    assert_eq!(
        String::from_utf8_lossy(&python_readback.stdout).trim(),
        "('success', 3, 2)"
    );
    fs::remove_file(&path).expect("temporary database removed");
}

#[tokio::test(flavor = "current_thread")]
async fn routing_domain_schema54_seed_opens_and_preserves_owned_state() {
    let path = temporary_database_path("routing-domain-seed");
    let database = open_database(&path).await;
    MigrationRunner::new(&database)
        .run()
        .await
        .expect("schema 54 migrations apply");
    database
        .call(|connection| {
            connection.execute_batch(include_str!(
                "../../migration-rs/fixtures/routing-domain/schema54-routing-domain-seed.sql"
            ))
        })
        .await
        .expect("D001 schema-54 seed applies");

    let counts = database
        .call(|connection| {
            Ok((
                connection.query_row("SELECT COUNT(*) FROM accounts", [], |row| {
                    row.get::<_, i64>(0)
                })?,
                connection.query_row(
                    "SELECT COUNT(*) FROM provider_model_metadata",
                    [],
                    |row| row.get::<_, i64>(0),
                )?,
                connection.query_row("SELECT COUNT(*) FROM catalog_refresh_state", [], |row| {
                    row.get::<_, i64>(0)
                })?,
                connection.query_row("SELECT COUNT(*) FROM account_backoffs", [], |row| {
                    row.get::<_, i64>(0)
                })?,
                connection.query_row("SELECT COUNT(*) FROM model_quarantine", [], |row| {
                    row.get::<_, i64>(0)
                })?,
            ))
        })
        .await
        .expect("routing-domain rows are readable");
    assert_eq!(counts, (3, 3, 2, 2, 2));

    let account = AccountRepository::new(&database)
        .get_by_name("account-a")
        .await
        .expect("seed account query succeeds")
        .expect("seed account exists");
    assert_eq!(account.provider_id, "provider-a");
    assert_eq!(account.weight, 2.0);
    let model = ModelRepository::new(&database)
        .get("shared-model", "provider-a")
        .await
        .expect("seed model query succeeds")
        .expect("seed model exists");
    assert_eq!(model.protocol, "openai");

    database.close().await.expect("database closes");
    std::fs::remove_file(&path).expect("temporary database removed");
}

#[tokio::test(flavor = "current_thread")]
async fn failed_transaction_rolls_back_without_partial_rows() {
    let path = temporary_database_path("rollback");
    let database = open_database(&path).await;
    MigrationRunner::new(&database)
        .run()
        .await
        .expect("migrations apply");
    let error = database
        .with_transaction(|connection| {
            connection.execute(
                "INSERT INTO accounts (name, api_key_env) VALUES (?1, ?2)",
                ("transient", "TEST"),
            )?;
            Err::<(), _>(tokio_rusqlite::rusqlite::Error::InvalidQuery)
        })
        .await
        .expect_err("transaction body fails");
    assert!(matches!(error, eggpool::db::DatabaseError::Sqlite { .. }));
    let count = database
        .call(|connection| {
            connection.query_row(
                "SELECT COUNT(*) FROM accounts WHERE name = 'transient'",
                [],
                |row| row.get::<_, i64>(0),
            )
        })
        .await
        .expect("count succeeds");
    assert_eq!(count, 0);
    database.close().await.expect("database closes");
    fs::remove_file(&path).expect("temporary database removed");
}

#[tokio::test(flavor = "current_thread")]
async fn second_connection_reports_bounded_busy_contention() {
    let path = temporary_database_path("busy");
    let first = open_database(&path).await;
    MigrationRunner::new(&first)
        .run()
        .await
        .expect("migrations apply");
    let second = Database::open(DatabaseConfig {
        path: path.to_string_lossy().into_owned(),
        busy_timeout_ms: 20,
        ..DatabaseConfig::default()
    })
    .await
    .expect("second connection opens");

    let holder = first.clone();
    let task = tokio::spawn(async move {
        holder
            .with_transaction(|connection| {
                connection.execute(
                    "INSERT INTO accounts (name, api_key_env) VALUES (?1, ?2)",
                    ("busy-holder", "TEST"),
                )?;
                thread::sleep(Duration::from_millis(100));
                Ok(())
            })
            .await
    });
    tokio::time::sleep(Duration::from_millis(10)).await;
    let error = second
        .with_transaction(|connection| {
            connection.execute(
                "INSERT INTO accounts (name, api_key_env) VALUES (?1, ?2)",
                ("busy-contender", "TEST"),
            )?;
            Ok(())
        })
        .await
        .expect_err("locked writer is rejected after the configured bound");
    assert!(matches!(error, eggpool::db::DatabaseError::Busy { .. }));
    task.await.expect("holder joins").expect("holder commits");
    first.close().await.expect("first database closes");
    second.close().await.expect("second database closes");
    fs::remove_file(&path).expect("temporary database removed");
}

#[tokio::test(flavor = "current_thread")]
async fn current_database_can_be_opened_read_only_without_writes() {
    let path = temporary_database_path("readonly");
    let database = open_database(&path).await;
    MigrationRunner::new(&database)
        .run()
        .await
        .expect("migrations apply");
    database.close().await.expect("writable database closes");

    let read_only = Database::open(DatabaseConfig {
        path: path.to_string_lossy().into_owned(),
        read_only: true,
        ..DatabaseConfig::default()
    })
    .await
    .expect("current database opens read-only");
    MigrationRunner::new(&read_only)
        .run()
        .await
        .expect("read-only startup validates ledger");
    let error = read_only
        .with_transaction(|connection| {
            connection.execute(
                "INSERT INTO accounts (name, api_key_env) VALUES (?1, ?2)",
                ("forbidden", "TEST"),
            )?;
            Ok(())
        })
        .await
        .expect_err("read-only connection rejects writes");
    assert!(matches!(error, eggpool::db::DatabaseError::ReadOnly));
    read_only.close().await.expect("read-only database closes");
    fs::remove_file(&path).expect("temporary database removed");
}
