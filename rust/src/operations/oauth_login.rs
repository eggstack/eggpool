//! OpenAI desktop-loopback authorization for a managed user's own account.
//!
//! The command owns only browser launch and the short-lived loopback callback.
//! Provider traffic uses the configured account's existing Eggfetch client;
//! the completed credential set is submitted to the server's authenticated
//! owner-bound commit route.

use std::{fs, future::Future, io::Write, net::IpAddr, path::Path, process::Stdio, time::Duration};

use bytes::Bytes;
use eggpass_auth::{ClientIdPolicy, OAuthClient, OAuthProviderConfig};
use http::{
    HeaderValue, Method, Request, StatusCode, Uri,
    header::{AUTHORIZATION, CONTENT_TYPE},
};
use http_body_util::{BodyExt, Full, Limited};
use hyper_util::{
    client::legacy::{Client, connect::HttpConnector},
    rt::TokioExecutor,
};
use serde::{Deserialize, Serialize};
use thiserror::Error;
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::{TcpListener, TcpStream},
    process::Command as TokioCommand,
};

use crate::{
    config::{AccountAuthMode, Config},
    oauth::OAuthCredentials,
    providers::ProviderClientPool,
};

const CALLBACK_TIMEOUT: Duration = Duration::from_secs(5 * 60);
const BROWSER_OPEN_CONFIRM_TIMEOUT: Duration = Duration::from_secs(3);
const CLIPBOARD_TIMEOUT: Duration = Duration::from_secs(3);
const EXCHANGE_TIMEOUT: Duration = Duration::from_secs(60);
const UPLOAD_TIMEOUT: Duration = Duration::from_secs(20);
const MAX_CALLBACK_REQUEST_BYTES: usize = 8 * 1024;
const MAX_CALLBACK_REQUEST_LINE_BYTES: usize = 4 * 1024 + 256;
const MAX_CALLBACK_HEADER_BYTES: usize = 4 * 1024;
const MAX_CALLBACK_HEADER_LINE_BYTES: usize = 1024;
const CALLBACK_HEADER_TERMINATOR: &[u8; 4] = b"\r\n\r\n";
const MAX_CALLBACK_QUERY_BYTES: usize = 4 * 1024;
const OAUTH_UPLOAD_PATH: &str = "/api/user/oauth-credentials";
const OAUTH_REAUTHORIZATION_HINT_PATH: &str = "/api/user/oauth-credentials/hint";
const OPENAI_INSTALLATION_ID_FILENAME: &str = "openai-installation-id";
const OPENAI_ISSUER: &str = "https://auth.openai.com";
const OPENAI_ENTRY_CLIENT_ID: &str = "dynamic_agent_client";
const OPENAI_RESOURCE: &str = "https://api.openai.com/v1";
const OPENAI_AGENT_NAME: &str = "EggPool";
const OPENAI_SCOPES: &[&str] = &[
    "openid",
    "profile",
    "email",
    "offline_access",
    "resource.invoke",
    "chatgpt.tokens.use.direct",
];

#[derive(Debug, Error)]
pub(crate) enum LoginError {
    #[error("OAuth is disabled by EggPool configuration")]
    Disabled,
    #[error("the configured OAuth account is unavailable")]
    AccountUnavailable,
    #[error("the configured provider account is not OAuth-enabled")]
    AccountNotOAuth,
    #[error("OpenAI reauthorization details are unavailable")]
    ReauthorizationHint,
    #[error("EggPool's configured provider transport is unavailable")]
    ProviderTransport,
    #[error("the stable OpenAI installation identifier is unavailable")]
    InstallationIdentity,
    #[error("the loopback OAuth callback listener could not be started")]
    CallbackListener,
    #[error("the OpenAI authorization request was rejected")]
    AuthorizationRequest,
    #[error(
        "browser launch could not be confirmed and the authorization URL could not be copied safely"
    )]
    BrowserUnavailable,
    #[error("OpenAI authorization was canceled")]
    Canceled,
    #[error("the OpenAI loopback callback timed out")]
    CallbackTimeout,
    #[error("the OpenAI loopback callback was invalid")]
    InvalidCallback,
    #[error("OpenAI authorization did not complete")]
    AuthorizationFailed,
    #[error("EggPool's local API endpoint is not available from this host")]
    LocalServerUnavailable,
    #[error("the managed EggPool credential was invalid")]
    InvalidEggPoolCredential,
    #[error("the OpenAI credentials could not be committed by EggPool")]
    CommitFailed,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum BrowserPresentation {
    BrowserOpenerSucceeded,
    CopiedToClipboard,
    ManualUrl,
}

#[derive(Serialize)]
struct CredentialUpload<'a> {
    provider_id: &'a str,
    account_name: &'a str,
    credentials: &'a OAuthCredentials,
}

struct CallbackConnection {
    stream: TcpStream,
    callback_url: String,
}

/// Run an OpenAI or xAI authorization-code/PKCE loopback flow. The caller
/// must obtain the EggPool credential via hidden TTY input or stdin.
pub(crate) async fn login_provider(
    config: &Config,
    provider_id: &str,
    account_name: &str,
    eggpool_token: &str,
    interactive_tty: bool,
    present_browser: impl FnOnce(BrowserPresentation, &str),
) -> Result<(), LoginError> {
    if !config.oauth.enabled {
        return Err(LoginError::Disabled);
    }
    if account_name.is_empty()
        || account_name.len() > 128
        || account_name.chars().any(char::is_control)
    {
        return Err(LoginError::AccountUnavailable);
    }
    if !matches!(provider_id, "openai" | "xai") {
        return Err(LoginError::AccountUnavailable);
    }
    let account = config
        .providers
        .get(provider_id)
        .and_then(|provider| {
            provider
                .accounts
                .iter()
                .find(|account| account.name == account_name)
        })
        .ok_or(LoginError::AccountUnavailable)?;
    if account.auth_mode != AccountAuthMode::OAuth {
        return Err(LoginError::AccountNotOAuth);
    }
    if !valid_eggpool_token(eggpool_token) {
        return Err(LoginError::InvalidEggPoolCredential);
    }

    let pool =
        ProviderClientPool::from_config(config).map_err(|_| LoginError::ProviderTransport)?;
    let provider_client = pool
        .get_client(provider_id, Some(account_name))
        .map_err(|_| LoginError::ProviderTransport)?;
    let provider = if provider_id == "openai" {
        let state_dir = crate::operations::paths::RuntimePaths::prepare()
            .map_err(|_| LoginError::InstallationIdentity)?
            .state_dir;
        let installation_id = load_or_create_openai_installation_id(&state_dir)?;
        openai_provider_config(installation_id)?
    } else {
        OAuthProviderConfig::xai()
    };
    let callback_path = provider.callback_path().to_owned();
    if !valid_callback_path(&callback_path) {
        return Err(LoginError::AuthorizationRequest);
    }
    let client = OAuthClient::new(provider_client.oauth_eggfetch_client(), provider);
    // Reuse only the issued OpenAI client ID. Stored access, refresh, and ID
    // tokens never leave EggPool; xAI has a fixed client profile.
    let existing_credentials = if provider_id == "openai" {
        request_authorization_hint(config, account_name, eggpool_token).await?
    } else {
        None
    };

    // Bind before constructing the redirect URI so OpenAI receives the exact
    // ephemeral loopback port owned by this process.
    let listener = TcpListener::bind(("127.0.0.1", 0))
        .await
        .map_err(|_| LoginError::CallbackListener)?;
    let address = listener
        .local_addr()
        .map_err(|_| LoginError::CallbackListener)?;
    if !address.ip().is_loopback() || address.port() == 0 {
        return Err(LoginError::CallbackListener);
    }
    let expected_host = format!("127.0.0.1:{}", address.port());
    let origin = format!("http://{expected_host}");
    let redirect_uri = format!("{origin}{callback_path}");
    let pending = client
        .begin_authorization(&redirect_uri, existing_credentials.as_ref())
        .map_err(|_| LoginError::AuthorizationRequest)?;
    let authorization_url = pending.authorization_url().to_owned();
    validate_provider_authorization_url(provider_id, &authorization_url)?;
    let authorization = async {
        let opener_succeeded = open_browser(&authorization_url).await;
        let copied_to_clipboard = if opener_succeeded {
            false
        } else {
            copy_authorization_url(&authorization_url).await
        };
        let presentation =
            choose_browser_presentation(opener_succeeded, copied_to_clipboard, interactive_tty)?;
        present_browser(presentation, &authorization_url);

        let callback =
            wait_for_callback(&listener, &expected_host, &callback_path, &origin).await?;
        let mut stream = callback.stream;
        let credentials = match tokio::time::timeout(
            EXCHANGE_TIMEOUT,
            client.complete_authorization(pending, &callback.callback_url),
        )
        .await
        {
            Ok(Ok(credentials)) => credentials,
            _ => {
                respond_to_callback(&mut stream, false).await;
                return Err(LoginError::AuthorizationFailed);
            }
        };
        respond_to_callback(&mut stream, true).await;
        upload_credentials(
            config,
            provider_id,
            account_name,
            eggpool_token,
            &credentials,
        )
        .await?;
        Ok(())
    };
    cancel_on_interrupt(authorization, async {
        let _ = tokio::signal::ctrl_c().await;
    })
    .await
}

fn load_or_create_openai_installation_id(state_dir: &Path) -> Result<String, LoginError> {
    crate::operations::paths::ensure_private_directory(state_dir)
        .map_err(|_| LoginError::InstallationIdentity)?;
    let path = state_dir.join(OPENAI_INSTALLATION_ID_FILENAME);
    if let Some(installation_id) = read_openai_installation_id(&path)? {
        return Ok(installation_id);
    }

    let installation_id = generate_openai_installation_host_id()?;
    let uuid_suffix = installation_id
        .strip_prefix("urn:uuid:")
        .filter(|uuid| is_canonical_uuid_v4(uuid))
        .ok_or(LoginError::InstallationIdentity)?;
    let temporary = state_dir.join(format!(
        ".{OPENAI_INSTALLATION_ID_FILENAME}.{uuid_suffix}.tmp"
    ));
    let mut file = crate::operations::paths::create_private_file(&temporary)
        .map_err(|_| LoginError::InstallationIdentity)?;
    if file
        .write_all(format!("{installation_id}\n").as_bytes())
        .and_then(|()| file.sync_all())
        .is_err()
    {
        drop(file);
        let _ = fs::remove_file(&temporary);
        return Err(LoginError::InstallationIdentity);
    }
    drop(file);

    match fs::hard_link(&temporary, &path) {
        Ok(()) => {
            let _ = fs::remove_file(&temporary);
            fs::File::open(state_dir)
                .and_then(|directory| directory.sync_all())
                .map_err(|_| LoginError::InstallationIdentity)?;
            Ok(installation_id)
        }
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
            let _ = fs::remove_file(&temporary);
            read_openai_installation_id(&path)?.ok_or(LoginError::InstallationIdentity)
        }
        Err(_) => {
            let _ = fs::remove_file(&temporary);
            Err(LoginError::InstallationIdentity)
        }
    }
}

fn read_openai_installation_id(path: &Path) -> Result<Option<String>, LoginError> {
    let metadata = match fs::symlink_metadata(path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(_) => return Err(LoginError::InstallationIdentity),
    };
    if !metadata.is_file() {
        return Err(LoginError::InstallationIdentity);
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::{MetadataExt, PermissionsExt};
        if metadata.uid() != nix::unistd::geteuid().as_raw()
            || metadata.permissions().mode() & 0o077 != 0
        {
            return Err(LoginError::InstallationIdentity);
        }
    }

    let contents = fs::read_to_string(path).map_err(|_| LoginError::InstallationIdentity)?;
    let installation_id = contents.strip_suffix('\n').unwrap_or(&contents);
    if !is_canonical_openai_host_id(installation_id) {
        return Err(LoginError::InstallationIdentity);
    }
    Ok(Some(installation_id.to_owned()))
}

fn generate_openai_installation_host_id() -> Result<String, LoginError> {
    let mut bytes = [0_u8; 16];
    getrandom::fill(&mut bytes).map_err(|_| LoginError::InstallationIdentity)?;
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    let uuid = format!(
        "{:02x}{:02x}{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}{:02x}{:02x}{:02x}{:02x}",
        bytes[0],
        bytes[1],
        bytes[2],
        bytes[3],
        bytes[4],
        bytes[5],
        bytes[6],
        bytes[7],
        bytes[8],
        bytes[9],
        bytes[10],
        bytes[11],
        bytes[12],
        bytes[13],
        bytes[14],
        bytes[15]
    );
    Ok(format!("urn:uuid:{uuid}"))
}

fn is_canonical_uuid_v4(value: &str) -> bool {
    let bytes = value.as_bytes();
    bytes.len() == 36
        && bytes[8] == b'-'
        && bytes[13] == b'-'
        && bytes[18] == b'-'
        && bytes[23] == b'-'
        && bytes[14] == b'4'
        && matches!(bytes[19], b'8' | b'9' | b'a' | b'b')
        && bytes.iter().enumerate().all(|(index, byte)| {
            matches!(index, 8 | 13 | 18 | 23)
                || byte.is_ascii_digit()
                || (b'a'..=b'f').contains(byte)
        })
}

fn is_canonical_openai_host_id(value: &str) -> bool {
    value
        .strip_prefix("urn:uuid:")
        .is_some_and(is_canonical_uuid_v4)
}

fn openai_provider_config(host_id: String) -> Result<OAuthProviderConfig, LoginError> {
    OAuthProviderConfig::custom("OpenAI", OPENAI_ISSUER, OPENAI_ENTRY_CLIENT_ID)
        .authorization_endpoint(format!("{OPENAI_ISSUER}/api/accounts/authorize"))
        .token_endpoint(format!("{OPENAI_ISSUER}/api/accounts/oauth/token"))
        .userinfo_endpoint(format!("{OPENAI_ISSUER}/api/accounts/oauth/userinfo"))
        .revocation_endpoint(format!("{OPENAI_ISSUER}/api/accounts/oauth/revoke"))
        .callback_path("/auth/callback")
        .client_id_policy(ClientIdPolicy::IssuedOnRegistration)
        .scopes(OPENAI_SCOPES.iter().copied())
        .authorization_parameter("resource", OPENAI_RESOURCE)
        .authorization_parameter("ext_agent_host_id", host_id)
        .registration_parameter("agent_name_hint", OPENAI_AGENT_NAME)
        .token_parameter("resource", OPENAI_RESOURCE)
        .build()
        .map_err(|_| LoginError::AuthorizationRequest)
}

fn choose_browser_presentation(
    opener_succeeded: bool,
    copied_to_clipboard: bool,
    interactive_tty: bool,
) -> Result<BrowserPresentation, LoginError> {
    if opener_succeeded {
        return Ok(BrowserPresentation::BrowserOpenerSucceeded);
    }
    if copied_to_clipboard {
        return Ok(BrowserPresentation::CopiedToClipboard);
    }
    if interactive_tty {
        return Ok(BrowserPresentation::ManualUrl);
    }
    Err(LoginError::BrowserUnavailable)
}

fn valid_eggpool_token(token: &str) -> bool {
    token.starts_with("epk1_")
        && token.len() <= 128
        && !token
            .bytes()
            .any(|byte| byte.is_ascii_whitespace() || byte.is_ascii_control())
}

fn valid_callback_path(path: &str) -> bool {
    path.starts_with('/')
        && path.len() <= 128
        && !path.contains(['?', '#', '\\'])
        && !path.chars().any(char::is_control)
}

fn validate_provider_authorization_url(provider_id: &str, raw: &str) -> Result<(), LoginError> {
    match provider_id {
        "openai" => validate_openai_authorization_url(raw),
        "xai" => validate_xai_authorization_url(raw),
        _ => Err(LoginError::AuthorizationRequest),
    }
}

fn validate_openai_authorization_url(raw: &str) -> Result<(), LoginError> {
    let uri: Uri = raw.parse().map_err(|_| LoginError::AuthorizationRequest)?;
    let Some(authority) = uri.authority() else {
        return Err(LoginError::AuthorizationRequest);
    };
    if uri.scheme_str() != Some("https")
        || authority.host() != "auth.openai.com"
        || authority.as_str().contains('@')
        || authority.port_u16().is_some_and(|port| port != 443)
        || uri.path() != "/api/accounts/authorize"
        || uri.query().is_none()
        || raw.contains('#')
    {
        return Err(LoginError::AuthorizationRequest);
    }
    Ok(())
}

fn validate_xai_authorization_url(raw: &str) -> Result<(), LoginError> {
    let uri: Uri = raw.parse().map_err(|_| LoginError::AuthorizationRequest)?;
    let Some(authority) = uri.authority() else {
        return Err(LoginError::AuthorizationRequest);
    };
    if uri.scheme_str() != Some("https")
        || authority.host() != "auth.x.ai"
        || authority.as_str().contains('@')
        || authority.port_u16().is_some_and(|port| port != 443)
        || uri.path() == "/"
        || uri.query().is_none()
        || raw.contains('#')
    {
        return Err(LoginError::AuthorizationRequest);
    }
    Ok(())
}

async fn wait_for_callback(
    listener: &TcpListener,
    expected_host: &str,
    expected_path: &str,
    origin: &str,
) -> Result<CallbackConnection, LoginError> {
    let accept = async {
        let (mut stream, peer) = listener
            .accept()
            .await
            .map_err(|_| LoginError::CallbackListener)?;
        if !peer.ip().is_loopback() {
            respond_to_callback(&mut stream, false).await;
            return Err(LoginError::InvalidCallback);
        }
        match read_callback_url(&mut stream, expected_host, expected_path, origin).await {
            Ok(callback_url) => Ok(CallbackConnection {
                stream,
                callback_url,
            }),
            Err(()) => {
                respond_to_callback(&mut stream, false).await;
                Err(LoginError::InvalidCallback)
            }
        }
    };

    tokio::time::timeout(CALLBACK_TIMEOUT, accept)
        .await
        .map_err(|_| LoginError::CallbackTimeout)?
}

async fn cancel_on_interrupt<T>(
    work: impl Future<Output = Result<T, LoginError>>,
    interrupt: impl Future<Output = ()>,
) -> Result<T, LoginError> {
    tokio::select! {
        biased;
        _ = interrupt => Err(LoginError::Canceled),
        result = work => result,
    }
}

async fn read_callback_url(
    stream: &mut TcpStream,
    expected_host: &str,
    expected_path: &str,
    origin: &str,
) -> Result<String, ()> {
    let mut bytes = Vec::with_capacity(1024);
    let mut chunk = [0_u8; 1024];
    loop {
        let read = stream.read(&mut chunk).await.map_err(|_| ())?;
        if read == 0 {
            return Err(());
        }
        bytes.extend_from_slice(&chunk[..read]);
        if bytes.len() > MAX_CALLBACK_REQUEST_BYTES {
            return Err(());
        }
        if bytes
            .windows(CALLBACK_HEADER_TERMINATOR.len())
            .any(|window| window == CALLBACK_HEADER_TERMINATOR)
        {
            break;
        }
    }
    parse_callback_request(&bytes, expected_host, expected_path, origin)
}

fn parse_callback_request(
    bytes: &[u8],
    expected_host: &str,
    expected_path: &str,
    origin: &str,
) -> Result<String, ()> {
    if bytes.len() > MAX_CALLBACK_REQUEST_BYTES {
        return Err(());
    }
    let Some(terminator_start) = bytes
        .windows(CALLBACK_HEADER_TERMINATOR.len())
        .position(|window| window == CALLBACK_HEADER_TERMINATOR)
    else {
        return Err(());
    };
    if terminator_start + CALLBACK_HEADER_TERMINATOR.len() != bytes.len() {
        return Err(());
    }
    let headers = std::str::from_utf8(&bytes[..terminator_start]).map_err(|_| ())?;
    let (request_line, header_block) = headers.split_once("\r\n").ok_or(())?;
    if request_line.is_empty()
        || request_line.len() > MAX_CALLBACK_REQUEST_LINE_BYTES
        || !request_line.is_ascii()
        || request_line.bytes().any(|byte| byte.is_ascii_control())
        || header_block.len() > MAX_CALLBACK_HEADER_BYTES
    {
        return Err(());
    }

    let mut request_line_parts = request_line.split(' ');
    if request_line_parts.next() != Some("GET") {
        return Err(());
    }
    let target = request_line_parts.next().ok_or(())?;
    if request_line_parts.next() != Some("HTTP/1.1") || request_line_parts.next().is_some() {
        return Err(());
    }
    if !target.starts_with('/') || target.starts_with("//") || target.contains('#') {
        return Err(());
    }
    let (path, query) = target.split_once('?').ok_or(())?;
    if path != expected_path
        || query.is_empty()
        || query.len() > MAX_CALLBACK_QUERY_BYTES
        || !openai_callback_query_is_valid(query)
    {
        return Err(());
    }

    let mut host = None;
    let mut content_length = None;
    let mut transfer_encoding = false;
    for line in header_block.split("\r\n") {
        if line.is_empty() || line.len() > MAX_CALLBACK_HEADER_LINE_BYTES || !line.is_ascii() {
            return Err(());
        }
        let (name, value) = line.split_once(':').ok_or(())?;
        if name.is_empty()
            || !name.bytes().all(|byte| {
                byte.is_ascii_alphanumeric()
                    || matches!(
                        byte,
                        33 | 35 | 36 | 37 | 38 | 39 | 42 | 43 | 45 | 46 | 94 | 95 | 96 | 124 | 126
                    )
            })
            || value
                .bytes()
                .any(|byte| (byte < 0x20 && byte != b'\t') || byte == 0x7f)
        {
            return Err(());
        }
        let value = value.trim_matches([' ', '\t']);
        if name.eq_ignore_ascii_case("host") {
            if host.replace(value).is_some() {
                return Err(());
            }
        } else if name.eq_ignore_ascii_case("content-length") {
            if content_length.replace(value).is_some() {
                return Err(());
            }
        } else if name.eq_ignore_ascii_case("transfer-encoding") {
            transfer_encoding = true;
        }
    }
    if host != Some(expected_host)
        || transfer_encoding
        || content_length.is_some_and(|length| length != "0")
    {
        return Err(());
    }
    Ok(format!("{}{}", origin, target))
}

fn openai_callback_query_is_valid(query: &str) -> bool {
    let mut keys = std::collections::BTreeSet::new();
    for pair in query.split('&') {
        if pair.is_empty() {
            return false;
        }
        let (raw_key, raw_value) = pair.split_once('=').unwrap_or((pair, ""));
        let Ok(key) = decode_query_component(raw_key) else {
            return false;
        };
        if key.is_empty()
            || !matches!(
                key.as_str(),
                "code" | "state" | "error" | "client_id" | "scope"
            )
            || !keys.insert(key)
            || decode_query_component(raw_value).is_err()
        {
            return false;
        }
    }
    true
}

fn decode_query_component(raw: &str) -> Result<String, ()> {
    let bytes = raw.as_bytes();
    let mut decoded = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        match bytes[index] {
            b'+' => {
                decoded.push(b' ');
                index += 1;
            }
            b'%' => {
                let high = *bytes.get(index + 1).ok_or(())?;
                let low = *bytes.get(index + 2).ok_or(())?;
                decoded.push((hex_value(high).ok_or(())? << 4) | hex_value(low).ok_or(())?);
                index += 3;
            }
            byte => {
                if byte.is_ascii_control() {
                    return Err(());
                }
                decoded.push(byte);
                index += 1;
            }
        }
    }
    let value = String::from_utf8(decoded).map_err(|_| ())?;
    if value.chars().any(char::is_control) {
        return Err(());
    }
    Ok(value)
}

fn hex_value(byte: u8) -> Option<u8> {
    match byte {
        b'0'..=b'9' => Some(byte - b'0'),
        b'a'..=b'f' => Some(byte - b'a' + 10),
        b'A'..=b'F' => Some(byte - b'A' + 10),
        _ => None,
    }
}

async fn respond_to_callback(stream: &mut TcpStream, accepted: bool) {
    let (status, body) = if accepted {
        (
            "200 OK",
            "<html><body><p>Authorization received. Return to EggPool CLI.</p></body></html>",
        )
    } else {
        (
            "400 Bad Request",
            "<html><body><p>Authorization callback was rejected. Return to EggPool CLI.</p></body></html>",
        )
    };
    let response = format!(
        "HTTP/1.1 {status}\r\nContent-Type: text/html; charset=utf-8\r\nCache-Control: no-store\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    );
    let _ = stream.write_all(response.as_bytes()).await;
}

async fn open_browser(url: &str) -> bool {
    #[cfg(target_os = "macos")]
    {
        let mut command = TokioCommand::new("open");
        command.arg(url);
        confirm_browser_launch(command).await
    }
    #[cfg(target_os = "linux")]
    {
        let mut command = TokioCommand::new("xdg-open");
        command.arg(url);
        confirm_browser_launch(command).await
    }
    #[cfg(target_os = "windows")]
    {
        let mut command = TokioCommand::new("rundll32.exe");
        command.args(["url.dll,FileProtocolHandler", url]);
        confirm_browser_launch(command).await
    }
    #[cfg(not(any(target_os = "macos", target_os = "linux", target_os = "windows")))]
    {
        let _ = url;
        false
    }
}

async fn confirm_browser_launch(mut command: TokioCommand) -> bool {
    command
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .kill_on_drop(true);
    let Ok(mut child) = command.spawn() else {
        return false;
    };
    matches!(
        tokio::time::timeout(BROWSER_OPEN_CONFIRM_TIMEOUT, child.wait()).await,
        Ok(Ok(status)) if status.success()
    )
}

async fn copy_authorization_url(url: &str) -> bool {
    #[cfg(target_os = "macos")]
    return pipe_to_clipboard("pbcopy", &[], url).await;
    #[cfg(target_os = "windows")]
    return pipe_to_clipboard("clip.exe", &[], url).await;
    #[cfg(target_os = "linux")]
    {
        if pipe_to_clipboard("wl-copy", &[], url).await {
            return true;
        }
        if pipe_to_clipboard("xclip", &["-selection", "clipboard"], url).await {
            return true;
        }
        return pipe_to_clipboard("xsel", &["--clipboard", "--input"], url).await;
    }
    #[cfg(not(any(target_os = "macos", target_os = "linux", target_os = "windows")))]
    {
        let _ = url;
        false
    }
}

async fn pipe_to_clipboard(program: &str, args: &[&str], value: &str) -> bool {
    let Ok(mut child) = TokioCommand::new(program)
        .args(args)
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .kill_on_drop(true)
        .spawn()
    else {
        return false;
    };
    let operation = async {
        let Some(mut stdin) = child.stdin.take() else {
            return Err(std::io::Error::new(
                std::io::ErrorKind::BrokenPipe,
                "clipboard process has no input pipe",
            ));
        };
        stdin.write_all(value.as_bytes()).await?;
        drop(stdin);
        child.wait().await
    };
    match tokio::time::timeout(CLIPBOARD_TIMEOUT, operation).await {
        Ok(Ok(status)) => status.success(),
        _ => {
            let _ = child.kill().await;
            let _ = child.wait().await;
            false
        }
    }
}

fn local_api_uri(config: &Config) -> Result<Uri, LoginError> {
    local_api_uri_for_path(config, OAUTH_UPLOAD_PATH)
}

fn local_api_uri_for_path(config: &Config, path: &str) -> Result<Uri, LoginError> {
    let bind_host = config.server.host.trim_matches(['[', ']']);
    let host = match bind_host.parse::<IpAddr>() {
        Ok(IpAddr::V4(address)) if address.is_loopback() => "127.0.0.1".to_owned(),
        Ok(IpAddr::V6(address)) if address.is_loopback() => "[::1]".to_owned(),
        Ok(IpAddr::V4(address)) if address.is_unspecified() => "127.0.0.1".to_owned(),
        Ok(IpAddr::V6(address)) if address.is_unspecified() => "[::1]".to_owned(),
        Err(_) if bind_host.eq_ignore_ascii_case("localhost") => "127.0.0.1".to_owned(),
        _ => return Err(LoginError::LocalServerUnavailable),
    };
    format!("http://{host}:{}{path}", config.server.port)
        .parse()
        .map_err(|_| LoginError::LocalServerUnavailable)
}

#[derive(Serialize)]
struct OAuthAuthorizationHintRequest<'a> {
    account_name: &'a str,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct OAuthAuthorizationHintResponse {
    #[serde(default)]
    client_id: Option<String>,
}

async fn request_authorization_hint(
    config: &Config,
    account_name: &str,
    eggpool_token: &str,
) -> Result<Option<OAuthCredentials>, LoginError> {
    let uri = local_api_uri_for_path(config, OAUTH_REAUTHORIZATION_HINT_PATH)?;
    let payload = serde_json::to_vec(&OAuthAuthorizationHintRequest { account_name })
        .map_err(|_| LoginError::ReauthorizationHint)?;
    let authorization = format!("Bearer {eggpool_token}");
    let authorization =
        HeaderValue::from_str(&authorization).map_err(|_| LoginError::InvalidEggPoolCredential)?;
    let request = Request::builder()
        .method(Method::POST)
        .uri(uri)
        .header(CONTENT_TYPE, "application/json")
        .header(AUTHORIZATION, authorization)
        .body(Full::new(Bytes::from(payload)))
        .map_err(|_| LoginError::ReauthorizationHint)?;
    let mut connector = HttpConnector::new();
    connector.enforce_http(true);
    let client: Client<HttpConnector, Full<Bytes>> =
        Client::builder(TokioExecutor::new()).build(connector);
    let response = tokio::time::timeout(UPLOAD_TIMEOUT, client.request(request))
        .await
        .map_err(|_| LoginError::ReauthorizationHint)?
        .map_err(|_| LoginError::ReauthorizationHint)?;
    if response.status() != StatusCode::OK {
        return Err(LoginError::ReauthorizationHint);
    }
    let body = Limited::new(response.into_body(), 4 * 1024)
        .collect()
        .await
        .map_err(|_| LoginError::ReauthorizationHint)?
        .to_bytes();
    let hint: OAuthAuthorizationHintResponse =
        serde_json::from_slice(&body).map_err(|_| LoginError::ReauthorizationHint)?;
    let Some(client_id) = hint.client_id else {
        return Ok(None);
    };
    Ok(Some(existing_openai_client_hint(&client_id)?))
}

fn existing_openai_client_hint(client_id: &str) -> Result<OAuthCredentials, LoginError> {
    if client_id.is_empty() || client_id.len() > 1024 || client_id.chars().any(char::is_control) {
        return Err(LoginError::ReauthorizationHint);
    }
    serde_json::from_value(serde_json::json!({
        "client_id": client_id,
        "issuer": OPENAI_ISSUER,
    }))
    .map_err(|_| LoginError::ReauthorizationHint)
}

async fn upload_credentials(
    config: &Config,
    provider_id: &str,
    account_name: &str,
    eggpool_token: &str,
    credentials: &OAuthCredentials,
) -> Result<(), LoginError> {
    let uri = local_api_uri(config)?;
    let payload = serde_json::to_vec(&CredentialUpload {
        provider_id,
        account_name,
        credentials,
    })
    .map_err(|_| LoginError::CommitFailed)?;
    let authorization = format!("Bearer {eggpool_token}");
    let authorization =
        HeaderValue::from_str(&authorization).map_err(|_| LoginError::InvalidEggPoolCredential)?;
    let request = Request::builder()
        .method(Method::POST)
        .uri(uri)
        .header(CONTENT_TYPE, "application/json")
        .header(AUTHORIZATION, authorization)
        .body(Full::new(Bytes::from(payload)))
        .map_err(|_| LoginError::CommitFailed)?;
    let mut connector = HttpConnector::new();
    connector.enforce_http(true);
    let client: Client<HttpConnector, Full<Bytes>> =
        Client::builder(TokioExecutor::new()).build(connector);
    let response = tokio::time::timeout(UPLOAD_TIMEOUT, client.request(request))
        .await
        .map_err(|_| LoginError::CommitFailed)?
        .map_err(|_| LoginError::CommitFailed)?;
    if response.status() != StatusCode::NO_CONTENT && !response.status().is_success() {
        return Err(LoginError::CommitFailed);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn query_value(query: &str, wanted: &str) -> Option<String> {
        query.split('&').find_map(|pair| {
            let (raw_key, raw_value) = pair.split_once('=')?;
            (decode_query_component(raw_key).ok()?.as_str() == wanted)
                .then(|| decode_query_component(raw_value).ok())
                .flatten()
        })
    }

    async fn read_http_request(stream: &mut TcpStream) -> Vec<u8> {
        let mut request = Vec::new();
        let mut chunk = [0_u8; 1024];
        loop {
            let read = stream.read(&mut chunk).await.expect("read HTTP request");
            if read == 0 {
                break;
            }
            request.extend_from_slice(&chunk[..read]);
            if let Some(headers_end) = request
                .windows(CALLBACK_HEADER_TERMINATOR.len())
                .position(|window| window == CALLBACK_HEADER_TERMINATOR)
            {
                let headers = String::from_utf8_lossy(&request[..headers_end]);
                let body_len = headers
                    .lines()
                    .find_map(|line| {
                        let (name, value) = line.split_once(':')?;
                        name.eq_ignore_ascii_case("content-length")
                            .then(|| value.trim().parse::<usize>().ok())
                            .flatten()
                    })
                    .unwrap_or(0);
                if request.len() >= headers_end + CALLBACK_HEADER_TERMINATOR.len() + body_len {
                    break;
                }
            }
        }
        request
    }

    #[test]
    fn returning_account_hint_contains_no_credentials_or_id_token() {
        let hint = existing_openai_client_hint("issued-client-id").expect("client ID hint");
        assert_eq!(hint.client_id(), "issued-client-id");
        assert_eq!(hint.issuer(), OPENAI_ISSUER);
        assert!(hint.access_token().is_empty());
        assert!(hint.refresh_token().is_none());
        assert!(hint.id_token().is_none());
    }

    #[test]
    fn authorization_url_is_limited_to_the_openai_https_endpoint() {
        assert!(
            validate_openai_authorization_url(
                "https://auth.openai.com/api/accounts/authorize?client_id=dynamic_agent_client"
            )
            .is_ok()
        );
        for url in [
            "http://auth.openai.com/api/accounts/authorize?client_id=x",
            "https://attacker.example/api/accounts/authorize?client_id=x",
            "https://user@auth.openai.com/api/accounts/authorize?client_id=x",
            "https://auth.openai.com:444/api/accounts/authorize?client_id=x",
            "https://auth.openai.com/other?client_id=x",
            "https://auth.openai.com/api/accounts/authorize#fragment",
        ] {
            assert!(validate_openai_authorization_url(url).is_err());
        }
    }

    #[test]
    fn xai_authorization_url_is_limited_to_the_xai_https_host() {
        let client = OAuthClient::new(eggfetch_core::Client::new(), OAuthProviderConfig::xai());
        let redirect_uri = format!(
            "http://127.0.0.1:43123{}",
            client.provider().callback_path()
        );
        let pending = client
            .begin_authorization(&redirect_uri, None)
            .expect("xAI authorization request");
        let authorization_url = pending.authorization_url();
        validate_provider_authorization_url("xai", authorization_url)
            .expect("xAI authorization host is trusted");
        for url in [
            "http://auth.x.ai/oauth/authorize?state=x",
            "https://attacker.example/oauth/authorize?state=x",
            "https://auth.x.ai.evil.example/oauth/authorize?state=x",
            "https://user@auth.x.ai/oauth/authorize?state=x",
            "https://auth.x.ai:444/oauth/authorize?state=x",
            "https://auth.x.ai/oauth/authorize#fragment",
        ] {
            assert!(validate_provider_authorization_url("xai", url).is_err());
        }
    }

    #[test]
    fn credential_upload_keeps_the_selected_provider_id() {
        let credentials: OAuthCredentials = serde_json::from_value(serde_json::json!({
            "access_token": "fixture-access-token",
            "refresh_token": "fixture-refresh-token",
            "token_type": "Bearer",
            "scope": "openid offline_access",
            "client_id": "fixture-client-id",
            "issuer": "https://auth.x.ai",
            "expires_at": 1_900_000_000_i64,
            "obtained_at": 1_800_000_000_i64
        }))
        .expect("xAI credential fixture");
        let payload = serde_json::to_value(CredentialUpload {
            provider_id: "xai",
            account_name: "personal",
            credentials: &credentials,
        })
        .expect("serialize xAI upload");
        assert_eq!(payload["provider_id"], "xai");
        assert_eq!(payload["account_name"], "personal");
    }

    #[test]
    fn callback_parser_accepts_only_exact_loopback_host_path_and_unique_query() {
        let request = b"GET /auth/callback?code=fixture-code&state=fixture-state HTTP/1.1\r\nHost: 127.0.0.1:43123\r\nConnection: close\r\n\r\n";
        assert!(
            parse_callback_request(
                request,
                "127.0.0.1:43123",
                "/auth/callback",
                "http://127.0.0.1:43123"
            )
            .is_ok()
        );
        for request in [
            &b"GET /wrong?code=x&state=y HTTP/1.1\r\nHost: 127.0.0.1:43123\r\n\r\n"[..],
            &b"GET /auth/callback?code=x&state=y HTTP/1.1\r\nHost: 127.0.0.1:43124\r\n\r\n"[..],
            &b"GET /auth/callback?code=x&state=y&%73tate=z HTTP/1.1\r\nHost: 127.0.0.1:43123\r\n\r\n"[..],
            &b"GET http://127.0.0.1:43123/auth/callback?code=x&state=y HTTP/1.1\r\nHost: 127.0.0.1:43123\r\n\r\n"[..],
            &b"POST /auth/callback?code=x&state=y HTTP/1.1\r\nHost: 127.0.0.1:43123\r\n\r\n"[..],
            &b"GET /auth/callback?code=x&state=y#fragment HTTP/1.1\r\nHost: 127.0.0.1:43123\r\n\r\n"[..],
            &b"GET /auth/callback?code=x&state=y HTTP/1.1\r\nHost: 127.0.0.1:43123\r\nHost: 127.0.0.1:43123\r\n\r\n"[..],
            &b"GET /auth/callback?code=x&state=y HTTP/1.1\nHost: 127.0.0.1:43123\n\n"[..],
            &b"GET /auth/callback?code=x&state=y HTTP/1.1\r\nHost: 127.0.0.1:43123\r\nContent-Length: 1\r\n\r\nx"[..],
        ] {
            assert!(
                parse_callback_request(
                    request,
                    "127.0.0.1:43123",
                    "/auth/callback",
                    "http://127.0.0.1:43123"
                )
                .is_err()
            );
        }

        let oversized_request_line = format!(
            "GET /auth/callback?code={}&state=y HTTP/1.1\r\nHost: 127.0.0.1:43123\r\n\r\n",
            "x".repeat(MAX_CALLBACK_REQUEST_LINE_BYTES)
        );
        assert!(
            parse_callback_request(
                oversized_request_line.as_bytes(),
                "127.0.0.1:43123",
                "/auth/callback",
                "http://127.0.0.1:43123"
            )
            .is_err()
        );

        let oversized_header = format!(
            "GET /auth/callback?code=x&state=y HTTP/1.1\r\nHost: 127.0.0.1:43123\r\nX-Long: {}\r\n\r\n",
            "x".repeat(MAX_CALLBACK_HEADER_LINE_BYTES)
        );
        assert!(
            parse_callback_request(
                oversized_header.as_bytes(),
                "127.0.0.1:43123",
                "/auth/callback",
                "http://127.0.0.1:43123"
            )
            .is_err()
        );

        let oversized_header_block = format!(
            "GET /auth/callback?code=x&state=y HTTP/1.1\r\nHost: 127.0.0.1:43123\r\nX-One: {}\r\nX-Two: {}\r\nX-Three: {}\r\nX-Four: {}\r\nX-Five: {}\r\n\r\n",
            "x".repeat(900),
            "x".repeat(900),
            "x".repeat(900),
            "x".repeat(900),
            "x".repeat(900)
        );
        assert!(
            parse_callback_request(
                oversized_header_block.as_bytes(),
                "127.0.0.1:43123",
                "/auth/callback",
                "http://127.0.0.1:43123"
            )
            .is_err()
        );
    }

    #[test]
    fn callback_parser_accepts_documented_openai_success_and_denial_parameters() {
        for query in [
            "code=fixture-code&state=fixture-state",
            "code=fixture-code&state=fixture-state&client_id=oaiapp_fixture&scope=openid+profile+email",
            "error=access_denied&state=fixture-state",
        ] {
            let request =
                format!("GET /auth/callback?{query} HTTP/1.1\r\nHost: 127.0.0.1:43123\r\n\r\n");
            assert!(
                parse_callback_request(
                    request.as_bytes(),
                    "127.0.0.1:43123",
                    "/auth/callback",
                    "http://127.0.0.1:43123"
                )
                .is_ok()
            );
        }
    }

    #[test]
    fn callback_parser_rejects_unknown_and_percent_encoded_unknown_parameters() {
        for query in [
            "code=x&state=y&unexpected=z",
            "code=x&state=y&%75nexpected=z",
            "code=x&state=y&error_description=denied",
        ] {
            let request =
                format!("GET /auth/callback?{query} HTTP/1.1\r\nHost: 127.0.0.1:43123\r\n\r\n");
            assert!(
                parse_callback_request(
                    request.as_bytes(),
                    "127.0.0.1:43123",
                    "/auth/callback",
                    "http://127.0.0.1:43123"
                )
                .is_err()
            );
        }
    }

    #[tokio::test]
    async fn callback_reader_accepts_browser_crlf_request_over_loopback_socket() {
        let listener = TcpListener::bind(("127.0.0.1", 0))
            .await
            .expect("loopback callback listener");
        let address = listener.local_addr().expect("loopback callback address");
        let host = format!("127.0.0.1:{}", address.port());
        let origin = format!("http://{host}");
        let mut browser = TcpStream::connect(address)
            .await
            .expect("loopback browser connection");
        let request = format!(
            "GET /auth/callback?code=fixture-code&state=fixture-state HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n"
        );
        browser
            .write_all(request.as_bytes())
            .await
            .expect("write browser callback request");

        let callback = wait_for_callback(&listener, &host, "/auth/callback", &origin)
            .await
            .expect("accept browser callback over loopback socket");
        assert_eq!(
            callback.callback_url,
            format!(
                "{}{}",
                origin, "/auth/callback?code=fixture-code&state=fixture-state"
            )
        );
        let mut callback_stream = callback.stream;
        respond_to_callback(&mut callback_stream, true).await;
        drop(callback_stream);

        let mut response = Vec::new();
        browser
            .read_to_end(&mut response)
            .await
            .expect("read callback response");
        assert!(response.starts_with(b"HTTP/1.1 200 OK\r\n"));
    }

    #[test]
    fn query_parser_rejects_malformed_encoding_duplicates_and_unknown_keys() {
        assert!(!openai_callback_query_is_valid("code=x&state=y&state=z"));
        assert!(!openai_callback_query_is_valid("code=x&%73tate=y&state=z"));
        assert!(!openai_callback_query_is_valid("code=%GG&state=y"));
        assert!(!openai_callback_query_is_valid("code=%FF&state=y"));
        assert!(!openai_callback_query_is_valid("code=x&state=y&other=z"));
        assert!(!openai_callback_query_is_valid("code=x&state=y&%6Fther=z"));
        assert!(openai_callback_query_is_valid(
            "code=one%2Btwo&state=unique"
        ));
        assert!(openai_callback_query_is_valid(
            "code=x&state=y&client_id=oaiapp_fixture&scope=openid+profile"
        ));
        assert!(openai_callback_query_is_valid(
            "error=access_denied&state=y"
        ));
    }

    #[test]
    fn browser_presentation_prefers_successful_opener_then_clipboard_then_tty_url() {
        assert_eq!(
            choose_browser_presentation(true, false, false).unwrap(),
            BrowserPresentation::BrowserOpenerSucceeded
        );
        assert_eq!(
            choose_browser_presentation(false, true, false).unwrap(),
            BrowserPresentation::CopiedToClipboard
        );
        assert_eq!(
            choose_browser_presentation(false, false, true).unwrap(),
            BrowserPresentation::ManualUrl
        );
        assert!(matches!(
            choose_browser_presentation(false, false, false),
            Err(LoginError::BrowserUnavailable)
        ));
    }

    #[tokio::test]
    async fn ctrl_c_after_callback_cancels_exchange_before_upload() {
        let (exchange_started_tx, exchange_started_rx) = tokio::sync::oneshot::channel();
        let (cancel_tx, cancel_rx) = tokio::sync::oneshot::channel();
        let upload_count = std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let observed_uploads = upload_count.clone();
        let work = async move {
            exchange_started_tx.send(()).expect("signal exchange start");
            std::future::pending::<()>().await;
            observed_uploads.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
            Ok(())
        };
        let task = tokio::spawn(cancel_on_interrupt(work, async move {
            let _ = cancel_rx.await;
        }));
        exchange_started_rx.await.expect("exchange started");
        cancel_tx.send(()).expect("cancel during exchange");
        assert!(matches!(
            task.await.expect("canceled exchange task"),
            Err(LoginError::Canceled)
        ));
        assert_eq!(upload_count.load(std::sync::atomic::Ordering::SeqCst), 0);
    }

    #[tokio::test]
    async fn ctrl_c_during_upload_drops_pending_commit_work() {
        let (upload_started_tx, upload_started_rx) = tokio::sync::oneshot::channel();
        let (cancel_tx, cancel_rx) = tokio::sync::oneshot::channel();
        let committed = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
        let observed_commit = committed.clone();
        let (commit_gate_tx, commit_gate_rx) = tokio::sync::oneshot::channel::<()>();
        let work = async move {
            upload_started_tx.send(()).expect("signal upload start");
            let _ = commit_gate_rx.await;
            observed_commit.store(true, std::sync::atomic::Ordering::SeqCst);
            Ok(())
        };
        let task = tokio::spawn(cancel_on_interrupt(work, async move {
            let _ = cancel_rx.await;
        }));
        upload_started_rx.await.expect("upload started");
        cancel_tx.send(()).expect("cancel during upload");
        assert!(matches!(
            task.await.expect("canceled upload task"),
            Err(LoginError::Canceled)
        ));
        assert!(!committed.load(std::sync::atomic::Ordering::SeqCst));
        drop(commit_gate_tx);
    }

    #[test]
    fn openai_authorization_url_contains_the_stable_host_identifier() {
        let host_id = "urn:uuid:00000000-0000-4000-8000-000000000000";
        let provider = openai_provider_config(host_id.to_owned())
            .expect("EggPool OpenAI profile with host identifier");
        let client = OAuthClient::new(eggfetch_core::Client::new(), provider);
        let pending = client
            .begin_authorization("http://127.0.0.1:43123/auth/callback", None)
            .expect("authorization request");
        let authorization_uri: Uri = pending
            .authorization_url()
            .parse()
            .expect("authorization URL");
        let query = authorization_uri.query().expect("authorization query");
        assert!(
            query.contains("ext_agent_host_id=urn%3Auuid%3A00000000-0000-4000-8000-000000000000")
        );
        assert!(query.contains("agent_name_hint=EggPool"));
        assert!(!query.contains("agent_name_hint=eggpass"));
    }

    #[test]
    fn openai_installation_identity_is_stable_canonical_and_private() {
        let directory = tempfile::tempdir().expect("state directory");
        let installation_id = load_or_create_openai_installation_id(directory.path())
            .expect("create OpenAI installation identity");
        assert!(is_canonical_openai_host_id(&installation_id));
        assert!(installation_id.starts_with("urn:uuid:"));
        assert_eq!(
            load_or_create_openai_installation_id(directory.path()).expect("reuse identity"),
            installation_id
        );

        let path = directory.path().join(OPENAI_INSTALLATION_ID_FILENAME);
        assert_eq!(
            fs::read_to_string(&path).expect("read persisted identity"),
            format!("{installation_id}\n")
        );
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(
                fs::metadata(path)
                    .expect("identity metadata")
                    .permissions()
                    .mode()
                    & 0o777,
                0o600
            );
        }
    }

    #[test]
    fn concurrent_openai_identity_initialization_converges_on_one_host_id() {
        let directory = tempfile::tempdir().expect("state directory");
        let workers = (0..8)
            .map(|_| {
                let state_dir = directory.path().to_owned();
                std::thread::spawn(move || load_or_create_openai_installation_id(&state_dir))
            })
            .collect::<Vec<_>>();
        let identities = workers
            .into_iter()
            .map(|worker| worker.join().expect("identity worker").expect("identity"))
            .collect::<Vec<_>>();
        assert!(identities.iter().all(|identity| identity == &identities[0]));
    }

    #[test]
    fn malformed_openai_installation_identity_is_not_replaced() {
        let directory = tempfile::tempdir().expect("state directory");
        crate::operations::paths::ensure_private_directory(directory.path())
            .expect("private state directory");
        let path = directory.path().join(OPENAI_INSTALLATION_ID_FILENAME);
        fs::write(&path, "not-a-uuid\n").expect("malformed identity");
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&path, fs::Permissions::from_mode(0o600))
                .expect("restrict malformed identity");
        }

        assert!(matches!(
            load_or_create_openai_installation_id(directory.path()),
            Err(LoginError::InstallationIdentity)
        ));
        assert_eq!(
            fs::read_to_string(path).expect("preserved malformed identity"),
            "not-a-uuid\n"
        );
    }

    #[cfg(unix)]
    #[test]
    fn openai_installation_identity_rejects_symlink_and_insecure_file() {
        use std::os::unix::fs::{PermissionsExt, symlink};

        let directory = tempfile::tempdir().expect("state directory");
        crate::operations::paths::ensure_private_directory(directory.path())
            .expect("private state directory");
        let target = directory.path().join("target");
        let identity_path = directory.path().join(OPENAI_INSTALLATION_ID_FILENAME);
        let valid_id = "urn:uuid:00000000-0000-4000-8000-000000000000";
        fs::write(&target, format!("{valid_id}\n")).expect("target");
        fs::set_permissions(&target, fs::Permissions::from_mode(0o600)).expect("private target");
        symlink(&target, &identity_path).expect("identity symlink");
        assert!(matches!(
            load_or_create_openai_installation_id(directory.path()),
            Err(LoginError::InstallationIdentity)
        ));

        fs::remove_file(&identity_path).expect("remove symlink");
        fs::write(&identity_path, format!("{valid_id}\n")).expect("identity file");
        fs::set_permissions(&identity_path, fs::Permissions::from_mode(0o644))
            .expect("make identity too broad");
        assert!(matches!(
            load_or_create_openai_installation_id(directory.path()),
            Err(LoginError::InstallationIdentity)
        ));
    }

    #[tokio::test]
    async fn provider_stub_completes_pkce_exchange_with_matching_state() {
        let listener = TcpListener::bind("127.0.0.1:0")
            .await
            .expect("local OAuth token endpoint");
        let base_url = format!("http://{}", listener.local_addr().expect("token address"));
        let server = tokio::spawn(async move {
            let (mut stream, _) = listener.accept().await.expect("token request");
            let request = read_http_request(&mut stream).await;
            let response_body = r#"{"access_token":"stub-access","refresh_token":"stub-refresh","token_type":"Bearer","expires_in":3600,"scope":"offline_access"}"#;
            let response = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{response_body}",
                response_body.len()
            );
            stream
                .write_all(response.as_bytes())
                .await
                .expect("token response");
            String::from_utf8(request).expect("HTTP request is UTF-8")
        });
        let profile = OAuthProviderConfig::custom("Local OAuth test", &base_url, "test-client")
            .authorization_endpoint(format!("{base_url}/authorize"))
            .token_endpoint(format!("{base_url}/token"))
            .callback_path("/auth/callback")
            .scopes(["offline_access"])
            .build()
            .expect("local OAuth provider profile");
        let client = OAuthClient::new(eggfetch_core::Client::new(), profile);
        let redirect_uri = "http://127.0.0.1:43123/auth/callback";
        let pending = client
            .begin_authorization(redirect_uri, None)
            .expect("begin local PKCE authorization");
        let authorization_uri: Uri = pending
            .authorization_url()
            .parse()
            .expect("authorization URL");
        let query = authorization_uri.query().expect("PKCE query");
        assert_eq!(
            query_value(query, "code_challenge_method").as_deref(),
            Some("S256")
        );
        assert!(query_value(query, "code_challenge").is_some());
        let state = query_value(query, "state").expect("generated state");
        let callback_url = format!("{redirect_uri}?code=fixture-code&state={state}");

        let credentials = client
            .complete_authorization(pending, &callback_url)
            .await
            .expect("complete local PKCE exchange");
        assert_eq!(credentials.access_token(), "stub-access");
        assert_eq!(credentials.refresh_token(), Some("stub-refresh"));
        let request = server.await.expect("token stub");
        assert!(request.starts_with("POST /token "));
        let body = request.split_once("\r\n\r\n").expect("form body").1;
        assert!(body.contains("grant_type=authorization_code"));
        assert!(body.contains("code=fixture-code"));
        assert!(body.contains("code_verifier="));
        assert!(!body.contains(&state));
    }

    #[tokio::test]
    async fn successful_cli_upload_posts_credentials_to_owner_bound_route() {
        let listener = TcpListener::bind("127.0.0.1:0")
            .await
            .expect("local EggPool upload stub");
        let address = listener.local_addr().expect("upload stub address");
        let server = tokio::spawn(async move {
            let (mut stream, _) = listener.accept().await.expect("credential upload");
            let request = read_http_request(&mut stream).await;
            stream
                .write_all(
                    b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
                )
                .await
                .expect("upload response");
            String::from_utf8(request).expect("upload request is UTF-8")
        });
        let mut config = Config::default();
        config.server.host = "127.0.0.1".to_owned();
        config.server.port = address.port();
        let credentials: OAuthCredentials = serde_json::from_value(serde_json::json!({
            "access_token": "fixture-access",
            "refresh_token": "fixture-refresh",
            "token_type": "Bearer",
            "scope": "openid offline_access",
            "client_id": "fixture-client",
            "issuer": "https://auth.x.ai",
            "expires_at": 1_900_000_000_i64,
            "obtained_at": 1_800_000_000_i64
        }))
        .expect("xAI credentials");
        upload_credentials(&config, "xai", "personal", "epk1_fixture", &credentials)
            .await
            .expect("upload completed");
        let request = server.await.expect("upload stub");
        assert!(request.starts_with("POST /api/user/oauth-credentials HTTP/1.1"));
        assert!(request.contains("Bearer epk1_fixture"));
        let body = request.split_once("\r\n\r\n").expect("JSON body").1;
        let payload: serde_json::Value = serde_json::from_str(body).expect("upload payload");
        assert_eq!(payload["provider_id"], "xai");
        assert_eq!(payload["account_name"], "personal");
        assert!(payload.get("user_id").is_none());
        assert!(payload.get("username").is_none());
    }

    #[test]
    fn loopback_api_url_requires_local_server_bind_configuration() {
        let mut config = Config::default();
        config.server.host = "0.0.0.0".to_owned();
        assert_eq!(local_api_uri(&config).unwrap().host(), Some("127.0.0.1"));
        config.server.host = "192.0.2.10".to_owned();
        assert!(local_api_uri(&config).is_err());
    }
}
