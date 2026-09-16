//! `eggpool-connect` binary: transactional desktop configurator.

#![forbid(unsafe_code)]

use std::io::{self, Write};
use std::path::Path;

use clap::Parser;
use eggpool_client_config::{ClientTarget, ConnectionProfileV1};
use eggpool_connect::backup::list_backups;
use eggpool_connect::cli::{Cli, Commands, parse_client_target};
use eggpool_connect::credential::resolve_api_key;
use eggpool_connect::detect::detect_client;
use eggpool_connect::fetch::{HyperProfileFetcher, ProfileFetcher};
use eggpool_connect::install::{FailureInjector, restore_backup};
use eggpool_connect::outcome::{ConnectError, ExitCode, redact};
use eggpool_connect::paths::state_root;
use eggpool_connect::process::RealProcessRunner;

#[tokio::main(flavor = "current_thread")]
async fn main() {
    let code = run().await;
    std::process::exit(code);
}

async fn run() -> i32 {
    let cli = Cli::parse();
    match dispatch(cli).await {
        Ok(()) => ExitCode::Success.code(),
        Err(error) => {
            eprintln!("eggpool-connect: {error}");
            error.exit_code().code()
        }
    }
}

async fn dispatch(cli: Cli) -> Result<(), ConnectError> {
    match cli.command {
        Commands::Plan {
            profile,
            client,
            config,
            no_verify_network,
            json,
            state_dir,
        } => {
            let state = state_dir.unwrap_or_else(state_root);
            plan(
                &profile,
                client.as_deref(),
                config.as_deref(),
                &state,
                !no_verify_network,
                json,
            )
            .await
        }
        Commands::Install {
            profile,
            client,
            config,
            yes,
            force,
            api_key_stdin,
            no_verify_network,
            json,
            state_dir,
        } => {
            let state = state_dir.unwrap_or_else(state_root);
            install(
                &profile,
                client.as_deref(),
                config.as_deref(),
                &state,
                yes,
                force,
                api_key_stdin,
                no_verify_network,
                json,
            )
            .await
        }
        Commands::Verify {
            client,
            config,
            json,
            state_dir,
        } => {
            let state = state_dir.unwrap_or_else(state_root);
            verify(&client, config.as_deref(), &state, json).await
        }
        Commands::Backups {
            client,
            json,
            state_dir,
        } => {
            let state = state_dir.unwrap_or_else(state_root);
            backups(client.as_deref(), &state, json)
        }
        Commands::Restore {
            backup_id,
            json,
            state_dir,
            api_key_stdin: _,
        } => {
            let state = state_dir.unwrap_or_else(state_root);
            restore(&backup_id, &state, json)
        }
        Commands::Remove {
            client,
            config,
            yes,
            force,
            json,
            state_dir,
        } => {
            let state = state_dir.unwrap_or_else(state_root);
            remove(&client, config.as_deref(), &state, yes, force, json).await
        }
    }
}

fn decode_profile(token: &str) -> Result<ConnectionProfileV1, ConnectError> {
    let trimmed = token.trim();
    eggpool_client_config::decode_profile(trimmed).map_err(|error| ConnectError::InvalidProfile {
        detail: error.to_string(),
    })
}

fn select_targets(
    connection: &ConnectionProfileV1,
    client: Option<&str>,
) -> Result<Vec<ClientTarget>, ConnectError> {
    let filter = parse_client_target(client)?;
    let mut targets = connection.targets.clone();
    if let Some(only) = filter {
        if !targets.contains(&only) {
            return Err(ConnectError::UnsupportedClient {
                detail: format!(
                    "profile targets are [{}] and do not include {only}",
                    targets
                        .iter()
                        .map(|target| target.name())
                        .collect::<Vec<_>>()
                        .join(", ")
                ),
            });
        }
        targets = vec![only];
    }
    targets.sort_by_key(|target| target.name());
    targets.dedup();
    Ok(targets)
}

async fn plan(
    token: &str,
    client: Option<&str>,
    config: Option<&Path>,
    state_root: &Path,
    with_network: bool,
    json: bool,
) -> Result<(), ConnectError> {
    let connection = decode_profile(token)?;
    let targets = select_targets(&connection, client)?;
    let runner = RealProcessRunner;
    let mut entries = Vec::new();
    // Best-effort credential for a full remote plan; plan never fails when
    // no credential is available — it shows the token-level plan instead.
    let credential = std::env::var("EGGPOOL_API_KEY")
        .ok()
        .map(|value| value.trim().to_owned())
        .filter(|value| !value.is_empty());
    for target in targets {
        let detection = detect_client(&runner, target, config).await?;
        let entry = if with_network && credential.is_some() {
            let api_key = credential.as_deref().unwrap_or("");
            let fetcher = HyperProfileFetcher;
            match fetcher.fetch(&connection, api_key).await {
                Ok(remote) => {
                    match eggpool_connect::install::build_mutation(&remote, &detection, state_root)
                    {
                        Ok(planned) => serde_json::json!({
                            "client": target.name(),
                            "config_path": detection.config_path.display().to_string(),
                            "detection": detection.summary(),
                            "remote_revision": planned.remote_revision,
                            "no_op": planned.no_op,
                            "diff": planned.diff_summary,
                        }),
                        Err(error) => serde_json::json!({
                            "client": target.name(),
                            "config_path": detection.config_path.display().to_string(),
                            "detection": detection.summary(),
                            "error": error.to_string(),
                        }),
                    }
                }
                Err(error) => serde_json::json!({
                    "client": target.name(),
                    "config_path": detection.config_path.display().to_string(),
                    "detection": detection.summary(),
                    "fetch_error": redact(&error.to_string(), api_key),
                }),
            }
        } else {
            serde_json::json!({
                "client": target.name(),
                "config_path": detection.config_path.display().to_string(),
                "detection": detection.summary(),
                "endpoint": connection.proxy.base_url,
                "note": "remote fetch skipped (no credential or --no-verify-network); install will fetch the current integration profile",
            })
        };
        entries.push(entry);
    }
    if json {
        println!(
            "{}",
            serde_json::to_string_pretty(&serde_json::json!({
                "endpoint": connection.proxy.base_url,
                "targets": entries,
            }))
            .unwrap_or_else(|_| "{}".to_owned())
        );
    } else {
        println!("EggPool endpoint: {}", connection.proxy.base_url);
        for entry in &entries {
            println!(
                "Client: {}",
                entry.get("client").and_then(|v| v.as_str()).unwrap_or("?")
            );
            println!(
                "Config: {}",
                entry
                    .get("config_path")
                    .and_then(|v| v.as_str())
                    .unwrap_or("?")
            );
            if let Some(diff) = entry.get("diff").and_then(|v| v.as_array()) {
                println!("Proposed changes:");
                for line in diff {
                    println!("  - {}", line.as_str().unwrap_or("?"));
                }
            } else if let Some(note) = entry.get("note").and_then(|v| v.as_str()) {
                println!("Note: {note}");
            }
            if let Some(error) = entry.get("error").and_then(|v| v.as_str()) {
                println!("Plan error: {error}");
            }
            if let Some(error) = entry.get("fetch_error").and_then(|v| v.as_str()) {
                println!("Fetch: {error}");
            }
            println!("Backup: {}/backups/<backup-id>/", state_root.display());
        }
        println!("Credential: required, not included in profile");
        println!("Run `install` to apply after review (no filesystem changes from `plan`).");
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
async fn install(
    token: &str,
    client: Option<&str>,
    config: Option<&Path>,
    state_root: &Path,
    yes: bool,
    force: bool,
    api_key_stdin: bool,
    _no_verify_network: bool,
    json: bool,
) -> Result<(), ConnectError> {
    let connection = decode_profile(token)?;
    let targets = select_targets(&connection, client)?;
    // Credential separately from the profile: env, TTY prompt, or stdin.
    let (api_key, _source) = resolve_api_key(api_key_stdin)?;
    let runner = RealProcessRunner;
    let fetcher = HyperProfileFetcher;
    let mut outcomes = Vec::new();
    for target in targets {
        let detection = detect_client(&runner, target, config).await?;
        // Show the plan and require confirmation by default.
        let remote = fetcher.fetch(&connection, &api_key).await?;
        let planned = eggpool_connect::install::build_mutation(&remote, &detection, state_root)?;
        if !json && !yes {
            println!("EggPool endpoint: {}", connection.proxy.base_url);
            println!("Client: {target}");
            println!("Config: {}", detection.config_path.display());
            println!("Proposed changes:");
            for line in &planned.diff_summary {
                println!("  - {line}");
            }
            println!("Backup: {}/backups/<backup-id>/", state_root.display());
            if !confirm("Apply? [y/N] ")? {
                return Err(ConnectError::Drift {
                    detail: "declined by user; no changes made".to_owned(),
                });
            }
        }
        // `require_native` is true for default interactive installs without
        // --yes: refuse before mutation when native verification is
        // impossible rather than pretending success.
        let outcome = eggpool_connect::install::install(
            &runner,
            &fetcher,
            &connection,
            Some(remote.clone()),
            &detection,
            state_root,
            &api_key,
            force,
            !yes && detection.executable.is_none(),
            FailureInjector::default(),
        )
        .await?;
        outcomes.push(outcome);
    }
    if json {
        let results: Vec<_> = outcomes
            .iter()
            .map(|outcome| {
                serde_json::json!({
                    "client": outcome.target.name(),
                    "config_path": outcome.config_path.display().to_string(),
                    "no_op": outcome.no_op,
                    "backup_id": outcome.backup_id,
                    "native_verified": outcome.native_verified,
                    "native_detail": outcome.native_detail,
                    "remote_revision": outcome.remote_revision,
                    "diff": outcome.diff_summary,
                })
            })
            .collect();
        println!(
            "{}",
            serde_json::to_string_pretty(&serde_json::json!({"results": results}))
                .unwrap_or_else(|_| "{}".to_owned())
        );
    } else {
        for outcome in &outcomes {
            if outcome.no_op {
                println!(
                    "{}: nothing changed (already current, revision {})",
                    outcome.target, outcome.remote_revision
                );
            } else if outcome.native_verified {
                println!(
                    "{}: installed and verified (backup {}, revision {})",
                    outcome.target,
                    outcome.backup_id.as_deref().unwrap_or("?"),
                    outcome.remote_revision
                );
            } else {
                println!(
                    "{}: installed but client-native verification unavailable (only after explicit consent; backup {})",
                    outcome.target,
                    outcome.backup_id.as_deref().unwrap_or("?")
                );
            }
        }
        println!(
            "Credential persistence is opt-in and deferred: set EGGPOOL_API_KEY in the environment that launches your client."
        );
    }
    Ok(())
}

fn confirm(prompt: &str) -> Result<bool, ConnectError> {
    print!("{prompt}");
    let _ = io::stdout().flush();
    let mut line = String::new();
    io::stdin()
        .read_line(&mut line)
        .map_err(|_| ConnectError::Io {
            detail: "cannot read confirmation".to_owned(),
        })?;
    Ok(matches!(
        line.trim().to_ascii_lowercase().as_str(),
        "y" | "yes"
    ))
}

async fn verify(
    client: &str,
    config: Option<&Path>,
    state_root: &Path,
    json: bool,
) -> Result<(), ConnectError> {
    let target: ClientTarget = client
        .parse()
        .map_err(|_| ConnectError::UnsupportedClient {
            detail: format!("unsupported client {client:?}; expected codex or opencode"),
        })?;
    let runner = RealProcessRunner;
    let detection = detect_client(&runner, target, config).await?;
    let existing = eggpool_connect::atomic::read_existing(&detection.config_path)?;
    let text = existing
        .as_ref()
        .map(|bytes| String::from_utf8_lossy(bytes).into_owned())
        .unwrap_or_default();
    // Shape-only local check (no remote profile): the owned block must exist
    // with the Responses contract and must not embed a key.
    let shape_ok = match target {
        ClientTarget::Codex => {
            let lines: Vec<String> = text.lines().map(str::to_owned).collect();
            eggpool_client_config::text::table_value(&lines, "model_providers.eggpool", "wire_api")
                .as_deref()
                == Some("responses")
                && eggpool_client_config::text::table_value(
                    &lines,
                    "model_providers.eggpool",
                    "env_key",
                )
                .as_deref()
                    == Some("EGGPOOL_API_KEY")
        }
        ClientTarget::Opencode => {
            !text.trim().is_empty()
                && !eggpool_client_config::text::has_jsonc_comments(&text)
                && serde_json::from_str::<serde_json::Value>(&text)
                    .ok()
                    .and_then(|value| {
                        value
                            .get("provider")
                            .and_then(|provider| provider.get("eggpool"))
                            .cloned()
                    })
                    .is_some()
        }
    };
    let native = if detection.native_verification_available {
        match eggpool_connect::verify::validate_native(
            &runner,
            target,
            &detection.config_path,
            state_root,
            "",
            eggpool_connect::detect::NATIVE_VERIFY_TIMEOUT,
        )
        .await
        {
            Ok(detail) => (true, detail),
            Err(error) => (false, error.to_string()),
        }
    } else {
        (
            false,
            "client-native verification unavailable (executable absent)".to_owned(),
        )
    };
    if json {
        println!(
            "{}",
            serde_json::to_string_pretty(&serde_json::json!({
                "client": target.name(),
                "config_path": detection.config_path.display().to_string(),
                "shape_ok": shape_ok,
                "native_ok": native.0,
                "native_detail": native.1,
            }))
            .unwrap_or_else(|_| "{}".to_owned())
        );
    } else {
        println!("Client: {target}");
        println!("Config: {}", detection.config_path.display());
        println!("Local shape: {}", if shape_ok { "ok" } else { "drift" });
        println!(
            "Native: {} ({})",
            if native.0 { "ok" } else { "unavailable/failed" },
            native.1
        );
    }
    if !shape_ok {
        return Err(ConnectError::Validation {
            detail: "client config does not match the EggPool Responses contract".to_owned(),
        });
    }
    Ok(())
}

fn backups(client: Option<&str>, state_root: &Path, json: bool) -> Result<(), ConnectError> {
    let filter = parse_client_target(client)?;
    let manifests = list_backups(state_root, filter)?;
    if json {
        println!(
            "{}",
            serde_json::to_string_pretty(&manifests).unwrap_or_else(|_| "[]".to_owned())
        );
    } else {
        if manifests.is_empty() {
            println!("No backups.");
        }
        for manifest in &manifests {
            println!(
                "{} {} {} {} {}",
                manifest.backup_id,
                manifest.created_unix,
                manifest.target,
                manifest.config_path.display(),
                manifest.pre_sha256.chars().take(12).collect::<String>(),
            );
        }
    }
    Ok(())
}

fn restore(backup_id: &str, state_root: &Path, json: bool) -> Result<(), ConnectError> {
    let pre = restore_backup(state_root, backup_id, "")?;
    if json {
        println!(
            "{}",
            serde_json::to_string_pretty(&serde_json::json!({
                "restored": backup_id,
                "pre_restore_backup": pre.id,
            }))
            .unwrap_or_else(|_| "{}".to_owned())
        );
    } else {
        println!(
            "Restored backup {backup_id} (pre-restore backup {} retained).",
            pre.id
        );
    }
    Ok(())
}

async fn remove(
    client: &str,
    config: Option<&Path>,
    state_root: &Path,
    yes: bool,
    force: bool,
    json: bool,
) -> Result<(), ConnectError> {
    let target: ClientTarget = client
        .parse()
        .map_err(|_| ConnectError::UnsupportedClient {
            detail: format!("unsupported client {client:?}; expected codex or opencode"),
        })?;
    let runner = RealProcessRunner;
    let detection = detect_client(&runner, target, config).await?;
    if !json && !yes {
        println!("Client: {target}");
        println!("Config: {}", detection.config_path.display());
        println!("Proposed: remove only EggPool-owned fields/artifacts.");
        if !confirm("Remove? [y/N] ")? {
            return Err(ConnectError::Drift {
                detail: "declined by user; no changes made".to_owned(),
            });
        }
    }
    let record = eggpool_connect::install::remove_owned(&detection, state_root, force)?;
    if json {
        println!(
            "{}",
            serde_json::to_string_pretty(&serde_json::json!({
                "client": target.name(),
                "backup_id": record.id,
            }))
            .unwrap_or_else(|_| "{}".to_owned())
        );
    } else {
        println!(
            "Removed EggPool-owned {} config (backup {}).",
            target, record.id
        );
    }
    Ok(())
}
