//! Authentication boundary guards for the downstream HTTP server.
//!
//! The authorization scheme token is case-insensitive by specification, so a
//! client may send `Bearer`, `bearer`, or `BEARER`. The credential itself is
//! still compared in constant time, the accepted header set is unchanged, and
//! a loopback peer does not bypass the credential.

use std::{sync::Arc, time::Duration};

use eggpool::{
    Config,
    db::{Database, DatabaseConfig, MigrationRunner},
    runtime_lifecycle::{ProcessRuntime, RuntimeGenerationFactory, RuntimeManager},
    server::{ServerRuntime, ShutdownReason},
};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::{TcpListener, TcpStream},
};

const SERVER_KEY: &str = "test-key-transport";

async fn runtime_fixture() -> (tempfile::TempDir, Database, ServerRuntime) {
    let directory = tempfile::tempdir().expect("temporary state directory");
    let database = Database::open(DatabaseConfig {
        path: directory
            .path()
            .join("eggpool.db")
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
    let process = ProcessRuntime::new(database.clone()).expect("process runtime builds");
    let mut config = Config::default();
    config.server.api_key = Some(SERVER_KEY.to_owned());
    let candidate =
        RuntimeGenerationFactory::prepare(&process, config.clone(), "server-auth".to_owned(), 1)
            .await
            .expect("initial generation prepares");
    let manager = Arc::new(RuntimeManager::new(
        candidate.transfer().expect("generation transfers"),
    ));
    process
        .install_initial_tasks((*manager).clone(), &config)
        .await
        .expect("initial tasks install");
    let runtime = ServerRuntime::new(process, Arc::clone(&manager), config);
    (directory, database, runtime)
}

async fn request(address: std::net::SocketAddr, raw: &[u8]) -> String {
    let mut stream = TcpStream::connect(address)
        .await
        .expect("connect to listener");
    stream.write_all(raw).await.expect("write HTTP request");
    let mut response = Vec::new();
    tokio::time::timeout(Duration::from_secs(2), stream.read_to_end(&mut response))
        .await
        .expect("response completes")
        .expect("read response");
    String::from_utf8_lossy(&response).into_owned()
}

async fn status_request(address: std::net::SocketAddr, authorization: Option<&str>) -> String {
    let authorization = match authorization {
        Some(value) => format!("Authorization: {value}\r\n"),
        None => String::new(),
    };
    let raw = format!(
        "GET /api/status HTTP/1.1\r\nHost: localhost\r\n{authorization}Connection: close\r\n\r\n"
    );
    request(address, raw.as_bytes()).await
}

#[tokio::test]
async fn authorization_scheme_token_is_case_insensitive() {
    let (_directory, database, runtime) = runtime_fixture().await;
    let handle = runtime.handle();
    let listener = TcpListener::bind(("127.0.0.1", 0))
        .await
        .expect("bind listener");
    let address = listener.local_addr().expect("listener address");
    let task = tokio::spawn(async move { runtime.serve_listener(listener).await });

    // The scheme token names the scheme in any letter case.
    for scheme in ["Bearer", "bearer", "BEARER", "BeArEr"] {
        let response = status_request(address, Some(&format!("{scheme} {SERVER_KEY}"))).await;
        assert!(
            response.starts_with("HTTP/1.1 200"),
            "{scheme} must be accepted: {response}"
        );
    }

    // An incorrect credential is still rejected under every scheme spelling,
    // whether it is the same length, shorter, or longer than the expected
    // key.
    for credential in [
        "wrong-key-transport",
        "test-key-transpor",
        "test-key-transport-extra",
    ] {
        for scheme in ["Bearer", "BEARER"] {
            let response = status_request(address, Some(&format!("{scheme} {credential}"))).await;
            assert!(
                response.starts_with("HTTP/1.1 401"),
                "{scheme} with {credential} must be rejected: {response}"
            );
        }
    }

    // A scheme other than `bearer` is still not an accepted credential
    // source, and the accepted header set is unchanged.
    let basic = status_request(address, Some(&format!("Basic {SERVER_KEY}"))).await;
    assert!(
        basic.starts_with("HTTP/1.1 401"),
        "a non-bearer scheme must be rejected: {basic}"
    );
    let header_key = request(
        address,
        format!(
            "GET /api/status HTTP/1.1\r\nHost: localhost\r\nX-API-Key: {SERVER_KEY}\r\nConnection: close\r\n\r\n"
        )
        .as_bytes(),
    )
    .await;
    assert!(
        header_key.starts_with("HTTP/1.1 200"),
        "the x-api-key header must still be accepted: {header_key}"
    );

    // A missing credential is still rejected, and a loopback peer does not
    // bypass the credential.
    let anonymous = status_request(address, None).await;
    assert!(
        anonymous.starts_with("HTTP/1.1 401"),
        "a loopback peer must not bypass the credential: {anonymous}"
    );

    assert!(handle.request_shutdown(ShutdownReason::Requested));
    let report = tokio::time::timeout(Duration::from_secs(6), task)
        .await
        .expect("server shutdown is bounded")
        .expect("server task joins")
        .expect("server shuts down cleanly");
    assert!(report.database_closed);
    database
        .close()
        .await
        .expect("database close is idempotent");
}

#[tokio::test]
async fn loopback_public_endpoints_stay_exempt_from_the_credential() {
    let (_directory, database, runtime) = runtime_fixture().await;
    let handle = runtime.handle();
    let listener = TcpListener::bind(("127.0.0.1", 0))
        .await
        .expect("bind listener");
    let address = listener.local_addr().expect("listener address");
    let task = tokio::spawn(async move { runtime.serve_listener(listener).await });

    let health = request(
        address,
        b"GET /v1/healthz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
    )
    .await;
    assert!(
        health.starts_with("HTTP/1.1 200"),
        "the public health endpoint must stay exempt: {health}"
    );

    assert!(handle.request_shutdown(ShutdownReason::Requested));
    let _ = tokio::time::timeout(Duration::from_secs(6), task)
        .await
        .expect("server shutdown is bounded");
    database
        .close()
        .await
        .expect("database close is idempotent");
}
