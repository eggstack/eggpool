//! Local selection claims and exact ownership compensation.

use std::{
    collections::{BTreeMap, VecDeque},
    sync::{Arc, Mutex},
};

use thiserror::Error;

use crate::{
    health::HealthManager,
    quota::{QuotaEstimator, QuotaInvariantError, RoutingScore},
};

use super::{
    FairnessDecision, RoutingCandidate, RoutingExclusion, RoutingPlan, RoutingRequestFacts,
};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ClaimState {
    Pending,
    Converted,
    Released,
    RolledBack,
}

#[derive(Debug, Error)]
pub enum ClaimError {
    #[error("claim {claim_id} is already {state}")]
    AlreadyTransitioned { claim_id: u64, state: &'static str },
    #[error("claim {claim_id} is not pending and cannot be converted")]
    NotPending { claim_id: u64 },
    #[error("claim quota ownership failed: {0}")]
    Quota(#[from] QuotaInvariantError),
    #[error("claim account {account_name:?} is not registered in local ownership")]
    UnknownAccount { account_name: String },
    #[error("active ownership for claim account {account_name:?} would underflow")]
    ActiveOwnershipUnderflow { account_name: String },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ClaimTransition {
    RolledBack,
    Converted,
    Released,
    AlreadyTransitioned,
}

/// Immutable evidence for one accepted local routing decision.
///
/// The snapshot is constructed from the pre-publication candidate state and
/// travels with the claim. It deliberately contains only bounded routing
/// metadata; request bodies, credentials, and provider error text never cross
/// this boundary.
#[derive(Debug, Clone, PartialEq, serde::Serialize)]
pub struct SelectionSnapshot {
    pub requested_model_id: String,
    pub provider_id: Option<String>,
    pub protocol: Option<String>,
    pub request_surface: String,
    pub candidates: Vec<RoutingCandidate>,
    pub exclusions: Vec<RoutingExclusion>,
    pub eligible_candidate_count: usize,
    pub top_account_name: Option<String>,
    pub top_score: Option<RoutingScore>,
    pub selected_account_name: Option<String>,
    pub selected_account_id: Option<i64>,
    pub selected_provider_id: Option<String>,
    pub selected_model_id: Option<String>,
    pub selected_upstream_model_id: Option<String>,
    pub selected_protocol: Option<String>,
    pub selected_priority: Option<u32>,
    pub selected_requires_transcode: Option<bool>,
    pub selected_score: Option<RoutingScore>,
    pub fairness: Option<FairnessDecision>,
    pub local_claim_id: Option<u64>,
}

impl SelectionSnapshot {
    pub(crate) fn accepted(
        facts: &RoutingRequestFacts,
        candidates: Vec<RoutingCandidate>,
        exclusions: Vec<RoutingExclusion>,
        fairness: Option<FairnessDecision>,
        selected: &RoutingCandidate,
        selected_account_id: i64,
    ) -> Self {
        let top = candidates.first();
        Self {
            requested_model_id: facts.canonical_model_id.clone(),
            provider_id: facts.provider_id.clone(),
            protocol: facts.requested_protocol.clone(),
            request_surface: facts.request_surface.clone(),
            eligible_candidate_count: candidates.len(),
            top_account_name: top.map(|candidate| candidate.account_name.clone()),
            top_score: top.map(|candidate| candidate.score.clone()),
            candidates,
            exclusions,
            selected_account_name: Some(selected.account_name.clone()),
            selected_account_id: Some(selected_account_id),
            selected_provider_id: Some(selected.provider_id.clone()),
            selected_model_id: Some(selected.canonical_model_id.clone()),
            selected_upstream_model_id: Some(selected.upstream_model_id.clone()),
            selected_protocol: selected.protocol.clone(),
            selected_priority: Some(selected.priority),
            selected_requires_transcode: Some(selected.requires_transcode),
            selected_score: Some(selected.score.clone()),
            fairness,
            local_claim_id: None,
        }
    }

    pub(crate) fn from_plan(plan: RoutingPlan) -> Self {
        let top = plan.candidates.first();
        Self {
            requested_model_id: plan.requested_model_id,
            provider_id: plan.requested_provider_id,
            protocol: plan.requested_protocol,
            request_surface: plan.request_surface,
            eligible_candidate_count: plan.candidates.len(),
            top_account_name: top.map(|candidate| candidate.account_name.clone()),
            top_score: top.map(|candidate| candidate.score.clone()),
            candidates: plan.candidates,
            exclusions: plan.exclusions,
            selected_account_name: None,
            selected_account_id: None,
            selected_provider_id: None,
            selected_model_id: None,
            selected_upstream_model_id: None,
            selected_protocol: None,
            selected_priority: None,
            selected_requires_transcode: None,
            selected_score: None,
            fairness: plan.fairness,
            local_claim_id: None,
        }
    }

    fn with_local_claim_id(mut self, id: u64) -> Self {
        self.local_claim_id = Some(id);
        self
    }
}

#[derive(Debug, Clone)]
struct OwnedClaim {
    state: ClaimState,
    quota_released: bool,
}

#[derive(Debug, Default)]
pub(crate) struct ClaimBook {
    next_id: u64,
    active_requests: BTreeMap<String, i64>,
    claims: BTreeMap<u64, OwnedClaim>,
    terminal_order: VecDeque<u64>,
}

/// A non-secret local ownership token returned after active/pending/probe
/// publication. It has no Drop side effects; callers must explicitly choose
/// rollback, conversion, and final active release.
#[derive(Debug, Clone)]
pub struct SelectionClaim {
    id: u64,
    account_id: i64,
    account_name: String,
    provider_id: String,
    canonical_model_id: String,
    upstream_model_id: String,
    protocol: Option<String>,
    priority: u32,
    requires_transcode: bool,
    projected_tokens: i64,
    projected_cost_microdollars: i64,
    owns_probe: bool,
    snapshot: Arc<SelectionSnapshot>,
    book: Arc<Mutex<ClaimBook>>,
    estimator: QuotaEstimator,
    health: Option<HealthManager>,
}

impl SelectionClaim {
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn new(
        account_id: i64,
        account_name: String,
        provider_id: String,
        canonical_model_id: String,
        upstream_model_id: String,
        protocol: Option<String>,
        priority: u32,
        requires_transcode: bool,
        projected_tokens: i64,
        projected_cost_microdollars: i64,
        owns_probe: bool,
        snapshot: SelectionSnapshot,
        book: Arc<Mutex<ClaimBook>>,
        estimator: QuotaEstimator,
        health: Option<HealthManager>,
    ) -> Self {
        Self {
            id: 0,
            account_id,
            account_name,
            provider_id,
            canonical_model_id,
            upstream_model_id,
            protocol,
            priority,
            requires_transcode,
            projected_tokens,
            projected_cost_microdollars,
            owns_probe,
            snapshot: Arc::new(snapshot),
            book,
            estimator,
            health,
        }
    }

    pub fn id(&self) -> u64 {
        self.id
    }
    pub fn account_id(&self) -> i64 {
        self.account_id
    }
    pub fn account_name(&self) -> &str {
        &self.account_name
    }
    pub fn provider_id(&self) -> &str {
        &self.provider_id
    }
    pub fn canonical_model_id(&self) -> &str {
        &self.canonical_model_id
    }
    pub fn upstream_model_id(&self) -> &str {
        &self.upstream_model_id
    }
    pub fn protocol(&self) -> Option<&str> {
        self.protocol.as_deref()
    }
    pub fn priority(&self) -> u32 {
        self.priority
    }
    pub fn requires_transcode(&self) -> bool {
        self.requires_transcode
    }
    pub fn projected_tokens(&self) -> i64 {
        self.projected_tokens
    }
    pub fn projected_cost_microdollars(&self) -> i64 {
        self.projected_cost_microdollars
    }
    pub fn owns_probe(&self) -> bool {
        self.owns_probe
    }

    pub fn selection_snapshot(&self) -> &SelectionSnapshot {
        &self.snapshot
    }

    pub fn rollback_claim(&self) -> Result<ClaimTransition, ClaimError> {
        let mut book = self.book.lock().expect("claim lock");
        let state = book
            .claims
            .get(&self.id)
            .map(|claim| claim.state)
            .ok_or_else(|| ClaimError::UnknownAccount {
                account_name: self.account_name.clone(),
            })?;
        if state != ClaimState::Pending {
            return Ok(ClaimTransition::AlreadyTransitioned);
        }
        self.estimator.release_pending_claim(
            &self.account_name,
            self.projected_tokens,
            self.projected_cost_microdollars,
        )?;
        decrement_active(&mut book, &self.account_name)?;
        release_probe(&self.health, &self.account_name, self.owns_probe);
        book.claims.get_mut(&self.id).expect("claim exists").state = ClaimState::RolledBack;
        prune_terminal_claims(&mut book);
        Ok(ClaimTransition::RolledBack)
    }

    pub fn convert_claim_after_durable_publication(&self) -> Result<ClaimTransition, ClaimError> {
        let mut book = self.book.lock().expect("claim lock");
        let claim = book
            .claims
            .get(&self.id)
            .ok_or_else(|| ClaimError::UnknownAccount {
                account_name: self.account_name.clone(),
            })?;
        if claim.state == ClaimState::Converted {
            return Ok(ClaimTransition::AlreadyTransitioned);
        }
        if claim.state != ClaimState::Pending {
            return Err(ClaimError::NotPending { claim_id: self.id });
        }
        self.estimator.convert_pending_claim(
            &self.account_name,
            self.projected_tokens,
            self.projected_cost_microdollars,
        )?;
        book.claims.get_mut(&self.id).expect("claim exists").state = ClaimState::Converted;
        Ok(ClaimTransition::Converted)
    }

    pub fn release_active_claim(&self) -> Result<ClaimTransition, ClaimError> {
        let mut book = self.book.lock().expect("claim lock");
        let claim = book
            .claims
            .get(&self.id)
            .ok_or_else(|| ClaimError::UnknownAccount {
                account_name: self.account_name.clone(),
            })?;
        if matches!(claim.state, ClaimState::Released | ClaimState::RolledBack) {
            return Ok(ClaimTransition::AlreadyTransitioned);
        }
        if claim.state == ClaimState::Pending {
            self.estimator.release_pending_claim(
                &self.account_name,
                self.projected_tokens,
                self.projected_cost_microdollars,
            )?;
        }
        decrement_active(&mut book, &self.account_name)?;
        release_probe(&self.health, &self.account_name, self.owns_probe);
        book.claims.get_mut(&self.id).expect("claim exists").state = ClaimState::Released;
        prune_terminal_claims(&mut book);
        Ok(ClaimTransition::Released)
    }

    /// Release the converted quota reservation independently of active-count
    /// ownership.  C006 calls this after durable terminal convergence; the
    /// separate bit keeps duplicate finalization from subtracting twice.
    pub fn release_quota_reservation(&self) -> Result<ClaimTransition, ClaimError> {
        let mut book = self.book.lock().expect("claim lock");
        let claim = book
            .claims
            .get(&self.id)
            .ok_or_else(|| ClaimError::UnknownAccount {
                account_name: self.account_name.clone(),
            })?;
        if claim.quota_released
            || matches!(claim.state, ClaimState::Pending | ClaimState::RolledBack)
        {
            return Ok(ClaimTransition::AlreadyTransitioned);
        }
        self.estimator.remove_reservation(
            &self.account_name,
            1,
            self.projected_tokens,
            self.projected_cost_microdollars,
        )?;
        book.claims
            .get_mut(&self.id)
            .expect("claim exists")
            .quota_released = true;
        Ok(ClaimTransition::Released)
    }
}

fn decrement_active(book: &mut ClaimBook, account_name: &str) -> Result<(), ClaimError> {
    let count = book.active_requests.get(account_name).copied().unwrap_or(0);
    if count < 1 {
        return Err(ClaimError::ActiveOwnershipUnderflow {
            account_name: account_name.into(),
        });
    }
    book.active_requests.insert(account_name.into(), count - 1);
    Ok(())
}

fn release_probe(health: &Option<HealthManager>, account_name: &str, owns_probe: bool) {
    if owns_probe {
        if let Some(health) = health {
            health.release_request(account_name);
        }
    }
}

pub(crate) fn book() -> Arc<Mutex<ClaimBook>> {
    Arc::new(Mutex::new(ClaimBook::default()))
}

pub(crate) fn publish(
    book: &Arc<Mutex<ClaimBook>>,
    mut claim: SelectionClaim,
) -> Result<SelectionClaim, ClaimError> {
    let mut state = book.lock().expect("claim lock");
    state.next_id = state.next_id.saturating_add(1);
    let id = state.next_id;
    *state
        .active_requests
        .entry(claim.account_name.clone())
        .or_default() = state
        .active_requests
        .get(&claim.account_name)
        .copied()
        .unwrap_or(0)
        .saturating_add(1);
    state.claims.insert(
        id,
        OwnedClaim {
            state: ClaimState::Pending,
            quota_released: false,
        },
    );
    state.terminal_order.push_back(id);
    claim.id = id;
    claim.snapshot = Arc::new((*claim.snapshot).clone().with_local_claim_id(id));
    Ok(claim)
}

fn prune_terminal_claims(book: &mut ClaimBook) {
    const CLAIM_RECORD_HARD_CAP: usize = 4_096;
    let scan_limit = book.terminal_order.len();
    for _ in 0..scan_limit {
        if book.claims.len() <= CLAIM_RECORD_HARD_CAP {
            break;
        }
        let Some(id) = book.terminal_order.pop_front() else {
            break;
        };
        if book.claims.get(&id).is_some_and(|claim| {
            matches!(claim.state, ClaimState::Released | ClaimState::RolledBack)
        }) {
            book.claims.remove(&id);
        } else {
            book.terminal_order.push_back(id);
        }
    }
}

pub(crate) fn active_snapshot(book: &Arc<Mutex<ClaimBook>>) -> BTreeMap<String, i64> {
    book.lock().expect("claim lock").active_requests.clone()
}

pub(crate) fn active_count(book: &Arc<Mutex<ClaimBook>>, account_name: &str) -> i64 {
    book.lock()
        .expect("claim lock")
        .active_requests
        .get(account_name)
        .copied()
        .unwrap_or(0)
}

pub(crate) fn claim_ids(book: &Arc<Mutex<ClaimBook>>) -> usize {
    book.lock().expect("claim lock").claims.len()
}
