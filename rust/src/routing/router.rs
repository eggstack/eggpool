//! Priority-aware routing plans and the local selection claim transaction.

use std::{
    collections::BTreeMap,
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};

use tokio::sync::Mutex as AsyncMutex;

use crate::{
    accounts::AccountRegistry,
    catalog::ModelCatalogCache,
    health::{
        AccountBackoffRecord, AccountBackoffRepository, BackoffReason, EvidenceProvenance,
        HealthManager, ModelQuarantine, ModelQuarantineRepository, compute_backoff_seconds,
        get_backoff_policy,
    },
    quota::QuotaEstimator,
    wire::ir::CanonicalUsage,
};

use super::{
    claim::{self, ClaimError, SelectionClaim},
    eligibility::{
        self, EligibilityPolicy, FairnessMode, FairnessScope, RoutingCandidate, RoutingPlan,
        RoutingRequestFacts, epoch_seconds,
    },
    fairness::{
        DeterministicFairnessRandom, FairnessDecision, FairnessKey, FairnessRandom, FairnessRotor,
    },
};

const RECOVERY_KEY_HARD_CAP: usize = 4_096;
/// The one floor both missing-account recovery readers apply. Keeping the
/// floor in a single place is what stops the attempt-retention window and the
/// attempt-allowance check from disagreeing about the same configured value.
const RECOVERY_MIN_INTERVAL_FLOOR_S: f64 = 1.0;

/// The policy-derived outcome of one failure effect.
struct PolicyBackoff {
    /// The bounded relative backoff, or `None` when the reason's policy has
    /// no timed backoff at all.
    delay: Option<Duration>,
    /// The model-scoped escalation rung.  The model-scoped durable row
    /// carries this, so a restart restores the model's own ladder.
    model_rung: u32,
    /// The account-scoped escalation rung.  The account-scoped durable row
    /// carries this.
    account_rung: u32,
}

impl PolicyBackoff {
    fn none() -> Self {
        Self {
            delay: None,
            model_rung: 1,
            account_rung: 1,
        }
    }
}
type RecoveryCallback = Arc<dyn Fn(&str) + Send + Sync>;

/// Non-persistent trace value passed to M7 for durable routing-decision
/// publication. Accepted traces are immutable claim snapshots; no request
/// body, credential, or error text enters the value.
pub type RoutingDecisionTrace = claim::SelectionSnapshot;

/// The durable health collaborators one generation owns.
///
/// The repositories are constructed once per generation against the
/// generation's database handle and are never constructed per request. They
/// mirror the in-memory health and quarantine state so a restart observes the
/// backoffs and quarantines the previous process decided.
#[derive(Clone, Debug)]
pub struct DurableHealth {
    backoffs: AccountBackoffRepository,
    quarantine: ModelQuarantineRepository,
}

impl DurableHealth {
    pub fn new(backoffs: AccountBackoffRepository, quarantine: ModelQuarantineRepository) -> Self {
        Self {
            backoffs,
            quarantine,
        }
    }
}

#[derive(Clone)]
struct RoutingState {
    registry: Arc<AccountRegistry>,
    catalog: Arc<Mutex<ModelCatalogCache>>,
    estimator: QuotaEstimator,
    health: Option<HealthManager>,
    quarantine: Option<ModelQuarantine>,
    durable: Option<DurableHealth>,
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
                durable: None,
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

    /// Attach the generation's durable health collaborators.
    ///
    /// The repositories are the single writer for the durable backoff and
    /// quarantine tables. Attaching them is what makes the failure path's
    /// effects survive a restart; without them the router keeps its effects
    /// in memory only.
    pub fn with_durable_health(mut self, durable: DurableHealth) -> Self {
        Arc::make_mut(&mut self.state).durable = Some(durable);
        self
    }

    pub fn with_missing_account_recovery(
        mut self,
        min_interval_s: f64,
        callback: RecoveryCallback,
    ) -> Self {
        // Reject a non-finite or non-positive interval instead of coercing
        // it. `f64::max` ignores NaN, so `f64::NAN.max(0.0)` is `0.0` and a
        // NaN interval silently became "no interval at all": every request
        // could re-probe a missing account immediately. An infinite interval
        // was accepted unchanged. A rejected value leaves the previously
        // configured interval in place, so the router is never left
        // partially applied.
        if min_interval_s.is_finite() && min_interval_s > 0.0 {
            Arc::make_mut(&mut self.state).recovery_min_interval_s = min_interval_s;
        }
        // The callback is stored in the type-erased recovery slot below. This
        // is installed as a wrapper so the lock-free selection code remains
        // independent of CatalogService's concrete type.
        Arc::make_mut(&mut self.state).recovery_callback = Some(callback);
        self
    }

    /// The effective minimum interval between missing-account recovery
    /// attempts. Both recovery readers call this, so they cannot disagree
    /// about the same configured value.
    fn recovery_min_interval_s(&self) -> f64 {
        self.state
            .recovery_min_interval_s
            .max(RECOVERY_MIN_INTERVAL_FLOOR_S)
    }

    pub fn with_recovery_clock(mut self, clock: Arc<dyn Fn() -> f64 + Send + Sync>) -> Self {
        Arc::make_mut(&mut self.state).recovery_clock = clock;
        self
    }

    pub fn build_routing_plan(&self, facts: &RoutingRequestFacts) -> RoutingPlan {
        let active = claim::active_snapshot(&self.state.claims);
        let catalog = self
            .state
            .catalog
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let (candidates, exclusions) = eligibility::build_eligible_candidates(
            &self.state.registry,
            &catalog,
            &self.state.estimator,
            self.state.health.as_ref(),
            self.state.quarantine.as_ref(),
            facts,
            &self.state.policy,
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
                .unwrap_or_else(|error| error.into_inner())
                .model_count(),
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
            .unwrap_or_else(|error| error.into_inner())
            .len()
    }

    /// Return bounded, secret-free counts for runtime diagnostics. The
    /// catalog is read only long enough to take its compact snapshot; no
    /// routing claim or fairness state is mutated.
    pub fn catalog_model_count(&self) -> usize {
        self.state
            .catalog
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .model_count()
    }

    /// Return the bounded model-id projection used by the public models
    /// endpoint. The snapshot is read-only and never performs provider or DB
    /// work.
    pub fn catalog_model_ids(&self) -> Vec<String> {
        self.state
            .catalog
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .exposed_model_ids()
    }

    /// Clone the generation-owned health manager for read-only status
    /// observation. The clone shares the underlying synchronous map; no
    /// probe is acquired and no breaker state is mutated.
    pub fn health_manager(&self) -> Option<HealthManager> {
        self.state.health.clone()
    }

    /// Return one health snapshot per registered account for status
    /// aggregation. Missing entries mean the account has no live observation.
    pub fn health_snapshots(&self) -> Vec<crate::health::AccountHealthSnapshot> {
        self.state
            .health
            .as_ref()
            .map_or_else(Vec::new, |health| health.accounts())
    }

    /// Return the full catalog snapshot for per-provider model counts.
    /// Read-only; never performs provider or DB work.
    pub fn catalog_snapshot(&self) -> crate::catalog::CacheSnapshot {
        self.state
            .catalog
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .snapshot()
    }

    /// Return immutable account identities for the active generation.
    pub fn account_identities(&self) -> Vec<crate::accounts::AccountIdentity> {
        self.state.registry.enabled_snapshot()
    }

    /// Return every account identity, including disabled rows, so status can
    /// list intentionally disabled providers explicitly.
    pub fn all_account_identities(&self) -> Vec<crate::accounts::AccountIdentity> {
        self.state.registry.all().cloned().collect()
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
        self.select_and_claim_with_preference(facts, exclude_accounts, None)
            .await
    }

    /// Select a new claim for one specific account.  C007 uses this only for
    /// an authorized alternate-wire retry: the account is held constant while
    /// the failed wire candidate is retired and its attempt is finalized.
    pub async fn select_and_claim_for_account(
        &self,
        facts: &RoutingRequestFacts,
        account_name: &str,
    ) -> Result<Option<SelectionClaim>, ClaimError> {
        self.select_and_claim_with_preference(
            facts,
            &std::collections::BTreeSet::new(),
            Some(account_name),
        )
        .await
    }

    async fn select_and_claim_with_preference(
        &self,
        facts: &RoutingRequestFacts,
        exclude_accounts: &std::collections::BTreeSet<String>,
        preferred_account: Option<&str>,
    ) -> Result<Option<SelectionClaim>, ClaimError> {
        self.maybe_recover_missing_support(facts, exclude_accounts);
        let _guard = self.selection_lock.lock().await;
        let active = claim::active_snapshot(&self.state.claims);
        let catalog = self
            .state
            .catalog
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let (mut candidates, mut exclusions) = eligibility::build_eligible_candidates(
            &self.state.registry,
            &catalog,
            &self.state.estimator,
            self.state.health.as_ref(),
            self.state.quarantine.as_ref(),
            facts,
            &self.state.policy,
            &active,
        );
        candidates.retain(|candidate| !exclude_accounts.contains(&candidate.account_name));
        if let Some(preferred_account) = preferred_account {
            candidates.retain(|candidate| candidate.account_name == preferred_account);
        }
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
        let selection = SelectionClaim::new(
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
        let published = match claim::publish(&self.state.claims, selection) {
            Ok(published) => published,
            Err(error) => {
                // `publish` refuses before the claim exists in the book, so
                // nothing downstream can release what was reserved above. The
                // pending quota claim and the half-open probe are compensated
                // here so a poisoned book cannot grow `pending_*` forever.
                if let Err(quota) = self.state.estimator.release_pending_claim(
                    &candidate.account_name,
                    facts.projected_tokens.max(0),
                    projected_cost,
                ) {
                    tracing::error!(
                        "routing claim publish failed and pending compensation failed: {quota}"
                    );
                }
                if owns_probe {
                    self.release_probe(&candidate.account_name);
                }
                return Err(error);
            }
        };
        // Fairness is committed only after the candidate owns all local
        // state, so a failed claim does not consume a rotor position.
        if let Some(fairness) = accepted_fairness.filter(|decision| decision.applied) {
            self.commit_fairness(&fairness, fairness.candidate_count);
        }
        drop(catalog);
        Ok(Some(published))
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

    /// Apply the one success transition owned by a completed finite request.
    /// The coordinator calls this before handing the claim to C006; duplicate
    /// finalization never repeats the health transition.
    pub fn record_success(&self, claim: &SelectionClaim) {
        if let Some(health) = self.state.health.as_ref() {
            health.record_success(claim.account_name(), Some(claim.canonical_model_id()));
        }
        // A terminal withdrawal has no expiry, so a stale withdrawal would
        // otherwise keep the account/model unroutable for the life of the
        // process. Serving the model successfully is authoritative evidence
        // that the withdrawal no longer holds. Quarantine is wall-clock
        // stamped: clear with wall time, not the monotonic recovery clock.
        if let Some(quarantine) = self.state.quarantine.as_ref() {
            quarantine.clear_authoritative_reappearance_for(
                claim.account_name(),
                claim.canonical_model_id(),
                epoch_seconds() as f64,
            );
        }
    }

    /// Fold one completed attempt's usage into the account's rolling quota
    /// window.
    ///
    /// The estimator only ever sees usage recorded here, so without this call
    /// the rolling window stayed empty for a whole process lifetime:
    /// `quota_score` never moved off its empty-window baseline and the hard-cap
    /// admission check had nothing to compare. Tokens are recorded on the same
    /// clock the scorer reads.
    pub fn record_usage(&self, claim: &SelectionClaim, usage: &CanonicalUsage) {
        // Saturate (not zero) on overflow: a corrupt huge usage must
        // over-count quota rather than vanish from the rolling window.
        // Missing usage stays zero.
        let bounded = |value: Option<u64>| {
            value
                .map(|value| i64::try_from(value).unwrap_or(i64::MAX))
                .unwrap_or_default()
        };
        let tokens = bounded(usage.input_tokens)
            .saturating_add(bounded(usage.output_tokens))
            .saturating_add(bounded(usage.cache_read_input_tokens))
            .saturating_add(bounded(usage.cache_write_input_tokens));
        // No attempt carries provider-reported cost: use the same estimated
        // pricing `estimate_cost` admits with (overrides/family/global) so
        // the per-token EWMA learns instead of staying permanently empty.
        let now = epoch_seconds() as f64;
        let cost = if tokens > 0 {
            self.state.estimator.estimate_cost(
                claim.account_name(),
                claim.canonical_model_id(),
                tokens,
                now,
            )
        } else {
            0
        };
        self.state.estimator.record_usage(
            claim.account_name(),
            tokens,
            cost,
            Some(claim.canonical_model_id()),
            now,
        );
    }

    /// Apply the bounded, typed portion of C005 effects available at the M5
    /// boundary. Model-scoped failures never advance the account circuit.
    ///
    /// The backoff duration is the policy's, not the caller's: the duration
    /// comes from the reason's own policy, escalated by the consecutive
    /// failure rung the health manager already tracks for the account or the
    /// model. `backoff_until` is the policy's retry-after hint input, so for
    /// the reasons whose policy consumes a provider hint it is the upstream
    /// hint and for every other reason the policy ignores it.
    ///
    /// The in-memory application happens first and is unchanged. The durable
    /// mirror follows, outside the selection lock: this runs on the failure
    /// path after the claim has already been published, so no selection
    /// critical section is held across the write. A durable write failure is
    /// reported and does not change the in-memory decision.
    // Each parameter is one bounded, typed effect decision. Grouping them
    // into a bag type would hide the invariant that each one is independently
    // owned by the failure engine, and the call sites read as a decision list
    // rather than a struct literal.
    #[allow(clippy::too_many_arguments)]
    pub async fn apply_failure_effects(
        &self,
        claim: &SelectionClaim,
        apply_account_penalty: bool,
        quarantine_model: bool,
        // The failure engine's model-effect label. Routing Selection M009
        // retired the only consumer that read it — the terminal-withdrawal
        // branch, which no producer could reach — so the router no longer
        // interprets it. The parameter is retained so the published
        // signature of this method does not change; the label itself is
        // still published on `FailureEffects::model_effect`.
        _model_effect: &str,
        backoff_reason: Option<&str>,
        backoff_until: Option<Duration>,
        circuit_penalty: bool,
    ) {
        let Some(health) = self.state.health.as_ref() else {
            return;
        };
        let reason = Self::resolve_backoff_reason(backoff_reason);
        // The policy owns the duration. `backoff_until` is the policy's
        // retry-after hint input: for the reasons whose policy consumes a
        // provider hint it is the upstream hint, and for every other reason
        // the policy ignores it and derives the duration from its own base
        // delay and the escalation rung.
        let policy = self.policy_backoff(health, claim, reason, backoff_until, quarantine_model);
        if quarantine_model {
            health.disable_model(
                claim.account_name(),
                claim.canonical_model_id(),
                policy.delay.map(|value| value.as_secs_f64()),
            );
            self.persist_model_quarantine(claim, reason, policy.delay, policy.model_rung)
                .await;
        }
        if !apply_account_penalty {
            return;
        }
        if let Some(delay) = policy.delay {
            health.record_cooldown(claim.account_name(), reason, delay.as_secs_f64());
        } else if circuit_penalty {
            health.record_failure(claim.account_name(), reason);
        }
        self.persist_account_backoff(claim, reason, policy.delay, policy.account_rung)
            .await;
    }

    /// Resolve the health reason a failure effect carries.
    ///
    /// The durable writer serializes a reason with
    /// [`BackoffReason::as_str`] and the durable reader parses it back with
    /// [`BackoffReason::try_from`], so the accepted vocabulary is exactly
    /// the written one. A value the reader cannot parse is corrupt state
    /// rather than a new vocabulary, so it is normalized to
    /// [`BackoffReason::Unknown`] and reported at a level an operator can
    /// observe instead of being silently dropped.
    fn resolve_backoff_reason(backoff_reason: Option<&str>) -> BackoffReason {
        match backoff_reason {
            None => BackoffReason::Unknown,
            Some(value) => BackoffReason::try_from(value).unwrap_or_else(|()| {
                // The value is deliberately not logged: the column is free
                // text in a corrupt row and is not part of the vocabulary.
                tracing::warn!("unrecognized stored backoff reason normalized to Unknown");
                BackoffReason::Unknown
            }),
        }
    }

    /// Derive the backoff a failure effect applies from the policy.
    ///
    /// The escalation rung is the health manager's own consecutive-failure
    /// counter: per model for the reasons whose policy scopes them to a
    /// model, per account for every other reason. The state that decides the
    /// duration is therefore the state the effect updates, and a repeated
    /// failure escalates without any second counter.
    ///
    /// The model-scoped rung is advanced only for the reasons whose policy
    /// scopes them to a model. For every other reason the model's own
    /// counter is read and never advanced, so a model quarantine caused by an
    /// account-scoped failure cannot inflate the model ladder.
    ///
    /// The duration never exceeds the cap the reason's policy declares.
    fn policy_backoff(
        &self,
        health: &HealthManager,
        claim: &SelectionClaim,
        reason: BackoffReason,
        retry_after: Option<Duration>,
        model_effect_applies: bool,
    ) -> PolicyBackoff {
        let Some(policy) = get_backoff_policy(reason) else {
            return PolicyBackoff::none();
        };
        let model_scoped = policy.account_model_scope && model_effect_applies;
        let model_rung = if model_scoped {
            health.record_model_failure(claim.account_name(), claim.canonical_model_id())
        } else {
            health.model_failure_count(claim.account_name(), claim.canonical_model_id())
        };
        let account_rung = health
            .snapshot(claim.account_name())
            .map(|state| {
                state
                    .consecutive_failures
                    .max(state.consecutive_cooldowns)
                    .saturating_add(1)
            })
            .unwrap_or(1);
        let rung = if model_scoped {
            model_rung
        } else {
            account_rung
        };
        let delay = compute_backoff_seconds(
            reason,
            rung,
            retry_after.map(|value| value.as_secs_f64()),
            true,
        )
        .filter(|seconds| seconds.is_finite())
        .map(|seconds| seconds.min(policy.cap).max(0.0))
        .and_then(|seconds| Duration::try_from_secs_f64(seconds).ok());
        PolicyBackoff {
            delay,
            model_rung,
            account_rung,
        }
    }

    /// Mirror the model-scoped effect into the durable quarantine table and
    /// the model-scoped durable backoff row.
    ///
    /// The key is the canonical-only partition the eligibility gate always
    /// evaluates, so the gate observes the same scope the live path decided.
    /// The expiry is the live backoff deadline rather than the quarantine
    /// TTL, so the gate and the health manager exclude the model for the
    /// same window.
    ///
    /// The row carries the live failure reason and the model's own
    /// escalation rung, so the durable vocabulary does not collapse two
    /// unrelated failure classes onto one reason and a restart cannot
    /// restore an account-scoped rung as the model's ladder.
    ///
    /// The live model-absence effect is always mirrored as a bounded
    /// observation. The runtime has no producer for the terminal withdrawal
    /// state, so this path cannot write one; the durable terminal vocabulary
    /// is retained as a reader for rows this runtime does not write, and
    /// mirroring the live effect as a bounded observation keeps the durable
    /// row consistent with the live one.
    async fn persist_model_quarantine(
        &self,
        claim: &SelectionClaim,
        reason: BackoffReason,
        backoff_until: Option<Duration>,
        model_rung: u32,
    ) {
        let (Some(durable), Some(quarantine)) =
            (self.state.durable.as_ref(), self.state.quarantine.as_ref())
        else {
            return;
        };
        let now = epoch_seconds() as f64;
        // The gate partitions a missing protocol as "unknown", never as a
        // concrete protocol, so the durable key must use the same partition.
        let upstream_protocol = claim.protocol().unwrap_or("unknown");
        let key = quarantine.key(
            claim.provider_id(),
            claim.account_name(),
            claim.canonical_model_id(),
            None,
            upstream_protocol,
        );
        let entry = quarantine.record_observation_until(
            key,
            EvidenceProvenance::RuntimeHttp,
            reason.as_str(),
            None,
            None,
            now,
            backoff_until.map(|delay| now + delay.as_secs_f64()),
        );
        if let Err(error) = durable.quarantine.upsert_entry(&entry).await {
            tracing::warn!("model quarantine persistence failed: {error}");
        }
        // The model-scoped backoff row is what restores the model disable
        // itself after a restart; the quarantine row restores the eligibility
        // gate. Both are needed for the effect to survive the process.
        let Some(delay) = backoff_until else {
            return;
        };
        let record = AccountBackoffRecord {
            id: 0,
            account_id: claim.account_id(),
            model_id: Some(claim.canonical_model_id().to_owned()),
            reason,
            status_code: None,
            error_class: None,
            consecutive_failures: model_rung.max(1),
            backoff_until_epoch: Some(now + delay.as_secs_f64()),
            last_failure_epoch: now,
            updated_epoch: now,
        };
        if let Err(error) = durable.backoffs.upsert(&record).await {
            tracing::warn!("model backoff persistence failed: {error}");
        }
    }

    /// Mirror the account-scoped cooldown into the durable backoff table.
    ///
    /// A timed cooldown is durable with the deadline the live path applied.
    /// The authentication-failure reason is the one shape the durable
    /// contract defines without a deadline: its policy declares no delay, and
    /// the durable validator accepts the deadline-less row, which the
    /// hydration restores as an account disable with no expiry. The row is
    /// written on the `(account_id, NULL, 'authentication_failed')` key, so a
    /// credential rejection survives a restart and is released by the
    /// credential-change path. Every other reason keeps its early return, so
    /// no other reason's durable behaviour changes.
    async fn persist_account_backoff(
        &self,
        claim: &SelectionClaim,
        reason: BackoffReason,
        backoff_until: Option<Duration>,
        consecutive_failures: u32,
    ) {
        let Some(durable) = self.state.durable.as_ref() else {
            return;
        };
        let deadline = if reason == BackoffReason::AuthenticationFailed {
            None
        } else {
            let Some(delay) = backoff_until else {
                return;
            };
            Some(delay.as_secs_f64())
        };
        let now = epoch_seconds() as f64;
        let record = AccountBackoffRecord {
            id: 0,
            account_id: claim.account_id(),
            model_id: None,
            reason,
            status_code: None,
            error_class: None,
            consecutive_failures,
            backoff_until_epoch: deadline.map(|seconds| now + seconds),
            last_failure_epoch: now,
            updated_epoch: now,
        };
        if let Err(error) = durable.backoffs.upsert(&record).await {
            tracing::warn!("account backoff persistence failed: {error}");
        }
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
        // Random preview (`apply=false`) returns identity order without
        // committing a random choice; it must not claim to have applied.
        let preview = self.state.policy.fairness_mode == FairnessMode::Random && !apply;
        let decision = FairnessDecision {
            mode: match self.state.policy.fairness_mode {
                FairnessMode::RoundRobin => "round_robin",
                FairnessMode::Random => "random",
                FairnessMode::Off => "off",
            }
            .into(),
            applied: !preview,
            key: key.to_key_string(),
            scope: self.scope_name(),
            candidate_count: band.len(),
            anchor_score: Some(best.score.final_score()),
            selected_index: if preview { None } else { Some(0) },
            selected_account_name: if preview {
                None
            } else {
                ordered_band
                    .first()
                    .map(|candidate| candidate.account_name.clone())
            },
            reason: if preview {
                "preview".into()
            } else {
                "ok".into()
            },
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
        let original_count = fairness.candidate_count;
        let band = std::mem::take(&mut fairness.ordered_accounts)
            .into_iter()
            .filter(|account| !rejected_accounts.contains(account.as_str()))
            .collect::<Vec<_>>();
        if let Some(selected_index) = band
            .iter()
            .position(|account| account == &selected.account_name)
        {
            // Commit the original ordering width so a probe-filtered band
            // still advances the contested rotor key instead of stalling on
            // the blocked head.
            fairness.candidate_count = original_count.max(band.len());
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

    fn commit_fairness(&self, fairness: &FairnessDecision, count: usize) {
        if self.state.policy.fairness_mode == FairnessMode::RoundRobin && count > 1 {
            // Advance the rotor under the key the band was *ordered* with,
            // not one recomputed from the selected candidate. The two differ
            // when the selected candidate reports a different protocol than
            // the band's best candidate, which would otherwise stall the
            // consulted rotor and skew an unrelated one.
            self.state.rotor.commit_keyed(&fairness.key, count);
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
            // Unreachable through the selection path: `probe_available_read_only`
            // already drops every candidate when no health manager is present.
            // Kept as a defensive branch that never owns a probe.
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
        let Some(health) = &self.state.health else {
            // Fail closed. An absent manager is not the same fact as a
            // present manager reporting a healthy account: an account whose
            // health cannot be evaluated is not treated as healthy. The
            // production construction path always supplies a manager, so this
            // changes only the construction variants that omit one.
            return false;
        };
        health.is_model_healthy_read_only(&candidate.account_name, &facts.canonical_model_id)
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
            .unwrap_or_else(|error| error.into_inner())
            .has_model(&facts.canonical_model_id)
        {
            return;
        }
        let now = (self.state.recovery_clock)();
        let catalog = self
            .state
            .catalog
            .lock()
            .unwrap_or_else(|error| error.into_inner());
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
                let Some(health) = &self.state.health else {
                    // Fail closed for the same reason as the read-only probe
                    // check: an account that cannot be health-evaluated is
                    // not eligible for a recovery probe.
                    return false;
                };
                health.is_model_healthy_read_only(&identity.account_name, &facts.canonical_model_id)
            })
            .filter(|identity| {
                !catalog.account_supports_model(&identity.account_name, &facts.canonical_model_id)
            })
            .map(|identity| identity.account_name)
            .collect();
        drop(catalog);
        let min_interval_s = self.recovery_min_interval_s();
        for account in missing {
            let mut attempts = self
                .state
                .recovery_attempt_at
                .lock()
                .unwrap_or_else(|error| error.into_inner());
            attempts.retain(|_, timestamp| now - *timestamp < min_interval_s);
            if attempts.len() >= RECOVERY_KEY_HARD_CAP
                && !attempts.contains_key(&account)
                && let Some(oldest) = attempts
                    .iter()
                    .min_by(|left, right| left.1.total_cmp(right.1))
                    .map(|(name, _)| name.clone())
            {
                attempts.remove(&oldest);
            }
            let allowed = attempts
                .get(&account)
                .is_none_or(|timestamp| now - *timestamp >= min_interval_s);
            if allowed {
                attempts.insert(account.clone(), now);
                drop(attempts);
                callback(&account);
            }
        }
    }
}
