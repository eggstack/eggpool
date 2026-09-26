use std::{
    collections::BTreeSet,
    sync::{
        Arc, Barrier,
        atomic::{AtomicBool, Ordering},
    },
    time::Duration,
};

use eggpool::{
    Config,
    accounts::{AccountRegistry, CredentialStore},
    catalog::{ModelCatalogCache, ModelInput, ProtocolResolutionStatus},
    coordinator::{
        DurableFinalizer, FinalizationData, FinalizationOutcome, PublicationError,
        PublicationFaultInjector, PublicationInput, PublicationOutcome, PublicationService,
        PublicationStage,
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
    DurableFinalizer::new(fixture.database.clone())
        .finalize_failed_attempt(
            &first.identity,
            FinalizationData {
                outcome: FinalizationOutcome::UpstreamError,
                release_reason: Some("retryable".into()),
                ..FinalizationData::default()
            },
            Some(first.claim),
        )
        .await
        .expect("finalize first attempt before retry");

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
    service
        .compensate_post_commit(&mut interruption)
        .await
        .expect("repeated compensation remains idempotent");
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
    tokio::time::timeout(Duration::from_secs(2), async {
        while !entered.load(Ordering::Acquire) {
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("publication reaches the BeforeCommit barrier");
    task.abort();
    barrier.wait();

    let released = tokio::time::timeout(Duration::from_secs(2), async {
        loop {
            if fixture.router.active_request_count("account-a") == 0 {
                break;
            }
            tokio::task::yield_now().await;
        }
    })
    .await;
    if released.is_err() {
        let counts = row_counts(&fixture.database).await;
        let reservation_status = if counts.2 == 1 {
            fixture
                .database
                .call(|connection| {
                    connection.query_row("SELECT status FROM reservations", [], |row| {
                        row.get::<_, String>(0)
                    })
                })
                .await
                .expect("reservation status")
        } else {
            "none".to_owned()
        };
        panic!(
            "publication compensation did not release the claim: active_request_count={}, row_counts={counts:?}, reservation_status={reservation_status:?}, before_commit_entered={}",
            fixture.router.active_request_count("account-a"),
            entered.load(Ordering::Acquire),
        );
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

struct ExclusionFixture {
    database: Database,
    router: RoutingRouter,
}

async fn exclusion_fixture() -> ExclusionFixture {
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
                "INSERT INTO accounts (id, name, api_key_env, enabled, provider_id)\n\
                 VALUES (2, 'account-b', 'UNUSED', 0, 'provider-a')",
                [],
            )?;
            connection.execute(
                "INSERT INTO accounts (id, name, api_key_env, enabled, provider_id)\n\
                 VALUES (3, 'account-c', 'UNUSED', 1, 'provider-a')",
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
    for (name, enabled) in [
        ("account-a", true),
        ("account-b", false),
        ("account-c", true),
    ] {
        provider.accounts.push(eggpool::config::AccountConfig {
            name: name.into(),
            enabled,
            ..Default::default()
        });
    }
    config.providers.insert("provider-a".into(), provider);
    config.validate().expect("fixture config validates");
    let registry = AccountRegistry::from_config(
        &config,
        &[
            Account {
                id: 1,
                name: "account-a".into(),
                api_key_env: "UNUSED".into(),
                enabled: true,
                weight: 1.0,
                provider_id: "provider-a".into(),
            },
            Account {
                id: 2,
                name: "account-b".into(),
                api_key_env: "UNUSED".into(),
                enabled: false,
                weight: 1.0,
                provider_id: "provider-a".into(),
            },
            Account {
                id: 3,
                name: "account-c".into(),
                api_key_env: "UNUSED".into(),
                enabled: true,
                weight: 1.0,
                provider_id: "provider-a".into(),
            },
        ],
        &CredentialStore::default(),
    )
    .expect("registry builds");
    // Only account-a receives catalog model support, so account-c is
    // excluded as no_model while account-b is excluded as disabled.
    let mut catalog = ModelCatalogCache::default();
    catalog.set_account_provider("account-a", "provider-a");
    catalog.set_account_provider("account-b", "provider-a");
    catalog.set_account_provider("account-c", "provider-a");
    let mut model = ModelInput::new("model-a");
    model.protocol = Some("openai".into());
    model.protocol_source = Some("fixture".into());
    model.resolution_status = ProtocolResolutionStatus::Resolved;
    catalog
        .update_from_account("account-a", "provider-a", &[model], true, true)
        .expect("catalog model");
    let estimator = QuotaEstimator::new([
        AccountQuota::new("account-a"),
        AccountQuota::new("account-b"),
        AccountQuota::new("account-c"),
    ]);
    let router = RoutingRouter::new(
        registry,
        catalog,
        estimator,
        None,
        EligibilityPolicy::default(),
    );
    ExclusionFixture { database, router }
}

#[tokio::test]
async fn prepared_routing_row_persists_multi_exclusion_and_score_facts() {
    let fixture = exclusion_fixture().await;
    let claim = fixture
        .router
        .select_and_claim(&facts(), &BTreeSet::new())
        .await
        .expect("claim succeeds")
        .expect("account-a is selectable");
    assert_eq!(claim.account_name(), "account-a");
    let snapshot = claim.selection_snapshot().clone();
    assert_eq!(snapshot.exclusions.len(), 2);
    let service = PublicationService::new(fixture.database.clone());
    let outcome = service
        .publish(claim, input(1))
        .await
        .expect("publication succeeds");
    assert!(matches!(outcome, PublicationOutcome::Published(_)));

    let row = fixture
        .database
        .call(|connection| {
            connection.query_row(
                "SELECT selected_account_id, selected_account_name, selected_tier, \
                 selected_score, eligible_count, scored_count, \
                 attempted_excluded_count, top_score, top_score_account_name, \
                 exclude_reasons_json, score_components_json \
                 FROM routing_decisions",
                [],
                |row| {
                    Ok((
                        row.get::<_, Option<i64>>(0)?,
                        row.get::<_, String>(1)?,
                        row.get::<_, Option<i64>>(2)?,
                        row.get::<_, Option<f64>>(3)?,
                        row.get::<_, i64>(4)?,
                        row.get::<_, i64>(5)?,
                        row.get::<_, i64>(6)?,
                        row.get::<_, Option<f64>>(7)?,
                        row.get::<_, Option<String>>(8)?,
                        row.get::<_, String>(9)?,
                        row.get::<_, String>(10)?,
                    ))
                },
            )
        })
        .await
        .expect("routing decision row reads");
    assert_eq!(row.0, Some(1));
    assert_eq!(row.1, "account-a");
    assert_eq!(row.2, snapshot.selected_priority.map(i64::from));
    assert_eq!(row.4, snapshot.eligible_candidate_count as i64);
    assert_eq!(row.5, snapshot.candidates.len() as i64);
    assert_eq!(row.6, snapshot.exclusions.len() as i64);
    assert_eq!(row.6, 2);
    let expected_selected = snapshot
        .selected_score
        .as_ref()
        .map(|score| score.final_score());
    assert_eq!(row.3, expected_selected);
    assert_eq!(
        row.7,
        snapshot
            .candidates
            .first()
            .map(|candidate| candidate.score.final_score())
    );
    assert_eq!(row.8, snapshot.top_account_name);

    let exclusions: serde_json::Value =
        serde_json::from_str(&row.9).expect("exclusions JSON parses");
    let mut reasons = exclusions
        .as_array()
        .expect("exclusions array")
        .iter()
        .map(|entry| {
            (
                entry["account"].as_str().expect("account").to_owned(),
                entry["reason"].as_str().expect("reason").to_owned(),
            )
        })
        .collect::<Vec<_>>();
    reasons.sort();
    assert_eq!(
        reasons,
        vec![
            ("account-b".to_owned(), "disabled".to_owned()),
            ("account-c".to_owned(), "no_model".to_owned()),
        ]
    );
    let components: serde_json::Value =
        serde_json::from_str(&row.10).expect("score components JSON parses");
    assert!(
        components.is_object(),
        "score components must be a JSON object"
    );
    // Byte comparison against the same deterministic serializer: the prepared
    // row must carry exactly the snapshot's score facts. (Parsed-Value float
    // comparison is avoided because decimal parsing need not reproduce the
    // identical f64 bits for 17-digit doubles.)
    assert_eq!(
        row.10,
        serde_json::to_string(snapshot.selected_score.as_ref().expect("selected score"))
            .expect("score serializes")
    );
    fixture.database.close().await.expect("database closes");
}
