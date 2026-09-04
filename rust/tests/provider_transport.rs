use std::{
    io::{Read, Write},
    net::{TcpListener, TcpStream},
    sync::{
        Arc, Mutex,
        atomic::{AtomicUsize, Ordering},
    },
    thread,
    time::{Duration, Instant},
};

use bytes::Bytes;
use eggpool::providers::{ProviderHttpClient, ProviderHttpConfig, TransportError};
use http::{HeaderMap, HeaderValue, Method};
use rustls::{
    ServerConfig, ServerConnection,
    pki_types::{CertificateDer, PrivateKeyDer},
};

#[derive(Debug, Clone, PartialEq, Eq)]
struct RequestObservation {
    method: String,
    target: String,
    headers: Vec<String>,
    body: Vec<u8>,
}

#[derive(Clone, Copy)]
enum ResponseMode {
    Normal,
    Chunked,
    SlowHeaders,
    Premature,
}

struct FixtureServer {
    address: String,
    connections: Arc<AtomicUsize>,
    requests: Arc<Mutex<Vec<RequestObservation>>>,
    tls_certificate: Option<Vec<u8>>,
    thread: Option<thread::JoinHandle<()>>,
}

impl FixtureServer {
    fn http(mode: ResponseMode, expected_requests: usize) -> Self {
        Self::start(mode, expected_requests, false)
    }

    fn https(expected_requests: usize) -> Self {
        Self::start(ResponseMode::Normal, expected_requests, true)
    }

    fn start(mode: ResponseMode, expected_requests: usize, tls: bool) -> Self {
        let listener = TcpListener::bind(("127.0.0.1", 0)).expect("fixture listener");
        let address = format!("127.0.0.1:{}", listener.local_addr().unwrap().port());
        let connections = Arc::new(AtomicUsize::new(0));
        let requests = Arc::new(Mutex::new(Vec::new()));
        let tls_material = tls.then(make_tls_material);
        let tls_certificate = tls_material.as_ref().map(|material| material.2.clone());
        let connection_count = Arc::clone(&connections);
        let request_log = Arc::clone(&requests);
        let thread = thread::spawn(move || {
            let tls_config = tls_material.as_ref().map(server_tls_config);
            let mut served = 0;
            while served < expected_requests {
                let (stream, _) = listener.accept().expect("fixture accept");
                stream
                    .set_read_timeout(Some(Duration::from_secs(2)))
                    .expect("fixture read timeout");
                connection_count.fetch_add(1, Ordering::SeqCst);
                let mut stream = if let Some(config) = &tls_config {
                    TestStream::Tls(Box::new(rustls::StreamOwned::new(
                        ServerConnection::new(Arc::clone(config)).expect("server connection"),
                        stream,
                    )))
                } else {
                    TestStream::Plain(stream)
                };
                loop {
                    let Some(request) = read_request(&mut stream) else {
                        if tls {
                            return;
                        }
                        break;
                    };
                    request_log.lock().unwrap().push(request);
                    write_response(&mut stream, mode);
                    served += 1;
                    if served == expected_requests {
                        break;
                    }
                }
            }
        });
        Self {
            address,
            connections,
            requests,
            tls_certificate,
            thread: Some(thread),
        }
    }

    fn http_url(&self) -> String {
        format!(
            "http://localhost:{}",
            self.address.rsplit(':').next().unwrap()
        )
    }

    fn https_url(&self) -> String {
        format!(
            "https://localhost:{}",
            self.address.rsplit(':').next().unwrap()
        )
    }

    fn https_ip_url(&self) -> String {
        format!(
            "https://127.0.0.1:{}",
            self.address.rsplit(':').next().unwrap()
        )
    }

    fn requests(&self) -> Vec<RequestObservation> {
        self.requests.lock().unwrap().clone()
    }
}

impl Drop for FixtureServer {
    fn drop(&mut self) {
        if let Some(thread) = self.thread.take() {
            thread.join().expect("fixture thread");
        }
    }
}

enum TestStream {
    Plain(TcpStream),
    Tls(Box<rustls::StreamOwned<ServerConnection, TcpStream>>),
}

impl Read for TestStream {
    fn read(&mut self, buffer: &mut [u8]) -> std::io::Result<usize> {
        match self {
            Self::Plain(stream) => stream.read(buffer),
            Self::Tls(stream) => stream.read(buffer),
        }
    }
}

impl Write for TestStream {
    fn write(&mut self, buffer: &[u8]) -> std::io::Result<usize> {
        match self {
            Self::Plain(stream) => stream.write(buffer),
            Self::Tls(stream) => stream.write(buffer),
        }
    }

    fn flush(&mut self) -> std::io::Result<()> {
        match self {
            Self::Plain(stream) => stream.flush(),
            Self::Tls(stream) => stream.flush(),
        }
    }
}

fn read_request(stream: &mut TestStream) -> Option<RequestObservation> {
    let mut head = Vec::new();
    let mut byte = [0; 1];
    loop {
        if stream.read(&mut byte).ok()? == 0 {
            return None;
        }
        head.push(byte[0]);
        if head.ends_with(b"\r\n\r\n") {
            break;
        }
        if head.len() > 64 * 1024 {
            return None;
        }
    }
    let header_end = head.len() - 4;
    let header_text = String::from_utf8_lossy(&head[..header_end]);
    let mut lines = header_text.split("\r\n");
    let first = lines.next()?;
    let mut first_parts = first.splitn(3, ' ');
    let method = first_parts.next()?.to_owned();
    let target = first_parts.next()?.to_owned();
    let mut headers = Vec::new();
    let mut content_length = 0;
    for line in lines {
        let Some((name, value)) = line.split_once(':') else {
            continue;
        };
        headers.push(name.to_ascii_lowercase());
        if name.eq_ignore_ascii_case("content-length") {
            content_length = value.trim().parse().ok()?;
        }
    }
    let mut body = vec![0; content_length];
    stream.read_exact(&mut body).ok()?;
    Some(RequestObservation {
        method,
        target,
        headers,
        body,
    })
}

fn write_response(stream: &mut TestStream, mode: ResponseMode) {
    match mode {
        ResponseMode::Normal => {
            stream
                .write_all(
                    b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\nconnection: keep-alive\r\n\r\nok",
                )
                .unwrap();
        }
        ResponseMode::Chunked => {
            stream
                .write_all(
                    b"HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\nconnection: close\r\n\r\n5\r\nfirst\r\n",
                )
                .unwrap();
            stream.flush().unwrap();
            thread::sleep(Duration::from_millis(150));
            stream.write_all(b"6\r\nsecond\r\n0\r\n\r\n").unwrap();
        }
        ResponseMode::SlowHeaders => {
            thread::sleep(Duration::from_millis(150));
            stream
                .write_all(b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\nconnection: close\r\n\r\nok")
                .unwrap();
        }
        ResponseMode::Premature => {
            stream
                .write_all(
                    b"HTTP/1.1 200 OK\r\ncontent-length: 10\r\nconnection: close\r\n\r\nshort",
                )
                .unwrap();
        }
    }
    stream.flush().unwrap();
}

fn make_tls_material() -> (Vec<u8>, Vec<u8>, Vec<u8>) {
    let mut ca_params =
        rcgen::CertificateParams::new(vec!["provider-test-ca".to_owned()]).expect("CA parameters");
    ca_params.is_ca = rcgen::IsCa::Ca(rcgen::BasicConstraints::Unconstrained);
    let ca_key = rcgen::KeyPair::generate().expect("CA key");
    let ca_certificate = ca_params.self_signed(&ca_key).expect("CA certificate");
    let ca_issuer = rcgen::Issuer::new(ca_params, ca_key);
    let mut leaf_params =
        rcgen::CertificateParams::new(vec!["localhost".to_owned()]).expect("leaf parameters");
    leaf_params
        .extended_key_usages
        .push(rcgen::ExtendedKeyUsagePurpose::ServerAuth);
    let leaf_key = rcgen::KeyPair::generate().expect("leaf key");
    let leaf = leaf_params
        .signed_by(&leaf_key, &ca_issuer)
        .expect("leaf certificate");
    (
        leaf.der().to_vec(),
        leaf_key.serialize_der(),
        ca_certificate.der().to_vec(),
    )
}

fn server_tls_config(material: &(Vec<u8>, Vec<u8>, Vec<u8>)) -> Arc<ServerConfig> {
    let certificates = vec![CertificateDer::from(material.0.clone())];
    let key = PrivateKeyDer::Pkcs8(material.1.clone().into());
    Arc::new(
        ServerConfig::builder_with_provider(Arc::new(rustls::crypto::ring::default_provider()))
            .with_safe_default_protocol_versions()
            .expect("TLS versions")
            .with_no_client_auth()
            .with_single_cert(certificates, key)
            .expect("fixture certificate/key"),
    )
}

fn make_client(base_url: &str) -> ProviderHttpClient {
    let mut config = ProviderHttpConfig::new(base_url).expect("provider config");
    config.connect_timeout = Duration::from_millis(100);
    config.read_timeout = Duration::from_millis(100);
    config.write_timeout = Duration::from_millis(100);
    config.pool_timeout = Duration::from_millis(50);
    config.max_connections = 2;
    config.max_keepalive = 1;
    ProviderHttpClient::new(config).expect("provider client")
}

fn header_map() -> HeaderMap {
    let mut headers = HeaderMap::new();
    headers.insert("x-contract", HeaderValue::from_static("fixture"));
    headers
}

#[tokio::test(flavor = "current_thread")]
async fn direct_http_preserves_request_shape_and_reuses_http11_connection() {
    let server = FixtureServer::http(ResponseMode::Normal, 2);
    let client = make_client(&server.http_url());
    let mut first = client
        .send(
            Method::POST,
            "/one?ignored=1",
            header_map(),
            Bytes::from_static(b"request body"),
        )
        .await
        .expect("first request");
    assert_eq!(first.status.as_u16(), 200);
    assert_eq!(
        first.body.next().await.unwrap().unwrap(),
        Bytes::from_static(b"ok")
    );
    let mut second = client
        .send(Method::GET, "/two", HeaderMap::new(), Bytes::new())
        .await
        .expect("second request");
    assert_eq!(
        second.body.next().await.unwrap().unwrap(),
        Bytes::from_static(b"ok")
    );
    drop(client);
    assert_eq!(server.connections.load(Ordering::SeqCst), 1);
    assert_eq!(
        server.requests(),
        vec![
            RequestObservation {
                method: "POST".into(),
                target: "/one?ignored=1".into(),
                headers: vec!["x-contract".into(), "host".into(), "content-length".into()],
                body: b"request body".to_vec()
            },
            RequestObservation {
                method: "GET".into(),
                target: "/two".into(),
                headers: vec!["host".into()],
                body: Vec::new()
            }
        ]
    );
}

#[tokio::test(flavor = "current_thread")]
async fn idle_keepalive_expiry_forces_a_new_physical_connection() {
    let server = FixtureServer::http(ResponseMode::Normal, 2);
    let mut config = ProviderHttpConfig::new(&server.http_url()).expect("provider config");
    config.keepalive_timeout = Duration::from_millis(25);
    let client = ProviderHttpClient::new(config).expect("provider client");
    let mut first = client
        .send(Method::GET, "/one", HeaderMap::new(), Bytes::new())
        .await
        .expect("first request");
    assert_eq!(
        first.body.next().await.unwrap().unwrap(),
        Bytes::from_static(b"ok")
    );
    assert!(first.body.next().await.is_none());
    tokio::time::sleep(Duration::from_millis(100)).await;
    let mut second = client
        .send(Method::GET, "/two", HeaderMap::new(), Bytes::new())
        .await
        .expect("second request");
    assert_eq!(
        second.body.next().await.unwrap().unwrap(),
        Bytes::from_static(b"ok")
    );
    assert!(second.body.next().await.is_none());
    drop(client);
    assert_eq!(server.connections.load(Ordering::SeqCst), 2);
}

#[tokio::test(flavor = "current_thread")]
async fn response_body_is_incremental_and_read_timeout_is_classified() {
    let server = FixtureServer::http(ResponseMode::Chunked, 1);
    let mut stream_config = ProviderHttpConfig::new(&server.http_url()).expect("provider config");
    stream_config.read_timeout = Duration::from_millis(500);
    let client = ProviderHttpClient::new(stream_config).expect("provider client");
    let started = Instant::now();
    let mut response = client
        .send(Method::GET, "/stream", HeaderMap::new(), Bytes::new())
        .await
        .expect("stream response");
    assert!(started.elapsed() < Duration::from_millis(100));
    assert_eq!(
        response.body.next().await.unwrap().unwrap(),
        Bytes::from_static(b"first")
    );
    assert_eq!(
        response.body.next().await.unwrap().unwrap(),
        Bytes::from_static(b"second")
    );
    assert!(response.body.next().await.is_none());

    let slow_server = FixtureServer::http(ResponseMode::SlowHeaders, 1);
    let slow_client = make_client(&slow_server.http_url());
    assert_eq!(
        slow_client
            .send(Method::GET, "/slow", HeaderMap::new(), Bytes::new())
            .await
            .expect_err("read timeout"),
        TransportError::ReadTimeout
    );
}

#[tokio::test(flavor = "current_thread")]
async fn premature_response_close_is_protocol_failure() {
    let server = FixtureServer::http(ResponseMode::Premature, 1);
    let client = make_client(&server.http_url());
    let mut response = client
        .send(Method::GET, "/premature", HeaderMap::new(), Bytes::new())
        .await
        .expect("response headers");
    assert_eq!(
        response.body.next().await.unwrap().unwrap(),
        Bytes::from_static(b"short")
    );
    assert_eq!(
        response.body.next().await.unwrap().unwrap_err(),
        TransportError::Protocol
    );
}

#[tokio::test(flavor = "current_thread")]
async fn pool_pressure_times_out_without_leaking_capacity() {
    let server = FixtureServer::http(ResponseMode::Chunked, 2);
    let mut config = ProviderHttpConfig::new(&server.http_url()).expect("provider config");
    config.max_connections = 1;
    config.max_keepalive = 1;
    config.pool_timeout = Duration::from_millis(30);
    let client = ProviderHttpClient::new(config).expect("provider client");
    let held = client
        .send(Method::GET, "/held", HeaderMap::new(), Bytes::new())
        .await
        .expect("held response");
    let error = client
        .send(Method::GET, "/wait", HeaderMap::new(), Bytes::new())
        .await
        .expect_err("pool timeout");
    assert_eq!(error, TransportError::PoolTimeout);
    drop(held);
    tokio::time::sleep(Duration::from_millis(200)).await;
    let released = client
        .send(Method::GET, "/released", HeaderMap::new(), Bytes::new())
        .await
        .expect("capacity released");
    assert_eq!(released.status.as_u16(), 200);
}

#[tokio::test(flavor = "current_thread")]
async fn cancellation_during_pool_wait_releases_no_permit() {
    let server = FixtureServer::http(ResponseMode::Chunked, 2);
    let mut config = ProviderHttpConfig::new(&server.http_url()).expect("provider config");
    config.max_connections = 1;
    config.max_keepalive = 1;
    config.pool_timeout = Duration::from_secs(5);
    let client = ProviderHttpClient::new(config).expect("provider client");
    let held = client
        .send(Method::GET, "/held", HeaderMap::new(), Bytes::new())
        .await
        .expect("held response");
    let waiting_client = client.clone();
    let mut waiting = tokio::spawn(async move {
        waiting_client
            .send(Method::GET, "/cancelled", HeaderMap::new(), Bytes::new())
            .await
    });
    tokio::select! {
        _ = tokio::time::sleep(Duration::from_millis(10)) => {}
        result = &mut waiting => panic!("pool wait completed before cancellation: {result:?}"),
    }
    waiting.abort();
    let _ = waiting.await;
    drop(held);
    tokio::time::sleep(Duration::from_millis(200)).await;
    let released = client
        .send(Method::GET, "/released", HeaderMap::new(), Bytes::new())
        .await
        .expect("capacity released");
    assert_eq!(released.status.as_u16(), 200);
}

#[tokio::test(flavor = "current_thread")]
async fn refused_connection_is_classified_as_connect_failure() {
    let listener = TcpListener::bind(("127.0.0.1", 0)).expect("closed-port fixture");
    let port = listener.local_addr().expect("closed-port address").port();
    drop(listener);
    let client = make_client(&format!("http://127.0.0.1:{port}"));
    assert_eq!(
        client
            .send(Method::GET, "/refused", HeaderMap::new(), Bytes::new())
            .await
            .expect_err("connection refusal"),
        TransportError::Connect
    );
}

#[tokio::test(flavor = "current_thread")]
async fn request_body_bound_is_enforced_before_connection() {
    let mut config = ProviderHttpConfig::new("http://127.0.0.1:1").expect("provider config");
    config.max_request_body_bytes = 4;
    let client = ProviderHttpClient::new(config).expect("provider client");
    assert_eq!(
        client
            .send(
                Method::POST,
                "/too-large",
                HeaderMap::new(),
                Bytes::from_static(b"12345")
            )
            .await
            .expect_err("request body bound"),
        TransportError::RequestBodyTooLarge
    );
}

#[tokio::test(flavor = "current_thread")]
async fn direct_https_uses_hostname_verified_explicit_test_root() {
    let server = FixtureServer::https(1);
    let mut config = ProviderHttpConfig::new(&server.https_url()).expect("provider config");
    config
        .additional_root_certificates
        .push(server.tls_certificate.clone().expect("TLS root"));
    let client = ProviderHttpClient::new(config).expect("provider client");
    let mut response = client
        .send(Method::GET, "/secure", HeaderMap::new(), Bytes::new())
        .await
        .expect("HTTPS response");
    assert_eq!(response.status.as_u16(), 200);
    assert_eq!(
        response.body.next().await.unwrap().unwrap(),
        Bytes::from_static(b"ok")
    );

    let mismatch_server = FixtureServer::https(1);
    let mut mismatch_config =
        ProviderHttpConfig::new(&mismatch_server.https_ip_url()).expect("provider config");
    mismatch_config
        .additional_root_certificates
        .push(mismatch_server.tls_certificate.clone().expect("TLS root"));
    let mismatch_client = ProviderHttpClient::new(mismatch_config).expect("provider client");
    assert_eq!(
        mismatch_client
            .send(
                Method::GET,
                "/hostname-mismatch",
                HeaderMap::new(),
                Bytes::new()
            )
            .await
            .expect_err("hostname verification failure"),
        TransportError::Tls
    );
}

#[test]
fn transport_error_display_is_secret_free_and_uri_validation_is_fail_closed() {
    let marker = "provider-api-key-unique-marker";
    let error = TransportError::Connect;
    assert!(!error.to_string().contains(marker));
    assert!(ProviderHttpConfig::new(&format!("https://user:{marker}@provider.example")).is_err());
}
