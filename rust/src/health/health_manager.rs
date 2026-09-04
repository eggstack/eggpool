//! Account health state and read-only versus mutating routing gates.

use std::{
    collections::{BTreeMap, BTreeSet},
    sync::{Arc, Mutex},
};

use thiserror::Error;

use super::{
    AccountBackoffRecord, BackoffReason, CircuitBreaker, CircuitStats,
    MAX_NONTERMINAL_BACKOFF_SECONDS,
};

#[derive(Debug, Error)]
pub enum HealthManagerError {
    #[error("backoff record for unknown account id {0}")]
    UnknownAccount(i64),
    #[error("model-scoped backoff {reason} for account {account_id} has no model")]
    MissingModel {
        account_id: i64,
        reason: BackoffReason,
    },
    #[error("nonterminal backoff {reason} has no expiry")]
    MissingExpiry { reason: BackoffReason },
}

#[derive(Debug, Clone)]
pub struct AccountHealth {
    pub account_id: Option<i64>,
    pub account_name: String,
    pub is_healthy: bool,
    pub health_state: String,
    pub last_check: f64,
    pub last_success: Option<f64>,
    pub last_failure: Option<f64>,
    pub last_failure_category: Option<BackoffReason>,
    pub consecutive_failures: u32,
    pub consecutive_cooldowns: u32,
    pub disabled_until: Option<f64>,
    pub disabled_reason: String,
    pub cooldown_until: f64,
    pub disabled_models: BTreeMap<String, Option<f64>>,
    pub terminal_models: BTreeSet<String>,
    pub circuit_breaker: CircuitBreaker,
}

#[derive(Debug, Clone, PartialEq)]
pub struct AccountHealthSnapshot {
    pub account_id: Option<i64>,
    pub account_name: String,
    pub is_healthy: bool,
    pub health_state: String,
    pub last_check: f64,
    pub last_success: Option<f64>,
    pub last_failure: Option<f64>,
    pub last_failure_category: Option<BackoffReason>,
    pub consecutive_failures: u32,
    pub consecutive_cooldowns: u32,
    pub disabled_until: Option<f64>,
    pub disabled_reason: String,
    pub cooldown_until: f64,
    pub disabled_models: BTreeMap<String, Option<f64>>,
    pub terminal_models: BTreeSet<String>,
    pub circuit: CircuitStats,
}

impl AccountHealth {
    fn snapshot(&self) -> AccountHealthSnapshot {
        AccountHealthSnapshot {
            account_id: self.account_id,
            account_name: self.account_name.clone(),
            is_healthy: self.is_healthy,
            health_state: self.health_state.clone(),
            last_check: self.last_check,
            last_success: self.last_success,
            last_failure: self.last_failure,
            last_failure_category: self.last_failure_category,
            consecutive_failures: self.consecutive_failures,
            consecutive_cooldowns: self.consecutive_cooldowns,
            disabled_until: self.disabled_until,
            disabled_reason: self.disabled_reason.clone(),
            cooldown_until: self.cooldown_until,
            disabled_models: self.disabled_models.clone(),
            terminal_models: self.terminal_models.clone(),
            circuit: self.circuit_breaker.stats(),
        }
    }

    fn is_disabled(&self, now: f64) -> bool {
        self.disabled_until.is_some_and(|until| now < until) || now < self.cooldown_until
    }

    fn is_model_disabled(&self, model_id: &str, now: f64) -> bool {
        if self.is_disabled(now) {
            return true;
        }
        match self.disabled_models.get(model_id) {
            None => false,
            Some(None) => true,
            Some(Some(until)) => now < *until,
        }
    }
}

/// Process-local health manager. Its map lock is synchronous and no lock is
/// held across async work; the breaker has its own short synchronous lock.
#[derive(Clone)]
pub struct HealthManager {
    accounts: Arc<Mutex<BTreeMap<String, AccountHealth>>>,
    clock: Arc<dyn Fn() -> f64 + Send + Sync>,
}

impl std::fmt::Debug for HealthManager {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("HealthManager")
            .field(
                "account_count",
                &self.accounts.lock().expect("health lock").len(),
            )
            .finish()
    }
}

impl Default for HealthManager {
    fn default() -> Self {
        let start = std::time::Instant::now();
        Self::with_clock(move || start.elapsed().as_secs_f64())
    }
}

impl HealthManager {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn with_clock(clock: impl Fn() -> f64 + Send + Sync + 'static) -> Self {
        Self {
            accounts: Arc::new(Mutex::new(BTreeMap::new())),
            clock: Arc::new(clock),
        }
    }

    pub fn register_account(&self, account_id: i64, account_name: &str) {
        let mut accounts = self.accounts.lock().expect("health lock");
        let clock = Arc::clone(&self.clock);
        accounts
            .entry(account_name.to_owned())
            .or_insert_with(|| AccountHealth {
                account_id: Some(account_id),
                account_name: account_name.to_owned(),
                is_healthy: true,
                health_state: "healthy".to_owned(),
                last_check: self.now(),
                last_success: None,
                last_failure: None,
                last_failure_category: None,
                consecutive_failures: 0,
                consecutive_cooldowns: 0,
                disabled_until: None,
                disabled_reason: String::new(),
                cooldown_until: 0.0,
                disabled_models: BTreeMap::new(),
                terminal_models: BTreeSet::new(),
                circuit_breaker: CircuitBreaker::with_clock(move || clock(), 5, 300.0, 1),
            });
        if let Some(account) = accounts.get_mut(account_name) {
            account.account_id = Some(account_id);
        }
    }

    pub fn accounts(&self) -> Vec<AccountHealthSnapshot> {
        self.accounts
            .lock()
            .expect("health lock")
            .values()
            .map(AccountHealth::snapshot)
            .collect()
    }

    pub fn snapshot(&self, account_name: &str) -> Option<AccountHealthSnapshot> {
        self.accounts
            .lock()
            .expect("health lock")
            .get(account_name)
            .map(AccountHealth::snapshot)
    }

    pub fn get_account_health(&self, account_name: &str) -> Option<AccountHealthSnapshot> {
        self.snapshot(account_name)
    }

    pub fn is_account_healthy(&self, account_name: &str) -> bool {
        self.is_account_healthy_read_only(account_name)
    }

    pub fn is_model_healthy(&self, account_name: &str, model_id: &str) -> bool {
        self.is_model_healthy_read_only(account_name, model_id)
    }

    pub fn get_healthy_accounts(&self, account_names: &[String]) -> Vec<String> {
        account_names
            .iter()
            .filter(|name| self.is_account_healthy(name))
            .cloned()
            .collect()
    }

    pub fn is_account_healthy_read_only(&self, account_name: &str) -> bool {
        let accounts = self.accounts.lock().expect("health lock");
        let Some(account) = accounts.get(account_name) else {
            return true;
        };
        let now = self.now();
        self.account_effectively_healthy(account, now) && account.circuit_breaker.can_request()
    }

    pub fn is_model_healthy_read_only(&self, account_name: &str, model_id: &str) -> bool {
        let accounts = self.accounts.lock().expect("health lock");
        let Some(account) = accounts.get(account_name) else {
            return true;
        };
        let now = self.now();
        self.account_effectively_healthy(account, now)
            && !account.is_model_disabled(model_id, now)
            && account.circuit_breaker.can_request()
    }

    /// Acquire the breaker probe slot after the caller has completed its
    /// read-only candidate checks. This is the only health claim operation.
    pub fn try_acquire_request(&self, account_name: &str, model_id: &str) -> bool {
        let accounts = self.accounts.lock().expect("health lock");
        let Some(account) = accounts.get(account_name) else {
            return false;
        };
        let now = self.now();
        if !self.account_effectively_healthy(account, now)
            || account.is_model_disabled(model_id, now)
        {
            return false;
        }
        account.circuit_breaker.allow_request()
    }

    pub fn release_request(&self, account_name: &str) {
        if let Some(account) = self.accounts.lock().expect("health lock").get(account_name) {
            account.circuit_breaker.release_probe();
        }
    }

    pub fn record_success(&self, account_name: &str, model_id: Option<&str>) {
        let now = self.now();
        let mut accounts = self.accounts.lock().expect("health lock");
        let Some(account) = accounts.get_mut(account_name) else {
            return;
        };
        account.consecutive_failures = 0;
        account.consecutive_cooldowns = 0;
        account.last_check = now;
        account.last_success = Some(now);
        if account.disabled_reason.is_empty() && account.health_state != "authentication_failed" {
            account.is_healthy = true;
            account.health_state = "healthy".to_owned();
            account.cooldown_until = 0.0;
        }
        if let Some(model_id) = model_id {
            if !account.terminal_models.contains(model_id) {
                account.disabled_models.remove(model_id);
            }
        }
        account.circuit_breaker.record_success();
    }

    pub fn record_failure(&self, account_name: &str, reason: BackoffReason) {
        let now = self.now();
        let mut accounts = self.accounts.lock().expect("health lock");
        let Some(account) = accounts.get_mut(account_name) else {
            return;
        };
        account.consecutive_failures = account.consecutive_failures.saturating_add(1);
        account.last_check = now;
        account.last_failure = Some(now);
        account.last_failure_category = Some(reason);
        account.circuit_breaker.record_failure();
        if reason == BackoffReason::AuthenticationFailed {
            account.health_state = reason.to_string();
            account.is_healthy = false;
            account.disabled_reason = reason.to_string();
            account.disabled_until = None;
        }
    }

    pub fn record_cooldown(&self, account_name: &str, reason: BackoffReason, delay: f64) {
        let now = self.now();
        let mut accounts = self.accounts.lock().expect("health lock");
        let Some(account) = accounts.get_mut(account_name) else {
            return;
        };
        let delay = if delay.is_finite() && delay >= 0.0 {
            delay.min(MAX_NONTERMINAL_BACKOFF_SECONDS)
        } else {
            0.0
        };
        account.consecutive_cooldowns = account.consecutive_cooldowns.saturating_add(1);
        account.last_check = now;
        account.last_failure = Some(now);
        account.last_failure_category = Some(reason);
        account.cooldown_until = account.cooldown_until.max(now + delay);
        account.health_state = reason.to_string();
    }

    pub fn disable_account(&self, account_name: &str, reason: &str, duration: Option<f64>) {
        let now = self.now();
        let mut accounts = self.accounts.lock().expect("health lock");
        let Some(account) = accounts.get_mut(account_name) else {
            return;
        };
        account.is_healthy = false;
        account.disabled_reason = reason.to_owned();
        account.disabled_until = duration.map(|value| {
            let value = if value.is_finite() {
                value.max(0.0)
            } else {
                0.0
            };
            now + value.min(MAX_NONTERMINAL_BACKOFF_SECONDS)
        });
        account.health_state = reason.to_owned();
    }

    pub fn enable_account(&self, account_name: &str) {
        let now = self.now();
        let mut accounts = self.accounts.lock().expect("health lock");
        let Some(account) = accounts.get_mut(account_name) else {
            return;
        };
        account.is_healthy = true;
        account.health_state = "healthy".to_owned();
        account.disabled_until = None;
        account.disabled_reason.clear();
        account.cooldown_until = 0.0;
        account.last_check = now;
        account.circuit_breaker.reset();
    }

    pub fn disable_model(
        &self,
        account_name: &str,
        model_id: &str,
        duration: Option<f64>,
        terminal: bool,
    ) {
        let now = self.now();
        let mut accounts = self.accounts.lock().expect("health lock");
        let Some(account) = accounts.get_mut(account_name) else {
            return;
        };
        if terminal || duration.is_none() {
            account.terminal_models.insert(model_id.to_owned());
        } else {
            account.terminal_models.remove(model_id);
        }
        account.disabled_models.insert(
            model_id.to_owned(),
            duration.map(|value| {
                let value = if value.is_finite() {
                    value.max(0.0)
                } else {
                    0.0
                };
                now + value.min(MAX_NONTERMINAL_BACKOFF_SECONDS)
            }),
        );
    }

    pub fn enable_model(&self, account_name: &str, model_id: &str) {
        if let Some(account) = self
            .accounts
            .lock()
            .expect("health lock")
            .get_mut(account_name)
        {
            account.disabled_models.remove(model_id);
            account.terminal_models.remove(model_id);
        }
    }

    pub fn prune_disabled_models(
        &self,
        account_name: &str,
        advertised: &BTreeSet<String>,
    ) -> usize {
        let mut accounts = self.accounts.lock().expect("health lock");
        let Some(account) = accounts.get_mut(account_name) else {
            return 0;
        };
        let stale: Vec<String> = account
            .disabled_models
            .keys()
            .filter(|model| !advertised.contains(*model))
            .cloned()
            .collect();
        for model in &stale {
            account.disabled_models.remove(model);
            account.terminal_models.remove(model);
        }
        stale.len()
    }

    /// Hydrate restart hints using a wall-clock remaining duration and anchor
    /// every resulting deadline in the process-local monotonic domain.
    pub fn hydrate_backoffs(
        &self,
        records: &[AccountBackoffRecord],
        account_names: &BTreeMap<i64, String>,
        wall_now: f64,
    ) -> Result<usize, HealthManagerError> {
        let mut applied = 0;
        for record in records {
            let Some(account_name) = account_names.get(&record.account_id) else {
                return Err(HealthManagerError::UnknownAccount(record.account_id));
            };
            let reason = record.reason;
            if reason == BackoffReason::AuthenticationFailed {
                self.disable_account(account_name, reason.as_str(), None);
                applied += 1;
                continue;
            }
            let Some(deadline) = record.backoff_until_epoch else {
                return Err(HealthManagerError::MissingExpiry { reason });
            };
            let remaining = (deadline - wall_now).max(0.0);
            if remaining == 0.0 {
                continue;
            }
            let remaining = remaining.min(MAX_NONTERMINAL_BACKOFF_SECONDS);
            if reason == BackoffReason::ModelUnavailable {
                let Some(model_id) = record.model_id.as_deref() else {
                    return Err(HealthManagerError::MissingModel {
                        account_id: record.account_id,
                        reason,
                    });
                };
                self.disable_model(account_name, model_id, Some(remaining), false);
            } else {
                self.record_cooldown(account_name, reason, remaining);
            }
            applied += 1;
        }
        Ok(applied)
    }

    fn now(&self) -> f64 {
        (self.clock)()
    }

    fn account_effectively_healthy(&self, account: &AccountHealth, now: f64) -> bool {
        if account.disabled_until.is_some_and(|until| now < until) || now < account.cooldown_until {
            return false;
        }
        let timed_disable_expired = if account.disabled_reason.is_empty() {
            false
        } else if account.health_state == "authentication_failed"
            || account.disabled_until.is_none()
        {
            return false;
        } else {
            true
        };
        timed_disable_expired
            || account.is_healthy
            || matches!(
                account.health_state.as_str(),
                "cooldown" | "quota_exhausted" | "rate_limited" | "operator"
            )
    }
}
