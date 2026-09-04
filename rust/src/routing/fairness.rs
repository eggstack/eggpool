//! Bounded, request-independent fairness for same-tier routing candidates.

use std::{
    collections::{HashMap, VecDeque},
    sync::{Arc, Mutex},
};

use serde::Serialize;

pub const FAIRNESS_KEY_HARD_CAP: usize = 4_096;

/// The dimensions that partition the round-robin rotor.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize)]
pub struct FairnessKey {
    pub provider_id: Option<String>,
    pub model_id: String,
    pub protocol: Option<String>,
    pub priority: u32,
    pub client_protocol: Option<String>,
}

impl FairnessKey {
    pub fn to_key_string(&self) -> String {
        let mut key = format!(
            "provider={}|model={}|protocol={}|tier={}",
            self.provider_id.as_deref().unwrap_or("*"),
            self.model_id,
            self.protocol.as_deref().unwrap_or("*"),
            self.priority
        );
        if let Some(client_protocol) = &self.client_protocol {
            key.push_str("|client_protocol=");
            key.push_str(client_protocol);
        }
        key
    }
}

/// Explain whether a fairness band changed the score order.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct FairnessDecision {
    pub mode: String,
    pub applied: bool,
    pub key: String,
    pub scope: String,
    pub candidate_count: usize,
    pub anchor_score: Option<f64>,
    pub selected_index: Option<usize>,
    pub selected_account_name: Option<String>,
    pub reason: String,
    pub ordered_accounts: Vec<String>,
}

impl FairnessDecision {
    pub fn not_applied(
        mode: &str,
        key: &FairnessKey,
        scope: &str,
        candidate_count: usize,
        anchor_score: Option<f64>,
        reason: &str,
    ) -> Self {
        Self {
            mode: mode.into(),
            applied: false,
            key: key.to_key_string(),
            scope: scope.into(),
            candidate_count,
            anchor_score,
            selected_index: None,
            selected_account_name: None,
            reason: reason.into(),
            ordered_accounts: Vec::new(),
        }
    }
}

#[derive(Debug, Default)]
struct RotorState {
    positions: HashMap<String, usize>,
    lru: VecDeque<String>,
}

/// Preview/commit fairness state. Preview is side-effect free; commit is used
/// only after a claim has acquired a candidate, which keeps readiness and
/// plan-building reads from advancing the rotor.
#[derive(Debug, Clone, Default)]
pub struct FairnessRotor {
    state: Arc<Mutex<RotorState>>,
}

impl FairnessRotor {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn key_count(&self) -> usize {
        self.state.lock().expect("fairness lock").positions.len()
    }

    pub fn preview<T>(&self, key: &FairnessKey, candidates: &[T]) -> (Vec<usize>, usize) {
        let mut indexes: Vec<usize> = (0..candidates.len()).collect();
        let position = self
            .state
            .lock()
            .expect("fairness lock")
            .positions
            .get(&key.to_key_string())
            .copied()
            .unwrap_or(0);
        if !indexes.is_empty() {
            let length = indexes.len();
            indexes.rotate_left(position % length);
        }
        (indexes, position)
    }

    /// Preview a list whose items expose an account name. Account names are
    /// sorted before rotation, making map/config insertion order irrelevant.
    pub fn preview_named<T, F>(
        &self,
        key: &FairnessKey,
        candidates: &[T],
        account_name: F,
    ) -> (Vec<usize>, usize)
    where
        F: Fn(&T) -> &str,
    {
        let mut indexes: Vec<usize> = (0..candidates.len()).collect();
        indexes.sort_by(|left, right| {
            account_name(&candidates[*left]).cmp(account_name(&candidates[*right]))
        });
        let position = self
            .state
            .lock()
            .expect("fairness lock")
            .positions
            .get(&key.to_key_string())
            .copied()
            .unwrap_or(0);
        if !indexes.is_empty() {
            let length = indexes.len();
            indexes.rotate_left(position % length);
        }
        (indexes, position)
    }

    pub fn commit(&self, key: &FairnessKey, candidate_count: usize) {
        if candidate_count < 2 {
            return;
        }
        let key = key.to_key_string();
        let mut state = self.state.lock().expect("fairness lock");
        if state.positions.contains_key(&key) {
            state.lru.retain(|item| item != &key);
        } else if state.positions.len() >= FAIRNESS_KEY_HARD_CAP {
            if let Some(oldest) = state.lru.pop_front() {
                state.positions.remove(&oldest);
            }
        }
        let position = state.positions.get(&key).copied().unwrap_or(0);
        state.positions.insert(key.clone(), position + 1);
        state.lru.push_back(key);
    }

    pub fn order_named<T, F>(
        &self,
        key: &FairnessKey,
        candidates: &[T],
        account_name: F,
    ) -> Vec<usize>
    where
        F: Fn(&T) -> &str,
    {
        self.preview_named(key, candidates, account_name).0
    }
}

/// Randomness is injected so differential tests do not depend on process RNG.
pub trait FairnessRandom: Send + Sync {
    fn choose_index(&self, candidate_count: usize) -> usize;
}

#[derive(Debug, Default)]
pub struct DeterministicFairnessRandom {
    next: std::sync::atomic::AtomicU64,
}

impl FairnessRandom for DeterministicFairnessRandom {
    fn choose_index(&self, candidate_count: usize) -> usize {
        if candidate_count == 0 {
            return 0;
        }
        let value = self.next.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        (value as usize) % candidate_count
    }
}
