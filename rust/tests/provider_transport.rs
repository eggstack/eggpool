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

#[cfg(feature = "test-support")]
use std::{
    path::PathBuf,
    process::{Child, Command, Stdio},
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
use eggress_core::{BoxStream, TargetHost};
use eggress_protocol_shadowsocks::{CipherMethod, compat::ssr::SsrConfig};
use http::{HeaderMap, HeaderValue, Method};
use rustls::{
    ServerConfig, ServerConnection,
    pki_types::{CertificateDer, PrivateKeyDer},
};
use serde_json::json;
#[cfg(feature = "test-support")]
use tempfile::TempDir;

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
        listener
            .set_nonblocking(true)
            .expect("proxy listener nonblocking");
        let address = listener.local_addr().expect("proxy address");
        let targets = Arc::new(Mutex::new(Vec::new()));
        let connect_headers = Arc::new(Mutex::new(Vec::new()));
        let target_log = Arc::clone(&targets);
        let header_log = Arc::clone(&connect_headers);
        let thread = thread::spawn(move || {
            let mut workers = Vec::with_capacity(expected_connections);
            for _ in 0..expected_connections {
                let deadline = Instant::now() + Duration::from_secs(2);
                let (stream, _) = loop {
                    match listener.accept() {
                        Ok(connection) => break connection,
                        Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                            if Instant::now() >= deadline {
                                return;
                            }
                            thread::sleep(Duration::from_millis(5));
                        }
                        Err(error) => panic!("proxy accept: {error}"),
                    }
                };
                stream
                    .set_nonblocking(false)
                    .expect("proxy stream blocking mode");
                let target_log = Arc::clone(&target_log);
                let header_log = Arc::clone(&header_log);
                workers.push(thread::spawn(move || {
                    handle_proxy_connection(stream, mode, &target_log, &header_log);
                }));
            }
            for worker in workers {
                worker.join().expect("proxy worker");
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

#[derive(Clone, Copy)]
enum EncryptedProxyKind {
    Shadowsocks,
    ShadowsocksR,
}

struct EncryptedProxyFixture {
    address: SocketAddr,
    targets: Arc<Mutex<Vec<String>>>,
    handshakes: Arc<AtomicUsize>,
    task: Option<tokio::task::JoinHandle<()>>,
}

impl EncryptedProxyFixture {
    fn start(kind: EncryptedProxyKind, password: &'static str, expected: usize) -> Self {
        Self::start_with_delay(kind, password, expected, Duration::ZERO, false)
    }

    fn start_with_delay(
        kind: EncryptedProxyKind,
        password: &'static str,
        expected: usize,
        handshake_delay: Duration,
        discard_first: bool,
    ) -> Self {
        let listener = TcpListener::bind(("127.0.0.1", 0)).expect("encrypted proxy listener");
        let address = listener.local_addr().expect("encrypted proxy address");
        listener
            .set_nonblocking(true)
            .expect("nonblocking encrypted proxy listener");
        let listener =
            tokio::net::TcpListener::from_std(listener).expect("tokio encrypted proxy listener");
        let targets = Arc::new(Mutex::new(Vec::new()));
        let handshakes = Arc::new(AtomicUsize::new(0));
        let target_log = Arc::clone(&targets);
        let handshake_log = Arc::clone(&handshakes);
        let task = tokio::spawn(async move {
            for connection_index in 0..expected {
                let Ok((stream, _)) = listener.accept().await else {
                    return;
                };
                tokio::time::sleep(handshake_delay).await;
                let boxed: BoxStream = Box::new(stream);
                let accepted = match kind {
                    EncryptedProxyKind::Shadowsocks => {
                        eggress_protocol_shadowsocks::shadowsocks_accept(
                            boxed,
                            password,
                            CipherMethod::Aes256Gcm,
                            None,
                        )
                        .await
                    }
                    EncryptedProxyKind::ShadowsocksR => {
                        eggress_protocol_shadowsocks::compat::ssr::ssr_accept(
                            boxed,
                            &SsrConfig::default(),
                        )
                        .await
                    }
                };
                let Ok((mut proxy_stream, target)) = accepted else {
                    continue;
                };
                handshake_log.fetch_add(1, Ordering::SeqCst);
                let authority = match &target.host {
                    TargetHost::Ip(ip) => format!("{}:{}", ip, target.port),
                    TargetHost::Domain(host) => format!("{}:{}", host, target.port),
                };
                target_log.lock().unwrap().push(authority);
                if discard_first && connection_index == 0 {
                    continue;
                }
                let target_stream = match &target.host {
                    TargetHost::Ip(ip) => tokio::net::TcpStream::connect((*ip, target.port)).await,
                    TargetHost::Domain(host) => {
                        tokio::net::TcpStream::connect((host.as_str(), target.port)).await
                    }
                };
                let Ok(mut target_stream) = target_stream else {
                    continue;
                };
                let _ = tokio::io::copy_bidirectional(&mut proxy_stream, &mut target_stream).await;
            }
        });
        Self {
            address,
            targets,
            handshakes,
            task: Some(task),
        }
    }

    fn uri(&self, kind: EncryptedProxyKind, password: &str) -> String {
        let scheme = match kind {
            EncryptedProxyKind::Shadowsocks => "ss://aes-256-gcm",
            EncryptedProxyKind::ShadowsocksR => "ssr://aes-256-cfb",
        };
        format!("{scheme}:{password}@127.0.0.1:{}", self.address.port())
    }

    fn targets(&self) -> Vec<String> {
        self.targets.lock().unwrap().clone()
    }

    fn handshake_count(&self) -> usize {
        self.handshakes.load(Ordering::SeqCst)
    }

    async fn shutdown(&mut self) {
        if let Some(task) = self.task.take() {
            task.abort();
            let _ = task.await;
        }
    }
}

impl Drop for EncryptedProxyFixture {
    fn drop(&mut self) {
        if let Some(task) = self.task.take() {
            task.abort();
        }
    }
}

#[cfg(feature = "test-support")]
struct TrojanProxyFixture {
    address: SocketAddr,
    ca_certificate: Vec<u8>,
    targets: Arc<Mutex<Vec<String>>>,
    task: Option<tokio::task::JoinHandle<()>>,
}

#[cfg(feature = "test-support")]
impl TrojanProxyFixture {
    async fn start(password: &'static str, expected: usize) -> Self {
        let listener = tokio::net::TcpListener::bind(("127.0.0.1", 0))
            .await
            .expect("Trojan proxy listener");
        let address = listener.local_addr().expect("Trojan proxy address");
        let material = make_tls_material_for_names(["localhost", "127.0.0.1"]);
        let ca_certificate = material.2.clone();
        let acceptor = tokio_rustls::TlsAcceptor::from(server_tls_config(&material));
        let targets = Arc::new(Mutex::new(Vec::new()));
        let target_log = Arc::clone(&targets);
        let task = tokio::spawn(async move {
            for _ in 0..expected {
                let Ok((stream, _)) = listener.accept().await else {
                    return;
                };
                let Ok(stream) = acceptor.accept(stream).await else {
                    continue;
                };
                let boxed: BoxStream = Box::new(stream);
                let Ok((mut proxy_stream, accepted)) =
                    eggress_protocol_trojan::trojan_accept(boxed, password).await
                else {
                    continue;
                };
                let target = accepted.target;
                let authority = match &target.host {
                    TargetHost::Ip(ip) => format!("{}:{}", ip, target.port),
                    TargetHost::Domain(host) => format!("{}:{}", host, target.port),
                };
                target_log.lock().unwrap().push(authority);
                let target_stream = match &target.host {
                    TargetHost::Ip(ip) => tokio::net::TcpStream::connect((*ip, target.port)).await,
                    TargetHost::Domain(host) => {
                        tokio::net::TcpStream::connect((host.as_str(), target.port)).await
                    }
                };
                let Ok(mut target_stream) = target_stream else {
                    continue;
                };
                let _ = tokio::io::copy_bidirectional(&mut proxy_stream, &mut target_stream).await;
            }
        });
        Self {
            address,
            ca_certificate,
            targets,
            task: Some(task),
        }
    }

    fn uri(&self, password: &str) -> String {
        format!(
            "trojan://aes-256-gcm:{password}@127.0.0.1:{}",
            self.address.port()
        )
    }

    fn targets(&self) -> Vec<String> {
        self.targets.lock().unwrap().clone()
    }

    async fn shutdown(&mut self) {
        if let Some(task) = self.task.take() {
            task.abort();
            let _ = task.await;
        }
    }
}

#[cfg(feature = "test-support")]
impl Drop for TrojanProxyFixture {
    fn drop(&mut self) {
        if let Some(task) = self.task.take() {
            task.abort();
        }
    }
}

#[cfg(feature = "test-support")]
struct SshProxyFixture {
    _directory: TempDir,
    child: Child,
    address: SocketAddr,
    user: String,
    private_key: PathBuf,
    wrong_private_key: PathBuf,
}

#[cfg(feature = "test-support")]
impl SshProxyFixture {
    async fn start() -> Self {
        assert!(
            command_available("sshd"),
            "sshd is required for SSH fixture"
        );
        assert!(
            command_available("ssh-keygen"),
            "ssh-keygen is required for SSH fixture"
        );
        let directory = tempfile::tempdir().expect("SSH fixture directory");
        let host_key = directory.path().join("host_key");
        let private_key = directory.path().join("client_key");
        let wrong_private_key = directory.path().join("wrong_client_key");
        run_checked(
            Command::new("ssh-keygen")
                .args(["-q", "-t", "ed25519", "-N", "", "-f"])
                .arg(&host_key),
        )
        .expect("SSH host key");
        run_checked(
            Command::new("ssh-keygen")
                .args(["-q", "-t", "ed25519", "-N", "", "-f"])
                .arg(&private_key),
        )
        .expect("SSH client key");
        run_checked(
            Command::new("ssh-keygen")
                .args(["-q", "-t", "ed25519", "-N", "", "-f"])
                .arg(&wrong_private_key),
        )
        .expect("SSH wrong client key");
        std::fs::copy(
            private_key.with_extension("pub"),
            directory.path().join("authorized_keys"),
        )
        .expect("SSH authorized keys");
        let user = std::env::var("USER")
            .ok()
            .filter(|user| !user.is_empty())
            .expect("SSH fixture user");
        let port = TcpListener::bind(("127.0.0.1", 0))
            .expect("SSH fixture port")
            .local_addr()
            .expect("SSH fixture address")
            .port();
        let config = directory.path().join("sshd_config");
        let pid_file = directory.path().join("sshd.pid");
        let config_text = format!(
            "Port {port}\nListenAddress 127.0.0.1\nHostKey {}\nAuthorizedKeysFile {}\nPidFile {}\nPasswordAuthentication no\nKbdInteractiveAuthentication no\nChallengeResponseAuthentication no\nUsePAM no\nPermitRootLogin yes\nPubkeyAuthentication yes\nAllowTcpForwarding yes\nAllowStreamLocalForwarding yes\nGatewayPorts no\nStrictModes no\nUseDNS no\nLogLevel QUIET\n",
            host_key.display(),
            directory.path().join("authorized_keys").display(),
            pid_file.display(),
        );
        std::fs::write(&config, config_text).expect("SSH fixture config");
        let config_check = Command::new("/usr/sbin/sshd")
            .args(["-t", "-f"])
            .arg(&config)
            .output()
            .expect("check SSH fixture config");
        assert!(
            config_check.status.success(),
            "invalid SSH fixture config: {}",
            String::from_utf8_lossy(&config_check.stderr)
        );
        let child = Command::new("/usr/sbin/sshd")
            .args(["-D", "-e", "-f"])
            .arg(&config)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .expect("start SSH fixture");
        let mut fixture = Self {
            _directory: directory,
            child,
            address: SocketAddr::from(([127, 0, 0, 1], port)),
            user,
            private_key,
            wrong_private_key,
        };
        assert!(
            wait_for_process(&mut fixture.child).await,
            "SSH fixture did not start"
        );
        fixture
    }

    fn uri_for_key(&self, key: &std::path::Path) -> String {
        format!(
            "ssh://127.0.0.1:{}#{}::{}",
            self.address.port(),
            self.user,
            key.display()
        )
    }

    fn uri(&self) -> String {
        self.uri_for_key(&self.private_key)
    }

    fn wrong_uri(&self) -> String {
        self.uri_for_key(&self.wrong_private_key)
    }
}

#[cfg(feature = "test-support")]
impl Drop for SshProxyFixture {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

#[cfg(feature = "test-support")]
fn command_available(command: &str) -> bool {
    Command::new("sh")
        .args(["-c", &format!("command -v {command} >/dev/null 2>&1")])
        .status()
        .is_ok_and(|status| status.success())
}

#[cfg(feature = "test-support")]
fn run_checked(command: &mut Command) -> std::io::Result<()> {
    command.stdout(Stdio::null()).stderr(Stdio::null());
    let status = command.status()?;
    if status.success() {
        Ok(())
    } else {
        Err(std::io::Error::other("fixture command failed"))
    }
}

#[cfg(feature = "test-support")]
async fn wait_for_process(child: &mut Child) -> bool {
    for _ in 0..100 {
        if child
            .try_wait()
            .expect("check SSH fixture process")
            .is_some()
        {
            return false;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    true
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
        Self::start(mode, expected_requests, false, Duration::from_secs(2))
    }

    fn http_with_idle_timeout(
        mode: ResponseMode,
        expected_requests: usize,
        idle_timeout: Duration,
    ) -> Self {
        Self::start(mode, expected_requests, false, idle_timeout)
    }

    fn https(expected_requests: usize) -> Self {
        Self::start(
            ResponseMode::Normal,
            expected_requests,
            true,
            Duration::from_secs(2),
        )
    }

    fn start(
        mode: ResponseMode,
        expected_requests: usize,
        tls: bool,
        idle_timeout: Duration,
    ) -> Self {
        let listener = TcpListener::bind(("127.0.0.1", 0)).expect("fixture listener");
        listener
            .set_nonblocking(true)
            .expect("fixture listener nonblocking");
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
            let mut idle_deadline = Instant::now() + idle_timeout;
            while served < expected_requests {
                let (stream, _) = loop {
                    match listener.accept() {
                        Ok(connection) => break connection,
                        Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                            if Instant::now() >= idle_deadline {
                                return;
                            }
                            thread::sleep(Duration::from_millis(5));
                        }
                        Err(error) => panic!("fixture accept: {error}"),
                    }
                };
                idle_deadline = Instant::now() + idle_timeout;
                stream
                    .set_nonblocking(false)
                    .expect("fixture stream blocking mode");
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
    make_tls_material_for_names(["localhost"])
}

fn install_test_crypto_provider() {
    let _ = rustls::crypto::ring::default_provider().install_default();
}

fn make_tls_material_for_names<const N: usize>(names: [&str; N]) -> (Vec<u8>, Vec<u8>, Vec<u8>) {
    install_test_crypto_provider();
    let mut ca_params =
        rcgen::CertificateParams::new(vec!["provider-test-ca".to_owned()]).expect("CA parameters");
    ca_params.is_ca = rcgen::IsCa::Ca(rcgen::BasicConstraints::Unconstrained);
    let ca_key = rcgen::KeyPair::generate().expect("CA key");
    let ca_certificate = ca_params.self_signed(&ca_key).expect("CA certificate");
    let ca_issuer = rcgen::Issuer::new(ca_params, ca_key);
    let mut leaf_params =
        rcgen::CertificateParams::new(names.into_iter().map(str::to_owned).collect::<Vec<_>>())
            .expect("leaf parameters");
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
async fn extended_encrypted_proxies_reach_the_target_through_provider_transport() {
    install_test_crypto_provider();
    for kind in [
        EncryptedProxyKind::Shadowsocks,
        EncryptedProxyKind::ShadowsocksR,
    ] {
        let server = FixtureServer::http(ResponseMode::Normal, 1);
        let mut proxy = EncryptedProxyFixture::start(kind, "ss-secret-marker", 1);
        let client = ProviderHttpClient::new_with_proxy(
            proxy_test_config(&server.http_url()),
            &proxy.uri(kind, "ss-secret-marker"),
        )
        .expect("encrypted proxy client");
        let mut response = client
            .send(Method::GET, "/encrypted", HeaderMap::new(), Bytes::new())
            .await
            .expect("encrypted proxy response");
        assert_eq!(response.status.as_u16(), 200);
        assert_eq!(
            response.body.next().await.unwrap().unwrap(),
            Bytes::from_static(b"ok")
        );
        assert!(response.body.next().await.is_none());
        drop(response);
        drop(client);
        proxy.shutdown().await;

        assert_eq!(proxy.handshake_count(), 1);
        assert_eq!(
            proxy.targets(),
            vec![format!("localhost:{}", server.port())]
        );
        assert_eq!(server.requests().len(), 1);
        assert_eq!(server.requests()[0].target, "/encrypted");
    }
}

#[tokio::test(flavor = "current_thread")]
async fn extended_encrypted_proxy_auth_failure_is_redacted_and_fail_closed() {
    install_test_crypto_provider();
    let kind = EncryptedProxyKind::Shadowsocks;
    let server = FixtureServer::http(ResponseMode::Normal, 1);
    let mut proxy = EncryptedProxyFixture::start(kind, "right-secret-marker", 1);
    let client = ProviderHttpClient::new_with_proxy(
        proxy_test_config(&server.http_url()),
        &proxy.uri(kind, "wrong-secret-marker"),
    )
    .expect("encrypted proxy client");
    let error = client
        .send(
            Method::GET,
            "/must-not-bypass",
            HeaderMap::new(),
            Bytes::new(),
        )
        .await
        .expect_err("wrong encrypted proxy secret");
    assert!(!error.to_string().contains("right-secret-marker"));
    assert!(!error.to_string().contains("wrong-secret-marker"));
    assert!(server.requests().is_empty());
    drop(client);
    proxy.shutdown().await;
    assert_eq!(proxy.handshake_count(), 0);
    assert!(proxy.targets().is_empty());
}

#[tokio::test(flavor = "current_thread")]
async fn extended_encrypted_proxy_cancellation_recovers_through_same_client() {
    install_test_crypto_provider();
    let server =
        FixtureServer::http_with_idle_timeout(ResponseMode::Normal, 1, Duration::from_secs(5));
    let mut proxy = EncryptedProxyFixture::start_with_delay(
        EncryptedProxyKind::Shadowsocks,
        "cancel-secret",
        2,
        Duration::from_millis(100),
        true,
    );
    let client = ProviderHttpClient::new_with_proxy(
        proxy_test_config(&server.http_url()),
        &proxy.uri(EncryptedProxyKind::Shadowsocks, "cancel-secret"),
    )
    .expect("encrypted proxy client");
    let cancelled_client = client.clone();
    let pending = tokio::spawn(async move {
        cancelled_client
            .send(
                Method::GET,
                "/cancelled-encrypted",
                HeaderMap::new(),
                Bytes::new(),
            )
            .await
    });
    tokio::time::sleep(Duration::from_millis(1)).await;
    pending.abort();
    let _ = pending.await;
    tokio::time::sleep(Duration::from_millis(50)).await;

    let mut response = client
        .send(
            Method::GET,
            "/after-encrypted-cancel",
            HeaderMap::new(),
            Bytes::new(),
        )
        .await
        .expect("encrypted request after cancellation");
    assert_eq!(response.status.as_u16(), 200);
    assert_eq!(
        response.body.next().await.unwrap().unwrap(),
        Bytes::from_static(b"ok")
    );
    assert_eq!(server.requests().len(), 1);
    assert_eq!(server.requests()[0].target, "/after-encrypted-cancel");
    drop(response);
    drop(client);
    proxy.shutdown().await;
    assert_eq!(proxy.handshake_count(), 2);
}

#[cfg(feature = "test-support")]
#[tokio::test(flavor = "current_thread")]
async fn trojan_proxy_reaches_the_target_with_test_only_proxy_root() {
    install_test_crypto_provider();
    let server = FixtureServer::http(ResponseMode::Normal, 1);
    let mut proxy = TrojanProxyFixture::start("trojan-secret-marker", 1).await;
    let client = ProviderHttpClient::new_with_proxy_test_root(
        proxy_test_config(&server.http_url()),
        &proxy.uri("trojan-secret-marker"),
        proxy.ca_certificate.clone(),
    )
    .expect("Trojan proxy client");
    let mut response = client
        .send(Method::GET, "/trojan", HeaderMap::new(), Bytes::new())
        .await
        .expect("Trojan proxy response");
    assert_eq!(response.status.as_u16(), 200);
    assert_eq!(
        response.body.next().await.unwrap().unwrap(),
        Bytes::from_static(b"ok")
    );
    assert!(response.body.next().await.is_none());
    drop(response);
    drop(client);
    proxy.shutdown().await;

    assert_eq!(
        proxy.targets(),
        vec![format!("localhost:{}", server.port())]
    );
    assert_eq!(server.requests().len(), 1);
    assert_eq!(server.requests()[0].target, "/trojan");
}

#[cfg(feature = "test-support")]
#[tokio::test(flavor = "current_thread")]
async fn trojan_auth_failure_is_redacted_and_cannot_fall_back_direct() {
    install_test_crypto_provider();
    let server =
        FixtureServer::http_with_idle_timeout(ResponseMode::Normal, 1, Duration::from_secs(5));
    let mut proxy = TrojanProxyFixture::start("right-trojan-secret", 1).await;
    let client = ProviderHttpClient::new_with_proxy_test_root(
        proxy_test_config(&server.http_url()),
        &proxy.uri("wrong-trojan-secret"),
        proxy.ca_certificate.clone(),
    )
    .expect("Trojan proxy client");
    let error = client
        .send(
            Method::GET,
            "/must-not-bypass-trojan",
            HeaderMap::new(),
            Bytes::new(),
        )
        .await
        .expect_err("wrong Trojan secret");
    assert!(!error.to_string().contains("right-trojan-secret"));
    assert!(!error.to_string().contains("wrong-trojan-secret"));
    assert!(server.requests().is_empty());
    drop(client);
    proxy.shutdown().await;
    assert!(proxy.targets().is_empty());
}

#[cfg(feature = "test-support")]
#[tokio::test(flavor = "current_thread")]
async fn ssh_proxy_reaches_the_target_with_the_eggress_compatibility_policy() {
    install_test_crypto_provider();
    let server =
        FixtureServer::http_with_idle_timeout(ResponseMode::Normal, 1, Duration::from_secs(5));
    let proxy = SshProxyFixture::start().await;
    let proxy_uri = proxy.uri();
    let client =
        ProviderHttpClient::new_with_proxy(proxy_test_config(&server.http_url()), &proxy_uri)
            .expect("SSH proxy client");
    let mut response = client
        .send(Method::GET, "/ssh", HeaderMap::new(), Bytes::new())
        .await
        .expect("SSH proxy response");
    assert_eq!(response.status.as_u16(), 200);
    assert_eq!(
        response.body.next().await.unwrap().unwrap(),
        Bytes::from_static(b"ok")
    );
    assert_eq!(server.requests().len(), 1);
    assert_eq!(server.requests()[0].target, "/ssh");
}

#[cfg(feature = "test-support")]
#[tokio::test(flavor = "current_thread")]
async fn ssh_auth_failure_is_redacted_and_cannot_fall_back_direct() {
    install_test_crypto_provider();
    let server = FixtureServer::http(ResponseMode::Normal, 1);
    let proxy = SshProxyFixture::start().await;
    let client = ProviderHttpClient::new_with_proxy(
        proxy_test_config(&server.http_url()),
        &proxy.wrong_uri(),
    )
    .expect("SSH proxy client");
    let error = client
        .send(
            Method::GET,
            "/must-not-bypass-ssh",
            HeaderMap::new(),
            Bytes::new(),
        )
        .await
        .expect_err("wrong SSH key");
    assert!(
        !error
            .to_string()
            .contains(&proxy.wrong_private_key.display().to_string())
    );
    assert!(server.requests().is_empty());
}

#[cfg(feature = "test-support")]
#[tokio::test(flavor = "current_thread")]
async fn ssh_cancellation_does_not_poison_the_provider_client() {
    install_test_crypto_provider();
    let server =
        FixtureServer::http_with_idle_timeout(ResponseMode::Normal, 1, Duration::from_secs(5));
    let proxy = SshProxyFixture::start().await;
    let client =
        ProviderHttpClient::new_with_proxy(proxy_test_config(&server.http_url()), &proxy.uri())
            .expect("SSH proxy client");
    let cancelled_client = client.clone();
    let pending = tokio::spawn(async move {
        cancelled_client
            .send(
                Method::GET,
                "/cancelled-ssh",
                HeaderMap::new(),
                Bytes::new(),
            )
            .await
    });
    tokio::time::sleep(Duration::from_millis(1)).await;
    pending.abort();
    let _ = pending.await;

    let mut response = client
        .send(
            Method::GET,
            "/after-ssh-cancel",
            HeaderMap::new(),
            Bytes::new(),
        )
        .await
        .expect("SSH request after cancellation");
    assert_eq!(response.status.as_u16(), 200);
    assert_eq!(
        response.body.next().await.unwrap().unwrap(),
        Bytes::from_static(b"ok")
    );
    assert_eq!(server.requests().len(), 1);
    assert_eq!(server.requests()[0].target, "/after-ssh-cancel");
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
