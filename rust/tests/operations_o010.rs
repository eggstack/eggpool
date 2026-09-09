//! O010 closure evidence for the complete M9 process-task boundary.

use std::{collections::BTreeSet, fs};

use eggpool::{
    Config,
    db::{Database, DatabaseConfig, MigrationRunner},
    runtime_lifecycle::ProcessRuntime,
    task_supervisor::{TaskOwnership, runtime_task_specs_for_config},
};

#[tokio::test]
async fn all_three_m9_callbacks_are_registered_once_in_a_configured_runtime() {
    let directory = tempfile::tempdir().expect("temporary directory");
    let database_path = directory.path().join("usage.sqlite3");
    let config_path = directory.path().join("config.toml");
    let mut config = Config::default();
    config.database.path = database_path.display().to_string();
    config.backup.enabled = true;
    config.update_checker.enabled = true;
    fs::write(
        &config_path,
        toml::to_string(&config).expect("config serializes"),
    )
    .expect("config writes");

    let database = Database::open(DatabaseConfig {
        path: database_path.display().to_string(),
        ..Default::default()
    })
    .await
    .expect("database opens");
    MigrationRunner::new(&database)
        .run()
        .await
        .expect("canonical migrations apply");
    let process =
        ProcessRuntime::with_config_path_and_config(database.clone(), &config_path, &config)
            .expect("configured process runtime");

    let inventory = process.task_capability_inventory();
    assert_eq!(inventory.len(), 6);
    assert!(inventory.iter().all(|capability| capability.registered));
    assert!(
        inventory
            .iter()
            .all(|capability| { capability.future_owner.is_none() && capability.reason.is_none() })
    );
    let registered = process
        .task_supervisor()
        .available_callback_kinds()
        .into_iter()
        .collect::<BTreeSet<_>>();
    assert_eq!(registered.len(), 6);
    assert!(registered.contains("metrics_flush"));
    assert!(registered.contains("update_checker"));
    assert!(registered.contains("automatic_backup"));

    let specs = runtime_task_specs_for_config(&config, true);
    assert_eq!(specs.len(), 6);
    assert_eq!(
        specs
            .iter()
            .map(|spec| spec.name.as_str())
            .collect::<BTreeSet<_>>()
            .len(),
        6
    );
    for name in ["metrics_flush", "update_checker", "automatic_backup"] {
        let spec = specs
            .iter()
            .find(|spec| spec.name == name)
            .expect("M9 task spec");
        assert_eq!(spec.ownership, TaskOwnership::Process);
        assert!(spec.enabled);
    }

    process.task_supervisor().shutdown().await;
    database.close().await.expect("database closes");
}
