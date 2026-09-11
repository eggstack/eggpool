//! R006 process task supervisor, inventory, and staged-diff contracts.

use std::sync::{
    Arc,
    atomic::{AtomicUsize, Ordering},
};

use eggpool::{
    Config,
    db::{Database, DatabaseConfig, MigrationRunner},
    runtime_lifecycle::{
        PreparedTaskDiff, ProcessRuntime, RuntimeGenerationFactory, RuntimeManager,
        RuntimeTaskSpec, RuntimeTaskSupervisor, TaskCallbackError, TaskCallbackRegistry,
        TaskOutcome, TaskOwnership, TaskSpecError, TaskTickContext, runtime_task_inventory,
        runtime_task_specs_for_config, task_callback,
    },
};
use serde_json::Value;
use tokio::{
    sync::mpsc,
    time::{Duration, sleep, timeout},
};

const ORACLE: &str = include_str!("../../tests/fixtures/runtime/compatibility-observations.json");

fn callback_registry(
    kind: &str,
    callback: eggpool::runtime_lifecycle::TaskCallback,
) -> TaskCallbackRegistry {
    TaskCallbackRegistry::new().with_callback(kind, callback)
}

fn test_spec(name: &str, interval_s: f64) -> RuntimeTaskSpec {
    RuntimeTaskSpec {
        name: name.to_owned(),
        interval_s,
        initial_delay_s: None,
        run_immediately: true,
        timeout_s: None,
        ownership: TaskOwnership::Process,
        enabled: true,
        description: "deterministic test callback".to_owned(),
        reloadable_fields: Vec::new(),
        generation_dependencies: Vec::new(),
        process_dependencies: Vec::new(),
        callback_kind: name.to_owned(),
    }
}

fn inventory_projection(spec: &RuntimeTaskSpec) -> Value {
    serde_json::json!({
        "name": spec.name,
        "ownership": spec.ownership.as_str(),
        "default_enabled": spec.enabled,
        "interval_s": spec.interval_s,
        "initial_delay_s": spec.initial_delay_s,
        "run_immediately": spec.run_immediately,
        "timeout_s": spec.timeout_s,
        "description": spec.description,
        "reloadable_fields": spec.reloadable_fields,
        "generation_dependencies": spec.generation_dependencies,
        "process_dependencies": spec.process_dependencies,
        "callback_kind": spec.callback_kind,
    })
}

#[test]
fn inventory_and_resolved_defaults_match_r001() {
    let oracle: Value = serde_json::from_str(ORACLE).expect("fixture JSON");
    let inventory = runtime_task_inventory();
    let expected = oracle["tasks"]["inventory"].as_array().expect("inventory");
    assert_eq!(inventory.len(), expected.len());
    for (actual, expected) in inventory.iter().zip(expected) {
        assert_eq!(inventory_projection(actual), *expected);
    }

    let resolved = runtime_task_specs_for_config(&Config::default(), false);
    let expected_default = &oracle["tasks"]["config_variants"]["default"];
    for (actual, expected) in resolved
        .iter()
        .zip(expected_default.as_array().expect("default"))
    {
        assert_eq!(actual.name, expected["name"]);
        assert_eq!(actual.enabled, expected["enabled"]);
        assert_eq!(actual.interval_s, expected["interval_s"]);
        assert_eq!(actual.initial_delay_s, expected["initial_delay_s"].as_f64());
        assert_eq!(actual.run_immediately, expected["run_immediately"]);
    }
}

#[tokio::test]
async fn invalid_or_duplicate_specs_do_not_mutate_active_tasks() {
    let supervisor = RuntimeTaskSupervisor::new();
    let callback = task_callback(|_| async { Ok::<(), TaskCallbackError>(()) });
    let callbacks = callback_registry("test", callback);
    let duplicate = vec![test_spec("test", 1.0), test_spec("test", 1.0)];
    assert!(matches!(
        supervisor.prepare_diff_with_callbacks(&[], &duplicate, &callbacks),
        Err(TaskSpecError::DuplicateName { .. })
    ));
    let mut invalid = test_spec("test", 0.0);
    invalid.enabled = true;
    assert!(matches!(
        supervisor.prepare_diff_with_callbacks(&[], &[invalid], &callbacks),
        Err(TaskSpecError::InvalidInterval { .. })
    ));
    assert_eq!(supervisor.task_count(), 0);
    assert_eq!(supervisor.join_handle_count(), 0);
}

#[tokio::test]
async fn staged_diff_is_idle_until_commit_and_rollback_is_side_effect_free() {
    let supervisor = RuntimeTaskSupervisor::new();
    let calls = Arc::new(AtomicUsize::new(0));
    let calls_for_callback = Arc::clone(&calls);
    let callback = task_callback(move |_| {
        let calls = Arc::clone(&calls_for_callback);
        async move {
            calls.fetch_add(1, Ordering::Relaxed);
            Ok(())
        }
    });
    let callbacks = callback_registry("test", callback);
    let spec = test_spec("test", 0.01);
    let mut prepared = supervisor
        .prepare_diff_with_callbacks(&[], std::slice::from_ref(&spec), &callbacks)
        .expect("preflight");
    prepared.preflight().expect("preflight remains valid");
    sleep(Duration::from_millis(20)).await;
    assert_eq!(calls.load(Ordering::Relaxed), 0);
    prepared.rollback();
    prepared.rollback();
    assert_eq!(supervisor.task_count(), 0);

    let mut prepared = supervisor
        .prepare_diff_with_callbacks(&[], &[spec], &callbacks)
        .expect("preflight");
    let transition = prepared.commit().await.expect("commit");
    assert_eq!(transition.added, vec!["test"]);
    timeout(Duration::from_secs(1), async {
        while calls.load(Ordering::Relaxed) == 0 {
            sleep(Duration::from_millis(1)).await;
        }
    })
    .await
    .expect("callback runs after commit");
    assert_eq!(supervisor.task_count(), 1);
    supervisor.shutdown().await;
    assert_eq!(supervisor.join_handle_count(), 0);
}

#[tokio::test]
async fn slow_process_callback_never_overlaps_itself() {
    let supervisor = RuntimeTaskSupervisor::new();
    let active = Arc::new(AtomicUsize::new(0));
    let maximum = Arc::new(AtomicUsize::new(0));
    let calls = Arc::new(AtomicUsize::new(0));
    let callback = {
        let active = Arc::clone(&active);
        let maximum = Arc::clone(&maximum);
        let calls = Arc::clone(&calls);
        task_callback(move |_| {
            let active = Arc::clone(&active);
            let maximum = Arc::clone(&maximum);
            let calls = Arc::clone(&calls);
            async move {
                let current = active.fetch_add(1, Ordering::AcqRel) + 1;
                maximum.fetch_max(current, Ordering::AcqRel);
                calls.fetch_add(1, Ordering::Relaxed);
                sleep(Duration::from_millis(15)).await;
                active.fetch_sub(1, Ordering::AcqRel);
                Ok(())
            }
        })
    };
    let mut prepared = supervisor
        .prepare_diff_with_callbacks(
            &[],
            &[test_spec("slow", 0.001)],
            &callback_registry("slow", callback),
        )
        .expect("preflight");
    prepared.commit().await.expect("commit");
    timeout(Duration::from_secs(1), async {
        while calls.load(Ordering::Relaxed) < 2 {
            sleep(Duration::from_millis(1)).await;
        }
    })
    .await
    .expect("two ticks");
    assert_eq!(maximum.load(Ordering::Relaxed), 1);
    supervisor.shutdown().await;
}

#[tokio::test]
async fn timeout_and_callback_error_do_not_stop_future_ticks() {
    let supervisor = RuntimeTaskSupervisor::new();
    let calls = Arc::new(AtomicUsize::new(0));
    let calls_for_callback = Arc::clone(&calls);
    let callback = task_callback(move |_| {
        let calls = Arc::clone(&calls_for_callback);
        async move {
            let call = calls.fetch_add(1, Ordering::Relaxed);
            if call == 0 {
                sleep(Duration::from_millis(25)).await;
                return Ok(());
            }
            if call == 1 {
                return Err(TaskCallbackError::Failed);
            }
            Ok(())
        }
    });
    let mut spec = test_spec("faulty", 0.002);
    spec.timeout_s = Some(0.005);
    let mut prepared = supervisor
        .prepare_diff_with_callbacks(&[], &[spec], &callback_registry("faulty", callback))
        .expect("preflight");
    prepared.commit().await.expect("commit");
    timeout(Duration::from_secs(1), async {
        while calls.load(Ordering::Relaxed) < 3 {
            sleep(Duration::from_millis(1)).await;
        }
    })
    .await
    .expect("subsequent ticks");
    let snapshot = supervisor.task_snapshot("faulty").expect("snapshot");
    assert!(snapshot.tick_count >= 3);
    assert_eq!(snapshot.last_outcome, Some(TaskOutcome::Success));
    supervisor.shutdown().await;
}

#[tokio::test]
async fn generation_task_leases_the_current_generation_each_tick() {
    let database = Database::open(DatabaseConfig::default())
        .await
        .expect("database");
    MigrationRunner::new(&database)
        .run()
        .await
        .expect("migrations");
    let process = ProcessRuntime::new(database.clone());
    let first =
        RuntimeGenerationFactory::prepare(&process, Config::default(), "r006-a".to_owned(), 1)
            .await
            .expect("generation A")
            .transfer()
            .expect("transfer A");
    let manager = RuntimeManager::new(first);
    let second_candidate =
        RuntimeGenerationFactory::prepare(&process, Config::default(), "r006-b".to_owned(), 2)
            .await
            .expect("generation B");
    let (sender, mut receiver) = mpsc::unbounded_channel();
    let callback = task_callback(move |context| {
        let sender = sender.clone();
        async move {
            if let TaskTickContext::Generation(lease) = context {
                sender.send(lease.generation_id()).expect("receiver");
            }
            Ok(())
        }
    });
    let supervisor = RuntimeTaskSupervisor::new();
    let mut prepared = supervisor
        .prepare_diff_with_callbacks(
            &[],
            &[{
                let mut spec = test_spec("leased", 0.01);
                spec.ownership = TaskOwnership::ActiveGenerationLeased;
                spec
            }],
            &callback_registry("leased", callback),
        )
        .expect("preflight");
    prepared.commit().await.expect("commit");
    supervisor.set_generation_manager(manager.clone());
    timeout(Duration::from_secs(1), receiver.recv())
        .await
        .expect("A tick")
        .expect("A id");

    let mut staged = manager.stage(1, &second_candidate).expect("stage B");
    staged.commit_pointer().expect("pointer B");
    staged.accept().expect("accept B");
    timeout(Duration::from_secs(1), async {
        loop {
            if receiver.recv().await == Some(2) {
                break;
            }
        }
    })
    .await
    .expect("B tick");
    supervisor.shutdown().await;
    manager.drain_retirements().await;
    database.close().await.expect("database close");
}

#[tokio::test]
async fn cancellation_while_generation_admission_is_closed_leaks_no_lease() {
    let database = Database::open(DatabaseConfig::default())
        .await
        .expect("database");
    MigrationRunner::new(&database)
        .run()
        .await
        .expect("migrations");
    let process = ProcessRuntime::new(database.clone());
    let generation =
        RuntimeGenerationFactory::prepare(&process, Config::default(), "r006-gate".to_owned(), 1)
            .await
            .expect("generation")
            .transfer()
            .expect("transfer");
    let manager = RuntimeManager::new(generation);
    let candidate = RuntimeGenerationFactory::prepare(
        &process,
        Config::default(),
        "r006-candidate".to_owned(),
        2,
    )
    .await
    .expect("candidate");
    let mut staged = manager.stage(1, &candidate).expect("stage");
    let supervisor = RuntimeTaskSupervisor::new();
    supervisor.set_generation_manager(manager.clone());
    let callback = task_callback(|_| async { Ok::<(), TaskCallbackError>(()) });
    let mut spec = test_spec("gated", 1.0);
    spec.ownership = TaskOwnership::ActiveGenerationLeased;
    let mut prepared = supervisor
        .prepare_diff_with_callbacks(&[], &[spec], &callback_registry("gated", callback))
        .expect("preflight");
    prepared.commit().await.expect("commit");
    sleep(Duration::from_millis(10)).await;
    supervisor.shutdown().await;
    assert_eq!(supervisor.task_count(), 0);
    assert_eq!(supervisor.join_handle_count(), 0);
    assert_eq!(manager.active_slot().active_lease_count(), 0);
    staged.rollback().expect("rollback gate");
    database.close().await.expect("database close");
}

#[tokio::test]
async fn disabled_specs_own_no_loop_and_repeated_changes_stay_bounded() {
    let supervisor = RuntimeTaskSupervisor::new();
    let callback = task_callback(|_| async { Ok::<(), TaskCallbackError>(()) });
    let callbacks = callback_registry("disabled", callback.clone());
    let mut disabled = test_spec("disabled", 0.0);
    disabled.enabled = false;
    disabled.run_immediately = false;
    let mut prepared = supervisor
        .prepare_diff_with_callbacks(&[], &[disabled], &callbacks)
        .expect("disabled preflight");
    let transition = prepared.commit().await.expect("disabled commit");
    assert!(transition.added.is_empty());
    assert_eq!(supervisor.task_count(), 0);

    let mut current = Vec::new();
    for interval in [0.01, 0.02, 0.03, 0.04] {
        let mut next = test_spec("bounded", interval);
        next.run_immediately = false;
        next.initial_delay_s = Some(interval);
        let mut diff: PreparedTaskDiff = supervisor
            .prepare_diff_with_callbacks(
                &current,
                &[next.clone()],
                &callback_registry("bounded", callback.clone()),
            )
            .expect("reschedule preflight");
        diff.commit().await.expect("reschedule commit");
        current = vec![next];
    }
    assert_eq!(supervisor.task_count(), 1);
    assert_eq!(supervisor.snapshot().len(), 1);
    supervisor.shutdown().await;
}

#[tokio::test]
async fn deferred_callbacks_are_explicit_missing_capabilities() {
    let supervisor = RuntimeTaskSupervisor::new();
    let specs = runtime_task_specs_for_config(&Config::default(), false);
    let callbacks = TaskCallbackRegistry::new();
    let result = supervisor.prepare_diff_with_callbacks(&[], &specs, &callbacks);
    assert!(matches!(
        result,
        Err(TaskSpecError::MissingCallbackCapability { name, .. }) if name == "catalog_refresh"
    ));
}

#[tokio::test]
async fn process_runtime_owns_one_shared_supervisor() {
    let database = Database::open(DatabaseConfig::default())
        .await
        .expect("database");
    let process = ProcessRuntime::new(database);
    // Cloned handles still address one bounded task map: a commit through one
    // handle is visible through the other.
    let left = process.task_supervisor();
    let right = process.task_supervisor();
    let callback = task_callback(|_| async { Ok::<(), TaskCallbackError>(()) });
    let mut diff = left
        .prepare_diff_with_callbacks(
            &[],
            &[test_spec("shared", 1.0)],
            &callback_registry("shared", callback),
        )
        .expect("preflight");
    diff.commit().await.expect("commit");
    assert_eq!(right.task_count(), 1);
    right.shutdown().await;
}
