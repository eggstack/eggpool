use std::collections::BTreeSet;
use std::time::Instant;

use eggpool::health::HealthManager;
use eggpool::{
    Config,
    accounts::{AccountRegistry, CredentialStore},
    catalog::{ModelCatalogCache, ModelInput, ProtocolResolutionStatus},
    coordinator::{
        CrashReconciler, DurableFinalizer, FinalizationData, FinalizationOutcome, PublicationError,
        PublicationInput, PublicationOutcome, PublicationService,
    },
    db::{Account, Database, DatabaseConfig, MigrationRunner},
    identity::{AuthenticatedPrincipal, PrincipalKind},
    instance_budget::{InstanceBudgetError, InstanceBudgetService},
    quota::{AccountQuota, QuotaEstimator},
    routing::{EligibilityPolicy, RoutingRequestFacts, RoutingRouter},
    user_budget::{BudgetEnforcement, BudgetLimits, BudgetProof, BudgetWindow, UserBudgetService},
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
            for model in ["model-a", "model-b"] {
                connection.execute(
                    "INSERT INTO models (model_id, protocol, provider_id, resolution_status)
                     VALUES (?1, 'openai', 'provider-a', 'resolved')",
                    [model],
                )?;
            }
            connection.execute(
                "INSERT INTO users(id, username, normalized_username, role, password_hash)
                 VALUES ('budget-admin', 'budget-admin', 'budget-admin', 'admin', 'test-hash')",
                [],
            )?;
            for user in ["budget-member", "budget-member-2"] {
                connection.execute(
                    "INSERT INTO users(id, username, normalized_username, role, password_hash)
                     VALUES (?1, ?1, ?1, 'member', 'test-hash')",
                    [user],
                )?;
            }
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
    let models: Vec<ModelInput> = ["model-a", "model-b"]
        .into_iter()
        .map(|model_id| {
            let mut model = ModelInput::new(model_id);
            model.protocol = Some("openai".into());
            model.protocol_source = Some("fixture".into());
            model.resolution_status = ProtocolResolutionStatus::Resolved;
            model
        })
        .collect();
    catalog
        .update_from_account("budget-account", "provider-a", &models, true, true)
        .expect("catalog models");
    // The health gate treats an account with no manager as not healthy, so the
    // fixture registers every account the router can select.
    let health = HealthManager::new();
    for account in registry.all() {
        health.register_account(account.account_id, &account.account_name);
    }
    let router = RoutingRouter::new(
        registry,
        catalog,
        QuotaEstimator::new([AccountQuota::new("budget-account")]),
        Some(health),
        EligibilityPolicy::default(),
    );
    Fixture { database, router }
}

async fn extra_member(database: &Database, username: &str) {
    database
        .call({
            let username = username.to_owned();
            move |connection| {
                connection.execute(
                    "INSERT INTO users(id, username, normalized_username, role, password_hash)
                     VALUES (?1, ?1, ?1, 'member', 'test-hash')",
                    [&username],
                )?;
                Ok(())
            }
        })
        .await
        .expect("extra member inserts");
}

async fn claim_for(fixture: &Fixture, model: &str) -> eggpool::routing::SelectionClaim {
    let mut facts = RoutingRequestFacts::new(model);
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

fn attribution_for(user: &str) -> eggpool::identity::RequestAttribution {
    AuthenticatedPrincipal {
        user_id: Some(user.into()),
        credential_id: None,
        role: Some("member".into()),
        scopes: vec!["inference".into()],
        auth_epoch: Some(1),
        kind: PrincipalKind::AgentToken,
    }
    .request_attribution()
}

fn estimated_input(proxy_id: &str, attempt: i64) -> PublicationInput {
    PublicationInput::new(proxy_id, "openai", "openai", false, attempt)
}

fn hard_input(
    proxy_id: &str,
    attempt: i64,
    output: Option<i64>,
    tariff: Option<i64>,
) -> PublicationInput {
    let mut input = PublicationInput::new(proxy_id, "openai", "openai", false, attempt);
    input.budget_proof = Some(BudgetProof {
        input_worst_case_tokens: 100,
        requested_output_tokens: output,
        tariff_microdollars_per_million_tokens: tariff,
    });
    input
}

fn estimated_limits(
    requests: Option<i64>,
    tokens: Option<i64>,
    microdollars: Option<i64>,
    concurrent: Option<i64>,
) -> BudgetLimits {
    BudgetLimits {
        requests,
        tokens,
        microdollars,
        concurrent,
        ..Default::default()
    }
}

fn hard_limits(tokens: Option<i64>, microdollars: Option<i64>) -> BudgetLimits {
    BudgetLimits {
        tokens,
        microdollars,
        enforcement: BudgetEnforcement::Hard,
        ..Default::default()
    }
}

async fn set_global(
    budgets: &InstanceBudgetService,
    actor: &str,
    model_id: Option<&str>,
    limits: BudgetLimits,
) {
    budgets
        .set_rule(actor, model_id, BudgetWindow::Day, limits)
        .await
        .expect("admin sets global rule");
}

async fn instance_counts(database: &Database) -> (i64, i64, i64) {
    database
        .call(|connection| {
            Ok((
                connection.query_row("SELECT count(*) FROM requests", [], |row| {
                    row.get::<_, i64>(0)
                })?,
                connection.query_row(
                    "SELECT count(*) FROM instance_budget_reservations WHERE status = 'active'",
                    [],
                    |row| row.get::<_, i64>(0),
                )?,
                connection.query_row("SELECT count(*) FROM instance_budget_usage", [], |row| {
                    row.get::<_, i64>(0)
                })?,
            ))
        })
        .await
        .unwrap()
}

async fn instance_usage_requests(database: &Database) -> i64 {
    database
        .call(|connection| {
            connection.query_row(
                "SELECT coalesce(sum(request_count), 0) FROM instance_budget_usage",
                [],
                |row| row.get::<_, i64>(0),
            )
        })
        .await
        .unwrap()
}

#[tokio::test]
async fn global_request_last_slot_admits_exactly_one_across_users() {
    let fixture = fixture().await;
    let budgets = InstanceBudgetService::new(fixture.database.clone());
    set_global(
        &budgets,
        "budget-admin",
        None,
        estimated_limits(Some(1), None, None, None),
    )
    .await;

    let service = PublicationService::new(fixture.database.clone());
    let first = service.publish_attributed(
        claim_for(&fixture, "model-a").await,
        estimated_input("global-race-a", 1),
        Some(attribution_for("budget-member")),
    );
    let second = service.publish_attributed(
        claim_for(&fixture, "model-a").await,
        estimated_input("global-race-b", 1),
        Some(attribution_for("budget-member-2")),
    );
    let (first, second) = tokio::join!(first, second);
    let mut winners = 0;
    let mut denied = 0;
    for result in [first, second] {
        match result {
            Ok(PublicationOutcome::Published(_)) => winners += 1,
            Err(PublicationError::BudgetExceeded) => denied += 1,
            other => panic!("unexpected global race result: {other:?}"),
        }
    }
    assert_eq!((winners, denied), (1, 1));
    // One logical request, one shared hold: two separately authenticated
    // accounts cannot both take the final global slot.
    assert_eq!(instance_counts(&fixture.database).await, (1, 1, 0));
    fixture.database.close().await.unwrap();
}

#[tokio::test]
async fn global_last_slot_race_scales_to_10_and_49_identities() {
    for size in [10_usize, 49] {
        let fixture = fixture().await;
        for index in 0..size {
            extra_member(&fixture.database, &format!("race-user-{index:02}")).await;
        }
        let budgets = InstanceBudgetService::new(fixture.database.clone());
        set_global(
            &budgets,
            "budget-admin",
            None,
            estimated_limits(Some(1), None, None, None),
        )
        .await;

        // Claims are built sequentially; only the durable publication races,
        // which is the atomicity boundary under test.
        let mut claims = Vec::new();
        for _ in 0..size {
            claims.push(claim_for(&fixture, "model-a").await);
        }
        let started = Instant::now();
        let mut racers = tokio::task::JoinSet::new();
        for (index, claim) in claims.into_iter().enumerate() {
            let service = PublicationService::new(fixture.database.clone());
            let user = format!("race-user-{index:02}");
            racers.spawn(async move {
                service
                    .publish_attributed(
                        claim,
                        estimated_input(&format!("global-race-{index:02}"), 1),
                        Some(attribution_for(&user)),
                    )
                    .await
            });
        }
        let mut winners = 0;
        let mut denied = 0;
        while let Some(outcome) = racers.join_next().await {
            match outcome.expect("racer completes") {
                Ok(PublicationOutcome::Published(_)) => winners += 1,
                Err(PublicationError::BudgetExceeded) => denied += 1,
                other => panic!("unexpected {size}-way race result: {other:?}"),
            }
        }
        let elapsed = started.elapsed();
        eprintln!("global last-slot race with {size} identities took {elapsed:?}");
        assert_eq!(winners, 1, "{size} racers must leave exactly one winner");
        assert_eq!(denied, size - 1);
        assert_eq!(instance_counts(&fixture.database).await, (1, 1, 0));
        fixture.database.close().await.unwrap();
    }
}

#[tokio::test]
async fn global_and_user_rules_intersect_without_bypass() {
    let fixture = fixture().await;
    let user_budgets = UserBudgetService::new(fixture.database.clone());
    user_budgets
        .set_rule(
            "budget-admin",
            "budget-member",
            None,
            BudgetWindow::Day,
            estimated_limits(Some(10), None, None, None),
        )
        .await
        .unwrap();
    let global = InstanceBudgetService::new(fixture.database.clone());
    set_global(
        &global,
        "budget-admin",
        None,
        estimated_limits(Some(1), None, None, None),
    )
    .await;

    let service = PublicationService::new(fixture.database.clone());
    service
        .publish_attributed(
            claim_for(&fixture, "model-a").await,
            estimated_input("intersect-a", 1),
            Some(attribution_for("budget-member")),
        )
        .await
        .expect("first request satisfies both scopes");
    // The user scope still allows nine more, but the global scope is spent.
    let denied = service
        .publish_attributed(
            claim_for(&fixture, "model-a").await,
            estimated_input("intersect-b", 1),
            Some(attribution_for("budget-member")),
        )
        .await;
    assert!(
        matches!(denied, Err(PublicationError::BudgetExceeded)),
        "global exhaustion must deny despite user headroom, got {denied:?}"
    );
    // A different user with a fresh user budget is still globally capped.
    let other = service
        .publish_attributed(
            claim_for(&fixture, "model-a").await,
            estimated_input("intersect-c", 1),
            Some(attribution_for("budget-member-2")),
        )
        .await;
    assert!(matches!(other, Err(PublicationError::BudgetExceeded)));
    fixture.database.close().await.unwrap();
}

#[tokio::test]
async fn model_scoped_global_shares_one_budget_across_two_models() {
    let fixture = fixture().await;
    let global = InstanceBudgetService::new(fixture.database.clone());
    set_global(
        &global,
        "budget-admin",
        None,
        estimated_limits(Some(2), None, None, None),
    )
    .await;
    // Instance-model ceiling on model-a only.
    global
        .set_rule(
            "budget-admin",
            Some("model-a"),
            BudgetWindow::Day,
            estimated_limits(Some(1), None, None, None),
        )
        .await
        .unwrap();

    let service = PublicationService::new(fixture.database.clone());
    service
        .publish_attributed(
            claim_for(&fixture, "model-a").await,
            estimated_input("shared-a", 1),
            Some(attribution_for("budget-member")),
        )
        .await
        .expect("first model-a request publishes");
    let model_a_again = service
        .publish_attributed(
            claim_for(&fixture, "model-a").await,
            estimated_input("shared-a2", 1),
            Some(attribution_for("budget-member-2")),
        )
        .await;
    assert!(
        matches!(model_a_again, Err(PublicationError::BudgetExceeded)),
        "model-scoped global ceiling must deny, got {model_a_again:?}"
    );
    // model-b shares the all-model global budget (one of two slots spent)
    // but is outside the model-a ceiling.
    service
        .publish_attributed(
            claim_for(&fixture, "model-b").await,
            estimated_input("shared-b", 1),
            Some(attribution_for("budget-member-2")),
        )
        .await
        .expect("model-b fits the remaining global slot");
    let global_spent = service
        .publish_attributed(
            claim_for(&fixture, "model-b").await,
            estimated_input("shared-b2", 1),
            Some(attribution_for("budget-member")),
        )
        .await;
    assert!(matches!(
        global_spent,
        Err(PublicationError::BudgetExceeded)
    ));
    fixture.database.close().await.unwrap();
}

#[tokio::test]
async fn global_retry_and_settlement_count_one_logical_request() {
    let fixture = fixture().await;
    let global = InstanceBudgetService::new(fixture.database.clone());
    set_global(
        &global,
        "budget-admin",
        None,
        estimated_limits(Some(1), None, None, None),
    )
    .await;

    let service = PublicationService::new(fixture.database.clone());
    let first = service
        .publish_attributed(
            claim_for(&fixture, "model-a").await,
            estimated_input("global-once", 1),
            Some(attribution_for("budget-member")),
        )
        .await
        .expect("first attempt publishes");
    let PublicationOutcome::Published(first) = first else {
        panic!("first attempt should publish");
    };
    DurableFinalizer::new(fixture.database.clone())
        .finalize_failed_attempt(
            &first.identity,
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
            Some(first.claim),
        )
        .await
        .expect("failed attempt keeps the global hold");
    // Retry reuses the logical hold: it must not consume a second global
    // request even though the slot is the last one.
    let retry = service
        .publish_attributed(
            claim_for(&fixture, "model-a").await,
            estimated_input("global-once", 2),
            Some(attribution_for("budget-member")),
        )
        .await
        .expect("retry extends the same logical hold");
    let PublicationOutcome::Published(retry) = retry else {
        panic!("retry should publish");
    };
    DurableFinalizer::new(fixture.database.clone())
        .finalize_request(
            &retry.identity,
            FinalizationData {
                outcome: FinalizationOutcome::Completed,
                input_tokens: 30,
                output_tokens: 12,
                input_tokens_reported: Some(30),
                output_tokens_reported: Some(12),
                cost_microdollars: 21,
                provider_cost_microdollars: Some(21),
                ..FinalizationData::default()
            },
            Some(retry.claim),
        )
        .await
        .expect("terminal settlement folds one logical request");
    // One logical request settled into day + month buckets; the hold is gone.
    assert_eq!(instance_counts(&fixture.database).await.1, 0);
    assert_eq!(instance_usage_requests(&fixture.database).await, 2);
    let usage = global
        .usage("2026-01-01", 10)
        .await
        .expect("bounded instance usage reads succeed");
    assert_eq!(usage.len(), 2);
    // Both attempts fold into one logical request: 10+5 failed, 30+12
    // terminal; provider-reported cost 4 + 21.
    assert!(usage.iter().all(|bucket| bucket.request_count == 1
        && bucket.token_count == 57
        && bucket.cost_microdollars == 25));
    fixture.database.close().await.unwrap();
}

#[tokio::test]
async fn global_concurrency_releases_only_after_terminal_settlement() {
    let fixture = fixture().await;
    let global = InstanceBudgetService::new(fixture.database.clone());
    set_global(
        &global,
        "budget-admin",
        None,
        estimated_limits(None, None, None, Some(1)),
    )
    .await;

    let service = PublicationService::new(fixture.database.clone());
    let first = service
        .publish_attributed(
            claim_for(&fixture, "model-a").await,
            estimated_input("global-conc-a", 1),
            Some(attribution_for("budget-member")),
        )
        .await
        .expect("first concurrent request publishes");
    let PublicationOutcome::Published(first) = first else {
        panic!("first request should publish");
    };
    let second = service
        .publish_attributed(
            claim_for(&fixture, "model-a").await,
            estimated_input("global-conc-b", 1),
            Some(attribution_for("budget-member-2")),
        )
        .await;
    assert!(matches!(second, Err(PublicationError::BudgetExceeded)));
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
        .expect("terminal settlement releases the global slot");
    service
        .publish_attributed(
            claim_for(&fixture, "model-a").await,
            estimated_input("global-conc-c", 1),
            Some(attribution_for("budget-member-2")),
        )
        .await
        .expect("settlement releases the concurrency slot");
    fixture.database.close().await.unwrap();
}

#[tokio::test]
async fn global_window_projection_ignores_crossed_utc_boundaries() {
    let fixture = fixture().await;
    let global = InstanceBudgetService::new(fixture.database.clone());
    global
        .set_rule(
            "budget-admin",
            None,
            BudgetWindow::Day,
            estimated_limits(Some(1), None, None, None),
        )
        .await
        .unwrap();
    global
        .set_rule(
            "budget-admin",
            None,
            BudgetWindow::Month,
            estimated_limits(Some(1), None, None, None),
        )
        .await
        .unwrap();

    let service = PublicationService::new(fixture.database.clone());
    let first = service
        .publish_attributed(
            claim_for(&fixture, "model-a").await,
            estimated_input("global-roll-a", 1),
            Some(attribution_for("budget-member")),
        )
        .await
        .expect("first request publishes");
    let PublicationOutcome::Published(first) = first else {
        panic!("first request should publish");
    };
    // Simulate the hold surviving across UTC resets: backdate both window
    // starts. Today's windows must read as free; the concurrency count must
    // still see the live hold.
    fixture
        .database
        .call({
            let request_id = first.identity.db_request_id;
            move |connection| {
                connection.execute(
                    "UPDATE instance_budget_reservations
                     SET day_start = date('now', '-1 day'),
                         month_start = strftime('%Y-%m-01', 'now', '-1 month')
                     WHERE request_id = ?1",
                    [request_id],
                )?;
                Ok(())
            }
        })
        .await
        .unwrap();
    service
        .publish_attributed(
            claim_for(&fixture, "model-a").await,
            estimated_input("global-roll-b", 1),
            Some(attribution_for("budget-member-2")),
        )
        .await
        .expect("crossed windows free today's request slot");
    // Today's slot is now held again: a third request is denied.
    let third = service
        .publish_attributed(
            claim_for(&fixture, "model-a").await,
            estimated_input("global-roll-c", 1),
            Some(attribution_for("budget-member")),
        )
        .await;
    assert!(matches!(third, Err(PublicationError::BudgetExceeded)));
    first.claim.release_all().expect("test claim is released");
    fixture.database.close().await.unwrap();
}

#[tokio::test]
async fn legacy_unattributed_requests_count_against_global_ceiling() {
    let fixture = fixture().await;
    let global = InstanceBudgetService::new(fixture.database.clone());
    set_global(
        &global,
        "budget-admin",
        None,
        estimated_limits(Some(1), None, None, None),
    )
    .await;

    let service = PublicationService::new(fixture.database.clone());
    service
        .publish_attributed(
            claim_for(&fixture, "model-a").await,
            estimated_input("global-legacy", 1),
            None,
        )
        .await
        .expect("unattributed request takes the global slot");
    // Switching to a named user cannot bypass the spent global ceiling,
    // and the legacy hold settles into instance usage without a user row.
    let bypass = service
        .publish_attributed(
            claim_for(&fixture, "model-a").await,
            estimated_input("global-legacy-b", 1),
            Some(attribution_for("budget-member")),
        )
        .await;
    assert!(
        matches!(bypass, Err(PublicationError::BudgetExceeded)),
        "identity switch must not bypass the global ceiling, got {bypass:?}"
    );
    fixture.database.close().await.unwrap();
}

#[tokio::test]
async fn legacy_unattributed_settlement_folds_instance_usage() {
    let fixture = fixture().await;
    let global = InstanceBudgetService::new(fixture.database.clone());
    set_global(
        &global,
        "budget-admin",
        None,
        estimated_limits(Some(10), None, None, None),
    )
    .await;

    let service = PublicationService::new(fixture.database.clone());
    let published = service
        .publish_attributed(
            claim_for(&fixture, "model-a").await,
            estimated_input("global-legacy-settle", 1),
            None,
        )
        .await
        .expect("unattributed request publishes");
    let PublicationOutcome::Published(published) = published else {
        panic!("request should publish");
    };
    DurableFinalizer::new(fixture.database.clone())
        .finalize_request(
            &published.identity,
            FinalizationData {
                outcome: FinalizationOutcome::Completed,
                input_tokens: 30,
                output_tokens: 12,
                input_tokens_reported: Some(30),
                output_tokens_reported: Some(12),
                cost_microdollars: 21,
                provider_cost_microdollars: Some(21),
                ..FinalizationData::default()
            },
            Some(published.claim),
        )
        .await
        .expect("unattributed terminal settlement folds instance usage");
    assert_eq!(instance_usage_requests(&fixture.database).await, 2);
    fixture.database.close().await.unwrap();
}

#[tokio::test]
async fn disabled_user_does_not_erase_global_history() {
    let fixture = fixture().await;
    let global = InstanceBudgetService::new(fixture.database.clone());
    set_global(
        &global,
        "budget-admin",
        None,
        estimated_limits(Some(10), None, None, None),
    )
    .await;

    let service = PublicationService::new(fixture.database.clone());
    let published = service
        .publish_attributed(
            claim_for(&fixture, "model-a").await,
            estimated_input("global-history", 1),
            Some(attribution_for("budget-member")),
        )
        .await
        .expect("member request publishes");
    let PublicationOutcome::Published(published) = published else {
        panic!("request should publish");
    };
    DurableFinalizer::new(fixture.database.clone())
        .finalize_request(
            &published.identity,
            FinalizationData {
                outcome: FinalizationOutcome::Completed,
                input_tokens: 42,
                input_tokens_reported: Some(42),
                ..FinalizationData::default()
            },
            Some(published.claim),
        )
        .await
        .expect("settlement records global usage");
    fixture
        .database
        .call(|connection| {
            connection.execute(
                "UPDATE users SET state = 'disabled' WHERE id = 'budget-member'",
                [],
            )?;
            Ok(())
        })
        .await
        .unwrap();
    // Global counters never depended on the user row: history and remaining
    // headroom are intact after disable.
    assert_eq!(instance_usage_requests(&fixture.database).await, 2);
    service
        .publish_attributed(
            claim_for(&fixture, "model-a").await,
            estimated_input("global-history-2", 1),
            Some(attribution_for("budget-member-2")),
        )
        .await
        .expect("remaining global headroom is unaffected by the disable");
    fixture.database.close().await.unwrap();
}

#[tokio::test]
async fn global_hard_token_and_usd_deny_unprovable_without_upstream_io() {
    let fixture = fixture().await;
    let global = InstanceBudgetService::new(fixture.database.clone());
    global
        .set_rule(
            "budget-admin",
            None,
            BudgetWindow::Day,
            hard_limits(Some(1_000_000), None),
        )
        .await
        .unwrap();
    let service = PublicationService::new(fixture.database.clone());
    let unbounded = service
        .publish_attributed(
            claim_for(&fixture, "model-a").await,
            hard_input("global-hard-unbounded", 1, None, Some(3_000_000)),
            Some(attribution_for("budget-member")),
        )
        .await;
    assert!(matches!(
        unbounded,
        Err(PublicationError::BudgetNotEnforceable)
    ));

    global
        .set_rule(
            "budget-admin",
            Some("model-b"),
            BudgetWindow::Day,
            hard_limits(None, Some(1_000_000)),
        )
        .await
        .unwrap();
    let unpriced = service
        .publish_attributed(
            claim_for(&fixture, "model-b").await,
            hard_input("global-hard-unpriced", 1, Some(50), None),
            Some(attribution_for("budget-member")),
        )
        .await;
    assert!(matches!(
        unpriced,
        Err(PublicationError::BudgetNotEnforceable)
    ));
    assert_eq!(instance_counts(&fixture.database).await, (0, 0, 0));

    // The same hard scopes admit with proof: token worst case 150 × 2 and
    // tariff-converted cost on the same hold.
    let admitted = service
        .publish_attributed(
            claim_for(&fixture, "model-b").await,
            hard_input("global-hard-ok", 1, Some(50), Some(1)),
            Some(attribution_for("budget-member")),
        )
        .await
        .expect("proven hard request publishes");
    let PublicationOutcome::Published(admitted) = admitted else {
        panic!("request should publish");
    };
    let held = fixture
        .database
        .call({
            let request_id = admitted.identity.db_request_id;
            move |connection| {
                connection.query_row(
                    "SELECT held_tokens, held_microdollars, enforcement, price_source
                     FROM instance_budget_reservations WHERE request_id = ?1",
                    [request_id],
                    |row| {
                        Ok((
                            row.get::<_, i64>(0)?,
                            row.get::<_, i64>(1)?,
                            row.get::<_, String>(2)?,
                            row.get::<_, Option<String>>(3)?,
                        ))
                    },
                )
            }
        })
        .await
        .unwrap();
    // Token-hard global rule is model-agnostic (matches model-b): 300-token
    // hold. Cost-hard model-b rule rounds the tiny positive conversion up to
    // one microdollar instead of admitting with a zero hold.
    assert_eq!(
        held,
        (
            300,
            1,
            "hard".to_owned(),
            Some("operator_tariff".to_owned())
        )
    );
    fixture.database.close().await.unwrap();
}

#[tokio::test]
async fn global_rules_validate_admin_scope_and_reject_malformed() {
    let fixture = fixture().await;
    let global = InstanceBudgetService::new(fixture.database.clone());
    assert!(matches!(
        global
            .set_rule(
                "budget-member",
                None,
                BudgetWindow::Day,
                estimated_limits(Some(1), None, None, None),
            )
            .await,
        Err(InstanceBudgetError::ActorNotAdmin)
    ));
    assert!(matches!(
        global
            .remove_rule("budget-member", None, BudgetWindow::Day)
            .await,
        Err(InstanceBudgetError::ActorNotAdmin)
    ));
    for bad in [
        estimated_limits(Some(0), None, None, None),
        estimated_limits(Some(-3), None, None, None),
        estimated_limits(None, None, None, None),
    ] {
        assert!(matches!(
            global
                .set_rule("budget-admin", None, BudgetWindow::Day, bad)
                .await,
            Err(InstanceBudgetError::InvalidRule)
        ));
    }
    global
        .set_rule(
            "budget-admin",
            Some("model-a"),
            BudgetWindow::Day,
            estimated_limits(Some(5), None, None, None),
        )
        .await
        .unwrap();
    assert_eq!(global.rules().await.unwrap().len(), 1);
    assert!(
        global
            .remove_rule("budget-admin", Some("model-a"), BudgetWindow::Day)
            .await
            .unwrap()
    );
    assert!(global.rules().await.unwrap().is_empty());
    assert!(global.usage("2026-01-01", 501).await.is_err());
    fixture.database.close().await.unwrap();
}

#[tokio::test]
async fn no_global_rules_preserves_single_admin_parity() {
    let fixture = fixture().await;
    let service = PublicationService::new(fixture.database.clone());
    // Attributed and legacy requests take no instance hold and write no
    // instance rows when no global rule exists.
    for (proxy, attribution) in [
        ("parity-a", Some(attribution_for("budget-member"))),
        ("parity-b", None),
    ] {
        service
            .publish_attributed(
                claim_for(&fixture, "model-a").await,
                estimated_input(proxy, 1),
                attribution,
            )
            .await
            .expect("rule-free publication is unchanged");
    }
    assert_eq!(instance_counts(&fixture.database).await, (2, 0, 0));
    fixture.database.close().await.unwrap();
}

// ---------------------------------------------------------------------------
// M010 crash recovery: instance-global holds converge on restart without
// folding usage, and a released global hold never blocks the next user.
// ---------------------------------------------------------------------------

#[tokio::test]
async fn crash_after_publication_releases_instance_hold_without_folding_usage() {
    let fixture = fixture().await;
    let global = InstanceBudgetService::new(fixture.database.clone());
    // One global concurrency slot shared by every user: a phantom hold
    // would wedge the whole instance, not just one member.
    set_global(
        &global,
        "budget-admin",
        None,
        BudgetLimits {
            tokens: Some(1_000_000),
            concurrent: Some(1),
            enforcement: BudgetEnforcement::Hard,
            ..Default::default()
        },
    )
    .await;
    let service = PublicationService::new(fixture.database.clone());
    let outcome = service
        .publish_attributed(
            claim_for(&fixture, "model-a").await,
            hard_input("global-crash-hold", 1, Some(50), None),
            Some(attribution_for("budget-member")),
        )
        .await
        .expect("bounded global request publishes");
    let PublicationOutcome::Published(published) = outcome else {
        panic!("request should publish");
    };
    let request_id = published.identity.db_request_id;
    assert_eq!(instance_counts(&fixture.database).await, (1, 1, 0));
    drop(published.claim);

    let report = CrashReconciler::new(fixture.database.clone())
        .reconcile_once()
        .await
        .expect("restart reconciliation succeeds");
    assert_eq!(report.requests_interrupted, 1);
    assert_eq!(report.instance_budget_holds_released, 1);
    // The attributed member request also carries a rule-free user hold for
    // usage accounting; it converges in the same pass.
    assert_eq!(report.user_budget_holds_released, 1);
    assert!(
        report
            .classification
            .pending_requests_with_active_instance_hold
            >= 1
    );
    // Compensation, not credit: released, never settled, no usage folded.
    assert_eq!(instance_counts(&fixture.database).await, (1, 0, 0));
    let hold: (String, Option<String>) = fixture
        .database
        .call(move |connection| {
            connection.query_row(
                "SELECT status, settled_at FROM instance_budget_reservations WHERE request_id = ?1",
                [request_id],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
        })
        .await
        .unwrap();
    assert_eq!(hold, ("released".to_owned(), None));

    // The freed global slot admits a different member's request: no
    // instance-wide wedge from the phantom hold.
    let next = service
        .publish_attributed(
            claim_for(&fixture, "model-a").await,
            hard_input("global-crash-next", 1, Some(50), None),
            Some(attribution_for("budget-member-2")),
        )
        .await
        .expect("next member admits after crash release");
    assert!(
        matches!(next, PublicationOutcome::Published(_)),
        "next member should publish"
    );
    fixture.database.close().await.unwrap();
}

#[tokio::test]
async fn settled_global_request_survives_restart_without_double_fold() {
    let fixture = fixture().await;
    let global = InstanceBudgetService::new(fixture.database.clone());
    set_global(
        &global,
        "budget-admin",
        None,
        hard_limits(Some(1_000_000), None),
    )
    .await;
    let outcome = PublicationService::new(fixture.database.clone())
        .publish_attributed(
            claim_for(&fixture, "model-a").await,
            hard_input("global-crash-settled", 1, Some(50), None),
            Some(attribution_for("budget-member")),
        )
        .await
        .expect("bounded global request publishes");
    let PublicationOutcome::Published(published) = outcome else {
        panic!("request should publish");
    };
    DurableFinalizer::new(fixture.database.clone())
        .finalize_request(
            &published.identity,
            FinalizationData {
                outcome: FinalizationOutcome::Completed,
                ..FinalizationData::default()
            },
            Some(published.claim),
        )
        .await
        .expect("terminal request settles");
    assert_eq!(instance_counts(&fixture.database).await, (1, 0, 2));
    assert_eq!(instance_usage_requests(&fixture.database).await, 2);
    // A restart after settlement touches neither the settled global hold
    // nor the folded buckets.
    let report = CrashReconciler::new(fixture.database.clone())
        .reconcile_once()
        .await
        .expect("restart reconciliation succeeds");
    assert!(report.converged);
    assert_eq!(report.instance_budget_holds_released, 0);
    assert_eq!(report.user_budget_holds_released, 0);
    assert_eq!(instance_counts(&fixture.database).await, (1, 0, 2));
    assert_eq!(instance_usage_requests(&fixture.database).await, 2);
    fixture.database.close().await.unwrap();
}

// ---------------------------------------------------------------------------
// M010 storage-pressure observations: paired baseline vs budget-rule
// publish/settle latency, WAL footprint, and row counts for 1/10/49
// distinct credential identities on ordinary local hardware. This is an
// observation record, not a performance gate: per-op assertions are
// generous anti-hang bounds only. Harness output lines prefixed with
// M010_GATE_OBSERVATION carry the JSON cells archived under
// artifacts/qualification/; M012 owns physical-SBC capacity proof.
// ---------------------------------------------------------------------------

use std::fmt::Write as _;

async fn insert_measurement_members(database: &Database, count: usize) -> Vec<String> {
    let names: Vec<String> = (0..count).map(|index| format!("measure-{index}")).collect();
    let snapshot = names.clone();
    database
        .call(move |connection| {
            for name in &snapshot {
                connection.execute(
                    "INSERT INTO users(id, username, normalized_username, role, password_hash)
                     VALUES (?1, ?1, ?1, 'member', 'test-hash')",
                    [name],
                )?;
            }
            Ok(())
        })
        .await
        .expect("measurement members insert");
    names
}

fn percentile(sorted: &mut [u128], ratio: f64) -> u128 {
    sorted.sort_unstable();
    if sorted.is_empty() {
        return 0;
    }
    let rank = ((sorted.len() as f64 - 1.0) * ratio).round() as usize;
    sorted[rank.min(sorted.len() - 1)]
}

fn wal_footprint(dir: &std::path::Path, stem: &str) -> (u64, u64, u64) {
    let size = |suffix: &str| {
        std::fs::metadata(dir.join(format!("{stem}{suffix}")))
            .map(|metadata| metadata.len())
            .unwrap_or(0)
    };
    (size(".sqlite3"), size(".sqlite3-wal"), size(".sqlite3-shm"))
}

fn process_rss_kb() -> Option<u64> {
    let output = std::process::Command::new("ps")
        .args(["-o", "rss=", "-p", &std::process::id().to_string()])
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    String::from_utf8_lossy(&output.stdout).trim().parse().ok()
}

#[tokio::test]
async fn paired_gate_observations_across_identities_and_rules() {
    let work_root = tempfile::tempdir().expect("work root");
    for identities in [1_usize, 10, 49] {
        for mode in ["baseline", "user_estimated", "user_hard_global_hard"] {
            let stem = format!("m010-gate-{mode}-{identities}");
            let fixture = fixture_on(&DatabaseConfig {
                path: work_root
                    .path()
                    .join(format!("{stem}.sqlite3"))
                    .display()
                    .to_string(),
                ..Default::default()
            })
            .await;
            let members = insert_measurement_members(&fixture.database, identities).await;
            if mode != "baseline" {
                let users = UserBudgetService::new(fixture.database.clone());
                for member in &members {
                    users
                        .set_rule(
                            "budget-admin",
                            member,
                            None,
                            BudgetWindow::Day,
                            BudgetLimits {
                                tokens: Some(1_000_000_000),
                                microdollars: Some(1_000_000_000),
                                ..Default::default()
                            },
                        )
                        .await
                        .expect("measurement user rule is set");
                }
            }
            if mode == "user_hard_global_hard" {
                let global = InstanceBudgetService::new(fixture.database.clone());
                global
                    .set_rule(
                        "budget-admin",
                        None,
                        BudgetWindow::Day,
                        BudgetLimits {
                            requests: Some(1_000_000),
                            tokens: Some(1_000_000_000),
                            enforcement: BudgetEnforcement::Hard,
                            ..Default::default()
                        },
                    )
                    .await
                    .expect("measurement global rule is set");
            }
            // Hard mode needs worst-case proofs; the other modes ride the
            // estimator projection exactly as production does.
            let service = PublicationService::new(fixture.database.clone());
            let finalizer = DurableFinalizer::new(fixture.database.clone());
            let mut publish_us: Vec<u128> = Vec::with_capacity(identities);
            let mut settle_us: Vec<u128> = Vec::with_capacity(identities);
            for (index, member) in members.iter().enumerate() {
                let proxy = format!("{stem}-{index}");
                let started = Instant::now();
                let outcome = if mode == "baseline" {
                    service
                        .publish(
                            claim_for(&fixture, "model-a").await,
                            estimated_input(&proxy, 1),
                        )
                        .await
                        .expect("baseline request publishes")
                } else if mode == "user_hard_global_hard" {
                    service
                        .publish_attributed(
                            claim_for(&fixture, "model-a").await,
                            hard_input(&proxy, 1, Some(50), Some(3_000_000)),
                            Some(attribution_for(member)),
                        )
                        .await
                        .expect("hard request publishes")
                } else {
                    service
                        .publish_attributed(
                            claim_for(&fixture, "model-a").await,
                            estimated_input(&proxy, 1),
                            Some(attribution_for(member)),
                        )
                        .await
                        .expect("estimated request publishes")
                };
                publish_us.push(started.elapsed().as_micros());
                let PublicationOutcome::Published(published) = outcome else {
                    panic!("measurement request should publish");
                };
                let started = Instant::now();
                finalizer
                    .finalize_request(
                        &published.identity,
                        FinalizationData {
                            outcome: FinalizationOutcome::Completed,
                            ..FinalizationData::default()
                        },
                        Some(published.claim),
                    )
                    .await
                    .expect("measurement request settles");
                settle_us.push(started.elapsed().as_micros());
            }
            for sample in publish_us.iter().chain(settle_us.iter()) {
                assert!(
                    *sample < 30_000_000,
                    "per-operation anti-hang bound exceeded: {sample}us"
                );
            }
            let (db_bytes, wal_bytes, shm_bytes) = wal_footprint(work_root.path(), &stem);
            fixture
                .database
                .call(|connection| {
                    connection.query_row("PRAGMA wal_checkpoint(TRUNCATE)", [], |_| Ok(()))?;
                    Ok(())
                })
                .await
                .expect("wal checkpoints");
            let (db_after, wal_after, shm_after) = wal_footprint(work_root.path(), &stem);
            let rows: (i64, i64, i64, i64) = fixture
                .database
                .call(|connection| {
                    Ok((
                        connection.query_row("SELECT COUNT(*) FROM requests", [], |row| {
                            row.get::<_, i64>(0)
                        })?,
                        connection.query_row(
                            "SELECT COUNT(*) FROM user_budget_reservations WHERE status = 'active'",
                            [],
                            |row| row.get::<_, i64>(0),
                        )?,
                        connection.query_row(
                            "SELECT COUNT(*) FROM instance_budget_reservations WHERE status = 'active'",
                            [],
                            |row| row.get::<_, i64>(0),
                        )?,
                        connection.query_row("SELECT COUNT(*) FROM user_budget_usage", [], |row| {
                            row.get::<_, i64>(0)
                        })?,
                    ))
                })
                .await
                .expect("row counts read");
            let mut cell = String::new();
            write!(
                cell,
                "{{\"mode\":\"{mode}\",\"identities\":{identities},\
                 \"publish_us_min\":{},\"publish_us_p50\":{},\"publish_us_max\":{},\
                 \"settle_us_min\":{},\"settle_us_p50\":{},\"settle_us_max\":{},\
                 \"db_bytes\":{db_bytes},\"wal_bytes\":{wal_bytes},\"shm_bytes\":{shm_bytes},\
                 \"wal_bytes_after_checkpoint\":{wal_after},\"shm_bytes_after_checkpoint\":{shm_after},\
                 \"db_bytes_after_checkpoint\":{db_after},\
                 \"requests\":{},\"active_user_holds\":{},\"active_instance_holds\":{},\
                 \"user_usage_rows\":{},\"arch\":\"{}\",\"os\":\"{}\",\"profile\":\"debug\"}}",
                percentile(&mut publish_us.clone(), 0.0),
                percentile(&mut publish_us.clone(), 0.5),
                percentile(&mut publish_us.clone(), 1.0),
                percentile(&mut settle_us.clone(), 0.0),
                percentile(&mut settle_us.clone(), 0.5),
                percentile(&mut settle_us.clone(), 1.0),
                rows.0,
                rows.1,
                rows.2,
                rows.3,
                std::env::consts::ARCH,
                std::env::consts::OS,
            )
            .expect("cell formats");
            println!("M010_GATE_OBSERVATION {cell}");
            fixture.database.close().await.expect("database closes");
        }
    }
    if let Some(rss) = process_rss_kb() {
        println!("M010_GATE_OBSERVATION {{\"rss_kb_after_suite\":{rss}}}");
    }
}
