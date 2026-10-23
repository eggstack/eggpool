//! Durable health effects, quarantine gating, and restart survival.
//!
//! Every test in this target is a regression guard for a behavior the
//! pre-milestone construction could not produce. Before the milestone the
//! durable health repositories were never constructed in production, the
//! quarantine was never attached to the router, and the routing path's
//! failure effects never left a durable row.

use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    path::PathBuf,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use eggpool::{
    Config,
    accounts::{AccountRegistry, CredentialStore},
    catalog::{ModelCatalogCache, ModelInput, ProtocolResolutionStatus},
    db::{Account, Database, DatabaseConfig, MigrationRunner},
    health::{
        AccountBackoffRecord, AccountBackoffRepository, BackoffReason, EvidenceProvenance,
        HealthManager, ModelQuarantine, ModelQuarantineRepository, QuarantineEntry,
        QuarantineState, hydrate_generation_health,
    },
    quota::{AccountQuota, QuotaEstimator},
    routing::{DurableHealth, EligibilityPolicy, RoutingRequestFacts, RoutingRouter},
};

fn now_epoch() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("clock")
        .as_secs_f64()
}

fn temporary_database_path(label: &str) -> PathBuf {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("clock")
        .as_nanos();
    std::env::temp_dir().join(format!(
        "eggpool-m005-{label}-{}-{nanos}.sqlite3",
        std::process::id()
    ))
}

async fn database(label: &str) -> (Database, PathBuf) {
    let path = temporary_database_path(label);
    let database = Database::open(DatabaseConfig {
        path: path.to_string_lossy().into_owned(),
        ..DatabaseConfig::default()
    })
    .await
    .expect("database opens");
    MigrationRunner::new(&database)
        .run()
        .await
        .expect("schema migrates");
    (database, path)
}

fn account(id: i64, name: &str) -> Account {
    Account {
        id,
        name: name.into(),
        api_key_env: format!("{name}_KEY"),
        enabled: true,
        weight: 1.0,
        provider_id: "provider-a".into(),
    }
}

fn config() -> Config {
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
    for name in ["account-a", "account-b"] {
        provider.accounts.push(eggpool::config::AccountConfig {
            name: name.into(),
            ..Default::default()
        });
    }
    config.providers.insert("provider-a".into(), provider);
    config.validate().expect("fixture config validates");
    config
}

fn registry() -> AccountRegistry {
    AccountRegistry::from_config(
        &config(),
        &[account(1, "account-a"), account(2, "account-b")],
        &CredentialStore::default(),
    )
    .expect("registry builds")
}

/// A catalog that exposes `shared-model` and `other-model` on both accounts.
fn catalog() -> ModelCatalogCache {
    let mut catalog = ModelCatalogCache::default();
    for name in ["account-a", "account-b"] {
        catalog.set_account_provider(name, "provider-a");
    }
    let models: Vec<ModelInput> = ["shared-model", "other-model"]
        .into_iter()
        .map(|model_id| {
            let mut model = ModelInput::new(model_id);
            model.protocol = Some("openai".into());
            model.protocol_source = Some("fixture".into());
            model.resolution_status = ProtocolResolutionStatus::Resolved;
            model
        })
        .collect();
    for name in ["account-a", "account-b"] {
        catalog
            .update_from_account(name, "provider-a", &models, true, true)
            .expect("catalog model");
    }
    catalog
}

/// A catalog that has no provider model for `provider-a`.
///
/// The eligibility gate resolves the upstream model by provider, so a model
/// registered only under another provider leaves the candidate's upstream
/// protocol unclassifiable and partitions it as `unknown`.
fn unclassified_catalog() -> ModelCatalogCache {
    let mut catalog = ModelCatalogCache::default();
    for name in ["account-a", "account-b"] {
        catalog.set_account_provider(name, "provider-a");
    }
    let mut model = ModelInput::new("shared-model");
    model.protocol = Some("openai".into());
    model.protocol_source = Some("fixture".into());
    model.resolution_status = ProtocolResolutionStatus::Resolved;
    catalog
        .update_from_account("account-b", "provider-b", &[model], true, true)
        .expect("catalog model");
    catalog
}

/// The durable health tables reference the account rows.
async fn seed_accounts(database: &Database) {
    database
        .call(|connection| {
            connection.execute_batch(
                "INSERT INTO accounts (id, name, api_key_env, enabled, weight, provider_id) \
                 VALUES (1, 'account-a', 'account-a_KEY', 1, 1.0, 'provider-a'), \
                        (2, 'account-b', 'account-b_KEY', 1, 1.0, 'provider-a')",
            )
        })
        .await
        .expect("account rows persist");
}

fn facts(model: &str) -> RoutingRequestFacts {
    let mut facts = RoutingRequestFacts::new(model);
    facts.requested_protocol = Some("openai".into());
    facts.projected_tokens = 128;
    facts.now = now_epoch() as i64;
    facts
}

fn account_names() -> BTreeMap<i64, String> {
    BTreeMap::from([(1, "account-a".to_owned()), (2, "account-b".to_owned())])
}

fn estimator() -> QuotaEstimator {
    QuotaEstimator::new([
        AccountQuota::new("account-a"),
        AccountQuota::new("account-b"),
    ])
}

fn health_for(registry: &AccountRegistry) -> HealthManager {
    let health = HealthManager::new();
    for identity in registry.all() {
        health.register_account(identity.account_id, &identity.account_name);
    }
    health
}

fn exclusion_reason(plan: &eggpool::routing::RoutingPlan, account_name: &str) -> Option<String> {
    plan.exclusions
        .iter()
        .find(|exclusion| exclusion.account_name == account_name)
        .map(|exclusion| exclusion.reason_code.clone())
}

/// One generation built the way the production build path builds one.
struct Generation {
    database: Database,
    router: RoutingRouter,
    health: HealthManager,
    quarantine: ModelQuarantine,
}

async fn build_generation(database: &Database) -> Generation {
    build_generation_with_catalog(database, catalog()).await
}

async fn build_generation_with_catalog(
    database: &Database,
    catalog: ModelCatalogCache,
) -> Generation {
    let registry = registry();
    let health = health_for(&registry);
    let quarantine = ModelQuarantine::default();
    let backoffs = AccountBackoffRepository::new(database);
    let quarantine_repository = ModelQuarantineRepository::new(database);
    hydrate_generation_health(
        &backoffs,
        &quarantine_repository,
        &health,
        &quarantine,
        &account_names(),
        now_epoch(),
    )
    .await
    .expect("durable health hydrates");
    let router = RoutingRouter::new(
        registry,
        catalog,
        estimator(),
        Some(health.clone()),
        EligibilityPolicy::default(),
    )
    .with_quarantine(quarantine.clone())
    .with_durable_health(DurableHealth::new(backoffs, quarantine_repository));
    Generation {
        database: database.clone(),
        router,
        health,
        quarantine,
    }
}

async fn seed_backoff(
    database: &Database,
    account_id: i64,
    model_id: Option<&str>,
    reason: BackoffReason,
    backoff_until_epoch: f64,
) {
    AccountBackoffRepository::new(database)
        .upsert(&AccountBackoffRecord {
            id: 0,
            account_id,
            model_id: model_id.map(str::to_owned),
            reason,
            status_code: Some(429),
            error_class: Some("rate_limited".to_owned()),
            consecutive_failures: 1,
            backoff_until_epoch: Some(backoff_until_epoch),
            last_failure_epoch: now_epoch(),
            updated_epoch: now_epoch(),
        })
        .await
        .expect("backoff row persists");
}

async fn seed_quarantine(
    database: &Database,
    account_name: &str,
    canonical_model_id: &str,
    upstream_protocol: &str,
    expiry_epoch: f64,
) {
    let quarantine = ModelQuarantine::default();
    let key = quarantine.key(
        "provider-a",
        account_name,
        canonical_model_id,
        None,
        upstream_protocol,
    );
    let entry = QuarantineEntry {
        key,
        state: QuarantineState::Suspected,
        evidence_provenance: EvidenceProvenance::RuntimeHttp,
        reason: "model_unavailable".to_owned(),
        first_observed: now_epoch(),
        last_observed: now_epoch(),
        observation_count: 1,
        expiry: Some(expiry_epoch),
        cleared_at: None,
        clear_reason: None,
        last_status_code: Some(404),
        last_error_class: Some("model_not_found".to_owned()),
    };
    ModelQuarantineRepository::new(database)
        .upsert_entry(&entry)
        .await
        .expect("quarantine row persists");
}

#[tokio::test(flavor = "current_thread")]
async fn a_durable_backoff_gates_the_account_after_hydration() {
    let (database, path) = database("backoff").await;
    seed_accounts(&database).await;
    seed_backoff(
        &database,
        1,
        None,
        BackoffReason::RateLimited,
        now_epoch() + 300.0,
    )
    .await;

    let generation = build_generation(&database).await;
    let plan = generation.router.build_routing_plan(&facts("shared-model"));
    assert!(
        !plan
            .candidates
            .iter()
            .any(|candidate| candidate.account_name == "account-a"),
        "a hydrated backoff must gate the account"
    );
    assert_eq!(
        exclusion_reason(&plan, "account-a").as_deref(),
        Some("rate_limited"),
        "the hydrated backoff must be visible to the health gate"
    );
    assert!(
        plan.candidates
            .iter()
            .any(|candidate| candidate.account_name == "account-b"),
        "the backoff of one account must not gate another"
    );
    let snapshot = generation
        .health
        .snapshot("account-a")
        .expect("the hydrated account is known to the health manager");
    assert_eq!(
        snapshot.health_state, "rate_limited",
        "the hydrated backoff must be visible in the generation's health state"
    );
    generation.database.close().await.expect("database closes");
    let _ = fs::remove_file(&path);
}

#[tokio::test(flavor = "current_thread")]
async fn a_model_scoped_durable_row_carries_the_live_reason() {
    let (database, path) = database("live-reason").await;
    seed_accounts(&database).await;
    let generation = build_generation(&database).await;

    let claim = generation
        .router
        .select_and_claim(&facts("shared-model"), &BTreeSet::new())
        .await
        .expect("claim operation")
        .expect("a claim is published");

    // A request timeout is classified as a connect timeout, not as a model
    // absence, and the model is quarantined all the same. The durable row
    // must record the reason the live path classified, so two unrelated
    // failure classes never collapse onto one persisted reason.
    generation
        .router
        .apply_failure_effects(
            &claim,
            true,
            true,
            "quarantine",
            Some("connect_timeout"),
            Some(Duration::from_secs(30)),
            true,
        )
        .await;

    let backoffs = AccountBackoffRepository::new(&database)
        .list_all(64)
        .await
        .expect("backoff rows are readable");
    let model_row = backoffs
        .iter()
        .find(|row| row.model_id.as_deref() == Some("shared-model"))
        .expect("a model-scoped row is persisted");
    assert_eq!(
        model_row.reason,
        BackoffReason::ConnectTimeout,
        "the model-scoped row must carry the live reason: {backoffs:?}"
    );
    assert!(
        !backoffs
            .iter()
            .any(|row| row.reason == BackoffReason::ModelUnavailable),
        "a connect timeout must not be persisted as a model absence: {backoffs:?}"
    );

    let quarantines = ModelQuarantineRepository::new(&database)
        .list_all(64)
        .await
        .expect("quarantine rows are readable");
    assert!(
        quarantines.iter().any(|row| row.account_id == "account-a"
            && row.canonical_model_id == "shared-model"
            && row.reason == "connect_timeout"),
        "the quarantine observation must carry the live reason: {quarantines:?}"
    );

    generation.database.close().await.expect("database closes");
    let _ = fs::remove_file(&path);
}

#[tokio::test(flavor = "current_thread")]
async fn the_durable_rungs_match_the_scope_of_their_row() {
    let (database, path) = database("scope-rung").await;
    seed_accounts(&database).await;
    let generation = build_generation(&database).await;

    let claim = generation
        .router
        .select_and_claim(&facts("shared-model"), &BTreeSet::new())
        .await
        .expect("claim operation")
        .expect("a claim is published");

    // Two account-scoped failures. The account ladder escalates; the model's
    // own counter never advances for an account-scoped reason, so the
    // model-scoped row must not carry the account's rung.
    for _ in 0..2 {
        generation
            .router
            .apply_failure_effects(
                &claim,
                true,
                true,
                "quarantine",
                Some("connect_timeout"),
                Some(Duration::from_secs(30)),
                true,
            )
            .await;
    }

    let backoffs = AccountBackoffRepository::new(&database)
        .list_all(64)
        .await
        .expect("backoff rows are readable");
    let model_row = backoffs
        .iter()
        .find(|row| row.model_id.as_deref() == Some("shared-model"))
        .expect("a model-scoped row is persisted");
    let account_row = backoffs
        .iter()
        .find(|row| row.model_id.is_none())
        .expect("an account-scoped row is persisted");
    assert_eq!(
        model_row.consecutive_failures, 1,
        "the model-scoped row must carry the model's own rung: {backoffs:?}"
    );
    assert_eq!(
        account_row.consecutive_failures, 2,
        "the account-scoped row must carry the account's rung: {backoffs:?}"
    );

    generation.database.close().await.expect("database closes");
    let _ = fs::remove_file(&path);
}

#[tokio::test(flavor = "current_thread")]
async fn a_model_scoped_row_restores_the_model_after_hydration() {
    let (database, path) = database("model-hydration").await;
    seed_accounts(&database).await;
    // A model-scoped row whose reason is not a model absence. The scope of
    // the row decides which effect is restored, so the model must still be
    // disabled after hydration.
    seed_backoff(
        &database,
        1,
        Some("shared-model"),
        BackoffReason::ConnectTimeout,
        now_epoch() + 300.0,
    )
    .await;

    let generation = build_generation(&database).await;
    let plan = generation.router.build_routing_plan(&facts("shared-model"));
    assert!(
        !plan
            .candidates
            .iter()
            .any(|candidate| candidate.account_name == "account-a"),
        "a hydrated model-scoped row must disable the model"
    );
    assert!(
        plan.candidates
            .iter()
            .any(|candidate| candidate.account_name == "account-b"),
        "the model disable of one account must not gate another"
    );
    let snapshot = generation
        .health
        .snapshot("account-a")
        .expect("the hydrated account is known to the health manager");
    assert!(
        snapshot.disabled_models.contains_key("shared-model"),
        "a model-scoped row must disable the model itself: {snapshot:?}"
    );
    assert!(
        snapshot.cooldown_until <= 0.0 && snapshot.disabled_until.is_none(),
        "a model-scoped row must not cool the whole account down: {snapshot:?}"
    );
    generation.database.close().await.expect("database closes");
    let _ = fs::remove_file(&path);
}

#[tokio::test(flavor = "current_thread")]
async fn an_exactly_quarantined_model_is_excluded_by_the_gate() {
    let (database, path) = database("quarantine").await;
    seed_accounts(&database).await;
    seed_quarantine(
        &database,
        "account-a",
        "shared-model",
        "openai",
        now_epoch() + 300.0,
    )
    .await;

    let generation = build_generation(&database).await;
    let plan = generation.router.build_routing_plan(&facts("shared-model"));
    assert!(
        !plan
            .candidates
            .iter()
            .any(|candidate| candidate.account_name == "account-a"),
        "a quarantined model must not be a candidate"
    );
    assert_eq!(
        exclusion_reason(&plan, "account-a").as_deref(),
        Some("model_quarantined"),
        "the quarantine gate must report its own exclusion reason code"
    );
    assert!(
        plan.candidates
            .iter()
            .any(|candidate| candidate.account_name == "account-b"),
        "the quarantine of one account must not gate another"
    );
    generation.database.close().await.expect("database closes");
    let _ = fs::remove_file(&path);
}

#[tokio::test(flavor = "current_thread")]
async fn a_quarantine_does_not_gate_another_model_on_the_same_account() {
    let (database, path) = database("other-model").await;
    seed_accounts(&database).await;
    seed_quarantine(
        &database,
        "account-a",
        "shared-model",
        "openai",
        now_epoch() + 300.0,
    )
    .await;

    let generation = build_generation(&database).await;
    let plan = generation.router.build_routing_plan(&facts("other-model"));
    assert!(
        plan.candidates
            .iter()
            .any(|candidate| candidate.account_name == "account-a"),
        "a quarantine is scoped to one model and must not gate another"
    );
    generation.database.close().await.expect("database closes");
    let _ = fs::remove_file(&path);
}

#[tokio::test(flavor = "current_thread")]
async fn an_unclassifiable_protocol_partition_does_not_bypass_the_quarantine() {
    let (database, path) = database("protocol").await;
    seed_accounts(&database).await;
    // The persisted row is in the `unknown` partition. `account-a` has no
    // catalog entry for the model, so the eligibility gate cannot classify
    // its upstream protocol and partitions it as `unknown` too. Collapsing
    // that partition into a concrete protocol would let the candidate
    // bypass the persisted quarantine.
    seed_quarantine(
        &database,
        "account-a",
        "shared-model",
        "unknown",
        now_epoch() + 300.0,
    )
    .await;

    let generation = build_generation_with_catalog(&database, unclassified_catalog()).await;
    let mut unclassified = facts("shared-model");
    unclassified.requested_protocol = None;
    let plan = generation.router.build_routing_plan(&unclassified);
    assert!(
        !plan
            .candidates
            .iter()
            .any(|candidate| candidate.account_name == "account-a"),
        "an exact persisted quarantine must not be bypassed"
    );
    assert_eq!(
        exclusion_reason(&plan, "account-a").as_deref(),
        Some("model_quarantined"),
        "the persisted quarantine must still exclude the candidate"
    );
    generation.database.close().await.expect("database closes");
    let _ = fs::remove_file(&path);
}

#[tokio::test(flavor = "current_thread")]
async fn a_stale_row_naming_an_unknown_account_does_not_fail_hydration() {
    let (database, path) = database("stale").await;
    seed_accounts(&database).await;
    // The account row exists durably but the account is not in this
    // generation's registry, so the row cannot be resolved to a name.
    database
        .call(|connection| {
            connection.execute_batch(
                "INSERT INTO accounts (id, name, api_key_env, enabled, weight, provider_id) \
                 VALUES (999, 'retired-a', 'retired-a_KEY', 0, 1.0, 'provider-a')",
            )
        })
        .await
        .expect("the durable account row persists");
    seed_backoff(
        &database,
        999,
        None,
        BackoffReason::RateLimited,
        now_epoch() + 300.0,
    )
    .await;

    let registry = registry();
    let health = health_for(&registry);
    let quarantine = ModelQuarantine::default();
    let backoffs = AccountBackoffRepository::new(&database);
    let quarantine_repository = ModelQuarantineRepository::new(&database);
    let report = hydrate_generation_health(
        &backoffs,
        &quarantine_repository,
        &health,
        &quarantine,
        &account_names(),
        now_epoch(),
    )
    .await
    .expect("a stale row must not fail the hydration");
    assert_eq!(report.backoffs, 0, "the stale row is skipped, not applied");
    build_generation(&database).await;
    database.close().await.expect("database closes");
    let _ = fs::remove_file(&path);
}

#[tokio::test(flavor = "current_thread")]
async fn a_failure_effect_through_the_routing_path_leaves_a_durable_row() {
    let (database, path) = database("effect").await;
    seed_accounts(&database).await;
    let generation = build_generation(&database).await;

    let claim = generation
        .router
        .select_and_claim(&facts("shared-model"), &BTreeSet::new())
        .await
        .expect("claim operation")
        .expect("a claim is published");
    let account_name = claim.account_name().to_owned();

    generation
        .router
        .apply_failure_effects(
            &claim,
            true,
            true,
            "quarantine",
            Some("rate_limited"),
            Some(Duration::from_secs(30)),
            true,
        )
        .await;

    let backoffs = AccountBackoffRepository::new(&database)
        .list_all(64)
        .await
        .expect("backoff rows are readable");
    assert!(
        backoffs
            .iter()
            .any(|row| row.account_id == 1 && row.reason == BackoffReason::RateLimited),
        "the account backoff must be persisted: {backoffs:?}"
    );
    let quarantines = ModelQuarantineRepository::new(&database)
        .list_all(64)
        .await
        .expect("quarantine rows are readable");
    assert!(
        quarantines.iter().any(|row| {
            row.account_id == account_name
                && row.canonical_model_id == "shared-model"
                && row.state != "healthy"
        }),
        "the model quarantine must be persisted: {quarantines:?}"
    );
    generation.database.close().await.expect("database closes");
    let _ = fs::remove_file(&path);
}

#[tokio::test(flavor = "current_thread")]
async fn the_persisted_effects_survive_a_restart() {
    let (database, path) = database("restart").await;
    seed_accounts(&database).await;
    let first_generation = build_generation(&database).await;
    let claim = first_generation
        .router
        .select_and_claim(&facts("shared-model"), &BTreeSet::new())
        .await
        .expect("claim operation")
        .expect("a claim is published");
    first_generation
        .router
        .apply_failure_effects(
            &claim,
            true,
            true,
            "quarantine",
            Some("rate_limited"),
            Some(Duration::from_secs(300)),
            true,
        )
        .await;
    drop(claim);
    first_generation
        .database
        .close()
        .await
        .expect("database closes");

    // A restart is a new process over the same durable file.
    let restarted = Database::open(DatabaseConfig {
        path: path.to_string_lossy().into_owned(),
        ..DatabaseConfig::default()
    })
    .await
    .expect("database reopens");
    let durable_rows = ModelQuarantineRepository::new(&restarted)
        .list_all(64)
        .await
        .expect("quarantine rows are readable");
    assert!(
        !durable_rows.is_empty(),
        "the quarantine row must be durable across the reopen: {durable_rows:?}"
    );
    let restarted_generation = build_generation(&restarted).await;
    let plan = restarted_generation
        .router
        .build_routing_plan(&facts("shared-model"));
    assert!(
        !plan
            .candidates
            .iter()
            .any(|candidate| candidate.account_name == "account-a"),
        "the persisted effect must still gate the account after a restart"
    );
    let entries = restarted_generation
        .quarantine
        .list_entries(now_epoch(), true);
    assert!(
        entries.iter().any(|entry| {
            entry.key.account_id == "account-a" && entry.key.canonical_model_id == "shared-model"
        }),
        "the persisted quarantine must survive a restart: {entries:?}"
    );
    restarted_generation
        .database
        .close()
        .await
        .expect("database closes");
    let _ = fs::remove_file(&path);
}

#[tokio::test(flavor = "current_thread")]
async fn an_expired_row_stops_gating_once_the_expiry_runs() {
    let (database, path) = database("expiry").await;
    seed_accounts(&database).await;
    seed_backoff(
        &database,
        1,
        None,
        BackoffReason::RateLimited,
        now_epoch() - 60.0,
    )
    .await;
    seed_quarantine(
        &database,
        "account-b",
        "shared-model",
        "openai",
        now_epoch() - 60.0,
    )
    .await;

    let expired = AccountBackoffRepository::new(&database)
        .expire_old(now_epoch())
        .await
        .expect("backoff expiry runs");
    let expired_quarantine = ModelQuarantineRepository::new(&database)
        .expire_old(now_epoch())
        .await
        .expect("quarantine expiry runs");
    assert_eq!(expired, 1, "the expired backoff row is retired");
    assert_eq!(
        expired_quarantine, 1,
        "the expired quarantine row is retired"
    );

    let generation = build_generation(&database).await;
    let plan = generation.router.build_routing_plan(&facts("shared-model"));
    assert!(
        plan.candidates
            .iter()
            .any(|candidate| candidate.account_name == "account-a"),
        "an expired backoff must not gate the account"
    );
    assert!(
        plan.candidates
            .iter()
            .any(|candidate| candidate.account_name == "account-b"),
        "an expired quarantine must not gate the candidate"
    );
    generation.database.close().await.expect("database closes");
    let _ = fs::remove_file(&path);
}

/// The durable write must not happen inside the selection critical section.
///
/// This is a source-level contract, the same shape as the existing wire
/// extraction and kernel-boundary guards: the selection path must not touch
/// the durable repositories at all, and the failure path must.
#[test]
fn the_selection_path_performs_no_durable_health_write() {
    let source = include_str!("../src/routing/router.rs");
    let selection = source
        .split("async fn select_and_claim_with_preference(")
        .nth(1)
        .expect("the selection entry point exists");
    let selection = selection
        .split("pub fn trace_for(")
        .next()
        .expect("the selection body is bounded");
    for forbidden in [
        "AccountBackoffRepository",
        "ModelQuarantineRepository",
        "upsert_entry",
        "with_transaction",
    ] {
        assert!(
            !selection.contains(forbidden),
            "the selection critical section must not reference {forbidden}"
        );
    }
    let failure = source
        .split("pub async fn apply_failure_effects(")
        .nth(1)
        .expect("the failure entry point exists");
    assert!(
        failure.contains("upsert") || failure.contains("persist_account_backoff"),
        "the failure path must own the durable write"
    );
}
