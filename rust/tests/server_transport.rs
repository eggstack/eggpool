//! Real-socket qualification for EggPool's downstream HTTP transport.

use std::{sync::Arc, time::Duration};

use eggpool::{
    Config,
    config::{
        AccountConfig, ModelWirePreference, ProviderAuthConfig, ProviderConfig,
        ProviderStaticModelConfig, ProviderWireSurfaceConfig,
    },
    db::{
        AccountConfig as DbAccountConfig, AccountRepository, Database, DatabaseConfig,
        MigrationRunner,
    },
    runtime_lifecycle::{ProcessRuntime, RuntimeGenerationFactory, RuntimeManager},
    server::{ServerRuntime, ShutdownReason},
};
use tokio::sync::oneshot;
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::{TcpListener, TcpSocket, TcpStream},
};

async fn runtime_fixture() -> (tempfile::TempDir, Database, ServerRuntime) {
    runtime_fixture_with_body_limit(10 * 1024 * 1024).await
}

async fn runtime_fixture_with_body_limit(
    max_request_body_bytes: u64,
) -> (tempfile::TempDir, Database, ServerRuntime) {
    let (directory, database, runtime, _, _, _) =
        runtime_fixture_with_generation_components(max_request_body_bytes).await;
    (directory, database, runtime)
}

async fn runtime_fixture_with_generation_components(
    max_request_body_bytes: u64,
) -> (
    tempfile::TempDir,
    Database,
    ServerRuntime,
    ProcessRuntime,
    Arc<RuntimeManager>,
    Config,
) {
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
    let process = ProcessRuntime::new(database.clone());
    let mut config = Config::default();
    config.server.api_key = Some("test-key-transport".to_owned());
    config.server.max_request_body_bytes = max_request_body_bytes;
    let candidate = RuntimeGenerationFactory::prepare(
        &process,
        config.clone(),
        "server-transport".to_owned(),
        1,
    )
    .await
    .expect("initial generation prepares");
    let manager = Arc::new(RuntimeManager::new(
        candidate.transfer().expect("generation transfers"),
    ));
    process
        .install_initial_tasks((*manager).clone(), &config)
        .await
        .expect("initial tasks install");
    let runtime = ServerRuntime::new(process.clone(), Arc::clone(&manager), config.clone());
    (directory, database, runtime, process, manager, config)
}

async fn publish_body_limit(
    process: &ProcessRuntime,
    manager: &Arc<RuntimeManager>,
    mut config: Config,
    max_request_body_bytes: u64,
    generation_id: u64,
) -> Config {
    config.server.max_request_body_bytes = max_request_body_bytes;
    let expected_generation = manager.active_generation().generation_id();
    let candidate = RuntimeGenerationFactory::prepare(
        process,
        config.clone(),
        format!("body-limit-{generation_id}"),
        generation_id,
    )
    .await
    .expect("body-limit generation prepares");
    let mut staged = manager
        .stage(expected_generation, &candidate)
        .expect("body-limit generation stages");
    staged
        .commit_pointer()
        .expect("body-limit generation pointer commits");
    staged.accept().expect("body-limit generation is accepted");
    config
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

async fn request_with_write_eof(address: std::net::SocketAddr, raw: &[u8]) -> String {
    let mut stream = TcpStream::connect(address)
        .await
        .expect("connect to listener");
    stream
        .write_all(raw)
        .await
        .expect("write malformed request");
    stream
        .shutdown()
        .await
        .expect("signal incomplete request body");
    let mut response = Vec::new();
    tokio::time::timeout(Duration::from_secs(2), stream.read_to_end(&mut response))
        .await
        .expect("malformed request closes promptly")
        .expect("read malformed response");
    String::from_utf8_lossy(&response).into_owned()
}

async fn streaming_fixture_provider(
    listener: TcpListener,
    stopped: oneshot::Sender<()>,
) -> std::io::Result<()> {
    loop {
        let (mut stream, _) = listener.accept().await?;
        let mut request = Vec::new();
        let mut header_end = None;
        while header_end.is_none() {
            let mut buffer = [0_u8; 4096];
            let read = stream.read(&mut buffer).await?;
            if read == 0 {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::UnexpectedEof,
                    "provider request ended before headers",
                ));
            }
            request.extend_from_slice(&buffer[..read]);
            header_end = request.windows(4).position(|window| window == b"\r\n\r\n");
        }
        let header_end = header_end.expect("header terminator was observed") + 4;
        let headers = String::from_utf8_lossy(&request[..header_end]);
        let content_length = headers
            .lines()
            .find_map(|line| {
                let (name, value) = line.split_once(':')?;
                name.eq_ignore_ascii_case("content-length")
                    .then(|| value.trim().parse::<usize>().ok())
                    .flatten()
            })
            .unwrap_or(0);
        while request.len() < header_end + content_length {
            let mut buffer = [0_u8; 4096];
            let read = stream.read(&mut buffer).await?;
            if read == 0 {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::UnexpectedEof,
                    "provider request body ended early",
                ));
            }
            request.extend_from_slice(&buffer[..read]);
        }
        if String::from_utf8_lossy(&request).contains("\"stream\":false") {
            let payload = br#"{"id":"response-transport","model":"stream-fixture","status":"completed","output":[{"type":"message","content":[{"type":"output_text","text":"finite transport answer"}]}],"usage":{"input_tokens":1,"output_tokens":3,"total_tokens":4}}"#;
            let head = format!(
                "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n",
                payload.len()
            );
            stream.write_all(head.as_bytes()).await?;
            stream.write_all(payload).await?;
            continue;
        }
        stream
            .write_all(
                b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\ntransfer-encoding: chunked\r\nconnection: close\r\n\r\n",
            )
            .await?;
        let payload = format!(
            "event: response.output_text.delta\ndata: {{\"type\":\"response.output_text.delta\",\"delta\":\"{}\"}}\n\n",
            "x".repeat(16 * 1024)
        );
        let chunk_header = format!("{:X}\r\n", payload.len());
        loop {
            if stream.write_all(chunk_header.as_bytes()).await.is_err()
                || stream.write_all(payload.as_bytes()).await.is_err()
                || stream.write_all(b"\r\n").await.is_err()
            {
                let _ = stopped.send(());
                return Ok(());
            }
        }
    }
}

async fn concurrent_streaming_fixture_provider(
    listener: TcpListener,
    stopped: oneshot::Sender<()>,
) -> std::io::Result<()> {
    let mut workers = Vec::new();
    for _ in 0..2 {
        let (mut stream, _) = listener.accept().await?;
        workers.push(tokio::spawn(async move {
            let mut request = Vec::new();
            let mut header_end = None;
            while header_end.is_none() {
                let mut buffer = [0_u8; 4096];
                let read = stream.read(&mut buffer).await?;
                if read == 0 {
                    return Err(std::io::Error::new(
                        std::io::ErrorKind::UnexpectedEof,
                        "provider request ended before headers",
                    ));
                }
                request.extend_from_slice(&buffer[..read]);
                header_end = request.windows(4).position(|window| window == b"\r\n\r\n");
            }
            let header_end = header_end.expect("header terminator was observed") + 4;
            let headers = String::from_utf8_lossy(&request[..header_end]);
            let content_length = headers
                .lines()
                .find_map(|line| {
                    let (name, value) = line.split_once(':')?;
                    name.eq_ignore_ascii_case("content-length")
                        .then(|| value.trim().parse::<usize>().ok())
                        .flatten()
                })
                .unwrap_or(0);
            while request.len() < header_end + content_length {
                let mut buffer = [0_u8; 4096];
                let read = stream.read(&mut buffer).await?;
                if read == 0 {
                    return Err(std::io::Error::new(
                        std::io::ErrorKind::UnexpectedEof,
                        "provider request body ended early",
                    ));
                }
                request.extend_from_slice(&buffer[..read]);
            }
            stream
                .write_all(
                    b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\ntransfer-encoding: chunked\r\nconnection: close\r\n\r\n",
                )
                .await?;
            let payload = b"event: response.output_text.delta\ndata: {\"type\":\"response.output_text.delta\",\"delta\":\"x\"}\n\n";
            stream
                .write_all(format!("{:X}\r\n", payload.len()).as_bytes())
                .await?;
            stream.write_all(payload).await?;
            stream.write_all(b"\r\n").await?;
            stream.write_all(b"0\r\n\r\n").await?;
            stream.flush().await?;
            Ok::<(), std::io::Error>(())
        }));
    }
    for worker in workers {
        worker
            .await
            .map_err(|error| std::io::Error::other(error.to_string()))??;
    }
    let _ = stopped.send(());
    Ok(())
}

async fn start_unknown_length_stream_request(address: std::net::SocketAddr) -> TcpStream {
    let mut client = TcpStream::connect(address)
        .await
        .expect("connect stream client");
    let body = br#"{"model":"stream-fixture","input":"ping","store":false,"stream":true}"#;
    client
        .write_all(
            b"POST /v1/responses HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer test-key-transport\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n",
        )
        .await
        .expect("write request headers");
    client
        .write_all(format!("{:X}\r\n", body.len()).as_bytes())
        .await
        .expect("write chunk header");
    client.write_all(body).await.expect("write request body");
    client
        .write_all(b"\r\n0\r\n\r\n")
        .await
        .expect("finish chunked request body");
    client
}

async fn read_stream_start(client: &mut TcpStream) -> std::io::Result<(String, Vec<u8>)> {
    let mut response_head = Vec::new();
    while !response_head.ends_with(b"\r\n\r\n") {
        let mut byte = [0_u8; 1];
        client.read_exact(&mut byte).await?;
        response_head.push(byte[0]);
    }
    let mut first_event = Vec::new();
    let marker = b"event: response.output_text.delta";
    while !first_event
        .windows(marker.len())
        .any(|window| window == marker)
    {
        let mut byte = [0_u8; 1];
        client.read_exact(&mut byte).await?;
        first_event.push(byte[0]);
    }
    Ok((
        String::from_utf8_lossy(&response_head).into_owned(),
        first_event,
    ))
}

#[tokio::test]
async fn production_listener_serves_health_and_preserves_auth_boundary() {
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
    assert!(health.starts_with("HTTP/1.1 200"), "{health}");

    let http10 = request(address, b"GET /v1/healthz HTTP/1.0\r\n\r\n").await;
    assert!(http10.starts_with("HTTP/1.0 200"), "{http10}");

    let inference = request(
        address,
        b"GET /api/status HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
    )
    .await;
    assert!(inference.starts_with("HTTP/1.1 401"), "{inference}");

    let authenticated = request(
        address,
        b"GET /api/status HTTP/1.1\r\nHost: localhost\r\nX-API-Key: test-key-transport\r\nConnection: close\r\n\r\n",
    )
    .await;
    assert!(authenticated.starts_with("HTTP/1.1 200"), "{authenticated}");
    assert!(
        authenticated
            .to_ascii_lowercase()
            .contains("content-type: application/json"),
        "{authenticated}"
    );

    let static_asset = request(
        address,
        b"GET /static/dashboard.css HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
    )
    .await;
    assert!(static_asset.starts_with("HTTP/1.1 200"), "{static_asset}");
    assert!(
        static_asset
            .to_ascii_lowercase()
            .contains("content-type: text/css"),
        "{static_asset}"
    );

    assert!(handle.request_shutdown(ShutdownReason::Requested));
    let report = tokio::time::timeout(Duration::from_secs(6), task)
        .await
        .expect("transport and EggPool shutdown are bounded")
        .expect("server task joins")
        .expect("server shuts down cleanly");
    assert!(report.database_closed);
    database
        .close()
        .await
        .expect("database close is idempotent");
}

/// Read exactly one HTTP/1 response from a keep-alive stream, following the
/// response's own framing (Content-Length or chunked). EggServe 0.4 retains
/// known-length framing for exact-size bodies, so callers must not assume
/// chunked encoding.
async fn read_framed_response(stream: &mut TcpStream) -> std::io::Result<Vec<u8>> {
    let mut head = Vec::new();
    while !head.ends_with(b"\r\n\r\n") {
        let mut byte = [0_u8; 1];
        stream.read_exact(&mut byte).await?;
        head.push(byte[0]);
    }
    let head_text = String::from_utf8_lossy(&head).to_ascii_lowercase();
    let content_length = head_text.lines().find_map(|line| {
        let (name, value) = line.split_once(':')?;
        name.trim()
            .eq_ignore_ascii_case("content-length")
            .then(|| value.trim().parse::<usize>().ok())
            .flatten()
    });
    let mut response = head;
    if let Some(length) = content_length {
        let mut body = vec![0_u8; length];
        stream.read_exact(&mut body).await?;
        response.extend_from_slice(&body);
    } else {
        while !response.ends_with(b"\r\n0\r\n\r\n") {
            let mut byte = [0_u8; 1];
            stream.read_exact(&mut byte).await?;
            response.push(byte[0]);
        }
    }
    Ok(response)
}

#[tokio::test]
async fn production_listener_reuses_keep_alive_connection() {
    let (_directory, database, runtime) = runtime_fixture().await;
    let handle = runtime.handle();
    let listener = TcpListener::bind(("127.0.0.1", 0))
        .await
        .expect("bind listener");
    let address = listener.local_addr().expect("listener address");
    let task = tokio::spawn(async move { runtime.serve_listener(listener).await });

    let mut stream = TcpStream::connect(address)
        .await
        .expect("connect to listener");
    stream
        .write_all(b"GET /v1/healthz HTTP/1.1\r\nHost: localhost\r\n\r\n")
        .await
        .expect("write first keep-alive request");
    let first = tokio::time::timeout(Duration::from_secs(2), read_framed_response(&mut stream))
        .await
        .expect("first response completes")
        .expect("read first response");
    assert!(
        String::from_utf8_lossy(&first).starts_with("HTTP/1.1 200"),
        "{}",
        String::from_utf8_lossy(&first)
    );
    stream
        .write_all(b"GET /v1/healthz HTTP/1.1\r\nHost: localhost\r\n\r\n")
        .await
        .expect("write second keep-alive request");
    let mut second =
        tokio::time::timeout(Duration::from_secs(2), read_framed_response(&mut stream))
            .await
            .expect("second response completes")
            .expect("read second response");
    assert!(
        String::from_utf8_lossy(&second).starts_with("HTTP/1.1 200"),
        "{}",
        String::from_utf8_lossy(&second)
    );

    assert!(handle.request_shutdown(ShutdownReason::Requested));
    tokio::time::timeout(Duration::from_secs(6), stream.read_to_end(&mut second))
        .await
        .expect("idle keep-alive closes during child drain")
        .expect("read transport close");
    tokio::time::timeout(Duration::from_secs(6), task)
        .await
        .expect("transport and EggPool shutdown are bounded")
        .expect("server task joins")
        .expect("server shuts down cleanly");
    database
        .close()
        .await
        .expect("database close is idempotent");
}

#[tokio::test]
async fn parser_rejections_leave_the_listener_healthy() {
    let (_directory, database, runtime) = runtime_fixture().await;
    let handle = runtime.handle();
    let listener = TcpListener::bind(("127.0.0.1", 0))
        .await
        .expect("bind listener");
    let address = listener.local_addr().expect("listener address");
    let task = tokio::spawn(async move { runtime.serve_listener(listener).await });

    let target = format!(
        "GET /{} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
        "x".repeat(16 * 1024 + 1)
    );
    let response = request(address, target.as_bytes()).await;
    assert!(
        !response.starts_with("HTTP/1.1 200"),
        "oversized target accepted"
    );
    let health = request(
        address,
        b"GET /v1/healthz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
    )
    .await;
    assert!(health.starts_with("HTTP/1.1 200"), "{health}");

    let mut many_headers =
        String::from("GET /v1/healthz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n");
    for index in 0..260 {
        many_headers.push_str(&format!("X-Filler-{index}: x\r\n"));
    }
    many_headers.push_str("\r\n");
    let response = request(address, many_headers.as_bytes()).await;
    assert!(
        !response.starts_with("HTTP/1.1 200"),
        "excessive headers accepted"
    );
    let health = request(
        address,
        b"GET /v1/healthz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
    )
    .await;
    assert!(health.starts_with("HTTP/1.1 200"), "{health}");

    let large_header = format!(
        "GET /v1/healthz HTTP/1.1\r\nHost: localhost\r\nX-Large: {}\r\nConnection: close\r\n\r\n",
        "x".repeat(129 * 1024)
    );
    let response = request(address, large_header.as_bytes()).await;
    assert!(
        !response.starts_with("HTTP/1.1 200"),
        "oversized headers accepted"
    );
    let health = request(
        address,
        b"GET /v1/healthz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
    )
    .await;
    assert!(health.starts_with("HTTP/1.1 200"), "{health}");

    let malformed = request_with_write_eof(
        address,
        b"POST /v1/responses HTTP/1.1\r\nHost: localhost\r\nContent-Length: 10\r\nConnection: close\r\n\r\n{}",
    )
    .await;
    assert!(
        !malformed.starts_with("HTTP/1.1 200"),
        "incomplete body accepted"
    );
    let health = request(
        address,
        b"GET /v1/healthz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
    )
    .await;
    assert!(health.starts_with("HTTP/1.1 200"), "{health}");

    assert!(handle.request_shutdown(ShutdownReason::Requested));
    tokio::time::timeout(Duration::from_secs(6), task)
        .await
        .expect("transport shutdown is bounded")
        .expect("server task joins")
        .expect("server shuts down cleanly");
    database
        .close()
        .await
        .expect("database close is idempotent");
}

#[tokio::test]
async fn eggpool_live_body_limit_applies_to_content_length_and_chunked_bodies() {
    let (_directory, database, runtime) = runtime_fixture_with_body_limit(64).await;
    let handle = runtime.handle();
    let listener = TcpListener::bind(("127.0.0.1", 0))
        .await
        .expect("bind listener");
    let address = listener.local_addr().expect("listener address");
    let task = tokio::spawn(async move { runtime.serve_listener(listener).await });
    let body = vec![b'x'; 65];
    let content_length = format!(
        "POST /v1/responses HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer test-key-transport\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        body.len()
    );
    let response = request(address, content_length.as_bytes()).await;
    assert!(response.starts_with("HTTP/1.1 413"), "{response}");
    let health = request(
        address,
        b"GET /v1/healthz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
    )
    .await;
    assert!(health.starts_with("HTTP/1.1 200"), "{health}");

    let mut chunked = format!(
        "POST /v1/responses HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer test-key-transport\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n{:X}\r\n",
        body.len()
    )
    .into_bytes();
    chunked.extend_from_slice(&body);
    chunked.extend_from_slice(b"\r\n0\r\n\r\n");
    let response = request(address, &chunked).await;
    assert!(response.starts_with("HTTP/1.1 413"), "{response}");
    let health = request(
        address,
        b"GET /v1/healthz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
    )
    .await;
    assert!(health.starts_with("HTTP/1.1 200"), "{health}");

    let mut upload = TcpStream::connect(address)
        .await
        .expect("connect incomplete upload");
    upload
        .write_all(
            b"POST /v1/responses HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer test-key-transport\r\nContent-Length: 512\r\n\r\n{\"model\":",
        )
        .await
        .expect("write incomplete upload prefix");
    drop(upload);
    let health = request(
        address,
        b"GET /v1/healthz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
    )
    .await;
    assert!(health.starts_with("HTTP/1.1 200"), "{health}");

    assert!(handle.request_shutdown(ShutdownReason::Requested));
    let report = tokio::time::timeout(Duration::from_secs(6), task)
        .await
        .expect("transport shutdown is bounded")
        .expect("server task joins")
        .expect("server shuts down cleanly");
    assert_eq!(report.body_tasks_at_deadline, 0);
    database
        .close()
        .await
        .expect("database close is idempotent");
}

#[tokio::test]
async fn aggregate_unknown_body_admission_rejects_without_reading_and_recovers() {
    let (_directory, database, runtime) = runtime_fixture_with_body_limit(64 * 1024 * 1024).await;
    let handle = runtime.handle();
    let listener = TcpListener::bind(("127.0.0.1", 0))
        .await
        .expect("bind listener");
    let address = listener.local_addr().expect("listener address");
    let task = tokio::spawn(async move { runtime.serve_listener(listener).await });

    let mut admitted = TcpStream::connect(address)
        .await
        .expect("first upload connects");
    admitted
        .write_all(b"POST /v1/responses HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer test-key-transport\r\nTransfer-Encoding: chunked\r\n\r\n")
        .await
        .expect("send first upload headers");

    let rejected = request(
        address,
        b"POST /v1/responses HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer test-key-transport\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n",
    )
    .await;
    assert!(rejected.starts_with("HTTP/1.1 503"), "{rejected}");
    assert!(!rejected.contains("67108864"), "{rejected}");
    drop(admitted);

    let recovery = request(
        address,
        b"POST /v1/responses HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer test-key-transport\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}",
    )
    .await;
    assert!(!recovery.starts_with("HTTP/1.1 503"), "{recovery}");

    assert!(handle.request_shutdown(ShutdownReason::Requested));
    tokio::time::timeout(Duration::from_secs(6), task)
        .await
        .expect("transport shutdown is bounded")
        .expect("server task joins")
        .expect("server shuts down cleanly");
    database
        .close()
        .await
        .expect("database close is idempotent");
}

#[tokio::test]
async fn body_admission_uses_each_requests_leased_generation_limit() {
    let (_directory, database, runtime, process, manager, config) =
        runtime_fixture_with_generation_components(32).await;
    let handle = runtime.handle();
    let listener = TcpListener::bind(("127.0.0.1", 0))
        .await
        .expect("bind listener");
    let address = listener.local_addr().expect("listener address");
    let task = tokio::spawn(async move { runtime.serve_listener(listener).await });

    let config = publish_body_limit(&process, &manager, config, 64, 2).await;
    let increased = vec![b'x'; 40];
    let mut increased_request = format!(
        "POST /v1/responses HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer test-key-transport\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        increased.len()
    )
    .into_bytes();
    increased_request.extend_from_slice(&increased);
    let response = request(address, &increased_request).await;
    assert!(!response.starts_with("HTTP/1.1 413"), "{response}");

    let old_slot = manager.active_slot();
    let mut admitted = TcpStream::connect(address)
        .await
        .expect("old-generation upload connects");
    admitted
        .write_all(
            b"POST /v1/responses HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer test-key-transport\r\nContent-Length: 64\r\nConnection: close\r\n\r\n",
        )
        .await
        .expect("old-generation headers are sent");
    tokio::time::timeout(Duration::from_secs(2), async {
        while old_slot.active_lease_count() == 0 {
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("old-generation request acquired its lease before upload");

    let _config = publish_body_limit(&process, &manager, config, 32, 3).await;
    admitted
        .write_all(&[b'x'; 64])
        .await
        .expect("finish the old-generation upload");
    let mut response = Vec::new();
    tokio::time::timeout(Duration::from_secs(2), admitted.read_to_end(&mut response))
        .await
        .expect("old-generation request completes")
        .expect("old-generation response reads");
    let response = String::from_utf8_lossy(&response);
    assert!(!response.starts_with("HTTP/1.1 413"), "{response}");

    let response = request(
        address,
        b"POST /v1/responses HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer test-key-transport\r\nContent-Length: 33\r\nConnection: close\r\n\r\n",
    )
    .await;
    assert!(response.starts_with("HTTP/1.1 413"), "{response}");

    assert!(handle.request_shutdown(ShutdownReason::Requested));
    tokio::time::timeout(Duration::from_secs(6), task)
        .await
        .expect("transport shutdown is bounded")
        .expect("server task joins")
        .expect("server shuts down cleanly");
    database
        .close()
        .await
        .expect("database close is idempotent");
}

#[tokio::test]
async fn stalled_stream_reader_is_forced_closed_before_shared_resources() {
    let provider_listener = TcpListener::bind(("127.0.0.1", 0))
        .await
        .expect("bind loopback provider");
    let provider_address = provider_listener.local_addr().expect("provider address");
    let (provider_stopped_tx, provider_stopped_rx) = oneshot::channel();
    let provider = tokio::spawn(streaming_fixture_provider(
        provider_listener,
        provider_stopped_tx,
    ));

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
    let mut config = Config::default();
    config.server.api_key = Some("test-key-transport".to_owned());
    config.models.startup_refresh = false;
    let mut provider_config = ProviderConfig {
        id: "fixture".to_owned(),
        base_url: format!("http://{provider_address}"),
        protocols: vec!["openai".to_owned()],
        accounts: vec![AccountConfig {
            name: "fixture-account".to_owned(),
            api_key: Some("fixture-provider-key".to_owned()),
            ..AccountConfig::default()
        }],
        ..ProviderConfig::default()
    };
    provider_config.wire_surfaces.insert(
        "openai_responses".to_owned(),
        ProviderWireSurfaceConfig {
            path_template: "/responses".to_owned(),
            auth: Some(ProviderAuthConfig::default()),
            ..ProviderWireSurfaceConfig::default()
        },
    );
    provider_config.model_wire.insert(
        "stream-fixture".to_owned(),
        ModelWirePreference {
            preferred_surface: "openai_responses".to_owned(),
            fixed: true,
        },
    );
    provider_config
        .static_models
        .push(ProviderStaticModelConfig {
            id: "stream-fixture".to_owned(),
            protocol: Some("openai".to_owned()),
            ..ProviderStaticModelConfig::default()
        });
    config
        .providers
        .insert("fixture".to_owned(), provider_config.clone());
    AccountRepository::new(&database)
        .sync_from_config(vec![DbAccountConfig {
            name: "fixture-account".to_owned(),
            api_key_env: String::new(),
            enabled: true,
            weight: 1.0,
            provider_id: "fixture".to_owned(),
        }])
        .await
        .expect("fixture account synchronizes");
    database
        .call(|connection| {
            connection.execute(
                "INSERT INTO models (model_id, protocol, provider_id, resolution_status) VALUES ('stream-fixture', 'openai', 'fixture', 'resolved')",
                [],
            )?;
            Ok(())
        })
        .await
        .expect("fixture model is catalogued");
    let process = ProcessRuntime::new(database.clone());
    let candidate = RuntimeGenerationFactory::prepare(
        &process,
        config.clone(),
        "transport-stream-shutdown".to_owned(),
        1,
    )
    .await
    .expect("streaming generation prepares");
    let manager = Arc::new(RuntimeManager::new(
        candidate.transfer().expect("generation transfers"),
    ));
    process
        .install_initial_tasks((*manager).clone(), &config)
        .await
        .expect("initial tasks install");
    let runtime = ServerRuntime::new(process, manager, config);
    let handle = runtime.handle();
    let listener = TcpListener::bind(("127.0.0.1", 0))
        .await
        .expect("bind HTTP listener");
    let address = listener.local_addr().expect("listener address");
    let task = tokio::spawn(async move { runtime.serve_listener(listener).await });

    let finite_body = br#"{"model":"stream-fixture","input":"ping","store":false,"stream":false}"#;
    let mut finite_request = format!(
        "POST /v1/responses HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer test-key-transport\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        finite_body.len()
    )
    .into_bytes();
    finite_request.extend_from_slice(finite_body);
    let finite_response = request(address, &finite_request).await;
    assert!(
        finite_response.starts_with("HTTP/1.1 200"),
        "{finite_response}"
    );
    assert!(
        finite_response.contains("finite transport answer"),
        "{finite_response}"
    );

    let socket = TcpSocket::new_v4().expect("create client socket");
    socket
        .set_recv_buffer_size(1024)
        .expect("constrain stalled client's receive window");
    let mut client = socket.connect(address).await.expect("connect client");
    let body = br#"{"model":"stream-fixture","input":"ping","store":false,"stream":true}"#;
    let request_head = format!(
        "POST /v1/responses HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer test-key-transport\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n",
        body.len()
    );
    client
        .write_all(request_head.as_bytes())
        .await
        .expect("write request headers");
    client.write_all(body).await.expect("write request body");
    let mut response_head = Vec::new();
    tokio::time::timeout(Duration::from_secs(3), async {
        while !response_head.ends_with(b"\r\n\r\n") {
            let mut byte = [0_u8; 1];
            client.read_exact(&mut byte).await?;
            response_head.push(byte[0]);
        }
        std::io::Result::Ok(())
    })
    .await
    .expect("stream response starts before provider completion")
    .expect("read response headers");
    if !String::from_utf8_lossy(&response_head).starts_with("HTTP/1.1 200") {
        let mut error_body = Vec::new();
        let _ =
            tokio::time::timeout(Duration::from_secs(1), client.read_to_end(&mut error_body)).await;
        panic!(
            "{}{}",
            String::from_utf8_lossy(&response_head),
            String::from_utf8_lossy(&error_body)
        );
    }
    let mut first_stream_event = Vec::new();
    tokio::time::timeout(Duration::from_secs(3), async {
        while !first_stream_event
            .windows(b"event: response.output_text.delta".len())
            .any(|window| window == b"event: response.output_text.delta")
        {
            let mut byte = [0_u8; 1];
            client.read_exact(&mut byte).await?;
            first_stream_event.push(byte[0]);
        }
        std::io::Result::Ok(())
    })
    .await
    .expect("first downstream event arrives before provider completion")
    .expect("read first downstream stream event");

    let started = tokio::time::Instant::now();
    assert!(handle.request_shutdown(ShutdownReason::Requested));
    let report = tokio::time::timeout(Duration::from_secs(11), task)
        .await
        .expect("EggServe child and EggPool resources stay within one deadline")
        .expect("server task joins")
        .expect("server shuts down cleanly");
    assert!(started.elapsed() <= Duration::from_secs(10));
    assert!(report.database_closed);
    tokio::time::timeout(Duration::from_secs(2), provider_stopped_rx)
        .await
        .expect("stream producer observes downstream cancellation")
        .expect("provider connection closes");
    let _ = tokio::time::timeout(Duration::from_secs(2), provider).await;
    let mut eof = Vec::new();
    tokio::time::timeout(Duration::from_secs(2), client.read_to_end(&mut eof))
        .await
        .expect("stalled downstream socket closes")
        .expect("read client close");
    database
        .close()
        .await
        .expect("database close is idempotent");
}

/// Compact fixture provider: accepts one finite upstream request and returns
/// deterministic remote-compaction replacement material.
async fn compact_fixture_provider(listener: TcpListener) -> std::io::Result<()> {
    loop {
        let (mut stream, _) = listener.accept().await?;
        let mut request = Vec::new();
        let mut header_end = None;
        while header_end.is_none() {
            let mut buffer = [0_u8; 4096];
            let read = stream.read(&mut buffer).await?;
            if read == 0 {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::UnexpectedEof,
                    "provider request ended before headers",
                ));
            }
            request.extend_from_slice(&buffer[..read]);
            header_end = request.windows(4).position(|window| window == b"\r\n\r\n");
            if request.len() > 64 * 1024 {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::InvalidData,
                    "provider request headers too large",
                ));
            }
        }
        let header_end = header_end.expect("header terminator was observed") + 4;
        let headers = String::from_utf8_lossy(&request[..header_end]);
        let content_length = headers
            .lines()
            .find_map(|line| {
                let (name, value) = line.split_once(':')?;
                name.eq_ignore_ascii_case("content-length")
                    .then(|| value.trim().parse::<usize>().ok())
                    .flatten()
            })
            .unwrap_or(0);
        while request.len() < header_end + content_length {
            let mut buffer = [0_u8; 4096];
            let read = stream.read(&mut buffer).await?;
            if read == 0 {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::UnexpectedEof,
                    "provider request body ended early",
                ));
            }
            request.extend_from_slice(&buffer[..read]);
        }
        let payload = br#"{"id":"resp-compact-1","object":"response","model":"compact-fixture","output":[{"type":"message","role":"assistant","content":[{"type":"output_text","text":"compact checkpoint summary"}]}],"usage":{"input_tokens":12,"output_tokens":4,"total_tokens":16}}"#;
        let head = format!(
            "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n",
            payload.len()
        );
        stream.write_all(head.as_bytes()).await?;
        stream.write_all(payload).await?;
    }
}

async fn compact_capable_runtime(
    provider_address: std::net::SocketAddr,
) -> (tempfile::TempDir, Database, ServerRuntime) {
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
    let mut config = Config::default();
    config.server.api_key = Some("test-key-transport".to_owned());
    config.models.startup_refresh = false;
    let mut provider_config = ProviderConfig {
        id: "fixture".to_owned(),
        base_url: format!("http://{provider_address}"),
        protocols: vec!["openai".to_owned()],
        accounts: vec![AccountConfig {
            name: "fixture-account".to_owned(),
            api_key: Some("fixture-provider-key".to_owned()),
            ..AccountConfig::default()
        }],
        ..ProviderConfig::default()
    };
    provider_config.wire_surfaces.insert(
        "openai_responses".to_owned(),
        ProviderWireSurfaceConfig {
            path_template: "/responses".to_owned(),
            auth: Some(ProviderAuthConfig::default()),
            supports_remote_compaction_v1: true,
            compact_path_template: Some("/responses/compact".to_owned()),
            ..ProviderWireSurfaceConfig::default()
        },
    );
    provider_config.model_wire.insert(
        "compact-fixture".to_owned(),
        ModelWirePreference {
            preferred_surface: "openai_responses".to_owned(),
            fixed: true,
        },
    );
    provider_config
        .static_models
        .push(ProviderStaticModelConfig {
            id: "compact-fixture".to_owned(),
            protocol: Some("openai".to_owned()),
            ..ProviderStaticModelConfig::default()
        });
    config
        .providers
        .insert("fixture".to_owned(), provider_config);
    AccountRepository::new(&database)
        .sync_from_config(vec![DbAccountConfig {
            name: "fixture-account".to_owned(),
            api_key_env: String::new(),
            enabled: true,
            weight: 1.0,
            provider_id: "fixture".to_owned(),
        }])
        .await
        .expect("fixture account synchronizes");
    database
        .call(|connection| {
            connection.execute(
                "INSERT INTO models (model_id, protocol, provider_id, resolution_status) VALUES ('compact-fixture', 'openai', 'fixture', 'resolved')",
                [],
            )?;
            Ok(())
        })
        .await
        .expect("fixture model is catalogued");
    let process = ProcessRuntime::new(database.clone());
    let candidate = RuntimeGenerationFactory::prepare(
        &process,
        config.clone(),
        "transport-compact".to_owned(),
        1,
    )
    .await
    .expect("compact generation prepares");
    let manager = Arc::new(RuntimeManager::new(
        candidate.transfer().expect("generation transfers"),
    ));
    process
        .install_initial_tasks((*manager).clone(), &config)
        .await
        .expect("initial tasks install");
    (
        directory,
        database,
        ServerRuntime::new(process, manager, config),
    )
}

#[tokio::test]
async fn compact_route_shares_generation_admission_and_returns_compact_result() {
    let provider_listener = TcpListener::bind(("127.0.0.1", 0))
        .await
        .expect("bind loopback compact provider");
    let provider_address = provider_listener.local_addr().expect("provider address");
    let provider = tokio::spawn(compact_fixture_provider(provider_listener));

    let (_directory, database, runtime) = compact_capable_runtime(provider_address).await;
    let handle = runtime.handle();
    let listener = TcpListener::bind(("127.0.0.1", 0))
        .await
        .expect("bind HTTP listener");
    let address = listener.local_addr().expect("listener address");
    let task = tokio::spawn(async move { runtime.serve_listener(listener).await });

    let compact_body = br#"{"model":"compact-fixture","input":"history to compact"}"#;
    let mut compact_request = format!(
        "POST /v1/responses/compact HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer test-key-transport\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        compact_body.len()
    )
    .into_bytes();
    compact_request.extend_from_slice(compact_body);
    let response = request(address, &compact_request).await;
    assert!(
        response.starts_with("HTTP/1.1 200"),
        "compact request did not reach compact application logic: {response}"
    );
    assert!(
        !response.contains("Missing extension") && !response.contains("Missing request extension"),
        "compact request failed on the pre-fix missing GenerationLease path: {response}"
    );
    assert!(
        response.contains("compact checkpoint summary"),
        "compact response did not carry provider replacement material: {response}"
    );

    let health = request(
        address,
        b"GET /v1/healthz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
    )
    .await;
    assert!(health.starts_with("HTTP/1.1 200"), "{health}");

    assert!(handle.request_shutdown(ShutdownReason::Requested));
    let report = tokio::time::timeout(Duration::from_secs(6), task)
        .await
        .expect("transport shutdown is bounded")
        .expect("server task joins")
        .expect("server shuts down cleanly");
    assert!(report.database_closed);
    provider.abort();
    database
        .close()
        .await
        .expect("database close is idempotent");
}

#[tokio::test]
async fn eggserve_040_finite_response_framing_is_valid_without_trailers() {
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
    assert!(health.starts_with("HTTP/1.1 200"), "{health}");
    let head_end = health.find("\r\n\r\n").expect("health head terminates");
    let head = health[..head_end].to_ascii_lowercase();
    assert!(
        head.contains("content-type: application/json"),
        "health keeps JSON content type: {health}"
    );
    let has_length = head.contains("content-length:");
    let has_chunked = head.contains("transfer-encoding:") && head.contains("chunked");
    assert!(
        has_length ^ has_chunked,
        "finite response carries exactly one framing declaration: {health}"
    );
    assert!(
        !head.contains("trailer"),
        "finite response declares no H1 trailers: {health}"
    );
    let body = &health[head_end + 4..];
    assert!(
        body.contains("\"status\""),
        "finite JSON body is intact: {health}"
    );

    assert!(handle.request_shutdown(ShutdownReason::Requested));
    let report = tokio::time::timeout(Duration::from_secs(6), task)
        .await
        .expect("transport shutdown is bounded")
        .expect("server task joins")
        .expect("server shuts down cleanly");
    assert!(report.database_closed);
    database
        .close()
        .await
        .expect("database close is idempotent");
}

#[tokio::test]
async fn eggserve_040_streaming_stays_incremental_without_trailers() {
    let provider_listener = TcpListener::bind(("127.0.0.1", 0))
        .await
        .expect("bind loopback provider");
    let provider_address = provider_listener.local_addr().expect("provider address");
    let (provider_stopped_tx, provider_stopped_rx) = oneshot::channel();
    let provider = tokio::spawn(streaming_fixture_provider(
        provider_listener,
        provider_stopped_tx,
    ));

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
    let mut config = Config::default();
    config.server.api_key = Some("test-key-transport".to_owned());
    config.models.startup_refresh = false;
    let mut provider_config = ProviderConfig {
        id: "fixture".to_owned(),
        base_url: format!("http://{provider_address}"),
        protocols: vec!["openai".to_owned()],
        accounts: vec![AccountConfig {
            name: "fixture-account".to_owned(),
            api_key: Some("fixture-provider-key".to_owned()),
            ..AccountConfig::default()
        }],
        ..ProviderConfig::default()
    };
    provider_config.wire_surfaces.insert(
        "openai_responses".to_owned(),
        ProviderWireSurfaceConfig {
            path_template: "/responses".to_owned(),
            auth: Some(ProviderAuthConfig::default()),
            ..ProviderWireSurfaceConfig::default()
        },
    );
    provider_config.model_wire.insert(
        "stream-fixture".to_owned(),
        ModelWirePreference {
            preferred_surface: "openai_responses".to_owned(),
            fixed: true,
        },
    );
    provider_config
        .static_models
        .push(ProviderStaticModelConfig {
            id: "stream-fixture".to_owned(),
            protocol: Some("openai".to_owned()),
            ..ProviderStaticModelConfig::default()
        });
    config
        .providers
        .insert("fixture".to_owned(), provider_config.clone());
    AccountRepository::new(&database)
        .sync_from_config(vec![DbAccountConfig {
            name: "fixture-account".to_owned(),
            api_key_env: String::new(),
            enabled: true,
            weight: 1.0,
            provider_id: "fixture".to_owned(),
        }])
        .await
        .expect("fixture account synchronizes");
    database
        .call(|connection| {
            connection.execute(
                "INSERT INTO models (model_id, protocol, provider_id, resolution_status) VALUES ('stream-fixture', 'openai', 'fixture', 'resolved')",
                [],
            )?;
            Ok(())
        })
        .await
        .expect("fixture model is catalogued");
    let process = ProcessRuntime::new(database.clone());
    let candidate = RuntimeGenerationFactory::prepare(
        &process,
        config.clone(),
        "transport-stream-trailer-guard".to_owned(),
        1,
    )
    .await
    .expect("streaming generation prepares");
    let manager = Arc::new(RuntimeManager::new(
        candidate.transfer().expect("generation transfers"),
    ));
    process
        .install_initial_tasks((*manager).clone(), &config)
        .await
        .expect("initial tasks install");
    let runtime = ServerRuntime::new(process, manager, config);
    let handle = runtime.handle();
    let listener = TcpListener::bind(("127.0.0.1", 0))
        .await
        .expect("bind HTTP listener");
    let address = listener.local_addr().expect("listener address");
    let task = tokio::spawn(async move { runtime.serve_listener(listener).await });

    let mut client = TcpStream::connect(address).await.expect("connect client");
    let body = br#"{"model":"stream-fixture","input":"ping","store":false,"stream":true}"#;
    let request_head = format!(
        "POST /v1/responses HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer test-key-transport\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n",
        body.len()
    );
    client
        .write_all(request_head.as_bytes())
        .await
        .expect("write request headers");
    client.write_all(body).await.expect("write request body");
    let mut response_head = Vec::new();
    tokio::time::timeout(Duration::from_secs(3), async {
        while !response_head.ends_with(b"\r\n\r\n") {
            let mut byte = [0_u8; 1];
            client.read_exact(&mut byte).await?;
            response_head.push(byte[0]);
        }
        std::io::Result::Ok(())
    })
    .await
    .expect("stream response starts before provider completion")
    .expect("read response headers");
    let head_text = String::from_utf8_lossy(&response_head);
    assert!(head_text.starts_with("HTTP/1.1 200"), "{head_text}");
    let head_lower = head_text.to_ascii_lowercase();
    assert!(
        head_lower.contains("text/event-stream"),
        "SSE keeps event-stream content type: {head_text}"
    );
    assert!(
        head_lower.contains("transfer-encoding:") && head_lower.contains("chunked"),
        "SSE stays incremental/chunked: {head_text}"
    );
    assert!(
        !head_lower.contains("trailer"),
        "SSE declares no H1 trailers without an EggPool declaration: {head_text}"
    );
    let mut first_stream_event = Vec::new();
    tokio::time::timeout(Duration::from_secs(3), async {
        while !first_stream_event
            .windows(b"event: response.output_text.delta".len())
            .any(|window| window == b"event: response.output_text.delta")
        {
            let mut byte = [0_u8; 1];
            client.read_exact(&mut byte).await?;
            first_stream_event.push(byte[0]);
        }
        std::io::Result::Ok(())
    })
    .await
    .expect("first downstream event arrives incrementally")
    .expect("read first downstream stream event");

    assert!(handle.request_shutdown(ShutdownReason::Requested));
    let report = tokio::time::timeout(Duration::from_secs(11), task)
        .await
        .expect("transport shutdown is bounded")
        .expect("server task joins")
        .expect("server shuts down cleanly");
    assert!(report.database_closed);
    tokio::time::timeout(Duration::from_secs(2), provider_stopped_rx)
        .await
        .expect("stream producer observes downstream cancellation")
        .expect("provider connection closes");
    let _ = tokio::time::timeout(Duration::from_secs(2), provider).await;
    database
        .close()
        .await
        .expect("database close is idempotent");
}

#[tokio::test]
async fn unknown_body_reservation_releases_at_stream_handoff() {
    const BODY_LIMIT: u64 = 64 * 1024 * 1024;
    let provider_listener = TcpListener::bind(("127.0.0.1", 0))
        .await
        .expect("bind loopback provider");
    let provider_address = provider_listener.local_addr().expect("provider address");
    let (provider_stopped_tx, provider_stopped_rx) = oneshot::channel();
    let provider = tokio::spawn(concurrent_streaming_fixture_provider(
        provider_listener,
        provider_stopped_tx,
    ));

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
    let mut config = Config::default();
    config.server.api_key = Some("test-key-transport".to_owned());
    config.server.max_request_body_bytes = BODY_LIMIT;
    config.models.startup_refresh = false;
    let mut provider_config = ProviderConfig {
        id: "fixture".to_owned(),
        base_url: format!("http://{provider_address}"),
        protocols: vec!["openai".to_owned()],
        accounts: ["fixture-account-a", "fixture-account-b"]
            .into_iter()
            .map(|name| AccountConfig {
                name: name.to_owned(),
                api_key: Some("fixture-provider-key".to_owned()),
                ..AccountConfig::default()
            })
            .collect(),
        ..ProviderConfig::default()
    };
    provider_config.wire_surfaces.insert(
        "openai_responses".to_owned(),
        ProviderWireSurfaceConfig {
            path_template: "/responses".to_owned(),
            auth: Some(ProviderAuthConfig::default()),
            ..ProviderWireSurfaceConfig::default()
        },
    );
    provider_config.model_wire.insert(
        "stream-fixture".to_owned(),
        ModelWirePreference {
            preferred_surface: "openai_responses".to_owned(),
            fixed: true,
        },
    );
    provider_config
        .static_models
        .push(ProviderStaticModelConfig {
            id: "stream-fixture".to_owned(),
            protocol: Some("openai".to_owned()),
            ..ProviderStaticModelConfig::default()
        });
    config
        .providers
        .insert("fixture".to_owned(), provider_config);
    AccountRepository::new(&database)
        .sync_from_config(
            ["fixture-account-a", "fixture-account-b"]
                .into_iter()
                .map(|name| DbAccountConfig {
                    name: name.to_owned(),
                    api_key_env: String::new(),
                    enabled: true,
                    weight: 1.0,
                    provider_id: "fixture".to_owned(),
                })
                .collect(),
        )
        .await
        .expect("fixture accounts synchronize");
    database
        .call(|connection| {
            connection.execute(
                "INSERT INTO models (model_id, protocol, provider_id, resolution_status) VALUES ('stream-fixture', 'openai', 'fixture', 'resolved')",
                [],
            )?;
            Ok(())
        })
        .await
        .expect("fixture model is catalogued");
    let process = ProcessRuntime::new(database.clone());
    let candidate = RuntimeGenerationFactory::prepare(
        &process,
        config.clone(),
        "stream-body-reservation".to_owned(),
        1,
    )
    .await
    .expect("streaming generation prepares");
    let manager = Arc::new(RuntimeManager::new(
        candidate.transfer().expect("generation transfers"),
    ));
    process
        .install_initial_tasks((*manager).clone(), &config)
        .await
        .expect("initial tasks install");
    let runtime = ServerRuntime::new(process, manager, config);
    let handle = runtime.handle();
    let listener = TcpListener::bind(("127.0.0.1", 0))
        .await
        .expect("bind HTTP listener");
    let address = listener.local_addr().expect("listener address");
    let task = tokio::spawn(async move { runtime.serve_listener(listener).await });

    let mut first = start_unknown_length_stream_request(address).await;
    let (first_head, _) =
        tokio::time::timeout(Duration::from_secs(5), read_stream_start(&mut first))
            .await
            .expect("first stream starts")
            .expect("read first stream");
    assert!(first_head.starts_with("HTTP/1.1 200"), "{first_head}");

    let mut second = start_unknown_length_stream_request(address).await;
    let (second_head, _) =
        tokio::time::timeout(Duration::from_secs(5), read_stream_start(&mut second))
            .await
            .expect("second stream starts after first handler handoff")
            .expect("read second stream");
    assert!(second_head.starts_with("HTTP/1.1 200"), "{second_head}");

    drop(first);
    drop(second);
    tokio::time::timeout(Duration::from_secs(2), provider_stopped_rx)
        .await
        .expect("provider observes downstream cancellation")
        .expect("provider task stop is signaled");
    tokio::time::timeout(Duration::from_secs(2), provider)
        .await
        .expect("provider workers stop")
        .expect("provider task joins")
        .expect("provider workers finish cleanly");
    assert!(handle.request_shutdown(ShutdownReason::Requested));
    let report = tokio::time::timeout(Duration::from_secs(11), task)
        .await
        .expect("transport shutdown is bounded")
        .expect("server task joins")
        .expect("server shuts down cleanly");
    assert!(report.database_closed);
    database
        .close()
        .await
        .expect("database close is idempotent");
}

#[tokio::test]
async fn compact_route_enforces_live_generation_body_ceiling() {
    let (_directory, database, runtime) = runtime_fixture_with_body_limit(64).await;
    let handle = runtime.handle();
    let listener = TcpListener::bind(("127.0.0.1", 0))
        .await
        .expect("bind listener");
    let address = listener.local_addr().expect("listener address");
    let task = tokio::spawn(async move { runtime.serve_listener(listener).await });

    // Below the live generation ceiling the request reaches compact
    // application logic: no provider is configured, so the coordinator
    // returns its deterministic application error rather than the Axum
    // missing-extension rejection or a 413.
    let small_body = br#"{"model":"m","input":"h"}"#;
    assert!(small_body.len() < 64, "fixture must stay below the ceiling");
    let mut small_request = format!(
        "POST /v1/responses/compact HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer test-key-transport\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        small_body.len()
    )
    .into_bytes();
    small_request.extend_from_slice(small_body);
    let response = request(address, &small_request).await;
    assert!(
        !response.starts_with("HTTP/1.1 413"),
        "below-limit compact request was body-limited: {response}"
    );
    assert!(
        !response.contains("Missing extension") && !response.contains("Missing request extension"),
        "below-limit compact request missed generation admission: {response}"
    );
    assert!(
        response.contains("No eligible account was available")
            || response.contains("upstream_error"),
        "below-limit compact request did not reach compact logic: {response}"
    );

    // An over-limit Content-Length compact request returns the existing
    // EggPool 413 JSON contract.
    let large_body = vec![b'x'; 65];
    let mut content_length = format!(
        "POST /v1/responses/compact HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer test-key-transport\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        large_body.len()
    )
    .into_bytes();
    content_length.extend_from_slice(&large_body);
    let response = request(address, &content_length).await;
    assert!(response.starts_with("HTTP/1.1 413"), "{response}");
    assert!(
        response.contains("Request body too large"),
        "over-limit compact request missed the 413 contract: {response}"
    );

    // A chunked compact request crossing the same live ceiling is also 413.
    let mut chunked = b"POST /v1/responses/compact HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer test-key-transport\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n41\r\n"
        .to_vec();
    chunked.extend_from_slice(&large_body);
    chunked.extend_from_slice(b"\r\n0\r\n\r\n");
    let response = request(address, &chunked).await;
    assert!(response.starts_with("HTTP/1.1 413"), "{response}");
    assert!(
        response.contains("Request body too large"),
        "chunked over-limit compact request missed the 413 contract: {response}"
    );

    // A healthy request still reaches compact logic afterward: the rejection
    // does not poison the connection or runtime.
    let response = request(address, &small_request).await;
    assert!(
        !response.starts_with("HTTP/1.1 413"),
        "rejection poisoned later compact requests: {response}"
    );
    assert!(
        !response.contains("Missing extension") && !response.contains("Missing request extension"),
        "later compact request missed generation admission: {response}"
    );
    let health = request(
        address,
        b"GET /v1/healthz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
    )
    .await;
    assert!(health.starts_with("HTTP/1.1 200"), "{health}");

    assert!(handle.request_shutdown(ShutdownReason::Requested));
    let report = tokio::time::timeout(Duration::from_secs(6), task)
        .await
        .expect("transport shutdown is bounded")
        .expect("server task joins")
        .expect("server shuts down cleanly");
    assert_eq!(report.body_tasks_at_deadline, 0);
    database
        .close()
        .await
        .expect("database close is idempotent");
}
