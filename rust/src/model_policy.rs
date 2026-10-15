//! Versioned, user-specific model authorization.
//!
//! Policy snapshots are immutable for one admitted request. SQLite revision
//! triggers invalidate the bounded process cache on the next request.

use std::{
    collections::{BTreeMap, BTreeSet, HashMap},
    sync::{Arc, Mutex},
};

use thiserror::Error;

use crate::db::Database;

const POLICY_CACHE_CAPACITY: usize = 256;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Effect {
    Allow,
    Deny,
}

impl Effect {
    fn parse(value: &str) -> Self {
        if value == "allow" {
            Self::Allow
        } else {
            Self::Deny
        }
    }
}

#[derive(Debug, Error)]
pub enum ModelPolicyError {
    #[error("user policy is unavailable")]
    UserUnavailable,
    #[error("model policy input is invalid")]
    InvalidPolicyInput,
    #[error("preferred model is not allowed by its profile")]
    PreferredModelDenied,
    #[error(transparent)]
    Database(#[from] crate::db::DatabaseError),
}

#[derive(Debug, Clone)]
pub struct ModelPolicySnapshot {
    revision: i64,
    role: String,
    default_effect: Option<Effect>,
    preferred_model_id: Option<String>,
    profile_rules: BTreeMap<String, Effect>,
    user_overrides: BTreeMap<String, Effect>,
    instance_denials: BTreeSet<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PolicyProfileSummary {
    pub id: String,
    pub name: String,
    pub default_effect: String,
    pub preferred_model_id: Option<String>,
    pub version: i64,
    pub assigned_users: i64,
    pub model_rules: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PolicyProfileRuleSummary {
    pub profile_id: String,
    pub model_id: String,
    pub effect: String,
}

impl ModelPolicySnapshot {
    /// Monotonic persisted revision used by discovery ETags and diagnostics.
    pub fn revision(&self) -> i64 {
        self.revision
    }

    pub fn preferred_model_id(&self) -> Option<&str> {
        self.preferred_model_id.as_deref()
    }

    /// Resolve exact provider-qualified rules before the canonical model rule.
    /// A global instance denial is always checked first.
    pub fn allows(&self, model_id: &str, provider_id: Option<&str>) -> bool {
        let qualified = provider_id.map(|provider| format!("{model_id}/{provider}"));
        if self.instance_denials.contains(model_id)
            || qualified
                .as_ref()
                .is_some_and(|model| self.instance_denials.contains(model))
        {
            return false;
        }
        if let Some(effect) = exact_effect(&self.user_overrides, model_id, qualified.as_deref()) {
            return *effect == Effect::Allow;
        }
        if let Some(effect) = exact_effect(&self.profile_rules, model_id, qualified.as_deref()) {
            return *effect == Effect::Allow;
        }
        self.default_effect.unwrap_or(if self.role == "admin" {
            Effect::Allow
        } else {
            Effect::Deny
        }) == Effect::Allow
    }

    pub fn filter_models(&self, model_ids: impl IntoIterator<Item = String>) -> Vec<String> {
        model_ids
            .into_iter()
            .filter(|model_id| self.allows_catalog_id(model_id))
            .collect()
    }

    /// Catalog and integration identifiers append the provider as a suffix.
    pub fn allows_catalog_id(&self, public_id: &str) -> bool {
        match public_id.rsplit_once('/') {
            Some((model_id, provider_id)) => self.allows(model_id, Some(provider_id)),
            None => self.allows(public_id, None),
        }
    }
}

fn exact_effect<'a>(
    rules: &'a BTreeMap<String, Effect>,
    model_id: &str,
    qualified: Option<&str>,
) -> Option<&'a Effect> {
    qualified
        .and_then(|model| rules.get(model))
        .or_else(|| rules.get(model_id))
}

#[derive(Clone)]
pub struct ModelPolicyService {
    database: Database,
    cache: Arc<Mutex<HashMap<String, Arc<ModelPolicySnapshot>>>>,
    mutation_fence: Option<AdminSessionFence>,
}

type AdminSessionFence = crate::web_session::SessionFence;

fn validate_mutation_actor(
    connection: &mut tokio_rusqlite::rusqlite::Connection,
    actor_id: &str,
    fence: Option<&AdminSessionFence>,
) -> Result<(), tokio_rusqlite::rusqlite::Error> {
    let Some(fence) = fence else {
        return Ok(());
    };
    if !fence.validates_actor(connection, actor_id, true)? {
        return Err(tokio_rusqlite::rusqlite::Error::QueryReturnedNoRows);
    }
    Ok(())
}

impl std::fmt::Debug for ModelPolicyService {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("ModelPolicyService")
            .field(
                "cached_users",
                &self.cache.lock().map(|cache| cache.len()).unwrap_or(0),
            )
            .finish()
    }
}

impl ModelPolicyService {
    pub fn new(database: Database) -> Self {
        Self {
            database,
            cache: Arc::new(Mutex::new(HashMap::new())),
            mutation_fence: None,
        }
    }

    /// Fence browser-admin mutations against session revocation or user
    /// changes that occur after middleware admits the request.
    pub fn with_browser_admin_session(mut self, principal: &crate::web_session::Principal) -> Self {
        self.mutation_fence = Some(AdminSessionFence::from_principal(principal));
        self
    }

    pub async fn create_profile(
        &self,
        id: &str,
        name: &str,
        default_effect: &str,
        actor_id: &str,
    ) -> Result<(), ModelPolicyError> {
        let id = id.trim();
        let name = name.trim().to_owned();
        if id.is_empty() || id.len() > 64 || name.is_empty() || name.len() > 64 {
            return Err(ModelPolicyError::InvalidPolicyInput);
        }
        let id = id.to_owned();
        let default_effect = validate_effect(default_effect)?;
        let actor_id = actor_id.to_owned();
        let mutation_fence = self.mutation_fence.clone();
        self.database
            .with_transaction(move |connection| {
                validate_mutation_actor(connection, &actor_id, mutation_fence.as_ref())?;
                connection.execute(
                    "INSERT INTO policy_profiles(id, name, default_effect, created_by, updated_by) \
                     VALUES (?1, ?2, ?3, ?4, ?4)",
                    (&id, &name, default_effect, &actor_id),
                )?;
                connection.execute(
                    "INSERT INTO user_admin_audit(actor_id, action, target_kind, target_id) \
                     VALUES (?1, 'create_profile', 'profile', ?2)",
                    (&actor_id, &id),
                )?;
                Ok(())
            })
            .await?;
        Ok(())
    }

    pub async fn list_profiles(&self) -> Result<Vec<PolicyProfileSummary>, ModelPolicyError> {
        self.database
            .call(|connection| {
                let mut statement = connection.prepare(
                    "SELECT p.id, p.name, p.default_effect, p.preferred_model_id, p.version, \
                            (SELECT COUNT(*) FROM user_profile_assignments a WHERE a.profile_id = p.id), \
                            (SELECT COUNT(*) FROM profile_model_rules r WHERE r.profile_id = p.id) \
                     FROM policy_profiles p ORDER BY p.name COLLATE NOCASE, p.id LIMIT 200",
                )?;
                statement
                    .query_map([], |row| {
                        Ok(PolicyProfileSummary {
                            id: row.get(0)?,
                            name: row.get(1)?,
                            default_effect: row.get(2)?,
                            preferred_model_id: row.get(3)?,
                            version: row.get(4)?,
                            assigned_users: row.get(5)?,
                            model_rules: row.get(6)?,
                        })
                    })?
                    .collect()
            })
            .await
            .map_err(ModelPolicyError::Database)
    }

    pub async fn list_profile_rules(
        &self,
    ) -> Result<Vec<PolicyProfileRuleSummary>, ModelPolicyError> {
        self.database
            .call(|connection| {
                let mut statement = connection.prepare(
                    "SELECT profile_id, model_id, effect FROM profile_model_rules \
                     ORDER BY profile_id, model_id LIMIT 2000",
                )?;
                statement
                    .query_map([], |row| {
                        Ok(PolicyProfileRuleSummary {
                            profile_id: row.get(0)?,
                            model_id: row.get(1)?,
                            effect: row.get(2)?,
                        })
                    })?
                    .collect()
            })
            .await
            .map_err(ModelPolicyError::Database)
    }

    pub async fn remove_profile_rule(
        &self,
        profile_id: &str,
        model_id: &str,
        actor_id: &str,
    ) -> Result<bool, ModelPolicyError> {
        let model_id = validate_model_id(model_id)?.to_owned();
        let profile_id = profile_id.to_owned();
        let actor_id = actor_id.to_owned();
        let mutation_fence = self.mutation_fence.clone();
        self.database
            .with_transaction(move |connection| {
                validate_mutation_actor(connection, &actor_id, mutation_fence.as_ref())?;
                let removed = connection.execute(
                    "DELETE FROM profile_model_rules WHERE profile_id = ?1 AND model_id = ?2",
                    (&profile_id, &model_id),
                )?;
                if removed == 1 {
                    connection.execute(
                        "UPDATE policy_profiles SET version = version + 1, updated_by = ?2, \
                         updated_at = CURRENT_TIMESTAMP WHERE id = ?1",
                        (&profile_id, &actor_id),
                    )?;
                    connection.execute(
                        "INSERT INTO user_admin_audit(actor_id, action, target_kind, target_id) \
                         VALUES (?1, 'remove_profile_rule', 'profile', ?2)",
                        (&actor_id, &profile_id),
                    )?;
                }
                Ok(removed == 1)
            })
            .await
            .map_err(ModelPolicyError::Database)
    }

    /// Compare-and-swap profile metadata. A stale browser form gets a conflict
    /// instead of silently overwriting a newer administrator edit.
    pub async fn update_profile(
        &self,
        id: &str,
        name: &str,
        default_effect: &str,
        expected_version: i64,
        actor_id: &str,
    ) -> Result<bool, ModelPolicyError> {
        let name = name.trim().to_owned();
        if name.is_empty() || name.len() > 64 || expected_version <= 0 {
            return Err(ModelPolicyError::InvalidPolicyInput);
        }
        let effect = validate_effect(default_effect)?;
        let id = id.to_owned();
        let actor_id = actor_id.to_owned();
        let mutation_fence = self.mutation_fence.clone();
        self.database
            .with_transaction(move |connection| {
                validate_mutation_actor(connection, &actor_id, mutation_fence.as_ref())?;
                let changed = connection.execute(
                    "UPDATE policy_profiles SET name = ?1, default_effect = ?2, \
                     version = version + 1, updated_by = ?3, updated_at = CURRENT_TIMESTAMP \
                     WHERE id = ?4 AND version = ?5",
                    (&name, effect, &actor_id, &id, expected_version),
                )?;
                if changed == 1 {
                    connection.execute(
                        "INSERT INTO user_admin_audit(actor_id, action, target_kind, target_id) \
                         VALUES (?1, 'update_profile', 'profile', ?2)",
                        (&actor_id, &id),
                    )?;
                }
                Ok(changed == 1)
            })
            .await
            .map_err(ModelPolicyError::Database)
    }

    pub async fn delete_profile(
        &self,
        id: &str,
        expected_version: i64,
        actor_id: &str,
    ) -> Result<bool, ModelPolicyError> {
        if expected_version <= 0 {
            return Err(ModelPolicyError::InvalidPolicyInput);
        }
        let id = id.to_owned();
        let actor_id = actor_id.to_owned();
        let mutation_fence = self.mutation_fence.clone();
        self.database
            .with_transaction(move |connection| {
                validate_mutation_actor(connection, &actor_id, mutation_fence.as_ref())?;
                let assigned: bool = connection.query_row(
                    "SELECT EXISTS(SELECT 1 FROM user_profile_assignments WHERE profile_id = ?1)",
                    [&id],
                    |row| row.get(0),
                )?;
                if assigned {
                    return Ok(false);
                }
                let changed = connection.execute(
                    "DELETE FROM policy_profiles WHERE id = ?1 AND version = ?2",
                    (&id, expected_version),
                )?;
                if changed == 1 {
                    connection.execute(
                        "INSERT INTO user_admin_audit(actor_id, action, target_kind, target_id) \
                         VALUES (?1, 'delete_profile', 'profile', ?2)",
                        (&actor_id, &id),
                    )?;
                }
                Ok(changed == 1)
            })
            .await
            .map_err(ModelPolicyError::Database)
    }

    pub async fn set_profile_rule(
        &self,
        profile_id: &str,
        model_id: &str,
        effect: &str,
        actor_id: &str,
    ) -> Result<(), ModelPolicyError> {
        let model_id = validate_model_id(model_id)?.to_owned();
        let effect = validate_effect(effect)?;
        let profile_id = profile_id.to_owned();
        let actor_id = actor_id.to_owned();
        let mutation_fence = self.mutation_fence.clone();
        self.database
            .with_transaction(move |connection| {
                validate_mutation_actor(connection, &actor_id, mutation_fence.as_ref())?;
                connection.execute(
                    "INSERT INTO profile_model_rules(profile_id, model_id, effect, created_by, updated_by) \
                     VALUES (?1, ?2, ?3, ?4, ?4) \
                     ON CONFLICT(profile_id, model_id) DO UPDATE SET \
                         effect = excluded.effect, updated_by = excluded.updated_by, \
                         updated_at = CURRENT_TIMESTAMP",
                    (&profile_id, &model_id, effect, &actor_id),
                )?;
                connection.execute(
                    "UPDATE policy_profiles SET version = version + 1, updated_by = ?2, \
                     updated_at = CURRENT_TIMESTAMP WHERE id = ?1",
                    (&profile_id, &actor_id),
                )?;
                connection.execute(
                    "INSERT INTO user_admin_audit(actor_id, action, target_kind, target_id) \
                     VALUES (?1, 'set_profile_rule', 'profile', ?2)",
                    (&actor_id, &profile_id),
                )?;
                Ok(())
            })
            .await?;
        Ok(())
    }

    pub async fn set_profile_preference(
        &self,
        profile_id: &str,
        model_id: Option<&str>,
        actor_id: &str,
    ) -> Result<(), ModelPolicyError> {
        let profile_id = profile_id.to_owned();
        let model_id = model_id
            .map(validate_model_id)
            .transpose()?
            .map(str::to_owned);
        let actor_id = actor_id.to_owned();
        let mutation_fence = self.mutation_fence.clone();
        self.database
            .with_transaction(move |connection| {
                validate_mutation_actor(connection, &actor_id, mutation_fence.as_ref())?;
                let default_effect = connection.query_row(
                    "SELECT default_effect FROM policy_profiles WHERE id = ?1",
                    [&profile_id],
                    |row| row.get::<_, String>(0),
                )?;
                if let Some(model_id) = model_id.as_deref() {
                    let rule = connection
                        .query_row(
                            "SELECT effect FROM profile_model_rules \
                             WHERE profile_id = ?1 AND model_id = ?2",
                            (&profile_id, model_id),
                            |row| row.get::<_, String>(0),
                        )
                        .optional()?;
                    let effect = rule
                        .as_deref()
                        .map(Effect::parse)
                        .unwrap_or_else(|| Effect::parse(&default_effect));
                    let instance_denied = connection.query_row(
                        "SELECT EXISTS(SELECT 1 FROM instance_model_denials WHERE model_id = ?1)",
                        [model_id],
                        |row| row.get::<_, bool>(0),
                    )?;
                    if effect == Effect::Deny || instance_denied {
                        return Err(tokio_rusqlite::rusqlite::Error::InvalidParameterName(
                            "preferred model is denied by policy".to_owned(),
                        ));
                    }
                }
                connection.execute(
                    "UPDATE policy_profiles SET preferred_model_id = ?2, \
                     version = version + 1, updated_by = ?3, updated_at = CURRENT_TIMESTAMP \
                     WHERE id = ?1",
                    (&profile_id, model_id, &actor_id),
                )?;
                connection.execute(
                    "INSERT INTO user_admin_audit(actor_id, action, target_kind, target_id) \
                     VALUES (?1, 'set_profile_preference', 'profile', ?2)",
                    (&actor_id, &profile_id),
                )?;
                Ok(())
            })
            .await
            .map_err(|error| {
                let preferred_denied = match &error {
                    crate::db::DatabaseError::Transaction { source }
                    | crate::db::DatabaseError::Sqlite { source, .. } => matches!(
                        source.as_ref(),
                        tokio_rusqlite::rusqlite::Error::InvalidParameterName(_)
                    ),
                    _ => false,
                };
                if preferred_denied {
                    ModelPolicyError::PreferredModelDenied
                } else {
                    ModelPolicyError::Database(error)
                }
            })?;
        Ok(())
    }

    pub async fn assign_profile(
        &self,
        user_id: &str,
        profile_id: &str,
        actor_id: &str,
    ) -> Result<(), ModelPolicyError> {
        let user_id = user_id.to_owned();
        let profile_id = profile_id.to_owned();
        let actor_id = actor_id.to_owned();
        let mutation_fence = self.mutation_fence.clone();
        self.database
            .with_transaction(move |connection| {
                validate_mutation_actor(connection, &actor_id, mutation_fence.as_ref())?;
                connection.execute(
                    "INSERT INTO user_profile_assignments(user_id, profile_id, assigned_by) \
                     VALUES (?1, ?2, ?3) \
                     ON CONFLICT(user_id) DO UPDATE SET \
                         profile_id = excluded.profile_id, \
                         version = user_profile_assignments.version + 1, \
                         assigned_by = excluded.assigned_by, updated_at = CURRENT_TIMESTAMP",
                    (&user_id, &profile_id, &actor_id),
                )?;
                connection.execute(
                    "INSERT INTO user_admin_audit(actor_id, action, target_kind, target_id) \
                     VALUES (?1, 'assign_profile', 'user', ?2)",
                    (&actor_id, &user_id),
                )?;
                Ok(())
            })
            .await?;
        Ok(())
    }

    pub async fn set_user_override(
        &self,
        user_id: &str,
        model_id: &str,
        effect: &str,
        actor_id: &str,
    ) -> Result<(), ModelPolicyError> {
        let user_id = user_id.to_owned();
        let model_id = validate_model_id(model_id)?.to_owned();
        let effect = validate_effect(effect)?;
        let actor_id = actor_id.to_owned();
        let mutation_fence = self.mutation_fence.clone();
        self.database
            .with_transaction(move |connection| {
                validate_mutation_actor(connection, &actor_id, mutation_fence.as_ref())?;
                connection.execute(
                    "INSERT INTO user_policy_overrides(user_id, model_id, effect, updated_by) \
                     VALUES (?1, ?2, ?3, ?4) \
                     ON CONFLICT(user_id, model_id) DO UPDATE SET \
                         effect = excluded.effect, updated_by = excluded.updated_by, \
                         updated_at = CURRENT_TIMESTAMP",
                    (&user_id, &model_id, effect, &actor_id),
                )?;
                connection.execute(
                    "INSERT INTO user_admin_audit(actor_id, action, target_kind, target_id) \
                     VALUES (?1, 'set_user_override', 'user', ?2)",
                    (&actor_id, &user_id),
                )?;
                Ok(())
            })
            .await?;
        Ok(())
    }

    pub async fn remove_user_override(
        &self,
        user_id: &str,
        model_id: &str,
        actor_id: &str,
    ) -> Result<bool, ModelPolicyError> {
        let user_id = user_id.to_owned();
        let model_id = validate_model_id(model_id)?.to_owned();
        let actor_id = actor_id.to_owned();
        let mutation_fence = self.mutation_fence.clone();
        self.database
            .with_transaction(move |connection| {
                validate_mutation_actor(connection, &actor_id, mutation_fence.as_ref())?;
                let removed = connection.execute(
                    "DELETE FROM user_policy_overrides WHERE user_id = ?1 AND model_id = ?2",
                    (&user_id, &model_id),
                )?;
                if removed == 1 {
                    connection.execute(
                        "INSERT INTO user_admin_audit(actor_id, action, target_kind, target_id) \
                         VALUES (?1, 'remove_user_override', 'user', ?2)",
                        (&actor_id, &user_id),
                    )?;
                }
                Ok(removed == 1)
            })
            .await
            .map_err(ModelPolicyError::Database)
    }

    pub async fn set_instance_denial(
        &self,
        model_id: &str,
        actor_id: &str,
        denied: bool,
    ) -> Result<(), ModelPolicyError> {
        let model_id = validate_model_id(model_id)?.to_owned();
        let actor_id = actor_id.to_owned();
        let mutation_fence = self.mutation_fence.clone();
        self.database
            .with_transaction(move |connection| {
                validate_mutation_actor(connection, &actor_id, mutation_fence.as_ref())?;
                if denied {
                    connection.execute(
                        "INSERT INTO instance_model_denials(model_id, created_by) \
                         VALUES (?1, ?2) ON CONFLICT(model_id) DO NOTHING",
                        (&model_id, &actor_id),
                    )?;
                } else {
                    connection.execute(
                        "DELETE FROM instance_model_denials WHERE model_id = ?1",
                        [&model_id],
                    )?;
                }
                connection.execute(
                    "INSERT INTO user_admin_audit(actor_id, action, target_kind, target_id) \
                     VALUES (?1, ?2, 'model', ?3)",
                    (
                        &actor_id,
                        if denied { "deny_model" } else { "allow_model" },
                        &model_id,
                    ),
                )?;
                Ok(())
            })
            .await?;
        Ok(())
    }

    pub async fn snapshot(
        &self,
        user_id: &str,
    ) -> Result<Arc<ModelPolicySnapshot>, ModelPolicyError> {
        let key = user_id.to_owned();
        let revision = self
            .database
            .call(|connection| {
                connection.query_row(
                    "SELECT revision FROM policy_state WHERE singleton = 1",
                    [],
                    |row| row.get::<_, i64>(0),
                )
            })
            .await?;
        if let Ok(cache) = self.cache.lock()
            && let Some(snapshot) = cache.get(&key)
            && snapshot.revision == revision
        {
            return Ok(snapshot.clone());
        }

        let user_id_for_db = key.clone();
        let snapshot = self
            .database
            .with_transaction(move |connection| {
                let revision = connection.query_row(
                    "SELECT revision FROM policy_state WHERE singleton = 1",
                    [],
                    |row| row.get::<_, i64>(0),
                )?;
                let user = connection
                    .query_row(
                        "SELECT role FROM users WHERE id = ?1 AND state = 'active'",
                        [&user_id_for_db],
                        |row| row.get::<_, String>(0),
                    )
                    .optional()?;
                let Some(role) = user else {
                    return Ok(None);
                };
                let profile = connection
                    .query_row(
                        "SELECT p.default_effect, p.preferred_model_id FROM user_profile_assignments a \
                         JOIN policy_profiles p ON p.id = a.profile_id \
                         WHERE a.user_id = ?1",
                        [&user_id_for_db],
                        |row| Ok((row.get::<_, String>(0)?, row.get::<_, Option<String>>(1)?)),
                    )
                    .optional()?;
                let profile_rules = query_rules(
                    connection,
                    "SELECT r.model_id, r.effect FROM profile_model_rules r \
                     JOIN user_profile_assignments a ON a.profile_id = r.profile_id \
                     WHERE a.user_id = ?1",
                    &user_id_for_db,
                )?;
                let user_overrides = query_rules(
                    connection,
                    "SELECT model_id, effect FROM user_policy_overrides WHERE user_id = ?1",
                    &user_id_for_db,
                )?;
                let mut statement =
                    connection.prepare("SELECT model_id FROM instance_model_denials")?;
                let instance_denials = statement
                    .query_map([], |row| row.get::<_, String>(0))?
                    .collect::<Result<BTreeSet<_>, _>>()?;
                Ok(Some(ModelPolicySnapshot {
                    revision,
                    role,
                    default_effect: profile.as_ref().map(|(effect, _)| Effect::parse(effect)),
                    preferred_model_id: profile.and_then(|(_, preferred)| preferred),
                    profile_rules,
                    user_overrides,
                    instance_denials,
                }))
            })
            .await?
            .ok_or(ModelPolicyError::UserUnavailable)?;
        let snapshot = Arc::new(snapshot);
        if let Ok(mut cache) = self.cache.lock() {
            if cache.len() >= POLICY_CACHE_CAPACITY
                && !cache.contains_key(&key)
                && let Some(eviction) = cache.keys().next().cloned()
            {
                cache.remove(&eviction);
            }
            cache.insert(key, snapshot.clone());
        }
        Ok(snapshot)
    }
}

fn validate_effect(effect: &str) -> Result<&'static str, ModelPolicyError> {
    match effect {
        "allow" => Ok("allow"),
        "deny" => Ok("deny"),
        _ => Err(ModelPolicyError::InvalidPolicyInput),
    }
}

fn validate_model_id(model_id: &str) -> Result<&str, ModelPolicyError> {
    if model_id.is_empty()
        || model_id.len() > 512
        || model_id.trim() != model_id
        || model_id.chars().any(char::is_whitespace)
        || model_id.contains('*')
    {
        return Err(ModelPolicyError::InvalidPolicyInput);
    }
    Ok(model_id)
}

fn query_rules(
    connection: &tokio_rusqlite::rusqlite::Connection,
    sql: &str,
    user_id: &str,
) -> Result<BTreeMap<String, Effect>, tokio_rusqlite::rusqlite::Error> {
    let mut statement = connection.prepare(sql)?;
    statement
        .query_map([user_id], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
        })?
        .map(|row| {
            let (model_id, effect) = row?;
            Ok((model_id, Effect::parse(&effect)))
        })
        .collect()
}

use tokio_rusqlite::rusqlite::OptionalExtension;
