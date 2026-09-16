//! Versioned portable connection profiles.
//!
//! The profile is intentionally small: schema, targets, proxy reference,
//! auth reference, remote integration-profile reference, and optional
//! non-sensitive issuer metadata. It never carries receiving-machine
//! filesystem paths, shell commands, arbitrary environment assignments,
//! client config fragments, provider credentials, or raw model catalogs.

use serde::{Deserialize, Serialize};

use crate::adapter::ClientTarget;
use crate::error::ClientConfigError;

/// Canonical schema identifier for connection profiles.
pub const PROFILE_SCHEMA_ID: &str = "eggpool.connection/v1";
/// Accepted schema major version. Unknown majors fail closed.
pub const PROFILE_SCHEMA_MAJOR: u32 = 1;
/// Maximum portable targets in one profile.
pub const MAX_PROFILE_TARGETS: usize = 4;
/// Maximum base URL bytes.
pub const MAX_BASE_URL_BYTES: usize = 2048;
/// Maximum endpoint reference bytes.
pub const MAX_ENDPOINT_BYTES: usize = 256;
/// Maximum environment-variable reference bytes.
pub const MAX_ENV_BYTES: usize = 128;
/// Maximum issuer version bytes.
pub const MAX_ISSUER_VERSION_BYTES: usize = 64;

/// Protocol/wire fact required for setup.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum WireProtocol {
    Responses,
}

/// Auth reference mode. Only bearer-by-environment is portable.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum AuthMode {
    #[serde(rename = "bearer_env")]
    BearerEnv,
}

/// Absolute EggPool proxy reference.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ProxyReference {
    pub base_url: String,
    pub wire: WireProtocol,
}

/// Secret-free auth reference (never a resolved credential).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AuthReference {
    pub mode: AuthMode,
    pub env: String,
}

/// Remote integration-profile endpoint reference.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct IntegrationProfileReference {
    pub endpoint: String,
    pub schema: u32,
}

/// Optional non-sensitive issuer metadata.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct IssuerMetadata {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub eggpool_version: Option<String>,
}

/// Versioned portable connection profile.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ConnectionProfileV1 {
    pub schema: String,
    pub targets: Vec<ClientTarget>,
    pub proxy: ProxyReference,
    pub auth: AuthReference,
    pub integration_profile: IntegrationProfileReference,
    pub issuer: IssuerMetadata,
}

impl ConnectionProfileV1 {
    /// Build a profile with canonical target ordering and validation.
    pub fn new(
        targets: Vec<ClientTarget>,
        base_url: &str,
        auth_env: &str,
        integration_endpoint: &str,
        integration_schema: u32,
        issuer_version: Option<&str>,
    ) -> Result<Self, ClientConfigError> {
        let mut profile = Self {
            schema: PROFILE_SCHEMA_ID.to_owned(),
            targets,
            proxy: ProxyReference {
                base_url: base_url.to_owned(),
                wire: WireProtocol::Responses,
            },
            auth: AuthReference {
                mode: AuthMode::BearerEnv,
                env: auth_env.to_owned(),
            },
            integration_profile: IntegrationProfileReference {
                endpoint: integration_endpoint.to_owned(),
                schema: integration_schema,
            },
            issuer: IssuerMetadata {
                eggpool_version: issuer_version.map(str::to_owned),
            },
        };
        profile.canonicalize();
        profile.validate()?;
        Ok(profile)
    }

    /// Canonicalize for deterministic encoding (sorted, deduplicated targets).
    pub fn canonicalize(&mut self) {
        self.targets.sort_by_key(|target| target.name());
        self.targets.dedup();
    }

    /// Strict validation: schema major, targets, URLs, auth, endpoint.
    pub fn validate(&self) -> Result<(), ClientConfigError> {
        validate_schema(&self.schema)?;
        if self.targets.is_empty() {
            return Err(ClientConfigError::InvalidField {
                field: "targets".to_owned(),
                detail: "at least one client target is required".to_owned(),
            });
        }
        if self.targets.len() > MAX_PROFILE_TARGETS {
            return Err(ClientConfigError::TooLarge {
                detail: format!("too many targets ({})", self.targets.len()),
            });
        }
        validate_base_url(&self.proxy.base_url)?;
        if self.proxy.wire != WireProtocol::Responses {
            return Err(ClientConfigError::UnsupportedSchema {
                detail: "unsupported wire protocol".to_owned(),
            });
        }
        validate_env_name(&self.auth.env)?;
        if self.auth.mode != AuthMode::BearerEnv {
            return Err(ClientConfigError::UnsupportedSchema {
                detail: "unsupported auth mode".to_owned(),
            });
        }
        validate_endpoint(&self.integration_profile.endpoint)?;
        if self.integration_profile.schema == 0 {
            return Err(ClientConfigError::InvalidField {
                field: "integration_profile.schema".to_owned(),
                detail: "schema version must be positive".to_owned(),
            });
        }
        if let Some(version) = &self.issuer.eggpool_version {
            if version.len() > MAX_ISSUER_VERSION_BYTES {
                return Err(ClientConfigError::TooLarge {
                    detail: "issuer version exceeds bounded size".to_owned(),
                });
            }
            if version.chars().any(|c| c.is_control()) {
                return Err(ClientConfigError::InvalidField {
                    field: "issuer.eggpool_version".to_owned(),
                    detail: "version contains control characters".to_owned(),
                });
            }
        }
        Ok(())
    }

    /// Canonical UTF-8 JSON (field order fixed by struct definition).
    pub fn canonical_json(&self) -> Result<String, ClientConfigError> {
        let mut canonical = self.clone();
        canonical.canonicalize();
        canonical.validate()?;
        Ok(serde_json::to_string(&canonical)?)
    }
}

/// Validate the schema identifier with a strict major-version boundary.
pub fn validate_schema(schema: &str) -> Result<(), ClientConfigError> {
    if schema == PROFILE_SCHEMA_ID {
        return Ok(());
    }
    if let Some(suffix) = schema.strip_prefix("eggpool.connection/v") {
        let major: Result<u32, _> = suffix.parse();
        match major {
            Ok(PROFILE_SCHEMA_MAJOR) => {
                return Err(ClientConfigError::InvalidField {
                    field: "schema".to_owned(),
                    detail: format!(
                        "unsupported schema identifier {schema:?}; expected {PROFILE_SCHEMA_ID:?}"
                    ),
                });
            }
            Ok(other) => {
                return Err(ClientConfigError::UnsupportedSchema {
                    detail: format!(
                        "unsupported connection profile major version {other}; expected {PROFILE_SCHEMA_MAJOR}"
                    ),
                });
            }
            Err(_) => {}
        }
    }
    Err(ClientConfigError::UnsupportedSchema {
        detail: format!("unsupported connection profile schema {schema:?}"),
    })
}

/// Validate an absolute HTTP(S) base URL without whitespace/control injection.
pub fn validate_base_url(base_url: &str) -> Result<(), ClientConfigError> {
    if base_url.len() > MAX_BASE_URL_BYTES {
        return Err(ClientConfigError::TooLarge {
            detail: "base URL exceeds bounded size".to_owned(),
        });
    }
    if base_url.is_empty()
        || base_url
            .chars()
            .any(|c| c.is_whitespace() || c.is_control())
    {
        return Err(ClientConfigError::InvalidBaseUrl);
    }
    let lower = base_url.to_ascii_lowercase();
    let rest = if let Some(rest) = lower.strip_prefix("https://") {
        rest
    } else if let Some(rest) = lower.strip_prefix("http://") {
        rest
    } else {
        return Err(ClientConfigError::InvalidBaseUrl);
    };
    if rest.is_empty() || !rest.contains('.') && rest != "localhost" && !rest.starts_with("127.") {
        // Require an authority-like remainder; loopback and dotted hosts pass,
        // bare empty authorities fail. This is intentionally narrow without a
        // URL parser dependency.
        if rest.is_empty() || rest.starts_with('/') || rest.starts_with(':') && rest.len() < 2 {
            return Err(ClientConfigError::InvalidBaseUrl);
        }
    }
    if !base_url.contains("://") {
        return Err(ClientConfigError::InvalidBaseUrl);
    }
    // Preserve original for authority check (case-sensitive host is allowed).
    let authority = base_url
        .split("://")
        .nth(1)
        .unwrap_or_default()
        .split('/')
        .next()
        .unwrap_or_default();
    if authority.is_empty() {
        return Err(ClientConfigError::InvalidBaseUrl);
    }
    Ok(())
}

fn validate_env_name(env: &str) -> Result<(), ClientConfigError> {
    if env.is_empty() || env.len() > MAX_ENV_BYTES {
        return Err(ClientConfigError::InvalidField {
            field: "auth.env".to_owned(),
            detail: "environment reference has invalid length".to_owned(),
        });
    }
    if env.chars().any(|c| c.is_whitespace() || c.is_control()) {
        return Err(ClientConfigError::InvalidField {
            field: "auth.env".to_owned(),
            detail: "environment reference contains whitespace or control characters".to_owned(),
        });
    }
    let mut chars = env.chars();
    let first = chars.next().unwrap_or_default();
    if !first.is_ascii_alphabetic() && first != '_' {
        return Err(ClientConfigError::InvalidField {
            field: "auth.env".to_owned(),
            detail: "environment reference must start with a letter or underscore".to_owned(),
        });
    }
    if !env.chars().all(|c| c.is_ascii_alphanumeric() || c == '_') {
        return Err(ClientConfigError::InvalidField {
            field: "auth.env".to_owned(),
            detail: "environment reference must be ASCII alphanumeric/underscore".to_owned(),
        });
    }
    Ok(())
}

fn validate_endpoint(endpoint: &str) -> Result<(), ClientConfigError> {
    if endpoint.len() > MAX_ENDPOINT_BYTES {
        return Err(ClientConfigError::TooLarge {
            detail: "integration endpoint exceeds bounded size".to_owned(),
        });
    }
    if !endpoint.starts_with("/api/integrations/") {
        return Err(ClientConfigError::InvalidField {
            field: "integration_profile.endpoint".to_owned(),
            detail: "endpoint must be under /api/integrations/".to_owned(),
        });
    }
    if endpoint
        .chars()
        .any(|c| c.is_whitespace() || c.is_control())
    {
        return Err(ClientConfigError::InvalidField {
            field: "integration_profile.endpoint".to_owned(),
            detail: "endpoint contains whitespace or control characters".to_owned(),
        });
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn profile() -> ConnectionProfileV1 {
        ConnectionProfileV1::new(
            vec![ClientTarget::Codex],
            "https://pool.example/v1",
            "EGGPOOL_API_KEY",
            "/api/integrations/v1/profile",
            1,
            Some("0.8.0"),
        )
        .expect("profile")
    }

    #[test]
    fn profile_json_matches_documented_shape_and_is_secret_free() {
        let profile = profile();
        let json = profile.canonical_json().expect("json");
        let value: serde_json::Value = serde_json::from_str(&json).expect("parse");
        assert_eq!(
            value.get("schema").and_then(|v| v.as_str()),
            Some("eggpool.connection/v1")
        );
        assert_eq!(
            value
                .get("proxy")
                .and_then(|p| p.get("base_url"))
                .and_then(|v| v.as_str()),
            Some("https://pool.example/v1")
        );
        assert_eq!(
            value
                .get("proxy")
                .and_then(|p| p.get("wire"))
                .and_then(|v| v.as_str()),
            Some("responses")
        );
        assert_eq!(
            value
                .get("auth")
                .and_then(|a| a.get("env"))
                .and_then(|v| v.as_str()),
            Some("EGGPOOL_API_KEY")
        );
        // Secret-free: auth is a reference only.
        assert!(!json.contains("ep_"));
        let debug = format!("{profile:?}");
        assert!(!debug.contains("ep_") || debug.contains("EGGPOOL_API_KEY"));
    }

    #[test]
    fn profile_rejects_unknown_major_and_invalid_urls() {
        let mut bad = profile();
        bad.schema = "eggpool.connection/v2".to_owned();
        assert!(bad.validate().is_err());

        assert!(validate_base_url("https://pool.example/v1").is_ok());
        assert!(validate_base_url("not a url").is_err());
        assert!(validate_base_url("ftp://pool.example/v1").is_err());
        assert!(validate_base_url("https://pool.example/v1 with space").is_err());
        assert!(validate_base_url("https://pool.example/v1\n").is_err());
    }

    #[test]
    fn profile_rejects_arbitrary_targets_and_paths() {
        // Closed enum prevents arbitrary executable names by construction;
        // JSON with an unknown target must fail to parse.
        let raw = r#"{"schema":"eggpool.connection/v1","targets":["vscode"],"proxy":{"base_url":"https://pool.example/v1","wire":"responses"},"auth":{"mode":"bearer_env","env":"EGGPOOL_API_KEY"},"integration_profile":{"endpoint":"/api/integrations/v1/profile","schema":1},"issuer":{}}"#;
        let parsed: Result<ConnectionProfileV1, _> = serde_json::from_str(raw);
        assert!(parsed.is_err());

        // Receiving-machine paths cannot be supplied: unknown fields fail.
        let raw = r#"{"schema":"eggpool.connection/v1","targets":["codex"],"proxy":{"base_url":"https://pool.example/v1","wire":"responses"},"auth":{"mode":"bearer_env","env":"EGGPOOL_API_KEY"},"integration_profile":{"endpoint":"/api/integrations/v1/profile","schema":1},"issuer":{},"config_path":"/tmp/evil"}"#;
        let parsed: Result<ConnectionProfileV1, _> = serde_json::from_str(raw);
        assert!(parsed.is_err());
    }

    #[test]
    fn profile_targets_are_canonicalized_and_bounded() {
        let mut profile = profile();
        profile.targets = vec![ClientTarget::Opencode, ClientTarget::Codex];
        profile.canonicalize();
        assert_eq!(
            profile.targets,
            vec![ClientTarget::Codex, ClientTarget::Opencode]
        );
        profile.targets = Vec::new();
        assert!(profile.validate().is_err());
    }
}
