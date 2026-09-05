//! Priority-aware routing plans and the local selection claim transaction.

use std::{
    collections::BTreeMap,
    sync::{Arc, Mutex},
    time::Instant,
};

use tokio::sync::Mutex as AsyncMutex;

use crate::{
    accounts::AccountRegistry,
    catalog::ModelCatalogCache,
    health::{HealthManager, ModelQuarantine},
    quota::QuotaEstimator,
};

use super::{
    claim::{self, ClaimError, SelectionClaim},
    eligibility::{
        self, EligibilityPolicy, FairnessMode, FairnessScope, RoutingCandidate, RoutingPlan,
        RoutingRequestFacts,
    },
    fairness::{
        DeterministicFairnessRandom, FairnessDecision, FairnessKey, FairnessRandom, FairnessRotor,
    },
};

const RECOVERY_KEY_HARD_CAP: usize = 4_096;
type RecoveryCallback = Arc<dyn Fn(&str) + Send + Sync>;

/// Non-persistent trace value passed to M7 for durable routing-decision
/// publication. Accepted traces are immutable claim snapshots; no request
/// body, credential, or error text enters the value.
pub type RoutingDecisionTrace = claim::SelectionSnapshot;

#[derive(Clone)]
struct RoutingState {
    registry: Arc<AccountRegistry>,
    catalog: Arc<Mutex<ModelCatalogCache>>,
    estimator: QuotaEstimator,
    health: Option<HealthManager>,
    quarantine: Option<ModelQuarantine>,
    policy: EligibilityPolicy,
    claims: Arc<Mutex<claim::ClaimBook>>,
    rotor: FairnessRotor,
    random: Arc<dyn FairnessRandom>,
    recovery_attempt_at: Arc<Mutex<BTreeMap<String, f64>>>,
    recovery_clock: Arc<dyn Fn() -> f64 + Send + Sync>,
    recovery_min_interval_s: f64,
    recovery_callback: Option<RecoveryCallback>,
}

/// Owns the in-memory routing state for one migration generation.
#[derive(Clone)]
pub struct RoutingRouter {
    state: Arc<RoutingState>,
    selection_lock: Arc<AsyncMutex<()>>,
}

impl std::fmt::Debug for RoutingRouter {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("RoutingRouter")
            .field("fairness_keys", &self.state.rotor.key_count())
            .field("claim_count", &claim::claim_ids(&self.state.claims))
            .finish()
    }
}

impl RoutingRouter {
    pub fn new(
        registry: AccountRegistry,
        catalog: ModelCatalogCache,
        estimator: QuotaEstimator,
        health: Option<HealthManager>,
        policy: EligibilityPolicy,
    ) -> Self {
        Self::with_shared_catalog(
            registry,
            Arc::new(Mutex::new(catalog)),
            estimator,
            health,
            policy,
        )
    }

    pub fn with_shared_catalog(
        registry: AccountRegistry,
        catalog: Arc<Mutex<ModelCatalogCache>>,
        estimator: QuotaEstimator,
        health: Option<HealthManager>,
        policy: EligibilityPolicy,
    ) -> Self {
        let clock = Instant::now();
        Self {
            state: Arc::new(RoutingState {
                registry: Arc::new(registry),
                catalog,
                estimator,
                health,
                quarantine: None,
                policy,
                claims: claim::book(),
                rotor: FairnessRotor::new(),
                random: Arc::new(DeterministicFairnessRandom::default()),
                recovery_attempt_at: Arc::new(Mutex::new(BTreeMap::new())),
                recovery_clock: Arc::new(move || clock.elapsed().as_secs_f64()),
                recovery_min_interval_s: 60.0,
                recovery_callback: None,
            }),
            selection_lock: Arc::new(AsyncMutex::new(())),
        }
    }

    pub fn with_random_source(mut self, random: Arc<dyn FairnessRandom>) -> Self {
        Arc::make_mut(&mut self.state).random = random;
        self
    }

    pub fn with_quarantine(mut self, quarantine: ModelQuarantine) -> Self {
        Arc::make_mut(&mut self.state).quarantine = Some(quarantine);
        self
    }

    pub fn with_missing_account_recovery(
        mut self,
        min_interval_s: f64,
        callback: RecoveryCallback,
    ) -> Self {
        Arc::make_mut(&mut self.state).recovery_min_interval_s = min_interval_s.max(0.0);
        // The callback is stored in the type-erased recovery slot below. This
        // is installed as a wrapper so the lock-free selection code remains
        // independent of CatalogService's concrete type.
        Arc::make_mut(&mut self.state).recovery_callback = Some(callback);
        self
    }

    pub fn with_recovery_clock(mut self, clock: Arc<dyn Fn() -> f64 + Send + Sync>) -> Self {
        Arc::make_mut(&mut self.state).recovery_clock = clock;
        self
    }

    pub fn build_routing_plan(&self, facts: &RoutingRequestFacts) -> RoutingPlan {
        let active = claim::active_snapshot(&self.state.claims);
        let catalog = self.state.catalog.lock().expect("catalog lock");
        let (candidates, exclusions) = eligibility::build_eligible_candidates(
            &self.state.registry,
            &catalog,
            &self.state.estimator,
            self.state.health.as_ref(),
            self.state.quarantine.as_ref(),
            facts,
            self.state.policy.clone(),
            &active,
        );
        drop(catalog);
        self.plan_from_candidates(facts, candidates, exclusions)
    }

    fn plan_from_candidates(
        &self,
        facts: &RoutingRequestFacts,
        candidates: Vec<RoutingCandidate>,
        exclusions: Vec<super::RoutingExclusion>,
    ) -> RoutingPlan {
        let eligible_account_names = candidates
            .iter()
            .map(|candidate| candidate.account_name.clone())
            .collect();
        let mut candidates = candidates;
        let fairness = self.fairness_order(facts, &mut candidates, false).1;
        RoutingPlan {
            requested_model_id: facts.canonical_model_id.clone(),
            requested_provider_id: facts.provider_id.clone(),
            requested_protocol: facts.requested_protocol.clone(),
            request_surface: facts.request_surface.clone(),
            eligible_account_names,
            candidates,
            exclusions,
            fairness,
            catalog_version: self
                .state
                .catalog
                .lock()
                .expect("catalog lock")
                .snapshot()
                .model_ids
                .len(),
            health_version: self
                .state
                .health
                .as_ref()
                .map_or(0, |health| health.accounts().len()),
        }
    }

    /// Read-only readiness pairing check. No health probe, rotor position,
    /// active count, pending load, callback, or database operation is used.
    pub fn has_eligible_pairing(&self, facts: &RoutingRequestFacts) -> bool {
        !self.build_routing_plan(facts).candidates.is_empty()
    }

    pub fn active_request_count(&self, account_name: &str) -> i64 {
        claim::active_count(&self.state.claims, account_name)
    }

    pub fn fairness_key_count(&self) -> usize {
        self.state.rotor.key_count()
    }
    pub fn recovery_key_count(&self) -> usize {
        self.state
            .recovery_attempt_at
            .lock()
            .expect("recovery lock")
            .len()
    }

    /// Select, acquire any required half-open probe, and publish active plus
    /// pending quota ownership while holding one async mutex. There is no
    /// await after the mutex is acquired and no SQLite/network operation in
    /// this critical section.
    pub async fn select_and_claim(
        &self,
        facts: &RoutingRequestFacts,
        exclude_accounts: &std::collections::BTreeSet<String>,
    ) -> Result<Option<SelectionClaim>, ClaimError> {
        self.maybe_recover_missing_support(facts, exclude_accounts);
        let _guard = self.selection_lock.lock().await;
        let active = claim::active_snapshot(&self.state.claims);
        let catalog = self.state.catalog.lock().expect("catalog lock");
        let (mut candidates, mut exclusions) = eligibility::build_eligible_candidates(
            &self.state.registry,
            &catalog,
            &self.state.estimator,
            self.state.health.as_ref(),
            self.state.quarantine.as_ref(),
            facts,
            self.state.policy.clone(),
            &active,
        );
        candidates.retain(|candidate| !exclude_accounts.contains(&candidate.account_name));
        candidates.retain(|candidate| self.probe_available_read_only(candidate, facts));
        let (ordered, fairness) = self.fairness_order(facts, &mut candidates, true);
        let mut accepted = None;
        let mut accepted_fairness = fairness;
        for candidate in ordered {
            let Some(owns_probe) = self.acquire_probe(&candidate, facts) else {
                exclusions.push(super::RoutingExclusion {
                    account_name: candidate.account_name,
                    reason_code: "probe_unavailable".into(),
                });
                continue;
            };
            accepted = Some((candidate, owns_probe));
            break;
        }
        let Some((candidate, owns_probe)) = accepted else {
            drop(catalog);
            return Ok(None);
        };
        let projected_cost = self.state.estimator.estimate_cost(
            &candidate.account_name,
            &facts.canonical_model_id,
            facts.projected_tokens.max(0),
            facts.now as f64,
        );
        let rejected_accounts = exclusions
            .iter()
            .filter(|exclusion| exclusion.reason_code == "probe_unavailable")
            .map(|exclusion| exclusion.account_name.as_str())
            .collect::<std::collections::BTreeSet<_>>();
        let trace_candidates = candidates
            .iter()
            .filter(|candidate| !rejected_accounts.contains(candidate.account_name.as_str()))
            .cloned()
            .collect::<Vec<_>>();
        accepted_fairness =
            self.accepted_fairness(accepted_fairness, &candidate, &rejected_accounts);
        let account_id = self
            .state
            .registry
            .get(&candidate.account_name)
            .map_or(0, |identity| identity.account_id);
        let snapshot = claim::SelectionSnapshot::accepted(
            facts,
            trace_candidates,
            exclusions,
            accepted_fairness.clone(),
            &candidate,
            account_id,
        );
        if let Err(error) = self.state.estimator.add_pending_claim(
            &candidate.account_name,
            facts.projected_tokens.max(0),
            projected_cost,
        ) {
            if owns_probe {
                self.release_probe(&candidate.account_name);
            }
            return Err(ClaimError::Quota(error));
        }
        let mut selection = SelectionClaim::new(
            account_id,
            candidate.account_name.clone(),
            candidate.provider_id.clone(),
            candidate.canonical_model_id.clone(),
            candidate.upstream_model_id.clone(),
            candidate.protocol.clone(),
            candidate.priority,
            candidate.requires_transcode,
            facts.projected_tokens.max(0),
            projected_cost,
            owns_probe,
            snapshot,
            self.state.claims.clone(),
            self.state.estimator.clone(),
            self.state.health.clone(),
        );
        selection = claim::publish(&self.state.claims, selection)?;
        // Fairness is committed only after the candidate owns all local
        // state, so a failed claim does not consume a rotor position.
        if let Some(fairness) = accepted_fairness.filter(|decision| decision.applied) {
            self.commit_fairness(facts, &candidate, fairness.candidate_count);
        }
        drop(catalog);
        Ok(Some(selection))
    }

    pub fn trace_for(
        &self,
        facts: &RoutingRequestFacts,
        claim: Option<&SelectionClaim>,
    ) -> RoutingDecisionTrace {
        if let Some(claim) = claim {
            return claim.selection_snapshot().clone();
        }
        claim::SelectionSnapshot::from_plan(self.build_routing_plan(facts))
    }

    fn fairness_order(
        &self,
        facts: &RoutingRequestFacts,
        candidates: &mut [RoutingCandidate],
        apply: bool,
    ) -> (Vec<RoutingCandidate>, Option<FairnessDecision>) {
        let Some(best) = candidates.first() else {
            return (Vec::new(), None);
        };
        let key = self.fairness_key(facts, best.priority, best.protocol.clone());
        let epsilon = self
            .state
            .policy
            .fairness_epsilon
            .unwrap_or(self.state.policy.scorer.near_tie_epsilon);
        let mut band = Vec::new();
        let mut rest = Vec::new();
        for candidate in candidates.iter().cloned() {
            if candidate.priority != best.priority
                || (self.state.policy.scorer.prefer_native
                    && candidate.requires_transcode != best.requires_transcode)
                || (candidate.score.final_score() - best.score.final_score()).abs() >= epsilon
            {
                rest.push(candidate);
            } else {
                band.push(candidate);
            }
        }
        if self.state.policy.fairness_mode == FairnessMode::Off || band.len() < 2 {
            let reason = if self.state.policy.fairness_mode == FairnessMode::Off {
                "disabled"
            } else if band.len() < 2 {
                "not_tied"
            } else {
                "ok"
            };
            return (
                band.into_iter().chain(rest).collect(),
                Some(FairnessDecision::not_applied(
                    match self.state.policy.fairness_mode {
                        FairnessMode::Off => "off",
                        FairnessMode::RoundRobin => "round_robin",
                        FairnessMode::Random => "random",
                    },
                    &key,
                    &self.scope_name(),
                    candidates.len(),
                    Some(best.score.final_score()),
                    reason,
                )),
            );
        }
        let ordered_indexes = if self.state.policy.fairness_mode == FairnessMode::RoundRobin {
            self.state
                .rotor
                .order_named(&key, &band, |candidate| candidate.account_name.as_str())
        } else if self.state.policy.fairness_mode == FairnessMode::Random && !apply {
            (0..band.len()).collect()
        } else {
            let mut indexes: Vec<usize> = (0..band.len()).collect();
            let chosen = self.state.random.choose_index(indexes.len());
            let length = indexes.len();
            indexes.rotate_left(chosen % length);
            indexes
        };
        let ordered_band: Vec<RoutingCandidate> = ordered_indexes
            .iter()
            .map(|index| band[*index].clone())
            .collect();
        let decision = FairnessDecision {
            mode: match self.state.policy.fairness_mode {
                FairnessMode::RoundRobin => "round_robin",
                FairnessMode::Random => "random",
                FairnessMode::Off => "off",
            }
            .into(),
            applied: true,
            key: key.to_key_string(),
            scope: self.scope_name(),
            candidate_count: band.len(),
            anchor_score: Some(best.score.final_score()),
            selected_index: Some(0),
            selected_account_name: ordered_band
                .first()
                .map(|candidate| candidate.account_name.clone()),
            reason: "ok".into(),
            ordered_accounts: ordered_band
                .iter()
                .map(|candidate| candidate.account_name.clone())
                .collect(),
        };
        (
            ordered_band.into_iter().chain(rest).collect(),
            Some(decision),
        )
    }

    fn accepted_fairness(
        &self,
        fairness: Option<FairnessDecision>,
        selected: &RoutingCandidate,
        rejected_accounts: &std::collections::BTreeSet<&str>,
    ) -> Option<FairnessDecision> {
        let mut fairness = fairness?;
        if !fairness.applied {
            return Some(fairness);
        }
        let band = std::mem::take(&mut fairness.ordered_accounts)
            .into_iter()
            .filter(|account| !rejected_accounts.contains(account.as_str()))
            .collect::<Vec<_>>();
        if let Some(selected_index) = band
            .iter()
            .position(|account| account == &selected.account_name)
        {
            fairness.candidate_count = band.len();
            fairness.selected_index = Some(selected_index);
            fairness.selected_account_name = Some(selected.account_name.clone());
            fairness.ordered_accounts = band;
        } else {
            fairness.applied = false;
            fairness.candidate_count = 0;
            fairness.selected_index = None;
            fairness.selected_account_name = None;
            fairness.reason = "probe_unavailable".into();
            fairness.ordered_accounts.clear();
        }
        Some(fairness)
    }

    fn commit_fairness(
        &self,
        facts: &RoutingRequestFacts,
        candidate: &RoutingCandidate,
        count: usize,
    ) {
        if self.state.policy.fairness_mode == FairnessMode::RoundRobin && count > 1 {
            self.state.rotor.commit(
                &self.fairness_key(facts, candidate.priority, candidate.protocol.clone()),
                count,
            );
        }
    }

    fn fairness_key(
        &self,
        facts: &RoutingRequestFacts,
        priority: u32,
        protocol: Option<String>,
    ) -> FairnessKey {
        FairnessKey {
            provider_id: match self.state.policy.fairness_scope {
                FairnessScope::PriorityModelProtocol => None,
                _ => facts.provider_id.clone(),
            },
            model_id: facts.canonical_model_id.clone(),
            protocol: match self.state.policy.fairness_scope {
                FairnessScope::ProviderModel => None,
                _ => protocol,
            },
            priority,
            client_protocol: facts.client_protocol.clone(),
        }
    }

    fn scope_name(&self) -> String {
        match self.state.policy.fairness_scope {
            FairnessScope::ProviderModelProtocol => "provider_model_protocol",
            FairnessScope::ProviderModel => "provider_model",
            FairnessScope::PriorityModelProtocol => "priority_model_protocol",
        }
        .into()
    }

    fn acquire_probe(
        &self,
        candidate: &RoutingCandidate,
        facts: &RoutingRequestFacts,
    ) -> Option<bool> {
        let Some(health) = &self.state.health else {
            return Some(false);
        };
        let before = health
            .snapshot(&candidate.account_name)
            .map_or(crate::health::CircuitState::Closed, |snapshot| {
                snapshot.circuit.state
            });
        if !health.try_acquire_request(&candidate.account_name, &facts.canonical_model_id) {
            return None;
        }
        Some(before != crate::health::CircuitState::Closed)
    }

    fn probe_available_read_only(
        &self,
        candidate: &RoutingCandidate,
        facts: &RoutingRequestFacts,
    ) -> bool {
        self.state.health.as_ref().is_none_or(|health| {
            health.is_model_healthy_read_only(&candidate.account_name, &facts.canonical_model_id)
        })
    }

    fn release_probe(&self, account_name: &str) {
        if let Some(health) = &self.state.health {
            health.release_request(account_name);
        }
    }

    fn maybe_recover_missing_support(
        &self,
        facts: &RoutingRequestFacts,
        excluded: &std::collections::BTreeSet<String>,
    ) {
        let Some(callback) = &self.state.recovery_callback else {
            return;
        };
        if !self
            .state
            .catalog
            .lock()
            .expect("catalog lock")
            .has_model(&facts.canonical_model_id)
        {
            return;
        }
        let now = (self.state.recovery_clock)();
        let catalog = self.state.catalog.lock().expect("catalog lock");
        let missing: Vec<String> = self
            .state
            .registry
            .enabled_snapshot()
            .into_iter()
            .filter(|identity| identity.has_usable_credentials)
            .filter(|identity| !excluded.contains(&identity.account_name))
            .filter(|identity| {
                facts
                    .provider_id
                    .as_ref()
                    .is_none_or(|provider| &identity.provider_id == provider)
            })
            .filter(|identity| {
                self.state.health.as_ref().is_none_or(|health| {
                    health.is_model_healthy_read_only(
                        &identity.account_name,
                        &facts.canonical_model_id,
                    )
                })
            })
            .filter(|identity| {
                !catalog.account_supports_model(&identity.account_name, &facts.canonical_model_id)
            })
            .map(|identity| identity.account_name)
            .collect();
        drop(catalog);
        for account in missing {
            let mut attempts = self
                .state
                .recovery_attempt_at
                .lock()
                .expect("recovery lock");
            attempts.retain(|_, timestamp| {
                now - *timestamp < self.state.recovery_min_interval_s.max(1.0)
            });
            if attempts.len() >= RECOVERY_KEY_HARD_CAP && !attempts.contains_key(&account) {
                if let Some(oldest) = attempts
                    .iter()
                    .min_by(|left, right| left.1.total_cmp(right.1))
                    .map(|(name, _)| name.clone())
                {
                    attempts.remove(&oldest);
                }
            }
            let allowed = attempts
                .get(&account)
                .is_none_or(|timestamp| now - *timestamp >= self.state.recovery_min_interval_s);
            if allowed {
                attempts.insert(account.clone(), now);
                drop(attempts);
                callback(&account);
            }
        }
    }
}
