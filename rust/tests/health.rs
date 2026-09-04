use std::{
    collections::BTreeMap,
    fs,
    path::PathBuf,
    process::Command,
    sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
    },
    time::{SystemTime, UNIX_EPOCH},
};

use eggpool::{
    db::{
        AccountBackoffRepository, Database, DatabaseConfig, MigrationRunner,
        ModelQuarantineRepository,
    },
    health::{
        BackoffReason, CircuitBreaker, CircuitState, EvidenceProvenance, HealthEffect,
        HealthEffectApplier, HealthManager, ModelQuarantine, QuarantineState,
        classify_failure_category, compute_backoff_seconds,
    },
};

#[test]
fn categories_and_backoff_match_the_frozen_python_boundary() {
    assert_eq!(
        classify_failure_category(None, Some(402)),
        BackoffReason::QuotaExhausted
    );
    assert_eq!(
        classify_failure_category(None, Some(408)),
        BackoffReason::ConnectTimeout
    );
    assert_eq!(
        classify_failure_category(Some("authentication_timeout"), None),
        BackoffReason::ConnectTimeout
    );
    assert_eq!(
        classify_failure_category(Some("auth"), None),
        BackoffReason::AuthenticationFailed
    );
    assert_eq!(
        classify_failure_category(None, Some(409)),
        BackoffReason::Unknown
    );
    assert_eq!(
        classify_failure_category(None, Some(500)),
        BackoffReason::UpstreamServerError
    );
    assert_eq!(
        compute_backoff_seconds(BackoffReason::QuotaExhausted, 1, None, false),
        Some(300.0)
    );
    assert_eq!(
        compute_backoff_seconds(BackoffReason::RateLimited, 6, None, false),
        Some(1_800.0)
    );
    assert_eq!(
        compute_backoff_seconds(BackoffReason::UpstreamServerError, 99, None, false),
        Some(1_800.0)
    );
    assert_eq!(
        compute_backoff_seconds(BackoffReason::RateLimited, 1, Some(2_000.0), false),
        Some(1_800.0)
    );
    assert_eq!(
        compute_backoff_seconds(BackoffReason::RateLimited, 1, Some(-1.0), false),
        Some(60.0)
    );
    assert_eq!(
        compute_backoff_seconds(BackoffReason::AuthenticationFailed, 1, None, false),
        None
    );
    assert_eq!(
        compute_backoff_seconds(BackoffReason::ContextLimitExceeded, 1, None, false),
        None
    );
}

#[test]
fn circuit_read_only_checks_do_not_consume_the_single_probe() {
    let now = Arc::new(AtomicU64::new(0));
    let clock = {
        let now = Arc::clone(&now);
        move || now.load(Ordering::Relaxed) as f64
    };
    let breaker = CircuitBreaker::with_clock(clock, 2, 300.0, 1);
    breaker.record_failure();
    breaker.record_failure();
    assert_eq!(breaker.state(), CircuitState::Open);
    now.store(300, Ordering::Relaxed);
    assert!(breaker.can_request());
    assert!(breaker.can_request());
    assert!(breaker.allow_request());
    assert!(!breaker.allow_request());
    breaker.release_probe();
    assert!(breaker.allow_request());
    breaker.record_success();
    assert_eq!(breaker.state(), CircuitState::Closed);
}

#[test]
fn health_separates_cooldowns_models_operator_disable_and_circuit() {
    let now = Arc::new(AtomicU64::new(100));
    let manager = {
        let now = Arc::clone(&now);
        HealthManager::with_clock(move || now.load(Ordering::Relaxed) as f64)
    };
    manager.register_account(1, "account-a");
    assert!(manager.is_model_healthy_read_only("account-a", "model-a"));
    manager.record_cooldown("account-a", BackoffReason::RateLimited, 60.0);
    assert!(!manager.is_account_healthy_read_only("account-a"));
    assert_eq!(
        manager
            .snapshot("account-a")
            .expect("health")
            .circuit
            .failure_count,
        0
    );
    now.store(160, Ordering::Relaxed);
    assert!(manager.is_account_healthy_read_only("account-a"));
    manager.disable_model("account-a", "model-a", Some(10.0), false);
    assert!(!manager.is_model_healthy_read_only("account-a", "model-a"));
    now.store(170, Ordering::Relaxed);
    assert!(manager.is_model_healthy_read_only("account-a", "model-a"));
    manager.disable_account("account-a", "operator", None);
    manager.record_success("account-a", None);
    assert!(!manager.is_account_healthy_read_only("account-a"));
    manager.enable_account("account-a");
    assert!(manager.is_account_healthy_read_only("account-a"));
}

#[test]
fn quarantine_is_exact_key_bounded_and_terminal_recovery_is_authoritative() {
    let quarantine = ModelQuarantine::default();
    let key = quarantine.key(
        "provider-a",
        "account-a",
        "model",
        Some("upstream"),
        "openai",
    );
    let sibling = quarantine.key(
        "provider-b",
        "account-a",
        "model",
        Some("upstream"),
        "openai",
    );
    let first = quarantine.record_observation(
        key.clone(),
        EvidenceProvenance::RuntimeHttp,
        "missing",
        Some(404),
        None,
        100.0,
    );
    assert_eq!(first.state, QuarantineState::Suspected);
    assert!(quarantine.is_model_quarantined(&key, 100.0));
    let second = quarantine.record_observation(
        key.clone(),
        EvidenceProvenance::RuntimeHttp,
        "missing",
        Some(404),
        None,
        101.0,
    );
    assert_eq!(second.state, QuarantineState::Quarantined);
    assert!(!quarantine.is_model_quarantined(&sibling, 101.0));
    assert!(quarantine.clear_exact_key(&key, "successful_request", 102.0));
    assert!(!quarantine.is_model_quarantined(&key, 102.0));
    let _ = quarantine.record_observation(
        key.clone(),
        EvidenceProvenance::RuntimeHttp,
        "missing",
        Some(404),
        None,
        103.0,
    );
    let terminal = quarantine
        .set_terminal_withdrawn(
            key.clone(),
            "catalog_absence",
            EvidenceProvenance::ProviderCatalog,
            104.0,
        )
        .expect("authoritative terminal state");
    assert_eq!(terminal.state, QuarantineState::TerminalWithdrawn);
    assert!(!quarantine.is_model_quarantined(&key, 104.0));
    assert!(quarantine.clear_authoritative_reappearance(&key, 105.0));
    assert_eq!(
        quarantine.get_entry(&key).expect("audit row").state,
        QuarantineState::Healthy
    );
}

#[tokio::test(flavor = "current_thread")]
async fn schema54_health_rows_round_trip_and_corrupt_state_fails_closed() {
    let database = Database::open(DatabaseConfig::default())
        .await
        .expect("database");
    MigrationRunner::new(&database)
        .run()
        .await
        .expect("migrations");
    database
        .call(|connection| {
            connection.execute_batch(include_str!(
                "../../migration-rs/fixtures/routing-domain/schema54-routing-domain-seed.sql"
            ))
        })
        .await
        .expect("seed");
    let backoffs = AccountBackoffRepository::new(&database)
        .list_all(10)
        .await
        .expect("backoffs");
    assert_eq!(backoffs.len(), 2);
    assert_eq!(backoffs[0].reason, BackoffReason::RateLimited);
    let quarantines = ModelQuarantineRepository::new(&database)
        .list_all(10)
        .await
        .expect("quarantine");
    assert_eq!(quarantines.len(), 2);
    assert!(
        quarantines
            .iter()
            .any(|row| row.upstream_model_id.is_none())
    );
    database
        .call(|connection| {
            connection.execute("UPDATE model_quarantine SET state='corrupt' WHERE id=1", [])
        })
        .await
        .expect("corrupt row");
    assert!(
        ModelQuarantineRepository::new(&database)
            .list_all(10)
            .await
            .is_err()
    );
    database.close().await.expect("close");
}

#[tokio::test(flavor = "current_thread")]
async fn effect_application_is_durable_without_retry_or_finalization_policy() {
    let database = Database::open(DatabaseConfig::default())
        .await
        .expect("database");
    MigrationRunner::new(&database)
        .run()
        .await
        .expect("migrations");
    database.call(|connection| {
        connection.execute("INSERT INTO accounts (id,name,api_key_env,enabled,weight,provider_id) VALUES (1,'account-a','KEY',1,1.0,'provider-a')", [])
    }).await.expect("account");
    let manager = HealthManager::default();
    manager.register_account(1, "account-a");
    let quarantine = ModelQuarantine::default();
    let backoffs = AccountBackoffRepository::new(&database);
    let quarantine_repository = ModelQuarantineRepository::new(&database);
    let applier = HealthEffectApplier {
        health: &manager,
        quarantine: &quarantine,
        account_backoffs: Some(&backoffs),
        quarantine_repository: Some(&quarantine_repository),
    };
    let mut rate = HealthEffect::account(
        1,
        "account-a",
        "provider-a",
        BackoffReason::RateLimited,
        1_000.0,
    );
    rate.retry_after_seconds = Some(500.0);
    let rate_result = applier.apply(&rate).await.expect("rate effect");
    assert_eq!(rate_result.backoff_seconds, Some(500.0));
    assert_eq!(
        manager
            .snapshot("account-a")
            .expect("health")
            .circuit
            .failure_count,
        0
    );
    let model_effect = HealthEffect::account(
        1,
        "account-a",
        "provider-a",
        BackoffReason::ModelUnavailable,
        1_001.0,
    )
    .model("model-a", Some("upstream-a".into()), "openai");
    let model_result = applier.apply(&model_effect).await.expect("model effect");
    assert!(model_result.model_changed);
    assert_eq!(
        manager
            .snapshot("account-a")
            .expect("health")
            .circuit
            .failure_count,
        0
    );
    assert!(!manager.is_model_healthy_read_only("account-a", "model-a"));
    assert_eq!(backoffs.list_all(10).await.expect("backoff rows").len(), 2);
    assert_eq!(
        quarantine_repository
            .list_all(10)
            .await
            .expect("quarantine rows")
            .len(),
        1
    );
    database.close().await.expect("close");
}

#[tokio::test(flavor = "current_thread")]
async fn rust_health_writes_are_readable_by_python_sqlite() {
    let path: PathBuf = std::env::temp_dir().join(format!(
        "eggpool-d005-{}-{}.sqlite3",
        std::process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock")
            .as_nanos()
    ));
    let database = Database::open(DatabaseConfig {
        path: path.to_string_lossy().into_owned(),
        ..DatabaseConfig::default()
    })
    .await
    .expect("database");
    MigrationRunner::new(&database)
        .run()
        .await
        .expect("migrations");
    database
        .call(|connection| {
            connection.execute(
                "INSERT INTO accounts (id,name,api_key_env,enabled,weight,provider_id) VALUES (1,'account-a','KEY',1,1.0,'provider-a')",
                [],
            )
        })
        .await
        .expect("account");
    let wall_now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("clock")
        .as_secs_f64();
    AccountBackoffRepository::new(&database)
        .upsert(&eggpool::health::AccountBackoffRecord {
            id: 0,
            account_id: 1,
            model_id: None,
            reason: BackoffReason::ProtocolError,
            status_code: Some(502),
            error_class: Some("protocol_error".into()),
            consecutive_failures: 1,
            backoff_until_epoch: Some(wall_now + 30.0),
            last_failure_epoch: wall_now,
            updated_epoch: wall_now,
        })
        .await
        .expect("backoff write");
    let quarantine = ModelQuarantine::default();
    let entry = quarantine.record_observation(
        quarantine.key("provider-a", "account-a", "model-rust", None, "openai"),
        EvidenceProvenance::RuntimeHttp,
        "model_unavailable",
        Some(404),
        Some("model_not_found".into()),
        wall_now,
    );
    ModelQuarantineRepository::new(&database)
        .upsert_entry(&entry)
        .await
        .expect("quarantine write");
    database.close().await.expect("close");
    let output = Command::new("python3")
        .args([
            "-c",
            "import sqlite3, sys; db=sqlite3.connect(sys.argv[1]); print(db.execute(\"SELECT reason FROM account_backoffs WHERE account_id=1\").fetchone()); print(db.execute(\"SELECT state, evidence_provenance FROM model_quarantine WHERE canonical_model_id='model-rust'\").fetchone())",
        ])
        .arg(&path)
        .output()
        .expect("Python is available");
    assert!(output.status.success());
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(stdout.contains("protocol_error"));
    assert!(stdout.contains("suspected"));
    fs::remove_file(path).expect("temporary database removed");
}

#[test]
fn backoff_hydration_uses_remaining_wall_duration_and_caps_it() {
    let now = Arc::new(AtomicU64::new(10));
    let manager = {
        let now = Arc::clone(&now);
        HealthManager::with_clock(move || now.load(Ordering::Relaxed) as f64)
    };
    manager.register_account(1, "account-a");
    let records = [eggpool::health::AccountBackoffRecord {
        id: 1,
        account_id: 1,
        model_id: None,
        reason: BackoffReason::RateLimited,
        status_code: Some(429),
        error_class: Some("rate_limit".into()),
        consecutive_failures: 2,
        backoff_until_epoch: Some(9_999.0),
        last_failure_epoch: 1.0,
        updated_epoch: 1.0,
    }];
    manager
        .hydrate_backoffs(&records, &BTreeMap::from([(1, "account-a".into())]), 10.0)
        .expect("hydrate");
    let snapshot = manager.snapshot("account-a").expect("health");
    assert!(snapshot.cooldown_until <= 1_810.0);
    assert!(snapshot.cooldown_until > 10.0);
}
