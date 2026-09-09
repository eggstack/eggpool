//! R008 startup recovery and generation-leased maintenance integration.

use std::collections::BTreeMap;

use eggpool::{
    Config,
    db::{Database, DatabaseConfig, MigrationRunner, RetentionCleanupPolicy},
    runtime_lifecycle::{ProcessRuntime, RuntimeGenerationFactory, RuntimeManager},
};
async fn database() -> Database {
    let database = Database::open(DatabaseConfig::default())
        .await
        .expect("database");
    MigrationRunner::new(&database)
        .run()
        .await
        .expect("migrations");
    database
}

#[tokio::test]
async fn startup_recovery_converges_multiple_bounded_passes_without_provider_work() {
    let database = database().await;
    database
        .with_transaction(|connection| {
            connection.execute(
                "INSERT INTO providers (provider_id, base_url, protocols) VALUES ('fixture', 'https://fixture.invalid', '[\"openai\"]')",
                [],
            )?;
            connection.execute(
                "INSERT INTO accounts (name, api_key_env, enabled, weight, provider_id) VALUES ('fixture-account', 'FIXTURE_KEY', 1, 1.0, 'fixture')",
                [],
            )?;
            connection.execute(
                "INSERT INTO models (model_id, protocol, provider_id) VALUES ('fixture-model', 'openai', 'fixture')",
                [],
            )?;
            for _ in 0..501 {
                connection.execute(
                    "INSERT INTO requests (account_id, model_id, status) VALUES (1, 'fixture-model', 'pending')",
                    [],
                )?;
            }
            Ok(())
        })
        .await
        .expect("seed crash leftovers");

    let process = ProcessRuntime::new(database.clone());
    let report = process.reconcile_startup().await.expect("startup recovery");
    assert!(report.passes >= 2);
    assert_eq!(report.requests_interrupted, 501);
    assert!(report.converged);
    assert_eq!(process.startup_recovery_report(), Some(report.clone()));

    let second = eggpool::coordinator::CrashReconciler::new(database.clone())
        .reconcile_once()
        .await
        .expect("second recovery pass");
    assert!(second.converged);
    assert_eq!(second.fixed_total(), 0);
    database.close().await.expect("database close");
}

#[tokio::test]
async fn maintenance_capabilities_are_registered_or_explicitly_deferred() {
    let database = database().await;
    let process = ProcessRuntime::new(database.clone());
    let capabilities = process.task_capability_inventory();
    let by_name = capabilities
        .into_iter()
        .map(|capability| (capability.name.clone(), capability))
        .collect::<BTreeMap<_, _>>();

    for name in ["catalog_refresh", "retention_cleanup", "checkpoint"] {
        assert!(by_name[name].registered, "{name} should be implemented");
    }
    for name in ["metrics_flush", "automatic_backup"] {
        assert!(!by_name[name].registered, "{name} must remain deferred");
        assert!(by_name[name].future_owner.is_some());
        assert!(by_name[name].reason.is_some());
    }
    assert!(by_name["update_checker"].registered);
    assert!(by_name["update_checker"].future_owner.is_none());

    let candidate = RuntimeGenerationFactory::prepare(
        &process,
        Config::default(),
        "r008-initial".to_owned(),
        1,
    )
    .await
    .expect("generation");
    let manager = RuntimeManager::new(candidate.transfer().expect("transfer"));
    process
        .install_initial_tasks(manager.clone(), &Config::default())
        .await
        .expect("initial tasks");
    let names = process
        .task_supervisor()
        .snapshot()
        .into_iter()
        .map(|snapshot| snapshot.name)
        .collect::<Vec<_>>();
    assert_eq!(
        names,
        vec!["catalog_refresh", "checkpoint", "retention_cleanup"]
    );

    manager.shutdown();
    let report = process.task_supervisor().shutdown().await;
    assert_eq!(report.remaining, 0);
    let _ = manager.active_generation().close().await;
    database.close().await.expect("database close");
}

#[tokio::test]
async fn retention_cleanup_is_bounded_and_preserves_pending_requests() {
    let database = database().await;
    database
        .with_transaction(|connection| {
            connection.execute(
                "INSERT INTO providers (provider_id, base_url, protocols) VALUES ('fixture', 'https://fixture.invalid', '[\"openai\"]')",
                [],
            )?;
            connection.execute(
                "INSERT INTO accounts (name, api_key_env, enabled, weight, provider_id) VALUES ('fixture-account', 'FIXTURE_KEY', 1, 1.0, 'fixture')",
                [],
            )?;
            connection.execute(
                "INSERT INTO models (model_id, protocol, provider_id) VALUES ('fixture-model', 'openai', 'fixture')",
                [],
            )?;
            connection.execute(
                "INSERT INTO requests (account_id, model_id, status, started_at) VALUES (1, 'fixture-model', 'completed', datetime('now', '-100 days'))",
                [],
            )?;
            connection.execute(
                "INSERT INTO requests (account_id, model_id, status, started_at) VALUES (1, 'fixture-model', 'pending', datetime('now', '-100 days'))",
                [],
            )?;
            connection.execute(
                "INSERT INTO operational_events (event_type, occurred_at) VALUES ('fixture', datetime('now', '-100 days'))",
                [],
            )?;
            Ok(())
        })
        .await
        .expect("seed historical rows");

    let report = database
        .cleanup_retention(RetentionCleanupPolicy {
            request_days: 30,
            event_days: 30,
            ping_days: 30,
            operational_event_days: 30,
            routing_decision_days: 30,
            rollup_days: 30,
            price_snapshot_days: 30,
            model_info_observation_days: 30,
            max_rows_per_batch: 1,
            max_batches: 1,
            max_tick_duration: std::time::Duration::from_secs(1),
        })
        .await
        .expect("retention cleanup");
    assert!(report.rows_changed > 0);
    assert_eq!(report.batches_completed, 1);
    assert!(report.budget_exhausted);

    let counts = database
        .call(|connection| {
            let completed: i64 = connection.query_row(
                "SELECT COUNT(*) FROM requests WHERE status = 'completed'",
                [],
                |row| row.get(0),
            )?;
            let pending: i64 = connection.query_row(
                "SELECT COUNT(*) FROM requests WHERE status = 'pending'",
                [],
                |row| row.get(0),
            )?;
            Ok((completed, pending))
        })
        .await
        .expect("read cleanup result");
    assert_eq!(counts, (0, 1));
    database.close().await.expect("database close");
}
