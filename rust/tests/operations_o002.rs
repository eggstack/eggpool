//! O002 local control, runtime path, and process-state qualification.

use std::{
    fs,
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
    time::Duration,
};

use eggpool::operations::{
    control::{
        ControlClient, ControlRequest, ControlResponse, ControlServerHandle, MAX_REQUEST_BYTES,
        ProtocolError, start,
    },
    paths::{PathEnvironment, RuntimePaths},
    process::{
        HealthProbe, ProcessIdentityProof, ProcessState, classify, clear_stale_pid, process_exists,
        read_pid, signal_term, write_pid_atomic,
    },
};
use eggpool::{
    Config,
    db::{Database, DatabaseConfig, MigrationRunner},
    runtime_lifecycle::{ProcessRuntime, RuntimeGenerationFactory, RuntimeManager},
    server::{ServerRuntime, ShutdownReason},
};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::UnixStream,
    time::sleep,
};

#[cfg(unix)]
use std::os::unix::fs::PermissionsExt;

fn private_directory(path: &std::path::Path) {
    fs::create_dir_all(path).expect("private directory creates");
    #[cfg(unix)]
    fs::set_permissions(path, fs::Permissions::from_mode(0o700)).expect("private mode");
}

#[test]
fn path_resolution_is_precedence_ordered_and_read_only() {
    let root = tempfile::tempdir().expect("temp root");
    let cwd = root.path().join("cwd");
    let xdg_config = root.path().join("config");
    let xdg_state = root.path().join("state");
    fs::create_dir_all(&cwd).expect("cwd");
    fs::create_dir_all(&xdg_config).expect("config");
    private_directory(&xdg_state.join("eggpool"));
    let config = xdg_config.join("eggpool/config.toml");
    fs::create_dir_all(config.parent().expect("config parent")).expect("config parent");
    fs::write(&config, "").expect("config fixture");

    let environment = PathEnvironment {
        home: Some(root.path().join("home")),
        cwd: Some(cwd.clone()),
        xdg_config_home: Some(xdg_config),
        xdg_state_home: Some(xdg_state.clone()),
        uid: 42,
        ..PathEnvironment::default()
    };
    let paths = RuntimePaths::resolve_with(&environment);
    assert_eq!(paths.config_path, config);
    assert_eq!(paths.state_dir, xdg_state.join("eggpool"));
    assert_eq!(paths.runtime_dir, paths.state_dir.join("runtime"));
    assert!(
        !paths.runtime_dir.exists(),
        "read-only resolution creates nothing"
    );
}

#[test]
fn production_config_resolves_shared_service_paths() {
    let environment = PathEnvironment {
        home: Some("/root".into()),
        eggpool_config: Some("/etc/eggpool/config.toml".into()),
        uid: 0,
        ..PathEnvironment::default()
    };
    let paths = RuntimePaths::resolve_with(&environment);
    assert_eq!(paths.config_dir, std::path::PathBuf::from("/etc/eggpool"));
    assert_eq!(paths.data_dir, std::path::PathBuf::from("/var/lib/eggpool"));
    assert_eq!(
        paths.state_dir,
        std::path::PathBuf::from("/var/lib/eggpool/.local/state/eggpool")
    );
    assert_eq!(
        paths.runtime_dir,
        std::path::PathBuf::from("/var/lib/eggpool/runtime")
    );
    assert_eq!(
        paths.pid_file,
        std::path::PathBuf::from("/var/lib/eggpool/.local/state/eggpool/eggpool.pid")
    );
    assert_eq!(
        paths.log_file,
        std::path::PathBuf::from("/var/log/eggpool/eggpool.log")
    );
}

#[tokio::test]
async fn pid_helpers_are_atomic_and_conservative_about_identity() {
    let root = tempfile::tempdir().expect("temp root");
    private_directory(root.path());
    let pid_path = root.path().join("eggpool.pid");
    write_pid_atomic(&pid_path, 999_999).expect("PID writes");
    assert_eq!(read_pid(&pid_path).expect("PID reads"), Some(999_999));
    assert!(!process_exists(999_999));
    assert!(clear_stale_pid(&pid_path, Some(999_999)).expect("stale PID clears"));
    assert!(!pid_path.exists());

    assert_eq!(
        classify(None, false, HealthProbe::Healthy, false),
        ProcessState::PortOccupiedHealthyUnknownOwner
    );
    assert_eq!(
        classify(Some(999_999), false, HealthProbe::Unreachable, false),
        ProcessState::StalePid
    );
    assert_eq!(
        classify(
            Some(std::process::id() as i32),
            true,
            HealthProbe::Unreachable,
            false
        ),
        ProcessState::Error
    );
    assert!(
        signal_term(
            std::process::id() as i32,
            ProcessIdentityProof {
                pid_file_matches: false,
                health: HealthProbe::Healthy,
                control_socket_reachable: true
            },
        )
        .is_err()
    );
    assert!(!pid_path.exists());
}

#[tokio::test]
async fn control_socket_is_private_bounded_single_shot_and_recoverable() {
    let root = tempfile::tempdir().expect("temp root");
    private_directory(root.path());
    let path = root.path().join("eggpool.sock");
    let handled = Arc::new(AtomicUsize::new(0));
    let counter = Arc::clone(&handled);
    let server = start(&path, move |request| {
        counter.fetch_add(1, Ordering::SeqCst);
        async move { ControlResponse::error(request.request_id, "test", "ok") }
    })
    .await
    .expect("control listener starts");

    #[cfg(unix)]
    {
        assert_eq!(
            fs::metadata(&path)
                .expect("socket metadata")
                .permissions()
                .mode()
                & 0o777,
            0o600
        );
        assert_eq!(
            fs::metadata(root.path())
                .expect("runtime metadata")
                .permissions()
                .mode()
                & 0o777,
            0o700
        );
    }

    let client = ControlClient::new(&path).with_timeout(Duration::from_secs(1));
    let response = client
        .reload(Some("a".repeat(64)))
        .await
        .expect("valid response");
    assert!(response.request_id.starts_with("rust-"));
    assert_eq!(handled.load(Ordering::SeqCst), 1);

    let mut malformed = UnixStream::connect(&path)
        .await
        .expect("malformed client connects");
    malformed
        .write_all(b"[]\n")
        .await
        .expect("malformed writes");
    let mut bytes = Vec::new();
    tokio::time::timeout(Duration::from_secs(1), malformed.read_to_end(&mut bytes))
        .await
        .expect("malformed response timeout")
        .expect("malformed response reads");
    assert!(String::from_utf8_lossy(&bytes).contains("request must be a JSON object"));
    assert_eq!(
        handled.load(Ordering::SeqCst),
        1,
        "bad clients never invoke handler"
    );

    let mut multi = UnixStream::connect(&path)
        .await
        .expect("multi client connects");
    let frame = serde_json::to_vec(&ControlRequest::reload("multi", Some("b".repeat(64))))
        .expect("frame serializes");
    multi
        .write_all(&[frame.clone(), frame].concat())
        .await
        .expect("multi writes");
    let mut ignored = Vec::new();
    let _ = tokio::time::timeout(Duration::from_secs(1), multi.read_to_end(&mut ignored)).await;
    assert_eq!(
        handled.load(Ordering::SeqCst),
        1,
        "multi-frame clients never invoke handler"
    );

    server.close().await.expect("listener closes");
    assert!(!path.exists());

    // An unowned regular file is a collision, while a real stale socket is
    // removed only after its failed connect probe.
    fs::write(&path, b"collision").expect("collision file");
    let error = match start(&path, |_request| async {
        ControlResponse::error("", "test", "")
    })
    .await
    {
        Ok(_) => panic!("regular collision must be refused"),
        Err(error) => error,
    };
    assert!(matches!(
        error,
        eggpool::operations::control::ControlError::SocketCollision
    ));
    fs::remove_file(&path).expect("collision removed");

    let stale_listener = tokio::net::UnixListener::bind(&path).expect("stale socket binds");
    drop(stale_listener);
    let recovered = start(&path, |_request| async {
        ControlResponse::error("", "test", "recovered")
    })
    .await
    .expect("stale socket recovers");
    recovered.close().await.expect("recovered listener closes");
    assert!(!path.exists());

    let live = start(&path, |_request| async {
        ControlResponse::error("", "test", "live")
    })
    .await
    .expect("live listener starts");
    let error = match start(&path, |_request| async {
        ControlResponse::error("", "test", "collision")
    })
    .await
    {
        Ok(_) => panic!("live socket must not be replaced"),
        Err(error) => error,
    };
    assert!(matches!(
        error,
        eggpool::operations::control::ControlError::SocketInUse
    ));
    live.close().await.expect("live listener closes");
}

#[tokio::test]
async fn control_disconnect_does_not_cancel_retained_handler() {
    let root = tempfile::tempdir().expect("temp root");
    private_directory(root.path());
    let path = root.path().join("eggpool.sock");
    let completed = Arc::new(AtomicUsize::new(0));
    let marker = Arc::clone(&completed);
    let server: ControlServerHandle = start(&path, move |request| {
        let marker = Arc::clone(&marker);
        async move {
            sleep(Duration::from_millis(20)).await;
            marker.fetch_add(1, Ordering::SeqCst);
            ControlResponse::error(request.request_id, "test", "done")
        }
    })
    .await
    .expect("control listener starts");
    let mut stream = UnixStream::connect(&path).await.expect("client connects");
    let frame =
        serde_json::to_vec(&ControlRequest::reload("disconnect", None)).expect("serializes");
    stream
        .write_all(&[frame, vec![b'\n']].concat())
        .await
        .expect("request writes");
    drop(stream);
    sleep(Duration::from_millis(80)).await;
    assert_eq!(completed.load(Ordering::SeqCst), 1);
    server.close().await.expect("listener closes");
}

#[test]
fn protocol_rejects_malformed_bounded_frames_without_leaking_input() {
    assert_eq!(ControlRequest::parse_frame(b""), Err(ProtocolError::Empty));
    assert_eq!(
        ControlRequest::parse_frame(br#"{"protocol_version":1}"#),
        Err(ProtocolError::MissingNewline)
    );
    assert_eq!(
        ControlRequest::parse_frame(
            br#"{"protocol_version":1,"request_id":"x","command":"reload_config"}
extra
"#
        ),
        Err(ProtocolError::MultipleFrames)
    );
    assert_eq!(
        ControlRequest::parse_frame(
            br#"{"protocol_version":1,"request_id":"x","command":"reload_config","validated_digest":"SECRET"}
"#,
        ),
        Err(ProtocolError::InvalidDigest)
    );
    assert_eq!(
        ControlRequest::parse_frame(
            br#"{"protocol_version":1,"request_id":"bad id","command":"reload_config"}
"#,
        ),
        Err(ProtocolError::InvalidRequestId)
    );
    assert_eq!(
        ControlRequest::parse_frame(
            br#"{"protocol_version":1,"request_id":"x","command":"nope"}
"#
        ),
        Err(ProtocolError::UnknownCommand)
    );
    assert_eq!(
        ControlRequest::parse_frame(
            br#"{"protocol_version":1,"request_id":"x","command":"reload_config","params":[]}
"#
        ),
        Err(ProtocolError::InvalidParams)
    );

    let oversized = vec![b'x'; MAX_REQUEST_BYTES + 1];
    assert_eq!(
        ControlRequest::parse_frame(&oversized),
        Err(ProtocolError::Oversized)
    );
    let wrong = br#"{"protocol_version":2,"request_id":"x","command":"reload_config"}
"#;
    assert_eq!(
        ControlRequest::parse_frame(wrong),
        Err(ProtocolError::WrongVersion)
    );
}

#[tokio::test]
async fn client_timeout_is_bounded_and_handler_survives_disconnect() {
    let root = tempfile::tempdir().expect("temp root");
    private_directory(root.path());
    let path = root.path().join("eggpool.sock");
    let completed = Arc::new(AtomicUsize::new(0));
    let marker = Arc::clone(&completed);
    let server = start(&path, move |request| {
        let marker = Arc::clone(&marker);
        async move {
            sleep(Duration::from_millis(30)).await;
            marker.fetch_add(1, Ordering::SeqCst);
            ControlResponse::error(request.request_id, "test", "done")
        }
    })
    .await
    .expect("control listener starts");

    let result = ControlClient::new(&path)
        .with_timeout(Duration::from_millis(5))
        .reload(None)
        .await;
    assert!(matches!(
        result,
        Err(eggpool::operations::control::ControlClientError::Timeout)
    ));
    sleep(Duration::from_millis(60)).await;
    assert_eq!(completed.load(Ordering::SeqCst), 1);
    server.close().await.expect("listener closes");
}

#[tokio::test]
async fn server_runtime_owns_control_shutdown_after_m8_readiness() {
    let root = tempfile::tempdir().expect("temp root");
    private_directory(root.path());
    let database = Database::open(DatabaseConfig {
        path: root.path().join("usage.sqlite3").display().to_string(),
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
        "o002-runtime".to_owned(),
        1,
    )
    .await
    .expect("generation prepares");
    let manager = Arc::new(RuntimeManager::new(
        candidate.transfer().expect("generation transfers"),
    ));
    process
        .install_initial_tasks((*manager).clone(), &Config::default())
        .await
        .expect("tasks install");
    let control_path = root.path().join("runtime/eggpool.sock");
    let control = start(&control_path, |request| async move {
        ControlResponse::error(request.request_id, "test", "runtime")
    })
    .await
    .expect("control starts");
    let runtime =
        ServerRuntime::new(process, manager, Config::default()).with_control_server(control);
    let handle = runtime.handle();
    let listener = tokio::net::TcpListener::bind(("127.0.0.1", 0))
        .await
        .expect("HTTP binds");
    let task = tokio::spawn(async move { runtime.serve_listener(listener).await });
    tokio::task::yield_now().await;
    assert!(handle.request_shutdown(ShutdownReason::Requested));
    task.await
        .expect("server joins")
        .expect("server shuts down");
    assert!(!control_path.exists(), "shutdown owns socket unlink");
    database
        .close()
        .await
        .expect("database close is idempotent");
}
