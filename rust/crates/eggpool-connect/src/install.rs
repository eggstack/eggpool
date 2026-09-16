//! Plan/install/verify/restore/remove orchestration over the transaction
//! state machine.
//!
//! All renderers come from `eggpool-client-config`; this module owns backup
//! ordering, atomic replacement, validation, and rollback — never a second
//! Codex/OpenCode engine.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use eggpool_client_config::{AgentIntegrationProfileV1, ClientTarget, ConnectionProfileV1};

use crate::atomic::{atomic_restore, atomic_write, read_existing};
use crate::backup::{
    BackupInput, BackupManifest, BackupRecord, create_backup, fingerprint_profile, list_backups,
    load_backup, read_snapshot,
};
use crate::detect::{Detection, variant_supports_auto_mutation};
use crate::fetch::ProfileFetcher;
use crate::outcome::{ConnectError, redact};
use crate::paths::helper_codex_catalog_path_for;
use crate::process::ProcessRunner;
use crate::transaction::{Phase, Transaction};
use crate::verify::{validate_local, validate_native};

/// Failure injection for deterministic rollback tests (never used in
/// production paths).
#[derive(Debug, Clone, Copy, Default)]
pub struct FailureInjector {
    pub fail_write: bool,
    pub fail_local_validation: bool,
    pub fail_native_validation: bool,
    pub fail_rollback: bool,
}

/// Proposed mutation without filesystem effects.
#[derive(Debug, Clone)]
pub struct PlannedMutation {
    pub target: ClientTarget,
    pub config_path: PathBuf,
    pub catalog_path: Option<PathBuf>,
    pub proposed_config: Vec<u8>,
    pub proposed_catalog: Option<Vec<u8>>,
    pub existing_config: Option<Vec<u8>>,
    pub existing_catalog: Option<Vec<u8>>,
    pub diff_summary: Vec<String>,
    pub no_op: bool,
    pub remote_revision: String,
    pub base_url: String,
}

/// Outcome of a successful install.
#[derive(Debug, Clone)]
pub struct InstallOutcome {
    pub target: ClientTarget,
    pub config_path: PathBuf,
    pub backup_id: Option<String>,
    pub backup_dir: Option<PathBuf>,
    pub no_op: bool,
    pub native_verified: bool,
    pub native_detail: String,
    pub remote_revision: String,
    pub diff_summary: Vec<String>,
}

/// Build the proposed mutation in memory (no filesystem effects beyond
/// reading the current files for diffing).
pub fn build_mutation(
    remote: &AgentIntegrationProfileV1,
    detection: &Detection,
    state_root: &Path,
) -> Result<PlannedMutation, ConnectError> {
    let target = detection.target;
    let config_path = detection.config_path.clone();
    let existing_config =
        read_existing(&config_path).map_err(|error| ConnectError::UnsafeConfig {
            detail: error.to_string(),
        })?;
    let existing_text = existing_config
        .as_ref()
        .map(|bytes| String::from_utf8_lossy(bytes).into_owned())
        .unwrap_or_default();

    match target {
        ClientTarget::Codex => {
            let catalog_path = helper_codex_catalog_path_for(state_root);
            let existing_catalog = read_existing(&catalog_path).ok().flatten();
            let catalog_json = eggpool_client_config::build_codex_catalog_json(&remote.models)
                .map_err(|error| ConnectError::InvalidProfile {
                    detail: format!("cannot render Codex catalog: {error}"),
                })?;
            let (proposed_text, previous) = eggpool_client_config::apply_codex_text_mutation(
                &existing_text,
                &remote.base_url,
                &catalog_path.to_string_lossy(),
                None,
                false,
            );
            let proposed_config = proposed_text.into_bytes();
            let proposed_catalog = catalog_json.into_bytes();
            let catalog_current = existing_catalog.as_ref() == Some(&proposed_catalog);
            let no_op = existing_config.as_ref() == Some(&proposed_config) && catalog_current;
            let mut diff_summary = Vec::new();
            if no_op {
                diff_summary.push("no changes required".to_owned());
            } else {
                diff_summary.push(format!(
                    "update Codex provider at {}",
                    config_path.display()
                ));
                diff_summary.push(format!(
                    "install generated Codex catalog at {} ({} models, revision {})",
                    catalog_path.display(),
                    remote.models.len(),
                    short_revision(&remote.revision),
                ));
                if previous
                    .model_provider
                    .as_deref()
                    .is_some_and(|value| !value.is_empty() && value != "eggpool")
                {
                    diff_summary.push(format!(
                        "capture previous model_provider {:?} for ownership-aware remove",
                        previous.model_provider.as_deref().unwrap_or("")
                    ));
                }
                if !detection.parseable {
                    diff_summary
                        .push("existing config has parse issues (see detection)".to_owned());
                }
            }
            Ok(PlannedMutation {
                target,
                config_path,
                catalog_path: Some(catalog_path),
                proposed_config,
                proposed_catalog: Some(proposed_catalog),
                existing_config,
                existing_catalog,
                diff_summary,
                no_op,
                remote_revision: remote.revision.clone(),
                base_url: remote.base_url.clone(),
            })
        }
        ClientTarget::Opencode => {
            // Shape-first variant selection (V1 `provider` vs V2
            // `providers`); ambiguous or unparseable documents fail closed
            // with a bounded location. JSONC comments and trailing commas
            // are preserved by the portable editor.
            let variant = eggpool_client_config::select_opencode_variant(
                &existing_text,
                detection.version_raw.as_deref(),
            )
            .map_err(|error| match error {
                eggpool_client_config::ClientConfigError::UnsupportedSchema { detail } => {
                    ConnectError::UnsupportedClient {
                        detail: format!(
                            "{detail} Update eggpool-connect to the latest release, or run `eggpool-connect plan` and apply the manual output."
                        ),
                    }
                }
                eggpool_client_config::ClientConfigError::UnsafeRewrite { detail } => {
                    ConnectError::UnsafeConfig { detail }
                }
                other => ConnectError::InvalidProfile {
                    detail: other.to_string(),
                },
            })?;
            let expected = eggpool_client_config::expected_opencode_provider_for(
                variant,
                &remote.base_url,
                &remote.models,
            )
            .map_err(|error| ConnectError::InvalidProfile {
                detail: format!("cannot render OpenCode provider: {error}"),
            })?;
            let mutation =
                eggpool_client_config::apply_opencode_document(&existing_text, variant, &expected)
                    .map_err(|error| match error {
                        eggpool_client_config::ClientConfigError::UnsafeRewrite { detail } => {
                            ConnectError::UnsafeConfig { detail }
                        }
                        other => ConnectError::InvalidProfile {
                            detail: other.to_string(),
                        },
                    })?;
            // Authoritative selection wins over the detection-time shape
            // probe (the probe is heuristic; selection parses fully).
            let proposed_text = mutation.text;
            let proposed_config = proposed_text.into_bytes();
            let no_op = existing_config.as_ref() == Some(&proposed_config);
            let mut diff_summary = Vec::new();
            if no_op {
                diff_summary.push("no changes required".to_owned());
            } else if existing_text.trim().is_empty() {
                diff_summary.push(format!(
                    "create OpenCode eggpool provider at {} ({} models, revision {})",
                    config_path.display(),
                    remote.models.len(),
                    short_revision(&remote.revision),
                ));
            } else {
                diff_summary.push(format!(
                    "converge OpenCode eggpool provider at {} ({} models, revision {})",
                    config_path.display(),
                    remote.models.len(),
                    short_revision(&remote.revision),
                ));
            }
            Ok(PlannedMutation {
                target,
                config_path,
                catalog_path: None,
                proposed_config,
                proposed_catalog: None,
                existing_config,
                existing_catalog: None,
                diff_summary,
                no_op,
                remote_revision: remote.revision.clone(),
                base_url: remote.base_url.clone(),
            })
        }
    }
}

/// Check owned-field drift. Revision-only model/catalog refreshes with the
/// same base URL and provider shape are safe to converge; any other owned
/// difference requires `--force`.
pub fn check_drift(
    planned: &PlannedMutation,
    remote: &AgentIntegrationProfileV1,
    force: bool,
) -> Result<(), ConnectError> {
    if planned.no_op {
        return Ok(());
    }
    let existing = planned
        .existing_config
        .as_ref()
        .map(|bytes| String::from_utf8_lossy(bytes).into_owned())
        .unwrap_or_default();
    if existing.trim().is_empty() {
        return Ok(());
    }
    let revision_only = match planned.target {
        ClientTarget::Codex => is_codex_revision_only_update(&existing, planned, remote),
        ClientTarget::Opencode => is_opencode_revision_only_update(&existing, planned, remote),
    };
    if revision_only {
        return Ok(());
    }
    // No owned block yet (fresh provider) is safe.
    if !has_owned_block(planned.target, &existing) {
        return Ok(());
    }
    if force {
        return Ok(());
    }
    Err(ConnectError::Drift {
        detail: format!(
            "EggPool-owned fields differ at {}. Review `plan` output and re-run with --force to converge deliberately",
            planned.config_path.display()
        ),
    })
}

fn has_owned_block(target: ClientTarget, existing: &str) -> bool {
    match target {
        ClientTarget::Codex => {
            let lines: Vec<String> = existing.lines().map(str::to_owned).collect();
            // EggPool ownership evidence is the managed table or an
            // `eggpool` provider selection. A pre-existing non-eggpool
            // `model_provider` (e.g. `"other"`) is a captured previous
            // value, not owned drift.
            if eggpool_client_config::text::find_table(&lines, "model_providers.eggpool").is_some()
            {
                return true;
            }
            eggpool_client_config::text::find_root_key(&lines, "model_provider")
                .and_then(|index| eggpool_client_config::text::toml_string_value(&lines[index]))
                .as_deref()
                == Some("eggpool")
        }
        ClientTarget::Opencode => existing.contains("\"eggpool\""),
    }
}

fn is_codex_revision_only_update(
    existing: &str,
    planned: &PlannedMutation,
    remote: &AgentIntegrationProfileV1,
) -> bool {
    let lines: Vec<String> = existing.lines().map(str::to_owned).collect();
    let table = "model_providers.eggpool";
    let base_matches = eggpool_client_config::text::table_value(&lines, table, "base_url")
        .as_deref()
        == Some(remote.base_url.as_str())
        || eggpool_client_config::text::table_value(&lines, table, "base_url").as_deref()
            == Some(remote.base_url.trim_end_matches('/'));
    if !base_matches {
        return false;
    }
    let shape_ok = eggpool_client_config::text::table_value(&lines, table, "wire_api").as_deref()
        == Some("responses")
        && eggpool_client_config::text::table_value(&lines, table, "env_key").as_deref()
            == Some("EGGPOOL_API_KEY")
        && eggpool_client_config::text::table_value(&lines, table, "name").as_deref()
            == Some("EggPool");
    if !shape_ok {
        return false;
    }
    // Same provider shape and base URL: only the catalog/models changed.
    let _ = planned;
    true
}

fn is_opencode_revision_only_update(
    existing: &str,
    planned: &PlannedMutation,
    remote: &AgentIntegrationProfileV1,
) -> bool {
    let _ = planned;
    // JSONC-aware: comments and trailing commas never count as drift.
    let Ok(variant) = eggpool_client_config::select_opencode_variant(existing, None) else {
        return false;
    };
    let Ok(document) = eggpool_client_config::jsonc::parse_value(existing) else {
        return false;
    };
    let Some(current) = eggpool_client_config::current_owned_entry(&document, variant) else {
        return false;
    };
    let Ok(expected) = eggpool_client_config::expected_opencode_provider_for(
        variant,
        &remote.base_url,
        &remote.models,
    ) else {
        return false;
    };
    // Same runtime/auth/endpoint identity: only models/limits changed.
    current == &expected
        || eggpool_client_config::owned_entry_allows_sync(current, &expected, variant)
}

/// Capture previous EggPool-owned values for ownership-aware `remove`.
#[must_use]
pub fn capture_previous(target: ClientTarget, existing_text: &str) -> BTreeMap<String, String> {
    let mut previous = BTreeMap::new();
    match target {
        ClientTarget::Codex => {
            let lines: Vec<String> = existing_text.lines().map(str::to_owned).collect();
            if let Some(index) =
                eggpool_client_config::text::find_root_key(&lines, "model_provider")
                && let Some(value) = eggpool_client_config::text::toml_string_value(&lines[index])
            {
                previous.insert("model_provider".to_owned(), value);
            }
            if let Some(index) =
                eggpool_client_config::text::find_root_key(&lines, "model_catalog_json")
                && let Some(value) = eggpool_client_config::text::toml_string_value(&lines[index])
            {
                previous.insert("model_catalog_json".to_owned(), value);
            }
            // Head only: trailing footer comments/blanks survive mutations
            // independently and are not restoration evidence.
            if let Some(head) = eggpool_client_config::current_provider_table_head(existing_text) {
                previous.insert("model_providers.eggpool".to_owned(), head);
            }
        }
        ClientTarget::Opencode => {
            // Exact raw capture (comments included) for byte-for-byte
            // restoration on remove. Variant defaults to V1 without version
            // evidence; callers that know the version select first.
            if let Ok(variant) = eggpool_client_config::select_opencode_variant(existing_text, None)
                && let Ok(Some((path, raw))) =
                    eggpool_client_config::capture_owned_raw(existing_text, variant)
            {
                previous.insert(path, raw);
            }
        }
    }
    previous
}

fn short_revision(revision: &str) -> String {
    revision.chars().take(12).collect()
}

/// Install one profile transactionally.
///
/// Sequence: decode/validate profile (caller), resolve credential (caller),
/// fetch remote profile, detect client, plan mutation, backup, write,
/// validate, native-verify, commit. Failures after `BackupCommitted` roll
/// back automatically.
#[allow(clippy::too_many_arguments)]
pub async fn install<R, F>(
    runner: &R,
    fetcher: &F,
    connection: &ConnectionProfileV1,
    remote: Option<AgentIntegrationProfileV1>,
    detection: &Detection,
    state_root: &Path,
    api_key: &str,
    force: bool,
    require_native: bool,
    injector: FailureInjector,
) -> Result<InstallOutcome, ConnectError>
where
    R: ProcessRunner,
    F: ProfileFetcher,
{
    let mut transaction = Transaction::new();
    // Remote fetch is required before any mutation (unless the caller
    // supplied a validated profile, e.g. `--no-verify-network` still uses the
    // required fetch; tests inject it directly).
    let remote = match remote {
        Some(remote) => {
            remote
                .validate()
                .map_err(|error| ConnectError::AuthNetwork {
                    detail: format!("integration profile failed validation: {error}"),
                })?;
            transaction.advance(Phase::RemoteProfileValidated)?;
            remote
        }
        None => {
            let fetched = fetcher.fetch(connection, api_key).await?;
            transaction.advance(Phase::RemoteProfileValidated)?;
            fetched
        }
    };
    transaction.advance(Phase::ClientDetected)?;

    // Fail closed on unparseable configs and unsupported variants before
    // planning any write.
    if !detection.parseable {
        return Err(ConnectError::UnsafeConfig {
            detail: detection
                .parse_issue
                .clone()
                .unwrap_or_else(|| "existing client config is not parseable".to_owned()),
        });
    }
    if !variant_supports_auto_mutation(detection.schema_variant) {
        return Err(ConnectError::UnsupportedClient {
            detail: format!(
                "schema variant {:?} does not support automatic mutation yet",
                detection.schema_variant
            ),
        });
    }
    // Native verification gating: without the executable the helper cannot
    // prove client-native validity. Require explicit `--yes` consent rather
    // than pretending full verification succeeded.
    let native_possible = detection.native_verification_available;
    if !native_possible && require_native {
        // `require_native` is true for default interactive installs: refuse
        // before mutation and explain the explicit path.
        return Err(ConnectError::UnsupportedClient {
            detail: format!(
                "no {} executable found, so native post-write verification cannot run. Re-run with an explicit --config path plus --yes to install without native verification, or install {} first",
                detection.target, detection.target
            ),
        });
    }

    let planned = build_mutation(&remote, detection, state_root)?;
    transaction.advance(Phase::MutationPlanned)?;
    check_drift(&planned, &remote, force)?;

    if planned.no_op {
        // Still run local + native validation so a repeated install proves
        // the current state is good.
        let existing_text = planned
            .existing_config
            .as_ref()
            .map(|bytes| String::from_utf8_lossy(bytes).into_owned())
            .unwrap_or_default();
        let catalog_text = planned
            .existing_catalog
            .as_ref()
            .map(|bytes| String::from_utf8_lossy(bytes).into_owned());
        if injector.fail_local_validation {
            return Err(ConnectError::Validation {
                detail: "injected local validation failure".to_owned(),
            });
        }
        validate_local(
            planned.target,
            &existing_text,
            catalog_text.as_deref(),
            &remote,
            api_key,
        )?;
        transaction.advance(Phase::LocalParseValidated)?;
        let (native_verified, native_detail) =
            match (native_possible, injector.fail_native_validation) {
                (_, true) => {
                    return Err(ConnectError::Validation {
                        detail: "injected native validation failure".to_owned(),
                    });
                }
                (true, false) => {
                    let expected: Vec<String> = remote
                        .models
                        .iter()
                        .map(|model| model.public_id.clone())
                        .collect();
                    let detail = validate_native(
                        runner,
                        planned.target,
                        &planned.config_path,
                        state_root,
                        api_key,
                        crate::detect::NATIVE_VERIFY_TIMEOUT,
                        &expected,
                    )
                    .await?;
                    (true, detail)
                }
                (false, false) => (
                    false,
                    "client-native verification unavailable (executable absent)".to_owned(),
                ),
            };
        transaction.advance(Phase::ClientNativeValidated)?;
        transaction.advance(Phase::Committed)?;
        return Ok(InstallOutcome {
            target: planned.target,
            config_path: planned.config_path,
            backup_id: None,
            backup_dir: None,
            no_op: true,
            native_verified,
            native_detail,
            remote_revision: planned.remote_revision,
            diff_summary: planned.diff_summary,
        });
    }

    // Backup before the first write (config + helper-owned artifacts).
    let canonical = connection
        .canonical_json()
        .map_err(|error| ConnectError::InvalidProfile {
            detail: format!("connection profile failed validation: {error}"),
        })?;
    let existing_text = planned
        .existing_config
        .as_ref()
        .map(|bytes| String::from_utf8_lossy(bytes).into_owned())
        .unwrap_or_default();
    let previous = capture_previous(planned.target, &existing_text);
    let mut artifacts: Vec<(String, PathBuf)> = Vec::new();
    if let Some(catalog_path) = &planned.catalog_path {
        artifacts.push(("codex-catalog.json".to_owned(), catalog_path.clone()));
    }
    let record = create_backup(BackupInput {
        state_root,
        target: planned.target,
        config_path: &planned.config_path,
        profile_fingerprint: &fingerprint_profile(&canonical),
        client_version: detection.version_raw.as_deref(),
        schema_variant: match detection.schema_variant {
            eggpool_client_config::ClientSchemaVariant::CodexToml => "codex-toml",
            eggpool_client_config::ClientSchemaVariant::OpencodeV1 => "opencode-v1",
            eggpool_client_config::ClientSchemaVariant::OpencodeV2 => "opencode-v2",
        },
        artifacts: &artifacts,
        previous_values: previous,
    })?;
    transaction.advance(Phase::BackupCommitted)?;

    // Write (config + catalog atomically; any failure rolls back).
    let write_result: Result<(), ConnectError> = (|| {
        if injector.fail_write {
            return Err(ConnectError::Mutation {
                detail: "injected write failure".to_owned(),
            });
        }
        atomic_write(&planned.config_path, &planned.proposed_config).map(|_| ())?;
        if let (Some(catalog_path), Some(catalog_bytes)) =
            (&planned.catalog_path, &planned.proposed_catalog)
        {
            atomic_write(catalog_path, catalog_bytes).map(|_| ())?;
        }
        Ok(())
    })();
    if let Err(error) = write_result {
        return rollback(
            state_root,
            &record.manifest,
            &record.dir,
            &planned,
            error,
            injector,
            api_key,
        )
        .await;
    }
    transaction.advance(Phase::ConfigWritten)?;

    // Layer 1: local parse/ownership validation on the installed bytes.
    let installed_text = String::from_utf8_lossy(&planned.proposed_config).into_owned();
    let installed_catalog = planned
        .proposed_catalog
        .as_ref()
        .map(|bytes| String::from_utf8_lossy(bytes).into_owned());
    let local_result: Result<(), ConnectError> = (|| {
        if injector.fail_local_validation {
            return Err(ConnectError::Validation {
                detail: "injected local validation failure".to_owned(),
            });
        }
        validate_local(
            planned.target,
            &installed_text,
            installed_catalog.as_deref(),
            &remote,
            api_key,
        )
    })();
    if let Err(error) = local_result {
        return rollback(
            state_root,
            &record.manifest,
            &record.dir,
            &planned,
            error,
            injector,
            api_key,
        )
        .await;
    }
    // Re-parse from disk (not just memory) to prove durability.
    let disk_text = std::fs::read_to_string(&planned.config_path).map_err(|error| {
        ConnectError::Validation {
            detail: format!("installed config is unreadable: {error}"),
        }
    })?;
    if disk_text.as_bytes() != planned.proposed_config.as_slice() {
        let error = ConnectError::Validation {
            detail: "installed bytes differ from the planned mutation".to_owned(),
        };
        return rollback(
            state_root,
            &record.manifest,
            &record.dir,
            &planned,
            error,
            injector,
            api_key,
        )
        .await;
    }
    transaction.advance(Phase::LocalParseValidated)?;

    // Layer 2: client-native validation (time-bound, redacted).
    let native_result: Result<String, ConnectError> = (|| async {
        if injector.fail_native_validation {
            return Err(ConnectError::Validation {
                detail: "injected native validation failure".to_owned(),
            });
        }
        if !native_possible {
            return Ok("client-native verification unavailable (executable absent)".to_owned());
        }
        let expected: Vec<String> = remote
            .models
            .iter()
            .map(|model| model.public_id.clone())
            .collect();
        validate_native(
            runner,
            planned.target,
            &planned.config_path,
            state_root,
            api_key,
            crate::detect::NATIVE_VERIFY_TIMEOUT,
            &expected,
        )
        .await
    })()
    .await;
    let native_detail = match native_result {
        Ok(detail) => detail,
        Err(error) => {
            return rollback(
                state_root,
                &record.manifest,
                &record.dir,
                &planned,
                error,
                injector,
                api_key,
            )
            .await;
        }
    };
    transaction.advance(Phase::ClientNativeValidated)?;
    transaction.advance(Phase::Committed)?;

    Ok(InstallOutcome {
        target: planned.target,
        config_path: planned.config_path,
        backup_id: Some(record.id),
        backup_dir: Some(record.dir),
        no_op: false,
        native_verified: native_possible,
        native_detail,
        remote_revision: planned.remote_revision,
        diff_summary: planned.diff_summary,
    })
}

#[allow(clippy::too_many_arguments)]
async fn rollback(
    _state_root: &Path,
    manifest: &BackupManifest,
    backup_dir: &Path,
    planned: &PlannedMutation,
    original: ConnectError,
    injector: FailureInjector,
    api_key: &str,
) -> Result<InstallOutcome, ConnectError> {
    if injector.fail_rollback {
        return Err(ConnectError::Rollback {
            backup_id: manifest.backup_id.clone(),
            backup_path: backup_dir.display().to_string(),
            detail: format!("{original}; rollback was blocked by failure injection"),
        });
    }
    let snapshot = read_snapshot(backup_dir, manifest).map_err(|error| ConnectError::Rollback {
        backup_id: manifest.backup_id.clone(),
        backup_path: backup_dir.display().to_string(),
        detail: format!("{original}; cannot read recovery snapshot: {error}"),
    })?;
    // Restore config first, then helper-owned artifacts.
    if let Err(error) = atomic_restore(&planned.config_path, snapshot.as_deref()) {
        return Err(ConnectError::Rollback {
            backup_id: manifest.backup_id.clone(),
            backup_path: backup_dir.display().to_string(),
            detail: format!("{original}; restoration failed: {error}"),
        });
    }
    if let (Some(catalog_path), Some(_)) = (&planned.catalog_path, &planned.proposed_catalog) {
        // Restore the pre-write catalog bytes (or absent).
        let pre_catalog = crate::backup::read_artifact_snapshot(backup_dir, "codex-catalog.json")
            .map_err(|error| ConnectError::Rollback {
            backup_id: manifest.backup_id.clone(),
            backup_path: backup_dir.display().to_string(),
            detail: format!("{original}; cannot read catalog snapshot: {error}"),
        })?;
        if let Err(error) = atomic_restore(catalog_path, pre_catalog.as_deref()) {
            return Err(ConnectError::Rollback {
                backup_id: manifest.backup_id.clone(),
                backup_path: backup_dir.display().to_string(),
                detail: format!("{original}; catalog restoration failed: {error}"),
            });
        }
    }
    // Verify restoration by re-reading and comparing hashes.
    let restored = read_existing(&planned.config_path).map_err(|error| ConnectError::Rollback {
        backup_id: manifest.backup_id.clone(),
        backup_path: backup_dir.display().to_string(),
        detail: format!("{original}; cannot verify restoration: {error}"),
    })?;
    let restored_bytes = restored.as_deref().unwrap_or(b"");
    let expected_bytes = snapshot.as_deref().unwrap_or(b"");
    if restored_bytes != expected_bytes {
        return Err(ConnectError::Rollback {
            backup_id: manifest.backup_id.clone(),
            backup_path: backup_dir.display().to_string(),
            detail: format!("{original}; restored bytes do not match the snapshot"),
        });
    }
    let _ = api_key;
    // Report both the original failure and the rollback result. The caller
    // maps this to the `write failed and rollback succeeded` outcome.
    Err(match original {
        ConnectError::Validation { detail } => ConnectError::Validation {
            detail: format!(
                "{detail}; rolled back to backup {} ({})",
                manifest.backup_id,
                redact(&backup_dir.display().to_string(), api_key)
            ),
        },
        ConnectError::Mutation { detail } => ConnectError::Mutation {
            detail: format!("{detail}; rolled back to backup {}", manifest.backup_id),
        },
        other => ConnectError::Validation {
            detail: format!("{other}; rolled back to backup {}", manifest.backup_id),
        },
    })
}

/// Restore a backup reversibly: take a pre-restore backup first, then restore
/// the selected snapshot atomically and validate hash + parse.
pub fn restore_backup(
    state_root: &Path,
    backup_id: &str,
    api_key: &str,
) -> Result<BackupRecord, ConnectError> {
    let (manifest, dir) = load_backup(state_root, backup_id)?;
    // Pre-restore backup makes restore itself reversible.
    let current_text =
        read_existing(&manifest.config_path).map_err(|error| ConnectError::Backup {
            detail: error.to_string(),
        })?;
    let _ = current_text;
    let pre = create_backup(BackupInput {
        state_root,
        target: if manifest.target == "codex" {
            ClientTarget::Codex
        } else {
            ClientTarget::Opencode
        },
        config_path: &manifest.config_path,
        profile_fingerprint: &manifest.profile_fingerprint,
        client_version: manifest.client_version.as_deref(),
        schema_variant: &manifest.schema_variant,
        artifacts: &[],
        previous_values: BTreeMap::new(),
    })?;
    let snapshot = read_snapshot(&dir, &manifest)?;
    atomic_restore(&manifest.config_path, snapshot.as_deref()).map_err(|error| {
        ConnectError::Mutation {
            detail: format!(
                "restore failed; pre-restore backup {} retained: {error}",
                pre.id
            ),
        }
    })?;
    // Validate restored bytes/hash.
    let restored =
        read_existing(&manifest.config_path).map_err(|error| ConnectError::Mutation {
            detail: format!("cannot verify restore: {error}"),
        })?;
    let restored_bytes = restored.as_deref().unwrap_or(b"");
    let expected_bytes = snapshot.as_deref().unwrap_or(b"");
    if restored_bytes != expected_bytes {
        return Err(ConnectError::Mutation {
            detail: format!(
                "restored bytes do not match backup {backup_id}; pre-restore backup {} retained",
                pre.id
            ),
        });
    }
    // Best-effort parse validation when the file exists.
    if let Some(bytes) = &restored {
        let text = String::from_utf8_lossy(bytes);
        if manifest.target == "codex" {
            text.parse::<toml::Value>()
                .map_err(|error| ConnectError::Validation {
                    detail: format!("restored Codex config does not parse: {error}"),
                })?;
        } else if !text.trim().is_empty() {
            // JSONC-aware: comments and trailing commas are valid.
            eggpool_client_config::jsonc::parse_value(&text).map_err(|error| {
                ConnectError::Validation {
                    detail: format!("restored OpenCode config does not parse: {error}"),
                }
            })?;
        }
    }
    let _ = api_key;
    Ok(pre)
}

/// Ownership-aware removal: remove only EggPool-owned fields/artifacts and
/// restore captured previous values when ownership evidence is valid.
///
/// Refuses on drift without `--force` and never deletes unrelated
/// providers/settings. Takes a backup first so removal is reversible.
pub fn remove_owned(
    detection: &Detection,
    state_root: &Path,
    force: bool,
) -> Result<BackupRecord, ConnectError> {
    let existing = read_existing(&detection.config_path).map_err(|error| ConnectError::Io {
        detail: error.to_string(),
    })?;
    let existing_text = existing
        .as_ref()
        .map(|bytes| String::from_utf8_lossy(bytes).into_owned())
        .unwrap_or_default();
    if existing_text.trim().is_empty() {
        return Err(ConnectError::Drift {
            detail: "nothing to remove: client config is absent or empty".to_owned(),
        });
    }
    // Restore source is the oldest backup's capture: first-ownership evidence
    // is the true pre-existing state, while later installs capture our own
    // (or externally drifted) values, which remove must never resurrect.
    // `list_backups` returns newest-first, so take the last entry. An empty
    // capture means nothing pre-existed and remove purely deletes owned
    // fields. Later pre-restore/pre-remove backups never precede the first
    // install, so the oldest entry always carries first-ownership evidence
    // (subject to the newest-10 retention bound).
    let backups = list_backups(state_root, Some(detection.target))?;
    let previous_map = backups
        .iter()
        .next_back()
        .map(|manifest| manifest.previous_values.clone())
        .unwrap_or_default();

    let removed_text = match detection.target {
        ClientTarget::Codex => {
            let mut previous = eggpool_client_config::CodexPrevious::default();
            if let Some(value) = previous_map.get("model_provider") {
                previous.model_provider = Some(value.clone());
            }
            if let Some(value) = previous_map.get("model_catalog_json") {
                previous.model_catalog_json = Some(value.clone());
            }
            if let Some(table) = previous_map.get("model_providers.eggpool") {
                previous.provider_table =
                    Some(table.lines().map(str::to_owned).collect::<Vec<_>>());
            }
            // Drift gate: if the current owned block matches neither our
            // generated shape nor the captured previous, refuse without force.
            if !force && owned_codex_drifted(&existing_text, &previous) {
                return Err(ConnectError::Drift {
                    detail: "EggPool-owned Codex fields changed externally; refusing automatic remove. Review `backups`/`restore` or re-run with --force".to_owned(),
                });
            }
            eggpool_client_config::remove_codex_owned_text(&existing_text, &previous, false)
        }
        ClientTarget::Opencode => {
            // Variant-aware, JSONC-preserving removal. A captured previous
            // entry is restored byte-for-byte; otherwise only the owned
            // entry goes (with safe empty-parent cleanup).
            let variant = eggpool_client_config::select_opencode_variant(
                &existing_text,
                detection.version_raw.as_deref(),
            )
            .map_err(|error| match error {
                eggpool_client_config::ClientConfigError::UnsupportedSchema { detail } => {
                    ConnectError::UnsupportedClient { detail }
                }
                eggpool_client_config::ClientConfigError::UnsafeRewrite { detail } => {
                    ConnectError::UnsafeConfig { detail }
                }
                other => ConnectError::InvalidProfile {
                    detail: other.to_string(),
                },
            })?;
            let owned_path = eggpool_client_config::owned_path_for(variant).to_owned();
            let previous_raw = previous_map.get(&owned_path).map(String::as_str);
            if !force {
                let document =
                    eggpool_client_config::jsonc::parse_value(&existing_text).map_err(|_| {
                        ConnectError::UnsafeConfig {
                            detail: "existing OpenCode config is not valid JSONC".to_owned(),
                        }
                    })?;
                let current =
                    eggpool_client_config::current_owned_entry(&document, variant).cloned();
                match (current, previous_raw) {
                    (None, _) => {
                        return Err(ConnectError::Drift {
                            detail: "no EggPool provider entry to remove".to_owned(),
                        });
                    }
                    (Some(entry), Some(previous)) => {
                        let previous_value =
                            eggpool_client_config::jsonc::parse_value(previous).ok();
                        if Some(&entry) != previous_value.as_ref()
                            && !eggpool_client_config::looks_like_eggpool_entry(&entry, variant)
                        {
                            return Err(ConnectError::Drift {
                                detail: "EggPool-owned OpenCode entry changed externally; refusing automatic remove. Use `restore` or --force".to_owned(),
                            });
                        }
                    }
                    (Some(entry), None) => {
                        if !eggpool_client_config::looks_like_eggpool_entry(&entry, variant) {
                            return Err(ConnectError::Drift {
                                detail: "EggPool-owned OpenCode entry changed externally; refusing automatic remove. Use `restore` or --force".to_owned(),
                            });
                        }
                    }
                }
            }
            eggpool_client_config::remove_opencode_document(&existing_text, variant, previous_raw)
                .map_err(|error| match error {
                eggpool_client_config::ClientConfigError::Drift { detail } => {
                    ConnectError::Drift { detail }
                }
                eggpool_client_config::ClientConfigError::UnsafeRewrite { detail } => {
                    ConnectError::UnsafeConfig { detail }
                }
                other => ConnectError::Mutation {
                    detail: other.to_string(),
                },
            })?
        }
    };

    // Backup before removal, then atomic replace.
    let record = create_backup(BackupInput {
        state_root,
        target: detection.target,
        config_path: &detection.config_path,
        profile_fingerprint: &"0".repeat(64),
        client_version: detection.version_raw.as_deref(),
        schema_variant: match detection.schema_variant {
            eggpool_client_config::ClientSchemaVariant::CodexToml => "codex-toml",
            eggpool_client_config::ClientSchemaVariant::OpencodeV1 => "opencode-v1",
            eggpool_client_config::ClientSchemaVariant::OpencodeV2 => "opencode-v2",
        },
        artifacts: &[],
        previous_values: BTreeMap::new(),
    })?;
    atomic_write(&detection.config_path, removed_text.as_bytes()).map_err(|error| {
        ConnectError::Mutation {
            detail: format!("remove failed after backup {}: {error}", record.id),
        }
    })?;
    Ok(record)
}

fn owned_codex_drifted(existing: &str, previous: &eggpool_client_config::CodexPrevious) -> bool {
    let lines: Vec<String> = existing.lines().map(str::to_owned).collect();
    // If there is no owned block, there is no drift.
    if eggpool_client_config::text::find_table(&lines, "model_providers.eggpool").is_none()
        && eggpool_client_config::text::find_root_key(&lines, "model_provider").is_none()
    {
        return false;
    }
    // Owned block present. If it matches our generated shape (eggpool
    // provider with Responses contract), it is ours, not drift.
    let table = "model_providers.eggpool";
    let ours = eggpool_client_config::text::table_value(&lines, table, "wire_api").as_deref()
        == Some("responses")
        && eggpool_client_config::text::table_value(&lines, table, "env_key").as_deref()
            == Some("EGGPOOL_API_KEY");
    if ours {
        return false;
    }
    // If it matches the captured previous exactly, removal will restore
    // cleanly; not drift either.
    if let Some(table_lines) = &previous.provider_table
        && let Some((start, end)) =
            eggpool_client_config::text::find_table(&lines, "model_providers.eggpool")
        && lines[start..end] == table_lines[..]
    {
        return false;
    }
    true
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::detect::Detection;
    use eggpool_client_config::{ClientSchemaVariant, IntegrationCapabilities};

    fn remote(base: &str) -> AgentIntegrationProfileV1 {
        AgentIntegrationProfileV1::new(base, Vec::new(), IntegrationCapabilities::default())
            .expect("remote")
    }

    fn codex_detection(config: &Path) -> Detection {
        Detection {
            target: ClientTarget::Codex,
            executable: None,
            version: None,
            version_raw: None,
            config_path: config.to_path_buf(),
            schema_variant: ClientSchemaVariant::CodexToml,
            config_exists: false,
            parseable: true,
            parse_issue: None,
            native_verification_available: false,
        }
    }

    #[test]
    fn codex_mutation_is_idempotent_and_secret_free() {
        let dir = tempfile::tempdir().expect("tempdir");
        let state = dir.path().join("state");
        let config = dir.path().join("config.toml");
        let detection = codex_detection(&config);
        let remote = remote("https://pool.example/v1");
        let first = build_mutation(&remote, &detection, &state).expect("plan");
        assert!(!first.no_op);
        assert!(first.catalog_path.is_some());
        let text = String::from_utf8(first.proposed_config.clone()).expect("utf8");
        assert!(text.contains("EGGPOOL_API_KEY"));
        assert!(!text.contains("ep_secret"));
        // Simulate installed state: second plan is a no-op.
        std::fs::create_dir_all(config.parent().expect("parent")).expect("mkdir");
        std::fs::write(&config, &first.proposed_config).expect("write");
        std::fs::create_dir_all(
            first
                .catalog_path
                .as_ref()
                .expect("catalog")
                .parent()
                .expect("parent"),
        )
        .expect("mkdir");
        std::fs::write(
            first.catalog_path.as_ref().expect("catalog"),
            first.proposed_catalog.as_ref().expect("catalog bytes"),
        )
        .expect("write");
        let second = build_mutation(&remote, &detection, &state).expect("plan");
        assert!(second.no_op);
    }

    #[test]
    fn opencode_jsonc_is_preserved_through_mutation() {
        let dir = tempfile::tempdir().expect("tempdir");
        let state = dir.path().join("state");
        let config = dir.path().join("opencode.json");
        std::fs::write(
            &config,
            "{\n// user comment\n\"provider\": {\n\"other\": {},\n},\n}\n",
        )
        .expect("write");
        let detection = Detection {
            target: ClientTarget::Opencode,
            executable: None,
            version: None,
            version_raw: Some("1.18.30".to_owned()),
            config_path: config,
            schema_variant: ClientSchemaVariant::OpencodeV1,
            config_exists: true,
            parseable: true,
            parse_issue: None,
            native_verification_available: false,
        };
        let remote = remote("https://pool.example/v1");
        let planned = build_mutation(&remote, &detection, &state).expect("plan");
        let text = String::from_utf8(planned.proposed_config).expect("utf8");
        assert!(text.contains("// user comment"));
        assert!(text.contains("\"other\""));
        assert!(text.contains("\"eggpool\""));
        assert!(!planned.no_op);
    }

    #[test]
    fn opencode_v2_shape_selects_v2_provider() {
        let dir = tempfile::tempdir().expect("tempdir");
        let state = dir.path().join("state");
        let config = dir.path().join("opencode.jsonc");
        std::fs::write(&config, "{\n\"providers\": {\n\"other\": {}\n}\n}\n").expect("write");
        let detection = Detection {
            target: ClientTarget::Opencode,
            executable: None,
            version: None,
            version_raw: None,
            config_path: config,
            schema_variant: ClientSchemaVariant::OpencodeV2,
            config_exists: true,
            parseable: true,
            parse_issue: None,
            native_verification_available: false,
        };
        let remote = remote("https://pool.example/v1");
        let planned = build_mutation(&remote, &detection, &state).expect("plan");
        let text = String::from_utf8(planned.proposed_config).expect("utf8");
        assert!(text.contains("\"providers\""));
        assert!(text.contains(eggpool_client_config::OPENCODE_V2_RESPONSES_PACKAGE));
        assert!(!text.contains("\"provider\":"));
    }

    #[test]
    fn opencode_malformed_jsonc_fails_closed_with_location() {
        let dir = tempfile::tempdir().expect("tempdir");
        let state = dir.path().join("state");
        let config = dir.path().join("opencode.json");
        std::fs::write(&config, "{\"provider\": }").expect("write");
        let detection = Detection {
            target: ClientTarget::Opencode,
            executable: None,
            version: None,
            version_raw: None,
            config_path: config,
            schema_variant: ClientSchemaVariant::OpencodeV1,
            config_exists: true,
            parseable: false,
            parse_issue: Some("invalid".to_owned()),
            native_verification_available: false,
        };
        let remote = remote("https://pool.example/v1");
        let error = build_mutation(&remote, &detection, &state).expect_err("must fail");
        assert!(error.to_string().contains("line"));
    }

    #[test]
    fn drift_requires_force_for_hand_edited_owned_fields() {
        let dir = tempfile::tempdir().expect("tempdir");
        let state = dir.path().join("state");
        let config = dir.path().join("config.toml");
        std::fs::write(
            &config,
            "model_provider = \"eggpool\"\n\n[model_providers.eggpool]\nname = \"Evil\"\nbase_url = \"https://evil.example/v1\"\nwire_api = \"responses\"\nsupports_websockets = false\nenv_key = \"EGGPOOL_API_KEY\"\n",
        )
        .expect("write");
        let detection = codex_detection(&config);
        let remote = remote("https://pool.example/v1");
        let planned = build_mutation(&remote, &detection, &state).expect("plan");
        assert!(!planned.no_op);
        assert!(check_drift(&planned, &remote, false).is_err());
        assert!(check_drift(&planned, &remote, true).is_ok());
    }

    #[test]
    fn capture_previous_records_owned_codex_values() {
        let previous = capture_previous(
            ClientTarget::Codex,
            "model_provider = \"other\"\n\n[model_providers.eggpool]\nname = \"Old\"\n",
        );
        assert_eq!(
            previous.get("model_provider").map(String::as_str),
            Some("other")
        );
        assert!(previous.contains_key("model_providers.eggpool"));
    }
}
