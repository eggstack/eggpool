//! Provider HTTP transport on Eggfetch with an Eggress route dialer.
//!
//! The client in this module is intentionally neutral: it knows about HTTP,
//! connection ownership, and timeouts, but not provider credentials, wire
//! formats, routing, retries, or finalization.
//!
//! Direct and proxied provider requests share Eggfetch's HTTP/1.1, origin TLS,
//! pooling, physical admission, and transport-I/O machinery through
//! `eggfetch-core` 0.1.5 native `Client::execute_http_body`. Proxied routes
//! supply the physical byte stream through a thin `EggressDialer` adapter that
//! implements Eggfetch's general custom `Dialer` interface over Eggress's
//! existing raw TCP-route API. Eggress owns route/proxy handshakes and
//! route-level TLS; Eggfetch still performs destination/origin TLS across the
//! returned stream, so proxy and origin trust planes remain separate.

use std::{
    error::Error as StdError,
    pin::Pin,
    sync::Arc,
    task::{Context, Poll},
    time::Duration,
};

use bytes::Bytes;
use eggfetch_core::{
    DialError, DialErrorKind, DialFuture, DialStream, DialTarget, Dialer, HttpVersionPolicy,
    NativeRequestOptions, PhysicalConnectionPolicy, Timeout as EggfetchTimeout,
    TransportIoDirection, TransportIoTimeout, TrustStore,
};
#[cfg(feature = "eggress-ssh-fallback")]
use eggress_core::{TargetAddr, TargetHost};
use http::{
    Extensions, HeaderMap, Method, Request, StatusCode, Uri,
    uri::{Authority, PathAndQuery, Scheme},
};
use http_body_util::{BodyExt, Full};
#[cfg(any(feature = "eggress-ssh-fallback", feature = "test-support"))]
use rustls::ClientConfig;
#[cfg(feature = "test-support")]
use rustls::{RootCertStore, pki_types::CertificateDer};
use thiserror::Error;
use tokio::io::{AsyncRead, AsyncWrite, ReadBuf};

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
    /// Response extensions, including connection metadata when present.
    pub extensions: Extensions,
    /// Stream-capable response body.
    pub body: ProviderBody,
}

/// A response body that never buffers the complete upstream response.
#[derive(Debug)]
pub struct ProviderBody {
    inner: Pin<Box<eggfetch_core::NativeResponseBody>>,
    /// Whether the owning client dials through an Eggress route. Dialer
    /// failures only occur on proxied routes, so error translation needs the
    /// route to classify establishment timeouts without conflating them with
    /// direct connection failures.
    proxy_transport: bool,
}

impl ProviderBody {
    fn new_eggfetch(inner: eggfetch_core::NativeResponseBody, proxy_transport: bool) -> Self {
        Self {
            inner: Box::pin(inner),
            proxy_transport,
        }
    }

    /// Wait for the next data chunk. Trailers are consumed and skipped.
    pub async fn next(&mut self) -> Option<Result<Bytes, TransportError>> {
        loop {
            match self.inner.frame().await {
                Some(Ok(frame)) => match frame.into_data() {
                    Ok(data) => return Some(Ok(data)),
                    Err(_) => continue,
                },
                Some(Err(error)) => {
                    return Some(Err(map_eggfetch_error(
                        &error,
                        TransportError::Read,
                        self.proxy_transport,
                    )));
                }
                None => return None,
            }
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
/// routes own a separate Eggfetch HTTP/1.1 client whose physical byte streams
/// are supplied by a thin `EggressDialer` adapter. Each proxied
/// `ProviderHttpClient` owns a distinct Eggfetch `Client` even when two
/// accounts resolve to the same proxy URI, so account/route pools never share
/// connection state.
#[derive(Clone)]
pub struct ProviderHttpClient {
    client: eggfetch_core::Client,
    /// Whether this client dials through an Eggress route. Dialer failures
    /// only occur on proxied routes, so error translation needs the route to
    /// classify establishment timeouts without conflating them with direct
    /// connection failures.
    proxy_transport: bool,
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
    /// Build a direct HTTP/1.1 provider client on Eggfetch with explicit
    /// WebPKI roots plus any additional test CA roots.
    pub fn new(config: ProviderHttpConfig) -> Result<Self, TransportError> {
        validate_config(&config)?;
        let client = build_eggfetch_client(&config, None)?;
        Ok(Self {
            client,
            proxy_transport: false,
            base_url: config.base_url,
            max_request_body_bytes: config.max_request_body_bytes,
        })
    }

    /// Build an HTTP/1.1 provider client whose physical routes are supplied
    /// by Eggress through Eggfetch's custom `Dialer` interface. Origin TLS
    /// and HTTP framing remain owned by Eggfetch.
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
        let dialer = build_chain_egress_dialer_for_test_root(proxy_url, proxy_root_certificate)?;
        let client = build_eggfetch_client(&config, Some(dialer))?;
        Ok(Self {
            client,
            proxy_transport: true,
            base_url: config.base_url,
            max_request_body_bytes: config.max_request_body_bytes,
        })
    }

    fn build(config: ProviderHttpConfig, proxy_url: Option<&str>) -> Result<Self, TransportError> {
        // Eggress may construct its shared TLS configuration while building
        // the proxy connector, so select the pinned provider before that
        // construction begins.
        let _ = rustls::crypto::ring::default_provider().install_default();
        validate_config(&config)?;
        let dialer = build_eggress_dialer(proxy_url)?;
        let proxy_transport = dialer.is_some();
        let client = build_eggfetch_client(&config, dialer)?;
        Ok(Self {
            client,
            proxy_transport,
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
        let client = &self.client;
        let proxy_transport = self.proxy_transport;
        let mut request = Request::new(Full::new(body));
        *request.method_mut() = method;
        *request.uri_mut() = uri;
        *request.headers_mut() = headers;
        *request.version_mut() = http::Version::HTTP_11;
        let response = client
            .execute_http_body(request, NativeRequestOptions::default())
            .await
            .map_err(|error| map_eggfetch_error(&error, TransportError::Write, proxy_transport))?;
        let (parts, body) = response.into_parts();
        Ok(ProviderResponse {
            status: parts.status,
            headers: parts.headers,
            extensions: parts.extensions,
            body: ProviderBody::new_eggfetch(body, proxy_transport),
        })
    }

    /// Return the validated base URL without exposing any credential-bearing data.
    pub fn base_url(&self) -> &Uri {
        &self.base_url
    }
}

/// One Eggress-owned byte stream adapted to Eggfetch's dialer contract.
///
/// The wrapper performs no HTTP framing and no TLS: Eggress owns the
/// route/proxy handshake (including any route-level TLS such as a Trojan
/// connection to the proxy), while Eggfetch performs destination/origin TLS
/// across this stream for `https://` upstreams. The stream type stays generic
/// so both the stable embed connector and the compatibility chain executor
/// can supply routes without naming an optional dependency.
struct EggressDialStream<S>(S);

impl<S> AsyncRead for EggressDialStream<S>
where
    S: AsyncRead + Unpin,
{
    fn poll_read(
        mut self: Pin<&mut Self>,
        context: &mut Context<'_>,
        buffer: &mut ReadBuf<'_>,
    ) -> Poll<std::io::Result<()>> {
        Pin::new(&mut self.0).poll_read(context, buffer)
    }
}

impl<S> AsyncWrite for EggressDialStream<S>
where
    S: AsyncWrite + Unpin,
{
    fn poll_write(
        mut self: Pin<&mut Self>,
        context: &mut Context<'_>,
        buffer: &[u8],
    ) -> Poll<Result<usize, std::io::Error>> {
        Pin::new(&mut self.0).poll_write(context, buffer)
    }

    fn poll_flush(
        mut self: Pin<&mut Self>,
        context: &mut Context<'_>,
    ) -> Poll<Result<(), std::io::Error>> {
        Pin::new(&mut self.0).poll_flush(context)
    }

    fn poll_shutdown(
        mut self: Pin<&mut Self>,
        context: &mut Context<'_>,
    ) -> Poll<Result<(), std::io::Error>> {
        Pin::new(&mut self.0).poll_shutdown(context)
    }
}

/// Thin Eggress-to-Eggfetch route adapter.
///
/// This is deliberately not a transport framework: it turns an Eggress raw
/// stream to the requested logical destination into Eggfetch's `DialStream`
/// and classifies route failures into `DialError` kinds. HTTP framing, origin
/// TLS, pooling, admission, and timeouts stay in Eggfetch; proxy protocol
/// behavior stays in Eggress. There is no direct-network fallback: a failed
/// dial is the only physical route a proxied Eggfetch client owns.
#[derive(Clone)]
struct EggressDialer {
    inner: EggressDialerInner,
}

#[derive(Clone)]
enum EggressDialerInner {
    Outbound {
        connector: Arc<eggress_embed::outbound::OutboundConnector>,
    },
    #[cfg(feature = "eggress-ssh-fallback")]
    Chain {
        executor: Arc<eggress_core::chain::ChainExecutor>,
        chain: Arc<Vec<eggress_uri::ProxyHopSpec>>,
    },
}

impl Dialer for EggressDialer {
    fn dial(&self, target: DialTarget) -> DialFuture<'_> {
        let inner = self.inner.clone();
        Box::pin(async move {
            match inner {
                EggressDialerInner::Outbound { connector } => connector
                    .connect_tcp(target.host(), target.port())
                    .await
                    .map(|(stream, _)| dial_stream(stream))
                    .map_err(map_egress_dial_error),
                #[cfg(feature = "eggress-ssh-fallback")]
                EggressDialerInner::Chain { executor, chain } => {
                    let target = target_addr_for_dial(target.host(), target.port());
                    executor
                        .execute(&chain, &target)
                        .await
                        .map(dial_stream)
                        .map_err(map_chain_dial_error)
                }
            }
        })
    }
}

/// Erase one Eggress-owned route stream into Eggfetch's dialer stream type.
///
/// The concrete Eggress stream type is inferred from the route API so this
/// boundary never names an optional dependency; the bounds below are the
/// actual contract Eggfetch needs.
fn dial_stream<S>(stream: S) -> DialStream
where
    S: AsyncRead + AsyncWrite + Send + Unpin + 'static,
{
    Box::new(EggressDialStream(stream))
}

/// Convert an Eggfetch logical dial target into an Eggress route target.
///
/// Domain names are preserved so SOCKS5 domain-address requests, proxy-side
/// DNS, and destination TLS SNI keep working. Only IP literals become
/// `TargetHost::Ip`; nothing is eagerly resolved locally.
#[cfg(feature = "eggress-ssh-fallback")]
fn target_addr_for_dial(host: &str, port: u16) -> TargetAddr {
    let host = host
        .parse()
        .map(TargetHost::Ip)
        .unwrap_or_else(|_| TargetHost::Domain(host.to_owned()));
    TargetAddr { host, port }
}

/// Select the Eggress route for one account.
///
/// `None` means direct Eggfetch dialing. `Some` means the returned custom
/// dialer owns the only physical route available to that account's Eggfetch
/// client: a failed dial never falls back to direct networking.
fn build_eggress_dialer(proxy_url: Option<&str>) -> Result<Option<EggressDialer>, TransportError> {
    let dialer = match proxy_url {
        None => None,
        Some("direct://") => {
            // Keep the explicit pproxy control form valid while using
            // the direct provider dialer for loopback/test targets that
            // Eggress deliberately rejects as private egress.
            build_egress_connector("direct://").map_err(|_| TransportError::ProxyConfiguration)?;
            None
        }
        Some(proxy_url) if proxy_uses_ssh(proxy_url) => Some(build_ssh_route_dialer(proxy_url)?),
        Some(proxy_url) => Some(EggressDialer {
            inner: EggressDialerInner::Outbound {
                connector: Arc::new(
                    build_egress_connector(proxy_url)
                        .map_err(|_| TransportError::ProxyConfiguration)?,
                ),
            },
        }),
    };
    Ok(dialer)
}

fn proxy_uses_ssh(proxy_url: &str) -> bool {
    proxy_url.split("__").any(|hop| {
        hop.split_once("://")
            .is_some_and(|(scheme, _)| scheme.split('+').any(|protocol| protocol == "ssh"))
    })
}

#[cfg(feature = "eggress-ssh-fallback")]
fn build_ssh_route_dialer(proxy_url: &str) -> Result<EggressDialer, TransportError> {
    build_chain_egress_dialer(proxy_url, None)
}

#[cfg(not(feature = "eggress-ssh-fallback"))]
fn build_ssh_route_dialer(_proxy_url: &str) -> Result<EggressDialer, TransportError> {
    Err(TransportError::ProxyConfiguration)
}

/// Build a chain-route dialer with a deterministic test-only Eggress TLS
/// root.  Production callers use [`build_eggress_dialer`], which preserves
/// Eggress's system-root verification.
#[cfg(feature = "test-support")]
fn build_chain_egress_dialer_for_test_root(
    proxy_url: &str,
    proxy_root_certificate: Vec<u8>,
) -> Result<EggressDialer, TransportError> {
    let proxy_roots = if proxy_root_certificate.is_empty() {
        Vec::new()
    } else {
        vec![proxy_root_certificate]
    };
    let tls_config = Arc::new(build_tls_config(&proxy_roots)?);
    build_chain_egress_dialer(proxy_url, Some(&tls_config))
}

#[cfg(feature = "eggress-ssh-fallback")]
fn build_chain_egress_dialer(
    proxy_url: &str,
    tls_config: Option<&Arc<ClientConfig>>,
) -> Result<EggressDialer, TransportError> {
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
    Ok(EggressDialer {
        inner: EggressDialerInner::Chain {
            executor: Arc::new(executor),
            chain: Arc::new(chain),
        },
    })
}

fn build_egress_connector(
    proxy_url: &str,
) -> Result<eggress_embed::outbound::OutboundConnector, eggress_embed::EggressError> {
    eggress_embed::outbound::OutboundConnector::from_pproxy_uri(proxy_url)
}

/// Classify an Eggress embed route failure into an Eggfetch dial kind.
///
/// The pinned embed API reports route execution as `EggressError::Runtime`
/// with the typed `ChainError` already rendered to a redacted string, so the
/// variant selects the category and only the route-failure bucket uses
/// conservative message predicates. Those predicates mirror the previous
/// transport classification exactly; if a future Eggress embed API exposes
/// the typed route error here, replace the predicates with a direct match.
/// The original error is retained as the `DialError` source. Eggress
/// documents its messages as credential-redacted, and the fixed display
/// message below carries no route input.
fn map_egress_dial_error(error: eggress_embed::EggressError) -> DialError {
    const MESSAGE: &str = "eggress route establishment failed";
    match &error {
        eggress_embed::EggressError::Runtime(detail) => {
            let message = detail.to_ascii_lowercase();
            if message.contains("timed out") || message.contains("timeout") {
                DialError::with_source(DialErrorKind::Timeout, MESSAGE, error)
            } else if message.contains("auth")
                || message.contains("credential")
                || message.contains("password")
                || message.contains("407")
            {
                DialError::with_source(DialErrorKind::Authentication, MESSAGE, error)
            } else if message.contains("target") || message.contains("destination") {
                DialError::with_source(DialErrorKind::Rejected, MESSAGE, error)
            } else {
                DialError::with_source(DialErrorKind::Connection, MESSAGE, error)
            }
        }
        _ => DialError::with_source(DialErrorKind::Other, MESSAGE, error),
    }
}

/// Classify a typed chain-executor route failure into an Eggfetch dial kind.
///
/// Unlike the embed facade path, the executor preserves `ChainError`
/// structure, so this mapping uses typed variants and predicates only. The
/// original error is retained as the `DialError` source; the fixed display
/// message carries no route input.
#[cfg(feature = "eggress-ssh-fallback")]
fn map_chain_dial_error(error: eggress_core::chain::ChainError) -> DialError {
    use eggress_core::ConnectError;

    const MESSAGE: &str = "eggress route establishment failed";
    let kind = match &error {
        eggress_core::chain::ChainError::ConnectFailed { source, .. } => match source {
            ConnectError::Timeout => DialErrorKind::Timeout,
            ConnectError::ReservedTarget(_) => DialErrorKind::Rejected,
            ConnectError::Io(error) if error.kind() == std::io::ErrorKind::TimedOut => {
                DialErrorKind::Timeout
            }
            ConnectError::ConnectionRefused
            | ConnectError::DnsResolution(_)
            | ConnectError::TlsHandshake(_)
            | ConnectError::Io(_) => DialErrorKind::Connection,
        },
        eggress_core::chain::ChainError::HandshakeFailed { source, .. } => {
            classify_chain_handshake_source(source.as_ref())
        }
        eggress_core::chain::ChainError::EmptyChain
        | eggress_core::chain::ChainError::InvalidChain { .. } => DialErrorKind::Other,
    };
    DialError::with_source(kind, MESSAGE, error)
}

/// Classify a chain handshake failure from its typed source chain.
///
/// Authentication evidence wins over generic I/O evidence; route-level TLS
/// failures stay route connection failures and never masquerade as origin
/// TLS. Unrecognized sources are `Other`: they remain fail-closed through
/// the proxy error category without claiming a more specific cause.
#[cfg(feature = "eggress-ssh-fallback")]
fn classify_chain_handshake_source(source: &(dyn StdError + 'static)) -> DialErrorKind {
    use eggress_core::{AuthError, chain::HandshakeError};

    if contains_source::<AuthError>(source) {
        return DialErrorKind::Authentication;
    }
    let mut current: Option<&(dyn StdError + 'static)> = Some(source);
    while let Some(next) = current {
        if let Some(handshake) = next.downcast_ref::<HandshakeError>() {
            match handshake {
                HandshakeError::AuthFailed => return DialErrorKind::Authentication,
                HandshakeError::Protocol(_) => return DialErrorKind::Rejected,
                HandshakeError::ConnectionRefused => return DialErrorKind::Connection,
                HandshakeError::Io(error) if error.kind() == std::io::ErrorKind::TimedOut => {
                    return DialErrorKind::Timeout;
                }
                HandshakeError::Io(_) | HandshakeError::Other(_) => {
                    return DialErrorKind::Connection;
                }
            }
        }
        if let Some(error) = next.downcast_ref::<std::io::Error>() {
            match error.kind() {
                std::io::ErrorKind::TimedOut => return DialErrorKind::Timeout,
                std::io::ErrorKind::ConnectionRefused
                | std::io::ErrorKind::ConnectionReset
                | std::io::ErrorKind::ConnectionAborted
                | std::io::ErrorKind::NotConnected => return DialErrorKind::Connection,
                _ => {}
            }
        }
        current = next.source();
    }
    DialErrorKind::Other
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

/// Rustls client configuration for the test-only Eggress TLS override.
///
/// Production route establishment uses Eggress's own system-root TLS stack;
/// only `new_with_proxy_test_root` supplies an explicit test CA, and only to
/// the route executor. Origin TLS always stays in Eggfetch.
#[cfg(feature = "test-support")]
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

/// Build the Eggfetch client shared by direct and proxied routes.
///
/// Semantics: HTTP/1.1 only, no
/// hidden canceled-request retry, physical live-connection admission via
/// `PhysicalConnectionPolicy` (never the logical request-concurrency limit),
/// Eggfetch idle-pool timeout plus per-host idle cap, connect timeout via
/// Eggfetch's connect facility, established read/write inactivity via
/// `TransportIoTimeout`, WebPKI roots plus explicit additional CA roots, and
/// no high-level Eggfetch retries, redirects, or request timeout layers.
///
/// A proxied route installs the account's `EggressDialer` as the client's
/// only physical route. Eggfetch still performs origin TLS across the dialed
/// stream using the same TLS configuration as the direct path, so proxy and
/// origin trust planes remain separate. Each `ProviderHttpClient` builds its
/// own Eggfetch `Client`, preserving per-account pool isolation.
fn build_eggfetch_client(
    config: &ProviderHttpConfig,
    dialer: Option<EggressDialer>,
) -> Result<eggfetch_core::Client, TransportError> {
    let mut tls_builder = eggfetch_core::TlsConfig::builder().trust_store(TrustStore::WebPkiOnly);
    if !config.additional_root_certificates.is_empty() {
        tls_builder = tls_builder
            .additional_ca_certificate_der(config.additional_root_certificates.clone())
            .map_err(|_| TransportError::Configuration)?;
    }
    let tls_config = tls_builder.build();
    let mut builder = eggfetch_core::Client::builder();
    builder = builder
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
        // Established transport inactivity guards. No high-level Timeout
        // read/write layer is added; doubling layers would change error
        // precedence for body/header stalls.
        .transport_io_timeout(TransportIoTimeout {
            read: Some(config.read_timeout),
            write: Some(config.write_timeout),
        })
        // Idle-pool reuse and expiry.
        .idle_timeout(config.keepalive_timeout)
        .max_idle_connections_per_host(config.max_keepalive)
        // Connection establishment only; admission wait stays in the physical
        // policy and classifies as PoolTimeout.
        .timeout(EggfetchTimeout {
            connect: Some(config.connect_timeout),
            ..Default::default()
        })
        .tls_config(tls_config);
    if let Some(dialer) = dialer {
        // The custom dialer is incompatible with Eggfetch's built-in proxy
        // routing, which stays disabled: the dialer owns the only physical
        // route, so route failures stay fail-closed without direct fallback.
        builder = builder.dialer(dialer);
    }
    Ok(builder.build())
}

fn safe_authority(uri: &Uri) -> Option<&Authority> {
    uri.authority()
}

/// Translate Eggfetch errors to the stable `TransportError`
/// contract without leaking Eggfetch types above this module.
///
/// Typed inspection order preserves specific timeout categories before broad
/// connection failures: physical admission, established I/O direction,
/// connect establishment, TLS, framing, custom dialer kinds, then ordinary
/// connection failures.
///
/// `proxy_transport` records whether the owning client dials through an
/// Eggress route. Physical admission timeout stays `PoolTimeout` on both
/// routes, but connection-establishment timeout on a proxied route is an
/// Eggress route timeout, never a direct connection failure.
fn map_eggfetch_error(
    error: &eggfetch_core::Error,
    fallback: TransportError,
    proxy_transport: bool,
) -> TransportError {
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
    // 4. Phase-aware timeouts. Only connect is configured on either route;
    // read/write/total/proxy phases are mapped defensively so a future
    // misconfiguration cannot silently become Connect. A connect timeout on
    // a proxied route covers Eggress route establishment plus origin TLS,
    // so it keeps the established Eggress timeout category.
    if let EggfetchError::Timeout { phase, .. } = error {
        return match phase {
            TimeoutPhase::Pool => TransportError::PoolTimeout,
            TimeoutPhase::Connect if proxy_transport => TransportError::ProxyConnectTimeout,
            TimeoutPhase::Connect => TransportError::ConnectTimeout,
            TimeoutPhase::Read => TransportError::ReadTimeout,
            TimeoutPhase::Write => TransportError::WriteTimeout,
            // No total deadline is configured on either route. Map to a
            // timeout category rather than a connection failure.
            TimeoutPhase::Total => TransportError::ReadTimeout,
            TimeoutPhase::ProxyConnect | TimeoutPhase::ProxyTls => {
                TransportError::ProxyConnectTimeout
            }
        };
    }
    // 5. TLS establishment/verification stays Tls, including rustls sources
    // wrapped through the Eggfetch engine's connector path.
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
    // Canceled requests map to Cancelled so coordinator accounting can
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
    // Proxy-route errors retain proxy categories; the direct path never
    // configures a proxy so these are defensive.
    match error {
        EggfetchError::InvalidProxyUrl(_)
        | EggfetchError::ProxyConnect(_)
        | EggfetchError::MalformedProxyResponse(_) => return TransportError::ProxyConnect,
        EggfetchError::ProxyAuthRequired => return TransportError::ProxyAuthentication,
        EggfetchError::ProxyConnectRejected { .. } => return TransportError::ProxyTargetConnect,
        _ => {}
    }
    // Custom dialer failures are Eggress route failures by construction:
    // only proxied clients install a dialer. The typed dial kind selects
    // the established Eggress category, keeping route authentication and
    // rejection distinct from ordinary direct connection failures and from
    // origin TLS.
    if let EggfetchError::CustomTransport(_) = error {
        return match error.custom_transport_error().map(DialError::kind) {
            Some(DialErrorKind::Timeout) => TransportError::ProxyConnectTimeout,
            Some(DialErrorKind::Authentication) => TransportError::ProxyAuthentication,
            Some(DialErrorKind::Rejected) => TransportError::ProxyTargetConnect,
            Some(DialErrorKind::Connection) | Some(DialErrorKind::Other) | None => {
                TransportError::ProxyConnect
            }
        };
    }
    // 7. Ordinary direct connection failures stay Connect. This includes
    // typed Connect, HyperClient connect failures, and I/O connection
    // refusal observed through the Eggfetch connector.
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
    // Remaining engine and I/O errors without a more specific classification fall back
    // to the caller phase: Write for dispatch, Read for body polling.
    fallback
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

    #[test]
    fn egress_route_failures_classify_into_stable_dial_kinds() {
        use super::{DialErrorKind, map_egress_dial_error};

        let cases = [
            (
                eggress_embed::EggressError::Runtime("connection timed out".to_owned()),
                DialErrorKind::Timeout,
            ),
            (
                eggress_embed::EggressError::Runtime("proxy timeout".to_owned()),
                DialErrorKind::Timeout,
            ),
            (
                eggress_embed::EggressError::Runtime(
                    "407 Proxy Authentication Required".to_owned(),
                ),
                DialErrorKind::Authentication,
            ),
            (
                eggress_embed::EggressError::Runtime("invalid credentials".to_owned()),
                DialErrorKind::Authentication,
            ),
            (
                eggress_embed::EggressError::Runtime(
                    "proxy could not connect to the target".to_owned(),
                ),
                DialErrorKind::Rejected,
            ),
            (
                eggress_embed::EggressError::Runtime("destination rejected".to_owned()),
                DialErrorKind::Rejected,
            ),
            (
                eggress_embed::EggressError::Runtime("connection refused".to_owned()),
                DialErrorKind::Connection,
            ),
            (
                eggress_embed::EggressError::Config("bad chain".to_owned()),
                DialErrorKind::Other,
            ),
        ];
        for (error, expected) in cases {
            assert_eq!(map_egress_dial_error(error).kind(), expected);
        }
    }

    #[test]
    fn egress_dial_errors_are_secret_free() {
        use super::map_egress_dial_error;

        let marker = "dial-secret-marker";
        let error = map_egress_dial_error(eggress_embed::EggressError::Runtime(format!(
            "route to proxy failed for caller {marker}"
        )));
        // The fixed display message carries no route input; only the
        // redacted source chain may retain the detail, and `DialError`
        // redacts source `Debug` output by construction.
        assert!(!error.message().contains(marker));
        assert!(!format!("{error}").contains(marker));
        assert!(!format!("{error:?}").contains(marker));
        assert!(std::error::Error::source(&error).is_some());
    }

    #[test]
    fn custom_dial_kinds_map_to_stable_proxy_transport_errors() {
        use super::{DialError, DialErrorKind, map_eggfetch_error};
        use eggfetch_core::Error as EggfetchError;

        for (kind, expected) in [
            (DialErrorKind::Timeout, TransportError::ProxyConnectTimeout),
            (
                DialErrorKind::Authentication,
                TransportError::ProxyAuthentication,
            ),
            (DialErrorKind::Rejected, TransportError::ProxyTargetConnect),
            (DialErrorKind::Connection, TransportError::ProxyConnect),
            (DialErrorKind::Other, TransportError::ProxyConnect),
        ] {
            let dial = DialError::new(kind, "route failed");
            let error = EggfetchError::CustomTransport(std::sync::Arc::new(dial));
            assert_eq!(
                map_eggfetch_error(&error, TransportError::Write, true),
                expected,
                "dial kind {kind:?}"
            );
        }
    }

    #[test]
    fn connect_timeout_keeps_route_aware_classification() {
        use super::map_eggfetch_error;
        use eggfetch_core::{Error as EggfetchError, TimeoutPhase};

        let error = EggfetchError::Timeout {
            phase: TimeoutPhase::Connect,
            elapsed: std::time::Duration::from_secs(2),
        };
        assert_eq!(
            map_eggfetch_error(&error, TransportError::Write, true),
            TransportError::ProxyConnectTimeout
        );
        assert_eq!(
            map_eggfetch_error(&error, TransportError::Write, false),
            TransportError::ConnectTimeout
        );
    }

    #[cfg(feature = "eggress-ssh-fallback")]
    #[test]
    fn chain_route_failures_classify_from_typed_variants() {
        use super::{DialErrorKind, map_chain_dial_error};
        use eggress_core::{AuthError, ConnectError, chain::ChainError};

        let cases = [
            (
                ChainError::ConnectFailed {
                    hop_index: 0,
                    endpoint: "127.0.0.1:8080".to_owned(),
                    source: ConnectError::Timeout,
                },
                DialErrorKind::Timeout,
            ),
            (
                ChainError::ConnectFailed {
                    hop_index: 0,
                    endpoint: "127.0.0.1:8080".to_owned(),
                    source: ConnectError::ConnectionRefused,
                },
                DialErrorKind::Connection,
            ),
            (
                ChainError::ConnectFailed {
                    hop_index: 0,
                    endpoint: "127.0.0.1:8080".to_owned(),
                    source: ConnectError::ReservedTarget("10.0.0.1".parse().unwrap()),
                },
                DialErrorKind::Rejected,
            ),
            (
                ChainError::HandshakeFailed {
                    hop_index: 0,
                    protocol: "Http".to_owned(),
                    source: Box::new(AuthError::InvalidCredentials),
                },
                DialErrorKind::Authentication,
            ),
            (
                ChainError::HandshakeFailed {
                    hop_index: 1,
                    protocol: "tls".to_owned(),
                    source: Box::new(std::io::Error::new(
                        std::io::ErrorKind::TimedOut,
                        "route TLS stall",
                    )),
                },
                DialErrorKind::Timeout,
            ),
            (
                ChainError::InvalidChain {
                    reason: "no handler".to_owned(),
                },
                DialErrorKind::Other,
            ),
        ];
        for (error, expected) in cases {
            assert_eq!(map_chain_dial_error(error).kind(), expected);
        }
    }

    #[cfg(feature = "eggress-ssh-fallback")]
    #[test]
    fn dial_targets_preserve_domain_names_and_ports() {
        use super::target_addr_for_dial;
        use eggress_core::TargetHost;

        let domain = target_addr_for_dial("provider.example", 443);
        assert_eq!(
            domain.host,
            TargetHost::Domain("provider.example".to_owned())
        );
        assert_eq!(domain.port, 443);
        let literal = target_addr_for_dial("127.0.0.1", 80);
        assert_eq!(literal.host, TargetHost::Ip("127.0.0.1".parse().unwrap()));
        assert_eq!(literal.port, 80);
    }
}
