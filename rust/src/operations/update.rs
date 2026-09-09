//! Rust-native release discovery, integrity verification, and self-update.
//!
//! The release authority is deliberately kept behind this small service.  It
//! uses the same Hyper/Rustls transport family as provider traffic, but does
//! not use provider routing or credentials: update metadata is public release
//! metadata and must never inherit an account's proxy or secret headers.

use std::{
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
use tokio::{process::Command, time::timeout};

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
    #[error("current executable path is unsupported or not writable")]
    UnsupportedInstallPath,
    #[error("current executable has unsafe links or ownership")]
    UnsafeExecutable,
    #[error("another update is already in progress")]
    UpdateInProgress,
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
            Self::UnsupportedInstallPath => "unsupported_install_path",
            Self::UnsafeExecutable => "unsafe_executable",
            Self::UpdateInProgress => "update_in_progress",
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
            executable, target, bytes, false, None,
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
        validate_managed_executable(executable)?;
        let metadata = self.resolve(target).await?;
        let artifact = metadata
            .artifact
            .as_ref()
            .ok_or(UpdateError::NoCompatibleArtifact)?;
        let bytes = self.download(artifact).await?;
        replace_executable(executable, &metadata.version, &bytes, was_running, restart).await
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
        let file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&path)
            .map_err(|error| {
                if error.kind() == io::ErrorKind::AlreadyExists {
                    UpdateError::UpdateInProgress
                } else {
                    UpdateError::Io(error)
                }
            })?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&path, fs::Permissions::from_mode(0o600))
                .map_err(UpdateError::Io)?;
        }
        Ok(Self { path, _file: file })
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
) -> Result<ApplyReport, UpdateError>
where
    F: Fn() -> Fut + Send + Sync,
    Fut: Future<Output = Result<(), UpdateError>> + Send,
{
    validate_managed_executable(executable)?;
    let _lock = UpdateLock::acquire(executable)?;
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
}
