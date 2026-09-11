//! Embedded K001 installable-release catalog used before update mutation.

use std::collections::BTreeMap;

use serde::Deserialize;

use super::update::{Platform, ReleaseTarget, ReleaseVersion, UpdateError};

const K001_CATALOG: &str = include_str!("../../assets/catalog/k001-installable-releases.json");

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReleaseEra {
    Python,
    Rust,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CatalogRelease {
    pub version: ReleaseVersion,
    pub era: ReleaseEra,
    pub package_requirement: String,
    pub python_requirement: Option<String>,
    pub yanked: bool,
    pub unavailable: bool,
    pub supported_target_classes: Vec<String>,
    pub rollback_compatible: bool,
}

#[derive(Debug, Clone)]
pub struct ReleaseCatalog {
    releases: BTreeMap<String, CatalogRelease>,
    latest_stable: Option<String>,
    cutover_version: String,
}

impl ReleaseCatalog {
    pub fn embedded() -> Result<Self, UpdateError> {
        let document: CatalogDocument =
            serde_json::from_str(K001_CATALOG).map_err(|_| UpdateError::CatalogMalformed)?;
        Self::from_document(document)
    }

    pub fn resolve(
        &self,
        target: &ReleaseTarget,
        platform: &Platform,
    ) -> Result<CatalogRelease, UpdateError> {
        let key = match target {
            ReleaseTarget::Latest => self
                .latest_stable
                .as_ref()
                .ok_or(UpdateError::TargetNotCatalogued)?,
            ReleaseTarget::Exact(version) => version.as_str(),
        };
        let release = self
            .releases
            .get(key)
            .ok_or(UpdateError::TargetNotCatalogued)?;
        if release.yanked || release.unavailable {
            return Err(UpdateError::TargetUnavailable);
        }
        if release.era == ReleaseEra::Rust
            && !release
                .supported_target_classes
                .iter()
                .any(|class| class == &platform.target_class())
        {
            return Err(UpdateError::UnsupportedPlatform);
        }
        Ok(release.clone())
    }

    pub fn contains(&self, version: &ReleaseVersion) -> bool {
        self.releases.contains_key(version.as_str())
    }

    fn from_document(document: CatalogDocument) -> Result<Self, UpdateError> {
        if document.catalog_version != "k001.v2" {
            return Err(UpdateError::CatalogMalformed);
        }
        let authority = document.version_authority;
        if authority.current_release_era != "rust"
            || authority.historical_release_era != "python"
            || authority.latest_resolution != "rust-only"
            || authority.historical_exact_resolution != "explicit-package-manager-only"
            || authority.pypi_artifacts != "immutable-external-history"
            || authority.requires_python_semantics != "package-manager-compatibility-only"
        {
            return Err(UpdateError::CatalogMalformed);
        }
        let cutover = ReleaseVersion::parse(&authority.cutover_version)
            .map_err(|_| UpdateError::CatalogMalformed)?;
        let defaults = document.release_defaults;
        if defaults.implementation_era != "python" {
            return Err(UpdateError::CatalogMalformed);
        }
        let package_template = defaults.package_manager_requirement.clone();
        let python_requirement = defaults.python_requirement.clone();
        let mut releases = BTreeMap::new();
        for entry in document.releases {
            let version =
                ReleaseVersion::parse(&entry.version).map_err(|_| UpdateError::CatalogMalformed)?;
            let era = match entry.implementation_era.as_deref().unwrap_or("python") {
                "python" => ReleaseEra::Python,
                "rust" => ReleaseEra::Rust,
                _ => return Err(UpdateError::CatalogMalformed),
            };
            let package_requirement = package_template.replace("{version}", version.as_str());
            if package_requirement != format!("eggpool=={}", version.as_str()) {
                return Err(UpdateError::CatalogMalformed);
            }
            let release = CatalogRelease {
                version: version.clone(),
                era,
                package_requirement,
                python_requirement: Some(python_requirement.clone()),
                yanked: defaults.yanked,
                unavailable: defaults.unavailable || !defaults.pypi_presence,
                supported_target_classes: Vec::new(),
                rollback_compatible: entry
                    .rollback_suitability
                    .as_deref()
                    .is_some_and(|value| value == "compatible-with-schema54"),
            };
            if releases
                .insert(version.as_str().to_owned(), release)
                .is_some()
            {
                return Err(UpdateError::CatalogMalformed);
            }
        }
        let cutover_key = cutover.as_str().to_owned();
        let cutover_release = releases
            .get_mut(&cutover_key)
            .ok_or(UpdateError::CatalogMalformed)?;
        if cutover_release.era != ReleaseEra::Rust {
            return Err(UpdateError::CatalogMalformed);
        }
        cutover_release.supported_target_classes = defaults.supported_target_classes;
        cutover_release.rollback_compatible = true;
        let latest_stable = releases
            .values()
            .filter(|release| {
                release.era == ReleaseEra::Rust && !release.yanked && !release.unavailable
            })
            .max_by(|left, right| {
                left.version
                    .ordering_key()
                    .cmp(&right.version.ordering_key())
            })
            .map(|release| release.version.as_str().to_owned());
        if latest_stable.as_deref() != Some(cutover_key.as_str()) {
            return Err(UpdateError::CatalogMalformed);
        }
        Ok(Self {
            releases,
            latest_stable,
            cutover_version: cutover_key,
        })
    }

    pub fn cutover_version(&self) -> &str {
        &self.cutover_version
    }
}

impl Platform {
    pub fn target_class(&self) -> String {
        match (self.os.as_str(), self.architecture.as_str()) {
            ("linux", "x86_64") => "linux-x86_64".to_owned(),
            ("linux", "aarch64") => "linux-aarch64".to_owned(),
            ("macos", "aarch64") => "macos-arm64".to_owned(),
            _ => format!("{}-{}", self.os, self.architecture),
        }
    }
}

#[derive(Debug, Deserialize)]
struct CatalogDocument {
    catalog_version: String,
    releases: Vec<CatalogEntry>,
    version_authority: VersionAuthority,
    release_defaults: ReleaseDefaults,
}

#[derive(Debug, Deserialize)]
struct VersionAuthority {
    cutover_version: String,
    current_release_era: String,
    historical_release_era: String,
    latest_resolution: String,
    historical_exact_resolution: String,
    pypi_artifacts: String,
    requires_python_semantics: String,
}

#[derive(Debug, Deserialize)]
struct ReleaseDefaults {
    implementation_era: String,
    supported_target_classes: Vec<String>,
    python_requirement: String,
    package_manager_requirement: String,
    pypi_presence: bool,
    yanked: bool,
    unavailable: bool,
}

#[derive(Debug, Deserialize)]
struct CatalogEntry {
    version: String,
    implementation_era: Option<String>,
    rollback_suitability: Option<String>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn latest_package_target_is_the_rust_cutover_candidate() {
        let catalog = ReleaseCatalog::embedded().expect("embedded catalog");
        let release = catalog
            .resolve(
                &ReleaseTarget::Latest,
                &Platform {
                    os: "linux".to_owned(),
                    architecture: "x86_64".to_owned(),
                },
            )
            .expect("latest release");

        assert_eq!(release.version.as_str(), "0.8.0");
        assert_eq!(release.era, ReleaseEra::Rust);
    }
}
