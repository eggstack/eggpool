use std::collections::BTreeSet;

use eggpool::{
    Config,
    accounts::{AccountRegistry, CredentialStore},
    catalog::{ModelCatalogCache, ModelInput, ProtocolResolutionStatus},
    db::Account,
    health::{EvidenceProvenance, ModelQuarantine},
    quota::{AccountQuota, QuotaEstimator},
    routing::{
        EligibilityPolicy, FairnessMode, FairnessScope, LocalQuotaMode, RoutingRequestFacts,
        RoutingRouter,
    },
};

fn account(id: i64, name: &str) -> Account {
    Account {
        id,
        name: name.into(),
        api_key_env: String::new(),
        enabled: true,
        weight: 1.0,
        provider_id: "provider-a".into(),
    }
}

fn fixture() -> (RoutingRouter, QuotaEstimator) {
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
    provider.accounts.push(eggpool::config::AccountConfig {
        name: "account-b".into(),
        ..Default::default()
    });
    config.providers.insert("provider-a".into(), provider);
    config.validate().expect("fixture config validates");
    let registry = AccountRegistry::from_config(
        &config,
        &[account(1, "account-a"), account(2, "account-b")],
        &CredentialStore::default(),
    )
    .expect("registry builds");
    let mut catalog = ModelCatalogCache::default();
    catalog.set_account_provider("account-a", "provider-a");
    catalog.set_account_provider("account-b", "provider-a");
    let mut model = ModelInput::new("model-a");
    model.protocol = Some("openai".into());
    model.protocol_source = Some("fixture".into());
    model.resolution_status = ProtocolResolutionStatus::Resolved;
    catalog
        .update_from_account("account-a", "provider-a", &[model.clone()], true, true)
        .expect("catalog account a");
    catalog
        .update_from_account("account-b", "provider-a", &[model], true, true)
        .expect("catalog account b");
    let estimator = QuotaEstimator::new([
        AccountQuota::new("account-a"),
        AccountQuota::new("account-b"),
    ]);
    let router = RoutingRouter::new(
        registry,
        catalog,
        estimator.clone(),
        None,
        EligibilityPolicy {
            fairness_mode: FairnessMode::RoundRobin,
            fairness_scope: FairnessScope::ProviderModelProtocol,
            local_quota_mode: LocalQuotaMode::ScoreOnly,
            ..Default::default()
        },
    );
    (router, estimator)
}

fn facts() -> RoutingRequestFacts {
    let mut facts = RoutingRequestFacts::new("model-a");
    facts.requested_protocol = Some("openai".into());
    facts.projected_tokens = 100;
    facts
}

#[tokio::test]
async fn claim_publishes_pending_load_and_rollback_is_idempotent() {
    let (router, estimator) = fixture();
    let empty = BTreeSet::new();
    let claim = router
        .select_and_claim(&facts(), &empty)
        .await
        .expect("claim succeeds")
        .expect("candidate exists");
    assert_eq!(claim.account_name(), "account-a");
    assert_eq!(router.active_request_count("account-a"), 1);
    let snapshot = estimator.snapshot(&["account-a".into()]);
    assert_eq!(snapshot["account-a"].pending_requests, 1);
    assert_eq!(snapshot["account-a"].pending_tokens, 100);
    assert_eq!(
        claim.rollback_claim().expect("rollback"),
        eggpool::routing::ClaimTransition::RolledBack
    );
    assert_eq!(router.active_request_count("account-a"), 0);
    assert_eq!(
        claim.rollback_claim().expect("duplicate rollback"),
        eggpool::routing::ClaimTransition::AlreadyTransitioned
    );
    let snapshot = estimator.snapshot(&["account-a".into()]);
    assert_eq!(snapshot["account-a"].pending_requests, 0);
}

#[tokio::test]
async fn concurrent_claims_observe_the_first_pending_claim() {
    let (router, estimator) = fixture();
    let empty = BTreeSet::new();
    let first = router
        .select_and_claim(&facts(), &empty)
        .await
        .expect("first")
        .expect("first candidate");
    let second = router
        .select_and_claim(&facts(), &empty)
        .await
        .expect("second")
        .expect("second candidate");
    assert_ne!(first.account_name(), second.account_name());
    let snapshot = estimator.snapshot(&["account-a".into(), "account-b".into()]);
    assert_eq!(snapshot["account-a"].pending_requests, 1);
    assert_eq!(snapshot["account-b"].pending_requests, 1);
    first.rollback_claim().expect("first rollback");
    second.rollback_claim().expect("second rollback");
}

#[tokio::test]
async fn readiness_and_plan_do_not_advance_fairness_or_claim_state() {
    let (router, _) = fixture();
    let facts = facts();
    assert!(router.has_eligible_pairing(&facts));
    let plan = router.build_routing_plan(&facts);
    assert_eq!(plan.candidates.len(), 2);
    assert_eq!(router.fairness_key_count(), 0);
    assert_eq!(router.active_request_count("account-a"), 0);
}

#[test]
fn fairness_rotor_is_lru_bounded() {
    let rotor = eggpool::routing::FairnessRotor::new();
    for index in 0..(eggpool::routing::FAIRNESS_KEY_HARD_CAP + 100) {
        rotor.commit(
            &eggpool::routing::FairnessKey {
                provider_id: Some("provider".into()),
                model_id: format!("model-{index}"),
                protocol: Some("openai".into()),
                priority: 0,
                client_protocol: None,
            },
            2,
        );
    }
    assert_eq!(rotor.key_count(), eggpool::routing::FAIRNESS_KEY_HARD_CAP);
}

#[tokio::test]
async fn conversion_moves_pending_to_reserved_once() {
    let (router, estimator) = fixture();
    let claim = router
        .select_and_claim(&facts(), &BTreeSet::new())
        .await
        .expect("claim")
        .expect("candidate");
    assert_eq!(
        claim
            .convert_claim_after_durable_publication()
            .expect("convert"),
        eggpool::routing::ClaimTransition::Converted
    );
    assert_eq!(
        claim
            .convert_claim_after_durable_publication()
            .expect("duplicate conversion"),
        eggpool::routing::ClaimTransition::AlreadyTransitioned
    );
    let snapshot = estimator.snapshot(&[claim.account_name().into()]);
    assert_eq!(snapshot[claim.account_name()].pending_requests, 0);
    assert_eq!(snapshot[claim.account_name()].reserved_requests, 1);
    claim.release_active_claim().expect("release active");
    assert_eq!(router.active_request_count(claim.account_name()), 0);
}

#[test]
fn quarantined_model_is_excluded_at_the_exact_scope() {
    let (router, _) = fixture();
    let quarantine = ModelQuarantine::default();
    let key = quarantine.key(
        "provider-a",
        "account-a",
        "model-a",
        Some("model-a"),
        "openai",
    );
    quarantine.record_observation(
        key.clone(),
        EvidenceProvenance::RuntimeHttp,
        "test",
        Some(500),
        None,
        0.0,
    );
    quarantine.record_observation(
        key,
        EvidenceProvenance::RuntimeHttp,
        "test",
        Some(500),
        None,
        0.0,
    );
    let plan = router
        .with_quarantine(quarantine)
        .build_routing_plan(&facts());
    assert_eq!(plan.candidates.len(), 1);
    assert_eq!(plan.candidates[0].account_name, "account-b");
    assert!(
        plan.exclusions
            .iter()
            .any(|item| item.reason_code == "model_quarantined")
    );
}

#[test]
fn provider_qualified_facts_use_the_catalog_parser() {
    let providers = ["provider-a".into()].into_iter().collect();
    let facts = RoutingRequestFacts::from_model_id("model-a/provider-a", &providers);
    assert_eq!(facts.canonical_model_id, "model-a");
    assert_eq!(facts.provider_id.as_deref(), Some("provider-a"));
}
