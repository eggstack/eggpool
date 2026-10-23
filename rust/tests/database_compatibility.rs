use std::{
    collections::BTreeSet,
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
    assert_eq!(first.applied_versions.len(), 63);
    assert_eq!(first.applied_this_run.len(), 63);
    let default_member_profile: Option<String> = database
        .call(|connection| {
            connection.query_row(
                "SELECT default_member_profile_id FROM policy_state WHERE singleton = 1",
                [],
                |row| row.get(0),
            )
        })
        .await
        .expect("fresh member default is readable");
    assert_eq!(default_member_profile, None);
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
async fn maintenance_checkpoint_preserves_wal_normal_autocheckpoint_and_close() {
    let path = temporary_database_path("checkpoint-maintenance");
    let database = open_database(&path).await;
    MigrationRunner::new(&database)
        .run()
        .await
        .expect("migrations apply");
    let pragmas = database
        .call(|connection| {
            Ok((
                connection.query_row("PRAGMA journal_mode", [], |row| row.get::<_, String>(0))?,
                connection.query_row("PRAGMA synchronous", [], |row| row.get::<_, i64>(0))?,
                connection
                    .query_row("PRAGMA wal_autocheckpoint", [], |row| row.get::<_, i64>(0))?,
            ))
        })
        .await
        .expect("pragmas readable");
    assert_eq!(pragmas.0.to_ascii_lowercase(), "wal");
    assert_eq!(pragmas.1, 1, "synchronous must remain NORMAL");
    assert_eq!(
        pragmas.2, 1000,
        "production automatic checkpoint safety ceiling must remain unchanged"
    );
    database
        .with_transaction(|connection| {
            connection.execute(
                "INSERT INTO providers (provider_id, base_url, protocols) VALUES ('fixture', 'https://fixture.invalid', '[\"openai\"]')",
                [],
            )?;
            Ok(())
        })
        .await
        .expect("write commits");
    database
        .checkpoint()
        .await
        .expect("compatibility checkpoint still runs on the existing worker");
    let destination = path.with_extension("backup.sqlite3");
    database
        .backup_to(destination.clone())
        .await
        .expect("backup works with recent checkpoint activity");
    assert!(destination.exists(), "backup snapshot exists");
    database
        .close()
        .await
        .expect("database closes after checkpoint");
    fs::remove_file(&path).expect("temporary database removed");
    fs::remove_file(&destination).expect("temporary backup removed");
}

#[tokio::test(flavor = "current_thread")]
async fn bundled_sqlite_supports_observational_noop_and_fixed_wal_reset_baseline() {
    let path = temporary_database_path("noop-baseline");
    let database = open_database(&path).await;
    let version = database
        .call(|connection| {
            connection.query_row("SELECT sqlite_version()", [], |row| row.get::<_, String>(0))
        })
        .await
        .expect("bundled SQLite version is readable");
    let version_parts = version
        .split('.')
        .map(|part| {
            part.parse::<u32>()
                .expect("numeric SQLite version component")
        })
        .collect::<Vec<_>>();
    assert!(
        version_parts.as_slice() >= &[3, 51, 3],
        "bundled SQLite {version} predates the NOOP/WAL-reset safety floor"
    );

    MigrationRunner::new(&database)
        .run()
        .await
        .expect("migrations apply");
    database
        .with_transaction(|connection| {
            connection.execute_batch(
                "CREATE TABLE noop_probe (value TEXT NOT NULL);\
                 WITH RECURSIVE values_to_insert(value) AS (\
                     VALUES(1) UNION ALL SELECT value + 1 FROM values_to_insert WHERE value < 32\
                 ) INSERT INTO noop_probe SELECT printf('%064d', value) FROM values_to_insert;",
            )
        })
        .await
        .expect("bounded WAL work commits below the automatic threshold");

    let noop = || async {
        database
            .call(|connection| {
                connection.query_row("PRAGMA wal_checkpoint(NOOP)", [], |row| {
                    Ok((
                        row.get::<_, i64>(0)?,
                        row.get::<_, i64>(1)?,
                        row.get::<_, i64>(2)?,
                    ))
                })
            })
            .await
            .expect("NOOP observation succeeds")
    };
    let first = noop().await;
    let second = noop().await;
    assert_eq!(first.0, 0, "NOOP reports no competing checkpoint");
    assert_eq!(
        second, first,
        "repeated NOOP does not advance checkpoint progress"
    );
    assert!(
        first.1 > first.2,
        "fixture leaves frames for explicit PASSIVE work"
    );

    let passive = database
        .call(|connection| {
            connection.query_row("PRAGMA wal_checkpoint(PASSIVE)", [], |row| {
                Ok((
                    row.get::<_, i64>(0)?,
                    row.get::<_, i64>(1)?,
                    row.get::<_, i64>(2)?,
                ))
            })
        })
        .await
        .expect("PASSIVE checkpoint succeeds");
    assert_eq!(passive.0, 0);
    assert!(passive.2 >= first.1, "PASSIVE advances/converges the WAL");
    database
        .backup_to(path.with_extension("noop-backup.sqlite3"))
        .await
        .expect("backup remains available after checkpoint work");
    database.close().await.expect("database closes");
    for candidate in [
        path.clone(),
        path.with_extension("noop-backup.sqlite3"),
        path.with_extension("sqlite3-wal"),
        path.with_extension("sqlite3-shm"),
    ] {
        let _ = fs::remove_file(candidate);
    }
}

#[tokio::test(flavor = "current_thread")]
async fn journal_size_limit_rejects_values_outside_sqlite_integer_range() {
    let path = temporary_database_path("journal-limit-overflow");
    let error = Database::open(DatabaseConfig {
        path: path.to_string_lossy().into_owned(),
        journal_size_limit: Some(u64::MAX),
        ..DatabaseConfig::default()
    })
    .await
    .expect_err("SQLite signed integer boundary is checked");
    let sqlite_error = std::error::Error::source(&error)
        .expect("SQLite conversion error remains in the source chain");
    let conversion_error = std::error::Error::source(sqlite_error)
        .expect("the out-of-range integer conversion error remains typed");
    assert!(
        conversion_error
            .downcast_ref::<std::num::TryFromIntError>()
            .is_some(),
        "unexpected journal-size-limit error: {error:?}"
    );
    let _ = fs::remove_file(&path);
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
    assert_eq!(state.applied_this_run.last(), Some(&63));

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
                "../../tests/fixtures/database/schema54-routing-domain-seed.sql"
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

// ---------------------------------------------------------------------------
// M010: an independent byte-valid schema-54 image restores, migrates
// forward, and recovers identity/policy/budgets without fabricating
// history. The frozen image is `tests/fixtures/database/schema54-
// pre-upgrade.sql` (SHA-256 recorded in the M010 closure); tests never run
// migrations 1..54 themselves — only the production 55..63 upgrade path.
// ---------------------------------------------------------------------------

use eggpool::health::HealthManager;
use eggpool::{
    Config,
    accounts::{AccountRegistry, CredentialStore},
    catalog::{ModelCatalogCache, ModelInput, ProtocolResolutionStatus},
    coordinator::{
        CrashReconciler, DurableFinalizer, FinalizationData, FinalizationOutcome, PublicationInput,
        PublicationOutcome, PublicationService,
    },
    db::Account,
    identity::{
        AuthenticatedPrincipal, PrincipalKind, access_state, admin_reset_password, bootstrap_admin,
        create_member, issue_personal_api_token, revoke_user_api_tokens, verify_api_token,
        verify_password,
    },
    instance_budget::InstanceBudgetService,
    model_policy::ModelPolicyService,
    operations::backup::BackupService,
    quota::{AccountQuota, QuotaEstimator},
    routing::{EligibilityPolicy, RoutingRequestFacts, RoutingRouter},
    user_budget::{BudgetLimits, BudgetWindow, UserBudgetService},
};

async fn load_schema54_image(path: &std::path::Path) -> Database {
    let database = open_database(path).await;
    database
        .call(|connection| {
            connection.execute_batch(include_str!(
                "../../tests/fixtures/database/schema54-pre-upgrade.sql"
            ))
        })
        .await
        .expect("frozen schema-54 image loads");
    database
}

async fn table_count(database: &Database, table: &str) -> i64 {
    let query = format!("SELECT COUNT(*) FROM {table}");
    database
        .call(move |connection| connection.query_row(&query, [], |row| row.get::<_, i64>(0)))
        .await
        .expect("count reads")
}

async fn prove_schema54_source(database: &Database) {
    // Source identity: the ledger names exactly migrations 1..54, the
    // embedded checksums validate, and the image is internally consistent.
    let ledger: Vec<(i64, String)> = database
        .call(|connection| {
            connection
                .prepare("SELECT version, name FROM _migrations ORDER BY version")?
                .query_map([], |row| Ok((row.get(0)?, row.get(1)?)))?
                .collect::<Result<Vec<_>, _>>()
        })
        .await
        .expect("ledger reads");
    assert_eq!(ledger.len(), 54);
    assert_eq!(ledger.first().map(|row| row.0), Some(1));
    assert_eq!(ledger.last().map(|row| row.0), Some(54));
    assert!(
        ledger
            .iter()
            .all(|row| { row.1.starts_with(&format!("{:04}_", row.0)) && row.1.ends_with(".sql") }),
        "every ledger entry carries its historical migration filename"
    );
    MigrationRunner::validate_embedded_checksums().expect("canonical checksums validate");
    let integrity: String = database
        .call(|connection| connection.query_row("PRAGMA integrity_check", [], |row| row.get(0)))
        .await
        .expect("integrity_check runs");
    assert_eq!(integrity, "ok");
    // Audited legacy content: upstream accounts, request/token-usage
    // history, and quarantine state — and no post-54 user/policy tables.
    assert_eq!(table_count(database, "accounts").await, 3);
    assert_eq!(table_count(database, "requests").await, 2);
    assert_eq!(table_count(database, "reservations").await, 2);
    assert_eq!(table_count(database, "model_quarantine").await, 2);
    assert_eq!(table_count(database, "catalog_refresh_state").await, 2);
    let modern: Vec<String> = database
        .call(|connection| {
            connection
                .prepare(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN \
                     ('users', 'user_budget_rules', 'instance_budget_rules', 'policy_profiles')",
                )?
                .query_map([], |row| row.get(0))?
                .collect::<Result<Vec<_>, _>>()
        })
        .await
        .expect("modern tables absent");
    assert!(modern.is_empty(), "pre-user image has no identity tables");
}

#[tokio::test(flavor = "current_thread")]
async fn schema54_image_upgrades_with_legacy_state_preserved() {
    let path = temporary_database_path("schema54-upgrade");
    let database = load_schema54_image(&path).await;
    prove_schema54_source(&database).await;

    // The protected matching backup is captured before the upgrade: old
    // binary rollback uses this archive, never a down-migration.
    let backup_root = tempfile::tempdir().expect("backup root");
    let config_path = backup_root.path().join("config.toml");
    let mut config = Config::default();
    config.database.path = path.to_string_lossy().into_owned();
    fs::write(&config_path, toml::to_string(&config).expect("config TOML")).expect("config writes");
    let backup = BackupService::from_config(&config_path, &config)
        .with_output_dir(backup_root.path().join("backups"));
    let archive = backup
        .create(&database)
        .await
        .expect("pre-upgrade backup")
        .archive;
    BackupService::validate_archive(&archive).expect("pre-upgrade archive validates");

    // Forward-only production upgrade: exactly migrations 55..63.
    let state = MigrationRunner::new(&database)
        .run()
        .await
        .expect("production upgrade applies");
    assert_eq!(state.applied_this_run.first(), Some(&55));
    assert_eq!(state.applied_this_run.last(), Some(&63));
    assert_eq!(state.applied_this_run.len(), 9);
    database
        .quick_check()
        .await
        .expect("upgraded image is valid");

    // Legacy history survives byte-identical: upstream accounts, the
    // completed request with its token/cost usage, quarantine, catalog.
    let legacy = database
        .call(|connection| {
            Ok((
                connection.query_row(
                    "SELECT status, input_tokens, output_tokens, cost_microdollars \
                     FROM requests WHERE id = 1",
                    [],
                    |row| {
                        Ok((
                            row.get::<_, String>(0)?,
                            row.get::<_, i64>(1)?,
                            row.get::<_, i64>(2)?,
                            row.get::<_, i64>(3)?,
                        ))
                    },
                )?,
                connection.query_row(
                    "SELECT enabled FROM accounts WHERE name = 'account-a'",
                    [],
                    |row| row.get::<_, i64>(0),
                )?,
                connection.query_row("SELECT COUNT(*) FROM model_quarantine", [], |row| {
                    row.get::<_, i64>(0)
                })?,
                connection.query_row("SELECT COUNT(*) FROM catalog_refresh_state", [], |row| {
                    row.get::<_, i64>(0)
                })?,
            ))
        })
        .await
        .expect("legacy rows read");
    assert_eq!(legacy.0, ("success".to_owned(), 100, 50, 700));
    assert_eq!(legacy.1, 1);
    assert_eq!(legacy.2, 2);
    assert_eq!(legacy.3, 2);

    // No fabricated human identity: historical shared-key inference stays
    // unattributed under the legacy principal kind.
    let attribution: (Option<String>, String) = database
        .call(|connection| {
            connection.query_row(
                "SELECT user_id, principal_kind FROM requests WHERE id = 1",
                [],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
        })
        .await
        .expect("legacy attribution reads");
    assert_eq!(attribution, (None, "legacy_shared_key".to_owned()));
    // Legacy shared-key state is distinct from fresh init: the upgraded
    // image reports legacy_unmigrated with zero users.
    assert_eq!(
        access_state(&database).await.expect("state reads"),
        "legacy_unmigrated"
    );
    assert_eq!(table_count(&database, "users").await, 0);

    database.close().await.expect("database closes");
    fs::remove_file(&path).expect("temporary database removed");
}

#[tokio::test(flavor = "current_thread")]
async fn upgraded_schema54_database_recovers_identity_policy_and_budgets() {
    // The database lives in a private directory: restore hardens its
    // parent directory permissions, which a shared temp root must never
    // receive.
    let work_root = tempfile::tempdir().expect("work root");
    let path = work_root.path().join("schema54-recover.sqlite3");
    let database = load_schema54_image(&path).await;
    prove_schema54_source(&database).await;
    MigrationRunner::new(&database)
        .run()
        .await
        .expect("production upgrade applies");

    // Identity recovery: bootstrap, login, member, personal-token
    // issue/revoke, password reset — all against the upgraded schema.
    let (admin, admin_credential) = bootstrap_admin(&database, "legacy-admin", "correct-horse-47")
        .await
        .expect("admin bootstraps on upgraded schema");
    assert_eq!(
        access_state(&database).await.expect("state reads"),
        "admin_initialized"
    );
    assert!(
        verify_password(&database, "legacy-admin", "correct-horse-47")
            .await
            .expect("login verifies")
            .is_some(),
        "bootstrap password logs in"
    );
    assert!(
        verify_password(&database, "legacy-admin", "wrong-password")
            .await
            .expect("login rejects")
            .is_none(),
        "wrong password does not log in"
    );
    let member = create_member(&database, &admin.id, "legacy-member", "temporary-99")
        .await
        .expect("member is created");
    let personal = issue_personal_api_token(&database, &admin.id, &member.id, "laptop")
        .await
        .expect("personal token issues");
    assert!(
        verify_api_token(&database, &personal.token)
            .await
            .expect("token verifies")
    );
    let revoked = revoke_user_api_tokens(&database, &admin.id, &member.id)
        .await
        .expect("personal tokens revoke");
    assert_eq!(revoked, 1);
    assert!(
        !verify_api_token(&database, &personal.token)
            .await
            .expect("revoked token rejects")
    );
    admin_reset_password(&database, &admin.id, &member.id, "rotated-1000")
        .await
        .expect("admin resets member password");
    assert!(
        verify_password(&database, "legacy-member", "rotated-1000")
            .await
            .expect("rotated password logs in")
            .is_some()
    );
    assert_eq!(admin_credential.user_id, admin.id);

    // Profile editing: create, rule, assign, edit, enforce.
    let policies = ModelPolicyService::new(database.clone());
    policies
        .create_profile("legacy-profile", "Legacy Profile", "allow", &admin.id)
        .await
        .expect("profile is created");
    policies
        .set_profile_rule("legacy-profile", "shared-model", "allow", &admin.id)
        .await
        .expect("profile rule is set");
    policies
        .assign_profile(&member.id, "legacy-profile", &admin.id)
        .await
        .expect("profile is assigned");
    // create (v1) plus the rule write (v2): the edit must target v2.
    assert!(
        policies
            .update_profile("legacy-profile", "Legacy Profile v2", "deny", 2, &admin.id)
            .await
            .expect("profile is edited")
    );
    let profiles = policies.list_profiles().await.expect("profiles list");
    assert_eq!(profiles.len(), 1);
    assert_eq!(profiles[0].name, "Legacy Profile v2");

    // Budget rules land on the upgraded schema and enforce for real: an
    // estimated member request publishes and settles, and a global rule
    // intersects it.
    database
        .call(|connection| {
            connection.execute(
                "INSERT INTO accounts (id, name, api_key_env, enabled, provider_id)
                 VALUES (10, 'budget-account', 'UNUSED', 1, 'provider-a')",
                [],
            )?;
            connection.execute(
                "INSERT INTO models (model_id, protocol, provider_id, resolution_status)
                 VALUES ('model-a', 'openai', 'provider-a', 'resolved')",
                [],
            )?;
            Ok(())
        })
        .await
        .expect("budget fixture rows insert");
    let user_budgets = UserBudgetService::new(database.clone());
    user_budgets
        .set_rule(
            &admin.id,
            &member.id,
            None,
            BudgetWindow::Day,
            BudgetLimits {
                tokens: Some(1_000_000),
                ..Default::default()
            },
        )
        .await
        .expect("estimated user rule is set");
    let global_budgets = InstanceBudgetService::new(database.clone());
    global_budgets
        .set_rule(
            &admin.id,
            None,
            BudgetWindow::Day,
            BudgetLimits {
                requests: Some(100),
                ..Default::default()
            },
        )
        .await
        .expect("global rule is set");
    let mut provider_config = eggpool::config::ProviderConfig {
        id: "provider-a".into(),
        base_url: "https://provider.invalid/v1".into(),
        protocols: vec!["openai".into()],
        auth: eggpool::config::ProviderAuthConfig {
            mode: "none".into(),
            ..Default::default()
        },
        ..Default::default()
    };
    provider_config
        .accounts
        .push(eggpool::config::AccountConfig {
            name: "budget-account".into(),
            ..Default::default()
        });
    let mut config = Config::default();
    config
        .providers
        .insert("provider-a".into(), provider_config);
    config.validate().expect("fixture config validates");
    let registry = AccountRegistry::from_config(
        &config,
        &[Account {
            id: 10,
            name: "budget-account".into(),
            api_key_env: "UNUSED".into(),
            enabled: true,
            weight: 1.0,
            provider_id: "provider-a".into(),
        }],
        &CredentialStore::default(),
    )
    .expect("registry builds");
    let mut catalog = ModelCatalogCache::default();
    catalog.set_account_provider("budget-account", "provider-a");
    let mut model = ModelInput::new("model-a");
    model.protocol = Some("openai".into());
    model.protocol_source = Some("fixture".into());
    model.resolution_status = ProtocolResolutionStatus::Resolved;
    catalog
        .update_from_account("budget-account", "provider-a", &[model], true, true)
        .expect("catalog model");
    // The health gate treats an account with no manager as not healthy, so the
    // fixture registers every account the router can select.
    let health = HealthManager::new();
    for account in registry.all() {
        health.register_account(account.account_id, &account.account_name);
    }
    let router = RoutingRouter::new(
        registry,
        catalog,
        QuotaEstimator::new([AccountQuota::new("budget-account")]),
        Some(health),
        EligibilityPolicy::default(),
    );
    let mut facts = RoutingRequestFacts::new("model-a");
    facts.requested_protocol = Some("openai".into());
    facts.client_protocol = Some("openai".into());
    facts.projected_tokens = 42;
    let claim = router
        .select_and_claim(&facts, &BTreeSet::new())
        .await
        .expect("claim succeeds")
        .expect("fixture has a candidate");
    // Resolve the live principal epoch exactly as the request edge does:
    // the password rotation above invalidated older epochs.
    let member_epoch: i64 = database
        .call({
            let member_id = member.id.clone();
            move |connection| {
                connection.query_row(
                    "SELECT auth_epoch FROM users WHERE id = ?1",
                    [member_id],
                    |row| row.get(0),
                )
            }
        })
        .await
        .expect("member epoch reads");
    let attribution = AuthenticatedPrincipal {
        user_id: Some(member.id.clone()),
        credential_id: None,
        role: Some("member".into()),
        scopes: vec!["inference".into()],
        auth_epoch: Some(member_epoch),
        kind: PrincipalKind::AgentToken,
    }
    .request_attribution();
    let outcome = PublicationService::new(database.clone())
        .publish_attributed(
            claim,
            PublicationInput::new("legacy-upgrade-request", "openai", "openai", false, 1),
            Some(attribution),
        )
        .await
        .expect("estimated request publishes under migrated rules");
    let PublicationOutcome::Published(published) = outcome else {
        panic!("upgraded request should publish");
    };
    DurableFinalizer::new(database.clone())
        .finalize_request(
            &published.identity,
            FinalizationData {
                outcome: FinalizationOutcome::Completed,
                ..FinalizationData::default()
            },
            Some(published.claim),
        )
        .await
        .expect("upgraded request settles");
    let settled_user: i64 = database
        .call(|connection| {
            connection.query_row(
                "SELECT COUNT(*) FROM user_budget_reservations WHERE status = 'settled'",
                [],
                |row| row.get(0),
            )
        })
        .await
        .expect("user settlement reads");
    assert_eq!(settled_user, 1);

    // Forced restart over the upgraded image: the legacy pending request
    // interrupts with its legacy reservation, the settled budget request
    // is untouched, and startup reconverges.
    database.close().await.expect("generation closes");
    let restarted = open_database(&path).await;
    let rerun = MigrationRunner::new(&restarted)
        .run()
        .await
        .expect("migrations stay idempotent across restart");
    assert!(rerun.applied_this_run.is_empty());
    let report = CrashReconciler::new(restarted.clone())
        .reconcile_once()
        .await
        .expect("restart reconciliation succeeds");
    assert_eq!(report.requests_interrupted, 1);
    assert_eq!(report.reservations_released, 1);
    assert_eq!(report.user_budget_holds_released, 0);
    assert_eq!(report.instance_budget_holds_released, 0);
    let second = CrashReconciler::new(restarted.clone())
        .reconcile_once()
        .await
        .expect("second pass succeeds");
    assert!(second.converged);

    // Backup/restore of the current (post-upgrade) state preserves users,
    // rules, usage, and audit across the round trip.
    let backup_root = tempfile::tempdir().expect("backup root");
    let config_path = backup_root.path().join("config.toml");
    let mut backup_config = Config::default();
    backup_config.database.path = path.to_string_lossy().into_owned();
    fs::write(
        &config_path,
        toml::to_string(&backup_config).expect("config TOML"),
    )
    .expect("config writes");
    let backup = BackupService::from_config(&config_path, &backup_config)
        .with_output_dir(backup_root.path().join("backups"));
    let archive = backup
        .create(&restarted)
        .await
        .expect("current-state backup")
        .archive;
    BackupService::validate_archive(&archive).expect("current archive validates");
    restarted
        .call(|connection| {
            connection.query_row("PRAGMA wal_checkpoint(TRUNCATE)", [], |_| Ok(()))?;
            Ok(())
        })
        .await
        .expect("wal checkpoints");
    restarted.close().await.expect("database closes");
    backup.recover(&archive).expect("current-state restore");
    let restored = open_database(&path).await;
    assert_eq!(table_count(&restored, "users").await, 2);
    assert_eq!(table_count(&restored, "user_budget_rules").await, 1);
    assert_eq!(table_count(&restored, "instance_budget_rules").await, 1);
    assert_eq!(table_count(&restored, "user_budget_usage").await, 2);
    // Creating the first member moved the instance to managed, and the
    // round trip preserves that activation state.
    assert_eq!(
        access_state(&restored).await.expect("state reads"),
        "managed"
    );
    restored.close().await.expect("restored database closes");
    fs::remove_file(&path).expect("temporary database removed");
}
