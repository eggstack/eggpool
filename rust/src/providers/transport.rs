//! Direct provider HTTP transport built on Hyper and Rustls.
//!
//! The client in this module is intentionally neutral: it knows about HTTP,
//! connection ownership, and timeouts, but not provider credentials, wire
//! formats, routing, retries, or finalization.

use std::{
    error::Error as StdError,
    future::Future,
    pin::Pin,
    sync::Arc,
    task::{Context, Poll},
    time::Duration,
};

use bytes::Bytes;
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
    inner: hyper::body::Incoming,
}

impl ProviderBody {
    fn new(inner: hyper::body::Incoming) -> Self {
        Self { inner }
    }

    /// Wait for the next data chunk. Trailers are consumed and skipped.
    pub async fn next(&mut self) -> Option<Result<Bytes, TransportError>> {
        loop {
            match self.inner.frame().await {
                Some(Ok(frame)) => match frame.into_data() {
                    Ok(data) => return Some(Ok(data)),
                    Err(_) => continue,
                },
                Some(Err(error)) => return Some(Err(map_hyper_error(&error, Stage::Read))),
                None => return None,
            }
        }
    }
}

/// Cheap cloneable handle around one provider-scoped Hyper connection pool.
#[derive(Clone)]
pub struct ProviderHttpClient {
    client:
        Client<AdmissionConnector<hyper_rustls::HttpsConnector<ProviderTcpConnector>>, Full<Bytes>>,
    base_url: Uri,
    max_request_body_bytes: usize,
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
    /// Build a direct HTTP/1.1 provider client with explicit Rustls roots.
    pub fn new(config: ProviderHttpConfig) -> Result<Self, TransportError> {
        Self::build(config, None)
    }

    /// Build an HTTP/1.1 provider client whose TCP connections use an
    /// Eggress outbound connector.  TLS and HTTP remain owned by this client.
    pub fn new_with_proxy(
        config: ProviderHttpConfig,
        proxy_url: &str,
    ) -> Result<Self, TransportError> {
        Self::build(config, Some(proxy_url))
    }

    fn build(config: ProviderHttpConfig, proxy_url: Option<&str>) -> Result<Self, TransportError> {
        validate_config(&config)?;
        let tls_config = build_tls_config(&config.additional_root_certificates)?;
        let tcp = ProviderTcpConnector::new(proxy_url)?;
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
            proxy_url.is_some_and(|url| url != "direct://"),
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
            client: builder.build(connector),
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
        let mut request = Request::new(Full::new(body));
        *request.method_mut() = method;
        *request.uri_mut() = uri;
        *request.headers_mut() = headers;
        let response = self
            .client
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
#[derive(Clone)]
struct ProviderTcpConnector {
    direct: HttpConnector,
    proxy: Option<Arc<eggress_embed::outbound::OutboundConnector>>,
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
            Some(proxy_url) => Some(Arc::new(
                build_egress_connector(proxy_url)
                    .map_err(|_| TransportError::ProxyConfiguration)?,
            )),
            None => None,
        };
        let mut direct = HttpConnector::new();
        direct.enforce_http(false);
        Ok(Self { direct, proxy })
    }
}

fn build_egress_connector(
    proxy_url: &str,
) -> Result<eggress_embed::outbound::OutboundConnector, eggress_embed::EggressError> {
    if !proxy_url.contains("__") {
        return eggress_embed::outbound::OutboundConnector::from_pproxy_uri(proxy_url);
    }

    // The embed convenience constructor intentionally accepts one pproxy
    // hop.  Full account chains are passed through the same Eggress parser by
    // its native upstream TOML shape, without adding another proxy stack.
    let mut upstream = toml::map::Map::new();
    upstream.insert("id".into(), toml::Value::String("eggpool-account".into()));
    upstream.insert("uri".into(), toml::Value::String(proxy_url.into()));
    let mut root = toml::map::Map::new();
    root.insert("version".into(), toml::Value::Integer(1));
    root.insert(
        "upstreams".into(),
        toml::Value::Array(vec![toml::Value::Table(upstream)]),
    );
    let source = toml::to_string(&toml::Value::Table(root))
        .map_err(|error| eggress_embed::EggressError::Config(error.to_string()))?;
    eggress_embed::outbound::OutboundConnector::from_toml(&source)
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
            match proxy.connect_tcp(&host, port).await {
                Ok((stream, _info)) => Ok(ProviderStream::new(TokioIo::new(stream))),
                Err(error) => {
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
                    Err(Box::new(TransportMarker::new(stage)) as BoxError)
                }
            }
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
    use super::{ProviderHttpConfig, TransportError, join_provider_target, parse_base_url};

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
}
