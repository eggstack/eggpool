//! Narrow, atomic configuration and provider mutations for the M9 CLI.
//!
//! This module intentionally edits only the small set of TOML surfaces owned
//! by O004.  It does not deserialize and reserialize the whole document: the
//! line editor keeps unrelated comments and sections intact while the typed
//! config parser remains the final validation authority.

use std::{
    collections::{BTreeMap, BTreeSet},
    env, fmt,
    fs::{self, File, OpenOptions},
    io::{self, IsTerminal, Read, Write},
    path::{Path, PathBuf},
    sync::{Mutex, OnceLock},
    time::Duration,
};

use toml::{Value, map::Map};

use crate::{
    Config, ConfigError,
    operations::{control::ControlClient, paths::RuntimePaths, process},
};

const MAX_CONFIG_BYTES: usize = 8 * 1024 * 1024;
const DEFAULT_CONFIG: &str = include_str!("../../../config.example.toml");
const BUNDLED_PROVIDERS: &str = include_str!("../../../src/eggpool/providers/_templates.toml");

static MUTATION_PATHS: OnceLock<Mutex<BTreeSet<PathBuf>>> = OnceLock::new();

struct MutationGuard {
    path: PathBuf,
}

impl Drop for MutationGuard {
    fn drop(&mut self) {
        if let Some(paths) = MUTATION_PATHS.get() {
            let mut paths = paths.lock().expect("mutation path lock");
            paths.remove(&self.path);
        }
    }
}

#[derive(Debug, thiserror::Error)]
pub enum MutationError {
    #[error("configuration file is too large")]
    TooLarge,
    #[error("configuration file could not be read")]
    Read(#[source] io::Error),
    #[error("configuration file could not be written")]
    Write(#[source] io::Error),
    #[error("configuration mutation is already in progress")]
    Busy,
    #[error("{0}")]
    Config(#[from] ConfigError),
    #[error("invalid configuration mutation: {0}")]
    Invalid(String),
    #[error("provider template could not be loaded")]
    Template(#[source] io::Error),
    #[error("provider template is invalid")]
    TemplateParse,
    #[error("local control could not apply the configuration")]
    Control,
    #[error("server restart failed")]
    Restart,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ApplyMode {
    LiveOrReport,
    RestartIfRunning,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ApplyOutcome {
    ServerNotRunning,
    RehashApplied,
    RehashNoop,
    RestartRequired(Vec<String>),
    ControlUnavailable,
    RehashFailed(String),
    Restarted,
}

#[derive(Debug, Clone)]
pub struct ProviderTemplate {
    pub id: String,
    pub display: String,
    pub url: String,
    pub status: String,
    pub category: String,
    pub recommended: bool,
    pub notes: String,
    data: Map<String, Value>,
}

#[derive(Clone, PartialEq, Eq)]
pub struct AccountMatch {
    pub provider_id: String,
    pub name: String,
    pub api_key_env: String,
    pub api_key: Option<String>,
}

impl fmt::Debug for AccountMatch {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("AccountMatch")
            .field("provider_id", &self.provider_id)
            .field("name", &self.name)
            .field("api_key_env", &self.api_key_env)
            .field("api_key", &self.api_key.as_deref().map(redact_key))
            .finish()
    }
}

fn mutation_path(path: &Path) -> PathBuf {
    let absolute = if path.is_absolute() {
        path.to_owned()
    } else {
        env::current_dir()
            .unwrap_or_else(|_| PathBuf::from("."))
            .join(path)
    };
    fs::canonicalize(&absolute).unwrap_or_else(|_| {
        let Some(parent) = absolute.parent() else {
            return absolute;
        };
        fs::canonicalize(parent)
            .map(|parent| parent.join(absolute.file_name().map(PathBuf::from).unwrap_or_default()))
            .unwrap_or(absolute)
    })
}

fn lock_mutation(path: &Path) -> Result<MutationGuard, MutationError> {
    let path = mutation_path(path);
    let paths = MUTATION_PATHS.get_or_init(|| Mutex::new(BTreeSet::new()));
    let mut active = paths.lock().expect("mutation path lock");
    if !active.insert(path.clone()) {
        return Err(MutationError::Busy);
    }
    Ok(MutationGuard { path })
}

fn read_bounded(path: &Path) -> Result<Vec<u8>, MutationError> {
    let mut file = File::open(path).map_err(MutationError::Read)?;
    let mut bytes = Vec::new();
    std::io::Read::by_ref(&mut file)
        .take((MAX_CONFIG_BYTES + 1) as u64)
        .read_to_end(&mut bytes)
        .map_err(MutationError::Read)?;
    if bytes.len() > MAX_CONFIG_BYTES {
        return Err(MutationError::TooLarge);
    }
    Ok(bytes)
}

fn validate_bytes(path: &Path, bytes: &[u8]) -> Result<(), MutationError> {
    let config = Config::from_toml_bytes(path, bytes)?;
    config.validate_account_credentials()?;
    Ok(())
}

fn existing_mode(path: &Path) -> Result<Option<fs::Permissions>, MutationError> {
    match fs::metadata(path) {
        Ok(metadata) => Ok(Some(metadata.permissions())),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(MutationError::Read(error)),
    }
}

fn atomic_replace(
    path: &Path,
    bytes: &[u8],
    mode: Option<fs::Permissions>,
) -> Result<(), MutationError> {
    let parent = path
        .parent()
        .filter(|value| !value.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."));
    fs::create_dir_all(parent).map_err(MutationError::Write)?;
    let name = path
        .file_name()
        .ok_or_else(|| MutationError::Invalid("configuration path has no file name".into()))?
        .to_string_lossy();
    let temporary = parent.join(format!(".{name}.tmp-{}", std::process::id()));
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut file = options.open(&temporary).map_err(MutationError::Write)?;
    let result = (|| {
        file.write_all(bytes).map_err(MutationError::Write)?;
        file.sync_all().map_err(MutationError::Write)?;
        if let Some(permissions) = mode {
            fs::set_permissions(&temporary, permissions).map_err(MutationError::Write)?;
        }
        fs::rename(&temporary, path).map_err(MutationError::Write)?;
        if let Ok(directory) = File::open(parent) {
            let _ = directory.sync_all();
        }
        Ok::<(), MutationError>(())
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temporary);
    }
    result
}

fn mutate_text<F>(path: &Path, create: bool, edit: F) -> Result<bool, MutationError>
where
    F: FnOnce(&str) -> Result<(String, bool), MutationError>,
{
    let _guard = lock_mutation(path)?;
    let original = if path.exists() {
        read_bounded(path)?
    } else if create {
        Vec::new()
    } else {
        return Err(MutationError::Read(io::Error::new(
            io::ErrorKind::NotFound,
            "configuration file not found",
        )));
    };
    let original_text = String::from_utf8(original.clone())
        .map_err(|_| MutationError::Invalid("configuration is not valid UTF-8".into()))?;
    let (updated, changed) = edit(&original_text)?;
    if !changed {
        return Ok(false);
    }
    let bytes = updated.as_bytes();
    validate_bytes(path, bytes)?;
    atomic_replace(path, bytes, existing_mode(path)?)?;
    Ok(true)
}

fn line_header(line: &str) -> Option<&str> {
    let trimmed = line.trim();
    if let Some(value) = trimmed
        .strip_prefix("[[")
        .and_then(|value| value.strip_suffix("]]"))
    {
        return Some(value);
    }
    trimmed.strip_prefix('[')?.strip_suffix(']')
}

fn assignment_key(line: &str) -> Option<&str> {
    let trimmed = line.trim_start();
    let (key, _) = trimmed.split_once('=')?;
    let key = key.trim();
    (!key.is_empty()
        && key
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-'))
    .then_some(key)
}

fn render_string(value: &str) -> String {
    let mut rendered = String::with_capacity(value.len() + 2);
    rendered.push('"');
    for character in value.chars() {
        match character {
            '\\' => rendered.push_str("\\\\"),
            '"' => rendered.push_str("\\\""),
            '\n' => rendered.push_str("\\n"),
            '\r' => rendered.push_str("\\r"),
            '\t' => rendered.push_str("\\t"),
            '\0' => rendered.push_str("\\u0000"),
            character => rendered.push(character),
        }
    }
    rendered.push('"');
    rendered
}

fn replace_or_insert_section_value(
    text: &str,
    section: &str,
    key: &str,
    value: &str,
    insert_missing_key: bool,
    append_missing_section: bool,
) -> Result<(String, bool), MutationError> {
    let mut lines: Vec<String> = text.lines().map(str::to_owned).collect();
    let mut section_start = None;
    let mut section_end = lines.len();
    for (index, line) in lines.iter().enumerate() {
        if line_header(line) == Some(section) {
            section_start = Some(index);
            section_end = lines[index + 1..]
                .iter()
                .position(|candidate| line_header(candidate).is_some())
                .map_or(lines.len(), |offset| index + 1 + offset);
            break;
        }
    }
    if let Some(start) = section_start {
        for line in &mut lines[start + 1..section_end] {
            if assignment_key(line) == Some(key) {
                let indentation = line.len() - line.trim_start().len();
                *line = format!("{}{} = {value}", &line[..indentation], key);
                return Ok((format!("{}\n", lines.join("\n")), true));
            }
        }
        if insert_missing_key {
            lines.insert(section_end, format!("{key} = {value}"));
            return Ok((format!("{}\n", lines.join("\n")), true));
        }
        return Err(MutationError::Invalid(format!(
            "key {key:?} not found in [{section}] section"
        )));
    }
    if !append_missing_section {
        return Err(MutationError::Invalid(format!(
            "section [{section}] not found"
        )));
    }
    if !lines.is_empty() && !lines.last().is_some_and(String::is_empty) {
        lines.push(String::new());
    }
    lines.push(format!("[{section}]"));
    lines.push(format!("{key} = {value}"));
    Ok((format!("{}\n", lines.join("\n")), true))
}

fn valid_identifier(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 96
        && value.chars().all(|character| {
            character.is_ascii_alphanumeric() || character == '-' || character == '_'
        })
}

pub fn set_server_value(path: &Path, key: &str, value: &str) -> Result<bool, MutationError> {
    let rendered = match key {
        "host" => render_string(value),
        "port" => {
            let port = value
                .parse::<u16>()
                .map_err(|_| MutationError::Invalid("port must be an integer".into()))?;
            if port == 0 {
                return Err(MutationError::Invalid(
                    "port must be between 1 and 65535".into(),
                ));
            }
            port.to_string()
        }
        _ => {
            return Err(MutationError::Invalid(format!(
                "unsupported server key {key:?}"
            )));
        }
    };
    mutate_text(path, false, |text| {
        replace_or_insert_section_value(text, "server", key, &rendered, false, false)
    })
}

pub fn set_dashboard_public(path: &Path, public: Option<bool>) -> Result<bool, MutationError> {
    let current = read_dashboard_public(path)?;
    let next = public.unwrap_or(!current);
    mutate_text(path, false, |text| {
        replace_or_insert_section_value(text, "dashboard", "public", &next.to_string(), true, true)
    })
}

pub fn read_dashboard_public(path: &Path) -> Result<bool, MutationError> {
    let bytes = read_bounded(path)?;
    let value: Value = std::str::from_utf8(&bytes)
        .map_err(|_| MutationError::Invalid("configuration is not valid UTF-8".into()))?
        .parse()
        .map_err(|_| MutationError::Invalid("configuration TOML is malformed".into()))?;
    Ok(value
        .get("dashboard")
        .and_then(Value::as_table)
        .and_then(|table| table.get("public"))
        .and_then(Value::as_bool)
        .unwrap_or(true))
}

pub fn init_config(path: &Path, force: bool) -> Result<bool, MutationError> {
    let _guard = lock_mutation(path)?;
    if path.exists() && !force {
        return Err(MutationError::Invalid(format!(
            "{} already exists; use --force to overwrite",
            path.display()
        )));
    }
    let bytes = DEFAULT_CONFIG.as_bytes();
    validate_bytes(path, bytes)?;
    atomic_replace(path, bytes, existing_mode(path)?)?;
    Ok(true)
}

pub fn read_server_key(path: &Path) -> Result<Option<String>, MutationError> {
    let bytes = read_bounded(path)?;
    let value: Value = std::str::from_utf8(&bytes)
        .map_err(|_| MutationError::Invalid("configuration is not valid UTF-8".into()))?
        .parse()
        .map_err(|_| MutationError::Invalid("configuration TOML is malformed".into()))?;
    let server = value
        .get("server")
        .and_then(Value::as_table)
        .ok_or_else(|| MutationError::Invalid("[server] section is missing".into()))?;
    if let Some(key) = server.get("api_key").and_then(Value::as_str) {
        return Ok((!key.is_empty()).then_some(key.to_owned()));
    }
    let Some(env_name) = server.get("api_key_env").and_then(Value::as_str) else {
        return Ok(None);
    };
    Ok(env::var(env_name)
        .ok()
        .filter(|value| !value.trim().is_empty()))
}

/// Resolve the server key for an integration snippet.
///
/// This is deliberately kept beside the O004 key mutation service so an
/// integration renderer cannot invent a second persistence or environment
/// precedence rule.  An explicitly configured environment-owned key is
/// never replaced with an inline generated value.
pub fn resolve_server_key(path: &Path) -> Result<(String, bool), MutationError> {
    let bytes = read_bounded(path)?;
    let value: Value = std::str::from_utf8(&bytes)
        .map_err(|_| MutationError::Invalid("configuration is not valid UTF-8".into()))?
        .parse()
        .map_err(|_| MutationError::Invalid("configuration TOML is malformed".into()))?;
    let server = value
        .get("server")
        .and_then(Value::as_table)
        .ok_or_else(|| MutationError::Invalid("[server] section is missing".into()))?;

    if let Some(key) = server.get("api_key").and_then(Value::as_str) {
        if !key.is_empty() {
            return Ok((key.to_owned(), false));
        }
    }
    if let Some(env_name) = server.get("api_key_env").and_then(Value::as_str) {
        if !env_name.trim().is_empty() {
            let key = env::var(env_name).map_err(|_| {
                MutationError::Invalid(
                    "[server].api_key_env is configured, but the referenced environment variable is not available to this process".into(),
                )
            })?;
            if key.trim().is_empty() {
                return Err(MutationError::Invalid(
                    "[server].api_key_env is configured, but the referenced environment variable is empty".into(),
                ));
            }
            return Ok((key, false));
        }
    }

    let key = generate_key()?;
    if !write_server_key(path, &key)? {
        return Err(MutationError::Invalid(
            "cannot persist a generated server API key".into(),
        ));
    }
    Ok((key, true))
}

/// Enable the compatibility transcoder when an enabled Anthropic-only
/// provider is exposed to OpenAI-compatible integrations.
pub fn set_transcoder_enabled(path: &Path, enabled: bool) -> Result<bool, MutationError> {
    mutate_text(path, false, |text| {
        replace_or_insert_section_value(
            text,
            "transcoder",
            "enabled",
            &enabled.to_string(),
            true,
            true,
        )
    })
}

pub fn redact_key(key: &str) -> String {
    let characters: Vec<char> = key.chars().collect();
    if characters.len() <= 8 {
        return "***".into();
    }
    let prefix: String = characters.iter().take(4).collect();
    let suffix: String = characters
        .iter()
        .rev()
        .take(4)
        .collect::<Vec<_>>()
        .into_iter()
        .rev()
        .collect();
    format!("{prefix}...{suffix}")
}

pub fn generate_key() -> Result<String, MutationError> {
    let mut bytes = [0_u8; 32];
    getrandom::fill(&mut bytes)
        .map_err(|_| MutationError::Read(io::Error::other("cryptographic entropy unavailable")))?;
    let mut output = String::with_capacity(64);
    for byte in bytes {
        use std::fmt::Write as _;
        write!(output, "{byte:02x}").expect("String formatting");
    }
    Ok(output)
}

pub fn write_server_key(path: &Path, key: &str) -> Result<bool, MutationError> {
    if key.is_empty() || key.chars().any(|c| matches!(c, '\r' | '\n' | '\0')) {
        return Err(MutationError::Invalid("generated key is invalid".into()));
    }
    let bytes = read_bounded(path)?;
    let value: Value = std::str::from_utf8(&bytes)
        .map_err(|_| MutationError::Invalid("configuration is not valid UTF-8".into()))?
        .parse()
        .map_err(|_| MutationError::Invalid("configuration TOML is malformed".into()))?;
    let env_owned = value
        .get("server")
        .and_then(Value::as_table)
        .and_then(|table| table.get("api_key_env"))
        .and_then(Value::as_str)
        .is_some_and(|name| !name.trim().is_empty());
    if env_owned {
        return Ok(false);
    }
    mutate_text(path, false, |text| {
        replace_or_insert_section_value(text, "server", "api_key", &render_string(key), true, true)
    })
}

pub fn load_provider_templates(
    path: Option<&Path>,
) -> Result<BTreeMap<String, ProviderTemplate>, MutationError> {
    let text = match path {
        Some(path) => fs::read_to_string(path).map_err(MutationError::Template)?,
        None => BUNDLED_PROVIDERS.to_owned(),
    };
    let root: Value = text.parse().map_err(|_| MutationError::TemplateParse)?;
    let providers = root
        .get("providers")
        .and_then(Value::as_table)
        .ok_or(MutationError::TemplateParse)?;
    let mut result = BTreeMap::new();
    for (id, raw) in providers {
        let Some(table) = raw.as_table() else {
            continue;
        };
        let mut data = table.clone();
        let display = data
            .remove("display_name")
            .and_then(|value| value.as_str().map(str::to_owned))
            .unwrap_or_else(|| id.clone());
        let url = data
            .get("base_url")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_owned();
        let status = data
            .remove("status")
            .and_then(|value| value.as_str().map(str::to_owned))
            .unwrap_or_else(|| "unverified".into());
        let category = data
            .remove("category")
            .and_then(|value| value.as_str().map(str::to_owned))
            .unwrap_or_else(|| "direct".into());
        let recommended = data
            .remove("recommended")
            .and_then(|value| value.as_bool())
            .unwrap_or(false);
        let notes = data
            .remove("notes")
            .and_then(|value| value.as_str().map(str::to_owned))
            .unwrap_or_default();
        data.remove("region");
        data.remove("api_key_env");
        data.insert("id".into(), Value::String(id.clone()));
        result.insert(
            id.clone(),
            ProviderTemplate {
                id: id.clone(),
                display,
                url,
                status,
                category,
                recommended,
                notes,
                data,
            },
        );
    }
    if !result.contains_key("opencode-go")
        && path.is_some()
        && let Some(default) = load_provider_templates(None)?.remove("opencode-go")
    {
        result.insert("opencode-go".into(), default);
    }
    if !result.contains_key("opencode-go") {
        return Err(MutationError::TemplateParse);
    }
    Ok(result)
}

fn provider_auth_mode(template: &ProviderTemplate) -> &str {
    template
        .data
        .get("auth")
        .and_then(Value::as_table)
        .and_then(|table| table.get("mode"))
        .and_then(Value::as_str)
        .unwrap_or("bearer")
}

fn read_secret_line(prompt: &str) -> Result<Option<String>, MutationError> {
    print!("{prompt}");
    io::stdout().flush().map_err(MutationError::Write)?;
    if !io::stdin().is_terminal() {
        let mut value = String::new();
        return Ok((io::stdin()
            .read_line(&mut value)
            .map_err(MutationError::Read)?
            > 0)
        .then(|| value.trim().to_owned()));
    }
    #[cfg(unix)]
    {
        use nix::sys::termios::{LocalFlags, SetArg, tcgetattr, tcsetattr};
        let stdin = io::stdin();
        let original = tcgetattr(&stdin)
            .map_err(|_| MutationError::Read(io::Error::other("terminal mode unavailable")))?;
        let mut hidden = original.clone();
        hidden.local_flags.remove(LocalFlags::ECHO);
        tcsetattr(&stdin, SetArg::TCSANOW, &hidden)
            .map_err(|_| MutationError::Read(io::Error::other("terminal mode unavailable")))?;
        let mut value = String::new();
        let read_result = stdin.read_line(&mut value).map_err(MutationError::Read);
        let restore_result = tcsetattr(&stdin, SetArg::TCSANOW, &original);
        println!();
        restore_result
            .map_err(|_| MutationError::Read(io::Error::other("terminal mode restore failed")))?;
        Ok((read_result? > 0).then(|| value.trim().to_owned()))
    }
    #[cfg(not(unix))]
    {
        let mut value = String::new();
        Ok((io::stdin()
            .read_line(&mut value)
            .map_err(MutationError::Read)?
            > 0)
        .then(|| value.trim().to_owned()))
    }
}

fn account_names(text: &str) -> Vec<String> {
    let mut names = Vec::new();
    let mut in_account = false;
    for line in text.lines() {
        let header = line_header(line).unwrap_or_default();
        if header.starts_with("providers.") && header.ends_with(".accounts") {
            in_account = true;
        } else if line_header(line).is_some() {
            in_account = false;
        }
        if in_account && assignment_key(line) == Some("name") {
            if let Some((_, raw)) = line.split_once('=') {
                let value = raw.trim().trim_matches(['"', '\'']);
                names.push(value.to_owned());
            }
        }
    }
    names
}

fn unique_account_name(provider_id: &str, names: &[String]) -> String {
    let mut counter = names.len() + 1;
    loop {
        let candidate = format!("{provider_id}-{counter:04}");
        if !names.iter().any(|name| name == &candidate) {
            return candidate;
        }
        counter += 1;
    }
}

fn provider_block(
    template: &ProviderTemplate,
    account_name: &str,
    api_key: Option<&str>,
) -> Result<String, MutationError> {
    let mut provider = template.data.clone();
    provider.insert("routing_priority".into(), Value::Integer(0));
    let mut account = Map::new();
    account.insert("name".into(), Value::String(account_name.into()));
    if let Some(api_key) = api_key {
        account.insert("api_key".into(), Value::String(api_key.into()));
    }
    provider.insert("accounts".into(), Value::Array(vec![Value::Table(account)]));
    let mut providers = Map::new();
    providers.insert(template.id.clone(), Value::Table(provider));
    let mut root = Map::new();
    root.insert("providers".into(), Value::Table(providers));
    let rendered =
        toml::to_string_pretty(&Value::Table(root)).map_err(|_| MutationError::TemplateParse)?;
    Ok(rendered.trim_end().to_owned())
}

fn provider_exists(text: &str, provider_id: &str) -> bool {
    text.lines()
        .any(|line| line_header(line) == Some(&format!("providers.{provider_id}")))
}

fn append_account(
    text: &str,
    provider_id: &str,
    account_name: &str,
    api_key: Option<&str>,
) -> Result<String, MutationError> {
    let lines: Vec<String> = text.lines().map(str::to_owned).collect();
    let header = format!("providers.{provider_id}.accounts");
    let mut start = None;
    let mut end = lines.len();
    for (index, line) in lines.iter().enumerate() {
        if line_header(line) == Some(&header) {
            start = Some(index);
            end = lines[index + 1..]
                .iter()
                .position(|candidate| line_header(candidate).is_some())
                .map_or(lines.len(), |offset| index + 1 + offset);
            break;
        }
    }
    let mut insertion = vec![
        String::new(),
        format!("[[providers.{provider_id}.accounts]]"),
        format!("name = {}", render_string(account_name)),
    ];
    if let Some(api_key) = api_key {
        insertion.push(format!("api_key = {}", render_string(api_key)));
    }
    let mut updated = lines;
    let index = end;
    if start.is_none() {
        let provider_header = format!("providers.{provider_id}");
        let provider_end = updated
            .iter()
            .position(|line| line_header(line).is_some_and(|header| header == provider_header))
            .map(|value| value + 1)
            .unwrap_or(updated.len());
        updated.splice(provider_end..provider_end, insertion);
    } else {
        updated.splice(index..index, insertion);
    }
    Ok(format!("{}\n", updated.join("\n")))
}

pub fn connect(
    path: &Path,
    providers_path: Option<&Path>,
) -> Result<Option<String>, MutationError> {
    let templates = load_provider_templates(providers_path)?;
    let mut ids: Vec<&String> = templates.keys().collect();
    ids.sort();
    println!("Available providers:");
    for (index, id) in ids.iter().enumerate() {
        let template = &templates[*id];
        let marker = if template.recommended { "*" } else { " " };
        println!(
            "  {marker} {}: {} ({}) [{}]{}",
            template.id,
            template.display,
            template.url,
            template.status,
            if template.notes.is_empty() {
                String::new()
            } else {
                format!(" — {}", template.notes)
            }
        );
        println!("    selection {index}");
    }
    print!("Select provider (id or number, Enter cancels): ");
    io::stdout().flush().map_err(MutationError::Write)?;
    let mut selection = String::new();
    if io::stdin()
        .read_line(&mut selection)
        .map_err(MutationError::Read)?
        == 0
    {
        return Ok(None);
    }
    let selection = selection.trim();
    if selection.is_empty()
        || matches!(
            selection.to_ascii_lowercase().as_str(),
            "q" | "quit" | "exit"
        )
    {
        return Ok(None);
    }
    let selected_provider_id = selection
        .parse::<usize>()
        .ok()
        .and_then(|index| ids.get(index).copied())
        .map_or(selection, |id| id.as_str());
    let Some(selected_template) = templates.get(selected_provider_id) else {
        return Err(MutationError::Invalid("unknown provider template".into()));
    };
    let mut template = selected_template.clone();
    let mut provider_id = selected_provider_id.to_owned();
    if template.category == "local" {
        let custom_id =
            read_line_prompt(&format!("Provider id [{}]: ", template.id))?.unwrap_or_default();
        let custom_url =
            read_line_prompt(&format!("Base URL [{}]: ", template.url))?.unwrap_or_default();
        if !custom_id.trim().is_empty() {
            provider_id = custom_id.trim().to_owned();
        }
        if !custom_url.trim().is_empty() {
            template.url = custom_url.trim().to_owned();
            template
                .data
                .insert("base_url".into(), Value::String(template.url.clone()));
        }
        template.id = provider_id.clone();
        template
            .data
            .insert("id".into(), Value::String(provider_id.clone()));
    }
    if !valid_identifier(&provider_id) {
        return Err(MutationError::Invalid(
            "provider id contains unsafe characters".into(),
        ));
    }
    let api_key = if provider_auth_mode(&template) == "none" {
        None
    } else {
        let Some(key) = read_secret_line(&format!(
            "Enter API key for {} (input is not displayed): ",
            template.display
        ))?
        else {
            return Ok(None);
        };
        if key.is_empty() {
            println!("No API key provided. Aborted.");
            return Ok(None);
        }
        Some(key)
    };
    let guard = lock_mutation(path)?;
    let original = if path.exists() {
        read_bounded(path)?
    } else {
        Vec::new()
    };
    let original_text = String::from_utf8(original)
        .map_err(|_| MutationError::Invalid("configuration is not valid UTF-8".into()))?;
    let names = account_names(&original_text);
    let account_name = unique_account_name(&provider_id, &names);
    let duplicate = api_key.as_ref().is_some_and(|key| {
        Config::from_toml_bytes(path, original_text.as_bytes())
            .ok()
            .is_some_and(|config| {
                config
                    .providers
                    .values()
                    .flat_map(|provider| provider.accounts.iter())
                    .any(|account| account.api_key.as_deref() == Some(key.as_str()))
            })
    });
    if duplicate {
        println!("An account with this API key is already configured. Aborted.");
        return Ok(None);
    }
    let updated = if provider_exists(&original_text, &provider_id) {
        append_account(
            &original_text,
            &provider_id,
            &account_name,
            api_key.as_deref(),
        )?
    } else {
        let block = provider_block(&template, &account_name, api_key.as_deref())?;
        let mut lines: Vec<String> = original_text.lines().map(str::to_owned).collect();
        let index = lines
            .iter()
            .position(|line| {
                line_header(line).is_some_and(|header| header.starts_with("providers."))
            })
            .unwrap_or(lines.len());
        let block_lines: Vec<String> = block.lines().map(str::to_owned).collect();
        lines.splice(
            index..index,
            block_lines
                .into_iter()
                .chain(std::iter::once(String::new())),
        );
        format!("{}\n", lines.join("\n"))
    };
    validate_bytes(path, updated.as_bytes())?;
    atomic_replace(path, updated.as_bytes(), existing_mode(path)?)?;
    drop(guard);
    println!("Added {account_name} to {provider_id}.");
    Ok(Some(provider_id))
}

pub fn list_accounts(path: &Path) -> Result<Vec<AccountMatch>, MutationError> {
    let config = Config::from_toml(path)?;
    Ok(config
        .providers
        .iter()
        .flat_map(|(provider_id, provider)| {
            provider.accounts.iter().map(|account| AccountMatch {
                provider_id: provider_id.clone(),
                name: account.name.clone(),
                api_key_env: account.api_key_env.clone(),
                api_key: account
                    .api_key
                    .clone()
                    .or_else(|| env::var(&account.api_key_env).ok()),
            })
        })
        .collect())
}

fn normalize_identifier(value: &str) -> String {
    value
        .chars()
        .filter(|c| c.is_ascii_alphanumeric())
        .flat_map(char::to_lowercase)
        .collect()
}

pub fn matching_accounts(
    path: &Path,
    target: Option<&str>,
) -> Result<Vec<AccountMatch>, MutationError> {
    let accounts = list_accounts(path)?;
    let Some(target) = target else {
        return Ok(accounts);
    };
    let normalized = normalize_identifier(target);
    Ok(accounts
        .into_iter()
        .filter(|account| {
            account.api_key.as_deref() == Some(target)
                || account.api_key_env == target
                || account.name == target
                || account.provider_id == target
                || normalize_identifier(&account.provider_id) == normalized
        })
        .collect())
}

fn remove_account_block(text: &str, account: &AccountMatch) -> Result<String, MutationError> {
    let header = format!("providers.{}.accounts", account.provider_id);
    let lines: Vec<String> = text.lines().map(str::to_owned).collect();
    let mut output = Vec::new();
    let mut index = 0;
    let account_header = format!("providers.{}.accounts", account.provider_id);
    while index < lines.len() {
        if line_header(&lines[index]) != Some(&account_header) {
            output.push(lines[index].clone());
            index += 1;
            continue;
        }
        let start = index;
        let mut end = index + 1;
        while end < lines.len() && line_header(&lines[end]).is_none() {
            end += 1;
        }
        let block = &lines[start..end];
        let name_matches = block.iter().any(|line| {
            assignment_key(line) == Some("name")
                && line.split_once('=').is_some_and(|(_, value)| {
                    value.trim().trim_matches(['"', '\'']) == account.name
                })
        });
        if name_matches {
            index = end;
            while index < lines.len() && lines[index].is_empty() {
                index += 1;
            }
        } else {
            output.extend_from_slice(block);
            index = end;
        }
    }
    if output.iter().any(|line| line_header(line) == Some(&header)) {
        return Ok(format!("{}\n", output.join("\n")));
    }
    remove_provider_block(&output.join("\n"), &account.provider_id)
}

fn remove_provider_block(text: &str, provider_id: &str) -> Result<String, MutationError> {
    let header = format!("providers.{provider_id}");
    let lines: Vec<String> = text.lines().map(str::to_owned).collect();
    let Some(start) = lines
        .iter()
        .position(|line| line_header(line) == Some(&header))
    else {
        return Ok(format!("{}\n", lines.join("\n")));
    };
    let child_prefix = format!("providers.{provider_id}.");
    let mut end = start + 1;
    while end < lines.len() {
        match line_header(&lines[end]) {
            Some(value) if value.starts_with(&child_prefix) => end += 1,
            Some(_) => break,
            None => end += 1,
        }
    }
    let mut output = lines[..start].to_vec();
    while output.last().is_some_and(String::is_empty) {
        output.pop();
    }
    output.extend_from_slice(&lines[end..]);
    Ok(format!("{}\n", output.join("\n")))
}

pub fn logout(path: &Path, target: Option<&str>) -> Result<Option<AccountMatch>, MutationError> {
    let mut matches = matching_accounts(path, target)?;
    if matches.is_empty() {
        return Ok(None);
    }
    let account = if matches.len() == 1 {
        matches.remove(0)
    } else {
        println!("Select provider account to remove:");
        for (index, item) in matches.iter().enumerate() {
            let key = item
                .api_key
                .as_deref()
                .map(redact_key)
                .unwrap_or_else(|| format!("env:{}", item.api_key_env));
            println!("  {index}: {}/{} {key}", item.provider_id, item.name);
        }
        print!("Selection (Enter cancels): ");
        io::stdout().flush().map_err(MutationError::Write)?;
        let mut selection = String::new();
        io::stdin()
            .read_line(&mut selection)
            .map_err(MutationError::Read)?;
        let Ok(index) = selection.trim().parse::<usize>() else {
            return Ok(None);
        };
        let Some(account) = matches.get(index).cloned() else {
            return Ok(None);
        };
        account
    };
    let guard = lock_mutation(path)?;
    let original = read_bounded(path)?;
    let text = String::from_utf8(original)
        .map_err(|_| MutationError::Invalid("configuration is not valid UTF-8".into()))?;
    let updated = remove_account_block(&text, &account)?;
    validate_bytes(path, updated.as_bytes())?;
    atomic_replace(path, updated.as_bytes(), existing_mode(path)?)?;
    drop(guard);
    Ok(Some(account))
}

pub async fn apply_after_mutation(
    path: &Path,
    mode: ApplyMode,
) -> Result<ApplyOutcome, MutationError> {
    if mode == ApplyMode::RestartIfRunning {
        return match crate::runtime::restart_for_mutation(path).await {
            Ok(true) => Ok(ApplyOutcome::Restarted),
            Ok(false) => Ok(ApplyOutcome::ServerNotRunning),
            Err(_) => Err(MutationError::Restart),
        };
    }
    let config = Config::from_toml(path)?;
    let digest = crate::config::content_digest(path)?;
    let client = ControlClient::new(RuntimePaths::resolve().control_socket);
    match client.reload(Some(digest)).await {
        Ok(response) if response.ok => {
            if response.stage == "noop" {
                Ok(ApplyOutcome::RehashNoop)
            } else {
                Ok(ApplyOutcome::RehashApplied)
            }
        }
        Ok(response) if !response.restart_required.is_empty() => {
            Ok(ApplyOutcome::RestartRequired(response.restart_required))
        }
        Ok(response) => Ok(ApplyOutcome::RehashFailed(response.message)),
        Err(_) => {
            let paths = RuntimePaths::resolve();
            let healthy = process::read_pid(&paths.pid_file)
                .ok()
                .flatten()
                .is_some_and(process::process_exists)
                && process::probe_health(&config.server.host, config.server.port).await
                    == process::HealthProbe::Healthy;
            if healthy {
                Ok(ApplyOutcome::ControlUnavailable)
            } else {
                Ok(ApplyOutcome::ServerNotRunning)
            }
        }
    }
}

pub fn read_line_prompt(prompt: &str) -> Result<Option<String>, MutationError> {
    print!("{prompt}");
    io::stdout().flush().map_err(MutationError::Write)?;
    if !io::stdin().is_terminal() {
        let mut value = String::new();
        return Ok((io::stdin()
            .read_line(&mut value)
            .map_err(MutationError::Read)?
            > 0)
        .then(|| value.trim().to_owned()));
    }
    let mut value = String::new();
    let read = io::stdin()
        .read_line(&mut value)
        .map_err(MutationError::Read)?;
    Ok((read > 0).then(|| value.trim().to_owned()))
}

#[allow(dead_code)]
fn _duration_for_control() -> Duration {
    Duration::from_secs(5)
}
