use std::collections::BTreeSet;

use eggpool::{
    Config,
    accounts::{AccountRegistry, CredentialStore},
    catalog::{ModelCatalogCache, ModelInput, ProtocolResolutionStatus},
    coordinator::{
        DurableFinalizer, FinalizationData, FinalizationOutcome, PublicationError,
        PublicationInput, PublicationOutcome, PublicationService,
    },
    db::{Account, Database, DatabaseConfig, MigrationRunner},
    identity::{AuthenticatedPrincipal, PrincipalKind},
    quota::{AccountQuota, QuotaEstimator},
    routing::{EligibilityPolicy, RoutingRequestFacts, RoutingRouter},
    user_budget::{BudgetLimits, BudgetWindow, UserBudgetError, UserBudgetService},
};

struct Fixture {
    database: Database,
    router: RoutingRouter,
}

async fn fixture() -> Fixture {
    let database = Database::open(DatabaseConfig::default())
        .await
        .expect("database opens");
    MigrationRunner::new(&database)
        .run()
        .await
        .expect("schema migrates");
    database
        .call(|connection| {
            connection.execute(
                "INSERT INTO accounts (id, name, api_key_env, enabled, provider_id)
                 VALUES (1, 'budget-account', 'UNUSED', 1, 'provider-a')",
                [],
            )?;
            connection.execute(
                "INSERT INTO models (model_id, protocol, provider_id, resolution_status)
                 VALUES ('model-a', 'openai', 'provider-a', 'resolved')",
                [],
            )?;
            connection.execute(
                "INSERT INTO users(id, username, normalized_username, role, password_hash)
                 VALUES ('budget-admin', 'budget-admin', 'budget-admin', 'admin', 'test-hash')",
                [],
            )?;
            connection.execute(
                "INSERT INTO users(id, username, normalized_username, role, password_hash)
                 VALUES ('budget-member', 'budget-member', 'budget-member', 'member', 'test-hash')",
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
        name: "budget-account".into(),
        ..Default::default()
    });
    config.providers.insert("provider-a".into(), provider);
    config.validate().expect("fixture config validates");
    let registry = AccountRegistry::from_config(
        &config,
        &[Account {
            id: 1,
            name: "budget-account".into(),
            api_key_env: "UNUSED".into(),
            enabled: true,
            weight: 1.0,
            provider_id: "provider-a".into(),
        }],
        &CredentialStore::default(),
    )
    .expect("registry builds");
    let mut catalog = ModelCatalogCache::default();
    catalog.set_account_provider("budget-account", "provider-a");
    let mut model = ModelInput::new("model-a");
    model.protocol = Some("openai".into());
    model.protocol_source = Some("fixture".into());
    model.resolution_status = ProtocolResolutionStatus::Resolved;
    catalog
        .update_from_account("budget-account", "provider-a", &[model], true, true)
        .expect("catalog model");
    let router = RoutingRouter::new(
        registry,
        catalog,
        QuotaEstimator::new([AccountQuota::new("budget-account")]),
        None,
        EligibilityPolicy::default(),
    );
    Fixture { database, router }
}

async fn claim(fixture: &Fixture) -> eggpool::routing::SelectionClaim {
    let mut facts = RoutingRequestFacts::new("model-a");
    facts.requested_protocol = Some("openai".into());
    facts.client_protocol = Some("openai".into());
    facts.projected_tokens = 42;
    fixture
        .router
        .select_and_claim(&facts, &BTreeSet::new())
        .await
        .unwrap()
        .unwrap()
}

fn attribution() -> eggpool::identity::RequestAttribution {
    AuthenticatedPrincipal {
        user_id: Some("budget-member".into()),
        credential_id: None,
        role: Some("member".into()),
        scopes: vec!["inference".into()],
        auth_epoch: Some(1),
        kind: PrincipalKind::AgentToken,
    }
    .request_attribution()
}

#[tokio::test]
async fn concurrent_last_request_reservations_cannot_over_admit() {
    let fixture = fixture().await;
    let budgets = UserBudgetService::new(fixture.database.clone());
    budgets
        .set_rule(
            "budget-admin",
            "budget-member",
            None,
            BudgetWindow::Day,
            BudgetLimits {
                requests: Some(1),
                tokens: Some(100),
                concurrent: Some(1),
                ..Default::default()
            },
        )
        .await
        .expect("admin sets request and concurrency ceilings");

    let service = PublicationService::new(fixture.database.clone());
    let first_claim = claim(&fixture).await;
    let second_claim = claim(&fixture).await;
    let first = service.publish_attributed(
        first_claim,
        PublicationInput::new("budget-race-a", "openai", "openai", false, 1),
        Some(attribution()),
    );
    let second = service.publish_attributed(
        second_claim,
        PublicationInput::new("budget-race-b", "openai", "openai", false, 1),
        Some(attribution()),
    );
    let (first, second) = tokio::join!(first, second);
    let mut published = None;
    let mut denied = 0;
    for result in [first, second] {
        match result {
            Ok(PublicationOutcome::Published(attempt)) => {
                assert!(published.replace(attempt).is_none());
            }
            Err(PublicationError::BudgetExceeded) => denied += 1,
            other => panic!("unexpected concurrent budget result: {other:?}"),
        }
    }
    assert_eq!(denied, 1);
    assert_eq!(
        fixture
            .database
            .call(|connection| {
                Ok((
                    connection.query_row("SELECT count(*) FROM requests", [], |row| {
                        row.get::<_, i64>(0)
                    })?,
                    connection.query_row(
                        "SELECT count(*) FROM user_budget_reservations WHERE status = 'active'",
                        [],
                        |row| row.get::<_, i64>(0),
                    )?,
                ))
            })
            .await
            .unwrap(),
        (1, 1)
    );
    let published = published.expect("one request was published");
    let finalizer = DurableFinalizer::new(fixture.database.clone());
    let proxy_request_id = published.identity.proxy_request_id.clone();
    finalizer
        .finalize_failed_attempt(
            &published.identity,
            FinalizationData {
                outcome: FinalizationOutcome::UpstreamError,
                status_code: Some(502),
                input_tokens: 10,
                output_tokens: 5,
                input_tokens_reported: Some(10),
                output_tokens_reported: Some(5),
                cost_microdollars: 4,
                provider_cost_microdollars: Some(4),
                ..FinalizationData::default()
            },
            Some(published.claim),
        )
        .await
        .expect("failed upstream attempt keeps the logical user hold");
    let retry = service
        .publish_attributed(
            claim(&fixture).await,
            PublicationInput::new(proxy_request_id, "openai", "openai", false, 2),
            Some(attribution()),
        )
        .await
        .expect("retry reuses the same logical reservation");
    let PublicationOutcome::Published(retry) = retry else {
        panic!("retry should publish a new attempt");
    };
    let final_data = FinalizationData {
        outcome: FinalizationOutcome::Completed,
        input_tokens: 30,
        output_tokens: 12,
        input_tokens_reported: Some(30),
        output_tokens_reported: Some(12),
        cost_microdollars: 21,
        provider_cost_microdollars: Some(21),
        ..FinalizationData::default()
    };
    finalizer
        .finalize_request(&retry.identity, final_data.clone(), Some(retry.claim))
        .await
        .expect("durable completion settles the user budget");
    finalizer
        .finalize_request(&retry.identity, final_data, None)
        .await
        .expect("duplicate finalization converges idempotently");
    let usage = budgets
        .usage("budget-member", "2026-01-01", 10)
        .await
        .expect("bounded usage query succeeds");
    assert_eq!(usage.len(), 2, "one daily and one monthly bucket are kept");
    assert!(usage.iter().all(|bucket| {
        bucket.request_count == 1
            && bucket.token_count == 57
            && bucket.cost_microdollars == 25
            && bucket.provider_reported_microdollars == 25
            && bucket.local_estimated_microdollars == 0
            && bucket.reservation_fallback_microdollars == 0
    }));
    fixture.database.close().await.unwrap();
}

#[tokio::test]
async fn rules_validate_admin_scope_and_usage_reads_are_bounded() {
    let fixture = fixture().await;
    let budgets = UserBudgetService::new(fixture.database.clone());
    assert!(matches!(
        budgets
            .set_rule(
                "budget-member",
                "budget-member",
                None,
                BudgetWindow::Day,
                BudgetLimits {
                    requests: Some(1),
                    ..Default::default()
                },
            )
            .await,
        Err(UserBudgetError::ActorNotAdmin)
    ));
    assert!(matches!(
        budgets
            .set_rule(
                "budget-admin",
                "budget-member",
                None,
                BudgetWindow::Day,
                BudgetLimits {
                    requests: Some(0),
                    ..Default::default()
                },
            )
            .await,
        Err(UserBudgetError::InvalidRule)
    ));
    budgets
        .set_rule(
            "budget-admin",
            "budget-member",
            Some("model-a"),
            BudgetWindow::Day,
            BudgetLimits {
                requests: Some(20),
                tokens: Some(50_000),
                microdollars: Some(10_000),
                concurrent: Some(1),
            },
        )
        .await
        .expect("admin rule is recorded");
    assert_eq!(budgets.rules("budget-member").await.unwrap().len(), 1);
    assert!(
        budgets
            .remove_rule(
                "budget-admin",
                "budget-member",
                Some("model-a"),
                BudgetWindow::Day,
            )
            .await
            .unwrap()
    );
    assert!(budgets.rules("budget-member").await.unwrap().is_empty());
    assert_eq!(
        fixture
            .database
            .call(|connection| {
                connection.query_row("SELECT count(*) FROM user_budget_rule_audit", [], |row| {
                    row.get::<_, i64>(0)
                })
            })
            .await
            .unwrap(),
        2
    );
    assert!(
        budgets
            .usage("budget-member", "2026-01-01", 501)
            .await
            .is_err()
    );
    fixture.database.close().await.unwrap();
}

#[tokio::test]
async fn unknown_cost_settlement_consumes_the_reserved_estimate() {
    let fixture = fixture().await;
    let budgets = UserBudgetService::new(fixture.database.clone());
    budgets
        .set_rule(
            "budget-admin",
            "budget-member",
            None,
            BudgetWindow::Day,
            BudgetLimits {
                microdollars: Some(10_000),
                ..Default::default()
            },
        )
        .await
        .unwrap();
    let outcome = PublicationService::new(fixture.database.clone())
        .publish_attributed(
            claim(&fixture).await,
            PublicationInput::new("budget-unknown-cost", "openai", "openai", false, 1),
            Some(attribution()),
        )
        .await
        .expect("conservative estimate fits configured budget");
    let PublicationOutcome::Published(published) = outcome else {
        panic!("request should publish");
    };
    let request_id = published.identity.db_request_id;
    let held = fixture
        .database
        .call(move |connection| {
            connection.query_row(
                "SELECT held_microdollars FROM user_budget_reservations WHERE request_id = ?1",
                [request_id],
                |row| row.get::<_, i64>(0),
            )
        })
        .await
        .unwrap();
    assert!(held > 0, "unpriced use still carries a nonzero hold");
    DurableFinalizer::new(fixture.database.clone())
        .finalize_request(
            &published.identity,
            FinalizationData::default(),
            Some(published.claim),
        )
        .await
        .expect("unknown usage settles conservatively");
    let usage = budgets
        .usage("budget-member", "2026-01-01", 10)
        .await
        .unwrap();
    assert!(usage.iter().all(|bucket| {
        bucket.cost_microdollars == held
            && bucket.reservation_fallback_microdollars == held
            && bucket.unknown_cost_requests == 1
            && bucket.unknown_token_requests == 1
    }));
    fixture.database.close().await.unwrap();
}

#[tokio::test]
async fn concurrent_limit_releases_only_after_terminal_settlement() {
    let fixture = fixture().await;
    let budgets = UserBudgetService::new(fixture.database.clone());
    budgets
        .set_rule(
            "budget-admin",
            "budget-member",
            None,
            BudgetWindow::Day,
            BudgetLimits {
                concurrent: Some(1),
                ..Default::default()
            },
        )
        .await
        .unwrap();
    let service = PublicationService::new(fixture.database.clone());
    let first = service
        .publish_attributed(
            claim(&fixture).await,
            PublicationInput::new("budget-concurrent-a", "openai", "openai", false, 1),
            Some(attribution()),
        )
        .await
        .unwrap();
    let PublicationOutcome::Published(first) = first else {
        panic!("first concurrent request should publish");
    };
    assert!(matches!(
        service
            .publish_attributed(
                claim(&fixture).await,
                PublicationInput::new("budget-concurrent-b", "openai", "openai", false, 1),
                Some(attribution()),
            )
            .await,
        Err(PublicationError::BudgetExceeded)
    ));
    DurableFinalizer::new(fixture.database.clone())
        .finalize_request(
            &first.identity,
            FinalizationData {
                outcome: FinalizationOutcome::Completed,
                input_tokens: 42,
                input_tokens_reported: Some(42),
                ..FinalizationData::default()
            },
            Some(first.claim),
        )
        .await
        .unwrap();
    let after_settlement = service
        .publish_attributed(
            claim(&fixture).await,
            PublicationInput::new("budget-concurrent-c", "openai", "openai", false, 1),
            Some(attribution()),
        )
        .await
        .unwrap();
    let PublicationOutcome::Published(after_settlement) = after_settlement else {
        panic!("terminal settlement should release the concurrency slot");
    };
    after_settlement
        .claim
        .release_all()
        .expect("test claim is released");
    fixture.database.close().await.unwrap();
}
