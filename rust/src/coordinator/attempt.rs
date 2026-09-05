//! One provider-bound attempt: prepare once, submit once.

use bytes::Bytes;
use http::{HeaderMap, HeaderName, HeaderValue, Method, StatusCode};
use thiserror::Error;

use crate::{
    config::{ProviderAuthConfig, ProviderConfig, ProviderStaticHeaderConfig},
    providers::{ProviderClientPool, ProviderClientPoolError, ProviderResponse, TransportError},
    wire::ir::ClientSurface,
    wire::{ConfiguredWireProfile, WireRuntime, WireRuntimeContext, WireRuntimeError},
};

use super::{FinalizationIdentity, wire_resolver::WireCandidate};

#[derive(Debug, Clone)]
pub struct AttemptInput {
    pub identity: FinalizationIdentity,
    pub provider: ProviderConfig,
    pub account_api_key: Option<String>,
    pub raw_body: Bytes,
    pub client_surface: ClientSurface,
    pub profile: ConfiguredWireProfile,
    pub stream: bool,
    pub candidate_fingerprint: String,
}

#[derive(Clone)]
pub struct PreparedUpstreamAttempt {
    pub identity: FinalizationIdentity,
    pub provider_id: String,
    pub account_name: String,
    pub upstream_model_id: String,
    pub profile: ConfiguredWireProfile,
    pub candidate_fingerprint: String,
    pub method: Method,
    pub path: String,
    pub headers: HeaderMap,
    pub body: Bytes,
    pub stream: bool,
}

impl std::fmt::Debug for PreparedUpstreamAttempt {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("PreparedUpstreamAttempt")
            .field("identity", &self.identity)
            .field("provider_id", &self.provider_id)
            .field("account_name", &self.account_name)
            .field("upstream_model_id", &self.upstream_model_id)
            .field("profile", &self.profile.definition.surface)
            .field("candidate_fingerprint", &self.candidate_fingerprint)
            .field("method", &self.method)
            .field("path", &self.path)
            .field(
                "header_names",
                &self
                    .headers
                    .keys()
                    .map(HeaderName::as_str)
                    .collect::<Vec<_>>(),
            )
            .field("body_bytes", &self.body.len())
            .field("stream", &self.stream)
            .finish()
    }
}

#[derive(Debug, Error)]
pub enum AttemptError {
    #[error("wire request preparation failed: {0}")]
    Wire(#[from] WireRuntimeError),
    #[error("provider client lookup failed: {0}")]
    ClientPool(#[from] ProviderClientPoolError),
    #[error("provider transport failed: {0}")]
    Transport(#[from] TransportError),
    #[error("provider attempt input is invalid: {0}")]
    InvalidInput(String),
}

#[derive(Debug)]
pub struct UpstreamResponseEvidence {
    pub status: StatusCode,
    pub headers: HeaderMap,
    pub body: crate::providers::ProviderBody,
}

#[derive(Clone, Debug)]
pub struct AttemptBuilder {
    clients: ProviderClientPool,
    wire: WireRuntime,
}

impl AttemptBuilder {
    pub fn new(clients: ProviderClientPool, wire: WireRuntime) -> Self {
        Self { clients, wire }
    }

    pub fn prepare(&self, input: AttemptInput) -> Result<PreparedUpstreamAttempt, AttemptError> {
        validate_input(&input)?;
        let mut context = WireRuntimeContext::new(
            input.client_surface,
            input.profile.clone(),
            input.identity.model_id.clone(),
            input.identity.model_id.clone(),
        );
        context.provider_id = Some(input.identity.provider_id.clone());
        context.provider_kind = input.provider.kind.clone();
        let prepared = self.wire.prepare_request(&input.raw_body, &context)?;
        let path_template = if input.stream {
            input
                .profile
                .stream_path_template
                .as_deref()
                .unwrap_or(&input.profile.path_template)
        } else {
            &input.profile.path_template
        };
        let path = expand_path(path_template, &input.identity.model_id)?;
        let identity = input.identity.clone();
        let mut headers = HeaderMap::new();
        headers.insert(
            http::header::CONTENT_TYPE,
            HeaderValue::from_static("application/json"),
        );
        headers.insert(
            http::header::ACCEPT,
            HeaderValue::from_static("application/json"),
        );
        add_static_headers(&mut headers, &input.provider.headers)?;
        if let Some(surface) = input
            .provider
            .wire_surfaces
            .get(input.profile.definition.surface.as_str())
        {
            add_static_headers(&mut headers, &surface.headers)?;
            if let Some(auth) = &surface.auth {
                add_auth_header(&mut headers, auth, input.account_api_key.as_deref())?;
            }
        } else {
            add_auth_header(
                &mut headers,
                &input.provider.auth,
                input.account_api_key.as_deref(),
            )?;
        }
        Ok(PreparedUpstreamAttempt {
            identity: identity.clone(),
            provider_id: identity.provider_id.clone(),
            account_name: identity.account_name.clone(),
            upstream_model_id: identity.model_id.clone(),
            profile: input.profile,
            candidate_fingerprint: input.candidate_fingerprint,
            method: Method::POST,
            path,
            headers,
            body: prepared.body.bytes,
            stream: input.stream,
        })
    }

    pub async fn submit_once(
        &self,
        attempt: PreparedUpstreamAttempt,
    ) -> Result<UpstreamResponseEvidence, AttemptError> {
        let client = self
            .clients
            .get_client(&attempt.provider_id, Some(&attempt.account_name))?;
        let response: ProviderResponse = client
            .send(attempt.method, &attempt.path, attempt.headers, attempt.body)
            .await?;
        Ok(UpstreamResponseEvidence {
            status: response.status,
            headers: response.headers,
            body: response.body,
        })
    }

    pub fn prepare_candidates(
        &self,
        profiles: Vec<ConfiguredWireProfile>,
        fingerprint: impl Into<String>,
    ) -> Vec<WireCandidate> {
        let fingerprint = fingerprint.into();
        profiles
            .into_iter()
            .map(|profile| WireCandidate::new(profile, fingerprint.clone()))
            .collect()
    }
}

fn validate_input(input: &AttemptInput) -> Result<(), AttemptError> {
    if input.identity.provider_id.trim().is_empty() || input.identity.model_id.trim().is_empty() {
        return Err(AttemptError::InvalidInput(
            "provider and model are required".into(),
        ));
    }
    if input.raw_body.is_empty() {
        return Err(AttemptError::InvalidInput("request body is empty".into()));
    }
    Ok(())
}

fn expand_path(template: &str, model_id: &str) -> Result<String, AttemptError> {
    if !template.starts_with('/') || template.contains("//") || template.contains("..") {
        return Err(AttemptError::InvalidInput(
            "provider path is not relative and safe".into(),
        ));
    }
    Ok(template
        .replace("{model}", model_id)
        .replace("{model_id}", model_id))
}

fn add_static_headers(
    headers: &mut HeaderMap,
    values: &[ProviderStaticHeaderConfig],
) -> Result<(), AttemptError> {
    for value in values {
        let Some(raw) = value.value.as_deref() else {
            continue;
        };
        let name = HeaderName::try_from(value.name.as_str())
            .map_err(|_| AttemptError::InvalidInput("invalid provider header name".into()))?;
        let header = HeaderValue::try_from(raw)
            .map_err(|_| AttemptError::InvalidInput("invalid provider header value".into()))?;
        headers.insert(name, header);
    }
    Ok(())
}

fn add_auth_header(
    headers: &mut HeaderMap,
    auth: &ProviderAuthConfig,
    key: Option<&str>,
) -> Result<(), AttemptError> {
    if auth.mode.eq_ignore_ascii_case("none") {
        return Ok(());
    }
    let key = key
        .filter(|value| !value.is_empty())
        .ok_or_else(|| AttemptError::InvalidInput("provider credential is required".into()))?;
    let value = if auth.scheme.is_empty() {
        key.to_owned()
    } else {
        format!("{} {key}", auth.scheme)
    };
    let name = HeaderName::try_from(auth.header.as_str())
        .map_err(|_| AttemptError::InvalidInput("invalid provider auth header".into()))?;
    let value = HeaderValue::try_from(value)
        .map_err(|_| AttemptError::InvalidInput("invalid provider auth value".into()))?;
    headers.insert(name, value);
    Ok(())
}
