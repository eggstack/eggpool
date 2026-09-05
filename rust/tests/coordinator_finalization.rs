use std::collections::BTreeSet;

use eggpool::{
    Config,
    accounts::{AccountRegistry, CredentialStore},
    catalog::{ModelCatalogCache, ModelInput, ProtocolResolutionStatus},
    coordinator::{
        DurableFinalizer, FinalizationCommand, FinalizationData, FinalizationError,
        FinalizationOutcome, FinalizationSupervisor, PublicationInput, PublicationOutcome,
        PublicationService, RetryPolicy, classify,
    },
    db::{Account, Database, DatabaseConfig, MigrationRunner},
    quota::{AccountQuota, QuotaEstimator},
    routing::{EligibilityPolicy, RoutingRequestFacts, RoutingRouter},
};

struct Fixture {
    database: Database,
    router: RoutingRouter,
    estimator: QuotaEstimator,
}

async fn fixture() -> Fixture {
    let database = Database::open(DatabaseConfig::default())
        .await
        .expect("database opens");
    MigrationRunner::new(&database)
        .run()
        .await
        .expect("migrations apply");
    database
        .call(|connection| {
            connection.execute(
                "INSERT INTO accounts (id, name, api_key_env, enabled, provider_id)
                 VALUES (1, 'account-a', 'UNUSED', 1, 'provider-a')",
                [],
            )?;
            connection.execute(
                "INSERT INTO models (model_id, protocol, provider_id, resolution_status)
                 VALUES ('model-a', 'openai', 'provider-a', 'resolved')",
                [],
            )?;
            Ok(())
        })
        .await
        .expect("fixture rows insert");
    let mut config = Config::default();
    let mut provider = eggpool::config::ProviderConfig {
        id: "provider-a".into(),
        base_url: "https://provider.invalid/v1".into(),
        protocols: vec!["openai".into()],
        auth: eggpool::config::ProviderAuthConfig {
            mode: "none".into(),
            ..Default::default()
        },
        ..Default::default()
    };
    provider.accounts.push(eggpool::config::AccountConfig {
        name: "account-a".into(),
        ..Default::default()
    });
    config.providers.insert("provider-a".into(), provider);
    config.validate().expect("fixture config validates");
    let registry = AccountRegistry::from_config(
        &config,
        &[Account {
            id: 1,
            name: "account-a".into(),
            api_key_env: "UNUSED".into(),
            enabled: true,
            weight: 1.0,
            provider_id: "provider-a".into(),
        }],
        &CredentialStore::default(),
    )
    .expect("registry builds");
    let mut catalog = ModelCatalogCache::default();
    catalog.set_account_provider("account-a", "provider-a");
    let mut model = ModelInput::new("model-a");
    model.protocol = Some("openai".into());
    model.protocol_source = Some("fixture".into());
    model.resolution_status = ProtocolResolutionStatus::Resolved;
    catalog
        .update_from_account("account-a", "provider-a", &[model], true, true)
        .expect("catalog model");
    let estimator = QuotaEstimator::new([AccountQuota::new("account-a")]);
    let router = RoutingRouter::new(
        registry,
        catalog,
        estimator.clone(),
        None,
        EligibilityPolicy::default(),
    );
    Fixture {
        database,
        router,
        estimator,
    }
}

async fn published(fixture: &Fixture, request_id: &str) -> eggpool::coordinator::PublishedAttempt {
    let mut facts = RoutingRequestFacts::new("model-a");
    facts.requested_protocol = Some("openai".into());
    facts.client_protocol = Some("openai".into());
    facts.projected_tokens = 42;
    let claim = fixture
        .router
        .select_and_claim(&facts, &BTreeSet::new())
        .await
        .expect("claim succeeds")
        .expect("candidate exists");
    let outcome = PublicationService::new(fixture.database.clone())
        .publish(
            claim,
            PublicationInput::new(request_id, "openai", "openai", false, 1),
        )
        .await
        .expect("publication succeeds");
    let PublicationOutcome::Published(value) = outcome else {
        panic!("expected published attempt");
    };
    *value
}

#[tokio::test]
async fn request_finalization_converges_rows_and_runtime_once() {
    let fixture = fixture().await;
    let published = published(&fixture, "c006-complete").await;
    let finalizer = DurableFinalizer::new(fixture.database.clone());
    let result = finalizer
        .finalize_request(
            &published.identity,
            FinalizationData {
                outcome: FinalizationOutcome::Completed,
                input_tokens: 3,
                output_tokens: 5,
                cost_microdollars: 7,
                release_reason: Some("completed".into()),
                ..FinalizationData::default()
            },
            Some(published.claim),
        )
        .await
        .expect("finalization succeeds");
    assert!(result.request_transitioned);
    assert!(result.attempt_transitioned);
    assert!(result.reservation_transitioned);
    assert!(result.runtime_released);
    assert_eq!(fixture.router.active_request_count("account-a"), 0);
    assert_eq!(
        fixture.estimator.snapshot(&["account-a".into()])["account-a"].reserved_requests,
        0
    );

    let observed = finalizer
        .finalize_request(
            &published.identity,
            FinalizationData {
                outcome: FinalizationOutcome::Completed,
                release_reason: Some("completed".into()),
                ..FinalizationData::default()
            },
            None,
        )
        .await
        .expect("duplicate compatible finalization observes convergence");
    assert!(!observed.request_transitioned);
    let conflict = finalizer
        .finalize_request(
            &published.identity,
            FinalizationData {
                outcome: FinalizationOutcome::ClientError,
                ..FinalizationData::default()
            },
            None,
        )
        .await
        .expect_err("incompatible terminal outcome must fail closed");
    assert!(matches!(
        conflict,
        FinalizationError::TerminalConflict { .. }
    ));
    fixture.database.close().await.expect("database closes");
}

#[tokio::test]
async fn failed_attempt_cleanup_leaves_request_retryable() {
    let fixture = fixture().await;
    let published = published(&fixture, "c006-retry").await;
    let finalizer = DurableFinalizer::new(fixture.database.clone());
    let result = finalizer
        .finalize_failed_attempt(
            &published.identity,
            FinalizationData {
                outcome: FinalizationOutcome::UpstreamError,
                status_code: Some(503),
                error_class: Some("temporary".into()),
                error_detail: Some("secret\0provider detail".into()),
                release_reason: Some("retryable".into()),
                ..FinalizationData::default()
            },
            Some(published.claim),
        )
        .await
        .expect("failed attempt cleanup succeeds");
    assert!(result.attempt_terminal);
    assert!(!result.request_terminal);
    let rows = fixture
        .database
        .call(|connection| {
            Ok((
                connection.query_row("SELECT status FROM requests WHERE id = 1", [], |row| {
                    row.get::<_, String>(0)
                })?,
                connection.query_row(
                    "SELECT completed_at IS NOT NULL FROM request_attempts WHERE id = 1",
                    [],
                    |row| row.get::<_, i64>(0),
                )?,
                connection.query_row(
                    "SELECT status FROM reservations WHERE id = 1",
                    [],
                    |row| row.get::<_, String>(0),
                )?,
                connection.query_row(
                    "SELECT error_detail FROM request_attempts WHERE id = 1",
                    [],
                    |row| row.get::<_, Option<String>>(0),
                )?,
            ))
        })
        .await
        .expect("lifecycle rows");
    assert_eq!(rows.0, "pending");
    assert_eq!(rows.1, 1);
    assert_eq!(rows.2, "released");
    assert!(!rows.3.unwrap().contains('\0'));
    fixture.database.close().await.expect("database closes");
}

#[tokio::test]
async fn retained_supervisor_shares_duplicate_job_and_is_bounded() {
    let fixture = fixture().await;
    let published = published(&fixture, "c006-supervisor").await;
    let finalizer = DurableFinalizer::new(fixture.database.clone());
    let supervisor = FinalizationSupervisor::with_capacity(finalizer, 1);
    let command = FinalizationCommand::Request {
        identity: published.identity.clone(),
        data: FinalizationData {
            outcome: FinalizationOutcome::Completed,
            release_reason: Some("completed".into()),
            ..FinalizationData::default()
        },
        claim: Some(published.claim),
    };
    let first = supervisor.register(command.clone()).expect("register job");
    let second = supervisor.register(command).expect("duplicate shares job");
    let (first, second) = tokio::join!(first.wait(), second.wait());
    assert!(first.is_ok());
    assert!(second.is_ok());
    supervisor.drain().await;
    assert_eq!(supervisor.snapshot().active_jobs, 0);
    fixture.database.close().await.expect("database closes");
}

#[test]
fn failure_policy_keeps_wire_account_and_handoff_scopes_distinct() {
    let mut observation =
        eggpool::coordinator::FailureObservation::response(7, 1, http::StatusCode::BAD_REQUEST);
    observation.wire_rejection = true;
    assert_eq!(
        classify(&observation, RetryPolicy::default()).action,
        eggpool::coordinator::NextAction::RetryWire
    );
    observation.wire_rejection = false;
    observation.status = Some(503);
    assert_eq!(
        classify(&observation, RetryPolicy::default()).retry_scope,
        eggpool::coordinator::RetryScope::Account
    );
    observation.response_started = true;
    assert_eq!(
        classify(&observation, RetryPolicy::default()).action,
        eggpool::coordinator::NextAction::Complete
    );
}
