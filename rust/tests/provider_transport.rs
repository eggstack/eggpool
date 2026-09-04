use std::{
    collections::BTreeMap,
    io::{Read, Write},
    net::{Shutdown, SocketAddr},
    net::{TcpListener, TcpStream},
    sync::{
        Arc, Mutex,
        atomic::{AtomicUsize, Ordering},
    },
    thread,
    time::{Duration, Instant},
};

use bytes::Bytes;
use eggpool::{
    Config, db,
    providers::{
        ProviderClientPool, ProviderClientPoolError, ProviderHttpClient, ProviderHttpConfig,
        TransportError,
    },
    server,
};
use http::{HeaderMap, HeaderValue, Method};
use rustls::{
    ServerConfig, ServerConnection,
    pki_types::{CertificateDer, PrivateKeyDer},
};
use serde_json::json;

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
    Redirect,
    SlowHeaders,
    Premature,
}

#[derive(Clone, Copy)]
enum ProxyMode {
    HttpConnect { authenticated: bool },
    Socks4,
    Socks5 { authenticated: bool },
}

struct ProxyFixture {
    address: SocketAddr,
    targets: Arc<Mutex<Vec<String>>>,
    connect_headers: Arc<Mutex<Vec<String>>>,
    thread: Option<thread::JoinHandle<()>>,
}

impl ProxyFixture {
    fn http_connect(authenticated: bool, expected_connections: usize) -> Self {
        Self::start(
            ProxyMode::HttpConnect { authenticated },
            expected_connections,
        )
    }

    fn socks5(authenticated: bool, expected_connections: usize) -> Self {
        Self::start(ProxyMode::Socks5 { authenticated }, expected_connections)
    }

    fn socks4(expected_connections: usize) -> Self {
        Self::start(ProxyMode::Socks4, expected_connections)
    }

    fn start(mode: ProxyMode, expected_connections: usize) -> Self {
        let listener = TcpListener::bind(("127.0.0.1", 0)).expect("proxy listener");
        let address = listener.local_addr().expect("proxy address");
        let targets = Arc::new(Mutex::new(Vec::new()));
        let connect_headers = Arc::new(Mutex::new(Vec::new()));
        let target_log = Arc::clone(&targets);
        let header_log = Arc::clone(&connect_headers);
        let thread = thread::spawn(move || {
            for _ in 0..expected_connections {
                let (stream, _) = listener.accept().expect("proxy accept");
                handle_proxy_connection(stream, mode, &target_log, &header_log);
            }
        });
        Self {
            address,
            targets,
            connect_headers,
            thread: Some(thread),
        }
    }

    fn uri(&self) -> String {
        format!("127.0.0.1:{}", self.address.port())
    }

    fn targets(&self) -> Vec<String> {
        self.targets.lock().unwrap().clone()
    }

    fn connect_headers(&self) -> Vec<String> {
        self.connect_headers.lock().unwrap().clone()
    }
}

impl Drop for ProxyFixture {
    fn drop(&mut self) {
        if let Some(thread) = self.thread.take() {
            thread.join().expect("proxy thread");
        }
    }
}

fn read_exact_bytes(stream: &mut TcpStream, length: usize) -> Option<Vec<u8>> {
    let mut bytes = vec![0; length];
    stream.read_exact(&mut bytes).ok()?;
    Some(bytes)
}

fn read_until_headers(stream: &mut TcpStream) -> Option<Vec<u8>> {
    let mut head = Vec::new();
    let mut byte = [0; 1];
    loop {
        stream.read_exact(&mut byte).ok()?;
        head.push(byte[0]);
        if head.ends_with(b"\r\n\r\n") {
            return Some(head);
        }
        if head.len() > 64 * 1024 {
            return None;
        }
    }
}

fn parse_target_authority(value: &str) -> Option<(String, u16)> {
    let (host, port) = value.rsplit_once(':')?;
    Some((host.trim_matches(['[', ']']).to_owned(), port.parse().ok()?))
}

fn handle_proxy_connection(
    mut client: TcpStream,
    mode: ProxyMode,
    targets: &Arc<Mutex<Vec<String>>>,
    connect_headers: &Arc<Mutex<Vec<String>>>,
) {
    let (host, port) = match mode {
        ProxyMode::HttpConnect { authenticated } => {
            let Some(head) = read_until_headers(&mut client) else {
                return;
            };
            let text = String::from_utf8_lossy(&head);
            connect_headers
                .lock()
                .unwrap()
                .extend(text.lines().skip(1).filter_map(|line| {
                    line.split_once(':')
                        .map(|(name, _)| name.to_ascii_lowercase())
                }));
            let mut first = text.lines().next().unwrap_or_default().split_whitespace();
            if first.next() != Some("CONNECT") {
                return;
            }
            let authority = first.next().unwrap_or_default();
            if authenticated
                && !text.lines().any(|line| {
                    line.eq_ignore_ascii_case(
                        "Proxy-Authorization: Basic cHJveHktdXNlcjpwcm94eS1wYXNz",
                    )
                })
            {
                client
                    .write_all(
                        b"HTTP/1.1 407 Proxy Authentication Required\r\nConnection: close\r\n\r\n",
                    )
                    .expect("proxy auth response");
                return;
            }
            let Some(target) = parse_target_authority(authority) else {
                return;
            };
            targets.lock().unwrap().push(authority.to_owned());
            client
                .write_all(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                .expect("proxy connect response");
            target
        }
        ProxyMode::Socks4 => {
            let Some(request) = read_exact_bytes(&mut client, 8) else {
                return;
            };
            if request[0] != 4 || request[1] != 1 {
                return;
            }
            let port = u16::from_be_bytes([request[2], request[3]]);
            let host = if request[4..8] == [0, 0, 0, 1] {
                let mut raw = Vec::new();
                loop {
                    let Some(byte) = read_exact_bytes(&mut client, 1) else {
                        return;
                    };
                    if byte[0] == 0 {
                        break;
                    }
                }
                loop {
                    let Some(byte) = read_exact_bytes(&mut client, 1) else {
                        return;
                    };
                    if byte[0] == 0 {
                        break;
                    }
                    raw.push(byte[0]);
                }
                let Ok(host) = String::from_utf8(raw) else {
                    return;
                };
                host
            } else {
                format!(
                    "{}.{}.{}.{}",
                    request[4], request[5], request[6], request[7]
                )
            };
            targets.lock().unwrap().push(format!("{host}:{port}"));
            client
                .write_all(&[
                    0, 90, request[2], request[3], request[4], request[5], request[6], request[7],
                ])
                .expect("SOCKS4 connect response");
            (host, port)
        }
        ProxyMode::Socks5 { authenticated } => {
            let Some(header) = read_exact_bytes(&mut client, 2) else {
                return;
            };
            let Some(_methods) = read_exact_bytes(&mut client, header[1] as usize) else {
                return;
            };
            let method = if authenticated { 2 } else { 0 };
            client
                .write_all(&[5, method])
                .expect("SOCKS method response");
            if authenticated {
                let Some(auth_head) = read_exact_bytes(&mut client, 2) else {
                    return;
                };
                let Some(_user) = read_exact_bytes(&mut client, auth_head[1] as usize) else {
                    return;
                };
                let Some(password_length) = read_exact_bytes(&mut client, 1) else {
                    return;
                };
                let Some(_password) = read_exact_bytes(&mut client, password_length[0] as usize)
                else {
                    return;
                };
                client.write_all(&[1, 0]).expect("SOCKS auth response");
            }
            let Some(request) = read_exact_bytes(&mut client, 4) else {
                return;
            };
            if request[0] != 5 || request[1] != 1 {
                return;
            }
            let (host, target_label) = match request[3] {
                1 => {
                    let Some(raw) = read_exact_bytes(&mut client, 4) else {
                        return;
                    };
                    let host = format!("{}.{}.{}.{}", raw[0], raw[1], raw[2], raw[3]);
                    (host.clone(), host)
                }
                3 => {
                    let Some(length) = read_exact_bytes(&mut client, 1) else {
                        return;
                    };
                    let Some(raw) = read_exact_bytes(&mut client, length[0] as usize) else {
                        return;
                    };
                    let Ok(host) = String::from_utf8(raw) else {
                        return;
                    };
                    (host.clone(), host)
                }
                _ => return,
            };
            let Some(raw_port) = read_exact_bytes(&mut client, 2) else {
                return;
            };
            let port = u16::from_be_bytes([raw_port[0], raw_port[1]]);
            targets.lock().unwrap().push(format!(
                "{}:{}",
                if request[3] == 3 { "domain" } else { "ip" },
                target_label
            ));
            client
                .write_all(&[5, 0, 0, 1, 127, 0, 0, 1, 0, 0])
                .expect("SOCKS connect response");
            (host, port)
        }
    };

    let Ok(target) = TcpStream::connect((host.as_str(), port)) else {
        return;
    };
    relay_streams(client, target);
}

fn relay_streams(mut client: TcpStream, mut target: TcpStream) {
    let mut reverse_client = client.try_clone().expect("proxy client clone");
    let mut reverse_target = target.try_clone().expect("proxy target clone");
    let to_target = thread::spawn(move || {
        let _ = std::io::copy(&mut client, &mut target);
    });
    let _ = std::io::copy(&mut reverse_target, &mut reverse_client);
    let _ = reverse_client.shutdown(Shutdown::Both);
    let _ = reverse_target.shutdown(Shutdown::Both);
    to_target.join().expect("proxy relay thread");
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

    fn port(&self) -> u16 {
        self.address.rsplit(':').next().unwrap().parse().unwrap()
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
        ResponseMode::Redirect => {
            stream
                .write_all(
                    b"HTTP/1.1 302 Found\r\nlocation: /redirected\r\ncontent-length: 0\r\nconnection: close\r\n\r\n",
                )
                .unwrap();
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
    assert_eq!(first.headers["content-length"], "2");
}

#[tokio::test(flavor = "current_thread")]
async fn direct_http_returns_redirect_without_following_or_retrying() {
    let server = FixtureServer::http(ResponseMode::Redirect, 1);
    let client = make_client(&server.http_url());
    let response = client
        .send(Method::GET, "/original", HeaderMap::new(), Bytes::new())
        .await
        .expect("redirect response");

    assert_eq!(response.status.as_u16(), 302);
    assert_eq!(response.headers["location"], "/redirected");
    assert_eq!(server.requests().len(), 1);
    assert_eq!(server.requests()[0].target, "/original");
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

fn proxy_test_config(base_url: &str) -> ProviderHttpConfig {
    let mut config = ProviderHttpConfig::new(base_url).expect("provider config");
    config.connect_timeout = Duration::from_secs(2);
    config.read_timeout = Duration::from_secs(2);
    config.write_timeout = Duration::from_secs(2);
    config.pool_timeout = Duration::from_secs(1);
    config
}

fn provider_config(
    id: &str,
    base_url: &str,
    accounts: Vec<eggpool::config::AccountConfig>,
) -> eggpool::config::ProviderConfig {
    eggpool::config::ProviderConfig {
        id: id.to_owned(),
        base_url: base_url.to_owned(),
        accounts,
        ..Default::default()
    }
}

fn config_with_providers(providers: Vec<eggpool::config::ProviderConfig>) -> Config {
    Config {
        providers: providers
            .into_iter()
            .map(|provider| (provider.id.clone(), provider))
            .collect::<BTreeMap<_, _>>(),
        ..Config::default()
    }
}

#[test]
fn provider_client_pool_builds_empty_and_provider_topologies() {
    let empty = ProviderClientPool::from_config(&Config::default()).expect("empty pool");
    assert_eq!(empty.providers(), Vec::<String>::new());
    assert_eq!(
        serde_json::to_value(empty.snapshot()).expect("snapshot JSON"),
        json!({
            "build_count": 0,
            "providers": {},
            "account_client_count": 0,
            "account_clients": [],
        })
    );

    let config = config_with_providers(vec![
        provider_config("alpha", "http://alpha.example", Vec::new()),
        provider_config("beta", "https://beta.example", Vec::new()),
    ]);
    let pool = ProviderClientPool::from_config(&config).expect("provider pool");
    assert_eq!(pool.providers(), vec!["alpha", "beta"]);
    assert!(pool.get_client("alpha", None).is_ok());
    assert_eq!(pool.snapshot().build_count, 2);
}

#[test]
fn provider_client_pool_uses_direct_fallback_and_dedicated_proxy_clients() {
    let mut direct = eggpool::config::AccountConfig {
        name: "direct".to_owned(),
        ..Default::default()
    };
    direct.api_key_env = "DIRECT_KEY".to_owned();
    let proxied = eggpool::config::AccountConfig {
        name: "proxied".to_owned(),
        proxy_url: Some("direct://".to_owned()),
        ..Default::default()
    };
    let config = config_with_providers(vec![provider_config(
        "provider",
        "http://provider.example",
        vec![direct, proxied],
    )]);
    let pool = ProviderClientPool::from_config(&config).expect("provider pool");
    let provider_client = pool.get_client("provider", None).expect("provider client");
    assert_eq!(
        pool.get_client("provider", Some("direct"))
            .expect("direct fallback")
            .base_url(),
        provider_client.base_url()
    );
    assert!(pool.get_client("provider", Some("proxied")).is_ok());
    assert_eq!(
        serde_json::to_value(pool.snapshot()).expect("snapshot JSON"),
        json!({
            "build_count": 2,
            "providers": {"provider": 2},
            "account_client_count": 1,
            "account_clients": [{"provider_id": "provider", "account_name": "proxied"}],
        })
    );
    assert_eq!(pool.snapshot().build_count, 2);
    assert_eq!(pool.snapshot().build_count, 2);
}

#[test]
fn provider_client_pool_fails_closed_for_missing_and_malformed_clients() {
    let pool = ProviderClientPool::default();
    assert_eq!(
        pool.get_client("missing", None)
            .expect_err("missing provider"),
        ProviderClientPoolError::ProviderNotFound {
            provider_id: "missing".to_owned(),
        }
    );

    let marker = "t004-proxy-secret-marker";
    let config = config_with_providers(vec![provider_config(
        "provider",
        "http://provider.example",
        vec![eggpool::config::AccountConfig {
            name: "proxied".to_owned(),
            proxy_url: Some(format!("not-a-proxy://{marker}")),
            ..Default::default()
        }],
    )]);
    let error = ProviderClientPool::from_config(&config).expect_err("malformed proxy");
    assert!(matches!(
        error,
        ProviderClientPoolError::AccountTransport {
            kind: TransportError::ProxyConfiguration,
            ..
        }
    ));
    assert!(!error.to_string().contains(marker));
    assert!(!format!("{error:?}").contains(marker));
}

#[tokio::test(flavor = "current_thread")]
async fn proxied_accounts_keep_separate_pools_even_with_identical_proxy_uris() {
    let server = FixtureServer::http(ResponseMode::Normal, 2);
    let proxy = ProxyFixture::http_connect(false, 2);
    let proxy_url = format!("http://{}", proxy.uri());
    let config = config_with_providers(vec![provider_config(
        "provider",
        &server.http_url(),
        vec![
            eggpool::config::AccountConfig {
                name: "first".to_owned(),
                proxy_url: Some(proxy_url.clone()),
                ..Default::default()
            },
            eggpool::config::AccountConfig {
                name: "second".to_owned(),
                proxy_url: Some(proxy_url),
                ..Default::default()
            },
        ],
    )]);
    let pool = ProviderClientPool::from_config(&config).expect("provider pool");
    let first = pool
        .get_client("provider", Some("first"))
        .expect("first client");
    let second = pool
        .get_client("provider", Some("second"))
        .expect("second client");
    let mut first_response = first
        .send(Method::GET, "/first", HeaderMap::new(), Bytes::new())
        .await
        .expect("first response");
    assert!(first_response.body.next().await.is_some());
    let mut second_response = second
        .send(Method::GET, "/second", HeaderMap::new(), Bytes::new())
        .await
        .expect("second response");
    assert!(second_response.body.next().await.is_some());
    drop(first_response);
    drop(second_response);
    drop(pool);

    assert_eq!(server.connections.load(Ordering::SeqCst), 2);
    assert_eq!(proxy.targets().len(), 2);
}

#[tokio::test(flavor = "current_thread")]
async fn failed_pool_build_after_bind_closes_database_and_releases_listener() {
    let reserved = TcpListener::bind(("127.0.0.1", 0)).expect("reserve port");
    let port = reserved.local_addr().expect("reserved address").port();
    drop(reserved);
    let marker = "t004-server-proxy-secret-marker";
    let database_path = std::env::temp_dir().join(format!(
        "eggpool-t004-{}-{port}.sqlite3",
        std::process::id()
    ));
    let _ = std::fs::remove_file(&database_path);

    let config = Config {
        server: eggpool::config::ServerConfig {
            port,
            ..Default::default()
        },
        database: eggpool::config::DatabaseConfig {
            path: database_path.to_string_lossy().into_owned(),
            ..Default::default()
        },
        providers: [(
            "provider".to_owned(),
            provider_config(
                "provider",
                "http://provider.example",
                vec![eggpool::config::AccountConfig {
                    name: "broken".to_owned(),
                    proxy_url: Some(format!("not-a-proxy://{marker}")),
                    ..Default::default()
                }],
            ),
        )]
        .into_iter()
        .collect(),
        ..Config::default()
    };
    let error = server::run(config)
        .await
        .expect_err("pool construction failure");
    assert!(matches!(error, server::ServerError::ProviderPool(_)));
    assert!(!error.to_string().contains(marker));

    let reusable = TcpListener::bind(("127.0.0.1", port)).expect("listener was released");
    drop(reusable);
    let database = db::Database::open(db::DatabaseConfig {
        path: database_path.to_string_lossy().into_owned(),
        ..Default::default()
    })
    .await
    .expect("database was closed cleanly");
    database.close().await.expect("database close");
    std::fs::remove_file(database_path).expect("test database cleanup");
}

#[test]
fn mandatory_proxy_corpus_uri_families_construct() {
    let port = 1;
    let uris = [
        "direct://".to_owned(),
        format!("http://127.0.0.1:{port}"),
        format!("http://127.0.0.1:{port}#proxy-user:proxy-pass"),
        format!("socks4://127.0.0.1:{port}"),
        format!("socks5://127.0.0.1:{port}"),
        format!("socks5://127.0.0.1:{port}#proxy-user:proxy-pass"),
        format!("http://127.0.0.1:{port}__socks5://127.0.0.1:{port}"),
        format!("ss://aes-256-gcm:synthetic-key@127.0.0.1:{port}"),
        format!("ssr://aes-256-cfb:synthetic-key@127.0.0.1:{port}"),
        format!("trojan://aes-256-gcm:synthetic-key@127.0.0.1:{port}"),
        format!("ssh://aes-256-cfb:synthetic-key@127.0.0.1:{port}"),
    ];
    for uri in uris {
        let result =
            ProviderHttpClient::new_with_proxy(proxy_test_config("http://127.0.0.1:1"), &uri);
        assert!(
            result.is_ok(),
            "mandatory proxy URI did not construct: {uri}"
        );
    }
}

#[tokio::test(flavor = "current_thread")]
async fn direct_eggress_control_uri_uses_the_same_http_client() {
    let server = FixtureServer::http(ResponseMode::Normal, 1);
    let client =
        ProviderHttpClient::new_with_proxy(proxy_test_config(&server.http_url()), "direct://")
            .expect("direct Eggress adapter");
    let mut response = client
        .send(Method::GET, "/control", HeaderMap::new(), Bytes::new())
        .await
        .expect("direct Eggress response");
    assert_eq!(
        response.body.next().await.unwrap().unwrap(),
        Bytes::from_static(b"ok")
    );
}

#[tokio::test(flavor = "current_thread")]
async fn http_connect_proxy_preserves_target_and_request_response() {
    let server = FixtureServer::http(ResponseMode::Normal, 1);
    let proxy = ProxyFixture::http_connect(false, 1);
    let client = ProviderHttpClient::new_with_proxy(
        proxy_test_config(&server.http_url()),
        &format!("http://{}", proxy.uri()),
    )
    .expect("HTTP CONNECT client");
    let mut response = client
        .send(Method::GET, "/through", HeaderMap::new(), Bytes::new())
        .await
        .expect("proxied response");
    assert_eq!(response.status.as_u16(), 200);
    assert_eq!(
        response.body.next().await.unwrap().unwrap(),
        Bytes::from_static(b"ok")
    );
    assert_eq!(
        proxy.targets(),
        vec![format!("localhost:{}", server.port())]
    );
}

#[tokio::test(flavor = "current_thread")]
async fn socks4_proxy_preserves_target_and_request_response() {
    let server = FixtureServer::http(ResponseMode::Normal, 1);
    let proxy = ProxyFixture::socks4(1);
    let client = ProviderHttpClient::new_with_proxy(
        proxy_test_config(&server.http_url()),
        &format!("socks4://{}", proxy.uri()),
    )
    .expect("SOCKS4 client");
    let mut response = client
        .send(Method::GET, "/through", HeaderMap::new(), Bytes::new())
        .await
        .expect("SOCKS4 response");

    assert_eq!(response.status.as_u16(), 200);
    assert_eq!(
        response.body.next().await.unwrap().unwrap(),
        Bytes::from_static(b"ok")
    );
    assert_eq!(
        proxy.targets(),
        vec![format!("localhost:{}", server.port())]
    );
}

#[tokio::test(flavor = "current_thread")]
async fn proxied_http_reuses_one_tunneled_connection() {
    let server = FixtureServer::http(ResponseMode::Normal, 2);
    let proxy = ProxyFixture::http_connect(false, 1);
    let client = ProviderHttpClient::new_with_proxy(
        proxy_test_config(&server.http_url()),
        &format!("http://{}", proxy.uri()),
    )
    .expect("HTTP CONNECT client");

    for path in ["/first", "/second"] {
        let mut response = client
            .send(Method::GET, path, HeaderMap::new(), Bytes::new())
            .await
            .expect("proxied response");
        assert_eq!(response.status.as_u16(), 200);
        assert_eq!(
            response.body.next().await.unwrap().unwrap(),
            Bytes::from_static(b"ok")
        );
        assert!(response.body.next().await.is_none());
    }

    assert_eq!(
        proxy.targets(),
        vec![format!("localhost:{}", server.port())]
    );
    assert_eq!(server.connections.load(Ordering::SeqCst), 1);
    assert_eq!(server.requests().len(), 2);
}

#[tokio::test(flavor = "current_thread")]
async fn authenticated_http_connect_preserves_provider_tls_verification() {
    let server = FixtureServer::https(1);
    let ca = server.tls_certificate.clone().expect("TLS root");
    let proxy = ProxyFixture::http_connect(true, 1);
    let mut config = proxy_test_config(&server.https_url());
    config.additional_root_certificates.push(ca);
    let client = ProviderHttpClient::new_with_proxy(
        config,
        &format!("http://{}#proxy-user:proxy-pass", proxy.uri()),
    )
    .expect("authenticated HTTP CONNECT client");
    let mut headers = HeaderMap::new();
    headers.insert(
        "authorization",
        HeaderValue::from_static("provider-secret-marker"),
    );
    let mut response = client
        .send(Method::GET, "/secure", headers, Bytes::new())
        .await
        .expect("proxied TLS response");
    assert_eq!(
        response.body.next().await.unwrap().unwrap(),
        Bytes::from_static(b"ok")
    );
    assert_eq!(
        proxy.targets(),
        vec![format!("localhost:{}", server.port())]
    );
    assert!(
        !proxy
            .connect_headers()
            .iter()
            .any(|name| name == "authorization")
    );
}

#[tokio::test(flavor = "current_thread")]
async fn http_then_socks5_chain_preserves_both_hops() {
    let server = FixtureServer::http(ResponseMode::Normal, 1);
    let first = ProxyFixture::http_connect(false, 1);
    let second = ProxyFixture::socks5(false, 1);
    let chain = format!("http://{}__socks5://{}", first.uri(), second.uri());
    let client = ProviderHttpClient::new_with_proxy(proxy_test_config(&server.http_url()), &chain)
        .expect("proxy chain client");
    let mut response = client
        .send(Method::GET, "/chained", HeaderMap::new(), Bytes::new())
        .await
        .expect("chained response");
    assert_eq!(
        response.body.next().await.unwrap().unwrap(),
        Bytes::from_static(b"ok")
    );
    assert_eq!(first.targets(), vec![second.uri()]);
    assert_eq!(second.targets(), vec!["domain:localhost".to_owned()]);
}

#[tokio::test(flavor = "current_thread")]
async fn socks5_auth_preserves_domain_target_and_rejection_is_not_direct() {
    let server = FixtureServer::http(ResponseMode::Normal, 1);
    let proxy = ProxyFixture::socks5(true, 1);
    let client = ProviderHttpClient::new_with_proxy(
        proxy_test_config(&server.http_url()),
        &format!("socks5://{}#proxy-user:proxy-pass", proxy.uri()),
    )
    .expect("authenticated SOCKS5 client");
    let mut response = client
        .send(Method::GET, "/through", HeaderMap::new(), Bytes::new())
        .await
        .expect("SOCKS5 response");
    assert_eq!(
        response.body.next().await.unwrap().unwrap(),
        Bytes::from_static(b"ok")
    );
    assert_eq!(proxy.targets(), vec!["domain:localhost".to_owned()]);

    let rejecting_proxy = ProxyFixture::http_connect(true, 1);
    let rejecting_client = ProviderHttpClient::new_with_proxy(
        proxy_test_config(&server.http_url()),
        &format!("http://{}", rejecting_proxy.uri()),
    )
    .expect("proxy client");
    let error = rejecting_client
        .send(
            Method::GET,
            "/must-not-be-direct",
            HeaderMap::new(),
            Bytes::new(),
        )
        .await
        .expect_err("proxy authentication rejection");
    assert!(matches!(
        error,
        TransportError::ProxyAuthentication | TransportError::ProxyConnect
    ));
    assert!(rejecting_proxy.targets().is_empty());
    assert_eq!(server.requests().len(), 1);
}

#[tokio::test(flavor = "current_thread")]
async fn identical_proxy_endpoints_keep_account_pools_isolated() {
    let server = FixtureServer::http(ResponseMode::Normal, 2);
    let proxy = ProxyFixture::http_connect(false, 2);
    let proxy_url = format!("http://{}", proxy.uri());
    {
        let client =
            ProviderHttpClient::new_with_proxy(proxy_test_config(&server.http_url()), &proxy_url)
                .expect("first account client");
        let mut response = client
            .send(Method::GET, "/one", HeaderMap::new(), Bytes::new())
            .await
            .expect("first account response");
        response
            .body
            .next()
            .await
            .expect("first body")
            .expect("body");
    }
    {
        let client =
            ProviderHttpClient::new_with_proxy(proxy_test_config(&server.http_url()), &proxy_url)
                .expect("second account client");
        let mut response = client
            .send(Method::GET, "/two", HeaderMap::new(), Bytes::new())
            .await
            .expect("second account response");
        response
            .body
            .next()
            .await
            .expect("second body")
            .expect("body");
    }
    assert_eq!(proxy.targets().len(), 2);
    assert_eq!(server.connections.load(Ordering::SeqCst), 2);
}

#[test]
fn malformed_proxy_fails_closed_without_secret_bearing_diagnostics() {
    let marker = "proxy-secret-marker-t003";
    let result = ProviderHttpClient::new_with_proxy(
        proxy_test_config("http://127.0.0.1:1"),
        &format!("unsupported://{marker}@127.0.0.1:1"),
    );
    let error = result.expect_err("unsupported proxy must fail construction");
    assert_eq!(error, TransportError::ProxyConfiguration);
    assert!(!error.to_string().contains(marker));
    assert!(!format!("{error:?}").contains(marker));
}
