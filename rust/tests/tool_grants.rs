use eggpool::{
    Config,
    db::{Database, DatabaseConfig, MigrationRunner},
    identity,
    model_policy::ModelPolicyService,
    operations::backup::BackupService,
    tool_policy::{DISCOVERY_SCOPE, GrantEffect, INVOCATION_SCOPE, ToolAction, ToolPolicyService},
};

#[tokio::test]
async fn tool_grants_survive_backup_and_restore() {
    let root = tempfile::tempdir().unwrap();
    let database_path = root.path().join("eggpool.db");
    let config_path = root.path().join("config.toml");
    let mut config = Config::default();
    config.database.path = database_path.to_string_lossy().into_owned();
    std::fs::write(&config_path, toml::to_string(&config).unwrap()).unwrap();
    let database = Database::open(DatabaseConfig {
        path: config.database.path.clone(),
        ..DatabaseConfig::default()
    })
    .await
    .unwrap();
    MigrationRunner::new(&database).run().await.unwrap();
    add_user(&database, "backup-admin", "backup-admin", "admin").await;
    add_user(&database, "backup-member", "backup-member", "member").await;
    let model_policy = ModelPolicyService::new(database.clone());
    model_policy
        .create_profile("retrieval", "Retrieval", "deny", "backup-admin")
        .await
        .unwrap();
    model_policy
        .assign_profile("backup-member", "retrieval", "backup-admin")
        .await
        .unwrap();
    ToolPolicyService::new(database.clone())
        .set_profile_rule(
            "retrieval",
            "web_search",
            Some(GrantEffect::Allow),
            None,
            "backup-admin",
        )
        .await
        .unwrap();

    let backup = BackupService::from_config(&config_path, &config)
        .with_output_dir(root.path().join("backups"));
    let archive = backup.create(&database).await.unwrap().archive;
    database.close().await.unwrap();
    backup.recover(&archive).unwrap();

    let restored = Database::open(DatabaseConfig {
        path: config.database.path,
        ..DatabaseConfig::default()
    })
    .await
    .unwrap();
    MigrationRunner::new(&restored).run().await.unwrap();
    let policy = ToolPolicyService::new(restored.clone());
    assert_eq!(
        policy
            .effective_tools_for_user("backup-member")
            .await
            .unwrap(),
        vec!["web_search"]
    );
    restored.close().await.unwrap();
}

async fn database() -> Database {
    let database = Database::open(DatabaseConfig::default())
        .await
        .expect("in-memory database opens");
    MigrationRunner::new(&database)
        .run()
        .await
        .expect("migrates");
    database
}

async fn add_user(
    database: &Database,
    id: &'static str,
    username: &'static str,
    role: &'static str,
) {
    database.call(move |connection| {
        connection.execute(
            "INSERT INTO users(id, username, normalized_username, role, password_hash) VALUES (?1, ?2, ?2, ?3, 'test-hash')",
            (id, username, role),
        )?;
        Ok(())
    }).await.expect("user inserts");
}

#[tokio::test]
async fn grants_scopes_precedence_and_revocation_fail_closed() {
    let database = database().await;
    add_user(&database, "admin-a", "admin", "admin").await;
    add_user(&database, "member-a", "member", "member").await;
    add_user(&database, "member-b", "member-b", "member").await;
    let model_policy = ModelPolicyService::new(database.clone());
    model_policy
        .create_profile("restricted", "Restricted", "deny", "admin-a")
        .await
        .unwrap();
    model_policy
        .assign_profile("member-a", "restricted", "admin-a")
        .await
        .unwrap();
    model_policy
        .create_profile("blocked", "Blocked", "deny", "admin-a")
        .await
        .unwrap();
    model_policy
        .assign_profile("member-b", "blocked", "admin-a")
        .await
        .unwrap();

    let policy = ToolPolicyService::new(database.clone());
    let no_scope = identity::issue_api_token(&database, "member", "old token")
        .await
        .unwrap();
    let discovery =
        identity::issue_api_token_with_tool_scopes(&database, "member", "discovery", true, false)
            .await
            .unwrap();
    let invocation =
        identity::issue_api_token_with_tool_scopes(&database, "member", "invocation", false, true)
            .await
            .unwrap();
    let no_scope = identity::verify_api_token_principal(&database, &no_scope.token)
        .await
        .unwrap()
        .unwrap();
    let discovery = identity::verify_api_token_principal(&database, &discovery.token)
        .await
        .unwrap()
        .unwrap();
    let invocation = identity::verify_api_token_principal(&database, &invocation.token)
        .await
        .unwrap()
        .unwrap();
    let member_b_token =
        identity::issue_api_token_with_tool_scopes(&database, "member-b", "isolated", false, true)
            .await
            .unwrap();
    let member_b = identity::verify_api_token_principal(&database, &member_b_token.token)
        .await
        .unwrap()
        .unwrap();

    policy
        .set_profile_rule(
            "restricted",
            "web_search",
            Some(GrantEffect::Allow),
            None,
            "admin-a",
        )
        .await
        .unwrap();
    assert!(
        !policy
            .authorize_tool(&no_scope, "eggsearch", "web_search", ToolAction::Invoke)
            .await
            .unwrap()
    );
    assert!(
        !policy
            .authorize_tool(&discovery, "eggsearch", "web_search", ToolAction::Invoke)
            .await
            .unwrap()
    );
    assert!(
        !policy
            .authorize_tool(&invocation, "eggsearch", "web_search", ToolAction::Discover)
            .await
            .unwrap()
    );
    assert!(
        policy
            .authorize_tool(&invocation, "eggsearch", "web_search", ToolAction::Invoke)
            .await
            .unwrap()
    );
    assert!(
        !policy
            .authorize_tool(&member_b, "eggsearch", "web_search", ToolAction::Invoke)
            .await
            .unwrap()
    );

    policy
        .set_user_rule("member", "web_search", Some(GrantEffect::Deny), "admin-a")
        .await
        .unwrap();
    assert!(
        !policy
            .authorize_tool(&invocation, "eggsearch", "web_search", ToolAction::Invoke)
            .await
            .unwrap()
    );
    policy
        .set_user_rule("member", "web_search", Some(GrantEffect::Allow), "admin-a")
        .await
        .unwrap();
    assert!(
        policy
            .authorize_tool(&invocation, "eggsearch", "web_search", ToolAction::Invoke)
            .await
            .unwrap()
    );
    policy
        .set_instance_denial("web_search", true, "admin-a")
        .await
        .unwrap();
    assert!(
        !policy
            .authorize_tool(&invocation, "eggsearch", "web_search", ToolAction::Invoke)
            .await
            .unwrap()
    );
    policy
        .set_instance_denial("web_search", false, "admin-a")
        .await
        .unwrap();
    assert!(
        policy
            .authorize_tool(&invocation, "eggsearch", "web_search", ToolAction::Invoke)
            .await
            .unwrap()
    );

    database
        .call(|connection| {
            connection.execute(
                "UPDATE users SET state = 'disabled' WHERE id = 'member-a'",
                [],
            )?;
            Ok(())
        })
        .await
        .unwrap();
    assert!(
        !policy
            .authorize_tool(&invocation, "eggsearch", "web_search", ToolAction::Invoke)
            .await
            .unwrap()
    );
    database
        .call(|connection| {
            connection.execute(
                "UPDATE users SET state = 'active' WHERE id = 'member-a'",
                [],
            )?;
            Ok(())
        })
        .await
        .unwrap();

    assert!(
        !policy
            .authorize_tool(&invocation, "other", "web_search", ToolAction::Invoke)
            .await
            .unwrap()
    );
    assert!(
        !policy
            .authorize_tool(&invocation, "eggsearch", "WEB_SEARCH", ToolAction::Invoke)
            .await
            .unwrap()
    );
    assert_eq!(
        discovery
            .scopes
            .iter()
            .filter(|scope| scope.as_str() == DISCOVERY_SCOPE)
            .count(),
        1
    );
    assert!(
        !discovery
            .scopes
            .iter()
            .any(|scope| scope == INVOCATION_SCOPE)
    );
    assert_eq!(
        policy.effective_tools_for_user("member").await.unwrap(),
        vec!["web_search"]
    );
    let audit: Vec<(String, String, String)> = database
        .call(|connection| {
            let mut statement = connection.prepare(
                "SELECT action, target_kind, target_id FROM user_admin_audit WHERE action LIKE '%tool%' ORDER BY id",
            )?;
            statement
                .query_map([], |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)))?
                .collect::<Result<Vec<_>, _>>()
        })
        .await
        .unwrap();
    assert!(audit.len() >= 4);
    assert!(audit.iter().all(|(_, _, target)| {
        target.is_ascii() && !target.contains('?') && !target.contains("http")
    }));

    identity::revoke_api_tokens(&database, "member")
        .await
        .unwrap();
    assert!(
        !policy
            .authorize_tool(&invocation, "eggsearch", "web_search", ToolAction::Invoke)
            .await
            .unwrap()
    );
    database.close().await.unwrap();
}

#[tokio::test]
async fn unknown_grants_and_unicode_or_wildcard_names_are_rejected() {
    let database = database().await;
    add_user(&database, "admin-a", "admin", "admin").await;
    ModelPolicyService::new(database.clone())
        .create_profile("restricted", "Restricted", "deny", "admin-a")
        .await
        .unwrap();
    let policy = ToolPolicyService::new(database.clone());
    for name in ["*", "WEB_SEARCH", "web_search\u{212a}", "missing_tool"] {
        assert!(
            policy
                .set_profile_rule(
                    "restricted",
                    name,
                    Some(GrantEffect::Allow),
                    None,
                    "admin-a"
                )
                .await
                .is_err()
        );
    }
    assert!(
        policy
            .set_profile_rule(
                "missing",
                "web_search",
                Some(GrantEffect::Allow),
                None,
                "admin-a"
            )
            .await
            .is_err()
    );
    database.close().await.unwrap();
}

#[tokio::test]
async fn stale_profile_version_cannot_overwrite_newer_tool_policy() {
    let database = database().await;
    add_user(&database, "admin-a", "admin", "admin").await;
    ModelPolicyService::new(database.clone())
        .create_profile("restricted", "Restricted", "deny", "admin-a")
        .await
        .unwrap();
    let policy = ToolPolicyService::new(database.clone());
    let observed = eggpool::tool_policy::profile_version(&database, "restricted")
        .await
        .unwrap();
    policy
        .set_profile_rule(
            "restricted",
            "web_search",
            Some(GrantEffect::Allow),
            Some(observed),
            "admin-a",
        )
        .await
        .unwrap();
    assert!(matches!(
        policy
            .set_profile_rule(
                "restricted",
                "web_search",
                Some(GrantEffect::Deny),
                Some(observed),
                "admin-a"
            )
            .await,
        Err(eggpool::tool_policy::ToolPolicyError::VersionConflict)
    ));
    database.close().await.unwrap();
}
