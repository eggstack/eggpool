use eggpool::{
    db::{Database, DatabaseConfig, MigrationRunner},
    identity,
    model_policy::{ModelPolicyError, ModelPolicyService},
    web_session,
};

async fn database() -> Database {
    let database = Database::open(DatabaseConfig::default())
        .await
        .expect("in-memory database opens");
    MigrationRunner::new(&database)
        .run()
        .await
        .expect("schema migrates");
    database
}

async fn add_user(database: &Database, id: &'static str, role: &'static str) {
    database
        .call(move |connection| {
            connection.execute(
                "INSERT INTO users(id, username, normalized_username, role, password_hash)\
                 VALUES (?1, ?1, ?1, ?2, 'test-hash')",
                (id, role),
            )?;
            Ok(())
        })
        .await
        .expect("user inserts");
}

#[tokio::test]
async fn profiles_are_default_deny_and_precedence_is_hard_deny_user_profile_default() {
    let database = database().await;
    add_user(&database, "admin-a", "admin").await;
    add_user(&database, "member-a", "member").await;
    let policy = ModelPolicyService::new(database.clone());

    let admin = policy.snapshot("admin-a").await.unwrap();
    let unassigned_member = policy.snapshot("member-a").await.unwrap();
    assert!(admin.allows("unlisted-model", None));
    assert!(!unassigned_member.allows("unlisted-model", None));

    policy
        .create_profile("restricted", "Restricted", "deny", "admin-a")
        .await
        .unwrap();
    policy
        .set_profile_rule("restricted", "model-a", "allow", "admin-a")
        .await
        .unwrap();
    policy
        .set_profile_rule("restricted", "model-a/provider-a", "deny", "admin-a")
        .await
        .unwrap();
    policy
        .set_profile_preference("restricted", Some("model-a"), "admin-a")
        .await
        .unwrap();
    let denied_preference = policy
        .set_profile_preference("restricted", Some("model-b"), "admin-a")
        .await;
    assert!(
        matches!(
            denied_preference,
            Err(ModelPolicyError::PreferredModelDenied)
        ),
        "unexpected preferred-model result: {denied_preference:?}"
    );
    policy
        .assign_profile("member-a", "restricted", "admin-a")
        .await
        .unwrap();

    let profile = policy.snapshot("member-a").await.unwrap();
    assert_eq!(profile.preferred_model_id(), Some("model-a"));
    assert!(profile.allows("model-a", None));
    assert!(!profile.allows("model-b", None));
    assert!(!profile.allows("model-a", Some("provider-a")));

    policy
        .set_user_override("member-a", "model-a", "allow", "admin-a")
        .await
        .unwrap();
    let user_override = policy.snapshot("member-a").await.unwrap();
    assert!(user_override.revision() > profile.revision());
    assert!(user_override.allows("model-a", Some("provider-a")));
    assert!(
        profile.allows("model-a", None),
        "old snapshots stay immutable"
    );

    policy
        .set_instance_denial("model-a", "admin-a", true)
        .await
        .unwrap();
    let denied = policy.snapshot("member-a").await.unwrap();
    assert!(!denied.allows("model-a", None));
    assert!(!denied.allows("model-a", Some("provider-a")));
    assert!(admin.allows("model-a", None), "admitted policy is frozen");

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
    assert!(matches!(
        policy.snapshot("member-a").await,
        Err(ModelPolicyError::UserUnavailable)
    ));
    database.close().await.unwrap();
}

#[tokio::test]
async fn wildcard_and_noncanonical_policy_keys_are_rejected() {
    let database = database().await;
    add_user(&database, "admin-a", "admin").await;
    let policy = ModelPolicyService::new(database.clone());
    assert!(
        policy
            .create_profile("restricted", &"x".repeat(65), "deny", "admin-a")
            .await
            .is_err()
    );
    assert!(
        policy
            .set_profile_rule("restricted", "model-*", "allow", "admin-a")
            .await
            .is_err()
    );
    assert!(
        policy
            .set_profile_rule("restricted", " model-a", "allow", "admin-a")
            .await
            .is_err()
    );
    database.close().await.unwrap();
}

#[tokio::test]
async fn concurrent_policy_edit_keeps_each_snapshot_consistent_and_refreshes_new_work() {
    let database = database().await;
    add_user(&database, "admin-a", "admin").await;
    add_user(&database, "member-a", "member").await;
    let policy = ModelPolicyService::new(database.clone());
    let before = policy.snapshot("member-a").await.unwrap();
    assert!(!before.allows("model-a", None));

    let (edit, observed) = tokio::join!(
        policy.set_user_override("member-a", "model-a", "allow", "admin-a"),
        policy.snapshot("member-a"),
    );
    edit.unwrap();
    let observed = observed.unwrap();
    let after = policy.snapshot("member-a").await.unwrap();
    if observed.revision() == before.revision() {
        assert!(!observed.allows("model-a", None));
    } else {
        assert!(observed.allows("model-a", None));
    }
    assert!(after.revision() > before.revision());
    assert!(after.allows("model-a", None));
    assert!(
        !before.allows("model-a", None),
        "in-flight policy is immutable"
    );
    database.close().await.unwrap();
}

#[tokio::test]
async fn revoked_admin_session_cannot_commit_a_policy_mutation() {
    let database = database().await;
    identity::mark_fresh_uninitialized(&database).await.unwrap();
    let (admin, _) = identity::bootstrap_admin(
        &database,
        "policy-admin",
        "M015 admin password before reset",
    )
    .await
    .unwrap();
    let issued = web_session::issue(&database, &admin.id).await.unwrap();
    let stale_principal = web_session::authenticate(&database, &issued.session_token)
        .await
        .unwrap()
        .expect("browser session is initially active");

    identity::reset_password(
        &database,
        &admin.username,
        "M015 admin password after reset",
    )
    .await
    .unwrap();
    let result = ModelPolicyService::new(database.clone())
        .with_browser_admin_session(&stale_principal)
        .create_profile(
            "stale-session-profile",
            "Stale session profile",
            "deny",
            &admin.id,
        )
        .await;
    assert!(matches!(result, Err(ModelPolicyError::Database(_))));
    let profile_exists = database
        .call(|connection| {
            connection.query_row(
                "SELECT EXISTS(SELECT 1 FROM policy_profiles WHERE id = 'stale-session-profile')",
                [],
                |row| row.get::<_, bool>(0),
            )
        })
        .await
        .unwrap();
    assert!(
        !profile_exists,
        "revoked session must not commit policy state"
    );
    database.close().await.unwrap();
}
