//! Install provenance detection for the package-owned update boundary.
//!
//! The detector deliberately works from the executable and distribution
//! metadata that are visible on disk.  PATH names are only used to locate an
//! explicitly named manager executable; they never decide ownership.

use std::{
    env, fs,
    path::{Path, PathBuf},
};

const MAX_METADATA_BYTES: usize = 128 * 1024;
const MAX_EVIDENCE_ITEMS: usize = 8;
const MAX_EVIDENCE_BYTES: usize = 160;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PackageMetadata {
    pub distribution: PathBuf,
    pub version: String,
    pub installer: Option<String>,
    pub direct_url: Option<DirectUrlMetadata>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DirectUrlMetadata {
    pub url: String,
    pub editable: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum InstallProvenance {
    UvTool {
        manager: Option<PathBuf>,
        environment: PathBuf,
        python: PathBuf,
        exposed_executable: PathBuf,
        package_metadata: PackageMetadata,
    },
    Pipx {
        manager: Option<PathBuf>,
        environment: PathBuf,
        python: PathBuf,
        exposed_executable: PathBuf,
        package_metadata: PackageMetadata,
    },
    PipEnvironment {
        python: PathBuf,
        environment: PathBuf,
        exposed_executable: PathBuf,
        package_metadata: PackageMetadata,
    },
    StandaloneRust {
        executable: PathBuf,
    },
    SourceCheckout {
        root: PathBuf,
        executable: PathBuf,
    },
    Ambiguous {
        evidence: Vec<String>,
    },
}

impl InstallProvenance {
    pub fn detect(executable: &Path) -> Self {
        Self::detect_with(&ProvenanceEnvironment::current(), executable)
    }

    pub fn detect_with(environment: &ProvenanceEnvironment, executable: &Path) -> Self {
        let exposed = absolute_path(executable, environment.cwd.as_deref());
        let resolved = fs::canonicalize(&exposed).unwrap_or_else(|_| exposed.clone());

        if let Some(root) = source_checkout_root(&resolved) {
            return Self::SourceCheckout {
                root,
                executable: resolved,
            };
        }

        let metadata = find_package_metadata(&resolved);
        let mut evidence = metadata
            .iter()
            .flat_map(|candidate| candidate.evidence.iter().cloned())
            .collect::<Vec<_>>();
        if evidence
            .iter()
            .any(|item| item.contains("malformed") || item.contains("could not be read"))
        {
            return Self::Ambiguous {
                evidence: bound_evidence(evidence),
            };
        }
        let candidates = metadata
            .into_iter()
            .filter_map(|candidate| {
                candidate
                    .metadata
                    .clone()
                    .map(|metadata| (candidate, metadata))
            })
            .collect::<Vec<_>>();

        if candidates.len() > 1 {
            evidence.push("multiple eggpool distributions are visible".to_owned());
            return Self::Ambiguous {
                evidence: bound_evidence(evidence),
            };
        }

        if let Some((candidate, package_metadata)) = candidates.into_iter().next() {
            if let Some(root) = source_checkout_from_direct_url(&package_metadata) {
                return Self::SourceCheckout {
                    root,
                    executable: resolved,
                };
            }
            let environment_root = candidate.environment;
            let python = python_for_environment(&environment_root);
            let uv_signal = candidate.uv_signal;
            let pipx_signal = candidate.pipx_signal;
            let installer = package_metadata
                .installer
                .as_deref()
                .map(str::to_ascii_lowercase);

            let uv = uv_signal;
            let pipx = pipx_signal;
            if uv && pipx {
                evidence.push("uv and pipx ownership evidence conflict".to_owned());
                return Self::Ambiguous {
                    evidence: bound_evidence(evidence),
                };
            }
            if installer.as_deref().is_some_and(|value| {
                (value == "uv" && !uv_signal) || (value == "pipx" && !pipx_signal)
            }) {
                evidence.push("INSTALLER conflicts with environment ownership".to_owned());
                return Self::Ambiguous {
                    evidence: bound_evidence(evidence),
                };
            }

            let manager_name = if uv {
                "uv"
            } else if pipx {
                "pipx"
            } else {
                ""
            };
            let manager = if manager_name.is_empty() {
                None
            } else {
                find_program(manager_name, &environment.path)
            };
            if uv {
                return Self::UvTool {
                    manager,
                    environment: environment_root,
                    python,
                    exposed_executable: exposed,
                    package_metadata,
                };
            }
            if pipx {
                return Self::Pipx {
                    manager,
                    environment: environment_root,
                    python,
                    exposed_executable: exposed,
                    package_metadata,
                };
            }

            if !has_virtual_environment_marker(&environment_root) {
                evidence.push("distribution is outside a verified virtual environment".to_owned());
                return Self::Ambiguous {
                    evidence: bound_evidence(evidence),
                };
            }
            return Self::PipEnvironment {
                python,
                environment: environment_root,
                exposed_executable: exposed,
                package_metadata,
            };
        }

        if looks_like_native_executable(&resolved) {
            return Self::StandaloneRust {
                executable: exposed,
            };
        }
        evidence.push("no trusted EggPool distribution owner was found".to_owned());
        Self::Ambiguous {
            evidence: bound_evidence(evidence),
        }
    }

    pub fn exposed_executable(&self) -> Option<&Path> {
        match self {
            Self::UvTool {
                exposed_executable, ..
            }
            | Self::Pipx {
                exposed_executable, ..
            }
            | Self::PipEnvironment {
                exposed_executable, ..
            } => Some(exposed_executable),
            Self::StandaloneRust { executable } | Self::SourceCheckout { executable, .. } => {
                Some(executable)
            }
            Self::Ambiguous { .. } => None,
        }
    }

    pub fn package_metadata(&self) -> Option<&PackageMetadata> {
        match self {
            Self::UvTool {
                package_metadata, ..
            }
            | Self::Pipx {
                package_metadata, ..
            }
            | Self::PipEnvironment {
                package_metadata, ..
            } => Some(package_metadata),
            _ => None,
        }
    }

    pub fn python(&self) -> Option<&Path> {
        match self {
            Self::UvTool { python, .. }
            | Self::Pipx { python, .. }
            | Self::PipEnvironment { python, .. } => Some(python),
            _ => None,
        }
    }

    pub fn manager(&self) -> Option<&Path> {
        match self {
            Self::UvTool { manager, .. } | Self::Pipx { manager, .. } => manager.as_deref(),
            _ => None,
        }
    }

    pub fn manager_kind(&self) -> Option<&'static str> {
        match self {
            Self::UvTool { .. } => Some("uv"),
            Self::Pipx { .. } => Some("pipx"),
            Self::PipEnvironment { .. } => Some("pip"),
            _ => None,
        }
    }
}

#[derive(Debug, Clone)]
pub struct ProvenanceEnvironment {
    pub cwd: Option<PathBuf>,
    pub home: Option<PathBuf>,
    pub path: Vec<PathBuf>,
}

impl ProvenanceEnvironment {
    pub fn current() -> Self {
        Self {
            cwd: env::current_dir().ok(),
            home: env::var_os("HOME").map(PathBuf::from),
            path: env::var_os("PATH")
                .map(|value| env::split_paths(&value).collect())
                .unwrap_or_default(),
        }
    }
}

#[derive(Debug)]
struct MetadataCandidate {
    environment: PathBuf,
    metadata: Option<PackageMetadata>,
    evidence: Vec<String>,
    uv_signal: bool,
    pipx_signal: bool,
}

fn find_package_metadata(executable: &Path) -> Vec<MetadataCandidate> {
    let mut roots = Vec::new();
    let mut ancestor = executable.parent();
    for _ in 0..5 {
        let Some(path) = ancestor else { break };
        roots.push(path.to_owned());
        ancestor = path.parent();
    }

    let mut candidates = Vec::new();
    let mut seen_distributions = Vec::new();
    for bin in roots {
        let Some(environment) = bin.parent() else {
            continue;
        };
        let package_dirs = [
            environment.join("lib"),
            environment.join("Lib"),
            environment.join("site-packages"),
        ];
        let mut site_packages = Vec::new();
        for lib in package_dirs {
            if lib.file_name().is_some_and(|name| name == "site-packages") {
                if !site_packages.contains(&lib) {
                    site_packages.push(lib);
                }
            } else if let Ok(entries) = fs::read_dir(&lib) {
                for entry in entries.flatten().take(16) {
                    let path = entry.path();
                    if path.is_dir()
                        && path
                            .file_name()
                            .is_some_and(|name| name.to_string_lossy().starts_with("python"))
                    {
                        let site = fs::canonicalize(path.join("site-packages"))
                            .unwrap_or_else(|_| path.join("site-packages"));
                        if !site_packages.contains(&site) {
                            site_packages.push(site);
                        }
                    }
                }
            }
        }
        for site in site_packages {
            let Ok(entries) = fs::read_dir(&site) else {
                continue;
            };
            for entry in entries.flatten().take(64) {
                let path = entry.path();
                let name = path.file_name().map(|value| value.to_string_lossy());
                if !path.is_dir()
                    || !name.as_deref().is_some_and(|value| {
                        value.starts_with("eggpool-") && value.ends_with(".dist-info")
                    })
                {
                    continue;
                }
                if seen_distributions.iter().any(|seen| seen == &path) {
                    continue;
                }
                seen_distributions.push(path.clone());
                let (metadata, evidence) = parse_package_metadata(&path);
                if metadata.is_some() || !evidence.is_empty() {
                    candidates.push(MetadataCandidate {
                        environment: environment.to_owned(),
                        metadata,
                        evidence,
                        uv_signal: is_uv_environment(environment),
                        pipx_signal: is_pipx_environment(environment),
                    });
                }
            }
        }
    }
    candidates
}

fn parse_package_metadata(path: &Path) -> (Option<PackageMetadata>, Vec<String>) {
    let mut evidence = Vec::new();
    let metadata_path = path.join("METADATA");
    let Ok(metadata) = read_bounded(&metadata_path) else {
        evidence.push("EggPool dist-info metadata could not be read".to_owned());
        return (None, evidence);
    };
    let mut name = None;
    let mut version = None;
    for line in metadata.lines() {
        if let Some(value) = line.strip_prefix("Name:") {
            name = Some(value.trim().to_owned());
        } else if let Some(value) = line.strip_prefix("Version:") {
            version = Some(value.trim().to_owned());
        }
    }
    if name.as_deref().map(|value| value.to_ascii_lowercase()) != Some("eggpool".to_owned())
        || version.as_deref().is_none_or(|value| value.is_empty())
    {
        evidence.push("EggPool dist-info metadata is malformed".to_owned());
        return (None, evidence);
    }
    let installer = read_bounded(&path.join("INSTALLER"))
        .ok()
        .map(|value| value.trim().to_ascii_lowercase())
        .filter(|value| !value.is_empty() && value.len() <= 32);
    let direct_url = read_bounded(&path.join("direct_url.json"))
        .ok()
        .and_then(|value| match parse_direct_url(&value) {
            Some(value) => Some(value),
            None => {
                evidence.push("EggPool direct URL metadata is malformed".to_owned());
                None
            }
        });
    (
        Some(PackageMetadata {
            distribution: path.to_owned(),
            version: version.unwrap_or_default(),
            installer,
            direct_url,
        }),
        evidence,
    )
}

fn parse_direct_url(value: &str) -> Option<DirectUrlMetadata> {
    let json: serde_json::Value = serde_json::from_str(value).ok()?;
    let url = json.get("url")?.as_str()?.trim();
    if url.is_empty() || url.len() > 512 || url.contains('\n') || url.contains('\r') {
        return None;
    }
    Some(DirectUrlMetadata {
        url: url.to_owned(),
        editable: json
            .get("dir_info")
            .and_then(|value| value.get("editable"))
            .and_then(serde_json::Value::as_bool)
            .unwrap_or(false),
    })
}

fn source_checkout_root(path: &Path) -> Option<PathBuf> {
    path.ancestors()
        .find(|candidate| candidate.join(".git").exists())
        .map(Path::to_owned)
}

fn source_checkout_from_direct_url(metadata: &PackageMetadata) -> Option<PathBuf> {
    let direct_url = metadata.direct_url.as_ref()?;
    let path = direct_url.url.strip_prefix("file://")?;
    let path = Path::new(path);
    source_checkout_root(path)
}

fn python_for_environment(environment: &Path) -> PathBuf {
    let unix = environment.join("bin/python");
    if unix.exists() {
        return unix;
    }
    environment.join("Scripts/python.exe")
}

fn has_virtual_environment_marker(environment: &Path) -> bool {
    environment.join("pyvenv.cfg").is_file()
}

fn is_uv_environment(path: &Path) -> bool {
    let components = path.components().map(|component| component.as_os_str());
    let mut saw_uv = false;
    let mut saw_tools = false;
    for component in components {
        saw_uv |= component == "uv";
        saw_tools |= component == "tools";
    }
    saw_uv && saw_tools
}

fn is_pipx_environment(path: &Path) -> bool {
    let components = path.components().map(|component| component.as_os_str());
    let mut saw_pipx = false;
    let mut saw_venvs = false;
    for component in components {
        saw_pipx |= component == "pipx";
        saw_venvs |= component == "venvs" || component == "shared";
    }
    saw_pipx && saw_venvs
}

fn find_program(name: &str, path: &[PathBuf]) -> Option<PathBuf> {
    path.iter()
        .map(|directory| directory.join(name))
        .find_map(|candidate| {
            let metadata = fs::metadata(&candidate).ok()?;
            if !metadata.is_file() {
                return None;
            }
            fs::canonicalize(candidate).ok()
        })
}

fn looks_like_native_executable(path: &Path) -> bool {
    let Ok(bytes) = fs::read(path) else {
        return false;
    };
    bytes.starts_with(b"\x7fELF")
        || bytes.starts_with(b"\xcf\xfa\xed\xfe")
        || bytes.starts_with(b"\xfe\xed\xfa\xcf")
}

fn read_bounded(path: &Path) -> std::io::Result<String> {
    let metadata = fs::metadata(path)?;
    if metadata.len() > MAX_METADATA_BYTES as u64 {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "metadata is too large",
        ));
    }
    let bytes = fs::read(path)?;
    if bytes.len() > MAX_METADATA_BYTES {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "metadata is too large",
        ));
    }
    Ok(String::from_utf8_lossy(&bytes).into_owned())
}

fn absolute_path(path: &Path, cwd: Option<&Path>) -> PathBuf {
    if path.is_absolute() {
        path.to_owned()
    } else {
        cwd.unwrap_or_else(|| Path::new(".")).join(path)
    }
}

fn bound_evidence(values: Vec<String>) -> Vec<String> {
    values
        .into_iter()
        .take(MAX_EVIDENCE_ITEMS)
        .map(|value| value.chars().take(MAX_EVIDENCE_BYTES).collect())
        .collect()
}

#[cfg(test)]
mod tests {
    use std::{fs, os::unix::fs::symlink};

    use tempfile::tempdir;

    use super::*;

    fn wheel(root: &Path, installer: &str, version: &str) -> PathBuf {
        let environment = root.join("env");
        let site = environment.join("lib/python3.11/site-packages/eggpool-0.8.0.dist-info");
        fs::create_dir_all(&site).unwrap();
        fs::write(
            site.join("METADATA"),
            format!("Name: eggpool\nVersion: {version}\n"),
        )
        .unwrap();
        fs::write(site.join("INSTALLER"), installer).unwrap();
        fs::write(environment.join("pyvenv.cfg"), "version = 3.11.0\n").unwrap();
        let bin = environment.join("bin");
        fs::create_dir_all(&bin).unwrap();
        let executable = bin.join("eggpool");
        fs::write(&executable, b"#!/bin/sh\n").unwrap();
        executable
    }

    #[test]
    fn detects_ordinary_venv_from_dist_info_without_installer() {
        let root = tempdir().unwrap();
        let executable = wheel(root.path(), "", "0.8.0");
        fs::remove_file(
            root.path()
                .join("env/lib/python3.11/site-packages/eggpool-0.8.0.dist-info/INSTALLER"),
        )
        .unwrap();
        let result = InstallProvenance::detect_with(
            &ProvenanceEnvironment {
                cwd: None,
                home: None,
                path: Vec::new(),
            },
            &executable,
        );
        assert!(matches!(result, InstallProvenance::PipEnvironment { .. }));
    }

    #[test]
    fn detects_uv_and_pipx_using_environment_structure() {
        let root = tempdir().unwrap();
        let uv = root.path().join("uv/tools/eggpool");
        fs::create_dir_all(&uv).unwrap();
        let executable = wheel(&uv, "", "0.8.0");
        let result = InstallProvenance::detect(&executable);
        assert!(matches!(result, InstallProvenance::UvTool { .. }));

        let pipx = root.path().join("pipx/venvs/eggpool");
        fs::create_dir_all(&pipx).unwrap();
        let executable = wheel(&pipx, "", "0.8.0");
        let result = InstallProvenance::detect(&executable);
        assert!(matches!(result, InstallProvenance::Pipx { .. }));
    }

    #[test]
    fn conflicting_installer_metadata_is_ambiguous() {
        let root = tempdir().unwrap();
        let uv = root.path().join("uv/tools/eggpool");
        fs::create_dir_all(&uv).unwrap();
        let executable = wheel(&uv, "pipx", "0.8.0");
        assert!(matches!(
            InstallProvenance::detect(&executable),
            InstallProvenance::Ambiguous { .. }
        ));
    }

    #[test]
    fn malformed_direct_url_is_not_transition_evidence() {
        let root = tempdir().unwrap();
        let executable = wheel(root.path(), "pip", "0.8.0");
        fs::write(
            root.path()
                .join("env/lib/python3.11/site-packages/eggpool-0.8.0.dist-info/direct_url.json"),
            "{\"url\":\"; rm -rf /\"",
        )
        .unwrap();
        assert!(matches!(
            InstallProvenance::detect(&executable),
            InstallProvenance::Ambiguous { .. }
        ));
    }

    #[test]
    fn source_checkout_wins_over_missing_distribution_metadata() {
        let root = tempdir().unwrap();
        fs::create_dir(root.path().join(".git")).unwrap();
        let executable = root.path().join("rust/target/debug/eggpool");
        fs::create_dir_all(executable.parent().unwrap()).unwrap();
        fs::write(&executable, b"not native").unwrap();
        assert!(matches!(
            InstallProvenance::detect(&executable),
            InstallProvenance::SourceCheckout { .. }
        ));
    }

    #[test]
    fn native_without_dist_info_is_standalone_and_symlink_is_retained() {
        let root = tempdir().unwrap();
        let real = root.path().join("eggpool-real");
        let exposed = root.path().join("eggpool");
        fs::write(&real, b"\x7fELF native").unwrap();
        symlink(&real, &exposed).unwrap();
        let result = InstallProvenance::detect(&exposed);
        assert!(matches!(result, InstallProvenance::StandaloneRust { .. }));
        assert_eq!(result.exposed_executable(), Some(exposed.as_path()));
    }
}
