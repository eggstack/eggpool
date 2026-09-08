//! O004 configuration/key/provider mutation contracts.

use std::fs;

use eggpool::operations::config_mutation::{self, ApplyMode, ApplyOutcome};
use tempfile::tempdir;

fn base_config() -> &'static str {
    "# keep this comment\n[server]\nhost = \"127.0.0.1\"\nport = 11300\n\n[database]\npath = \"usage.sqlite3\"\n"
}

#[test]
fn server_edits_preserve_unrelated_text_and_reject_invalid_candidates() {
    let directory = tempdir().expect("temporary directory");
    let path = directory.path().join("config.toml");
    fs::write(&path, base_config()).expect("config");

    config_mutation::set_server_value(&path, "host", "0.0.0.0").expect("host edit");
    let after_host = fs::read_to_string(&path).expect("updated config");
    assert!(after_host.starts_with("# keep this comment\n"));
    assert!(after_host.contains("host = \"0.0.0.0\""));
    assert!(after_host.contains("path = \"usage.sqlite3\""));

    let before_invalid = after_host.clone();
    assert!(config_mutation::set_server_value(&path, "port", "70000").is_err());
    assert_eq!(fs::read_to_string(&path).expect("config"), before_invalid);
}

#[test]
fn dashboard_edits_insert_only_the_owned_key() {
    let directory = tempdir().expect("temporary directory");
    let path = directory.path().join("config.toml");
    fs::write(&path, base_config()).expect("config");

    assert!(config_mutation::read_dashboard_public(&path).expect("default"));
    config_mutation::set_dashboard_public(&path, Some(false)).expect("dashboard edit");
    let text = fs::read_to_string(&path).expect("config");
    assert!(text.contains("[dashboard]\npublic = false"));
    assert!(!config_mutation::read_dashboard_public(&path).expect("updated"));
}

#[test]
fn key_rotation_is_random_and_env_owned_keys_are_not_replaced_inline() {
    let directory = tempdir().expect("temporary directory");
    let inline = directory.path().join("inline.toml");
    fs::write(&inline, base_config()).expect("config");
    let first = config_mutation::generate_key().expect("key");
    let second = config_mutation::generate_key().expect("key");
    assert_eq!(first.len(), 64);
    assert_ne!(first, second);
    assert!(config_mutation::write_server_key(&inline, &first).expect("write key"));
    assert_eq!(
        config_mutation::read_server_key(&inline).expect("read key"),
        Some(first)
    );

    let env_owned = directory.path().join("env.toml");
    fs::write(
        &env_owned,
        "[server]\nhost = \"127.0.0.1\"\nport = 11300\napi_key_env = \"O004_TEST_KEY\"\n",
    )
    .expect("config");
    assert!(!config_mutation::write_server_key(&env_owned, &second).expect("env-owned key"));
    assert!(
        !fs::read_to_string(&env_owned)
            .expect("config")
            .contains("api_key =")
    );
}

#[test]
fn bundled_and_custom_templates_are_structurally_loaded() {
    let bundled = config_mutation::load_provider_templates(None).expect("bundled templates");
    assert!(bundled.contains_key("opencode-go"));
    assert!(bundled.contains_key("ollama-local"));

    let directory = tempdir().expect("temporary directory");
    let path = directory.path().join("providers.toml");
    fs::write(
        &path,
        "[providers.example]\nbase_url = \"https://example.invalid/v1\"\nprotocols = [\"openai\"]\nstatus = \"experimental\"\n",
    )
    .expect("template");
    let custom = config_mutation::load_provider_templates(Some(&path)).expect("custom template");
    assert!(custom.contains_key("example"));
    assert!(custom.contains_key("opencode-go"));
}

#[test]
fn account_matching_is_secret_safe_and_apply_outcome_is_typed() {
    let directory = tempdir().expect("temporary directory");
    let path = directory.path().join("config.toml");
    fs::write(
        &path,
        concat!(
            "[providers.example]\n",
            "id = \"example\"\n",
            "base_url = \"https://example.invalid/v1\"\n",
            "protocols = [\"openai\"]\n",
            "[[providers.example.accounts]]\n",
            "name = \"example-0001\"\n",
            "api_key = \"synthetic-secret\"\n"
        ),
    )
    .expect("config");
    let matches = config_mutation::matching_accounts(&path, Some("example")).expect("matches");
    assert_eq!(matches.len(), 1);
    assert_eq!(matches[0].name, "example-0001");
    assert_eq!(
        config_mutation::redact_key("synthetic-secret"),
        "synt...cret"
    );
    let debug = format!("{:?}", matches[0]);
    assert!(!debug.contains("synthetic-secret"));
    assert!(debug.contains("synt...cret"));
    assert_eq!(ApplyMode::LiveOrReport, ApplyMode::LiveOrReport);
    assert_ne!(
        ApplyOutcome::ServerNotRunning,
        ApplyOutcome::ControlUnavailable
    );
}
