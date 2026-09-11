//! Rust-native release discovery, integrity verification, and self-update.
//!
//! The release authority is deliberately kept behind this small service.  It
//! uses the same Hyper/Rustls transport family as provider traffic, but does
//! not use provider routing or credentials: update metadata is public release
//! metadata and must never inherit an account's proxy or secret headers.

use std::{
    env,
    fs::{self, File, OpenOptions},
    future::Future,
    io::{self, Write},
    path::{Path, PathBuf},
    process::Stdio,
    sync::{Arc, Mutex},
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use bytes::Bytes;
use http::{Method, Request, StatusCode, Uri, header};
use http_body_util::{BodyExt, Empty};
use hyper::body::Incoming;
use hyper_rustls::HttpsConnectorBuilder;
use hyper_util::{
    client::legacy::{Client, connect::HttpConnector},
    rt::{TokioExecutor, TokioTimer},
};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use thiserror::Error;
use tokio::{
    io::{AsyncRead, AsyncReadExt},
    process::Command,
    time::timeout,
};

pub use super::catalog::{CatalogRelease, ReleaseCatalog, ReleaseEra};
pub use super::provenance::{
    DirectUrlMetadata, InstallProvenance, PackageMetadata, ProvenanceEnvironment,
};
use crate::version::PACKAGE_VERSION;

pub const DEFAULT_RELEASE_API: &str = "https://api.github.com/repos/eggstack/eggpool/releases";
const DEFAULT_USER_AGENT: &str = "eggpool-rust-update";
const MAX_METADATA_BYTES: usize = 2 * 1024 * 1024;
const MAX_ARTIFACT_BYTES: usize = 128 * 1024 * 1024;
const MAX_REDIRECTS: usize = 3;
const CONNECT_TIMEOUT: Duration = Duration::from_secs(5);
const READ_TIMEOUT: Duration = Duration::from_secs(10);
const OVERALL_TIMEOUT: Duration = Duration::from_secs(20);
const SELF_CHECK_TIMEOUT: Duration = Duration::from_secs(5);
const MAX_SELF_CHECK_OUTPUT: usize = 8192;

type HttpClient = Client<hyper_rustls::HttpsConnector<HttpConnector>, Empty<Bytes>>;

/// A supported EggPool release version using the repository's PEP-440
/// compatible release subset.  The representation keeps the original
/// normalized spelling for safe tag/artifact construction.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReleaseVersion {
    normalized: String,
    release: Vec<u64>,
    rank: u8,
    suffix: u64,
}

impl ReleaseVersion {
    pub fn parse(raw: &str) -> Result<Self, UpdateError> {
        let value = raw.trim();
        let normalized = value
            .strip_prefix('v')
            .or_else(|| value.strip_prefix('V'))
            .unwrap_or(value);
        if normalized.is_empty() {
            return Err(UpdateError::InvalidVersion);
        }

        let (release_text, suffix_text) = split_release_suffix(normalized)?;
        let mut release = Vec::new();
        for part in release_text.split('.') {
            if part.is_empty() {
                return Err(UpdateError::InvalidVersion);
            }
            release.push(
                part.parse::<u64>()
                    .map_err(|_| UpdateError::InvalidVersion)?,
            );
        }
        while release.len() > 1 && release.last() == Some(&0) {
            release.pop();
        }

        let (rank, suffix) = match suffix_text {
            None => (4, 0),
            Some((kind, number)) => {
                let suffix = number.unwrap_or(0);
                match kind {
                    "dev" => (0, suffix),
                    "a" => (1, suffix),
                    "b" => (2, suffix),
                    "rc" => (3, suffix),
                    "post" => (5, suffix),
                    _ => return Err(UpdateError::InvalidVersion),
                }
            }
        };
        Ok(Self {
            normalized: normalized.to_owned(),
            release,
            rank,
            suffix,
        })
    }

    pub fn normalize(raw: &str) -> Result<String, UpdateError> {
        Ok(Self::parse(raw)?.normalized)
    }

    pub fn is_newer_than(&self, current: &Self) -> bool {
        self.cmp_key() > current.cmp_key()
    }

    pub fn equivalent(&self, other: &Self) -> bool {
        self.cmp_key() == other.cmp_key()
    }

    pub fn as_str(&self) -> &str {
        &self.normalized
    }

    fn cmp_key(&self) -> (&[u64], u8, u64) {
        (&self.release, self.rank, self.suffix)
    }

    pub(crate) fn ordering_key(&self) -> (Vec<u64>, u8, u64) {
        (self.release.clone(), self.rank, self.suffix)
    }
}

type VersionSuffix<'a> = Option<(&'a str, Option<u64>)>;

fn split_release_suffix(value: &str) -> Result<(&str, VersionSuffix<'_>), UpdateError> {
    let mut boundary = value.len();
    for (index, character) in value.char_indices() {
        if !character.is_ascii_digit() && character != '.' {
            boundary = index;
            break;
        }
    }
    let release = value[..boundary].trim_end_matches('.');
    if release.is_empty() {
        return Err(UpdateError::InvalidVersion);
    }
    if boundary == value.len() {
        return Ok((release, None));
    }
    let suffix = value[boundary..].trim_start_matches(['.', '-', '_']);
    let suffix = suffix.to_ascii_lowercase();
    let (kind, remainder) = ["post", "dev", "rc", "a", "b"]
        .iter()
        .find_map(|kind| suffix.strip_prefix(kind).map(|rest| (*kind, rest)))
        .ok_or(UpdateError::InvalidVersion)?;
    let remainder = remainder.trim_start_matches(['.', '-', '_']);
    let number = if remainder.is_empty() {
        None
    } else {
        Some(
            remainder
                .parse::<u64>()
                .map_err(|_| UpdateError::InvalidVersion)?,
        )
    };
    Ok((release, Some((kind, number))))
}

/// Requested release target.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ReleaseTarget {
    Latest,
    Exact(ReleaseVersion),
}

impl ReleaseTarget {
    pub fn parse(raw: Option<&str>) -> Result<Self, UpdateError> {
        match raw {
            None => Ok(Self::Latest),
            Some(value) => ReleaseVersion::parse(value).map(Self::Exact),
        }
    }
}

/// Platform identity used by the release asset contract.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Platform {
    pub os: String,
    pub architecture: String,
}

impl Platform {
    pub fn current() -> Self {
        Self {
            os: std::env::consts::OS.to_owned(),
            architecture: std::env::consts::ARCH.to_owned(),
        }
    }

    pub fn artifact_name(&self, version: &ReleaseVersion) -> String {
        format!(
            "eggpool-{}-{}-{}",
            version.as_str(),
            self.os,
            self.architecture
        )
    }
}

/// The minimum M11-compatible raw executable asset contract.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ArtifactDescriptor {
    pub name: String,
    pub download_url: Uri,
    pub sha256: [u8; 32],
    pub size: Option<u64>,
}

#[derive(Debug, Clone)]
pub struct ReleaseMetadata {
    pub version: ReleaseVersion,
    pub prerelease: bool,
    pub artifact: Option<ArtifactDescriptor>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct UpdateInfo {
    pub current_version: String,
    pub latest_version: String,
    pub update_available: bool,
    pub install_method: String,
    pub update_command: String,
    pub last_check_at: u64,
    pub last_check_error: String,
}

impl Default for UpdateInfo {
    fn default() -> Self {
        Self {
            current_version: PACKAGE_VERSION.to_owned(),
            latest_version: String::new(),
            update_available: false,
            install_method: "rust".to_owned(),
            update_command: "eggpool update".to_owned(),
            last_check_at: 0,
            last_check_error: String::new(),
        }
    }
}

#[derive(Debug, Error)]
pub enum UpdateError {
    #[error("invalid EggPool release version")]
    InvalidVersion,
    #[error("release metadata request failed")]
    MetadataTransport,
    #[error("release metadata returned HTTP {0}")]
    MetadataStatus(u16),
    #[error("release metadata response is too large")]
    MetadataTooLarge,
    #[error("release metadata is malformed")]
    MetadataJson,
    #[error("release metadata redirect is not trusted")]
    UntrustedRedirect,
    #[error("release metadata is a draft or unsupported pre-release")]
    UnsupportedRelease,
    #[error("no compatible Rust artifact for this platform")]
    NoCompatibleArtifact,
    #[error("release artifact URL is invalid")]
    InvalidArtifactUrl,
    #[error("release artifact request failed")]
    ArtifactTransport,
    #[error("release artifact returned HTTP {0}")]
    ArtifactStatus(u16),
    #[error("release artifact is too large")]
    ArtifactTooLarge,
    #[error("release artifact size does not match its metadata")]
    ArtifactSizeMismatch,
    #[error("release artifact integrity evidence is missing")]
    IntegrityMissing,
    #[error("release artifact SHA-256 digest is malformed")]
    IntegrityMalformed,
    #[error("release artifact SHA-256 digest does not match")]
    IntegrityMismatch,
    #[error("installable release catalog is malformed")]
    CatalogMalformed,
    #[error("requested release is not in the installable catalog")]
    TargetNotCatalogued,
    #[error("requested release is unavailable or yanked")]
    TargetUnavailable,
    #[error("requested release is unsupported on this platform")]
    UnsupportedPlatform,
    #[error("requested release is incompatible with the owning Python environment")]
    IncompatiblePythonEnvironment,
    #[error("the current install provenance is ambiguous")]
    AmbiguousProvenance,
    #[error("source checkouts must be transitioned by the developer workflow")]
    SourceCheckout,
    #[error("the current database/config state is incompatible with the target")]
    IncompatibleDatabaseConfig,
    #[error("package manager executable is unavailable")]
    ManagerUnavailable,
    #[error("package manager metadata is malformed")]
    ManagerMetadataMalformed,
    #[error("package manager transition timed out")]
    ManagerTimeout,
    #[error("package manager returned a non-zero exit status")]
    ManagerFailed,
    #[error("package manager output exceeded the retained limit")]
    ManagerOutputTooLarge,
    #[error("installed version does not match the requested release")]
    WrongInstalledVersion,
    #[error("installed executable ownership changed unexpectedly")]
    OwnershipChanged,
    #[error("target config validation failed")]
    TargetConfigInvalid,
    #[error("standalone binaries can transition only to Rust release assets")]
    StandaloneTargetUnsupported,
    #[error("current executable path is unsupported or not writable")]
    UnsupportedInstallPath,
    #[error("current executable has unsafe links or ownership")]
    UnsafeExecutable,
    #[error("another update is already in progress")]
    UpdateInProgress,
    #[error(
        "automatic rollback failed after target {target_version} was installed; previous version {previous_version} requires manual recovery with `{recovery_command}` ({reason})"
    )]
    RollbackFailed {
        previous_version: String,
        target_version: String,
        reason: String,
        recovery_command: String,
    },
    #[error("staged executable self-check failed")]
    SelfCheckFailed,
    #[error("executable replacement failed")]
    ReplacementFailed,
    #[error("server restart after update failed")]
    RestartFailed,
    #[error("I/O operation failed")]
    Io(#[source] io::Error),
}

impl UpdateError {
    pub fn category(&self) -> &'static str {
        match self {
            Self::InvalidVersion => "invalid_version",
            Self::MetadataTransport => "metadata_transport",
            Self::MetadataStatus(_) => "metadata_status",
            Self::MetadataTooLarge => "metadata_too_large",
            Self::MetadataJson => "metadata_json",
            Self::UntrustedRedirect => "untrusted_redirect",
            Self::UnsupportedRelease => "unsupported_release",
            Self::NoCompatibleArtifact => "no_compatible_artifact",
            Self::InvalidArtifactUrl => "invalid_artifact_url",
            Self::ArtifactTransport => "artifact_transport",
            Self::ArtifactStatus(_) => "artifact_status",
            Self::ArtifactTooLarge => "artifact_too_large",
            Self::ArtifactSizeMismatch => "artifact_size_mismatch",
            Self::IntegrityMissing => "integrity_missing",
            Self::IntegrityMalformed => "integrity_malformed",
            Self::IntegrityMismatch => "integrity_mismatch",
            Self::CatalogMalformed => "catalog_malformed",
            Self::TargetNotCatalogued => "target_not_catalogued",
            Self::TargetUnavailable => "target_unavailable",
            Self::UnsupportedPlatform => "unsupported_platform",
            Self::IncompatiblePythonEnvironment => "incompatible_python_environment",
            Self::AmbiguousProvenance => "install_provenance_ambiguous",
            Self::SourceCheckout => "source_checkout",
            Self::IncompatibleDatabaseConfig => "db_config_rollback_incompatible",
            Self::ManagerUnavailable => "package_manager_unavailable",
            Self::ManagerMetadataMalformed => "package_manager_metadata_malformed",
            Self::ManagerTimeout => "package_manager_timeout",
            Self::ManagerFailed => "package_manager_failure",
            Self::ManagerOutputTooLarge => "package_manager_output_too_large",
            Self::WrongInstalledVersion => "post_install_wrong_version",
            Self::OwnershipChanged => "ownership_changed_unexpectedly",
            Self::TargetConfigInvalid => "target_config_invalid",
            Self::StandaloneTargetUnsupported => "standalone_target_unsupported",
            Self::UnsupportedInstallPath => "unsupported_install_path",
            Self::UnsafeExecutable => "unsafe_executable",
            Self::UpdateInProgress => "update_in_progress",
            Self::RollbackFailed { .. } => "rollback_failed",
            Self::SelfCheckFailed => "self_check_failed",
            Self::ReplacementFailed => "replacement_failed",
            Self::RestartFailed => "restart_failed",
            Self::Io(_) => "io",
        }
    }
}

/// Bounded Hyper/Rustls release metadata and artifact client.
#[derive(Clone)]
pub struct ReleaseClient {
    client: HttpClient,
    release_api: Uri,
    platform: Platform,
    user_agent: String,
}

impl std::fmt::Debug for ReleaseClient {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("ReleaseClient")
            .field("release_api", &self.release_api.authority())
            .field("platform", &self.platform)
            .finish()
    }
}

impl ReleaseClient {
    pub fn new() -> Result<Self, UpdateError> {
        Self::with_release_api(DEFAULT_RELEASE_API)
    }

    pub fn with_release_api(value: &str) -> Result<Self, UpdateError> {
        let _ = rustls::crypto::ring::default_provider().install_default();
        let release_api: Uri = value.parse().map_err(|_| UpdateError::InvalidArtifactUrl)?;
        if release_api.scheme_str() != Some("https") && release_api.scheme_str() != Some("http") {
            return Err(UpdateError::InvalidArtifactUrl);
        }
        let mut connector = HttpConnector::new();
        connector.enforce_http(false);
        connector.set_connect_timeout(Some(CONNECT_TIMEOUT));
        let https = HttpsConnectorBuilder::new()
            .with_webpki_roots()
            .https_or_http()
            .enable_http1()
            .wrap_connector(connector);
        let mut builder = Client::builder(TokioExecutor::new());
        builder
            .retry_canceled_requests(false)
            .pool_timer(TokioTimer::new())
            .pool_idle_timeout(Duration::from_secs(30));
        Ok(Self {
            client: builder.build(https),
            release_api,
            platform: Platform::current(),
            user_agent: format!("{DEFAULT_USER_AGENT}/{PACKAGE_VERSION}"),
        })
    }

    pub fn with_platform(mut self, platform: Platform) -> Self {
        self.platform = platform;
        self
    }

    pub fn release_api(&self) -> &Uri {
        &self.release_api
    }

    pub async fn resolve(&self, target: &ReleaseTarget) -> Result<ReleaseMetadata, UpdateError> {
        let uri = match target {
            ReleaseTarget::Latest => join_path(&self.release_api, "latest")?,
            ReleaseTarget::Exact(version) => {
                join_path(&self.release_api, &format!("tags/v{}", version.as_str()))?
            }
        };
        let response: GithubRelease = self.get_json(uri, MAX_METADATA_BYTES).await?;
        if response.draft || (matches!(target, ReleaseTarget::Latest) && response.prerelease) {
            return Err(UpdateError::UnsupportedRelease);
        }
        let version = ReleaseVersion::parse(&response.tag_name)?;
        if let ReleaseTarget::Exact(requested) = target
            && version.cmp_key() != requested.cmp_key()
        {
            return Err(UpdateError::MetadataJson);
        }
        let expected_name = self.platform.artifact_name(&version);
        let mut artifact = None;
        for asset in response.assets {
            if asset.name != expected_name {
                continue;
            }
            let digest_text = asset
                .digest
                .as_deref()
                .ok_or(UpdateError::IntegrityMissing)?;
            let digest = parse_digest(digest_text).ok_or(UpdateError::IntegrityMalformed)?;
            let download_url = asset
                .browser_download_url
                .parse()
                .map_err(|_| UpdateError::InvalidArtifactUrl)?;
            artifact = Some(ArtifactDescriptor {
                name: asset.name,
                download_url,
                sha256: digest,
                size: asset.size,
            });
            break;
        }
        Ok(ReleaseMetadata {
            version,
            prerelease: response.prerelease,
            artifact,
        })
    }

    pub async fn download(&self, artifact: &ArtifactDescriptor) -> Result<Vec<u8>, UpdateError> {
        if !trusted_redirect(&self.release_api, &artifact.download_url) {
            return Err(UpdateError::UntrustedRedirect);
        }
        if artifact
            .size
            .is_some_and(|size| size > MAX_ARTIFACT_BYTES as u64)
        {
            return Err(UpdateError::ArtifactTooLarge);
        }
        let bytes = self
            .get_bytes(artifact.download_url.clone(), MAX_ARTIFACT_BYTES)
            .await?;
        if artifact.size.is_some_and(|size| size != bytes.len() as u64) {
            return Err(UpdateError::ArtifactSizeMismatch);
        }
        let digest = Sha256::digest(&bytes);
        if digest.as_slice() != artifact.sha256 {
            return Err(UpdateError::IntegrityMismatch);
        }
        Ok(bytes)
    }

    async fn get_json<T: for<'de> Deserialize<'de>>(
        &self,
        uri: Uri,
        max_bytes: usize,
    ) -> Result<T, UpdateError> {
        let bytes = self.get_bytes(uri, max_bytes).await?;
        serde_json::from_slice(&bytes).map_err(|_| UpdateError::MetadataJson)
    }

    async fn get_bytes(&self, uri: Uri, max_bytes: usize) -> Result<Vec<u8>, UpdateError> {
        timeout(OVERALL_TIMEOUT, self.get_bytes_inner(uri, max_bytes))
            .await
            .map_err(|_| UpdateError::MetadataTransport)?
    }

    async fn get_bytes_inner(
        &self,
        mut uri: Uri,
        max_bytes: usize,
    ) -> Result<Vec<u8>, UpdateError> {
        let original = uri.clone();
        for redirect in 0..=MAX_REDIRECTS {
            let request = Request::builder()
                .method(Method::GET)
                .uri(&uri)
                .header(header::ACCEPT, "application/json")
                .header(header::USER_AGENT, &self.user_agent)
                .body(Empty::<Bytes>::new())
                .map_err(|_| UpdateError::MetadataTransport)?;
            let response = timeout(OVERALL_TIMEOUT, self.client.request(request))
                .await
                .map_err(|_| UpdateError::MetadataTransport)?
                .map_err(|_| UpdateError::MetadataTransport)?;
            if is_redirect(response.status()) {
                if redirect == MAX_REDIRECTS {
                    return Err(UpdateError::UntrustedRedirect);
                }
                let location = response
                    .headers()
                    .get(header::LOCATION)
                    .and_then(|value| value.to_str().ok())
                    .ok_or(UpdateError::UntrustedRedirect)?;
                let next = resolve_redirect(&uri, location)?;
                if !trusted_redirect(&original, &next) {
                    return Err(UpdateError::UntrustedRedirect);
                }
                uri = next;
                continue;
            }
            if response.status() != StatusCode::OK {
                let status = response.status().as_u16();
                return if original.path().ends_with("/latest") || original.path().contains("/tags/")
                {
                    Err(UpdateError::MetadataStatus(status))
                } else {
                    Err(UpdateError::ArtifactStatus(status))
                };
            }
            return read_body(response.into_body(), max_bytes).await;
        }
        Err(UpdateError::UntrustedRedirect)
    }
}

#[derive(Debug, Deserialize)]
struct GithubRelease {
    tag_name: String,
    #[serde(default)]
    prerelease: bool,
    #[serde(default)]
    draft: bool,
    #[serde(default)]
    assets: Vec<GithubAsset>,
}

#[derive(Debug, Deserialize)]
struct GithubAsset {
    name: String,
    browser_download_url: String,
    digest: Option<String>,
    size: Option<u64>,
}

async fn read_body(mut body: Incoming, max_bytes: usize) -> Result<Vec<u8>, UpdateError> {
    let mut output = Vec::new();
    while let Some(frame) = timeout(READ_TIMEOUT, body.frame())
        .await
        .map_err(|_| UpdateError::MetadataTransport)?
        .transpose()
        .map_err(|_| UpdateError::MetadataTransport)?
    {
        if let Some(data) = frame.data_ref() {
            if output.len().saturating_add(data.len()) > max_bytes {
                return if max_bytes == MAX_METADATA_BYTES {
                    Err(UpdateError::MetadataTooLarge)
                } else {
                    Err(UpdateError::ArtifactTooLarge)
                };
            }
            output.extend_from_slice(data);
        }
    }
    Ok(output)
}

fn parse_digest(value: &str) -> Option<[u8; 32]> {
    let hex = value.strip_prefix("sha256:").unwrap_or(value);
    if hex.len() != 64 {
        return None;
    }
    let mut output = [0_u8; 32];
    for (index, pair) in hex.as_bytes().chunks_exact(2).enumerate() {
        output[index] = u8::from_str_radix(std::str::from_utf8(pair).ok()?, 16).ok()?;
    }
    Some(output)
}

fn join_path(base: &Uri, suffix: &str) -> Result<Uri, UpdateError> {
    let path = format!(
        "{}/{}",
        base.path().trim_end_matches('/'),
        suffix.trim_start_matches('/')
    );
    let mut parts = base.clone().into_parts();
    parts.path_and_query = Some(path.parse().map_err(|_| UpdateError::InvalidArtifactUrl)?);
    Uri::from_parts(parts).map_err(|_| UpdateError::InvalidArtifactUrl)
}

fn resolve_redirect(base: &Uri, location: &str) -> Result<Uri, UpdateError> {
    if let Ok(uri) = location.parse::<Uri>() {
        return Ok(uri);
    }
    if !location.starts_with('/') {
        return Err(UpdateError::UntrustedRedirect);
    }
    let mut parts = base.clone().into_parts();
    parts.path_and_query = Some(
        location
            .parse()
            .map_err(|_| UpdateError::UntrustedRedirect)?,
    );
    Uri::from_parts(parts).map_err(|_| UpdateError::UntrustedRedirect)
}

fn trusted_redirect(original: &Uri, next: &Uri) -> bool {
    let Some(scheme) = next.scheme_str() else {
        return false;
    };
    if scheme != "https" && scheme != "http" {
        return false;
    }
    if original.authority() == next.authority() {
        return true;
    }
    matches!(
        next.host(),
        Some(
            "api.github.com"
                | "github.com"
                | "objects.githubusercontent.com"
                | "release-assets.githubusercontent.com"
        )
    )
}

fn is_redirect(status: StatusCode) -> bool {
    matches!(
        status,
        StatusCode::MOVED_PERMANENTLY
            | StatusCode::FOUND
            | StatusCode::SEE_OTHER
            | StatusCode::TEMPORARY_REDIRECT
            | StatusCode::PERMANENT_REDIRECT
    )
}

/// Check-only service used by both the CLI and the process-owned task.
#[derive(Clone)]
pub struct UpdateService {
    client: ReleaseClient,
}

/// Bounded caller-owned facts needed before changing a package environment.
#[derive(Debug, Clone, Default)]
pub struct TransitionContext {
    /// `None` means the owning environment did not expose a parseable
    /// `pyvenv.cfg` version.  In that case a package transition is refused;
    /// K005 may supply an explicitly observed interpreter version.
    pub python_version: Option<(u8, u8)>,
    /// The caller's DB/config compatibility precheck.  K004 does not open or
    /// mutate the database and therefore requires this fact from its caller.
    pub db_config_compatible: bool,
    /// Optional config path used for the target's read-only `check-config`
    /// probe after the package manager returns.
    pub config_path: Option<PathBuf>,
    /// Explicit manager environment for deployment-owned package roots.  The
    /// values are fixed by the caller and are never copied into diagnostics.
    pub manager_environment: Option<Vec<(String, String)>>,
}

impl TransitionContext {
    pub const SAFE_DEFAULT: Self = Self {
        python_version: Some((3, 11)),
        db_config_compatible: true,
        config_path: None,
        manager_environment: None,
    };
}

/// An argv-only package-manager invocation.  The environment is intentionally
/// private so it cannot accidentally be serialized or included in diagnostics.
pub struct ManagerCommand {
    pub program: PathBuf,
    pub args: Vec<String>,
    environment: Vec<(String, String)>,
}

impl std::fmt::Debug for ManagerCommand {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("ManagerCommand")
            .field("program", &self.program)
            .field("args", &self.args)
            .field(
                "environment_keys",
                &self
                    .environment
                    .iter()
                    .map(|(key, _)| key)
                    .collect::<Vec<_>>(),
            )
            .finish()
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TransitionResult {
    pub target_version: String,
    pub manager: String,
    pub restarted: bool,
}

pub struct TransitionRequest<'a> {
    pub provenance: &'a InstallProvenance,
    pub target: &'a ReleaseTarget,
    pub current: &'a ReleaseVersion,
    pub executable: &'a Path,
    pub context: TransitionContext,
    pub was_running: bool,
    /// Held by the caller across service stop, package mutation, validation,
    /// and recovery.  `None` keeps the service boundary usable by callers
    /// that do not own a running deployment.
    pub guard: Option<TransitionGuard>,
}

/// One ownership guard for the complete deployed-service transition.
pub struct TransitionGuard {
    _lock: UpdateLock,
}

impl TransitionGuard {
    pub fn acquire(executable: &Path) -> Result<Self, UpdateError> {
        Ok(Self {
            _lock: UpdateLock::acquire(executable)?,
        })
    }
}

/// Package-manager transition authority.  It owns no package-manager state;
/// every invocation is reconstructed from the current provenance snapshot.
#[derive(Clone)]
pub struct PackageTransitionService {
    raw_updater: UpdateService,
    catalog: ReleaseCatalog,
    platform: Platform,
}

impl PackageTransitionService {
    pub fn new(raw_updater: UpdateService) -> Result<Self, UpdateError> {
        Ok(Self {
            raw_updater,
            catalog: ReleaseCatalog::embedded()?,
            platform: Platform::current(),
        })
    }

    pub fn with_catalog(
        raw_updater: UpdateService,
        catalog: ReleaseCatalog,
        platform: Platform,
    ) -> Self {
        Self {
            raw_updater,
            catalog,
            platform,
        }
    }

    pub fn catalog(&self) -> &ReleaseCatalog {
        &self.catalog
    }

    pub fn resolve_target(&self, target: &ReleaseTarget) -> Result<CatalogRelease, UpdateError> {
        self.catalog.resolve(target, &self.platform)
    }

    pub fn command_for(
        &self,
        provenance: &InstallProvenance,
        target: &CatalogRelease,
    ) -> Result<ManagerCommand, UpdateError> {
        build_manager_command(provenance, target)
    }

    pub async fn transition<F, Fut>(
        &self,
        request: TransitionRequest<'_>,
        restart: Option<F>,
    ) -> Result<TransitionResult, UpdateError>
    where
        F: Fn() -> Fut + Send + Sync,
        Fut: Future<Output = Result<(), UpdateError>> + Send,
    {
        let TransitionRequest {
            provenance,
            target,
            current,
            executable,
            context,
            was_running,
            guard,
        } = request;
        if let Some(metadata) = provenance.package_metadata() {
            let observed = ReleaseVersion::parse(&metadata.version)
                .map_err(|_| UpdateError::ManagerMetadataMalformed)?;
            if !observed.equivalent(current) {
                return Err(UpdateError::ManagerMetadataMalformed);
            }
        }
        let selection = self.resolve_target(target)?;
        if selection.version.equivalent(current)
            || (matches!(target, ReleaseTarget::Latest)
                && !selection.version.is_newer_than(current))
        {
            return Ok(TransitionResult {
                target_version: current.as_str().to_owned(),
                manager: provenance.manager_kind().unwrap_or("none").to_owned(),
                restarted: false,
            });
        }
        if !context.db_config_compatible {
            return Err(UpdateError::IncompatibleDatabaseConfig);
        }

        match provenance {
            InstallProvenance::SourceCheckout { .. } => Err(UpdateError::SourceCheckout),
            InstallProvenance::Ambiguous { evidence } => {
                if evidence
                    .iter()
                    .any(|item| item.contains("malformed") || item.contains("could not be read"))
                {
                    Err(UpdateError::ManagerMetadataMalformed)
                } else {
                    Err(UpdateError::AmbiguousProvenance)
                }
            }
            InstallProvenance::StandaloneRust { .. } => {
                if selection.era != ReleaseEra::Rust {
                    return Err(UpdateError::StandaloneTargetUnsupported);
                }
                let report = self
                    .raw_updater
                    .apply_current_executable_with_guard(
                        target,
                        executable,
                        was_running,
                        restart,
                        guard,
                    )
                    .await?;
                Ok(TransitionResult {
                    target_version: report.target_version,
                    manager: "standalone-rust".to_owned(),
                    restarted: report.restarted,
                })
            }
            InstallProvenance::UvTool { .. }
            | InstallProvenance::Pipx { .. }
            | InstallProvenance::PipEnvironment { .. } => {
                if selection.era == ReleaseEra::Python
                    && !context
                        .python_version
                        .is_some_and(|(major, minor)| (major, minor) >= (3, 11))
                {
                    return Err(UpdateError::IncompatiblePythonEnvironment);
                }
                let manager_executable = provenance.exposed_executable().unwrap_or(executable);
                let _guard = match guard {
                    Some(guard) => guard,
                    None => TransitionGuard::acquire(manager_executable)?,
                };
                let command = build_manager_command_with_environment(
                    provenance,
                    &selection,
                    context.manager_environment.as_deref().unwrap_or_default(),
                )?;
                if let Err(error) = run_manager(command).await {
                    if !matches!(error, UpdateError::ManagerUnavailable) {
                        return Err(self
                            .rollback_after_mutation(
                                provenance,
                                current,
                                &selection,
                                manager_executable,
                                &context,
                                was_running,
                                restart.as_ref(),
                                error,
                            )
                            .await);
                    }
                    return Err(error);
                }
                if let Err(error) = verify_package_install(
                    provenance,
                    &selection,
                    manager_executable,
                    context.config_path.as_deref(),
                )
                .await
                {
                    return Err(self
                        .rollback_after_mutation(
                            provenance,
                            current,
                            &selection,
                            manager_executable,
                            &context,
                            was_running,
                            restart.as_ref(),
                            error,
                        )
                        .await);
                }
                if was_running {
                    let restart_error = match restart.as_ref() {
                        Some(restart) => restart().await.err(),
                        None => Some(UpdateError::RestartFailed),
                    };
                    if let Some(error) = restart_error {
                        return Err(self
                            .rollback_after_mutation(
                                provenance,
                                current,
                                &selection,
                                manager_executable,
                                &context,
                                was_running,
                                restart.as_ref(),
                                error,
                            )
                            .await);
                    }
                }
                Ok(TransitionResult {
                    target_version: selection.version.as_str().to_owned(),
                    manager: provenance.manager_kind().unwrap_or("package").to_owned(),
                    restarted: was_running,
                })
            }
        }
    }

    #[allow(clippy::too_many_arguments)]
    async fn rollback_after_mutation<F, Fut>(
        &self,
        provenance: &InstallProvenance,
        current: &ReleaseVersion,
        target: &CatalogRelease,
        executable: &Path,
        context: &TransitionContext,
        was_running: bool,
        restart: Option<&F>,
        original: UpdateError,
    ) -> UpdateError
    where
        F: Fn() -> Fut + Send + Sync,
        Fut: Future<Output = Result<(), UpdateError>> + Send,
    {
        let previous_version = current.as_str().to_owned();
        let target_version = target.version.as_str().to_owned();
        let recovery_command = format!("eggpool update {previous_version}");
        let rollback_target = self
            .catalog
            .resolve(&ReleaseTarget::Exact(current.clone()), &self.platform);
        let rollback_result = async {
            let rollback_target = rollback_target?;
            if rollback_target.version.equivalent(&target.version) {
                return Err(UpdateError::TargetNotCatalogued);
            }
            let command = self.command_for(provenance, &rollback_target)?;
            run_manager(command).await?;
            verify_package_install(
                provenance,
                &rollback_target,
                executable,
                context.config_path.as_deref(),
            )
            .await?;
            if was_running {
                let restart = restart.ok_or(UpdateError::RestartFailed)?;
                restart().await?;
            }
            Ok::<(), UpdateError>(())
        }
        .await;

        match rollback_result {
            Ok(()) => original,
            Err(error) => UpdateError::RollbackFailed {
                previous_version,
                target_version,
                reason: error.category().to_owned(),
                recovery_command,
            },
        }
    }
}

fn build_manager_command(
    provenance: &InstallProvenance,
    target: &CatalogRelease,
) -> Result<ManagerCommand, UpdateError> {
    build_manager_command_with_environment(provenance, target, &[])
}

fn build_manager_command_with_environment(
    provenance: &InstallProvenance,
    target: &CatalogRelease,
    overrides: &[(String, String)],
) -> Result<ManagerCommand, UpdateError> {
    let requirement = exact_requirement(&target.version)?;
    let (program, args, manager) = match provenance {
        InstallProvenance::UvTool { manager, .. } => {
            let program = manager.clone().ok_or(UpdateError::ManagerUnavailable)?;
            (
                program,
                vec![
                    "tool".to_owned(),
                    "install".to_owned(),
                    "--force".to_owned(),
                    requirement,
                ],
                "uv",
            )
        }
        InstallProvenance::Pipx { manager, .. } => {
            let program = manager.clone().ok_or(UpdateError::ManagerUnavailable)?;
            (
                program,
                vec!["install".to_owned(), "--force".to_owned(), requirement],
                "pipx",
            )
        }
        InstallProvenance::PipEnvironment { python, .. } => {
            if !python.is_absolute() || !python.is_file() {
                return Err(UpdateError::ManagerUnavailable);
            }
            (
                python.clone(),
                vec![
                    "-m".to_owned(),
                    "pip".to_owned(),
                    "install".to_owned(),
                    "--upgrade".to_owned(),
                    "--force-reinstall".to_owned(),
                    requirement,
                ],
                "pip",
            )
        }
        _ => return Err(UpdateError::ManagerMetadataMalformed),
    };
    Ok(ManagerCommand {
        program,
        args,
        environment: allowed_environment(manager, overrides),
    })
}

fn exact_requirement(version: &ReleaseVersion) -> Result<String, UpdateError> {
    let value = version.as_str();
    if value.is_empty()
        || value.len() > 32
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
    {
        return Err(UpdateError::InvalidVersion);
    }
    Ok(format!("eggpool=={value}"))
}

fn allowed_environment(manager: &str, overrides: &[(String, String)]) -> Vec<(String, String)> {
    const COMMON: &[&str] = &[
        "PATH",
        "HOME",
        "USERPROFILE",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
    ];
    let mut names = COMMON.to_vec();
    match manager {
        "uv" => names.extend([
            "UV_TOOL_DIR",
            "UV_TOOL_BIN_DIR",
            "UV_CACHE_DIR",
            "UV_NO_CONFIG",
            "UV_INDEX_URL",
            "UV_DEFAULT_INDEX",
            "UV_EXTRA_INDEX_URL",
            "UV_FIND_LINKS",
            "UV_NO_INDEX",
            "UV_PYTHON",
        ]),
        "pipx" => names.extend([
            "PIPX_HOME",
            "PIPX_BIN_DIR",
            "PIPX_MAN_DIR",
            "PIPX_DEFAULT_PYTHON",
            "PIP_INDEX_URL",
            "PIP_FIND_LINKS",
            "PIP_NO_INDEX",
        ]),
        "pip" => names.extend([
            "PIP_INDEX_URL",
            "PIP_EXTRA_INDEX_URL",
            "PIP_TRUSTED_HOST",
            "PIP_FIND_LINKS",
            "PIP_NO_INDEX",
        ]),
        _ => {}
    }
    let mut environment = env::vars_os()
        .filter_map(|(key, value)| {
            let key = key.into_string().ok()?;
            if !names.contains(&key.as_str()) {
                return None;
            }
            Some((key, value.into_string().ok()?))
        })
        .collect::<Vec<_>>();
    for (key, value) in overrides {
        if names.contains(&key.as_str()) {
            environment.retain(|(existing, _)| existing != key);
            environment.push((key.clone(), value.clone()));
        }
    }
    environment
}

const MANAGER_TIMEOUT: Duration = Duration::from_secs(300);
const MAX_MANAGER_OUTPUT: usize = 64 * 1024;

async fn run_manager(command: ManagerCommand) -> Result<(), UpdateError> {
    let mut child = Command::new(&command.program);
    child
        .args(&command.args)
        .env_clear()
        .envs(command.environment)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);
    let mut child = child.spawn().map_err(|_| UpdateError::ManagerUnavailable)?;
    let stdout = child.stdout.take().ok_or(UpdateError::ManagerFailed)?;
    let stderr = child.stderr.take().ok_or(UpdateError::ManagerFailed)?;
    let mut stdout_task = Box::pin(tokio::spawn(read_manager_output(stdout)));
    let mut stderr_task = Box::pin(tokio::spawn(read_manager_output(stderr)));
    let mut child_wait = Box::pin(child.wait());
    let result = timeout(MANAGER_TIMEOUT, async {
        let mut stdout_done = false;
        let mut stderr_done = false;
        loop {
            tokio::select! {
                output = &mut stdout_task, if !stdout_done => {
                    stdout_done = true;
                    if matches!(output, Ok(Err(UpdateError::ManagerOutputTooLarge))) {
                        return Err(UpdateError::ManagerOutputTooLarge);
                    }
                    if output.is_err() || output.as_ref().is_ok_and(|result| result.is_err()) {
                        return Err(UpdateError::ManagerFailed);
                    }
                }
                output = &mut stderr_task, if !stderr_done => {
                    stderr_done = true;
                    if matches!(output, Ok(Err(UpdateError::ManagerOutputTooLarge))) {
                        return Err(UpdateError::ManagerOutputTooLarge);
                    }
                    if output.is_err() || output.as_ref().is_ok_and(|result| result.is_err()) {
                        return Err(UpdateError::ManagerFailed);
                    }
                }
                status = &mut child_wait => {
                    let status = status.map_err(|_| UpdateError::ManagerFailed)?;
                    if !stdout_done {
                        let _ = (&mut stdout_task).await;
                    }
                    if !stderr_done {
                        let _ = (&mut stderr_task).await;
                    }
                    return if status.success() {
                        Ok(())
                    } else {
                        Err(UpdateError::ManagerFailed)
                    };
                }
            }
        }
    })
    .await;
    drop(child_wait);
    match result {
        Ok(Ok(())) => Ok(()),
        Ok(Err(error)) => {
            let _ = child.kill().await;
            let _ = child.wait().await;
            Err(error)
        }
        Err(_) => {
            let _ = child.kill().await;
            let _ = child.wait().await;
            Err(UpdateError::ManagerTimeout)
        }
    }
}

async fn read_manager_output<R: AsyncRead + Unpin>(reader: R) -> Result<Vec<u8>, UpdateError> {
    let mut output = Vec::new();
    reader
        .take((MAX_MANAGER_OUTPUT + 1) as u64)
        .read_to_end(&mut output)
        .await
        .map_err(|_| UpdateError::ManagerFailed)?;
    if output.len() > MAX_MANAGER_OUTPUT {
        return Err(UpdateError::ManagerOutputTooLarge);
    }
    Ok(output)
}

async fn verify_package_install(
    previous: &InstallProvenance,
    target: &CatalogRelease,
    executable: &Path,
    config_path: Option<&Path>,
) -> Result<(), UpdateError> {
    let environment = ProvenanceEnvironment::current();
    let observed = InstallProvenance::detect_with(&environment, executable);
    if observed.manager_kind() != previous.manager_kind()
        || observed
            .package_metadata()
            .is_none_or(|metadata| metadata.version != target.version.as_str())
    {
        return Err(UpdateError::OwnershipChanged);
    }
    self_check(executable, &target.version)
        .await
        .map_err(|_| UpdateError::WrongInstalledVersion)?;
    if let Some(config_path) = config_path {
        config_self_check(executable, config_path)
            .await
            .map_err(|_| UpdateError::TargetConfigInvalid)?;
    }
    Ok(())
}

async fn config_self_check(executable: &Path, config_path: &Path) -> Result<(), UpdateError> {
    let mut child = Command::new(executable);
    child
        .arg("--config")
        .arg(config_path)
        .arg("check-config")
        .env_clear()
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .kill_on_drop(true);
    let output = timeout(SELF_CHECK_TIMEOUT, child.output())
        .await
        .map_err(|_| UpdateError::TargetConfigInvalid)?
        .map_err(|_| UpdateError::TargetConfigInvalid)?;
    if output.status.success() && output.stdout.len() <= MAX_SELF_CHECK_OUTPUT {
        Ok(())
    } else {
        Err(UpdateError::TargetConfigInvalid)
    }
}

impl UpdateService {
    pub fn new() -> Result<Self, UpdateError> {
        Ok(Self {
            client: ReleaseClient::new()?,
        })
    }

    pub fn with_release_api(value: &str) -> Result<Self, UpdateError> {
        Ok(Self {
            client: ReleaseClient::with_release_api(value)?,
        })
    }

    pub fn with_client(client: ReleaseClient) -> Self {
        Self { client }
    }

    pub async fn check_latest(&self) -> Result<ReleaseMetadata, UpdateError> {
        self.client.resolve(&ReleaseTarget::Latest).await
    }

    pub async fn resolve(&self, target: &ReleaseTarget) -> Result<ReleaseMetadata, UpdateError> {
        self.client.resolve(target).await
    }

    pub async fn download(&self, artifact: &ArtifactDescriptor) -> Result<Vec<u8>, UpdateError> {
        self.client.download(artifact).await
    }

    /// Apply already-downloaded, already-verified bytes. This narrow seam is
    /// useful to deterministic local fault tests and keeps replacement logic
    /// independent from the release authority.
    pub async fn replace_verified_executable(
        &self,
        executable: &Path,
        target: &ReleaseVersion,
        bytes: &[u8],
    ) -> Result<ApplyReport, UpdateError> {
        replace_executable::<fn() -> std::future::Ready<Result<(), UpdateError>>, _>(
            executable, target, bytes, false, None, None,
        )
        .await
    }

    pub fn current_version(&self) -> Result<ReleaseVersion, UpdateError> {
        ReleaseVersion::parse(PACKAGE_VERSION)
    }

    pub async fn check_info(&self, previous: &UpdateInfo) -> UpdateInfo {
        let current = self
            .current_version()
            .expect("Cargo package version is valid");
        match self.check_latest().await {
            Ok(metadata) => UpdateInfo {
                current_version: current.as_str().to_owned(),
                latest_version: metadata.version.as_str().to_owned(),
                update_available: metadata.version.is_newer_than(&current),
                last_check_at: unix_timestamp(),
                last_check_error: String::new(),
                ..previous.clone()
            },
            Err(error) => UpdateInfo {
                current_version: current.as_str().to_owned(),
                update_available: ReleaseVersion::parse(&previous.latest_version)
                    .is_ok_and(|latest| latest.is_newer_than(&current)),
                last_check_at: unix_timestamp(),
                last_check_error: error.category().to_owned(),
                ..previous.clone()
            },
        }
    }

    pub async fn apply_current_executable<F, Fut>(
        &self,
        target: &ReleaseTarget,
        executable: &Path,
        was_running: bool,
        restart: Option<F>,
    ) -> Result<ApplyReport, UpdateError>
    where
        F: Fn() -> Fut + Send + Sync,
        Fut: Future<Output = Result<(), UpdateError>> + Send,
    {
        self.apply_current_executable_with_guard(target, executable, was_running, restart, None)
            .await
    }

    pub async fn apply_current_executable_with_guard<F, Fut>(
        &self,
        target: &ReleaseTarget,
        executable: &Path,
        was_running: bool,
        restart: Option<F>,
        guard: Option<TransitionGuard>,
    ) -> Result<ApplyReport, UpdateError>
    where
        F: Fn() -> Fut + Send + Sync,
        Fut: Future<Output = Result<(), UpdateError>> + Send,
    {
        validate_managed_executable(executable)?;
        let metadata = self.resolve(target).await?;
        let artifact = metadata
            .artifact
            .as_ref()
            .ok_or(UpdateError::NoCompatibleArtifact)?;
        let bytes = self.download(artifact).await?;
        replace_executable(
            executable,
            &metadata.version,
            &bytes,
            was_running,
            restart,
            guard,
        )
        .await
    }
}

/// State retained by the process-owned update-checker task and status route.
#[derive(Clone)]
pub struct UpdateCheckerState {
    service: UpdateService,
    info: Arc<Mutex<UpdateInfo>>,
}

impl UpdateCheckerState {
    pub fn new(service: UpdateService) -> Self {
        Self {
            service,
            info: Arc::new(Mutex::new(UpdateInfo::default())),
        }
    }

    pub async fn check_once(&self) -> UpdateInfo {
        let previous = self.snapshot();
        let next = self.service.check_info(&previous).await;
        *self.info.lock().expect("update checker info lock") = next.clone();
        next
    }

    pub fn snapshot(&self) -> UpdateInfo {
        self.info.lock().expect("update checker info lock").clone()
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ApplyReport {
    pub target_version: String,
    pub restarted: bool,
}

struct UpdateLock {
    path: PathBuf,
    _file: File,
}

impl UpdateLock {
    fn acquire(executable: &Path) -> Result<Self, UpdateError> {
        let path = executable.with_file_name(".eggpool-update.lock");
        let mut file = match OpenOptions::new().write(true).create_new(true).open(&path) {
            Ok(file) => file,
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {
                if !recover_stale_lock(&path, executable)? {
                    return Err(UpdateError::UpdateInProgress);
                }
                OpenOptions::new()
                    .write(true)
                    .create_new(true)
                    .open(&path)
                    .map_err(|retry| {
                        if retry.kind() == io::ErrorKind::AlreadyExists {
                            UpdateError::UpdateInProgress
                        } else {
                            UpdateError::Io(retry)
                        }
                    })?
            }
            Err(error) => return Err(UpdateError::Io(error)),
        };
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).map_err(|error| {
                let _ = fs::remove_file(&path);
                UpdateError::Io(error)
            })?;
        }
        let record = serde_json::json!({
            "pid": std::process::id(),
            "executable": fs::canonicalize(executable)
                .unwrap_or_else(|_| executable.to_owned())
                .to_string_lossy(),
            "created_at": unix_timestamp(),
        });
        if file
            .write_all(record.to_string().as_bytes())
            .and_then(|_| file.sync_all())
            .is_err()
        {
            let _ = fs::remove_file(&path);
            return Err(UpdateError::Io(io::Error::other(
                "could not write update lock record",
            )));
        }
        Ok(Self { path, _file: file })
    }
}

fn recover_stale_lock(path: &Path, executable: &Path) -> Result<bool, UpdateError> {
    let contents = fs::read(path).map_err(UpdateError::Io)?;
    if contents.len() > 1024 {
        return Ok(false);
    }
    let Ok(record) = serde_json::from_slice::<serde_json::Value>(&contents) else {
        return Ok(false);
    };
    let Some(pid) = record.get("pid").and_then(serde_json::Value::as_u64) else {
        return Ok(false);
    };
    let Some(recorded_path) = record.get("executable").and_then(serde_json::Value::as_str) else {
        return Ok(false);
    };
    let expected_path = fs::canonicalize(executable).unwrap_or_else(|_| executable.to_owned());
    if recorded_path != expected_path.to_string_lossy()
        || pid == 0
        || pid > i32::MAX as u64
        || super::process::process_exists(pid as i32)
    {
        return Ok(false);
    }
    match fs::remove_file(path) {
        Ok(()) => Ok(true),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(true),
        Err(error) => Err(UpdateError::Io(error)),
    }
}

impl Drop for UpdateLock {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.path);
    }
}

async fn replace_executable<F, Fut>(
    executable: &Path,
    target: &ReleaseVersion,
    bytes: &[u8],
    was_running: bool,
    restart: Option<F>,
    guard: Option<TransitionGuard>,
) -> Result<ApplyReport, UpdateError>
where
    F: Fn() -> Fut + Send + Sync,
    Fut: Future<Output = Result<(), UpdateError>> + Send,
{
    validate_managed_executable(executable)?;
    let _guard = match guard {
        Some(guard) => guard,
        None => TransitionGuard::acquire(executable)?,
    };
    let parent = executable
        .parent()
        .ok_or(UpdateError::UnsupportedInstallPath)?;
    let stage = parent.join(format!(".eggpool-update-{}.stage", std::process::id()));
    let rollback = parent.join(format!(".eggpool-update-{}.rollback", std::process::id()));
    let result = async {
        write_private_stage(&stage, bytes)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&stage, fs::Permissions::from_mode(0o755))
                .map_err(UpdateError::Io)?;
        }
        self_check(&stage, target).await?;
        // Re-check the target immediately before the first rename to narrow
        // the current-executable TOCTOU window.
        validate_managed_executable(executable)?;
        fs::rename(executable, &rollback).map_err(|_| UpdateError::ReplacementFailed)?;
        if fs::rename(&stage, executable).is_err() {
            let _ = fs::rename(&rollback, executable);
            return Err(UpdateError::ReplacementFailed);
        }
        if self_check(executable, target).await.is_err() {
            let _ = fs::remove_file(executable);
            let restored = fs::rename(&rollback, executable).is_ok();
            return Err(if restored {
                UpdateError::SelfCheckFailed
            } else {
                UpdateError::ReplacementFailed
            });
        }
        if was_running {
            let restart = restart.as_ref().ok_or(UpdateError::RestartFailed)?;
            if restart().await.is_err() {
                let _ = fs::remove_file(executable);
                let restored = fs::rename(&rollback, executable).is_ok();
                if restored {
                    let _ = restart().await;
                }
                return Err(UpdateError::RestartFailed);
            }
        }
        fs::remove_file(&rollback).map_err(|_| UpdateError::ReplacementFailed)?;
        Ok(ApplyReport {
            target_version: target.as_str().to_owned(),
            restarted: was_running,
        })
    }
    .await;
    let _ = fs::remove_file(&stage);
    result
}

fn validate_managed_executable(path: &Path) -> Result<(), UpdateError> {
    let metadata = fs::symlink_metadata(path).map_err(|_| UpdateError::UnsupportedInstallPath)?;
    if !metadata.file_type().is_file() {
        return Err(UpdateError::UnsafeExecutable);
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        if metadata.uid() != nix::unistd::geteuid().as_raw() || metadata.nlink() > 1 {
            return Err(UpdateError::UnsafeExecutable);
        }
    }
    let parent = path.parent().ok_or(UpdateError::UnsupportedInstallPath)?;
    if !parent.is_dir() || fs::metadata(parent).is_err() {
        return Err(UpdateError::UnsupportedInstallPath);
    }
    let probe = parent.join(format!(".eggpool-update-probe-{}", std::process::id()));
    OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&probe)
        .map_err(|_| UpdateError::UnsupportedInstallPath)
        .and_then(|_| {
            fs::remove_file(&probe).map_err(UpdateError::Io)?;
            Ok(())
        })
}

fn write_private_stage(path: &Path, bytes: &[u8]) -> Result<(), UpdateError> {
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut file = options.open(path).map_err(UpdateError::Io)?;
    file.write_all(bytes).map_err(UpdateError::Io)?;
    file.sync_all().map_err(UpdateError::Io)
}

async fn self_check(path: &Path, target: &ReleaseVersion) -> Result<(), UpdateError> {
    let mut child = Command::new(path);
    child
        .arg("version")
        .env_clear()
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .kill_on_drop(true);
    let output = timeout(SELF_CHECK_TIMEOUT, child.output())
        .await
        .map_err(|_| UpdateError::SelfCheckFailed)?
        .map_err(|_| UpdateError::SelfCheckFailed)?;
    if !output.status.success()
        || output.stdout.len() > MAX_SELF_CHECK_OUTPUT
        || !output
            .stdout
            .windows(target.as_str().len())
            .any(|window| window == target.as_str().as_bytes())
    {
        return Err(UpdateError::SelfCheckFailed);
    }
    Ok(())
}

fn unix_timestamp() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}

#[cfg(test)]
mod tests {
    use std::os::unix::fs::PermissionsExt;

    use super::*;

    #[test]
    fn versions_normalize_and_order_like_python_oracle() {
        assert_eq!(ReleaseVersion::normalize("v0.6.5").unwrap(), "0.6.5");
        assert!(
            ReleaseVersion::parse("0.6.6")
                .unwrap()
                .is_newer_than(&ReleaseVersion::parse("0.6.5").unwrap())
        );
        assert!(
            ReleaseVersion::parse("0.6.5")
                .unwrap()
                .is_newer_than(&ReleaseVersion::parse("0.6.5rc1").unwrap())
        );
        assert!(
            ReleaseVersion::parse("0.6.5.post1")
                .unwrap()
                .is_newer_than(&ReleaseVersion::parse("0.6.5").unwrap())
        );
        assert!(
            ReleaseVersion::parse("0.6.5")
                .unwrap()
                .is_newer_than(&ReleaseVersion::parse("0.6.5.dev1").unwrap())
        );
        assert!(ReleaseVersion::parse("not-a-version").is_err());
    }

    #[test]
    fn artifact_contract_is_stable() {
        let version = ReleaseVersion::parse("0.7.4").unwrap();
        let platform = Platform {
            os: "linux".into(),
            architecture: "x86_64".into(),
        };
        assert_eq!(
            platform.artifact_name(&version),
            "eggpool-0.7.4-linux-x86_64"
        );
    }

    #[test]
    fn digest_parser_requires_sha256() {
        assert!(
            parse_digest("sha256:0000000000000000000000000000000000000000000000000000000000000000")
                .is_some()
        );
        assert!(parse_digest("sha1:0000000000000000000000000000000000000000").is_none());
        assert!(parse_digest("hostile").is_none());
    }

    fn catalog_target(version: &str) -> CatalogRelease {
        let catalog = ReleaseCatalog::embedded().expect("catalog");
        catalog
            .resolve(
                &ReleaseTarget::Exact(ReleaseVersion::parse(version).expect("version")),
                &Platform {
                    os: "linux".into(),
                    architecture: "x86_64".into(),
                },
            )
            .expect("catalog target")
    }

    fn package_metadata(root: &std::path::Path, version: &str) -> PackageMetadata {
        PackageMetadata {
            distribution: root.join("lib/python3.11/site-packages/eggpool.dist-info"),
            version: version.to_owned(),
            installer: Some("pip".to_owned()),
            direct_url: None,
        }
    }

    #[test]
    fn catalog_rejects_unknown_and_preserves_exact_requirement() {
        let catalog = ReleaseCatalog::embedded().expect("catalog");
        let platform = Platform {
            os: "linux".into(),
            architecture: "x86_64".into(),
        };
        assert!(matches!(
            catalog.resolve(
                &ReleaseTarget::Exact(ReleaseVersion::parse("9.9.9").unwrap()),
                &platform,
            ),
            Err(UpdateError::TargetNotCatalogued)
        ));
        assert_eq!(
            catalog_target("0.7.4").package_requirement,
            "eggpool==0.7.4"
        );
        assert!(matches!(
            catalog.resolve(
                &ReleaseTarget::Exact(ReleaseVersion::parse("0.8.0").unwrap()),
                &Platform {
                    os: "windows".into(),
                    architecture: "x86_64".into(),
                },
            ),
            Err(UpdateError::UnsupportedPlatform)
        ));
    }

    #[test]
    fn transition_guard_covers_the_deployed_service_lock_boundary() {
        let root = tempfile::tempdir().expect("root");
        let executable = root.path().join("bin/eggpool");
        std::fs::create_dir_all(executable.parent().expect("bin")).expect("bin");
        std::fs::write(&executable, b"managed").expect("executable");

        let guard = TransitionGuard::acquire(&executable).expect("first guard");
        assert!(matches!(
            TransitionGuard::acquire(&executable),
            Err(UpdateError::UpdateInProgress)
        ));
        drop(guard);
        TransitionGuard::acquire(&executable).expect("guard after release");
    }

    #[test]
    fn manager_commands_are_fixed_argv_and_never_shell_strings() {
        let root = tempfile::tempdir().expect("root");
        let python = root.path().join("bin/python");
        std::fs::create_dir_all(python.parent().expect("bin")).expect("bin");
        std::fs::write(&python, b"#!/bin/sh\n").expect("python");
        let metadata = package_metadata(root.path(), "0.7.4");
        let target = catalog_target("0.8.0");
        let pip = InstallProvenance::PipEnvironment {
            python,
            environment: root.path().to_owned(),
            exposed_executable: root.path().join("bin/eggpool"),
            package_metadata: metadata,
        };
        let command = build_manager_command(&pip, &target).expect("command");
        assert_eq!(
            command.args,
            [
                "-m",
                "pip",
                "install",
                "--upgrade",
                "--force-reinstall",
                "eggpool==0.8.0",
            ]
        );
        assert!(
            command
                .args
                .iter()
                .all(|argument| !argument.contains("sh -c") && !argument.contains(';'))
        );

        let uv = InstallProvenance::UvTool {
            manager: Some(root.path().join("uv")),
            environment: root.path().to_owned(),
            python: root.path().join("bin/python"),
            exposed_executable: root.path().join("bin/eggpool"),
            package_metadata: package_metadata(root.path(), "0.7.4"),
        };
        assert_eq!(
            build_manager_command(&uv, &target)
                .expect("uv command")
                .args,
            ["tool", "install", "--force", "eggpool==0.8.0"]
        );
        let pipx = InstallProvenance::Pipx {
            manager: Some(root.path().join("pipx")),
            environment: root.path().to_owned(),
            python: root.path().join("bin/python"),
            exposed_executable: root.path().join("bin/eggpool"),
            package_metadata: package_metadata(root.path(), "0.7.4"),
        };
        assert_eq!(
            build_manager_command(&pipx, &target)
                .expect("pipx command")
                .args,
            ["install", "--force", "eggpool==0.8.0"]
        );
    }

    #[tokio::test]
    async fn pip_transition_updates_metadata_and_runs_target_without_raw_replacement() {
        let root = tempfile::tempdir().expect("root");
        let environment = root.path();
        let site = environment.join("lib/python3.11/site-packages/eggpool-0.8.0.dist-info");
        let bin = environment.join("bin");
        std::fs::create_dir_all(&site).expect("site");
        std::fs::write(site.join("METADATA"), "Name: eggpool\nVersion: 0.7.4\n").expect("metadata");
        std::fs::write(site.join("INSTALLER"), "pip\n").expect("installer");
        std::fs::write(environment.join("pyvenv.cfg"), "version = 3.11.0\n").expect("venv");
        std::fs::create_dir_all(&bin).expect("bin");
        let executable = bin.join("eggpool");
        std::fs::write(&executable, b"#!/bin/sh\nprintf '0.7.4\\n'\n").expect("eggpool");
        let python = bin.join("python");
        let manager = format!(
            "#!/bin/sh\nprintf 'Name: eggpool\\nVersion: 0.8.0\\n' > '{}'\nprintf '#!/bin/sh\\nprintf %%s\\\\n 0.8.0\\n' > '{}'\nchmod 755 '{}'\n",
            site.join("METADATA").display(),
            executable.display(),
            executable.display(),
        );
        std::fs::write(&python, manager).expect("manager");
        std::fs::set_permissions(&python, std::fs::Permissions::from_mode(0o755))
            .expect("manager mode");
        std::fs::set_permissions(&executable, std::fs::Permissions::from_mode(0o755))
            .expect("eggpool mode");
        let provenance = InstallProvenance::PipEnvironment {
            python: python.clone(),
            environment: environment.to_owned(),
            exposed_executable: executable.clone(),
            package_metadata: package_metadata(environment, "0.7.4"),
        };
        let service = PackageTransitionService::with_catalog(
            UpdateService::new().expect("raw service"),
            ReleaseCatalog::embedded().expect("catalog"),
            Platform {
                os: "linux".into(),
                architecture: "x86_64".into(),
            },
        );
        let result = service
            .transition(
                TransitionRequest {
                    provenance: &provenance,
                    target: &ReleaseTarget::Exact(ReleaseVersion::parse("0.8.0").unwrap()),
                    current: &ReleaseVersion::parse("0.7.4").unwrap(),
                    executable: &executable,
                    context: TransitionContext::SAFE_DEFAULT,
                    was_running: false,
                    guard: None,
                },
                None::<fn() -> std::future::Ready<Result<(), UpdateError>>>,
            )
            .await
            .expect("package transition");
        assert_eq!(result.manager, "pip");
        assert_eq!(result.target_version, "0.8.0");
        assert_eq!(
            std::fs::read_to_string(site.join("METADATA")).expect("metadata"),
            "Name: eggpool\nVersion: 0.8.0\n"
        );
        assert!(!environment.join(".eggpool-update.lock").exists());
    }

    #[tokio::test]
    async fn manager_output_is_bounded() {
        let root = tempfile::tempdir().expect("root");
        let manager = root.path().join("manager");
        std::fs::write(&manager, "#!/bin/sh\nhead -c 70000 /dev/zero\n").expect("manager");
        std::fs::set_permissions(&manager, std::fs::Permissions::from_mode(0o755)).expect("mode");
        let result = run_manager(ManagerCommand {
            program: manager,
            args: Vec::new(),
            environment: Vec::new(),
        })
        .await;
        assert!(matches!(result, Err(UpdateError::ManagerOutputTooLarge)));
    }

    #[tokio::test]
    async fn package_failure_after_mutation_restores_the_previous_exact_release() {
        let root = tempfile::tempdir().expect("root");
        let environment = root.path();
        let site = environment.join("lib/python3.11/site-packages/eggpool-0.8.0.dist-info");
        let bin = environment.join("bin");
        std::fs::create_dir_all(&site).expect("site");
        std::fs::write(site.join("METADATA"), "Name: eggpool\nVersion: 0.7.4\n").expect("metadata");
        std::fs::write(site.join("INSTALLER"), "pip\n").expect("installer");
        std::fs::write(environment.join("pyvenv.cfg"), "version = 3.11.0\n").expect("venv");
        std::fs::create_dir_all(&bin).expect("bin");
        let executable = bin.join("eggpool");
        std::fs::write(&executable, "#!/bin/sh\nprintf '%s\\n' 0.7.4\n").expect("eggpool");
        let python = bin.join("python");
        let manager = format!(
            "#!/bin/sh\ncase \"$*\" in *eggpool==0.8.0*) \\\n             printf 'Name: eggpool\\nVersion: 0.8.0\\n' > '{}'; \\\n             printf '#!/bin/sh\\nprintf %%s\\\\n 0.8.0\\n' > '{}'; chmod 755 '{}'; exit 7; \\\n             ;; esac; \\\n             printf 'Name: eggpool\\nVersion: 0.7.4\\n' > '{}'; \\\n             printf '#!/bin/sh\\nprintf %%s\\\\n 0.7.4\\n' > '{}'; chmod 755 '{}'; exit 0\n",
            site.join("METADATA").display(),
            executable.display(),
            executable.display(),
            site.join("METADATA").display(),
            executable.display(),
            executable.display(),
        );
        std::fs::write(&python, manager).expect("manager");
        std::fs::set_permissions(&python, std::fs::Permissions::from_mode(0o755))
            .expect("manager mode");
        std::fs::set_permissions(&executable, std::fs::Permissions::from_mode(0o755))
            .expect("eggpool mode");
        let provenance = InstallProvenance::PipEnvironment {
            python: python.clone(),
            environment: environment.to_owned(),
            exposed_executable: executable.clone(),
            package_metadata: package_metadata(environment, "0.7.4"),
        };
        let service = PackageTransitionService::with_catalog(
            UpdateService::new().expect("raw service"),
            ReleaseCatalog::embedded().expect("catalog"),
            Platform {
                os: "linux".into(),
                architecture: "x86_64".into(),
            },
        );
        let result = service
            .transition(
                TransitionRequest {
                    provenance: &provenance,
                    target: &ReleaseTarget::Exact(ReleaseVersion::parse("0.8.0").unwrap()),
                    current: &ReleaseVersion::parse("0.7.4").unwrap(),
                    executable: &executable,
                    context: TransitionContext::SAFE_DEFAULT,
                    was_running: false,
                    guard: None,
                },
                None::<fn() -> std::future::Ready<Result<(), UpdateError>>>,
            )
            .await;
        assert!(matches!(result, Err(UpdateError::ManagerFailed)));
        assert!(
            std::fs::read_to_string(site.join("METADATA"))
                .expect("metadata")
                .contains("Version: 0.7.4")
        );
        let version = std::process::Command::new(&executable)
            .output()
            .expect("restored executable");
        assert!(String::from_utf8_lossy(&version.stdout).contains("0.7.4"));
        assert!(!environment.join(".eggpool-update.lock").exists());
    }

    #[tokio::test]
    async fn rollback_failure_is_typed_with_manual_recovery() {
        let root = tempfile::tempdir().expect("root");
        let environment = root.path();
        let site = environment.join("lib/python3.11/site-packages/eggpool-0.8.0.dist-info");
        let bin = environment.join("bin");
        std::fs::create_dir_all(&site).expect("site");
        std::fs::write(site.join("METADATA"), "Name: eggpool\nVersion: 0.7.4\n").expect("metadata");
        std::fs::write(site.join("INSTALLER"), "pip\n").expect("installer");
        std::fs::write(environment.join("pyvenv.cfg"), "version = 3.11.0\n").expect("venv");
        std::fs::create_dir_all(&bin).expect("bin");
        let executable = bin.join("eggpool");
        std::fs::write(&executable, "#!/bin/sh\nprintf '%s\\n' 0.7.4\n").expect("eggpool");
        let python = bin.join("python");
        let manager = format!(
            "#!/bin/sh\nprintf 'Name: eggpool\nVersion: 0.8.0\n' > '{}'\nprintf '#!/bin/sh\nprintf %%s\\n 0.8.0\n' > '{}'\nchmod 755 '{}'\ncase \"$*\" in *eggpool==0.8.0*) exit 7;; *) exit 8;; esac\n",
            site.join("METADATA").display(),
            executable.display(),
            executable.display(),
        );
        std::fs::write(&python, manager).expect("manager");
        std::fs::set_permissions(&python, std::fs::Permissions::from_mode(0o755))
            .expect("manager mode");
        std::fs::set_permissions(&executable, std::fs::Permissions::from_mode(0o755))
            .expect("eggpool mode");
        let provenance = InstallProvenance::PipEnvironment {
            python: python.clone(),
            environment: environment.to_owned(),
            exposed_executable: executable.clone(),
            package_metadata: package_metadata(environment, "0.7.4"),
        };
        let service = PackageTransitionService::with_catalog(
            UpdateService::new().expect("raw service"),
            ReleaseCatalog::embedded().expect("catalog"),
            Platform {
                os: "linux".into(),
                architecture: "x86_64".into(),
            },
        );
        let result = service
            .transition(
                TransitionRequest {
                    provenance: &provenance,
                    target: &ReleaseTarget::Exact(ReleaseVersion::parse("0.8.0").unwrap()),
                    current: &ReleaseVersion::parse("0.7.4").unwrap(),
                    executable: &executable,
                    context: TransitionContext::SAFE_DEFAULT,
                    was_running: false,
                    guard: None,
                },
                None::<fn() -> std::future::Ready<Result<(), UpdateError>>>,
            )
            .await;
        assert!(matches!(
            result,
            Err(UpdateError::RollbackFailed {
                ref previous_version,
                ref target_version,
                ref recovery_command,
                ..
            }) if previous_version == "0.7.4"
                && target_version == "0.8.0"
                && recovery_command == "eggpool update 0.7.4"
        ));
        assert!(!environment.join(".eggpool-update.lock").exists());
    }

    #[test]
    fn stale_lock_is_recovered_only_when_identity_matches() {
        let root = tempfile::tempdir().expect("root");
        let executable = root.path().join("eggpool");
        std::fs::write(&executable, b"native").expect("executable");
        let lock = root.path().join(".eggpool-update.lock");
        let mut stale = std::process::Command::new("/bin/sh")
            .arg("-c")
            .arg("exit 0")
            .spawn()
            .expect("stale owner");
        let stale_pid = stale.id();
        stale.wait().expect("stale owner exit");
        std::fs::write(
            &lock,
            serde_json::json!({
                "pid": stale_pid,
                "executable": std::fs::canonicalize(&executable)
                    .expect("canonical executable")
                    .to_string_lossy(),
                "created_at": 1,
            })
            .to_string(),
        )
        .expect("stale lock");
        let guard = UpdateLock::acquire(&executable).expect("stale lock recovery");
        assert!(lock.is_file());
        assert!(matches!(
            UpdateLock::acquire(&executable),
            Err(UpdateError::UpdateInProgress)
        ));
        drop(guard);
        assert!(!lock.exists());
    }
}
