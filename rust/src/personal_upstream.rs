//! Secret-free personal upstream grants and bindings.
//!
//! The plaintext provider secret is written only to the config-adjacent
//! environment file. SQLite records who may select the account. Routing reads
//! a generation snapshot of those rows and never a credential.

use std::{
    collections::BTreeMap,
    path::{Path, PathBuf},
};

use sha2::{Digest, Sha256};
use thiserror::Error;
use tokio_rusqlite::rusqlite::{self, OptionalExtension};

use crate::{
    config_reload_policy::ConfigTransition,
    db::{Database, DatabaseError},
    operations::config_mutation::{
        ApplyMode, ApplyOutcome, MutationError, apply_after_mutation,
        install_personal_account_files, omit_personal_account_files,
    },
};

#[derive(Debug, Error)]
pub enum PersonalUpstreamError {
    #[error("administrator required")]
    AdministratorRequired,
    #[error("user was not found")]
    UserNotFound,
    #[error("provider is not a bundled profile")]
    UnknownProvider,
    #[error("account name is already in use")]
    NameCollision,
    #[error("upstream secret was rejected")]
    SecretRejected,
    #[error("self-service is not allowed")]
    GrantDenied,
    #[error("binding was not found")]
    BindingNotFound,
    #[error("publication failed and the previous files were restored")]
    PublicationRestored,
    #[error("the account is not routable and the files need operator repair")]
    RepairRequired,
    #[error(transparent)]
    Database(#[from] DatabaseError),
    #[error("{0}")]
    Configuration(String),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GrantEffect {
    Allow,
    Deny,
    Inherit,
}

impl GrantEffect {
    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "allow" => Some(Self::Allow),
            "deny" => Some(Self::Deny),
            "inherit" => Some(Self::Inherit),
            _ => None,
        }
    }

    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Allow => "allow",
            Self::Deny => "deny",
            Self::Inherit => "inherit",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum BindingState {
    Active(String),
    Withdrawn,
    Ambiguous,
}

/// Generation-scoped view of personal bindings. No row means the account is
/// shared. The snapshot stores no secret.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct PersonalUpstreamSnapshot {
    bindings: BTreeMap<(String, String), BindingState>,
}

impl PersonalUpstreamSnapshot {
    pub fn excludes(
        &self,
        provider_id: &str,
        account_name: &str,
        principal_user_id: Option<&str>,
    ) -> bool {
        match self
            .bindings
            .get(&(provider_id.to_owned(), account_name.to_owned()))
        {
            None => false,
            Some(BindingState::Withdrawn | BindingState::Ambiguous) => true,
            Some(BindingState::Active(owner)) => principal_user_id != Some(owner.as_str()),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PersonalBinding {
    pub provider_id: String,
    pub account_name: String,
    pub user_id: String,
    pub state: String,
    pub env_var: String,
}

pub fn upstream_env_var(provider_id: &str, account_name: &str) -> String {
    let mut hasher = Sha256::new();
    hasher.update(provider_id.as_bytes());
    hasher.update([0]);
    hasher.update(account_name.as_bytes());
    let digest = hasher.finalize();
    let hex = digest.iter().take(8).fold(String::new(), |mut text, byte| {
        text.push_str(&format!("{byte:02x}"));
        text
    });
    format!("EGGPOOL_UPSTREAM_{hex}")
}

pub fn personal_env_path(config_path: &Path) -> PathBuf {
    if let Some(explicit) = std::env::var_os("EGGPOOL_ENV") {
        return PathBuf::from(explicit);
    }
    config_path
        .parent()
        .unwrap_or_else(|| Path::new("."))
        .join(".env")
}

pub async fn instance_grant(database: &Database) -> Result<GrantEffect, PersonalUpstreamError> {
    let effect = database
        .call(|connection| {
            connection.query_row(
                "SELECT effect FROM personal_upstream_grants WHERE scope = 'instance' AND user_id = ''",
                [],
                |row| row.get::<_, String>(0),
            )
        })
        .await?;
    Ok(GrantEffect::parse(&effect).unwrap_or(GrantEffect::Deny))
}

pub async fn effective_grant(
    database: &Database,
    user_id: &str,
) -> Result<GrantEffect, PersonalUpstreamError> {
    let user_id = user_id.to_owned();
    let stored = database
        .call(move |connection| {
            let user: Option<String> = connection
                .query_row(
                    "SELECT effect FROM personal_upstream_grants WHERE scope = 'user' AND user_id = ?1",
                    [&user_id],
                    |row| row.get(0),
                )
                .optional()?;
            let instance: String = connection.query_row(
                "SELECT effect FROM personal_upstream_grants WHERE scope = 'instance' AND user_id = ''",
                [],
                |row| row.get(0),
            )?;
            Ok((user, instance))
        })
        .await?;
    let effect = match stored.0.as_deref().and_then(GrantEffect::parse) {
        Some(GrantEffect::Inherit) | None => {
            GrantEffect::parse(&stored.1).unwrap_or(GrantEffect::Deny)
        }
        Some(effect) => effect,
    };
    Ok(match effect {
        GrantEffect::Inherit => GrantEffect::Deny,
        other => other,
    })
}

pub async fn stored_user_grant(
    database: &Database,
    user_id: &str,
) -> Result<GrantEffect, PersonalUpstreamError> {
    let user_id = user_id.to_owned();
    let stored = database
        .call(move |connection| {
            connection
                .query_row(
                    "SELECT effect FROM personal_upstream_grants WHERE scope = 'user' AND user_id = ?1",
                    [&user_id],
                    |row| row.get::<_, String>(0),
                )
                .optional()
        })
        .await?;
    Ok(stored
        .as_deref()
        .and_then(GrantEffect::parse)
        .unwrap_or(GrantEffect::Inherit))
}

pub async fn set_instance_grant(
    database: &Database,
    actor_id: &str,
    effect: GrantEffect,
) -> Result<(), PersonalUpstreamError> {
    if !matches!(effect, GrantEffect::Allow | GrantEffect::Deny) {
        return Err(PersonalUpstreamError::Configuration(
            "instance grant must be allow or deny".into(),
        ));
    }
    write_grant(database, actor_id, "instance", "", effect, "instance").await
}

pub async fn set_user_grant(
    database: &Database,
    actor_id: &str,
    user_id: &str,
    effect: GrantEffect,
) -> Result<(), PersonalUpstreamError> {
    if GrantEffect::parse(effect.as_str()).is_none() {
        return Err(PersonalUpstreamError::Configuration(
            "user grant must be allow, deny, or inherit".into(),
        ));
    }
    write_grant(database, actor_id, "user", user_id, effect, user_id).await
}

async fn write_grant(
    database: &Database,
    actor_id: &str,
    scope: &str,
    user_id: &str,
    effect: GrantEffect,
    audit_target: &str,
) -> Result<(), PersonalUpstreamError> {
    let actor_id = actor_id.to_owned();
    let scope = scope.to_owned();
    let user_id = user_id.to_owned();
    let audit_target = audit_target.to_owned();
    let effect_text = effect.as_str().to_owned();
    database
        .with_transaction(move |connection| {
            if !actor_is_admin(connection, &actor_id)? {
                return Ok(Err(PersonalUpstreamError::AdministratorRequired));
            }
            if scope == "user" && !user_exists(connection, &user_id)? {
                return Ok(Err(PersonalUpstreamError::UserNotFound));
            }
            connection.execute(
                "INSERT INTO personal_upstream_grants(scope, user_id, effect, updated_by, updated_at)
                 VALUES (?1, ?2, ?3, ?4, CURRENT_TIMESTAMP)
                 ON CONFLICT(scope, user_id) DO UPDATE SET
                    effect = excluded.effect,
                    updated_by = excluded.updated_by,
                    updated_at = CURRENT_TIMESTAMP",
                (&scope, &user_id, &effect_text, &actor_id),
            )?;
            connection.execute(
                "INSERT INTO user_admin_audit(actor_id, action, target_kind, target_id)
                 VALUES (?1, 'upstream_grant', 'user', ?2)",
                (&actor_id, &audit_target),
            )?;
            Ok(Ok(()))
        })
        .await??;
    Ok(())
}

pub async fn list_bindings(
    database: &Database,
    user_id: Option<&str>,
) -> Result<Vec<PersonalBinding>, PersonalUpstreamError> {
    let user_id = user_id.map(str::to_owned);
    let rows = database
        .call(move |connection| {
            let mut statement = connection.prepare(
                "SELECT provider_id, account_name, user_id, state
                 FROM personal_upstream_bindings
                 WHERE (?1 IS NULL OR user_id = ?1)
                 ORDER BY provider_id, account_name",
            )?;
            let mapped = statement.query_map([&user_id], |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, String>(3)?,
                ))
            })?;
            mapped.collect::<Result<Vec<_>, _>>()
        })
        .await?;
    Ok(rows
        .into_iter()
        .map(|(provider_id, account_name, user_id, state)| {
            let env_var = upstream_env_var(&provider_id, &account_name);
            PersonalBinding {
                provider_id,
                account_name,
                user_id,
                state,
                env_var,
            }
        })
        .collect())
}

pub async fn load_snapshot(
    database: &Database,
) -> Result<PersonalUpstreamSnapshot, PersonalUpstreamError> {
    let rows = database
        .call(|connection| {
            let mut statement = connection.prepare(
                "SELECT provider_id, account_name, user_id, state FROM personal_upstream_bindings",
            )?;
            let mapped = statement.query_map([], |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, String>(3)?,
                ))
            })?;
            mapped.collect::<Result<Vec<_>, _>>()
        })
        .await?;
    let mut bindings = BTreeMap::new();
    for (provider_id, account_name, user_id, state) in rows {
        let key = (provider_id, account_name);
        let next = match state.as_str() {
            "active" => BindingState::Active(user_id),
            "withdrawn" => BindingState::Withdrawn,
            _ => BindingState::Ambiguous,
        };
        match bindings.get(&key) {
            Some(_) => {
                bindings.insert(key, BindingState::Ambiguous);
            }
            None => {
                bindings.insert(key, next);
            }
        }
    }
    Ok(PersonalUpstreamSnapshot { bindings })
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PublishMode {
    Live,
    Fail,
}

/// One personal-account add or remove. The secret is borrowed only for the
/// write and is never stored on the binding.
pub struct PersonalAccountWrite<'a> {
    pub actor_id: &'a str,
    pub user_id: &'a str,
    pub provider_id: &'a str,
    pub account_name: &'a str,
    pub secret: &'a str,
    pub config_path: &'a Path,
    pub env_path: &'a Path,
    pub require_self_service: bool,
}

pub async fn add_personal_account(
    database: &Database,
    write: PersonalAccountWrite<'_>,
) -> Result<(), PersonalUpstreamError> {
    add_personal_account_publishing(database, write, PublishMode::Live).await
}

pub async fn add_personal_account_publishing(
    database: &Database,
    write: PersonalAccountWrite<'_>,
    publish: PublishMode,
) -> Result<(), PersonalUpstreamError> {
    let PersonalAccountWrite {
        actor_id,
        user_id,
        provider_id,
        account_name,
        secret,
        config_path,
        env_path,
        require_self_service,
    } = write;
    validate_names(provider_id, account_name)?;
    if !bundled_provider(provider_id) {
        return Err(PersonalUpstreamError::UnknownProvider);
    }
    if !acceptable_secret(secret) {
        return Err(PersonalUpstreamError::SecretRejected);
    }
    authorize(database, actor_id, user_id, require_self_service).await?;
    if name_is_bound(database, provider_id, account_name).await? {
        return Err(PersonalUpstreamError::NameCollision);
    }
    insert_binding(
        database,
        actor_id,
        user_id,
        provider_id,
        account_name,
        "active",
        "upstream_add",
    )
    .await?;
    let env_var = upstream_env_var(provider_id, account_name);
    let published = install_personal_account_files(
        config_path,
        env_path,
        provider_id,
        account_name,
        &env_var,
        secret,
    );
    let (rollback, transition) = match published {
        Ok(value) => value,
        Err(error) => {
            let _ = delete_binding(database, provider_id, account_name).await;
            return Err(configuration_error(secret, error));
        }
    };
    if run_publish(publish, config_path, &transition)
        .await
        .is_err()
    {
        let _ = rollback.restore();
        let _ = delete_binding(database, provider_id, account_name).await;
        return Err(PersonalUpstreamError::PublicationRestored);
    }
    Ok(())
}

pub async fn remove_personal_account(
    database: &Database,
    write: PersonalAccountWrite<'_>,
) -> Result<(), PersonalUpstreamError> {
    remove_personal_account_publishing(database, write, PublishMode::Live).await
}

pub async fn remove_personal_account_publishing(
    database: &Database,
    write: PersonalAccountWrite<'_>,
    publish: PublishMode,
) -> Result<(), PersonalUpstreamError> {
    let PersonalAccountWrite {
        actor_id,
        user_id,
        provider_id,
        account_name,
        config_path,
        env_path,
        require_self_service,
        ..
    } = write;
    validate_names(provider_id, account_name)?;
    authorize(database, actor_id, user_id, require_self_service).await?;
    if !binding_is_active(database, provider_id, account_name, user_id).await? {
        return Err(PersonalUpstreamError::BindingNotFound);
    }
    withdraw_binding(database, actor_id, provider_id, account_name).await?;
    let env_var = upstream_env_var(provider_id, account_name);
    let published =
        omit_personal_account_files(config_path, env_path, provider_id, account_name, &env_var);
    let (rollback, transition) = match published {
        Ok(value) => value,
        Err(_) => return Err(PersonalUpstreamError::RepairRequired),
    };
    if run_publish(publish, config_path, &transition)
        .await
        .is_err()
    {
        let _ = rollback.restore();
        return Err(PersonalUpstreamError::RepairRequired);
    }
    delete_binding(database, provider_id, account_name).await
}

impl PersonalUpstreamError {
    pub fn kind(&self) -> &'static str {
        match self {
            Self::AdministratorRequired => "administrator_required",
            Self::UserNotFound => "user_not_found",
            Self::UnknownProvider => "unknown_provider",
            Self::NameCollision => "name_collision",
            Self::SecretRejected => "secret_rejected",
            Self::GrantDenied => "grant_denied",
            Self::BindingNotFound => "binding_not_found",
            Self::PublicationRestored => "publication_restored",
            Self::RepairRequired => "repair_required",
            Self::Database(_) => "database",
            Self::Configuration(_) => "configuration",
        }
    }
}

async fn run_publish(
    mode: PublishMode,
    path: &Path,
    transition: &ConfigTransition,
) -> Result<(), ()> {
    match mode {
        PublishMode::Fail => Err(()),
        PublishMode::Live => live_publish(path, transition).await,
    }
}

async fn live_publish(path: &Path, transition: &ConfigTransition) -> Result<(), ()> {
    match apply_after_mutation(path, ApplyMode::LiveOrReport, transition).await {
        Ok(
            ApplyOutcome::RehashApplied | ApplyOutcome::RehashNoop | ApplyOutcome::ServerNotRunning,
        ) => Ok(()),
        _ => Err(()),
    }
}

fn validate_names(provider_id: &str, account_name: &str) -> Result<(), PersonalUpstreamError> {
    let valid = |value: &str| {
        !value.is_empty()
            && value.len() <= 96
            && value.chars().all(|character| {
                character.is_ascii_alphanumeric() || character == '-' || character == '_'
            })
    };
    if valid(provider_id) && valid(account_name) {
        Ok(())
    } else {
        Err(PersonalUpstreamError::Configuration(
            "provider id or account name contains unsafe characters".into(),
        ))
    }
}

fn acceptable_secret(secret: &str) -> bool {
    !secret.is_empty()
        && secret.len() <= 4096
        && !secret.chars().any(|character| character.is_control())
}

fn bundled_provider(provider_id: &str) -> bool {
    crate::provider_profile::bundled_profile(provider_id).is_ok()
}

fn configuration_error(secret: &str, error: MutationError) -> PersonalUpstreamError {
    let message = error.to_string().replace(secret, "[redacted]");
    if message.contains("already configured") || message.contains("already defined") {
        PersonalUpstreamError::NameCollision
    } else if message.contains("not a bundled profile") {
        PersonalUpstreamError::UnknownProvider
    } else if message.contains("secret was rejected") {
        PersonalUpstreamError::SecretRejected
    } else {
        PersonalUpstreamError::Configuration(message)
    }
}

async fn authorize(
    database: &Database,
    actor_id: &str,
    user_id: &str,
    require_self_service: bool,
) -> Result<(), PersonalUpstreamError> {
    let actor_id = actor_id.to_owned();
    let user_id_owned = user_id.to_owned();
    let (admin, actor_is_user, active) = database
        .call(move |connection| {
            Ok((
                actor_is_admin(connection, &actor_id)?,
                connection.query_row(
                    "SELECT EXISTS(SELECT 1 FROM users WHERE id = ?1 AND id = ?2 AND state = 'active')",
                    (&actor_id, &user_id_owned),
                    |row| row.get::<_, bool>(0),
                )?,
                user_exists(connection, &user_id_owned)?,
            ))
        })
        .await?;
    if require_self_service {
        if !actor_is_user {
            return Err(PersonalUpstreamError::GrantDenied);
        }
        let effect = effective_grant(database, user_id).await?;
        if effect != GrantEffect::Allow {
            return Err(PersonalUpstreamError::GrantDenied);
        }
        return Ok(());
    }
    if !admin {
        return Err(PersonalUpstreamError::AdministratorRequired);
    }
    if !active {
        return Err(PersonalUpstreamError::UserNotFound);
    }
    Ok(())
}

fn actor_is_admin(
    connection: &rusqlite::Connection,
    actor_id: &str,
) -> Result<bool, rusqlite::Error> {
    connection.query_row(
        "SELECT EXISTS(SELECT 1 FROM users WHERE id = ?1 AND role = 'admin' AND state = 'active')",
        [actor_id],
        |row| row.get(0),
    )
}

fn user_exists(connection: &rusqlite::Connection, user_id: &str) -> Result<bool, rusqlite::Error> {
    connection.query_row(
        "SELECT EXISTS(SELECT 1 FROM users WHERE id = ?1)",
        [user_id],
        |row| row.get(0),
    )
}

async fn name_is_bound(
    database: &Database,
    provider_id: &str,
    account_name: &str,
) -> Result<bool, PersonalUpstreamError> {
    let provider_id = provider_id.to_owned();
    let account_name = account_name.to_owned();
    Ok(database
        .call(move |connection| {
            connection.query_row(
                "SELECT EXISTS(
                    SELECT 1 FROM personal_upstream_bindings
                    WHERE account_name = ?1 OR (provider_id = ?2 AND account_name = ?1)
                 )",
                (&account_name, &provider_id),
                |row| row.get(0),
            )
        })
        .await?)
}

async fn binding_is_active(
    database: &Database,
    provider_id: &str,
    account_name: &str,
    user_id: &str,
) -> Result<bool, PersonalUpstreamError> {
    let provider_id = provider_id.to_owned();
    let account_name = account_name.to_owned();
    let user_id = user_id.to_owned();
    Ok(database
        .call(move |connection| {
            connection.query_row(
                "SELECT EXISTS(
                    SELECT 1 FROM personal_upstream_bindings
                    WHERE provider_id = ?1 AND account_name = ?2 AND user_id = ?3 AND state = 'active'
                 )",
                (&provider_id, &account_name, &user_id),
                |row| row.get(0),
            )
        })
        .await?)
}

async fn insert_binding(
    database: &Database,
    actor_id: &str,
    user_id: &str,
    provider_id: &str,
    account_name: &str,
    state: &str,
    action: &str,
) -> Result<(), PersonalUpstreamError> {
    let actor_id = actor_id.to_owned();
    let user_id = user_id.to_owned();
    let provider_id = provider_id.to_owned();
    let account_name = account_name.to_owned();
    let state = state.to_owned();
    let action = action.to_owned();
    database
        .with_transaction(move |connection| {
            connection.execute(
                "INSERT INTO personal_upstream_bindings(
                    provider_id, account_name, user_id, state, actor_id
                 ) VALUES (?1, ?2, ?3, ?4, ?5)",
                (&provider_id, &account_name, &user_id, &state, &actor_id),
            )?;
            connection.execute(
                "INSERT INTO user_admin_audit(actor_id, action, target_kind, target_id)
                 VALUES (?1, ?2, 'account', ?3)",
                (&actor_id, &action, &account_name),
            )?;
            Ok(())
        })
        .await?;
    Ok(())
}

async fn withdraw_binding(
    database: &Database,
    actor_id: &str,
    provider_id: &str,
    account_name: &str,
) -> Result<(), PersonalUpstreamError> {
    let actor_id = actor_id.to_owned();
    let provider_id = provider_id.to_owned();
    let account_name = account_name.to_owned();
    database
        .with_transaction(move |connection| {
            connection.execute(
                "UPDATE personal_upstream_bindings
                 SET state = 'withdrawn', actor_id = ?1, updated_at = CURRENT_TIMESTAMP
                 WHERE provider_id = ?2 AND account_name = ?3 AND state = 'active'",
                (&actor_id, &provider_id, &account_name),
            )?;
            connection.execute(
                "INSERT INTO user_admin_audit(actor_id, action, target_kind, target_id)
                 VALUES (?1, 'upstream_remove', 'account', ?2)",
                (&actor_id, &account_name),
            )?;
            Ok(())
        })
        .await?;
    Ok(())
}

async fn delete_binding(
    database: &Database,
    provider_id: &str,
    account_name: &str,
) -> Result<(), PersonalUpstreamError> {
    let provider_id = provider_id.to_owned();
    let account_name = account_name.to_owned();
    database
        .call(move |connection| {
            connection.execute(
                "DELETE FROM personal_upstream_bindings WHERE provider_id = ?1 AND account_name = ?2",
                (&provider_id, &account_name),
            )?;
            Ok(())
        })
        .await?;
    Ok(())
}

pub fn withdraw_user_bindings(
    connection: &rusqlite::Connection,
    user_id: &str,
) -> Result<(), rusqlite::Error> {
    connection.execute(
        "UPDATE personal_upstream_bindings
         SET state = 'withdrawn', updated_at = CURRENT_TIMESTAMP
         WHERE user_id = ?1 AND state = 'active'",
        [user_id],
    )?;
    Ok(())
}
