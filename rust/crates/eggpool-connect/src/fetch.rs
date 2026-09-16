//! Remote integration-profile fetch.
//!
//! The helper never imports EggPool's full provider transport stack to fetch
//! one small JSON document. This module uses the narrowest reusable HTTPS
//! facility already in the workspace (Hyper + Rustls + webpki roots) with TLS
//! verification and bounded response behavior.

use eggpool_client_config::{
    AgentIntegrationProfileV1, ConnectionProfileV1, MAX_INTEGRATION_PROFILE_BYTES,
};

use http_body_util::BodyExt;
use hyper::Request;
use hyper_rustls::HttpsConnectorBuilder;
use hyper_util::client::legacy::Client;
use hyper_util::rt::TokioExecutor;

use crate::outcome::ConnectError;

/// Timeout for the authenticated profile fetch.
pub const FETCH_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(20);

/// Narrow profile fetcher seam (fake in tests, Hyper in production).
pub trait ProfileFetcher: Send + Sync {
    /// Fetch and validate the versioned integration profile.
    fn fetch(
        &self,
        profile: &ConnectionProfileV1,
        api_key: &str,
    ) -> impl std::future::Future<Output = Result<AgentIntegrationProfileV1, ConnectError>> + Send;
}

/// Hyper-backed fetcher with TLS verification and bounded responses.
#[derive(Debug, Clone, Copy, Default)]
pub struct HyperProfileFetcher;

impl ProfileFetcher for HyperProfileFetcher {
    async fn fetch(
        &self,
        profile: &ConnectionProfileV1,
        api_key: &str,
    ) -> Result<AgentIntegrationProfileV1, ConnectError> {
        fetch_integration_profile(profile, api_key).await
    }
}

/// Build the request URL for `GET <server-root><endpoint>`.
///
/// The advertised connection URL is the API root (`.../v1`) while the
/// integration endpoint is server-root-relative (`/api/integrations/...`),
/// so one trailing `/v1` segment is stripped before joining. The endpoint
/// prefix gate keeps profiles from pointing the helper at arbitrary paths.
pub fn integration_profile_url(profile: &ConnectionProfileV1) -> Result<String, ConnectError> {
    let base = profile.proxy.base_url.trim_end_matches('/');
    let endpoint = profile.integration_profile.endpoint.as_str();
    if !endpoint.starts_with("/api/integrations/") {
        return Err(ConnectError::InvalidProfile {
            detail: "integration endpoint must be under /api/integrations/".to_owned(),
        });
    }
    let root = base.strip_suffix("/v1").unwrap_or(base);
    Ok(format!("{root}{endpoint}"))
}

/// Fetch `GET <server-root><endpoint>` with Bearer auth and validate the
/// versioned schema, bounds, and revision.
pub async fn fetch_integration_profile(
    profile: &ConnectionProfileV1,
    api_key: &str,
) -> Result<AgentIntegrationProfileV1, ConnectError> {
    if api_key.is_empty() {
        return Err(ConnectError::Credential {
            detail: "credential is required to fetch the integration profile".to_owned(),
        });
    }
    let base = profile.proxy.base_url.trim_end_matches('/');
    let url = integration_profile_url(profile)?;
    let uri: hyper::Uri = url.parse().map_err(|_| ConnectError::AuthNetwork {
        detail: "advertised EggPool URL is not a valid URI".to_owned(),
    })?;
    if uri.scheme_str() != Some("http") && uri.scheme_str() != Some("https") {
        return Err(ConnectError::AuthNetwork {
            detail: "advertised EggPool URL must be http(s)".to_owned(),
        });
    }

    let connector = HttpsConnectorBuilder::new()
        .with_webpki_roots()
        .https_or_http()
        .enable_http1()
        .build();
    let client: Client<_, http_body_util::Full<hyper::body::Bytes>> =
        Client::builder(TokioExecutor::new()).build(connector);

    let request = Request::builder()
        .method("GET")
        .uri(uri)
        .header("authorization", format!("Bearer {api_key}"))
        .header("x-api-key", api_key)
        .header("accept", "application/json")
        .body(http_body_util::Full::new(hyper::body::Bytes::new()))
        .map_err(|_| ConnectError::AuthNetwork {
            detail: "cannot build EggPool request".to_owned(),
        })?;

    let response = tokio::time::timeout(FETCH_TIMEOUT, client.request(request))
        .await
        .map_err(|_| ConnectError::AuthNetwork {
            detail: "EggPool request timed out".to_owned(),
        })?
        .map_err(|error| ConnectError::AuthNetwork {
            detail: format!("EggPool request failed: {error}"),
        })?;

    let status = response.status().as_u16();
    if status == 401 || status == 403 {
        return Err(ConnectError::AuthNetwork {
            detail: "EggPool rejected the credential (401/403); check EGGPOOL_API_KEY".to_owned(),
        });
    }
    if status == 304 {
        return Err(ConnectError::AuthNetwork {
            detail: "unexpected 304 without a cached revision".to_owned(),
        });
    }
    if !(200..300).contains(&status) {
        return Err(ConnectError::AuthNetwork {
            detail: format!("EggPool returned status {status}"),
        });
    }

    let body = response.into_body();
    // Bound the response before buffering: one byte over the portable limit
    // is enough to fail closed without unbounded allocation.
    let limit = (MAX_INTEGRATION_PROFILE_BYTES + 1) as u64;
    let collected = tokio::time::timeout(FETCH_TIMEOUT, body.collect())
        .await
        .map_err(|_| ConnectError::AuthNetwork {
            detail: "EggPool response timed out".to_owned(),
        })?
        .map_err(|error| ConnectError::AuthNetwork {
            detail: format!("cannot read EggPool response: {error}"),
        })?;
    let bytes = collected.to_bytes();
    if bytes.len() as u64 > limit {
        return Err(ConnectError::AuthNetwork {
            detail: "integration profile exceeds bounded size".to_owned(),
        });
    }
    if bytes.len() > MAX_INTEGRATION_PROFILE_BYTES {
        return Err(ConnectError::AuthNetwork {
            detail: "integration profile exceeds bounded size".to_owned(),
        });
    }
    let text = std::str::from_utf8(&bytes).map_err(|_| ConnectError::AuthNetwork {
        detail: "integration profile is not valid UTF-8".to_owned(),
    })?;
    let remote: AgentIntegrationProfileV1 =
        serde_json::from_str(text).map_err(|error| ConnectError::AuthNetwork {
            detail: format!("integration profile is not valid JSON: {error}"),
        })?;
    // Validate schema, bounds, ordering, and deterministic revision.
    remote
        .validate()
        .map_err(|error| ConnectError::AuthNetwork {
            detail: format!("integration profile failed validation: {error}"),
        })?;
    if remote.schema_version != profile.integration_profile.schema {
        return Err(ConnectError::AuthNetwork {
            detail: format!(
                "integration profile schema {} does not match connection profile schema {}",
                remote.schema_version, profile.integration_profile.schema
            ),
        });
    }
    // The fetched base URL must match the connection target after
    // trailing-slash normalization; otherwise the helper would configure a
    // different EggPool than the one it authenticated to.
    let fetched_base = remote.base_url.trim_end_matches('/');
    if fetched_base != base {
        return Err(ConnectError::AuthNetwork {
            detail: "integration profile base URL does not match the connection profile".to_owned(),
        });
    }
    Ok(remote)
}

/// Fake fetcher for deterministic tests (no network).
#[derive(Debug, Clone)]
pub struct FakeProfileFetcher {
    pub profile: Option<AgentIntegrationProfileV1>,
    pub error: Option<String>,
}

impl FakeProfileFetcher {
    #[must_use]
    pub const fn ok(profile: AgentIntegrationProfileV1) -> Self {
        Self {
            profile: Some(profile),
            error: None,
        }
    }

    #[must_use]
    pub fn err(detail: &str) -> Self {
        Self {
            profile: None,
            error: Some(detail.to_owned()),
        }
    }
}

impl ProfileFetcher for FakeProfileFetcher {
    async fn fetch(
        &self,
        _profile: &ConnectionProfileV1,
        api_key: &str,
    ) -> Result<AgentIntegrationProfileV1, ConnectError> {
        if api_key.is_empty() {
            return Err(ConnectError::Credential {
                detail: "credential is required".to_owned(),
            });
        }
        if let Some(error) = &self.error {
            return Err(ConnectError::AuthNetwork {
                detail: error.clone(),
            });
        }
        self.profile
            .clone()
            .ok_or_else(|| ConnectError::AuthNetwork {
                detail: "no fake profile configured".to_owned(),
            })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use eggpool_client_config::{ClientTarget, ConnectionProfileV1};

    fn connection() -> ConnectionProfileV1 {
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

    #[tokio::test]
    async fn fake_fetcher_requires_credential_and_returns_profile() {
        let remote = AgentIntegrationProfileV1::new(
            "https://pool.example/v1",
            Vec::new(),
            eggpool_client_config::IntegrationCapabilities::default(),
        )
        .expect("remote");
        let fetcher = FakeProfileFetcher::ok(remote.clone());
        assert!(
            fetcher.fetch(&connection(), "").await.is_err(),
            "empty credential must fail"
        );
        let fetched = fetcher
            .fetch(&connection(), "ep_test_key")
            .await
            .expect("fetch");
        assert_eq!(fetched.revision, remote.revision);
    }

    #[tokio::test]
    async fn fake_fetcher_surfaces_network_errors() {
        let fetcher = FakeProfileFetcher::err("dns failure");
        let error = fetcher
            .fetch(&connection(), "ep_test_key")
            .await
            .expect_err("must fail");
        assert!(error.to_string().contains("dns"));
    }

    #[test]
    fn profile_url_joins_server_root_not_api_root() {
        // The advertised connection URL ends in /v1 but the integration
        // endpoint is server-root-relative; joining must strip one /v1
        // segment (live-qualification regression: /v1/api/... 404s).
        let url = integration_profile_url(&connection()).expect("url");
        assert_eq!(url, "https://pool.example/api/integrations/v1/profile");
        assert!(!url.contains("/v1/api/"));
    }
}
