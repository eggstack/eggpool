//! R007 transactional live-rehash and coherent-acceptance contracts.

use std::sync::Arc;

use eggpool::{
    Config,
    config::{AccountConfig, ProviderConfig},
    db::{AccountRepository, Database, DatabaseConfig, MigrationRunner},
    reload::{ReloadResultCategory, ReloadService},
    runtime_lifecycle::{ProcessRuntime, RuntimeGenerationFactory, RuntimeManager},
};

async fn fixture() -> (Database, ProcessRuntime, RuntimeManager, ReloadService) {
    let database = Database::open(DatabaseConfig::default())
        .await
        .expect("database");
    MigrationRunner::new(&database)
        .run()
        .await
        .expect("migrations");
    let process = ProcessRuntime::new(database.clone());
    let candidate = RuntimeGenerationFactory::prepare(
        &process,
        Config::default(),
        "initial-digest".to_owned(),
        1,
    )
    .await
    .expect("initial generation");
    let manager = RuntimeManager::new(candidate.transfer().expect("initial transfer"));
    let reload = process.reload_service(manager.clone());
    (database, process, manager, reload)
}

fn toml_for(config: &Config) -> Vec<u8> {
    toml::to_string(config).expect("config TOML").into_bytes()
}

#[tokio::test]
async fn identical_semantic_config_is_a_noop_without_candidate_or_epoch_change() {
    let (database, _process, manager, reload) = fixture().await;
    let before = manager.active_generation().config().clone();
    let result = reload
        .reload_bytes("config.toml", toml_for(&before), None)
        .await;
    assert_eq!(result.category, ReloadResultCategory::Noop);
    assert_eq!(result.active_generation_id, 1);
    assert_eq!(manager.publication_epoch(), 0);
    assert_eq!(manager.retiring_slot_count(), 0);
    database.close().await.expect("database close");
}

#[tokio::test]
async fn live_reload_keeps_gate_narrow_and_accepts_one_coherent_generation() {
    let (database, _process, manager, reload) = fixture().await;
    let mut next = Config::default();
    next.server.max_request_body_bytes += 1;
    let result = reload
        .reload_bytes("config.toml", toml_for(&next), None)
        .await;
    assert_eq!(result.category, ReloadResultCategory::Applied);
    assert_eq!(result.active_generation_id, 2);
    assert_eq!(manager.publication_epoch(), 1);
    assert!(!manager.admission_closed());
    assert_eq!(
        manager
            .active_generation()
            .config()
            .server
            .max_request_body_bytes,
        next.server.max_request_body_bytes
    );
    manager.drain_retirements().await;
    database.close().await.expect("database close");
}

#[tokio::test]
async fn restart_invalid_and_stale_inputs_leave_runtime_unchanged() {
    let (database, _process, manager, reload) = fixture().await;
    let mut restart = Config::default();
    restart.server.port += 1;
    let result = reload
        .reload_bytes("config.toml", toml_for(&restart), None)
        .await;
    assert_eq!(result.category, ReloadResultCategory::RestartRequired);
    assert_eq!(manager.active_slot().generation_id(), 1);

    let invalid = reload
        .reload_bytes("config.toml", b"[server\n".to_vec(), None)
        .await;
    assert_eq!(invalid.category, ReloadResultCategory::ValidationFailed);
    assert_eq!(manager.active_slot().generation_id(), 1);

    let stale = reload
        .reload_bytes(
            "config.toml",
            toml_for(&Config::default()),
            Some("wrong".into()),
        )
        .await;
    assert_eq!(stale.category, ReloadResultCategory::StaleDigest);
    assert_eq!(manager.active_slot().generation_id(), 1);
    assert_eq!(manager.publication_epoch(), 0);
    database.close().await.expect("database close");
}

#[tokio::test]
async fn new_account_is_projected_before_candidate_build_and_persisted_atomically() {
    let (database, process, _manager, _reload) = fixture().await;
    let mut old = Config::default();
    old.providers.insert(
        "provider".to_owned(),
        ProviderConfig {
            id: "provider".to_owned(),
            base_url: "https://provider.example.test".to_owned(),
            accounts: vec![AccountConfig {
                name: "one".to_owned(),
                api_key: Some("test-key-one".to_owned()),
                ..AccountConfig::default()
            }],
            ..ProviderConfig::default()
        },
    );
    AccountRepository::new(&database)
        .sync_from_config(vec![eggpool::db::AccountConfig {
            name: "one".to_owned(),
            api_key_env: "ACCOUNT_ONE".to_owned(),
            enabled: true,
            weight: 1.0,
            provider_id: "provider".to_owned(),
        }])
        .await
        .expect("initial account sync");
    let first = RuntimeGenerationFactory::prepare(&process, old.clone(), "old".to_owned(), 1)
        .await
        .expect("old graph");
    let old_generation = first.transfer().expect("old transfer");
    let manager = RuntimeManager::new(old_generation);
    let reload = process.reload_service(manager.clone());

    let mut next = old;
    next.providers
        .get_mut("provider")
        .expect("provider")
        .accounts
        .push(AccountConfig {
            name: "two".to_owned(),
            api_key: Some("test-key-two".to_owned()),
            ..AccountConfig::default()
        });
    let result = reload
        .reload_bytes("config.toml", toml_for(&next), None)
        .await;
    assert_eq!(result.category, ReloadResultCategory::Applied);
    let accounts = AccountRepository::new(&database)
        .list_all()
        .await
        .expect("accounts");
    assert_eq!(accounts.len(), 2);
    assert!(accounts.iter().any(|account| account.name == "one"));
    assert!(accounts.iter().any(|account| account.name == "two"));
    manager.drain_retirements().await;
    database.close().await.expect("database close");
}

#[tokio::test]
async fn concurrent_callers_are_serialized_with_a_busy_result() {
    let (database, _process, _manager, reload) = fixture().await;
    let service = Arc::new(reload);
    let left = Arc::clone(&service);
    let right = Arc::clone(&service);
    let config = toml_for(&Config::default());
    let first = tokio::spawn(async move { left.reload_bytes("config.toml", config, None).await });
    let second = right
        .reload_bytes("config.toml", toml_for(&Config::default()), None)
        .await;
    let first = first.await.expect("first reload");
    assert!(matches!(
        (first.category, second.category),
        (ReloadResultCategory::Noop, ReloadResultCategory::Noop)
            | (ReloadResultCategory::Busy, ReloadResultCategory::Noop)
            | (ReloadResultCategory::Noop, ReloadResultCategory::Busy)
    ));
    database.close().await.expect("database close");
}

#[tokio::test]
async fn caller_cancellation_does_not_leave_admission_closed() {
    let (database, _process, manager, reload) = fixture().await;
    let task = tokio::spawn(async move {
        reload
            .reload_bytes(
                "config.toml",
                b"[server]\nmax_request_body_bytes = 10485761\n".to_vec(),
                None,
            )
            .await
    });
    task.abort();
    let _ = task.await;
    tokio::time::sleep(std::time::Duration::from_millis(25)).await;
    assert!(!manager.admission_closed());
    assert!(!manager.is_shutting_down());
    database.close().await.expect("database close");
}
