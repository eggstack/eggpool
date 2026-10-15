use std::collections::BTreeSet;

use eggpool::{
    Config,
    accounts::{AccountRegistry, CredentialStore},
    catalog::{ModelCatalogCache, ModelInput, ProtocolResolutionStatus},
    coordinator::{
        CrashReconciler, DurableFinalizer, EndpointError, FinalizationData, FinalizationError,
        FinalizationOutcome, PublicationError, PublicationFaultInjector, PublicationInput,
        PublicationOutcome, PublicationService, PublicationStage, endpoint_error_body_for,
    },
    db::{Account, Database, DatabaseConfig, MigrationRunner},
    identity::{AuthenticatedPrincipal, PrincipalKind},
    operations::backup::BackupService,
    quota::{AccountQuota, QuotaEstimator},
    routing::{EligibilityPolicy, RoutingRequestFacts, RoutingRouter},
    user_budget::{
        BudgetEnforcement, BudgetLimits, BudgetProof, BudgetWindow, UserBudgetError,
        UserBudgetService,
    },
    wire::ir::ClientSurface,
};

struct Fixture {
    database: Database,
    router: RoutingRouter,
}

async fn fixture() -> Fixture {
    fixture_on(&DatabaseConfig::default()).await
}

async fn fixture_on(config: &DatabaseConfig) -> Fixture {
    let database = Database::open(config.clone())
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
                ..Default::default()
            },
        )
        .await
        .expect("admin rule is recorded");
    let recorded = budgets.rules("budget-member").await.unwrap();
    assert_eq!(recorded.len(), 1);
    // Compatibility default: rules created without an explicit mode are
    // estimated, never silently hard.
    assert_eq!(
        recorded[0].enforcement,
        eggpool::user_budget::BudgetEnforcement::Estimated
    );
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

// M008 hard-mode helpers: the proof carries admission output bounds plus the
// operator tariff snapshot, mirroring what the coordinators assemble from
// the claim snapshot and provider configuration.
fn hard_input(
    proxy_id: &str,
    attempt: i64,
    input_worst: i64,
    output: Option<i64>,
    tariff: Option<i64>,
) -> PublicationInput {
    let mut input = PublicationInput::new(proxy_id, "openai", "openai", false, attempt);
    input.budget_proof = Some(BudgetProof {
        input_worst_case_tokens: input_worst,
        requested_output_tokens: output,
        tariff_microdollars_per_million_tokens: tariff,
    });
    input
}

async fn hard_rule(budgets: &UserBudgetService, model_id: Option<&str>, limits: BudgetLimits) {
    budgets
        .set_rule(
            "budget-admin",
            "budget-member",
            model_id,
            BudgetWindow::Day,
            limits,
        )
        .await
        .expect("admin sets hard budget rule");
}

fn hard_limits(tokens: Option<i64>, microdollars: Option<i64>) -> BudgetLimits {
    BudgetLimits {
        tokens,
        microdollars,
        enforcement: BudgetEnforcement::Hard,
        ..Default::default()
    }
}

async fn table_counts(database: &Database) -> (i64, i64) {
    database
        .call(|connection| {
            Ok((
                connection.query_row("SELECT count(*) FROM requests", [], |row| {
                    row.get::<_, i64>(0)
                })?,
                connection.query_row(
                    "SELECT count(*) FROM user_budget_reservations",
                    [],
                    |row| row.get::<_, i64>(0),
                )?,
            ))
        })
        .await
        .unwrap()
}

async fn reservation_hold(
    database: &Database,
    request_id: i64,
) -> (i64, i64, String, i64, i64, Option<String>) {
    database
        .call(move |connection| {
            connection.query_row(
                "SELECT held_tokens, held_microdollars, enforcement,
                        bound_tokens, bound_microdollars, price_source
                 FROM user_budget_reservations WHERE request_id = ?1",
                [request_id],
                |row| {
                    Ok((
                        row.get(0)?,
                        row.get(1)?,
                        row.get(2)?,
                        row.get(3)?,
                        row.get(4)?,
                        row.get(5)?,
                    ))
                },
            )
        })
        .await
        .unwrap()
}

#[tokio::test]
async fn hard_token_rule_admits_bounded_request_with_worst_case_hold() {
    let fixture = fixture().await;
    let budgets = UserBudgetService::new(fixture.database.clone());
    hard_rule(&budgets, None, hard_limits(Some(1_000), None)).await;
    // Single-attempt worst case is 100 + 50 = 150; the initial hold covers
    // two attempt-equivalents (concrete attempt plus selector overhead).
    let outcome = PublicationService::new(fixture.database.clone())
        .publish_attributed(
            claim(&fixture).await,
            hard_input("budget-hard-token", 1, 100, Some(50), None),
            Some(attribution()),
        )
        .await
        .expect("bounded hard-token request publishes");
    let PublicationOutcome::Published(published) = outcome else {
        panic!("bounded request should publish");
    };
    let request_id = published.identity.db_request_id;
    let (held_tokens, held_micros, enforcement, bound_tokens, bound_micros, price) =
        reservation_hold(&fixture.database, request_id).await;
    assert_eq!(held_tokens, 300);
    assert_eq!(enforcement, "hard");
    assert_eq!(bound_tokens, 300);
    // Token-hard leaves the cost dimension on the estimator projection: no
    // hard price is claimed.
    assert!(held_micros > 0, "estimated cost projection is preserved");
    assert_eq!(bound_micros, 0);
    assert_eq!(price, None);
    assert_eq!(table_counts(&fixture.database).await, (1, 1));
    fixture.database.close().await.unwrap();
}

#[tokio::test]
async fn hard_token_rule_denies_unbounded_request_without_upstream_io() {
    let fixture = fixture().await;
    let budgets = UserBudgetService::new(fixture.database.clone());
    hard_rule(&budgets, None, hard_limits(Some(1_000_000), None)).await;
    let result = PublicationService::new(fixture.database.clone())
        .publish_attributed(
            claim(&fixture).await,
            hard_input("budget-hard-unbounded", 1, 100, None, None),
            Some(attribution()),
        )
        .await;
    assert!(
        matches!(result, Err(PublicationError::BudgetNotEnforceable)),
        "unbounded hard-token request must be denied as not enforceable, got {result:?}"
    );
    // Denial happens inside the publication transaction before claim
    // conversion, so no request row exists for later dispatch.
    assert_eq!(table_counts(&fixture.database).await, (0, 0));
    fixture.database.close().await.unwrap();
}

#[tokio::test]
async fn hard_token_rule_denies_missing_proof_without_upstream_io() {
    let fixture = fixture().await;
    let budgets = UserBudgetService::new(fixture.database.clone());
    hard_rule(&budgets, None, hard_limits(Some(1_000_000), None)).await;
    let result = PublicationService::new(fixture.database.clone())
        .publish_attributed(
            claim(&fixture).await,
            PublicationInput::new("budget-hard-noproof", "openai", "openai", false, 1),
            Some(attribution()),
        )
        .await;
    assert!(matches!(
        result,
        Err(PublicationError::BudgetNotEnforceable)
    ));
    assert_eq!(table_counts(&fixture.database).await, (0, 0));
    fixture.database.close().await.unwrap();
}

#[tokio::test]
async fn hard_usd_rule_denies_unknown_price_without_upstream_io() {
    let fixture = fixture().await;
    let budgets = UserBudgetService::new(fixture.database.clone());
    hard_rule(&budgets, None, hard_limits(None, Some(1_000_000))).await;
    // A bounded token proof with no operator tariff still cannot prove a
    // hard USD ceiling: estimator fallbacks are never a hard price.
    let result = PublicationService::new(fixture.database.clone())
        .publish_attributed(
            claim(&fixture).await,
            hard_input("budget-hard-noprice", 1, 100, Some(50), None),
            Some(attribution()),
        )
        .await;
    assert!(matches!(
        result,
        Err(PublicationError::BudgetNotEnforceable)
    ));
    assert_eq!(table_counts(&fixture.database).await, (0, 0));
    fixture.database.close().await.unwrap();
}

#[tokio::test]
async fn hard_usd_rule_admits_with_operator_tariff_hold() {
    let fixture = fixture().await;
    let budgets = UserBudgetService::new(fixture.database.clone());
    hard_rule(&budgets, None, hard_limits(None, Some(1_000_000))).await;
    // This positive bound rounds up instead of becoming zero at a tiny tariff.
    let outcome = PublicationService::new(fixture.database.clone())
        .publish_attributed(
            claim(&fixture).await,
            hard_input("budget-hard-tariff", 1, 100, Some(50), Some(1)),
            Some(attribution()),
        )
        .await
        .expect("tariff-backed hard-USD request publishes");
    let PublicationOutcome::Published(published) = outcome else {
        panic!("request should publish");
    };
    assert_eq!(
        reservation_hold(&fixture.database, published.identity.db_request_id).await,
        (
            42,
            1,
            "hard".to_owned(),
            0,
            1,
            Some("operator_tariff".to_owned())
        )
    );
    fixture.database.close().await.unwrap();
}

#[tokio::test]
async fn hard_token_and_usd_rule_admits_with_combined_worst_case_hold() {
    let fixture = fixture().await;
    let budgets = UserBudgetService::new(fixture.database.clone());
    hard_rule(&budgets, None, hard_limits(Some(100_000), Some(1_000_000))).await;
    // Worst case 150 tokens times two equivalents; USD converts the same
    // 300-token bound at $3/MTok.
    let outcome = PublicationService::new(fixture.database.clone())
        .publish_attributed(
            claim(&fixture).await,
            hard_input("budget-hard-combined", 1, 100, Some(50), Some(3_000_000)),
            Some(attribution()),
        )
        .await
        .expect("combined hard rule publishes within bounds");
    let PublicationOutcome::Published(published) = outcome else {
        panic!("request should publish");
    };
    assert_eq!(
        reservation_hold(&fixture.database, published.identity.db_request_id).await,
        (
            300,
            900,
            "hard".to_owned(),
            300,
            900,
            Some("operator_tariff".to_owned())
        )
    );
    fixture.database.close().await.unwrap();
}

#[tokio::test]
async fn hard_retry_denies_extension_beyond_remaining_budget() {
    let fixture = fixture().await;
    let budgets = UserBudgetService::new(fixture.database.clone());
    hard_rule(&budgets, None, hard_limits(Some(400), None)).await;
    let service = PublicationService::new(fixture.database.clone());
    let first = service
        .publish_attributed(
            claim(&fixture).await,
            hard_input("budget-hard-retry", 1, 100, Some(50), None),
            Some(attribution()),
        )
        .await
        .expect("initial 300-token hold fits the 400-token cap");
    let PublicationOutcome::Published(first) = first else {
        panic!("first attempt should publish");
    };
    DurableFinalizer::new(fixture.database.clone())
        .finalize_failed_attempt(
            &first.identity,
            FinalizationData {
                outcome: FinalizationOutcome::UpstreamError,
                status_code: Some(502),
                ..FinalizationData::default()
            },
            Some(first.claim),
        )
        .await
        .expect("failed attempt keeps the logical hold");
    // Retry would extend by one 150-token equivalent: 300 + 150 > 400.
    let retry = service
        .publish_attributed(
            claim(&fixture).await,
            hard_input("budget-hard-retry", 2, 100, Some(50), None),
            Some(attribution()),
        )
        .await;
    assert!(
        matches!(retry, Err(PublicationError::BudgetExceeded)),
        "retry beyond remaining hard budget must be denied, got {retry:?}"
    );
    fixture.database.close().await.unwrap();
}

#[tokio::test]
async fn hard_retry_extends_within_remaining_budget() {
    let fixture = fixture().await;
    let budgets = UserBudgetService::new(fixture.database.clone());
    hard_rule(&budgets, None, hard_limits(Some(1_000), None)).await;
    let service = PublicationService::new(fixture.database.clone());
    let first = service
        .publish_attributed(
            claim(&fixture).await,
            hard_input("budget-hard-retry-ok", 1, 100, Some(50), None),
            Some(attribution()),
        )
        .await
        .expect("initial hold publishes");
    let PublicationOutcome::Published(first) = first else {
        panic!("first attempt should publish");
    };
    let proxy_request_id = first.identity.proxy_request_id.clone();
    DurableFinalizer::new(fixture.database.clone())
        .finalize_failed_attempt(
            &first.identity,
            FinalizationData {
                outcome: FinalizationOutcome::UpstreamError,
                status_code: Some(502),
                ..FinalizationData::default()
            },
            Some(first.claim),
        )
        .await
        .expect("failed attempt keeps the logical hold");
    let retry = service
        .publish_attributed(
            claim(&fixture).await,
            hard_input(&proxy_request_id, 2, 100, Some(50), None),
            Some(attribution()),
        )
        .await
        .expect("retry within remaining hard budget publishes");
    let PublicationOutcome::Published(retry) = retry else {
        panic!("retry should publish");
    };
    // 300 initial + 150 retry extension, all inside the 1000-token cap.
    let (held, _, _, _, _, _) =
        reservation_hold(&fixture.database, retry.identity.db_request_id).await;
    assert_eq!(held, 450);
    fixture.database.close().await.unwrap();
}

#[tokio::test]
async fn hard_overflow_and_nonpositive_output_fail_closed() {
    let fixture = fixture().await;
    let budgets = UserBudgetService::new(fixture.database.clone());
    hard_rule(&budgets, None, hard_limits(Some(i64::MAX), None)).await;
    let service = PublicationService::new(fixture.database.clone());
    for (label, output) in [
        ("overflow", Some(i64::MAX)),
        ("zero", Some(0)),
        ("negative", Some(-5)),
    ] {
        let result = service
            .publish_attributed(
                claim(&fixture).await,
                hard_input("budget-hard-overflow", 1, 100, output, None),
                Some(attribution()),
            )
            .await;
        assert!(
            matches!(result, Err(PublicationError::BudgetNotEnforceable)),
            "{label} output bound must fail closed, got {result:?}"
        );
    }
    assert_eq!(table_counts(&fixture.database).await, (0, 0));
    fixture.database.close().await.unwrap();
}

#[tokio::test]
async fn hard_settlement_records_provider_overrun_without_hiding() {
    let fixture = fixture().await;
    let budgets = UserBudgetService::new(fixture.database.clone());
    hard_rule(&budgets, None, hard_limits(Some(100_000), None)).await;
    let outcome = PublicationService::new(fixture.database.clone())
        .publish_attributed(
            claim(&fixture).await,
            hard_input("budget-hard-overrun", 1, 100, Some(50), None),
            Some(attribution()),
        )
        .await
        .expect("bounded request publishes");
    let PublicationOutcome::Published(published) = outcome else {
        panic!("request should publish");
    };
    // The provider reports 400 tokens against a 300-token worst-case hold.
    // Settlement must charge what was observed, not silently clamp to held.
    DurableFinalizer::new(fixture.database.clone())
        .finalize_request(
            &published.identity,
            FinalizationData {
                outcome: FinalizationOutcome::Completed,
                input_tokens: 200,
                output_tokens: 200,
                input_tokens_reported: Some(200),
                output_tokens_reported: Some(200),
                ..FinalizationData::default()
            },
            Some(published.claim),
        )
        .await
        .expect("terminal settlement records observed usage");
    let usage = budgets
        .usage("budget-member", "2026-01-01", 10)
        .await
        .expect("usage reads succeed");
    assert!(
        usage
            .iter()
            .all(|bucket| bucket.token_count == 400 && bucket.unknown_token_requests == 0),
        "observed overrun must be charged visibly, got {usage:?}"
    );
    let observed = fixture
        .database
        .call({
            let request_id = published.identity.db_request_id;
            move |connection| {
                connection.query_row(
                    "SELECT observed_tokens FROM user_budget_reservations WHERE request_id = ?1",
                    [request_id],
                    |row| row.get::<_, i64>(0),
                )
            }
        })
        .await
        .unwrap();
    assert_eq!(observed, 400);
    fixture.database.close().await.unwrap();
}

#[tokio::test]
async fn zero_cost_tariff_admits_hard_usd_without_hold() {
    let fixture = fixture().await;
    let budgets = UserBudgetService::new(fixture.database.clone());
    hard_rule(&budgets, None, hard_limits(None, Some(1_000_000))).await;
    // An explicit zero tariff is a verified free declaration, distinct from
    // unknown price: hard USD admits with a zero hold.
    let outcome = PublicationService::new(fixture.database.clone())
        .publish_attributed(
            claim(&fixture).await,
            hard_input("budget-hard-zerocost", 1, 100, Some(50), Some(0)),
            Some(attribution()),
        )
        .await
        .expect("verified zero-cost request publishes");
    let PublicationOutcome::Published(published) = outcome else {
        panic!("request should publish");
    };
    assert_eq!(
        reservation_hold(&fixture.database, published.identity.db_request_id).await,
        (
            42,
            0,
            "hard".to_owned(),
            0,
            0,
            Some("operator_tariff".to_owned())
        )
    );
    fixture.database.close().await.unwrap();
}

#[tokio::test]
async fn endpoint_error_maps_not_enforceable_to_stable_429() {
    let error = EndpointError::BudgetNotEnforceable;
    assert_eq!(error.status().as_u16(), 429);
    assert_eq!(
        error.to_string(),
        "Hard budget cannot be enforced: include a positive output cap supported by this API, and configure an operator tariff when a hard USD limit applies"
    );
    let body = endpoint_error_body_for(ClientSurface::ChatCompletions, &error);
    let parsed: serde_json::Value = serde_json::from_slice(&body).expect("error body is JSON");
    assert_eq!(
        parsed["error"]["message"].as_str(),
        Some(
            "Hard budget cannot be enforced: include a positive output cap supported by this API, and configure an operator tariff when a hard USD limit applies"
        )
    );
    assert_eq!(
        parsed["error"]["code"].as_str(),
        Some("hard_budget_not_enforceable")
    );
    let messages = endpoint_error_body_for(ClientSurface::Messages, &error);
    let parsed: serde_json::Value = serde_json::from_slice(&messages).expect("JSON error");
    assert_eq!(parsed["error"]["code"], "hard_budget_not_enforceable");
    let exceeded = endpoint_error_body_for(
        ClientSurface::ChatCompletions,
        &EndpointError::BudgetExceeded,
    );
    let parsed: serde_json::Value = serde_json::from_slice(&exceeded).expect("JSON error");
    assert_eq!(parsed["error"]["code"], "budget_exceeded");
}

#[tokio::test]
async fn admission_extracts_per_surface_output_bounds() {
    use eggpool::request::{AdmissionOptions, admit_compact_request, admit_request};

    let chat_capped =
        br#"{"model":"m","messages":[{"role":"user","content":"hi"}],"max_tokens":128}"#;
    let admitted = admit_request(
        chat_capped,
        AdmissionOptions {
            client_surface: ClientSurface::ChatCompletions,
            ..AdmissionOptions::default()
        },
    )
    .expect("chat admission succeeds");
    assert_eq!(admitted.budget_facts.requested_output_tokens, Some(128));
    assert!(admitted.budget_facts.input_worst_case_tokens >= chat_capped.len() as i64);

    let chat_uncapped = br#"{"model":"m","messages":[{"role":"user","content":"hi"}]}"#;
    let admitted = admit_request(
        chat_uncapped,
        AdmissionOptions {
            client_surface: ClientSurface::ChatCompletions,
            ..AdmissionOptions::default()
        },
    )
    .expect("uncapped chat admission succeeds");
    assert_eq!(admitted.budget_facts.requested_output_tokens, None);

    let chat_zero = br#"{"model":"m","messages":[{"role":"user","content":"hi"}],"max_tokens":0}"#;
    let admitted = admit_request(
        chat_zero,
        AdmissionOptions {
            client_surface: ClientSurface::ChatCompletions,
            ..AdmissionOptions::default()
        },
    )
    .expect("zero-cap chat admission succeeds structurally");
    // An unusable cap degrades to unbounded: hard rules fail closed on it.
    assert_eq!(admitted.budget_facts.requested_output_tokens, None);

    let responses_capped = br#"{"model":"m","input":"hi","max_output_tokens":64}"#;
    let admitted = admit_request(
        responses_capped,
        AdmissionOptions {
            client_surface: ClientSurface::Responses,
            ..AdmissionOptions::default()
        },
    )
    .expect("responses admission succeeds");
    assert_eq!(admitted.budget_facts.requested_output_tokens, Some(64));

    let messages_capped =
        br#"{"model":"m","max_tokens":32,"messages":[{"role":"user","content":"hi"}]}"#;
    let admitted = admit_request(
        messages_capped,
        AdmissionOptions {
            client_surface: ClientSurface::Messages,
            ..AdmissionOptions::default()
        },
    )
    .expect("messages admission succeeds");
    assert_eq!(admitted.budget_facts.requested_output_tokens, Some(32));

    let compact_capped = br#"{"model":"m","input":"history","max_output_tokens":16}"#;
    let compact = admit_compact_request(
        compact_capped,
        AdmissionOptions {
            client_surface: ClientSurface::Responses,
            ..AdmissionOptions::default()
        },
    )
    .expect("compact admission succeeds");
    assert_eq!(compact.budget_facts.requested_output_tokens, Some(16));
}

#[test]
fn invalid_agent_output_caps_are_rejected_across_surfaces() {
    use eggpool::request::{AdmissionOptions, admit_request};

    for (surface, cap_field) in [
        (ClientSurface::ChatCompletions, "max_tokens"),
        (ClientSurface::Responses, "max_output_tokens"),
        (ClientSurface::Messages, "max_tokens"),
    ] {
        let mut payload = serde_json::Map::new();
        payload.insert("model".into(), serde_json::json!("synthetic-model"));
        payload.insert(cap_field.into(), serde_json::json!("unbounded"));
        let body = serde_json::to_vec(&payload).expect("synthetic request encodes");
        let result = admit_request(
            &body,
            AdmissionOptions {
                client_surface: surface,
                ..AdmissionOptions::default()
            },
        );
        assert!(
            result.is_err(),
            "invalid output cap accepted for {surface:?}"
        );
    }
}

#[test]
fn sanitized_agent_budget_capture_corpus_records_supported_surfaces() {
    let corpus: serde_json::Value = serde_json::from_str(include_str!(
        "fixtures/on_prem_budget_agent_compatibility.json"
    ))
    .expect("compatibility corpus is valid JSON");
    assert_eq!(corpus["clients"][0]["version"], "0.162.1");
    assert_eq!(corpus["clients"][0]["surface"], "responses");
    assert!(corpus["clients"][0]["output_cap"].is_null());
    assert_eq!(
        corpus["clients"][0]["budget_compatibility"],
        "unbounded; EggPool hard rules reject before dispatch under the proof contract"
    );
    assert_eq!(corpus["clients"][1]["version"], "1.18.35");
    assert_eq!(corpus["clients"][1]["output_cap"], 1024);
    assert_eq!(
        corpus["surface_fields"]["chat_completions"],
        serde_json::json!(["max_completion_tokens", "max_tokens"])
    );
    assert_eq!(
        corpus["surface_fields"]["responses"],
        serde_json::json!(["max_output_tokens", "max_completion_tokens", "max_tokens"])
    );
    assert_eq!(
        corpus["surface_fields"]["compact_responses"],
        corpus["surface_fields"]["responses"]
    );
    assert_eq!(
        corpus["surface_fields"]["anthropic_messages"],
        serde_json::json!(["max_tokens"])
    );
}

#[tokio::test]
async fn admission_preserves_routing_inputs_for_transcode_eligibility() {
    use eggpool::request::{AdmissionOptions, StaticRoutingFacts, admit_request};

    // Regression guard: routing facts built from admission must carry the
    // transcode protocols (and other routing inputs) untouched, or
    // cross-surface requests lose upstream eligibility with no admission
    // error. M008 dropped this copy and broke messages-to-responses
    // transcoding while native surfaces kept passing.
    let body = br#"{"model":"m","max_tokens":32,"messages":[{"role":"user","content":"hi"}]}"#;
    let admitted = admit_request(
        body.as_slice(),
        AdmissionOptions {
            client_surface: ClientSurface::Messages,
            ..AdmissionOptions::default()
        },
    )
    .expect("messages admission succeeds");
    let mut inputs = StaticRoutingFacts {
        requested_protocol: Some("anthropic".into()),
        transcode_protocols: vec!["openai".into(), "anthropic".into(), "gemini".into()],
        catalog_stale_after_s: Some(60),
        now: 1_700_000_000,
        ..StaticRoutingFacts::default()
    };
    inputs.capability_policy.insert("k".into(), "v".into());
    let facts = admitted.routing_facts(&inputs);
    assert_eq!(
        facts.transcode_protocols,
        vec![
            "openai".to_owned(),
            "anthropic".to_owned(),
            "gemini".to_owned()
        ]
    );
    assert_eq!(facts.requested_protocol.as_deref(), Some("anthropic"));
    assert_eq!(facts.catalog_stale_after_s, Some(60));
    assert_eq!(facts.now, 1_700_000_000);
    assert_eq!(facts.request_surface, "messages");
    assert_eq!(facts.budget_requested_output_tokens, Some(32));
}

#[tokio::test]
async fn provider_tariff_overflow_is_rejected_by_config_validation() {
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
    provider.budget.max_microdollars_per_million_tokens = Some(u64::MAX);
    config.providers.insert("provider-a".into(), provider);
    assert!(
        config.validate().is_err(),
        "tariffs beyond i64 must be rejected"
    );

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
    provider.budget.max_microdollars_per_million_tokens = Some(3_000_000);
    config.providers.insert("provider-a".into(), provider);
    config.validate().expect("in-range tariff validates");
}

#[tokio::test]
async fn hard_proof_flows_from_admission_through_claim_snapshot() {
    use eggpool::coordinator::budget_proof_for_claim;
    use eggpool::request::{AdmissionOptions, StaticRoutingFacts, admit_request};

    let fixture = fixture().await;
    let budgets = UserBudgetService::new(fixture.database.clone());
    hard_rule(
        &budgets,
        None,
        hard_limits(Some(100_000_000), Some(1_000_000_000)),
    )
    .await;

    // Admission extracts the caller output cap and the byte-length input
    // bound; routing facts and the claim snapshot carry them untouched.
    let body =
        br#"{"model":"model-a","messages":[{"role":"user","content":"hi"}],"max_tokens":64}"#;
    let admitted = admit_request(
        body.as_slice(),
        AdmissionOptions {
            client_surface: ClientSurface::ChatCompletions,
            ..AdmissionOptions::default()
        },
    )
    .expect("chat admission succeeds");
    let mut inputs = StaticRoutingFacts::default();
    inputs.known_provider_ids.insert("provider-a".into());
    inputs.requested_protocol = Some("openai".into());
    let facts = admitted.routing_facts(&inputs);
    assert_eq!(facts.budget_requested_output_tokens, Some(64));
    let claim = fixture
        .router
        .select_and_claim(&facts, &BTreeSet::new())
        .await
        .unwrap()
        .expect("fixture route is selectable");
    assert_eq!(claim.budget_requested_output_tokens(), Some(64));
    assert!(claim.budget_input_worst_case_tokens() >= body.len() as i64);

    // The coordinator assembles the proof exactly as production does:
    // snapshot facts plus the selected provider's tariff snapshot.
    let worst = claim.budget_input_worst_case_tokens() + 64;
    let mut input = PublicationInput::new("budget-carriage", "openai", "openai", false, 1);
    input.budget_proof = Some(budget_proof_for_claim(&claim, Some(3_000_000)));
    let outcome = PublicationService::new(fixture.database.clone())
        .publish_attributed(claim, input, Some(attribution()))
        .await
        .expect("carried proof publishes under hard rules");
    let PublicationOutcome::Published(published) = outcome else {
        panic!("request should publish");
    };
    let (held_tokens, held_micros, enforcement, bound_tokens, bound_micros, price) =
        reservation_hold(&fixture.database, published.identity.db_request_id).await;
    assert_eq!(held_tokens, worst * 2);
    assert_eq!(held_micros, worst * 2 * 3);
    assert_eq!(enforcement, "hard");
    assert_eq!(bound_tokens, worst * 2);
    assert_eq!(bound_micros, worst * 2 * 3);
    assert_eq!(price, Some("operator_tariff".to_owned()));
    fixture.database.close().await.unwrap();
}

// ---------------------------------------------------------------------------
// M010 crash recovery: budget holds converge on restart without folding
// usage, and a released hold never blocks the next admitted request.
// ---------------------------------------------------------------------------

async fn hold_status(database: &Database, request_id: i64) -> (String, Option<String>) {
    database
        .call(move |connection| {
            connection.query_row(
                "SELECT status, settled_at FROM user_budget_reservations WHERE request_id = ?1",
                [request_id],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
        })
        .await
        .unwrap()
}

async fn budget_row_counts(database: &Database) -> (i64, i64, i64) {
    database
        .call(|connection| {
            Ok((
                connection.query_row(
                    "SELECT count(*) FROM user_budget_reservations WHERE status = 'active'",
                    [],
                    |row| row.get::<_, i64>(0),
                )?,
                connection.query_row(
                    "SELECT count(*) FROM user_budget_attempt_usage",
                    [],
                    |row| row.get::<_, i64>(0),
                )?,
                connection.query_row("SELECT count(*) FROM user_budget_usage", [], |row| {
                    row.get::<_, i64>(0)
                })?,
            ))
        })
        .await
        .unwrap()
}

async fn concurrent_hard_rule(budgets: &UserBudgetService) {
    budgets
        .set_rule(
            "budget-admin",
            "budget-member",
            None,
            BudgetWindow::Day,
            BudgetLimits {
                tokens: Some(1_000_000),
                concurrent: Some(1),
                enforcement: BudgetEnforcement::Hard,
                ..Default::default()
            },
        )
        .await
        .expect("admin sets concurrent hard budget rule");
}

#[tokio::test]
async fn crash_after_publication_releases_user_hold_without_folding_usage() {
    let fixture = fixture().await;
    let budgets = UserBudgetService::new(fixture.database.clone());
    concurrent_hard_rule(&budgets).await;
    let outcome = PublicationService::new(fixture.database.clone())
        .publish_attributed(
            claim(&fixture).await,
            hard_input("budget-crash-hold", 1, 100, Some(50), None),
            Some(attribution()),
        )
        .await
        .expect("bounded hard-token request publishes");
    let PublicationOutcome::Published(published) = outcome else {
        panic!("request should publish");
    };
    let request_id = published.identity.db_request_id;
    // The crash shape: committed publication, dead process. The hold is
    // durably active and no usage has been folded.
    assert_eq!(hold_status(&fixture.database, request_id).await.0, "active");
    assert_eq!(budget_row_counts(&fixture.database).await, (1, 0, 0));
    drop(published.claim);

    let report = CrashReconciler::new(fixture.database.clone())
        .reconcile_once()
        .await
        .expect("restart reconciliation succeeds");
    assert_eq!(report.requests_interrupted, 1);
    assert_eq!(report.user_budget_holds_released, 1);
    assert_eq!(report.instance_budget_holds_released, 0);
    assert!(report.classification.pending_requests_with_active_user_hold >= 1);

    // The hold is compensation, not credit: released, never settled, and no
    // usage bucket or attempt fact was folded.
    assert_eq!(
        hold_status(&fixture.database, request_id).await,
        ("released".to_owned(), None)
    );
    assert_eq!(budget_row_counts(&fixture.database).await, (0, 0, 0));
    let request_status: String = fixture
        .database
        .call(move |connection| {
            connection.query_row(
                "SELECT status FROM requests WHERE id = ?1",
                [request_id],
                |row| row.get(0),
            )
        })
        .await
        .unwrap();
    assert_eq!(request_status, "interrupted");

    // A second pass is a no-op: convergence is idempotent.
    let second = CrashReconciler::new(fixture.database.clone())
        .reconcile_once()
        .await
        .expect("second pass succeeds");
    assert!(second.converged);
    assert_eq!(second.user_budget_holds_released, 0);

    // The released hold frees the single concurrency slot: the next
    // admitted request publishes instead of wedging on a phantom hold.
    let next = PublicationService::new(fixture.database.clone())
        .publish_attributed(
            claim(&fixture).await,
            hard_input("budget-crash-next", 1, 100, Some(50), None),
            Some(attribution()),
        )
        .await
        .expect("next request admits after crash release");
    let PublicationOutcome::Published(next) = next else {
        panic!("next request should publish");
    };
    assert_eq!(
        hold_status(&fixture.database, next.identity.db_request_id)
            .await
            .0,
        "active"
    );
    fixture.database.close().await.unwrap();
}

#[tokio::test]
async fn retry_after_crash_release_fails_closed_without_new_hold() {
    let fixture = fixture().await;
    let budgets = UserBudgetService::new(fixture.database.clone());
    hard_rule(&budgets, None, hard_limits(Some(1_000_000), None)).await;
    let outcome = PublicationService::new(fixture.database.clone())
        .publish_attributed(
            claim(&fixture).await,
            hard_input("budget-crash-retry", 1, 100, Some(50), None),
            Some(attribution()),
        )
        .await
        .expect("bounded hard-token request publishes");
    let PublicationOutcome::Published(published) = outcome else {
        panic!("request should publish");
    };
    drop(published.claim);
    CrashReconciler::new(fixture.database.clone())
        .reconcile_once()
        .await
        .expect("restart reconciliation succeeds");
    // The interrupted request can never settle, so a retry under the same
    // proxy id must deny instead of resurrecting a hold on dead work. The
    // duplicate gate refuses non-pending requests before any budget or
    // dispatch work happens.
    let retry = PublicationService::new(fixture.database.clone())
        .publish_attributed(
            claim(&fixture).await,
            hard_input("budget-crash-retry", 2, 100, Some(50), None),
            Some(attribution()),
        )
        .await;
    assert!(
        matches!(retry, Err(PublicationError::DuplicateConflict { .. })),
        "retry on an interrupted request must fail closed, got {retry:?}"
    );
    assert_eq!(budget_row_counts(&fixture.database).await, (0, 0, 0));
    fixture.database.close().await.unwrap();
}

#[tokio::test]
async fn compensated_publication_retry_reuses_hold_then_crash_releases_it() {
    let fixture = fixture().await;
    let budgets = UserBudgetService::new(fixture.database.clone());
    hard_rule(&budgets, None, hard_limits(Some(1_000_000), None)).await;
    let injector = PublicationFaultInjector::fail_once_at(PublicationStage::AfterCommit);
    let service = PublicationService::new(fixture.database.clone()).with_fault_injector(injector);
    let error = service
        .publish_attributed(
            claim(&fixture).await,
            hard_input("budget-crash-compensate", 1, 100, Some(50), None),
            Some(attribution()),
        )
        .await
        .expect_err("post-commit interruption is surfaced");
    let PublicationError::PostCommit { mut interruption } = error else {
        panic!("expected retained post-commit identity");
    };
    let request_id = interruption.identity.db_request_id;
    service
        .compensate_post_commit(&mut interruption)
        .await
        .expect("compensation converges");
    // In-process compensation releases the legacy reservation but keeps the
    // request pending with its budget hold: a retry must reuse the same
    // hold rather than leak a second one.
    assert_eq!(hold_status(&fixture.database, request_id).await.0, "active");
    let retry = service
        .publish_attributed(
            claim(&fixture).await,
            hard_input("budget-crash-compensate", 2, 100, Some(50), None),
            Some(attribution()),
        )
        .await
        .expect("retry reuses the compensated hold");
    let PublicationOutcome::Published(retried) = retry else {
        panic!("retry should publish a second attempt");
    };
    assert_eq!(retried.identity.db_request_id, request_id);
    assert_eq!(budget_row_counts(&fixture.database).await, (1, 0, 0));
    // Crash before the retry dispatches: the extended hold converges on
    // restart with no usage folded.
    drop(retried.claim);
    let report = CrashReconciler::new(fixture.database.clone())
        .reconcile_once()
        .await
        .expect("restart reconciliation succeeds");
    assert_eq!(report.user_budget_holds_released, 1);
    assert_eq!(
        hold_status(&fixture.database, request_id).await,
        ("released".to_owned(), None)
    );
    assert_eq!(budget_row_counts(&fixture.database).await, (0, 0, 0));
    fixture.database.close().await.unwrap();
}

#[tokio::test]
async fn settled_request_survives_restart_without_double_fold() {
    let fixture = fixture().await;
    let budgets = UserBudgetService::new(fixture.database.clone());
    hard_rule(&budgets, None, hard_limits(Some(1_000_000), None)).await;
    let outcome = PublicationService::new(fixture.database.clone())
        .publish_attributed(
            claim(&fixture).await,
            hard_input("budget-crash-settled", 1, 100, Some(50), None),
            Some(attribution()),
        )
        .await
        .expect("bounded hard-token request publishes");
    let PublicationOutcome::Published(published) = outcome else {
        panic!("request should publish");
    };
    let request_id = published.identity.db_request_id;
    DurableFinalizer::new(fixture.database.clone())
        .finalize_request(
            &published.identity,
            FinalizationData::default(),
            Some(published.claim),
        )
        .await
        .expect("terminal request settles");
    assert_eq!(
        hold_status(&fixture.database, request_id).await.0,
        "settled"
    );
    assert_eq!(budget_row_counts(&fixture.database).await, (0, 1, 2));
    // A restart after settlement must not touch the settled hold or fold
    // usage a second time.
    let report = CrashReconciler::new(fixture.database.clone())
        .reconcile_once()
        .await
        .expect("restart reconciliation succeeds");
    assert_eq!(report.user_budget_holds_released, 0);
    assert_eq!(report.instance_budget_holds_released, 0);
    assert!(report.converged);
    assert_eq!(
        hold_status(&fixture.database, request_id).await.0,
        "settled"
    );
    assert_eq!(budget_row_counts(&fixture.database).await, (0, 1, 2));
    fixture.database.close().await.unwrap();
}

#[tokio::test]
async fn duplicate_terminal_after_crash_release_cannot_double_fold() {
    let fixture = fixture().await;
    let budgets = UserBudgetService::new(fixture.database.clone());
    hard_rule(&budgets, None, hard_limits(Some(1_000_000), None)).await;
    let outcome = PublicationService::new(fixture.database.clone())
        .publish_attributed(
            claim(&fixture).await,
            hard_input("budget-crash-duplicate", 1, 100, Some(50), None),
            Some(attribution()),
        )
        .await
        .expect("bounded hard-token request publishes");
    let PublicationOutcome::Published(published) = outcome else {
        panic!("request should publish");
    };
    let identity = published.identity.clone();
    drop(published.claim);
    CrashReconciler::new(fixture.database.clone())
        .reconcile_once()
        .await
        .expect("restart reconciliation succeeds");
    // A stale duplicate terminal command for the interrupted request
    // conflicts instead of folding usage onto a released hold.
    let duplicate = DurableFinalizer::new(fixture.database.clone())
        .finalize_request(&identity, FinalizationData::default(), None)
        .await;
    assert!(
        matches!(duplicate, Err(FinalizationError::TerminalConflict { .. })),
        "duplicate terminal must conflict, got {duplicate:?}"
    );
    assert_eq!(budget_row_counts(&fixture.database).await, (0, 0, 0));
    fixture.database.close().await.unwrap();
}

#[tokio::test]
async fn cancel_terminalizes_and_settles_user_hold_conservatively() {
    let fixture = fixture().await;
    let budgets = UserBudgetService::new(fixture.database.clone());
    hard_rule(&budgets, None, hard_limits(Some(1_000_000), None)).await;
    let outcome = PublicationService::new(fixture.database.clone())
        .publish_attributed(
            claim(&fixture).await,
            hard_input("budget-crash-cancel", 1, 100, Some(50), None),
            Some(attribution()),
        )
        .await
        .expect("bounded hard-token request publishes");
    let PublicationOutcome::Published(published) = outcome else {
        panic!("request should publish");
    };
    let request_id = published.identity.db_request_id;
    let held: i64 = fixture
        .database
        .call(move |connection| {
            connection.query_row(
                "SELECT held_microdollars FROM user_budget_reservations WHERE request_id = ?1",
                [request_id],
                |row| row.get(0),
            )
        })
        .await
        .unwrap();
    DurableFinalizer::new(fixture.database.clone())
        .finalize_request(
            &published.identity,
            FinalizationData {
                outcome: FinalizationOutcome::ClientCancelled,
                ..FinalizationData::default()
            },
            Some(published.claim),
        )
        .await
        .expect("cancel terminalizes");
    // Cancel is terminal: the hold settles at the conservative unknown-usage
    // charge and a restart leaves it untouched. Day and month buckets each
    // carry the held charge exactly once.
    assert_eq!(
        hold_status(&fixture.database, request_id).await.0,
        "settled"
    );
    let bucket_costs: Vec<i64> = fixture
        .database
        .call(|connection| {
            let mut statement = connection
                .prepare("SELECT cost_microdollars FROM user_budget_usage ORDER BY window")?;
            let rows = statement.query_map([], |row| row.get(0))?;
            rows.collect::<Result<Vec<_>, _>>()
        })
        .await
        .unwrap();
    assert_eq!(bucket_costs, vec![held, held]);
    let report = CrashReconciler::new(fixture.database.clone())
        .reconcile_once()
        .await
        .expect("restart reconciliation succeeds");
    assert!(report.converged);
    assert_eq!(report.user_budget_holds_released, 0);
    fixture.database.close().await.unwrap();
}

#[tokio::test]
async fn backup_during_active_reservation_captures_hold_verbatim() {
    let directory = tempfile::tempdir().expect("temp root");
    let config_path = directory.path().join("config.toml");
    let db_path = directory.path().join("usage.sqlite3");
    let mut config = Config::default();
    config.database.path = db_path.display().to_string();
    std::fs::write(&config_path, toml::to_string(&config).expect("config TOML"))
        .expect("config writes");
    let fixture = fixture_on(&DatabaseConfig {
        path: db_path.display().to_string(),
        ..Default::default()
    })
    .await;
    let budgets = UserBudgetService::new(fixture.database.clone());
    hard_rule(&budgets, None, hard_limits(Some(1_000_000), None)).await;
    let outcome = PublicationService::new(fixture.database.clone())
        .publish_attributed(
            claim(&fixture).await,
            hard_input("budget-backup-hold", 1, 100, Some(50), None),
            Some(attribution()),
        )
        .await
        .expect("bounded hard-token request publishes");
    let PublicationOutcome::Published(published) = outcome else {
        panic!("request should publish");
    };
    let request_id = published.identity.db_request_id;

    // Snapshot while the hold is active: the online backup must carry the
    // pending request and its active hold exactly as committed.
    let service = BackupService::from_config(&config_path, &config)
        .with_output_dir(directory.path().join("backups"));
    let archive = service
        .create(&fixture.database)
        .await
        .expect("backup during active hold")
        .archive;
    BackupService::validate_archive(&archive).expect("archive validates");

    // Restore over the live paths and prove the hold survived the round
    // trip with its active status and held amounts intact. The WAL is
    // checkpointed before close so no stale sidecar can shadow the
    // restored image on reopen.
    drop(published.claim);
    fixture
        .database
        .call(|connection| {
            connection.query_row("PRAGMA wal_checkpoint(TRUNCATE)", [], |_| Ok(()))?;
            Ok(())
        })
        .await
        .expect("wal checkpoints");
    fixture
        .database
        .close()
        .await
        .expect("live database closes");
    let restored = service.recover(&archive).expect("restore");
    assert_eq!(restored.database, db_path);
    let reopened = Database::open(DatabaseConfig {
        path: db_path.display().to_string(),
        ..Default::default()
    })
    .await
    .expect("restored database opens");
    let request_status: String = reopened
        .call(move |connection| {
            connection.query_row(
                "SELECT status FROM requests WHERE id = ?1",
                [request_id],
                |row| row.get(0),
            )
        })
        .await
        .unwrap();
    assert_eq!(request_status, "pending");
    assert_eq!(hold_status(&reopened, request_id).await.0, "active");
    // The restored copy reconciles exactly like a crashed live database:
    // the request interrupts and the hold releases with no usage folded.
    let report = CrashReconciler::new(reopened.clone())
        .reconcile_once()
        .await
        .expect("restored database reconciles");
    assert_eq!(report.requests_interrupted, 1);
    assert_eq!(report.user_budget_holds_released, 1);
    assert_eq!(hold_status(&reopened, request_id).await.0, "released");
    reopened.close().await.expect("restored database closes");
}
