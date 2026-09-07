//! One provider-bound attempt: prepare once, submit once.

use bytes::Bytes;
use http::{HeaderMap, HeaderName, HeaderValue, Method, StatusCode};
use std::time::{Duration, Instant};
use thiserror::Error;

use crate::{
    config::{ProviderAuthConfig, ProviderConfig, ProviderStaticHeaderConfig},
    providers::{ProviderClientPool, ProviderClientPoolError, ProviderResponse, TransportError},
    request::AdmittedRequest,
    wire::ir::ClientSurface,
    wire::{ConfiguredWireProfile, WireRuntime, WireRuntimeContext, WireRuntimeError},
};

use super::{
    CoordinatorFaultInjector, CrashFaultPoint, FinalizationIdentity, wire_resolver::WireCandidate,
};

#[derive(Debug, Clone)]
pub struct AttemptInput {
    pub identity: FinalizationIdentity,
    pub provider: ProviderConfig,
    pub account_api_key: Option<String>,
    /// The original incoming headers.  Local credentials, hop-by-hop, and
    /// framing headers are filtered before provider headers are overlaid.
    pub incoming_headers: HeaderMap,
    /// Caller request identity forwarded only through the canonical request
    /// ID header when present; generated IDs stay bounded and secret-free.
    pub request_id: Option<String>,
    pub correlation_id: Option<String>,
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
    #[error("injected crash fault at {point:?}")]
    Injected { point: CrashFaultPoint },
}

#[derive(Debug)]
pub struct UpstreamResponseEvidence {
    pub status: StatusCode,
    pub headers: HeaderMap,
    pub body: crate::providers::ProviderBody,
    pub upstream_request_id: Option<String>,
    pub headers_elapsed: Duration,
    pub bytes_observed: usize,
}

#[derive(Clone, Debug)]
pub struct AttemptBuilder {
    clients: ProviderClientPool,
    wire: WireRuntime,
    fault_injector: Option<CoordinatorFaultInjector>,
}

impl AttemptBuilder {
    pub fn new(clients: ProviderClientPool, wire: WireRuntime) -> Self {
        Self {
            clients,
            wire,
            fault_injector: None,
        }
    }

    /// Attach a test-only crash fault injector for provider-send
    /// boundaries. `None` (the default) keeps submission unchanged.
    pub fn with_fault_injector(mut self, injector: CoordinatorFaultInjector) -> Self {
        self.fault_injector = Some(injector);
        self
    }

    pub fn prepare(&self, input: AttemptInput) -> Result<PreparedUpstreamAttempt, AttemptError> {
        validate_input(&input)?;
        self.prepare_with_admission(input, None)
    }

    /// Prepare an attempt from the admission result already used to build
    /// routing facts.  This keeps request decoding at the M6 boundary exactly
    /// once on the finite coordinator path.
    pub fn prepare_admitted(
        &self,
        input: AttemptInput,
        admission: AdmittedRequest,
    ) -> Result<PreparedUpstreamAttempt, AttemptError> {
        validate_input(&input)?;
        self.prepare_with_admission(input, Some(admission))
    }

    fn prepare_with_admission(
        &self,
        input: AttemptInput,
        admission: Option<AdmittedRequest>,
    ) -> Result<PreparedUpstreamAttempt, AttemptError> {
        let mut context = WireRuntimeContext::new(
            input.client_surface,
            input.profile.clone(),
            input.identity.model_id.clone(),
            input.identity.upstream_model_id.clone(),
        );
        context.provider_id = Some(input.identity.provider_id.clone());
        context.provider_kind = input.provider.kind.clone();
        let prepared = match admission {
            Some(admission) => {
                self.wire
                    .prepare_admitted_request(admission, &input.raw_body, &context)?
            }
            None => self.wire.prepare_request(&input.raw_body, &context)?,
        };
        let path_template = if input.stream {
            input
                .profile
                .stream_path_template
                .as_deref()
                .unwrap_or(&input.profile.path_template)
        } else {
            &input.profile.path_template
        };
        let path = expand_path(path_template, &input.identity.upstream_model_id)?;
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
        add_forwarded_headers(&mut headers, &input.incoming_headers)?;
        add_request_identity_headers(
            &mut headers,
            input.request_id.as_deref().or_else(|| {
                (!input.identity.proxy_request_id.is_empty())
                    .then_some(input.identity.proxy_request_id.as_str())
            }),
            input.correlation_id.as_deref(),
        )?;
        add_static_headers(&mut headers, &input.provider.headers)?;
        if let Some(surface) = input
            .provider
            .wire_surfaces
            .get(input.profile.definition.surface.as_str())
        {
            add_static_headers(&mut headers, &surface.headers)?;
            add_auth_header(
                &mut headers,
                surface.auth.as_ref().unwrap_or(&input.provider.auth),
                input.account_api_key.as_deref(),
            )?;
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
            upstream_model_id: identity.upstream_model_id.clone(),
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
        if let Some(injector) = self.fault_injector.as_ref() {
            injector.pause_at(CrashFaultPoint::ProviderSendStartBefore);
            if injector.should_fail(CrashFaultPoint::ProviderSendStartBefore) {
                return Err(AttemptError::Injected {
                    point: CrashFaultPoint::ProviderSendStartBefore,
                });
            }
        }
        let client = self
            .clients
            .get_client(&attempt.provider_id, Some(&attempt.account_name))?;
        let started = Instant::now();
        let response: ProviderResponse = client
            .send(attempt.method, &attempt.path, attempt.headers, attempt.body)
            .await?;
        if let Some(injector) = self.fault_injector.as_ref() {
            injector.pause_at(CrashFaultPoint::ProviderHeaderReceiptAfter);
            if injector.should_fail(CrashFaultPoint::ProviderHeaderReceiptAfter) {
                return Err(AttemptError::Injected {
                    point: CrashFaultPoint::ProviderHeaderReceiptAfter,
                });
            }
        }
        let upstream_request_id = [
            "x-request-id",
            "request-id",
            "anthropic-request-id",
            "x-amzn-requestid",
        ]
        .iter()
        .find_map(|name| response.headers.get(*name))
        .and_then(|value| value.to_str().ok())
        .map(|value| value.chars().take(128).collect());
        Ok(UpstreamResponseEvidence {
            status: response.status,
            headers: response.headers,
            body: response.body,
            upstream_request_id,
            headers_elapsed: started.elapsed(),
            bytes_observed: 0,
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

const HOP_BY_HOP_HEADERS: &[&str] = &[
    "connection",
    "keep-alive",
    "proxy-connection",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
];

const LOCAL_HEADERS: &[&str] = &[
    "authorization",
    "proxy-authorization",
    "x-api-key",
    "host",
    "content-length",
    "x-eggpool-route-session",
];

fn add_forwarded_headers(
    headers: &mut HeaderMap,
    incoming: &HeaderMap,
) -> Result<(), AttemptError> {
    let connection_tokens = incoming
        .get_all(http::header::CONNECTION)
        .iter()
        .filter_map(|value| value.to_str().ok())
        .flat_map(|value| value.split(','))
        .map(|value| value.trim().to_ascii_lowercase())
        .collect::<std::collections::BTreeSet<_>>();
    for (name, value) in incoming {
        let lower = name.as_str().to_ascii_lowercase();
        if HOP_BY_HOP_HEADERS.contains(&lower.as_str())
            || LOCAL_HEADERS.contains(&lower.as_str())
            || connection_tokens.contains(&lower)
        {
            continue;
        }
        headers.insert(name.clone(), value.clone());
    }
    Ok(())
}

fn add_request_identity_headers(
    headers: &mut HeaderMap,
    request_id: Option<&str>,
    correlation_id: Option<&str>,
) -> Result<(), AttemptError> {
    for (name, value) in [
        ("x-request-id", request_id),
        ("x-correlation-id", correlation_id),
    ] {
        let Some(value) = value.filter(|value| !value.is_empty()) else {
            continue;
        };
        let value = value
            .get(..value.len().min(128))
            .ok_or_else(|| AttemptError::InvalidInput("request ID is not UTF-8 bounded".into()))?;
        headers.insert(
            HeaderName::from_bytes(name.as_bytes())
                .map_err(|_| AttemptError::InvalidInput("invalid request ID header".into()))?,
            HeaderValue::try_from(value)
                .map_err(|_| AttemptError::InvalidInput("invalid request ID value".into()))?,
        );
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
        let raw = match (value.value.as_deref(), value.value_env.as_deref()) {
            (Some(value), _) => Some(value.to_owned()),
            (None, Some(environment)) => std::env::var(environment).ok(),
            (None, None) => None,
        };
        let Some(raw) = raw.as_deref() else { continue };
        if raw
            .chars()
            .any(|character| matches!(character, '\r' | '\n' | '\0'))
        {
            return Err(AttemptError::InvalidInput(
                "provider header value contains a control character".into(),
            ));
        }
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
        if let Some(key) = key.filter(|value| !value.is_empty()) {
            for additional in &auth.additional {
                add_auth_header_value(
                    headers,
                    &additional.header,
                    &additional.mode,
                    &additional.scheme,
                    key,
                )?;
            }
        }
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
    for additional in &auth.additional {
        add_auth_header_value(
            headers,
            &additional.header,
            &additional.mode,
            &additional.scheme,
            key,
        )?;
    }
    Ok(())
}

fn add_auth_header_value(
    headers: &mut HeaderMap,
    header: &str,
    mode: &str,
    scheme: &str,
    key: &str,
) -> Result<(), AttemptError> {
    if mode.eq_ignore_ascii_case("none") {
        return Ok(());
    }
    let value = if scheme.is_empty() {
        key.to_owned()
    } else {
        format!("{} {key}", scheme)
    };
    let name = HeaderName::try_from(header)
        .map_err(|_| AttemptError::InvalidInput("invalid provider auth header".into()))?;
    let value = HeaderValue::try_from(value)
        .map_err(|_| AttemptError::InvalidInput("invalid provider auth value".into()))?;
    headers.insert(name, value);
    Ok(())
}
