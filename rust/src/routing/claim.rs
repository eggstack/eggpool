//! Local selection claims and exact ownership compensation.

use std::{
    collections::{BTreeMap, VecDeque},
    sync::{Arc, Mutex},
};

use thiserror::Error;

use crate::{
    health::HealthManager,
    quota::{QuotaEstimator, QuotaInvariantError},
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

#[derive(Debug, Clone)]
struct OwnedClaim {
    state: ClaimState,
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
        },
    );
    state.terminal_order.push_back(id);
    claim.id = id;
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
