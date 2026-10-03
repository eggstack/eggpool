//! Dashboard TTFT percentile contract tests and opt-in file-backed qualification.

use std::time::Instant;

use eggpool::db::{Database, DatabaseConfig, MigrationRunner, UsageRollupRepository};
use tempfile::TempDir;
use tokio_rusqlite::rusqlite::params;

async fn open_database(directory: &TempDir, name: &str) -> (Database, std::path::PathBuf) {
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
                "INSERT INTO accounts (name, api_key_env) VALUES ('qualification', 'KEY')",
                [],
            )?;
            connection.execute(
                "INSERT INTO models (model_id) VALUES ('qualification-model')",
                [],
            )?;
            Ok(())
        })
        .await
        .expect("fixture identity rows insert");
    (database, path)
}

async fn insert_ttft_rows(database: &Database, values: &[(i64, i64, &str)]) {
    let values: Vec<_> = values
        .iter()
        .map(|(streamed, ttft, age)| (*streamed, *ttft, (*age).to_owned()))
        .collect();
    database
        .call(move |connection| {
            for (streamed, ttft, age) in values {
                connection.execute(
                    "INSERT INTO requests (account_id, model_id, status, streamed, first_byte_ms, started_at) VALUES (1, 'qualification-model', 'completed', ?1, ?2, datetime('now', ?3))",
                    params![streamed, (ttft >= 0).then_some(ttft), age],
                )?;
            }
            Ok(())
        })
        .await
        .expect("TTFT rows insert");
}

#[tokio::test(flavor = "current_thread")]
async fn dashboard_ttft_percentiles_preserve_population_and_rank_definitions() {
    let directory = tempfile::tempdir().expect("fixture directory");
    let (database, _path) = open_database(&directory, "semantic").await;
    let repository = UsageRollupRepository::new(&database);

    let empty = repository
        .dashboard_summary_basic("24h")
        .await
        .expect("empty summary");
    assert_eq!((empty.p50_ttft_ms, empty.p99_ttft_ms), (0.0, 0.0));

    insert_ttft_rows(&database, &[(1, 17, "-1 hour")]).await;
    let single = repository
        .dashboard_summary_basic("24h")
        .await
        .expect("single summary");
    assert_eq!((single.p50_ttft_ms, single.p99_ttft_ms), (17.0, 17.0));

    database
        .call(|connection| connection.execute("DELETE FROM requests", []).map(|_| ()))
        .await
        .expect("clear single row");
    insert_ttft_rows(
        &database,
        &[
            (1, 100, "-1 hour"),
            (1, 200, "-1 hour"),
            (1, 200, "-1 hour"),
            (1, 400, "-1 hour"),
            (1, 1000, "-1 hour"),
            (0, 9000, "-1 hour"),
            (1, -1, "-1 hour"),
            (1, 7000, "-25 hours"),
        ],
    )
    .await;
    let odd = repository
        .dashboard_summary_basic("24h")
        .await
        .expect("odd summary");
    assert_eq!((odd.p50_ttft_ms, odd.p99_ttft_ms), (200.0, 1000.0));

    database
        .call(|connection| connection.execute("DELETE FROM requests", []).map(|_| ()))
        .await
        .expect("clear odd rows");
    insert_ttft_rows(
        &database,
        &[
            (1, 1, "-1 hour"),
            (1, 3, "-1 hour"),
            (1, 7, "-1 hour"),
            (1, 9, "-1 hour"),
        ],
    )
    .await;
    let even = repository
        .dashboard_summary_basic("24h")
        .await
        .expect("even summary");
    assert_eq!((even.p50_ttft_ms, even.p99_ttft_ms), (5.0, 9.0));

    database.close().await.expect("database closes");
}

#[tokio::test(flavor = "current_thread")]
#[ignore = "manual file-backed 10k/100k query-plan and gate-contention qualification"]
async fn qualify_dashboard_ttft_query_plans_and_serialized_write_delay() {
    for (name, row_count) in [("10k", 10_000_i64), ("100k", 100_000_i64)] {
        let directory = tempfile::tempdir().expect("fixture directory");
        let (database, path) = open_database(&directory, name).await;
        database
            .call(move |connection| {
                let transaction = connection.transaction()?;
                {
                    let mut insert = transaction.prepare_cached(
                        "INSERT INTO requests (account_id, model_id, status, streamed, first_byte_ms, started_at) VALUES (1, 'qualification-model', 'completed', ?1, ?2, datetime('now', ?3))",
                    )?;
                    for index in 0..row_count {
                        let streamed = i64::from(index % 5 != 0);
                        let has_ttft = streamed == 1 && index % 7 != 0;
                        let ttft = has_ttft.then_some((index * 7919) % 5000);
                        let age = if index % 10 == 0 {
                            "-45 days"
                        } else {
                            "-12 hours"
                        };
                        insert.execute(params![streamed, ttft, age])?;
                    }
                }
                transaction.commit()
            })
            .await
            .expect("large history inserts");

        let (sqlite_version, index_list, count_plan, ordered_plan) = database
            .call(|connection| {
                let version = connection.query_row(
                    "SELECT sqlite_version()",
                    [],
                    |row| row.get::<_, String>(0),
                )?;
                let indexes = connection
                    .prepare("SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='requests' AND name LIKE '%ttft%'")?
                    .query_map([], |row| Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?)))?
                    .collect::<Result<Vec<_>, _>>()?;
                let explain_count = || -> Result<Vec<String>, tokio_rusqlite::rusqlite::Error> {
                    connection
                        .prepare("EXPLAIN QUERY PLAN SELECT COUNT(*) FROM requests WHERE streamed = 1 AND first_byte_ms IS NOT NULL AND started_at >= CASE ?1 WHEN '1h' THEN datetime('now', '-1 hour') WHEN '7d' THEN datetime('now', '-7 days') WHEN '30d' THEN datetime('now', '-30 days') ELSE datetime('now', '-24 hours') END AND started_at < datetime('now')")?
                        .query_map(params!["24h"], |row| row.get(3))?
                        .collect()
                };
                let count = explain_count()?;
                let ordered = connection
                    .prepare("EXPLAIN QUERY PLAN SELECT first_byte_ms FROM requests WHERE streamed = 1 AND first_byte_ms IS NOT NULL AND started_at >= CASE ?1 WHEN '1h' THEN datetime('now', '-1 hour') WHEN '7d' THEN datetime('now', '-7 days') WHEN '30d' THEN datetime('now', '-30 days') ELSE datetime('now', '-24 hours') END AND started_at < datetime('now') ORDER BY first_byte_ms LIMIT 1 OFFSET ?2")?
                    .query_map(params!["24h", 300], |row| row.get(3))?
                    .collect::<Result<Vec<String>, _>>()?;
                Ok((version, indexes, count, ordered))
            })
            .await
            .expect("query plans inspect");
        let db_bytes = std::fs::metadata(&path).expect("database metadata").len();
        let repository = UsageRollupRepository::new(&database);
        let mut durations = Vec::new();
        for _ in 0..20 {
            let start = Instant::now();
            repository
                .dashboard_summary_basic("24h")
                .await
                .expect("summary query");
            durations.push(start.elapsed());
        }
        durations.sort_unstable();

        let mut writer_delays = Vec::new();
        for _ in 0..10 {
            let writer_db = database.clone();
            let summary = repository.dashboard_summary_basic("24h");
            let write = async move {
                let start = Instant::now();
                writer_db
                    .call(|connection| {
                        connection.execute(
                            "INSERT INTO requests (account_id, model_id, status, streamed, first_byte_ms) VALUES (1, 'qualification-model', 'completed', 0, NULL)",
                            [],
                        )?;
                        Ok(())
                    })
                    .await
                    .expect("serialized writer insert");
                start.elapsed()
            };
            let (_, delay) = tokio::join!(summary, write);
            writer_delays.push(delay);
        }
        writer_delays.sort_unstable();

        println!(
            "fixture={name} rows={row_count} sqlite={sqlite_version} db_bytes={db_bytes} indexes={index_list:?}"
        );
        println!("count_plan={count_plan:?}");
        println!("ordered_offset_plan={ordered_plan:?}");
        println!(
            "summary_ms p50={} p95={} max={}",
            durations[9].as_secs_f64() * 1000.0,
            durations[18].as_secs_f64() * 1000.0,
            durations[19].as_secs_f64() * 1000.0
        );
        println!(
            "concurrent_writer_ms p50={} max={}",
            writer_delays[5].as_secs_f64() * 1000.0,
            writer_delays[9].as_secs_f64() * 1000.0
        );
        database.close().await.expect("database closes");
    }
}
