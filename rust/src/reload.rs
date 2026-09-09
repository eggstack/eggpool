//! Transactional live rehash for the process-owned Rust runtime (R007).
//!
//! This module deliberately stops at the typed server-side API. M9 owns the
//! control socket and CLI adapter. Candidate construction, durable
//! config-derived reconciliation, task-spec preflight, and the short
//! stage/acceptance window are kept together here so no caller can accidentally
//! reopen admission while one of those authorities is still reversible.

use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    path::{Path, PathBuf},
    sync::Arc,
};

use serde::Serialize;
use sha2::{Digest, Sha256};
use thiserror::Error;
use tokio::sync::Mutex;

use crate::{
    Config,
    config_reload_policy::{ConfigDiff, compute_diff, verify_expected_digest},
    db::{Account, AccountConfig, DatabaseError, DatabaseTransaction},
    runtime_lifecycle::{
        GenerationStageError, RuntimeGenerationFactory, RuntimeManager, RuntimeTaskSpec,
        RuntimeTaskSupervisor, StagedGenerationSwap,
    },
    task_supervisor::PreparedTaskDiff,
};

const MAX_REASON_BYTES: usize = 96;

/// Input accepted by the server-side reload API. Bytes retain a canonical
/// path only for safe parse diagnostics; they are never retained in results.
#[derive(Debug, Clone)]
pub enum ReloadInput {
    Path(PathBuf),
    Bytes {
        canonical_path: PathBuf,
        content: Vec<u8>,
    },
}

impl ReloadInput {
    pub fn path(path: impl Into<PathBuf>) -> Self {
        Self::Path(path.into())
    }

    pub fn bytes(path: impl Into<PathBuf>, content: impl Into<Vec<u8>>) -> Self {
        Self::Bytes {
            canonical_path: path.into(),
            content: content.into(),
        }
    }
}

#[derive(Debug, Clone)]
pub struct ReloadRequest {
    pub input: ReloadInput,
    pub expected_digest: Option<String>,
}

impl ReloadRequest {
    pub fn from_input(input: ReloadInput) -> Self {
        Self {
            input,
            expected_digest: None,
        }
    }

    pub fn expected_digest(mut self, digest: impl Into<String>) -> Self {
        self.expected_digest = Some(digest.into());
        self
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ReloadResultCategory {
    Applied,
    Noop,
    RestartRequired,
    ValidationFailed,
    StaleDigest,
    Busy,
    RetirementBacklog,
    Aborted,
    CompensationFailed,
}

/// One-shot failures used by deterministic runtime-lifecycle qualification.
/// This is compiled only with the existing test-support feature and is never
/// part of the normal reload surface.
#[cfg(feature = "test-support")]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReloadTestFault {
    TaskPreflight,
    TaskCommit,
    PersistenceBegin,
    PersistenceApply,
    PersistenceCommit,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ReloadResult {
    pub category: ReloadResultCategory,
    pub active_generation_id: u64,
    pub active_digest_prefix: String,
    pub changed_sections: Vec<String>,
    pub restart_required_paths: Vec<String>,
    pub retirement_pending: bool,
    pub reason_code: String,
}

impl ReloadResult {
    pub(crate) fn from_active(
        manager: &RuntimeManager,
        category: ReloadResultCategory,
        reason_code: &str,
    ) -> Self {
        let slot = manager.active_slot();
        Self {
            category,
            active_generation_id: slot.generation_id(),
            active_digest_prefix: slot.digest_prefix().to_owned(),
            changed_sections: Vec::new(),
            restart_required_paths: Vec::new(),
            retirement_pending: manager.retiring_slot_count() > 0,
            reason_code: bounded_reason(reason_code),
        }
    }
}

#[derive(Debug, Error)]
enum ReloadPreparationError {
    #[error("configuration input could not be read")]
    Read,
    #[error("persistence delta could not be prepared")]
    Persistence,
}

#[derive(Debug, Clone)]
struct ProviderProjection {
    provider_id: String,
    base_url: String,
    protocols_json: String,
}

#[derive(Debug, Clone)]
struct AuthBackoffRow {
    id: i64,
    account_id: i64,
    model_id: Option<String>,
    status_code: Option<i64>,
    error_class: Option<String>,
    consecutive_failures: i64,
    backoff_until: Option<String>,
    last_failure_at: String,
    updated_at: String,
}

#[derive(Debug, Clone)]
struct AccountProjection {
    id: i64,
    config: AccountConfig,
}

#[derive(Debug, Clone)]
struct PersistenceDelta {
    providers: Vec<ProviderProjection>,
    accounts: Vec<AccountProjection>,
    old_providers: Vec<ProviderProjection>,
    old_accounts: Vec<AccountProjection>,
    auth_reset_account_ids: Vec<i64>,
    auth_rows: Vec<AuthBackoffRow>,
}

impl PersistenceDelta {
    async fn prepare(
        database: &crate::db::Database,
        old: &Config,
        candidate: &Config,
    ) -> Result<(Self, Vec<Account>), ReloadPreparationError> {
        let existing = crate::db::AccountRepository::new(database)
            .list_all()
            .await
            .map_err(|_| ReloadPreparationError::Persistence)?;
        let old_accounts = account_projections(old, &existing)?;
        let (accounts, candidate_durable) = account_projections_with_new_ids(candidate, &existing)?;
        let providers = provider_projections(candidate);
        let old_providers = provider_projections(old);
        let auth_reset_names = authentication_reset_names(old, candidate);
        let ids_by_name: BTreeMap<&str, i64> = existing
            .iter()
            .map(|account| (account.name.as_str(), account.id))
            .collect();
        let auth_reset_account_ids = auth_reset_names
            .iter()
            .filter_map(|name| ids_by_name.get(name.as_str()).copied())
            .collect::<Vec<_>>();
        let auth_rows = load_auth_rows(database, &auth_reset_account_ids)
            .await
            .map_err(|_| ReloadPreparationError::Persistence)?;
        Ok((
            Self {
                providers,
                accounts,
                old_providers,
                old_accounts,
                auth_reset_account_ids,
                auth_rows,
            },
            candidate_durable,
        ))
    }

    async fn apply(&self, transaction: &DatabaseTransaction) -> Result<(), DatabaseError> {
        let providers = self.providers.clone();
        let accounts = self.accounts.clone();
        let auth_ids = self.auth_reset_account_ids.clone();
        transaction
            .call(move |connection| {
                apply_provider_rows(connection, &providers)?;
                apply_account_rows(connection, &accounts)?;
                clear_auth_rows(connection, &auth_ids)?;
                Ok(())
            })
            .await
    }

    async fn restore(&self, transaction: &DatabaseTransaction) -> Result<(), DatabaseError> {
        let providers = self.old_providers.clone();
        let accounts = self.old_accounts.clone();
        let auth_ids = self.auth_reset_account_ids.clone();
        let auth_rows = self.auth_rows.clone();
        transaction
            .call(move |connection| {
                apply_provider_rows(connection, &providers)?;
                apply_account_rows(connection, &accounts)?;
                clear_auth_rows(connection, &auth_ids)?;
                restore_auth_rows(connection, &auth_rows)?;
                Ok(())
            })
            .await
    }
}

/// Process-owned serialized reload service. Clones address one lock and one
/// supervisor.
#[derive(Clone)]
pub struct ReloadService {
    process: crate::runtime_lifecycle::ProcessRuntime,
    manager: RuntimeManager,
    lock: Arc<Mutex<()>>,
    #[cfg(feature = "test-support")]
    test_fault: Arc<std::sync::Mutex<Option<ReloadTestFault>>>,
}

impl std::fmt::Debug for ReloadService {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("ReloadService")
            .field(
                "active_generation_id",
                &self.manager.active_slot().generation_id(),
            )
            .field("reload_busy", &self.lock.try_lock().is_err())
            .finish()
    }
}

impl ReloadService {
    pub fn new(process: crate::runtime_lifecycle::ProcessRuntime, manager: RuntimeManager) -> Self {
        process
            .task_supervisor()
            .set_generation_manager(manager.clone());
        let lock = process.reload_lock();
        Self {
            process,
            manager,
            lock,
            #[cfg(feature = "test-support")]
            test_fault: Arc::new(std::sync::Mutex::new(None)),
        }
    }

    /// Arm one deterministic, one-shot reload failure for R013 qualification.
    #[cfg(feature = "test-support")]
    pub fn inject_test_fault(&self, fault: ReloadTestFault) {
        *self.test_fault.lock().expect("reload test fault lock") = Some(fault);
    }

    #[cfg(feature = "test-support")]
    fn take_test_fault(&self, fault: ReloadTestFault) -> bool {
        let mut armed = self.test_fault.lock().expect("reload test fault lock");
        if *armed == Some(fault) {
            *armed = None;
            true
        } else {
            false
        }
    }

    pub fn manager(&self) -> &RuntimeManager {
        &self.manager
    }

    pub async fn reload_path(
        &self,
        path: impl Into<PathBuf>,
        expected_digest: Option<String>,
    ) -> ReloadResult {
        self.reload(ReloadRequest {
            input: ReloadInput::Path(path.into()),
            expected_digest,
        })
        .await
    }

    pub async fn reload_bytes(
        &self,
        canonical_path: impl Into<PathBuf>,
        content: impl Into<Vec<u8>>,
        expected_digest: Option<String>,
    ) -> ReloadResult {
        self.reload(ReloadRequest {
            input: ReloadInput::Bytes {
                canonical_path: canonical_path.into(),
                content: content.into(),
            },
            expected_digest,
        })
        .await
    }

    /// Run reload work in an owned task. Caller cancellation therefore never
    /// drops a staged candidate or leaves the admission gate unresolved.
    pub async fn reload(&self, request: ReloadRequest) -> ReloadResult {
        let service = self.clone();
        let join = tokio::spawn(async move { service.reload_owned(request).await });
        match join.await {
            Ok(result) => result,
            Err(_) => ReloadResult::from_active(
                &self.manager,
                ReloadResultCategory::Aborted,
                "worker_failed",
            ),
        }
    }

    async fn reload_owned(&self, request: ReloadRequest) -> ReloadResult {
        let Ok(_guard) = self.lock.try_lock() else {
            return ReloadResult::from_active(
                &self.manager,
                ReloadResultCategory::Busy,
                "reload_busy",
            );
        };
        let diagnostics = self.process.begin_reload_diagnostics(&self.manager);
        let result = self.reload_owned_transaction(request).await;
        diagnostics.finish(&result);
        result
    }

    async fn reload_owned_transaction(&self, request: ReloadRequest) -> ReloadResult {
        if self.manager.is_shutting_down() {
            return ReloadResult::from_active(
                &self.manager,
                ReloadResultCategory::Aborted,
                "shutdown_started",
            );
        }
        let (path, bytes) = match read_input(&request.input, self.process.config_path()) {
            Ok(value) => value,
            Err(_) => {
                return ReloadResult::from_active(
                    &self.manager,
                    ReloadResultCategory::ValidationFailed,
                    "config_read_failed",
                );
            }
        };
        let digest = digest_bytes(&bytes);
        if verify_expected_digest(request.expected_digest.as_deref(), &digest).is_err() {
            return ReloadResult::from_active(
                &self.manager,
                ReloadResultCategory::StaleDigest,
                "digest_mismatch",
            );
        }
        let candidate_config = match Config::from_toml_bytes(&path, &bytes) {
            Ok(config) => config,
            Err(_) => {
                return ReloadResult::from_active(
                    &self.manager,
                    ReloadResultCategory::ValidationFailed,
                    "config_validation_failed",
                );
            }
        };
        let active = self.manager.active_slot();
        let old_config = active.generation().config().clone();
        let diff = match compute_diff(&old_config, &candidate_config) {
            Ok(diff) => diff,
            Err(_) => {
                return ReloadResult::from_active(
                    &self.manager,
                    ReloadResultCategory::ValidationFailed,
                    "config_diff_failed",
                );
            }
        };
        if diff.is_noop() {
            return self.result_with_diff(ReloadResultCategory::Noop, "no_changes", &diff, false);
        }
        if diff.has_restart_required() {
            return self.result_with_diff(
                ReloadResultCategory::RestartRequired,
                "restart_required",
                &diff,
                false,
            );
        }
        let expected_generation = active.generation_id();
        let generation_id = expected_generation.saturating_add(1);
        let (persistence, durable_accounts) = match PersistenceDelta::prepare(
            &self.process.database(),
            &old_config,
            &candidate_config,
        )
        .await
        {
            Ok(value) => value,
            Err(_) => {
                return self.result_with_diff(
                    ReloadResultCategory::Aborted,
                    "persistence_prepare_failed",
                    &diff,
                    false,
                );
            }
        };
        let candidate = match RuntimeGenerationFactory::prepare_with_durable_accounts(
            &self.process,
            candidate_config.clone(),
            digest,
            generation_id,
            durable_accounts,
        )
        .await
        {
            Ok(candidate) => candidate,
            Err(_) => {
                return self.result_with_diff(
                    ReloadResultCategory::Aborted,
                    "candidate_prepare_failed",
                    &diff,
                    false,
                );
            }
        };
        let supervisor = self.process.task_supervisor();
        let current_specs = supervisor.active_specs();
        let candidate_specs = available_task_specs(&supervisor, &candidate_config, &current_specs);
        let mut task_diff = match supervisor.prepare_diff(&current_specs, &candidate_specs) {
            Ok(diff) => diff,
            Err(_) => {
                candidate.abort().await;
                return self.result_with_diff(
                    ReloadResultCategory::Aborted,
                    "task_preflight_failed",
                    &diff,
                    false,
                );
            }
        };
        #[cfg(feature = "test-support")]
        if self.take_test_fault(ReloadTestFault::TaskPreflight) {
            candidate.abort().await;
            return self.result_with_diff(
                ReloadResultCategory::Aborted,
                "task_preflight_failed",
                &diff,
                false,
            );
        }
        if task_diff.preflight().is_err() {
            candidate.abort().await;
            return self.result_with_diff(
                ReloadResultCategory::Aborted,
                "task_preflight_failed",
                &diff,
                false,
            );
        }
        let mut staged = match self.manager.stage(expected_generation, &candidate) {
            Ok(staged) => staged,
            Err(GenerationStageError::RetirementBacklog) => {
                task_diff.rollback();
                candidate.abort().await;
                return self.result_with_diff(
                    ReloadResultCategory::RetirementBacklog,
                    "retirement_backlog",
                    &diff,
                    true,
                );
            }
            Err(_) => {
                task_diff.rollback();
                candidate.abort().await;
                return self.result_with_diff(
                    ReloadResultCategory::Aborted,
                    "stage_failed",
                    &diff,
                    false,
                );
            }
        };
        let mut wire_policy = match self.process.stage_wire_resolver_policy(&candidate_config) {
            Ok(stage) => stage,
            Err(_) => {
                return self
                    .abort_staged(staged, task_diff, &diff, "wire_policy_stage_failed")
                    .await;
            }
        };
        #[cfg(feature = "test-support")]
        if self.take_test_fault(ReloadTestFault::PersistenceBegin) {
            return self
                .abort_staged(staged, task_diff, &diff, "persistence_begin_failed")
                .await;
        }
        let transaction = match self.process.database().begin_transaction().await {
            Ok(transaction) => transaction,
            Err(_) => {
                return self
                    .abort_staged(staged, task_diff, &diff, "persistence_begin_failed")
                    .await;
            }
        };
        #[cfg(feature = "test-support")]
        let persistence_apply_failed = self.take_test_fault(ReloadTestFault::PersistenceApply);
        #[cfg(not(feature = "test-support"))]
        let persistence_apply_failed = false;
        if persistence_apply_failed || persistence.apply(&transaction).await.is_err() {
            let _ = transaction.rollback().await;
            return self
                .abort_staged(staged, task_diff, &diff, "persistence_apply_failed")
                .await;
        }
        if staged.commit_pointer().is_err() {
            let _ = transaction.rollback().await;
            return self
                .abort_staged(staged, task_diff, &diff, "pointer_commit_failed")
                .await;
        }
        #[cfg(feature = "test-support")]
        let task_commit_failed = self.take_test_fault(ReloadTestFault::TaskCommit);
        #[cfg(not(feature = "test-support"))]
        let task_commit_failed = false;
        if task_commit_failed || task_diff.commit().await.is_err() {
            let _ = transaction.rollback().await;
            let _ = staged.rollback_pointer();
            return self
                .abort_staged(staged, task_diff, &diff, "task_commit_failed")
                .await;
        }
        #[cfg(feature = "test-support")]
        let persistence_commit_fault = self.take_test_fault(ReloadTestFault::PersistenceCommit);
        #[cfg(feature = "test-support")]
        let persistence_commit_failed = if persistence_commit_fault {
            // Release the transaction before exercising the same repair path
            // as a real commit failure; otherwise the test-only branch would
            // retain the SQLite writer while compensation tries to begin.
            let _ = transaction.rollback().await;
            true
        } else {
            transaction.commit().await.is_err()
        };
        #[cfg(not(feature = "test-support"))]
        let persistence_commit_failed = transaction.commit().await.is_err();
        if persistence_commit_failed {
            let mut compensation_failed = false;
            if let Ok(repair_transaction) = self.process.database().begin_transaction().await {
                if persistence.restore(&repair_transaction).await.is_err()
                    || repair_transaction.commit().await.is_err()
                {
                    compensation_failed = true;
                }
            } else {
                compensation_failed = true;
            }
            let pointer_rolled_back = staged.rollback_pointer().is_ok();
            if pointer_rolled_back {
                wire_policy.rollback();
            }
            if !pointer_rolled_back || task_diff.rollback_committed().await.is_err() {
                compensation_failed = true;
            }
            if compensation_failed {
                if !pointer_rolled_back {
                    wire_policy.finalize();
                }
                return self.result_with_diff(
                    ReloadResultCategory::CompensationFailed,
                    "compensation_failed",
                    &diff,
                    true,
                );
            }
            if let Ok(candidate) = staged.rollback() {
                candidate.close().await;
            }
            return self.result_with_diff(
                ReloadResultCategory::Aborted,
                "persistence_commit_failed",
                &diff,
                false,
            );
        }
        // The durable transaction is now irreversible. Publish the shared
        // process policy while admission remains closed, immediately before
        // the matching generation/task acceptance. A pre-accept failure can
        // therefore never expose candidate wire behavior.
        wire_policy.commit();
        let publication = if self.manager.is_shutting_down() {
            match staged.accept_during_shutdown() {
                Ok(publication) => publication,
                Err(_) => {
                    return self.acceptance_failure_after_commit(
                        &mut wire_policy,
                        staged,
                        &diff,
                        "shutdown_acceptance_failed",
                    );
                }
            }
        } else {
            match staged.accept() {
                Ok(publication) => publication,
                Err(_) => {
                    if self.manager.is_shutting_down() {
                        match staged.accept_during_shutdown() {
                            Ok(publication) => publication,
                            Err(_) => {
                                return self.acceptance_failure_after_commit(
                                    &mut wire_policy,
                                    staged,
                                    &diff,
                                    "shutdown_acceptance_failed",
                                );
                            }
                        }
                    } else {
                        return self.acceptance_failure_after_commit(
                            &mut wire_policy,
                            staged,
                            &diff,
                            "acceptance_failed",
                        );
                    }
                }
            }
        };
        wire_policy.finalize();
        let mut result =
            self.result_with_diff(ReloadResultCategory::Applied, "applied", &diff, true);
        result.active_generation_id = publication.new_slot.generation_id();
        result.active_digest_prefix = publication.new_slot.digest_prefix().to_owned();
        result.retirement_pending = self.manager.retiring_slot_count() > 0;
        result
    }

    async fn abort_staged(
        &self,
        mut staged: StagedGenerationSwap,
        mut task_diff: PreparedTaskDiff,
        diff: &ConfigDiff,
        reason: &str,
    ) -> ReloadResult {
        task_diff.rollback();
        if let Ok(candidate) = staged.rollback() {
            candidate.close().await;
        }
        self.result_with_diff(ReloadResultCategory::Aborted, reason, diff, false)
    }

    fn acceptance_failure_after_commit(
        &self,
        wire_policy: &mut crate::coordinator::WireResolverPolicyStage,
        staged: StagedGenerationSwap,
        diff: &ConfigDiff,
        reason: &str,
    ) -> ReloadResult {
        // The database is already committed here. Keep the new pointer
        // fail-closed rather than allowing Drop to restore the old runtime.
        staged.fail_closed();
        wire_policy.finalize();
        self.result_with_diff(ReloadResultCategory::CompensationFailed, reason, diff, true)
    }

    fn result_with_diff(
        &self,
        category: ReloadResultCategory,
        reason: &str,
        diff: &ConfigDiff,
        retirement_pending: bool,
    ) -> ReloadResult {
        let mut result = ReloadResult::from_active(&self.manager, category, reason);
        result.changed_sections = diff.changed_sections();
        result.restart_required_paths = diff
            .restart_required()
            .into_iter()
            .map(|change| change.path.clone())
            .collect();
        result.retirement_pending = retirement_pending || self.manager.retiring_slot_count() > 0;
        result
    }
}

fn bounded_reason(reason: &str) -> String {
    reason.chars().take(MAX_REASON_BYTES).collect()
}

fn read_input(
    input: &ReloadInput,
    default_path: Option<&Path>,
) -> Result<(PathBuf, Vec<u8>), ReloadPreparationError> {
    match input {
        ReloadInput::Path(path) => fs::read(path)
            .map(|bytes| (path.clone(), bytes))
            .map_err(|_| ReloadPreparationError::Read),
        ReloadInput::Bytes {
            canonical_path,
            content,
        } => Ok((canonical_path.clone(), content.clone())),
    }
    .or_else(|_| {
        default_path
            .map(|path| fs::read(path).map(|bytes| (path.to_owned(), bytes)))
            .transpose()
            .map_err(|_| ReloadPreparationError::Read)?
            .ok_or(ReloadPreparationError::Read)
    })
}

fn digest_bytes(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

fn provider_projections(config: &Config) -> Vec<ProviderProjection> {
    config
        .providers
        .iter()
        .map(|(provider_id, provider)| ProviderProjection {
            provider_id: provider_id.clone(),
            base_url: provider.base_url.clone(),
            protocols_json: serde_json::to_string(&provider.protocols)
                .unwrap_or_else(|_| "[]".into()),
        })
        .collect()
}

fn configured_accounts(config: &Config) -> Vec<AccountConfig> {
    config
        .providers
        .iter()
        .flat_map(|(provider_id, provider)| {
            provider.accounts.iter().map(move |account| AccountConfig {
                name: account.name.clone(),
                api_key_env: account.api_key_env.clone(),
                enabled: account.enabled,
                weight: account.weight,
                provider_id: provider_id.clone(),
            })
        })
        .collect()
}

fn account_projections(
    config: &Config,
    existing: &[Account],
) -> Result<Vec<AccountProjection>, ReloadPreparationError> {
    let by_name: BTreeMap<&str, &Account> = existing
        .iter()
        .map(|row| (row.name.as_str(), row))
        .collect();
    configured_accounts(config)
        .into_iter()
        .map(|account| {
            let id = by_name
                .get(account.name.as_str())
                .map(|row| row.id)
                .ok_or(ReloadPreparationError::Persistence)?;
            Ok(AccountProjection {
                id,
                config: account,
            })
        })
        .collect()
}

fn account_projections_with_new_ids(
    config: &Config,
    existing: &[Account],
) -> Result<(Vec<AccountProjection>, Vec<Account>), ReloadPreparationError> {
    let mut next_id = existing.iter().map(|row| row.id).max().unwrap_or(0) + 1;
    let by_name: BTreeMap<&str, &Account> = existing
        .iter()
        .map(|row| (row.name.as_str(), row))
        .collect();
    let desired = configured_accounts(config);
    let desired_names = desired
        .iter()
        .map(|row| row.name.clone())
        .collect::<BTreeSet<_>>();
    let mut projection = Vec::new();
    let mut durable = existing.to_vec();
    for account in desired {
        let id = by_name.get(account.name.as_str()).map_or_else(
            || {
                let id = next_id;
                next_id += 1;
                id
            },
            |row| row.id,
        );
        projection.push(AccountProjection {
            id,
            config: account.clone(),
        });
        if let Some(existing) = durable.iter_mut().find(|row| row.name == account.name) {
            existing.api_key_env = account.api_key_env.clone();
            existing.enabled = account.enabled;
            existing.weight = account.weight;
            existing.provider_id = account.provider_id.clone();
        } else {
            durable.push(Account {
                id,
                name: account.name.clone(),
                api_key_env: account.api_key_env.clone(),
                enabled: account.enabled,
                weight: account.weight,
                provider_id: account.provider_id.clone(),
            });
        }
    }
    for row in &mut durable {
        if !desired_names.contains(&row.name) {
            row.enabled = false;
        }
    }
    Ok((projection, durable))
}

fn authentication_reset_names(old: &Config, candidate: &Config) -> Vec<String> {
    let old_by_name: BTreeMap<&str, (&crate::config::AccountConfig, &str)> = old
        .providers
        .iter()
        .flat_map(|(provider_id, provider)| {
            provider
                .accounts
                .iter()
                .map(move |account| (account.name.as_str(), (account, provider_id.as_str())))
        })
        .collect();
    let mut names = Vec::new();
    for (provider_id, provider) in &candidate.providers {
        for account in &provider.accounts {
            let Some((previous, previous_provider)) = old_by_name.get(account.name.as_str()) else {
                continue;
            };
            if previous.api_key != account.api_key
                || previous.api_key_env != account.api_key_env
                || *previous_provider != provider_id.as_str()
                || (!previous.enabled && account.enabled)
            {
                names.push(account.name.clone());
            }
        }
    }
    names
}

fn available_task_specs(
    supervisor: &RuntimeTaskSupervisor,
    config: &Config,
    current: &[RuntimeTaskSpec],
) -> Vec<RuntimeTaskSpec> {
    let available = supervisor
        .available_callback_kinds()
        .into_iter()
        .collect::<BTreeSet<_>>();
    let current_names = current
        .iter()
        .map(|spec| spec.name.as_str())
        .collect::<BTreeSet<_>>();
    crate::task_supervisor::runtime_task_specs_for_config(config, true)
        .into_iter()
        .filter(|spec| {
            available.contains(&spec.callback_kind) || current_names.contains(spec.name.as_str())
        })
        .collect()
}

fn apply_provider_rows(
    connection: &mut tokio_rusqlite::rusqlite::Connection,
    providers: &[ProviderProjection],
) -> Result<(), tokio_rusqlite::rusqlite::Error> {
    for provider in providers {
        connection.execute(
            "INSERT INTO providers (provider_id, base_url, protocols) VALUES (?1, ?2, ?3) ON CONFLICT(provider_id) DO UPDATE SET base_url=excluded.base_url, protocols=excluded.protocols, enabled=1",
            tokio_rusqlite::rusqlite::params![
                provider.provider_id,
                provider.base_url,
                provider.protocols_json
            ],
        )?;
    }
    if providers.is_empty() {
        connection.execute("UPDATE providers SET enabled = 0 WHERE enabled = 1", [])?;
    } else {
        let placeholders = (1..=providers.len())
            .map(|index| format!("?{index}"))
            .collect::<Vec<_>>()
            .join(", ");
        let ids = providers
            .iter()
            .map(|provider| provider.provider_id.as_str())
            .collect::<Vec<_>>();
        connection.execute(
            &format!(
                "UPDATE providers SET enabled = 0 WHERE enabled = 1 AND provider_id NOT IN ({placeholders})"
            ),
            tokio_rusqlite::rusqlite::params_from_iter(ids),
        )?;
    }
    Ok(())
}

fn apply_account_rows(
    connection: &mut tokio_rusqlite::rusqlite::Connection,
    accounts: &[AccountProjection],
) -> Result<(), tokio_rusqlite::rusqlite::Error> {
    for account in accounts {
        connection.execute(
            "INSERT INTO accounts (id, name, api_key_env, enabled, weight, provider_id) VALUES (?1, ?2, ?3, ?4, ?5, ?6) ON CONFLICT(name) DO UPDATE SET api_key_env=excluded.api_key_env, enabled=excluded.enabled, weight=excluded.weight, provider_id=excluded.provider_id",
            tokio_rusqlite::rusqlite::params![
                account.id,
                account.config.name,
                account.config.api_key_env,
                account.config.enabled as i64,
                account.config.weight,
                account.config.provider_id
            ],
        )?;
    }
    if accounts.is_empty() {
        connection.execute("UPDATE accounts SET enabled = 0 WHERE enabled = 1", [])?;
    } else {
        let placeholders = (1..=accounts.len())
            .map(|index| format!("?{index}"))
            .collect::<Vec<_>>()
            .join(", ");
        let names = accounts
            .iter()
            .map(|account| account.config.name.as_str())
            .collect::<Vec<_>>();
        connection.execute(
            &format!(
                "UPDATE accounts SET enabled = 0 WHERE enabled = 1 AND name NOT IN ({placeholders})"
            ),
            tokio_rusqlite::rusqlite::params_from_iter(names),
        )?;
    }
    Ok(())
}

fn clear_auth_rows(
    connection: &mut tokio_rusqlite::rusqlite::Connection,
    account_ids: &[i64],
) -> Result<(), tokio_rusqlite::rusqlite::Error> {
    for account_id in account_ids {
        connection.execute(
            "DELETE FROM account_backoffs WHERE account_id = ?1 AND reason = 'authentication_failed'",
            [account_id],
        )?;
    }
    Ok(())
}

fn restore_auth_rows(
    connection: &mut tokio_rusqlite::rusqlite::Connection,
    rows: &[AuthBackoffRow],
) -> Result<(), tokio_rusqlite::rusqlite::Error> {
    for row in rows {
        connection.execute(
            "INSERT OR REPLACE INTO account_backoffs (id, account_id, model_id, reason, status_code, error_class, consecutive_failures, backoff_until, last_failure_at, updated_at) VALUES (?1, ?2, ?3, 'authentication_failed', ?4, ?5, ?6, ?7, ?8, ?9)",
            tokio_rusqlite::rusqlite::params![
                row.id,
                row.account_id,
                row.model_id,
                row.status_code,
                row.error_class,
                row.consecutive_failures,
                row.backoff_until,
                row.last_failure_at,
                row.updated_at
            ],
        )?;
    }
    Ok(())
}

async fn load_auth_rows(
    database: &crate::db::Database,
    ids: &[i64],
) -> Result<Vec<AuthBackoffRow>, DatabaseError> {
    let ids = ids.to_vec();
    database
        .call(move |connection| {
            if ids.is_empty() {
                return Ok(Vec::new());
            }
            let placeholders = (1..=ids.len())
                .map(|index| format!("?{index}"))
                .collect::<Vec<_>>()
                .join(", ");
            let mut statement = connection.prepare(&format!(
                "SELECT id, account_id, model_id, status_code, error_class, consecutive_failures, backoff_until, last_failure_at, updated_at FROM account_backoffs WHERE reason = 'authentication_failed' AND account_id IN ({placeholders})"
            ))?;
            statement
                .query_map(tokio_rusqlite::rusqlite::params_from_iter(ids), |row| {
                    Ok(AuthBackoffRow {
                        id: row.get(0)?,
                        account_id: row.get(1)?,
                        model_id: row.get(2)?,
                        status_code: row.get(3)?,
                        error_class: row.get(4)?,
                        consecutive_failures: row.get(5)?,
                        backoff_until: row.get(6)?,
                        last_failure_at: row.get(7)?,
                        updated_at: row.get(8)?,
                    })
                })?
                .collect()
        })
        .await
}
