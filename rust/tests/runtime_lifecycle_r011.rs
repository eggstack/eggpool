//! R011 integrated M8 qualification and closure matrix.
//!
//! These tests deliberately compose the public process/runtime, reload, task,
//! Axum, and SQLite boundaries.  The slice tests prove each mechanism in
//! isolation; this binary proves that the mechanisms still agree when a live
//! generation is reloaded, retired, and shut down.

use std::{collections::BTreeMap, sync::Arc, time::Duration};

use axum::body::Body;
use eggpool::{
    Config,
    config::{AccountConfig, ModelRouteConfig, ModelRouterConfig, ProviderConfig},
    db::{AccountRepository, Database, DatabaseConfig, MigrationRunner},
    reload::{ReloadResultCategory, ReloadService},
    runtime_lifecycle::{
        CandidateOwnership, GenerationSlotState, ProcessRuntime, RuntimeGenerationFactory,
        RuntimeManager, RuntimeTaskCapability,
    },
    server::{AppState, build_router},
};
use http::{Request, StatusCode};
use serde_json::Value;
use tempfile::TempDir;
use tokio::time::{sleep, timeout};
use tower::ServiceExt;

const R001_ORACLE: &str =
    include_str!("../../tests/fixtures/runtime/compatibility-observations.json");

struct Fixture {
    _directory: TempDir,
    database: Database,
    process: ProcessRuntime,
    manager: RuntimeManager,
    reload: ReloadService,
}

async fn fixture(install_tasks: bool) -> Fixture {
    let directory = tempfile::tempdir().expect("temporary directory");
    let database = Database::open(DatabaseConfig {
        path: directory
            .path()
            .join("runtime.sqlite3")
            .to_string_lossy()
            .into_owned(),
        ..DatabaseConfig::default()
    })
    .await
    .expect("database opens");
    MigrationRunner::new(&database)
        .run()
        .await
        .expect("migrations run");
    let process = ProcessRuntime::new(database.clone());
    let candidate = RuntimeGenerationFactory::prepare(
        &process,
        Config::default(),
        "r011-generation-a".to_owned(),
        1,
    )
    .await
    .expect("generation A prepares");
    let manager = RuntimeManager::new(candidate.transfer().expect("generation A transfers"));
    if install_tasks {
        process
            .install_initial_tasks(manager.clone(), &Config::default())
            .await
            .expect("initial task specs install");
    }
    let reload = process.reload_service(manager.clone());
    Fixture {
        _directory: directory,
        database,
        process,
        manager,
        reload,
    }
}

async fn close_fixture(fixture: Fixture) {
    let _ = fixture
        .process
        .task_supervisor()
        .shutdown_with_timeout(Duration::from_secs(1))
        .await;
    let _ = fixture
        .manager
        .close_for_shutdown(Duration::from_secs(1), false)
        .await;
    fixture
        .database
        .close()
        .await
        .expect("database closes last");
}

fn toml_for(config: &Config) -> Vec<u8> {
    toml::to_string(config)
        .expect("configuration serializes")
        .into_bytes()
}

fn router_config() -> ModelRouterConfig {
    ModelRouterConfig {
        selector_model: "selector-model".to_owned(),
        default_model: "model-default".to_owned(),
        routes: BTreeMap::from([
            (
                "default".to_owned(),
                ModelRouteConfig {
                    model: "model-default".to_owned(),
                    description: "default route".to_owned(),
                },
            ),
            (
                "fast".to_owned(),
                ModelRouteConfig {
                    model: "model-fast".to_owned(),
                    description: "fast route".to_owned(),
                },
            ),
        ]),
        ..ModelRouterConfig::default()
    }
}

#[test]
fn qualification_is_anchored_to_the_committed_r001_oracle() {
    let oracle: Value = serde_json::from_str(R001_ORACLE).expect("R001 oracle JSON");
    assert_eq!(
        oracle["schema_version"],
        "m8-runtime-lifecycle-r001-observations/v1"
    );
    let cases = oracle["reload"]["cases"]
        .as_array()
        .expect("R001 reload cases");
    assert_eq!(cases.len(), 13);
    assert!(
        cases
            .iter()
            .any(|case| case["name"] == "valid_live_only_change")
    );
    assert!(
        cases
            .iter()
            .any(|case| case["name"] == "cancellation_during_or_after_acceptance")
    );
    assert_eq!(
        oracle["config_policy"]["default_unknown_disposition"],
        "restart_required"
    );
    assert_eq!(
        oracle["normalization"]["excluded"]
            .as_array()
            .unwrap()
            .len(),
        4
    );
}

#[tokio::test]
async fn factory_reload_and_process_state_have_one_coherent_boundary() {
    let fixture = fixture(false).await;
    let affinity = fixture.process.model_router_affinity();
    let first_generation = fixture.manager.active_generation();
    let first_inference = Arc::clone(first_generation.inference());

    let mut next = Config::default();
    next.server.max_request_body_bytes += 1;
    next.model_routers
        .insert("virtual-route".to_owned(), router_config());
    next.models.refresh_interval_s = 17;
    let result = fixture
        .reload
        .reload_bytes("config.toml", toml_for(&next), None)
        .await;

    assert_eq!(result.category, ReloadResultCategory::Applied);
    assert_eq!(result.active_generation_id, 2);
    assert_eq!(fixture.manager.publication_epoch(), 1);
    let second_generation = fixture.manager.active_generation();
    assert!(!Arc::ptr_eq(&first_generation, &second_generation));
    assert!(!Arc::ptr_eq(
        &first_inference,
        second_generation.inference()
    ));
    assert_eq!(
        second_generation.config().server.max_request_body_bytes,
        next.server.max_request_body_bytes
    );
    assert_eq!(second_generation.config().model_routers.len(), 1);
    assert_eq!(
        second_generation
            .config()
            .model_routers
            .get("virtual-route")
            .expect("reloaded router")
            .default_model,
        "model-default"
    );
    assert!(Arc::ptr_eq(
        &affinity,
        &fixture.process.model_router_affinity()
    ));
    assert_eq!(
        fixture.manager.active_slot().state(),
        GenerationSlotState::Active
    );
    assert!(!fixture.manager.admission_closed());

    close_fixture(fixture).await;
}

#[tokio::test]
async fn reload_matrix_matches_r001_and_keeps_task_state_transactional() {
    let fixture = fixture(true).await;
    let initial_generation = fixture.manager.active_slot().generation_id();
    let initial_transition_count = fixture.process.task_supervisor().transition_count();

    let noop = fixture
        .reload
        .reload_bytes("config.toml", toml_for(&Config::default()), None)
        .await;
    assert_eq!(noop.category, ReloadResultCategory::Noop);
    assert_eq!(fixture.manager.publication_epoch(), 0);

    let mut live = Config::default();
    live.server.max_request_body_bytes += 1;
    live.models.refresh_interval_s = 7;
    live.model_routers
        .insert("virtual-route".to_owned(), router_config());
    let applied = fixture
        .reload
        .reload_bytes("config.toml", toml_for(&live), None)
        .await;
    assert_eq!(applied.category, ReloadResultCategory::Applied);
    assert_eq!(fixture.manager.publication_epoch(), 1);
    assert_eq!(fixture.manager.active_slot().generation_id(), 2);
    assert_eq!(fixture.process.task_supervisor().task_count(), 3);
    assert_eq!(
        fixture.process.task_supervisor().transition_count(),
        initial_transition_count + 1
    );
    assert_eq!(
        fixture
            .process
            .task_supervisor()
            .task_snapshot("catalog_refresh")
            .expect("catalog task")
            .reschedule_count,
        1
    );

    let mut restart = live.clone();
    restart.server.port += 1;
    let restart_result = fixture
        .reload
        .reload_bytes("config.toml", toml_for(&restart), None)
        .await;
    assert_eq!(
        restart_result.category,
        ReloadResultCategory::RestartRequired
    );
    assert_eq!(fixture.manager.active_slot().generation_id(), 2);

    let mut mixed = live.clone();
    mixed.server.port += 1;
    mixed.server.max_request_body_bytes += 1;
    let mixed_result = fixture
        .reload
        .reload_bytes("config.toml", toml_for(&mixed), None)
        .await;
    assert_eq!(mixed_result.category, ReloadResultCategory::RestartRequired);
    assert_eq!(fixture.manager.publication_epoch(), 1);

    let invalid = fixture
        .reload
        .reload_bytes("config.toml", b"[server\n".to_vec(), None)
        .await;
    assert_eq!(invalid.category, ReloadResultCategory::ValidationFailed);
    let stale = fixture
        .reload
        .reload_bytes("config.toml", toml_for(&live), Some("stale".to_owned()))
        .await;
    assert_eq!(stale.category, ReloadResultCategory::StaleDigest);
    assert_eq!(fixture.manager.active_slot().generation_id(), 2);
    assert_eq!(
        initial_generation + 1,
        fixture.manager.active_slot().generation_id()
    );
    assert!(!fixture.manager.admission_closed());

    close_fixture(fixture).await;
}

#[tokio::test]
async fn publication_gate_has_no_old_generation_leases_after_commit() {
    let fixture = fixture(false).await;
    let old_lease = fixture.manager.acquire().await.expect("generation A lease");
    let candidate = RuntimeGenerationFactory::prepare(
        &fixture.process,
        Config::default(),
        "r011-generation-b".to_owned(),
        2,
    )
    .await
    .expect("generation B prepares");
    let mut staged = fixture
        .manager
        .stage(1, &candidate)
        .expect("candidate stages");

    let manager = fixture.manager.clone();
    let waiters = (0..32)
        .map(|_| {
            let manager = manager.clone();
            tokio::spawn(async move { manager.acquire().await.expect("waiter acquires") })
        })
        .collect::<Vec<_>>();
    timeout(Duration::from_secs(1), async {
        loop {
            if fixture
                .process
                .diagnostics(&fixture.manager)
                .publication
                .reload_gate_waiters
                >= 32
            {
                break;
            }
            sleep(Duration::from_millis(1)).await;
        }
    })
    .await
    .expect("all waiters observe the closed gate");

    staged.commit_pointer().expect("pointer commits");
    assert_eq!(fixture.manager.active_slot().generation_id(), 2);
    assert!(fixture.manager.admission_closed());
    assert_eq!(fixture.manager.publication_epoch(), 0);
    assert_eq!(old_lease.generation_id(), 1);
    staged.accept().expect("publication accepts");

    for waiter in waiters {
        assert_eq!(waiter.await.expect("waiter joins").generation_id(), 2);
    }
    drop(old_lease);
    assert_eq!(fixture.manager.publication_epoch(), 1);
    assert_eq!(fixture.manager.active_slot().active_lease_count(), 0);

    close_fixture(fixture).await;
}

#[tokio::test]
async fn cancelled_gate_waiter_and_retirement_backlog_leave_bounded_state() {
    let fixture = fixture(false).await;
    let mut retained_leases = Vec::new();

    for generation_id in 2..=5 {
        retained_leases.push(fixture.manager.acquire().await.expect("old lease"));
        let candidate = RuntimeGenerationFactory::prepare(
            &fixture.process,
            Config::default(),
            format!("r011-generation-{generation_id}"),
            generation_id,
        )
        .await
        .expect("candidate prepares");
        let mut staged = fixture
            .manager
            .stage(generation_id - 1, &candidate)
            .expect("candidate stages");
        staged.commit_pointer().expect("pointer commits");
        staged.accept().expect("publication accepts");
    }
    assert_eq!(fixture.manager.retiring_slot_count(), 4);

    let blocked_candidate = RuntimeGenerationFactory::prepare(
        &fixture.process,
        Config::default(),
        "r011-backlog-blocked".to_owned(),
        6,
    )
    .await
    .expect("blocked candidate prepares");
    let blocked = fixture.manager.stage(5, &blocked_candidate);
    assert!(matches!(
        blocked,
        Err(eggpool::runtime_lifecycle::GenerationStageError::RetirementBacklog)
    ));
    assert_eq!(blocked_candidate.ownership(), CandidateOwnership::Prepared);
    blocked_candidate.abort().await;

    drop(retained_leases);
    fixture.manager.drain_retirements().await;
    assert_eq!(fixture.manager.retiring_slot_count(), 0);

    let waiter = {
        let candidate = RuntimeGenerationFactory::prepare(
            &fixture.process,
            Config::default(),
            "r011-cancel-waiter".to_owned(),
            6,
        )
        .await
        .expect("waiter candidate prepares");
        let mut staged = fixture
            .manager
            .stage(5, &candidate)
            .expect("stages after reaping");
        let manager = fixture.manager.clone();
        let waiter = tokio::spawn(async move { manager.acquire().await });
        timeout(Duration::from_secs(1), async {
            loop {
                if fixture
                    .process
                    .diagnostics(&fixture.manager)
                    .publication
                    .reload_gate_waiters
                    > 0
                {
                    break;
                }
                sleep(Duration::from_millis(1)).await;
            }
        })
        .await
        .expect("cancelled waiter registers");
        waiter.abort();
        let _ = waiter.await;
        let returned = staged.rollback().expect("rollback reopens gate");
        returned.close().await;
        fixture
            .process
            .diagnostics(&fixture.manager)
            .publication
            .reload_gate_waiters
    };
    assert_eq!(waiter, 0);

    close_fixture(fixture).await;
}

#[tokio::test]
async fn axum_live_authority_uses_reloaded_body_limit_before_buffering() {
    let fixture = fixture(false).await;
    let mut next = Config::default();
    next.server.max_request_body_bytes = 32;
    let result = fixture
        .reload
        .reload_bytes("config.toml", toml_for(&next), None)
        .await;
    assert_eq!(result.category, ReloadResultCategory::Applied);

    let app = build_router(AppState::from_runtime(
        Config::default(),
        fixture.database.clone(),
        Arc::new(fixture.manager.clone()),
    ));
    let request = Request::builder()
        .method("POST")
        .uri("/v1/chat/completions")
        .body(Body::from(vec![b'x'; 33]))
        .expect("request builds");
    let response = app.oneshot(request).await.expect("request completes");
    assert_eq!(response.status(), StatusCode::PAYLOAD_TOO_LARGE);

    close_fixture(fixture).await;
}

#[tokio::test]
async fn task_inventory_is_real_or_explicitly_deferred_and_never_duplicates() {
    let fixture = fixture(true).await;
    let inventory = fixture.process.task_capability_inventory();
    assert_eq!(inventory.len(), 6);
    assert_eq!(
        inventory
            .iter()
            .filter(|capability| capability.registered)
            .map(|capability| capability.name.as_str())
            .collect::<Vec<_>>(),
        vec![
            "catalog_refresh",
            "retention_cleanup",
            "checkpoint",
            "update_checker",
        ]
    );
    for capability in inventory.iter().filter(|capability| !capability.registered) {
        assert!(capability.future_owner.is_some());
        assert!(capability.reason.is_some());
    }
    assert_eq!(fixture.process.task_supervisor().task_count(), 3);
    assert_eq!(fixture.process.task_supervisor().join_handle_count(), 3);

    let mut next = Config::default();
    next.models.refresh_interval_s = 1;
    let result = fixture
        .reload
        .reload_bytes("config.toml", toml_for(&next), None)
        .await;
    assert_eq!(result.category, ReloadResultCategory::Applied);
    assert_eq!(fixture.process.task_supervisor().task_count(), 3);
    assert_eq!(fixture.process.task_supervisor().join_handle_count(), 3);
    assert!(
        fixture
            .process
            .task_supervisor()
            .snapshot()
            .iter()
            .any(|snapshot| snapshot.name == "catalog_refresh" && snapshot.reschedule_count == 1)
    );

    close_fixture(fixture).await;
}

#[tokio::test]
async fn startup_recovery_precedes_first_request_and_same_db_remains_readable() {
    let directory = tempfile::tempdir().expect("temporary directory");
    let path = directory.path().join("recovery.sqlite3");
    let database = Database::open(DatabaseConfig {
        path: path.to_string_lossy().into_owned(),
        ..DatabaseConfig::default()
    })
    .await
    .expect("database opens");
    MigrationRunner::new(&database)
        .run()
        .await
        .expect("migrations run");
    database
        .with_transaction(|connection| {
            connection.execute(
                "INSERT INTO providers (provider_id, base_url, protocols) VALUES ('recovery', 'https://recovery.invalid', '[\"openai\"]')",
                [],
            )?;
            connection.execute(
                "INSERT INTO accounts (name, api_key_env, enabled, weight, provider_id) VALUES ('recovery-account', 'RECOVERY_KEY', 1, 1.0, 'recovery')",
                [],
            )?;
            connection.execute(
                "INSERT INTO models (model_id, protocol, provider_id) VALUES ('recovery-model', 'openai', 'recovery')",
                [],
            )?;
            connection.execute(
                "INSERT INTO requests (account_id, model_id, status) VALUES (1, 'recovery-model', 'pending')",
                [],
            )?;
            Ok(())
        })
        .await
        .expect("seed interrupted request");

    let process = ProcessRuntime::new(database.clone());
    let recovery = process
        .reconcile_startup()
        .await
        .expect("recovery converges");
    assert!(recovery.converged);
    assert_eq!(recovery.requests_interrupted, 1);
    let mut recovered_config = Config::default();
    recovered_config.providers.insert(
        "recovery".to_owned(),
        ProviderConfig {
            id: "recovery".to_owned(),
            base_url: "https://recovery.invalid".to_owned(),
            accounts: vec![AccountConfig {
                name: "recovery-account".to_owned(),
                api_key: Some("recovery-test-key".to_owned()),
                api_key_env: "RECOVERY_KEY".to_owned(),
                ..AccountConfig::default()
            }],
            ..ProviderConfig::default()
        },
    );
    let candidate = RuntimeGenerationFactory::prepare(
        &process,
        recovered_config.clone(),
        "r011-recovered".to_owned(),
        1,
    )
    .await
    .expect("generation prepares after recovery");
    let manager = RuntimeManager::new(candidate.transfer().expect("generation transfers"));
    let app = build_router(AppState::from_runtime(
        recovered_config,
        database.clone(),
        Arc::new(manager.clone()),
    ));
    let response = app
        .oneshot(
            Request::builder()
                .uri("/v1/healthz")
                .body(Body::empty())
                .expect("health request builds"),
        )
        .await
        .expect("health request completes");
    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(
        AccountRepository::new(&database)
            .list_all()
            .await
            .unwrap()
            .len(),
        1
    );

    manager
        .close_for_shutdown(Duration::from_secs(1), false)
        .await;
    database.close().await.expect("first DB closes");

    let reopened = Database::open(DatabaseConfig {
        path: path.to_string_lossy().into_owned(),
        ..DatabaseConfig::default()
    })
    .await
    .expect("same DB reopens");
    MigrationRunner::new(&reopened)
        .run()
        .await
        .expect("same DB remains Python-compatible");
    let statuses = reopened
        .call(|connection| {
            let status: String =
                connection
                    .query_row("SELECT status FROM requests LIMIT 1", [], |row| row.get(0))?;
            Ok(status)
        })
        .await
        .expect("recovered row reads");
    assert_eq!(statuses, "interrupted");
    reopened.close().await.expect("reopened DB closes");
}

#[tokio::test]
async fn diagnostics_and_debug_remain_bounded_and_secret_free_after_cycles() {
    let fixture = fixture(true).await;
    let sentinel = "r011-api-key-proxy-session-body-error-secret";
    let mut next = Config::default();
    next.server.api_key = Some(sentinel.to_owned());
    next.pricing.catalogs.openrouter.api_key = Some(sentinel.to_owned());
    let result = fixture
        .reload
        .reload_bytes("config.toml", toml_for(&next), None)
        .await;
    assert_eq!(result.category, ReloadResultCategory::RestartRequired);

    for cycle in 0..8 {
        let mut live = Config::default();
        live.server.max_request_body_bytes += cycle + 1;
        let result = fixture
            .reload
            .reload_bytes("config.toml", toml_for(&live), None)
            .await;
        assert_eq!(result.category, ReloadResultCategory::Applied);
        fixture.manager.drain_retirements().await;
    }
    let snapshot = fixture.process.diagnostics(&fixture.manager);
    let json = serde_json::to_string(&snapshot).expect("diagnostic JSON");
    let debug = format!("{snapshot:?}");
    assert!(snapshot.retiring_generations.len() <= 4);
    assert!(snapshot.tasks.len() <= 6);
    assert!(snapshot.reload.last_result.is_some());
    assert!(!json.contains(sentinel));
    assert!(!debug.contains(sentinel));
    assert!(!format!("{:?}", fixture.manager.active_generation()).contains(sentinel));
    assert!(
        fixture
            .process
            .task_capability_inventory()
            .iter()
            .all(|capability: &RuntimeTaskCapability| capability.name.len() <= 64)
    );

    close_fixture(fixture).await;
}

#[test]
fn authority_source_audit_has_no_long_lived_generation_service_escape() {
    let source = std::fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/src/server.rs"))
        .expect("server source");
    assert!(source.contains("pub runtime: Arc<RuntimeManager>"));
    assert!(!source.contains("pub inference: Arc<InferenceState>"));
    assert!(!source.contains("pub client_pool: ProviderClientPool"));
    assert!(source.contains("Extension<Arc<GenerationLease>>"));
    assert!(source.contains("Limited::new(body, limit)"));
}
