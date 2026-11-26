use std::path::PathBuf;

use std::process::Command;

use eggpool::config::{Config, ConfigError};

const REMOVED_MODULES: &[&str] = &[
    "oauth.rs",
    "operations/oauth_login.rs",
    "server/oauth_credentials.rs",
    "server/oauth_login.rs",
    "coordinator/oauth_integration_tests.rs",
];

#[test]
fn removed_oauth_surface_stays_absent() {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("src");
    for relative in REMOVED_MODULES {
        assert!(
            !root.join(relative).exists(),
            "retired module returned: {relative}"
        );
    }

    let library = include_str!("../src/lib.rs");
    let operations = include_str!("../src/operations/mod.rs");
    let server = include_str!("../src/server/mod.rs");
    let identity = include_str!("../src/identity.rs");
    let config_source = include_str!("../src/config.rs");
    let cli_source = include_str!("../src/cli.rs");
    let coordinator = include_str!("../src/coordinator/mod.rs");
    let finite = include_str!("../src/coordinator/finite.rs");
    let streaming = include_str!("../src/coordinator/streaming/coordinator.rs");

    for (source, retired) in [
        (library, "mod oauth;"),
        (operations, "mod oauth_login;"),
        (server, "mod oauth_login;"),
        (server, "mod oauth_credentials;"),
        (identity, "oauth_credentials_write"),
        (identity, "OAUTH_CREDENTIALS_WRITE_SCOPE"),
        (config_source, "pub struct OAuthConfig"),
        (config_source, "AccountAuthMode::OAuth"),
        (config_source, "pub oauth:"),
        (cli_source, "oauth-login"),
        (cli_source, "OAuthLogin"),
        (coordinator, "oauth_401_for_account"),
        (coordinator, "refresh_rejected_oauth_credential"),
        (finite, "terminalize_oauth_401"),
        (streaming, "terminalize_oauth_401"),
    ] {
        assert!(
            !source.contains(retired),
            "retired surface returned: {retired}"
        );
    }
    for route in [
        "/account/tokens/oauth",
        "/api/user/oauth-credentials",
        "/api/user/oauth-credentials/hint",
        "/api/user/oauth/xai/",
        "/api/user/oauth/openai/",
        "/oauth/openai/callback",
    ] {
        assert!(!server.contains(route), "retired route returned: {route}");
    }

    let output = Command::new(env!("CARGO_BIN_EXE_eggpool"))
        .args(["accounts", "--help"])
        .output()
        .expect("invoke accounts help");
    assert!(output.status.success());
    let help = String::from_utf8(output.stdout).expect("CLI help is UTF-8");
    assert!(help.contains("migrate-oauth"));
    assert!(!help.contains("oauth-login"));

    let serialized = toml::to_string(&Config::default()).expect("default config serializes");
    assert!(!serialized.contains("\n[oauth]"));
}

#[test]
fn legacy_oauth_configuration_fails_with_the_migration_command() {
    for (source, expected_surface) in [
        ("[oauth]\nenabled = true\n", "[oauth]"),
        (
            "[[providers.xai.accounts]]\nname = \"legacy\"\nauth_mode = \"oauth\"\n",
            "account auth_mode = \"oauth\"",
        ),
    ] {
        let error = Config::from_toml_bytes("legacy.toml", source.as_bytes())
            .expect_err("legacy OAuth shape must fail closed");
        match &error {
            ConfigError::LegacyOAuth { surface } => assert_eq!(*surface, expected_surface),
            other => panic!("expected typed legacy OAuth error, got {other}"),
        }
        assert!(error.to_string().contains("eggpool accounts migrate-oauth"));
    }
}

#[test]
fn legacy_credential_directory_is_detected_and_preserved_on_config_load() {
    let directory = tempfile::tempdir().expect("temporary config directory");
    let config_path = directory.path().join("config.toml");
    std::fs::write(&config_path, "[server]\nport = 11300\n").expect("write valid config");
    let credentials = directory.path().join(".eggpool-oauth");
    std::fs::create_dir(&credentials).expect("create legacy credential directory");
    let sentinel = credentials.join("legacy-credential.json");
    std::fs::write(&sentinel, b"preserved legacy bytes").expect("write sentinel");

    assert_eq!(
        eggpool::config::legacy_oauth_directory(&config_path).as_deref(),
        Some(credentials.as_path())
    );
    Config::from_toml(&config_path).expect("legacy directory alone must not block startup");
    assert!(credentials.is_dir());
    assert_eq!(std::fs::read(sentinel).unwrap(), b"preserved legacy bytes");
}

#[test]
fn migrate_oauth_json_yes_emits_one_document_with_backup_recommendation() {
    let directory = tempfile::tempdir().expect("temporary migration root");
    let config_path = directory.path().join("config.toml");
    let database_path = directory.path().join("unused.sqlite3");
    let config = format!(
        "[database]\npath = {:?}\n[oauth]\nenabled = true\n",
        database_path.display().to_string()
    );
    std::fs::write(&config_path, config).expect("write legacy config");

    let output = Command::new(env!("CARGO_BIN_EXE_eggpool"))
        .arg("--config")
        .arg(&config_path)
        .args(["accounts", "migrate-oauth", "--json", "--yes"])
        .env("EGGPOOL_CONFIG", &config_path)
        .env("EGGPOOL_RUNTIME_DIR", directory.path().join("runtime"))
        .env("HOME", directory.path())
        .env("XDG_CONFIG_HOME", directory.path().join("xdg-config"))
        .env("XDG_DATA_HOME", directory.path().join("xdg-data"))
        .env("XDG_STATE_HOME", directory.path().join("xdg-state"))
        .output()
        .expect("invoke OAuth migration CLI");
    assert!(
        output.status.success(),
        "migration failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(output.stdout.first(), Some(&b'{'));
    let summary: serde_json::Value =
        serde_json::from_slice(&output.stdout).expect("stdout is exactly one JSON document");
    assert_eq!(summary["status"], "completed");
    assert_eq!(
        summary["backup_recommendation"],
        "Back up the config and credential directory before proceeding."
    );
}
