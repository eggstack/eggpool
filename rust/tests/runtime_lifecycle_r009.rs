//! R009 foreground startup, signal, graceful-drain, and forced-close tests.

use std::sync::Arc;

use eggpool::{
    Config,
    db::{Database, DatabaseConfig, MigrationRunner},
    runtime_lifecycle::{ProcessRuntime, RuntimeGenerationFactory, RuntimeManager},
    server::{ServerError, ServerRuntime, ShutdownPhase, ShutdownReason},
};
use tempfile::TempDir;
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::{TcpListener, TcpStream},
    time::Duration,
};

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
    let process = ProcessRuntime::new(database.clone()).expect("process runtime builds");
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

/// First-run robustness: `run_with_digest` creates a missing database parent
/// directory instead of failing SQLite open. The database path points into a
/// nested directory that does not exist yet; readiness on `/v1/healthz` is
/// the observable proof the server got past database open.
#[tokio::test]
async fn first_run_creates_missing_database_parent_directory() {
    let directory = tempfile::tempdir().expect("temporary state directory");
    let path = directory.path().join("nested").join("first-run.db");
    assert!(
        !path.parent().expect("database parent").exists(),
        "fixture must start with a missing database parent directory"
    );
    let mut config = Config::default();
    config.server.host = "127.0.0.1".to_owned();
    config.server.api_key = Some("test-key-first-run".to_owned());
    config.models.startup_refresh = false;
    config.database.path = path.to_string_lossy().into_owned();
    // Probe a free loopback port, then release it for the server under test.
    // A small retry loop keeps the probe-then-bind sequence robust when the
    // port is stolen between probe and bind.
    for _ in 0..3 {
        let probe = TcpListener::bind(("127.0.0.1", 0))
            .await
            .expect("free port probe");
        config.server.port = probe.local_addr().expect("probe address").port();
        drop(probe);
        let task = tokio::spawn(eggpool::server::run_with_digest(
            config.clone(),
            "r009-first-run".to_owned(),
            None,
        ));
        if wait_for_healthz(config.server.port, &task).await {
            assert!(
                path.is_file(),
                "server creates the database file on first run"
            );
            task.abort();
            let _ = task.await;
            return;
        }
        match task.await.expect("server task joins") {
            Err(ServerError::Bind(_)) => continue,
            other => panic!("server failed before serving: {other:?}"),
        }
    }
    panic!("server never reached ready on a free loopback port");
}

/// Poll `/v1/healthz` until it reports ok or the server task finishes.
/// Returns true only on observed readiness under the bounded timeout.
async fn wait_for_healthz(
    port: u16,
    task: &tokio::task::JoinHandle<Result<(), ServerError>>,
) -> bool {
    let deadline = tokio::time::Instant::now() + Duration::from_secs(15);
    while tokio::time::Instant::now() < deadline {
        if task.is_finished() {
            return false;
        }
        if healthz_is_ok(port).await {
            return true;
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    false
}

async fn healthz_is_ok(port: u16) -> bool {
    let address = format!("127.0.0.1:{port}");
    let connect =
        tokio::time::timeout(Duration::from_millis(200), TcpStream::connect(&address)).await;
    let Ok(Ok(mut stream)) = connect else {
        return false;
    };
    let request = b"GET /v1/healthz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n";
    if stream.write_all(request).await.is_err() {
        return false;
    }
    let mut body = Vec::new();
    if tokio::time::timeout(Duration::from_secs(2), stream.read_to_end(&mut body))
        .await
        .is_err()
    {
        return false;
    }
    let text = String::from_utf8_lossy(&body);
    text.contains("200") && text.contains("\"status\":\"ok\"")
}
