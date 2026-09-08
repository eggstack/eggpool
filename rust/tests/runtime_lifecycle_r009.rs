//! R009 foreground startup, signal, graceful-drain, and forced-close tests.

use std::sync::Arc;

use eggpool::{
    Config,
    db::{Database, DatabaseConfig, MigrationRunner},
    runtime_lifecycle::{ProcessRuntime, RuntimeGenerationFactory, RuntimeManager},
    server::{ServerError, ServerRuntime, ShutdownPhase, ShutdownReason},
};
use tempfile::TempDir;
use tokio::{net::TcpListener, time::Duration};

async fn runtime_fixture(timeout: Duration) -> (TempDir, Database, ServerRuntime) {
    let directory = tempfile::tempdir().expect("temporary state directory");
    let path = directory.path().join("eggpool.db");
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
    let process = ProcessRuntime::new(database.clone());
    let candidate = RuntimeGenerationFactory::prepare(
        &process,
        Config::default(),
        "r009-initial".to_owned(),
        1,
    )
    .await
    .expect("initial generation prepares");
    let manager = Arc::new(RuntimeManager::new(
        candidate.transfer().expect("initial generation transfers"),
    ));
    process
        .install_initial_tasks((*manager).clone(), &Config::default())
        .await
        .expect("initial tasks install");
    let runtime =
        ServerRuntime::new(process, manager, Config::default()).with_shutdown_timeout(timeout);
    (directory, database, runtime)
}

#[tokio::test]
async fn bind_failure_happens_before_database_creation_or_mutation() {
    let directory = tempfile::tempdir().expect("temporary state directory");
    let path = directory.path().join("must-not-exist.db");
    let blocker = TcpListener::bind(("127.0.0.1", 0))
        .await
        .expect("bind blocker");
    let mut config = Config::default();
    config.server.host = "127.0.0.1".to_owned();
    config.server.port = blocker.local_addr().expect("blocker address").port();
    config.database.path = path.to_string_lossy().into_owned();

    let result = eggpool::server::run_with_digest(config, "r009-bind".to_owned(), None).await;
    assert!(matches!(result, Err(ServerError::Bind(_))));
    assert!(!path.exists(), "bind failure must precede DB creation");
    drop(blocker);
}

#[tokio::test]
async fn shutdown_request_is_idempotent_and_closes_database_last() {
    let (_directory, database, runtime) = runtime_fixture(Duration::from_millis(250)).await;
    let handle = runtime.handle();
    let listener = TcpListener::bind(("127.0.0.1", 0))
        .await
        .expect("server listener");
    let task = tokio::spawn(async move { runtime.serve_listener(listener).await });
    tokio::task::yield_now().await;

    assert!(handle.request_shutdown(ShutdownReason::Requested));
    assert!(!handle.request_shutdown(ShutdownReason::CtrlC));
    let report = task
        .await
        .expect("server task joins")
        .expect("clean shutdown");
    assert_eq!(report.phase, ShutdownPhase::Stopped);
    assert_eq!(report.reason, ShutdownReason::Requested);
    assert!(!report.forced);
    assert!(report.database_closed);
    assert_eq!(handle.phase(), ShutdownPhase::Stopped);
    assert!(database.close().await.is_ok(), "DB close is idempotent");
}

#[tokio::test]
async fn graceful_shutdown_waits_for_active_generation_lease() {
    let (_directory, database, runtime) = runtime_fixture(Duration::from_millis(250)).await;
    let lease = runtime.manager().acquire().await.expect("active lease");
    let handle = runtime.handle();
    let listener = TcpListener::bind(("127.0.0.1", 0))
        .await
        .expect("server listener");
    let task = tokio::spawn(async move { runtime.serve_listener(listener).await });
    tokio::task::yield_now().await;
    assert!(handle.request_shutdown(ShutdownReason::Sigterm));
    drop(lease);

    let report = task
        .await
        .expect("server task joins")
        .expect("clean shutdown");
    assert!(!report.forced);
    assert!(report.database_closed);
    database.close().await.expect("idempotent DB close");
}

#[tokio::test]
async fn stalled_lease_forces_bounded_shutdown_and_database_reopens() {
    let (directory, database, runtime) = runtime_fixture(Duration::from_millis(25)).await;
    let lease = runtime.manager().acquire().await.expect("stalled lease");
    let handle = runtime.handle();
    let listener = TcpListener::bind(("127.0.0.1", 0))
        .await
        .expect("server listener");
    let task = tokio::spawn(async move { runtime.serve_listener(listener).await });
    tokio::task::yield_now().await;
    assert!(handle.request_shutdown(ShutdownReason::Requested));

    let result = task.await.expect("server task joins");
    let report = match result {
        Err(ServerError::ForcedShutdown(report)) => report,
        other => panic!("expected forced shutdown, got {other:?}"),
    };
    assert!(report.forced);
    assert!(report.active_leases_at_deadline > 0);
    assert!(report.database_closed);
    drop(lease);

    let path = directory.path().join("eggpool.db");
    let reopened = Database::open(DatabaseConfig {
        path: path.to_string_lossy().into_owned(),
        ..DatabaseConfig::default()
    })
    .await
    .expect("same DB reopens after forced shutdown");
    MigrationRunner::new(&reopened)
        .run()
        .await
        .expect("reopened DB remains readable");
    reopened.close().await.expect("reopened DB closes");
    database.close().await.expect("original DB handle closes");
}

#[tokio::test]
async fn initial_tasks_are_installed_once_before_acceptance() {
    let (_directory, database, runtime) = runtime_fixture(Duration::from_millis(250)).await;
    let handle = runtime.handle();
    let listener = TcpListener::bind(("127.0.0.1", 0))
        .await
        .expect("server listener");
    let task = tokio::spawn(async move { runtime.serve_listener(listener).await });
    tokio::task::yield_now().await;
    handle.request_shutdown(ShutdownReason::Requested);
    let report = task.await.expect("server task joins").expect("shutdown");
    assert_eq!(report.task_count_at_start, 3);
    assert_eq!(report.task_count_joined, 3);
    database.close().await.expect("idempotent DB close");
}
