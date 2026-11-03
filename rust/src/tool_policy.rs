//! Default-deny authorization for the optional local Eggsearch tool backend.
//!
//! Each authorization reads current credential, user, assignment, and grant
//! state on the serialized database connection. A committed revocation is
//! therefore visible to every subsequently admitted tool operation.

use thiserror::Error;
use tokio_rusqlite::rusqlite::OptionalExtension;

use crate::{
    db::Database,
    identity::{AuthenticatedPrincipal, PrincipalKind},
};

pub const BACKEND_ID: &str = "eggsearch";
pub const DISCOVERY_SCOPE: &str = "tools:discover:eggsearch";
pub const INVOCATION_SCOPE: &str = "tools:invoke:eggsearch";

pub const KNOWN_TOOLS: &[&str] = &[
    "web_search",
    "web_fetch",
    "batch_fetch",
    "provider_status",
    "repo_search",
    "repo_fetch",
    "repo_map",
    "security_search",
    "research_search",
    "build_evidence_bundle",
];

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ToolAction {
    Discover,
    Invoke,
}

impl ToolAction {
    fn required_scope(self) -> &'static str {
        match self {
            Self::Discover => DISCOVERY_SCOPE,
            Self::Invoke => INVOCATION_SCOPE,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GrantEffect {
    Allow,
    Deny,
}

impl GrantEffect {
    fn as_str(self) -> &'static str {
        match self {
            Self::Allow => "allow",
            Self::Deny => "deny",
        }
    }
}

#[derive(Debug, Error)]
pub enum ToolPolicyError {
    #[error("tool policy input is invalid")]
    InvalidInput,
    #[error("tool policy target was not found")]
    NotFound,
    #[error("tool profile changed since it was read; refresh and retry")]
    VersionConflict,
    #[error("tool policy database operation failed")]
    Database(#[from] crate::db::DatabaseError),
}

#[derive(Debug, Clone)]
pub struct ToolPolicyService {
    database: Database,
}

impl ToolPolicyService {
    pub fn new(database: Database) -> Self {
        Self { database }
    }

    /// Authorize a single exact backend/tool/action tuple. False is the only
    /// result for malformed names, non-managed principals, stale credentials,
    /// missing scopes, and absent grants.
    pub async fn authorize_tool(
        &self,
        principal: &AuthenticatedPrincipal,
        backend: &str,
        tool: &str,
        action: ToolAction,
    ) -> Result<bool, ToolPolicyError> {
        if backend != BACKEND_ID || !KNOWN_TOOLS.contains(&tool) {
            return Ok(false);
        }
        let (Some(user_id), Some(credential_id)) =
            (principal.user_id.clone(), principal.credential_id.clone())
        else {
            return Ok(false);
        };
        if principal.kind != PrincipalKind::AgentToken {
            return Ok(false);
        }
        let backend = backend.to_owned();
        let tool = tool.to_owned();
        let required_scope = action.required_scope().to_owned();
        let expected_auth_epoch = principal.auth_epoch;
        self.database
            .call(move |connection| {
                let live: Option<(String, String, String, i64)> = connection
                    .query_row(
                        "SELECT c.user_id, c.scopes, u.state, u.auth_epoch \
                         FROM user_credentials c JOIN users u ON u.id = c.user_id \
                         WHERE c.id = ?1 AND c.revoked_at IS NULL \
                           AND (c.expires_at IS NULL OR c.expires_at > CURRENT_TIMESTAMP)",
                        [&credential_id],
                        |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
                    )
                    .optional()?;
                let Some((stored_user, scopes, state, auth_epoch)) = live else {
                    return Ok(false);
                };
                if stored_user != user_id
                    || state != "active"
                    || expected_auth_epoch != Some(auth_epoch)
                    || !scopes
                        .split(',')
                        .map(str::trim)
                        .any(|scope| scope == required_scope)
                {
                    return Ok(false);
                }

                let denied: bool = connection.query_row(
                    "SELECT EXISTS(SELECT 1 FROM instance_tool_denials \
                     WHERE backend_id = ?1 AND tool_name = ?2)",
                    (&backend, &tool),
                    |row| row.get(0),
                )?;
                if denied {
                    return Ok(false);
                }

                let override_effect: Option<String> = connection
                    .query_row(
                        "SELECT effect FROM user_tool_overrides \
                         WHERE user_id = ?1 AND backend_id = ?2 AND tool_name = ?3",
                        (&user_id, &backend, &tool),
                        |row| row.get(0),
                    )
                    .optional()?;
                if let Some(effect) = override_effect {
                    return Ok(effect == "allow");
                }

                let profile_id: Option<String> = connection
                    .query_row(
                        "SELECT profile_id FROM user_profile_assignments WHERE user_id = ?1",
                        [&user_id],
                        |row| row.get(0),
                    )
                    .optional()?;
                let Some(profile_id) = profile_id else {
                    return Ok(false);
                };
                let profile_effect: Option<String> = connection
                    .query_row(
                        "SELECT effect FROM profile_tool_rules \
                         WHERE profile_id = ?1 AND backend_id = ?2 AND tool_name = ?3",
                        (&profile_id, &backend, &tool),
                        |row| row.get(0),
                    )
                    .optional()?;
                Ok(profile_effect.as_deref() == Some("allow"))
            })
            .await
            .map_err(ToolPolicyError::Database)
    }

    pub async fn set_profile_rule(
        &self,
        profile_id: &str,
        tool: &str,
        effect: Option<GrantEffect>,
        expected_version: Option<i64>,
        actor_id: &str,
    ) -> Result<(), ToolPolicyError> {
        validate_tool(tool)?;
        validate_actor(actor_id)?;
        let profile_id = validate_id(profile_id)?;
        let tool = tool.to_owned();
        let actor_id = actor_id.to_owned();
        let effect = effect.map(GrantEffect::as_str);
        let audit_target = format!("{profile_id}:{BACKEND_ID}:{tool}");
        self.database.with_transaction(move |connection| {
            ensure_admin(connection, &actor_id)?;
            let version: Option<i64> = connection.query_row(
                "SELECT version FROM policy_profiles WHERE id = ?1",
                [&profile_id], |row| row.get(0))?;
            let Some(version) = version else { return Err(tokio_rusqlite::rusqlite::Error::QueryReturnedNoRows); };
            if expected_version.is_some_and(|expected| expected != version) {
                return Err(tokio_rusqlite::rusqlite::Error::InvalidQuery);
            }
            if let Some(effect) = effect {
                connection.execute(
                    "INSERT INTO profile_tool_rules(profile_id, backend_id, tool_name, effect, created_by, updated_by) \
                     VALUES (?1, ?2, ?3, ?4, ?5, ?5) \
                     ON CONFLICT(profile_id, backend_id, tool_name) DO UPDATE SET effect = excluded.effect, updated_by = excluded.updated_by, updated_at = CURRENT_TIMESTAMP",
                    (&profile_id, BACKEND_ID, &tool, effect, &actor_id))?;
            } else {
                connection.execute("DELETE FROM profile_tool_rules WHERE profile_id = ?1 AND backend_id = ?2 AND tool_name = ?3", (&profile_id, BACKEND_ID, &tool))?;
            }
            connection.execute(
                "INSERT INTO user_admin_audit(actor_id, action, target_kind, target_id) VALUES (?1, 'set_tool_profile_rule', 'profile_tool', ?2)",
                (&actor_id, &audit_target))?;
            Ok(())
        }).await.map_err(map_mutation_error)
    }

    pub async fn set_user_rule(
        &self,
        username: &str,
        tool: &str,
        effect: Option<GrantEffect>,
        actor_id: &str,
    ) -> Result<(), ToolPolicyError> {
        validate_tool(tool)?;
        validate_actor(actor_id)?;
        let username = validate_id(username)?.to_ascii_lowercase();
        let tool = tool.to_owned();
        let actor_id = actor_id.to_owned();
        let effect = effect.map(GrantEffect::as_str);
        self.database.with_transaction(move |connection| {
            ensure_admin(connection, &actor_id)?;
            let user_id: Option<String> = connection.query_row(
                "SELECT id FROM users WHERE normalized_username = ?1",
                [&username], |row| row.get(0)).optional()?;
            let Some(user_id) = user_id else { return Err(tokio_rusqlite::rusqlite::Error::QueryReturnedNoRows); };
            let audit_target = format!("{user_id}:{BACKEND_ID}:{tool}");
            if let Some(effect) = effect {
                connection.execute(
                    "INSERT INTO user_tool_overrides(user_id, backend_id, tool_name, effect, updated_by) VALUES (?1, ?2, ?3, ?4, ?5) \
                     ON CONFLICT(user_id, backend_id, tool_name) DO UPDATE SET effect = excluded.effect, updated_by = excluded.updated_by, updated_at = CURRENT_TIMESTAMP",
                    (&user_id, BACKEND_ID, &tool, effect, &actor_id))?;
            } else {
                connection.execute(
                    "DELETE FROM user_tool_overrides WHERE user_id = ?1 AND backend_id = ?2 AND tool_name = ?3",
                    (&user_id, BACKEND_ID, &tool))?;
            }
            connection.execute(
                "INSERT INTO user_admin_audit(actor_id, action, target_kind, target_id) VALUES (?1, 'set_user_tool_rule', 'user_tool', ?2)",
                (&actor_id, &audit_target))?;
            Ok(())
        }).await.map_err(map_mutation_error)
    }

    pub async fn set_instance_denial(
        &self,
        tool: &str,
        denied: bool,
        actor_id: &str,
    ) -> Result<(), ToolPolicyError> {
        validate_tool(tool)?;
        validate_actor(actor_id)?;
        let tool = tool.to_owned();
        let actor_id = actor_id.to_owned();
        self.database.with_transaction(move |connection| {
            ensure_admin(connection, &actor_id)?;
            if denied {
                connection.execute(
                    "INSERT INTO instance_tool_denials(backend_id, tool_name, created_by) VALUES (?1, ?2, ?3) ON CONFLICT(backend_id, tool_name) DO NOTHING",
                    (BACKEND_ID, &tool, &actor_id))?;
            } else {
                connection.execute("DELETE FROM instance_tool_denials WHERE backend_id = ?1 AND tool_name = ?2", (BACKEND_ID, &tool))?;
            }
            connection.execute(
                "INSERT INTO user_admin_audit(actor_id, action, target_kind, target_id) VALUES (?1, 'set_instance_tool_denial', 'backend_tool', ?2)",
                (&actor_id, &tool))?;
            Ok(())
        }).await.map_err(map_mutation_error)
    }

    pub async fn effective_tools_for_user(
        &self,
        username: &str,
    ) -> Result<Vec<String>, ToolPolicyError> {
        let username = validate_id(username)?.to_ascii_lowercase();
        self.database.call(move |connection| {
            let user: Option<(String, Option<String>)> = connection.query_row(
                "SELECT u.id, a.profile_id FROM users u LEFT JOIN user_profile_assignments a ON a.user_id = u.id WHERE u.normalized_username = ?1 AND u.state = 'active'",
                [&username], |row| Ok((row.get(0)?, row.get(1)?))).optional()?;
            let Some((user_id, profile_id)) = user else { return Ok(Vec::new()); };
            let mut allowed = Vec::new();
            for tool in KNOWN_TOOLS {
                let denied: bool = connection.query_row(
                    "SELECT EXISTS(SELECT 1 FROM instance_tool_denials WHERE backend_id = ?1 AND tool_name = ?2)",
                    (BACKEND_ID, tool), |row| row.get(0))?;
                if denied { continue; }
                let override_effect: Option<String> = connection.query_row(
                    "SELECT effect FROM user_tool_overrides WHERE user_id = ?1 AND backend_id = ?2 AND tool_name = ?3",
                    (&user_id, BACKEND_ID, tool), |row| row.get(0)).optional()?;
                if let Some(effect) = override_effect {
                    if effect == "allow" { allowed.push((*tool).to_owned()); }
                    continue;
                }
                if let Some(profile_id) = profile_id.as_ref() {
                    let profile_effect: Option<String> = connection.query_row(
                        "SELECT effect FROM profile_tool_rules WHERE profile_id = ?1 AND backend_id = ?2 AND tool_name = ?3",
                        (profile_id, BACKEND_ID, tool), |row| row.get(0)).optional()?;
                    if profile_effect.as_deref() == Some("allow") { allowed.push((*tool).to_owned()); }
                }
            }
            Ok(allowed)
        }).await.map_err(ToolPolicyError::Database)
    }

    pub async fn assigned_profile_version(
        &self,
        username: &str,
    ) -> Result<Option<(String, i64)>, ToolPolicyError> {
        let username = validate_id(username)?.to_ascii_lowercase();
        self.database
            .call(move |connection| {
                connection
                    .query_row(
                        "SELECT p.id, p.version FROM users u \
                         JOIN user_profile_assignments a ON a.user_id = u.id \
                         JOIN policy_profiles p ON p.id = a.profile_id \
                         WHERE u.normalized_username = ?1 AND u.state = 'active'",
                        [&username],
                        |row| Ok((row.get(0)?, row.get(1)?)),
                    )
                    .optional()
            })
            .await
            .map_err(ToolPolicyError::Database)
    }
}

fn ensure_admin(
    connection: &mut tokio_rusqlite::rusqlite::Connection,
    actor_id: &str,
) -> Result<(), tokio_rusqlite::rusqlite::Error> {
    let admin: bool = connection.query_row(
        "SELECT EXISTS(SELECT 1 FROM users WHERE id = ?1 AND role = 'admin' AND state = 'active')",
        [actor_id],
        |row| row.get(0),
    )?;
    if admin {
        Ok(())
    } else {
        Err(tokio_rusqlite::rusqlite::Error::QueryReturnedNoRows)
    }
}

fn validate_tool(tool: &str) -> Result<(), ToolPolicyError> {
    if KNOWN_TOOLS.contains(&tool) {
        Ok(())
    } else {
        Err(ToolPolicyError::InvalidInput)
    }
}

fn validate_actor(actor: &str) -> Result<(), ToolPolicyError> {
    if !actor.is_empty() && actor.len() <= 128 {
        Ok(())
    } else {
        Err(ToolPolicyError::InvalidInput)
    }
}

fn validate_id(value: &str) -> Result<String, ToolPolicyError> {
    let trimmed = value.trim();
    if trimmed.is_empty() || trimmed.len() > 128 || !trimmed.is_ascii() {
        Err(ToolPolicyError::InvalidInput)
    } else {
        Ok(trimmed.to_owned())
    }
}

fn map_mutation_error(error: crate::db::DatabaseError) -> ToolPolicyError {
    match error {
        crate::db::DatabaseError::Transaction { ref source }
            if matches!(
                source.as_ref(),
                tokio_rusqlite::rusqlite::Error::QueryReturnedNoRows
            ) =>
        {
            ToolPolicyError::NotFound
        }
        crate::db::DatabaseError::Sqlite { ref source, .. }
            if matches!(
                source.as_ref(),
                tokio_rusqlite::rusqlite::Error::QueryReturnedNoRows
            ) =>
        {
            ToolPolicyError::NotFound
        }
        crate::db::DatabaseError::Transaction { ref source }
            if matches!(
                source.as_ref(),
                tokio_rusqlite::rusqlite::Error::InvalidQuery
            ) =>
        {
            ToolPolicyError::VersionConflict
        }
        crate::db::DatabaseError::Sqlite { ref source, .. }
            if matches!(
                source.as_ref(),
                tokio_rusqlite::rusqlite::Error::InvalidQuery
            ) =>
        {
            ToolPolicyError::VersionConflict
        }
        other => ToolPolicyError::Database(other),
    }
}

pub async fn profile_version(
    database: &Database,
    profile_id: &str,
) -> Result<i64, ToolPolicyError> {
    let profile_id = validate_id(profile_id)?;
    database
        .call(move |connection| {
            connection.query_row(
                "SELECT version FROM policy_profiles WHERE id = ?1",
                [&profile_id],
                |row| row.get(0),
            )
        })
        .await
        .map_err(ToolPolicyError::Database)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn catalog_names_are_unique_ascii_exact_ids() {
        let names: std::collections::BTreeSet<_> = KNOWN_TOOLS.iter().copied().collect();
        assert_eq!(names.len(), KNOWN_TOOLS.len());
        assert!(
            KNOWN_TOOLS
                .iter()
                .all(|name| name.is_ascii() && !name.is_empty())
        );
        assert_eq!(
            KNOWN_TOOLS,
            &[
                "web_search",
                "web_fetch",
                "batch_fetch",
                "provider_status",
                "repo_search",
                "repo_fetch",
                "repo_map",
                "security_search",
                "research_search",
                "build_evidence_bundle",
            ]
        );
        assert!(!KNOWN_TOOLS.contains(&"WEB_SEARCH"));
        assert!(!KNOWN_TOOLS.contains(&"web_search\u{212a}"));
    }
}
