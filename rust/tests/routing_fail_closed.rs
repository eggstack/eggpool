//! Fail-closed corrections for the routing selection gates.
//!
//! Every test in this target is a regression guard for a correction that the
//! pre-correction code could not produce. The corrections are:
//!
//! - the `LocalQuotaMode::HardCap` gate excludes an account that has no
//!   quota record instead of admitting it unchecked;
//! - an absent `HealthManager` is not the same fact as a healthy account;
//! - the missing-account recovery interval rejects a non-finite or
//!   non-positive value, and its two readers apply the same floor;
//! - a non-finite quota observation timestamp is rejected before any counter
//!   moves.

use std::{
    collections::BTreeSet,
    sync::{
        Arc, Mutex,
        atomic::{AtomicU64, Ordering},
    },
};

use eggpool::{
    Config,
    accounts::{AccountRegistry, CredentialStore},
    catalog::{ModelCatalogCache, ModelInput, ProtocolResolutionStatus},
    db::Account,
    health::HealthManager,
    quota::{AccountQuota, QuotaEstimator},
    routing::{EligibilityPolicy, LocalQuotaMode, RoutingRequestFacts, RoutingRouter},
};

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
    for name in ["account-a", "account-b", "account-c"] {
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
        &[
            account(1, "account-a"),
            account(2, "account-b"),
            account(3, "account-c"),
        ],
        &CredentialStore::default(),
    )
    .expect("registry builds")
}

/// A catalog that exposes `model-a` through `account-a` only.
///
/// `account-b` and `account-c` are routable accounts that the catalog does
/// not know the model for, which is what the missing-account recovery path
/// looks for.
fn catalog() -> ModelCatalogCache {
    let mut catalog = ModelCatalogCache::default();
    for name in ["account-a", "account-b", "account-c"] {
        catalog.set_account_provider(name, "provider-a");
    }
    let mut model = ModelInput::new("model-a");
    model.protocol = Some("openai".into());
    model.protocol_source = Some("fixture".into());
    model.resolution_status = ProtocolResolutionStatus::Resolved;
    catalog
        .update_from_account("account-a", "provider-a", &[model], true, true)
        .expect("catalog account a");
    catalog
}

fn facts() -> RoutingRequestFacts {
    let mut facts = RoutingRequestFacts::new("model-a");
    facts.requested_protocol = Some("openai".into());
    facts.projected_tokens = 128;
    facts.now = 1_000;
    facts
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

#[test]
fn hard_cap_excludes_an_account_without_a_quota_record() {
    let registry = registry();
    let health = health_for(&registry);
    // The estimator knows nothing about `account-a`: the account is routable
    // and the catalog supports the model, but there is no quota record to
    // compare the request against.
    let estimator = QuotaEstimator::new([AccountQuota::new("account-b")]);
    let router = RoutingRouter::new(
        registry,
        catalog(),
        estimator,
        Some(health),
        EligibilityPolicy {
            local_quota_mode: LocalQuotaMode::HardCap,
            ..Default::default()
        },
    );

    let plan = router.build_routing_plan(&facts());
    assert!(
        !plan
            .candidates
            .iter()
            .any(|candidate| candidate.account_name == "account-a"),
        "a hard-cap account without a quota record must not be a candidate"
    );
    assert_eq!(
        exclusion_reason(&plan, "account-a").as_deref(),
        Some("quota_exhausted"),
        "the hard-cap gate must fail closed with the existing reason code"
    );
}

#[test]
fn score_only_never_applies_the_quota_gate() {
    let registry = registry();
    let health = health_for(&registry);
    let estimator = QuotaEstimator::new([AccountQuota::new("account-b")]);
    let router = RoutingRouter::new(
        registry,
        catalog(),
        estimator,
        Some(health),
        EligibilityPolicy {
            local_quota_mode: LocalQuotaMode::ScoreOnly,
            ..Default::default()
        },
    );

    let plan = router.build_routing_plan(&facts());
    assert_ne!(
        exclusion_reason(&plan, "account-a").as_deref(),
        Some("quota_exhausted"),
        "the score-only mode must not exclude on quota grounds"
    );
}

#[tokio::test(flavor = "current_thread")]
async fn absent_health_manager_is_not_evaluated_as_healthy() {
    let registry = registry();
    let estimator = QuotaEstimator::new([
        AccountQuota::new("account-a"),
        AccountQuota::new("account-b"),
        AccountQuota::new("account-c"),
    ]);
    let router = RoutingRouter::new(
        registry,
        catalog(),
        estimator,
        None,
        EligibilityPolicy::default(),
    );

    let claim = router
        .select_and_claim(&facts(), &BTreeSet::new())
        .await
        .expect("claim operation");
    assert!(
        claim.is_none(),
        "an account whose health cannot be evaluated is not healthy, so the \
         router must not claim it"
    );
}

#[tokio::test(flavor = "current_thread")]
async fn present_health_manager_claims_a_registered_healthy_account() {
    let registry = registry();
    let health = health_for(&registry);
    let estimator = QuotaEstimator::new([
        AccountQuota::new("account-a"),
        AccountQuota::new("account-b"),
        AccountQuota::new("account-c"),
    ]);
    let router = RoutingRouter::new(
        registry,
        catalog(),
        estimator,
        Some(health),
        EligibilityPolicy::default(),
    );

    let claim = router
        .select_and_claim(&facts(), &BTreeSet::new())
        .await
        .expect("claim operation")
        .expect("a registered healthy account is still selected");
    assert_eq!(claim.account_name(), "account-a");
    claim.release_all().expect("claim releases");
}

/// Build a router whose only recovery target is `account-c`.
///
/// The recovery callback records every account the router decides to
/// re-probe, so the test observes the recovery decision rather than a
/// timing side effect.
fn recovery_router(
    interval_s: f64,
    clock: Arc<AtomicU64>,
    recovered: Arc<Mutex<Vec<String>>>,
) -> RoutingRouter {
    let registry = registry();
    let health = health_for(&registry);
    let estimator = QuotaEstimator::new([
        AccountQuota::new("account-a"),
        AccountQuota::new("account-b"),
        AccountQuota::new("account-c"),
    ]);
    let clock_value = Arc::clone(&clock);
    let recovered_value = Arc::clone(&recovered);
    RoutingRouter::new(
        registry,
        catalog(),
        estimator,
        Some(health),
        EligibilityPolicy::default(),
    )
    .with_recovery_clock(Arc::new(move || {
        // The recovery clock is stored in milliseconds so a test can place a
        // decision strictly inside a one-second floor.
        clock_value.load(Ordering::SeqCst) as f64 / 1000.0
    }))
    .with_missing_account_recovery(
        interval_s,
        Arc::new(move |account: &str| {
            recovered_value
                .lock()
                .unwrap_or_else(|error| error.into_inner())
                .push(account.to_owned());
        }),
    )
}

/// Drive one selection at `at_millis` on the recovery clock.
async fn recovery_count(router: &RoutingRouter, clock: &Arc<AtomicU64>, at_millis: u64) -> usize {
    clock.store(at_millis, Ordering::SeqCst);
    let _ = router
        .select_and_claim(&facts(), &BTreeSet::new())
        .await
        .expect("claim operation");
    router
        .health_manager()
        .expect("health manager")
        .accounts()
        .len()
}

#[tokio::test(flavor = "current_thread")]
async fn non_finite_recovery_interval_is_rejected_and_keeps_the_previous_value() {
    // A NaN interval is rejected: the previously configured interval stays
    // in place, so the second attempt inside that window is still refused.
    let clock = Arc::new(AtomicU64::new(0));
    let recovered = Arc::new(Mutex::new(Vec::<String>::new()));
    let router = recovery_router(10.0, Arc::clone(&clock), Arc::clone(&recovered))
        .with_missing_account_recovery(
            f64::NAN,
            Arc::new({
                let recovered = Arc::clone(&recovered);
                move |account: &str| {
                    recovered
                        .lock()
                        .unwrap_or_else(|error| error.into_inner())
                        .push(account.to_owned());
                }
            }),
        );
    assert_eq!(
        recovery_count(&router, &clock, 0).await,
        3,
        "fixture accounts"
    );
    assert_eq!(
        recovered.lock().unwrap().len(),
        2,
        "the first recovery attempt is always allowed for every account the \
         catalog does not know the model for"
    );
    assert_eq!(recovery_count(&router, &clock, 5_000).await, 3);
    assert_eq!(
        recovered.lock().unwrap().len(),
        2,
        "a rejected interval must keep the previously configured value, so the \
         attempt inside that window stays refused"
    );

    // An infinite interval is rejected the same way.
    let clock = Arc::new(AtomicU64::new(0));
    let recovered = Arc::new(Mutex::new(Vec::<String>::new()));
    let router = recovery_router(10.0, Arc::clone(&clock), Arc::clone(&recovered))
        .with_missing_account_recovery(
            f64::INFINITY,
            Arc::new({
                let recovered = Arc::clone(&recovered);
                move |account: &str| {
                    recovered
                        .lock()
                        .unwrap_or_else(|error| error.into_inner())
                        .push(account.to_owned());
                }
            }),
        );
    assert_eq!(recovery_count(&router, &clock, 0).await, 3);
    assert_eq!(recovered.lock().unwrap().len(), 2);
    assert_eq!(recovery_count(&router, &clock, 11_000).await, 3);
    assert_eq!(
        recovered.lock().unwrap().len(),
        4,
        "an infinite interval must be rejected, so the previously configured \
         value still governs the next attempt"
    );
}

#[tokio::test(flavor = "current_thread")]
async fn recovery_readers_apply_the_same_floor() {
    // A configured interval below the floor is raised to the floor by both
    // readers. Before the correction the attempt-allowance reader used the
    // raw value while the attempt-retention reader used the floored one, so
    // the two disagreed about the same configured interval.
    let clock = Arc::new(AtomicU64::new(0));
    let recovered = Arc::new(Mutex::new(Vec::<String>::new()));
    let router = recovery_router(0.5, Arc::clone(&clock), Arc::clone(&recovered));
    assert_eq!(recovery_count(&router, &clock, 0).await, 3);
    assert_eq!(recovered.lock().unwrap().len(), 2);
    // Half a second is inside the one-second floor. The attempt-retention
    // reader keeps the attempt, so the attempt-allowance reader must refuse
    // it as well. Before the correction the allowance reader compared
    // against the raw 0.5 s value and let the attempt through.
    assert_eq!(recovery_count(&router, &clock, 500).await, 3);
    assert_eq!(
        recovered.lock().unwrap().len(),
        2,
        "both readers must apply the same floor, so the second attempt inside \
         the floored interval stays refused"
    );
    assert_eq!(recovery_count(&router, &clock, 2_000).await, 3);
    assert_eq!(
        recovered.lock().unwrap().len(),
        4,
        "the attempt is allowed again once the floored interval has elapsed"
    );
}
