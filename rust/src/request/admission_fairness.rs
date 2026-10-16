//! Bounded process-local short-window inference admission fairness.
//!
//! The limiter keys principals by the authenticated user id, so credentials
//! owned by one user share one bucket. It is consulted before the inference
//! body is collected and never touches durable budget state or provider
//! routing. State is intentionally ephemeral across process restarts.

use std::{
    collections::HashMap,
    sync::{Arc, Mutex, MutexGuard},
    time::{Duration, Instant},
};

const WINDOW: Duration = Duration::from_secs(60);
const STALE_AFTER: Duration = Duration::from_secs(120);
const LEGACY_PRINCIPAL: &str = "\0legacy-shared-key";

/// Validated policy projected from the active runtime generation.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub(crate) struct AdmissionFairnessPolicy {
    pub(crate) requests_per_minute_per_principal: Option<u32>,
    pub(crate) global_requests_per_minute: Option<u32>,
    pub(crate) max_tracked_principals: usize,
}

impl AdmissionFairnessPolicy {
    fn is_enabled(self) -> bool {
        self.requests_per_minute_per_principal.is_some()
            || self.global_requests_per_minute.is_some()
    }
}

/// Result of one authenticated admission decision.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum AdmissionFairnessDecision {
    Disabled,
    Allowed {
        remaining: u32,
        scope: &'static str,
    },
    Limited {
        retry_after_seconds: u64,
        scope: &'static str,
    },
}

#[derive(Clone, Default)]
pub(crate) struct AdmissionFairnessLimiter {
    inner: Arc<Mutex<LimiterState>>,
}

#[derive(Default)]
struct LimiterState {
    principals: HashMap<String, Bucket>,
    global: Option<Bucket>,
}

#[derive(Clone, Copy)]
struct Bucket {
    tokens: f64,
    updated_at: Instant,
    last_seen: Instant,
}

impl Bucket {
    fn full(capacity: u32, now: Instant) -> Self {
        Self {
            tokens: f64::from(capacity),
            updated_at: now,
            last_seen: now,
        }
    }

    fn refill(&mut self, capacity: u32, now: Instant) {
        let elapsed = now.saturating_duration_since(self.updated_at).as_secs_f64();
        let refill_per_second = f64::from(capacity) / WINDOW.as_secs_f64();
        self.tokens = (self.tokens + elapsed * refill_per_second).min(f64::from(capacity));
        self.updated_at = now;
        self.last_seen = now;
    }

    fn retry_after(self, capacity: u32) -> u64 {
        let refill_per_second = f64::from(capacity) / WINDOW.as_secs_f64();
        let wait = ((1.0 - self.tokens).max(0.0) / refill_per_second).ceil();
        wait.max(1.0) as u64
    }
}

impl AdmissionFairnessLimiter {
    /// Admit one request using a supplied monotonic instant. The clock seam
    /// makes window, simultaneous-slot, and stale-eviction behavior testable.
    pub(crate) fn admit_at(
        &self,
        policy: AdmissionFairnessPolicy,
        principal_id: Option<&str>,
        now: Instant,
    ) -> AdmissionFairnessDecision {
        if !policy.is_enabled() {
            return AdmissionFairnessDecision::Disabled;
        }
        let mut state = lock(&self.inner);
        let principal_key = policy
            .requests_per_minute_per_principal
            .map(|_| principal_id.unwrap_or(LEGACY_PRINCIPAL));
        let principal_existing = principal_key
            .as_ref()
            .and_then(|key| state.principals.get(*key).copied());

        if let (Some(_), None) = (principal_key.as_ref(), principal_existing)
            && state.principals.len() >= policy.max_tracked_principals
        {
            let stale = state
                .principals
                .iter()
                .filter(|(_, bucket)| {
                    now.saturating_duration_since(bucket.last_seen) >= STALE_AFTER
                })
                .min_by_key(|(_, bucket)| bucket.last_seen)
                .map(|(id, bucket)| (id.clone(), bucket.last_seen));
            if let Some((stale_id, _)) = stale {
                state.principals.remove(&stale_id);
            } else {
                let retry_after_seconds = state
                    .principals
                    .values()
                    .map(|bucket| {
                        STALE_AFTER
                            .saturating_sub(now.saturating_duration_since(bucket.last_seen))
                            .as_secs()
                            .max(1)
                    })
                    .min()
                    .unwrap_or(1);
                return AdmissionFairnessDecision::Limited {
                    retry_after_seconds,
                    scope: "principal_state_capacity",
                };
            }
        }

        let user_capacity = policy.requests_per_minute_per_principal;
        let mut user_bucket = match (&principal_key, principal_existing, user_capacity) {
            (Some(_), Some(mut bucket), Some(capacity)) => {
                bucket.refill(capacity, now);
                Some(bucket)
            }
            (Some(_), None, Some(capacity)) => Some(Bucket::full(capacity, now)),
            _ => None,
        };
        let mut global_bucket = match (policy.global_requests_per_minute, state.global) {
            (Some(capacity), Some(mut bucket)) => {
                bucket.refill(capacity, now);
                Some(bucket)
            }
            (Some(capacity), None) => Some(Bucket::full(capacity, now)),
            _ => None,
        };

        if let (Some(capacity), Some(bucket)) = (user_capacity, user_bucket)
            && bucket.tokens < 1.0
        {
            return AdmissionFairnessDecision::Limited {
                retry_after_seconds: bucket.retry_after(capacity),
                scope: "principal",
            };
        }
        if let (Some(capacity), Some(bucket)) = (policy.global_requests_per_minute, global_bucket)
            && bucket.tokens < 1.0
        {
            return AdmissionFairnessDecision::Limited {
                retry_after_seconds: bucket.retry_after(capacity),
                scope: "global",
            };
        }

        let mut remaining = u32::MAX;
        if let (Some(key), Some(mut bucket)) = (principal_key, user_bucket.take()) {
            bucket.tokens -= 1.0;
            bucket.last_seen = now;
            remaining = remaining.min(bucket.tokens.floor() as u32);
            state.principals.insert(key.to_owned(), bucket);
        }
        if let Some(mut bucket) = global_bucket.take() {
            bucket.tokens -= 1.0;
            state.global = Some(bucket);
            remaining = remaining.min(bucket.tokens.floor() as u32);
        }
        let scope = match (
            user_capacity.is_some(),
            policy.global_requests_per_minute.is_some(),
        ) {
            (true, true) => "principal,global",
            (true, false) => "principal",
            (false, true) => "global",
            (false, false) => "none",
        };
        AdmissionFairnessDecision::Allowed { remaining, scope }
    }

    #[cfg(test)]
    fn principal_count(&self) -> usize {
        lock(&self.inner).principals.len()
    }
}

fn lock(mutex: &Mutex<LimiterState>) -> MutexGuard<'_, LimiterState> {
    mutex
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

#[cfg(test)]
mod tests {
    use super::{AdmissionFairnessDecision as Decision, AdmissionFairnessLimiter as Limiter, *};
    use std::thread;

    fn policy(per_user: Option<u32>, global: Option<u32>, max: usize) -> AdmissionFairnessPolicy {
        AdmissionFairnessPolicy {
            requests_per_minute_per_principal: per_user,
            global_requests_per_minute: global,
            max_tracked_principals: max,
        }
    }

    #[test]
    fn absent_policy_keeps_the_no_rule_fast_path() {
        let limiter = Limiter::default();
        assert_eq!(
            limiter.admit_at(policy(None, None, 8), Some("u1"), Instant::now()),
            Decision::Disabled
        );
        assert_eq!(limiter.principal_count(), 0);
    }

    #[test]
    fn all_credentials_for_one_principal_share_a_bucket() {
        let limiter = Limiter::default();
        let start = Instant::now();
        let policy = policy(Some(2), None, 8);
        assert!(matches!(
            limiter.admit_at(policy, Some("user-a"), start),
            Decision::Allowed { .. }
        ));
        assert!(matches!(
            limiter.admit_at(policy, Some("user-a"), start),
            Decision::Allowed { .. }
        ));
        assert_eq!(
            limiter.admit_at(policy, Some("user-a"), start),
            Decision::Limited {
                retry_after_seconds: 30,
                scope: "principal"
            }
        );
        assert!(matches!(
            limiter.admit_at(policy, Some("user-b"), start),
            Decision::Allowed { .. }
        ));
    }

    #[test]
    fn global_and_principal_rules_intersect_without_consuming_denied_slots() {
        let limiter = Limiter::default();
        let start = Instant::now();
        let policy = policy(Some(4), Some(1), 8);
        assert!(matches!(
            limiter.admit_at(policy, Some("a"), start),
            Decision::Allowed { .. }
        ));
        assert_eq!(
            limiter.admit_at(policy, Some("a"), start),
            Decision::Limited {
                retry_after_seconds: 60,
                scope: "global"
            }
        );
        assert_eq!(limiter.principal_count(), 1);
        assert_eq!(
            limiter.admit_at(policy, Some("b"), start),
            Decision::Limited {
                retry_after_seconds: 60,
                scope: "global"
            }
        );
        assert_eq!(limiter.principal_count(), 1);
    }

    #[test]
    fn monotonic_refill_admits_again_at_the_window_boundary() {
        let limiter = Limiter::default();
        let start = Instant::now();
        let policy = policy(Some(1), None, 8);
        assert!(matches!(
            limiter.admit_at(policy, Some("a"), start),
            Decision::Allowed { .. }
        ));
        assert!(matches!(
            limiter.admit_at(policy, Some("a"), start + Duration::from_secs(60)),
            Decision::Allowed { .. }
        ));
    }

    #[test]
    fn principal_table_is_bounded_and_evicts_only_fully_stale_state() {
        let limiter = Limiter::default();
        let start = Instant::now();
        let policy = policy(Some(10), None, 2);
        for id in ["a", "b"] {
            assert!(matches!(
                limiter.admit_at(policy, Some(id), start),
                Decision::Allowed { .. }
            ));
        }
        assert_eq!(
            limiter.admit_at(policy, Some("c"), start + Duration::from_secs(1)),
            Decision::Limited {
                retry_after_seconds: 119,
                scope: "principal_state_capacity"
            }
        );
        assert!(matches!(
            limiter.admit_at(policy, Some("c"), start + STALE_AFTER),
            Decision::Allowed { .. }
        ));
        assert_eq!(limiter.principal_count(), 2);
    }

    #[test]
    fn simultaneous_last_slot_is_granted_once() {
        let limiter = Limiter::default();
        let policy = policy(Some(1), None, 8);
        let start = Instant::now();
        let handles = (0..32)
            .map(|_| {
                let limiter = limiter.clone();
                thread::spawn(move || limiter.admit_at(policy, Some("same-user"), start))
            })
            .collect::<Vec<_>>();
        let allowed = handles
            .into_iter()
            .map(|handle| handle.join().expect("worker"))
            .filter(|decision| matches!(decision, Decision::Allowed { .. }))
            .count();
        assert_eq!(allowed, 1);
    }

    #[test]
    fn many_principals_remain_within_declared_capacity() {
        let limiter = Limiter::default();
        let policy = policy(Some(2), None, 49);
        let start = Instant::now();
        for index in 0..100 {
            let _ = limiter.admit_at(policy, Some(&format!("principal-{index}")), start);
        }
        assert_eq!(limiter.principal_count(), 49);
    }

    #[test]
    fn qualification_user_counts_keep_buckets_separate() {
        for principal_count in [1, 5, 25, 49] {
            let limiter = Limiter::default();
            let policy = policy(Some(1), None, principal_count);
            let start = Instant::now();
            for index in 0..principal_count {
                let id = format!("member-{index}");
                assert!(matches!(
                    limiter.admit_at(policy, Some(&id), start),
                    Decision::Allowed { .. }
                ));
                assert!(matches!(
                    limiter.admit_at(policy, Some(&id), start),
                    Decision::Limited {
                        scope: "principal",
                        ..
                    }
                ));
            }
            assert_eq!(limiter.principal_count(), principal_count);
        }
    }
}
