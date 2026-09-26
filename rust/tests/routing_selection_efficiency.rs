//! Routing-selection M001 ownership cleanup: selection must stay
//! deterministic across account counts, quota modes, fairness modes/scopes,
//! provider pinning, and transcode preference after removing the transient
//! String-keyed scoring maps and candidate re-cloning.
//!
//! Numeric parity between the private ordered scoring path and the public
//! scorer is proven by in-crate seam tests in `quota/scorer.rs` (the private
//! path is intentionally not public API); this target covers the public
//! selection shape through `RoutingRouter` — twin-router determinism, exact
//! eligible sets, exact exclusion codes, malformed-score handling, and the
//! unchanged deterministic comparator. The router defaults to deterministic
//! fairness randomness, so every mode is exactly reproducible.

use std::collections::BTreeSet;

use eggpool::{
    Config,
    accounts::{AccountRegistry, CredentialStore},
    catalog::{ModelCatalogCache, ModelInput, ProtocolResolutionStatus},
    db::Account,
    quota::{AccountQuota, QuotaEstimator, RoutingScore, ScoringPolicy},
    routing::{
        EligibilityPolicy, FairnessMode, FairnessScope, LocalQuotaMode, RoutingCandidate,
        RoutingRequestFacts, RoutingRouter, SelectionSnapshot,
    },
};

fn account_name(index: usize) -> String {
    format!("account-{index:03}")
}

fn score_is_equal(left: f64, right: f64) -> bool {
    left == right || (left.is_nan() && right.is_nan())
}

fn assert_scores_equal(left: &[RoutingScore], right: &[RoutingScore]) {
    assert_eq!(left.len(), right.len(), "score vector lengths must match");
    for (left, right) in left.iter().zip(right) {
        assert_eq!(left.account_name, right.account_name);
        let name = left.account_name.as_str();
        assert!(
            score_is_equal(left.quota_score, right.quota_score),
            "{name}"
        );
        assert!(score_is_equal(left.weight, right.weight), "{name}");
        assert_eq!(left.is_eligible, right.is_eligible);
        assert!(
            score_is_equal(left.inflight_penalty, right.inflight_penalty),
            "{name}"
        );
        assert!(
            score_is_equal(left.health_penalty, right.health_penalty),
            "{name}"
        );
        assert!(
            score_is_equal(left.final_score(), right.final_score()),
            "{name}"
        );
        assert_eq!(
            left.reserved_microdollars, right.reserved_microdollars,
            "{name}"
        );
        assert_eq!(left.reserved_requests, right.reserved_requests, "{name}");
        assert_eq!(left.reserved_tokens, right.reserved_tokens, "{name}");
        assert_eq!(
            left.cost_5h_microdollars, right.cost_5h_microdollars,
            "{name}"
        );
        assert_eq!(
            left.cost_7d_microdollars, right.cost_7d_microdollars,
            "{name}"
        );
        assert_eq!(
            left.cost_30d_microdollars, right.cost_30d_microdollars,
            "{name}"
        );
        assert_eq!(left.request_count_5h, right.request_count_5h, "{name}");
        assert_eq!(left.request_count_7d, right.request_count_7d, "{name}");
        assert_eq!(left.request_count_30d, right.request_count_30d, "{name}");
        assert_eq!(left.token_count_5h, right.token_count_5h, "{name}");
        assert_eq!(left.token_count_7d, right.token_count_7d, "{name}");
        assert_eq!(left.token_count_30d, right.token_count_30d, "{name}");
        assert_eq!(
            left.capacity_5h_microdollars, right.capacity_5h_microdollars,
            "{name}"
        );
        assert_eq!(
            left.capacity_7d_microdollars, right.capacity_7d_microdollars,
            "{name}"
        );
        assert_eq!(
            left.capacity_30d_microdollars, right.capacity_30d_microdollars,
            "{name}"
        );
        assert_eq!(
            left.capacity_5h_requests, right.capacity_5h_requests,
            "{name}"
        );
        assert_eq!(
            left.capacity_7d_requests, right.capacity_7d_requests,
            "{name}"
        );
        assert_eq!(
            left.capacity_30d_requests, right.capacity_30d_requests,
            "{name}"
        );
        assert_eq!(left.capacity_5h_tokens, right.capacity_5h_tokens, "{name}");
        assert_eq!(left.capacity_7d_tokens, right.capacity_7d_tokens, "{name}");
        assert_eq!(
            left.capacity_30d_tokens, right.capacity_30d_tokens,
            "{name}"
        );
        assert_eq!(
            left.active_request_count, right.active_request_count,
            "{name}"
        );
        assert_eq!(left.tier, right.tier, "{name}");
        assert_eq!(left.requires_transcode, right.requires_transcode, "{name}");
    }
}

fn assert_candidates_equal(left: &[RoutingCandidate], right: &[RoutingCandidate]) {
    assert_eq!(left.len(), right.len(), "candidate counts must match");
    for (left, right) in left.iter().zip(right) {
        assert_eq!(left.account_name, right.account_name);
        assert_eq!(left.provider_id, right.provider_id);
        assert_eq!(left.canonical_model_id, right.canonical_model_id);
        assert_eq!(left.upstream_model_id, right.upstream_model_id);
        assert_eq!(left.protocol, right.protocol);
        assert_eq!(left.priority, right.priority);
        assert_eq!(left.requires_transcode, right.requires_transcode);
    }
    let left_scores = left
        .iter()
        .map(|candidate| candidate.score.clone())
        .collect::<Vec<_>>();
    let right_scores = right
        .iter()
        .map(|candidate| candidate.score.clone())
        .collect::<Vec<_>>();
    assert_scores_equal(&left_scores, &right_scores);
}

fn assert_snapshots_equal(left: &SelectionSnapshot, right: &SelectionSnapshot) {
    assert_eq!(left.requested_model_id, right.requested_model_id);
    assert_eq!(left.provider_id, right.provider_id);
    assert_eq!(left.protocol, right.protocol);
    assert_eq!(left.request_surface, right.request_surface);
    assert_candidates_equal(&left.candidates, &right.candidates);
    assert_eq!(left.exclusions, right.exclusions);
    assert_eq!(
        left.eligible_candidate_count,
        right.eligible_candidate_count
    );
    assert_eq!(left.top_account_name, right.top_account_name);
    match (&left.top_score, &right.top_score) {
        (Some(left), Some(right)) => {
            assert_scores_equal(std::slice::from_ref(left), std::slice::from_ref(right))
        }
        (None, None) => {}
        _ => panic!("top score presence must match"),
    }
    assert_eq!(left.selected_account_name, right.selected_account_name);
    assert_eq!(left.selected_account_id, right.selected_account_id);
    assert_eq!(left.selected_provider_id, right.selected_provider_id);
    assert_eq!(left.selected_model_id, right.selected_model_id);
    assert_eq!(
        left.selected_upstream_model_id,
        right.selected_upstream_model_id
    );
    assert_eq!(left.selected_protocol, right.selected_protocol);
    assert_eq!(left.selected_priority, right.selected_priority);
    assert_eq!(
        left.selected_requires_transcode,
        right.selected_requires_transcode
    );
    match (&left.selected_score, &right.selected_score) {
        (Some(left), Some(right)) => {
            assert_scores_equal(std::slice::from_ref(left), std::slice::from_ref(right))
        }
        (None, None) => {}
        _ => panic!("selected score presence must match"),
    }
    assert_eq!(left.fairness, right.fairness);
}

struct SharedHarness {
    registry: AccountRegistry,
    catalog: ModelCatalogCache,
}

fn shared_harness(account_count: usize) -> SharedHarness {
    let mut config = Config::default();
    let mut provider = eggpool::config::ProviderConfig {
        id: "provider-a".into(),
        base_url: "https://provider-a.invalid/v1".into(),
        protocols: vec!["openai".into()],
        auth: eggpool::config::ProviderAuthConfig {
            mode: "none".into(),
            ..Default::default()
        },
        ..Default::default()
    };
    let mut rows = Vec::new();
    for index in 0..account_count {
        // Every fifth account starting at index 4 is disabled, so the
        // smallest counts keep at least one eligible candidate.
        let enabled = index % 5 != 4;
        provider.accounts.push(eggpool::config::AccountConfig {
            name: account_name(index),
            enabled,
            weight: 1.0 + (index % 3) as f64,
            ..Default::default()
        });
        rows.push(Account {
            id: index as i64 + 1,
            name: account_name(index),
            api_key_env: format!("{}_KEY", account_name(index)),
            enabled,
            weight: 1.0 + (index % 3) as f64,
            provider_id: "provider-a".into(),
        });
    }
    config.providers.insert("provider-a".into(), provider);
    config.validate().expect("harness config validates");
    let registry = AccountRegistry::from_config(&config, &rows, &CredentialStore::default())
        .expect("registry builds");
    let mut catalog = ModelCatalogCache::default();
    for index in 0..account_count {
        catalog.set_account_provider(account_name(index), "provider-a");
    }
    // Only even accounts support the model, so odd enabled accounts are
    // excluded as no_model alongside the disabled ones.
    let mut model = ModelInput::new("model-a");
    model.protocol = Some("openai".into());
    model.protocol_source = Some("fixture".into());
    model.resolution_status = ProtocolResolutionStatus::Resolved;
    for index in (0..account_count).step_by(2) {
        catalog
            .update_from_account(
                &account_name(index),
                "provider-a",
                &[model.clone()],
                true,
                true,
            )
            .expect("catalog model");
    }
    SharedHarness { registry, catalog }
}

fn fresh_estimator(account_count: usize) -> QuotaEstimator {
    let estimator =
        QuotaEstimator::new((0..account_count).map(|index| AccountQuota::new(account_name(index))));
    for index in 0..account_count {
        // Active load on every third account plus pending reservations on
        // every fourth, so scoring sees non-trivial quota mirrors.
        if index % 4 == 0 {
            estimator
                .add_pending_claim(&account_name(index), 100, 500)
                .expect("pending claim");
        }
        if index % 6 == 0 {
            estimator
                .add_reservation(&account_name(index), 1, 50, 250)
                .expect("reservation");
        }
    }
    estimator
}

fn policy(
    quota_mode: LocalQuotaMode,
    fairness_mode: FairnessMode,
    fairness_scope: FairnessScope,
    prefer_native: bool,
) -> EligibilityPolicy {
    EligibilityPolicy {
        local_quota_mode: quota_mode,
        scorer: ScoringPolicy {
            prefer_native,
            ..ScoringPolicy::default()
        },
        fairness_mode,
        fairness_scope,
        ..EligibilityPolicy::default()
    }
}

fn facts(pinned_provider: bool, projected_tokens: i64) -> RoutingRequestFacts {
    let mut facts = RoutingRequestFacts::new("model-a");
    facts.requested_protocol = Some("openai".into());
    facts.client_protocol = Some("openai".into());
    facts.projected_tokens = projected_tokens;
    if pinned_provider {
        facts.provider_id = Some("provider-a".into());
    }
    facts
}

fn assert_eligibility_sorted(candidates: &[RoutingCandidate], prefer_native: bool) {
    for pair in candidates.windows(2) {
        let (left, right) = (&pair[0], &pair[1]);
        let ordering = right
            .priority
            .cmp(&left.priority)
            .then_with(|| {
                left.score
                    .final_score()
                    .total_cmp(&right.score.final_score())
            })
            .then_with(|| {
                if prefer_native {
                    left.requires_transcode.cmp(&right.requires_transcode)
                } else {
                    std::cmp::Ordering::Equal
                }
            })
            .then_with(|| left.account_name.cmp(&right.account_name));
        assert!(
            ordering != std::cmp::Ordering::Greater,
            "candidate order must follow the deterministic comparator"
        );
    }
}

#[tokio::test]
async fn selection_stays_deterministic_across_modes_and_pressure() {
    let fairness_modes = [
        FairnessMode::Off,
        FairnessMode::RoundRobin,
        FairnessMode::Random,
    ];
    let fairness_scopes = [
        FairnessScope::ProviderModelProtocol,
        FairnessScope::ProviderModel,
        FairnessScope::PriorityModelProtocol,
    ];
    for account_count in [1_usize, 4, 16, 128] {
        let shared = shared_harness(account_count);
        // Full fairness matrix at every count would be redundant: modes and
        // scopes vary the post-scoring rotor only, so cover all modes/scopes
        // at 4 and 16 accounts and the Off mode everywhere.
        let modes: Vec<(FairnessMode, Vec<FairnessScope>)> =
            if account_count == 4 || account_count == 16 {
                fairness_modes
                    .iter()
                    .map(|mode| (*mode, fairness_scopes.to_vec()))
                    .collect()
            } else {
                vec![(
                    FairnessMode::Off,
                    vec![FairnessScope::ProviderModelProtocol],
                )]
            };
        for (fairness_mode, scopes) in modes {
            for fairness_scope in scopes {
                for quota_mode in [LocalQuotaMode::ScoreOnly, LocalQuotaMode::HardCap] {
                    for pinned_provider in [false, true] {
                        for prefer_native in [true, false] {
                            let cell_policy =
                                policy(quota_mode, fairness_mode, fairness_scope, prefer_native);
                            let cell_facts = facts(pinned_provider, 250);
                            let mut snapshots = Vec::new();
                            for _ in 0..2 {
                                let router = RoutingRouter::new(
                                    shared.registry.clone(),
                                    shared.catalog.clone(),
                                    fresh_estimator(account_count),
                                    None,
                                    cell_policy.clone(),
                                );
                                // Two warm-up selections populate active
                                // request pressure on both twins identically
                                // before the measured selection.
                                for _ in 0..2 {
                                    router
                                        .select_and_claim(&cell_facts, &BTreeSet::new())
                                        .await
                                        .expect("warm-up selection runs")
                                        .expect("warm-up candidate");
                                }
                                let claim = router
                                    .select_and_claim(&cell_facts, &BTreeSet::new())
                                    .await
                                    .expect("selection runs")
                                    .expect("a candidate is selectable");
                                snapshots.push(claim.selection_snapshot().clone());
                            }
                            assert_snapshots_equal(&snapshots[0], &snapshots[1]);
                            let snapshot = &snapshots[0];
                            // Eligible set: enabled (index % 5 != 4) and
                            // supported (even index).
                            let mut expected: Vec<String> = (0..account_count)
                                .filter(|index| index % 5 != 4 && index % 2 == 0)
                                .map(account_name)
                                .collect();
                            expected.sort();
                            let mut actual: Vec<String> = snapshot
                                .candidates
                                .iter()
                                .map(|candidate| candidate.account_name.clone())
                                .collect();
                            actual.sort();
                            assert_eq!(actual, expected, "eligible set (n={account_count})");
                            let mut reasons: Vec<(String, String)> = snapshot
                                .exclusions
                                .iter()
                                .map(|exclusion| {
                                    (
                                        exclusion.account_name.clone(),
                                        exclusion.reason_code.clone(),
                                    )
                                })
                                .collect();
                            reasons.sort();
                            reasons.dedup();
                            if account_count > 4 {
                                assert!(
                                    reasons.iter().any(|reason| reason.1 == "disabled"),
                                    "disabled exclusion (n={account_count})"
                                );
                            }
                            if account_count > 1 {
                                assert!(
                                    reasons.iter().any(|reason| reason.1 == "no_model"),
                                    "no_model exclusion (n={account_count})"
                                );
                            }
                            for candidate in &snapshot.candidates {
                                assert!(candidate.score.is_eligible);
                                assert!(candidate.score.final_score().is_finite());
                            }
                            assert_eligibility_sorted(&snapshot.candidates, prefer_native);
                            // With fairness off, the accepted selection is
                            // the top sorted candidate.
                            if fairness_mode == FairnessMode::Off {
                                assert_eq!(
                                    snapshot.selected_account_name,
                                    snapshot
                                        .candidates
                                        .first()
                                        .map(|candidate| candidate.account_name.clone())
                                );
                            }
                        }
                    }
                }
            }
        }
    }
}

#[test]
fn missing_estimator_entries_stay_excluded_as_malformed() {
    // The registry knows lonely-a, but the estimator has no entry for it, so
    // the ordered path must preserve the malformed_score exclusion. The
    // claim-less trace exposes the exclusion without needing a claim.
    let mut config = Config::default();
    let mut provider = eggpool::config::ProviderConfig {
        id: "provider-a".into(),
        base_url: "https://provider-a.invalid/v1".into(),
        protocols: vec!["openai".into()],
        auth: eggpool::config::ProviderAuthConfig {
            mode: "none".into(),
            ..Default::default()
        },
        ..Default::default()
    };
    provider.accounts.push(eggpool::config::AccountConfig {
        name: "lonely-a".into(),
        ..Default::default()
    });
    config.providers.insert("provider-a".into(), provider);
    config.validate().expect("config validates");
    let registry = AccountRegistry::from_config(
        &config,
        &[Account {
            id: 1,
            name: "lonely-a".into(),
            api_key_env: "LONELY_KEY".into(),
            enabled: true,
            weight: 1.0,
            provider_id: "provider-a".into(),
        }],
        &CredentialStore::default(),
    )
    .expect("registry builds");
    let mut catalog = ModelCatalogCache::default();
    catalog.set_account_provider("lonely-a", "provider-a");
    let mut model = ModelInput::new("model-a");
    model.protocol = Some("openai".into());
    model.protocol_source = Some("fixture".into());
    model.resolution_status = ProtocolResolutionStatus::Resolved;
    catalog
        .update_from_account("lonely-a", "provider-a", &[model], true, true)
        .expect("catalog model");
    let router = RoutingRouter::new(
        registry,
        catalog,
        QuotaEstimator::default(),
        None,
        EligibilityPolicy::default(),
    );
    let trace = router.trace_for(&facts(false, 10), None);
    assert!(trace.candidates.is_empty());
    assert_eq!(trace.exclusions.len(), 1);
    assert_eq!(trace.exclusions[0].account_name, "lonely-a");
    assert_eq!(trace.exclusions[0].reason_code, "malformed_score");
}
