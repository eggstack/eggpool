use std::{fs, process::Stdio};

use eggpool::{
    Config,
    db::{Database, DatabaseConfig, MigrationRunner},
    identity::{self, access_state},
    model_policy::ModelPolicyService,
};
use tempfile::tempdir;
use tokio::{io::AsyncWriteExt, process::Command};

async fn run_cli(
    config_path: &std::path::Path,
    args: &[&str],
    password: Option<&str>,
) -> std::process::Output {
    let mut child = Command::new(env!("CARGO_BIN_EXE_eggpool"))
        .args(["--config", config_path.to_str().expect("config path")])
        .args(args)
        .env("EGGPOOL_CONFIG", config_path)
        .env_remove("EGGPOOL_API_KEY")
        .stdin(if password.is_some() {
            Stdio::piped()
        } else {
            Stdio::null()
        })
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("CLI starts");
    if let Some(password) = password {
        child
            .stdin
            .take()
            .expect("stdin pipe")
            .write_all(format!("{password}\n").as_bytes())
            .await
            .expect("password is sent through stdin");
    }
    child.wait_with_output().await.expect("CLI completes")
}

async fn open_database(path: &std::path::Path) -> Database {
    let config = DatabaseConfig {
        path: path.display().to_string(),
        ..DatabaseConfig::default()
    };
    Database::open(config).await.expect("database opens")
}

#[tokio::test]
async fn headless_member_lifecycle_is_atomic_audited_and_works_with_stale_config() {
    let directory = tempdir().expect("temporary directory");
    let database_path = directory.path().join("eggpool.sqlite3");
    let config_path = directory.path().join("config.toml");
    let mut config = Config::default();
    config.database.path = database_path.display().to_string();
    config.server.api_key = Some("legacy-shared-key-for-test-only".to_owned());
    config.dashboard.public = true;
    fs::write(
        &config_path,
        toml::to_string(&config).expect("config serializes"),
    )
    .expect("config file is written");

    let database = open_database(&database_path).await;
    MigrationRunner::new(&database)
        .run()
        .await
        .expect("schema migrates");
    identity::mark_fresh_uninitialized(&database)
        .await
        .expect("fresh state is marked");
    let (admin, admin_credential) =
        identity::bootstrap_admin(&database, "admin", "M020 strong admin password passphrase")
            .await
            .expect("first administrator bootstraps");
    ModelPolicyService::new(database.clone())
        .create_profile("restricted", "Restricted", "deny", &admin.id)
        .await
        .expect("profile is created");
    database.close().await.expect("database closes before CLI");

    let invalid_profile = run_cli(
        &config_path,
        &[
            "users",
            "add",
            "member",
            "--password-stdin",
            "--profile-id",
            "missing-profile",
        ],
        Some("M020 temporary member password"),
    )
    .await;
    assert!(!invalid_profile.status.success());
    let database = open_database(&database_path).await;
    assert_eq!(access_state(&database).await.unwrap(), "admin_initialized");
    assert_eq!(identity::list_users(&database).await.unwrap().len(), 1);
    assert!(
        identity::verify_api_token(&database, &admin_credential.token)
            .await
            .unwrap()
    );
    let failed_transition_audit = identity::admin_audit(&database, 20).await.unwrap();
    assert!(!failed_transition_audit.iter().any(|entry| {
        matches!(
            entry.action.as_str(),
            "enable_managed" | "create_user" | "assign_profile"
        )
    }));
    database.close().await.expect("database closes");

    let default_set = run_cli(
        &config_path,
        &["users", "defaults", "set", "restricted"],
        None,
    )
    .await;
    assert!(
        default_set.status.success(),
        "{}",
        String::from_utf8_lossy(&default_set.stderr)
    );
    let default_shown = run_cli(&config_path, &["users", "defaults", "show"], None).await;
    assert!(default_shown.status.success());
    assert!(String::from_utf8_lossy(&default_shown.stdout).contains("restricted"));

    let created = run_cli(
        &config_path,
        &["users", "add", "member", "--password-stdin"],
        Some("M020 temporary member password"),
    )
    .await;
    assert!(
        created.status.success(),
        "{}",
        String::from_utf8_lossy(&created.stderr)
    );
    assert!(!String::from_utf8_lossy(&created.stdout).contains("M020 temporary member password"));
    assert!(String::from_utf8_lossy(&created.stderr).contains("configuration hygiene"));

    let shown = run_cli(&config_path, &["users", "show", "member"], None).await;
    assert!(shown.status.success());
    let shown_stdout = String::from_utf8_lossy(&shown.stdout);
    assert!(shown_stdout.contains("role: member"));
    assert!(shown_stdout.contains("state: active"));
    assert!(shown_stdout.contains("must_change_password: true"));
    assert!(!shown_stdout.contains("password_hash"));

    let token = run_cli(
        &config_path,
        &["users", "token", "member", "--show-secret"],
        None,
    )
    .await;
    assert!(token.status.success());
    let token_stdout = String::from_utf8_lossy(&token.stdout);
    let personal_token = token_stdout
        .split_whitespace()
        .find(|value| value.starts_with("epk1_"))
        .expect("one-time member token is printed")
        .to_owned();

    let disabled = run_cli(&config_path, &["users", "disable", "member"], None).await;
    assert!(disabled.status.success());
    let enabled = run_cli(&config_path, &["users", "enable", "member"], None).await;
    assert!(enabled.status.success());
    assert!(
        String::from_utf8_lossy(&enabled.stdout)
            .contains("Previously revoked credentials stay revoked")
    );

    let no_profile = run_cli(
        &config_path,
        &[
            "users",
            "add",
            "explicit-none",
            "--password-stdin",
            "--no-profile",
        ],
        Some("M021 explicit no-profile password"),
    )
    .await;
    assert!(
        no_profile.status.success(),
        "{}",
        String::from_utf8_lossy(&no_profile.stderr)
    );
    let default_clear = run_cli(&config_path, &["users", "defaults", "clear"], None).await;
    assert!(
        default_clear.status.success(),
        "{}",
        String::from_utf8_lossy(&default_clear.stderr)
    );
    let default_shown = run_cli(&config_path, &["users", "defaults", "show"], None).await;
    assert!(default_shown.status.success());
    assert!(
        String::from_utf8_lossy(&default_shown.stdout).contains("default_member_profile: none")
    );

    let duplicate = run_cli(
        &config_path,
        &["users", "add", "member", "--password-stdin"],
        Some("M020 another member password"),
    )
    .await;
    assert!(!duplicate.status.success());
    let last_admin = run_cli(&config_path, &["users", "disable", "admin"], None).await;
    assert!(!last_admin.status.success());

    let idempotent_transition = run_cli(&config_path, &["users", "enable-managed"], None).await;
    assert!(
        idempotent_transition.status.success(),
        "{}",
        String::from_utf8_lossy(&idempotent_transition.stderr)
    );

    let database = open_database(&database_path).await;
    assert_eq!(access_state(&database).await.unwrap(), "managed");
    let member = identity::get_user_by_name(&database, "member")
        .await
        .unwrap()
        .expect("member is present");
    assert_eq!(member.state, "active");
    assert!(member.must_change_password);
    assert!(
        !identity::verify_api_token(&database, &personal_token)
            .await
            .unwrap(),
        "disabled credentials remain revoked after reactivation"
    );
    let (assigned_profile, explicit_none_assignment, audit_actions) = database
        .call(move |connection| {
            let profile: String = connection.query_row(
                "SELECT profile_id FROM user_profile_assignments WHERE user_id = ?1",
                [&member.id],
                |row| row.get(0),
            )?;
            let explicit_none: bool = connection.query_row(
                "SELECT EXISTS(SELECT 1 FROM user_profile_assignments a JOIN users u ON u.id = a.user_id WHERE u.username = 'explicit-none')",
                [],
                |row| row.get(0),
            )?;
            let mut statement =
                connection.prepare("SELECT action FROM user_admin_audit ORDER BY id")?;
            let actions = statement
                .query_map([], |row| row.get::<_, String>(0))?
                .collect::<Result<Vec<_>, _>>()?;
            Ok((profile, explicit_none, actions))
        })
        .await
        .expect("profile and audit evidence are readable");
    assert_eq!(assigned_profile, "restricted");
    assert!(
        !explicit_none_assignment,
        "--no-profile bypasses the configured default"
    );
    assert!(
        audit_actions
            .iter()
            .any(|action| action == "enable_managed")
    );
    assert!(audit_actions.iter().any(|action| action == "create_user"));
    assert!(
        audit_actions
            .iter()
            .any(|action| action == "assign_profile")
    );
    assert!(audit_actions.iter().any(|action| action == "disable_user"));
    assert!(
        audit_actions
            .iter()
            .any(|action| action == "reactivate_user")
    );
    database.close().await.expect("database closes");
}

#[tokio::test]
async fn enable_managed_uses_the_same_cutover_with_stale_config_settings() {
    let directory = tempdir().expect("temporary directory");
    let database_path = directory.path().join("eggpool.sqlite3");
    let config_path = directory.path().join("config.toml");
    let mut config = Config::default();
    config.database.path = database_path.display().to_string();
    config.server.api_key = Some("legacy-shared-key-for-test-only".to_owned());
    config.dashboard.public = true;
    fs::write(
        &config_path,
        toml::to_string(&config).expect("config serializes"),
    )
    .expect("config file is written");

    let database = open_database(&database_path).await;
    MigrationRunner::new(&database)
        .run()
        .await
        .expect("schema migrates");
    identity::mark_fresh_uninitialized(&database)
        .await
        .expect("fresh state is marked");
    identity::bootstrap_admin(&database, "owner", "M020 strong admin password passphrase")
        .await
        .expect("administrator bootstraps");
    database.close().await.expect("database closes before CLI");

    let output = run_cli(
        &config_path,
        &["users", "enable-managed", "--actor", "owner"],
        None,
    )
    .await;
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(String::from_utf8_lossy(&output.stderr).contains("configuration hygiene"));

    let database = open_database(&database_path).await;
    assert_eq!(access_state(&database).await.unwrap(), "managed");
    let audit = identity::admin_audit(&database, 10).await.unwrap();
    assert_eq!(audit.len(), 1);
    assert_eq!(audit[0].action, "enable_managed");
    database.close().await.expect("database closes");
}
