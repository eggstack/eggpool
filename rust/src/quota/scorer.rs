//! Deterministic request/token fair-share scoring.

use std::collections::BTreeMap;

use serde::Serialize;

use super::{QuotaAccountSnapshot, QuotaEstimator, QuotaWindowName};

/// Score constants frozen by D001's Python oracle.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct ScoringPolicy {
    pub mean_weight: f64,
    pub inflight_penalty_per_request: f64,
    pub health_penalty_value: f64,
    pub near_tie_epsilon: f64,
    pub prefer_native: bool,
}

impl Default for ScoringPolicy {
    fn default() -> Self {
        Self {
            mean_weight: 0.15,
            inflight_penalty_per_request: 0.01,
            health_penalty_value: 10.0,
            near_tie_epsilon: 0.01,
            prefer_native: true,
        }
    }
}

/// All score inputs needed by D006 and routing diagnostics.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct RoutingScore {
    pub account_name: String,
    pub quota_score: f64,
    pub weight: f64,
    pub is_eligible: bool,
    pub inflight_penalty: f64,
    pub health_penalty: f64,
    pub reserved_microdollars: i64,
    pub reserved_requests: i64,
    pub reserved_tokens: i64,
    pub cost_5h_microdollars: i64,
    pub cost_7d_microdollars: i64,
    pub cost_30d_microdollars: i64,
    pub request_count_5h: i64,
    pub request_count_7d: i64,
    pub request_count_30d: i64,
    pub token_count_5h: i64,
    pub token_count_7d: i64,
    pub token_count_30d: i64,
    pub capacity_5h_microdollars: i64,
    pub capacity_7d_microdollars: i64,
    pub capacity_30d_microdollars: i64,
    pub capacity_5h_requests: i64,
    pub capacity_7d_requests: i64,
    pub capacity_30d_requests: i64,
    pub capacity_5h_tokens: i64,
    pub capacity_7d_tokens: i64,
    pub capacity_30d_tokens: i64,
    pub active_request_count: i64,
    pub tier: u32,
    pub requires_transcode: bool,
}

impl RoutingScore {
    pub fn final_score(&self) -> f64 {
        if self.is_eligible {
            self.quota_score + self.inflight_penalty + self.health_penalty
        } else {
            f64::INFINITY
        }
    }
}

/// Pure scorer. It copies estimator state once per call and never performs
/// per-account locking or SQLite access.
#[derive(Debug, Clone, Copy, Default)]
pub struct QuotaFairScorer {
    pub policy: ScoringPolicy,
}

impl QuotaFairScorer {
    pub fn new(policy: ScoringPolicy) -> Self {
        Self { policy }
    }

    pub fn score_accounts(
        &self,
        estimator: &QuotaEstimator,
        account_names: &[String],
        active_requests: &BTreeMap<String, i64>,
        projected_tokens: &BTreeMap<String, i64>,
        health_penalties: &BTreeMap<String, f64>,
    ) -> Vec<RoutingScore> {
        let snapshots = estimator.snapshot(account_names);
        let mut scores = Vec::with_capacity(account_names.len());
        for name in account_names {
            let Some(mut snapshot) = snapshots.get(name).cloned() else {
                scores.push(empty_score(name));
                continue;
            };
            scores.push(self.score_one(
                name,
                &mut snapshot,
                *active_requests.get(name).unwrap_or(&0),
                *projected_tokens.get(name).unwrap_or(&0),
                *health_penalties.get(name).unwrap_or(&0.0),
            ));
        }
        scores
    }

    pub fn rank_accounts(&self, mut scores: Vec<RoutingScore>) -> Vec<RoutingScore> {
        if self.policy.prefer_native {
            scores.sort_by(|left, right| {
                left.final_score()
                    .total_cmp(&right.final_score())
                    .then_with(|| native_key(left).cmp(&native_key(right)))
                    .then_with(|| left.account_name.cmp(&right.account_name))
            });
        } else {
            scores.sort_by(|left, right| {
                left.final_score()
                    .total_cmp(&right.final_score())
                    .then_with(|| left.account_name.cmp(&right.account_name))
            });
        }
        scores
    }

    pub fn near_ties<'a>(&self, scores: &'a [RoutingScore]) -> Vec<&'a RoutingScore> {
        let Some(best) = scores
            .iter()
            .filter(|score| score.is_eligible)
            .min_by(|left, right| left.final_score().total_cmp(&right.final_score()))
        else {
            return Vec::new();
        };
        scores
            .iter()
            .filter(|score| {
                score.is_eligible
                    && (score.final_score() - best.final_score()).abs()
                        < self.policy.near_tie_epsilon
                    && score.requires_transcode == best.requires_transcode
            })
            .collect()
    }

    fn score_one(
        &self,
        name: &str,
        snapshot: &mut QuotaAccountSnapshot,
        active_requests: i64,
        projected_tokens: i64,
        health_penalty: f64,
    ) -> RoutingScore {
        let now = snapshot
            .quota
            .persisted_snapshot
            .map_or(0.0, |snapshot| snapshot.loaded_at);
        let p5 = snapshot.quota.utilization(
            QuotaWindowName::FiveHour,
            snapshot
                .reserved_requests
                .saturating_add(snapshot.pending_requests),
            snapshot
                .reserved_tokens
                .saturating_add(snapshot.pending_tokens),
            projected_tokens.max(0),
            now,
        );
        let p7 = snapshot.quota.utilization(
            QuotaWindowName::Weekly,
            snapshot
                .reserved_requests
                .saturating_add(snapshot.pending_requests),
            snapshot
                .reserved_tokens
                .saturating_add(snapshot.pending_tokens),
            projected_tokens.max(0),
            now,
        );
        let p30 = snapshot.quota.utilization(
            QuotaWindowName::Monthly,
            snapshot
                .reserved_requests
                .saturating_add(snapshot.pending_requests),
            snapshot
                .reserved_tokens
                .saturating_add(snapshot.pending_tokens),
            projected_tokens.max(0),
            now,
        );
        let mean = (p5 + p7 + p30) / 3.0;
        let quota_score = p5.max(p7).max(p30) + self.policy.mean_weight * mean;
        let is_eligible = quota_score.is_finite();
        let active_request_count = active_requests.max(0);
        let (cost_5h, cost_7d, cost_30d) = snapshot.quota.cost_snapshot(now);
        let persisted = snapshot.quota.persisted_snapshot;
        RoutingScore {
            account_name: name.to_owned(),
            quota_score,
            weight: snapshot.quota.weight,
            is_eligible,
            inflight_penalty: active_request_count as f64
                * self.policy.inflight_penalty_per_request,
            health_penalty: if health_penalty == 0.0 {
                0.0
            } else {
                health_penalty
            },
            reserved_microdollars: snapshot.reserved_cost.saturating_add(snapshot.pending_cost),
            reserved_requests: snapshot
                .reserved_requests
                .saturating_add(snapshot.pending_requests),
            reserved_tokens: snapshot
                .reserved_tokens
                .saturating_add(snapshot.pending_tokens),
            cost_5h_microdollars: cost_5h,
            cost_7d_microdollars: cost_7d,
            cost_30d_microdollars: cost_30d,
            request_count_5h: persisted.map_or(0, |value| value.request_count_5h),
            request_count_7d: persisted.map_or(0, |value| value.request_count_7d),
            request_count_30d: persisted.map_or(0, |value| value.request_count_30d),
            token_count_5h: persisted.map_or(0, |value| value.token_count_5h),
            token_count_7d: persisted.map_or(0, |value| value.token_count_7d),
            token_count_30d: persisted.map_or(0, |value| value.token_count_30d),
            capacity_5h_microdollars: snapshot.quota.policy.capacity_5h_microdollars.unwrap_or(0),
            capacity_7d_microdollars: snapshot.quota.policy.capacity_7d_microdollars.unwrap_or(0),
            capacity_30d_microdollars: snapshot.quota.policy.capacity_30d_microdollars.unwrap_or(0),
            capacity_5h_requests: snapshot.quota.request_capacity(QuotaWindowName::FiveHour),
            capacity_7d_requests: snapshot.quota.request_capacity(QuotaWindowName::Weekly),
            capacity_30d_requests: snapshot.quota.request_capacity(QuotaWindowName::Monthly),
            capacity_5h_tokens: snapshot.quota.token_capacity(QuotaWindowName::FiveHour),
            capacity_7d_tokens: snapshot.quota.token_capacity(QuotaWindowName::Weekly),
            capacity_30d_tokens: snapshot.quota.token_capacity(QuotaWindowName::Monthly),
            active_request_count,
            tier: 0,
            requires_transcode: false,
        }
    }
}

fn native_key(score: &RoutingScore) -> u8 {
    u8::from(score.requires_transcode)
}

fn empty_score(name: &str) -> RoutingScore {
    RoutingScore {
        account_name: name.to_owned(),
        quota_score: f64::INFINITY,
        weight: 0.0,
        is_eligible: false,
        inflight_penalty: 0.0,
        health_penalty: 0.0,
        reserved_microdollars: 0,
        reserved_requests: 0,
        reserved_tokens: 0,
        cost_5h_microdollars: 0,
        cost_7d_microdollars: 0,
        cost_30d_microdollars: 0,
        request_count_5h: 0,
        request_count_7d: 0,
        request_count_30d: 0,
        token_count_5h: 0,
        token_count_7d: 0,
        token_count_30d: 0,
        capacity_5h_microdollars: 0,
        capacity_7d_microdollars: 0,
        capacity_30d_microdollars: 0,
        capacity_5h_requests: 0,
        capacity_7d_requests: 0,
        capacity_30d_requests: 0,
        capacity_5h_tokens: 0,
        capacity_7d_tokens: 0,
        capacity_30d_tokens: 0,
        active_request_count: 0,
        tier: 0,
        requires_transcode: false,
    }
}
