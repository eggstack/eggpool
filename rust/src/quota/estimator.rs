//! Bounded estimates and the estimator-owned quota mirrors.

use std::collections::{BTreeMap, VecDeque};

use crate::db::repositories::UsageWindowSnapshot;

use super::state::{
    AccountQuota, ESTIMATED_COST_PER_TOKEN_CEILING_MICRODOLLARS, PersistedWindowSnapshot,
    QuotaInvariantError, QuotaPolicy, RESERVATION_COST_CEILING_MICRODOLLARS, SQLITE_INTEGER_MAX,
};

pub const EWMA_HARD_CAP: usize = 4_096;
pub const GLOBAL_EWMA_HARD_CAP: usize = 1_024;

const MODEL_FAMILY_FALLBACKS: &[(&str, (f64, f64))] = &[
    ("claude-3.5-sonnet", (3.0, 15.0)),
    ("gpt-4o-mini", (0.15, 0.6)),
    ("gpt-3.5-turbo", (0.5, 1.5)),
    ("claude-3-haiku", (0.25, 1.25)),
    ("claude-3-sonnet", (3.0, 15.0)),
    ("claude-3-opus", (15.0, 75.0)),
    ("gpt-4o", (2.5, 10.0)),
    ("gpt-4", (30.0, 60.0)),
];

const GLOBAL_FALLBACK_MICRODOLLARS_PER_TOKEN: f64 = 3.0;
const GLOBAL_FALLBACK_FLOOR_MICRODOLLARS_PER_TOKEN: f64 = 0.5;

/// Bounded exponentially weighted moving average.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct EwmaEstimate {
    pub alpha: f64,
    pub estimate_cost_per_token: f64,
    pub sample_count: u64,
    pub last_updated: f64,
}

impl Default for EwmaEstimate {
    fn default() -> Self {
        Self {
            alpha: 0.2,
            estimate_cost_per_token: 0.0,
            sample_count: 0,
            last_updated: 0.0,
        }
    }
}

impl EwmaEstimate {
    pub fn update(&mut self, observed_cost_per_token: f64, now: f64) {
        if self.sample_count == 0 {
            self.estimate_cost_per_token = observed_cost_per_token;
        } else {
            self.estimate_cost_per_token = self.alpha * observed_cost_per_token
                + (1.0 - self.alpha) * self.estimate_cost_per_token;
        }
        self.sample_count = self.sample_count.saturating_add(1);
        self.last_updated = now;
    }
}

#[derive(Debug, Clone, Default)]
struct EstimatorState {
    accounts: BTreeMap<String, AccountQuota>,
    account_model_ewma: BTreeMap<String, BTreeMap<String, EwmaEstimate>>,
    account_lru: VecDeque<String>,
    model_lru: BTreeMap<String, VecDeque<String>>,
    global_model_ewma: BTreeMap<String, EwmaEstimate>,
    global_lru: VecDeque<String>,
    global_outlier_counts: BTreeMap<String, u8>,
    global_outlier_lru: VecDeque<String>,
    model_overrides: BTreeMap<String, (f64, f64)>,
    account_model_overrides: BTreeMap<String, BTreeMap<String, (f64, f64)>>,
    reserved_cost: BTreeMap<String, i64>,
    reserved_requests: BTreeMap<String, i64>,
    reserved_tokens: BTreeMap<String, i64>,
    pending_cost: BTreeMap<String, i64>,
    pending_requests: BTreeMap<String, i64>,
    pending_tokens: BTreeMap<String, i64>,
}

/// A lock-free copy of the small aggregate needed by the scorer.
#[derive(Debug, Clone, PartialEq)]
pub struct QuotaAccountSnapshot {
    pub quota: AccountQuota,
    pub reserved_cost: i64,
    pub reserved_requests: i64,
    pub reserved_tokens: i64,
    pub pending_cost: i64,
    pub pending_requests: i64,
    pub pending_tokens: i64,
}

/// Estimator and local ownership state. The mutex is never held by scoring
/// while database or network I/O occurs.
#[derive(Debug, Clone)]
pub struct QuotaEstimator {
    state: std::sync::Arc<std::sync::Mutex<EstimatorState>>,
    pub ewma_hard_cap: usize,
    pub global_ewma_hard_cap: usize,
    pub default_safety_factor: f64,
    pub default_unknown_reservation_microdollars: i64,
    pub ewma_outlier_max_ratio: f64,
}

impl Default for QuotaEstimator {
    fn default() -> Self {
        Self {
            state: std::sync::Arc::new(std::sync::Mutex::new(EstimatorState::default())),
            ewma_hard_cap: EWMA_HARD_CAP,
            global_ewma_hard_cap: GLOBAL_EWMA_HARD_CAP,
            default_safety_factor: 1.15,
            default_unknown_reservation_microdollars: 1_000_000,
            ewma_outlier_max_ratio: 100.0,
        }
    }
}

impl QuotaEstimator {
    pub fn new(accounts: impl IntoIterator<Item = AccountQuota>) -> Self {
        let estimator = Self {
            state: std::sync::Arc::new(std::sync::Mutex::new(EstimatorState::default())),
            ewma_hard_cap: EWMA_HARD_CAP,
            global_ewma_hard_cap: GLOBAL_EWMA_HARD_CAP,
            default_safety_factor: 1.15,
            default_unknown_reservation_microdollars: 1_000_000,
            ewma_outlier_max_ratio: 100.0,
        };
        {
            let mut state = estimator.lock();
            for account in accounts {
                let name = account.account_name.clone();
                state
                    .reserved_cost
                    .insert(name.clone(), account.reserved_cost);
                state
                    .reserved_requests
                    .insert(name.clone(), account.reserved_requests);
                state
                    .reserved_tokens
                    .insert(name.clone(), account.reserved_tokens);
                state.accounts.insert(name, account);
            }
        }
        estimator
    }

    pub fn add_account(&self, account: AccountQuota) {
        let mut state = self.lock();
        let name = account.account_name.clone();
        state
            .reserved_cost
            .insert(name.clone(), account.reserved_cost);
        state
            .reserved_requests
            .insert(name.clone(), account.reserved_requests);
        state
            .reserved_tokens
            .insert(name.clone(), account.reserved_tokens);
        state.accounts.insert(name, account);
    }

    pub fn account_names(&self) -> Vec<String> {
        self.lock().accounts.keys().cloned().collect()
    }

    pub fn ewma_sizes(&self) -> (usize, usize, usize) {
        let state = self.lock();
        (
            state.account_model_ewma.len(),
            state
                .account_model_ewma
                .values()
                .map(BTreeMap::len)
                .max()
                .unwrap_or(0),
            state.global_model_ewma.len(),
        )
    }

    pub fn get_account_quota(&self, account_name: &str) -> Option<AccountQuota> {
        self.lock().accounts.get(account_name).cloned()
    }

    pub fn set_persisted_snapshot(&self, account_name: &str, snapshot: PersistedWindowSnapshot) {
        self.ensure_account(account_name);
        self.lock()
            .accounts
            .get_mut(account_name)
            .expect("account inserted")
            .persisted_snapshot = Some(snapshot);
    }

    /// Apply a single batched repository result to the in-memory accounts.
    pub fn hydrate_usage_windows(
        &self,
        snapshots: &BTreeMap<i64, UsageWindowSnapshot>,
        account_ids: &BTreeMap<String, i64>,
        loaded_at: f64,
    ) {
        let mut state = self.lock();
        for (name, account_id) in account_ids {
            let snapshot = snapshots.get(account_id).copied().unwrap_or_default();
            state
                .accounts
                .entry(name.clone())
                .or_insert_with(|| AccountQuota::new(name))
                .persisted_snapshot = Some(PersistedWindowSnapshot {
                account_id: *account_id,
                cost_5h: snapshot.cost_5h,
                cost_7d: snapshot.cost_7d,
                cost_30d: snapshot.cost_30d,
                request_count_5h: snapshot.request_count_5h,
                request_count_7d: snapshot.request_count_7d,
                request_count_30d: snapshot.request_count_30d,
                token_count_5h: snapshot.token_count_5h,
                token_count_7d: snapshot.token_count_7d,
                token_count_30d: snapshot.token_count_30d,
                loaded_at,
            });
        }
    }

    pub fn configure_policy(
        &self,
        account_name: &str,
        weight: f64,
        policy: QuotaPolicy,
    ) -> Result<(), QuotaInvariantError> {
        if !weight.is_finite() || weight <= 0.0 {
            return Err(QuotaInvariantError::InvalidWeight);
        }
        self.ensure_account(account_name);
        let mut state = self.lock();
        let quota = state
            .accounts
            .get_mut(account_name)
            .expect("account inserted");
        quota.weight = weight;
        quota.policy = policy;
        Ok(())
    }

    pub fn record_usage(
        &self,
        account_name: &str,
        tokens: i64,
        cost_microdollars: i64,
        model_id: Option<&str>,
        _now: f64,
    ) {
        self.ensure_account(account_name);
        let mut state = self.lock();
        let quota = state
            .accounts
            .get_mut(account_name)
            .expect("account inserted");
        quota
            .hourly_window
            .add_observation(_now, tokens, cost_microdollars);
        quota
            .daily_window
            .add_observation(_now, tokens, cost_microdollars);
        if let (Some(model_id), true) = (
            model_id.filter(|id| !id.is_empty()),
            tokens > 0 && cost_microdollars > 0,
        ) {
            let rate = cost_microdollars as f64 / tokens as f64;
            if rate <= ESTIMATED_COST_PER_TOKEN_CEILING_MICRODOLLARS as f64 {
                if !is_global_outlier(&state, model_id, rate, self.ewma_outlier_max_ratio) {
                    record_account_ewma(
                        &mut state,
                        account_name,
                        model_id,
                        rate,
                        _now,
                        self.ewma_hard_cap,
                    );
                }
                record_global_ewma(
                    &mut state,
                    model_id,
                    rate,
                    _now,
                    self.global_ewma_hard_cap,
                    self.ewma_outlier_max_ratio,
                );
            }
        }
    }

    pub fn estimate_cost(
        &self,
        account_name: &str,
        model_id: &str,
        estimated_tokens: i64,
        _now: f64,
    ) -> i64 {
        if estimated_tokens <= 0 {
            return 0;
        }
        let state = self.lock();
        let raw_rate = state
            .account_model_ewma
            .get(account_name)
            .and_then(|models| models.get(model_id))
            .filter(|estimate| estimate.sample_count >= 5)
            .map(|estimate| estimate.estimate_cost_per_token)
            .or_else(|| {
                state
                    .global_model_ewma
                    .get(model_id)
                    .filter(|estimate| estimate.sample_count >= 5)
                    .map(|estimate| estimate.estimate_cost_per_token)
            })
            .or_else(|| {
                state
                    .account_model_overrides
                    .get(account_name)
                    .and_then(|models| models.get(model_id))
                    .copied()
                    .or_else(|| state.model_overrides.get(model_id).copied())
                    .map(|(input, output)| (input + output) / 2.0)
            })
            .or_else(|| family_rate(model_id))
            .unwrap_or_else(|| {
                GLOBAL_FALLBACK_MICRODOLLARS_PER_TOKEN
                    .max(GLOBAL_FALLBACK_FLOOR_MICRODOLLARS_PER_TOKEN)
            });
        finalize_estimate(estimated_tokens, raw_rate, self.default_safety_factor)
    }

    pub fn set_model_override(&self, model_id: &str, input_rate: f64, output_rate: f64) {
        self.lock()
            .model_overrides
            .insert(model_id.to_owned(), (input_rate, output_rate));
    }

    pub fn set_account_model_override(
        &self,
        account_name: &str,
        model_id: &str,
        input_rate: f64,
        output_rate: f64,
    ) {
        self.lock()
            .account_model_overrides
            .entry(account_name.to_owned())
            .or_default()
            .insert(model_id.to_owned(), (input_rate, output_rate));
    }

    pub fn add_pending_claim(
        &self,
        account_name: &str,
        tokens: i64,
        cost: i64,
    ) -> Result<(), QuotaInvariantError> {
        require_non_negative(tokens, "pending tokens")?;
        require_non_negative(cost, "pending cost")?;
        self.ensure_account(account_name);
        let mut state = self.lock();
        *state
            .pending_requests
            .entry(account_name.to_owned())
            .or_default() =
            saturating_add(*state.pending_requests.get(account_name).unwrap_or(&0), 1);
        *state
            .pending_tokens
            .entry(account_name.to_owned())
            .or_default() = saturating_add(
            *state.pending_tokens.get(account_name).unwrap_or(&0),
            tokens,
        );
        *state
            .pending_cost
            .entry(account_name.to_owned())
            .or_default() =
            saturating_add(*state.pending_cost.get(account_name).unwrap_or(&0), cost);
        sync_mirrors(&mut state, account_name);
        Ok(())
    }

    pub fn release_pending_claim(
        &self,
        account_name: &str,
        tokens: i64,
        cost: i64,
    ) -> Result<(), QuotaInvariantError> {
        require_non_negative(tokens, "pending tokens")?;
        require_non_negative(cost, "pending cost")?;
        let mut state = self.lock();
        let requests = *state.pending_requests.get(account_name).unwrap_or(&0);
        let current_tokens = *state.pending_tokens.get(account_name).unwrap_or(&0);
        let current_cost = *state.pending_cost.get(account_name).unwrap_or(&0);
        if requests < 1 || current_tokens < tokens {
            return Err(QuotaInvariantError::PendingOwnershipUnderflow {
                account: account_name.to_owned(),
            });
        }
        if current_cost < cost {
            return Err(QuotaInvariantError::PendingCostUnderflow {
                account: account_name.to_owned(),
            });
        }
        state
            .pending_requests
            .insert(account_name.to_owned(), requests - 1);
        state
            .pending_tokens
            .insert(account_name.to_owned(), current_tokens - tokens);
        state
            .pending_cost
            .insert(account_name.to_owned(), current_cost - cost);
        sync_mirrors(&mut state, account_name);
        Ok(())
    }

    pub fn convert_pending_claim(
        &self,
        account_name: &str,
        tokens: i64,
        cost: i64,
    ) -> Result<(), QuotaInvariantError> {
        require_non_negative(tokens, "pending tokens")?;
        require_non_negative(cost, "pending cost")?;
        let mut state = self.lock();
        let requests = *state.pending_requests.get(account_name).unwrap_or(&0);
        let current_tokens = *state.pending_tokens.get(account_name).unwrap_or(&0);
        let current_cost = *state.pending_cost.get(account_name).unwrap_or(&0);
        if requests < 1 || current_tokens < tokens {
            return Err(QuotaInvariantError::PendingOwnershipUnderflow {
                account: account_name.to_owned(),
            });
        }
        if current_cost < cost {
            return Err(QuotaInvariantError::PendingCostUnderflow {
                account: account_name.to_owned(),
            });
        }
        state
            .pending_requests
            .insert(account_name.to_owned(), requests - 1);
        state
            .pending_tokens
            .insert(account_name.to_owned(), current_tokens - tokens);
        state
            .pending_cost
            .insert(account_name.to_owned(), current_cost - cost);
        *state
            .reserved_requests
            .entry(account_name.to_owned())
            .or_default() =
            saturating_add(*state.reserved_requests.get(account_name).unwrap_or(&0), 1);
        *state
            .reserved_tokens
            .entry(account_name.to_owned())
            .or_default() = saturating_add(
            *state.reserved_tokens.get(account_name).unwrap_or(&0),
            tokens,
        );
        *state
            .reserved_cost
            .entry(account_name.to_owned())
            .or_default() =
            saturating_add(*state.reserved_cost.get(account_name).unwrap_or(&0), cost);
        sync_mirrors(&mut state, account_name);
        Ok(())
    }

    /// Durable mirror removal intentionally clamps at zero. Pending claim
    /// release above is the ownership-checking operation.
    pub fn add_reservation(
        &self,
        account_name: &str,
        requests: i64,
        tokens: i64,
        cost: i64,
    ) -> Result<(), QuotaInvariantError> {
        require_non_negative(requests, "reservation requests")?;
        require_non_negative(tokens, "reservation tokens")?;
        require_non_negative(cost, "reservation cost")?;
        self.ensure_account(account_name);
        let mut state = self.lock();
        add_counter(&mut state.reserved_requests, account_name, requests);
        add_counter(&mut state.reserved_tokens, account_name, tokens);
        add_counter(&mut state.reserved_cost, account_name, cost);
        sync_mirrors(&mut state, account_name);
        Ok(())
    }

    pub fn remove_reservation(
        &self,
        account_name: &str,
        requests: i64,
        tokens: i64,
        cost: i64,
    ) -> Result<(), QuotaInvariantError> {
        require_non_negative(requests, "reservation requests")?;
        require_non_negative(tokens, "reservation tokens")?;
        require_non_negative(cost, "reservation cost")?;
        let mut state = self.lock();
        subtract_counter(&mut state.reserved_requests, account_name, requests);
        subtract_counter(&mut state.reserved_tokens, account_name, tokens);
        subtract_counter(&mut state.reserved_cost, account_name, cost);
        sync_mirrors(&mut state, account_name);
        Ok(())
    }

    pub fn snapshot(&self, account_names: &[String]) -> BTreeMap<String, QuotaAccountSnapshot> {
        let mut state = self.lock();
        let mut snapshots = BTreeMap::new();
        for name in account_names {
            sync_mirrors(&mut state, name);
            if let Some(quota) = state.accounts.get(name).cloned() {
                snapshots.insert(
                    name.clone(),
                    QuotaAccountSnapshot {
                        quota,
                        reserved_cost: *state.reserved_cost.get(name).unwrap_or(&0),
                        reserved_requests: *state.reserved_requests.get(name).unwrap_or(&0),
                        reserved_tokens: *state.reserved_tokens.get(name).unwrap_or(&0),
                        pending_cost: *state.pending_cost.get(name).unwrap_or(&0),
                        pending_requests: *state.pending_requests.get(name).unwrap_or(&0),
                        pending_tokens: *state.pending_tokens.get(name).unwrap_or(&0),
                    },
                );
            }
        }
        snapshots
    }

    fn ensure_account(&self, account_name: &str) {
        let mut state = self.lock();
        state
            .accounts
            .entry(account_name.to_owned())
            .or_insert_with(|| AccountQuota::new(account_name));
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, EstimatorState> {
        self.state
            .lock()
            .expect("quota state mutex is not poisoned")
    }
}

fn require_non_negative(value: i64, field: &'static str) -> Result<(), QuotaInvariantError> {
    if value < 0 {
        Err(QuotaInvariantError::NegativeValue { field })
    } else {
        Ok(())
    }
}

fn saturating_add(left: i64, right: i64) -> i64 {
    left.max(0).saturating_add(right.max(0))
}

fn add_counter(map: &mut BTreeMap<String, i64>, name: &str, value: i64) {
    let current = *map.get(name).unwrap_or(&0);
    map.insert(name.to_owned(), saturating_add(current, value));
}

fn subtract_counter(map: &mut BTreeMap<String, i64>, name: &str, value: i64) {
    let current = *map.get(name).unwrap_or(&0);
    map.insert(name.to_owned(), (current - value).max(0));
}

fn sync_mirrors(state: &mut EstimatorState, name: &str) {
    let cost = saturating_add(
        *state.reserved_cost.get(name).unwrap_or(&0),
        *state.pending_cost.get(name).unwrap_or(&0),
    );
    let requests = saturating_add(
        *state.reserved_requests.get(name).unwrap_or(&0),
        *state.pending_requests.get(name).unwrap_or(&0),
    );
    let tokens = saturating_add(
        *state.reserved_tokens.get(name).unwrap_or(&0),
        *state.pending_tokens.get(name).unwrap_or(&0),
    );
    if let Some(quota) = state.accounts.get_mut(name) {
        quota.reserved_cost = cost;
        quota.reserved_requests = requests;
        quota.reserved_tokens = tokens;
    }
}

fn touch(queue: &mut VecDeque<String>, key: &str) {
    if let Some(position) = queue.iter().position(|item| item == key) {
        queue.remove(position);
    }
    queue.push_back(key.to_owned());
}

fn record_account_ewma(
    state: &mut EstimatorState,
    account: &str,
    model: &str,
    rate: f64,
    now: f64,
    cap: usize,
) {
    let bucket = state
        .account_model_ewma
        .entry(account.to_owned())
        .or_default();
    let models = state.model_lru.entry(account.to_owned()).or_default();
    if !bucket.contains_key(model) && bucket.len() >= cap {
        if let Some(oldest) = models.pop_front() {
            bucket.remove(&oldest);
        }
    }
    let estimate = bucket.entry(model.to_owned()).or_default();
    estimate.update(rate, now);
    touch(models, model);
    touch(&mut state.account_lru, account);
    while state.account_model_ewma.len() > cap {
        if let Some(oldest) = state.account_lru.pop_front() {
            state.account_model_ewma.remove(&oldest);
            state.model_lru.remove(&oldest);
        } else {
            break;
        }
    }
}

fn record_global_ewma(
    state: &mut EstimatorState,
    model: &str,
    rate: f64,
    now: f64,
    cap: usize,
    outlier_max_ratio: f64,
) {
    if is_global_outlier(state, model, rate, outlier_max_ratio) {
        let count = state
            .global_outlier_counts
            .entry(model.to_owned())
            .or_default();
        *count = count.saturating_add(1);
        let count = *count;
        touch(&mut state.global_outlier_lru, model);
        while state.global_outlier_counts.len() > cap {
            if let Some(oldest) = state.global_outlier_lru.pop_front() {
                state.global_outlier_counts.remove(&oldest);
            } else {
                break;
            }
        }
        if count < 3 {
            return;
        }
        state.global_outlier_counts.remove(model);
        if let Some(position) = state
            .global_outlier_lru
            .iter()
            .position(|entry| entry == model)
        {
            state.global_outlier_lru.remove(position);
        }
        state
            .global_model_ewma
            .insert(model.to_owned(), EwmaEstimate::default());
    } else {
        state.global_outlier_counts.remove(model);
        if let Some(position) = state
            .global_outlier_lru
            .iter()
            .position(|entry| entry == model)
        {
            state.global_outlier_lru.remove(position);
        }
    }
    if !state.global_model_ewma.contains_key(model) && state.global_model_ewma.len() >= cap {
        if let Some(oldest) = state.global_lru.pop_front() {
            state.global_model_ewma.remove(&oldest);
        }
    }
    state
        .global_model_ewma
        .entry(model.to_owned())
        .or_default()
        .update(rate, now);
    touch(&mut state.global_lru, model);
}

fn is_global_outlier(
    state: &EstimatorState,
    model: &str,
    rate: f64,
    outlier_max_ratio: f64,
) -> bool {
    let ratio_limit = if outlier_max_ratio.is_finite() && outlier_max_ratio > 1.0 {
        outlier_max_ratio
    } else {
        100.0
    };
    state.global_model_ewma.get(model).is_some_and(|estimate| {
        estimate.sample_count > 0
            && estimate.estimate_cost_per_token > 0.0
            && (rate / estimate.estimate_cost_per_token > ratio_limit
                || rate / estimate.estimate_cost_per_token < 1.0 / ratio_limit)
    })
}

fn family_rate(model: &str) -> Option<f64> {
    let lower = model.to_ascii_lowercase();
    MODEL_FAMILY_FALLBACKS
        .iter()
        .filter(|(family, _)| lower.contains(family))
        .max_by_key(|(family, _)| family.len())
        .map(|(_, (input, output))| (input + output) / 2.0)
}

fn finalize_estimate(tokens: i64, rate: f64, safety_factor: f64) -> i64 {
    if !rate.is_finite() || rate <= 0.0 {
        return 1;
    }
    let per_token = rate.min(ESTIMATED_COST_PER_TOKEN_CEILING_MICRODOLLARS as f64);
    let raw = (tokens as f64 * per_token * safety_factor).floor();
    if !raw.is_finite() || raw <= 0.0 {
        return 1;
    }
    raw.min(RESERVATION_COST_CEILING_MICRODOLLARS as f64)
        .min(SQLITE_INTEGER_MAX as f64) as i64
}
