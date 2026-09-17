//! Provider HTTP transport with an Eggfetch direct path and Eggress proxy path.
//!
//! The client in this module is intentionally neutral: it knows about HTTP,
//! connection ownership, and timeouts, but not provider credentials, wire
//! formats, routing, retries, or finalization.
//!
//! Direct provider requests use `eggfetch-core` 0.1.5 native
//! `Client::execute_http_body` with HTTP/1.1 only, disabled canceled-request
//! retries, physical connection admission, idle-pool policy, connect timeout,
//! established transport I/O timeouts, and WebPKI plus explicit additional CA
//! roots. Proxy/account routes retain the existing Hyper/Rustls/Eggress stack
//! until phase 2 provides a thin Eggress `Dialer` adapter.

use std::{
    error::Error as StdError,
    future::Future,
    pin::Pin,
    sync::Arc,
    task::{Context, Poll},
    time::Duration,
};

use bytes::Bytes;
use eggfetch_core::{
    HttpVersionPolicy, NativeRequestOptions, PhysicalConnectionPolicy, Timeout as EggfetchTimeout,
    TransportIoDirection, TransportIoTimeout, TrustStore,
};
#[cfg(feature = "eggress-ssh-fallback")]
use eggress_core::{TargetAddr, TargetHost};
use http::{
    Extensions, HeaderMap, Method, Request, StatusCode, Uri,
    uri::{Authority, PathAndQuery, Scheme},
};
use http_body_util::{BodyExt, Full};
use hyper::rt::{Read, ReadBufCursor, Write};
use hyper_rustls::HttpsConnectorBuilder;
use hyper_util::{
    client::legacy::{
        Client,
        connect::{Connected, Connection, HttpConnector},
    },
    rt::{TokioExecutor, TokioIo, TokioTimer},
};
use rustls::{ClientConfig, RootCertStore, pki_types::CertificateDer};
use thiserror::Error;
use tokio::{
    sync::{OwnedSemaphorePermit, Semaphore},
    time,
};
use tower_service::Service;

type BoxError = Box<dyn StdError + Send + Sync>;

const DEFAULT_MAX_REQUEST_BODY_BYTES: usize = 10 * 1024 * 1024;

/// Stable transport categories exposed to later request classification.
#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum TransportError {
    /// The provider transport configuration cannot be used.
    #[error("provider transport configuration is invalid")]
    Configuration,
    /// The configured outbound proxy cannot be parsed or constructed.
    #[error("provider proxy configuration is invalid")]
    ProxyConfiguration,
    /// The request target is not a safe relative provider path.
    #[error("provider transport target is invalid")]
    InvalidTarget,
    /// The finite request body exceeds the configured transport bound.
    #[error("provider request body exceeds the configured limit")]
    RequestBodyTooLarge,
    /// Waiting for a physical connection exceeded the pool timeout.
    #[error("provider connection pool wait timed out")]
    PoolTimeout,
    /// TCP, DNS, or TLS establishment exceeded the connect timeout.
    #[error("provider connection timed out")]
    ConnectTimeout,
    /// TCP, DNS, or other connection establishment failed.
    #[error("provider connection failed")]
    Connect,
    /// The outbound proxy connection timed out.
    #[error("provider proxy connection timed out")]
    ProxyConnectTimeout,
    /// The outbound proxy connection failed.
    #[error("provider proxy connection failed")]
    ProxyConnect,
    /// The outbound proxy rejected its credentials.
    #[error("provider proxy authentication failed")]
    ProxyAuthentication,
    /// The outbound proxy could not connect to the requested target.
    #[error("provider proxy target connection failed")]
    ProxyTargetConnect,
    /// TLS verification or negotiation failed.
    #[error("provider TLS negotiation failed")]
    Tls,
    /// The request could not be written before the write guardrail expired.
    #[error("provider request write timed out")]
    WriteTimeout,
    /// The request could not be written.
    #[error("provider request write failed")]
    Write,
    /// Response body progress exceeded the read guardrail.
    #[error("provider response read timed out")]
    ReadTimeout,
    /// Response body reading failed.
    #[error("provider response read failed")]
    Read,
    /// The finite response body exceeded the catalog/request boundary.
    #[error("provider response body exceeds the configured limit")]
    ResponseBodyTooLarge,
    /// HTTP framing or protocol validation failed.
    #[error("provider HTTP protocol failed")]
    Protocol,
    /// The caller cancelled the transport future.
    #[error("provider transport was cancelled")]
    Cancelled,
}

/// Provider transport settings independent of provider wire semantics.
#[derive(Debug, Clone)]
pub struct ProviderHttpConfig {
    /// Absolute credential-free HTTP(S) provider base URL.
    pub base_url: Uri,
    /// Connect timeout, including DNS/TCP/TLS establishment.
    pub connect_timeout: Duration,
    /// Response read inactivity guardrail.
    pub read_timeout: Duration,
    /// Request write inactivity guardrail.
    pub write_timeout: Duration,
    /// Maximum time waiting for physical connection capacity.
    pub pool_timeout: Duration,
    /// Maximum number of live physical connections.
    pub max_connections: usize,
    /// Maximum number of idle connections retained for the authority.
    pub max_keepalive: usize,
    /// Idle connection expiry. Zero means connections expire immediately.
    pub keepalive_timeout: Duration,
    /// Maximum finite request body accepted by [`ProviderHttpClient`].
    pub max_request_body_bytes: usize,
    /// Additional DER trust anchors, intended for deterministic test CAs.
    /// Production callers should leave this empty to use Mozilla webpki roots.
    pub additional_root_certificates: Vec<Vec<u8>>,
}

impl ProviderHttpConfig {
    /// Construct settings with the standard provider defaults.
    pub fn new(base_url: &str) -> Result<Self, TransportError> {
        let base_url = parse_base_url(base_url)?;
        Ok(Self {
            base_url,
            connect_timeout: Duration::from_secs(5),
            read_timeout: Duration::from_secs(300),
            write_timeout: Duration::from_secs(30),
            pool_timeout: Duration::from_secs(30),
            max_connections: 32,
            max_keepalive: 8,
            keepalive_timeout: Duration::from_secs(30),
            max_request_body_bytes: DEFAULT_MAX_REQUEST_BODY_BYTES,
            additional_root_certificates: Vec::new(),
        })
    }
}

impl TryFrom<&crate::config::ProviderConfig> for ProviderHttpConfig {
    type Error = TransportError;

    fn try_from(provider: &crate::config::ProviderConfig) -> Result<Self, Self::Error> {
        let read_timeout = provider
            .stream_timeouts
            .first_byte_timeout_s
            .into_iter()
            .chain(provider.stream_timeouts.idle_timeout_s)
            .fold(provider.read_timeout_s, f64::max);
        let mut config = Self::new(&provider.base_url)?;
        config.connect_timeout = duration_from_seconds(provider.connect_timeout_s)?;
        config.read_timeout = duration_from_seconds(read_timeout)?;
        config.write_timeout = duration_from_seconds(provider.write_timeout_s)?;
        config.pool_timeout = duration_from_seconds(provider.pool_timeout_s)?;
        config.max_connections =
            usize::try_from(provider.max_connections).map_err(|_| TransportError::Configuration)?;
        config.max_keepalive =
            usize::try_from(provider.max_keepalive).map_err(|_| TransportError::Configuration)?;
        config.keepalive_timeout = duration_from_seconds_allow_zero(provider.keepalive_timeout_s)?;
        validate_limits(&config)?;
        Ok(config)
    }
}

/// A response with raw HTTP facts and a lazy, incremental body.
#[derive(Debug)]
pub struct ProviderResponse {
    /// HTTP status returned by the provider.
    pub status: StatusCode,
    /// Response headers returned by the provider.
    pub headers: HeaderMap,
    /// Hyper response extensions, including connection metadata when present.
    pub extensions: Extensions,
    /// Stream-capable response body.
    pub body: ProviderBody,
}

/// A response body that never buffers the complete upstream response.
#[derive(Debug)]
pub struct ProviderBody {
    inner: ProviderBodyInner,
}

#[derive(Debug)]
enum ProviderBodyInner {
    Hyper(hyper::body::Incoming),
    Eggfetch(std::pin::Pin<Box<eggfetch_core::NativeResponseBody>>),
}

impl ProviderBody {
    fn new(inner: hyper::body::Incoming) -> Self {
        Self {
            inner: ProviderBodyInner::Hyper(inner),
        }
    }

    fn new_eggfetch(inner: eggfetch_core::NativeResponseBody) -> Self {
        Self {
            inner: ProviderBodyInner::Eggfetch(Box::pin(inner)),
        }
    }

    /// Wait for the next data chunk. Trailers are consumed and skipped.
    pub async fn next(&mut self) -> Option<Result<Bytes, TransportError>> {
        match &mut self.inner {
            ProviderBodyInner::Hyper(inner) => loop {
                match inner.frame().await {
                    Some(Ok(frame)) => match frame.into_data() {
                        Ok(data) => return Some(Ok(data)),
                        Err(_) => continue,
                    },
                    Some(Err(error)) => {
                        return Some(Err(map_hyper_error(&error, Stage::Read)));
                    }
                    None => return None,
                }
            },
            ProviderBodyInner::Eggfetch(inner) => loop {
                match inner.frame().await {
                    Some(Ok(frame)) => match frame.into_data() {
                        Ok(data) => return Some(Ok(data)),
                        Err(_) => continue,
                    },
                    Some(Err(error)) => {
                        return Some(Err(map_eggfetch_error(&error, Stage::Read)));
                    }
                    None => return None,
                }
            },
        }
    }

    /// Buffer a response only when the caller supplies an explicit finite
    /// bound.  Catalog discovery is the first caller of this helper; the
    /// neutral transport itself never assumes an unbounded response body.
    pub async fn read_to_bytes(&mut self, max_bytes: usize) -> Result<Bytes, TransportError> {
        let mut body = Vec::new();
        while let Some(chunk) = self.next().await {
            let chunk = chunk?;
            if body.len().saturating_add(chunk.len()) > max_bytes {
                return Err(TransportError::ResponseBodyTooLarge);
            }
            body.extend_from_slice(&chunk);
        }
        Ok(Bytes::from(body))
    }
}

/// Cheap cloneable handle around one provider-scoped connection pool.
///
/// Direct provider routes own an Eggfetch HTTP/1.1 client; configured proxy
/// routes retain the Hyper/Rustls/Eggress client until phase 2 migrates them
/// to a thin Eggress `Dialer` adapter.
#[derive(Clone)]
pub struct ProviderHttpClient {
    inner: ProviderHttpClientInner,
    base_url: Uri,
    max_request_body_bytes: usize,
}

#[derive(Clone)]
enum ProviderHttpClientInner {
    Direct(eggfetch_core::Client),
    Proxied(
        Box<
            Client<
                AdmissionConnector<hyper_rustls::HttpsConnector<ProviderTcpConnector>>,
                Full<Bytes>,
            >,
        >,
    ),
}

impl std::fmt::Debug for ProviderHttpClient {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("ProviderHttpClient")
            .field("base_url", &safe_authority(&self.base_url))
            .field("max_request_body_bytes", &self.max_request_body_bytes)
            .finish()
    }
}

impl ProviderHttpClient {
    /// Build a direct HTTP/1.1 provider client on Eggfetch with explicit
    /// WebPKI roots plus any additional test CA roots.
    pub fn new(config: ProviderHttpConfig) -> Result<Self, TransportError> {
        validate_config(&config)?;
        let client = build_direct_eggfetch_client(&config)?;
        Ok(Self {
            inner: ProviderHttpClientInner::Direct(client),
            base_url: config.base_url,
            max_request_body_bytes: config.max_request_body_bytes,
        })
    }

    /// Build an HTTP/1.1 provider client whose TCP connections use an
    /// Eggress outbound connector.  TLS and HTTP remain owned by this client.
    pub fn new_with_proxy(
        config: ProviderHttpConfig,
        proxy_url: &str,
    ) -> Result<Self, TransportError> {
        Self::build(config, Some(proxy_url))
    }

    /// Build a client with a deterministic test-only Eggress TLS root.
    ///
    /// Production callers must use [`Self::new_with_proxy`], which preserves
    /// Eggress's system-root verification for proxy protocols such as Trojan.
    #[cfg(feature = "test-support")]
    pub fn new_with_proxy_test_root(
        config: ProviderHttpConfig,
        proxy_url: &str,
        proxy_root_certificate: Vec<u8>,
    ) -> Result<Self, TransportError> {
        let _ = rustls::crypto::ring::default_provider().install_default();
        validate_config(&config)?;
        let tcp = ProviderTcpConnector::new_with_test_root(proxy_url, proxy_root_certificate)?;
        Self::build_with_tcp(config, tcp)
    }

    fn build(config: ProviderHttpConfig, proxy_url: Option<&str>) -> Result<Self, TransportError> {
        // Eggress may construct its shared TLS configuration while building
        // the proxy connector, so select the pinned provider before that
        // construction begins.
        let _ = rustls::crypto::ring::default_provider().install_default();
        validate_config(&config)?;
        let tcp = ProviderTcpConnector::new(proxy_url)?;
        Self::build_with_tcp(config, tcp)
    }

    fn build_with_tcp(
        config: ProviderHttpConfig,
        tcp: ProviderTcpConnector,
    ) -> Result<Self, TransportError> {
        let tls_config = build_tls_config(&config.additional_root_certificates)?;
        let proxy_transport = tcp.proxy.is_some();
        let https = HttpsConnectorBuilder::new()
            .with_tls_config(tls_config)
            .https_or_http()
            .enable_http1()
            .wrap_connector(tcp);
        let connector = AdmissionConnector::new(
            https,
            Arc::new(Semaphore::new(config.max_connections)),
            config.pool_timeout,
            config.connect_timeout,
            config.read_timeout,
            config.write_timeout,
            proxy_transport,
        );
        let mut builder = Client::builder(TokioExecutor::new());
        builder
            // Request retry/failover belongs to the coordinator, after it
            // owns persistence and attempt state.  Hyper-util otherwise
            // retries a request that loses a reused idle connection before
            // writing, which would silently consume a transport attempt.
            .retry_canceled_requests(false)
            .pool_timer(TokioTimer::new())
            .pool_idle_timeout(config.keepalive_timeout)
            .pool_max_idle_per_host(config.max_keepalive);
        Ok(Self {
            inner: ProviderHttpClientInner::Proxied(Box::new(builder.build(connector))),
            base_url: config.base_url,
            max_request_body_bytes: config.max_request_body_bytes,
        })
    }

    /// Send one neutral HTTP request to a provider-relative path.
    pub async fn send(
        &self,
        method: Method,
        target: &str,
        headers: HeaderMap,
        body: Bytes,
    ) -> Result<ProviderResponse, TransportError> {
        if body.len() > self.max_request_body_bytes {
            return Err(TransportError::RequestBodyTooLarge);
        }
        let uri = join_provider_target(&self.base_url, target)?;
        match &self.inner {
            ProviderHttpClientInner::Direct(client) => {
                let mut request = Request::new(Full::new(body));
                *request.method_mut() = method;
                *request.uri_mut() = uri;
                *request.headers_mut() = headers;
                *request.version_mut() = http::Version::HTTP_11;
                let response = client
                    .execute_http_body(request, NativeRequestOptions::default())
                    .await
                    .map_err(|error| map_eggfetch_error(&error, Stage::Write))?;
                let (parts, body) = response.into_parts();
                Ok(ProviderResponse {
                    status: parts.status,
                    headers: parts.headers,
                    extensions: parts.extensions,
                    body: ProviderBody::new_eggfetch(body),
                })
            }
            ProviderHttpClientInner::Proxied(client) => {
                let mut request = Request::new(Full::new(body));
                *request.method_mut() = method;
                *request.uri_mut() = uri;
                *request.headers_mut() = headers;
                let response = client
                    .request(request)
                    .await
                    .map_err(|error| map_transport_error(&error, Stage::Write))?;
                let (parts, body) = response.into_parts();
                Ok(ProviderResponse {
                    status: parts.status,
                    headers: parts.headers,
                    extensions: parts.extensions,
                    body: ProviderBody::new(body),
                })
            }
        }
    }

    /// Return the validated base URL without exposing any credential-bearing data.
    pub fn base_url(&self) -> &Uri {
        &self.base_url
    }
}

#[derive(Debug, Clone, Copy)]
enum Stage {
    PoolTimeout,
    ConnectTimeout,
    Connect,
    ProxyConnectTimeout,
    ProxyConnect,
    ProxyAuthentication,
    ProxyTargetConnect,
    Tls,
    WriteTimeout,
    Write,
    ReadTimeout,
    Read,
}

trait ProviderHyperStream: Read + Write + Send + Unpin {}
impl<T: Read + Write + Send + Unpin> ProviderHyperStream for T {}

struct ProviderStream {
    inner: Box<dyn ProviderHyperStream>,
}

impl ProviderStream {
    fn new<T: Read + Write + Send + Unpin + 'static>(inner: T) -> Self {
        Self {
            inner: Box::new(inner),
        }
    }
}

impl Connection for ProviderStream {
    fn connected(&self) -> Connected {
        Connected::new()
    }
}

impl Read for ProviderStream {
    fn poll_read(
        mut self: Pin<&mut Self>,
        context: &mut Context<'_>,
        buffer: ReadBufCursor<'_>,
    ) -> Poll<Result<(), std::io::Error>> {
        Pin::new(&mut *self.inner).poll_read(context, buffer)
    }
}

impl Write for ProviderStream {
    fn poll_write(
        mut self: Pin<&mut Self>,
        context: &mut Context<'_>,
        buffer: &[u8],
    ) -> Poll<Result<usize, std::io::Error>> {
        Pin::new(&mut *self.inner).poll_write(context, buffer)
    }

    fn poll_flush(
        mut self: Pin<&mut Self>,
        context: &mut Context<'_>,
    ) -> Poll<Result<(), std::io::Error>> {
        Pin::new(&mut *self.inner).poll_flush(context)
    }

    fn poll_shutdown(
        mut self: Pin<&mut Self>,
        context: &mut Context<'_>,
    ) -> Poll<Result<(), std::io::Error>> {
        Pin::new(&mut *self.inner).poll_shutdown(context)
    }

    fn is_write_vectored(&self) -> bool {
        self.inner.is_write_vectored()
    }

    fn poll_write_vectored(
        mut self: Pin<&mut Self>,
        context: &mut Context<'_>,
        buffers: &[std::io::IoSlice<'_>],
    ) -> Poll<Result<usize, std::io::Error>> {
        Pin::new(&mut *self.inner).poll_write_vectored(context, buffers)
    }
}

/// Establishes the TCP leg either directly or through one account's Eggress
/// connector.  It deliberately returns one stream type so the surrounding
/// Rustls and Hyper stack is identical for both paths.
type ProxyConnectFuture = Pin<Box<dyn Future<Output = Result<ProviderStream, BoxError>> + Send>>;

trait ProxyDialer: Send + Sync {
    fn connect(&self, host: String, port: u16) -> ProxyConnectFuture;
}

#[derive(Clone)]
struct EgressProxyDialer {
    connector: Arc<eggress_embed::outbound::OutboundConnector>,
}

impl ProxyDialer for EgressProxyDialer {
    fn connect(&self, host: String, port: u16) -> ProxyConnectFuture {
        let connector = Arc::clone(&self.connector);
        Box::pin(async move {
            connector
                .connect_tcp(&host, port)
                .await
                .map(|(stream, _)| stream)
                .map(TokioIo::new)
                .map(ProviderStream::new)
                .map_err(|error| Box::new(error) as BoxError)
        })
    }
}

#[cfg(feature = "eggress-ssh-fallback")]
struct ChainEgressProxyDialer {
    executor: Arc<eggress_core::chain::ChainExecutor>,
    chain: Arc<Vec<eggress_uri::ProxyHopSpec>>,
}

#[cfg(feature = "eggress-ssh-fallback")]
impl ProxyDialer for ChainEgressProxyDialer {
    fn connect(&self, host: String, port: u16) -> ProxyConnectFuture {
        let executor = Arc::clone(&self.executor);
        let chain = Arc::clone(&self.chain);
        Box::pin(async move {
            let host = host
                .parse()
                .map(TargetHost::Ip)
                .unwrap_or_else(|_| TargetHost::Domain(host));
            executor
                .execute(&chain, &TargetAddr { host, port })
                .await
                .map(TokioIo::new)
                .map(ProviderStream::new)
                .map_err(|error| Box::new(error) as BoxError)
        })
    }
}

#[derive(Clone)]
struct ProviderTcpConnector {
    direct: HttpConnector,
    proxy: Option<Arc<dyn ProxyDialer>>,
}

impl ProviderTcpConnector {
    fn new(proxy_url: Option<&str>) -> Result<Self, TransportError> {
        let proxy = match proxy_url {
            Some("direct://") => {
                // Keep the explicit pproxy control form valid while using
                // the direct provider dialer for loopback/test targets that
                // Eggress deliberately rejects as private egress.
                build_egress_connector("direct://")
                    .map_err(|_| TransportError::ProxyConfiguration)?;
                None
            }
            Some(proxy_url) if proxy_uses_ssh(proxy_url) => {
                Some(build_ssh_proxy_dialer(proxy_url)?)
            }
            Some(proxy_url) => Some(Arc::new(EgressProxyDialer {
                connector: Arc::new(
                    build_egress_connector(proxy_url)
                        .map_err(|_| TransportError::ProxyConfiguration)?,
                ),
            }) as Arc<dyn ProxyDialer>),
            None => None,
        };
        let mut direct = HttpConnector::new();
        direct.enforce_http(false);
        Ok(Self { direct, proxy })
    }

    #[cfg(feature = "test-support")]
    fn new_with_test_root(
        proxy_url: &str,
        proxy_root_certificate: Vec<u8>,
    ) -> Result<Self, TransportError> {
        let proxy_roots = if proxy_root_certificate.is_empty() {
            Vec::new()
        } else {
            vec![proxy_root_certificate]
        };
        let tls_config = Arc::new(build_tls_config(&proxy_roots)?);
        let dialer = build_chain_egress_dialer(proxy_url, Some(&tls_config))?;
        let mut direct = HttpConnector::new();
        direct.enforce_http(false);
        Ok(Self {
            direct,
            proxy: Some(Arc::new(dialer)),
        })
    }
}

fn proxy_uses_ssh(proxy_url: &str) -> bool {
    proxy_url.split("__").any(|hop| {
        hop.split_once("://")
            .is_some_and(|(scheme, _)| scheme.split('+').any(|protocol| protocol == "ssh"))
    })
}

#[cfg(feature = "eggress-ssh-fallback")]
fn build_ssh_proxy_dialer(proxy_url: &str) -> Result<Arc<dyn ProxyDialer>, TransportError> {
    Ok(Arc::new(build_chain_egress_dialer(proxy_url, None)?) as Arc<dyn ProxyDialer>)
}

#[cfg(not(feature = "eggress-ssh-fallback"))]
fn build_ssh_proxy_dialer(_proxy_url: &str) -> Result<Arc<dyn ProxyDialer>, TransportError> {
    Err(TransportError::ProxyConfiguration)
}

#[cfg(feature = "eggress-ssh-fallback")]
fn build_chain_egress_dialer(
    proxy_url: &str,
    tls_config: Option<&Arc<ClientConfig>>,
) -> Result<ChainEgressProxyDialer, TransportError> {
    let parsed = eggress_pproxy_compat::uri::parse_pproxy_chain(proxy_url)
        .map_err(|_| TransportError::ProxyConfiguration)?;
    let output = eggress_pproxy_compat::translate_from_uris(
        &eggress_pproxy_compat::PproxyArgs::default_args(),
        &[],
        &[parsed],
    )
    .map_err(|_| TransportError::ProxyConfiguration)?;
    let config: eggress_config::model::ConfigFile =
        toml::from_str(&output.toml).map_err(|_| TransportError::ProxyConfiguration)?;
    let runtime = eggress_config::compile::compile_config(&config)
        .map_err(|_| TransportError::ProxyConfiguration)?;
    let chain = runtime
        .upstreams
        .into_iter()
        .next()
        .map(|upstream| upstream.chain.hops)
        .ok_or(TransportError::ProxyConfiguration)?;
    let ssh_sessions = Some(Arc::new(
        eggress_transport_ssh::SshSessionCache::new_compatibility(),
    ));
    let executor = eggress_server::build_chain_executor(tls_config, None, ssh_sessions);
    Ok(ChainEgressProxyDialer {
        executor: Arc::new(executor),
        chain: Arc::new(chain),
    })
}

fn build_egress_connector(
    proxy_url: &str,
) -> Result<eggress_embed::outbound::OutboundConnector, eggress_embed::EggressError> {
    eggress_embed::outbound::OutboundConnector::from_pproxy_uri(proxy_url)
}

impl Service<Uri> for ProviderTcpConnector {
    type Response = ProviderStream;
    type Error = BoxError;
    type Future = Pin<Box<dyn Future<Output = Result<Self::Response, Self::Error>> + Send>>;

    fn poll_ready(&mut self, context: &mut Context<'_>) -> Poll<Result<(), Self::Error>> {
        if self.proxy.is_some() {
            Poll::Ready(Ok(()))
        } else {
            self.direct
                .poll_ready(context)
                .map_err(|error| Box::new(error) as BoxError)
        }
    }

    fn call(&mut self, destination: Uri) -> Self::Future {
        let Some(proxy) = &self.proxy else {
            let future = self.direct.call(destination);
            return Box::pin(async move {
                future
                    .await
                    .map(ProviderStream::new)
                    .map_err(|error| Box::new(error) as BoxError)
            });
        };

        let Some(host) = destination.host().map(str::to_owned) else {
            return Box::pin(async {
                Err(Box::new(TransportMarker::new(Stage::ProxyConnect)) as BoxError)
            });
        };
        let port = destination.port_u16().unwrap_or_else(|| {
            if destination.scheme().is_some_and(|s| s == &Scheme::HTTPS) {
                443
            } else {
                80
            }
        });
        let proxy = Arc::clone(proxy);
        Box::pin(async move {
            proxy.connect(host, port).await.map_err(|error| {
                let message = error.to_string().to_ascii_lowercase();
                let stage = if message.contains("timed out") || message.contains("timeout") {
                    Stage::ProxyConnectTimeout
                } else if message.contains("auth")
                    || message.contains("credential")
                    || message.contains("password")
                    || message.contains("407")
                {
                    Stage::ProxyAuthentication
                } else if message.contains("target") || message.contains("destination") {
                    Stage::ProxyTargetConnect
                } else {
                    Stage::ProxyConnect
                };
                Box::new(TransportMarker::with_source(stage, error)) as BoxError
            })
        })
    }
}

struct TransportMarker {
    stage: Stage,
    source: Option<BoxError>,
}

impl std::fmt::Debug for TransportMarker {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("TransportMarker")
            .field("stage", &self.stage)
            .finish_non_exhaustive()
    }
}

impl TransportMarker {
    fn new(stage: Stage) -> Self {
        Self {
            stage,
            source: None,
        }
    }

    fn with_source(stage: Stage, source: BoxError) -> Self {
        Self {
            stage,
            source: Some(source),
        }
    }
}

impl std::fmt::Display for TransportMarker {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("provider transport stage failure")
    }
}

impl StdError for TransportMarker {
    fn source(&self) -> Option<&(dyn StdError + 'static)> {
        self.source
            .as_ref()
            .map(|source| source.as_ref() as &(dyn StdError + 'static))
    }
}

#[derive(Clone)]
struct AdmissionConnector<C> {
    inner: C,
    permits: Arc<Semaphore>,
    pool_timeout: Duration,
    connect_timeout: Duration,
    read_timeout: Duration,
    write_timeout: Duration,
    proxy_transport: bool,
}

impl<C> AdmissionConnector<C> {
    fn new(
        inner: C,
        permits: Arc<Semaphore>,
        pool_timeout: Duration,
        connect_timeout: Duration,
        read_timeout: Duration,
        write_timeout: Duration,
        proxy_transport: bool,
    ) -> Self {
        Self {
            inner,
            permits,
            pool_timeout,
            connect_timeout,
            read_timeout,
            write_timeout,
            proxy_transport,
        }
    }
}

impl<C> Service<Uri> for AdmissionConnector<C>
where
    C: Service<Uri> + Clone + Send + 'static,
    C::Response: Read + Write + Connection + Unpin + Send + 'static,
    C::Future: Future<Output = Result<C::Response, C::Error>> + Send + 'static,
    C::Error: Into<BoxError> + std::fmt::Debug + Send + Sync + 'static,
{
    type Response = TimedConnection<C::Response>;
    type Error = BoxError;
    type Future = Pin<Box<dyn Future<Output = Result<Self::Response, Self::Error>> + Send>>;

    fn poll_ready(&mut self, context: &mut Context<'_>) -> Poll<Result<(), Self::Error>> {
        match self.inner.poll_ready(context) {
            Poll::Ready(Ok(())) => Poll::Ready(Ok(())),
            Poll::Ready(Err(error)) => {
                let source: BoxError = error.into();
                let stage = if contains_source::<rustls::Error>(source.as_ref()) {
                    Stage::Tls
                } else {
                    Stage::Connect
                };
                Poll::Ready(Err(Box::new(TransportMarker::with_source(stage, source))))
            }
            Poll::Pending => Poll::Pending,
        }
    }

    fn call(&mut self, destination: Uri) -> Self::Future {
        let is_https = destination.scheme() == Some(&Scheme::HTTPS);
        let mut inner = self.inner.clone();
        let permits = Arc::clone(&self.permits);
        let pool_timeout = self.pool_timeout;
        let connect_timeout = self.connect_timeout;
        let read_timeout = self.read_timeout;
        let write_timeout = self.write_timeout;
        let proxy_transport = self.proxy_transport;
        Box::pin(async move {
            let permit = match time::timeout(pool_timeout, permits.acquire_owned()).await {
                Ok(Ok(permit)) => permit,
                Ok(Err(error)) => {
                    let source: BoxError = error.into();
                    let stage = if is_https
                        && contains_io_kind(source.as_ref(), std::io::ErrorKind::InvalidData)
                    {
                        Stage::Tls
                    } else {
                        Stage::Connect
                    };
                    return Err(Box::new(TransportMarker::with_source(stage, source)) as BoxError);
                }
                Err(_) => {
                    return Err(Box::new(TransportMarker::new(Stage::PoolTimeout)) as BoxError);
                }
            };
            let connecting = inner.call(destination);
            let stream = match time::timeout(connect_timeout, connecting).await {
                Ok(Ok(stream)) => stream,
                Ok(Err(error)) => {
                    let source: BoxError = error.into();
                    let stage = find_marker(source.as_ref()).unwrap_or_else(|| {
                        if is_https && contains_source::<rustls::Error>(source.as_ref()) {
                            Stage::Tls
                        } else {
                            Stage::Connect
                        }
                    });
                    return Err(Box::new(TransportMarker::with_source(stage, source)) as BoxError);
                }
                Err(_) => {
                    let stage = if proxy_transport {
                        Stage::ProxyConnectTimeout
                    } else {
                        Stage::ConnectTimeout
                    };
                    return Err(Box::new(TransportMarker::new(stage)) as BoxError);
                }
            };
            Ok(TimedConnection::new(
                stream,
                permit,
                read_timeout,
                write_timeout,
            ))
        })
    }
}

struct TimedConnection<T> {
    inner: T,
    _permit: OwnedSemaphorePermit,
    read_timeout: Duration,
    write_timeout: Duration,
    read_timer: Option<Pin<Box<time::Sleep>>>,
    write_timer: Option<Pin<Box<time::Sleep>>>,
}

impl<T> TimedConnection<T> {
    fn new(
        inner: T,
        permit: OwnedSemaphorePermit,
        read_timeout: Duration,
        write_timeout: Duration,
    ) -> Self {
        Self {
            inner,
            _permit: permit,
            read_timeout,
            write_timeout,
            read_timer: None,
            write_timer: None,
        }
    }
}

impl<T: Connection> Connection for TimedConnection<T> {
    fn connected(&self) -> Connected {
        self.inner.connected()
    }
}

impl<T: Read + Unpin> Read for TimedConnection<T> {
    fn poll_read(
        self: Pin<&mut Self>,
        context: &mut Context<'_>,
        buffer: ReadBufCursor<'_>,
    ) -> Poll<Result<(), std::io::Error>> {
        let this = self.get_mut();
        if this.read_timer.is_none() {
            this.read_timer = Some(Box::pin(time::sleep(this.read_timeout)));
        }
        match Pin::new(&mut this.inner).poll_read(context, buffer) {
            Poll::Ready(result) => {
                this.read_timer = None;
                Poll::Ready(result)
            }
            Poll::Pending => {
                if this
                    .read_timer
                    .as_mut()
                    .is_some_and(|timer| timer.as_mut().poll(context).is_ready())
                {
                    Poll::Ready(Err(std::io::Error::new(
                        std::io::ErrorKind::TimedOut,
                        TransportMarker::new(Stage::ReadTimeout),
                    )))
                } else {
                    Poll::Pending
                }
            }
        }
    }
}

impl<T: Read + Write + Unpin> Write for TimedConnection<T> {
    fn poll_write(
        self: Pin<&mut Self>,
        context: &mut Context<'_>,
        buffer: &[u8],
    ) -> Poll<Result<usize, std::io::Error>> {
        let this = self.get_mut();
        if this.write_timer.is_none() {
            this.write_timer = Some(Box::pin(time::sleep(this.write_timeout)));
        }
        match Pin::new(&mut this.inner).poll_write(context, buffer) {
            Poll::Ready(result) => {
                this.write_timer = None;
                Poll::Ready(result)
            }
            Poll::Pending => {
                if this
                    .write_timer
                    .as_mut()
                    .is_some_and(|timer| timer.as_mut().poll(context).is_ready())
                {
                    Poll::Ready(Err(std::io::Error::new(
                        std::io::ErrorKind::TimedOut,
                        TransportMarker::new(Stage::WriteTimeout),
                    )))
                } else {
                    Poll::Pending
                }
            }
        }
    }

    fn poll_flush(
        self: Pin<&mut Self>,
        context: &mut Context<'_>,
    ) -> Poll<Result<(), std::io::Error>> {
        let this = self.get_mut();
        if this.write_timer.is_none() {
            this.write_timer = Some(Box::pin(time::sleep(this.write_timeout)));
        }
        match Pin::new(&mut this.inner).poll_flush(context) {
            Poll::Ready(result) => {
                this.write_timer = None;
                Poll::Ready(result)
            }
            Poll::Pending => {
                if this
                    .write_timer
                    .as_mut()
                    .is_some_and(|timer| timer.as_mut().poll(context).is_ready())
                {
                    Poll::Ready(Err(std::io::Error::new(
                        std::io::ErrorKind::TimedOut,
                        TransportMarker::new(Stage::WriteTimeout),
                    )))
                } else {
                    Poll::Pending
                }
            }
        }
    }

    fn poll_shutdown(
        self: Pin<&mut Self>,
        context: &mut Context<'_>,
    ) -> Poll<Result<(), std::io::Error>> {
        Pin::new(&mut self.get_mut().inner).poll_shutdown(context)
    }
}

fn parse_base_url(value: &str) -> Result<Uri, TransportError> {
    let uri: Uri = value.parse().map_err(|_| TransportError::Configuration)?;
    let valid_scheme = uri
        .scheme()
        .is_some_and(|scheme| scheme == &Scheme::HTTP || scheme == &Scheme::HTTPS);
    if !valid_scheme
        || uri
            .authority()
            .is_none_or(|authority| authority.as_str().contains('@'))
        || uri
            .path_and_query()
            .is_some_and(|path| path.query().is_some())
    {
        return Err(TransportError::Configuration);
    }
    Ok(uri)
}

fn join_provider_target(base: &Uri, target: &str) -> Result<Uri, TransportError> {
    let relative: Uri = target.parse().map_err(|_| TransportError::InvalidTarget)?;
    if relative.scheme().is_some() || relative.authority().is_some() || target.starts_with("//") {
        return Err(TransportError::InvalidTarget);
    }
    let relative_path = relative.path();
    let relative_path = if relative_path.is_empty() {
        "/"
    } else {
        relative_path
    };
    let base_path = base.path().trim_end_matches('/');
    let path = if relative_path == "/" {
        if base_path.is_empty() {
            "/".to_owned()
        } else {
            format!("{base_path}/")
        }
    } else {
        format!("{base_path}/{}", relative_path.trim_start_matches('/'))
    };
    let path_and_query = if let Some(query) = relative.query() {
        format!("{path}?{query}")
    } else {
        path
    };
    Uri::builder()
        .scheme(
            base.scheme()
                .cloned()
                .ok_or(TransportError::Configuration)?,
        )
        .authority(
            base.authority()
                .cloned()
                .ok_or(TransportError::Configuration)?,
        )
        .path_and_query(
            path_and_query
                .parse::<PathAndQuery>()
                .map_err(|_| TransportError::InvalidTarget)?,
        )
        .build()
        .map_err(|_| TransportError::InvalidTarget)
}

fn validate_config(config: &ProviderHttpConfig) -> Result<(), TransportError> {
    if config.max_connections == 0
        || config.max_keepalive == 0
        || config.max_keepalive > config.max_connections
        || config.max_request_body_bytes == 0
        || config.connect_timeout.is_zero()
        || config.read_timeout.is_zero()
        || config.write_timeout.is_zero()
        || config.pool_timeout.is_zero()
    {
        return Err(TransportError::Configuration);
    }
    if config
        .base_url
        .scheme()
        .is_none_or(|scheme| scheme != &Scheme::HTTP && scheme != &Scheme::HTTPS)
        || config
            .base_url
            .authority()
            .is_none_or(|authority| authority.as_str().contains('@'))
    {
        return Err(TransportError::Configuration);
    }
    Ok(())
}

fn validate_limits(config: &ProviderHttpConfig) -> Result<(), TransportError> {
    validate_config(config)
}

fn duration_from_seconds(seconds: f64) -> Result<Duration, TransportError> {
    let duration = duration_from_seconds_allow_zero(seconds)?;
    if duration.is_zero() {
        return Err(TransportError::Configuration);
    }
    Ok(duration)
}

fn duration_from_seconds_allow_zero(seconds: f64) -> Result<Duration, TransportError> {
    if !seconds.is_finite() || seconds < 0.0 {
        return Err(TransportError::Configuration);
    }
    Duration::try_from_secs_f64(seconds).map_err(|_| TransportError::Configuration)
}

fn build_tls_config(certificates: &[Vec<u8>]) -> Result<ClientConfig, TransportError> {
    let mut roots = RootCertStore::from_iter(webpki_roots::TLS_SERVER_ROOTS.iter().cloned());
    for certificate in certificates {
        roots
            .add(CertificateDer::from(certificate.clone()))
            .map_err(|_| TransportError::Configuration)?;
    }
    Ok(
        ClientConfig::builder_with_provider(Arc::new(rustls::crypto::ring::default_provider()))
            .with_safe_default_protocol_versions()
            .map_err(|_| TransportError::Configuration)?
            .with_root_certificates(roots)
            .with_no_client_auth(),
    )
}

/// Build the Eggfetch direct client for phase 1.
///
/// Semantics mirror the previous direct Hyper/Rustls stack: HTTP/1.1 only, no
/// hidden canceled-request retry, physical live-connection admission via
/// `PhysicalConnectionPolicy` (never the logical request-concurrency limit),
/// Hyper idle-pool timeout plus per-host idle cap, connect timeout via
/// Eggfetch's connect facility, established read/write inactivity via
/// `TransportIoTimeout`, WebPKI roots plus explicit additional CA roots, and
/// no high-level Eggfetch retries, redirects, or request timeout layers.
fn build_direct_eggfetch_client(
    config: &ProviderHttpConfig,
) -> Result<eggfetch_core::Client, TransportError> {
    let mut tls_builder = eggfetch_core::TlsConfig::builder().trust_store(TrustStore::WebPkiOnly);
    if !config.additional_root_certificates.is_empty() {
        tls_builder = tls_builder
            .additional_ca_certificate_der(config.additional_root_certificates.clone())
            .map_err(|_| TransportError::Configuration)?;
    }
    let tls_config = tls_builder.build();
    Ok(eggfetch_core::Client::builder()
        .http_version_policy(HttpVersionPolicy::Http1Only)
        // One coordinator attempt maps to one upstream transport attempt.
        .retry_canceled_requests(false)
        // Physical lifecycle: max_connections bounds live physical
        // connections including idle pooled sockets; pool_timeout bounds
        // admission wait. Never use the logical max_connections control.
        .physical_connection_policy(PhysicalConnectionPolicy {
            max_live: Some(config.max_connections),
            admission_timeout: Some(config.pool_timeout),
        })
        // Established transport inactivity guards replacing TimedConnection.
        // No high-level Timeout read/write layer is added; doubling layers
        // would change error precedence for body/header stalls.
        .transport_io_timeout(TransportIoTimeout {
            read: Some(config.read_timeout),
            write: Some(config.write_timeout),
        })
        // Idle-pool reuse and expiry matching the previous Hyper policy.
        .idle_timeout(config.keepalive_timeout)
        .max_idle_connections_per_host(config.max_keepalive)
        // Connection establishment only; admission wait stays in the physical
        // policy and classifies as PoolTimeout.
        .timeout(EggfetchTimeout {
            connect: Some(config.connect_timeout),
            ..Default::default()
        })
        .tls_config(tls_config)
        .build())
}

fn safe_authority(uri: &Uri) -> Option<&Authority> {
    uri.authority()
}

fn map_hyper_error(error: &hyper::Error, default_stage: Stage) -> TransportError {
    map_transport_error(error, default_stage)
}

fn map_transport_error(error: &(dyn StdError + 'static), default_stage: Stage) -> TransportError {
    if let Some(hyper_error) = error.downcast_ref::<hyper::Error>() {
        if hyper_error.is_canceled() {
            return TransportError::Cancelled;
        }
        if hyper_error.is_parse()
            || hyper_error.is_incomplete_message()
            || contains_io_kind(hyper_error, std::io::ErrorKind::UnexpectedEof)
        {
            return TransportError::Protocol;
        }
    }
    if contains_source::<rustls::Error>(error) {
        return TransportError::Tls;
    }
    if let Some(stage) = find_marker(error) {
        return map_stage(stage);
    }
    map_stage(default_stage)
}

/// Translate Eggfetch direct-path errors to the stable `TransportError`
/// contract without leaking Eggfetch types above this module.
///
/// Typed inspection order preserves specific timeout categories before broad
/// connection failures: physical admission, established I/O direction,
/// connect establishment, TLS, framing, then ordinary direct failures.
fn map_eggfetch_error(error: &eggfetch_core::Error, default_stage: Stage) -> TransportError {
    use eggfetch_core::{Error as EggfetchError, TimeoutPhase};

    // 1. Physical connection admission timeout stays PoolTimeout. The
    // physical policy is authoritative for live connections including idle
    // pooled sockets; never conflate with logical request concurrency.
    if error.is_physical_connection_admission_timeout() {
        return TransportError::PoolTimeout;
    }
    if let EggfetchError::Pool(message) = error {
        // Zero max_live or admission without max_live is a construction bug;
        // validate_config already rejects max_connections == 0, so fail
        // closed as configuration rather than a transient pool timeout.
        if message.contains("max_live") {
            return TransportError::Configuration;
        }
        return TransportError::PoolTimeout;
    }
    // 2-3. Established transport inactivity direction is typed.
    if let EggfetchError::TransportIoTimeout { direction, .. } = error {
        return match direction {
            TransportIoDirection::Read => TransportError::ReadTimeout,
            TransportIoDirection::Write => TransportError::WriteTimeout,
        };
    }
    // 4. Phase-aware timeouts. Only connect is configured on the direct
    // path; read/write/total/proxy phases are mapped defensively so a
    // future misconfiguration cannot silently become Connect.
    if let EggfetchError::Timeout { phase, .. } = error {
        return match phase {
            TimeoutPhase::Pool => TransportError::PoolTimeout,
            TimeoutPhase::Connect => TransportError::ConnectTimeout,
            TimeoutPhase::Read => TransportError::ReadTimeout,
            TimeoutPhase::Write => TransportError::WriteTimeout,
            // No total deadline is configured on the direct path. Map to a
            // timeout category rather than a connection failure.
            TimeoutPhase::Total => TransportError::ReadTimeout,
            TimeoutPhase::ProxyConnect | TimeoutPhase::ProxyTls => {
                TransportError::ProxyConnectTimeout
            }
        };
    }
    // 5. TLS establishment/verification stays Tls, including rustls sources
    // wrapped through Hyper/HyperClient on the standard connector path.
    match error {
        EggfetchError::Tls(_)
        | EggfetchError::TlsConfig(_)
        | EggfetchError::CaBundle(_)
        | EggfetchError::ClientCert(_)
        | EggfetchError::PrivateKey(_)
        | EggfetchError::CertificateVerification(_)
        | EggfetchError::HostnameVerification(_) => return TransportError::Tls,
        _ => {}
    }
    if contains_source::<rustls::Error>(error) {
        return TransportError::Tls;
    }
    // Canceled Hyper requests map to Cancelled so coordinator accounting can
    // distinguish caller cancellation from transport failures.
    if eggfetch_source_is_canceled(error) {
        return TransportError::Cancelled;
    }
    // 6. Malformed/protocol/body framing failures map to Protocol.
    if matches!(
        error,
        EggfetchError::Protocol(_)
            | EggfetchError::Body(_)
            | EggfetchError::Decompression(_)
            | EggfetchError::UnsupportedContentEncoding(_)
            | EggfetchError::DecodedBodyTooLarge
            | EggfetchError::DecompressionRatioExceeded
            | EggfetchError::Http2GoAway { .. }
            | EggfetchError::Http2StreamReset { .. }
            | EggfetchError::Http2FlowControl(_)
            | EggfetchError::Http2Protocol(_)
            | EggfetchError::H3Connect(_)
            | EggfetchError::H3ConnectionClosed(_)
            | EggfetchError::H3Stream(_)
            | EggfetchError::H3Protocol(_)
    ) {
        return TransportError::Protocol;
    }
    if eggfetch_source_is_protocol(error) {
        return TransportError::Protocol;
    }
    // Target construction failures stay InvalidTarget; other request-build
    // failures are malformed requests and map to Protocol.
    match error {
        EggfetchError::InvalidUrl(_)
        | EggfetchError::InvalidResolvedTarget(_)
        | EggfetchError::ResolvedTargetRedirect => return TransportError::InvalidTarget,
        EggfetchError::InvalidMethod(_)
        | EggfetchError::InvalidHeaderName(_)
        | EggfetchError::InvalidHeaderValue(_)
        | EggfetchError::RequestBuild(_)
        | EggfetchError::InvalidRedirectLocation(_)
        | EggfetchError::InvalidAuthHeader(_)
        | EggfetchError::ConflictingAuth(_)
        | EggfetchError::BodyNotReplayableForRedirect
        | EggfetchError::BodyNotReplayableForRetry
        | EggfetchError::JsonSerialize(_)
        | EggfetchError::JsonDeserialize(_) => return TransportError::Protocol,
        EggfetchError::RetryBudgetExhausted { .. } | EggfetchError::RetryNotConfigured => {
            return TransportError::Protocol;
        }
        EggfetchError::TraceCallbackAborted => return TransportError::Cancelled,
        EggfetchError::Unsupported(_) => return TransportError::Configuration,
        _ => {}
    }
    // Proxy-route errors retain proxy categories for phase 2; the direct
    // path never configures a proxy so these are defensive.
    match error {
        EggfetchError::InvalidProxyUrl(_)
        | EggfetchError::ProxyConnect(_)
        | EggfetchError::MalformedProxyResponse(_) => return TransportError::ProxyConnect,
        EggfetchError::ProxyAuthRequired => return TransportError::ProxyAuthentication,
        EggfetchError::ProxyConnectRejected { .. } => return TransportError::ProxyTargetConnect,
        _ => {}
    }
    // 7. Ordinary direct connection failures stay Connect. This includes
    // typed Connect, HyperClient connect failures, and I/O connection
    // refusal observed through the standard connector.
    if let EggfetchError::HyperClient(inner) = error
        && inner.is_connect()
    {
        return TransportError::Connect;
    }
    if let EggfetchError::Connect(_) = error {
        return TransportError::Connect;
    }
    if let EggfetchError::Io(inner) = error
        && inner.kind() == std::io::ErrorKind::ConnectionRefused
    {
        return TransportError::Connect;
    }
    if let EggfetchError::CustomTransport(_) = error {
        return TransportError::Connect;
    }
    // Hyper and I/O errors without a more specific classification fall back
    // to the caller phase: Write for dispatch, Read for body polling,
    // matching the previous Hyper mapping defaults.
    map_stage(default_stage)
}

/// Return true when any Hyper error in the Eggfetch source chain reports a
/// canceled request.
fn eggfetch_source_is_canceled(error: &(dyn StdError + 'static)) -> bool {
    let mut current: Option<&(dyn StdError + 'static)> = Some(error);
    while let Some(next) = current {
        if next
            .downcast_ref::<hyper::Error>()
            .is_some_and(|hyper_error| hyper_error.is_canceled())
        {
            return true;
        }
        current = next.source();
    }
    false
}

/// Return true when any Hyper framing error or truncated-body I/O marker in
/// the Eggfetch source chain indicates a protocol failure.
fn eggfetch_source_is_protocol(error: &(dyn StdError + 'static)) -> bool {
    let mut current: Option<&(dyn StdError + 'static)> = Some(error);
    while let Some(next) = current {
        if let Some(hyper_error) = next.downcast_ref::<hyper::Error>()
            && (hyper_error.is_parse() || hyper_error.is_incomplete_message())
        {
            return true;
        }
        current = next.source();
    }
    contains_io_kind(error, std::io::ErrorKind::UnexpectedEof)
}

fn find_marker(error: &(dyn StdError + 'static)) -> Option<Stage> {
    if let Some(marker) = error.downcast_ref::<TransportMarker>() {
        return Some(marker.stage);
    }
    if let Some(io_error) = error.downcast_ref::<std::io::Error>()
        && let Some(inner) = io_error.get_ref()
        && let Some(stage) = find_marker(inner)
    {
        return Some(stage);
    }
    error.source().and_then(find_marker)
}

fn contains_source<T: StdError + 'static>(error: &(dyn StdError + 'static)) -> bool {
    if error.downcast_ref::<T>().is_some() {
        return true;
    }
    if let Some(io_error) = error.downcast_ref::<std::io::Error>()
        && let Some(inner) = io_error.get_ref()
        && contains_source::<T>(inner)
    {
        return true;
    }
    error.source().is_some_and(contains_source::<T>)
}

fn contains_io_kind(error: &(dyn StdError + 'static), kind: std::io::ErrorKind) -> bool {
    if error
        .downcast_ref::<std::io::Error>()
        .is_some_and(|io_error| io_error.kind() == kind)
    {
        return true;
    }
    if let Some(io_error) = error.downcast_ref::<std::io::Error>()
        && let Some(inner) = io_error.get_ref()
        && contains_io_kind(inner, kind)
    {
        return true;
    }
    if let Some(source) = error.source()
        && contains_io_kind(source, kind)
    {
        return true;
    }
    false
}

fn map_stage(stage: Stage) -> TransportError {
    match stage {
        Stage::PoolTimeout => TransportError::PoolTimeout,
        Stage::ConnectTimeout => TransportError::ConnectTimeout,
        Stage::Connect => TransportError::Connect,
        Stage::ProxyConnectTimeout => TransportError::ProxyConnectTimeout,
        Stage::ProxyConnect => TransportError::ProxyConnect,
        Stage::ProxyAuthentication => TransportError::ProxyAuthentication,
        Stage::ProxyTargetConnect => TransportError::ProxyTargetConnect,
        Stage::Tls => TransportError::Tls,
        Stage::WriteTimeout => TransportError::WriteTimeout,
        Stage::Write => TransportError::Write,
        Stage::ReadTimeout => TransportError::ReadTimeout,
        Stage::Read => TransportError::Read,
    }
}

#[cfg(test)]
mod tests {
    use super::{
        ProviderHttpConfig, TransportError, join_provider_target, parse_base_url, proxy_uses_ssh,
    };

    #[test]
    fn joins_base_path_and_query_without_changing_authority() {
        let base = parse_base_url("https://provider.example/v1/").expect("base URL");
        let joined =
            join_provider_target(&base, "/chat/completions?stream=true").expect("relative target");
        assert_eq!(
            joined.to_string(),
            "https://provider.example/v1/chat/completions?stream=true"
        );
    }

    #[test]
    fn rejects_absolute_and_authority_changing_targets() {
        let base = parse_base_url("http://provider.example/v1").expect("base URL");
        for target in ["https://attacker.example/x", "//attacker.example/x"] {
            assert_eq!(
                join_provider_target(&base, target),
                Err(TransportError::InvalidTarget)
            );
        }
    }

    #[test]
    fn rejects_secret_bearing_base_urls_and_invalid_limits() {
        assert_eq!(
            parse_base_url("https://user:secret@provider.example"),
            Err(TransportError::Configuration)
        );
        let mut config = ProviderHttpConfig::new("http://provider.example").expect("base URL");
        config.max_keepalive = config.max_connections + 1;
        assert_eq!(
            super::validate_config(&config),
            Err(TransportError::Configuration)
        );
    }

    #[test]
    fn detects_ssh_only_as_a_protocol_token_in_any_chain_hop() {
        for proxy_url in [
            "ssh://user@proxy.example:22",
            "ssh+http://user@proxy.example:22",
            "http://proxy.example:8080__ssh://user@proxy.example:22",
            "http+ssh://proxy.example:8080",
        ] {
            assert!(proxy_uses_ssh(proxy_url), "expected SSH in {proxy_url}");
        }
        for proxy_url in [
            "http://proxy.example:8080",
            "https://ssh.example",
            "socks5://proxy.example:1080__trojan://ssh.example",
        ] {
            assert!(!proxy_uses_ssh(proxy_url), "unexpected SSH in {proxy_url}");
        }
    }
}
