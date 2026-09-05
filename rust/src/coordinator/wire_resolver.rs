//! Process-owned wire candidate ordering and negotiation ownership.
//!
//! The resolver is deliberately unaware of HTTP status codes and response
//! bodies.  Callers report an authorized accept/reject decision after the
//! provider boundary has classified the result.

use std::{
    collections::{BTreeMap, VecDeque},
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};

use tokio::sync::{OwnedSemaphorePermit, Semaphore};

use crate::wire::{ConfiguredWireProfile, WireSurface};

const DEFAULT_CACHE_CAPACITY: usize = 2_048;
const DEFAULT_LEARNED_TTL: Duration = Duration::from_secs(86_400);
const DEFAULT_REJECTION_TTL: Duration = Duration::from_secs(300);
const DEFAULT_NEGOTIATION_INTERVAL: Duration = Duration::from_secs(1);

#[derive(Debug, Clone)]
pub struct WireResolverConfig {
    pub cache_capacity: usize,
    pub learned_ttl: Duration,
    pub rejection_ttl: Duration,
    pub min_negotiation_interval: Duration,
    pub max_concurrent_per_provider: usize,
}

impl Default for WireResolverConfig {
    fn default() -> Self {
        Self {
            cache_capacity: DEFAULT_CACHE_CAPACITY,
            learned_ttl: DEFAULT_LEARNED_TTL,
            rejection_ttl: DEFAULT_REJECTION_TTL,
            min_negotiation_interval: DEFAULT_NEGOTIATION_INTERVAL,
            max_concurrent_per_provider: 1,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WireCandidate {
    pub profile: ConfiguredWireProfile,
    pub fingerprint: String,
}

impl WireCandidate {
    pub fn new(profile: ConfiguredWireProfile, fingerprint: impl Into<String>) -> Self {
        Self {
            profile,
            fingerprint: fingerprint.into(),
        }
    }

    pub fn surface(&self) -> WireSurface {
        self.profile.definition.surface
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WireResolution {
    pub candidates: Vec<WireCandidate>,
    pub fingerprint: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NegotiationRole {
    NotNeeded,
    Leader,
    Follower,
    Throttled,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum NegotiationResult {
    Accepted(WireSurface),
    Rejected,
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
struct CacheKey {
    provider_id: String,
    model_id: String,
    fingerprint: String,
}

#[derive(Debug, Clone)]
struct Learned {
    surface: WireSurface,
    expires_at: Instant,
}

#[derive(Debug, Default)]
struct CacheEntry {
    learned: Option<Learned>,
    rejected_until: BTreeMap<WireSurface, Instant>,
}

#[derive(Debug)]
struct Flight {
    result: Mutex<Option<NegotiationResult>>,
    notify: tokio::sync::Notify,
}

#[derive(Debug, Default)]
struct ResolverState {
    entries: BTreeMap<CacheKey, CacheEntry>,
    lru: VecDeque<CacheKey>,
    flights: BTreeMap<CacheKey, Arc<Flight>>,
    last_negotiation: BTreeMap<String, Instant>,
}

#[derive(Debug)]
pub struct NegotiationLease {
    resolver: WireResolver,
    key: CacheKey,
    role: NegotiationRole,
    permit: Option<OwnedSemaphorePermit>,
    flight: Arc<Flight>,
    finished: bool,
}

impl NegotiationLease {
    pub fn role(&self) -> NegotiationRole {
        self.role
    }

    pub async fn wait(self) -> NegotiationResult {
        if self.role == NegotiationRole::Leader {
            return NegotiationResult::Rejected;
        }
        loop {
            if let Some(result) = self.flight.result.lock().expect("flight lock").clone() {
                return result;
            }
            self.flight.notify.notified().await;
        }
    }

    pub fn finish(mut self, result: NegotiationResult, now: Instant) {
        if self.role != NegotiationRole::Leader {
            return;
        }
        self.finished = true;
        self.resolver
            .finish_leader(&self.key, &self.flight, result, now);
    }
}

impl Drop for NegotiationLease {
    fn drop(&mut self) {
        if self.role == NegotiationRole::Leader && !self.finished {
            self.resolver.cancel_leader(&self.key, &self.flight);
        }
        let _ = self.permit.take();
    }
}

#[derive(Debug, Clone)]
pub struct WireResolver {
    config: WireResolverConfig,
    state: Arc<Mutex<ResolverState>>,
    provider_gates: Arc<Mutex<BTreeMap<String, Arc<Semaphore>>>>,
}

impl WireResolver {
    pub fn new(config: WireResolverConfig) -> Self {
        Self {
            config,
            state: Arc::new(Mutex::new(ResolverState::default())),
            provider_gates: Arc::new(Mutex::new(BTreeMap::new())),
        }
    }

    pub fn resolve(
        &self,
        provider_id: &str,
        model_id: &str,
        mut candidates: Vec<WireCandidate>,
        now: Instant,
    ) -> WireResolution {
        let fingerprint = candidates
            .iter()
            .map(|candidate| candidate.fingerprint.as_str())
            .collect::<Vec<_>>()
            .join("|");
        let key = CacheKey {
            provider_id: provider_id.to_owned(),
            model_id: model_id.to_owned(),
            fingerprint: fingerprint.clone(),
        };
        let mut state = self.state.lock().expect("wire resolver lock");
        let entry = state.entries.entry(key.clone()).or_default();
        entry.rejected_until.retain(|_, until| *until > now);
        let learned = entry
            .learned
            .as_ref()
            .filter(|learned| learned.expires_at > now)
            .map(|learned| learned.surface);
        candidates.retain(|candidate| {
            entry
                .rejected_until
                .get(&candidate.surface())
                .is_none_or(|until| *until <= now)
        });
        candidates.sort_by_key(|candidate| {
            let learned_rank = if Some(candidate.surface()) == learned {
                0
            } else {
                1
            };
            (
                learned_rank,
                candidate.profile.priority,
                candidate.surface(),
            )
        });
        touch_lru(&mut state, key, self.config.cache_capacity);
        WireResolution {
            candidates,
            fingerprint,
        }
    }

    pub async fn begin_negotiation(
        &self,
        provider_id: &str,
        model_id: &str,
        fingerprint: &str,
        now: Instant,
    ) -> NegotiationLease {
        let key = CacheKey {
            provider_id: provider_id.to_owned(),
            model_id: model_id.to_owned(),
            fingerprint: fingerprint.to_owned(),
        };
        let throttled_by_interval = {
            let state = self.state.lock().expect("wire resolver lock");
            !state.flights.contains_key(&key)
                && state.last_negotiation.get(provider_id).is_some_and(|last| {
                    now.saturating_duration_since(*last) < self.config.min_negotiation_interval
                })
        };
        if throttled_by_interval {
            let flight = Arc::new(Flight {
                result: Mutex::new(Some(NegotiationResult::Rejected)),
                notify: tokio::sync::Notify::new(),
            });
            return NegotiationLease {
                resolver: self.clone(),
                key,
                role: NegotiationRole::Throttled,
                permit: None,
                flight,
                finished: true,
            };
        }
        let (flight, role) = {
            let mut state = self.state.lock().expect("wire resolver lock");
            if let Some(flight) = state.flights.get(&key) {
                (Arc::clone(flight), NegotiationRole::Follower)
            } else {
                let flight = Arc::new(Flight {
                    result: Mutex::new(None),
                    notify: tokio::sync::Notify::new(),
                });
                state.flights.insert(key.clone(), Arc::clone(&flight));
                (flight, NegotiationRole::Leader)
            }
        };
        if role == NegotiationRole::Follower {
            return NegotiationLease {
                resolver: self.clone(),
                key,
                role,
                permit: None,
                flight,
                finished: true,
            };
        }
        let gate = {
            let mut gates = self.provider_gates.lock().expect("wire gates lock");
            gates
                .entry(provider_id.to_owned())
                .or_insert_with(|| {
                    Arc::new(Semaphore::new(
                        self.config.max_concurrent_per_provider.max(1),
                    ))
                })
                .clone()
        };
        let permitted = gate.try_acquire_owned();
        let role = if permitted.is_ok() {
            NegotiationRole::Leader
        } else {
            self.cancel_leader(&key, &flight);
            NegotiationRole::Throttled
        };
        if role == NegotiationRole::Throttled {
            return NegotiationLease {
                resolver: self.clone(),
                key,
                role,
                permit: None,
                flight,
                finished: true,
            };
        }
        {
            let mut state = self.state.lock().expect("wire resolver lock");
            state.last_negotiation.insert(provider_id.to_owned(), now);
        }
        NegotiationLease {
            resolver: self.clone(),
            key,
            role,
            permit: permitted.ok(),
            flight,
            finished: false,
        }
    }

    pub fn accept(
        &self,
        provider_id: &str,
        model_id: &str,
        fingerprint: &str,
        surface: WireSurface,
        now: Instant,
    ) {
        self.record_learning(provider_id, model_id, fingerprint, surface, now);
    }

    pub fn reject(
        &self,
        provider_id: &str,
        model_id: &str,
        fingerprint: &str,
        surface: WireSurface,
        now: Instant,
    ) {
        let key = CacheKey {
            provider_id: provider_id.to_owned(),
            model_id: model_id.to_owned(),
            fingerprint: fingerprint.to_owned(),
        };
        let mut state = self.state.lock().expect("wire resolver lock");
        state
            .entries
            .entry(key)
            .or_default()
            .rejected_until
            .insert(surface, now + self.config.rejection_ttl);
    }

    pub fn snapshot_size(&self) -> usize {
        self.state.lock().expect("wire resolver lock").entries.len()
    }

    fn record_learning(
        &self,
        provider_id: &str,
        model_id: &str,
        fingerprint: &str,
        surface: WireSurface,
        now: Instant,
    ) {
        let key = CacheKey {
            provider_id: provider_id.to_owned(),
            model_id: model_id.to_owned(),
            fingerprint: fingerprint.to_owned(),
        };
        self.state
            .lock()
            .expect("wire resolver lock")
            .entries
            .entry(key)
            .or_default()
            .learned = Some(Learned {
            surface,
            expires_at: now + self.config.learned_ttl,
        });
    }

    fn finish_leader(
        &self,
        key: &CacheKey,
        flight: &Arc<Flight>,
        result: NegotiationResult,
        now: Instant,
    ) {
        *flight.result.lock().expect("flight lock") = Some(result.clone());
        flight.notify.notify_waiters();
        let mut state = self.state.lock().expect("wire resolver lock");
        state.flights.remove(key);
        if let NegotiationResult::Accepted(surface) = result {
            state.entries.entry(key.clone()).or_default().learned = Some(Learned {
                surface,
                expires_at: now + self.config.learned_ttl,
            });
        }
    }

    fn cancel_leader(&self, key: &CacheKey, flight: &Arc<Flight>) {
        *flight.result.lock().expect("flight lock") = Some(NegotiationResult::Rejected);
        flight.notify.notify_waiters();
        self.state
            .lock()
            .expect("wire resolver lock")
            .flights
            .remove(key);
    }
}

fn touch_lru(state: &mut ResolverState, key: CacheKey, capacity: usize) {
    state.lru.retain(|existing| existing != &key);
    state.lru.push_back(key);
    while state.entries.len() > capacity.max(1) {
        let Some(oldest) = state.lru.pop_front() else {
            break;
        };
        state.entries.remove(&oldest);
    }
}
