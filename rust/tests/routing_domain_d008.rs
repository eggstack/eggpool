//! Integrated M5 qualification fixtures.
//!
//! These tests intentionally construct one generation from a schema-54
//! database copy and then feed the request-independent D001 DTOs through the
//! catalog, quota, health, routing, and model-router boundaries together.
//! They are closure evidence, not a second production implementation.

use std::{
    collections::{BTreeMap, BTreeSet},
    sync::{
        Arc, Mutex,
        atomic::{AtomicU64, Ordering},
    },
};

use eggpool::{
    Config,
    accounts::{AccountRegistry, CredentialStore},
    catalog::{ModelCatalogCache, ModelInput, ProtocolResolutionStatus},
    db::{AccountRepository, Database, DatabaseConfig, MigrationRunner, UsageWindowRepository},
    health::{
        AccountBackoffRepository, BackoffReason, EvidenceProvenance, HealthManager,
        ModelQuarantine, ModelQuarantineRepository, QuarantineState,
    },
    model_router::{
        AffinityDecisionSource, AffinitySelection, ModelRouterAffinity, ModelRouterRegistry,
        session_identity_from_header,
    },
    quota::{AccountQuota, PersistedWindowSnapshot, QuotaEstimator},
    routing::{EligibilityPolicy, LocalQuotaMode, RoutingRequestFacts, RoutingRouter},
};

struct Generation {
    database: Database,
    directory: tempfile::TempDir,
    registry: AccountRegistry,
    catalog: Arc<Mutex<ModelCatalogCache>>,
    estimator: QuotaEstimator,
    health: HealthManager,
    quarantine: ModelQuarantine,
    router: RoutingRouter,
    model_routers: ModelRouterRegistry,
    clock: Arc<AtomicU64>,
}

const SEED_EPOCH: i64 = 1_788_480_000;

impl Generation {
    async fn close(self) {
        self.database.close().await.expect("database closes");
        drop(self.directory);
    }
}

fn config() -> Config {
    let mut config = Config::default();
    config.providers.insert(
        "provider-a".into(),
        eggpool::config::ProviderConfig {
            id: "provider-a".into(),
            base_url: "https://provider-a.invalid/v1".into(),
            protocols: vec!["openai".into(), "anthropic".into()],
            routing_priority: 10,
            auth: eggpool::config::ProviderAuthConfig {
                mode: "none".into(),
                ..Default::default()
            },
            accounts: vec![
                eggpool::config::AccountConfig {
                    name: "account-a".into(),
                    weight: 2.0,
                    ..Default::default()
                },
                eggpool::config::AccountConfig {
                    name: "disabled-a".into(),
                    enabled: false,
                    ..Default::default()
                },
            ],
            ..Default::default()
        },
    );
    config.providers.insert(
        "provider-b".into(),
        eggpool::config::ProviderConfig {
            id: "provider-b".into(),
            base_url: "https://provider-b.invalid/v1".into(),
            protocols: vec!["openai".into(), "anthropic".into()],
            routing_priority: 5,
            auth: eggpool::config::ProviderAuthConfig {
                mode: "none".into(),
                ..Default::default()
            },
            accounts: vec![eggpool::config::AccountConfig {
                name: "account-b".into(),
                ..Default::default()
            }],
            ..Default::default()
        },
    );
    let mut router = eggpool::config::ModelRouterConfig {
        selector_model: "selector-model".into(),
        default_model: "shared-model".into(),
        affinity_ttl_s: 60.0,
        max_input_bytes: 256,
        ..Default::default()
    };
    router.routes.insert(
        "default".into(),
        eggpool::config::ModelRouteConfig {
            model: "shared-model".into(),
            description: "default route".into(),
        },
    );
    config.model_routers.insert("virtual-route".into(), router);
    config.validate().expect("integrated config validates");
    config
}

async fn generation() -> Generation {
    let directory = tempfile::tempdir().expect("temporary directory");
    let database = Database::open(DatabaseConfig {
        path: directory
            .path()
            .join("state.sqlite3")
            .to_string_lossy()
            .into_owned(),
        ..Default::default()
    })
    .await
    .expect("database opens");
    MigrationRunner::new(&database)
        .run()
        .await
        .expect("schema 54 migrates");
    database
        .call(|connection| {
            connection.execute_batch(include_str!(
                "../../migration-rs/fixtures/routing-domain/schema54-routing-domain-seed.sql"
            ))
        })
        .await
        .expect("schema 54 fixture applies");

    let config = config();
    let credentials = CredentialStore::default();
    let account_repository = AccountRepository::new(&database);
    let registry = AccountRegistry::hydrate_from_db(&config, &account_repository, &credentials)
        .await
        .expect("accounts hydrate");
    let mut hydrated_catalog = ModelCatalogCache::default();
    hydrated_catalog.set_config(&config);
    hydrated_catalog
        .hydrate_from_db(&database)
        .await
        .expect("catalog hydrates");
    let catalog = Arc::new(Mutex::new(hydrated_catalog));

    let estimator = QuotaEstimator::new(registry.enabled_snapshot().into_iter().map(|identity| {
        let mut quota = AccountQuota::new(identity.account_name.clone());
        quota.weight = identity.weight;
        quota
    }));
    let usage = UsageWindowRepository::new(&database)
        .get_all_usage_windows("2026-09-04 00:00:00")
        .await
        .expect("usage hydrates");
    let account_ids = registry
        .enabled_snapshot()
        .into_iter()
        .map(|identity| (identity.account_name, identity.account_id))
        .collect::<BTreeMap<_, _>>();
    estimator.hydrate_usage_windows(&usage, &account_ids, 0.0);
    // Keep the fixture's durable reservation shape visible in the local
    // mirror while leaving the pending claim path under test below.
    estimator.set_persisted_snapshot(
        "account-b",
        PersistedWindowSnapshot {
            account_id: 2,
            ..PersistedWindowSnapshot::empty(2, 0.0)
        },
    );

    let clock = Arc::new(AtomicU64::new(0));
    let health_clock = Arc::clone(&clock);
    let health = HealthManager::with_clock(move || health_clock.load(Ordering::SeqCst) as f64);
    for identity in registry.enabled_snapshot() {
        health.register_account(identity.account_id, &identity.account_name);
    }
    let wall_now = database
        .call(|connection| {
            connection.query_row(
                "SELECT CAST(strftime('%s', '2026-09-04 00:00:00') AS INTEGER)",
                [],
                |row| row.get::<_, i64>(0),
            )
        })
        .await
        .expect("fixture epoch");
    let backoffs = AccountBackoffRepository::new(&database)
        .list_all(32)
        .await
        .expect("backoffs hydrate");
    health
        .hydrate_backoffs(
            &backoffs,
            &BTreeMap::from([(1, "account-a".into()), (2, "account-b".into())]),
            wall_now as f64,
        )
        .expect("backoffs apply");
    let quarantine = ModelQuarantine::default();
    ModelQuarantineRepository::new(&database)
        .hydrate_into(&quarantine, wall_now as f64)
        .await
        .expect("quarantine hydrates");

    let router = RoutingRouter::with_shared_catalog(
        registry.clone(),
        Arc::clone(&catalog),
        estimator.clone(),
        Some(health.clone()),
        EligibilityPolicy::default(),
    )
    .with_quarantine(quarantine.clone());
    let model_routers =
        ModelRouterRegistry::from_config(&config.model_routers).expect("model routers compile");
    Generation {
        database,
        directory,
        registry,
        catalog,
        estimator,
        health,
        quarantine,
        router,
        model_routers,
        clock,
    }
}

fn routing_facts(model: &str, now: i64) -> RoutingRequestFacts {
    let mut facts = RoutingRequestFacts::new(model);
    facts.requested_protocol = Some("openai".into());
    facts.transcode_protocols = vec!["anthropic".into()];
    facts.projected_tokens = 128;
    facts.now = SEED_EPOCH + now;
    facts
}

#[tokio::test(flavor = "current_thread")]
async fn schema54_generation_hydrates_and_routes_as_one_m5_state() {
    let generation = generation().await;
    assert_eq!(
        generation
            .router
            .build_routing_plan(&routing_facts("shared-model", 0))
            .candidates
            .len(),
        0
    );
    assert_eq!(
        generation.estimator.snapshot(&["account-a".into()])["account-a"]
            .quota
            .persisted_snapshot
            .unwrap()
            .request_count_5h,
        1
    );

    generation.clock.store(121, Ordering::SeqCst);
    let after_quarantine_expiry = generation
        .router
        .build_routing_plan(&routing_facts("shared-model", 121));
    assert_eq!(
        after_quarantine_expiry
            .candidates
            .iter()
            .map(|item| item.account_name.as_str())
            .collect::<Vec<_>>(),
        vec!["account-b"],
        "exclusions: {:?}",
        after_quarantine_expiry.exclusions
    );

    generation.clock.store(601, Ordering::SeqCst);
    let restored = generation
        .router
        .build_routing_plan(&routing_facts("shared-model", 601));
    assert_eq!(restored.candidates[0].account_name, "account-a");
    assert!(
        restored
            .candidates
            .iter()
            .any(|item| item.account_name == "account-b")
    );
    assert!(
        restored
            .exclusions
            .iter()
            .any(|item| item.reason_code == "disabled")
    );

    let claim = generation
        .router
        .select_and_claim(&routing_facts("shared-model", 601), &BTreeSet::new())
        .await
        .expect("selection succeeds")
        .expect("selected account");
    assert_eq!(claim.account_name(), "account-a");
    assert_eq!(claim.projected_tokens(), 128);
    assert_eq!(generation.router.active_request_count("account-a"), 1);
    assert_eq!(
        generation.estimator.snapshot(&["account-a".into()])["account-a"].pending_requests,
        1
    );
    claim.rollback_claim().expect("claim rollback");
    assert_eq!(generation.router.active_request_count("account-a"), 0);

    let router = generation
        .model_routers
        .get("virtual-route")
        .expect("virtual router");
    let identity = session_identity_from_header(Some("d008-session")).expect("session identity");
    let affinity = ModelRouterAffinity::with_capacity(4);
    let first = affinity
        .resolve(&router, &identity, || async {
            let route = router.route_for_id("0").expect("route");
            Ok(AffinitySelection {
                virtual_model: router.virtual_model.clone(),
                route_id: route.route_id.clone(),
                route_label: route.label.clone(),
                concrete_model: route.model.clone(),
                source: AffinityDecisionSource::Selector,
            })
        })
        .await
        .expect("first affinity resolution");
    let second = affinity
        .resolve(&router, &identity, || async {
            panic!("cache hit must not call selector")
        })
        .await
        .expect("affinity hit");
    assert!(!first.cache_hit);
    assert!(second.cache_hit);
    let selected = routing_facts(&second.decision.concrete_model, 601);
    assert!(generation.router.has_eligible_pairing(&selected));
    assert_eq!(affinity.stats().entry_count, 1);
    generation.close().await;
}

#[tokio::test(flavor = "current_thread")]
async fn catalog_uncertainty_and_restart_state_are_non_destructive() {
    let generation = generation().await;
    let key =
        generation
            .quarantine
            .key("provider-b", "account-b", "shared-model", None, "anthropic");
    let before = generation
        .catalog
        .lock()
        .expect("catalog lock")
        .account_supports_model("account-a", "shared-model");
    assert!(before);
    let invalid = ModelInput::new("");
    assert!(
        generation
            .catalog
            .lock()
            .expect("catalog lock")
            .update_from_account("account-a", "provider-a", &[invalid], true, true)
            .is_err()
    );
    assert!(
        generation
            .catalog
            .lock()
            .expect("catalog lock")
            .account_supports_model("account-a", "shared-model")
    );
    generation
        .catalog
        .lock()
        .expect("catalog lock")
        .update_from_account("account-a", "provider-a", &[], false, false)
        .expect("partial preserves");
    assert!(
        generation
            .catalog
            .lock()
            .expect("catalog lock")
            .account_supports_model("account-a", "shared-model")
    );

    generation
        .catalog
        .lock()
        .expect("catalog lock")
        .update_from_account("account-a", "provider-a", &[], true, true)
        .expect("authoritative withdrawal");
    assert!(
        !generation
            .catalog
            .lock()
            .expect("catalog lock")
            .account_supports_model("account-a", "shared-model")
    );
    let mut reappeared = ModelInput::new("shared-model");
    reappeared.protocol = Some("openai".into());
    reappeared.protocol_source = Some("provider_catalog".into());
    reappeared.resolution_status = ProtocolResolutionStatus::Resolved;
    generation
        .catalog
        .lock()
        .expect("catalog lock")
        .update_from_account("account-a", "provider-a", &[reappeared], true, true)
        .expect("reappearance");
    assert!(
        generation
            .catalog
            .lock()
            .expect("catalog lock")
            .account_supports_model("account-a", "shared-model")
    );

    let hydrated = generation.health.snapshot("account-a").expect("health");
    assert_eq!(hydrated.health_state, "rate_limited");
    assert!(hydrated.cooldown_until >= 599.0 && hydrated.cooldown_until <= 601.0);
    generation.clock.store(600, Ordering::SeqCst);
    assert!(generation.health.is_account_healthy_read_only("account-a"));
    let expiry = generation
        .quarantine
        .get_entry(&key)
        .expect("hydrated quarantine")
        .expiry
        .expect("bounded quarantine expiry");
    assert!(
        generation
            .quarantine
            .is_model_quarantined(&key, expiry - 1.0)
    );
    assert!(!generation.quarantine.is_model_quarantined(&key, expiry));

    let terminal_key = generation.quarantine.key(
        "provider-a",
        "account-a",
        "terminal-model",
        Some("terminal-model"),
        "openai",
    );
    assert!(
        generation
            .quarantine
            .set_terminal_withdrawn(
                terminal_key.clone(),
                "catalog",
                EvidenceProvenance::ProviderCatalog,
                700.0,
            )
            .is_ok()
    );
    assert_eq!(
        generation
            .quarantine
            .get_entry(&terminal_key)
            .expect("terminal entry")
            .state,
        QuarantineState::TerminalWithdrawn
    );
    assert!(
        generation
            .quarantine
            .clear_authoritative_reappearance(&terminal_key, 701.0)
    );

    let records = ModelQuarantineRepository::new(&generation.database)
        .list_all(32)
        .await
        .expect("durable quarantine");
    assert_eq!(records.len(), 2);
    let invalid_backoff = eggpool::health::AccountBackoffRecord {
        id: 0,
        account_id: 1,
        model_id: None,
        reason: BackoffReason::RateLimited,
        status_code: Some(429),
        error_class: None,
        consecutive_failures: 1,
        backoff_until_epoch: Some(f64::NAN),
        last_failure_epoch: 0.0,
        updated_epoch: 0.0,
    };
    assert!(
        AccountBackoffRepository::new(&generation.database)
            .upsert(&invalid_backoff)
            .await
            .is_err()
    );
    generation.close().await;
}

#[tokio::test(flavor = "current_thread")]
async fn local_quota_mode_only_changes_scoring_gate_not_authoritative_health() {
    let generation = generation().await;
    generation.clock.store(601, Ordering::SeqCst);
    generation
        .estimator
        .configure_policy(
            "account-a",
            2.0,
            eggpool::quota::QuotaPolicy {
                capacity_5h_requests: Some(1),
                ..Default::default()
            },
        )
        .expect("quota policy");
    generation.estimator.set_persisted_snapshot(
        "account-a",
        PersistedWindowSnapshot {
            account_id: 1,
            request_count_5h: 1,
            ..PersistedWindowSnapshot::empty(1, 0.0)
        },
    );
    let score_only = RoutingRouter::with_shared_catalog(
        generation.registry.clone(),
        Arc::clone(&generation.catalog),
        generation.estimator.clone(),
        Some(generation.health.clone()),
        EligibilityPolicy::default(),
    )
    .with_quarantine(generation.quarantine.clone());
    assert!(
        score_only
            .build_routing_plan(&routing_facts("shared-model", 601))
            .candidates
            .iter()
            .any(|candidate| candidate.account_name == "account-a")
    );

    let hard_cap = RoutingRouter::with_shared_catalog(
        generation.registry.clone(),
        Arc::clone(&generation.catalog),
        generation.estimator.clone(),
        Some(generation.health.clone()),
        EligibilityPolicy {
            local_quota_mode: LocalQuotaMode::HardCap,
            ..Default::default()
        },
    )
    .with_quarantine(generation.quarantine.clone());
    let hard_plan = hard_cap.build_routing_plan(&routing_facts("shared-model", 601));
    assert!(
        !hard_plan
            .candidates
            .iter()
            .any(|candidate| candidate.account_name == "account-a")
    );
    assert!(
        hard_plan
            .exclusions
            .iter()
            .any(|exclusion| exclusion.account_name == "account-a"
                && exclusion.reason_code == "quota_exhausted")
    );

    generation
        .health
        .record_cooldown("account-a", BackoffReason::QuotaExhausted, 60.0);
    assert!(
        !score_only
            .build_routing_plan(&routing_facts("shared-model", 601))
            .candidates
            .iter()
            .any(|candidate| candidate.account_name == "account-a")
    );
    generation.close().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn claims_and_half_open_probe_are_serialized_under_contention() {
    let generation = generation().await;
    generation.clock.store(601, Ordering::SeqCst);
    let request_facts = routing_facts("shared-model", 601);
    let barrier = Arc::new(tokio::sync::Barrier::new(8));
    let mut tasks = Vec::new();
    for _ in 0..8 {
        let router = generation.router.clone();
        let barrier = Arc::clone(&barrier);
        let facts = request_facts.clone();
        tasks.push(tokio::spawn(async move {
            barrier.wait().await;
            router.select_and_claim(&facts, &BTreeSet::new()).await
        }));
    }
    let mut claims = Vec::new();
    for task in tasks {
        if let Some(claim) = task.await.expect("selector task").expect("claim operation") {
            claims.push(claim);
        }
    }
    let ids = claims
        .iter()
        .map(|claim| claim.id())
        .collect::<BTreeSet<_>>();
    assert_eq!(ids.len(), claims.len());
    assert_eq!(
        claims
            .iter()
            .map(|claim| claim.account_name())
            .filter(|name| *name == "account-a")
            .count(),
        generation.router.active_request_count("account-a") as usize
    );
    assert_eq!(
        claims
            .iter()
            .map(|claim| claim.account_name())
            .filter(|name| *name == "account-b")
            .count(),
        generation.router.active_request_count("account-b") as usize
    );
    for claim in claims {
        claim.rollback_claim().expect("rollback is exact");
    }

    // Restrict the request to one account and make its circuit half-open.
    let mut provider_facts = routing_facts("shared-model", 901);
    provider_facts.provider_id = Some("provider-a".into());
    for _ in 0..5 {
        generation
            .health
            .record_failure("account-a", BackoffReason::UpstreamServerError);
    }
    generation.clock.store(1201, Ordering::SeqCst);
    let barrier = Arc::new(tokio::sync::Barrier::new(8));
    let mut tasks = Vec::new();
    for _ in 0..8 {
        let router = generation.router.clone();
        let barrier = Arc::clone(&barrier);
        let facts = provider_facts.clone();
        tasks.push(tokio::spawn(async move {
            barrier.wait().await;
            router.select_and_claim(&facts, &BTreeSet::new()).await
        }));
    }
    let mut probe_claims = Vec::new();
    for task in tasks {
        if let Some(claim) = task.await.expect("probe task").expect("probe operation") {
            probe_claims.push(claim);
        }
    }
    assert_eq!(probe_claims.len(), 1);
    assert!(probe_claims[0].owns_probe());
    probe_claims[0].rollback_claim().expect("probe rollback");
    assert!(
        !generation
            .health
            .snapshot("account-a")
            .expect("account")
            .circuit
            .probe_in_flight
    );
    let retry = generation
        .router
        .select_and_claim(&provider_facts, &BTreeSet::new())
        .await
        .expect("retry")
        .expect("retry claim");
    assert!(retry.owns_probe());
    retry.rollback_claim().expect("retry rollback");
    generation.close().await;
}
