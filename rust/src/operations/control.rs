//! Bounded local Unix-domain control protocol.
//!
//! The protocol is intentionally one request, one response, and one
//! connection.  It is an adapter only: a reload handler is supplied by the
//! M8 runtime and owns all config diff, staging, publication, and diagnostics.

use std::{
    collections::BTreeMap,
    future::Future,
    io,
    path::{Path, PathBuf},
    pin::Pin,
    sync::atomic::{AtomicU64, Ordering},
    sync::{Arc, Mutex},
    time::Duration,
};

use serde::{Deserialize, Serialize};
use serde_json::Value;
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::UnixListener,
    sync::Notify,
    task::JoinHandle,
    time::timeout,
};

use super::paths::{PathError, RuntimePaths};
use crate::reload::{ReloadResult, ReloadResultCategory};

pub const PROTOCOL_VERSION: u32 = 1;
pub const MAX_REQUEST_BYTES: usize = 65_536;
pub const MAX_REQUEST_ID_BYTES: usize = 256;
pub const CONTROL_TIMEOUT: Duration = Duration::from_secs(30);
const MAX_SOCKET_PATH_BYTES: usize = 103;
static NEXT_REQUEST_ID: AtomicU64 = AtomicU64::new(1);

pub type ControlFuture = Pin<Box<dyn Future<Output = ControlResponse> + Send>>;
pub type ControlHandler = Arc<dyn Fn(ControlRequest) -> ControlFuture + Send + Sync>;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ControlError {
    UnsupportedPlatform,
    PathTooLong,
    UnsafeRuntimeDirectory,
    SocketInUse,
    SocketCollision,
    Bind(io::ErrorKind),
    Io(io::ErrorKind),
    Join,
}

impl std::fmt::Display for ControlError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let text = match self {
            Self::UnsupportedPlatform => "local control is unsupported on this platform",
            Self::PathTooLong => "control socket path is too long",
            Self::UnsafeRuntimeDirectory => "control runtime directory is not private",
            Self::SocketInUse => "control socket is already in use",
            Self::SocketCollision => "control socket path is not a removable socket",
            Self::Bind(_) => "control socket could not be bound",
            Self::Io(_) => "control socket I/O failed",
            Self::Join => "control accept loop could not be joined",
        };
        formatter.write_str(text)
    }
}

impl std::error::Error for ControlError {}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ProtocolError {
    Empty,
    Oversized,
    MissingNewline,
    MultipleFrames,
    InvalidJson,
    NotObject,
    WrongVersion,
    InvalidRequestId,
    UnknownCommand,
    InvalidDigest,
    InvalidParams,
}

impl std::fmt::Display for ProtocolError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let text = match self {
            Self::Empty => "empty request",
            Self::Oversized => "request exceeds byte limit",
            Self::MissingNewline => "request must be one newline-terminated frame",
            Self::MultipleFrames => "multiple requests in one connection are not supported",
            Self::InvalidJson => "invalid JSON",
            Self::NotObject => "request must be a JSON object",
            Self::WrongVersion => "unsupported protocol version",
            Self::InvalidRequestId => "missing or invalid request_id",
            Self::UnknownCommand => "unknown command",
            Self::InvalidDigest => "invalid validated_digest",
            Self::InvalidParams => "params must be a JSON object",
        };
        formatter.write_str(text)
    }
}

impl std::error::Error for ProtocolError {}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
pub struct ControlRequest {
    pub protocol_version: u32,
    pub request_id: String,
    pub command: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub validated_digest: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub params: Option<BTreeMap<String, Value>>,
}

impl ControlRequest {
    pub fn reload(request_id: impl Into<String>, digest: Option<String>) -> Self {
        Self {
            protocol_version: PROTOCOL_VERSION,
            request_id: request_id.into(),
            command: "reload_config".to_owned(),
            validated_digest: digest,
            params: None,
        }
    }

    pub fn parse_frame(frame: &[u8]) -> Result<Self, ProtocolError> {
        if frame.is_empty() || frame == b"\n" || frame == b"\r\n" {
            return Err(ProtocolError::Empty);
        }
        if frame.len() > MAX_REQUEST_BYTES {
            return Err(ProtocolError::Oversized);
        }
        if !frame.ends_with(b"\n") {
            return Err(ProtocolError::MissingNewline);
        }
        if frame[..frame.len() - 1].contains(&b'\n') {
            return Err(ProtocolError::MultipleFrames);
        }
        let value: Value = serde_json::from_slice(&frame[..frame.len() - 1])
            .map_err(|_| ProtocolError::InvalidJson)?;
        let object = value.as_object().ok_or(ProtocolError::NotObject)?;
        let protocol_version = object
            .get("protocol_version")
            .and_then(Value::as_u64)
            .filter(|value| *value <= u32::MAX as u64)
            .map(|value| value as u32)
            .ok_or(ProtocolError::WrongVersion)?;
        if protocol_version != PROTOCOL_VERSION {
            return Err(ProtocolError::WrongVersion);
        }
        let request_id = object
            .get("request_id")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .ok_or(ProtocolError::InvalidRequestId)?;
        if request_id.len() > MAX_REQUEST_ID_BYTES
            || request_id
                .chars()
                .any(|character| character.is_control() || character.is_whitespace())
        {
            return Err(ProtocolError::InvalidRequestId);
        }
        let command = object
            .get("command")
            .and_then(Value::as_str)
            .filter(|value| *value == "reload_config")
            .ok_or(ProtocolError::UnknownCommand)?;
        let validated_digest = match object.get("validated_digest") {
            None | Some(Value::Null) => None,
            Some(Value::String(value))
                if value.len() == 64
                    && value
                        .chars()
                        .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase()) =>
            {
                Some(value.clone())
            }
            _ => return Err(ProtocolError::InvalidDigest),
        };
        let params = match object.get("params") {
            None | Some(Value::Null) => None,
            Some(Value::Object(value)) => Some(value.clone().into_iter().collect()),
            _ => return Err(ProtocolError::InvalidParams),
        };
        Ok(Self {
            protocol_version,
            request_id: request_id.to_owned(),
            command: command.to_owned(),
            validated_digest,
            params,
        })
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ControlResponse {
    pub protocol_version: u32,
    pub request_id: String,
    pub ok: bool,
    pub stage: String,
    pub generation: Option<u64>,
    pub changed_sections: Vec<String>,
    pub warnings: Vec<String>,
    pub restart_required: Vec<String>,
    pub retirement_pending: bool,
    pub message: String,
}

impl ControlResponse {
    pub fn error(request_id: impl Into<String>, stage: impl Into<String>, message: &str) -> Self {
        Self {
            protocol_version: PROTOCOL_VERSION,
            request_id: request_id.into(),
            ok: false,
            stage: stage.into(),
            generation: None,
            changed_sections: Vec::new(),
            warnings: Vec::new(),
            restart_required: Vec::new(),
            retirement_pending: false,
            message: message.to_owned(),
        }
    }

    pub fn from_reload(request_id: impl Into<String>, result: ReloadResult) -> Self {
        let (ok, stage, message) = match result.category {
            ReloadResultCategory::Applied => (true, "activation", "reload applied"),
            ReloadResultCategory::Noop => (true, "commit", "no changes"),
            ReloadResultCategory::RestartRequired => (false, "diff", "restart required"),
            ReloadResultCategory::Busy => (false, "reload_in_progress", "reload is busy"),
            ReloadResultCategory::StaleDigest => (false, "validation", "content digest mismatch"),
            ReloadResultCategory::ValidationFailed => {
                (false, "validation", "configuration validation failed")
            }
            ReloadResultCategory::RetirementBacklog => (false, "retirement", "retirement backlog"),
            ReloadResultCategory::Aborted => (false, "error", "reload aborted"),
            ReloadResultCategory::CompensationFailed => {
                (false, "error", "reload compensation failed")
            }
        };
        Self {
            protocol_version: PROTOCOL_VERSION,
            request_id: request_id.into(),
            ok,
            stage: stage.to_owned(),
            generation: Some(result.active_generation_id),
            changed_sections: result.changed_sections,
            warnings: Vec::new(),
            restart_required: result.restart_required_paths,
            retirement_pending: result.retirement_pending,
            message: message.to_owned(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ControlClientError {
    UnsupportedPlatform,
    Timeout,
    Connection(io::ErrorKind),
    Protocol(ProtocolError),
    Io(io::ErrorKind),
}

impl std::fmt::Display for ControlClientError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::UnsupportedPlatform => {
                formatter.write_str("local control is unsupported on this platform")
            }
            Self::Timeout => formatter.write_str("control request timed out"),
            Self::Connection(_) => formatter.write_str("control socket is unavailable"),
            Self::Protocol(error) => write!(formatter, "invalid control response: {error}"),
            Self::Io(_) => formatter.write_str("control socket I/O failed"),
        }
    }
}

impl std::error::Error for ControlClientError {}

/// Typed one-shot client used by `rehash` and later lifecycle commands.
#[derive(Debug, Clone)]
pub struct ControlClient {
    path: PathBuf,
    timeout: Duration,
}

impl ControlClient {
    pub fn new(path: impl Into<PathBuf>) -> Self {
        Self {
            path: path.into(),
            timeout: CONTROL_TIMEOUT,
        }
    }

    pub fn with_timeout(mut self, timeout: Duration) -> Self {
        self.timeout = timeout.max(Duration::from_millis(1));
        self
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    pub async fn reload(
        &self,
        validated_digest: Option<String>,
    ) -> Result<ControlResponse, ControlClientError> {
        let request_id = format!(
            "rust-{}-{}",
            std::process::id(),
            NEXT_REQUEST_ID.fetch_add(1, Ordering::Relaxed)
        );
        self.send(ControlRequest::reload(request_id, validated_digest))
            .await
    }

    pub async fn send(
        &self,
        request: ControlRequest,
    ) -> Result<ControlResponse, ControlClientError> {
        #[cfg(not(unix))]
        {
            let _ = request;
            return Err(ControlClientError::UnsupportedPlatform);
        }
        #[cfg(unix)]
        {
            let mut stream = timeout(self.timeout, tokio::net::UnixStream::connect(&self.path))
                .await
                .map_err(|_| ControlClientError::Timeout)?
                .map_err(|error| ControlClientError::Connection(error.kind()))?;
            let mut frame = serde_json::to_vec(&request)
                .map_err(|_| ControlClientError::Protocol(ProtocolError::InvalidJson))?;
            frame.push(b'\n');
            if frame.len() > MAX_REQUEST_BYTES {
                return Err(ControlClientError::Protocol(ProtocolError::Oversized));
            }
            timeout(self.timeout, stream.write_all(&frame))
                .await
                .map_err(|_| ControlClientError::Timeout)?
                .map_err(|error| ControlClientError::Io(error.kind()))?;
            let response = timeout(self.timeout, read_response_frame(&mut stream))
                .await
                .map_err(|_| ControlClientError::Timeout)?
                .map_err(ControlClientError::Protocol)?;
            let response: ControlResponse = serde_json::from_slice(&response)
                .map_err(|_| ControlClientError::Protocol(ProtocolError::InvalidJson))?;
            if response.protocol_version != PROTOCOL_VERSION {
                return Err(ControlClientError::Protocol(ProtocolError::WrongVersion));
            }
            if response.request_id != request.request_id {
                return Err(ControlClientError::Protocol(
                    ProtocolError::InvalidRequestId,
                ));
            }
            Ok(response)
        }
    }
}

/// A running listener.  Clones share one accept-loop owner and one socket
/// identity; only `close` removes the pathname.
pub struct ControlServerHandle {
    inner: Arc<ControlServerInner>,
}

struct ControlServerInner {
    path: PathBuf,
    identity: SocketIdentity,
    stop: Arc<Notify>,
    accept_task: Mutex<Option<JoinHandle<()>>>,
}

impl Clone for ControlServerHandle {
    fn clone(&self) -> Self {
        Self {
            inner: Arc::clone(&self.inner),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct SocketIdentity {
    device: u64,
    inode: u64,
}

impl ControlServerHandle {
    pub fn path(&self) -> &Path {
        &self.inner.path
    }

    /// Stop accepting and unlink only the socket created by this listener.
    /// In-flight connection tasks are not canceled; a retained M8 reload may
    /// therefore finish even when its client has disconnected.
    pub async fn close(&self) -> Result<(), ControlError> {
        self.inner.stop.notify_one();
        let task = self
            .inner
            .accept_task
            .lock()
            .expect("control accept task lock")
            .take();
        if let Some(task) = task {
            task.await.map_err(|_| ControlError::Join)?;
        }
        remove_socket(&self.inner.path, self.inner.identity)
    }
}

impl Drop for ControlServerHandle {
    fn drop(&mut self) {
        if Arc::strong_count(&self.inner) == 1 {
            self.inner.stop.notify_one();
            if let Ok(mut task) = self.inner.accept_task.lock() {
                if let Some(task) = task.take() {
                    task.abort();
                }
            }
            let _ = remove_socket(&self.inner.path, self.inner.identity);
        }
    }
}

/// Bind and start the sole process-local control listener.
#[cfg(unix)]
pub async fn start<F, Fut>(
    path: impl Into<PathBuf>,
    handler: F,
) -> Result<ControlServerHandle, ControlError>
where
    F: Fn(ControlRequest) -> Fut + Send + Sync + 'static,
    Fut: Future<Output = ControlResponse> + Send + 'static,
{
    let path = path.into();
    if path.as_os_str().as_encoded_bytes().len() > MAX_SOCKET_PATH_BYTES {
        return Err(ControlError::PathTooLong);
    }
    let parent = path.parent().ok_or(ControlError::UnsafeRuntimeDirectory)?;
    ensure_runtime_directory(parent)?;
    clean_stale_socket(&path).await?;
    let listener = UnixListener::bind(&path).map_err(|error| ControlError::Bind(error.kind()))?;
    let identity = socket_identity(&path).ok_or(ControlError::SocketCollision)?;
    if let Err(error) = restrict_socket_permissions(&path) {
        let _ = remove_socket(&path, identity);
        return Err(error);
    }
    let stop = Arc::new(Notify::new());
    let accept_stop = Arc::clone(&stop);
    let handler: ControlHandler = Arc::new(move |request| Box::pin(handler(request)));
    let accept_task = tokio::spawn(async move {
        accept_loop(listener, handler, accept_stop).await;
    });
    Ok(ControlServerHandle {
        inner: Arc::new(ControlServerInner {
            path,
            identity,
            stop,
            accept_task: Mutex::new(Some(accept_task)),
        }),
    })
}

#[cfg(not(unix))]
pub async fn start<F, Fut>(_: impl Into<PathBuf>, _: F) -> Result<ControlServerHandle, ControlError>
where
    F: Fn(ControlRequest) -> Fut + Send + Sync + 'static,
    Fut: Future<Output = ControlResponse> + Send + 'static,
{
    Err(ControlError::UnsupportedPlatform)
}

#[cfg(unix)]
async fn accept_loop(listener: UnixListener, handler: ControlHandler, stop: Arc<Notify>) {
    loop {
        tokio::select! {
            _ = stop.notified() => return,
            accepted = listener.accept() => {
                let Ok((stream, _)) = accepted else { continue };
                let handler = Arc::clone(&handler);
                tokio::spawn(async move { handle_connection(stream, handler).await; });
            }
        }
    }
}

#[cfg(unix)]
async fn handle_connection(mut stream: tokio::net::UnixStream, handler: ControlHandler) {
    let response = match timeout(CONTROL_TIMEOUT, read_frame(&mut stream)).await {
        Ok(Ok(frame)) => match ControlRequest::parse_frame(&frame) {
            Ok(request) => match timeout(CONTROL_TIMEOUT, handler(request.clone())).await {
                Ok(response) => response,
                Err(_) => {
                    ControlResponse::error(request.request_id, "timeout", "request timed out")
                }
            },
            Err(error) => {
                ControlResponse::error(safe_request_id(&frame), "parse", &error.to_string())
            }
        },
        Ok(Err(error)) => ControlResponse::error("", "parse", &error.to_string()),
        Err(_) => ControlResponse::error("", "timeout", "request timed out"),
    };
    let mut output = match serde_json::to_vec(&response) {
        Ok(output) => output,
        Err(_) => return,
    };
    output.push(b'\n');
    let _ = timeout(CONTROL_TIMEOUT, stream.write_all(&output)).await;
}

#[cfg(unix)]
async fn read_frame(stream: &mut tokio::net::UnixStream) -> Result<Vec<u8>, ProtocolError> {
    let mut frame = Vec::with_capacity(512);
    let mut chunk = [0_u8; 4096];
    loop {
        let count = stream
            .read(&mut chunk)
            .await
            .map_err(|_| ProtocolError::MissingNewline)?;
        if count == 0 {
            return Err(if frame.is_empty() {
                ProtocolError::Empty
            } else {
                ProtocolError::MissingNewline
            });
        }
        let bytes = &chunk[..count];
        if let Some(newline) = bytes.iter().position(|byte| *byte == b'\n') {
            frame.extend_from_slice(&bytes[..=newline]);
            if frame.len() > MAX_REQUEST_BYTES {
                return Err(ProtocolError::Oversized);
            }
            if bytes[newline + 1..].contains(&b'\n') || !bytes[newline + 1..].is_empty() {
                return Err(ProtocolError::MultipleFrames);
            }
            return Ok(frame);
        }
        frame.extend_from_slice(bytes);
        if frame.len() >= MAX_REQUEST_BYTES {
            return Err(ProtocolError::Oversized);
        }
    }
}

#[cfg(unix)]
async fn read_response_frame(
    stream: &mut tokio::net::UnixStream,
) -> Result<Vec<u8>, ProtocolError> {
    let mut frame = Vec::with_capacity(512);
    let mut chunk = [0_u8; 4096];
    loop {
        let count = stream
            .read(&mut chunk)
            .await
            .map_err(|_| ProtocolError::MissingNewline)?;
        if count == 0 {
            return Err(if frame.is_empty() {
                ProtocolError::Empty
            } else {
                ProtocolError::MissingNewline
            });
        }
        let bytes = &chunk[..count];
        if let Some(newline) = bytes.iter().position(|byte| *byte == b'\n') {
            frame.extend_from_slice(&bytes[..newline]);
            if frame.len() > MAX_REQUEST_BYTES {
                return Err(ProtocolError::Oversized);
            }
            if !bytes[newline + 1..].is_empty() {
                return Err(ProtocolError::MultipleFrames);
            }
            return Ok(frame);
        }
        frame.extend_from_slice(bytes);
        if frame.len() >= MAX_REQUEST_BYTES {
            return Err(ProtocolError::Oversized);
        }
    }
}

fn safe_request_id(frame: &[u8]) -> String {
    let Ok(value) = serde_json::from_slice::<Value>(frame) else {
        return String::new();
    };
    let Some(value) = value.get("request_id").and_then(Value::as_str) else {
        return String::new();
    };
    if value.len() <= MAX_REQUEST_ID_BYTES
        && value.chars().all(|c| !c.is_control() && !c.is_whitespace())
    {
        value.to_owned()
    } else {
        String::new()
    }
}

#[cfg(unix)]
async fn clean_stale_socket(path: &Path) -> Result<(), ControlError> {
    let Some(before) = socket_identity(path) else {
        return Ok(());
    };
    let metadata = std::fs::symlink_metadata(path).map_err(|_| ControlError::SocketCollision)?;
    use std::os::unix::fs::{FileTypeExt, MetadataExt};
    if !metadata.file_type().is_socket() || metadata.uid() != nix::unistd::geteuid().as_raw() {
        return Err(ControlError::SocketCollision);
    }
    match timeout(
        Duration::from_secs(1),
        tokio::net::UnixStream::connect(path),
    )
    .await
    {
        Ok(Ok(_)) => return Err(ControlError::SocketInUse),
        Ok(Err(error)) if error.kind() == io::ErrorKind::ConnectionRefused => {}
        Ok(Err(error)) if error.kind() == io::ErrorKind::NotFound => return Ok(()),
        _ => return Err(ControlError::SocketInUse),
    }
    if socket_identity(path) == Some(before) {
        std::fs::remove_file(path).map_err(|error| ControlError::Io(error.kind()))?;
    }
    Ok(())
}

#[cfg(unix)]
fn restrict_socket_permissions(path: &Path) -> Result<(), ControlError> {
    use std::os::unix::fs::{FileTypeExt, MetadataExt, PermissionsExt};
    let metadata =
        std::fs::symlink_metadata(path).map_err(|error| ControlError::Io(error.kind()))?;
    if !metadata.file_type().is_socket() || metadata.uid() != nix::unistd::geteuid().as_raw() {
        return Err(ControlError::SocketCollision);
    }
    let mut permissions = metadata.permissions();
    permissions.set_mode(0o600);
    std::fs::set_permissions(path, permissions).map_err(|error| ControlError::Io(error.kind()))?;
    let after = std::fs::symlink_metadata(path).map_err(|error| ControlError::Io(error.kind()))?;
    if !after.file_type().is_socket()
        || after.uid() != nix::unistd::geteuid().as_raw()
        || after.mode() & 0o777 != 0o600
    {
        return Err(ControlError::SocketCollision);
    }
    Ok(())
}

#[cfg(unix)]
fn socket_identity(path: &Path) -> Option<SocketIdentity> {
    use std::os::unix::fs::MetadataExt;
    let metadata = std::fs::symlink_metadata(path).ok()?;
    Some(SocketIdentity {
        device: metadata.dev(),
        inode: metadata.ino(),
    })
}

#[cfg(unix)]
fn remove_socket(path: &Path, identity: SocketIdentity) -> Result<(), ControlError> {
    if socket_identity(path) != Some(identity) {
        return Ok(());
    }
    std::fs::remove_file(path).map_err(|error| ControlError::Io(error.kind()))
}

#[cfg(not(unix))]
fn remove_socket(_: &Path, _: SocketIdentity) -> Result<(), ControlError> {
    Err(ControlError::UnsupportedPlatform)
}

fn ensure_runtime_directory(path: &Path) -> Result<(), ControlError> {
    if path.as_os_str().is_empty() {
        return Err(ControlError::UnsafeRuntimeDirectory);
    }
    let runtime = RuntimePaths::resolve();
    if path == runtime.runtime_dir {
        return runtime.ensure_runtime_dir().map_err(map_path_error);
    }
    #[cfg(unix)]
    {
        let metadata = std::fs::symlink_metadata(path);
        if metadata.is_err() {
            std::fs::create_dir_all(path).map_err(|_| ControlError::UnsafeRuntimeDirectory)?;
            use std::os::unix::fs::PermissionsExt;
            let mut permissions = std::fs::metadata(path)
                .map_err(|_| ControlError::UnsafeRuntimeDirectory)?
                .permissions();
            permissions.set_mode(0o700);
            std::fs::set_permissions(path, permissions)
                .map_err(|_| ControlError::UnsafeRuntimeDirectory)?;
        }
        let metadata =
            std::fs::symlink_metadata(path).map_err(|_| ControlError::UnsafeRuntimeDirectory)?;
        use std::os::unix::fs::MetadataExt;
        if !metadata.is_dir()
            || metadata.uid() != nix::unistd::geteuid().as_raw()
            || metadata.mode() & 0o077 != 0
        {
            return Err(ControlError::UnsafeRuntimeDirectory);
        }
        Ok(())
    }
    #[cfg(not(unix))]
    {
        let _ = path;
        Err(ControlError::UnsupportedPlatform)
    }
}

fn map_path_error(error: PathError) -> ControlError {
    match error {
        PathError::UnsupportedPlatform => ControlError::UnsupportedPlatform,
        PathError::NotDirectory | PathError::UnsafeDirectory | PathError::Io(_) => {
            ControlError::UnsafeRuntimeDirectory
        }
    }
}
