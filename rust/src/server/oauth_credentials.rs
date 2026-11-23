//! Principal-bound upload of a completed OpenAI or xAI OAuth credential set.

use axum::{
    Extension, Json,
    extract::State,
    http::StatusCode,
    response::{IntoResponse, Response},
};
use serde::{Deserialize, Serialize};

use super::AppState;

pub(crate) const MAX_REQUEST_BYTES: usize = 64 * 1024;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct OAuthCredentialUpload {
    provider_id: String,
    account_name: String,
    credentials: crate::oauth::OAuthCredentials,
}

fn principal_user_id(principal: &crate::identity::AuthenticatedPrincipal) -> Option<&str> {
    if principal.kind != crate::identity::PrincipalKind::AgentToken
        || principal.role.as_deref() != Some("agent")
        || principal.scopes.len() != 1
        || !principal.has_scope(crate::identity::RequiredAgentScope::OAuthCredentialsWrite)
    {
        return None;
    }
    principal
        .user_id
        .as_deref()
        .filter(|user_id| !user_id.is_empty())
}

pub(super) async fn upload(
    State(state): State<AppState>,
    Extension(principal): Extension<crate::identity::AuthenticatedPrincipal>,
    Json(request): Json<OAuthCredentialUpload>,
) -> Response {
    let Some(user_id) = principal_user_id(&principal) else {
        return (
            StatusCode::FORBIDDEN,
            Json(serde_json::json!({"detail": {"code": "oauth_credentials_write_required"}})),
        )
            .into_response();
    };
    if !matches!(request.provider_id.as_str(), "openai" | "xai")
        || request.account_name.is_empty()
        || request.account_name.len() > 128
        || request.account_name.chars().any(char::is_control)
    {
        return (
            StatusCode::BAD_REQUEST,
            Json(serde_json::json!({"detail": {"code": "invalid_oauth_credentials_request"}})),
        )
            .into_response();
    }
    let lease = match state.runtime.acquire().await {
        Ok(lease) => lease,
        Err(_) => return oauth_unavailable(),
    };
    let Some(manager) = lease.generation().inference().oauth_account_manager() else {
        return oauth_unavailable();
    };
    match manager
        .commit_credentials_for_user(
            user_id,
            request.provider_id.as_str(),
            &request.account_name,
            request.credentials,
        )
        .await
    {
        Ok(()) => StatusCode::NO_CONTENT.into_response(),
        Err(crate::oauth::OAuthAccountError::Disabled) => (
            StatusCode::FORBIDDEN,
            Json(serde_json::json!({"detail": {"code": "oauth_disabled"}})),
        )
            .into_response(),
        Err(
            crate::oauth::OAuthAccountError::AccountNotConfigured
            | crate::oauth::OAuthAccountError::InvalidOwner,
        ) => (
            StatusCode::FORBIDDEN,
            Json(serde_json::json!({"detail": {"code": "oauth_account_not_available"}})),
        )
            .into_response(),
        Err(
            crate::oauth::OAuthAccountError::UnsupportedProvider
            | crate::oauth::OAuthAccountError::InvalidCredentials,
        ) => (
            StatusCode::BAD_REQUEST,
            Json(serde_json::json!({"detail": {"code": "invalid_oauth_credentials"}})),
        )
            .into_response(),
        Err(_) => oauth_unavailable(),
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct OAuthAuthorizationHintRequest {
    account_name: String,
}

#[derive(Serialize)]
struct OAuthAuthorizationHintResponse {
    client_id: Option<String>,
}

pub(super) async fn authorization_hint(
    State(state): State<AppState>,
    Extension(principal): Extension<crate::identity::AuthenticatedPrincipal>,
    Json(request): Json<OAuthAuthorizationHintRequest>,
) -> Response {
    let Some(user_id) = principal_user_id(&principal) else {
        return (
            StatusCode::FORBIDDEN,
            Json(serde_json::json!({"detail": {"code": "oauth_credentials_write_required"}})),
        )
            .into_response();
    };
    if request.account_name.is_empty()
        || request.account_name.len() > 128
        || request.account_name.chars().any(char::is_control)
    {
        return (
            StatusCode::BAD_REQUEST,
            Json(serde_json::json!({"detail": {"code": "invalid_oauth_credentials_request"}})),
        )
            .into_response();
    }
    let lease = match state.runtime.acquire().await {
        Ok(lease) => lease,
        Err(_) => return oauth_unavailable(),
    };
    let Some(manager) = lease.generation().inference().oauth_account_manager() else {
        return oauth_unavailable();
    };
    match manager
        .reauthorization_client_id_for_user(user_id, "openai", &request.account_name)
        .await
    {
        Ok(client_id) => Json(OAuthAuthorizationHintResponse { client_id }).into_response(),
        Err(crate::oauth::OAuthAccountError::Disabled) => (
            StatusCode::FORBIDDEN,
            Json(serde_json::json!({"detail": {"code": "oauth_disabled"}})),
        )
            .into_response(),
        Err(
            crate::oauth::OAuthAccountError::AccountNotConfigured
            | crate::oauth::OAuthAccountError::InvalidOwner,
        ) => (
            StatusCode::FORBIDDEN,
            Json(serde_json::json!({"detail": {"code": "oauth_account_not_available"}})),
        )
            .into_response(),
        Err(
            crate::oauth::OAuthAccountError::UnsupportedProvider
            | crate::oauth::OAuthAccountError::InvalidCredentials,
        ) => (
            StatusCode::BAD_REQUEST,
            Json(serde_json::json!({"detail": {"code": "invalid_oauth_credentials"}})),
        )
            .into_response(),
        Err(_) => oauth_unavailable(),
    }
}

fn oauth_unavailable() -> Response {
    (
        StatusCode::SERVICE_UNAVAILABLE,
        Json(serde_json::json!({"detail": {"code": "oauth_credentials_unavailable"}})),
    )
        .into_response()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn upload_payload() -> serde_json::Value {
        serde_json::json!({
            "provider_id": "openai",
            "account_name": "personal",
            "credentials": {
                "access_token": "fixture-access-token",
                "refresh_token": "fixture-refresh-token",
                "id_token": "fixture-id-token",
                "token_type": "Bearer",
                "scope": "openid offline_access",
                "client_id": "fixture-client-id",
                "issuer": "https://auth.openai.com",
                "expires_at": 1_900_000_000_i64,
                "obtained_at": 1_800_000_000_i64
            }
        })
    }

    #[test]
    fn upload_payload_has_no_owner_authority() {
        assert!(serde_json::from_value::<OAuthCredentialUpload>(upload_payload()).is_ok());
        for owner_field in ["user_id", "username", "owner_id"] {
            let mut payload = upload_payload();
            payload.as_object_mut().expect("object payload").insert(
                owner_field.to_owned(),
                serde_json::Value::String("other-user".to_owned()),
            );
            assert!(serde_json::from_value::<OAuthCredentialUpload>(payload).is_err());
        }
    }

    #[test]
    fn reauthorization_hint_request_has_no_owner_authority() {
        let mut payload = serde_json::json!({"account_name": "personal"});
        assert!(serde_json::from_value::<OAuthAuthorizationHintRequest>(payload.clone()).is_ok());
        payload["user_id"] = serde_json::json!("other-user");
        assert!(serde_json::from_value::<OAuthAuthorizationHintRequest>(payload).is_err());
    }

    #[test]
    fn reauthorization_hint_response_contains_only_client_id() {
        let value = serde_json::to_value(OAuthAuthorizationHintResponse {
            client_id: Some("issued-client-id".to_owned()),
        })
        .expect("serialize hint");
        assert_eq!(value, serde_json::json!({"client_id": "issued-client-id"}));
        for token_field in ["access_token", "refresh_token", "id_token"] {
            assert!(value.get(token_field).is_none());
        }
    }

    #[test]
    fn upload_requires_the_single_self_only_agent_scope() {
        let principal = crate::identity::AuthenticatedPrincipal {
            user_id: Some("owner".to_owned()),
            credential_id: Some("credential".to_owned()),
            role: Some("agent".to_owned()),
            scopes: vec![crate::identity::OAUTH_CREDENTIALS_WRITE_SCOPE.to_owned()],
            auth_epoch: Some(1),
            kind: crate::identity::PrincipalKind::AgentToken,
        };
        assert_eq!(principal_user_id(&principal), Some("owner"));

        let mut ordinary = principal.clone();
        ordinary.scopes = vec!["inference".to_owned()];
        assert_eq!(principal_user_id(&ordinary), None);
        let mut mixed = principal.clone();
        mixed.scopes.push("inference".to_owned());
        assert_eq!(principal_user_id(&mixed), None);
        let mut browser = principal;
        browser.kind = crate::identity::PrincipalKind::BrowserSession;
        assert_eq!(principal_user_id(&browser), None);
    }
}
