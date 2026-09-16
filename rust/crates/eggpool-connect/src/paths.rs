//! Portable OS and client path resolution for the receiving machine.
//!
//! Profiles never supply receiving-machine paths. The desktop decides local
//! paths from its client adapter using the rules below. Every resolver has a
//! pure `*_for` variant so Linux/macOS/Windows behavior is deterministically
//! testable without touching the real user home.

use std::env;
use std::path::{Path, PathBuf};

/// Receiving-machine operating system class.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OsKind {
    Linux,
    Macos,
    Windows,
    Other,
}

impl OsKind {
    #[must_use]
    pub const fn name(self) -> &'static str {
        match self {
            Self::Linux => "linux",
            Self::Macos => "macos",
            Self::Windows => "windows",
            Self::Other => "other",
        }
    }
}

/// Detect the receiving-machine OS class.
#[must_use]
pub fn detect_os() -> OsKind {
    detect_os_for(env::consts::OS)
}

/// Pure OS classifier for tests.
#[must_use]
pub fn detect_os_for(os: &str) -> OsKind {
    match os {
        "linux" => OsKind::Linux,
        "macos" => OsKind::Macos,
        "windows" => OsKind::Windows,
        _ => OsKind::Other,
    }
}

/// Resolve the helper state root (`<user-state>/eggpool-connect/`).
///
/// Precedence: explicit `EGGPOOL_CONNECT_STATE_DIR` override (tests and
/// advanced operators), then native per-OS conventions:
/// Linux honors `XDG_STATE_HOME` with `~/.local/state` fallback; macOS uses
/// `~/Library/Application Support`; Windows uses `%LOCALAPPDATA%` with
/// `%USERPROFILE%` fallback. The exact path is documented in
/// `docs/agent-configuration.md`.
#[must_use]
pub fn state_root() -> PathBuf {
    if let Some(dir) = env::var_os("EGGPOOL_CONNECT_STATE_DIR")
        && !dir.is_empty()
    {
        return PathBuf::from(dir);
    }
    let os = detect_os();
    let home = home_dir();
    let xdg_state = env::var_os("XDG_STATE_HOME").map(PathBuf::from);
    let local_app_data = env::var_os("LOCALAPPDATA").map(PathBuf::from);
    state_root_for(
        os,
        home.as_deref(),
        xdg_state.as_deref(),
        local_app_data.as_deref(),
    )
}

/// Pure state-root resolver for deterministic cross-platform tests.
#[must_use]
pub fn state_root_for(
    os: OsKind,
    home: Option<&Path>,
    xdg_state: Option<&Path>,
    local_app_data: Option<&Path>,
) -> PathBuf {
    let fallback_home = PathBuf::from(".");
    let home = home.unwrap_or(&fallback_home);
    match os {
        OsKind::Windows => {
            if let Some(dir) = local_app_data {
                return dir.join("eggpool-connect");
            }
            home.join("AppData").join("Local").join("eggpool-connect")
        }
        OsKind::Macos => home
            .join("Library")
            .join("Application Support")
            .join("eggpool-connect"),
        OsKind::Linux | OsKind::Other => {
            if let Some(dir) = xdg_state {
                return dir.join("eggpool-connect");
            }
            home.join(".local").join("state").join("eggpool-connect")
        }
    }
}

fn home_dir() -> Option<PathBuf> {
    // Windows uses USERPROFILE; POSIX uses HOME. Try both so tests and
    // cross-compiled helpers behave predictably.
    env::var_os("USERPROFILE")
        .or_else(|| env::var_os("HOME"))
        .map(PathBuf::from)
}

/// Backups root: `<state>/backups/`.
#[must_use]
pub fn backups_root_for(state_root: &Path) -> PathBuf {
    state_root.join("backups")
}

/// Generated Codex catalog path owned by the helper.
///
/// The path is always an absolute receiving-machine path under the helper
/// state root, never a server path and never a profile-supplied path.
#[must_use]
pub fn helper_codex_catalog_path_for(state_root: &Path) -> PathBuf {
    state_root
        .join("artifacts")
        .join("codex")
        .join("eggpool-codex-models.json")
}

/// Resolve the Codex client config path, respecting `CODEX_HOME`.
#[must_use]
pub fn codex_config_path() -> PathBuf {
    if let Some(home) = env::var_os("CODEX_HOME")
        && !home.is_empty()
    {
        return PathBuf::from(home).join("config.toml");
    }
    codex_config_path_for(home_dir().as_deref())
}

/// Pure Codex path resolver for tests.
#[must_use]
pub fn codex_config_path_for(home: Option<&Path>) -> PathBuf {
    let fallback = PathBuf::from(".");
    let home = home.unwrap_or(&fallback);
    home.join(".codex").join("config.toml")
}

/// Resolve the OpenCode global config path.
///
/// Respects `OPENCODE_CONFIG` first, then platform conventions: Windows uses
/// `%APPDATA%` (never Unix-only `$HOME/.config` assumptions); other systems
/// honor `XDG_CONFIG_HOME` with `~/.config` fallback.
#[must_use]
pub fn opencode_config_path() -> PathBuf {
    if let Some(custom) = env::var_os("OPENCODE_CONFIG")
        && !custom.is_empty()
    {
        return PathBuf::from(custom);
    }
    let os = detect_os();
    let home = home_dir();
    let xdg_config = env::var_os("XDG_CONFIG_HOME").map(PathBuf::from);
    let appdata = env::var_os("APPDATA").map(PathBuf::from);
    opencode_config_path_for(
        os,
        home.as_deref(),
        xdg_config.as_deref(),
        appdata.as_deref(),
    )
}

/// Pure OpenCode path resolver for deterministic Windows/Unix tests.
#[must_use]
pub fn opencode_config_path_for(
    os: OsKind,
    home: Option<&Path>,
    xdg_config: Option<&Path>,
    appdata: Option<&Path>,
) -> PathBuf {
    let fallback = PathBuf::from(".");
    let home = home.unwrap_or(&fallback);
    match os {
        OsKind::Windows => {
            if let Some(dir) = appdata {
                return dir.join("opencode").join("opencode.json");
            }
            home.join(".config").join("opencode").join("opencode.json")
        }
        OsKind::Linux | OsKind::Macos | OsKind::Other => {
            if let Some(dir) = xdg_config {
                return dir.join("opencode").join("opencode.json");
            }
            home.join(".config").join("opencode").join("opencode.json")
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn os_classifier_covers_supported_hosts() {
        assert_eq!(detect_os_for("linux"), OsKind::Linux);
        assert_eq!(detect_os_for("macos"), OsKind::Macos);
        assert_eq!(detect_os_for("windows"), OsKind::Windows);
        assert_eq!(detect_os_for("freebsd"), OsKind::Other);
    }

    #[test]
    fn state_root_follows_native_conventions() {
        let home = Path::new("/home/alice");
        assert_eq!(
            state_root_for(OsKind::Linux, Some(home), None, None),
            Path::new("/home/alice/.local/state/eggpool-connect")
        );
        assert_eq!(
            state_root_for(
                OsKind::Linux,
                Some(home),
                Some(Path::new("/tmp/state")),
                None
            ),
            Path::new("/tmp/state/eggpool-connect")
        );
        assert_eq!(
            state_root_for(OsKind::Macos, Some(Path::new("/Users/alice")), None, None),
            Path::new("/Users/alice/Library/Application Support/eggpool-connect")
        );
        assert_eq!(
            state_root_for(
                OsKind::Windows,
                Some(Path::new(r"C:\Users\alice")),
                None,
                Some(Path::new(r"C:\Users\alice\AppData\Local"))
            ),
            Path::new(r"C:\Users\alice\AppData\Local/eggpool-connect")
        );
        // Windows never falls back to Unix `$HOME/.config` assumptions.
        let windows_fallback = state_root_for(
            OsKind::Windows,
            Some(Path::new(r"C:\Users\alice")),
            None,
            None,
        );
        assert!(windows_fallback.to_string_lossy().contains("AppData"));
    }

    #[test]
    fn opencode_windows_uses_appdata_not_home_config() {
        let path = opencode_config_path_for(
            OsKind::Windows,
            Some(Path::new(r"C:\Users\alice")),
            None,
            Some(Path::new(r"C:\Users\alice\AppData\Roaming")),
        );
        assert_eq!(
            path,
            Path::new(r"C:\Users\alice\AppData\Roaming/opencode/opencode.json")
        );
        let unix =
            opencode_config_path_for(OsKind::Linux, Some(Path::new("/home/alice")), None, None);
        assert_eq!(
            unix,
            Path::new("/home/alice/.config/opencode/opencode.json")
        );
    }

    #[test]
    fn codex_honors_home_without_server_assumptions() {
        assert_eq!(
            codex_config_path_for(Some(Path::new("/home/alice"))),
            Path::new("/home/alice/.codex/config.toml")
        );
    }

    #[test]
    fn helper_catalog_stays_under_helper_state() {
        let state = Path::new("/tmp/state/eggpool-connect");
        let catalog = helper_codex_catalog_path_for(state);
        assert!(catalog.starts_with(state));
        assert!(
            catalog
                .to_string_lossy()
                .contains("eggpool-codex-models.json")
        );
    }
}
