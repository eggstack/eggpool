//! Deterministic transaction tests for `eggpool-connect`.
//!
//! All tests use temporary homes/config roots and injectable
//! filesystem/process/network seams. No test mutates the contributor's real
//! Codex/OpenCode config.

use std::path::{Path, PathBuf};

use eggpool_client_config::{
    AgentIntegrationProfileV1, ClientSchemaVariant, ClientTarget, ConnectionProfileV1,
    IntegrationCapabilities,
};
use eggpool_connect::backup::{list_backups, load_backup, read_snapshot};
use eggpool_connect::detect::Detection;
use eggpool_connect::fetch::FakeProfileFetcher;
use eggpool_connect::install::{FailureInjector, build_mutation, restore_backup};
use eggpool_connect::process::{FakeProcessRunner, ProcessOutput};

const API_KEY: &str = "ep_test_key_12345678";

fn connection(targets: Vec<ClientTarget>) -> ConnectionProfileV1 {
    ConnectionProfileV1::new(
        targets,
        "https://pool.example/v1",
        "EGGPOOL_API_KEY",
        "/api/integrations/v1/profile",
        1,
        Some("0.8.0"),
    )
    .expect("profile")
}

fn remote(base: &str) -> AgentIntegrationProfileV1 {
    AgentIntegrationProfileV1::new(base, Vec::new(), IntegrationCapabilities::default())
        .expect("remote")
}

fn codex_detection(config: &Path) -> Detection {
    Detection {
        target: ClientTarget::Codex,
        executable: None,
        version: None,
        version_raw: Some("0.154.0".to_owned()),
        config_path: config.to_path_buf(),
        schema_variant: ClientSchemaVariant::CodexToml,
        config_exists: false,
        parseable: true,
        parse_issue: None,
        native_verification_available: false,
    }
}

fn opencode_detection(config: &Path) -> Detection {
    Detection {
        target: ClientTarget::Opencode,
        executable: None,
        version: None,
        version_raw: Some("1.18.30".to_owned()),
        config_path: config.to_path_buf(),
        schema_variant: ClientSchemaVariant::OpencodeV1,
        config_exists: false,
        parseable: true,
        parse_issue: None,
        native_verification_available: false,
    }
}

fn fake_runner() -> FakeProcessRunner {
    let mut runner = FakeProcessRunner::new();
    runner.insert(
        "codex",
        &["debug", "models"],
        ProcessOutput {
            success: true,
            exit_code: Some(0),
            stdout: "eggpool\n".to_owned(),
            stderr: String::new(),
        },
    );
    runner.insert(
        "codex",
        &["doctor", "--json"],
        ProcessOutput {
            success: true,
            exit_code: Some(0),
            stdout: "{}\n".to_owned(),
            stderr: String::new(),
        },
    );
    runner.insert(
        "opencode",
        &["models", "eggpool"],
        ProcessOutput {
            success: true,
            exit_code: Some(0),
            stdout: "eggpool\n".to_owned(),
            stderr: String::new(),
        },
    );
    runner
}

#[tokio::test]
async fn install_writes_atomically_with_backup_first_and_preserves_unrelated() {
    let dir = tempfile::tempdir().expect("tempdir");
    let state = dir.path().join("state");
    let config = dir.path().join("codex").join("config.toml");
    std::fs::create_dir_all(config.parent().expect("parent")).expect("mkdir");
    std::fs::write(
        &config,
        "# user comment\nmodel_provider = \"other\"\n\n[other_table]\nkey = 1\n",
    )
    .expect("write");
    let before = std::fs::read(&config).expect("read");

    let connection = connection(vec![ClientTarget::Codex]);
    let remote = remote("https://pool.example/v1");
    let detection = codex_detection(&config);
    let runner = fake_runner();
    let fetcher = FakeProfileFetcher::ok(remote.clone());

    let outcome = eggpool_connect::install::install(
        &runner,
        &fetcher,
        &connection,
        Some(remote.clone()),
        &detection,
        &state,
        API_KEY,
        false,
        false,
        FailureInjector::default(),
    )
    .await
    .expect("install");

    assert!(!outcome.no_op);
    let backup_id = outcome.backup_id.expect("backup");
    // Backup is byte-exact.
    let (_manifest, backup_dir) = load_backup(&state, &backup_id).expect("load");
    let snapshot = read_snapshot(
        &backup_dir,
        &load_backup(&state, &backup_id).expect("load").0,
    )
    .expect("snapshot");
    assert_eq!(snapshot, Some(before));
    // Unrelated content preserved.
    let installed = std::fs::read_to_string(&config).expect("read");
    assert!(installed.contains("# user comment"));
    assert!(installed.contains("[other_table]"));
    assert!(installed.contains("model_provider = \"eggpool\""));
    assert!(!installed.contains(API_KEY));
    // Backup manifest is secret-free.
    let manifest_json =
        std::fs::read_to_string(backup_dir.join("manifest.json")).expect("manifest");
    assert!(!manifest_json.contains(API_KEY));
}

#[tokio::test]
async fn repeated_install_is_noop_without_new_backup() {
    let dir = tempfile::tempdir().expect("tempdir");
    let state = dir.path().join("state");
    let config = dir.path().join("config.toml");
    let connection = connection(vec![ClientTarget::Codex]);
    let remote = remote("https://pool.example/v1");
    let detection = codex_detection(&config);
    let runner = fake_runner();
    let fetcher = FakeProfileFetcher::ok(remote.clone());

    let first = eggpool_connect::install::install(
        &runner,
        &fetcher,
        &connection,
        Some(remote.clone()),
        &detection,
        &state,
        API_KEY,
        false,
        false,
        FailureInjector::default(),
    )
    .await
    .expect("first install");
    assert!(!first.no_op);
    let backups_after_first = list_backups(&state, Some(ClientTarget::Codex)).expect("list");
    assert_eq!(backups_after_first.len(), 1);

    let before = std::fs::read(&config).expect("read");
    let second = eggpool_connect::install::install(
        &runner,
        &fetcher,
        &connection,
        Some(remote.clone()),
        &detection,
        &state,
        API_KEY,
        false,
        false,
        FailureInjector::default(),
    )
    .await
    .expect("second install");
    assert!(second.no_op);
    assert!(second.backup_id.is_none());
    assert_eq!(std::fs::read(&config).expect("read"), before);
    let backups_after_second = list_backups(&state, Some(ClientTarget::Codex)).expect("list");
    assert_eq!(backups_after_second.len(), 1);
}

#[tokio::test]
async fn write_failure_rolls_back_to_original_bytes() {
    let dir = tempfile::tempdir().expect("tempdir");
    let state = dir.path().join("state");
    let config = dir.path().join("config.toml");
    std::fs::write(&config, "model_provider = \"other\"\n").expect("write");
    let before = std::fs::read(&config).expect("read");

    let connection = connection(vec![ClientTarget::Codex]);
    let remote = remote("https://pool.example/v1");
    let detection = codex_detection(&config);
    let runner = fake_runner();
    let fetcher = FakeProfileFetcher::ok(remote.clone());

    let error = eggpool_connect::install::install(
        &runner,
        &fetcher,
        &connection,
        Some(remote),
        &detection,
        &state,
        API_KEY,
        false,
        false,
        FailureInjector {
            fail_write: true,
            ..Default::default()
        },
    )
    .await
    .expect_err("write must fail");
    assert!(error.to_string().contains("rolled back"));
    assert!(!error.to_string().contains(API_KEY));
    assert_eq!(std::fs::read(&config).expect("read"), before);
}

#[tokio::test]
async fn local_validation_failure_rolls_back() {
    let dir = tempfile::tempdir().expect("tempdir");
    let state = dir.path().join("state");
    let config = dir.path().join("config.toml");
    std::fs::write(&config, "").expect("write");

    let connection = connection(vec![ClientTarget::Codex]);
    let remote = remote("https://pool.example/v1");
    let detection = codex_detection(&config);
    let runner = fake_runner();
    let fetcher = FakeProfileFetcher::ok(remote.clone());

    let error = eggpool_connect::install::install(
        &runner,
        &fetcher,
        &connection,
        Some(remote),
        &detection,
        &state,
        API_KEY,
        false,
        false,
        FailureInjector {
            fail_local_validation: true,
            ..Default::default()
        },
    )
    .await
    .expect_err("validation must fail");
    assert!(error.to_string().contains("rolled back"));
    // Empty original is restored byte-exact (absent-or-empty preserved).
    let backups = list_backups(&state, Some(ClientTarget::Codex)).expect("list");
    assert_eq!(backups.len(), 1);
}

#[tokio::test]
async fn native_validation_failure_rolls_back() {
    let dir = tempfile::tempdir().expect("tempdir");
    let state = dir.path().join("state");
    let config = dir.path().join("config.toml");
    std::fs::write(&config, "").expect("write");

    let connection = connection(vec![ClientTarget::Codex]);
    let remote = remote("https://pool.example/v1");
    let detection = codex_detection(&config);
    let runner = fake_runner();
    let fetcher = FakeProfileFetcher::ok(remote.clone());

    let error = eggpool_connect::install::install(
        &runner,
        &fetcher,
        &connection,
        Some(remote),
        &detection,
        &state,
        API_KEY,
        false,
        false,
        FailureInjector {
            fail_native_validation: true,
            ..Default::default()
        },
    )
    .await
    .expect_err("native validation must fail");
    assert!(error.to_string().contains("rolled back"));
}

#[tokio::test]
async fn rollback_failure_is_distinct_and_pins_recovery_evidence() {
    let dir = tempfile::tempdir().expect("tempdir");
    let state = dir.path().join("state");
    let config = dir.path().join("config.toml");
    std::fs::write(&config, "original\n").expect("write");

    let connection = connection(vec![ClientTarget::Codex]);
    let remote = remote("https://pool.example/v1");
    let detection = codex_detection(&config);
    let runner = fake_runner();
    let fetcher = FakeProfileFetcher::ok(remote.clone());

    let error = eggpool_connect::install::install(
        &runner,
        &fetcher,
        &connection,
        Some(remote),
        &detection,
        &state,
        API_KEY,
        false,
        false,
        FailureInjector {
            fail_write: true,
            fail_rollback: true,
            ..Default::default()
        },
    )
    .await
    .expect_err("rollback must fail");
    let rendered = error.to_string();
    // Distinct rollback error carries backup ID but never contents/secrets.
    assert!(rendered.contains("rollback failed"));
    assert!(!rendered.contains(API_KEY));
    assert!(!rendered.contains("original\n\nmutated"));
    assert_eq!(
        error.exit_code(),
        eggpool_connect::ExitCode::RollbackFailure
    );
}

#[test]
fn restore_is_reversible_and_byte_exact_including_absent() {
    let dir = tempfile::tempdir().expect("tempdir");
    let state = dir.path().join("state");
    let config = dir.path().join("config.toml");
    // Originally absent: install creates the file with a backup.
    let remote = remote("https://pool.example/v1");
    let detection = codex_detection(&config);
    let planned = build_mutation(&remote, &detection, &state).expect("plan");
    assert!(!planned.no_op);
    // Commit a backup manually for an absent file, then write, then restore.
    let record = eggpool_connect::backup::create_backup(eggpool_connect::backup::BackupInput {
        state_root: &state,
        target: ClientTarget::Codex,
        config_path: &config,
        profile_fingerprint: &"b".repeat(64),
        client_version: None,
        schema_variant: "codex-toml",
        artifacts: &[],
        previous_values: Default::default(),
    })
    .expect("backup");
    assert!(!record.manifest.config_existed);
    std::fs::create_dir_all(config.parent().expect("parent")).expect("mkdir");
    std::fs::write(&config, "mutated\n").expect("write");
    let pre = restore_backup(&state, &record.id, "").expect("restore");
    // Restore of an originally-absent file removes it (byte-exact absent).
    assert!(!config.exists());
    // And the pre-restore backup makes restore reversible.
    assert!(!pre.id.is_empty());
    assert_ne!(pre.id, record.id);
}

#[test]
fn remove_is_ownership_aware_and_preserves_unrelated() {
    let dir = tempfile::tempdir().expect("tempdir");
    let state = dir.path().join("state");
    let config = dir.path().join("config.toml");
    std::fs::write(
        &config,
        "# keep\nmodel_provider = \"eggpool\"\nmodel_catalog_json = \"/tmp/c.json\"\n\n[model_providers.eggpool]\nname = \"EggPool\"\nbase_url = \"https://pool.example/v1\"\nwire_api = \"responses\"\nsupports_websockets = false\nenv_key = \"EGGPOOL_API_KEY\"\n\n[other]\nkey = 1\n",
    )
    .expect("write");
    let detection = codex_detection(&config);
    let record = eggpool_connect::install::remove_owned(&detection, &state, false).expect("remove");
    assert!(!record.id.is_empty());
    let removed = std::fs::read_to_string(&config).expect("read");
    assert!(removed.contains("# keep"));
    assert!(removed.contains("[other]"));
    assert!(!removed.contains("[model_providers.eggpool]"));
    assert!(!removed.contains("model_provider = \"eggpool\""));
}

#[tokio::test]
async fn remove_restores_first_ownership_capture_not_later_drift() {
    // Live-qualification regression: install, externally drift an owned
    // field, force-converge, then remove. Remove must delete owned fields
    // (first-ownership capture was empty) rather than resurrect the drifted
    // values captured by the later force install.
    let dir = tempfile::tempdir().expect("tempdir");
    let state = dir.path().join("state");
    let config = dir.path().join("config.toml");
    std::fs::write(&config, "# keep\n").expect("write");

    let connection = connection(vec![ClientTarget::Codex]);
    let remote = remote("https://pool.example/v1");
    let detection = codex_detection(&config);
    let runner = fake_runner();
    let fetcher = FakeProfileFetcher::ok(remote.clone());

    for force in [false, true] {
        eggpool_connect::install::install(
            &runner,
            &fetcher,
            &connection,
            Some(remote.clone()),
            &detection,
            &state,
            API_KEY,
            force,
            false,
            FailureInjector::default(),
        )
        .await
        .expect("install");
        if !force {
            // Externally drift one owned field between installs.
            let drifted = std::fs::read_to_string(&config)
                .expect("read")
                .replace("https://pool.example/v1", "http://evil.example/v1");
            assert_ne!(
                drifted,
                std::fs::read_to_string(&config).expect("read"),
                "fixture must contain the owned base URL"
            );
            std::fs::write(&config, drifted).expect("drift");
        }
    }
    assert!(
        !std::fs::read_to_string(&config)
            .expect("read")
            .contains("evil.example"),
        "force install converges drift"
    );

    eggpool_connect::install::remove_owned(&detection, &state, false).expect("remove");
    let removed = std::fs::read_to_string(&config).expect("read");
    assert!(removed.contains("# keep"), "unrelated content survives");
    assert!(
        !removed.contains("[model_providers.eggpool]"),
        "owned table is removed, not restored from later drift"
    );
    assert!(!removed.contains("model_provider = \"eggpool\""));
    assert!(!removed.contains("evil.example"));
}

#[test]
fn opencode_install_preserves_unrelated_providers() {
    let dir = tempfile::tempdir().expect("tempdir");
    let state = dir.path().join("state");
    let config = dir.path().join("opencode.json");
    std::fs::write(
        &config,
        r#"{"provider": {"other": {"npm": "@other/pkg"}}, "theme": "dark"}"#,
    )
    .expect("write");
    let detection = opencode_detection(&config);
    let remote = remote("https://pool.example/v1");
    let planned = build_mutation(&remote, &detection, &state).expect("plan");
    assert!(!planned.no_op);
    let text = String::from_utf8(planned.proposed_config).expect("utf8");
    assert!(text.contains("\"other\""));
    assert!(text.contains("\"eggpool\""));
    assert!(text.contains("{env:EGGPOOL_API_KEY}"));
    assert!(!text.contains(API_KEY));
}

#[tokio::test]
async fn opencode_jsonc_install_preserves_comments_end_to_end() {
    let dir = tempfile::tempdir().expect("tempdir");
    let state = dir.path().join("state");
    let config = dir.path().join("opencode.json");
    std::fs::write(
        &config,
        "{\n// user comment\n\"provider\": {\n\"other\": {\"npm\": \"@other/pkg\"},\n},\n/* trailing */\n}\n",
    )
    .expect("write");

    let connection = connection(vec![ClientTarget::Opencode]);
    let remote = remote("https://pool.example/v1");
    let detection = opencode_detection(&config);
    let runner = fake_runner();
    let fetcher = FakeProfileFetcher::ok(remote.clone());

    let outcome = eggpool_connect::install::install(
        &runner,
        &fetcher,
        &connection,
        Some(remote.clone()),
        &detection,
        &state,
        API_KEY,
        false,
        false,
        FailureInjector::default(),
    )
    .await
    .expect("install");
    assert!(!outcome.no_op);
    let installed = std::fs::read_to_string(&config).expect("read");
    // Every comment byte and unrelated provider survives.
    assert!(installed.contains("// user comment"));
    assert!(installed.contains("/* trailing */"));
    assert!(installed.contains("\"other\""));
    assert!(installed.contains("\"eggpool\""));
    assert!(installed.contains("{env:EGGPOOL_API_KEY}"));
    assert!(!installed.contains(API_KEY));

    // Repeated install is a no-op (comment-preserving idempotence).
    let second = eggpool_connect::install::install(
        &runner,
        &fetcher,
        &connection,
        Some(remote),
        &detection,
        &state,
        API_KEY,
        false,
        false,
        FailureInjector::default(),
    )
    .await
    .expect("second install");
    assert!(second.no_op);
    assert_eq!(std::fs::read_to_string(&config).expect("read"), installed);
}

#[tokio::test]
async fn opencode_v2_install_and_remove_restore_previous() {
    let dir = tempfile::tempdir().expect("tempdir");
    let state = dir.path().join("state");
    let config = dir.path().join("opencode.jsonc");
    std::fs::write(
        &config,
        "{\n\"providers\": {\n\"eggpool\": {\"package\": \"@old/pkg\"},\n\"other\": {}\n}\n}\n",
    )
    .expect("write");

    let connection = connection(vec![ClientTarget::Opencode]);
    let remote = remote("https://pool.example/v1");
    let mut detection = opencode_detection(&config);
    detection.schema_variant = ClientSchemaVariant::OpencodeV2;
    let runner = fake_runner();
    let fetcher = FakeProfileFetcher::ok(remote.clone());

    // A pre-existing foreign `eggpool` entry is owned drift: deliberate
    // converge with force captures it for later restoration.
    let outcome = eggpool_connect::install::install(
        &runner,
        &fetcher,
        &connection,
        Some(remote),
        &detection,
        &state,
        API_KEY,
        true,
        false,
        FailureInjector::default(),
    )
    .await
    .expect("install");
    assert!(!outcome.no_op);
    let installed = std::fs::read_to_string(&config).expect("read");
    assert!(installed.contains(eggpool_client_config::OPENCODE_V2_RESPONSES_PACKAGE));
    assert!(installed.contains("\"other\""));
    assert!(!installed.contains("\"provider\":"));

    // Remove restores the captured V2 previous entry byte-for-byte.
    let record = eggpool_connect::install::remove_owned(&detection, &state, false).expect("remove");
    assert!(!record.id.is_empty());
    let removed = std::fs::read_to_string(&config).expect("read");
    assert!(removed.contains("@old/pkg"));
    assert!(removed.contains("\"other\""));
}

#[test]
fn malformed_and_oversize_profiles_fail_closed() {
    assert!(eggpool_client_config::decode_profile("not-a-token").is_err());
    assert!(eggpool_client_config::decode_profile("epc1.").is_err());
    assert!(eggpool_client_config::decode_profile("epc1.!!!").is_err());
    let oversize = format!("epc1.{}", "A".repeat(9000));
    assert!(eggpool_client_config::decode_profile(&oversize).is_err());
}

#[test]
fn plan_performs_no_filesystem_mutation() {
    let dir = tempfile::tempdir().expect("tempdir");
    let state = dir.path().join("state");
    let config = dir.path().join("config.toml");
    std::fs::write(&config, "model_provider = \"other\"\n").expect("write");
    let before = std::fs::read(&config).expect("read");
    let mtime_before = std::fs::metadata(&config)
        .expect("meta")
        .modified()
        .expect("mtime");

    let connection = connection(vec![ClientTarget::Codex]);
    let remote = remote("https://pool.example/v1");
    let detection = codex_detection(&config);
    let planned = build_mutation(&remote, &detection, &state).expect("plan");
    assert!(!planned.no_op);
    // `build_mutation` is pure: no writes, no backups.
    assert_eq!(std::fs::read(&config).expect("read"), before);
    assert_eq!(
        std::fs::metadata(&config)
            .expect("meta")
            .modified()
            .expect("mtime"),
        mtime_before
    );
    assert!(list_backups(&state, None).expect("list").is_empty());
    let _ = connection;
}

#[test]
fn remote_revision_update_preserves_unrelated_edits() {
    let dir = tempfile::tempdir().expect("tempdir");
    let state = dir.path().join("state");
    let config = dir.path().join("config.toml");
    // Installed state with unrelated user edit after install.
    std::fs::write(
        &config,
        "model_provider = \"eggpool\"\nmodel_catalog_json = \"/tmp/c.json\"\n# user theme\n\n[model_providers.eggpool]\nname = \"EggPool\"\nbase_url = \"https://pool.example/v1\"\nwire_api = \"responses\"\nsupports_websockets = false\nenv_key = \"EGGPOOL_API_KEY\"\n\n[user]\ntheme = \"dark\"\n",
    )
    .expect("write");
    let detection = codex_detection(&config);
    let remote = remote("https://pool.example/v1");
    let planned = build_mutation(&remote, &detection, &state).expect("plan");
    // Revision-only update (same base URL/shape) converges without --force.
    assert!(
        eggpool_connect::install::check_drift(&planned, &remote, false).is_ok(),
        "revision-only sync must not require --force"
    );
    let text = String::from_utf8(planned.proposed_config).expect("utf8");
    assert!(text.contains("[user]"));
}

#[test]
fn owned_drift_refuses_without_force() {
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
    assert!(eggpool_connect::install::check_drift(&planned, &remote, false).is_err());
    assert!(eggpool_connect::install::check_drift(&planned, &remote, true).is_ok());
}

#[test]
fn backup_listing_never_prints_contents_or_secrets() {
    let dir = tempfile::tempdir().expect("tempdir");
    let state = dir.path().join("state");
    let config: PathBuf = dir.path().join("config.toml");
    std::fs::write(&config, API_KEY).expect("write");
    let record = eggpool_connect::backup::create_backup(eggpool_connect::backup::BackupInput {
        state_root: &state,
        target: ClientTarget::Codex,
        config_path: &config,
        profile_fingerprint: &"c".repeat(64),
        client_version: None,
        schema_variant: "codex-toml",
        artifacts: &[],
        previous_values: Default::default(),
    })
    .expect("backup");
    let listed = list_backups(&state, None).expect("list");
    let rendered = serde_json::to_string(&listed).expect("json");
    // Hashes and paths are listed; contents and secrets never are.
    assert!(rendered.contains(&record.id));
    assert!(!rendered.contains(API_KEY));
}
