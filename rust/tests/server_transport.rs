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
    (
        directory,
        database,
        ServerRuntime::new(process, manager, config),
    )
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

    let inference = request(
        address,
        b"GET /api/status HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
    )
    .await;
    assert!(inference.starts_with("HTTP/1.1 401"), "{inference}");

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
    let mut first = Vec::new();
    tokio::time::timeout(Duration::from_secs(2), async {
        while !first.ends_with(b"\r\n0\r\n\r\n") {
            let mut byte = [0_u8; 1];
            stream.read_exact(&mut byte).await?;
            first.push(byte[0]);
        }
        std::io::Result::Ok(())
    })
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
    let mut second = Vec::new();
    tokio::time::timeout(Duration::from_secs(2), async {
        while !second.ends_with(b"\r\n0\r\n\r\n") {
            let mut byte = [0_u8; 1];
            stream.read_exact(&mut byte).await?;
            second.push(byte[0]);
        }
        std::io::Result::Ok(())
    })
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
    let mut content_length = format!(
        "POST /v1/responses HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer test-key-transport\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        body.len()
    )
    .into_bytes();
    content_length.extend_from_slice(&body);
    let response = request(address, &content_length).await;
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
