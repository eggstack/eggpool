//! Quota state, persisted usage snapshots, and local ownership mirrors.
//!
//! This module deliberately contains no request parsing, provider I/O, or
//! durable request/reservation lifecycle.  It is the small state boundary
//! shared by the future router and coordinator.

use std::collections::VecDeque;

use serde::{Deserialize, Serialize};
use thiserror::Error;

pub const SQLITE_INTEGER_MAX: i64 = i64::MAX;
pub const DEFAULT_REQUEST_CAPACITY_5H: i64 = 2_500;
pub const DEFAULT_REQUEST_CAPACITY_7D: i64 = 35_000;
pub const DEFAULT_REQUEST_CAPACITY_30D: i64 = 150_000;
pub const DEFAULT_TOKEN_CAPACITY_5H: i64 = 500_000_000;
pub const DEFAULT_TOKEN_CAPACITY_7D: i64 = 7_000_000_000;
pub const DEFAULT_TOKEN_CAPACITY_30D: i64 = 30_000_000_000;

pub const RESERVATION_COST_CEILING_MICRODOLLARS: i64 = 2_500_000;
pub const ESTIMATED_COST_PER_TOKEN_CEILING_MICRODOLLARS: i64 = 100;

fn clamp_non_negative(value: i64) -> i64 {
    value.max(0)
}

fn saturating_add(left: i64, right: i64) -> i64 {
    left.max(0).saturating_add(right.max(0))
}

/// The three persisted routing horizons.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub enum QuotaWindowName {
    FiveHour,
    Weekly,
    Monthly,
}

/// A bounded in-memory rolling window used only when persisted 5h data is not
/// available yet.  Seven- and thirty-day routing data never falls back to it.
#[derive(Debug, Clone, PartialEq)]
pub struct QuotaWindow {
    pub window_seconds: u64,
    pub used_tokens: i64,
    pub used_cost_microdollars: i64,
    observations: VecDeque<(f64, i64, i64)>,
    last_observation_timestamp: Option<f64>,
}

impl QuotaWindow {
    pub fn new(window_seconds: u64) -> Self {
        Self {
            window_seconds,
            used_tokens: 0,
            used_cost_microdollars: 0,
            observations: VecDeque::new(),
            last_observation_timestamp: None,
        }
    }

    pub fn add_observation(&mut self, timestamp: f64, tokens: i64, cost: i64) {
        let observation = (
            timestamp,
            clamp_non_negative(tokens),
            clamp_non_negative(cost),
        );
        if self
            .last_observation_timestamp
            .is_none_or(|last| timestamp >= last)
        {
            self.observations.push_back(observation);
            self.used_tokens = saturating_add(self.used_tokens, observation.1);
            self.used_cost_microdollars =
                saturating_add(self.used_cost_microdollars, observation.2);
            self.last_observation_timestamp = Some(timestamp);
            self.prune(timestamp);
            return;
        }

        self.observations.push_back(observation);
        let mut ordered: Vec<_> = self.observations.drain(..).collect();
        ordered.sort_by(|left, right| left.0.total_cmp(&right.0));
        self.observations = ordered.into_iter().collect();
        self.last_observation_timestamp = Some(
            self.last_observation_timestamp
                .map_or(timestamp, |last| last.max(timestamp)),
        );
        self.rebuild(self.last_observation_timestamp.unwrap_or(timestamp));
    }

    pub fn usage(&mut self, current_time: f64) -> (i64, i64) {
        self.prune(current_time);
        (self.used_tokens, self.used_cost_microdollars)
    }

    fn prune(&mut self, current_time: f64) {
        let cutoff = current_time - self.window_seconds as f64;
        while self
            .observations
            .front()
            .is_some_and(|observation| observation.0 < cutoff)
        {
            if let Some((_timestamp, tokens, cost)) = self.observations.pop_front() {
                self.used_tokens = (self.used_tokens - tokens).max(0);
                self.used_cost_microdollars = (self.used_cost_microdollars - cost).max(0);
            }
        }
    }

    fn rebuild(&mut self, current_time: f64) {
        let cutoff = current_time - self.window_seconds as f64;
        self.observations
            .retain(|observation| observation.0 >= cutoff);
        self.used_tokens = self
            .observations
            .iter()
            .map(|observation| observation.1)
            .fold(0, saturating_add);
        self.used_cost_microdollars = self
            .observations
            .iter()
            .map(|observation| observation.2)
            .fold(0, saturating_add);
    }
}

/// The exact persisted usage shape consumed by routing.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct PersistedWindowSnapshot {
    pub account_id: i64,
    pub cost_5h: i64,
    pub cost_7d: i64,
    pub cost_30d: i64,
    pub request_count_5h: i64,
    pub request_count_7d: i64,
    pub request_count_30d: i64,
    pub token_count_5h: i64,
    pub token_count_7d: i64,
    pub token_count_30d: i64,
    pub loaded_at: f64,
}

impl PersistedWindowSnapshot {
    pub fn empty(account_id: i64, loaded_at: f64) -> Self {
        Self {
            account_id,
            cost_5h: 0,
            cost_7d: 0,
            cost_30d: 0,
            request_count_5h: 0,
            request_count_7d: 0,
            request_count_30d: 0,
            token_count_5h: 0,
            token_count_7d: 0,
            token_count_30d: 0,
            loaded_at,
        }
    }
}

/// A quota policy supplied by configuration or a later routing plan.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct QuotaPolicy {
    pub capacity_5h_requests: Option<i64>,
    pub capacity_7d_requests: Option<i64>,
    pub capacity_30d_requests: Option<i64>,
    pub capacity_5h_tokens: Option<i64>,
    pub capacity_7d_tokens: Option<i64>,
    pub capacity_30d_tokens: Option<i64>,
    pub capacity_5h_microdollars: Option<i64>,
    pub capacity_7d_microdollars: Option<i64>,
    pub capacity_30d_microdollars: Option<i64>,
    pub request_offset_5h: i64,
    pub request_offset_7d: i64,
    pub request_offset_30d: i64,
    pub token_offset_5h: i64,
    pub token_offset_7d: i64,
    pub token_offset_30d: i64,
    pub five_hour_offset: i64,
    pub weekly_offset: i64,
    pub monthly_offset: i64,
}

/// Quota state for one account.  The `reserved_*` fields are diagnostic
/// mirrors of durable reservations plus pending local claims.
#[derive(Debug, Clone, PartialEq)]
pub struct AccountQuota {
    pub account_name: String,
    pub weight: f64,
    pub policy: QuotaPolicy,
    pub persisted_snapshot: Option<PersistedWindowSnapshot>,
    pub hourly_window: QuotaWindow,
    pub daily_window: QuotaWindow,
    pub reserved_cost: i64,
    pub reserved_requests: i64,
    pub reserved_tokens: i64,
}

impl AccountQuota {
    pub fn new(account_name: impl Into<String>) -> Self {
        Self {
            account_name: account_name.into(),
            weight: 1.0,
            policy: QuotaPolicy::default(),
            persisted_snapshot: None,
            hourly_window: QuotaWindow::new(3_600),
            daily_window: QuotaWindow::new(86_400),
            reserved_cost: 0,
            reserved_requests: 0,
            reserved_tokens: 0,
        }
    }

    pub fn get_persisted_cost(&mut self, window: QuotaWindowName, now: f64) -> i64 {
        match self.persisted_snapshot {
            Some(snapshot) => match window {
                QuotaWindowName::FiveHour => snapshot.cost_5h,
                QuotaWindowName::Weekly => snapshot.cost_7d,
                QuotaWindowName::Monthly => snapshot.cost_30d,
            },
            None => match window {
                QuotaWindowName::FiveHour => self.hourly_window.usage(now).1,
                QuotaWindowName::Weekly | QuotaWindowName::Monthly => 0,
            },
        }
    }

    pub fn get_persisted_requests(&self, window: QuotaWindowName) -> i64 {
        self.persisted_snapshot.map_or(0, |snapshot| match window {
            QuotaWindowName::FiveHour => snapshot.request_count_5h,
            QuotaWindowName::Weekly => snapshot.request_count_7d,
            QuotaWindowName::Monthly => snapshot.request_count_30d,
        })
    }

    pub fn get_persisted_tokens(&mut self, window: QuotaWindowName, now: f64) -> i64 {
        match self.persisted_snapshot {
            Some(snapshot) => match window {
                QuotaWindowName::FiveHour => snapshot.token_count_5h,
                QuotaWindowName::Weekly => snapshot.token_count_7d,
                QuotaWindowName::Monthly => snapshot.token_count_30d,
            },
            None => match window {
                QuotaWindowName::FiveHour => self.hourly_window.usage(now).0,
                QuotaWindowName::Weekly | QuotaWindowName::Monthly => 0,
            },
        }
    }

    pub fn request_capacity(&self, window: QuotaWindowName) -> i64 {
        self.policy
            .request_capacity(window)
            .unwrap_or(match window {
                QuotaWindowName::FiveHour => DEFAULT_REQUEST_CAPACITY_5H,
                QuotaWindowName::Weekly => DEFAULT_REQUEST_CAPACITY_7D,
                QuotaWindowName::Monthly => DEFAULT_REQUEST_CAPACITY_30D,
            })
    }

    pub fn token_capacity(&self, window: QuotaWindowName) -> i64 {
        self.policy.token_capacity(window).unwrap_or(match window {
            QuotaWindowName::FiveHour => DEFAULT_TOKEN_CAPACITY_5H,
            QuotaWindowName::Weekly => DEFAULT_TOKEN_CAPACITY_7D,
            QuotaWindowName::Monthly => DEFAULT_TOKEN_CAPACITY_30D,
        })
    }

    pub fn is_within_limits(&mut self, now: f64) -> bool {
        for window in [
            QuotaWindowName::FiveHour,
            QuotaWindowName::Weekly,
            QuotaWindowName::Monthly,
        ] {
            let cost = self
                .get_persisted_cost(window, now)
                .saturating_add(self.policy.cost_offset(window))
                .saturating_add(if window == QuotaWindowName::FiveHour {
                    self.reserved_cost
                } else {
                    0
                });
            let requests = self
                .get_persisted_requests(window)
                .saturating_add(self.policy.request_offset(window))
                .saturating_add(if window == QuotaWindowName::FiveHour {
                    self.reserved_requests
                } else {
                    0
                });
            let tokens = self
                .get_persisted_tokens(window, now)
                .saturating_add(self.policy.token_offset(window))
                .saturating_add(if window == QuotaWindowName::FiveHour {
                    self.reserved_tokens
                } else {
                    0
                });
            if self
                .policy
                .cost_capacity(window)
                .is_some_and(|capacity| cost >= capacity)
                || self
                    .policy
                    .request_capacity(window)
                    .is_some_and(|capacity| requests >= capacity)
                || self
                    .policy
                    .token_capacity(window)
                    .is_some_and(|capacity| tokens >= capacity)
            {
                return false;
            }
        }
        true
    }

    pub fn remaining_capacity(&mut self, now: f64) -> f64 {
        let mut remaining = Vec::new();
        for window in [
            QuotaWindowName::FiveHour,
            QuotaWindowName::Weekly,
            QuotaWindowName::Monthly,
        ] {
            if let Some(capacity) = self.policy.cost_capacity(window) {
                remaining.push(remaining_ratio(
                    self.get_persisted_cost(window, now)
                        .saturating_add(self.policy.cost_offset(window))
                        .saturating_add(if window == QuotaWindowName::FiveHour {
                            self.reserved_cost
                        } else {
                            0
                        }),
                    capacity,
                ));
            }
            if let Some(capacity) = self.policy.request_capacity(window) {
                remaining.push(remaining_ratio(
                    self.get_persisted_requests(window)
                        .saturating_add(self.policy.request_offset(window))
                        .saturating_add(if window == QuotaWindowName::FiveHour {
                            self.reserved_requests
                        } else {
                            0
                        }),
                    capacity,
                ));
            }
            if let Some(capacity) = self.policy.token_capacity(window) {
                remaining.push(remaining_ratio(
                    self.get_persisted_tokens(window, now)
                        .saturating_add(self.policy.token_offset(window))
                        .saturating_add(if window == QuotaWindowName::FiveHour {
                            self.reserved_tokens
                        } else {
                            0
                        }),
                    capacity,
                ));
            }
        }
        remaining.into_iter().reduce(f64::min).unwrap_or(1.0)
    }

    pub(crate) fn utilization(
        &mut self,
        window: QuotaWindowName,
        pending_requests: i64,
        pending_tokens: i64,
        incoming_tokens: i64,
        now: f64,
    ) -> f64 {
        let weight = if self.weight.is_finite() && self.weight > 0.0 {
            self.weight
        } else {
            return f64::INFINITY;
        };
        let request_capacity = self.request_capacity(window) as f64 * weight;
        let token_capacity = self.token_capacity(window) as f64 * weight;
        let requests = self
            .get_persisted_requests(window)
            .saturating_add(pending_requests)
            .saturating_add(1)
            .saturating_add(self.policy.request_offset(window))
            .max(0) as f64;
        let tokens = self
            .get_persisted_tokens(window, now)
            .saturating_add(pending_tokens)
            .saturating_add(incoming_tokens.max(0))
            .saturating_add(self.policy.token_offset(window))
            .max(0) as f64;
        let request_util = if request_capacity > 0.0 {
            requests / request_capacity
        } else {
            f64::INFINITY
        };
        let token_util = if token_capacity > 0.0 {
            tokens / token_capacity
        } else {
            f64::INFINITY
        };
        request_util.max(token_util)
    }

    pub(crate) fn cost_snapshot(&mut self, now: f64) -> (i64, i64, i64) {
        (
            self.get_persisted_cost(QuotaWindowName::FiveHour, now),
            self.get_persisted_cost(QuotaWindowName::Weekly, now),
            self.get_persisted_cost(QuotaWindowName::Monthly, now),
        )
    }
}

impl QuotaPolicy {
    fn request_capacity(self, window: QuotaWindowName) -> Option<i64> {
        match window {
            QuotaWindowName::FiveHour => self.capacity_5h_requests,
            QuotaWindowName::Weekly => self.capacity_7d_requests,
            QuotaWindowName::Monthly => self.capacity_30d_requests,
        }
    }

    fn token_capacity(self, window: QuotaWindowName) -> Option<i64> {
        match window {
            QuotaWindowName::FiveHour => self.capacity_5h_tokens,
            QuotaWindowName::Weekly => self.capacity_7d_tokens,
            QuotaWindowName::Monthly => self.capacity_30d_tokens,
        }
    }

    fn cost_capacity(self, window: QuotaWindowName) -> Option<i64> {
        match window {
            QuotaWindowName::FiveHour => self.capacity_5h_microdollars,
            QuotaWindowName::Weekly => self.capacity_7d_microdollars,
            QuotaWindowName::Monthly => self.capacity_30d_microdollars,
        }
    }

    fn request_offset(self, window: QuotaWindowName) -> i64 {
        match window {
            QuotaWindowName::FiveHour => self.request_offset_5h,
            QuotaWindowName::Weekly => self.request_offset_7d,
            QuotaWindowName::Monthly => self.request_offset_30d,
        }
    }

    fn token_offset(self, window: QuotaWindowName) -> i64 {
        match window {
            QuotaWindowName::FiveHour => self.token_offset_5h,
            QuotaWindowName::Weekly => self.token_offset_7d,
            QuotaWindowName::Monthly => self.token_offset_30d,
        }
    }

    fn cost_offset(self, window: QuotaWindowName) -> i64 {
        match window {
            QuotaWindowName::FiveHour => self.five_hour_offset,
            QuotaWindowName::Weekly => self.weekly_offset,
            QuotaWindowName::Monthly => self.monthly_offset,
        }
    }
}

fn remaining_ratio(used: i64, capacity: i64) -> f64 {
    let ratio = if capacity > 0 {
        used as f64 / capacity as f64
    } else {
        f64::INFINITY
    };
    (1.0 - ratio).max(0.0)
}

/// Errors indicate a broken local ownership invariant, not an upstream
/// failure. Pending claims are never silently clamped during release.
#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum QuotaInvariantError {
    #[error("quota value {field} must be non-negative")]
    NegativeValue { field: &'static str },
    #[error("pending claim ownership underflow for account {account:?}")]
    PendingOwnershipUnderflow { account: String },
    #[error("pending claim cost ownership underflow for account {account:?}")]
    PendingCostUnderflow { account: String },
    #[error("reservation ownership underflow for account {account:?}")]
    ReservationOwnershipUnderflow { account: String },
    #[error("account weight must be finite and greater than zero")]
    InvalidWeight,
}
