//! Member-only one-time provider-agent enrollment and revocation routes.
//!
//! These adapters validate the browser boundary and delegate credential,
//! binding, and file publication to the existing owner-bound mutation service.

use std::{
    net::{IpAddr, Ipv4Addr, SocketAddr},
    path::PathBuf,
    time::Duration,
};

use axum::{
    Json,
    extract::{Extension, Form, State, rejection::FormRejection},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
};
use serde::Deserialize;
use zeroize::Zeroizing;

use crate::{
    agents::{EnrollmentRegistryError, EnrollmentTicket, TicketError},
    config::{AccountAuthMode, AccountConfig, AgentAccountConfig},
    personal_upstream::{self, GrantEffect, PersonalUpstreamError},
};

pub(super) const MAX_FORM_BYTES: usize = 4 * 1024;
const MAX_AGENT_TUNNEL_START_ATTEMPTS: usize = 3;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct EnrollForm {
    provider: String,
    account_name: String,
    ticket: String,
    csrf: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct RevokeForm {
    provider: String,
    account_name: String,
    csrf: String,
}

pub(super) async fn enroll(
    State(state): State<super::AppState>,
    Extension(session): Extension<crate::web_session::Principal>,
    headers: HeaderMap,
    form: Result<Form<EnrollForm>, FormRejection>,
) -> Response {
    let Form(form) = match form {
        Ok(form) => form,
        Err(_) => return error_response(StatusCode::BAD_REQUEST, "request_invalid"),
    };
    if !csrf_matches(&session, &headers, &form.csrf) {
        csrf_rejected(&state, &session);
        return error_response(StatusCode::FORBIDDEN, "csrf_invalid");
    }
    if let Some(error) = self_service_role_error(&session) {
        let (status, code) = upstream_error_response(&error);
        return enrollment_error(
            &state,
            &session,
            status,
            code,
            crate::security_events::SecurityEventOutcome::Denied,
        );
    }
    if !session_is_current(&state, &session).await {
        return enrollment_error(
            &state,
            &session,
            StatusCode::FORBIDDEN,
            "session_changed",
            crate::security_events::SecurityEventOutcome::Denied,
        );
    }

    let lease = match state.runtime.acquire().await {
        Ok(lease) => lease,
        Err(_) => {
            return enrollment_error(
                &state,
                &session,
                StatusCode::SERVICE_UNAVAILABLE,
                "agent_unavailable",
                crate::security_events::SecurityEventOutcome::Refused,
            );
        }
    };
    let config = lease.generation().config();
    if !config.agents.enabled {
        return enrollment_error(
            &state,
            &session,
            StatusCode::FORBIDDEN,
            "agents_disabled",
            crate::security_events::SecurityEventOutcome::Denied,
        );
    }
    match personal_upstream::effective_grant(&state.database, &session.user_id).await {
        Ok(GrantEffect::Allow) => {}
        Ok(GrantEffect::Deny | GrantEffect::Inherit) | Err(_) => {
            return enrollment_error(
                &state,
                &session,
                StatusCode::FORBIDDEN,
                "self_service_not_granted",
                crate::security_events::SecurityEventOutcome::Denied,
            );
        }
    }
    if !valid_identifier(&form.account_name) {
        return enrollment_error(
            &state,
            &session,
            StatusCode::BAD_REQUEST,
            "account_name_invalid",
            crate::security_events::SecurityEventOutcome::Refused,
        );
    }
    if !valid_identifier(&form.provider)
        || crate::provider_profile::bundled_profile(&form.provider).is_err()
    {
        return enrollment_error(
            &state,
            &session,
            StatusCode::BAD_REQUEST,
            "provider_invalid",
            crate::security_events::SecurityEventOutcome::Refused,
        );
    }
    if config.providers.values().any(|provider| {
        provider
            .accounts
            .iter()
            .any(|account| account.name == form.account_name)
    }) {
        return enrollment_error(
            &state,
            &session,
            StatusCode::CONFLICT,
            "account_conflict",
            crate::security_events::SecurityEventOutcome::Denied,
        );
    }
    let Some(config_path) = state.config_path().map(PathBuf::from) else {
        return enrollment_error(
            &state,
            &session,
            StatusCode::SERVICE_UNAVAILABLE,
            "configuration_unavailable",
            crate::security_events::SecurityEventOutcome::Refused,
        );
    };
    let Some(tls) = config.agents.tls.clone() else {
        return enrollment_error(
            &state,
            &session,
            StatusCode::SERVICE_UNAVAILABLE,
            "agent_tls_unavailable",
            crate::security_events::SecurityEventOutcome::Refused,
        );
    };
    // The ticket string itself contains the adopted transport credential.
    // Zeroize the form copy as soon as the decoder has taken ownership.
    let ticket_input = Zeroizing::new(form.ticket);
    let now = current_unix_time();
    let ticket = match EnrollmentTicket::decode(ticket_input.as_str(), now) {
        Ok(ticket) => ticket,
        Err(error) => {
            let (status, code) = ticket_error_response(&error);
            return enrollment_error(
                &state,
                &session,
                status,
                code,
                crate::security_events::SecurityEventOutcome::Refused,
            );
        }
    };
    let control = match ticket.validate_operator_address(&config.agents) {
        Ok(address) => address,
        Err(error) => {
            let (status, code) = ticket_error_response(&error);
            return enrollment_error(
                &state,
                &session,
                status,
                code,
                crate::security_events::SecurityEventOutcome::Refused,
            );
        }
    };
    if !ticket.providers.is_empty()
        && !ticket
            .providers
            .iter()
            .any(|provider| provider == &form.provider)
    {
        return enrollment_error(
            &state,
            &session,
            StatusCode::UNPROCESSABLE_ENTITY,
            "provider_not_permitted",
            crate::security_events::SecurityEventOutcome::Denied,
        );
    }
    if let Err(error) = ticket.verify_server_ca(tls.server_ca_path.as_deref()) {
        let (status, code) = ticket_error_response(&error);
        return enrollment_error(
            &state,
            &session,
            status,
            code,
            crate::security_events::SecurityEventOutcome::Refused,
        );
    }
    // CA verification reads operator-controlled material and may take time.
    // Revalidate before reserving the one-time ticket or opening a listener.
    if !session_is_current(&state, &session).await {
        return enrollment_error(
            &state,
            &session,
            StatusCode::FORBIDDEN,
            "session_changed",
            crate::security_events::SecurityEventOutcome::Denied,
        );
    }
    if !ticket.has_safe_display_label() {
        return enrollment_error(
            &state,
            &session,
            StatusCode::UNPROCESSABLE_ENTITY,
            "ticket_optional_field_invalid",
            crate::security_events::SecurityEventOutcome::Refused,
        );
    }
    let Some(process) = state.process.as_ref() else {
        return enrollment_error(
            &state,
            &session,
            StatusCode::SERVICE_UNAVAILABLE,
            "agent_unavailable",
            crate::security_events::SecurityEventOutcome::Refused,
        );
    };
    let agent_tunnels = process.agent_tunnel_manager();
    let ticket_digest = ticket.ticket_digest();
    match personal_upstream::agent_ticket_was_consumed(
        &state.database,
        ticket_digest,
        current_unix_time(),
    )
    .await
    {
        Ok(false) => {}
        Ok(true) => {
            return enrollment_error(
                &state,
                &session,
                StatusCode::CONFLICT,
                "ticket_replayed",
                crate::security_events::SecurityEventOutcome::Denied,
            );
        }
        Err(_) => {
            return enrollment_error(
                &state,
                &session,
                StatusCode::SERVICE_UNAVAILABLE,
                "agent_database_failed",
                crate::security_events::SecurityEventOutcome::Refused,
            );
        }
    }
    let reservation = match state.agent_enrollments.reserve(
        ticket_input.as_str(),
        &session.session_id,
        &session.user_id,
        ticket.remaining_lifetime(current_unix_time()),
    ) {
        Ok(reservation) => reservation,
        Err(EnrollmentRegistryError::Replay) => {
            return enrollment_error(
                &state,
                &session,
                StatusCode::CONFLICT,
                "ticket_replayed",
                crate::security_events::SecurityEventOutcome::Denied,
            );
        }
        Err(EnrollmentRegistryError::Capacity) => {
            return enrollment_error(
                &state,
                &session,
                StatusCode::TOO_MANY_REQUESTS,
                "enrollment_capacity",
                crate::security_events::SecurityEventOutcome::Throttled,
            );
        }
        Err(EnrollmentRegistryError::Invalid) => {
            return enrollment_error(
                &state,
                &session,
                StatusCode::CONFLICT,
                "ticket_lifetime_invalid",
                crate::security_events::SecurityEventOutcome::Refused,
            );
        }
    };
    let api_key_env = personal_upstream::agent_api_key_env_var(&form.provider, &form.account_name);
    let control_bind_ip = match config.agents.control_bind_host.parse::<IpAddr>() {
        Ok(ip) => ip,
        Err(_) => {
            return enrollment_error(
                &state,
                &session,
                StatusCode::SERVICE_UNAVAILABLE,
                "agent_configuration_invalid",
                crate::security_events::SecurityEventOutcome::Refused,
            );
        }
    };
    let control_bind = SocketAddr::new(control_bind_ip, control.port);
    let mut external_bind = match allocate_external_bind().await {
        Ok(bind) => bind,
        Err(_) => {
            return enrollment_error(
                &state,
                &session,
                StatusCode::SERVICE_UNAVAILABLE,
                "agent_port_unavailable",
                crate::security_events::SecurityEventOutcome::Refused,
            );
        }
    };
    let mut bootstrap_tls = tls.clone();
    bootstrap_tls.require_client_cert = false;
    bootstrap_tls.client_ca_path = None;
    let mut bootstrap_agent = AgentAccountConfig {
        control_bind: control_bind.to_string(),
        control_endpoint: Some(ticket.control.clone()),
        server_name: Some(ticket.server_name.clone()),
        external_bind: external_bind.to_string(),
        transport_secret_env: String::new(),
        tls: Some(bootstrap_tls),
        ..AgentAccountConfig::default()
    };
    let mut bootstrap_account = AccountConfig {
        name: form.account_name.clone(),
        auth_mode: AccountAuthMode::Agent,
        api_key_env: api_key_env.clone(),
        base_url: Some(format!("http://{external_bind}/v1")),
        agent: Some(bootstrap_agent.clone()),
        label: ticket.agent_label.clone(),
        ..AccountConfig::default()
    };
    let issued = match crate::agents::bootstrap::issue_client_certificate() {
        Ok(issued) => issued,
        Err(_) => {
            return enrollment_error(
                &state,
                &session,
                StatusCode::SERVICE_UNAVAILABLE,
                "agent_certificate_issue_failed",
                crate::security_events::SecurityEventOutcome::Refused,
            );
        }
    };
    let mut bootstrap_tunnel = None;
    for attempt in 1..=MAX_AGENT_TUNNEL_START_ATTEMPTS {
        if attempt > 1 {
            external_bind = match allocate_external_bind().await {
                Ok(bind) => bind,
                Err(_) => {
                    return enrollment_error(
                        &state,
                        &session,
                        StatusCode::SERVICE_UNAVAILABLE,
                        "agent_port_unavailable",
                        crate::security_events::SecurityEventOutcome::Refused,
                    );
                }
            };
            bootstrap_agent.external_bind = external_bind.to_string();
            bootstrap_account.base_url = Some(format!("http://{external_bind}/v1"));
            bootstrap_account.agent = Some(bootstrap_agent.clone());
        }
        match agent_tunnels
            .start_account(
                config,
                &form.provider,
                &bootstrap_account,
                Some(&config_path),
                crate::agents::AgentTunnelAuthMode::EnrollmentBootstrap,
                ticket.enroll_secret(),
            )
            .await
        {
            Ok(started) => {
                bootstrap_tunnel = Some(started);
                break;
            }
            Err(crate::agents::AgentTunnelError::IncompatibleGeneration { .. }) => {
                return enrollment_error(
                    &state,
                    &session,
                    StatusCode::CONFLICT,
                    "account_conflict",
                    crate::security_events::SecurityEventOutcome::Denied,
                );
            }
            Err(crate::agents::AgentTunnelError::ListenerStart { .. })
                if attempt < MAX_AGENT_TUNNEL_START_ATTEMPTS =>
            {
                tracing::warn!(
                    attempt,
                    max_attempts = MAX_AGENT_TUNNEL_START_ATTEMPTS,
                    "agent bootstrap listener failed to bind; retrying with a fresh external port"
                );
            }
            Err(_) => {
                return enrollment_error(
                    &state,
                    &session,
                    StatusCode::SERVICE_UNAVAILABLE,
                    "agent_tunnel_start_failed",
                    crate::security_events::SecurityEventOutcome::Refused,
                );
            }
        }
    }
    let Some(bootstrap_tunnel) = bootstrap_tunnel else {
        return enrollment_error(
            &state,
            &session,
            StatusCode::SERVICE_UNAVAILABLE,
            "agent_tunnel_start_failed",
            crate::security_events::SecurityEventOutcome::Refused,
        );
    };
    if !bootstrap_tunnel
        .wait_for_control_connection(&form.provider, &form.account_name, Duration::from_secs(20))
        .await
    {
        let close_report = bootstrap_tunnel.close(Duration::from_secs(5)).await;
        if close_report.shutdown_timed_out > 0 {
            reservation.retain_until_expiry();
        }
        return enrollment_error(
            &state,
            &session,
            StatusCode::SERVICE_UNAVAILABLE,
            "agent_bootstrap_timeout",
            crate::security_events::SecurityEventOutcome::Refused,
        );
    }
    if let Err(error) = crate::agents::bootstrap::deliver_enrollment_callback(
        external_bind,
        ticket.enroll_secret(),
        &issued.certificate_pem,
        issued.private_key_pem.as_str(),
    )
    .await
    {
        let close_report = bootstrap_tunnel.close(Duration::from_secs(5)).await;
        if close_report.shutdown_timed_out > 0
            || !matches!(
                error,
                crate::agents::bootstrap::BootstrapError::CallbackRejected
            )
        {
            reservation.retain_until_expiry();
        }
        return enrollment_error(
            &state,
            &session,
            StatusCode::SERVICE_UNAVAILABLE,
            "agent_callback_failed",
            crate::security_events::SecurityEventOutcome::Refused,
        );
    }
    match personal_upstream::record_consumed_agent_ticket(
        &state.database,
        ticket_digest,
        ticket.expires_at(),
        current_unix_time(),
    )
    .await
    {
        Ok(true) => reservation.commit(),
        Ok(false) => {
            reservation.commit();
            let _ = bootstrap_tunnel.close(Duration::from_secs(5)).await;
            return enrollment_error(
                &state,
                &session,
                StatusCode::CONFLICT,
                "ticket_replayed",
                crate::security_events::SecurityEventOutcome::Denied,
            );
        }
        Err(_) => {
            reservation.retain_until_expiry();
            let _ = bootstrap_tunnel.close(Duration::from_secs(5)).await;
            return enrollment_error(
                &state,
                &session,
                StatusCode::SERVICE_UNAVAILABLE,
                "agent_database_failed",
                crate::security_events::SecurityEventOutcome::Refused,
            );
        }
    }
    let close_report = bootstrap_tunnel.close(Duration::from_secs(5)).await;
    if close_report.shutdown_timed_out > 0 {
        return enrollment_error(
            &state,
            &session,
            StatusCode::SERVICE_UNAVAILABLE,
            "agent_tunnel_start_failed",
            crate::security_events::SecurityEventOutcome::Refused,
        );
    }
    let client_ca_path = match crate::operations::config_mutation::write_agent_client_ca_bundle(
        &config_path,
        &form.provider,
        &form.account_name,
        issued.ca_pem.as_bytes(),
    ) {
        Ok(path) => path,
        Err(_) => {
            return enrollment_error(
                &state,
                &session,
                StatusCode::SERVICE_UNAVAILABLE,
                "agent_configuration_failed",
                crate::security_events::SecurityEventOutcome::Refused,
            );
        }
    };
    let mut mtls_tls = tls;
    mtls_tls.client_ca_path = Some(client_ca_path.to_string_lossy().into_owned());
    mtls_tls.require_client_cert = true;
    let mut agent = AgentAccountConfig {
        control_bind: control_bind.to_string(),
        control_endpoint: Some(ticket.control.clone()),
        server_name: Some(ticket.server_name.clone()),
        external_bind: external_bind.to_string(),
        transport_secret_env: String::new(),
        tls: Some(mtls_tls),
        ..AgentAccountConfig::default()
    };
    let mut account = AccountConfig {
        name: form.account_name.clone(),
        auth_mode: AccountAuthMode::Agent,
        api_key_env: api_key_env.clone(),
        base_url: Some(format!("http://{external_bind}/v1")),
        agent: Some(agent.clone()),
        label: ticket.agent_label.clone(),
        ..AccountConfig::default()
    };
    let mut tunnel = None;
    for attempt in 1..=MAX_AGENT_TUNNEL_START_ATTEMPTS {
        if attempt > 1 {
            external_bind = match allocate_external_bind().await {
                Ok(bind) => bind,
                Err(_) => {
                    let cleanup = crate::operations::config_mutation::remove_agent_client_ca_bundle(
                        &config_path,
                        &form.provider,
                        &form.account_name,
                    );
                    return enrollment_error(
                        &state,
                        &session,
                        StatusCode::SERVICE_UNAVAILABLE,
                        if cleanup.is_ok() {
                            "agent_port_unavailable"
                        } else {
                            "operator_repair_required"
                        },
                        crate::security_events::SecurityEventOutcome::Refused,
                    );
                }
            };
            agent.external_bind = external_bind.to_string();
            account.base_url = Some(format!("http://{external_bind}/v1"));
            account.agent = Some(agent.clone());
        }
        match agent_tunnels
            .start_account(
                config,
                &form.provider,
                &account,
                Some(&config_path),
                crate::agents::AgentTunnelAuthMode::ConfiguredAccount,
                "",
            )
            .await
        {
            Ok(started) => {
                tunnel = Some(started);
                break;
            }
            Err(crate::agents::AgentTunnelError::IncompatibleGeneration { .. }) => {
                let cleanup = crate::operations::config_mutation::remove_agent_client_ca_bundle(
                    &config_path,
                    &form.provider,
                    &form.account_name,
                );
                return enrollment_error(
                    &state,
                    &session,
                    StatusCode::CONFLICT,
                    if cleanup.is_ok() {
                        "account_conflict"
                    } else {
                        "operator_repair_required"
                    },
                    crate::security_events::SecurityEventOutcome::Denied,
                );
            }
            Err(crate::agents::AgentTunnelError::ListenerStart { .. })
                if attempt < MAX_AGENT_TUNNEL_START_ATTEMPTS =>
            {
                tracing::warn!(
                    attempt,
                    max_attempts = MAX_AGENT_TUNNEL_START_ATTEMPTS,
                    "agent mTLS listener failed to bind; retrying with a fresh external port"
                );
            }
            Err(_) => {
                let cleanup = crate::operations::config_mutation::remove_agent_client_ca_bundle(
                    &config_path,
                    &form.provider,
                    &form.account_name,
                );
                return enrollment_error(
                    &state,
                    &session,
                    StatusCode::SERVICE_UNAVAILABLE,
                    if cleanup.is_ok() {
                        "agent_tunnel_start_failed"
                    } else {
                        "operator_repair_required"
                    },
                    crate::security_events::SecurityEventOutcome::Refused,
                );
            }
        }
    }
    let Some(tunnel) = tunnel else {
        let cleanup = crate::operations::config_mutation::remove_agent_client_ca_bundle(
            &config_path,
            &form.provider,
            &form.account_name,
        );
        return enrollment_error(
            &state,
            &session,
            StatusCode::SERVICE_UNAVAILABLE,
            if cleanup.is_ok() {
                "agent_tunnel_start_failed"
            } else {
                "operator_repair_required"
            },
            crate::security_events::SecurityEventOutcome::Refused,
        );
    };
    if !tunnel
        .wait_for_control_connection(&form.provider, &form.account_name, Duration::from_secs(30))
        .await
    {
        let close_report = tunnel.close(Duration::from_secs(5)).await;
        let cleanup = crate::operations::config_mutation::remove_agent_client_ca_bundle(
            &config_path,
            &form.provider,
            &form.account_name,
        );
        return enrollment_error(
            &state,
            &session,
            StatusCode::SERVICE_UNAVAILABLE,
            if close_report.shutdown_timed_out == 0 && cleanup.is_ok() {
                "agent_mtls_timeout"
            } else {
                "operator_repair_required"
            },
            crate::security_events::SecurityEventOutcome::Refused,
        );
    }
    if !session_is_current(&state, &session).await {
        let close_report = tunnel.close(Duration::from_secs(5)).await;
        let cleanup = crate::operations::config_mutation::remove_agent_client_ca_bundle(
            &config_path,
            &form.provider,
            &form.account_name,
        );
        return enrollment_error(
            &state,
            &session,
            StatusCode::FORBIDDEN,
            if close_report.shutdown_timed_out == 0 && cleanup.is_ok() {
                "session_changed"
            } else {
                "operator_repair_required"
            },
            crate::security_events::SecurityEventOutcome::Denied,
        );
    }
    let env_path = personal_upstream::personal_env_path(&config_path);
    match personal_upstream::add_personal_agent_account_publishing(
        &state.database,
        personal_upstream::PersonalAgentAccountWrite {
            actor_id: &session.user_id,
            user_id: &session.user_id,
            provider_id: &form.provider,
            account_name: &form.account_name,
            api_key_env: &api_key_env,
            api_key: ticket.api_key(),
            base_url: account.base_url.as_deref().unwrap_or_default(),
            label: account.label.as_deref(),
            agent: &agent,
            config_path: &config_path,
            env_path: &env_path,
            require_self_service: true,
        },
        tunnel,
        personal_upstream::PublishMode::Live,
    )
    .await
    {
        Ok(()) => {
            state.record_security_event(
                crate::security_events::SecurityEventCode::AgentEnrollment,
                crate::security_events::SecurityEventOutcome::Success,
                crate::security_events::SecurityPrincipalKind::BrowserSession,
                Some(&session.user_id),
            );
            Json(serde_json::json!({
                "status": "ok",
                "provider": form.provider,
                "account_name": form.account_name,
            }))
            .into_response()
        }
        Err(error) => {
            let retain_ca_bundle = matches!(&error, PersonalUpstreamError::RepairRequired);
            let (status, code) = upstream_error_response(&error);
            if !retain_ca_bundle {
                let _ = crate::operations::config_mutation::remove_agent_client_ca_bundle(
                    &config_path,
                    &form.provider,
                    &form.account_name,
                );
            }
            enrollment_error(
                &state,
                &session,
                status,
                code,
                crate::security_events::SecurityEventOutcome::Denied,
            )
        }
    }
}

pub(super) async fn revoke(
    State(state): State<super::AppState>,
    Extension(session): Extension<crate::web_session::Principal>,
    headers: HeaderMap,
    form: Result<Form<RevokeForm>, FormRejection>,
) -> Response {
    let Form(form) = match form {
        Ok(form) => form,
        Err(_) => return error_response(StatusCode::BAD_REQUEST, "request_invalid"),
    };
    if !csrf_matches(&session, &headers, &form.csrf) {
        csrf_rejected(&state, &session);
        return error_response(StatusCode::FORBIDDEN, "csrf_invalid");
    }
    if let Some(error) = self_service_role_error(&session) {
        let (status, code) = upstream_error_response(&error);
        return revocation_error(
            &state,
            &session,
            status,
            code,
            crate::security_events::SecurityEventOutcome::Denied,
        );
    }
    if !session_is_current(&state, &session).await {
        return revocation_error(
            &state,
            &session,
            StatusCode::FORBIDDEN,
            "session_changed",
            crate::security_events::SecurityEventOutcome::Denied,
        );
    }
    if !valid_identifier(&form.provider) || !valid_identifier(&form.account_name) {
        return revocation_error(
            &state,
            &session,
            StatusCode::BAD_REQUEST,
            "account_name_invalid",
            crate::security_events::SecurityEventOutcome::Refused,
        );
    }
    let lease = match state.runtime.acquire().await {
        Ok(lease) => lease,
        Err(_) => {
            return revocation_error(
                &state,
                &session,
                StatusCode::SERVICE_UNAVAILABLE,
                "agent_unavailable",
                crate::security_events::SecurityEventOutcome::Refused,
            );
        }
    };
    let Some(config_path) = state.config_path().map(PathBuf::from) else {
        return revocation_error(
            &state,
            &session,
            StatusCode::SERVICE_UNAVAILABLE,
            "configuration_unavailable",
            crate::security_events::SecurityEventOutcome::Refused,
        );
    };
    let agent_account = lease
        .generation()
        .config()
        .providers
        .get(&form.provider)
        .and_then(|provider| {
            provider
                .accounts
                .iter()
                .find(|account| account.name == form.account_name)
        })
        .filter(|account| account.auth_mode == AccountAuthMode::Agent);
    let remove_config = agent_account.is_some();
    let api_key_env = agent_account
        .map(|account| account.api_key_env.clone())
        .unwrap_or_else(|| {
            personal_upstream::agent_api_key_env_var(&form.provider, &form.account_name)
        });
    let transport_secret_env = agent_account
        .and_then(|account| account.agent.as_ref())
        .map(|agent| agent.transport_secret_env.clone())
        .unwrap_or_else(|| personal_upstream::agent_env_var(&form.provider, &form.account_name));
    // Do not hold the old generation lease during rehash: its close releases
    // the revoked tunnel after the replacement generation is published.
    drop(lease);
    let env_path = personal_upstream::personal_env_path(&config_path);
    match personal_upstream::remove_personal_agent_account_publishing(
        &state.database,
        personal_upstream::PersonalAgentAccountRevoke {
            actor_id: &session.user_id,
            user_id: &session.user_id,
            provider_id: &form.provider,
            account_name: &form.account_name,
            api_key_env: &api_key_env,
            transport_secret_env: &transport_secret_env,
            remove_config,
            config_path: &config_path,
            env_path: &env_path,
            publish: personal_upstream::PublishMode::Live,
        },
    )
    .await
    {
        Ok(()) => revocation_success(&state, &session, true),
        Err(PersonalUpstreamError::BindingNotFound) => revocation_success(&state, &session, false),
        Err(error) => {
            let (status, code) = upstream_error_response(&error);
            revocation_error(
                &state,
                &session,
                status,
                code,
                crate::security_events::SecurityEventOutcome::Denied,
            )
        }
    }
}

fn self_service_role_error(
    session: &crate::web_session::Principal,
) -> Option<PersonalUpstreamError> {
    match session.role.as_str() {
        "member" => None,
        "admin" => Some(PersonalUpstreamError::AdministratorRequired),
        _ => Some(PersonalUpstreamError::GrantDenied),
    }
}

fn csrf_matches(
    session: &crate::web_session::Principal,
    headers: &HeaderMap,
    supplied: &str,
) -> bool {
    crate::web_session::verify_csrf(session, supplied)
        && crate::web_session::csrf_cookie_value(headers)
            .is_some_and(|cookie| crate::web_session::verify_csrf(session, &cookie))
}

fn csrf_rejected(state: &super::AppState, session: &crate::web_session::Principal) {
    state.record_security_event(
        crate::security_events::SecurityEventCode::CsrfRejected,
        crate::security_events::SecurityEventOutcome::Denied,
        crate::security_events::SecurityPrincipalKind::BrowserSession,
        Some(&session.user_id),
    );
}

async fn session_is_current(
    state: &super::AppState,
    session: &crate::web_session::Principal,
) -> bool {
    let fence = crate::web_session::SessionFence::from_principal(session);
    let actor = session.user_id.clone();
    state
        .database
        .call(move |connection| fence.validates_current_session(connection, &actor))
        .await
        .is_ok_and(|valid| valid)
}

async fn allocate_external_bind() -> Result<SocketAddr, std::io::Error> {
    // Eggress accepts a socket address rather than an inherited listener. Ask
    // the OS for an unused loopback port, release the probe, then start the
    // reverse server before any account files are published.
    let listener =
        tokio::net::TcpListener::bind(SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), 0)).await?;
    let address = listener.local_addr()?;
    drop(listener);
    Ok(address)
}

fn current_unix_time() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |duration| duration.as_secs().min(i64::MAX as u64) as i64)
}

fn valid_identifier(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 96
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_'))
}

fn ticket_error_response(error: &TicketError) -> (StatusCode, &'static str) {
    match error {
        TicketError::TooLarge => (StatusCode::PAYLOAD_TOO_LARGE, "ticket_too_large"),
        TicketError::Prefix => (StatusCode::BAD_REQUEST, "ticket_prefix_invalid"),
        TicketError::Base64 => (StatusCode::BAD_REQUEST, "ticket_encoding_invalid"),
        TicketError::Malformed => (StatusCode::BAD_REQUEST, "ticket_malformed"),
        TicketError::Version => (StatusCode::BAD_REQUEST, "ticket_version_unsupported"),
        TicketError::Expiry => (StatusCode::BAD_REQUEST, "ticket_expiry_invalid"),
        TicketError::Expired => (StatusCode::CONFLICT, "ticket_expired"),
        TicketError::MissingField => (StatusCode::BAD_REQUEST, "ticket_field_missing"),
        TicketError::ControlAddress => (StatusCode::UNPROCESSABLE_ENTITY, "ticket_control_invalid"),
        TicketError::ControlEndpointMismatch => {
            (StatusCode::UNPROCESSABLE_ENTITY, "ticket_control_mismatch")
        }
        TicketError::ControlPort => (
            StatusCode::UNPROCESSABLE_ENTITY,
            "ticket_control_port_invalid",
        ),
        TicketError::LoopbackControl => {
            (StatusCode::UNPROCESSABLE_ENTITY, "ticket_control_loopback")
        }
        TicketError::ServerName => (
            StatusCode::UNPROCESSABLE_ENTITY,
            "ticket_server_name_invalid",
        ),
        TicketError::CaPin => (StatusCode::UNPROCESSABLE_ENTITY, "ticket_ca_pin_invalid"),
        TicketError::MissingCaAuthority => (
            StatusCode::SERVICE_UNAVAILABLE,
            "agent_ca_authority_missing",
        ),
        TicketError::CaMismatch => (StatusCode::UNPROCESSABLE_ENTITY, "ticket_ca_mismatch"),
        TicketError::EnrollmentSecret => (
            StatusCode::UNPROCESSABLE_ENTITY,
            "ticket_enrollment_secret_invalid",
        ),
        TicketError::OptionalField => (
            StatusCode::UNPROCESSABLE_ENTITY,
            "ticket_optional_field_invalid",
        ),
    }
}

fn upstream_error_response(error: &PersonalUpstreamError) -> (StatusCode, &'static str) {
    match error {
        PersonalUpstreamError::AdministratorRequired => {
            (StatusCode::FORBIDDEN, "administrator_not_permitted")
        }
        PersonalUpstreamError::GrantDenied => (StatusCode::FORBIDDEN, "self_service_not_granted"),
        PersonalUpstreamError::UnknownProvider => (StatusCode::BAD_REQUEST, "provider_invalid"),
        PersonalUpstreamError::NameCollision => (StatusCode::CONFLICT, "account_conflict"),
        PersonalUpstreamError::SecretRejected => (
            StatusCode::UNPROCESSABLE_ENTITY,
            "ticket_enrollment_secret_invalid",
        ),
        PersonalUpstreamError::BindingNotFound => (StatusCode::NOT_FOUND, "agent_not_found"),
        PersonalUpstreamError::PublicationRestored => {
            (StatusCode::SERVICE_UNAVAILABLE, "publication_rolled_back")
        }
        PersonalUpstreamError::RepairRequired => {
            (StatusCode::SERVICE_UNAVAILABLE, "operator_repair_required")
        }
        PersonalUpstreamError::Database(_) => {
            (StatusCode::SERVICE_UNAVAILABLE, "agent_database_failed")
        }
        PersonalUpstreamError::Configuration(_) => (
            StatusCode::SERVICE_UNAVAILABLE,
            "agent_configuration_failed",
        ),
        PersonalUpstreamError::UserNotFound => (StatusCode::FORBIDDEN, "member_unavailable"),
    }
}

fn error_response(status: StatusCode, code: &'static str) -> Response {
    (
        status,
        Json(serde_json::json!({"status": "error", "error_code": code})),
    )
        .into_response()
}

fn enrollment_error(
    state: &super::AppState,
    session: &crate::web_session::Principal,
    status: StatusCode,
    code: &'static str,
    outcome: crate::security_events::SecurityEventOutcome,
) -> Response {
    state.record_security_event(
        crate::security_events::SecurityEventCode::AgentEnrollment,
        outcome,
        crate::security_events::SecurityPrincipalKind::BrowserSession,
        Some(&session.user_id),
    );
    error_response(status, code)
}

fn revocation_error(
    state: &super::AppState,
    session: &crate::web_session::Principal,
    status: StatusCode,
    code: &'static str,
    outcome: crate::security_events::SecurityEventOutcome,
) -> Response {
    state.record_security_event(
        crate::security_events::SecurityEventCode::AgentRevocation,
        outcome,
        crate::security_events::SecurityPrincipalKind::BrowserSession,
        Some(&session.user_id),
    );
    error_response(status, code)
}

fn revocation_success(
    state: &super::AppState,
    session: &crate::web_session::Principal,
    revoked: bool,
) -> Response {
    state.record_security_event(
        crate::security_events::SecurityEventCode::AgentRevocation,
        crate::security_events::SecurityEventOutcome::Success,
        crate::security_events::SecurityPrincipalKind::BrowserSession,
        Some(&session.user_id),
    );
    Json(serde_json::json!({"status": "ok", "revoked": revoked})).into_response()
}
