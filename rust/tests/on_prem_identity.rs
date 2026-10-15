use eggpool::{
    db::{Database, DatabaseConfig, MigrationRunner},
    identity::{self, IdentityError},
};

async fn database() -> Database {
    let database = Database::open(DatabaseConfig::default())
        .await
        .expect("in-memory DB opens");
    MigrationRunner::new(&database)
        .run()
        .await
        .expect("canonical schema migrates");
    database
}

#[tokio::test]
async fn bootstrap_is_atomic_and_personal_token_is_one_way() {
    let database = database().await;
    assert_eq!(
        identity::access_state(&database).await.unwrap(),
        "legacy_unmigrated"
    );
    identity::mark_fresh_uninitialized(&database).await.unwrap();
    assert_eq!(
        identity::access_state(&database).await.unwrap(),
        "fresh_uninitialized"
    );
    let (admin, credential) =
        identity::bootstrap_admin(&database, "admin", "correct horse battery staple")
            .await
            .expect("first admin is created");

    assert_eq!(admin.role, "admin");
    assert_eq!(
        identity::access_state(&database).await.unwrap(),
        "admin_initialized"
    );
    assert!(
        identity::verify_api_token(&database, &credential.token)
            .await
            .expect("valid token check")
    );
    assert!(
        !identity::verify_api_token(&database, "not-a-token")
            .await
            .expect("invalid token check")
    );
    assert_eq!(
        identity::verify_password(&database, "ADMIN", "correct horse battery staple")
            .await
            .expect("password check")
            .expect("admin exists")
            .id,
        admin.id
    );

    let stored_hash = database
        .call({
            let admin_id = admin.id.clone();
            move |connection| {
                connection.query_row(
                    "SELECT password_hash FROM users WHERE id = ?1",
                    [&admin_id],
                    |row| row.get::<_, String>(0),
                )
            }
        })
        .await
        .expect("hash is readable for the invariant check");
    assert!(!stored_hash.contains("correct horse battery staple"));
    assert!(!stored_hash.is_empty());
    match identity::bootstrap_admin(&database, "other", "another valid password").await {
        Err(IdentityError::AdminAlreadyInitialized) => {}
        Err(error) => panic!("unexpected bootstrap error: {error}"),
        Ok(_) => panic!("second bootstrap unexpectedly succeeded"),
    }
    assert_eq!(identity::list_users(&database).await.unwrap().len(), 1);
    database.close().await.unwrap();
}

#[tokio::test]
async fn managed_transition_requires_an_active_admin_and_audits_once() {
    let database = database().await;
    identity::mark_fresh_uninitialized(&database).await.unwrap();
    let (admin, _) = identity::bootstrap_admin(&database, "admin", "M020 strong admin password")
        .await
        .unwrap();

    assert!(matches!(
        identity::enable_managed(&database, "missing-admin").await,
        Err(IdentityError::AdministratorRequired)
    ));
    assert_eq!(
        identity::access_state(&database).await.unwrap(),
        "admin_initialized"
    );

    identity::enable_managed(&database, &admin.id)
        .await
        .unwrap();
    identity::enable_managed(&database, &admin.id)
        .await
        .unwrap();
    assert_eq!(identity::access_state(&database).await.unwrap(), "managed");
    let audit = identity::admin_audit(&database, 10).await.unwrap();
    assert_eq!(
        audit
            .iter()
            .filter(|entry| entry.action == "enable_managed")
            .count(),
        1
    );
    database.close().await.unwrap();
}

#[tokio::test]
async fn concurrent_first_member_creation_commits_one_transition_and_one_user() {
    let database = database().await;
    identity::mark_fresh_uninitialized(&database).await.unwrap();
    let (admin, _) = identity::bootstrap_admin(&database, "admin", "M020 strong admin password")
        .await
        .unwrap();

    let (first, second) = tokio::join!(
        identity::create_member(
            &database,
            &admin.id,
            "ConcurrentMember",
            "M020 concurrent member password A"
        ),
        identity::create_member(
            &database,
            &admin.id,
            "concurrentmember",
            "M020 concurrent member password B"
        )
    );
    assert!(matches!(
        (&first, &second),
        (Ok(_), Err(IdentityError::UsernameConflict))
            | (Err(IdentityError::UsernameConflict), Ok(_))
    ));
    assert_eq!(identity::access_state(&database).await.unwrap(), "managed");
    assert_eq!(identity::list_users(&database).await.unwrap().len(), 2);
    let audit = identity::admin_audit(&database, 20).await.unwrap();
    assert_eq!(
        audit
            .iter()
            .filter(|entry| entry.action == "enable_managed")
            .count(),
        1
    );
    assert_eq!(
        audit
            .iter()
            .filter(|entry| entry.action == "create_user")
            .count(),
        1
    );
    database.close().await.unwrap();
}

#[tokio::test]
async fn simultaneous_bootstrap_commits_only_one_admin() {
    let database = database().await;
    let (first, second) = tokio::join!(
        identity::bootstrap_admin(&database, "admin", "first safe password 123"),
        identity::bootstrap_admin(&database, "admin", "second safe password 456"),
    );
    assert_ne!(first.is_ok(), second.is_ok());
    assert_eq!(identity::list_users(&database).await.unwrap().len(), 1);
    database.close().await.unwrap();
}

#[tokio::test]
async fn password_reset_revokes_api_credentials_and_rejects_bad_passwords() {
    let database = database().await;
    let (_, credential) =
        identity::bootstrap_admin(&database, "admin", "initial safe password 123")
            .await
            .unwrap();
    assert!(
        identity::verify_api_token(&database, &credential.token)
            .await
            .unwrap()
    );
    assert!(matches!(
        identity::reset_password(&database, "admin", "short").await,
        Err(IdentityError::InvalidPassword)
    ));

    identity::reset_password(&database, "admin", "replacement password 456")
        .await
        .expect("password is changed");
    assert!(
        !identity::verify_api_token(&database, &credential.token)
            .await
            .unwrap()
    );
    assert!(
        identity::verify_password(&database, "admin", "replacement password 456")
            .await
            .unwrap()
            .is_some()
    );
    assert!(
        identity::verify_password(&database, "admin", "initial safe password 123")
            .await
            .unwrap()
            .is_none()
    );
    let replacement_credential = identity::issue_api_token(&database, "admin", "test client")
        .await
        .expect("personal credential can be issued");
    assert_eq!(
        identity::revoke_api_tokens(&database, "admin")
            .await
            .expect("personal credentials can be revoked"),
        1
    );
    assert!(
        !identity::verify_api_token(&database, &replacement_credential.token)
            .await
            .unwrap()
    );
    database.close().await.unwrap();
}
