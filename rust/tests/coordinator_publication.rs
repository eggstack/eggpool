use std::{
    collections::BTreeSet,
    sync::{
        Arc, Barrier,
        atomic::{AtomicBool, Ordering},
    },
};

use eggpool::{
    Config,
    accounts::{AccountRegistry, CredentialStore},
    catalog::{ModelCatalogCache, ModelInput, ProtocolResolutionStatus},
    coordinator::{
        PublicationError, PublicationFaultInjector, PublicationInput, PublicationOutcome,
        PublicationService, PublicationStage,
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
        .expect("canonical migrations apply");
    database
        .call(|connection| {
            connection.execute(
                "INSERT INTO accounts (id, name, api_key_env, enabled, provider_id)\n\
                 VALUES (1, 'account-a', 'UNUSED', 1, 'provider-a')",
                [],
            )?;
            connection.execute(
                "INSERT INTO models (model_id, protocol, provider_id, resolution_status)\n\
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

fn facts() -> RoutingRequestFacts {
    let mut facts = RoutingRequestFacts::new("model-a");
    facts.requested_protocol = Some("openai".into());
    facts.client_protocol = Some("openai".into());
    facts.projected_tokens = 42;
    facts
}

async fn claim(fixture: &Fixture) -> eggpool::routing::SelectionClaim {
    fixture
        .router
        .select_and_claim(&facts(), &BTreeSet::new())
        .await
        .expect("claim succeeds")
        .expect("fixture has a candidate")
}

async fn row_counts(database: &Database) -> (i64, i64, i64, i64) {
    database
        .call(|connection| {
            Ok((
                connection.query_row("SELECT COUNT(*) FROM requests", [], |row| row.get(0))?,
                connection.query_row("SELECT COUNT(*) FROM request_attempts", [], |row| {
                    row.get(0)
                })?,
                connection.query_row("SELECT COUNT(*) FROM reservations", [], |row| row.get(0))?,
                connection.query_row("SELECT COUNT(*) FROM routing_decisions", [], |row| {
                    row.get(0)
                })?,
            ))
        })
        .await
        .expect("row counts")
}

fn input(attempt_number: i64) -> PublicationInput {
    PublicationInput::new("proxy-c002", "openai", "openai", false, attempt_number)
}

#[tokio::test]
async fn publication_commits_all_rows_and_converts_the_claim_once() {
    let fixture = fixture().await;
    let service = PublicationService::new(fixture.database.clone());
    let outcome = service
        .publish(claim(&fixture).await, input(1))
        .await
        .expect("publication succeeds");
    let PublicationOutcome::Published(published) = outcome else {
        panic!("expected a new publication");
    };
    assert_eq!(published.identity.proxy_request_id, "proxy-c002");
    assert_eq!(published.identity.attempt_number, 1);
    assert!(published.receipt.pending_request_added);
    assert!(published.receipt.pending_tokens_added);
    assert!(published.receipt.pending_load_converted);
    assert!(published.receipt.quota_reservation_added);
    assert!(published.receipt.routing_decision_persisted);
    assert_eq!(row_counts(&fixture.database).await, (1, 1, 1, 1));
    let snapshot = fixture.estimator.snapshot(&["account-a".into()]);
    assert_eq!(snapshot["account-a"].pending_requests, 0);
    assert_eq!(snapshot["account-a"].reserved_requests, 1);

    published
        .claim
        .release_active_claim()
        .expect("later lifecycle can release the converted claim");
    assert_eq!(fixture.router.active_request_count("account-a"), 0);
    fixture.database.close().await.expect("database closes");
}

#[tokio::test]
async fn every_precommit_failure_rolls_back_the_complete_publication() {
    for stage in [
        PublicationStage::Validation,
        PublicationStage::RequestInsert,
        PublicationStage::ReservationInsert,
        PublicationStage::AttemptInsert,
        PublicationStage::RoutingDecisionInsert,
        PublicationStage::BeforeCommit,
    ] {
        let fixture = fixture().await;
        let injector = PublicationFaultInjector::fail_once_at(stage);
        let service =
            PublicationService::new(fixture.database.clone()).with_fault_injector(injector);
        let error = service
            .publish(claim(&fixture).await, input(1))
            .await
            .expect_err("injected failure must reject publication");
        assert!(matches!(error, PublicationError::Injected { stage: actual } if actual == stage));
        assert_eq!(row_counts(&fixture.database).await, (0, 0, 0, 0));
        assert_eq!(fixture.router.active_request_count("account-a"), 0);
        let snapshot = fixture.estimator.snapshot(&["account-a".into()]);
        assert_eq!(snapshot["account-a"].pending_requests, 0);
        assert_eq!(snapshot["account-a"].reserved_requests, 0);
        fixture.database.close().await.expect("database closes");
    }
}

#[tokio::test]
async fn duplicate_attempt_observes_existing_identity_without_row_fanout() {
    let fixture = fixture().await;
    let service = PublicationService::new(fixture.database.clone());
    let first = service
        .publish(claim(&fixture).await, input(1))
        .await
        .expect("first publication");
    let PublicationOutcome::Published(first) = first else {
        panic!("first publication must be new");
    };
    first
        .claim
        .release_active_claim()
        .expect("release first claim");

    let second = service
        .publish(claim(&fixture).await, input(1))
        .await
        .expect("duplicate observes existing publication");
    let PublicationOutcome::AlreadyPublished(identity) = second else {
        panic!("second publication must observe the first");
    };
    assert_eq!(identity, first.identity);
    assert_eq!(row_counts(&fixture.database).await, (1, 1, 1, 1));
    assert_eq!(fixture.router.active_request_count("account-a"), 0);
    fixture.database.close().await.expect("database closes");
}

#[tokio::test]
async fn later_attempt_reuses_the_pending_request_without_creating_a_parent() {
    let fixture = fixture().await;
    let service = PublicationService::new(fixture.database.clone());
    let first = service
        .publish(claim(&fixture).await, input(1))
        .await
        .expect("first publication");
    let PublicationOutcome::Published(first) = first else {
        panic!("first publication must be new");
    };
    first
        .claim
        .release_active_claim()
        .expect("release first claim");

    let second = service
        .publish(claim(&fixture).await, input(2))
        .await
        .expect("retry publication");
    let PublicationOutcome::Published(second) = second else {
        panic!("retry publication must create a second attempt");
    };
    assert_eq!(second.identity.db_request_id, first.identity.db_request_id);
    assert_eq!(second.identity.attempt_number, 2);
    assert_eq!(row_counts(&fixture.database).await, (1, 2, 2, 2));
    second
        .claim
        .release_active_claim()
        .expect("release second claim");
    fixture.database.close().await.expect("database closes");
}

#[tokio::test]
async fn postcommit_interruption_retains_identity_and_compensates_idempotently() {
    let fixture = fixture().await;
    let injector = PublicationFaultInjector::fail_once_at(PublicationStage::AfterCommit);
    let service = PublicationService::new(fixture.database.clone()).with_fault_injector(injector);
    let error = service
        .publish(claim(&fixture).await, input(1))
        .await
        .expect_err("post-commit interruption is surfaced");
    let PublicationError::PostCommit { mut interruption } = error else {
        panic!("expected retained post-commit identity");
    };
    assert_eq!(row_counts(&fixture.database).await, (1, 1, 1, 1));
    service
        .compensate_post_commit(&mut interruption)
        .await
        .expect("compensation converges");
    let state = fixture
        .database
        .call(|connection| {
            Ok((
                connection.query_row(
                    "SELECT completed_at IS NOT NULL FROM request_attempts",
                    [],
                    |row| row.get::<_, i64>(0),
                )?,
                connection.query_row("SELECT status FROM reservations", [], |row| {
                    row.get::<_, String>(0)
                })?,
            ))
        })
        .await
        .expect("compensated rows read");
    assert_eq!(state, (1, "released".to_owned()));
    assert_eq!(fixture.router.active_request_count("account-a"), 0);
    fixture.database.close().await.expect("database closes");
}

#[tokio::test]
async fn cancelling_the_waiter_cannot_strand_a_claim_or_durable_rows() {
    let fixture = fixture().await;
    let barrier = Arc::new(Barrier::new(2));
    let entered = Arc::new(AtomicBool::new(false));
    let injector = PublicationFaultInjector::block_once_at(
        PublicationStage::BeforeCommit,
        Arc::clone(&barrier),
        Arc::clone(&entered),
    );
    let service = PublicationService::new(fixture.database.clone()).with_fault_injector(injector);
    let pending_claim = claim(&fixture).await;
    let task = tokio::spawn({
        let service = service.clone();
        async move { service.publish(pending_claim, input(1)).await }
    });
    while !entered.load(Ordering::Acquire) {
        tokio::task::yield_now().await;
    }
    task.abort();
    barrier.wait();
    for _ in 0..100 {
        if fixture.router.active_request_count("account-a") == 0 {
            break;
        }
        tokio::task::yield_now().await;
    }
    assert_eq!(fixture.router.active_request_count("account-a"), 0);
    let counts = row_counts(&fixture.database).await;
    assert!(counts == (0, 0, 0, 0) || counts == (1, 1, 1, 1));
    if counts == (1, 1, 1, 1) {
        let status = fixture
            .database
            .call(|connection| {
                connection.query_row("SELECT status FROM reservations", [], |row| {
                    row.get::<_, String>(0)
                })
            })
            .await
            .expect("reservation status");
        assert_eq!(status, "released");
    }
    fixture.database.close().await.expect("database closes");
}
